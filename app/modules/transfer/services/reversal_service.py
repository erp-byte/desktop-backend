"""In-transit bookkeeping + backfill (P3b), ported from pending_stock_tools.py:
  - park_boxes / park_lines : dispatch → one 'In Transit' pending_transfer_stock row per
                              box (a copy of its stock row) / per unit of a box-less line
                              (a transfer without a request only)
  - restore_to_source  : cancel / edit a transfer → delete its pending rows
  - in_transit_rows    : edit a transfer → its pending rows, so a kept box re-parks from its own copy
  - unpick_to_pending  : re-open / edit / delete a receipt → re-park the transfer's boxes In
                         Transit (not one a newer transfer has taken on since)
  - backfill           : park the boxes of existing transfers into pending (not one a newer
                         transfer has taken on since)

Sites are classified by the site table (app.core.sites): cold or not by kind
('Cold Storage' included), and the cold-stock company by the cold store.

RECORD ONLY. The stock tables ({cfpl,cdpl}_boxes_v2, _bulk_entry_boxes, _cold_stocks)
are kept by IMS, and ERP Material In's boxes (po_box, with po_header / po_line /
po_section) by the purchase module. These functions READ them, to find a box's row and
snapshot it into its pending row (source_table, source_row_id, cold_storage_data), and
never INSERT, UPDATE or DELETE a row there: a dispatch copies a box and leaves its stock
row as it is, so an undo has nothing to put back. Only pending_transfer_stock is written.
Callers (create / update / delete / receive / backfill) wrap them in a transaction.
asyncpg type-strictness handled via stock_service._dec.
"""
from __future__ import annotations

import json
from datetime import datetime

from app.core import sites
from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.transfer.services import lookup_service, material_in_source
from app.modules.transfer.services.stock_service import _dec, _rows_affected, _table_exists

_COLD_TABLES = ("cfpl_cold_stocks", "cdpl_cold_stocks")
_WH_TABLES = ("cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes")


def _is_cold_site(site) -> bool:
    """Whether a site is cold in kind (sites.is_cold_site): every cold store in any
    spelling ('Rishi Cold', 'D-39', 'Eskimo', 'Savla Bond') and the legacy 'Cold
    Storage'. A dispatch TO one is recorded as a cold destination (to_storage_type /
    destination_table), and one FROM it reads the cold stock first."""
    return sites.is_cold_site(site)


def _destination_table(to_storage_type: str, to_company: str) -> str:
    return f"{to_company}_cold_stocks" if to_storage_type == "cold" else f"{to_company}_boxes_v2"


def _cold_row_to_json(row: dict) -> dict:
    def g(name):
        v = row.get(name)
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        try:
            from decimal import Decimal
            if isinstance(v, Decimal):
                return float(v)
        except Exception:
            pass
        return v
    keys = ("inward_dt", "unit", "inward_no", "item_mark", "vakkal", "group_name",
            "item_subgroup", "storage_location", "exporter", "last_purchase_rate",
            "value", "total_inventory_kgs", "spl_remarks")
    return {k: g(k) for k in keys}


async def _cold_tables(conn) -> list[str]:
    """The cold-stock tables that exist, cfpl first."""
    return [t for t in _COLD_TABLES if await _table_exists(conn, t)]


async def _cold_exact(conn, tables, box_id, tno):
    """(table, row) of this box's cold row by box_id + transaction_no, in `tables` order;
    (None, None) without one."""
    for table in tables:
        row = await conn.fetchrow(
            f"SELECT * FROM {table} WHERE box_id = $1 AND transaction_no = $2 LIMIT 1", box_id, tno)
        if row:
            return table, dict(row)
    return None, None


async def _cold_by_box_id(conn, tables, box_id):
    """(table, row) of a cold row with this box_id under any transaction, in `tables`
    order; (None, None) without one."""
    for table in tables:
        row = await conn.fetchrow(f"SELECT * FROM {table} WHERE box_id = $1 LIMIT 1", box_id)
        if row:
            return table, dict(row)
    return None, None


async def _find_in_cold_stocks(conn, box_id, tno, strict: bool = False):
    """Cold row for this box: box_id + transaction_no, else (unless `strict`) box_id alone.
    The exact match is tried in BOTH tables before the box_id-only fallback, so a cfpl row
    that merely shares the box_id (not unique) never shadows the exact cdpl row."""
    tables = await _cold_tables(conn)
    table, row = await _cold_exact(conn, tables, box_id, tno)
    if row is None and not strict:
        table, row = await _cold_by_box_id(conn, tables, box_id)
    return table, row


async def _from_material_in(conn, b: dict, box_id: str, tno: str, *,
                            check_bulk: bool = False, check_cold: bool = False):
    """(source_table, source_row, warehouse_data) of the box's ERP Material In row
    (material_in_source.snapshot), copied as a warehouse box's stock row is: the row's
    gross weight, its net weight else the box's, its article. (None, None, {}) when the
    box has none, and when IMS has a row with its very key (box_id + transaction_no;
    legacy TR- numbers can collide) in a table park_boxes has not searched for it yet:
    boxes_v2 / bulk entry (`check_bulk`) or the cold stocks (`check_cold`), searched only
    once a po_box row is found. The lookups find IMS's row first (lookup_service._find),
    so such a box is IMS's, and it parks as it always did."""
    snap = await material_in_source.snapshot(conn, box_id, tno)
    if snap is None:
        return None, None, {}
    if check_bulk and (await _find_in_bulk_entry(conn, box_id, tno))[1] is not None:
        return None, None, {}
    if check_cold and (await _cold_exact(conn, await _cold_tables(conn), box_id, tno))[1] is not None:
        return None, None, {}
    return material_in_source.SOURCE_TABLE, snap, {
        "gross_weight": float(snap.get("gross_weight") or 0),
        "net_weight": float(snap.get("net_weight") or b.get("net_weight") or 0),
        "article": snap.get("article"),
    }


async def _find_in_bulk_entry(conn, box_id, tno):
    for table in ("cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes"):
        if not await _table_exists(conn, table):
            continue
        row = await conn.fetchrow(
            f"SELECT * FROM {table} WHERE box_id = $1 AND transaction_no = $2 LIMIT 1", box_id, tno)
        if row:
            return table, dict(row)
    return None, None


async def restore_to_source(conn, transfer_out_id: int) -> int:
    """Release this transfer-out's in-transit boxes (cancel / delete / edit): delete every
    pending_transfer_stock row it has, boxes and box-less LINE- rows alike. Returns the
    count removed.

    Record only. Nothing goes back into a stock table — the dispatch copied each box and
    never took it out — so the name is the reference's, not what it does any more. Once
    the transfer itself is gone (delete_transfer) its boxes are free for another one."""
    tag = await conn.execute(
        "DELETE FROM pending_transfer_stock WHERE transfer_out_id = $1", transfer_out_id)
    return _rows_affected(tag)


_PENDING_INSERT = (
    "INSERT INTO pending_transfer_stock "
    "(transfer_type, transfer_out_id, transfer_out_challan_no, box_id, transaction_no, "
    "from_company, to_company, from_site, to_site, from_storage_type, to_storage_type, "
    "source_table, source_row_id, destination_table, item_description, lot_no, weight_kg, "
    "no_of_cartons, cold_storage_data, gross_weight, net_weight, article, status, dispatched_at, dispatched_by, id) "
    "VALUES ('INTERUNIT',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,CAST($18 AS JSONB),"
    "$19,$20,$21,'In Transit',$22,$23,$24) ON CONFLICT (box_id, transaction_no) DO NOTHING"
)


_RECEIVED_VALUES = ("article", "lot_number", "batch_number", "net_weight", "gross_weight")


async def unpick_to_pending(conn, transfer_in_id: int, transfer_out_id: int) -> int:
    """Put a transfer's boxes back in transit when its receipt is undone (re-open / edit
    / delete a receipt): re-create a pending_transfer_stock row (In Transit) for each
    box of the TRANSFER (interunit_transfer_boxes), the way the dispatch parked it —
    park_boxes finds each box's stock row again and snapshots it. Returns the count
    re-parked.

    Every box of the transfer, not only those the receipt acknowledged: the transfer is
    in transit again, so a box written off as short on it is expected again too. The
    receipt's box rows only lend a box they acknowledge (lookup_service._acknowledges)
    their received values (article, lot, batch, weights; one left blank keeps the box's
    own); a receipt row that is no box of this transfer is ignored.

    The stock row is matched on box_id AND transaction_no only (strict_cold, also for a
    cold From site): box_id alone is not unique, and the row may be gone (a transfer
    dispatched before record only had it deleted) or moved by IMS since, so a box_id-only
    match could copy another inward's row. An ERP Material In box re-parks from its po_box
    row (its PO deleted since or not). Without an exact match the box parks from its
    values above.

    Record only. The receipt posted nothing to a stock table, so nothing is taken back
    out of one; the stock rows are only read. A box already parked on this transfer is
    left alone; a stale in-transit row another transfer still has for one of these boxes
    gives way (take_over: this transfer is the box's latest). A row with no box_id /
    transaction_no (or 'DIRECT') is not a box and is skipped.

    Skipped too: a box a newer transfer has (lookup_service.sent_on_since: received here,
    it was sent on from this To site) — it left this site on that transfer, so it is not
    in transit on this one, and a row here would make that transfer's own dispatch (or a
    later one) skip it as already parked; and a box IMS's cold receive took in on this
    transfer (lookup_service.taken_in_by_ims), which is in the To site's cold stock."""
    tout = await conn.fetchrow(
        "SELECT id, challan_no, from_site, to_site, created_by, created_ts FROM interunit_transfers_header "
        "WHERE id = $1",
        transfer_out_id)
    if not tout:
        return 0

    own = [dict(b) for b in await conn.fetch(
        "SELECT * FROM interunit_transfer_boxes WHERE header_id = $1", transfer_out_id)]
    received = [dict(r) for r in await conn.fetch(
        "SELECT * FROM interunit_transfer_in_boxes WHERE header_id = $1", transfer_in_id)]
    gone = await lookup_service.sent_on_since(
        conn, [(b["box_id"], b["transaction_no"]) for b in own], transfer_out_id, tout["created_ts"])
    gone |= await lookup_service.taken_in_by_ims(conn, transfer_out_id)

    boxes = []
    for b in own:
        key = lookup_service._box_key(b.get("box_id"), b.get("transaction_no"))
        if key is None or key in gone:
            continue
        box = {"box_id": key[0], "transaction_no": key[1], **{f: b.get(f) for f in _RECEIVED_VALUES}}
        acked = next((r for r in received
                      if lookup_service._acknowledges(r, b.get("id"), *key)), None)
        if acked is not None:
            box.update({f: acked[f] for f in _RECEIVED_VALUES
                        if acked.get(f) is not None and acked.get(f) != ""})
        boxes.append(box)
    s = await park_boxes(
        conn, transfer_out_id=transfer_out_id, challan_no=tout["challan_no"],
        from_site=tout["from_site"] or "", to_site=tout["to_site"] or "",
        boxes=boxes, dispatched_at=datetime.now(),
        dispatched_by=tout["created_by"] or "system", cold_fallback=True, strict_cold=True,
        take_over=True,
    )
    return s["parked_from_cold"] + s["parked_from_warehouse"] + s["parked_without_source"]


async def in_transit_rows(conn, transfer_out_id: int) -> dict:
    """This transfer-out's pending_transfer_stock rows by (box_id, transaction_no),
    trimmed. update_transfer reads them before restore_to_source drops them and hands
    them to park_boxes as `prior`."""
    rows = await conn.fetch(
        "SELECT * FROM pending_transfer_stock WHERE transfer_out_id = $1", transfer_out_id)
    return {((r["box_id"] or "").strip(), (r["transaction_no"] or "").strip()): dict(r) for r in rows}


def _own_snapshot(p: dict):
    """(source_table, source_row, cold_data, warehouse_data) as park_boxes found them for
    a box, rebuilt from the pending row it parked then: a Material In copy (source_table
    material_in_source.SOURCE_TABLE; po_box has no integer id, so no source_row_id)
    carries its company, weights and article; a cold copy its cold item / lot / weight /
    cartons, a warehouse copy its weights and article. A row parked without a source
    gives nothing, so the box is looked up again."""
    if p.get("source_table") == material_in_source.SOURCE_TABLE:
        return material_in_source.SOURCE_TABLE, {"id": None, "_company": p.get("from_company")}, None, {
            "gross_weight": float(p.get("gross_weight") or 0),
            "net_weight": float(p.get("net_weight") or 0),
            "article": p.get("article"),
        }
    cold = p.get("cold_storage_data")
    if isinstance(cold, str):
        cold = json.loads(cold)
    if cold is not None:
        row = {"id": p.get("source_row_id"), "item_description": p.get("item_description"),
               "lot_no": p.get("lot_no"), "weight_kg": p.get("weight_kg"),
               "no_of_cartons": p.get("no_of_cartons")}
        return p.get("source_table"), row, cold, {}
    if p.get("source_row_id") is not None:
        return p.get("source_table"), {"id": p["source_row_id"]}, None, {
            "gross_weight": float(p.get("gross_weight") or 0),
            "net_weight": float(p.get("net_weight") or 0),
            "article": p.get("article"),
        }
    return None, None, None, {}


async def park_boxes(conn, *, transfer_out_id: int, challan_no: str, from_site, to_site,
                     boxes: list, dispatched_at, dispatched_by: str, cold_fallback: bool = False,
                     strict_cold: bool = False, prior: dict | None = None,
                     take_over: bool = False) -> dict:
    """Park each dispatched box into pending_transfer_stock (status 'In Transit') as a
    copy of its stock row: the row is found (cold_stocks, boxes_v2, bulk_entry_boxes or
    ERP Material In's po_box) and snapshotted — source_table, source_row_id,
    cold_storage_data, item / lot / weight — and is left as it is. Record only: the
    stock tables are IMS's (and po_box the purchase module's) and a dispatch never
    deducts from them, so it is the box guard (a box in transit, at another site or
    written off is refused before a dispatch gets here: lookup_service / create_service)
    that keeps a box on one transfer at a time.

    `boxes` are dicts carrying box_id / transaction_no / article / lot_number /
    batch_number / net_weight / gross_weight (the shape of both interunit_transfer_boxes
    rows and the BoxCreate payload). transaction_no == 'DIRECT' or a missing
    box_id/transaction_no are quantity-only entries — skipped here (use park_lines
    to give them a tracking row). Returns per-source counters.

    Where the row is looked for, when the box has no copy of its own (`prior`):
      - a cold From site: its cold row on box_id + transaction_no (either company), then
        its Material In row (material_in_source.snapshot), then a cold row on box_id
        alone (not with `strict_cold`: unpick_to_pending), then boxes_v2 / bulk entry;
      - any other From site: boxes_v2 / bulk entry, then (with `cold_fallback`) a cold
        row on box_id + transaction_no, then its Material In row.
    The Material In row is taken only when no IMS stock table has a row with the box's
    very key (legacy TR- numbers can collide): the lookups find IMS's row first, so such
    a box is IMS's (_from_material_in). So an IMS box is copied as it always was, and an
    ERP box is never copied from an IMS cold row that merely shares its box_id (IMS and
    ERP mint box ids alike). A Material In copy is a warehouse copy (parked_from_warehouse):
    source_table material_in_source.SOURCE_TABLE, no source_row_id (po_box has no integer
    id), no cold snapshot; the row's gross weight, its net weight else the box's, its
    article. `prior` (an edit: in_transit_rows from before it; a weight edit on the scan
    screen: the box's own row) gives a box the transfer keeps its own copy back instead
    of a new look at the stock tables, whose row may have changed or gone since the
    dispatch; route, date and the box's own weights are applied as for any other box.

    A box with an in-transit row already is skipped (skipped_already_parked) when that
    row is this transfer's. With `take_over` (the live callers: create / update, the scan
    screen, a receipt undone — each after the box guard, or sent_on_since, has found this
    transfer to be the box's latest) another transfer's row for the box is stale — a
    legacy row, or one a transfer received or edited since left — and is deleted, and
    the box is parked here. Without it (backfill) the box is skipped, as before.

    The companies come from the site table (sites.cold_company): the source row's table
    names the From company (a Material In copy: its PO's company; a box with no stock
    row: the cold store's company, else cfpl), and a cold store To names its own (a
    warehouse To has none: the From's).

    This is the single-transfer park used by create_transfer / update_transfer and the
    scan screen (real-time dispatch), unpick_to_pending (a receipt undone) and backfill
    (one-off ledger reconstruction), so finding and snapshotting the source row lives in
    exactly one place.
    """
    from_st = "cold" if _is_cold_site(from_site) else "warehouse"
    to_st = "cold" if _is_cold_site(to_site) else "warehouse"
    s = {"parked_from_cold": 0, "parked_from_warehouse": 0, "parked_without_source": 0,
         "skipped_already_parked": 0, "missing_id": 0}

    for brow in boxes:
        b = dict(brow)
        box_id = (b.get("box_id") or "").strip()
        tno = (b.get("transaction_no") or "").strip()
        if not box_id or not tno or tno == "DIRECT":
            s["missing_id"] += 1
            continue
        held = await conn.fetch(
            "SELECT id, transfer_out_id FROM pending_transfer_stock WHERE box_id = $1 AND transaction_no = $2",
            box_id, tno)
        if any(r["transfer_out_id"] == transfer_out_id for r in held) or (held and not take_over):
            s["skipped_already_parked"] += 1
            continue
        if held:
            await conn.execute("DELETE FROM pending_transfer_stock WHERE id = ANY($1::bigint[])",
                               [r["id"] for r in held])

        source_table = source_row = cold_data = None
        warehouse_data: dict = {}
        own = (prior or {}).get((box_id, tno))
        if own is not None:
            source_table, source_row, cold_data, warehouse_data = _own_snapshot(own)
        if source_row is None and from_st == "cold":
            # Its exact cold row, then its Material In row (not when boxes_v2 / bulk entry
            # has its very key: then it is IMS's box), then a cold row by box_id alone.
            cold_tables = await _cold_tables(conn)
            source_table, source_row = await _cold_exact(conn, cold_tables, box_id, tno)
            if source_row is None:
                source_table, source_row, warehouse_data = await _from_material_in(
                    conn, b, box_id, tno, check_bulk=True)
            if source_row is None and not strict_cold:
                source_table, source_row = await _cold_by_box_id(conn, cold_tables, box_id)
            if source_row is not None and source_table != material_in_source.SOURCE_TABLE:
                cold_data = _cold_row_to_json(source_row)
        if source_row is None:
            wh_table, wh_row = await _find_in_bulk_entry(conn, box_id, tno)
            if wh_row is not None:
                source_table, source_row = wh_table, wh_row
                warehouse_data = {
                    "gross_weight": float(wh_row.get("gross_weight") or 0),
                    "net_weight": float(wh_row.get("net_weight") or b.get("net_weight") or 0),
                    "article": wh_row.get("article_description"),
                }
        # The box lookup searches cold stocks last, so a box held only in cold storage can be
        # scanned onto a dispatch whose From is not a cold store. Snapshot it from there too,
        # or it would park with no source and no cold snapshot (unit, location, rate).
        # Exact box + transaction match only, as the lookup matched it. Live dispatch / edit /
        # re-park only (cold_fallback): backfill keeps its old behaviour for historical transfers.
        # from_storage_type stays the From site's type (the in-transit list groups on it, so a
        # per-box value would split one challan into two rows); source_table and
        # cold_storage_data carry the cold origin.
        if source_row is None and cold_fallback and from_st != "cold":
            source_table, source_row = await _find_in_cold_stocks(conn, box_id, tno, strict=True)
            if source_row is not None:
                cold_data = _cold_row_to_json(source_row)
        # ERP Material In last (a cold From site has looked there already, above). Without
        # cold_fallback the cold stocks were not searched: a cold row with the box's very key
        # still makes it IMS's box, which parks without a source, as it did.
        if source_row is None and from_st != "cold":
            source_table, source_row, warehouse_data = await _from_material_in(
                conn, b, box_id, tno, check_cold=not cold_fallback)

        if cold_data is not None and source_row is not None:
            item_description = source_row.get("item_description") or b.get("article") or ""
            lot_no = source_row.get("lot_no") or b.get("lot_number")
            weight_kg = float(source_row.get("weight_kg") or b.get("net_weight") or 0)
            no_of_cartons = int(source_row.get("no_of_cartons") or 1)
        elif warehouse_data:
            item_description = warehouse_data.get("article") or b.get("article") or ""
            lot_no = b.get("lot_number")
            weight_kg = float(warehouse_data.get("net_weight") or b.get("net_weight") or 0)
            no_of_cartons = 1
        else:
            item_description = b.get("article") or ""
            lot_no = b.get("lot_number")
            weight_kg = float(b.get("net_weight") or 0)
            no_of_cartons = 1
            gc = sites.cold_company(from_site) or "cfpl"
            source_table = f"{gc}_cold_stocks" if from_st == "cold" else f"{gc}_bulk_entry_boxes"

        if source_table == material_in_source.SOURCE_TABLE:
            # Its PO's company: the table's name ('po_box') names none.
            from_company = source_row.get("_company") or "cfpl"
        else:
            from_company = "cfpl" if (source_table or "").startswith("cfpl") else "cdpl"
        to_company = sites.cold_company(to_site) or from_company
        destination_table = _destination_table(to_st, to_company)

        async def _ins_pending(b=b, source_row=source_row, source_table=source_table,
                               destination_table=destination_table, cold_data=cold_data,
                               item_description=item_description, lot_no=lot_no,
                               weight_kg=weight_kg, no_of_cartons=no_of_cartons,
                               warehouse_data=warehouse_data, from_company=from_company,
                               to_company=to_company):
            return await conn.execute(
                _PENDING_INSERT,
                transfer_out_id, challan_no, box_id, tno, from_company, to_company,
                from_site, to_site, from_st, to_st, source_table,
                source_row.get("id") if source_row is not None else None, destination_table,
                item_description, lot_no, _dec(weight_kg), no_of_cartons,
                json.dumps(cold_data) if cold_data else None,
                _dec(warehouse_data.get("gross_weight") if warehouse_data else b.get("gross_weight")),
                _dec(warehouse_data.get("net_weight") if warehouse_data else b.get("net_weight")),
                warehouse_data.get("article") or b.get("article"),
                dispatched_at, dispatched_by, new_short_time_id(),
            )
        # The stock row stays where it is (record only): nothing is deleted from source_table.
        await insert_with_pk_retry(conn, _ins_pending)

        if cold_data is not None:
            s["parked_from_cold"] += 1
        elif warehouse_data:
            s["parked_from_warehouse"] += 1
        else:
            s["parked_without_source"] += 1

    return s


async def park_lines(conn, *, transfer_out_id: int, challan_no: str, from_site, to_site,
                     lines: list, dispatched_at, dispatched_by: str) -> int:
    """Insert one tracking-only 'In Transit' pending row PER UNIT for box-less lines
    (manual/article-only entries with no scanned box) of a transfer without a request
    (create_service._settle decides which lines; a transfer from a request has none,
    only its scanned boxes are in transit). There is no stock row behind
    them: source_table/destination_table are empty sentinels, and restore_to_source /
    pick_from_pending delete them like any other pending row — they only keep the
    in-transit ledger complete. box_id is the synthetic "LINE-<line_id>-<n>" so the
    (box_id, transaction_no) uniqueness constraint is satisfied.

    `lines` are dicts: id, item_desc_raw, qty, net_weight, total_weight, lot_number,
    batch_number. Mirrors the reference park_lines_in_pending but writes only the
    columns list_pending_transfers reads (item/lot/weights) — not the extra
    classification columns — to stay within the proven _PENDING_INSERT shape.
    """
    parked = 0
    for lrow in lines:
        l = dict(lrow)
        article = (l.get("item_desc_raw") or "").strip()
        qty = int(l.get("qty") or 0)
        if not article or qty <= 0:
            continue
        line_id = l.get("id")
        total_net = float(l.get("net_weight") or 0)
        total_gross = float(l.get("total_weight") or 0) or total_net
        per_unit_net = round(total_net / qty, 3) if qty else total_net
        per_unit_gross = round(total_gross / qty, 3) if qty else total_gross
        lot_no = (l.get("lot_number") or "") or None
        for n in range(1, qty + 1):
            async def _ins_pending(n=n, line_id=line_id, article=article, lot_no=lot_no,
                                   per_unit_net=per_unit_net, per_unit_gross=per_unit_gross):
                return await conn.execute(
                    _PENDING_INSERT,
                    transfer_out_id, challan_no, f"LINE-{line_id}-{n}", challan_no, "", "",
                    from_site, to_site,
                    "cold" if _is_cold_site(from_site) else "warehouse",
                    "cold" if _is_cold_site(to_site) else "warehouse",
                    "", None, "",
                    article, lot_no, _dec(per_unit_net), 1,
                    None,
                    _dec(per_unit_gross), _dec(per_unit_net), article,
                    dispatched_at, dispatched_by, new_short_time_id(),
                )
            await insert_with_pk_retry(conn, _ins_pending)
            parked += 1
    return parked


async def backfill_pending_from_existing_transfers(conn) -> dict:
    """Park the boxes of existing in-transit transfers that have no pending rows yet (a
    one-off reconstruction of the in-transit ledger). Record only, like a dispatch: each
    box's stock row is read and copied, never deducted.

    The candidates are the transfers in transit by status (Dispatch / Partial, and IMS's
    older In Transit / Completed, which IMS parks as in transit too) with no Received
    receipt in either ledger (a GRN, or IMS's cold receipt). Of a candidate's boxes it
    parks only those still in transit on it: not one a newer transfer has
    (lookup_service.sent_on_since: the box went on, and a row here would make that
    transfer's dispatch skip it as already parked; boxes_skipped_sent_on), nor one IMS's
    cold receive took in on it (lookup_service.taken_in_by_ims; boxes_skipped_received)."""
    if not await _table_exists(conn, "pending_transfer_stock"):
        return {"error": "pending_transfer_stock table missing"}

    received_gates = ["""NOT EXISTS (SELECT 1 FROM interunit_transfer_in_header ti
                          WHERE ti.transfer_out_id = h.id AND LOWER(COALESCE(ti.status,'')) = 'received')"""]
    if await _table_exists(conn, "cold_transfer_in_headers"):
        received_gates.append("""NOT EXISTS (SELECT 1 FROM cold_transfer_in_headers ch
                          WHERE ch.transfer_out_id = h.id AND LOWER(COALESCE(ch.status,'')) = 'received')""")
    candidates = await conn.fetch(
        f"""
        SELECT h.id, h.challan_no, h.from_site, h.to_site, h.status, h.created_by, h.created_ts
        FROM interunit_transfers_header h
        WHERE LOWER(COALESCE(h.status,'')) IN ('dispatch','partial','completed','in transit')
          AND {' AND '.join(received_gates)}
        ORDER BY h.created_ts ASC
        """)
    s = {"transfers_scanned": len(candidates), "transfers_with_existing_pending": 0,
         "boxes_parked_from_cold": 0, "boxes_parked_from_warehouse": 0,
         "boxes_parked_without_source": 0, "boxes_skipped_already_parked": 0, "boxes_with_missing_id": 0,
         "boxes_skipped_sent_on": 0, "boxes_skipped_received": 0}

    async with conn.transaction():
        for trow in candidates:
            t = dict(trow)
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM pending_transfer_stock WHERE transfer_out_id = $1", t["id"])
            if existing:
                s["transfers_with_existing_pending"] += 1
                continue
            boxes = [dict(b) for b in await conn.fetch(
                """SELECT box_id, transaction_no, article, lot_number, batch_number, net_weight, gross_weight
                   FROM interunit_transfer_boxes WHERE header_id = $1""", t["id"])]
            gone = await lookup_service.sent_on_since(
                conn, [(b["box_id"], b["transaction_no"]) for b in boxes], t["id"], t["created_ts"])
            taken = await lookup_service.taken_in_by_ims(conn, t["id"])
            keep = []
            for b in boxes:
                key = lookup_service._box_key(b["box_id"], b["transaction_no"])
                if key in gone:
                    s["boxes_skipped_sent_on"] += 1
                elif key in taken:
                    s["boxes_skipped_received"] += 1
                else:
                    keep.append(b)
            stats = await park_boxes(
                conn, transfer_out_id=t["id"], challan_no=t["challan_no"],
                from_site=t["from_site"], to_site=t["to_site"],
                boxes=keep,
                dispatched_at=t["created_ts"] or datetime.now(),
                dispatched_by=t["created_by"] or "backfill",
            )
            s["boxes_parked_from_cold"] += stats["parked_from_cold"]
            s["boxes_parked_from_warehouse"] += stats["parked_from_warehouse"]
            s["boxes_parked_without_source"] += stats["parked_without_source"]
            s["boxes_skipped_already_parked"] += stats["skipped_already_parked"]
            s["boxes_with_missing_id"] += stats["missing_id"]

    return s
