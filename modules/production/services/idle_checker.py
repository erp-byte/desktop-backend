"""Idle Material Alert — flags materials sitting idle on floors for 3-5 days.

3-day warning, 5-day critical escalation.
Skips materials actively allocated to a job card.
De-duplicates alerts within 24 hours.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

IDLE_WARNING_DAYS = 3
IDLE_CRITICAL_DAYS = 5


async def check_idle_materials(conn, entity: str) -> dict:
    """Check for idle materials on all floors. Create alerts for stores manager."""

    warning_threshold = datetime.utcnow() - timedelta(days=IDLE_WARNING_DAYS)
    critical_threshold = datetime.utcnow() - timedelta(days=IDLE_CRITICAL_DAYS)

    # Find idle materials
    idle_items = await conn.fetch(
        """
        SELECT inventory_id, sku_name, item_type, floor_location, quantity_kg, last_updated
        FROM floor_inventory
        WHERE entity = $1
          AND quantity_kg > 0
          AND floor_location IN ('production_floor', 'rm_store', 'pm_store')
          AND last_updated < $2
        ORDER BY last_updated ASC
        """,
        entity, warning_threshold,
    )

    warnings = 0
    criticals = 0
    skipped_allocated = 0
    skipped_duplicate = 0

    for item in idle_items:
        sku = item['sku_name']
        floor = item['floor_location']
        qty = float(item['quantity_kg'])
        last_update = item['last_updated']

        # Check if material is allocated to an active job card
        allocated = await conn.fetchval(
            """
            SELECT COUNT(*) FROM job_card_rm_indent ri
            JOIN job_card jc ON ri.job_card_id = jc.job_card_id
            WHERE ri.material_sku_name ILIKE $1
              AND jc.status IN ('unlocked', 'assigned', 'material_received', 'in_progress')
              AND jc.entity = $2
            """,
            f"%{sku}%", entity,
        )
        if not allocated:
            allocated = await conn.fetchval(
                """
                SELECT COUNT(*) FROM job_card_pm_indent pi
                JOIN job_card jc ON pi.job_card_id = jc.job_card_id
                WHERE pi.material_sku_name ILIKE $1
                  AND jc.status IN ('unlocked', 'assigned', 'material_received', 'in_progress')
                  AND jc.entity = $2
                """,
                f"%{sku}%", entity,
            )

        if allocated and allocated > 0:
            skipped_allocated += 1
            continue

        # Calculate idle days
        idle_days = (datetime.utcnow() - last_update).days

        # De-duplicate: check if alert exists within last 24h
        recent_alert = await conn.fetchval(
            """
            SELECT COUNT(*) FROM store_alert
            WHERE entity = $1
              AND message ILIKE $2
              AND created_at > NOW() - interval '24 hours'
            """,
            entity, f"%{sku}%{floor}%",
        )
        if recent_alert and recent_alert > 0:
            skipped_duplicate += 1
            continue

        # Create alert
        if idle_days >= IDLE_CRITICAL_DAYS:
            await conn.execute(
                """
                INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
                VALUES ('material_idle_critical', 'stores', $1, $2, 'inventory', $3)
                """,
                f"CRITICAL: {sku} ({qty:.1f} kg) idle on {floor} for {idle_days} days. "
                f"No active job card references this material. Investigate immediately.",
                item['inventory_id'], entity,
            )
            criticals += 1
        else:
            await conn.execute(
                """
                INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
                VALUES ('material_idle_warning', 'stores', $1, $2, 'inventory', $3)
                """,
                f"WARNING: {sku} ({qty:.1f} kg) on {floor} idle for {idle_days} days. "
                f"Consider returning to store or assigning to production.",
                item['inventory_id'], entity,
            )
            warnings += 1

    total_checked = len(idle_items)
    logger.info("Idle check (entity=%s): %d checked, %d warnings, %d criticals, %d allocated (skipped), %d duplicates (skipped)",
                entity, total_checked, warnings, criticals, skipped_allocated, skipped_duplicate)

    return {
        "total_checked": total_checked,
        "warnings": warnings,
        "criticals": criticals,
        "skipped_allocated": skipped_allocated,
        "skipped_duplicate": skipped_duplicate,
        "alerts_created": warnings + criticals,
    }
