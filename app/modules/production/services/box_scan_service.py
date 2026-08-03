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
"""
from __future__ import annotations

from app.modules.production.services.box_identify_service import identify_box


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

    Resolution runs in AUTOCOMMIT on purpose: identify_box swallows
    missing-legacy-table errors, which would poison a surrounding transaction —
    so the caller must NOT wrap this in one (the single upsert is atomic anyway).
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
    if box is None:
        ident = await identify_box(conn, code)
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
    return {"scanned": True, "scan": dict(row)}


async def list_scans(conn, *, job_card_id: int) -> dict:
    """All scans for a JC (enriched with floor/entity/SKU) + running totals."""
    rows = await conn.fetch(
        "SELECT * FROM jc_box_scan_enriched WHERE job_card_id = $1 "
        "ORDER BY scanned_at DESC",
        job_card_id,
    )
    scans = [dict(r) for r in rows]
    return {
        "job_card_id": job_card_id,
        "scans": scans,
        "totals": {
            "boxes":        len(scans),
            "net_weight":   round(sum(float(s["net_weight"]   or 0) for s in scans), 3),
            "gross_weight": round(sum(float(s["gross_weight"] or 0) for s in scans), 3),
            "count":        sum(int(s["count"] or 0) for s in scans),
        },
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
