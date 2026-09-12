"""/api/v1/stock-take/* — the console's view over the stock-take entries.

    GET /api/v1/stock-take/latest-stock    stock as counted on the most recent
                                           count date, plus that date
    GET /api/v1/stock-take/filter-options  distinct values for the filter controls
    GET /api/v1/stock-take/entries/export  every matching count row as .xlsx

Rows come from `new_stock_entries` (business_day.ENTRIES_TABLE), the canonical
copy of the `stocktake_entries` that the separate Stock Take app
(Stock_Take/backend_st) writes into the same RDS `warehouse_db`. Its floor names
are canonicalised to FLOORS_BY_WAREHOUSE and its timestamps are timestamptz.

The counting flow stays in that app: there is deliberately no POST/PATCH/DELETE
for counts here. The one write this console makes is the adjustment row folded in
by transactions_service.write_back_entry.

Counts keyed on the floor app AFTER the 2026-09-08 backfill land in
`stocktake_entries` and are not visible here until they are copied across.

GATED ON THE `stock_take` PERMISSION (app/db/102_stock_take_rbac.sql), so the
module is reachable by admins and by holders of the `stock_take` role, and by
nobody else. It previously ran on `get_current_user` alone -- any authenticated
user could read counted stock and post adjustments -- and leaned on the console
tile being admin-only. A hidden tile is a convention, not an authorisation
boundary: it removes the link, not the route.

Three actions rather than one: `view` for the reads, `create` for posting an
adjustment, `export` for the two spreadsheet downloads. An export takes the whole
stock position out of the building, which is worth being able to withhold from
someone who may otherwise look.

A NEW module rather than more routes on production/router.py: that file is past
7k lines and this screen shares no state with it — the same reasoning the BOM
module records.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.stock_take.services import (
    entries_export, export_xlsx, latest_stock_service, transactions_service,
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
    user: AuthUser = Depends(require_permission("stock_take", action="view")),
) -> dict[str, Any]:
    """Aggregated stock for the most recent count date matching the filters.

    The date is resolved under the filters, so `?warehouse=W202` reports W202's
    own last count rather than an empty page for a day it was not counted on.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        places = await latest_stock_service.fetch_places(conn)
        whs, by_wh = _read_scope(user, places)

        # A filter the caller may not use is refused, never silently widened:
        # dropping an out-of-scope value would leave NO warehouse predicate,
        # which reads as "everything" -- the opposite of what was asked for.
        if warehouse:
            bad = [w for w in warehouse
                   if transactions_service._normalise_warehouse(w) not in whs]
            if bad:
                raise HTTPException(403, detail={
                    "error": "warehouse_not_allowed",
                    "message": f"You are not assigned to warehouse {bad[0]!r}.",
                    "details": {"requested": bad, "allowed_warehouses": whs}})
        else:
            # "all" means all of YOURS. Only applied when the profile actually
            # restricts; an unrestricted caller keeps an unfiltered query rather
            # than one pinned to whatever happens to be in the table today.
            if [w for w in (user.allowed_warehouses or []) if str(w).strip()]:
                warehouse = list(whs)

        # Floors are clamped ONLY when the caller holds floor grants. Defaulting
        # an ungranted caller to the scoped floor list would hide every row on a
        # floor the profile never declared -- 301 W202 rows sit on STORE -- and
        # "I can see the warehouse" has to mean all of it.
        if [f for f in (user.allowed_floors or []) if str(f).strip()]:
            allowed_f = {f.strip().upper() for fl in by_wh.values() for f in fl}
            if floor_name:
                bad_f = [f for f in floor_name if f.strip().upper() not in allowed_f]
                if bad_f:
                    raise HTTPException(403, detail={
                        "error": "floor_not_allowed",
                        "message": f"You are not assigned to floor {bad_f[0]!r}.",
                        "details": {"requested": bad_f, "allowed_floors": sorted(allowed_f)}})
            else:
                floor_name = sorted(allowed_f)

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
    user: AuthUser = Depends(require_permission("stock_take", action="view")),
    # dict[str, Any], NOT dict[str, list[str]]: FastAPI turns the return
    # annotation into a response model and VALIDATES against it, so the nested
    # floors_by_warehouse map made every call fail with 500 -- and the browser
    # swallows a failed filter-options, so the only symptom was four empty
    # dropdowns with nothing in the console.
) -> dict[str, Any]:
    """Distinct warehouses / floors / item types / stock types the caller may see.

    The place dimensions are scoped to the caller's profile; item type and stock
    type are not, because neither is a scope the profile expresses.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        opts = await latest_stock_service.fetch_filter_options(conn)
        places = await latest_stock_service.fetch_places(conn)

    whs, by_wh = _read_scope(user, places)
    floors: list[str] = []
    for wh in whs:
        for f in by_wh[wh]:
            if f not in floors:
                floors.append(f)
    return {
        **opts,
        "warehouses": whs,
        "floors": sorted(floors),
        # So the browser can narrow Floor once a Warehouse is picked instead of
        # listing another building's floors, same as the Adjust form.
        "floors_by_warehouse": by_wh,
    }


async def _available(conn) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Every warehouse and floor stock is actually recorded at.

    The fallback set for an UNRESTRICTED user. Empty allowed_floors means "no
    restriction" (auth_schema.sql:35), not "no access", so such a user — every
    admin included — is offered everything rather than being locked out.
    """
    opts = await latest_stock_service.fetch_filter_options(conn)
    places = await latest_stock_service.fetch_places(conn)
    return opts.get("warehouses", []), opts.get("floors", []), places


def _read_scope(user: AuthUser, places: dict[str, list[str]]) -> tuple[list[str], dict[str, list[str]]]:
    """(warehouses, floors-per-warehouse) this caller may LOOK at.

    Deliberately built from `places` -- the floors stock is actually recorded at
    -- and not from the declared floor profile the posting form uses. A floor
    nobody declared can still hold counted stock, and a read filter that cannot
    name it makes that stock unreachable rather than merely unpostable.

    Empty grants mean "no restriction", the same rule as everywhere else:
    auth_schema.sql:35, and the admin screen renders an empty list as "All".
    """
    granted_w = [w for w in (user.allowed_warehouses or []) if str(w).strip()]
    granted_f = [f for f in (user.allowed_floors or []) if str(f).strip()]

    whs = (sorted({transactions_service._normalise_warehouse(w) for w in granted_w})
           if granted_w else sorted(places))
    keep = {str(f).strip().upper() for f in granted_f}
    by_wh: dict[str, list[str]] = {}
    for wh in whs:
        floors = list(places.get(wh, []))
        if keep:
            floors = [f for f in floors if f.strip().upper() in keep]
        by_wh[wh] = floors
    return whs, by_wh


@router.get("/scope")
async def my_scope(
    request: Request,
    user: AuthUser = Depends(require_permission("stock_take", action="view")),
) -> dict[str, Any]:
    """The warehouses and floors this user may post transactions against.

    The form calls this to decide whether to pin a single value, offer a choice,
    or explain why it cannot open — so that policy lives here rather than being
    re-derived in the browser from /me.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        avail_w, avail_f, places = await _available(conn)

    scope = transactions_service.effective_scope(
        user, available_warehouses=avail_w, available_floors=avail_f, places=places)
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
    user: AuthUser = Depends(require_permission("stock_take", action="create")),
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
        avail_w, avail_f, places = await _available(conn)
        try:
            warehouse, location = transactions_service.resolve_scope(
                user, warehouse=body.warehouse, location=body.location,
                available_warehouses=avail_w, available_floors=avail_f, places=places)
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
    user: AuthUser = Depends(require_permission("stock_take", action="view")),
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


class VerifyBody(BaseModel):
    """Either an explicit set of adjustment rows, or a day (plus optional place).

    Both shapes exist because both are real: a reviewer working down the screen
    signs off one article at a time, and the end-of-day flow signs off everything
    at once. Sending neither means "today, everywhere I am scoped to", which is
    the common case and the one worth being the default.
    """

    model_config = ConfigDict(populate_by_name=True)

    entry_ids: Optional[list[int]] = Field(default=None, alias="entryIds")
    day: Optional[str] = Field(default=None, description="IST day, YYYY-MM-DD")
    warehouse: Optional[str] = None
    location: Optional[str] = Field(default=None, alias="floorName")
    item_name: Optional[str] = Field(default=None, alias="itemName")
    stock_type: Optional[str] = Field(default=None, alias="stockType")


@router.post("/adjustments/verify")
async def verify_stock_lines(
    request: Request,
    body: VerifyBody = Body(default_factory=VerifyBody),
    user: AuthUser = Depends(require_permission("stock_take", action="verify")),
) -> dict[str, Any]:
    """Sign off stock lines: mark their new_stock_entries rows verified.

    Gated on `verify`, which the stock_take role deliberately does NOT hold --
    the point of a sign-off is that someone other than the person who posted the
    adjustment gives it. See app/db/108_stock_take_verification_role.sql.

    The verification is recorded on the ADJUSTMENT row in new_stock_entries.
    stocktake_transactions is append-only, so it could never carry a mutable
    flag; a transaction's state is read back from the row it rolls into, joined
    on the key uq_nse_adjustment_day already enforces. One sign-off therefore
    covers every posting made against that article and place on that day.

    Adjusting the same article again clears the sign-off — write_back_entry's
    upsert resets verified/verified_by/verified_at, so a figure that moved after
    being checked goes back to needing a check with nothing to remember.
    """
    day = None
    if body.day:
        day = latest_stock_service.normalise_date(body.day)
        if day is None:
            raise HTTPException(400, detail={
                "error": "invalid_day",
                "message": f"Invalid day {body.day!r}; expected YYYY-MM-DD",
                "details": {"day": body.day}})

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # Scope-check the place the caller is signing off, so a verifier cannot
        # sign for a warehouse they are not assigned to.
        places = await latest_stock_service.fetch_places(conn)
        whs, _ = _read_scope(user, places)
        if body.warehouse:
            if transactions_service._normalise_warehouse(body.warehouse) not in whs:
                raise HTTPException(403, detail={
                    "error": "warehouse_not_allowed",
                    "message": f"You are not assigned to warehouse {body.warehouse!r}.",
                    "details": {"allowed_warehouses": whs}})

        async with conn.transaction():
            result = await transactions_service.verify_entries(
                conn,
                actor=_actor(user),
                day=day,
                warehouse=body.warehouse,
                location=body.location,
                item_name=body.item_name,
                stock_type=body.stock_type,
                entry_ids=body.entry_ids,
            )
    return result


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
    user: AuthUser = Depends(require_permission("stock_take", action="export")),
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
    user: AuthUser = Depends(require_permission("stock_take", action="view")),
) -> dict[str, Any]:
    """Counted + netted balance for one article at the caller's scope.

    The form reads this as the operator picks an article, so the overdraw warning
    appears before submit rather than after.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        avail_w, avail_f, places = await _available(conn)
        try:
            wh, loc = transactions_service.resolve_scope(
                user, warehouse=warehouse, location=location,
                available_warehouses=avail_w, available_floors=avail_f, places=places)
        except transactions_service.ScopeError as exc:
            status = 400 if exc.code in ("floor_required", "warehouse_required") else 403
            raise HTTPException(status, detail={
                "error": exc.code, "message": exc.message, "details": exc.details}) from exc
        return await transactions_service.current_balance(
            conn, item_name=item_name, stock_type=stock_type, warehouse=wh, location=loc)


# ── Floor count export ─────────────────────────────────────────────────────
# The third read, and the only one that returns rows as counted rather than
# aggregated:
#   GET /latest-stock         netted, one row per article+place
#   GET /transactions/export  the adjustment ledger
#   GET /entries/export       THIS — every individual weighing
# The filter vocabulary is /latest-stock's, so the download matches the screen
# it is launched from; dateFrom/dateTo are added because a raw-row export is
# read by period where the aggregate is read as-of a day.
_ENTRY_QUERY = {
    "warehouse": Query(None, description="Warehouse code(s); repeat the param"),
    "floor_name": Query(None, alias="floorName", description="Floor name(s)"),
    "item_type": Query(None, alias="itemType", description="PM / RM / FG"),
    "category": Query(None, description="Item group(s)"),
    "subcategory": Query(None, description="Item sub-group(s)"),
    "stock_type": Query(None, alias="stockType", description="Fresh Stock / Off Grade/Rejection"),
    "entered_by": Query(None, alias="enteredBy", description="Counter name, substring match"),
    "search": Query(None, description="Free text across item, group, warehouse, floor, counter"),
    "verified": Query(None, description="Filter on the manager verification flag"),
    "date_from": Query(None, alias="dateFrom", description="IST day, YYYY-MM-DD (inclusive)"),
    "date_to": Query(None, alias="dateTo", description="IST day, YYYY-MM-DD (inclusive)"),
}


def _slug(value: Optional[list[str]]) -> str:
    """First selected value, squashed for a filename — the floor app's rule."""
    if not value:
        return ""
    return "_" + "".join(str(value[0]).split())


@router.get("/entries/export")
async def export_entries(
    request: Request,
    warehouse: Optional[list[str]] = _ENTRY_QUERY["warehouse"],
    floor_name: Optional[list[str]] = _ENTRY_QUERY["floor_name"],
    item_type: Optional[list[str]] = _ENTRY_QUERY["item_type"],
    category: Optional[list[str]] = _ENTRY_QUERY["category"],
    subcategory: Optional[list[str]] = _ENTRY_QUERY["subcategory"],
    stock_type: Optional[list[str]] = _ENTRY_QUERY["stock_type"],
    entered_by: Optional[str] = _ENTRY_QUERY["entered_by"],
    search: Optional[str] = _ENTRY_QUERY["search"],
    verified: Optional[bool] = _ENTRY_QUERY["verified"],
    date_from: Optional[str] = _ENTRY_QUERY["date_from"],
    date_to: Optional[str] = _ENTRY_QUERY["date_to"],
    user: AuthUser = Depends(require_permission("stock_take", action="export")),
) -> StreamingResponse:
    """Every matching floor count row as .xlsx — UNPAGINATED, drafts excluded.

    Drafts are not a filter here. A draft is a count the operator has not
    submitted, so exporting one would put an unowned figure in a signed sheet;
    the floor app excludes them too, and both say how many were left out.

    An empty match returns a one-sheet workbook that says so, rather than the
    404 the Express endpoint returns. A download that 404s is indistinguishable
    from a broken endpoint at the browser, and the filters are stamped into the
    sheet, so the file itself explains why it is empty.

    SCOPE: like the other reads in this module, this is gated on authentication
    alone and returns every floor's rows regardless of the caller's
    allowed_floors. That is the module-wide gap, not one this endpoint invents —
    it must be closed here at the same time as /latest-stock, GET /transactions
    and /transactions/export, or it just moves.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            rows, applied, drafts = await entries_export.fetch_entries(
                conn,
                warehouse=warehouse, floor_name=floor_name, item_type=item_type,
                category=category, subcategory=subcategory, stock_type=stock_type,
                entered_by=entered_by, search=search, verified=verified,
                date_from=date_from, date_to=date_to,
            )
        except ValueError as exc:
            raise HTTPException(400, detail={
                "error": "invalid_filter", "message": str(exc),
                "details": {"dateFrom": date_from, "dateTo": date_to}}) from exc

    stream = export_xlsx.build_entries_workbook(rows, applied, drafts, _actor(user))
    stamp = (date_to or date_from or date.today().isoformat())
    filename = (f"StockTakeEntries_{stamp}"
                f"{_slug(warehouse)}{_slug(floor_name)}"
                f"{('_' + ''.join(entered_by.split())) if entered_by else ''}.xlsx")
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Lets the caller show a real count and makes a truncated download
            # detectable rather than silent; X-Draft-Rows is what was left out.
            # Both are listed in the CORS expose_headers in main.py — without
            # that a cross-origin fetch reads them back as null, not as an error.
            "X-Total-Rows": str(len(rows)),
            "X-Draft-Rows": str(drafts),
        },
    )
