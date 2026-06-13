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

import asyncpg
from fastapi import HTTPException

from app.core.helpers import new_short_time_id
from app.modules.sample.services import npd_auth
from app.modules.sample.services import sample_inventory_service as inv

# Live BOMs minted on promotion carry the legal entity axis; the sample module
# stays on 'cfpl' (the physical-warehouse axis lives only on sample tables).
_BOM_ENTITY = "cfpl"

# Customer + dispatch-planning columns shared by sample_requisitions and
# npd_dev_job_cards — the card inherits them from the requisition it spawns.
_DISPATCH_FIELDS = (
    "company_name", "customer_name", "customer_contact", "customer_ship_to_address",
    "mode_of_transport", "expected_dispatch_date", "confirmed_dispatch_date",
)


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


async def _insert_lines(conn, dev_jc_id: int, lines: list[dict], *, phase_id=None) -> None:
    """Insert recipe lines. phase_id IS NULL = the card base recipe; a phase_id
    ties the lines to one trial phase's recipe."""
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


async def create_dev_job_card(conn, *, payload: dict, user) -> dict:
    """Create a standalone NPD development job card (optionally cloning a base BOM)."""
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    base_bom_id = payload.get("base_bom_id")
    async with conn.transaction():
        seq = await conn.fetchval("SELECT nextval('seq_npd_dev_jc')")
        number = _gen_dev_jc_number(seq)
        src_req = payload.get("source_requisition_id")
        # Customer + dispatch planning is attached to the job card: inherit each
        # field from the source requisition (the explicit payload value wins) so a
        # card developed from a request carries its company / customer / dispatch
        # plan automatically.
        inherit_cols = _DISPATCH_FIELDS + ("pcs", "weight_per_piece")
        cust = {k: payload.get(k) for k in inherit_cols}
        if src_req:
            rq = await conn.fetchrow(
                f"SELECT {', '.join(inherit_cols)} FROM sample_requisitions WHERE id = $1",
                src_req)
            if rq:
                for k in inherit_cols:
                    if cust[k] is None:
                        cust[k] = rq[k]
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
                            (id, dev_jc_number, title, description, warehouse, base_bom_id,
                             fg_sku_id, fg_sku_name, target_qty, uom, source_requisition_id,
                             company_name, customer_name, customer_contact, customer_ship_to_address,
                             mode_of_transport, expected_dispatch_date, confirmed_dispatch_date,
                             pcs, weight_per_piece,
                             status, created_by)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                                $12, $13, $14, $15, $16, $17, $18, $19, $20, 'DRAFT', $21)
                        """,
                        cand, number, payload["title"], payload.get("description"),
                        payload.get("warehouse"), base_bom_id, payload.get("fg_sku_id"),
                        payload.get("fg_sku_name"), payload.get("target_qty"),
                        payload.get("uom"), src_req,
                        cust["company_name"], cust["customer_name"], cust["customer_contact"],
                        cust["customer_ship_to_address"], cust["mode_of_transport"],
                        cust["expected_dispatch_date"], cust["confirmed_dispatch_date"],
                        cust["pcs"], cust["weight_per_piece"],
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
    all_lines = [dict(r) for r in await conn.fetch(
        "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 ORDER BY line_order, id", dev_jc_id)]
    # Card base recipe = lines with no phase; each phase carries its own recipe.
    jc["lines"] = [l for l in all_lines if l.get("phase_id") is None]
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
    # Resolve the base BOM's FG name for the detail header (lineage clarity).
    jc["base_bom_name"] = (
        await conn.fetchval("SELECT fg_sku_name FROM bom_header WHERE bom_id = $1", jc["base_bom_id"])
        if jc.get("base_bom_id") else None)
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


async def _finalize_promote(conn, dev_jc_id, *, promote_phase_id, close_payload: dict, user) -> dict:
    """IN_DEVELOPMENT -> CLOSED: record the trial output AND promote the recipe
    into a live bom_header + bom_line. Returns the closed job card with its
    promoted_bom_id set.

    This is the real promote body, run only once the dual-approval gate clears
    (see promote_approval_service.finalize_if_ready). It assumes it is authorized
    to run: the IN_DEVELOPMENT gate + CLOSE auth live in request_promote, which is
    what the operator hits first. promote_phase_id comes from the arg and the
    output/accounting fields come from close_payload (the stashed close dict)."""
    jc = await _fetch(conn, dev_jc_id)
    # Promote the recipe of the operator-chosen FINAL-TRIAL phase; fall back to
    # the card base recipe (phase_id IS NULL) when no phase is given (a legacy /
    # no-phase card). When a phase is chosen the card's output + accounting are
    # INHERITED from that phase (already recorded when it was completed) — the
    # close is a "pick the final trial and finish" step, not a second accounting
    # entry. The card-level payload accounting is only used for no-phase cards.
    phase = None
    if promote_phase_id is not None:
        phase = await _fetch_phase(conn, dev_jc_id, promote_phase_id)   # 404 if not on this card
        lines = await conn.fetch(
            "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 AND phase_id = $2 "
            "ORDER BY line_order, id", dev_jc_id, promote_phase_id)
        if not lines:
            raise HTTPException(422, detail={"error": "empty_recipe",
                                             "message": "The chosen phase has no recipe lines to promote",
                                             "details": {"phase_id": promote_phase_id}})
    else:
        lines = await conn.fetch(
            "SELECT * FROM npd_dev_job_card_lines WHERE dev_jc_id = $1 AND phase_id IS NULL "
            "ORDER BY line_order, id", dev_jc_id)
        if not lines:
            raise HTTPException(422, detail={"error": "empty_recipe",
                                             "message": "Cannot close — pick a phase whose recipe to promote",
                                             "details": {"id": dev_jc_id}})

    fg_name = jc["fg_sku_name"] or jc["title"]
    if phase is not None:
        # Inherit the final-trial phase's recorded output + accounting verbatim.
        out_qty = phase["output_qty"]
        out_uom = phase["output_uom"]
        rm_consumed = phase["rm_consumed_qty"]
        wastage = phase["wastage_qty"]
        ega = phase["extra_give_away_qty"]
        yield_pct = phase["yield_pct"]
    else:
        # Legacy no-phase card — accounting comes from the close payload. Auto
        # yield % = FG output / RM consumed × 100; falls back to supplied yield_pct.
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
        # Promote the trial recipe into a live BOM. Only ONE active BOM is allowed
        # per fg_sku_name (uq_bom_header_active_fg), and (fg_sku_name, version) is
        # unique among active rows — so supersede any existing active BOM for this
        # FG and mint the NEXT version, rather than always inserting version 1
        # (which 500'd with a UniqueViolation on a repeated FG name).
        await conn.execute(
            "UPDATE bom_header SET is_active = FALSE WHERE fg_sku_name = $1 AND is_active = TRUE", fg_name)
        next_ver = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM bom_header WHERE fg_sku_name = $1", fg_name)
        try:
            new_bom_id = await conn.fetchval(
                """
                INSERT INTO bom_header (fg_sku_name, version, is_active, entity, notes)
                VALUES ($1, $2, TRUE, $3, $4)
                RETURNING bom_id
                """,
                fg_name, next_ver, _BOM_ENTITY,
                f"Promoted from NPD development job card {jc['dev_jc_number']} (v{next_ver})")
        except asyncpg.UniqueViolationError as e:
            raise HTTPException(409, detail={
                "error": "bom_conflict",
                "message": f"Couldn't promote — a live BOM for '{fg_name}' already exists. "
                           "Rename the target product or deactivate the existing BOM.",
                "details": {"fg_sku_name": fg_name}}) from e
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
                uom=(out_uom or jc["uom"] or "kg"),
                lot_number=jc["dev_jc_number"], user=user)
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
             WHERE id = $11
            """,
            new_bom_id, out_qty, out_uom, yield_pct, out_notes,
            rm_consumed, wastage, ega, fg_batch_id,
            user.user_id, dev_jc_id)
        # Mirror the confirmed dispatch date onto the source requisition so its
        # view reflects the NPD-confirmed dispatch once the trial is closed.
        if jc.get("source_requisition_id"):
            await conn.execute(
                "UPDATE sample_requisitions SET confirmed_dispatch_date = CURRENT_DATE, "
                "updated_at = NOW() WHERE id = $1", jc["source_requisition_id"])
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
