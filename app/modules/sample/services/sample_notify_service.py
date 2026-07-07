"""Sample-module notification dispatch (NPD plan §11) — the mail / approval matrix.

Routes every gated event to (a) the right in-app store_alert(s) and (b) a real
SMTP email, driven by the data-driven sample_notification_map (event ×
sample_type → notify_teams). Reuses the production mail_service SMTP transport
(best-effort: a no-op when SMTP_HOST is unset; failures logged, never raised, so
a mail outage can't fail the API call that triggered the notification).
"""
from __future__ import annotations

import logging

from app.modules.production.services import mail_service
from app.modules.sample.services import notification_service

logger = logging.getLogger(__name__)

# Notify-team -> the role names whose active users receive the email for it.
TEAM_ROLES: dict[str, list[str]] = {
    "stores": ["inventory_manager"],
    "inventory": ["inventory_manager"],
    "npd": ["npd_team"],
    "business": ["business_head"],
    "production": ["floor_manager"],
}


async def _matrix_row(conn, event: str, sample_type: str) -> dict | None:
    row = await conn.fetchrow(
        """SELECT * FROM sample_notification_map
            WHERE event = $1 AND sample_type = $2 AND is_active ORDER BY id LIMIT 1""",
        event, sample_type)
    if not row and sample_type != "*":
        row = await conn.fetchrow(
            """SELECT * FROM sample_notification_map
                WHERE event = $1 AND sample_type = '*' AND is_active ORDER BY id LIMIT 1""",
            event)
    return dict(row) if row else None


async def _emails_for_teams(conn, teams: list[str]) -> list[str]:
    roles: set[str] = set()
    for t in teams:
        roles.update(TEAM_ROLES.get(t, []))
    if not roles:
        return []
    rows = await conn.fetch(
        """SELECT DISTINCT au.email
             FROM auth_user au JOIN auth_role r ON au.role_id = r.role_id
            WHERE r.role_name = ANY($1::text[]) AND au.is_active
              AND au.email IS NOT NULL AND TRIM(au.email) <> ''
            ORDER BY au.email""",
        list(roles))
    return [r["email"] for r in rows]


async def notify_event(conn, *, event: str, sample_type: str = "*", message: str,
                       subject: str | None = None, mail_body: str | None = None,
                       related_id: int | None = None,
                       related_type: str = notification_service.REL_SAMPLE_REQ,
                       entity: str = "cfpl") -> dict:
    """Dispatch in-app alerts + email per the matrix. Best-effort — never raises.
    Returns {teams, emails} for observability."""
    sent = {"teams": [], "emails": 0}
    try:
        row = await _matrix_row(conn, event, sample_type)
        if not row:
            return sent
        teams = list(row.get("notify_teams") or [])
        sent["teams"] = teams
        for team in teams:
            await notification_service.emit_alert(
                conn, alert_type=event, target_team=team, message=message,
                related_id=related_id, related_type=related_type, entity=entity)
        if row.get("mail_enabled", True):
            emails = await _emails_for_teams(conn, teams)
            if emails:
                # mail_service._send is best-effort and a no-op when SMTP is unset.
                mail_service._send(subject or message, mail_body or message, emails, [])
                sent["emails"] = len(emails)
    except Exception:
        logger.exception("[sample-notify] event=%s sample_type=%s failed (swallowed)", event, sample_type)
    return sent
