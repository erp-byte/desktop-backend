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
      • 'extra received'     — total received net weight > total ordered po_weight
      • 'partially received' — at least one box exists
      • 'pending'            — no boxes
    """
    row = await conn.fetchrow(
        """
        SELECT
            (SELECT COUNT(*)                     FROM po_box  WHERE transaction_no = $1) AS n_boxes,
            (SELECT COALESCE(SUM(net_weight), 0) FROM po_box  WHERE transaction_no = $1) AS rec_net,
            (SELECT COALESCE(SUM(po_weight), 0)  FROM po_line WHERE transaction_no = $1) AS ord_wt
        """,
        transaction_no,
    )
    if (row["n_boxes"] or 0) == 0:
        status = "pending"
    elif float(row["rec_net"] or 0) > float(row["ord_wt"] or 0):
        status = "extra received"
    else:
        status = "partially received"
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

    if not body.sections:
        raise HTTPException(400, detail="No sections provided.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            # ── Server-side box_id generation ─────────────────────────────────
            # box_id is a global TEXT PRIMARY KEY. Minting it on the client as
            # {Date.now()-last8}-{per-batch counter} restarts the counter every
            # batch (so appended boxes aren't sequential) and can collide on the
            # PK. Generate it here instead: reuse the transaction's existing
            # 8-digit base and take the next counter GLOBALLY for that base — so
            # boxes continue the sequence (…-2, …-3) and never hit the PK.
            def _base_of(bid):
                return bid.rsplit("-", 1)[0] if bid and "-" in bid else (bid or None)

            _existing_bid = await conn.fetchval(
                """
                SELECT box_id FROM po_box WHERE transaction_no = $1
                ORDER BY created_at DESC, box_id DESC LIMIT 1
                """,
                transaction_no,
            )
            box_base = _base_of(_existing_bid)
            if not box_base:
                # First boxes for this transaction — seed the base from the
                # client's proposed id ({last8}-{n}); constant fallback otherwise.
                for _s in body.sections:
                    if _s.boxes:
                        box_base = _base_of(_s.boxes[0].box_id)
                        if box_base:
                            break
            if not box_base:
                box_base = "10000000"
            _max_seq = await conn.fetchval(
                """
                SELECT COALESCE(MAX(CAST(split_part(box_id, '-', 2) AS INTEGER)), 0)
                FROM po_box
                WHERE box_id LIKE $1 || '-%' AND split_part(box_id, '-', 2) ~ '^[0-9]+$'
                """,
                box_base,
            )
            next_box_seq = (_max_seq or 0) + 1

            for section in body.sections:
                # The same payload again (a retry after its reply was lost):
                # its boxes are already saved, so don't add them twice.
                if not await _claim_request_key(conn, transaction_no, section.client_key):
                    continue
                if section.section_number is not None:
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
                        section.section_number,
                    )
                    if not sec_exists:
                        raise HTTPException(
                            404,
                            detail=f"Section {section.section_number} not found for line {section.line_number}.",
                        )
                    section_number = section.section_number
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
                        len(section.boxes),
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
                # Get SKU name and warehouse for inventory batch creation
                line_info = await conn.fetchrow(
                    "SELECT sku_name FROM po_line WHERE transaction_no = $1 AND line_number = $2",
                    transaction_no, section.line_number)
                sku_for_batch = line_info['sku_name'] if line_info else 'UNKNOWN'
                warehouse_for_batch = existing.get('warehouse', None)
                entity_for_batch = existing.get('entity', 'cfpl')
                po_date_for_batch = existing.get('po_date', None)

                # box_number continues from the section's actual last box, not the
                # client's guess (which is stale in lazy/paginated mode). New
                # section → 0 → boxes start at 1.
                next_box_number = (await conn.fetchval(
                    "SELECT COALESCE(MAX(box_number), 0) FROM po_box "
                    "WHERE transaction_no = $1 AND line_number = $2 AND section_number = $3",
                    transaction_no, section.line_number, section_number,
                )) + 1

                # Build all rows first, then insert in two batched round-trips
                # (executemany) instead of 2 per box — the big speedup for large
                # sections.
                inward_date = _coerce_date(po_date_for_batch)
                mfg_date = _coerce_date(section.manufacturing_date)
                exp_date = _coerce_date(section.expiry_date)
                po_box_rows = []
                batch_rows = []
                for b in section.boxes:
                    # Server-generated, globally-unique, sequential box_id.
                    b_box_id = f"{box_base}-{next_box_seq}"
                    next_box_seq += 1
                    this_box_number = next_box_number
                    next_box_number += 1
                    po_box_rows.append((
                        b_box_id, transaction_no, section.line_number, section_number,
                        this_box_number, b.net_weight, b.gross_weight, b.lot_number, b.count,
                    ))
                    # Unweighed boxes (net weight empty) are saved without a
                    # batch; PUT /boxes books it once the weight is recorded.
                    if _is_weighed(b.net_weight):
                        batch_rows.append((
                            b_box_id, sku_for_batch, transaction_no, b.lot_number,
                            inward_date, mfg_date, exp_date,
                            b.net_weight, warehouse_for_batch, entity_for_batch,
                        ))

                if po_box_rows:
                    await conn.executemany(
                        """
                        INSERT INTO po_box (
                            box_id, transaction_no, line_number, section_number,
                            box_number, net_weight, gross_weight, lot_number, count
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        po_box_rows,
                    )
                if batch_rows:
                    # Auto-create one inventory batch per weighed box.
                    await conn.executemany(_INWARD_BATCH_SQL, batch_rows)
            await _refresh_receiving_status(conn, transaction_no)

    updated = await pool.fetchrow(
        "SELECT * FROM po_header WHERE transaction_no = $1", transaction_no
    )
    details = await fetch_po_details(pool, [transaction_no], [updated])
    return POHeaderOut(**details[0])
