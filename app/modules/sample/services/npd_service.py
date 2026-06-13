"""NPD draft BOM authoring + promotion (checklist A8 / spec §4.4–4.5, §13.5).

Draft BOMs live only in npd_draft_boms / npd_draft_bom_lines so MRP, BOM
dropdowns and production reports never see them. A draft becomes a real BOM
only via promote_draft(), which writes bom_header + bom_line and stamps
promoted_bom_id. Promotion is allowed once the parent requisition has cleared
business-head approval (the BH gate per §13.5).
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.sample.services import audit_service
from app.modules.sample.services import npd_auth
from app.modules.sample.services import requisition_service as req_svc


async def _fetch_draft(conn, draft_id: int) -> dict:
    row = await conn.fetchrow("SELECT * FROM npd_draft_boms WHERE id = $1", draft_id)
    if not row:
        raise HTTPException(404, detail={"error": "not_found",
                                         "message": f"NPD draft BOM {draft_id} not found",
                                         "details": {"id": draft_id}})
    return dict(row)


async def _insert_lines(conn, draft_id: int, lines: list[dict]) -> None:
    for i, ln in enumerate(lines):
        await conn.execute(
            """
            INSERT INTO npd_draft_bom_lines
                (draft_bom_id, sku_id, sku_name, qty, uom, item_type,
                 delta_type, original_qty, ownership, is_off_master,
                 customer_lot_ref, received_qty, line_order, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            """,
            draft_id, ln.get("sku_id"), ln["sku_name"], ln["qty"], ln["uom"],
            ln.get("item_type"), ln.get("delta_type", "UNCHANGED"), ln.get("original_qty"),
            ln.get("ownership", "OWN"), ln.get("is_off_master", False),
            ln.get("customer_lot_ref"), ln.get("received_qty"),
            ln.get("line_order", i), ln.get("notes"))


async def create_draft_bom(conn, req_id: int, *, payload: dict, user) -> dict:
    """Create an NPD draft BOM for a requisition (optionally cloned from base)."""
    req = req_svc._require(await req_svc._fetch_req(conn, req_id), req_id)
    if req["sample_type"] not in ("NPD", "TRIAL"):
        raise HTTPException(409, detail={"error": "wrong_flow",
                                         "message": "Draft BOMs apply only to NPD / TRIAL samples",
                                         "details": {"sample_type": req["sample_type"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    base_bom_id = payload.get("base_bom_id") or req["base_bom_id"]
    async with conn.transaction():
        draft_id = await conn.fetchval(
            """
            INSERT INTO npd_draft_boms
                (requisition_id, base_bom_id, fg_sku_id, fg_sku_name, description,
                 status, created_by)
            VALUES ($1, $2, $3, $4, $5, 'DRAFT', $6)
            RETURNING id
            """,
            req_id, base_bom_id, payload.get("fg_sku_id"),
            payload.get("fg_sku_name"), payload.get("description"), user.user_id)

        if payload.get("clone_from_base") and base_bom_id:
            base_lines = await conn.fetch(
                "SELECT * FROM bom_line WHERE bom_id = $1 ORDER BY line_number", base_bom_id)
            # bom_line is name-based (no sku_id), so cloned lines carry a NULL
            # sku_id + the material_sku_name snapshot. npd_draft_bom_lines.sku_id
            # is nullable for exactly this case.
            await _insert_lines(conn, draft_id, [
                {"sku_id": None, "sku_name": bl["material_sku_name"],
                 "qty": float(bl["quantity_per_unit"]), "uom": bl["uom"] or "kg",
                 "item_type": bl["item_type"], "delta_type": "UNCHANGED",
                 "line_order": bl["line_number"]} for bl in base_lines])
        elif payload.get("lines"):
            await _insert_lines(conn, draft_id, payload["lines"])

        await conn.execute(
            "UPDATE sample_requisitions SET npd_draft_bom_id = $1, updated_at = NOW() WHERE id = $2",
            draft_id, req_id)
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_NPD,
            new_value={"draft_bom_id": draft_id},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="NPD draft BOM created")
    return await get_draft_bom(conn, draft_id)


async def get_draft_bom(conn, draft_id: int) -> dict:
    draft = await _fetch_draft(conn, draft_id)
    lines = await conn.fetch(
        "SELECT * FROM npd_draft_bom_lines WHERE draft_bom_id = $1 ORDER BY line_order, id", draft_id)
    draft["lines"] = [dict(r) for r in lines]
    return draft


async def replace_lines(conn, draft_id: int, *, lines: list[dict], user) -> dict:
    draft = await _fetch_draft(conn, draft_id)
    if draft["status"] not in ("DRAFT",):
        raise HTTPException(409, detail={"error": "not_editable",
                                         "message": "Only DRAFT BOMs can be edited",
                                         "details": {"status": draft["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "AUTHOR")
    async with conn.transaction():
        await conn.execute("DELETE FROM npd_draft_bom_lines WHERE draft_bom_id = $1", draft_id)
        await _insert_lines(conn, draft_id, lines)
        await audit_service.write_audit(
            conn, draft["requisition_id"], audit_service.EV_NPD,
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks=f"NPD draft BOM {draft_id} lines updated")
    return await get_draft_bom(conn, draft_id)


async def promote_draft(conn, draft_id: int, *, user) -> dict:
    """Promote a DRAFT BOM into a live bom_header + bom_line (spec §13.5)."""
    draft = await _fetch_draft(conn, draft_id)
    if draft["status"] != "DRAFT":
        raise HTTPException(409, detail={"error": "not_promotable",
                                         "message": "Only DRAFT BOMs can be promoted",
                                         "details": {"status": draft["status"]}})
    await npd_auth.require_npd_authorized(conn, user, "PROMOTE")
    req = req_svc._require(await req_svc._fetch_req(conn, draft["requisition_id"]),
                           draft["requisition_id"])
    # BH gate: the parent requisition must have cleared business-head approval.
    if req["status"] in ("DRAFT", "SUBMITTED", "BH_REJECTED", "CANCELLED"):
        raise HTTPException(409, detail={
            "error": "bh_gate",
            "message": "Promotion requires the requisition to be BH-approved",
            "details": {"status": req["status"]}})

    lines = await conn.fetch(
        "SELECT * FROM npd_draft_bom_lines WHERE draft_bom_id = $1 ORDER BY line_order, id", draft_id)
    if not lines:
        raise HTTPException(422, detail={"error": "empty_bom",
                                         "message": "Cannot promote a draft with no lines",
                                         "details": {"id": draft_id}})

    # Link the requisition's BH_APPROVAL row as the promotion approval, if any.
    approval_id = await conn.fetchval(
        """SELECT id FROM sample_approvals
            WHERE requisition_id = $1 AND approval_stage = 'BH_APPROVAL' AND action = 'APPROVED'
            ORDER BY sequence_no DESC LIMIT 1""", req["id"])

    async with conn.transaction():
        # Supersede any existing active BOM for this FG and mint the next version —
        # only one active BOM per fg_sku_name is allowed (uq_bom_header_active_fg),
        # so always inserting version 1 active would 500 on a repeated FG name.
        fg_name = draft["fg_sku_name"] or f"NPD-{draft_id}"
        entity = req.get("entity") or "cfpl"
        await conn.execute(
            "UPDATE bom_header SET is_active = FALSE WHERE fg_sku_name = $1 AND is_active = TRUE", fg_name)
        next_ver = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM bom_header WHERE fg_sku_name = $1", fg_name)
        new_bom_id = await conn.fetchval(
            """
            INSERT INTO bom_header (fg_sku_name, customer_name, version, is_active, entity, notes)
            VALUES ($1, $2, $3, TRUE, $4, $5)
            RETURNING bom_id
            """,
            fg_name, None, next_ver, entity,
            f"Promoted from NPD draft {draft_id} (requisition {req['request_id']}, v{next_ver})")
        for i, ln in enumerate(lines, 1):
            await conn.execute(
                """
                INSERT INTO bom_line
                    (bom_id, line_number, material_sku_name, item_type,
                     quantity_per_unit, uom)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                new_bom_id, i, ln["sku_name"], ln["item_type"] or "rm",
                ln["qty"], ln["uom"])
        await conn.execute(
            """UPDATE npd_draft_boms
                  SET status='PROMOTED', promoted_bom_id=$1, promoted_at=NOW(),
                      promoted_by=$2, promotion_approval_id=$3
                WHERE id=$4""",
            new_bom_id, user.user_id, approval_id, draft_id)
        await audit_service.write_audit(
            conn, req["id"], audit_service.EV_NPD,
            new_value={"promoted_bom_id": new_bom_id, "draft_bom_id": draft_id},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="NPD draft BOM promoted to live BOM")
    return await get_draft_bom(conn, draft_id)
