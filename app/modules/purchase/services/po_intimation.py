"""No-PO purchase intimation — WhatsApp to the purchase team.

Fired by the Material In page's "Send Purchase Intimation" action for walk-in
material that arrived WITHOUT a purchase order. Sends the approved
``purchase_without_po_intimation`` template on the shared WABA:

  header  : IMAGE — the article table (Article / Rate / Qty (Kgs) / Base Value /
            GST Value + a Total row) rendered as a PNG.
  body    : invoice_no, vendor_name, vehicle_number, article_list, quantity,
            base_value, gst_value, indentor, warehouse, timestamp.
  buttons : the template's own quick replies ("PO Created & Uploaded" /
            "Don't Accept the Material"). Taps come back through the SHARED
            inbound webhook (sample/whatsapp_service.handle_inbound), which calls
            handle_po_intimation_tap() below — see section 5.

Recipients are every ACTIVE user holding a purchase role, counting BOTH the
primary ``auth_user.role_id`` and the ``auth_user_role`` multi-role rows.

Config + creds are read from os.environ at call time (mirrors qc_intimation),
so a WHATSAPP_ENABLED flip takes effect without a restart.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.modules.auth.services.phone import normalize as normalize_phone

logger = logging.getLogger(__name__)

_GRAPH_BASE_DEFAULT = "https://graph.facebook.com/v21.0"
_TPL_DEFAULT = "purchase_without_po_intimation"
_TPL_LANG_DEFAULT = "en"  # approved under "English" = en (not en_US), like qc_inward_intimation

# Roles that own PO creation. Only `purchase_manager` exists in auth_role today;
# the tuple keeps it a one-word change if a second purchase role is added.
PURCHASE_ROLES = ("purchase_manager",)

# Who gets told once the PO exists. `store_head` is the codebase's name for the
# stores / Material-In clerk (ROLE_MODULE_SCOPE, the Purchase landing gate, and
# 075_rbac_notes_catalog.sql all use it); 085 finally creates the auth_role row.
STORES_ROLES = ("store_head",)

# Body variables in the template's {{1}}..{{10}} NUMBERING — which is NOT the
# order they read in the message. The approved template puts {{6}} on the
# Timestamp line and {{7}} on Warehouse, below {{8}}/{{9}}/{{10}} (Base Value /
# GST value / Indentor); sending them in reading order shuffled five fields in
# production (base_value surfaced as "Timestamp", indentor as "Base Value", …).
#
# Derived from an actual delivered message rather than guessed: base 100090 ×
# 0.05 = gst 5004.5 identified which numeric was which, and the remaining three
# were unambiguous. Meta's API cannot be asked — the WABA system-user token can
# reach /messages but no WABA id is discoverable from the phone-number id or
# debug_token, so /message_templates is unreachable. If the template is ever
# re-authored, re-derive this tuple the same way.
#
# Sent POSITIONALLY first (the template uses numbered variables, so the named
# form 400s); the named attempt stays as a fallback in case it is rebuilt with
# named variables, where the key matches by name and this order is irrelevant.
_BODY_ORDER = (
    "invoice_no",      # {{1}}
    "vendor_name",     # {{2}}
    "vehicle_number",  # {{3}}
    "article_list",    # {{4}}
    "quantity",        # {{5}}
    "timestamp",       # {{6}} — sits on the "Timestamp" line
    "warehouse",       # {{7}} — "Warehouse"
    "base_value",      # {{8}} — "Base Value"
    "gst_value",       # {{9}} — "GST value"
    "indentor",        # {{10}} — "Indentor"
)


# ── env helpers ──────────────────────────────────────────────────────────────
def _graph_base() -> str:
    return os.environ.get("WHATSAPP_GRAPH_BASE", _GRAPH_BASE_DEFAULT).rstrip("/")


def _access_token() -> str:
    return os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()


def _phone_number_id() -> str:
    return os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()


def _tpl_name() -> str:
    return os.environ.get("WHATSAPP_TPL_PURCHASE_WITHOUT_PO", _TPL_DEFAULT)


def _tpl_lang() -> str:
    return os.environ.get("WHATSAPP_PURCHASE_WITHOUT_PO_LANG", _TPL_LANG_DEFAULT)


def _wa_enabled() -> bool:
    if os.environ.get("WHATSAPP_ENABLED", "true").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    return bool(_access_token() and _phone_number_id())


# ── number / text formatting ─────────────────────────────────────────────────
def _num(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _fmt_num(v: Any) -> str:
    """Integral values render without a trailing '.0'; else up to 2 dp, trimmed."""
    f = _num(v)
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}".rstrip("0").rstrip(".")


def _one_line(v: Any) -> str:
    """Collapse ALL internal whitespace — the Cloud API 400s a template body
    parameter containing a newline, tab, or 4+ consecutive spaces."""
    s = " ".join(str(v).split()) if v is not None else ""
    return s or "-"


def gst_fraction(v: Any) -> float:
    """all_sku.gst is MIXED: ~3.9k rows store a fraction (0.050, 0.180) but ~476
    store the percent instead (5, 12, 18). Anything above 1 can only be a percent
    — the top real GST slab is 28% — so scale it. Without this the percent-style
    SKUs bill 100× the GST value."""
    g = _num(v)
    return g / 100 if g > 1 else g


def priced_lines(rows: list[dict]) -> list[dict]:
    """Normalise the caller's rows into {name, rate, qty, base, gst_value}."""
    out: list[dict] = []
    for r in rows:
        rate = _num(r.get("rate"))
        qty = _num(r.get("qty"))
        base = rate * qty
        out.append(
            {
                "name": r.get("name") or r.get("sku_name") or "-",
                "rate": rate,
                "qty": qty,
                "base": base,
                "gst_value": base * gst_fraction(r.get("gst")),
            }
        )
    return out


# ── 1. article-table PNG ─────────────────────────────────────────────────────
# ponytail: near-duplicate of customer_returns/wa_notify.render_articles_png
# (same yellow-header table style, different columns). Merge the two into one
# generic renderer if a third template ever needs a table.
_IMG_W = 980
_MARGIN = 18
_PAD = 12
_TITLE_SIZE = 26
_HEAD_SIZE = 21
_BODY_SIZE = 20
_ROW_PAD = 10
_NUM_COL_W = 155
_TITLE = "PO Intimation - Purchased W/O PO"
_HEADERS = ("Rate", "Qty (Kgs)", "Base Value", "GST Value")
_C_TITLE = (24, 24, 24)
_C_TEXT = (34, 34, 34)
_C_BORDER = (170, 170, 170)
_C_HEADFILL = (252, 236, 141)   # soft yellow — matches the approved sample sheet
_C_STRIPE = (250, 250, 250)


def render_articles_png(lines: list[dict]) -> str:
    """Render the priced article table to a temp PNG; returns its path (caller
    deletes). Uses PIL's default TrueType — no font files needed."""
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

    left, right = _MARGIN, _IMG_W - _MARGIN
    # Four right-aligned numeric columns pinned to the right edge; the article
    # name takes whatever is left.
    col_r = [right - _PAD - (3 - i) * _NUM_COL_W for i in range(4)]
    verticals = [right - k * _NUM_COL_W for k in range(4, 0, -1)]
    art_l = left + _PAD
    art_max = max(120, verticals[0] - _PAD - art_l)

    wrapped = [_wrap(l.get("name"), body_font, art_max) for l in lines]
    row_h = [len(w) * body_lh + _ROW_PAD for w in wrapped]

    title_h = _lh(title_font) + _ROW_PAD * 2
    total_h = title_h + row_step + sum(row_h) + row_step + _MARGIN * 2

    img = Image.new("RGB", (_IMG_W, total_h), (255, 255, 255))
    d = ImageDraw.Draw(img)

    def _cell_r(text: str, x_r: int, y_t: int, font, fill=_C_TEXT):
        d.text((x_r - font.getlength(text), y_t), text, font=font, fill=fill)

    y = _MARGIN
    d.text((art_l, y + _ROW_PAD), _TITLE, font=title_font, fill=_C_TITLE)
    y += title_h

    table_top = y
    d.rectangle([(left, y), (right, y + row_step)], fill=_C_HEADFILL)
    hy = y + (row_step - _lh(head_font)) // 2
    d.text((art_l, hy), "Article", font=head_font, fill=_C_TITLE)
    for i, h in enumerate(_HEADERS):
        _cell_r(h, col_r[i], hy, head_font, _C_TITLE)
    y += row_step

    tot_qty = tot_base = tot_gst = 0.0
    for i, l in enumerate(lines):
        h = row_h[i]
        if i % 2 == 1:
            d.rectangle([(left, y), (right, y + h)], fill=_C_STRIPE)
        d.line([(left, y), (right, y)], fill=_C_BORDER, width=1)
        ty = y + _ROW_PAD // 2
        for ln in wrapped[i]:
            d.text((art_l, ty), ln, font=body_font, fill=_C_TEXT)
            ty += body_lh
        cy = y + (h - body_lh) // 2
        for j, key in enumerate(("rate", "qty", "base", "gst_value")):
            _cell_r(_fmt_num(l.get(key)), col_r[j], cy, body_font)
        tot_qty += _num(l.get("qty"))
        tot_base += _num(l.get("base"))
        tot_gst += _num(l.get("gst_value"))
        y += h

    d.line([(left, y), (right, y)], fill=_C_BORDER, width=1)
    d.rectangle([(left, y), (right, y + row_step)], fill=_C_HEADFILL)
    ty = y + (row_step - _lh(head_font)) // 2
    d.text((art_l, ty), "Total", font=head_font, fill=_C_TITLE)
    _cell_r(_fmt_num(tot_qty), col_r[1], ty, head_font, _C_TITLE)
    _cell_r(_fmt_num(tot_base), col_r[2], ty, head_font, _C_TITLE)
    _cell_r(_fmt_num(tot_gst), col_r[3], ty, head_font, _C_TITLE)
    y += row_step

    d.rectangle([(left, table_top), (right, y)], outline=_C_BORDER, width=1)
    for x in verticals:
        d.line([(x, table_top), (x, y)], fill=_C_BORDER, width=1)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    img.save(tmp.name, format="PNG")
    return tmp.name


# ── 2. WABA I/O ──────────────────────────────────────────────────────────────
async def _upload_media(png_path: str) -> str:
    with open(png_path, "rb") as fh:
        file_bytes = fh.read()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_graph_base()}/{_phone_number_id()}/media",
            headers={"Authorization": f"Bearer {_access_token()}"},
            data={"messaging_product": "whatsapp", "type": "image/png"},
            files={"file": (os.path.basename(png_path), file_bytes, "image/png")},
        )
    if resp.status_code >= 400:
        logger.error("[po-wa] media upload failed %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"Media upload failed: HTTP {resp.status_code} — {resp.text[:200]}")
    return resp.json()["id"]


def _payload(to: str, media_id: str, values: dict[str, str], *, named: bool) -> dict:
    params = [
        ({"type": "text", "parameter_name": k, "text": values[k]} if named
         else {"type": "text", "text": values[k]})
        for k in _BODY_ORDER
    ]
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": _tpl_name(),
            "language": {"code": _tpl_lang()},
            "components": [
                {"type": "header", "parameters": [{"type": "image", "image": {"id": media_id}}]},
                {"type": "body", "parameters": params},
            ],
        },
    }


async def _send_template(to: str, media_id: str, values: dict[str, str]) -> dict[str, Any]:
    """Send to one recipient. Tries POSITIONAL body params (what the approved
    template actually uses), then NAMED — Meta 400s on the wrong shape."""
    url = f"{_graph_base()}/{_phone_number_id()}/messages"
    headers = {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}
    last = ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        for named in (False, True):
            resp = await client.post(url, json=_payload(to, media_id, values, named=named), headers=headers)
            if resp.status_code < 400:
                return resp.json()
            last = f"HTTP {resp.status_code} — {resp.text[:200]}"
            logger.warning("[po-wa] %s-param send rejected to=%s: %s",
                           "named" if named else "positional", to, last)
    raise RuntimeError(f"Template send failed: {last}")


async def _send_text(to: str, body: str) -> None:
    """Free-form reply. Only deliverable inside Meta's 24-hour customer-service
    window (i.e. to someone who messaged us recently) — which a button tap opens,
    so the replies to a tapper always land.

    ponytail: the stores notification rides this too, and stores has NOT
    necessarily messaged us — outside the window Meta returns 131047 and the send
    is reported as failed rather than silently dropped. Upgrade path is an
    approved template (add WHATSAPP_TPL_PO_CREATED_STORES + a _payload variant).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_graph_base()}/{_phone_number_id()}/messages",
            headers={"Authorization": f"Bearer {_access_token()}",
                     "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": to, "type": "text",
                  "text": {"body": body}},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} — {resp.text[:200]}")


async def _recipients(conn, roles) -> list:
    """Active users holding ANY of `roles`, counting the primary auth_user.role_id
    AND auth_user_role — a user whose purchase/stores role is secondary must still
    be notified."""
    return await conn.fetch(
        """
        SELECT DISTINCT u.phone, r.role_name
          FROM auth_user u
          JOIN auth_role r
            ON r.role_id = u.role_id
            OR r.role_id IN (SELECT ur.role_id FROM auth_user_role ur WHERE ur.user_id = u.user_id)
         WHERE r.role_name = ANY($1)
           AND u.is_active
           AND u.phone IS NOT NULL
           AND btrim(u.phone) <> ''
        """,
        list(roles),
    )


def _wamid(resp: dict) -> str | None:
    try:
        return (resp.get("messages") or [{}])[0].get("id")
    except (AttributeError, IndexError, TypeError):
        return None


# ── 3. Orchestrator ──────────────────────────────────────────────────────────
async def send_po_creation_intimation(
    pool,
    *,
    transaction_no: str | None = None,
    invoice_no: str | None,
    vendor_name: str | None,
    vehicle_number: str | None,
    indentor: str | None,
    warehouse: str | None,
    lines: list[dict],
) -> dict[str, Any]:
    """Notify the purchase team that material arrived without a PO.

    ``lines`` = [{name, rate, qty, gst}] — gst is the all_sku fraction.
    Returns {template, recipients, skipped, errors} (same shape as
    qc_intimation.send_intimation so the UI banner is shared).
    """
    tname = _tpl_name()

    if not _wa_enabled():
        return {"template": tname, "recipients": [],
                "skipped": [{"role": "*", "reason": "whatsapp_disabled"}], "errors": []}

    async with pool.acquire() as conn:
        rows = await _recipients(conn, PURCHASE_ROLES)

    if not rows:
        return {"template": tname, "recipients": [],
                "skipped": [{"role": "/".join(PURCHASE_ROLES), "reason": "no_purchase_recipients"}],
                "errors": []}

    priced = priced_lines(lines)
    ist = timezone(timedelta(hours=5, minutes=30))
    values = {
        "invoice_no": _one_line(invoice_no),
        "vendor_name": _one_line(vendor_name),
        "vehicle_number": _one_line(vehicle_number),
        "article_list": _one_line(", ".join(l["name"] for l in priced)),
        "quantity": _fmt_num(sum(l["qty"] for l in priced)),
        "base_value": _fmt_num(sum(l["base"] for l in priced)),
        "gst_value": _fmt_num(sum(l["gst_value"] for l in priced)),
        "indentor": _one_line(indentor),
        "warehouse": _one_line(warehouse),
        "timestamp": datetime.now(ist).strftime("%d-%m-%Y %H:%M"),
    }

    png_path: str | None = None
    recipients: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        try:
            png_path = render_articles_png(priced)
            media_id = await _upload_media(png_path)
        except Exception as exc:  # noqa: BLE001 — report, never break the arrival record
            logger.exception("[po-wa] header image failed")
            errors.append(f"Article image failed: {exc}")
            return {"template": tname, "recipients": [], "skipped": [], "errors": errors}

        for row in rows:
            raw_phone = row["phone"]
            to_wa = (normalize_phone(raw_phone) or raw_phone).lstrip("+")
            try:
                resp = await _send_template(to_wa, media_id, values)
                recipients.append({"role": row["role_name"], "phone": to_wa, "status": "sent"})
                # Remember what this message was about, so a "PO Created & Uploaded"
                # tap (which quotes this wamid and carries nothing else) resolves back.
                await _store_sent_message(
                    pool, _wamid(resp),
                    transaction_no=transaction_no,
                    vendor_name=values["vendor_name"],
                    invoice_no=values["invoice_no"],
                    article_list=values["article_list"],
                    wa_phone=to_wa,
                )
            except Exception as exc:  # noqa: BLE001 — one bad number must not abort the batch
                logger.warning("[po-wa] send failed for %s: %s", to_wa, exc)
                recipients.append({"role": row["role_name"], "phone": to_wa,
                                   "status": "failed", "error": str(exc)})
    finally:
        if png_path:
            try:
                os.remove(png_path)
            except OSError:
                pass

    return {"template": tname, "recipients": recipients, "skipped": [], "errors": errors}


# ── 4. wamid ↔ arrival mapping + pending PO-number capture ───────────────────
async def _store_sent_message(pool, wamid, *, transaction_no, vendor_name,
                              invoice_no, article_list, wa_phone) -> None:
    """Best-effort — the intimation itself has already been delivered, so a failure
    here must not turn a successful send into a failed one. It only costs the
    button taps on that message."""
    if not wamid or not transaction_no:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO wa_po_intimation_message
                    (wamid, transaction_no, vendor_name, invoice_no, article_list, wa_phone)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (wamid) DO NOTHING
                """,
                wamid, transaction_no, vendor_name, invoice_no, article_list, wa_phone,
            )
    except Exception:  # noqa: BLE001
        logger.exception("[po-wa] could not store wamid %s for %s", wamid, transaction_no)


# ── 5. INBOUND: a button tap / reply on a purchase intimation ────────────────
# Meta quick replies carry only their label + context.id, never a payload of our
# own, so the label text is what routing keys on.
_CREATED_PREFIXES = ("PO CREATED", "PO_CREATED", "CREATED")
_REJECT_PREFIXES = ("DON'T ACCEPT", "DONT ACCEPT", "DO NOT ACCEPT", "REJECT")


def _match(text: str, prefixes) -> bool:
    t = " ".join((text or "").upper().split())
    return any(t.startswith(p) for p in prefixes)


async def notify_stores_po_created(conn, *, po_number: str, row) -> dict[str, Any]:
    """Tell the stores team a PO now exists for a walk-in arrival.

    `row` is the wa_po_intimation_message record, so the message names the
    consignment (vendor / invoice / articles) and not just a bare number.
    """
    recipients = await _recipients(conn, STORES_ROLES)
    if not recipients:
        logger.info("[po-wa] no stores recipients for PO %s", po_number)
        return {"sent": 0, "skipped": "no_stores_recipients"}

    body = (
        f"PO created for walk-in arrival {row['transaction_no']}.\n\n"
        f"PO No. - {po_number}\n"
        f"VENDOR NAME - {row['vendor_name'] or '-'}\n"
        f"Invoice No. - {row['invoice_no'] or '-'}\n"
        f"Article list - {row['article_list'] or '-'}\n\n"
        "The material can now be received against this PO."
    )

    sent, failed = 0, []
    for r in recipients:
        to_wa = (normalize_phone(r["phone"]) or r["phone"]).lstrip("+")
        try:
            await _send_text(to_wa, body)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — one bad number must not abort the batch
            logger.warning("[po-wa] stores notify failed for %s: %s", to_wa, exc)
            failed.append({"phone": to_wa, "error": str(exc)})
    return {"sent": sent, "failed": failed}


async def handle_po_intimation_tap(conn, wa: str, text: str, context_id: str | None) -> dict | None:
    """Route an inbound message that belongs to a purchase intimation.

    Returns None when the message is NOT ours, so the shared webhook keeps routing
    it to the CR / promote / NPD flows. Never raises out of the webhook.

    Two entry points:
      1. A button tap quoting an intimation we sent (context_id → wa_po_intimation_message).
         "PO Created & Uploaded" arms a pending capture and asks for the number;
         "Don't Accept the Material" is acknowledged and closes the thread.
      2. A plain reply (no context_id) while that phone has a capture armed — the
         reply IS the PO number, and stores gets told.
    """
    body = (text or "").strip()

    # (2) armed capture — checked first: a PO number like "PO Created" is not a thing,
    #     and a reply with no quoted message can only be the answer to our prompt.
    if not context_id:
        try:
            pending = await conn.fetchrow(
                "SELECT wamid, transaction_no FROM wa_po_intimation_pending WHERE wa_phone = $1", wa)
        except Exception:  # noqa: BLE001 — 085 not applied yet / transient: let other flows run
            logger.exception("[po-wa] pending lookup failed for %s — deferring", wa)
            return None
        if not pending:
            return None
        po_number = " ".join(body.split())
        if not po_number:
            await _send_text(wa, "Please send the PO number as a text message.")
            return {"ok": False, "reason": "empty_po_number"}
        # Own it from here: clear the prompt so a retype isn't read as a second PO.
        await conn.execute("DELETE FROM wa_po_intimation_pending WHERE wa_phone = $1", wa)
        row = await conn.fetchrow(
            "SELECT transaction_no, vendor_name, invoice_no, article_list "
            "FROM wa_po_intimation_message WHERE wamid = $1", pending["wamid"])
        if row is None:  # message row pruned — still forward what we know
            row = {"transaction_no": pending["transaction_no"], "vendor_name": None,
                   "invoice_no": None, "article_list": None}
        result = await notify_stores_po_created(conn, po_number=po_number, row=row)
        if result["sent"]:
            await _send_text(wa, f"✓ Noted PO {po_number}. Stores has been informed.")
        else:
            await _send_text(
                wa, f"✓ Noted PO {po_number}. Stores could not be reached on WhatsApp — "
                    "please inform them directly.")
        logger.info("[po-wa] PO %s captured for %s, stores sent=%s",
                    po_number, row["transaction_no"], result["sent"])
        return {"ok": True, "po_number": po_number,
                "transaction_no": row["transaction_no"], "stores": result}

    # (1) a tap quoting one of our intimations
    try:
        row = await conn.fetchrow(
            "SELECT transaction_no, vendor_name, invoice_no, article_list "
            "FROM wa_po_intimation_message WHERE wamid = $1", context_id)
    except Exception:  # noqa: BLE001 — never abort a tap that belongs to another flow
        logger.exception("[po-wa] wa_po_intimation_message lookup failed for ctx=%s", context_id)
        return None
    if row is None:
        return None  # not ours

    try:
        if _match(body, _CREATED_PREFIXES):
            await conn.execute(
                """
                INSERT INTO wa_po_intimation_pending (wa_phone, wamid, transaction_no)
                VALUES ($1, $2, $3)
                ON CONFLICT (wa_phone) DO UPDATE
                  SET wamid = EXCLUDED.wamid, transaction_no = EXCLUDED.transaction_no,
                      created_at = NOW()
                """,
                wa, context_id, row["transaction_no"],
            )
            await _send_text(wa, "Thanks — please reply with the PO number so stores can be informed.")
            return {"ok": True, "awaiting": "po_number", "transaction_no": row["transaction_no"]}

        if _match(body, _REJECT_PREFIXES):
            await conn.execute("DELETE FROM wa_po_intimation_pending WHERE wa_phone = $1", wa)
            await _send_text(
                wa, f"Noted — material for {row['transaction_no']} is not to be accepted. "
                    "Please inform the gate/stores team.")
            return {"ok": True, "decision": "rejected", "transaction_no": row["transaction_no"]}

        await _send_text(wa, "Please tap “PO Created & Uploaded” or “Don't Accept the Material”.")
        return {"ok": False, "reason": "unparsed", "transaction_no": row["transaction_no"]}
    except Exception:  # noqa: BLE001 — an owned tap must not raise into the shared webhook
        logger.exception("[po-wa] handle_po_intimation_tap failed ctx=%s", context_id)
        return {"ok": False, "reason": "exception", "transaction_no": row["transaction_no"]}


if __name__ == "__main__":  # self-check: totals, GST styles, slot order, PNG
    rows = [
        {"name": "Oats", "rate": 120, "qty": 100, "gst": 0.05},
        {"name": "Rolled Oats", "rate": 140, "qty": 150, "gst": 0.05},
        {"name": "Besan", "rate": 50, "qty": 250, "gst": 0.05},
    ]
    p = priced_lines(rows)
    assert [l["base"] for l in p] == [12000, 21000, 12500], p
    assert [l["gst_value"] for l in p] == [600, 1050, 625], p
    assert _fmt_num(sum(l["qty"] for l in p)) == "500"
    assert _fmt_num(sum(l["base"] for l in p)) == "45500"
    assert _fmt_num(sum(l["gst_value"] for l in p)) == "2275"
    assert _one_line("a\n b    c") == "a b c"
    assert _one_line(None) == "-"

    # all_sku stores GST both ways — 18 and 0.18 must bill the same.
    assert gst_fraction(0.18) == gst_fraction(18) == 0.18
    assert gst_fraction(5) == 0.05 and gst_fraction(None) == 0.0
    pct = priced_lines([{"name": "X", "rate": 100, "qty": 10, "gst": 18}])
    assert pct[0]["gst_value"] == 180, pct  # not 18000

    # Positional slots must match the approved template's numbering, NOT the
    # order the lines read in the message. Regression guard for the live shuffle
    # that put base_value on the "Timestamp" line.
    vals = {k: k for k in _BODY_ORDER}
    slots = [q["text"] for q in _payload("91x", "mid", vals, named=False)["template"]["components"][1]["parameters"]]
    assert slots[5] == "timestamp" and slots[6] == "warehouse", slots
    assert slots[7] == "base_value" and slots[8] == "gst_value" and slots[9] == "indentor", slots
    assert len(slots) == 10 and slots[0] == "invoice_no", slots

    png = render_articles_png(p)
    assert os.path.getsize(png) > 0
    print("ok —", png)
