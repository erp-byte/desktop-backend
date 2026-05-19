"""Lot Picker, FIFO Skip, Force Assign, Issue Note service — Sections C4, D2-D4.
Aligned with SAP MM: Movement types, Material Documents, FIFO/FEFO, QC_HOLD filtering.
"""
import json
from datetime import datetime, timezone, timedelta, date as date_type


def _gen_isn(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"ISN-{d}-{seq_val:04d}"


def _gen_block_id(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"BLK-{d}-{seq_val:04d}"


# ── List lots for item (FIFO sorted) ──
async def get_lots(conn, *, item_description, warehouse=None, job_card_id=None,
                   entity="cfpl"):
    # Exclude QC_HOLD stock (SAP: only unrestricted stock available for issue)
    conditions = ["ib.sku_name ILIKE $1", "ib.current_qty_kg > 0",
                  "ib.status NOT IN ('QC_HOLD','SCRAPPED','DISCARDED')"]
    params = [f"%{item_description}%"]
    idx = 2

    if warehouse:
        conditions.append(f"(ib.floor_id ILIKE ${idx} OR ib.warehouse_id ILIKE ${idx})")
        params.append(f"%{warehouse}%")
        idx += 1

    if entity:
        conditions.append(f"ib.entity = ${idx}")
        params.append(entity)
        idx += 1

    where = " AND ".join(conditions)

    rows = await conn.fetch(f"""
        SELECT ib.batch_id, ib.sku_name AS item_description, ib.item_type,
               ib.lot_number, ib.inward_date, ib.current_qty_kg AS net_wt,
               ib.current_qty_kg AS current_qty_kg, ib.current_qty_kg AS total_inventory,
               ib.status, ib.floor_id, ib.warehouse_id AS warehouse,
               ib.ownership,
               ib.transaction_no AS tr_number, ib.entity,
               ib.expiry_date,
               CASE WHEN ib.batch_id LIKE 'LEGACY-%' THEN TRUE ELSE FALSE END AS is_legacy,
               CASE WHEN LOWER(ib.floor_id) LIKE '%cold%' OR LOWER(ib.warehouse_id) LIKE '%cold%'
                    THEN TRUE ELSE FALSE END AS is_cold_storage,
               lb.blocked_for_so, lb.blocked_by_user AS blocked_by,
               lb.blocked_at, lb.skip_reason AS block_reason
        FROM inventory_batch ib
        LEFT JOIN lot_block lb ON lb.batch_id = ib.batch_id AND lb.is_active = TRUE
        WHERE {where}
        ORDER BY
            -- FEFO: if expiry_date exists, sort by expiry first; fallback to FIFO (inward_date)
            CASE WHEN ib.expiry_date IS NOT NULL THEN ib.expiry_date ELSE '2099-12-31'::date END ASC,
            ib.inward_date ASC, ib.created_at ASC
    """, *params)

    lots = []
    today = date_type.today()
    for r in rows:
        lot = dict(r)
        # Shelf life warning (SAP: min remaining shelf life check)
        if lot.get("expiry_date"):
            exp = lot["expiry_date"] if isinstance(lot["expiry_date"], date_type) else None
            if exp:
                days_left = (exp - today).days
                lot["days_to_expiry"] = days_left
                lot["expiry_warning"] = days_left <= 30
                lot["expired"] = days_left <= 0
            else:
                lot["days_to_expiry"] = None
                lot["expiry_warning"] = False
                lot["expired"] = False
        # Override status if blocked
        if lot.get("blocked_for_so"):
            lot["status"] = "BLOCKED"
        lot["lot_id"] = lot["batch_id"]
        lots.append(lot)

    return {"lots": lots}


# ── List lots at other warehouses ──
async def get_lots_other_warehouses(conn, *, item_description, exclude_warehouse=None,
                                     entity="cfpl"):
    conditions = ["ib.sku_name ILIKE $1", "ib.current_qty_kg > 0", "ib.status = 'AVAILABLE'"]
    params = [f"%{item_description}%"]
    idx = 2

    if exclude_warehouse:
        conditions.append(f"ib.floor_id NOT ILIKE ${idx} AND ib.warehouse_id NOT ILIKE ${idx}")
        params.append(f"%{exclude_warehouse}%")
        idx += 1

    if entity:
        conditions.append(f"ib.entity = ${idx}")
        params.append(entity)
        idx += 1

    where = " AND ".join(conditions)

    rows = await conn.fetch(f"""
        SELECT ib.batch_id, ib.sku_name AS item_description,
               ib.lot_number, ib.inward_date, ib.current_qty_kg AS net_wt,
               ib.status, ib.floor_id, ib.warehouse_id AS warehouse,
               CASE WHEN LOWER(ib.floor_id) LIKE '%cold%' OR LOWER(ib.warehouse_id) LIKE '%cold%' THEN TRUE ELSE FALSE END AS is_cold_storage
        FROM inventory_batch ib
        WHERE {where}
        ORDER BY ib.inward_date ASC
    """, *params)

    return {"lots": [dict(r) for r in rows]}


# ── FIFO skip recording ──
async def record_fifo_skip(conn, *, batch_id, job_card_id=None, reason,
                            detail=None, disposition="leave_available",
                            block_for_so=None, skipped_by):
    # Log the skip
    await conn.execute("""
        INSERT INTO fifo_skip_log (batch_id, job_card_id, reason, detail,
                                    disposition, block_for_so, skipped_by)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
    """, batch_id, job_card_id, reason, detail, disposition, block_for_so, skipped_by)

    # If blocking for SO, create lot_block
    if disposition == "block_for_so" and block_for_so:
        seq = await conn.fetchval("SELECT nextval('seq_block')")
        block_id = _gen_block_id(seq)
        await conn.execute("""
            INSERT INTO lot_block (block_id, batch_id, lot_number, blocked_for_so,
                                    blocked_by_user, skip_reason)
            VALUES ($1,$2, (SELECT lot_number FROM inventory_batch WHERE batch_id=$2),
                    $3,$4,$5)
        """, block_id, batch_id, block_for_so, skipped_by, reason)

        # Update batch status
        await conn.execute("""
            UPDATE inventory_batch SET status = 'BLOCKED' WHERE batch_id = $1
        """, batch_id)

    return {"recorded": True}


# ── Force assign blocked lot ──
async def force_assign_lot(conn, *, batch_id, new_so_id, override_comment,
                            force_assigned_by):
    # Get current block
    block = await conn.fetchrow("""
        SELECT * FROM lot_block WHERE batch_id = $1 AND is_active = TRUE
    """, batch_id)

    if block:
        # Deactivate old block
        await conn.execute("""
            UPDATE lot_block SET is_active = FALSE,
                force_assigned_by = $2, force_assigned_at = NOW(),
                override_comment = $3, previous_so = blocked_for_so
            WHERE id = $1
        """, block["id"], force_assigned_by, override_comment)

    # Create new block for new SO
    seq = await conn.fetchval("SELECT nextval('seq_block')")
    block_id = _gen_block_id(seq)
    await conn.execute("""
        INSERT INTO lot_block (block_id, batch_id, lot_number, blocked_for_so,
                                blocked_by_user, skip_reason, comment)
        VALUES ($1,$2, (SELECT lot_number FROM inventory_batch WHERE batch_id=$2),
                $3,$4,'force_reassign',$5)
    """, block_id, batch_id, new_so_id, force_assigned_by, override_comment)

    return {"reassigned": True, "new_block_id": block_id}


# ── Get box by ID ──
async def get_box(conn, box_id):
    row = await conn.fetchrow("""
        SELECT b.box_id, b.net_weight AS net_wt, b.gross_weight AS gross_wt,
               b.lot_number, b.count, b.transaction_no,
               l.sku_name AS item_description, h.warehouse
        FROM po_box b
        JOIN po_line l ON b.transaction_no = l.transaction_no AND b.line_number = l.line_number
        JOIN po_header h ON b.transaction_no = h.transaction_no
        WHERE b.box_id = $1
        LIMIT 1
    """, box_id)
    if not row:
        return None
    return dict(row)


# ── Create issue note ──
async def create_issue_note(conn, *, job_card_id, so_id=None, customer_name=None,
                             bom_line_id=None, issued_by, status="confirmed",
                             lines, reservation_minutes=30):
    seq = await conn.fetchval("SELECT nextval('seq_isn')")
    isn_id = _gen_isn(seq)

    total_wt = sum(l.get("net_wt_issued", 0) for l in lines)
    expires = datetime.now(timezone.utc) + timedelta(minutes=reservation_minutes) if status == "draft" else None

    await conn.execute("""
        INSERT INTO issue_note
            (issue_note_id, job_card_id, so_id, customer_name, bom_line_id,
             issued_by, status, reservation_expires_at, total_weight_kg)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
    """, isn_id, job_card_id, so_id, customer_name, bom_line_id,
         issued_by, status, expires, total_wt)

    # Insert lines
    for line in lines:
        await conn.execute("""
            INSERT INTO issue_note_line
                (issue_note_id, bom_line_id, sku, material_type, lot_number,
                 lot_id, tr_number, warehouse, net_wt_issued, qty_cartons,
                 box_id, fifo_skipped, skip_reason)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        """, isn_id, line.get("bom_line_id"), line.get("sku"),
             line.get("material_type"), line.get("lot_number"),
             line.get("lot_id"), line.get("tr_number"),
             line.get("warehouse"), line.get("net_wt_issued", 0),
             line.get("qty_cartons"), line.get("box_id"),
             line.get("fifo_skipped", False), line.get("skip_reason"))

    # If confirmed, update inventory + create material document (SAP mvt 261)
    if status == "confirmed":
        from .material_document_service import create_material_document, MVT_GI_PRODUCTION
        mat_doc_lines = []
        for line in lines:
            if line.get("lot_id"):
                await conn.execute("""
                    UPDATE inventory_batch
                    SET current_qty_kg = GREATEST(0, current_qty_kg - $2)
                    WHERE batch_id = $1
                """, line["lot_id"], line.get("net_wt_issued", 0))
                mat_doc_lines.append({
                    "sku_name": line.get("sku", ""),
                    "batch_id": line["lot_id"],
                    "quantity_kg": line.get("net_wt_issued", 0),
                    "from_location": line.get("warehouse", ""),
                    "to_location": f"Production: {job_card_id}",
                    "from_status": "AVAILABLE",
                    "to_status": "ISSUED",
                    "lot_number": line.get("lot_number"),
                    "box_id": line.get("box_id"),
                })

        if mat_doc_lines:
            await create_material_document(
                conn,
                movement_type=MVT_GI_PRODUCTION,
                reference_type="ISN",
                reference_id=isn_id,
                created_by=issued_by,
                lines=mat_doc_lines,
            )

    return {"issue_note_id": isn_id, "total_weight_kg": total_wt, "lines": len(lines)}


# ── Raise purchase indent from JC BOM shortfall ──
async def raise_purchase_indent(conn, *, material_sku_name, item_category=None,
                                 material_type, required_qty_kg, uom="kg",
                                 job_card_id=None, so_reference=None,
                                 customer_name=None, trigger_reason="Insufficient stock",
                                 entity="cfpl"):
    from .indent_manager import create_indent
    result = await create_indent(
        conn,
        material_sku_name=material_sku_name,
        required_qty_kg=required_qty_kg,
        entity=entity,
        source="auto_shortfall",
        job_card_id=job_card_id,
        so_reference=so_reference,
        customer_name=customer_name,
        cascade_reason=trigger_reason,
    )
    return result
