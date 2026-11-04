"""Transfer-OUT create + update (docs 07 / 08).

Ported from interunit_tools.create_transfer / update_transfer + cold_transfer_out_tools
into one unified cold+warehouse path. RECORD ONLY: the stock tables ({cfpl,cdpl}_boxes_v2,
_bulk_entry_boxes, _cold_stocks) are kept by IMS, and ERP Material In's (po_box) by the
purchase module; they are only read here, never written.
create_transfer and update_transfer share `_persist_lines_boxes_and_park`, which:
  - inserts the lines (net-weight recompute) and boxes (duplicate (box_id, transaction_no)
    rejected — the guard against the "boxes collapsed to 1" bug; so is a second box with
    the box_id of another transaction's box, 409: IMS and ERP Material In mint box ids
    alike, and a receipt takes one box per box id; each box matched to its line by
    article);
  - parks each scanned/derived box into pending_transfer_stock as a copy of its stock
    row, which is left as it is (reversal_service.park_boxes);
  - settles the transfer (_settle), from its rows as they now are. Every save of a
    transfer ends with it — these two, and the scan screen's scan / remove / weight
    edit (scan_service) — so each leaves the rows create would have written:
      - a transfer without a request parks its box-less manual lines as tracking-only
        rows so manual stock is never dropped (a line counts as boxed when a box of its
        article — and of its lot, if the line has one — covers it). A transfer from a
        request has none: only its scanned boxes are in transit;
      - tags from_cold_unit for cold-source transfers;
      - recomputes Dispatch vs Partial (from a request: its boxes against what the
        request asks for; without one: its boxes plus its box-less lines);
      - records the ordered-vs-shipped gap on the header (flag-only, warehouse source only).
Articles are compared everywhere as _article_key (whitespace collapsed, upper case).

Where the box is (_refuse_unavailable_boxes): since a dispatch takes nothing out of
stock, a box (box_id + transaction_no, not 'DIRECT') is judged by its latest transfer
(lookup_service.unavailable_boxes, by when the box joined each; on an edit the edited
transfer is left out): one in transit, one received at a site other than this dispatch's
From site, or one written off as short on its receipt is refused with 409 naming it and
its challan, before anything is written (the box lookups refuse it too). A box received
at this From site, in any spelling of it, may go on. The boxes are advisory-locked
first, so two dispatches of one box cannot both pass.

Sites are compared as sites (app.core.sites: 'A185' is 'A-185' and 'Warehouse A185') and
a new transfer names them by their canonical names; refusals name them so too.

A dispatch from a request, and an edit of one, also carries at least one real box
(409 "Scan at least one box before sending.") and only the boxes the request asks for
(_refuse_boxes_off_the_request, 409 listing each other box); both before any write.
Each real box there is judged, and stored, by its own article and lot (_as_recorded:
an edit's own boxes as saved, any other as the box lookups find it in stock; 409 for
one found nowhere), not by what the client sends.

create_transfer additionally inserts the header (status 'Dispatch') and flips the
originating request to 'Transferred' — a transfer that names a request is refused
unless that request is 'Approved'. Its work in the transaction (_create) is also the
first scan of a request on the scan screen. update_transfer edits only a transfer in
transit (refuse_received, with its header row locked: 409 once it is received or a
receipt of it has begun); it drops the prior pending rows (restore_to_source) before
re-inserting/re-parking — a kept box re-parks from its own prior row, not from the stock
tables, and keeps the time it joined the transfer (unless it went on a newer transfer
since, when it joins again now) — then stamps edited_at; an edit never changes which
request a transfer came from.
A transfer from a request is locked to the request as approved (_dispatch_terms): its
route (409 on any other), its lines and its reason text. The boxes, the date and the
vehicle / driver / approver details are the client's. If the request has since been
deleted, an edit is locked to what the transfer dispatched instead.
Everything runs inside one transaction (atomic dispatch / edit), which for a transfer
from a request first locks the request row (_LOCK_REQUEST_OF), as the scan screen does.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import NamedTuple

from fastapi import HTTPException

from app.core import sites
from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.auth.middleware import AuthUser
from app.modules.transfer import permissions, schemas
from app.modules.transfer.services import lookup_service, query_service, reversal_service
from app.modules.transfer.services.stock_service import _date, _dec, _table_exists

# Boxes named in a refusal before the rest are counted ("And 3 more.").
_REFUSAL_MAX_LISTED = 10

# One transaction-scoped advisory lock per box of a dispatch / edit, taken in hash order
# so two transactions sharing boxes cannot deadlock (Postgres evaluates the volatile
# lock call after the ORDER BY). The keys are "interunit_transfer_box|<box_id>|<tno>".
_LOCK_BOXES = (
    "SELECT pg_advisory_xact_lock(hashtext(k)::bigint) FROM unnest($1::text[]) AS k "
    "ORDER BY hashtext(k)"
)

# A line's tracking-only in-transit rows (reversal_service.park_lines): LINE-<line_id>-<n>.
_LINE_ROW = re.compile(r"LINE-(\d+)-\d+")

# The request row of a transfer ($1), locked before anything else of a save is read or
# written: every change to a request's transfer — the scan screen's (scan_service), an
# edit (update_transfer), a delete (delete_service.delete_transfer) — takes it, so they
# run one after another. It locks nothing for a transfer without a request.
_LOCK_REQUEST_OF = (
    "SELECT id, request_no, status FROM interunit_transfer_requests "
    "WHERE id = (SELECT request_id FROM interunit_transfers_header WHERE id = $1) FOR UPDATE"
)


def _gen_challan_no() -> str:
    return "TRANS" + datetime.now().strftime("%Y%m%d%H%M")


def _article_key(article) -> str:
    """An article name as it is compared with another: whitespace collapsed, upper case
    (the screens' tracker compares the same way)."""
    return " ".join((article or "").split()).upper()


def _lot_key(lot) -> str:
    """A lot as it is compared with another: trimmed, otherwise as is."""
    return (lot or "").strip()


def _line_qty(line) -> int:
    """A line's ordered quantity as it is stored (no quantity: 1)."""
    return int(line.quantity) if line.quantity else 1


def _line_boxes(line) -> int:
    """How many boxes a request line asks for (R1): its qty; none for no quantity or one
    below 1. The screens read it so (lib/transferBoxMatch lineQty; the API sends no
    quantity as "0"), unlike _line_qty, which stores no quantity as 1."""
    return max(int(line.quantity), 0) if line.quantity else 0


def _not_approved(status) -> str:
    """The refusal of a dispatch from a request that is not Approved."""
    return f"Request is '{status or 'Pending'}'; only an Approved request can be dispatched"


def _dispatched_at(stock_trf_date) -> datetime:
    """When a transfer's in-transit rows say it left: midnight of its stock_trf_date (none:
    today), as a datetime — pending_transfer_stock.dispatched_at is a timestamp."""
    return datetime.combine(stock_trf_date or datetime.now().date(), datetime.min.time())


def _shared_box_id(box_id: str, held_tno: str, tno: str, challan: str | None = None) -> str:
    """The refusal of box `box_id` of transaction `tno` on a transfer that has (on
    `challan`, the scan screen) or is being sent with (no `challan`, a dispatch or an edit)
    the box of that box_id under transaction `held_tno`. IMS and ERP Material In mint box
    ids alike, and a receipt records one box per box id (receive_service), so such a
    transfer could never be received in full."""
    if challan:
        return (f"Box {box_id} of {tno} has the same box id as box {box_id} of {held_tno}, which is "
                f"already on transfer {challan}; a transfer can carry only one box with a given box id, "
                "so send this one on another transfer.")
    return (f"Box {box_id} of {tno} has the same box id as box {box_id} of {held_tno} on this transfer; "
            "a transfer can carry only one box with a given box id, so send one of them on another transfer.")


def _refusal(messages: list[str]) -> str:
    """One 409 detail from several refusals: one alone as it is; more as sentences, the
    first _REFUSAL_MAX_LISTED of them, then "And <n> more." for the rest."""
    if len(messages) == 1:
        return messages[0]
    listed = [m if m.endswith(".") else m + "." for m in messages[:_REFUSAL_MAX_LISTED]]
    if len(messages) > _REFUSAL_MAX_LISTED:
        listed.append(f"And {len(messages) - _REFUSAL_MAX_LISTED} more.")
    return " ".join(listed)


async def lock_boxes(conn, keys) -> None:
    """Take the box guard's advisory lock (_LOCK_BOXES) on each real box of `keys` (box
    keys, lookup_service._box_key; None for a quantity-only entry is skipped), held to
    commit. A dispatch, an edit and an inner cold transfer take it before R3 reads a
    box's transfers, so two of them sharing a box run one after the other. The caller
    holds a transaction. Nothing is locked for no box."""
    keys = sorted({k for k in keys if k})
    if keys:
        await conn.execute(_LOCK_BOXES, [f"interunit_transfer_box|{bid}|{tno}" for bid, tno in keys])


async def _refuse_unavailable_boxes(conn, boxes_in: list | None, from_site: str,
                                    transfer_id: int | None = None) -> None:
    """409 when a box of this dispatch cannot be sent from `from_site`, judged by its
    latest transfer (lookup_service.unavailable_boxes): in transit, received at another
    site, or written off as short; listing each such box with its challan. The stock
    tables are IMS's and a dispatch never takes a box out of them, so nothing else stops
    a box going on a second challan. A box received (and acknowledged) at `from_site`
    may be sent on. On an edit (`transfer_id`) that transfer is left out of the history,
    so its own boxes are judged by where they were before it. Quantity-only entries (no
    box_id / transaction_no, or 'DIRECT') are not boxes and pass. Callers run it inside
    their transaction, before any write.

    The boxes are locked first (lock_boxes, held to commit), so of two dispatches or
    edits carrying the same box the second waits for the first to commit, and its read
    then sees the first one's boxes and refuses."""
    await lock_boxes(conn, [lookup_service._box_key(b.box_id, b.transaction_no) for b in boxes_in or []])
    held = await lookup_service.unavailable_boxes(
        conn, [(b.box_id, b.transaction_no) for b in boxes_in or []],
        from_warehouse=from_site, transfer_id=transfer_id)
    if held:
        raise HTTPException(409, _refusal([lookup_service.unavailable_message(h) for h in held]))


def _off_request_boxes(lines: list, boxes: list, request_no: str) -> list[str]:
    """The boxes a request does not ask for, as refusals, in the order of `boxes`.

    The boxes are matched to the request's lines as the screens' tracker matches them:
    lines with a lot take boxes of their article AND lot first, then lines without one
    take boxes of their article with any lot; each box counts once, a line takes at most
    its qty boxes, and earlier boxes are taken first. A box left over is refused:
      - no line has its article: "Box <id> is <article>, which is not on request <REQ>.";
      - its article is only on lines of other lots:
        "Box <id> is lot <lot>; request <REQ> asks for lot <L> of <article>.";
      - else the lines that could take it are full:
        "Request <REQ> asks for <qty> box(es) of <article>; all are already on this dispatch."
    (<qty> is those lines' total; a line's qty is _line_boxes.) Every entry counts, a
    quantity-only one too."""
    keys = [(_article_key(l.item_description), _lot_key(l.lot_number)) for l in lines]
    room = [_line_boxes(l) for l in lines]
    taken = [False] * len(boxes)
    for with_lot in (True, False):
        for i, (art, lot) in enumerate(keys):
            if bool(lot) != with_lot:
                continue
            for j, b in enumerate(boxes):
                if room[i] <= 0:
                    break
                if (not taken[j] and _article_key(b.article) == art
                        and (not lot or _lot_key(b.lot_number) == lot)):
                    taken[j] = True
                    room[i] -= 1

    out: list[str] = []
    for j, b in enumerate(boxes):
        if taken[j]:
            continue
        box = (b.box_id or "").strip() or f"#{b.box_number}"
        art, lot = _article_key(b.article), _lot_key(b.lot_number)
        mine = [i for i, (a, _) in enumerate(keys) if a == art]
        if not mine:
            article = " ".join((b.article or "").split()) or "(no article)"
            out.append(f"Box {box} is {article}, which is not on request {request_no}.")
            continue
        article = " ".join((lines[mine[0]].item_description or "").split())
        could = [i for i in mine if not keys[i][1] or keys[i][1] == lot]
        if not could:
            lots = ", ".join(dict.fromkeys(keys[i][1] for i in mine))
            out.append(f"Box {box} is lot {lot or '(none)'}; request {request_no} asks for lot {lots} "
                       f"of {article}.")
        else:
            qty = sum(_line_boxes(lines[i]) for i in could)
            out.append(f"Request {request_no} asks for {qty} box(es) of {article}; "
                       "all are already on this dispatch.")
    return out


async def _as_recorded(conn, boxes: list, transfer_id: int | None = None) -> list:
    """`boxes` with each real box's article and lot taken from the box's own record, not
    from the client: on an edit (`transfer_id`) a box the transfer already has keeps its
    saved article and lot (its stock row may have changed or gone since); any other is
    looked up in the stock tables, then ERP Material In, as the "TR-" QR lookup finds it
    (lookup_service.stock_records). So a request dispatch is judged, and recorded, by
    what each box is. 409 listing each real box found in neither ("Box <id> with
    transaction <tno> was not found."). Quantity-only entries are returned as they are.
    Read only."""
    keys = [lookup_service._box_key(b.box_id, b.transaction_no) for b in boxes]
    known: dict = {}
    if transfer_id is not None:
        for r in await conn.fetch(
                "SELECT box_id, transaction_no, article, lot_number FROM interunit_transfer_boxes "
                "WHERE header_id = $1", transfer_id):
            key = lookup_service._box_key(r["box_id"], r["transaction_no"])
            if key:
                known.setdefault(key, (r["article"], r["lot_number"]))
    for key, box in (await lookup_service.stock_records(
            conn, [k for k in keys if k and k not in known])).items():
        known[key] = (box["item_description"], box["lot_number"])
    missing = dict.fromkeys(k for k in keys if k and k not in known)
    if missing:
        raise HTTPException(409, _refusal([f"Box {bid} with transaction {tno} was not found."
                                           for bid, tno in missing]))
    return [b if k is None else b.model_copy(update={"article": known[k][0] or "", "lot_number": known[k][1]})
            for b, k in zip(boxes, keys)]


async def _refuse_boxes_off_the_request(conn, terms: _Terms, boxes_in: list | None,
                                        transfer_id: int | None = None) -> list:
    """For a dispatch from a request, an edit of one, or a box scanned onto one (terms
    from _dispatch_terms; the transfer's id, for the last two): 409 "Scan at least one
    box before sending." without a real box (box_id + transaction_no, not 'DIRECT');
    409 for a real box not found (_as_recorded);
    and 409 listing every box the request does not ask for (_off_request_boxes), each
    real box judged by its own article and lot. Returns the boxes as recorded, which the
    caller stores. Nothing is written here."""
    boxes = list(boxes_in or [])
    if not any(lookup_service._box_key(b.box_id, b.transaction_no) for b in boxes):
        raise HTTPException(409, "Scan at least one box before sending.")
    boxes = await _as_recorded(conn, boxes, transfer_id)
    refused = _off_request_boxes(terms.lines, boxes, terms.request_no)
    if refused:
        raise HTTPException(409, _refusal(refused))
    return boxes


def _line_weights(line: schemas.TransferLineCreate) -> tuple[float, float]:
    """(net_weight, total_weight): use the frontend value when supplied, else recompute —
    FG = unit_pack_size * pack_size * qty; RM/PM/other = pack_size * qty."""
    pack_size = float(line.pack_size) if line.pack_size else 0.0
    qty = int(line.quantity) if line.quantity else 1
    ups = float(line.unit_pack_size) if line.unit_pack_size else 1.0
    fe_net = float(line.net_weight) if line.net_weight else 0.0
    if fe_net > 0:
        net = round(fe_net, 3)
    elif (line.material_type or "").upper() == "FG":
        net = round(ups * pack_size * qty, 3)
    else:
        net = round(pack_size * qty, 3)
    fe_total = float(line.total_weight) if line.total_weight else 0.0
    total = round(fe_total, 3) if fe_total > 0 else net
    return net, total


def _line_for(line_rows: list, article):
    """The id of the line a box is stored against: the line of its article (_article_key;
    of several, the last), else the first line. `line_rows` are the transfer's lines in
    order (id, item_desc_raw)."""
    by_article = {_article_key(l["item_desc_raw"]): l["id"] for l in line_rows}
    return by_article.get(_article_key(article), line_rows[0]["id"] if line_rows else None)


async def _insert_box(conn, header_id: int, line_rows: list, box, joined_at: datetime | None = None) -> dict:
    """Insert one box row of a transfer against its line (_line_for), box_id and
    transaction_no stored trimmed, as the guard and the pending rows match them, and
    created_at = when the box joined the transfer: `joined_at` (an edit keeps a kept
    box's), else now. A box's history is ordered by it (lookup_service), so a box scanned
    onto a transfer days after it was created is on it from then. Returns the box as
    park_boxes takes it."""
    matched = _line_for(line_rows, box.article)
    bid, tno = (box.box_id or "").strip(), (box.transaction_no or "").strip()
    created_at = joined_at or datetime.now()
    async def _ins_box():
        return await conn.execute(
            """
            INSERT INTO interunit_transfer_boxes
                (header_id, transfer_line_id, box_number, box_id, article, lot_number,
                 batch_number, transaction_no, net_weight, gross_weight, created_at, id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            header_id, matched, box.box_number, bid, box.article,
            box.lot_number or "", box.batch_number or "", tno,
            _dec(box.net_weight), _dec(box.gross_weight), created_at, new_short_time_id(),
        )
    await insert_with_pk_retry(conn, _ins_box)
    return {
        "box_id": bid, "transaction_no": tno,
        "article": box.article, "lot_number": box.lot_number,
        "batch_number": box.batch_number,
        "net_weight": box.net_weight, "gross_weight": box.gross_weight,
    }


async def _persist_lines_boxes_and_park(
    conn, *, header_id: int, challan_no: str, from_warehouse: str, to_warehouse: str,
    lines_in: list, boxes_in: list | None, stock_trf_date, created_by: str,
    prior: dict | None = None, joined: dict | None = None,
) -> None:
    """Insert lines + boxes for an existing header, park the boxes to pending (a copy of
    each box's stock row; stock is not touched), then settle the transfer (_settle: line
    rows, from_cold_unit, status, the warehouse reconcile flag). The caller owns the
    transaction and the header row (written with this route, stock_trf_date and
    created_by), and has run the box guards. `prior` is an edit's in-transit rows from
    before it (reversal_service.park_boxes); `joined` an edit's {box key: when the box
    joined the transfer}, so a box it keeps keeps that time (_insert_box). The boxes
    have passed the box guard, so an in-transit row another transfer still has for one
    of them is stale and gives way (park_boxes' take_over)."""
    # ── Lines ──
    line_rows: list[dict] = []
    for line in lines_in:
        net, total = _line_weights(line)
        qty = _line_qty(line)
        async def _ins_line(line=line, qty=qty, net=net, total=total):
            return await conn.fetchrow(
                """
                INSERT INTO interunit_transfers_lines
                    (header_id, rm_pm_fg_type, item_category, sub_category, item_desc_raw,
                     pack_size, qty, uom, unit_pack_size, net_weight, total_weight,
                     batch_number, lot_number, vakkal, id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                RETURNING id, item_desc_raw, lot_number, qty
                """,
                header_id, line.material_type, line.item_category, line.sub_category,
                line.item_description, _dec(line.pack_size) or 0, qty, line.uom or None,
                _dec(line.unit_pack_size) or 0, _dec(net), _dec(total),
                line.batch_number or "", line.lot_number or "", line.vakkal or "",
                new_short_time_id(),
            )
        line_rows.append(dict(await insert_with_pk_retry(conn, _ins_line)))

    # ── Boxes ── reject duplicate (box_id, transaction_no) within this transfer, and a
    #     second box with another transaction's box_id (409, _shared_box_id).
    box_input = list(boxes_in or [])
    seen: set = set()
    first_tno: dict = {}
    for box in box_input:
        bid = (box.box_id or "").strip()
        tno = (box.transaction_no or "").strip()
        if bid and tno and tno != "DIRECT":
            if (bid, tno) in seen:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Duplicate box_id '{bid}' for transaction '{tno}' in this transfer. "
                        "Every physical box must carry a unique box_id."
                    ),
                )
            seen.add((bid, tno))
            held_tno = first_tno.setdefault(bid, tno)
            if held_tno != tno:
                raise HTTPException(409, _shared_box_id(bid, held_tno, tno))

    box_dicts: list[dict] = []
    for box in box_input:
        key = lookup_service._box_key(box.box_id, box.transaction_no)
        box_dicts.append(await _insert_box(conn, header_id, line_rows, box, (joined or {}).get(key)))

    # ── Park boxes → pending, a snapshot of each stock row (the row itself stays) ──
    if box_dicts:
        await reversal_service.park_boxes(
            conn, transfer_out_id=header_id, challan_no=challan_no,
            from_site=from_warehouse, to_site=to_warehouse,
            boxes=box_dicts, dispatched_at=_dispatched_at(stock_trf_date), dispatched_by=created_by,
            cold_fallback=True, prior=prior, take_over=True,
        )

    await _settle(conn, header_id)


def _uncovered_lines(lines: list, boxes: list) -> list:
    """The lines (rows: id, item_desc_raw, lot_number) no box row (article, lot_number)
    covers, in line order. A line with a lot is covered by a box of the same article AND
    lot; a line without one by a box of the same article with any lot (a request line
    rarely names a lot, its boxes always carry one — without this it would park as
    phantom LINE- rows on top of its real boxes). Each box covers at most one line, and
    the lot-matched lines take their boxes first. Every box row counts, a quantity-only
    one too."""
    boxes_by_key: dict = {}
    boxes_by_article: dict = {}
    for box in boxes:
        art = _article_key(box["article"])
        k = (art, _lot_key(box["lot_number"]))
        boxes_by_key[k] = boxes_by_key.get(k, 0) + 1
        boxes_by_article[art] = boxes_by_article.get(art, 0) + 1
    covered_ids: set = set()
    for l in lines:
        art, lot = _article_key(l["item_desc_raw"]), _lot_key(l["lot_number"])
        if lot and boxes_by_key.get((art, lot), 0) > 0:
            boxes_by_key[(art, lot)] -= 1
            boxes_by_article[art] -= 1
            covered_ids.add(l["id"])
    for l in lines:
        art, lot = _article_key(l["item_desc_raw"]), _lot_key(l["lot_number"])
        if not lot and boxes_by_article.get(art, 0) > 0:
            boxes_by_article[art] -= 1
            covered_ids.add(l["id"])
    return [l for l in lines if l["id"] not in covered_ids]


def _snapshot(value):
    """An in-transit row's cold_storage_data as a dict (asyncpg returns JSONB as text);
    None for none, or for text that is not JSON."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _cold_unit(cold: dict) -> str | None:
    """The cold store a cold snapshot's row was at, by its canonical name
    (sites.cold_row_site: its unit, else its storage_location; 'D-39' -> 'Savla D-39',
    'Supreme Cold' -> 'Supreme', 'Eskimo Cold' -> 'Eskimo'); None when neither names a
    cold store: 'Cold Storage' (no one store), a warehouse floor, a name the site table
    does not know."""
    def text(v):
        return None if v is None else str(v)
    return sites.cold_row_site(text(cold.get("unit")), text(cold.get("storage_location"))) or None


async def _settle(conn, transfer_id: int) -> None:
    """Finish a save of a transfer from its rows as they now are (its header, lines, box
    rows and in-transit rows), so every save — create, edit, and the scan screen's scan,
    remove and weight edit (scan_service) — ends the same way and leaves what create
    would have written for the same lines and boxes:
      - line rows (LINE-<line_id>-<n>, reversal_service.park_lines): a transfer without a
        request has them for each line no box covers (_uncovered_lines), so manual stock
        is never dropped — parked for such a line that has none yet, dropped for every
        other line. A transfer from a request has none, whatever its lines: only its
        scanned boxes are in transit, and any line rows it has from before are dropped;
      - from_cold_unit (cold From site): the cold stores its in-transit rows' cold
        snapshots were at (their unit, else their storage location), by canonical name
        (_cold_unit), e.g. "Rishi, Savla D-39" or "Supreme"; NULL when none;
      - status, when it has a box row: Dispatch when what it ships covers what it orders,
        else Partial. From a request, its real boxes (box_id + transaction_no, not
        'DIRECT') against what the request asks for (_request_status); without one, its
        box rows plus its box-less lines against the qty its lines order;
      - unallocated_boxes (flag only, warehouse source only): its box rows not in transit,
        the ordered-vs-shipped box gap. A cold source isn't reconciled here — the parked
        boxes are the in-transit truth.
    The caller owns the transaction. Record only: no stock table is read or written."""
    h = await conn.fetchrow(
        "SELECT challan_no, from_site, to_site, request_id, stock_trf_date, created_by "
        "FROM interunit_transfers_header WHERE id = $1",
        transfer_id)
    lines = sorted(
        (dict(r) for r in await conn.fetch(
            "SELECT id, item_desc_raw, lot_number, qty, net_weight, total_weight "
            "FROM interunit_transfers_lines WHERE header_id = $1",
            transfer_id)),
        key=lambda l: l["id"])
    boxes = await conn.fetch(
        "SELECT box_id, transaction_no, article, lot_number FROM interunit_transfer_boxes WHERE header_id = $1",
        transfer_id)
    pending = await conn.fetch(
        "SELECT id, box_id, transaction_no, cold_storage_data FROM pending_transfer_stock "
        "WHERE transfer_out_id = $1",
        transfer_id)
    from_request = h["request_id"] is not None
    is_cold_source = reversal_service._is_cold_site(h["from_site"])

    # ── Line rows: one set per uncovered line of a transfer without a request ──
    uncovered = [] if from_request else _uncovered_lines(lines, boxes)
    keep = {l["id"] for l in uncovered}
    has_rows: set = set()
    drop: list = []
    for p in pending:
        m = _LINE_ROW.fullmatch(p["box_id"] or "")
        if not m or p["transaction_no"] != h["challan_no"]:
            continue
        if int(m[1]) in keep:
            has_rows.add(int(m[1]))
        else:
            drop.append(p["id"])
    if drop:
        await conn.execute("DELETE FROM pending_transfer_stock WHERE id = ANY($1::bigint[])", drop)
    unparked = [l for l in uncovered if l["id"] not in has_rows]
    if unparked:
        await reversal_service.park_lines(
            conn, transfer_out_id=transfer_id, challan_no=h["challan_no"],
            from_site=h["from_site"], to_site=h["to_site"], lines=unparked,
            dispatched_at=_dispatched_at(h["stock_trf_date"]), dispatched_by=h["created_by"] or "system",
        )

    # ── from_cold_unit (cold source): from the boxes' cold snapshots ──
    if is_cold_source:
        units: set = set()
        for p in pending:
            cold = _snapshot(p["cold_storage_data"])
            if isinstance(cold, dict) and (u := _cold_unit(cold)):
                units.add(u)
        await conn.execute(
            "UPDATE interunit_transfers_header SET from_cold_unit = $1 WHERE id = $2",
            ", ".join(sorted(units)) or None, transfer_id,
        )

    # ── Status: Dispatch when what it ships covers what it orders, else Partial. ──
    if boxes:
        if from_request:
            status = await _request_status(conn, h["request_id"], lines, boxes)
        else:
            ordered = sum(int(l["qty"] or 0) for l in lines)
            status = "Dispatch" if len(boxes) + len(uncovered) >= ordered else "Partial"
        await conn.execute(
            "UPDATE interunit_transfers_header SET status = $1 WHERE id = $2", status, transfer_id)

    # ── Reconcile (flag-only). Warehouse source only: record the ordered-vs-shipped box gap. ──
    if not is_cold_source:
        box_count = await conn.fetchval(
            "SELECT COUNT(*) FROM interunit_transfer_boxes WHERE header_id = $1", transfer_id) or 0
        parked = await conn.fetchval(
            "SELECT COUNT(*) FROM pending_transfer_stock WHERE transfer_out_id = $1 AND status = 'In Transit'",
            transfer_id) or 0
        await conn.execute(
            "UPDATE interunit_transfers_header SET unallocated_boxes = $1 WHERE id = $2",
            max(int(box_count) - int(parked), 0), transfer_id,
        )


async def _request_status(conn, request_id: int, lines: list, boxes: list) -> str:
    """Dispatch / Partial of a transfer from a request: Dispatch once its real boxes
    (box_id + transaction_no, not 'DIRECT') reach what the request asks for, counted as
    R1 counts it (_line_boxes: a line with no quantity, or one below 1, asks for none —
    its stored qty of 1 would leave the transfer Partial for good, as R1 refuses that line
    every box). Once the request has been deleted, what the transfer's own `lines` order:
    the lines an edit is then held to (_dispatch_terms). `lines` and `boxes` are the
    transfer's rows (qty; box_id, transaction_no). Read only."""
    asked = await conn.fetch(
        "SELECT qty FROM interunit_transfer_request_lines WHERE request_id = $1", request_id)
    if asked:
        ordered = sum(max(int(r["qty"]), 0) if r["qty"] else 0 for r in asked)
    else:
        ordered = sum(int(l["qty"] or 0) for l in lines)
    shipped = sum(1 for b in boxes if lookup_service._box_key(b["box_id"], b["transaction_no"]))
    return "Dispatch" if shipped >= ordered else "Partial"


async def in_transit_status(conn, transfer_id: int) -> str:
    """The status a transfer goes back to once its receipt is undone and its boxes are in
    transit again (receive_service.reopen_transfer_in, delete_service.delete_transfer_in):
    one from a request is Dispatch / Partial by its boxes, as every save of it leaves it
    (_request_status); one without a request, or without a box row, is 'Dispatch', as it
    always has been. Read only."""
    h = await conn.fetchrow("SELECT request_id FROM interunit_transfers_header WHERE id = $1", transfer_id)
    if h is None or h["request_id"] is None:
        return "Dispatch"
    boxes = await conn.fetch(
        "SELECT box_id, transaction_no FROM interunit_transfer_boxes WHERE header_id = $1", transfer_id)
    if not boxes:
        return "Dispatch"
    lines = await conn.fetch("SELECT qty FROM interunit_transfers_lines WHERE header_id = $1", transfer_id)
    return await _request_status(conn, h["request_id"], lines, boxes)


class _Terms(NamedTuple):
    """What a transfer's header and lines are written with (see _dispatch_terms), and the
    request they come from as refusals name it (its request_no; None without a request)."""
    from_site: str
    to_site: str
    lines: list
    remark: str | None
    reason_code: str | None
    request_no: str | None = None


def _request_line_to_transfer_line(r: dict) -> schemas.TransferLineCreate:
    """A request line (or a transfer's own stored line) as the transfer line it is
    dispatched as. Numbers come back from the database as int / Decimal and go in as
    text, as the form sends them; qty is a whole count. A request has no batch or
    vakkal, so those are empty."""
    def txt(v):
        return None if v is None else str(v)
    return schemas.TransferLineCreate(
        material_type=r["rm_pm_fg_type"] or "", item_category=r["item_category"] or "",
        sub_category=r["sub_category"] or "", item_description=r["item_desc_raw"] or "",
        quantity=txt(int(r["qty"])) if r["qty"] is not None else None, uom=r["uom"] or "",
        pack_size=txt(r["pack_size"]), unit_pack_size=txt(r["unit_pack_size"]),
        net_weight=txt(r["net_weight"]), total_weight=txt(r["total_weight"]),
        batch_number=r.get("batch_number") or "", lot_number=r["lot_number"] or "",
        vakkal=r.get("vakkal") or "",
    )


def _same_leg(a, b) -> bool:
    """One leg of a route against another, compared as sites (sites.same_site: 'A185' is
    'A-185' and 'Warehouse A185', 'D-39' is 'Savla D-39'); two blank legs agree too."""
    return sites.same_site(a, b) or not (sites.site_key(a) or sites.site_key(b))


def _same_route(h: schemas.TransferHeaderCreate, from_site, to_site) -> bool:
    """The header's route is from_site -> to_site, each leg compared as a site (_same_leg)."""
    return _same_leg(h.from_warehouse, from_site) and _same_leg(h.to_warehouse, to_site)


def _route(from_site, to_site) -> str:
    """A route as a refusal names it: its sites by canonical name."""
    return f"{sites.canonical_site(from_site) or '—'} -> {sites.canonical_site(to_site) or '—'}"


def _written_route(from_site, to_site, saved=None) -> tuple[str, str]:
    """The route a save writes on the header: each leg by its canonical name
    (sites.canonical_site), as every new transfer is written; on an edit (`saved`, the
    header row) a leg that still names the saved leg's site keeps the saved spelling, so
    an edit rewrites no history."""
    def leg(new, old):
        return old if old is not None and _same_leg(new, old) else sites.canonical_site(new)
    if saved is None:
        return sites.canonical_site(from_site), sites.canonical_site(to_site)
    return leg(from_site, saved["from_site"]), leg(to_site, saved["to_site"])


async def _dispatch_terms(conn, request_id: int | None, h: schemas.TransferHeaderCreate,
                          lines_in: list, saved=None) -> _Terms:
    """The route, lines, remark and reason_code a transfer is written with.

    Without a request they are the client's, a blank reason_code stored as NULL (the
    reason is optional). With one, everything but the boxes is the request's as the
    inventory manager approved it:
      - the route must be the request's (compared as sites, _same_route, so A185 ==
        A-185 == Warehouse A185), else 409 naming the request's route by canonical
        names; the request's sites are the route (written by canonical name, _create);
      - the lines are rebuilt from the request's lines, in the order they were raised;
        the client's lines are ignored;
      - remark is the request's reason text (its reason description) and reason_code is
        NULL; the client's values for both are ignored.
    `saved` is the transfer's header row on an edit (id, from_site, to_site, remark). A
    request can be deleted after its dispatch (delete_request has no status guard); the
    edit then stays locked to what the transfer dispatched: its stored route (409 on any
    other), its stored lines, its stored remark and no reason_code; refusals then name the
    request by its id. Without `saved` a missing request is 404.
    Nothing is written here, so a refusal leaves the transfer as it was. The request's
    status is the caller's business (create checks it; an edit does not need to)."""
    if request_id is None:
        return _Terms(h.from_warehouse, h.to_warehouse, list(lines_in), h.remark,
                      (h.reason_code or "").strip() or None)

    req = await conn.fetchrow(
        "SELECT request_no, from_site, to_site, reason_code FROM interunit_transfer_requests WHERE id = $1",
        request_id,
    )
    if not req and saved is not None:
        if not _same_route(h, saved["from_site"], saved["to_site"]):
            raise HTTPException(
                409,
                f"This transfer was dispatched for {_route(saved['from_site'], saved['to_site'])}; "
                "an edit has to keep the same route.",
            )
        stored = await conn.fetch(
            """
            SELECT id, rm_pm_fg_type, item_category, sub_category, item_desc_raw, qty, uom,
                   pack_size, unit_pack_size, net_weight, total_weight, batch_number,
                   lot_number, vakkal
            FROM interunit_transfers_lines
            WHERE header_id = $1
            ORDER BY id
            """,
            saved["id"],
        )
        return _Terms(saved["from_site"], saved["to_site"],
                      [_request_line_to_transfer_line(dict(r)) for r in stored],
                      saved["remark"] or "", None, str(request_id))
    if not req:
        raise HTTPException(404, "Request not found")
    if not _same_route(h, req["from_site"], req["to_site"]):
        raise HTTPException(
            409,
            f"This request was approved for {_route(req['from_site'], req['to_site'])}; "
            "a dispatch from it has to use the same route.",
        )
    line_rows = await conn.fetch(
        """
        SELECT id, rm_pm_fg_type, item_category, sub_category, item_desc_raw, qty, uom,
               pack_size, unit_pack_size, net_weight, total_weight, lot_number
        FROM interunit_transfer_request_lines
        WHERE request_id = $1
        ORDER BY id
        """,
        request_id,
    )
    if not line_rows:
        raise HTTPException(409, f"Request {req['request_no'] or request_id} has no items to dispatch")
    return _Terms(req["from_site"], req["to_site"],
                  [_request_line_to_transfer_line(dict(r)) for r in line_rows],
                  req["reason_code"] or "", None, req["request_no"] or str(request_id))


async def _create(conn, h: schemas.TransferHeaderCreate, lines_in: list, boxes_in: list | None,
                  request_id: int | None, created_by: str) -> int:
    """create_transfer's work, inside the caller's transaction; returns the new transfer's
    id. The first scan of a request on the scan screen (scan_service) is this too, with
    that one box: whatever is refused here is refused there, and what is written is the
    same."""
    challan_no = h.challan_no or _gen_challan_no()
    stock_trf_date = _date(h.stock_trf_date) or datetime.now().date()

    # ── Originating request must be Approved. Checked before anything is written,
    #     with the request row locked until commit so two dispatches can't both
    #     pass on the same request. ──
    if request_id is not None:
        req = await conn.fetchrow(
            "SELECT status FROM interunit_transfer_requests WHERE id = $1 FOR UPDATE",
            request_id,
        )
        if not req:
            raise HTTPException(404, "Request not found")
        if req["status"] != "Approved":
            raise HTTPException(409, _not_approved(req["status"]))

    # ── Route / lines / reason: the request's as approved, when there is one. ──
    t = await _dispatch_terms(conn, request_id, h, lines_in)
    # ── A dispatch from a request: at least one box, and only boxes it asks for,
    #     each judged and recorded by its own article and lot. ──
    boxes = boxes_in
    if request_id is not None:
        boxes = await _refuse_boxes_off_the_request(conn, t, boxes_in)
    # ── Every box at this From site: not in transit, elsewhere or written off. ──
    await _refuse_unavailable_boxes(conn, boxes, t.from_site)

    # A new transfer names its sites by their canonical names (app.core.sites).
    from_site, to_site = _written_route(t.from_site, t.to_site)

    async def _ins_header():
        return await conn.fetchrow(
            """
            INSERT INTO interunit_transfers_header
                (challan_no, stock_trf_date, from_site, to_site, vehicle_no, driver_name,
                 approved_by, remark, reason_code, status, request_id, created_by, created_ts, id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'Dispatch', $10, $11, $12, $13)
            RETURNING id, challan_no
            """,
            challan_no, stock_trf_date, from_site, to_site, h.vehicle_no,
            h.driver_name, h.approved_by, t.remark, t.reason_code, request_id,
            created_by, datetime.now(), new_short_time_id(),
        )
    header = await insert_with_pk_retry(conn, _ins_header)
    header_id = header["id"]

    await _persist_lines_boxes_and_park(
        conn, header_id=header_id, challan_no=challan_no,
        from_warehouse=from_site, to_warehouse=to_site,
        lines_in=t.lines, boxes_in=boxes, stock_trf_date=stock_trf_date,
        created_by=created_by,
    )

    # ── Originating request → Transferred (create only) ──
    if request_id:
        await conn.execute(
            "UPDATE interunit_transfer_requests SET status = 'Transferred', updated_at = $1 WHERE id = $2",
            datetime.now(), request_id,
        )
    return header_id


async def create_transfer(conn, data: schemas.TransferCreate, created_by: str) -> dict:
    async with conn.transaction():
        header_id = await _create(conn, data.header, data.lines, data.boxes, data.request_id, created_by)

    # Read back AFTER commit — get_transfer's best-effort enrichment queries swallow errors,
    # which inside the write txn would poison the connection and roll back the dispatch.
    return await query_service.get_transfer(conn, header_id)


async def refuse_received(conn, t, tail: str) -> None:
    """409 once transfer `t` (its header row: id, challan_no, status, to_site) is no
    longer in transit: a status that is not an in-transit one
    (lookup_service.is_in_transit_status: Dispatch / Partial, or IMS's older In Transit /
    Completed), or any receipt of it, a pending one too, in either ledger (a GRN, or a
    cold receipt IMS's cold receive began). `tail` ends the refusal ("it can't be changed
    here." / "it can't be edited."). Read only; callers hold the header row locked (FOR
    UPDATE), which a receipt takes before it begins (receive_service), so a receipt and
    the change never cross."""
    received = not lookup_service.is_in_transit_status(t["status"])
    for table in ("interunit_transfer_in_header", "cold_transfer_in_headers"):
        if received:
            break
        received = await _table_exists(conn, table) and bool(await conn.fetchval(
            f"SELECT COUNT(*) FROM {table} WHERE transfer_out_id = $1", t["id"]))
    if received:
        raise HTTPException(
            409, f"Transfer {t['challan_no']} has already been received at "
                 f"{sites.canonical_site(t['to_site']) or '—'}; {tail}")


async def update_transfer(conn, transfer_id: int, data: schemas.TransferCreate,
                          user: AuthUser | None = None) -> dict:
    """Edit an existing transfer-OUT (doc 08, ?editId). Drops the prior pending rows,
    replaces lines+boxes, re-parks, and stamps edited_at — record only, no stock is put
    back or taken. A box the edit keeps keeps the stock-row copy its dispatch made, and
    the time it joined the transfer (one that went on a newer transfer since joins it
    again now); a box it adds is copied from its stock row now. A
    box that cannot be sent from the From site is 409 (_refuse_unavailable_boxes, judged
    without this transfer, so its own boxes pass unless another transfer has them
    since). challan_no / created_by / created_ts are preserved. Only a transfer in
    transit is edited: its header row is locked (FOR UPDATE, as a receipt locks it before
    it begins) and a received one, or one with a receipt begun in either ledger, is 409
    (refuse_received), before anything is judged or written.

    request_id is never changed by an edit (the client's is ignored). A transfer from a
    request stays locked to it as create left it: same route (409 otherwise), lines
    rebuilt from the request, remark from the request, no reason_code; the boxes, the
    date and the vehicle / driver / approver details can change, but it keeps at least
    one box and only boxes the request asks for (_refuse_boxes_off_the_request; a box it
    keeps judged by its saved article and lot, one it adds by its stock record). If that
    request has been deleted since, the transfer stays locked to its own stored route,
    lines and remark, and its boxes are matched to those lines. Either way only its boxes
    are in transit: a line no box covers gets no line rows (_settle), and Dispatch /
    Partial counts its boxes against what the request asks for (_request_status).
    Its request row is locked first (_LOCK_REQUEST_OF), as the scan screen's saves of
    the same transfer lock it, so an edit and those saves never interleave. The route is
    written as _written_route writes it: a leg naming the stored site keeps its stored
    spelling, a new one is written by canonical name.

    With `user` (the router's caller), under the same header lock and before anything is
    judged: the user must be able to dispatch from the transfer's STORED From site, not
    only the one sent (permissions.assert_can_dispatch_from, 403), and a transfer
    without a request keeps its route unless an admin edits it (409)."""
    read = """
        SELECT id, challan_no, status, created_by, request_id, from_site, to_site, remark
        FROM interunit_transfers_header WHERE id = $1
        """
    existing = await conn.fetchrow(read, transfer_id)
    if not existing:
        raise HTTPException(404, "Transfer not found")

    h = data.header
    stock_trf_date = _date(h.stock_trf_date) or datetime.now().date()
    dispatched_by = existing["created_by"] or "system"

    async with conn.transaction():
        if existing["request_id"] is not None:
            # Its request row first: a save on the scan screen may have just removed its
            # last box, which deletes the transfer (read again below).
            await conn.fetchrow(_LOCK_REQUEST_OF, transfer_id)
        # The header row, locked; then nothing changes it but this edit until commit.
        existing = await conn.fetchrow(read.rstrip() + " FOR UPDATE", transfer_id)
        if not existing:
            raise HTTPException(404, "Transfer not found")
        await refuse_received(conn, existing, "it can't be edited.")
        if user is not None:
            permissions.assert_can_dispatch_from(user, existing["from_site"])
            if existing["request_id"] is None and not user.is_admin and not _same_route(
                    h, existing["from_site"], existing["to_site"]):
                raise HTTPException(
                    409, f"This transfer was dispatched for {_route(existing['from_site'], existing['to_site'])}; "
                         "only an admin can change its route.")
        # Before anything is written: a route other than the request's is refused here,
        # so is a request transfer without a box or with a box the request does not ask
        # for, and so is a box that cannot be sent from the From site.
        t = await _dispatch_terms(conn, existing["request_id"], h, data.lines, saved=existing)
        boxes = data.boxes
        if existing["request_id"] is not None:
            boxes = await _refuse_boxes_off_the_request(conn, t, data.boxes, transfer_id=transfer_id)
        await _refuse_unavailable_boxes(conn, boxes, t.from_site, transfer_id=transfer_id)
        from_site, to_site = _written_route(t.from_site, t.to_site, saved=existing)

        # Drop the prior pending rows before re-parking (replace lines + boxes). A box the
        # edit keeps is re-parked from its own row (`prior`), the copy its dispatch made:
        # the stock row may have changed or gone since (a transfer dispatched before
        # record only had it deleted), so it is not copied again. It keeps the time it
        # joined the transfer (`joined`), which orders its history (lookup_service) —
        # unless it went on a newer transfer since (lookup_service.sent_on_since: its
        # receipt here undone after it was sent on and came back, which the box guard
        # let pass from this From site). Such a box joins the transfer again now, so R3
        # reads this transfer as its latest, as the edit parks it in transit here.
        prior = await reversal_service.in_transit_rows(conn, transfer_id)
        joined = {}
        for r in await conn.fetch("SELECT * FROM interunit_transfer_boxes WHERE header_id = $1", transfer_id):
            key = lookup_service._box_key(r["box_id"], r["transaction_no"])
            if key and r.get("created_at") is not None:
                joined.setdefault(key, r["created_at"])
        for key in await lookup_service.sent_on_since(conn, list(joined), transfer_id):
            joined.pop(key, None)
        await reversal_service.restore_to_source(conn, transfer_id)

        await conn.execute(
            """
            UPDATE interunit_transfers_header
            SET stock_trf_date = $1, from_site = $2, to_site = $3, vehicle_no = $4,
                driver_name = $5, approved_by = $6, remark = $7, reason_code = $8
            WHERE id = $9
            """,
            stock_trf_date, from_site, to_site, h.vehicle_no, h.driver_name,
            h.approved_by, t.remark, t.reason_code, transfer_id,
        )
        await conn.execute("DELETE FROM interunit_transfer_boxes WHERE header_id = $1", transfer_id)
        await conn.execute("DELETE FROM interunit_transfers_lines WHERE header_id = $1", transfer_id)

        await _persist_lines_boxes_and_park(
            conn, header_id=transfer_id, challan_no=existing["challan_no"],
            from_warehouse=from_site, to_warehouse=to_site,
            lines_in=t.lines, boxes_in=boxes, stock_trf_date=stock_trf_date,
            created_by=dispatched_by, prior=prior, joined=joined,
        )

        # Genuine edit marker — edited_at is written ONLY here, so the pending list's
        # "Edited" badge is honest (updated_ts moves on routine churn).
        await conn.execute(
            "UPDATE interunit_transfers_header SET edited_at = $1 WHERE id = $2",
            datetime.now(), transfer_id)

    return await query_service.get_transfer(conn, transfer_id)
