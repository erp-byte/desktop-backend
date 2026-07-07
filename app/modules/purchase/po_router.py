"""POST /api/v1/po/* — the documented spec router.

Mounted in parallel to the legacy /api/v1/purchase/* router. This module:
    • Uses JWT-aware auth via `require_permission` from app.modules.auth.middleware
    • Maps each endpoint to the spec'd permission key
      (purchase.po.{create,edit,read,delete})
    • Surfaces all errors through the structured envelope (`AuthError`)

Endpoints:
    POST   /api/v1/po/preview              purchase.po.create (entity-scoped)
    POST   /api/v1/po/commit               purchase.po.edit
    GET    /api/v1/po                      purchase.po.read   (entity-scoped)
    GET    /api/v1/po/{transaction_no}     purchase.po.read   (entity-scoped)
    GET    /api/v1/po/{transaction_no}/lines purchase.po.read (entity-scoped)
    DELETE /api/v1/po/{transaction_no}     purchase.po.delete (entity re-checked)
"""

from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from app.core.middleware.request_context import AuthError
from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.purchase.schemas.po_api import (
    CommitRequest,
    CommitResponse,
    PoDeleteResponse,
    PoDetailResponse,
    PoIntimationRequest,
    PoIntimationResponse,
    PoLinesResponse,
    PoListResponse,
    PreviewResponse,
)
from app.modules.purchase.services import po_commit, po_delete, po_preview, po_query
from app.modules.purchase.services import qc_intimation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/po", tags=["PO"])


# ── allowed-entity helpers ───────────────────────────────────────────────


async def _allowed_entities_for(request: Request, user: AuthUser) -> list[str] | None:
    """Return the entities this user can act on, or None for unrestricted (admin).

    Sources, in order:
        1. admin → None (unrestricted).
        2. union of allowed_entities across user's role-permission rows for
           module=purchase, sub_module=po.
        3. fallback to user.entity (single-entity user).
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
               AND p.module = 'purchase' AND p.sub_module = 'po'
            """,
            user.role_ids,
        )
    # CR-01: mirror permission_service.check_permission's NULL-means-all-entities
    # convention. If ANY matching role-permission row has NULL allowed_entities,
    # the user has unrestricted entity scope for purchase.po.* — treat as admin
    # for scope purposes. Only a non-NULL non-empty array narrows scope.
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


def _check_entity_allowed(entity: str, allowed: list[str] | None) -> None:
    """Raise 403 if `entity` is not in `allowed`. None means admin (any)."""
    if allowed is None:
        return
    if entity.lower() not in allowed:
        raise AuthError(
            "forbidden",
            f"You do not have access to entity {entity!r}.",
            403,
            details={"entity": entity, "allowed_entities": allowed},
        )


# ═══════════════════════════════════════════════════════════════════════════
# 1. POST /api/v1/po/preview
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/preview", response_model=PreviewResponse)
async def preview(
    request: Request,
    file: UploadFile = File(...),
    entity: str = Query(..., pattern="^(cfpl|cdpl)$"),
    user: AuthUser = Depends(require_permission("purchase", "po", action="create")),
):
    allowed = await _allowed_entities_for(request, user)
    _check_entity_allowed(entity, allowed)

    if not file or not file.filename:
        raise AuthError("invalid_file", "No file attached.", 400)
    contents = await file.read()

    pool = request.app.state.db_pool
    master_items = request.app.state.master_items

    return await po_preview.preview(
        pool, file_bytes=contents, filename=file.filename,
        master_items=master_items, entity=entity,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. POST /api/v1/po/commit
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/commit", response_model=CommitResponse)
async def commit(
    request: Request,
    body: CommitRequest,
    user: AuthUser = Depends(require_permission("purchase", "po", action="edit")),
):
    allowed = await _allowed_entities_for(request, user)
    _check_entity_allowed(body.entity, allowed)

    if not body.pos:
        raise AuthError("validation_error", "pos[] must contain at least one PO", 400)

    pool = request.app.state.db_pool
    return await po_commit.commit(
        pool,
        entity=body.entity,
        mode=body.mode,
        pos=[p.model_dump(exclude_none=False) for p in body.pos],
        actor_user_id=str(user.user_id),
        actor_role=user.role_name,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. GET /api/v1/po (paginated list)
# ═══════════════════════════════════════════════════════════════════════════


def _filter_dict_from_query(**kwargs: Any) -> dict[str, Any]:
    """Strip None-valued query params before passing to the WHERE builder."""
    return {k: v for k, v in kwargs.items() if v is not None and v != ""}


@router.get("", response_model=PoListResponse)
async def list_pos(
    request: Request,
    # equality
    transaction_no: str | None = Query(None),
    entity: str | None = Query(None, pattern="^(cfpl|cdpl)$"),
    po_number: str | None = Query(None),
    voucher_type: str | None = Query(None),
    order_reference_no: str | None = Query(None),
    supplier_id: str | None = Query(None),
    vendor_supplier_name: str | None = Query(None),
    # ILIKE contains
    po_number_contains: str | None = Query(None),
    order_reference_no_contains: str | None = Query(None),
    vendor_supplier_name_contains: str | None = Query(None),
    narration_contains: str | None = Query(None),
    # date range
    po_date_from: str | None = Query(None),
    po_date_to: str | None = Query(None),
    # numeric ranges
    gross_total_min: float | None = Query(None), gross_total_max: float | None = Query(None),
    total_amount_min: float | None = Query(None), total_amount_max: float | None = Query(None),
    sgst_amount_min: float | None = Query(None), sgst_amount_max: float | None = Query(None),
    cgst_amount_min: float | None = Query(None), cgst_amount_max: float | None = Query(None),
    igst_amount_min: float | None = Query(None), igst_amount_max: float | None = Query(None),
    round_off_min: float | None = Query(None), round_off_max: float | None = Query(None),
    freight_transport_local_min: float | None = Query(None),
    freight_transport_local_max: float | None = Query(None),
    apmc_tax_min: float | None = Query(None), apmc_tax_max: float | None = Query(None),
    packing_charges_min: float | None = Query(None),
    packing_charges_max: float | None = Query(None),
    freight_transport_charges_min: float | None = Query(None),
    freight_transport_charges_max: float | None = Query(None),
    loading_unloading_charges_min: float | None = Query(None),
    loading_unloading_charges_max: float | None = Query(None),
    other_charges_non_gst_min: float | None = Query(None),
    other_charges_non_gst_max: float | None = Query(None),
    # visibility / pagination
    include_deleted: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort: str = Query("po_date:desc"),
    user: AuthUser = Depends(require_permission("purchase", "po", action="read")),
):
    sort_col, sort_dir = po_query.parse_sort(sort)
    allowed = await _allowed_entities_for(request, user)

    # If a non-admin asked for a specific entity they can't see, 403 early.
    if entity is not None:
        _check_entity_allowed(entity, allowed)

    filters = _filter_dict_from_query(
        transaction_no=transaction_no, entity=entity, po_number=po_number,
        voucher_type=voucher_type, order_reference_no=order_reference_no,
        supplier_id=supplier_id, vendor_supplier_name=vendor_supplier_name,
        po_number_contains=po_number_contains,
        order_reference_no_contains=order_reference_no_contains,
        vendor_supplier_name_contains=vendor_supplier_name_contains,
        narration_contains=narration_contains,
        po_date_from=po_date_from, po_date_to=po_date_to,
        gross_total_min=gross_total_min, gross_total_max=gross_total_max,
        total_amount_min=total_amount_min, total_amount_max=total_amount_max,
        sgst_amount_min=sgst_amount_min, sgst_amount_max=sgst_amount_max,
        cgst_amount_min=cgst_amount_min, cgst_amount_max=cgst_amount_max,
        igst_amount_min=igst_amount_min, igst_amount_max=igst_amount_max,
        round_off_min=round_off_min, round_off_max=round_off_max,
        freight_transport_local_min=freight_transport_local_min,
        freight_transport_local_max=freight_transport_local_max,
        apmc_tax_min=apmc_tax_min, apmc_tax_max=apmc_tax_max,
        packing_charges_min=packing_charges_min,
        packing_charges_max=packing_charges_max,
        freight_transport_charges_min=freight_transport_charges_min,
        freight_transport_charges_max=freight_transport_charges_max,
        loading_unloading_charges_min=loading_unloading_charges_min,
        loading_unloading_charges_max=loading_unloading_charges_max,
        other_charges_non_gst_min=other_charges_non_gst_min,
        other_charges_non_gst_max=other_charges_non_gst_max,
    )

    where_sql, params = po_query.build_where(
        filters=filters,
        include_deleted=include_deleted,
        entity_scope=allowed,
    )

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM po_header WHERE {where_sql}", *params,
        )
        total = int(total or 0)
        offset = (page - 1) * page_size
        # bind LIMIT/OFFSET as the next two params
        rows = await conn.fetch(
            f"""
            SELECT * FROM po_header
             WHERE {where_sql}
             ORDER BY {sort_col} {sort_dir} NULLS LAST, transaction_no
             LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params, page_size, offset,
        )

    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
        "items": [po_query.header_row_to_listitem(r) for r in rows],
    }


# ═══════════════════════════════════════════════════════════════════════════
# 4. GET /api/v1/po/{transaction_no} (header-only detail)
# ═══════════════════════════════════════════════════════════════════════════


async def _fetch_header(
    request: Request, transaction_no: str, include_deleted: bool, user: AuthUser,
) -> dict:
    if not (1 <= len(transaction_no) <= 64):
        raise AuthError("validation_error", "transaction_no must be 1–64 chars", 400)

    allowed = await _allowed_entities_for(request, user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if include_deleted:
            row = await conn.fetchrow(
                "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no,
            )
        else:
            row = await conn.fetchrow(
                "SELECT * FROM po_header WHERE transaction_no = $1 AND deleted_at IS NULL",
                transaction_no,
            )
    if row is None:
        raise AuthError("not_found", "Purchase order not found", 404)
    if allowed is not None and (row["entity"] or "").lower() not in allowed:
        # Don't leak existence across entities — same code as not-found.
        raise AuthError("not_found", "Purchase order not found", 404)
    return po_query.header_row_to_listitem(row)


@router.get("/{transaction_no}", response_model=PoDetailResponse)
async def get_po(
    request: Request,
    transaction_no: str,
    include_deleted: bool = Query(False),
    user: AuthUser = Depends(require_permission("purchase", "po", action="read")),
):
    header = await _fetch_header(request, transaction_no, include_deleted, user)
    return {"header": header}


# ═══════════════════════════════════════════════════════════════════════════
# 5. GET /api/v1/po/{transaction_no}/lines
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/{transaction_no}/lines", response_model=PoLinesResponse)
async def get_po_lines(
    request: Request,
    transaction_no: str,
    include_deleted: bool = Query(False),
    user: AuthUser = Depends(require_permission("purchase", "po", action="read")),
):
    header = await _fetch_header(request, transaction_no, include_deleted, user)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        line_rows = await conn.fetch(
            "SELECT * FROM po_line WHERE transaction_no = $1 ORDER BY line_number ASC",
            transaction_no,
        )
    lines = [po_query.line_row_to_dict(r) for r in line_rows]
    return {
        "header": header,
        "total_lines": len(lines),
        "lines": lines,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 6. DELETE /api/v1/po/{transaction_no}?reason=...
# ═══════════════════════════════════════════════════════════════════════════


@router.delete("/{transaction_no}", response_model=PoDeleteResponse)
async def delete_po(
    request: Request,
    transaction_no: str,
    reason: str = Query(..., min_length=1),
    user: AuthUser = Depends(require_permission("purchase", "po", action="delete")),
):
    """Soft-delete with entity recheck and STORE_HEAD gate when boxes weighed."""
    if not (1 <= len(transaction_no) <= 64):
        raise AuthError("validation_error", "transaction_no must be 1–64 chars", 400)

    # We re-check entity scope against the resolved PO inside the service —
    # but allowed_entities is checked here against the row's entity for
    # consistency with read endpoints (don't leak existence across entities).
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT entity FROM po_header WHERE transaction_no = $1", transaction_no,
        )
    if row is None:
        raise AuthError("not_found", "Purchase order not found", 404)

    allowed = await _allowed_entities_for(request, user)
    if allowed is not None and (row["entity"] or "").lower() not in allowed:
        raise AuthError("not_found", "Purchase order not found", 404)

    return await po_delete.soft_delete(
        pool,
        transaction_no=transaction_no,
        reason=reason,
        actor_user_id=str(user.user_id),
        actor_role=user.role_name,
        actor_is_admin=user.is_admin,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 7. POST /api/v1/po/{transaction_no}/intimation
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/{transaction_no}/intimation", response_model=PoIntimationResponse)
async def send_qc_intimation(
    request: Request,
    transaction_no: str,
    body: PoIntimationRequest,
    user: AuthUser = Depends(require_permission("purchase", "po", action="edit")),
):
    """Send a QC Inward Intimation WhatsApp message to all active qc_member /
    qc_manager users, with a generated article-list PNG as the header image.

    Returns per-recipient send status; never hard-fails on WhatsApp errors so
    the caller can surface partial success.
    """
    # Load PO header (entity-scoped, 404 if missing/wrong entity).
    header = await _fetch_header(request, transaction_no, include_deleted=False, user=user)

    # Fetch selected lines.
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        line_rows = await conn.fetch(
            "SELECT * FROM po_line WHERE transaction_no = $1 AND line_number = ANY($2::int[]) ORDER BY line_number",
            transaction_no, body.line_numbers,
        )

    article_names = [
        (r["sku_name"] or r["particulars"] or "—") for r in line_rows
    ]
    if not article_names:
        raise AuthError("validation_error", "No matching article lines for this PO", 400)

    # Persist one qc_intimation arrival row per selected article BEFORE
    # attempting WhatsApp send — arrivals are recorded even when WA is disabled.
    line_dicts = [dict(r) for r in line_rows]
    await qc_intimation.record_arrivals(
        pool,
        po_number=header.get("po_number"),
        transaction_no=transaction_no,
        supplier_id=header.get("supplier_id"),
        supplier_name=header.get("vendor_supplier_name"),
        entity=header.get("entity"),
        vehicle_no=body.vehicle_number,
        invoice_no=body.invoice_no,
        lines=line_dicts,
    )

    result = await qc_intimation.send_intimation(
        pool,
        po_number=header.get("po_number") or transaction_no,
        vendor_name=header.get("vendor_supplier_name") or "—",
        article_names=article_names,
        vehicle_number=body.vehicle_number,
        invoice_no=body.invoice_no,
    )
    return result
