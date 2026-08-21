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
# 086 — the requisition-stage business-head gate on an NPD/TRIAL request. Its own stage
# rather than BH_APPROVAL, because for NPD/TRIAL the NPD team's review already writes
# BH_APPROVAL rows (act_npd_review) and sharing the stage would make "did the business
# head sign off?" unanswerable from the approvals table.
REQUESTOR_BH_SIGNOFF       = "REQUESTOR_BH_SIGNOFF"
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
                        user, remarks: str | None = None,
                        approver_user_id: int | None = None,
                        role_at_action: str | None = None) -> dict:
    """Insert one sample_approvals row (APPROVED / REJECTED) + audit it.

    `approver_user_id` / `role_at_action` override whose decision is RECORDED, while the
    audit line still names the actor who triggered it. Used by the 086 auto-approval,
    where the sales POC submitting the request IS the business head: the approval belongs
    to them as BH, not to the submit action."""
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
        req_id, approval_stage,
        approver_user_id if approver_user_id is not None else user.user_id,
        role_at_action or user.role_name, action, remarks, seq)
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

        # 085: when the requisition names its business head, ONLY that BH (or an admin)
        # may clear this gate — the create-time permission alone would let any BH in the
        # pool approve a request raised for someone else. Requisitions created before 085
        # carry no binding and keep the pool behaviour.
        bound_bh = locked["business_head_user_id"]
        if (bound_bh is not None
                and user.user_id != bound_bh
                and not getattr(user, "is_admin", False)):
            raise HTTPException(403, detail={
                "error": "not_the_approver",
                "message": "This requisition is awaiting approval from its own business head",
                "details": {"id": req_id}})

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

    # Post the BH decision into the trail, after commit. Best-effort — a mail failure must
    # never roll back a recorded approval.
    fresh = await req_svc.get_requisition(conn, req_id)
    try:
        from app.modules.sample.services import sample_mail_service as mail
        await mail.notify_requisition_event(
            conn, fresh,
            event=("approved" if action == "APPROVED" else "rejected"), reason=remarks)
    except Exception:  # noqa: BLE001
        logger.exception("BH approval email failed for req %s", req_id)
    return fresh


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
        # 086: a request whose business head has not signed off yet was never handed to
        # NPD — no alert, no review card. Refuse it here too, so a stale queue entry or a
        # hand-crafted call cannot start development on an unapproved request. Pre-086
        # rows carry NULL and are unaffected.
        if locked.get("bh_signoff_state") == "PENDING":
            raise HTTPException(409, detail={
                "error": "awaiting_bh_signoff",
                "message": "This request is still awaiting its business head's approval",
                "details": {"id": req_id}})

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
            # ONE outcome mail into the trail — the detail card carries the reason, so
            # there is no separate one-line "the request is updated" note behind it.
            await mail.notify_requisition_event(
                conn, dict(locked),
                event=("accepted" if act in ("ACCEPT", "APPROVE") else "on hold"),
                reason=reason)
            # Hold loop: re-offer the buttoned review card to npd_team as a reply into the
            # same thread, so a held request can be accepted (ends the loop) or held again.
            if act == "HOLD":
                await mail.notify_npd_review_email(conn, dict(locked))
        except Exception:  # noqa: BLE001
            logger.exception("Sample outcome email failed for req %s", req_id)

    return await req_svc.get_requisition(conn, req_id)


# ---------------------------------------------------------------------------
# 086 — requisition-stage business-head sign-off (NPD / TRIAL)
#
# The BH approval used to sit at the very END of the NPD flow: the dev job card's
# promote raised a REQUESTOR_BH gate, so the business head was asked to approve a
# recipe only after all the development work was already done. It now sits at the
# START, on the request itself, and the promote keeps only its INV_MGR gate.
#
# The gate is raised ONLY when someone else raised the request on the BH's behalf
# (sales_poc_user_id <> business_head_user_id). A BH raising their own request has
# already said yes by raising it — that auto-approves with no message sent.
# ---------------------------------------------------------------------------
BH_SIGNOFF_PENDING       = "PENDING"
BH_SIGNOFF_APPROVED      = "APPROVED"
BH_SIGNOFF_AUTO_APPROVED = "AUTO_APPROVED"
BH_SIGNOFF_REJECTED      = "REJECTED"
BH_SIGNOFF_NOT_REQUIRED  = "NOT_REQUIRED"


def _poc_uid(req: dict):
    """The user whose raising of the request counts as the sales POC.

    sales_poc_user_id is set on every 085 requisition (it defaults to the creator). It
    is NULL only for a free-text POC with no login, or a pre-085 row — in both cases the
    person who actually raised it is created_by, and that is who "is the sales POC the
    business head?" has to be asked about."""
    return req.get("sales_poc_user_id") or req.get("created_by")


def bh_signoff_decision(req: dict) -> tuple[str, int | None]:
    """(state, bh_user_id) the gate should take for `req`, with no DB writes.

    Pure so the same rule serves arm_bh_signoff and any caller that wants to preview it.
      • no business head bound      -> NOT_REQUIRED (there is nobody to ask)
      • sales POC IS that BH        -> AUTO_APPROVED (they raised it themselves)
      • otherwise                   -> PENDING
    """
    bh_uid = req.get("business_head_user_id")
    if bh_uid is None:
        return BH_SIGNOFF_NOT_REQUIRED, None
    if _poc_uid(req) == bh_uid:
        return BH_SIGNOFF_AUTO_APPROVED, bh_uid
    return BH_SIGNOFF_PENDING, bh_uid


async def arm_bh_signoff(conn, req: dict, *, user) -> str:
    """Decide + persist the gate for a just-submitted NPD/TRIAL request. Returns the
    state. MUST run inside the submit transaction — the state is what decides whether
    the NPD team is told about the request at all, so it cannot be allowed to diverge
    from the status move.

    An AUTO_APPROVED gate still writes a REQUESTOR_BH_SIGNOFF approval row: "nobody was
    asked" and "nobody approved" have to be distinguishable months later.
    """
    if not await req_svc.has_bh_signoff_columns(conn):
        return BH_SIGNOFF_NOT_REQUIRED      # migration 086 not applied → pre-086 behaviour
    state, bh_uid = bh_signoff_decision(req)
    req_id = req["id"]
    if state == BH_SIGNOFF_PENDING:
        await conn.execute(
            """UPDATE sample_requisitions
                  SET bh_signoff_state = 'PENDING', bh_signoff_at = NULL, bh_signoff_by = NULL
                WHERE id = $1""", req_id)
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_APPROVAL,
            new_value={"bh_signoff_state": state, "business_head_user_id": bh_uid},
            actor_user_id=user.user_id, actor_role=user.role_name,
            remarks="Awaiting business-head approval")
        return state

    await conn.execute(
        """UPDATE sample_requisitions
              SET bh_signoff_state = $2, bh_signoff_at = NOW(), bh_signoff_by = $3
            WHERE id = $1""", req_id, state, bh_uid)
    if state == BH_SIGNOFF_AUTO_APPROVED:
        await record_action(
            conn, req_id, approval_stage=REQUESTOR_BH_SIGNOFF, action="APPROVED",
            user=user, approver_user_id=bh_uid, role_at_action="business_head",
            remarks="Auto-approved — the sales POC is the business head on this request")
    return state


async def notify_bh_signoff(conn, req: dict) -> None:
    """Ask the bound business head to approve the request — email card with
    Approve/Reject buttons, plus the WhatsApp template with the same two quick replies.
    Best-effort: neither transport may break the submit that preceded it."""
    try:
        from app.modules.sample.services import sample_mail_service as mail
        await mail.notify_bh_signoff_email(conn, req)
    except Exception:  # noqa: BLE001
        logger.exception("BH sign-off email failed for req %s", req.get("id"))
    try:
        from app.modules.sample.services import whatsapp_service as wa
        await wa.notify_bh_signoff(conn, req)
    except Exception:  # noqa: BLE001
        logger.exception("BH sign-off WhatsApp failed for req %s", req.get("id"))


async def act_bh_signoff(conn, req_id: int, *, action: str, user,
                         remarks: str | None = None) -> dict:
    """The bound business head approves or rejects a held NPD/TRIAL request.

    APPROVE keeps the requisition SUBMITTED and releases it to the NPD team (whose own
    review then owns the SUBMITTED -> BH_APPROVED move, unchanged). REJECT moves it to
    BH_REJECTED with the reason, and NPD never sees it. Idempotent by construction: the
    gate is only actionable while bh_signoff_state = 'PENDING'.
    """
    act = (action or "").upper()
    if act in ("APPROVE", "ACCEPT"):
        act = "APPROVED"
    elif act in ("REJECT",):
        act = "REJECTED"
    if act not in ("APPROVED", "REJECTED"):
        raise HTTPException(422, detail={
            "error": "invalid_action",
            "message": "action must be APPROVED or REJECTED",
            "details": {"action": action}})
    if act == "REJECTED" and not (remarks or "").strip():
        raise HTTPException(422, detail={
            "error": "reason_required",
            "message": "A reason is required to reject",
            "details": {"id": req_id}})

    async with conn.transaction():
        locked = await conn.fetchrow(
            "SELECT * FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
            req_id)
        if locked is None:
            raise HTTPException(404, detail={"error": "not_found",
                                             "message": f"Sample requisition {req_id} not found",
                                             "details": {"id": req_id}})
        locked = dict(locked)
        if locked.get("bh_signoff_state") != BH_SIGNOFF_PENDING:
            raise HTTPException(409, detail={
                "error": "stage_already_actioned",
                "message": "This request is not awaiting business-head approval",
                "details": {"bh_signoff_state": locked.get("bh_signoff_state"),
                            "status": locked["status"]}})
        bound_bh = locked.get("business_head_user_id")
        if user.user_id != bound_bh and not getattr(user, "is_admin", False):
            raise HTTPException(403, detail={
                "error": "not_the_approver",
                "message": "This request is awaiting approval from its own business head",
                "details": {"id": req_id}})

        await record_action(conn, req_id, approval_stage=REQUESTOR_BH_SIGNOFF,
                            action=act, user=user, remarks=remarks)
        await conn.execute(
            """UPDATE sample_requisitions
                  SET bh_signoff_state = $2, bh_signoff_at = NOW(), bh_signoff_by = $3,
                      updated_at = NOW(), updated_by = $3
                WHERE id = $1""",
            req_id, BH_SIGNOFF_APPROVED if act == "APPROVED" else BH_SIGNOFF_REJECTED,
            user.user_id)

        if act == "REJECTED":
            # The request dies here — NPD was never told about it, so the only party to
            # tell is the business team that raised it.
            await req_svc.transition_status(conn, req_id, target="BH_REJECTED", user=user,
                                            remarks=f"BH rejected: {remarks}")
            await notification_service.emit_alert(
                conn, alert_type="sample_bh_rejected",
                target_team=notification_service.TEAM_BUSINESS,
                message=f"Sample {locked['request_id']} rejected by business head: {remarks}",
                related_id=req_id)

    fresh = await req_svc.get_requisition(conn, req_id)
    # Post the decision into the trail, then — on an approval — do the handoff that
    # submit deliberately held back. After commit, best-effort.
    try:
        from app.modules.sample.services import sample_mail_service as mail
        await mail.notify_requisition_event(
            conn, fresh,
            event=("bh signed off" if act == "APPROVED" else "rejected"), reason=remarks)
    except Exception:  # noqa: BLE001
        logger.exception("BH sign-off decision email failed for req %s", req_id)
    if act == "APPROVED":
        await req_svc.release_to_npd(conn, fresh)
    return fresh
