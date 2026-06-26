"""POST/GET/PUT/DELETE /api/v1/receipt/* — COA + invoice/receipt-doc endpoints.

Mounts:
    POST   /api/v1/receipt/coa-upload          receipt.coa.create
    GET    /api/v1/receipt/coa                 receipt.coa.read
    GET    /api/v1/receipt/coa/{coa_id}        receipt.coa.read   (entity-rechecked)
    PUT    /api/v1/receipt/coa/{coa_id}        receipt.coa.update (entity-rechecked)
    DELETE /api/v1/receipt/coa/{coa_id}        receipt.coa.delete (entity-rechecked)
    POST   /api/v1/receipt/invoice-upload      receipt.invoice.create

Plus a token-protected local-storage download endpoint:
    GET    /api/v1/receipt/files/{token}       (no permission gate; the
                                                token IS the auth — short
                                                TTL, signed with JWT secret)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response

from app.core.middleware.request_context import AuthError
from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.receipt import storage as storage_mod
from app.modules.receipt.schemas import (
    CoaDetailResponse,
    CoaListResponse,
    CoaReplaceResponse,
    CoaUploadResponse,
    InvoiceUploadResponse,
)
from app.modules.receipt.services import coa_service, invoice_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/receipt", tags=["Receipt"])


# ── pre-flight upload-size guard (WR-08) ─────────────────────────────────
#
# Reject obvious oversize uploads BEFORE reading the request body into
# memory. Honest clients send Content-Length; malicious ones may not — for
# them we still rely on `_validate_file` (which checks `len(content)` after
# `await file.read()`) but at least the well-formed-but-large case doesn't
# burn 100 MB of process memory just to 413 it.

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB — matches services


def _check_request_size(request: Request) -> None:
    raw = request.headers.get("content-length")
    if not raw:
        return  # missing header → fall through to in-service check
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return
    if n > _MAX_UPLOAD_BYTES + (32 * 1024):  # +32KB grace for multipart overhead
        raise AuthError(
            "file_too_large",
            f"Request body exceeds the {_MAX_UPLOAD_BYTES // (1024*1024)} MB limit.",
            413,
        )


# ── shared scope helper (mirrors po_router's _allowed_entities_for) ──────


async def _allowed_entities_for(request: Request, user: AuthUser,
                                sub_module: str) -> list[str] | None:
    """Return entities this user can act on, or None for unrestricted (admin
    or NULL allowed_entities).

    `sub_module` is "coa" or "invoice" so we look at the right perm row.
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
             WHERE rp.role_id = ANY($1)
               AND p.module = 'receipt' AND p.sub_module = $2
            """,
            user.role_ids, sub_module,
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


# ═══════════════════════════════════════════════════════════════════════════
# 1. POST /api/v1/receipt/coa-upload
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/coa-upload", response_model=CoaUploadResponse, status_code=201)
async def coa_upload(
    request: Request,
    file: UploadFile = File(...),
    transaction_no: str | None = Form(None),
    line_number: int | None = Form(None),
    dock_intimation_id: int | None = Form(None),
    qc_intimation_id: int | None = Form(None),
    sku_id: int | None = Form(None),
    sku_name_raw: str | None = Form(None),
    supplier_id: int | None = Form(None),
    lot_number: str | None = Form(None),
    vendor_coa_date: str | None = Form(None),
    parsed_params_json: str | None = Form(None),
    remarks: str | None = Form(None),
    user: AuthUser = Depends(require_permission("receipt", "coa", action="create")),
):
    _check_request_size(request)        # WR-08
    settings = request.app.state.settings
    contents = await file.read()
    return await coa_service.upload_coa(
        request.app.state.db_pool,
        file_name=file.filename,
        file_bytes=contents,
        declared_mime=file.content_type,
        transaction_no=transaction_no,
        line_number=line_number,
        dock_intimation_id=dock_intimation_id,
        qc_intimation_id=qc_intimation_id,
        sku_id=sku_id,
        sku_name_raw=sku_name_raw,
        supplier_id=supplier_id,
        lot_number=lot_number,
        vendor_coa_date=vendor_coa_date,
        parsed_params_json_raw=parsed_params_json,
        remarks=remarks,
        actor_user_id=str(user.user_id),
        settings=settings,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. GET /api/v1/receipt/coa (paginated list)
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/coa", response_model=CoaListResponse)
async def coa_list(
    request: Request,
    transaction_no: str | None = Query(None),
    dock_intimation_id: int | None = Query(None),
    qc_intimation_id: int | None = Query(None),
    po_number: str | None = Query(None),
    sku_id: int | None = Query(None),
    supplier_id: int | None = Query(None),
    lot_number: str | None = Query(None),
    coa_status: str = Query("active"),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: AuthUser = Depends(require_permission("receipt", "coa", action="read")),
):
    settings = request.app.state.settings
    scope = await _allowed_entities_for(request, user, "coa")
    return await coa_service.list_coa(
        request.app.state.db_pool,
        transaction_no=transaction_no,
        dock_intimation_id=dock_intimation_id,
        qc_intimation_id=qc_intimation_id,
        po_number=po_number, sku_id=sku_id, supplier_id=supplier_id,
        lot_number=lot_number, coa_status=coa_status,
        from_date=from_date, to_date=to_date,
        page=page, page_size=page_size,
        entity_scope=scope, settings=settings,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. GET /api/v1/receipt/coa/{coa_id} (detail)
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/coa/{coa_id}", response_model=CoaDetailResponse)
async def coa_detail(
    request: Request,
    coa_id: str,
    include_deleted: bool = Query(False),
    user: AuthUser = Depends(require_permission("receipt", "coa", action="read")),
):
    settings = request.app.state.settings
    scope = await _allowed_entities_for(request, user, "coa")
    return await coa_service.get_coa_detail(
        request.app.state.db_pool, coa_id=coa_id,
        entity_scope=scope, include_deleted=include_deleted, settings=settings,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. PUT /api/v1/receipt/coa/{coa_id} (replace)
# ═══════════════════════════════════════════════════════════════════════════


@router.put("/coa/{coa_id}", response_model=CoaReplaceResponse)
async def coa_replace(
    request: Request,
    coa_id: str,
    file: UploadFile = File(...),
    replaced_reason: str = Form(..., min_length=1),
    vendor_coa_date: str | None = Form(None),
    parsed_params_json: str | None = Form(None),
    user: AuthUser = Depends(require_permission("receipt", "coa", action="update")),
):
    _check_request_size(request)        # WR-08
    settings = request.app.state.settings
    scope = await _allowed_entities_for(request, user, "coa")

    # Re-check entity at the resolved row (don't leak existence across entities)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT entity FROM coa_document WHERE coa_id = $1", coa_id,
        )
    if row is None:
        raise AuthError("coa_not_found", "COA not found", 404)
    if scope is not None and row["entity"] and row["entity"].lower() not in scope:
        raise AuthError("coa_not_found", "COA not found", 404)

    contents = await file.read()
    return await coa_service.replace_coa(
        pool, coa_id=coa_id,
        file_name=file.filename, file_bytes=contents,
        declared_mime=file.content_type,
        replaced_reason=replaced_reason, vendor_coa_date=vendor_coa_date,
        parsed_params_json_raw=parsed_params_json,
        actor_user_id=str(user.user_id), actor_is_admin=user.is_admin,
        settings=settings,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5. DELETE /api/v1/receipt/coa/{coa_id}?reason=...
# ═══════════════════════════════════════════════════════════════════════════


@router.delete("/coa/{coa_id}", status_code=204)
async def coa_delete(
    request: Request,
    coa_id: str,
    reason: str = Query(..., min_length=1),
    user: AuthUser = Depends(require_permission("receipt", "coa", action="delete")),
):
    scope = await _allowed_entities_for(request, user, "coa")
    await coa_service.soft_delete_coa(
        request.app.state.db_pool, coa_id=coa_id, reason=reason,
        actor_user_id=str(user.user_id), entity_scope=scope,
    )
    return None


# ═══════════════════════════════════════════════════════════════════════════
# 6. POST /api/v1/receipt/invoice-upload
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/invoice-upload", response_model=InvoiceUploadResponse, status_code=201)
async def invoice_upload(
    request: Request,
    file: UploadFile = File(...),
    document_type: str | None = Form(None),
    dock_intimation_id: int | None = Form(None),
    transaction_no: str | None = Form(None),
    remarks: str | None = Form(None),
    user: AuthUser = Depends(require_permission("receipt", "invoice", action="create")),
):
    _check_request_size(request)        # WR-08
    settings = request.app.state.settings
    contents = await file.read()
    return await invoice_service.upload_invoice(
        request.app.state.db_pool,
        file_name=file.filename, file_bytes=contents,
        declared_mime=file.content_type, document_type=document_type,
        transaction_no=transaction_no, dock_intimation_id=dock_intimation_id,
        remarks=remarks, actor_user_id=str(user.user_id), settings=settings,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Local-storage download endpoint
# ═══════════════════════════════════════════════════════════════════════════
#
# The presigned-URL contract (5-min TTL) is honoured for both backends:
#   • S3   → real boto3 presigned GET, returned directly to the client.
#   • Local → a JWT-signed token URL pointing here. The token IS the auth.
#
# This endpoint deliberately does NOT use require_permission — the URL is
# what the legitimate client just received from /coa or /coa/{id}. Anyone
# with the URL can fetch (matching real S3 presigned-URL semantics).


@router.get("/files/{token}")
async def download_local_file(request: Request, token: str):
    settings = request.app.state.settings
    s3_key = storage_mod.verify_local_token(token, settings)
    if not s3_key:
        raise AuthError("invalid_token", "Download URL is invalid or expired.", 403)

    storage = storage_mod.get_storage(settings)
    try:
        content = storage.open_for_read(s3_key)
    except FileNotFoundError:
        raise AuthError("not_found", "File not found.", 404)
    except (PermissionError, ValueError):
        raise AuthError("invalid_token", "Bad path.", 403)

    # Best-effort mime guess from the extension; falls back to octet-stream.
    mime = "application/octet-stream"
    if s3_key.endswith(".pdf"):
        mime = "application/pdf"
    elif s3_key.endswith(".jpg") or s3_key.endswith(".jpeg"):
        mime = "image/jpeg"
    elif s3_key.endswith(".png"):
        mime = "image/png"

    return Response(content=content, media_type=mime)
