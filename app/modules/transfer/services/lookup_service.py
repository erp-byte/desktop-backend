"""Box lookups for the transfer-OUT form (doc 07).

Read-only ports of inward_tools.get_box_by_number / get_box_by_box_id and
interunit_tools.get_bulk_entry_box (asyncpg). They back the form's three box
ingestion paths:
  - manual entry (box_number + transaction_no)          → get_box_by_number
  - new "TR-" QR (box_id + transaction_no)               → get_box_by_box_id
  - bulk-entry "BE-" QR (box_id + transaction_no)        → get_bulk_entry_box

All return {"success": True, "box": {...}} on a hit, or raise HTTPException(404).
The legacy "TX/CONS" QR path (reference GET /inward/{company}/{txn}) is NOT ported
— the rebuild has no inward module — so that one QR format is unsupported here.

Every path ends with the cold-storage stock ({cfpl,cdpl}_cold_stocks), searched only
after the tables above miss: a cold-destined inward writes the same box into both
boxes_v2 and cold_stocks, and the boxes_v2 row is the richer one. A box held only in
cold storage is still found, and park_boxes snapshots it from there on dispatch.

The manual and "TR-" paths (and stock_records) then try ERP Material In, the last
source, read only (material_in_source.find_box: po_box with its PO header, line and lot
section; the Material In QR is {"tx": the PO's transaction_no, "bi": the box_id}). It is
a separate, later statement that runs only when IMS's search above has missed, so that
search, its SQL and its order are as they were, and an IMS row always wins over a
Material In box with the same key. The "BE-" path never reads it.

Where the box is. The stock tables are kept by IMS and a dispatch never takes a box out
of them (record only), so a box already sent is still found here. Every hit, whichever
table it came from, is checked against the box's transfer history (unavailable_boxes):
its LATEST transfer that exists decides, latest by when the box joined it (a box scanned
onto a transfer days after it was created joined it then). No transfer: free. In transit
(Dispatch / Partial, or no receipt yet): 409. Received and acknowledged on that receipt:
the box is at the transfer's To site, so it may go on from there and nowhere else (409
naming that site). Received without it (written off as short): 409. Sites are compared
as sites (app.core.sites.same_site: 'Warehouse A185' is A185, 'D-39' is Savla D-39;
'Cold Storage' is a site of its own). Receipts are read from both ledgers: the interunit
one (ERP's, and IMS's warehouse receive) and IMS's cold one (cold_transfer_in_headers /
cold_transfer_inboxes, which IMS's cold receive writes until IMS closes, 2026-10-19).
The lookups take the From site of the dispatch being made (`from_warehouse`); without
one every transfer in the history blocks. create_service runs the same check on POST /
PUT /transfers with the dispatch's own From site. A deleted transfer frees its boxes.
A Material In box is judged the same way: never transferred, it is free from any site.
stock_records finds boxes as the "TR-" QR lookup does, without that check, for
create_service to judge a request dispatch's boxes by their own records.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.core import sites
from app.modules.transfer.services import material_in_source
from app.modules.transfer.services.stock_service import _table_exists


def _prefix(company: str) -> str:
    return "cdpl" if (company or "").strip().lower() == "cdpl" else "cfpl"


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _box_from_row(r: dict) -> dict:
    """Shape a unified search row (see _V2_BRANCH / _BULK_BRANCH) into the box payload.
    quantity_units/packaging_type/quality_grade/count are unconsumed by the scanner, so
    they're no longer selected — kept in the shape as null for response compatibility."""
    return {
        "box_id": r.get("box_id"),
        "transaction_no": r.get("transaction_no"),
        "box_number": r.get("box_number"),
        "article_description": r.get("article_description"),
        "item_description": r.get("item_description") or r.get("article_description"),
        "sku_id": r.get("sku_id"),
        "item_category": r.get("item_category"),
        "sub_category": r.get("sub_category"),
        "material_type": r.get("material_type"),
        "net_weight": _f(r.get("net_weight")),
        "gross_weight": _f(r.get("gross_weight")),
        "lot_number": r.get("lot_number"),
        "batch_number": r.get("batch_number"),
        "uom": r.get("uom"),
        "quantity_units": None,
        "packaging_type": None,
        "quality_grade": None,
        "count": None,
    }


# One UNION-ALL branch per candidate table, projecting the SAME columns/types in the SAME
# order so the branches union cleanly. Casts pin the types (a NULL/literal in one branch must
# match a real column in another). `{idcol}` is box_id or box_number; every branch binds
# $1 (the id) and $2 (transaction_no). A `_prio` literal drives ORDER BY so v2 wins over bulk.
_V2_BRANCH = """
    SELECT b.box_id::text AS box_id, b.transaction_no::text AS transaction_no,
           b.box_number::bigint AS box_number, b.article_description::text AS article_description,
           a.item_description::text AS item_description, a.sku_id::bigint AS sku_id,
           a.item_category::text AS item_category, a.sub_category::text AS sub_category,
           a.material_type::text AS material_type,
           b.net_weight::numeric AS net_weight, b.gross_weight::numeric AS gross_weight,
           COALESCE(NULLIF(b.lot_number, ''), a.lot_number)::text AS lot_number,
           b.batch_number::text AS batch_number, a.uom::text AS uom,
           {prio} AS _prio
    FROM {box} b
    LEFT JOIN {art} a
      ON b.transaction_no = a.transaction_no
     AND b.article_description = a.item_description
    WHERE b.{idcol} = $1 AND b.transaction_no = $2
"""

_BULK_BRANCH = """
    SELECT box_id::text AS box_id, transaction_no::text AS transaction_no,
           COALESCE(box_number, 0)::bigint AS box_number, article_description::text AS article_description,
           article_description::text AS item_description, NULL::bigint AS sku_id,
           ''::text AS item_category, ''::text AS sub_category, 'RM'::text AS material_type,
           net_weight::numeric AS net_weight, gross_weight::numeric AS gross_weight,
           lot_number::text AS lot_number, ''::text AS batch_number, 'BAG'::text AS uom,
           {prio} AS _prio
    FROM {table}
    WHERE box_id = $1 AND transaction_no = $2
"""

# Cold-storage stock. It has no box_number, gross weight, batch, unit or material type:
# box_number is the box_id's numeric suffix ('34732254-5' -> 5; none, or longer than 9
# digits, -> 0, so it fits interunit_transfer_boxes' integer column and a JS number), the
# one weight (weight_kg) stands for both net and gross, and group / subgroup fill
# category / sub category. `{match}` is _COLD_MATCH[id_col].
_COLD_BRANCH = """
    SELECT box_id::text AS box_id, transaction_no::text AS transaction_no,
           COALESCE(NULLIF(substring(box_id from '-([0-9]{{1,9}})$'), '')::bigint, 0) AS box_number,
           item_description::text AS article_description,
           item_description::text AS item_description, NULL::bigint AS sku_id,
           COALESCE(group_name, '')::text AS item_category, COALESCE(item_subgroup, '')::text AS sub_category,
           NULL::text AS material_type,
           weight_kg::numeric AS net_weight, weight_kg::numeric AS gross_weight,
           lot_no::text AS lot_number, ''::text AS batch_number, NULL::text AS uom,
           {prio} AS _prio
    FROM {table}
    WHERE {match}
"""
_COLD_MATCH = {
    "box_id": "box_id = $1 AND transaction_no = $2",
    "box_number": "transaction_no = $2 AND substring(box_id from '-([0-9]{1,9})$')::bigint = $1",
}


# Process-lifetime existence cache. `to_regclass` is a catalog round-trip and the box tables
# don't come/go at runtime, so each table is probed once instead of on every scan.
# ponytail: restart the process to pick up a newly-created box table (rare; acceptable ceiling).
_EXISTS: dict[str, bool] = {}


async def _exists(conn, table: str) -> bool:
    hit = _EXISTS.get(table)
    if hit is None:
        hit = await _table_exists(conn, table)
        _EXISTS[table] = hit
    return hit


async def _find(conn, *, id_col: str, id_val, transaction_no: str,
                prefixes: tuple[str, ...], include_v2: bool, include_bulk: bool,
                include_cold: bool = True, include_po: bool = False) -> dict | None:
    """Single-round-trip box search: UNION ALL over the candidate tables that exist, ordered
    by priority (v2, then bulk, then cold stocks, each in `prefixes` order), LIMIT 1.
    Returns the top hit as the box payload (_box_from_row), or None. With `include_po`,
    when that search misses (or there is no such table to search), ERP Material In last
    (material_in_source.find_box, which may refuse a box it finds with 409): a separate,
    later statement, so IMS's search is sent as it always was and an IMS row wins over a
    Material In box with the same key. Where the box is is not judged here (_search does
    that)."""
    branches: list[str] = []
    prio = 0
    if include_v2:
        for prefix in prefixes:
            box, art = f"{prefix}_boxes_v2", f"{prefix}_articles_v2"
            if await _exists(conn, box):
                prio += 1
                branches.append(_V2_BRANCH.format(box=box, art=art, idcol=id_col, prio=prio))
    if include_bulk:
        for prefix in prefixes:
            table = f"{prefix}_bulk_entry_boxes"
            if await _exists(conn, table):
                prio += 1
                branches.append(_BULK_BRANCH.format(table=table, prio=prio))
    if include_cold:
        for prefix in prefixes:
            table = f"{prefix}_cold_stocks"
            if await _exists(conn, table):
                prio += 1
                branches.append(_COLD_BRANCH.format(table=table, prio=prio, match=_COLD_MATCH[id_col]))
    if branches:
        sql = "SELECT * FROM (" + " UNION ALL ".join(branches) + ") u ORDER BY _prio LIMIT 1"
        row = await conn.fetchrow(sql, id_val, transaction_no)
        if row:
            return _box_from_row(dict(row))
    if include_po:
        return await material_in_source.find_box(conn, id_col=id_col, id_val=id_val,
                                                 transaction_no=transaction_no)
    return None


async def _search(conn, *, id_col: str, id_val, transaction_no: str,
                  prefixes: tuple[str, ...], include_v2: bool, include_bulk: bool,
                  include_cold: bool = True, include_po: bool = False,
                  from_warehouse: str | None = None) -> dict | None:
    """_find, and a hit that cannot be sent from `from_warehouse` (in transit, at another
    site, written off — unavailable_boxes) is 409."""
    box = await _find(conn, id_col=id_col, id_val=id_val, transaction_no=transaction_no,
                      prefixes=prefixes, include_v2=include_v2, include_bulk=include_bulk,
                      include_cold=include_cold, include_po=include_po)
    if box is None:
        return None
    held = await unavailable_boxes(conn, [(box["box_id"], box["transaction_no"])],
                                   from_warehouse=from_warehouse)
    if held:
        raise HTTPException(409, unavailable_message(held[0]))
    return box


async def stock_records(conn, boxes) -> dict[tuple[str, str], dict]:
    """The stock record of each real box of `boxes` ((box_id, transaction_no) pairs) as
    the "TR-" QR lookup finds it — boxes_v2, then bulk_entry_boxes, then cold_stocks,
    cfpl before cdpl, then (none of them having it) ERP Material In, read only
    (material_in_source.find_box: its article and lot as resolved there; 409 for a box on
    a deleted PO, or with a lot or article too long to record) — as the box payload, by
    box key (_box_key); a box found nowhere is left out. Where the box is is not judged
    (unavailable_boxes does that). Read only: one query per box, and for a box IMS's
    tables miss up to two more. create_service judges a request dispatch's boxes by these
    records, not by what the client says they are."""
    out: dict[tuple[str, str], dict] = {}
    for key in dict.fromkeys(k for k in (_box_key(b, t) for b, t in boxes) if k):
        box = await _find(conn, id_col="box_id", id_val=key[0], transaction_no=key[1],
                          prefixes=("cfpl", "cdpl"), include_v2=True, include_bulk=True,
                          include_po=True)
        if box is not None:
            out[key] = box
    return out


# ── Where a box is: its transfer history ─────────────────────────────────────
# The box rows of the given box ids ($1), on any transfer, matched trimmed: IMS stores a
# box's box_id / transaction_no as they were sent. SELECT *: the table is IMS's, and
# created_at (when the box joined the transfer) is read with .get(), so a database whose
# rows lack it orders by the transfer alone. Their transfers are read next (_TRANSFERS,
# also whole, for the same reason); a box row whose transfer is gone is an orphan and
# holds nothing (a transfer's boxes are deleted with it).
_HISTORY = "SELECT * FROM interunit_transfer_boxes WHERE btrim(box_id) = ANY($1::text[])"
_TRANSFERS = "SELECT * FROM interunit_transfers_header WHERE id = ANY($1::bigint[])"

# The receipts of the given transfers ($1), in two ledgers. The interunit one: ERP's
# receipts and IMS's warehouse receive; each receipt's box rows are read whole
# (_RECEIPT_BOXES; SELECT *: original_box_id, the box id IMS's STBR swapped out, is
# IMS's column). IMS's cold one, written by IMS's cold receive until IMS closes
# (2026-10-19): it put each box into the destination's cold stock as it was received,
# kept the receipt Pending (and the transfer's own status) until every box was in, and
# then dropped its interunit staging rows.
_RECEIPTS = ("SELECT id, transfer_out_id, status FROM interunit_transfer_in_header "
             "WHERE transfer_out_id = ANY($1::bigint[])")
_RECEIPT_BOXES = "SELECT * FROM interunit_transfer_in_boxes WHERE header_id = ANY($1::bigint[])"
_COLD_RECEIPTS = ("SELECT id, transfer_out_id, status FROM cold_transfer_in_headers "
                  "WHERE transfer_out_id = ANY($1::bigint[])")
_COLD_RECEIPT_BOXES = ("SELECT header_id, box_id, transaction_no FROM cold_transfer_inboxes "
                       "WHERE header_id = ANY($1::bigint[])")

# Transfer statuses, lower case. Besides ERP's Dispatch / Partial / Received, IMS left
# three on older transfers. IMS shows 'Completed' as Dispatch and 'In Transit' as in
# transit, and its backfill parks the boxes of both as in transit: they are in transit
# (with a Received receipt, received). 'Partially Received' is IMS's old cold receive
# part-way: the boxes its receipts took in are at the To site, the rest in transit.
_RECEIVED = "received"
_PARTLY_RECEIVED = "partially received"
_IN_TRANSIT = frozenset({"dispatch", "partial", "in transit", "completed"})


def _box_key(box_id, transaction_no) -> tuple[str, str] | None:
    """(box_id, transaction_no) of a real box, trimmed; None for a quantity-only entry (no
    box_id or transaction_no, or transaction 'DIRECT'), which the guard never refuses."""
    bid, tno = (box_id or "").strip(), (transaction_no or "").strip()
    if not bid or not tno or tno == "DIRECT":
        return None
    return bid, tno


def _status(status) -> str:
    return (status or "").strip().lower()


def is_in_transit_status(status) -> bool:
    """Whether a transfer's own status says it is in transit: Dispatch / Partial, or
    IMS's older 'In Transit' / 'Completed' (case and spaces aside)."""
    return _status(status) in _IN_TRANSIT


def _joined(created_ts, created_at):
    """When a box joined a transfer: its box row's created_at (a box added later, by a
    later scan or an edit, joined then), but never before the transfer's created_ts: a
    box cannot join a transfer before it exists, so a created_at stamped by another
    clock cannot move the transfer earlier. Either alone when the other is missing;
    None when both are."""
    if created_ts is None or created_at is None:
        return created_ts if created_at is None else created_at
    try:
        return max(created_ts, created_at)
    except TypeError:            # one with a time zone and one without
        return created_ts


def _recency(row) -> tuple:
    """Sort key of a transfer in a box's history: when the box joined it (_joined; none =
    oldest). Not the id: ids are time-based short ids and wrap. The id only breaks a tie."""
    return (row["joined_ts"] is not None, row["joined_ts"], row["transfer_id"])


async def _history(conn, wanted) -> dict:
    """{box key: [one dict per transfer that exists and has the box: box_row_id (its
    interunit_transfer_boxes id), transfer_id, challan_no, status, to_site, joined_ts]}
    for the `wanted` box keys (_box_key); a box on no transfer is not in it. Read only:
    two queries, none without the transfer tables."""
    if not wanted or not (await _exists(conn, "interunit_transfer_boxes")
                          and await _exists(conn, "interunit_transfers_header")):
        return {}
    rows = [r for r in await conn.fetch(_HISTORY, sorted({bid for bid, _ in wanted}))
            if _box_key(r["box_id"], r["transaction_no"]) in wanted]
    ids = sorted({r["header_id"] for r in rows if r["header_id"] is not None})
    if not ids:
        return {}
    transfers = {t["id"]: t for t in await conn.fetch(_TRANSFERS, ids)}
    history: dict[tuple, list] = {}
    for r in rows:
        t = transfers.get(r["header_id"])
        if t is None:
            continue
        history.setdefault(_box_key(r["box_id"], r["transaction_no"]), []).append({
            "box_row_id": r.get("id"), "transfer_id": t["id"], "challan_no": t.get("challan_no"),
            "status": t.get("status"), "to_site": t.get("to_site"),
            "joined_ts": _joined(t.get("created_ts"), r.get("created_at")),
        })
    return history


def _latest(history: dict, transfer_id: int | None = None) -> dict:
    """{box key: its LATEST transfer in `history` (by _recency)}, leaving out
    `transfer_id`; a box on no other transfer is not in it."""
    latest = {}
    for key, rows in history.items():
        rows = [r for r in rows if transfer_id is None or r["transfer_id"] != transfer_id]
        if rows:
            latest[key] = max(rows, key=_recency)
    return latest


def _acknowledges(row, box_row_id, box_id, transaction_no) -> bool:
    """Whether receipt box row `row` acknowledges the dispatched box (`box_row_id`, its
    interunit_transfer_boxes id; `box_id` / `transaction_no` as dispatched): by
    transfer_out_box_id, the box row the receiving screen names; else by its box_id and
    transaction_no, trimmed, or by IMS's original_box_id (the box id its STBR swapped
    out) and transaction_no; a row without a transaction_no (a quantity-only line, or a
    client that sent none) by box_id alone."""
    named = row.get("transfer_out_box_id")
    if named is not None and box_row_id is not None and named == box_row_id:
        return True
    bid = (box_id or "").strip()
    ids = {(row.get("box_id") or "").strip(), (row.get("original_box_id") or "").strip()}
    if not bid or bid not in ids:
        return False
    tno = (row.get("transaction_no") or "").strip()
    return not tno or tno == (transaction_no or "").strip()


async def _cold_receipts(conn, transfer_ids) -> tuple[dict, dict]:
    """IMS's cold receipts of `transfer_ids`: ({transfer id: its cold receipt headers (id,
    status)}, {transfer id: the box keys they took in}). Both empty without IMS's cold
    tables. Read only: two queries at most."""
    heads: dict[int, list] = {}
    taken: dict[int, set] = {}
    if not transfer_ids or not (await _exists(conn, "cold_transfer_in_headers")
                                and await _exists(conn, "cold_transfer_inboxes")):
        return heads, taken
    owner = {}
    for h in await conn.fetch(_COLD_RECEIPTS, sorted(transfer_ids)):
        heads.setdefault(h["transfer_out_id"], []).append(h)
        owner[h["id"]] = h["transfer_out_id"]
    if owner:
        for b in await conn.fetch(_COLD_RECEIPT_BOXES, sorted(owner)):
            key = _box_key(b["box_id"], b["transaction_no"])
            if key and b["header_id"] in owner:
                taken.setdefault(owner[b["header_id"]], set()).add(key)
    return heads, taken


async def taken_in_by_ims(conn, transfer_id: int) -> set[tuple[str, str]]:
    """The box keys IMS's cold receive took in on transfer `transfer_id`: each is in the
    To site's cold stock, whatever the transfer's status says, so it is not put back in
    transit (reversal_service). Empty without IMS's cold tables. Read only."""
    return (await _cold_receipts(conn, [transfer_id]))[1].get(transfer_id, set())


async def _where(conn, latest: dict) -> dict:
    """{box key: 'arrived' | 'in_transit' | 'short'} for each box of `latest` (_latest), by
    its latest transfer and that transfer's receipts in both ledgers:
      - taken in by IMS's cold receive: arrived, whatever the status;
      - received (its status 'Received', or a Received receipt in either ledger): with no
        receipt at all still in transit; acknowledged on a receipt (_acknowledges):
        arrived; else written off: short;
      - 'Partially Received': acknowledged, arrived; else in transit;
      - any other status: in transit (an acknowledgement on a Pending receipt is not a
        receipt until it is finalized).
    Read only: up to four queries, for these transfers only."""
    ids = sorted({r["transfer_id"] for r in latest.values()})
    if not ids:
        return {}
    receipts: dict[int, list] = {}
    if await _exists(conn, "interunit_transfer_in_header"):
        for h in await conn.fetch(_RECEIPTS, ids):
            receipts.setdefault(h["transfer_out_id"], []).append(h)
    cold, taken = await _cold_receipts(conn, ids)
    status = {r["transfer_id"]: _status(r["status"]) for r in latest.values()}
    received = {tid for tid in ids if status[tid] == _RECEIVED or any(
        _status(h["status"]) == _RECEIVED for h in (*receipts.get(tid, ()), *cold.get(tid, ())))}
    counted = received | {tid for tid in ids if status[tid] == _PARTLY_RECEIVED}

    # The interunit receipts' box rows, only where an acknowledgement counts.
    owner = {h["id"]: tid for tid in counted for h in receipts.get(tid, ())}
    acks: dict[int, list] = {}
    if owner and await _exists(conn, "interunit_transfer_in_boxes"):
        for b in await conn.fetch(_RECEIPT_BOXES, sorted(owner)):
            acks.setdefault(owner.get(b["header_id"]), []).append(b)

    where = {}
    for key, r in latest.items():
        tid = r["transfer_id"]
        acked = any(_acknowledges(b, r["box_row_id"], *key) for b in acks.get(tid, ()))
        if key in taken.get(tid, ()):
            where[key] = "arrived"
        elif tid in received:
            has_receipt = bool(receipts.get(tid) or cold.get(tid))
            where[key] = "in_transit" if not has_receipt else "arrived" if acked else "short"
        else:
            where[key] = "arrived" if tid in counted and acked else "in_transit"
    return where


async def sent_on_since(conn, boxes, transfer_id: int, created_ts=None) -> set[tuple[str, str]]:
    """The keys of `boxes` ((box_id, transaction_no) pairs) that a transfer newer than
    `transfer_id` has: one the box joined after it joined `transfer_id` (ordered as
    _recency orders them; a box not on `transfer_id` counts as having joined it at
    `created_ts`, that transfer's own). Such a box went on after it, so undoing that
    transfer's receipt must not put the box back in transit on it, nor may a backfill
    park it there (reversal_service). Read only."""
    wanted = dict.fromkeys(k for k in (_box_key(b, t) for b, t in boxes) if k)
    out: set[tuple[str, str]] = set()
    for key, rows in (await _history(conn, wanted)).items():
        mine = [r for r in rows if r["transfer_id"] == transfer_id]
        this = _recency(max(mine, key=_recency) if mine else {"joined_ts": created_ts, "transfer_id": transfer_id})
        if any(_recency(r) > this for r in rows if r["transfer_id"] != transfer_id):
            out.add(key)
    return out


async def unavailable_boxes(conn, boxes, *, from_warehouse: str | None = None,
                            transfer_id: int | None = None) -> list[dict]:
    """Which of `boxes` ((box_id, transaction_no) pairs) cannot be sent from
    `from_warehouse`, judged by each box's LATEST transfer that exists (by when the box
    joined it, _recency; with `transfer_id`, an edit, that transfer is left out of the
    history) and its receipts (_where):
      - none: free, not reported;
      - in transit (not received, or received with no receipt): 'in_transit';
      - received, the box not acknowledged (written off as short): 'short';
      - arrived (acknowledged, or taken in by IMS's cold receive): the box is at that
        transfer's To site. Free when `from_warehouse` names that site (sites.same_site:
        'A-185' / 'Warehouse A185', 'D-39' / 'Savla D-39'), else 'elsewhere'. Without a
        `from_warehouse` it matches no site, so every transfer in the history blocks;
        'Cold Storage' is a site of its own and matches no store.
    One dict per refused box, in the order of `boxes`: box_id, transaction_no, reason,
    transfer_id, challan_no, site (that transfer's To site, as stored). Quantity-only
    entries (_box_key None) are never refused. Read only: two queries for the history
    and, when a box has a transfer, up to four for its receipts."""
    wanted = dict.fromkeys(k for k in (_box_key(b, t) for b, t in boxes) if k)
    latest = _latest(await _history(conn, wanted), transfer_id)
    where = await _where(conn, latest)

    out: list[dict] = []
    for key in wanted:
        last = latest.get(key)
        if last is None:
            continue
        reason = where[key]
        if reason == "arrived":
            if sites.same_site(last["to_site"], from_warehouse):
                continue
            reason = "elsewhere"
        out.append({"box_id": key[0], "transaction_no": key[1], "reason": reason,
                    "transfer_id": last["transfer_id"], "challan_no": last["challan_no"],
                    "site": last["to_site"]})
    return out


def unavailable_message(hit: dict) -> str:
    """The refusal for one unavailable_boxes hit, naming the site by its canonical name:
      in transit: Box <id> is already on challan <C> (in transit)
      elsewhere:  Box <id> was received at <site> on challan <C>; send it from <site>.
      short:      Box <id> did not arrive on challan <C> (written off as short)."""
    box, challan = hit["box_id"], hit.get("challan_no") or "(unknown)"
    if hit.get("reason") == "short":
        return f"Box {box} did not arrive on challan {challan} (written off as short)."
    if hit.get("reason") == "elsewhere":
        site = sites.canonical_site(hit.get("site")) or "(unknown)"
        return f"Box {box} was received at {site} on challan {challan}; send it from {site}."
    return f"Box {box} is already on challan {challan} (in transit)"


async def get_box_by_number(conn, company: str, box_number: int, transaction_no: str,
                            from_warehouse: str | None = None) -> dict:
    """Manual box entry: box_number + transaction_no. Named company's boxes_v2 first, then the
    other (company-agnostic UI), then both cold stocks (box number = the box_id's numeric
    suffix there). One query across the existing tables; when it misses, ERP Material In
    last (material_in_source.find_box: the PO's box of that number, 409 when more than one
    box carries it, as box numbers restart in every lot section). `from_warehouse` is the
    From site of the dispatch the box is scanned for (see unavailable_boxes)."""
    primary = _prefix(company)
    other = "cdpl" if primary == "cfpl" else "cfpl"
    box = await _search(conn, id_col="box_number", id_val=box_number, transaction_no=transaction_no,
                        prefixes=(primary, other), include_v2=True, include_bulk=False,
                        include_po=True, from_warehouse=from_warehouse)
    if box:
        return {"success": True, "box": box}
    raise HTTPException(404, f"Box #{box_number} with transaction_no '{transaction_no}' not found")


async def get_box_by_box_id(conn, company: str, box_id: str, transaction_no: str,
                            from_warehouse: str | None = None) -> dict:
    """New "TR-" QR: box_id + transaction_no. Both companies' boxes_v2 first, then
    bulk_entry_boxes, then cold_stocks — all in one query (earlier tables win via _prio).
    When that misses, ERP Material In last, read only (material_in_source.find_box; its
    QR label carries the PO's transaction_no and the box_id)."""
    box = await _search(conn, id_col="box_id", id_val=box_id, transaction_no=transaction_no,
                        prefixes=("cfpl", "cdpl"), include_v2=True, include_bulk=True,
                        include_po=True, from_warehouse=from_warehouse)
    if box:
        return {"success": True, "box": box}
    raise HTTPException(404, f"Box '{box_id}' with transaction '{transaction_no}' not found")


async def get_bulk_entry_box(conn, company: str, box_id: str, transaction_no: str,
                             from_warehouse: str | None = None) -> dict:
    """Bulk-entry "BE-" QR: box_id + transaction_no in bulk_entry_boxes, then cold_stocks
    (both companies); never ERP Material In."""
    box = await _search(conn, id_col="box_id", id_val=box_id, transaction_no=transaction_no,
                        prefixes=("cfpl", "cdpl"), include_v2=False, include_bulk=True,
                        from_warehouse=from_warehouse)
    if box:
        return {"success": True, "box": box}
    raise HTTPException(404, f"Box with box_id '{box_id}' and transaction_no '{transaction_no}' not found in bulk entry boxes or cold storage")
