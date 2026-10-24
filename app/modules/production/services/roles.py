"""Whether a user holds one of some roles — read from ALL their roles.

A session carries role_ids (every role the user holds, auth_user_role) next to
the primary role_name. Gates that compared role_name only missed people whose
QC or floor-manager role isn't their first one (audit 2026-10-23).
"""
from __future__ import annotations


async def holds_role(conn, user, *roles: str) -> bool:
    """Admins, and anyone holding one of `roles` among all their roles."""
    if getattr(user, "is_admin", False):
        return True
    role_ids = [r for r in (getattr(user, "role_ids", None) or []) if r is not None]
    wanted = [r.strip().lower() for r in roles if r and r.strip()]
    if not role_ids or not wanted:
        return False
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM auth_role WHERE role_id = ANY($1) "
        "AND lower(trim(role_name)) = ANY($2::text[]))",
        role_ids, wanted,
    ))
