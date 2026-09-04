"""/api/v1/stock-take/* — read-only view over the Stock Take app's entries.

    GET /api/v1/stock-take/latest-stock    stock as counted on the most recent
                                           count date, plus that date
    GET /api/v1/stock-take/filter-options  distinct values for the filter controls

`stocktake_entries` is written by the separate Stock Take app (Stock_Take/backend_st)
into the same RDS `warehouse_db`. This module only reads it — the counting flow
stays in that app, and there is deliberately no POST/PATCH/DELETE here.

Gated on `get_current_user` alone, matching the Express endpoint's own posture
(authMiddleware, no role check) and the ledger router. The console tile is
admin-only, so exposure is bounded by the UI rather than by a permission row.

A NEW module rather than more routes on production/router.py: that file is past
7k lines and this screen shares no state with it — the same reasoning the BOM
module records.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.stock_take.services import (
    export_xlsx, latest_stock_service, transactions_service,
)

router = APIRouter(prefix="/api/v1/stock-take", tags=["Stock Take"])


def _actor(user: AuthUser) -> str:
    """Display name for created_by, from the token — never the request body."""
    return (getattr(user, "full_name", None)
            or getattr(user, "email", None)
            or getattr(user, "phone", None)
            or f"user:{getattr(user, 'user_id', '?')}")


class TransactionCreate(BaseModel):
    """Body for POST /transactions.

    txn_id, created_at, created_by, created_by_user_id and is_reversal are
    DELIBERATELY ABSENT — all are derived server-side. extra="forbid" so a client
    that sends one gets a 422 rather than having it silently dropped.
    warehouse and location are accepted only to CHOOSE among the values the
    caller was already granted; they are validated against the token's scope and
    never trusted as given.
    """

    model_config = ConfigDict(extra="forbid")

    item_name: str = Field(min_length=1, max_length=255)
    sku_id: Optional[int] = None
    is_new_article: bool = False
    material_type: str = Field(min_length=1, max_length=100)
    item_category: str = Field(min_length=1, max_length=255)
    item_subcategory: str = Field(min_length=1, max_length=255)
    stock_type: str = "Fresh Stock"
    # Both operator-entered and stored as given; no derivation is enforced
    # between them (see the table comment in 098_stocktake_transactions.sql).
    units: Decimal = Field(gt=0)
    qty_kg: Decimal = Field(gt=0)
    operation: str = Field(pattern="^(ADDITION|SUBTRACTION)$")
    reason: str = Field(min_length=1)
    warehouse: Optional[str] = None
    location: Optional[str] = None
    reverses_txn_id: Optional[int] = None


@router.get("/latest-stock")
async def latest_stock(
    request: Request,
    warehouse: Optional[list[str]] = Query(None, description="Warehouse code(s); repeat or comma-separate"),
    floor_name: Optional[list[str]] = Query(None, alias="floorName", description="Floor name(s)"),
    item_type: Optional[list[str]] = Query(None, alias="itemType", description="PM / RM / FG"),
    category: Optional[list[str]] = Query(None, description="Item group(s)"),
    subcategory: Optional[list[str]] = Query(None, description="Item sub-group(s)"),
    stock_type: Optional[list[str]] = Query(None, alias="stockType", description="Fresh Stock / Off Grade/Rejection"),
    entered_by: Optional[str] = Query(None, alias="enteredBy", description="Counter name, substring match"),
    search: Optional[str] = Query(None, description="Free text across item, group, warehouse, floor, counter"),
    verified: Optional[bool] = Query(None, description="Filter on the manager verification flag"),
    include_drafts: bool = Query(False, alias="includeDrafts", description="Include unsubmitted draft rows"),
    as_of: Optional[str] = Query(None, alias="asOf", description="Latest count on or before this YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000, alias="pageSize"),
    sort_by: str = Query(latest_stock_service.DEFAULT_SORT, alias="sortBy"),
    sort_order: str = Query("desc", alias="sortOrder", pattern="^(asc|desc)$"),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Aggregated stock for the most recent count date matching the filters.

    The date is resolved under the filters, so `?warehouse=W202` reports W202's
    own last count rather than an empty page for a day it was not counted on.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            return await latest_stock_service.fetch_latest_stock(
                conn,
                warehouse=warehouse,
                floor_name=floor_name,
                item_type=item_type,
                category=category,
                subcategory=subcategory,
                stock_type=stock_type,
                entered_by=entered_by,
                search=search,
                verified=verified,
                include_drafts=include_drafts,
                as_of=as_of,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        except ValueError as exc:
            # Only raised for a malformed asOf — see the service. Rejected rather
            # than dropped, so a back-dated request never silently returns today.
            raise HTTPException(
                400,
                detail={
                    "error": "invalid_as_of",
                    "message": str(exc),
                    "details": {"asOf": as_of},
                },
            ) from exc


@router.get("/filter-options")
async def filter_options(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, list[str]]:
    """Distinct warehouses / floors / item types / stock types, from live data."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await latest_stock_service.fetch_filter_options(conn)


async def _available(conn) -> tuple[list[str], list[str]]:
    """Every warehouse and floor stock is actually recorded at.

    The fallback set for an UNRESTRICTED user. Empty allowed_floors means "no
    restriction" (auth_schema.sql:35), not "no access", so such a user — every
    admin included — is offered everything rather than being locked out.
    """
    opts = await latest_stock_service.fetch_filter_options(conn)
    return opts.get("warehouses", []), opts.get("floors", [])


@router.get("/scope")
async def my_scope(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """The warehouses and floors this user may post transactions against.

    The form calls this to decide whether to pin a single value, offer a choice,
    or explain why it cannot open — so that policy lives here rather than being
    re-derived in the browser from /me.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        avail_w, avail_f = await _available(conn)

    scope = transactions_service.effective_scope(
        user, available_warehouses=avail_w, available_floors=avail_f)
    warehouses, floors = scope["warehouses"], scope["floors"]
    return {
        **scope,
        "can_post": bool(warehouses and floors),
        # Named so the UI renders the ACTUAL cause. "unrestricted but nothing
        # available" is a server/database misconfiguration, not a permissions
        # problem, and must not be reported as one — see resolve_scope.
        "blocked_reason": (
            None if warehouses and floors
            else "no_stock_data" if (scope["floors_unrestricted"] and not floors)
            else "no_floor_access" if not floors
            else "no_warehouse_access"
        ),
    }


@router.post("/transactions", status_code=201)
async def create_transaction(
    request: Request,
    body: TransactionCreate = Body(...),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Post one stock adjustment. The row is FINAL once created.

    Corrections are new rows carrying `reverses_txn_id`; the table blocks UPDATE
    and DELETE at the database level, so there is no edit path by design.

    A SUBTRACTION larger than the available balance is ALLOWED and reported back
    with `overdrawn: true` — floors routinely run ahead of the count, and refusing
    would make a real movement unrecordable. The caller is expected to surface the
    warning, not to be prevented.
    """
    payload = body.model_dump()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # Resolved with a connection in hand: an unrestricted user's choices come
        # from the data, not from their (empty) grant list.
        avail_w, avail_f = await _available(conn)
        try:
            warehouse, location = transactions_service.resolve_scope(
                user, warehouse=body.warehouse, location=body.location,
                available_warehouses=avail_w, available_floors=avail_f)
        except transactions_service.ScopeError as exc:
            # 403 for "not yours", 400 when the caller simply has to pick one.
            status = 400 if exc.code in ("floor_required", "warehouse_required") else 403
            raise HTTPException(status, detail={
                "error": exc.code, "message": exc.message, "details": exc.details}) from exc
        # One transaction so the balance reported back and the row inserted see
        # the same snapshot.
        async with conn.transaction():
            try:
                return await transactions_service.create_transaction(
                    conn, payload,
                    warehouse=warehouse, location=location,
                    created_by=_actor(user), created_by_user_id=getattr(user, "user_id", None),
                )
            except ValueError as exc:
                raise HTTPException(400, detail={
                    "error": "invalid_transaction", "message": str(exc)}) from exc


# The two ledger reads share one filter set on purpose (see _ledger_filters):
#   GET /transactions         paged, 200 per page  -> the on-screen view
#   GET /transactions/export  UNPAGED xlsx         -> the download
# An export that filtered differently from the screen it was launched from would
# quietly hand someone a spreadsheet that disagrees with what they were reading.
_LEDGER_QUERY = {
    "warehouse": Query(None, description="Warehouse code; W-202 and W202 both match"),
    "location": Query(None, description="Floor, as granted"),
    "item_name": Query(None, alias="itemName", description="Exact article name"),
    "operation": Query(None, description="ADDITION or SUBTRACTION"),
    "on_date": Query(None, alias="date", description="Exact day, YYYY-MM-DD; overrides the range"),
    "date_from": Query(None, alias="dateFrom", description="Range start, YYYY-MM-DD (inclusive)"),
    "date_to": Query(None, alias="dateTo", description="Range end, YYYY-MM-DD (inclusive)"),
}


def _bad_filter(exc: ValueError) -> HTTPException:
    return HTTPException(400, detail={"error": "invalid_filter", "message": str(exc)})


@router.get("/transactions")
async def list_transactions(
    request: Request,
    warehouse: Optional[str] = _LEDGER_QUERY["warehouse"],
    location: Optional[str] = _LEDGER_QUERY["location"],
    item_name: Optional[str] = _LEDGER_QUERY["item_name"],
    operation: Optional[str] = _LEDGER_QUERY["operation"],
    on_date: Optional[str] = _LEDGER_QUERY["on_date"],
    date_from: Optional[str] = _LEDGER_QUERY["date_from"],
    date_to: Optional[str] = _LEDGER_QUERY["date_to"],
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500, alias="pageSize"),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """A page of ledger rows, newest first. 200 per page by default."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            return await transactions_service.list_transactions(
                conn, page=page, page_size=page_size,
                warehouse=warehouse, location=location, item_name=item_name,
                operation=operation, on_date=on_date, date_from=date_from, date_to=date_to)
        except ValueError as exc:
            raise _bad_filter(exc) from exc


@router.get("/transactions/export")
async def export_transactions(
    request: Request,
    warehouse: Optional[str] = _LEDGER_QUERY["warehouse"],
    location: Optional[str] = _LEDGER_QUERY["location"],
    item_name: Optional[str] = _LEDGER_QUERY["item_name"],
    operation: Optional[str] = _LEDGER_QUERY["operation"],
    on_date: Optional[str] = _LEDGER_QUERY["on_date"],
    date_from: Optional[str] = _LEDGER_QUERY["date_from"],
    date_to: Optional[str] = _LEDGER_QUERY["date_to"],
    user: AuthUser = Depends(get_current_user),
) -> StreamingResponse:
    """Every matching ledger row as .xlsx — deliberately UNPAGINATED.

    No page/pageSize is accepted at all. A truncated export is worse than a slow
    one: the recipient cannot tell it is partial, and the active filters are
    stamped into the sheet header so the numbers stay attributable once the file
    leaves this screen.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            rows, applied = await transactions_service.export_transactions(
                conn, warehouse=warehouse, location=location, item_name=item_name,
                operation=operation, on_date=on_date, date_from=date_from, date_to=date_to)
        except ValueError as exc:
            raise _bad_filter(exc) from exc

    stream = export_xlsx.build_ledger_workbook(rows, applied, _actor(user))
    stamp = (on_date or date_from or "all").replace("-", "")
    filename = f"stock-transactions-{stamp}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The browser fetch reads this to size a progress hint, and it makes a
            # truncated download detectable rather than silent.
            "X-Total-Rows": str(len(rows)),
        },
    )


@router.get("/balance")
async def balance(
    request: Request,
    item_name: str = Query(..., alias="itemName"),
    stock_type: str = Query("Fresh Stock", alias="stockType"),
    warehouse: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Counted + netted balance for one article at the caller's scope.

    The form reads this as the operator picks an article, so the overdraw warning
    appears before submit rather than after.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        avail_w, avail_f = await _available(conn)
        try:
            wh, loc = transactions_service.resolve_scope(
                user, warehouse=warehouse, location=location,
                available_warehouses=avail_w, available_floors=avail_f)
        except transactions_service.ScopeError as exc:
            status = 400 if exc.code in ("floor_required", "warehouse_required") else 403
            raise HTTPException(status, detail={
                "error": exc.code, "message": exc.message, "details": exc.details}) from exc
        return await transactions_service.current_balance(
            conn, item_name=item_name, stock_type=stock_type, warehouse=wh, location=loc)
