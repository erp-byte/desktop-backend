"""/api/v1/sample/* — Sample Issuing module endpoints.

Covers all four flows: requisition CRUD + lifecycle, business-head approval,
Basis RM / Internal outward + dispatch, FG / NPD job-card generation, NPD draft
BOM authoring + promotion, gate-pass issuance / print / void, and internal ->
external conversion (full + partial).
"""
from __future__ import annotations

import json
import logging
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response

from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.sample import schemas
from app.modules.sample.services import (
    approval_service,
    conversion_service,
    gate_pass_service,
    jobcard_service,
    npd_dev_service,
    npd_service,
    outward_service,
    requisition_service,
    rm_issue_form_service,
    whatsapp_service,
)
from app.modules.sample.services.sample_gate_pass_pdf import generate_sample_gate_pass_pdf
from app.modules.sample.services.rm_issue_form_pdf import generate_rm_issue_form_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sample", tags=["Sample"])


# ── WhatsApp webhook (Meta Cloud API) ──────────────────────────────────────
# PUBLIC (no auth dep) — Meta calls these. GET is the one-time verify handshake;
# POST receives inbound messages and drives the NPD accept/hold flow. Inbound
# senders are authenticated by matching their number to an NPD-role auth_user
# inside whatsapp_service.handle_inbound; POST bodies are HMAC-checked when
# WHATSAPP_APP_SECRET is set.
@router.get("/whatsapp/webhook")
async def whatsapp_webhook_verify(request: Request):
    p = request.query_params
    if (p.get("hub.mode") == "subscribe" and whatsapp_service.VERIFY_TOKEN
            and p.get("hub.verify_token") == whatsapp_service.VERIFY_TOKEN):
        return Response(content=p.get("hub.challenge") or "", media_type="text/plain")
    raise HTTPException(403, detail="verification failed")


@router.post("/whatsapp/webhook")
async def whatsapp_webhook_receive(request: Request):
    raw = await request.body()
    if not whatsapp_service.verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403, detail="bad signature")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        payload = {}

    # Visitor Management approvals (approve_<id>/reject_<id>) share this WABA but belong to
    # the separate visitor system. Forward them to its webhook and exclude them from the NPD
    # loop, so an approver never gets an "NPD reviewer" reply. Entirely gated on
    # VISITOR_APPROVAL_FORWARD_URL: when unset, none of this runs and behaviour is unchanged.
    forwarded_ids: set[str] = set()
    if whatsapp_service.visitor_forward_url():
        visitor_msgs = whatsapp_service.visitor_approval_messages(payload)
        if visitor_msgs:
            forwarded = await whatsapp_service.forward_visitor_approvals(
                visitor_msgs, request.headers.get("X-Hub-Signature-256"))
            forwarded_ids = {m.get("id") for m in visitor_msgs if m.get("id")}
            logger.info("Forwarded %d/%d visitor-approval tap(s) to the visitor webhook",
                        forwarded, len(visitor_msgs))

    messages = [m for m in whatsapp_service.extract_messages(payload)
                if m.get("id") not in forwarded_ids]
    if messages:
        # `payload` is the machine id behind a tap (visitor sends "approve_<id>", NPD/promote
        # send the button text) — log it, it's the only way to tell why routing chose a flow.
        logger.info("WhatsApp inbound %d msg(s): %s", len(messages),
                    [{"from": m.get("from"), "type": m.get("type"),
                      "text": (m.get("text") or "")[:60], "payload": m.get("payload"),
                      "ctx": m.get("context_id")}
                     for m in messages])
    else:
        # No messages parsed → almost always a delivery/read STATUS callback (value
        # carries `statuses`, not `messages`), or a payload shape we don't parse.
        # Log the value keys so a real button tap can be told apart from a status ping.
        shapes = [list((c.get("value") or {}).keys())
                  for e in (payload.get("entry") or []) for c in (e.get("changes") or [])]
        logger.info("WhatsApp webhook, no messages; value keys=%s", shapes)
    results = []
    if messages:
        pool = request.app.state.db_pool
        async with pool.acquire() as conn:
            for m in messages:
                try:
                    results.append(await whatsapp_service.handle_inbound(
                        conn, from_phone=m["from"], text=m["text"],
                        context_id=m.get("context_id"), raw=m.get("raw")))
                except Exception:  # noqa: BLE001 — always 200 so Meta doesn't retry-storm
                    logger.exception("WhatsApp inbound handling failed")
        logger.info("WhatsApp inbound results: %s", results)
    return {"received": len(messages), "results": results}


def _confirm_page(*, heading: str, summary: str, action_url: str, hidden: dict,
                  button_label: str, button_color: str = "#16a34a") -> Response:
    """A tiny POST-confirm landing page for the email action buttons. The mutating
    endpoints below are POST-only; the mail buttons GET this page, which carries the
    action behind an explicit Confirm submit — so a mailbox link-scanner / prefetch
    (which only issues GETs) can never auto-trigger the accept/approve. Nothing mutates
    until the human clicks Confirm."""
    from html import escape
    inputs = "".join(
        f'<input type="hidden" name="{escape(str(k))}" value="{escape(str(v))}">'
        for k, v in hidden.items())
    html = (
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow"></head>'
        '<body style="margin:0;background:#f4f5f7;font-family:-apple-system,\'Segoe UI\',Roboto,Arial,sans-serif">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:48px 12px"><tr><td align="center">'
        '<table role="presentation" width="440" cellpadding="0" cellspacing="0" style="max-width:440px;width:100%;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:28px 26px;text-align:center"><tr><td>'
        f'<div style="font-size:18px;font-weight:700;color:#111827;margin-bottom:6px">{escape(heading)}</div>'
        f'<div style="font-size:13px;color:#6b7280;margin-bottom:22px">{escape(summary)}</div>'
        f'<form method="post" action="{escape(action_url)}">{inputs}'
        f'<button type="submit" style="background:{button_color};color:#fff;font-size:14px;font-weight:600;border:0;border-radius:6px;padding:12px 34px;cursor:pointer">{escape(button_label)}</button>'
        '</form>'
        '<div style="font-size:11px;color:#9ca3af;margin-top:16px">You can close this tab to cancel — nothing changes until you confirm.</div>'
        '</td></tr></table></td></tr></table></body></html>')
    return Response(html, media_type="text/html")


# ── Email accept/hold — review-mail buttons ───────────────────────────────────
# PUBLIC (no auth dep). The mail buttons GET this; ACCEPT renders a POST-confirm page
# (so a link-scanner's GET can't auto-accept) and the POST below does the real work.
#   • status=accept → render the confirm page (no mutation here).
#   • status=hold   → redirect to the portal request page; the hold is recorded there.
@router.get("/email/npd-action")
async def email_npd_action(
    request: Request,
    request_id: int = Query(...),
    status: str = Query(...),
    email: str = Query(""),
    t: str = Query(""),
):
    from app.config import Settings
    st = (status or "").strip().lower()
    web = Settings().WEB_APP_URL.rstrip("/")

    # HOLD → send the reviewer to the request page; held from there.
    if st == "hold":
        pool = request.app.state.db_pool
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM sample_requisitions WHERE request_id = $1 AND deleted_at IS NULL", request_id)
        target = f"{web}/modules/sample/{row['id']}" if row else f"{web}/modules/sample"
        return Response(status_code=302, headers={"Location": target})

    if st != "accept":
        return Response("<h3>Unknown action.</h3>", media_type="text/html", status_code=400)
    if not (email or "").strip():  # never auth a blank identifier
        return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)
    from app.modules.sample.services.email_link_token import verify
    if not verify(t, "npd", request_id, email):
        return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)

    base = Settings().PUBLIC_BACKEND_URL.rstrip("/")
    return _confirm_page(
        heading="Accept this NPD sample request?",
        summary=f"Request {request_id}",
        action_url=f"{base}/api/v1/sample/email/npd-action",
        hidden={"request_id": request_id, "email": email, "t": t},
        button_label="✓ Confirm accept")


@router.post("/email/npd-action")
async def email_npd_action_confirm(
    request: Request,
    request_id: int = Form(...),
    email: str = Form(""),
    t: str = Form(""),
):
    """The real accept — POST-only, behind the confirm button. Verify `email` is an
    ACTIVE npd_team reviewer, then accept (idempotent: only SUBMITTED/ON_HOLD). Auth
    fail / wrong state → no mutation, the call is ignored."""
    if not (email or "").strip():
        return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)
    from app.modules.sample.services.email_link_token import verify
    if not verify(t, "npd", request_id, email):
        return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, status FROM sample_requisitions WHERE request_id = $1 AND deleted_at IS NULL",
            request_id)
        if row is None:
            return Response("<h3>Request not found.</h3>", media_type="text/html", status_code=404)
        urow = await conn.fetchrow(
            """SELECT a.user_id, r.role_name FROM auth_user a JOIN auth_role r ON a.role_id = r.role_id
                WHERE r.role_name = 'npd_team' AND lower(a.email) = lower($1)
                  AND COALESCE(a.is_active, TRUE) ORDER BY a.user_id LIMIT 1""", email)
        if urow is None:
            return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)
        if row["status"] not in ("SUBMITTED", "ON_HOLD"):
            return Response(f"<h3>Already actioned (status {row['status']}).</h3>", media_type="text/html")
        import types as _t
        from app.modules.sample.services import approval_service
        user = _t.SimpleNamespace(user_id=urow["user_id"], role_name=urow["role_name"],
                                  is_admin=False, full_name="email")
        try:
            await approval_service.act_npd_review(conn, row["id"], action="ACCEPT", user=user)
        except HTTPException:
            return Response("<h3>Could not accept — it may already have been actioned.</h3>",
                            media_type="text/html")
        return Response("<h3>&#10003; Accepted. You can close this tab.</h3>"
                        "<script>setTimeout(function(){try{window.close()}catch(e){}},400)</script>",
                        media_type="text/html")


async def _resolve_promote_approver(conn, dev_jc_id: int, kind: str, email: str):
    """Authenticate an email-link approver against the gate it claims (mandatory check):
    INV_MGR → an ACTIVE inventory_manager with that email; REQUESTOR_BH → the user bound
    to this JC's BH gate with that email (matched on the LATEST promote request, so a late
    click still resolves). Returns the auth_user row (user_id, role_name) or None."""
    if kind == "INV_MGR":
        return await conn.fetchrow(
            """SELECT a.user_id, r.role_name FROM auth_user a JOIN auth_role r ON a.role_id = r.role_id
                WHERE r.role_name = 'inventory_manager' AND lower(a.email) = lower($1)
                  AND COALESCE(a.is_active, TRUE) ORDER BY a.user_id LIMIT 1""", email)
    return await conn.fetchrow(
        """SELECT a.user_id, COALESCE(r.role_name, '') AS role_name
             FROM npd_dev_promote_request pr
             JOIN npd_dev_promote_approval ap
               ON ap.promote_request_id = pr.id AND ap.approver_kind = 'REQUESTOR_BH'
             JOIN auth_user a ON a.user_id = ap.approver_user_id
             LEFT JOIN auth_role r ON a.role_id = r.role_id
            WHERE pr.dev_jc_id = $1
              AND lower(a.email) = lower($2) AND COALESCE(a.is_active, TRUE)
            ORDER BY pr.created_at DESC LIMIT 1""", dev_jc_id, email)


# ── Email approve/reject — promote dual-approval gate buttons ─────────────────
# PUBLIC (no auth dep). The mail buttons GET this.
#   • status=approve → render a POST-confirm page (a link-scanner's GET can't auto-act);
#     the POST below does the real approve.
#   • status=reject  → redirect to the web app's job-card page, which pops a reason dialog
#     and submits the reject through POST /email/promote-reject (email-authenticated).
@router.get("/email/promote-action")
async def email_promote_action(
    request: Request,
    dev_jc_id: int = Query(...),
    approver_kind: str = Query(...),
    status: str = Query(...),
    email: str = Query(""),
    t: str = Query(""),
):
    from app.config import Settings
    from urllib.parse import quote
    st = (status or "").strip().lower()
    kind = (approver_kind or "").strip().upper()

    # REJECT → web app job-card page, carrying the gate + email so it can pop the reason
    # dialog and authenticate the submit. (The mail's Reject button links here directly;
    # this remains as a defensive fallback for the backend URL form.)
    if st == "reject":
        web = Settings().WEB_APP_URL.rstrip("/")
        q = (f"?promote_reject={kind}&email={quote(email)}"
             if kind in ("INV_MGR", "REQUESTOR_BH") and (email or "").strip() else "")
        return Response(status_code=302,
                        headers={"Location": f"{web}/modules/npd-development/job-cards/{dev_jc_id}{q}"})
    if st != "approve":
        return Response("<h3>Unknown action.</h3>", media_type="text/html", status_code=400)
    if kind not in ("INV_MGR", "REQUESTOR_BH"):
        return Response("<h3>Unknown approval gate.</h3>", media_type="text/html", status_code=400)
    if not (email or "").strip():  # never auth a blank identifier
        return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)

    from app.modules.sample.services.email_link_token import verify
    if not verify(t, "promote", dev_jc_id, kind, email):
        return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)

    base = Settings().PUBLIC_BACKEND_URL.rstrip("/")
    gate_label = "Inventory manager" if kind == "INV_MGR" else "Requestor (business head)"
    return _confirm_page(
        heading="Approve recipe promotion?",
        summary=f"Dev JC {dev_jc_id} · your gate: {gate_label}",
        action_url=f"{base}/api/v1/sample/email/promote-action",
        hidden={"dev_jc_id": dev_jc_id, "approver_kind": kind, "email": email, "t": t},
        button_label="✓ Confirm approve")


@router.post("/email/promote-action")
async def email_promote_action_confirm(
    request: Request,
    dev_jc_id: int = Form(...),
    approver_kind: str = Form(...),
    email: str = Form(""),
    t: str = Form(""),
):
    """The real approve — POST-only, behind the confirm button. Verify `email` owns the
    gate, then accept it (idempotent; promotes once BOTH gates clear). Auth fail / wrong
    state → no mutation, the call is ignored."""
    kind = (approver_kind or "").strip().upper()
    if kind not in ("INV_MGR", "REQUESTOR_BH"):
        return Response("<h3>Unknown approval gate.</h3>", media_type="text/html", status_code=400)
    if not (email or "").strip():
        return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)
    from app.modules.sample.services.email_link_token import verify
    if not verify(t, "promote", dev_jc_id, kind, email):
        return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        urow = await _resolve_promote_approver(conn, dev_jc_id, kind, email)
        if urow is None:
            return Response("<h3>This link is not authorised.</h3>", media_type="text/html", status_code=403)
        import types as _t
        from app.modules.sample.services import promote_approval_service as pas
        user = _t.SimpleNamespace(user_id=urow["user_id"], role_name=urow["role_name"],
                                  is_admin=False, full_name="email")
        try:
            await pas.act_promote_approval(conn, dev_jc_id, action="ACCEPT", user=user, approver_kind=kind)
        except HTTPException:
            return Response("<h3>Could not approve — it may already have been actioned.</h3>",
                            media_type="text/html")
        return Response("<h3>&#10003; Approved. You can close this tab.</h3>"
                        "<script>setTimeout(function(){try{window.close()}catch(e){}},400)</script>",
                        media_type="text/html")


@router.post("/email/promote-reject")
async def email_promote_reject(request: Request, body: schemas.PromoteEmailReject):
    """Reject a promote gate from the web app's email-driven reason dialog. PUBLIC — the
    submit is authenticated by the recipient `email` OWNING the gate (mandatory), and a
    reason is required. Returns the act_promote_approval result as JSON."""
    kind = body.approver_kind
    email = (body.email or "").strip()
    remarks = (body.remarks or "").strip()
    if not email:
        raise HTTPException(403, detail={"error": "unauthorised", "message": "This link is not authorised"})
    if not remarks:
        raise HTTPException(422, detail={"error": "reason_required", "message": "A reason is required to reject"})
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        urow = await _resolve_promote_approver(conn, body.dev_jc_id, kind, email)
        if urow is None:
            raise HTTPException(403, detail={"error": "not_an_approver", "message": "This link is not authorised"})
        import types as _t
        from app.modules.sample.services import promote_approval_service as pas
        user = _t.SimpleNamespace(user_id=urow["user_id"], role_name=urow["role_name"],
                                  is_admin=False, full_name="email")
        return await pas.act_promote_approval(conn, body.dev_jc_id, action="REJECT",
                                              user=user, remarks=remarks, approver_kind=kind)


@router.post("/email/run-reminders")
async def run_reminders(request: Request, user: AuthUser = Depends(require_permission("sample", "npd", action="create"))):
    """Cron-invokable: send any due NPD review reminders (capped cadence)."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        from app.modules.sample.services import sample_mail_service as mail
        return {"sent": await mail.send_due_reminders(conn)}


# ── Requisitions ─────────────────────────────────────────────────────────
@router.post("/requisitions")
async def create_requisition(
    request: Request,
    body: schemas.RequisitionCreate,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.create_requisition(
            conn, payload=body.model_dump(), user=user)


# NPD sample requisition — a pure request (type / target article / qty / desc /
# purpose / requestor / warehouse). NPD-mandatory fields are enforced by the
# NpdRequisitionCreate schema; it delegates to the shared create with no article
# lines (the recipe is authored later on /develop). Surfaced id = request_id.
@router.post("/npd-requisitions")
async def create_npd_requisition(
    request: Request,
    body: schemas.NpdRequisitionCreate,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="create")),
):
    payload = body.model_dump()
    payload["articles"] = []            # NPD request carries no article lines
    payload["internal_override"] = False
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.create_requisition(
            conn, payload=payload, user=user)


# Business-head names for the requestor dropdown on the NPD request form — a sales/admin
# user raises a requisition on behalf of a BH. Gated on requisition-create so the whole
# requesting side (sales / business_head / planner / npd_team / admin) can populate it.
@router.get("/business-heads")
async def list_business_heads(
    request: Request,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.list_business_heads(conn)


@router.get("/requisitions")
async def list_requisitions(
    request: Request,
    status: str | None = Query(None),
    sample_type: str | None = Query(None),
    warehouse: str | None = Query(None),
    sample_types: str | None = Query(None, description="CSV of sample_type values (e.g. NPD,TRIAL)"),
    statuses: str | None = Query(None, description="CSV of status values (e.g. DRAFT,SUBMITTED)"),
    requestor: str | None = Query(None),
    q: str | None = Query(None, description="Free-text search: number / request_id / target / description / requestor"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    types = [t for t in (sample_types.split(",") if sample_types else []) if t] or None
    status_set = [s for s in (statuses.split(",") if statuses else []) if s] or None
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.list_requisitions(
            conn, status=status, sample_type=sample_type, warehouse=warehouse,
            sample_types=types, statuses=status_set, requestor=requestor, q=q,
            date_from=date_from, date_to=date_to, limit=limit, offset=offset)


# NB: declared BEFORE /requisitions/{req_id} so "requestors" isn't parsed as an id.
@router.get("/requisitions/requestors")
async def list_requisition_requestors(
    request: Request,
    sample_types: str | None = Query(None, description="CSV of sample_type values"),
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    """Distinct requestor labels for the queue's Requestor filter dropdown."""
    types = [t for t in (sample_types.split(",") if sample_types else []) if t] or None
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.list_requestors(conn, sample_types=types)


@router.get("/requisitions/{req_id}")
async def get_requisition(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.get_requisition(conn, req_id)


@router.patch("/requisitions/{req_id}")
async def update_requisition(
    request: Request,
    req_id: int,
    body: schemas.RequisitionUpdate,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.update_requisition(
            conn, req_id, payload=body.model_dump(exclude_unset=True), user=user)


@router.post("/requisitions/{req_id}/submit")
async def submit_requisition(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.submit_requisition(conn, req_id, user=user)


@router.post("/requisitions/{req_id}/cancel")
async def cancel_requisition(
    request: Request,
    req_id: int,
    body: schemas.CancelBody,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="delete")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.cancel_requisition(
            conn, req_id, reason=body.reason, user=user)


@router.post("/requisitions/{req_id}/close")
async def close_requisition(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.close_requisition(conn, req_id, user=user)


# ── Approvals ────────────────────────────────────────────────────────────
@router.post("/requisitions/{req_id}/approve")
async def bh_approve(
    request: Request,
    req_id: int,
    body: schemas.ApprovalAction,
    user: AuthUser = Depends(require_permission("sample", "approve", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await approval_service.act_bh_approval(
            conn, req_id, action=body.action, user=user, remarks=body.remarks)


@router.post("/requisitions/{req_id}/npd-review")
async def npd_review(
    request: Request,
    req_id: int,
    body: schemas.NpdReviewBody,
    user: AuthUser = Depends(require_permission("sample", "npd", action="create")),
):
    """NPD team reviews a BH-sent request: approve / reject / hold (reason + hold start date)."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await approval_service.act_npd_review(
            conn, req_id, action=body.action, user=user, reason=body.reason,
            start_date=body.start_date)


# ── Outward (Basis RM / Internal) ────────────────────────────────────────
@router.post("/requisitions/{req_id}/outward")
async def issue_outward(
    request: Request,
    req_id: int,
    body: schemas.OutwardBody,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    issued = {line.article_id: line.qty for line in (body.issued or [])} or None
    async with pool.acquire() as conn:
        return await outward_service.issue_outward(
            conn, req_id, user=user, from_location=body.from_location, issued=issued)


@router.post("/requisitions/{req_id}/dispatch-internal")
async def dispatch_internal(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await outward_service.dispatch_internal(conn, req_id, user=user)


# ── FG / NPD job cards ────────────────────────────────────────────────────
@router.post("/requisitions/{req_id}/start-production")
async def start_production(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "production_ack", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await jobcard_service.start_production(conn, req_id, user=user)


@router.post("/requisitions/{req_id}/mark-packing")
async def mark_packing(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "production_ack", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await jobcard_service.mark_packing(conn, req_id, user=user)


@router.post("/requisitions/{req_id}/mark-ready")
async def mark_ready(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await jobcard_service.mark_ready(conn, req_id, user=user)


# ── NPD draft BOM ─────────────────────────────────────────────────────────
@router.post("/requisitions/{req_id}/npd-draft")
async def create_npd_draft(
    request: Request,
    req_id: int,
    body: schemas.NpdDraftCreate,
    user: AuthUser = Depends(require_permission("sample", "npd", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_service.create_draft_bom(conn, req_id, payload=body.model_dump(), user=user)


@router.get("/npd-drafts/{draft_id}")
async def get_npd_draft(
    request: Request,
    draft_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_service.get_draft_bom(conn, draft_id)


@router.put("/npd-drafts/{draft_id}/lines")
async def replace_npd_lines(
    request: Request,
    draft_id: int,
    body: schemas.NpdLinesReplace,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_service.replace_lines(
            conn, draft_id, lines=[ln.model_dump() for ln in body.lines], user=user)


@router.post("/npd-drafts/{draft_id}/promote")
async def promote_npd_draft(
    request: Request,
    draft_id: int,
    user: AuthUser = Depends(require_permission("sample", "npd", "promote", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_service.promote_draft(conn, draft_id, user=user)


# ── Standalone NPD development job cards ───────────────────────────────────
# Pure R&D, decoupled from sample requisitions. Create / edit / start gate on
# the `sample/npd` permission (npd_team + admin); closing — which promotes the
# recipe into a live BOM — gates on `sample/npd/promote` like the draft promote.
@router.post("/npd-dev-job-cards")
async def create_dev_job_card(
    request: Request,
    body: schemas.DevJobCardCreate,
    user: AuthUser = Depends(require_permission("sample", "npd", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.create_dev_job_card(conn, payload=body.model_dump(), user=user)


@router.get("/npd-dev-job-cards")
async def list_dev_job_cards(
    request: Request,
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.list_dev_job_cards(
            conn, status=status, limit=limit, offset=offset)


@router.get("/boms")
async def search_boms(
    request: Request,
    search: str | None = Query(None),
    limit: int = Query(30, le=100),
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    """Searchable BOM list for the dev job-card 'Base BOM' picker."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.search_boms(conn, search=search, limit=limit)


@router.get("/bom-browse")
async def browse_boms(
    request: Request,
    item_type: str | None = Query(None),
    item_group: str | None = Query(None),
    sub_group: str | None = Query(None),
    particulars: str | None = Query(None),
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    """Cascade browse (Item type -> Group -> Sub-group -> Item) for the Base-BOM picker."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.browse_boms(
            conn, item_type=item_type, item_group=item_group,
            sub_group=sub_group, particulars=particulars)


@router.get("/boms/{bom_id}/lines")
async def get_bom_lines(
    request: Request,
    bom_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    """Full material list of a BOM — seeds the dev job-card trial recipe."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.get_bom_lines(conn, bom_id)


@router.get("/npd-dev-job-cards/{dev_jc_id}")
async def get_dev_job_card(
    request: Request,
    dev_jc_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.get_dev_job_card(conn, dev_jc_id)


@router.put("/npd-dev-job-cards/{dev_jc_id}/lines")
async def replace_dev_lines(
    request: Request,
    dev_jc_id: int,
    body: schemas.NpdLinesReplace,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.replace_lines(
            conn, dev_jc_id, lines=[ln.model_dump() for ln in body.lines], user=user)


@router.put("/npd-dev-job-cards/{dev_jc_id}/articles")
async def replace_dev_articles(
    request: Request,
    dev_jc_id: int,
    body: schemas.DevArticlesReplace,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.replace_dev_articles(
            conn, dev_jc_id, articles=[a.model_dump() for a in body.articles], user=user)


@router.patch("/npd-dev-job-cards/{dev_jc_id}")
async def update_dev_job_card_details(
    request: Request,
    dev_jc_id: int,
    body: schemas.DevJobCardDetailsUpdate,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    """Edit the card's customer & dispatch header (DRAFT/IN_DEVELOPMENT). PATCH — only the
    sent fields are updated."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.update_dev_job_card_details(
            conn, dev_jc_id, payload=body.model_dump(exclude_unset=True), user=user)


@router.post("/npd-dev-job-cards/{dev_jc_id}/start")
async def start_dev_job_card(
    request: Request,
    dev_jc_id: int,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.start_development(conn, dev_jc_id, user=user)


@router.post("/npd-dev-job-cards/{dev_jc_id}/close")
async def close_dev_job_card(
    request: Request,
    dev_jc_id: int,
    body: schemas.DevJobCardClose,
    user: AuthUser = Depends(require_permission("sample", "npd", "promote", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.request_promote(conn, dev_jc_id, payload=body.model_dump(), user=user)


@router.post("/npd-dev-job-cards/{dev_jc_id}/promote-approval")
async def promote_approval(
    request: Request,
    dev_jc_id: int,
    body: schemas.PromoteApprovalBody,
    # Both approver roles (inventory_manager + the requestor's BH/planner) hold
    # sample view; the real per-gate authorization is enforced inside
    # act_promote_approval, so gate this endpoint on the broad view permission.
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    from app.modules.sample.services import promote_approval_service as pas
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await pas.act_promote_approval(conn, dev_jc_id, action=body.action, user=user,
                                              remarks=body.remarks, approver_kind=body.approver_kind)


@router.post("/npd-dev-job-cards/{dev_jc_id}/dispatch")
async def dispatch_dev_sample(
    request: Request,
    dev_jc_id: int,
    body: schemas.DevDispatchBody,
    user: AuthUser = Depends(require_permission("sample", "npd", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.dispatch_dev_sample(
            conn, dev_jc_id, recipient=body.recipient, qty=body.qty, article_id=body.article_id,
            uom=body.uom, user=user)


@router.post("/npd-dev-job-cards/{dev_jc_id}/cancel")
async def cancel_dev_job_card(
    request: Request,
    dev_jc_id: int,
    body: schemas.CancelBody,
    user: AuthUser = Depends(require_permission("sample", "npd", action="delete")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.cancel_dev_job_card(conn, dev_jc_id, reason=body.reason, user=user)


# ── Dev job-card trial phases (multi-day start/complete) ───────────────────
@router.post("/npd-dev-job-cards/{dev_jc_id}/phases")
async def add_dev_phase(
    request: Request,
    dev_jc_id: int,
    body: schemas.DevPhaseCreate,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.add_phase(
            conn, dev_jc_id, name=body.name,
            clone_from_phase_id=body.clone_from_phase_id, user=user)


@router.put("/npd-dev-job-cards/{dev_jc_id}/phases/{phase_id}/lines")
async def replace_dev_phase_lines(
    request: Request,
    dev_jc_id: int,
    phase_id: int,
    body: schemas.NpdLinesReplace,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.replace_phase_lines(
            conn, dev_jc_id, phase_id, lines=[ln.model_dump() for ln in body.lines], user=user)


@router.delete("/npd-dev-job-cards/{dev_jc_id}/phases/{phase_id}")
async def delete_dev_phase(
    request: Request,
    dev_jc_id: int,
    phase_id: int,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.delete_phase(conn, dev_jc_id, phase_id, user=user)


@router.post("/npd-dev-job-cards/{dev_jc_id}/phases/{phase_id}/start")
async def start_dev_phase(
    request: Request,
    dev_jc_id: int,
    phase_id: int,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.start_phase(conn, dev_jc_id, phase_id, user=user)


@router.post("/npd-dev-job-cards/{dev_jc_id}/phases/{phase_id}/complete")
async def complete_dev_phase(
    request: Request,
    dev_jc_id: int,
    phase_id: int,
    body: schemas.DevPhaseComplete,
    user: AuthUser = Depends(require_permission("sample", "npd", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_dev_service.complete_phase(conn, dev_jc_id, phase_id, payload=body.model_dump(), user=user)


# ── RM Issue / Collection Form (Document 015, §10) ─────────────────────────
# Raise Indent is the NPD author (maker → sample/npd); approve + issue are the
# Store/Inventory checker (sample/inv_signoff). The Store-issue action fires the
# 265 Goods Issue (Step A). Maker can never be the checker.
@router.post("/rm-issue-forms")
async def create_rm_issue_form(
    request: Request,
    body: schemas.RmIssueFormCreate,
    user: AuthUser = Depends(require_permission("sample", "npd", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await rm_issue_form_service.raise_indent(conn, payload=body.model_dump(), user=user)


@router.get("/rm-issue-forms")
async def list_rm_issue_forms(
    request: Request,
    status: str | None = Query(None),
    source_type: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await rm_issue_form_service.list_forms(
            conn, status=status, source_type=source_type, limit=limit, offset=offset)


@router.get("/rm-issue-forms/{form_id}")
async def get_rm_issue_form(
    request: Request,
    form_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await rm_issue_form_service.get_form(conn, form_id)


@router.post("/rm-issue-forms/{form_id}/submit")
async def submit_rm_issue_form(
    request: Request,
    form_id: int,
    user: AuthUser = Depends(require_permission("sample", "npd", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await rm_issue_form_service.submit_form(conn, form_id, user=user)


@router.post("/rm-issue-forms/{form_id}/approve")
async def approve_rm_issue_form(
    request: Request,
    form_id: int,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await rm_issue_form_service.approve_form(conn, form_id, user=user)


@router.post("/rm-issue-forms/{form_id}/issue")
async def issue_rm_issue_form(
    request: Request,
    form_id: int,
    body: schemas.RmIssueBody,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    issued = {ln.line_id: {"issued_qty": ln.issued_qty, "lot_no": ln.lot_no} for ln in body.issued}
    async with pool.acquire() as conn:
        return await rm_issue_form_service.issue_form(conn, form_id, issued=issued, user=user)


@router.post("/rm-issue-forms/{form_id}/cancel")
async def cancel_rm_issue_form(
    request: Request,
    form_id: int,
    body: schemas.CancelBody,
    user: AuthUser = Depends(require_permission("sample", "npd", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await rm_issue_form_service.cancel_form(conn, form_id, reason=body.reason, user=user)


@router.get("/rm-issue-forms/{form_id}/pdf")
async def print_rm_issue_form(
    request: Request,
    form_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        form = await rm_issue_form_service.get_form(conn, form_id)
        pdf = generate_rm_issue_form_pdf(form)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={form['form_number']}.pdf"})


# ── Gate pass ─────────────────────────────────────────────────────────────
@router.post("/requisitions/{req_id}/inv-verify")
async def inv_verify(
    request: Request,
    req_id: int,
    body: schemas.InvVerifyBody,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await gate_pass_service.inv_verify(conn, req_id, user=user, remarks=body.remarks)


@router.post("/requisitions/{req_id}/issue-gate-pass")
async def issue_gate_pass(
    request: Request,
    req_id: int,
    body: schemas.GatePassIssueBody,
    user: AuthUser = Depends(require_permission("sample", "gate_pass", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await gate_pass_service.issue_gate_pass(conn, req_id, user=user, **body.model_dump())


@router.get("/gate-passes/{gp_id}")
async def get_gate_pass(
    request: Request,
    gp_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await gate_pass_service.get_gate_pass(conn, gp_id)


@router.post("/gate-passes/{gp_id}/print")
async def print_gate_pass(
    request: Request,
    gp_id: int,
    user: AuthUser = Depends(require_permission("sample", "gate_pass", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        gp = await gate_pass_service.register_print(conn, gp_id, user=user)
        req_id = (gp.get("sample_details") or {}).get("requisition_id")
        if req_id:
            items = await conn.fetch(
                "SELECT sku_name, required_qty, issued_qty, uom "
                "FROM sample_requisition_articles WHERE requisition_id = $1 ORDER BY id", req_id)
            gp["items"] = [dict(r) for r in items]
        pdf = generate_sample_gate_pass_pdf(gp)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={gp['gate_pass_number']}.pdf"})


@router.post("/gate-passes/{gp_id}/void")
async def void_gate_pass(
    request: Request,
    gp_id: int,
    body: schemas.VoidBody,
    user: AuthUser = Depends(require_permission("sample", "gate_pass", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await gate_pass_service.void_gate_pass(conn, gp_id, reason=body.reason, user=user)


# ── Internal -> external conversion ───────────────────────────────────────
@router.post("/requisitions/{req_id}/convert-full")
async def convert_full(
    request: Request,
    req_id: int,
    body: schemas.ConvertFullBody,
    user: AuthUser = Depends(require_permission("sample", "convert", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await conversion_service.convert_full(conn, req_id, user=user, payload=body.model_dump())


@router.post("/requisitions/{req_id}/convert-partial")
async def convert_partial(
    request: Request,
    req_id: int,
    body: schemas.ConvertPartialBody,
    user: AuthUser = Depends(require_permission("sample", "convert", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await conversion_service.convert_partial(
            conn, req_id, qty=body.qty, user=user, payload=body.model_dump())
