"""Day-End operations — dispatch, balance scan, missing scan alerts.

18. Day-End Dispatch — record dispatch qty, sign-offs, summary
19. Balance Scan — physical count vs system, variance handling, reconciliation
"""

import logging
from datetime import date, datetime, timedelta
from app.webhooks import events

logger = logging.getLogger(__name__)

VARIANCE_THRESHOLD_PCT = 2.0
FLOORS_REQUIRING_SCAN = ['rm_store', 'pm_store', 'production_floor', 'fg_store']


# ---------------------------------------------------------------------------
# 18. Day-End Dispatch
# ---------------------------------------------------------------------------

async def get_day_end_summary(conn, entity: str, target_date: date | None = None) -> dict:
    """Today's completed final-stage job cards with output data."""
    d = target_date or date.today()

    rows = await conn.fetch(
        """
        SELECT jc.job_card_id, jc.job_card_number, jc.fg_sku_name, jc.customer_name,
               jc.batch_number, jc.batch_size_kg, jc.step_number, jc.status,
               o.fg_expected_units, o.fg_actual_units, o.fg_expected_kg, o.fg_actual_kg,
               o.process_loss_kg, o.net_output_kg, o.yield_pct,
               COALESCE(bp.total_bp_kg, 0) AS offgrade_kg,
               jc.dispatched_to_next_kg AS dispatch_qty
        FROM job_card jc
        JOIN production_order po ON jc.prod_order_id = po.prod_order_id
        LEFT JOIN job_card_output o ON jc.job_card_id = o.job_card_id
        LEFT JOIN (
            SELECT job_card_id, SUM(quantity_kg) AS total_bp_kg
            FROM job_card_byproduct GROUP BY job_card_id
        ) bp ON bp.job_card_id = jc.job_card_id
        WHERE jc.entity = $1
          AND jc.step_number = po.total_stages
          AND jc.status IN ('completed', 'closed')
          AND DATE(jc.end_time) = $2
        ORDER BY jc.end_time
        """,
        entity, d,
    )

    items = [dict(r) for r in rows]
    total_fg_kg = sum(float(r.get('fg_actual_kg') or 0) for r in items)
    total_dispatch = sum(float(r.get('dispatch_qty') or 0) for r in items)
    total_loss = sum(float(r.get('process_loss_kg') or 0) for r in items)
    total_offgrade = sum(float(r.get('offgrade_kg') or 0) for r in items)

    return {
        "date": str(d),
        "entity": entity,
        "completed_orders": len(items),
        "total_fg_output_kg": round(total_fg_kg, 3),
        "total_dispatch_kg": round(total_dispatch, 3),
        "total_process_loss_kg": round(total_loss, 3),
        "total_offgrade_kg": round(total_offgrade, 3),
        "items": items,
    }


async def bulk_dispatch(conn, dispatches: list[dict], entity: str) -> dict:
    """Bulk update dispatch quantity for multiple job cards.

    Note: the legacy `job_card_output.dispatch_qty` column was removed by
    migration 011. Dispatch quantity is now tracked on the job_card row itself
    via `dispatched_to_next_kg`. The side-effects below (fulfillment update,
    floor_movement audit, fg_store deduction) are still authoritative.
    """
    updated = 0
    for d in dispatches:
        jc_id = d['job_card_id']
        qty = d['dispatch_qty']

        # Record dispatch qty on the job_card row (replaces the removed
        # job_card_output.dispatch_qty column).
        await conn.execute(
            "UPDATE job_card SET dispatched_to_next_kg = $2 WHERE job_card_id = $1",
            jc_id, qty,
        )

        # Update fulfillment dispatched_qty
        jc = await conn.fetchrow(
            "SELECT prod_order_id, fg_sku_name, entity FROM job_card WHERE job_card_id = $1", jc_id,
        )
        if jc:
            order = await conn.fetchrow(
                "SELECT plan_line_id FROM production_order WHERE prod_order_id = $1", jc['prod_order_id'],
            )
            if order and order['plan_line_id']:
                pl = await conn.fetchrow(
                    "SELECT linked_so_fulfillment_ids FROM production_plan_line WHERE plan_line_id = $1",
                    order['plan_line_id'],
                )
                if pl and pl['linked_so_fulfillment_ids']:
                    for fid in pl['linked_so_fulfillment_ids']:
                        await conn.execute(
                            "UPDATE so_fulfillment SET dispatched_qty_kg = dispatched_qty_kg + $2, updated_at = NOW() WHERE fulfillment_id = $1",
                            fid, qty,
                        )

            # Floor movement: fg_store → dispatched
            await conn.execute(
                """
                INSERT INTO floor_movement (sku_name, from_location, to_location, quantity_kg, reason, job_card_id, entity)
                VALUES ($1, 'fg_store', 'dispatched', $2, 'dispatch', $3, $4)
                """,
                jc['fg_sku_name'], qty, jc_id, entity,
            )

            # Deduct from fg_store
            await conn.execute(
                """
                UPDATE floor_inventory SET quantity_kg = GREATEST(0, quantity_kg - $2), last_updated = NOW()
                WHERE sku_name = $1 AND floor_location = 'fg_store' AND entity = $3
                """,
                jc['fg_sku_name'], qty, entity,
            )

        updated += 1

    logger.info("Day-end dispatch: %d job cards updated", updated)
    return {"updated": updated}


# ---------------------------------------------------------------------------
# 19. Balance Scan
# ---------------------------------------------------------------------------

async def submit_balance_scan(conn, floor_location: str, entity: str,
                               submitted_by: str, scan_lines: list[dict]) -> dict:
    """Submit a day-end balance scan for a floor."""

    today = date.today()

    # Get system quantities for this floor
    sys_items = await conn.fetch(
        "SELECT sku_name, item_type, quantity_kg FROM floor_inventory WHERE floor_location = $1 AND entity = $2 AND quantity_kg > 0",
        floor_location, entity,
    )
    sys_map = {r['sku_name']: float(r['quantity_kg']) for r in sys_items}

    total_system = sum(sys_map.values())
    total_scanned = sum(float(sl.get('scanned_qty_kg', 0)) for sl in scan_lines)
    total_variance = total_scanned - total_system

    # Create scan header
    scan_id = await conn.fetchval(
        """
        INSERT INTO day_end_balance_scan (
            floor_location, scan_date, submitted_by, submitted_at,
            total_system_qty, total_scanned_qty, total_variance, status, entity
        ) VALUES ($1, $2, $3, NOW(), $4, $5, $6, 'submitted', $7)
        ON CONFLICT (floor_location, scan_date, entity) DO UPDATE SET
            submitted_by = $3, submitted_at = NOW(),
            total_system_qty = $4, total_scanned_qty = $5, total_variance = $6, status = 'submitted'
        RETURNING scan_id
        """,
        floor_location, today, submitted_by, total_system, total_scanned, total_variance, entity,
    )

    # Create scan line items
    variance_flags = 0
    for sl in scan_lines:
        sku = sl['sku_name']
        scanned = float(sl.get('scanned_qty_kg', 0))
        system = sys_map.get(sku, 0)
        var_kg = scanned - system
        var_pct = (var_kg / system * 100) if system > 0 else (100 if scanned > 0 else 0)
        line_status = 'ok'

        if abs(var_pct) > VARIANCE_THRESHOLD_PCT:
            line_status = 'variance_detected'
            variance_flags += 1

        await conn.execute(
            """
            INSERT INTO day_end_balance_scan_line (
                scan_id, sku_name, item_type, system_qty_kg, scanned_qty_kg,
                variance_kg, variance_pct, scanned_box_ids, variance_reason, status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            scan_id, sku, sl.get('item_type'), system, scanned,
            round(var_kg, 3), round(var_pct, 3),
            sl.get('scanned_box_ids'), sl.get('variance_reason'),
            line_status,
        )

    # Update scan status if variances found
    if variance_flags > 0:
        await conn.execute(
            "UPDATE day_end_balance_scan SET status = 'variance_flagged' WHERE scan_id = $1", scan_id,
        )
        # Create alert
        await conn.execute(
            """
            INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
            VALUES ('balance_variance', 'stores', $1, $2, 'balance_scan', $3)
            """,
            f"Balance variance on {floor_location}: {variance_flags} item(s) exceed {VARIANCE_THRESHOLD_PCT}% threshold. Review required.",
            scan_id, entity,
        )

    logger.info("Balance scan submitted: floor=%s, %d items, %d variance flags", floor_location, len(scan_lines), variance_flags)
    return {
        "scan_id": scan_id,
        "floor_location": floor_location,
        "scan_date": str(today),
        "items_scanned": len(scan_lines),
        "variance_flags": variance_flags,
        "status": "variance_flagged" if variance_flags > 0 else "submitted",
        "total_system_qty": round(total_system, 3),
        "total_scanned_qty": round(total_scanned, 3),
        "total_variance": round(total_variance, 3),
    }


async def get_scan_status(conn, entity: str, target_date: date | None = None) -> list[dict]:
    """Get today's scan status per floor."""
    d = target_date or date.today()

    results = []
    for floor in FLOORS_REQUIRING_SCAN:
        scan = await conn.fetchrow(
            "SELECT scan_id, status, submitted_by, submitted_at, total_variance FROM day_end_balance_scan WHERE floor_location = $1 AND scan_date = $2 AND entity = $3",
            floor, d, entity,
        )
        results.append({
            "floor_location": floor,
            "scan_date": str(d),
            "submitted": scan is not None,
            "scan_id": scan['scan_id'] if scan else None,
            "status": scan['status'] if scan else "pending",
            "submitted_by": scan['submitted_by'] if scan else None,
            "submitted_at": str(scan['submitted_at']) if scan and scan['submitted_at'] else None,
            "total_variance": float(scan['total_variance']) if scan and scan['total_variance'] else None,
        })

    return results


async def get_scan_detail(conn, scan_id: int) -> dict | None:
    """Get scan detail with all line items."""
    scan = await conn.fetchrow("SELECT * FROM day_end_balance_scan WHERE scan_id = $1", scan_id)
    if not scan:
        return None

    lines = await conn.fetch(
        "SELECT * FROM day_end_balance_scan_line WHERE scan_id = $1 ORDER BY scan_line_id", scan_id,
    )

    result = dict(scan)
    result["lines"] = [dict(l) for l in lines]
    return result


async def reconcile_scan(conn, scan_id: int, reviewed_by: str) -> dict:
    """Reconcile a balance scan — adjust floor_inventory to match physical count."""
    scan = await conn.fetchrow("SELECT * FROM day_end_balance_scan WHERE scan_id = $1", scan_id)
    if not scan:
        return {"error": "not_found"}
    if scan['status'] not in ('submitted', 'variance_flagged'):
        return {"error": "invalid_status", "message": "Can only reconcile submitted/flagged scans"}

    lines = await conn.fetch(
        "SELECT * FROM day_end_balance_scan_line WHERE scan_id = $1 AND status = 'variance_detected'",
        scan_id,
    )

    adjustments = 0
    for line in lines:
        sku = line['sku_name']
        scanned = float(line['scanned_qty_kg'] or 0)
        system = float(line['system_qty_kg'] or 0)
        diff = scanned - system

        if abs(diff) < 0.001:
            continue

        # Adjust floor_inventory
        await conn.execute(
            """
            UPDATE floor_inventory SET quantity_kg = $3, last_updated = NOW()
            WHERE sku_name = $1 AND floor_location = $2 AND entity = $4
            """,
            sku, scan['floor_location'], scanned, scan['entity'],
        )

        # Audit trail
        reason = f"balance_adjustment ({line.get('variance_reason') or 'reconciled'})"
        if diff > 0:
            await conn.execute(
                """
                INSERT INTO floor_movement (sku_name, from_location, to_location, quantity_kg, reason, entity, moved_by)
                VALUES ($1, 'adjustment', $2, $3, $4, $5, $6)
                """,
                sku, scan['floor_location'], abs(diff), reason, scan['entity'], reviewed_by,
            )
        else:
            await conn.execute(
                """
                INSERT INTO floor_movement (sku_name, from_location, to_location, quantity_kg, reason, entity, moved_by)
                VALUES ($1, $2, 'adjustment', $3, $4, $5, $6)
                """,
                sku, scan['floor_location'], abs(diff), reason, scan['entity'], reviewed_by,
            )

        # Update line status
        await conn.execute(
            "UPDATE day_end_balance_scan_line SET status = 'reconciled' WHERE scan_line_id = $1",
            line['scan_line_id'],
        )
        adjustments += 1

    # Update scan header
    await conn.execute(
        "UPDATE day_end_balance_scan SET status = 'reconciled', reviewed_by = $2, reviewed_at = NOW() WHERE scan_id = $1",
        scan_id, reviewed_by,
    )

    # H1: event emits must never fail the business operation.
    try:
        await events.dayend_reconciled(scan['entity'], scan_id=scan_id, floor_location=scan['floor_location'])
    except Exception:
        logger.exception("dayend_reconciled emit failed; swallowing")

    logger.info("Reconciled scan %d: %d adjustments", scan_id, adjustments)
    return {"scan_id": scan_id, "status": "reconciled", "adjustments": adjustments, "reviewed_by": reviewed_by}


async def check_missing_scans(conn, entity: str, target_date: date | None = None) -> dict:
    """Check which floors have NOT submitted balance scans. Create alerts."""
    d = target_date or date.today()
    missing = []

    for floor in FLOORS_REQUIRING_SCAN:
        exists = await conn.fetchval(
            "SELECT COUNT(*) FROM day_end_balance_scan WHERE floor_location = $1 AND scan_date = $2 AND entity = $3",
            floor, d, entity,
        )
        if not exists or exists == 0:
            missing.append(floor)

            # Check if alert already created today
            alert_exists = await conn.fetchval(
                """
                SELECT COUNT(*) FROM store_alert
                WHERE alert_type = 'balance_scan_missing' AND entity = $1
                  AND message ILIKE $2 AND DATE(created_at) = $3
                """,
                entity, f"%{floor}%", d,
            )
            if not alert_exists:
                await conn.execute(
                    """
                    INSERT INTO store_alert (alert_type, target_team, message, entity)
                    VALUES ('balance_scan_missing', 'stores', $1, $2)
                    """,
                    f"ALERT: Day-end balance scan NOT submitted for {floor}. Submit immediately.",
                    entity,
                )
                # Escalate to production
                await conn.execute(
                    """
                    INSERT INTO store_alert (alert_type, target_team, message, entity)
                    VALUES ('balance_scan_missing', 'production', $1, $2)
                    """,
                    f"ESCALATION: Balance scan missing for {floor}. Follow up with stores team.",
                    entity,
                )

    logger.info("Missing scan check: %d of %d floors missing", len(missing), len(FLOORS_REQUIRING_SCAN))
    return {
        "date": str(d),
        "total_floors": len(FLOORS_REQUIRING_SCAN),
        "submitted": len(FLOORS_REQUIRING_SCAN) - len(missing),
        "missing": missing,
        "alerts_created": len(missing) * 2,
    }
