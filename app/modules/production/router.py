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


class GeneratePlanRequest(BaseModel):
    entity: str
    date: date
    fulfillment_ids: list[int]


class GenerateWeeklyPlanRequest(BaseModel):
    entity: str
    date_from: date
    date_to: date
    fulfillment_ids: list[int]


class ManualPlanRequest(BaseModel):
    entity: str
    plan_name: str
    plan_type: str = "daily"
    date_from: date
    date_to: date


class PlanLineEdit(BaseModel):
    fg_sku_name: str | None = None
    customer_name: str | None = None
    bom_id: int | None = None
    planned_qty_kg: float | None = None
    planned_qty_units: int | None = None
    machine_id: int | None = None
    priority: int | None = None
    shift: str | None = None
    stage_sequence: list[str] | None = None
    estimated_hours: float | None = None
    reasoning: str | None = None
    status: str | None = None


class PlanLineAdd(BaseModel):
    fg_sku_name: str
    customer_name: str | None = None
    bom_id: int | None = None
    planned_qty_kg: float
    planned_qty_units: int | None = None
    machine_id: int | None = None
    priority: int = 5
    shift: str = "day"


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
            customer=customer, search=search, page=page, page_size=page_size,
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
# Plan generation endpoints
# ---------------------------------------------------------------------------

@router.post("/plans/generate-daily")
async def generate_daily_plan(request: Request, body: GeneratePlanRequest):
    """Generate a daily production plan using Claude AI for selected fulfillment IDs."""
    from app.modules.production.services.ai_planner import (
        collect_planning_context, call_claude, create_plan_from_ai, DAILY_PLAN_PROMPT,
    )
    pool = request.app.state.db_pool
    settings = request.app.state.settings

    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    async with pool.acquire() as conn:
        context = await collect_planning_context(conn, body.entity, body.date, body.fulfillment_ids)

    if not context["demand"]:
        return {"error": "no_demand", "no_bom_items": context.get("no_bom_items", []),
                "message": "No valid demand items found for the selected fulfillment IDs"}

    ai_result = await call_claude(settings, DAILY_PLAN_PROMPT, context)

    async with pool.acquire() as conn:
        async with conn.transaction():
            plan = await create_plan_from_ai(conn, body.entity, "daily", body.date, body.date, ai_result, settings)

    plan["no_bom_items"] = context.get("no_bom_items", [])
    return plan


@router.post("/plans/generate-weekly")
async def generate_weekly_plan(request: Request, body: GenerateWeeklyPlanRequest):
    """Generate a weekly production plan using Claude AI."""
    from app.modules.production.services.ai_planner import (
        collect_planning_context, call_claude, create_plan_from_ai, WEEKLY_PLAN_PROMPT,
    )
    pool = request.app.state.db_pool
    settings = request.app.state.settings

    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    async with pool.acquire() as conn:
        context = await collect_planning_context(conn, body.entity, body.date_from, body.fulfillment_ids)

    if not context["demand"]:
        return {"error": "no_demand", "no_bom_items": context.get("no_bom_items", [])}

    ai_result = await call_claude(settings, WEEKLY_PLAN_PROMPT, context)

    async with pool.acquire() as conn:
        async with conn.transaction():
            plan = await create_plan_from_ai(conn, body.entity, "weekly", body.date_from, body.date_to, ai_result, settings)

    plan["no_bom_items"] = context.get("no_bom_items", [])
    return plan


# ---------------------------------------------------------------------------
# Plan CRUD endpoints
# ---------------------------------------------------------------------------

@router.post("/plans/create")
async def create_manual_plan(request: Request, body: ManualPlanRequest):
    """Create an empty manual plan (no AI)."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        plan_id = await conn.fetchval(
            """
            INSERT INTO production_plan (plan_name, entity, plan_type, plan_date, date_from, date_to, status, ai_generated)
            VALUES ($1, $2, $3, $4, $5, $6, 'draft', FALSE)
            RETURNING plan_id
            """,
            body.plan_name, body.entity, body.plan_type, body.date_from, body.date_from, body.date_to,
        )
    return {"plan_id": plan_id, "status": "draft"}


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
        conditions.append(f"plan_date >= ${idx}::date")
        params.append(date_from)
        idx += 1
    if date_to:
        conditions.append(f"plan_date <= ${idx}::date")
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


@router.put("/plans/{plan_id}/lines/{line_id}")
async def edit_plan_line(request: Request, plan_id: int, line_id: int, body: PlanLineEdit):
    """Edit a specific plan line (only while plan is draft).
    Only fields explicitly sent in the request body are updated — unsent fields are untouched.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status FROM production_plan WHERE plan_id = $1", plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        if plan['status'] != 'draft':
            raise HTTPException(status_code=400, detail="Can only edit draft plans")

        # Only update fields that were explicitly sent in the request
        sent_fields = body.model_fields_set
        editable = ['fg_sku_name', 'customer_name', 'bom_id', 'planned_qty_kg',
                     'planned_qty_units', 'machine_id', 'priority', 'shift',
                     'stage_sequence', 'estimated_hours', 'reasoning', 'status']

        updates = []
        params = []
        idx = 1
        for field in editable:
            if field in sent_fields:
                updates.append(f"{field} = ${idx}")
                params.append(getattr(body, field))
                idx += 1

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        params.append(line_id)
        params.append(plan_id)
        result = await conn.execute(
            f"UPDATE production_plan_line SET {', '.join(updates)} WHERE plan_line_id = ${idx} AND plan_id = ${idx + 1}",
            *params,
        )
        if result == 'UPDATE 0':
            raise HTTPException(status_code=404, detail="Plan line not found")

    return {"plan_line_id": line_id, "updated": True, "fields_changed": list(sent_fields & set(editable))}


@router.post("/plans/{plan_id}/lines")
async def add_plan_line(request: Request, plan_id: int, body: PlanLineAdd):
    """Add a manual line to a draft plan."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status, entity FROM production_plan WHERE plan_id = $1", plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        if plan['status'] != 'draft':
            raise HTTPException(status_code=400, detail="Can only add lines to draft plans")

        # Find bom_id if not provided
        bom_id = body.bom_id
        if not bom_id:
            bom_id = await conn.fetchval(
                "SELECT bom_id FROM bom_header WHERE fg_sku_name = $1 AND is_active = TRUE LIMIT 1",
                body.fg_sku_name,
            )

        line_id = await conn.fetchval(
            """
            INSERT INTO production_plan_line (
                plan_id, fg_sku_name, customer_name, bom_id,
                planned_qty_kg, planned_qty_units, machine_id, priority, shift
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING plan_line_id
            """,
            plan_id, body.fg_sku_name, body.customer_name, bom_id,
            body.planned_qty_kg, body.planned_qty_units, body.machine_id,
            body.priority, body.shift,
        )

    return {"plan_line_id": line_id, "plan_id": plan_id}


@router.delete("/plans/{plan_id}/lines/{line_id}")
async def remove_plan_line(request: Request, plan_id: int, line_id: int):
    """Remove a line from a draft plan."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status FROM production_plan WHERE plan_id = $1", plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        if plan['status'] != 'draft':
            raise HTTPException(status_code=400, detail="Can only remove lines from draft plans")

        result = await conn.execute(
            "DELETE FROM production_plan_line WHERE plan_line_id = $1 AND plan_id = $2",
            line_id, plan_id,
        )
        if result == 'DELETE 0':
            raise HTTPException(status_code=404, detail="Plan line not found")

    return {"deleted": True}


@router.put("/plans/{plan_id}/approve")
async def approve_plan(request: Request, plan_id: int, approved_by: str = Query(...)):
    """Approve a draft plan. Triggers MRP run (Part 3)."""
    pool = request.app.state.db_pool
    from app.modules.production.services.mrp import run_mrp
    from app.modules.production.services.indent_manager import generate_draft_indents

    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status, entity FROM production_plan WHERE plan_id = $1", plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        if plan['status'] != 'draft':
            raise HTTPException(status_code=400, detail="Only draft plans can be approved")

        async with conn.transaction():
            await conn.execute(
                "UPDATE production_plan SET status = 'approved', approved_by = $2, approved_at = NOW() WHERE plan_id = $1",
                plan_id, approved_by,
            )

            # Run MRP and generate draft indents for shortages
            mrp_result = await run_mrp(conn, plan_id, plan['entity'])
            draft_result = await generate_draft_indents(conn, mrp_result, plan_id, plan['entity'])

    return {
        "plan_id": plan_id,
        "status": "approved",
        "approved_by": approved_by,
        "mrp_summary": mrp_result["summary"],
        "draft_indents": draft_result["indents"],
    }


@router.put("/plans/{plan_id}/cancel")
async def cancel_plan(request: Request, plan_id: int):
    """Cancel a plan."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status FROM production_plan WHERE plan_id = $1", plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        if plan['status'] not in ('draft', 'approved'):
            raise HTTPException(status_code=400, detail="Cannot cancel a plan in this status")

        await conn.execute(
            "UPDATE production_plan SET status = 'cancelled' WHERE plan_id = $1", plan_id,
        )

    return {"plan_id": plan_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# MRP endpoints
# ---------------------------------------------------------------------------


class MRPRunRequest(BaseModel):
    plan_id: int


@router.post("/mrp/run")
async def mrp_run(request: Request, body: MRPRunRequest):
    """Run MRP for an approved plan. Returns material check + creates draft indents."""
    from app.modules.production.services.mrp import run_mrp
    from app.modules.production.services.indent_manager import generate_draft_indents

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status, entity FROM production_plan WHERE plan_id = $1", body.plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

        async with conn.transaction():
            mrp_result = await run_mrp(conn, body.plan_id, plan['entity'])
            draft_result = await generate_draft_indents(conn, mrp_result, body.plan_id, plan['entity'])

    mrp_result["draft_indents"] = draft_result["indents"]
    return mrp_result


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
# Indent endpoints
# ---------------------------------------------------------------------------


class IndentEditRequest(BaseModel):
    required_qty_kg: float | None = None
    required_by_date: date | None = None
    priority: int | None = None


class IndentAcknowledgeRequest(BaseModel):
    acknowledged_by: str


class IndentLinkPORequest(BaseModel):
    po_reference: str


class IndentBulkSendRequest(BaseModel):
    indent_ids: list[int]


@router.get("/indents")
async def list_indents(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
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


@router.put("/indents/{indent_id}/edit")
async def edit_indent_endpoint(request: Request, indent_id: int, body: IndentEditRequest):
    """Edit a draft indent before sending."""
    from app.modules.production.services.indent_manager import edit_indent
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await edit_indent(
            conn, indent_id,
            required_qty_kg=body.required_qty_kg,
            required_by_date=body.required_by_date,
            priority=body.priority,
        )
    if "error" in result:
        if result["error"] == "not_found":
            raise HTTPException(status_code=404, detail="Indent not found")
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.put("/indents/{indent_id}/send")
async def send_indent_endpoint(request: Request, indent_id: int):
    """Send a draft indent → raised. Creates alerts for purchase + stores."""
    from app.modules.production.services.indent_manager import send_indent
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await send_indent(conn, indent_id)
    if "error" in result:
        if result["error"] == "not_found":
            raise HTTPException(status_code=404, detail="Indent not found")
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.post("/indents/send-bulk")
async def send_bulk_indents_endpoint(request: Request, body: IndentBulkSendRequest):
    """Send multiple draft indents at once."""
    from app.modules.production.services.indent_manager import send_bulk_indents
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await send_bulk_indents(conn, body.indent_ids)


@router.put("/indents/{indent_id}/acknowledge")
async def acknowledge_indent_endpoint(request: Request, indent_id: int, body: IndentAcknowledgeRequest):
    """Purchase team acknowledges indent. raised → acknowledged."""
    from app.modules.production.services.indent_manager import acknowledge_indent
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await acknowledge_indent(conn, indent_id, body.acknowledged_by)
    if "error" in result:
        if result["error"] == "not_found":
            raise HTTPException(status_code=404, detail="Indent not found")
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
    return result


@router.put("/indents/{indent_id}/link-po")
async def link_indent_po_endpoint(request: Request, indent_id: int, body: IndentLinkPORequest):
    """Link indent to a PO reference. acknowledged → po_created."""
    from app.modules.production.services.indent_manager import link_indent_to_po
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await link_indent_to_po(conn, indent_id, body.po_reference)
    if "error" in result:
        if result["error"] == "not_found":
            raise HTTPException(status_code=404, detail="Indent not found")
        raise HTTPException(status_code=400, detail=result.get("message", result["error"]))
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
    stage: str = Query(None),
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
    if stage:
        conditions.append(f"stage = ${idx}"); params.append(stage); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM job_card WHERE {where}", *params)
        rows = await conn.fetch(
            f"""SELECT job_card_id, job_card_number, prod_order_id, step_number, process_name, stage,
                       fg_sku_name, customer_name, batch_number, batch_size_kg,
                       assigned_to_team_leader, is_locked, status, start_time, factory, floor, entity
                FROM job_card WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}""",
            *params, page_size, offset,
        )
    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "total_pages": (total + page_size - 1) // page_size if total else 0},
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
    material_return_kg: float = 0
    rejection_kg: float = 0
    rejection_reason: str | None = None
    process_loss_kg: float = 0
    process_loss_pct: float = 0
    offgrade_kg: float = 0
    offgrade_category: str | None = None
    dispatch_qty: float | None = None


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


# ---------------------------------------------------------------------------
# Plan Revision endpoints
# ---------------------------------------------------------------------------


class RevisionRequest(BaseModel):
    plan_id: int
    change_event: str
    new_fulfillment_ids: list[int] | None = None


@router.post("/plans/revise")
async def revise_plan(request: Request, body: RevisionRequest):
    """Revise an existing plan via Claude AI. Creates a new plan with revision_number++."""
    from app.modules.production.services.ai_planner import (
        collect_revision_context, call_claude, create_revised_plan, PLAN_REVISION_PROMPT,
    )
    pool = request.app.state.db_pool
    settings = request.app.state.settings

    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT entity FROM production_plan WHERE plan_id = $1", body.plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

        context = await collect_revision_context(conn, body.plan_id, body.change_event, plan['entity'])

    ai_result = await call_claude(settings, PLAN_REVISION_PROMPT, context)

    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await create_revised_plan(conn, body.plan_id, plan['entity'], ai_result, settings)

    return result


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
