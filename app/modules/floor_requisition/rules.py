"""Floor requisitions — the rules that need no database.

The unit a request carries, what quantity may be stored, and the snapshot of
what the floor was looking at when it asked. These mirror the job card tab's
arithmetic (web_replica/src/lib/floorStock.ts): the requirement is the article's
indent lines' gross_qty (falling back to reqd_qty), compared against the floor's
FRESH STOCK only, in the requirement's own unit.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

KG = "kg"
PCS = "pcs"
FRESH = "Fresh Stock"

_KG = {"kg", "kgs", "kilogram", "kilograms"}
_PCS = {"pc", "pcs", "piece", "pieces", "no", "nos", "unit", "units"}
_THOUSANDTH = Decimal("0.001")
# NUMERIC(14,3) holds up to 99,999,999,999.999.
_LIMIT = Decimal("100000000000")


def article_key(name: Optional[str]) -> str:
    """An article's identity: the stock-take module's UPPER(BTRIM(name))."""
    return (name or "").strip().upper()


def normalise_unit(uom: Optional[str]) -> Optional[str]:
    """'KGS' -> 'kg', 'PCS' / 'NOS' -> 'pcs'; None when it is neither."""
    s = (uom or "").strip().lower()
    if s in _KG:
        return KG
    if s in _PCS:
        return PCS
    return None


def unit_for(indent_uom: Optional[str], item_type: Optional[str]) -> str:
    """The request's unit: its indent line's, else pieces for PM, else kg."""
    return normalise_unit(indent_uom) or (PCS if article_key(item_type) == "PM" else KG)


class QtyError(ValueError):
    """A quantity that cannot be stored. The message is shown to the person as-is."""


def parse_qty(value: Any, unit: str) -> Decimal:
    """A positive quantity in `unit`: whole for pieces, at most 3 decimals for kg."""
    if isinstance(value, bool):
        raise QtyError("Enter a quantity as a number.")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise QtyError("Enter a quantity as a number.") from None
    if not d.is_finite():
        raise QtyError("Enter a quantity as a number.")
    if d <= 0:
        raise QtyError("The quantity must be more than 0.")
    if d >= _LIMIT:
        raise QtyError("That quantity is too large.")
    if unit == PCS and d != d.to_integral_value():
        raise QtyError("Pieces are whole numbers.")
    if d != d.quantize(_THOUSANDTH, rounding=ROUND_DOWN):
        raise QtyError("Kilograms go to 3 decimals at most.")
    return d.quantize(_THOUSANDTH)


def _num(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _round3(d: Decimal) -> Decimal:
    # `+ Decimal(0)` turns -0.000 into 0.000.
    return d.quantize(_THOUSANDTH, rounding=ROUND_HALF_UP) + Decimal(0)


@dataclass(frozen=True)
class Snapshot:
    unit: str
    required: Optional[Decimal]
    available: Decimal
    shortage: Optional[Decimal]


def snapshot(
    *,
    unit: str,
    indent_lines: Iterable[tuple[Any, Any, Any]],
    stock: Iterable[Mapping[str, Any]],
) -> Snapshot:
    """What the floor saw when it asked, for ONE article.

    indent_lines: (uom, gross_qty, reqd_qty) of the article's indent lines.
    stock: the article's floor-stock rows (floor_stock_service items).
    A line in another recognised unit cannot be added to this one and is skipped;
    a line with an unrecognised uom is counted. No usable line = no requirement,
    so no shortage either.
    """
    required: Optional[Decimal] = None
    for uom, gross, reqd in indent_lines:
        line_unit = normalise_unit(uom)
        if line_unit is not None and line_unit != unit:
            continue
        q = _num(gross)
        if q is None:
            q = _num(reqd)
        if q is None:
            continue
        required = q if required is None else required + q

    field = "available_kg" if unit == KG else "available_quantity"
    available = _round3(sum(
        (_num(s.get(field)) or Decimal(0) for s in stock if s.get("stock_type") == FRESH),
        Decimal(0),
    ))
    if required is None:
        return Snapshot(unit, None, available, None)
    required = _round3(required)
    return Snapshot(unit, required, available, _round3(max(required - available, Decimal(0))))


def actor_name(user) -> str:
    """Who did it, from the access token — never from the request body.

    The production router's _actor_name rule: a session may carry an empty
    full_name, so fall back to email, then phone, then the numeric id.
    """
    return user.full_name or user.email or user.phone or f"user:{user.user_id}"
