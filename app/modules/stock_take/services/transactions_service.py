"""Append-only stock adjustment ledger over `stocktake_entries`.

One row per physical movement recorded between counts. A posted row is FINAL —
`stocktake_transactions` blocks UPDATE and DELETE at the database level
(app/db/098_stocktake_transactions.sql), so a mistake is corrected by posting a
reversal, never by editing.

SCOPE IS DERIVED FROM THE TOKEN, NOT THE BODY. `warehouse` and `location` come
from the caller's `allowed_warehouses` / `allowed_floors`; a request body cannot
name a floor the user was not granted. That is the actual access control here —
the endpoint itself is open to any authenticated user, matching the read side.

ARTICLE IDENTITY IS A STRING. `stocktake_entries.item_name` is free text with no
FK, and the Stock Take floor UI deliberately allows custom items, so a
transaction joins counted stock on UPPER(BTRIM(item_name)) plus stock_type — the
same identity both latest-stock implementations use. `sku_id` is recorded when
the operator picked from the catalogue, purely as audit trail.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

import asyncpg

from .business_day import ENTRY_DAY, TXN_DAY

log = logging.getLogger(__name__)

OPERATIONS = ("ADDITION", "SUBTRACTION")

# Columns the caller is allowed to supply. An explicit allowlist rather than
# splatting the pydantic model: a body field like created_by or warehouse must be
# impossible to inject even if the model later stops forbidding extras.
_INSERT_COLS = (
    "item_name", "sku_id", "is_new_article",
    "material_type", "item_category", "item_subcategory", "stock_type",
    "units", "qty_kg", "operation", "reason",
    "warehouse", "location",
    "reverses_txn_id", "is_reversal",
    "created_by", "created_by_user_id",
)

# txn_code is the 8-digit YYMMDD+NN reference the UI shows (099_stocktake_txn_code.sql).
# It is minted by a BEFORE INSERT trigger, so it is never in _INSERT_COLS — it is
# only ever read back. txn_id remains the key: reverses_txn_id points at it, and a
# correction chain must not depend on a display format.
_RETURNING = ", ".join(("txn_id", "txn_code") + _INSERT_COLS + ("created_at",))


class ScopeError(Exception):
    """Caller may not act on the requested warehouse/floor."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def _normalise_warehouse(code: Optional[str]) -> str:
    """'W-202' -> 'W202'.

    auth_user.allowed_warehouses carries BOTH spellings (a single row can hold
    {W202,A185,W-202,A-185}) while stocktake_entries only ever uses the
    unhyphenated form, so the ledger stores the unhyphenated one or nothing joins.
    Mirrors normaliseWarehouseCode in web_replica/src/lib/warehouseScope.ts.
    """
    return _norm(code).replace("-", "")


def effective_scope(
    user: Any,
    *,
    available_warehouses: Optional[Sequence[str]] = None,
    available_floors: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """The warehouses and floors this user may post against.

    CRITICAL SEMANTIC: an EMPTY `allowed_floors` / `allowed_warehouses` means
    "no restriction", NOT "no access" — auth_schema.sql:35 says so, the auth
    middleware only enforces scope `if user.allowed_floors`, and the profile
    screen renders an empty list as "All". Admins bypass scope entirely
    (middleware.py:160). Treating empty as a denial locked out every
    unrestricted user, including admins.

    So an unrestricted user is offered everything stock actually exists at
    (the distinct values in stocktake_entries) and picks one; a scoped user is
    offered only their grants.
    """
    granted_floors = [f for f in (user.allowed_floors or []) if str(f).strip()]
    granted_whs = [w for w in (user.allowed_warehouses or []) if str(w).strip()]
    is_admin = bool(getattr(user, "is_admin", False))

    floors_unrestricted = is_admin or not granted_floors
    whs_unrestricted = is_admin or not granted_whs

    floors = list(available_floors or []) if floors_unrestricted else granted_floors
    whs_raw = list(available_warehouses or []) if whs_unrestricted else granted_whs
    whs = sorted({_normalise_warehouse(w) for w in whs_raw if str(w).strip()})

    return {
        "warehouses": whs,
        "floors": floors,
        "warehouses_unrestricted": whs_unrestricted,
        "floors_unrestricted": floors_unrestricted,
    }


def resolve_scope(
    user: Any, *, warehouse: Optional[str], location: Optional[str],
    available_warehouses: Optional[Sequence[str]] = None,
    available_floors: Optional[Sequence[str]] = None,
) -> tuple[str, str]:
    """The (warehouse, floor) this transaction is attributed to, or ScopeError.

    Policy: pin when exactly one is available, require a choice among several,
    and refuse only when there is genuinely nothing to choose from. "Available"
    means the user's grants, or everything in the data when they are unrestricted
    — see effective_scope for why empty grants are not a denial.
    """
    scope = effective_scope(
        user, available_warehouses=available_warehouses, available_floors=available_floors)
    floors = scope["floors"]
    if not floors:
        # Distinguish the two very different causes. An UNRESTRICTED user with
        # nothing available is not a permissions problem at all — it means this
        # server's database has no stock-take data (the Supabase config carries
        # no stocktake tables, and fetch_filter_options degrades to an empty list
        # rather than erroring). Reporting that as "ask an administrator for
        # floor access" sends people to fix the wrong thing.
        if scope["floors_unrestricted"]:
            raise ScopeError(
                "no_stock_data",
                "No stock-take locations are available on this server. Its database has no "
                "stocktake_entries data — check which database DATABASE_URL points at.",
                {"unrestricted": True},
            )
        raise ScopeError(
            "no_floor_access",
            "You have no floor assigned, so a stock transaction cannot be attributed to a "
            "location. Ask an administrator to set your floor access.",
            {"allowed_floors": list(user.allowed_floors or [])},
        )
    if location:
        match = next((f for f in floors if _norm(f) == _norm(location)), None)
        if match is None:
            raise ScopeError(
                "floor_not_allowed",
                f"You are not assigned to floor {location!r}.",
                {"requested": location, "allowed_floors": floors},
            )
        floor = match  # stored as GRANTED, preserving the Title Case spelling
    elif len(floors) == 1:
        floor = floors[0]
    else:
        raise ScopeError(
            "floor_required",
            "You are assigned to several floors — choose which one this transaction is for.",
            {"allowed_floors": floors},
        )

    canonical = scope["warehouses"]
    if not canonical:
        raise ScopeError(
            "no_warehouse_access",
            "There is no warehouse available to attribute this transaction to. "
            "Ask an administrator to set your warehouse access.",
            {"allowed_warehouses": list(user.allowed_warehouses or [])},
        )
    if warehouse:
        wh = _normalise_warehouse(warehouse)
        if wh not in canonical:
            raise ScopeError(
                "warehouse_not_allowed",
                f"You are not assigned to warehouse {warehouse!r}.",
                {"requested": warehouse, "allowed_warehouses": canonical},
            )
    elif len(canonical) == 1:
        wh = canonical[0]
    else:
        raise ScopeError(
            "warehouse_required",
            "You are assigned to several warehouses — choose which one this transaction is for.",
            {"allowed_warehouses": canonical},
        )
    return wh, floor


async def current_balance(
    conn: asyncpg.Connection, *, item_name: str, stock_type: str, warehouse: str, location: str,
) -> dict[str, Any]:
    """Counted quantity for one article at one place, netted with posted ledger rows.

    Returned to the caller so the form can WARN on an overdraw. It is advisory by
    design — the decision was "warn but allow" — so nothing here rejects.

    The baseline is that article's own latest count date AT THIS warehouse+floor,
    not the global latest: a floor counted last week must net against its own
    count, not against a day it was not counted on.
    """
    key = _norm(item_name)
    # Both day expressions come from business_day: the two tables store their
    # timestamps differently, so the same "day" is not the same SQL.
    row = await conn.fetchrow(
        f"""
        WITH scoped AS (
            SELECT * FROM stocktake_entries
             WHERE (status IS NULL OR status != 'draft')
               -- Physical counts only. The adjustment write-back puts an
               -- ADJUSTMENT row in this table too; counting it here would both
               -- double it (the ledger subquery below already has it) and move
               -- this article's baseline onto a day nobody counted.
               AND (source_kind IS NULL OR source_kind = 'COUNT')
               AND UPPER(BTRIM(item_name))  = $1
               AND COALESCE(stock_type, 'Fresh Stock') = $2
               AND UPPER(BTRIM(warehouse))  = $3
               AND UPPER(BTRIM(floor_name)) = $4
        ),
        baseline AS (SELECT MAX({ENTRY_DAY}) AS d FROM scoped)
        SELECT
            (SELECT d FROM baseline)                                        AS as_of_date,
            COALESCE((SELECT SUM(total_weight) FROM scoped
                       WHERE {ENTRY_DAY} = (SELECT d FROM baseline)), 0) AS counted_kg,
            COALESCE((SELECT SUM(CASE WHEN operation = 'ADDITION' THEN qty_kg ELSE -qty_kg END)
                        FROM stocktake_transactions
                       WHERE UPPER(BTRIM(item_name)) = $1
                         AND COALESCE(stock_type, 'Fresh Stock') = $2
                         AND UPPER(BTRIM(warehouse))  = $3
                         AND UPPER(BTRIM(location))   = $4
                         AND ((SELECT d FROM baseline) IS NULL
                              OR {TXN_DAY} >= (SELECT d FROM baseline))), 0) AS net_adjustment_kg
        """,
        key, stock_type, _normalise_warehouse(warehouse), _norm(location),
    )
    counted = float(row["counted_kg"] or 0)
    net = float(row["net_adjustment_kg"] or 0)
    d = row["as_of_date"]
    return {
        "as_of_date": d.isoformat() if d else None,
        "counted_kg": counted,
        "net_adjustment_kg": net,
        "available_kg": counted + net,
        # True when the article has never been counted at this place — the
        # "new article" case. A subtraction against it is allowed but flagged.
        "uncounted": d is None,
    }


async def create_transaction(
    conn: asyncpg.Connection, payload: dict[str, Any], *, warehouse: str, location: str,
    created_by: str, created_by_user_id: Optional[int],
) -> dict[str, Any]:
    """Insert one ledger row and return it, with the balance it was posted against.

    Caller supplies an open transaction: the balance read and the insert must see
    the same snapshot, otherwise the warning reported back describes a state that
    no longer exists.
    """
    operation = str(payload.get("operation", "")).upper()
    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {OPERATIONS}, got {operation!r}")

    reverses = payload.get("reverses_txn_id")
    is_reversal = reverses is not None
    reverses_code = None
    if is_reversal:
        target = await conn.fetchrow(
            "SELECT txn_id, txn_code, is_reversal FROM stocktake_transactions WHERE txn_id = $1",
            reverses)
        if target is None:
            raise ValueError(f"Transaction {reverses} does not exist")
        if target["is_reversal"]:
            # Matches material_document.create_reversal, which refuses to reverse
            # a reversal — otherwise a correction chain has no defined direction.
            raise ValueError(f"Transaction {reverses} is itself a reversal and cannot be reversed")
        reverses_code = target["txn_code"]

    item_name = _norm(payload.get("item_name"))
    if not item_name:
        raise ValueError("item_name is required")
    stock_type = (payload.get("stock_type") or "Fresh Stock").strip() or "Fresh Stock"

    balance = await current_balance(
        conn, item_name=item_name, stock_type=stock_type, warehouse=warehouse, location=location)

    values = {
        "item_name": item_name,
        "sku_id": payload.get("sku_id"),
        "is_new_article": bool(payload.get("is_new_article", False)),
        "material_type": (payload.get("material_type") or "").strip(),
        "item_category": (payload.get("item_category") or "").strip(),
        "item_subcategory": (payload.get("item_subcategory") or "").strip(),
        "stock_type": stock_type,
        "units": payload.get("units"),
        "qty_kg": payload.get("qty_kg"),
        "operation": operation,
        "reason": (payload.get("reason") or "").strip(),
        # Never from the body — see the module docstring.
        "warehouse": warehouse,
        "location": location,
        "reverses_txn_id": reverses,
        "is_reversal": is_reversal,
        "created_by": created_by,
        "created_by_user_id": created_by_user_id,
    }

    placeholders = ", ".join(f"${i}" for i in range(1, len(_INSERT_COLS) + 1))
    row = await conn.fetchrow(
        f"INSERT INTO stocktake_transactions ({', '.join(_INSERT_COLS)}) "
        f"VALUES ({placeholders}) RETURNING {_RETURNING}",
        *(values[c] for c in _INSERT_COLS),
    )

    created = dict(row)
    created["created_at"] = created["created_at"].isoformat()
    # Same shape the list/export rows carry, so a client can render a freshly
    # posted reversal without re-fetching the page.
    created["reverses_txn_code"] = reverses_code
    for k in ("units", "qty_kg"):
        created[k] = float(created[k]) if created[k] is not None else None

    # Cast ONCE, then branch. The previous form returned a Decimal for ADDITION
    # and a float for SUBTRACTION; float() downstream hid it, but mixing the two
    # types raises TypeError the moment someone arithmetics them together.
    qty = float(values["qty_kg"])
    delta = qty if operation == "ADDITION" else -qty
    sign = 1.0 if operation == "ADDITION" else -1.0

    # Mirror the movement into stocktake_entries. Same transaction as the ledger
    # INSERT above, deliberately: the caller owns the transaction, so either both
    # rows land or neither does. A ledger row without its entries row (or the
    # reverse) could never be reconciled afterwards, because the ledger blocks
    # UPDATE and DELETE while stocktake_entries does not.
    entry = await write_back_entry(
        conn,
        item_name=item_name, stock_type=stock_type,
        warehouse=warehouse, location=location,
        units_delta=float(values["units"]) * sign,
        kg_delta=qty * sign,
        actor=created_by,
        material_type=values["material_type"],
        item_category=values["item_category"],
        item_subcategory=values["item_subcategory"],
    )

    return {
        "transaction": created,
        "balance_before": balance,
        "balance_after_kg": balance["available_kg"] + delta,
        # Advisory only — "warn but allow" was the decision, so this never blocks.
        "overdrawn": operation == "SUBTRACTION" and qty > balance["available_kg"],
        # What the write-back did, so a caller can show or log it.
        "stock_entry": entry,
    }


def _ledger_filters(
    *, warehouse: Optional[str] = None, location: Optional[str] = None,
    item_name: Optional[str] = None, on_date: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    operation: Optional[str] = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    """Shared WHERE for both the paged view and the unpaged export.

    One builder on purpose: an export that filtered differently from the screen
    it was launched from would quietly hand someone a spreadsheet that does not
    match what they were looking at.

    Dates are compared on the IST calendar day (business_day.TXN_DAY), NOT on the
    server's UTC day: a filter for "the 5th" must return what an operator posted
    on the 5th as they saw it, and it must agree with the date encoded in
    txn_code. `on_date` is an exact day and wins over the range; a half-open
    range is allowed (only a start, or only an end).
    """
    conds: list[str] = []
    params: list[Any] = []
    applied: dict[str, Any] = {}

    def add(sql: str, value: Any, key: str, echo: Any) -> None:
        params.append(value)
        conds.append(sql.format(n=len(params)))
        applied[key] = echo

    if warehouse:
        add("UPPER(BTRIM(warehouse)) = ${n}", _normalise_warehouse(warehouse),
            "warehouse", _normalise_warehouse(warehouse))
    if location:
        add("UPPER(BTRIM(location)) = ${n}", _norm(location), "location", location)
    if item_name:
        add("UPPER(BTRIM(item_name)) = ${n}", _norm(item_name), "itemName", item_name)
    if operation:
        op = str(operation).upper()
        if op not in OPERATIONS:
            raise ValueError(f"operation must be one of {OPERATIONS}, got {operation!r}")
        add("operation = ${n}", op, "operation", op)

    def _day(v: str, label: str):
        from .latest_stock_service import normalise_date
        d = normalise_date(v)
        if d is None:
            raise ValueError(f"Invalid {label} date {v!r}; expected YYYY-MM-DD")
        return d

    if on_date:
        add(TXN_DAY + " = ${n}::date", _day(on_date, "date"), "date", on_date)
    else:
        if date_from:
            add(TXN_DAY + " >= ${n}::date", _day(date_from, "dateFrom"), "dateFrom", date_from)
        if date_to:
            add(TXN_DAY + " <= ${n}::date", _day(date_to, "dateTo"), "dateTo", date_to)

    return (f"WHERE {' AND '.join(conds)}" if conds else ""), params, applied


def _shape(rows) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = d["created_at"].isoformat()
        for k in ("units", "qty_kg"):
            d[k] = float(d[k]) if d[k] is not None else None
        out.append(d)
    return out


async def write_back_entry(
    conn: asyncpg.Connection, *, item_name: str, stock_type: str,
    warehouse: str, location: str, units_delta: float, kg_delta: float,
    actor: str, material_type: str = "", item_category: str = "",
    item_subcategory: str = "",
) -> dict[str, Any]:
    """Fold one adjustment into TODAY's stocktake_entries row for this article.

    "Today" is the Asia/Kolkata day (business_day.ENTRY_DAY), so an adjustment
    posted at 1am IST lands on the day the operator thinks it is rather than on
    the previous UTC day.

    One statement, not read-then-write: uq_entries_adjustment_day makes
    (IST day, item, warehouse, floor, stock_type) unique among ADJUSTMENT rows,
    so ON CONFLICT does "update today's row if it exists, else create it"
    atomically. Two operators adjusting the same article at the same moment
    therefore accumulate instead of racing to insert duplicates.

    Deltas are SIGNED and accumulate. The row holds the net movement for the
    day, not a stock level — a subtraction leaves it negative, which is correct
    for a delta row and is why nothing here clamps at zero.

    created_at is written as NAIVE UTC to match how backend_st writes it
    (routes/items.ts passes the SQL literal CURRENT_TIMESTAMP into a
    `timestamp WITHOUT time zone` column on a UTC server). Writing naive IST
    would put the row 5.5 hours in the future for every other reader of this
    table.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO stocktake_entries
            (item_name, item_type, item_category, item_subcategory,
             floor_name, warehouse, total_quantity, unit_uom, total_weight,
             entered_by, authority, stock_type, status, source_kind,
             verified, verified_by, verified_at, is_checked,
             created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, $9, 'Console adjustment',
                $10, 'submitted', 'ADJUSTMENT',
                -- Pre-verified ON PURPOSE. The floor app's getFloorSummaries
                -- (Stock_Take/backend_st/routes/items.ts:1174-1190) reports
                -- COUNT(*) FILTER (WHERE COALESCE(verified,false)=false) as a
                -- manager's "unverified" queue. A console adjustment is
                -- authorised at the point it is posted — it carries a mandatory
                -- reason and a named actor — so leaving it unverified would put
                -- work in that queue that no floor manager can meaningfully
                -- verify, and would grow every time anyone adjusts stock.
                TRUE, $9, (now() AT TIME ZONE 'UTC'), TRUE,
                (now() AT TIME ZONE 'UTC'), (now() AT TIME ZONE 'UTC'))
        ON CONFLICT ((((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date),
                     UPPER(BTRIM(item_name)), UPPER(BTRIM(warehouse)),
                     UPPER(BTRIM(floor_name)), stock_type)
                WHERE source_kind = 'ADJUSTMENT'
        DO UPDATE SET
            total_quantity = stocktake_entries.total_quantity + EXCLUDED.total_quantity,
            total_weight   = stocktake_entries.total_weight   + EXCLUDED.total_weight,
            updated_at     = (now() AT TIME ZONE 'UTC')
        RETURNING id, total_quantity, total_weight,
                  (xmax = 0) AS created_new
        """,
        item_name, (material_type or None), (item_category or None),
        (item_subcategory or None), location, warehouse,
        units_delta, kg_delta, actor, stock_type,
    )
    return {
        "entry_id": row["id"],
        "created_new": bool(row["created_new"]),
        "day_total_quantity": float(row["total_quantity"] or 0),
        "day_total_weight": float(row["total_weight"] or 0),
    }


async def _attach_reverses_code(conn: asyncpg.Connection, rows: list[dict[str, Any]]) -> None:
    """Resolve each reversal's target txn_id to the 8-digit code, in place.

    A second small query rather than a self-join on the main SELECT: the filters
    in _ledger_filters name bare columns (`warehouse`, `created_at`), so aliasing
    the ledger for a join would make every one of them ambiguous — a silent
    source of wrong results the moment someone adds a filter. Reversals are rare
    and the id list is at most one page long, so the extra round trip is cheap.
    """
    targets = {r["reverses_txn_id"] for r in rows if r.get("reverses_txn_id")}
    codes = {}
    if targets:
        codes = {r["txn_id"]: r["txn_code"] for r in await conn.fetch(
            "SELECT txn_id, txn_code FROM stocktake_transactions WHERE txn_id = ANY($1::bigint[])",
            list(targets))}
    for r in rows:
        r["reverses_txn_code"] = codes.get(r.get("reverses_txn_id"))


async def list_transactions(
    conn: asyncpg.Connection, *, page: int = 1, page_size: int = 200, **filters: Any,
) -> dict[str, Any]:
    """A page of ledger rows, newest first. Read-only."""
    where, params, applied = _ledger_filters(**filters)
    total = await conn.fetchval(
        f"SELECT COUNT(*)::bigint FROM stocktake_transactions {where}", *params) or 0
    rows = await conn.fetch(
        f"SELECT {_RETURNING} FROM stocktake_transactions {where} "
        f"ORDER BY created_at DESC, txn_id DESC LIMIT ${len(params)+1} OFFSET ${len(params)+2}",
        *params, page_size, (page - 1) * page_size,
    )
    shaped = _shape(rows)
    await _attach_reverses_code(conn, shaped)
    return {
        "transactions": shaped,
        "pagination": {
            "page": page, "page_size": page_size, "total": int(total),
            "total_pages": (int(total) + page_size - 1) // page_size if total else 0,
        },
        "filters": applied,
    }


async def export_transactions(conn: asyncpg.Connection, **filters: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """EVERY matching ledger row, unpaginated, for the spreadsheet.

    Deliberately no LIMIT: a truncated export is worse than a slow one, because
    the recipient cannot tell it is partial. The ledger is append-only and small
    relative to stocktake_entries, and idx_stk_txn_created_at covers the sort.
    """
    where, params, applied = _ledger_filters(**filters)
    rows = await conn.fetch(
        f"SELECT {_RETURNING} FROM stocktake_transactions {where} "
        f"ORDER BY created_at DESC, txn_id DESC",
        *params,
    )
    shaped = _shape(rows)
    await _attach_reverses_code(conn, shaped)
    return shaped, applied
