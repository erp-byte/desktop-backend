"""Customer-Returns WhatsApp head-approval notification.

On "Send for Approval" every approver of the CR — its Business Head (when the name
resolves to an e-mail) AND every standing deputy approver (`deputy_approver` rows in
cr_email_routing), i.e. mail_service.resolve_approvers, the people approval_service
lets decide — except whoever raised it (mail_service.eligible_approvers: nobody
decides their own return, so they are not sent buttons that would only refuse them)
gets the `customer_returns_head_approval` template on the shared WABA:
an IMAGE header (the articles table rendered as a PNG) + positional body variables
({{1}}..{{11}}) + the template's built-in Approve / Reject / Hold quick-reply buttons.
Each is sent to the number on the ERP account with that approver's e-mail. A button
tap is quoted back with `context.id` = the sent message's wamid; the shared webhook
(sample/whatsapp_service.handle_inbound) resolves it via `wa_return_message` and calls
`handle_return_button_tap` here. A tap counts only from the number that message went
to, and decides as the e-mail of the ERP user on that number through the SAME
approval_service.apply_cr_action the mail link and the app use — so the same people
may decide, and a decided CR stays decided.

One message per approver means one wamid per approver, so a deputy's tap resolves
through their own row — the inbound handler needed no change to accept it. Two people
tapping is safe: apply_cr_action is idempotent and reports the second as already
actioned.

Self-contained WABA client (mirrors purchase/qc_intimation + sample/whatsapp_service —
each tenant on this number has its own). Best-effort: a missing/unconfigured WABA or a
send failure is logged and never breaks the send-for-approval action — but the result
says what happened (status sent / partial / failed / skipped, with counts), so a
failed send is not reported as sent. Config + creds read from os.environ at call time
(hydrated in main.py lifespan).
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from app.modules.customer_returns.services import approval_service, mail_service, query_service

logger = logging.getLogger(__name__)

_GRAPH_BASE_DEFAULT = "https://graph.facebook.com/v21.0"
_TPL_DEFAULT = "customer_returns_head_approval"
_TPL_LANG_DEFAULT = "en"


# ── env / WABA config (read at call time) ─────────────────────────────────────
def _graph_base() -> str:
    return os.environ.get("WHATSAPP_GRAPH_BASE", _GRAPH_BASE_DEFAULT).rstrip("/")


def _access_token() -> str:
    return os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()


def _phone_number_id() -> str:
    return os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()


def _tpl_name() -> str:
    return os.environ.get("WHATSAPP_TPL_CR_HEAD_APPROVAL", _TPL_DEFAULT)


def _tpl_lang() -> str:
    return os.environ.get("WHATSAPP_CR_HEAD_APPROVAL_LANG", _TPL_LANG_DEFAULT)


def _wa_enabled() -> bool:
    if os.environ.get("WHATSAPP_ENABLED", "true").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    return bool(_access_token() and _phone_number_id())


def _fmt_phone(raw) -> str:
    """E.164 digits, no '+'. Bare 10-digit Indian numbers get a 91 prefix."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    return digits


def _same_phone(a, b) -> bool:
    """Two numbers are one when their digits are, read the way wa_phone was stored
    (_fmt_phone: '+' and separators dropped, a bare 10-digit number given 91)."""
    ka = _fmt_phone(a)
    return bool(ka) and ka == _fmt_phone(b)


# ── number formatting ─────────────────────────────────────────────────────────
def _num(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0  # NUMERIC 'NaN'/overflow -> 0 (int(nan) would raise)


def _fmt_num(v: Any) -> str:
    """Integral values render without a trailing '.0'; else up to 3 dp, trimmed."""
    f = _num(v)
    if f == int(f):
        return str(int(f))
    return f"{f:.3f}".rstrip("0").rstrip(".")


# ── 1. article-table PNG ──────────────────────────────────────────────────────
_IMG_W = 820
_MARGIN = 18
_PAD = 12
_TITLE_SIZE = 26
_HEAD_SIZE = 22
_BODY_SIZE = 21
_ROW_PAD = 10
_C_TITLE = (24, 24, 24)
_C_TEXT = (34, 34, 34)
_C_BORDER = (170, 170, 170)
_C_HEADFILL = (252, 236, 141)   # soft yellow, like the sample sheet header
_C_TOTALFILL = (252, 236, 141)
_C_STRIPE = (250, 250, 250)


def render_articles_png(customer: str, lines: list[dict]) -> str:
    """Render the returned-articles table (Article / Qty / In Kg / Value + Total)
    to a temp PNG and return its path (caller deletes). Uses PIL default TrueType
    (no font files → Lambda-safe)."""
    from PIL import Image, ImageDraw, ImageFont  # lazy import

    title_font = ImageFont.load_default(size=_TITLE_SIZE)
    head_font = ImageFont.load_default(size=_HEAD_SIZE)
    body_font = ImageFont.load_default(size=_BODY_SIZE)

    def _lh(font) -> int:
        b = font.getbbox("Ayg")
        return b[3] - b[1]

    def _wrap(text: str, font, max_px: int) -> list[str]:
        words = str(text or "-").split()
        if not words:
            return ["-"]
        out, cur = [], ""
        for w in words:
            trial = w if not cur else f"{cur} {w}"
            if font.getlength(trial) <= max_px:
                cur = trial
            else:
                if cur:
                    out.append(cur)
                cur = w if font.getlength(w) <= max_px else w[: max(1, len(w) // 2)]
        if cur:
            out.append(cur)
        return out or ["-"]

    body_lh = _lh(body_font)
    row_step = body_lh + _ROW_PAD

    # column x-edges
    left = _MARGIN
    right = _IMG_W - _MARGIN
    col_qty_r = right - int((right - left) * 0.36)
    col_kg_r = right - int((right - left) * 0.18)
    col_val_r = right - _PAD
    art_l = left + _PAD
    art_max = col_qty_r - 70 - art_l

    wrapped = [_wrap(l.get("item_description", "-"), body_font, art_max) for l in lines]
    data_row_h = [len(w) * body_lh + _ROW_PAD for w in wrapped]

    title_h = _lh(title_font) + _ROW_PAD * 2
    head_h = row_step
    total_h_row = row_step
    total_h = title_h + head_h + sum(data_row_h) + total_h_row + _MARGIN * 2

    img = Image.new("RGB", (_IMG_W, total_h), (255, 255, 255))
    d = ImageDraw.Draw(img)

    y = _MARGIN
    d.text((left + _PAD, y + _ROW_PAD), f"Customer Returned - {customer or '-'}",
           font=title_font, fill=_C_TITLE)
    y += title_h

    table_top = y
    col_edges = [left, col_qty_r, col_kg_r, col_val_r + _PAD]  # verticals

    def _cell_right(text: str, x_r: int, y_t: int, font, fill=_C_TEXT):
        d.text((x_r - font.getlength(text), y_t), text, font=font, fill=fill)

    # header row
    d.rectangle([(left, y), (right, y + head_h)], fill=_C_HEADFILL)
    hy = y + (head_h - _lh(head_font)) // 2
    d.text((art_l, hy), "Article", font=head_font, fill=_C_TITLE)
    _cell_right("Qty", col_qty_r, hy, head_font, _C_TITLE)
    _cell_right("In Kg", col_kg_r, hy, head_font, _C_TITLE)
    _cell_right("Value", col_val_r, hy, head_font, _C_TITLE)
    y += head_h

    tot_qty = tot_kg = tot_val = 0.0
    for i, l in enumerate(lines):
        h = data_row_h[i]
        if i % 2 == 1:
            d.rectangle([(left, y), (right, y + h)], fill=_C_STRIPE)
        ty = y + _ROW_PAD // 2
        for ln in wrapped[i]:
            d.text((art_l, ty), ln, font=body_font, fill=_C_TEXT)
            ty += body_lh
        cy = y + (h - _lh(body_font)) // 2
        _cell_right(_fmt_num(l.get("qty")), col_qty_r, cy, body_font)
        _cell_right(_fmt_num(l.get("net_weight")), col_kg_r, cy, body_font)
        _cell_right(_fmt_num(l.get("value")), col_val_r, cy, body_font)
        tot_qty += _num(l.get("qty"))
        tot_kg += _num(l.get("net_weight"))
        tot_val += _num(l.get("value"))
        d.line([(left, y), (right, y)], fill=_C_BORDER, width=1)
        y += h

    # total row
    d.rectangle([(left, y), (right, y + total_h_row)], fill=_C_TOTALFILL)
    ty = y + (total_h_row - _lh(head_font)) // 2
    d.text((art_l, ty), "Total", font=head_font, fill=_C_TITLE)
    _cell_right(_fmt_num(tot_qty), col_qty_r, ty, head_font, _C_TITLE)
    _cell_right(_fmt_num(tot_kg), col_kg_r, ty, head_font, _C_TITLE)
    _cell_right(_fmt_num(tot_val), col_val_r, ty, head_font, _C_TITLE)
    y += total_h_row

    # outer border + column verticals
    d.rectangle([(left, table_top), (right, y)], outline=_C_BORDER, width=1)
    for x in col_edges[1:]:
        d.line([(x, table_top), (x, y)], fill=_C_BORDER, width=1)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    img.save(tmp.name, format="PNG")
    return tmp.name


# ── 2. WABA I/O (best-effort) ─────────────────────────────────────────────────
async def _upload_media(png_path: str) -> Optional[str]:
    url = f"{_graph_base()}/{_phone_number_id()}/media"
    with open(png_path, "rb") as fh:
        file_bytes = fh.read()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url, headers={"Authorization": f"Bearer {_access_token()}"},
            data={"messaging_product": "whatsapp", "type": "image/png"},
            files={"file": (os.path.basename(png_path), file_bytes, "image/png")},
        )
    if resp.status_code >= 400:
        logger.error("[cr-wa] media upload failed %s %s", resp.status_code, resp.text[:300])
        return None
    return resp.json().get("id")


async def _post_message(payload: dict) -> dict:
    url = f"{_graph_base()}/{_phone_number_id()}/messages"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers={
            "Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"})
    if resp.status_code >= 400:
        logger.error("[cr-wa] send failed %s %s", resp.status_code, resp.text[:400])
        return {}
    return resp.json()


async def _send_text(to: str, text: str) -> None:
    if not _wa_enabled():
        return
    try:
        await _post_message({"messaging_product": "whatsapp", "to": _fmt_phone(to),
                             "type": "text", "text": {"body": text}})
    except Exception:  # noqa: BLE001 — confirmations must never raise
        logger.exception("[cr-wa] _send_text failed")


def _wamid(resp: dict) -> Optional[str]:
    try:
        return (resp.get("messages") or [{}])[0].get("id")
    except Exception:  # noqa: BLE001
        return None


# ── 3. recipient / actor resolution ───────────────────────────────────────────
async def _phone_for_email(conn, email: Optional[str]) -> Optional[str]:
    """WhatsApp number (E.164 digits) of the active ERP user with this e-mail, else None.

    By e-mail, not display name: the account found is the one whose tap resolves
    back (_resolve_actor_email) to this same e-mail, which is what approval_service
    checks — so everyone messaged can actually decide, and a namesake of the BU Head
    is never sent the buttons.
    """
    if not email or not str(email).strip():
        return None
    phone = await conn.fetchval(
        "SELECT phone FROM auth_user WHERE lower(btrim(email)) = lower(btrim($1)) "
        "AND COALESCE(is_active, TRUE) AND phone IS NOT NULL AND btrim(phone) <> '' "
        "ORDER BY user_id LIMIT 1",
        str(email).strip(),
    )
    return _fmt_phone(phone) if phone else None


async def _resolve_approver_phones(conn, detail: dict) -> list[tuple[str, str]]:
    """[(name, phone)] for every approver of the CR — the BU Head (when their name
    resolves to an e-mail) and each standing deputy, mail_service.resolve_approvers:
    the same people approval_service lets decide — but the person who raised it
    (mail_service.eligible_approvers), deduped by number.

    Deduped by PHONE, not by name: if a deputy happens to be the CR's own BU Head
    they must not get the same template twice, which would also write two
    wa_return_message rows and let one person's second tap look like a second
    decision.

    An approver with no active account under their e-mail (or no number on it) is
    simply absent from this list — their mail still carries the buttons. WhatsApp
    is the faster channel, never the only one.
    """
    routing = await mail_service.fetch_routing(conn)
    approvers = mail_service.resolve_approvers(routing, detail.get("business_head"))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in mail_service.eligible_approvers(approvers, detail.get("created_by")):
        phone = await _phone_for_email(conn, a["email"])
        if not phone:
            logger.info("[cr-wa] no phone on file for %r (%s) — skipping their WhatsApp",
                        a["name"], a["email"])
            continue
        if phone in seen:
            continue
        seen.add(phone)
        out.append((a["name"], phone))
    return out


async def _resolve_actor_email(conn, wa: str) -> Optional[str]:
    """E-mail of the active ERP user on the tapping number, else None — the identity
    a tap decides as (approval_service checks it like the other channels)."""
    try:
        from app.modules.auth.services.phone import lookup_keys
        keys = lookup_keys(wa)
    except Exception:  # noqa: BLE001
        keys = [wa]
    row = await conn.fetchrow(
        "SELECT email FROM auth_user WHERE phone = ANY($1::text[]) "
        "AND COALESCE(is_active, TRUE) "
        "ORDER BY (COALESCE(btrim(email), '') = ''), user_id LIMIT 1", list(keys))
    email = str((row["email"] if row else None) or "").strip()
    return email or None


# ── 4. wamid <-> CR mapping ────────────────────────────────────────────────────
async def _store_return_message(conn, wamid: str, company: str, rtv_id: str, wa_phone: str) -> None:
    await conn.execute(
        "INSERT INTO wa_return_message (wamid, company, rtv_id, wa_phone) "
        "VALUES ($1,$2,$3,$4) ON CONFLICT (wamid) DO NOTHING",
        wamid, company.upper(), rtv_id, wa_phone,
    )


async def _return_for_wamid(conn, wamid: str):
    return await conn.fetchrow(
        "SELECT company, rtv_id, wa_phone FROM wa_return_message WHERE wamid = $1", wamid)


# ── 5. OUTBOUND: send the head-approval template ──────────────────────────────
def _body_params(detail: dict) -> list[str]:
    """Positional {{1}}..{{11}} in the confirmed order."""
    lines = detail.get("lines") or []
    sale_group = next((l.get("sale_group") for l in lines if l.get("sale_group")), "") or "-"
    tot_qty = sum(_num(l.get("qty")) for l in lines)
    tot_val = sum(_num(l.get("value")) for l in lines)

    # Collapse ALL internal whitespace: the WhatsApp Cloud API rejects a template body
    # parameter containing a newline, tab, or 4+ consecutive spaces (a multi-line remark
    # would otherwise 400 the whole send).
    def g(v):
        return (" ".join(str(v).split()) or "-") if v is not None else "-"
    return [
        g(detail.get("rtv_id")),          # {{1}} CR No
        g(detail.get("factory_unit")),    # {{2}} Warehouse
        g(detail.get("customer")),        # {{3}} Customer
        g(sale_group),                    # {{4}} Sales Group
        g(detail.get("business_head")),   # {{5}} BU Head
        g(detail.get("sales_poc")),       # {{6}} Sales POC
        g(detail.get("invoice_number")),  # {{7}} Invoice Number
        str(len(lines)),                  # {{8}} Article (count)
        _fmt_num(tot_qty),                # {{9}} Qty
        _fmt_num(tot_val),                # {{10}} Value
        g(detail.get("remark")),          # {{11}} Remarks/Reason
    ]


def _wa_outcome(targets: list, recipients: list[dict], reason: Optional[str] = None) -> dict:
    """{status, sent, failed, reason?, recipients[, to, wamid]} — status is 'sent' when
    every approver's message went out (and its tap can be traced back), 'failed' when
    none did, 'partial' in between. `to`/`wamid` stay top-level (the first recipient)
    for the single-recipient shape the logs already read."""
    ok = sum(1 for r in recipients if r.get("sent"))
    failed = max(len(targets), len(recipients)) - ok
    out: dict = {"status": "sent" if not failed else ("partial" if ok else "failed"),
                 "sent": ok, "failed": failed, "recipients": recipients}
    if reason and failed:
        out["reason"] = reason
    if recipients:
        out["to"], out["wamid"] = recipients[0]["to"], recipients[0]["wamid"]
    return out


async def notify_cr_head_approval(conn, detail: dict) -> dict:
    """Send the head-approval WhatsApp (image header + body + buttons) to every
    approver of the CR who can decide it (_resolve_approver_phones). Best-effort —
    never raises — and truthful: {status: sent | partial | failed | skipped, sent,
    failed, reason?}. 'skipped' (WhatsApp not configured, or no such approver has a
    number on file) is not a failure: the mail carries the same buttons.

    Each recipient gets their own message, so each has its own wamid and its own
    wa_return_message row; a tap resolves through the row it quotes, which is what
    lets a deputy act on the same CR without any change to the inbound handler.
    The decision itself is idempotent, so two people tapping is safe. A message
    Graph refuses (no wamid) is that recipient's failure and the rest still go; a
    mapping row that cannot be stored stops the run — that tap could not be traced
    back to the CR, and neither could any later one.

    The articles PNG is rendered and uploaded ONCE and the media id reused — the
    image is identical for every recipient, and re-uploading it per person would
    triple the Graph round-trips on the send-for-approval request path.
    """
    company = str(detail.get("company") or "").upper()
    cr_id = str(detail.get("rtv_id") or "")
    if not _wa_enabled():
        return {"status": "skipped", "reason": "wa_disabled", "sent": 0, "failed": 0}
    png_path = None
    targets: list[tuple[str, str]] = []
    recipients: list[dict] = []
    try:
        targets = await _resolve_approver_phones(conn, detail)
        if not targets:
            logger.info("[cr-wa] no approver phone for %s (business_head=%r) — skipping",
                        cr_id, detail.get("business_head"))
            return {"status": "skipped", "reason": "no_approver_phone", "sent": 0, "failed": 0}
        png_path = render_articles_png(detail.get("customer") or "", detail.get("lines") or [])
        media_id = await _upload_media(png_path)
        if not media_id:
            return _wa_outcome(targets, [], "media_upload_failed")

        reason: Optional[str] = None
        for name, phone in targets:
            payload = {
                "messaging_product": "whatsapp", "to": phone, "type": "template",
                "template": {
                    "name": _tpl_name(), "language": {"code": _tpl_lang()},
                    "components": [
                        {"type": "header", "parameters": [{"type": "image", "image": {"id": media_id}}]},
                        {"type": "body", "parameters": [
                            {"type": "text", "text": p} for p in _body_params(detail)]},
                    ],
                },
            }
            wamid = _wamid(await _post_message(payload))
            if not wamid:
                # _post_message logged Graph's refusal; the next number may still go.
                reason = reason or "send_failed"
                recipients.append({"name": name, "to": phone, "wamid": None, "sent": False})
                logger.warning("[cr-wa] head-approval NOT sent cr=%s to=%s (%s)", cr_id, phone, name)
                continue
            try:
                await _store_return_message(conn, wamid, company, cr_id, phone)
            except Exception:  # noqa: BLE001 — delivered, but its tap could not find the CR
                logger.exception("[cr-wa] wa_return_message write failed cr=%s wamid=%s — "
                                 "stopping the WhatsApp leg", cr_id, wamid)
                recipients.append({"name": name, "to": phone, "wamid": wamid, "sent": False})
                return _wa_outcome(targets, recipients, "mapping_store_failed")
            recipients.append({"name": name, "to": phone, "wamid": wamid, "sent": True})
            logger.info("[cr-wa] head-approval sent cr=%s to=%s (%s) wamid=%s",
                        cr_id, phone, name, wamid)
        return _wa_outcome(targets, recipients, reason)
    except Exception:  # noqa: BLE001 — must not break send-for-approval
        logger.exception("[cr-wa] notify_cr_head_approval failed cr=%s", cr_id)
        return _wa_outcome(targets, recipients, "exception") if (targets or recipients) else \
            {"status": "failed", "reason": "exception", "sent": 0, "failed": 0}
    finally:
        if png_path:
            try:
                os.unlink(png_path)
            except OSError:
                pass


# ── 6. INBOUND: a button tap that quotes a head-approval message ───────────────
_TAP_ACTION = {"APPROVE": "approve", "APPROVED": "approve",
               "REJECT": "reject", "REJECTED": "reject",
               "HOLD": "hold"}


async def handle_return_button_tap(conn, wa: str, text: str, context_id: Optional[str]) -> Optional[dict]:
    """If `context_id` quotes a head-approval message we sent, apply the tapped
    decision and reply. Returns a result dict when handled, or None when the tap is
    NOT a customer-return message (so the caller's NPD/promote flow continues).

    A tap decides only when it comes from the number that message was sent to
    (wa_return_message.wa_phone) — from any other number it is answered and changes
    nothing — and then as the e-mail of the ERP user on that number, through
    approval_service.apply_cr_action like the mail link and the app: a refusal (not
    an approver, their own return, no approver) is replied with its reason."""
    if not context_id:
        return None
    # Best-effort resolution: if the lookup fails (wa_return_message absent because 083
    # isn't applied yet, or a transient DB error), return None so the shared webhook's
    # promote / NPD-review branches still run — this branch must NEVER abort a tap that
    # belongs to another flow.
    try:
        row = await _return_for_wamid(conn, context_id)
    except Exception:  # noqa: BLE001
        logger.exception("[cr-wa] wa_return_message lookup failed for ctx=%s — deferring", context_id)
        return None
    if not row:
        return None  # not ours — let the shared webhook keep routing

    # From here the tap IS a customer-return message: OWN it. Never fall through (that
    # would double-process it in another flow) and never raise out of the shared webhook.
    company, cr_id = row["company"], row["rtv_id"]
    try:
        if not _same_phone(wa, row["wa_phone"]):
            logger.warning("[cr-wa] tap on cr=%s from %s, but it was sent to %s — nothing done",
                           cr_id, wa, row["wa_phone"])
            await _send_text(wa, "This approval was sent to a different number.")
            return {"ok": False, "reason": "cr_wrong_phone", "rtv_id": cr_id}

        first = (text or "").strip().split(maxsplit=1)[0].upper() if text else ""
        action = _TAP_ACTION.get(first)
        if not action:
            await _send_text(wa, f"Reply Approve / Reject / Hold on customer return {cr_id}.")
            return {"ok": False, "reason": "cr_unparsed", "rtv_id": cr_id}

        actor = await _resolve_actor_email(conn, wa)
        try:
            result = await approval_service.apply_cr_action(conn, company, cr_id, action, actor)
        except HTTPException as exc:
            await _send_text(wa, f"Customer return {cr_id} was not changed. "
                                 f"{approval_service.error_message(exc)}")
            error = exc.detail.get("error") if isinstance(exc.detail, dict) else None
            return {"ok": False, "reason": "cr_refused", "error": error,
                    "status_code": exc.status_code, "rtv_id": cr_id}
        status = result["status"]
        if result["already_actioned"]:
            await _send_text(wa, f"Customer return {cr_id} is already "
                                 f"“{approval_service.status_label(status)}”.")
            return {"ok": True, "already_actioned": True, "rtv_id": cr_id, "status": status}

        # threaded status mail, mirroring the email-action endpoint (best-effort)
        try:
            detail = await query_service.get_cr(conn, company, cr_id)
            detail["company"] = company
            await mail_service.notify_cr_status_changed(conn, detail, status, actor)
        except Exception:  # noqa: BLE001
            logger.exception("[cr-wa] status-change mail failed cr=%s", cr_id)

        await _send_text(wa, f"✓ Customer return {cr_id} is now “{status}”.")
        return {"ok": True, "rtv_id": cr_id, "status": status, "actor": actor}
    except Exception:  # noqa: BLE001 — an owned tap must not raise into the shared webhook
        logger.exception("[cr-wa] handle_return_button_tap failed cr=%s", cr_id)
        # Nothing after the decision raises (the status mail is guarded above), so the
        # tap was not recorded — say so rather than leave the approver guessing.
        await _send_text(wa, f"Your reply on customer return {cr_id} could not be recorded. "
                             f"Please use the approval e-mail or the ERP.")
        return {"ok": False, "reason": "cr_exception", "rtv_id": cr_id}
