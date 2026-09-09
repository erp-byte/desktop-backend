"""Mutation gates for the Job Work module.

The reference gated only DELETE (a hardcoded two-address allowlist checked
against a `?user_email=` QUERY PARAM — spoofable by anyone who can call the
API). Create had no gate at all: any caller could dispatch stock out of any
warehouse.

Create stays open to authenticated operators — that IS the production
behaviour, and job-work dispatch is routine floor work — but the warehouse lock
from the transfer module applies, so a warehouse-bound operator cannot dispatch
out of a site they do not work at. The identity comes from the verified JWT.
"""
from __future__ import annotations

from app.core.middleware.request_context import AuthError
from app.core.warehouse_scope import user_has_warehouse
from app.modules.auth.middleware import AuthUser


def assert_can_dispatch_from(u: AuthUser, from_warehouse: str | None) -> None:
    """Material Out must originate from the operator's own warehouse.

    Admins and users with no warehouse restriction on their profile are
    unrestricted — same rule as transfer.permissions.assert_can_dispatch_from.
    """
    if u.is_admin or not u.allowed_warehouses:
        return
    if not user_has_warehouse(u.allowed_warehouses, from_warehouse):
        raise AuthError(
            "forbidden",
            "Job Work material out must originate from your assigned warehouse "
            f"({', '.join(u.allowed_warehouses)}); got '{from_warehouse or '—'}'.",
            403,
        )
