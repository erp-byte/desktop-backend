"""/api/v1/cold-storage/* — Cold Storage module (doc 08 cold picker).

Currently the read endpoints the transfer-OUT form's cold-stock picker needs:
lot/group search and FIFO per-box pick. Reads require a valid token.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.cold_storage.services import search_service

router = APIRouter(prefix="/api/v1/cold-storage", tags=["Cold Storage"])


@router.get("/stocks/search")
async def search_stocks(
    request: Request,
    lot_no: Optional[str] = Query(None),
    item_description: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    inward_dt: Optional[str] = Query(None),
    unit: Optional[str] = Query(None),
    storage_location: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    company: Optional[str] = Query(None, description="ignored — searches cfpl then cdpl"),
    limit: int = Query(50, ge=1, le=500),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await search_service.search_cold_storage_stocks(
            conn, lot_no=lot_no, item_description=item_description, group_name=group_name,
            inward_dt=inward_dt, unit=unit, storage_location=storage_location, q=q, limit=limit)


@router.get("/stocks/pick-boxes")
async def pick_boxes(
    request: Request,
    company: str = Query(..., description="cfpl or cdpl"),
    item_description: str = Query(...),
    lot_no: str = Query(...),
    inward_no: str = Query(...),
    qty: int = Query(..., ge=1),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await search_service.pick_boxes(
            conn, company=company, item_description=item_description, lot_no=lot_no,
            inward_no=inward_no, qty=qty)
