from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class QuoteBatchImport(BaseModel):
    """报价批次导入请求。rows 保持松散结构，逐行业务校验在服务层完成并汇总错误。"""

    batch_no: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    source: str = Field(min_length=1, max_length=200)
    imported_by: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class RequirementCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=200)
    sat_length_m: float = Field(gt=0, le=60)
    sat_width_m: float = Field(gt=0, le=60)
    sat_height_m: float = Field(gt=0, le=60)
    sat_mass_kg: float = Field(gt=0, le=100000)
    quantity: int = Field(ge=1, le=10000)
    redundant_quantity: int = Field(default=0, ge=0, le=10000)
    latest_delivery_date: date
    created_by: str = Field(min_length=1, max_length=120)


class RequirementUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    sat_length_m: float | None = Field(default=None, gt=0, le=60)
    sat_width_m: float | None = Field(default=None, gt=0, le=60)
    sat_height_m: float | None = Field(default=None, gt=0, le=60)
    sat_mass_kg: float | None = Field(default=None, gt=0, le=100000)
    quantity: int | None = Field(default=None, ge=1, le=10000)
    redundant_quantity: int | None = Field(default=None, ge=0, le=10000)
    latest_delivery_date: date | None = None
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class CalculateRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=1000)


class LockRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class UnlockRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class ReportExport(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
