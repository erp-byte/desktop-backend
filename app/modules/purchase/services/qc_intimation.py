"""QC Inward Intimation — WhatsApp send service.

Responsibilities:
  1. render_article_png  — Pillow-based PNG of the article list (temp file).
  2. upload_media        — POST the PNG to Meta's media endpoint; returns media_id.
  3. send_template       — POST the qc_inward_intimation template to one recipient.
  4. send_intimation     — Orchestrator: resolve recipients, generate/upload image,
                           fan-out send, clean up temp file.

Config is read from os.environ at call time (same pattern as otp_service) so a
WHATSAPP_ENABLED flip takes effect without a restart.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.auth.services.phone import normalize as normalize_phone

logger = logging.getLogger(__name__)

# ── env helpers ─────────────────────────────────────────────────────────────

_GRAPH_BASE_DEFAULT = "https://graph.facebook.com/v21.0"
_INTIMATION_TEMPLATE_DEFAULT = "qc_inward_intimation"
_INTIMATION_LANG_DEFAULT = "en"  # qc_inward_intimation is approved under "English" = en (not en_US)


def _graph_base() -> str:
    return os.environ.get("WHATSAPP_GRAPH_BASE", _GRAPH_BASE_DEFAULT).rstrip("/")


def _access_token() -> str:
    return os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()


def _phone_number_id() -> str:
    return os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()


def _template_name() -> str:
    return os.environ.get(
        "WHATSAPP_INTIMATION_TEMPLATE_NAME", _INTIMATION_TEMPLATE_DEFAULT
    )


def _template_lang() -> str:
    return os.environ.get("WHATSAPP_INTIMATION_LANG", _INTIMATION_LANG_DEFAULT)


def _wa_enabled() -> bool:
    """Mirrors otp_service's three-gate check (ENABLED + TOKEN + PHONE_ID)."""
    enabled_raw = os.environ.get("WHATSAPP_ENABLED", "true").strip().lower()
    if enabled_raw not in ("1", "true", "yes", "on"):
        return False
    return bool(_access_token() and _phone_number_id())


# ── 1. PNG rendering ─────────────────────────────────────────────────────────

# Layout constants
_IMG_WIDTH = 760
_PAD_X = 30          # horizontal padding inside the image
_PAD_TOP = 22        # top padding before the header bar
_PAD_BOTTOM = 22
_TITLE_SIZE = 30
_BODY_SIZE = 23
_ROW_GAP = 12        # extra gap between rows (on top of line height)
_SEP_COLOR = (226, 226, 226)        # light grey row separator
_BG_COLOR = (255, 255, 255)         # white background
_TITLE_COLOR = (24, 24, 24)         # near-black title
_TEXT_COLOR = (44, 44, 44)          # body text
_HEADER_BAR_COLOR = (247, 247, 247) # very light grey header bar
_STRIPE_COLOR = (250, 250, 250)     # alternating row stripe
_WATERMARK_ALPHA = 26               # 0-255 — faint centered logo watermark

# Bundled brand logo (copied from frontend_replica/candor_logo v1.jpg).
# services/ -> purchase/ -> assets/candor_logo.jpg
_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "candor_logo.jpg"


def _wrap_text(text: str, font: Any, max_px: int) -> list[str]:
    """Word-wrap *text* into lines that each fit within *max_px* px.

    Long article names flow onto multiple lines (rendered below) instead of
    being truncated. A single word wider than *max_px* is hard-broken
    character by character so nothing ever overflows the image width.
    """
    words = str(text).split()
    if not words:
        return ["-"]
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = w if not cur else f"{cur} {w}"
        if font.getlength(trial) <= max_px:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if font.getlength(w) <= max_px:
            cur = w
        else:
            piece = ""
            for ch in w:
                if font.getlength(piece + ch) <= max_px:
                    piece += ch
                else:
                    if piece:
                        lines.append(piece)
                    piece = ch
            cur = piece
    if cur:
        lines.append(cur)
    return lines or ["-"]


def _load_logo():
    """Load the brand logo as RGBA, or None if it isn't bundled (graceful)."""
    try:
        from PIL import Image
        return Image.open(_LOGO_PATH).convert("RGBA")
    except Exception as exc:  # missing file / unreadable — skip the watermark
        logger.warning("render_article_png: logo unavailable (%s); skipping watermark", exc)
        return None


def render_article_png(article_names: list[str]) -> str:
    """Render a clean titled PNG of the article names with a faint Candor logo
    watermark + a small header logo.

    Title is ASCII-only and long names are ellipsised so nothing is clipped.
    Uses PIL.ImageFont.load_default(size=N) (a scalable TrueType in Pillow 10+)
    — no external font files required (safe for Lambda / minimal Linux).
    The logo is bundled in assets/; if absent the image still renders cleanly.

    Returns the path to the generated temp PNG (caller must delete it).
    """
    from PIL import Image, ImageDraw, ImageFont  # lazy import

    title_font = ImageFont.load_default(size=_TITLE_SIZE)
    body_font = ImageFont.load_default(size=_BODY_SIZE)

    def _lh(font) -> int:
        b = font.getbbox("Ayg")        # include ascender + descender
        return b[3] - b[1]

    title_lh = _lh(title_font)
    body_lh = _lh(body_font)

    header_h = max(title_lh, 46) + _ROW_GAP * 2
    available_w = _IMG_WIDTH - _PAD_X * 2
    line_step = body_lh + 5   # vertical step between wrapped lines of one article

    # Wrap each article name (multi-line) instead of truncating with "...".
    wrapped = [_wrap_text(name if name else "-", body_font, available_w) for name in article_names]
    row_heights = [len(lines) * line_step + _ROW_GAP for lines in wrapped]
    rows_h = sum(row_heights) if row_heights else (body_lh + _ROW_GAP)
    total_h = _PAD_TOP + header_h + rows_h + _PAD_BOTTOM

    base = Image.new("RGBA", (_IMG_WIDTH, total_h), _BG_COLOR + (255,))
    draw = ImageDraw.Draw(base)

    rows_top = _PAD_TOP + header_h

    # Header bar
    draw.rectangle([(0, _PAD_TOP), (_IMG_WIDTH, rows_top)], fill=_HEADER_BAR_COLOR)

    # Row stripes + separators — one block per article (variable height),
    # drawn before the watermark so the logo sits above the stripes but
    # below the text.
    y = rows_top
    for i, h in enumerate(row_heights):
        if i % 2 == 1:
            draw.rectangle([(0, y), (_IMG_WIDTH, y + h)], fill=_STRIPE_COLOR)
        draw.line([(0, y), (_IMG_WIDTH, y)], fill=_SEP_COLOR, width=1)
        y += h
    draw.line([(0, y), (_IMG_WIDTH, y)], fill=_SEP_COLOR, width=1)

    logo = _load_logo()

    # Faint centered watermark over the article-rows area.
    if logo is not None and rows_h > 20:
        box_w, box_h = int(_IMG_WIDTH * 0.52), int(rows_h * 0.7)
        scale = min(box_w / logo.width, box_h / logo.height)
        wm = logo.resize((max(1, int(logo.width * scale)), max(1, int(logo.height * scale))))
        faint = wm.split()[3].point(lambda v: int(v * (_WATERMARK_ALPHA / 255)))
        wm.putalpha(faint)
        wx = (_IMG_WIDTH - wm.width) // 2
        wy = rows_top + (rows_h - wm.height) // 2
        base.alpha_composite(wm, (max(0, wx), max(rows_top, wy)))

    # Small crisp header logo + title.
    title_x = _PAD_X
    if logo is not None:
        target_h = max(30, title_lh)
        lr = target_h / logo.height
        small = logo.resize((max(1, int(logo.width * lr)), target_h))
        base.alpha_composite(small, (_PAD_X, _PAD_TOP + (header_h - small.height) // 2))
        title_x = _PAD_X + small.width + 16

    draw.text(
        (title_x, _PAD_TOP + (header_h - title_lh) // 2 - 2),
        "QC Inward - Article List",
        font=title_font, fill=_TITLE_COLOR,
    )

    # Article rows text — stacked wrapped lines per article (on top of the
    # stripes + watermark).
    y = rows_top
    for i, lines in enumerate(wrapped):
        ty = y + _ROW_GAP // 2
        for ln in lines:
            draw.text((_PAD_X, ty), ln, font=body_font, fill=_TEXT_COLOR)
            ty += line_step
        y += row_heights[i]

    out = base.convert("RGB")  # base is fully opaque white → flatten safely
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    out.save(tmp.name, format="PNG")
    logger.debug("render_article_png: wrote %d articles to %s", len(article_names), tmp.name)
    return tmp.name


# ── 2. Media upload ──────────────────────────────────────────────────────────


async def upload_media(png_path: str) -> str:
    """Upload *png_path* to Meta's media endpoint and return the media_id.

    POST {GRAPH_BASE}/{PHONE_NUMBER_ID}/media
    multipart/form-data:
      messaging_product = whatsapp
      type              = image/png
      file              = <binary>
    """
    url = f"{_graph_base()}/{_phone_number_id()}/media"
    headers = {"Authorization": f"Bearer {_access_token()}"}

    filename = os.path.basename(png_path)
    with open(png_path, "rb") as fh:
        file_bytes = fh.read()

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers=headers,
            data={"messaging_product": "whatsapp", "type": "image/png"},
            files={"file": (filename, file_bytes, "image/png")},
        )

    if resp.status_code >= 400:
        logger.error(
            "upload_media: Meta rejected upload status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        raise RuntimeError(
            f"Media upload failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )

    body = resp.json()
    media_id: str = body["id"]
    logger.info("upload_media: media_id=%s", media_id)
    return media_id


# ── 3. Template send ─────────────────────────────────────────────────────────


async def send_template(
    to_e164_no_plus: str,
    media_id: str,
    *,
    po_number: str,
    invoice_no: str,
    vendor_name: str,
    vehicle_number: str,
    timestamp: str,
    template_name: str,
    lang: str,
) -> dict[str, Any]:
    """Send the qc_inward_intimation template to one recipient.

    Uses NAMED body parameters (parameter_name) as required by the approved
    Meta utility template with 5 named params.

    Returns the parsed Graph API response dict. Raises RuntimeError on
    HTTP errors so the caller can capture per-recipient failures without
    aborting the batch.
    """
    url = f"{_graph_base()}/{_phone_number_id()}/messages"
    headers = {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_e164_no_plus,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {"type": "image", "image": {"id": media_id}},
                    ],
                },
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "parameter_name": "po_number",      "text": po_number},
                        {"type": "text", "parameter_name": "invoice_no",     "text": invoice_no},
                        {"type": "text", "parameter_name": "vendor_name",    "text": vendor_name},
                        {"type": "text", "parameter_name": "vehicle_number", "text": vehicle_number},
                        {"type": "text", "parameter_name": "timestamp",      "text": timestamp},
                    ],
                },
            ],
        },
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code >= 400:
        logger.error(
            "send_template: Graph API error to=%s status=%s body=%s",
            to_e164_no_plus, resp.status_code, resp.text[:500],
        )
        raise RuntimeError(
            f"Template send failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}

    try:
        msg = (body.get("messages") or [{}])[0]
        logger.info(
            "send_template: accepted to=%s wamid=%s status=%s",
            to_e164_no_plus, msg.get("id"), msg.get("message_status"),
        )
    except (AttributeError, IndexError, KeyError, TypeError):
        logger.info("send_template: response=%s", str(body)[:300])

    return body


# ── 3b. Arrival persistence ──────────────────────────────────────────────────


async def record_arrivals(
    pool,
    *,
    po_number: str | None,
    transaction_no: str,
    supplier_id,
    supplier_name: str | None,
    entity: str | None,
    vehicle_no: str | None,
    invoice_no: str | None,
    lines: list[dict],
) -> int:
    """Persist one qc_intimation (arrival event) row per selected article line.

    Each call inserts NEW rows (no dedup) so the same transaction+article can
    arrive multiple times (partial arrivals). status='pending'. Returns count.

    Skips lines that have neither sku_name/particulars nor sku_id (defensive).
    """
    rows_to_insert = []
    for line in lines:
        sku_id = line.get("sku_id")
        sku_name_raw = line.get("sku_name")
        sku_name = sku_name_raw or line.get("particulars")
        if not sku_name and sku_id is None:
            logger.debug(
                "record_arrivals: skipping line with no sku_name/particulars and no sku_id"
            )
            continue
        rows_to_insert.append(
            (
                po_number,
                transaction_no,
                sku_id,
                sku_name,
                sku_name_raw,
                supplier_id,
                supplier_name,
                line.get("lot_number"),  # likely None — po_line may not have it
                vehicle_no,
                None,           # warehouse — not known at arrival time
                entity,
                invoice_no,
            )
        )

    if not rows_to_insert:
        logger.warning(
            "record_arrivals: no insertable lines for transaction_no=%s", transaction_no
        )
        return 0

    # qc_intimation_id is an app-supplied 8-digit time-based BIGINT (see
    # app.core.helpers.new_short_time_id). Insert per-row (not executemany) so
    # each row gets a fresh id and PK retry can resolve the same-millisecond
    # collisions expected when several articles arrive in one batch.
    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in rows_to_insert:
                async def _insert_arrival(_row=row):
                    return await conn.execute(
                        """
                        INSERT INTO qc_intimation (
                            qc_intimation_id,
                            po_number, transaction_no,
                            sku_id, sku_name, sku_name_raw,
                            supplier_id, supplier_name,
                            lot_number, vehicle_no, warehouse,
                            entity, invoice_no,
                            status, coa_received
                        ) VALUES (
                            $1,
                            $2, $3,
                            $4, $5, $6,
                            $7, $8,
                            $9, $10, $11,
                            $12, $13,
                            'pending', FALSE
                        )
                        """,
                        new_short_time_id(),
                        *_row,
                    )

                await insert_with_pk_retry(conn, _insert_arrival)

    logger.info(
        "record_arrivals: inserted %d arrival rows for transaction_no=%s",
        len(rows_to_insert),
        transaction_no,
    )
    return len(rows_to_insert)


# ── 4. Orchestrator ──────────────────────────────────────────────────────────


async def send_intimation(
    pool,
    *,
    po_number: str,
    vendor_name: str,
    article_names: list[str],
    vehicle_number: str,
    invoice_no: str,
) -> dict[str, Any]:
    """Generate the article PNG, upload to Meta, send to all active QC users.

    Returns:
      {
        "template": <name>,
        "recipients": [{"role", "phone", "status", "error?"}...],
        "skipped":    [{"role", "reason"}...],
        "errors":     [str...],
      }
    """
    tname = _template_name()
    tlang = _template_lang()

    # ── Gate: WhatsApp disabled OR creds missing ─────────────────────────────
    # Full three-gate check (flag + access token + phone-number-id) so we don't
    # render/upload a PNG and fire a doomed request when creds are absent.
    if not _wa_enabled():
        return {
            "template": tname,
            "recipients": [],
            "skipped": [{"role": "*", "reason": "whatsapp_disabled"}],
            "errors": [],
        }

    # ── Resolve recipients ───────────────────────────────────────────────────
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.phone, r.role_name
              FROM auth_user u
              JOIN auth_role r ON r.role_id = u.role_id
             WHERE r.role_name = ANY($1)
               AND u.is_active
               AND u.phone IS NOT NULL
            """,
            ["qc_inspector", "qc_manager"],
        )

    if not rows:
        return {
            "template": tname,
            "recipients": [],
            "skipped": [
                {
                    "role": "qc_inspector/qc_manager",
                    "reason": "no_qc_recipients",
                }
            ],
            "errors": [],
        }

    # ── IST timestamp ────────────────────────────────────────────────────────
    ist = timezone(timedelta(hours=5, minutes=30))
    timestamp = datetime.now(ist).strftime("%d/%m/%Y %H:%M")

    # ── Generate PNG + upload ────────────────────────────────────────────────
    png_path: str | None = None
    recipients: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        try:
            png_path = render_article_png(article_names)
        except Exception as exc:
            logger.exception("send_intimation: PNG render failed")
            errors.append(f"PNG render failed: {exc}")
            return {
                "template": tname,
                "recipients": [],
                "skipped": [],
                "errors": errors,
            }

        try:
            media_id = await upload_media(png_path)
        except Exception as exc:
            logger.exception("send_intimation: media upload failed")
            errors.append(f"Media upload failed: {exc}")
            return {
                "template": tname,
                "recipients": [],
                "skipped": [],
                "errors": errors,
            }

        # ── Fan-out send ─────────────────────────────────────────────────────
        for row in rows:
            raw_phone: str = row["phone"]
            role_name: str = row["role_name"]

            norm = normalize_phone(raw_phone)
            to_wa = (norm or raw_phone).lstrip("+")

            try:
                await send_template(
                    to_wa,
                    media_id,
                    po_number=po_number,
                    invoice_no=invoice_no,
                    vendor_name=vendor_name,
                    vehicle_number=vehicle_number,
                    timestamp=timestamp,
                    template_name=tname,
                    lang=tlang,
                )
                recipients.append({"role": role_name, "phone": to_wa, "status": "sent"})
            except Exception as exc:
                logger.warning(
                    "send_intimation: send failed for %s (%s): %s",
                    to_wa, role_name, exc,
                )
                recipients.append(
                    {
                        "role": role_name,
                        "phone": to_wa,
                        "status": "failed",
                        "error": str(exc),
                    }
                )

    finally:
        # Always clean up the temp PNG regardless of success/failure.
        if png_path is not None:
            try:
                os.remove(png_path)
                logger.debug("send_intimation: removed temp PNG %s", png_path)
            except OSError as exc:
                logger.warning("send_intimation: could not remove temp PNG %s: %s", png_path, exc)

    return {
        "template": tname,
        "recipients": recipients,
        "skipped": [],
        "errors": errors,
    }
