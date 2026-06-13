"""WhatsApp notifications + inbound capture for the NPD sample-review flow.

Outbound (Meta Cloud API, same WABA / Graph pattern as the password-reset OTP in
auth.otp_service):
  • notify_npd_review   — when an NPD/TRIAL request is submitted, message the NPD
                          reviewers with the full request + Accept / Hold buttons.
  • notify_npd_updated  — when a request already under review is edited, message the
                          reviewers the updated details (same Accept / Hold buttons).
  • notify_requestor    — when NPD accepts or holds, message the REQUESTOR with the
                          outcome (the hold message carries the reason).

Inbound (handle_inbound, driven by the webhook in router.py). A reviewer acts EITHER
by tapping a template button OR by typing a command:
  • Tapping the *Accept* / *Hold* quick-reply button on a review/updated message →
    the inbound "button" payload quotes the original message (context.id = its wamid),
    which we map back to the request via wa_review_message. Accept approves it; Hold
    arms a pending-reason and asks for the reason — their NEXT message is captured as
    the hold reason ("read the reason in the next message").
  • Typing a command still works as a fallback:
        ACCEPT <req#>                 → approve
        HOLD <req#>                   → ask for the reason (next reply = reason)
        HOLD <req#> <reason…>         → hold immediately with the inline reason
The reviewer is resolved from their WhatsApp number → auth_user and must hold an NPD
role. Everything is best-effort and config-gated: when WhatsApp is disabled or
unconfigured we log and no-op (so the web flow is never blocked).

────────────────────────────────────────────────────────────────────────────────
META TEMPLATES TO REGISTER (Business Manager → WhatsApp Manager → Message templates)
All category = UTILITY, language = en. Placeholders are positional; HEADER and BODY
have INDEPENDENT numbering. The send call must supply exactly these parameters in
this order — keep the live template text aligned with the layouts below.

1. npd_request_review        (to NPD reviewers, on submit)  — env WHATSAPP_TPL_NPD_REVIEW
   HEADER (text):  New {{1}} sample request                        [ {{1}}=request no ]
   BODY:
     Request: {{1}}
     ⏎
     Company: {{2}}
     Customer: {{3}}
     Customer contact: {{4}}
     ⏎
     Target NPD article: {{5}}
     Pcs: {{6}}
     Weight per piece: {{7}} kg
     Quantity: {{8}} kg
     Warehouse: {{9}}
     ⏎
     Purpose: {{10}}
     Mode of transport: {{11}}
     Expected dispatch: {{12}}
     Description: {{13}}
     Requestor: {{14}}
     ⏎
     Tap *Accept* to approve, or *Hold* and we'll ask you for the reason.
   BUTTONS: two QUICK REPLY buttons — "Accept" and "Hold".

2. npd_request_updated       (to NPD reviewers, on edit)    — env WHATSAPP_TPL_NPD_UPDATED
   HEADER (text):  Updated NPD Sample Request {{1}}                [ {{1}}=request no ]
   BODY: identical to #1 EXCEPT the first body line is the request type, not the no:
     This request was just edited — please review the updated details.
     ⏎
     Type: {{1}}
     Company: {{2}}            … (rest {{3}}…{{14}} exactly as #1) …
     Requestor: {{14}}
     ⏎
     Tap *Accept* to approve, or *Hold* and we'll ask you for the reason.
   BUTTONS: two QUICK REPLY buttons — "Accept" and "Hold".

3. npd_request_accepted      (to the requestor, on accept)  — env WHATSAPP_TPL_NPD_ACCEPTED
   "Good news — your sample request *{{1}}* ({{2}}) has been ACCEPTED by the NPD team.
    Expected dispatch date: {{3}}."           [ 1=req no · 2=article · 3=expected date ]

4. npd_request_on_hold       (to the requestor, on hold)    — env WHATSAPP_TPL_NPD_HOLD
   "Update on your sample request *{{1}}* ({{2}}): the NPD team has placed it ON HOLD.
    Reason: {{3}}."                           [ 1=req no · 2=article · 3=hold reason ]

The reviewer-facing prompts/confirmations ("Please reply with the reason", "✓ Accepted")
are sent as plain session text (no template — the reviewer just messaged us, so the
24-hour customer-service window is open).
────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from app.modules.auth.services.phone import lookup_keys, normalize as normalize_phone

logger = logging.getLogger(__name__)

# Reuse the OTP integration's WABA/Graph config; add the sample-flow extras.
GRAPH_API_BASE = os.environ.get("WHATSAPP_GRAPH_BASE", "https://graph.facebook.com/v21.0")
TEMPLATE_LANG = os.environ.get("WHATSAPP_SAMPLE_LANG", "en")
TPL_REVIEW = os.environ.get("WHATSAPP_TPL_NPD_REVIEW", "npd_request_review")
TPL_UPDATED = os.environ.get("WHATSAPP_TPL_NPD_UPDATED", "npd_request_updated")
TPL_ACCEPTED = os.environ.get("WHATSAPP_TPL_NPD_ACCEPTED", "npd_request_accepted")
TPL_HOLD = os.environ.get("WHATSAPP_TPL_NPD_HOLD", "npd_request_on_hold")
# Verify token for the webhook GET handshake (set the same value in Meta).
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
# NPD roles permitted to ACT over WhatsApp (inbound Accept/Hold).
_NPD_ROLES = {"npd_team", "admin"}
# Roles whose members RECEIVE the review / edit messages — resolved from
# auth_user.phone, not a static list. CSV-overridable; defaults to npd_team.
_REVIEW_ROLES = [r.strip() for r in
                 os.environ.get("WHATSAPP_NPD_REVIEW_ROLES", "npd_team").split(",") if r.strip()]


def _is_truthy(s: str | None) -> bool:
    return (s or "").strip().lower() in {"1", "true", "yes", "on"}


def _wa_enabled() -> bool:
    """Mirror otp_service: WHATSAPP_ENABLED (default on) + token + phone-number id."""
    if not _is_truthy(os.environ.get("WHATSAPP_ENABLED", "true")):
        return False
    return bool(os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
                and os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip())


def _fmt_phone(raw: str) -> str:
    """E.164 with no leading '+', as the Cloud API expects."""
    return (normalize_phone(raw) or raw).lstrip("+")


def npd_review_numbers() -> list[str]:
    """Optional STATIC reviewer numbers (CSV in WHATSAPP_NPD_REVIEW_NUMBERS), added
    on top of the role-resolved numbers. Usually empty — the DB drives recipients."""
    raw = os.environ.get("WHATSAPP_NPD_REVIEW_NUMBERS", "")
    return [_fmt_phone(n) for n in raw.split(",") if n.strip()]


async def _db_review_numbers(conn) -> list[str]:
    """NPD reviewer numbers resolved from the DB: every auth_user holding an NPD
    review role (default npd_team) that has a phone on file."""
    if not _REVIEW_ROLES:
        return []
    try:
        rows = await conn.fetch(
            """SELECT u.phone
                 FROM auth_user u
                 JOIN auth_role r ON u.role_id = r.role_id
                WHERE r.role_name = ANY($1::text[])
                  AND COALESCE(u.is_active, TRUE)
                  AND u.phone IS NOT NULL AND btrim(u.phone) <> ''""",
            _REVIEW_ROLES)
    except Exception:  # noqa: BLE001 — a lookup error must never block the lifecycle
        logger.exception("Failed to resolve NPD reviewer numbers from DB")
        return []
    return [_fmt_phone(r["phone"]) for r in rows]


async def _resolve_recipients(conn) -> list[str]:
    """Union of role-resolved (DB) + any static env numbers, de-duped, order-stable."""
    out: list[str] = []
    seen: set[str] = set()
    for n in (await _db_review_numbers(conn)) + npd_review_numbers():
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


async def _post(payload: dict[str, Any]) -> dict[str, Any]:
    """POST one message to the Cloud API. Log-only no-op when disabled. Never
    raises to the caller — notifications must not break the lifecycle action."""
    if not _wa_enabled():
        logger.info("WhatsApp disabled/unconfigured — would send: %s", str(payload)[:300])
        return {"dev_fallback": True}
    token = os.environ["WHATSAPP_ACCESS_TOKEN"].strip()
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"].strip()
    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            logger.error("WhatsApp send rejected: status=%s body=%s", resp.status_code, resp.text[:400])
            return {"error": f"HTTP {resp.status_code}"}
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.exception("WhatsApp send failed")
        return {"error": str(e)}


def _wamid(resp: dict | None) -> str | None:
    """Pull the sent message's wamid from a Cloud API response, if any."""
    try:
        return ((resp or {}).get("messages") or [{}])[0].get("id")
    except (AttributeError, IndexError, TypeError):
        return None


async def _send_template(to: str, name: str, body_params: list[str],
                         header_params: list[str] | None = None) -> dict[str, Any]:
    """Send a template message. `header_params` supplies a text-header variable when
    the template's header carries one (review / updated); omit for header-less ones."""
    components: list[dict[str, Any]] = []
    if header_params:
        components.append({"type": "header",
                           "parameters": [{"type": "text", "text": str(p)} for p in header_params]})
    components.append({"type": "body",
                       "parameters": [{"type": "text", "text": str(p)} for p in body_params]})
    return await _post({
        "messaging_product": "whatsapp", "to": _fmt_phone(to), "type": "template",
        "template": {"name": name, "language": {"code": TEMPLATE_LANG}, "components": components},
    })


async def _send_text(to: str, text: str) -> dict[str, Any]:
    return await _post({
        "messaging_product": "whatsapp", "to": _fmt_phone(to),
        "type": "text", "text": {"body": text},
    })


# ── request → template parameters ───────────────────────────────────────────
def _num(v: Any) -> str:
    """Tidy a numeric (Decimal/float/int) for a template: 10, 0.3, 3 — no trailing
    zeros. Meta rejects empty parameters, so None renders as an em dash."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _txt(v)          # blank / non-numeric → em-dash (never an empty param)
    if f == int(f):
        return str(int(f))
    return ("%.3f" % f).rstrip("0").rstrip(".")


def _txt(v: Any, dash: str = "—") -> str:
    """Non-empty single-line text. Meta rejects a body param that is empty or that
    contains a newline, a tab, or a run of >4 spaces — so collapse ALL whitespace
    runs to a single space and fall back to an em-dash when blank."""
    s = re.sub(r"\s+", " ", "" if v is None else str(v)).strip()
    return s or dash


async def _augment_requestor(conn, req: dict) -> None:
    """Fill a missing requestor label from the auth_user full name (display only)."""
    if (req.get("requestor_team") or "").strip():
        return
    uid = req.get("requestor_user_id")
    if uid:
        try:
            name = await conn.fetchval("SELECT full_name FROM auth_user WHERE user_id = $1", uid)
        except Exception:  # noqa: BLE001 — display nicety, never block the send
            name = None
        if name:
            req["requestor_team"] = name


def _req_no(req: dict) -> str:
    return _txt(req.get("requisition_number") or req.get("request_id") or req.get("id"))


def _common_body_tail(req: dict) -> list[str]:
    """Body params {{3}}…{{14}} for both review + updated (Company onward is shared;
    {{2}} differs and is prepended by the caller)."""
    exp = req.get("expected_dispatch_date")
    return [
        _txt(req.get("customer_name")),                       # {{3}} Customer
        _txt(req.get("customer_contact")),                    # {{4}} Customer contact
        _txt(req.get("npd_target_name")),                     # {{5}} Target NPD article
        _num(req.get("pcs")),                                 # {{6}} Pcs
        _num(req.get("weight_per_piece")),                    # {{7}} Weight per piece
        _num(req.get("quantity")),                            # {{8}} Quantity
        _txt(req.get("warehouse")),                           # {{9}} Warehouse
        _txt(req.get("purpose_tag") or req.get("purpose_note")),  # {{10}} Purpose
        _txt(req.get("mode_of_transport")),                   # {{11}} Mode of transport
        str(exp)[:10] if exp else "TBC",                      # {{12}} Expected dispatch
        _txt(req.get("description")),                          # {{13}} Description
        _txt(req.get("requestor_team")),                      # {{14}} Requestor
    ]


def _review_params(req: dict) -> tuple[list[str], list[str]]:
    """(header, body) for npd_request_review — body {{1}}=request no."""
    body = [_req_no(req), _txt(req.get("company_name"))] + _common_body_tail(req)
    return [_req_no(req)], body


def _updated_params(req: dict) -> tuple[list[str], list[str]]:
    """(header, body) for npd_request_updated — body {{1}}=request type."""
    type_label = "Trial" if req.get("sample_type") == "TRIAL" else "NPD"
    body = [type_label, _txt(req.get("company_name"))] + _common_body_tail(req)
    return [_req_no(req)], body


# ── outbound notifications ──────────────────────────────────────────────────
async def _store_review_message(conn, wamid: str, req_id: int, kind: str, wa_phone: str) -> None:
    """Remember which request a sent template message was about, so a later button
    tap (context.id = this wamid) resolves back to it. Best-effort."""
    try:
        await conn.execute(
            """INSERT INTO wa_review_message (wamid, requisition_id, kind, wa_phone)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (wamid) DO UPDATE
                 SET requisition_id = EXCLUDED.requisition_id,
                     kind = EXCLUDED.kind, wa_phone = EXCLUDED.wa_phone""",
            wamid, req_id, kind, wa_phone)
    except Exception:  # noqa: BLE001 — mapping is a convenience; text commands still work
        logger.exception("Failed to store wa_review_message for wamid %s", wamid)


async def _notify_reviewers(conn, req: dict, *, template: str, kind: str,
                            params: tuple[list[str], list[str]]) -> None:
    nums = await _resolve_recipients(conn)
    if not nums:
        # WARNING (not INFO): a silent "0 delivered" almost always traces here —
        # no user in these roles has a phone on file.
        logger.warning("No NPD reviewers with a phone (roles=%s) — skipping %s notify; "
                       "assign the role + a phone on auth_user, or set "
                       "WHATSAPP_NPD_REVIEW_NUMBERS", _REVIEW_ROLES, kind)
        return
    header, body = params
    req_id = req.get("id")
    for n in nums:
        resp = await _send_template(n, template, body, header_params=header)
        wamid = _wamid(resp)
        if wamid and req_id is not None:
            await _store_review_message(conn, wamid, req_id, kind, n)
        elif isinstance(resp, dict) and resp.get("error"):
            logger.warning("%s notify to %s failed: %s (template %s — check it is "
                           "Approved in WhatsApp Manager)", kind, n, resp.get("error"), template)


async def notify_npd_review(conn, req: dict) -> None:
    """Message the NPD reviewers that an NPD/TRIAL request needs a verdict."""
    await _augment_requestor(conn, req)
    await _notify_reviewers(conn, req, template=TPL_REVIEW, kind="REVIEW",
                            params=_review_params(req))


async def notify_npd_updated(conn, req: dict) -> None:
    """Message the NPD reviewers that a request already under review was edited."""
    await _augment_requestor(conn, req)
    await _notify_reviewers(conn, req, template=TPL_UPDATED, kind="UPDATED",
                            params=_updated_params(req))


async def _requestor_phone(conn, req: dict) -> str | None:
    uid = req.get("requestor_user_id")
    if not uid:
        return None
    return await conn.fetchval("SELECT phone FROM auth_user WHERE user_id = $1", uid)


async def notify_requestor(conn, req: dict, *, action: str, reason: str | None = None) -> None:
    """Tell the requestor the outcome. action in {'APPROVE','HOLD'}."""
    phone = await _requestor_phone(conn, req)
    if not phone:
        logger.info("Requestor has no phone — skipping outcome notify for req %s", req.get("id"))
        return
    req_no = req.get("requisition_number") or str(req.get("request_id") or req.get("id"))
    target = req.get("npd_target_name") or "—"
    if action == "APPROVE":
        # At accept time the trial hasn't closed, so the confirmed dispatch date is
        # not known yet — show the BD team's EXPECTED dispatch date instead.
        disp = req.get("expected_dispatch_date")
        tpl = TPL_ACCEPTED
        resp = await _send_template(phone, tpl, [req_no, target, str(disp)[:10] if disp else "TBC"])
    elif action == "HOLD":
        tpl = TPL_HOLD
        resp = await _send_template(phone, tpl, [req_no, target, reason or "—"])
    else:
        return
    # Surface a missing/unapproved outcome template instead of swallowing it — these
    # two templates (npd_request_accepted / npd_request_on_hold) must exist in
    # WhatsApp Manager or the requestor silently gets nothing.
    if isinstance(resp, dict) and resp.get("error"):
        logger.warning("Requestor %s notify failed for req %s: %s — is template '%s' "
                       "registered + Approved (lang %s)?",
                       action, req.get("id"), resp.get("error"), tpl, TEMPLATE_LANG)


# ── inbound: pending-reason state ───────────────────────────────────────────
async def _set_pending_hold(conn, wa_phone: str, req_id: int) -> None:
    await conn.execute(
        """INSERT INTO wa_pending_action (wa_phone, requisition_id, action)
           VALUES ($1, $2, 'HOLD')
           ON CONFLICT (wa_phone) DO UPDATE
             SET requisition_id = EXCLUDED.requisition_id, action = 'HOLD', created_at = NOW()""",
        wa_phone, req_id)


async def _pop_pending(conn, wa_phone: str) -> dict | None:
    row = await conn.fetchrow(
        "DELETE FROM wa_pending_action WHERE wa_phone = $1 RETURNING requisition_id, action", wa_phone)
    return dict(row) if row else None


async def _resolve_reviewer(conn, wa_phone: str) -> dict | None:
    """Map the inbound WhatsApp number to an NPD-authorised auth_user."""
    row = await conn.fetchrow(
        """SELECT u.user_id, COALESCE(r.role_name, '') AS role_name
             FROM auth_user u
             LEFT JOIN auth_role r ON u.role_id = r.role_id
            WHERE u.phone = ANY($1::text[])
            LIMIT 1""",
        lookup_keys(wa_phone))
    if not row:
        return None
    if row["role_name"] not in _NPD_ROLES:
        return None
    return dict(row)


async def _find_req(conn, ref: str) -> dict | None:
    """Resolve a request by its request_id (8-digit) or requisition_number (SMP-…)."""
    return await conn.fetchrow(
        """SELECT id, status, sample_type, requisition_number
             FROM sample_requisitions
            WHERE deleted_at IS NULL AND (request_id::text = $1 OR requisition_number = $1)
            LIMIT 1""",
        ref.strip())


async def _req_for_wamid(conn, wamid: str | None) -> dict | None:
    """Resolve the request a tapped Accept/Hold button refers to via its quoted
    message id (context.id = the wamid of the review/updated message we sent)."""
    if not wamid:
        return None
    rid = await conn.fetchval("SELECT requisition_id FROM wa_review_message WHERE wamid = $1", wamid)
    if rid is None:
        return None
    return await conn.fetchrow(
        """SELECT id, status, sample_type, requisition_number
             FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL""", rid)


class _WaUser:
    """Minimal user object for act_npd_review (uses .user_id / .role_name)."""
    def __init__(self, user_id: int, role_name: str):
        self.user_id = user_id
        self.role_name = role_name
        self.is_admin = role_name == "admin"
        self.full_name = "WhatsApp"


async def handle_inbound(conn, *, from_phone: str, text: str, context_id: str | None = None) -> dict:
    """Parse one inbound message from an NPD reviewer and act. Returns a small
    result dict (for the webhook log / tests). Never raises — replies guide the
    reviewer instead. The DB writes for act_npd_review run in its own txn.

    `context_id` is the wamid of the message the reply quotes — present when the
    reviewer taps an Accept/Hold quick-reply button — and is how a button tap (which
    carries no request number) is mapped back to its request."""
    from app.modules.sample.services import approval_service  # lazy: avoid import cycle

    wa = _fmt_phone(from_phone)
    body = (text or "").strip()
    reviewer = await _resolve_reviewer(conn, wa)
    if reviewer is None:
        await _send_text(wa, "Sorry, this number isn't recognised as an NPD reviewer.")
        return {"ok": False, "reason": "unauthorised"}
    user = _WaUser(reviewer["user_id"], reviewer["role_name"])

    # A button tap (context_id present) or an explicit ACCEPT/APPROVE/HOLD verb is a
    # NEW action — never a hold reason — even while a hold-reason prompt is open.
    first = body.split(maxsplit=1)[0].upper() if body else ""
    is_new_action = bool(context_id) or first in ("ACCEPT", "APPROVE", "HOLD")

    # 1) Awaiting a hold reason from a prior "HOLD"? A plain free-text reply IS the
    #    reason. But if the reviewer instead taps a button or sends a command, drop
    #    the stale prompt and handle that action — don't swallow it as the reason.
    pending = await _pop_pending(conn, wa)
    if pending and not is_new_action:
        if not body:
            await _set_pending_hold(conn, wa, pending["requisition_id"])  # re-arm
            await _send_text(wa, "Please send the hold reason as a text message.")
            return {"ok": False, "reason": "empty_reason"}
        return await _apply(conn, user, wa, pending["requisition_id"], "HOLD", body, approval_service)

    # 2) Otherwise parse a command. ACCEPT/APPROVE/HOLD may arrive as a typed
    #    command ("HOLD <req#> [reason]") OR as a bare button tap ("Accept"/"Hold"),
    #    in which case the request is resolved from the quoted message (context_id).
    parts = body.split(maxsplit=2)
    cmd = (parts[0].upper() if parts else "")
    if cmd == "APPROVE":
        cmd = "ACCEPT"
    if cmd not in ("ACCEPT", "HOLD"):
        await _send_text(wa, "Reply  ACCEPT <request#>  or  HOLD <request#>  — "
                             "or tap the Accept / Hold button on the request.")
        return {"ok": False, "reason": "unparsed"}

    ref = parts[1] if len(parts) >= 2 else None
    req = await _find_req(conn, ref) if ref else await _req_for_wamid(conn, context_id)
    if req is None:
        hint = (f"Couldn't find request {ref}." if ref
                else "Couldn't tell which request that was — reply ACCEPT <request#> or HOLD <request#>.")
        await _send_text(wa, hint)
        return {"ok": False, "reason": "not_found"}

    if cmd == "ACCEPT":
        return await _apply(conn, user, wa, req["id"], "APPROVE", None, approval_service)

    # HOLD: inline reason → act now; else arm pending and ask for the reason.
    # Tolerate a "HOLD <req#> : reason" / "- reason" separator.
    reason = parts[2].strip().lstrip(":-").strip() if len(parts) >= 3 else ""
    if reason:
        return await _apply(conn, user, wa, req["id"], "HOLD", reason, approval_service)
    await _set_pending_hold(conn, wa, req["id"])
    await _send_text(wa, f"Holding {req['requisition_number']}. Please reply with the reason.")
    return {"ok": True, "awaiting": "reason", "requisition_id": req["id"]}


def extract_messages(payload: dict) -> list[dict]:
    """Flatten a Meta webhook payload into [{from, text, context_id, type}]. Pulls
    the text body from text / quick-reply button / interactive-reply message shapes
    and the quoted-message id from `context.id` (set when the reviewer taps a button
    or replies to a message); ignores delivery-status callbacks (no 'messages')."""
    out: list[dict] = []
    for entry in (payload or {}).get("entry", []):
        for change in entry.get("changes", []):
            for m in (change.get("value") or {}).get("messages", []):
                frm, t = m.get("from"), m.get("type")
                if t == "text":
                    text = (m.get("text") or {}).get("body", "")
                elif t == "button":
                    b = m.get("button") or {}
                    text = b.get("text") or b.get("payload") or ""
                elif t == "interactive":
                    inter = m.get("interactive") or {}
                    br = inter.get("button_reply") or inter.get("list_reply") or {}
                    text = br.get("title") or br.get("id") or ""
                else:
                    text = ""
                if frm:
                    out.append({"from": frm, "text": text, "type": t,
                                "context_id": (m.get("context") or {}).get("id")})
    return out


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Validate Meta's X-Hub-Signature-256 over the raw body. Only enforced when
    WHATSAPP_APP_SECRET is configured; otherwise returns True (open)."""
    import hashlib
    import hmac
    secret = os.environ.get("WHATSAPP_APP_SECRET", "").strip()
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])


async def _apply(conn, user, wa: str, req_id: int, action: str, reason, approval_service) -> dict:
    """Run act_npd_review and reply with the outcome. Translates the service's
    HTTPException into a friendly WhatsApp message instead of bubbling up."""
    from fastapi import HTTPException
    try:
        updated = await approval_service.act_npd_review(
            conn, req_id, action=action, user=user, reason=reason)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        await _send_text(wa, f"Couldn't {action.lower()}: {detail.get('message', 'not allowed')}")
        return {"ok": False, "reason": detail.get("error", "error")}
    no = updated.get("requisition_number")
    await _send_text(wa, f"✓ {no} {'accepted' if action == 'APPROVE' else 'put on hold'}."
                         + (f" Reason: {reason}" if action == "HOLD" else ""))
    return {"ok": True, "requisition_id": req_id, "action": action}
