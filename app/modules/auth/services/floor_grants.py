"""A user's floor grants are checked before they are saved.

They used to be saved exactly as sent (audit 2026-10-23), while the job-card
routes compared them letter for letter: a grant spelt even slightly differently
from a card's floor closed that card to its own floor manager.

A grant is kept when it names:
  * a floor a warehouse declares (stock_take/floors.py, the list the admin screen
    offers) — saved in the declared spelling, whatever case or spacing it came in;
  * a floor Stock Take has recorded at a warehouse that declares none (the cold
    stores and godowns, whose floors are whatever their counts are filed under —
    floors.floors_for) — saved as recorded;
  * a floor the user already holds — left as it is, so a profile written by hand
    (Savla, Savla Bond) still saves.
Anything else is refused, naming it.
"""
from __future__ import annotations

from typing import Iterable, Optional

from fastapi import HTTPException

from app.core.warehouse_scope import normalise_floor
from app.modules.stock_take import floors as _floors


def canonical_floor_grants(floors: Iterable[str], *, recorded: Iterable[str] = (),
                           held: Optional[Iterable[str]] = None) -> tuple[list[str], list[str]]:
    """(grants to save, names that are no known floor). Pure."""
    known: dict[str, str] = {}
    for declared in _floors.FLOORS_BY_WAREHOUSE.values():
        for f in declared:
            known.setdefault(normalise_floor(f), f)
    for f in recorded:
        known.setdefault(normalise_floor(f), str(f).strip())
    for f in held or ():
        known.setdefault(normalise_floor(f), f)
    out: list[str] = []
    unknown: list[str] = []
    for f in floors or ():
        key = normalise_floor(f)
        if not key:
            continue
        name = known.get(key)
        if name is None:
            unknown.append(str(f).strip())
        elif name not in out:
            out.append(name)
    return out, unknown


async def recorded_floors(conn) -> list[str]:
    """Floors Stock Take has recorded at warehouses that declare no floors."""
    from app.modules.stock_take.services.latest_stock_service import fetch_places
    places = await fetch_places(conn)
    return [f for wh, fls in places.items()
            if not _floors.FLOORS_BY_WAREHOUSE.get(_floors.normalise_warehouse(wh))
            for f in fls]


async def checked_floor_grants(conn, floors: Iterable[str], *,
                               user_id: Optional[int] = None) -> list[str]:
    """The grants to save for this user (None for a new one); 400 naming any
    floor that isn't one."""
    held = None
    if user_id is not None:
        held = await conn.fetchval("SELECT allowed_floors FROM auth_user WHERE user_id = $1", user_id)
    out, unknown = canonical_floor_grants(floors, recorded=await recorded_floors(conn), held=held)
    if unknown:
        raise HTTPException(status_code=400, detail={
            "error": "unknown_floor",
            "message": (f"Not a known floor: {', '.join(unknown)}. "
                        "Pick floors from the list for the user's warehouses."),
            "unknown": unknown,
        })
    return out
