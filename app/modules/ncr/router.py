"""POST/GET/PUT/DELETE /api/v1/ncr/* — 13 spec endpoints + dual-approve.

Pass-2 fixes wired in:
    • CR-01 — POST /{ncr_no}/dual-approve added (so critical NCRs are closable)
    • HI-02 — entity-scope helper threaded through every handler
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request

from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.ncr.schemas import (
    CapaResponse,
    CapaSubmitRequest,
    CapaUpdateRequest,
    DispositionRequest,
    DispositionResponse,
    DispositionUpdateRequest,
    DualApproveRequest,
    DualApproveResponse,
    NcrCancelRequest,
    NcrDetailResponse,
    NcrListResponse,
    NcrRaiseRequest,
    NcrRaiseResponse,
    NcrReopenRequest,
    NcrUpdateRequest,
    VerifyRequest,
    VerifyResponse,
)
from app.modules.ncr.services import ncr_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ncr", tags=["NCR"])


# ── HI-02: entity-scope helper (mirrors po_router._allowed_entities_for) ─


async def _allowed_entities_for(request: Request, user: AuthUser,
                                sub_module: str) -> list[str] | None:
    """Return the entities this user can act on, or None for unrestricted.

    Sources, in order:
        1. admin → None (unrestricted).
        2. union of allowed_entities across role-permission rows for
           module=ncr, sub_module=<sub_module>.
        3. fallback to user.entity (single-entity user).
        NULL allowed_entities on any matching row → None (unrestricted).
    """
    if user.is_admin:
        return None
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rp.allowed_entities
              FROM auth_role_permission rp
              JOIN auth_permission p ON rp.permission_id = p.permission_id
             WHERE rp.role_id = $1
               AND p.module = 'ncr' AND p.sub_module = $2
            """,
            user.role_id, sub_module,
        )
    if any(r["allowed_entities"] is None for r in rows):
        return None
    found: set[str] = set()
    for r in rows:
        for e in (r["allowed_entities"] or []):
            if e:
                found.add(e.lower())
    if not found and user.entity:
        found.add(user.entity.lower())
    return sorted(found)


# ── 2.1 raise ────────────────────────────────────────────────────────────


@router.post("/raise", response_model=NcrRaiseResponse, status_code=201)
async def raise_ncr(
    request: Request,
    body: NcrRaiseRequest,
    user: AuthUser = Depends(require_permission("ncr", "record", action="create")),
):
    return await ncr_service.raise_ncr(
        request.app.state.db_pool,
        inspection_id=body.inspection_id,
        transaction_no=body.transaction_no,
        line_number=body.line_number,
        sku_id=body.sku_id,
        supplier_id=body.supplier_id,
        lot_number=body.lot_number,
        rejected_qty=body.rejected_qty,
        severity=body.severity,
        summary=body.summary,
        documented_date=body.documented_date,
        parameter_details=[p.model_dump() for p in body.parameter_details],
        raised_via="manual",
        actor_user_id=str(user.user_id),
    )


# ── 2.2 list ─────────────────────────────────────────────────────────────


@router.get("", response_model=NcrListResponse)
@router.get("/", response_model=NcrListResponse)
async def list_ncrs(
    request: Request,
    status: str | None = Query(None),
    severity: str | None = Query(None),
    supplier_id: int | None = Query(None),
    sku_id: int | None = Query(None),
    disposition: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: AuthUser = Depends(require_permission("ncr", "record", action="read")),
):
    scope = await _allowed_entities_for(request, user, "record")
    return await ncr_service.list_ncrs(
        request.app.state.db_pool,
        status=status, severity=severity, supplier_id=supplier_id, sku_id=sku_id,
        disposition=disposition, from_date=from_date, to_date=to_date,
        page=page, page_size=page_size, entity_scope=scope,
    )


# ── 2.3 detail ───────────────────────────────────────────────────────────


@router.get("/{ncr_no}", response_model=NcrDetailResponse)
async def get_detail(
    request: Request, ncr_no: str,
    user: AuthUser = Depends(require_permission("ncr", "record", action="read")),
):
    scope = await _allowed_entities_for(request, user, "record")
    return await ncr_service.get_detail(
        request.app.state.db_pool, ncr_no=ncr_no, entity_scope=scope,
    )


# ── 2.4 update header ────────────────────────────────────────────────────


@router.put("/{ncr_no}")
async def update_header(
    request: Request, ncr_no: str, body: NcrUpdateRequest,
    user: AuthUser = Depends(require_permission("ncr", "record", action="update")),
):
    scope = await _allowed_entities_for(request, user, "record")
    await ncr_service.update_header(
        request.app.state.db_pool, ncr_no=ncr_no,
        severity=body.severity, summary=body.summary,
        documented_date=body.documented_date,
        actor_user_id=str(user.user_id), entity_scope=scope,
    )
    return {"updated": True}


# ── 2.5 disposition (POST) ───────────────────────────────────────────────


@router.post("/{ncr_no}/disposition", response_model=DispositionResponse)
async def submit_disposition(
    request: Request, ncr_no: str, body: DispositionRequest,
    user: AuthUser = Depends(require_permission("ncr", "record", action="approve")),
):
    scope = await _allowed_entities_for(request, user, "record")
    return await ncr_service.submit_disposition(
        request.app.state.db_pool, ncr_no=ncr_no,
        body=body.model_dump(), actor_user_id=str(user.user_id),
        entity_scope=scope,
    )


# ── 2.6 disposition edit (PUT) ───────────────────────────────────────────


@router.put("/{ncr_no}/disposition")
async def edit_disposition(
    request: Request, ncr_no: str, body: DispositionUpdateRequest,
    user: AuthUser = Depends(require_permission("ncr", "record", action="update")),
):
    scope = await _allowed_entities_for(request, user, "record")
    await ncr_service.edit_disposition(
        request.app.state.db_pool, ncr_no=ncr_no,
        body=body.model_dump(exclude_none=False),
        actor_user_id=str(user.user_id), entity_scope=scope,
    )
    return {"updated": True}


# ── 2.5b dual-approve (CR-01) ────────────────────────────────────────────


@router.post("/{ncr_no}/dual-approve", response_model=DualApproveResponse)
async def dual_approve(
    request: Request, ncr_no: str, body: DualApproveRequest,
    user: AuthUser = Depends(require_permission("ncr", "record", action="approve")),
):
    """CR-01: record the second-pair-of-eyes approver on a critical NCR.

    Without this, critical NCRs (`requires_dual_approval=true`) cannot be
    closed via /verify because the dual_approval_by column starts NULL and
    only reopen used to write it.
    """
    scope = await _allowed_entities_for(request, user, "record")
    return await ncr_service.set_dual_approval(
        request.app.state.db_pool, ncr_no=ncr_no,
        approver_user_id=body.approver_user_id, reason=body.reason,
        actor_user_id=str(user.user_id), entity_scope=scope,
    )


# ── 2.7 CAPA submit ──────────────────────────────────────────────────────


@router.post("/{ncr_no}/capa", response_model=CapaResponse, status_code=201)
async def submit_capa(
    request: Request, ncr_no: str, body: CapaSubmitRequest,
    user: AuthUser = Depends(require_permission("ncr", "capa", action="create")),
):
    scope = await _allowed_entities_for(request, user, "capa")
    return await ncr_service.submit_capa(
        request.app.state.db_pool, ncr_no=ncr_no,
        body=body.model_dump(), actor_user_id=str(user.user_id),
        entity_scope=scope,
    )


# ── 2.8 CAPA update ──────────────────────────────────────────────────────


@router.put("/{ncr_no}/capa/{action_id}")
async def update_capa(
    request: Request, ncr_no: str, action_id: int, body: CapaUpdateRequest,
    user: AuthUser = Depends(require_permission("ncr", "capa", action="update")),
):
    scope = await _allowed_entities_for(request, user, "capa")
    await ncr_service.update_capa(
        request.app.state.db_pool, ncr_no=ncr_no, action_id=action_id,
        body=body.model_dump(exclude_none=False),
        actor_user_id=str(user.user_id), entity_scope=scope,
    )
    return {"updated": True}


# ── 2.9 CAPA delete ──────────────────────────────────────────────────────


@router.delete("/{ncr_no}/capa/{action_id}", status_code=204)
async def delete_capa(
    request: Request, ncr_no: str, action_id: int,
    user: AuthUser = Depends(require_permission("ncr", "capa", action="delete")),
):
    scope = await _allowed_entities_for(request, user, "capa")
    await ncr_service.delete_capa(
        request.app.state.db_pool, ncr_no=ncr_no, action_id=action_id,
        actor_user_id=str(user.user_id), entity_scope=scope,
    )
    return None


# ── 2.10 verify ──────────────────────────────────────────────────────────


@router.post("/{ncr_no}/verify", response_model=VerifyResponse)
async def verify_capa(
    request: Request, ncr_no: str, body: VerifyRequest,
    user: AuthUser = Depends(require_permission("ncr", "capa", action="verify")),
):
    """WR-06: dropped unused `actor_is_admin` from the service signature."""
    scope = await _allowed_entities_for(request, user, "capa")
    return await ncr_service.verify_capa(
        request.app.state.db_pool, ncr_no=ncr_no, body=body.model_dump(),
        actor_user_id=str(user.user_id), entity_scope=scope,
    )


# ── 2.11 cancel ──────────────────────────────────────────────────────────


@router.post("/{ncr_no}/cancel")
async def cancel_ncr(
    request: Request, ncr_no: str, body: NcrCancelRequest,
    user: AuthUser = Depends(require_permission("ncr", "record", action="approve")),
):
    scope = await _allowed_entities_for(request, user, "record")
    await ncr_service.cancel_ncr(
        request.app.state.db_pool, ncr_no=ncr_no,
        reason=body.reason, actor_user_id=str(user.user_id),
        entity_scope=scope,
    )
    return {"cancelled": True}


# ── 2.12 reopen ──────────────────────────────────────────────────────────


@router.post("/{ncr_no}/reopen")
async def reopen_ncr(
    request: Request, ncr_no: str, body: NcrReopenRequest,
    user: AuthUser = Depends(require_permission("ncr", "record", action="approve")),
):
    scope = await _allowed_entities_for(request, user, "record")
    await ncr_service.reopen_ncr(
        request.app.state.db_pool, ncr_no=ncr_no,
        reason=body.reason,
        dual_approver_user_id=body.dual_approver_user_id,
        actor_user_id=str(user.user_id), entity_scope=scope,
    )
    return {"reopened": True}


# ── 2.13 audit ───────────────────────────────────────────────────────────


@router.get("/{ncr_no}/audit")
async def audit(
    request: Request, ncr_no: str,
    user: AuthUser = Depends(require_permission("ncr", "record", action="read")),
):
    scope = await _allowed_entities_for(request, user, "record")
    return await ncr_service.audit(
        request.app.state.db_pool, ncr_no=ncr_no, entity_scope=scope,
    )
