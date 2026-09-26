from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Iterable

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.procurement.repository import ProcurementRepository

ALGORITHM_VERSION = "combo-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS proc_quote_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    content_digest TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    imported_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proc_quote_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES proc_quote_batches(id) ON DELETE RESTRICT,
    line_no INTEGER NOT NULL,
    provider TEXT NOT NULL,
    vehicle TEXT NOT NULL,
    launch_date TEXT NOT NULL,
    price_amount REAL NOT NULL CHECK(price_amount > 0),
    price_currency TEXT NOT NULL,
    price_unit TEXT NOT NULL CHECK(price_unit IN ('per_kg','total')),
    capacity_kg REAL NOT NULL CHECK(capacity_kg > 0),
    envelope_length_m REAL NOT NULL CHECK(envelope_length_m > 0),
    envelope_width_m REAL NOT NULL CHECK(envelope_width_m > 0),
    envelope_height_m REAL NOT NULL CHECK(envelope_height_m > 0),
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, line_no),
    UNIQUE(batch_id, provider, vehicle, launch_date)
);
CREATE INDEX IF NOT EXISTS idx_proc_lines_key ON proc_quote_lines(provider, vehicle, launch_date);
CREATE TABLE IF NOT EXISTS proc_requirements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    sat_length_m REAL NOT NULL CHECK(sat_length_m > 0),
    sat_width_m REAL NOT NULL CHECK(sat_width_m > 0),
    sat_height_m REAL NOT NULL CHECK(sat_height_m > 0),
    sat_mass_kg REAL NOT NULL CHECK(sat_mass_kg > 0),
    quantity INTEGER NOT NULL CHECK(quantity >= 1),
    redundant_quantity INTEGER NOT NULL DEFAULT 0 CHECK(redundant_quantity >= 0),
    latest_delivery_date TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proc_calc_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER NOT NULL REFERENCES proc_requirements(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    requirement_version INTEGER NOT NULL,
    requirement_snapshot_json TEXT NOT NULL,
    quote_set_digest TEXT NOT NULL,
    quote_line_count INTEGER NOT NULL,
    quote_batches_json TEXT NOT NULL DEFAULT '[]',
    candidate_count INTEGER NOT NULL DEFAULT 0,
    diagnostics_json TEXT NOT NULL DEFAULT '{}',
    triggered_by TEXT NOT NULL,
    trigger_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(requirement_id, version)
);
CREATE TABLE IF NOT EXISTS proc_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES proc_calc_runs(id) ON DELETE CASCADE,
    requirement_id INTEGER NOT NULL REFERENCES proc_requirements(id) ON DELETE CASCADE,
    combo_key TEXT NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','locked','stale')),
    carried_from_candidate_id INTEGER REFERENCES proc_candidates(id),
    currency TEXT NOT NULL,
    satellites INTEGER NOT NULL,
    total_mass_kg REAL NOT NULL,
    total_cost REAL NOT NULL,
    cost_per_kg REAL NOT NULL,
    cost_per_satellite REAL NOT NULL,
    breakdown_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    locked_by TEXT,
    locked_at TEXT,
    lock_reason TEXT,
    unlocked_by TEXT,
    unlocked_at TEXT,
    unlock_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, combo_key)
);
CREATE INDEX IF NOT EXISTS idx_proc_candidates_req ON proc_candidates(requirement_id, status);
CREATE TABLE IF NOT EXISTS proc_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER NOT NULL REFERENCES proc_requirements(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    run_id INTEGER NOT NULL REFERENCES proc_calc_runs(id),
    content_json TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    exported_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(requirement_id, version)
);
CREATE TABLE IF NOT EXISTS proc_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proc_events_entity ON proc_events(entity_type, entity_id);
"""

PRICE_UNITS = ("per_kg", "total")
MAX_PRICE_AMOUNT = 1_000_000_000_000.0
MAX_CAPACITY_KG = 1_000_000.0
MAX_DIMENSION_M = 60.0
MIN_LAUNCH_DATE = date(2020, 1, 1)
MAX_LAUNCH_DATE = date(2100, 12, 31)
MAX_CANDIDATES_PER_RUN = 50
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")

MONEY_PLACES = Decimal("0.01")
UNIT_PRICE_PLACES = Decimal("0.000001")

FORMULA_TEXT = (
    "unit_price = price_amount（per_kg）或 price_amount ÷ capacity_kg（total），保留 6 位小数；"
    "line_cost = allocated_kg × unit_price，保留 2 位小数；"
    "total_cost = Σ line_cost；cost_per_kg = total_cost ÷ total_mass_kg；"
    "cost_per_satellite = total_cost ÷ satellites"
)


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _quantize(value: Decimal, places: Decimal) -> Decimal:
    return value.quantize(places, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> float:
    return float(_quantize(value, MONEY_PLACES))


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# ---------------------------------------------------------------------------
# 报价行校验：逐行收集错误，任何错误都会导致整个批次被拒绝（保证幂等重放安全）
# ---------------------------------------------------------------------------

def _err(index: int, field: str | None, code: str, message: str) -> dict[str, Any]:
    return {"row": index, "field": field, "code": code, "message": message}


def _text_field(row: dict[str, Any], field: str, index: int, errors: list[dict[str, Any]], max_length: int = 120) -> str | None:
    value = row.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(_err(index, field, "missing_field", f"缺少必填字段 {field}"))
        return None
    if not isinstance(value, str):
        errors.append(_err(index, field, "invalid_type", f"{field} 必须是字符串"))
        return None
    text = value.strip()
    if len(text) > max_length:
        errors.append(_err(index, field, "out_of_range", f"{field} 长度不能超过 {max_length} 个字符"))
        return None
    return text


def _number_field(row: dict[str, Any], field: str, index: int, errors: list[dict[str, Any]], *, minimum: float, maximum: float) -> float | None:
    value = row.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(_err(index, field, "missing_field", f"缺少必填字段 {field}"))
        return None
    if isinstance(value, bool):
        errors.append(_err(index, field, "invalid_type", f"{field} 必须是数值"))
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            errors.append(_err(index, field, "invalid_type", f"{field} 必须是数值"))
            return None
    else:
        errors.append(_err(index, field, "invalid_type", f"{field} 必须是数值"))
        return None
    if not math.isfinite(number):
        errors.append(_err(index, field, "out_of_range", f"{field} 必须是有限数值"))
        return None
    if number <= minimum or number > maximum:
        errors.append(_err(index, field, "out_of_range", f"{field} 必须大于 {minimum} 且不超过 {maximum}"))
        return None
    return number


def _date_field(row: dict[str, Any], field: str, index: int, errors: list[dict[str, Any]]) -> str | None:
    value = row.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(_err(index, field, "missing_field", f"缺少必填字段 {field}"))
        return None
    if not isinstance(value, str):
        errors.append(_err(index, field, "invalid_type", f"{field} 必须是 YYYY-MM-DD 格式的字符串"))
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        errors.append(_err(index, field, "invalid_value", f"{field} 必须是有效的 YYYY-MM-DD 日期"))
        return None
    if parsed < MIN_LAUNCH_DATE or parsed > MAX_LAUNCH_DATE:
        errors.append(_err(index, field, "out_of_range", f"{field} 必须在 {MIN_LAUNCH_DATE.isoformat()} 与 {MAX_LAUNCH_DATE.isoformat()} 之间"))
        return None
    return parsed.isoformat()


def _price_unit_field(row: dict[str, Any], index: int, errors: list[dict[str, Any]]) -> str | None:
    value = row.get("price_unit")
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(_err(index, "price_unit", "missing_unit", "缺少计价单位（per_kg 每公斤单价 或 total 整包总价）"))
        return None
    if not isinstance(value, str):
        errors.append(_err(index, "price_unit", "invalid_type", "price_unit 必须是字符串"))
        return None
    unit = value.strip().lower()
    if unit not in PRICE_UNITS:
        errors.append(_err(index, "price_unit", "invalid_value", f"price_unit 只能是 {PRICE_UNITS} 之一"))
        return None
    return unit


def _currency_field(row: dict[str, Any], index: int, errors: list[dict[str, Any]]) -> str | None:
    value = row.get("price_currency")
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(_err(index, "price_currency", "missing_field", "缺少必填字段 price_currency"))
        return None
    if not isinstance(value, str):
        errors.append(_err(index, "price_currency", "invalid_type", "price_currency 必须是字符串"))
        return None
    currency = value.strip().upper()
    if not CURRENCY_PATTERN.match(currency):
        errors.append(_err(index, "price_currency", "invalid_value", "price_currency 必须是三位字母币种代码（如 USD、CNY、EUR）"))
        return None
    return currency


def validate_quote_row(row: Any, index: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """校验单个报价行，返回规范化后的行与逐条错误。"""
    if not isinstance(row, dict):
        return None, [_err(index, None, "invalid_type", "报价行必须是对象")]
    errors: list[dict[str, Any]] = []
    provider = _text_field(row, "provider", index, errors)
    vehicle = _text_field(row, "vehicle", index, errors)
    launch_date = _date_field(row, "launch_date", index, errors)
    price_amount = _number_field(row, "price_amount", index, errors, minimum=0.0, maximum=MAX_PRICE_AMOUNT)
    price_currency = _currency_field(row, index, errors)
    price_unit = _price_unit_field(row, index, errors)
    capacity_kg = _number_field(row, "capacity_kg", index, errors, minimum=0.0, maximum=MAX_CAPACITY_KG)
    envelope_length = _number_field(row, "envelope_length_m", index, errors, minimum=0.0, maximum=MAX_DIMENSION_M)
    envelope_width = _number_field(row, "envelope_width_m", index, errors, minimum=0.0, maximum=MAX_DIMENSION_M)
    envelope_height = _number_field(row, "envelope_height_m", index, errors, minimum=0.0, maximum=MAX_DIMENSION_M)
    if errors:
        return None, errors
    normalized = {
        "provider": provider,
        "vehicle": vehicle,
        "launch_date": launch_date,
        "price_amount": price_amount,
        "price_currency": price_currency,
        "price_unit": price_unit,
        "capacity_kg": capacity_kg,
        "envelope_length_m": envelope_length,
        "envelope_width_m": envelope_width,
        "envelope_height_m": envelope_height,
    }
    return normalized, []


# ---------------------------------------------------------------------------
# 候选组合生成（combo-v1）：约束过滤 + 三种确定性策略 + 逐行成本分解
# ---------------------------------------------------------------------------

def _unit_price_of(line: dict[str, Any]) -> Decimal:
    price = Decimal(str(line["price_amount"]))
    if line["price_unit"] == "per_kg":
        return _quantize(price, UNIT_PRICE_PLACES)
    return _quantize(price / Decimal(str(line["capacity_kg"])), UNIT_PRICE_PLACES)


def _fits(sat_dims: list[float], envelope_dims: list[float]) -> bool:
    return all(sat <= env + 1e-9 for sat, env in zip(sorted(sat_dims), sorted(envelope_dims)))


StrategyKey = Callable[[dict[str, Any]], Any]

STRATEGIES: tuple[tuple[str, StrategyKey], ...] = (
    ("lowest_unit_price", lambda line: (line["_unit_price"], line["launch_date"], line["id"])),
    ("fewest_launches", lambda line: (-line["capacity_kg"], line["_unit_price"], line["id"])),
    ("earliest_delivery", lambda line: (line["launch_date"], line["_unit_price"], line["id"])),
)


class ProcurementService:
    """报价批次、载荷需求、候选组合、锁定与版本化决策报告的事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = ProcurementRepository(self.connection)
        ensure_schema()

    # ---- 报价批次 ----

    def import_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch_no = payload["batch_no"]
        normalized: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen_keys: dict[tuple[str, str, str], int] = {}
        for index, raw_row in enumerate(payload["rows"]):
            cleaned, row_errors = validate_quote_row(raw_row, index)
            if row_errors:
                errors.extend(row_errors)
                continue
            assert cleaned is not None
            natural_key = (cleaned["provider"], cleaned["vehicle"], cleaned["launch_date"])
            if natural_key in seen_keys:
                errors.append(_err(index, "launch_date", "duplicate_in_batch", f"与第 {seen_keys[natural_key]} 行重复（同一供应商、型号与发射日期）"))
                continue
            seen_keys[natural_key] = index
            normalized.append(cleaned)
        if errors:
            raise ValidationError("报价批次校验失败，未导入任何行", context={"batch_no": batch_no, "errors": errors})
        batch_digest = _digest({"rows": normalized})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcurementRepository(connection)
            existing = repository.batch_by_no(batch_no)
            if existing is not None:
                if existing["content_digest"] == batch_digest:
                    return self._batch_payload(repository, existing, replayed=True)
                raise ConflictError(
                    "报价批次号已存在且内容不一致；报价更新必须使用新的批次号，已导入批次不可改写",
                    context={"batch_no": batch_no},
                )
            batch = repository.create_batch(
                batch_no=batch_no, source=payload["source"], note=payload.get("note", ""),
                content_digest=batch_digest, line_count=len(normalized),
                imported_by=payload["imported_by"], now=now,
            )
            for line_no, cleaned in enumerate(normalized):
                repository.create_line(batch_id=batch["id"], line_no=line_no, row=cleaned, content_digest=_digest(cleaned), now=now)
            repository.add_event(
                entity_type="quote_batch", entity_id=batch_no, action="import", actor=payload["imported_by"],
                before=None, after={"batch_no": batch_no, "line_count": len(normalized), "content_digest": batch_digest}, now=now,
            )
            return self._batch_payload(repository, repository.batch_by_no(batch_no), replayed=False)

    def list_batches(self) -> list[dict[str, Any]]:
        return [_row_to_dict(row) for row in self.repository.list_batches()]

    def get_batch(self, batch_no: str) -> dict[str, Any]:
        batch = self.repository.batch_by_no(batch_no)
        if batch is None:
            raise NotFoundError("报价批次不存在")
        return self._batch_payload(self.repository, batch, replayed=False)

    def current_quotes(self) -> list[dict[str, Any]]:
        return [_row_to_dict(row) for row in self.repository.current_lines()]

    def _batch_payload(self, repository: ProcurementRepository, batch: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        payload = _row_to_dict(batch)
        payload["replayed"] = replayed
        payload["lines"] = [_row_to_dict(row) for row in repository.lines_for_batch(batch["id"])]
        return payload

    # ---- 载荷需求 ----

    def create_requirement(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcurementRepository(connection)
            if repository.requirement_by_code(payload["code"]) is not None:
                raise ConflictError("载荷需求编码已存在")
            requirement = repository.create_requirement(payload=payload, now=now)
            repository.add_event(
                entity_type="requirement", entity_id=str(requirement["id"]), action="create",
                actor=payload["created_by"], before=None, after=_row_to_dict(requirement), now=now,
            )
            return _row_to_dict(requirement)

    def list_requirements(self) -> list[dict[str, Any]]:
        return [_row_to_dict(row) for row in self.repository.list_requirements()]

    def get_requirement(self, requirement_id: int) -> dict[str, Any]:
        requirement = self.repository.requirement_by_id(requirement_id)
        if requirement is None:
            raise NotFoundError("载荷需求不存在")
        payload = _row_to_dict(requirement)
        payload["runs"] = [self._run_summary(row) for row in self.repository.runs_for_requirement(requirement_id)]
        return payload

    def update_requirement(self, requirement_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        actor = payload["actor"]
        reason = payload["reason"]
        changes = {
            key: value
            for key, value in payload.items()
            if key in {"name", "sat_length_m", "sat_width_m", "sat_height_m", "sat_mass_kg", "quantity", "redundant_quantity", "latest_delivery_date"}
            and value is not None
        }
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcurementRepository(connection)
            requirement = repository.requirement_by_id(requirement_id)
            if requirement is None:
                raise NotFoundError("载荷需求不存在")
            if not changes:
                return _row_to_dict(requirement)
            before = _row_to_dict(requirement)
            updated = repository.update_requirement(requirement_id, changes, now)
            repository.add_event(
                entity_type="requirement", entity_id=str(requirement_id), action="update",
                actor=actor, before=before, after={**_row_to_dict(updated), "reason": reason}, now=now,
            )
            return _row_to_dict(updated)

    # ---- 候选组合计算 ----

    def calculate(self, requirement_id: int, actor: str, reason: str = "") -> dict[str, Any]:
        """生成新的计算版本：锁定组合原样结转，未锁定部分按当前报价簿重新计算。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcurementRepository(connection)
            requirement = repository.requirement_by_id(requirement_id)
            if requirement is None:
                raise NotFoundError("载荷需求不存在")
            current_lines = [_row_to_dict(row) for row in repository.current_lines()]
            previous = repository.latest_run(requirement_id)
            version = 1 if previous is None else int(previous["version"]) + 1
            carried = [] if previous is None else [self._candidate_payload(repository, row) for row in repository.locked_candidates(previous["id"])]
            quote_set_digest = _digest([{"id": line["id"], "digest": line["content_digest"]} for line in current_lines])
            quote_batches = self._quote_batches_of(current_lines)
            candidates, diagnostics = self._generate_candidates(requirement, current_lines, quote_set_digest, version)
            carried_keys = {candidate["combo_key"] for candidate in carried}
            candidates = [candidate for candidate in candidates if candidate["combo_key"] not in carried_keys]
            if previous is not None:
                repository.mark_unlocked_stale(previous["id"])
            run = repository.create_run(
                requirement_id=requirement_id, version=version, algorithm_version=ALGORITHM_VERSION,
                requirement_version=requirement["version"], requirement_snapshot=_row_to_dict(requirement),
                quote_set_digest=quote_set_digest, quote_line_count=len(current_lines), quote_batches=quote_batches,
                candidate_count=len(carried) + len(candidates), diagnostics=diagnostics,
                triggered_by=actor, trigger_reason=reason, now=now,
            )
            for locked in carried:
                repository.create_candidate(
                    run_id=run["id"], requirement_id=requirement_id,
                    candidate={key: locked[key] for key in ("combo_key", "strategy", "currency", "satellites", "total_mass_kg", "total_cost", "cost_per_kg", "cost_per_satellite", "breakdown", "provenance")},
                    status="locked", carried_from=locked["id"],
                    lock={"locked_by": locked["locked_by"], "locked_at": locked["locked_at"], "lock_reason": locked["lock_reason"]},
                    now=now,
                )
            for candidate in candidates:
                repository.create_candidate(run_id=run["id"], requirement_id=requirement_id, candidate=candidate, status="proposed", carried_from=None, lock=None, now=now)
            repository.add_event(
                entity_type="calculation", entity_id=str(run["id"]), action="run", actor=actor,
                before=None,
                after={"requirement_id": requirement_id, "version": version, "candidate_count": len(carried) + len(candidates), "carried_locked": len(carried), "quote_set_digest": quote_set_digest},
                now=now,
            )
            return self._run_payload(repository, repository.run_by_id(run["id"]))

    def list_runs(self, requirement_id: int) -> list[dict[str, Any]]:
        if self.repository.requirement_by_id(requirement_id) is None:
            raise NotFoundError("载荷需求不存在")
        return [self._run_summary(row) for row in self.repository.runs_for_requirement(requirement_id)]

    def get_run(self, run_id: int) -> dict[str, Any]:
        run = self.repository.run_by_id(run_id)
        if run is None:
            raise NotFoundError("计算版本不存在")
        return self._run_payload(self.repository, run)

    def get_candidate(self, candidate_id: int) -> dict[str, Any]:
        candidate = self.repository.candidate_by_id(candidate_id)
        if candidate is None:
            raise NotFoundError("候选组合不存在")
        return self._candidate_payload(self.repository, candidate)

    def _generate_candidates(self, requirement: sqlite3.Row, lines: list[dict[str, Any]], quote_set_digest: str, run_version: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        needed = int(requirement["quantity"]) + int(requirement["redundant_quantity"])
        sat_mass = float(requirement["sat_mass_kg"])
        sat_dims = [float(requirement["sat_length_m"]), float(requirement["sat_width_m"]), float(requirement["sat_height_m"])]
        latest_delivery = requirement["latest_delivery_date"]
        eligible: list[dict[str, Any]] = []
        excluded = {"dimensions": 0, "launch_date": 0, "capacity": 0}
        for line in lines:
            envelope = [line["envelope_length_m"], line["envelope_width_m"], line["envelope_height_m"]]
            if not _fits(sat_dims, envelope):
                excluded["dimensions"] += 1
                continue
            if line["launch_date"] > latest_delivery:
                excluded["launch_date"] += 1
                continue
            capacity_sats = math.floor(float(line["capacity_kg"]) / sat_mass + 1e-9)
            if capacity_sats < 1:
                excluded["capacity"] += 1
                continue
            eligible.append({**line, "_unit_price": _unit_price_of(line), "_capacity_sats": capacity_sats})
        by_currency: dict[str, list[dict[str, Any]]] = {}
        for line in eligible:
            by_currency.setdefault(line["price_currency"], []).append(line)
        diagnostics: dict[str, Any] = {"needed_satellites": needed, "excluded": excluded, "currencies": {}}
        candidates: list[dict[str, Any]] = []
        seen_combos: set[str] = set()
        for currency in sorted(by_currency):
            group = by_currency[currency]
            coverable = sum(line["_capacity_sats"] for line in group)
            currency_diag: dict[str, Any] = {"eligible_lines": len(group), "coverable_satellites": coverable, "needed_satellites": needed}
            if coverable < needed:
                currency_diag["status"] = "infeasible"
                currency_diag["message"] = f"{currency} 报价可用运力不足：最多承载 {coverable} 颗，需求 {needed} 颗"
                diagnostics["currencies"][currency] = currency_diag
                continue
            currency_diag["status"] = "ok"
            diagnostics["currencies"][currency] = currency_diag
            for strategy_name, key_fn in STRATEGIES:
                if len(candidates) >= MAX_CANDIDATES_PER_RUN:
                    break
                allocation = self._allocate(sorted(group, key=key_fn), needed)
                if allocation is None:
                    continue
                combo_key = ",".join(str(line["id"]) for line, _ in sorted(allocation, key=lambda item: item[0]["id"]))
                if combo_key in seen_combos:
                    continue
                seen_combos.add(combo_key)
                candidates.append(self._build_candidate(requirement, allocation, currency, strategy_name, combo_key, needed, sat_mass, quote_set_digest, run_version))
        return candidates, diagnostics

    @staticmethod
    def _allocate(ordered: list[dict[str, Any]], needed: int) -> list[tuple[dict[str, Any], int]] | None:
        remaining = needed
        allocation: list[tuple[dict[str, Any], int]] = []
        for line in ordered:
            if remaining <= 0:
                break
            take = min(remaining, line["_capacity_sats"])
            if take > 0:
                allocation.append((line, take))
                remaining -= take
        return allocation if remaining == 0 else None

    def _build_candidate(self, requirement: sqlite3.Row, allocation: list[tuple[dict[str, Any], int]], currency: str, strategy: str, combo_key: str, needed: int, sat_mass: float, quote_set_digest: str, run_version: int) -> dict[str, Any]:
        sat_mass_dec = Decimal(str(sat_mass))
        breakdown_lines: list[dict[str, Any]] = []
        total_cost = Decimal("0")
        for line, take in allocation:
            allocated_kg = Decimal(take) * sat_mass_dec
            line_cost = _quantize(allocated_kg * line["_unit_price"], MONEY_PLACES)
            total_cost += line_cost
            breakdown_lines.append({
                "quote_line_id": line["id"],
                "batch_no": line["batch_no"],
                "provider": line["provider"],
                "vehicle": line["vehicle"],
                "launch_date": line["launch_date"],
                "allocated_satellites": take,
                "allocated_kg": float(allocated_kg),
                "unit_price": float(line["_unit_price"]),
                "unit_price_basis": "per_kg" if line["price_unit"] == "per_kg" else "total_normalized",
                "price_amount": line["price_amount"],
                "price_unit": line["price_unit"],
                "capacity_kg": line["capacity_kg"],
                "currency": currency,
                "line_cost": float(line_cost),
                "quote_digest": line["content_digest"],
            })
        total_mass = Decimal(needed) * sat_mass_dec
        breakdown = {
            "currency": currency,
            "satellites_required": needed,
            "satellite_mass_kg": sat_mass,
            "lines": breakdown_lines,
            "total_mass_kg": float(total_mass),
            "total_cost": float(total_cost),
            "cost_per_kg": _money(total_cost / total_mass),
            "cost_per_satellite": _money(total_cost / Decimal(needed)),
            "formula": FORMULA_TEXT,
        }
        provenance = {
            "algorithm_version": ALGORITHM_VERSION,
            "requirement_version": requirement["version"],
            "generated_in_run_version": run_version,
            "quote_set_digest": quote_set_digest,
            "quote_lines": [
                {"quote_line_id": line["id"], "batch_no": line["batch_no"], "content_digest": line["content_digest"]}
                for line, _ in allocation
            ],
        }
        return {
            "combo_key": combo_key,
            "strategy": strategy,
            "currency": currency,
            "satellites": needed,
            "total_mass_kg": breakdown["total_mass_kg"],
            "total_cost": breakdown["total_cost"],
            "cost_per_kg": breakdown["cost_per_kg"],
            "cost_per_satellite": breakdown["cost_per_satellite"],
            "breakdown": breakdown,
            "provenance": provenance,
        }

    @staticmethod
    def _quote_batches_of(lines: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        batches: dict[int, dict[str, Any]] = {}
        for line in lines:
            batches.setdefault(line["batch_id"], {"batch_id": line["batch_id"], "batch_no": line["batch_no"]})
        return [batches[key] for key in sorted(batches)]

    # ---- 锁定与解锁 ----

    def lock_candidate(self, candidate_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcurementRepository(connection)
            candidate = self._latest_run_candidate(repository, candidate_id)
            if candidate["status"] == "locked":
                return self._candidate_payload(repository, candidate)
            locked = repository.locked_candidates(candidate["run_id"])
            if locked:
                raise ConflictError("已存在锁定的候选组合，请先解锁", context={"locked_candidate_id": locked[0]["id"]})
            updated = repository.set_candidate_lock(candidate_id, actor, reason, now)
            repository.add_event(
                entity_type="candidate", entity_id=str(candidate_id), action="lock", actor=actor,
                before={"status": candidate["status"]}, after={"status": "locked", "locked_by": actor, "lock_reason": reason}, now=now,
            )
            return self._candidate_payload(repository, updated)

    def unlock_candidate(self, candidate_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcurementRepository(connection)
            candidate = self._latest_run_candidate(repository, candidate_id)
            if candidate["status"] != "locked":
                return self._candidate_payload(repository, candidate)
            updated = repository.set_candidate_unlock(candidate_id, actor, reason, now)
            repository.add_event(
                entity_type="candidate", entity_id=str(candidate_id), action="unlock", actor=actor,
                before={"status": "locked", "locked_by": candidate["locked_by"], "lock_reason": candidate["lock_reason"]},
                after={"status": "proposed", "unlocked_by": actor, "unlock_reason": reason}, now=now,
            )
            return self._candidate_payload(repository, updated)

    @staticmethod
    def _latest_run_candidate(repository: ProcurementRepository, candidate_id: int) -> sqlite3.Row:
        candidate = repository.candidate_by_id(candidate_id)
        if candidate is None:
            raise NotFoundError("候选组合不存在")
        run = repository.run_by_id(candidate["run_id"])
        latest = repository.latest_run(run["requirement_id"])
        if latest is None or latest["id"] != run["id"]:
            raise ConflictError("只能操作最新计算版本中的候选组合", context={"latest_run_id": None if latest is None else latest["id"]})
        return candidate

    # ---- 决策报告 ----

    def export_report(self, requirement_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcurementRepository(connection)
            requirement = repository.requirement_by_id(requirement_id)
            if requirement is None:
                raise NotFoundError("载荷需求不存在")
            run = repository.latest_run(requirement_id)
            if run is None:
                raise ConflictError("尚未生成计算版本，无法导出决策报告")
            candidates = [self._candidate_payload(repository, row) for row in repository.candidates_for_run(run["id"])]
            locked = [candidate for candidate in candidates if candidate["status"] == "locked"]
            version = repository.next_report_version(requirement_id)
            content = {
                "report_version": version,
                "requirement": _row_to_dict(requirement),
                "calculation": {
                    "run_id": run["id"],
                    "run_version": run["version"],
                    "algorithm_version": run["algorithm_version"],
                    "requirement_version": run["requirement_version"],
                    "quote_set_digest": run["quote_set_digest"],
                    "quote_batches": json.loads(run["quote_batches_json"]),
                    "generated_at": run["created_at"],
                    "triggered_by": run["triggered_by"],
                    "trigger_reason": run["trigger_reason"],
                },
                "locked_candidate": locked[0] if locked else None,
                "candidates": candidates,
                "exported_by": actor,
                "exported_at": now,
            }
            content_digest = _digest(content)
            report = repository.create_report(
                requirement_id=requirement_id, version=version, run_id=run["id"],
                content=content, content_digest=content_digest, exported_by=actor, now=now,
            )
            repository.add_event(
                entity_type="report", entity_id=str(report["id"]), action="export", actor=actor,
                before=None, after={"requirement_id": requirement_id, "version": version, "run_id": run["id"], "content_digest": content_digest}, now=now,
            )
            return self._report_payload(report)

    def list_reports(self, requirement_id: int) -> list[dict[str, Any]]:
        if self.repository.requirement_by_id(requirement_id) is None:
            raise NotFoundError("载荷需求不存在")
        return [self._report_summary(row) for row in self.repository.reports_for_requirement(requirement_id)]

    def get_report(self, report_id: int) -> dict[str, Any]:
        report = self.repository.report_by_id(report_id)
        if report is None:
            raise NotFoundError("决策报告不存在")
        return self._report_payload(report)

    def list_events(self, entity_type: str | None = None, entity_id: str | None = None) -> list[dict[str, Any]]:
        return [_row_to_dict(row) for row in self.repository.events(entity_type, entity_id)]

    # ---- 输出组装 ----

    @staticmethod
    def _run_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "requirement_id": row["requirement_id"],
            "version": row["version"],
            "algorithm_version": row["algorithm_version"],
            "requirement_version": row["requirement_version"],
            "quote_set_digest": row["quote_set_digest"],
            "quote_line_count": row["quote_line_count"],
            "candidate_count": row["candidate_count"],
            "triggered_by": row["triggered_by"],
            "trigger_reason": row["trigger_reason"],
            "created_at": row["created_at"],
        }

    def _run_payload(self, repository: ProcurementRepository, run: sqlite3.Row) -> dict[str, Any]:
        payload = self._run_summary(run)
        payload["requirement_snapshot"] = json.loads(run["requirement_snapshot_json"])
        payload["quote_batches"] = json.loads(run["quote_batches_json"])
        payload["diagnostics"] = json.loads(run["diagnostics_json"])
        payload["candidates"] = [self._candidate_payload(repository, row) for row in repository.candidates_for_run(run["id"])]
        return payload

    def _candidate_payload(self, repository: ProcurementRepository, row: sqlite3.Row) -> dict[str, Any]:
        run = repository.run_by_id(row["run_id"])
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "requirement_id": row["requirement_id"],
            "combo_key": row["combo_key"],
            "strategy": row["strategy"],
            "status": row["status"],
            "carried_from_candidate_id": row["carried_from_candidate_id"],
            "currency": row["currency"],
            "satellites": row["satellites"],
            "total_mass_kg": row["total_mass_kg"],
            "total_cost": row["total_cost"],
            "cost_per_kg": row["cost_per_kg"],
            "cost_per_satellite": row["cost_per_satellite"],
            "breakdown": json.loads(row["breakdown_json"]),
            "provenance": json.loads(row["provenance_json"]),
            "locked_by": row["locked_by"],
            "locked_at": row["locked_at"],
            "lock_reason": row["lock_reason"],
            "unlocked_by": row["unlocked_by"],
            "unlocked_at": row["unlocked_at"],
            "unlock_reason": row["unlock_reason"],
            "created_at": row["created_at"],
            "calculation": {
                "run_version": run["version"],
                "algorithm_version": run["algorithm_version"],
                "requirement_version": run["requirement_version"],
                "quote_set_digest": run["quote_set_digest"],
            },
        }

    @staticmethod
    def _report_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "requirement_id": row["requirement_id"],
            "version": row["version"],
            "run_id": row["run_id"],
            "content_digest": row["content_digest"],
            "exported_by": row["exported_by"],
            "created_at": row["created_at"],
        }

    def _report_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = self._report_summary(row)
        payload["content"] = json.loads(row["content_json"])
        return payload
