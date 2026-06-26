"""UOM master + per-item-kind allowed lists.

Source: the operations team's bill-of-units sheet:

    Universal    → GMS, KGS, LTR, NOS, PCS, ROLL, SETS, BUNDLE
    RM           → KGS, GMS, LTRS (rarely), NOS (rarely)
    FG           → KGS, LTRS (rarely), NOS (mass), PCS
    PM           → KGS (rolls/films), NOS (all), ROLL (rolls/films),
                   SETS (monocarton + tray + outer sleeve), PCS,
                   BUNDLES (carton) — depends on bill

The lists are advisory; the canonical source of truth for "this particular
RM uses KGS" is the BOM line. This module gates which UoMs are *acceptable*
for a given item kind in admin / planning UI so we don't end up with an
RM measured in BUNDLES by accident.
"""

from __future__ import annotations

# Canonical universal list — anything not in here is rejected outright.
UOM_UNIVERSAL: list[str] = [
    'GMS', 'KGS', 'LTR', 'LTRS', 'NOS', 'PCS', 'ROLL', 'SETS', 'BUNDLE',
]

# Per-kind allow-lists. Keep these in sync with the ops sheet — when the
# sheet changes, update both this constant AND the comment block above.
ALLOWED_BY_KIND: dict[str, list[str]] = {
    'RM': ['KGS', 'GMS', 'LTRS', 'NOS'],
    'FG': ['KGS', 'LTRS', 'NOS', 'PCS'],
    'PM': ['KGS', 'NOS', 'ROLL', 'SETS', 'PCS', 'BUNDLE'],
    # WIP / SFG inherit the FG kind's list — they're FG-shaped material in
    # flight between stages. Listed separately so callers don't fall back
    # to "RM" by accident on intermediate stages.
    'WIP': ['KGS', 'LTRS', 'NOS', 'PCS'],
    'SFG': ['KGS', 'LTRS', 'NOS', 'PCS'],
}


def normalise(uom: str | None) -> str | None:
    """Strip + uppercase. Returns None for empty input."""
    if uom is None:
        return None
    s = uom.strip().upper()
    return s or None


def is_valid_universal(uom: str | None) -> bool:
    """`uom` is one of the canonical universal values (case-insensitive)."""
    n = normalise(uom)
    return n in UOM_UNIVERSAL if n else False


def is_valid_for_kind(uom: str | None, kind: str) -> bool:
    """`uom` is acceptable for the given item kind (RM / FG / PM / WIP / SFG)."""
    allowed = ALLOWED_BY_KIND.get(kind)
    if not allowed:
        return False
    n = normalise(uom)
    return n in allowed if n else False
