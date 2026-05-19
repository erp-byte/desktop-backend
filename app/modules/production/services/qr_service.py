"""QR Material Receipt — scan po_box QR codes to receive material into job cards.

Flow:
  1. Scan box_id (QR code on physical box)
  2. Lookup po_box → get net_weight, lot_number
  3. Match against job_card_rm_indent or job_card_pm_indent
  4. Accumulate issued_qty, deduct floor_inventory, create floor_movement
  5. When all indents fulfilled → job_card.status = 'material_received'
"""

import logging

logger = logging.getLogger(__name__)


async def receive_material_via_qr(conn, job_card_id: int, box_ids: list[str], entity: str) -> dict:
    """Receive material by scanning QR codes (po_box.box_id) against job card indents."""

    jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}

    # No store approval gate — floor manager raises indent, store provides material directly

    # Load all indent lines for this job card
    rm_indents = await conn.fetch(
        "SELECT * FROM job_card_rm_indent WHERE job_card_id = $1", job_card_id,
    )
    pm_indents = await conn.fetch(
        "SELECT * FROM job_card_pm_indent WHERE job_card_id = $1", job_card_id,
    )

    accepted = []
    rejected = []

    for box_id in box_ids:
        # 1. Lookup po_box
        box = await conn.fetchrow("SELECT * FROM po_box WHERE box_id = $1", box_id)
        if not box:
            rejected.append({"box_id": box_id, "reason": "Box not found in system"})
            continue

        # 2. Check not already consumed
        already_used = await conn.fetchval(
            """
            SELECT COUNT(*) FROM job_card_rm_indent
            WHERE $1 = ANY(scanned_box_ids)
            """,
            box_id,
        )
        if not already_used:
            already_used = await conn.fetchval(
                "SELECT COUNT(*) FROM job_card_pm_indent WHERE $1 = ANY(scanned_box_ids)",
                box_id,
            )
        if already_used and already_used > 0:
            rejected.append({"box_id": box_id, "reason": "Box already consumed in another job card"})
            continue

        # 3. Get material name from po_line
        po_line = await conn.fetchrow(
            "SELECT sku_name FROM po_line WHERE transaction_no = $1 AND line_number = $2",
            box['transaction_no'], box['line_number'],
        )
        material_name = po_line['sku_name'] if po_line else None
        net_weight = float(box['net_weight']) if box['net_weight'] else 0
        lot_number = box.get('lot_number')

        # 4. Match against RM indent
        matched = False
        for indent in rm_indents:
            sku = indent['material_sku_name'].lower()
            mat = (material_name or '').lower()
            if sku in mat or mat in sku:
                # Update indent
                await conn.execute(
                    """
                    UPDATE job_card_rm_indent SET
                        scanned_box_ids = array_append(COALESCE(scanned_box_ids, '{}'), $2),
                        issued_qty = COALESCE(issued_qty, 0) + $3,
                        batch_no = COALESCE($4, batch_no),
                        variance = (COALESCE(issued_qty, 0) + $3) - gross_qty,
                        status = CASE
                            WHEN (COALESCE(issued_qty, 0) + $3) >= gross_qty THEN 'fulfilled'
                            ELSE 'partial'
                        END
                    WHERE rm_indent_id = $1
                    """,
                    indent['rm_indent_id'], box_id, net_weight, lot_number,
                )
                matched = True
                break

        # Try PM indent if not matched to RM
        if not matched:
            for indent in pm_indents:
                sku = indent['material_sku_name'].lower()
                mat = (material_name or '').lower()
                if sku in mat or mat in sku:
                    await conn.execute(
                        """
                        UPDATE job_card_pm_indent SET
                            scanned_box_ids = array_append(COALESCE(scanned_box_ids, '{}'), $2),
                            issued_qty = COALESCE(issued_qty, 0) + $3,
                            batch_no = COALESCE($4, batch_no),
                            variance = (COALESCE(issued_qty, 0) + $3) - gross_qty,
                            status = CASE
                                WHEN (COALESCE(issued_qty, 0) + $3) >= gross_qty THEN 'fulfilled'
                                ELSE 'partial'
                            END
                        WHERE pm_indent_id = $1
                        """,
                        indent['pm_indent_id'], box_id, net_weight, lot_number,
                    )
                    matched = True
                    break

        if not matched:
            rejected.append({"box_id": box_id, "reason": f"Material '{material_name}' does not match any indent line"})
            continue

        # 5. Deduct floor_inventory
        floor_loc = 'rm_store'  # default
        await conn.execute(
            """
            UPDATE floor_inventory SET
                quantity_kg = GREATEST(0, quantity_kg - $3),
                last_updated = NOW()
            WHERE sku_name ILIKE $1 AND floor_location = $2 AND entity = $4
            """,
            f"%{material_name}%", floor_loc, net_weight, entity,
        )

        # 5b. Debit inventory_batch (batch_id = box_id)
        await conn.execute("""
            UPDATE inventory_batch SET
                current_qty_kg = GREATEST(0, current_qty_kg - $2),
                status = CASE WHEN (current_qty_kg - $2) <= 0 THEN 'ISSUED' ELSE status END,
                updated_at = NOW()
            WHERE batch_id = $1
        """, box_id, net_weight)

        # 6. Create floor_movement
        await conn.execute(
            """
            INSERT INTO floor_movement (
                sku_name, from_location, to_location, quantity_kg,
                reason, job_card_id, scanned_qr_codes, entity
            ) VALUES ($1, $2, 'production_floor', $3, 'production', $4, $5, $6)
            """,
            material_name or box_id, floor_loc, net_weight,
            job_card_id, [box_id], entity,
        )

        accepted.append({
            "box_id": box_id,
            "material": material_name,
            "net_weight": net_weight,
            "lot_number": lot_number,
        })

    # Check if all indents are now fulfilled
    rm_pending = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_rm_indent WHERE job_card_id = $1 AND status != 'fulfilled'",
        job_card_id,
    )
    pm_pending = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_pm_indent WHERE job_card_id = $1 AND status != 'fulfilled'",
        job_card_id,
    )

    all_fulfilled = (rm_pending == 0) and (pm_pending == 0)
    if all_fulfilled and (rm_indents or pm_indents):
        await conn.execute(
            "UPDATE job_card SET status = 'material_received' WHERE job_card_id = $1",
            job_card_id,
        )

    # Build indent summary
    rm_updated = await conn.fetch(
        "SELECT material_sku_name, gross_qty, issued_qty, status FROM job_card_rm_indent WHERE job_card_id = $1",
        job_card_id,
    )
    pm_updated = await conn.fetch(
        "SELECT material_sku_name, gross_qty, issued_qty, status FROM job_card_pm_indent WHERE job_card_id = $1",
        job_card_id,
    )

    indent_summary = []
    for r in list(rm_updated) + list(pm_updated):
        gross = float(r['gross_qty'] or 0)
        issued = float(r['issued_qty'] or 0)
        indent_summary.append({
            "material": r['material_sku_name'],
            "gross_qty": gross,
            "issued_qty": issued,
            "remaining": max(0, gross - issued),
            "status": r['status'],
        })

    total_issued = sum(a['net_weight'] for a in accepted)

    logger.info("QR receipt on JC %d: %d accepted, %d rejected, %.1f kg issued",
                job_card_id, len(accepted), len(rejected), total_issued)

    return {
        "job_card_id": job_card_id,
        "boxes_scanned": len(box_ids),
        "boxes_accepted": len(accepted),
        "boxes_rejected": len(rejected),
        "total_issued_kg": round(total_issued, 3),
        "material_status": "material_received" if all_fulfilled else "partial",
        "indent_summary": indent_summary,
        "accepted_boxes": accepted,
        "rejected_boxes": rejected,
    }


# ══════════════════════════════════════════
#  MANUAL ACKNOWLEDGEMENT (fallback to QR)
# ══════════════════════════════════════════

async def manual_acknowledge_material(conn, job_card_id: int, indent_lines: list[dict],
                                       acknowledged_by: str, entity: str) -> dict:
    """Manually acknowledge material receipt without QR scanning.

    Args:
        indent_lines: [{ indent_type: 'rm'|'pm', indent_id: int }]
        acknowledged_by: name of person acknowledging
    """
    jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}

    # No store approval gate — material acknowledgement is direct

    acknowledged = []
    skipped = []

    for line in indent_lines:
        indent_type = line.get('indent_type', 'rm')
        indent_id = line.get('indent_id')
        if not indent_id:
            continue

        if indent_type == 'rm':
            table = 'job_card_rm_indent'
            pk = 'rm_indent_id'
        else:
            table = 'job_card_pm_indent'
            pk = 'pm_indent_id'

        row = await conn.fetchrow(f"SELECT * FROM {table} WHERE {pk} = $1 AND job_card_id = $2", indent_id, job_card_id)
        if not row:
            skipped.append({"indent_type": indent_type, "indent_id": indent_id, "reason": "not_found"})
            continue

        # Skip already fulfilled lines
        if row['status'] == 'fulfilled':
            skipped.append({"indent_type": indent_type, "indent_id": indent_id, "reason": "already_fulfilled"})
            continue

        # Issue the full required quantity
        issued = float(row['gross_qty'])

        variance = round(issued - float(row['gross_qty']), 3)

        await conn.execute(f"""
            UPDATE {table}
            SET issued_qty = $2, status = 'fulfilled', variance = $3,
                manual_ack_by = $4, manual_ack_at = NOW()
            WHERE {pk} = $1
        """, indent_id, issued, variance, acknowledged_by)

        acknowledged.append({
            "indent_type": indent_type,
            "indent_id": indent_id,
            "material": row['material_sku_name'],
            "issued_qty": issued,
        })

    # Completion check — same as QR
    rm_pending = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_rm_indent WHERE job_card_id = $1 AND status != 'fulfilled'",
        job_card_id,
    )
    pm_pending = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_pm_indent WHERE job_card_id = $1 AND status != 'fulfilled'",
        job_card_id,
    )
    all_fulfilled = (rm_pending == 0) and (pm_pending == 0)

    rm_count = await conn.fetchval("SELECT COUNT(*) FROM job_card_rm_indent WHERE job_card_id = $1", job_card_id)
    pm_count = await conn.fetchval("SELECT COUNT(*) FROM job_card_pm_indent WHERE job_card_id = $1", job_card_id)
    if all_fulfilled and (rm_count > 0 or pm_count > 0):
        await conn.execute(
            "UPDATE job_card SET status = 'material_received' WHERE job_card_id = $1",
            job_card_id,
        )

    logger.info("Manual ack on JC %d: %d acknowledged, %d skipped, by %s",
                job_card_id, len(acknowledged), len(skipped), acknowledged_by)

    return {
        "job_card_id": job_card_id,
        "acknowledged": len(acknowledged),
        "skipped": len(skipped),
        "material_status": "material_received" if all_fulfilled else "partial",
        "acknowledged_lines": acknowledged,
        "skipped_lines": skipped,
    }


async def manual_acknowledge_all(conn, job_card_id: int, acknowledged_by: str, entity: str) -> dict:
    """Acknowledge ALL unfulfilled indent lines for a job card."""
    indent_lines = []

    rm_rows = await conn.fetch(
        "SELECT rm_indent_id FROM job_card_rm_indent WHERE job_card_id = $1 AND status != 'fulfilled'",
        job_card_id,
    )
    for r in rm_rows:
        indent_lines.append({"indent_type": "rm", "indent_id": r['rm_indent_id']})

    pm_rows = await conn.fetch(
        "SELECT pm_indent_id FROM job_card_pm_indent WHERE job_card_id = $1 AND status != 'fulfilled'",
        job_card_id,
    )
    for r in pm_rows:
        indent_lines.append({"indent_type": "pm", "indent_id": r['pm_indent_id']})

    if not indent_lines:
        return {"job_card_id": job_card_id, "acknowledged": 0, "skipped": 0,
                "material_status": "no_eligible_lines", "acknowledged_lines": [], "skipped_lines": []}

    return await manual_acknowledge_material(conn, job_card_id, indent_lines, acknowledged_by, entity)
