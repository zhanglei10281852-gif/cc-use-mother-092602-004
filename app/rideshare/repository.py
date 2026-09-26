"""拼车发射比较领域的 SQLite 读写。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class RideRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # -- 报价批次与报价行 -------------------------------------------------

    def batch_by_no(self, batch_no: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ride_quote_batches WHERE batch_no=?", (batch_no,)).fetchone()

    def batch_by_id(self, batch_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ride_quote_batches WHERE id=?", (batch_id,)).fetchone()

    def create_batch(self, *, batch_no: str, supplier: str, note: str, source_digest: str, imported_by: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO ride_quote_batches(batch_no,supplier,note,source_digest,imported_by,imported_at) VALUES(?,?,?,?,?,?)",
            (batch_no, supplier, note, source_digest, imported_by, now),
        )
        return self.batch_by_id(cursor.lastrowid)

    def finalize_batch(self, batch_id: int, *, row_count: int, accepted: int, rejected: int) -> None:
        self.connection.execute(
            "UPDATE ride_quote_batches SET row_count=?,accepted_count=?,rejected_count=? WHERE id=?",
            (row_count, accepted, rejected, batch_id),
        )

    def insert_quote_row(self, *, batch_id: int, batch_no: str, row_index: int, row: dict[str, Any], row_digest: str, now: str) -> int:
        cursor = self.connection.execute(
            """INSERT INTO ride_quote_rows(batch_id,batch_no,row_index,provider,vehicle,launch_site,earliest_date,latest_date,currency,
               base_price_value,per_kg_price_value,capacity_value,capacity_unit,
               fairing_length_value,fairing_length_unit,fairing_width_value,fairing_width_unit,fairing_height_value,fairing_height_unit,
               row_digest,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                batch_id, batch_no, row_index, row["provider"], row["vehicle"], row["launch_site"],
                row["earliest_date"], row["latest_date"], row["currency"],
                float(row["base_price_value"]), float(row["per_kg_price_value"]),
                float(row["capacity_value"]), row["capacity_unit"],
                float(row["fairing_length_value"]), row["fairing_length_unit"],
                float(row["fairing_width_value"]), row["fairing_width_unit"],
                float(row["fairing_height_value"]), row["fairing_height_unit"],
                row_digest, now,
            ),
        )
        return int(cursor.lastrowid)

    def insert_row_error(self, *, batch_id: int, batch_no: str, row_index: int, raw: dict[str, Any], errors: list[str], now: str) -> None:
        self.connection.execute(
            "INSERT INTO ride_quote_row_errors(batch_id,batch_no,row_index,raw_json,errors_json,created_at) VALUES(?,?,?,?,?,?)",
            (batch_id, batch_no, row_index, _dump(raw), _dump(errors), now),
        )

    def quote_rows(self, batch_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM ride_quote_rows WHERE batch_id=? ORDER BY row_index", (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def quote_rows_by_ids(self, row_ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = sorted(set(int(value) for value in row_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(f"SELECT * FROM ride_quote_rows WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
        return [dict(row) for row in rows]

    def quote_row_errors(self, batch_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM ride_quote_row_errors WHERE batch_id=? ORDER BY row_index", (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_batches(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM ride_quote_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -- 载荷需求 ---------------------------------------------------------

    def requirement_by_code(self, payload_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ride_payload_requirements WHERE payload_code=?", (payload_code,)).fetchone()

    def requirements_by_codes(self, codes: Iterable[str]) -> list[dict[str, Any]]:
        values = sorted(set(codes))
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        rows = self.connection.execute(f"SELECT * FROM ride_payload_requirements WHERE payload_code IN ({placeholders}) ORDER BY payload_code", values).fetchall()
        return [dict(row) for row in rows]

    def upsert_requirement(self, *, item: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        existing = self.requirement_by_code(item["payload_code"])
        if existing is None:
            self.connection.execute(
                """INSERT INTO ride_payload_requirements(payload_code,name,unit_mass_value,unit_mass_unit,length_value,length_unit,
                   width_value,width_unit,height_value,height_unit,copies,redundancy_spares,updated_by,created_at,updated_at,version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    item["payload_code"], item.get("name", ""), float(item["unit_mass_value"]), item["unit_mass_unit"],
                    float(item["length_value"]), item["length_unit"], float(item["width_value"]), item["width_unit"],
                    float(item["height_value"]), item["height_unit"], int(item["copies"]), int(item["redundancy_spares"]),
                    actor, now, now,
                ),
            )
        else:
            self.connection.execute(
                """UPDATE ride_payload_requirements SET name=?,unit_mass_value=?,unit_mass_unit=?,length_value=?,length_unit=?,
                   width_value=?,width_unit=?,height_value=?,height_unit=?,copies=?,redundancy_spares=?,updated_by=?,updated_at=?,version=version+1
                   WHERE payload_code=?""",
                (
                    item.get("name", existing["name"]), float(item["unit_mass_value"]), item["unit_mass_unit"],
                    float(item["length_value"]), item["length_unit"], float(item["width_value"]), item["width_unit"],
                    float(item["height_value"]), item["height_unit"], int(item["copies"]), int(item["redundancy_spares"]),
                    actor, now, item["payload_code"],
                ),
            )
        return dict(self.requirement_by_code(item["payload_code"]))

    def list_requirements(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM ride_payload_requirements ORDER BY payload_code").fetchall()
        return [dict(row) for row in rows]

    # -- 比较与版本 --------------------------------------------------------

    def comparison_by_no(self, comparison_no: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ride_comparisons WHERE comparison_no=?", (comparison_no,)).fetchone()

    def comparison_by_id(self, comparison_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ride_comparisons WHERE id=?", (comparison_id,)).fetchone()

    def create_comparison(self, *, comparison_no: str, title: str, deadline: str, quote_batch_ids: list[int], actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO ride_comparisons(comparison_no,title,deadline,quote_batch_ids_json,status,current_version,created_by,created_at,updated_at) VALUES(?,?,?,?,'draft',0,?,?,?)",
            (comparison_no, title, deadline, _dump(sorted(set(int(v) for v in quote_batch_ids))), actor, now, now),
        )
        return dict(self.comparison_by_id(cursor.lastrowid))

    def touch_comparison(self, comparison_id: int, *, current_version: int, status: str, now: str) -> None:
        self.connection.execute(
            "UPDATE ride_comparisons SET current_version=?,status=?,updated_at=? WHERE id=?",
            (current_version, status, now, comparison_id),
        )

    def list_comparisons(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM ride_comparisons ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def insert_version(self, *, comparison_id: int, version: int, trigger: str, engine_version: str, input_digest: str,
                       input_snapshot: dict[str, Any], fixed_slots: dict[str, Any], candidates: list[dict[str, Any]],
                       summary: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO ride_comparison_versions(comparison_id,version,trigger,engine_version,input_digest,input_snapshot_json,
               fixed_slots_json,candidates_json,summary_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (comparison_id, version, trigger, engine_version, input_digest, _dump(input_snapshot), _dump(fixed_slots),
             _dump(candidates), _dump(summary), actor, now),
        )
        return dict(self.connection.execute("SELECT * FROM ride_comparison_versions WHERE id=?", (cursor.lastrowid,)).fetchone())

    def version_row(self, comparison_id: int, version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ride_comparison_versions WHERE comparison_id=? AND version=?", (comparison_id, version)
        ).fetchone()

    def list_versions(self, comparison_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id,comparison_id,version,trigger,engine_version,input_digest,created_by,created_at FROM ride_comparison_versions WHERE comparison_id=? ORDER BY version",
            (comparison_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- 人工锁定 ----------------------------------------------------------

    def active_locks(self, comparison_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM ride_locks WHERE comparison_id=? AND active=1 ORDER BY quote_row_id", (comparison_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def lock_row(self, comparison_id: int, quote_row_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ride_locks WHERE comparison_id=? AND quote_row_id=?", (comparison_id, quote_row_id)
        ).fetchone()

    def upsert_lock(self, *, comparison_id: int, quote_row_id: int, units: list[str], actor: str, reason: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            """INSERT INTO ride_locks(comparison_id,quote_row_id,units_json,actor,reason,active,created_at,revoked_at)
               VALUES(?,?,?,?,?,1,?,NULL)
               ON CONFLICT(comparison_id,quote_row_id) DO UPDATE SET units_json=excluded.units_json,actor=excluded.actor,
               reason=excluded.reason,active=1,created_at=excluded.created_at,revoked_at=NULL""",
            (comparison_id, quote_row_id, _dump(sorted(units)), actor, reason, now),
        )
        return dict(self.lock_row(comparison_id, quote_row_id))

    def revoke_lock(self, *, comparison_id: int, quote_row_id: int, now: str) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            "UPDATE ride_locks SET active=0,revoked_at=? WHERE comparison_id=? AND quote_row_id=? AND active=1",
            (now, comparison_id, quote_row_id),
        )
        if cursor.rowcount != 1:
            return None
        return dict(self.lock_row(comparison_id, quote_row_id))

    # -- 决策报告 ----------------------------------------------------------

    def insert_report(self, *, comparison_id: int, report_version: int, comparison_version: int, engine_version: str,
                      title: str, content: dict[str, Any], content_digest: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO ride_reports(comparison_id,report_version,comparison_version,engine_version,title,content_json,content_digest,exported_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (comparison_id, report_version, comparison_version, engine_version, title, _dump(content), content_digest, actor, now),
        )
        return dict(self.connection.execute("SELECT * FROM ride_reports WHERE id=?", (cursor.lastrowid,)).fetchone())

    def report_by_version(self, comparison_id: int, report_version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ride_reports WHERE comparison_id=? AND report_version=?", (comparison_id, report_version)
        ).fetchone()

    def report_by_comparison_version(self, comparison_id: int, comparison_version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ride_reports WHERE comparison_id=? AND comparison_version=? ORDER BY report_version LIMIT 1",
            (comparison_id, comparison_version),
        ).fetchone()

    def list_reports(self, comparison_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id,comparison_id,report_version,comparison_version,engine_version,title,content_digest,exported_by,created_at FROM ride_reports WHERE comparison_id=? ORDER BY report_version",
            (comparison_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def next_report_version(self, comparison_id: int) -> int:
        value = self.connection.execute(
            "SELECT COALESCE(MAX(report_version),0)+1 FROM ride_reports WHERE comparison_id=?", (comparison_id,)
        ).fetchone()[0]
        return int(value)

    # -- 决策审计 ----------------------------------------------------------

    def add_audit(self, *, comparison_id: int | None, action: str, actor: str, reason: str, target_type: str,
                  target_key: str, before: Any, after: Any, now: str) -> None:
        self.connection.execute(
            "INSERT INTO ride_decision_audit(comparison_id,action,actor,reason,target_type,target_key,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (comparison_id, action, actor, reason, target_type, target_key, _dump(before), _dump(after), now),
        )

    def list_audit(self, comparison_id: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM ride_decision_audit WHERE comparison_id=? ORDER BY id DESC LIMIT ?", (comparison_id, limit)
        ).fetchall()
        return [dict(row) for row in rows]
