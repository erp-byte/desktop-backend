"""Production Planning module router — fulfillment sync, AI plan generation, plan CRUD."""

import json
import logging
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/production", tags=["Production"])


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
    page_size: int = Query(50, ge=1, le=200),
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
):
    """Aggregated data for dashboard charts — not paginated."""
    from app.modules.production.services.fulfillment import get_chart_summary
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_chart_summary(
            conn, entity=entity, financial_year=financial_year,
            customer=customer, so_number=so_number, article=article, status=status,
        )


@router.get("/fulfillment/filter-options")
async def filter_options(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
):
    """Distinct values for Customer, SO Number, Article dropdowns."""
    from app.modules.production.services.fulfillment import get_filter_options
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_filter_options(conn, entity=entity, financial_year=financial_year)


@router.get("/fulfillment/customer-view")
async def customer_view(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
    customer: str = Query(None),
):
    """Customer-grouped fulfillment with BOM details, process route + floors, and inventory status."""
    from app.modules.production.services.fulfillment import get_enriched_fulfillment
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_enriched_fulfillment(
            conn, entity=entity, financial_year=financial_year, customer=customer,
        )


@router.get("/fulfillment/fy-review")
async def fy_review(
    request: Request,
    entity: str = Query(None),
    financial_year: str = Query(None),
):
    """All unfulfilled orders for FY close review."""
    from app.modules.production.services.fulfillment import get_fy_review
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_fy_review(conn, entity, financial_year)


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
# ---------------------------------------------------------------------------
# Plan creation with AI
# ---------------------------------------------------------------------------

class CreatePlanItem(BaseModel):
    fulfillment_id: int
    custom_qty_kg: float
    bom_overrides: list = []


class CreatePlanWithAIRequest(BaseModel):
    entity: str
    plan_type: str = "daily"
    plan_date: date | None = None
    plan_name: str = ""
    created_by: str = ""
    selected_items: list[CreatePlanItem]


@router.post("/plans/create-with-ai")
async def create_plan_with_ai(request: Request, body: CreatePlanWithAIRequest):
    """Generate a production plan using Claude AI from selected fulfillment items."""
    from app.modules.production.services.ai_planner import (
        collect_planning_context, call_claude, create_plan_from_ai, DAILY_PLAN_PROMPT,
    )

    if not body.selected_items:
        raise HTTPException(status_code=400, detail="No items selected")

    pool = request.app.state.db_pool
    settings = request.app.state.settings
    target_date = body.plan_date or date.today()
    fulfillment_ids = [item.fulfillment_id for item in body.selected_items]

    async with pool.acquire() as conn:
        context = await collect_planning_context(conn, body.entity, target_date, fulfillment_ids)
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
# Plan read endpoints (planning actions are via MCP only)
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
    page_size: int = Query(50, ge=1, le=200),
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
            date_from = date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_from format. Use YYYY-MM-DD.")
        conditions.append(f"plan_date >= ${idx}")
        params.append(date_from)
        idx += 1
    if date_to:
        try:
            date_to = date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_to format. Use YYYY-MM-DD.")
        conditions.append(f"plan_date <= ${idx}")
        params.append(date_to)
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
    page_size: int = Query(50, ge=1, le=200),
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
    page_size: int = Query(50, ge=1, le=200),
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
    page_size: int = Query(50, ge=1, le=200),
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
    dispatched_by: str


@router.get("/store/pending-allocations")
async def store_pending_allocations(
    request: Request,
    entity: str = Query(None),
    job_card_id: int = Query(None),
    material: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """List all pending allocation requests for store team."""
    from app.modules.production.services.store_controller import get_pending_allocations
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await get_pending_allocations(
            conn, entity=entity, job_card_id=job_card_id,
            material=material, page=page, page_size=page_size,
        )


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
        async with conn.transaction():
            return await decide_allocation(
                conn, [d.model_dump() for d in body.decisions],
                body.decided_by, entity,
            )


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
    if body.qty_kg <= 0:
        raise HTTPException(status_code=422, detail="qty_kg must be > 0")
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await dispatch_partial_to_next_stage(
                conn, job_card_id, body.qty_kg, body.dispatched_by, entity,
            )
            if "error" in result:
                raise HTTPException(status_code=400, detail=result["error"])
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
    page_size: int = Query(50, ge=1, le=200),
):
    """List job cards with filters."""
    pool = request.app.state.db_pool
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',')]
        ph = ', '.join(f'${idx+i}' for i in range(len(statuses)))
        conditions.append(f"status IN ({ph})"); params.extend(statuses); idx += len(statuses)
    if team_leader:
        conditions.append(f"assigned_to_team_leader ILIKE ${idx}"); params.append(f"%{team_leader}%"); idx += 1
    if floor:
        conditions.append(f"floor ILIKE ${idx}"); params.append(f"%{floor}%"); idx += 1
    if factory:
        conditions.append(f"factory ILIKE ${idx}"); params.append(f"%{factory}%"); idx += 1
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

        # Get distinct values for filter dropdowns
        filter_options = {}
        filter_options['customers'] = [r['customer_name'] for r in await conn.fetch(
            "SELECT DISTINCT customer_name FROM job_card WHERE customer_name IS NOT NULL ORDER BY customer_name")]
        filter_options['team_leaders'] = [r['assigned_to_team_leader'] for r in await conn.fetch(
            "SELECT DISTINCT assigned_to_team_leader FROM job_card WHERE assigned_to_team_leader IS NOT NULL ORDER BY assigned_to_team_leader")]
        filter_options['floors'] = [r['floor'] for r in await conn.fetch(
            "SELECT DISTINCT floor FROM job_card WHERE floor IS NOT NULL ORDER BY floor")]
        filter_options['factories'] = [r['factory'] for r in await conn.fetch(
            "SELECT DISTINCT factory FROM job_card WHERE factory IS NOT NULL ORDER BY factory")]
        filter_options['stages'] = [r['stage'] for r in await conn.fetch(
            "SELECT DISTINCT stage FROM job_card WHERE stage IS NOT NULL ORDER BY stage")]

    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "total_pages": (total + page_size - 1) // page_size if total else 0},
        "filter_options": filter_options,
    }


@router.get("/job-cards/team-dashboard")
async def team_dashboard(
    request: Request,
    team_leader: str = Query(...),
    entity: str = Query(None),
):
    """Job cards assigned to a specific team leader, priority-sorted."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        conditions = ["assigned_to_team_leader ILIKE $1"]
        params = [f"%{team_leader}%"]
        idx = 2
        if entity:
            conditions.append(f"entity = ${idx}"); params.append(entity)
        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"""SELECT * FROM job_card WHERE {where}
                AND status NOT IN ('closed', 'completed')
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
    floor: str = Query(...),
    entity: str = Query(None),
):
    """All job cards on a specific floor."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        conditions = ["floor ILIKE $1"]
        params = [f"%{floor}%"]
        idx = 2
        if entity:
            conditions.append(f"entity = ${idx}"); params.append(entity)
        where = " AND ".join(conditions)
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
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await assign_job_card(conn, job_card_id, body.team_leader, body.team_members)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.post("/job-cards/{job_card_id}/receive-material")
async def receive_material(request: Request, job_card_id: int, body: ReceiveMaterialRequest):
    from app.modules.production.services.qr_service import receive_material_via_qr
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity FROM job_card WHERE job_card_id = $1", job_card_id)
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        async with conn.transaction():
            return await receive_material_via_qr(conn, job_card_id, body.box_ids, jc['entity'])


class ManualAckRequest(BaseModel):
    indent_lines: list[dict] | None = None  # [{ indent_type, indent_id }] — None = acknowledge all
    acknowledged_by: str


@router.post("/job-cards/{job_card_id}/acknowledge-material")
async def acknowledge_material(request: Request, job_card_id: int, body: ManualAckRequest):
    from app.modules.production.services.qr_service import manual_acknowledge_material, manual_acknowledge_all
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity FROM job_card WHERE job_card_id = $1", job_card_id)
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        async with conn.transaction():
            if body.indent_lines:
                result = await manual_acknowledge_material(conn, job_card_id, body.indent_lines, body.acknowledged_by, jc['entity'])
            else:
                result = await manual_acknowledge_all(conn, job_card_id, body.acknowledged_by, jc['entity'])
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.put("/job-cards/{job_card_id}/start")
async def start_jc(request: Request, job_card_id: int):
    from app.modules.production.services.job_card_engine import start_job_card
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await start_job_card(conn, job_card_id)
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
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity FROM job_card WHERE job_card_id = $1", job_card_id)
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        async with conn.transaction():
            result = await complete_job_card(conn, job_card_id, jc['entity'])
    if "error" in result:
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.put("/job-cards/{job_card_id}/sign-off")
async def sign_off_jc(request: Request, job_card_id: int, body: SignOffRequest):
    from app.modules.production.services.job_card_engine import sign_off
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await sign_off(conn, job_card_id, body.sign_off_type, body.name)


@router.put("/job-cards/{job_card_id}/close")
async def close_jc(request: Request, job_card_id: int):
    from app.modules.production.services.job_card_engine import close_job_card
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await close_job_card(conn, job_card_id)
    if "error" in result:
        if result["error"] == "missing_sign_offs":
            raise HTTPException(status_code=400, detail=f"Missing sign-offs: {result['missing']}")
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.put("/job-cards/{job_card_id}/force-unlock")
async def force_unlock_jc(request: Request, job_card_id: int, body: ForceUnlockRequest):
    from app.modules.production.services.job_card_engine import force_unlock
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity FROM job_card WHERE job_card_id = $1", job_card_id)
        if not jc:
            raise HTTPException(status_code=404, detail="Job card not found")
        async with conn.transaction():
            result = await force_unlock(conn, job_card_id, body.authority, body.reason, jc['entity'])
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
    page_size: int = Query(50, ge=1, le=200),
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
    page_size: int = Query(50, ge=1, le=200),
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
    page_size: int = Query(50, ge=1, le=200),
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
        async with conn.transaction():
            return await report_discrepancy(
                conn, discrepancy_type=body.discrepancy_type, severity=body.severity,
                affected_material=body.affected_material, affected_machine_id=body.affected_machine_id,
                details=body.details, reported_by=body.reported_by, entity=body.entity,
            )


@router.get("/discrepancy")
async def list_discrepancies(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    discrepancy_type: str = Query(None),
    severity: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
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
    page_size: int = Query(20, ge=1, le=100),
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
    page_size: int = Query(50, ge=1, le=200),
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
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await raise_purchase_indent(conn, **body.model_dump())


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
        async with conn.transaction():
            return await submit_inspection(conn, inspection_id, **body.model_dump())


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


@router.post("/rtv/dispositions")
async def assign_rtv_disposition(request: Request, body: RtvDispositionBody):
    from app.modules.production.services.rtv_disposition_service import assign_disposition
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await assign_disposition(conn, **body.model_dump())


class DiscardBody(BaseModel):
    rtv_id: str
    reason: str
    authorised_by: str


@router.post("/rtv/discard")
async def approve_discard(request: Request, body: DiscardBody):
    from app.modules.production.services.rtv_disposition_service import approve_discard
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
    page_size: int = Query(50, ge=1, le=200),
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


class BalanceMaterialV2(BaseModel):
    material_id: int | None = None
    material_name: str
    balance_type: str               # extra_given | returned | wastage | control_sample
    qty_kg: float
    remarks: str | None = None


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
    rm_consumed_kg: float | None = None
    process_loss_kg: float = 0.0
    byproducts: list[ByproductLineV2] = []
    balance_materials: list[BalanceMaterialV2] = []
    qc: QCDataV2 | None = None


@router.post("/job-cards/{job_card_id}/output")
async def record_output_v2(request: Request, job_card_id: int, body: JobCardOutputV2Request):
    """V2 consolidated: record FG output, byproducts, balance materials, and QC in one atomic call."""
    from app.modules.production.services.job_card_engine import record_output_v2 as _record
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await _record(conn, job_card_id, body.model_dump())
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
            "SELECT material_id, material_name, balance_type, qty_kg, remarks FROM job_card_balance_material WHERE job_card_id = $1",
            job_card_id,
        )
        loss_recon = await conn.fetch(
            """
            SELECT loss_category, budgeted_loss_pct, budgeted_loss_kg, actual_loss_kg, variance_kg, remarks
            FROM job_card_loss_reconciliation WHERE job_card_id = $1 ORDER BY loss_category
            """,
            job_card_id,
        )
        qc = await conn.fetchrow(
            "SELECT result, findings, corrective_action, inspector_user, inspection_date FROM qc_inspection WHERE job_card_id = $1 ORDER BY created_at DESC LIMIT 1",
            job_card_id,
        )

    return {
        "output": dict(output),
        "byproducts": [dict(r) for r in byproducts],
        "balance_materials": [dict(r) for r in balance_materials],
        "loss_reconciliation": [dict(r) for r in loss_recon],
        "qc": dict(qc) if qc else None,
    }