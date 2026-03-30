"""Floor Inventory State Machine — tracks stock across locations with validated transitions.

Locations: rm_store, pm_store, production_floor, fg_store, offgrade

Allowed transitions:
  rm_store         → production_floor  (job card material receipt)
  pm_store         → production_floor  (packaging stage)
  production_floor → fg_store          (FG output)
  production_floor → offgrade          (off-grade captured)
  production_floor → rm_store          (material return, unused)
  offgrade         → production_floor  (off-grade reuse in another batch)
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)

_ALLOWED_TRANSITIONS = {
    ('rm_store', 'production_floor'),
    ('pm_store', 'production_floor'),
    ('production_floor', 'fg_store'),
    ('production_floor', 'offgrade'),
    ('production_floor', 'rm_store'),
    ('offgrade', 'production_floor'),
}


async def move_material(conn, sku_name: str, from_location: str, to_location: str,
                         qty_kg: float, entity: str, *,
                         reason: str | None = None, job_card_id: int | None = None,
                         moved_by: str | None = None, scanned_qr_codes: list[str] | None = None) -> dict:
    """Move material between floor locations with validation."""

    # Validate transition
    if (from_location, to_location) not in _ALLOWED_TRANSITIONS:
        return {
            "error": "invalid_transition",
            "message": f"Cannot move from '{from_location}' to '{to_location}'. "
                       f"Allowed: {', '.join(f'{a}→{b}' for a, b in sorted(_ALLOWED_TRANSITIONS))}",
        }

    # Check sufficient stock at source
    on_hand = await conn.fetchval(
        """
        SELECT COALESCE(SUM(quantity_kg), 0) FROM floor_inventory
        WHERE sku_name = $1 AND floor_location = $2 AND entity = $3
        """,
        sku_name, from_location, entity,
    )
    on_hand = float(on_hand or 0)
    if on_hand < qty_kg:
        return {
            "error": "insufficient_stock",
            "message": f"Only {on_hand:.3f} kg available at {from_location}, need {qty_kg:.3f} kg",
        }

    # Debit source
    await conn.execute(
        """
        UPDATE floor_inventory SET quantity_kg = quantity_kg - $4, last_updated = NOW()
        WHERE sku_name = $1 AND floor_location = $2 AND entity = $3
        """,
        sku_name, from_location, entity, qty_kg,
    )

    # Credit destination (upsert)
    await conn.execute(
        """
        INSERT INTO floor_inventory (sku_name, floor_location, quantity_kg, entity, last_updated)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (sku_name, floor_location, lot_number, entity)
        DO UPDATE SET quantity_kg = floor_inventory.quantity_kg + $3, last_updated = NOW()
        """,
        sku_name, to_location, qty_kg, entity,
    )

    # Create audit trail
    movement_id = await conn.fetchval(
        """
        INSERT INTO floor_movement (
            sku_name, from_location, to_location, quantity_kg,
            reason, job_card_id, scanned_qr_codes, entity, moved_by
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING movement_id
        """,
        sku_name, from_location, to_location, qty_kg,
        reason, job_card_id, scanned_qr_codes, entity, moved_by,
    )

    logger.info("Moved %.3f kg of '%s' from %s → %s (entity=%s)", qty_kg, sku_name, from_location, to_location, entity)
    return {
        "movement_id": movement_id,
        "sku_name": sku_name,
        "from_location": from_location,
        "to_location": to_location,
        "quantity_kg": qty_kg,
        "moved": True,
    }


async def get_floor_summary(conn, entity: str) -> list[dict]:
    """Aggregated stock per floor location."""
    rows = await conn.fetch(
        """
        SELECT floor_location,
               COUNT(DISTINCT sku_name) AS item_count,
               COALESCE(SUM(quantity_kg), 0) AS total_kg
        FROM floor_inventory
        WHERE entity = $1 AND quantity_kg > 0
        GROUP BY floor_location
        ORDER BY floor_location
        """,
        entity,
    )
    return [{"floor_location": r['floor_location'], "item_count": r['item_count'],
             "total_kg": float(r['total_kg'])} for r in rows]


async def get_floor_detail(conn, floor_location: str, entity: str,
                            search: str | None = None, page: int = 1, page_size: int = 50) -> dict:
    """All items on a specific floor with pagination."""
    conditions = ["floor_location = $1", "entity = $2", "quantity_kg > 0"]
    params = [floor_location, entity]
    idx = 3

    if search:
        conditions.append(f"sku_name ILIKE ${idx}")
        params.append(f"%{search}%")
        idx += 1

    where = " AND ".join(conditions)
    offset = (page - 1) * page_size

    total = await conn.fetchval(f"SELECT COUNT(*) FROM floor_inventory WHERE {where}", *params)
    rows = await conn.fetch(
        f"""
        SELECT * FROM floor_inventory WHERE {where}
        ORDER BY quantity_kg DESC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params, page_size, offset,
    )

    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "total_pages": (total + page_size - 1) // page_size if total else 0},
    }


async def get_movement_history(conn, entity: str, *,
                                sku_name: str | None = None,
                                from_location: str | None = None,
                                to_location: str | None = None,
                                date_from: str | None = None,
                                date_to: str | None = None,
                                job_card_id: int | None = None,
                                page: int = 1, page_size: int = 50) -> dict:
    """Movement audit trail with filters."""
    conditions = ["entity = $1"]
    params = [entity]
    idx = 2

    if sku_name:
        conditions.append(f"sku_name ILIKE ${idx}"); params.append(f"%{sku_name}%"); idx += 1
    if from_location:
        conditions.append(f"from_location = ${idx}"); params.append(from_location); idx += 1
    if to_location:
        conditions.append(f"to_location = ${idx}"); params.append(to_location); idx += 1
    if date_from:
        conditions.append(f"moved_at >= ${idx}::date"); params.append(date_from); idx += 1
    if date_to:
        conditions.append(f"moved_at <= ${idx}::date + interval '1 day'"); params.append(date_to); idx += 1
    if job_card_id:
        conditions.append(f"job_card_id = ${idx}"); params.append(job_card_id); idx += 1

    where = " AND ".join(conditions)
    offset = (page - 1) * page_size

    total = await conn.fetchval(f"SELECT COUNT(*) FROM floor_movement WHERE {where}", *params)
    rows = await conn.fetch(
        f"SELECT * FROM floor_movement WHERE {where} ORDER BY moved_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
        *params, page_size, offset,
    )

    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "total_pages": (total + page_size - 1) // page_size if total else 0},
    }
