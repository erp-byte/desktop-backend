"""/api/v1/bom/* — BOM module (rolled-up Bill of Materials browser).

Two read endpoints, both gated on the single ('bom', NULL, NULL, 'view')
permission seeded by app/db/095_bom_module_rbac.sql:

    GET /api/v1/bom/aggregate  — one row per BOM, figures rolled up in SQL.
    GET /api/v1/bom/{bom_id}   — header + lines + process route + counts.

Deliberately a NEW module rather than more routes on production/router.py: that
file is past 7k lines and the BOM screen shares no state with it.

The route strip and the line list are returned as two INDEPENDENT collections —
bom_line and bom_process_route share no key and are never joined. See the
module docstring of services/bom_aggregate_service.py for why.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.bom.services import bom_aggregate_service, bom_write_service

router = APIRouter(prefix="/api/v1/bom", tags=["BOM"])


def _not_found(bom_id: int) -> HTTPException:
    # detail keyed on "error"/"message" so request_context surfaces the specific
    # machine code at the envelope's top level (the dominant house convention),
    # not buried under `details`.
    return HTTPException(
        404,
        detail={
            "error": "bom_not_found",
            "message": f"No bom_header row with bom_id {bom_id}",
            "details": {"bom_id": bom_id},
        },
    )


# Declared BEFORE /{bom_id} so the literal segment is matched first.
@router.get("/aggregate")
async def list_bom_aggregate(
    request: Request,
    search: Optional[str] = Query(None, description="FG SKU / customer / group / bom id"),
    entity: Optional[str] = Query(None, description="cfpl or cdpl"),
    item_group: Optional[str] = Query(None),
    customer_name: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    item_type: Optional[str] = Query(
        None, description="only BOMs having at least one line of this type (rm/pm/sfg)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: AuthUser = Depends(require_permission("bom", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await bom_aggregate_service.list_bom_aggregate(
            conn, search=search, entity=entity, item_group=item_group,
            customer_name=customer_name, is_active=is_active,
            item_type=item_type, page=page, page_size=page_size)


@router.get("/{bom_id}")
async def get_bom_detail(
    request: Request,
    bom_id: int,
    user: AuthUser = Depends(require_permission("bom", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        detail = await bom_aggregate_service.get_bom_detail(conn, bom_id)
    if detail is None:
        raise _not_found(bom_id)
    return detail


# ── Create ───────────────────────────────────────────────────────────────────
# Declared after the GETs; POST /api/v1/bom has no path parameter so it cannot
# collide with GET /{bom_id}.

class BomLineIn(BaseModel):
    material_sku_name: str
    item_type: str                              # rm | pm | sfg
    quantity_per_unit: float                    # per ONE unit of FG, must be > 0
    uom: str | None = None
    loss_pct: float | None = None
    godown: str | None = None
    can_use_offgrade: bool | None = None
    offgrade_max_pct: float | None = None
    unit_rate_inr: float | None = None
    process_stage: str | None = None
    staging_method: str | None = None           # pick | backflush | floor_stock
    consumed_at_stage: str | None = None
    # No line_number: it is assigned 1..N from array order server-side, because
    # it is UNIQUE(bom_id, line_number) and a client-chosen value turns a
    # duplicate into a constraint error the operator cannot act on.


class BomRouteStepIn(BaseModel):
    process_name: str
    stage: str | None = None                    # defaults to a slug of process_name
    std_time_min: float | None = None
    loss_pct: float | None = None
    qc_check: str | None = None
    machine_type: str | None = None
    practical_operation: str | None = None
    stage_bucket: str | None = None
    input_kind: str | None = None
    output_kind: str | None = None
    input_code: str | None = None
    output_code: str | None = None
    # No step_number: assigned 1..N from array order, as above.


class BomCreateRequest(BaseModel):
    # Identity
    fg_sku_name: str
    entity: str | None = None                   # cfpl | cdpl
    customer_name: str | None = None            # null = generic BOM
    pack_size_kg: float | None = None
    output_uom: str | None = None

    # Classification / routing metadata
    item_group: str | None = None
    sub_group: str | None = None
    process_category: str | None = None
    business_unit: str | None = None
    factory: str | None = None
    floors: list[str] | None = None
    machines: list[str] | None = None
    bar_line_process: str | None = None

    # Commercial / compliance
    shelf_life_days: int | None = None
    gst_rate: float | None = None
    hsn_sac: str | None = None
    inventory_group: str | None = None
    customer_code: str | None = None
    allowed_balance_tolerance_pct: float | None = None   # NOT NULL DEFAULT 0.001

    # Validity
    effective_from: date | None = None          # defaults to CURRENT_DATE in DB
    effective_to: date | None = None
    notes: str | None = None

    lines: list[BomLineIn] = Field(min_length=1)
    route: list[BomRouteStepIn] = Field(default_factory=list)

    # version / is_active / bom_id are server-assigned and deliberately absent.


@router.post("", status_code=201)
async def create_bom(
    request: Request,
    body: BomCreateRequest,
    user: AuthUser = Depends(require_permission("bom", action="create")),
):
    """Create one BOM: header + lines + optional process route, in one transaction.

    STRICT create. An active BOM for the same fg_sku_name is a 409, not a
    supersede — unlike POST /plans-v2/bom, which deactivates the incumbent.
    bom_id is FK'd from sixteen tables and bom_line_id from seven, so a
    double-submitted form must not be able to deactivate a BOM the floor is
    running.

    A 201 may still carry `warnings` — notably that the Tally refresh can delete
    hand-created rm/pm lines. See services/bom_write_service.py.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await bom_write_service.create_bom(
                conn, body.model_dump(), created_by=_actor(user))
            err = result.get("error")
            if err:
                # 409 only for the "someone already has this" case; every other
                # rejection is a malformed payload.
                raise HTTPException(409 if err == "bom_exists" else 400,
                                    detail=result)
    return result


def _actor(user: AuthUser) -> str:
    """Display name for created_by, from the token — never the request body."""
    return (getattr(user, "full_name", None)
            or getattr(user, "email", None)
            or getattr(user, "phone", None)
            or f"user:{getattr(user, 'user_id', '?')}")
