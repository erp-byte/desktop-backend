"""SO Fulfillment Sync — bridges Sales Orders to Production Planning.

Reads so_header + so_line, creates so_fulfillment records with FY tracking,
supports carryforward and revision with audit logging.
"""

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def _derive_fy(so_date: date) -> str:
    """Derive financial year string from SO date. Apr-Mar → '2025-26'."""
    if so_date.month >= 4:
        return f"{so_date.year}-{str(so_date.year + 1)[2:]}"
    return f"{so_date.year - 1}-{str(so_date.year)[2:]}"


def _derive_entity(company: str | None) -> str | None:
    if not company:
        return None
    c = company.strip().upper()
    if 'CFPL' in c or 'CANDOR FOODS' in c:
        return 'cfpl'
    if 'CDPL' in c:
        return 'cdpl'
    return None


async def sync_fulfillment(conn, entity: str | None = None) -> dict:
    """Sync all FG SO lines into so_fulfillment. Idempotent via UNIQUE constraint."""

    # Find SO lines not yet in so_fulfillment
    query = """
        SELECT h.so_id, h.so_date, h.customer_name, h.company,
               l.so_line_id, l.sku_name, l.quantity, l.quantity_units
        FROM so_header h
        JOIN so_line l ON h.so_id = l.so_id
        WHERE l.so_line_id NOT IN (SELECT so_line_id FROM so_fulfillment)
    """
    params = []
    if entity:
        query += " AND LOWER(h.company) LIKE $1"
        params.append(f"%{entity}%")

    rows = await conn.fetch(query, *params)

    synced = 0
    skipped = 0

    for r in rows:
        so_date = r['so_date']
        if not so_date:
            skipped += 1
            continue

        fy = _derive_fy(so_date)
        ent = _derive_entity(r['company'])
        qty_kg = float(r['quantity']) if r['quantity'] else 0
        deadline = so_date + timedelta(days=7)

        result = await conn.execute(
            """
            INSERT INTO so_fulfillment (
                so_line_id, so_id, financial_year, fg_sku_name, customer_name,
                original_qty_kg, pending_qty_kg, entity, delivery_deadline,
                priority, order_status
            ) VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8, 5, 'open')
            ON CONFLICT (so_line_id, financial_year) DO NOTHING
            """,
            r['so_line_id'], r['so_id'], fy, r['sku_name'], r['customer_name'],
            qty_kg, ent, deadline,
        )
        if result == 'INSERT 0 1':
            synced += 1
        else:
            skipped += 1

    total = synced + skipped
    logger.info("Fulfillment sync: %d synced, %d skipped, %d total", synced, skipped, total)
    return {"synced": synced, "skipped": skipped, "total": total}


async def get_demand_summary(conn, entity: str | None = None, financial_year: str | None = None) -> list[dict]:
    """Aggregate pending demand grouped by product + customer."""

    query = """
        SELECT fg_sku_name, customer_name,
               SUM(pending_qty_kg) AS total_qty_kg,
               COUNT(*) AS order_count,
               MIN(delivery_deadline) AS earliest_deadline
        FROM so_fulfillment
        WHERE order_status IN ('open', 'partial')
    """
    params = []
    idx = 1
    if entity:
        query += f" AND entity = ${idx}"
        params.append(entity)
        idx += 1
    if financial_year:
        query += f" AND financial_year = ${idx}"
        params.append(financial_year)
        idx += 1

    query += " GROUP BY fg_sku_name, customer_name ORDER BY MIN(delivery_deadline), SUM(pending_qty_kg) DESC"

    rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def get_fulfillment_list(conn, *, entity=None, status=None, financial_year=None,
                                customer=None, search=None, page=1, page_size=50) -> dict:
    """Paginated list of fulfillment records with filters."""

    conditions = []
    params = []
    idx = 1

    if entity:
        conditions.append(f"f.entity = ${idx}")
        params.append(entity)
        idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',')]
        placeholders = ', '.join(f'${idx + i}' for i in range(len(statuses)))
        conditions.append(f"f.order_status IN ({placeholders})")
        params.extend(statuses)
        idx += len(statuses)
    if financial_year:
        conditions.append(f"f.financial_year = ${idx}")
        params.append(financial_year)
        idx += 1
    if customer:
        conditions.append(f"f.customer_name ILIKE ${idx}")
        params.append(f"%{customer}%")
        idx += 1
    if search:
        conditions.append(f"(f.fg_sku_name ILIKE ${idx} OR f.customer_name ILIKE ${idx})")
        params.append(f"%{search}%")
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"

    # Count
    count = await conn.fetchval(f"SELECT COUNT(*) FROM so_fulfillment f WHERE {where}", *params)

    # Fetch page
    offset = (page - 1) * page_size
    rows = await conn.fetch(
        f"""
        SELECT f.*, h.so_number, h.so_date
        FROM so_fulfillment f
        LEFT JOIN so_header h ON f.so_id = h.so_id
        WHERE {where}
        ORDER BY f.delivery_deadline ASC, f.priority ASC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params, page_size, offset,
    )

    return {
        "results": [dict(r) for r in rows],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": count,
            "total_pages": (count + page_size - 1) // page_size if count else 0,
        },
    }


async def get_fy_review(conn, entity: str | None = None, financial_year: str | None = None) -> list[dict]:
    """All unfulfilled orders for FY close review, grouped by customer."""

    fy = financial_year
    if not fy:
        today = date.today()
        fy = _derive_fy(today)

    query = """
        SELECT f.*, h.so_number
        FROM so_fulfillment f
        LEFT JOIN so_header h ON f.so_id = h.so_id
        WHERE f.financial_year = $1
          AND f.order_status IN ('open', 'partial')
    """
    params = [fy]
    if entity:
        query += " AND f.entity = $2"
        params.append(entity)

    query += " ORDER BY f.customer_name, f.delivery_deadline"
    rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def carryforward_orders(conn, fulfillment_ids: list[int], new_fy: str, revised_by: str) -> dict:
    """Bulk carry forward selected fulfillment records to a new FY."""

    carried = 0
    for fid in fulfillment_ids:
        old = await conn.fetchrow("SELECT * FROM so_fulfillment WHERE fulfillment_id = $1", fid)
        if not old or old['order_status'] == 'carryforward':
            continue

        # Create new record in new FY
        new_id = await conn.fetchval(
            """
            INSERT INTO so_fulfillment (
                so_line_id, so_id, financial_year, fg_sku_name, customer_name,
                original_qty_kg, pending_qty_kg, entity, delivery_deadline,
                priority, order_status, carryforward_from_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'open', $11)
            ON CONFLICT (so_line_id, financial_year) DO NOTHING
            RETURNING fulfillment_id
            """,
            old['so_line_id'], old['so_id'], new_fy, old['fg_sku_name'], old['customer_name'],
            old['pending_qty_kg'], old['pending_qty_kg'], old['entity'],
            old['delivery_deadline'], old['priority'], fid,
        )

        if new_id:
            # Mark old as carryforward
            await conn.execute(
                "UPDATE so_fulfillment SET order_status = 'carryforward', updated_at = NOW() WHERE fulfillment_id = $1",
                fid,
            )
            # Audit log
            await conn.execute(
                """
                INSERT INTO so_revision_log (fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
                VALUES ($1, 'carryforward', $2, $3, 'FY transition', $4)
                """,
                fid, old['financial_year'], new_fy, revised_by,
            )
            carried += 1

    logger.info("Carryforward: %d of %d orders carried to %s", carried, len(fulfillment_ids), new_fy)
    return {"carried": carried, "total_requested": len(fulfillment_ids), "new_fy": new_fy}


async def revise_order(conn, fulfillment_id: int, *, new_qty: float | None = None,
                       new_date: date | None = None, reason: str = "", revised_by: str = "") -> dict:
    """Revise qty or deadline on a fulfillment record with audit log."""

    old = await conn.fetchrow("SELECT * FROM so_fulfillment WHERE fulfillment_id = $1", fulfillment_id)
    if not old:
        return {"error": "not_found"}

    if new_qty is not None:
        await conn.execute(
            """
            UPDATE so_fulfillment SET revised_qty_kg = $2, pending_qty_kg = $2 - produced_qty_kg, updated_at = NOW()
            WHERE fulfillment_id = $1
            """,
            fulfillment_id, new_qty,
        )
        await conn.execute(
            """
            INSERT INTO so_revision_log (fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
            VALUES ($1, 'qty_change', $2, $3, $4, $5)
            """,
            fulfillment_id, str(float(old['original_qty_kg'])), str(new_qty), reason, revised_by,
        )

    if new_date is not None:
        await conn.execute(
            "UPDATE so_fulfillment SET delivery_deadline = $2, updated_at = NOW() WHERE fulfillment_id = $1",
            fulfillment_id, new_date,
        )
        await conn.execute(
            """
            INSERT INTO so_revision_log (fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
            VALUES ($1, 'date_change', $2, $3, $4, $5)
            """,
            fulfillment_id, str(old['delivery_deadline']), str(new_date), reason, revised_by,
        )

    return {"fulfillment_id": fulfillment_id, "revised": True}
