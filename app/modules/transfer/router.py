"""/api/v1/transfer/* — Inter-Unit Transfer module endpoints.

Phase 1: the Transfer Dashboard surface — list/detail reads for requests,
transfers (OUT) and transfer-ins (IN); the in-transit pending-stock list +
backfill; the four delete actions; and the inner-cold tab list + delete.

Reads require only a valid token (production had no page-level gate on viewing
the dashboard). Mutations go through `require_transfer_mutate`.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.transfer import permissions, schemas
from app.modules.transfer.services import (
    create_service,
    dashboard_service,
    delete_service,
    dropdown_service,
    inner_cold_service,
    lookup_service,
    pending_service,
    query_service,
    receive_service,
    request_service,
)

router = APIRouter(prefix="/api/v1/transfer", tags=["Transfer"])


# ── Summary dashboard (doc 04) ────────────────────────────────────────────
# All transfer-line records loaded once; KPIs/filters/aggregation client-side.
@router.get("/dashboard/all-data")
async def dashboard_all_data(
    request: Request,
    from_date: Optional[str] = Query(None, description="YYYY-MM-DD lower bound on stock_trf_date"),
    to_date: Optional[str] = Query(None, description="YYYY-MM-DD upper bound on stock_trf_date"),
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_view_dashboard(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await dashboard_service.get_all_data(
            conn, scope=permissions.scope_warehouses(user),
            from_date=from_date, to_date=to_date)


@router.get("/dashboard/filter-options")
async def dashboard_filter_options(
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_view_dashboard(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await dashboard_service.get_filter_options(
            conn, scope=permissions.scope_warehouses(user))


# ── Requests ──────────────────────────────────────────────────────────────
@router.get("/requests", response_model=schemas.RequestListResponse)
async def list_requests(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(15, ge=1, le=1000),
    status: Optional[str] = Query(None),
    from_warehouse: Optional[str] = Query(None),
    to_warehouse: Optional[str] = Query(None),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.list_requests(
            conn, page=page, per_page=per_page, status=status,
            from_warehouse=from_warehouse, to_warehouse=to_warehouse,
            scope=permissions.scope_warehouses(user),
        )


@router.post("/requests", response_model=schemas.RequestWithLines, status_code=201)
async def create_request(
    request: Request,
    body: schemas.RequestCreate,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await request_service.create_request(conn, body, user.email or "unknown")


# ── Dropdowns / lookups (New Request form) ────────────────────────────────────
@router.get("/dropdowns/warehouse-sites", response_model=list[schemas.WarehouseSiteResponse])
async def list_warehouse_sites(
    request: Request,
    active_only: bool = Query(True),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await dropdown_service.get_warehouse_sites(conn, active_only)


@router.get("/categorial-search", response_model=schemas.CategorialSearchResponse)
async def categorial_search(
    request: Request,
    search: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await dropdown_service.categorial_search(conn, search, limit, offset)


@router.get("/categorial-dropdown", response_model=schemas.CategorialDropdownResponse)
async def categorial_dropdown(
    request: Request,
    material_type: Optional[str] = Query(None),
    item_category: Optional[str] = Query(None),
    sub_category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await dropdown_service.categorial_dropdown(
            conn, material_type, item_category, sub_category, search, limit, offset)


@router.get("/requests/{request_id}", response_model=schemas.RequestWithLines)
async def get_request(
    request: Request,
    request_id: int,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.get_request(conn, request_id)


@router.delete("/requests/{request_id}", response_model=schemas.DeleteResponse)
async def delete_request(
    request: Request,
    request_id: int,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_delete_request(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await delete_service.delete_request(conn, request_id)


# ── Transfers (OUT) ─────────────────────────────────────────────────────────
@router.get("/transfers", response_model=schemas.TransferListResponse)
async def list_transfers(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(15, ge=1, le=1000),
    status: Optional[str] = Query(None),
    from_site: Optional[str] = Query(None),
    to_site: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    challan_no: Optional[str] = Query(None),
    sort_by: str = Query("created_ts"),
    sort_order: str = Query("desc"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.list_transfers(
            conn, page=page, per_page=per_page, status=status,
            from_site=from_site, to_site=to_site, from_date=from_date,
            to_date=to_date, challan_no=challan_no,
            sort_by=sort_by, sort_order=sort_order,
            scope=permissions.scope_warehouses(user),
        )


@router.post("/transfers", response_model=schemas.TransferWithLines, status_code=201)
async def create_transfer(
    request: Request,
    body: schemas.TransferCreate,
    user: AuthUser = Depends(get_current_user),
):
    # Authenticated operators create dispatches (parity with create_request — no
    # extra gate; the page itself has no role gate). Deduction is atomic in the service.
    # Profile lock: a warehouse-bound operator may only dispatch FROM their warehouse.
    permissions.assert_can_dispatch_from(user, body.header.from_warehouse)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.create_transfer(conn, body, user.email or "unknown")


# ── Box lookups (transfer-OUT form: manual entry + TR-/BE- QR) ───────────────
@router.get("/box-lookup/{company}")
async def box_lookup(
    request: Request,
    company: str,
    box_number: int = Query(..., description="Box number"),
    transaction_no: str = Query(..., description="Transaction number"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await lookup_service.get_box_by_number(conn, company, box_number, transaction_no)


@router.get("/box-lookup-by-id/{company}")
async def box_lookup_by_id(
    request: Request,
    company: str,
    box_id: str = Query(..., description="Box ID from a TR- QR"),
    transaction_no: str = Query(..., description="Transaction number"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await lookup_service.get_box_by_box_id(conn, company, box_id, transaction_no)


@router.get("/bulk-entry-box-lookup/{company}")
async def bulk_entry_box_lookup(
    request: Request,
    company: str,
    box_id: str = Query(..., description="Box ID from a BE- QR"),
    transaction_no: str = Query(..., description="Transaction number starting with BE-"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await lookup_service.get_bulk_entry_box(conn, company, box_id, transaction_no)


@router.put("/transfers/{transfer_id}", response_model=schemas.TransferWithLines)
async def update_transfer(
    request: Request,
    transfer_id: int,
    body: schemas.TransferCreate,
    user: AuthUser = Depends(get_current_user),
):
    # Edit (doc 08, ?editId). Authenticated operators edit dispatches (parity with create);
    # the dashboard gates editing of Received/Completed transfers.
    # Profile lock: the edited from_warehouse must stay within the operator's scope.
    permissions.assert_can_dispatch_from(user, body.header.from_warehouse)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.update_transfer(conn, transfer_id, body)


@router.get("/transfers/{transfer_id}", response_model=schemas.TransferWithLines)
async def get_transfer(
    request: Request,
    transfer_id: int,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.get_transfer(conn, transfer_id)


@router.delete("/transfers/{transfer_id}", response_model=schemas.TransferDeleteResponse)
async def delete_transfer(
    request: Request,
    transfer_id: int,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_delete_transfer(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await delete_service.delete_transfer(conn, transfer_id)


# ── Transfers IN (GRN) ────────────────────────────────────────────────────────
@router.get("/transfer-in", response_model=schemas.TransferInListResponse)
async def list_transfer_ins(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(15, ge=1, le=1000),
    receiving_warehouse: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.list_transfer_ins(
            conn, page=page, per_page=per_page,
            receiving_warehouse=receiving_warehouse,
            from_date=from_date, to_date=to_date,
            sort_by=sort_by, sort_order=sort_order,
            scope=permissions.scope_warehouses(user),
        )


# ── Transfer-IN receive lifecycle (P8b) ───────────────────────────────────────
# Declared BEFORE /transfer-in/{transfer_in_id} so the literal `pending` /
# `acknowledge` paths are never captured as a numeric id.
@router.post("/transfer-in", status_code=201)
async def create_transfer_in(
    request: Request,
    body: dict | None = None,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_acknowledge(user)
    # Profile lock (bulk receipt): receiving warehouse must be in the operator's scope.
    permissions.assert_can_receive_into(user, (body or {}).get("receiving_warehouse"))
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.create_transfer_in(conn, body)


@router.post("/transfer-in/pending", status_code=201)
async def create_pending_transfer_in(
    request: Request,
    body: schemas.PendingTransferInCreate,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_acknowledge(user)
    # Profile lock: a warehouse-bound operator may only receive INTO their warehouse.
    permissions.assert_can_receive_into(user, body.receiving_warehouse)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.create_pending_transfer_in(conn, body)


@router.get("/transfer-in/pending/by-transfer-out/{transfer_out_id}")
async def get_pending_by_transfer_out(
    request: Request,
    transfer_out_id: int,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.get_pending_by_transfer_out(conn, transfer_out_id)


@router.post("/transfer-in/{header_id}/acknowledge")
async def acknowledge_box(
    request: Request,
    header_id: int,
    body: schemas.PendingBoxAcknowledge,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_acknowledge(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.acknowledge_pending_box(conn, header_id, body)


@router.post("/transfer-in/{header_id}/acknowledge-batch")
async def acknowledge_batch(
    request: Request,
    header_id: int,
    body: List[schemas.PendingBoxAcknowledge],
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_acknowledge(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.acknowledge_pending_boxes_batch(conn, header_id, body)


@router.delete("/transfer-in/{header_id}/acknowledge/{box_id:path}")
async def unacknowledge_box(
    request: Request,
    header_id: int,
    box_id: str,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_acknowledge(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.unacknowledge_pending_box(conn, header_id, box_id)


@router.post("/transfer-in/{header_id}/finalize")
async def finalize_transfer_in(
    request: Request,
    header_id: int,
    body: schemas.FinalizeTransferIn,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_acknowledge(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.finalize_transfer_in(conn, header_id, body)


@router.post("/transfer-in/{header_id}/reopen")
async def reopen_transfer_in(
    request: Request,
    header_id: int,
    body: schemas.ReopenTransferIn | None = None,
    user: AuthUser = Depends(get_current_user),
):
    # Reverses a Received GRN back to Pending (un-posts stock) for corrections.
    permissions.assert_can_reopen(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.reopen_transfer_in(conn, header_id, user.email or "system")


@router.post("/transfer-in/{header_id}/close-with-shortage")
async def close_transfer_in_with_shortage(
    request: Request,
    header_id: int,
    body: schemas.CloseWithShortage,
    user: AuthUser = Depends(get_current_user),
):
    # Finalize the acknowledged boxes and write off the un-received shortfall.
    permissions.assert_can_acknowledge(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.close_with_shortage(conn, header_id, body.shortage_reason, user.email or "system")


@router.put("/transfer-in/{transfer_in_id}")
async def edit_transfer_in(
    request: Request,
    transfer_in_id: int,
    body: schemas.EditTransferIn,
    user: AuthUser = Depends(get_current_user),
):
    # Edit a Received GRN: header-only edits update in place; per-box edits reverse,
    # correct, and re-post the destination stock.
    permissions.assert_can_reopen(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.edit_transfer_in(conn, transfer_in_id, body, user.email or "system")


@router.get("/transfer-in/{transfer_in_id}", response_model=schemas.TransferInDetail)
async def get_transfer_in(
    request: Request,
    transfer_in_id: int,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.get_transfer_in(conn, transfer_in_id)


@router.delete("/transfer-in/{transfer_in_id}", response_model=schemas.DeleteResponse)
async def delete_transfer_in(
    request: Request,
    transfer_in_id: int,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_delete_transfer_in(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await delete_service.delete_transfer_in(conn, transfer_in_id, user.email)


# ── Pending stock (in-transit) ────────────────────────────────────────────────
@router.get("/pending-stock")
async def list_pending_stock(
    request: Request,
    from_site: Optional[str] = Query(None),
    to_site: Optional[str] = Query(None),
    company: Optional[str] = Query(None, description="cfpl or cdpl"),
    from_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    search: Optional[str] = Query(None),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await pending_service.list_pending_transfers(
            conn, from_site=from_site, to_site=to_site, company=company,
            from_date=from_date, to_date=to_date, search=search,
            scope=permissions.scope_warehouses(user),
        )


@router.get("/pending-stock/by-lot")
async def pending_stock_by_lot(
    request: Request,
    lot_no: Optional[str] = Query(None),
    item_description: Optional[str] = Query(None),
    from_site: Optional[str] = Query(None),
    from_company: Optional[str] = Query(None, description="cfpl or cdpl"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await pending_service.pending_by_lot(
            conn, lot_no=lot_no, item_description=item_description,
            from_site=from_site, from_company=from_company)


@router.post("/pending-stock/backfill")
async def backfill_pending_stock(
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_backfill(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await pending_service.backfill_pending_from_existing_transfers(conn)


# ── Inner cold transfer (doc 11 form + dashboard tab) ─────────────────────────
@router.get("/inner-transfer/list", response_model=schemas.InnerColdListResponse)
async def list_inner_cold(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(15, ge=1, le=1000),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await inner_cold_service.list_inner_cold(
            conn, page=page, per_page=per_page, scope=permissions.scope_warehouses(user))


@router.post("/inner-transfer", status_code=201)
async def create_inner_transfer(
    request: Request,
    body: schemas.InnerTransferPayload,
    user: AuthUser = Depends(get_current_user),
):
    # Relabel/relocate within cold storage (doc 11). Authenticated operators (parity with
    # the other transfer mutations); mutates cfpl/cdpl_cold_stocks + writes the audit rows.
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await inner_cold_service.create_inner_transfer(conn, body)


# Declared AFTER /inner-transfer/list so the literal `list` is never captured as a challan.
@router.get("/inner-transfer/{challan_no}")
async def get_inner_transfer(
    request: Request,
    challan_no: str,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await inner_cold_service.get_inner_transfer(conn, challan_no)


@router.delete("/inner-transfer/{challan_no}", response_model=schemas.DeleteResponse)
async def delete_inner_cold(
    request: Request,
    challan_no: str,
    user: AuthUser = Depends(get_current_user),
):
    permissions.assert_can_delete_inner_cold(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await inner_cold_service.delete_inner_cold(conn, challan_no)
