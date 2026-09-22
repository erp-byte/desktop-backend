"""Which warehouse + floor a caller may act on.

Read straight off the caller's GRANTS (auth_user.allowed_warehouses /
allowed_floors), not off the places that already hold counts: a job card's
floor can hold nothing yet, and refusing it would report a permissions problem
where the truth is "nothing recorded here". Empty grants mean "no restriction"
(auth_schema.sql:35).

Shared by the stock-take floor-stock read and floor requisitions, so a floor is
open or closed to someone the same way on both screens.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.stock_take.services.transactions_service import _normalise_warehouse


def granted(user) -> tuple[list[str], list[str]]:
    """(warehouses, floors) the caller is limited to: normalised warehouse codes
    and upper-cased floors, sorted. An empty list means no limit on that axis."""
    warehouses = sorted({_normalise_warehouse(w)
                         for w in (user.allowed_warehouses or []) if str(w).strip()})
    floors = sorted({str(f).strip().upper()
                     for f in (user.allowed_floors or []) if str(f).strip()})
    return warehouses, floors


def assert_place_allowed(user, warehouse: str, floor: str) -> None:
    """Raise 403 unless the caller's grants cover this warehouse + floor."""
    wh = _normalise_warehouse(warehouse)
    fl = (floor or "").strip()
    granted_w, granted_f = granted(user)
    if granted_w and wh not in granted_w:
        raise HTTPException(403, detail={
            "error": "warehouse_not_allowed",
            "message": f"You are not assigned to warehouse {wh!r}.",
            "details": {"requested": wh, "allowed_warehouses": granted_w}})
    if granted_f and fl.upper() not in granted_f:
        raise HTTPException(403, detail={
            "error": "floor_not_allowed",
            "message": f"You are not assigned to floor {fl!r}.",
            "details": {"requested": fl, "allowed_floors": granted_f}})
