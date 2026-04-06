"""Indent & Alert management for MRP shortages.

Flow:
  1. MRP detects shortage → generate_draft_indents() creates draft indents
  2. Planner reviews/edits → edit_indent()
  3. Planner sends → send_indent() transitions draft→raised, creates alerts
  4. Purchase acknowledges → acknowledge_indent()
  5. Purchase links PO → link_indent_to_po()
  6. Material arrives → on_material_received() closes indent, alerts production
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)


async def generate_draft_indents(conn, mrp_result: dict, plan_id: int, entity: str) -> dict:
    """Create draft indents for each MRP shortage. Planner reviews before sending."""

    shortages = [m for m in mrp_result["materials"] if m["status"] == "SHORTAGE"]
    if not shortages:
        return {"indents": [], "total_indents": 0, "total_shortage_kg": 0}

    today_str = date.today().strftime('%Y%m%d')
    indents = []

    for mat in shortages:
        # Generate indent number
        seq = await conn.fetchval(
            "SELECT COUNT(*) + 1 FROM purchase_indent WHERE indent_number LIKE $1",
            f"IND-{today_str}%",
        )
        indent_number = f"IND-{today_str}-{seq:03d}"

        # Get earliest delivery deadline from linked fulfillment
        plan_line = await conn.fetchrow(
            "SELECT linked_so_fulfillment_ids FROM production_plan_line WHERE plan_line_id = $1",
            mat["plan_line_id"],
        )
        deadline = None
        if plan_line and plan_line['linked_so_fulfillment_ids']:
            deadline = await conn.fetchval(
                "SELECT MIN(delivery_deadline) FROM so_fulfillment WHERE fulfillment_id = ANY($1)",
                plan_line['linked_so_fulfillment_ids'],
            )

        indent_id = await conn.fetchval(
            """
            INSERT INTO purchase_indent (
                indent_number, material_sku_name, required_qty_kg, required_by_date,
                priority, plan_line_id, entity, status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'draft')
            RETURNING indent_id
            """,
            indent_number, mat["material_sku_name"], mat["shortage_kg"],
            deadline, 5, mat["plan_line_id"], entity,
        )

        indents.append({
            "indent_id": indent_id,
            "indent_number": indent_number,
            "material_sku_name": mat["material_sku_name"],
            "required_qty_kg": mat["shortage_kg"],
            "required_by_date": str(deadline) if deadline else None,
            "priority": 5,
            "plan_line_id": mat["plan_line_id"],
            "status": "draft",
        })

    total_shortage = sum(m["shortage_kg"] for m in shortages)
    logger.info("Generated %d draft indents for plan %d (%.1f kg total shortage)",
                len(indents), plan_id, total_shortage)

    return {
        "indents": indents,
        "total_indents": len(indents),
        "total_shortage_kg": round(total_shortage, 3),
    }


async def create_indent(conn, *, material_sku_name: str, required_qty_kg: float,
                        entity: str = "cfpl", source: str = "manual",
                        job_card_id=None, so_reference=None, customer_name=None,
                        cascade_reason: str = "Insufficient stock") -> dict:
    """Create and raise a purchase indent directly (from job card / floor manager)."""

    # Ensure entity passes the DB CHECK constraint (must be 'cfpl' or 'cdpl')
    if not entity or entity not in ('cfpl', 'cdpl'):
        entity = 'cfpl'

    today_str = date.today().strftime('%Y%m%d')
    seq = await conn.fetchval(
        "SELECT COUNT(*) + 1 FROM purchase_indent WHERE indent_number LIKE $1",
        f"IND-{today_str}%",
    )
    indent_number = f"IND-{today_str}-{seq:03d}"

    indent_id = await conn.fetchval(
        """
        INSERT INTO purchase_indent (
            indent_number, material_sku_name, required_qty_kg,
            priority, entity, status, indent_source,
            job_card_id, customer_name, cascade_reason
        ) VALUES ($1, $2, $3, $4, $5, 'raised', $6, $7, $8, $9)
        RETURNING indent_id
        """,
        indent_number, material_sku_name, required_qty_kg,
        5, entity, source,
        int(job_card_id) if job_card_id else None,
        customer_name, cascade_reason,
    )

    logger.info("Created indent %s for %s (%.1f kg) entity=%s source=%s",
                indent_number, material_sku_name, required_qty_kg, entity, source)

    return {
        "indent_id": indent_id,
        "indent_number": indent_number,
        "material_sku_name": material_sku_name,
        "required_qty_kg": required_qty_kg,
        "status": "raised",
        "entity": entity,
    }


async def edit_indent(conn, indent_id: int, *,
                       required_qty_kg: float | None = None,
                       required_by_date: date | None = None,
                       priority: int | None = None) -> dict:
    """Edit a draft indent before sending. Only works when status='draft'."""

    indent = await conn.fetchrow(
        "SELECT status FROM purchase_indent WHERE indent_id = $1", indent_id,
    )
    if not indent:
        return {"error": "not_found"}
    if indent['status'] != 'draft':
        return {"error": "not_draft", "message": "Can only edit draft indents"}

    sent_fields = body_fields = []
    updates = []
    params = []
    idx = 1

    if required_qty_kg is not None:
        updates.append(f"required_qty_kg = ${idx}")
        params.append(required_qty_kg)
        sent_fields.append("required_qty_kg")
        idx += 1
    if required_by_date is not None:
        updates.append(f"required_by_date = ${idx}")
        params.append(required_by_date)
        sent_fields.append("required_by_date")
        idx += 1
    if priority is not None:
        updates.append(f"priority = ${idx}")
        params.append(priority)
        sent_fields.append("priority")
        idx += 1

    if not updates:
        return {"error": "no_fields", "message": "No fields to update"}

    params.append(indent_id)
    await conn.execute(
        f"UPDATE purchase_indent SET {', '.join(updates)} WHERE indent_id = ${idx}",
        *params,
    )

    return {"indent_id": indent_id, "updated": True, "fields_changed": sent_fields}


async def send_indent(conn, indent_id: int) -> dict:
    """Send a draft indent → raised. Creates alerts for purchase + stores."""

    indent = await conn.fetchrow(
        "SELECT * FROM purchase_indent WHERE indent_id = $1", indent_id,
    )
    if not indent:
        return {"error": "not_found"}
    if indent['status'] != 'draft':
        return {"error": "not_draft", "message": "Can only send draft indents"}

    # Transition to raised
    await conn.execute(
        "UPDATE purchase_indent SET status = 'raised' WHERE indent_id = $1", indent_id,
    )

    # Get plan line info for alert message
    plan_info = ""
    if indent['plan_line_id']:
        pl = await conn.fetchrow(
            "SELECT fg_sku_name FROM production_plan_line WHERE plan_line_id = $1",
            indent['plan_line_id'],
        )
        if pl:
            plan_info = f" for {pl['fg_sku_name']}"

    material = indent['material_sku_name']
    qty = float(indent['required_qty_kg'])
    deadline = indent['required_by_date']

    # Alert to purchase team
    await conn.execute(
        """
        INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
        VALUES ('material_shortage', 'purchase', $1, $2, 'indent', $3)
        """,
        f"SHORTAGE: {material} — Need {qty:.1f} kg by {deadline}{plan_info}",
        indent_id, indent['entity'],
    )

    # Alert to stores team
    await conn.execute(
        """
        INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
        VALUES ('indent_raised', 'stores', $1, $2, 'indent', $3)
        """,
        f"Indent raised for {material} — {qty:.1f} kg. Check existing stock.",
        indent_id, indent['entity'],
    )

    logger.info("Indent %s sent (raised): %s %.1f kg", indent['indent_number'], material, qty)
    return {
        "indent_id": indent_id,
        "indent_number": indent['indent_number'],
        "status": "raised",
        "alerts_created": 2,
    }


async def send_bulk_indents(conn, indent_ids: list[int]) -> dict:
    """Send multiple draft indents at once."""
    sent = 0
    alerts = 0
    failed = 0

    for iid in indent_ids:
        result = await send_indent(conn, iid)
        if "error" in result:
            failed += 1
        else:
            sent += 1
            alerts += result.get("alerts_created", 0)

    return {"sent": sent, "alerts_created": alerts, "failed": failed}


async def acknowledge_indent(conn, indent_id: int, acknowledged_by: str) -> dict:
    """Purchase team acknowledges indent. raised → acknowledged."""

    indent = await conn.fetchrow(
        "SELECT status FROM purchase_indent WHERE indent_id = $1", indent_id,
    )
    if not indent:
        return {"error": "not_found"}
    if indent['status'] != 'raised':
        return {"error": "invalid_status", "message": "Can only acknowledge raised indents"}

    await conn.execute(
        """
        UPDATE purchase_indent
        SET status = 'acknowledged', acknowledged_by = $2, acknowledged_at = NOW()
        WHERE indent_id = $1
        """,
        indent_id, acknowledged_by,
    )

    row = await conn.fetchrow("SELECT * FROM purchase_indent WHERE indent_id = $1", indent_id)
    return {
        "indent_id": indent_id,
        "status": "acknowledged",
        "acknowledged_by": acknowledged_by,
        "acknowledged_at": str(row['acknowledged_at']),
    }


async def link_indent_to_po(conn, indent_id: int, po_reference: str) -> dict:
    """Link indent to a PO. acknowledged → po_created."""

    indent = await conn.fetchrow(
        "SELECT status FROM purchase_indent WHERE indent_id = $1", indent_id,
    )
    if not indent:
        return {"error": "not_found"}
    if indent['status'] != 'acknowledged':
        return {"error": "invalid_status", "message": "Can only link PO to acknowledged indents"}

    await conn.execute(
        "UPDATE purchase_indent SET status = 'po_created', po_reference = $2 WHERE indent_id = $1",
        indent_id, po_reference,
    )

    return {"indent_id": indent_id, "status": "po_created", "po_reference": po_reference}


async def on_material_received(conn, material_sku: str, received_qty: float, entity: str) -> dict:
    """Called when PO module receives material. Updates indents, creates alerts."""

    # Find matching indents
    indents = await conn.fetch(
        """
        SELECT indent_id, indent_number, material_sku_name, plan_line_id
        FROM purchase_indent
        WHERE material_sku_name ILIKE $1
          AND entity = $2
          AND status IN ('raised', 'acknowledged', 'po_created')
        """,
        f"%{material_sku}%", entity,
    )

    updated_ids = []
    for ind in indents:
        await conn.execute(
            "UPDATE purchase_indent SET status = 'received' WHERE indent_id = $1",
            ind['indent_id'],
        )
        updated_ids.append(ind['indent_id'])

        # Alert production
        await conn.execute(
            """
            INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
            VALUES ('material_received', 'production', $1, $2, 'indent', $3)
            """,
            f"Material received: {material_sku} — {received_qty:.1f} kg. Ready for production.",
            ind['indent_id'], entity,
        )

    # Collect affected plan_line_ids for MRP re-check
    affected_plan_lines = [ind['plan_line_id'] for ind in indents if ind['plan_line_id']]

    logger.info("Material received: %s %.1f kg, %d indents updated", material_sku, received_qty, len(updated_ids))
    return {
        "material": material_sku,
        "received_qty": received_qty,
        "indents_updated": len(updated_ids),
        "affected_plan_line_ids": affected_plan_lines,
    }
