"""Who may write Customer Returns: the server's half of the screens' gate.

Every create, edit, delete and box write depends on cr_permission(sub_module, action):

  * admins pass, as on the screens (which open only for admins today);
  * anyone else needs a role granted customer_returns.<sub_module>.<action>, the
    catalog in app/db/133_customer_returns_permissions.sql. No role is granted any
    of them yet, so until one is, only admins write; a missing catalog (migration
    not applied) refuses the same way, since check_permission finds no grant;
  * a user locked to companies (auth_user.allowed_entities) writes only to those,
    and one locked to warehouses (allowed_warehouses) only to returns of those
    factory units — both the return's own unit and, when the request sets one, the
    new unit, so a return can't be moved out of reach either. The company is the
    path's CFPL / CDPL and the unit is read from the return or the body, so
    require_permission's own query-parameter locks can't see them; they are checked
    here, and passed on to the role's entity / warehouse scope.

    returns.create   raise a return and send it for approval
    returns.edit     change its header or lines (still only while undecided)
    returns.delete   delete it (still only while undecided)
    boxes.edit       print, save or remove its boxes; write the box-edit log

What a screen action needs: the New screen's first save is a create (with its boxes:
create and boxes.edit), but printing a box first and then sending (or retrying after
a failure) is the one-step save of the header, lines and boxes (edit and boxes.edit,
plus create when it also sends), and Discard deletes what a print created — so a
warehouse role needs create, edit, delete and boxes.edit together; boxes.edit alone
covers boxes after approval.

Reads stay login-only: a business head reviews a return in the app without being
an admin, and deciding one is approval_service's own who / when rule.
"""
from __future__ import annotations

import json

from fastapi import Depends, HTTPException, Request

from app.core.warehouse_scope import user_has_warehouse
from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.auth.services import permission_service
from app.modules.customer_returns.tables import cr_table_names

MODULE = "customer_returns"

_WHAT = {
    ("returns", "create"): "raise customer returns",
    ("returns", "edit"): "edit customer returns",
    ("returns", "delete"): "delete customer returns",
    ("boxes", "edit"): "enter or print customer-return boxes",
}


def _forbidden(message: str, **details) -> HTTPException:
    return HTTPException(403, detail={"error": "forbidden", "message": message, "details": details})


async def _body_unit(request: Request) -> str | None:
    """The factory unit a create or header edit sets, if any. FastAPI has already read
    the body (it is cached on the request), so reading it here costs nothing; a body
    that isn't JSON is left for the route's own validation to refuse."""
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    header = body.get("header") if isinstance(body.get("header"), dict) else body
    unit = header.get("factory_unit")
    return unit.strip() if isinstance(unit, str) and unit.strip() else None


async def _units(request: Request, conn, company: str | None) -> list[str]:
    """Every factory unit this request touches: the stored return's, and the one the
    body sets. An unknown return contributes nothing (the route answers 404)."""
    units: list[str] = []
    cr_id = request.path_params.get("cr_id")
    if company and cr_id:
        stored = await conn.fetchval(
            f"SELECT factory_unit FROM {cr_table_names(company)['header']} WHERE rtv_id = $1", cr_id)
        if stored and str(stored).strip():
            units.append(str(stored).strip())
    if request.method in ("POST", "PUT"):
        new = await _body_unit(request)
        if new and new not in units:
            units.append(new)
    return units


def cr_permission(sub_module: str, action: str):
    """FastAPI dependency: the logged-in user, if they may do `action` on
    customer_returns.<sub_module> for the path's company and the factory units the
    request touches; 403 otherwise."""
    what = _WHAT[(sub_module, action)]

    async def _dependency(request: Request, user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.is_admin:
            return user
        company = str(request.path_params.get("company") or "").strip().lower() or None
        locked_to = {str(e).strip().lower() for e in user.allowed_entities if e}
        if company and locked_to and company not in locked_to:
            raise _forbidden(f"You can't {what} for {company.upper()}.",
                             company=company.upper(), allowed_entities=sorted(locked_to))
        async with request.app.state.db_pool.acquire() as conn:
            units = await _units(request, conn, company)
            for unit in units:
                if user.allowed_warehouses and not user_has_warehouse(user.allowed_warehouses, unit):
                    raise _forbidden(f"You can't {what} for {unit}.",
                                     factory_unit=unit, allowed_warehouses=list(user.allowed_warehouses))
            for unit in units or [None]:
                allowed = await permission_service.check_permission(
                    conn, user.role_ids, user.is_admin, MODULE, sub_module, None, action,
                    entity=company, warehouse=unit)
                if not allowed:
                    raise _forbidden(f"You don't have permission to {what}"
                                     + (f" for {unit}." if unit else "."),
                                     permission=f"{MODULE}.{sub_module}.{action}")
        return user

    return _dependency
