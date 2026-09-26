"""拼车发射比较领域的 SQLite 表结构。"""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS ride_quote_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no TEXT NOT NULL UNIQUE,
    supplier TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    source_digest TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    imported_by TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ride_quote_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES ride_quote_batches(id) ON DELETE RESTRICT,
    batch_no TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    provider TEXT NOT NULL,
    vehicle TEXT NOT NULL,
    launch_site TEXT NOT NULL,
    earliest_date TEXT NOT NULL,
    latest_date TEXT NOT NULL,
    currency TEXT NOT NULL,
    base_price_value REAL NOT NULL,
    per_kg_price_value REAL NOT NULL,
    capacity_value REAL NOT NULL,
    capacity_unit TEXT NOT NULL,
    fairing_length_value REAL NOT NULL,
    fairing_length_unit TEXT NOT NULL,
    fairing_width_value REAL NOT NULL,
    fairing_width_unit TEXT NOT NULL,
    fairing_height_value REAL NOT NULL,
    fairing_height_unit TEXT NOT NULL,
    row_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, row_index)
);
CREATE INDEX IF NOT EXISTS idx_ride_quote_rows_batch ON ride_quote_rows(batch_id, id);
CREATE TABLE IF NOT EXISTS ride_quote_row_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES ride_quote_batches(id) ON DELETE RESTRICT,
    batch_no TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    errors_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ride_quote_errors_batch ON ride_quote_row_errors(batch_id, row_index);
CREATE TABLE IF NOT EXISTS ride_payload_requirements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    unit_mass_value REAL NOT NULL,
    unit_mass_unit TEXT NOT NULL,
    length_value REAL NOT NULL,
    length_unit TEXT NOT NULL,
    width_value REAL NOT NULL,
    width_unit TEXT NOT NULL,
    height_value REAL NOT NULL,
    height_unit TEXT NOT NULL,
    copies INTEGER NOT NULL,
    redundancy_spares INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ride_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_no TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    deadline TEXT NOT NULL DEFAULT '',
    quote_batch_ids_json TEXT NOT NULL DEFAULT '[]',
    payload_codes_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','locked')),
    current_version INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ride_comparison_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id INTEGER NOT NULL REFERENCES ride_comparisons(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    trigger TEXT NOT NULL CHECK(trigger IN ('generate','lock','unlock','recompute')),
    engine_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL,
    fixed_slots_json TEXT NOT NULL DEFAULT '{}',
    candidates_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(comparison_id, version)
);
CREATE INDEX IF NOT EXISTS idx_ride_versions_comparison ON ride_comparison_versions(comparison_id, version);
CREATE TABLE IF NOT EXISTS ride_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id INTEGER NOT NULL REFERENCES ride_comparisons(id) ON DELETE RESTRICT,
    quote_row_id INTEGER NOT NULL REFERENCES ride_quote_rows(id) ON DELETE RESTRICT,
    units_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(comparison_id, quote_row_id)
);
CREATE INDEX IF NOT EXISTS idx_ride_locks_comparison ON ride_locks(comparison_id, active);
CREATE TABLE IF NOT EXISTS ride_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id INTEGER NOT NULL REFERENCES ride_comparisons(id) ON DELETE RESTRICT,
    report_version INTEGER NOT NULL,
    comparison_version INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    exported_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(comparison_id, report_version)
);
CREATE INDEX IF NOT EXISTS idx_ride_reports_comparison ON ride_reports(comparison_id, report_version);
CREATE TABLE IF NOT EXISTS ride_decision_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    target_key TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ride_audit_comparison ON ride_decision_audit(comparison_id, id);
CREATE INDEX IF NOT EXISTS idx_ride_audit_created ON ride_decision_audit(created_at);
"""


def ensure_schema() -> None:
    from app.database import get_connection

    get_connection().executescript(SCHEMA)
