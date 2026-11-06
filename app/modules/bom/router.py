"""/api/v1/bom/* — BOM module (rolled-up Bill of Materials browser + editor).

Seven endpoints. Five are gated on one ('bom', NULL, NULL, <action>) permission
tuple — 'view' and 'create' are seeded by app/db/095_bom_module_rbac.sql,
'update' and 'delete' by app/db/139_bom_module_rbac_update_delete.sql. The two
lookups need only a login (see below):

    GET    /api/v1/bom/aggregate           view    one row per BOM, figures rolled up in SQL
    GET    /api/v1/bom/lookups/materials   (login) type-ahead over all_sku for the line picker
    GET    /api/v1/bom/lookups/processes   (login) distinct process names for the route picker
    GET    /api/v1/bom/{bom_id}            view    header + lines + process route + counts
    POST   /api/v1/bom                     create  header + lines (+ route), strict
    PATCH  /api/v1/bom/{bom_id}            update  partial header edit; lines / route
                                                   replaced only when sent
    DELETE /api/v1/bom/{bom_id}            delete  SOFT delete — is_active = FALSE, nothing
                                                   is removed

Deliberately a NEW module rather than more routes on production/router.py: that
file is past 7k lines and the BOM screen shares no state with it.

The route strip and the line list are returned as two INDEPENDENT collections —
bom_line and bom_process_route share no key and are never joined. See the
module docstring of services/bom_aggregate_service.py for why.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.modules.auth.middleware import AuthUser, get_current_user, require_permission
from app.modules.bom.services import (
    bom_aggregate_service, bom_lookup_service, bom_write_service,
)

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


# Service error codes that mean "someone else holds this" rather than "your
# payload is malformed". Everything else the services return is a 400.
_CONFLICT_CODES = frozenset({"bom_exists", "line_referenced"})


def _status_for(err: str) -> int:
    return 409 if err in _CONFLICT_CODES else 400


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


# ── Lookups (type-ahead for the form) ────────────────────────────────────────
# Two-segment literal paths, so they can never be captured by GET /{bom_id};
# still declared ahead of it for the same reason /aggregate is.
#
# AUTHENTICATION ONLY, no bom.* gate — the same reasoning as /so/sku-lookup:
# all_sku names and the process names already on BOM routes are shared
# reference data, and the pickers that read them are embedded in OTHER modules'
# screens (the plan-builder's inline BOM editor on SO Creation is used by
# planners, who do not hold bom.view, which is admin-only). Gating per module
# would 403 a different role every time another screen reuses the picker, and
# the picker would render that as "no matches". Nothing here reveals a BOM.

@router.get("/lookups/materials")
async def lookup_materials(
    request: Request,
    q: str = Query("", max_length=200,
                   description="type-ahead text; every token must appear; blank returns nothing"),
    item_type: Optional[str] = Query(
        None, description="rm | pm | sfg | fg — omit to search every type (hampers use FG as rm)"),
    limit: int = Query(20, ge=1, le=100),
    user: AuthUser = Depends(get_current_user),
):
    """Ranked all_sku matches for the BOM line Material picker.

    Returns {results: [{sku_id, particulars, item_type, item_group, sub_group}]}.
    See services/bom_lookup_service.py for why this is not /so/sku-lookup.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await bom_lookup_service.search_materials(
            conn, q, item_type=item_type, limit=limit)
    return {"results": rows}


@router.get("/lookups/processes")
async def lookup_processes(
    request: Request,
    q: str = Query("", max_length=200, description="substring; blank lists every process"),
    limit: int = Query(30, ge=1, le=100),
    user: AuthUser = Depends(get_current_user),
):
    """Distinct process names already used on any BOM route, most-used first.

    Returns {results: [{process_name, stage, boms}]}. The form still accepts a
    name that is not in this list — this is a suggestion source, not a master.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await bom_lookup_service.search_processes(conn, q, limit=limit)
    return {"results": rows}


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


# ── Request bodies ───────────────────────────────────────────────────────────

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


class BomLineUpdateIn(BomLineIn):
    # Present  -> UPDATE that existing line in place (its bom_line_id survives,
    #             so job-card indents / consumption keep pointing at it).
    # Absent   -> INSERT a new line.
    # An existing line whose id is NOT in the payload is DELETEd — unless a job
    # card references it, which is a 409 `line_referenced`.
    bom_line_id: int | None = None


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


class BomUpdateRequest(BaseModel):
    """Every field optional. Only the fields actually SENT are touched (the
    router dumps with exclude_unset=True), so `{"notes": null}` clears notes
    while omitting `notes` leaves it alone. bom_id, version and created_at are
    not here and cannot be changed."""
    # Identity
    fg_sku_name: str | None = None
    entity: str | None = None
    customer_name: str | None = None
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
    allowed_balance_tolerance_pct: float | None = None   # null is refused (NOT NULL)

    # Validity
    effective_from: date | None = None
    effective_to: date | None = None
    notes: str | None = None

    # Status. `true` re-activates a deactivated BOM (409 if another active BOM
    # holds the FG name); `false` is the same soft delete DELETE performs.
    is_active: bool | None = None

    # Collections. Omitted = untouched. `lines` must hold at least one line when
    # sent (a BOM with no lines is the broken state the browser exists to find);
    # `route` may be an empty list, which clears the route.
    lines: Annotated[list[BomLineUpdateIn], Field(min_length=1)] | None = None
    route: list[BomRouteStepIn] | None = None


# ── Create ───────────────────────────────────────────────────────────────────
# Declared after the GETs; POST /api/v1/bom has no path parameter so it cannot
# collide with GET /{bom_id}.

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
                raise HTTPException(_status_for(err), detail=result)
    return result


# ── Update ───────────────────────────────────────────────────────────────────

@router.patch("/{bom_id}")
async def update_bom(
    request: Request,
    bom_id: int,
    body: BomUpdateRequest,
    user: AuthUser = Depends(require_permission("bom", action="update")),
):
    """Partially update one BOM, in one transaction.

    Header fields are updated only when sent. `lines`, when sent, replaces the
    line set matched by bom_line_id (existing ids are updated in place, new
    lines inserted, missing ones deleted — a 409 `line_referenced` if a job
    card still points at one). `route`, when sent, replaces the whole route.

    Returns the change summary plus the fresh `detail` (header, lines, route,
    counts — the same shape as GET /{bom_id}) so a client can refresh in place.
    """
    provided = body.model_dump(exclude_unset=True)
    if not provided:
        raise HTTPException(
            400,
            detail={"error": "empty_update",
                    "message": "Provide at least one field to update",
                    "details": {"bom_id": bom_id}},
        )

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await bom_write_service.update_bom(
                conn, bom_id, provided, updated_by=_actor(user))
            if result is None:
                raise _not_found(bom_id)
            err = result.get("error")
            if err:
                # Raised INSIDE the transaction block so every write the
                # service made before refusing (line deletes that did succeed,
                # the header UPDATE) is rolled back with it.
                raise HTTPException(_status_for(err), detail=result)
            result["detail"] = await bom_aggregate_service.get_bom_detail(conn, bom_id)
    return result


# ── Delete (soft) ────────────────────────────────────────────────────────────

@router.delete("/{bom_id}")
async def deactivate_bom(
    request: Request,
    bom_id: int,
    user: AuthUser = Depends(require_permission("bom", action="delete")),
):
    """Mark one BOM inactive. NOTHING is deleted.

    bom_id is referenced from plan lines, job cards, QC and costing, so a hard
    delete is never safe; `is_active = FALSE` hides the BOM from every resolver
    (they all filter on it) while every existing job card keeps its recipe.
    Idempotent — a second call is a 200 with `already_inactive: true`. Reverse
    it with PATCH {"is_active": true}.

    200 with a body rather than 204: the caller needs to know whether this call
    did the deactivating or found it already done.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await bom_write_service.deactivate_bom(
                conn, bom_id, deactivated_by=_actor(user))
    if result is None:
        raise _not_found(bom_id)
    return result


def _actor(user: AuthUser) -> str:
    """Display name for created_by / updated_by, from the token — never the
    request body."""
    return (getattr(user, "full_name", None)
            or getattr(user, "email", None)
            or getattr(user, "phone", None)
            or f"user:{getattr(user, 'user_id', '?')}")
