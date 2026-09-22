"""The floors each warehouse declares — the server-side half of the ERP profile.

WHY THIS EXISTS
The admin screen assigns floors from FLOORS_BY_WAREHOUSE in
web_replica/src/lib/admin-api.ts, and labels the control:

    "Floor / Area (filtered by warehouses; empty = all in those warehouses)"

That sentence is the whole policy, and until now only the browser knew it. The
server offered "every distinct floor_name in the table" instead, which is a
different set: it spans warehouses the user is not assigned to, and it includes
the names nobody has canonicalised — TEESTTTT, TEST FLOOR, 1ST : FIRST LINE,
REJECTION COLD & RACK (a stock type, not a place). Those are facts about what has
been typed, not choices anyone should be offered.

KEPT IN STEP BY A TEST, NOT BY DISCIPLINE
This is a duplicate of a TypeScript literal, so it will drift unless something
checks. tests/services/test_stock_take_floor_profile.py parses admin-api.ts and
asserts the two are identical. Add a floor in one place and that test fails.
"""
from __future__ import annotations

from typing import Iterable, Sequence

#: Mirrors FLOORS_BY_WAREHOUSE in web_replica/src/lib/admin-api.ts.
FLOORS_BY_WAREHOUSE: dict[str, list[str]] = {
    "W202": [
        "Lower Basement", "Upper Basement", "First Floor", "First Floor Mezz",
        "Second Floor", "Second Floor Mezz", "Terrace",
        "Store",
    ],
    "A185": [
        "Roasting Area", "Mezzanine", "Sorting Area", "Printing Area",
        "Dmart Production Area", "Dmart Packing Area", "Cheese Floor",
        "FG store", "FFS Packing Area",
        "A185 Stores", "A185 Stores Rack", "A185 Cold",
    ],
}


#: What a person calls the cold stores, keyed by the normalised code. The code
#: alone reads as noise on a report ("D39", "ESKIMO"); these are the names the
#: rest of the ERP already uses (transfer DC, gate pass, inventory ledger).
#: A warehouse missing here is shown by its code.
WAREHOUSE_LABELS: dict[str, str] = {
    "D39": "Savla D-39",
    "D514": "Savla D-514",
    "RISHI": "Rishi",
    "ESKIMO": "Eskimo",
    "SUPREME": "Supreme",
}

#: The third-party cold stores, listed after the factories on screens and sheets.
COLD_WAREHOUSES: frozenset[str] = frozenset(WAREHOUSE_LABELS)


def normalise_warehouse(code: str | None) -> str:
    """'W-202' -> 'W202'. auth_user.allowed_warehouses carries both spellings."""
    return (code or "").strip().upper().replace("-", "")


def warehouse_label(code: str | None) -> str:
    """'D39' / 'D-39' -> 'Savla D-39'; a code with no name comes back as the code."""
    key = normalise_warehouse(code)
    return WAREHOUSE_LABELS.get(key, key)


def declared_floors(warehouses: Iterable[str]) -> list[str]:
    """Every floor the given warehouses declare, in declaration order, deduped.

    Declaration order is deliberate: it is how the floors are laid out on the
    admin screen and roughly how the building is walked, which reads better in a
    dropdown than alphabetical would.
    """
    out: list[str] = []
    for wh in warehouses:
        for f in FLOORS_BY_WAREHOUSE.get(normalise_warehouse(wh), ()):
            if f not in out:
                out.append(f)
    return out


def undeclared(warehouses: Iterable[str]) -> list[str]:
    """Those of `warehouses` that declare no floors at all (F53, A68, ...).

    They are not an error. A warehouse with no declared floors has to fall back
    to whatever the data holds, or its users get an empty dropdown and cannot
    post at all.
    """
    return [w for w in warehouses if not FLOORS_BY_WAREHOUSE.get(normalise_warehouse(w))]


def floors_for(warehouses: Sequence[str], data_floors: Iterable[str]) -> list[str]:
    """What to offer for `warehouses`: declared floors, plus data for the rest.

    `data_floors` is the distinct floor_name set from the entries table. It is
    used ONLY for warehouses that declare nothing — never to widen a warehouse
    that does declare its floors, because that is exactly how the uncanonicalised
    names got into the dropdown in the first place.
    """
    out = declared_floors(warehouses)
    if undeclared(warehouses):
        for f in data_floors:
            if f and f not in out:
                out.append(f)
    return out
