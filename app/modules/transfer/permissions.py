"""Per-action mutation gates for the Transfer module.

The production backend used per-endpoint allowlists (see interunit_server.py /
cold_storage_server.py): requests + transfers + backfill →
{yash, b.hrithik} or admin/developer; transfer-in delete → {yash} only;
inner-cold delete → {hrithik, yash}. We mirror those, derived from the verified
JWT (`AuthUser`) rather than a client-supplied query param, and let `is_admin`
(the replica's superuser flag) bypass — consistent with `require_permission`.
"""
from __future__ import annotations

from app.core.middleware.request_context import AuthError
from app.modules.auth.middleware import AuthUser

# Mirrors interunit_server.AUTHORIZED_DELETE_EMAILS / ADMIN_ROLES.
AUTHORIZED_DELETE_EMAILS = {"yash@candorfoods.in", "b.hrithik@candorfoods.in"}
ADMIN_ROLES = {"admin", "developer"}
# Stricter per-endpoint allowlists (reference: delete_transfer_in / inner-cold).
TRANSFER_IN_DELETE_EMAILS = {"yash@candorfoods.in"}
INNER_COLD_DELETE_EMAILS = {"hrithik@candorfoods.in", "yash@candorfoods.in"}
# Reference page gate AUTHORIZED_ACKNOWLEDGE_USERS (transfer-in receive).
ACKNOWLEDGE_EMAILS = {
    "yash@candorfoods.in", "b.hrithik@candorfoods.in", "sunil.jasoria@candorfoods.in",
}
# The Transfer Summary analytics dashboard is admin-only in the replica
# (see assert_can_view_dashboard) — no email allowlist.


def _email(u: AuthUser) -> str:
    return (u.email or "").lower()


def _role(u: AuthUser) -> str:
    return (u.role_name or "").lower()


def _forbid(detail: str) -> None:
    raise AuthError("forbidden", detail, 403)


def _is_standard_mutator(u: AuthUser) -> bool:
    """The default transfer mutate set: allowlisted email or admin/developer."""
    return u.is_admin or _email(u) in AUTHORIZED_DELETE_EMAILS or _role(u) in ADMIN_ROLES


def assert_can_delete_request(u: AuthUser) -> AuthUser:
    if not _is_standard_mutator(u):
        _forbid("You are not authorized to delete records")
    return u


def assert_can_delete_transfer(u: AuthUser) -> AuthUser:
    if not _is_standard_mutator(u):
        _forbid("You are not authorized to delete records")
    return u


def assert_can_backfill(u: AuthUser) -> AuthUser:
    if not _is_standard_mutator(u):
        _forbid("You are not authorized to sync pending stock")
    return u


def assert_can_delete_transfer_in(u: AuthUser) -> AuthUser:
    if not (u.is_admin or _email(u) in TRANSFER_IN_DELETE_EMAILS):
        _forbid("You are not authorized to delete transfer-in records")
    return u


def assert_can_delete_inner_cold(u: AuthUser) -> AuthUser:
    if not (u.is_admin or _email(u) in INNER_COLD_DELETE_EMAILS):
        _forbid("You are not authorized to delete inner cold transfers")
    return u


def assert_can_acknowledge(u: AuthUser) -> AuthUser:
    """Gate for receiving a transfer-in (create pending / acknowledge / finalize)."""
    if not (u.is_admin or _email(u) in ACKNOWLEDGE_EMAILS):
        _forbid("You are not authorized to receive transfers")
    return u


def assert_can_view_dashboard(u: AuthUser) -> AuthUser:
    """Gate for the Transfer Summary analytics dashboard. Admin-only — the
    superuser flag or an admin/developer role. (The reference used a 2-email
    allowlist; the replica restricts it to admins instead.)"""
    if not (u.is_admin or _role(u) in ADMIN_ROLES):
        _forbid("You are not authorized to view the transfer summary dashboard")
    return u


def scope_warehouses(u: AuthUser) -> list[str]:
    """Warehouses the user is restricted to for read scoping. Empty list means
    unrestricted — admins, and users with no warehouse lock, see everything.
    Otherwise list endpoints only return rows where one of these is the source
    or destination."""
    if u.is_admin:
        return []
    return list(u.allowed_warehouses or [])
