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

from fastapi import APIRouter, Depends, Query, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.customer_returns import schemas
from app.modules.customer_returns.approval_token import verify_action_token
from app.modules.customer_returns.services import (
    create_service, query_service, box_service, export_xlsx,
    mail_service, approval_service,
)


class CRDecisionRequest(BaseModel):
    action: str  # approve | reject | hold
    remark: str | None = None


def _action_page(title: str, message: str, color: str) -> str:
    """Self-contained HTML confirmation card for the emailed magic-link click,
    with a best-effort self-close (the BH usually opens it from a mail app)."""
    from html import escape
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title></head>"
        f"<body style='font-family:Arial,sans-serif;background:#f4f4f5;margin:0;"
        f"display:flex;min-height:100vh;align-items:center;justify-content:center;'>"
        f"<div style='background:#fff;border-radius:12px;padding:32px 40px;max-width:420px;"
        f"text-align:center;box-shadow:0 6px 24px rgba(0,0,0,.08);'>"
        f"<div style='width:56px;height:56px;border-radius:50%;background:{color};margin:0 auto 16px;'></div>"
        f"<h2 style='margin:0 0 8px;color:{color};'>{escape(title)}</h2>"
        f"<p style='margin:0;color:#444;font-size:15px;'>{escape(message)}</p></div>"
        f"<script>setTimeout(function(){{try{{window.close();}}catch(e){{}}}},1200);</script>"
        f"</body></html>"
    )

router = APIRouter(prefix="/api/v1/customer-returns", tags=["Customer Returns"])
_IST = ZoneInfo("Asia/Kolkata")


def _actor(user: AuthUser) -> str:
    return user.email or user.full_name or str(user.user_id)


# ── Read-only gate ────────────────────────────────────────────────────────────
# Phase 2 (write path on the shared live _rtv_* tables) is built + verified, so
# writes are ENABLED. The guard mechanism is retained as a one-line kill switch:
# set CR_READ_ONLY = True to instantly fall back to the read-only phase (all 9
# mutating endpoints 403, email-action shows an "approvals paused" card).
CR_READ_ONLY = False


def _ensure_writable() -> None:
    if CR_READ_ONLY:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "read_only",
                "message": "Customer Returns is read-only in CFERP for now — "
                           "create or edit returns in IMS.",
            },
        )


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


@router.post("/box-edit-log", response_model=schemas.CRBoxEditLogResponse,
             dependencies=[Depends(_ensure_writable)])
async def log_customer_return_box_edits(
    body: schemas.CRBoxEditLogRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.log_box_edits(conn, body, email_id=_actor(user))


@router.get("/email-routing")
async def get_email_routing(
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    """Customer-Returns approval e-mail routing (DB-backed; replaces the old
    hardcoded maps). Company-agnostic. Consumed by the FE _approvalMatrix.ts and
    the create form's Business-Head / Sales-POC dropdowns. Declared BEFORE the
    ``/{company}`` routes so "email-routing" isn't captured as a company segment.

    business_head / sales_poc are the PEOPLE holding the ``business_head`` /
    ``sales`` roles (auth_user) — the source of truth for who is a BH / sales POC,
    assigned on the Admin screen. warehouse_cc / constant_cc / notify_to have no
    user/role source and come from the cr_email_routing config table.
    """
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await mail_service.fetch_routing(conn)


@router.get("/email-action", response_class=HTMLResponse)
async def customer_return_email_action(token: str, request: Request):
    """PUBLIC magic-link target for the emailed Approve/Reject/Hold buttons — a
    browser click carries no login, so the signed token IS the authorization.
    Applies the decision idempotently, fires the threaded status mail, and renders
    a self-closing confirmation card. Declared BEFORE /{company} routes.
    """
    if CR_READ_ONLY:
        return HTMLResponse(
            _action_page("Approvals paused", "Customer-return approvals are handled in IMS for now. No action was taken.", "#e67e22"),
            status_code=403,
        )
    claims = verify_action_token(token)
    if not claims:
        return HTMLResponse(
            _action_page("Link invalid or expired", "This approval link is no longer valid. Open the CR in the app to decide.", "#c0392b"),
            status_code=400,
        )
    company, cr_id, action = claims["company"], claims["cr_id"], claims["action"]
    actor = claims.get("approver") or "email-link"
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            result = await approval_service.apply_cr_action(conn, company, cr_id, action, actor)
            if not result["already_actioned"]:
                detail = await query_service.get_cr(conn, company, cr_id)
                detail["company"] = company
                await mail_service.notify_cr_status_changed(conn, detail, result["status"], actor)
    except HTTPException as exc:
        return HTMLResponse(_action_page("Action failed", str(exc.detail), "#c0392b"), status_code=exc.status_code)
    if result["already_actioned"]:
        return HTMLResponse(_action_page("Already actioned", f'This customer return is already “{result["status"]}”.', "#e67e22"))
    return HTMLResponse(_action_page(f'Customer return {result["status"]}', f'{cr_id} is now “{result["status"]}”. You can close this tab.', "#27ae60"))


@router.post("/{company}", status_code=201, response_model=schemas.CRWithDetails,
             dependencies=[Depends(_ensure_writable)])
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


@router.put("/{company}/{cr_id}", response_model=schemas.CRHeaderResponse,
            dependencies=[Depends(_ensure_writable)])
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


@router.put("/{company}/{cr_id}/lines", response_model=schemas.CRLinesUpdateResponse,
            dependencies=[Depends(_ensure_writable)])
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


@router.delete("/{company}/{cr_id}", response_model=schemas.CRDeleteResponse,
               dependencies=[Depends(_ensure_writable)])
async def delete_customer_return(
    company: str,
    cr_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.delete_cr(conn, company, cr_id)


@router.put("/{company}/{cr_id}/box", response_model=schemas.CRBoxUpsertResponse,
            dependencies=[Depends(_ensure_writable)])
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


@router.put("/{company}/{cr_id}/boxes", response_model=schemas.CRBulkBoxUpdateResponse,
            dependencies=[Depends(_ensure_writable)])
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


@router.post("/{company}/{cr_id}/send-for-approval",
             dependencies=[Depends(_ensure_writable)])
async def send_customer_return_for_approval(
    company: str,
    cr_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    """Fire the threaded approval mail: the mapped Business Head gets the
    Approve/Reject/Hold magic-link buttons; everyone else gets a threaded copy.
    Best-effort send (no-op if SMTP is unconfigured). Returns the resolved
    recipients so the UI can confirm who was notified."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        detail = await query_service.get_cr(conn, company, cr_id)
        detail["company"] = company
        rec = await mail_service.notify_cr_created(conn, detail)
    return {
        "status": "sent",
        "approver": rec["approver"],
        "to": rec["to"],
        "cc_count": len(rec["cc"]),
    }


@router.post("/{company}/{cr_id}/approve",
             dependencies=[Depends(_ensure_writable)])
async def decide_customer_return(
    company: str,
    cr_id: str,
    body: CRDecisionRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    """In-app Approve/Reject/Hold. Idempotent status transition + audit, then the
    threaded status-change mail. (The emailed buttons hit the public /email-action
    route instead.)"""
    pool = request.app.state.db_pool
    actor = _actor(user)
    async with pool.acquire() as conn:
        result = await approval_service.apply_cr_action(conn, company, cr_id, body.action, actor, body.remark)
        if not result["already_actioned"]:
            detail = await query_service.get_cr(conn, company, cr_id)
            detail["company"] = company
            await mail_service.notify_cr_status_changed(conn, detail, result["status"], actor)
    return result
