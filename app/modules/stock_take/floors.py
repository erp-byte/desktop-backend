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
    "PM COLD": "PM Cold",
}

#: The third-party cold stores, listed after the factories on screens and sheets.
COLD_WAREHOUSES: frozenset[str] = frozenset(WAREHOUSE_LABELS)


#: A185 floors merged into others on 18 Nov 2026; scripts/apply_a185_floor_merge.py
#: moved their stock. Keyed by warehouse code, then the floor's UPPER name -> the
#: floor it went into.
#:
#: STOCK TAKE ONLY. Stock Take stops offering these floors (filters, the Adjust
#: form, posting) and leaves them out of its downloads once they are empty. The
#: admin screen, job cards, floor requisitions and FLOORS_BY_WAREHOUSE itself
#: still list them: the instruction was to change this module and no other.
MERGED_FLOORS: dict[str, dict[str, str]] = {
    "A185": {
        "FFS PACKING AREA": "FG store",
        "DMART PRODUCTION AREA": "Dmart Packing Area",
        "SORTING AREA": "Dmart Packing Area",
    },
}

#: What Stock Take CALLS a floor whose stored name stays as it is. Display only:
#: entries, postings, grants and filter values all keep the stored name, so
#: nothing that reads or writes the data sees a different floor.
FLOOR_DISPLAY: dict[str, dict[str, str]] = {
    "A185": {"DMART PACKING AREA": "Packing Area"},
}


def normalise_warehouse(code: str | None) -> str:
    """'W-202' -> 'W202'. auth_user.allowed_warehouses carries both spellings."""
    return (code or "").strip().upper().replace("-", "")


def is_merged_floor(warehouse: str | None, floor: str | None) -> bool:
    """True for a floor Stock Take no longer offers (see MERGED_FLOORS)."""
    return (floor or "").strip().upper() in MERGED_FLOORS.get(normalise_warehouse(warehouse), {})


def visible_floors(warehouse: str | None, floors: Iterable[str]) -> list[str]:
    """`floors` without the ones merged away, order kept."""
    return [f for f in floors if not is_merged_floor(warehouse, f)]


def floor_display(warehouse: str | None, floor: str | None) -> str:
    """'Dmart Packing Area' at A185 -> 'Packing Area'; any other floor as given, trimmed."""
    name = (floor or "").strip()
    return FLOOR_DISPLAY.get(normalise_warehouse(warehouse), {}).get(name.upper(), name)


def floor_display_any(floor: str | None, warehouses: str | Iterable[str] | None = None) -> str:
    """floor_display for a floor named WITHOUT its warehouse, as a filter value is.

    Tried against each of `warehouses` (every warehouse with a renamed floor when
    none is given); the first that shows it differently wins."""
    name = (floor or "").strip()
    whs = [warehouses] if isinstance(warehouses, str) else list(warehouses or FLOOR_DISPLAY)
    for wh in whs:
        shown = floor_display(wh, name)
        if shown != name:
            return shown
    return name


def floor_labels() -> dict[str, str]:
    """{stored floor name: what Stock Take shows} for every renamed floor.

    Keyed by the declared spelling; the browser matches ignoring case, the way
    the server compares floors. Not narrowed to the caller's scope: a label is
    not access, and old postings at a floor need it whatever the caller holds."""
    out: dict[str, str] = {}
    for wh, names in FLOOR_DISPLAY.items():
        for key, shown in names.items():
            declared = next((f for f in FLOORS_BY_WAREHOUSE.get(wh, []) if f.upper() == key), key.title())
            out[declared] = shown
    return out


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
