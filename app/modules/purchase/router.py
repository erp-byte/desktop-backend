"""Purchase module router — upload, view, export, summary, detail, stores receive/boxes."""

import logging
import math
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from pydantic import BaseModel, Field

from app.modules.auth.middleware import require_permission

from app.modules.purchase.schemas import (
    POHeaderOut,
    POSummary,
    POViewResponse,
    POExportResponse,
    POUploadResponse,
)
from app.modules.purchase.schemas.box import BoxOut
from app.modules.purchase.services.ingest import ingest_po_book
from app.modules.purchase.services.po_site_scope import address_hidden, hidden_sites_for
from app.modules.purchase.services.receipt_match import LINE_RECEIPT_SQL, line_match, receiving_status
from app.modules.purchase.services.queries import (
    build_where_clause,
    fetch_po_details,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/purchase", tags=["Purchase"])


def _coerce_date(v):
    """Coerce a date / datetime / ISO 'YYYY-MM-DD' string / None to a date.

    po_section stores manufacturing_date / expiry_date as TEXT, but
    inventory_batch types them DATE — so the raw strings the UI sends must be
    parsed here before binding as asyncpg DATE params (else: DataError
    'str object has no attribute toordinal'). Returns None on blank/unparseable.
    """
    if v is None or v == "":
        return None
    if isinstance(v, datetime):  # datetime is a subclass of date — check first
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


async def _refresh_receiving_status(conn, transaction_no: str) -> None:
    """Set po_header.status from what's been received via boxes. Called on every
    box change so the Material In dashboard reflects receiving immediately:
    'pending', 'partially received', 'fully received' (every line matched) or
    'extra received' -- the rule is services/receipt_match.py, shared with the
    receipt summary's per-line ✓."""
    lines = [line_match(r) for r in await conn.fetch(LINE_RECEIPT_SQL, transaction_no)]
    status = receiving_status(lines)
    await conn.execute(
        "UPDATE po_header SET status = $2 WHERE transaction_no = $1",
        transaction_no, status,
    )


# ---------------------------------------------------------------------------
# Purchase Team: Upload
# ---------------------------------------------------------------------------


@router.post("/upload", response_model=POUploadResponse, status_code=201)
async def upload_po_book_endpoint(
    request: Request,
    file: UploadFile = File(...),
    entity: str = Query(..., pattern="^(cfpl|cdpl)$"),
    user=Depends(require_permission("purchase", "po", action="create")),
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
        pattern="^(transaction_no|po_date|vendor_supplier_name|consignee_address|customer_party_name|gross_total|warehouse)$",
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
    user=Depends(require_permission("purchase", "po", action="read")),
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
        hidden_sites=await _hidden_sites(pool, user),
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
        "consignee_address": "h.consignee_address",
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
        pattern="^(transaction_no|po_date|vendor_supplier_name|consignee_address|customer_party_name|gross_total|warehouse)$",
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
    user=Depends(require_permission("purchase", "po", action="read")),
):
    pool = request.app.state.db_pool

    where_clause, params = build_where_clause(
        search=search, entity=entity, vendor=vendor, customer=customer,
        date_from=date_from, date_to=date_to, status=status,
        warehouse=warehouse, item_category=item_category,
        sub_category=sub_category, item_type=item_type,
        hidden_sites=await _hidden_sites(pool, user),
    )

    sort_col = {
        "transaction_no": "h.transaction_no",
        "po_date": "h.po_date",
        "vendor_supplier_name": "h.vendor_supplier_name",
        "consignee_address": "h.consignee_address",
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
    user=Depends(require_permission("purchase", "po", action="read")),
):
    pool = request.app.state.db_pool

    where_clause, params = build_where_clause(
        search=search, entity=entity, vendor=vendor, customer=customer,
        date_from=date_from, date_to=date_to, status=status,
        warehouse=warehouse, item_category=item_category,
        sub_category=sub_category, item_type=item_type,
        hidden_sites=await _hidden_sites(pool, user),
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


async def _hidden_sites(pool, user) -> list[str]:
    """Site keys whose POs `user` may not see (po_site_scope)."""
    return await hidden_sites_for(pool, user)


async def _assert_site_visible(pool, user, transaction_no: str, header=None) -> None:
    """404 for a PO addressed to a warehouse outside the user's access
    (po_site_scope), as for a missing one. `header` is the po_header row when
    the caller already has it."""
    hidden = await _hidden_sites(pool, user)
    if not hidden:
        return
    address = header["consignee_address"] if header is not None else await pool.fetchval(
        "SELECT consignee_address FROM po_header WHERE transaction_no = $1", transaction_no)
    if address_hidden(address, hidden):
        raise HTTPException(404, detail="Transaction not found.")


@router.get("/{transaction_no}", response_model=POHeaderOut)
async def get_po(
    request: Request,
    transaction_no: str,
    include_boxes: bool = Query(
        True,
        description="When false, sections omit box rows (counts only); boxes load lazily via GET /{txn}/boxes.",
    ),
    user=Depends(require_permission("purchase", "po", action="read")),
):
    pool = request.app.state.db_pool
    header = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    if not header:
        raise HTTPException(404, detail="Transaction not found.")
    await _assert_site_visible(pool, user, transaction_no, header)

    details = await fetch_po_details(pool, [transaction_no], [header], include_boxes=include_boxes)
    return POHeaderOut(**details[0])


# ---------------------------------------------------------------------------
# Stores Team: Section boxes — lazy paginated load + print (all / range)
# ---------------------------------------------------------------------------


class BoxListResponse(BaseModel):
    boxes: list[BoxOut]
    total: int
    page: int
    page_size: int


class BoxPrintResponse(BaseModel):
    boxes: list[BoxOut]
    total: int


def _box_row_to_out(b) -> dict:
    return {
        "box_id": b["box_id"],
        "transaction_no": b["transaction_no"],
        "line_number": b["line_number"],
        "section_number": b["section_number"],
        "box_number": b["box_number"],
        "net_weight": float(b["net_weight"]) if b["net_weight"] is not None else None,
        "gross_weight": float(b["gross_weight"]) if b["gross_weight"] is not None else None,
        "lot_number": b["lot_number"],
        "count": int(b["count"]) if b["count"] is not None else None,
    }


@router.get("/{transaction_no}/boxes", response_model=BoxListResponse)
async def list_section_boxes(
    request: Request,
    transaction_no: str,
    line_number: int = Query(..., description="PO line the section belongs to"),
    section_number: int = Query(..., description="Section within the line"),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=1000),
    user=Depends(require_permission("purchase", "material_in", action="view")),
):
    """Paginated boxes for ONE section — loaded lazily when a section is expanded."""
    pool = request.app.state.db_pool
    await _assert_site_visible(pool, user, transaction_no)
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM po_box "
        "WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3",
        transaction_no, line_number, section_number,
    )
    offset = (page - 1) * page_size
    rows = await pool.fetch(
        "SELECT * FROM po_box "
        "WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3 "
        "ORDER BY box_number LIMIT $4 OFFSET $5",
        transaction_no, line_number, section_number, page_size, offset,
    )
    return BoxListResponse(
        boxes=[_box_row_to_out(b) for b in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.get("/{transaction_no}/boxes/print", response_model=BoxPrintResponse)
async def list_section_boxes_for_print(
    request: Request,
    transaction_no: str,
    line_number: int = Query(...),
    section_number: int = Query(...),
    from_box: int | None = Query(None, description="Lowest box_number to include (inclusive)"),
    to_box: int | None = Query(None, description="Highest box_number to include (inclusive)"),
    limit: int = Query(20000, ge=1, le=50000),
    user=Depends(require_permission("purchase", "material_in", action="view")),
):
    """All boxes for a section, or a box_number range — for label printing (no pagination)."""
    pool = request.app.state.db_pool
    await _assert_site_visible(pool, user, transaction_no)
    clauses = ["transaction_no = $1", "line_number = $2", "section_number = $3"]
    params: list = [transaction_no, line_number, section_number]
    if from_box is not None:
        params.append(from_box)
        clauses.append(f"box_number >= ${len(params)}")
    if to_box is not None:
        params.append(to_box)
        clauses.append(f"box_number <= ${len(params)}")
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM po_box WHERE {' AND '.join(clauses)} ORDER BY box_number LIMIT ${len(params)}",
        *params,
    )
    return BoxPrintResponse(boxes=[_box_row_to_out(b) for b in rows], total=len(rows))


# ---------------------------------------------------------------------------
# Stores Team: Receive (update header logistics + line dates/weights)
# ---------------------------------------------------------------------------


class StoresHeaderUpdate(BaseModel):
    customer_party_name: str | None = None
    vehicle_number: str | None = None
    transporter_name: str | None = None
    lr_number: str | None = None
    source_location: str | None = None
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
async def stores_receive(request: Request, transaction_no: str, body: StoresReceiveRequest,
                         user=Depends(require_permission("purchase", "material_in", action="edit"))):
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
    await _assert_site_visible(pool, user, transaction_no, existing)

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
                        challan_number = COALESCE($7, challan_number),
                        invoice_number = COALESCE($8, invoice_number),
                        grn_number = COALESCE($9, grn_number),
                        system_grn_date = COALESCE($10, system_grn_date),
                        purchased_by = COALESCE($11, purchased_by),
                        inward_authority = COALESCE($12, inward_authority),
                        warehouse = COALESCE($13, warehouse)
                    WHERE transaction_no = $1
                    """,
                    transaction_no,
                    h.customer_party_name,
                    h.vehicle_number,
                    h.transporter_name,
                    h.lr_number,
                    h.source_location,
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
# Inventory batches for received boxes
# ---------------------------------------------------------------------------

# One inventory batch per weighed box — the box's net weight becomes stock.
# original_qty_kg is NOT NULL, so a box saved before it is weighed gets its
# batch later, when PUT /boxes records the weight.
_INWARD_BATCH_SQL = """
    INSERT INTO inventory_batch
        (batch_id, sku_name, item_type, transaction_no, lot_number, source,
         inward_date, manufacturing_date, expiry_date,
         original_qty_kg, current_qty_kg, warehouse_id, floor_id, entity)
    VALUES ($1, $2, 'rm', $3, $4, 'INWARD',
            COALESCE($5, CURRENT_DATE), $6, $7,
            $8, $8, $9, 'rm_store', $10)
    ON CONFLICT (batch_id) DO NOTHING
"""


def _is_weighed(net_weight) -> bool:
    return net_weight is not None and net_weight > 0


async def _book_missing_inward_batches(conn, header, transaction_no: str, box_ids: list[str]) -> None:
    """Create the batch for boxes of this PO that now carry a net weight but
    have no batch yet (they were saved before weighing). Boxes that already
    have a batch are left as they are."""
    if not box_ids:
        return
    rows = await conn.fetch(
        """
        SELECT b.box_id, b.lot_number, b.net_weight, l.sku_name,
               s.manufacturing_date, s.expiry_date
          FROM po_box b
          LEFT JOIN po_line l
                 ON l.transaction_no = b.transaction_no AND l.line_number = b.line_number
          LEFT JOIN po_section s
                 ON s.transaction_no = b.transaction_no AND s.line_number = b.line_number
                AND s.section_number = b.section_number
         WHERE b.transaction_no = $1
           AND b.box_id = ANY($2::text[])
           AND b.net_weight > 0
           AND NOT EXISTS (SELECT 1 FROM inventory_batch ib WHERE ib.batch_id = b.box_id)
        """,
        transaction_no,
        box_ids,
    )
    if not rows:
        return
    inward_date = _coerce_date(header.get('po_date', None))
    await conn.executemany(_INWARD_BATCH_SQL, [
        (
            r['box_id'], r['sku_name'] or 'UNKNOWN', transaction_no, r['lot_number'],
            inward_date, _coerce_date(r['manufacturing_date']), _coerce_date(r['expiry_date']),
            r['net_weight'], header.get('warehouse', None), header.get('entity', 'cfpl'),
        )
        for r in rows
    ])


# Batches in these states are used up: the stock is gone, so a later edit of
# the box must not rewrite them.
_USED_UP = ("ISSUED", "SCRAPPED", "DISCARDED")


async def _lock_box_batches(conn, transaction_no: str, box_ids: list[str]) -> dict[str, dict]:
    """The edited boxes that have a batch, with that batch, as they stand before
    the edit: box_id -> row. Both rows stay locked until commit, so a
    consumption of the batch can't slip in between reading and correcting it."""
    if not box_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT b.box_id, b.line_number, b.box_number, b.net_weight, b.lot_number,
               ib.status, ib.original_qty_kg, ib.current_qty_kg
          FROM po_box b
          JOIN inventory_batch ib ON ib.batch_id = b.box_id
         WHERE b.transaction_no = $1
           AND b.box_id = ANY($2::text[])
           FOR UPDATE OF b, ib
        """,
        transaction_no,
        box_ids,
    )
    return {r['box_id']: dict(r) for r in rows}


async def _sync_box_corrections(conn, transaction_no: str, before: dict[str, dict], actor: str | None) -> None:
    """Carry corrected box weights and lots into the boxes' batches.

    A weight correction keeps what has already been drawn from the batch: its
    original becomes the new weight and its current moves by the same amount
    (10 kg corrected to 9.5 kg with 2 kg issued leaves 7.5 kg). A used-up batch,
    or one with more drawn than the new weight, is refused with 409 — that needs
    a stock adjustment, not a Material In edit. Each weight correction is logged
    as an ADJUSTED inventory event."""
    if not before:
        return
    after = await conn.fetch(
        "SELECT box_id, net_weight, lot_number FROM po_box "
        "WHERE transaction_no = $1 AND box_id = ANY($2::text[])",
        transaction_no,
        list(before),
    )
    refused, weights, events, lots = [], [], [], []
    for new in after:
        old = before[new['box_id']]
        used_up = old['status'] in _USED_UP
        if new['net_weight'] is not None and new['net_weight'] != old['net_weight']:
            current = new['net_weight'] - (old['original_qty_kg'] - old['current_qty_kg'])
            if used_up or current < 0:
                refused.append(f"line {old['line_number']} box {old['box_number']}")
                continue
            weights.append((new['box_id'], new['net_weight'], current))
            events.append((
                new['box_id'], old['status'], current - old['current_qty_kg'], actor,
                f"Material In {transaction_no}: box weight corrected "
                f"{old['net_weight']} -> {new['net_weight']} kg",
            ))
        if new['lot_number'] != old['lot_number'] and not used_up:
            lots.append((new['box_id'], new['lot_number']))
    if refused:
        raise HTTPException(409, detail={
            "error": "box_stock_used",
            "message": (
                f"Can't change the weight of {', '.join(refused)}: the stock is used up, or more has "
                "already been issued than the new weight. Undo that change and ask the inventory "
                "team to adjust the stock."
            ),
            "boxes": refused,
        })
    if weights:
        await conn.executemany(
            "UPDATE inventory_batch SET original_qty_kg = $2, current_qty_kg = $3, updated_at = NOW() "
            "WHERE batch_id = $1",
            weights,
        )
        await conn.executemany(
            """
            INSERT INTO inventory_event_log
                (batch_id, event_type, from_status, to_status, quantity_kg,
                 reference_type, performed_by, notes)
            VALUES ($1, 'ADJUSTED', $2, $2, $3, 'material_in', $4, $5)
            """,
            events,
        )
    if lots:
        await conn.executemany(
            "UPDATE inventory_batch SET lot_number = $2, updated_at = NOW() WHERE batch_id = $1",
            lots,
        )


async def _sync_section_dates(conn, transaction_no: str, line_number: int, section_number: int) -> None:
    """Carry the section's manufacturing / expiry dates into the batches of all
    its boxes still in stock — not only the boxes loaded on screen."""
    sec = await conn.fetchrow(
        "SELECT manufacturing_date, expiry_date FROM po_section "
        "WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3",
        transaction_no, line_number, section_number,
    )
    if sec is None:
        return
    mfg, exp = _coerce_date(sec['manufacturing_date']), _coerce_date(sec['expiry_date'])
    if mfg is None and exp is None:
        return
    await conn.execute(
        """
        UPDATE inventory_batch ib
           SET manufacturing_date = COALESCE($4::date, ib.manufacturing_date),
               expiry_date        = COALESCE($5::date, ib.expiry_date),
               updated_at         = NOW()
          FROM po_box b
         WHERE b.transaction_no = $1 AND b.line_number = $2 AND b.section_number = $3
           AND ib.batch_id = b.box_id
           AND ib.status <> ALL($6::text[])
           AND (ib.manufacturing_date IS DISTINCT FROM COALESCE($4::date, ib.manufacturing_date)
                OR ib.expiry_date IS DISTINCT FROM COALESCE($5::date, ib.expiry_date))
        """,
        transaction_no, line_number, section_number, mfg, exp, list(_USED_UP),
    )


# po_box_request_key (migration 128) has been seen. Until then the table is
# looked for on each save, so the check starts working once the migration runs.
_request_keys_ready = False


async def _claim_request_key(conn, transaction_no: str, client_key: str | None) -> bool:
    """Record the browser's key for one section payload of POST /boxes.

    False when an earlier attempt already applied that payload — a retry after
    its reply was lost — so the caller skips it instead of adding the boxes
    twice. True when there is no key, or no key table yet."""
    global _request_keys_ready
    if not client_key:
        return True
    if not _request_keys_ready:
        if not await conn.fetchval("SELECT to_regclass('public.po_box_request_key')"):
            return True
        _request_keys_ready = True
    claimed = await conn.fetchval(
        "INSERT INTO po_box_request_key (transaction_no, client_key) VALUES ($1, $2) "
        "ON CONFLICT DO NOTHING RETURNING 1",
        transaction_no, client_key,
    )
    return claimed is not None


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
async def stores_update_boxes(request: Request, transaction_no: str, body: UpdateSectionsRequest,
                              user=Depends(require_permission("purchase", "material_in", action="edit"))):
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
    await _assert_site_visible(pool, user, transaction_no, existing)

    if not body.sections:
        raise HTTPException(400, detail="No sections provided.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            box_ids = [b.box_id for s in body.sections for b in s.boxes]
            # The edited boxes' stock as it stands before this edit (locked).
            before = await _lock_box_batches(conn, transaction_no, box_ids)
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

                # Update boxes within this section — one batched round-trip.
                # Only this PO's boxes: a box_id from another PO is left alone.
                if section.boxes:
                    await conn.executemany(
                        """
                        UPDATE po_box SET
                            box_number = COALESCE($2, box_number),
                            net_weight = COALESCE($3, net_weight),
                            gross_weight = COALESCE($4, gross_weight),
                            lot_number = COALESCE($5, lot_number),
                            count = COALESCE($6, count)
                        WHERE box_id = $1 AND transaction_no = $7
                        """,
                        [
                            (b.box_id, b.box_number, b.net_weight, b.gross_weight, b.lot_number, b.count,
                             transaction_no)
                            for b in section.boxes
                        ],
                    )
                if section.manufacturing_date is not None or section.expiry_date is not None:
                    await _sync_section_dates(conn, transaction_no, section.line_number, section.section_number)
            # Corrected weights and lots reach the boxes' stock; boxes saved
            # before weighing get their stock batch once weighed.
            await _sync_box_corrections(conn, transaction_no, before, getattr(user, "full_name", None))
            await _book_missing_inward_batches(conn, existing, transaction_no, box_ids)
            await _refresh_receiving_status(conn, transaction_no)

    updated = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    details = await fetch_po_details(pool, [transaction_no], [updated])
    return POHeaderOut(**details[0])


# ---------------------------------------------------------------------------
# Box creation helpers (POST /boxes for whole sections, POST /box for one box)
# ---------------------------------------------------------------------------

# Held until the transaction commits: section numbers, box numbers and box ids
# are each "the next free one", so two saves on one PO must not interleave.
_BOX_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('po_box:' || $1))"

# Migration 131 (po_section / po_box client_key) and box_edit_logs have been
# seen. Until then they are looked for on each save, so the key lookups and the
# deleted-id guard start working once they exist.
_client_keys_ready = False
_box_log_ready = False


async def _client_keys(conn) -> bool:
    """Whether po_section and po_box carry client_key (migration 131)."""
    global _client_keys_ready
    if not _client_keys_ready:
        n = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'client_key' "
            "AND table_name IN ('po_section', 'po_box')"
        )
        _client_keys_ready = n == 2
    return _client_keys_ready


async def _box_log(conn) -> bool:
    """Whether box_edit_logs (the global box audit, migration 070) exists."""
    global _box_log_ready
    if not _box_log_ready:
        _box_log_ready = bool(await conn.fetchval("SELECT to_regclass('public.box_edit_logs')"))
    return _box_log_ready


async def _saved_box_keys(conn, transaction_no: str, keys: list[str | None]) -> set[str]:
    """The given box keys already saved on this PO."""
    keys = [k for k in keys if k]
    if not keys:
        return set()
    rows = await conn.fetch(
        "SELECT client_key FROM po_box WHERE transaction_no = $1 AND client_key = ANY($2::text[])",
        transaction_no, keys,
    )
    return {r["client_key"] for r in rows}


def _base_of(bid):
    return bid.rsplit("-", 1)[0] if bid and "-" in bid else (bid or None)


async def _next_box_ids(conn, transaction_no: str, proposed: str | None) -> tuple[str, int]:
    """(base, next counter) for minting box ids '{base}-{n}'.

    box_id is a global TEXT PRIMARY KEY. Minting it on the client as
    {Date.now()-last8}-{per-batch counter} restarts the counter every batch (so
    appended boxes aren't sequential) and can collide on the PK. So: reuse the
    transaction's existing 8-digit base (the client's proposed id seeds it for
    a PO's first boxes) and take the next counter GLOBALLY for that base --
    past the ids of deleted boxes too (box_edit_logs), because a deleted box's
    printed label and stock batch still carry its id. Call under _BOX_LOCK_SQL."""
    logged = await _box_log(conn)
    latest = await conn.fetchval(
        "SELECT box_id FROM po_box WHERE transaction_no = $1 ORDER BY created_at DESC, box_id DESC LIMIT 1",
        transaction_no,
    )
    if not latest and logged:
        latest = await conn.fetchval(
            "SELECT box_id FROM box_edit_logs WHERE transaction_no = $1 AND field_name = 'deleted' "
            "ORDER BY edited_at DESC NULLS LAST LIMIT 1",
            transaction_no,
        )
    base = _base_of(latest) or _base_of(proposed) or "10000000"
    # A deleted weighed box keeps its (discarded) batch under its id, so the
    # PO's batches guard those ids even where the audit row is missing.
    deleted_ids = (
        "UNION ALL SELECT box_id FROM box_edit_logs WHERE field_name = 'deleted' AND box_id LIKE $1 || '-%'"
        if logged else ""
    )
    max_seq = await conn.fetchval(
        f"""
        SELECT COALESCE(MAX(CAST(split_part(id, '-', 2) AS INTEGER)), 0)
          FROM (SELECT box_id AS id FROM po_box WHERE box_id LIKE $1 || '-%'
                UNION ALL
                SELECT batch_id FROM inventory_batch WHERE transaction_no = $2 AND batch_id LIKE $1 || '-%'
                {deleted_ids}) ids
         WHERE split_part(id, '-', 2) ~ '^[0-9]+$'
        """,
        base, transaction_no,
    )
    return base, (max_seq or 0) + 1


async def _taken_box_numbers(conn, transaction_no: str, line_number: int, section_number: int) -> set[int]:
    rows = await conn.fetch(
        "SELECT box_number FROM po_box WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3",
        transaction_no, line_number, section_number,
    )
    return {r["box_number"] for r in rows if r["box_number"] is not None}


def _pick_box_number(requested: int | None, taken: set[int]) -> int:
    """The number the screen shows when it is free in the section, so a box
    saved on its own keeps its number; else the one after the highest (the
    client's guess is stale in lazy/paginated mode). Marks it taken."""
    n = requested if requested and requested > 0 and requested not in taken else max(taken, default=0) + 1
    taken.add(n)
    return n


async def _insert_section(conn, transaction_no: str, line_number: int, section_number: int,
                          lot_number, box_count, mfg, exp, client_key: str | None) -> None:
    if client_key:
        await conn.execute(
            """
            INSERT INTO po_section (transaction_no, line_number, section_number,
                                    lot_number, box_count, manufacturing_date, expiry_date, client_key)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            transaction_no, line_number, section_number, lot_number, box_count, mfg, exp, client_key,
        )
    else:
        await conn.execute(
            """
            INSERT INTO po_section (transaction_no, line_number, section_number,
                                    lot_number, box_count, manufacturing_date, expiry_date)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            transaction_no, line_number, section_number, lot_number, box_count, mfg, exp,
        )


async def _insert_boxes(conn, header, transaction_no: str, line_number: int, section_number: int,
                        mfg, exp, rows: list[dict], keys: bool) -> None:
    """Insert the boxes and one inventory batch per weighed box -- the net weight
    becomes stock. A box saved before it is weighed gets its batch when PUT
    /boxes (or PUT /box) records the weight. Two batched round-trips."""
    if not rows:
        return
    sku = await conn.fetchval(
        "SELECT sku_name FROM po_line WHERE transaction_no = $1 AND line_number = $2",
        transaction_no, line_number,
    ) or "UNKNOWN"
    cols = "box_id, transaction_no, line_number, section_number, box_number, net_weight, gross_weight, lot_number, count"
    if keys:
        cols += ", client_key"
    marks = ", ".join(f"${i}" for i in range(1, cols.count(",") + 2))
    await conn.executemany(
        f"INSERT INTO po_box ({cols}) VALUES ({marks})",
        [
            (r["box_id"], transaction_no, line_number, section_number, r["box_number"],
             r["net_weight"], r["gross_weight"], r["lot_number"], r["count"])
            + ((r["client_key"],) if keys else ())
            for r in rows
        ],
    )
    weighed = [r for r in rows if _is_weighed(r["net_weight"])]
    if weighed:
        inward_date = _coerce_date(header.get("po_date", None))
        await conn.executemany(_INWARD_BATCH_SQL, [
            (r["box_id"], sku, transaction_no, r["lot_number"], inward_date,
             _coerce_date(mfg), _coerce_date(exp), r["net_weight"],
             header.get("warehouse", None), header.get("entity", "cfpl"))
            for r in weighed
        ])


def _actor(user) -> str:
    return getattr(user, "email", None) or getattr(user, "phone", None) or getattr(user, "full_name", None) or "unknown"


async def _log_box(conn, user, transaction_no: str, box_id: str, field: str, old, new, description: str) -> None:
    """One row in box_edit_logs, the global box audit. Best effort, in its own
    savepoint: a failed audit insert must not abort -- and so silently roll
    back -- the box change it records."""
    if not await _box_log(conn):
        return
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO box_edit_logs (email_id, description, transaction_no, box_id,
                                           field_name, old_value, new_value, edited_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                """,
                _actor(user), description, transaction_no, box_id, field,
                None if old is None else str(old), None if new is None else str(new),
            )
    except Exception as e:  # noqa: BLE001 -- audit is best effort
        logger.warning("box_edit_logs.write_failed box_id=%s field=%s err=%r", box_id, field, e)


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
    # The browser's key for this box. A box already saved under it (by a
    # single-box save, POST /box) is skipped instead of added again.
    client_key: str | None = Field(default=None, max_length=80)


class SectionInput(BaseModel):
    line_number: int
    # When provided, boxes are APPENDED to this existing section (the per-section
    # "+ Add Boxes" panel). When omitted, a new section is created (max+1).
    section_number: int | None = None
    box_count: int | None = None
    lot_number: str | None = None
    manufacturing_date: str | None = None
    expiry_date: str | None = None
    # The browser's random key for this payload. A payload whose key was already
    # applied (a retry after a lost reply) is skipped — see _claim_request_key.
    client_key: str | None = Field(default=None, max_length=64)
    boxes: list[BoxInput] = []


class AddSectionsRequest(BaseModel):
    sections: list[SectionInput]


@router.post("/{transaction_no}/boxes", response_model=POHeaderOut, status_code=201)
async def stores_add_boxes(request: Request, transaction_no: str, body: AddSectionsRequest,
                           user=Depends(require_permission("purchase", "material_in", action="create"))):
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
    await _assert_site_visible(pool, user, transaction_no, existing)

    if not body.sections:
        raise HTTPException(400, detail="No sections provided.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_BOX_LOCK_SQL, transaction_no)
            keys = await _client_keys(conn)
            # Server-side box ids (see _next_box_ids); the client's proposed id
            # only seeds the base for a PO's first boxes.
            proposed = next((s.boxes[0].box_id for s in body.sections if s.boxes), None)
            box_base, next_box_seq = await _next_box_ids(conn, transaction_no, proposed)

            for section in body.sections:
                # The same payload again (a retry after its reply was lost):
                # its boxes are already saved, so don't add them twice.
                if not await _claim_request_key(conn, transaction_no, section.client_key):
                    continue
                section_number = section.section_number
                boxes = section.boxes
                if keys:
                    if section_number is None and section.client_key:
                        # A draft section a single-box save already made: append to it.
                        section_number = await conn.fetchval(
                            "SELECT section_number FROM po_section "
                            "WHERE transaction_no = $1 AND line_number = $2 AND client_key = $3",
                            transaction_no, section.line_number, section.client_key,
                        )
                    # Boxes a single-box save already made are saved: skip them.
                    saved = await _saved_box_keys(conn, transaction_no, [b.client_key for b in boxes])
                    boxes = [b for b in boxes if not b.client_key or b.client_key not in saved]
                if section_number is not None:
                    # Append boxes to an EXISTING section (the per-section
                    # "+ Add Boxes" panel). Verify it exists, then grow its
                    # box_count by the number of appended boxes.
                    sec_exists = await conn.fetchval(
                        """
                        SELECT 1 FROM po_section
                        WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3
                        """,
                        transaction_no,
                        section.line_number,
                        section_number,
                    )
                    if not sec_exists:
                        raise HTTPException(
                            404,
                            detail=f"Section {section_number} not found for line {section.line_number}.",
                        )
                    await conn.execute(
                        """
                        UPDATE po_section SET
                            box_count = COALESCE(box_count, 0) + $4,
                            lot_number = COALESCE($5, lot_number),
                            manufacturing_date = COALESCE($6, manufacturing_date),
                            expiry_date = COALESCE($7, expiry_date)
                        WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3
                        """,
                        transaction_no,
                        section.line_number,
                        section_number,
                        len(boxes),
                        section.lot_number,
                        section.manufacturing_date,
                        section.expiry_date,
                    )
                else:
                    # Determine next section_number for this line (new section)
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

                    await _insert_section(
                        conn, transaction_no, section.line_number, section_number,
                        section.lot_number, section.box_count,
                        section.manufacturing_date, section.expiry_date,
                        section.client_key if keys else None,
                    )

                # The boxes, each with a server-minted box_id and the number the
                # screen shows when it is free in the section (_pick_box_number).
                taken = await _taken_box_numbers(conn, transaction_no, section.line_number, section_number)
                rows = []
                for b in boxes:
                    rows.append({
                        "box_id": f"{box_base}-{next_box_seq}",
                        "box_number": _pick_box_number(b.box_number, taken),
                        "net_weight": b.net_weight, "gross_weight": b.gross_weight,
                        "lot_number": b.lot_number, "count": b.count,
                        "client_key": b.client_key if keys else None,
                    })
                    next_box_seq += 1
                await _insert_boxes(
                    conn, existing, transaction_no, section.line_number, section_number,
                    section.manufacturing_date, section.expiry_date, rows, keys,
                )
            await _refresh_receiving_status(conn, transaction_no)

    updated = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    details = await fetch_po_details(pool, [transaction_no], [updated])
    return POHeaderOut(**details[0])


# ---------------------------------------------------------------------------
# Stores Team: one box at a time (Material In per-box print, Update, Delete)
# ---------------------------------------------------------------------------


class OneBoxSection(BaseModel):
    """The draft section's fields, used when this box's save creates the section."""
    lot_number: str | None = None
    manufacturing_date: str | None = None
    expiry_date: str | None = None


class OneBoxCreate(BaseModel):
    line_number: int
    # The saved section the box goes into. Omitted for a draft section not saved
    # yet: section_key then finds the section an earlier box of that draft made,
    # or the section is made here (with `section`'s fields).
    section_number: int | None = None
    section_key: str | None = Field(default=None, max_length=64)
    section: OneBoxSection = Field(default_factory=OneBoxSection)
    box_number: int | None = None  # the number on screen; kept when free in the section
    net_weight: float | None = None
    gross_weight: float | None = None
    lot_number: str | None = None
    count: int | None = None
    # The browser's key for this box: a retry (lost reply) returns the box the
    # first attempt saved instead of adding it again.
    client_key: str | None = Field(default=None, max_length=80)


class OneBoxUpdate(BaseModel):
    net_weight: float | None = None
    gross_weight: float | None = None
    lot_number: str | None = None
    count: int | None = None


class OneBoxResponse(BaseModel):
    box: BoxOut
    created: bool  # False: the box already existed (a retry, or an update)


class OneBoxDeleteResponse(BaseModel):
    box_id: str
    deleted: bool = True


async def _po_header_or_404(pool, user, transaction_no: str):
    header = await pool.fetchrow("SELECT * FROM po_header WHERE transaction_no = $1", transaction_no)
    if not header:
        raise HTTPException(404, detail="Transaction not found.")
    await _assert_site_visible(pool, user, transaction_no, header)
    return header


@router.post("/{transaction_no}/box", response_model=OneBoxResponse, status_code=201)
async def stores_add_one_box(request: Request, transaction_no: str, body: OneBoxCreate,
                             user=Depends(require_permission("purchase", "material_in", action="create"))):
    """Save ONE new box onto the PO (keyed by its transaction number) and return
    it with its box_id, so its label can carry the QR. The box is appended to
    `section_number`, or to the draft section `section_key` names (made by the
    first box saved from it). Nothing else on the PO is touched. A weighed box
    gets its inventory batch, as in POST /boxes."""
    pool = request.app.state.db_pool
    header = await _po_header_or_404(pool, user, transaction_no)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_BOX_LOCK_SQL, transaction_no)
            keys = await _client_keys(conn)
            if keys and body.client_key:
                again = await conn.fetchrow(
                    "SELECT * FROM po_box WHERE transaction_no = $1 AND client_key = $2",
                    transaction_no, body.client_key,
                )
                if again:
                    return {"box": _box_row_to_out(again), "created": False}
            if not await conn.fetchval(
                "SELECT 1 FROM po_line WHERE transaction_no = $1 AND line_number = $2",
                transaction_no, body.line_number,
            ):
                raise HTTPException(404, detail=f"Line {body.line_number} not found on this PO.")

            section_number = body.section_number
            if section_number is None and keys and body.section_key:
                section_number = await conn.fetchval(
                    "SELECT section_number FROM po_section "
                    "WHERE transaction_no = $1 AND line_number = $2 AND client_key = $3",
                    transaction_no, body.line_number, body.section_key,
                )
            if section_number is None:
                section_number = await conn.fetchval(
                    "SELECT COALESCE(MAX(section_number), 0) + 1 FROM po_section "
                    "WHERE transaction_no = $1 AND line_number = $2",
                    transaction_no, body.line_number,
                )
                await _insert_section(
                    conn, transaction_no, body.line_number, section_number,
                    body.section.lot_number, 0,
                    body.section.manufacturing_date, body.section.expiry_date,
                    body.section_key if keys else None,
                )
            sec = await conn.fetchrow(
                "SELECT manufacturing_date, expiry_date FROM po_section "
                "WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3",
                transaction_no, body.line_number, section_number,
            )
            if sec is None:
                raise HTTPException(
                    404, detail=f"Section {section_number} not found for line {body.line_number}.")

            taken = await _taken_box_numbers(conn, transaction_no, body.line_number, section_number)
            base, seq = await _next_box_ids(conn, transaction_no, None)
            box_id = f"{base}-{seq}"
            await _insert_boxes(conn, header, transaction_no, body.line_number, section_number,
                                sec["manufacturing_date"], sec["expiry_date"], [{
                                    "box_id": box_id,
                                    "box_number": _pick_box_number(body.box_number, taken),
                                    "net_weight": body.net_weight, "gross_weight": body.gross_weight,
                                    "lot_number": body.lot_number, "count": body.count,
                                    "client_key": body.client_key if keys else None,
                                }], keys)
            await conn.execute(
                "UPDATE po_section SET box_count = COALESCE(box_count, 0) + 1 "
                "WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3",
                transaction_no, body.line_number, section_number,
            )
            await _refresh_receiving_status(conn, transaction_no)
            row = await conn.fetchrow("SELECT * FROM po_box WHERE box_id = $1", box_id)
    return {"box": _box_row_to_out(row), "created": True}


_BOX_FIELDS = ("net_weight", "gross_weight", "lot_number", "count")


@router.put("/{transaction_no}/box/{box_id}", response_model=OneBoxResponse)
async def stores_update_one_box(request: Request, transaction_no: str, box_id: str, body: OneBoxUpdate,
                                user=Depends(require_permission("purchase", "material_in", action="edit"))):
    """Update ONE saved box, found by its box_id on this PO: only its weights,
    lot and count, and only the fields sent (a field left out or null keeps its
    value). No other box, section or PO field changes. A weight correction
    reaches the box's stock as in PUT /boxes (409 if that stock is used up); a
    box weighed for the first time gets its batch. Each change is logged in
    box_edit_logs."""
    pool = request.app.state.db_pool
    header = await _po_header_or_404(pool, user, transaction_no)
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                "SELECT * FROM po_box WHERE box_id = $1 AND transaction_no = $2 FOR UPDATE",
                box_id, transaction_no,
            )
            if old is None:
                raise HTTPException(404, detail="Box not found on this PO.")
            before = await _lock_box_batches(conn, transaction_no, [box_id])
            await conn.execute(
                """
                UPDATE po_box SET
                    net_weight   = COALESCE($3, net_weight),
                    gross_weight = COALESCE($4, gross_weight),
                    lot_number   = COALESCE($5, lot_number),
                    count        = COALESCE($6, count)
                WHERE box_id = $1 AND transaction_no = $2
                """,
                box_id, transaction_no, body.net_weight, body.gross_weight, body.lot_number, body.count,
            )
            await _sync_box_corrections(conn, transaction_no, before, getattr(user, "full_name", None))
            await _book_missing_inward_batches(conn, header, transaction_no, [box_id])
            new = await conn.fetchrow("SELECT * FROM po_box WHERE box_id = $1", box_id)
            for f in _BOX_FIELDS:
                if old[f] != new[f]:
                    await _log_box(conn, user, transaction_no, box_id, f, old[f], new[f],
                                   f"Material In: box {new['box_number']} {f} {old[f]} -> {new[f]}")
            await _refresh_receiving_status(conn, transaction_no)
    return {"box": _box_row_to_out(new), "created": False}


# Records that carry a PO box's id once the box has moved on (a transfer, a job
# card scan, an issue). Such a box is in use: deleting it would orphan them.
_BOX_IN_USE: tuple[tuple[str, str, str], ...] = (
    ("interunit_transfer_boxes", "box_id", "an inter-unit transfer"),
    ("interunit_transfer_in_boxes", "box_id", "an inter-unit transfer in"),
    ("pending_transfer_stock", "box_id", "a pending transfer"),
    ("jc_box_scan", "box_id", "a job card scan"),
    ("issue_note_line", "box_id", "an issue note"),
    ("material_document_line", "box_id", "a material document"),
)


@router.delete("/{transaction_no}/box/{box_id}", response_model=OneBoxDeleteResponse)
async def stores_delete_one_box(request: Request, transaction_no: str, box_id: str,
                                user=Depends(require_permission("purchase", "material_in", action="edit"))):
    """Delete ONE box, found by its box_id on this PO. No other box is touched:
    the rest keep their numbers and ids, and its section's box count drops by
    one. Refused (409) once the box is in use -- on a GRN, in QC or stage-2,
    moved, scanned or issued, or its stock no longer whole and available. Its
    stock batch is discarded (kept, at 0 kg, with an ADJUSTED event), and the
    deletion is logged in box_edit_logs, so its id is never given to another box."""
    pool = request.app.state.db_pool
    await _po_header_or_404(pool, user, transaction_no)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_BOX_LOCK_SQL, transaction_no)
            box = await conn.fetchrow(
                "SELECT * FROM po_box WHERE box_id = $1 AND transaction_no = $2 FOR UPDATE",
                box_id, transaction_no,
            )
            if box is None:
                raise HTTPException(404, detail="Box not found on this PO.")
            label = f"Box {box['box_number']} (line {box['line_number']})"

            def in_use(why: str):
                return HTTPException(409, detail={
                    "error": "box_in_use",
                    "message": f"{label} can't be deleted: {why}.",
                })

            if box["grn_no"]:
                raise in_use(f"it is on GRN {box['grn_no']}")
            if box["qc_inspection_id"]:
                raise in_use("it is in a QC inspection")
            if box["stage2_approval_id"]:
                raise in_use("it is in a stage-2 approval")
            for table, col, what in _BOX_IN_USE:
                if await conn.fetchval("SELECT to_regclass($1)", f"public.{table}") and await conn.fetchval(
                    f"SELECT 1 FROM {table} WHERE {col} = $1 LIMIT 1", box_id,
                ):
                    raise in_use(f"it is on {what}")

            batch = await conn.fetchrow(
                "SELECT status, original_qty_kg, current_qty_kg FROM inventory_batch "
                "WHERE batch_id = $1 FOR UPDATE",
                box_id,
            )
            if batch is not None:
                if batch["status"] != "AVAILABLE" or batch["current_qty_kg"] != batch["original_qty_kg"]:
                    raise in_use(
                        f"its stock is {str(batch['status']).lower()} with {batch['current_qty_kg']} of "
                        f"{batch['original_qty_kg']} kg left; ask the inventory team to adjust it")
                await conn.execute(
                    "UPDATE inventory_batch SET status = 'DISCARDED', current_qty_kg = 0, updated_at = NOW() "
                    "WHERE batch_id = $1",
                    box_id,
                )
                await conn.execute(
                    """
                    INSERT INTO inventory_event_log
                        (batch_id, event_type, from_status, to_status, quantity_kg,
                         reference_type, performed_by, notes)
                    VALUES ($1, 'ADJUSTED', 'AVAILABLE', 'DISCARDED', $2, 'material_in', $3, $4)
                    """,
                    box_id, -batch["current_qty_kg"], getattr(user, "full_name", None),
                    f"Material In {transaction_no}: {label.lower()} deleted",
                )

            await conn.execute("DELETE FROM po_box WHERE box_id = $1 AND transaction_no = $2", box_id, transaction_no)
            await conn.execute(
                "UPDATE po_section SET box_count = GREATEST(COALESCE(box_count, 0) - 1, 0) "
                "WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3",
                transaction_no, box["line_number"], box["section_number"],
            )
            snapshot = {k: box[k] for k in ("line_number", "section_number", "box_number", *_BOX_FIELDS)}
            await _log_box(conn, user, transaction_no, box_id, "deleted", snapshot, None,
                           f"Material In: {label.lower()} deleted")
            await _refresh_receiving_status(conn, transaction_no)
    return {"box_id": box_id, "deleted": True}
