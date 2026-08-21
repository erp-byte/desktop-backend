"""Warehouse -> canonical godown for the Inventory Ledger.

Raw `warehouse` values in the legacy inward tables are inconsistent ('savla d-39',
'd39', 'old savla', ...). Grouping the ledger on the raw column fragments one
physical godown across many rows.

Built from legacy_backend/shared/canonicalize.py (the authoritative copy, 11
canonical warehouses), merged with the hyphen variants that only exist in
legacy_backend/services/ims_service/inward_tools.py, plus two deliberate deltas:

  1. 'savla bond' becomes its own godown. Both legacy copies fold it into
     Savla D-39; the ledger keeps it separate by requirement.
  2. 'a-185' / 'a-185 cold' are added — the hyphenated form appears in real
     inventory data and matches nothing in any legacy copy.

Deliberately NOT named canonical_warehouse(): that name is taken by an arity-2
function in legacy_backend/shared/canonicalize.py which returns None for
unrecognised values. This one takes a single value and never returns None.
"""
from __future__ import annotations

UNASSIGNED = "Unassigned"

# Keys are normalised: strip().lower().replace("_", " ")
GODOWN_ALIASES: dict[str, str] = {
    # Savla D-39
    "savla d-39": "Savla D-39",
    "savla d39": "Savla D-39",
    "savla-d39": "Savla D-39",
    "savla-d-39": "Savla D-39",
    "d-39": "Savla D-39",
    "d39": "Savla D-39",
    "old savla": "Savla D-39",
    "savla d-39 cold": "Savla D-39",
    "savla d39 cold": "Savla D-39",
    "savla": "Savla D-39",          # ambiguous — see AMBIGUOUS_ALIASES
    # Savla D-514
    "savla d-514": "Savla D-514",
    "savla d514": "Savla D-514",
    "savla-d514": "Savla D-514",
    "savla-d-514": "Savla D-514",
    "d-514": "Savla D-514",
    "d514": "Savla D-514",
    "new savla": "Savla D-514",
    "savla d-514 cold": "Savla D-514",
    "savla d514 cold": "Savla D-514",
    # Savla Bond — split out from D-39
    "savla bond": "Savla Bond",
    # Cold storages
    "rishi": "Rishi",
    "rishi cold": "Rishi",
    "rishi cold storage": "Rishi",
    "rishi cold storage pvt ltd": "Rishi",
    "supreme": "Supreme",
    "supreme cold": "Supreme",
    "supreme cold storage": "Supreme",
    "eskimo": "Eskimo",
    "eskimo cold": "Eskimo",
    "eskimo cold storage": "Eskimo",
    # Regular warehouses
    "w202": "W202",
    "warehouse w202": "W202",
    "a101": "A101",
    "warehouse a101": "A101",
    "a185": "A185",
    "warehouse a185": "A185",
    "a-185": "A185",
    "a-185 cold": "A185",
    "a68": "A68",
    "warehouse a68": "A68",
    "f53": "F53",
    "warehouse f53": "F53",
    "dev int": "Dev Int",
}

# Normalised keys whose mapping is inherited guesswork rather than confirmed.
# The service logs how many rows resolve through these so the exposure is visible.
AMBIGUOUS_ALIASES: frozenset[str] = frozenset({"savla"})


def normalise(warehouse: str | None) -> str:
    """Lowercase, trim, and treat underscores as spaces — matching legacy rules."""
    if warehouse is None:
        return ""
    return warehouse.strip().lower().replace("_", " ")


def ledger_godown(warehouse: str | None) -> str:
    """Canonical godown name. Never returns None or an empty string.

    - missing/blank      -> "Unassigned"
    - recognised alias   -> canonical name
    - anything else      -> title-cased passthrough (never dropped)
    """
    key = normalise(warehouse)
    if not key:
        return UNASSIGNED
    mapped = GODOWN_ALIASES.get(key)
    if mapped is not None:
        return mapped
    return " ".join(word.capitalize() for word in key.split())
