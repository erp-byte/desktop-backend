"""/api/v1/transfer/request-email-action - the Accept / Hold / Reject and Approve /
Hold / Reject links in the transfer request notice emails (services/request_notify.py).

PUBLIC: a click from a mail app carries no login, so the signed token is the
authorization (services/request_action_token.py: the request, the action and the ERP
user the mail was sent to; 14 days). It is a router of its own, registered in
app/main.py, and its path is outside /requests/{request_id}, which the transfer router
would otherwise claim.

  GET   shows the request and asks: Accept / Approve behind a Confirm button, Hold and
        Reject with a box for the reason. Nothing changes on a GET, so a mailbox's link scanner
        opening the link cannot decide the request (the sample module's email links are
        built the same way). A request that cannot be decided now - moved on, or the
        user may not decide it - is said so at once.
  POST  makes the decision as the user the link was sent to, through
        request_action.decide, so the ERP's checks apply unchanged. A request that has
        moved on (decided in the ERP, or from another copy of the mail) is left as it is
        and the page says nothing was done and what state it is in: a link used twice
        only reports the state the second time.

The pages are customer_returns' email-action card.
"""
from __future__ import annotations

from html import escape
from typing import Any, Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from app.core.sites import canonical_site
from app.modules.transfer.services import request_action
from app.modules.transfer.services.request_action_token import verify_token
from app.modules.transfer.services.request_notify import ACTION_PATH, item_text

router = APIRouter(prefix="/api/v1/transfer", tags=["Transfer"])

GREEN, ORANGE, RED = "#27ae60", "#e67e22", "#c0392b"
_PATH = ACTION_PATH[len(router.prefix):]          # "/request-email-action"
_CONFIRM = {"accept": ("Accept this transfer request?", "Confirm accept", GREEN),
            "approve": ("Approve this transfer request?", "Confirm approve", GREEN),
            "store_hold": ("Put this transfer request on hold?", "Put on hold", ORANGE),
            "hold": ("Put this transfer request on hold?", "Put on hold", ORANGE),
            "store_reject": ("Reject this transfer request?", "Reject request", RED),
            "reject": ("Reject this transfer request?", "Reject request", RED)}
_REASON_LABEL = {"store_hold": "Reason for the hold", "hold": "Reason for the hold",
                 "store_reject": "Reason for rejecting", "reject": "Reason for rejecting"}
_INVALID = ("Link invalid or expired",
            "This link is no longer valid, so nothing was done. Open the request in the ERP to answer it.")
_NOT_YOURS = ("Link not valid for your account",
              "This link was sent to an account that is no longer active or no longer has this email "
              "address, so nothing was done. Open the request in the ERP to answer it.")


def _page(title: str, message: str, color: str, *, body: str = "", status: int = 200) -> HTMLResponse:
    """customer_returns' confirmation card, with room for the request and a form."""
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        f"<title>{escape(title)}</title></head>"
        "<body style='font-family:Arial,sans-serif;background:#f4f4f5;margin:0;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;'>"
        "<div style='background:#fff;border-radius:12px;padding:32px 36px;max-width:480px;width:100%;"
        "box-sizing:border-box;margin:16px;text-align:center;box-shadow:0 6px 24px rgba(0,0,0,.08);'>"
        f"<div style='width:56px;height:56px;border-radius:50%;background:{color};margin:0 auto 16px;'></div>"
        f"<h2 style='margin:0 0 8px;color:{color};'>{escape(title)}</h2>"
        f"<p style='margin:0;color:#444;font-size:15px;'>{escape(message)}</p>"
        f"{body}</div></body></html>")
    return HTMLResponse(html, status_code=status)


def _summary(req: dict[str, Any]) -> str:
    rows = [("Request no", (req.get("request_no") or "").strip() or f"#{req.get('id')}"),
            ("From", canonical_site(req.get("from_warehouse")) or "-"),
            ("To", canonical_site(req.get("to_warehouse")) or "-"),
            ("Reason", (req.get("reason_description") or "").strip() or "-"),
            ("Raised by", req.get("created_by") or "-")]
    info = "".join(f"<tr><td style='padding:2px 10px 2px 0;color:#666;'>{escape(k)}</td>"
                   f"<td style='padding:2px 0;font-weight:600;'>{escape(str(v))}</td></tr>" for k, v in rows)
    items = "".join(f"<li>{escape(item_text(l))}</li>" for l in req.get("lines") or []) or "<li>-</li>"
    return ("<div style='text-align:left;margin:18px 0 0;font-size:14px;color:#222;'>"
            f"<table style='border-collapse:collapse;'>{info}</table>"
            f"<div style='margin:10px 0 4px;color:#666;'>Items</div>"
            f"<ul style='margin:0;padding-left:18px;'>{items}</ul></div>")


def _form(token: str, action: str, req: dict[str, Any], *, error: Optional[str] = None,
          reason: str = "") -> HTMLResponse:
    title, button, color = _CONFIRM[action]
    needs_reason = request_action.STEPS[action].needs_reason
    reason_box = ""
    if needs_reason:
        reason_box = (
            "<label for='reason' style='display:block;text-align:left;margin:18px 0 6px;font-weight:600;'>"
            f"{_REASON_LABEL[action]}</label>"
            "<textarea id='reason' name='reason' rows='4' maxlength='500' required "
            "style='width:100%;box-sizing:border-box;padding:8px;font:inherit;border:1px solid #ccc;"
            f"border-radius:6px;'>{escape(reason)}</textarea>")
    error_html = f"<p style='color:{RED};margin:12px 0 0;'>{escape(error)}</p>" if error else ""
    body = (
        f"{_summary(req)}"
        f"<form method='post' action='{escape(ACTION_PATH)}' style='margin:0;'>"
        f"<input type='hidden' name='token' value='{escape(token)}'>"
        f"{reason_box}{error_html}"
        f"<button type='submit' style='margin-top:18px;background:{color};color:#fff;font-size:15px;"
        f"font-weight:600;border:0;border-radius:6px;padding:12px 32px;cursor:pointer;'>{escape(button)}</button>"
        "</form>"
        "<p style='font-size:12px;color:#999;margin:14px 0 0;'>You can close this tab to cancel; "
        "nothing changes until you confirm.</p>")
    question = ("The store's answer is needed." if request_action.STEPS[action].stage == "store"
                else "An inventory manager's decision is needed.")
    return _page(title, question, color, body=body)


async def _user_for(conn, claims: dict):
    """The active user the link was sent to, while their account still has the address
    it was sent to; else None."""
    user = await request_action.load_user(conn, user_id=claims["uid"])
    if user is None or (user.email or "").strip().lower() != claims["em"]:
        return None
    return user


@router.get(_PATH, response_class=HTMLResponse)
async def transfer_request_email_action_page(request: Request, token: str = Query("")):
    claims = verify_token(token)
    if not claims:
        return _page(*_INVALID, RED, status=400)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        user = await _user_for(conn, claims)
        if user is None:
            return _page(*_NOT_YOURS, RED, status=403)
        refusal, req = await request_action.precheck(conn, user, claims["rid"], claims["act"])
    if refusal is not None:
        return _page("Nothing was done", refusal["message"], ORANGE)
    return _form(token, claims["act"], req)


@router.post(_PATH, response_class=HTMLResponse)
async def transfer_request_email_action_submit(request: Request, token: str = Form(""), reason: str = Form("")):
    claims = verify_token(token)
    if not claims:
        return _page(*_INVALID, RED, status=400)
    action, request_id = claims["act"], claims["rid"]
    reason = (reason or "").strip()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        user = await _user_for(conn, claims)
        if user is None:
            return _page(*_NOT_YOURS, RED, status=403)
        if request_action.STEPS[action].needs_reason and not reason:
            refusal, req = await request_action.precheck(conn, user, request_id, action)
            if refusal is not None:
                return _page("Nothing was done", refusal["message"], ORANGE)
            return _form(token, action, req, error="Please type the reason.")
        outcome = await request_action.decide(conn, user, request_id, action, reason or None)
    if outcome["ok"]:
        return _page(f"Transfer request {request_action.STEPS[action].done}", outcome["message"], GREEN)
    return _page("Nothing was done", outcome["message"], ORANGE)
