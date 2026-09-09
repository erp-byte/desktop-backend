"""cold_unit -> physical cold-storage table resolution.

Ported from job_work_server._resolve_cold_table. The returned name is
interpolated into SQL, so it can only ever be one of the two literals in
`_COLD_TABLES` — never a caller-supplied string (SQL-injection guard, same rule
as transfer/stock_service and customer_returns/tables).
"""
from __future__ import annotations

# The only table names this module will ever put in a query.
_COLD_TABLES = ("cfpl_cold_stocks", "cdpl_cold_stocks")

_BY_CODE = {"cfpl": "cfpl_cold_stocks", "cdpl": "cdpl_cold_stocks"}

# Operators pick a cold store by display name, not company code, so the form
# sends "Savla D-39" / "Rishi" / "Eskimo" rather than cfpl/cdpl. Substring match
# because the exact label has drifted over time ("Savla D39", "SAVLA D-39 CFPL").
_BY_DISPLAY = (
    (("savla", "d-39", "d39"), "cfpl_cold_stocks"),
    (("rishi", "eskimo"),      "cdpl_cold_stocks"),
)


def resolve_cold_table(cold_unit: str | None) -> str | None:
    """Physical cold_stocks table for a cold_unit, or None if unrecognised.

    None means "do not deduct" — the caller skips the line rather than guessing.
    Silently picking a default would delete the wrong company's stock.
    """
    if not cold_unit:
        return None
    cu = cold_unit.strip().lower()
    if cu in _BY_CODE:
        return _BY_CODE[cu]
    for needles, table in _BY_DISPLAY:
        if any(n in cu for n in needles):
            return table
    return None


def company_of(table: str | None) -> str | None:
    """'cfpl_cold_stocks' -> 'CFPL'. For the disposition ledger's from_company."""
    if not table:
        return None
    return table.split("_", 1)[0].upper()
