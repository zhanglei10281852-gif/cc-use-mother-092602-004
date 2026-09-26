from __future__ import annotations


def quote_rows():
    return [
        {"provider": "StarRide", "vehicle": "Falcon-9R", "launch_date": "2026-12-01", "price_amount": 3200, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 4000, "envelope_length_m": 3.0, "envelope_width_m": 2.0, "envelope_height_m": 2.0},
        {"provider": "OrbitVan", "vehicle": "Vega-X", "launch_date": "2027-01-15", "price_amount": 2800, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 1500, "envelope_length_m": 2.5, "envelope_width_m": 2.0, "envelope_height_m": 1.8},
        {"provider": "LongMarchShare", "vehicle": "LM-6A", "launch_date": "2026-11-01", "price_amount": 2000000, "price_currency": "CNY", "price_unit": "total", "capacity_kg": 1000, "envelope_length_m": 2.2, "envelope_width_m": 2.2, "envelope_height_m": 2.2},
        {"provider": "LateCo", "vehicle": "Slow-1", "launch_date": "2027-06-01", "price_amount": 1000, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 5000, "envelope_length_m": 4.0, "envelope_width_m": 4.0, "envelope_height_m": 4.0},
        {"provider": "TinyCo", "vehicle": "Pico", "launch_date": "2026-12-01", "price_amount": 900, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 50, "envelope_length_m": 4.0, "envelope_width_m": 4.0, "envelope_height_m": 4.0},
        {"provider": "SlimCo", "vehicle": "Slim", "launch_date": "2026-12-01", "price_amount": 900, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 5000, "envelope_length_m": 1.0, "envelope_width_m": 1.0, "envelope_height_m": 1.0},
    ]


def import_batch(client, batch_no="Q2026-09-A", rows=None):
    return client.post(
        "/api/procurement/quote-batches",
        json={"batch_no": batch_no, "source": "procurement-portal", "imported_by": "buyer-1", "rows": rows if rows is not None else quote_rows()},
    )


def requirement_payload(**overrides):
    payload = {
        "code": "req-leo-5u",
        "name": "LEO 星座首批五星",
        "sat_length_m": 1.5,
        "sat_width_m": 1.0,
        "sat_height_m": 0.8,
        "sat_mass_kg": 145,
        "quantity": 4,
        "redundant_quantity": 1,
        "latest_delivery_date": "2027-03-01",
        "created_by": "buyer-1",
    }
    payload.update(overrides)
    return payload


def setup_calculated(client):
    """导入批次 A、创建需求并生成第一个计算版本，返回 (requirement_id, run)。"""
    assert import_batch(client).status_code == 201
    created = client.post("/api/procurement/requirements", json=requirement_payload())
    assert created.status_code == 201, created.text
    requirement_id = created.json()["id"]
    run = client.post(f"/api/procurement/requirements/{requirement_id}/calculations", json={"actor": "analyst-1", "reason": "首次测算"})
    assert run.status_code == 201, run.text
    return requirement_id, run.json()


def find_candidate(run, currency, strategy, status=None):
    for candidate in run["candidates"]:
        if candidate["currency"] == currency and candidate["strategy"] == strategy:
            if status is None or candidate["status"] == status:
                return candidate
    raise AssertionError(f"未找到 {currency}/{strategy}/{status} 候选组合")


def test_import_batch_and_idempotent_replay(client):
    first = import_batch(client)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["replayed"] is False
    assert body["line_count"] == 6
    assert len(body["lines"]) == 6
    assert body["content_digest"]

    replay = import_batch(client)
    assert replay.status_code == 200
    replayed = replay.json()
    assert replayed["replayed"] is True
    assert replayed["id"] == body["id"]
    assert [line["id"] for line in replayed["lines"]] == [line["id"] for line in body["lines"]]

    batches = client.get("/api/procurement/quote-batches").json()["items"]
    assert len(batches) == 1


def test_import_same_batch_no_with_different_content_conflicts(client):
    assert import_batch(client).status_code == 201
    changed = quote_rows()
    changed[0]["price_amount"] = 3100
    conflict = import_batch(client, rows=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"
    batches = client.get("/api/procurement/quote-batches").json()["items"]
    assert len(batches) == 1
    detail = client.get("/api/procurement/quote-batches/Q2026-09-A").json()
    assert detail["lines"][0]["price_amount"] == 3200


def test_import_row_errors_are_returned_per_row_and_nothing_is_imported(client):
    rows = [
        {"provider": "NoUnit", "vehicle": "V1", "launch_date": "2026-12-01", "price_amount": 100, "price_currency": "USD", "capacity_kg": 100, "envelope_length_m": 2, "envelope_width_m": 2, "envelope_height_m": 2},
        {"provider": "Negative", "vehicle": "V2", "launch_date": "2026-12-01", "price_amount": -5, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 100, "envelope_length_m": 2, "envelope_width_m": 2, "envelope_height_m": 2},
        {"provider": "ZeroCap", "vehicle": "V3", "launch_date": "2026-12-01", "price_amount": 100, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 0, "envelope_length_m": 2, "envelope_width_m": 2, "envelope_height_m": 2},
        {"provider": "BadDate", "vehicle": "V4", "launch_date": "not-a-date", "price_amount": 100, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 100, "envelope_length_m": 2, "envelope_width_m": 2, "envelope_height_m": 2},
        {"provider": "Dup", "vehicle": "V5", "launch_date": "2026-12-01", "price_amount": 100, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 100, "envelope_length_m": 2, "envelope_width_m": 2, "envelope_height_m": 2},
        {"provider": "Dup", "vehicle": "V5", "launch_date": "2026-12-01", "price_amount": 200, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 100, "envelope_length_m": 2, "envelope_width_m": 2, "envelope_height_m": 2},
    ]
    response = import_batch(client, batch_no="Q2026-09-BAD", rows=rows)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    errors = error["context"]["errors"]
    by_row = {}
    for item in errors:
        by_row.setdefault(item["row"], []).append(item["code"])
    assert "missing_unit" in by_row[0]
    assert "out_of_range" in by_row[1]
    assert "out_of_range" in by_row[2]
    assert "invalid_value" in by_row[3]
    assert "duplicate_in_batch" in by_row[5]
    assert client.get("/api/procurement/quote-batches").json()["items"] == []


def test_calculate_generates_candidates_with_cost_breakdown(client):
    _, run = setup_calculated(client)
    assert run["version"] == 1
    assert run["algorithm_version"] == "combo-v1"
    assert run["candidate_count"] == 3
    assert run["diagnostics"]["excluded"] == {"dimensions": 1, "launch_date": 1, "capacity": 1}

    cheapest = find_candidate(run, "USD", "lowest_unit_price")
    assert cheapest["satellites"] == 5
    assert cheapest["total_mass_kg"] == 725
    assert cheapest["total_cost"] == 2030000.0
    assert cheapest["cost_per_kg"] == 2800.0
    assert cheapest["cost_per_satellite"] == 406000.0
    line = cheapest["breakdown"]["lines"][0]
    assert line["provider"] == "OrbitVan"
    assert line["allocated_satellites"] == 5
    assert line["allocated_kg"] == 725
    assert line["unit_price"] == 2800.0
    assert line["line_cost"] == 2030000.0

    consolidated = find_candidate(run, "USD", "fewest_launches")
    assert consolidated["breakdown"]["lines"][0]["provider"] == "StarRide"
    assert consolidated["total_cost"] == 2320000.0

    cny = find_candidate(run, "CNY", "lowest_unit_price")
    cny_line = cny["breakdown"]["lines"][0]
    assert cny_line["unit_price_basis"] == "total_normalized"
    assert cny_line["unit_price"] == 2000.0
    assert cny["total_cost"] == 1450000.0
    assert cny["currency"] == "CNY"


def test_candidate_detail_traces_every_number(client):
    _, run = setup_calculated(client)
    candidate = find_candidate(run, "USD", "lowest_unit_price")
    detail = client.get(f"/api/procurement/candidates/{candidate['id']}").json()
    assert detail["calculation"]["run_version"] == 1
    assert detail["calculation"]["algorithm_version"] == "combo-v1"
    assert detail["calculation"]["quote_set_digest"] == run["quote_set_digest"]
    assert detail["provenance"]["algorithm_version"] == "combo-v1"
    assert detail["provenance"]["requirement_version"] == 1
    line = detail["breakdown"]["lines"][0]
    assert line["batch_no"] == "Q2026-09-A"
    assert line["quote_line_id"] in [int(part) for part in detail["combo_key"].split(",")]
    assert line["quote_digest"]
    provenance_ids = {item["quote_line_id"] for item in detail["provenance"]["quote_lines"]}
    assert provenance_ids == {line["quote_line_id"]}


def test_lock_recalculate_and_quote_update_does_not_rewrite_history(client):
    requirement_id, run1 = setup_calculated(client)
    locked = find_candidate(run1, "USD", "lowest_unit_price")
    lock = client.post(f"/api/procurement/candidates/{locked['id']}/lock", json={"actor": "manager-1", "reason": "价格与档期综合最优"})
    assert lock.status_code == 200, lock.text
    assert lock.json()["status"] == "locked"

    updated = quote_rows()
    updated[1]["price_amount"] = 2600
    updated.append({"provider": "NewCo", "vehicle": "Neo-1", "launch_date": "2026-12-10", "price_amount": 2500, "price_currency": "USD", "price_unit": "per_kg", "capacity_kg": 800, "envelope_length_m": 2.5, "envelope_width_m": 2.0, "envelope_height_m": 2.0})
    assert import_batch(client, batch_no="Q2026-09-B", rows=updated).status_code == 201

    current = {row["provider"]: row for row in client.get("/api/procurement/quotes/current").json()["items"]}
    assert current["OrbitVan"]["price_amount"] == 2600

    recalc = client.post(f"/api/procurement/requirements/{requirement_id}/calculations", json={"actor": "analyst-1", "reason": "新批次报价到货重算"})
    assert recalc.status_code == 201, recalc.text
    run2 = recalc.json()
    assert run2["version"] == 2

    carried = [c for c in run2["candidates"] if c["status"] == "locked"]
    assert len(carried) == 1
    assert carried[0]["carried_from_candidate_id"] == locked["id"]
    assert carried[0]["total_cost"] == 2030000.0
    assert carried[0]["breakdown"]["lines"][0]["unit_price"] == 2800.0

    fresh_new = find_candidate(run2, "USD", "lowest_unit_price", status="proposed")
    assert fresh_new["breakdown"]["lines"][0]["provider"] == "NewCo"
    assert fresh_new["total_cost"] == 1812500.0

    # 历史计算版本与已锁定组合的论证不被报价更新改写
    run1_after = client.get(f"/api/procurement/calculations/{run1['id']}").json()
    original = find_candidate(run1_after, "USD", "lowest_unit_price")
    assert original["total_cost"] == 2030000.0
    assert original["breakdown"]["lines"][0]["unit_price"] == 2800.0
    stale = [c for c in run1_after["candidates"] if c["status"] == "stale"]
    assert stale


def test_lock_conflict_and_unlock_flow(client):
    _, run = setup_calculated(client)
    first = find_candidate(run, "USD", "lowest_unit_price")
    second = find_candidate(run, "USD", "fewest_launches")
    assert client.post(f"/api/procurement/candidates/{first['id']}/lock", json={"actor": "m1", "reason": "首选"}).status_code == 200
    conflict = client.post(f"/api/procurement/candidates/{second['id']}/lock", json={"actor": "m1", "reason": "换一个"})
    assert conflict.status_code == 409
    unlock = client.post(f"/api/procurement/candidates/{first['id']}/unlock", json={"actor": "m1", "reason": "重新评估"})
    assert unlock.status_code == 200
    assert unlock.json()["status"] == "proposed"
    assert unlock.json()["unlocked_by"] == "m1"
    assert client.post(f"/api/procurement/candidates/{second['id']}/lock", json={"actor": "m1", "reason": "改选整合方案"}).status_code == 200


def test_report_export_is_versioned_and_immutable(client):
    requirement_id, run = setup_calculated(client)
    locked = find_candidate(run, "USD", "lowest_unit_price")
    client.post(f"/api/procurement/candidates/{locked['id']}/lock", json={"actor": "m1", "reason": "定稿"})

    first = client.post(f"/api/procurement/requirements/{requirement_id}/reports", json={"actor": "m1"})
    assert first.status_code == 201, first.text
    report1 = first.json()
    assert report1["version"] == 1
    assert report1["content"]["calculation"]["run_version"] == 1
    assert report1["content"]["locked_candidate"]["total_cost"] == 2030000.0
    assert report1["content"]["calculation"]["quote_batches"] == [{"batch_id": 1, "batch_no": "Q2026-09-A"}]

    client.post(f"/api/procurement/requirements/{requirement_id}/calculations", json={"actor": "analyst-1"})
    second = client.post(f"/api/procurement/requirements/{requirement_id}/reports", json={"actor": "m1"})
    assert second.json()["version"] == 2
    assert second.json()["content"]["calculation"]["run_version"] == 2

    fetched = client.get(f"/api/procurement/reports/{report1['id']}").json()
    assert fetched["content"] == report1["content"]
    assert fetched["content_digest"] == report1["content_digest"]
    assert fetched["content"]["calculation"]["run_version"] == 1

    reports = client.get(f"/api/procurement/requirements/{requirement_id}/reports").json()["items"]
    assert [item["version"] for item in reports] == [1, 2]


def test_requirement_update_versions_and_keeps_old_snapshots(client):
    requirement_id, run1 = setup_calculated(client)
    update = client.put(
        f"/api/procurement/requirements/{requirement_id}",
        json={"quantity": 5, "actor": "buyer-1", "reason": "星座计划增加一颗正选星"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["version"] == 2
    assert update.json()["quantity"] == 5

    run2 = client.post(f"/api/procurement/requirements/{requirement_id}/calculations", json={"actor": "analyst-1"}).json()
    assert run2["requirement_version"] == 2
    cheapest = find_candidate(run2, "USD", "lowest_unit_price")
    assert cheapest["satellites"] == 6
    assert cheapest["total_cost"] == 6 * 145 * 2800

    old = client.get(f"/api/procurement/calculations/{run1['id']}").json()
    assert old["requirement_snapshot"]["quantity"] == 4
    assert old["requirement_version"] == 1


def test_infeasible_requirement_records_diagnostics(client):
    assert import_batch(client).status_code == 201
    created = client.post("/api/procurement/requirements", json=requirement_payload(code="req-heavy", quantity=200, redundant_quantity=0))
    requirement_id = created.json()["id"]
    run = client.post(f"/api/procurement/requirements/{requirement_id}/calculations", json={"actor": "analyst-1"}).json()
    assert run["candidate_count"] == 0
    assert run["diagnostics"]["currencies"]["USD"]["status"] == "infeasible"
    assert run["diagnostics"]["currencies"]["CNY"]["status"] == "infeasible"
    report = client.post(f"/api/procurement/requirements/{requirement_id}/reports", json={"actor": "m1"})
    assert report.status_code == 201
    assert report.json()["content"]["locked_candidate"] is None


def test_events_record_full_audit_trail(client):
    requirement_id, run = setup_calculated(client)
    locked = find_candidate(run, "USD", "lowest_unit_price")
    client.post(f"/api/procurement/candidates/{locked['id']}/lock", json={"actor": "m1", "reason": "定稿"})
    client.post(f"/api/procurement/requirements/{requirement_id}/reports", json={"actor": "m1"})
    events = client.get("/api/procurement/events").json()["items"]
    actions = [(event["entity_type"], event["action"]) for event in events]
    assert ("quote_batch", "import") in actions
    assert ("requirement", "create") in actions
    assert ("calculation", "run") in actions
    assert ("candidate", "lock") in actions
    assert ("report", "export") in actions
