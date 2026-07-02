"""Company -> physical table-name resolution for the customer-returns module.

The company string is whitelisted to a fixed prefix; table names are never
f-strings of raw input (SQL-injection guard, mirrors transfer/stock_service).
"""
from __future__ import annotations

from fastapi import HTTPException

_PREFIX = {"CFPL": "cfpl", "CDPL": "cdpl"}


def cr_table_names(company: str) -> dict:
    prefix = _PREFIX.get((company or "").strip().upper())
    if not prefix:
        raise HTTPException(
            400,
            detail={
                "error": "invalid_company",
                "message": "company must be CFPL or CDPL",
                "details": {"company": company},
            },
        )
    return {
        "header": f"{prefix}_customer_return_header",
        "lines": f"{prefix}_customer_return_lines",
        "boxes": f"{prefix}_customer_return_boxes",
    }
