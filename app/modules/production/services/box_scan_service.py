"""Box-scan capture — one row per scanned box, anchored to a job card.

Each scan is EITHER a PO/RM box (po_box.box_id, carries transaction_no) or an
SFG box (sfg_box.carton_id) — the jc_box_scan CHECK enforces that XOR. The client
sends only the scanned code + job_card_id (+ optional re-weighed values); the
article and batch context are resolved here from the box itself. Re-scanning a
box on the same JC upserts (latest weights win) via the (job_card_id, box_id) /
(job_card_id, sfg_box_id) partial unique indexes. Floor / entity / SKU are NOT
stored — they come from job_card_v2 through the jc_box_scan_enriched view.

jc_box_scan / jc_box_scan_enriched are created out-of-band (see docs). Row identity is
the natural key — there is no surrogate PK.

Stores' record comes first (floor_requisition_box, migration 113 — Stores ->
Production Indents -> Scan). A box Stores sent for ANOTHER job card's request is
refused before anything else is read; one sent for THIS job card is recorded with
the article / weights / count Stores recorded (values typed on the job card still
win); a box Stores never sent is resolved exactly as before. The scan and the
job card's list of scans carry Stores' reference for each box sent for THIS job
card — the request and, for a printed box, its sticker number — or None.

Manual print (print_boxes) is Stores' Manual print on the job card's Raw Material
tab: each box is minted in sfg_box as an RM box (stage_bucket 'Raw Material') and
recorded here in the same transaction, with no Stores request. The list of scans
says which boxes were printed there — their sticker "Box #" and LOT — and the next
free Box #, from the per-job-card counter Stores and the WIP boxes share.
"""
from __future__ import annotations

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.production.services.box_identify_service import identify_box


# Which request — and so which job card — Stores sent the box for, and what Stores
# recorded for it. box_code is unique there (migration 114): one request per box.
_STORES_RECORD_SQL = """
    SELECT b.requisition_id, b.box_table, b.transaction_no, b.article,
           b.net_weight, b.gross_weight, b.count, b.source, b.box_number,
           fr.job_card_id, jc.job_card_number
      FROM floor_requisition_box b
      JOIN floor_requisition fr ON fr.requisition_id = b.requisition_id
      LEFT JOIN job_card_v2 jc ON jc.job_card_id = fr.job_card_id
     WHERE b.box_code = $1
     LIMIT 1
"""

# Stores' reference for each listed box that was sent for this job card.
_STORES_REFS_SQL = """
    SELECT b.box_code, b.requisition_id, b.box_number, b.source
      FROM floor_requisition_box b
      JOIN floor_requisition fr ON fr.requisition_id = b.requisition_id
     WHERE fr.job_card_id = $1
       AND b.box_code = ANY($2::text[])
"""

# Manual print on the Raw Material tab files its sfg_box rows under this bucket
# (Stores' Manual print uses 'Stores'): it is how the list knows a box was printed here.
PRINT_STAGE_BUCKET = "Raw Material"

# Prints on one job card take turns, so two of them cannot read the same counter.
# $1 is the job card id, a number: asyncpg refuses an int for a text parameter.
_PRINT_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('jc_box_print:' || $1::bigint::text, 0))"

# The lock create_wip_boxes takes on the job card: the WIP boxes mint from the same
# counter, so a print waits for them too and neither can reuse a Box #.
_COUNTER_LOCK_SQL = "SELECT pg_advisory_xact_lock($1)"

# The Box # is the carton id's counter, which the job card's counter reads as an
# integer (int4): past it, every later read of that counter would fail.
MAX_BOX_NUMBER = 99_999_999

# Stores' _INSERT_SFG_SQL, with the job card's floor and this tab's bucket.
_INSERT_SFG_SQL = """
    INSERT INTO sfg_box (carton_id, item_type, job_card_id, job_card_number, sfg_code, fg_sku_name,
                         entity, floor, stage_bucket, batch_code, net_weight, gross_weight, units,
                         status, created_by)
    VALUES ($1, 'rm', $2, $3, $4, $4, $5, $6, $7, $8, $9, $10, $11, 'PRINTED', $12)
    RETURNING carton_id
"""

# A new carton id, so there is nothing to conflict with.
_INSERT_PRINTED_SCAN_SQL = """
    INSERT INTO jc_box_scan (job_card_id, batch_id, sfg_box_id, article, net_weight, gross_weight,
                             count, scanned_by)
    VALUES ($1, NULL, $2, $3, $4, $5, $6, $7)
"""

# Which listed boxes were printed on this job card's Raw Material tab.
_PRINTED_SQL = """
    SELECT carton_id, batch_code
      FROM sfg_box
     WHERE carton_id = ANY($1::text[])
       AND job_card_id = $2
       AND item_type = 'rm'
       AND stage_bucket = $3
"""


def _stores_print():
    """Stores' Manual print (floor_requisition box_service): its per-job-card
    counter and box checks. Imported on use, so loading this module does not load
    the floor-requisition modules."""
    from app.modules.floor_requisition.services import box_service
    return box_service


def _sticker_number(carton_id: str) -> int | None:
    """The "Box #" on a sticker printed here: the counter after the id's last '-'."""
    _, dash, counter = carton_id.rpartition("-")
    # ASCII only: str.isdigit() also accepts "\u00b2", which int() rejects.
    return int(counter) if dash and counter.isascii() and counter.isdigit() else None


async def _stores_table_exists(conn) -> bool:
    """Whether migration 113 is applied. Without it there is no Stores record to read."""
    return bool(await conn.fetchval("SELECT to_regclass('floor_requisition_box') IS NOT NULL"))


async def _stores_record(conn, code: str) -> dict | None:
    """Stores' record of the box, or None when Stores never sent it — or when
    migration 113 is not applied, so there is no record to read."""
    if not await _stores_table_exists(conn):
        return None
    row = await conn.fetchrow(_STORES_RECORD_SQL, code)
    return dict(row) if row else None


def _stores_ref(row) -> dict:
    """What the job card shows of Stores' record: the request the box went out on
    and, for a printed box, the "Box #" on its sticker (None for a scanned one)."""
    return {"requisition_id": row["requisition_id"], "box_number": row["box_number"],
            "source": row["source"]}


def _box_from_stores(code: str, stores: dict) -> dict:
    """A box Stores recorded but no box table holds: Stores' record is enough. A
    manually printed box lives in sfg_box, so it goes to the sfg side."""
    is_sfg = stores["box_table"] == "sfg_box"
    return {
        "source_type":    "sfg" if is_sfg else "po",
        "sfg_box_id":     code if is_sfg else None,
        "transaction_no": None if is_sfg else stores["transaction_no"],
        "box_id":         None if is_sfg else code,
        "batch_id":       None,
        "article":        stores["article"],
        "net_weight":     stores["net_weight"],
        "gross_weight":   stores["gross_weight"],
        "count":          stores["count"],
    }


async def _resolve_box(conn, code: str, source_hint: str | None) -> dict | None:
    """Resolve a scanned code to its source box.

    Tries SFG (sfg_box.carton_id) then PO/RM (po_box.box_id) unless `source_hint`
    pins one side. Returns a dict with the source columns + the box's stored
    article/weights, or None when the code matches no known box.
    """
    code = (code or "").strip()
    if not code:
        return None
    want_sfg = source_hint in (None, "sfg")
    want_po = source_hint in (None, "po")

    if want_sfg:
        sfg = await conn.fetchrow(
            "SELECT carton_id, fg_sku_name, sfg_code, net_weight, gross_weight, "
            "       units, batch_id FROM sfg_box WHERE carton_id = $1",
            code,
        )
        if sfg:
            return {
                "source_type": "sfg",
                "sfg_box_id": sfg["carton_id"],
                "transaction_no": None,
                "box_id": None,
                "batch_id": sfg["batch_id"],
                "article": sfg["fg_sku_name"] or sfg["sfg_code"],
                "net_weight": sfg["net_weight"],
                "gross_weight": sfg["gross_weight"],
                "count": sfg["units"],
            }

    if want_po:
        po = await conn.fetchrow(
            "SELECT b.box_id, b.transaction_no, b.net_weight, b.gross_weight, "
            "       b.count, l.sku_name "
            "FROM   po_box b "
            "LEFT   JOIN po_line l ON l.transaction_no = b.transaction_no "
            "                     AND l.line_number    = b.line_number "
            "WHERE  b.box_id = $1",
            code,
        )
        if po:
            return {
                "source_type": "po",
                "sfg_box_id": None,
                "transaction_no": po["transaction_no"],
                "box_id": po["box_id"],
                "batch_id": None,
                "article": po["sku_name"],
                "net_weight": po["net_weight"],
                "gross_weight": po["gross_weight"],
                "count": po["count"],
            }

    return None


async def scan_box(conn, *, job_card_id: int, code: str,
                   source_type: str | None = None,
                   article: str | None = None,
                   net_weight=None, gross_weight=None, count=None,
                   scanned_by: str | None = None) -> dict:
    """Upsert a box scan against a job card.

    The scanned code is resolved WIDELY: first po_box / sfg_box (which also
    carries the SFG batch), then — via the universal box-identify — the legacy
    warehouse / cold tables. When it resolves anywhere, the article + weights
    are auto-filled (a client-supplied value still wins). When it resolves
    NOWHERE, the box is still stored using the operator-entered ``article``
    (required) + weights/count, so any physical box can be recorded.

    Stores' record (floor_requisition_box) is read before all of that. The
    returned scan carries ``stores``: Stores' reference when the box was sent for
    this job card, else None.

    Resolution runs in AUTOCOMMIT on purpose. identify_box no longer swallows
    errors — it plans its query from information_schema and includes only the
    tables and columns that exist — but it still catches schema drift under a
    cached plan, and any failed statement would poison a surrounding transaction.
    So the caller must NOT wrap this in one (the single upsert is atomic anyway).
    """
    jc = await conn.fetchval(
        "SELECT 1 FROM job_card_v2 WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}

    code = (code or "").strip()
    if not code:
        return {"error": "box_not_found", "code": code}

    # Stores' record first: a box Stores sent for another job card's request is
    # refused here, before the duplicate guard and the box tables are read.
    stores = await _stores_record(conn, code)
    if stores and stores["job_card_id"] != job_card_id:
        return {"error": "sent_for_other_job_card", "box_id": code,
                "requisition_id": stores["requisition_id"], "job_card_id": stores["job_card_id"],
                "job_card_number": stores["job_card_number"]}

    # Redundant-box guard: a physical box id must not be recorded twice. Matched
    # by the scanned id ALONE across EVERY job card (not the per-JC key), so the
    # same box can't be stored again anywhere. Remove the existing row (the ✕ in
    # the RM tab) to re-record it.
    dup = await conn.fetchval(
        "SELECT 1 FROM jc_box_scan WHERE box_id = $1 OR sfg_box_id = $1 LIMIT 1",
        code,
    )
    if dup:
        return {"error": "duplicate_box", "box_id": code}

    entered = (article or "").strip() or None

    # po_box / sfg_box first (fast, carries the SFG batch); then widen to the
    # legacy warehouse/cold tables the universal identify covers.
    box = await _resolve_box(conn, code, source_type)
    if box is None and stores:
        box = _box_from_stores(code, stores)  # Stores already resolved it; skip the wide search
    if box is None:
        ident = await identify_box(conn, code)
        if ident.get("ambiguous"):
            # The scan matched the same id in more than one table. box_id is in no
            # unique key and its 8-digit base repeats about every 27.7 hours, so
            # picking the top row would attach THIS job card to a box that may be
            # a different physical carton. Refuse and tell the operator to scan the
            # QR with its transaction number, which disambiguates.
            return {
                "error": "ambiguous_box",
                "code": code,
                "tables": [ident.get("table"), *(ident.get("also_in") or [])],
            }
        if ident.get("found"):
            b = ident.get("box") or {}
            is_sfg = ident.get("table") == "sfg_box"
            box = {
                "source_type":    "sfg" if is_sfg else "po",
                "sfg_box_id":     code if is_sfg else None,
                "transaction_no": None if is_sfg else b.get("transaction_no"),
                "box_id":         None if is_sfg else code,
                "batch_id":       None,  # identify doesn't carry a v2 batch id
                "article":        b.get("item_description"),
                "net_weight":     b.get("net_weight"),
                "gross_weight":   b.get("gross_weight"),
                "count":          b.get("count"),
            }
    if box is None:
        # Unknown everywhere — record it as a PO/RM-side box with entered detail.
        if not entered:
            return {"error": "article_required", "code": code}
        box = {"source_type": "po", "sfg_box_id": None, "transaction_no": None,
               "box_id": code, "batch_id": None, "article": entered,
               "net_weight": None, "gross_weight": None, "count": None}

    if stores:
        # Sent for this job card: what Stores recorded is what went out. A figure
        # Stores left blank keeps the box table's value.
        box = {**box, **{k: stores[k] for k in ("article", "net_weight", "gross_weight", "count")
                         if stores[k] is not None}}

    final_article = entered or box.get("article")
    if not final_article:
        return {"error": "article_required", "code": code}

    # Client-supplied values win; fall back to the resolved box's own values.
    nw = net_weight   if net_weight   is not None else box["net_weight"]
    gw = gross_weight if gross_weight is not None else box["gross_weight"]
    cnt = count       if count        is not None else box["count"]
    if cnt is not None:
        try:
            cnt = int(float(cnt))  # identify may hand back a string count
        except (TypeError, ValueError):
            cnt = None

    if box["source_type"] == "sfg":
        row = await conn.fetchrow(
            """
            INSERT INTO jc_box_scan (job_card_id, batch_id, sfg_box_id,
                                  article, net_weight, gross_weight, count, scanned_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (job_card_id, sfg_box_id) WHERE sfg_box_id IS NOT NULL
            DO UPDATE SET article      = EXCLUDED.article,
                          net_weight   = EXCLUDED.net_weight,
                          gross_weight = EXCLUDED.gross_weight,
                          count        = EXCLUDED.count,
                          batch_id     = EXCLUDED.batch_id,
                          scanned_by   = EXCLUDED.scanned_by,
                          scanned_at   = now()
            RETURNING *
            """,
            job_card_id, box["batch_id"], box["sfg_box_id"],
            final_article, nw, gw, cnt, scanned_by,
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO jc_box_scan (job_card_id, batch_id, transaction_no, box_id,
                                  article, net_weight, gross_weight, count, scanned_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (job_card_id, box_id) WHERE box_id IS NOT NULL
            DO UPDATE SET article      = EXCLUDED.article,
                          net_weight   = EXCLUDED.net_weight,
                          gross_weight = EXCLUDED.gross_weight,
                          count        = EXCLUDED.count,
                          batch_id     = EXCLUDED.batch_id,
                          scanned_by   = EXCLUDED.scanned_by,
                          scanned_at   = now()
            RETURNING *
            """,
            job_card_id, box["batch_id"], box["transaction_no"], box["box_id"],
            final_article, nw, gw, cnt, scanned_by,
        )
    scan = dict(row)
    # A box sent for another job card was refused above, so `stores` is this one's.
    scan["stores"] = _stores_ref(stores) if stores else None
    return {"scanned": True, "scan": scan}


async def print_boxes(conn, *, job_card_id: int, article: str, boxes: list[dict],
                      actor: str | None) -> dict:
    """Manual print on the Raw Material tab: Stores' Manual print, recorded on this
    job card. Each box is minted in sfg_box ('rm', PRINTED, stage_bucket 'Raw
    Material') and scanned into jc_box_scan, in the order given — no Stores
    request. Its sticker "Box #" is the counter in its id, so a number at or below
    the job card's last one is refused, and so is one past MAX_BOX_NUMBER. The
    browser prints the stickers with the ids returned.

    The checks are Stores' (check_print_box); refusals are RequisitionError. Must
    run inside the caller's transaction: insert_with_pk_retry uses savepoints, and
    no box may reach sfg_box without its scan.
    """
    from app.modules.floor_requisition.services.requisition_service import RequisitionError
    stores = _stores_print()

    jc = await conn.fetchrow(
        "SELECT job_card_number, entity, floor FROM job_card_v2 "
        "WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
    if not jc:
        raise RequisitionError(404, "job_card_not_found", "Job card not found.")
    name = (article or "").strip()
    if not name:
        raise RequisitionError(400, "article_required", "Choose the article to print.")
    if len(name) > stores._TEXT_LIMIT:
        raise RequisitionError(400, "article_too_long",
                               f"The article name is over {stores._TEXT_LIMIT} characters.")
    if not 1 <= len(boxes) <= stores.MAX_PRINT:
        raise RequisitionError(400, "bad_box_count", f"Print between 1 and {stores.MAX_PRINT} boxes at a time.")
    checked = []
    for b in boxes:
        c = stores.check_print_box(b)
        if c["box_number"] > MAX_BOX_NUMBER:
            raise RequisitionError(400, "bad_box_number",
                                   f"Box number {c['box_number']} is too high. Box numbers go up to {MAX_BOX_NUMBER}.",
                                   box_number=c["box_number"])
        checked.append(c)
    numbers = [c["box_number"] for c in checked]
    if len(set(numbers)) != len(numbers):
        raise RequisitionError(400, "duplicate_box_number", "The same box number appears twice.")

    await conn.execute(_PRINT_LOCK_SQL, job_card_id)
    await conn.execute(_COUNTER_LOCK_SQL, job_card_id)
    last = await conn.fetchval(stores._COUNTER_SQL, job_card_id) or 0
    taken = sorted(n for n in numbers if n <= last)
    if taken:
        raise RequisitionError(409, "box_number_taken",
                               f"Box number {', '.join(map(str, taken))} is already used on this job card. "
                               "Generate the boxes again.", box_numbers=taken, next_box_number=last + 1)

    out = []
    for c in checked:
        # "<8-digit time>-<Box #>", as Stores mints; a PK clash re-rolls the time.
        async def _insert(_c=c):
            return await conn.fetchval(
                _INSERT_SFG_SQL, f"{new_short_time_id()}-{_c['box_number']}", job_card_id,
                jc["job_card_number"], name, jc["entity"], jc["floor"], PRINT_STAGE_BUCKET,
                _c["lot_number"], _c["net_weight"], _c["gross_weight"], _c["count"], actor)

        carton_id = await insert_with_pk_retry(conn, _insert)
        await conn.execute(_INSERT_PRINTED_SCAN_SQL, job_card_id, carton_id, name,
                           c["net_weight"], c["gross_weight"], c["count"], actor)
        out.append({"box_code": carton_id, "box_number": c["box_number"], "article": name,
                    "net_weight": c["net_weight"], "gross_weight": c["gross_weight"],
                    "count": c["count"], "lot_number": c["lot_number"]})
    return {"job_card_id": job_card_id, "job_card_number": jc["job_card_number"],
            "entity": jc["entity"], "boxes": out}


async def list_scans(conn, *, job_card_id: int) -> dict:
    """All scans for a JC (enriched with floor/entity/SKU) + running totals. Each
    scan carries ``stores``: Stores' reference when the box was sent for this job
    card, else None; and ``printed``: its sticker "Box #" and LOT when it was
    printed on this job card's Raw Material tab, else None. ``next_box_number`` is
    the Box # Manual print offers next."""
    rows = await conn.fetch(
        "SELECT * FROM jc_box_scan_enriched WHERE job_card_id = $1 "
        "ORDER BY scanned_at DESC",
        job_card_id,
    )
    scans = [dict(r) for r in rows]
    codes = [s["box_id"] or s["sfg_box_id"] for s in scans if s["box_id"] or s["sfg_box_id"]]
    refs: dict[str, dict] = {}
    if codes and await _stores_table_exists(conn):
        for r in await conn.fetch(_STORES_REFS_SQL, job_card_id, codes):
            refs[r["box_code"]] = _stores_ref(r)
    sfg_ids = [s["sfg_box_id"] for s in scans if s["sfg_box_id"]]
    printed: dict[str, dict] = {}
    if sfg_ids:
        for r in await conn.fetch(_PRINTED_SQL, sfg_ids, job_card_id, PRINT_STAGE_BUCKET):
            printed[r["carton_id"]] = {"box_number": _sticker_number(r["carton_id"]),
                                       "lot_number": r["batch_code"]}
    for s in scans:
        s["stores"] = refs.get(s["box_id"] or s["sfg_box_id"])
        s["printed"] = printed.get(s["sfg_box_id"])
    last = await conn.fetchval(_stores_print()._COUNTER_SQL, job_card_id) or 0
    return {
        "job_card_id": job_card_id,
        "scans": scans,
        "totals": {
            "boxes":        len(scans),
            "net_weight":   round(sum(float(s["net_weight"]   or 0) for s in scans), 3),
            "gross_weight": round(sum(float(s["gross_weight"] or 0) for s in scans), 3),
            "count":        sum(int(s["count"] or 0) for s in scans),
        },
        "next_box_number": int(last) + 1,
    }


async def delete_scan(conn, *, job_card_id: int, code: str) -> dict:
    """Un-scan a box — matches the code against either source column."""
    res = await conn.execute(
        "DELETE FROM jc_box_scan WHERE job_card_id = $1 "
        "AND (box_id = $2 OR sfg_box_id = $2)",
        job_card_id, (code or "").strip(),
    )
    deleted = int(res.split()[-1]) if res.startswith("DELETE") else 0
    if deleted == 0:
        return {"error": "scan_not_found", "code": code}
    return {"deleted": True, "count": deleted}
