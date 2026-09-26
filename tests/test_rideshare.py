"""拼车发射方案比较器的端到端测试。

覆盖：报价批次幂等导入、逐行错误、载荷需求校验、候选组合与成本分解、
人工锁定与重算、版本不可变、决策报告幂等导出与来源追溯。
"""
from __future__ import annotations

import pytest

QUOTE_A = {
    "provider": "长光航天", "vehicle": "长征八号", "launch_site": "文昌",
    "earliest_date": "2026-10-01", "latest_date": "2026-12-20", "currency": "CNY",
    "base_price_value": 200000, "per_kg_price_value": 5000,
    "capacity_value": 500, "capacity_unit": "kg",
    "fairing_length_value": 4.5, "fairing_length_unit": "m",
    "fairing_width_value": 3.0, "fairing_width_unit": "m",
    "fairing_height_value": 3.0, "fairing_height_unit": "m",
}
QUOTE_B = {
    "provider": "星河动力", "vehicle": "谷神星一号", "launch_site": "酒泉",
    "earliest_date": "2026-09-01", "latest_date": "2026-11-30", "currency": "CNY",
    "base_price_value": 80000, "per_kg_price_value": 8000,
    "capacity_value": 300000, "capacity_unit": "g",
    "fairing_length_value": 400, "fairing_length_unit": "cm",
    "fairing_width_value": 200, "fairing_width_unit": "cm",
    "fairing_height_value": 200, "fairing_height_unit": "cm",
}
REQUIREMENT = {
    "payload_code": "SAT-A", "name": "光学卫星A",
    "unit_mass_value": 120, "unit_mass_unit": "kg",
    "length_value": 1.8, "length_unit": "m",
    "width_value": 1.5, "width_unit": "m",
    "height_value": 1.5, "height_unit": "m",
    "copies": 2, "redundancy_spares": 1,
}


def import_batch(client, rows, batch_no="Q-2026-09", actor="buyer-zhang"):
    response = client.post(
        f"/api/rideshare/quote-batches?actor={actor}",
        json={"batch_no": batch_no, "supplier": "三家承运商", "rows": rows},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_requirement(client, payload=None, actor="buyer-zhang"):
    response = client.put(f"/api/rideshare/payload-requirements?actor={actor}", json=payload or REQUIREMENT)
    assert response.status_code == 200, response.text
    return response.json()


def create_comparison(client, deadline="2026-12-31", comparison_no="CMP-001", actor="buyer-zhang"):
    response = client.post(
        f"/api/rideshare/comparisons?actor={actor}",
        json={
            "comparison_no": comparison_no,
            "title": "首批拼车",
            "deadline": deadline,
            "quote_batch_nos": ["Q-2026-09"],
            "payload_codes": ["SAT-A"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def prepared(client):
    batch = import_batch(client, [QUOTE_A, QUOTE_B])
    create_requirement(client)
    comparison = create_comparison(client)
    return {"batch": batch, "comparison": comparison}


# ---------------------------------------------------------------------------
# 报价导入
# ---------------------------------------------------------------------------

def test_import_rejects_rows_with_missing_units_and_out_of_range(client):
    bad_rows = [
        {**QUOTE_A, "capacity_unit": ""},                      # 缺单位
        {**QUOTE_A, "fairing_length_unit": None},              # 缺单位
        {**QUOTE_A, "base_price_value": -5},                   # 超范围
        {**QUOTE_A, "capacity_value": 9e12, "capacity_unit": "kg"},  # 超范围
        {**QUOTE_A, "earliest_date": "2026-13-40", "currency": "CNY"},   # 非法日期
        {**QUOTE_B, "currency": "rmb"},                        # 非法币种
        "not-a-dict",                                          # 非对象行
    ]
    result = import_batch(client, [QUOTE_A, *bad_rows])
    assert result["accepted_count"] == 1
    assert result["rejected_count"] == len(bad_rows)
    by_index = {item["row_index"]: item["errors"] for item in result["rejected"]}
    assert any("capacity_unit 缺少单位" in error for error in by_index[1])
    assert any("fairing_length_unit 缺少单位" in error for error in by_index[2])
    assert any("超出允许范围" in error for error in by_index[3])
    assert any("超出允许范围" in error for error in by_index[4])
    assert any("YYYY-MM-DD" in error for error in by_index[5])
    assert any("币种" in error for error in by_index[6])
    assert by_index[7] == ["行内容不是 JSON 对象"]


def test_import_is_idempotent_per_batch_and_rejects_conflicting_content(client):
    first = import_batch(client, [QUOTE_A, QUOTE_B])
    second = import_batch(client, [QUOTE_A, QUOTE_B])
    assert second["replayed"] is True
    assert second["batch_id"] == first["batch_id"]
    assert second["accepted"] == first["accepted"]

    batches = client.get("/api/rideshare/quote-batches").json()["items"]
    assert len(batches) == 1

    conflict = client.post(
        "/api/rideshare/quote-batches?actor=buyer-zhang",
        json={"batch_no": "Q-2026-09", "rows": [QUOTE_A]},
    )
    assert conflict.status_code == 409
    assert "不允许覆盖" in conflict.json()["error"]["message"]

    # 报价更新走新批次号，旧批次保持原样
    updated = import_batch(client, [{**QUOTE_A, "per_kg_price_value": 4500}], batch_no="Q-2026-10")
    assert updated["accepted_count"] == 1
    original = client.get("/api/rideshare/quote-batches/Q-2026-09").json()
    assert original["rows"][0]["per_kg_price_value"] == QUOTE_A["per_kg_price_value"]


def test_quote_row_trace_endpoint_shows_source_and_normalized_units(client):
    batch = import_batch(client, [QUOTE_B])
    row_id = batch["accepted"][0]["quote_row_id"]
    row = client.get(f"/api/rideshare/quote-rows/{row_id}").json()
    assert row["batch"]["batch_no"] == "Q-2026-09"
    assert row["batch"]["imported_by"] == "buyer-zhang"
    assert row["normalized"]["capacity_kg"] == pytest.approx(300.0)
    assert row["normalized"]["fairing_length_m"] == pytest.approx(4.0)
    assert row["row_digest"] == batch["accepted"][0]["row_digest"]


# ---------------------------------------------------------------------------
# 载荷需求
# ---------------------------------------------------------------------------

def test_requirement_validation_and_versioning(client):
    create_requirement(client)
    updated = create_requirement(client, {**REQUIREMENT, "copies": 3})
    assert updated["version"] == 2 and updated["copies"] == 3

    missing_unit = client.put(
        "/api/rideshare/payload-requirements?actor=buyer-zhang",
        json={**REQUIREMENT, "payload_code": "SAT-BAD", "unit_mass_unit": ""},
    )
    assert missing_unit.status_code == 422

    out_of_range = client.put(
        "/api/rideshare/payload-requirements?actor=buyer-zhang",
        json={**REQUIREMENT, "payload_code": "SAT-HUGE", "unit_mass_value": 9e12},
    )
    assert out_of_range.status_code == 422


# ---------------------------------------------------------------------------
# 候选组合与成本分解
# ---------------------------------------------------------------------------

def test_candidates_carry_cost_breakdown_and_sources(client, prepared):
    comparison = prepared["comparison"]
    version = client.get(f"/api/rideshare/comparisons/CMP-001/versions/{comparison['current_version']}").json()
    assert version["engine_version"].startswith("rideshare-engine-")
    assert version["input_digest"]
    candidates = version["candidates"]
    assert candidates, "应至少生成一个候选组合"

    best = candidates[0]
    assert best["rank"] == 1
    assert best["satisfies_deadline"] is True
    # 最优方案：3 件全部装入报价 1（运力 500kg >= 360kg）
    assert best["total_mass"]["value"] == pytest.approx(360.0)
    assert best["total_cost"]["value"] == pytest.approx(200000 + 5000 * 360)
    assert best["effective_per_kg"]["value"] == pytest.approx((200000 + 5000 * 360) / 360, rel=1e-4)
    assert best["effective_per_kg"]["unit"] == "CNY/kg"

    allocation = best["allocations"][0]
    assert allocation["allocated_mass"]["value"] == pytest.approx(360.0)
    assert allocation["capacity"]["value"] == pytest.approx(500.0)
    assert allocation["utilization"]["value"] == pytest.approx(0.72)
    assert allocation["base_fee"]["value"] == pytest.approx(200000)
    assert allocation["mass_fee"]["value"] == pytest.approx(5000 * 360)
    assert allocation["subtotal"]["value"] == pytest.approx(allocation["base_fee"]["value"] + allocation["mass_fee"]["value"])
    # 每个数字都能追溯到报价行字段或需求字段
    for field in ("allocated_mass", "capacity", "base_fee", "mass_fee", "subtotal"):
        assert allocation[field]["sources"], field
    assert any(source.startswith("quote-row:") for source in allocation["subtotal"]["sources"])
    assert any(source.startswith("requirement:") for source in allocation["allocated_mass"]["sources"])


def test_deadline_marks_late_combinations(client):
    import_batch(client, [QUOTE_A, QUOTE_B])
    # 2 件载荷（1 正件 + 1 冗余件）= 240kg，报价 B 的 300kg 运力可以单独承运
    create_requirement(client, {**REQUIREMENT, "copies": 1, "redundancy_spares": 1})
    comparison = create_comparison(client, deadline="2026-12-01")
    version = client.get(f"/api/rideshare/comparisons/CMP-001/versions/{comparison['current_version']}").json()
    candidates = version["candidates"]
    assert candidates
    # 报价 A 最晚 12-20 超出 12-01 交付期限；纯 B 组合（11-30 前）满足期限
    feasible = [item for item in candidates if item["satisfies_deadline"]]
    late = [item for item in candidates if not item["satisfies_deadline"]]
    assert feasible and late
    for item in feasible:
        assert item["latest_launch"] <= "2026-12-01"
        assert all(allocation["provider"] == "星河动力" for allocation in item["allocations"])
    assert any(any(allocation["provider"] == "长光航天" for allocation in item["allocations"]) for item in late)


def test_redundancy_spares_are_counted_in_total_mass(client, prepared):
    comparison = prepared["comparison"]
    version = client.get(f"/api/rideshare/comparisons/CMP-001/versions/{comparison['current_version']}").json()
    snapshot = version["input_snapshot"]
    requirement = snapshot["requirements"][0]
    assert requirement["copies"] == 2 and requirement["redundancy_spares"] == 1
    best = version["candidates"][0]
    # 2 正件 + 1 冗余件 = 3 件 × 120kg
    assert best["total_mass"]["value"] == pytest.approx(3 * 120.0)


# ---------------------------------------------------------------------------
# 人工锁定、重算与版本不可变
# ---------------------------------------------------------------------------

def test_lock_recomputes_only_unlocked_parts(client, prepared):
    batch = prepared["batch"]
    comparison = prepared["comparison"]
    row_b = batch["accepted"][1]["quote_row_id"]

    locked = client.post(
        "/api/rideshare/comparisons/CMP-001/locks",
        json={"quote_row_id": row_b, "units": ["SAT-A"], "actor": "leader-wang", "reason": "战略备份"},
    )
    assert locked.status_code == 201, locked.text
    version = locked.json()
    assert version["version"] == comparison["current_version"] + 1
    assert version["trigger"] == "lock"
    assert version["fixed_slots"] == {str(row_b): ["SAT-A"]}

    for candidate in version["candidates"]:
        locked_allocations = [item for item in candidate["allocations"] if item["quote_row_id"] == row_b]
        assert locked_allocations, "每个候选都必须包含锁定槽位"
        assert locked_allocations[0]["locked"] is True
        assert locked_allocations[0]["locked_unit_count"] == 1
        assert locked_allocations[0]["locked_units"] == ["SAT-A"]
        assert locked_allocations[0]["unit_count"] >= 1

    detail = client.get("/api/rideshare/comparisons/CMP-001").json()
    assert detail["status"] == "locked"
    assert detail["locks"][0]["quote_row_id"] == row_b
    assert detail["locks"][0]["reason"] == "战略备份"


def test_versions_are_immutable_after_quote_update_and_recompute(client, prepared):
    batch = prepared["batch"]
    comparison = prepared["comparison"]
    row_a = batch["accepted"][0]["quote_row_id"]

    client.post(
        "/api/rideshare/comparisons/CMP-001/locks",
        json={"quote_row_id": row_a, "units": ["SAT-A", "SAT-A"], "actor": "leader-wang", "reason": "主星已定"},
    )
    before = client.get("/api/rideshare/comparisons/CMP-001/versions/2").json()

    # 报价更新：新批次导入更低价格，比较试图切换到仅新批次并重算
    import_batch(client, [{**QUOTE_A, "per_kg_price_value": 4000}], batch_no="Q-2026-10")
    switched = client.post(
        "/api/rideshare/comparisons/CMP-001/recompute",
        json={"actor": "buyer-zhang", "reason": "仅采用新报价", "quote_batch_nos": ["Q-2026-10"]},
    )
    # 锁定的报价行来自旧批次，切出范围后明确报错，锁定论证不会被悄悄改写
    assert switched.status_code == 409
    assert switched.json()["error"]["code"] == "conflict"
    # 比较版本号没有增加
    detail = client.get("/api/rideshare/comparisons/CMP-001").json()
    assert detail["current_version"] == 2

    recomputed = client.post(
        "/api/rideshare/comparisons/CMP-001/recompute",
        json={"actor": "buyer-zhang", "reason": "补充新批次但保留旧批次", "quote_batch_nos": ["Q-2026-10", "Q-2026-09"]},
    )
    assert recomputed.status_code == 201, recomputed.text
    new_version = recomputed.json()
    assert new_version["version"] == 3
    # 锁定槽位原样保留
    assert new_version["fixed_slots"] == {str(row_a): ["SAT-A", "SAT-A"]}

    after = client.get("/api/rideshare/comparisons/CMP-001/versions/2").json()
    assert after["candidates"] == before["candidates"]
    assert after["input_digest"] == before["input_digest"]


def test_unlock_restores_draft_status(client, prepared):
    batch = prepared["batch"]
    row_b = batch["accepted"][1]["quote_row_id"]
    client.post(
        "/api/rideshare/comparisons/CMP-001/locks",
        json={"quote_row_id": row_b, "units": ["SAT-A"], "actor": "leader-wang", "reason": "备份"},
    )
    unlocked = client.post(
        "/api/rideshare/comparisons/CMP-001/unlocks",
        json={"quote_row_id": row_b, "actor": "leader-wang", "reason": "战略调整"},
    )
    assert unlocked.status_code == 201
    assert unlocked.json()["trigger"] == "unlock"
    detail = client.get("/api/rideshare/comparisons/CMP-001").json()
    assert detail["status"] == "draft"
    assert detail["locks"] == []


def test_lock_rejects_unknown_quote_row_and_payload(client, prepared):
    response = client.post(
        "/api/rideshare/comparisons/CMP-001/locks",
        json={"quote_row_id": 9999, "units": ["SAT-A"], "actor": "leader-wang", "reason": "无效"},
    )
    assert response.status_code == 422
    row_a = prepared["batch"]["accepted"][0]["quote_row_id"]
    response = client.post(
        "/api/rideshare/comparisons/CMP-001/locks",
        json={"quote_row_id": row_a, "units": ["SAT-UNKNOWN"], "actor": "leader-wang", "reason": "无效"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 决策报告
# ---------------------------------------------------------------------------

def test_report_export_is_idempotent_per_comparison_version(client, prepared):
    first = client.post("/api/rideshare/comparisons/CMP-001/reports", json={"actor": "leader-wang", "title": "九月底版"}).json()
    assert first["report_version"] == 1
    assert first["comparison_version"] == 1
    assert first["replayed"] is False
    assert first["content_digest"]

    again = client.post("/api/rideshare/comparisons/CMP-001/reports", json={"actor": "leader-wang"}).json()
    assert again["report_version"] == 1 and again["replayed"] is True
    assert again["content_digest"] == first["content_digest"]

    # 重算产生新比较版本后，导出得到新的报告版本，旧报告内容不变
    client.post("/api/rideshare/comparisons/CMP-001/recompute", json={"actor": "buyer-zhang", "reason": "例行重算"})
    second = client.post("/api/rideshare/comparisons/CMP-001/reports", json={"actor": "leader-wang"}).json()
    assert second["report_version"] == 2
    assert second["comparison_version"] == 2

    reports = client.get("/api/rideshare/comparisons/CMP-001/reports").json()["items"]
    assert [item["report_version"] for item in reports] == [1, 2]
    old = client.get("/api/rideshare/comparisons/CMP-001/reports/1").json()
    assert old["content_digest"] == first["content_digest"]
    assert old["content"]["comparison_version"] == 1


def test_report_content_traces_every_number(client, prepared):
    report = client.post("/api/rideshare/comparisons/CMP-001/reports", json={"actor": "leader-wang"}).json()
    content = report["content"]
    assert content["engine_version"] == report["engine_version"]
    assert content["input_digest"]
    assert content["inputs"]["quote_rows"], "报告必须包含报价快照"
    assert content["inputs"]["requirements"], "报告必须包含需求快照"
    for candidate in content["candidates"]:
        for key in ("total_cost", "total_mass", "effective_per_kg"):
            assert candidate[key]["sources"], key
        for allocation in candidate["allocations"]:
            assert allocation["subtotal"]["sources"]
    # 快照中的报价行与候选引用的 quote-row 来源一致
    snapshot_ids = {row["id"] for row in content["inputs"]["quote_rows"]}
    for candidate in content["candidates"]:
        for allocation in candidate["allocations"]:
            assert allocation["quote_row_id"] in snapshot_ids


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def test_audit_trail_records_decisions(client, prepared):
    row_b = prepared["batch"]["accepted"][1]["quote_row_id"]
    client.post(
        "/api/rideshare/comparisons/CMP-001/locks",
        json={"quote_row_id": row_b, "units": ["SAT-A"], "actor": "leader-wang", "reason": "战略备份"},
    )
    client.post("/api/rideshare/comparisons/CMP-001/reports", json={"actor": "leader-wang"})
    actions = [item["action"] for item in client.get("/api/rideshare/comparisons/CMP-001/audit").json()["items"]]
    assert "comparison.create" in actions
    assert "comparison.lock" in actions
    assert "report.export" in actions
