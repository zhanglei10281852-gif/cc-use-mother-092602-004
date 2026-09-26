"""拼车发射方案计算引擎（纯函数，不触碰数据库与 HTTP）。

所有金额与质量在内部统一换算为基础单位：
- 质量：千克（kg）
- 长度：米（m）
- 货币：由报价声明的 currency 原样保留（比较时要求同一币种）

每个计算结果都以 :class:`Quantity` 携带数值、单位和来源，便于审计追溯。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: 计算引擎版本。任何成本分解口径或约束算法的变更都必须提升该版本，
#: 已生成的比较版本不会因此被改写，只影响之后的计算。
ENGINE_VERSION = "rideshare-engine-1.0.0"

MASS_TO_KG: dict[str, float] = {
    "kg": 1.0,
    "kilogram": 1.0,
    "kilograms": 1.0,
    "千克": 1.0,
    "公斤": 1.0,
    "g": 0.001,
    "gram": 0.001,
    "grams": 0.001,
    "克": 0.001,
    "lb": 0.45359237,
    "lbs": 0.45359237,
    "pound": 0.45359237,
    "pounds": 0.45359237,
    "磅": 0.45359237,
}

LENGTH_TO_M: dict[str, float] = {
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "米": 1.0,
    "cm": 0.01,
    "centimeter": 0.01,
    "centimeters": 0.01,
    "厘米": 0.01,
    "mm": 0.001,
    "millimeter": 0.001,
    "millimeters": 0.001,
    "毫米": 0.001,
    "in": 0.0254,
    "inch": 0.0254,
    "inches": 0.0254,
    "英寸": 0.0254,
    "ft": 0.3048,
    "foot": 0.3048,
    "feet": 0.3048,
    "英尺": 0.3048,
}

#: 报价行数值字段允许的范围（基础单位）。
PRICE_LIMITS = {
    "base_price": (0.0, 1.0e12),
    "per_kg_price": (0.0, 1.0e9),
    "capacity_kg": (0.0, 1.0e7),
    "fairing_length_m": (0.0, 1.0e4),
    "fairing_width_m": (0.0, 1.0e4),
    "fairing_height_m": (0.0, 1.0e4),
}

#: 载荷需求字段允许的范围（基础单位）。
REQUIREMENT_LIMITS = {
    "unit_mass": (0.0, 1.0e7),
    "length": (0.0, 1.0e4),
    "width": (0.0, 1.0e4),
    "height": (0.0, 1.0e4),
    "copies": (1, 1_000_000),
    "redundancy_spares": (0, 1_000_000),
}

#: ISO 4217 三位字母币种代码集合（比较时仅允许同币种直接比价）。
ISO_CURRENCIES = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD
CAD CDF CHE CHW CLF CLP CNH CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP
GEL GHS GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW
KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD
NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLL
SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES
VND VUV WST XAF XAG XAU XBA XBB XBC XBD XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW ZWL
""".split())

MAX_OFFERS_PER_COMBINATION = 8
MAX_CANDIDATES = 500
MAX_SEARCH_SPACE = 1_000_000


class EngineError(ValueError):
    """计算输入无法形成有效比较。"""


@dataclass(frozen=True, slots=True)
class Quantity:
    """带单位与来源的数值。"""

    value: float
    unit: str
    sources: tuple[str, ...]

    def as_dict(self, ndigits: int = 6) -> dict[str, Any]:
        rendered = round(self.value, ndigits)
        return {"value": rendered, "unit": self.unit, "sources": list(self.sources)}


@dataclass(frozen=True, slots=True)
class Offer:
    quote_row_id: int
    batch_no: str
    provider: str
    vehicle: str
    launch_site: str
    earliest_date: str
    latest_date: str
    currency: str
    base_price: float
    per_kg_price: float
    capacity_kg: float
    fairing_length_m: float
    fairing_width_m: float
    fairing_height_m: float

    @property
    def key(self) -> str:
        return f"{self.provider}|{self.vehicle}|{self.launch_site}"

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.vehicle}@{self.launch_site}"


@dataclass(frozen=True, slots=True)
class PayloadUnit:
    mass_kg: float
    length_m: float
    width_m: float
    height_m: float


@dataclass(frozen=True, slots=True)
class Requirement:
    payload_code: str
    unit: PayloadUnit
    copies: int
    redundancy_spares: int

    @property
    def total_units(self) -> int:
        return self.copies + self.redundancy_spares

    @property
    def total_mass_kg(self) -> float:
        return self.unit.mass_kg * self.total_units


@dataclass(frozen=True, slots=True)
class LoadedOffer:
    offer: Offer
    units: tuple[str, ...]  # payload_code 重复列表，长度为装入件数
    allocated_mass_kg: float
    fits: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Allocation:
    """组合中一条报价的装载与计费明细。"""

    quote_row_id: int
    offer_label: str
    provider: str
    vehicle: str
    launch_site: str
    launch_date: str
    currency: str
    units: list[dict[str, str]]
    unit_count: int
    allocated_mass: dict[str, Any]
    capacity: dict[str, Any]
    utilization: dict[str, Any]
    base_fee: dict[str, Any]
    mass_fee: dict[str, Any]
    subtotal: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Candidate:
    signature: str
    allocations: list[Allocation]
    total_mass: dict[str, Any]
    total_cost: dict[str, Any]
    effective_per_kg: dict[str, Any]
    earliest_launch: str
    latest_launch: str
    satisfies_deadline: bool
    currency: str
    rank: int = 0


# ---------------------------------------------------------------------------
# 单位换算与范围校验
# ---------------------------------------------------------------------------

def normalize_unit(raw: str | None, table: dict[str, float], label: str) -> tuple[float, str]:
    """把 ``数值 + 单位`` 的单位部分归一化，返回 (换算系数, 规范单位)。"""
    if raw is None or not str(raw).strip():
        raise EngineError(f"{label}缺少单位")
    token = str(raw).strip().lower()
    factor = table.get(token)
    if factor is None:
        raise EngineError(f"{label}单位无法识别：{raw}")
    canonical = "kg" if table is MASS_TO_KG else "m"
    return factor, canonical


def convert_quantity(raw_value: Any, raw_unit: str | None, table: dict[str, float], label: str) -> tuple[float, str]:
    if isinstance(raw_value, bool):
        raise EngineError(f"{label}不是合法数字：{raw_value!r}")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise EngineError(f"{label}不是合法数字：{raw_value!r}") from None
    factor, canonical = normalize_unit(raw_unit, table, label)
    return value * factor, canonical


def check_range(name: str, value: float, limits: tuple[float, float]) -> None:
    low, high = limits
    if value < low or value > high:
        raise EngineError(f"{name}超出允许范围 [{low}, {high}]（归一化后为 {value:g}）")


def build_offer(row: dict[str, Any]) -> Offer:
    """从已落库的报价行构造归一化报价；任何单位/范围问题在此抛出。"""
    capacity_kg, _ = convert_quantity(row["capacity_value"], row["capacity_unit"], MASS_TO_KG, "运力")
    if capacity_kg <= 0:
        raise EngineError(f"运力必须为正数（归一化后为 {capacity_kg:g}kg）")
    check_range("运力", capacity_kg, PRICE_LIMITS["capacity_kg"])
    length_m, _ = convert_quantity(row["fairing_length_value"], row["fairing_length_unit"], LENGTH_TO_M, "整流罩长度")
    width_m, _ = convert_quantity(row["fairing_width_value"], row["fairing_width_unit"], LENGTH_TO_M, "整流罩宽度")
    height_m, _ = convert_quantity(row["fairing_height_value"], row["fairing_height_unit"], LENGTH_TO_M, "整流罩高度")
    for name, value in (("整流罩长度", length_m), ("整流罩宽度", width_m), ("整流罩高度", height_m)):
        if value <= 0:
            raise EngineError(f"{name}必须为正数（归一化后为 {value:g}m）")
        check_range(name, value, PRICE_LIMITS["fairing_length_m"])
    base_price = float(row["base_price_value"])
    per_kg = float(row["per_kg_price_value"])
    check_range("起步价", base_price, PRICE_LIMITS["base_price"])
    check_range("每公斤价格", per_kg, PRICE_LIMITS["per_kg_price"])
    return Offer(
        quote_row_id=int(row["id"]),
        batch_no=row["batch_no"],
        provider=row["provider"],
        vehicle=row["vehicle"],
        launch_site=row["launch_site"],
        earliest_date=row["earliest_date"],
        latest_date=row["latest_date"],
        currency=row["currency"],
        base_price=base_price,
        per_kg_price=per_kg,
        capacity_kg=capacity_kg,
        fairing_length_m=length_m,
        fairing_width_m=width_m,
        fairing_height_m=height_m,
    )


def build_requirement(item: dict[str, Any]) -> Requirement:
    mass_kg, _ = convert_quantity(item["unit_mass_value"], item["unit_mass_unit"], MASS_TO_KG, f"载荷 {item.get('payload_code') or ''} 单件质量")
    length_m, _ = convert_quantity(item["length_value"], item["length_unit"], LENGTH_TO_M, "载荷长度")
    width_m, _ = convert_quantity(item["width_value"], item["width_unit"], LENGTH_TO_M, "整流罩/载荷宽度")
    height_m, _ = convert_quantity(item["height_value"], item["height_unit"], LENGTH_TO_M, "载荷高度")
    copies = int(item["copies"])
    spares = int(item["redundancy_spares"])
    for label, value in (("单件质量", mass_kg), ("长度", length_m), ("宽度", width_m), ("高度", height_m)):
        if value <= 0:
            raise EngineError(f"{label}必须为正数（归一化后为 {value:g}）")
    check_range("单件质量", mass_kg, REQUIREMENT_LIMITS["unit_mass"])
    check_range("长度", length_m, REQUIREMENT_LIMITS["length"])
    check_range("宽度", width_m, REQUIREMENT_LIMITS["width"])
    check_range("高度", height_m, REQUIREMENT_LIMITS["height"])
    if not (1 <= copies <= REQUIREMENT_LIMITS["copies"][1]):
        raise EngineError("副本数必须为正整数且不超过 1000000")
    if not (0 <= spares <= REQUIREMENT_LIMITS["redundancy_spares"][1]):
        raise EngineError("冗余件数必须为非负整数且不超过 1000000")
    return Requirement(
        payload_code=str(item["payload_code"]),
        unit=PayloadUnit(mass_kg, length_m, width_m, height_m),
        copies=copies,
        redundancy_spares=spares,
    )


# ---------------------------------------------------------------------------
# 装载可行性
# ---------------------------------------------------------------------------

def fits_dimensions(requirement: Requirement, offer: Offer) -> bool:
    """允许在整流罩横截面上旋转 90 度（长宽互换），高度不互换。"""
    envelope = offer.fairing_length_m
    width = offer.fairing_width_m
    height = offer.fairing_height_m
    unit = requirement.unit
    normal = unit.length_m <= envelope and unit.width_m <= width
    rotated = unit.width_m <= envelope and unit.length_m <= width
    return (normal or rotated) and unit.height_m <= height


def load_offer(offer: Offer, requirements: list[Requirement], counts: dict[str, int]) -> LoadedOffer:
    """按给定件数把载荷装入一条报价，判定质量、尺寸与件数可行性。"""
    reasons: list[str] = []
    units: list[str] = []
    mass = 0.0
    for requirement in requirements:
        amount = counts.get(requirement.payload_code, 0)
        if amount <= 0:
            continue
        if not fits_dimensions(requirement, offer):
            reasons.append(f"载荷 {requirement.payload_code} 尺寸超出 {offer.label} 整流罩")
        units.extend([requirement.payload_code] * amount)
        mass += requirement.unit.mass_kg * amount
    if mass - offer.capacity_kg > 1e-9:
        reasons.append(f"装载质量 {mass:g}kg 超过 {offer.label} 运力 {offer.capacity_kg:g}kg")
    return LoadedOffer(offer=offer, units=tuple(units), allocated_mass_kg=mass, fits=not reasons, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# 候选组合枚举
# ---------------------------------------------------------------------------

def _group_counts(units: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for code in units:
        counts[code] = counts.get(code, 0) + 1
    return counts


def generate_candidates(
    offers: list[Offer],
    requirements: list[Requirement],
    *,
    deadline: str | None = None,
    fixed_slots: dict[int, list[str]] | None = None,
) -> list[Candidate]:
    """枚举可行候选组合。

    ``fixed_slots`` 为 ``{报价行id: [载荷编码, ...]}``，表示人工锁定的槽位：
    这些件必然由指定报价承运，求解器只需要安排剩余件；同时会校验锁定槽位
    本身仍然可行（质量、尺寸、最晚交付日期）。
    """
    if not offers:
        raise EngineError("没有可用报价，无法生成候选组合")
    if not requirements:
        raise EngineError("没有载荷需求，无法生成候选组合")
    currencies = {offer.currency for offer in offers}
    if len(currencies) > 1:
        raise EngineError(f"报价币种不一致，无法直接比较：{sorted(currencies)}")
    currency = currencies.pop()

    fixed_slots = fixed_slots or {}
    offers_by_id = {offer.quote_row_id: offer for offer in offers}
    missing = [row_id for row_id in fixed_slots if row_id not in offers_by_id]
    if missing:
        raise EngineError(f"锁定的报价行不在当前报价集中：{sorted(missing)}")

    requirement_by_code = {item.payload_code: item for item in requirements}
    remaining_demand = {item.payload_code: item.total_units for item in requirements}
    fixed_loads: dict[int, LoadedOffer] = {}
    for row_id, unit_list in fixed_slots.items():
        offer = offers_by_id[row_id]
        counts = _group_counts(tuple(unit_list))
        unknown = sorted(set(counts) - set(requirement_by_code))
        if unknown:
            raise EngineError(f"锁定槽位包含未知载荷：{unknown}")
        for code, amount in counts.items():
            if remaining_demand[code] < amount:
                raise EngineError(f"载荷 {code} 锁定件数超过需求件数（含冗余件）")
            remaining_demand[code] -= amount
        loaded = load_offer(offer, requirements, counts)
        if not loaded.fits:
            raise EngineError(f"锁定组合不可行：{'；'.join(loaded.reasons)}")
        fixed_loads[row_id] = loaded

    remaining_types = [
        (requirement, amount)
        for requirement in requirements
        if (amount := remaining_demand[requirement.payload_code]) > 0
    ]

    # 每条报价在锁定槽位之外还能容纳多少质量，以及哪些载荷类型装得进去。
    residual_capacity = {
        offer.quote_row_id: offer.capacity_kg - fixed_loads[offer.quote_row_id].allocated_mass_kg
        if offer.quote_row_id in fixed_loads else offer.capacity_kg
        for offer in offers
    }
    compatible: dict[str, list[int]] = {}
    for requirement, _ in remaining_types:
        rows = [offer.quote_row_id for offer in offers if fits_dimensions(requirement, offer)]
        if not rows:
            raise EngineError(f"载荷 {requirement.payload_code} 无法装入任何报价的整流罩")
        compatible[requirement.payload_code] = rows

    _assert_search_bounded(remaining_types, offers)

    seen: set[str] = set()
    candidates: list[Candidate] = []

    def push(count_vectors: dict[str, dict[int, int]]) -> None:
        free_counts: dict[int, dict[str, int]] = {}
        for code, vector in count_vectors.items():
            for row_id, amount in vector.items():
                if amount:
                    free_counts.setdefault(row_id, {})[code] = amount
        merged: dict[int, LoadedOffer] = {}
        for row_id in set(fixed_loads) | set(free_counts):
            fixed_counts = _group_counts(fixed_loads[row_id].units) if row_id in fixed_loads else {}
            counts = dict(fixed_counts)
            for code, amount in free_counts.get(row_id, {}).items():
                counts[code] = counts.get(code, 0) + amount
            loaded = load_offer(offers_by_id[row_id], requirements, counts)
            if not loaded.fits:
                return
            merged[row_id] = loaded
        if len(merged) > MAX_OFFERS_PER_COMBINATION:
            return
        signature_parts = tuple(
            (row_id, _group_counts(loaded.units))
            for row_id, loaded in sorted(merged.items())
        )
        signature = repr(signature_parts)
        if signature in seen:
            return
        seen.add(signature)
        candidate = _build_candidate(merged, requirements, currency, deadline)
        if candidate is not None:
            candidates.append(candidate)

    _enumerate_vectors(remaining_types, compatible, residual_capacity, 0, {}, push)

    candidates.sort(key=lambda item: (not item.satisfies_deadline, item.total_cost["value"]))
    ranked: list[Candidate] = []
    for index, candidate in enumerate(candidates[:MAX_CANDIDATES], start=1):
        ranked.append(Candidate(
            signature=candidate.signature,
            allocations=candidate.allocations,
            total_mass=candidate.total_mass,
            total_cost=candidate.total_cost,
            effective_per_kg=candidate.effective_per_kg,
            earliest_launch=candidate.earliest_launch,
            latest_launch=candidate.latest_launch,
            satisfies_deadline=candidate.satisfies_deadline,
            currency=candidate.currency,
            rank=index,
        ))
    return ranked


def _assert_search_bounded(remaining_types: list[tuple[Requirement, int]], offers: list[Offer]) -> None:
    """组合枚举前估算搜索空间，避免件数或报价数过大时失控。"""
    fan_out = 1
    for requirement, demand in remaining_types:
        rows = [offer for offer in offers if fits_dimensions(requirement, offer)]
        bounds = [min(demand, int(offer.capacity_kg // requirement.unit.mass_kg)) for offer in rows]
        fan_out *= _bounded_compositions(demand, bounds)
        if fan_out > MAX_SEARCH_SPACE:
            raise EngineError(
                f"候选组合搜索空间超过上限（约 {fan_out} > {MAX_SEARCH_SPACE}），请缩小报价集或减少件数后重试"
            )


def _bounded_compositions(demand: int, bounds: list[int]) -> int:
    """满足 c_i <= bounds[i] 且总和为 demand 的非负整数向量个数。"""
    ways = [1] + [0] * demand
    for bound in bounds:
        updated = [0] * (demand + 1)
        for total in range(demand + 1):
            updated[total] = sum(ways[total - amount] for amount in range(min(bound, total) + 1))
        ways = updated
    return ways[demand]


def _enumerate_vectors(
    remaining_types: list[tuple[Requirement, int]],
    compatible: dict[str, list[int]],
    residual_capacity: dict[int, float],
    type_index: int,
    vectors: dict[str, dict[int, int]],
    emit,
) -> None:
    """按载荷类型枚举 ``各报价承运件数`` 向量，质量约束即时剪枝。"""
    if type_index == len(remaining_types):
        emit(vectors)
        return
    requirement, demand = remaining_types[type_index]
    code = requirement.payload_code
    rows = compatible[code]
    unit_mass = requirement.unit.mass_kg

    def allocated_to(row_id: int) -> float:
        return sum(
            other_unit_mass(remaining_types, other_code) * vector.get(row_id, 0)
            for other_code, vector in vectors.items()
        )

    def distribute(row_index: int, left: int) -> None:
        if row_index == len(rows) - 1:
            row_id = rows[row_index]
            headroom = residual_capacity[row_id] - allocated_to(row_id)
            if left * unit_mass <= headroom + 1e-9:
                if left:
                    vectors.setdefault(code, {})[row_id] = left
                _enumerate_vectors(remaining_types, compatible, residual_capacity, type_index + 1, vectors, emit)
                if left:
                    vectors[code].pop(row_id, None)
            return
        row_id = rows[row_index]
        headroom = residual_capacity[row_id] - allocated_to(row_id)
        maximum = min(left, int(headroom // unit_mass)) if headroom >= 0 else 0
        for amount in range(maximum + 1):
            if amount:
                vectors.setdefault(code, {})[row_id] = amount
            distribute(row_index + 1, left - amount)
            if amount:
                vectors[code].pop(row_id, None)

    distribute(0, demand)


def other_unit_mass(remaining_types: list[tuple[Requirement, int]], code: str) -> float:
    for requirement, _ in remaining_types:
        if requirement.payload_code == code:
            return requirement.unit.mass_kg
    raise EngineError(f"未知载荷编码：{code}")


# ---------------------------------------------------------------------------
# 成本分解
# ---------------------------------------------------------------------------

def _build_candidate(load_map: dict[int, LoadedOffer], requirements: list[Requirement], currency: str, deadline: str | None) -> Candidate | None:
    allocations: list[Allocation] = []
    total_mass = 0.0
    total_cost = 0.0
    launches: list[str] = []
    for row_id in sorted(load_map):
        loaded = load_map[row_id]
        offer = loaded.offer
        counts = _group_counts(loaded.units)
        unit_rows = [
            {"payload_code": code, "quantity": str(amount),
             "need": f"{requirement_by(requirements, code).copies}+{requirement_by(requirements, code).redundancy_spares}"}
            for code, amount in sorted(counts.items())
        ]
        mass_src = (f"quote-row:{offer.quote_row_id}#capacity",) + tuple(
            f"requirement:{code}#unit_mass" for code in sorted(counts)
        )
        base_src = (f"quote-row:{offer.quote_row_id}#base_price",)
        perkg_src = (f"quote-row:{offer.quote_row_id}#per_kg_price", f"quote-row:{offer.quote_row_id}#capacity")
        allocated = Quantity(loaded.allocated_mass_kg, "kg", mass_src)
        capacity = Quantity(offer.capacity_kg, "kg", (f"quote-row:{offer.quote_row_id}#capacity",))
        utilization = Quantity(
            loaded.allocated_mass_kg / offer.capacity_kg if offer.capacity_kg else 0.0,
            "ratio",
            (f"quote-row:{offer.quote_row_id}#capacity", *mass_src[1:]),
        )
        base_fee = Quantity(offer.base_price, currency, base_src)
        mass_fee = Quantity(offer.per_kg_price * loaded.allocated_mass_kg, currency, (*perkg_src, *mass_src[1:]))
        subtotal = Quantity(base_fee.value + mass_fee.value, currency, base_src + perkg_src + mass_src[1:])
        allocations.append(Allocation(
            quote_row_id=offer.quote_row_id,
            offer_label=offer.label,
            provider=offer.provider,
            vehicle=offer.vehicle,
            launch_site=offer.launch_site,
            launch_date=offer.latest_date,
            currency=currency,
            units=unit_rows,
            unit_count=len(loaded.units),
            allocated_mass=allocated.as_dict(),
            capacity=capacity.as_dict(),
            utilization={**utilization.as_dict(ndigits=4), "unit": "ratio"},
            base_fee=base_fee.as_dict(),
            mass_fee=mass_fee.as_dict(),
            subtotal=subtotal.as_dict(),
        ))
        total_mass += allocated.value
        total_cost += subtotal.value
        launches.append(offer.latest_date)

    total_mass_q = Quantity(total_mass, "kg", tuple(
        source for allocation in allocations for source in allocation.allocated_mass["sources"]
    ))
    total_cost_q = Quantity(total_cost, currency, tuple(
        source for allocation in allocations for source in allocation.subtotal["sources"]
    ))
    effective = Quantity(total_cost / total_mass if total_mass else 0.0, f"{currency}/kg",
                         total_cost_q.sources + total_mass_q.sources)
    earliest = min(item.launch_date for item in allocations) if allocations else ""
    latest = max(launches) if launches else ""
    satisfies = True if not deadline else latest <= deadline
    signature = "|".join(
        str(allocation.quote_row_id) + ":" + ",".join(f"{unit['payload_code']}x{unit['quantity']}" for unit in allocation.units)
        for allocation in allocations
    )
    return Candidate(
        signature=signature,
        allocations=allocations,
        total_mass=total_mass_q.as_dict(),
        total_cost=total_cost_q.as_dict(),
        effective_per_kg=effective.as_dict(ndigits=4),
        earliest_launch=earliest,
        latest_launch=latest,
        satisfies_deadline=satisfies,
        currency=currency,
    )


def requirement_by(requirements: list[Requirement], code: str) -> Requirement:
    for requirement in requirements:
        if requirement.payload_code == code:
            return requirement
    raise EngineError(f"未知载荷编码：{code}")


def candidate_to_dict(candidate: Candidate, *, fixed_slots: dict[int, list[str]] | None = None) -> dict[str, Any]:
    fixed_slots = fixed_slots or {}
    allocations_out: list[dict[str, Any]] = []
    for allocation in candidate.allocations:
        item = asdict(allocation)
        locked_units = fixed_slots.get(allocation.quote_row_id, [])
        item["locked"] = bool(locked_units)
        item["locked_unit_count"] = len(locked_units)
        item["locked_units"] = locked_units
        allocations_out.append(item)
    return {
        "rank": candidate.rank,
        "signature": candidate.signature,
        "satisfies_deadline": candidate.satisfies_deadline,
        "earliest_launch": candidate.earliest_launch,
        "latest_launch": candidate.latest_launch,
        "currency": candidate.currency,
        "total_mass": candidate.total_mass,
        "total_cost": candidate.total_cost,
        "effective_per_kg": candidate.effective_per_kg,
        "allocations": allocations_out,
    }
