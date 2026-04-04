import logging
import math
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File

from app.modules.so.schemas import (
    SOUploadResponse,
    SOCreateRequest,
    SOViewResponse,
    SOExportResponse,
    SOHeaderOut,
    SOLineOut,
    GSTReconResponse,
    GSTReconLineOut,
    GSTReconSummary,
    SKUDetail,
    SKUDropdownOptions,
    SKULookupResponse,
    SOUpdatePreviewResponse,
    SOUpdateConfirmRequest,
    SOUpdateConfirmResponse,
    SOManualUpdateRequest,
    SOManualUpdateResponse,
)
from app.modules.so.services.ingest import (
    ingest_sales_register,
    ingest_manual_so,
    ingest_so_book,
)
from app.modules.so.services.updater import (
    preview_sales_register_update,
    confirm_sales_register_update,
    manual_update_so,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/so", tags=["Sales Orders"])


@router.post("/upload", response_model=SOUploadResponse, status_code=201)
async def upload_excel(request: Request, file: UploadFile = File(...)):
    """Upload a Sales Register Excel file. Processes entirely in memory."""

    if not file or not file.filename:
        raise HTTPException(400, detail="No file attached.")

    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, detail="Only Excel files (.xlsx) are accepted.")

    contents = await file.read()
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(400, detail="File too large. Maximum 50 MB.")

    pool = request.app.state.db_pool
    master_items = request.app.state.master_items

    try:
        result = await ingest_sales_register(pool, contents, master_items)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("Excel ingestion failed")
        error_msg = str(e)
        if "unique" in error_msg.lower() or "duplicate" in error_msg.lower():
            raise HTTPException(
                409,
                detail=f"Duplicate data detected. Some Sales Orders in this file may already exist. {error_msg}",
            )
        raise HTTPException(500, detail=f"Failed to process Excel file: {error_msg}")

    return SOUploadResponse(**result)


@router.post("/upload-so-book", response_model=SOUploadResponse, status_code=201)
async def upload_so_book(request: Request, file: UploadFile = File(...)):
    """Upload a Sales Order Book Excel file (state-machine parsed, GST apportioned to lines)."""

    if not file or not file.filename:
        raise HTTPException(400, detail="No file attached.")

    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, detail="Only Excel files (.xlsx) are accepted.")

    contents = await file.read()
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(400, detail="File too large. Maximum 50 MB.")

    pool = request.app.state.db_pool
    master_items = request.app.state.master_items

    try:
        result = await ingest_so_book(pool, contents, master_items)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("SO Book ingestion failed")
        error_msg = str(e)
        if "unique" in error_msg.lower() or "duplicate" in error_msg.lower():
            raise HTTPException(409, detail=f"Duplicate data detected. {error_msg}")
        raise HTTPException(500, detail=f"Failed to process SO Book: {error_msg}")

    return SOUploadResponse(**result)


@router.post("/update-preview", response_model=SOUpdatePreviewResponse)
async def update_preview(request: Request, file: UploadFile = File(...)):
    """
    Upload an Excel file to preview changes against existing SOs.
    Only SOs with actual data differences are returned.
    Numeric fields are compared at 3-decimal precision.
    """
    if not file or not file.filename:
        raise HTTPException(400, detail="No file attached.")

    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, detail="Only Excel files (.xlsx) are accepted.")

    contents = await file.read()
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(400, detail="File too large. Maximum 50 MB.")

    pool = request.app.state.db_pool

    try:
        result = await preview_sales_register_update(pool, contents)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("Update preview failed")
        raise HTTPException(500, detail=f"Failed to preview update: {str(e)}")

    # Store the file bytes in app state for the confirm step
    # Keyed by a hash so multiple previews can coexist
    import hashlib
    file_hash = hashlib.sha256(contents).hexdigest()[:16]
    if not hasattr(request.app.state, "pending_updates"):
        request.app.state.pending_updates = {}
    request.app.state.pending_updates[file_hash] = contents

    # Include file_hash in response so frontend can reference it
    result["file_hash"] = file_hash

    return SOUpdatePreviewResponse(**result)


@router.post("/update-confirm", response_model=SOUpdateConfirmResponse)
async def update_confirm(request: Request, body: SOUpdateConfirmRequest, file_hash: str = Query(...)):
    """
    Confirm and apply updates for the selected SOs.
    Pass the file_hash from the preview response and the so_ids to update.
    """
    pool = request.app.state.db_pool
    master_items = request.app.state.master_items

    pending = getattr(request.app.state, "pending_updates", {})
    file_bytes = pending.get(file_hash)
    if not file_bytes:
        raise HTTPException(400, detail="Preview expired or invalid file_hash. Please re-upload for preview.")

    if not body.so_ids:
        raise HTTPException(400, detail="No SO IDs provided for update.")

    try:
        result = await confirm_sales_register_update(pool, body.so_ids, file_bytes, master_items)
    except Exception as e:
        logger.exception("Update confirm failed")
        error_msg = str(e)
        raise HTTPException(500, detail=f"Failed to apply update: {error_msg}")

    # Clean up stored file
    pending.pop(file_hash, None)

    return SOUpdateConfirmResponse(**result)


@router.put("/update", response_model=SOManualUpdateResponse)
async def manual_update(request: Request, body: SOManualUpdateRequest):
    """
    Manually update a single SO.
    Frontend sends old state + new state; backend validates old state matches DB
    (stale check), computes diff, and applies the new data.
    """
    if not body.new_lines:
        raise HTTPException(400, detail="At least one line is required in new_lines.")

    pool = request.app.state.db_pool
    master_items = request.app.state.master_items

    try:
        result = await manual_update_so(pool, body.model_dump(), master_items)
    except ValueError as e:
        raise HTTPException(409, detail=str(e))
    except Exception as e:
        logger.exception("Manual SO update failed")
        raise HTTPException(500, detail=f"Failed to update SO: {str(e)}")

    return SOManualUpdateResponse(**result)


@router.post("/create", response_model=SOUploadResponse, status_code=201)
async def create_so(request: Request, body: SOCreateRequest):
    """Manually create a single SO with multiple articles (JSON body)."""

    if not body.lines:
        raise HTTPException(400, detail="At least one article line is required.")

    pool = request.app.state.db_pool
    master_items = request.app.state.master_items

    try:
        result = await ingest_manual_so(pool, body.model_dump(), master_items)
    except Exception as e:
        logger.exception("Manual SO creation failed")
        error_msg = str(e)
        if "unique" in error_msg.lower() or "duplicate" in error_msg.lower():
            raise HTTPException(409, detail=f"Duplicate data detected. {error_msg}")
        raise HTTPException(500, detail=f"Failed to create SO: {error_msg}")

    return SOUploadResponse(**result)


# --- Shared helpers for /view and /export ---


def _build_where_clause(
    *,
    search, status, company, voucher_type, customer_name, common_customer_name,
    date_from, date_to, item_category, sub_category, uom, grp_code,
    rate_type, item_type, sales_group, match_source, line_status,
) -> tuple[str, list]:
    """Build a WHERE clause and params list from filter values."""
    conditions = []
    params: list = []
    param_idx = 0

    if search:
        param_idx += 1
        search_param = f"%{search}%"
        conditions.append(
            f"(h.so_number ILIKE ${param_idx}"
            f" OR h.customer_name ILIKE ${param_idx}"
            f" OR h.common_customer_name ILIKE ${param_idx}"
            f" OR h.company ILIKE ${param_idx})"
        )
        params.append(search_param)

    # Header-level filters: comma-separated → OR within field, AND across fields
    for col, val in [
        ("h.company", company),
        ("h.voucher_type", voucher_type),
        ("h.customer_name", customer_name),
        ("h.common_customer_name", common_customer_name),
    ]:
        if val:
            vals = [v.strip() for v in val.split(",") if v.strip()]
            placeholders = []
            for v in vals:
                param_idx += 1
                placeholders.append(f"${param_idx}")
                params.append(v)
            conditions.append(f"{col} IN ({', '.join(placeholders)})")

    # Date range filter — auto-swap if reversed, single date if both are same
    if date_from or date_to:
        try:
            d1 = date.fromisoformat(date_from) if date_from else None
            d2 = date.fromisoformat(date_to) if date_to else None
        except ValueError:
            raise HTTPException(400, detail="Invalid date format. Use YYYY-MM-DD.")

        if d1 and d2:
            start, end = min(d1, d2), max(d1, d2)
            if start == end:
                param_idx += 1
                conditions.append(f"h.so_date = ${param_idx}")
                params.append(start)
            else:
                param_idx += 1
                start_param = param_idx
                param_idx += 1
                end_param = param_idx
                conditions.append(f"h.so_date >= ${start_param} AND h.so_date <= ${end_param}")
                params.append(start)
                params.append(end)
        elif d1:
            param_idx += 1
            conditions.append(f"h.so_date >= ${param_idx}")
            params.append(d1)
        else:
            param_idx += 1
            conditions.append(f"h.so_date <= ${param_idx}")
            params.append(d2)

    # Line-level filters: keep SOs that have at least one line matching
    line_filters = {
        "item_category": item_category,
        "sub_category": sub_category,
        "uom": uom,
        "grp_code": grp_code,
        "rate_type": rate_type,
        "item_type": item_type,
        "sales_group": sales_group,
        "match_source": match_source,
        "status": line_status,
    }
    for col, val in line_filters.items():
        if val:
            vals = [v.strip() for v in val.split(",") if v.strip()]
            placeholders = []
            for v in vals:
                param_idx += 1
                placeholders.append(f"${param_idx}")
                params.append(v)
            conditions.append(
                f"EXISTS (SELECT 1 FROM so_line sl WHERE sl.so_id = h.so_id AND sl.{col} IN ({', '.join(placeholders)}))"
            )

    # Status filter: derived from GST recon counts per SO
    if status:
        if status == "mismatch":
            conditions.append(
                "EXISTS (SELECT 1 FROM so_gst_reconciliation g WHERE g.so_id = h.so_id AND g.status = 'mismatch')"
            )
        elif status == "warning":
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM so_gst_reconciliation g WHERE g.so_id = h.so_id AND g.status = 'mismatch')"
                " AND EXISTS (SELECT 1 FROM so_gst_reconciliation g WHERE g.so_id = h.so_id AND g.status = 'warning')"
            )
        else:  # ok
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM so_gst_reconciliation g WHERE g.so_id = h.so_id AND g.status = 'mismatch')"
                " AND NOT EXISTS (SELECT 1 FROM so_gst_reconciliation g WHERE g.so_id = h.so_id AND g.status = 'warning')"
            )

    where_clause = (" AND ".join(conditions)) if conditions else "TRUE"
    return where_clause, params


def _build_so_detail(h, so_lines, recon_by_line) -> dict:
    """Build a single SO detail dict from header, lines, and recon data."""
    so_id = h["so_id"]
    so_lines_with_recon = []
    so_gst_ok = 0
    so_gst_mismatch = 0
    so_gst_warning = 0

    for l in so_lines:
        so_line_id = l["so_line_id"]
        r = recon_by_line.get(so_line_id)

        if r:
            st = r.get("status", "ok")
            if st == "ok":
                so_gst_ok += 1
            elif st == "mismatch":
                so_gst_mismatch += 1
            else:
                so_gst_warning += 1

        so_lines_with_recon.append({
            "line": {
                "so_line_id": so_line_id,
                "line_number": l["line_number"],
                "sku_name": l.get("sku_name"),
                "item_category": l.get("item_category"),
                "sub_category": l.get("sub_category"),
                "uom": l.get("uom"),
                "grp_code": l.get("grp_code"),
                "quantity": float(l["quantity"]) if l.get("quantity") is not None else None,
                "quantity_units": int(l["quantity_units"]) if l.get("quantity_units") is not None else None,
                "rate_inr": float(l["rate_inr"]) if l.get("rate_inr") is not None else None,
                "rate_type": l.get("rate_type"),
                "amount_inr": float(l["amount_inr"]) if l.get("amount_inr") is not None else None,
                "igst_amount": float(l["igst_amount"]) if l.get("igst_amount") is not None else None,
                "sgst_amount": float(l["sgst_amount"]) if l.get("sgst_amount") is not None else None,
                "cgst_amount": float(l["cgst_amount"]) if l.get("cgst_amount") is not None else None,
                "total_amount_inr": float(l["total_amount_inr"]) if l.get("total_amount_inr") is not None else None,
                "apmc_amount": float(l["apmc_amount"]) if l.get("apmc_amount") is not None else None,
                "packing_amount": float(l["packing_amount"]) if l.get("packing_amount") is not None else None,
                "freight_amount": float(l["freight_amount"]) if l.get("freight_amount") is not None else None,
                "processing_amount": float(l["processing_amount"]) if l.get("processing_amount") is not None else None,
                "item_type": l.get("item_type"),
                "item_description": l.get("item_description"),
                "sales_group": l.get("sales_group"),
                "match_score": float(l["match_score"]) if l.get("match_score") is not None else None,
                "match_source": l.get("match_source"),
                "status": l.get("status", "pending"),
            },
            "gst_recon": {
                "so_line_id": so_line_id,
                "line_number": l["line_number"],
                "sku_name": l.get("sku_name"),
                "expected_gst_rate": float(r["expected_gst_rate"]) if r and r.get("expected_gst_rate") is not None else None,
                "actual_gst_rate": float(r["actual_gst_rate"]) if r and r.get("actual_gst_rate") is not None else None,
                "expected_gst_amount": float(r["expected_gst_amount"]) if r and r.get("expected_gst_amount") is not None else None,
                "actual_gst_amount": float(r["actual_gst_amount"]) if r and r.get("actual_gst_amount") is not None else None,
                "gst_difference": float(r["gst_difference"]) if r and r.get("gst_difference") is not None else None,
                "gst_type": r.get("gst_type") if r else None,
                "gst_type_valid": r.get("gst_type_valid") if r else None,
                "sgst_cgst_equal": r.get("sgst_cgst_equal") if r else None,
                "total_with_gst_valid": r.get("total_with_gst_valid") if r else None,
                "uom_match": r.get("uom_match") if r else None,
                "item_type_flag": r.get("item_type_flag") if r else None,
                "rate_type": r.get("rate_type") if r else None,
                "matched_item_description": r.get("matched_item_description") if r else None,
                "matched_item_type": r.get("matched_item_type") if r else None,
                "matched_item_category": r.get("matched_item_category") if r else None,
                "matched_sub_category": r.get("matched_sub_category") if r else None,
                "matched_sales_group": r.get("matched_sales_group") if r else None,
                "matched_uom": float(r["matched_uom"]) if r and r.get("matched_uom") is not None else None,
                "match_score": float(r["match_score"]) if r and r.get("match_score") is not None else None,
                "status": r.get("status", "ok") if r else "ok",
                "notes": r.get("notes") if r else None,
            },
        })

    return {
        "so_id": so_id,
        "so_number": h.get("so_number"),
        "so_date": str(h["so_date"]) if h.get("so_date") else None,
        "customer_name": h.get("customer_name"),
        "common_customer_name": h.get("common_customer_name"),
        "company": h.get("company"),
        "voucher_type": h.get("voucher_type"),
        "total_lines": len(so_lines),
        "gst_ok": so_gst_ok,
        "gst_mismatch": so_gst_mismatch,
        "gst_warning": so_gst_warning,
        "lines": so_lines_with_recon,
    }


async def _fetch_so_details(pool, so_ids, headers) -> list[dict]:
    """Fetch lines + recon for a list of SO IDs and build detail dicts."""
    if not so_ids:
        return []

    lines = await pool.fetch(
        "SELECT * FROM so_line WHERE so_id = ANY($1) ORDER BY so_id, line_number",
        so_ids,
    )
    recons = await pool.fetch(
        "SELECT * FROM so_gst_reconciliation WHERE so_id = ANY($1)",
        so_ids,
    )

    lines_by_so: dict[int, list] = {}
    for l in lines:
        lines_by_so.setdefault(l["so_id"], []).append(l)

    recon_by_line = {r["so_line_id"]: r for r in recons}

    return [
        _build_so_detail(h, lines_by_so.get(h["so_id"], []), recon_by_line)
        for h in headers
    ]


# --- Endpoints ---


@router.get("/view", response_model=SOViewResponse)
async def view_all_sos(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    search: str = Query(None),
    status: str = Query(None, pattern="^(ok|mismatch|warning)$"),
    sort_by: str = Query("so_date", pattern="^(so_id|so_number|so_date|customer_name|company)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    company: str = Query(None),
    voucher_type: str = Query(None),
    customer_name: str = Query(None),
    common_customer_name: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    item_category: str = Query(None),
    sub_category: str = Query(None),
    uom: str = Query(None),
    grp_code: str = Query(None),
    rate_type: str = Query(None),
    item_type: str = Query(None),
    sales_group: str = Query(None),
    match_source: str = Query(None),
    line_status: str = Query(None),
):
    """View Sales Orders with server-side pagination, filtering, sorting, and search."""
    pool = request.app.state.db_pool

    # --- Filter options (from ALL data in DB, always unfiltered) ---
    header_opts = await pool.fetchrow(
        """
        SELECT
            COALESCE(array_agg(DISTINCT company) FILTER (WHERE company IS NOT NULL), '{}') AS companies,
            COALESCE(array_agg(DISTINCT voucher_type) FILTER (WHERE voucher_type IS NOT NULL), '{}') AS voucher_types,
            COALESCE(array_agg(DISTINCT customer_name) FILTER (WHERE customer_name IS NOT NULL), '{}') AS customer_names,
            COALESCE(array_agg(DISTINCT common_customer_name) FILTER (WHERE common_customer_name IS NOT NULL), '{}') AS common_customer_names
        FROM so_header
        """
    )
    line_opts = await pool.fetchrow(
        """
        SELECT
            COALESCE(array_agg(DISTINCT item_category) FILTER (WHERE item_category IS NOT NULL), '{}') AS item_categories,
            COALESCE(array_agg(DISTINCT sub_category) FILTER (WHERE sub_category IS NOT NULL), '{}') AS sub_categories,
            COALESCE(array_agg(DISTINCT uom) FILTER (WHERE uom IS NOT NULL), '{}') AS uoms,
            COALESCE(array_agg(DISTINCT grp_code) FILTER (WHERE grp_code IS NOT NULL), '{}') AS grp_codes,
            COALESCE(array_agg(DISTINCT rate_type) FILTER (WHERE rate_type IS NOT NULL), '{}') AS rate_types,
            COALESCE(array_agg(DISTINCT item_type) FILTER (WHERE item_type IS NOT NULL), '{}') AS item_types,
            COALESCE(array_agg(DISTINCT sales_group) FILTER (WHERE sales_group IS NOT NULL), '{}') AS sales_groups,
            COALESCE(array_agg(DISTINCT match_source) FILTER (WHERE match_source IS NOT NULL), '{}') AS match_sources,
            COALESCE(array_agg(DISTINCT status) FILTER (WHERE status IS NOT NULL), '{}') AS statuses
        FROM so_line
        """
    )
    filter_options = {
        "companies": sorted(header_opts["companies"]),
        "voucher_types": sorted(header_opts["voucher_types"]),
        "customer_names": sorted(header_opts["customer_names"]),
        "common_customer_names": sorted(header_opts["common_customer_names"]),
        "item_categories": sorted(line_opts["item_categories"]),
        "sub_categories": sorted(line_opts["sub_categories"]),
        "uoms": sorted(line_opts["uoms"]),
        "grp_codes": sorted(line_opts["grp_codes"]),
        "rate_types": sorted(line_opts["rate_types"]),
        "item_types": sorted(line_opts["item_types"]),
        "sales_groups": sorted(line_opts["sales_groups"]),
        "match_sources": sorted(line_opts["match_sources"]),
        "statuses": sorted(line_opts["statuses"]),
    }

    # --- Build WHERE clause ---
    where_clause, params = _build_where_clause(
        search=search, status=status, company=company, voucher_type=voucher_type,
        customer_name=customer_name, common_customer_name=common_customer_name,
        date_from=date_from, date_to=date_to, item_category=item_category,
        sub_category=sub_category, uom=uom, grp_code=grp_code,
        rate_type=rate_type, item_type=item_type, sales_group=sales_group,
        match_source=match_source, line_status=line_status,
    )

    # --- Global summary across ALL filtered SOs ---
    summary_row = await pool.fetchrow(
        f"""
        SELECT
            COUNT(*) AS total_sos,
            COALESCE(SUM(lc.cnt), 0) AS total_lines,
            COALESCE(SUM(lc.matched), 0) AS matched_lines,
            COALESCE(SUM(lc.unmatched), 0) AS unmatched_lines,
            COALESCE(SUM(lc.gst_ok), 0) AS gst_ok,
            COALESCE(SUM(lc.gst_mismatch), 0) AS gst_mismatch,
            COALESCE(SUM(lc.gst_warning), 0) AS gst_warning,
            COUNT(*) FILTER (WHERE lc.gst_mismatch > 0) AS so_mismatch,
            COUNT(*) FILTER (WHERE lc.gst_mismatch = 0 AND lc.gst_warning > 0) AS so_warning,
            COUNT(*) FILTER (WHERE lc.gst_mismatch = 0 AND lc.gst_warning = 0) AS so_ok
        FROM so_header h
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) AS cnt,
                COUNT(*) FILTER (WHERE l.match_source IS NOT NULL) AS matched,
                COUNT(*) FILTER (WHERE l.match_source IS NULL) AS unmatched,
                COUNT(*) FILTER (WHERE r.status = 'ok') AS gst_ok,
                COUNT(*) FILTER (WHERE r.status = 'mismatch') AS gst_mismatch,
                COUNT(*) FILTER (WHERE r.status = 'warning') AS gst_warning
            FROM so_line l
            LEFT JOIN so_gst_reconciliation r ON r.so_line_id = l.so_line_id
            WHERE l.so_id = h.so_id
        ) lc ON TRUE
        WHERE {where_clause}
        """,
        *params,
    )

    total = summary_row["total_sos"]
    total_pages = max(1, math.ceil(total / page_size))

    summary = {
        "total_sos": total,
        "total_lines": summary_row["total_lines"],
        "matched_lines": summary_row["matched_lines"],
        "unmatched_lines": summary_row["unmatched_lines"],
        "gst_ok": summary_row["gst_ok"],
        "gst_mismatch": summary_row["gst_mismatch"],
        "gst_warning": summary_row["gst_warning"],
        "so_ok": summary_row["so_ok"],
        "so_mismatch": summary_row["so_mismatch"],
        "so_warning": summary_row["so_warning"],
    }

    # --- Paginated SO headers ---
    sort_col = {
        "so_id": "h.so_id",
        "so_number": "h.so_number",
        "so_date": "h.so_date",
        "customer_name": "h.customer_name",
        "company": "h.company",
    }[sort_by]

    offset = (page - 1) * page_size
    param_idx = len(params)
    param_idx += 1
    limit_param = param_idx
    param_idx += 1
    offset_param = param_idx

    paginated_headers = await pool.fetch(
        f"""
        SELECT * FROM so_header h
        WHERE {where_clause}
        ORDER BY {sort_col} {sort_order} NULLS LAST
        LIMIT ${limit_param} OFFSET ${offset_param}
        """,
        *params, page_size, offset,
    )

    so_ids = [h["so_id"] for h in paginated_headers]
    all_so_details = await _fetch_so_details(pool, so_ids, paginated_headers)

    return SOViewResponse(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        summary=summary,
        filter_options=filter_options,
        sales_orders=all_so_details,
    )


@router.get("/export", response_model=SOExportResponse)
async def export_sos(
    request: Request,
    search: str = Query(None),
    status: str = Query(None, pattern="^(ok|mismatch|warning)$"),
    sort_by: str = Query("so_date", pattern="^(so_id|so_number|so_date|customer_name|company)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    company: str = Query(None),
    voucher_type: str = Query(None),
    customer_name: str = Query(None),
    common_customer_name: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    item_category: str = Query(None),
    sub_category: str = Query(None),
    uom: str = Query(None),
    grp_code: str = Query(None),
    rate_type: str = Query(None),
    item_type: str = Query(None),
    sales_group: str = Query(None),
    match_source: str = Query(None),
    line_status: str = Query(None),
):
    """Export all filtered Sales Orders (no pagination) for download."""
    pool = request.app.state.db_pool

    where_clause, params = _build_where_clause(
        search=search, status=status, company=company, voucher_type=voucher_type,
        customer_name=customer_name, common_customer_name=common_customer_name,
        date_from=date_from, date_to=date_to, item_category=item_category,
        sub_category=sub_category, uom=uom, grp_code=grp_code,
        rate_type=rate_type, item_type=item_type, sales_group=sales_group,
        match_source=match_source, line_status=line_status,
    )

    sort_col = {
        "so_id": "h.so_id",
        "so_number": "h.so_number",
        "so_date": "h.so_date",
        "customer_name": "h.customer_name",
        "company": "h.company",
    }[sort_by]

    headers = await pool.fetch(
        f"""
        SELECT * FROM so_header h
        WHERE {where_clause}
        ORDER BY {sort_col} {sort_order} NULLS LAST
        """,
        *params,
    )

    so_ids = [h["so_id"] for h in headers]
    all_so_details = await _fetch_so_details(pool, so_ids, headers)

    return SOExportResponse(
        total=len(all_so_details),
        sales_orders=all_so_details,
    )


@router.get("/gst-reconciliation/summary", response_model=GSTReconSummary)
async def get_gst_summary(request: Request):
    """Aggregate GST reconciliation summary across all SOs."""
    pool = request.app.state.db_pool

    row = await pool.fetchrow(
        """
        SELECT
            COUNT(DISTINCT so_id) AS total_sos,
            COUNT(*) AS total_lines,
            COUNT(*) FILTER (WHERE status = 'ok') AS ok_count,
            COUNT(*) FILTER (WHERE status = 'mismatch') AS mismatch_count,
            COUNT(*) FILTER (WHERE status = 'warning') AS warning_count
        FROM so_gst_reconciliation
        """
    )

    return GSTReconSummary(
        total_sos=row["total_sos"],
        total_lines=row["total_lines"],
        ok_count=row["ok_count"],
        mismatch_count=row["mismatch_count"],
        warning_count=row["warning_count"],
    )


@router.get("/sku-lookup", response_model=SKULookupResponse)
async def sku_lookup(
    request: Request,
    item_type: str = Query(None),
    item_group: str = Query(None),
    sub_group: str = Query(None),
    sales_group: str = Query(None),
    search: str = Query(None),
    particulars: str = Query(None),
):
    """
    Cascading dropdown lookup against the all_sku master table.

    - Pass any combination of filters to narrow down the other dropdowns.
    - Pass `search` for space/case-tolerant text search on particulars.
    - Pass `particulars` to get the full SKU detail (uom, gst, etc.).
    """
    pool = request.app.state.db_pool

    conditions: list[str] = []
    params: list = []
    idx = 0

    # Exact dropdown filters — case & space insensitive
    for col, val in [
        ("item_type", item_type),
        ("item_group", item_group),
        ("sub_group", sub_group),
        ("sale_group", sales_group),
    ]:
        if val:
            idx += 1
            conditions.append(
                f"LOWER(TRIM(REGEXP_REPLACE({col}, '\\s+', ' ', 'g')))"
                f" = LOWER(TRIM(REGEXP_REPLACE(${idx}, '\\s+', ' ', 'g')))"
            )
            params.append(val)

    # Text search on particulars — case & space insensitive
    # "toor  dal" → "%toor%dal%" matches "Toor Dal 1kg", "TOOR DAL", etc.
    if search:
        tokens = search.strip().split()
        if tokens:
            pattern = "%" + "%".join(t.lower() for t in tokens) + "%"
            idx += 1
            conditions.append(
                f"LOWER(REGEXP_REPLACE(particulars, '\\s+', ' ', 'g')) LIKE ${idx}"
            )
            params.append(pattern)

    where = " AND ".join(conditions) if conditions else "TRUE"

    rows = await pool.fetch(
        f"SELECT * FROM all_sku WHERE {where} ORDER BY particulars",
        *params,
    )

    # Build distinct, sorted dropdown options from filtered rows
    options = SKUDropdownOptions(
        item_types=sorted({r["item_type"] for r in rows if r.get("item_type")}),
        particulars=[r["particulars"] for r in rows if r.get("particulars")],
        item_groups=sorted({r["item_group"] for r in rows if r.get("item_group")}),
        sub_groups=sorted({r["sub_group"] for r in rows if r.get("sub_group")}),
        sales_groups=sorted({r["sale_group"] for r in rows if r.get("sale_group")}),
    )

    # When a specific particulars value is selected, return full SKU detail
    selected_item = None
    if particulars:
        normalized = " ".join(particulars.strip().split()).lower()
        for r in rows:
            if r.get("particulars") and " ".join(r["particulars"].strip().split()).lower() == normalized:
                selected_item = SKUDetail(
                    sku_id=r["sku_id"],
                    particulars=r["particulars"],
                    item_type=r.get("item_type"),
                    item_group=r.get("item_group"),
                    sub_group=r.get("sub_group"),
                    uom=float(r["uom"]) if r.get("uom") is not None else None,
                    sale_group=r.get("sale_group"),
                    gst=float(r["gst"]) if r.get("gst") is not None else None,
                )
                break

        # Fallback: search without other filters in case it was excluded
        if not selected_item:
            row = await pool.fetchrow(
                "SELECT * FROM all_sku WHERE LOWER(TRIM(REGEXP_REPLACE(particulars, '\\s+', ' ', 'g')))"
                " = LOWER(TRIM(REGEXP_REPLACE($1, '\\s+', ' ', 'g'))) LIMIT 1",
                particulars,
            )
            if row:
                selected_item = SKUDetail(
                    sku_id=row["sku_id"],
                    particulars=row["particulars"],
                    item_type=row.get("item_type"),
                    item_group=row.get("item_group"),
                    sub_group=row.get("sub_group"),
                    uom=float(row["uom"]) if row.get("uom") is not None else None,
                    sale_group=row.get("sale_group"),
                    gst=float(row["gst"]) if row.get("gst") is not None else None,
                )

    return SKULookupResponse(options=options, selected_item=selected_item)


@router.get("/{so_id}", response_model=SOHeaderOut)
async def get_so(request: Request, so_id: int):
    """Get SO header with all line items."""
    pool = request.app.state.db_pool

    header = await pool.fetchrow("SELECT * FROM so_header WHERE so_id = $1", so_id)
    if not header:
        raise HTTPException(404, detail="SO not found.")

    lines = await pool.fetch(
        "SELECT * FROM so_line WHERE so_id = $1 ORDER BY line_number", so_id
    )

    return SOHeaderOut(
        so_id=header["so_id"],
        so_number=header.get("so_number"),
        so_date=str(header["so_date"]) if header.get("so_date") else None,
        customer_name=header.get("customer_name"),
        common_customer_name=header.get("common_customer_name"),
        company=header.get("company"),
        voucher_type=header.get("voucher_type"),
        total_lines=len(lines),
        lines=[
            SOLineOut(
                so_line_id=l["so_line_id"],
                line_number=l["line_number"],
                sku_name=l.get("sku_name"),
                item_category=l.get("item_category"),
                sub_category=l.get("sub_category"),
                uom=l.get("uom"),
                grp_code=l.get("grp_code"),
                quantity=float(l["quantity"]) if l.get("quantity") is not None else None,
                quantity_units=int(l["quantity_units"]) if l.get("quantity_units") is not None else None,
                rate_inr=float(l["rate_inr"]) if l.get("rate_inr") is not None else None,
                rate_type=l.get("rate_type"),
                amount_inr=float(l["amount_inr"]) if l.get("amount_inr") is not None else None,
                igst_amount=float(l["igst_amount"]) if l.get("igst_amount") is not None else None,
                sgst_amount=float(l["sgst_amount"]) if l.get("sgst_amount") is not None else None,
                cgst_amount=float(l["cgst_amount"]) if l.get("cgst_amount") is not None else None,
                total_amount_inr=float(l["total_amount_inr"]) if l.get("total_amount_inr") is not None else None,
                apmc_amount=float(l["apmc_amount"]) if l.get("apmc_amount") is not None else None,
                packing_amount=float(l["packing_amount"]) if l.get("packing_amount") is not None else None,
                freight_amount=float(l["freight_amount"]) if l.get("freight_amount") is not None else None,
                processing_amount=float(l["processing_amount"]) if l.get("processing_amount") is not None else None,
                item_type=l.get("item_type"),
                item_description=l.get("item_description"),
                sales_group=l.get("sales_group"),
                match_score=float(l["match_score"]) if l.get("match_score") is not None else None,
                match_source=l.get("match_source"),
                status=l.get("status", "pending"),
            )
            for l in lines
        ],
    )


@router.get("/{so_id}/gst-reconciliation", response_model=GSTReconResponse)
async def get_gst_reconciliation(request: Request, so_id: int):
    """Get GST reconciliation results for an SO."""
    pool = request.app.state.db_pool

    header = await pool.fetchrow("SELECT so_id FROM so_header WHERE so_id = $1", so_id)
    if not header:
        raise HTTPException(404, detail="SO not found.")

    rows = await pool.fetch(
        """
        SELECT r.*, l.line_number, l.sku_name
        FROM so_gst_reconciliation r
        JOIN so_line l ON r.so_line_id = l.so_line_id
        WHERE r.so_id = $1
        ORDER BY l.line_number
        """,
        so_id,
    )

    ok = sum(1 for r in rows if r["status"] == "ok")
    mismatch = sum(1 for r in rows if r["status"] == "mismatch")
    warning = sum(1 for r in rows if r["status"] == "warning")

    return GSTReconResponse(
        so_id=so_id,
        total_lines=len(rows),
        ok_count=ok,
        mismatch_count=mismatch,
        warning_count=warning,
        lines=[
            GSTReconLineOut(
                recon_id=r["recon_id"],
                so_line_id=r["so_line_id"],
                line_number=r.get("line_number"),
                sku_name=r.get("sku_name"),
                expected_gst_rate=float(r["expected_gst_rate"]) if r.get("expected_gst_rate") is not None else None,
                actual_gst_rate=float(r["actual_gst_rate"]) if r.get("actual_gst_rate") is not None else None,
                expected_gst_amount=float(r["expected_gst_amount"]) if r.get("expected_gst_amount") is not None else None,
                actual_gst_amount=float(r["actual_gst_amount"]) if r.get("actual_gst_amount") is not None else None,
                gst_difference=float(r["gst_difference"]) if r.get("gst_difference") is not None else None,
                gst_type=r.get("gst_type"),
                gst_type_valid=r.get("gst_type_valid"),
                sgst_cgst_equal=r.get("sgst_cgst_equal"),
                total_with_gst_valid=r.get("total_with_gst_valid"),
                uom_match=r.get("uom_match"),
                item_type_flag=r.get("item_type_flag"),
                rate_type=r.get("rate_type"),
                matched_item_description=r.get("matched_item_description"),
                matched_item_type=r.get("matched_item_type"),
                matched_item_category=r.get("matched_item_category"),
                matched_sub_category=r.get("matched_sub_category"),
                matched_sales_group=r.get("matched_sales_group"),
                matched_uom=float(r["matched_uom"]) if r.get("matched_uom") is not None else None,
                match_score=float(r["match_score"]) if r.get("match_score") is not None else None,
                status=r.get("status", "ok"),
                notes=r.get("notes"),
            )
            for r in rows
        ],
    )
