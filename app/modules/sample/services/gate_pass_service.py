"""Sample gate passes on the generic gate_passes table (checklist A9 / spec §6, §9.4, §10).

Issuance: READY_FOR_DISPATCH -> GATE_PASS_ISSUED. Inventory manager verifies
(INV_MGR_VERIFICATION) then signs off (INV_MGR_SIGNOFF); the gate pass row is
written inside the status transaction, PDF rendering is a separate concern
(spec §9.4 — the DB row is the source of truth, never the PDF). Numbering uses
the house _gen_id pattern: GP-YYYYMMDD-NNNN via seq_gate_pass.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

from app.modules.production.services.material_document_service import MVT_GI_SAMPLE
from app.modules.sample.services import audit_service, notification_service
from app.modules.sample.services import approval_service as appr
from app.modules.sample.services import requisition_service as req_svc


def _gen_gate_pass_number(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"GP-{d}-{seq_val:04d}"


async def _bh_approver(conn, req_id: int) -> int | None:
    return await conn.fetchval(
        """SELECT approver_user_id FROM sample_approvals
            WHERE requisition_id = $1 AND approval_stage = 'BH_APPROVAL' AND action = 'APPROVED'
            ORDER BY sequence_no DESC LIMIT 1""", req_id)


async def _sample_material_doc_id(conn, req_id: int) -> int | None:
    return await conn.fetchval(
        "SELECT id FROM material_document WHERE sample_requisition_id = $1 AND movement_type = $2 "
        "ORDER BY id DESC LIMIT 1", req_id, MVT_GI_SAMPLE)


async def inv_verify(conn, req_id: int, *, user, remarks: str | None = None) -> dict:
    """Record INV_MGR_VERIFICATION (no status change; logs + notifies BH)."""
    async with conn.transaction():
        req = await conn.fetchrow(
            "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", req_id)
        if req is None:
            raise HTTPException(404, detail={"error": "not_found",
                                             "message": f"Sample requisition {req_id} not found",
                                             "details": {"id": req_id}})
        if req["status"] != "READY_FOR_DISPATCH":
            raise HTTPException(409, detail={"error": "not_ready",
                                             "message": "Requisition is not READY_FOR_DISPATCH",
                                             "details": {"status": req["status"]}})
        await appr.record_action(conn, req_id, approval_stage=appr.INV_MGR_VERIFICATION,
                                 action="APPROVED", user=user, remarks=remarks)
        await notification_service.emit_alert(
            conn, alert_type="sample_inv_mgr_verified",
            target_team=notification_service.TEAM_BUSINESS,
            message=f"Sample {req['request_id']} verified by inventory manager.",
            related_id=req_id)
    return await req_svc.get_requisition(conn, req_id)


async def issue_gate_pass(conn, req_id: int, *, user, recipient_name: str | None = None,
                          recipient_contact: str | None = None, vehicle_carrier: str | None = None,
                          driver_name: str | None = None, from_location: str | None = None) -> dict:
    """INV_MGR_SIGNOFF + write gate pass row + GATE_PASS_ISSUED (spec §9.1)."""
    async with conn.transaction():
        req = await conn.fetchrow(
            "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", req_id)
        if req is None:
            raise HTTPException(404, detail={"error": "not_found",
                                             "message": f"Sample requisition {req_id} not found",
                                             "details": {"id": req_id}})
        if req["status"] != "READY_FOR_DISPATCH":
            raise HTTPException(409, detail={"error": "not_ready",
                                             "message": "Requisition is not READY_FOR_DISPATCH",
                                             "details": {"status": req["status"]}})

        await appr.record_action(conn, req_id, approval_stage=appr.INV_MGR_SIGNOFF,
                                 action="APPROVED", user=user)
        gp_id = await _create_gate_pass_row(
            conn, req=dict(req), user=user, recipient_name=recipient_name,
            recipient_contact=recipient_contact, vehicle_carrier=vehicle_carrier,
            driver_name=driver_name, from_location=from_location,
            converted_from_internal=False, conversion_qty=None,
            original_requisition_id=None)
        await req_svc.transition_status(
            conn, req_id, target="GATE_PASS_ISSUED", user=user,
            remarks="Gate pass issued", extra={"linked_gate_pass_id": gp_id})
        await notification_service.emit_alert(
            conn, alert_type="sample_gate_pass_issued",
            target_team=notification_service.TEAM_BUSINESS,
            message=f"Gate pass issued for sample {req['request_id']}.",
            related_id=gp_id, related_type=notification_service.REL_SAMPLE_GATE_PASS)
    return await get_gate_pass(conn, gp_id)


async def _create_gate_pass_row(conn, *, req: dict, user, recipient_name, recipient_contact,
                                vehicle_carrier, driver_name, from_location,
                                converted_from_internal: bool, conversion_qty,
                                original_requisition_id) -> int:
    """Insert a generic gate_passes row + the sample 1:1 detail companion."""
    seq = await conn.fetchval("SELECT nextval('seq_gate_pass')")
    number = _gen_gate_pass_number(seq)
    bh_id = await _bh_approver(conn, req["id"])
    md_id = await _sample_material_doc_id(conn, req["id"])

    gp_id = await conn.fetchval(
        """
        INSERT INTO gate_passes
            (gate_pass_number, gate_pass_type, source_ref_type, source_ref_id,
             material_document_id, from_location, to_location, recipient_name,
             recipient_contact, vehicle_carrier, driver_name, issued_by,
             approver1_user_id, approver1_at, approver2_user_id, approver2_at, warehouse)
        VALUES ($1, 'SAMPLE', 'SAMPLE_REQ', $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, NOW(), $12, NOW(), $13)
        RETURNING id
        """,
        number, req["id"], md_id, from_location, None, recipient_name,
        recipient_contact, vehicle_carrier, driver_name, user.user_id,
        bh_id, user.user_id, req["warehouse"])
    await conn.execute(
        """
        INSERT INTO gate_pass_sample_details
            (gate_pass_id, requisition_id, original_requisition_id, sample_type,
             purpose_tag, purpose_note, converted_from_internal, conversion_qty)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        gp_id, req["id"], original_requisition_id, req["sample_type"],
        req["purpose_tag"], req["purpose_note"], converted_from_internal, conversion_qty)
    if md_id is not None:
        await conn.execute(
            "UPDATE material_document SET gate_pass_id = $1 WHERE id = $2", gp_id, md_id)
    await audit_service.write_audit(
        conn, req["id"], audit_service.EV_GATE_PASS,
        new_value={"gate_pass_id": gp_id, "gate_pass_number": number},
        actor_user_id=user.user_id, actor_role=user.role_name,
        remarks=f"Gate pass {number} created")
    return gp_id


async def get_gate_pass(conn, gate_pass_id: int) -> dict:
    gp = await conn.fetchrow("SELECT * FROM gate_passes WHERE id = $1", gate_pass_id)
    if gp is None:
        raise HTTPException(404, detail={"error": "not_found",
                                         "message": f"Gate pass {gate_pass_id} not found",
                                         "details": {"id": gate_pass_id}})
    details = await conn.fetchrow(
        "SELECT * FROM gate_pass_sample_details WHERE gate_pass_id = $1", gate_pass_id)
    out = dict(gp)
    out["sample_details"] = dict(details) if details else None
    return out


async def register_print(conn, gate_pass_id: int, *, user) -> dict:
    """Atomically bump print_count + last_printed_at (spec §9.3). PDF render is
    a separate step in the router; the DB row stays the source of truth."""
    gp = await conn.fetchrow(
        "UPDATE gate_passes SET print_count = print_count + 1, last_printed_at = NOW() "
        "WHERE id = $1 RETURNING *", gate_pass_id)
    if gp is None:
        raise HTTPException(404, detail={"error": "not_found",
                                         "message": f"Gate pass {gate_pass_id} not found",
                                         "details": {"id": gate_pass_id}})
    sd = await conn.fetchrow(
        "SELECT requisition_id FROM gate_pass_sample_details WHERE gate_pass_id = $1", gate_pass_id)
    if sd:
        await audit_service.write_audit(
            conn, sd["requisition_id"], audit_service.EV_GATE_PASS_PRINT,
            new_value={"gate_pass_id": gate_pass_id, "print_count": gp["print_count"]},
            actor_user_id=user.user_id, actor_role=user.role_name)
    return await get_gate_pass(conn, gate_pass_id)


async def void_gate_pass(conn, gate_pass_id: int, *, reason: str, user) -> dict:
    """Void a gate pass. No auto-reversal of stock — a manual 266 entry handles
    that (spec §9.5, §15.12)."""
    if not (reason or "").strip():
        raise HTTPException(422, detail={"error": "reason_required",
                                         "message": "void_reason is required",
                                         "details": {"id": gate_pass_id}})
    gp = await conn.fetchrow("SELECT * FROM gate_passes WHERE id = $1", gate_pass_id)
    if gp is None:
        raise HTTPException(404, detail={"error": "not_found",
                                         "message": f"Gate pass {gate_pass_id} not found",
                                         "details": {"id": gate_pass_id}})
    async with conn.transaction():
        await conn.execute(
            "UPDATE gate_passes SET voided = TRUE, voided_at = NOW(), voided_by = $2, void_reason = $3 "
            "WHERE id = $1", gate_pass_id, user.user_id, reason)
        sd = await conn.fetchrow(
            "SELECT requisition_id FROM gate_pass_sample_details WHERE gate_pass_id = $1", gate_pass_id)
        if sd:
            await audit_service.write_audit(
                conn, sd["requisition_id"], audit_service.EV_VOID,
                new_value={"gate_pass_id": gate_pass_id, "reason": reason},
                actor_user_id=user.user_id, actor_role=user.role_name, remarks=reason)
            await notification_service.emit_alert(
                conn, alert_type="sample_gate_pass_voided",
                target_team=notification_service.TEAM_STORES,
                message=f"Gate pass {gp['gate_pass_number']} voided: {reason}",
                related_id=gate_pass_id, related_type=notification_service.REL_SAMPLE_GATE_PASS)
    return await get_gate_pass(conn, gate_pass_id)
