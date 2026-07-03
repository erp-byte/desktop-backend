"""/api/v1/customer-returns/* — Customer-Returns module (Phase 1: header+lines CRUD).

Thin router; every endpoint requires a valid access token and derives the actor
from the JWT (never from request params). Company is a CFPL/CDPL path segment
mapped to a table prefix by the service layer.
"""
from __future__ import annotations

from io import BytesIO
from typing import Optional
from zoneinfo import ZoneInfo
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import (
    create_service, query_service, box_service, export_xlsx,
)

router = APIRouter(prefix="/api/v1/customer-returns", tags=["Customer Returns"])
_IST = ZoneInfo("Asia/Kolkata")


def _actor(user: AuthUser) -> str:
    return user.email or user.full_name or str(user.user_id)


@router.get("/export")
async def export_customer_returns(
    request: Request,
    company: str = Query(..., description="CFPL or CDPL"),
    status: Optional[str] = Query(None),
    customer: Optional[str] = Query(None),
    factory_unit: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    to_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    sort_by: str = Query("created_ts"),
    sort_order: str = Query("desc"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await query_service.export_cr_records(
            conn, company=company, status=status, customer=customer,
            factory_unit=factory_unit, from_date=from_date, to_date=to_date,
            sort_by=sort_by, sort_order=sort_order,
        )
        rtv_ids = list({r["RTV ID"] for r in rows if r["RTV ID"]})
        edited = await query_service.get_edited_cells(conn, rtv_ids)
    buf: BytesIO = export_xlsx.build_export_workbook(rows, edited)
    stamp = datetime.now(_IST).strftime("%Y%m%d")
    filename = f"customer_returns_{company}_{stamp}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/box-edit-log", response_model=schemas.CRBoxEditLogResponse)
async def log_customer_return_box_edits(
    body: schemas.CRBoxEditLogRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.log_box_edits(conn, body, email_id=_actor(user))


@router.post("/{company}", status_code=201, response_model=schemas.CRWithDetails)
async def create_customer_return(
    company: str,
    body: schemas.CRCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.create_cr(conn, company, body, _actor(user))


@router.get("/{company}", response_model=schemas.CRListResponse)
async def list_customer_returns(
    company: str,
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    status: Optional[str] = Query(None),
    factory_unit: Optional[str] = Query(None),
    customer: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    to_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    sort_by: str = Query("created_ts"),
    sort_order: str = Query("desc"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.list_crs(
            conn, company=company, page=page, per_page=per_page, status=status,
            factory_unit=factory_unit, customer=customer, from_date=from_date,
            to_date=to_date, sort_by=sort_by, sort_order=sort_order,
        )


@router.get("/{company}/{cr_id}", response_model=schemas.CRWithDetails)
async def get_customer_return(
    company: str,
    cr_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.get_cr(conn, company, cr_id)


@router.put("/{company}/{cr_id}", response_model=schemas.CRHeaderResponse)
async def update_customer_return(
    company: str,
    cr_id: str,
    body: schemas.CRHeaderUpdate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.update_cr(conn, company, cr_id, body)


@router.put("/{company}/{cr_id}/lines", response_model=schemas.CRLinesUpdateResponse)
async def update_customer_return_lines(
    company: str,
    cr_id: str,
    body: schemas.CRLinesUpdateRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.update_cr_lines(conn, company, cr_id, body)


@router.delete("/{company}/{cr_id}", response_model=schemas.CRDeleteResponse)
async def delete_customer_return(
    company: str,
    cr_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.delete_cr(conn, company, cr_id)


@router.put("/{company}/{cr_id}/box", response_model=schemas.CRBoxUpsertResponse)
async def upsert_customer_return_box(
    company: str,
    cr_id: str,
    body: schemas.CRBoxUpsertRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.upsert_box(conn, company, cr_id, body)


@router.put("/{company}/{cr_id}/boxes", response_model=schemas.CRBulkBoxUpdateResponse)
async def bulk_save_customer_return_boxes(
    company: str,
    cr_id: str,
    body: schemas.CRBulkBoxUpdateRequest,
    request: Request,
    notify_discrepancy: bool = Query(True),
    allow_clear: bool = Query(False),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.bulk_save_boxes(
            conn, company, cr_id, body,
            notify_discrepancy=notify_discrepancy, allow_clear=allow_clear)
