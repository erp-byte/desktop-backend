"""Blocking approval gate for an NPD dev-JC promote (spec: NPD email-approval epic).

open_promote_request() stashes the close payload + raises the INV_MGR PENDING gate
(any inventory_manager). act_promote_approval() flips one gate (role-checked).
finalize_if_ready() runs the real promote (npd_dev_service._finalize_promote) once
every gate is ACCEPTED. ids are app-supplied 8-digit BIGINTs (new_short_time_id)
with a unique retry.

086 moved the BUSINESS-HEAD approval OFF this gate and onto the requisition, where it
is now asked BEFORE the NPD team starts work (requisition_service.submit_requisition
-> approval_service.act_bh_signoff). A promote therefore raises only INV_MGR. The
REQUESTOR_BH branches below are retained deliberately: promotes opened before 086 still
carry a live REQUESTOR_BH row, and dropping the handling would wedge them forever."""
from __future__ import annotations
import json
import logging
import types as _t

import asyncpg
from fastapi import HTTPException

from app.core.helpers import new_short_time_id
from app.modules.sample.services import notification_service

logger = logging.getLogger(__name__)


async def _insert_8d(conn, sql: str, *params):
    """Insert with an app-supplied 8-digit id, retrying ONLY on a primary-key
    collision (a freshly minted id may clash). Any OTHER unique violation (e.g. a
    real business constraint like uq_promote_req_live) is re-raised so the caller
    can map it — mirroring the house insert_with_pk_retry contract, which never
    silently swallows non-PK conflicts."""
    for _ in range(5):
        rid = new_short_time_id()
        try:
            async with conn.transaction():
                return await conn.fetchval(sql, rid, *params)
        except asyncpg.UniqueViolationError as e:
            if not (e.constraint_name or "").endswith("_pkey"):
                raise
            continue
    raise HTTPException(500, detail={"error": "id_alloc", "message": "could not allocate id"})


async def open_promote_request(conn, dev_jc_id: int, *, payload: dict, user) -> dict:
    async with conn.transaction():
        # Only one live promote per job card (uq_promote_req_live, partial unique on
        # dev_jc_id WHERE status='PENDING'). A second open — sequential OR a race that
        # slips past — surfaces as a clean 409, never a confusing id-alloc 500.
        try:
            req_id = await _insert_8d(conn,
                """INSERT INTO npd_dev_promote_request (id, dev_jc_id, promote_phase_id, close_payload, created_by)
                   VALUES ($1,$2,$3,$4::jsonb,$5) RETURNING id""",
                dev_jc_id, payload.get("promote_phase_id"), json.dumps(payload), user.user_id)
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, detail={"error": "promote_already_pending",
                "message": "A promote is already awaiting approval for this job card",
                "details": {"dev_jc_id": dev_jc_id}})
        # 086: the inventory manager is the ONLY gate on a promote. The business head
        # signed off on the request at the requisition stage (or was auto-approved as
        # its own sales POC) before NPD ever picked it up, so asking them again here —
        # after the development work is finished — is the ask this change removed.
        await _insert_8d(conn,
            """INSERT INTO npd_dev_promote_approval (id, promote_request_id, approver_kind, approver_user_id)
               VALUES ($1,$2,'INV_MGR',NULL) RETURNING id""", req_id)
        await notification_service.emit_alert(
            conn, alert_type="npd_promote_requested",
            target_team=notification_service.TEAM_INVENTORY,
            message=f"Dev job card {dev_jc_id}: promote awaiting inventory-manager acceptance.",
            related_id=dev_jc_id)
    # requestor_uid is deliberately NOT passed: with no REQUESTOR_BH gate to clear,
    # a buttoned card to the BH would offer an approval that does not exist. They stay
    # on the trail's broadcast copy (resolve_recipients puts the requestor on To).
    try:
        from app.modules.sample.services import sample_mail_service as mail
        await mail.notify_promote_review_email(conn, dev_jc_id=dev_jc_id)
    except Exception:  # noqa: BLE001 — best-effort; a mail failure must not break the request
        pass
    try:
        from app.modules.sample.services import whatsapp_service as wa
        await wa.notify_promote_review(conn, dev_jc_id=dev_jc_id)
    except Exception:  # noqa: BLE001 — best-effort; a WhatsApp failure must not break the request
        pass
    return {"ok": True, "promote_request_id": req_id, "status": "PENDING_APPROVAL"}


async def act_promote_approval(conn, dev_jc_id: int, *, action: str, user,
                               remarks: str | None = None, approver_kind: str | None = None) -> dict:
    act = (action or "").upper()
    if act not in ("ACCEPT", "REJECT"):
        raise HTTPException(422, detail={"error": "invalid_action", "message": "action must be ACCEPT or REJECT"})
    async with conn.transaction():
        pr = await conn.fetchrow(
            "SELECT id FROM npd_dev_promote_request WHERE dev_jc_id=$1 AND status='PENDING' FOR UPDATE", dev_jc_id)
        if pr is None:
            raise HTTPException(409, detail={"error": "no_pending_promote",
                "message": "No pending promote request for this job card"})
        # Which gate(s) may this user act on? inventory_manager → INV_MGR; the
        # requisition's requestor → REQUESTOR_BH (a user can, in rare configs, be both).
        eligible: set[str] = set()
        if getattr(user, "role_name", "") == "inventory_manager":
            eligible.add("INV_MGR")
        bh_row = await conn.fetchrow(
            "SELECT id FROM npd_dev_promote_approval WHERE promote_request_id=$1 AND approver_kind='REQUESTOR_BH' AND approver_user_id=$2",
            pr["id"], user.user_id)
        if bh_row:
            eligible.add("REQUESTOR_BH")
        # An admin can act on any gate (mirrors the house admin bypass elsewhere).
        if getattr(user, "is_admin", False):
            eligible.update({"INV_MGR", "REQUESTOR_BH"})
        if not eligible:
            raise HTTPException(403, detail={"error": "not_an_approver",
                "message": "You are not an approver on this promote"})
        # Restrict to the eligible gates that are still PENDING.
        rows = await conn.fetch(
            "SELECT approver_kind FROM npd_dev_promote_approval "
            "WHERE promote_request_id=$1 AND status='PENDING' AND approver_kind = ANY($2::text[])",
            pr["id"], list(eligible))
        pending_kinds = [r["approver_kind"] for r in rows]
        if not pending_kinds:
            raise HTTPException(409, detail={"error": "already_actioned",
                "message": "Your approval on this promote is already recorded"})
        # Act on EXACTLY ONE gate — a single user holding both gates must still act
        # on each separately (preserves the two-person control), so disambiguate.
        if approver_kind:
            target = approver_kind.upper()
            if target not in pending_kinds:
                raise HTTPException(409, detail={"error": "gate_not_actionable",
                    "message": "That approval gate is not yours or is already actioned",
                    "details": {"approver_kind": target, "pending": pending_kinds}})
        elif len(pending_kinds) == 1:
            target = pending_kinds[0]
        else:
            raise HTTPException(422, detail={"error": "specify_gate",
                "message": "You hold both gates — specify approver_kind (INV_MGR or REQUESTOR_BH)",
                "details": {"pending": pending_kinds}})
        new_status = "ACCEPTED" if act == "ACCEPT" else "REJECTED"
        # Stamp WHICH user acted (records the specific inventory_manager who
        # accepted; harmless for REQUESTOR_BH, whose approver is already bound).
        await conn.execute(
            "UPDATE npd_dev_promote_approval SET status=$3, remarks=$4, decided_at=NOW(), approver_user_id=$5 "
            "WHERE promote_request_id=$1 AND approver_kind=$2 AND status='PENDING'",
            pr["id"], target, new_status, remarks, user.user_id)
        if act == "REJECT":
            await conn.execute("UPDATE npd_dev_promote_request SET status='VOID', decided_at=NOW() WHERE id=$1", pr["id"])
            result = {"ok": True, "status": "REJECTED"}
        else:
            # Finalize INSIDE this transaction so a promote failure rolls the gate
            # accept back (retryable). finalize_if_ready opens its own transaction,
            # which nests as a savepoint here — if _finalize_promote raises, the whole
            # outer transaction (including this gate flip) rolls back.
            result = await finalize_if_ready(conn, dev_jc_id)
    # Mail the decision into the job card's trail, AFTER commit so SMTP never rides the
    # transaction. This is the one choke point every channel funnels through — WhatsApp
    # (_apply_promote), the email button (POST /email/promote-action) and the in-app
    # endpoint — so a WhatsApp approval reaches the mail trail by construction.
    # Best-effort: a mail failure must not undo a recorded decision.
    try:
        from app.modules.sample.services import sample_mail_service as mail
        await mail.notify_promote_status_email(
            conn, dev_jc_id=dev_jc_id, gate=target, action=act,
            actor_user_id=getattr(user, "user_id", None),
            actor_name=getattr(user, "full_name", None), remarks=remarks, result=result)
    except Exception:  # noqa: BLE001
        logger.exception("Promote status email failed for dev JC %s", dev_jc_id)
    return result


async def finalize_if_ready(conn, dev_jc_id: int) -> dict:
    from app.modules.sample.services import npd_dev_service
    async with conn.transaction():
        pr = await conn.fetchrow(
            "SELECT id, promote_phase_id, close_payload, created_by FROM npd_dev_promote_request "
            "WHERE dev_jc_id=$1 AND status='PENDING' FOR UPDATE", dev_jc_id)
        if pr is None:
            return {"ok": True, "status": "no_pending"}
        n_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM npd_dev_promote_approval WHERE promote_request_id=$1 AND status<>'ACCEPTED'", pr["id"])
        if n_pending:
            return {"ok": True, "status": "PENDING_APPROVAL", "remaining": int(n_pending)}
        payload = json.loads(pr["close_payload"]) if isinstance(pr["close_payload"], str) else pr["close_payload"]
        creator = await conn.fetchrow(
            "SELECT a.user_id, r.role_name FROM auth_user a JOIN auth_role r ON a.role_id=r.role_id WHERE a.user_id=$1",
            pr["created_by"])
        user = _t.SimpleNamespace(user_id=pr["created_by"],
                                  role_name=(creator["role_name"] if creator else "npd_team"),
                                  is_admin=False, full_name="promote")
        await npd_dev_service._finalize_promote(conn, dev_jc_id,
            promote_phase_id=pr["promote_phase_id"], close_payload=payload, user=user)
        await conn.execute("UPDATE npd_dev_promote_request SET status='APPROVED', decided_at=NOW() WHERE id=$1", pr["id"])
    return {"ok": True, "status": "PROMOTED"}
