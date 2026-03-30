"""Shared Pydantic types with decimal precision enforcement."""

from typing import Annotated

from pydantic import BeforeValidator


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
