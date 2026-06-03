"""/api/v1/sample/* — Sample Issuing module endpoints.

Covers all four flows: requisition CRUD + lifecycle, business-head approval,
Basis RM / Internal outward + dispatch, FG / NPD job-card generation, NPD draft
BOM authoring + promotion, gate-pass issuance / print / void, and internal ->
external conversion (full + partial).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, Response

from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.sample import schemas
from app.modules.sample.services import (
    approval_service,
    conversion_service,
    gate_pass_service,
    jobcard_service,
    npd_service,
    outward_service,
    requisition_service,
)
from app.modules.sample.services.sample_gate_pass_pdf import generate_sample_gate_pass_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sample", tags=["Sample"])


# ── Requisitions ─────────────────────────────────────────────────────────
@router.post("/requisitions")
async def create_requisition(
    request: Request,
    body: schemas.RequisitionCreate,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.create_requisition(
            conn, payload=body.model_dump(), user=user)


@router.get("/requisitions")
async def list_requisitions(
    request: Request,
    status: str | None = Query(None),
    sample_type: str | None = Query(None),
    entity: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.list_requisitions(
            conn, status=status, sample_type=sample_type, entity=entity,
            limit=limit, offset=offset)


@router.get("/requisitions/{req_id}")
async def get_requisition(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.get_requisition(conn, req_id)


@router.patch("/requisitions/{req_id}")
async def update_requisition(
    request: Request,
    req_id: int,
    body: schemas.RequisitionUpdate,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.update_requisition(
            conn, req_id, payload=body.model_dump(exclude_unset=True), user=user)


@router.post("/requisitions/{req_id}/submit")
async def submit_requisition(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.submit_requisition(conn, req_id, user=user)


@router.post("/requisitions/{req_id}/cancel")
async def cancel_requisition(
    request: Request,
    req_id: int,
    body: schemas.CancelBody,
    user: AuthUser = Depends(require_permission("sample", "requisition", action="edit")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.cancel_requisition(
            conn, req_id, reason=body.reason, user=user)


@router.post("/requisitions/{req_id}/close")
async def close_requisition(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await requisition_service.close_requisition(conn, req_id, user=user)


# ── Approvals ────────────────────────────────────────────────────────────
@router.post("/requisitions/{req_id}/approve")
async def bh_approve(
    request: Request,
    req_id: int,
    body: schemas.ApprovalAction,
    user: AuthUser = Depends(require_permission("sample", "approve", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await approval_service.act_bh_approval(
            conn, req_id, action=body.action, user=user, remarks=body.remarks)


# ── Outward (Basis RM / Internal) ────────────────────────────────────────
@router.post("/requisitions/{req_id}/outward")
async def issue_outward(
    request: Request,
    req_id: int,
    body: schemas.OutwardBody,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    issued = {line.article_id: line.qty for line in (body.issued or [])} or None
    async with pool.acquire() as conn:
        return await outward_service.issue_outward(
            conn, req_id, user=user, from_location=body.from_location, issued=issued)


@router.post("/requisitions/{req_id}/dispatch-internal")
async def dispatch_internal(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await outward_service.dispatch_internal(conn, req_id, user=user)


# ── FG / NPD job cards ────────────────────────────────────────────────────
@router.post("/requisitions/{req_id}/start-production")
async def start_production(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "production_ack", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await jobcard_service.start_production(conn, req_id, user=user)


@router.post("/requisitions/{req_id}/mark-packing")
async def mark_packing(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "production_ack", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await jobcard_service.mark_packing(conn, req_id, user=user)


@router.post("/requisitions/{req_id}/mark-ready")
async def mark_ready(
    request: Request,
    req_id: int,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await jobcard_service.mark_ready(conn, req_id, user=user)


# ── NPD draft BOM ─────────────────────────────────────────────────────────
@router.post("/requisitions/{req_id}/npd-draft")
async def create_npd_draft(
    request: Request,
    req_id: int,
    body: schemas.NpdDraftCreate,
    user: AuthUser = Depends(require_permission("sample", "npd", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_service.create_draft_bom(conn, req_id, payload=body.model_dump(), user=user)


@router.get("/npd-drafts/{draft_id}")
async def get_npd_draft(
    request: Request,
    draft_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_service.get_draft_bom(conn, draft_id)


@router.put("/npd-drafts/{draft_id}/lines")
async def replace_npd_lines(
    request: Request,
    draft_id: int,
    body: schemas.NpdLinesReplace,
    user: AuthUser = Depends(require_permission("sample", "npd", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_service.replace_lines(
            conn, draft_id, lines=[ln.model_dump() for ln in body.lines], user=user)


@router.post("/npd-drafts/{draft_id}/promote")
async def promote_npd_draft(
    request: Request,
    draft_id: int,
    user: AuthUser = Depends(require_permission("sample", "npd", "promote", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await npd_service.promote_draft(conn, draft_id, user=user)


# ── Gate pass ─────────────────────────────────────────────────────────────
@router.post("/requisitions/{req_id}/inv-verify")
async def inv_verify(
    request: Request,
    req_id: int,
    body: schemas.InvVerifyBody,
    user: AuthUser = Depends(require_permission("sample", "inv_signoff", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await gate_pass_service.inv_verify(conn, req_id, user=user, remarks=body.remarks)


@router.post("/requisitions/{req_id}/issue-gate-pass")
async def issue_gate_pass(
    request: Request,
    req_id: int,
    body: schemas.GatePassIssueBody,
    user: AuthUser = Depends(require_permission("sample", "gate_pass", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await gate_pass_service.issue_gate_pass(conn, req_id, user=user, **body.model_dump())


@router.get("/gate-passes/{gp_id}")
async def get_gate_pass(
    request: Request,
    gp_id: int,
    user: AuthUser = Depends(require_permission("sample", action="view")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await gate_pass_service.get_gate_pass(conn, gp_id)


@router.post("/gate-passes/{gp_id}/print")
async def print_gate_pass(
    request: Request,
    gp_id: int,
    user: AuthUser = Depends(require_permission("sample", "gate_pass", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        gp = await gate_pass_service.register_print(conn, gp_id, user=user)
        req_id = (gp.get("sample_details") or {}).get("requisition_id")
        if req_id:
            items = await conn.fetch(
                "SELECT sku_name, required_qty, issued_qty, uom "
                "FROM sample_requisition_articles WHERE requisition_id = $1 ORDER BY id", req_id)
            gp["items"] = [dict(r) for r in items]
        pdf = generate_sample_gate_pass_pdf(gp)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={gp['gate_pass_number']}.pdf"})


@router.post("/gate-passes/{gp_id}/void")
async def void_gate_pass(
    request: Request,
    gp_id: int,
    body: schemas.VoidBody,
    user: AuthUser = Depends(require_permission("sample", "gate_pass", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await gate_pass_service.void_gate_pass(conn, gp_id, reason=body.reason, user=user)


# ── Internal -> external conversion ───────────────────────────────────────
@router.post("/requisitions/{req_id}/convert-full")
async def convert_full(
    request: Request,
    req_id: int,
    body: schemas.ConvertFullBody,
    user: AuthUser = Depends(require_permission("sample", "convert", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await conversion_service.convert_full(conn, req_id, user=user, payload=body.model_dump())


@router.post("/requisitions/{req_id}/convert-partial")
async def convert_partial(
    request: Request,
    req_id: int,
    body: schemas.ConvertPartialBody,
    user: AuthUser = Depends(require_permission("sample", "convert", action="create")),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await conversion_service.convert_partial(
            conn, req_id, qty=body.qty, user=user, payload=body.model_dump())
