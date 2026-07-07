"""GET /api/v1/lookups — read-only access to the `lookup_value` master.

Every vendor / SO / production form on the frontend pulls dropdown
options from this single endpoint, keyed by `lookup_type`. The shape
matches what the vendor-api client expects:

    [{lookup_id, code, label, display_order, is_active, parent_lookup_id}]

Active-only by default (matches the frontend's filter); pass
`include_inactive=true` to read archived values.

Read-only by design — populating `lookup_value` is an admin / data-ops
operation, not something application users do at runtime. No POST/PATCH/
DELETE on this router.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from app.modules.auth.middleware import AuthUser, get_current_user

router = APIRouter(prefix="/api/v1/lookups", tags=["Lookups"])


class LookupRow(BaseModel):
    model_config = ConfigDict(extra="allow")
    lookup_id: str
    lookup_type: str
    code: str | None = None
    label: str | None = None
    parent_lookup_id: str | None = None
    display_order: int | None = None
    is_active: bool = True


def _row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    # Drop non-JSON-friendly columns the UI never reads (created_at, effective dates).
    for k in ("created_at", "effective_from", "effective_to"):
        d.pop(k, None)
    return d


@router.get("", response_model=list[LookupRow])
@router.get("/", response_model=list[LookupRow], include_in_schema=False)
async def list_lookups(
    request: Request,
    type: str = Query(
        ...,
        alias="type",
        description="lookup_type filter (e.g. SUPPLIER_TYPE, FIRM_STATUS, CATEGORY_CODE).",
    ),
    include_inactive: bool = Query(
        False,
        description="Set true to include rows where is_active = false.",
    ),
    parent_lookup_id: str | None = Query(
        None,
        description="For dependent lookups — limits to children of this parent.",
    ),
    user: AuthUser = Depends(get_current_user),
):
    """Active rows of one lookup_type, ordered by display_order then label.

    Effective-date filter: drops rows whose effective_to is in the past
    (so retired values disappear automatically). Future-dated rows are
    kept — they're "scheduled to activate" and the UI may want to show
    them as preview.
    """
    pool = request.app.state.db_pool
    where = ["lookup_type = $1", "(effective_to IS NULL OR effective_to >= current_date)"]
    args: list[Any] = [type]
    if not include_inactive:
        where.append("is_active = true")
    if parent_lookup_id:
        args.append(parent_lookup_id)
        where.append(f"parent_lookup_id = ${len(args)}")

    sql = (
        "SELECT lookup_id, lookup_type, code, label, parent_lookup_id, "
        "       display_order, is_active "
        "  FROM lookup_value "
        f" WHERE {' AND '.join(where)} "
        "  ORDER BY display_order NULLS LAST, label NULLS LAST"
    )
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(sql, *args)
        except asyncpg.UndefinedTableError:
            # Defensive — if a fresh DB hasn't bootstrapped lookup_value yet,
            # return empty rather than 500. Frontend already handles [].
            return []
    return [LookupRow.model_validate(_row_to_dict(r)) for r in rows]


@router.get("/types", response_model=list[str])
async def list_lookup_types(
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    """Enumerate the distinct lookup_type values currently in the table.

    Useful for admin tools that want to discover what lookup families
    exist without hardcoding the list.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT DISTINCT lookup_type FROM lookup_value "
                " WHERE is_active = true ORDER BY lookup_type"
            )
        except asyncpg.UndefinedTableError:
            return []
    return [r["lookup_type"] for r in rows]
