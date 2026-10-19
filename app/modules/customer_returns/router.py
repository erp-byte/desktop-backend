"""/api/v1/customer-returns/* — Customer-Returns module (Phase 1: header+lines CRUD).

Thin router; every endpoint requires a valid access token and derives the actor
from the JWT (never from request params) — except the public /email-action pair,
where the signed link token names the approver. Company is a CFPL/CDPL path
segment mapped to a table prefix by the service layer. Who may decide a return,
and when, is approval_service's rule for every channel.
"""
from __future__ import annotations

import logging
from html import escape
from io import BytesIO
from typing import Optional
from zoneinfo import ZoneInfo
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.customer_returns import schemas
from app.modules.customer_returns.approval_token import verify_action_token
from app.modules.customer_returns.services import (
    create_service, query_service, box_service, export_xlsx,
    mail_service, approval_service, wa_notify,
)

logger = logging.getLogger(__name__)


class CRDecisionRequest(BaseModel):
    action: str  # approve | reject | hold
    remark: str | None = None


def _action_page(title: str, message: str, color: str, *, body: str = "",
                 auto_close: bool = True) -> str:
    """Self-contained HTML card for the emailed magic-link pages: the result cards,
    with a best-effort self-close (the BH usually opens them from a mail app), and —
    with ``body`` and auto_close=False — the confirmation form, which must stay open."""
    close = ("<script>setTimeout(function(){try{window.close();}catch(e){}},1200);</script>"
             if auto_close else "")
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta name='robots' content='noindex,nofollow'>"
        f"<title>{escape(title)}</title></head>"
        f"<body style='font-family:Arial,sans-serif;background:#f4f4f5;margin:0;"
        f"display:flex;min-height:100vh;align-items:center;justify-content:center;'>"
        f"<div style='background:#fff;border-radius:12px;padding:32px 40px;max-width:420px;"
        f"width:100%;box-sizing:border-box;margin:16px;"
        f"text-align:center;box-shadow:0 6px 24px rgba(0,0,0,.08);'>"
        f"<div style='width:56px;height:56px;border-radius:50%;background:{color};margin:0 auto 16px;'></div>"
        f"<h2 style='margin:0 0 8px;color:{color};'>{escape(title)}</h2>"
        f"<p style='margin:0;color:#444;font-size:15px;'>{escape(message)}</p>{body}</div>"
        f"{close}</body></html>"
    )

router = APIRouter(prefix="/api/v1/customer-returns", tags=["Customer Returns"])
_IST = ZoneInfo("Asia/Kolkata")

# ── emailed magic link: the confirmation page and its POST ────────────────────
_EMAIL_ACTION_PATH = f"{router.prefix}/email-action"
_ACTION_STYLE = {action: (label, color) for action, label, color in mail_service.ACTION_META}
_CONFIRM_TITLE = {"approve": "Approve this customer return?",
                  "reject": "Reject this customer return?",
                  "hold": "Put this customer return on hold?"}
_RED, _ORANGE, _GREEN = "#c0392b", "#e67e22", "#27ae60"
_PAUSED = ("Approvals paused",
           "Customer-return approvals are handled in IMS for now. No action was taken.")
_INVALID_LINK = ("Link invalid or expired",
                 "This approval link is no longer valid. Open the CR in the app to decide.")


def _confirm_page(token: str, action: str, cr: dict) -> str:
    """The GET page: the return, the chosen action and a Confirm button that POSTs the
    token back (plus an optional remark for reject / hold). Nothing has changed yet."""
    label, color = _ACTION_STYLE[action]
    rows = [("Customer return", cr.get("rtv_id")), ("Customer", cr.get("customer")),
            ("Factory unit", cr.get("factory_unit")), ("Invoice no", cr.get("invoice_number")),
            ("Business Head", cr.get("business_head")), ("Raised by", cr.get("created_by")),
            ("Status", approval_service.status_label(cr.get("status"))), ("Your decision", label)]
    info = "".join(
        f"<tr><td style='padding:3px 12px 3px 0;color:#666;vertical-align:top;'>{escape(k)}</td>"
        f"<td style='padding:3px 0;font-weight:600;word-break:break-word;'>{escape(str(v or '—'))}</td></tr>"
        for k, v in rows)
    remark = ""
    if action in ("reject", "hold"):
        remark = (
            "<label for='remark' style='display:block;text-align:left;margin:16px 0 6px;"
            "font-weight:600;font-size:14px;'>Remark (optional)</label>"
            "<textarea id='remark' name='remark' rows='3' maxlength='500' "
            "style='width:100%;box-sizing:border-box;padding:8px;font:inherit;font-size:14px;"
            "border:1px solid #ccc;border-radius:6px;'></textarea>")
    body = (
        f"<table style='border-collapse:collapse;text-align:left;margin:18px auto 0;font-size:14px;"
        f"color:#222;'>{info}</table>"
        f"<form method='post' action='{escape(_EMAIL_ACTION_PATH)}' style='margin:0;'>"
        f"<input type='hidden' name='token' value='{escape(token)}'>{remark}"
        f"<button type='submit' style='margin-top:18px;background:{color};color:#fff;font-size:15px;"
        f"font-weight:600;border:0;border-radius:6px;padding:12px 32px;cursor:pointer;'>"
        f"Confirm {escape(label.lower())}</button></form>"
        "<p style='font-size:12px;color:#999;margin:14px 0 0;'>You can close this tab to cancel; "
        "nothing changes until you confirm.</p>")
    return _action_page(_CONFIRM_TITLE[action], "Check the return, then confirm your decision.",
                        color, body=body, auto_close=False)


async def _notify_decision(conn, company: str, cr_id: str, status: str, actor: str,
                           remark: Optional[str] = None) -> None:
    """The threaded status-change mail after a decision. Best-effort: the decision is
    already committed, so a failure here is logged, never reported as a failed decision."""
    try:
        detail = await query_service.get_cr(conn, company, cr_id)
        detail["company"] = company
        await mail_service.notify_cr_status_changed(conn, detail, status, actor, remark=remark)
    except Exception:  # noqa: BLE001
        logger.exception("[cr] status-change mail failed cr=%s", cr_id)


# ── send-for-approval outcome ─────────────────────────────────────────────────
def _send_status(mail: dict, wa: dict, has_decider: bool) -> str:
    """'sent' when every mail copy went out and WhatsApp did not fail (skipped — not
    configured, no number on file — is not a failure: the mail carries the same
    buttons); 'failed' when nobody who can decide was reached on either channel, or
    nobody can decide the CR at all; 'partial' in between. Only deciders count —
    the person who raised the return is never one (mail_service.eligible_approvers)."""
    if has_decider and mail.get("sent") and wa.get("status") in ("sent", "skipped"):
        return "sent"
    reached = has_decider and (mail.get("approvers_mailed", 0) > 0 or wa.get("sent", 0) > 0)
    return "partial" if reached else "failed"


def _public_wa(wa: dict) -> dict:
    """The WhatsApp outcome for the response: counts, reason and who was reached —
    no phone numbers or wamids (a wamid is what a forged tap would quote)."""
    out = {k: wa[k] for k in ("status", "sent", "failed", "reason") if k in wa}
    out["recipients"] = [{"name": r.get("name"), "sent": bool(r.get("sent"))}
                         for r in wa.get("recipients") or []]
    return out


def _actor(user: AuthUser) -> str:
    return user.email or user.full_name or str(user.user_id)


# ── Read-only gate ────────────────────────────────────────────────────────────
# Phase 2 (write path on the shared live _rtv_* tables) is built + verified, so
# writes are ENABLED. The guard mechanism is retained as a one-line kill switch:
# set CR_READ_ONLY = True to instantly fall back to the read-only phase (all 9
# mutating endpoints 403, email-action shows an "approvals paused" card).
CR_READ_ONLY = False


def _ensure_writable() -> None:
    if CR_READ_ONLY:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "read_only",
                "message": "Customer Returns is read-only in CFERP for now — "
                           "create or edit returns in IMS.",
            },
        )


@router.get("/export")
async def export_customer_returns(
    request: Request,
    company: str = Query(..., description="CFPL or CDPL"),
    status: Optional[str] = Query(None),
    customer: Optional[str] = Query(None),
    factory_unit: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    to_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    sort_by: str = Query("created_ts"),
    sort_order: str = Query("desc"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await query_service.export_cr_records(
            conn, company=company, status=status, customer=customer,
            factory_unit=factory_unit, from_date=from_date, to_date=to_date,
            sort_by=sort_by, sort_order=sort_order,
        )
        rtv_ids = list({r["RTV ID"] for r in rows if r["RTV ID"]})
        edited = await query_service.get_edited_cells(conn, rtv_ids)
    buf: BytesIO = export_xlsx.build_export_workbook(rows, edited)
    stamp = datetime.now(_IST).strftime("%Y%m%d")
    filename = f"customer_returns_{company}_{stamp}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/box-edit-log", response_model=schemas.CRBoxEditLogResponse,
             dependencies=[Depends(_ensure_writable)])
async def log_customer_return_box_edits(
    body: schemas.CRBoxEditLogRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.log_box_edits(conn, body, email_id=_actor(user))


@router.get("/email-routing")
async def get_email_routing(
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    """Customer-Returns approval e-mail routing (DB-backed; replaces the old
    hardcoded maps). Company-agnostic. Consumed by the FE _approvalMatrix.ts and
    the create form's Business-Head / Sales-POC dropdowns. Declared BEFORE the
    ``/{company}`` routes so "email-routing" isn't captured as a company segment.

    business_head / sales_poc are the PEOPLE holding the ``business_head`` /
    ``sales`` roles (auth_user) — the source of truth for who is a BH / sales POC,
    assigned on the Admin screen. warehouse_cc / constant_cc / notify_to have no
    user/role source and come from the cr_email_routing config table.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await mail_service.fetch_routing(conn)


@router.get("/email-action", response_class=HTMLResponse)
async def customer_return_email_action(request: Request, token: str = Query("")):
    """PUBLIC landing page for the emailed Approve/Reject/Hold buttons — a browser
    click carries no login, so the signed token says who the link is for. It changes
    NOTHING: it checks the link, shows the return and the chosen action, and asks for
    a Confirm that POSTs back here (with an optional remark for reject / hold), so a
    mail scanner or link preview fetching the URL can never decide a return. A return
    this address can't decide now — already decided, or not (any longer) theirs to
    decide — is said so at once. Declared BEFORE the /{company} routes.
    """
    if CR_READ_ONLY:
        return HTMLResponse(_action_page(*_PAUSED, _ORANGE), status_code=403)
    claims = verify_action_token(token)
    if not claims:
        return HTMLResponse(_action_page(*_INVALID_LINK, _RED), status_code=400)
    company, cr_id, action = claims["company"], claims["cr_id"], claims["action"]
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            cr = await approval_service.check_cr_action(conn, company, cr_id, claims["approver"])
    except HTTPException as exc:
        return HTMLResponse(_action_page("Action not available", approval_service.error_message(exc),
                                         _RED, auto_close=False), status_code=exc.status_code)
    except Exception:  # noqa: BLE001 — a mail click must get a page, not a raw 500
        logger.exception("[cr] email-action page failed cr=%s", cr_id)
        return HTMLResponse(_action_page("Something went wrong", "This return could not be loaded. "
                                         "Try the link again later, or open the CR in the app.",
                                         _RED, auto_close=False), status_code=500)
    if cr["decided"]:
        return HTMLResponse(_action_page(
            "Already actioned",
            f"This customer return is already “{approval_service.status_label(cr['status'])}”.", _ORANGE))
    return HTMLResponse(_confirm_page(token, action, cr))


@router.post("/email-action", response_class=HTMLResponse)
async def customer_return_email_action_confirm(request: Request, token: str = Form(""),
                                               remark: str = Form("")):
    """PUBLIC: the Confirm button of the page above. Verifies the token again and
    decides as the address it was minted for (its ``approver`` claim) through
    apply_cr_action — which, like for every channel, refuses anyone who is not (any
    longer) one of the CR's approvers, or who raised it. Then the threaded status
    mail (best-effort) and the result card. Idempotent: posting again, or after
    another approver decided, is told the return is already actioned. Declared
    BEFORE POST /{company}, which would otherwise take "email-action" as a company.
    """
    if CR_READ_ONLY:
        return HTMLResponse(_action_page(*_PAUSED, _ORANGE), status_code=403)
    claims = verify_action_token(token)
    if not claims:
        return HTMLResponse(_action_page(*_INVALID_LINK, _RED), status_code=400)
    company, cr_id, action = claims["company"], claims["cr_id"], claims["action"]
    actor = str(claims["approver"]).strip()
    remark = (remark or "").strip()[:500] or None
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            result = await approval_service.apply_cr_action(conn, company, cr_id, action, actor, remark)
            if not result["already_actioned"]:
                await _notify_decision(conn, company, cr_id, result["status"], actor, remark)
    except HTTPException as exc:
        return HTMLResponse(_action_page("Nothing was done", approval_service.error_message(exc),
                                         _RED, auto_close=False), status_code=exc.status_code)
    except Exception:  # noqa: BLE001 — a mail click must get a page, not a raw 500
        logger.exception("[cr] email-action decision failed cr=%s", cr_id)
        return HTMLResponse(_action_page("Something went wrong", "Your decision could not be recorded. "
                                         "Try again, or open the CR in the app.",
                                         _RED, auto_close=False), status_code=500)
    status = result["status"]
    if result["already_actioned"]:
        return HTMLResponse(_action_page(
            "Already actioned",
            f"This customer return is already “{approval_service.status_label(status)}”.", _ORANGE))
    return HTMLResponse(_action_page(f"Customer return {status}",
                                     f"{cr_id} is now “{status}”. You can close this tab.", _GREEN))


@router.post("/{company}", status_code=201, response_model=schemas.CRWithDetails,
             dependencies=[Depends(_ensure_writable)])
async def create_customer_return(
    company: str,
    body: schemas.CRCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.create_cr(conn, company, body, _actor(user))


@router.get("/{company}", response_model=schemas.CRListResponse)
async def list_customer_returns(
    company: str,
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    status: Optional[str] = Query(None),
    factory_unit: Optional[str] = Query(None),
    customer: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    to_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    sort_by: str = Query("created_ts"),
    sort_order: str = Query("desc"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.list_crs(
            conn, company=company, page=page, per_page=per_page, status=status,
            factory_unit=factory_unit, customer=customer, from_date=from_date,
            to_date=to_date, sort_by=sort_by, sort_order=sort_order,
        )


@router.get("/{company}/{cr_id}", response_model=schemas.CRWithDetails)
async def get_customer_return(
    company: str,
    cr_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.get_cr(conn, company, cr_id)


@router.put("/{company}/{cr_id}", response_model=schemas.CRHeaderResponse,
            dependencies=[Depends(_ensure_writable)])
async def update_customer_return(
    company: str,
    cr_id: str,
    body: schemas.CRHeaderUpdate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    """Header edit — never the status: CRHeaderUpdate has no status field (a sent one
    is ignored), so a return is decided only through apply_cr_action — POST /approve,
    the mailed link, the WhatsApp tap — with its who / when checks and audit."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.update_cr(conn, company, cr_id, body)


@router.put("/{company}/{cr_id}/lines", response_model=schemas.CRLinesUpdateResponse,
            dependencies=[Depends(_ensure_writable)])
async def update_customer_return_lines(
    company: str,
    cr_id: str,
    body: schemas.CRLinesUpdateRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.update_cr_lines(conn, company, cr_id, body)


@router.delete("/{company}/{cr_id}", response_model=schemas.CRDeleteResponse,
               dependencies=[Depends(_ensure_writable)])
async def delete_customer_return(
    company: str,
    cr_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.delete_cr(conn, company, cr_id)


@router.put("/{company}/{cr_id}/box", response_model=schemas.CRBoxUpsertResponse,
            dependencies=[Depends(_ensure_writable)])
async def upsert_customer_return_box(
    company: str,
    cr_id: str,
    body: schemas.CRBoxUpsertRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.upsert_box(conn, company, cr_id, body)


@router.put("/{company}/{cr_id}/boxes", response_model=schemas.CRBulkBoxUpdateResponse,
            dependencies=[Depends(_ensure_writable)])
async def bulk_save_customer_return_boxes(
    company: str,
    cr_id: str,
    body: schemas.CRBulkBoxUpdateRequest,
    request: Request,
    notify_discrepancy: bool = Query(True),
    allow_clear: bool = Query(False),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.bulk_save_boxes(
            conn, company, cr_id, body,
            notify_discrepancy=notify_discrepancy, allow_clear=allow_clear)


@router.post("/{company}/{cr_id}/send-for-approval",
             dependencies=[Depends(_ensure_writable)])
async def send_customer_return_for_approval(
    company: str,
    cr_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    """Send the approval request — the threaded mail (each approver who can decide,
    i.e. the mapped Business Head and the standing deputies minus whoever raised the
    return, gets the Approve/Reject/Hold buttons; everyone else, the creator
    included, a threaded copy) and the WhatsApp head-approval card to the same
    deciders. Allowed only while the CR is undecided (Pending / On Hold), so it is
    also the Re-send; a decided CR is 409 already_decided.

    Sending stays best-effort — a channel that fails never fails the request — but
    the response says what actually happened:
      approvers [{name, email}] who can decide it, BH first — never its creator
      mail      {sent, to, cc_count, approvers_mailed, reason?, failed?}
                reason: smtp_not_configured | no_recipients | smtp_error | error
      whatsapp  {status: sent | partial | failed | skipped, sent, failed, reason?,
                 recipients: [{name, sent}]}
      status    sent | partial | failed (_send_status), with a reason when nobody
                can decide the CR: 'no_approver' (no BH resolves and no deputy is
                configured) or 'only_creator' (its only approver raised it).
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        detail = await query_service.get_cr(conn, company, cr_id)
        if not approval_service.is_undecided(detail.get("status")):
            label = approval_service.status_label(detail.get("status"))
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "already_decided",
                    "message": f"{cr_id} is already “{label}”, so it can't be sent for approval again.",
                    "details": {"rtv_id": cr_id, "status": detail.get("status")},
                },
            )
        detail["company"] = company
        rec = await mail_service.notify_cr_created(conn, detail)
        # WhatsApp head-approval to every approver who can decide (image header +
        # Approve/Reject/Hold buttons). Best-effort — never raises; skipped if
        # WABA/phone unconfigured.
        wa = await wa_notify.notify_cr_head_approval(conn, detail)
    deciders = rec["deciders"]
    out = {
        "status": _send_status(rec["mail"], wa, bool(deciders)),
        "approver": rec["approver"],
        "approvers": deciders,
        "to": rec["to"],
        "cc_count": len(rec["cc"]),
        "mail": rec["mail"],
        "whatsapp": _public_wa(wa),
    }
    if not deciders:
        out["reason"] = "only_creator" if rec["approvers"] else "no_approver"
    if out["status"] != "sent":
        logger.warning("[cr] send-for-approval %s cr=%s mail=%s whatsapp=%s",
                       out["status"], cr_id, rec["mail"], out["whatsapp"])
    return out


@router.post("/{company}/{cr_id}/approve",
             dependencies=[Depends(_ensure_writable)])
async def decide_customer_return(
    company: str,
    cr_id: str,
    body: CRDecisionRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    """In-app Approve/Reject/Hold as the logged-in user's e-mail. apply_cr_action lets
    only the CR's Business Head or a standing deputy decide — an admin as such may
    not, nor the person who raised it (403); an account with no e-mail can't be
    matched to an approver (403) — and only while it is Pending / On Hold.
    Idempotent status transition + audit, then the threaded status-change mail
    (best-effort). (The emailed buttons go through the public /email-action page.)"""
    pool = request.app.state.db_pool
    actor = (user.email or "").strip()
    async with pool.acquire() as conn:
        result = await approval_service.apply_cr_action(conn, company, cr_id, body.action, actor, body.remark)
        if not result["already_actioned"]:
            await _notify_decision(conn, company, cr_id, result["status"], actor, body.remark)
    return result
