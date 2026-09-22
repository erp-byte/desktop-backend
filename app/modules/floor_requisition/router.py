"""/api/v1/floor-requisitions/* — the floor asks store for material.

    GET  /api/v1/floor-requisitions                          list (view)
    POST /api/v1/floor-requisitions                          raise (create)
    POST /api/v1/floor-requisitions/{requisition_id}/issue   store issues (issue)
    POST /api/v1/floor-requisitions/{requisition_id}/receive floor confirms (receive)
    POST /api/v1/floor-requisitions/{requisition_id}/cancel  while raised (cancel)

    GET    /api/v1/floor-requisitions/{requisition_id}/boxes            boxes sent, paged; ?find= (view)
    POST   /api/v1/floor-requisitions/{requisition_id}/boxes/scan       scan one (issue)
    POST   /api/v1/floor-requisitions/{requisition_id}/boxes/print      manual print (issue)
    DELETE /api/v1/floor-requisitions/{requisition_id}/boxes/{box_code} remove one (issue)

Raised from the job card's "Material allocation and requisition" tab; issued from
Production → Floor Requisitions or Stores → Production Indents. Once a raise has
committed, the store_head users covering its place are told by email (and by
WhatsApp once a template is configured) — services/notify_service.py, run as a
background task so it can neither slow down nor fail the request. Gated on production.floor_requisitions.*
(app/db/111_floor_requisition.sql): floor_manager raises / receives, store_head
issues, both may cancel. Every step is also limited to the caller's granted
warehouses and floors (stock_take.place_scope).

The actor of each step is the access token's user; no body field can name one.

A NEW module rather than more routes on production/router.py, which is past 7k
lines — the reasoning the stock_take and BOM modules record.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.floor_requisition.services import box_service as box_svc
from app.modules.floor_requisition.services import notify_service
from app.modules.floor_requisition.services import requisition_service as svc

router = APIRouter(prefix="/api/v1/floor-requisitions", tags=["Floor Requisitions"])


def _perm(action: str):
    return require_permission("production", "floor_requisitions", action=action)


class RaiseBody(BaseModel):
    job_card_id: int
    material_sku_name: str = Field(..., min_length=1, max_length=500)
    # A number or its text; the unit rules are applied by the service.
    requested_qty: Union[float, str]
    note: Optional[str] = Field(None, max_length=500)


class IssueBody(BaseModel):
    issued_qty: Union[float, str]
    issue_note: Optional[str] = Field(None, max_length=500)


class CancelBody(BaseModel):
    reason: str = Field("", max_length=500)


class ScanBoxBody(BaseModel):
    # The raw QR: Material-In's {"tx","bi"} or a bare box id.
    code: str = Field(..., min_length=1, max_length=2000)


class PrintBoxLine(BaseModel):
    box_number: int = Field(..., ge=1)
    net_weight: float
    gross_weight: Optional[float] = None
    count: Optional[int] = Field(None, ge=0)
    lot_number: Optional[str] = Field(None, max_length=100)


class PrintBoxesBody(BaseModel):
    article: str = Field(..., min_length=1, max_length=500)
    stock_type: Literal["Fresh Stock", "Off Grade/Rejection"] = "Fresh Stock"
    boxes: list[PrintBoxLine] = Field(..., min_length=1, max_length=500)


def _http(exc: svc.RequisitionError) -> HTTPException:
    return HTTPException(exc.status, detail={
        "error": exc.error, "message": exc.message, "details": exc.details})


@router.get("")
async def list_floor_requisitions(
    request: Request,
    status: Optional[Literal["raised", "issued", "received", "cancelled"]] = Query(None),
    warehouse: Optional[str] = Query(None, description="W-202 and W202 both work"),
    floor_name: Optional[str] = Query(None, alias="floorName"),
    job_card_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=svc.MAX_PAGE_SIZE),
    user: AuthUser = Depends(_perm("view")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await svc.list_requisitions(
            conn, user, status=status, warehouse=warehouse, floor=floor_name,
            job_card_id=job_card_id, search=search, page=page, page_size=page_size)


@router.post("")
async def raise_floor_requisition(
    request: Request, body: RaiseBody, background_tasks: BackgroundTasks,
    user: AuthUser = Depends(_perm("create")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await svc.raise_requisition(
                    conn, user, job_card_id=body.job_card_id,
                    material_sku_name=body.material_sku_name,
                    requested_qty=body.requested_qty, note=body.note)
    except svc.RequisitionError as exc:
        raise _http(exc) from None
    # Committed: tell store. Scheduled only here, after the transaction block, so a
    # refused or rolled-back raise tells nobody; it runs once the response is sent.
    background_tasks.add_task(notify_service.notify_store_of_raise, pool, row)
    return row


@router.post("/{requisition_id}/issue")
async def issue_floor_requisition(
    request: Request, requisition_id: int, body: IssueBody,
    user: AuthUser = Depends(_perm("issue")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await svc.issue_requisition(
                    conn, user, requisition_id,
                    issued_qty=body.issued_qty, issue_note=body.issue_note)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.post("/{requisition_id}/receive")
async def receive_floor_requisition(
    request: Request, requisition_id: int, user: AuthUser = Depends(_perm("receive")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await svc.receive_requisition(conn, user, requisition_id)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.post("/{requisition_id}/cancel")
async def cancel_floor_requisition(
    request: Request, requisition_id: int, body: CancelBody,
    user: AuthUser = Depends(_perm("cancel")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await svc.cancel_requisition(conn, user, requisition_id, reason=body.reason)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.get("/{requisition_id}/boxes")
async def list_requisition_boxes(
    request: Request, requisition_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(box_svc.DEFAULT_PAGE_SIZE, ge=1, le=box_svc.MAX_PAGE_SIZE),
    # A box id, a scanned sticker's QR, or a sticker "Box #": opens the page holding it.
    find: Optional[str] = Query(None, max_length=2000),
    user: AuthUser = Depends(_perm("view")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            # One snapshot for the page, the totals and find: a scan committing between
            # the reads would otherwise move the found box off the page that names it.
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                return await box_svc.list_boxes(conn, user, requisition_id, page=page,
                                                page_size=page_size, find=find)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.post("/{requisition_id}/boxes/scan")
async def scan_requisition_box(
    request: Request, requisition_id: int, body: ScanBoxBody,
    user: AuthUser = Depends(_perm("issue")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        # No transaction: the lookup can fail a statement on schema drift, which
        # would poison one (box_service docstring). Its one write is guarded itself.
        async with pool.acquire() as conn:
            return await box_svc.scan_box(conn, user, requisition_id, code=body.code)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.post("/{requisition_id}/boxes/print")
async def print_requisition_boxes(
    request: Request, requisition_id: int, body: PrintBoxesBody,
    user: AuthUser = Depends(_perm("issue")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await box_svc.print_boxes(
                    conn, user, requisition_id, article=body.article, stock_type=body.stock_type,
                    boxes=[b.model_dump() for b in body.boxes])
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.delete("/{requisition_id}/boxes/{box_code}")
async def remove_requisition_box(
    request: Request, requisition_id: int, box_code: str,
    user: AuthUser = Depends(_perm("issue")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await box_svc.remove_box(conn, user, requisition_id, box_code)
    except svc.RequisitionError as exc:
        raise _http(exc) from None
