"""Internal -> external sample conversion (checklist A9/A11 / spec §6, §9.5).

Scenario A (full): an internally-dispatched sample is taken externally. The
original 265 material_document is NOT reversed or duplicated — it is flagged
converted_to_external and a gate pass is issued. No second deduction.

Scenario B (partial): only part of the internally-dispatched qty goes external.
A child requisition (converted_from_id = parent) carries the converted portion
to GATE_PASS_ISSUED; the parent becomes PARTIALLY_CONVERTED. The qty was already
deducted by the parent's 265, so no new movement is booked — and asking for MORE
than was internally issued is blocked (would need a fresh 265, spec §9.5).

A fresh CONVERSION_APPROVAL row is always written, even same-day / same-approver
(spec §15.11).
"""
from __future__ import annotations

import asyncpg
from fastapi import HTTPException

from app.core.helpers import new_short_time_id
from app.modules.production.services.material_document_service import MVT_GI_SAMPLE
from app.modules.sample.services import audit_service, notification_service
from app.modules.sample.services import approval_service as appr
from app.modules.sample.services import gate_pass_service as gp_svc
from app.modules.sample.services import requisition_service as req_svc


def _recipient(payload: dict) -> dict:
    return {
        "recipient_name": payload.get("recipient_name"),
        "recipient_contact": payload.get("recipient_contact"),
        "vehicle_carrier": payload.get("vehicle_carrier"),
        "driver_name": payload.get("driver_name"),
        "from_location": payload.get("from_location"),
    }


async def convert_full(conn, req_id: int, *, user, payload: dict | None = None) -> dict:
    """Scenario A — full conversion to a gate pass (no second deduction)."""
    payload = payload or {}
    async with conn.transaction():
        req = await conn.fetchrow(
            "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", req_id)
        if req is None:
            raise HTTPException(404, detail={"error": "not_found",
                                             "message": f"Sample requisition {req_id} not found",
                                             "details": {"id": req_id}})
        if req["status"] != "INTERNALLY_DISPATCHED":
            raise HTTPException(409, detail={"error": "not_convertible",
                                             "message": "Only INTERNALLY_DISPATCHED samples can be converted",
                                             "details": {"status": req["status"]}})

        await appr.record_action(conn, req_id, approval_stage=appr.CONVERSION_APPROVAL,
                                 action="APPROVED", user=user,
                                 remarks=payload.get("remarks"))
        # Flag the original movement; do NOT create a new one (no double deduction)
        await conn.execute(
            "UPDATE material_document SET converted_to_external = TRUE "
            "WHERE sample_requisition_id = $1 AND movement_type = $2", req_id, MVT_GI_SAMPLE)
        gp_id = await gp_svc._create_gate_pass_row(
            conn, req=dict(req), user=user, converted_from_internal=True,
            conversion_qty=None, original_requisition_id=None, **_recipient(payload))
        await req_svc.transition_status(
            conn, req_id, target="GATE_PASS_ISSUED", user=user,
            remarks="Converted internal -> external (full)",
            extra={"converted_to_external": True, "linked_gate_pass_id": gp_id})
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_CONVERSION,
            new_value={"mode": "full", "gate_pass_id": gp_id},
            actor_user_id=user.user_id, actor_role=user.role_name)
        await notification_service.emit_alert(
            conn, alert_type="sample_conversion_approved",
            target_team=notification_service.TEAM_INVENTORY,
            message=f"Sample {req['request_id']} converted to external gate pass.",
            related_id=gp_id, related_type=notification_service.REL_SAMPLE_GATE_PASS)
    return await gp_svc.get_gate_pass(conn, gp_id)


async def convert_partial(conn, req_id: int, *, qty: float, user,
                          payload: dict | None = None) -> dict:
    """Scenario B — partial conversion via a child requisition (no new movement)."""
    payload = payload or {}
    async with conn.transaction():
        parent = await conn.fetchrow(
            "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", req_id)
        if parent is None:
            raise HTTPException(404, detail={"error": "not_found",
                                             "message": f"Sample requisition {req_id} not found",
                                             "details": {"id": req_id}})
        if parent["status"] != "INTERNALLY_DISPATCHED":
            raise HTTPException(409, detail={"error": "not_convertible",
                                             "message": "Only INTERNALLY_DISPATCHED samples can be converted",
                                             "details": {"status": parent["status"]}})
        total_issued = float(await conn.fetchval(
            "SELECT COALESCE(SUM(issued_qty), 0) FROM sample_requisition_articles WHERE requisition_id = $1",
            req_id) or 0)
        if qty <= 0 or qty > total_issued:
            raise HTTPException(422, detail={
                "error": "qty_out_of_range",
                "message": "Conversion qty must be > 0 and <= the internally-issued qty "
                           "(extra qty would require a fresh 265 movement)",
                "details": {"qty": qty, "issued": total_issued}})

        await appr.record_action(conn, req_id, approval_stage=appr.CONVERSION_APPROVAL,
                                 action="APPROVED", user=user, remarks=payload.get("remarks"))

        # Child requisition carrying the converted portion. request_id is the
        # app-supplied 8-digit time-based BIGINT PK (new_short_time_id) — retry on
        # the rare unique collision via a per-attempt savepoint.
        child_id = None
        for _attempt in range(5):
            child_request_id = new_short_time_id()
            try:
                async with conn.transaction():
                    child_id = await conn.fetchval(
                        """
                        INSERT INTO sample_requisitions
                            (request_id, sample_type, status, requestor_user_id,
                             requestor_team, purpose_tag, purpose_note, warehouse,
                             converted_from_id, converted_to_external, created_by, updated_by)
                        VALUES ($1, $2, 'READY_FOR_DISPATCH', $3, $4, $5, $6, $7, $8, TRUE, $9, $9)
                        RETURNING id
                        """,
                        child_request_id, parent["sample_type"], parent["requestor_user_id"],
                        parent["requestor_team"], parent["purpose_tag"], parent["purpose_note"],
                        parent["warehouse"], req_id, user.user_id)
                break
            except asyncpg.UniqueViolationError:
                if _attempt == 4:
                    raise
        await audit_service.write_audit(
            conn, child_id, audit_service.EV_CONVERSION,
            new_value={"mode": "partial_child", "parent": req_id, "qty": qty},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Partial-conversion child created")

        # Gate pass for the child (no material doc -> no new deduction).
        child = await conn.fetchrow("SELECT * FROM sample_requisitions WHERE id = $1", child_id)
        gp_id = await gp_svc._create_gate_pass_row(
            conn, req=dict(child), user=user, converted_from_internal=True,
            conversion_qty=qty, original_requisition_id=req_id, **_recipient(payload))
        await req_svc.transition_status(
            conn, child_id, target="GATE_PASS_ISSUED", user=user,
            remarks="Partial conversion gate pass", extra={"linked_gate_pass_id": gp_id})

        # Parent -> PARTIALLY_CONVERTED
        await req_svc.transition_status(
            conn, req_id, target="PARTIALLY_CONVERTED", user=user,
            remarks=f"Partial conversion of {qty} to {child_request_id}")
        await notification_service.emit_alert(
            conn, alert_type="sample_conversion_initiated",
            target_team=notification_service.TEAM_INVENTORY,
            message=f"Partial conversion of {parent['request_id']} ({qty}) -> {child_request_id}.",
            related_id=gp_id, related_type=notification_service.REL_SAMPLE_GATE_PASS)
    return await req_svc.get_requisition(conn, req_id)
