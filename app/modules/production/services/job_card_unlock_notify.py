"""Tell the floor a job card has been UNLOCKED and can start.

A card in a chain is created locked ("waiting for the previous stage") and is
released when the previous stage hands it material — a batch close that
auto-dispatches, Dispatch to next, a merged-process group dispatch — or by a
force-unlock or a chain edit. Each of those routes passes the id of the card it
released: always the NEXT card, the one that can now start, never the card whose
batch closed or whose material went out. This module tells that card's people.

Who is told, once each, never the person who unlocked it:

  * the floor managers ASSIGNED to the unlocked card's factory and floor — the
    same rule as the creation notice (wa_notify.assigned);
  * the team leader named on the card (job_card_v2.assigned_to_team_leader), when
    that free-text name is exactly one active user — two accounts with the same
    name are not guessed between;
  * when the card has NO floor, nobody can be its floor manager, so the notice
    goes instead to the floor managers of the previous stage's floor and to the
    planner who made the plan, with a note asking for a floor to be set.

Two channels, both best-effort, like the creation notice: one plain-text email
per card (off when SMTP_HOST is empty) and one WhatsApp per recipient using the
UTILITY template job_card_unlocked_floor (English, positional):

    header  Job card ready to start — {{1}}
    body    Job card {{1}} is unlocked and ready to start.
            Product: {{2}}
            Customer: {{3}}
            Step: {{4}}
            Place: {{5}}
            Material received: {{6}} from {{7}}
            Unlocked by: {{8}} at {{9}}
            Note: {{10}}
            Please start it from Production > Job Cards.
    footer  Candor Foods · Production
    button  optional "Open job card" URL button, <web app>/modules/job-card/{{1}}

The body is submitted to Meta exactly as BODY_TEXT below. Set OPEN_BUTTON to
match what was submitted: Meta refuses a component the approved template does
not declare, and a missing one the template does.

Each unlocking route schedules notify_floor_of_unlocked_job_cards AFTER its
transaction has committed, as a background task. The notice re-reads the cards
on its own connection and never raises: every failure is logged and reported.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app.modules.auth.services.phone import normalize as normalize_phone
from app.modules.production.services import mail_service
from app.modules.production.services.job_card_notify import (
    _amount, _join, card_link, floor_role_holders,
)
from app.modules.production.services.wa_notify import (
    IST, assigned, current_settings as _settings, param as _param,
    send_template as _send_whatsapp_template, whatsapp_off_reason as _whatsapp_off_reason,
)

logger = logging.getLogger(__name__)

TEMPLATE_NAME = "job_card_unlocked_floor"
TEMPLATE_LANG = "en"
# True only if the template was submitted with the "Open job card" URL button.
OPEN_BUTTON = False

HEADER_TEXT = "Job card ready to start — {{1}}"
BODY_TEXT = (
    "Job card {{1}} is unlocked and ready to start.\n"
    "Product: {{2}}\n"
    "Customer: {{3}}\n"
    "Step: {{4}}\n"
    "Place: {{5}}\n"
    "Material received: {{6}} from {{7}}\n"
    "Unlocked by: {{8}} at {{9}}\n"
    "Note: {{10}}\n"
    "Please start it from Production > Job Cards."
)
NO_FLOOR_NOTE = "Floor not set on this card - please assign one"

_VAR = re.compile(r"\{\{\d+\}\}")
_HEADER_MAX = 60                                        # Meta's limit for a text header
_HEADER_ROOM = _HEADER_MAX - len(_VAR.sub("", HEADER_TEXT))
# Meta refuses a body longer than 1024 characters once the variables are filled
# in; the ten variables share what the fixed text leaves.
_BODY_BUDGET = (("job_card", 60), ("product", 110), ("customer", 80), ("step", 80),
                ("place", 60), ("received", 40), ("from", 90), ("unlocked_by", 90),
                ("unlocked_at", 10), ("note", 200))
assert len(_VAR.sub("", BODY_TEXT)) + sum(n for _, n in _BODY_BUDGET) <= 1024

_CARDS_SQL = """
    SELECT job_card_id, job_card_number, fg_sku_name, customer_name,
           step_number, process_name, factory, floor, status,
           prev_job_card_id, assigned_to_team_leader, plan_id
      FROM job_card_v2
     WHERE job_card_id = ANY($1::bigint[])
"""
_PLANS_SQL = """
    SELECT plan_id, created_by
      FROM production_plan_v2
     WHERE plan_id = ANY($1::bigint[])
"""
# Users by name (team leader, planner) — the same account test as wa_notify's
# RECIPIENTS_SQL, so nobody who could not sign in is messaged.
_NAMED_USERS_SQL = r"""
    SELECT u.user_id, u.full_name, u.email, u.phone, u.allowed_warehouses, u.allowed_floors
      FROM auth_user u
     WHERE u.is_active
       AND COALESCE(u.status, 'active') = 'active'
       AND lower(regexp_replace(btrim(u.full_name), '\s+', ' ', 'g')) = ANY($1::text[])
     ORDER BY u.full_name, u.user_id
"""


# ── who ──────────────────────────────────────────────────────────────────────

def _key(name: Any) -> str:
    """A name as it is matched: whitespace collapsed, case ignored."""
    return " ".join(str(name or "").split()).lower()


async def _named_users(conn, names: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    keys = sorted({_key(n) for n in names if _key(n)})
    if not keys:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in await conn.fetch(_NAMED_USERS_SQL, keys):
        out.setdefault(_key(r["full_name"]), []).append({
            "user_id": r["user_id"], "name": r["full_name"],
            "email": (r["email"] or "").strip(), "phone": (r["phone"] or "").strip(),
            "allowed_warehouses": r["allowed_warehouses"], "allowed_floors": r["allowed_floors"]})
    return out


def _one(named: dict[str, list[dict[str, Any]]], name: Any) -> Optional[dict[str, Any]]:
    """The single user this name belongs to; None when it is nobody or ambiguous."""
    matches = named.get(_key(name), [])
    return matches[0] if len(matches) == 1 else None


def recipients(card: dict[str, Any], source: Optional[dict[str, Any]], holders: list[dict[str, Any]],
               named: dict[str, list[dict[str, Any]]], planner: Any,
               actor_user_id: Optional[int]) -> list[dict[str, Any]]:
    """The unlocked card's floor managers and team leader — or, for a card with no
    floor, the previous stage's floor managers and the planner. Once each, never
    the person who unlocked it."""
    floor = str(card.get("floor") or "").strip()
    people: list[dict[str, Any]] = []
    if floor:
        people += assigned(holders, card.get("factory") or "", floor)
    elif source and str(source.get("floor") or "").strip():
        people += assigned(holders, source.get("factory") or "", source.get("floor"))
    for person in (_one(named, card.get("assigned_to_team_leader")),
                   None if floor else _one(named, planner)):
        if person:
            people.append(person)
    out, seen = [], set()
    for p in people:
        if p["user_id"] in seen or (actor_user_id is not None and p["user_id"] == actor_user_id):
            continue
        seen.add(p["user_id"])
        out.append(p)
    return out


# ── what ─────────────────────────────────────────────────────────────────────

def facts(card: dict[str, Any], source: Optional[dict[str, Any]], unlock: dict[str, Any],
          unlocked_by: Any, when: Optional[datetime]) -> dict[str, str]:
    """Everything the notice says, about the UNLOCKED card."""
    at = when.astimezone(IST) if when else None
    floor = str(card.get("floor") or "").strip()
    from_id = unlock.get("from_job_card_id")
    shown_from = "-"
    if from_id:
        process = source.get("process_name") if source and source.get("job_card_id") == from_id else None
        shown_from = _join(from_id, process)
    qty = unlock.get("qty_kg")
    notes = []
    if unlock.get("how") == "force_unlock":
        reason = str(unlock.get("reason") or "").strip()
        notes.append(f"Force-unlocked: {reason}" if reason else "Force-unlocked")
    if not floor:
        notes.append(NO_FLOOR_NOTE)
    who = str(unlocked_by or "").strip()
    return {
        "job_card": str(card.get("job_card_id") or "-"),
        "product": card.get("fg_sku_name") or "-",
        "customer": card.get("customer_name") or "-",
        "step": _join(card.get("step_number"), card.get("process_name")),
        "place": _join(card.get("factory"), floor or "floor not set"),
        "received": _amount(qty, "kg", 3) if qty else "-",
        "from": shown_from,
        "unlocked_by": (f"{who} on {at:%d %b %Y}" if at else who) if who else "-",
        "unlocked_at": f"{at:%H:%M}" if at else "-",
        "note": "; ".join(notes) or "-",
    }


def template_message(to: str, f: dict[str, str]) -> dict[str, Any]:
    """The Cloud API request body for one recipient."""
    components: list[dict[str, Any]] = [
        {"type": "header", "parameters": [{"type": "text", "text": _param(f["job_card"], _HEADER_ROOM)}]},
        {"type": "body", "parameters": [{"type": "text", "text": _param(f[key], limit)}
                                        for key, limit in _BODY_BUDGET]},
    ]
    if OPEN_BUTTON:
        components.append({"type": "button", "sub_type": "url", "index": "0",
                           "parameters": [{"type": "text", "text": _param(f["job_card"], 60)}]})
    return {"messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": TEMPLATE_NAME, "language": {"code": TEMPLATE_LANG},
                         "components": components}}


def compose_email(card: dict[str, Any], f: dict[str, str], web_url: str) -> tuple[str, str]:
    """(subject, plain-text body) for the people of one unlocked card."""
    subject = f"Job card {f['job_card']} unlocked - {card.get('process_name') or f['step']} at {f['place']}"
    lines = [
        "A job card is unlocked and ready to start.",
        "",
        f"Job card          : {f['job_card']}",
        f"Product           : {f['product']}",
        f"Customer          : {f['customer']}",
        f"Step              : {f['step']}",
        f"Place             : {f['place']}",
    ]
    if f["received"] != "-" or f["from"] != "-":
        received = f["received"] if f["from"] == "-" else f"{f['received']} from {f['from']}"
        lines.append(f"Material received : {received}")
    if f["unlocked_by"] != "-":
        lines.append(f"Unlocked by       : {f['unlocked_by']} at {f['unlocked_at']}")
    if f["note"] != "-":
        lines.append(f"Note              : {f['note']}")
    lines += ["", "Open it in Production > Job Cards:", card_link(card, web_url)]
    return subject, "\n".join(lines)


# ── send ─────────────────────────────────────────────────────────────────────

def _unlocks(raw: Optional[Iterable[Any]]) -> list[dict[str, Any]]:
    """One entry per unlocked card, in order; entries without a usable id dropped."""
    out: dict[int, dict[str, Any]] = {}
    for u in raw or []:
        try:
            jid = int((u or {}).get("job_card_id"))
        except (TypeError, ValueError):
            continue
        out.setdefault(jid, {**u, "job_card_id": jid})
    return list(out.values())


async def _notify_one(settings, people: list[dict[str, Any]], card: dict[str, Any],
                      f: dict[str, str], unlocked_by: Any) -> dict[str, Any]:
    """Email + WhatsApp one unlocked card's people. Never raises."""
    jid = card.get("job_card_id")
    entry: dict[str, Any] = {"recipients": [p["name"] for p in people],
                             "email": {"to": [], "skipped": None},
                             "whatsapp": {"sent": 0, "failed": [], "skipped": None}}
    if not people:
        entry["email"]["skipped"] = entry["whatsapp"]["skipped"] = "nobody_to_tell"
        logger.info("[job-card-unlock] %s: no floor manager, team leader or planner to tell", jid)
        return entry

    emails = list(dict.fromkeys(p["email"] for p in people if p["email"]))
    if not emails:
        entry["email"]["skipped"] = "no_email_on_recipients"
    elif not str(settings.SMTP_HOST or "").strip():
        entry["email"]["skipped"] = "smtp_not_configured"
    else:
        subject, body = compose_email(card, f, settings.WEB_APP_URL)
        try:
            accepted = await asyncio.to_thread(
                mail_service._send, subject, body, emails, [],
                entity_type="JobCard", entity_id=str(jid), event="unlocked",
                status=card.get("status"), actor=unlocked_by)
            if accepted:
                entry["email"]["to"] = emails
            else:
                entry["email"]["skipped"] = "send_failed"
        except Exception as exc:  # noqa: BLE001 - notification must never fail the request
            logger.exception("[job-card-unlock] %s: email failed", jid)
            entry["email"]["skipped"] = f"error: {exc}"

    off = _whatsapp_off_reason(settings)
    if off:
        entry["whatsapp"]["skipped"] = off
        return entry
    phones = list(dict.fromkeys(
        (normalize_phone(p["phone"]) or p["phone"]).lstrip("+") for p in people if p["phone"]))
    if not phones:
        entry["whatsapp"]["skipped"] = "no_phone_on_recipients"
    for to in phones:
        try:
            await _send_whatsapp_template(settings, template_message(to, f))
            entry["whatsapp"]["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
            logger.warning("[job-card-unlock] %s: WhatsApp to %s failed: %s", jid, to, exc)
            entry["whatsapp"]["failed"].append({"phone": to, "error": str(exc)})
    return entry


async def notify_floor_of_unlocked_job_cards(pool, unlocks: Optional[Iterable[Any]],
                                             unlocked_by: Optional[str] = None,
                                             actor_user_id: Optional[int] = None,
                                             now: Optional[datetime] = None) -> dict[str, Any]:
    """Email + WhatsApp the people of every card a just-committed request unlocked.

    `unlocks` holds one dict per released card: job_card_id (the card that can now
    start), and optionally from_job_card_id + qty_kg (the dispatch that freed it),
    how ('dispatch' | 'force_unlock' | 'chain_edit') and reason. Never raises."""
    items = _unlocks(unlocks)
    report: dict[str, Any] = {"job_card_ids": [u["job_card_id"] for u in items],
                              "cards": [], "missing": [], "error": None}
    if not items:
        return report
    when = now or datetime.now(timezone.utc)
    try:
        # Read everything first, then give the connection back before any network I/O.
        async with pool.acquire() as conn:
            holders = await floor_role_holders(conn)
            cards = {r["job_card_id"]: dict(r) for r in
                     await conn.fetch(_CARDS_SQL, [u["job_card_id"] for u in items])}
            source_id = {u["job_card_id"]: u.get("from_job_card_id")
                         or (cards.get(u["job_card_id"]) or {}).get("prev_job_card_id") for u in items}
            more = sorted({int(s) for s in source_id.values() if s and int(s) not in cards})
            if more:
                cards.update({r["job_card_id"]: dict(r) for r in await conn.fetch(_CARDS_SQL, more)})
            no_floor_plans = sorted({c["plan_id"] for jid, c in cards.items()
                                     if jid in source_id and c.get("plan_id")
                                     and not str(c.get("floor") or "").strip()})
            planners = ({r["plan_id"]: r["created_by"] for r in await conn.fetch(_PLANS_SQL, no_floor_plans)}
                        if no_floor_plans else {})
            named = await _named_users(conn, [cards[u["job_card_id"]].get("assigned_to_team_leader")
                                              for u in items if u["job_card_id"] in cards]
                                       + list(planners.values()))
        report["missing"] = [u["job_card_id"] for u in items if u["job_card_id"] not in cards]
        if report["missing"]:
            logger.warning("[job-card-unlock] job card(s) %s are not there to notify about", report["missing"])
        settings = _settings() if len(report["missing"]) < len(items) else None
        for u in items:
            card = cards.get(u["job_card_id"])
            if card is None:
                continue
            sid = source_id.get(u["job_card_id"])
            source = cards.get(int(sid)) if sid else None
            people = recipients(card, source, holders, named, planners.get(card.get("plan_id")), actor_user_id)
            f = facts(card, source, u, unlocked_by, when)
            entry = await _notify_one(settings, people, card, f, unlocked_by)
            report["cards"].append({"job_card_id": u["job_card_id"],
                                    "from_job_card_id": u.get("from_job_card_id"), **entry})
    except Exception as exc:  # noqa: BLE001
        logger.exception("[job-card-unlock] notifying about %s failed", report["job_card_ids"])
        report["error"] = str(exc)
    logger.info("[job-card-unlock] unlock notice: %s", report)
    return report
