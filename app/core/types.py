"""Shared Pydantic types with decimal precision enforcement."""

from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BeforeValidator


def _amount2(v):
    """STRICT money validator: reject more than 2 decimal places (don't silently
    round), reject negatives. None passes through. Use for amounts that must be
    'strictly two decimals' — unlike _round3, which rounds."""
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("amount must be a number")
    if d < 0:
        raise ValueError("amount must not be negative")
    if d.as_tuple().exponent < -2:
        raise ValueError("amount must have at most 2 decimal places")
    return float(d)


def _round3(v):
    """Round to 3 decimal places, pass through None."""
    if v is None:
        return None
    try:
        return round(float(v), 3)
    except (ValueError, TypeError):
        return None


def _round3_zero(v):
    """Round to 3 decimal places, default to 0.0 on None."""
    if v is None:
        return 0.0
    try:
        return round(float(v), 3)
    except (ValueError, TypeError):
        return 0.0


# Nullable float rounded to 3dp — use for optional numeric fields
Decimal3 = Annotated[float | None, BeforeValidator(_round3)]

# Non-null float rounded to 3dp, defaults None → 0.0 — use for charge fields with default 0
Decimal3Z = Annotated[float, BeforeValidator(_round3_zero)]

# Nullable money — at most 2 decimals, non-negative; rejects (not rounds) >2dp.
Amount2 = Annotated[float | None, BeforeValidator(_amount2)]
