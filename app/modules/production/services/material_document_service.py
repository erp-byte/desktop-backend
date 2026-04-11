"""Material Document service — SAP MIGO equivalent.
Every stock movement creates an immutable material document.
The material_document is the source of truth; inventory_batch is derived/cached.
"""
from datetime import datetime, timezone


def _gen_id(seq_val: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"MATDOC-{d}-{seq_val:04d}"


# ── Movement type constants (SAP-aligned) ──
MVT_GR_PO           = '101'   # Goods Receipt against PO
MVT_GR_REVERSAL     = '102'   # GR Reversal
MVT_RETURN_VENDOR   = '122'   # Return to Vendor
MVT_GI_PRODUCTION   = '261'   # Goods Issue to Production Order
MVT_GI_RETURN       = '262'   # Return from Production Order
MVT_TRANSFER_PLANT  = '301'   # Plant-to-Plant transfer
MVT_TRANSFER_LOC    = '311'   # Storage Location transfer
MVT_QC_ACCEPT       = '321'   # QC Hold → Unrestricted (Accept)
MVT_QC_REJECT       = '322'   # QC Hold → Blocked (Reject)
MVT_FG_RECEIPT      = '531'   # FG Receipt from Production
MVT_SCRAP           = '551'   # Scrapping / Write-off
MVT_LEGACY          = '561'   # Initial Stock Upload


async def create_material_document(conn, *, movement_type, reference_type=None,
                                    reference_id=None, created_by, entity="cfpl",
                                    lines, reversal_of=None, notes=None):
    """Create an immutable material document with one or more lines.

    Args:
        movement_type: SAP-aligned code (101, 261, 301, etc.)
        reference_type: PO, JOB_CARD, TRANSFER, QC, RTV, ISN
        reference_id: the referenced document ID
        created_by: user name
        entity: cfpl or cdpl
        lines: list of dicts with keys:
            sku_name, batch_id, quantity_kg, uom,
            from_location, to_location, from_status, to_status,
            lot_number, box_id
        reversal_of: mat_doc_id if this is a reversal
        notes: free text

    Returns:
        dict with mat_doc_id and line count
    """
    seq = await conn.fetchval("SELECT nextval('seq_matdoc')")
    doc_id = _gen_id(seq)

    await conn.execute("""
        INSERT INTO material_document
            (mat_doc_id, movement_type, reference_type, reference_id,
             created_by, entity, reversal_of, is_reversal, notes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
    """, doc_id, movement_type, reference_type, reference_id,
         created_by, entity, reversal_of, reversal_of is not None, notes)

    for i, line in enumerate(lines, 1):
        await conn.execute("""
            INSERT INTO material_document_line
                (mat_doc_id, line_number, sku_name, batch_id, movement_type,
                 quantity_kg, uom, from_location, to_location,
                 from_status, to_status, lot_number, box_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        """, doc_id, i, line.get("sku_name", ""), line.get("batch_id"),
             movement_type, line.get("quantity_kg", 0), line.get("uom", "kg"),
             line.get("from_location"), line.get("to_location"),
             line.get("from_status"), line.get("to_status"),
             line.get("lot_number"), line.get("box_id"))

    return {"mat_doc_id": doc_id, "movement_type": movement_type, "lines": len(lines)}


async def create_reversal(conn, *, original_mat_doc_id, created_by, reason=None):
    """Create a reversal document for an existing material document."""
    orig = await conn.fetchrow(
        "SELECT * FROM material_document WHERE mat_doc_id = $1", original_mat_doc_id
    )
    if not orig:
        return {"error": "Original document not found"}
    if orig["is_reversal"]:
        return {"error": "Cannot reverse a reversal document"}

    # Get reversal movement type
    rev_type = await conn.fetchval(
        "SELECT reversal_type FROM movement_type_ref WHERE movement_type = $1",
        orig["movement_type"]
    )
    if not rev_type:
        rev_type = orig["movement_type"]  # fallback: same type with reversal flag

    # Get original lines
    orig_lines = await conn.fetch(
        "SELECT * FROM material_document_line WHERE mat_doc_id = $1 ORDER BY line_number",
        original_mat_doc_id
    )

    # Create reversed lines (swap from/to, negate quantity conceptually)
    rev_lines = []
    for ol in orig_lines:
        rev_lines.append({
            "sku_name": ol["sku_name"],
            "batch_id": ol["batch_id"],
            "quantity_kg": ol["quantity_kg"],
            "uom": ol["uom"],
            "from_location": ol["to_location"],   # swap
            "to_location": ol["from_location"],    # swap
            "from_status": ol["to_status"],
            "to_status": ol["from_status"],
            "lot_number": ol["lot_number"],
            "box_id": ol["box_id"],
        })

    result = await create_material_document(
        conn,
        movement_type=rev_type,
        reference_type=orig["reference_type"],
        reference_id=orig["reference_id"],
        created_by=created_by,
        entity=orig["entity"],
        lines=rev_lines,
        reversal_of=original_mat_doc_id,
        notes=reason or f"Reversal of {original_mat_doc_id}",
    )

    return result


async def get_documents_for_reference(conn, reference_type, reference_id):
    """Get all material documents for a reference (e.g., all docs for a PO)."""
    rows = await conn.fetch("""
        SELECT md.*, array_agg(json_build_object(
            'line_number', ml.line_number, 'sku_name', ml.sku_name,
            'batch_id', ml.batch_id, 'quantity_kg', ml.quantity_kg,
            'from_location', ml.from_location, 'to_location', ml.to_location
        ) ORDER BY ml.line_number) AS lines
        FROM material_document md
        JOIN material_document_line ml ON md.mat_doc_id = ml.mat_doc_id
        WHERE md.reference_type = $1 AND md.reference_id = $2
        GROUP BY md.id
        ORDER BY md.created_at DESC
    """, reference_type, reference_id)
    return [dict(r) for r in rows]


async def reconcile_batch(conn, batch_id):
    """Reconcile a batch's current_qty_kg against material documents.
    Returns the expected qty from documents vs actual in inventory_batch.
    """
    # Sum all IN movements
    total_in = await conn.fetchval("""
        SELECT COALESCE(SUM(ml.quantity_kg), 0)
        FROM material_document_line ml
        JOIN material_document md ON ml.mat_doc_id = md.mat_doc_id
        JOIN movement_type_ref mtr ON md.movement_type = mtr.movement_type
        WHERE ml.batch_id = $1 AND mtr.direction = 'IN'
    """, batch_id) or 0

    # Sum all OUT movements
    total_out = await conn.fetchval("""
        SELECT COALESCE(SUM(ml.quantity_kg), 0)
        FROM material_document_line ml
        JOIN material_document md ON ml.mat_doc_id = md.mat_doc_id
        JOIN movement_type_ref mtr ON md.movement_type = mtr.movement_type
        WHERE ml.batch_id = $1 AND mtr.direction = 'OUT'
    """, batch_id) or 0

    expected = float(total_in) - float(total_out)

    actual = await conn.fetchval(
        "SELECT current_qty_kg FROM inventory_batch WHERE batch_id = $1", batch_id
    )
    actual = float(actual) if actual else 0

    return {
        "batch_id": batch_id,
        "total_in_kg": float(total_in),
        "total_out_kg": float(total_out),
        "expected_qty_kg": expected,
        "actual_qty_kg": actual,
        "variance_kg": round(actual - expected, 3),
        "reconciled": abs(actual - expected) < 0.01,
    }
