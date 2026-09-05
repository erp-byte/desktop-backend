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
   HEADER (text): Sample request {{1}} approved            [ header {{1}} = request no ]
   BODY:
     NPD team has ACCEPTED your sample request.
     Target NPD article: {{1}}                             [ body {{1}} = target article ]
     Expected dispatch: {{2}}                              [ body {{2}} = expected date / TBC ]
     <a closing line — body must not END on a variable>

4. npd_request_on_hold       (to the requestor, on hold)    — env WHATSAPP_TPL_NPD_HOLD
   HEADER (text): Sample request {{1}} on hold             [ header {{1}} = request no ]
   BODY:
     NPD team has placed your sample request ON HOLD.
     Target NPD article: {{1}}                             [ body {{1}} = target article ]
     Reason: {{2}}                                         [ body {{2}} = hold reason ]
     <a closing line — body must not END on a variable>

5. npd_promote_approval      (to the promote approvers)     — env WHATSAPP_TPL_NPD_PROMOTE
   The NPD dev-JC promote dual-approval gate. Sent to every inventory_manager (INV_MGR
   gate) + the requestor BH (REQUESTOR_BH gate). Two QUICK REPLY buttons — "Approve"
   and "Reject" (Reject → we ask for the reason in the next reply).
   HEADER (text): New promote approval — Dev JC {{1}}     [ {{1}} = dev JC id (8-digit) ]
   BODY ({{1}}..{{9}}):
     Your gate: {{1}}                                      [ "Inventory manager" / "Business head" ]
     Dev job card: {{2}}                                   [ dev JC id (8-digit) ]
     Target FG article: {{3}}   Target quantity: {{4}}
     Company: {{5}}   Customer: {{6}}
     Return type: {{7}}   Paid: {{8}}   Amount: {{9}}
     <a closing "Tap *Approve* … or *Reject* …" line — body must not END on a variable>

6. npd_dispatch_due_tomorrow_team  (to the NPD team, D-1)  — env WHATSAPP_TPL_DISPATCH_DUE_TEAM
   Fired by dispatch_reminder_service's daily 09:00 IST scan for every open requisition
   whose expected_dispatch_date is tomorrow. No buttons — purely informational.
   HEADER (text): Dispatch due tomorrow — {{1}}          [ header {{1}} = request no ]
   BODY ({{1}}..{{6}}):
     This sample request is due for dispatch tomorrow.
     ⏎
     Request: {{1}}                                      [ body {{1}} = request no ]
     Expected dispatch: {{2}}                            [ YYYY-MM-DD, or TBC ]
     Target article: {{3}}
     Quantity: {{4}} kg                                  [ the "kg" is literal template
                                                           text; requisition quantity is
                                                           pcs × weight_per_piece in kg ]
     Customer: {{5}}
     Warehouse: {{6}}
     ⏎
     Please make sure the trial and its output are ready in time.

7. npd_dispatch_due_tomorrow_owner (to the business head, D-1) — env WHATSAPP_TPL_DISPATCH_DUE_OWNER
   The same D-1 scan, addressed to the requisition's bound business_head_user_id. No
   buttons — the ACTIONS live on the overdue chase, which this copy points forward to.
   HEADER (text): Dispatch due tomorrow — {{1}}          [ header {{1}} = request no ]
   BODY ({{1}}..{{4}}) — FOUR vars, not the team template's six:
     The sample request you raised is due for dispatch tomorrow.
     ⏎
     Request: {{1}}                                      [ body {{1}} = request no ]
     Expected dispatch: {{2}}                            [ YYYY-MM-DD, or TBC ]
     Target article: {{3}}
     Customer: {{4}}
     ⏎
     The NPD team has been notified. If it is going to slip, the overdue reminder will
     offer a one-tap way to move the date or cancel.

8. npd_dispatch_overdue_team    (to the business head, daily) — env WHATSAPP_TPL_DISPATCH_OVERDUE
   The chase once expected_dispatch_date is in the PAST. Registered with a "_team" suffix
   but addressed to the bound business_head_user_id — they are the only one who can act.
   Repeats every day until the date moves or the request is cancelled.
   HEADER (text): Dispatch date passed — {{1}}            [ header {{1}} = request no ]
   BODY ({{1}}..{{5}}):
     The sample request you raised has passed its expected dispatch date.
     ⏎
     Request: {{1}}                                      [ body {{1}} = request no ]
     Expected dispatch: {{2}}                            [ the date it MISSED ]
     Days overdue: {{3}}
     Target article: {{4}}
     Customer: {{5}}
     ⏎
     Use a button below to set a new date or cancel the request. …
   BUTTONS: two QUICK REPLY buttons — "Change expected date" and "Cancel request".
   Both are two messages deep: the tap asks for a reason, and Change expected date then
   asks for the date itself. State lives in wa_dispatch_pending (088).

9. npd_dispatch_overdue_team_notifier (to the NPD team, daily) — env WHATSAPP_TPL_DISPATCH_OVERDUE_TEAM
   The same chase as #8, informational, to the npd_team pool. Identical HEADER + five BODY
   vars; NO buttons — the actions belong to the business head alone. Closing line differs:
     Its business head has been asked to cancel it or set a new date.

The reviewer-facing prompts/confirmations ("Please reply with the reason", "✓ Accepted")
are sent as plain session text (no template — the reviewer just messaged us, so the
24-hour customer-service window is open).
────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
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
# Promote approval gate (NPD dev job card): Approve / Reject quick replies.
TPL_PROMOTE = os.environ.get("WHATSAPP_TPL_NPD_PROMOTE", "npd_promote_approval")
_PROMOTE_GATE_LABEL = {"INV_MGR": "Inventory manager", "REQUESTOR_BH": "Business head"}
# Requisition-stage business-head approval (086): the SAME Approve / Reject quick replies,
# now asked at the start of the flow instead of on the finished job card. It needs its own
# Meta-approved template because the copy is about a REQUEST, not a recipe promotion —
# create `npd_bh_approval` (1 header var + 9 body vars, Approve/Reject quick replies, same
# shape as npd_promote_approval) in WhatsApp Manager. Until it is approved, point
# WHATSAPP_TPL_BH_SIGNOFF at npd_promote_approval to fall back to the promote copy; the
# send failure is logged loudly either way and the email card is unaffected.
TPL_BH_SIGNOFF = os.environ.get("WHATSAPP_TPL_BH_SIGNOFF", "npd_bh_approval")
# Dispatch-date reminder (D-1) fired by dispatch_reminder_service's daily scan. No
# buttons: purely informational, so there is no wa_review_message mapping to keep.
TPL_DISPATCH_DUE_TEAM = os.environ.get("WHATSAPP_TPL_DISPATCH_DUE_TEAM",
                                       "npd_dispatch_due_tomorrow_team")
# Same D-1 warning, addressed to the requisition's bound business head — the one person
# who can move the date or cancel. Shorter copy, so a SEPARATE template with four body
# vars instead of the team template's six.
TPL_DISPATCH_DUE_OWNER = os.environ.get("WHATSAPP_TPL_DISPATCH_DUE_OWNER",
                                        "npd_dispatch_due_tomorrow_owner")
# The daily chase once the date has PASSED. Two quick-reply buttons whose inbound payload
# is the button text — "Change expected date" / "Cancel request" — handled in handle_inbound.
# Registered as npd_dispatch_overdue_team but addressed to the business head: they are the
# only one who can move the date or cancel, and the buttons are theirs.
TPL_DISPATCH_OVERDUE = os.environ.get("WHATSAPP_TPL_DISPATCH_OVERDUE",
                                      "npd_dispatch_overdue_team")
# The same chase, informational, to the NPD team pool. Same five body vars as the business
# head's copy but NO buttons — the actions belong to the one person who can take them.
TPL_DISPATCH_OVERDUE_TEAM = os.environ.get("WHATSAPP_TPL_DISPATCH_OVERDUE_TEAM",
                                           "npd_dispatch_overdue_team_notifier")
# Verify token for the webhook GET handshake (set the same value in Meta).
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
# This WABA is shared with the standalone Visitor Management system, which sends its own
# approval templates with quick-reply payloads "approve_<id>" / "reject_<id>". Meta allows
# ONE webhook URL per app, so those taps land here. When this URL is set we forward them to
# the visitor webhook and skip NPD handling for them; left empty, this whole feature is a
# no-op and the webhook behaves exactly as before (opt-in for ops). See
# docs: 2026-06-18-whatsapp-visitor-approval-forwarding-design.md.
# Read at CALL time, not import time: app/main.py's lifespan hydrates .env-only values into
# os.environ, and that runs AFTER this module is imported — a module-level constant would
# have been frozen to "" and silently disabled forwarding on every .env-based deploy.
def visitor_forward_url() -> str:
    return os.environ.get("VISITOR_APPROVAL_FORWARD_URL", "").strip()


# Visitor quick-reply payloads are "<verb>_<numeric id>"; NPD/promote replies carry the
# button TEXT ("Approve"/"Reject") with no "_<digits>", so this never matches NPD traffic.
_VISITOR_APPROVAL_RE = re.compile(r"^(?:approve|reject)_\d+$", re.IGNORECASE)
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
    return _txt(req.get("request_id") or req.get("id"))


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


def _dispatch_due_team_params(req: dict) -> tuple[list[str], list[str]]:
    """(header, body) for npd_dispatch_due_tomorrow_team — SIX body vars. HEADER and BODY number
    INDEPENDENTLY — header {{1}} and body {{1}} are two separate parameters that happen
    to carry the same request no."""
    exp = req.get("expected_dispatch_date")
    no = _req_no(req)
    return [no], [
        no,                                        # {{1}} Request
        str(exp)[:10] if exp else "TBC",           # {{2}} Expected dispatch
        _txt(req.get("npd_target_name")),          # {{3}} Target article
        _num(req.get("quantity")),                 # {{4}} Quantity (template supplies "kg")
        _txt(req.get("customer_name")),            # {{5}} Customer
        _txt(req.get("warehouse")),                # {{6}} Warehouse
    ]


def _dispatch_due_owner_params(req: dict) -> tuple[list[str], list[str]]:
    """(header, body) for npd_dispatch_due_tomorrow_owner — FOUR body vars, not the team
    template's six. The business head is not making the batch, so the quantity and the
    warehouse are noise; they only need enough to recognise which request this is."""
    exp = req.get("expected_dispatch_date")
    no = _req_no(req)
    return [no], [
        no,                                        # {{1}} Request
        str(exp)[:10] if exp else "TBC",           # {{2}} Expected dispatch
        _txt(req.get("npd_target_name")),          # {{3}} Target article
        _txt(req.get("customer_name")),            # {{4}} Customer
    ]


def _dispatch_overdue_params(req: dict, *, days: int) -> tuple[list[str], list[str]]:
    """(header, body) for npd_dispatch_overdue_team — FIVE body vars.

    `days` comes from the scan, which computed it against the IST day; recomputing it here
    off the server clock would disagree with the reminder that decided to send.
    """
    exp = req.get("expected_dispatch_date")
    no = _req_no(req)
    return [no], [
        no,                                        # {{1}} Request
        str(exp)[:10] if exp else "TBC",           # {{2}} Expected dispatch
        _num(days),                                # {{3}} Days overdue
        _txt(req.get("npd_target_name")),          # {{4}} Target article
        _txt(req.get("customer_name")),            # {{5}} Customer
    ]


async def notify_dispatch_overdue(conn, req: dict, *, days: int, audience: str) -> bool:
    """The daily chase once the expected dispatch date has passed.

    `audience` is "npd" (the team pool — informational, so they know the request they are
    making has slipped) or "owner" (the bound business head — the copy that carries the
    Change-date / Cancel buttons). Two registered templates with the SAME five parameters;
    only the owner's has buttons, and sending that one to the pool would let any team
    member cancel a live request.

    Same True/False contract as the D-1 warning: False releases the day's claim so the next
    tick retries.
    """
    if audience == "npd":
        nums = await _resolve_recipients(conn)
        if not nums:
            logger.warning("No NPD reviewers with a phone (roles=%s) — skipping the overdue "
                           "notice for req %s; assign the role + a phone on auth_user, or "
                           "set WHATSAPP_NPD_REVIEW_NUMBERS", _REVIEW_ROLES, req.get("id"))
            return False
        header, body = _dispatch_overdue_params(req, days=days)
        sent_any = False
        for n in nums:
            resp = await _send_template(n, TPL_DISPATCH_OVERDUE_TEAM, body,
                                        header_params=header)
            if isinstance(resp, dict) and resp.get("error"):
                logger.warning("Overdue notice to %s failed for req %s: %s (template %s)",
                               n, req.get("id"), resp.get("error"), TPL_DISPATCH_OVERDUE_TEAM)
            else:
                sent_any = True
        # Deliberately NO wa_review_message row: this copy has no buttons, and a mapping
        # would create a way for a stray reply from the pool to resolve as the BH's answer.
        return sent_any

    # ── the business head's copy: the only one with the two actions on it ──
    # Its wamid IS mapped, because the quick-reply tap that answers it carries no request
    # number — only context.id. Without that row the tap is unattributable.
    bh_uid = req.get("business_head_user_id")
    if not bh_uid:
        logger.warning("Req %s has no business head bound — no overdue chase on WhatsApp "
                       "(requisitions raised before 086 carry no business_head_user_id)",
                       req.get("id"))
        return False
    phone = await _phone_for_user(conn, bh_uid)
    if not phone:
        logger.warning("Business head %s has no phone — skipping the overdue WhatsApp chase "
                       "for req %s (the email card, with the same two actions, still went "
                       "out)", bh_uid, req.get("id"))
        return False
    header, body = _dispatch_overdue_params(req, days=days)
    resp = await _send_template(phone, TPL_DISPATCH_OVERDUE, body, header_params=header)
    if isinstance(resp, dict) and resp.get("error"):
        logger.warning("Overdue chase to %s failed for req %s: %s (template %s — check it "
                       "is Approved in WhatsApp Manager)",
                       phone, req.get("id"), resp.get("error"), TPL_DISPATCH_OVERDUE)
        return False
    wamid = _wamid(resp)
    if wamid and req.get("id") is not None:
        await _store_review_message(conn, wamid, req["id"], "DISPATCH_OVERDUE", phone)
    return True


# ── the date the business head types back ────────────────────────────────────
# dd-mm-yyyy, with / and . allowed too: the prompt asks for dashes but a phone keyboard
# makes the other two just as likely, and day-month-year order is unambiguous either way.
_DDMMYYYY_RE = re.compile(r"^\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})\s*$")


def _parse_ddmmyyyy(s: Any, *, today: date) -> date | None:
    """Parse the new expected dispatch date, or None when it is not usable.

    Strict on purpose, because both loose readings are silent disasters: a two-digit year
    is ambiguous by a century, and a date at or before today would re-arm the overdue chase
    on the very next scan — the BH would answer the prompt and be chased again in the
    morning. Asking again costs one message; guessing costs the whole point of the button.
    """
    m = _DDMMYYYY_RE.match(str(s or ""))
    if not m:
        return None
    d, mo, y = (int(g) for g in m.groups())
    try:
        out = date(y, mo, d)
    except ValueError:                     # 31-02, month 13, day 0 …
        return None
    return out if out > today else None


# ── overdue chase: the two buttons and the replies that answer them ──────────
# The quick-reply payload is the button TEXT (Meta sends no separate payload for these),
# so these are matched case-insensitively against the registered labels.
_BTN_REDATE = "change expected date"
_BTN_CANCEL = "cancel request"

_ASK_REDATE_REASON = ("Changing the expected dispatch date for request {no}.\n"
                      "Please reply with the reason for the change.")
_ASK_CANCEL_REASON = ("Cancelling request {no}.\n"
                      "Please reply with the reason for the cancellation.")
_ASK_DATE = ("Reason noted. Now reply with the new expected dispatch date in "
             "dd-mm-yyyy format — for example 15-09-2026.")
_BAD_DATE = ("Sorry, I could not read that as a date. Please reply in dd-mm-yyyy format "
             "— for example 15-09-2026. It has to be a date after today.")


def _dispatch_today() -> date:
    """Today in IST — seam for the tests, and the same +05:30 the reminder scan uses."""
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


async def _apply_dispatch_redate(conn, req_id: int, *, new_date, user, reason=None):
    """Lazy wrapper around the requisition service (import cycle: requisition_service
    imports this module for its own notify calls). Also the seam the inbound tests patch."""
    from app.modules.sample.services import requisition_service
    return await requisition_service.set_expected_dispatch_date(
        conn, req_id, new_date=new_date, user=user, reason=reason)


async def _apply_dispatch_cancel(conn, req_id: int, *, reason: str, user):
    from app.modules.sample.services import requisition_service
    return await requisition_service.cancel_requisition(
        conn, req_id, reason=reason, user=user)


async def _release_overdue_rows(conn, req_id: int) -> None:
    """Forget the chase so the NEW date earns a fresh warning. Best-effort: the date move
    has already committed, and a failure here must not read back as a failed redate."""
    try:
        from app.modules.sample.services import dispatch_reminder_service as drs
        if await drs.has_log_table(conn):
            await drs.release_overdue(conn, req_id)
    except Exception:  # noqa: BLE001
        logger.exception("release_overdue failed for req %s", req_id)


async def _wamid_kind(conn, wamid: str) -> str | None:
    """What flow a quoted message belongs to, or None when we never sent it."""
    row = await conn.fetchrow(
        "SELECT kind, requisition_id FROM wa_review_message WHERE wamid = $1", wamid)
    return row["kind"] if row else None


# Latched False the first time wa_dispatch_pending turns out to be missing. Every inbound
# WhatsApp message reaches this handler, so without the latch an unmigrated deploy logs a
# failure per message and buries anything real in the same log.
_DISPATCH_PENDING_READY = True


async def _peek_dispatch_pending(conn, wa_phone: str) -> dict | None:
    """The armed prompt for this number, or None. 088 is hand-applied like every other
    samples/ migration, so a deploy can land before the table does — and this runs on the
    path of EVERY inbound message, where raising would break the whole webhook."""
    global _DISPATCH_PENDING_READY
    if not _DISPATCH_PENDING_READY:
        return None
    try:
        row = await conn.fetchrow(
            "SELECT requisition_id, action, stage, reason FROM wa_dispatch_pending "
            " WHERE wa_phone = $1", wa_phone)
    except Exception as e:  # noqa: BLE001
        _DISPATCH_PENDING_READY = False
        logger.warning("wa_dispatch_pending unavailable — the overdue chase's buttons are "
                       "inert until migration 088 is applied (%s)", e)
        return None
    return dict(row) if row else None


async def _arm_dispatch_pending(conn, wa_phone: str, req_id: int, action: str) -> None:
    await conn.execute(
        """INSERT INTO wa_dispatch_pending (wa_phone, requisition_id, action, stage)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (wa_phone) DO UPDATE
             SET requisition_id = EXCLUDED.requisition_id, action = EXCLUDED.action,
                 stage = EXCLUDED.stage, reason = NULL, created_at = NOW()""",
        wa_phone, req_id, action, "REASON")


async def _advance_dispatch_pending(conn, wa_phone: str, *, stage: str, reason) -> None:
    await conn.execute(
        "UPDATE wa_dispatch_pending SET stage = $1, reason = $2 WHERE wa_phone = $3",
        stage, reason, wa_phone)


async def _clear_dispatch_pending(conn, wa_phone: str) -> None:
    await conn.execute("DELETE FROM wa_dispatch_pending WHERE wa_phone = $1", wa_phone)


async def _dispatch_actor(conn, wa_phone: str, req_id: int):
    """(user, request_no) when this number belongs to the requisition's bound business
    head, else (None, request_no). The chase is ADDRESSED to the BH, but a number is not
    a signature — cancelling is terminal, so the acting user is checked against the row."""
    req = await conn.fetchrow(
        "SELECT id, request_id, status, business_head_user_id FROM sample_requisitions "
        " WHERE id = $1 AND deleted_at IS NULL", req_id)
    if req is None:
        return None, req_id
    u = await _resolve_user(conn, wa_phone)
    if u is None or u["user_id"] != req["business_head_user_id"]:
        return None, req["request_id"]
    return _WaUser(u["user_id"], u["role_name"]), req["request_id"]


async def handle_dispatch_action(conn, wa_phone: str, text: str,
                                 context_id: str | None) -> dict | None:
    """The overdue chase's buttons and their follow-up replies.

    Returns None when the message belongs to some other flow, so handle_inbound falls
    through unchanged — the same contract as handle_return_button_tap / the PO intimation
    handler. Never raises: a failure replies to the business head instead.
    """
    body = (text or "").strip()
    wa = _fmt_phone(wa_phone)
    if context_id:
        kind = await _wamid_kind(conn, context_id)
        if kind == "DISPATCH_OVERDUE":
            req_id = await conn.fetchval(
                "SELECT requisition_id FROM wa_review_message "
                " WHERE wamid = $1 AND kind = 'DISPATCH_OVERDUE'", context_id)
            return await _handle_dispatch_tap(conn, wa, req_id, body)
        if kind is not None:
            return None                  # another flow's card — leave the tap alone
        # An unmapped quote (our own text prompt, quoted back) falls through to the
        # pending below: otherwise the reply-quote UI would silently drop the answer.
    pend = await _peek_dispatch_pending(conn, wa)
    if pend is None:
        return None
    return await _handle_dispatch_reply(conn, wa, pend, body)


async def _handle_dispatch_tap(conn, wa: str, req_id, body: str) -> dict:
    low = body.lower()
    if low == _BTN_REDATE:
        action, awaiting, prompt = "REDATE", "redate_reason", _ASK_REDATE_REASON
    elif low == _BTN_CANCEL:
        action, awaiting, prompt = "CANCEL", "cancel_reason", _ASK_CANCEL_REASON
    else:
        # Free text quoting the card is a question, not a decision. Arming a cancellation
        # off it would let an idle reply start killing a live request.
        await _send_text(wa, "Tap *Change expected date* or *Cancel request* on the "
                             "reminder above.")
        return {"ok": False, "reason": "unparsed", "requisition_id": req_id}
    user, req_no = await _dispatch_actor(conn, wa, req_id)
    if user is None:
        await _send_text(wa, "Sorry, only this request's business head can action it.")
        return {"ok": False, "reason": "unauthorised", "requisition_id": req_id}
    await _arm_dispatch_pending(conn, wa, req_id, action)
    await _send_text(wa, prompt.format(no=req_no))
    return {"ok": True, "awaiting": awaiting, "requisition_id": req_id}


async def _handle_dispatch_reply(conn, wa: str, pend: dict, body: str) -> dict:
    req_id = pend["requisition_id"]
    user, req_no = await _dispatch_actor(conn, wa, req_id)
    if user is None:
        await _clear_dispatch_pending(conn, wa)
        await _send_text(wa, "Sorry, only this request's business head can action it.")
        return {"ok": False, "reason": "unauthorised", "requisition_id": req_id}

    if pend["stage"] == "REASON":
        if not body:
            await _send_text(wa, "Please reply with the reason — it is recorded against "
                                 "the request.")
            return {"ok": False, "reason": "reason_required", "requisition_id": req_id}
        if pend["action"] == "REDATE":
            await _advance_dispatch_pending(conn, wa, stage="DATE", reason=body)
            await _send_text(wa, _ASK_DATE)
            return {"ok": True, "awaiting": "redate_date", "requisition_id": req_id}
        try:
            await _apply_dispatch_cancel(conn, req_id, reason=body, user=user)
        except Exception as e:  # noqa: BLE001 — reply with the reason, never 500 the webhook
            return await _dispatch_failed(conn, wa, req_id, e, "cancel")
        await _clear_dispatch_pending(conn, wa)
        await _send_text(wa, f"✓ Request {req_no} has been cancelled. Reason recorded.")
        return {"ok": True, "action": "CANCELLED", "requisition_id": req_id}

    new_date = _parse_ddmmyyyy(body, today=_dispatch_today())
    if new_date is None:
        # Deliberately does NOT clear the pending: losing the reason would make the BH
        # type it again over a typo in the date.
        await _send_text(wa, _BAD_DATE)
        return {"ok": False, "reason": "bad_date", "requisition_id": req_id}
    try:
        await _apply_dispatch_redate(conn, req_id, new_date=new_date, user=user,
                                     reason=pend.get("reason"))
    except Exception as e:  # noqa: BLE001
        return await _dispatch_failed(conn, wa, req_id, e, "redate")
    await _release_overdue_rows(conn, req_id)
    await _clear_dispatch_pending(conn, wa)
    await _send_text(wa, f"✓ Expected dispatch for request {req_no} moved to "
                         f"{new_date.strftime('%d-%m-%Y')}. Reason recorded.")
    return {"ok": True, "action": "REDATED", "requisition_id": req_id,
            "expected_dispatch_date": new_date.isoformat()}


async def _dispatch_failed(conn, wa: str, req_id, exc: Exception, what: str) -> dict:
    """One place to turn a service refusal into a reply. The pending row is cleared so the
    business head is not stuck answering a prompt that can never succeed (a request already
    cancelled elsewhere, say)."""
    detail = getattr(exc, "detail", None)
    msg = (detail or {}).get("message") if isinstance(detail, dict) else None
    logger.warning("Dispatch %s failed for req %s: %s", what, req_id, exc)
    await _clear_dispatch_pending(conn, wa)
    await _send_text(wa, msg or "Sorry, that could not be applied. Please use the portal.")
    return {"ok": False, "reason": what + "_failed", "requisition_id": req_id}


async def _resolve_due_audience(conn, req: dict, audience: str):
    """(numbers, template, params) for one audience, or None when nobody is reachable.

    Mirrors sample_mail_service.notify_dispatch_due_tomorrow's split: "npd" is the team
    pool (everyone who has to make it), "owner" is the requisition's bound business head
    — the single person who can move the date or cancel it.
    """
    if audience == "owner":
        bh_uid = req.get("business_head_user_id")
        if not bh_uid:
            logger.warning("Req %s has no business head bound — no owner dispatch reminder "
                           "on WhatsApp (requisitions raised before 086 carry no "
                           "business_head_user_id)", req.get("id"))
            return None
        phone = await _phone_for_user(conn, bh_uid)
        if not phone:
            logger.warning("Business head %s has no phone — skipping the owner dispatch "
                           "reminder for req %s (the email card still went out)",
                           bh_uid, req.get("id"))
            return None
        return [phone], TPL_DISPATCH_DUE_OWNER, _dispatch_due_owner_params(req)
    nums = await _resolve_recipients(conn)
    if not nums:
        # WARNING (not INFO): a silent "0 delivered" almost always traces here —
        # no user in these roles has a phone on file.
        logger.warning("No NPD reviewers with a phone (roles=%s) — skipping the dispatch "
                       "reminder for req %s; assign the role + a phone on auth_user, or "
                       "set WHATSAPP_NPD_REVIEW_NUMBERS", _REVIEW_ROLES, req.get("id"))
        return None
    return nums, TPL_DISPATCH_DUE_TEAM, _dispatch_due_team_params(req)


async def notify_dispatch_due_tomorrow(conn, req: dict, *, audience: str) -> bool:
    """Warn that a sample requisition is due for dispatch tomorrow. `audience` is "npd"
    (the team pool) or "owner" (the bound business head) — two audiences, two registered
    Meta templates, two different parameter counts.

    Returns True only if at least one number was actually messaged. The caller's
    send-once guard releases the day's claim on False, so an unconfigured recipient list
    or a Meta rejection is retried on the next tick instead of silently consuming the day.
    A disabled/unconfigured WhatsApp counts as sent: _post's dev fallback carries no
    error, and retrying a channel that is switched off every hour would be noise.
    """
    resolved = await _resolve_due_audience(conn, req, audience)
    if resolved is None:
        return False
    nums, template, (header, body) = resolved
    sent_any = False
    for n in nums:
        resp = await _send_template(n, template, body, header_params=header)
        if isinstance(resp, dict) and resp.get("error"):
            logger.warning("Dispatch reminder (%s) to %s failed: %s (template %s — check "
                           "it is Approved in WhatsApp Manager)",
                           audience, n, resp.get("error"), template)
        else:
            sent_any = True
    return sent_any


async def _requestor_phone(conn, req: dict) -> str | None:
    uid = req.get("requestor_user_id")
    if not uid:
        return None
    return await conn.fetchval("SELECT phone FROM auth_user WHERE user_id = $1", uid)


async def notify_requestor(conn, req: dict, *, action: str, reason: str | None = None) -> None:
    """Tell the requestor the outcome. action in {'APPROVE','ACCEPT','HOLD'}."""
    phone = await _requestor_phone(conn, req)
    if not phone:
        logger.info("Requestor has no phone — skipping outcome notify for req %s", req.get("id"))
        return
    # Layout: HEADER text var {{1}} = request no; BODY {{1}} = target article,
    # {{2}} = expected dispatch (accepted) / hold reason (on-hold).
    req_no = _txt(req.get("request_id") or req.get("id"))
    target = _txt(req.get("npd_target_name"))
    if action in ("APPROVE", "ACCEPT"):
        # At accept time the trial hasn't closed, so the confirmed dispatch date is
        # not known yet — show the BD team's EXPECTED dispatch date instead.
        disp = req.get("expected_dispatch_date")
        tpl = TPL_ACCEPTED
        resp = await _send_template(phone, tpl, [target, str(disp)[:10] if disp else "TBC"],
                                    header_params=[req_no])
    elif action == "HOLD":
        tpl = TPL_HOLD
        resp = await _send_template(phone, tpl, [target, _txt(reason or "—")],
                                    header_params=[req_no])
    else:
        return
    # Surface a missing/unapproved outcome template instead of swallowing it — these
    # two templates (npd_request_accepted / npd_request_on_hold) must exist in
    # WhatsApp Manager or the requestor silently gets nothing.
    if isinstance(resp, dict) and resp.get("error"):
        logger.warning("Requestor %s notify failed for req %s: %s — is template '%s' "
                       "registered + Approved (lang %s)?",
                       action, req.get("id"), resp.get("error"), tpl, TEMPLATE_LANG)


# ── requisition-stage business-head approval (086) ───────────────────────────
def _bh_signoff_params(req: dict) -> tuple[list[str], list[str]]:
    """(header, body) for the BH approval template. HEADER {{1}} = request no; BODY
    {{1}} = who raised it, {{2}} = request no, {{3}} = target article, {{4}} = qty,
    {{5}} = company, {{6}} = customer, {{7}} = return type, {{8}} = paid, {{9}} = amount.
    Deliberately the same 1+9 shape as _promote_params, so the promote template can stand
    in as a fallback (see TPL_BH_SIGNOFF) without a parameter-count mismatch."""
    number = _req_no(req)
    qty = req.get("quantity")
    qty_s = f"{_num(qty)} kg" if qty is not None else "—"
    rtype = ("Returnable" if req.get("returnable")
             else "Non-returnable" if req.get("non_returnable") else "—")
    amt = req.get("amount")
    amount = "—"
    if req.get("paid") and amt is not None:
        try:                                  # NUMERIC(12,2) → Decimal; defensive anyway
            amount = f"{float(amt):,.2f}"
        except (TypeError, ValueError):
            amount = _txt(amt)
    body = [_txt(req.get("sales_poc_name") or req.get("sales_poc_email") or "Sales"),
            number, _txt(req.get("npd_target_name")), qty_s,
            _txt(req.get("company_name")), _txt(req.get("customer_name")),
            rtype, ("Yes" if req.get("paid") else "No"), amount]
    return [number], body


async def notify_bh_signoff(conn, req: dict) -> None:
    """Message the bound business head that a request raised on their behalf needs their
    Approve / Reject. One recipient — the gate binds to that one person. Best-effort."""
    bh_uid = req.get("business_head_user_id")
    phone = await _phone_for_user(conn, bh_uid)
    if not phone:
        logger.warning("Business head %s has no phone — skipping BH approval WhatsApp for "
                       "req %s (the email card still went out)", bh_uid, req.get("id"))
        return
    header, body = _bh_signoff_params(req)
    resp = await _send_template(phone, TPL_BH_SIGNOFF, body, header_params=header)
    wamid = _wamid(resp)
    if wamid and req.get("id") is not None:
        await _store_review_message(conn, wamid, req["id"], "BH_SIGNOFF", phone)
    elif isinstance(resp, dict) and resp.get("error"):
        logger.warning("BH approval notify to %s failed for req %s: %s — is template '%s' "
                       "registered + Approved (lang %s)?",
                       phone, req.get("id"), resp.get("error"), TPL_BH_SIGNOFF, TEMPLATE_LANG)


# ── promote dual-approval gate (job-card approval) ───────────────────────────
async def _phones_for_role(conn, role: str) -> list[str]:
    """Active auth_user phones for a role (E.164, no '+'), empty if none."""
    rows = await conn.fetch(
        """SELECT u.phone FROM auth_user u JOIN auth_role r ON u.role_id = r.role_id
            WHERE r.role_name = $1 AND COALESCE(u.is_active, TRUE)
              AND u.phone IS NOT NULL AND btrim(u.phone) <> ''""", role)
    return [_fmt_phone(r["phone"]) for r in rows]


async def _phone_for_user(conn, uid) -> str | None:
    if not uid:
        return None
    p = await conn.fetchval("SELECT phone FROM auth_user WHERE user_id = $1", uid)
    return _fmt_phone(p) if p else None


def _promote_params(jc: dict, gate_label: str) -> tuple[list[str], list[str]]:
    """(header, body) for npd_promote_approval. HEADER {{1}} = dev JC id; BODY
    {{1}} = gate label, {{2}} = dev JC id, {{3}} = target FG, {{4}} = qty,
    {{5}} = company, {{6}} = customer, {{7}} = return type, {{8}} = paid, {{9}} = amount."""
    number = _txt(jc.get("id"))
    tq, uom = jc.get("target_qty"), (jc.get("uom") or "kg")
    qty = f"{_num(tq)} {_txt(uom)}" if tq is not None else "—"
    rtype = ("Returnable" if jc.get("returnable")
             else "Non-returnable" if jc.get("non_returnable") else "—")
    amt = jc.get("amount")
    amount = "—"
    if jc.get("paid") and amt is not None:
        try:                                  # NUMERIC(12,2) → Decimal; defensive anyway
            amount = f"{float(amt):,.2f}"
        except (TypeError, ValueError):
            amount = _txt(amt)
    body = [gate_label, number, _txt(jc.get("fg_sku_name") or jc.get("title")), qty,
            _txt(jc.get("company_name")), _txt(jc.get("customer_name")),
            rtype, ("Yes" if jc.get("paid") else "No"), amount]
    return [number], body


async def _store_promote_message(conn, wamid: str, dev_jc_id: int, approver_kind: str, wa_phone: str) -> None:
    """Remember which (dev JC, gate) a sent promote template was about, so a later
    Approve/Reject button tap (context.id = this wamid) resolves back. Best-effort."""
    try:
        await conn.execute(
            """INSERT INTO wa_promote_message (wamid, dev_jc_id, approver_kind, wa_phone)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (wamid) DO UPDATE
                 SET dev_jc_id = EXCLUDED.dev_jc_id, approver_kind = EXCLUDED.approver_kind,
                     wa_phone = EXCLUDED.wa_phone""",
            wamid, dev_jc_id, approver_kind, wa_phone)
    except Exception:  # noqa: BLE001 — mapping is a convenience; the flow still works without it
        logger.exception("Failed to store wa_promote_message for wamid %s", wamid)


async def _upsert_promote_reminder(conn, dev_jc_id, approver_kind: str, *, reset: bool) -> None:
    """Track resend pacing for one promote gate (079). reset=True on the initial send
    (resend_count → 0, last_sent_at → now); reset=False after a reminder resend (bump
    the count, restamp). Best-effort — a missing table (079 not applied) or any error
    must never break the send/loop."""
    try:
        if reset:
            await conn.execute(
                """INSERT INTO wa_promote_reminder (dev_jc_id, approver_kind, last_sent_at, resend_count)
                   VALUES ($1, $2, NOW(), 0)
                   ON CONFLICT (dev_jc_id, approver_kind)
                     DO UPDATE SET last_sent_at = NOW(), resend_count = 0""",
                dev_jc_id, approver_kind)
        else:
            await conn.execute(
                """UPDATE wa_promote_reminder SET last_sent_at = NOW(), resend_count = resend_count + 1
                    WHERE dev_jc_id = $1 AND approver_kind = $2""",
                dev_jc_id, approver_kind)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to upsert wa_promote_reminder for %s/%s", dev_jc_id, approver_kind)


async def notify_promote_review(conn, *, dev_jc_id, requestor_uid=None) -> None:
    """Message the promote approvers with Approve / Reject buttons: every
    inventory_manager (INV_MGR gate) + the source requisition's requestor BH
    (REQUESTOR_BH gate; skipped when sourceless / no phone). Best-effort, never raises."""
    jc = await conn.fetchrow("SELECT * FROM npd_dev_job_cards WHERE id = $1", dev_jc_id)
    if jc is None:
        return
    jc = dict(jc)
    targets = [(p, "INV_MGR") for p in await _phones_for_role(conn, "inventory_manager")]
    if requestor_uid:
        bh = await _phone_for_user(conn, requestor_uid)
        if bh:
            targets.append((bh, "REQUESTOR_BH"))
    # Arm the resend tracker for every PENDING gate on THIS request — independent of
    # whether a phone resolved right now. A fresh request resets the counter, and the
    # reminder loop then recovers (starts delivering) once a phone is added to a gate.
    for g in await conn.fetch(
            """SELECT DISTINCT a.approver_kind FROM npd_dev_promote_approval a
                 JOIN npd_dev_promote_request pr ON a.promote_request_id = pr.id
                WHERE pr.dev_jc_id = $1 AND pr.status = 'PENDING' AND a.status = 'PENDING'""",
            dev_jc_id):
        await _upsert_promote_reminder(conn, dev_jc_id, g["approver_kind"], reset=True)

    if not targets:
        logger.warning("No promote approvers with a phone for dev JC %s — skipping WhatsApp "
                       "(assign a phone to the inventory_manager / requestor on auth_user)", dev_jc_id)
        return
    for phone, kind in targets:
        try:
            header, body = _promote_params(jc, _PROMOTE_GATE_LABEL[kind])
            resp = await _send_template(phone, TPL_PROMOTE, body, header_params=header)
        except Exception:  # noqa: BLE001 — one bad recipient must not abort the rest
            logger.exception("Promote notify build/send failed for %s (dev JC %s)", phone, dev_jc_id)
            continue
        wamid = _wamid(resp)
        if wamid:
            await _store_promote_message(conn, wamid, dev_jc_id, kind, phone)
        elif isinstance(resp, dict) and resp.get("error"):
            logger.warning("Promote notify to %s failed: %s (template %s — Approved in "
                           "WhatsApp Manager?)", phone, resp.get("error"), TPL_PROMOTE)


async def resend_due_promotes(conn, *, ttl_hours: float, max_resends: int) -> int:
    """Re-send promote-approval templates for gates still PENDING past the timeout.
    Driven by promote_reminder_loop. Recipients are re-resolved at send time
    (INV_MGR → every inventory_manager phone; REQUESTOR_BH → the bound requestor),
    so a phone added/changed since the first send is picked up. Returns gates re-sent."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    due = await conn.fetch(
        """SELECT r.dev_jc_id, r.approver_kind, a.approver_user_id
             FROM wa_promote_reminder r
             JOIN npd_dev_promote_request pr
               ON pr.dev_jc_id = r.dev_jc_id AND pr.status = 'PENDING'
             JOIN npd_dev_promote_approval a
               ON a.promote_request_id = pr.id
              AND a.approver_kind = r.approver_kind
              AND a.status = 'PENDING'
            WHERE r.resend_count < $1 AND r.last_sent_at < $2""",
        max_resends, cutoff)
    sent = 0
    for row in due:
        dev_jc_id, kind, approver_uid = row["dev_jc_id"], row["approver_kind"], row["approver_user_id"]
        jc = await conn.fetchrow("SELECT * FROM npd_dev_job_cards WHERE id = $1", dev_jc_id)
        if jc is None:
            continue
        jc = dict(jc)
        if kind == "INV_MGR":
            phones = await _phones_for_role(conn, "inventory_manager")
        else:  # REQUESTOR_BH
            p = await _phone_for_user(conn, approver_uid)
            phones = [p] if p else []
        any_ok = False
        for phone in phones:
            try:
                header, body = _promote_params(jc, _PROMOTE_GATE_LABEL[kind])
                resp = await _send_template(phone, TPL_PROMOTE, body, header_params=header)
            except Exception:  # noqa: BLE001 — one bad recipient must not abort the rest
                logger.exception("Promote resend build/send failed for %s (dev JC %s)", phone, dev_jc_id)
                continue
            if _wamid(resp):
                await _store_promote_message(conn, _wamid(resp), dev_jc_id, kind, phone)
                any_ok = True
        if any_ok:
            await _upsert_promote_reminder(conn, dev_jc_id, kind, reset=False)  # bump count + restamp
            sent += 1
        else:
            # Nothing sent (no phone / all failed) — restamp so we don't re-select every
            # tick, but don't burn a resend on a non-send (lets it recover once a phone exists).
            await conn.execute(
                "UPDATE wa_promote_reminder SET last_sent_at = NOW() "
                "WHERE dev_jc_id = $1 AND approver_kind = $2", dev_jc_id, kind)
    return sent


async def promote_reminder_loop(pool) -> None:
    """In-process background loop: periodically re-send promote-approval templates for
    gates still PENDING past WHATSAPP_PROMOTE_RESEND_HOURS (default 4), up to
    WHATSAPP_PROMOTE_RESEND_MAX times (default 2). Mirrors the webhook dispatcher_loop.
    NOTE: only ticks when the app runs as a persistent server (uvicorn/ECS) — on the
    Lambda/Mangum path it does not run, exactly like dispatcher_loop. Assumes a SINGLE
    persistent instance (like dispatcher_loop); running several would double-send, since
    the due-gate SELECT takes no row lock — add FOR UPDATE SKIP LOCKED if that changes."""
    tick_s = max(60, int(os.environ.get("WHATSAPP_PROMOTE_RESEND_TICK_MIN", "15")) * 60)
    ttl_h = float(os.environ.get("WHATSAPP_PROMOTE_RESEND_HOURS", "4"))
    max_n = int(os.environ.get("WHATSAPP_PROMOTE_RESEND_MAX", "2"))
    logger.info("Promote reminder loop started (tick=%ds, ttl=%.1fh, max=%d)", tick_s, ttl_h, max_n)
    try:
        while True:
            await asyncio.sleep(tick_s)
            try:
                if not _wa_enabled() or max_n <= 0:
                    continue
                async with pool.acquire() as conn:
                    n = await resend_due_promotes(conn, ttl_hours=ttl_h, max_resends=max_n)
                if n:
                    logger.info("Promote reminder: re-sent %d pending gate(s)", n)
            except Exception:  # noqa: BLE001 — a bad tick must never kill the loop
                logger.exception("Promote reminder loop tick failed")
    except asyncio.CancelledError:
        logger.info("Promote reminder loop stopped")
        raise


async def _promote_for_wamid(conn, wamid: str | None) -> dict | None:
    """(dev_jc_id, approver_kind) for a tapped promote button's quoted message id."""
    if not wamid:
        return None
    row = await conn.fetchrow(
        "SELECT dev_jc_id, approver_kind FROM wa_promote_message WHERE wamid = $1", wamid)
    return dict(row) if row else None


async def _set_pending_promote_reject(conn, wa_phone: str, dev_jc_id: int, approver_kind: str) -> None:
    await conn.execute(
        """INSERT INTO wa_promote_pending (wa_phone, dev_jc_id, approver_kind)
           VALUES ($1, $2, $3)
           ON CONFLICT (wa_phone) DO UPDATE
             SET dev_jc_id = EXCLUDED.dev_jc_id, approver_kind = EXCLUDED.approver_kind,
                 created_at = NOW()""",
        wa_phone, dev_jc_id, approver_kind)


async def _pop_promote_pending(conn, wa_phone: str) -> dict | None:
    row = await conn.fetchrow(
        "DELETE FROM wa_promote_pending WHERE wa_phone = $1 RETURNING dev_jc_id, approver_kind", wa_phone)
    return dict(row) if row else None


async def _apply_promote(conn, user, wa: str, dev_jc_id: int, approver_kind: str,
                         action: str, reason, pas) -> dict:
    """Run act_promote_approval and reply with the outcome. Translates the service's
    HTTPException into a friendly WhatsApp message (idempotent: a stale re-tap just
    reports 'already actioned')."""
    from fastapi import HTTPException
    try:
        result = await pas.act_promote_approval(
            conn, dev_jc_id, action=action, user=user, remarks=reason, approver_kind=approver_kind)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        await _send_text(wa, f"Couldn't {action.lower()} — {detail.get('message', 'it may already have been actioned')}.")
        return {"ok": False, "reason": detail.get("error", "error")}
    status = (result or {}).get("status")
    if action == "REJECT":
        msg = "✗ Rejected — the promote was voided." + (f" Reason: {reason}" if reason else "")
    elif status == "PROMOTED":
        msg = "✓ Approved — both gates cleared; the recipe is now a live BOM."
    else:
        msg = "✓ Approved — your gate is cleared. Waiting on the other approver."
    await _send_text(wa, msg)
    return {"ok": True, "dev_jc_id": dev_jc_id, "approver_kind": approver_kind, "action": action, "status": status}


# ── promote resolution when the quoted-message id doesn't map ───────────────
# Promote quick-reply buttons are "Approve"/"Reject" (NPD review uses "Accept"/"Hold"),
# so these verbs route to the promote gate. A tap that DOESN'T carry a usable context.id
# (Meta omits it, or quotes a message we never stored) would otherwise dead-end in the
# NPD-review path ("not recognised as an NPD reviewer"); these helpers let us instead
# resolve the gate from the sender's own PENDING promote(s) or a typed job-card number.
_PROMOTE_APPROVE_WORDS = {"APPROVE", "APPROVED"}
_PROMOTE_REJECT_WORDS = {"REJECT", "REJECTED", "DECLINE", "DECLINED"}
_PROMOTE_VERBS = _PROMOTE_APPROVE_WORDS | _PROMOTE_REJECT_WORDS
# Bare yes/no synonyms — accepted ONLY as a whole-message reply (a single word), so a
# multi-word NPD hold reason like "ok, hold this" is never mistaken for an approval.
# Like the verbs, they only act when the sender has exactly one pending promote gate.
_PROMOTE_APPROVE_SYNONYMS = {"YES", "Y", "OK", "OKAY", "CONFIRM", "CONFIRMED"}
_PROMOTE_REJECT_SYNONYMS = {"NO", "N"}


async def _resolve_jc_ref(conn, ref: str | None):
    """Resolve a dev-JC reference to its 8-digit id. The 8-digit id is the only
    identifier, so the reference must be that id. None if unresolved."""
    ref = (ref or "").strip()
    if not ref or not ref.isdigit():
        return None
    return await conn.fetchval("SELECT id FROM npd_dev_job_cards WHERE id = $1", int(ref))


async def _eligible_pending_gates(conn, user) -> list[dict]:
    """PENDING promote gates this user may act on — mirrors act_promote_approval's
    eligibility so routing matches what the action will actually allow: admin → any;
    inventory_manager → INV_MGR gates; the bound requestor → their REQUESTOR_BH gate.
    Returns [{dev_jc_id, approver_kind}], oldest first."""
    rows = await conn.fetch(
        """SELECT pr.dev_jc_id, a.approver_kind
             FROM npd_dev_promote_approval a
             JOIN npd_dev_promote_request pr ON a.promote_request_id = pr.id
            WHERE pr.status = 'PENDING' AND a.status = 'PENDING'
              AND ( $2::boolean
                 OR (a.approver_kind = 'INV_MGR' AND $3::boolean)
                 OR (a.approver_kind = 'REQUESTOR_BH' AND a.approver_user_id = $1) )
            ORDER BY pr.created_at""",
        user.user_id, bool(getattr(user, "is_admin", False)),
        getattr(user, "role_name", "") == "inventory_manager")
    return [dict(r) for r in rows]


async def _resolve_promote_target(conn, user, *, jc_ref: str | None) -> dict:
    """Pick the promote gate a sender's Approve/Reject should act on when there is no
    usable quoted-message id. status ∈ {ok, none, ambiguous, jc_not_found, jc_not_eligible}."""
    gates = await _eligible_pending_gates(conn, user)
    if jc_ref:
        dev_jc_id = await _resolve_jc_ref(conn, jc_ref)
        if dev_jc_id is None:
            return {"status": "jc_not_found", "ref": jc_ref}
        forjc = [g for g in gates if g["dev_jc_id"] == dev_jc_id]
        if not forjc:
            return {"status": "jc_not_eligible", "dev_jc_id": dev_jc_id}
        if len(forjc) > 1:                       # sender holds BOTH gates on one JC (rare)
            return {"status": "ambiguous", "options": forjc}
        return {"status": "ok", "dev_jc_id": dev_jc_id, "approver_kind": forjc[0]["approver_kind"]}
    if not gates:
        return {"status": "none"}
    if len(gates) == 1:
        return {"status": "ok", "dev_jc_id": gates[0]["dev_jc_id"],
                "approver_kind": gates[0]["approver_kind"]}
    return {"status": "ambiguous", "options": gates}


# ── inbound: pending-reason state ───────────────────────────────────────────
async def _set_pending_hold(conn, wa_phone: str, req_id: int) -> None:
    await conn.execute(
        """INSERT INTO wa_pending_action (wa_phone, requisition_id, action)
           VALUES ($1, $2, 'HOLD')
           ON CONFLICT (wa_phone) DO UPDATE
             SET requisition_id = EXCLUDED.requisition_id, action = 'HOLD', created_at = NOW()""",
        wa_phone, req_id)


async def _pop_pending(conn, wa_phone: str) -> dict | None:
    """Pop an armed NPD HOLD prompt. Scoped to action='HOLD' so a BH's armed reject
    reason (086) can never be swallowed by the NPD review flow, and vice versa."""
    row = await conn.fetchrow(
        "DELETE FROM wa_pending_action WHERE wa_phone = $1 AND action = 'HOLD' "
        "RETURNING requisition_id, action", wa_phone)
    return dict(row) if row else None


async def _set_pending_bh_reject(conn, wa_phone: str, req_id: int) -> None:
    """Arm 'the next plain reply from this number is the BH's reject reason' — the same
    arm-and-wait the NPD hold prompt uses, since a rejection must carry a reason."""
    await conn.execute(
        """INSERT INTO wa_pending_action (wa_phone, requisition_id, action)
           VALUES ($1, $2, 'BH_REJECT')
           ON CONFLICT (wa_phone) DO UPDATE
             SET requisition_id = EXCLUDED.requisition_id, action = 'BH_REJECT', created_at = NOW()""",
        wa_phone, req_id)


async def _pop_pending_bh_reject(conn, wa_phone: str) -> int | None:
    return await conn.fetchval(
        "DELETE FROM wa_pending_action WHERE wa_phone = $1 AND action = 'BH_REJECT' "
        "RETURNING requisition_id", wa_phone)


async def _bh_signoff_req_for_wamid(conn, wamid: str | None) -> int | None:
    """The requisition a tapped BH Approve/Reject refers to, via the quoted message id."""
    if not wamid:
        return None
    return await conn.fetchval(
        "SELECT requisition_id FROM wa_review_message WHERE wamid = $1 AND kind = 'BH_SIGNOFF'",
        wamid)


async def _apply_bh_signoff(conn, user, wa: str, req_id: int, action: str,
                            reason: str | None) -> dict:
    """Run act_bh_signoff and reply with the outcome. Translates the service's
    HTTPException into a friendly reply (a stale re-tap just reports 'already actioned')."""
    from fastapi import HTTPException
    from app.modules.sample.services import approval_service
    try:
        await approval_service.act_bh_signoff(
            conn, req_id, action=action, user=user, remarks=reason)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        await _send_text(wa, "Couldn't record that — "
                             f"{detail.get('message', 'it may already have been actioned')}.")
        return {"ok": False, "reason": detail.get("error", "error")}
    if action == "REJECTED":
        msg = "✗ Rejected — the request will not go to NPD." + (f" Reason: {reason}" if reason else "")
    else:
        msg = "✓ Approved — the request has been sent to the NPD team."
    await _send_text(wa, msg)
    return {"ok": True, "requisition_id": req_id, "action": action}


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


async def _resolve_user(conn, wa_phone: str) -> dict | None:
    """Map an inbound number to ANY active auth_user (no role gate) — the promote
    flow uses this and lets act_promote_approval enforce per-gate authorization."""
    row = await conn.fetchrow(
        """SELECT u.user_id, COALESCE(r.role_name, '') AS role_name
             FROM auth_user u
             LEFT JOIN auth_role r ON u.role_id = r.role_id
            WHERE u.phone = ANY($1::text[])
            ORDER BY u.user_id LIMIT 1""",
        lookup_keys(wa_phone))
    return dict(row) if row else None


async def _find_req(conn, ref: str) -> dict | None:
    """Resolve a request by its 8-digit request_id."""
    return await conn.fetchrow(
        """SELECT id, status, sample_type, request_id
             FROM sample_requisitions
            WHERE deleted_at IS NULL AND request_id::text = $1
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
        """SELECT id, status, sample_type, request_id
             FROM sample_requisitions WHERE id = $1 AND deleted_at IS NULL""", rid)


class _WaUser:
    """Minimal user object for act_npd_review (uses .user_id / .role_name)."""
    def __init__(self, user_id: int, role_name: str):
        self.user_id = user_id
        self.role_name = role_name
        self.is_admin = role_name == "admin"
        self.full_name = "WhatsApp"


async def handle_inbound(conn, *, from_phone: str, text: str, context_id: str | None = None,
                         raw: dict | None = None) -> dict:
    """Parse one inbound message from an NPD reviewer and act. Returns a small
    result dict (for the webhook log / tests). Never raises — replies guide the
    reviewer instead. The DB writes for act_npd_review run in its own txn.

    `context_id` is the wamid of the message the reply quotes — present when the
    reviewer taps an Accept/Hold quick-reply button — and is how a button tap (which
    carries no request number) is mapped back to its request.

    `raw` is the original Meta message object, used only as the last-resort visitor
    forward below."""
    from app.modules.sample.services import approval_service  # lazy: avoid import cycle
    from app.modules.sample.services import promote_approval_service as pas

    wa = _fmt_phone(from_phone)
    body = (text or "").strip()
    first = body.split(maxsplit=1)[0].upper() if body else ""

    # ── CUSTOMER-RETURNS head approval (context.id → wa_return_message) — resolved FIRST,
    #    since the BU-Head approver is neither an npd_team reviewer nor a promote approver.
    #    Returns None when the tap isn't a customer-return message, so the flows below run. ──
    if context_id:
        from app.modules.customer_returns.services import wa_notify as _cr_wa  # lazy: avoid cycle
        cr_res = await _cr_wa.handle_return_button_tap(conn, wa, body, context_id)
        if cr_res is not None:
            return cr_res

    # ── PURCHASE no-PO intimation ("PO Created & Uploaded" / "Don't Accept the Material")
    #    — the tapper is a purchase_manager, not an NPD reviewer or promote approver.
    #    Handles BOTH the tap (context.id → wa_po_intimation_message) and the plain-text
    #    PO number that answers it (no context.id, keyed on an armed capture for this
    #    phone), so it must run for context-less messages too. Returns None when the
    #    message isn't ours, leaving every flow below unchanged. ──
    from app.modules.purchase.services import po_intimation as _po_wa  # lazy: avoid cycle
    po_res = await _po_wa.handle_po_intimation_tap(conn, wa, body, context_id)
    if po_res is not None:
        return po_res

    # ── OVERDUE-DISPATCH chase (088): "Change expected date" / "Cancel request".
    #    MUST run before the NPD-review flow below: _req_for_wamid resolves a quoted wamid
    #    WITHOUT filtering on kind, so a dispatch tap reaching it first would be answered
    #    as an Accept/Hold on the request. Returns None for anything that isn't ours. ──
    disp_res = await handle_dispatch_action(conn, wa, body, context_id)
    if disp_res is not None:
        return disp_res

    # ── BUSINESS-HEAD approval on the REQUEST (086) — resolved before the promote and
    #    NPD-review flows, since the BH is neither an npd_team reviewer nor a promote
    #    approver, and a bare "Approve" tap must not be mistaken for either. ──
    # (a) Approve / Reject quoting the BH approval card (context.id → wa_review_message).
    bh_req_id = await _bh_signoff_req_for_wamid(conn, context_id) if context_id else None
    if bh_req_id:
        u = await _resolve_user(conn, wa)
        if u is None:
            await _send_text(wa, "Sorry, this number isn't recognised.")
            return {"ok": False, "reason": "unauthorised"}
        user = _WaUser(u["user_id"], u["role_name"])
        if first in _PROMOTE_REJECT_WORDS or body.strip().upper() in _PROMOTE_REJECT_SYNONYMS:
            await _set_pending_bh_reject(conn, wa, bh_req_id)
            await _send_text(wa, "Rejecting the request — please reply with the reason.")
            return {"ok": True, "awaiting": "bh_reject_reason", "requisition_id": bh_req_id}
        # Only an explicit approve VERB approves. Free text quoting the card is a question,
        # not a decision — answering it with "✓ Approved" would record one nobody made.
        if not (first in _PROMOTE_APPROVE_WORDS
                or body.strip().upper() in _PROMOTE_APPROVE_SYNONYMS):
            await _send_text(wa, "Tap Approve or Reject on the request above — "
                                 "or reply APPROVE / REJECT.")
            return {"ok": False, "reason": "unparsed", "requisition_id": bh_req_id}
        return await _apply_bh_signoff(conn, user, wa, bh_req_id, "APPROVED", None)
    # (b) A reply with NO quoted button, while a BH reject is armed, IS the reason.
    if not context_id:
        pending_bh = await _pop_pending_bh_reject(conn, wa)
        if pending_bh:
            if not body:
                await _set_pending_bh_reject(conn, wa, pending_bh)   # re-arm
                await _send_text(wa, "Please send the reject reason as a text message.")
                return {"ok": False, "reason": "empty_reason"}
            u = await _resolve_user(conn, wa)
            if u is None:
                await _send_text(wa, "Sorry, this number isn't recognised.")
                return {"ok": False, "reason": "unauthorised"}
            return await _apply_bh_signoff(conn, _WaUser(u["user_id"], u["role_name"]),
                                           wa, pending_bh, "REJECTED", body)

    # ── PROMOTE gate (job-card approval) — resolved BEFORE the NPD review flow, since
    #    its approvers (inventory_manager / requestor BH) are NOT npd_team reviewers. ──
    # (a) Approve / Reject button tap quoting a promote message (context.id → wa_promote_message).
    pm = await _promote_for_wamid(conn, context_id) if context_id else None
    if pm:
        u = await _resolve_user(conn, wa)
        if u is None:
            await _send_text(wa, "Sorry, this number isn't recognised.")
            return {"ok": False, "reason": "unauthorised"}
        user = _WaUser(u["user_id"], u["role_name"])
        if first == "REJECT":
            # Capture a reason first (next reply), like the NPD Hold flow.
            await _set_pending_promote_reject(conn, wa, pm["dev_jc_id"], pm["approver_kind"])
            await _send_text(wa, "Rejecting the promote — please reply with the reason.")
            return {"ok": True, "awaiting": "promote_reason", "dev_jc_id": pm["dev_jc_id"]}
        return await _apply_promote(conn, user, wa, pm["dev_jc_id"], pm["approver_kind"], "ACCEPT", None, pas)
    # (b) A reply with NO quoted button, while a promote reject is armed, IS the reason.
    #     A reject prompt takes priority over re-parsing the reply, so any non-button reply
    #     is captured as the reason (even one starting with "reject…"); empty → re-arm + re-ask.
    if not context_id:
        pp = await _pop_promote_pending(conn, wa)
        if pp:
            if not body:
                await _set_pending_promote_reject(conn, wa, pp["dev_jc_id"], pp["approver_kind"])
                await _send_text(wa, "Please send the reject reason as a text message.")
                return {"ok": False, "reason": "empty_reason"}
            u = await _resolve_user(conn, wa)
            if u is None:
                await _send_text(wa, "Sorry, this number isn't recognised.")
                return {"ok": False, "reason": "unauthorised"}
            return await _apply_promote(conn, _WaUser(u["user_id"], u["role_name"]), wa,
                                        pp["dev_jc_id"], pp["approver_kind"], "REJECT", body, pas)

    # (c) Approve / Reject that DIDN'T resolve via a quoted promote message — Meta
    #     omitted context.id, or quoted a message we never stored. Rather than dead-end
    #     in the NPD-review path, resolve the gate from the sender's OWN pending
    #     promote(s): a bare button tap acts on their single pending gate; "APPROVE/
    #     REJECT <job card no>" targets a specific one; several pending → ask for the no.
    #     Only engages when the sender actually has a promote to act on, so real NPD-review
    #     traffic still falls through unchanged.
    first = body.split(maxsplit=1)[0].upper() if body else ""
    single = body.strip().upper()
    syn_approve = single in _PROMOTE_APPROVE_SYNONYMS
    syn_reject = single in _PROMOTE_REJECT_SYNONYMS
    if first in _PROMOTE_VERBS or syn_approve or syn_reject:
        # A tap quoting a known NPD-REVIEW or BH-approval message is requisition traffic
        # — leave it alone (a BH card was already handled above; this stops an unresolved
        # one from being re-read as a promote).
        review_ctx = bool(context_id) and await conn.fetchval(
            "SELECT 1 FROM wa_review_message WHERE wamid = $1", context_id)
        # A bare yes/no synonym must NOT swallow a one-word NPD hold reason: if this phone
        # has an armed NPD hold, let a synonym-only reply fall through to the review flow.
        # Real APPROVE/REJECT verbs keep priority. Non-destructive check (does not pop).
        synonym_only = first not in _PROMOTE_VERBS
        armed_hold = synonym_only and bool(await conn.fetchval(
            "SELECT 1 FROM wa_pending_action WHERE wa_phone = $1", wa))
        if not review_ctx and not armed_hold:
            u = await _resolve_user(conn, wa)
            if u is not None:
                user = _WaUser(u["user_id"], u["role_name"])
                parts = body.split(maxsplit=2)
                # A jc number can only trail an actual verb ("APPROVE 123"); a bare
                # yes/no synonym is the whole message, so it never carries a ref.
                jc_ref = parts[1] if (first in _PROMOTE_VERBS and len(parts) >= 2) else None
                tgt = await _resolve_promote_target(conn, user, jc_ref=jc_ref)
                if tgt["status"] == "ok":
                    dev_jc_id, kind = tgt["dev_jc_id"], tgt["approver_kind"]
                    if first in _PROMOTE_REJECT_WORDS or syn_reject:
                        inline = (parts[2].strip().lstrip(":-").strip()
                                  if jc_ref and len(parts) >= 3 else "")
                        if inline:
                            return await _apply_promote(conn, user, wa, dev_jc_id, kind, "REJECT", inline, pas)
                        await _set_pending_promote_reject(conn, wa, dev_jc_id, kind)
                        await _send_text(wa, "Rejecting the promote — please reply with the reason.")
                        return {"ok": True, "awaiting": "promote_reason", "dev_jc_id": dev_jc_id}
                    return await _apply_promote(conn, user, wa, dev_jc_id, kind, "ACCEPT", None, pas)
                if tgt["status"] == "ambiguous":
                    opts = "\n".join(f"• {o['dev_jc_id']}" for o in tgt["options"])
                    await _send_text(wa, "You have more than one promote awaiting approval. "
                                         f"Reply  {first} <job card no>  for one of:\n{opts}")
                    return {"ok": False, "reason": "ambiguous_promote",
                            "dev_jc_ids": [o["dev_jc_id"] for o in tgt["options"]]}
                if tgt["status"] == "jc_not_found":
                    await _send_text(wa, f"Couldn't find dev job card {tgt['ref']}.")
                    return {"ok": False, "reason": "jc_not_found"}
                if tgt["status"] == "jc_not_eligible":
                    await _send_text(wa, "That promote isn't awaiting your approval "
                                         "(it may already be actioned).")
                    return {"ok": False, "reason": "jc_not_eligible"}
                # status == "none" → sender has no pending promote; fall through to NPD review.

    # ── NPD review flow (unchanged) ──
    reviewer = await _resolve_reviewer(conn, wa)
    if reviewer is None:
        # Anything reaching here belongs to no ERP flow: the sender is neither an NPD
        # reviewer nor a promote approver, and nothing they sent resolved to a request or
        # gate. Hand it to whichever tenant owns it and stay SILENT — replying for a
        # system they aren't talking to is what made visitor approvers see "you aren't an
        # NPD reviewer" for everything (4e00810).
        #   • approve_<id>/reject_<id> → the visitor system. ONLY these: it accepts
        #     nothing else, and 4e00810's "forward every unattributed inbound" made a
        #     plain "Hi" fan out to visitor AND the maintenance bot, both of which reply.
        #   • everything else ("Hi", an asset name, a photo) → already relayed to the
        #     maintenance bot by the webhook, so say nothing and let it own the reply.
        # Both legs are config-gated: with neither URL set, the reply below is unchanged.
        if raw is not None and is_visitor_approval_payload(_button_payload(raw)):
            if await forward_visitor_approvals([raw]):
                logger.info("Unattributed visitor tap from %s forwarded to the visitor "
                            "webhook (payload=%r)", wa, _button_payload(raw))
                return {"ok": True, "forwarded": "visitor"}
        elif maintenance_forward_url():
            logger.info("Unattributed inbound from %s left to the maintenance bot "
                        "(type=%s text=%r)", wa, (raw or {}).get("type"), body[:60])
            return {"ok": True, "forwarded": "maintenance"}
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
        # Being an NPD reviewer is a property of the PERSON, not of the message.
        # _resolve_reviewer only asks "is this phone an npd_team/admin user?", so every
        # unrelated thing a reviewer ever types landed here and got the ACCEPT/HOLD hint.
        # Meanwhile the SAME webhook relays all non-ERP inbound to the maintenance bot
        # (router.py → forward_maintenance, which is unconditional and fire-and-forget),
        # so a reviewer who typed "hi" got BOTH the maintenance module menu AND this hint.
        #
        # That is the same two-systems-answer-one-message bug the unattributed branch
        # above already fixed for non-reviewers (4e00810) — the principle just never got
        # applied to recognised ones. Only answer when the message is addressed to US:
        # it quotes an NPD review card. An ERP verb would have matched `cmd` already, and
        # an armed hold prompt was consumed further up, so both still reply as before.
        quotes_review = bool(context_id) and bool(await conn.fetchval(
            "SELECT 1 FROM wa_review_message WHERE wamid = $1", context_id))
        if not quotes_review and maintenance_forward_url():
            logger.info("Non-NPD inbound from reviewer %s left to the maintenance bot "
                        "(type=%s text=%r)", wa, (raw or {}).get("type"), body[:60])
            return {"ok": True, "forwarded": "maintenance"}
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
    await _send_text(wa, f"Holding {req['request_id']}. Please reply with the reason.")
    return {"ok": True, "awaiting": "reason", "requisition_id": req["id"]}


def _button_payload(m: dict) -> str | None:
    """The machine payload behind a tap, whatever shape Meta used: a template
    quick-reply's `button.payload`, or an interactive reply's `button_reply.id`.
    None for text and everything else."""
    t = m.get("type")
    if t == "button":
        return (m.get("button") or {}).get("payload")
    if t == "interactive":
        inter = m.get("interactive") or {}
        br = inter.get("button_reply") or inter.get("list_reply") or {}
        return br.get("id")
    return None


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
                                "id": m.get("id"), "payload": _button_payload(m), "raw": m,
                                "context_id": (m.get("context") or {}).get("id")})
    return out


# ── visitor-management approval forwarding (shared WABA) ─────────────────────
def is_visitor_approval_payload(payload_str: str | None) -> bool:
    """True for a Visitor Management quick-reply payload (approve_<id>/reject_<id>)."""
    return bool(payload_str and _VISITOR_APPROVAL_RE.match(payload_str.strip()))


def visitor_approval_messages(payload: dict) -> list[dict]:
    """Raw Meta message objects that are Visitor Management approve/reject taps. These
    belong to the separate visitor system sharing this WABA, not NPD — the webhook
    forwards them to the visitor backend and skips NPD handling for them."""
    out: list[dict] = []
    for entry in (payload or {}).get("entry", []):
        for change in entry.get("changes", []):
            for m in (change.get("value") or {}).get("messages", []):
                if is_visitor_approval_payload(_button_payload(m)):
                    out.append(m)
    return out


async def forward_visitor_approvals(messages: list[dict],
                                    signature: str | None = None) -> int:
    """Forward visitor approve/reject taps to the Visitor Management webhook. Each message
    is wrapped in a minimal valid Meta envelope so the visitor backend parses it with its
    existing handler. Best-effort and config-gated (no URL → no-op): a forwarding failure
    must never break this ERP webhook, so this never raises. Returns the count accepted."""
    url = visitor_forward_url()
    if not url or not messages:
        return 0
    headers = {"Content-Type": "application/json"}
    if signature:
        headers["X-Hub-Signature-256"] = signature  # pass through in case the target verifies
    ok = 0
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for m in messages:
                envelope = {
                    "object": "whatsapp_business_account",
                    "entry": [{"changes": [{"field": "messages", "value": {
                        "messaging_product": "whatsapp", "messages": [m]}}]}],
                }
                try:
                    resp = await client.post(url, json=envelope, headers=headers)
                    if resp.status_code < 400:
                        ok += 1
                    else:
                        logger.warning("Visitor-approval forward rejected: status=%s body=%s",
                                       resp.status_code, resp.text[:300])
                except httpx.HTTPError:
                    logger.exception("Visitor-approval forward failed for message %s", m.get("id"))
    except Exception:  # noqa: BLE001 — forwarding must never break the ERP webhook
        logger.exception("Visitor-approval forwarding aborted unexpectedly")
    return ok


# ── maintenance ticket bot forwarding (shared WABA, third tenant) ────────────
# A THIRD system on this number: an internal maintenance ticket bot. It owns the DEFAULT
# conversation on this number, so the rule is a DENYLIST: relay everything except the two
# flows we know are ours (NPD/promote) or the visitor system's. An allowlist was tried
# first and is wrong here — the bot's form answers are free text, photos, voice notes,
# documents, locations… i.e. every remaining message shape, so enumerating them just
# means silently dropping the next one they add.
# Purely ADDITIVE: unlike the visitor block above, nothing here removes a message from
# the NPD / promote / reason-capture flows — they see exactly what they saw before.
# Read the URL at CALL time — see visitor_forward_url() above for why a module-level
# constant silently freezes to "" on every .env-based deploy.
#
# ERP-owned quick replies carry the button TEXT as their payload (NPD review sends
# "Accept"/"Hold", the promote gate sends "Approve"/"Reject") — there is no id namespace
# to match on, which is why this set exists. Maintenance ids ARE namespaced ("mnt:…",
# "tkt:…") so they can never be caught by it.
_ERP_VERBS = {"accept", "approve", "approved", "hold",
              "reject", "rejected", "decline", "declined"}
# asyncio keeps only WEAK references to tasks: without this set a fire-and-forget
# forward can be garbage-collected mid-flight and vanish with no log line.
_maint_tasks: set[asyncio.Task] = set()


def maintenance_forward_url() -> str:
    return os.environ.get("MAINTENANCE_FORWARD_URL", "").strip()


def is_erp_or_visitor_message(m: dict) -> bool:
    """True for inbound that belongs to a flow ALREADY on this webhook: a Visitor
    Management approve_<id>/reject_<id> tap, or an NPD-review / promote-gate action
    (quick-reply tap or the equivalent typed command).

    Deliberately NOT excluded — the bare yes/no synonyms the promote gate also accepts
    (YES/OK/CONFIRM/NO/N). The ERP only acts on those when the sender has a pending
    promote gate, whereas "yes" answered to a maintenance prompt is ordinary traffic;
    excluding them would break the far more common case to protect the rarer one."""
    p = (_button_payload(m) or "").strip()
    if is_visitor_approval_payload(p):
        return True
    if p.lower() in _ERP_VERBS:
        return True
    if (m.get("type") or "") == "text":
        words = ((m.get("text") or {}).get("body") or "").strip().split(maxsplit=1)
        return bool(words) and words[0].lower() in _ERP_VERBS
    return False


def is_maintenance_message(m: dict) -> bool:
    """Everything that isn't ours or the visitor system's belongs to the maintenance bot.
    Over-forwarding is cheap — they dedupe on wamid and ignore what isn't theirs —
    whereas under-forwarding silently breaks their conversation."""
    return not is_erp_or_visitor_message(m)


def has_maintenance_message(payload: dict) -> bool:
    """True if ANY message in this webhook body is maintenance traffic. The decision is
    per-BODY, not per-message, because the whole raw body is what gets forwarded — a
    rebuilt subset would invalidate the X-Hub-Signature-256 that signs the original
    bytes. Only `value.messages` is scanned, so delivery/read `value.statuses`
    callbacks are never forwarded."""
    return any(is_maintenance_message(m)
               for e in (payload or {}).get("entry", [])
               for c in e.get("changes", [])
               for m in (c.get("value") or {}).get("messages", []))


async def _post_maintenance(raw: bytes, signature: str | None) -> None:
    headers = {"Content-Type": "application/json"}
    if signature:
        headers["X-Hub-Signature-256"] = signature   # signs the bytes below, so it stays valid
    # Shared secret proving the relay really came from this ERP webhook. Meta's signature
    # can't do that job: it authenticates META, not us, and anyone replaying a body they
    # captured would carry a valid one. Unset → header omitted, so we can ship this before
    # the maintenance side starts enforcing it (enforce-first would black-hole every relay).
    secret = os.environ.get("MAINTENANCE_FORWARD_SECRET", "").strip()
    if secret:
        headers["X-Forward-Secret"] = secret
    try:
        # content=raw — the ORIGINAL bytes, unmodified (metadata / contacts / entry.id
        # all intact). httpx `json=` would re-serialise and change key order/spacing,
        # breaking the HMAC the header signs.
        # 60s + one retry, per the maintenance team's spec: their host is free-tier Render
        # (30-60s cold start, so the FIRST relay after idle usually times out) and they
        # dedupe on wamid, so a duplicate relay is dropped and never doubles a reply.
        async with httpx.AsyncClient(timeout=60.0) as client:
            for attempt in (1, 2):
                try:
                    resp = await client.post(maintenance_forward_url(), content=raw,
                                             headers=headers)
                    if resp.status_code < 400:
                        return
                    # 4xx won't fix itself (bad secret, wrong path) — don't burn the retry.
                    if resp.status_code < 500 or attempt == 2:
                        logger.warning("Maintenance forward rejected: status=%s body=%s",
                                       resp.status_code, resp.text[:300])
                        return
                except httpx.HTTPError:
                    if attempt == 2:
                        raise
                logger.info("Maintenance forward attempt %d failed — retrying once", attempt)
    except Exception:  # noqa: BLE001 — a background forward must never surface anywhere
        logger.exception("Maintenance forward failed")


def forward_maintenance(raw: bytes, payload: dict, signature: str | None) -> bool:
    """Relay the full raw webhook body to the maintenance ticket backend. Returns
    whether a forward was queued. Fire-and-forget on purpose: Meta retries the webhook
    (→ DUPLICATE tickets on their side) if we don't 200 quickly, and the target
    cold-starts slowly, so this must never sit on the request path. No-op when
    MAINTENANCE_FORWARD_URL is unset — the webhook then behaves exactly as before."""
    if not maintenance_forward_url():
        return False
    if not has_maintenance_message(payload):
        # Logged because a silent "nothing relayed" is indistinguishable from a denylist
        # that has started over-matching and is swallowing the bot's traffic.
        logger.info("Nothing to relay to maintenance — body is all ERP/visitor traffic")
        return False
    t = asyncio.create_task(_post_maintenance(raw, signature))
    _maint_tasks.add(t)
    t.add_done_callback(_maint_tasks.discard)
    return True


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
    no = updated.get("request_id")
    await _send_text(wa, f"✓ {no} {'accepted' if action == 'APPROVE' else 'put on hold'}."
                         + (f" Reason: {reason}" if action == "HOLD" else ""))
    return {"ok": True, "requisition_id": req_id, "action": action}
