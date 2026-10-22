"""Material In site scope: a receiving-desk user sees only the POs for their own
warehouses.

Applies to a user who is not an admin, has warehouses in their access
(auth_user.allowed_warehouses), and does not hold the purchase_manager role.
Such a user does not see a PO whose consignee address
names a warehouse outside that access -- not in the PO lists, not opened by its
transaction number, not to receive into. A PO whose address names no warehouse
(none uploaded, or no plant code at its start) stays visible: it is no one
site's, and every PO uploaded before the Consignee Address column has none.

Admins, the purchase team (who upload every site's POs) and users with no
warehouse lock see every PO, as before. Sites compare as sites: a grant 'W-202'
covers an address opening 'W-202(A)'.

The purchase team is told by role, not by the purchase.po.create permission:
on RDS (2026-10-22) store_head still holds PO create/edit/delete, and a
purchase manager locked to W202 + A185 must still see A68's POs in PO Upload.
"""

from __future__ import annotations

from app.core.sites import scope_keys, warehouse_sites
from app.core.warehouse_scope import user_has_warehouse
from app.modules.purchase.services.consignee_site import consignee_site_key

PURCHASE_TEAM_ROLE = "purchase_manager"


async def hidden_sites_for(pool, user) -> list[str]:
    """Site keys of the warehouses whose POs `user` may not see; [] for none.
    Takes a connection only for the role check, when it is needed."""
    if user.is_admin or not user.allowed_warehouses:
        return []
    async with pool.acquire() as conn:
        if await conn.fetchval(
            "SELECT 1 FROM auth_role WHERE role_id = ANY($1::int[]) AND role_name = $2",
            list(user.role_ids or []), PURCHASE_TEAM_ROLE,
        ):
            return []
    return scope_keys([
        site for site in warehouse_sites()
        if not user_has_warehouse(user.allowed_warehouses, site)
    ])


def address_hidden(address: str | None, hidden: list[str]) -> bool:
    """True when the consignee address is for one of the `hidden` sites."""
    key = consignee_site_key(address)
    return bool(key) and key in hidden
