"""Customer-Returns WhatsApp head-approval notification.

On "Send for Approval" the mapped Business Head AND every standing deputy approver
(`deputy_approver` rows in cr_email_routing) get the `customer_returns_head_approval`
template on the shared WABA: an IMAGE header (the articles table rendered as a PNG) +
positional body variables ({{1}}..{{11}}) + the template's built-in Approve / Reject /
Hold quick-reply buttons. A button tap is quoted back with `context.id` = the sent
message's wamid; the shared webhook (sample/whatsapp_service.handle_inbound) resolves it
via `wa_return_message` and calls `handle_return_button_tap` here, which applies the CR
decision through the SAME approval_service the email magic-link uses.

One message per approver means one wamid per approver, so a deputy's tap resolves
through their own row — the inbound handler needed no change to accept it. Two people
tapping is safe: apply_cr_action is idempotent and reports the second as already
actioned.

Self-contained WABA client (mirrors purchase/qc_intimation + sample/whatsapp_service —
each tenant on this number has its own). Best-effort: a missing/unconfigured WABA or a
send failure is logged and swallowed so it never breaks the send-for-approval action.
Config + creds read from os.environ at call time (hydrated in main.py lifespan).
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any, Optional

import httpx

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
async def _resolve_bh_phone(conn, business_head: Optional[str]) -> Optional[str]:
    """Phone of a person by auth_user.full_name (active, non-blank phone).

    Used for the CR's mapped Business Head and, identically, for each standing
    deputy — a deputy's `match_key` in cr_email_routing IS their full_name.
    """
    if not business_head or not str(business_head).strip():
        return None
    phone = await conn.fetchval(
        "SELECT phone FROM auth_user WHERE lower(btrim(full_name)) = lower(btrim($1)) "
        "AND COALESCE(is_active, TRUE) AND phone IS NOT NULL AND btrim(phone) <> '' LIMIT 1",
        business_head,
    )
    return _fmt_phone(phone) if phone else None


async def _resolve_approver_phones(conn, detail: dict) -> list[tuple[str, str]]:
    """[(name, phone)] for the BU Head and every standing deputy, deduped by number.

    Deduped by PHONE, not by name: if a deputy happens to be the CR's own BU Head
    they must not get the same template twice, which would also write two
    wa_return_message rows and let one person's second tap look like a second
    decision.

    A deputy with no auth_user row (or no number on it) is simply absent from this
    list — their mail still carries the buttons. WhatsApp is the faster channel,
    never the only one.
    """
    names = [detail.get("business_head")]
    try:
        routing = await mail_service.fetch_routing(conn)
        names += [d.get("name") for d in routing.get("deputy_approver", [])]
    except Exception:  # noqa: BLE001 — a routing failure must not cost the BH send
        logger.exception("[cr-wa] deputy routing lookup failed — BU Head only")

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in names:
        if not name or not str(name).strip():
            continue
        phone = await _resolve_bh_phone(conn, name)
        if not phone:
            logger.info("[cr-wa] no phone on file for %r — skipping their WhatsApp", name)
            continue
        if phone in seen:
            continue
        seen.add(phone)
        out.append((str(name).strip(), phone))
    return out


async def _resolve_actor(conn, wa: str) -> str:
    """Identify the tapping user for the approval audit (email/name), else the phone."""
    try:
        from app.modules.auth.services.phone import lookup_keys
        keys = lookup_keys(wa)
    except Exception:  # noqa: BLE001
        keys = [wa]
    row = await conn.fetchrow(
        "SELECT email, full_name FROM auth_user WHERE phone = ANY($1::text[]) "
        "AND COALESCE(is_active, TRUE) LIMIT 1", list(keys))
    if row:
        return (row["email"] or row["full_name"] or wa)
    return wa


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


async def notify_cr_head_approval(conn, detail: dict) -> dict:
    """Send the head-approval WhatsApp (image header + body + buttons) to the CR's
    Business Head AND every standing deputy. Best-effort — never raises.

    Each recipient gets their own message, so each has its own wamid and its own
    wa_return_message row; a tap resolves through the row it quotes, which is what
    lets a deputy act on the same CR without any change to the inbound handler.
    The decision itself is idempotent, so two people tapping is safe.

    The articles PNG is rendered and uploaded ONCE and the media id reused — the
    image is identical for every recipient, and re-uploading it per person would
    triple the Graph round-trips on the send-for-approval request path.
    """
    company = str(detail.get("company") or "").upper()
    cr_id = str(detail.get("rtv_id") or "")
    if not _wa_enabled():
        return {"status": "skipped", "reason": "wa_disabled"}
    png_path = None
    try:
        targets = await _resolve_approver_phones(conn, detail)
        if not targets:
            logger.info("[cr-wa] no approver phone for %s (business_head=%r) — skipping",
                        cr_id, detail.get("business_head"))
            return {"status": "skipped", "reason": "no_bh_phone"}
        png_path = render_articles_png(detail.get("customer") or "", detail.get("lines") or [])
        media_id = await _upload_media(png_path)
        if not media_id:
            return {"status": "error", "reason": "media_upload_failed"}

        sent: list[dict] = []
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
            resp = await _post_message(payload)
            wamid = _wamid(resp)
            if wamid:
                await _store_return_message(conn, wamid, company, cr_id, phone)
            sent.append({"name": name, "to": phone, "wamid": wamid})
            logger.info("[cr-wa] head-approval sent cr=%s to=%s (%s) wamid=%s",
                        cr_id, phone, name, wamid)

        first = sent[0]
        # `to`/`wamid` stay top-level for the single-recipient shape callers and the
        # existing logs already read.
        return {"status": "sent", "to": first["to"], "wamid": first["wamid"],
                "recipients": sent}
    except Exception:  # noqa: BLE001 — must not break send-for-approval
        logger.exception("[cr-wa] notify_cr_head_approval failed cr=%s", cr_id)
        return {"status": "error", "reason": "exception"}
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
    NOT a customer-return message (so the caller's NPD/promote flow continues)."""
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
        first = (text or "").strip().split(maxsplit=1)[0].upper() if text else ""
        action = _TAP_ACTION.get(first)
        if not action:
            await _send_text(wa, f"Reply Approve / Reject / Hold on customer return {cr_id}.")
            return {"ok": False, "reason": "cr_unparsed", "rtv_id": cr_id}

        actor = await _resolve_actor(conn, wa)
        result = await approval_service.apply_cr_action(conn, company, cr_id, action, actor)
        status = result["status"]
        if result["already_actioned"]:
            await _send_text(wa, f"Customer return {cr_id} is already “{status}”.")
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
        return {"ok": False, "reason": "cr_exception", "rtv_id": cr_id}
