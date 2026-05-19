"""Amendment tracking top-level router — mounted at /api/v1/amendments."""
from fastapi import APIRouter, Request, Query

router = APIRouter(prefix="/api/v1/amendments", tags=["Amendments"])


@router.get("")
async def get_amendments(
    request: Request,
    record_id: str = Query(...),
    record_type: str = Query(...),
    field: str = Query(None),
):
    from app.modules.production.services.amendment_service import get_amendments as _get
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await _get(conn, record_id=record_id, record_type=record_type, field=field)


@router.get("/count")
async def get_amendment_count(
    request: Request,
    record_id: str = Query(...),
    record_type: str = Query(...),
):
    from app.modules.production.services.amendment_service import get_amendment_count as _count
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await _count(conn, record_id=record_id, record_type=record_type)
