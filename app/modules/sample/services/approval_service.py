"""Sample approvals (checklist A5 / spec §7, §8, §9.3).

The required role for each approval stage is resolved from the config table
sample_approval_role_map (seeded with real roles in migration 037), so the
approval matrix can be remapped without code changes. Coarse module access is
still gated by require_permission in the router; this layer records who acted,
under which role, and moves the requisition's status under a row lock.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from app.modules.sample.services import audit_service, notification_service
from app.modules.sample.services import requisition_service as req_svc

logger = logging.getLogger(__name__)

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
    if action not in ("APPROVED", "REJECTED", "HOLD"):
        raise HTTPException(422, detail={
            "error": "invalid_action",
            "message": "action must be APPROVED, REJECTED or HOLD",
            "details": {"action": action}})
    if action in ("REJECTED", "HOLD") and not (remarks or "").strip():
        raise HTTPException(422, detail={
            "error": "remarks_required",
            "message": "A reason is required to reject or hold",
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

        # Notifications (spec §11). store_alert is legal-entity scoped, not by
        # warehouse — emit_alert defaults to the 'cfpl' entity.
        if action == "APPROVED":
            # NPD / TRIAL clear into NPD development (promote the draft BOM);
            # FG samples go to production; everything else to inventory outward.
            st = locked["sample_type"]
            if st in ("NPD", "TRIAL"):
                team = notification_service.TEAM_NPD
            elif st == "BASIS_FG":
                team = notification_service.TEAM_PRODUCTION
            else:
                team = notification_service.TEAM_INVENTORY
            await notification_service.emit_alert(
                conn, alert_type="sample_bh_approved", target_team=team,
                message=f"Sample {locked['request_id']} approved by business head.",
                related_id=req_id)
        else:
            await notification_service.emit_alert(
                conn, alert_type="sample_bh_rejected",
                target_team=notification_service.TEAM_BUSINESS,
                message=f"Sample {locked['request_id']} rejected: {remarks}",
                related_id=req_id)

    return await req_svc.get_requisition(conn, req_id)


# ---------------------------------------------------------------------------
# NPD review orchestration (the BH SENDS the request; NPD reviews it):
#   APPROVE -> BH_APPROVED (promotable) | REJECT -> BH_REJECTED | HOLD -> ON_HOLD
# Each carries a reason (required for reject + hold). Applies to NPD / TRIAL only.
# ---------------------------------------------------------------------------
_NPD_REVIEW = {
    "ACCEPT":  ("APPROVED", "BH_APPROVED"),
    "APPROVE": ("APPROVED", "BH_APPROVED"),   # legacy alias (WhatsApp/web)
    "REJECT":  ("REJECTED", "BH_REJECTED"),
    "HOLD":    ("HOLD",     "ON_HOLD"),
}


async def act_npd_review(conn, req_id: int, *, action: str, user,
                         reason: str | None = None, start_date=None) -> dict:
    """NPD team reviews a sent requisition (spec — NPD review gate).

    `start_date` is the date a HOLD takes effect (stored on the requisition);
    ignored for approve / reject.
    """
    act = (action or "").upper()
    if act not in _NPD_REVIEW:
        raise HTTPException(422, detail={
            "error": "invalid_action",
            "message": "action must be ACCEPT, REJECT or HOLD",
            "details": {"action": action}})
    appr_action, target = _NPD_REVIEW[act]

    async with conn.transaction():
        locked = await conn.fetchrow(
            "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
            req_id)
        if locked is None:
            raise HTTPException(404, detail={"error": "not_found",
                                             "message": f"Sample requisition {req_id} not found",
                                             "details": {"id": req_id}})
        if locked["sample_type"] not in ("NPD", "TRIAL"):
            raise HTTPException(409, detail={
                "error": "wrong_flow",
                "message": "NPD review applies only to NPD / TRIAL requests",
                "details": {"sample_type": locked["sample_type"]}})
        if locked["status"] not in ("SUBMITTED", "ON_HOLD"):
            raise HTTPException(409, detail={
                "error": "not_under_review",
                "message": "Requisition is not awaiting NPD review",
                "details": {"status": locked["status"]}})

        # record_action enforces the reason-required rule for REJECTED / HOLD.
        await record_action(conn, req_id, approval_stage=BH_APPROVAL,
                            action=appr_action, user=user, remarks=reason)
        await req_svc.transition_status(
            conn, req_id, target=target, user=user,
            remarks=f"NPD {act.lower()}" + (f": {reason}" if (reason or "").strip() else ""))

        # Record the hold's effective date on the requisition (HOLD only).
        if target == "ON_HOLD" and start_date is not None:
            await conn.execute(
                "UPDATE sample_requisitions SET hold_start_date = $2 WHERE id = $1",
                req_id, start_date)

        # Tell the business team the outcome.
        msg = f"Sample {locked['request_id']} {appr_action.lower()} by NPD"
        msg += f": {reason}" if (reason or "").strip() else "."
        await notification_service.emit_alert(
            conn, alert_type=f"sample_npd_{act.lower()}",
            target_team=notification_service.TEAM_BUSINESS,
            message=msg, related_id=req_id)

    # WhatsApp the requestor the outcome (best-effort, after commit — a transport
    # failure must never roll back or block the review). The hold reason captured
    # via WhatsApp (or the web form) flows straight into the on-hold template.
    if act in ("ACCEPT", "APPROVE", "HOLD"):
        try:
            from app.modules.sample.services import whatsapp_service as wa
            await wa.notify_requestor(conn, dict(locked), action=act, reason=reason)
            # Hold loop (WhatsApp mirror of the email re-offer): re-send the review
            # message with Accept/Hold buttons to the NPD reviewers, so a held request can
            # be accepted (ends the loop) or held again. Human-driven (one re-send per
            # recorded hold) — fires here so it covers web, email-redirect and WhatsApp holds.
            if act == "HOLD":
                await wa.notify_npd_review(conn, dict(locked))
        except Exception:  # noqa: BLE001
            logger.exception("WhatsApp requestor notify failed for req %s", req_id)
        try:
            from app.modules.sample.services import sample_mail_service as mail
            await mail.notify_requestor_email(conn, dict(locked), action=act, reason=reason)
            await mail.notify_inventory_informative(
                conn, dict(locked),
                event=("accepted" if act in ("ACCEPT", "APPROVE") else "on hold"))
            # Hold loop: re-offer the buttoned review card to npd_team as a reply into the
            # same thread, so a held request can be accepted (ends the loop) or held again.
            if act == "HOLD":
                await mail.notify_npd_review_email(conn, dict(locked), threaded=True)
        except Exception:  # noqa: BLE001
            logger.exception("Sample outcome email failed for req %s", req_id)

    return await req_svc.get_requisition(conn, req_id)
