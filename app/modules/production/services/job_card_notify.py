"""Tell the floor a job card has been created.

Every user who holds the ``floor_manager`` role and could sign in and open the
card is told about it, once per card:

  * the account is active and its status is 'active' (not suspended/disabled),
    the same test validate_session applies;
  * floor_manager is among the user's EFFECTIVE roles, resolved like
    auth_service._effective_roles: auth_user_role is the source of truth, and the
    primary auth_user.role_id counts only for a user with no auth_user_role rows;
  * their granted places cover the card's place, by the rule the screens enforce
    (stock_take.place_scope): the card's FACTORY is the warehouse axis (matched
    hyphen-blind, so a grant of 'A185' covers a card at 'A-185') and its FLOOR is
    the floor axis. An empty grant list means no limit on that axis.

The notice goes out on CREATION — including for a card that is still locked
waiting for its previous stage, which is what the Note line is for.

Two channels, both best-effort:

  email     one plain-text message per card to the floor managers covering it,
            through the production module's SMTP helper (mail_service._send).
            Off when SMTP_HOST is empty.
  WhatsApp  one message each on the shared WABA, using the UTILITY template
            job_card_created_floor (English, positional), submitted to Meta on
            23 Sep 2026 (id 892382947145980) and IN REVIEW there:

              header  "New job card — {{1}}"        (60 chars max in all)
              body    {{1}} job card        {{6}}  place (factory · floor)
                      {{2}} product         {{7}}  input the stage runs on
                      {{3}} customer        {{8}}  created by …
                      {{4}} quantity        {{9}}  … at (time, IST)
                      {{5}} step            {{10}} note
              footer  "Candor Foods · Production"
              buttons none

            The body line reads "Created by: {{8}} at {{9}}" — the template
            supplies the " at ", so {{8}} carries the name AND the date
            ("Ravi K on 23 Sep 2026") and {{9}} carries the time alone
            ("10:42"): "Created by: Ravi K on 23 Sep 2026 at 10:42". With no
            creator known {{8}} is "-", never a bare date.

            A template (not free text) is required: floor managers have not
            messaged the business number, and Meta drops free text outside its
            24-hour window (131047). While the template is still in review a send
            can legitimately come back as a template error — it is logged once
            per message and never retried in a loop. Off when WHATSAPP_ENABLED is
            false or the WhatsApp credentials are unset.

Everything identical to the store notice (recipient query, place rule, variable
sanitiser, Graph call) is shared through services/wa_notify.py rather than
copied — the two notifiers must not drift apart.

Each creating route schedules notify_floor_of_new_job_cards AFTER its
transaction has committed, as a background task, passing only the ids of the
cards it created. The notice re-reads those cards on its own connection, so
nothing is held across the commit, and it never raises: every failure is logged
and reported in the returned dict.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app.modules.auth.services.phone import normalize as normalize_phone
from app.modules.production.services import mail_service
from app.modules.production.services.wa_notify import (
    IST, RECIPIENTS_SQL as _RECIPIENTS_SQL, assigned, assigned_to_place,
    current_settings as _settings, param as _param, role_holders,
    send_template as _send_whatsapp_template, whatsapp_off_reason as _whatsapp_off_reason,
)

logger = logging.getLogger(__name__)

FLOOR_ROLES = ("floor_manager",)
# The web app's job card list, with one card at JOB_CARDS_PAGE/{job_card_id}.
# There is no catch-all route there, so a path that only looks right 404s.
JOB_CARDS_PAGE = "/modules/job-card"

# The approved template, hard-coded by request (see the module docstring).
TEMPLATE_NAME = "job_card_created_floor"
TEMPLATE_LANG = "en"
_HEADER_FIXED = "New job card — "                       # 15 characters
_HEADER_MAX = 60                                        # Meta's limit for a text header

AWAITING_PREVIOUS = "awaiting_previous_stage"
WAITING_NOTE = "Waiting for the previous stage"

# Everything the notice says about a card, read back after the commit.
_CARDS_SQL = """
    SELECT job_card_id, job_card_number, fg_sku_name, customer_name,
           planned_qty_kg, planned_qty_units, step_number, process_name,
           factory, floor, input_kind, input_code,
           is_locked, locked_reason, status, created_at
      FROM job_card_v2
     WHERE job_card_id = ANY($1::bigint[])
"""


# ── who ──────────────────────────────────────────────────────────────────────

async def floor_role_holders(conn) -> list[dict[str, Any]]:
    """Every user who can sign in with floor_manager among their roles."""
    return await role_holders(conn, FLOOR_ROLES)


# ── what ─────────────────────────────────────────────────────────────────────

async def cards_by_id(conn, ids: list[int]) -> dict[int, dict[str, Any]]:
    """job_card_id -> the row the notice reads. A card cancelled or soft-deleted
    between the commit and the notice still reads here: it was created."""
    rows = await conn.fetch(_CARDS_SQL, ids)
    return {r["job_card_id"]: dict(r) for r in rows}


def _amount(value: Any, unit: str, places: int) -> str:
    try:
        return f"{float(value):,.{places}f} {unit}"
    except (TypeError, ValueError):
        return f"{value} {unit}"


def _qty(kg: Any, units: Any) -> str:
    """'64.000 kg · 640 units', or the kg alone when the card counts no units."""
    parts = [_amount(kg, "kg", 3)] if kg is not None else []
    if units:
        parts.append(_amount(units, "units", 0))
    return " · ".join(parts)


def _join(*parts: Any) -> str:
    """'1 · Flavouring + Mixing' — only the pieces that are actually there."""
    return " · ".join(str(p).strip() for p in parts if p is not None and str(p).strip())


def _input(card: dict[str, Any]) -> str:
    """What the stage runs on: the SFG code it consumes when it has one, else the
    kind of material, else raw material straight from store."""
    return (str(card.get("input_code") or "").strip()
            or str(card.get("input_kind") or "").strip()
            or "RM from store")


def _ist(value: Any) -> Optional[datetime]:
    if not value:
        return None
    dt = value if isinstance(value, datetime) else None
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def _creator(card: dict[str, Any], when: Optional[datetime]) -> str:
    """{{8}}: 'Ravi K on 23 Sep 2026'. The template writes ' at ' before {{9}}, so
    the date rides with the name and {{9}} carries the time alone.

    The slot names a person, so with nobody known it is "-": a date on its own
    would render as "Created by: 23 Sep 2026 at 10:42"."""
    who = str(card.get("created_by") or "").strip()
    if not who:
        return "-"
    return f"{who} on {when:%d %b %Y}" if when else who


def _note(card: dict[str, Any]) -> str:
    """Why the card cannot start yet, in plain words."""
    locked = bool(card.get("is_locked")) or card.get("status") == "locked"
    reason = str(card.get("locked_reason") or "").strip()
    if not locked or not reason:
        return "-"
    return WAITING_NOTE if reason == AWAITING_PREVIOUS else reason


def _facts(card: dict[str, Any]) -> dict[str, str]:
    when = _ist(card.get("created_at"))
    return {
        # The card's own 8-digit id, not job_card_number: the number spells out the
        # whole chain (PLAN-71969018-L71969608-S2-B2), which is more than the floor
        # needs and does not match the id the job card page is opened by.
        "job_card": str(card.get("job_card_id") or "-"),
        "product": card.get("fg_sku_name") or "-",
        "customer": card.get("customer_name") or "-",
        "quantity": _qty(card.get("planned_qty_kg"), card.get("planned_qty_units")),
        "step": _join(card.get("step_number"), card.get("process_name")),
        "place": _join(card.get("factory"), card.get("floor")),
        "input": _input(card),
        "created_by": _creator(card, when),
        "created_at": f"{when:%H:%M}" if when else "-",
        "note": _note(card),
    }


def card_link(card: dict[str, Any], web_url: str) -> str:
    """The web page for this card - JOB_CARDS_PAGE/{job_card_id}. A card with no
    id at all falls back to the list."""
    base = (web_url or "").rstrip("/")
    jid = card.get("job_card_id")
    return f"{base}{JOB_CARDS_PAGE}/{jid}" if jid else f"{base}{JOB_CARDS_PAGE}"


def compose_email(card: dict[str, Any], web_url: str) -> tuple[str, str]:
    """(subject, plain-text body) for the floor managers of one card."""
    f = _facts(card)
    subject = f"New job card {f['job_card']} - {f['product']} for {f['place']}"
    # With no creator known the line gives the time alone, not "- at 10:42".
    created = (f"Created by : {f['created_by']} at {f['created_at']}" if f["created_by"] != "-"
               else f"Created at : {f['created_at']}")
    lines = [
        "A job card has been created for your floor.",
        "",
        f"Job card   : {f['job_card']}",
        f"Product    : {f['product']}",
        f"Customer   : {f['customer']}",
        f"Quantity   : {f['quantity']}",
        f"Step       : {f['step']}",
        f"Place      : {f['place']}",
        f"Input      : {f['input']}",
        created,
    ]
    if f["note"] != "-":
        lines.append(f"Note       : {f['note']}")
    lines += [
        "",
        "Open it in Production > Job Cards:",
        card_link(card, web_url),
    ]
    return subject, "\n".join(lines)


# Meta refuses a template message whose body, with the variables filled in, is
# longer than 1024 characters. The approved body's fixed text is 164 characters,
# so the ten variables share a budget of 710 (874 at most).
_BODY_BUDGET = (("job_card", 60), ("product", 110), ("customer", 80), ("quantity", 40),
                ("step", 80), ("place", 60), ("input", 60), ("created_by", 90),
                ("created_at", 10), ("note", 120))


def header_param(card: dict[str, Any]) -> str:
    """The header's job card: the 8-digit id, the same value the body's {{1}} and
    the card's own page use. It cannot outgrow the header's 60 characters, but it
    is still measured against them rather than trusted."""
    room = _HEADER_MAX - len(_HEADER_FIXED)
    return _param(str(card.get("job_card_id") or "-"), room)


def body_params(card: dict[str, Any]) -> list[str]:
    """The ten positional body variables, in the template's order."""
    f = _facts(card)
    values = dict(f, job_card=f["job_card"].lstrip("#"))
    return [_param(values[key], limit) for key, limit in _BODY_BUDGET]


def template_message(to: str, card: dict[str, Any]) -> dict[str, Any]:
    """The Cloud API request body for one recipient. No button component: the
    approved template declares none, and Meta refuses components it has not."""
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": TEMPLATE_LANG},
            "components": [
                {"type": "header", "parameters": [{"type": "text", "text": header_param(card)}]},
                {"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params(card)]},
            ],
        },
    }


# ── send ─────────────────────────────────────────────────────────────────────

def _ids(job_card_ids: Optional[Iterable[Any]]) -> list[int]:
    """The ids worth notifying: numeric, in order, each one only once — a card
    created and then replaced inside one request must not be told about twice."""
    out: list[int] = []
    for i in job_card_ids or []:
        try:
            out.append(int(i))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


async def _notify_one(settings, holders: list[dict[str, Any]], card: dict[str, Any]) -> dict[str, Any]:
    """Email + WhatsApp one card's floor managers. Never raises."""
    jid = card.get("job_card_id")
    entry: dict[str, Any] = {"job_card_id": jid, "job_card_number": card.get("job_card_number"),
                             "recipients": [], "email": {"to": [], "skipped": None},
                             "whatsapp": {"sent": 0, "failed": [], "skipped": None}}
    # Only the floor's OWN manager(s): assigned names both axes in the recipient's
    # grants, so an unrestricted account is not told about every card (wa_notify).
    people = assigned(holders, card.get("factory") or "", card.get("floor") or "")
    entry["recipients"] = [p["name"] for p in people]
    if not people:
        entry["email"]["skipped"] = entry["whatsapp"]["skipped"] = "no_floor_manager_for_this_place"
        logger.info("[job-card] %s: %d floor_manager user(s), none assigned to %s / %s - nobody notified",
                    jid, len(holders), card.get("factory"), card.get("floor"))
        return entry

    emails = list(dict.fromkeys(p["email"] for p in people if p["email"]))
    if not emails:
        entry["email"]["skipped"] = "no_email_on_floor_managers"
    elif not str(settings.SMTP_HOST or "").strip():
        entry["email"]["skipped"] = "smtp_not_configured"
    else:
        subject, body = compose_email(card, settings.WEB_APP_URL)
        try:
            # _send is blocking; run it off the event loop. It swallows SMTP errors
            # itself and returns whether the server accepted the message.
            accepted = await asyncio.to_thread(
                mail_service._send, subject, body, emails, [],
                entity_type="JobCard", entity_id=str(jid), event="created",
                status=card.get("status"), actor=card.get("created_by"))
            if accepted:
                entry["email"]["to"] = emails
            else:
                entry["email"]["skipped"] = "send_failed"
        except Exception as exc:  # noqa: BLE001 - notification must never fail the request
            logger.exception("[job-card] %s: email to the floor failed", jid)
            entry["email"]["skipped"] = f"error: {exc}"

    off = _whatsapp_off_reason(settings)
    if off:
        entry["whatsapp"]["skipped"] = off
        return entry
    phones = list(dict.fromkeys(
        (normalize_phone(p["phone"]) or p["phone"]).lstrip("+") for p in people if p["phone"]))
    if not phones:
        entry["whatsapp"]["skipped"] = "no_phone_on_floor_managers"
    for to in phones:
        try:
            await _send_whatsapp_template(settings, template_message(to, card))
            entry["whatsapp"]["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
            # While the template is in review a rejection is expected; log it and
            # move on rather than retrying the same refused message.
            logger.warning("[job-card] %s: WhatsApp to %s failed: %s", jid, to, exc)
            entry["whatsapp"]["failed"].append({"phone": to, "error": str(exc)})
    return entry


async def notify_floor_of_new_job_cards(pool, job_card_ids: Optional[Iterable[Any]],
                                        created_by: Optional[str] = None) -> dict[str, Any]:
    """Email + WhatsApp the floor managers of every just-committed job card.

    Takes ids, not rows: the creating transaction has already committed and this
    reads what it needs on its own connection. Never raises."""
    ids = _ids(job_card_ids)
    report: dict[str, Any] = {"job_card_ids": ids, "cards": [], "missing": [],
                              "skipped": None, "error": None}
    if not ids:
        return report
    try:
        # Read what is needed, then give the connection back before any network I/O.
        async with pool.acquire() as conn:
            holders = await floor_role_holders(conn)
            cards = await cards_by_id(conn, ids)
        report["missing"] = [i for i in ids if i not in cards]
        if report["missing"]:
            logger.warning("[job-card] job card(s) %s are not there to notify about", report["missing"])
        if not holders:
            # Not a place problem: nobody can receive floor notices at all (the role
            # is missing on this database, or no active user holds it). Say so loudly.
            report["skipped"] = "no_active_floor_manager_users"
            logger.warning("[job-card] no active user holds the floor_manager role - "
                           "nobody notified about %s", ids)
            return report
        settings = _settings()
        for jid in ids:
            card = cards.get(jid)
            if card is not None:
                report["cards"].append(await _notify_one(settings, holders,
                                                         {**card, "created_by": created_by}))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[job-card] notifying the floor of %s failed", ids)
        report["error"] = str(exc)
    logger.info("[job-card] new job card notice: %s", report)
    return report
