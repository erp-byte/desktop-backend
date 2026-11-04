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

The godown names come from the ERP's one site table (app.core.sites, the same
names the transfer screens use); the ledger keeps its own list of the spellings it
groups, and its own exact-spelling lookup, so its grouping is as it was. Where the
two differ, on purpose:
  * bare 'savla' is no site in the table (it does not say which Savla store); the
    ledger still groups it under Savla D-39 and logs it (AMBIGUOUS_ALIASES);
  * spellings the table knows but the ledger does not list stay godowns of their
    own here: 'Savla D-34', 'A185 Cold', 'APMC F53', 'Savla D-39 Cold Storage',
    'Savla D-514 Cold Storage', and any spelling that differs only by punctuation
    or spacing ('W-202', 'A 185'). Adding them would move rows between godowns.
"""
from __future__ import annotations

from app.core.sites import canonical_site

UNASSIGNED = "Unassigned"

# The spellings the ledger groups. Keys are normalised: strip().lower().replace("_", " ")
_LEDGER_SPELLINGS: tuple[str, ...] = (
    # Savla D-39
    "savla d-39", "savla d39", "savla-d39", "savla-d-39", "d-39", "d39", "old savla",
    "savla d-39 cold", "savla d39 cold",
    # Savla D-514
    "savla d-514", "savla d514", "savla-d514", "savla-d-514", "d-514", "d514", "new savla",
    "savla d-514 cold", "savla d514 cold",
    # Savla Bond — its own godown, as in the site table
    "savla bond",
    # Cold storages
    "rishi", "rishi cold", "rishi cold storage", "rishi cold storage pvt ltd",
    "supreme", "supreme cold", "supreme cold storage",
    "eskimo", "eskimo cold", "eskimo cold storage",
    # Regular warehouses
    "w202", "warehouse w202", "a101", "warehouse a101",
    "a185", "warehouse a185", "a-185", "a-185 cold",
    "a68", "warehouse a68", "f53", "warehouse f53",
    "dev int",
)

# Where the ledger names a godown the site table does not.
_LEDGER_ONLY: dict[str, str] = {
    "savla": "Savla D-39",          # ambiguous — see AMBIGUOUS_ALIASES
}

GODOWN_ALIASES: dict[str, str] = {
    **{spelling: canonical_site(spelling) for spelling in _LEDGER_SPELLINGS},
    **_LEDGER_ONLY,
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
