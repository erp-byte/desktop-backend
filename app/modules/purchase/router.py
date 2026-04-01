"""Purchase module router — upload, view, export, summary, detail, stores receive/boxes."""

import logging
import math
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File
from pydantic import BaseModel

from app.modules.purchase.schemas import (
    POHeaderOut,
    POSummary,
    POViewResponse,
    POExportResponse,
    POUploadResponse,
)
from app.modules.purchase.services.ingest import ingest_po_book
from app.modules.purchase.services.queries import (
    build_where_clause,
    fetch_po_details,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/purchase", tags=["Purchase"])


# ---------------------------------------------------------------------------
# Purchase Team: Upload
# ---------------------------------------------------------------------------


@router.post("/upload", response_model=POUploadResponse, status_code=201)
async def upload_po_book_endpoint(
    request: Request,
    file: UploadFile = File(...),
    entity: str = Query(..., pattern="^(cfpl|cdpl)$"),
):
    """Upload a Purchase Order Book Excel file."""
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
        result = await ingest_po_book(pool, contents, master_items, entity)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.exception("PO Book ingestion failed")
        error_msg = str(e)
        if "unique" in error_msg.lower() or "duplicate" in error_msg.lower():
            raise HTTPException(409, detail=f"Duplicate data detected. {error_msg}")
        raise HTTPException(500, detail=f"Failed to process PO Book: {error_msg}")

    return POUploadResponse(**result)


# ---------------------------------------------------------------------------
# Purchase Team: View (paginated)
# ---------------------------------------------------------------------------


@router.get("/view", response_model=POViewResponse)
async def view_pos(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    search: str = Query(None),
    entity: str = Query(None),
    sort_by: str = Query(
        "po_date",
        pattern="^(transaction_no|po_date|vendor_supplier_name|customer_party_name|gross_total|warehouse)$",
    ),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    vendor: str = Query(None),
    customer: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    status: str = Query(None),
    warehouse: str = Query(None),
    item_category: str = Query(None),
    sub_category: str = Query(None),
    item_type: str = Query(None),
):
    pool = request.app.state.db_pool

    header_opts = await pool.fetchrow(
        """
        SELECT
            COALESCE(array_agg(DISTINCT entity) FILTER (WHERE entity IS NOT NULL), '{}'::text[]) AS entities,
            COALESCE(array_agg(DISTINCT voucher_type) FILTER (WHERE voucher_type IS NOT NULL), '{}'::text[]) AS voucher_types,
            COALESCE(array_agg(DISTINCT vendor_supplier_name) FILTER (WHERE vendor_supplier_name IS NOT NULL), '{}'::text[]) AS vendors,
            COALESCE(array_agg(DISTINCT customer_party_name) FILTER (WHERE customer_party_name IS NOT NULL), '{}'::text[]) AS customers,
            COALESCE(array_agg(DISTINCT warehouse) FILTER (WHERE warehouse IS NOT NULL), '{}'::text[]) AS warehouses,
            COALESCE(array_agg(DISTINCT status) FILTER (WHERE status IS NOT NULL), '{}'::text[]) AS statuses
        FROM po_header
        """
    )
    line_opts = await pool.fetchrow(
        """
        SELECT
            COALESCE(array_agg(DISTINCT item_category) FILTER (WHERE item_category IS NOT NULL), '{}'::text[]) AS item_categories,
            COALESCE(array_agg(DISTINCT sub_category) FILTER (WHERE sub_category IS NOT NULL), '{}'::text[]) AS sub_categories,
            COALESCE(array_agg(DISTINCT item_type) FILTER (WHERE item_type IS NOT NULL), '{}'::text[]) AS item_types
        FROM po_line
        """
    )
    filter_options = {
        "entities": sorted(header_opts["entities"]),
        "voucher_types": sorted(header_opts["voucher_types"]),
        "vendors": sorted(header_opts["vendors"]),
        "customers": sorted(header_opts["customers"]),
        "warehouses": sorted(header_opts["warehouses"]),
        "statuses": sorted(header_opts["statuses"]),
        "item_categories": sorted(line_opts["item_categories"]),
        "sub_categories": sorted(line_opts["sub_categories"]),
        "item_types": sorted(line_opts["item_types"]),
    }

    where_clause, params = build_where_clause(
        search=search, entity=entity, vendor=vendor, customer=customer,
        date_from=date_from, date_to=date_to, status=status,
        warehouse=warehouse, item_category=item_category,
        sub_category=sub_category, item_type=item_type,
    )

    summary_row = await pool.fetchrow(
        f"""
        SELECT
            COUNT(*) AS total_transactions,
            COALESCE(SUM(lc.line_count), 0) AS total_lines,
            COALESCE(SUM(lc.box_count), 0) AS total_boxes,
            COALESCE(SUM(h.gross_total), 0) AS total_amount,
            COALESCE(SUM(
                COALESCE(h.sgst_amount, 0) + COALESCE(h.cgst_amount, 0) + COALESCE(h.igst_amount, 0)
            ), 0) AS total_tax,
            COALESCE(SUM(lc.po_weight_sum), 0) AS total_net_weight
        FROM po_header h
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) AS line_count,
                COALESCE(SUM(l.po_weight), 0) AS po_weight_sum,
                (SELECT COUNT(*) FROM po_box b WHERE b.transaction_no = h.transaction_no) AS box_count
            FROM po_line l
            WHERE l.transaction_no = h.transaction_no
        ) lc ON TRUE
        WHERE {where_clause}
        """,
        *params,
    )

    total = summary_row["total_transactions"]
    total_pages = max(1, math.ceil(total / page_size))

    summary = {
        "total_transactions": total,
        "total_lines": summary_row["total_lines"],
        "total_boxes": summary_row["total_boxes"],
        "total_amount": float(summary_row["total_amount"]) if summary_row["total_amount"] else None,
        "total_tax": float(summary_row["total_tax"]) if summary_row["total_tax"] else None,
        "total_net_weight": float(summary_row["total_net_weight"]) if summary_row["total_net_weight"] else None,
    }

    sort_col = {
        "transaction_no": "h.transaction_no",
        "po_date": "h.po_date",
        "vendor_supplier_name": "h.vendor_supplier_name",
        "customer_party_name": "h.customer_party_name",
        "gross_total": "h.gross_total",
        "warehouse": "h.warehouse",
    }[sort_by]

    offset = (page - 1) * page_size
    param_idx = len(params)
    param_idx += 1
    limit_param = param_idx
    param_idx += 1
    offset_param = param_idx

    paginated_headers = await pool.fetch(
        f"""
        SELECT * FROM po_header h
        WHERE {where_clause}
        ORDER BY {sort_col} {sort_order} NULLS LAST
        LIMIT ${limit_param} OFFSET ${offset_param}
        """,
        *params, page_size, offset,
    )

    txn_nos = [h["transaction_no"] for h in paginated_headers]
    all_details = await fetch_po_details(pool, txn_nos, paginated_headers)

    return POViewResponse(
        page=page, page_size=page_size, total=total, total_pages=total_pages,
        summary=summary, filter_options=filter_options, transactions=all_details,
    )


# ---------------------------------------------------------------------------
# Purchase Team: Export
# ---------------------------------------------------------------------------


@router.get("/export", response_model=POExportResponse)
async def export_pos(
    request: Request,
    search: str = Query(None),
    entity: str = Query(None),
    sort_by: str = Query(
        "po_date",
        pattern="^(transaction_no|po_date|vendor_supplier_name|customer_party_name|gross_total|warehouse)$",
    ),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    vendor: str = Query(None),
    customer: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    status: str = Query(None),
    warehouse: str = Query(None),
    item_category: str = Query(None),
    sub_category: str = Query(None),
    item_type: str = Query(None),
):
    pool = request.app.state.db_pool

    where_clause, params = build_where_clause(
        search=search, entity=entity, vendor=vendor, customer=customer,
        date_from=date_from, date_to=date_to, status=status,
        warehouse=warehouse, item_category=item_category,
        sub_category=sub_category, item_type=item_type,
    )

    sort_col = {
        "transaction_no": "h.transaction_no",
        "po_date": "h.po_date",
        "vendor_supplier_name": "h.vendor_supplier_name",
        "customer_party_name": "h.customer_party_name",
        "gross_total": "h.gross_total",
        "warehouse": "h.warehouse",
    }[sort_by]

    headers = await pool.fetch(
        f"SELECT * FROM po_header h WHERE {where_clause} ORDER BY {sort_col} {sort_order} NULLS LAST",
        *params,
    )

    txn_nos = [h["transaction_no"] for h in headers]
    all_details = await fetch_po_details(pool, txn_nos, headers)

    return POExportResponse(total=len(all_details), transactions=all_details)


# ---------------------------------------------------------------------------
# Purchase Team: Summary
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=POSummary)
async def get_summary(
    request: Request,
    entity: str = Query(None),
    search: str = Query(None),
    vendor: str = Query(None),
    customer: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    status: str = Query(None),
    warehouse: str = Query(None),
    item_category: str = Query(None),
    sub_category: str = Query(None),
    item_type: str = Query(None),
):
    pool = request.app.state.db_pool

    where_clause, params = build_where_clause(
        search=search, entity=entity, vendor=vendor, customer=customer,
        date_from=date_from, date_to=date_to, status=status,
        warehouse=warehouse, item_category=item_category,
        sub_category=sub_category, item_type=item_type,
    )

    row = await pool.fetchrow(
        f"""
        SELECT
            COUNT(*) AS total_transactions,
            COALESCE(SUM(lc.line_count), 0) AS total_lines,
            COALESCE(SUM(lc.box_count), 0) AS total_boxes,
            COALESCE(SUM(h.gross_total), 0) AS total_amount,
            COALESCE(SUM(
                COALESCE(h.sgst_amount, 0) + COALESCE(h.cgst_amount, 0) + COALESCE(h.igst_amount, 0)
            ), 0) AS total_tax,
            COALESCE(SUM(lc.po_weight_sum), 0) AS total_net_weight
        FROM po_header h
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) AS line_count,
                COALESCE(SUM(l.po_weight), 0) AS po_weight_sum,
                (SELECT COUNT(*) FROM po_box b WHERE b.transaction_no = h.transaction_no) AS box_count
            FROM po_line l
            WHERE l.transaction_no = h.transaction_no
        ) lc ON TRUE
        WHERE {where_clause}
        """,
        *params,
    )

    return POSummary(
        total_transactions=row["total_transactions"],
        total_lines=row["total_lines"],
        total_boxes=row["total_boxes"],
        total_amount=float(row["total_amount"]) if row["total_amount"] else None,
        total_tax=float(row["total_tax"]) if row["total_tax"] else None,
        total_net_weight=float(row["total_net_weight"]) if row["total_net_weight"] else None,
    )


# ---------------------------------------------------------------------------
# Single PO detail
# ---------------------------------------------------------------------------


@router.get("/{transaction_no}", response_model=POHeaderOut)
async def get_po(request: Request, transaction_no: str):
    pool = request.app.state.db_pool
    header = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    if not header:
        raise HTTPException(404, detail="Transaction not found.")

    details = await fetch_po_details(pool, [transaction_no], [header])
    return POHeaderOut(**details[0])


# ---------------------------------------------------------------------------
# Stores Team: Receive (update header logistics + line dates/weights)
# ---------------------------------------------------------------------------


class StoresHeaderUpdate(BaseModel):
    customer_party_name: str | None = None
    vehicle_number: str | None = None
    transporter_name: str | None = None
    lr_number: str | None = None
    source_location: str | None = None
    destination_location: str | None = None
    challan_number: str | None = None
    invoice_number: str | None = None
    grn_number: str | None = None
    system_grn_date: datetime | None = None
    purchased_by: str | None = None
    inward_authority: str | None = None
    warehouse: str | None = None


class StoresLineUpdate(BaseModel):
    line_number: int
    carton_weight: float | None = None


class StoresReceiveRequest(BaseModel):
    header: StoresHeaderUpdate | None = None
    lines: list[StoresLineUpdate] = []


@router.put("/{transaction_no}/receive", response_model=POHeaderOut)
async def stores_receive(request: Request, transaction_no: str, body: StoresReceiveRequest):
    """
    Stores Team: Update header logistics + line dates/weights.
    Only updates Stores-owned fields — never touches Purchase Team data.
    Fetches current state first, then applies changes additively.
    """
    pool = request.app.state.db_pool

    # Verify PO exists
    existing = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    if not existing:
        raise HTTPException(404, detail="Transaction not found.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Update header — only Stores-owned fields
            if body.header:
                h = body.header
                await conn.execute(
                    """
                    UPDATE po_header SET
                        customer_party_name = COALESCE($2, customer_party_name),
                        vehicle_number = COALESCE($3, vehicle_number),
                        transporter_name = COALESCE($4, transporter_name),
                        lr_number = COALESCE($5, lr_number),
                        source_location = COALESCE($6, source_location),
                        destination_location = COALESCE($7, destination_location),
                        challan_number = COALESCE($8, challan_number),
                        invoice_number = COALESCE($9, invoice_number),
                        grn_number = COALESCE($10, grn_number),
                        system_grn_date = COALESCE($11, system_grn_date),
                        purchased_by = COALESCE($12, purchased_by),
                        inward_authority = COALESCE($13, inward_authority),
                        warehouse = COALESCE($14, warehouse)
                    WHERE transaction_no = $1
                    """,
                    transaction_no,
                    h.customer_party_name,
                    h.vehicle_number,
                    h.transporter_name,
                    h.lr_number,
                    h.source_location,
                    h.destination_location,
                    h.challan_number,
                    h.invoice_number,
                    h.grn_number,
                    h.system_grn_date,
                    h.purchased_by,
                    h.inward_authority,
                    h.warehouse,
                )

            # Update lines — only Stores-owned fields
            for line in body.lines:
                await conn.execute(
                    """
                    UPDATE po_line SET
                        carton_weight = COALESCE($3, carton_weight)
                    WHERE transaction_no = $1 AND line_number = $2
                    """,
                    transaction_no,
                    line.line_number,
                    line.carton_weight,
                )

    # Return full updated PO
    updated = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    details = await fetch_po_details(pool, [transaction_no], [updated])
    return POHeaderOut(**details[0])


# ---------------------------------------------------------------------------
# Stores Team: Update existing sections & boxes
# ---------------------------------------------------------------------------


class BoxUpdate(BaseModel):
    box_id: str
    box_number: int | None = None
    net_weight: float | None = None
    gross_weight: float | None = None
    lot_number: str | None = None
    count: int | None = None


class SectionUpdate(BaseModel):
    line_number: int
    section_number: int
    box_count: int | None = None
    lot_number: str | None = None
    manufacturing_date: str | None = None
    expiry_date: str | None = None
    boxes: list[BoxUpdate] = []


class UpdateSectionsRequest(BaseModel):
    sections: list[SectionUpdate]


@router.put("/{transaction_no}/boxes", response_model=POHeaderOut)
async def stores_update_boxes(request: Request, transaction_no: str, body: UpdateSectionsRequest):
    """
    Stores Team: Update existing sections and boxes.
    Uses COALESCE — only overwrites fields you provide, leaves others untouched.
    """
    pool = request.app.state.db_pool

    existing = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    if not existing:
        raise HTTPException(404, detail="Transaction not found.")

    if not body.sections:
        raise HTTPException(400, detail="No sections provided.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            for section in body.sections:
                # Update section fields
                await conn.execute(
                    """
                    UPDATE po_section SET
                        lot_number = COALESCE($4, lot_number),
                        box_count = COALESCE($5, box_count),
                        manufacturing_date = COALESCE($6, manufacturing_date),
                        expiry_date = COALESCE($7, expiry_date)
                    WHERE transaction_no = $1
                      AND line_number = $2
                      AND section_number = $3
                    """,
                    transaction_no,
                    section.line_number,
                    section.section_number,
                    section.lot_number,
                    section.box_count,
                    section.manufacturing_date,
                    section.expiry_date,
                )

                # Update boxes within this section
                for b in section.boxes:
                    await conn.execute(
                        """
                        UPDATE po_box SET
                            box_number = COALESCE($2, box_number),
                            net_weight = COALESCE($3, net_weight),
                            gross_weight = COALESCE($4, gross_weight),
                            lot_number = COALESCE($5, lot_number),
                            count = COALESCE($6, count)
                        WHERE box_id = $1
                        """,
                        b.box_id,
                        b.box_number,
                        b.net_weight,
                        b.gross_weight,
                        b.lot_number,
                        b.count,
                    )

    updated = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    details = await fetch_po_details(pool, [transaction_no], [updated])
    return POHeaderOut(**details[0])


# ---------------------------------------------------------------------------
# Stores Team: Add new sections & boxes (append only)
# ---------------------------------------------------------------------------


class BoxInput(BaseModel):
    box_id: str
    box_number: int
    net_weight: float | None = None
    gross_weight: float | None = None
    lot_number: str | None = None
    count: int | None = None


class SectionInput(BaseModel):
    line_number: int
    box_count: int | None = None
    lot_number: str | None = None
    manufacturing_date: str | None = None
    expiry_date: str | None = None
    boxes: list[BoxInput] = []


class AddSectionsRequest(BaseModel):
    sections: list[SectionInput]


@router.post("/{transaction_no}/boxes", response_model=POHeaderOut, status_code=201)
async def stores_add_boxes(request: Request, transaction_no: str, body: AddSectionsRequest):
    """
    Stores Team: Add sections with boxes for a transaction.
    Append only — never deletes existing sections/boxes.
    Each section represents a lot grouping for an article line.
    """
    pool = request.app.state.db_pool

    existing = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    if not existing:
        raise HTTPException(404, detail="Transaction not found.")

    if not body.sections:
        raise HTTPException(400, detail="No sections provided.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            for section in body.sections:
                # Determine next section_number for this line
                max_sn = await conn.fetchval(
                    """
                    SELECT COALESCE(MAX(section_number), 0)
                    FROM po_section
                    WHERE transaction_no = $1 AND line_number = $2
                    """,
                    transaction_no,
                    section.line_number,
                )
                section_number = max_sn + 1

                # Insert section
                await conn.execute(
                    """
                    INSERT INTO po_section (
                        transaction_no, line_number, section_number,
                        lot_number, box_count, manufacturing_date, expiry_date
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    transaction_no,
                    section.line_number,
                    section_number,
                    section.lot_number,
                    section.box_count,
                    section.manufacturing_date,
                    section.expiry_date,
                )

                # Insert boxes for this section
                for b in section.boxes:
                    await conn.execute(
                        """
                        INSERT INTO po_box (
                            box_id, transaction_no, line_number, section_number,
                            box_number, net_weight, gross_weight, lot_number, count
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        b.box_id,
                        transaction_no,
                        section.line_number,
                        section_number,
                        b.box_number,
                        b.net_weight,
                        b.gross_weight,
                        b.lot_number,
                        b.count,
                    )

    updated = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    details = await fetch_po_details(pool, [transaction_no], [updated])
    return POHeaderOut(**details[0])
