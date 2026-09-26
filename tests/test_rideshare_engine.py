"""拼车比较计算引擎的单元测试：单位换算、可行性、枚举边界与成本分解。"""
from __future__ import annotations

import pytest

from app.rideshare import engine


def offer_row(quote_id, **overrides):
    base = {
        "id": quote_id, "batch_no": "Q1", "provider": f"P{quote_id}", "vehicle": "V", "launch_site": "S",
        "earliest_date": "2026-10-01", "latest_date": "2026-12-01", "currency": "CNY",
        "base_price_value": 100_000, "per_kg_price_value": 5_000,
        "capacity_value": 500, "capacity_unit": "kg",
        "fairing_length_value": 4, "fairing_length_unit": "m",
        "fairing_width_value": 3, "fairing_width_unit": "m",
        "fairing_height_value": 3, "fairing_height_unit": "m",
    }
    base.update(overrides)
    return base


def requirement(code="SAT", mass=100, copies=1, spares=0, size=(2, 2, 2)):
    return engine.build_requirement({
        "payload_code": code,
        "unit_mass_value": mass, "unit_mass_unit": "kg",
        "length_value": size[0], "length_unit": "m",
        "width_value": size[1], "width_unit": "m",
        "height_value": size[2], "height_unit": "m",
        "copies": copies, "redundancy_spares": spares,
    })


def test_unit_conversion_mass_and_length():
    pound, _ = engine.convert_quantity(100, "lb", engine.MASS_TO_KG, "质量")
    assert pound == pytest.approx(45.359237)
    inch, _ = engine.convert_quantity(12, "in", engine.LENGTH_TO_M, "长度")
    assert inch == pytest.approx(0.3048)
    with pytest.raises(engine.EngineError, match="缺少单位"):
        engine.convert_quantity(1, "", engine.MASS_TO_KG, "质量")
    with pytest.raises(engine.EngineError, match="单位无法识别"):
        engine.convert_quantity(1, "parsec", engine.LENGTH_TO_M, "长度")
    with pytest.raises(engine.EngineError, match="不是合法数字"):
        engine.convert_quantity("abc", "kg", engine.MASS_TO_KG, "质量")


def test_zero_and_negative_quantities_rejected():
    with pytest.raises(engine.EngineError, match="正数"):
        engine.build_offer(offer_row(1, capacity_value=0))
    with pytest.raises(engine.EngineError, match="正数"):
        engine.build_offer(offer_row(2, fairing_height_value=-1, fairing_height_unit="m"))
    with pytest.raises(engine.EngineError, match="超出允许范围"):
        engine.build_offer(offer_row(3, base_price_value=1e15))


def test_currency_mismatch_rejected():
    offers = [
        engine.build_offer(offer_row(1)),
        engine.build_offer(offer_row(2, currency="USD")),
    ]
    with pytest.raises(engine.EngineError, match="币种不一致"):
        engine.generate_candidates(offers, [requirement()])


def test_dimension_envelope_allows_rotation_but_not_oversize():
    offer = engine.build_offer(offer_row(1, fairing_length_value=2, fairing_length_unit="m",
                                         fairing_width_value=3, fairing_width_unit="m",
                                         fairing_height_value=2, fairing_height_unit="m"))
    wide = requirement(size=(3, 2, 2))   # 旋转 90 度可装入
    tall = requirement(size=(2, 2, 3))   # 超高不可装
    assert engine.fits_dimensions(wide, offer) is True
    assert engine.fits_dimensions(tall, offer) is False


def test_candidates_cover_all_distributions_without_duplicates():
    offers = [engine.build_offer(offer_row(1)), engine.build_offer(offer_row(2))]
    req = requirement(copies=2, spares=1)  # 3 件
    candidates = engine.generate_candidates(offers, [req], deadline="2026-12-31")
    signatures = {candidate.signature for candidate in candidates}
    assert len(signatures) == len(candidates)
    # 3 件在两条等价报价间的分布数（含只用一条的情形）
    assert len(candidates) == 4
    for candidate in candidates:
        total_units = sum(allocation.unit_count for allocation in candidate.allocations)
        assert total_units == 3
    assert candidates[0].rank == 1  # 已按成本排序


def test_capacity_constraint_excludes_overloaded_combinations():
    # 每条报价只能装 1 件（150kg 运力 < 2×100kg），3 件必须三发三一
    offers = [
        engine.build_offer(offer_row(1, capacity_value=150)),
        engine.build_offer(offer_row(2, capacity_value=150)),
        engine.build_offer(offer_row(3, capacity_value=150)),
    ]
    req = requirement(copies=3)
    candidates = engine.generate_candidates(offers, [req])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert len(candidate.allocations) == 3
    for allocation in candidate.allocations:
        assert allocation.unit_count == 1


def test_fixed_slots_are_preserved_and_remainder_resolved():
    offers = [engine.build_offer(offer_row(1)), engine.build_offer(offer_row(2))]
    req = requirement(copies=2, spares=0)
    candidates = engine.generate_candidates(offers, [req], fixed_slots={2: ["SAT"]})
    assert candidates
    # 锁定 1 件后剩余 1 件可分给任一条，共 2 种组合；锁定槽位在每种组合中都至少 1 件
    assert len(candidates) == 2
    assert all(any(a.quote_row_id == 2 and a.unit_count >= 1 for a in c.allocations) for c in candidates)


def test_fixed_slot_exceeding_demand_rejected():
    offers = [engine.build_offer(offer_row(1))]
    req = requirement(copies=1)
    with pytest.raises(engine.EngineError, match="锁定件数超过需求"):
        engine.generate_candidates(offers, [req], fixed_slots={1: ["SAT", "SAT"]})


def test_search_space_is_bounded():
    # 4 条报价、两种载荷各 60 件且件小容量大 -> 件数向量组合远超上限
    offers = [
        engine.build_offer(offer_row(idx, capacity_value=1_000_000,
                                     fairing_length_value=100, fairing_length_unit="m",
                                     fairing_width_value=100, fairing_width_unit="m",
                                     fairing_height_value=100, fairing_height_unit="m"))
        for idx in range(1, 5)
    ]
    requirements = [requirement("A", copies=60), requirement("B", mass=50, copies=60)]
    with pytest.raises(engine.EngineError, match="搜索空间超过上限"):
        engine.generate_candidates(offers, requirements)


def test_cost_breakdown_arithmetic_and_sources():
    offers = [engine.build_offer(offer_row(1, base_price_value=50_000, per_kg_price_value=2_000))]
    req = requirement(mass=120, copies=2)
    candidate = engine.generate_candidates(offers, [req])[0]
    allocation = candidate.allocations[0]
    assert allocation.base_fee["value"] == 50_000
    assert allocation.mass_fee["value"] == pytest.approx(2_000 * 240)
    assert allocation.subtotal["value"] == pytest.approx(50_000 + 2_000 * 240)
    assert candidate.total_cost["value"] == pytest.approx(allocation.subtotal["value"])
    assert candidate.effective_per_kg["value"] == pytest.approx(allocation.subtotal["value"] / 240)
    sources = allocation.subtotal["sources"]
    assert "quote-row:1#base_price" in sources
    assert "quote-row:1#per_kg_price" in sources
    assert "requirement:SAT#unit_mass" in sources
