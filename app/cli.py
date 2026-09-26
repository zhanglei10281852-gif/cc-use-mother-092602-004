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


def command_rideshare_demo() -> int:
    batch_no = "rideshare-demo-0001"
    comparison_no = "rideshare-demo-cmp"
    rows = [
        {
            "provider": "长光航天", "vehicle": "长征八号", "launch_site": "文昌",
            "earliest_date": "2026-10-01", "latest_date": "2026-12-20", "currency": "CNY",
            "base_price_value": 200000, "per_kg_price_value": 5000,
            "capacity_value": 500, "capacity_unit": "kg",
            "fairing_length_value": 4.5, "fairing_length_unit": "m",
            "fairing_width_value": 3.0, "fairing_width_unit": "m",
            "fairing_height_value": 3.0, "fairing_height_unit": "m",
        },
        {
            "provider": "星河动力", "vehicle": "谷神星一号", "launch_site": "酒泉",
            "earliest_date": "2026-09-01", "latest_date": "2026-11-30", "currency": "CNY",
            "base_price_value": 80000, "per_kg_price_value": 8000,
            "capacity_value": 300000, "capacity_unit": "g",
            "fairing_length_value": 400, "fairing_length_unit": "cm",
            "fairing_width_value": 200, "fairing_width_unit": "cm",
            "fairing_height_value": 200, "fairing_height_unit": "cm",
        },
    ]
    with TestClient(app) as client:
        imported = client.post("/api/rideshare/quote-batches?actor=cli-demo",
                               json={"batch_no": batch_no, "supplier": "演示承运商", "rows": rows})
        if imported.status_code not in {201}:
            print(imported.text)
            return 1
        requirement = client.put(
            "/api/rideshare/payload-requirements?actor=cli-demo",
            json={
                "payload_code": "SAT-DEMO", "name": "演示卫星",
                "unit_mass_value": 120, "unit_mass_unit": "kg",
                "length_value": 1.8, "length_unit": "m",
                "width_value": 1.5, "width_unit": "m",
                "height_value": 1.5, "height_unit": "m",
                "copies": 2, "redundancy_spares": 1,
            },
        )
        if requirement.status_code != 200:
            print(requirement.text)
            return 1
        existing = client.post(
            "/api/rideshare/comparisons?actor=cli-demo",
            json={"comparison_no": comparison_no, "title": "拼车演示", "deadline": "2026-12-31",
                  "quote_batch_nos": [batch_no], "payload_codes": ["SAT-DEMO"]},
        )
        if existing.status_code == 409:
            comparison = client.get(f"/api/rideshare/comparisons/{comparison_no}").json()
        elif existing.status_code == 201:
            comparison = existing.json()
        else:
            print(existing.text)
            return 1
        version = client.get(f"/api/rideshare/comparisons/{comparison_no}/versions/{comparison['current_version']}").json()
        best = version["summary"]["best"]
        report = client.post(f"/api/rideshare/comparisons/{comparison_no}/reports",
                             json={"actor": "cli-demo", "title": "拼车演示决策报告"})
    result = {
        "batch": imported.status_code,
        "comparison_version": version["version"],
        "candidate_count": version["summary"]["candidate_count"],
        "best_signature": best["signature"] if best else None,
        "best_total_cost": best["total_cost"]["value"] if best else None,
        "report": report.status_code,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if report.status_code == 201 and best else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("rideshare-demo", help="执行拼车发射方案比较演示")
    args = parser.parse_args()
    commands = {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "rideshare-demo": command_rideshare_demo,
    }
    return commands[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
