"""Internal Discrepancy Management — report, auto-hold, resolve.

Handles: RM grade mismatch, QC failure, machine breakdown, contamination, short delivery.
Auto-holds affected job cards. 5 resolution paths with audit trail.
"""

import logging
from datetime import datetime
from app.webhooks import events

logger = logging.getLogger(__name__)


async def report_discrepancy(conn, *, discrepancy_type: str, severity: str = "major",
                               affected_material: str | None = None,
                               affected_machine_id: int | None = None,
                               details: str | None = None,
                               reported_by: str | None = None,
                               entity: str) -> dict:
    """Report a discrepancy. Auto-identifies impacted job cards and holds them."""

    # Find affected job cards
    affected_jc_ids = []
    affected_plan_line_ids = []
    total_affected_qty = 0

    if affected_material:
        # Find job cards with this material in their RM/PM indent
        jc_rows = await conn.fetch(
            """
            SELECT DISTINCT jc.job_card_id, jc.job_card_number, jc.status, jc.batch_size_kg,
                   jc.fg_sku_name, jc.customer_name
            FROM job_card jc
            JOIN job_card_rm_indent ri ON jc.job_card_id = ri.job_card_id
            WHERE ri.material_sku_name ILIKE $1
              AND jc.entity = $2
              AND jc.status IN ('unlocked', 'assigned', 'material_received', 'in_progress')
            """,
            f"%{affected_material}%", entity,
        )
        for r in jc_rows:
            affected_jc_ids.append(r['job_card_id'])
            total_affected_qty += float(r['batch_size_kg'] or 0)

    if affected_machine_id:
        mach_rows = await conn.fetch(
            """
            SELECT job_card_id, job_card_number, status, batch_size_kg, fg_sku_name
            FROM job_card
            WHERE machine_id = $1 AND entity = $2
              AND status IN ('unlocked', 'assigned', 'material_received', 'in_progress')
            """,
            affected_machine_id, entity,
        )
        for r in mach_rows:
            if r['job_card_id'] not in affected_jc_ids:
                affected_jc_ids.append(r['job_card_id'])
                total_affected_qty += float(r['batch_size_kg'] or 0)

    # Find affected plan lines
    if affected_jc_ids:
        pl_rows = await conn.fetch(
            """
            SELECT DISTINCT pl.plan_line_id
            FROM production_plan_line pl
            JOIN production_order po ON pl.plan_line_id = po.plan_line_id
            JOIN job_card jc ON po.prod_order_id = jc.prod_order_id
            WHERE jc.job_card_id = ANY($1)
            """,
            affected_jc_ids,
        )
        affected_plan_line_ids = [r['plan_line_id'] for r in pl_rows]

    # Build customer impact
    customers = set()
    if affected_jc_ids:
        cust_rows = await conn.fetch(
            "SELECT DISTINCT customer_name FROM job_card WHERE job_card_id = ANY($1)",
            affected_jc_ids,
        )
        customers = {r['customer_name'] for r in cust_rows if r['customer_name']}

    customer_impact = f"{len(customers)} customer(s): {', '.join(sorted(customers))}" if customers else None

    # Create discrepancy report
    disc_id = await conn.fetchval(
        """
        INSERT INTO discrepancy_report (
            discrepancy_type, severity, affected_material, affected_machine_id,
            affected_job_card_ids, affected_plan_line_ids,
            details, total_affected_qty_kg, customer_impact,
            reported_by, entity, status
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'open')
        RETURNING discrepancy_id
        """,
        discrepancy_type, severity, affected_material, affected_machine_id,
        affected_jc_ids or None, affected_plan_line_ids or None,
        details, total_affected_qty, customer_impact,
        reported_by, entity,
    )

    # Auto-hold affected job cards
    held = 0
    alerted = 0
    for jc_id in affected_jc_ids:
        jc = await conn.fetchrow("SELECT status, job_card_number FROM job_card WHERE job_card_id = $1", jc_id)
        if not jc:
            continue

        if jc['status'] in ('unlocked', 'assigned', 'material_received'):
            await conn.execute(
                "UPDATE job_card SET status = 'locked', is_locked = TRUE, locked_reason = 'discrepancy_hold' WHERE job_card_id = $1",
                jc_id,
            )
            held += 1
        elif jc['status'] == 'in_progress':
            # Don't auto-lock in_progress — alert the team leader
            await conn.execute(
                """
                INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
                VALUES ('internal_discrepancy', 'production', $1, $2, 'job_card', $3)
                """,
                f"STOP: Discrepancy reported ({discrepancy_type}). Job card {jc['job_card_number']} in progress — await instructions.",
                jc_id, entity,
            )
            alerted += 1

    # Create main alert
    await conn.execute(
        """
        INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
        VALUES ('internal_discrepancy', 'production', $1, $2, 'discrepancy', $3)
        """,
        f"DISCREPANCY: {discrepancy_type} ({severity}). "
        f"{affected_material or 'Machine issue'}. "
        f"Impact: {len(affected_jc_ids)} job cards, {total_affected_qty:.0f} kg. "
        f"{customer_impact or ''}",
        disc_id, entity,
    )

    # H1: event emits must never fail the business operation.
    try:
        await events.dayend_discrepancy_found(entity, discrepancy_type=discrepancy_type, severity=severity, affected_material=affected_material, affected_job_cards=len(affected_jc_ids))
    except Exception:
        logger.exception("dayend_discrepancy_found emit failed; swallowing")

    logger.info("Discrepancy %d reported: %s, %d JCs held, %d alerted",
                disc_id, discrepancy_type, held, alerted)

    return {
        "discrepancy_id": disc_id,
        "discrepancy_type": discrepancy_type,
        "severity": severity,
        "affected_job_cards": len(affected_jc_ids),
        "job_cards_held": held,
        "job_cards_alerted": alerted,
        "total_affected_qty_kg": round(total_affected_qty, 3),
        "customer_impact": customer_impact,
        "status": "open",
    }


async def get_discrepancy_detail(conn, discrepancy_id: int) -> dict | None:
    """Get discrepancy detail with affected job cards."""
    disc = await conn.fetchrow("SELECT * FROM discrepancy_report WHERE discrepancy_id = $1", discrepancy_id)
    if not disc:
        return None

    result = dict(disc)

    # Get affected job card details
    if disc['affected_job_card_ids']:
        jcs = await conn.fetch(
            "SELECT job_card_id, job_card_number, fg_sku_name, status, is_locked, locked_reason FROM job_card WHERE job_card_id = ANY($1)",
            disc['affected_job_card_ids'],
        )
        result["affected_job_cards_detail"] = [dict(j) for j in jcs]

    return result


async def resolve_discrepancy(conn, discrepancy_id: int, *,
                                resolution_type: str, resolution_details: str,
                                resolved_by: str, entity: str) -> dict:
    """Resolve a discrepancy. Unlock held job cards."""

    disc = await conn.fetchrow("SELECT * FROM discrepancy_report WHERE discrepancy_id = $1", discrepancy_id)
    if not disc:
        return {"error": "not_found"}
    if disc['status'] == 'resolved':
        return {"error": "already_resolved"}

    # Update discrepancy
    await conn.execute(
        """
        UPDATE discrepancy_report SET
            resolution_type = $2, resolution_details = $3,
            resolved_by = $4, resolved_at = NOW(), status = 'resolved'
        WHERE discrepancy_id = $1
        """,
        discrepancy_id, resolution_type, resolution_details, resolved_by,
    )

    # Unlock held job cards (unless resolution is 'deferred' or 'cancelled_replanned')
    unlocked = 0
    if resolution_type not in ('deferred', 'cancelled_replanned') and disc['affected_job_card_ids']:
        for jc_id in disc['affected_job_card_ids']:
            jc = await conn.fetchrow("SELECT status, locked_reason FROM job_card WHERE job_card_id = $1", jc_id)
            if jc and jc['locked_reason'] == 'discrepancy_hold':
                await conn.execute(
                    "UPDATE job_card SET status = 'unlocked', is_locked = FALSE, locked_reason = NULL WHERE job_card_id = $1",
                    jc_id,
                )
                unlocked += 1

    # If cancelled_replanned, cancel affected job cards and orders
    cancelled = 0
    if resolution_type == 'cancelled_replanned' and disc['affected_job_card_ids']:
        for jc_id in disc['affected_job_card_ids']:
            jc = await conn.fetchrow("SELECT status FROM job_card WHERE job_card_id = $1", jc_id)
            if jc and jc['status'] not in ('completed', 'closed', 'in_progress'):
                await conn.execute(
                    "UPDATE job_card SET status = 'locked', locked_reason = 'cancelled_by_discrepancy' WHERE job_card_id = $1",
                    jc_id,
                )
                cancelled += 1

    # Alert
    await conn.execute(
        """
        INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
        VALUES ('internal_discrepancy', 'production', $1, $2, 'discrepancy', $3)
        """,
        f"Discrepancy #{discrepancy_id} RESOLVED: {resolution_type}. {resolution_details}",
        discrepancy_id, entity,
    )

    logger.info("Discrepancy %d resolved: %s, %d unlocked, %d cancelled",
                discrepancy_id, resolution_type, unlocked, cancelled)

    return {
        "discrepancy_id": discrepancy_id,
        "status": "resolved",
        "resolution_type": resolution_type,
        "job_cards_unlocked": unlocked,
        "job_cards_cancelled": cancelled,
    }
