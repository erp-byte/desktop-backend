"""Shared helper functions used across modules."""


def safe_float(val) -> float | None:
    """Parse value to float rounded to 3dp, return None on failure."""
    if val is None:
        return None
    try:
        return round(float(val), 3)
    except (ValueError, TypeError):
        return None


def safe_float_zero(val) -> float:
    """Parse value to float rounded to 3dp, return 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return round(float(val), 3)
    except (ValueError, TypeError):
        return 0.0


def safe_str(val) -> str | None:
    """Strip string, return None if empty."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None
