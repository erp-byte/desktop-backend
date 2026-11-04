"""Inner-cold transfer list + delete + create + get (doc 11 form + dashboard tab).

Ported from the reference cold_storage_server.py inner_cold_transfer endpoints. Records
are grouped by challan_no, each carrying its relabel lines.

create_inner_transfer is the one transfer path that edits stock rows, by design: it
relabels the lot of cold-stock rows (cfpl_cold_stocks / cdpl_cold_stocks) in place, and
may correct their location, inside ONE cold store, the challan's From:
  - the From must be a cold store the user may work in (permissions.
    assert_can_work_in_cold_store); each line's picked row must be at it (its unit, else
    its storage_location, names it: _row_store), and a new location must name it too:
    moving boxes to another cold store is an inter-unit transfer (409 CROSS_STORE);
  - the rows a line moves: the picked stock row first, then the other rows of the group
    the cold search showed it in (same table, item, lot, inward, item_mark, location and
    unit), in id order, never rows of another store; a row whose box the box rule
    (lookup_service.unavailable_boxes, R3) does not place at this store (in transit,
    received elsewhere, written off) is left alone;
  - a row with a box id moves whole and is never split: a quantity that would split one
    is refused. A row without one may split, its new row taking that row's own fields;
    weight_kg is per carton, so both keep it, and total_inventory_kgs and value are
    shared by cartons;
  - the From and a new location are written by their canonical site names, and every
    row changed or inserted is recorded with its values before and after
    (inner_cold_transfer_changes, migration 124).
Every line is judged and applied in one transaction with the rows locked: a line that
cannot be done rolls the whole batch back ({status: 'error'}, nothing written). The cold
tables are externally managed (absent in dev).

delete_inner_cold undoes a challan in one transaction: every row it changed gets its old
lot / location / cartons / weight / value back, every row it inserted is deleted, then
its records go. It is refused (409) when one of those rows has been changed since, when
one of their boxes has gone on an inter-unit transfer since (R3, as for the create), or
when the challan was saved before its changes were recorded.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException

from app.core.sites import (
    canonical_site,
    cold_company,
    cold_row_site,
    cold_sites,
    same_site,
    scope_keys,
    site_kind,
)
from app.core.warehouse_scope import WAREHOUSE_NORM_ANY_SQL
from app.modules.auth.middleware import AuthUser
from app.modules.transfer import permissions
from app.modules.transfer.services import create_service, lookup_service
from app.modules.transfer.services.stock_service import _dec

_COLD_TABLES = ("cfpl_cold_stocks", "cdpl_cold_stocks")
# The cold-stock columns a change record keeps: old_<column> before, new_<column> after.
_TRACKED = ("lot_no", "storage_location", "no_of_cartons", "weight_kg", "total_inventory_kgs", "value")
_RETURNING = "RETURNING " + ", ".join(_TRACKED)
# A split's new row copies these from its row too, where the table has them (IMS's
# cfpl_cold_stocks does); the canonical_* columns are IMS's trigger's to fill.
_SPLIT_COPIES = ("cold_item_mark", "spl_remarks", "inward_transaction_no", "auto_created_from_inward")
CROSS_STORE = "Moving boxes to another cold store has to be an inter-unit transfer."
# The rows of the group the cold search shows a row in (search_service._SEARCH_SQL
# groups by these), each locked; the arguments are _group_key's.
_GROUP = """
    SELECT * FROM {table}
    WHERE COALESCE(item_description, '') = $1 AND COALESCE(lot_no, '') = $2
      AND COALESCE(inward_no, '') = $3 AND COALESCE(item_mark, '') = $4
      AND COALESCE(storage_location, '') = $5 AND COALESCE(unit, '') = $6
    ORDER BY id FOR UPDATE
"""


class _Refused(Exception):
    """A line of the batch cannot be done: the transaction rolls back and the errors go
    back as {status: 'error'}."""


async def _table_exists(conn) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass('public.inner_cold_transfer')"))


async def _regclass(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"))


def _num(v) -> Decimal:
    return _dec(v) or Decimal(0)


def _n(v) -> str:
    """A carton count as a message shows it: 3, 2.5."""
    return f"{float(v):g}"


def _has_box(row: dict) -> bool:
    return bool((row.get("box_id") or "").strip())


def _row_store(row: dict) -> str:
    """The cold store a cold-stock row is at (sites.cold_row_site): its unit when that
    names a cold store, else its storage_location (the order IMS' canonical_warehouse_fn
    reads them, and the cold search's location filter); '' when neither does (a bare
    'Savla', say)."""
    return cold_row_site(row.get("unit"), row.get("storage_location"))


def _group_key(row: dict) -> tuple:
    return tuple(str(row.get(c) or "") for c in
                 ("item_description", "lot_no", "inward_no", "item_mark", "storage_location", "unit"))


def _tracked(values: dict | None) -> list:
    """The tracked columns of a row as inner_cold_transfer_changes stores them (two text
    columns, then numbers); all None for none."""
    values = values or {}
    texts = [None if values.get(c) is None else str(values[c]) for c in _TRACKED[:2]]
    return [*texts, *(_dec(values.get(c)) for c in _TRACKED[2:])]


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    return Decimal(str(a)) == Decimal(str(b))


async def list_inner_cold(conn, *, page, per_page, scope=None) -> dict:
    if not await _table_exists(conn):
        return {"records": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

    # Warehouse scope (empty/None = unrestricted) filters by from_warehouse, matching
    # every spelling of each of the caller's sites (sites.scope_keys).
    where, scope_args = "", []
    if scope:
        scope_args = [scope_keys(scope)]
        where = f"WHERE {WAREHOUSE_NORM_ANY_SQL('from_warehouse', '$1')}"

    total = await conn.fetchval(
        f"SELECT COUNT(DISTINCT challan_no) FROM inner_cold_transfer {where}", *scope_args) or 0
    total_pages = max(1, -(-total // per_page)) if total else 0
    offset = (page - 1) * per_page

    challans = await conn.fetch(
        f"""
        SELECT
            challan_no,
            MIN(transfer_date)   AS transfer_date,
            MIN(from_warehouse)  AS from_warehouse,
            MIN(reason_code)     AS reason_code,
            MIN(remark)          AS remark,
            MIN(status)          AS status,
            COUNT(*)             AS line_count,
            SUM(quantity)        AS total_boxes,
            MIN(created_at)      AS created_at
        FROM inner_cold_transfer
        {where}
        GROUP BY challan_no
        ORDER BY MIN(created_at) DESC
        LIMIT ${len(scope_args) + 1} OFFSET ${len(scope_args) + 2}
        """,
        *scope_args, per_page, offset,
    )

    records = []
    for c in challans:
        cr = dict(c)
        line_rows = await conn.fetch(
            """
            SELECT item_description, item_category, quantity,
                   old_lot_number, new_lot_number, net_weight_kg,
                   new_storage_location
            FROM inner_cold_transfer
            WHERE challan_no = $1
            ORDER BY id
            """,
            cr["challan_no"],
        )
        records.append({
            "challan_no": cr["challan_no"],
            "transfer_date": cr["transfer_date"],
            "from_warehouse": cr["from_warehouse"],
            "reason_code": cr["reason_code"],
            "remark": cr["remark"],
            "status": cr["status"] or "COMPLETED",
            "line_count": cr["line_count"],
            "total_boxes": int(cr["total_boxes"]) if cr["total_boxes"] is not None else None,
            "created_at": str(cr["created_at"]) if cr["created_at"] else None,
            "lines": [
                {
                    "item_description": ln["item_description"],
                    "item_category": ln["item_category"],
                    "quantity": ln["quantity"],
                    "old_lot_number": ln["old_lot_number"],
                    "new_lot_number": ln["new_lot_number"],
                    "net_weight_kg": float(ln["net_weight_kg"]) if ln["net_weight_kg"] else 0,
                    "new_storage_location": ln["new_storage_location"],
                }
                for ln in line_rows
            ],
        })

    return {
        "records": records,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


async def get_inner_transfer(conn, challan_no: str, *, scope=None) -> dict:
    """Header (MIN-aggregated over the challan group) + lines, for the edit-mode prefill.
    net_weight_kg is the TOTAL transferred weight; the frontend divides by quantity for
    the per-box weight. With `scope` (permissions.scope_warehouses), a challan made in a
    store that is none of the reader's sites is 404, as list_inner_cold leaves it out."""
    if not await _table_exists(conn):
        raise HTTPException(404, "Inner cold transfer table not found")
    header = await conn.fetchrow(
        """
        SELECT challan_no, MIN(transfer_date) AS transfer_date, MIN(from_warehouse) AS from_warehouse,
               MIN(reason_code) AS reason_code, MIN(remark) AS remark, MIN(status) AS status,
               MIN(created_at) AS created_at
        FROM inner_cold_transfer WHERE challan_no = $1 GROUP BY challan_no
        """,
        challan_no,
    )
    if not header:
        raise HTTPException(404, "Inner cold transfer not found")
    lines = await conn.fetch(
        """
        SELECT id, stock_record_id, item_category, item_description, net_weight_kg, quantity,
               old_lot_number, new_lot_number, new_storage_location, from_warehouse
        FROM inner_cold_transfer WHERE challan_no = $1 ORDER BY id
        """,
        challan_no,
    )
    if not permissions.in_scope(scope, header["from_warehouse"], *(ln["from_warehouse"] for ln in lines)):
        raise HTTPException(404, "Inner cold transfer not found")
    return {
        "challan_no": header["challan_no"],
        "transfer_date": header["transfer_date"],
        "from_warehouse": header["from_warehouse"],
        "reason_code": header["reason_code"],
        "remark": header["remark"],
        "status": header["status"] or "COMPLETED",
        "created_at": str(header["created_at"]) if header["created_at"] else None,
        "lines": [{
            "id": ln["id"],
            "stock_record_id": ln["stock_record_id"],
            "item_category": ln["item_category"],
            "item_description": ln["item_description"],
            "net_weight_kg": float(ln["net_weight_kg"]) if ln["net_weight_kg"] else 0,
            "quantity": ln["quantity"],
            "old_lot_number": ln["old_lot_number"],
            "new_lot_number": ln["new_lot_number"],
            "new_storage_location": ln["new_storage_location"],
        } for ln in lines],
    }


def _check_location(location, store: str) -> None:
    """A new location must name the challan's store (any spelling): 409 CROSS_STORE for
    another site, and 409 for a name that is no site at all."""
    location = (location or "").strip()
    if not location or same_site(location, store):
        return
    if site_kind(location) != "unknown":
        raise HTTPException(409, CROSS_STORE)
    raise HTTPException(
        409, f"'{location}' is not a location of {store}; an inner cold transfer keeps its boxes in "
             f"{store}, so pick {store} or no new location.")


async def _check_challan_store(conn, challan_no, store: str) -> None:
    """Lines added to a saved challan (the edit page) must be for its store: 409 otherwise."""
    held = {canonical_site(r["from_warehouse"]) for r in await conn.fetch(
        "SELECT from_warehouse FROM inner_cold_transfer WHERE challan_no = $1", challan_no)} - {""}
    if held and not any(same_site(s, store) for s in held):
        raise HTTPException(
            409, f"Inner cold transfer {challan_no} is for {', '.join(sorted(held))}; lines for {store} "
                 "need an inner cold transfer of their own.")


def _company(value) -> str | None:
    """'cfpl' / 'cdpl' as a payload names a cold table; None for anything else."""
    company = (value or "").strip().lower()
    return company if company in ("cfpl", "cdpl") else None


async def _picked_row(conn, line, store: str, company=None):
    """(table, row) of the stock row a line picked, locked: stock_record_id in the cold
    table of the line's company; without one, in whichever cold table holds that id at
    the challan's store, the payload's `company` table first (the page sends the picked
    rows' table there: ids collide across the two tables), then the store's own. None
    when no table has it."""
    if _company(line.company):
        tables: tuple = (f"{_company(line.company)}_cold_stocks",)
    else:
        first = [f"{_company(company)}_cold_stocks"] if _company(company) else []
        tables = tuple(dict.fromkeys([*first, f"{cold_company(store)}_cold_stocks", *_COLD_TABLES]))
    found = []
    for table in tables:
        if await _regclass(conn, table):
            row = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = $1 FOR UPDATE", line.stock_record_id)
            if row:
                found.append((table, dict(row)))
    return next((f for f in found if same_site(_row_store(f[1]), store)), found[0] if found else None)


async def _picked_rows(conn, lines, store: str, errors: list, company=None) -> list:
    """Each line's picked row as it is before the batch changes anything: (n, line, table,
    row) per line that can be done, an error per line that cannot; 409 for a picked row
    at another store (or at none the table knows). `company` is the payload's (_picked_row)."""
    picks = []
    for n, line in enumerate(lines, 1):
        if not line.stock_record_id:
            errors.append(f"Line {n}: stock_record_id is required"); continue
        if int(line.quantity) <= 0:
            errors.append(f"Line {n}: quantity must be greater than 0"); continue
        if not (line.new_lot_number or "").strip():
            errors.append(f"Line {n}: the new lot number is required"); continue
        found = await _picked_row(conn, line, store, company)
        if found is None:
            errors.append(f"Line {n}: record {line.stock_record_id} not found in any cold table"); continue
        table, row = found
        at = _row_store(row)
        if not same_site(at, store):
            where = at or (f"no cold store the ERP knows (unit '{row.get('unit') or ''}', "
                           f"location '{row.get('storage_location') or ''}')")
            raise HTTPException(409, f"Line {n}: the stock picked is at {where}, not {store}; an inner cold "
                                     "transfer stays inside one cold store.")
        if (line.old_lot_number or "").strip() != str(row["lot_no"] or "").strip():
            errors.append(f"Line {n}: the stock picked is now lot {row['lot_no'] or '(none)'}, not "
                          f"{line.old_lot_number or '(none)'}; search for it again"); continue
        picks.append((n, line, table, row))
    return picks


def _plan(rows: list, qty: Decimal, n: int):
    """The cartons a line takes from each row, in order: whole rows while they fit, then
    part of the next, which only a row without a box id may give; a box that would have
    to be split is the refusal. ([(row, cartons)], None) or ([], refusal)."""
    moves, left = [], qty
    for row in rows:
        if left <= 0:
            break
        cartons = _num(row["no_of_cartons"])
        if cartons <= 0:
            continue
        if cartons <= left:
            moves.append((row, cartons))
            left -= cartons
        elif _has_box(row):
            taken = qty - left
            asks = " or ".join(_n(q) for q in (taken, taken + cartons) if q > 0)
            return [], (f"Line {n}: box {row['box_id'].strip()} holds {_n(cartons)} cartons and a box moves "
                        f"whole, never split; ask for {asks} instead of {_n(qty)}.")
        else:
            moves.append((row, left))
            left = Decimal(0)
    return moves, None


async def _record(conn, challan_no, line_id, table, row_id, kind, old, new) -> None:
    """One inner_cold_transfer_changes row: the cold-stock row `kind` ('update' / 'insert')
    changed, with its tracked values before (`old`; none for an insert) and after (`new`),
    which delete_inner_cold checks and restores."""
    await conn.execute(
        """INSERT INTO inner_cold_transfer_changes
            (challan_no, line_id, cold_table, row_id, kind,
             old_lot_no, old_storage_location, old_no_of_cartons, old_weight_kg,
             old_total_inventory_kgs, old_value,
             new_lot_no, new_storage_location, new_no_of_cartons, new_weight_kg,
             new_total_inventory_kgs, new_value)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)""",
        challan_no, line_id, table, row_id, kind, *_tracked(old), *_tracked(new))


async def _relabel(conn, table, row, lot, location, challan_no, line_id) -> None:
    """A whole row: the new lot (and location)."""
    if location:
        new = await conn.fetchrow(
            f"UPDATE {table} SET lot_no = $1, storage_location = $2 WHERE id = $3 {_RETURNING}",
            lot, location, row["id"])
    else:
        new = await conn.fetchrow(f"UPDATE {table} SET lot_no = $1 WHERE id = $2 {_RETURNING}", lot, row["id"])
    await _record(conn, challan_no, line_id, table, row["id"], "update", row, dict(new))


async def _split(conn, table, row, take, lot, location, challan_no, line_id) -> None:
    """`take` cartons of a row without a box id: the row keeps the rest, and a new row
    with the row's own fields (those of _SPLIT_COPIES too, where the table has them), the
    new lot (and location) and no box id holds the cartons moved. weight_kg is per
    carton, so both keep it; total_inventory_kgs and value are shared by cartons."""
    cartons = _num(row["no_of_cartons"])
    total, value = _dec(row["total_inventory_kgs"]), _dec(row["value"])
    moved_total = None if total is None else (total * take / cartons).quantize(Decimal("0.001"))
    moved_value = None if value is None else (value * take / cartons).quantize(Decimal("0.01"))
    kept = await conn.fetchrow(
        f"""UPDATE {table} SET no_of_cartons = $1::numeric, total_inventory_kgs = $2::numeric,
                               value = $3::numeric
            WHERE id = $4 {_RETURNING}""",
        cartons - take, None if total is None else total - moved_total,
        None if value is None else value - moved_value, row["id"])
    await _record(conn, challan_no, line_id, table, row["id"], "update", row, dict(kept))
    # The row's own columns the insert names only where the table has them (the row is
    # the table's whole row, SELECT *): cold tables differ across databases.
    copies = [c for c in _SPLIT_COPIES if c in row]
    copy_cols = "".join(f", {c}" for c in copies)
    copy_args = "".join(f", ${17 + i}" for i in range(len(copies)))
    new = dict(await conn.fetchrow(
        f"""INSERT INTO {table}
            (inward_dt, unit, inward_no, item_description, item_mark, vakkal, lot_no,
             no_of_cartons, weight_kg, total_inventory_kgs, group_name, item_subgroup,
             storage_location, exporter, last_purchase_rate, value, box_id, transaction_no{copy_cols})
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::numeric, $9::numeric, $10::numeric, $11, $12,
                    $13, $14, $15::numeric, $16::numeric, NULL, NULL{copy_args})
            RETURNING id, {", ".join(_TRACKED)}""",
        row["inward_dt"], row["unit"], row["inward_no"], row["item_description"], row["item_mark"],
        row["vakkal"], lot, take, _dec(row["weight_kg"]), moved_total, row["group_name"],
        row.get("item_subgroup"), location or row["storage_location"], row["exporter"],
        _dec(row["last_purchase_rate"]), moved_value, *(row[c] for c in copies)))
    await _record(conn, challan_no, line_id, table, new.pop("id"), "insert", None, new)


async def _apply_line(conn, n, line, table, ref, h, store: str, errors: list) -> None:
    """Move one line's cartons (see the module doc) and write its inner_cold_transfer row;
    an error instead when the line cannot be done."""
    rows = sorted((dict(r) for r in await conn.fetch(_GROUP.format(table=table), *_group_key(ref))),
                  key=lambda r: (r["id"] != ref["id"], r["id"]))      # the picked row first
    keys = {r["id"]: lookup_service._box_key(r.get("box_id"), r.get("transaction_no")) for r in rows}
    # The boxes are locked as a dispatch locks them before R3 reads their transfers
    # (create_service.lock_boxes): a dispatch of one of them waits for this to commit,
    # and this waits for one under way, so a box is never relabelled while it leaves.
    await create_service.lock_boxes(conn, keys.values())
    held = {(u["box_id"], u["transaction_no"]) for u in await lookup_service.unavailable_boxes(
        conn, [k for k in keys.values() if k], from_warehouse=store)}
    here = [r for r in rows if keys[r["id"]] not in held]
    qty = Decimal(int(line.quantity))
    available = sum((_num(r["no_of_cartons"]) for r in here), Decimal(0))
    if qty > available:
        away = len(rows) - len(here)
        errors.append(f"Line {n}: transfer qty ({_n(qty)}) exceeds available ({_n(available)}) at {store}"
                      + (f"; {away} of its boxes are not counted, as their inter-unit transfers do not "
                         f"place them at {store}" if away else ""))
        return
    moves, refusal = _plan(here, qty, n)
    if refusal:
        errors.append(refusal)
        return
    lot = line.new_lot_number.strip()
    location = canonical_site(line.new_storage_location) or None
    moved_kg = sum((_num(r["weight_kg"]) * take for r, take in moves), Decimal(0))
    line_id = await conn.fetchval(
        """INSERT INTO inner_cold_transfer
            (challan_no, transfer_date, from_warehouse, reason_code, remark, stock_record_id,
             item_category, item_description, net_weight_kg, quantity, old_lot_number,
             new_lot_number, new_storage_location, old_storage_location, transfer_type)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
           RETURNING id""",
        h.challan_no, h.transfer_name, store, h.reason_code, h.remark, line.stock_record_id,
        line.item_category, line.item_description, moved_kg.quantize(Decimal("0.001")), int(line.quantity),
        line.old_lot_number, lot, location, ref["storage_location"], h.transfer_type or "INNER_COLD")
    for row, take in moves:
        if take == _num(row["no_of_cartons"]):
            await _relabel(conn, table, row, lot, location, h.challan_no, line_id)
        else:
            await _split(conn, table, row, take, lot, location, h.challan_no, line_id)


async def create_inner_transfer(conn, payload, user: AuthUser) -> dict:
    """Relabel lots, and correct locations, of cold-stock rows inside one cold store (see
    the module doc). {status, updated_records, errors, challan_no}: 'error' with a reason
    per line that cannot be done, and then nothing is written. 403 for a store the user
    may not work in; 409 for a line at, or a location naming, another store."""
    if not await _table_exists(conn):
        raise HTTPException(404, "Inner cold transfer table not available")
    if not await _regclass(conn, "inner_cold_transfer_changes"):
        raise HTTPException(
            503, "Inner cold transfers are switched off until the database has been updated "
                 "(migration 124_inner_cold_transfer_changes.sql).")
    h = payload.header
    store = canonical_site(h.from_warehouse)
    if not cold_company(store):
        raise HTTPException(
            400, f"An inner cold transfer is made inside one cold store, picked as its From "
                 f"({', '.join(cold_sites())}); {store or 'no store'} is not one.")
    permissions.assert_can_work_in_cold_store(user, store)
    for line in payload.lines:
        _check_location(line.new_storage_location, store)

    errors: list[str] = []
    try:
        async with conn.transaction():
            await _check_challan_store(conn, h.challan_no, store)
            picks = await _picked_rows(conn, payload.lines, store, errors, payload.company)
            if errors:
                raise _Refused
            for n, line, table, ref in picks:
                await _apply_line(conn, n, line, table, ref, h, store, errors)
            if errors:
                raise _Refused
    except _Refused:
        # Atomic: a line that cannot be done rolls back every line before it.
        return {"status": "error", "updated_records": 0, "errors": errors, "challan_no": h.challan_no}
    return {"status": "success", "updated_records": len(picks), "errors": [], "challan_no": h.challan_no}


async def _changed_since(conn, challan_no: str, changed: list, chains: dict) -> str:
    """The refusal of an undo: how many of the challan's rows have changed since, and the
    later inner cold transfers that changed them (to be deleted first)."""
    later = set()
    for r in await conn.fetch(
            "SELECT id, challan_no, cold_table, row_id FROM inner_cold_transfer_changes "
            "WHERE row_id = ANY($1::bigint[])", sorted({row_id for _, row_id in changed})):
        key = (r["cold_table"], r["row_id"])
        if key in changed and r["challan_no"] != challan_no and r["id"] > chains[key][-1]["id"]:
            later.add(r["challan_no"])
    by = (f" by inner cold transfer {', '.join(sorted(later))}, which has to be deleted first" if later
          else " (or have gone)")
    return (f"Inner cold transfer {challan_no} cannot be undone: {len(changed)} of the stock rows it changed "
            f"have been changed again since{by}. Nothing was changed or deleted.")


async def delete_inner_cold(conn, challan_no: str, user: AuthUser | None = None) -> dict:
    """Delete an inner cold challan and undo it, in one transaction: each cold-stock row it
    changed gets its old lot / location / cartons / weight / value back (its first
    change's old values), each row it inserted is deleted, then its change records and
    lines go. 409, with nothing changed, when a row's values are no longer what the
    challan left (its last change's new values: changed again, or gone), when a box of
    those rows is not at the store any more by R3 (sent on an inter-unit transfer since),
    or when a line has no change record (saved before they were kept). With `user`, the
    challan's store must be one they may work in (403)."""
    if not await _table_exists(conn):
        raise HTTPException(404, "Inner cold transfer table not found")
    async with conn.transaction():
        lines = await conn.fetch(
            "SELECT id, from_warehouse FROM inner_cold_transfer WHERE challan_no = $1 FOR UPDATE", challan_no)
        if not lines:
            raise HTTPException(404, "Inner cold transfer not found")
        if user is not None:
            for store in dict.fromkeys(canonical_site(ln["from_warehouse"]) for ln in lines):
                permissions.assert_can_work_in_cold_store(user, store)
        changes = []
        if await _regclass(conn, "inner_cold_transfer_changes"):
            changes = sorted((dict(c) for c in await conn.fetch(
                "SELECT * FROM inner_cold_transfer_changes WHERE challan_no = $1", challan_no)),
                key=lambda c: c["id"])
        recorded = {c["line_id"] for c in changes}
        if any(ln["id"] not in recorded for ln in lines):
            raise HTTPException(
                409, f"Inner cold transfer {challan_no} cannot be undone: it was saved before the ERP kept a "
                     "record of the stock rows an inner cold transfer changes, so it is not deleted and its "
                     "stock rows stay as they are.")

        chains: dict[tuple, list] = {}            # (cold table, row id) -> its changes, oldest first
        for c in changes:
            if c["cold_table"] not in _COLD_TABLES:
                raise HTTPException(409, f"Inner cold transfer {challan_no} names an unknown stock table.")
            chains.setdefault((c["cold_table"], c["row_id"]), []).append(c)
        current: dict[tuple, dict] = {}
        for table in sorted({t for t, _ in chains}):
            if await _regclass(conn, table):
                for r in await conn.fetch(
                        f"SELECT id, box_id, transaction_no, {', '.join(_TRACKED)} FROM {table} "
                        "WHERE id = ANY($1::bigint[]) FOR UPDATE",
                        sorted(row_id for t, row_id in chains if t == table)):
                    current[(table, r["id"])] = dict(r)
        changed = [key for key, chain in chains.items()
                   if key not in current or not all(_same(current[key][c], chain[-1][f"new_{c}"]) for c in _TRACKED)]
        if changed:
            raise HTTPException(409, await _changed_since(conn, challan_no, changed, chains))
        # As the create, the undo touches only boxes the box rule (R3) still places at the
        # store: one sent on an inter-unit transfer since keeps the lot it was sent with.
        # They are locked first, as the create locks them (create_service.lock_boxes).
        store = canonical_site(lines[0]["from_warehouse"])
        boxes = [k for k in (lookup_service._box_key(r.get("box_id"), r.get("transaction_no"))
                             for r in current.values()) if k]
        await create_service.lock_boxes(conn, boxes)
        gone = await lookup_service.unavailable_boxes(conn, boxes, from_warehouse=store)
        if gone:
            raise HTTPException(
                409, f"Inner cold transfer {challan_no} cannot be undone: {len(gone)} of its boxes have gone on "
                     f"an inter-unit transfer since (box {gone[0]['box_id']}, challan "
                     f"{gone[0].get('challan_no') or '(unknown)'}), so they are not at {store} to be put back. "
                     "Nothing was changed or deleted.")

        restored = removed = 0
        for (table, row_id), chain in chains.items():
            first = chain[0]
            if first["kind"] == "insert":
                await conn.execute(f"DELETE FROM {table} WHERE id = $1", row_id)
                removed += 1
            else:
                await conn.execute(
                    f"""UPDATE {table} SET lot_no = $1, storage_location = $2, no_of_cartons = $3::numeric,
                                           weight_kg = $4::numeric, total_inventory_kgs = $5::numeric,
                                           value = $6::numeric
                        WHERE id = $7""",
                    *(first[f"old_{c}"] for c in _TRACKED), row_id)
                restored += 1
        await conn.execute("DELETE FROM inner_cold_transfer_changes WHERE challan_no = $1", challan_no)
        await conn.execute("DELETE FROM inner_cold_transfer WHERE challan_no = $1", challan_no)
    return {"success": True,
            "message": f"Inner cold transfer {challan_no} deleted and undone: {restored} stock row(s) restored, "
                       f"{removed} split row(s) removed."}
