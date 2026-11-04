"""Cold-storage stock search + FIFO box pick (doc 08 cold picker).

Read-only asyncpg ports of cold_storage_server.search_cold_storage_stocks / pick_boxes.
The cold tables (cfpl_cold_stocks / cdpl_cold_stocks) are externally managed; each query
is guarded by to_regclass so it returns empty where the tables are absent (e.g. this dev
DB) rather than erroring.

Where a box is, here as on every dispatch, is R3 (the transfer module's
lookup_service.unavailable_boxes): the box's latest transfer decides. No transfer: free
from any site. In transit, or written off as short: unavailable. Received and
acknowledged: available only from that transfer's To site. A transfer never deducts a
box from the cold table (record only), so the search's counts are the table's own; given
the From site of the dispatch being made it also says how many of each lot's boxes R3
lets go from there, and the pick returns only those boxes.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

from fastapi import HTTPException

from app.core.sites import cold_company, cold_sites, scope_keys
from app.core.warehouse_scope import WAREHOUSE_NORM_ANY_SQL
from app.modules.transfer.services import lookup_service

_COLD_TABLES = ("cfpl_cold_stocks", "cdpl_cold_stocks")
# Candidate rows read per round trip while the pick looks for qty boxes R3 lets go.
_PICK_PAGE = 200
# The keys of every spelling of every cold store: a row's unit names its store when it
# is one of these (sites.cold_row_site).
_COLD_KEYS = scope_keys(cold_sites())


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"))


def _get_cold_table(company: Optional[str]) -> str:
    return "cdpl_cold_stocks" if (company or "").strip().lower() == "cdpl" else "cfpl_cold_stocks"


def _company(table: str) -> str:
    return "cdpl" if table == "cdpl_cold_stocks" else "cfpl"


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _box_identity(box_id, transaction_no) -> Optional[tuple[str, str]]:
    """(box_id, transaction_no) of a row that is one trackable box, trimmed, as the
    transfer's box guard keys a box; None for a row without both (an inner cold split
    row, say) or with the quantity-only marker 'DIRECT' -- R3 cannot follow such a row,
    so the pick never returns it and available_here never counts it."""
    bid, tno = (box_id or "").strip(), (transaction_no or "").strip()
    if not bid or not tno or tno == "DIRECT":
        return None
    return bid, tno


def _excluded(exclude: Optional[str]) -> set[tuple[str, str]]:
    """The boxes in `exclude` ("box_id|transaction_no", comma-separated), keyed as
    _box_identity keys them; an entry without both parts is ignored."""
    out: set[tuple[str, str]] = set()
    for item in (exclude or "").split(","):
        bid, sep, tno = item.partition("|")
        key = _box_identity(bid, tno) if sep else None
        if key:
            out.add(key)
    return out


async def _unavailable(conn, keys: Iterable[tuple[str, str]], from_warehouse, transfer_id=None) -> set:
    """The keys R3 refuses from `from_warehouse` (none given: every transfer history
    refuses), leaving `transfer_id` out of each box's history; read only."""
    keys = list(keys)
    if not keys:
        return set()
    held = await lookup_service.unavailable_boxes(
        conn, keys, from_warehouse=(from_warehouse or "").strip() or None, transfer_id=transfer_id)
    return {(str(h["box_id"]).strip(), str(h["transaction_no"]).strip()) for h in held}


_SEARCH_SQL = """
    SELECT MIN(id) AS id,
           MIN(inward_dt) AS inward_dt,
           MIN(unit) AS unit,
           COALESCE(inward_no, '') AS inward_no,
           item_description,
           COALESCE(item_mark, '') AS item_mark,
           MAX(vakkal) AS vakkal,
           COALESCE(lot_no, '') AS lot_no,
           SUM(no_of_cartons) AS no_of_cartons,
           MIN(weight_kg) AS weight_kg,
           SUM(COALESCE(no_of_cartons, 0) * COALESCE(weight_kg, 0)) AS total_inventory_kgs,
           MAX(group_name) AS group_name,
           COALESCE(storage_location, '') AS storage_location,
           MAX(exporter) AS exporter,
           MIN(last_purchase_rate) AS last_purchase_rate,
           SUM(value) AS value,
           MIN(box_id) AS box_id,
           MIN(transaction_no) AS transaction_no
    FROM {table}
    WHERE {where_sql}
    GROUP BY item_description, COALESCE(lot_no, ''), COALESCE(inward_no, ''),
             COALESCE(item_mark, ''), COALESCE(storage_location, ''), COALESCE(unit, '')
    ORDER BY MIN(inward_dt) ASC, MIN(id) ASC
    LIMIT {limit_ph}
"""


# The boxes of the given lots (search groups), for available_here: the rows the search
# counted ({where_sql}) whose group key is one of the unnest()ed lots.
_LOT_BOXES_SQL = """
    SELECT COALESCE(item_description, '') AS item_description, COALESCE(lot_no, '') AS lot_no,
           COALESCE(inward_no, '') AS inward_no, COALESCE(item_mark, '') AS item_mark,
           COALESCE(storage_location, '') AS storage_location, COALESCE(unit, '') AS unit,
           box_id, transaction_no
    FROM {table}
    WHERE {where_sql}
      AND (COALESCE(item_description, ''), COALESCE(lot_no, ''), COALESCE(inward_no, ''),
           COALESCE(item_mark, ''), COALESCE(storage_location, ''), COALESCE(unit, ''))
          IN (SELECT * FROM unnest({lots}))
"""


def _lot_key(r) -> tuple:
    """A search group's key, as _SEARCH_SQL groups (a NULL with '')."""
    return tuple(r[c] or "" for c in
                 ("item_description", "lot_no", "inward_no", "item_mark", "storage_location", "unit"))


def _fifo(found) -> tuple:
    """Merge order of (company, group) pairs: oldest inward first (none last, as Postgres
    orders NULLs in ASC), then cfpl before cdpl, then the group's first row id -- the
    order each table's query returns its groups in."""
    company, r = found
    dt = r["inward_dt"]
    return (dt is None, dt.isoformat() if isinstance(dt, (date, datetime)) else str(dt or ""),
            company != "cfpl", r["id"])


async def _available_here(conn, found, where_sql: str, args: list, from_warehouse: str,
                          transfer_id=None) -> dict:
    """{(company, lot key): how many of that lot's boxes R3 lets go from
    `from_warehouse`, leaving `transfer_id` (the transfer being edited) out of each box's
    history, as the pick does} for the `found` (company, group) pairs. A row without a
    box identity is not counted (the pick never returns one). Read only: one query per
    company with lots found, and one R3 judgement for all their boxes."""
    boxes: dict[tuple, set] = {}
    for table in _COLD_TABLES:
        lots = [_lot_key(r) for company, r in found if company == _company(table)]
        if not lots:
            continue
        cols = [list(col) for col in zip(*lots)]
        n = len(args)
        sql = _LOT_BOXES_SQL.format(
            table=table, where_sql=where_sql,
            lots=", ".join(f"${n + i}::text[]" for i in range(1, len(cols) + 1)))
        for r in await conn.fetch(sql, *args, *cols):
            key = _box_identity(r["box_id"], r["transaction_no"])
            if key:
                boxes.setdefault((_company(table), _lot_key(r)), set()).add(key)
    held = await _unavailable(conn, sorted({k for keys in boxes.values() for k in keys}), from_warehouse,
                              transfer_id)
    return {lot: len(keys - held) for lot, keys in boxes.items()}


async def search_cold_storage_stocks(
    conn, *, lot_no=None, item_description=None, group_name=None, inward_dt=None,
    unit=None, storage_location=None, q=None, limit=50, from_warehouse=None, transfer_id=None,
) -> dict:
    """Search both companies' cold tables (cfpl_cold_stocks and cdpl_cold_stocks) and
    merge their lots in FIFO order, oldest inward first, the limit applied after the
    merge. Each result carries its REAL source company, so the box pick reads the
    correct cold table.

    `unit` and `storage_location` compare sites, not strings (app.core.sites). `unit`
    is the unit column's site: 'Savla D-39' finds rows of unit 'D-39'. `storage_location`
    is the store a row is at, as every reader of a cold row takes it
    (sites.cold_row_site): its unit when that names a cold store, else its
    storage_location -- so 'Savla D-39' finds rows of unit 'D-39' whatever their
    location says (bare 'Savla', a chamber, 'Savla Bond'), and 'Rishi' finds a row of
    no store's unit stored at 'Rishi Cold Storage'. 'Savla Bond' and 'Cold Storage' are
    sites of their own, never spellings of a store's name.

    The counts are the cold table's own (IMS keeps it; a transfer never deducts from it),
    so boxes already on a transfer are counted too. With `from_warehouse`, the From site
    of the dispatch being made, each lot also carries available_here: how many of its
    boxes R3 lets go from that site, the most pick_boxes can return for it, leaving out
    `transfer_id` (the transfer being edited, as the pick leaves it out: its own boxes
    may be picked again). Without one available_here is None."""
    clauses: list = []
    args: list = []

    def add(clause_tmpl: str, value):
        args.append(value)
        clauses.append(clause_tmpl.format(n=len(args)))

    if lot_no:
        add("CAST(lot_no AS TEXT) ILIKE ${n}", f"%{lot_no}%")
    if item_description:
        add("item_description ILIKE ${n}", f"%{item_description}%")
    if group_name:
        add("group_name ILIKE ${n}", f"%{group_name}%")
    if inward_dt:
        add("CAST(inward_dt AS TEXT) ILIKE ${n}", f"%{inward_dt}%")
    if unit:
        args.append(scope_keys(unit))
        clauses.append(WAREHOUSE_NORM_ANY_SQL("unit", f"${len(args)}"))
    if storage_location:
        # The row's store (sites.cold_row_site): its unit when that names a cold store,
        # else its storage_location. A site that is no cold store is only ever a location.
        args.append(scope_keys(storage_location))
        site = f"${len(args)}"
        args.append(_COLD_KEYS)
        by_location = (f"(NOT ({WAREHOUSE_NORM_ANY_SQL('unit', f'${len(args)}')}) "
                       f"AND {WAREHOUSE_NORM_ANY_SQL('storage_location', site)})")
        clauses.append(f"({WAREHOUSE_NORM_ANY_SQL('unit', site)} OR {by_location})"
                       if cold_company(storage_location) else by_location)
    if q:
        args.append(f"%{q}%")
        clauses.append(
            f"(item_description ILIKE ${len(args)} OR group_name ILIKE ${len(args)} "
            f"OR CAST(lot_no AS TEXT) ILIKE ${len(args)})")

    where_sql = " AND ".join(clauses) if clauses else "TRUE"
    limit_ph = f"${len(args) + 1}"

    found: list = []
    for tbl in _COLD_TABLES:
        if not await _table_exists(conn, tbl):
            continue
        rows = await conn.fetch(_SEARCH_SQL.format(table=tbl, where_sql=where_sql, limit_ph=limit_ph),
                                *args, int(limit))
        found += [(_company(tbl), r) for r in rows]
    found = sorted(found, key=_fifo)[:int(limit)]

    here = (from_warehouse or "").strip()
    available = (await _available_here(conn, found, where_sql, args, here, transfer_id)
                 if here and found else {})

    results = [{
        "id": r["id"],
        "inward_dt": str(r["inward_dt"]) if r["inward_dt"] else None,
        "unit": r["unit"],
        "inward_no": r["inward_no"],
        "item_description": r["item_description"],
        "item_mark": r["item_mark"],
        "vakkal": r["vakkal"],
        "lot_no": r["lot_no"],
        "net_qty_on_cartons": _f(r["no_of_cartons"]),
        "weight_kg": _f(r["weight_kg"]),
        "total_inventory_kgs": _f(r["total_inventory_kgs"]),
        "group_name": r["group_name"],
        "storage_location": r["storage_location"],
        "stock": None,
        "exporter": r["exporter"],
        "last_purchase_rate": _f(r["last_purchase_rate"]),
        "value": _f(r["value"]),
        "box_id": r["box_id"],
        "transaction_no": r["transaction_no"],
        "company": company,
        "available_here": available.get((company, _lot_key(r)), 0) if here else None,
    } for company, r in found]

    return {"results": results, "total": len(results)}


# One page of a lot's candidate rows, oldest first; {store} keeps to the picked search
# row's unit and storage_location, {mark} to its item_mark, {after} continues after the
# last row id read.
_PICK_SQL = """
    SELECT id, box_id, transaction_no, weight_kg, item_mark, inward_dt, unit, inward_no,
           item_description, vakkal, lot_no, no_of_cartons, total_inventory_kgs,
           group_name, storage_location, exporter, last_purchase_rate, value
    FROM {table} c
    WHERE item_description = $1 AND CAST(lot_no AS TEXT) = $2 AND COALESCE(inward_no, '') = $3{store}{mark}{after}
    ORDER BY id ASC
    LIMIT {limit_ph}
"""


async def pick_boxes(conn, *, company: str, item_description: str, lot_no: str,
                     inward_no: str, qty: int, from_warehouse: Optional[str] = None,
                     transfer_id: Optional[int] = None, exclude: Optional[str] = None,
                     unit: Optional[str] = None, storage_location: Optional[str] = None,
                     item_mark: Optional[str] = None) -> dict:
    """Return up to `qty` individual box rows in FIFO order (id ASC) for item+lot+inward_no,
    each one box (box_id + transaction_no) that may go on the dispatch being made, so the
    dispatch never collapses many boxes to one id and never gets a box it would refuse.

    Each box is judged by R3 (lookup_service.unavailable_boxes) from `from_warehouse`, the
    dispatch's From site, leaving out `transfer_id`, the transfer being edited (a box
    taken off it may be picked again). Without a From site any transfer in a box's
    history (but that one) refuses it, as strictly as before. Rows without a box
    identity, and the boxes in `exclude` ("box_id|transaction_no", comma-separated: the
    ones already on the list), are skipped. `unit` and `storage_location` are the picked
    search row's own values: given either (blank included), the pick keeps to rows with
    exactly that unit and that storage_location (blank, or one left out, matching blank
    or NULL, as the search groups them), never another store's rows of the same lot.
    `item_mark`, given (blank included), keeps it to the search row's mark the same way,
    as the search groups by it and counts available_here per mark.

    The candidates are read a page at a time until `qty` boxes pass, so boxes already on
    a transfer never hide free ones further down the lot. Read only. A transfer copies its
    boxes and leaves their cold rows (record only), which is why boxes on a transfer are
    still here to be judged."""
    table = _get_cold_table(company)
    if not await _table_exists(conn, table):
        raise HTTPException(404, f"Cold table {table} not available")
    qty = int(qty)
    args: list = [item_description, lot_no, inward_no]
    store = mark = ""
    if unit is not None or storage_location is not None:
        args += [unit or "", storage_location or ""]
        store = f" AND COALESCE(unit, '') = ${len(args) - 1} AND COALESCE(storage_location, '') = ${len(args)}"
    if item_mark is not None:
        args.append(item_mark)
        mark = f" AND COALESCE(item_mark, '') = ${len(args)}"
    excluded = _excluded(exclude)

    picked: list = []   # (key, row) of each box taken, in FIFO order
    seen: set = set()
    last_id = None
    while len(picked) < qty:
        page_args = list(args)
        after = ""
        if last_id is not None:
            page_args.append(last_id)
            after = f" AND id > ${len(page_args)}"
        page_args.append(_PICK_PAGE)
        page = await conn.fetch(
            _PICK_SQL.format(table=table, store=store, mark=mark, after=after, limit_ph=f"${len(page_args)}"),
            *page_args)
        if not page:
            break
        last_id = page[-1]["id"]
        fresh = []
        for r in page:
            key = _box_identity(r["box_id"], r["transaction_no"])
            if key is None or key in excluded or key in seen:
                continue
            seen.add(key)
            fresh.append((key, r))
        held = await _unavailable(conn, [key for key, _ in fresh], from_warehouse, transfer_id)
        picked += [(key, r) for key, r in fresh if key not in held][:qty - len(picked)]
        if len(page) < _PICK_PAGE:
            break
    # box_id / transaction_no come back trimmed, as the transfer stores and keys them.
    return {"boxes": [{
        "id": r["id"],
        "box_id": key[0],
        "transaction_no": key[1],
        "weight_kg": _f(r["weight_kg"]) or 0,
        "item_mark": r["item_mark"] or "",
        "inward_dt": str(r["inward_dt"]) if r["inward_dt"] else "",
        "unit": r["unit"] or "",
        "inward_no": r["inward_no"] or "",
        "item_description": r["item_description"] or "",
        "vakkal": r["vakkal"] or "",
        "lot_no": str(r["lot_no"]) if r["lot_no"] else "",
        "no_of_cartons": int(r["no_of_cartons"]) if r["no_of_cartons"] else 0,
        "total_inventory_kgs": _f(r["total_inventory_kgs"]) or 0,
        "group_name": r["group_name"] or "",
        "storage_location": r["storage_location"] or "",
        "exporter": r["exporter"] or "",
        "last_purchase_rate": _f(r["last_purchase_rate"]) or 0,
        "value": _f(r["value"]) or 0,
    } for key, r in picked]}
