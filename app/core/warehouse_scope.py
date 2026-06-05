"""Warehouse-code matching that tolerates "A185" vs "A-185" vs "a 185".

Mirror of web_replica/src/lib/warehouseScope.ts so server-side filters and
frontend filters collapse the same way. Without this, an admin who saved
'A185' to a user's allowed_warehouses sees zero JCs / plans for that user
when the JC/plan rows were created with legacy 'A-185'.

Use ``normalise_warehouse_code`` for in-process comparisons and
``WAREHOUSE_NORM_SQL`` for column-level comparisons inside a query.
"""

from __future__ import annotations

import re
from typing import Iterable

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def normalise_warehouse_code(value: str | None) -> str:
    """Uppercase, strip every non-alphanumeric. '' for None/empty."""
    if not value:
        return ""
    return _NON_ALNUM.sub("", value.strip().upper())


def user_has_warehouse(
    user_warehouses: Iterable[str] | None,
    code: str | None,
) -> bool:
    """True when ``code`` matches any of ``user_warehouses`` after
    normalising both sides. Empty/None on either side returns False —
    callers that want admin/wildcard semantics must check that themselves."""
    target = normalise_warehouse_code(code)
    if not target:
        return False
    if not user_warehouses:
        return False
    return any(normalise_warehouse_code(w) == target for w in user_warehouses)


def user_has_any_warehouse(
    user_warehouses: Iterable[str] | None,
    aliases: Iterable[str],
) -> bool:
    return any(user_has_warehouse(user_warehouses, a) for a in aliases)


def WAREHOUSE_NORM_SQL(expr: str) -> str:
    return f"regexp_replace(upper(coalesce({expr}, '')), '[^A-Z0-9]', '', 'g')"


def WAREHOUSE_NORM_ANY_SQL(col_expr: str, param_placeholder: str) -> str:
    return (
        f"{WAREHOUSE_NORM_SQL(col_expr)} = ANY("
        f"  SELECT {WAREHOUSE_NORM_SQL('w')} "
        f"  FROM unnest({param_placeholder}::text[]) AS w"
        f")"
    )
