"""Boxes store sends for a floor requisition — Stores → Production Indents → Scan.

A box joins a request (floor_requisition_box, app/db/113) one of two ways:

  * scanned — it already carries a sticker. It is looked up in the same tables as
    the job card's Raw Material scanner: sfg_box by carton_id, po_box (with
    po_line for the article) by box_id — and by transaction_no when the label
    carries one — then the universal identify over the legacy warehouse / cold
    tables (production/services/box_identify_service.py). A label's tx decides
    the order: a PO label ({"tx","bi"}) is tried against po_box first.
  * printed — it has no sticker. Manual print mints it in sfg_box as item_type
    'rm' (id "<8-digit time>-<per-job-card counter>", as create_wip_boxes does)
    and the browser prints Material-In's sticker, QR {"tx": request no, "bi": id}.

The floor still scans these boxes into the job card (jc_box_scan), so RM issued
is counted there, once; jc_box_scan is only read here, to refuse removing a
printed box the floor has already used.

A box goes out on ONE request: a scan looks the box up in floor_requisition_box
across every request FIRST, before any box table, and refuses one already there;
box_code is unique there (migration 114), so two people scanning the same box at
once cannot both record it.

Boxes change only while the request is 'issued'. Refusals are RequisitionError;
a place outside the caller's grants is place_scope's 403 (via _load).

Scanning must NOT run inside a transaction: the identify step can fail a
statement on schema drift, which would poison it (box_scan_service says the
same). Its one write is a single guarded INSERT. Print and remove must run
inside the caller's transaction.
"""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.floor_requisition import rules
from app.modules.floor_requisition.services.requisition_service import RequisitionError, _load
from app.modules.production.services.box_identify_service import identify_box

MAX_PRINT = 500
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 10
STOCK_TYPES = ("Fresh Stock", "Off Grade/Rejection")
UNKNOWN_ARTICLE = "Unknown article"
_TEXT_LIMIT = 500
_LOT_LIMIT = 100

BOX_COLS = """requisition_id, box_code, source, box_table, box_number, transaction_no, article,
              stock_type, lot_number, net_weight, gross_weight, count, recorded_by, recorded_at"""

# Newest first. The page and find queries must share this order, or "find" would
# open the wrong page.
_ORDER = "recorded_at DESC, box_number DESC NULLS LAST, box_code"

_PAGE_SQL = f"""
    SELECT {BOX_COLS} FROM floor_requisition_box
     WHERE requisition_id = $1
     ORDER BY {_ORDER}
     LIMIT $2 OFFSET $3
"""

# The whole request, whatever page is shown: the header totals and the next free
# "Box #" for Manual print.
_SUMMARY_SQL = """
    SELECT count(*) AS boxes,
           COALESCE(sum(net_weight), 0) AS net_weight,
           COALESCE(sum(gross_weight), 0) AS gross_weight,
           COALESCE(sum(count), 0) AS count,
           COALESCE(max(box_number), 0) AS last_box_number
      FROM floor_requisition_box
     WHERE requisition_id = $1
"""

_BY_ARTICLE_SQL = """
    SELECT article, COALESCE(sum(net_weight), 0) AS net_weight, count(*) AS boxes
      FROM floor_requisition_box
     WHERE requisition_id = $1
     GROUP BY article
     ORDER BY 2 DESC, article
"""

# A box's 1-based place in the list. An exact box id wins over a sticker "Box #"
# (a bare number can be either).
_FIND_SQL = f"""
    SELECT box_code, pos FROM (
        SELECT box_code, box_number, row_number() OVER (ORDER BY {_ORDER}) AS pos
          FROM floor_requisition_box
         WHERE requisition_id = $1
    ) ranked
     WHERE upper(box_code) = upper($2) OR ($3::int IS NOT NULL AND box_number = $3)
     ORDER BY (upper(box_code) = upper($2)) DESC, pos
     LIMIT 1
"""

_SFG_SQL = """
    SELECT carton_id, fg_sku_name, sfg_code, batch_code, net_weight, gross_weight, units, status
      FROM sfg_box WHERE carton_id = $1
"""

# LIMIT 2: box_id is in no unique key, so without the label's transaction the same
# id on two purchase orders is ambiguous, not "the first one".
_PO_SQL = """
    SELECT b.box_id, b.transaction_no, b.lot_number, b.net_weight, b.gross_weight, b.count,
           l.sku_name
      FROM po_box b
      LEFT JOIN po_line l ON l.transaction_no = b.transaction_no AND l.line_number = b.line_number
     WHERE b.box_id = $1 AND ($2::text IS NULL OR b.transaction_no = $2)
     ORDER BY b.transaction_no
     LIMIT 2
"""

# Guarded twice: only while the request is still issued, and a box already on any
# request (a concurrent scan: this request's key, or box_code's unique index from
# migration 114) inserts nothing rather than failing.
_INSERT_SCANNED_SQL = f"""
    INSERT INTO floor_requisition_box
           (requisition_id, box_code, source, box_table, transaction_no, article, lot_number,
            net_weight, gross_weight, count, recorded_by)
    SELECT $1, $2, 'scanned', $3, $4, $5, $6, $7, $8, $9, $10
     WHERE EXISTS (SELECT 1 FROM floor_requisition WHERE requisition_id = $1 AND status = 'issued')
    ON CONFLICT DO NOTHING
    RETURNING {BOX_COLS}
"""


# Which request a box already went out on — across every request.
_WHERE_SENT_SQL = """
    SELECT b.requisition_id, fr.job_card_id, jc.job_card_number
      FROM floor_requisition_box b
      JOIN floor_requisition fr ON fr.requisition_id = b.requisition_id
      LEFT JOIN job_card_v2 jc ON jc.job_card_id = fr.job_card_id
     WHERE b.box_code = $1
     LIMIT 1
"""


def _key(name: Optional[str]) -> str:
    return " ".join((name or "").split()).upper()


def _float(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def _int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def box_out(row, requested_article: str) -> dict[str, Any]:
    out = dict(row)
    out["net_weight"] = _float(out["net_weight"])
    out["gross_weight"] = _float(out["gross_weight"])
    out["recorded_at"] = out["recorded_at"].isoformat() if out.get("recorded_at") else None
    out["article_mismatch"] = _key(out["article"]) != _key(requested_article)
    return out


def parse_label(raw: Optional[str]) -> tuple[str, Optional[str]]:
    """(box id, transaction no) from a scanned QR: Material-In's JSON {"tx","bi"},
    or a bare box id. An empty box id means the QR carries none."""
    text = (raw or "").strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text, None
    if isinstance(parsed, dict) and isinstance(parsed.get("bi"), str):
        tx = parsed.get("tx")
        tx_text = str(tx).strip() if tx is not None else ""
        return parsed["bi"].strip(), tx_text or None
    return text, None


def _not_issued(requisition_id: int, status: Optional[str]) -> RequisitionError:
    return RequisitionError(409, "not_issued",
                            f"Request {requisition_id} is {status}. Boxes can be changed only while it is issued.",
                            requisition_id=requisition_id, status=status)


def _duplicate(requisition_id: int, code: str, sent) -> RequisitionError:
    """`sent` is the _WHERE_SENT_SQL row, or None when the box that beat this scan
    to the insert has since been removed again."""
    if sent is None:
        return RequisitionError(409, "duplicate_box",
                                f"Box {code} was just recorded by someone else. Scan it again to see where.",
                                requisition_id=requisition_id, box_code=code)
    other = sent["requisition_id"]
    if other == requisition_id:
        return RequisitionError(409, "duplicate_box", f"Box {code} is already on this request.",
                                requisition_id=requisition_id, box_code=code)
    job_card = sent["job_card_number"] or sent["job_card_id"]
    return RequisitionError(409, "duplicate_box",
                            f"Box {code} was already sent on request #{other} (job card {job_card}).",
                            requisition_id=requisition_id, box_code=code, sent_on_requisition_id=other)


def _ambiguous(code: str, tables: list) -> RequisitionError:
    return RequisitionError(409, "ambiguous_box",
                            f"Box {code} matches more than one box. Scan the sticker's QR that "
                            "carries its transaction number.",
                            box_code=code, tables=tables)


async def _load_issued(conn, user, requisition_id: int, *, lock: bool = False):
    """The request, refused unless it is issued. `lock` holds the row for the
    caller's transaction so it cannot move on mid-write."""
    row = await _load(conn, user, requisition_id)
    status = row["status"]
    if lock:
        status = await conn.fetchval(
            "SELECT status FROM floor_requisition WHERE requisition_id = $1 FOR UPDATE", requisition_id)
    if status != "issued":
        raise _not_issued(requisition_id, status)
    return row


async def _from_sfg(conn, code: str, tx: Optional[str]) -> Optional[dict[str, Any]]:
    row = await conn.fetchrow(_SFG_SQL, code)
    if row is None:
        return None
    if row["status"] == "CANCELLED":
        raise RequisitionError(409, "box_cancelled", f"Box {code} was cancelled, so it can't be sent.",
                               box_code=code)
    return {"box_code": code, "box_table": "sfg_box", "transaction_no": None,
            "article": row["fg_sku_name"] or row["sfg_code"], "lot_number": row["batch_code"],
            "net_weight": row["net_weight"], "gross_weight": row["gross_weight"], "count": row["units"]}


async def _from_po(conn, code: str, tx: Optional[str]) -> Optional[dict[str, Any]]:
    rows = await conn.fetch(_PO_SQL, code, tx)
    if not rows:
        return None
    if len(rows) > 1:
        raise _ambiguous(code, ["po_box"])
    r = rows[0]
    return {"box_code": code, "box_table": "po_box", "transaction_no": r["transaction_no"],
            "article": r["sku_name"], "lot_number": r["lot_number"],
            "net_weight": r["net_weight"], "gross_weight": r["gross_weight"], "count": r["count"]}


async def resolve_box(conn, raw: str) -> dict[str, Any]:
    """What a scanned label is: {box_code, box_table, transaction_no, article,
    lot_number, net_weight, gross_weight, count}, or a refusal."""
    code, tx = parse_label(raw)
    if not code:
        raise RequisitionError(400, "no_box_id", "That QR has no box id.")
    for find in ((_from_po, _from_sfg) if tx else (_from_sfg, _from_po)):
        box = await find(conn, code, tx)
        if box:
            return box
    ident = await identify_box(conn, raw)
    if ident.get("ambiguous"):
        raise _ambiguous(code, [ident.get("table"), *(ident.get("also_in") or [])])
    if ident.get("found"):
        b = ident.get("box") or {}
        return {"box_code": code, "box_table": ident.get("table") or "unknown",
                "transaction_no": b.get("transaction_no"), "article": b.get("item_description"),
                "lot_number": b.get("lot_number"), "net_weight": b.get("net_weight"),
                "gross_weight": b.get("gross_weight"), "count": b.get("count")}
    raise RequisitionError(404, "box_not_found",
                           f"Box {code} isn't in any box table. Print a sticker for it under Manual print.",
                           box_code=code)


def _find_terms(find: Optional[str]) -> Optional[tuple[str, Optional[int]]]:
    """(box id, sticker Box #) to look for: a pasted or scanned sticker gives its
    box id; a short plain number may also be a Box #. None when blank."""
    code, _ = parse_label(find)
    # PostgreSQL text cannot hold a NUL, so no box id has one: a miss, not a 500.
    if not code or chr(0) in code:
        return None
    # ASCII only: str.isdigit() also accepts "\u00b2" or "\u2460", which int() rejects.
    number = int(code) if code.isascii() and code.isdigit() and len(code) <= 9 else None
    return code, number


async def list_boxes(conn, user, requisition_id: int, *, page: int = 1,
                     page_size: int = DEFAULT_PAGE_SIZE, find: Optional[str] = None) -> dict[str, Any]:
    """One page of the request's boxes, newest first, with figures for the whole
    request. `find` opens the page holding that box (`found` names it) and leaves
    the asked page when no box matches (`found` is None)."""
    req = await _load(conn, user, requisition_id)
    size = min(max(int(page_size), 1), MAX_PAGE_SIZE)
    summary = await conn.fetchrow(_SUMMARY_SQL, requisition_id)
    total = int(summary["boxes"])
    pages = max(1, math.ceil(total / size))
    page = max(int(page), 1)

    found = None
    terms = _find_terms(find)
    if terms:
        hit = await conn.fetchrow(_FIND_SQL, requisition_id, *terms)
        if hit:
            found = hit["box_code"]
            page = math.ceil(int(hit["pos"]) / size)
    page = min(page, pages)

    rows = await conn.fetch(_PAGE_SQL, requisition_id, size, (page - 1) * size)
    by_article = await conn.fetch(_BY_ARTICLE_SQL, requisition_id)
    return {
        "requisition_id": requisition_id,
        "status": req["status"],
        "boxes": [box_out(r, req["material_sku_name"]) for r in rows],
        "page": page,
        "page_size": size,
        "total": total,
        "pages": pages,
        "totals": {"boxes": total, "net_weight": round(float(summary["net_weight"]), 3),
                   "gross_weight": round(float(summary["gross_weight"]), 3), "count": int(summary["count"])},
        "by_article": [{"article": a["article"], "net_weight": round(float(a["net_weight"]), 3),
                        "boxes": int(a["boxes"])} for a in by_article],
        "next_box_number": int(summary["last_box_number"]) + 1,
        "found": found,
    }


async def scan_box(conn, user, requisition_id: int, *, code: str) -> dict[str, Any]:
    req = await _load_issued(conn, user, requisition_id)
    box_code, _ = parse_label(code)
    # floor_requisition_box first, across every request: a box already sent is
    # refused before any box table is read.
    if box_code:
        sent = await conn.fetchrow(_WHERE_SENT_SQL, box_code)
        if sent:
            raise _duplicate(requisition_id, box_code, sent)
    box = await resolve_box(conn, code)
    article = (box["article"] or "").strip()[:_TEXT_LIMIT] or UNKNOWN_ARTICLE
    row = await conn.fetchrow(
        _INSERT_SCANNED_SQL, requisition_id, box["box_code"], box["box_table"], box["transaction_no"],
        article, box["lot_number"], box["net_weight"], box["gross_weight"], _int(box["count"]),
        rules.actor_name(user))
    if row is None:
        status = await conn.fetchval("SELECT status FROM floor_requisition WHERE requisition_id = $1",
                                     requisition_id)
        if status != "issued":
            raise _not_issued(requisition_id, status)
        # Someone recorded the same box between the check above and this insert.
        raise _duplicate(requisition_id, box["box_code"],
                         await conn.fetchrow(_WHERE_SENT_SQL, box["box_code"]))
    return box_out(row, req["material_sku_name"])


_COUNTER_SQL = """
    SELECT COALESCE(MAX(CAST(split_part(carton_id, '-', 2) AS INTEGER)), 0)
      FROM sfg_box
     WHERE job_card_id = $1
       AND split_part(carton_id, '-', 2) ~ '^[0-9]+$'
"""

_INSERT_SFG_SQL = """
    INSERT INTO sfg_box (carton_id, item_type, job_card_id, job_card_number, sfg_code, fg_sku_name,
                         entity, floor, stage_bucket, batch_code, net_weight, gross_weight, units,
                         status, created_by)
    VALUES ($1, 'rm', $2, $3, $4, $4, $5, $6, 'Stores', $7, $8, $9, $10, 'PRINTED', $11)
    RETURNING carton_id
"""

_INSERT_PRINTED_SQL = f"""
    INSERT INTO floor_requisition_box
           (requisition_id, box_code, source, box_table, box_number, transaction_no, article,
            stock_type, lot_number, net_weight, gross_weight, count, recorded_by)
    VALUES ($1, $2, 'printed', 'sfg_box', $3, $4, $5, $6, $7, $8, $9, $10, $11)
    RETURNING {BOX_COLS}
"""


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _whole(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_print_box(box: dict[str, Any]) -> dict[str, Any]:
    """One box of a print: the browser's checks, repeated (lib/box-scan.ts)."""
    n = box.get("box_number")
    if not _whole(n) or n < 1:
        raise RequisitionError(400, "bad_box_number", "Every box needs a box number of 1 or more.")
    net, gross, count = box.get("net_weight"), box.get("gross_weight"), box.get("count")
    if not _finite(net) or net <= 0:
        raise RequisitionError(400, "bad_box", f"Box {n}: net wt must be more than 0.", box_number=n)
    if gross is not None and (not _finite(gross) or gross < net):
        raise RequisitionError(400, "bad_box", f"Box {n}: gross wt can't be less than net wt.", box_number=n)
    if count is not None and (not _whole(count) or count < 0):
        raise RequisitionError(400, "bad_box", f"Box {n}: count must be a whole number, 0 or more.",
                               box_number=n)
    lot = (box.get("lot_number") or "").strip()[:_LOT_LIMIT] or None
    return {"box_number": n, "net_weight": round(float(net), 3),
            "gross_weight": round(float(gross), 3) if gross is not None else None,
            "count": count, "lot_number": lot}


async def print_boxes(conn, user, requisition_id: int, *, article: str, stock_type: str,
                      boxes: list[dict[str, Any]]) -> dict[str, Any]:
    """Mint one sfg_box row ('rm', PRINTED) and one request row per box. The
    browser prints the stickers with the ids returned, in the order given."""
    req = await _load_issued(conn, user, requisition_id, lock=True)
    name = (article or "").strip()
    if not name:
        raise RequisitionError(400, "article_required", "Choose the article to print.")
    if len(name) > _TEXT_LIMIT:
        raise RequisitionError(400, "article_too_long", f"The article name is over {_TEXT_LIMIT} characters.")
    if stock_type not in STOCK_TYPES:
        raise RequisitionError(400, "bad_stock_type", f"Stock type must be one of: {', '.join(STOCK_TYPES)}.")
    if not 1 <= len(boxes) <= MAX_PRINT:
        raise RequisitionError(400, "bad_box_count", f"Print between 1 and {MAX_PRINT} boxes at a time.")
    checked = [check_print_box(b) for b in boxes]
    numbers = [c["box_number"] for c in checked]
    if len(set(numbers)) != len(numbers):
        raise RequisitionError(400, "duplicate_box_number", "The same box number appears twice.")
    taken = sorted(r["box_number"] for r in await conn.fetch(
        "SELECT box_number FROM floor_requisition_box WHERE requisition_id = $1 AND box_number = ANY($2::int[])",
        requisition_id, numbers))
    if taken:
        raise RequisitionError(409, "box_number_taken",
                               f"Box number {', '.join(map(str, taken))} is already on this request. "
                               "Generate the boxes again.", box_numbers=taken)

    jc = await conn.fetchrow("SELECT job_card_number, entity FROM job_card_v2 WHERE job_card_id = $1",
                             req["job_card_id"])
    last = await conn.fetchval(_COUNTER_SQL, req["job_card_id"]) or 0
    actor, txn = rules.actor_name(user), str(requisition_id)
    out = []
    for i, c in enumerate(checked, 1):
        # "<8-digit time>-<counter>", as create_wip_boxes mints; a PK clash re-rolls the time.
        async def _insert(_c=c, _counter=last + i):
            return await conn.fetchval(
                _INSERT_SFG_SQL, f"{new_short_time_id()}-{_counter}", req["job_card_id"],
                jc["job_card_number"] if jc else None, name, jc["entity"] if jc else None, req["floor"],
                _c["lot_number"], _c["net_weight"], _c["gross_weight"], _c["count"], actor)

        carton_id = await insert_with_pk_retry(conn, _insert)
        row = await conn.fetchrow(_INSERT_PRINTED_SQL, requisition_id, carton_id, c["box_number"], txn,
                                  name, stock_type, c["lot_number"], c["net_weight"], c["gross_weight"],
                                  c["count"], actor)
        out.append(box_out(row, req["material_sku_name"]))
    return {"requisition_id": requisition_id, "boxes": out}


def _in_use(code: str) -> RequisitionError:
    return RequisitionError(409, "box_in_use",
                            f"Box {code} is already in use (scanned into a job card or received), "
                            "so it can't be removed.", box_code=code)


async def _in_job_card(conn, code: str) -> bool:
    # jc_box_scan is created out-of-band and exists only on RDS; elsewhere nothing uses the box.
    if not await conn.fetchval("SELECT to_regclass('jc_box_scan') IS NOT NULL"):
        return False
    return bool(await conn.fetchval(
        "SELECT 1 FROM jc_box_scan WHERE sfg_box_id = $1 OR box_id = $1 LIMIT 1", code))


async def remove_box(conn, user, requisition_id: int, box_code: str) -> dict[str, Any]:
    """Take a box off the request. A printed box's sfg_box row goes too, so its
    sticker stops being recognised — unless the floor has already used it."""
    await _load_issued(conn, user, requisition_id, lock=True)
    code = (box_code or "").strip()
    source = await conn.fetchval(
        "SELECT source FROM floor_requisition_box WHERE requisition_id = $1 AND box_code = $2",
        requisition_id, code)
    if source is None:
        raise RequisitionError(404, "box_not_on_request", f"Box {code} isn't on request {requisition_id}.",
                               requisition_id=requisition_id, box_code=code)
    if source == "printed":
        if await _in_job_card(conn, code):
            raise _in_use(code)
        deleted = await conn.execute(
            "DELETE FROM sfg_box WHERE carton_id = $1 AND item_type = 'rm' AND status = 'PRINTED'", code)
        if deleted.split()[-1] == "0" and await conn.fetchval("SELECT 1 FROM sfg_box WHERE carton_id = $1",
                                                              code):
            raise _in_use(code)
    await conn.execute("DELETE FROM floor_requisition_box WHERE requisition_id = $1 AND box_code = $2",
                       requisition_id, code)
    return {"requisition_id": requisition_id, "box_code": code, "removed": True}
