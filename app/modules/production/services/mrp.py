"""MRP (Material Requirements Planning) engine.

Runs against approved production plan lines:
  1. Calculates gross material requirements from BOM (with loss allowance)
  2. Checks off-grade reuse possibilities
  3. Compares against floor_inventory + pending POs
  4. Returns per-material SUFFICIENT / SHORTAGE status
"""

import logging

logger = logging.getLogger(__name__)


async def run_mrp(conn, plan_id: int, entity: str) -> dict:
    """Run MRP for all plan lines in an approved plan.

    Returns per-material availability check with shortage/surplus.
    """
    plan_lines = await conn.fetch(
        "SELECT * FROM production_plan_line WHERE plan_id = $1 AND status = 'planned'",
        plan_id,
    )

    materials = []

    for pl in plan_lines:
        bom_id = pl['bom_id']
        if not bom_id:
            continue

        planned_qty = float(pl['planned_qty_kg'])
        fg_name = pl['fg_sku_name']

        # Get BOM header for item_group (needed for off-grade lookup)
        bom_header = await conn.fetchrow(
            "SELECT item_group FROM bom_header WHERE bom_id = $1", bom_id,
        )
        bom_group = bom_header['item_group'] if bom_header else None

        # Get all BOM lines (RM + PM)
        bom_lines = await conn.fetch(
            "SELECT * FROM bom_line WHERE bom_id = $1", bom_id,
        )

        for bl in bom_lines:
            material_sku = bl['material_sku_name']
            item_type = bl['item_type']
            qty_per_unit = float(bl['quantity_per_unit'])
            loss_pct = float(bl['loss_pct'] or 0)
            can_use_offgrade = bl['can_use_offgrade'] or False
            offgrade_max_pct = float(bl['offgrade_max_pct'] or 0)

            # 1. Gross requirement with loss allowance
            net_need = planned_qty * qty_per_unit
            gross_req = net_need / (1 - loss_pct / 100) if loss_pct < 100 else net_need

            # 2. Off-grade reuse check
            offgrade_avail = 0.0
            offgrade_use = 0.0
            if can_use_offgrade and bom_group:
                # Find reuse rule
                rule = await conn.fetchrow(
                    """
                    SELECT max_substitution_pct FROM offgrade_reuse_rule
                    WHERE target_item_group = $1 AND is_active = TRUE
                    """,
                    bom_group,
                )
                if rule:
                    offgrade_avail_row = await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(available_qty_kg), 0) FROM offgrade_inventory
                        WHERE item_group = $1 AND status = 'available' AND entity = $2
                        """,
                        bom_group, entity,
                    )
                    offgrade_avail = float(offgrade_avail_row or 0)
                    max_allowed = gross_req * float(rule['max_substitution_pct']) / 100
                    offgrade_use = min(offgrade_avail, max_allowed)

            final_need = gross_req - offgrade_use

            # 3. On-hand stock (rm_store + pm_store)
            on_hand = await conn.fetchval(
                """
                SELECT COALESCE(SUM(quantity_kg), 0) FROM floor_inventory
                WHERE sku_name ILIKE $1
                  AND floor_location IN ('rm_store', 'pm_store')
                  AND entity = $2
                """,
                f"%{material_sku}%", entity,
            )
            on_hand = float(on_hand or 0)

            # 4. On-order (pending POs)
            on_order = await conn.fetchval(
                """
                SELECT COALESCE(SUM(l.po_weight), 0) FROM po_line l
                JOIN po_header h ON l.transaction_no = h.transaction_no
                WHERE l.sku_name ILIKE $1 AND h.status = 'pending'
                """,
                f"%{material_sku}%",
            )
            on_order = float(on_order or 0)

            # 5. Result
            available = on_hand + on_order
            shortage = max(0, final_need - available)
            surplus = max(0, available - final_need)
            status = 'SUFFICIENT' if shortage == 0 else 'SHORTAGE'

            materials.append({
                "plan_line_id": pl['plan_line_id'],
                "fg_sku_name": fg_name,
                "material_sku_name": material_sku,
                "item_type": item_type,
                "gross_requirement_kg": round(gross_req, 3),
                "offgrade_available_kg": round(offgrade_avail, 3),
                "offgrade_used_kg": round(offgrade_use, 3),
                "net_requirement_kg": round(final_need, 3),
                "on_hand_kg": round(on_hand, 3),
                "on_order_kg": round(on_order, 3),
                "available_kg": round(available, 3),
                "shortage_kg": round(shortage, 3),
                "surplus_kg": round(surplus, 3),
                "status": status,
            })

    sufficient = sum(1 for m in materials if m['status'] == 'SUFFICIENT')
    shortage_count = sum(1 for m in materials if m['status'] == 'SHORTAGE')
    total_shortage = sum(m['shortage_kg'] for m in materials)

    logger.info("MRP run for plan %d: %d materials, %d sufficient, %d shortage (%.1f kg total)",
                plan_id, len(materials), sufficient, shortage_count, total_shortage)

    return {
        "plan_id": plan_id,
        "materials": materials,
        "summary": {
            "total_materials": len(materials),
            "sufficient": sufficient,
            "shortage": shortage_count,
            "total_shortage_kg": round(total_shortage, 3),
        },
    }


async def check_availability(conn, material_sku: str, qty_needed: float, entity: str) -> dict:
    """Quick single-material availability check."""

    on_hand = await conn.fetchval(
        """
        SELECT COALESCE(SUM(quantity_kg), 0) FROM floor_inventory
        WHERE sku_name ILIKE $1
          AND floor_location IN ('rm_store', 'pm_store')
          AND entity = $2
        """,
        f"%{material_sku}%", entity,
    )
    on_hand = float(on_hand or 0)

    on_order = await conn.fetchval(
        """
        SELECT COALESCE(SUM(l.po_weight), 0) FROM po_line l
        JOIN po_header h ON l.transaction_no = h.transaction_no
        WHERE l.sku_name ILIKE $1 AND h.status = 'pending'
        """,
        f"%{material_sku}%",
    )
    on_order = float(on_order or 0)

    available = on_hand + on_order
    shortage = max(0, qty_needed - available)

    return {
        "material": material_sku,
        "needed_kg": round(qty_needed, 3),
        "on_hand_kg": round(on_hand, 3),
        "on_order_kg": round(on_order, 3),
        "available_kg": round(available, 3),
        "shortage_kg": round(shortage, 3),
        "status": "SUFFICIENT" if shortage == 0 else "SHORTAGE",
    }
