"""Production Planning module router — fulfillment sync, AI plan generation, plan CRUD."""

import json
import logging
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator

from app.webhooks.event_bus import deferred_events
from app.modules.auth.middleware import AuthUser, get_current_user, require_permission
from app.modules.auth.services.permission_service import check_permission
from app.modules.production.schemas.job_card_edit import (
    JobCardPatchRequest, JobCardCancelRequest,
    EnvironmentPatchRequest, MetalDetectionPatchRequest,
    WeightCheckPatchRequest, LossReconciliationPatchRequest,
    RemarkPatchRequest, AnnexureDeleteRequest,
)
# L3: B13 cost-metric gate. Hoisted to the top so endpoint bodies stay
# clean and the gate is one obvious dependency at the module head.
from app.modules.production.services.response_filters import strip_cost_fields
from app.core.warehouse_scope import user_has_warehouse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/production", tags=["Production"])


# ---------------------------------------------------------------------------
# Router helpers
# ---------------------------------------------------------------------------

def _raise_if_locked(result: dict | None) -> None:
    """R6 lock-guard translation: if a v2 JC service returns the locked
    error dict (set by services/job_card_v2.py::assert_not_locked), raise
    HTTP 409 with the full lock context so the client can show the lock
    reason and offer the force-unlock action.

    Called immediately after the service await on every endpoint whose
    service uses assert_not_locked() - prevents a generic catch-all
    `if result.get("error"):` from mis-mapping the 409 to a 400.
    """
    if result and result.get("error") == "locked":
        raise HTTPException(status_code=409, detail=result)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class FulfillmentSyncRequest(BaseModel):
    entity: str | None = None


class ReviseRequest(BaseModel):
    new_qty: float | None = None
    new_date: date | None = None
    reason: str = ""
    revised_by: str = ""


class CarryforwardRequest(BaseModel):
    fulfillment_ids: list[int]
    new_fy: str
    revised_by: str = ""


class FulfillmentV2SyncRequest(BaseModel):
    entity: str | None = None


class FulfillmentBySoLinesRequest(BaseModel):
    so_line_ids: list[int] = []
    entity: str | None = None
    financial_year: str | None = None


class ReviseV2Request(BaseModel):
    new_qty: float | None = None
    new_units: float | None = None
    new_date: date | None = None
    reason: str = ""
    revised_by: str = ""


class CarryforwardV2Request(BaseModel):
    fulfillment_ids: list[int]
    new_fy: str
    revised_by: str = ""


class CancelV2Request(BaseModel):
    fulfillment_ids: list[int]
    reason: str
    cancelled_by: str = ""


class BomOverrideV2Request(BaseModel):
    overrides: list[dict] = []
    overridden_by: str = ""


class FloorStockV2Request(BaseModel):
    entries: list[dict] = []
    added_by: str = ""


# ---- Routing-Gap Resolution request models ----
class RoutingGapAssignment(BaseModel):
    article: str
    process_category: str  # may be blank -> skipped_no_pc


class RoutingGapApplyRequest(BaseModel):
    assignments: list[RoutingGapAssignment] = []
    performed_by: str | None = None


# ---- Plan v2 request models ----
class PlanV2Create(BaseModel):
    entity: str
    warehouse: str
    plan_type: str = "daily"
    plan_date: date
    date_from: date | None = None
    date_to: date | None = None
    lines: list[dict] = []


class PlanV2Update(BaseModel):
    plan_date: date | None = None
    date_from: date | None = None
    date_to: date | None = None
    plan_type: str | None = None


class PlanV2Approve(BaseModel):
    approved_by: str


class PlanV2Cancel(BaseModel):
    reason: str = ""


class PlanV2Delete(BaseModel):
    # Reason is required — the value is included in the notification email
    # so the admin distribution list can see why the plan was deleted.
    reason: str
    deleted_by: str = ""


class BomLineV2Create(BaseModel):
    material_sku_name: str = Field(..., min_length=1)
    item_type: Literal["rm", "pm"]
    quantity_per_unit: float = Field(..., gt=0)
    uom: str | None = None
    loss_pct: float | None = 0


class BomCreateV2Request(BaseModel):
    # Inline "Add BOM" from the plan-builder card: create/supersede the master
    # BOM for one FG SKU so plan creation (POST /plans-v2) resolves it.
    fg_sku_name: str = Field(..., min_length=1)
    entity: Literal["cfpl", "cdpl"]
    pack_size_kg: float | None = None
    customer_name: str | None = None
    lines: list[BomLineV2Create] = Field(..., min_length=1)


class PlanLineV2Patch(BaseModel):
    # All optional — caller sends ONLY the fields they want to update.
    # planned_qty_* are NUMERIC(12,3) NOT NULL CHECK (> 0) on the column,
    # so submitting zero or negative will surface a 400 from the service.
    planned_qty_kg: float | None = None
    planned_qty_units: float | None = None
    area: str | None = None
    deadline_date: date | None = None


class StepV2Reorder(BaseModel):
    step_ids: list[int]


class StepV2Patch(BaseModel):
    process_name: str | None = None
    stage: str | None = None
    floor: str | None = None
    std_time_min: float | None = None
    loss_pct: float | None = None
    notes: str | None = None


class StepV2Add(BaseModel):
    process_name: str
    stage: str | None = None
    floor: str | None = None
    std_time_min: float | None = None
    loss_pct: float | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health")
async def production_health(request: Request):
    pool = request.app.state.db_pool
    counts = {}
    for table in ['bom_header', 'bom_line', 'bom_process_route', 'machine',
                   'machine_capacity', 'so_fulfillment', 'production_plan']:
        counts[table] = await pool.fetchval(f"SELECT COUNT(*) FROM {table}")
    return {"status": "ok", "module": "production", "tables": counts}


# ---------------------------------------------------------------------------
# Fulfillment endpoints
# ---------------------------------------------------------------------------

@router.post("/fulfillment/sync")
async def sync_fulfillment(request: Request, body: FulfillmentSyncRequest):
    """Sync all FG SO lines into so_fulfillment. Idempotent."""
    from app.modules.production.services.fulfillment import sync_fulfillment as _sync
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await _sync(conn, body.entity)
    return result


@router.get("/fulfillment")
async def list_fulfillment(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
    so_number: str = Query(None),
    article: str = Query(None),
    search: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """Paginated list of fulfillment records with filters."""
    from app.modules.production.services.fulfillment import get_fulfillment_list
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_fulfillment_list(
            conn, entity=entity, status=status, financial_year=financial_year,
            customer=customer, so_number=so_number, article=article,
            search=search, page=page, page_size=page_size,
        )


@router.get("/fulfillment/all")
async def list_fulfillment_all(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
    so_number: str = Query(None),
    article: str = Query(None),
    search: str = Query(None),
):
    """All fulfillment records matching filters, no pagination."""
    from app.modules.production.services.fulfillment import get_fulfillment_list
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_fulfillment_list(
            conn, entity=entity, status=status, financial_year=financial_year,
            customer=customer, so_number=so_number, article=article,
            search=search, page=1, page_size=100000,
        )
    return result["results"]


@router.get("/fulfillment/demand-summary")
async def demand_summary(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
):
    """Aggregated pending demand grouped by product + customer."""
    from app.modules.production.services.fulfillment import get_demand_summary
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_demand_summary(conn, entity, financial_year)


@router.get("/fulfillment/chart-summary")
async def chart_summary(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
    so_number: str = Query(None),
    article: str = Query(None),
    status: str = Query(None),
    user=Depends(get_current_user),
):
    """Aggregated data for dashboard charts — not paginated.

    B13 cost-metric gate: the SO surface this aggregates over can carry
    ``rate_inr`` / ``amount_inr``; gate the response for deny-listed roles."""
    from app.modules.production.services.fulfillment import get_chart_summary
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_chart_summary(
            conn, entity=entity, financial_year=financial_year,
            customer=customer, so_number=so_number, article=article, status=status,
        )
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/fulfillment/filter-options")
async def filter_options(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
    so_number: str = Query(None),
    article: str = Query(None),
):
    """Distinct values for Customer, SO Number, Article dropdowns.
    Supports smart cross-filtering: pass current sibling selections to
    narrow each list to matching options only (comma-separated multi-value).
    """
    from app.modules.production.services.fulfillment import get_filter_options
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_filter_options(
            conn, entity=entity, financial_year=financial_year,
            customer=customer, so_number=so_number, article=article,
        )


@router.get("/fulfillment/customer-view")
async def customer_view(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
    user=Depends(get_current_user),
):
    """Customer-grouped fulfillment with BOM details, process route + floors, and inventory status.

    B13 cost-metric gate: customer-view embeds SO lines that often carry
    ``rate_inr`` and amount columns; strip for deny-listed roles."""
    from app.modules.production.services.fulfillment import get_enriched_fulfillment
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_enriched_fulfillment(
            conn, entity=entity, financial_year=financial_year, customer=customer,
        )
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/fulfillment/fy-review")
async def fy_review(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
    user=Depends(get_current_user),
):
    """All unfulfilled orders for FY close review.

    B13 cost-metric gate: FY-review surfaces SO money columns; strip for
    deny-listed roles."""
    from app.modules.production.services.fulfillment import get_fy_review
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_fy_review(conn, entity, financial_year)
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/fulfillment/{fulfillment_id}/detail")
async def get_fulfillment_detail_endpoint(request: Request, fulfillment_id: int):
    """Get full fulfillment detail: BOM lines with inventory status, floor machines, linked SO, revision log."""
    from app.modules.production.services.fulfillment import get_fulfillment_detail
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_fulfillment_detail(conn, fulfillment_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Fulfillment not found")
    return result


@router.get("/fulfillment/{fulfillment_id}/bom-override")
async def get_bom_override(request: Request, fulfillment_id: int):
    """Get current BOM overrides for a fulfillment with master values for comparison."""
    from app.modules.production.services.fulfillment import get_bom_overrides
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_bom_overrides(conn, fulfillment_id)
    if "error" in result and result["error"] == "not_found":
        raise HTTPException(status_code=404, detail="Fulfillment not found")
    return result


class BomOverrideItem(BaseModel):
    bom_line_id: int | None = None
    material_sku_name: str | None = None
    quantity_per_unit: float | None = None
    loss_pct: float | None = None
    uom: str | None = None
    godown: str | None = None
    is_removed: bool = False
    override_reason: str = ""


class BomOverrideRequest(BaseModel):
    overrides: list[BomOverrideItem]
    overridden_by: str = ""


@router.put("/fulfillment/{fulfillment_id}/bom-override")
async def save_bom_override(request: Request, fulfillment_id: int, body: BomOverrideRequest):
    """Save per-fulfillment BOM overrides. Does NOT change the master BOM."""
    from app.modules.production.services.fulfillment import save_bom_overrides
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await save_bom_overrides(
                conn, fulfillment_id,
                [ov.model_dump() for ov in body.overrides],
                body.overridden_by,
            )
    if "error" in result:
        if result["error"] == "not_found":
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


class FloorStockItem(BaseModel):
    material_sku_name: str
    item_type: str = "pm"
    quantity_kg: float
    unit: str = "KG"
    floor_location: str
    notes: str = ""


class FloorStockRequest(BaseModel):
    entries: list[FloorStockItem]
    added_by: str = ""


@router.get("/fulfillment/{fulfillment_id}/floor-stock")
async def get_floor_stock(request: Request, fulfillment_id: int):
    """Get floor stock entries for a fulfillment."""
    from app.modules.production.services.fulfillment import get_floor_stock
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_floor_stock(conn, fulfillment_id)


@router.put("/fulfillment/{fulfillment_id}/floor-stock")
async def save_floor_stock(request: Request, fulfillment_id: int, body: FloorStockRequest):
    """Save floor stock entries for a fulfillment."""
    from app.modules.production.services.fulfillment import save_floor_stock
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await save_floor_stock(
                conn, fulfillment_id,
                [e.model_dump() for e in body.entries],
                body.added_by,
            )
    if "error" in result:
        if result["error"] == "not_found":
            raise HTTPException(status_code=404, detail="Fulfillment not found")
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.get("/floors")
async def list_floors(request: Request, entity: str = Query(None)):
    """Distinct floor locations from machines + inventory."""
    from app.modules.production.services.fulfillment import get_floor_locations
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_floor_locations(conn, entity)


@router.post("/fulfillment/carryforward")
async def carryforward(request: Request, body: CarryforwardRequest):
    """Bulk carry forward selected fulfillment records to a new FY."""
    from app.modules.production.services.fulfillment import carryforward_orders
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await carryforward_orders(conn, body.fulfillment_ids, body.new_fy, body.revised_by)


@router.put("/fulfillment/{fulfillment_id}/revise")
async def revise(request: Request, fulfillment_id: int, body: ReviseRequest):
    """Revise qty or deadline on a fulfillment record."""
    from app.modules.production.services.fulfillment import revise_order
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await revise_order(
                    conn, fulfillment_id,
                    new_qty=body.new_qty, new_date=body.new_date,
                    reason=body.reason, revised_by=body.revised_by,
                )
    if "error" in result:
        raise HTTPException(status_code=404, detail="Fulfillment record not found")
    return result


# ---------------------------------------------------------------------------
# Fulfillment v2 endpoints (manual planning workflow)
# ---------------------------------------------------------------------------

@router.get("/fulfillment-v2")
async def list_fulfillment_v2(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
    so_number: str = Query(None),
    article: str = Query(None),
    search: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """Paginated v2 fulfillment list — drop-in for v1 GET /fulfillment."""
    from app.modules.production.services.fulfillment_v2 import list_fulfillment
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await list_fulfillment(
            conn, entity=entity, status=status, financial_year=financial_year,
            customer=customer, so_number=so_number, article=article,
            search=search, page=page, page_size=page_size,
        )


@router.get("/fulfillment-v2/filter-options")
async def filter_options_v2(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
    so_number: str = Query(None),
    article: str = Query(None),
):
    """Distinct dropdown values for v2 fulfillment filters."""
    from app.modules.production.services.fulfillment_v2 import get_filter_options
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_filter_options(
            conn, entity=entity, financial_year=financial_year,
            customer=customer, so_number=so_number, article=article,
        )


@router.post("/fulfillment-v2/sync")
async def sync_fulfillment_v2(request: Request, body: FulfillmentV2SyncRequest):
    """Sync FG SO lines into so_fulfillment_v2. Idempotent."""
    from app.modules.production.services.fulfillment_v2 import sync_fulfillment as _sync
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await _sync(conn, body.entity)
    return result


@router.post("/fulfillment-v2/by-so-lines")
async def fulfillment_v2_by_so_lines(request: Request, body: FulfillmentBySoLinesRequest):
    """Resolve SO line ids to their fulfillment rows (list shape).

    Read-only. Backs the SO-Creation "Selected for Plan" panel: returns the
    so_fulfillment_v2 rows for the given so_line_ids plus the so_line_ids that
    have no fulfillment row yet (so the UI can prompt a Sync)."""
    from app.modules.production.services.fulfillment_v2 import get_fulfillment_by_so_lines
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_fulfillment_by_so_lines(
            conn, body.so_line_ids, entity=body.entity,
            financial_year=body.financial_year,
        )


@router.get("/fulfillment-v2/demand-summary")
async def demand_summary_v2(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
):
    from app.modules.production.services.fulfillment_v2 import get_demand_summary
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_demand_summary(conn, entity=entity, financial_year=financial_year)


@router.get("/fulfillment-v2/chart-summary")
async def chart_summary_v2(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
    so_number: str = Query(None),
    article: str = Query(None),
    status: str = Query(None),
    user=Depends(get_current_user),
):
    """B13 cost-metric gate applied — same rationale as v1 chart-summary."""
    from app.modules.production.services.fulfillment_v2 import get_chart_summary
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_chart_summary(
            conn, entity=entity, financial_year=financial_year,
            customer=customer, so_number=so_number, article=article, status=status,
        )
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/fulfillment-v2/customer-view")
async def customer_view_v2(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
    user=Depends(get_current_user),
):
    """B13 cost-metric gate applied — same rationale as v1 customer-view."""
    from app.modules.production.services.fulfillment_v2 import get_enriched_fulfillment
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_enriched_fulfillment(
            conn, entity=entity, financial_year=financial_year, customer=customer,
        )
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/fulfillment-v2/fy-review")
async def fy_review_v2(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
    user=Depends(get_current_user),
):
    """B13 cost-metric gate applied — same rationale as v1 fy-review."""
    from app.modules.production.services.fulfillment_v2 import get_fy_review
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_fy_review(conn, entity=entity, financial_year=financial_year)
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.post("/fulfillment-v2/cancel")
async def cancel_fulfillment_v2(request: Request, body: CancelV2Request):
    """Cancel selected v2 fulfillment rows with reason."""
    from app.modules.production.services.fulfillment_v2 import cancel_orders
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await cancel_orders(
                conn, body.fulfillment_ids,
                reason=body.reason, cancelled_by=body.cancelled_by,
            )


@router.post("/fulfillment-v2/carryforward")
async def carryforward_v2(request: Request, body: CarryforwardV2Request):
    """Bulk carry forward selected v2 fulfillment rows to a new FY."""
    from app.modules.production.services.fulfillment_v2 import carryforward_orders
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await carryforward_orders(
                conn, body.fulfillment_ids, body.new_fy, body.revised_by,
            )


@router.put("/fulfillment-v2/{so_fulfillment_id}/revise")
async def revise_v2(request: Request, so_fulfillment_id: int, body: ReviseV2Request):
    """Revise qty or deadline on a v2 fulfillment row with audit log."""
    from app.modules.production.services.fulfillment_v2 import revise_order
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await revise_order(
                    conn, so_fulfillment_id,
                    new_qty=body.new_qty, new_units=body.new_units,
                    new_date=body.new_date,
                    reason=body.reason, revised_by=body.revised_by,
                )
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Fulfillment record not found")
    if result.get("error") == "no_change":
        raise HTTPException(status_code=400, detail=result.get("message", "No change provided"))
    if result.get("error") in ("invalid_qty", "invalid_units"):
        raise HTTPException(status_code=400, detail=result.get("message", "Invalid value"))
    return result


@router.get("/fulfillment-v2/{so_fulfillment_id}/detail")
async def detail_v2(request: Request, so_fulfillment_id: int):
    """Full v2 fulfillment detail (drop-in shape for v1's modal)."""
    from app.modules.production.services.fulfillment_v2 import get_fulfillment_detail
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_fulfillment_detail(conn, so_fulfillment_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Fulfillment record not found")
    return result


@router.get("/fulfillment-v2/{so_fulfillment_id}/bom-override")
async def get_bom_override_v2(request: Request, so_fulfillment_id: int):
    from app.modules.production.services.fulfillment_v2 import get_bom_overrides
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_bom_overrides(conn, so_fulfillment_id)
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Fulfillment record not found")
    return result


@router.put("/fulfillment-v2/{so_fulfillment_id}/bom-override")
async def save_bom_override_v2(request: Request, so_fulfillment_id: int, body: BomOverrideV2Request):
    from app.modules.production.services.fulfillment_v2 import save_bom_overrides
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await save_bom_overrides(
                conn, so_fulfillment_id, body.overrides, body.overridden_by,
            )
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Fulfillment record not found")
    if result.get("error") in ("invalid_status", "no_bom"):
        raise HTTPException(status_code=400, detail=result.get("message", "Invalid"))
    return result


@router.get("/fulfillment-v2/{so_fulfillment_id}/floor-stock")
async def get_floor_stock_v2(request: Request, so_fulfillment_id: int):
    from app.modules.production.services.fulfillment_v2 import get_floor_stock
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_floor_stock(conn, so_fulfillment_id)
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Fulfillment record not found")
    return result


@router.put("/fulfillment-v2/{so_fulfillment_id}/floor-stock")
async def save_floor_stock_v2(request: Request, so_fulfillment_id: int, body: FloorStockV2Request):
    from app.modules.production.services.fulfillment_v2 import save_floor_stock
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await save_floor_stock(
                conn, so_fulfillment_id, body.entries, body.added_by,
            )
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Fulfillment record not found")
    return result


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Plan creation with AI
# ---------------------------------------------------------------------------

class CreatePlanItem(BaseModel):
    fulfillment_id: int
    custom_qty_kg: float
    so_number: str | None = None
    custom_qty_units: int | None = None
    uom: float | None = None
    bom_overrides: list = []
    floors: list[str] = []
    machines: dict[str, list[str]] = {}  # floor → [machine_names]
    floor_qty: dict[str, float] = {}     # floor → qty_kg
    floor_qty_units: dict[str, int] = {}  # floor → qty_units (whole units)


class AnsweredQuestion(BaseModel):
    id: str
    question: str
    answer: str


class CreatePlanWithAIRequest(BaseModel):
    entity: str
    plan_type: str = "daily"
    plan_date: date | None = None
    plan_name: str = ""
    created_by: str = ""
    selected_items: list[CreatePlanItem]
    # When the previous /create-with-ai call returned questions and the user has
    # answered them, the frontend re-submits the SAME selected_items plus this
    # array. Backend forwards it into Claude's context so the second call has
    # everything needed to commit a schedule. See "Token-efficient batching" in
    # ai_planner.DAILY_PLAN_PROMPT.
    answered_questions: list[AnsweredQuestion] = []


@router.post("/plans/create-with-ai")
async def create_plan_with_ai(request: Request, body: CreatePlanWithAIRequest):
    """Generate a production plan using Claude AI from selected fulfillment items.

    Two-call pattern:
      Call 1 — frontend submits selected_items. If Claude has questions it
        returns status='needs_clarification' with a `questions` array; no plan
        is written.
      Call 2 — frontend re-submits the same body PLUS `answered_questions`.
        Claude sees both items + answers and commits a schedule.
    """
    from app.modules.production.services.ai_planner import (
        collect_planning_context, call_claude, create_plan_from_ai, DAILY_PLAN_PROMPT,
    )

    if not body.selected_items:
        raise HTTPException(status_code=400, detail="No items selected")

    pool = request.app.state.db_pool
    settings = request.app.state.settings
    target_date = body.plan_date or date.today()
    fulfillment_ids = [item.fulfillment_id for item in body.selected_items]

    # Build user constraints from frontend selections
    user_constraints = []
    for item in body.selected_items:
        if item.floors or item.machines:
            user_constraints.append({
                "fulfillment_id": item.fulfillment_id,
                "allowed_floors": item.floors,
                "allowed_machines": item.machines,
                "floor_qty_kg": item.floor_qty,
                "floor_qty_units": item.floor_qty_units,
            })

    answered = [a.model_dump() for a in body.answered_questions] if body.answered_questions else None

    async with pool.acquire() as conn:
        context = await collect_planning_context(
            conn, body.entity, target_date, fulfillment_ids,
            user_constraints=user_constraints,
            answered_questions=answered,
        )
        ai_result = await call_claude(settings, DAILY_PLAN_PROMPT, context)
        async with conn.transaction():
            plan_result = await create_plan_from_ai(
                conn, body.entity, body.plan_type,
                target_date, target_date,
                ai_result, settings,
            )

    parsed = ai_result["parsed"]
    return {
        **plan_result,
        "plan_name": body.plan_name or f"{body.plan_type.title()} Plan — {target_date}",
        "schedule": parsed.get("schedule", []),
    }


# ---------------------------------------------------------------------------
# Plan revision with AI
# ---------------------------------------------------------------------------

class ReviseWithAIRequest(BaseModel):
    change_event: str
    # See CreatePlanWithAIRequest.answered_questions — same two-call pattern.
    answered_questions: list[AnsweredQuestion] = []


# Source plan must be in one of these statuses to be revisable.
# Domain per production_schema.sql:147 is {draft, approved, executed, cancelled, revised}.
# 'revised' is terminal (superseded). 'cancelled' is terminal.
_REVISABLE_STATUSES = {'draft', 'approved', 'executed'}


@router.post("/plans/{plan_id}/revise-with-ai")
async def revise_plan_with_ai(
    plan_id: int,
    request: Request,
    body: ReviseWithAIRequest,
    user: AuthUser = Depends(
        require_permission("production", "plans", "revise", "create")
    ),
):
    """Generate a revised production plan using Claude AI.

    Takes the existing plan, a free-text change_event (e.g. "Roaster A breakdown"),
    and produces a new plan with revision_number incremented. Lines that are
    in_progress or completed are preserved verbatim; planned lines may be
    rescheduled, cancelled, or new lines added.

    Entity is always derived from the source plan — callers cannot override it.
    Permission scope is re-checked against the plan's actual entity AFTER fetch,
    since the dependency-level check uses query-param entity which we don't have.
    """
    from app.modules.production.services.ai_planner import (
        collect_revision_context, call_claude, create_revised_plan, PLAN_REVISION_PROMPT,
    )

    if not body.change_event or not body.change_event.strip():
        raise HTTPException(status_code=400, detail="change_event is required")

    pool = request.app.state.db_pool
    settings = request.app.state.settings

    async with pool.acquire() as conn:
        # Pre-flight existence + status check (cheap fail before burning Claude tokens)
        plan = await conn.fetchrow(
            "SELECT entity, status FROM production_plan WHERE plan_id = $1", plan_id,
        )
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

        # Entity-scope re-check using the plan's actual entity (the Depends-level
        # check at the top sees no entity for this endpoint and so cannot enforce
        # allowed_entities scoping). Admin bypasses inside check_permission.
        scope_ok = await check_permission(
            conn, user.role_ids, user.is_admin,
            "production", "plans", "revise", "create",
            entity=plan['entity'],
        )
        if not scope_ok:
            raise HTTPException(
                status_code=403,
                detail=f"revise_plan not allowed for entity '{plan['entity']}'",
            )

        if plan['status'] not in _REVISABLE_STATUSES:
            # Walk the full forward revision chain to find the most recent
            # non-terminal descendant, so the error message points at the
            # actual head of the chain rather than just a direct child.
            latest_id = await conn.fetchval(
                """
                WITH RECURSIVE chain(plan_id, status, revision_number) AS (
                    SELECT plan_id, status, revision_number
                      FROM production_plan WHERE plan_id = $1
                    UNION ALL
                    SELECT p.plan_id, p.status, p.revision_number
                      FROM production_plan p
                      JOIN chain c ON p.previous_plan_id = c.plan_id
                )
                SELECT plan_id FROM chain
                 WHERE plan_id <> $1 AND status NOT IN ('revised', 'cancelled')
                 ORDER BY revision_number DESC NULLS LAST, plan_id DESC
                 LIMIT 1
                """,
                plan_id,
            )
            detail = (
                f"Plan status '{plan['status']}' is not revisable "
                f"(allowed: {sorted(_REVISABLE_STATUSES)})."
            )
            if latest_id:
                detail += f" Latest revisable plan in this chain is plan_id={latest_id}."
            raise HTTPException(status_code=409, detail=detail)

        entity = plan['entity']
        answered = [a.model_dump() for a in body.answered_questions] if body.answered_questions else None
        context = await collect_revision_context(
            conn, plan_id, body.change_event, entity, answered_questions=answered,
        )
        ai_result = await call_claude(settings, PLAN_REVISION_PROMPT, context)

        async with conn.transaction():
            # Re-check status under row lock to prevent concurrent revisions racing past
            # the pre-flight check during the multi-second Claude call.
            locked_status = await conn.fetchval(
                "SELECT status FROM production_plan WHERE plan_id = $1 FOR UPDATE",
                plan_id,
            )
            if locked_status not in _REVISABLE_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=f"Plan status changed to '{locked_status}' during revision; aborted",
                )
            result = await create_revised_plan(conn, plan_id, entity, ai_result, settings)

    parsed = ai_result["parsed"]
    return {**result, "revised_schedule": parsed.get("revised_schedule", [])}


# ---------------------------------------------------------------------------
# Plan read endpoints
# ---------------------------------------------------------------------------


@router.get("/plans")
async def list_plans(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    plan_type: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """Paginated list of production plans with filters."""
    pool = request.app.state.db_pool

    conditions = []
    params = []
    idx = 1

    if entity:
        conditions.append(f"entity = ${idx}")
        params.append(entity)
        idx += 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if plan_type:
        conditions.append(f"plan_type = ${idx}")
        params.append(plan_type)
        idx += 1
    if date_from:
        try:
            df = date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_from format. Use YYYY-MM-DD.")
        conditions.append(f"plan_date >= ${idx}")
        params.append(df)
        idx += 1
    if date_to:
        try:
            dt = date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_to format. Use YYYY-MM-DD.")
        conditions.append(f"plan_date <= ${idx}")
        params.append(dt)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM production_plan WHERE {where}", *params)
        rows = await conn.fetch(
            f"""
            SELECT plan_id, plan_name, entity, plan_type, plan_date, date_from, date_to,
                   status, ai_generated, revision_number, approved_by, approved_at, created_at
            FROM production_plan WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, page_size, offset,
        )

    return {
        "results": [dict(r) for r in rows],
        "pagination": {
            "page": page, "page_size": page_size, "total": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
        },
    }


@router.get("/plans/all")
async def list_plans_all(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    plan_type: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
):
    """All plans matching filters, no pagination."""
    result = await list_plans(request, entity, status, plan_type, date_from, date_to, page=1, page_size=100000)
    return result["results"]


@router.get("/plans/{plan_id}")
async def get_plan_detail(request: Request, plan_id: int):
    """Get plan detail with all lines, material check, and risk flags."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT * FROM production_plan WHERE plan_id = $1", plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

        lines = await conn.fetch(
            "SELECT * FROM production_plan_line WHERE plan_id = $1 ORDER BY priority, plan_line_id",
            plan_id,
        )

    result = dict(plan)
    result["lines"] = [dict(l) for l in lines]

    # Extract material_check and risk_flags from ai_analysis_json
    ai_json = plan.get("ai_analysis_json")
    if ai_json:
        if isinstance(ai_json, str):
            ai_json = json.loads(ai_json)
        result["material_check"] = ai_json.get("material_check", [])
        result["risk_flags"] = ai_json.get("risk_flags", [])
    else:
        result["material_check"] = []
        result["risk_flags"] = []

    return result


# ---------------------------------------------------------------------------
# MRP read endpoint
# ---------------------------------------------------------------------------

@router.get("/mrp/availability")
async def mrp_availability(
    request: Request,
    material: str = Query(...),
    qty: float = Query(...),
    entity: str = Query(...),
):
    """Quick single-material availability check."""
    from app.modules.production.services.mrp import check_availability
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await check_availability(conn, material, qty, entity)


# ---------------------------------------------------------------------------
# Indent read endpoints (indent actions are via MCP only)
# ---------------------------------------------------------------------------


@router.get("/indents")
async def list_indents(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    source: str = Query(None),
    search: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """List purchase indents with filters."""
    pool = request.app.state.db_pool

    conditions = []
    params = []
    idx = 1

    if entity:
        conditions.append(f"i.entity = ${idx}")
        params.append(entity)
        idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',')]
        ph = ', '.join(f'${idx + j}' for j in range(len(statuses)))
        conditions.append(f"i.status IN ({ph})")
        params.extend(statuses)
        idx += len(statuses)
    if source:
        conditions.append(f"i.indent_source = ${idx}")
        params.append(source)
        idx += 1
    if search:
        conditions.append(f"(i.material_sku_name ILIKE ${idx} OR i.indent_number ILIKE ${idx} OR i.customer_name ILIKE ${idx})")
        params.append(f"%{search}%")
        idx += 1
    if date_from:
        conditions.append(f"i.created_at >= ${idx}::date")
        params.append(date_from)
        idx += 1
    if date_to:
        conditions.append(f"i.created_at <= ${idx}::date + interval '1 day'")
        params.append(date_to)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM purchase_indent i WHERE {where}", *params)
        rows = await conn.fetch(
            f"""
            SELECT i.* FROM purchase_indent i
            WHERE {where}
            ORDER BY i.created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, page_size, offset,
        )

    return {
        "results": [dict(r) for r in rows],
        "pagination": {
            "page": page, "page_size": page_size, "total": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
        },
    }


@router.get("/indents/all")
async def list_indents_all(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    source: str = Query(None),
    search: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
):
    """All indents matching filters, no pagination."""
    result = await list_indents(request, entity, status, source, search, date_from, date_to, page=1, page_size=100000)
    return result["results"]


@router.get("/indents/{indent_id}")
async def get_indent(request: Request, indent_id: int):
    """Get indent detail with linked plan line info."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        indent = await conn.fetchrow("SELECT * FROM purchase_indent WHERE indent_id = $1", indent_id)
        if not indent:
            raise HTTPException(status_code=404, detail="Indent not found")

        result = dict(indent)

        if indent['plan_line_id']:
            pl = await conn.fetchrow(
                "SELECT fg_sku_name, customer_name, planned_qty_kg FROM production_plan_line WHERE plan_line_id = $1",
                indent['plan_line_id'],
            )
            result["plan_line"] = dict(pl) if pl else None

    return result


# ---------------------------------------------------------------------------
# Alert endpoints
# ---------------------------------------------------------------------------


@router.get("/alerts")
async def list_alerts(
    request: Request,
    target_team: str = Query(None),
    is_read: bool = Query(None),
    entity: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """List store alerts with filters."""
    pool = request.app.state.db_pool

    conditions = []
    params = []
    idx = 1

    if target_team:
        conditions.append(f"target_team = ${idx}")
        params.append(target_team)
        idx += 1
    if is_read is not None:
        conditions.append(f"is_read = ${idx}")
        params.append(is_read)
        idx += 1
    if entity:
        conditions.append(f"entity = ${idx}")
        params.append(entity)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM store_alert WHERE {where}", *params)
        rows = await conn.fetch(
            f"""
            SELECT * FROM store_alert WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, page_size, offset,
        )

    return {
        "results": [dict(r) for r in rows],
        "pagination": {
            "page": page, "page_size": page_size, "total": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
        },
    }


@router.put("/alerts/{alert_id}/read")
async def mark_alert_read(request: Request, alert_id: int):
    """Mark an alert as read."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE store_alert SET is_read = TRUE WHERE alert_id = $1", alert_id,
        )
        if result == 'UPDATE 0':
            raise HTTPException(status_code=404, detail="Alert not found")
    return {"alert_id": alert_id, "is_read": True}


# ---------------------------------------------------------------------------
# Production Order endpoints
# ---------------------------------------------------------------------------


class CreateOrdersRequest(BaseModel):
    plan_id: int


class GenerateJobCardsRequest(BaseModel):
    prod_order_id: int


@router.post("/orders/create-from-plan")
async def create_orders_from_plan(request: Request, body: CreateOrdersRequest):
    """Create production orders from all lines in an approved plan."""
    from app.modules.production.services.job_card_engine import create_production_orders
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT entity FROM production_plan WHERE plan_id = $1", body.plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        async with conn.transaction():
            return await create_production_orders(conn, body.plan_id, plan['entity'])


@router.get("/orders")
async def list_orders(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """List production orders with filters."""
    pool = request.app.state.db_pool
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status = ${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM production_order WHERE {where}", *params)
        rows = await conn.fetch(
            f"SELECT * FROM production_order WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, page_size, offset,
        )
    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "total_pages": (total + page_size - 1) // page_size if total else 0},
    }


@router.get("/orders/all")
async def list_orders_all(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
):
    """All production orders matching filters, no pagination."""
    result = await list_orders(request, entity, status, page=1, page_size=100000)
    return result["results"]


@router.get("/orders/{prod_order_id}/job-card-chain")
async def job_card_chain(request: Request, prod_order_id: int):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT job_card_id, job_card_number, step_number, process_name, stage,
                   floor, status, batch_size_kg, carried_qty_kg, dispatched_to_next_kg,
                   prev_job_card_id, next_job_card_id
            FROM job_card
            WHERE prod_order_id = $1
            ORDER BY step_number
            """,
            prod_order_id,
        )
        if not rows:
            raise HTTPException(status_code=404, detail="No job cards found for this production order")
        return [
            {
                "job_card_id": r['job_card_id'],
                "job_card_number": r['job_card_number'],
                "step_number": r['step_number'],
                "process_name": r['process_name'],
                "stage": r['stage'],
                "floor": r['floor'],
                "status": r['status'],
                "batch_size_kg": float(r['batch_size_kg']) if r['batch_size_kg'] else None,
                "carried_qty_kg": float(r['carried_qty_kg']) if r['carried_qty_kg'] is not None else None,
                "dispatched_to_next_kg": float(r['dispatched_to_next_kg']) if r['dispatched_to_next_kg'] is not None else None,
                "prev_job_card_id": r['prev_job_card_id'],
                "next_job_card_id": r['next_job_card_id'],
            }
            for r in rows
        ]


@router.get("/orders/{prod_order_id}")
async def get_order_detail(request: Request, prod_order_id: int):
    """Get production order detail with job cards."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM production_order WHERE prod_order_id = $1", prod_order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        jcs = await conn.fetch(
            "SELECT job_card_id, job_card_number, step_number, process_name, stage, status, is_locked FROM job_card WHERE prod_order_id = $1 ORDER BY step_number",
            prod_order_id,
        )
    result = dict(order)
    result["job_cards"] = [dict(j) for j in jcs]
    return result


# ---------------------------------------------------------------------------
# Job Card endpoints
# ---------------------------------------------------------------------------


# ═══════════════════════════════════════════════════════════════
# Store Control Endpoints
# ═══════════════════════════════════════════════════════════════


class AllocationDecision(BaseModel):
    allocation_id: int
    decision: str  # approved, rejected, partial
    approved_qty: float | None = None
    rejected_qty: float | None = None
    rejection_reason: str | None = None
    rejection_detail: str | None = None
    reserved_for_customer: str | None = None
    quality_grade_available: str | None = None
    quality_grade_required: str | None = None
    expiry_date: str | None = None
    raise_purchase_indent: bool = False


class AllocationRequest(BaseModel):
    decisions: list[AllocationDecision]
    decided_by: str


class FloorVerification(BaseModel):
    allocation_id: int
    verified_qty: float
    condition_notes: str = ""


class FloorVerifyRequest(BaseModel):
    job_card_id: int
    verifications: list[FloorVerification]
    verified_by: str


class SuggestAlternativeRequest(BaseModel):
    allocation_id: int
    offgrade_id: int
    qty: float
    suggested_by: str


class DispatchToNextRequest(BaseModel):
    qty_kg: float
    dispatched_by: str = ""


@router.get("/store/pending-allocations")
async def store_pending_allocations(
    request: Request,
    entity: str = Query(None),
    job_card_id: int = Query(None),
    material: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    size: int = Query(None, ge=1, le=500),
):
    """List all pending allocation requests for store team."""
    from app.modules.production.services.store_controller import get_pending_allocations
    if size is not None:
        page_size = size
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_pending_allocations(
            conn, entity=entity, job_card_id=job_card_id,
            material=material, page=page, page_size=page_size,
        )


@router.get("/store/pending-allocations/all")
async def store_pending_allocations_all(
    request: Request,
    entity: str = Query(None),
    job_card_id: int = Query(None),
    material: str = Query(None),
):
    """All pending allocations matching filters, no pagination."""
    from app.modules.production.services.store_controller import get_pending_allocations
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_pending_allocations(
            conn, entity=entity, job_card_id=job_card_id,
            material=material, page=1, page_size=100000,
        )
    return result["results"]


@router.get("/store/dashboard")
async def store_dashboard(request: Request, entity: str = Query(...)):
    """Aggregated store dashboard stats."""
    from app.modules.production.services.store_controller import get_store_dashboard
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_store_dashboard(conn, entity)


@router.post("/store/decide")
async def store_decide(request: Request, body: AllocationRequest, entity: str = Query(...)):
    """Submit allocation decisions (approve/reject/partial)."""
    from app.modules.production.services.store_controller import decide_allocation
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await decide_allocation(
                    conn, [d.model_dump() for d in body.decisions],
                    body.decided_by, entity,
                )
    return result


@router.post("/store/verify-floor-stock")
async def store_verify_floor(request: Request, body: FloorVerifyRequest, entity: str = Query(...)):
    """Store verifies material already on production floor."""
    from app.modules.production.services.store_controller import verify_floor_stock
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await verify_floor_stock(
                conn, body.job_card_id,
                [v.model_dump() for v in body.verifications],
                body.verified_by, entity,
            )


@router.post("/store/suggest-alternative")
async def store_suggest_alt(request: Request, body: SuggestAlternativeRequest, entity: str = Query(...)):
    """Store suggests off-grade alternative."""
    from app.modules.production.services.store_controller import suggest_alternative
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await suggest_alternative(
                conn, body.allocation_id, body.offgrade_id,
                body.qty, body.suggested_by, entity,
            )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/job-cards/{job_card_id}/allocations")
async def job_card_allocations(request: Request, job_card_id: int):
    """Get store allocation records for a specific job card."""
    from app.modules.production.services.store_controller import get_allocation_summary
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_allocation_summary(conn, job_card_id)


@router.post("/job-cards/{job_card_id}/dispatch-to-next")
async def dispatch_to_next(
    request: Request,
    job_card_id: int,
    body: DispatchToNextRequest,
    entity: str = Query(...),
):
    from app.modules.production.services.job_card_engine import dispatch_partial_to_next_stage
    from app.webhooks import events
    if body.qty_kg <= 0:
        raise HTTPException(status_code=422, detail="qty_kg must be > 0")
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT job_card_number FROM job_card WHERE job_card_id = $1", job_card_id)
        # M1/M4: buffer event inside transaction; use authoritative qty from the
        # service result (service may have clamped it via atomic UPDATE) rather
        # than echoing the request-body qty verbatim.
        async with deferred_events():
            async with conn.transaction():
                result = await dispatch_partial_to_next_stage(
                    conn, job_card_id, body.qty_kg, body.dispatched_by, entity,
                )
                if "error" in result:
                    raise HTTPException(status_code=400, detail=result["error"])
                if jc:
                    try:
                        await events.job_card_dispatched_to_next(
                            entity,
                            job_card_id=job_card_id,
                            job_card_number=jc['job_card_number'],
                            qty_kg=float(result.get("qty_kg", body.qty_kg)),
                            dispatched_by=body.dispatched_by,
                        )
                    except Exception:
                        logger.exception("job_card_dispatched_to_next emit buffering failed; swallowing")
    return result


# ══════════════════════════════════════════
#  INVENTORY BATCH ENDPOINTS
# ══════════════════════════════════════════

@router.get("/inventory/batches")
async def list_batches(
    request: Request,
    entity: str = Query(...),
    sku_name: str = Query(None),
    status: str = Query(None),
    floor_id: str = Query(None),
    warehouse_id: str = Query(None),
):
    from app.modules.production.services.inventory_service import get_available_batches, get_inventory_summary
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if sku_name:
            batches = await get_available_batches(conn, sku_name, entity,
                                                   exclude_blocked=(status != 'BLOCKED'),
                                                   floor_id=floor_id)
            return {"batches": batches}
        else:
            summary = await get_inventory_summary(conn, entity, floor_id=floor_id,
                                                   warehouse_id=warehouse_id, status=status)
            return {"summary": summary}


@router.get("/inventory/batch/{batch_id}")
async def get_batch_detail(request: Request, batch_id: str):
    from app.modules.production.services.inventory_service import get_batch, get_batch_history
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        batch = await get_batch(conn, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")
        history = await get_batch_history(conn, batch_id)
        return {"batch": batch, "history": history}


class BatchFlagRequest(BaseModel):
    reason: str
    detail: str | None = None
    performed_by: str


@router.post("/inventory/batch/{batch_id}/flag")
async def flag_batch_endpoint(request: Request, batch_id: str, body: BatchFlagRequest):
    from app.modules.production.services.inventory_service import flag_batch
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await flag_batch(conn, batch_id, body.reason, body.detail, body.performed_by)
    return {"status": "flagged", "batch_id": batch_id}


class BatchBlockRequest(BaseModel):
    so_id: int
    blocked_by: str
    block_reason: str | None = None


@router.post("/inventory/batch/{batch_id}/block")
async def block_batch_endpoint(request: Request, batch_id: str, body: BatchBlockRequest):
    from app.modules.production.services.inventory_service import block_batch
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await block_batch(conn, batch_id, body.so_id, body.blocked_by, body.block_reason)
    return {"status": "blocked", "batch_id": batch_id, "so_id": body.so_id}


class ForceReassignRequest(BaseModel):
    new_so_id: int
    override_by: str
    override_note: str


@router.post("/inventory/batch/{batch_id}/force-reassign")
async def force_reassign_endpoint(request: Request, batch_id: str, body: ForceReassignRequest,
                                   entity: str = Query(...)):
    from app.modules.production.services.inventory_service import force_reassign_batch
    # Permission check: require FORCE_REASSIGN permission
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # Check auth permission if session exists
        session_token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if session_token:
            has_perm = await conn.fetchval("""
                SELECT 1 FROM auth_session s
                JOIN auth_user u ON s.user_id = u.user_id
                JOIN auth_role_permission rp ON u.role_id = rp.role_id
                JOIN auth_permission p ON rp.permission_id = p.permission_id
                WHERE s.session_token = $1 AND s.is_active = TRUE
                  AND p.action = 'force_reassign'
                LIMIT 1
            """, session_token)
            # If auth is configured but user lacks permission, reject
            auth_configured = await conn.fetchval("SELECT COUNT(*) FROM auth_user")
            if auth_configured and auth_configured > 0 and not has_perm:
                raise HTTPException(status_code=403, detail="FORCE_REASSIGN permission required")
        async with conn.transaction():
            result = await force_reassign_batch(conn, batch_id, body.new_so_id,
                                                body.override_by, body.override_note, entity)
    return result


class LegacyImportItem(BaseModel):
    sku_name: str
    item_type: str | None = None
    qty_kg: float
    warehouse_id: str | None = None
    floor_id: str | None = None


class LegacyImportRequest(BaseModel):
    items: list[LegacyImportItem]
    performed_by: str | None = None


@router.post("/inventory/legacy-import")
async def legacy_import_endpoint(request: Request, body: LegacyImportRequest,
                                  entity: str = Query(...)):
    from app.modules.production.services.inventory_service import import_legacy_batches
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await import_legacy_batches(
                conn, [item.dict() for item in body.items], entity, body.performed_by)
    return result


class InternalIssueRequest(BaseModel):
    sku_name: str
    batch_id: str | None = None
    qty_kg: float
    source_warehouse: str | None = None
    source_floor: str | None = None
    destination_floor: str
    purpose: str
    requested_by: str


@router.post("/inventory/internal-issue")
async def create_internal_issue_endpoint(request: Request, body: InternalIssueRequest,
                                          entity: str = Query(...)):
    from app.modules.production.services.inventory_service import create_internal_issue
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await create_internal_issue(conn, sku_name=body.sku_name,
                batch_id=body.batch_id, qty_kg=body.qty_kg,
                source_warehouse=body.source_warehouse, source_floor=body.source_floor,
                destination_floor=body.destination_floor, purpose=body.purpose,
                requested_by=body.requested_by, entity=entity)
    return result


class ApproveIssueRequest(BaseModel):
    approved_by: str


@router.post("/inventory/internal-issue/{note_id}/approve")
async def approve_internal_issue_endpoint(request: Request, note_id: int,
                                           body: ApproveIssueRequest):
    from app.modules.production.services.inventory_service import approve_internal_issue
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await approve_internal_issue(conn, note_id, body.approved_by)
    return result


@router.get("/inventory/shortfall")
async def check_shortfall_endpoint(request: Request,
                                    sku_name: str = Query(...),
                                    required_qty: float = Query(...),
                                    entity: str = Query(...),
                                    so_id: int = Query(None),
                                    job_card_id: int = Query(None)):
    from app.modules.production.services.inventory_service import check_shortfall
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await check_shortfall(conn, sku_name, required_qty, entity,
                                     so_id=so_id, job_card_id=job_card_id)


@router.get("/inventory/reconcile")
async def reconcile_endpoint(request: Request, entity: str = Query(...)):
    from app.modules.production.services.inventory_service import reconcile_quantities
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await reconcile_quantities(conn, entity)


class BatchRejectRequest(BaseModel):
    rejected_by: str
    reason_code: str  # QUALITY_ISSUE/CONTAMINATION/DAMAGED/PENDING_QC/OTHER
    reason_text: str | None = None
    job_card_id: int | None = None
    so_id: int | None = None


@router.post("/inventory/batch/{batch_id}/reject")
async def reject_batch_endpoint(request: Request, batch_id: str, body: BatchRejectRequest,
                                 entity: str = Query(...)):
    from app.modules.production.services.inventory_service import log_batch_rejection
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await log_batch_rejection(conn, batch_id, body.rejected_by,
                body.reason_code, body.reason_text, body.job_card_id, body.so_id, entity)
    return result


class ResolveFlagRequest(BaseModel):
    resolution: str  # AVAILABLE or SCRAPPED
    resolved_by: str
    notes: str | None = None


@router.post("/inventory/batch/{batch_id}/resolve")
async def resolve_batch_endpoint(request: Request, batch_id: str, body: ResolveFlagRequest):
    from app.modules.production.services.inventory_service import resolve_flagged_batch
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await resolve_flagged_batch(conn, batch_id, body.resolution,
                                               body.resolved_by, body.notes)


class ReturnBatchRequest(BaseModel):
    qty_kg: float
    return_reason: str
    returned_by: str
    destination_floor: str | None = None


@router.post("/inventory/batch/{batch_id}/return")
async def return_batch_endpoint(request: Request, batch_id: str, body: ReturnBatchRequest):
    from app.modules.production.services.inventory_service import return_batch
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await return_batch(conn, batch_id, body.qty_kg, body.return_reason,
                                      body.returned_by, body.destination_floor)


@router.get("/inventory/batch/{batch_id}/rejections")
async def batch_rejections_endpoint(request: Request, batch_id: str):
    from app.modules.production.services.inventory_service import get_batch_rejections
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_batch_rejections(conn, batch_id)


@router.post("/inventory/internal-issue/{note_id}/approve-constrained")
async def approve_constrained_endpoint(request: Request, note_id: int,
                                        body: ApproveIssueRequest,
                                        space_constrained: bool = Query(False)):
    from app.modules.production.services.inventory_service import approve_internal_issue_with_space_constraint
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await approve_internal_issue_with_space_constraint(
                conn, note_id, body.approved_by, space_constrained)


class RejectIssueRequest(BaseModel):
    rejected_by: str
    reason: str


@router.post("/inventory/internal-issue/{note_id}/reject")
async def reject_internal_issue_endpoint(request: Request, note_id: int, body: RejectIssueRequest):
    from app.modules.production.services.inventory_service import reject_internal_issue
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await reject_internal_issue(conn, note_id, body.rejected_by, body.reason)


@router.get("/inventory/internal-issues")
async def list_internal_issues_endpoint(request: Request, entity: str = Query(...),
                                         status: str = Query(None)):
    from app.modules.production.services.inventory_service import list_internal_issues
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await list_internal_issues(conn, entity, status)


@router.get("/inventory/legacy-log")
async def legacy_import_log_endpoint(request: Request, entity: str = Query(...)):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM legacy_import_log WHERE entity = $1 ORDER BY generated_at DESC LIMIT 100",
            entity)
        return [dict(r) for r in rows]


@router.get("/inventory/reconciliation-failures")
async def reconciliation_failures_endpoint(request: Request, entity: str = Query(...)):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM reconciliation_failures WHERE entity = $1 ORDER BY detected_at DESC LIMIT 50",
            entity)
        return [dict(r) for r in rows]


@router.post("/job-cards/generate")
async def generate_job_cards(request: Request, body: GenerateJobCardsRequest):
    """Generate sequential job cards for a production order."""
    from app.modules.production.services.job_card_engine import create_job_cards
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await create_job_cards(conn, body.prod_order_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/job-cards")
async def list_job_cards(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    team_leader: str = Query(None),
    floor: str = Query(None),
    factory: str = Query(None),
    stage: str = Query(None),
    search: str = Query(None),
    customer: str = Query(None),
    article: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    size: int = Query(None, ge=1, le=500),
    include_cancelled: bool = Query(False),
    user=Depends(get_current_user),
):
    """List job cards. For non-admin users with a floor / warehouse lock,
    the result is restricted to JCs on those floors / warehouses regardless
    of the corresponding query params (a request for a floor outside the
    lock returns 403)."""
    if size is not None:
        page_size = size
    pool = request.app.state.db_pool
    conditions = []
    params = []
    idx = 1

    # ── User-level scope lock ─────────────────────────────────────────────
    # The middleware-level lock checks ?floor= / ?warehouse= and 403s when
    # they're out of range. Here we also intersect the IMPLICIT scope (no
    # query param given) so a floor-locked user defaults to their floors
    # instead of "all floors". Admin bypasses both.
    user_floors     = getattr(user, "allowed_floors", []) or []
    user_warehouses = getattr(user, "allowed_warehouses", []) or []
    is_admin = getattr(user, "is_admin", False)

    if not include_cancelled:
        conditions.append("deleted_at IS NULL")
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',')]
        ph = ', '.join(f'${idx+i}' for i in range(len(statuses)))
        conditions.append(f"status IN ({ph})"); params.extend(statuses); idx += len(statuses)
    if team_leader:
        conditions.append(f"assigned_to_team_leader ILIKE ${idx}"); params.append(f"%{team_leader}%"); idx += 1

    # Floor handling: explicit param wins (after lock check); else apply lock.
    if floor:
        if not is_admin and user_floors and floor not in user_floors:
            raise HTTPException(status_code=403,
                                detail=f"User is not assigned to floor '{floor}'")
        conditions.append(f"floor ILIKE ${idx}"); params.append(f"%{floor}%"); idx += 1
    elif not is_admin and user_floors:
        conditions.append(f"floor = ANY(${idx}::text[])")
        params.append(list(user_floors)); idx += 1

    if factory:
        if not is_admin and user_warehouses and factory not in user_warehouses:
            raise HTTPException(status_code=403,
                                detail=f"User is not assigned to factory '{factory}'")
        conditions.append(f"factory ILIKE ${idx}"); params.append(f"%{factory}%"); idx += 1
    elif not is_admin and user_warehouses:
        conditions.append(f"factory = ANY(${idx}::text[])")
        params.append(list(user_warehouses)); idx += 1
    if stage:
        conditions.append(f"stage = ${idx}"); params.append(stage); idx += 1
    if search:
        conditions.append(f"(job_card_number ILIKE ${idx} OR fg_sku_name ILIKE ${idx} OR customer_name ILIKE ${idx} OR batch_number ILIKE ${idx})")
        params.append(f"%{search}%"); idx += 1
    if customer:
        conditions.append(f"customer_name ILIKE ${idx}"); params.append(f"%{customer}%"); idx += 1
    if article:
        conditions.append(f"fg_sku_name ILIKE ${idx}"); params.append(f"%{article}%"); idx += 1
    if date_from:
        conditions.append(f"created_at::date >= ${idx}::date"); params.append(date_from); idx += 1
    if date_to:
        conditions.append(f"created_at::date <= ${idx}::date"); params.append(date_to); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM job_card WHERE {where}", *params)
        rows = await conn.fetch(
            f"""SELECT job_card_id, job_card_number, prod_order_id, step_number, process_name, stage,
                       fg_sku_name, customer_name, batch_number, batch_size_kg,
                       assigned_to_team_leader, team_members, is_locked, force_unlocked, status,
                       start_time, end_time, total_time_min, factory, floor, entity,
                       store_allocation_status, created_at
                FROM job_card WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}""",
            *params, page_size, offset,
        )

        # Get distinct values for filter dropdowns. Mirror the include_cancelled
        # gate so dropdowns don't show stale values from soft-cancelled cards.
        fo_filter = "" if include_cancelled else " AND deleted_at IS NULL"
        filter_options = {}
        filter_options['customers'] = [r['customer_name'] for r in await conn.fetch(
            f"SELECT DISTINCT customer_name FROM job_card WHERE customer_name IS NOT NULL{fo_filter} ORDER BY customer_name")]
        filter_options['team_leaders'] = [r['assigned_to_team_leader'] for r in await conn.fetch(
            f"SELECT DISTINCT assigned_to_team_leader FROM job_card WHERE assigned_to_team_leader IS NOT NULL{fo_filter} ORDER BY assigned_to_team_leader")]
        filter_options['floors'] = [r['floor'] for r in await conn.fetch(
            f"SELECT DISTINCT floor FROM job_card WHERE floor IS NOT NULL{fo_filter} ORDER BY floor")]
        filter_options['factories'] = [r['factory'] for r in await conn.fetch(
            f"SELECT DISTINCT factory FROM job_card WHERE factory IS NOT NULL{fo_filter} ORDER BY factory")]
        filter_options['stages'] = [r['stage'] for r in await conn.fetch(
            f"SELECT DISTINCT stage FROM job_card WHERE stage IS NOT NULL{fo_filter} ORDER BY stage")]

    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "total_pages": (total + page_size - 1) // page_size if total else 0},
        "filter_options": filter_options,
    }


@router.get("/job-cards/all")
async def list_job_cards_all(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    team_leader: str = Query(None),
    floor: str = Query(None),
    factory: str = Query(None),
    stage: str = Query(None),
    search: str = Query(None),
    customer: str = Query(None),
    article: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    include_cancelled: bool = Query(False),
):
    """All job cards matching filters, no pagination."""
    result = await list_job_cards(request, entity, status, team_leader, floor, factory, stage, search, customer, article, date_from, date_to, page=1, page_size=100000, include_cancelled=include_cancelled)
    return result["results"]


@router.get("/job-cards/team-dashboard")
async def team_dashboard(
    request: Request,
    entity: str = Query(None),
    team_leader: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    include_cancelled: bool = Query(False),
):
    """Job cards assigned to a team leader, priority-sorted. All filters optional."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1
        if not include_cancelled:
            conditions.append("deleted_at IS NULL")
        if team_leader:
            conditions.append(f"assigned_to_team_leader ILIKE ${idx}"); params.append(f"%{team_leader}%"); idx += 1
        if entity:
            conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
        if date_from:
            conditions.append(f"created_at::date >= ${idx}::date"); params.append(date_from); idx += 1
        if date_to:
            conditions.append(f"created_at::date <= ${idx}::date"); params.append(date_to); idx += 1
        conditions.append("status NOT IN ('closed', 'completed', 'cancelled')")
        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"""SELECT * FROM job_card WHERE {where}
                ORDER BY
                  CASE status WHEN 'in_progress' THEN 1 WHEN 'material_received' THEN 2
                              WHEN 'assigned' THEN 3 WHEN 'unlocked' THEN 4 ELSE 5 END,
                  created_at""",
            *params,
        )
    return [dict(r) for r in rows]


@router.get("/job-cards/floor-dashboard")
async def floor_dashboard(
    request: Request,
    entity: str = Query(None),
    floor: str = Query(None),
    stage: str = Query(None),
    include_cancelled: bool = Query(False),
):
    """Job cards on a floor, optionally filtered by stage. All filters optional."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1
        if not include_cancelled:
            conditions.append("deleted_at IS NULL")
        if floor:
            conditions.append(f"floor ILIKE ${idx}"); params.append(f"%{floor}%"); idx += 1
        if entity:
            conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
        if stage:
            conditions.append(f"stage = ${idx}"); params.append(stage); idx += 1
        where = " AND ".join(conditions) if conditions else "TRUE"
        rows = await conn.fetch(
            f"SELECT * FROM job_card WHERE {where} ORDER BY status, created_at",
            *params,
        )
    return [dict(r) for r in rows]


@router.get("/job-cards/{job_card_id}/floor-stock-status")
async def floor_stock_status(request: Request, job_card_id: int):
    """Per-material floor stock status for a job card (RM + PM)."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # Get entity from job card
        jc = await conn.fetchrow(
            "SELECT entity, fg_sku_name FROM job_card WHERE job_card_id = $1", job_card_id
        )
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")

        entity = jc['entity']

        # Batch floor inventory
        inv_rows = await conn.fetch(
            """
            SELECT sku_name, floor_location, SUM(quantity_kg) as qty
            FROM floor_inventory
            WHERE entity = $1 AND floor_location IN ('rm_store', 'pm_store')
            GROUP BY sku_name, floor_location
            """,
            entity,
        )
        inv_map = {}
        for r in inv_rows:
            inv_map[(r['sku_name'].lower(), r['floor_location'])] = float(r['qty'])

        materials = []

        # RM indents
        rm_rows = await conn.fetch(
            """
            SELECT ri.rm_indent_id, ri.material_sku_name, ri.gross_qty, ri.status,
                   ri.store_approved_qty,
                   pi.required_qty_kg AS indent_qty, pi.status AS indent_status
            FROM job_card_rm_indent ri
            LEFT JOIN LATERAL (
                SELECT required_qty_kg, status
                FROM purchase_indent
                WHERE job_card_id = $1
                  AND LOWER(material_sku_name) = LOWER(ri.material_sku_name)
                ORDER BY created_at DESC
                LIMIT 1
            ) pi ON TRUE
            WHERE ri.job_card_id = $1
            """,
            job_card_id,
        )
        for r in rm_rows:
            on_floor = inv_map.get((r['material_sku_name'].lower(), 'rm_store'), 0.0)
            shortfall = max(0.0, float(r['gross_qty']) - on_floor)
            materials.append({
                "material": r['material_sku_name'],
                "type": "rm",
                "gross_req": float(r['gross_qty']),
                "on_floor": on_floor,
                "shortfall": round(shortfall, 3),
                "indent_status": r['status'],
                "store_approved_qty": float(r['store_approved_qty']) if r['store_approved_qty'] else None,
                "purchase_indent_qty": float(r['indent_qty']) if r['indent_qty'] else None,
                "purchase_indent_status": r['indent_status'],
            })

        # PM indents
        pm_rows = await conn.fetch(
            """
            SELECT pi2.pm_indent_id, pi2.material_sku_name, pi2.gross_qty, pi2.status,
                   pi2.store_approved_qty,
                   pi.required_qty_kg AS indent_qty, pi.status AS indent_status
            FROM job_card_pm_indent pi2
            LEFT JOIN LATERAL (
                SELECT required_qty_kg, status
                FROM purchase_indent
                WHERE job_card_id = $1
                  AND LOWER(material_sku_name) = LOWER(pi2.material_sku_name)
                ORDER BY created_at DESC
                LIMIT 1
            ) pi ON TRUE
            WHERE pi2.job_card_id = $1
            """,
            job_card_id,
        )
        for r in pm_rows:
            on_floor = inv_map.get((r['material_sku_name'].lower(), 'pm_store'), 0.0)
            shortfall = max(0.0, float(r['gross_qty']) - on_floor)
            materials.append({
                "material": r['material_sku_name'],
                "type": "pm",
                "gross_req": float(r['gross_qty']),
                "on_floor": on_floor,
                "shortfall": round(shortfall, 3),
                "indent_status": r['status'],
                "store_approved_qty": float(r['store_approved_qty']) if r['store_approved_qty'] else None,
                "purchase_indent_qty": float(r['indent_qty']) if r['indent_qty'] else None,
                "purchase_indent_status": r['indent_status'],
            })

        return {"job_card_id": job_card_id, "fg_sku_name": jc['fg_sku_name'], "materials": materials}


@router.get("/job-cards/{job_card_id}/dispatch-log")
async def dispatch_log(request: Request, job_card_id: int):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT dispatch_id, from_job_card_id, to_job_card_id,
                   qty_kg, dispatched_at, dispatched_by
            FROM job_card_partial_dispatch
            WHERE from_job_card_id = $1 OR to_job_card_id = $1
            ORDER BY dispatched_at DESC
            """,
            job_card_id,
        )
        return [
            {
                "dispatch_id": r['dispatch_id'],
                "from_job_card_id": r['from_job_card_id'],
                "to_job_card_id": r['to_job_card_id'],
                "qty_kg": float(r['qty_kg']),
                "dispatched_at": r['dispatched_at'].isoformat() if r['dispatched_at'] else None,
                "dispatched_by": r['dispatched_by'],
            }
            for r in rows
        ]


@router.get("/job-cards/{job_card_id}")
async def get_job_card(request: Request, job_card_id: int):
    """Get full job card detail matching CFC/PRD/JC/V3.0 PDF structure."""
    from app.modules.production.services.job_card_engine import get_job_card_detail
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_job_card_detail(conn, job_card_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job card not found")
    return result


@router.patch("/job-cards/{job_card_id}")
async def update_job_card(request: Request, job_card_id: int, body: JobCardPatchRequest):
    """Partial update of editable header fields. Only fields supplied in
    the request body are written; all other columns retain their current
    values. Returns 404 if not found, 409 if status is non-editable,
    422 if no editable fields supplied."""
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed_fields = await jc_editor.patch_job_card(
                    conn, job_card_id, body.model_dump(exclude_unset=True),
                )
                try:
                    await events.job_card_updated(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        changed_fields=changed_fields, updated_by=body.updated_by,
                    )
                except Exception:
                    logger.exception("job_card_updated emit buffering failed; swallowing")
    return {"ok": True, "job_card": row, "changed_fields": changed_fields}


@router.delete("/job-cards/{job_card_id}")
async def cancel_job_card_endpoint(request: Request, job_card_id: int, body: JobCardCancelRequest):
    """Soft-delete with cancellation reason. Allowed only when status ∈
    {locked, unlocked, assigned}. Returns 409 once material has been
    received — use force-unlock + close instead."""
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.cancel_job_card(
                    conn, job_card_id,
                    reason=body.cancellation_reason, deleted_by=body.deleted_by,
                )
                try:
                    await events.job_card_cancelled(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        cancellation_reason=body.cancellation_reason,
                        deleted_by=body.deleted_by,
                    )
                except Exception:
                    logger.exception("job_card_cancelled emit buffering failed; swallowing")
    return {"ok": True, "job_card": row}


# ---------------------------------------------------------------------------
# Job Card Lifecycle endpoints
# ---------------------------------------------------------------------------


class AssignRequest(BaseModel):
    team_leader: str
    team_members: list[str] | None = None


class CompleteStepRequest(BaseModel):
    step_number: int
    operator_name: str | None = None
    qc_passed: bool = False


class RecordOutputRequest(BaseModel):
    fg_expected_units: int | None = None
    fg_expected_kg: float | None = None
    fg_actual_units: int | None = None
    fg_actual_kg: float | None = None
    rm_consumed_kg: float | None = None
    process_loss_kg: float = 0


class SignOffRequest(BaseModel):
    sign_off_type: str
    name: str


class ForceUnlockRequest(BaseModel):
    authority: str
    reason: str


class ReceiveMaterialRequest(BaseModel):
    box_ids: list[str]


@router.put("/job-cards/{job_card_id}/assign")
async def assign_jc(request: Request, job_card_id: int, body: AssignRequest):
    from app.modules.production.services.job_card_engine import assign_job_card
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT job_card_number, entity FROM job_card WHERE job_card_id = $1", job_card_id)
        # M1: buffer events so they fire only on transaction commit.
        async with deferred_events():
            async with conn.transaction():
                result = await assign_job_card(conn, job_card_id, body.team_leader, body.team_members)
                if "error" not in result and jc:
                    try:
                        await events.job_card_team_assigned(jc['entity'], job_card_id=job_card_id, job_card_number=jc['job_card_number'], team_leader=body.team_leader, member_count=len(body.team_members or []))
                    except Exception:
                        logger.exception("job_card_team_assigned emit buffering failed; swallowing")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.post("/job-cards/{job_card_id}/receive-material")
async def receive_material(request: Request, job_card_id: int, body: ReceiveMaterialRequest):
    from app.modules.production.services.qr_service import receive_material_via_qr
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity, job_card_number FROM job_card WHERE job_card_id = $1", job_card_id)
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        # M1 + M2: buffer events so they fire only on commit, and skip emit
        # on an error result so a partial failure doesn't trigger downstream.
        async with deferred_events():
            async with conn.transaction():
                result = await receive_material_via_qr(conn, job_card_id, body.box_ids, jc['entity'])
                if "error" not in result:
                    try:
                        await events.job_card_material_received(jc['entity'], job_card_id=job_card_id, job_card_number=jc['job_card_number'], boxes_scanned=len(body.box_ids), total_kg=result.get("total_kg", 0))
                    except Exception:
                        logger.exception("job_card_material_received emit buffering failed; swallowing")
    return result


class ManualAckRequest(BaseModel):
    indent_lines: list[dict] | None = None  # [{ indent_type, indent_id }] — empty list / null = acknowledge all
    acknowledged_by: str


@router.post("/job-cards/{job_card_id}/acknowledge-material")
async def acknowledge_material(request: Request, job_card_id: int, body: ManualAckRequest):
    from app.modules.production.services.qr_service import manual_acknowledge_material, manual_acknowledge_all
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity, job_card_number FROM job_card WHERE job_card_id = $1", job_card_id)
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        async with conn.transaction():
            if body.indent_lines:
                result = await manual_acknowledge_material(conn, job_card_id, body.indent_lines, body.acknowledged_by, jc['entity'])
            else:
                result = await manual_acknowledge_all(conn, job_card_id, body.acknowledged_by, jc['entity'])
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    # M1/H1: post-commit emit, swallow failures so a broadcaster hiccup
    # does not flip a successful ack to a client-visible failure.
    try:
        await events.job_card_material_acknowledged(jc['entity'], job_card_id=job_card_id, job_card_number=jc['job_card_number'])
    except Exception:
        logger.exception("job_card_material_acknowledged emit failed; swallowing")
    return result


@router.put("/job-cards/{job_card_id}/start")
async def start_jc(request: Request, job_card_id: int):
    from app.modules.production.services.job_card_engine import start_job_card
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT job_card_number, fg_sku_name, floor, entity FROM job_card WHERE job_card_id = $1", job_card_id)
        # M1: buffer events so they're only published if the write succeeds.
        async with deferred_events():
            async with conn.transaction():
                result = await start_job_card(conn, job_card_id)
                if "error" not in result and jc:
                    try:
                        await events.job_card_started(jc['entity'], job_card_id=job_card_id, job_card_number=jc['job_card_number'], fg_sku_name=jc['fg_sku_name'], floor=jc['floor'])
                    except Exception:
                        logger.exception("job_card_started emit buffering failed; swallowing")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.put("/job-cards/{job_card_id}/complete-step")
async def complete_step(request: Request, job_card_id: int, body: CompleteStepRequest):
    from app.modules.production.services.job_card_engine import complete_process_step
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await complete_process_step(conn, job_card_id, body.step_number, body.operator_name, body.qc_passed)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.put("/job-cards/{job_card_id}/record-output")
async def record_jc_output(request: Request, job_card_id: int, body: RecordOutputRequest):
    from app.modules.production.services.job_card_engine import record_output
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await record_output(conn, job_card_id, body.model_dump())
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.put("/job-cards/{job_card_id}/complete")
async def complete_jc(request: Request, job_card_id: int):
    from app.modules.production.services.job_card_engine import complete_job_card
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity, job_card_number, fg_sku_name FROM job_card WHERE job_card_id = $1", job_card_id)
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        async with deferred_events():
            async with conn.transaction():
                result = await complete_job_card(conn, job_card_id, jc['entity'])
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    await events.job_card_completed(jc['entity'], job_card_id=job_card_id, job_card_number=jc['job_card_number'], fg_sku_name=jc['fg_sku_name'], duration_minutes=result.get("duration_minutes"))
    return result


@router.put("/job-cards/{job_card_id}/sign-off")
async def sign_off_jc(request: Request, job_card_id: int, body: SignOffRequest):
    from app.modules.production.services.job_card_engine import sign_off
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT job_card_number, entity FROM job_card WHERE job_card_id = $1", job_card_id)
        # M1 + M2: buffer events; skip emit on error result.
        async with deferred_events():
            async with conn.transaction():
                result = await sign_off(conn, job_card_id, body.sign_off_type, body.name)
                if "error" not in result and jc:
                    try:
                        await events.job_card_signed_off(jc['entity'], job_card_id=job_card_id, job_card_number=jc['job_card_number'], sign_off_type=body.sign_off_type, signed_by=body.name)
                    except Exception:
                        logger.exception("job_card_signed_off emit buffering failed; swallowing")
    return result


@router.put("/job-cards/{job_card_id}/close")
async def close_jc(request: Request, job_card_id: int):
    """Close a job card and, when every JC on the linked plan has reached a
    terminal state, auto-transition the plan_v2 row to 'executed'.

    The plan-close hook is fire-and-forget within the same transaction: if
    the JC close succeeds but the plan auto-close errors, we still surface
    the JC close success — the plan auto-close runs again on every JC
    close attempt so eventual consistency is fine.
    """
    from app.modules.production.services.job_card_engine import close_job_card
    from app.modules.production.services.job_card_v2 import maybe_close_plan_from_jcs
    pool = request.app.state.db_pool
    plan_closed = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await close_job_card(conn, job_card_id)
            if "error" not in result:
                plan_id = await conn.fetchval(
                    "SELECT plan_id FROM job_card WHERE job_card_id=$1",
                    job_card_id,
                )
                if plan_id is not None:
                    try:
                        plan_closed = await maybe_close_plan_from_jcs(conn, plan_id)
                    except Exception:
                        logger.exception("maybe_close_plan_from_jcs failed (jc_id=%d) — JC close stands", job_card_id)
    if "error" in result:
        if result["error"] == "missing_sign_offs":
            raise HTTPException(status_code=400, detail=f"Missing sign-offs: {result['missing']}")
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    if plan_closed:
        result["plan_auto_closed"] = True
    return result


@router.put("/job-cards/{job_card_id}/force-unlock")
async def force_unlock_jc(request: Request, job_card_id: int, body: ForceUnlockRequest):
    from app.modules.production.services.job_card_engine import force_unlock
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity, job_card_number FROM job_card WHERE job_card_id = $1", job_card_id)
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        # M1: buffer event inside the transaction.
        async with deferred_events():
            async with conn.transaction():
                result = await force_unlock(conn, job_card_id, body.authority, body.reason, jc['entity'])
                if "error" not in result:
                    try:
                        await events.job_card_force_unlocked(jc['entity'], job_card_id=job_card_id, job_card_number=jc['job_card_number'], reason=body.reason)
                    except Exception:
                        logger.exception("job_card_force_unlocked emit buffering failed; swallowing")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


# ---------------------------------------------------------------------------
# Job Card Annexure endpoints
# ---------------------------------------------------------------------------


class EnvironmentParam(BaseModel):
    parameter_name: str
    value: str


class EnvironmentRequest(BaseModel):
    parameters: list[EnvironmentParam]


class MetalDetectionRequest(BaseModel):
    check_type: str  # pre_packaging, post_packaging
    fe_pass: bool | None = None
    nfe_pass: bool | None = None
    ss_pass: bool | None = None
    failed_units: int = 0
    remarks: str | None = None
    seal_check: bool | None = None
    seal_failed_units: int = 0
    wt_check: bool | None = None
    wt_failed_units: int = 0
    dough_temp_c: float | None = None
    oven_temp_c: float | None = None
    baking_temp_c: float | None = None


class WeightSample(BaseModel):
    sample_number: int
    net_weight: float | None = None
    gross_weight: float | None = None
    leak_test_pass: bool | None = None


class WeightCheckRequest(BaseModel):
    target_wt_g: float | None = None
    tolerance_g: float | None = None
    accept_range_min: float | None = None
    accept_range_max: float | None = None
    samples: list[WeightSample]


class LossEntry(BaseModel):
    loss_category: str
    budgeted_loss_pct: float | None = None
    budgeted_loss_kg: float | None = None
    actual_loss_kg: float | None = None
    remarks: str | None = None


class LossReconciliationRequest(BaseModel):
    entries: list[LossEntry]


class RemarkRequest(BaseModel):
    remark_type: str  # observation, deviation, corrective_action
    content: str
    recorded_by: str | None = None


@router.post("/job-cards/{job_card_id}/environment")
async def add_environment(request: Request, job_card_id: int, body: EnvironmentRequest):
    """Record Annexure C — environmental parameters."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        for p in body.parameters:
            await conn.execute(
                "INSERT INTO job_card_environment (job_card_id, parameter_name, value) VALUES ($1, $2, $3)",
                job_card_id, p.parameter_name, p.value,
            )
    return {"saved": len(body.parameters)}


@router.post("/job-cards/{job_card_id}/metal-detection")
async def add_metal_detection(request: Request, job_card_id: int, body: MetalDetectionRequest):
    """Record Annexure A/B — metal detection validation."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        det_id = await conn.fetchval(
            """
            INSERT INTO job_card_metal_detection (
                job_card_id, check_type, fe_pass, nfe_pass, ss_pass, failed_units, remarks,
                seal_check, seal_failed_units, wt_check, wt_failed_units,
                dough_temp_c, oven_temp_c, baking_temp_c
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            RETURNING detection_id
            """,
            job_card_id, body.check_type, body.fe_pass, body.nfe_pass, body.ss_pass,
            body.failed_units, body.remarks,
            body.seal_check, body.seal_failed_units, body.wt_check, body.wt_failed_units,
            body.dough_temp_c, body.oven_temp_c, body.baking_temp_c,
        )
    return {"detection_id": det_id}


@router.post("/job-cards/{job_card_id}/weight-checks")
async def add_weight_checks(request: Request, job_card_id: int, body: WeightCheckRequest):
    """Record Annexure B — 20-sample weight/leak checks."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        for s in body.samples:
            await conn.execute(
                """
                INSERT INTO job_card_weight_check (
                    job_card_id, sample_number, net_weight, gross_weight, leak_test_pass,
                    target_wt_g, tolerance_g, accept_range_min, accept_range_max
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                job_card_id, s.sample_number, s.net_weight, s.gross_weight, s.leak_test_pass,
                body.target_wt_g, body.tolerance_g, body.accept_range_min, body.accept_range_max,
            )
    return {"saved": len(body.samples)}


@router.post("/job-cards/{job_card_id}/loss-reconciliation")
async def add_loss_reconciliation(request: Request, job_card_id: int, body: LossReconciliationRequest):
    """Record Annexure D — loss reconciliation."""
    pool = request.app.state.db_pool
    total_budgeted = 0
    total_actual = 0
    async with pool.acquire() as conn:
        for e in body.entries:
            budgeted = e.budgeted_loss_kg or 0
            actual = e.actual_loss_kg or 0
            variance = actual - budgeted
            await conn.execute(
                """
                INSERT INTO job_card_loss_reconciliation (
                    job_card_id, loss_category, budgeted_loss_pct, budgeted_loss_kg,
                    actual_loss_kg, variance_kg, remarks
                ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                job_card_id, e.loss_category, e.budgeted_loss_pct,
                budgeted, actual, variance, e.remarks,
            )
            total_budgeted += budgeted
            total_actual += actual
    return {"saved": len(body.entries), "total_budgeted_kg": round(total_budgeted, 3), "total_actual_kg": round(total_actual, 3)}


@router.post("/job-cards/{job_card_id}/remarks")
async def add_remarks(request: Request, job_card_id: int, body: RemarkRequest):
    """Record Annexure E — remarks & deviations."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        remark_id = await conn.fetchval(
            "INSERT INTO job_card_remarks (job_card_id, remark_type, content, recorded_by) VALUES ($1,$2,$3,$4) RETURNING remark_id",
            job_card_id, body.remark_type, body.content, body.recorded_by,
        )
    return {"remark_id": remark_id}


# ---------------------------------------------------------------------------
# Floor Inventory endpoints
# ---------------------------------------------------------------------------


class MoveRequest(BaseModel):
    sku_name: str
    from_location: str
    to_location: str
    quantity_kg: float
    entity: str
    reason: str | None = None
    job_card_id: int | None = None
    moved_by: str | None = None


@router.get("/floor-inventory")
async def list_floor_inventory(
    request: Request,
    entity: str = Query(...),
    floor_location: str = Query(None),
    search: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """List floor inventory items with filters."""
    from app.modules.production.services.floor_tracker import get_floor_detail
    pool = request.app.state.db_pool
    loc = floor_location or 'rm_store'
    async with pool.acquire() as conn:
        if floor_location:
            return await get_floor_detail(conn, floor_location, entity, search, page, page_size)
        # All locations
        conditions = ["entity = $1", "quantity_kg > 0"]
        params = [entity]
        idx = 2
        if search:
            conditions.append(f"sku_name ILIKE ${idx}"); params.append(f"%{search}%"); idx += 1
        where = " AND ".join(conditions)
        offset = (page - 1) * page_size
        total = await conn.fetchval(f"SELECT COUNT(*) FROM floor_inventory WHERE {where}", *params)
        rows = await conn.fetch(
            f"SELECT * FROM floor_inventory WHERE {where} ORDER BY floor_location, quantity_kg DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, page_size, offset,
        )
        return {
            "results": [dict(r) for r in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total,
                           "total_pages": (total + page_size - 1) // page_size if total else 0},
        }


@router.get("/floor-inventory/summary")
async def floor_summary(request: Request, entity: str = Query(...)):
    """Aggregated stock per floor location."""
    from app.modules.production.services.floor_tracker import get_floor_summary
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_floor_summary(conn, entity)


class InventorySeedItem(BaseModel):
    sku_name: str
    item_type: str          # rm, pm, fg, wip
    floor_location: str     # rm_store, pm_store, fg_store, production_floor
    quantity_kg: float
    uom: str = "kg"
    lot_number: str | None = None


class InventorySeedRequest(BaseModel):
    entity: str
    items: list[InventorySeedItem]
    overwrite: bool = False  # if True, SET quantity; if False, ADD to existing


@router.post("/floor-inventory/seed")
async def seed_floor_inventory(request: Request, body: InventorySeedRequest):
    """Manually seed opening stock for PM/FG or any store that wasn't in the Excel ingest.
    overwrite=false adds to existing qty; overwrite=true sets it absolutely."""
    pool = request.app.state.db_pool
    inserted = updated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in body.items:
                if body.overwrite:
                    result = await conn.execute(
                        """
                        INSERT INTO floor_inventory (sku_name, item_type, floor_location, quantity_kg, uom, lot_number, entity)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (sku_name, floor_location, lot_number, entity)
                        DO UPDATE SET quantity_kg = $4, uom = $5, last_updated = NOW()
                        """,
                        item.sku_name, item.item_type, item.floor_location,
                        item.quantity_kg, item.uom, item.lot_number or '', body.entity,
                    )
                else:
                    result = await conn.execute(
                        """
                        INSERT INTO floor_inventory (sku_name, item_type, floor_location, quantity_kg, uom, lot_number, entity)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (sku_name, floor_location, lot_number, entity)
                        DO UPDATE SET quantity_kg = floor_inventory.quantity_kg + $4, uom = $5, last_updated = NOW()
                        """,
                        item.sku_name, item.item_type, item.floor_location,
                        item.quantity_kg, item.uom, item.lot_number or '', body.entity,
                    )
                if 'INSERT 0 1' in result:
                    inserted += 1
                else:
                    updated += 1
    return {"inserted": inserted, "updated": updated, "total": len(body.items)}


@router.post("/floor-inventory/move")
async def move_material_endpoint(request: Request, body: MoveRequest):
    """Manual material movement between floors."""
    from app.modules.production.services.floor_tracker import move_material
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await move_material(
                    conn, body.sku_name, body.from_location, body.to_location,
                    body.quantity_kg, body.entity,
                    reason=body.reason, job_card_id=body.job_card_id, moved_by=body.moved_by,
                )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/floor-inventory/movements")
async def movement_history(
    request: Request,
    entity: str = Query(...),
    sku_name: str = Query(None),
    from_location: str = Query(None),
    to_location: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    job_card_id: int = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """Movement audit trail with filters."""
    from app.modules.production.services.floor_tracker import get_movement_history
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_movement_history(
            conn, entity, sku_name=sku_name, from_location=from_location,
            to_location=to_location, date_from=date_from, date_to=date_to,
            job_card_id=job_card_id, page=page, page_size=page_size,
        )


@router.post("/floor-inventory/check-idle")
async def check_idle(request: Request, entity: str = Query(...)):
    """Trigger idle material check. Creates alerts for materials idle 3-5 days."""
    from app.modules.production.services.idle_checker import check_idle_materials
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await check_idle_materials(conn, entity)


# ---------------------------------------------------------------------------
# Off-Grade endpoints
# ---------------------------------------------------------------------------


class OffgradeRuleCreate(BaseModel):
    source_item_group: str
    target_item_group: str
    max_substitution_pct: float
    notes: str | None = None


class OffgradeRuleUpdate(BaseModel):
    max_substitution_pct: float | None = None
    is_active: bool | None = None
    notes: str | None = None


@router.get("/offgrade/inventory")
async def list_offgrade(
    request: Request,
    entity: str = Query(None),
    status: str = Query("available"),
    item_group: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """List off-grade inventory."""
    pool = request.app.state.db_pool
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status = ${idx}"); params.append(status); idx += 1
    if item_group:
        conditions.append(f"item_group ILIKE ${idx}"); params.append(f"%{item_group}%"); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM offgrade_inventory WHERE {where}", *params)
        rows = await conn.fetch(
            f"SELECT * FROM offgrade_inventory WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, page_size, offset,
        )
    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "total_pages": (total + page_size - 1) // page_size if total else 0},
    }


@router.get("/offgrade/rules")
async def list_offgrade_rules(request: Request):
    """List all off-grade reuse rules."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM offgrade_reuse_rule ORDER BY source_item_group, target_item_group")
    return [dict(r) for r in rows]


@router.post("/offgrade/rules/create")
async def create_offgrade_rule(request: Request, body: OffgradeRuleCreate):
    """Create an off-grade reuse rule."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rule_id = await conn.fetchval(
            """
            INSERT INTO offgrade_reuse_rule (source_item_group, target_item_group, max_substitution_pct, notes)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (source_item_group, target_item_group) DO UPDATE SET
                max_substitution_pct = $3, notes = $4
            RETURNING rule_id
            """,
            body.source_item_group, body.target_item_group, body.max_substitution_pct, body.notes,
        )
    return {"rule_id": rule_id}


@router.put("/offgrade/rules/{rule_id}")
async def update_offgrade_rule(request: Request, rule_id: int, body: OffgradeRuleUpdate):
    """Update an off-grade reuse rule."""
    pool = request.app.state.db_pool
    sent = body.model_fields_set
    updates = []
    params = []
    idx = 1
    for field in ['max_substitution_pct', 'is_active', 'notes']:
        if field in sent:
            updates.append(f"{field} = ${idx}"); params.append(getattr(body, field)); idx += 1
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    params.append(rule_id)
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"UPDATE offgrade_reuse_rule SET {', '.join(updates)} WHERE rule_id = ${idx}", *params,
        )
        if result == 'UPDATE 0':
            raise HTTPException(status_code=404, detail="Rule not found")
    return {"rule_id": rule_id, "updated": True}


# ---------------------------------------------------------------------------
# Process Loss & Yield endpoints
# ---------------------------------------------------------------------------


@router.get("/loss/analysis")
async def loss_analysis(
    request: Request,
    entity: str = Query(None),
    product_name: str = Query(None),
    stage: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    group_by: str = Query("product"),  # product, stage, month
):
    """Loss analysis with aggregation."""
    pool = request.app.state.db_pool
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if product_name:
        conditions.append(f"product_name ILIKE ${idx}"); params.append(f"%{product_name}%"); idx += 1
    if stage:
        conditions.append(f"stage = ${idx}"); params.append(stage); idx += 1
    if date_from:
        conditions.append(f"production_date >= ${idx}::date"); params.append(date_from); idx += 1
    if date_to:
        conditions.append(f"production_date <= ${idx}::date"); params.append(date_to); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"

    group_col = {
        "product": "product_name",
        "stage": "stage",
        "month": "TO_CHAR(production_date, 'YYYY-MM')",
        "machine": "machine_name",
    }.get(group_by, "product_name")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {group_col} AS group_key,
                   COUNT(*) AS batch_count,
                   ROUND(AVG(loss_pct)::numeric, 3) AS avg_loss_pct,
                   ROUND(SUM(loss_kg)::numeric, 3) AS total_loss_kg,
                   ROUND(MIN(loss_pct)::numeric, 3) AS min_loss_pct,
                   ROUND(MAX(loss_pct)::numeric, 3) AS max_loss_pct
            FROM process_loss WHERE {where}
            GROUP BY {group_col}
            ORDER BY SUM(loss_kg) DESC
            """,
            *params,
        )
    return [dict(r) for r in rows]


@router.get("/loss/anomalies")
async def loss_anomalies(
    request: Request,
    entity: str = Query(None),
    threshold_multiplier: float = Query(2.0),
):
    """Batches with loss significantly above average (default: 2x avg)."""
    pool = request.app.state.db_pool
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"p.entity = ${idx}"); params.append(entity); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            WITH stats AS (
                SELECT product_name, stage,
                       AVG(loss_pct) AS avg_pct, STDDEV(loss_pct) AS std_pct
                FROM process_loss WHERE {where.replace('p.', '')}
                GROUP BY product_name, stage
            )
            SELECT p.*, s.avg_pct, s.std_pct
            FROM process_loss p
            JOIN stats s ON p.product_name = s.product_name AND p.stage = s.stage
            WHERE {where} AND p.loss_pct > s.avg_pct * ${idx}
            ORDER BY (p.loss_pct - s.avg_pct) DESC
            LIMIT 50
            """,
            *params, threshold_multiplier,
        )
    return [dict(r) for r in rows]


@router.get("/yield/summary")
async def yield_summary(
    request: Request,
    entity: str = Query(None),
    product_name: str = Query(None),
    period: str = Query(None),
):
    """Yield summary by product/period."""
    pool = request.app.state.db_pool
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if product_name:
        conditions.append(f"product_name ILIKE ${idx}"); params.append(f"%{product_name}%"); idx += 1
    if period:
        conditions.append(f"period = ${idx}"); params.append(period); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM yield_summary WHERE {where} ORDER BY computed_at DESC LIMIT 100",
            *params,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Day-End endpoints
# ---------------------------------------------------------------------------


class DispatchItem(BaseModel):
    job_card_id: int
    dispatch_qty: float


class BulkDispatchRequest(BaseModel):
    dispatches: list[DispatchItem]
    entity: str


class ScanLineItem(BaseModel):
    sku_name: str
    item_type: str | None = None
    scanned_qty_kg: float
    scanned_box_ids: list[str] | None = None
    variance_reason: str | None = None


class BalanceScanSubmitRequest(BaseModel):
    floor_location: str
    entity: str
    submitted_by: str
    scan_lines: list[ScanLineItem]


class ReconcileRequest(BaseModel):
    reviewed_by: str


class FulfillmentCancelRequest(BaseModel):
    fulfillment_ids: list[int]
    reason: str
    cancelled_by: str = ""


@router.get("/day-end/summary")
async def day_end_summary(
    request: Request,
    entity: str = Query(...),
    target_date: str = Query(None),
):
    """Today's completed final-stage job cards with dispatch data."""
    from app.modules.production.services.day_end import get_day_end_summary
    pool = request.app.state.db_pool
    d = date.fromisoformat(target_date) if target_date else None
    async with pool.acquire() as conn:
        return await get_day_end_summary(conn, entity, d)


@router.put("/day-end/dispatch")
async def day_end_dispatch(request: Request, body: BulkDispatchRequest):
    """Bulk update dispatch quantities for completed job cards."""
    from app.modules.production.services.day_end import bulk_dispatch
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await bulk_dispatch(conn, [d.model_dump() for d in body.dispatches], body.entity)


# ---------------------------------------------------------------------------
# Balance Scan endpoints
# ---------------------------------------------------------------------------


@router.post("/balance-scan/submit")
async def submit_scan(request: Request, body: BalanceScanSubmitRequest):
    """Submit a day-end balance scan for a floor."""
    from app.modules.production.services.day_end import submit_balance_scan
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await submit_balance_scan(
                conn, body.floor_location, body.entity, body.submitted_by,
                [sl.model_dump() for sl in body.scan_lines],
            )


@router.get("/balance-scan/status")
async def scan_status(
    request: Request,
    entity: str = Query(...),
    target_date: str = Query(None),
):
    """Today's scan submission status per floor."""
    from app.modules.production.services.day_end import get_scan_status
    pool = request.app.state.db_pool
    d = date.fromisoformat(target_date) if target_date else None
    async with pool.acquire() as conn:
        return await get_scan_status(conn, entity, d)


@router.get("/balance-scan/{scan_id}")
async def scan_detail(request: Request, scan_id: int):
    """Get balance scan detail with all line items."""
    from app.modules.production.services.day_end import get_scan_detail
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_scan_detail(conn, scan_id)
    if not result:
        raise HTTPException(status_code=404, detail="Scan not found")
    return result


@router.put("/balance-scan/{scan_id}/reconcile")
async def reconcile_scan_endpoint(request: Request, scan_id: int, body: ReconcileRequest):
    """Reconcile a balance scan — adjust floor_inventory to match physical count."""
    from app.modules.production.services.day_end import reconcile_scan
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await reconcile_scan(conn, scan_id, body.reviewed_by)
    if "error" in result:
        if result["error"] == "not_found":
            raise HTTPException(status_code=404, detail="Scan not found")
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.post("/balance-scan/check-missing")
async def check_missing(request: Request, entity: str = Query(...), target_date: str = Query(None)):
    """Check which floors haven't submitted balance scans. Creates alerts."""
    from app.modules.production.services.day_end import check_missing_scans
    pool = request.app.state.db_pool
    d = date.fromisoformat(target_date) if target_date else None
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await check_missing_scans(conn, entity, d)


# ---------------------------------------------------------------------------
# FY Cancel endpoint (complements existing carryforward/revise)
# ---------------------------------------------------------------------------


@router.post("/fulfillment/cancel")
async def cancel_fulfillment(request: Request, body: FulfillmentCancelRequest):
    """Cancel selected fulfillment records with reason."""
    pool = request.app.state.db_pool
    cancelled = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for fid in body.fulfillment_ids:
                old = await conn.fetchrow("SELECT order_status FROM so_fulfillment WHERE fulfillment_id = $1", fid)
                if not old or old['order_status'] in ('cancelled', 'fulfilled'):
                    continue
                await conn.execute(
                    "UPDATE so_fulfillment SET order_status = 'cancelled', updated_at = NOW() WHERE fulfillment_id = $1",
                    fid,
                )
                await conn.execute(
                    """
                    INSERT INTO so_revision_log (fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
                    VALUES ($1, 'cancel', $2, 'cancelled', $3, $4)
                    """,
                    fid, old['order_status'], body.reason, body.cancelled_by,
                )
                cancelled += 1
    return {"cancelled": cancelled, "total_requested": len(body.fulfillment_ids)}


@router.get("/plans/{plan_id}/revision-history")
async def revision_history(request: Request, plan_id: int):
    """Get the chain of revisions for a plan."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # Walk the chain backwards from this plan
        chain = []
        current_id = plan_id
        while current_id:
            plan = await conn.fetchrow(
                "SELECT plan_id, plan_name, revision_number, status, previous_plan_id, created_at FROM production_plan WHERE plan_id = $1",
                current_id,
            )
            if not plan:
                break
            chain.append(dict(plan))
            current_id = plan['previous_plan_id']

        # Also walk forward (find plans that reference this one)
        forward_id = plan_id
        while True:
            next_plan = await conn.fetchrow(
                "SELECT plan_id, plan_name, revision_number, status, previous_plan_id, created_at FROM production_plan WHERE previous_plan_id = $1",
                forward_id,
            )
            if not next_plan:
                break
            chain.insert(0, dict(next_plan))
            forward_id = next_plan['plan_id']

    # Deduplicate and sort by revision_number
    seen = set()
    unique = []
    for p in chain:
        if p['plan_id'] not in seen:
            seen.add(p['plan_id'])
            unique.append(p)
    unique.sort(key=lambda x: x.get('revision_number') or 0)

    return {"plan_id": plan_id, "revision_chain": unique}


# ---------------------------------------------------------------------------
# Discrepancy endpoints
# ---------------------------------------------------------------------------


class DiscrepancyReportRequest(BaseModel):
    discrepancy_type: str
    severity: str = "major"
    affected_material: str | None = None
    affected_machine_id: int | None = None
    details: str | None = None
    reported_by: str | None = None
    entity: str


class DiscrepancyResolveRequest(BaseModel):
    resolution_type: str  # material_substituted, machine_rescheduled, deferred, cancelled_replanned, proceed_with_deviation
    resolution_details: str
    resolved_by: str


@router.post("/discrepancy/report")
async def report_discrepancy_endpoint(request: Request, body: DiscrepancyReportRequest):
    """Report an internal discrepancy. Auto-holds affected job cards."""
    from app.modules.production.services.discrepancy_manager import report_discrepancy
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await report_discrepancy(
                    conn, discrepancy_type=body.discrepancy_type, severity=body.severity,
                    affected_material=body.affected_material, affected_machine_id=body.affected_machine_id,
                    details=body.details, reported_by=body.reported_by, entity=body.entity,
                )
    return result


@router.get("/discrepancy")
async def list_discrepancies(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    discrepancy_type: str = Query(None),
    severity: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """List discrepancy reports with filters."""
    pool = request.app.state.db_pool
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status = ${idx}"); params.append(status); idx += 1
    if discrepancy_type:
        conditions.append(f"discrepancy_type = ${idx}"); params.append(discrepancy_type); idx += 1
    if severity:
        conditions.append(f"severity = ${idx}"); params.append(severity); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM discrepancy_report WHERE {where}", *params)
        rows = await conn.fetch(
            f"SELECT * FROM discrepancy_report WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, page_size, offset,
        )
    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "total_pages": (total + page_size - 1) // page_size if total else 0},
    }


@router.get("/discrepancy/{discrepancy_id}")
async def get_discrepancy(request: Request, discrepancy_id: int):
    """Get discrepancy detail with affected job cards."""
    from app.modules.production.services.discrepancy_manager import get_discrepancy_detail
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_discrepancy_detail(conn, discrepancy_id)
    if not result:
        raise HTTPException(status_code=404, detail="Discrepancy not found")
    return result


@router.put("/discrepancy/{discrepancy_id}/resolve")
async def resolve_discrepancy_endpoint(request: Request, discrepancy_id: int, body: DiscrepancyResolveRequest):
    """Resolve a discrepancy with one of 5 resolution types."""
    from app.modules.production.services.discrepancy_manager import resolve_discrepancy
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        disc = await conn.fetchrow("SELECT entity FROM discrepancy_report WHERE discrepancy_id = $1", discrepancy_id)
        if not disc:
            raise HTTPException(status_code=404, detail="Discrepancy not found")
        async with conn.transaction():
            result = await resolve_discrepancy(
                conn, discrepancy_id,
                resolution_type=body.resolution_type,
                resolution_details=body.resolution_details,
                resolved_by=body.resolved_by,
                entity=disc['entity'],
            )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


# ---------------------------------------------------------------------------
# AI Insights endpoints
# ---------------------------------------------------------------------------


class AIFeedbackRequest(BaseModel):
    status: str  # accepted, rejected
    feedback: str | None = None


@router.get("/ai/recommendations")
async def list_ai_recommendations(
    request: Request,
    entity: str = Query(None),
    recommendation_type: str = Query(None),
    status: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    """List all AI recommendations."""
    pool = request.app.state.db_pool
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if recommendation_type:
        conditions.append(f"recommendation_type = ${idx}"); params.append(recommendation_type); idx += 1
    if status:
        conditions.append(f"status = ${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM ai_recommendation WHERE {where}", *params)
        rows = await conn.fetch(
            f"""SELECT recommendation_id, recommendation_type, entity, tokens_used, latency_ms,
                       model_used, status, feedback, plan_id, created_at
                FROM ai_recommendation WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}""",
            *params, page_size, offset,
        )
    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "total_pages": (total + page_size - 1) // page_size if total else 0},
    }


@router.put("/ai/recommendations/{rec_id}/feedback")
async def ai_feedback(request: Request, rec_id: int, body: AIFeedbackRequest):
    """Accept or reject an AI recommendation with feedback."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE ai_recommendation SET status = $2, feedback = $3 WHERE recommendation_id = $1",
            rec_id, body.status, body.feedback,
        )
        if result == 'UPDATE 0':
            raise HTTPException(status_code=404, detail="Recommendation not found")
    return {"recommendation_id": rec_id, "status": body.status}


# ═══════════════════════════════════════════════════════════════════════════
#  PRODUCTION INDENTS (FG/SFG) — Section A2
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/production-indents")
async def list_production_indents(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    search: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    from app.modules.production.services.production_indent_service import list_production_indents as _list
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await _list(conn, entity=entity, status=status, search=search,
                           date_from=date_from, date_to=date_to,
                           page=page, page_size=page_size)


@router.get("/production-indents/{indent_id}")
async def get_production_indent(request: Request, indent_id: str):
    from app.modules.production.services.production_indent_service import get_production_indent as _get
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await _get(conn, indent_id)
        if not result:
            raise HTTPException(status_code=404, detail="Production indent not found")
        return result


class ProductionIndentCreate(BaseModel):
    item_description: str
    material_type: str = "FG"
    uom: str = "kg"
    required_qty: float
    available_qty: float = 0
    shortfall_qty: float = 0
    triggered_by_job_card: str | None = None
    triggered_by_so: str | None = None
    customer_name: str | None = None
    maker_user: str
    status: str = "draft"
    entity: str = "cfpl"


@router.post("/production-indents")
async def create_production_indent(request: Request, body: ProductionIndentCreate):
    from app.modules.production.services.production_indent_service import create_production_indent as _create
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await _create(conn, **body.model_dump())
            if result.get("duplicate"):
                raise HTTPException(status_code=409, detail=result["error"])
            return result


@router.put("/production-indents/{indent_id}/submit")
async def submit_production_indent(request: Request, indent_id: str):
    from app.modules.production.services.production_indent_service import submit_indent
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await submit_indent(conn, indent_id)


class CheckerAction(BaseModel):
    checker_user: str
    checker_comment: str = ""


@router.put("/production-indents/{indent_id}/approve")
async def approve_production_indent(request: Request, indent_id: str, body: CheckerAction):
    from app.modules.production.services.production_indent_service import approve_indent
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await approve_indent(conn, indent_id, checker_user=body.checker_user,
                                     checker_comment=body.checker_comment)


@router.put("/production-indents/{indent_id}/return")
async def return_production_indent(request: Request, indent_id: str, body: CheckerAction):
    from app.modules.production.services.production_indent_service import return_indent
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await return_indent(conn, indent_id, checker_user=body.checker_user,
                                    checker_comment=body.checker_comment)


class CancelBody(BaseModel):
    cancel_reason: str


@router.put("/production-indents/{indent_id}/cancel")
async def cancel_production_indent(request: Request, indent_id: str, body: CancelBody):
    from app.modules.production.services.production_indent_service import cancel_indent
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await cancel_indent(conn, indent_id, cancel_reason=body.cancel_reason)


@router.post("/production-indents/{indent_id}/create-internal-order")
async def create_internal_order(request: Request, indent_id: str):
    from app.modules.production.services.production_indent_service import create_internal_order as _create
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await _create(conn, indent_id)
            if result.get("error"):
                raise HTTPException(status_code=400, detail=result["error"])
            return result


# ═══════════════════════════════════════════════════════════════════════════
#  LOT PICKER / ISSUANCE — Sections C4, D2-D4
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/lots")
async def get_lots(
    request: Request,
    item_description: str = Query(""),
    warehouse: str = Query(None),
    job_card_id: str = Query(None),
    entity: str = Query("cfpl"),
):
    from app.modules.production.services.lot_issuance_service import get_lots as _get
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await _get(conn, item_description=item_description,
                          warehouse=warehouse, job_card_id=job_card_id, entity=entity)


@router.get("/lots/other-warehouses")
async def get_lots_other_warehouses(
    request: Request,
    item_description: str = Query(""),
    exclude_warehouse: str = Query(None),
    entity: str = Query("cfpl"),
):
    from app.modules.production.services.lot_issuance_service import get_lots_other_warehouses as _get
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await _get(conn, item_description=item_description,
                          exclude_warehouse=exclude_warehouse, entity=entity)


class FifoSkipBody(BaseModel):
    batch_id: str
    job_card_id: str | None = None
    reason: str
    detail: str | None = None
    disposition: str = "leave_available"
    block_for_so: str | None = None
    skipped_by: str


@router.post("/lots/fifo-skip")
async def fifo_skip(request: Request, body: FifoSkipBody):
    from app.modules.production.services.lot_issuance_service import record_fifo_skip
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await record_fifo_skip(conn, **body.model_dump())


class ForceAssignBody(BaseModel):
    batch_id: str
    new_so_id: str
    override_comment: str
    force_assigned_by: str


@router.post("/lots/force-assign")
async def force_assign(request: Request, body: ForceAssignBody):
    from app.modules.production.services.lot_issuance_service import force_assign_lot
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await force_assign_lot(conn, **body.model_dump())


@router.get("/boxes/{box_id}")
async def get_box(request: Request, box_id: str):
    from app.modules.production.services.lot_issuance_service import get_box as _get
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await _get(conn, box_id)
        if not result:
            raise HTTPException(status_code=404, detail="Box not found")
        return result


class ScanIdentifyBody(BaseModel):
    value: str  # raw QR contents: JSON {"tx","bi"} or a bare box id


@router.post("/scan-identify")
async def scan_identify(request: Request, body: ScanIdentifyBody):
    """Universal box identify: which table does this scanned box belong to."""
    from app.modules.production.services.box_identify_service import identify_box
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await identify_box(conn, body.value)


class IssueNoteLine(BaseModel):
    bom_line_id: str | None = None
    sku: str | None = None
    material_type: str | None = None
    lot_number: str | None = None
    lot_id: str | None = None
    tr_number: str | None = None
    warehouse: str | None = None
    net_wt_issued: float = 0
    qty_cartons: int | None = None
    box_id: str | None = None
    fifo_skipped: bool = False
    skip_reason: str | None = None


class IssueNoteCreate(BaseModel):
    job_card_id: str
    so_id: str | None = None
    customer_name: str | None = None
    bom_line_id: str | None = None
    issued_by: str
    status: str = "confirmed"
    lines: list[IssueNoteLine]


@router.post("/issue-notes")
async def create_issue_note(request: Request, body: IssueNoteCreate):
    from app.modules.production.services.lot_issuance_service import create_issue_note as _create
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _create(
                conn,
                job_card_id=body.job_card_id, so_id=body.so_id,
                customer_name=body.customer_name, bom_line_id=body.bom_line_id,
                issued_by=body.issued_by, status=body.status,
                lines=[l.model_dump() for l in body.lines],
            )


class RaiseIndentBody(BaseModel):
    material_sku_name: str
    item_category: str | None = None
    material_type: str
    required_qty_kg: float
    uom: str = "kg"
    job_card_id: str | None = None
    so_reference: str | None = None
    customer_name: str | None = None
    trigger_reason: str = "Insufficient stock"
    entity: str = "cfpl"


@router.post("/indents/raise")
async def raise_indent(request: Request, body: RaiseIndentBody):
    from app.modules.production.services.lot_issuance_service import raise_purchase_indent
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await raise_purchase_indent(conn, **body.model_dump())
    if result.get("indent_id"):
        jc_id_int = int(body.job_card_id) if body.job_card_id else None
        await events.indent_raised(body.entity or "cfpl", indent_id=result["indent_id"], material=body.material_sku_name, qty_kg=body.required_qty_kg, source="floor", job_card_id=jc_id_int)
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  QC DASHBOARD — Section G1
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/qc/queue")
async def qc_queue(request: Request):
    from app.modules.production.services.qc_service import get_qc_queue
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_qc_queue(conn)


class QCInspectionBody(BaseModel):
    result: str
    findings: str | None = None
    corrective_action: str | None = None
    inspector_user: str


@router.put("/qc/inspections/{inspection_id}")
async def submit_qc_inspection(request: Request, inspection_id: str, body: QCInspectionBody):
    from app.modules.production.services.qc_service import submit_inspection
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                result = await submit_inspection(conn, inspection_id, **body.model_dump())
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  RTV DISPOSITION — Sections H1-H4
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/rtv/dispositions")
async def list_rtv_dispositions(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
):
    from app.modules.production.services.rtv_disposition_service import list_dispositions
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await list_dispositions(conn, entity=entity, status=status)


class RtvDispositionBody(BaseModel):
    rtv_id: str
    disposition_type: str
    decided_by: str
    qc_remarks: str | None = None
    business_head: str | None = None  # key from BUSINESS_HEADS registry


@router.post("/rtv/dispositions")
async def assign_rtv_disposition(request: Request, body: RtvDispositionBody):
    from app.modules.production.services.rtv_disposition_service import assign_disposition
    from app.modules.production.services.mail_service import BUSINESS_HEADS
    if body.business_head and body.business_head not in BUSINESS_HEADS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid business_head; must be one of {sorted(BUSINESS_HEADS)}",
        )
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await assign_disposition(conn, **body.model_dump())


class DiscardBody(BaseModel):
    rtv_id: str
    reason: str
    authorised_by: str
    business_head: str | None = None  # overrides stored business_head if provided


@router.post("/rtv/discard")
async def approve_discard(request: Request, body: DiscardBody):
    from app.modules.production.services.rtv_disposition_service import approve_discard
    from app.modules.production.services.mail_service import BUSINESS_HEADS
    if body.business_head and body.business_head not in BUSINESS_HEADS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid business_head; must be one of {sorted(BUSINESS_HEADS)}",
        )
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await approve_discard(conn, **body.model_dump())
            if result.get("error"):
                raise HTTPException(status_code=400, detail=result["error"])
            return result


# ═══════════════════════════════════════════════════════════════════════════
#  AMENDMENT TRACKING — Section I2
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/amendments")
async def get_amendments(
    request: Request,
    record_id: str = Query(...),
    record_type: str = Query(...),
    field: str = Query(None),
):
    from app.modules.production.services.amendment_service import get_amendments as _get
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await _get(conn, record_id=record_id, record_type=record_type, field=field)


@router.get("/amendments/count")
async def get_amendment_count(
    request: Request,
    record_id: str = Query(...),
    record_type: str = Query(...),
):
    from app.modules.production.services.amendment_service import get_amendment_count as _count
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await _count(conn, record_id=record_id, record_type=record_type)


# ═══════════════════════════════════════════════════════════════════════════
#  MATERIAL DOCUMENTS — SAP MIGO equivalent (source of truth)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/material-documents")
async def list_material_documents(
    request: Request,
    reference_type: str = Query(None),
    reference_id: str = Query(None),
    movement_type: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1
        if reference_type:
            conditions.append(f"md.reference_type = ${idx}")
            params.append(reference_type)
            idx += 1
        if reference_id:
            conditions.append(f"md.reference_id = ${idx}")
            params.append(reference_id)
            idx += 1
        if movement_type:
            conditions.append(f"md.movement_type = ${idx}")
            params.append(movement_type)
            idx += 1
        if date_from:
            conditions.append(f"md.posting_date >= ${idx}::date")
            params.append(date_from)
            idx += 1
        if date_to:
            conditions.append(f"md.posting_date <= ${idx}::date")
            params.append(date_to)
            idx += 1
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = await conn.fetchval(f"SELECT COUNT(*) FROM material_document md{where}", *params)
        rows = await conn.fetch(f"""
            SELECT md.*, array_agg(json_build_object(
                'line', ml.line_number, 'sku', ml.sku_name, 'batch', ml.batch_id,
                'qty', ml.quantity_kg, 'from', ml.from_location, 'to', ml.to_location
            ) ORDER BY ml.line_number) AS lines
            FROM material_document md
            LEFT JOIN material_document_line ml ON md.mat_doc_id = ml.mat_doc_id
            {where}
            GROUP BY md.id ORDER BY md.created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
        """, *params, page_size, (page - 1) * page_size)
        return {
            "results": [dict(r) for r in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total,
                           "total_pages": max(1, -(-total // page_size))},
        }


@router.get("/material-documents/{mat_doc_id}/reconcile")
async def reconcile_batch_doc(request: Request, mat_doc_id: str):
    """Reconcile a batch quantity against material documents."""
    from app.modules.production.services.material_document_service import reconcile_batch
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # Get batch_id from the document
        batch_id = await conn.fetchval(
            "SELECT batch_id FROM material_document_line WHERE mat_doc_id = $1 LIMIT 1",
            mat_doc_id
        )
        if not batch_id:
            raise HTTPException(status_code=404, detail="Document not found")
        return await reconcile_batch(conn, batch_id)


@router.get("/movement-types")
async def list_movement_types(request: Request):
    """List all SAP-aligned movement types."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM movement_type_ref ORDER BY movement_type")
        return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
#  JOB CARD OUTPUT v2  — consolidated output / byproduct / balance / QC
# ═══════════════════════════════════════════════════════════════════════════

class ByproductLineV2(BaseModel):
    category: str                   # tukda | offgrade | without_shell | empty_shells | other
    qty_kg: float
    uom: str = "kg"
    remarks: str | None = None
    # Migration 034 — article attribution. Both optional: control_sample,
    # pm_*, dust etc. don't carry an article. Off-grade rows DO. Without
    # these fields on the model, Pydantic silently dropped them on parse
    # and the operator's article selection vanished on save.
    material_name: str | None = None
    bom_line_id:   int | None = None


class RmConsumptionLineV2(BaseModel):
    """Consumption of a single BOM RM line for this job card."""
    bom_line_id: int | None = None      # FK to bom_line — preferred handle
    material_sku_name: str              # required; matches BOM line label
    consumed_qty_kg: float = Field(ge=0)
    uom: str = "kg"
    remarks: str | None = None


class BalanceMaterialV2(BaseModel):
    bom_line_id: int | None = None      # FK to bom_line — links balance to BOM row
    material_id: int | None = None
    material_name: str
    balance_type: str               # extra_given | returned | wastage | control_sample
    qty_kg: float
    remarks: str | None = None


class AdditiveLineV2(BaseModel):
    """Data-keeping additive consumption row.  Either `sku_name` or
    `material_name` must be set; both null is rejected by the table's
    CHECK constraint (and gracefully dropped by save_additives with a
    logger.warning so a partial upload doesn't 500 the save)."""
    sku_name:      str | None = None    # from all_sku dropdown
    material_name: str | None = None    # free-text "Others" path
    qty_kg:        float
    remarks:       str | None = None


class QCDataV2(BaseModel):
    passed: bool
    remarks: str | None = None
    corrective_action: str | None = None
    inspector: str | None = None


class JobCardOutputV2Request(BaseModel):
    fg_actual_kg: float | None = None
    fg_actual_units: int | None = None
    fg_expected_kg: float | None = None
    fg_expected_units: int | None = None
    rm_consumed: list[RmConsumptionLineV2] = []   # per-BOM-line consumption (preferred)
    rm_consumed_kg: float | None = None           # legacy scalar — engine falls back to this when rm_consumed is empty
    process_loss_kg: float = 0.0
    byproducts: list[ByproductLineV2] = []
    balance_materials: list[BalanceMaterialV2] = []
    qc: QCDataV2 | None = None


@router.post("/job-cards/{job_card_id}/output")
async def record_output_v2(request: Request, job_card_id: int, body: JobCardOutputV2Request):
    """V2 consolidated: record FG output, byproducts, balance materials, and QC in one atomic call."""
    from app.modules.production.services.job_card_engine import record_output_v2 as _record
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT job_card_number, entity FROM job_card WHERE job_card_id = $1", job_card_id)
        # M1 + M2: buffer event inside txn; skip emit on error result.
        async with deferred_events():
            async with conn.transaction():
                result = await _record(conn, job_card_id, body.model_dump())
                if "error" not in result and jc:
                    try:
                        await events.job_card_output_saved(jc['entity'], job_card_id=job_card_id, job_card_number=jc['job_card_number'], fg_actual_kg=result.get("fg_actual_kg", 0), yield_pct=result.get("yield_pct"))
                    except Exception:
                        logger.exception("job_card_output_saved emit buffering failed; swallowing")
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    return result


@router.get("/job-cards/{job_card_id}/output")
async def get_output_v2(request: Request, job_card_id: int):
    """Get full output summary: output row + byproducts + balance materials + loss recon."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        output = await conn.fetchrow(
            "SELECT * FROM job_card_output WHERE job_card_id = $1", job_card_id
        )
        if not output:
            raise HTTPException(status_code=404, detail="No output recorded yet")

        byproducts = await conn.fetch(
            "SELECT category, quantity_kg, uom, remarks FROM job_card_byproduct WHERE job_card_id = $1",
            job_card_id,
        )
        balance_materials = await conn.fetch(
            "SELECT bom_line_id, material_id, material_name, balance_type, qty_kg, remarks FROM job_card_balance_material WHERE job_card_id = $1",
            job_card_id,
        )
        # Per-line consumed_qty lives on the indent rows now — surface from there.
        rm_consumed = await conn.fetch(
            "SELECT bom_line_id, material_sku_name, consumed_qty AS consumed_qty_kg, uom FROM job_card_rm_indent WHERE job_card_id = $1 AND consumed_qty IS NOT NULL ORDER BY rm_indent_id",
            job_card_id,
        )
        loss_recon = await conn.fetch(
            """
            SELECT loss_category, budgeted_loss_pct, budgeted_loss_kg, actual_loss_kg, variance_kg, remarks
            FROM job_card_loss_reconciliation WHERE job_card_id = $1 AND deleted_at IS NULL ORDER BY loss_category
            """,
            job_card_id,
        )
        qc = await conn.fetchrow(
            "SELECT result, findings, corrective_action, inspector_user, inspection_date FROM qc_inspection WHERE job_card_id = $1 ORDER BY created_at DESC LIMIT 1",
            job_card_id,
        )

    return {
        "output": dict(output),
        "rm_consumed": [dict(r) for r in rm_consumed],
        "byproducts": [dict(r) for r in byproducts],
        "balance_materials": [dict(r) for r in balance_materials],
        "loss_reconciliation": [dict(r) for r in loss_recon],
        "qc": dict(qc) if qc else None,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Annexure PATCH + DELETE endpoints (added 2026-05-07)
# ═══════════════════════════════════════════════════════════════════════════


@router.patch("/job-cards/{job_card_id}/environment/{env_id}")
async def update_environment(request: Request, job_card_id: int, env_id: int,
                             body: EnvironmentPatchRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_environment(
                    conn, job_card_id, env_id, body.model_dump(exclude_unset=True),
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="environment", annexure_id=env_id,
                        action="updated", changed_by=body.updated_by,
                        changed_fields=changed,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/environment/{env_id}")
async def delete_environment_endpoint(request: Request, job_card_id: int, env_id: int,
                                      body: AnnexureDeleteRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_environment(
                    conn, job_card_id, env_id, body.deleted_by,
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="environment", annexure_id=env_id,
                        action="deleted", changed_by=body.deleted_by,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")
    return {"ok": True, "row": row}


@router.patch("/job-cards/{job_card_id}/metal-detection/{detection_id}")
async def update_metal_detection(request: Request, job_card_id: int, detection_id: int,
                                 body: MetalDetectionPatchRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_metal_detection(
                    conn, job_card_id, detection_id, body.model_dump(exclude_unset=True),
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="metal_detection", annexure_id=detection_id,
                        action="updated", changed_by=body.updated_by,
                        changed_fields=changed,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/metal-detection/{detection_id}")
async def delete_metal_detection_endpoint(request: Request, job_card_id: int, detection_id: int,
                                          body: AnnexureDeleteRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_metal_detection(
                    conn, job_card_id, detection_id, body.deleted_by,
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="metal_detection", annexure_id=detection_id,
                        action="deleted", changed_by=body.deleted_by,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")
    return {"ok": True, "row": row}


@router.patch("/job-cards/{job_card_id}/weight-checks/{check_id}")
async def update_weight_check(request: Request, job_card_id: int, check_id: int,
                              body: WeightCheckPatchRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_weight_check(
                    conn, job_card_id, check_id, body.model_dump(exclude_unset=True),
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="weight_check", annexure_id=check_id,
                        action="updated", changed_by=body.updated_by,
                        changed_fields=changed,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/weight-checks/{check_id}")
async def delete_weight_check_endpoint(request: Request, job_card_id: int, check_id: int,
                                       body: AnnexureDeleteRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_weight_check(
                    conn, job_card_id, check_id, body.deleted_by,
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="weight_check", annexure_id=check_id,
                        action="deleted", changed_by=body.deleted_by,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")
    return {"ok": True, "row": row}


@router.patch("/job-cards/{job_card_id}/loss-reconciliation/{recon_id}")
async def update_loss_reconciliation(request: Request, job_card_id: int, recon_id: int,
                                     body: LossReconciliationPatchRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_loss_reconciliation(
                    conn, job_card_id, recon_id, body.model_dump(exclude_unset=True),
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="loss_reconciliation", annexure_id=recon_id,
                        action="updated", changed_by=body.updated_by,
                        changed_fields=changed,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/loss-reconciliation/{recon_id}")
async def delete_loss_reconciliation_endpoint(request: Request, job_card_id: int, recon_id: int,
                                              body: AnnexureDeleteRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_loss_reconciliation(
                    conn, job_card_id, recon_id, body.deleted_by,
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="loss_reconciliation", annexure_id=recon_id,
                        action="deleted", changed_by=body.deleted_by,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")
    return {"ok": True, "row": row}


@router.patch("/job-cards/{job_card_id}/remarks/{remark_id}")
async def update_remark(request: Request, job_card_id: int, remark_id: int,
                        body: RemarkPatchRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_remark(
                    conn, job_card_id, remark_id, body.model_dump(exclude_unset=True),
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="remarks", annexure_id=remark_id,
                        action="updated", changed_by=body.updated_by,
                        changed_fields=changed,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/remarks/{remark_id}")
async def delete_remark_endpoint(request: Request, job_card_id: int, remark_id: int,
                                 body: AnnexureDeleteRequest):
    from app.modules.production.services import jc_editor
    from app.webhooks import events
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_remark(
                    conn, job_card_id, remark_id, body.deleted_by,
                )
                try:
                    await events.job_card_annexure_changed(
                        jc["entity"], job_card_id=job_card_id,
                        job_card_number=jc["job_card_number"],
                        annexure_type="remarks", annexure_id=remark_id,
                        action="deleted", changed_by=body.deleted_by,
                    )
                except Exception:
                    logger.exception("job_card_annexure_changed emit buffering failed; swallowing")


# ===========================================================================
# Plan v2 endpoints (manual planning workflow)
# ===========================================================================

@router.post("/plans-v2")
async def create_plan_v2(
    request: Request,
    body: PlanV2Create,
    user=Depends(get_current_user),
):
    """Create a plan with header + lines; steps auto-snapshotted from BOM.

    Enforces the user-level factory lock: a non-admin user assigned to a
    specific list of warehouses cannot create a plan against a warehouse
    outside that list.
    """
    if (not user.is_admin
            and user.allowed_warehouses
            and not user_has_warehouse(user.allowed_warehouses, body.warehouse)):
        raise HTTPException(
            status_code=403,
            detail=f"User is not assigned to warehouse '{body.warehouse}'",
        )

    from app.modules.production.services.plan_v2 import create_plan
    pool = request.app.state.db_pool
    # Stamp the creating user on the header (same identity convention as the
    # other audit fields, e.g. record_output's recorded_by). Surfaces in the
    # Plan List "Created by" column via list_plans' SELECT p.*.
    payload = body.model_dump()
    payload["created_by"] = user.full_name or user.phone
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await create_plan(conn, payload)
    except ValueError as exc:
        # Over-allocation against so_fulfillment_v2 pending_qty bounds.
        # Surface as a structured envelope so the frontend's friendlyApiError
        # mapper can label it instead of rendering raw JSON.
        raise HTTPException(
            status_code=400,
            detail={"error": "over_allocation", "message": str(exc)},
        )
    if result.get("error") in ("no_lines", "no_bom"):
        # Pass the full {error, message} envelope through so the client gets
        # an action-quality error ("create plan — one or more selected SKUs
        # have no BOM") instead of the bare message text. Without this,
        # FastAPI's HTTPException would stringify detail and the frontend
        # mapper falls back to raw text.
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/plans-v2")
async def list_plans_v2(
    request: Request,
    entity: str = Query(None),
    warehouse: str = Query(None),
    plan_type: str = Query(None),
    status: str = Query(None),
    date_from: date = Query(None),
    date_to: date = Query(None),
    search: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user=Depends(get_current_user),
):
    """List plans. For non-admin users with a warehouse lock, the result set
    is restricted to their assigned warehouses regardless of the `warehouse`
    query param (a request for a warehouse outside the lock returns 403).

    `search` is a free-text ILIKE applied to plan_name, plan_id,
    warehouse, and joined fg_sku_name + customer_name so operators can
    find a plan by article or customer."""
    user_scope_warehouses: list[str] | None = None
    if not user.is_admin and user.allowed_warehouses:
        if warehouse:
            if not user_has_warehouse(user.allowed_warehouses, warehouse):
                raise HTTPException(
                    status_code=403,
                    detail=f"User is not assigned to warehouse '{warehouse}'",
                )
        else:
            # No explicit filter — apply the user's lock list as the implicit
            # filter. The service layer intersects this with `warehouse`.
            user_scope_warehouses = list(user.allowed_warehouses)

    from app.modules.production.services.plan_v2 import list_plans
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await list_plans(
            conn, entity=entity, warehouse=warehouse, plan_type=plan_type,
            status=status, date_from=date_from, date_to=date_to,
            search=search, page=page, page_size=page_size,
            user_scope_warehouses=user_scope_warehouses,
        )


@router.get("/plans-v2/{plan_id}")
async def get_plan_v2(request: Request, plan_id: int):
    """Full nested plan: header + lines + ordered steps."""
    from app.modules.production.services.plan_v2 import get_plan
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_plan(conn, plan_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return result


@router.get("/plans-v2/{plan_id}/job-card-groups")
async def plan_job_card_groups(request: Request, plan_id: int, user=Depends(get_current_user)):
    """Plan's job cards grouped per plan-line (one product per group) with a
    per-group summary, so the Job Cards view can render each product's stage
    chain as its own section instead of one interleaved flat list.

    Returns ``{"plan_id", "group_count", "groups": [...]}``. Empty ``groups``
    when the plan has no job cards (or doesn't exist) — callers show
    "no job cards" rather than 404, matching job_card_chain_v2's convention.
    """
    from app.modules.production.services.job_card_v2 import get_plan_job_card_groups
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # Factory lock: a plan is warehouse-scoped; don't expose a plan's job
        # cards to a non-admin outside their assigned warehouse.
        if not getattr(user, "is_admin", False) and getattr(user, "allowed_warehouses", None):
            wh = await conn.fetchval(
                "SELECT warehouse FROM production_plan_v2 WHERE plan_id = $1", plan_id,
            )
            if wh is not None and not user_has_warehouse(user.allowed_warehouses, wh):
                raise HTTPException(status_code=403, detail="Plan outside your factory scope")
        return await get_plan_job_card_groups(conn, plan_id)


@router.put("/plans-v2/{plan_id}")
async def update_plan_v2(request: Request, plan_id: int, body: PlanV2Update):
    from app.modules.production.services.plan_v2 import update_plan
    pool = request.app.state.db_pool
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await update_plan(conn, plan_id, fields)
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Plan not found")
    if result.get("error") == "no_change":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/plans-v2/{plan_id}/approve")
async def approve_plan_v2(request: Request, plan_id: int, body: PlanV2Approve):
    from app.modules.production.services.plan_v2 import approve_plan
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await approve_plan(conn, plan_id, body.approved_by)
    if result.get("error") == "missing_approver":
        raise HTTPException(status_code=400, detail=result.get("message"))
    if result.get("error") == "not_found_or_invalid_status":
        raise HTTPException(status_code=404, detail="Plan not found or status not approvable")
    return result


@router.post("/plans-v2/{plan_id}/split")
async def split_plan_v2(
    request: Request,
    plan_id: int,
    mode: Literal["per_line", "sku", "customer"] = Query("per_line"),
    user=Depends(get_current_user),
):
    """Split a DRAFT plan's lines into separate plans (one product per plan).

    ``mode``: per_line (default) | sku | customer. The first group keeps the
    original plan_id; the rest get fresh draft plans. Refuses non-draft plans
    (409) — approved plans carry job cards / MRP / indents that need
    re-derivation, which is out of scope for this version.
    """
    from app.modules.production.services.plan_v2 import split_plan
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if not getattr(user, "is_admin", False) and getattr(user, "allowed_warehouses", None):
            wh = await conn.fetchval(
                "SELECT warehouse FROM production_plan_v2 WHERE plan_id = $1", plan_id,
            )
            if wh is not None and not user_has_warehouse(user.allowed_warehouses, wh):
                raise HTTPException(status_code=403, detail="Plan outside your factory scope")
        async with conn.transaction():
            result = await split_plan(conn, plan_id, mode)
    err = result.get("error")
    if err == "plan_not_found":
        raise HTTPException(status_code=404, detail="Plan not found")
    if err in ("not_draft", "has_job_cards"):
        raise HTTPException(status_code=409, detail=result.get("message"))
    if err in ("nothing_to_split", "invalid_mode"):
        raise HTTPException(status_code=400, detail=result.get("message") or "Cannot split plan")
    return result


@router.post("/plans-v2/{plan_id}/cancel")
async def cancel_plan_v2(
    request: Request,
    plan_id: int,
    body: PlanV2Cancel,
    user=Depends(get_current_user),
):
    """Cancel a v2 plan. **Admin-only** — the cancel_plan service releases
    every line's planned_qty back to the linked so_fulfillment_v2 rows
    (see plan_v2.cancel_plan), which is a destructive change to demand
    allocation and must not be available to floor / shop roles."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "admin_only",
                "message": "Only admin users can cancel a plan.",
            },
        )
    from app.modules.production.services.plan_v2 import cancel_plan
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await cancel_plan(conn, plan_id, body.reason)
    if result.get("error") == "not_found_or_already_cancelled":
        raise HTTPException(status_code=404, detail="Plan not found or already cancelled")
    return result


@router.post("/plans-v2/{plan_id}/delete")
async def delete_plan_v2(
    request: Request,
    plan_id: int,
    body: PlanV2Delete,
    user=Depends(get_current_user),
):
    """Delete an APPROVED plan and notify every active admin by email.

    Uses POST (not HTTP DELETE) so a body can carry the reason + actor.
    Reuses the cancel-plan accounting (planned_qty release) under the hood
    but only accepts approved plans — draft plans go through /cancel.
    **Admin-only** — same rationale as /plans-v2/{id}/cancel (releases
    planned_qty back to fulfillment, mass notification side-effect).

    Email is best-effort: a transient SMTP failure is logged and swallowed
    so the API still confirms the delete. `admin_email_count` in the
    response tells the caller how many admins were notified (0 means the
    admin list was empty or SMTP was disabled).
    """
    if not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "admin_only",
                "message": "Only admin users can delete an approved plan.",
            },
        )
    if not body.reason or not body.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required")

    from app.modules.production.services.plan_v2 import delete_plan
    from app.modules.production.services.mail_service import send_plan_deletion_email

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await delete_plan(
                conn, plan_id,
                reason=body.reason.strip(),
                deleted_by=body.deleted_by,
            )
            if result.get("error") == "not_found_or_invalid_status":
                raise HTTPException(
                    status_code=404,
                    detail="Plan not found or not in approved status",
                )
            # Fan out the admin notification INSIDE the same transaction so
            # the email count is recorded before the response goes out.
            # _send itself opens a separate SMTP socket — failures don't
            # roll the transaction back (caught in the helper).
            plan = result.get("plan") or {}
            try:
                admin_count = await send_plan_deletion_email(
                    conn,
                    plan_id=plan_id,
                    plan_name=plan.get("plan_name"),
                    warehouse=plan.get("warehouse"),
                    entity=plan.get("entity"),
                    reason=body.reason.strip(),
                    deleted_by=body.deleted_by,
                )
            except Exception:
                logger.exception("[plan-delete] admin email fan-out failed (plan_id=%s)", plan_id)
                admin_count = 0

    return {
        "deleted": True,
        "plan_id": plan_id,
        "status": plan.get("status"),
        "admin_email_count": admin_count,
    }


# --- Plan line-level edits ---

@router.put("/plans-v2/lines/{plan_line_id}")
async def update_plan_line_v2(request: Request, plan_line_id: int, body: PlanLineV2Patch):
    """Partial update for a plan line. Mirrors PUT /plans-v2/{plan_id} —
    server filters None-valued keys and applies only the supplied fields.

    Allowed: planned_qty_kg, planned_qty_units, area, deadline_date. The
    qty fields are CHECK > 0 at the column level; zero/negative submissions
    surface as 400 with the column-constraint error.
    """
    from app.modules.production.services.plan_v2 import update_plan_line
    pool = request.app.state.db_pool
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No editable fields supplied")
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                result = await update_plan_line(conn, plan_line_id, fields)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Plan line not found")
    return result


# --- Step-level endpoints ---

@router.put("/plans-v2/lines/{plan_line_id}/steps/reorder")
async def reorder_steps_v2(request: Request, plan_line_id: int, body: StepV2Reorder):
    """Bulk reorder: step_ids[0] becomes step_order=1, etc."""
    from app.modules.production.services.plan_v2 import reorder_steps
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await reorder_steps(conn, plan_line_id, body.step_ids)
    if result.get("error") == "step_set_mismatch":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/plans-v2/lines/{plan_line_id}/steps")
async def add_step_v2(
    request: Request,
    plan_line_id: int,
    body: StepV2Add,
    user=Depends(get_current_user),
):
    """Append a step at the end of the line.

    Enforces the user-level floor lock — same rules as update_step_v2.
    """
    if (body.floor
            and not user.is_admin
            and user.allowed_floors
            and body.floor not in user.allowed_floors):
        raise HTTPException(
            status_code=403,
            detail=f"User is not assigned to floor '{body.floor}'",
        )

    from app.modules.production.services.plan_v2 import add_step
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await add_step(conn, plan_line_id, body.model_dump())
    if result.get("error") == "missing_process_name":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


# --- Create Job Card (per-article wizard) ---

class JobCardLineCreateStep(BaseModel):
    """One WIP process in the Create-Job-Card wizard."""
    process: str
    floor: str
    sfg_output: str | None = None


class JobCardLineCreate(BaseModel):
    """POST /plans-v2/lines/{plan_line_id}/job-cards"""
    qty_kg: float
    qty_units: float | None = None
    wip_steps: list[JobCardLineCreateStep]
    pkg_floor: str


@router.post("/plans-v2/lines/{plan_line_id}/job-cards")
async def create_line_job_cards_v2(
    request: Request,
    plan_line_id: int,
    body: JobCardLineCreate,
    user=Depends(get_current_user),
):
    """Create the chained job cards for ONE plan line (the Plan-List
    "Create Job Card" wizard) and dispatch each to its floor.

    One job card per WIP process → a terminating Packaging card; stage-1 is
    unlocked on its floor, the rest await the previous stage's handoff.
    Enforces the user-level floor lock for every WIP + packaging floor (same
    rule as add_step_v2), runs in one transaction, and is idempotent per line.
    """
    floors = [s.floor for s in body.wip_steps] + [body.pkg_floor]
    if not user.is_admin and user.allowed_floors:
        bad = sorted({f for f in floors if f and f not in user.allowed_floors})
        if bad:
            raise HTTPException(
                status_code=403,
                detail=f"User is not assigned to floor(s): {', '.join(bad)}",
            )

    from app.modules.production.services.job_card_v2 import create_job_cards_for_line
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await create_job_cards_for_line(
                conn,
                plan_line_id,
                qty_kg=body.qty_kg,
                qty_units=body.qty_units,
                wip_steps=[s.model_dump() for s in body.wip_steps],
                pkg_floor=body.pkg_floor,
            )
    err = result.get("error")
    if err == "line_not_found":
        raise HTTPException(status_code=404, detail="Plan line not found")
    if err == "job_cards_already_exist":
        raise HTTPException(
            status_code=409,
            detail=(f"This article already has {result.get('count')} job card(s). "
                    "Cancel them before recreating."),
        )
    if err in ("invalid_qty", "no_wip_steps", "missing_pkg_floor"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/plans-v2/lines/{plan_line_id}/job-cards")
async def get_line_job_cards_v2(request: Request, plan_line_id: int):
    """Return the current job-card config for a plan line, shaped to prefill the
    Edit-Job-Card wizard ({exists, editable, qty_kg, qty_units, wip_steps, pkg_floor})."""
    from app.modules.production.services.job_card_v2 import get_line_job_card_config
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_line_job_card_config(conn, plan_line_id)


@router.put("/plans-v2/lines/{plan_line_id}/job-cards")
async def replace_line_job_cards_v2(
    request: Request,
    plan_line_id: int,
    body: JobCardLineCreate,
    user=Depends(get_current_user),
):
    """Edit (replace) a plan line's job cards from the wizard. Deletes the
    current chain and recreates it; refused once any stage has started. Same
    floor-lock + transaction rules as the create endpoint."""
    floors = [s.floor for s in body.wip_steps] + [body.pkg_floor]
    if not user.is_admin and user.allowed_floors:
        bad = sorted({f for f in floors if f and f not in user.allowed_floors})
        if bad:
            raise HTTPException(
                status_code=403,
                detail=f"User is not assigned to floor(s): {', '.join(bad)}",
            )

    from app.modules.production.services.job_card_v2 import replace_job_cards_for_line
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await replace_job_cards_for_line(
                conn,
                plan_line_id,
                qty_kg=body.qty_kg,
                qty_units=body.qty_units,
                wip_steps=[s.model_dump() for s in body.wip_steps],
                pkg_floor=body.pkg_floor,
            )
    err = result.get("error")
    if err == "line_not_found":
        raise HTTPException(status_code=404, detail="Plan line not found")
    if err == "not_editable":
        raise HTTPException(status_code=409, detail=result.get("message"))
    if err in ("invalid_qty", "no_wip_steps", "missing_pkg_floor"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


class JobCardLineEditStep(BaseModel):
    """One WIP process in the LIVE Edit-Job-Card payload. Existing steps carry
    their job_card_id; newly added steps omit it (None)."""
    job_card_id: int | None = None
    process: str
    floor: str
    sfg_output: str | None = None


class JobCardLineApplyEdits(BaseModel):
    """POST /plans-v2/lines/{plan_line_id}/job-cards/apply-edits — constrained
    live edit of a STARTED chain (floor/qty change, add in the un-started tail,
    remove with forced JC-data snapshot). Any existing WIP job_card_id absent
    from `steps` is treated as a removal."""
    qty_kg: float
    qty_units: float | None = None
    steps: list[JobCardLineEditStep]
    pkg_floor: str
    pkg_job_card_id: int | None = None
    remove_reasons: dict[str, str] | None = None


@router.post("/plans-v2/lines/{plan_line_id}/job-cards/apply-edits")
async def apply_line_job_card_edits_v2(
    request: Request,
    plan_line_id: int,
    body: JobCardLineApplyEdits,
    user=Depends(get_current_user),
):
    """Apply constrained LIVE edits to a plan line's job cards even after stages
    have started: real-time floor change, add a process (un-started tail), qty
    change (synced to the linked SO — ledger + so_line), and remove a process
    (force-records the JC's data then cancels it). Same floor-lock + single
    transaction as create/replace."""
    floors = [s.floor for s in body.steps] + [body.pkg_floor]
    if not user.is_admin and user.allowed_floors:
        bad = sorted({f for f in floors if f and f not in user.allowed_floors})
        if bad:
            raise HTTPException(
                status_code=403,
                detail=f"User is not assigned to floor(s): {', '.join(bad)}",
            )

    from app.modules.production.services.job_card_v2 import apply_live_job_card_edits
    pool = request.app.state.db_pool
    actor = user.full_name or user.phone
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await apply_live_job_card_edits(
                    conn,
                    plan_line_id,
                    qty_kg=body.qty_kg,
                    qty_units=body.qty_units,
                    steps=[s.model_dump() for s in body.steps],
                    pkg_floor=body.pkg_floor,
                    pkg_job_card_id=body.pkg_job_card_id,
                    user=actor,
                    remove_reasons=body.remove_reasons or {},
                )
    except ValueError as exc:
        # Over-allocation against so_fulfillment_v2 pending bounds (qty sync).
        raise HTTPException(
            status_code=400,
            detail={"error": "over_allocation", "message": str(exc)},
        )
    err = result.get("error")
    if err in ("no_job_cards", "job_card_not_found"):
        raise HTTPException(status_code=404, detail=result.get("message") or "Not found")
    if err in ("cannot_remove_terminal", "cannot_remove_started_midchain",
               "cannot_reorder_started_region"):
        raise HTTPException(status_code=409, detail=result.get("message"))
    if err in ("invalid_qty", "no_wip_steps"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/plans-v2/lines/{plan_line_id}/dispatch-info")
async def get_line_dispatch_info_v2(
    request: Request,
    plan_line_id: int,
    user=Depends(get_current_user),
):
    """Prefill payload for the FG-dispatch modal: the packaging (FG) job card +
    its batches (the batch selector source, defaulting to the packaging stage)
    + recipient role labels."""
    from app.modules.production.services.fg_dispatch_service import get_line_dispatch_info
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_line_dispatch_info(conn, plan_line_id)


class FgDispatchBody(BaseModel):
    """POST /plans-v2/lines/{plan_line_id}/dispatch — one packaging batch.
    num_boxes / customer_location / transport fields are operator-entered (no
    stored source)."""
    batch_id: int
    num_boxes: int | None = None
    customer_location: str | None = None
    vehicle_number: str | None = None
    transporter: str | None = None
    transport_location: str | None = None


@router.post("/plans-v2/lines/{plan_line_id}/dispatch")
async def dispatch_line_fg_v2(
    request: Request,
    plan_line_id: int,
    body: FgDispatchBody,
    user=Depends(get_current_user),
):
    """Raise an FG dispatch for one article + packaging batch: emails To
    (billing, candor_operations, store_head) + CC (business_head,
    operations_head, inventory_manager, production_manager) with the job-card
    body, and records it in fg_dispatch_log_v2."""
    from app.modules.production.services.fg_dispatch_service import dispatch_fg
    pool = request.app.state.db_pool
    actor = user.full_name or user.phone
    async with pool.acquire() as conn:
        result = await dispatch_fg(
            conn, plan_line_id,
            batch_id=body.batch_id,
            num_boxes=body.num_boxes,
            customer_location=body.customer_location,
            vehicle_number=body.vehicle_number,
            transporter=body.transporter,
            transport_location=body.transport_location,
            dispatched_by=actor,
        )
    err = result.get("error")
    if err == "no_packaging_jc":
        raise HTTPException(status_code=404, detail=result.get("message"))
    if err == "batch_not_found":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.put("/plans-v2/steps/{step_id}")
async def update_step_v2(
    request: Request,
    step_id: int,
    body: StepV2Patch,
    user=Depends(get_current_user),
):
    """Patch a step's floor / notes / std_time_min / loss_pct / name / stage.

    Uses `exclude_unset=True` so an explicit `null` from the client (e.g. the
    user clearing the floor dropdown) is treated as "set this column to NULL"
    rather than being silently filtered out as a missing field.

    Enforces the user-level floor lock: a non-admin user assigned to a
    specific list of floors cannot set a step's floor to a value outside
    that list. Clearing the floor (sending null) is always permitted.
    """
    fields = body.model_dump(exclude_unset=True)
    new_floor = fields.get("floor")
    if (new_floor
            and not user.is_admin
            and user.allowed_floors
            and new_floor not in user.allowed_floors):
        raise HTTPException(
            status_code=403,
            detail=f"User is not assigned to floor '{new_floor}'",
        )

    from app.modules.production.services.plan_v2 import update_step
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await update_step(conn, step_id, fields)
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Step not found")
    if result.get("error") == "no_change":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.delete("/plans-v2/steps/{step_id}")
async def delete_step_v2(request: Request, step_id: int):
    from app.modules.production.services.plan_v2 import delete_step
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await delete_step(conn, step_id)
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Step not found")
    if result.get("error") == "jc_exists":
        # Approved-plan path: the matching JC still references this
        # step.  409 (Conflict) so the client can distinguish "step
        # gone" (404) from "step still wired into a JC".  The admin's
        # path forward is to cancel the JC first.
        raise HTTPException(status_code=409, detail=result.get("message"))
    return result


@router.get("/plans-v2/bom/{bom_id}")
async def get_bom_summary_v2(request: Request, bom_id: int, full: bool = False):
    """Lightweight BOM summary intended for the Plan Detail BOM hover-card.

    Returns the header, a compact list of materials (truncated at 30 for the
    tooltip), and the ordered process route. Heavier reads should go through
    the existing fulfillment-detail endpoint.

    ``full=true`` drops the 30-line tooltip cap so callers that need the
    COMPLETE material list (e.g. the Create-Job-Card wizard's per-step
    RM/PM breakdown) get every line; default stays capped for the hover-card.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        header = await conn.fetchrow(
            """
            SELECT bom_id, fg_sku_name, customer_name, pack_size_kg, version,
                   is_active, item_group, entity, effective_from, effective_to,
                   notes
            FROM bom_header
            WHERE bom_id = $1
            """,
            bom_id,
        )
        if not header:
            raise HTTPException(status_code=404, detail="BOM not found")

        line_count_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE item_type = 'rm') AS rm_count,
                COUNT(*) FILTER (WHERE item_type = 'pm') AS pm_count,
                COUNT(*)                                  AS total_count
            FROM bom_line WHERE bom_id = $1
            """,
            bom_id,
        )
        lines = await conn.fetch(
            """
            SELECT bom_line_id, line_number, material_sku_name, item_type,
                   quantity_per_unit, uom, loss_pct, godown
            FROM bom_line
            WHERE bom_id = $1
            ORDER BY line_number
            """ + ("" if full else "\n            LIMIT 30\n            "),
            bom_id,
        )
        steps = await conn.fetch(
            """
            SELECT step_number, process_name, stage, std_time_min, loss_pct,
                   machine_type
            FROM bom_process_route
            WHERE bom_id = $1
            ORDER BY step_number
            """,
            bom_id,
        )

    def _norm(row):
        from decimal import Decimal
        from datetime import date as _d, datetime as _dt
        out = {}
        for k, v in dict(row).items():
            if isinstance(v, Decimal):
                out[k] = float(v)
            elif isinstance(v, (_d, _dt)):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out

    return {
        "header": _norm(header),
        "counts": _norm(line_count_row),
        "lines": [_norm(r) for r in lines],
        "steps": [_norm(r) for r in steps],
    }


@router.post("/plans-v2/bom")
async def create_bom_master_v2(
    request: Request,
    body: BomCreateV2Request,
    user=Depends(get_current_user),
):
    """Create/supersede the master BOM for one FG SKU so plan creation
    (POST /plans-v2) resolves it via bom_header ILIKE + is_active."""
    from app.modules.production.services.plan_v2 import create_bom
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await create_bom(conn, body.model_dump())
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  JOB CARD V2 — list/detail + multi-shift time capture
# ═══════════════════════════════════════════════════════════════════════════
# Backed by job_card_v2 + related v2 tables (migration 017). Parallel to
# the legacy /job-cards surface, which continues to serve v1 callers until
# the frontend migrates. A single v2 job card may run across multiple
# shifts and multiple days — each active segment is a row in
# job_card_shift_log_v2; job_card_v2.total_time_min is the running roll-up.

class ShiftStartRequest(BaseModel):
    """POST /job-cards-v2/{id}/shifts/start"""
    shift: Literal['A', 'B', 'C', 'general']
    shift_date: date | None = None   # defaults to today
    operator_name: str | None = None
    notes: str | None = None


class ShiftStopRequest(BaseModel):
    """POST /job-cards-v2/shifts/{log_id}/stop"""
    paused_minutes: int = 0
    notes: str | None = None


@router.get("/job-cards-v2")
async def list_job_cards_v2(
    request: Request,
    entity:     str | None = Query(None),
    factory:    str | None = Query(None),
    floor:      str | None = Query(None),
    status:     str | None = Query(None),
    plan_id:    int | None = Query(None),
    so_number:  str | None = Query(None),
    machine_id: int | None = Query(None),
    customer:   str | None = Query(None),
    search:     str | None = Query(None),
    date_field: Literal["created_at", "start_time", "end_time"] = Query("created_at"),
    date_from:  date | None = Query(None),
    date_to:    date | None = Query(None),
    pendency:   Literal["overdue", "due_today", "due_this_week", "future"] | None = Query(
        None,
        description="overdue | due_today | due_this_week | future",
    ),
    sort_by:    Literal[
        "created_at", "start_time", "end_time", "plan_id",
        "status", "step_number", "job_card_id", "planned_qty_kg",
        "plan_date",
    ] = Query("plan_date"),  # operator-stated default: latest plan first
    sort_order: Literal["ASC", "DESC", "asc", "desc"] = Query("DESC"),
    page:       int = Query(1, ge=1),
    page_size:  int = Query(100, ge=1, le=500),
    user=Depends(get_current_user),
):
    """Paginated list of v2 job cards with R3.D filter / sort / counter
    extensions. Non-admin users with a factory / floor lock get the result
    intersected with their assignment when no explicit factory / floor
    param is given. Explicit out-of-scope params return 403.

    Filters:
      entity, factory, floor (scope), status (comma-sep), plan_id,
      so_number (ILIKE), machine_id, customer (comma-sep), search
      (ILIKE on job_card_number / fg_sku_name / customer_name / batch_number).
    Date window:
      date_field (created_at | start_time | end_time), date_from, date_to.
    Pendency chip:
      pendency (overdue | due_today | due_this_week | future).
    Sort:
      sort_by (created_at | start_time | end_time | plan_id | status |
               step_number | job_card_id | planned_qty_kg),
      sort_order (ASC | DESC).
    Returns:
      results, pagination, counters (total / locked / in_progress /
      completed / pending_issuance / overdue), sort metadata.
    """
    from app.modules.production.services.job_card_v2 import list_job_cards
    pool = request.app.state.db_pool

    user_warehouses = getattr(user, "allowed_warehouses", []) or []
    user_floors     = getattr(user, "allowed_floors",     []) or []
    is_admin        = getattr(user, "is_admin", False)

    if factory and not is_admin and user_warehouses and not user_has_warehouse(user_warehouses, factory):
        raise HTTPException(status_code=403,
                            detail=f"User is not assigned to factory '{factory}'")
    if floor and not is_admin and user_floors and floor not in user_floors:
        raise HTTPException(status_code=403,
                            detail=f"User is not assigned to floor '{floor}'")

    async with pool.acquire() as conn:
        result = await list_job_cards(
            conn,
            entity=entity, factory=factory, floor=floor,
            status=status, plan_id=plan_id, so_number=so_number,
            machine_id=machine_id,
            customer=customer, search=search,
            date_field=date_field, date_from=date_from, date_to=date_to,
            pendency=pendency,
            sort_by=sort_by, sort_order=sort_order,
            page=page, page_size=page_size,
            user_scope_warehouses=None if is_admin or factory else user_warehouses or None,
            user_scope_floors=None     if is_admin or floor   else user_floors     or None,
        )
    # FastAPI's Literal validation catches typos at the param boundary,
    # but the service still validates defensively. Surface those as 400.
    if isinstance(result, dict) and result.get("error") in (
        "invalid_sort_by", "invalid_date_field", "invalid_pendency",
    ):
        raise HTTPException(status_code=400, detail=result)
    # B13: strip cost fields from each result row.
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/search")
async def search_job_cards_v2(
    request: Request,
    q:       str | None = Query(None, description="Free-text needle; matched case-insensitively across job_card_number, fg_sku_name (article), customer_name, batch_number, process_name, stage, assigned_to_team_leader, factory, floor, entity, plan_id, job_card_id, and the linked SO number."),
    status:  str | None = Query(None, description="Comma-separated status list."),
    entity:  str | None = Query(None),
    factory: str | None = Query(None),
    floor:   str | None = Query(None),
    user=Depends(get_current_user),
):
    """Free-text search across the v2 job-card surface.

    NOT paginated by design — a search query is "find anything matching" and
    callers should not have to juggle page indexes for it. The service caps
    results at SEARCH_HARD_CAP (1000) and sets ``capped: true`` in the response
    when the cap is hit, so the UI can prompt the user to narrow the query.

    Pagination params (``page``, ``page_size``) are intentionally absent from
    this endpoint's signature. Any caller that sends them gets the default
    FastAPI 422 "Unprocessable Entity" — but only if they use the structured
    form; ``Query(None)`` would silently accept and ignore extras. Either way
    the response is identical: the unpaginated result set."""
    from app.modules.production.services.job_card_v2 import search_job_cards
    pool = request.app.state.db_pool

    user_warehouses = getattr(user, "allowed_warehouses", []) or []
    user_floors     = getattr(user, "allowed_floors",     []) or []
    is_admin        = getattr(user, "is_admin", False)

    # Same explicit-out-of-scope guard as list_job_cards_v2. Without this an
    # operator filtered to a plant outside their assignment would silently
    # see an empty result instead of a clear 403.
    if factory and not is_admin and user_warehouses and not user_has_warehouse(user_warehouses, factory):
        raise HTTPException(status_code=403,
                            detail=f"User is not assigned to factory '{factory}'")
    if floor and not is_admin and user_floors and floor not in user_floors:
        raise HTTPException(status_code=403,
                            detail=f"User is not assigned to floor '{floor}'")

    async with pool.acquire() as conn:
        result = await search_job_cards(
            conn,
            q=q, status=status, entity=entity, factory=factory, floor=floor,
            user_scope_warehouses=None if is_admin or factory else user_warehouses or None,
            user_scope_floors=None     if is_admin or floor   else user_floors     or None,
        )
    # B13 cost-metric gate on search hits (same surface as list_job_cards_v2).
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/sfg-inventory")
async def sfg_inventory_v2(
    request: Request,
    sku_name: str = Query(..., description="The SFG#### code to look up"),
    entity: str = Query(...),
    floor_id: str | None = Query(None),
    user=Depends(get_current_user),
):
    """Slice 5 — Stage-2 SFG inventory picker source. Lists AVAILABLE WIP/SFG
    batches (inventory_batch item_type='wip') for the given SFG#### code in FIFO
    order, so the Final-FG stage can issue the semi-finished input materialised by
    its upstream Create-WIP stage's close. Mirrors the RM/PM picker
    (GET /inventory/batches) but scoped to item_type='wip'.

    MUST be declared BEFORE /job-cards-v2/{job_card_id} so 'sfg-inventory' is not
    captured as a job_card_id. Cost-gated (labels carry no cost, but the gate is
    wired defensively so any future cost column is auto-stripped for deny-listed
    roles)."""
    # Entity/floor scope (Slice-5 review #6): a non-admin must not read another
    # entity's (or floor's) WIP stock. Empty allowed list = wildcard.
    if not getattr(user, "is_admin", False):
        allowed_ent = getattr(user, "allowed_entities", []) or []
        if allowed_ent and entity not in allowed_ent:
            raise HTTPException(status_code=403, detail="Entity outside your scope")
        allowed_fl = getattr(user, "allowed_floors", []) or []
        if floor_id and allowed_fl and floor_id not in allowed_fl:
            raise HTTPException(status_code=403, detail="Floor outside your scope")
    from app.modules.production.services.inventory_service import get_available_batches
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # by_sfg_code (PT1/057): match the canonical inventory_batch.sfg_code
        # column (sku_name now holds the article name). exclude_expired: G3 strict.
        batches = await get_available_batches(
            conn, sku_name, entity, item_type='wip', floor_id=floor_id,
            by_sfg_code=True, exclude_expired=True,
        )
    return strip_cost_fields(
        {"batches": batches},
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/sfg-master")
async def sfg_master_v2(
    request: Request,
    search: str | None = Query(None, description="match SFG#### code or name"),
    sfg_code: str | None = Query(None, description="exact SFG#### code"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user=Depends(get_current_user),
):
    """The dedicated SFG catalogue (design ref §8.1) — projected from the
    sfg_master view over all_sku(item_type='sfg'). Entity-agnostic (the SFG
    catalogue is shared across entities). Cost-gated defensively.

    MUST be declared BEFORE /job-cards-v2/{job_card_id} so 'sfg-master' is not
    captured as a job_card_id."""
    from app.modules.production.services.sfg_catalog_service import list_sfg_master
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await list_sfg_master(conn, search=search, sfg_code=sfg_code,
                                       page=page, page_size=page_size)
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/sfg-where-used")
async def sfg_where_used_v2(
    request: Request,
    sfg_code: str = Query(..., description="the SFG#### code to reverse-look-up"),
    entity: str | None = Query(None),
    user=Depends(get_current_user),
):
    """Reverse index (design ref §9.2): which FGs consume this SFG####.
    Sourced from the sfg_where_used view. Entity-scoped for non-admins.

    MUST be declared BEFORE /job-cards-v2/{job_card_id}."""
    # Non-admin scope: restrict the where-used fan-out to the caller's entities.
    if not getattr(user, "is_admin", False):
        allowed_ent = getattr(user, "allowed_entities", []) or []
        if entity and allowed_ent and entity not in allowed_ent:
            raise HTTPException(status_code=403, detail="Entity outside your scope")
        if not entity and len(allowed_ent) == 1:
            entity = allowed_ent[0]
    from app.modules.production.services.sfg_catalog_service import get_sfg_where_used
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_sfg_where_used(conn, sfg_code, entity=entity)
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/sfg-wip-stock")
async def sfg_wip_stock_v2(
    request: Request,
    entity: str = Query(...),
    search: str | None = Query(None, description="match SFG#### code or name"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user=Depends(get_current_user),
):
    """WIP/SFG on-hand stock grouped by SFG#### (design ref §9.5 WIP-stock view),
    from the inventory_batch item_type='wip' ledger. Entity-scoped for non-admins.

    MUST be declared BEFORE /job-cards-v2/{job_card_id}."""
    if not getattr(user, "is_admin", False):
        allowed_ent = getattr(user, "allowed_entities", []) or []
        if allowed_ent and entity not in allowed_ent:
            raise HTTPException(status_code=403, detail="Entity outside your scope")
    from app.modules.production.services.sfg_catalog_service import list_wip_stock
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await list_wip_stock(conn, entity, search=search, page=page, page_size=page_size)
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/sfg-binding")
async def sfg_binding_v2(
    request: Request,
    bom_id: int | None = Query(None),
    fg_sku_name: str | None = Query(None),
    sfg_code: str | None = Query(None),
    entity: str | None = Query(None),
    user=Depends(get_current_user),
):
    """FG↔stage↔SFG#### binding (design ref §9.2) from the fg_sfg_binding view.
    Filter by FG (bom_id or name), SFG code, and/or entity. Entity-scoped for
    non-admins.

    MUST be declared BEFORE /job-cards-v2/{job_card_id}."""
    if not getattr(user, "is_admin", False):
        allowed_ent = getattr(user, "allowed_entities", []) or []
        if entity and allowed_ent and entity not in allowed_ent:
            raise HTTPException(status_code=403, detail="Entity outside your scope")
        if not entity and len(allowed_ent) == 1:
            entity = allowed_ent[0]
    from app.modules.production.services.sfg_catalog_service import get_fg_sfg_binding
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_fg_sfg_binding(conn, bom_id=bom_id, fg_sku_name=fg_sku_name,
                                          sfg_code=sfg_code, entity=entity)
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/{job_card_id}")
async def get_job_card_v2(
    request: Request,
    job_card_id: int,
    user=Depends(get_current_user),
):
    """Full v2 job card detail — header + shifts + outputs + indents + sign-offs.

    B13 cost-metric gate: strip currency-bearing fields when the caller
    is deny-listed.
    """
    from app.modules.production.services.job_card_v2 import get_job_card
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_job_card(conn, job_card_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Job card not found")
    # Enforce user-level factory/floor lock at read time too. Admin bypasses.
    if not getattr(user, "is_admin", False):
        if user.allowed_warehouses and not user_has_warehouse(user.allowed_warehouses, result.get("factory")):
            raise HTTPException(status_code=403, detail="JC outside your factory scope")
        if user.allowed_floors and result.get("floor") and result["floor"] not in user.allowed_floors:
            raise HTTPException(status_code=403, detail="JC outside your floor scope")
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/{job_card_id}/chain")
async def job_card_chain_v2(
    request: Request,
    job_card_id: int,
    user=Depends(get_current_user),
):
    """Stage chain for a v2 JC — siblings on the same plan_line ordered by
    step_number. The current JC is marked with ``is_current: true`` so the
    UI can highlight it without a separate lookup.

    Empty array on missing JC instead of 404 because the JC may have been
    soft-cancelled; callers display "no chain" rather than erroring out."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        anchor = await conn.fetchrow(
            "SELECT plan_line_id, factory, floor FROM job_card_v2 WHERE job_card_id=$1",
            job_card_id,
        )
        if not anchor:
            return []
        # Same factory/floor lock the detail endpoint applies — don't expose
        # the chain of a JC the caller couldn't read directly.
        if not getattr(user, "is_admin", False):
            if user.allowed_warehouses and not user_has_warehouse(user.allowed_warehouses, anchor["factory"]):
                raise HTTPException(status_code=403, detail="JC outside your factory scope")
            if user.allowed_floors and anchor["floor"] and anchor["floor"] not in user.allowed_floors:
                raise HTTPException(status_code=403, detail="JC outside your floor scope")

        rows = await conn.fetch(
            """
            SELECT job_card_id, job_card_number, step_number,
                   process_name, stage,
                   factory, floor, status,
                   input_kind, output_kind, input_code, output_code,
                   planned_qty_kg, carried_qty_kg, dispatched_to_next_kg,
                   prev_job_card_id, next_job_card_id,
                   start_time, end_time
            FROM   job_card_v2
            WHERE  plan_line_id = $1
              AND  deleted_at IS NULL
            ORDER  BY step_number
            """,
            anchor["plan_line_id"],
        )
        result = [
            {
                "job_card_id":           r["job_card_id"],
                "job_card_number":       r["job_card_number"],
                "step_number":           r["step_number"],
                "process_name":          r["process_name"],
                "stage":                 r["stage"],
                "factory":               r["factory"],
                "floor":                 r["floor"],
                "status":                r["status"],
                "input_kind":            r["input_kind"],
                "output_kind":           r["output_kind"],
                "input_code":            r["input_code"],
                "output_code":           r["output_code"],
                "planned_qty_kg":        float(r["planned_qty_kg"])         if r["planned_qty_kg"]         is not None else None,
                "carried_qty_kg":        float(r["carried_qty_kg"])         if r["carried_qty_kg"]         is not None else None,
                "dispatched_to_next_kg": float(r["dispatched_to_next_kg"])  if r["dispatched_to_next_kg"]  is not None else None,
                "prev_job_card_id":      r["prev_job_card_id"],
                "next_job_card_id":      r["next_job_card_id"],
                "start_time":            r["start_time"].isoformat() if r["start_time"] else None,
                "end_time":              r["end_time"].isoformat()   if r["end_time"]   else None,
                "is_current":            r["job_card_id"] == job_card_id,
            }
            for r in rows
        ]
        # B13 cost-metric gate: today the chain query selects no cost
        # columns, but the gate is wired in defensively so future SELECT
        # additions are auto-stripped for deny-listed roles.
        return strip_cost_fields(
            result,
            getattr(user, "role_name", None),
            is_admin=getattr(user, "is_admin", False),
        )


@router.post("/job-cards-v2/{job_card_id}/shifts/start")
async def start_shift_v2(
    request: Request,
    job_card_id: int,
    body: ShiftStartRequest,
    user=Depends(get_current_user),
):
    """Open a new shift segment on this v2 job card.

    Refuses (400) when another segment is currently open — must stop the
    prior one first. The first start_shift on a JC also stamps the JC's
    headline start_time and transitions it to 'in_progress'.
    """
    from app.modules.production.services.job_card_v2 import start_shift
    pool = request.app.state.db_pool
    shift_date_v = body.shift_date or date.today()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await start_shift(
                conn,
                job_card_id=job_card_id,
                shift=body.shift,
                shift_date=shift_date_v,
                operator_name=body.operator_name or user.full_name or user.phone,
                notes=body.notes,
            )
    _raise_if_locked(result)
    if result.get("error") == "open_segment_exists":
        raise HTTPException(status_code=400, detail=result)
    if result.get("error") == "invalid_shift":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/job-cards-v2/shifts/{log_id}/stop")
async def stop_shift_v2(
    request: Request,
    log_id: int,
    body: ShiftStopRequest,
    user=Depends(get_current_user),
):
    """Close an open shift segment + recompute the v2 JC's total_time_min."""
    from app.modules.production.services.job_card_v2 import stop_shift
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await stop_shift(
                conn,
                log_id=log_id,
                paused_minutes=body.paused_minutes,
                notes=body.notes,
            )
    if result.get("error") == "log_not_found":
        raise HTTPException(status_code=404, detail="Shift log not found")
    if result.get("error") == "already_closed":
        raise HTTPException(status_code=400, detail="Shift segment is already closed")
    if result.get("error") == "negative_pause":
        raise HTTPException(status_code=400, detail="paused_minutes must be >= 0")
    return result


@router.get("/job-cards-v2/{job_card_id}/shifts")
async def list_shifts_v2(
    request: Request,
    job_card_id: int,
    user=Depends(get_current_user),
):
    """Return all shift segments for the v2 job card, ordered by start_at."""
    from app.modules.production.services.job_card_v2 import list_shifts
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return {"segments": await list_shifts(conn, job_card_id)}


# ─── Team assignment ────────────────────────────────────────────────────────

class AssignTeamV2Request(BaseModel):
    """PUT /job-cards-v2/{id}/assign"""
    team_leader: str
    team_members: list[str] | None = None


@router.put("/job-cards-v2/{job_card_id}/assign")
async def assign_team_v2(
    request: Request,
    job_card_id: int,
    body: AssignTeamV2Request,
    user=Depends(get_current_user),
):
    """Assign a team leader and optional members to a v2 JC.

    - Refuses on a 'locked' JC (downstream stage waiting on RM handoff).
    - Refuses on terminal 'closed' / 'cancelled' status.
    - Moves an 'unlocked' JC to 'assigned'; leaves later statuses as-is
      (re-assignment is fine, just updates names).
    """
    from app.modules.production.services.job_card_v2 import assign_team
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await assign_team(
                conn,
                job_card_id=job_card_id,
                team_leader=body.team_leader,
                team_members=body.team_members,
            )
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") == "missing_team_leader":
        raise HTTPException(status_code=400, detail=result["message"])
    if result.get("error") == "terminal_state":
        raise HTTPException(status_code=400,
                            detail=f"JC is already {result['current_status']}")
    if result.get("error") == "locked":
        raise HTTPException(status_code=400, detail=result["message"])
    return result


# ─── Multi-stage material accounting (migration 018) ──────────────────────
#
# Three sub-endpoints under /job-cards-v2/{id}/accounting/* :
#   GET                    — full view (consumption + byproducts + summary + stage context)
#   PUT  /consumption      — upsert consumption rows
#   PUT  /byproducts       — upsert byproduct rows
#   PUT  /summary          — save the balance summary; backend computes
#                            is_balanced and the loss percentages

class AccountingConsumptionRow(BaseModel):
    """One input line on the consumption table. SFG/WIP rows describe
    material carried from a previous stage; RM rows mirror the indent.
    Adapter classes for the two kinds aren't needed because the column
    set is identical — what differs is the `input_kind` discriminator."""
    material_sku_name:    str
    input_kind:           Literal['RM', 'SFG', 'WIP', 'PM'] = 'RM'
    uom:                  str
    issued_qty:           float = 0
    actual_consumed_qty:  float = 0
    return_qty:           float = 0
    source_rm_indent_id:  int | None = None
    source_dispatch_id:   int | None = None
    remarks:              str | None = None


class AccountingConsumptionRequest(BaseModel):
    rows: list[AccountingConsumptionRow]


class AccountingByproductRow(BaseModel):
    # B6 C1 fix: the pm_* categories were added in migration 028 and the
    # service-side gate exists, but this Literal had to be extended too -
    # otherwise FastAPI returned 422 *before* the gate could fire,
    # making the gate dead code.
    category:  Literal[
        'tukda', 'damaged', 'black_stained', 'without_shell', 'empty_shells',
        'dust', 'balance_material', 'rejection', 'control_sample', 'other',
        'pm_torn', 'pm_damaged', 'pm_misprint', 'pm_rejection', 'pm_wasted',
    ]
    quantity:  float
    uom:       str = 'KGS'
    remarks:   str | None = None
    # Migration 034 — see ByproductLineV2 above. Mirrored here so the
    # dedicated /accounting/byproducts endpoint accepts the attribution too.
    material_name: str | None = None
    bom_line_id:   int | None = None


class AccountingByproductsRequest(BaseModel):
    rows: list[AccountingByproductRow]


class AccountingSummaryRequest(BaseModel):
    total_input_qty:        float
    input_uom:              str = 'KGS'
    output_qty:             float = 0
    output_uom:             str = 'KGS'
    output_qty_units:       float | None = None
    process_loss_qty:       float = 0
    # 6 sub-categories the UI exposes: moisture_loss, roasting_loss,
    # floor_waste, dust_loss, machine_waste, sticky_material. Keys are
    # free-form so future categories don't need a migration.
    process_loss_breakdown: dict[str, float] | None = None
    extra_give_away_qty:    float = 0
    balance_material_qty:   float = 0
    offgrade_total_qty:     float = 0
    rejection_qty:          float = 0
    wastage_qty:            float = 0
    control_sample_qty:     float = 0
    # Migration 049: per-batch accounting. When set, the upsert keys on
    # (job_card_id, COALESCE(batch_id, 0)) so each batch keeps its own
    # IS_BALANCED + percentages instead of all batches stomping a single
    # JC-level row. Older clients that omit this hit the COALESCE
    # sentinel 0 — backward-compat with pre-049 single-row-per-JC saves.
    batch_id:               int | None = None


@router.get("/job-cards-v2/{job_card_id}/accounting")
async def get_accounting_v2(
    request: Request,
    job_card_id: int,
    batch_id: int | None = Query(None),
    user=Depends(get_current_user),
):
    """Full accounting view for a v2 JC. Includes:
       stage context (step number, position, prev/next IDs, carried qty),
       consumption rows, byproduct rows, accounting summary.

    When `batch_id` is supplied, consumption + byproducts are filtered to
    rows tagged with that batch (and rows still carrying NULL batch_id are
    surfaced as well — defensive parity with the frontend's matchesBatch
    helper, which keeps legacy rows visible under the picked batch instead
    of vanishing). The accounting summary row is JC-level (one row per JC)
    and is returned as-is regardless of the batch filter.

    B13 cost-metric gate: strips currency-bearing fields from the
    response when the caller's role is in the deny list. Qty / yield /
    conservation math stays for everyone; only cost is restricted.
    """
    from app.modules.production.services.jc_accounting_v2 import get_accounting
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_accounting(conn, job_card_id, batch_id=batch_id)
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    return strip_cost_fields(
        result,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.put("/job-cards-v2/{job_card_id}/accounting/consumption")
async def save_consumption_v2(
    request: Request,
    job_card_id: int,
    body: AccountingConsumptionRequest,
    user=Depends(get_current_user),
):
    """Upsert consumption rows on this JC. The (job_card_id,
    material_sku_name) unique index makes re-saves an UPDATE."""
    from app.modules.production.services.jc_accounting_v2 import save_consumption
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await save_consumption(
                conn, job_card_id=job_card_id,
                rows=[r.model_dump() for r in body.rows],
                recorded_by=user.full_name or user.phone,
            )
    _raise_if_locked(result)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.put("/job-cards-v2/{job_card_id}/accounting/byproducts")
async def save_byproducts_v2(
    request: Request,
    job_card_id: int,
    body: AccountingByproductsRequest,
    user=Depends(get_current_user),
):
    """Upsert byproduct rows. Zero-qty saves let the UI clear a row."""
    from app.modules.production.services.jc_accounting_v2 import save_byproducts
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await save_byproducts(
                conn, job_card_id=job_card_id,
                rows=[r.model_dump() for r in body.rows],
                recorded_by=user.full_name or user.phone,
            )
    _raise_if_locked(result)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.put("/job-cards-v2/{job_card_id}/accounting/summary")
async def save_accounting_summary_v2(
    request: Request,
    job_card_id: int,
    body: AccountingSummaryRequest,
    user=Depends(get_current_user),
):
    """Save the summary balance row. Backend computes is_balanced and the
    loss percentages; returns the saved row + the residual difference."""
    from app.modules.production.services.jc_accounting_v2 import save_accounting
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await save_accounting(
                conn, job_card_id=job_card_id,
                payload=body.model_dump(),
                saved_by=user.full_name or user.phone,
                # Migration 049: tag the summary row with the batch the
                # operator was looking at. None falls back to the legacy
                # COALESCE-0 sentinel (one row per JC) for older clients.
                batch_id=body.batch_id,
            )
    _raise_if_locked(result)
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result)
    return result


# ─── Stage handoff (WIP/SFG → next stage) ──────────────────────────────────

class DispatchToNextRequest(BaseModel):
    """POST /job-cards-v2/{id}/dispatch-to-next

    Pydantic-level `gt=0` mirrors the service-side `invalid_qty` check
    so a malformed client (qty_kg=0 or negative) is rejected with a
    422 before the service is even invoked. Service still re-checks
    defensively because other call paths bypass the router.
    """
    qty_kg:    float = Field(gt=0)
    qty_units: float | None = Field(default=None, ge=0)
    notes:     str | None = None


@router.post("/job-cards-v2/{job_card_id}/dispatch-to-next")
async def dispatch_to_next_v2(
    request: Request,
    job_card_id: int,
    body: DispatchToNextRequest,
    user=Depends(get_current_user),
):
    """Hand qty from this JC to its next_job_card_id partner. Auto-unlocks
    the downstream JC when it was waiting on the previous stage."""
    from app.modules.production.services.job_card_v2 import dispatch_to_next
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await dispatch_to_next(
                conn,
                job_card_id=job_card_id,
                qty_kg=body.qty_kg,
                qty_units=body.qty_units,
                dispatched_by=user.full_name or user.phone,
                notes=body.notes,
            )
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") in ("invalid_qty", "no_next_stage", "chain_broken"):
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


# ─── Output capture ─────────────────────────────────────────────────────────

def _coerce_float(v):
    """Accept int / float / numeric string from the client and store as
    float. Empty strings collapse to None so DB columns stay NULL rather
    than 0.0."""
    if v is None or v == "":
        return None
    return float(v)


class ConsumedLineV2(BaseModel):
    """Per-BOM-line consumption row. `consumed_qty` is in the row's own
    UOM (the v2 indent CHECK constraint already pinned the UOM at
    materialisation time, so we accept the operator's number as-is and
    stamp it onto the indent row by bom_line_id)."""
    bom_line_id:       int
    material_sku_name: str | None = None
    consumed_qty:      float
    remarks:           str | None = None
    # Slice 4: per-row opening-input kind (RM | PM | SFG | WIP). Defaults to the
    # bucket kind in the handler when omitted; an SFG/WIP line sends its own so
    # it persists as input_kind='SFG'/'WIP' (gate G1 Option B), not 'RM'.
    input_kind:        str | None = None
    source_dispatch_id: int | None = None

    @field_validator("consumed_qty", mode="before")
    @classmethod
    def _to_float(cls, v):
        return _coerce_float(v)


class RecordOutputV2Request(BaseModel):
    """POST /job-cards-v2/{id}/outputs

    Accepts both the lean v2 shape (`output_qty_kg` / `rm_consumed_kg`
    scalars) AND the legacy fat shape the v1 client still sends
    (`fg_actual_kg` / `fg_actual_units` + `rm_consumed[]` per-line). The
    fat fields are mapped onto the lean ones by the model validator
    below so downstream code only sees the v2 surface.

    `process_loss_kg` persists on the output row (migration 026).
    `balance_materials` write to job_card_balance_material_v2 and `qc`
    writes to job_card_qc_v2 (migration 027). `fg_expected_*` is
    informational and silently dropped here.

    Stage 2 / migration 038: `batch_id` tags every persisted row.  When
    omitted, the handler resolves it from the JC's currently-open
    batch (1 open → use it; 0 → 400 no_open_batch; ≥ 2 → 400
    ambiguous_open_batch).  Stage 3 UI passes it explicitly via the
    batch selector dropdown."""
    # Lean v2 fields (canonical)
    rm_consumed_kg:   float | None = None
    output_qty_kg:    float | None = None
    output_qty_units: float | None = None
    output_kind:      Literal['SFG', 'WIP', 'FG'] | None = None
    uom:              str | None = None
    notes:            str | None = None
    # Per-line consumption — operator's record of which articles were
    # consumed and by how much. Each entry is matched to its indent row
    # by bom_line_id (cheap O(1) lookup via the FK index added in
    # migration 023). Splitting RM and PM keeps the kind unambiguous so
    # we hit the right indent table without secondary lookups.
    #
    # R10 — diff-on-save semantics: None (field omitted) = "leave this
    # section untouched". Empty list [] = "explicit clear". Old clients
    # that always sent a list keep working; the new Edit Batch flow
    # omits fields it didn't touch so the server preserves them.
    rm_consumed:      list[ConsumedLineV2] | None = None
    pm_consumed:      list[ConsumedLineV2] | None = None
    # Legacy v1 aliases — populated by older clients. The model
    # validator maps these onto the lean fields when the lean fields
    # are absent.
    fg_actual_kg:     float | None = None
    fg_actual_units:  float | None = None
    fg_expected_kg:   float | None = None     # informational only — not stored
    fg_expected_units: int | None = None      # informational only — not stored
    process_loss_kg:  float | None = None     # persisted on the output row
    byproducts:       list[ByproductLineV2]    | None = None
    balance_materials: list[BalanceMaterialV2] | None = None  # job_card_balance_material_v2
    additives:        list[AdditiveLineV2]     | None = None  # job_card_additive_consumption_v2
    qc:               QCDataV2 | None = None         # job_card_qc_v2
    # Stage 2: tag every row written by this save with the batch_id.
    # None → server-side defaulting from the JC's open batch (see
    # record_output_v2 handler).
    batch_id:         int | None = None
    # Stage 3 polish: admin-only override that permits a save against
    # a closed / cancelled batch.  The handler validates the caller
    # actually has is_admin before honouring it; a non-admin sending
    # this flag gets a 403.
    admin_override:   bool       = False

    @field_validator(
        "rm_consumed_kg", "output_qty_kg", "output_qty_units",
        "fg_actual_kg", "fg_actual_units", "fg_expected_kg", "process_loss_kg",
        mode="before",
    )
    @classmethod
    def _to_float(cls, v):
        return _coerce_float(v)

    @model_validator(mode="after")
    def _bridge_legacy(self):
        # output_qty_kg ← fg_actual_kg when the lean field isn't set.
        if self.output_qty_kg is None and self.fg_actual_kg is not None:
            self.output_qty_kg = self.fg_actual_kg
        if self.output_qty_units is None and self.fg_actual_units is not None:
            self.output_qty_units = self.fg_actual_units
        # rm_consumed_kg ← sum of rm_consumed[].consumed_qty when the
        # operator didn't send a scalar total. Matches the v1 engine
        # behaviour at job_card_engine.record_output_v2.
        if self.rm_consumed_kg is None and self.rm_consumed:
            self.rm_consumed_kg = float(sum(r.consumed_qty for r in self.rm_consumed))
        return self


@router.post("/job-cards-v2/{job_card_id}/outputs")
async def record_output_v2(
    request: Request,
    job_card_id: int,
    body: RecordOutputV2Request,
    user=Depends(get_current_user),
):
    """Append an output row (RM consumed + output qty + yield) for this JC.

    When the body includes per-BOM-line `rm_consumed` / `pm_consumed`
    entries, each is written back to the matching indent row's
    `consumed_qty` column so the JC-detail response round-trips the
    operator's per-line entry on the next read. Rows whose bom_line_id
    doesn't belong to this JC are rejected — prevents a stale or
    malicious payload from updating consumption on a different JC.
    """
    from app.modules.production.services.job_card_v2 import (
        assert_not_locked, record_output, upsert_consumption_lines,
    )
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # R6 lock guard fired at the endpoint top because /outputs
            # is the only handler that calls multiple write services
            # (upsert_consumption_lines + record_output). Checking once
            # here means a locked JC cannot waste the rm/pm consumption
            # upserts before record_output's own guard fires. record_output
            # still re-checks defensively for callers that bypass the
            # router (e.g. job_card_engine.record_output_v2).
            lock_err = await assert_not_locked(conn, job_card_id)
            _raise_if_locked(lock_err)

            # ── Stage 2: resolve batch_id ─────────────────────────────
            # When the caller doesn't pass an explicit batch_id, fall
            # back to "the JC's currently-open batch".  Exactly one
            # open batch → use it (preserves the existing single-batch
            # UI flow).  Zero → 400 no_open_batch (operator must open
            # one).  Two+ → 400 ambiguous_open_batch (Stage 3 UI will
            # pass explicit batch_id in this case).
            resolved_batch_id: int | None = body.batch_id
            if resolved_batch_id is None:
                open_rows = await conn.fetch(
                    "SELECT batch_id FROM job_card_batch_v2 "
                    "WHERE  job_card_id = $1 AND status = 'open' "
                    "ORDER  BY batch_number",
                    job_card_id,
                )
                if len(open_rows) == 1:
                    resolved_batch_id = open_rows[0]["batch_id"]
                elif len(open_rows) == 0:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "no_open_batch",
                            "message": (
                                "Open a batch on this job card before "
                                "saving output."
                            ),
                        },
                    )
                else:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "ambiguous_open_batch",
                            "open_batch_ids": [r["batch_id"] for r in open_rows],
                            "message": (
                                "Multiple batches are open on this job "
                                "card — pass batch_id explicitly in the "
                                "request body to disambiguate."
                            ),
                        },
                    )
            else:
                # Verify the supplied batch belongs to this JC + is open.
                row = await conn.fetchrow(
                    "SELECT status, job_card_id "
                    "FROM   job_card_batch_v2 WHERE batch_id = $1",
                    resolved_batch_id,
                )
                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail={"error": "batch_not_found",
                                "batch_id": resolved_batch_id},
                    )
                if row["job_card_id"] != job_card_id:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "batch_jc_mismatch",
                            "message": "batch_id belongs to a different "
                                       "job card than the URL path.",
                        },
                    )
                # Closed/cancelled batches reject writes by default.
                # Admin override (Stage 3 polish) lets admins correct
                # output post-close.  Non-admin senders of the override
                # flag get a 403 — surface the role check explicitly
                # rather than letting the write slip through and a
                # different gate (e.g. lock) absorb the mistake.
                if row["status"] not in ("open",):
                    if body.admin_override:
                        if not user.is_admin:
                            raise HTTPException(
                                status_code=403,
                                detail={
                                    "error": "admin_override_forbidden",
                                    "message": (
                                        "admin_override is restricted to "
                                        "users with the admin role."
                                    ),
                                },
                            )
                        # Audit: append a marker to the batch row's notes
                        # so a future reviewer can see this batch was
                        # edited post-close.  Stamped once per save.
                        audit_actor = user.full_name or user.phone or "admin"
                        await conn.execute(
                            """
                            UPDATE job_card_batch_v2
                               SET notes = COALESCE(notes || E'\n', '')
                                         || '[admin_override] saved by '
                                         || $2 || ' at '
                                         || to_char(NOW() AT TIME ZONE 'Asia/Kolkata',
                                                    'YYYY-MM-DD HH24:MI:SS')
                                         || ' IST'
                             WHERE batch_id = $1
                            """,
                            resolved_batch_id, audit_actor,
                        )
                        # Sync the BatchRow snapshot with the new values.
                        # close_batch wrote fg_actual_kg / fg_actual_units /
                        # process_loss_kg on the underlying job_card_phase_v2
                        # row when the batch was closed. Without this UPDATE,
                        # an admin_override save would INSERT a fresh
                        # job_card_output_v2 row with the corrected values
                        # but the form would re-open showing the OLD
                        # batch-table values — making the save look like a
                        # no-op even though the audit log + output row both
                        # changed. COALESCE preserves untouched fields when
                        # the operator's diff-on-save omits them.
                        # `body.output_qty_kg` / `output_qty_units` are
                        # populated by the _bridge_legacy validator from
                        # `fg_actual_kg` / `fg_actual_units` when only the
                        # legacy aliases are sent.
                        await conn.execute(
                            """
                            UPDATE job_card_batch_v2
                               SET fg_actual_kg     = COALESCE($2, fg_actual_kg),
                                   fg_actual_units  = COALESCE($3, fg_actual_units),
                                   process_loss_kg  = COALESCE($4, process_loss_kg)
                             WHERE batch_id = $1
                            """,
                            resolved_batch_id,
                            body.output_qty_kg, body.output_qty_units,
                            body.process_loss_kg,
                        )
                    else:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "error": "batch_not_open",
                                "status": row["status"],
                                "message": "Cannot save output against a "
                                           f"batch in status '{row['status']}'."
                                           " Admin users may pass "
                                           "admin_override=true.",
                            },
                        )
            # ── Per-BOM-line consumption ──────────────────────────────
            # Validated against the JC's BOM catalog (bom_line for the
            # JC's bom_id) — every stage can record consumption against
            # every BOM article, not just the ones materialised into
            # this stage's indent rows. The packaging (last) stage
            # commonly records both RM and PM here.
            #
            # R10 — diff-on-save: rm_consumed / pm_consumed are now
            # Optional. None = "section omitted, leave alone" (treated
            # like empty in the set-union below — no rows to validate
            # or upsert).
            rm_rows = body.rm_consumed or []
            pm_rows = body.pm_consumed or []
            submitted_bom_lines = (
                {int(r.bom_line_id) for r in rm_rows} |
                {int(p.bom_line_id) for p in pm_rows}
            )
            if submitted_bom_lines:
                valid = await conn.fetch(
                    """
                    SELECT bom_line_id
                    FROM   bom_line bl
                    JOIN   job_card_v2 jc ON jc.bom_id = bl.bom_id
                    WHERE  jc.job_card_id = $1
                    """,
                    job_card_id,
                )
                valid_ids = {r["bom_line_id"] for r in valid}
                invalid = submitted_bom_lines - valid_ids
                if invalid:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "invalid_bom_line",
                            "message": (
                                f"bom_line_id(s) {sorted(invalid)} do not "
                                "belong to this job card's BOM"
                            ),
                        },
                    )

                # Upsert into job_card_material_consumption_v2 — Stage 2
                # UNIQUE is (job_card_id, COALESCE(batch_id, 0),
                # material_sku_name) so the same article can be
                # recorded once per batch.  Re-save on the same batch
                # updates in place; first save on a new batch inserts.
                rec_by = user.full_name or user.phone
                await upsert_consumption_lines(
                    conn, job_card_id=job_card_id,
                    entries=[r.model_dump() for r in rm_rows],
                    input_kind='RM', recorded_by=rec_by,
                    batch_id=resolved_batch_id,
                )
                await upsert_consumption_lines(
                    conn, job_card_id=job_card_id,
                    entries=[p.model_dump() for p in pm_rows],
                    input_kind='PM', recorded_by=rec_by,
                    batch_id=resolved_batch_id,
                )

            # R10 — diff-on-save: skip record_output() entirely when the
            # caller didn't send an FG quantity. The output row REQUIRES
            # output_qty_kg (record_output returns missing_qty otherwise),
            # so secondary fields alone — process_loss_kg, rm_consumed_kg
            # (often auto-derived from rm_consumed[].consumed_qty by
            # _bridge_legacy when the operator types per-line consumption)
            # — must NOT be enough to trigger the insert. Without this
            # tighter check, a save with only consumption + byproducts
            # filled (no FG Actual) 400'd because rm_consumed_kg got
            # bridged from the line list and process_loss_kg defaulted to
            # 0 on the frontend, flipping has_output_payload true and
            # forcing record_output to fail validation.
            has_output_payload = (
                body.output_qty_kg is not None
                or body.output_qty_units is not None
                or body.fg_actual_kg is not None
                or body.fg_actual_units is not None
            )
            if has_output_payload:
                result = await record_output(
                    conn,
                    job_card_id=job_card_id,
                    rm_consumed_kg=body.rm_consumed_kg,
                    output_qty_kg=body.output_qty_kg,
                    output_qty_units=body.output_qty_units,
                    output_kind=body.output_kind,
                    uom=body.uom,
                    notes=body.notes,
                    process_loss_kg=body.process_loss_kg,
                    recorded_by=user.full_name or user.phone,
                    # Tag the output row with the batch it belongs to.
                    # Sibling persistence calls (upsert_consumption_lines,
                    # save_byproducts, replace_balance_materials) already
                    # pass this; record_output was the odd one out and was
                    # leaving job_card_output_v2.batch_id = NULL on every
                    # save — which broke the frontend's batchScopedDefaults
                    # fallback (it filters rows by batch_id, so null-tagged
                    # outputs were always skipped). Result: FG Actual /
                    # Process Loss looked blank after every reload.
                    batch_id=resolved_batch_id,
                )
                _raise_if_locked(result)
            else:
                result = {"recorded": False, "skipped": "no_output_payload"}
            # ── Byproducts (v2) ───────────────────────────────────────
            # The legacy v1 client posts byproducts alongside the
            # output row; persist them into job_card_byproducts_v2 via
            # the shared accounting helper. UOM 'kg' is normalised to
            # 'KGS' so the v2 universal-UOM check passes.
            rec_by = user.full_name or user.phone
            if body.byproducts and "error" not in result:
                from app.modules.production.services.jc_accounting_v2 import (
                    save_byproducts,
                )
                rows = []
                for b in body.byproducts:
                    raw_uom = (b.uom or "KGS").strip().upper()
                    norm_uom = "KGS" if raw_uom == "KG" else raw_uom
                    rows.append({
                        "category":      b.category,
                        "quantity":      b.qty_kg,
                        "uom":           norm_uom,
                        "remarks":       b.remarks,
                        # Migration 034 — article attribution must be
                        # forwarded explicitly; the previous hand-built
                        # dict dropped these fields and the operator's
                        # picked SKU vanished on the way to
                        # save_byproducts.
                        "material_name": b.material_name,
                        "bom_line_id":   b.bom_line_id,
                    })
                bp_result = await save_byproducts(
                    conn, job_card_id=job_card_id,
                    rows=rows, recorded_by=rec_by,
                    batch_id=resolved_batch_id,
                )
                if "error" in bp_result:
                    raise HTTPException(status_code=400, detail=bp_result)
                result["byproducts"] = bp_result.get("rows", [])

            # ── Balance materials (v2, migration 027) ─────────────────
            # Per-BOM-line leftover / wastage / control-sample rows.
            # The Android form posts one entry per filled article; the
            # service skips zero-qty no-remark rows so we don't write
            # noise.
            if body.balance_materials and "error" not in result:
                from app.modules.production.services.job_card_v2 import (
                    replace_balance_materials,
                )
                bm_result = await replace_balance_materials(
                    conn, job_card_id=job_card_id,
                    rows=[m.model_dump() for m in body.balance_materials],
                    recorded_by=rec_by,
                    batch_id=resolved_batch_id,
                )
                if "error" in bm_result:
                    raise HTTPException(status_code=400, detail=bm_result)
                result["balance_materials"] = bm_result.get("rows", [])

            # ── Additives (035) — data-keeping bucket ────────────────
            # Replace-all semantics. None = "section omitted, leave
            # alone" (R10 diff-on-save).  Empty list [] = "explicit
            # clear" — the operator removed every additive row in the
            # UI and we honour that.  Old clients that always sent a
            # list keep working; new Edit Batch flow only sends the
            # field when additives were edited.
            if body.additives is not None and "error" not in result:
                from app.modules.production.services.jc_additives_v2 import (
                    save_additives,
                )
                add_result = await save_additives(
                    conn, job_card_id=job_card_id,
                    rows=[a.model_dump() for a in body.additives],
                    recorded_by=rec_by,
                    batch_id=resolved_batch_id,
                )
                result["additives"] = add_result
            result["batch_id"] = resolved_batch_id

            # ── QC summary (v2, migration 027) ───────────────────────
            # Single-row roll-up. Passing `qc.passed` = None keeps the
            # row at 'pending' rather than asserting a verdict the
            # operator didn't make.
            if body.qc is not None and "error" not in result:
                from app.modules.production.services.job_card_v2 import (
                    upsert_qc,
                )
                qc_result = await upsert_qc(
                    conn, job_card_id=job_card_id,
                    passed=body.qc.passed,
                    findings=body.qc.remarks,
                    corrective_action=body.qc.corrective_action,
                    inspector_user=body.qc.inspector,
                    recorded_by=rec_by,
                )
                result["qc"] = qc_result.get("qc")
    # Structured envelopes so the frontend's friendlyApiError mapper can
    # decode the code and render a sentence ("Cannot save — an output qty
    # is required") instead of dumping raw JSON to the operator. Mirrors
    # the same fix applied to plans-v2 in 1820645.
    if result.get("error") == "job_card_not_found":
        raise HTTPException(
            status_code=404,
            detail={"error": "job_card_not_found", "message": "Job card not found"},
        )
    if result.get("error") == "negative_qty":
        raise HTTPException(
            status_code=400,
            detail={"error": "negative_qty", "message": "qty values must be >= 0"},
        )
    if result.get("error") == "missing_qty":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_qty",
                "message": "output_qty_kg (or fg_actual_kg) is required",
            },
        )
    if result.get("error") == "implausible_yield":
        # Operator typo: yield outside ±999.999% almost always means a
        # value was entered in the wrong unit (grams instead of kg, units
        # vs kg, etc.). Surface the numbers in details so the frontend
        # mapper can show them ("grams vs kg is the usual culprit").
        raise HTTPException(
            status_code=400,
            detail={
                "error": "implausible_yield",
                "yield_pct": result.get("yield_pct"),
                "output_qty_kg": result.get("output_qty_kg"),
                "rm_consumed_kg": result.get("rm_consumed_kg"),
                "message": (
                    f"Yield computes to {result['yield_pct']:.2f}% — check that "
                    f"output_qty_kg ({result['output_qty_kg']}) and "
                    f"rm_consumed_kg ({result['rm_consumed_kg']}) are both in kg."
                ),
            },
        )
    return result


# ─── R13 Batch closure ─────────────────────────────────────────────────────
# (Renamed from "phase" in migration 036 / Stage 1 of the Batch redesign.)

class BatchOpenRequest(BaseModel):
    """POST /job-cards-v2/{id}/batches/open"""
    planned_qty_kg: float | None = Field(default=None, ge=0)
    batch_date:     date  | None = None
    notes:          str   | None = None
    # Operator-typed free-text batch name (072). Blank → server keeps NULL and
    # the UI falls back to "Batch <number>". batch_number stays the internal key.
    batch_label:    str   | None = Field(default=None, max_length=120)
    # Stage 2: how much of the JC pool this batch claims to consume.
    # Optional — no server-side gate; informational only.
    input_qty_kg:   float | None = Field(default=None, ge=0)


class BatchCloseRequest(BaseModel):
    """POST /job-cards-v2/{id}/batches/{batch_id}/close"""
    produced_qty_kg:     float        = Field(ge=0)
    output_kind:         str   | None = None
    output_uom:          str   | None = None
    output_qty_units:    float | None = None
    yield_pct:           float | None = None
    rm_consumed_kg:      float        = 0.0
    extra_give_away_qty: float        = 0.0
    notes:               str   | None = None
    # Stage 2 per-batch summary snapshot.  The UI computes these
    # client-side from the live Output & Accounting form and posts
    # them on close so the batch row carries a self-contained record.
    input_qty_kg:           float | None = None
    process_loss_kg:        float | None = None
    control_sample_kg:      float | None = None
    is_balanced:            bool  | None = None
    balance_difference_qty: float | None = None
    closure_remarks:        str   | None = None
    # Stage 3 final: per-batch partial dispatch.  When omitted, the
    # full produced_qty_kg goes to the next JC (legacy behaviour).
    # When supplied, only `dispatch_qty_kg` flows downstream and the
    # remainder stays at this JC's stage — the operator can dispatch
    # it later via /dispatch-to-next or roll it into a subsequent
    # batch's close.  Clamped to [0, produced_qty_kg] by close_batch.
    dispatch_qty_kg:        float | None = Field(default=None, ge=0)


class BatchCancelRequest(BaseModel):
    """POST /job-cards-v2/{id}/batches/{batch_id}/cancel"""
    reason: str | None = Field(default=None, max_length=500)


class BatchRenameRequest(BaseModel):
    """POST /job-cards-v2/{id}/batches/{batch_id}/rename (072) — set/clear the
    operator-typed batch name. Blank clears back to the "Batch <number>" fallback."""
    batch_label: str | None = Field(default=None, max_length=120)


@router.post("/job-cards-v2/{job_card_id}/batches/open")
async def batch_open_v2(
    request: Request,
    job_card_id: int,
    body: BatchOpenRequest | None = None,
    user=Depends(get_current_user),
):
    """R13: open a new batch row for this JC. Stage 1 still enforces "at
    most one open batch per JC" (drops in Stage 2)."""
    from app.modules.production.services import job_card_batch_v2 as svc
    pool = request.app.state.db_pool
    body = body or BatchOpenRequest()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await svc.open_batch(
                conn,
                job_card_id=job_card_id,
                planned_qty_kg=body.planned_qty_kg,
                batch_date=body.batch_date,
                input_qty_kg=body.input_qty_kg,
                batch_label=body.batch_label,
                notes=body.notes,
            )
    _raise_if_locked(result)
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") == "batch_already_open":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/job-cards-v2/{job_card_id}/batches/{batch_id}/close")
async def batch_close_v2(
    request: Request,
    job_card_id: int,
    batch_id: int,
    body: BatchCloseRequest,
    user=Depends(get_current_user),
):
    """R13: close a batch. In one txn: stamp batch row, write the output,
    auto-dispatch to next JC, unlock downstream if it was waiting."""
    from app.modules.production.services import job_card_batch_v2 as svc
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await svc.close_batch(
                conn,
                batch_id=batch_id,
                job_card_id=job_card_id,
                produced_qty_kg=body.produced_qty_kg,
                output_kind=body.output_kind,
                output_uom=body.output_uom,
                output_qty_units=body.output_qty_units,
                yield_pct=body.yield_pct,
                rm_consumed_kg=body.rm_consumed_kg,
                extra_give_away_qty=body.extra_give_away_qty,
                input_qty_kg=body.input_qty_kg,
                process_loss_kg=body.process_loss_kg,
                control_sample_kg=body.control_sample_kg,
                is_balanced=body.is_balanced,
                balance_difference_qty=body.balance_difference_qty,
                closure_remarks=body.closure_remarks,
                dispatch_qty_kg=body.dispatch_qty_kg,
                notes=body.notes,
                closed_by=user.full_name or user.phone,
            )
    _raise_if_locked(result)
    if result.get("error") in ("batch_not_found", "job_card_not_found"):
        raise HTTPException(status_code=404, detail=result.get("message", result["error"]))
    if result.get("error") == "batch_jc_mismatch":
        raise HTTPException(status_code=404, detail=result)
    if result.get("error") in ("batch_not_open", "batch_already_open",
                                "batch_date_taken", "batch_number_taken"):
        raise HTTPException(status_code=409, detail=result)
    if result.get("error") in ("invalid_produced_qty", "yield_unreasonable"):
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.get("/job-cards-v2/{job_card_id}/batches")
async def batch_list_v2(
    request: Request,
    job_card_id: int,
    user=Depends(get_current_user),
):
    """R13: list all batches (open + closed + cancelled) for the JC,
    ordered by batch_number."""
    from app.modules.production.services import job_card_batch_v2 as svc
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await svc.list_batches(conn, job_card_id)
    return {"batches": rows}


@router.post("/job-cards-v2/{job_card_id}/batches/{batch_id}/cancel")
async def batch_cancel_v2(
    request: Request,
    job_card_id: int,
    batch_id: int,
    body: BatchCancelRequest | None = None,
    user=Depends(get_current_user),
):
    """R13: cancel an open batch that has no attached output / dispatch /
    shift rows. Useful for batches opened by mistake."""
    from app.modules.production.services import job_card_batch_v2 as svc
    pool = request.app.state.db_pool
    body = body or BatchCancelRequest()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await svc.cancel_batch(
                conn,
                batch_id=batch_id,
                job_card_id=job_card_id,
                reason=body.reason,
                cancelled_by=user.full_name or user.phone,
            )
    _raise_if_locked(result)
    if result.get("error") == "batch_not_found":
        raise HTTPException(status_code=404, detail="Batch not found")
    if result.get("error") == "batch_jc_mismatch":
        raise HTTPException(status_code=404, detail=result)
    if result.get("error") in ("batch_not_open", "batch_has_attached_rows"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/job-cards-v2/{job_card_id}/batches/{batch_id}/rename")
async def batch_rename_v2(
    request: Request,
    job_card_id: int,
    batch_id: int,
    body: BatchRenameRequest | None = None,
    user=Depends(get_current_user),
):
    """072: set/clear a batch's free-text name (shown instead of "Batch N")."""
    from app.modules.production.services import job_card_batch_v2 as svc
    pool = request.app.state.db_pool
    body = body or BatchRenameRequest()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await svc.rename_batch(
                conn,
                batch_id=batch_id,
                job_card_id=job_card_id,
                batch_label=body.batch_label,
                changed_by=(user.full_name or user.phone),
            )
    _raise_if_locked(result)
    if result.get("error") == "batch_not_found":
        raise HTTPException(status_code=404, detail="Batch not found")
    if result.get("error") == "batch_jc_mismatch":
        raise HTTPException(status_code=404, detail=result)
    return result


# ─── R12 notify-QC ──────────────────────────────────────────────────────────

class NotifyQCRequest(BaseModel):
    """POST /job-cards-v2/{id}/notify-qc (R12)

    Triggered once the JC is `completed` to alert the assigned QC team.
    The transport itself is pluggable - see services/qc_notify.py for
    the hook registry. note is an optional free-form payload that gets
    threaded into the hook call and logged to qc_notification_log_v2.
    """
    note: str | None = Field(default=None, max_length=2000)


@router.post("/job-cards-v2/{job_card_id}/notify-qc")
async def notify_qc_v2(
    request: Request,
    job_card_id: int,
    body: NotifyQCRequest | None = None,
    user=Depends(get_current_user),
):
    """Alert the QC team scoped to this JC's entity / factory / floor.
    Allowed only when the JC is in status='completed'. Logs every
    dispatch attempt to qc_notification_log_v2 regardless of hook
    delivery outcome.
    """
    from app.modules.production.services import qc_notify
    pool = request.app.state.db_pool
    body = body or NotifyQCRequest()

    # B8 C2 fix: hook calls must NOT hold a Postgres transaction open
    # (a live WhatsApp transport could take seconds per recipient and
    # would otherwise pin connection-pool slots and row locks). We split
    # the work into three phases:
    #   1. Short txn: validate JC status, fetch recipients.
    #   2. NO txn:    iterate recipients calling the (possibly slow) hook;
    #                 collect outcomes in memory.
    #   3. Short txn: write all log rows in a single tight loop.
    async with pool.acquire() as conn:
        async with conn.transaction():
            jc = await conn.fetchrow(
                """
                SELECT status, entity, factory, floor
                FROM   job_card_v2
                WHERE  job_card_id=$1 AND deleted_at IS NULL
                """,
                job_card_id,
            )
            if jc is None:
                raise HTTPException(status_code=404, detail="Job card not found")
            if jc["status"] != 'completed':
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error":  "invalid_status",
                        "status": jc["status"],
                        "message": (
                            "notify-qc fires only on 'completed' JCs - "
                            f"current status is '{jc['status']}'."
                        ),
                    },
                )
            recipients = await conn.fetch(
                """
                SELECT u.user_id, u.phone, u.full_name
                FROM   auth_user u
                JOIN   auth_role r ON r.role_id = u.role_id
                WHERE  r.role_name = 'qc_inspector'
                  AND  u.is_active = TRUE
                  AND  (
                        SELECT TRUE FROM auth_role_permission rp
                         WHERE  rp.role_id = u.role_id
                           AND  (rp.allowed_entities   IS NULL
                                 OR cardinality(rp.allowed_entities)   = 0
                                 OR $1 = ANY(rp.allowed_entities))
                           AND  (rp.allowed_warehouses IS NULL
                                 OR cardinality(rp.allowed_warehouses) = 0
                                 OR $2 = ANY(rp.allowed_warehouses))
                           AND  (rp.allowed_floors     IS NULL
                                 OR cardinality(rp.allowed_floors)     = 0
                                 OR $3 IS NULL
                                 OR $3 = ANY(rp.allowed_floors))
                         LIMIT 1
                  )
                """,
                jc["entity"], jc["factory"], jc["floor"],
            )

    # If no recipients matched, log a single sentinel row (audit "we
    # tried but nobody was scoped") and return the real dispatch result
    # so the notification_id is observable. B8 H4 fix.
    if not recipients:
        async with pool.acquire() as conn:
            base = (body.note or "")
            suffix = " [no_qc_recipients_in_scope]"
            trimmed = base[: max(0, 2000 - len(suffix))]
            result = await qc_notify.dispatch(
                conn,
                job_card_id=job_card_id,
                recipients=[{"user_id": None, "phone": None, "full_name": None}],
                note=trimmed + suffix,
                dispatched_by=user.user_id,
            )
        result.setdefault("warning", "no_qc_recipients_in_scope")
        return result

    # Dispatch acquires per-recipient mini-txns internally; do NOT wrap
    # in an outer transaction here - the slow hook call must stay out
    # of any open txn (B8 C2).
    async with pool.acquire() as conn:
        result = await qc_notify.dispatch(
            conn,
            job_card_id=job_card_id,
            recipients=[dict(r) for r in recipients],
            note=body.note,
            dispatched_by=user.user_id,
        )
    return result


# ─── Sign-off ───────────────────────────────────────────────────────────────

class SignOffRequest(BaseModel):
    """POST /job-cards-v2/{id}/sign-off

    `signed_by_name` lets the operator at the device record a sign-off
    on behalf of someone else (e.g. the production head standing
    nearby). When provided, it overrides the JWT-derived signer for
    the persisted `signed_by` column. When omitted, the bearer-token
    user is used — preserving the old behaviour for callers that don't
    send the field.

    `notes` is a free-form audit trail field, kept separate from
    `signed_by_name` so the signer's name and any operational notes
    are recorded in distinct columns.
    """
    role:           str
    notes:          str | None = None
    signed_by_name: str | None = None


@router.post("/job-cards-v2/{job_card_id}/sign-off")
async def sign_off_v2(
    request: Request,
    job_card_id: int,
    body: SignOffRequest,
    user=Depends(get_current_user),
):
    """Record a per-role sign-off. UNIQUE (job_card_id, role) — re-signing
    under the same role refreshes the row instead of erroring.

    R12 access gate (qc_inspector role only):
      * The calling user must hold the qc_inspector role.
      * The caller's permission scope (allowed_entities / warehouses /
        floors) must cover the JC's entity, factory, and floor. Wildcard
        (empty list) scopes are treated as "all allowed".
      * signed_by is force-stamped from the JWT identity - body's
        signed_by_name is IGNORED on QC sign-offs to block front-end
        spoofing. Other roles keep the typed-name override.
    """
    from app.modules.production.services.job_card_v2 import add_sign_off
    pool = request.app.state.db_pool

    is_qc_sign_off = body.role == 'qc_inspector'

    # B7 H2/H3 fix: ONE pool.acquire() and ONE transaction wrapping the
    # scope check, the JC existence assertion (with deleted_at IS NULL AND
    # FOR UPDATE), and the sign-off insert. Closes the race where a PATCH
    # moves the JC out of scope between read and write.
    async with pool.acquire() as conn:
        async with conn.transaction():
            jc_scope = await conn.fetchrow(
                "SELECT entity, factory, floor FROM job_card_v2 "
                "WHERE  job_card_id=$1 AND deleted_at IS NULL "
                "FOR    UPDATE",
                job_card_id,
            )
            if jc_scope is None:
                raise HTTPException(status_code=404, detail="Job card not found")

            if is_qc_sign_off:
                # B7 C1 fix: admin users bypass the role gate (matches the
                # admin-can-do-anything convention used elsewhere). For
                # non-admin callers the role must be qc_inspector AND the
                # caller's scope must cover the JC's location.
                if not (getattr(user, "is_admin", False)
                        or user.role_name == 'qc_inspector'):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "error": "qc_role_required",
                            "message": (
                                "Only admin or qc_inspector users can verify "
                                "QC on a JC."
                            ),
                        },
                    )

                def _in_scope(value, allowed: list[str]) -> bool:
                    # Empty allowed list = wildcard (no restriction).
                    return (not allowed) or (value in allowed)

                # Warehouse needs tolerant matching ('A185' ≡ 'A-185'); the
                # other two compare entity / floor literally.
                def _wh_in_scope(value, allowed: list[str]) -> bool:
                    return (not allowed) or user_has_warehouse(allowed, value)

                if not (getattr(user, "is_admin", False)
                        or (_in_scope(jc_scope["entity"],   user.allowed_entities)
                            and _wh_in_scope(jc_scope["factory"], user.allowed_warehouses)
                            and _in_scope(jc_scope["floor"],   user.allowed_floors))):
                    # B7 L2: don't leak the user's full allowed list back.
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "error": "qc_scope_mismatch",
                            "jc_entity":   jc_scope["entity"],
                            "jc_factory":  jc_scope["factory"],
                            "jc_floor":    jc_scope["floor"],
                            "message": (
                                "Your QC scope does not cover this JC's entity, "
                                "factory, or floor."
                            ),
                        },
                    )
                # Server-stamp the signer; ignore body.signed_by_name on QC.
                signer = user.full_name or user.phone or f"user#{user.user_id}"
            else:
                # B7 H1: other roles also keep server-stamped signer when
                # the caller is admin. Non-admin callers can still pass a
                # typed name (e.g. signing on the production_head's behalf
                # while they're at the device) - this matches the legacy
                # behaviour and the framework expressly defers role-scoped
                # enforcement for non-QC roles to a later workstream.
                typed = (body.signed_by_name or "").strip()
                signer = typed if typed else (user.full_name or user.phone or f"user#{user.user_id}")

            result = await add_sign_off(
                conn,
                job_card_id=job_card_id,
                role=body.role,
                signed_by=signer,
                notes=body.notes,
            )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ─── Lifecycle: start / complete / force-unlock / patch / cancel ──────────

class ForceUnlockV2Request(BaseModel):
    """PUT /job-cards-v2/{id}/force-unlock"""
    authority: str       # operator / supervisor name
    reason:    str


class StopJobCardV2Request(BaseModel):
    """POST /job-cards-v2/{id}/stop (R1)

    Mid-run cancellation for material_received / in_progress JCs.
    Cancels any open R13 phase atomically. Approval per R8 row 8 is
    enforced upstream at the amendments layer; this endpoint records
    the linkage but does not (yet) require it - field becomes
    mandatory once B11 ships the amendment service.
    """
    reason:     str       = Field(min_length=1, max_length=500)
    request_id: int | None = Field(
        default=None,
        description=(
            "bom_amendment_request_v2.request_id that authorised this stop. "
            "Optional until B11 lands the maker-checker enforcement."
        ),
    )


class PatchJobCardV2Request(BaseModel):
    """PATCH /job-cards-v2/{id}

    Whitelisted fields only — status / lineage / chain pointers can't be
    patched here; use the dedicated lifecycle endpoints. Sending unknown
    keys is harmless (silently ignored)."""
    fg_sku_name:         str | None = None
    customer_name:       str | None = None
    batch_number:        str | None = None
    planned_qty_kg:      float | None = None
    planned_qty_units:   float | None = None
    uom:                 str | None = None
    assigned_to_team_leader: str | None = None
    team_members:        list[str] | None = None
    floor:               str | None = None
    machine_id:          int | None = None


class CancelJobCardV2Request(BaseModel):
    """DELETE /job-cards-v2/{id}"""
    reason: str


@router.put("/job-cards-v2/{job_card_id}/start")
async def start_jc_v2(
    request: Request,
    job_card_id: int,
    user=Depends(get_current_user),
):
    """Move a v2 JC into 'in_progress'."""
    from app.modules.production.services.job_card_v2 import start_job_card
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await start_job_card(conn, job_card_id=job_card_id)
    _raise_if_locked(result)
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") == "invalid_status":
        raise HTTPException(status_code=400, detail=result["message"])
    return result


class CompleteJCV2Request(BaseModel):
    """PUT /job-cards-v2/{id}/complete (R9 closure gate + R13 phase gate)

    Body is optional - default complete with no overrides. To force-close
    an unbalanced JC (per R8 row 12) the caller must supply BOTH
    force=true AND request_id pointing at the approved
    bom_amendment_request_v2 row.
    """
    force:      bool       = Field(default=False)
    request_id: int | None = Field(
        default=None,
        description=(
            "bom_amendment_request_v2.request_id authorising an unbalanced "
            "close. Required when force=true. Approval-status validation "
            "lands with B11."
        ),
    )


@router.put("/job-cards-v2/{job_card_id}/complete")
async def complete_jc_v2(
    request: Request,
    job_card_id: int,
    body: CompleteJCV2Request | None = None,
    user=Depends(get_current_user),
):
    """Move a v2 JC from 'in_progress' to 'completed'. Refuses when:
      * An open shift segment is still running (would skew total_time_min)
      * Any R13 phase row is still 'open' (close the phase first)
      * R9 balance check fails (is_balanced=false on the latest accounting
        save) unless an R8 'unbalanced_close_override' is supplied via
        force=true + request_id.
    """
    from app.modules.production.services.job_card_v2 import complete_job_card
    pool = request.app.state.db_pool
    body = body or CompleteJCV2Request()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await complete_job_card(
                conn,
                job_card_id=job_card_id,
                force=body.force,
                request_id=body.request_id,
                completed_by=user.full_name or user.phone,
            )
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") == "no_accounting":
        raise HTTPException(status_code=400, detail=result["message"])
    if result.get("error") == "accounting_save_failed":
        # Auto-derive ran but save_accounting refused (most likely a
        # locked JC). Surface enough detail for the operator to act.
        raise HTTPException(status_code=400, detail=result)
    # B5 H2: invalid_status / open_shift are state conflicts -> 409.
    if result.get("error") in ("invalid_status", "open_shift",
                                "unbalanced", "open_batch",
                                "override_request_not_found",
                                "override_request_wrong_type",
                                "override_request_not_approved"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.put("/job-cards-v2/{job_card_id}/force-unlock")
async def force_unlock_v2(
    request: Request,
    job_card_id: int,
    body: ForceUnlockV2Request,
    user=Depends(get_current_user),
):
    """Admin override: flip a locked JC to 'unlocked' regardless of
    upstream-handoff state. Stamps force_unlocked + audit fields."""
    from app.modules.production.services.job_card_v2 import force_unlock
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await force_unlock(
                conn,
                job_card_id=job_card_id,
                authority=body.authority,
                reason=body.reason,
            )
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") in ("missing_authority", "missing_reason", "not_locked"):
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/job-cards-v2/{job_card_id}/stop")
async def stop_jc_v2(
    request: Request,
    job_card_id: int,
    body: StopJobCardV2Request,
    user=Depends(get_current_user),
):
    """R1 Stop Process: mid-run cancel for JCs already receiving material
    or in_progress. Cancels any open R13 phase first inside the same txn,
    then flips the JC to 'cancelled' with a [STOP_PROCESS]-prefixed reason.

    Approval composition with R8 row 8 (floor_manager maker, admin or
    production_manager checker) lives at the /amendments layer - this
    endpoint executes once the request is approved.
    """
    from app.modules.production.services.job_card_v2 import stop_job_card
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await stop_job_card(
                conn,
                job_card_id=job_card_id,
                reason=body.reason,
                stopped_by=user.full_name or user.phone,
                request_id=body.request_id,
            )
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") in ("invalid_status", "missing_reason"):
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.patch("/job-cards-v2/{job_card_id}")
async def patch_jc_v2(
    request: Request,
    job_card_id: int,
    body: PatchJobCardV2Request,
    user=Depends(get_current_user),
):
    """Partial update of header fields. Use lifecycle endpoints for
    status / start / complete / close / cancel — those aren't patchable
    through this surface."""
    from app.modules.production.services.job_card_v2 import patch_job_card
    pool = request.app.state.db_pool
    # exclude_unset so omitted fields don't get blanked.
    fields = body.model_dump(exclude_unset=True)
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await patch_job_card(
                conn,
                job_card_id=job_card_id,
                fields=fields,
                updated_by=user.full_name or user.phone,
            )
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") == "invalid_status":
        # R1 header-edit gate (PATCHABLE_STATUSES). 409 because the JC's
        # state conflicts with the requested operation.
        raise HTTPException(status_code=409, detail=result)
    if result.get("error") == "no_change":
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.delete("/job-cards-v2/{job_card_id}")
async def cancel_jc_v2(
    request: Request,
    job_card_id: int,
    body: CancelJobCardV2Request,
    user=Depends(get_current_user),
):
    """Soft-cancel a v2 JC. Allowed only before 'in_progress' — past that
    point, finish via complete/close instead. **Admin-only** — non-admin
    operators see a 403; this is a destructive action that releases
    plan-level allocations and rewrites the JC status, so it stays
    behind the admin gate. The cancel_job_card service stamps a JSONB
    snapshot of the full pre-cancel state on the row before flipping
    status (migration 043) so a future read can reconstruct what the
    operator saw at the moment they hit Cancel."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "admin_only",
                "message": "Only admin users can cancel a job card.",
            },
        )
    from app.modules.production.services.job_card_v2 import cancel_job_card
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await cancel_job_card(
                conn,
                job_card_id=job_card_id,
                reason=body.reason,
                deleted_by=user.full_name or user.phone,
            )
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") in ("missing_reason", "invalid_status"):
        raise HTTPException(status_code=400, detail=result["message"])
    return result


# ─── Close (with plan auto-close) ──────────────────────────────────────────

class CloseJobCardV2Request(BaseModel):
    """PUT /job-cards-v2/{id}/close"""
    allow_partial: bool = False     # admin override of sign-off check


@router.post("/job-cards-v2/{job_card_id}/backfill-indents")
async def backfill_indents_v2(
    request: Request,
    job_card_id: int,
    user=Depends(get_current_user),
):
    """Retro-materialise RM/PM indent rows for a v2 JC that was created
    before indent materialisation was wired into ``create_job_cards_from_plan``.

    Idempotent: any side (RM or PM) that already has indent rows is
    skipped. Stage 1 fills RM rows; the final stage fills PM rows;
    intermediate stages produce nothing because they consume upstream
    WIP via dispatch_to_next, not a fresh issuance.

    Returns ``{job_card_id, rm_added, pm_added, rm_skipped, pm_skipped}``
    or HTTP 404 when the JC doesn't exist.
    """
    from app.modules.production.services.job_card_v2 import backfill_indents_for_jc
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await backfill_indents_for_jc(conn, job_card_id)
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    return result


@router.put("/job-cards-v2/{job_card_id}/close")
async def close_job_card_v2(
    request: Request,
    job_card_id: int,
    body: CloseJobCardV2Request | None = None,
    user=Depends(get_current_user),
):
    """Close a v2 job card. Refuses on missing sign-offs or an open shift
    segment. After a successful close, if every JC on the linked plan has
    reached a terminal state, the plan is auto-flipped to 'executed'.

    `allow_partial=true` skips the sign-off check — admin-only.
    """
    from app.modules.production.services.job_card_v2 import (
        close_job_card, maybe_close_plan_from_jcs,
    )
    allow_partial = bool(body and body.allow_partial)
    if allow_partial and not user.is_admin:
        raise HTTPException(status_code=403, detail="allow_partial requires admin")

    pool = request.app.state.db_pool
    plan_closed = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await close_job_card(
                conn, job_card_id=job_card_id, allow_partial=allow_partial,
            )
            if "error" not in result and result.get("plan_id") is not None:
                try:
                    plan_closed = await maybe_close_plan_from_jcs(conn, result["plan_id"])
                except Exception:
                    logger.exception("maybe_close_plan_from_jcs failed (jc_id=%d) — JC close stands", job_card_id)

    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") == "terminal_state":
        raise HTTPException(status_code=400,
                            detail=f"JC is already {result['current_status']}")
    if result.get("error") == "open_shift":
        raise HTTPException(status_code=400, detail=result["message"])
    if result.get("error") == "missing_sign_offs":
        raise HTTPException(status_code=400,
                            detail={"error": "missing_sign_offs", "missing": result["missing"]})
    if plan_closed:
        result["plan_auto_closed"] = True
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  v2 QUALITY ANNEXURES — Metal / Weight / Environment / Loss / Remarks
# ═══════════════════════════════════════════════════════════════════════════


async def _assert_jc_writable_by_user(conn, *, job_card_id: int, user):
    """Floor/factory scope guard applied to every v2 annexure write.

    The list endpoint already filters JCs to the user's allowed scope on
    READ. WRITES need their own guard because the JC ID can be guessed
    or typed directly; without this, a floor-locked operator could
    submit annexure rows against any JC.
    """
    if getattr(user, "is_admin", False):
        return  # admin bypass
    from app.modules.production.services.jc_annexures_v2 import assert_jc_in_scope
    result = await assert_jc_in_scope(
        conn,
        job_card_id=job_card_id,
        allowed_warehouses=getattr(user, "allowed_warehouses", []) or None,
        allowed_floors=getattr(user, "allowed_floors", []) or None,
    )
    if result.get("error") == "job_card_not_found":
        raise HTTPException(status_code=404, detail="Job card not found")
    if result.get("error") == "out_of_scope":
        raise HTTPException(
            status_code=403,
            detail=f"User not assigned to {result['scope']} '{result['value']}'",
        )
#
# Each annexure exposes POST (add row), PATCH (update row), DELETE (soft-
# delete with reason). All payload bodies are flat — the service module
# filters to its allow-list and stamps the audit columns.

# ─── Annexure A — Metal Detection ───────────────────────────────────────

class MetalDetectionAddRequest(BaseModel):
    check_type:   Literal['pre_packaging', 'post_packaging']
    fe_pass:      bool | None = None
    nfe_pass:     bool | None = None
    ss_pass:      bool | None = None
    failed_units: int = 0
    remarks:      str | None = None


class MetalDetectionPatchRequest(BaseModel):
    check_type:   Literal['pre_packaging', 'post_packaging'] | None = None
    fe_pass:      bool | None = None
    nfe_pass:     bool | None = None
    ss_pass:      bool | None = None
    failed_units: int  | None = None
    remarks:      str  | None = None


class AnnexureDeleteRequest(BaseModel):
    """Shared body for all annexure DELETE endpoints."""
    reason: str | None = None


@router.post("/job-cards-v2/{job_card_id}/metal-detection")
async def add_metal_detection_v2(
    request: Request, job_card_id: int,
    body: MetalDetectionAddRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import add_metal_detection
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await add_metal_detection(
                conn, job_card_id=job_card_id,
                check_type=body.check_type, fe_pass=body.fe_pass,
                nfe_pass=body.nfe_pass, ss_pass=body.ss_pass,
                failed_units=body.failed_units, remarks=body.remarks,
                recorded_by=user.full_name or user.phone,
            )
    _raise_if_locked(r)
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.patch("/job-cards-v2/{job_card_id}/metal-detection/{detection_id}")
async def patch_metal_detection_v2(
    request: Request, job_card_id: int, detection_id: int,
    body: MetalDetectionPatchRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import patch_metal_detection
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await patch_metal_detection(
                conn, detection_id=detection_id, job_card_id=job_card_id,
                fields=body.model_dump(exclude_unset=True),
                updated_by=user.full_name or user.phone,
            )
    if r.get("error") == "not_found": raise HTTPException(status_code=404, detail="Row not found")
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.api_route(
    "/job-cards-v2/{job_card_id}/metal-detection/{detection_id}",
    methods=["DELETE"],
)
async def delete_metal_detection_v2(
    request: Request, job_card_id: int, detection_id: int,
    body: AnnexureDeleteRequest | None = None, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import delete_metal_detection
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await delete_metal_detection(
                conn, detection_id=detection_id, job_card_id=job_card_id,
                deleted_by=user.full_name or user.phone,
                reason=body.reason if body else None,
            )
    if r.get("error") == "not_found_or_already_deleted":
        raise HTTPException(status_code=404, detail="Row not found or already deleted")
    return r


# ─── Annexure B — Weight Checks ─────────────────────────────────────────

class WeightCheckAddRequest(BaseModel):
    sample_number:   int
    net_weight:      float | None = None
    gross_weight:    float | None = None
    leak_test_pass:  bool  | None = None
    remarks:         str   | None = None


class WeightCheckPatchRequest(BaseModel):
    sample_number:   int   | None = None
    net_weight:      float | None = None
    gross_weight:    float | None = None
    leak_test_pass:  bool  | None = None
    remarks:         str   | None = None


@router.post("/job-cards-v2/{job_card_id}/weight-checks")
async def add_weight_check_v2(
    request: Request, job_card_id: int,
    body: WeightCheckAddRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import add_weight_check
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await add_weight_check(
                conn, job_card_id=job_card_id, **body.model_dump(),
                recorded_by=user.full_name or user.phone,
            )
    _raise_if_locked(r)
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.patch("/job-cards-v2/{job_card_id}/weight-checks/{check_id}")
async def patch_weight_check_v2(
    request: Request, job_card_id: int, check_id: int,
    body: WeightCheckPatchRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import patch_weight_check
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await patch_weight_check(
                conn, check_id=check_id, job_card_id=job_card_id,
                fields=body.model_dump(exclude_unset=True),
                updated_by=user.full_name or user.phone,
            )
    if r.get("error") == "not_found": raise HTTPException(status_code=404, detail="Row not found")
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.api_route(
    "/job-cards-v2/{job_card_id}/weight-checks/{check_id}", methods=["DELETE"],
)
async def delete_weight_check_v2(
    request: Request, job_card_id: int, check_id: int,
    body: AnnexureDeleteRequest | None = None, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import delete_weight_check
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await delete_weight_check(
                conn, check_id=check_id, job_card_id=job_card_id,
                deleted_by=user.full_name or user.phone,
                reason=body.reason if body else None,
            )
    if r.get("error") == "not_found_or_already_deleted":
        raise HTTPException(status_code=404, detail="Row not found or already deleted")
    return r


# ─── Annexure C — Environment ────────────────────────────────────────────

class EnvironmentAddRequest(BaseModel):
    parameter_name: str
    value:          str | None = None
    unit:           str | None = None
    remarks:        str | None = None


class EnvironmentPatchRequest(BaseModel):
    parameter_name: str | None = None
    value:          str | None = None
    unit:           str | None = None
    remarks:        str | None = None


@router.post("/job-cards-v2/{job_card_id}/environment")
async def add_environment_v2(
    request: Request, job_card_id: int,
    body: EnvironmentAddRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import add_environment
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await add_environment(
                conn, job_card_id=job_card_id, **body.model_dump(),
                recorded_by=user.full_name or user.phone,
            )
    _raise_if_locked(r)
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.patch("/job-cards-v2/{job_card_id}/environment/{env_id}")
async def patch_environment_v2(
    request: Request, job_card_id: int, env_id: int,
    body: EnvironmentPatchRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import patch_environment
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await patch_environment(
                conn, env_id=env_id, job_card_id=job_card_id,
                fields=body.model_dump(exclude_unset=True),
                updated_by=user.full_name or user.phone,
            )
    if r.get("error") == "not_found": raise HTTPException(status_code=404, detail="Row not found")
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.api_route(
    "/job-cards-v2/{job_card_id}/environment/{env_id}", methods=["DELETE"],
)
async def delete_environment_v2(
    request: Request, job_card_id: int, env_id: int,
    body: AnnexureDeleteRequest | None = None, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import delete_environment
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await delete_environment(
                conn, env_id=env_id, job_card_id=job_card_id,
                deleted_by=user.full_name or user.phone,
                reason=body.reason if body else None,
            )
    if r.get("error") == "not_found_or_already_deleted":
        raise HTTPException(status_code=404, detail="Row not found or already deleted")
    return r


# ─── Annexure D — Loss Reconciliation ────────────────────────────────────

class LossReconAddRequest(BaseModel):
    loss_category:     Literal[
        'sorting_rejection', 'roasting_loss', 'packaging_rejection',
        'metal_detector', 'spillage', 'qc_sample', 'other']
    budgeted_loss_pct: float | None = None
    budgeted_loss_qty: float | None = None
    actual_loss_qty:   float | None = None
    uom:               str   | None = 'KGS'
    remarks:           str   | None = None


class LossReconPatchRequest(BaseModel):
    loss_category:     Literal[
        'sorting_rejection', 'roasting_loss', 'packaging_rejection',
        'metal_detector', 'spillage', 'qc_sample', 'other'] | None = None
    budgeted_loss_pct: float | None = None
    budgeted_loss_qty: float | None = None
    actual_loss_qty:   float | None = None
    uom:               str   | None = None
    remarks:           str   | None = None


@router.post("/job-cards-v2/{job_card_id}/loss-reconciliation")
async def add_loss_recon_v2(
    request: Request, job_card_id: int,
    body: LossReconAddRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import add_loss_reconciliation
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await add_loss_reconciliation(
                conn, job_card_id=job_card_id, **body.model_dump(),
                recorded_by=user.full_name or user.phone,
            )
    _raise_if_locked(r)
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.patch("/job-cards-v2/{job_card_id}/loss-reconciliation/{recon_id}")
async def patch_loss_recon_v2(
    request: Request, job_card_id: int, recon_id: int,
    body: LossReconPatchRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import patch_loss_reconciliation
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await patch_loss_reconciliation(
                conn, recon_id=recon_id, job_card_id=job_card_id,
                fields=body.model_dump(exclude_unset=True),
                updated_by=user.full_name or user.phone,
            )
    if r.get("error") == "not_found": raise HTTPException(status_code=404, detail="Row not found")
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.api_route(
    "/job-cards-v2/{job_card_id}/loss-reconciliation/{recon_id}", methods=["DELETE"],
)
async def delete_loss_recon_v2(
    request: Request, job_card_id: int, recon_id: int,
    body: AnnexureDeleteRequest | None = None, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import delete_loss_reconciliation
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await delete_loss_reconciliation(
                conn, recon_id=recon_id, job_card_id=job_card_id,
                deleted_by=user.full_name or user.phone,
                reason=body.reason if body else None,
            )
    if r.get("error") == "not_found_or_already_deleted":
        raise HTTPException(status_code=404, detail="Row not found or already deleted")
    return r


# ─── Annexure E — Remarks ────────────────────────────────────────────────

class RemarkAddRequest(BaseModel):
    remark_type: Literal['observation', 'deviation', 'corrective_action']
    content:     str


class RemarkPatchRequest(BaseModel):
    remark_type: Literal['observation', 'deviation', 'corrective_action'] | None = None
    content:     str | None = None


@router.post("/job-cards-v2/{job_card_id}/remarks")
async def add_remark_v2(
    request: Request, job_card_id: int,
    body: RemarkAddRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import add_remark
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await add_remark(
                conn, job_card_id=job_card_id,
                remark_type=body.remark_type, content=body.content,
                recorded_by=user.full_name or user.phone,
            )
    _raise_if_locked(r)
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.patch("/job-cards-v2/{job_card_id}/remarks/{remark_id}")
async def patch_remark_v2(
    request: Request, job_card_id: int, remark_id: int,
    body: RemarkPatchRequest, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import patch_remark
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await patch_remark(
                conn, remark_id=remark_id, job_card_id=job_card_id,
                fields=body.model_dump(exclude_unset=True),
                updated_by=user.full_name or user.phone,
            )
    if r.get("error") == "not_found": raise HTTPException(status_code=404, detail="Row not found")
    if r.get("error"): raise HTTPException(status_code=400, detail=r)
    return r


@router.api_route(
    "/job-cards-v2/{job_card_id}/remarks/{remark_id}", methods=["DELETE"],
)
async def delete_remark_v2(
    request: Request, job_card_id: int, remark_id: int,
    body: AnnexureDeleteRequest | None = None, user=Depends(get_current_user),
):
    from app.modules.production.services.jc_annexures_v2 import delete_remark
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _assert_jc_writable_by_user(conn, job_card_id=job_card_id, user=user)
            r = await delete_remark(
                conn, remark_id=remark_id, job_card_id=job_card_id,
                deleted_by=user.full_name or user.phone,
                reason=body.reason if body else None,
            )
    if r.get("error") == "not_found_or_already_deleted":
        raise HTTPException(status_code=404, detail="Row not found or already deleted")
    return r


# ═══════════════════════════════════════════════════════════════════════════
#  v2 PDF + read endpoints + material receipt
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/job-cards-v2/{job_card_id}/pdf")
async def job_card_pdf_v2(
    request: Request,
    job_card_id: int,
    mode: Literal['bom', 'full'] = Query('full'),
    user=Depends(get_current_user),
):
    """Render a v2 job card to PDF using the same fpdf renderer as v1.
    The renderer reads `section_1_product` / `section_3_team` / etc. from
    the JC dict — get_job_card (v2) already populates those legacy keys
    so the renderer works without code changes.

    B13 cost-metric gate: strip currency-bearing keys from the dict the
    renderer reads BEFORE the PDF is generated. The memory note flags
    PDFs as an easy oversight surface — if a deny-listed role can hit
    this URL, the rendered PDF must not embed cost figures."""
    from app.modules.production.services.job_card_v2 import get_job_card
    from app.modules.production.services.job_card_pdf import generate_job_card_pdf
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc_data = await get_job_card(conn, job_card_id)
    if jc_data is None:
        raise HTTPException(status_code=404, detail="Job card not found")
    # Enforce the SAME factory/floor scope as GET /job-cards-v2/{id} — otherwise a
    # caller 403'd on the JSON detail could still pull the full JC via the PDF.
    if not getattr(user, "is_admin", False):
        if user.allowed_warehouses and not user_has_warehouse(user.allowed_warehouses, jc_data.get("factory")):
            raise HTTPException(status_code=403, detail="JC outside your factory scope")
        if user.allowed_floors and jc_data.get("floor") and jc_data["floor"] not in user.allowed_floors:
            raise HTTPException(status_code=403, detail="JC outside your floor scope")
    # H1: strip cost fields before the renderer reads them. Deep-copies
    # the dict so callers (including the v1 fallback below) see an
    # untouched original.
    jc_data = strip_cost_fields(
        jc_data,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )
    pdf_bytes = generate_job_card_pdf(jc_data, mode=mode)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="JC-{job_card_id}-{mode}.pdf"'},
    )


@router.get("/job-cards/{job_card_id}/pdf")
async def job_card_pdf_v1(
    request: Request,
    job_card_id: int,
    mode: Literal['bom', 'full'] = Query('full'),
    user=Depends(get_current_user),
):
    """v1 PDF for legacy job_card rows. Auth-required — when the row
    isn't in v1 (because it's a v2 JC and the caller hit this URL by
    mistake), falls back to the v2 detail so the PDF still renders.

    B13 cost-metric gate applied here too — both the v1 detail dict and
    the v2 fallback dict get stripped before the renderer reads them."""
    from app.modules.production.services.job_card_engine import get_job_card_detail
    from app.modules.production.services.job_card_v2 import get_job_card as get_jc_v2
    from app.modules.production.services.job_card_pdf import generate_job_card_pdf
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            jc_data = await get_job_card_detail(conn, job_card_id)
        except Exception:
            jc_data = None
        if not jc_data:
            jc_data = await get_jc_v2(conn, job_card_id)
    if jc_data is None:
        raise HTTPException(status_code=404, detail="Job card not found")
    # H1: strip cost fields BEFORE the renderer reads them. Applies to
    # whichever branch produced jc_data (v1 detail or v2 fallback).
    jc_data = strip_cost_fields(
        jc_data,
        getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )
    pdf_bytes = generate_job_card_pdf(jc_data, mode=mode)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="JC-{job_card_id}-{mode}.pdf"'},
    )


# ── SFG WIP boxes & QR labels (Slice 6) ──────────────────────────────────
class WipBoxItem(BaseModel):
    net_weight: float
    gross_weight: float | None = None
    batch_code: str | None = None   # FE "Batch" → sfg_box.batch_code (the batch's display name)
    batch_id: int | None = None     # FE batch dropdown → sfg_box.batch_id (job_card_batch_v2, 8-digit bigint)
    units: int | None = None        # FE "Count"


class CreateWipBoxesRequest(BaseModel):
    boxes: list[WipBoxItem]
    expected_net_kg: float | None = None


class UpdateWipBoxItem(BaseModel):
    box_id: str                     # carton_id of the existing box to edit
    net_weight: float
    gross_weight: float | None = None
    batch_code: str | None = None
    batch_id: int | None = None
    units: int | None = None
    mark_printed: bool = False      # per-box print action: also flip PENDING → PRINTED


class UpdateWipBoxesRequest(BaseModel):
    boxes: list[UpdateWipBoxItem]


class ScanSfgBoxesRequest(BaseModel):
    box_ids: list[str]              # box QR payloads are now "<8-digit>-<counter>" TEXT


class FgCartonItem(BaseModel):
    net_weight: float
    units: int | None = None
    gross_weight: float | None = None


class CreateFgCartonsRequest(BaseModel):
    cartons: list[FgCartonItem]
    batch_id: int | None = None
    batch_code: str | None = None
    expected_net_kg: float | None = None


@router.post("/job-cards-v2/{job_card_id}/wip-boxes")
async def create_wip_boxes_endpoint(
    request: Request, job_card_id: int, body: CreateWipBoxesRequest,
    user=Depends(get_current_user),
):
    """Split a WIP-stage JC's net SFG into weighed boxes; mint a
    "<8-digit-time-base>-<per-JC counter>" box_id (the QR payload) per box, the
    counter continuing from the JC's last box. Print labels via …/wip-boxes/labels.pdf."""
    from app.modules.production.services.sfg_box_service import create_wip_boxes
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await create_wip_boxes(
                conn, job_card_id, [b.model_dump() for b in body.boxes],
                expected_net_kg=body.expected_net_kg,
            )
    if "error" in result:
        code = 404 if result["error"] == "not_found" else 400
        raise HTTPException(status_code=code, detail=result.get("message", result["error"]))
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.put("/job-cards-v2/{job_card_id}/wip-boxes")
async def update_wip_boxes_endpoint(
    request: Request, job_card_id: int, body: UpdateWipBoxesRequest,
    user=Depends(get_current_user),
):
    """Edit mutable fields (net/gross weight, batch link, count) of already-saved
    SFG boxes. Only PRINTED boxes are editable; received/consumed ones are skipped."""
    from app.modules.production.services.sfg_box_service import update_wip_boxes
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await update_wip_boxes(
                conn, job_card_id, [b.model_dump() for b in body.boxes],
                changed_by=(user.full_name or user.phone),
            )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/{job_card_id}/wip-boxes")
async def list_wip_boxes_endpoint(
    request: Request, job_card_id: int, user=Depends(get_current_user),
):
    """List the boxes produced by a WIP-stage JC (+ Σ net weight)."""
    from app.modules.production.services.sfg_box_service import get_boxes_for_jc
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_boxes_for_jc(conn, job_card_id)
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/{job_card_id}/edit-log")
async def job_card_edit_log_endpoint(
    request: Request, job_card_id: int, user=Depends(get_current_user),
):
    """Whole-job-card change history (header/tabs + box data) from amendment_log.
    The frontend uses each row's field_name to paint the ever-edited value red."""
    from app.modules.production.services.amendment_service import list_jc_edit_log
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await list_jc_edit_log(conn, job_card_id)
    return {"job_card_id": job_card_id, "changes": rows}


@router.get("/job-cards-v2/{job_card_id}/wip-boxes/labels.pdf")
async def wip_box_labels_endpoint(
    request: Request, job_card_id: int, user=Depends(get_current_user),
):
    """One QR label per box for a WIP-stage JC (labels carry no cost figures)."""
    from app.modules.production.services.sfg_box_service import get_boxes_for_jc
    from app.modules.production.services.label_service import wip_box_labels_pdf
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_boxes_for_jc(conn, job_card_id)
    if not result.get("boxes"):
        raise HTTPException(status_code=404, detail="No boxes to label for this job card")
    pdf_bytes = wip_box_labels_pdf(result.get("boxes", []))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="SFG-boxes-{job_card_id}.pdf"'},
    )


@router.post("/job-cards-v2/{job_card_id}/scan-sfg-boxes")
async def scan_sfg_boxes_endpoint(
    request: Request, job_card_id: int, body: ScanSfgBoxesRequest,
    user=Depends(get_current_user),
):
    """Scan SFG box QR ids into a downstream consuming JC (verify SFG + source)."""
    from app.modules.production.services.sfg_box_service import scan_receive_sfg_box
    pool = request.app.state.db_pool
    scanned_by = getattr(user, "full_name", None) or getattr(user, "phone", None)
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await scan_receive_sfg_box(
                conn, job_card_id, body.box_ids, scanned_by=scanned_by
            )
    if "error" in result:
        code = 404 if result["error"] == "not_found" else 400
        raise HTTPException(status_code=code, detail=result.get("message", result["error"]))
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/sfg-boxes/{box_id}")
async def get_sfg_box_endpoint(
    request: Request, box_id: str, user=Depends(get_current_user),
):
    """Single SFG box lookup (mirror of GET /boxes/{box_id} for po_box)."""
    from app.modules.production.services.sfg_box_service import get_box
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        box = await get_box(conn, box_id)
    if not box:
        raise HTTPException(status_code=404, detail="Box not found")
    # Entity scope: every sibling SFG read enforces it — a scoped caller must not
    # read another entity's box by guessing its id. Admin / wildcard bypass.
    if not getattr(user, "is_admin", False):
        allowed_ent = getattr(user, "allowed_entities", []) or []
        if box.get("entity") and allowed_ent and box["entity"] not in allowed_ent:
            raise HTTPException(status_code=403, detail="Entity outside your scope")
    return strip_cost_fields(
        box, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


# ── Phase 7: box→box→lot genealogy reads ────────────────────────────────────

@router.get("/job-cards-v2/{job_card_id}/sfg-genealogy")
async def jc_sfg_genealogy_endpoint(
    request: Request, job_card_id: int, user=Depends(get_current_user),
):
    """Phase 7 — per-JC SFG box genealogy.

    Returns ``{"job_card_id": id, "produced": [box...], "consumed": [box...]}``
    where ``produced`` = boxes this JC minted (sfg_box.job_card_id) and
    ``consumed`` = boxes scanned INTO this JC (sfg_box.received_into_job_card_id),
    each consumed box also carrying ``source_job_card_id``. Cost-gated (labels
    carry no cost, but stripped defensively). Entity scope is enforced for
    non-admins (mirror of the sfg-inventory endpoint)."""
    from app.modules.production.services.sfg_box_service import get_jc_genealogy
    pool = request.app.state.db_pool
    is_admin = getattr(user, "is_admin", False)
    allowed_ent = [] if is_admin else (getattr(user, "allowed_entities", []) or [])
    async with pool.acquire() as conn:
        if not is_admin:
            jc_ent = await conn.fetchval(
                "SELECT entity FROM job_card_v2 WHERE job_card_id = $1", job_card_id
            )
            if jc_ent and allowed_ent and jc_ent not in allowed_ent:
                raise HTTPException(status_code=403, detail="Entity outside your scope")
        # Pass the scope so CONSUMED boxes from another entity are filtered out
        # (the JC-entity check above only gates the JC itself, not its inputs).
        result = await get_jc_genealogy(conn, job_card_id,
                                        allowed_entities=allowed_ent or None)
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/sfg-boxes/{box_id}/genealogy")
async def sfg_box_genealogy_endpoint(
    request: Request, box_id: str, user=Depends(get_current_user),
):
    """Phase 7 — single-box upstream ancestry chain.

    Returns ``{"box_id": id, "chain": [box+level...]}`` walking UPSTREAM:
    parent_box_id (box→box) + source_inventory_batch_id → producer JC → that JC's
    consumed boxes (lot/batch hop). ``level`` is 0 for this box and increases
    upstream; recursion is depth-capped. Cost-gated defensively; entity scope
    enforced for non-admins via the start box's entity."""
    from app.modules.production.services.sfg_box_service import get_box_genealogy
    pool = request.app.state.db_pool
    is_admin = getattr(user, "is_admin", False)
    allowed_ent = [] if is_admin else (getattr(user, "allowed_entities", []) or [])
    async with pool.acquire() as conn:
        if not is_admin:
            box_ent = await conn.fetchval(
                "SELECT entity FROM sfg_box WHERE carton_id = $1", box_id
            )
            if box_ent and allowed_ent and box_ent not in allowed_ent:
                raise HTTPException(status_code=403, detail="Entity outside your scope")
        # Pass the scope so UPSTREAM ancestor boxes from another entity are not
        # walked into (the start-box check above only gates the entry point).
        result = await get_box_genealogy(conn, box_id,
                                         allowed_entities=allowed_ent or None)
    if result is None:
        raise HTTPException(status_code=404, detail="Box not found")
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


# ── Canonical SFG catalogue search (typeahead for the Job-Card SFG field) ───

@router.get("/sfg-canonical/search")
async def sfg_canonical_search_endpoint(
    request: Request, q: str = "", entity: str | None = None, limit: int = 20,
    user=Depends(get_current_user),
):
    """Typeahead over the canonical SFG catalogue (``sfg_canonical_map``) for the
    Create/Edit Job-Card SFG-output autocomplete (SFG canonicalization design
    §5.4). Matches ``q`` against the canonical SFG name or the FG (article) name;
    returns distinct canonical SFG suggestions, entity-preferred."""
    from app.modules.production.services.sfg_canonical import search_canonical_sfg
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        results = await search_canonical_sfg(conn, q, entity, limit)
    return {"q": q, "results": results}


# ── FG cartons (packing stage) — sibling of the wip-boxes routes ────────────

@router.post("/job-cards-v2/{job_card_id}/fg-cartons")
async def create_fg_cartons_endpoint(
    request: Request, job_card_id: int, body: CreateFgCartonsRequest,
    user=Depends(get_current_user),
):
    """Pack a terminal FG/packing JC's output into cartons; mint an 8-digit
    carton_id (the QR payload) per carton. Print stickers via
    …/fg-cartons/labels.pdf."""
    from app.modules.production.services.sfg_box_service import create_fg_cartons
    pool = request.app.state.db_pool
    created_by = getattr(user, "full_name", None) or getattr(user, "phone", None)
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await create_fg_cartons(
                conn, job_card_id, [c.model_dump() for c in body.cartons],
                batch_id=body.batch_id, batch_code=body.batch_code,
                expected_net_kg=body.expected_net_kg, created_by=created_by,
            )
    if "error" in result:
        code = 404 if result["error"] == "not_found" else 400
        raise HTTPException(status_code=code, detail=result.get("message", result["error"]))
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/{job_card_id}/fg-cartons")
async def list_fg_cartons_endpoint(
    request: Request, job_card_id: int, user=Depends(get_current_user),
):
    """List the cartons packed by a terminal FG/packing JC (+ Σ net weight)."""
    from app.modules.production.services.sfg_box_service import get_cartons_for_jc
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_cartons_for_jc(conn, job_card_id)
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/{job_card_id}/fg-cartons/labels.pdf")
async def fg_carton_labels_endpoint(
    request: Request, job_card_id: int, user=Depends(get_current_user),
):
    """One QR sticker per carton for a packing JC (stickers carry no cost figures)."""
    from app.modules.production.services.sfg_box_service import get_cartons_for_jc
    from app.modules.production.services.label_service import fg_carton_labels_pdf
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_cartons_for_jc(conn, job_card_id)
    if not result.get("cartons"):
        raise HTTPException(status_code=404, detail="No cartons to label for this job card")
    pdf_bytes = fg_carton_labels_pdf(result.get("cartons", []))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="FG-cartons-{job_card_id}.pdf"'},
    )


@router.get("/fg-cartons/{carton_id}/label.pdf")
async def fg_carton_single_label_endpoint(
    request: Request, carton_id: str, user=Depends(get_current_user),
):
    """Single carton sticker. Entity-scoped for non-admins (mirror of the box reads)."""
    from app.modules.production.services.sfg_box_service import get_box
    from app.modules.production.services.label_service import fg_carton_label_pdf
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        carton = await get_box(conn, carton_id)
    if not carton or carton.get("item_type") != "fg":
        raise HTTPException(status_code=404, detail="Carton not found")
    if not getattr(user, "is_admin", False):
        allowed_ent = getattr(user, "allowed_entities", []) or []
        if carton.get("entity") and allowed_ent and carton["entity"] not in allowed_ent:
            raise HTTPException(status_code=403, detail="Entity outside your scope")
    pdf_bytes = fg_carton_label_pdf(carton)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="FG-carton-{carton_id}.pdf"'},
    )


@router.get("/fg-cartons/{carton_id}/genealogy")
async def fg_carton_genealogy_endpoint(
    request: Request, carton_id: str, user=Depends(get_current_user),
):
    """Carton upstream trace: carton → SFG boxes consumed into the packing JC →
    each box's box→lot lineage. ``level`` 0 = the carton; increases upstream."""
    from app.modules.production.services.sfg_box_service import get_carton_genealogy
    pool = request.app.state.db_pool
    is_admin = getattr(user, "is_admin", False)
    allowed_ent = [] if is_admin else (getattr(user, "allowed_entities", []) or [])
    async with pool.acquire() as conn:
        if not is_admin:
            ent = await conn.fetchval(
                "SELECT entity FROM sfg_box WHERE carton_id = $1 AND item_type = 'fg'",
                carton_id,
            )
            if ent and allowed_ent and ent not in allowed_ent:
                raise HTTPException(status_code=403, detail="Entity outside your scope")
        result = await get_carton_genealogy(conn, carton_id, allowed_entities=allowed_ent or None)
    if result is None:
        raise HTTPException(status_code=404, detail="Carton not found")
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/job-cards-v2/{job_card_id}/allocations")
async def get_allocations_v2(
    request: Request, job_card_id: int, user=Depends(get_current_user),
):
    """Store allocations recorded against this v2 JC's batch. Reads the
    shared store_allocation table by joining on batch_number."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow(
            "SELECT batch_number FROM job_card_v2 WHERE job_card_id = $1 AND deleted_at IS NULL",
            job_card_id,
        )
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        rows = await conn.fetch(
            "SELECT * FROM store_allocation WHERE batch_number = $1 ORDER BY allocated_at DESC",
            jc["batch_number"],
        )

    def _norm(row):
        from decimal import Decimal
        from datetime import datetime as _dt, date as _d
        out = {}
        for k, v in dict(row).items():
            if isinstance(v, Decimal):     out[k] = float(v)
            elif isinstance(v, (_d, _dt)): out[k] = v.isoformat()
            else:                          out[k] = v
        return out
    return {"job_card_id": job_card_id, "allocations": [_norm(r) for r in rows]}


@router.get("/job-cards-v2/{job_card_id}/floor-stock-status")
async def get_floor_stock_status_v2(
    request: Request, job_card_id: int, user=Depends(get_current_user),
):
    """Live floor-stock for this JC's batch. Excludes terminal statuses
    so the consumer sees what's still actionable on the floor."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow(
            "SELECT batch_number, floor FROM job_card_v2 WHERE job_card_id = $1 AND deleted_at IS NULL",
            job_card_id,
        )
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        rows = await conn.fetch(
            """
            SELECT * FROM floor_stock
            WHERE  batch_number = $1
              AND  (status IS NULL OR status NOT IN ('consumed','expired'))
            ORDER  BY received_at DESC
            """,
            jc["batch_number"],
        )

    def _norm(row):
        from decimal import Decimal
        from datetime import datetime as _dt, date as _d
        out = {}
        for k, v in dict(row).items():
            if isinstance(v, Decimal):     out[k] = float(v)
            elif isinstance(v, (_d, _dt)): out[k] = v.isoformat()
            else:                          out[k] = v
        return out
    return {
        "job_card_id":  job_card_id,
        "batch_number": jc["batch_number"],
        "floor":        jc["floor"],
        "floor_stock":  [_norm(r) for r in rows],
    }


# ─── Material receipt + acknowledgement (QR / manual) ──────────────────────

class ReceiveMaterialV2Request(BaseModel):
    """POST /job-cards-v2/{id}/receive-material"""
    box_ids: list[str]


class AcknowledgeMaterialV2Request(BaseModel):
    """POST /job-cards-v2/{id}/acknowledge-material"""
    indent_id:    int | None = None
    acknowledged: bool = True
    notes:        str | None = None


@router.post("/job-cards-v2/{job_card_id}/receive-material")
async def receive_material_v2(
    request: Request, job_card_id: int,
    body: ReceiveMaterialV2Request, user=Depends(get_current_user),
):
    """Attach QR-scanned boxes to this v2 JC's RM indent. Looks each
    box up in po_box, matches material_sku_name to the indent row,
    appends to scanned_box_ids and increments issued_qty. Flips JC to
    'material_received' if any box actually attached."""
    if not body.box_ids:
        raise HTTPException(status_code=400, detail="No box_ids supplied")

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            jc = await conn.fetchrow(
                "SELECT status FROM job_card_v2 WHERE job_card_id = $1 AND deleted_at IS NULL",
                job_card_id,
            )
            if not jc:
                raise HTTPException(status_code=404, detail="Job card not found")

            # Per-box atomicity: wrap each box's lookup+update in a
            # savepoint so a hard failure on box N (network blip,
            # constraint violation, etc.) doesn't roll back the boxes
            # that already attached successfully. Each box's outcome is
            # reported in the response regardless.
            attached: list[dict] = []
            for box_id in body.box_ids:
                try:
                    async with conn.transaction():   # SAVEPOINT
                        box = await conn.fetchrow(
                            "SELECT box_id, material_sku_name, qty_kg FROM po_box WHERE box_id = $1",
                            box_id,
                        )
                        if not box:
                            attached.append({"box_id": box_id, "error": "box_not_found"})
                            continue
                        # FOR UPDATE on the indent row so two concurrent
                        # receive-material calls can't both observe the
                        # same scanned_box_ids snapshot and double-issue.
                        indent = await conn.fetchrow(
                            """
                            SELECT rm_indent_id, scanned_box_ids
                            FROM   job_card_rm_indent_v2
                            WHERE  job_card_id = $1
                              AND  material_sku_name ILIKE $2
                            FOR UPDATE
                            """,
                            job_card_id, box["material_sku_name"],
                        )
                        if not indent:
                            attached.append({"box_id": box_id, "error": "no_matching_indent",
                                             "material_sku_name": box["material_sku_name"]})
                            continue
                        existing = list(indent["scanned_box_ids"] or [])
                        if box_id in existing:
                            attached.append({"box_id": box_id, "status": "already_scanned"})
                            continue
                        existing.append(box_id)
                        await conn.execute(
                            """
                            UPDATE job_card_rm_indent_v2
                               SET scanned_box_ids = $1,
                                   issued_qty      = COALESCE(issued_qty, 0) + $2,
                                   status          = CASE
                                                        WHEN issued_qty + $2 >= gross_qty THEN 'fulfilled'
                                                        WHEN issued_qty + $2 > 0          THEN 'partial'
                                                        ELSE status
                                                      END
                             WHERE rm_indent_id = $3
                            """,
                            existing, float(box["qty_kg"] or 0), indent["rm_indent_id"],
                        )
                        attached.append({"box_id": box_id, "status": "attached",
                                         "rm_indent_id": indent["rm_indent_id"]})
                except Exception as exc:
                    # Savepoint rolled back automatically by the `async
                    # with conn.transaction()` exit; report the failure
                    # for this box and continue with the next.
                    logger.exception("receive-material: box %s failed (jc_id=%d)", box_id, job_card_id)
                    attached.append({"box_id": box_id, "error": "save_failed",
                                     "message": str(exc)})

            if any(a.get("status") == "attached" for a in attached) and \
               jc["status"] in ('unlocked', 'assigned'):
                await conn.execute(
                    "UPDATE job_card_v2 SET status = 'material_received' WHERE job_card_id = $1",
                    job_card_id,
                )
    return {"received": True, "attached": attached}


@router.post("/job-cards-v2/{job_card_id}/acknowledge-material")
async def acknowledge_material_v2(
    request: Request, job_card_id: int,
    body: AcknowledgeMaterialV2Request, user=Depends(get_current_user),
):
    """Mark RM indent rows on this JC as fulfilled. With `indent_id`,
    acknowledges just that row; otherwise acknowledges every row on the
    JC. Moves JC to 'material_received' when applicable."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            jc = await conn.fetchrow(
                "SELECT status FROM job_card_v2 WHERE job_card_id = $1 AND deleted_at IS NULL",
                job_card_id,
            )
            if not jc:
                raise HTTPException(status_code=404, detail="Job card not found")

            if body.indent_id is not None:
                r = await conn.execute(
                    "UPDATE job_card_rm_indent_v2 SET status = 'fulfilled' "
                    "WHERE rm_indent_id = $1 AND job_card_id = $2",
                    body.indent_id, job_card_id,
                )
                if r == 'UPDATE 0':
                    raise HTTPException(status_code=404, detail="Indent not found on this JC")
            else:
                await conn.execute(
                    "UPDATE job_card_rm_indent_v2 SET status = 'fulfilled' "
                    "WHERE job_card_id = $1",
                    job_card_id,
                )

            if jc["status"] in ('unlocked', 'assigned'):
                await conn.execute(
                    "UPDATE job_card_v2 SET status = 'material_received' WHERE job_card_id = $1",
                    job_card_id,
                )

            # Persist the operator's note (if any) as a v2 remark so it
            # lands in the audit trail. Previously this field was
            # accepted by the Pydantic model but silently dropped by
            # the handler — the audit caught it.
            if body.notes and body.notes.strip():
                from app.modules.production.services.jc_annexures_v2 import add_remark
                content = "Ack: " + body.notes.strip()
                if body.indent_id is not None:
                    content += f" (indent {body.indent_id})"
                try:
                    await add_remark(
                        conn, job_card_id=job_card_id,
                        remark_type='observation', content=content,
                        recorded_by=user.full_name or user.phone,
                    )
                except Exception:
                    # Don't let an annexure write break the ack — the
                    # ack itself is the operator's primary action.
                    logger.exception("ack-material: failed to persist notes as remark (jc_id=%d)", job_card_id)
    return {"acknowledged": True, "job_card_id": job_card_id}


# ---------------------------------------------------------------------------
# Routing-Gap Resolution — close the remaining ~342 unrouted gap FG articles
# ---------------------------------------------------------------------------
# Reads the offline reconciliation gap union (Article_Master_FINAL.csv, gap_flags
# 403/238), excludes already-routed/promoted articles (the ~108 Slice-7 ones),
# groups the rest by a heuristic family classifier with a suggested process
# category, and applies production-confirmed assignments via the SAME promote_one
# primitive scripts/promote_fg_master_gaps.py uses. Read endpoints are cost-gated
# (master-data reads, like /job-cards-v2/sfg-master); apply is a planner/admin
# master-data write (production/plans/create — admin bypasses).

@router.get("/routing-gaps")
async def routing_gaps(
    request: Request,
    entity: str | None = Query(None, description="filter by cfpl/cdpl"),
    family: str | None = Query(None, description="filter by classify_family value"),
    user=Depends(get_current_user),
):
    """Grouped list of FG articles still missing a routing (Process Category).

    Shape: {"total": n, "families": [{"family", "suggested_process_category",
    "count", "needs_review", "articles": [{"article", "in_all_sku",
    "current_process_category", "suggested_process_category"}]}]}.
    The ~108 Slice-7 promoted articles are excluded (they now have a route)."""
    from app.modules.production.services.routing_gap_service import get_routing_gaps
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await get_routing_gaps(conn, entity=entity, family=family)
    return strip_cost_fields(
        result, getattr(user, "role_name", None),
        is_admin=getattr(user, "is_admin", False),
    )


@router.get("/routing-gaps/worksheet.csv")
async def routing_gaps_worksheet(
    request: Request,
    entity: str | None = Query(None),
    family: str | None = Query(None),
    user=Depends(get_current_user),
):
    """Download the outstanding gap list as a CSV worksheet for production to
    fill (columns: article, family, in_all_sku, suggested_process_category,
    assigned_process_category[blank]). Fill assigned_process_category, then POST
    the rows to /routing-gaps/apply."""
    from app.modules.production.services.routing_gap_service import build_worksheet_csv
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        csv_text = await build_worksheet_csv(conn, entity=entity, family=family)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=routing_gaps_worksheet.csv"},
    )


@router.post("/routing-gaps/apply")
async def routing_gaps_apply(
    request: Request,
    body: RoutingGapApplyRequest,
    user: AuthUser = Depends(
        require_permission("production", "plans", None, "create")
    ),
):
    """Apply confirmed routing assignments — upsert bom_header + derive routes
    (same primitive as the Slice-7 promotion). Idempotent + audited; one bad
    assignment doesn't abort the rest (per-article savepoint).

    Body: {"assignments": [{"article", "process_category"}], "performed_by"?}.
    A blank process_category is skipped (status skipped_no_pc).
    Returns: {"applied": n, "skipped": n, "results": [{"article", "status",
    "bom_id", "detail"}]} where status is promoted|routed_existing|skipped_no_pc|error."""
    from app.modules.production.services.routing_gap_service import promote_articles
    assignments = [a.model_dump() for a in body.assignments]
    performed_by = body.performed_by or getattr(user, "full_name", None) \
        or getattr(user, "phone", None)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await promote_articles(
                conn, assignments, performed_by=performed_by,
            )
    return result