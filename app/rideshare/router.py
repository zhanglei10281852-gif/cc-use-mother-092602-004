"""拼车发射方案比较 HTTP 接口。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.rideshare.schemas import (
    ComparisonCreate,
    LockRequest,
    QuoteImport,
    RecomputeRequest,
    ReportExport,
    RequirementUpsert,
    UnlockRequest,
)
from app.rideshare.service import RideCompareService

router = APIRouter(prefix="/api/rideshare", tags=["拼车发射方案比较"])


def service() -> RideCompareService:
    return RideCompareService()


# -- 报价导入与查询 -------------------------------------------------------

@router.post("/quote-batches", status_code=201)
def import_quotes(payload: QuoteImport, actor: str = Query(..., min_length=1)):
    return service().import_quotes(payload.model_dump(), actor)


@router.get("/quote-batches")
def list_batches(limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_batches(limit)}


@router.get("/quote-batches/{batch_no}")
def get_batch(batch_no: str):
    return service().get_batch(batch_no)


@router.get("/quote-rows/{quote_row_id}")
def get_quote_row(quote_row_id: int):
    return service().get_quote_row(quote_row_id)


# -- 载荷需求 -------------------------------------------------------------

@router.put("/payload-requirements")
def upsert_requirement(payload: RequirementUpsert, actor: str = Query(..., min_length=1)):
    return service().upsert_requirement(payload.model_dump(), actor)


@router.get("/payload-requirements")
def list_requirements():
    return {"items": service().list_requirements()}


# -- 比较与版本 -----------------------------------------------------------

@router.post("/comparisons", status_code=201)
def create_comparison(payload: ComparisonCreate, actor: str = Query(..., min_length=1)):
    return service().create_comparison(payload.model_dump(), actor)


@router.get("/comparisons")
def list_comparisons(limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_comparisons(limit)}


@router.get("/comparisons/{comparison_no}")
def get_comparison(comparison_no: str):
    return service().get_comparison(comparison_no)


@router.post("/comparisons/{comparison_no}/recompute", status_code=201)
def recompute(comparison_no: str, payload: RecomputeRequest):
    return service().recompute(comparison_no, actor=payload.actor, reason=payload.reason,
                               quote_batch_nos=payload.quote_batch_nos)


@router.get("/comparisons/{comparison_no}/versions/{version}")
def get_version(comparison_no: str, version: int):
    return service().get_version(comparison_no, version)


# -- 人工锁定 -------------------------------------------------------------

@router.post("/comparisons/{comparison_no}/locks", status_code=201)
def lock(comparison_no: str, payload: LockRequest):
    return service().lock(comparison_no, quote_row_id=payload.quote_row_id, units=payload.units,
                          actor=payload.actor, reason=payload.reason)


@router.post("/comparisons/{comparison_no}/unlocks", status_code=201)
def unlock(comparison_no: str, payload: UnlockRequest):
    return service().unlock(comparison_no, quote_row_id=payload.quote_row_id,
                            actor=payload.actor, reason=payload.reason)


# -- 决策报告 -------------------------------------------------------------

@router.post("/comparisons/{comparison_no}/reports", status_code=201)
def export_report(comparison_no: str, payload: ReportExport):
    return service().export_report(comparison_no, actor=payload.actor, title=payload.title)


@router.get("/comparisons/{comparison_no}/reports")
def list_reports(comparison_no: str):
    return {"items": service().list_reports(comparison_no)}


@router.get("/comparisons/{comparison_no}/reports/{report_version}")
def get_report(comparison_no: str, report_version: int):
    return service().get_report(comparison_no, report_version)


# -- 审计 -----------------------------------------------------------------

@router.get("/comparisons/{comparison_no}/audit")
def list_audit(comparison_no: str, limit: int = Query(default=200, ge=1, le=1000)):
    return {"items": service().list_audit(comparison_no, limit)}
