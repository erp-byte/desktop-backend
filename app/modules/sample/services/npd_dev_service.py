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

Numbering follows the house _gen_id pattern (NPDJC-YYYYMMDD-NNNN via seq_npd_dev_jc).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

from app.modules.sample.services import npd_auth
from app.modules.sample.services import sample_inventory_service as inv

# Live BOMs minted on promotion carry the legal entity axis; the sample module
# stays on 'cfpl' (the physical-warehouse axis lives only on sample tables).
_BOM_ENTITY = "cfpl"


def _gen_dev_jc_number(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"NPDJC-{d}-{seq_val:04d}"


async def _fetch(conn, dev_jc_id: int) -> dict:
    row = await conn.fetchrow("SELECT * FROM npd_dev_job_cards WHERE id = $1", dev_jc_id)
    if not row:
        raise HTTPException(404, detail={"error": "not_found",
                                         "message": f"NPD development job card {dev_jc_id} not found",
                                         "details": {"id": dev_jc_id}})
    return dict(row)


async def _insert_lines(conn, dev_jc_id: int, lines: list[dict]) -> None:
    for i, ln in enumerate(lines):
        await conn.execute(
            """
            INSERT INTO npd_dev_job_card_lines
                (dev_jc_id, sku_id, sku_name, qty, uom, item_type,
                 ownership, is_off_master, customer_lot_ref, received_qty, line_order, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            dev_jc_id, ln.get("sku_id"), ln["sku_name"], ln["qty"], ln["uom"], ln.get("item_type"),
            ln.get("ownership", "OWN"), ln.get("is_off_master", False),
            ln.get("customer_lot_ref"), ln.get("received_qty"),
            ln.get("line_order", i), ln.get("notes"))


async def create_dev_job_card(conn, *, payload: dict, user) -> dict:
    """Create a standalone NPD development job card (optionally cloning a base BOM)."""
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    base_bom_id = payload.get("base_bom_id")
    async with conn.transaction():
        seq = await conn.fetchval("SELECT nextval('seq_npd_dev_jc')")
        number = _gen_dev_jc_number(seq)
        dev_jc_id = await conn.fetchval(
            """
            INSERT INTO npd_dev_job_cards
                (dev_jc_number, title, description, warehouse, base_bom_id,
                 fg_sku_id, fg_sku_name, target_qty, uom, status, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'DRAFT', $10)
            RETURNING id
            """,
            number, payload["title"], payload.get("description"), payload.get("warehouse"),
            base_bom_id, payload.get("fg_sku_id"), payload.get("fg_sku_name"),
            payload.get("target_qty"), payload.get("uom"), user.user_id)

        if payload.get("clone_from_base") and base_bom_id:
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


async def get_dev_job_card(conn, dev_jc_id: int) -> dict:
    jc = await _fetch(conn, dev_jc_id)
    lines = await conn.fetch(
        "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 ORDER BY line_order, id", dev_jc_id)
    jc["lines"] = [dict(r) for r in lines]
    # Resolve the base BOM's FG name for the detail header (lineage clarity).
    jc["base_bom_name"] = (
        await conn.fetchval("SELECT fg_sku_name FROM bom_header WHERE bom_id = $1", jc["base_bom_id"])
        if jc.get("base_bom_id") else None)
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
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] != "DRAFT":
        raise HTTPException(409, detail={"error": "not_editable",
                                         "message": "Recipe lines can only be edited while the job card is a DRAFT",
                                         "details": {"status": jc["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    async with conn.transaction():
        await conn.execute("DELETE FROM npd_dev_job_card_lines WHERE dev_jc_id = $1", dev_jc_id)
        await _insert_lines(conn, dev_jc_id, lines)
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


async def close_dev_job_card(conn, dev_jc_id: int, *, payload: dict, user) -> dict:
    """IN_DEVELOPMENT -> CLOSED: record the trial output AND promote the recipe
    into a live bom_header + bom_line. Returns the closed job card with its
    promoted_bom_id set."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] != "IN_DEVELOPMENT":
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Only a job card that is IN_DEVELOPMENT can be closed",
                                         "details": {"status": jc["status"]}})
    # Close both records output and promotes the recipe → gate on CLOSE.
    await npd_auth.require_npd_authorized(conn, user, "CLOSE")
    lines = await conn.fetch(
        "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 ORDER BY line_order, id", dev_jc_id)
    if not lines:
        raise HTTPException(422, detail={"error": "empty_recipe",
                                         "message": "Cannot close — the development recipe has no lines to promote",
                                         "details": {"id": dev_jc_id}})

    fg_name = jc["fg_sku_name"] or jc["title"]
    out_qty = payload.get("output_qty")
    rm_consumed = payload.get("rm_consumed_qty")
    # Auto yield % = FG output / RM consumed × 100 (the job-card material-balance
    # yield). Falls back to any explicitly-provided yield_pct when RM consumed is
    # absent/zero.
    yield_pct = payload.get("yield_pct")
    if out_qty is not None and rm_consumed not in (None, 0) and float(rm_consumed) > 0:
        yield_pct = round(float(out_qty) / float(rm_consumed) * 100, 2)
    async with conn.transaction():
        # Promote the trial recipe into a live BOM (mirrors npd_service.promote_draft
        # minus the requisition BH gate — there is no requisition here).
        new_bom_id = await conn.fetchval(
            """
            INSERT INTO bom_header (fg_sku_name, version, is_active, entity, notes)
            VALUES ($1, 1, TRUE, $2, $3)
            RETURNING bom_id
            """,
            fg_name, _BOM_ENTITY,
            f"Promoted from NPD development job card {jc['dev_jc_number']}")
        for i, ln in enumerate(lines, 1):
            await conn.execute(
                """
                INSERT INTO bom_line
                    (bom_id, line_number, material_sku_name, item_type, quantity_per_unit, uom)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                new_bom_id, i, ln["sku_name"], ln["item_type"] or "rm", ln["qty"], ln["uom"])

        # Step B (NPD plan §1) — receive the finished trial sample into the R&D
        # location when an output quantity was recorded.
        fg_batch_id = None
        if out_qty and float(out_qty) > 0:
            recv = await inv.receive_fg_sample(
                conn, sku_name=fg_name, qty_kg=float(out_qty),
                reference_type="NPD_DEV_JC", reference_id=dev_jc_id, entity=_BOM_ENTITY,
                uom=(payload.get("output_uom") or jc["uom"] or "kg"),
                lot_number=jc["dev_jc_number"], user=user)
            fg_batch_id = recv["batch_id"]

        await conn.execute(
            """
            UPDATE npd_dev_job_cards
               SET status = 'CLOSED', promoted_bom_id = $1,
                   output_qty = $2, output_uom = $3, yield_pct = $4, output_notes = $5,
                   rm_consumed_qty = $6, wastage_qty = $7, extra_give_away_qty = $8,
                   fg_sample_batch_id = $9,
                   closed_by = $10, closed_at = NOW(), updated_at = NOW()
             WHERE id = $11
            """,
            new_bom_id, payload.get("output_qty"), payload.get("output_uom"),
            yield_pct, payload.get("output_notes"),
            payload.get("rm_consumed_qty"), payload.get("wastage_qty"),
            payload.get("extra_give_away_qty"), fg_batch_id,
            user.user_id, dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


async def dispatch_dev_sample(conn, dev_jc_id: int, *, recipient: str | None, qty, user) -> dict:
    """Section 2 Step C — issue the developed FG sample out of the R&D location
    (265) to a recipient. The card stays CLOSED; dispatch_* columns record it."""
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] != "CLOSED":
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Only a CLOSED development job card can dispatch its sample",
                                         "details": {"status": jc["status"]}})
    if not jc.get("fg_sample_batch_id"):
        raise HTTPException(422, detail={"error": "no_fg_sample",
                                         "message": "No FG sample to dispatch — close with an output quantity first",
                                         "details": {"id": dev_jc_id}})
    if jc.get("dispatched_at"):
        raise HTTPException(409, detail={"error": "already_dispatched",
                                         "message": "This sample has already been dispatched",
                                         "details": {"id": dev_jc_id}})
    q = float(qty) if qty else float(jc.get("output_qty") or 0)
    async with conn.transaction():
        res = await inv.issue_named_batch(
            conn, batch_id=jc["fg_sample_batch_id"], sku_name=(jc["fg_sku_name"] or jc["title"]),
            qty_kg=q, reference_id=dev_jc_id, reference_type="NPD_DEV_JC", entity=_BOM_ENTITY,
            uom=(jc["uom"] or "kg"), to_location=(recipient or "SAMPLE_OUT"), user=user,
            notes=f"Dev sample dispatch ({jc['dev_jc_number']}) to {recipient or '-'}")
        await conn.execute(
            """UPDATE npd_dev_job_cards
                  SET dispatched_at = NOW(), dispatched_by = $1, dispatch_recipient = $2,
                      dispatch_qty = $3, dispatch_mat_doc_id = $4, updated_at = NOW()
                WHERE id = $5""",
            user.user_id, recipient, q, res["mat_doc_id"], dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)


async def cancel_dev_job_card(conn, dev_jc_id: int, *, reason: str, user) -> dict:
    jc = await _fetch(conn, dev_jc_id)
    if jc["status"] in ("CLOSED", "CANCELLED"):
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Job card is already finalised",
                                         "details": {"status": jc["status"]}})
    await conn.execute(
        """UPDATE npd_dev_job_cards
              SET status = 'CANCELLED', cancellation_reason = $1, updated_at = NOW()
            WHERE id = $2""",
        reason, dev_jc_id)
    return await get_dev_job_card(conn, dev_jc_id)
