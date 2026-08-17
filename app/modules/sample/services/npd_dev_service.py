"""Standalone NPD development job cards (NPD product-development track).

Pure R&D, decoupled from sample requisitions: a development job card is created
directly, its trial recipe is authored in npd_dev_job_card_lines, and CLOSING it
records the trial output AND promotes the recipe into a live bom_header +
bom_line (so the new product becomes real). This is a different process from the
sample-issuance lifecycle in requisition_service — nothing here touches
sample_requisitions.

State machine:  DRAFT --start--> IN_DEVELOPMENT --close--> CLOSED
                  └────────────── cancel ──────────────> CANCELLED
Recipe lines are editable only while DRAFT.

The single identifier is the 8-digit time-based BIGINT id (new_short_time_id),
surfaced everywhere — there is no separate house number.
"""
from __future__ import annotations

import logging

import asyncpg
from fastapi import HTTPException

from app.core.helpers import new_short_time_id
from app.modules.sample.services import npd_auth
from app.modules.sample.services import sample_inventory_service as inv

logger = logging.getLogger(__name__)

# Live BOMs minted on promotion carry the legal entity axis; the sample module
# stays on 'cfpl' (the physical-warehouse axis lives only on sample tables).
_BOM_ENTITY = "cfpl"

# Customer + dispatch-planning columns shared by sample_requisitions and
# npd_dev_job_cards — the card inherits them from the requisition it spawns.
_DISPATCH_FIELDS = (
    "company_name", "customer_name", "customer_contact", "customer_ship_to_address",
    "mode_of_transport", "expected_dispatch_date", "confirmed_dispatch_date",
)


def _normalize_billing(returnable, non_returnable, paid, amount):
    """Enforce the sample billing checklist on the EFFECTIVE quartet: returnable XOR
    non_returnable; amount is 0 unless paid, else must be > 0. Returns the normalized tuple
    (amount forced to 0 when not paid). Raises 422 on a conflicting combo. Used post-inheritance
    (create) and post-coalesce (update) so a partial/mixed payload can't persist an invalid state
    — the per-field model validator can't see fields the caller omitted."""
    if returnable and non_returnable:
        raise HTTPException(422, detail={"error": "billing_conflict",
                                         "message": "A sample can't be both returnable and non-returnable",
                                         "details": {}})
    if paid:
        if amount is None or float(amount) <= 0:
            raise HTTPException(422, detail={"error": "amount_required",
                                             "message": "Amount is required and must be greater than 0 when paid",
                                             "details": {}})
    else:
        amount = 0
    return returnable, non_returnable, paid, amount


async def _fetch(conn, dev_jc_id: int) -> dict:
    row = await conn.fetchrow("SELECT * FROM npd_dev_job_cards WHERE id = $1", dev_jc_id)
    if not row:
        raise HTTPException(404, detail={"error": "not_found",
                                         "message": f"NPD development job card {dev_jc_id} not found",
                                         "details": {"id": dev_jc_id}})
    return dict(row)


async def _insert_lines(conn, dev_jc_id: int, lines: list[dict], *, phase_id=None) -> None:
    """Insert recipe lines. phase_id IS NULL = the card base recipe; a phase_id
    ties the lines to one trial phase's recipe. (Card-level / phase recipes — does NOT
    reference article_id, so it keeps working whether or not 082 is applied.)"""
    for i, ln in enumerate(lines):
        await conn.execute(
            """
            INSERT INTO npd_dev_job_card_lines
                (dev_jc_id, phase_id, sku_id, sku_name, qty, uom, item_type,
                 ownership, is_off_master, customer_lot_ref, received_qty, line_order, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            dev_jc_id, phase_id, ln.get("sku_id"), ln["sku_name"], ln["qty"], ln["uom"], ln.get("item_type"),
            ln.get("ownership", "OWN"), ln.get("is_off_master", False),
            ln.get("customer_lot_ref"), ln.get("received_qty"),
            ln.get("line_order", i), ln.get("notes"))


async def _insert_article_lines(conn, dev_jc_id: int, article_id: int, lines: list[dict]) -> None:
    """Insert one target article's trial-recipe lines (082 — carries article_id). Separate
    from _insert_lines so the card/phase recipe inserts never reference the article_id
    column, isolating the 082 dependency to the per-article create path only."""
    for i, ln in enumerate(lines):
        await conn.execute(
            """
            INSERT INTO npd_dev_job_card_lines
                (dev_jc_id, article_id, sku_id, sku_name, qty, uom, item_type,
                 ownership, is_off_master, customer_lot_ref, received_qty, line_order, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            dev_jc_id, article_id, ln.get("sku_id"), ln["sku_name"], ln["qty"], ln["uom"], ln.get("item_type"),
            ln.get("ownership", "OWN"), ln.get("is_off_master", False),
            ln.get("customer_lot_ref"), ln.get("received_qty"),
            ln.get("line_order", i), ln.get("notes"))


async def _is_per_article(conn, dev_jc_id: int) -> bool:
    """True if the card develops target ARTICLES (082) rather than one card-level recipe.
    Best-effort — 082 not applied → no article rows → False. The probe runs in its own
    savepoint so a missing-table error (082 absent) rolls back cleanly instead of poisoning
    an enclosing transaction (this is called inside _finalize_promote's promote txn)."""
    try:
        async with conn.transaction():
            return bool(await conn.fetchval(
                "SELECT 1 FROM npd_dev_job_card_articles WHERE dev_jc_id = $1 LIMIT 1", dev_jc_id))
    except Exception:  # noqa: BLE001 — 082 not applied
        return False


async def _write_dev_articles(conn, dev_jc_id: int, articles: list[dict], *, uom=None) -> None:
    """Insert each target article (082) + its own trial recipe. Each article's recipe
    comes from its explicit `lines`, else a clone of its base BOM's lines. article_id is
    an app-supplied 8-digit BIGINT (new_short_time_id) with per-row collision retry."""
    # Each article promotes into its own live BOM keyed by name (only one active BOM per
    # fg_sku_name). Two same-named articles would silently supersede each other on promote —
    # article #1's BOM deactivated by article #2 — so reject the clash up front.
    names = [(a.get("name") or "").strip() for a in articles]
    if len(set(names)) != len(names):
        raise HTTPException(422, detail={"error": "duplicate_article_name",
            "message": "Two articles share the same target product name — each must be unique (each becomes its own BOM)",
            "details": {}})
    for idx, a in enumerate(articles):
        article_id = None
        for _attempt in range(5):
            cand = new_short_time_id()
            try:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO npd_dev_job_card_articles
                               (article_id, dev_jc_id, name, pcs, weight_per_piece, quantity, uom,
                                base_bom_id, base_bom_name, line_order)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                        cand, dev_jc_id, a["name"], a.get("pcs"), a.get("weight_per_piece"),
                        a.get("quantity"), uom, a.get("base_bom_id"), a.get("base_bom_name"), idx)
                article_id = cand
                break
            except asyncpg.UniqueViolationError:
                if _attempt == 4:
                    raise
        # The frontend replicates the base-BOM recipe client-side and sends it in `lines`,
        # so each article's recipe is always explicit — no server-side per-article clone.
        a_lines = a.get("lines") or []
        if a_lines:
            await _insert_article_lines(conn, dev_jc_id, article_id, a_lines)


async def create_dev_job_card(conn, *, payload: dict, user) -> dict:
    """Create a standalone NPD development job card. It develops one or more target
    ARTICLES (082), each its own product with its own base BOM + trial recipe; the
    card-level fg_sku_name / pcs / weight / target_qty / base_bom_id MIRROR article #1.
    A legacy single-product payload (no `articles`) keeps the old card-base-recipe path,
    so it works whether or not 082 is applied."""
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    # Per-article path when `articles` is sent; else the legacy single-product mirror.
    articles = payload.get("articles")
    if articles:
        articles = [dict(a) for a in articles]
        for a in articles:
            p, w = a.get("pcs"), a.get("weight_per_piece")
            a["quantity"] = round(float(p) * float(w), 3) if p is not None and w is not None else None
        first = articles[0]
        base_bom_id = first.get("base_bom_id")
        fg_sku_name = first.get("name")
        target_qty = first.get("quantity")
        mirror_pcs, mirror_wpp = first.get("pcs"), first.get("weight_per_piece")
    else:
        base_bom_id = payload.get("base_bom_id")
        fg_sku_name = payload.get("fg_sku_name")
        target_qty = payload.get("target_qty")
        mirror_pcs = mirror_wpp = None   # taken from cust (source req) below
    async with conn.transaction():
        src_req = payload.get("source_requisition_id")
        # Customer + dispatch planning is attached to the job card: inherit each
        # field from the source requisition (the explicit payload value wins) so a
        # card developed from a request carries its company / customer / dispatch
        # plan automatically.
        inherit_cols = _DISPATCH_FIELDS + ("pcs", "weight_per_piece",
                                           "returnable", "non_returnable", "paid", "amount")
        cust = {k: payload.get(k) for k in inherit_cols}
        if src_req:
            rq = await conn.fetchrow(
                f"SELECT {', '.join(inherit_cols)} FROM sample_requisitions WHERE id = $1",
                src_req)
            if rq:
                for k in inherit_cols:
                    if cust[k] is None:
                        cust[k] = rq[k]
        # Normalize the EFFECTIVE billing (post-inheritance) so a mixed None/explicit payload
        # from a direct caller can't persist an invalid combo (both returnable + non_returnable,
        # or paid=false with amount>0) — the model validator only saw the sent fields.
        (cust["returnable"], cust["non_returnable"], cust["paid"], cust["amount"]) = _normalize_billing(
            cust["returnable"], cust["non_returnable"], cust["paid"], cust["amount"])
        # id is an app-supplied 8-digit time-based BIGINT (new_short_time_id, the
        # same handle pattern as request_id / job_card_id). It is the PK, so retry
        # on the rare unique collision via a per-attempt savepoint.
        dev_jc_id = None
        for _attempt in range(5):
            cand = new_short_time_id()
            try:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO npd_dev_job_cards
                            (id, title, description, warehouse, base_bom_id,
                             fg_sku_id, fg_sku_name, target_qty, uom, source_requisition_id,
                             company_name, customer_name, customer_contact, customer_ship_to_address,
                             mode_of_transport, expected_dispatch_date, confirmed_dispatch_date,
                             pcs, weight_per_piece,
                             returnable, non_returnable, paid, amount,
                             status, created_by)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                                $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, 'DRAFT', $24)
                        """,
                        cand, payload["title"], payload.get("description"),
                        payload.get("warehouse"), base_bom_id, payload.get("fg_sku_id"),
                        fg_sku_name, target_qty,
                        payload.get("uom"), src_req,
                        cust["company_name"], cust["customer_name"], cust["customer_contact"],
                        cust["customer_ship_to_address"], cust["mode_of_transport"],
                        cust["expected_dispatch_date"], cust["confirmed_dispatch_date"],
                        (mirror_pcs if articles else cust["pcs"]),
                        (mirror_wpp if articles else cust["weight_per_piece"]),
                        # Billing inherited from the source requisition (NULL for a
                        # standalone/sourceless card — read-only copy, 072 is the source of truth).
                        cust["returnable"], cust["non_returnable"], cust["paid"], cust["amount"],
                        user.user_id)
                dev_jc_id = cand
                break
            except asyncpg.UniqueViolationError:
                if _attempt == 4:
                    raise

        # Back-link the request so its detail/list shows "Open" (to this card)
        # instead of "Develop". Soft link — no FK; the dev-jc track stays decoupled.
        if src_req:
            await conn.execute(
                "UPDATE sample_requisitions SET linked_dev_jc_id = $1, updated_at = NOW() WHERE id = $2",
                dev_jc_id, src_req)

        if articles:
            await _write_dev_articles(conn, dev_jc_id, articles, uom=payload.get("uom"))
        elif payload.get("clone_from_base") and base_bom_id:
            base_lines = await conn.fetch(
                "SELECT * FROM bom_line WHERE bom_id = $1 ORDER BY line_number", base_bom_id)
            await _insert_lines(conn, dev_jc_id, [
                {"sku_id": None, "sku_name": bl["material_sku_name"],
                 "qty": float(bl["quantity_per_unit"]), "uom": bl["uom"] or "kg",
                 "item_type": bl["item_type"], "line_order": bl["line_number"]}
                for bl in base_lines])
        elif payload.get("lines"):
            await _insert_lines(conn, dev_jc_id, payload["lines"])
    return await get_dev_job_card(conn, dev_jc_id)


async def replace_dev_articles(conn, dev_jc_id: int, *, articles: list[dict], user) -> dict:
    """Replace ALL target articles + their recipes on a DRAFT/IN_DEVELOPMENT card (082),
    and re-mirror the card header (fg_sku_name/pcs/weight/target_qty/base_bom_id) from
    article #1. Deleting the article rows cascades their recipe lines (article_id FK)."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] not in ("DRAFT", "IN_DEVELOPMENT"):
        raise HTTPException(409, detail={"error": "not_editable",
                                         "message": "Articles are editable only while the card is DRAFT or IN_DEVELOPMENT",
                                         "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    arts = [dict(a) for a in (articles or [])]
    for a in arts:
        p, w = a.get("pcs"), a.get("weight_per_piece")
        a["quantity"] = round(float(p) * float(w), 3) if p is not None and w is not None else None
    if not arts:
        raise HTTPException(422, detail={"error": "no_article",
                                         "message": "A per-article dev job card needs at least one article",
                                         "details": {}})
    async with conn.transaction():
        await conn.execute("DELETE FROM npd_dev_job_card_articles WHERE dev_jc_id = $1", dev_jc_id)
        # Also clear any stale legacy card-level base recipe (article_id IS NULL AND
        # phase_id IS NULL) — a card first created via the single-product path then edited
        # into articles would otherwise keep those lines and pollute reads + promote.
        await conn.execute(
            "DELETE FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 "
            "AND article_id IS NULL AND phase_id IS NULL", dev_jc_id)
        # And any trial phases (+ their lines, ON DELETE CASCADE): per-article cards don't
        # use phases, so a legacy card with phases converted here would otherwise be left
        # hasArticles && hasPhases — which hides BOTH promote surfaces (a dead-end).
        await conn.execute("DELETE FROM npd_dev_job_card_phases WHERE dev_jc_id = $1", dev_jc_id)
        await _write_dev_articles(conn, dev_jc_id, arts, uom=jc.get("uom"))
        first = arts[0]
        await conn.execute(
            """UPDATE npd_dev_job_cards
                  SET fg_sku_name = $2, base_bom_id = $3, pcs = $4, weight_per_piece = $5,
                      target_qty = $6, updated_at = NOW()
                WHERE id = $1""",
            dev_jc_id, first["name"], first.get("base_bom_id"), first.get("pcs"),
            first.get("weight_per_piece"), first.get("quantity"))
    return await get_dev_job_card(conn, dev_jc_id)


async def update_dev_job_card_details(conn, dev_jc_id: int, *, payload: dict, user) -> dict:
    """Update a dev job card's customer & dispatch header (company / customer / contact /
    ship-to / mode-of-transport / expected-dispatch + billing). Editable while the card is
    DRAFT or IN_DEVELOPMENT. PATCH: only the whitelisted fields present in `payload` are
    written (the router passes model_dump(exclude_unset=True))."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] not in ("DRAFT", "IN_DEVELOPMENT"):
        raise HTTPException(409, detail={"error": "not_editable",
                                         "message": "Customer & dispatch details are editable only while the card is DRAFT or IN_DEVELOPMENT",
                                         "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    allowed = ("company_name", "customer_name", "customer_contact", "customer_ship_to_address",
               "mode_of_transport", "expected_dispatch_date",
               "returnable", "non_returnable", "paid", "amount")
    fields = {k: payload[k] for k in allowed if k in payload}
    # Billing must stay consistent even for a PARTIAL patch: coalesce each of the four against
    # the current row, normalize (returnable XOR non_returnable; amount 0 unless paid), and
    # write all four together — else e.g. PATCH {returnable:true} on a non_returnable card, or
    # PATCH {amount:100} on an unpaid card, would persist an invalid combo.
    _billing = ("returnable", "non_returnable", "paid", "amount")
    if any(k in fields for k in _billing):
        eff = _normalize_billing(fields.get("returnable", jc["returnable"]),
                                 fields.get("non_returnable", jc["non_returnable"]),
                                 fields.get("paid", jc["paid"]),
                                 fields.get("amount", jc["amount"]))
        (fields["returnable"], fields["non_returnable"], fields["paid"], fields["amount"]) = eff
    if fields:
        # Column names come from the fixed `allowed` whitelist (never user input → no
        # injection); the values are parameterised.
        cols = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        await conn.execute(
            f"UPDATE npd_dev_job_cards SET {cols}, updated_at = NOW() WHERE id = $1",
            dev_jc_id, *fields.values())
    return await get_dev_job_card(conn, dev_jc_id)


async def _dev_articles_for(conn, jc: dict, all_lines: list[dict]) -> list[dict]:
    """The card's target articles (082) with each one's own base recipe lines. Legacy
    cards (no article rows, or 082 not applied → read raises) synthesize a single article
    from the card header + the card-level base recipe, so the UI still shows one article."""
    rows: list = []
    try:
        rows = await conn.fetch(
            "SELECT * FROM npd_dev_job_card_articles WHERE dev_jc_id = $1 ORDER BY line_order, article_id",
            jc["id"])
    except Exception:  # noqa: BLE001 — 082 not applied → degrade to the header synthesis
        rows = []
    if rows:
        out = []
        for r in rows:
            a = dict(r)
            a["lines"] = [l for l in all_lines
                          if l.get("article_id") == a["article_id"] and l.get("phase_id") is None]
            out.append(a)
        return out
    if jc.get("fg_sku_name") or jc.get("base_bom_id"):
        return [{"article_id": None, "name": jc.get("fg_sku_name") or jc.get("title"),
                 "pcs": jc.get("pcs"), "weight_per_piece": jc.get("weight_per_piece"),
                 "quantity": jc.get("target_qty"), "uom": jc.get("uom"),
                 "base_bom_id": jc.get("base_bom_id"), "base_bom_name": jc.get("base_bom_name"),
                 "lines": [l for l in all_lines
                           if l.get("phase_id") is None and l.get("article_id") is None]}]
    return []


async def get_dev_job_card(conn, dev_jc_id: int) -> dict:
    jc = await _fetch(conn, dev_jc_id)
    all_lines = [dict(r) for r in await conn.fetch(
        "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 ORDER BY line_order, id", dev_jc_id)]
    # Card base recipe = lines with no phase AND no article; each phase / article
    # carries its own recipe (article_id may be absent when 082 isn't applied → None).
    jc["lines"] = [l for l in all_lines if l.get("phase_id") is None and l.get("article_id") is None]
    # Open phases at the top (the active one first), completed phases at the
    # bottom: IN_PROGRESS -> PENDING -> COMPLETED, then by phase_number.
    phases = [dict(r) for r in await conn.fetch(
        """SELECT * FROM npd_dev_job_card_phases WHERE dev_jc_id = $1
            ORDER BY CASE status
                       WHEN 'IN_PROGRESS' THEN 0
                       WHEN 'PENDING' THEN 1
                       ELSE 2 END,
                     phase_number""", dev_jc_id)]
    for ph in phases:
        ph["lines"] = [l for l in all_lines if l.get("phase_id") == ph["phase_id"]]
    jc["phases"] = phases
    # Partial outs (dispatch ledger, 078) — each row is one issued part + its 265
    # GI + its own outpass, ordered by seq (the outpass sub-number). dispatched_total
    # / remaining_qty let the UI bound the next partial and show the balance.
    dispatches = [dict(r) for r in await conn.fetch(
        "SELECT * FROM npd_dev_dispatch WHERE dev_jc_id = $1 ORDER BY seq", dev_jc_id)]
    jc["dispatches"] = dispatches
    _out_q = float(jc.get("output_qty") or 0)
    _disp_total = sum(float(d["qty"]) for d in dispatches)
    # Legacy fallback: pre-078 single-shot dispatches live only in the mirror
    # columns (no ledger row) — count them so remaining doesn't overstate.
    if not dispatches and jc.get("dispatched_at") and jc.get("dispatch_qty"):
        _disp_total = float(jc["dispatch_qty"])
    jc["dispatched_total"] = _disp_total
    jc["remaining_qty"] = _out_q - _disp_total
    # Resolve the base BOM's FG name for the detail header (lineage clarity).
    jc["base_bom_name"] = (
        await conn.fetchval("SELECT fg_sku_name FROM bom_header WHERE bom_id = $1", jc["base_bom_id"])
        if jc.get("base_bom_id") else None)
    # Requestor (the business head this was raised for) + sales POC, read from the source
    # requisition — no columns on the card itself. The sales-POC columns are selected
    # defensively: samples/ migrations are hand-applied, so 085 may not be in yet and a
    # bare SELECT of them would break the whole card view.
    jc["source_requestor_name"] = None
    jc["source_sales_poc_name"] = jc["source_sales_poc_email"] = None
    if jc.get("source_requisition_id"):
        has_poc = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'sample_requisitions' AND column_name = 'sales_poc_user_id'")
        src = await conn.fetchrow(
            f"""SELECT r.requestor_team, u.full_name AS requestor_full_name
                       {', r.sales_poc_name, r.sales_poc_email' if has_poc else ''}
                  FROM sample_requisitions r
                  LEFT JOIN auth_user u ON u.user_id = r.requestor_user_id
                 WHERE r.id = $1""", jc["source_requisition_id"])
        if src:
            jc["source_requestor_name"] = src["requestor_full_name"] or src["requestor_team"]
            if has_poc:
                jc["source_sales_poc_name"] = src["sales_poc_name"]
                jc["source_sales_poc_email"] = src["sales_poc_email"]
    # Target articles (082) + each one's own trial recipe. Legacy cards (no article rows,
    # or 082 not applied) synthesize a single article from the card header + base recipe.
    jc["articles"] = await _dev_articles_for(conn, jc, all_lines)
    # Per-article dispatch balances (083): each article dispatches its OWN finalized output
    # from its own FG batch, so attach its own ledger rows + dispatched_total + remaining_qty
    # (article_id is absent from the dispatch rows when 083 isn't applied → .get() → None).
    for a in jc["articles"]:
        if a.get("article_id") is not None:
            a_disp = [d for d in dispatches if d.get("article_id") == a["article_id"]]
            a["dispatches"] = a_disp
            a_total = sum(float(d["qty"]) for d in a_disp)
            a["dispatched_total"] = a_total
            a["remaining_qty"] = float(a.get("output_qty") or 0) - a_total
    # Pending dual-approval promote gate (None if no live request). Lets the
    # frontend render the two gate rows + their statuses. Best-effort: a couple
    # of SELECTs against the 069/070 tables.
    gate = None
    pr = await conn.fetchrow(
        "SELECT id, status, created_at FROM npd_dev_promote_request "
        "WHERE dev_jc_id = $1 AND status = 'PENDING'", dev_jc_id)
    if pr:
        appr = await conn.fetch(
            "SELECT approver_kind, approver_user_id, status FROM npd_dev_promote_approval "
            "WHERE promote_request_id = $1 ORDER BY approver_kind", pr["id"])
        gate = {"id": pr["id"], "status": pr["status"], "created_at": pr["created_at"],
                "approvals": [dict(r) for r in appr]}
    jc["promote_gate"] = gate
    # Gate-pass digital signatures: the promote-gate approvers (name + decided date)
    # from the LATEST promote request regardless of status — so a CLOSED/promoted card
    # still shows who digitally approved (Business head = REQUESTOR_BH, Inventory
    # manager = INV_MGR). Empty {} when no promote was ever raised. Best-effort.
    sigs: dict = {}
    pr_latest = await conn.fetchrow(
        "SELECT id FROM npd_dev_promote_request WHERE dev_jc_id = $1 ORDER BY created_at DESC LIMIT 1",
        dev_jc_id)
    if pr_latest:
        for r in await conn.fetch(
            """SELECT ap.approver_kind, ap.status, ap.decided_at, u.full_name
                 FROM npd_dev_promote_approval ap
                 LEFT JOIN auth_user u ON u.user_id = ap.approver_user_id
                WHERE ap.promote_request_id = $1""", pr_latest["id"]):
            sigs[r["approver_kind"]] = {"name": r["full_name"], "status": r["status"],
                                        "decided_at": r["decided_at"]}
    jc["gate_signatures"] = sigs
    return jc


async def list_dev_job_cards(conn, *, status: str | None = None,
                             limit: int = 50, offset: int = 0) -> list[dict]:
    clauses, params = [], []
    if status:
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([limit, offset])
    rows = await conn.fetch(
        f"""SELECT jc.*, (SELECT COUNT(*) FROM npd_dev_job_card_lines l
                            WHERE l.dev_jc_id = jc.id) AS line_count
             FROM npd_dev_job_cards jc{where}
             ORDER BY jc.created_at DESC
             LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
        *params)
    return [dict(r) for r in rows]


async def search_boms(conn, *, search: str | None = None, limit: int = 30) -> list[dict]:
    """Typeahead source for the 'Base BOM' picker on the dev job-card form.

    There are ~1300 active BOMs, so the UI can't render a static <select>; it
    searches by FG name / customer / numeric id and we return a compact list.
    Active BOMs surface first; an exact numeric id match is always included.
    """
    clauses, params = [], []
    if search and search.strip():
        s = search.strip()
        params.append(f"%{s}%")
        like = f"(fg_sku_name ILIKE ${len(params)} OR customer_name ILIKE ${len(params)}"
        if s.isdigit():
            params.append(int(s))
            like += f" OR bom_id = ${len(params)}"
        like += ")"
        clauses.append(like)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = await conn.fetch(
        f"""SELECT bom_id, fg_sku_name, customer_name, version, is_active, pack_size_kg
              FROM bom_header{where}
             ORDER BY is_active DESC, fg_sku_name ASC
             LIMIT ${len(params)}""",
        *params)
    return [dict(r) for r in rows]


# Normalise a name for the BOM⨝article link: lowercase + collapse whitespace.
# bom_header.fg_sku_name rarely matches all_sku.particulars exactly (11/1557) but
# matches 1546/1557 once normalised, so the browse cascade can ride the article
# master and resolve the chosen FG back to its live BOM(s).
_NORM = "lower(btrim(regexp_replace({col}, '\\s+', ' ', 'g')))"


async def browse_boms(conn, *, item_type: str | None = None, item_group: str | None = None,
                      sub_group: str | None = None, particulars: str | None = None) -> dict:
    """Cascade browse for the Base-BOM picker's 'Browse' tab.

    Drills down the article master (all_sku) joined to bom_header on the
    normalised FG name: Item type -> Item group -> Sub-group -> Item description
    (particulars). Returns the filtered option lists for the next dropdown and,
    once a particular is chosen, the matching BOM rows to pick from.
    """
    where, params = [], []
    for col, val in (("item_type", item_type), ("item_group", item_group),
                     ("sub_group", sub_group), ("particulars", particulars)):
        if val:
            params.append(val)
            where.append(f"s.{col} = ${len(params)}")
    wsql = (" AND " + " AND ".join(where)) if where else ""
    rows = await conn.fetch(
        f"""
        SELECT s.item_type, s.item_group, s.sub_group, s.particulars,
               b.bom_id, b.fg_sku_name, b.customer_name, b.version, b.is_active
          FROM all_sku s
          JOIN bom_header b
            ON {_NORM.format(col='s.particulars')} = {_NORM.format(col='b.fg_sku_name')}
         WHERE TRUE{wsql}
        """,
        *params)
    opts = {
        "item_types": sorted({r["item_type"] for r in rows if r["item_type"]}),
        "item_groups": sorted({r["item_group"] for r in rows if r["item_group"]}),
        "sub_groups": sorted({r["sub_group"] for r in rows if r["sub_group"]}),
        "particulars": sorted({r["particulars"] for r in rows if r["particulars"]}),
    }
    boms: list[dict] = []
    if particulars:
        seen: set = set()
        for r in rows:
            if r["bom_id"] in seen:
                continue
            seen.add(r["bom_id"])
            boms.append({k: r[k] for k in
                         ("bom_id", "fg_sku_name", "customer_name", "version", "is_active")})
        boms.sort(key=lambda x: (not x["is_active"], (x["fg_sku_name"] or "")))
    return {"options": opts, "boms": boms}


async def get_bom_lines(conn, bom_id: int) -> list[dict]:
    """Full material list of a BOM — seeds the dev job-card 'Trial recipe' and the
    requisition's article lines. BOM lines reference material by name only, so we
    resolve each to its all_sku.sku_id via the normalised-name match (nullable —
    ~99% resolve) so callers that need a real sku_id (e.g. requisition articles)
    can use it directly."""
    rows = await conn.fetch(
        f"""SELECT bl.line_number, bl.material_sku_name, bl.item_type,
                   bl.quantity_per_unit, bl.uom,
                   (SELECT s.sku_id FROM all_sku s
                     WHERE {_NORM.format(col='s.particulars')}
                         = {_NORM.format(col='bl.material_sku_name')}
                     LIMIT 1) AS sku_id
              FROM bom_line bl WHERE bl.bom_id = $1 ORDER BY bl.line_number""", bom_id)
    return [dict(r) for r in rows]


async def replace_lines(conn, dev_jc_id: int, *, lines: list[dict], user) -> dict:
    """Replace the card BASE recipe (phase_id IS NULL) — the starting point the
    first phase clones. Editable while the card is DRAFT or IN_DEVELOPMENT (same
    rule as the per-phase recipes). Per-phase recipes use replace_phase_lines."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] not in ("DRAFT", "IN_DEVELOPMENT"):
        raise HTTPException(409, detail={"error": "not_editable",
                                         "message": "The base recipe is editable while the card is a DRAFT or IN_DEVELOPMENT",
                                         "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 AND phase_id IS NULL", dev_jc_id)
        await _insert_lines(conn, dev_jc_id, lines, phase_id=None)
        await conn.execute("UPDATE npd_dev_job_cards SET updated_at = NOW() WHERE id = $1", dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


async def start_development(conn, dev_jc_id: int, *, user) -> dict:
    """DRAFT -> IN_DEVELOPMENT. Locks the recipe so the trial can run."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] != "DRAFT":
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Only a DRAFT development job card can be started",
                                         "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    if await _is_per_article(conn, dev_jc_id):
        # Per-article card: EVERY article must carry its own recipe (article_id lines) — an
        # article with an empty recipe would otherwise pass the card-wide count here and only
        # fail at promote (empty BOM). Catch it now, naming the offending article(s).
        empties = await conn.fetch(
            """SELECT a.name FROM npd_dev_job_card_articles a
                WHERE a.dev_jc_id = $1
                  AND NOT EXISTS (SELECT 1 FROM npd_dev_job_card_lines l
                                   WHERE l.article_id = a.article_id AND l.phase_id IS NULL)
                ORDER BY a.line_order, a.article_id""", dev_jc_id)
        if empties:
            names = ", ".join(e["name"] for e in empties)
            raise HTTPException(422, detail={"error": "empty_recipe",
                                             "message": f"Add a recipe to every article before starting — no recipe yet on: {names}",
                                             "details": {"articles": [e["name"] for e in empties]}})
    else:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM npd_dev_job_card_lines WHERE dev_jc_id = $1", dev_jc_id)
        if not n:
            raise HTTPException(422, detail={"error": "empty_recipe",
                                             "message": "Add at least one recipe line before starting development",
                                             "details": {"id": dev_jc_id}})
    await conn.execute(
        """UPDATE npd_dev_job_cards
              SET status = 'IN_DEVELOPMENT', started_by = $1, started_at = NOW(), updated_at = NOW()
            WHERE id = $2""",
        user.user_id, dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


# ---------------------------------------------------------------------------
# Trial phases (multi-day) — operator-defined, started/completed independently.
# Mirrors the production job_card_process_step lifecycle (PENDING -> IN_PROGRESS
# -> COMPLETED). Phases live inside the card's IN_DEVELOPMENT state.
# ---------------------------------------------------------------------------
async def _fetch_phase(conn, dev_jc_id: int, phase_id: int) -> dict:
    row = await conn.fetchrow(
        "SELECT * FROM npd_dev_job_card_phases WHERE phase_id = $1 AND dev_jc_id = $2",
        phase_id, dev_jc_id)
    if not row:
        raise HTTPException(404, detail={"error": "not_found",
                                         "message": f"Phase {phase_id} not found on this job card",
                                         "details": {"phase_id": phase_id, "dev_jc_id": dev_jc_id}})
    return dict(row)


async def add_phase(conn, dev_jc_id: int, *, name: str, clone_from_phase_id=None, user) -> dict:
    """Add a trial phase, cloning a recipe as its starting point. Source order:
    the given clone_from_phase_id, else the latest existing phase, else the card
    base recipe. Allowed while DRAFT (planning) or IN_DEVELOPMENT."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] not in ("DRAFT", "IN_DEVELOPMENT"):
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Phases can be added only while the card is a DRAFT or IN_DEVELOPMENT",
                                         "details": {"status": jc["status"]}})
    # Per-article cards (082) develop each article's own recipe directly — no shared trial
    # phases (they'd merge every article's phase_id-IS-NULL lines into one phase's clone).
    if await _is_per_article(conn, dev_jc_id):
        raise HTTPException(409, detail={"error": "per_article_card",
                                         "message": "This card develops per-article recipes — trial phases don't apply",
                                         "details": {"dev_jc_id": dev_jc_id}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    async with conn.transaction():
        n = await conn.fetchval(
            "SELECT COALESCE(MAX(phase_number), 0) + 1 FROM npd_dev_job_card_phases WHERE dev_jc_id = $1",
            dev_jc_id)
        # Resolve the recipe to clone: explicit phase, else the latest phase.
        src_phase = clone_from_phase_id
        if src_phase is None:
            src_phase = await conn.fetchval(
                "SELECT phase_id FROM npd_dev_job_card_phases WHERE dev_jc_id = $1 "
                "ORDER BY phase_number DESC LIMIT 1", dev_jc_id)
        if src_phase is not None:
            src_lines = await conn.fetch(
                "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 AND phase_id = $2 "
                "ORDER BY line_order, id", dev_jc_id, src_phase)
        else:  # first phase → clone the card base recipe
            src_lines = await conn.fetch(
                "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 AND phase_id IS NULL "
                "ORDER BY line_order, id", dev_jc_id)
        # phase_id is an app-supplied 8-digit time-based BIGINT (new_short_time_id),
        # the same handle pattern as the job-card id. Retry on the rare collision.
        new_phase_id = None
        for _attempt in range(5):
            cand = new_short_time_id()
            try:
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO npd_dev_job_card_phases (phase_id, dev_jc_id, phase_number, name) "
                        "VALUES ($1, $2, $3, $4)",
                        cand, dev_jc_id, n, name.strip())
                new_phase_id = cand
                break
            except asyncpg.UniqueViolationError:
                if _attempt == 4:
                    raise
        if src_lines:
            await _insert_lines(conn, dev_jc_id, [dict(r) for r in src_lines], phase_id=new_phase_id)
        await conn.execute("UPDATE npd_dev_job_cards SET updated_at = NOW() WHERE id = $1", dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


async def replace_phase_lines(conn, dev_jc_id: int, phase_id: int, *, lines: list[dict], user) -> dict:
    """Replace one phase's recipe (its own independent formulation). Editable while
    the card is DRAFT or IN_DEVELOPMENT and the phase is not yet COMPLETED."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] not in ("DRAFT", "IN_DEVELOPMENT"):
        raise HTTPException(409, detail={"error": "not_editable",
                                         "message": "Phase recipes are editable while the card is a DRAFT or IN_DEVELOPMENT",
                                         "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    ph = await _fetch_phase(conn, dev_jc_id, phase_id)
    if ph["status"] == "COMPLETED":
        raise HTTPException(409, detail={"error": "phase_completed",
                                         "message": "A completed phase's recipe can no longer be edited",
                                         "details": {"phase_id": phase_id}})
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 AND phase_id = $2", dev_jc_id, phase_id)
        await _insert_lines(conn, dev_jc_id, lines, phase_id=phase_id)
        await conn.execute("UPDATE npd_dev_job_cards SET updated_at = NOW() WHERE id = $1", dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


async def start_phase(conn, dev_jc_id: int, phase_id: int, *, user) -> dict:
    """PENDING -> IN_PROGRESS. The card must be IN_DEVELOPMENT (the trial is running)."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] != "IN_DEVELOPMENT":
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Start development before running phases",
                                         "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    ph = await _fetch_phase(conn, dev_jc_id, phase_id)
    if ph["status"] != "PENDING":
        raise HTTPException(409, detail={"error": "wrong_phase_status",
                                         "message": "Only a PENDING phase can be started",
                                         "details": {"status": ph["status"]}})
    await conn.execute(
        """UPDATE npd_dev_job_card_phases
              SET status = 'IN_PROGRESS', started_at = NOW(), started_by = $1
            WHERE phase_id = $2""",
        user.user_id, phase_id)
    await conn.execute("UPDATE npd_dev_job_cards SET updated_at = NOW() WHERE id = $1", dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


async def complete_phase(conn, dev_jc_id: int, phase_id: int, *, payload=None, user) -> dict:
    """IN_PROGRESS -> COMPLETED, recording the phase's output + material accounting.
    yield_pct is derived from output / RM consumed (same rule as the card close)."""
    payload = payload or {}
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] != "IN_DEVELOPMENT":
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Phases can only be completed while the card is IN_DEVELOPMENT",
                                         "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    ph = await _fetch_phase(conn, dev_jc_id, phase_id)
    if ph["status"] != "IN_PROGRESS":
        raise HTTPException(409, detail={"error": "wrong_phase_status",
                                         "message": "Only an IN_PROGRESS phase can be completed",
                                         "details": {"status": ph["status"]}})
    out_qty = payload.get("output_qty")
    rm = payload.get("rm_consumed_qty")
    yield_pct = None
    if out_qty is not None and rm not in (None, 0) and float(rm) > 0:
        yield_pct = round(float(out_qty) / float(rm) * 100, 2)
    await conn.execute(
        """UPDATE npd_dev_job_card_phases
              SET status = 'COMPLETED', completed_at = NOW(), completed_by = $1,
                  output_qty = $2, output_uom = $3, rm_consumed_qty = $4,
                  wastage_qty = $5, extra_give_away_qty = $6, yield_pct = $7,
                  notes = COALESCE($8, notes)
            WHERE phase_id = $9""",
        user.user_id, out_qty, payload.get("output_uom"), rm,
        payload.get("wastage_qty"), payload.get("extra_give_away_qty"), yield_pct,
        payload.get("notes"), phase_id)
    await conn.execute("UPDATE npd_dev_job_cards SET updated_at = NOW() WHERE id = $1", dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


async def delete_phase(conn, dev_jc_id: int, phase_id: int, *, user) -> dict:
    """Delete a trial phase and its recipe lines (npd_dev_job_card_lines.phase_id is
    ON DELETE CASCADE). Allowed while the card is a DRAFT or IN_DEVELOPMENT."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] not in ("DRAFT", "IN_DEVELOPMENT"):
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Phases can be deleted only while the card is a DRAFT or IN_DEVELOPMENT",
                                         "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    await _fetch_phase(conn, dev_jc_id, phase_id)   # 404 if not on this card
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM npd_dev_job_card_phases WHERE phase_id = $1 AND dev_jc_id = $2",
            phase_id, dev_jc_id)
        await conn.execute("UPDATE npd_dev_job_cards SET updated_at = NOW() WHERE id = $1", dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


# bom_header carries THREE unique indexes — bom_header_pkey (bom_id),
# uq_bom_header_active_fg (fg_sku_name WHERE is_active) and uq_bom_header_fg_version
# (fg_sku_name, version WHERE is_active). Only the last two are about the product
# name, so the constraint has to be identified before blaming it on the operator.
_FG_NAME_CONSTRAINTS = ("uq_bom_header_active_fg", "uq_bom_header_fg_version")


async def _insert_bom_header(conn, fg_name: str, version: int, note: str) -> int:
    return await conn.fetchval(
        """INSERT INTO bom_header (fg_sku_name, version, is_active, entity, notes)
           VALUES ($1, $2, TRUE, $3, $4) RETURNING bom_id""",
        fg_name, version, _BOM_ENTITY, note)


async def _mint_bom(conn, *, fg_name: str, lines, note: str) -> int:
    """Promote one recipe into a live bom_header + bom_line, returning the new bom_id.
    Only ONE active BOM is allowed per fg_sku_name (uq_bom_header_active_fg), so supersede
    any existing active BOM for this FG and mint the NEXT version (a repeated promote bumps
    the version rather than 500'ing on the unique constraint)."""
    await conn.execute(
        "UPDATE bom_header SET is_active = FALSE WHERE fg_sku_name = $1 AND is_active = TRUE", fg_name)
    next_ver = await conn.fetchval(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM bom_header WHERE fg_sku_name = $1", fg_name)
    try:
        # Nested transaction = SAVEPOINT. A failed INSERT aborts the transaction, so
        # without this the sequence repair below could not run and the whole promote
        # (including the already-minted sibling articles) would be unrecoverable.
        async with conn.transaction():
            new_bom_id = await _insert_bom_header(conn, fg_name, next_ver, note)
    except asyncpg.UniqueViolationError as e:
        new_bom_id = await _mint_bom_recover(conn, e, fg_name, next_ver, note)
    for i, ln in enumerate(lines, 1):
        await conn.execute(
            """INSERT INTO bom_line
                   (bom_id, line_number, material_sku_name, item_type, quantity_per_unit, uom)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            new_bom_id, i, ln["sku_name"], ln["item_type"] or "rm", ln["qty"], ln["uom"])
    return new_bom_id


async def _mint_bom_recover(conn, err: asyncpg.UniqueViolationError,
                            fg_name: str, next_ver: int, note: str) -> int:
    """Decide what a unique violation on the bom_header INSERT actually means.

    This used to report EVERY unique violation as "a live BOM for '<name>' already
    exists — rename the target product", which sent operators off to rename a product
    that was never the problem. That advice is almost always wrong here: _mint_bom
    deactivates the existing active BOM for this exact fg_sku_name and bumps to the
    next version immediately before inserting, so the FG-name indexes cannot collide
    on a single-threaded promote. The realistic cause is bom_header_pkey.
    """
    constraint = (err.constraint_name or "").lower()

    if constraint == "bom_header_pkey":
        # bom_id is a plain serial, and bom_header was bulk-loaded with explicit ids.
        # An import that does not resync the sequence leaves nextval() handing out ids
        # that are already taken, so EVERY promote fails until the sequence catches up.
        # setval to MAX only ever moves the sequence FORWARD, and sequences are
        # non-transactional, so the repair sticks even if this promote later rolls back.
        logger.warning(
            "bom_header id sequence is behind MAX(bom_id) — resyncing and retrying the "
            "promote of %r (constraint %s)", fg_name, constraint)
        await conn.execute(
            """SELECT setval(pg_get_serial_sequence('bom_header', 'bom_id'),
                             (SELECT COALESCE(MAX(bom_id), 1) FROM bom_header))""")
        try:
            async with conn.transaction():
                return await _insert_bom_header(conn, fg_name, next_ver, note)
        except asyncpg.UniqueViolationError as e2:
            raise HTTPException(409, detail={
                "error": "bom_id_sequence",
                "message": "Couldn't promote — the bom_header id sequence is out of sync "
                           "and did not recover automatically. A DBA needs to run: "
                           "SELECT setval(pg_get_serial_sequence('bom_header','bom_id'), "
                           "(SELECT MAX(bom_id) FROM bom_header));",
                "details": {"fg_sku_name": fg_name,
                            "constraint": (e2.constraint_name or "").lower()}}) from e2

    if constraint in _FG_NAME_CONSTRAINTS:
        raise HTTPException(409, detail={
            "error": "bom_conflict",
            "message": f"Couldn't promote — a live BOM for '{fg_name}' already exists. "
                       "Rename the target product or deactivate the existing BOM.",
            "details": {"fg_sku_name": fg_name, "constraint": constraint}}) from err

    # Some other unique index. Name it rather than guessing — guessing is what made
    # this bug take a rename-the-product detour in the first place.
    raise HTTPException(409, detail={
        "error": "bom_conflict",
        "message": f"Couldn't promote '{fg_name}' — the BOM insert violated unique "
                   f"constraint {constraint or 'unknown'}.",
        "details": {"fg_sku_name": fg_name, "constraint": constraint}}) from err


async def _finalize_promote(conn, dev_jc_id, *, promote_phase_id, close_payload: dict, user) -> dict:
    """IN_DEVELOPMENT -> CLOSED: record the trial output AND promote the recipe
    into a live bom_header + bom_line. Returns the closed job card with its
    promoted_bom_id set.

    A per-article card (082) fans out — each article's own recipe is promoted into its
    OWN live BOM (article.promoted_bom_id), and the card's promoted_bom_id MIRRORS
    article #1. A single-recipe card promotes the chosen phase (else the card base recipe).

    This is the real promote body, run only once the dual-approval gate clears
    (see promote_approval_service.finalize_if_ready). It assumes it is authorized
    to run: the IN_DEVELOPMENT gate + CLOSE auth live in request_promote, which is
    what the operator hits first. promote_phase_id comes from the arg and the
    output/accounting fields come from close_payload (the stashed close dict)."""
    jc = await _fetch(conn, dev_jc_id)
    # Guard: the card may have been cancelled (or already closed) between the
    # promote request opening and both gates clearing — never promote a card that
    # is no longer IN_DEVELOPMENT (the gate accept rolls back via the outer txn).
    if jc["status"] != "IN_DEVELOPMENT":
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Job card is no longer IN_DEVELOPMENT",
                                         "details": {"status": jc["status"]}})
    # Per-article card (082) → each article promotes its own recipe into its own BOM.
    per_article = await _is_per_article(conn, dev_jc_id)
    fg_name = jc["fg_sku_name"] or jc["title"]

    # Accounting: a chosen FINAL-TRIAL phase's recorded output is INHERITED verbatim (the
    # close is "pick the final trial and finish", not a second entry). No-phase single-product
    # cards take the top-level close payload; per-article cards take the per-article `articles`
    # output list (each article its own output → its own FG batch → its own dispatch balance).
    phase = None
    if not per_article and promote_phase_id is not None:
        phase = await _fetch_phase(conn, dev_jc_id, promote_phase_id)   # 404 if not on this card
    if phase is not None:
        out_qty = phase["output_qty"]
        out_uom = phase["output_uom"]
        rm_consumed = phase["rm_consumed_qty"]
        wastage = phase["wastage_qty"]
        ega = phase["extra_give_away_qty"]
        yield_pct = phase["yield_pct"]
    else:
        # Auto yield % = FG output / RM consumed × 100; falls back to supplied yield_pct.
        out_qty = close_payload.get("output_qty")
        out_uom = close_payload.get("output_uom")
        rm_consumed = close_payload.get("rm_consumed_qty")
        wastage = close_payload.get("wastage_qty")
        ega = close_payload.get("extra_give_away_qty")
        yield_pct = close_payload.get("yield_pct")
        if out_qty is not None and rm_consumed not in (None, 0) and float(rm_consumed) > 0:
            yield_pct = round(float(out_qty) / float(rm_consumed) * 100, 2)
    out_notes = close_payload.get("output_notes")
    async with conn.transaction():
        if per_article:
            # Promote EACH article's own recipe (article_id = X, phase_id IS NULL) into its
            # own live BOM, AND receive its own finished FG sample into R&D (its own batch).
            # The card header MIRRORS the totals across articles (display + legacy columns).
            arts = [dict(r) for r in await conn.fetch(
                "SELECT * FROM npd_dev_job_card_articles WHERE dev_jc_id = $1 ORDER BY line_order, article_id",
                dev_jc_id)]
            all_lines = [dict(r) for r in await conn.fetch(
                "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 ORDER BY line_order, id", dev_jc_id)]
            if not arts:  # defensive — never close a per-article card with no BOM minted
                raise HTTPException(422, detail={"error": "no_article",
                                                 "message": "This card has no articles to promote",
                                                 "details": {"id": dev_jc_id}})
            # Per-article output/accounting from the close payload, keyed by article_id.
            art_out = {int(o["article_id"]): o for o in (close_payload.get("articles") or [])
                       if o.get("article_id") is not None}
            new_bom_id = fg_batch_id = None
            sum_out = sum_rm = sum_waste = sum_ega = 0.0
            for a in arts:
                a_lines = [l for l in all_lines
                           if l.get("article_id") == a["article_id"] and l.get("phase_id") is None]
                if not a_lines:
                    raise HTTPException(422, detail={"error": "empty_recipe",
                                                     "message": f"Article '{a['name']}' has no recipe to promote",
                                                     "details": {"article_id": a["article_id"]}})
                bom_id = await _mint_bom(conn, fg_name=a["name"], lines=a_lines,
                                         note=f"Promoted from NPD development job card {jc['id']} · article {a['name']}")
                o = art_out.get(a["article_id"], {})
                a_out = o.get("output_qty")
                a_uom = o.get("output_uom") or jc["uom"] or "kg"
                a_rm = o.get("rm_consumed_qty")
                a_waste = o.get("wastage_qty")
                a_ega = o.get("extra_give_away_qty")
                a_yield = (round(float(a_out) / float(a_rm) * 100, 2)
                           if a_out is not None and a_rm not in (None, 0) and float(a_rm) > 0 else None)
                # Each article's finished FG into R&D — its own batch, keyed by article_id.
                # Uses a DISTINCT reference_type ('_ART') so the batch id lives in a disjoint
                # namespace from the card-level FGSMP-NPD_DEV_JC-<dev_jc_id> batch — article_id
                # and dev_jc_id are independent 8-digit id spaces that can otherwise collide
                # on the same FGSMP id (create_batch would DO NOTHING and bind the wrong stock).
                a_batch = None
                if a_out and float(a_out) > 0:
                    recv = await inv.receive_fg_sample(
                        conn, sku_name=a["name"], qty_kg=float(a_out),
                        reference_type="NPD_DEV_JC_ART", reference_id=a["article_id"], entity=_BOM_ENTITY,
                        uom=a_uom, lot_number=str(jc["id"]), user=user)
                    a_batch = recv["batch_id"]
                await conn.execute(
                    """UPDATE npd_dev_job_card_articles
                          SET promoted_bom_id = $1, status = 'CLOSED', output_qty = $2, output_uom = $3,
                              rm_consumed_qty = $4, wastage_qty = $5, extra_give_away_qty = $6,
                              yield_pct = $7, fg_sample_batch_id = $8
                        WHERE article_id = $9""",
                    bom_id, a_out, a_uom, a_rm, a_waste, a_ega, a_yield, a_batch, a["article_id"])
                if new_bom_id is None:
                    new_bom_id, fg_batch_id = bom_id, a_batch   # card mirrors article #1
                sum_out += float(a_out or 0)
                sum_rm += float(a_rm or 0)
                sum_waste += float(a_waste or 0)
                sum_ega += float(a_ega or 0)
            # Card header mirror = TOTALS across articles (card-level Output card + 046 mirror
            # columns). Per-article dispatch uses each article's own batch, not this fg_batch_id.
            out_qty = sum_out or None
            out_uom = jc["uom"]
            rm_consumed = sum_rm or None
            wastage = sum_waste or None
            ega = sum_ega or None
            yield_pct = round(sum_out / sum_rm * 100, 2) if sum_out and sum_rm else None
        else:
            # Single-recipe card: the chosen final-trial phase, else the card base recipe.
            if phase is not None:
                lines = await conn.fetch(
                    "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 AND phase_id = $2 "
                    "ORDER BY line_order, id", dev_jc_id, promote_phase_id)
                empty_msg = "The chosen phase has no recipe lines to promote"
            else:
                lines = await conn.fetch(
                    "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 AND phase_id IS NULL "
                    "ORDER BY line_order, id", dev_jc_id)
                empty_msg = "Cannot close — pick a phase whose recipe to promote"
            if not lines:
                raise HTTPException(422, detail={"error": "empty_recipe", "message": empty_msg,
                                                 "details": {"id": dev_jc_id}})
            new_bom_id = await _mint_bom(conn, fg_name=fg_name, lines=lines,
                                         note=f"Promoted from NPD development job card {jc['id']}")

        # Step B (NPD plan §1) — receive the finished trial sample into the R&D
        # location when an output quantity was recorded. Single-product card only: a
        # per-article card already received each article's own FG above (fg_batch_id set).
        if not per_article:
            fg_batch_id = None
            if out_qty and float(out_qty) > 0:
                recv = await inv.receive_fg_sample(
                    conn, sku_name=fg_name, qty_kg=float(out_qty),
                    reference_type="NPD_DEV_JC", reference_id=dev_jc_id, entity=_BOM_ENTITY,
                    uom=(out_uom or jc["uom"] or "kg"),
                    lot_number=str(jc["id"]), user=user)
                fg_batch_id = recv["batch_id"]

        await conn.execute(
            """
            UPDATE npd_dev_job_cards
               SET status = 'CLOSED', promoted_bom_id = $1,
                   output_qty = $2, output_uom = $3, yield_pct = $4, output_notes = $5,
                   rm_consumed_qty = $6, wastage_qty = $7, extra_give_away_qty = $8,
                   fg_sample_batch_id = $9,
                   -- Confirmed dispatch date (By NPD) = the job card's closing date.
                   confirmed_dispatch_date = CURRENT_DATE,
                   closed_by = $10, closed_at = NOW(), updated_at = NOW()
             WHERE id = $11 AND status = 'IN_DEVELOPMENT'
            """,
            new_bom_id, out_qty, out_uom, yield_pct, out_notes,
            rm_consumed, wastage, ega, fg_batch_id,
            user.user_id, dev_jc_id)
        # Mirror the confirmed dispatch date onto the source requisition so its
        # view reflects the NPD-confirmed dispatch once the trial is closed.
        #
        # Also hand the freshly promoted BOM back to the requisition. Without this the
        # development loop never closes: a sample that needed a recipe gets one developed
        # and promoted, but base_bom_id stays NULL, so start_production keeps refusing to
        # raise job cards. COALESCE so a requisition that already names a BOM keeps it —
        # a promote must never silently repoint a deliberate choice.
        if jc.get("source_requisition_id"):
            await conn.execute(
                "UPDATE sample_requisitions "
                "   SET confirmed_dispatch_date = CURRENT_DATE, "
                "       base_bom_id = COALESCE(base_bom_id, $2), "
                "       updated_at = NOW() "
                " WHERE id = $1", jc["source_requisition_id"], new_bom_id)
    return await get_dev_job_card(conn, dev_jc_id)


async def request_promote(conn, dev_jc_id, *, payload: dict, user) -> dict:
    """'Record output & promote' — instead of promoting now, open a pending promote
    request + two gate approvals (inventory_manager + requestor BH). Promote runs
    only once both accept (promote_approval_service.finalize_if_ready)."""
    from app.modules.sample.services import promote_approval_service as pas
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] != "IN_DEVELOPMENT":
        raise HTTPException(409, detail={"error": "wrong_status",
            "message": "Only a job card that is IN_DEVELOPMENT can be promoted",
            "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "CLOSE")
    promote_phase_id = payload.get("promote_phase_id")
    if promote_phase_id is not None:
        await _fetch_phase(conn, dev_jc_id, promote_phase_id)   # 404 if not on this card
    return await pas.open_promote_request(conn, dev_jc_id, payload=payload, user=user)


async def dispatch_dev_sample(conn, dev_jc_id: int, *, recipient: str | None, qty, user,
                              article_id: int | None = None, uom: str | None = None) -> dict:
    """Section 2 Step C — issue the developed FG sample out of the R&D location (265) to a
    recipient, IN PARTS. A CLOSED card's finalized output is dispatched over multiple partial
    outs — each is one npd_dev_dispatch row + one 265 Goods Issue + its own outpass.

    Per-article card (083): pass article_id to dispatch THAT article's own finalized output
    from ITS own FG batch, bounded by ITS own output_qty. A single-product card omits it (the
    card-level FG batch + the 046 dispatch_* mirror). The SELECT … FOR UPDATE on the card
    serializes the sum-check either way. Omit qty to dispatch the whole remaining balance.

    uom (084) is the custom unit for THIS part (else the article/card uom). ponytail: it's a
    per-part label used on the 265 doc + the outpass — the qty is NOT unit-converted, so the
    remaining balance stays in the article's output unit and the bound is numeric."""
    async with conn.transaction():
        jc = await conn.fetchrow(
            "SELECT * FROM npd_dev_job_cards WHERE id = $1 FOR UPDATE", dev_jc_id)
        if not jc:
            raise HTTPException(404, detail={"error": "not_found",
                                             "message": f"NPD development job card {dev_jc_id} not found",
                                             "details": {"id": dev_jc_id}})
        if jc["status"] != "CLOSED":
            raise HTTPException(409, detail={"error": "wrong_status",
                                             "message": "Only a CLOSED development job card can dispatch its sample",
                                             "details": {"status": jc["status"]}})
        if article_id is not None:
            # This article's own FG batch + output balance (Σ dispatch scoped to the article).
            art = await conn.fetchrow(
                "SELECT * FROM npd_dev_job_card_articles WHERE article_id = $1 AND dev_jc_id = $2",
                article_id, dev_jc_id)
            if not art:
                raise HTTPException(404, detail={"error": "article_not_found",
                                                 "message": "That article is not on this job card",
                                                 "details": {"article_id": article_id, "dev_jc_id": dev_jc_id}})
            batch_id = art["fg_sample_batch_id"]
            sku_name = art["name"]
            src_uom = art["output_uom"] or jc["uom"] or "kg"
            output_qty = float(art["output_qty"] or 0)
            if not batch_id:
                raise HTTPException(422, detail={"error": "no_fg_sample",
                                                 "message": f"Article '{art['name']}' has no FG sample to dispatch — promote with an output quantity first",
                                                 "details": {"article_id": article_id}})
            dispatched = float(await conn.fetchval(
                "SELECT COALESCE(SUM(qty), 0) FROM npd_dev_dispatch WHERE dev_jc_id = $1 AND article_id = $2",
                dev_jc_id, article_id))
        else:
            # A per-article card must dispatch per article (its card-level output_qty is the
            # SUM across articles but fg_sample_batch_id is only article #1's batch — a
            # card-level dispatch would over-issue that one batch against the total). Require
            # article_id so the balance + batch resolve to the right article.
            if await _is_per_article(conn, dev_jc_id):
                raise HTTPException(422, detail={"error": "article_required",
                                                 "message": "This card develops per-article outputs — dispatch each article (article_id required)",
                                                 "details": {"dev_jc_id": dev_jc_id}})
            if not jc["fg_sample_batch_id"]:
                raise HTTPException(422, detail={"error": "no_fg_sample",
                                                 "message": "No FG sample to dispatch — close with an output quantity first",
                                                 "details": {"id": dev_jc_id}})
            batch_id = jc["fg_sample_batch_id"]
            sku_name = jc["fg_sku_name"] or jc["title"]
            src_uom = jc["uom"] or "kg"
            output_qty = float(jc["output_qty"] or 0)
            # NOTE: no article_id in this SQL — the legacy path must work when 083 is unapplied.
            dispatched = float(await conn.fetchval(
                "SELECT COALESCE(SUM(qty), 0) FROM npd_dev_dispatch WHERE dev_jc_id = $1", dev_jc_id))
            # Legacy safety net: a card dispatched under the old single-shot 046 model has
            # dispatched_at + dispatch_qty set but no ledger row (if the 078 backfill was
            # not run). Count the mirror so remaining can't overstate → no duplicate GI.
            if dispatched == 0 and jc["dispatched_at"] is not None and jc["dispatch_qty"]:
                dispatched = float(jc["dispatch_qty"])
        remaining = output_qty - dispatched
        # Normalize to the ledger's 3-dp storage granularity so a value that rounds
        # to 0.000 is rejected here (not by the qty>0 CHECK as a 500); and treat an
        # OMITTED qty (None) — NOT an explicit 0 — as "dispatch the whole balance".
        q = round(float(qty) if qty is not None else remaining, 3)
        if q <= 0:
            raise HTTPException(422, detail={"error": "invalid_qty",
                                             "message": "Dispatch quantity must be greater than zero",
                                             "details": {"qty": q, "remaining": round(remaining, 3)}})
        if q > remaining + 1e-6:
            raise HTTPException(422, detail={"error": "over_dispatch",
                                             "message": f"Cannot dispatch {round(q, 3)} — only {round(remaining, 3)} remains of the finalized output",
                                             "details": {"requested": q, "remaining": round(remaining, 3),
                                                         "output_qty": output_qty, "dispatched": dispatched}})
        # Custom unit for this part (084) — the label on the 265 doc + the outpass; qty is
        # NOT converted (see the docstring). Falls back to the article/card uom.
        disp_uom = (uom or src_uom or "kg").strip() or src_uom
        # 265 Goods Issue for THIS part out of the R&D location (from the resolved batch).
        res = await inv.issue_named_batch(
            conn, batch_id=batch_id, sku_name=sku_name,
            qty_kg=q, reference_id=dev_jc_id, reference_type="NPD_DEV_JC", entity=_BOM_ENTITY,
            uom=disp_uom, to_location=(recipient or "SAMPLE_OUT"), user=user,
            notes=f"Dev sample partial dispatch ({jc['id']}) to {recipient or '-'}")
        # seq is global per card (UNIQUE(dev_jc_id, seq)) — the outpass sub-number.
        seq = int(await conn.fetchval(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM npd_dev_dispatch WHERE dev_jc_id = $1", dev_jc_id))
        # Persist the custom uom only if 084 is applied (the column exists) — else the row
        # is written without it and the outpass falls back to the article uom. No exception,
        # so the legacy/083-only path is unaffected.
        has_uom = bool(await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'npd_dev_dispatch' AND column_name = 'uom'"))
        # dispatch_id is an app-supplied 8-digit time-based BIGINT (new_short_time_id),
        # the same handle pattern as the phase/card ids. Retry on the rare collision.
        # Per-article rows carry article_id (083); the legacy INSERT omits the column so it
        # keeps working when 083 is unapplied.
        dispatch_id = None
        for _attempt in range(5):
            cand = new_short_time_id()
            try:
                async with conn.transaction():
                    if article_id is not None and has_uom:
                        await conn.execute(
                            """INSERT INTO npd_dev_dispatch
                                   (dispatch_id, dev_jc_id, article_id, seq, qty, uom, recipient, mat_doc_id, dispatched_by)
                               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                            cand, dev_jc_id, article_id, seq, q, disp_uom, recipient, res["mat_doc_id"], user.user_id)
                    elif article_id is not None:
                        await conn.execute(
                            """INSERT INTO npd_dev_dispatch
                                   (dispatch_id, dev_jc_id, article_id, seq, qty, recipient, mat_doc_id, dispatched_by)
                               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                            cand, dev_jc_id, article_id, seq, q, recipient, res["mat_doc_id"], user.user_id)
                    else:
                        await conn.execute(
                            """INSERT INTO npd_dev_dispatch
                                   (dispatch_id, dev_jc_id, seq, qty, recipient, mat_doc_id, dispatched_by)
                               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                            cand, dev_jc_id, seq, q, recipient, res["mat_doc_id"], user.user_id)
                dispatch_id = cand
                break
            except asyncpg.UniqueViolationError:
                if _attempt == 4:
                    raise
        # Mirror the latest partial onto the card (046 columns) — single-product card only.
        # A per-article card's card-level dispatch_* would be ambiguous (which article?), so
        # its per-article dispatch balances are read from the ledger rows instead.
        if article_id is None:
            await conn.execute(
                """UPDATE npd_dev_job_cards
                      SET dispatched_at = NOW(), dispatched_by = $1, dispatch_recipient = $2,
                          dispatch_qty = $3, dispatch_mat_doc_id = $4, updated_at = NOW()
                    WHERE id = $5""",
                user.user_id, recipient, q, res["mat_doc_id"], dev_jc_id)
    # Close this dispatch out in the job card's mail trail, AFTER commit — the dispatch
    # summary plus a link to THIS partial out's Delivery Challan + Gate Pass. Threads into
    # the source requisition's trail when the card came from one. Best-effort: a mail
    # failure must never undo a recorded goods issue.
    try:
        from app.modules.sample.services import sample_mail_service as mail
        await mail.notify_dev_dispatch_email(
            conn, dev_jc_id=dev_jc_id, dispatch_id=dispatch_id, seq=seq, qty=q,
            uom=disp_uom, recipient=recipient,
            actor_user_id=getattr(user, "user_id", None),
            actor_name=getattr(user, "full_name", None))
    except Exception:  # noqa: BLE001
        logger.exception("Dispatch DC email failed for dev JC %s", dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


async def cancel_dev_job_card(conn, dev_jc_id: int, *, reason: str, user) -> dict:
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] in ("CLOSED", "CANCELLED"):
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Job card is already finalised",
                                         "details": {"status": jc["status"]}})
    async with conn.transaction():
        await conn.execute(
            """UPDATE npd_dev_job_cards
                  SET status = 'CANCELLED', cancellation_reason = $1, updated_at = NOW()
                WHERE id = $2""",
            reason, dev_jc_id)
        # Void any pending promote request so it can never later finalize against a
        # cancelled card (the dual-approval gate would otherwise still be clearable).
        await conn.execute(
            "UPDATE npd_dev_promote_request SET status='VOID', decided_at=NOW() "
            "WHERE dev_jc_id=$1 AND status='PENDING'", dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)
