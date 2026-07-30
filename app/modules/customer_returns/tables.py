"""Company -> physical table-name resolution for the customer-returns module.

The company string is whitelisted to a fixed prefix; table names are never
f-strings of raw input (SQL-injection guard, mirrors transfer/stock_service).

CFERP shares the LIVE legacy `_rtv_*` tables that the IMS app reads/writes (the
production source of truth: rtv_header PK `id` + UNIQUE `rtv_id`, lines/boxes FK
`header_id`). The read side (query_service) joins lines/boxes via `header_id`;
the write side is gated read-only for now (see router `_ensure_writable`) until
the `_rtv_*`-shaped write path lands. The module's own `_customer_return_*`
tables are unused while pointed here.
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
        "header": f"{prefix}_rtv_header",
        "lines": f"{prefix}_rtv_lines",
        "boxes": f"{prefix}_rtv_boxes",
    }
