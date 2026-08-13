"""GET /api/v1/ledger — read-only feed for the Inventory Ledger module.

The frontend derives every screen (stock summary tree, group drill, item hub,
ageing, FIFO) from one flat leaf feed, so this single endpoint drives the module.

Only the Inward column is sourced. The other six movement columns are zero, which
means the derived Closing is NOT a stock balance — the UI renders an "Inward only"
chip to prevent that being misread.

Read-only by design. No POST/PATCH/DELETE on this router.
"""
from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query, Request

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.ledger.services.leaves_service import ENTITIES, fetch_leaves

router = APIRouter(prefix="/api/v1/ledger", tags=["Ledger"])


@router.get("/leaves")
async def list_leaves(
    request: Request,
    entity: str = Query(
        "both",
        description="Entity scope: cfpl, cdpl, or both.",
    ),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, list[dict[str, Any]]]:
    """Flat inward leaf rows, one per SKU x godown x material type x entity."""
    if entity not in (*ENTITIES, "both"):
        entity = "both"

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            data = await fetch_leaves(conn, entity=entity)
        except asyncpg.UndefinedTableError:
            # A legacy inward table is absent in this environment. The frontend
            # already renders an empty state; a 500 would just be noise.
            return {"data": []}
    return {"data": data}
