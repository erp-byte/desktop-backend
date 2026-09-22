"""Tell store when the floor raises a requisition.

When a request is raised from a job card's Material allocation tab, every user
who holds the ``store_head`` role and could sign in and open it is told about it:

  * the account is active and its status is 'active' (not suspended/disabled),
    the same test validate_session applies;
  * store_head is among the user's EFFECTIVE roles, resolved like
    auth_service._effective_roles: auth_user_role is the source of truth, and the
    primary auth_user.role_id counts only for a user with no auth_user_role rows;
  * their granted places cover the request's warehouse and floor, by the rule the
    screens enforce (stock_take.place_scope): an empty allowed_warehouses /
    allowed_floors list means no limit on that axis.

Two channels, both best-effort:

  email     one plain-text message to all of them, through the production
            module's SMTP helper (mail_service._send, the fg_dispatch pattern).
            Off when SMTP_HOST is empty.
  WhatsApp  one message each on the shared WABA, using the Meta-approved UTILITY
            template floor_requisition_raised_store (English, positional). Its
            definition, read from the Graph API on 17 Sep 2026 (id 2646466059117649):

              header  "Material requested — Job card {{1}}"   (60 chars max in all)
              body    {{1}} requisition no      {{7}}  short on the floor
                      {{2}} job card            {{8}}  deliver to (place)
                      {{3}} FG                  {{9}}  requested by
                      {{4}} customer            {{10}} raised at
                      {{5}} material            {{11}} note
                      {{6}} quantity requested
              footer  "Candor Foods · Floor requisitions"
              buttons quick replies "Accept" (index 0) and "Hold" (index 1)

            Each button is sent with a payload "floor_req:<accept|hold>:<id>", so a
            tap names its request without a lookup table; services/wa_tap.py claims
            those taps first in the shared webhook and records the reply. A template
            (not free text) is required: store users have not messaged the business
            number, and Meta drops free text outside its 24-hour window (131047).
            Off when WHATSAPP_ENABLED is false or the WhatsApp credentials are unset.

The router schedules notify_store_of_raise AFTER the raise has committed, as a
background task. Under uvicorn (Dockerfile / Procfile / App Runner) it runs after
the response has been sent, so a slow mail server or Graph API cannot delay the
floor's request. Under the Mangum Lambda handler (Dockerfile.lambda) the
invocation only returns once background tasks finish, so there the response DOES
wait for the notice (bounded by the SMTP and per-phone WhatsApp timeouts).
Either way nothing here raises: every failure is logged and reported in the
returned dict, and the committed request is never undone.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Optional

import httpx

from app.config import Settings
from app.modules.auth.services.phone import normalize as normalize_phone
from app.modules.floor_requisition.services import requisition_service
from app.modules.production.services import mail_service
from app.modules.stock_take import place_scope

logger = logging.getLogger(__name__)

STORE_ROLES = ("store_head",)
STORES_PAGE = "/modules/stores/production-indents"

# The approved template, hard-coded by request (see the module docstring).
TEMPLATE_NAME = "floor_requisition_raised_store"
TEMPLATE_LANG = "en"
_HEADER_FIXED = "Material requested — Job card "      # 30 characters
_HEADER_MAX = 60                                        # Meta's limit for a text header
PAYLOAD_PREFIX = "floor_req"
ACCEPT, HOLD = "accept", "hold"                         # button index 0, 1
IST = timezone(timedelta(hours=5, minutes=30))

# One row per user. Role resolution mirrors auth_service._effective_roles and the
# account test mirrors validate_session (is_active AND status = 'active').
_RECIPIENTS_SQL = """
    SELECT u.user_id, u.full_name, u.email, u.phone, u.allowed_warehouses, u.allowed_floors
      FROM auth_user u
     WHERE u.is_active
       AND COALESCE(u.status, 'active') = 'active'
       AND (
             EXISTS (SELECT 1 FROM auth_user_role ur JOIN auth_role r ON r.role_id = ur.role_id
                      WHERE ur.user_id = u.user_id AND r.role_name = ANY($1::text[]))
          OR (NOT EXISTS (SELECT 1 FROM auth_user_role ur WHERE ur.user_id = u.user_id)
              AND EXISTS (SELECT 1 FROM auth_role r
                           WHERE r.role_id = u.role_id AND r.role_name = ANY($1::text[])))
           )
     ORDER BY u.full_name, u.user_id
"""


def _settings():
    """One read of the settings per notification (tests replace this)."""
    return Settings()


# ── who ──────────────────────────────────────────────────────────────────────

def covers_place(allowed_warehouses: Optional[Iterable[str]], allowed_floors: Optional[Iterable[str]],
                 warehouse: str, floor: str) -> bool:
    """Whether a user with these grants may open a request at warehouse/floor —
    place_scope's rule exactly, so notification and access never disagree."""
    granted_w, granted_f = place_scope.granted(SimpleNamespace(
        allowed_warehouses=list(allowed_warehouses or []), allowed_floors=list(allowed_floors or [])))
    wh = place_scope._normalise_warehouse(warehouse)
    fl = (floor or "").strip().upper()
    return (not granted_w or wh in granted_w) and (not granted_f or fl in granted_f)


async def store_role_holders(conn) -> list[dict[str, Any]]:
    """Every user who can sign in with store_head among their roles, one entry each."""
    rows = await conn.fetch(_RECIPIENTS_SQL, list(STORE_ROLES))
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["user_id"] in seen:
            continue
        seen.add(r["user_id"])
        out.append({"user_id": r["user_id"], "name": r["full_name"],
                    "email": (r["email"] or "").strip(), "phone": (r["phone"] or "").strip(),
                    "allowed_warehouses": r["allowed_warehouses"], "allowed_floors": r["allowed_floors"]})
    return out


def covering(holders: list[dict[str, Any]], warehouse: str, floor: str) -> list[dict[str, Any]]:
    """The holders whose granted places include this warehouse and floor."""
    return [h for h in holders
            if covers_place(h["allowed_warehouses"], h["allowed_floors"], warehouse, floor)]


# ── what ─────────────────────────────────────────────────────────────────────

def _qty(value: Any, unit: Optional[str]) -> str:
    if value is None:
        return "-"
    unit = unit or "kg"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return f"{value} {unit}"
    return f"{n:,.0f} {unit}" if unit == "pcs" else f"{n:,.3f} {unit}"


def _when(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %H:%M")


def _facts(req: dict[str, Any], card: Optional[dict[str, Any]]) -> dict[str, str]:
    unit = req.get("requested_unit") or "kg"
    product = "-"
    if card:
        product = card.get("fg_sku_name") or "-"
        if card.get("customer_name"):
            product = f"{product} - {card['customer_name']}"
    article = req.get("material_sku_name") or "-"
    if req.get("item_type"):
        article = f"{article} ({req['item_type']})"
    return {
        "number": f"#{req.get('requisition_id')}",
        "job_card": (card or {}).get("job_card_number") or f"#{req.get('job_card_id')}",
        "product": product,
        "place": f"{req.get('warehouse') or '-'} · {req.get('floor') or '-'}",
        "article": article,
        "requested": _qty(req.get("requested_qty"), unit),
        "required": _qty(req.get("required_qty"), req.get("required_unit") or unit),
        "available": _qty(req.get("available_qty"), req.get("available_unit") or unit),
        "shortage": _qty(req.get("shortage_qty"), req.get("shortage_unit") or unit),
        "raised_by": req.get("raised_by") or "-",
        "raised_at": _when(req.get("raised_at")),
        "note": (req.get("note") or "").strip(),
    }


def compose_email(req: dict[str, Any], card: Optional[dict[str, Any]], web_url: str) -> tuple[str, str]:
    """(subject, plain-text body) for the store users."""
    f = _facts(req, card)
    subject = f"Material request {f['number']} - {req.get('material_sku_name') or 'article'} for {f['job_card']}"
    lines = [
        "The production floor has requested material from store.",
        "",
        f"Request no.   : {f['number']}",
        f"Raised by     : {f['raised_by']}, {f['raised_at']}",
        f"Job card      : {f['job_card']}",
        f"Product       : {f['product']}",
        f"Place         : {f['place']}",
        f"Article       : {f['article']}",
        f"Requested     : {f['requested']}",
        f"Required      : {f['required']}",
        f"Fresh stock   : {f['available']}",
        f"Shortage      : {f['shortage']}",
    ]
    if f["note"]:
        lines.append(f"Note          : {f['note']}")
    lines += [
        "",
        "Issue or cancel it in Stores > Production Indents:",
        f"{(web_url or '').rstrip('/')}{STORES_PAGE}",
    ]
    return subject, "\n".join(lines)


_SPACES = re.compile(r"\s+")

# Meta refuses a template message whose body, with the variables filled in, is
# longer than 1024 characters. The approved body's fixed text is 265 characters,
# so the eleven variables share a budget of 700 (965 at most).
_BODY_BUDGET = (("number", 12), ("job_card", 60), ("fg", 110), ("customer", 80), ("material", 120),
                ("requested", 30), ("shortage", 30), ("place", 78), ("raised_by", 60),
                ("raised_at", 20), ("note", 100))


def _param(text: str, limit: int) -> str:
    """A WhatsApp template variable: Meta refuses empty values, new lines, tabs and
    runs of spaces, so collapse all whitespace, cap the length and never send blank."""
    t = _SPACES.sub(" ", str(text or "")).strip()
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t or "-"


def button_payload(action: str, requisition_id: Any) -> str:
    """What a quick-reply tap sends back: 'floor_req:accept:87654321'."""
    return f"{PAYLOAD_PREFIX}:{action}:{requisition_id}"


def header_param(req: dict[str, Any], card: Optional[dict[str, Any]]) -> str:
    """The header's job card. The whole header may be 60 characters, so a job card
    number that does not fit falls back to the card's id rather than being cut."""
    room = _HEADER_MAX - len(_HEADER_FIXED)
    number = _param((card or {}).get("job_card_number") or "", room)
    if number != "-" and not number.endswith("…"):
        return number
    return _param(str(req.get("job_card_id") or "-"), room)


def body_params(req: dict[str, Any], card: Optional[dict[str, Any]]) -> list[str]:
    """The eleven positional body variables, in the template's order."""
    f = _facts(req, card)
    values = {
        "number": str(req.get("requisition_id") or "-"),
        "job_card": f["job_card"].lstrip("#"),
        "fg": (card or {}).get("fg_sku_name") or "-",
        "customer": (card or {}).get("customer_name") or "-",
        "material": f["article"],
        "requested": f["requested"],
        "shortage": f["shortage"],
        "place": f["place"],
        "raised_by": f["raised_by"],
        "raised_at": f["raised_at"],
        "note": f["note"] or "-",
    }
    return [_param(values[key], limit) for key, limit in _BODY_BUDGET]


def template_message(to: str, req: dict[str, Any], card: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The Cloud API request body for one recipient."""
    rid = req.get("requisition_id")
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": TEMPLATE_LANG},
            "components": [
                {"type": "header", "parameters": [{"type": "text", "text": header_param(req, card)}]},
                {"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params(req, card)]},
                {"type": "button", "sub_type": "quick_reply", "index": "0",
                 "parameters": [{"type": "payload", "payload": button_payload(ACCEPT, rid)}]},
                {"type": "button", "sub_type": "quick_reply", "index": "1",
                 "parameters": [{"type": "payload", "payload": button_payload(HOLD, rid)}]},
            ],
        },
    }


# ── send ─────────────────────────────────────────────────────────────────────

async def _send_whatsapp_template(settings, message: dict[str, Any]) -> None:
    base = (settings.WHATSAPP_GRAPH_BASE or "https://graph.facebook.com/v21.0").rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{base}/{settings.WHATSAPP_PHONE_NUMBER_ID.strip()}/messages",
            headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN.strip()}",
                     "Content-Type": "application/json"},
            json=message,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} - {resp.text[:200]}")


def _whatsapp_off_reason(settings) -> Optional[str]:
    if not settings.WHATSAPP_ENABLED:
        return "whatsapp_disabled"
    if not str(settings.WHATSAPP_ACCESS_TOKEN or "").strip() or not str(settings.WHATSAPP_PHONE_NUMBER_ID or "").strip():
        return "whatsapp_credentials_missing"
    return None


async def notify_store_of_raise(pool, requisition: dict[str, Any]) -> dict[str, Any]:
    """Email + WhatsApp the store users for a just-committed requisition. Never raises."""
    rid = requisition.get("requisition_id")
    report: dict[str, Any] = {"requisition_id": rid, "recipients": [],
                              "email": {"to": [], "skipped": None},
                              "whatsapp": {"sent": 0, "failed": [], "skipped": None},
                              "error": None}
    try:
        # Read what is needed, then give the connection back before any network I/O.
        async with pool.acquire() as conn:
            holders = await store_role_holders(conn)
            cards = await requisition_service.job_cards_by_id(conn, [requisition.get("job_card_id")])
        card = cards.get(requisition.get("job_card_id"))
        people = covering(holders, requisition.get("warehouse") or "", requisition.get("floor") or "")
        report["recipients"] = [p["name"] for p in people]
        if not holders:
            # Not a place problem: nobody can receive store notices at all (the role is
            # missing on this database, or no active user holds it). Say so loudly.
            report["email"]["skipped"] = report["whatsapp"]["skipped"] = "no_active_store_head_users"
            logger.warning("[floor-req] #%s: no active user holds the store_head role - nobody notified", rid)
            return report
        if not people:
            report["email"]["skipped"] = report["whatsapp"]["skipped"] = "no_store_user_for_this_place"
            logger.info("[floor-req] #%s: %d store_head user(s), none covering %s / %s - nobody notified",
                        rid, len(holders), requisition.get("warehouse"), requisition.get("floor"))
            return report

        settings = _settings()

        emails = list(dict.fromkeys(p["email"] for p in people if p["email"]))
        if not emails:
            report["email"]["skipped"] = "no_email_on_store_users"
        elif not str(settings.SMTP_HOST or "").strip():
            report["email"]["skipped"] = "smtp_not_configured"
        else:
            subject, body = compose_email(requisition, card, settings.WEB_APP_URL)
            try:
                # _send is blocking; run it off the event loop. It swallows SMTP errors
                # itself and returns whether the server accepted the message.
                accepted = await asyncio.to_thread(
                    mail_service._send, subject, body, emails, [],
                    entity_type="FloorRequisition", entity_id=str(rid), event="raised",
                    status="raised", actor=requisition.get("raised_by"))
                if accepted:
                    report["email"]["to"] = emails
                else:
                    report["email"]["skipped"] = "send_failed"
            except Exception as exc:  # noqa: BLE001 - notification must never fail the request
                logger.exception("[floor-req] #%s: email to store failed", rid)
                report["email"]["skipped"] = f"error: {exc}"

        off = _whatsapp_off_reason(settings)
        if off:
            report["whatsapp"]["skipped"] = off
        else:
            phones = list(dict.fromkeys(
                (normalize_phone(p["phone"]) or p["phone"]).lstrip("+") for p in people if p["phone"]))
            if not phones:
                report["whatsapp"]["skipped"] = "no_phone_on_store_users"
            for to in phones:
                try:
                    await _send_whatsapp_template(settings, template_message(to, requisition, card))
                    report["whatsapp"]["sent"] += 1
                except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
                    logger.warning("[floor-req] #%s: WhatsApp to %s failed: %s", rid, to, exc)
                    report["whatsapp"]["failed"].append({"phone": to, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("[floor-req] #%s: notifying store failed", rid)
        report["error"] = str(exc)
    logger.info("[floor-req] #%s store notice: %s", rid, report)
    return report
