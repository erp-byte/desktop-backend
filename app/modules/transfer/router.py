"""/api/v1/transfer/* — Inter-Unit Transfer module endpoints.

Phase 1: the Transfer Dashboard surface — list/detail reads for requests,
transfers (OUT) and transfer-ins (IN); the in-transit pending-stock list +
backfill; the four delete actions; and the inner-cold tab list + delete.

Reads require only a valid token (production had no page-level gate on viewing
the dashboard), and are scoped to a warehouse-locked user's sites
(permissions.scope_warehouses): the lists leave other rows out, and a detail read
of one is 404. Mutations go through `require_transfer_mutate`.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.sites import canonical_site, same_site
from app.modules.auth.middleware import AuthUser, get_current_user, require_permission
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
    request_decision_service,
    request_service,
    scan_service,
)

router = APIRouter(prefix="/api/v1/transfer", tags=["Transfer"])

# Admins and inventory managers: deleting requests, transfers, receipts and inner cold
# challans, the in-transit sync, receiving a transfer and re-opening / editing a receipt.
# An inventory manager is a holder of transfer.requests.approve (migration 121 grants it
# to inventory_manager and inventory_manager_substitute; the admin screen can grant it
# to any role); admins pass on their flag. No email lists.
_ADMIN_OR_MANAGER = require_permission("transfer", "requests", action="approve")


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
    # A request is raised by the requesting site: a warehouse-locked user only for their
    # own warehouse as the To site (admins and users with no lock: any).
    permissions.assert_can_request_for(user, body.form_data.to_warehouse)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await request_service.create_request(conn, body, user.email or "unknown")


@router.put("/requests/{request_id}", response_model=schemas.RequestWithLines)
async def update_request(
    request: Request,
    request_id: int,
    body: schemas.RequestCreate,
    user: AuthUser = Depends(get_current_user),
):
    # Edit while Pending only (before the store accepts it), by its raiser or an admin,
    # with the same To-site lock as create; the service checks all of it with the row
    # locked. Its request number, raiser and status stay.
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await request_service.update_request(conn, request_id, body, user)


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
        return await query_service.get_request(conn, request_id, scope=permissions.scope_warehouses(user))


@router.delete("/requests/{request_id}", response_model=schemas.DeleteResponse)
async def delete_request(
    request: Request,
    request_id: int,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    # Only a Pending or Rejected request, with its row locked (409 otherwise).
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await delete_service.delete_request(conn, request_id)


# ── Request decisions ─────────────────────────────────────────────────────────
# Pending → Accepted (the supplying store) → Approved (an inventory manager); either
# side may reject instead, with a reason. Gated on the transfer.requests.accept /
# .approve role permissions (app/db/121_transfer_request_decisions.sql). The rules
# that depend on the request row — the store's warehouse lock, nobody deciding their
# own request — run in the service, inside the transaction that locks the row.
# accept / approve take no body; whatever the client sends ({}) is ignored.
@router.post("/requests/{request_id}/accept", response_model=schemas.RequestWithLines)
async def accept_request(
    request: Request,
    request_id: int,
    user: AuthUser = Depends(require_permission("transfer", "requests", action="accept")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await request_decision_service.accept_request(conn, request_id, user)


@router.post("/requests/{request_id}/store-reject", response_model=schemas.RequestWithLines)
async def store_reject_request(
    request: Request,
    request_id: int,
    body: schemas.RequestReject,
    user: AuthUser = Depends(require_permission("transfer", "requests", action="accept")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await request_decision_service.store_reject_request(conn, request_id, user, body.reason)


# Holds, with a reason (migration 127). The store's hold keeps the request Pending (it
# can still accept or reject it); an inventory manager's makes an Accepted one 'On Hold'
# (they can still approve or reject it). Gated and checked as accept / approve.
@router.post("/requests/{request_id}/store-hold", response_model=schemas.RequestWithLines)
async def store_hold_request(
    request: Request,
    request_id: int,
    body: schemas.RequestHold,
    user: AuthUser = Depends(require_permission("transfer", "requests", action="accept")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await request_decision_service.store_hold_request(conn, request_id, user, body.reason)


@router.post("/requests/{request_id}/hold", response_model=schemas.RequestWithLines)
async def hold_request(
    request: Request,
    request_id: int,
    body: schemas.RequestHold,
    user: AuthUser = Depends(require_permission("transfer", "requests", action="approve")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await request_decision_service.hold_request(conn, request_id, user, body.reason)


@router.post("/requests/{request_id}/approve", response_model=schemas.RequestWithLines)
async def approve_request(
    request: Request,
    request_id: int,
    user: AuthUser = Depends(require_permission("transfer", "requests", action="approve")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await request_decision_service.approve_request(conn, request_id, user)


@router.post("/requests/{request_id}/reject", response_model=schemas.RequestWithLines)
async def reject_request(
    request: Request,
    request_id: int,
    body: schemas.RequestReject,
    user: AuthUser = Depends(require_permission("transfer", "requests", action="approve")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await request_decision_service.reject_request(conn, request_id, user, body.reason)


# ── Scan & send: each scanned box saved at once ───────────────────────────────
# The first scan of an Approved request creates its transfer (as POST /transfers does,
# with that box), each later one adds its box to it; the transfer routes below remove
# boxes, edit a box's weights and the transport details in place. Authenticated
# operators, as for create; the profile lock is on the request's stored From site and
# runs in the service, which reads the row (with it locked).
@router.post("/requests/{request_id}/scan", response_model=schemas.ScanResult)
async def scan_box(
    request: Request,
    request_id: int,
    body: schemas.ScanBox,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await scan_service.scan_box(conn, request_id, body, user)


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
    # extra gate; the page itself has no role gate). Atomic in the service; record only
    # (no stock is deducted). A box in transit, received at another site or written off
    # is refused (409); so, from a request, is a dispatch without a box or with a box
    # the request does not ask for.
    # Profile lock: a warehouse-bound operator may only dispatch FROM their warehouse.
    permissions.assert_can_dispatch_from(user, body.header.from_warehouse)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.create_transfer(conn, body, user.email or "unknown")


# ── Box lookups (transfer-OUT form: manual entry + TR-/BE- QR) ───────────────
# `from_warehouse` is the From site of the dispatch the box is scanned for: a box
# received (and acknowledged) there may be sent on; without it any transfer of the box
# blocks it. A box in transit, at another site or written off is 409 either way.
_FROM_WAREHOUSE = "From site of the dispatch the box is scanned for"


@router.get("/box-lookup/{company}")
async def box_lookup(
    request: Request,
    company: str,
    box_number: int = Query(..., description="Box number"),
    transaction_no: str = Query(..., description="Transaction number"),
    from_warehouse: Optional[str] = Query(None, description=_FROM_WAREHOUSE),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await lookup_service.get_box_by_number(
            conn, company, box_number, transaction_no, from_warehouse=from_warehouse)


@router.get("/box-lookup-by-id/{company}")
async def box_lookup_by_id(
    request: Request,
    company: str,
    box_id: str = Query(..., description="Box ID from a TR- QR"),
    transaction_no: str = Query(..., description="Transaction number"),
    from_warehouse: Optional[str] = Query(None, description=_FROM_WAREHOUSE),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await lookup_service.get_box_by_box_id(
            conn, company, box_id, transaction_no, from_warehouse=from_warehouse)


@router.get("/bulk-entry-box-lookup/{company}")
async def bulk_entry_box_lookup(
    request: Request,
    company: str,
    box_id: str = Query(..., description="Box ID from a BE- QR"),
    transaction_no: str = Query(..., description="Transaction number starting with BE-"),
    from_warehouse: Optional[str] = Query(None, description=_FROM_WAREHOUSE),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await lookup_service.get_bulk_entry_box(
            conn, company, box_id, transaction_no, from_warehouse=from_warehouse)


@router.put("/transfers/{transfer_id}", response_model=schemas.TransferWithLines)
async def update_transfer(
    request: Request,
    transfer_id: int,
    body: schemas.TransferCreate,
    user: AuthUser = Depends(get_current_user),
):
    # Edit (doc 08, ?editId). Authenticated operators edit dispatches (parity with create);
    # the dashboard gates editing of Received/Completed transfers. A box that cannot be
    # sent from the From site is refused (409), judged without this transfer; an edit of a
    # request transfer keeps at least one box and only boxes the request asks for (409).
    # Profile lock: the edited from_warehouse must stay within the operator's scope, and so
    # must the transfer's STORED From site — else a locked user could take over another
    # site's transfer by sending their own site as its From. The service checks the stored
    # From again with the transfer's row locked, and keeps the route of a transfer without
    # a request unless an admin edits it (409).
    permissions.assert_can_dispatch_from(user, body.header.from_warehouse)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if permissions.has_warehouse_lock(user):
            stored = await query_service.transfer_sites(conn, transfer_id)
            if stored is not None:
                permissions.assert_can_dispatch_from(user, stored["from_site"])
        return await create_service.update_transfer(conn, transfer_id, body, user=user)


@router.get("/transfers/{transfer_id}", response_model=schemas.TransferWithLines)
async def get_transfer(
    request: Request,
    transfer_id: int,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.get_transfer(conn, transfer_id, scope=permissions.scope_warehouses(user))


@router.delete("/transfers/{transfer_id}", response_model=schemas.TransferDeleteResponse)
async def delete_transfer(
    request: Request,
    transfer_id: int,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await delete_service.delete_transfer(conn, transfer_id)


# Scan & send's changes to a request's transfer while it is in transit (scan_service):
# the profile lock is on the transfer's stored From site, in the service.
@router.post("/transfers/{transfer_id}/remove-boxes", response_model=schemas.RemoveBoxesResult,
             response_model_exclude_none=True)
async def remove_transfer_boxes(
    request: Request,
    transfer_id: int,
    body: schemas.RemoveBoxes,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await scan_service.remove_boxes(conn, transfer_id, body, user)


@router.patch("/transfers/{transfer_id}/boxes", response_model=schemas.ScannedBox)
async def update_transfer_box_weights(
    request: Request,
    transfer_id: int,
    body: schemas.BoxWeights,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await scan_service.update_box_weights(conn, transfer_id, body, user)


@router.patch("/transfers/{transfer_id}/transport", response_model=schemas.TransportResult)
async def update_transfer_transport(
    request: Request,
    transfer_id: int,
    body: schemas.TransportUpdate,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await scan_service.update_transport(conn, transfer_id, body, user)


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
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    # Profile lock (bulk receipt): on the transfer's stored To site, in the service, as
    # on the pending route below — never the client's receiving_warehouse.
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.create_transfer_in(conn, body, user)


@router.post("/transfer-in/pending", status_code=201)
async def create_pending_transfer_in(
    request: Request,
    body: schemas.PendingTransferInCreate,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    # Profile lock: a warehouse-bound operator may only receive INTO their warehouse —
    # the transfer's stored To site, checked in the service with the transfer's row
    # locked; the client's receiving_warehouse is not trusted (the receipt stores the To
    # site's canonical name).
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.create_pending_transfer_in(conn, body, user)


@router.get("/transfer-in/pending/by-transfer-out/{transfer_out_id}")
async def get_pending_by_transfer_out(
    request: Request,
    transfer_out_id: int,
    user: AuthUser = Depends(get_current_user),
):
    # Scoped as GET /transfers/{id}: the receipt of a transfer the user cannot read is 404.
    scope = permissions.scope_warehouses(user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if scope:
            sites = await query_service.transfer_sites(conn, transfer_out_id)
            if sites is not None and not permissions.in_scope(scope, *sites.values()):
                raise HTTPException(404, "Transfer not found")
        return await receive_service.get_pending_by_transfer_out(conn, transfer_out_id)


# The receipt's own steps (acknowledge, un-acknowledge, finalize, close with shortage,
# re-open) carry the receive lock too: the user must be able to receive into the stored
# To site of the receipt's transfer, checked in the service (receive_service).
@router.post("/transfer-in/{header_id}/acknowledge")
async def acknowledge_box(
    request: Request,
    header_id: int,
    body: schemas.PendingBoxAcknowledge,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.acknowledge_pending_box(conn, header_id, body, user=user)


@router.post("/transfer-in/{header_id}/acknowledge-batch")
async def acknowledge_batch(
    request: Request,
    header_id: int,
    body: List[schemas.PendingBoxAcknowledge],
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.acknowledge_pending_boxes_batch(conn, header_id, body, user=user)


@router.delete("/transfer-in/{header_id}/acknowledge/{box_id:path}")
async def unacknowledge_box(
    request: Request,
    header_id: int,
    box_id: str,
    transaction_no: Optional[str] = Query(
        None, description="the box's transaction: of two boxes sharing a box_id only that one goes"),
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.unacknowledge_pending_box(
            conn, header_id, box_id, transaction_no=transaction_no, user=user)


@router.post("/transfer-in/{header_id}/finalize")
async def finalize_transfer_in(
    request: Request,
    header_id: int,
    body: schemas.FinalizeTransferIn,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.finalize_transfer_in(conn, header_id, body, user=user)


@router.post("/transfer-in/{header_id}/reopen")
async def reopen_transfer_in(
    request: Request,
    header_id: int,
    body: schemas.ReopenTransferIn | None = None,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    # Reverses a Received GRN back to Pending (its boxes back in transit; record only).
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.reopen_transfer_in(conn, header_id, user.email or "system", user=user)


@router.post("/transfer-in/{header_id}/close-with-shortage")
async def close_transfer_in_with_shortage(
    request: Request,
    header_id: int,
    body: schemas.CloseWithShortage,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    # Finalize the acknowledged boxes and write off the un-received shortfall (record only).
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await receive_service.close_with_shortage(
            conn, header_id, body.shortage_reason, user.email or "system", user=user)


@router.put("/transfer-in/{transfer_in_id}")
async def edit_transfer_in(
    request: Request,
    transfer_in_id: int,
    body: schemas.EditTransferIn,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    # Edit a Received GRN: header-only edits update in place; per-box edits re-open,
    # correct and receive again (record only — no stock table is written).
    # Profile lock: the receipt is of a transfer to the operator's warehouse (its stored
    # To site). A receiving warehouse sent with the edit must be that site (409
    # otherwise) and is stored by its canonical name.
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        to_site = await query_service.receipt_to_site(conn, transfer_in_id)
        if to_site is not None:
            permissions.assert_can_receive_into(user, to_site)
            if body.receiving_warehouse is not None:
                if not same_site(body.receiving_warehouse, to_site):
                    raise HTTPException(
                        409, f"This receipt is for a transfer to {canonical_site(to_site)}; its receiving "
                             f"warehouse can't be {canonical_site(body.receiving_warehouse) or 'blank'}.")
                body.receiving_warehouse = canonical_site(to_site)
        return await receive_service.edit_transfer_in(conn, transfer_in_id, body, user.email or "system", user=user)


@router.get("/transfer-in/{transfer_in_id}", response_model=schemas.TransferInDetail)
async def get_transfer_in(
    request: Request,
    transfer_in_id: int,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.get_transfer_in(conn, transfer_in_id, scope=permissions.scope_warehouses(user))


@router.delete("/transfer-in/{transfer_in_id}", response_model=schemas.DeleteResponse)
async def delete_transfer_in(
    request: Request,
    transfer_in_id: int,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
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
            from_site=from_site, from_company=from_company,
            scope=permissions.scope_warehouses(user))


@router.post("/pending-stock/backfill")
async def backfill_pending_stock(
    request: Request,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
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
    # Relabel lots / correct locations inside ONE cold store (doc 11). Authenticated
    # operators (parity with the other transfer mutations), locked to their warehouse: the
    # store must be theirs (403), and a line at or to another store is 409. Edits
    # cfpl/cdpl_cold_stocks in place + writes the audit and change rows (the undo's).
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await inner_cold_service.create_inner_transfer(conn, body, user)


# Declared AFTER /inner-transfer/list so the literal `list` is never captured as a challan.
@router.get("/inner-transfer/{challan_no}")
async def get_inner_transfer(
    request: Request,
    challan_no: str,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await inner_cold_service.get_inner_transfer(
            conn, challan_no, scope=permissions.scope_warehouses(user))


@router.delete("/inner-transfer/{challan_no}", response_model=schemas.DeleteResponse)
async def delete_inner_cold(
    request: Request,
    challan_no: str,
    user: AuthUser = Depends(_ADMIN_OR_MANAGER),
):
    # Deleting a challan undoes it: its cold-stock changes are put back (409 when a row
    # has been changed since, or for a challan saved before its changes were recorded).
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await inner_cold_service.delete_inner_cold(conn, challan_no, user)
