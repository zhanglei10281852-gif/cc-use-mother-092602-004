"""拼车发射方案比较的事务服务。

不变量：
- 报价批次按批次号幂等；同号不同内容拒绝，绝不覆盖已入库报价。
- 每次生成/锁定/解锁/重算都产生新的比较版本，旧版本与已导出报告只读。
- 人工锁定的槽位在重算时原样保留，只重新求解未锁定部分。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.rideshare import engine
from app.rideshare.repository import RideRepository
from app.rideshare.schema import ensure_schema

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

QUOTE_TEXT_FIELDS = ("provider", "vehicle", "launch_site")
QUOTE_QUANTITY_FIELDS = (
    ("capacity_value", "capacity_unit", engine.MASS_TO_KG, "运力", "capacity_kg"),
    ("fairing_length_value", "fairing_length_unit", engine.LENGTH_TO_M, "整流罩长度", "fairing_length_m"),
    ("fairing_width_value", "fairing_width_unit", engine.LENGTH_TO_M, "整流罩宽度", "fairing_width_m"),
    ("fairing_height_value", "fairing_height_unit", engine.LENGTH_TO_M, "整流罩高度", "fairing_height_m"),
)


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def validate_quote_row(row: Any, row_index: int) -> tuple[dict[str, Any] | None, list[str]]:
    """校验单行报价，返回 (归一化行, 错误列表)；有错误时归一化行为 None。"""
    if not isinstance(row, dict):
        return None, ["行内容不是 JSON 对象"]
    errors: list[str] = []
    normalized: dict[str, Any] = {}
    for field in QUOTE_TEXT_FIELDS:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"字段 {field} 缺失或不是非空字符串")
        else:
            normalized[field] = value.strip()
    currency = row.get("currency")
    if not isinstance(currency, str) or not _CURRENCY_RE.match(currency.strip().upper()):
        errors.append("字段 currency 缺失或不是三位币种代码（如 CNY、USD）")
    elif currency.strip().upper() not in engine.ISO_CURRENCIES:
        errors.append(f"字段 currency 不是 ISO 4217 币种代码：{currency}")
    else:
        normalized["currency"] = currency.strip().upper()
    earliest = row.get("earliest_date")
    latest = row.get("latest_date")
    if not _valid_date(earliest):
        errors.append("字段 earliest_date 缺失或不是 YYYY-MM-DD 日期")
    if not _valid_date(latest):
        errors.append("字段 latest_date 缺失或不是 YYYY-MM-DD 日期")
    if _valid_date(earliest) and _valid_date(latest):
        if earliest > latest:  # type: ignore[operator]
            errors.append("最早发射日期晚于最晚发射日期")
        normalized["earliest_date"] = earliest
        normalized["latest_date"] = latest
    for value_field, limits in (("base_price_value", engine.PRICE_LIMITS["base_price"]), ("per_kg_price_value", engine.PRICE_LIMITS["per_kg_price"])):
        raw_number = row.get(value_field)
        if isinstance(raw_number, bool):
            errors.append(f"字段 {value_field} 缺失或不是数字")
            continue
        try:
            number = float(raw_number)
        except (TypeError, ValueError):
            errors.append(f"字段 {value_field} 缺失或不是数字")
            continue
        try:
            engine.check_range(value_field, number, limits)
            normalized[value_field] = number
        except engine.EngineError as exc:
            errors.append(str(exc))
    for value_field, unit_field, table, label, limit_key in QUOTE_QUANTITY_FIELDS:
        raw_value = row.get(value_field)
        raw_unit = row.get(unit_field)
        if raw_unit is None or not str(raw_unit).strip():
            errors.append(f"字段 {unit_field} 缺少单位")
            continue
        try:
            converted, canonical = engine.convert_quantity(raw_value, raw_unit, table, label)
            engine.check_range(label, converted, engine.PRICE_LIMITS[limit_key])
            if converted <= 0:
                raise engine.EngineError(f"{label}必须为正数（归一化后为 {converted:g}）")
            normalized[value_field] = float(raw_value)
            normalized[unit_field] = str(raw_unit).strip()
        except engine.EngineError as exc:
            errors.append(str(exc))
        except (TypeError, ValueError):
            errors.append(f"字段 {value_field} 不是数字")
    if errors:
        return None, errors
    return normalized, []


class RideCompareService:
    """报价导入、载荷需求、候选生成、人工锁定与决策报告。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        ensure_schema()
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 报价导入（按批次号幂等）
    # ------------------------------------------------------------------

    def import_quotes(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        batch_no = str(payload.get("batch_no") or "").strip()
        if not batch_no:
            raise ValidationError("缺少批次号 batch_no")
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValidationError("rows 必须是非空数组")
        supplier = str(payload.get("supplier") or "").strip()
        note = str(payload.get("note") or "").strip()
        source_digest = digest({"batch_no": batch_no, "supplier": supplier, "note": note, "rows": rows})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RideRepository(connection)
            existing = repository.batch_by_no(batch_no)
            if existing is not None:
                if existing["source_digest"] != source_digest:
                    raise ConflictError(
                        f"批次 {batch_no} 已导入且内容不一致；报价不允许覆盖，请使用新批次号",
                        context={"batch_id": existing["id"], "imported_at": existing["imported_at"]},
                    )
                return self._import_result(repository, dict(existing), replayed=True)
            batch = dict(repository.create_batch(batch_no=batch_no, supplier=supplier, note=note,
                                                 source_digest=source_digest, imported_by=actor, now=now))
            accepted: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            for index, raw in enumerate(rows):
                normalized, errors = validate_quote_row(raw, index)
                if normalized is None:
                    repository.insert_row_error(batch_id=batch["id"], batch_no=batch_no, row_index=index,
                                                raw=raw if isinstance(raw, dict) else {"raw": raw}, errors=errors, now=now)
                    rejected.append({"row_index": index, "errors": errors})
                    continue
                row_digest = digest(normalized)
                row_id = repository.insert_quote_row(batch_id=batch["id"], batch_no=batch_no, row_index=index,
                                                     row=normalized, row_digest=row_digest, now=now)
                accepted.append({"row_index": index, "quote_row_id": row_id, "row_digest": row_digest})
            repository.finalize_batch(batch["id"], row_count=len(rows), accepted=len(accepted), rejected=len(rejected))
            repository.add_audit(comparison_id=None, action="quote.import", actor=actor,
                                 reason=f"导入报价批次 {batch_no}", target_type="quote_batch", target_key=batch_no,
                                 before={}, after={"batch_id": batch["id"], "accepted": len(accepted), "rejected": len(rejected)}, now=now)
            batch = dict(repository.batch_by_id(batch["id"]))
            return self._import_result(repository, batch, replayed=False,
                                       accepted=accepted, rejected=rejected)

    @staticmethod
    def _import_result(repository: RideRepository, batch: dict[str, Any], *, replayed: bool,
                       accepted: list[dict[str, Any]] | None = None,
                       rejected: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if accepted is None or rejected is None:
            accepted = [
                {"row_index": row["row_index"], "quote_row_id": row["id"], "row_digest": row["row_digest"]}
                for row in repository.quote_rows(batch["id"])
            ]
            rejected = [
                {"row_index": row["row_index"], "errors": json.loads(row["errors_json"])}
                for row in repository.quote_row_errors(batch["id"])
            ]
        return {
            "batch_id": batch["id"],
            "batch_no": batch["batch_no"],
            "replayed": replayed,
            "row_count": batch["row_count"],
            "accepted_count": batch["accepted_count"],
            "rejected_count": batch["rejected_count"],
            "accepted": accepted,
            "rejected": rejected,
        }

    def list_batches(self, limit: int = 100) -> list[dict[str, Any]]:
        return RideRepository(self.connection).list_batches(limit)

    def get_batch(self, batch_no: str) -> dict[str, Any]:
        repository = RideRepository(self.connection)
        batch = repository.batch_by_no(batch_no)
        if batch is None:
            raise NotFoundError(f"报价批次不存在：{batch_no}")
        result = dict(batch)
        result["rows"] = repository.quote_rows(batch["id"])
        result["row_errors"] = [
            {**row, "raw": json.loads(row["raw_json"]), "errors": json.loads(row["errors_json"])}
            for row in repository.quote_row_errors(batch["id"])
        ]
        for row in result["row_errors"]:
            row.pop("raw_json", None)
            row.pop("errors_json", None)
        return result

    def get_quote_row(self, quote_row_id: int) -> dict[str, Any]:
        repository = RideRepository(self.connection)
        rows = repository.quote_rows_by_ids([quote_row_id])
        if not rows:
            raise NotFoundError(f"报价行不存在：{quote_row_id}")
        row = rows[0]
        batch = repository.batch_by_id(row["batch_id"])
        row["batch"] = {"id": batch["id"], "batch_no": batch["batch_no"], "supplier": batch["supplier"],
                        "imported_by": batch["imported_by"], "imported_at": batch["imported_at"]} if batch else None
        row["normalized"] = {
            "capacity_kg": engine.convert_quantity(row["capacity_value"], row["capacity_unit"], engine.MASS_TO_KG, "运力")[0],
            "fairing_length_m": engine.convert_quantity(row["fairing_length_value"], row["fairing_length_unit"], engine.LENGTH_TO_M, "整流罩长度")[0],
            "fairing_width_m": engine.convert_quantity(row["fairing_width_value"], row["fairing_width_unit"], engine.LENGTH_TO_M, "整流罩宽度")[0],
            "fairing_height_m": engine.convert_quantity(row["fairing_height_value"], row["fairing_height_unit"], engine.LENGTH_TO_M, "整流罩高度")[0],
        }
        return row

    # ------------------------------------------------------------------
    # 载荷需求
    # ------------------------------------------------------------------

    def upsert_requirement(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        required = ("payload_code", "unit_mass_value", "unit_mass_unit", "length_value", "length_unit",
                    "width_value", "width_unit", "height_value", "height_unit", "copies", "redundancy_spares")
        missing = [field for field in required if field not in payload]
        if missing:
            raise ValidationError("载荷需求缺少字段", context={"missing": missing})
        try:
            engine.build_requirement(payload)
        except (engine.EngineError, TypeError, ValueError) as exc:
            raise ValidationError(f"载荷需求校验失败：{exc}") from exc
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RideRepository(connection)
            before = repository.requirement_by_code(payload["payload_code"])
            saved = repository.upsert_requirement(item=payload, actor=actor, now=now)
            repository.add_audit(comparison_id=None, action="requirement.upsert", actor=actor,
                                 reason=f"维护载荷需求 {payload['payload_code']}", target_type="payload_requirement",
                                 target_key=str(payload["payload_code"]),
                                 before=dict(before) if before else {}, after=saved, now=now)
            return saved

    def list_requirements(self) -> list[dict[str, Any]]:
        return RideRepository(self.connection).list_requirements()

    # ------------------------------------------------------------------
    # 比较：创建、版本生成、重算
    # ------------------------------------------------------------------

    def create_comparison(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        comparison_no = str(payload.get("comparison_no") or "").strip()
        if not comparison_no:
            raise ValidationError("缺少比较编号 comparison_no")
        deadline = str(payload.get("deadline") or "").strip()
        if deadline and not _valid_date(deadline):
            raise ValidationError("deadline 必须是 YYYY-MM-DD 日期")
        batch_nos = [str(item).strip() for item in payload.get("quote_batch_nos") or []]
        payload_codes = [str(item).strip() for item in payload.get("payload_codes") or []]
        if not batch_nos:
            raise ValidationError("至少选择一个报价批次 quote_batch_nos")
        if not payload_codes:
            raise ValidationError("至少选择一个载荷需求 payload_codes")
        title = str(payload.get("title") or "").strip()
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RideRepository(connection)
            if repository.comparison_by_no(comparison_no) is not None:
                raise ConflictError(f"比较编号已存在：{comparison_no}")
            batch_ids = self._resolve_batches(repository, batch_nos)
            self._resolve_requirements(repository, payload_codes)
            comparison = repository.create_comparison(comparison_no=comparison_no, title=title, deadline=deadline,
                                                      quote_batch_ids=batch_ids, actor=actor, now=now)
            self._set_payload_codes(connection, comparison["id"], payload_codes)
            comparison["payload_codes_json"] = json.dumps(sorted(set(payload_codes)), ensure_ascii=False)
            try:
                version = self._new_version(repository, comparison, trigger="generate", actor=actor, now=now)
            except engine.EngineError as exc:
                raise ValidationError(f"候选组合生成失败：{exc}") from exc
            repository.add_audit(comparison_id=comparison["id"], action="comparison.create", actor=actor,
                                 reason="创建比较并生成首个版本", target_type="comparison", target_key=comparison_no,
                                 before={}, after={"comparison_no": comparison_no, "version": version["version"]}, now=now)
            return self.get_comparison(comparison_no, connection=connection)

    def recompute(self, comparison_no: str, *, actor: str, reason: str, quote_batch_nos: list[str] | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RideRepository(connection)
            comparison = self._must_comparison(repository, comparison_no)
            if quote_batch_nos is not None:
                batch_ids = self._resolve_batches(repository, [str(item).strip() for item in quote_batch_nos])
                comparison["quote_batch_ids_json"] = json.dumps(sorted(batch_ids), ensure_ascii=False)
                connection.execute("UPDATE ride_comparisons SET quote_batch_ids_json=?,updated_at=? WHERE id=?",
                                   (comparison["quote_batch_ids_json"], now, comparison["id"]))
            try:
                version = self._new_version(repository, comparison, trigger="recompute", actor=actor, now=now)
            except engine.EngineError as exc:
                raise ConflictError(f"重算失败：{exc}") from exc
            repository.add_audit(comparison_id=comparison["id"], action="comparison.recompute", actor=actor,
                                 reason=reason, target_type="comparison", target_key=comparison_no,
                                 before={}, after={"version": version["version"], "input_digest": version["input_digest"]}, now=now)
            return self.get_version(comparison_no, version["version"], connection=connection)

    def _new_version(self, repository: RideRepository, comparison: dict[str, Any], *, trigger: str,
                     actor: str, now: str) -> dict[str, Any]:
        comparison_id = int(comparison["id"])
        batch_ids = json.loads(comparison["quote_batch_ids_json"])
        payload_codes = json.loads(comparison.get("payload_codes_json") or "[]")
        batches = [dict(repository.batch_by_id(batch_id)) for batch_id in batch_ids]
        quote_rows: list[dict[str, Any]] = []
        for batch_id in batch_ids:
            quote_rows.extend(repository.quote_rows(batch_id))
        requirements = self._resolve_requirements(repository, payload_codes)
        locks = repository.active_locks(comparison_id)
        fixed_slots: dict[int, list[str]] = {int(lock["quote_row_id"]): list(json.loads(lock["units_json"])) for lock in locks}
        in_scope = {row["id"] for row in quote_rows}
        out_of_scope = sorted(row_id for row_id in fixed_slots if row_id not in in_scope)
        if out_of_scope:
            raise engine.EngineError(f"人工锁定的报价行不在当前报价范围内：{out_of_scope}，请先解除锁定或把对应批次纳入比较")
        offers = [engine.build_offer(row) for row in quote_rows]
        requirement_objs = [engine.build_requirement(item) for item in requirements]
        candidates = engine.generate_candidates(
            offers, requirement_objs,
            deadline=comparison["deadline"] or None,
            fixed_slots=fixed_slots,
        )
        locked_ids = set(fixed_slots)
        candidate_dicts = [engine.candidate_to_dict(candidate, fixed_slots=fixed_slots) for candidate in candidates]
        snapshot = {
            "comparison_no": comparison["comparison_no"],
            "deadline": comparison["deadline"],
            "quote_batches": [
                {
                    "id": batch["id"],
                    "batch_no": batch["batch_no"],
                    "supplier": batch["supplier"],
                    "source_digest": batch["source_digest"],
                    "imported_by": batch["imported_by"],
                    "imported_at": batch["imported_at"],
                }
                for batch in batches
            ],
            "quote_rows": quote_rows,
            "requirements": requirements,
            "fixed_slots": {str(key): value for key, value in sorted(fixed_slots.items())},
        }
        feasible = [item for item in candidate_dicts if item["satisfies_deadline"]]
        summary = {
            "candidate_count": len(candidate_dicts),
            "feasible_count": len(feasible),
            "best": feasible[0] if feasible else (candidate_dicts[0] if candidate_dicts else None),
            "locked_quote_row_ids": sorted(locked_ids),
        }
        version_no = int(comparison["current_version"]) + 1
        version = repository.insert_version(
            comparison_id=comparison_id, version=version_no, trigger=trigger, engine_version=engine.ENGINE_VERSION,
            input_digest=digest(snapshot), input_snapshot=snapshot, fixed_slots={str(k): v for k, v in sorted(fixed_slots.items())},
            candidates=candidate_dicts, summary=summary, actor=actor, now=now,
        )
        status = "locked" if locks else "draft"
        repository.touch_comparison(comparison_id, current_version=version_no, status=status, now=now)
        comparison["current_version"] = version_no
        comparison["status"] = status
        return version

    # ------------------------------------------------------------------
    # 人工锁定与解锁
    # ------------------------------------------------------------------

    def lock(self, comparison_no: str, *, quote_row_id: int, units: list[str], actor: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("锁定必须填写原因")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RideRepository(connection)
            comparison = self._must_comparison(repository, comparison_no)
            in_scope = {row["id"] for batch_id in json.loads(comparison["quote_batch_ids_json"])
                        for row in repository.quote_rows(batch_id)}
            if quote_row_id not in in_scope:
                raise ValidationError(f"报价行 {quote_row_id} 不在比较 {comparison_no} 的报价范围内")
            payload_codes = set(json.loads(comparison["payload_codes_json"]))
            unknown = sorted(set(units) - payload_codes)
            if unknown:
                raise ValidationError("锁定槽位包含比较外的载荷编码", context={"unknown": unknown})
            if not units:
                raise ValidationError("锁定槽位不能为空")
            before = repository.lock_row(comparison["id"], quote_row_id)
            lock = repository.upsert_lock(comparison_id=comparison["id"], quote_row_id=quote_row_id,
                                          units=units, actor=actor, reason=reason, now=now)
            try:
                version = self._new_version(repository, comparison, trigger="lock", actor=actor, now=now)
            except engine.EngineError as exc:
                raise ConflictError(f"锁定后组合不可行：{exc}") from exc
            repository.add_audit(comparison_id=comparison["id"], action="comparison.lock", actor=actor, reason=reason,
                                 target_type="quote_row", target_key=str(quote_row_id),
                                 before=dict(before) if before else {}, after=lock, now=now)
            result = self.get_version(comparison_no, version["version"], connection=connection)
            result["lock"] = lock
            return result

    def unlock(self, comparison_no: str, *, quote_row_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RideRepository(connection)
            comparison = self._must_comparison(repository, comparison_no)
            before = repository.lock_row(comparison["id"], quote_row_id)
            revoked = repository.revoke_lock(comparison_id=comparison["id"], quote_row_id=quote_row_id, now=now)
            if revoked is None:
                raise NotFoundError(f"报价行 {quote_row_id} 在比较 {comparison_no} 中没有生效的锁定")
            try:
                version = self._new_version(repository, comparison, trigger="unlock", actor=actor, now=now)
            except engine.EngineError as exc:
                raise ConflictError(f"解锁后重算失败：{exc}") from exc
            repository.add_audit(comparison_id=comparison["id"], action="comparison.unlock", actor=actor, reason=reason,
                                 target_type="quote_row", target_key=str(quote_row_id),
                                 before=dict(before) if before else {}, after=revoked, now=now)
            return self.get_version(comparison_no, version["version"], connection=connection)

    # ------------------------------------------------------------------
    # 决策报告（按比较版本幂等）
    # ------------------------------------------------------------------

    def export_report(self, comparison_no: str, *, actor: str, title: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RideRepository(connection)
            comparison = self._must_comparison(repository, comparison_no)
            current_version = int(comparison["current_version"])
            if current_version < 1:
                raise ConflictError("比较还没有任何计算版本，无法导出报告")
            existing = repository.report_by_comparison_version(comparison["id"], current_version)
            if existing is not None:
                return self._report_payload(dict(existing), replayed=True)
            version_row = repository.version_row(comparison["id"], current_version)
            locks = repository.active_locks(comparison["id"])
            report_version = repository.next_report_version(comparison["id"])
            content = {
                "comparison_no": comparison_no,
                "title": title or f"{comparison_no} 决策报告",
                "report_version": report_version,
                "comparison_version": current_version,
                "engine_version": version_row["engine_version"],
                "input_digest": version_row["input_digest"],
                "generated_by": actor,
                "generated_at": now,
                "deadline": comparison["deadline"],
                "locks": [{**lock, "units": json.loads(lock["units_json"])} for lock in locks],
                "summary": json.loads(version_row["summary_json"]),
                "candidates": json.loads(version_row["candidates_json"]),
                "inputs": json.loads(version_row["input_snapshot_json"]),
            }
            content_digest = digest(content)
            report = repository.insert_report(
                comparison_id=comparison["id"], report_version=report_version, comparison_version=current_version,
                engine_version=version_row["engine_version"], title=content["title"], content=content,
                content_digest=content_digest, actor=actor, now=now,
            )
            repository.add_audit(comparison_id=comparison["id"], action="report.export", actor=actor,
                                 reason=f"导出决策报告 v{report_version}", target_type="report",
                                 target_key=f"{comparison_no}#{report_version}",
                                 before={}, after={"report_version": report_version, "comparison_version": current_version,
                                                   "content_digest": content_digest}, now=now)
            return self._report_payload(report, replayed=False)

    @staticmethod
    def _report_payload(report: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
        content = json.loads(report["content_json"])
        return {
            "id": report["id"],
            "comparison_id": report["comparison_id"],
            "report_version": report["report_version"],
            "comparison_version": report["comparison_version"],
            "engine_version": report["engine_version"],
            "title": report["title"],
            "content_digest": report["content_digest"],
            "exported_by": report["exported_by"],
            "created_at": report["created_at"],
            "replayed": replayed,
            "content": content,
        }

    def list_reports(self, comparison_no: str) -> list[dict[str, Any]]:
        repository = RideRepository(self.connection)
        comparison = self._must_comparison(repository, comparison_no)
        return repository.list_reports(comparison["id"])

    def get_report(self, comparison_no: str, report_version: int) -> dict[str, Any]:
        repository = RideRepository(self.connection)
        comparison = self._must_comparison(repository, comparison_no)
        report = repository.report_by_version(comparison["id"], report_version)
        if report is None:
            raise NotFoundError(f"报告不存在：{comparison_no} v{report_version}")
        return self._report_payload(dict(report), replayed=True)

    # ------------------------------------------------------------------
    # 查询与追溯
    # ------------------------------------------------------------------

    def list_comparisons(self, limit: int = 100) -> list[dict[str, Any]]:
        return RideRepository(self.connection).list_comparisons(limit)

    def get_comparison(self, comparison_no: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        repository = RideRepository(connection or self.connection)
        comparison = self._must_comparison(repository, comparison_no)
        result = dict(comparison)
        result["quote_batch_ids"] = json.loads(result.pop("quote_batch_ids_json"))
        result["payload_codes"] = json.loads(result.pop("payload_codes_json") or "[]")
        result["locks"] = [
            {**lock, "units": json.loads(lock["units_json"])} for lock in repository.active_locks(comparison["id"])
        ]
        for lock in result["locks"]:
            lock.pop("units_json", None)
        result["versions"] = repository.list_versions(comparison["id"])
        return result

    def get_version(self, comparison_no: str, version: int, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        repository = RideRepository(connection or self.connection)
        comparison = self._must_comparison(repository, comparison_no)
        row = repository.version_row(comparison["id"], version)
        if row is None:
            raise NotFoundError(f"比较 {comparison_no} 不存在版本 {version}")
        result = dict(row)
        result["comparison_no"] = comparison_no
        result["input_snapshot"] = json.loads(result.pop("input_snapshot_json"))
        result["fixed_slots"] = json.loads(result.pop("fixed_slots_json"))
        result["candidates"] = json.loads(result.pop("candidates_json"))
        result["summary"] = json.loads(result.pop("summary_json"))
        return result

    def list_audit(self, comparison_no: str, limit: int = 200) -> list[dict[str, Any]]:
        repository = RideRepository(self.connection)
        comparison = self._must_comparison(repository, comparison_no)
        rows = repository.list_audit(comparison["id"], limit)
        for row in rows:
            row["before"] = json.loads(row.pop("before_json"))
            row["after"] = json.loads(row.pop("after_json"))
        return rows

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _must_comparison(repository: RideRepository, comparison_no: str) -> dict[str, Any]:
        comparison = repository.comparison_by_no(comparison_no)
        if comparison is None:
            raise NotFoundError(f"比较不存在：{comparison_no}")
        return dict(comparison)

    @staticmethod
    def _resolve_batches(repository: RideRepository, batch_nos: list[str]) -> list[int]:
        ids: list[int] = []
        missing: list[str] = []
        for batch_no in dict.fromkeys(batch_nos):
            batch = repository.batch_by_no(batch_no)
            if batch is None:
                missing.append(batch_no)
            else:
                ids.append(int(batch["id"]))
        if missing:
            raise ValidationError("报价批次不存在", context={"missing": missing})
        return sorted(ids)

    @staticmethod
    def _resolve_requirements(repository: RideRepository, payload_codes: list[str]) -> list[dict[str, Any]]:
        requirements = repository.requirements_by_codes(payload_codes)
        found = {item["payload_code"] for item in requirements}
        missing = sorted(set(payload_codes) - found)
        if missing:
            raise ValidationError("载荷需求不存在", context={"missing": missing})
        return requirements

    @staticmethod
    def _set_payload_codes(connection: sqlite3.Connection, comparison_id: int, payload_codes: list[str]) -> None:
        connection.execute(
            "UPDATE ride_comparisons SET payload_codes_json=? WHERE id=?",
            (json.dumps(sorted(set(payload_codes)), ensure_ascii=False), comparison_id),
        )
