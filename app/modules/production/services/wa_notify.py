"""Shared plumbing for the WhatsApp + email notices this backend sends its own
users.

Two notices use it today — floor_requisition/services/notify_service.py (store,
when the floor raises a requisition) and job_card_notify.py (the floor managers,
when a job card is created). Everything that MUST behave identically in both
lives here, so a fix lands once:

  * the recipient query, which tests an account exactly as validate_session does
    (is_active AND status = 'active') and resolves roles exactly as
    auth_service._effective_roles does (auth_user_role is the source of truth;
    the primary auth_user.role_id counts only for a user with no rows there);
  * covers_place — stock_take.place_scope's rule, so a notice never reaches
    someone who could not open the thing it is about, and a warehouse matches
    whether it is written 'A-185' or 'A185';
  * the template variable sanitiser Meta's Cloud API requires;
  * the Graph call itself and the two switches that turn it off.

What differs per notice — its role, template, wording and email — stays in the
notice's own module.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Optional

import httpx

from app.config import Settings
from app.modules.stock_take import place_scope

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# One row per user. Role resolution mirrors auth_service._effective_roles and the
# account test mirrors validate_session (is_active AND status = 'active').
RECIPIENTS_SQL = """
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


def current_settings():
    """One read of the settings per notification (tests replace this)."""
    return Settings()


# ── who ──────────────────────────────────────────────────────────────────────

def covers_place(allowed_warehouses: Optional[Iterable[str]], allowed_floors: Optional[Iterable[str]],
                 warehouse: str, floor: str) -> bool:
    """Whether a user with these grants may open something at warehouse/floor —
    place_scope's rule exactly, so notification and access never disagree.

    One caveat on the floor axis: place_scope upper-cases both sides, while the
    job card readers compare the grant strings raw (GET /job-cards filters
    `floor = ANY($n)`, jc_annexures_v2.assert_jc_in_scope tests `not in`). A
    grant that differs from the floor only in case is therefore notified about a
    card it cannot open - correct the grant rather than loosening this rule.
    """
    granted_w, granted_f = place_scope.granted(SimpleNamespace(
        allowed_warehouses=list(allowed_warehouses or []), allowed_floors=list(allowed_floors or [])))
    wh = place_scope._normalise_warehouse(warehouse)
    fl = (floor or "").strip().upper()
    return (not granted_w or wh in granted_w) and (not granted_f or fl in granted_f)


async def role_holders(conn, roles: Iterable[str]) -> list[dict[str, Any]]:
    """Every user who can sign in with one of these roles among theirs, one entry each."""
    rows = await conn.fetch(RECIPIENTS_SQL, list(roles))
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


def assigned_to_place(allowed_warehouses: Optional[Iterable[str]], allowed_floors: Optional[Iterable[str]],
                      warehouse: str, floor: str) -> bool:
    """Whether this place is one the user is actually ASSIGNED to — both axes
    named in their own grants.

    Stricter than covers_place on purpose. There an empty grant means "no limit",
    which is the right rule for what a person may OPEN but the wrong one for whom
    to message: an unrestricted account would be told about every job card on
    every floor. A notice meant for "the floor's manager" must name the floor.
    """
    granted_w, granted_f = place_scope.granted(SimpleNamespace(
        allowed_warehouses=list(allowed_warehouses or []), allowed_floors=list(allowed_floors or [])))
    wh = place_scope._normalise_warehouse(warehouse)
    fl = (floor or "").strip().upper()
    return bool(granted_w and granted_f and wh in granted_w and fl in granted_f)


def assigned(holders: list[dict[str, Any]], warehouse: str, floor: str) -> list[dict[str, Any]]:
    """The holders this warehouse + floor is assigned to (assigned_to_place)."""
    return [h for h in holders
            if assigned_to_place(h["allowed_warehouses"], h["allowed_floors"], warehouse, floor)]


def covering(holders: list[dict[str, Any]], warehouse: str, floor: str) -> list[dict[str, Any]]:
    """The holders whose granted places include this warehouse and floor."""
    return [h for h in holders
            if covers_place(h["allowed_warehouses"], h["allowed_floors"], warehouse, floor)]


# ── what ─────────────────────────────────────────────────────────────────────

_SPACES = re.compile(r"\s+")


def param(text: Any, limit: int) -> str:
    """A WhatsApp template variable: Meta refuses empty values, new lines, tabs and
    runs of spaces, so collapse all whitespace, cap the length and never send blank."""
    t = _SPACES.sub(" ", str(text or "")).strip()
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t or "-"


# ── send ─────────────────────────────────────────────────────────────────────

async def send_template(settings, message: dict[str, Any]) -> None:
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


def whatsapp_off_reason(settings) -> Optional[str]:
    if not settings.WHATSAPP_ENABLED:
        return "whatsapp_disabled"
    if not str(settings.WHATSAPP_ACCESS_TOKEN or "").strip() or not str(settings.WHATSAPP_PHONE_NUMBER_ID or "").strip():
        return "whatsapp_credentials_missing"
    return None
