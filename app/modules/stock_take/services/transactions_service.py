"""Append-only stock adjustment ledger over `new_stock_entries`.

One row per physical movement recorded between counts. A posted row is FINAL —
`stocktake_transactions` blocks UPDATE and DELETE at the database level
(app/db/098_stocktake_transactions.sql), so a mistake is corrected by posting a
reversal, never by editing.

SCOPE IS DERIVED FROM THE TOKEN, NOT THE BODY. `warehouse` and `location` come
from the caller's `allowed_warehouses` / `allowed_floors`; a request body cannot
name a floor the user was not granted. That is the actual access control here —
the endpoint itself is open to any authenticated user, matching the read side.

ARTICLE IDENTITY IS A STRING. `new_stock_entries.item_name` is free text with no
FK, and the Stock Take floor UI deliberately allows custom items, so a
transaction joins counted stock on UPPER(BTRIM(item_name)) plus stock_type — the
same identity both latest-stock implementations use. `sku_id` is recorded when
the operator picked from the catalogue, purely as audit trail.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional, Sequence

import asyncpg

from .. import floors as _floors
from .business_day import BUSINESS_TZ, ENTRIES_TABLE, ENTRY_DAY, TXN_DAY

log = logging.getLogger(__name__)

OPERATIONS = ("ADDITION", "SUBTRACTION")

#: The only two values chk_nse_stock_type admits. Validated HERE, not left to
#: that CHECK, because write_back_entry runs inside create_transaction's
#: transaction: a mis-cased "off grade/rejection" would abort the ledger INSERT
#: too, losing the posting entirely and reporting it as a 500. Stock type is half
#: the article identity, so a near-miss is not a typo to shrug at -- it would
#: silently open a third line for an article that is meant to have two.
STOCK_TYPES = ("Fresh Stock", "Off Grade/Rejection")

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
#
# verified/verified_by/verified_at are read back but never inserted: a posting is
# born unsigned (110_stocktake_txn_verification.sql defaults them), and the only
# way they change afterwards is verify_transactions or the cascade out of
# verify_entries. Putting them in _INSERT_COLS would let a poster sign their own
# work, which is the control 108 exists to add.
_VERIFY_COLS = ("verified", "verified_by", "verified_at")
_RETURNING = ", ".join(
    ("txn_id", "txn_code") + _INSERT_COLS + ("created_at",) + _VERIFY_COLS)

#: The group a sign-off reconciles over: one adjustment row in new_stock_entries
#: covers every posting for one article, at one place, of one stock type, on one
#: IST day — the key uq_nse_adjustment_day enforces.
#:
#: WRITTEN ONCE, USED FOUR TIMES. Both reconciliation directions, the backfill in
#: 110 and the index it creates all have to agree character for character, and an
#: index is not an error when it fails to match — it is just silently unused. The
#: warehouse is de-hyphenated on THIS side only: the ledger holds both 'W-202' and
#: 'W202', new_stock_entries only the unhyphenated form.
TXN_GROUP_KEY = (
    "(created_at AT TIME ZONE 'Asia/Kolkata')::date",
    "UPPER(BTRIM(item_name))",
    "REPLACE(UPPER(BTRIM(COALESCE(warehouse, ''))), '-', '')",
    "UPPER(BTRIM(COALESCE(location, '')))",
    "COALESCE(stock_type, 'Fresh Stock')",
)
_GROUP_ALIASES = ("k_day", "k_item", "k_wh", "k_fl", "k_stock")
#: "expr AS alias, expr AS alias, ..." for a SELECT list.
TXN_GROUP_SELECT = ", ".join(
    f"{expr} AS {alias}" for expr, alias in zip(TXN_GROUP_KEY, _GROUP_ALIASES))

#: The same five values read off a new_stock_entries row. Deliberately NOT
#: de-hyphenated — that table only ever holds the unhyphenated spelling, and
#: applying REPLACE here too would hide a real mismatch rather than expose it.
ENTRY_GROUP_KEY = (
    "(created_at AT TIME ZONE 'Asia/Kolkata')::date",
    "UPPER(BTRIM(item_name))",
    "UPPER(BTRIM(COALESCE(warehouse, '')))",
    "UPPER(BTRIM(COALESCE(floor_name, '')))",
    "COALESCE(stock_type, 'Fresh Stock')",
)
ENTRY_GROUP_SELECT = ", ".join(
    f"{expr} AS {alias}" for expr, alias in zip(ENTRY_GROUP_KEY, _GROUP_ALIASES))


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
    {W202,A185,W-202,A-185}) while the entries table only ever uses the
    unhyphenated form, so the ledger stores the unhyphenated one or nothing joins.
    Mirrors normaliseWarehouseCode in web_replica/src/lib/warehouseScope.ts.
    """
    return _norm(code).replace("-", "")


def effective_scope(
    user: Any,
    *,
    available_warehouses: Optional[Sequence[str]] = None,
    available_floors: Optional[Sequence[str]] = None,
    places: Optional[dict[str, list[str]]] = None,
) -> dict[str, Any]:
    """The warehouses and floors this user may post against.

    CRITICAL SEMANTIC: an EMPTY `allowed_floors` / `allowed_warehouses` means
    "no restriction", NOT "no access" — auth_schema.sql:35 says so, the auth
    middleware only enforces scope `if user.allowed_floors`, and the profile
    screen renders an empty list as "All". Treating empty as a denial locked out
    every unrestricted user, including admins.

    BEING ADMIN MEANS NOT BEING BLOCKED, NOT BEING UNASSIGNED. This used to read
    `is_admin or not granted_floors`, which threw away an admin's actual profile
    and offered them every warehouse and every floor_name in the table. An admin
    assigned W202 was still shown A185 and F53. Admins already bypass the
    enforcement check (middleware.py:160), so nothing here needs to widen them a
    second time — and a dropdown is a suggestion, not a permission.

    WHAT "UNRESTRICTED" IS OFFERED. The admin screen labels its floor control
    "filtered by warehouses; empty = all in those warehouses", so that is what an
    empty grant means: every floor the SCOPED WAREHOUSES declare, not every
    string the table happens to contain. Offering the latter put TEESTTTT,
    TEST FLOOR, 1ST : FIRST LINE and REJECTION COLD & RACK (a stock type, not a
    place) in front of people as though they were locations. Warehouses that
    declare no floors at all fall back to their own data — see floors.floors_for.
    """
    granted_floors = [f for f in (user.allowed_floors or []) if str(f).strip()]
    granted_whs = [w for w in (user.allowed_warehouses or []) if str(w).strip()]

    floors_unrestricted = not granted_floors
    whs_unrestricted = not granted_whs

    whs_raw = list(available_warehouses or []) if whs_unrestricted else granted_whs
    whs = sorted({_normalise_warehouse(w) for w in whs_raw if str(w).strip()})

    # Per warehouse, so the form can narrow the floor list once a warehouse is
    # chosen instead of listing another building's floors.
    places = places or {}
    by_wh: dict[str, list[str]] = {}
    for wh in whs:
        offered = _floors.FLOORS_BY_WAREHOUSE.get(wh) or places.get(wh) or []
        if not floors_unrestricted:
            keep = {_norm(f) for f in granted_floors}
            offered = [f for f in offered if _norm(f) in keep]
        by_wh[wh] = list(offered)

    floors: list[str] = []
    for wh in whs:
        for f in by_wh[wh]:
            if f not in floors:
                floors.append(f)

    return {
        "warehouses": whs,
        "floors": floors,
        "floors_by_warehouse": by_wh,
        "warehouses_unrestricted": whs_unrestricted,
        "floors_unrestricted": floors_unrestricted,
    }


def resolve_scope(
    user: Any, *, warehouse: Optional[str], location: Optional[str],
    available_warehouses: Optional[Sequence[str]] = None,
    available_floors: Optional[Sequence[str]] = None,
    places: Optional[dict[str, list[str]]] = None,
) -> tuple[str, str]:
    """The (warehouse, floor) this transaction is attributed to, or ScopeError.

    Policy: pin when exactly one is available, require a choice among several,
    and refuse only when there is genuinely nothing to choose from. "Available"
    means the user's grants, or everything in the data when they are unrestricted
    — see effective_scope for why empty grants are not a denial.
    """
    scope = effective_scope(
        user, available_warehouses=available_warehouses,
        available_floors=available_floors, places=places)
    # Narrow to the requested warehouse before checking the floor, so "is this
    # floor allowed" is asked about the building the caller actually named.
    floors = (scope["floors_by_warehouse"].get(_normalise_warehouse(warehouse))
              if warehouse else None) or scope["floors"]

    # WAREHOUSE FIRST. Floors are now derived FROM the warehouses, so a caller
    # with no warehouse also has no floors — and reporting that as "you have no
    # floor assigned" sends them to fix the wrong half of their profile.
    if not scope["warehouses"]:
        raise ScopeError(
            "no_warehouse_access",
            "There is no warehouse available to attribute this transaction to. "
            "Ask an administrator to set your warehouse access.",
            {"allowed_warehouses": list(user.allowed_warehouses or [])},
        )
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
                "entries data — check which database DATABASE_URL points at.",
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
            SELECT * FROM {ENTRIES_TABLE}
             WHERE (status IS NULL OR status != 'draft')
               -- Physical counts only. The adjustment write-back puts an
               -- ADJUSTMENT row in this table too; counting it here would both
               -- double it (the ledger subquery below already has it) and move
               -- this article's baseline onto a day nobody counted.
               AND (source_kind IS NULL OR source_kind = 'COUNT')
               AND UPPER(BTRIM(item_name))  = $1
               AND COALESCE(stock_type, 'Fresh Stock') = $2
               -- Hyphen-blind: the cold stores' counts are stored as 'D-39'
               -- while $3 (and every ledger row) is 'D39'.
               AND REPLACE(UPPER(BTRIM(warehouse)), '-', '') = $3
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
                         AND REPLACE(UPPER(BTRIM(warehouse)), '-', '') = $3
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
    if stock_type not in STOCK_TYPES:
        raise ValueError(
            f"stock_type must be one of {STOCK_TYPES}, got {stock_type!r}")

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

    # Mirror the movement into the entries table. Same transaction as the ledger
    # INSERT above, deliberately: the caller owns the transaction, so either both
    # rows land or neither does. A ledger row without its entries row (or the
    # reverse) could never be reconciled afterwards, because the ledger blocks
    # UPDATE and DELETE while the entries table does not.
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
    item_name: Optional[str] = None, item_search: Optional[str] = None,
    stock_type: Optional[str] = None, on_date: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    operation: Optional[str] = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    """Shared WHERE for both the paged view and the unpaged export.

    One builder on purpose: an export that filtered differently from the screen
    it was launched from would quietly hand someone a spreadsheet that does not
    match what they were looking at.

    TWO ARTICLE FILTERS, AND THEY ARE NOT INTERCHANGEABLE. `item_name` is an
    exact match because it means identity: the adjust screen expands one row and
    asks this for the postings behind that row's number. `item_search` is the
    substring the ledger screen's Article box sends. Collapsing them into one
    loose match would be a silent wrong answer rather than a missing feature --
    nine article pairs in this ledger have one name contained in another, so
    "ZAHIDI DATES" as a substring pulls in PL ZAHIDI DATES 500G, ZAHIDI DATES 1KG
    and ZAHIDI DATES ROASTED & CUT, and the row breakdown would report their
    movements as its own.

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
        # Column normalised too: the ledger holds both 'W-202' and 'W202'.
        add("REPLACE(UPPER(BTRIM(warehouse)), '-', '') = ${n}", _normalise_warehouse(warehouse),
            "warehouse", _normalise_warehouse(warehouse))
    if location:
        add("UPPER(BTRIM(location)) = ${n}", _norm(location), "location", location)
    if item_name:
        add("UPPER(BTRIM(item_name)) = ${n}", _norm(item_name), "itemName", item_name)
    if item_search:
        # Wildcards are escaped, so someone searching for "50%" gets the literal
        # string rather than every row. Same helper the counting screen uses, so
        # the two search boxes behave identically.
        from .latest_stock_service import _like
        add("UPPER(item_name) LIKE ${n} ESCAPE '\\'", _like(str(item_search)),
            "itemSearch", item_search)
    if stock_type:
        if stock_type not in STOCK_TYPES:
            raise ValueError(f"stock_type must be one of {STOCK_TYPES}, got {stock_type!r}")
        # COALESCE because the column is nullable and a null has always meant
        # Fresh Stock everywhere else in this module; without it, filtering for
        # Fresh Stock would quietly drop the rows that predate the column.
        add("COALESCE(stock_type, 'Fresh Stock') = ${n}", stock_type, "stockType", stock_type)
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
        # The IST calendar day, kept next to the raw instant. created_at is
        # timestamptz, so its .isoformat() is UTC and slicing [:10] off it would
        # put an adjustment posted after 18:30 IST on the previous day -- which
        # is exactly the key used to find its adjustment row.
        d["business_day"] = TXN_DAY_OF(d["created_at"])
        d["created_at"] = d["created_at"].isoformat()
        for k in ("units", "qty_kg"):
            d[k] = float(d[k]) if d[k] is not None else None
        # The sign-off now comes off the row itself rather than being looked up
        # afterwards (110_stocktake_txn_verification.sql). Same three keys the
        # old _attach_verification stamped, so every caller and the .xlsx export
        # keep working unchanged.
        if "verified" in d:
            d["verified"] = bool(d["verified"])
            d["verified_at"] = (d["verified_at"].isoformat()
                                if d.get("verified_at") else None)
        out.append(d)
    return out


async def write_back_entry(
    conn: asyncpg.Connection, *, item_name: str, stock_type: str,
    warehouse: str, location: str, units_delta: float, kg_delta: float,
    actor: str, material_type: str = "", item_category: str = "",
    item_subcategory: str = "",
) -> dict[str, Any]:
    """Fold one adjustment into TODAY's `new_stock_entries` row for this article.

    "Today" is the Asia/Kolkata day (business_day.ENTRY_DAY), so an adjustment
    posted at 1am IST lands on the day the operator thinks it is rather than on
    the previous UTC day.

    One statement, not read-then-write: uq_nse_adjustment_day makes
    (IST day, item, warehouse, floor, stock_type) unique among ADJUSTMENT rows,
    so ON CONFLICT does "update today's row if it exists, else create it"
    atomically. Two operators adjusting the same article at the same moment
    therefore accumulate instead of racing to insert duplicates.

    THE INFERENCE CLAUSE MUST MATCH THAT INDEX CHARACTER FOR CHARACTER. It is not
    the shape the old uq_entries_adjustment_day had: warehouse and floor_name are
    wrapped in COALESCE(..., '') and the day is the one-step IST form. Postgres
    matches ON CONFLICT to an index by comparing parsed expressions, so a
    near-miss is not a silent fallback -- it raises "no unique or exclusion
    constraint matching the ON CONFLICT specification" on every adjustment.

    Deltas are SIGNED and accumulate. The row holds the net movement for the
    day, not a stock level — a subtraction leaves it negative, which is correct
    for a delta row and is why nothing here clamps at zero.

    created_at is written as a plain now(). The column is timestamptz here,
    unlike the naive-UTC column the floor app writes, so the old
    `now() AT TIME ZONE 'UTC'` would strip the zone off an already-absolute
    instant and land the row 5.5 hours in the past.
    """
    row = await conn.fetchrow(
        f"""
        INSERT INTO {ENTRIES_TABLE}
            (item_name, item_type, item_category, item_subcategory,
             floor_name, warehouse, total_quantity, unit_uom, total_weight,
             entered_by, authority, stock_type, status, source_kind,
             verified, verified_by, verified_at, is_checked,
             created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, $9, 'Console adjustment',
                $10, 'submitted', 'ADJUSTMENT',
                -- UNVERIFIED. This used to insert TRUE with the poster's own
                -- name, because the console then wrote stocktake_entries and an
                -- unverified row there landed in the floor managers' queue
                -- (Stock_Take/backend_st/routes/items.ts getFloorSummaries counts
                -- COUNT(*) FILTER (WHERE COALESCE(verified,false)=false)). The
                -- console now writes new_stock_entries, which that app never
                -- reads, so the reason is gone -- and a poster stamping their own
                -- name was never a verification. A separate `verify` action signs
                -- the day off; see 108_stock_take_verification_role.sql.
                FALSE, NULL, NULL, TRUE,
                now(), now())
        ON CONFLICT (((created_at AT TIME ZONE 'Asia/Kolkata')::date),
                     UPPER(BTRIM(item_name)),
                     UPPER(BTRIM(COALESCE(warehouse, ''))),
                     UPPER(BTRIM(COALESCE(floor_name, ''))), stock_type)
                WHERE source_kind = 'ADJUSTMENT'
        DO UPDATE SET
            total_quantity = {ENTRIES_TABLE}.total_quantity + EXCLUDED.total_quantity,
            total_weight   = {ENTRIES_TABLE}.total_weight   + EXCLUDED.total_weight,
            updated_at     = now(),
            -- "If anything changes, it needs verifying again." The day's figure
            -- just moved, so a sign-off given against the OLD figure no longer
            -- describes this row. Clearing all three here is what makes that
            -- automatic: there is no separate invalidation path to forget,
            -- because every further adjustment comes through this same upsert.
            verified       = FALSE,
            verified_by    = NULL,
            verified_at    = NULL
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


async def reconcile_entry_from_transactions(
    conn: asyncpg.Connection, *, actor: str, groups: Sequence[dict[str, Any]],
) -> int:
    """Rule 1: an adjustment row is signed exactly when all its postings are.

    Runs after verify_transactions, over only the groups that call touched. The
    rule is an equivalence, not a one-way trigger, so this both SETS and CLEARS:
    verifying the last unsigned posting in a group signs the row off, and
    un-verifying any one of them takes the signature back off. Anything else
    would let the row claim a sign-off that no longer describes its postings.

    THE EMPTY GROUP IS THE TRAP. "Every transaction is verified" is vacuously
    true of a group with no transactions at all, and most adjustment rows on the
    screen are for lines nobody has ever adjusted. COUNT(*) > 0 in the HAVING is
    what stops this from silently signing off the entire table.

    Returns the number of adjustment rows whose state actually moved.
    """
    if not groups:
        return 0

    rows = await conn.fetch(
        f"""
        WITH touched(k_day, k_item, k_wh, k_fl, k_stock) AS (
            SELECT x.k_day::date, x.k_item, x.k_wh, x.k_fl, x.k_stock
              FROM UNNEST($2::date[], $3::text[], $4::text[], $5::text[], $6::text[])
                     AS x(k_day, k_item, k_wh, k_fl, k_stock)
        ),
        state AS (
            SELECT {TXN_GROUP_SELECT},
                   BOOL_AND(verified) AS all_verified,
                   COUNT(*)           AS n
              FROM stocktake_transactions
             GROUP BY 1, 2, 3, 4, 5
            HAVING COUNT(*) > 0
        ),
        want AS (
            SELECT s.* FROM state s JOIN touched t USING (k_day, k_item, k_wh, k_fl, k_stock)
        )
        UPDATE {ENTRIES_TABLE} e
           SET verified    = w.all_verified,
               verified_by = CASE WHEN w.all_verified THEN $1 ELSE NULL END,
               verified_at = CASE WHEN w.all_verified THEN now() ELSE NULL END,
               updated_at  = now()
          FROM want w
         WHERE e.source_kind = 'ADJUSTMENT'
           AND ({ENTRY_GROUP_KEY[0]}, {ENTRY_GROUP_KEY[1]}, {ENTRY_GROUP_KEY[2]},
                {ENTRY_GROUP_KEY[3]}, {ENTRY_GROUP_KEY[4]})
               = (w.k_day, w.k_item, w.k_wh, w.k_fl, w.k_stock)
           -- Only rows whose state actually moves, so a no-op reconcile does not
           -- re-stamp a signature with a new name and time.
           AND COALESCE(e.verified, FALSE) IS DISTINCT FROM w.all_verified
        RETURNING e.id
        """,
        actor,
        [g["k_day"] for g in groups], [g["k_item"] for g in groups],
        [g["k_wh"] for g in groups], [g["k_fl"] for g in groups],
        [g["k_stock"] for g in groups],
    )
    return len(rows)


async def verify_transactions(
    conn: asyncpg.Connection, *, actor: str, txn_ids: Sequence[int],
    verified: bool = True,
) -> dict[str, Any]:
    """Sign off (or un-sign) individual postings, then apply rule 1.

    THE LEDGER IS STILL APPEND-ONLY. 110 narrowed trg_stk_txn_no_update to the
    three verification columns rather than removing it, so this UPDATE is the
    only shape of UPDATE the table accepts; touching any other column from here
    would raise exactly as it always did.

    ONLY ROWS WHOSE STATE MOVES ARE TOUCHED, so re-ticking an already-signed
    posting is a no-op rather than a re-stamp under a new name. That also keeps
    `changed` honest as "what this call did".

    Reversals are ordinary postings here: a correction is itself something
    somebody has to look at, so both it and the row it reverses must be signed
    before their line reconciles.
    """
    if not txn_ids:
        return {"changed": 0, "entries_reconciled": 0, "transactions": []}

    rows = await conn.fetch(
        f"""
        UPDATE stocktake_transactions
           SET verified    = $2,
               verified_by = CASE WHEN $2 THEN $3 ELSE NULL END,
               verified_at = CASE WHEN $2 THEN now() ELSE NULL END
         WHERE txn_id = ANY($1::bigint[])
           AND verified IS DISTINCT FROM $2
        RETURNING txn_id, txn_code, verified, verified_by, verified_at,
                  {TXN_GROUP_SELECT}
        """,
        list(txn_ids), verified, actor,
    )

    groups = {(r["k_day"], r["k_item"], r["k_wh"], r["k_fl"], r["k_stock"]) for r in rows}
    reconciled = await reconcile_entry_from_transactions(
        conn, actor=actor,
        groups=[dict(zip(_GROUP_ALIASES, g)) for g in groups])

    return {
        "changed": len(rows),
        "entries_reconciled": reconciled,
        "verified": verified,
        "transactions": [
            {"txn_id": r["txn_id"], "txn_code": r["txn_code"],
             "verified": bool(r["verified"]), "verified_by": r["verified_by"],
             "verified_at": r["verified_at"].isoformat() if r["verified_at"] else None}
            for r in rows
        ],
    }


async def cascade_transactions_from_entries(
    conn: asyncpg.Connection, *, actor: str, entry_ids: Sequence[int], verified: bool,
) -> int:
    """Rule 2: signing a line off signs off every posting behind it.

    The inverse of reconcile_entry_from_transactions, and deliberately a separate
    one-shot call rather than a database trigger on either table. Triggers in
    both directions would re-enter each other on every write; two explicit
    statements, each called once by the endpoint that owns the decision, cannot.

    A line with no postings -- most of them, since a stock position usually has
    only counts behind it -- matches nothing here and is a no-op.

    Returns the number of postings whose state actually moved.
    """
    if not entry_ids:
        return 0

    rows = await conn.fetch(
        f"""
        WITH target AS (
            SELECT {ENTRY_GROUP_SELECT}
              FROM {ENTRIES_TABLE}
             WHERE id = ANY($1::bigint[])
        )
        UPDATE stocktake_transactions t
           SET verified    = $2,
               verified_by = CASE WHEN $2 THEN $3 ELSE NULL END,
               verified_at = CASE WHEN $2 THEN now() ELSE NULL END
          FROM target g
         WHERE ({TXN_GROUP_KEY[0]}, {TXN_GROUP_KEY[1]}, {TXN_GROUP_KEY[2]},
                {TXN_GROUP_KEY[3]}, {TXN_GROUP_KEY[4]})
               = (g.k_day, g.k_item, g.k_wh, g.k_fl, g.k_stock)
           AND t.verified IS DISTINCT FROM $2
        RETURNING t.txn_id
        """,
        list(entry_ids), verified, actor,
    )
    return len(rows)


async def verify_entries(
    conn: asyncpg.Connection,
    *,
    actor: str,
    day: Optional[date] = None,
    warehouse: Optional[str] = None,
    location: Optional[str] = None,
    item_name: Optional[str] = None,
    stock_type: Optional[str] = None,
    entry_ids: Optional[Sequence[int]] = None,
    verified: bool = True,
) -> dict[str, Any]:
    """Sign off stock lines in new_stock_entries. Returns what was signed.

    COVERS COUNTS AS WELL AS ADJUSTMENTS. A line on the adjust screen is a stock
    position, and most positions have no adjustment at all -- restricting this to
    source_kind='ADJUSTMENT' would have left the majority of rows with nothing a
    reviewer could sign. What is being confirmed is the figure on the line.

    Verification lives here rather than on the ledger because
    stocktake_transactions is append-only and could not carry a mutable flag. A
    transaction reads its state back from the adjustment row it rolls into, on
    the key uq_nse_adjustment_day enforces, so one sign-off still covers every
    posting against that article and place on that day.

    WHEN `day` BINDS. Naming an article means "this line, however far back it
    goes", so the day filter is dropped -- a line's counted figure can be weeks
    old, and the screen says so ("Stock here as of 25 Aug 2026"). With NO article
    named it defaults to today, because an unbounded bulk sign-off across every
    article and every day is not something a button should do by accident.

    ONLY ROWS WHOSE STATE MOVES ARE TOUCHED. Re-running is therefore a no-op
    rather than a re-stamp with a new name and time, so a second click cannot
    quietly rewrite who signed a figure off.

    IT REVERSES NOW. `verified=False` withdraws the sign-off, clearing the name
    and time with it -- leaving the name of whoever last signed a row that is no
    longer signed reads as an accusation. Before per-transaction verification the
    only way back was to adjust the stock and let write_back_entry's upsert
    un-verify the line, which is still what happens when a new posting lands.

    THE CASCADE IS THE POINT. Every line this touches has its postings moved to
    match (rule 2, cascade_transactions_from_entries), and the inverse -- a line
    following its postings -- is rule 1 in reconcile_entry_from_transactions.
    The two together are one invariant: an adjustment row is signed exactly when
    every posting behind it is.
    """
    # Only rows whose state actually moves. As a one-way action this read
    # "= FALSE" and meant "a second click cannot quietly rewrite who signed a
    # figure off"; as a two-way one it has to mean the same in both directions,
    # so it is now a difference from the target rather than a fixed value.
    conds = ["COALESCE(verified, FALSE) IS DISTINCT FROM $2"]
    params: list[Any] = [actor, verified]

    if entry_ids:
        params.append(list(entry_ids))
        conds.append(f"id = ANY(${len(params)}::bigint[])")
    else:
        if day is not None:
            params.append(day)
            conds.append(f"{ENTRY_DAY} = ${len(params)}::date")
        elif not item_name:
            # No article and no day: fall back to TODAY in IST, not the server's
            # date. An adjustment posted at 01:00 IST belongs to the day the
            # operator is working, and this is the end-of-day button. An article
            # WAS named, so the line is signed however far back it goes.
            conds.append(f"{ENTRY_DAY} = (now() AT TIME ZONE 'Asia/Kolkata')::date")
        if warehouse:
            params.append(_normalise_warehouse(warehouse))
            # Hyphen-blind, so signing off a Savla line reaches its 'D-39' counts.
            conds.append(f"REPLACE(UPPER(BTRIM(COALESCE(warehouse, ''))), '-', '') = ${len(params)}")
        if location:
            params.append(_norm(location))
            conds.append(f"UPPER(BTRIM(COALESCE(floor_name, ''))) = ${len(params)}")
        # Narrowing to one article lets a reviewer sign off a single line on the
        # adjust screen. Matched on the same UPPER(BTRIM(...)) identity the rest
        # of the module uses, because item_name is free text with no FK.
        if item_name:
            params.append(_norm(item_name))
            conds.append(f"UPPER(BTRIM(item_name)) = ${len(params)}")
        if stock_type:
            params.append(stock_type)
            conds.append(f"COALESCE(stock_type, 'Fresh Stock') = ${len(params)}")

    rows = await conn.fetch(
        f"""
        UPDATE {ENTRIES_TABLE}
           SET verified    = $2,
               verified_by = CASE WHEN $2 THEN $1 ELSE NULL END,
               verified_at = CASE WHEN $2 THEN now() ELSE NULL END,
               updated_at  = now()
         WHERE {' AND '.join(conds)}
        RETURNING id, item_name, warehouse, floor_name, stock_type,
                  total_quantity, total_weight, verified_by, verified_at,
                  {ENTRY_DAY} AS day
        """,
        *params,
    )

    # RULE 2. Signing the line off signs off every posting behind it, and taking
    # the signature back off takes theirs off too -- otherwise un-verifying a
    # line would leave its postings claiming a sign-off the line itself no longer
    # has, and rule 1 would immediately put the line back. Done here rather than
    # in a database trigger: a trigger on each table would re-enter the other on
    # every write, where two explicit one-shot calls cannot.
    cascaded = await cascade_transactions_from_entries(
        conn, actor=actor, entry_ids=[r["id"] for r in rows], verified=verified)

    return {
        "verified_count": len(rows),
        "verified": verified,
        "transactions_cascaded": cascaded,
        "verified_by": actor if verified else None,
        "rows": [
            {
                "entry_id": r["id"],
                "item_name": r["item_name"],
                "warehouse": r["warehouse"],
                "floor_name": r["floor_name"],
                "stock_type": r["stock_type"],
                "total_weight": float(r["total_weight"] or 0),
                "verified_by": r["verified_by"],
                "verified_at": r["verified_at"],
                "day": r["day"],
            }
            for r in rows
        ],
    }


def TXN_DAY_OF(ts) -> str:
    """The Asia/Kolkata calendar day of a timestamptz, as YYYY-MM-DD.

    The Python counterpart of business_day.TXN_DAY. Both exist because the day
    is needed on both sides of the same question -- SQL filters on it, and the
    verification join keys on it -- and they must agree.
    """
    from zoneinfo import ZoneInfo
    return ts.astimezone(ZoneInfo(BUSINESS_TZ)).date().isoformat()


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
    relative to the entries table, and idx_stk_txn_created_at covers the sort.
    """
    where, params, applied = _ledger_filters(**filters)
    rows = await conn.fetch(
        f"SELECT {_RETURNING} FROM stocktake_transactions {where} "
        f"ORDER BY created_at DESC, txn_id DESC",
        *params,
    )
    shaped = _shape(rows)
    await _attach_reverses_code(conn, shaped)
    # The spreadsheet carries the sign-off too -- an export that showed the
    # postings but not whether anyone had checked them is the half that gets
    # forwarded and acted on. It rides along in _RETURNING now rather than
    # needing a second query, because the sign-off is a column on the row.
    return shaped, applied
