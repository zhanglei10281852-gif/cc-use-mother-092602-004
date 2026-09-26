"""拼车发射比较接口的请求模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QuoteImport(BaseModel):
    batch_no: str = Field(min_length=1, max_length=120)
    supplier: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=1000)
    rows: list[Any] = Field(min_length=1, max_length=2000)


class RequirementUpsert(BaseModel):
    payload_code: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=200)
    unit_mass_value: float
    unit_mass_unit: str = Field(min_length=1, max_length=20)
    length_value: float
    length_unit: str = Field(min_length=1, max_length=20)
    width_value: float
    width_unit: str = Field(min_length=1, max_length=20)
    height_value: float
    height_unit: str = Field(min_length=1, max_length=20)
    copies: int = Field(ge=1, le=1_000_000)
    redundancy_spares: int = Field(ge=0, le=1_000_000)


class ComparisonCreate(BaseModel):
    comparison_no: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=200)
    deadline: str = Field(default="", max_length=10)
    quote_batch_nos: list[str] = Field(min_length=1, max_length=200)
    payload_codes: list[str] = Field(min_length=1, max_length=200)


class RecomputeRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    quote_batch_nos: list[str] | None = Field(default=None, max_length=200)


class LockRequest(BaseModel):
    quote_row_id: int = Field(ge=1)
    units: list[str] = Field(min_length=1, max_length=1000)
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class UnlockRequest(BaseModel):
    quote_row_id: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class ReportExport(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=200)
