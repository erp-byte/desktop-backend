"""Two-person NPD authoring allow-list (NPD plan §2).

Narrows the broad `npd_team` role (already enforced on the routes via
require_permission) to the named users in `npd_authorized_users`, per capability:

    AUTHOR  — create / edit / start a recipe (draft BOM or dev job card)
    CLOSE   — close a dev job card (records output + promotes)
    PROMOTE — promote a requisition draft BOM to a live BOM

Until the allow-list is seeded for a capability it is EMPTY, in which case the
role gate on the route is sufficient and this check is a no-op — so designating
"the 2 people" is a single config INSERT, not a deploy. Admins always pass.
"""
from __future__ import annotations

from fastapi import HTTPException

CAPABILITIES = ("AUTHOR", "CLOSE", "PROMOTE")


async def require_npd_authorized(conn, user, capability: str) -> None:
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown NPD capability {capability!r}")
    if getattr(user, "is_admin", False):
        return
    # Not yet configured for this capability → fall back to the route's role gate.
    configured = await conn.fetchval(
        "SELECT COUNT(*) FROM npd_authorized_users WHERE capability = $1 AND active",
        capability)
    if not configured:
        return
    allowed = await conn.fetchval(
        "SELECT 1 FROM npd_authorized_users WHERE user_id = $1 AND capability = $2 AND active",
        user.user_id, capability)
    if not allowed:
        raise HTTPException(403, detail={
            "error": "npd_not_authorized",
            "message": "Only the designated NPD authors may perform this action",
            "details": {"capability": capability, "user_id": user.user_id}})
