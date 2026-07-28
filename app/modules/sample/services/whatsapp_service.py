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
     Your gate: {{1}}                                      [ "Inventory manager" / "Requestor (business head)" ]
     Dev job card: {{2}}                                   [ dev JC id (8-digit) ]
     Target FG article: {{3}}   Target quantity: {{4}}
     Company: {{5}}   Customer: {{6}}
     Return type: {{7}}   Paid: {{8}}   Amount: {{9}}
     <a closing "Tap *Approve* … or *Reject* …" line — body must not END on a variable>

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
from datetime import datetime, timedelta, timezone
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
# Promote dual-approval gate (NPD dev job card): Approve / Reject quick replies.
TPL_PROMOTE = os.environ.get("WHATSAPP_TPL_NPD_PROMOTE", "npd_promote_approval")
_PROMOTE_GATE_LABEL = {"INV_MGR": "Inventory manager", "REQUESTOR_BH": "Requestor (business head)"}
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
        # A tap quoting a known NPD-REVIEW message is review traffic — leave it alone.
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
        # gate. On this shared WABA that makes them the visitor system's user, so hand the
        # whole message over (taps AND typed text — the visitor backend accepts both) and
        # stay silent rather than answering for a system they aren't talking to.
        # No-ops when VISITOR_APPROVAL_FORWARD_URL is unset, so a single-tenant deploy
        # keeps the reply below unchanged.
        if raw is not None and await forward_visitor_approvals([raw]):
            logger.info("Unattributed inbound from %s forwarded to the visitor webhook "
                        "(type=%s payload=%r text=%r)",
                        wa, raw.get("type"), _button_payload(raw), body[:60])
            return {"ok": True, "forwarded": "visitor"}
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
