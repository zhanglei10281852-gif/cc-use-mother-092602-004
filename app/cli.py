from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_procurement_demo() -> int:
    rows = [
        {"provider": "StarRide", "vehicle": "Falcon-9R", "launch_date": "2026-12-01", "price_amount": 3200, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 4000, "envelope_length_m": 3.0, "envelope_width_m": 2.0, "envelope_height_m": 2.0},
        {"provider": "OrbitVan", "vehicle": "Vega-X", "launch_date": "2027-01-15", "price_amount": 2800, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 1500, "envelope_length_m": 2.5, "envelope_width_m": 2.0, "envelope_height_m": 1.8},
        {"provider": "LongMarchShare", "vehicle": "LM-6A", "launch_date": "2026-11-01", "price_amount": 2000000, "price_currency": "CNY", "price_unit": "total", "capacity_kg": 1000, "envelope_length_m": 2.2, "envelope_width_m": 2.2, "envelope_height_m": 2.2},
    ]
    with TestClient(app) as client:
        batch = client.post("/api/procurement/quote-batches", json={"batch_no": "Q2026-DEMO", "source": "cli-demo", "imported_by": "cli-user", "rows": rows})
        if batch.status_code not in {200, 201}:
            print(batch.text)
            return 1
        requirement = client.post(
            "/api/procurement/requirements",
            json={"code": "req-demo", "name": "演示载荷需求", "sat_length_m": 1.5, "sat_width_m": 1.0, "sat_height_m": 0.8, "sat_mass_kg": 145, "quantity": 4, "redundant_quantity": 1, "latest_delivery_date": "2027-03-01", "created_by": "cli-user"},
        )
        if requirement.status_code not in {201, 409}:
            print(requirement.text)
            return 1
        requirement_id = requirement.json().get("id") or client.get("/api/procurement/requirements").json()["items"][0]["id"]
        run = client.post(f"/api/procurement/requirements/{requirement_id}/calculations", json={"actor": "cli-user", "reason": "演示测算"})
        if run.status_code != 201:
            print(run.text)
            return 1
        candidates = run.json()["candidates"]
        locked = None
        if candidates:
            target = min((c for c in candidates if c["status"] == "proposed"), key=lambda c: c["total_cost"], default=None)
            if target is not None:
                locked = client.post(f"/api/procurement/candidates/{target['id']}/lock", json={"actor": "cli-user", "reason": "演示锁定"}).json()
        report = client.post(f"/api/procurement/requirements/{requirement_id}/reports", json={"actor": "cli-user"})
    result = {
        "batch": batch.status_code,
        "run_version": run.json()["version"],
        "candidates": len(candidates),
        "locked_candidate": None if locked is None else locked["id"],
        "report_version": report.json().get("version"),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if report.status_code == 201 else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("procurement-demo", help="执行拼车发射采购比较演示")
    args = parser.parse_args()
    return {"init-db": command_init, "check-db": command_check, "smoke": command_smoke, "compute-demo": command_compute_demo, "procurement-demo": command_procurement_demo}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
