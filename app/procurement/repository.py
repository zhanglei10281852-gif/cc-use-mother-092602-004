from __future__ import annotations

import json
import sqlite3
from typing import Any


class ProcurementRepository:
    """封装拼车发射采购比较领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 报价批次与报价行 ----

    def batch_by_no(self, batch_no: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM proc_quote_batches WHERE batch_no=?", (batch_no,)).fetchone()

    def batch_by_id(self, batch_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM proc_quote_batches WHERE id=?", (batch_id,)).fetchone()

    def list_batches(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM proc_quote_batches ORDER BY id").fetchall()

    def create_batch(self, *, batch_no: str, source: str, note: str, content_digest: str, line_count: int, imported_by: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO proc_quote_batches(batch_no,source,note,content_digest,line_count,imported_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (batch_no, source, note, content_digest, line_count, imported_by, now),
        )
        return self.batch_by_id(cursor.lastrowid)

    def create_line(self, *, batch_id: int, line_no: int, row: dict[str, Any], content_digest: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO proc_quote_lines(batch_id,line_no,provider,vehicle,launch_date,price_amount,price_currency,price_unit,capacity_kg,envelope_length_m,envelope_width_m,envelope_height_m,content_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_id, line_no, row["provider"], row["vehicle"], row["launch_date"],
                row["price_amount"], row["price_currency"], row["price_unit"], row["capacity_kg"],
                row["envelope_length_m"], row["envelope_width_m"], row["envelope_height_m"],
                content_digest, now,
            ),
        )

    def lines_for_batch(self, batch_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM proc_quote_lines WHERE batch_id=? ORDER BY line_no", (batch_id,)).fetchall()

    def current_lines(self) -> list[sqlite3.Row]:
        """当前报价簿：同一供应商、型号、发射日期的报价以最新批次为准，历史行保留用于追溯。"""
        return self.connection.execute(
            """
            SELECT q.*, b.batch_no FROM proc_quote_lines q
            JOIN proc_quote_batches b ON b.id = q.batch_id
            JOIN (
                SELECT provider, vehicle, launch_date, MAX(batch_id) AS max_batch
                FROM proc_quote_lines
                GROUP BY provider, vehicle, launch_date
            ) latest ON latest.provider=q.provider AND latest.vehicle=q.vehicle
                AND latest.launch_date=q.launch_date AND latest.max_batch=q.batch_id
            ORDER BY q.id
            """
        ).fetchall()

    # ---- 载荷需求 ----

    def requirement_by_id(self, requirement_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM proc_requirements WHERE id=?", (requirement_id,)).fetchone()

    def requirement_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM proc_requirements WHERE code=?", (code,)).fetchone()

    def list_requirements(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM proc_requirements ORDER BY id").fetchall()

    def create_requirement(self, *, payload: dict[str, Any], now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO proc_requirements(code,name,sat_length_m,sat_width_m,sat_height_m,sat_mass_kg,quantity,redundant_quantity,latest_delivery_date,version,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (
                payload["code"], payload["name"], payload["sat_length_m"], payload["sat_width_m"], payload["sat_height_m"],
                payload["sat_mass_kg"], payload["quantity"], payload["redundant_quantity"], payload["latest_delivery_date"],
                payload["created_by"], now, now,
            ),
        )
        return self.requirement_by_id(cursor.lastrowid)

    def update_requirement(self, requirement_id: int, changes: dict[str, Any], now: str) -> sqlite3.Row:
        assignments = ", ".join(f"{key}=?" for key in changes)
        self.connection.execute(
            f"UPDATE proc_requirements SET {assignments}, version=version+1, updated_at=? WHERE id=?",
            (*changes.values(), now, requirement_id),
        )
        return self.requirement_by_id(requirement_id)

    # ---- 计算运行与候选组合 ----

    def latest_run(self, requirement_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM proc_calc_runs WHERE requirement_id=? ORDER BY version DESC LIMIT 1", (requirement_id,)
        ).fetchone()

    def run_by_id(self, run_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM proc_calc_runs WHERE id=?", (run_id,)).fetchone()

    def runs_for_requirement(self, requirement_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM proc_calc_runs WHERE requirement_id=? ORDER BY version", (requirement_id,)).fetchall()

    def create_run(self, *, requirement_id: int, version: int, algorithm_version: str, requirement_version: int, requirement_snapshot: dict[str, Any], quote_set_digest: str, quote_line_count: int, quote_batches: list[dict[str, Any]], candidate_count: int, diagnostics: dict[str, Any], triggered_by: str, trigger_reason: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO proc_calc_runs(requirement_id,version,algorithm_version,requirement_version,requirement_snapshot_json,quote_set_digest,quote_line_count,quote_batches_json,candidate_count,diagnostics_json,triggered_by,trigger_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                requirement_id, version, algorithm_version, requirement_version,
                json.dumps(requirement_snapshot, ensure_ascii=False, sort_keys=True),
                quote_set_digest, quote_line_count,
                json.dumps(quote_batches, ensure_ascii=False, sort_keys=True),
                candidate_count, json.dumps(diagnostics, ensure_ascii=False, sort_keys=True),
                triggered_by, trigger_reason, now,
            ),
        )
        return self.run_by_id(cursor.lastrowid)

    def mark_unlocked_stale(self, run_id: int) -> None:
        self.connection.execute("UPDATE proc_candidates SET status='stale' WHERE run_id=? AND status='proposed'", (run_id,))

    def create_candidate(self, *, run_id: int, requirement_id: int, candidate: dict[str, Any], status: str, carried_from: int | None, lock: dict[str, Any] | None, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO proc_candidates(run_id,requirement_id,combo_key,strategy,status,carried_from_candidate_id,currency,satellites,total_mass_kg,total_cost,cost_per_kg,cost_per_satellite,breakdown_json,provenance_json,locked_by,locked_at,lock_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, requirement_id, candidate["combo_key"], candidate["strategy"], status, carried_from,
                candidate["currency"], candidate["satellites"], candidate["total_mass_kg"], candidate["total_cost"],
                candidate["cost_per_kg"], candidate["cost_per_satellite"],
                json.dumps(candidate["breakdown"], ensure_ascii=False, sort_keys=True),
                json.dumps(candidate["provenance"], ensure_ascii=False, sort_keys=True),
                (lock or {}).get("locked_by"), (lock or {}).get("locked_at"), (lock or {}).get("lock_reason"), now,
            ),
        )
        return self.candidate_by_id(cursor.lastrowid)

    def candidate_by_id(self, candidate_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM proc_candidates WHERE id=?", (candidate_id,)).fetchone()

    def candidates_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM proc_candidates WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    def locked_candidates(self, run_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM proc_candidates WHERE run_id=? AND status='locked' ORDER BY id", (run_id,)).fetchall()

    def set_candidate_lock(self, candidate_id: int, actor: str, reason: str, now: str) -> sqlite3.Row:
        self.connection.execute(
            "UPDATE proc_candidates SET status='locked', locked_by=?, locked_at=?, lock_reason=? WHERE id=?",
            (actor, now, reason, candidate_id),
        )
        return self.candidate_by_id(candidate_id)

    def set_candidate_unlock(self, candidate_id: int, actor: str, reason: str, now: str) -> sqlite3.Row:
        self.connection.execute(
            "UPDATE proc_candidates SET status='proposed', unlocked_by=?, unlocked_at=?, unlock_reason=? WHERE id=?",
            (actor, now, reason, candidate_id),
        )
        return self.candidate_by_id(candidate_id)

    # ---- 决策报告 ----

    def next_report_version(self, requirement_id: int) -> int:
        row = self.connection.execute("SELECT MAX(version) FROM proc_reports WHERE requirement_id=?", (requirement_id,)).fetchone()
        return int(row[0] or 0) + 1

    def create_report(self, *, requirement_id: int, version: int, run_id: int, content: dict[str, Any], content_digest: str, exported_by: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO proc_reports(requirement_id,version,run_id,content_json,content_digest,exported_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (requirement_id, version, run_id, json.dumps(content, ensure_ascii=False, sort_keys=True), content_digest, exported_by, now),
        )
        return self.report_by_id(cursor.lastrowid)

    def report_by_id(self, report_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM proc_reports WHERE id=?", (report_id,)).fetchone()

    def reports_for_requirement(self, requirement_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM proc_reports WHERE requirement_id=? ORDER BY version", (requirement_id,)).fetchall()

    # ---- 审计事件 ----

    def add_event(self, *, entity_type: str, entity_id: str, action: str, actor: str, before: dict[str, Any] | None, after: dict[str, Any] | None, now: str) -> None:
        self.connection.execute(
            "INSERT INTO proc_events(entity_type,entity_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, action, actor,
                json.dumps(before or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(after or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

    def events(self, entity_type: str | None = None, entity_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM proc_events"
        conditions: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            conditions.append("entity_type=?")
            params.append(entity_type)
        if entity_id is not None:
            conditions.append("entity_id=?")
            params.append(entity_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        return self.connection.execute(query + " ORDER BY id", params).fetchall()
