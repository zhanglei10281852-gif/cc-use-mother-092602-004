from __future__ import annotations

from fastapi import APIRouter, Response

from app.procurement.schemas import (
    CalculateRequest,
    LockRequest,
    QuoteBatchImport,
    ReportExport,
    RequirementCreate,
    RequirementUpdate,
    UnlockRequest,
)
from app.procurement.service import ProcurementService

router = APIRouter(prefix="/api/procurement", tags=["拼车发射采购比较"])


def service() -> ProcurementService:
    return ProcurementService()


@router.post("/quote-batches", status_code=201)
def import_quote_batch(payload: QuoteBatchImport, response: Response):
    """导入报价批次。同一批次号重复导入且内容一致时幂等重放（200），内容不一致时拒绝（409）。"""
    result = service().import_batch(payload.model_dump())
    if result["replayed"]:
        response.status_code = 200
    return result


@router.get("/quote-batches")
def list_quote_batches():
    return {"items": service().list_batches()}


@router.get("/quote-batches/{batch_no}")
def get_quote_batch(batch_no: str):
    return service().get_batch(batch_no)


@router.get("/quotes/current")
def current_quotes():
    """当前报价簿：同一供应商、型号、发射日期以最新批次为准。"""
    return {"items": service().current_quotes()}


@router.post("/requirements", status_code=201)
def create_requirement(payload: RequirementCreate):
    return service().create_requirement(payload.model_dump(mode="json"))


@router.get("/requirements")
def list_requirements():
    return {"items": service().list_requirements()}


@router.get("/requirements/{requirement_id}")
def get_requirement(requirement_id: int):
    return service().get_requirement(requirement_id)


@router.put("/requirements/{requirement_id}")
def update_requirement(requirement_id: int, payload: RequirementUpdate):
    return service().update_requirement(requirement_id, payload.model_dump(mode="json"))


@router.post("/requirements/{requirement_id}/calculations", status_code=201)
def calculate(requirement_id: int, payload: CalculateRequest):
    """生成新的计算版本：锁定组合原样结转，未锁定部分按当前报价簿重新计算。"""
    return service().calculate(requirement_id, payload.actor, payload.reason)


@router.get("/requirements/{requirement_id}/calculations")
def list_calculations(requirement_id: int):
    return {"items": service().list_runs(requirement_id)}


@router.get("/calculations/{run_id}")
def get_calculation(run_id: int):
    return service().get_run(run_id)


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: int):
    """候选组合详情：每个数字均可追溯到报价行、批次号与计算版本。"""
    return service().get_candidate(candidate_id)


@router.post("/candidates/{candidate_id}/lock")
def lock_candidate(candidate_id: int, payload: LockRequest):
    return service().lock_candidate(candidate_id, payload.actor, payload.reason)


@router.post("/candidates/{candidate_id}/unlock")
def unlock_candidate(candidate_id: int, payload: UnlockRequest):
    return service().unlock_candidate(candidate_id, payload.actor, payload.reason)


@router.post("/requirements/{requirement_id}/reports", status_code=201)
def export_report(requirement_id: int, payload: ReportExport):
    return service().export_report(requirement_id, payload.actor)


@router.get("/requirements/{requirement_id}/reports")
def list_reports(requirement_id: int):
    return {"items": service().list_reports(requirement_id)}


@router.get("/reports/{report_id}")
def get_report(report_id: int):
    return service().get_report(report_id)


@router.get("/events")
def list_events(entity_type: str | None = None, entity_id: str | None = None):
    return {"items": service().list_events(entity_type, entity_id)}
