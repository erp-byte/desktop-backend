"""Blocking dual-approval gate for an NPD dev-JC promote (spec: NPD email-approval epic).

open_promote_request() stashes the close payload + raises two PENDING gates
(INV_MGR -> any inventory_manager; REQUESTOR_BH -> the source requisition's
requestor). act_promote_approval() flips one gate (role-checked). finalize_if_ready()
runs the real promote (npd_dev_service._finalize_promote) once both gates ACCEPTED.
ids are app-supplied 8-digit BIGINTs (new_short_time_id) with a unique retry."""
from __future__ import annotations
import json
import types as _t

import asyncpg
from fastapi import HTTPException

from app.core.helpers import new_short_time_id
from app.modules.sample.services import notification_service


async def _insert_8d(conn, sql: str, *params):
    for _ in range(5):
        rid = new_short_time_id()
        try:
            async with conn.transaction():
                return await conn.fetchval(sql, rid, *params)
        except asyncpg.UniqueViolationError:
            continue
    raise HTTPException(500, detail={"error": "id_alloc", "message": "could not allocate id"})


async def open_promote_request(conn, dev_jc_id: int, *, payload: dict, user) -> dict:
    async with conn.transaction():
        req_id = await _insert_8d(conn,
            """INSERT INTO npd_dev_promote_request (id, dev_jc_id, promote_phase_id, close_payload, created_by)
               VALUES ($1,$2,$3,$4::jsonb,$5) RETURNING id""",
            dev_jc_id, payload.get("promote_phase_id"), json.dumps(payload), user.user_id)
        src_req = await conn.fetchval(
            "SELECT source_requisition_id FROM npd_dev_job_cards WHERE id = $1", dev_jc_id)
        requestor_uid = None
        if src_req:
            requestor_uid = await conn.fetchval(
                "SELECT requestor_user_id FROM sample_requisitions WHERE id = $1", src_req)
        await _insert_8d(conn,
            """INSERT INTO npd_dev_promote_approval (id, promote_request_id, approver_kind, approver_user_id)
               VALUES ($1,$2,'INV_MGR',NULL) RETURNING id""", req_id)
        await _insert_8d(conn,
            """INSERT INTO npd_dev_promote_approval (id, promote_request_id, approver_kind, approver_user_id)
               VALUES ($1,$2,'REQUESTOR_BH',$3) RETURNING id""", req_id, requestor_uid)
        await notification_service.emit_alert(
            conn, alert_type="npd_promote_requested",
            target_team=notification_service.TEAM_INVENTORY,
            message=f"Dev job card {dev_jc_id}: promote awaiting inventory-manager acceptance.",
            related_id=dev_jc_id)
    try:
        from app.modules.sample.services import sample_mail_service as mail
        await mail.notify_inventory_promote_requested(conn, dev_jc_id=dev_jc_id, requestor_uid=requestor_uid)
    except Exception:  # noqa: BLE001 — sample_mail_service arrives in Part 3; best-effort
        pass
    return {"ok": True, "promote_request_id": req_id, "status": "PENDING_APPROVAL"}


async def act_promote_approval(conn, dev_jc_id: int, *, action: str, user, remarks: str | None = None) -> dict:
    act = (action or "").upper()
    if act not in ("ACCEPT", "REJECT"):
        raise HTTPException(422, detail={"error": "invalid_action", "message": "action must be ACCEPT or REJECT"})
    async with conn.transaction():
        pr = await conn.fetchrow(
            "SELECT id FROM npd_dev_promote_request WHERE dev_jc_id=$1 AND status='PENDING' FOR UPDATE", dev_jc_id)
        if pr is None:
            raise HTTPException(409, detail={"error": "no_pending_promote",
                "message": "No pending promote request for this job card"})
        kinds = []
        if getattr(user, "role_name", "") == "inventory_manager":
            kinds.append("INV_MGR")
        bh_row = await conn.fetchrow(
            "SELECT id FROM npd_dev_promote_approval WHERE promote_request_id=$1 AND approver_kind='REQUESTOR_BH' AND approver_user_id=$2",
            pr["id"], user.user_id)
        if bh_row:
            kinds.append("REQUESTOR_BH")
        if not kinds:
            raise HTTPException(403, detail={"error": "not_an_approver",
                "message": "You are not an approver on this promote"})
        new_status = "ACCEPTED" if act == "ACCEPT" else "REJECTED"
        await conn.execute(
            "UPDATE npd_dev_promote_approval SET status=$3, remarks=$4, decided_at=NOW() "
            "WHERE promote_request_id=$1 AND approver_kind = ANY($2::text[]) AND status='PENDING'",
            pr["id"], kinds, new_status, remarks)
        if act == "REJECT":
            await conn.execute("UPDATE npd_dev_promote_request SET status='VOID', decided_at=NOW() WHERE id=$1", pr["id"])
            return {"ok": True, "status": "REJECTED"}
    return await finalize_if_ready(conn, dev_jc_id)


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
