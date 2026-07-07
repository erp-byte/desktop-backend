"""QC Inward Inspection — FastAPI router.

Mounted at /api/v1/qc.  All endpoints require a valid JWT bearer token and
the caller's role to hold the relevant qc.inspection.{read|create|edit}
permission.

Route declaration order matters — FastAPI matches in registration order:
  1. Static sub-paths with no path params come first.
  2. /inspection/start (static) is declared before /inspection/{inspection_id}
     (parametrised) so "start" is never captured as an id.
  3. Sub-resources of /{inspection_id} are declared before a generic
     /{inspection_id} GET so the sub-path segments are never swallowed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request

from app.core.middleware.request_context import AuthError
from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.qc.schemas import (
    ArrivalListResponse,
    ArrivalSummaryResponse,
    AuditEvent,
    CancelRequest,
    HeaderUpdateRequest,
    InspectionDetail,
    InspectionListResponse,
    IntimationListResponse,
    OkResponse,
    OverrideRequest,
    OverrideResponse,
    ReadingDeleteRequest,
    Reading,
    ReadingsBatchRequest,
    ReadingsBatchResponse,
    ReadingUpdateRequest,
    ReopenRequest,
    ReportResponse,
    StartRequest,
    StartResponse,
    VerdictRequest,
    VerdictResponse,
)
from app.modules.qc.ncr_schemas import (
    NcrCreateRequest,
    NcrCreateResponse,
    NcrDetail,
    NcrListResponse,
    NcrUpdateRequest,
    ParameterListResponse,
)
from app.modules.qc.services import arrivals_service
from app.modules.qc.services import inspection_service
from app.modules.qc.services import ncr_service
from app.modules.qc.services import parameters_service
from app.modules.qc.services.audit import list_audit
from app.modules.qc.services import readings_service
from app.modules.qc.services import reports_service
from app.modules.qc.services import verdict_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qc", tags=["QC"])


# ── helpers ──────────────────────────────────────────────────────────────────


def _actor(user: AuthUser) -> dict:
    """Return a dict of actor kwargs to spread into service calls."""
    return {
        "actor_user_id": user.user_id,
        "actor_name": getattr(user, "full_name", None),
        "actor_role": user.role_name,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. GET /api/v1/qc/inspection  — paginated list
# Declared first so it never collides with sub-paths.
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/inspection", response_model=InspectionListResponse)
async def list_inspections(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    transaction_no: str | None = Query(None),
    supplier_id: int | None = Query(None),
    sku_id: int | None = Query(None),
    verdict: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    user: AuthUser = Depends(require_permission("qc", "inspection", action="read")),
):
    filters = {
        k: v
        for k, v in {
            "status": status,
            "transaction_no": transaction_no,
            "supplier_id": supplier_id,
            "sku_id": sku_id,
            "verdict": verdict,
            "from_date": from_date,
            "to_date": to_date,
        }.items()
        if v is not None
    }
    pool = request.app.state.db_pool
    return await inspection_service.list_inspections(
        pool, filters=filters, page=page, page_size=page_size
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. GET /api/v1/qc/intimations  — pending intimations picker
# MUST come before /{inspection_id} routes or "intimations" gets captured.
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/intimations", response_model=IntimationListResponse)
async def list_intimations(
    request: Request,
    status: str = Query("pending"),   # accepted for API compat; service always returns pending
    limit: int = Query(50, ge=1, le=500),
    q: str | None = Query(None),
    user: AuthUser = Depends(require_permission("qc", "inspection", action="read")),
):
    pool = request.app.state.db_pool
    items = await inspection_service.list_pending_intimations(pool, q=q, limit=limit)
    return {"items": items}


# ═══════════════════════════════════════════════════════════════════════════
# 2b. GET /api/v1/qc/parameters  — RM-check parameter catalog
# Reference data for the readings picker + parameters page. Reuses the
# inspection read permission.
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/parameters", response_model=ParameterListResponse)
async def list_parameters(
    request: Request,
    active: bool = Query(True),
    user: AuthUser = Depends(require_permission("qc", "inspection", action="read")),
):
    pool = request.app.state.db_pool
    items = await parameters_service.list_parameters(pool, active_only=active)
    return {"items": items}


# ═══════════════════════════════════════════════════════════════════════════
# 2c. Material arrivals — Material In QC status
#   GET /api/v1/qc/arrivals/summary?transaction_no=A&transaction_no=B
#   GET /api/v1/qc/arrivals?transaction_no=A
# Distinct static paths; reuse the inspection read permission.
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/arrivals/summary", response_model=ArrivalSummaryResponse)
async def arrivals_summary(
    request: Request,
    transaction_no: list[str] = Query(default=[]),
    user: AuthUser = Depends(require_permission("qc", "inspection", action="read")),
):
    pool = request.app.state.db_pool
    items = await arrivals_service.summary(pool, transaction_no)
    return {"items": items}


@router.get("/arrivals", response_model=ArrivalListResponse)
async def list_arrivals(
    request: Request,
    transaction_no: str = Query(...),
    user: AuthUser = Depends(require_permission("qc", "inspection", action="read")),
):
    pool = request.app.state.db_pool
    items = await arrivals_service.list_arrivals(pool, transaction_no)
    return {"items": items}


# ═══════════════════════════════════════════════════════════════════════════
# 3. POST /api/v1/qc/inspection/start  — create a new inspection
# Static path; declared before /inspection/{inspection_id} to avoid capture.
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/inspection/start", response_model=StartResponse)
async def start_inspection(
    request: Request,
    body: StartRequest,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="create")),
):
    pool = request.app.state.db_pool
    inspection_id = await inspection_service.start(
        pool,
        qc_intimation_id=body.qc_intimation_id,
        sample_size=body.sample_size,
        inspection_method=body.inspection_method,
        remarks=body.remarks,
        actor_user_id=user.user_id,
        actor_name=getattr(user, "full_name", None),
    )
    return {"inspection_id": inspection_id}


# ═══════════════════════════════════════════════════════════════════════════
# Sub-resources of /{inspection_id}
# Declared before the plain GET /{inspection_id} so their segments are not
# swallowed by the parameterised route.
# ═══════════════════════════════════════════════════════════════════════════


# 5. GET /api/v1/qc/inspection/{inspection_id}/audit
@router.get("/inspection/{inspection_id}/audit", response_model=list[AuditEvent])
async def get_audit(
    request: Request,
    inspection_id: int,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="read")),
):
    pool = request.app.state.db_pool
    return await list_audit(pool, inspection_id)


# 6. POST /api/v1/qc/inspection/{inspection_id}/readings
@router.post("/inspection/{inspection_id}/readings", response_model=ReadingsBatchResponse)
async def add_readings(
    request: Request,
    inspection_id: int,
    body: ReadingsBatchRequest,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    return await readings_service.add_batch(
        pool,
        inspection_id=inspection_id,
        readings=[r.model_dump() for r in body.readings],
        actor_user_id=user.user_id,
        actor_role=user.role_name,
        is_admin=getattr(user, "is_admin", False),
    )


# 7. PUT /api/v1/qc/inspection/{inspection_id}/readings/{reading_id}
@router.put("/inspection/{inspection_id}/readings/{reading_id}", response_model=Reading)
async def update_reading(
    request: Request,
    inspection_id: int,
    reading_id: int,
    body: ReadingUpdateRequest,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    return await readings_service.update_reading(
        pool,
        inspection_id=inspection_id,
        reading_id=reading_id,
        fields={k: v for k, v in body.model_dump().items() if v is not None},
        actor_user_id=user.user_id,
        actor_role=user.role_name,
        is_admin=getattr(user, "is_admin", False),
    )


# 8. DELETE /api/v1/qc/inspection/{inspection_id}/readings/{reading_id}
@router.delete("/inspection/{inspection_id}/readings/{reading_id}", response_model=OkResponse)
async def delete_reading(
    request: Request,
    inspection_id: int,
    reading_id: int,
    body: ReadingDeleteRequest = ReadingDeleteRequest(),
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    await readings_service.delete_reading(
        pool,
        inspection_id=inspection_id,
        reading_id=reading_id,
        reason=body.reason,
        actor_user_id=user.user_id,
        actor_role=user.role_name,
        is_admin=getattr(user, "is_admin", False),
    )
    return {"ok": True}


# 9. POST /api/v1/qc/inspection/{inspection_id}/verdict
@router.post("/inspection/{inspection_id}/verdict", response_model=VerdictResponse)
async def set_verdict(
    request: Request,
    inspection_id: int,
    body: VerdictRequest,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    return await verdict_service.set_verdict(
        pool,
        inspection_id=inspection_id,
        verdict=body.verdict,
        accepted_qty=body.accepted_qty,
        rejected_qty=body.rejected_qty,
        summary_remarks=body.summary_remarks,
        actor_user_id=user.user_id,
        actor_role="admin" if getattr(user, "is_admin", False) else user.role_name,
        is_admin=getattr(user, "is_admin", False),
    )


# 10. POST /api/v1/qc/inspection/{inspection_id}/verdict/override
# Declare BEFORE /{inspection_id}/verdict (same prefix) — FastAPI matches in
# order and "override" segment would shadow the bare /verdict route for POST
# if they were reversed. Because FastAPI distinguishes by full path, this is
# actually fine in both orders, but keeping override first is clearer.
@router.post("/inspection/{inspection_id}/verdict/override", response_model=OverrideResponse)
async def override_verdict(
    request: Request,
    inspection_id: int,
    body: OverrideRequest,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    # Admins pass as "admin" so the service's role allow-list accepts them.
    effective_role = "admin" if getattr(user, "is_admin", False) else user.role_name
    return await verdict_service.override(
        pool,
        inspection_id=inspection_id,
        new_verdict=body.new_verdict,
        reason=body.reason,
        actor_user_id=user.user_id,
        actor_role=effective_role,
    )


# 11. POST /api/v1/qc/inspection/{inspection_id}/cancel
@router.post("/inspection/{inspection_id}/cancel", response_model=OkResponse)
async def cancel_inspection(
    request: Request,
    inspection_id: int,
    body: CancelRequest,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    await inspection_service.cancel(
        pool,
        inspection_id=inspection_id,
        reason=body.reason,
        actor_user_id=user.user_id,
        actor_name=getattr(user, "full_name", None) or "",
    )
    return {"ok": True}


# 12. POST /api/v1/qc/inspection/{inspection_id}/reopen
@router.post("/inspection/{inspection_id}/reopen", response_model=OkResponse)
async def reopen_inspection(
    request: Request,
    inspection_id: int,
    body: ReopenRequest,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    await inspection_service.reopen(
        pool,
        inspection_id=inspection_id,
        reason=body.reason,
        actor_user_id=user.user_id,
        actor_name=getattr(user, "full_name", None) or "",
    )
    return {"ok": True}


# 14. POST /api/v1/qc/inspection/{inspection_id}/rm-report
@router.post("/inspection/{inspection_id}/rm-report", response_model=ReportResponse)
async def generate_rm_report(
    request: Request,
    inspection_id: int,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    return await reports_service.generate_rm_report(pool, inspection_id)


# 15. POST /api/v1/qc/inspection/{inspection_id}/ncr-report
@router.post("/inspection/{inspection_id}/ncr-report", response_model=ReportResponse)
async def generate_ncr_report(
    request: Request,
    inspection_id: int,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    return await reports_service.generate_ncr_report(pool, inspection_id)


# ═══════════════════════════════════════════════════════════════════════════
# 4. GET /api/v1/qc/inspection/{inspection_id}  — full detail
# Declared AFTER all static sub-paths of /inspection/... so it doesn't
# swallow "start" or any future static segments.
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/inspection/{inspection_id}", response_model=InspectionDetail)
async def get_inspection(
    request: Request,
    inspection_id: int,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="read")),
):
    pool = request.app.state.db_pool
    detail = await inspection_service.get_detail(pool, inspection_id)
    if detail is None:
        raise AuthError("not_found", "Inspection not found", 404)
    return detail


# 13. PUT /api/v1/qc/inspection/{inspection_id}  — partial header update
# Declared after GET of the same path; method differs so no collision.
@router.put("/inspection/{inspection_id}", response_model=OkResponse)
async def update_inspection(
    request: Request,
    inspection_id: int,
    body: HeaderUpdateRequest,
    user: AuthUser = Depends(require_permission("qc", "inspection", action="edit")),
):
    pool = request.app.state.db_pool
    await inspection_service.update_header(
        pool,
        inspection_id=inspection_id,
        sample_size=body.sample_size,
        inspection_method=body.inspection_method,
        inspector_user_id=body.inspector_user_id,
        remarks=body.remarks,
        actor_user_id=user.user_id,
    )
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
# NCR (Non-Conformance Report) — /api/v1/qc/ncr
# Static /ncr list+create declared before /ncr/{ncr_id} so the id route never
# swallows them. Guarded by the qc.ncr.* permission set.
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/ncr", response_model=NcrListResponse)
async def list_ncr(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    supplier_id: int | None = Query(None),
    transaction_no: str | None = Query(None),
    q: str | None = Query(None),
    user: AuthUser = Depends(require_permission("qc", "ncr", action="read")),
):
    filters = {
        k: v
        for k, v in {
            "status": status,
            "supplier_id": supplier_id,
            "transaction_no": transaction_no,
            "q": q,
        }.items()
        if v is not None
    }
    pool = request.app.state.db_pool
    return await ncr_service.list_ncr(pool, filters=filters, page=page, page_size=page_size)


@router.post("/ncr", response_model=NcrCreateResponse)
async def create_ncr(
    request: Request,
    body: NcrCreateRequest,
    user: AuthUser = Depends(require_permission("qc", "ncr", action="create")),
):
    pool = request.app.state.db_pool
    return await ncr_service.create(
        pool,
        body=body.model_dump(exclude_unset=True),
        actor_user_id=user.user_id,
    )


@router.get("/ncr/{ncr_id}", response_model=NcrDetail)
async def get_ncr(
    request: Request,
    ncr_id: int,
    user: AuthUser = Depends(require_permission("qc", "ncr", action="read")),
):
    pool = request.app.state.db_pool
    detail = await ncr_service.get_detail(pool, ncr_id)
    if detail is None:
        raise AuthError("ncr_not_found", "NCR not found", 404)
    return detail


@router.patch("/ncr/{ncr_id}", response_model=NcrDetail)
async def update_ncr(
    request: Request,
    ncr_id: int,
    body: NcrUpdateRequest,
    user: AuthUser = Depends(require_permission("qc", "ncr", action="edit")),
):
    pool = request.app.state.db_pool
    return await ncr_service.update(
        pool,
        ncr_id=ncr_id,
        fields=body.model_dump(exclude_unset=True),
    )


@router.delete("/ncr/{ncr_id}", response_model=OkResponse)
async def delete_ncr(
    request: Request,
    ncr_id: int,
    user: AuthUser = Depends(require_permission("qc", "ncr", action="delete")),
):
    pool = request.app.state.db_pool
    await ncr_service.delete(pool, ncr_id)
    return {"ok": True}
