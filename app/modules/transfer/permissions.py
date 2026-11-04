"""Per-action rules for the Transfer module.

Who may delete (requests, transfers, receipts, inner cold challans), run the in-transit
sync, receive a transfer and re-open / edit a receipt is a role permission, not a list
of people: admins and inventory managers (holders of transfer.requests.approve), checked
by the router's `require_permission` dependency (router._ADMIN_OR_MANAGER). The rules
here are the ones that depend on the user's warehouses or on the row: the profile
warehouse locks, read scoping, the request edit / own-request rules and the dashboard
gate. All derive from the verified JWT (`AuthUser`), and `is_admin` (the superuser flag)
bypasses them as it does `require_permission`.
"""
from __future__ import annotations

from app.core.middleware.request_context import AuthError
from app.core.sites import canonical_site
from app.core.warehouse_scope import user_has_warehouse
from app.modules.auth.middleware import AuthUser

ADMIN_ROLES = {"admin", "developer"}
# The Transfer Summary analytics dashboard is admin-only in the replica
# (see assert_can_view_dashboard).


def _email(u: AuthUser) -> str:
    return (u.email or "").lower()


def _role(u: AuthUser) -> str:
    return (u.role_name or "").lower()


def _forbid(detail: str) -> None:
    raise AuthError("forbidden", detail, 403)


def _site(site: str | None) -> str:
    """A site as a refusal names it: its canonical name ('D-39' -> 'Savla D-39')."""
    return canonical_site(site) or "—"


def _sites(sites) -> str:
    """The user's warehouses as a refusal names them: canonical names, each once."""
    return ", ".join(dict.fromkeys(n for n in map(canonical_site, sites or ()) if n)) or "—"


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
    or destination (or, for a cold dispatch, one of the cold units its boxes came
    from), and a detail read of any other row is 404 (`in_scope`)."""
    if u.is_admin:
        return []
    return list(u.allowed_warehouses or [])


def in_scope(scope: list[str] | None, *sites: str | None) -> bool:
    """Whether a row whose sites are `sites` (its From, To, receiving warehouse, ...) is
    visible under `scope` (`scope_warehouses`): always for an empty scope; otherwise
    when one of them names a site of the scope, in any spelling (`user_has_warehouse`).
    A from_cold_unit such as 'Rishi, Savla D-39' counts each of its units. The
    detail reads apply this as the list endpoints apply it in SQL."""
    if not scope:
        return True
    return any(user_has_warehouse(scope, part) for site in sites for part in (site or "").split(","))


def has_warehouse_lock(u: AuthUser) -> bool:
    """True for a user the profile warehouse lock applies to: not an admin, and with a
    non-empty `allowed_warehouses`."""
    return not u.is_admin and bool(u.allowed_warehouses)


# ── Profile warehouse lock (mutations) ───────────────────────────────────────
# A warehouse-locked operator may only DISPATCH FROM and RECEIVE INTO a warehouse
# in their profile scope (`allowed_warehouses`). Admins and users with no lock
# (empty scope) are unrestricted — consistent with `scope_warehouses` read scoping.
# Sites are compared as sites (`user_has_warehouse`: A185 == A-185 == 'Warehouse
# A185', the grant code 'D-39' == 'Savla D-39'), and the refusals name them by their
# canonical names (app.core.sites).
def assert_can_dispatch_from(u: AuthUser, from_warehouse: str | None) -> None:
    """Transfer-OUT lock: from_warehouse must be the operator's profile warehouse."""
    if not has_warehouse_lock(u):
        return
    if not user_has_warehouse(u.allowed_warehouses, from_warehouse):
        _forbid(
            "Transfer OUT must originate from your assigned warehouse "
            f"({_sites(u.allowed_warehouses)}); this transfer is from {_site(from_warehouse)}."
        )


def assert_can_receive_into(u: AuthUser, to_warehouse: str | None) -> None:
    """Transfer-IN lock: receiving (to) warehouse must be the operator's profile warehouse."""
    if not has_warehouse_lock(u):
        return
    if not user_has_warehouse(u.allowed_warehouses, to_warehouse):
        _forbid(
            "Transfer IN must be received into your assigned warehouse "
            f"({_sites(u.allowed_warehouses)}); this transfer is to {_site(to_warehouse)}."
        )


def assert_can_work_in_cold_store(u: AuthUser, store: str | None) -> None:
    """Inner cold transfer lock: the cold store whose stock rows it relabels (or, on a
    delete, restores) must be the operator's profile warehouse. Same rule as
    `assert_can_dispatch_from`."""
    if not has_warehouse_lock(u):
        return
    if not user_has_warehouse(u.allowed_warehouses, store):
        _forbid(
            "An inner cold transfer can only be made in your assigned warehouse "
            f"({_sites(u.allowed_warehouses)}); this stock is at {_site(store)}."
        )


# ── Raising and editing a request ────────────────────────────────────────────
def assert_can_request_for(u: AuthUser, to_warehouse: str | None) -> None:
    """Request lock: a request is raised by the requesting site, so its To site must be
    the operator's profile warehouse. Same rule as `assert_can_dispatch_from`."""
    if not has_warehouse_lock(u):
        return
    if not user_has_warehouse(u.allowed_warehouses, to_warehouse):
        _forbid(
            "A request can only be raised for your assigned warehouse "
            f"({_sites(u.allowed_warehouses)}); this one is for {_site(to_warehouse)}."
        )


def assert_can_edit_request(u: AuthUser, created_by: str | None) -> None:
    """Edit rule: only the person who raised the request, or an admin. Emails are
    compared case-insensitively; an account with no email cannot be matched to the
    request's raiser (recorded as 'unknown'), so only an admin may edit for it."""
    if u.is_admin:
        return
    email = _email(u).strip()
    if not email or email != (created_by or "").strip().lower():
        _forbid("Only the person who raised this request, or an admin, can edit it")


# ── Transfer request decisions ───────────────────────────────────────────────
# The role permission (transfer.requests.accept / .approve) is the route's
# `require_permission` dependency. These are the rules that depend on the request
# row; request_decision_service runs them with the row locked.
def assert_can_answer_request_from(u: AuthUser, from_warehouse: str | None) -> None:
    """Store accept / reject lock: the request's supplying (from) site must be the
    operator's profile warehouse. Same rule as `assert_can_dispatch_from`."""
    if not has_warehouse_lock(u):
        return
    if not user_has_warehouse(u.allowed_warehouses, from_warehouse):
        _forbid(
            "Only the store of the supplying warehouse can accept or reject this request: "
            f"it is to be sent from {_site(from_warehouse)}, and you are assigned to "
            f"{_sites(u.allowed_warehouses)}."
        )


def assert_not_own_request(u: AuthUser, created_by: str | None, store_decided_by: str | None) -> None:
    """Approve / reject rule: nobody — admins included — decides a request they
    raised or accepted. Emails are compared case-insensitively. An account with no
    email is recorded as 'unknown' on the request, so it is compared as 'unknown'
    too: two such accounts cannot be told apart, and the decision is refused —
    saying that, since this account may not be the one that raised or accepted it."""
    email = _email(u).strip()
    me = email or "unknown"
    raised = me == (created_by or "").strip().lower()
    accepted = me == (store_decided_by or "").strip().lower()
    if not email and (raised or accepted):
        _forbid(
            "Your account has no email address, so it cannot be told apart from the account that "
            "raised or accepted this request: an inventory manager whose account has an email "
            "address has to approve or reject it"
        )
    if raised:
        _forbid("You raised this request, so another inventory manager has to approve or reject it")
    if accepted:
        _forbid("You accepted this request for the store, so another inventory manager has to approve or reject it")
