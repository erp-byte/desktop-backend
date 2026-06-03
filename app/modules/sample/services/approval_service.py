"""Sample approvals (checklist A5 / spec §7, §8, §9.3).

The required role for each approval stage is resolved from the config table
sample_approval_role_map (seeded with real roles in migration 037), so the
approval matrix can be remapped without code changes. Coarse module access is
still gated by require_permission in the router; this layer records who acted,
under which role, and moves the requisition's status under a row lock.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.sample.services import audit_service, notification_service
from app.modules.sample.services import requisition_service as req_svc

# Approval stages (mirror the CHECK in sample_approvals)
BH_APPROVAL                = "BH_APPROVAL"
PRODUCTION_ACK             = "PRODUCTION_ACK"
INV_MGR_VERIFICATION       = "INV_MGR_VERIFICATION"
INV_MGR_SIGNOFF            = "INV_MGR_SIGNOFF"
CONVERSION_APPROVAL        = "CONVERSION_APPROVAL"
CONVERSION_INV_MGR_SIGNOFF = "CONVERSION_INV_MGR_SIGNOFF"


async def resolve_required_role(conn, *, approval_stage: str,
                                sample_type: str | None = None,
                                entity: str | None = None) -> str | None:
    """Return the role_name required for a stage, most-specific match first."""
    return await conn.fetchval(
        """
        SELECT required_role FROM sample_approval_role_map
         WHERE approval_stage = $1 AND is_active
           AND (sample_type = $2 OR sample_type = '*')
           AND (entity = $3 OR entity = '*')
         ORDER BY (sample_type <> '*') DESC, (entity <> '*') DESC
         LIMIT 1
        """,
        approval_stage, sample_type, entity,
    )


async def _next_sequence_no(conn, req_id: int) -> int:
    n = await conn.fetchval(
        "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM sample_approvals WHERE requisition_id = $1",
        req_id)
    return int(n)


async def record_action(conn, req_id: int, *, approval_stage: str, action: str,
                        user, remarks: str | None = None) -> dict:
    """Insert one sample_approvals row (APPROVED / REJECTED) + audit it."""
    if action not in ("APPROVED", "REJECTED"):
        raise HTTPException(422, detail={
            "error": "invalid_action",
            "message": "action must be APPROVED or REJECTED",
            "details": {"action": action}})
    if action == "REJECTED" and not (remarks or "").strip():
        raise HTTPException(422, detail={
            "error": "remarks_required",
            "message": "Rejection requires remarks",
            "details": {"approval_stage": approval_stage}})

    seq = await _next_sequence_no(conn, req_id)
    row = await conn.fetchrow(
        """
        INSERT INTO sample_approvals
            (requisition_id, approval_stage, approver_user_id, role_at_action,
             action, remarks, sequence_no, actioned_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        RETURNING *
        """,
        req_id, approval_stage, user.user_id, user.role_name, action, remarks, seq)
    await audit_service.write_audit(
        conn, req_id, audit_service.EV_APPROVAL,
        new_value={"approval_stage": approval_stage, "action": action, "sequence_no": seq},
        actor_user_id=user.user_id, actor_role=user.role_name, remarks=remarks)
    return dict(row)


async def has_approved(conn, req_id: int, approval_stage: str) -> bool:
    return bool(await conn.fetchval(
        """SELECT EXISTS(
               SELECT 1 FROM sample_approvals
                WHERE requisition_id = $1 AND approval_stage = $2 AND action = 'APPROVED')""",
        req_id, approval_stage))


# ---------------------------------------------------------------------------
# Business-head approval orchestration (SUBMITTED -> BH_APPROVED | BH_REJECTED)
# ---------------------------------------------------------------------------
async def act_bh_approval(conn, req_id: int, *, action: str, user,
                          remarks: str | None = None) -> dict:
    """Approve or reject a SUBMITTED requisition under a row lock (spec §9.3)."""
    async with conn.transaction():
        locked = await conn.fetchrow(
            "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
            req_id)
        if locked is None:
            raise HTTPException(404, detail={"error": "not_found",
                                             "message": f"Sample requisition {req_id} not found",
                                             "details": {"id": req_id}})
        if locked["status"] != "SUBMITTED":
            raise HTTPException(409, detail={
                "error": "stage_already_actioned",
                "message": "Requisition is not awaiting business-head approval",
                "details": {"status": locked["status"]}})

        await record_action(conn, req_id, approval_stage=BH_APPROVAL,
                            action=action, user=user, remarks=remarks)

        target = "BH_APPROVED" if action == "APPROVED" else "BH_REJECTED"
        await req_svc.transition_status(conn, req_id, target=target, user=user,
                                        remarks=f"BH {action.lower()}")

        # Notifications (spec §11)
        entity = locked["entity"]
        if action == "APPROVED":
            if locked["sample_type"] in ("BASIS_FG", "NPD"):
                team = notification_service.TEAM_PRODUCTION
            else:
                team = notification_service.TEAM_INVENTORY
            await notification_service.emit_alert(
                conn, alert_type="sample_bh_approved", target_team=team,
                message=f"Sample {locked['requisition_number']} approved by business head.",
                related_id=req_id, entity=entity)
        else:
            await notification_service.emit_alert(
                conn, alert_type="sample_bh_rejected",
                target_team=notification_service.TEAM_BUSINESS,
                message=f"Sample {locked['requisition_number']} rejected: {remarks}",
                related_id=req_id, entity=entity)

    return await req_svc.get_requisition(conn, req_id)
