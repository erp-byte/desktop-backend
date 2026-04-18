"""SO Fulfillment Sync — bridges Sales Orders to Production Planning.

Reads so_header + so_line, creates so_fulfillment records with FY tracking,
supports carryforward and revision with audit logging.
"""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.webhooks import events

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
        SELECT h.so_id, h.so_number, h.so_date, h.customer_name, h.company,
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
    if synced > 0:
        # H1: event emits must never fail the business operation.
        try:
            await events.fulfillment_synced(entity or "", synced=synced, skipped=skipped, total=total)
        except Exception:
            logger.exception("fulfillment_synced emit failed; swallowing")
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
                                customer=None, so_number=None, article=None,
                                search=None, page=1, page_size=50) -> dict:
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
    # Multi-value aware (comma-separated from frontend multi-select widgets)
    if customer:
        values = [v.strip() for v in str(customer).split(',') if v.strip()]
        if values:
            clauses = []
            for v in values:
                clauses.append(f"f.customer_name ILIKE ${idx}")
                params.append(f"%{v}%")
                idx += 1
            conditions.append("(" + " OR ".join(clauses) + ")")
    if so_number:
        values = [v.strip() for v in str(so_number).split(',') if v.strip()]
        if values:
            placeholders = ', '.join(f'${idx + i}' for i in range(len(values)))
            conditions.append(f"h.so_number IN ({placeholders})")
            params.extend(values)
            idx += len(values)
    if article:
        values = [v.strip() for v in str(article).split(',') if v.strip()]
        if values:
            clauses = []
            for v in values:
                clauses.append(f"f.fg_sku_name ILIKE ${idx}")
                params.append(f"%{v}%")
                idx += 1
            conditions.append("(" + " OR ".join(clauses) + ")")
    if search:
        conditions.append(
            f"(f.fg_sku_name ILIKE ${idx} OR f.customer_name ILIKE ${idx}"
            f" OR h.so_number ILIKE ${idx}"
            f" OR f.entity ILIKE ${idx}"
            f" OR f.order_status ILIKE ${idx}"
            f" OR f.financial_year ILIKE ${idx})"
        )
        params.append(f"%{search}%")
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"

    # Count
    count = await conn.fetchval(
        f"SELECT COUNT(*) FROM so_fulfillment f LEFT JOIN so_header h ON f.so_id = h.so_id WHERE {where}",
        *params,
    )

    # Fetch page
    offset = (page - 1) * page_size
    rows = await conn.fetch(
        f"""
        SELECT f.*, h.so_number AS so_number, h.so_date
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
        SELECT f.*, h.so_number AS so_number
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

    # H1: event emits must never fail the business operation.
    try:
        await events.fulfillment_revised(old['entity'] or "", fulfillment_id=fulfillment_id, new_qty=new_qty, new_date=str(new_date) if new_date else None, revised_by=revised_by)
    except Exception:
        logger.exception("fulfillment_revised emit failed; swallowing")

    return {"fulfillment_id": fulfillment_id, "revised": True}


# ---------------------------------------------------------------------------
# Enriched Customer View helpers
# ---------------------------------------------------------------------------


async def _get_effective_bom_lines(conn, bom_id: int, fulfillment_id: int) -> list[dict]:
    """Return BOM lines merged with any per-fulfillment overrides."""
    rows = await conn.fetch(
        """
        SELECT bl.bom_line_id, bl.line_number,
               COALESCE(o.material_sku_name, bl.material_sku_name) AS material_sku_name,
               bl.item_type,
               COALESCE(o.quantity_per_unit, bl.quantity_per_unit) AS quantity_per_unit,
               COALESCE(o.uom, bl.uom) AS uom,
               COALESCE(o.loss_pct, bl.loss_pct) AS loss_pct,
               COALESCE(o.godown, bl.godown) AS godown,
               COALESCE(o.is_removed, FALSE) AS is_removed,
               CASE WHEN o.override_id IS NOT NULL THEN TRUE ELSE FALSE END AS is_overridden
        FROM bom_line bl
        LEFT JOIN fulfillment_bom_override o
            ON o.bom_line_id = bl.bom_line_id AND o.fulfillment_id = $2
        WHERE bl.bom_id = $1
        ORDER BY bl.line_number
        """,
        bom_id, fulfillment_id,
    )
    return [dict(r) for r in rows if not r['is_removed']]


async def _derive_floor_for_step(conn, stage: str, item_group: str | None,
                                  entity: str, allowed_floors: list | None,
                                  step_index: int) -> str | None:
    """Derive which floor a process step runs on.

    Primary: find a machine with matching (stage, item_group) on an allowed floor.
    Fallback: use positional index into allowed_floors[].
    """
    if allowed_floors:
        row = await conn.fetchrow(
            """
            SELECT DISTINCT m.floor
            FROM machine_capacity mc
            JOIN machine m ON mc.machine_id = m.machine_id
            WHERE mc.stage ILIKE $1
              AND ($2::text IS NULL OR mc.item_group ILIKE $2)
              AND m.entity = $3
              AND m.status = 'active'
              AND m.floor = ANY($4)
            LIMIT 1
            """,
            stage, item_group, entity, allowed_floors,
        )
        if row:
            return row['floor']
        # Fallback: positional
        idx = min(step_index, len(allowed_floors) - 1)
        return allowed_floors[idx]
    return None


async def get_enriched_fulfillment(conn, *, entity: str | None = None,
                                    financial_year: str | None = None,
                                    customer: str | None = None) -> dict:
    """Customer-grouped fulfillment view with BOM, process route, floor mapping, and inventory.

    Fully batched — uses ~8 DB queries total regardless of data volume.
    """

    # 1. Fetch open/partial fulfillments
    conditions = ["f.order_status IN ('open', 'partial')"]
    params = []
    idx = 1
    if entity:
        conditions.append(f"f.entity = ${idx}"); params.append(entity); idx += 1
    if financial_year:
        conditions.append(f"f.financial_year = ${idx}"); params.append(financial_year); idx += 1
    if customer:
        conditions.append(f"f.customer_name ILIKE ${idx}"); params.append(f"%{customer}%"); idx += 1
    where = " AND ".join(conditions)

    fulfillments = await conn.fetch(
        f"""
        SELECT f.fulfillment_id, f.fg_sku_name, f.customer_name, f.pending_qty_kg,
               f.delivery_deadline, f.priority, f.order_status, f.entity,
               h.so_number AS so_number
        FROM so_fulfillment f
        LEFT JOIN so_header h ON f.so_id = h.so_id
        WHERE {where}
        ORDER BY f.customer_name, f.delivery_deadline, f.priority
        """,
        *params,
    )

    if not fulfillments:
        return {"customers": [], "summary": {"total_customers": 0, "total_articles": 0, "materials_with_shortage": 0}}

    ent = entity or (fulfillments[0]['entity'] if fulfillments else 'cfpl')

    # 2. Batch-fetch ALL active BOMs at once, index by fg_sku_name (lowercase)
    all_boms = await conn.fetch(
        "SELECT bom_id, fg_sku_name, process_category, item_group, floors, machines, output_uom "
        "FROM bom_header WHERE is_active = TRUE",
    )
    bom_by_name: dict[str, dict] = {}
    for b in all_boms:
        key = (b['fg_sku_name'] or '').strip().lower()
        if key not in bom_by_name:
            bom_by_name[key] = dict(b)

    # Map fulfillment fg_sku_name → bom
    fg_names = list({r['fg_sku_name'] for r in fulfillments})
    bom_map: dict[str, dict] = {}
    for fg in fg_names:
        key = fg.strip().lower()
        if key in bom_by_name:
            bom_map[fg] = bom_by_name[key]

    # 3. Batch-fetch process routes for all matched BOMs
    bom_ids = list({b['bom_id'] for b in bom_map.values()})
    route_map: dict[int, list[dict]] = {bid: [] for bid in bom_ids}
    if bom_ids:
        routes = await conn.fetch(
            "SELECT bom_id, step_number, process_name, stage, std_time_min, loss_pct "
            "FROM bom_process_route WHERE bom_id = ANY($1) ORDER BY bom_id, step_number",
            bom_ids,
        )
        for r in routes:
            route_map[r['bom_id']].append(dict(r))

    # 4. Batch-fetch BOM lines for all matched BOMs
    bom_lines_map: dict[int, list[dict]] = {bid: [] for bid in bom_ids}
    if bom_ids:
        all_lines = await conn.fetch(
            "SELECT bom_line_id, bom_id, line_number, material_sku_name, item_type, "
            "quantity_per_unit, uom, loss_pct, godown "
            "FROM bom_line WHERE bom_id = ANY($1) ORDER BY bom_id, line_number",
            bom_ids,
        )
        for bl in all_lines:
            bom_lines_map[bl['bom_id']].append(dict(bl))

    # 5. Batch-fetch ALL overrides for all fulfillment_ids in one query
    fids = [f['fulfillment_id'] for f in fulfillments]
    override_map: dict[tuple[int, int], dict] = {}  # (fulfillment_id, bom_line_id) → override
    if fids:
        overrides = await conn.fetch(
            "SELECT fulfillment_id, bom_line_id, material_sku_name, quantity_per_unit, "
            "loss_pct, uom, godown, is_removed "
            "FROM fulfillment_bom_override WHERE fulfillment_id = ANY($1)",
            fids,
        )
        for o in overrides:
            override_map[(o['fulfillment_id'], o['bom_line_id'])] = dict(o)

    # 6. Batch-fetch floor + machine mapping: stage × item_group → floor, machines, factory
    floor_cache: dict[tuple[str, str | None], str | None] = {}
    machine_cache: dict[tuple[str, str | None], list[dict]] = {}
    if bom_ids:
        floor_rows = await conn.fetch(
            "SELECT mc.stage, mc.item_group, m.floor, m.factory, "
            "m.machine_name, mc.capacity_kg_per_hr "
            "FROM machine_capacity mc JOIN machine m ON mc.machine_id = m.machine_id "
            "WHERE m.entity = $1 AND m.status = 'active' "
            "ORDER BY mc.stage, m.floor, m.machine_name",
            ent,
        )
        for r in floor_rows:
            key = ((r['stage'] or '').lower(), (r['item_group'] or '').lower())
            if key not in floor_cache:
                floor_cache[key] = r['floor']
            if key not in machine_cache:
                machine_cache[key] = []
            machine_cache[key].append({
                "machine_name": r['machine_name'],
                "floor": r['floor'],
                "factory": r['factory'],
                "capacity_kg_per_hr": float(r['capacity_kg_per_hr']) if r['capacity_kg_per_hr'] else None,
            })

    # 7. Build articles (pure Python, no more DB calls)
    all_material_skus: set[str] = set()
    articles_by_customer: dict[str, list[dict]] = {}

    for f in fulfillments:
        cust = f['customer_name'] or 'Unknown'
        fg = f['fg_sku_name']
        bom = bom_map.get(fg)
        pending = float(f['pending_qty_kg'] or 0)

        article: dict = {
            "fulfillment_id": f['fulfillment_id'],
            "fg_sku_name": fg,
            "pending_qty_kg": pending,
            "delivery_deadline": str(f['delivery_deadline']) if f['delivery_deadline'] else None,
            "priority": f['priority'],
            "order_status": f['order_status'],
            "so_number": f.get('so_number'),
            "bom_id": bom['bom_id'] if bom else None,
            "bom_found": bom is not None,
            "process_route": [],
            "materials": [],
            "has_overrides": False,
        }

        if bom:
            bid = bom['bom_id']
            allowed_floors = bom.get('floors') or []
            item_group = (bom.get('item_group') or '').lower()

            # Process route with floor + machine mapping (from cache)
            for i, step in enumerate(route_map.get(bid, [])):
                stage_key = ((step['stage'] or '').lower(), item_group)
                floor = floor_cache.get(stage_key)
                if not floor and allowed_floors:
                    floor = allowed_floors[min(i, len(allowed_floors) - 1)]
                machines = machine_cache.get(stage_key, [])
                article["process_route"].append({
                    "step_number": step['step_number'],
                    "process_name": step['process_name'],
                    "stage": step['stage'],
                    "floor": floor,
                    "machines": machines,
                    "std_time_min": float(step['std_time_min']) if step['std_time_min'] else None,
                    "loss_pct": float(step['loss_pct']) if step['loss_pct'] else None,
                })

            # BOM lines merged with overrides (from cache)
            has_override = False
            for bl in bom_lines_map.get(bid, []):
                ov = override_map.get((f['fulfillment_id'], bl['bom_line_id']))
                if ov and ov.get('is_removed'):
                    continue
                mat_name = (ov or {}).get('material_sku_name') or bl['material_sku_name']
                qty_per = float((ov or {}).get('quantity_per_unit') or bl['quantity_per_unit'] or 0)
                loss = float((ov or {}).get('loss_pct') or bl['loss_pct'] or 0)
                uom = (ov or {}).get('uom') or bl['uom']
                is_overridden = ov is not None
                if is_overridden:
                    has_override = True
                gross = (pending * qty_per / (1 - loss / 100)) if loss < 100 else (pending * qty_per)
                all_material_skus.add(mat_name)
                article["materials"].append({
                    "bom_line_id": bl['bom_line_id'],
                    "material_sku_name": mat_name,
                    "item_type": bl['item_type'],
                    "quantity_per_unit": qty_per,
                    "loss_pct": loss,
                    "uom": uom,
                    "gross_requirement_kg": round(gross, 3),
                    "on_hand_kg": 0.0,
                    "status": "UNKNOWN",
                    "is_overridden": is_overridden,
                })
            article["has_overrides"] = has_override

        if cust not in articles_by_customer:
            articles_by_customer[cust] = []
        articles_by_customer[cust].append(article)

    # 8. Batch inventory lookup (single query)
    inv_map: dict[str, float] = {}
    if all_material_skus:
        inv_rows = await conn.fetch(
            """
            SELECT sku_name, COALESCE(SUM(quantity_kg), 0) AS qty
            FROM floor_inventory
            WHERE sku_name = ANY($1) AND entity = $2
            GROUP BY sku_name
            """,
            list(all_material_skus), ent,
        )
        inv_map = {r['sku_name']: float(r['qty']) for r in inv_rows}

    # Distribute inventory + compute status
    shortage_count = 0
    for articles in articles_by_customer.values():
        for art in articles:
            for mat in art['materials']:
                on_hand = inv_map.get(mat['material_sku_name'], 0.0)
                mat['on_hand_kg'] = round(on_hand, 3)
                mat['status'] = 'SUFFICIENT' if on_hand >= mat['gross_requirement_kg'] else 'SHORTAGE'
                if mat['status'] == 'SHORTAGE':
                    shortage_count += 1

    # 9. Build customer-grouped response
    customers = []
    for cust, articles in articles_by_customer.items():
        total_kg = sum(a['pending_qty_kg'] for a in articles)
        earliest = min((a['delivery_deadline'] for a in articles if a['delivery_deadline']), default=None)
        customers.append({
            "customer_name": cust,
            "total_pending_kg": round(total_kg, 3),
            "order_count": len(articles),
            "earliest_deadline": earliest,
            "articles": articles,
        })

    return {
        "customers": customers,
        "summary": {
            "total_customers": len(customers),
            "total_articles": sum(len(c['articles']) for c in customers),
            "materials_with_shortage": shortage_count,
        },
    }


# ---------------------------------------------------------------------------
# BOM Override CRUD
# ---------------------------------------------------------------------------


async def save_bom_overrides(conn, fulfillment_id: int, overrides: list[dict],
                              overridden_by: str = "") -> dict:
    """Save per-fulfillment BOM overrides. Does NOT touch master BOM."""

    # Validate fulfillment
    ful = await conn.fetchrow(
        "SELECT fulfillment_id, fg_sku_name, order_status FROM so_fulfillment WHERE fulfillment_id = $1",
        fulfillment_id,
    )
    if not ful:
        return {"error": "not_found"}
    if ful['order_status'] not in ('open', 'partial'):
        return {"error": "invalid_status", "message": "Can only override BOM for open/partial fulfillments"}

    # Find the BOM for this FG
    bom = await conn.fetchrow(
        "SELECT bom_id FROM bom_header WHERE fg_sku_name ILIKE $1 AND is_active = TRUE LIMIT 1",
        ful['fg_sku_name'],
    )
    if not bom:
        return {"error": "no_bom", "message": f"No active BOM for {ful['fg_sku_name']}"}

    # Validate all bom_line_ids belong to this BOM
    valid_ids = {r['bom_line_id'] for r in await conn.fetch(
        "SELECT bom_line_id FROM bom_line WHERE bom_id = $1", bom['bom_id'],
    )}

    # Clear previously added items (bom_line_id IS NULL) before re-saving
    await conn.execute(
        "DELETE FROM fulfillment_bom_override WHERE fulfillment_id = $1 AND bom_line_id IS NULL",
        fulfillment_id,
    )

    applied = 0
    added = 0
    for ov in overrides:
        blid = ov.get('bom_line_id')

        # Added item (not in original BOM) — bom_line_id is None or negative
        if blid is None or blid < 0:
            mat_name = (ov.get('material_sku_name') or '').strip()
            if not mat_name:
                continue
            await conn.execute(
                """
                INSERT INTO fulfillment_bom_override (
                    fulfillment_id, bom_line_id, material_sku_name, quantity_per_unit,
                    loss_pct, uom, godown, is_removed, override_reason, overridden_by
                ) VALUES ($1, NULL, $2, $3, $4, $5, $6, FALSE, $7, $8)
                """,
                fulfillment_id, mat_name,
                ov.get('quantity_per_unit'), ov.get('loss_pct'),
                ov.get('uom'), ov.get('godown'),
                ov.get('override_reason', 'Added item'), overridden_by,
            )
            added += 1
            continue

        if blid not in valid_ids:
            continue

        await conn.execute(
            """
            INSERT INTO fulfillment_bom_override (
                fulfillment_id, bom_line_id, material_sku_name, quantity_per_unit,
                loss_pct, uom, godown, is_removed, override_reason, overridden_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (fulfillment_id, bom_line_id) DO UPDATE SET
                material_sku_name = $3, quantity_per_unit = $4, loss_pct = $5,
                uom = $6, godown = $7, is_removed = $8,
                override_reason = $9, overridden_by = $10, updated_at = NOW()
            """,
            fulfillment_id, blid,
            ov.get('material_sku_name'), ov.get('quantity_per_unit'),
            ov.get('loss_pct'), ov.get('uom'), ov.get('godown'),
            ov.get('is_removed', False), ov.get('override_reason', ''),
            overridden_by,
        )
        applied += 1

    # Audit log
    total = applied + added
    if total > 0:
        await conn.execute(
            """
            INSERT INTO so_revision_log (fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
            VALUES ($1, 'bom_override', NULL, $2, 'BOM override applied', $3)
            """,
            fulfillment_id,
            f"{applied} lines overridden, {added} items added",
            overridden_by,
        )

    return {"fulfillment_id": fulfillment_id, "overrides_applied": applied}


async def get_bom_overrides(conn, fulfillment_id: int) -> dict:
    """Get current BOM overrides for a fulfillment with master values for comparison."""

    ful = await conn.fetchrow(
        "SELECT fulfillment_id, fg_sku_name FROM so_fulfillment WHERE fulfillment_id = $1",
        fulfillment_id,
    )
    if not ful:
        return {"error": "not_found"}

    bom = await conn.fetchrow(
        "SELECT bom_id FROM bom_header WHERE fg_sku_name ILIKE $1 AND is_active = TRUE LIMIT 1",
        ful['fg_sku_name'],
    )
    if not bom:
        return {"fulfillment_id": fulfillment_id, "bom_found": False, "overrides": []}

    rows = await conn.fetch(
        """
        SELECT bl.bom_line_id, bl.material_sku_name AS master_material,
               bl.quantity_per_unit AS master_qty, bl.loss_pct AS master_loss,
               bl.uom AS master_uom, bl.item_type,
               o.material_sku_name AS override_material,
               o.quantity_per_unit AS override_qty, o.loss_pct AS override_loss,
               o.uom AS override_uom, o.is_removed, o.override_reason, o.overridden_by
        FROM bom_line bl
        LEFT JOIN fulfillment_bom_override o
            ON o.bom_line_id = bl.bom_line_id AND o.fulfillment_id = $2
        WHERE bl.bom_id = $1
        ORDER BY bl.line_number
        """,
        bom['bom_id'], fulfillment_id,
    )

    overrides = []
    for r in rows:
        has_override = (r['override_material'] is not None or r['override_qty'] is not None
                        or r['override_loss'] is not None or r['is_removed'])
        overrides.append({
            "bom_line_id": r['bom_line_id'],
            "item_type": r['item_type'],
            "master": {
                "material_sku_name": r['master_material'],
                "quantity_per_unit": float(r['master_qty']) if r['master_qty'] else None,
                "loss_pct": float(r['master_loss']) if r['master_loss'] else None,
                "uom": r['master_uom'],
            },
            "override": {
                "material_sku_name": r['override_material'],
                "quantity_per_unit": float(r['override_qty']) if r['override_qty'] else None,
                "loss_pct": float(r['override_loss']) if r['override_loss'] else None,
                "uom": r['override_uom'],
                "is_removed": r['is_removed'] or False,
                "reason": r['override_reason'],
            } if has_override else None,
            "has_override": has_override,
        })

    # Fetch added items (bom_line_id IS NULL)
    added_rows = await conn.fetch(
        """
        SELECT override_id, material_sku_name, quantity_per_unit, loss_pct,
               uom, override_reason, overridden_by
        FROM fulfillment_bom_override
        WHERE fulfillment_id = $1 AND bom_line_id IS NULL
        ORDER BY created_at
        """,
        fulfillment_id,
    )
    added_items = []
    for r in added_rows:
        added_items.append({
            "override_id": r['override_id'],
            "material_sku_name": r['material_sku_name'],
            "quantity_per_unit": float(r['quantity_per_unit']) if r['quantity_per_unit'] else None,
            "loss_pct": float(r['loss_pct']) if r['loss_pct'] else None,
            "uom": r['uom'],
        })

    return {
        "fulfillment_id": fulfillment_id, "bom_id": bom['bom_id'],
        "overrides": overrides, "added_items": added_items,
    }


# ---------------------------------------------------------------------------
# Chart Summary + Filter Options
# ---------------------------------------------------------------------------


def _build_chart_where(*, entity=None, financial_year=None, customer=None,
                        so_number=None, article=None, status=None):
    """Shared WHERE builder for chart/filter queries."""
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"f.entity = ${idx}"); params.append(entity); idx += 1
    if financial_year:
        conditions.append(f"f.financial_year = ${idx}"); params.append(financial_year); idx += 1
    if customer:
        conditions.append(f"f.customer_name = ${idx}"); params.append(customer); idx += 1
    if so_number:
        conditions.append(f"h.so_number = ${idx}"); params.append(so_number); idx += 1
    if article:
        conditions.append(f"f.fg_sku_name ILIKE ${idx}"); params.append(f"%{article}%"); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',')]
        placeholders = ', '.join(f'${idx + i}' for i in range(len(statuses)))
        conditions.append(f"f.order_status IN ({placeholders})")
        params.extend(statuses); idx += len(statuses)
    where = " AND ".join(conditions) if conditions else "TRUE"
    return where, params


async def get_chart_summary(conn, *, entity=None, financial_year=None,
                             customer=None, so_number=None, article=None,
                             status=None) -> dict:
    """Aggregated data for dashboard charts."""

    where, params = _build_chart_where(
        entity=entity, financial_year=financial_year, customer=customer,
        so_number=so_number, article=article, status=status,
    )

    base = f"""
        FROM so_fulfillment f
        LEFT JOIN so_header h ON f.so_id = h.so_id
        WHERE {where}
    """

    # 1. Demand by customer (top 15)
    by_customer = await conn.fetch(
        f"SELECT f.customer_name, SUM(f.pending_qty_kg) AS qty, COUNT(*) AS cnt {base}"
        " GROUP BY f.customer_name ORDER BY qty DESC LIMIT 15", *params,
    )

    # 2. Status breakdown
    by_status = await conn.fetch(
        f"SELECT f.order_status, COUNT(*) AS cnt, SUM(f.pending_qty_kg) AS qty {base}"
        " GROUP BY f.order_status ORDER BY cnt DESC", *params,
    )

    # 3. Deadline timeline (by week)
    by_deadline = await conn.fetch(
        f"SELECT DATE_TRUNC('week', f.delivery_deadline) AS week_start,"
        f" COUNT(*) AS cnt, SUM(f.pending_qty_kg) AS qty {base}"
        " AND f.delivery_deadline IS NOT NULL"
        " GROUP BY week_start ORDER BY week_start LIMIT 20", *params,
    )

    # 4. SO-level summary (top 15)
    by_so = await conn.fetch(
        f"SELECT h.so_number, SUM(f.pending_qty_kg) AS qty, COUNT(*) AS cnt {base}"
        " AND h.so_number IS NOT NULL"
        " GROUP BY h.so_number ORDER BY qty DESC LIMIT 15", *params,
    )

    # 5. Summary totals
    totals = await conn.fetchrow(
        f"SELECT COUNT(*) AS total_orders, SUM(f.pending_qty_kg) AS total_qty,"
        f" COUNT(DISTINCT f.fg_sku_name) AS unique_skus,"
        f" COUNT(DISTINCT f.customer_name) AS unique_customers,"
        f" MIN(f.delivery_deadline) AS earliest_deadline {base}", *params,
    )

    return {
        "by_customer": [{"name": r['customer_name'], "qty": float(r['qty'] or 0), "count": r['cnt']} for r in by_customer],
        "by_status": [{"status": r['order_status'], "count": r['cnt'], "qty": float(r['qty'] or 0)} for r in by_status],
        "by_deadline": [{"week": str(r['week_start'])[:10] if r['week_start'] else None, "count": r['cnt'], "qty": float(r['qty'] or 0)} for r in by_deadline],
        "by_so": [{"so_number": r['so_number'], "qty": float(r['qty'] or 0), "count": r['cnt']} for r in by_so],
        "totals": {
            "total_orders": totals['total_orders'],
            "total_qty": float(totals['total_qty'] or 0),
            "unique_skus": totals['unique_skus'],
            "unique_customers": totals['unique_customers'],
            "earliest_deadline": str(totals['earliest_deadline']) if totals['earliest_deadline'] else None,
        },
    }


async def get_filter_options(conn, *, entity=None, financial_year=None,
                              customer=None, so_number=None, article=None) -> dict:
    """Distinct values for filter dropdowns — narrowed to options matching
    the currently-applied SIBLING filters (cross-filter / smart filtering).

    Each returned list excludes its OWN filter from the WHERE clause so the
    user can still see all options for the field they're currently editing,
    while the other two lists narrow based on their current selections.
    """

    def build(include_customer, include_so, include_article):
        conds = []
        pars = []
        i = 1
        if entity:
            conds.append(f"f.entity = ${i}"); pars.append(entity); i += 1
        if financial_year:
            conds.append(f"f.financial_year = ${i}"); pars.append(financial_year); i += 1
        if include_customer and customer:
            values = [v.strip() for v in str(customer).split(',') if v.strip()]
            if values:
                clauses = []
                for v in values:
                    clauses.append(f"f.customer_name ILIKE ${i}")
                    pars.append(f"%{v}%")
                    i += 1
                conds.append("(" + " OR ".join(clauses) + ")")
        if include_so and so_number:
            values = [v.strip() for v in str(so_number).split(',') if v.strip()]
            if values:
                placeholders = ', '.join(f'${i + k}' for k in range(len(values)))
                conds.append(f"h.so_number IN ({placeholders})")
                pars.extend(values); i += len(values)
        if include_article and article:
            values = [v.strip() for v in str(article).split(',') if v.strip()]
            if values:
                clauses = []
                for v in values:
                    clauses.append(f"f.fg_sku_name ILIKE ${i}")
                    pars.append(f"%{v}%")
                    i += 1
                conds.append("(" + " OR ".join(clauses) + ")")
        where_sql = " AND ".join(conds) if conds else "TRUE"
        return where_sql, pars

    base_tbl = "FROM so_fulfillment f LEFT JOIN so_header h ON f.so_id = h.so_id"

    # customers: apply so+article filters (exclude self)
    w_c, p_c = build(include_customer=False, include_so=True, include_article=True)
    customers = await conn.fetch(
        f"SELECT DISTINCT f.customer_name {base_tbl} WHERE {w_c} ORDER BY f.customer_name", *p_c,
    )

    # so_numbers: apply customer+article filters (exclude self)
    w_s, p_s = build(include_customer=True, include_so=False, include_article=True)
    so_numbers = await conn.fetch(
        f"SELECT DISTINCT h.so_number AS so_number {base_tbl}"
        f" WHERE {w_s} AND h.so_number IS NOT NULL ORDER BY so_number",
        *p_s,
    )

    # articles: apply customer+so filters (exclude self)
    w_a, p_a = build(include_customer=True, include_so=True, include_article=False)
    articles = await conn.fetch(
        f"SELECT DISTINCT f.fg_sku_name {base_tbl} WHERE {w_a} ORDER BY f.fg_sku_name", *p_a,
    )

    return {
        "customers": [r['customer_name'] for r in customers],
        "so_numbers": [r['so_number'] for r in so_numbers],
        "articles": [r['fg_sku_name'] for r in articles],
    }


# ---------------------------------------------------------------------------
# Floor Stock Adjustments
# ---------------------------------------------------------------------------


async def get_floor_stock(conn, fulfillment_id: int) -> dict:
    """Get floor stock entries for a fulfillment."""
    ful = await conn.fetchrow(
        "SELECT fulfillment_id FROM so_fulfillment WHERE fulfillment_id = $1",
        fulfillment_id,
    )
    if not ful:
        return {"error": "not_found"}

    rows = await conn.fetch(
        """
        SELECT floor_stock_id, material_sku_name, item_type, quantity_kg,
               unit, floor_location, added_by, notes, created_at
        FROM fulfillment_floor_stock
        WHERE fulfillment_id = $1
        ORDER BY created_at
        """,
        fulfillment_id,
    )
    return {
        "fulfillment_id": fulfillment_id,
        "entries": [
            {
                "floor_stock_id": r['floor_stock_id'],
                "material_sku_name": r['material_sku_name'],
                "item_type": r['item_type'],
                "quantity_kg": float(r['quantity_kg']),
                "unit": r['unit'] or 'KG',
                "floor_location": r['floor_location'],
                "added_by": r['added_by'],
                "notes": r['notes'],
            }
            for r in rows
        ],
    }


async def save_floor_stock(conn, fulfillment_id: int, entries: list[dict],
                            added_by: str = "") -> dict:
    """Save floor stock entries. Replaces all existing entries for this fulfillment."""
    ful = await conn.fetchrow(
        "SELECT fulfillment_id FROM so_fulfillment WHERE fulfillment_id = $1",
        fulfillment_id,
    )
    if not ful:
        return {"error": "not_found"}

    # Clear existing entries for this fulfillment
    await conn.execute(
        "DELETE FROM fulfillment_floor_stock WHERE fulfillment_id = $1",
        fulfillment_id,
    )

    saved = 0
    for e in entries:
        mat = (e.get('material_sku_name') or '').strip()
        qty = e.get('quantity_kg') or 0
        floor = (e.get('floor_location') or '').strip()
        if not mat or not floor or qty <= 0:
            continue
        await conn.execute(
            """
            INSERT INTO fulfillment_floor_stock
                (fulfillment_id, material_sku_name, item_type, quantity_kg, unit, floor_location, added_by, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            fulfillment_id, mat, e.get('item_type', 'pm'), qty,
            e.get('unit', 'KG'), floor, added_by, e.get('notes', ''),
        )
        saved += 1

    return {"fulfillment_id": fulfillment_id, "entries_saved": saved}


async def get_floor_locations(conn, entity: str | None = None) -> dict:
    """Distinct floor locations from machines + floor_inventory.

    Returns:
        {
            "floors": ["Floor 1", "Floor 2", ...],
            "machines_by_floor": {
                "Floor 1": [
                    {"machine_name": "M-A", "machine_type": "Sealer", "has_active_job": false},
                    ...
                ],
                ...
            }
        }
    A machine has has_active_job=true if allocation != 'idle'
    OR if it has a job_card in status 'pending'/'in_progress'.
    """
    # Query 1: active machines with floor assignment
    params: list = []
    entity_clause = ""
    if entity:
        entity_clause = " AND entity = $1"
        params.append(entity)

    machine_rows = await conn.fetch(
        f"SELECT machine_id, floor, machine_name, machine_type, allocation "
        f"FROM machine "
        f"WHERE floor IS NOT NULL AND status = 'active'{entity_clause} "
        f"ORDER BY floor, machine_name",
        *params,
    )

    # Query 2: which of those machines have an active job card
    active_machine_ids: set = set()
    if machine_rows:
        mid_list = [r["machine_id"] for r in machine_rows]
        active_rows = await conn.fetch(
            "SELECT DISTINCT machine_id FROM job_card "
            "WHERE machine_id = ANY($1) AND status IN ('pending', 'in_progress')",
            mid_list,
        )
        active_machine_ids = {r["machine_id"] for r in active_rows}

    # Query 3: floor names from inventory (no machines, just names)
    inv_params: list = []
    inv_q = "SELECT DISTINCT floor_location FROM floor_inventory WHERE floor_location IS NOT NULL"
    if entity:
        inv_q += " AND entity = $1"
        inv_params.append(entity)
    inv_floors_rows = await conn.fetch(inv_q, *inv_params)

    machines_by_floor: dict[str, list] = {}
    machine_floor_names: set[str] = set()

    for r in machine_rows:
        floor = r["floor"]
        machine_floor_names.add(floor)
        if floor not in machines_by_floor:
            machines_by_floor[floor] = []
        allocation = r["allocation"] or "idle"
        has_active_job = (allocation != "idle") or (r["machine_id"] in active_machine_ids)
        machines_by_floor[floor].append({
            "machine_name": r["machine_name"],
            "machine_type": r["machine_type"] or "",
            "has_active_job": has_active_job,
        })

    inv_floor_names = {r["floor_location"] for r in inv_floors_rows}
    all_floors = sorted(machine_floor_names | inv_floor_names)

    return {"floors": all_floors, "machines_by_floor": machines_by_floor}


# ---------------------------------------------------------------------------
# Single-fulfillment detail
# ---------------------------------------------------------------------------

async def get_fulfillment_detail(conn, fulfillment_id: int) -> dict | None:
    """Full detail for one fulfillment: BOM lines + inventory, floor machines, linked SO, logs."""

    def _to_float(v):
        if v is None:
            return None
        if isinstance(v, Decimal):
            return float(v)
        return float(v)

    def _to_str(v):
        if v is None:
            return None
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return str(v)

    def _serialize_row(row):
        out = {}
        for k, v in dict(row).items():
            if isinstance(v, Decimal):
                out[k] = float(v)
            elif isinstance(v, (date, datetime)):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out

    # ------------------------------------------------------------------
    # Query 1 — Fulfillment row + so_number
    # ------------------------------------------------------------------
    row = await conn.fetchrow(
        """
        SELECT sf.*, h.so_number AS so_number
        FROM so_fulfillment sf
        LEFT JOIN so_header h ON sf.so_id = h.so_id
        WHERE sf.fulfillment_id = $1
        """,
        fulfillment_id,
    )
    if row is None:
        return None

    fulfillment = _serialize_row(row)

    # ------------------------------------------------------------------
    # Query 2 — is_planned check
    # ------------------------------------------------------------------
    plan_row = await conn.fetchrow(
        """
        SELECT plan_line_id FROM production_plan_line
        WHERE $1 = ANY(linked_so_fulfillment_ids) LIMIT 1
        """,
        fulfillment_id,
    )
    fulfillment["is_planned"] = plan_row is not None
    fulfillment["plan_line_id"] = plan_row["plan_line_id"] if plan_row else None
    fulfillment["pending_qty_warning"] = not bool(fulfillment.get("pending_qty_kg"))

    # ------------------------------------------------------------------
    # Query 3 — Linked SO line (fetched early so item_type is available for BOM check)
    # ------------------------------------------------------------------
    so_line_id = row.get("so_line_id")
    linked_so = None
    if so_line_id is not None:
        so_row = await conn.fetchrow(
            """
            SELECT h.so_number, h.so_date, h.customer_name, h.voucher_type,
                   l.line_number, l.sku_name, l.quantity, l.rate_inr,
                   l.amount_inr, l.total_amount_inr, l.item_type
            FROM so_line l
            JOIN so_header h ON l.so_id = h.so_id
            WHERE l.so_line_id = $1
            """,
            so_line_id,
        )
        if so_row:
            linked_so = _serialize_row(so_row)

    so_item_type = (linked_so or {}).get("item_type", "")

    # ------------------------------------------------------------------
    # Query 4 — BOM header
    # ------------------------------------------------------------------
    fg_sku = row["fg_sku_name"]
    bom_row = await conn.fetchrow(
        """
        SELECT bom_id, machines, floors, item_group, notes
        FROM bom_header
        WHERE fg_sku_name ILIKE $1 AND is_active = TRUE
        ORDER BY version DESC LIMIT 1
        """,
        fg_sku,
    )

    bom_id = bom_row["bom_id"] if bom_row else None
    bom_machines_raw = bom_row["machines"] if bom_row else []

    # Normalise bom machines to lowercased list for is_in_bom comparison
    bom_machine_names_lower = set()
    if bom_machines_raw:
        for m in bom_machines_raw:
            bom_machine_names_lower.add((m or "").strip().lower())

    process_routes = []
    lines = []
    bom_note = None

    if bom_id is None:
        if (so_item_type or "").lower() == "rm":
            bom_note = "RM SO — no BOM expected"
        else:
            bom_note = "No BOM found"
    else:
        # ------------------------------------------------------------------
        # Query 5 — BOM process routes
        # ------------------------------------------------------------------
        route_rows = await conn.fetch(
            """
            SELECT step_number, process_name, stage, std_time_min, loss_pct, machine_type
            FROM bom_process_route
            WHERE bom_id = $1
            ORDER BY step_number
            """,
            bom_id,
        )
        for r in route_rows:
            process_routes.append({
                "step_number": r["step_number"],
                "process_name": r["process_name"],
                "stage": r["stage"],
                "std_time_min": _to_float(r["std_time_min"]),
                "loss_pct": float(r["loss_pct"]) if r["loss_pct"] is not None else 0.0,
                "machine_type": r["machine_type"],
            })

        # ------------------------------------------------------------------
        # Query 6 — BOM lines merged with overrides
        # ------------------------------------------------------------------
        line_rows = await conn.fetch(
            """
            SELECT
              bl.bom_line_id,
              COALESCE(o.material_sku_name, bl.material_sku_name) AS material_sku_name,
              bl.item_type,
              COALESCE(o.quantity_per_unit, bl.quantity_per_unit) AS quantity_per_unit,
              COALESCE(o.loss_pct, bl.loss_pct) AS loss_pct,
              COALESCE(o.uom, bl.uom) AS uom,
              COALESCE(o.godown, bl.godown) AS godown,
              COALESCE(o.is_removed, FALSE) AS is_removed,
              CASE WHEN o.override_id IS NOT NULL THEN TRUE ELSE FALSE END AS is_overridden,
              o.override_reason,
              bl.process_stage,
              bl.can_use_offgrade
            FROM bom_line bl
            LEFT JOIN fulfillment_bom_override o
              ON o.bom_line_id = bl.bom_line_id AND o.fulfillment_id = $2
            WHERE bl.bom_id = $1
            ORDER BY bl.line_number
            """,
            bom_id,
            fulfillment_id,
        )

        # ------------------------------------------------------------------
        # Query 7 — Added overrides (bom_line_id IS NULL)
        # ------------------------------------------------------------------
        added_rows = await conn.fetch(
            """
            SELECT override_id, material_sku_name, quantity_per_unit, loss_pct, uom, godown,
                   override_reason, is_removed
            FROM fulfillment_bom_override
            WHERE fulfillment_id = $1 AND bom_line_id IS NULL
            ORDER BY created_at
            """,
            fulfillment_id,
        )

        pending_qty_kg = float(row.get("pending_qty_kg") or 0)

        def _build_line(mat_sku, item_type, qty_per_unit, loss_pct_val,
                        uom, godown, is_removed, is_overridden,
                        override_reason, process_stage, can_use_offgrade,
                        bom_line_id=None):
            qty_per = float(qty_per_unit) if qty_per_unit is not None else 0.0
            lp = float(loss_pct_val) if loss_pct_val is not None else 0.0
            if lp < 100:
                gross = pending_qty_kg * qty_per / (1 - lp / 100) if (1 - lp / 100) != 0 else pending_qty_kg * qty_per
                on_hand_kg = 0.0        # filled in after inventory fetch
                inventory_status = "SHORTAGE"  # filled in after
                shortage_kg = 0.0       # filled in after
            else:
                # loss_pct >= 100 means denominator is zero — gross is undefined
                gross = None
                on_hand_kg = 0.0
                inventory_status = "INDETERMINATE"
                shortage_kg = None
            return {
                "bom_line_id": bom_line_id,
                "material_sku_name": mat_sku,
                "item_type": item_type,
                "quantity_per_unit": qty_per,
                "loss_pct": lp,
                "uom": uom,
                "godown": godown,
                "gross_requirement_kg": gross,
                "on_hand_kg": on_hand_kg,
                "inventory_status": inventory_status,
                "shortage_kg": shortage_kg,
                "is_overridden": is_overridden,
                "is_removed": is_removed,
                "override_reason": override_reason,
                "process_stage": process_stage,
                "can_use_offgrade": bool(can_use_offgrade) if can_use_offgrade is not None else False,
            }

        for r in line_rows:
            lines.append(_build_line(
                mat_sku=r["material_sku_name"],
                item_type=r["item_type"],
                qty_per_unit=r["quantity_per_unit"],
                loss_pct_val=r["loss_pct"],
                uom=r["uom"],
                godown=r["godown"],
                is_removed=bool(r["is_removed"]),
                is_overridden=bool(r["is_overridden"]),
                override_reason=r["override_reason"],
                process_stage=r["process_stage"],
                can_use_offgrade=r["can_use_offgrade"],
                bom_line_id=r["bom_line_id"],
            ))

        for r in added_rows:
            lines.append(_build_line(
                mat_sku=r["material_sku_name"],
                item_type=None,
                qty_per_unit=r["quantity_per_unit"],
                loss_pct_val=r["loss_pct"],
                uom=r["uom"],
                godown=r["godown"],
                is_removed=bool(r["is_removed"]),
                is_overridden=True,
                override_reason=r["override_reason"],
                process_stage=None,
                can_use_offgrade=False,
                bom_line_id=None,
            ))

    # ------------------------------------------------------------------
    # Query 8 — Inventory (entity from fulfillment row)
    # ------------------------------------------------------------------
    entity_val = row.get("entity") or "cfpl"
    if not row.get("entity"):
        logger.warning("fulfillment_id=%d has no entity set; defaulting to 'cfpl' for inventory/machine queries", fulfillment_id)
    inv_rows = await conn.fetch(
        """
        SELECT sku_name, SUM(quantity_kg) AS total_kg
        FROM floor_inventory
        WHERE entity = $1 AND quantity_kg > 0
        GROUP BY sku_name
        """,
        entity_val,
    )
    inventory: dict[str, float] = {}
    for inv in inv_rows:
        inventory[(inv["sku_name"] or "").strip().lower()] = float(inv["total_kg"])

    # Match each line by fuzzy name
    for line in lines:
        if line["inventory_status"] == "INDETERMINATE":
            # gross is undefined (loss_pct >= 100) — skip inventory computation
            continue
        mat_lower = (line["material_sku_name"] or "").strip().lower()
        on_hand = 0.0
        for inv_name, inv_qty in inventory.items():
            if inv_name and mat_lower and (inv_name in mat_lower or mat_lower in inv_name):
                on_hand += inv_qty
        gross = line["gross_requirement_kg"]
        line["on_hand_kg"] = round(on_hand, 3)
        line["inventory_status"] = "SUFFICIENT" if on_hand >= gross else "SHORTAGE"
        line["shortage_kg"] = round(max(0.0, gross - on_hand), 3)

    # ------------------------------------------------------------------
    # Query 9 — Floor machines
    # ------------------------------------------------------------------
    machine_rows = await conn.fetch(
        """
        SELECT m.machine_id, m.machine_name, m.machine_type, m.floor,
               m.status, m.allocation,
               array_agg(
                 json_build_object(
                   'stage', mc.stage,
                   'item_group', mc.item_group,
                   'capacity_kg_per_hr', mc.capacity_kg_per_hr
                 )
               ) FILTER (WHERE mc.machine_id IS NOT NULL) AS capacity
        FROM machine m
        LEFT JOIN machine_capacity mc ON m.machine_id = mc.machine_id
        WHERE m.entity = $1
        GROUP BY m.machine_id, m.machine_name, m.machine_type, m.floor, m.status, m.allocation
        ORDER BY m.floor, m.machine_name
        """,
        entity_val,
    )

    floor_machines = []
    for m in machine_rows:
        mname_lower = (m["machine_name"] or "").strip().lower()
        is_in_bom = mname_lower in bom_machine_names_lower if bom_machine_names_lower else False

        # Parse capacity — asyncpg may return list of dicts or JSON strings
        raw_cap = m["capacity"]
        capacity = []
        if raw_cap:
            for cap_item in raw_cap:
                if isinstance(cap_item, str):
                    cap_item = json.loads(cap_item)
                capacity.append({
                    "stage": cap_item.get("stage"),
                    "item_group": cap_item.get("item_group"),
                    "capacity_kg_per_hr": _to_float(cap_item.get("capacity_kg_per_hr")),
                })

        floor_machines.append({
            "machine_id": m["machine_id"],
            "machine_name": m["machine_name"],
            "machine_type": m["machine_type"],
            "floor": m["floor"],
            "status": m["status"],
            "allocation": m["allocation"],
            "is_in_bom": is_in_bom,
            "capacity": capacity,
        })

    # ------------------------------------------------------------------
    # Query 10 — Revision log + floor stock
    # ------------------------------------------------------------------
    # asyncpg connections are single-threaded — asyncio.gather on the same conn
    # raises "another operation is in progress". Run sequentially instead.
    rev_rows = await conn.fetch(
        """
        SELECT * FROM so_revision_log
        WHERE fulfillment_id = $1
        ORDER BY revised_at DESC LIMIT 10
        """,
        fulfillment_id,
    )
    stock_rows = await conn.fetch(
        """
        SELECT * FROM fulfillment_floor_stock
        WHERE fulfillment_id = $1
        ORDER BY created_at
        """,
        fulfillment_id,
    )

    revision_log = [_serialize_row(r) for r in rev_rows]
    floor_stock = [_serialize_row(r) for r in stock_rows]

    return {
        "fulfillment": fulfillment,
        "bom": {
            "bom_id": bom_id,
            "bom_note": bom_note,
            "process_routes": process_routes,
            "lines": lines,
            "floors": list(bom_row["floors"] or []) if bom_row else [],
        },
        "floor_machines": floor_machines,
        "linked_so": linked_so,
        "revision_log": revision_log,
        "floor_stock": floor_stock,
    }
