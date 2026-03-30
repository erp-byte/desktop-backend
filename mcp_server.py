"""MCP Server for Production Planning — Streamable HTTP transport.

Deploy on Render as a web service. Connect from Claude Desktop via URL.

Tools exposed:
  1. sync_fulfillment — Sync SO lines to fulfillment table
  2. get_planning_context — Get demand, inventory, machines, BOMs for plan generation
  3. get_demand_summary — Aggregated pending demand by product/customer
  4. get_fulfillment_list — Paginated fulfillment records
  5. save_production_plan — Save Claude's generated plan to database
  6. list_plans — List existing production plans
  7. get_plan_detail — Get a plan with all lines

Run locally:  python mcp_server.py
Deploy:       Add to Render as web service, start command: python mcp_server.py
"""

import json
import logging
import os
from datetime import date, timedelta

import asyncpg
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_port = int(os.environ.get("MCP_PORT", os.environ.get("PORT", "8001")))

mcp = FastMCP(
    "Candor Foods Production Planner",
    host="0.0.0.0",
    port=_port,
    streamable_http_path="/",
)

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            # Fallback: load from .env file
            from pathlib import Path
            env_path = Path(__file__).parent / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("DATABASE_URL="):
                        db_url = line.split("=", 1)[1].strip()
                        break
        if not db_url:
            raise RuntimeError("DATABASE_URL not set")
        _pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
    return _pool


# ---------------------------------------------------------------------------
# Tool 1: Sync Fulfillment
# ---------------------------------------------------------------------------

@mcp.tool()
async def sync_fulfillment(entity: str = "") -> str:
    """Sync all FG Sales Order lines into so_fulfillment table.
    Creates fulfillment records for SO lines not yet synced.
    Returns count of synced/skipped records.
    Optional: pass entity='cfpl' or 'cdpl' to filter.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            query = """
                SELECT h.so_id, h.so_date, h.customer_name, h.company,
                       l.so_line_id, l.sku_name, l.quantity
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
            for r in rows:
                so_date = r['so_date']
                if not so_date:
                    continue
                fy = f"{so_date.year}-{str(so_date.year+1)[2:]}" if so_date.month >= 4 else f"{so_date.year-1}-{str(so_date.year)[2:]}"
                company = (r['company'] or '').upper()
                ent = 'cfpl' if 'CFPL' in company or 'CANDOR FOODS' in company else ('cdpl' if 'CDPL' in company else None)
                qty = float(r['quantity']) if r['quantity'] else 0

                await conn.execute(
                    """
                    INSERT INTO so_fulfillment (
                        so_line_id, so_id, financial_year, fg_sku_name, customer_name,
                        original_qty_kg, pending_qty_kg, entity, delivery_deadline, priority, order_status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8, 5, 'open')
                    ON CONFLICT (so_line_id, financial_year) DO NOTHING
                    """,
                    r['so_line_id'], r['so_id'], fy, r['sku_name'], r['customer_name'],
                    qty, ent, so_date + timedelta(days=7),
                )
                synced += 1

    return f"Synced {synced} fulfillment records from {len(rows)} SO lines."


# ---------------------------------------------------------------------------
# Tool 2: Get Planning Context
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_planning_context(entity: str, fulfillment_ids: list[int], target_date: str = "") -> str:
    """Get full planning context for production plan generation.
    Pass the entity ('cfpl' or 'cdpl') and list of fulfillment_ids to plan for.
    Optional target_date (YYYY-MM-DD), defaults to today.

    Returns: demand with BOM details (production vs repackaging),
    inventory (RM/PM/FG stores), machines with capacity, active jobs, pending indents.

    Use this data to create a production schedule, then call save_production_plan with the result.
    """
    pool = await get_pool()
    t_date = target_date or str(date.today())

    async with pool.acquire() as conn:
        # 1. Demand
        demand_rows = await conn.fetch(
            """
            SELECT f.fulfillment_id, f.fg_sku_name, f.customer_name, f.pending_qty_kg,
                   f.delivery_deadline, f.priority
            FROM so_fulfillment f
            WHERE f.fulfillment_id = ANY($1) AND f.order_status IN ('open', 'partial')
            ORDER BY f.delivery_deadline, f.priority
            """,
            fulfillment_ids,
        )

        demand = []
        no_bom = []
        for r in demand_rows:
            bom = await conn.fetchrow(
                "SELECT bom_id, process_category FROM bom_header WHERE fg_sku_name = $1 AND is_active = TRUE LIMIT 1",
                r['fg_sku_name'],
            )
            if not bom:
                no_bom.append({"fulfillment_id": r['fulfillment_id'], "fg_sku_name": r['fg_sku_name']})
                continue

            bom_id = bom['bom_id']
            rm_count = await conn.fetchval("SELECT COUNT(*) FROM bom_line WHERE bom_id = $1 AND item_type = 'rm'", bom_id)
            production_type = 'production' if rm_count > 0 else 'repackaging'

            route_rows = await conn.fetch(
                "SELECT process_name, stage FROM bom_process_route WHERE bom_id = $1 ORDER BY step_number", bom_id,
            )
            process_route = [r2['stage'] for r2 in route_rows]

            mat_rows = await conn.fetch(
                "SELECT material_sku_name, item_type, quantity_per_unit, uom, loss_pct FROM bom_line WHERE bom_id = $1", bom_id,
            )
            qty_kg = float(r['pending_qty_kg'])
            materials = []
            for m in mat_rows:
                need = qty_kg * float(m['quantity_per_unit'])
                loss = float(m['loss_pct'] or 0)
                gross = need / (1 - loss / 100) if loss < 100 else need
                materials.append({
                    "name": m['material_sku_name'], "type": m['item_type'],
                    "need_qty": round(gross, 3), "uom": m['uom'], "loss_pct": loss,
                })

            demand.append({
                "fulfillment_id": r['fulfillment_id'], "fg_sku_name": r['fg_sku_name'],
                "customer": r['customer_name'], "qty_kg": qty_kg,
                "deadline": str(r['delivery_deadline']), "priority": r['priority'],
                "production_type": production_type, "bom_id": bom_id,
                "process_route": process_route or ['packaging'], "materials": materials,
            })

        # 2. Inventory
        inv_rows = await conn.fetch(
            "SELECT sku_name, item_type, floor_location, SUM(quantity_kg) as qty_kg "
            "FROM floor_inventory WHERE entity = $1 GROUP BY sku_name, item_type, floor_location", entity,
        )
        inventory = {"rm_store": [], "pm_store": [], "fg_store": []}
        for r in inv_rows:
            loc = r['floor_location']
            if loc in inventory:
                inventory[loc].append({"sku": r['sku_name'], "qty_kg": float(r['qty_kg'])})

        # 3. Machines + capacity
        machine_rows = await conn.fetch(
            """
            SELECT m.machine_id, m.machine_name, m.floor, m.allocation,
                   mc.stage, mc.item_group, mc.capacity_kg_per_hr
            FROM machine m LEFT JOIN machine_capacity mc ON m.machine_id = mc.machine_id
            WHERE m.entity = $1 AND m.status = 'active' ORDER BY m.machine_name
            """, entity,
        )
        machines_map = {}
        for r in machine_rows:
            mid = r['machine_id']
            if mid not in machines_map:
                machines_map[mid] = {"name": r['machine_name'], "floor": r['floor'], "allocation": r['allocation'], "capacity": []}
            if r['stage']:
                machines_map[mid]["capacity"].append({"group": r['item_group'], "stage": r['stage'], "kg_hr": float(r['capacity_kg_per_hr'])})

        # 4. Active jobs
        jobs = await conn.fetch(
            "SELECT job_card_number, fg_sku_name, stage, status, batch_size_kg FROM job_card WHERE entity = $1 AND status IN ('in_progress','unlocked','assigned')",
            entity,
        )

        # 5. Pending indents
        indents = await conn.fetch(
            "SELECT material_sku_name, required_qty_kg, required_by_date, status FROM purchase_indent WHERE entity = $1 AND status IN ('raised','acknowledged')",
            entity,
        )

    context = {
        "date": t_date, "entity": entity,
        "demand": demand, "no_bom_items": no_bom,
        "inventory": inventory, "machines": list(machines_map.values()),
        "in_progress_jobs": [dict(j) for j in jobs],
        "pending_indents": [dict(i) for i in indents],
    }
    return json.dumps(context, default=str, indent=2)


# ---------------------------------------------------------------------------
# Tool 3: Demand Summary
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_demand_summary(entity: str = "", financial_year: str = "") -> str:
    """Get aggregated pending demand grouped by product and customer.
    Shows total qty, order count, and earliest deadline per product-customer combo.
    Use this to see what needs to be planned.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        query = """
            SELECT fg_sku_name, customer_name,
                   SUM(pending_qty_kg) AS total_qty_kg, COUNT(*) AS order_count,
                   MIN(delivery_deadline) AS earliest_deadline
            FROM so_fulfillment WHERE order_status IN ('open', 'partial')
        """
        params = []
        idx = 1
        if entity:
            query += f" AND entity = ${idx}"; params.append(entity); idx += 1
        if financial_year:
            query += f" AND financial_year = ${idx}"; params.append(financial_year); idx += 1
        query += " GROUP BY fg_sku_name, customer_name ORDER BY MIN(delivery_deadline)"
        rows = await conn.fetch(query, *params)

    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ---------------------------------------------------------------------------
# Tool 4: Fulfillment List
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_fulfillment_list(entity: str = "", status: str = "open,partial", page: int = 1, page_size: int = 50) -> str:
    """Get paginated list of fulfillment records.
    Filter by entity ('cfpl'/'cdpl') and status ('open','partial','fulfilled','carryforward','cancelled').
    Returns fulfillment_id, fg_sku_name, customer, qty, deadline, priority — use fulfillment_ids for planning.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1
        if entity:
            conditions.append(f"f.entity = ${idx}"); params.append(entity); idx += 1
        if status:
            statuses = [s.strip() for s in status.split(',')]
            ph = ', '.join(f'${idx+i}' for i in range(len(statuses)))
            conditions.append(f"f.order_status IN ({ph})"); params.extend(statuses); idx += len(statuses)

        where = " AND ".join(conditions) if conditions else "TRUE"
        count = await conn.fetchval(f"SELECT COUNT(*) FROM so_fulfillment f WHERE {where}", *params)
        offset = (page - 1) * page_size
        rows = await conn.fetch(
            f"""SELECT f.fulfillment_id, f.fg_sku_name, f.customer_name, f.pending_qty_kg,
                       f.delivery_deadline, f.priority, f.order_status, f.financial_year
                FROM so_fulfillment f WHERE {where}
                ORDER BY f.delivery_deadline, f.priority
                LIMIT ${idx} OFFSET ${idx+1}""",
            *params, page_size, offset,
        )

    return json.dumps({"total": count, "page": page, "results": [dict(r) for r in rows]}, default=str, indent=2)


# ---------------------------------------------------------------------------
# Tool 5: Save Production Plan
# ---------------------------------------------------------------------------

@mcp.tool()
async def save_production_plan(
    entity: str,
    plan_type: str,
    date_from: str,
    date_to: str,
    schedule_json: str,
    material_check_json: str = "[]",
    risk_flags_json: str = "[]",
) -> str:
    """Save a production plan generated by Claude to the database.

    Args:
        entity: 'cfpl' or 'cdpl'
        plan_type: 'daily' or 'weekly'
        date_from: Plan start date (YYYY-MM-DD)
        date_to: Plan end date (YYYY-MM-DD)
        schedule_json: JSON array of schedule items. Each item must have:
            fg_sku_name, customer_name, qty_kg, qty_units, bom_id,
            production_type, machine_name, priority, shift,
            stage_sequence (array), estimated_hours, linked_fulfillment_ids (array), reasoning
        material_check_json: JSON array of material check results
        risk_flags_json: JSON array of risk flags

    Returns: plan_id and line count.
    """
    schedule = json.loads(schedule_json)
    material_check = json.loads(material_check_json)
    risk_flags = json.loads(risk_flags_json)

    full_analysis = {"schedule": schedule, "material_check": material_check, "risk_flags": risk_flags}

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            plan_id = await conn.fetchval(
                """
                INSERT INTO production_plan (
                    plan_name, entity, plan_type, plan_date, date_from, date_to,
                    status, ai_generated, ai_analysis_json
                ) VALUES ($1, $2, $3, $4::date, $5::date, $6::date, 'draft', TRUE, $7)
                RETURNING plan_id
                """,
                f"{plan_type.title()} Plan — {date_from}",
                entity, plan_type, date_from, date_from, date_to,
                json.dumps(full_analysis, default=str),
            )

            # Machine lookup
            machines = await conn.fetch("SELECT machine_id, machine_name FROM machine WHERE entity = $1", entity)
            machine_lookup = {r['machine_name'].strip().lower(): r['machine_id'] for r in machines}

            lines_created = 0
            for item in schedule:
                fg_name = item.get("fg_sku_name", "")
                machine_name = item.get("machine_name", "")
                machine_id = machine_lookup.get(machine_name.strip().lower())

                bom_id = item.get("bom_id")
                if not bom_id:
                    bom_id = await conn.fetchval(
                        "SELECT bom_id FROM bom_header WHERE fg_sku_name = $1 AND is_active = TRUE LIMIT 1", fg_name,
                    )

                stage_seq = item.get("stage_sequence", [])
                linked_ids = item.get("linked_fulfillment_ids", [])

                await conn.execute(
                    """
                    INSERT INTO production_plan_line (
                        plan_id, fg_sku_name, customer_name, bom_id,
                        planned_qty_kg, planned_qty_units, machine_id,
                        priority, shift, stage_sequence, estimated_hours,
                        linked_so_fulfillment_ids, reasoning
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    """,
                    plan_id, fg_name, item.get("customer_name"),
                    bom_id, item.get("qty_kg", 0), item.get("qty_units"),
                    machine_id, item.get("priority", 5), item.get("shift", "day"),
                    stage_seq or None, item.get("estimated_hours"),
                    linked_ids or None, item.get("reasoning"),
                )
                lines_created += 1

    return f"Plan saved! plan_id={plan_id}, {lines_created} lines created. Status: draft (awaiting approval)."


# ---------------------------------------------------------------------------
# Tool 6: List Plans
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_plans(entity: str = "", status: str = "", plan_type: str = "") -> str:
    """List existing production plans. Filter by entity, status (draft/approved/cancelled), plan_type (daily/weekly)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1
        if entity:
            conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
        if status:
            conditions.append(f"status = ${idx}"); params.append(status); idx += 1
        if plan_type:
            conditions.append(f"plan_type = ${idx}"); params.append(plan_type); idx += 1
        where = " AND ".join(conditions) if conditions else "TRUE"

        rows = await conn.fetch(
            f"""SELECT plan_id, plan_name, entity, plan_type, plan_date, date_from, date_to,
                       status, ai_generated, revision_number, created_at
                FROM production_plan WHERE {where} ORDER BY created_at DESC LIMIT 20""",
            *params,
        )
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ---------------------------------------------------------------------------
# Tool 7: Plan Detail
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_plan_detail(plan_id: int) -> str:
    """Get a production plan with all its lines, material check, and risk flags."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT * FROM production_plan WHERE plan_id = $1", plan_id)
        if not plan:
            return "Plan not found."
        lines = await conn.fetch(
            "SELECT * FROM production_plan_line WHERE plan_id = $1 ORDER BY priority", plan_id,
        )

    result = dict(plan)
    result["lines"] = [dict(l) for l in lines]
    ai_json = result.get("ai_analysis_json")
    if ai_json:
        if isinstance(ai_json, str):
            ai_json = json.loads(ai_json)
        result["material_check"] = ai_json.get("material_check", [])
        result["risk_flags"] = ai_json.get("risk_flags", [])

    return json.dumps(result, default=str, indent=2)


# ---------------------------------------------------------------------------
# Part 2 additions: Fulfillment & Plan Management
# ---------------------------------------------------------------------------


@mcp.tool()
async def fy_review(entity: str = "", financial_year: str = "") -> str:
    """Get all unfulfilled orders for FY close review. Shows orders that are open/partial for the financial year."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        fy = financial_year
        if not fy:
            from datetime import date as d
            today = d.today()
            fy = f"{today.year}-{str(today.year+1)[2:]}" if today.month >= 4 else f"{today.year-1}-{str(today.year)[2:]}"
        query = "SELECT f.*, h.so_number FROM so_fulfillment f LEFT JOIN so_header h ON f.so_id = h.so_id WHERE f.financial_year = $1 AND f.order_status IN ('open','partial')"
        params = [fy]
        if entity:
            query += " AND f.entity = $2"
            params.append(entity)
        query += " ORDER BY f.customer_name, f.delivery_deadline"
        rows = await conn.fetch(query, *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def carryforward_orders(fulfillment_ids: list[int], new_fy: str, revised_by: str = "") -> str:
    """Bulk carry forward selected fulfillment records to a new financial year. Old records get status='carryforward', new records created in new FY."""
    pool = await get_pool()
    carried = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for fid in fulfillment_ids:
                old = await conn.fetchrow("SELECT * FROM so_fulfillment WHERE fulfillment_id = $1", fid)
                if not old or old['order_status'] == 'carryforward':
                    continue
                new_id = await conn.fetchval(
                    "INSERT INTO so_fulfillment (so_line_id, so_id, financial_year, fg_sku_name, customer_name, original_qty_kg, pending_qty_kg, entity, delivery_deadline, priority, order_status, carryforward_from_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open',$11) ON CONFLICT (so_line_id, financial_year) DO NOTHING RETURNING fulfillment_id",
                    old['so_line_id'], old['so_id'], new_fy, old['fg_sku_name'], old['customer_name'], old['pending_qty_kg'], old['pending_qty_kg'], old['entity'], old['delivery_deadline'], old['priority'], fid)
                if new_id:
                    await conn.execute("UPDATE so_fulfillment SET order_status='carryforward', updated_at=NOW() WHERE fulfillment_id=$1", fid)
                    await conn.execute("INSERT INTO so_revision_log (fulfillment_id, revision_type, old_value, new_value, reason, revised_by) VALUES ($1,'carryforward',$2,$3,'FY transition',$4)", fid, old['financial_year'], new_fy, revised_by)
                    carried += 1
    return f"Carried forward {carried} of {len(fulfillment_ids)} orders to {new_fy}."


@mcp.tool()
async def revise_fulfillment(fulfillment_id: int, new_qty: float = 0, new_date: str = "", reason: str = "", revised_by: str = "") -> str:
    """Revise a fulfillment record's quantity and/or delivery deadline. Creates audit trail."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow("SELECT * FROM so_fulfillment WHERE fulfillment_id = $1", fulfillment_id)
            if not old:
                return "Fulfillment record not found."
            if new_qty > 0:
                await conn.execute("UPDATE so_fulfillment SET revised_qty_kg=$2, pending_qty_kg=$2-produced_qty_kg, updated_at=NOW() WHERE fulfillment_id=$1", fulfillment_id, new_qty)
                await conn.execute("INSERT INTO so_revision_log (fulfillment_id, revision_type, old_value, new_value, reason, revised_by) VALUES ($1,'qty_change',$2,$3,$4,$5)", fulfillment_id, str(float(old['original_qty_kg'])), str(new_qty), reason, revised_by)
            if new_date:
                await conn.execute("UPDATE so_fulfillment SET delivery_deadline=$2::date, updated_at=NOW() WHERE fulfillment_id=$1", fulfillment_id, new_date)
                await conn.execute("INSERT INTO so_revision_log (fulfillment_id, revision_type, old_value, new_value, reason, revised_by) VALUES ($1,'date_change',$2,$3,$4,$5)", fulfillment_id, str(old['delivery_deadline']), new_date, reason, revised_by)
    return f"Fulfillment {fulfillment_id} revised."


@mcp.tool()
async def cancel_fulfillment(fulfillment_ids: list[int], reason: str, cancelled_by: str = "") -> str:
    """Cancel selected fulfillment records with reason. Creates audit trail."""
    pool = await get_pool()
    cancelled = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for fid in fulfillment_ids:
                old = await conn.fetchrow("SELECT order_status FROM so_fulfillment WHERE fulfillment_id=$1", fid)
                if not old or old['order_status'] in ('cancelled','fulfilled'):
                    continue
                await conn.execute("UPDATE so_fulfillment SET order_status='cancelled', updated_at=NOW() WHERE fulfillment_id=$1", fid)
                await conn.execute("INSERT INTO so_revision_log (fulfillment_id, revision_type, old_value, new_value, reason, revised_by) VALUES ($1,'cancel',$2,'cancelled',$3,$4)", fid, old['order_status'], reason, cancelled_by)
                cancelled += 1
    return f"Cancelled {cancelled} of {len(fulfillment_ids)} orders."


@mcp.tool()
async def create_manual_plan(entity: str, plan_name: str, plan_type: str = "daily", date_from: str = "", date_to: str = "") -> str:
    """Create an empty manual production plan (no AI). Add lines manually after creation."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan_id = await conn.fetchval(
            "INSERT INTO production_plan (plan_name, entity, plan_type, plan_date, date_from, date_to, status, ai_generated) VALUES ($1,$2,$3,$4::date,$5::date,$6::date,'draft',FALSE) RETURNING plan_id",
            plan_name, entity, plan_type, date_from, date_from, date_to or date_from)
    return json.dumps({"plan_id": plan_id, "status": "draft"})


@mcp.tool()
async def edit_plan_line(plan_id: int, line_id: int, planned_qty_kg: float = 0, machine_id: int = 0, priority: int = 0, shift: str = "", reasoning: str = "") -> str:
    """Edit a specific plan line (only while plan is draft). Only non-zero/non-empty fields are updated."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan:
            return "Plan not found."
        if plan['status'] != 'draft':
            return "Can only edit draft plans."
        updates, params, idx = [], [], 1
        if planned_qty_kg > 0:
            updates.append(f"planned_qty_kg=${idx}"); params.append(planned_qty_kg); idx += 1
        if machine_id > 0:
            updates.append(f"machine_id=${idx}"); params.append(machine_id); idx += 1
        if priority > 0:
            updates.append(f"priority=${idx}"); params.append(priority); idx += 1
        if shift:
            updates.append(f"shift=${idx}"); params.append(shift); idx += 1
        if reasoning:
            updates.append(f"reasoning=${idx}"); params.append(reasoning); idx += 1
        if not updates:
            return "No fields to update."
        params.extend([line_id, plan_id])
        await conn.execute(f"UPDATE production_plan_line SET {','.join(updates)} WHERE plan_line_id=${idx} AND plan_id=${idx+1}", *params)
    return f"Plan line {line_id} updated."


@mcp.tool()
async def add_plan_line(plan_id: int, fg_sku_name: str, planned_qty_kg: float, customer_name: str = "", priority: int = 5, shift: str = "day") -> str:
    """Add a manual line to a draft plan."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status, entity FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan or plan['status'] != 'draft':
            return "Plan not found or not in draft status."
        bom_id = await conn.fetchval("SELECT bom_id FROM bom_header WHERE fg_sku_name=$1 AND is_active=TRUE LIMIT 1", fg_sku_name)
        line_id = await conn.fetchval(
            "INSERT INTO production_plan_line (plan_id, fg_sku_name, customer_name, bom_id, planned_qty_kg, priority, shift) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING plan_line_id",
            plan_id, fg_sku_name, customer_name, bom_id, planned_qty_kg, priority, shift)
    return json.dumps({"plan_line_id": line_id, "plan_id": plan_id})


@mcp.tool()
async def delete_plan_line(plan_id: int, line_id: int) -> str:
    """Remove a line from a draft plan."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan or plan['status'] != 'draft':
            return "Plan not found or not in draft status."
        result = await conn.execute("DELETE FROM production_plan_line WHERE plan_line_id=$1 AND plan_id=$2", line_id, plan_id)
        if result == 'DELETE 0':
            return "Line not found."
    return f"Line {line_id} deleted from plan {plan_id}."


@mcp.tool()
async def approve_plan(plan_id: int, approved_by: str) -> str:
    """Approve a draft plan. Triggers MRP run and creates draft indents for material shortages."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status, entity FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan:
            return "Plan not found."
        if plan['status'] != 'draft':
            return "Only draft plans can be approved."
        async with conn.transaction():
            await conn.execute("UPDATE production_plan SET status='approved', approved_by=$2, approved_at=NOW() WHERE plan_id=$1", plan_id, approved_by)
            # Import and run MRP
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.mrp import run_mrp
            from app.modules.production.services.indent_manager import generate_draft_indents
            mrp_result = await run_mrp(conn, plan_id, plan['entity'])
            draft_result = await generate_draft_indents(conn, mrp_result, plan_id, plan['entity'])
    return json.dumps({"plan_id": plan_id, "status": "approved", "approved_by": approved_by, "mrp_summary": mrp_result["summary"], "draft_indents": draft_result["indents"]}, default=str, indent=2)


@mcp.tool()
async def cancel_plan(plan_id: int) -> str:
    """Cancel a plan (draft or approved)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan:
            return "Plan not found."
        if plan['status'] not in ('draft', 'approved'):
            return f"Cannot cancel plan in status '{plan['status']}'."
        await conn.execute("UPDATE production_plan SET status='cancelled' WHERE plan_id=$1", plan_id)
    return f"Plan {plan_id} cancelled."


# ---------------------------------------------------------------------------
# Part 3: MRP & Indent
# ---------------------------------------------------------------------------


@mcp.tool()
async def run_mrp(plan_id: int) -> str:
    """Run Material Requirements Planning for an approved plan. Returns per-material availability check and creates draft indents for shortages."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status, entity FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan:
            return "Plan not found."
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.mrp import run_mrp as _mrp
            from app.modules.production.services.indent_manager import generate_draft_indents
            mrp_result = await _mrp(conn, plan_id, plan['entity'])
            draft_result = await generate_draft_indents(conn, mrp_result, plan_id, plan['entity'])
    mrp_result["draft_indents"] = draft_result["indents"]
    return json.dumps(mrp_result, default=str, indent=2)


@mcp.tool()
async def check_material_availability(material: str, qty_needed: float, entity: str) -> str:
    """Quick check if a specific material is available in sufficient quantity. Returns on-hand, on-order, shortage."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        on_hand = await conn.fetchval("SELECT COALESCE(SUM(quantity_kg),0) FROM floor_inventory WHERE sku_name ILIKE $1 AND floor_location IN ('rm_store','pm_store') AND entity=$2", f"%{material}%", entity)
        on_order = await conn.fetchval("SELECT COALESCE(SUM(l.po_weight),0) FROM po_line l JOIN po_header h ON l.transaction_no=h.transaction_no WHERE l.sku_name ILIKE $1 AND h.status='pending'", f"%{material}%")
    avail = float(on_hand or 0) + float(on_order or 0)
    shortage = max(0, qty_needed - avail)
    return json.dumps({"material": material, "needed_kg": qty_needed, "on_hand_kg": float(on_hand or 0), "on_order_kg": float(on_order or 0), "available_kg": avail, "shortage_kg": shortage, "status": "SUFFICIENT" if shortage == 0 else "SHORTAGE"})


@mcp.tool()
async def list_indents(entity: str = "", status: str = "", page: int = 1, page_size: int = 50) -> str:
    """List purchase indents. Filter by entity and status (draft/raised/acknowledged/po_created/received/cancelled)."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',')]
        ph = ','.join(f'${idx+i}' for i in range(len(statuses)))
        conditions.append(f"status IN ({ph})"); params.extend(statuses); idx += len(statuses)
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM purchase_indent WHERE {where}", *params)
        rows = await conn.fetch(f"SELECT * FROM purchase_indent WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}", *params, page_size, offset)
    return json.dumps({"total": total, "page": page, "results": [dict(r) for r in rows]}, default=str, indent=2)


@mcp.tool()
async def get_indent_detail(indent_id: int) -> str:
    """Get indent detail with linked plan line info."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        indent = await conn.fetchrow("SELECT * FROM purchase_indent WHERE indent_id=$1", indent_id)
        if not indent:
            return "Indent not found."
        result = dict(indent)
        if indent['plan_line_id']:
            pl = await conn.fetchrow("SELECT fg_sku_name, customer_name, planned_qty_kg FROM production_plan_line WHERE plan_line_id=$1", indent['plan_line_id'])
            result["plan_line"] = dict(pl) if pl else None
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def edit_indent(indent_id: int, required_qty_kg: float = 0, required_by_date: str = "", priority: int = 0) -> str:
    """Edit a draft indent before sending. Only works when status='draft'."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        indent = await conn.fetchrow("SELECT status FROM purchase_indent WHERE indent_id=$1", indent_id)
        if not indent:
            return "Indent not found."
        if indent['status'] != 'draft':
            return "Can only edit draft indents."
        updates, params, idx = [], [], 1
        if required_qty_kg > 0:
            updates.append(f"required_qty_kg=${idx}"); params.append(required_qty_kg); idx += 1
        if required_by_date:
            updates.append(f"required_by_date=${idx}::date"); params.append(required_by_date); idx += 1
        if priority > 0:
            updates.append(f"priority=${idx}"); params.append(priority); idx += 1
        if not updates:
            return "No fields to update."
        params.append(indent_id)
        await conn.execute(f"UPDATE purchase_indent SET {','.join(updates)} WHERE indent_id=${idx}", *params)
    return f"Indent {indent_id} updated."


@mcp.tool()
async def send_indent(indent_id: int) -> str:
    """Send a draft indent to purchase team. Creates alerts for purchase + stores teams."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        indent = await conn.fetchrow("SELECT * FROM purchase_indent WHERE indent_id=$1", indent_id)
        if not indent or indent['status'] != 'draft':
            return "Indent not found or not in draft status."
        async with conn.transaction():
            await conn.execute("UPDATE purchase_indent SET status='raised' WHERE indent_id=$1", indent_id)
            mat = indent['material_sku_name']; qty = float(indent['required_qty_kg']); dl = indent['required_by_date']
            await conn.execute("INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity) VALUES ('material_shortage','purchase',$1,$2,'indent',$3)", f"SHORTAGE: {mat} — Need {qty:.1f} kg by {dl}", indent_id, indent['entity'])
            await conn.execute("INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity) VALUES ('indent_raised','stores',$1,$2,'indent',$3)", f"Indent raised for {mat} — {qty:.1f} kg. Check existing stock.", indent_id, indent['entity'])
    return f"Indent {indent['indent_number']} sent (raised). 2 alerts created."


@mcp.tool()
async def send_bulk_indents(indent_ids: list[int]) -> str:
    """Send multiple draft indents at once."""
    pool = await get_pool()
    sent = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for iid in indent_ids:
                indent = await conn.fetchrow("SELECT * FROM purchase_indent WHERE indent_id=$1 AND status='draft'", iid)
                if not indent:
                    continue
                await conn.execute("UPDATE purchase_indent SET status='raised' WHERE indent_id=$1", iid)
                mat = indent['material_sku_name']; qty = float(indent['required_qty_kg'])
                await conn.execute("INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity) VALUES ('material_shortage','purchase',$1,$2,'indent',$3)", f"SHORTAGE: {mat} — {qty:.1f} kg", iid, indent['entity'])
                await conn.execute("INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity) VALUES ('indent_raised','stores',$1,$2,'indent',$3)", f"Indent for {mat} — {qty:.1f} kg", iid, indent['entity'])
                sent += 1
    return f"Sent {sent} of {len(indent_ids)} indents."


@mcp.tool()
async def acknowledge_indent(indent_id: int, acknowledged_by: str) -> str:
    """Purchase team acknowledges receipt of indent. raised -> acknowledged."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE purchase_indent SET status='acknowledged', acknowledged_by=$2, acknowledged_at=NOW() WHERE indent_id=$1 AND status='raised'", indent_id, acknowledged_by)
        if result == 'UPDATE 0':
            return "Indent not found or not in raised status."
    return f"Indent {indent_id} acknowledged by {acknowledged_by}."


@mcp.tool()
async def link_indent_to_po(indent_id: int, po_reference: str) -> str:
    """Link an acknowledged indent to a Purchase Order. acknowledged -> po_created."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE purchase_indent SET status='po_created', po_reference=$2 WHERE indent_id=$1 AND status='acknowledged'", indent_id, po_reference)
        if result == 'UPDATE 0':
            return "Indent not found or not acknowledged."
    return f"Indent {indent_id} linked to PO {po_reference}."


@mcp.tool()
async def list_alerts(target_team: str = "", entity: str = "", is_read: str = "") -> str:
    """List store alerts. Filter by target_team (purchase/stores/production/qc), entity, is_read (true/false)."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if target_team:
        conditions.append(f"target_team=${idx}"); params.append(target_team); idx += 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if is_read:
        conditions.append(f"is_read=${idx}"); params.append(is_read.lower() == 'true'); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM store_alert WHERE {where} ORDER BY created_at DESC LIMIT 50", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def mark_alert_read(alert_id: int) -> str:
    """Mark an alert as read."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE store_alert SET is_read=TRUE WHERE alert_id=$1", alert_id)
    return f"Alert {alert_id} marked as read."


# ---------------------------------------------------------------------------
# Part 4: Job Card Engine
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_production_orders(plan_id: int) -> str:
    """Create production orders for all lines in an approved plan."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT entity FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan:
            return "Plan not found."
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.job_card_engine import create_production_orders as _create
            result = await _create(conn, plan_id, plan['entity'])
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def list_orders(entity: str = "", status: str = "", page: int = 1, page_size: int = 50) -> str:
    """List production orders."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM production_order WHERE {where}", *params)
        rows = await conn.fetch(f"SELECT * FROM production_order WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}", *params, page_size, offset)
    return json.dumps({"total": total, "results": [dict(r) for r in rows]}, default=str, indent=2)


@mcp.tool()
async def get_order_detail(prod_order_id: int) -> str:
    """Get production order detail with all its job cards."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM production_order WHERE prod_order_id=$1", prod_order_id)
        if not order:
            return "Order not found."
        jcs = await conn.fetch("SELECT job_card_id, job_card_number, step_number, process_name, stage, status, is_locked FROM job_card WHERE prod_order_id=$1 ORDER BY step_number", prod_order_id)
    result = dict(order)
    result["job_cards"] = [dict(j) for j in jcs]
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def generate_job_cards(prod_order_id: int) -> str:
    """Generate sequential job cards for a production order. First stage unlocked, rest locked."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.job_card_engine import create_job_cards as _create
            result = await _create(conn, prod_order_id)
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def list_job_cards(entity: str = "", status: str = "", team_leader: str = "", floor: str = "", page: int = 1, page_size: int = 50) -> str:
    """List job cards with filters."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',')]
        ph = ','.join(f'${idx+i}' for i in range(len(statuses)))
        conditions.append(f"status IN ({ph})"); params.extend(statuses); idx += len(statuses)
    if team_leader:
        conditions.append(f"assigned_to_team_leader ILIKE ${idx}"); params.append(f"%{team_leader}%"); idx += 1
    if floor:
        conditions.append(f"floor ILIKE ${idx}"); params.append(f"%{floor}%"); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM job_card WHERE {where}", *params)
        rows = await conn.fetch(f"SELECT job_card_id, job_card_number, prod_order_id, step_number, process_name, stage, fg_sku_name, customer_name, batch_number, batch_size_kg, assigned_to_team_leader, is_locked, status, start_time, factory, floor, entity FROM job_card WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}", *params, page_size, offset)
    return json.dumps({"total": total, "results": [dict(r) for r in rows]}, default=str, indent=2)


@mcp.tool()
async def get_job_card_detail(job_card_id: int) -> str:
    """Get full job card detail matching CFC/PRD/JC/V3.0 PDF — all sections and annexures."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.job_card_engine import get_job_card_detail as _detail
        result = await _detail(conn, job_card_id)
    if not result:
        return "Job card not found."
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def team_dashboard(team_leader: str, entity: str = "") -> str:
    """Get job cards assigned to a specific team leader, priority-sorted."""
    pool = await get_pool()
    conditions = ["assigned_to_team_leader ILIKE $1"]
    params = [f"%{team_leader}%"]
    idx = 2
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity)
    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM job_card WHERE {where} AND status NOT IN ('closed','completed') ORDER BY CASE status WHEN 'in_progress' THEN 1 WHEN 'material_received' THEN 2 WHEN 'assigned' THEN 3 WHEN 'unlocked' THEN 4 ELSE 5 END, created_at", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def floor_dashboard(floor: str, entity: str = "") -> str:
    """Get all job cards on a specific floor."""
    pool = await get_pool()
    conditions = ["floor ILIKE $1"]
    params = [f"%{floor}%"]
    if entity:
        conditions.append(f"entity=$2"); params.append(entity)
    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM job_card WHERE {where} ORDER BY status, created_at", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def assign_job_card(job_card_id: int, team_leader: str, team_members: list[str] = []) -> str:
    """Assign a job card to a team leader."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.job_card_engine import assign_job_card as _assign
        result = await _assign(conn, job_card_id, team_leader, team_members or None)
    return json.dumps(result, default=str)


@mcp.tool()
async def receive_material_qr(job_card_id: int, box_ids: list[str]) -> str:
    """Scan QR codes (po_box box_ids) to receive material into a job card. Validates boxes, deducts inventory, creates floor movement."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity FROM job_card WHERE job_card_id=$1", job_card_id)
        if not jc:
            return "Job card not found."
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.qr_service import receive_material_via_qr
            result = await receive_material_via_qr(conn, job_card_id, box_ids, jc['entity'])
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def start_job_card(job_card_id: int) -> str:
    """Start production on a job card. Sets status to in_progress."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.job_card_engine import start_job_card as _start
        result = await _start(conn, job_card_id)
    return json.dumps(result, default=str)


@mcp.tool()
async def complete_process_step(job_card_id: int, step_number: int, operator_name: str = "", qc_passed: bool = False) -> str:
    """Complete a process step within a job card."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.job_card_engine import complete_process_step as _complete
        result = await _complete(conn, job_card_id, step_number, operator_name, qc_passed)
    return json.dumps(result, default=str)


@mcp.tool()
async def record_output(job_card_id: int, fg_actual_units: int = 0, fg_actual_kg: float = 0, rm_consumed_kg: float = 0, material_return_kg: float = 0, rejection_kg: float = 0, rejection_reason: str = "", process_loss_kg: float = 0, process_loss_pct: float = 0, offgrade_kg: float = 0, offgrade_category: str = "") -> str:
    """Record Section 5 output data for a job card."""
    pool = await get_pool()
    data = {k: v for k, v in {"fg_actual_units": fg_actual_units, "fg_actual_kg": fg_actual_kg, "rm_consumed_kg": rm_consumed_kg, "material_return_kg": material_return_kg, "rejection_kg": rejection_kg, "rejection_reason": rejection_reason, "process_loss_kg": process_loss_kg, "process_loss_pct": process_loss_pct, "offgrade_kg": offgrade_kg, "offgrade_category": offgrade_category}.items() if v}
    async with pool.acquire() as conn:
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.job_card_engine import record_output as _record
            result = await _record(conn, job_card_id, data)
    return json.dumps(result, default=str)


@mcp.tool()
async def complete_job_card(job_card_id: int) -> str:
    """Complete a job card. Auto-unlocks next stage or completes the production order if last stage."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity FROM job_card WHERE job_card_id=$1", job_card_id)
        if not jc:
            return "Job card not found."
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.job_card_engine import complete_job_card as _complete
            result = await _complete(conn, job_card_id, jc['entity'])
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def sign_off_job_card(job_card_id: int, sign_off_type: str, name: str) -> str:
    """Record a sign-off on a job card. Types: production_incharge, quality_analysis, warehouse_incharge, plant_head."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO job_card_sign_off (job_card_id, sign_off_type, name, signed_at) VALUES ($1,$2,$3,NOW()) ON CONFLICT (job_card_id, sign_off_type) DO UPDATE SET name=$3, signed_at=NOW()", job_card_id, sign_off_type, name)
    return f"Sign-off recorded: {sign_off_type} by {name}."


@mcp.tool()
async def close_job_card(job_card_id: int) -> str:
    """Close a job card after all required sign-offs (production_incharge, quality_analysis, warehouse_incharge)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.job_card_engine import close_job_card as _close
        result = await _close(conn, job_card_id)
    return json.dumps(result, default=str)


@mcp.tool()
async def force_unlock_job_card(job_card_id: int, authority: str, reason: str) -> str:
    """Force unlock a locked job card. Requires authority name and reason. Creates audit trail."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT entity FROM job_card WHERE job_card_id=$1", job_card_id)
        if not jc:
            return "Job card not found."
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.job_card_engine import force_unlock as _force
            result = await _force(conn, job_card_id, authority, reason, jc['entity'])
    return json.dumps(result, default=str)


@mcp.tool()
async def add_environment_data(job_card_id: int, parameters_json: str) -> str:
    """Record Annexure C environmental parameters. Pass JSON array: [{"parameter_name":"humidity_pct","value":"45"}, ...]"""
    params = json.loads(parameters_json)
    pool = await get_pool()
    async with pool.acquire() as conn:
        for p in params:
            await conn.execute("INSERT INTO job_card_environment (job_card_id, parameter_name, value) VALUES ($1,$2,$3)", job_card_id, p['parameter_name'], p['value'])
    return f"Saved {len(params)} environment parameters."


@mcp.tool()
async def add_metal_detection(job_card_id: int, check_type: str, fe_pass: bool = True, nfe_pass: bool = True, ss_pass: bool = True, failed_units: int = 0, seal_check: bool = True, seal_failed_units: int = 0, wt_check: bool = True, wt_failed_units: int = 0) -> str:
    """Record Annexure A/B metal detection validation. check_type: 'pre_packaging' or 'post_packaging'."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        det_id = await conn.fetchval("INSERT INTO job_card_metal_detection (job_card_id, check_type, fe_pass, nfe_pass, ss_pass, failed_units, seal_check, seal_failed_units, wt_check, wt_failed_units) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING detection_id", job_card_id, check_type, fe_pass, nfe_pass, ss_pass, failed_units, seal_check, seal_failed_units, wt_check, wt_failed_units)
    return f"Metal detection recorded (detection_id={det_id})."


@mcp.tool()
async def add_weight_checks(job_card_id: int, samples_json: str, target_wt_g: float = 0, tolerance_g: float = 0) -> str:
    """Record Annexure B weight/leak checks. Pass JSON array of samples: [{"sample_number":1,"net_weight":500.5,"gross_weight":510.2,"leak_test_pass":true}, ...]"""
    samples = json.loads(samples_json)
    pool = await get_pool()
    async with pool.acquire() as conn:
        for s in samples:
            await conn.execute("INSERT INTO job_card_weight_check (job_card_id, sample_number, net_weight, gross_weight, leak_test_pass, target_wt_g, tolerance_g) VALUES ($1,$2,$3,$4,$5,$6,$7)", job_card_id, s['sample_number'], s.get('net_weight'), s.get('gross_weight'), s.get('leak_test_pass'), target_wt_g or None, tolerance_g or None)
    return f"Saved {len(samples)} weight check samples."


@mcp.tool()
async def add_loss_reconciliation(job_card_id: int, entries_json: str) -> str:
    """Record Annexure D loss reconciliation. Pass JSON array: [{"loss_category":"sorting_rejection","budgeted_loss_pct":2.0,"budgeted_loss_kg":25.5,"actual_loss_kg":22.0,"remarks":"Within limits"}, ...]"""
    entries = json.loads(entries_json)
    pool = await get_pool()
    total_b, total_a = 0, 0
    async with pool.acquire() as conn:
        for e in entries:
            b = e.get('budgeted_loss_kg', 0); a = e.get('actual_loss_kg', 0)
            await conn.execute("INSERT INTO job_card_loss_reconciliation (job_card_id, loss_category, budgeted_loss_pct, budgeted_loss_kg, actual_loss_kg, variance_kg, remarks) VALUES ($1,$2,$3,$4,$5,$6,$7)", job_card_id, e['loss_category'], e.get('budgeted_loss_pct'), b, a, a - b, e.get('remarks'))
            total_b += b; total_a += a
    return f"Saved {len(entries)} loss entries. Budgeted: {total_b:.1f} kg, Actual: {total_a:.1f} kg."


@mcp.tool()
async def add_remarks(job_card_id: int, remark_type: str, content: str, recorded_by: str = "") -> str:
    """Record Annexure E remarks. remark_type: 'observation', 'deviation', 'corrective_action'."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        remark_id = await conn.fetchval("INSERT INTO job_card_remarks (job_card_id, remark_type, content, recorded_by) VALUES ($1,$2,$3,$4) RETURNING remark_id", job_card_id, remark_type, content, recorded_by)
    return f"Remark recorded (remark_id={remark_id})."


# ---------------------------------------------------------------------------
# Part 5: Inventory & Tracking
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_floor_inventory(entity: str, floor_location: str = "", search: str = "", page: int = 1, page_size: int = 50) -> str:
    """List floor inventory items. Filter by floor_location (rm_store/pm_store/production_floor/fg_store) and search by SKU name."""
    pool = await get_pool()
    conditions = ["entity=$1", "quantity_kg > 0"]
    params = [entity]; idx = 2
    if floor_location:
        conditions.append(f"floor_location=${idx}"); params.append(floor_location); idx += 1
    if search:
        conditions.append(f"sku_name ILIKE ${idx}"); params.append(f"%{search}%"); idx += 1
    where = " AND ".join(conditions)
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM floor_inventory WHERE {where}", *params)
        rows = await conn.fetch(f"SELECT * FROM floor_inventory WHERE {where} ORDER BY quantity_kg DESC LIMIT ${idx} OFFSET ${idx+1}", *params, page_size, offset)
    return json.dumps({"total": total, "results": [dict(r) for r in rows]}, default=str, indent=2)


@mcp.tool()
async def get_floor_summary(entity: str) -> str:
    """Get aggregated stock per floor location — item count and total kg per floor."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT floor_location, COUNT(DISTINCT sku_name) as item_count, COALESCE(SUM(quantity_kg),0) as total_kg FROM floor_inventory WHERE entity=$1 AND quantity_kg > 0 GROUP BY floor_location ORDER BY floor_location", entity)
    return json.dumps([{"floor_location": r['floor_location'], "item_count": r['item_count'], "total_kg": float(r['total_kg'])} for r in rows])


@mcp.tool()
async def move_material(sku_name: str, from_location: str, to_location: str, quantity_kg: float, entity: str, reason: str = "", moved_by: str = "") -> str:
    """Move material between floor locations. Validates allowed transitions and sufficient stock."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.floor_tracker import move_material as _move
            result = await _move(conn, sku_name, from_location, to_location, quantity_kg, entity, reason=reason, moved_by=moved_by)
    if "error" in result:
        return result["message"]
    return json.dumps(result, default=str)


@mcp.tool()
async def get_movement_history(entity: str, sku_name: str = "", from_location: str = "", to_location: str = "", page: int = 1, page_size: int = 50) -> str:
    """Get floor movement audit trail with filters."""
    pool = await get_pool()
    conditions = ["entity=$1"]; params = [entity]; idx = 2
    if sku_name:
        conditions.append(f"sku_name ILIKE ${idx}"); params.append(f"%{sku_name}%"); idx += 1
    if from_location:
        conditions.append(f"from_location=${idx}"); params.append(from_location); idx += 1
    if to_location:
        conditions.append(f"to_location=${idx}"); params.append(to_location); idx += 1
    where = " AND ".join(conditions)
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM floor_movement WHERE {where}", *params)
        rows = await conn.fetch(f"SELECT * FROM floor_movement WHERE {where} ORDER BY moved_at DESC LIMIT ${idx} OFFSET ${idx+1}", *params, page_size, offset)
    return json.dumps({"total": total, "results": [dict(r) for r in rows]}, default=str, indent=2)


@mcp.tool()
async def check_idle_materials(entity: str) -> str:
    """Check for materials idle 3+ days on any floor. Creates alerts for 3-day warning and 5-day critical."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.idle_checker import check_idle_materials as _check
            result = await _check(conn, entity)
    return json.dumps(result, default=str)


@mcp.tool()
async def list_offgrade_inventory(entity: str = "", status: str = "available", item_group: str = "") -> str:
    """List off-grade inventory."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    if item_group:
        conditions.append(f"item_group ILIKE ${idx}"); params.append(f"%{item_group}%"); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM offgrade_inventory WHERE {where} ORDER BY created_at DESC LIMIT 100", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def list_offgrade_rules() -> str:
    """List all off-grade reuse rules."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM offgrade_reuse_rule ORDER BY source_item_group")
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def create_offgrade_rule(source_item_group: str, target_item_group: str, max_substitution_pct: float, notes: str = "") -> str:
    """Create an off-grade reuse rule."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rule_id = await conn.fetchval("INSERT INTO offgrade_reuse_rule (source_item_group, target_item_group, max_substitution_pct, notes) VALUES ($1,$2,$3,$4) ON CONFLICT (source_item_group, target_item_group) DO UPDATE SET max_substitution_pct=$3, notes=$4 RETURNING rule_id", source_item_group, target_item_group, max_substitution_pct, notes)
    return f"Rule created/updated (rule_id={rule_id})."


@mcp.tool()
async def get_loss_analysis(entity: str = "", group_by: str = "product", product_name: str = "", stage: str = "") -> str:
    """Get loss analysis with aggregation. group_by: product, stage, month, machine."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if product_name:
        conditions.append(f"product_name ILIKE ${idx}"); params.append(f"%{product_name}%"); idx += 1
    if stage:
        conditions.append(f"stage=${idx}"); params.append(stage); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    group_col = {"product": "product_name", "stage": "stage", "month": "TO_CHAR(production_date,'YYYY-MM')", "machine": "machine_name"}.get(group_by, "product_name")
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {group_col} AS group_key, COUNT(*) AS batch_count, ROUND(AVG(loss_pct)::numeric,3) AS avg_loss_pct, ROUND(SUM(loss_kg)::numeric,3) AS total_loss_kg FROM process_loss WHERE {where} GROUP BY {group_col} ORDER BY SUM(loss_kg) DESC", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def get_loss_anomalies(entity: str = "", threshold_multiplier: float = 2.0) -> str:
    """Find batches with loss significantly above average. Default: 2x average."""
    pool = await get_pool()
    conditions = []
    params = []
    idx = 1
    if entity:
        conditions.append(f"p.entity=${idx}"); params.append(entity); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"WITH stats AS (SELECT product_name, stage, AVG(loss_pct) AS avg_pct FROM process_loss WHERE {where.replace('p.','')} GROUP BY product_name, stage) SELECT p.*, s.avg_pct FROM process_loss p JOIN stats s ON p.product_name=s.product_name AND p.stage=s.stage WHERE {where} AND p.loss_pct > s.avg_pct * ${idx} ORDER BY (p.loss_pct - s.avg_pct) DESC LIMIT 50", *params, threshold_multiplier)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ---------------------------------------------------------------------------
# Part 6: Day-End & Balance Scan
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_day_end_summary(entity: str, target_date: str = "") -> str:
    """Get today's completed final-stage job cards with output and dispatch data."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.day_end import get_day_end_summary as _summary
        d = date.fromisoformat(target_date) if target_date else None
        result = await _summary(conn, entity, d)
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def submit_dispatch(dispatches_json: str, entity: str) -> str:
    """Bulk update dispatch quantities. Pass JSON array: [{"job_card_id":1,"dispatch_qty":1240}, ...]"""
    dispatches = json.loads(dispatches_json)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.day_end import bulk_dispatch
            result = await bulk_dispatch(conn, dispatches, entity)
    return json.dumps(result, default=str)


@mcp.tool()
async def submit_balance_scan(floor_location: str, entity: str, submitted_by: str, scan_lines_json: str) -> str:
    """Submit a day-end balance scan for a floor. Pass scan_lines_json: [{"sku_name":"...","scanned_qty_kg":100.5}, ...]"""
    scan_lines = json.loads(scan_lines_json)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.day_end import submit_balance_scan as _submit
            result = await _submit(conn, floor_location, entity, submitted_by, scan_lines)
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def get_scan_status(entity: str, target_date: str = "") -> str:
    """Get today's balance scan submission status per floor (4 required floors)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.day_end import get_scan_status as _status
        d = date.fromisoformat(target_date) if target_date else None
        result = await _status(conn, entity, d)
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def get_scan_detail(scan_id: int) -> str:
    """Get balance scan detail with all line items."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.day_end import get_scan_detail as _detail
        result = await _detail(conn, scan_id)
    if not result:
        return "Scan not found."
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def reconcile_scan(scan_id: int, reviewed_by: str) -> str:
    """Reconcile a balance scan — adjusts floor_inventory to match physical count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.day_end import reconcile_scan as _reconcile
            result = await _reconcile(conn, scan_id, reviewed_by)
    return json.dumps(result, default=str)


@mcp.tool()
async def check_missing_scans(entity: str, target_date: str = "") -> str:
    """Check which floors haven't submitted balance scans. Creates alerts for missing floors."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.day_end import check_missing_scans as _check
            d = date.fromisoformat(target_date) if target_date else None
            result = await _check(conn, entity, d)
    return json.dumps(result, default=str)


@mcp.tool()
async def get_yield_summary(entity: str = "", product_name: str = "", period: str = "") -> str:
    """Get yield summary by product/period."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if product_name:
        conditions.append(f"product_name ILIKE ${idx}"); params.append(f"%{product_name}%"); idx += 1
    if period:
        conditions.append(f"period=${idx}"); params.append(period); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM yield_summary WHERE {where} ORDER BY computed_at DESC LIMIT 100", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ---------------------------------------------------------------------------
# Part 7: Discrepancy & AI
# ---------------------------------------------------------------------------


@mcp.tool()
async def revise_plan(plan_id: int, change_event: str) -> str:
    """Revise an existing plan via Claude AI (to be called from Claude Desktop — collects context for you to analyze and then save)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT entity FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan:
            return "Plan not found."
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.ai_planner import collect_revision_context
        context = await collect_revision_context(conn, plan_id, change_event, plan['entity'])
    return json.dumps(context, default=str, indent=2)


@mcp.tool()
async def get_revision_history(plan_id: int) -> str:
    """Get the chain of revisions for a plan."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        chain = []
        current_id = plan_id
        while current_id:
            p = await conn.fetchrow("SELECT plan_id, plan_name, revision_number, status, previous_plan_id, created_at FROM production_plan WHERE plan_id=$1", current_id)
            if not p:
                break
            chain.append(dict(p))
            current_id = p['previous_plan_id']
        forward_id = plan_id
        while True:
            nxt = await conn.fetchrow("SELECT plan_id, plan_name, revision_number, status, previous_plan_id, created_at FROM production_plan WHERE previous_plan_id=$1", forward_id)
            if not nxt:
                break
            chain.insert(0, dict(nxt))
            forward_id = nxt['plan_id']
    seen = set()
    unique = [p for p in chain if p['plan_id'] not in seen and not seen.add(p['plan_id'])]
    unique.sort(key=lambda x: x.get('revision_number') or 0)
    return json.dumps({"plan_id": plan_id, "revision_chain": unique}, default=str, indent=2)


@mcp.tool()
async def report_discrepancy(discrepancy_type: str, entity: str, severity: str = "major", affected_material: str = "", affected_machine_id: int = 0, details: str = "", reported_by: str = "") -> str:
    """Report an internal discrepancy. Auto-holds affected job cards. Types: rm_grade_mismatch, rm_qc_failure, rm_expired, machine_breakdown, contamination, short_delivery."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.discrepancy_manager import report_discrepancy as _report
            result = await _report(conn, discrepancy_type=discrepancy_type, severity=severity, affected_material=affected_material or None, affected_machine_id=affected_machine_id or None, details=details, reported_by=reported_by, entity=entity)
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def list_discrepancies(entity: str = "", status: str = "", discrepancy_type: str = "") -> str:
    """List discrepancy reports with filters."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    if discrepancy_type:
        conditions.append(f"discrepancy_type=${idx}"); params.append(discrepancy_type); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM discrepancy_report WHERE {where} ORDER BY created_at DESC LIMIT 50", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def get_discrepancy_detail(discrepancy_id: int) -> str:
    """Get discrepancy detail with affected job cards."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
        from app.modules.production.services.discrepancy_manager import get_discrepancy_detail as _detail
        result = await _detail(conn, discrepancy_id)
    if not result:
        return "Discrepancy not found."
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def resolve_discrepancy(discrepancy_id: int, resolution_type: str, resolution_details: str, resolved_by: str) -> str:
    """Resolve a discrepancy. resolution_type: material_substituted, machine_rescheduled, deferred, cancelled_replanned, proceed_with_deviation."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        disc = await conn.fetchrow("SELECT entity FROM discrepancy_report WHERE discrepancy_id=$1", discrepancy_id)
        if not disc:
            return "Discrepancy not found."
        async with conn.transaction():
            import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.discrepancy_manager import resolve_discrepancy as _resolve
            result = await _resolve(conn, discrepancy_id, resolution_type=resolution_type, resolution_details=resolution_details, resolved_by=resolved_by, entity=disc['entity'])
    return json.dumps(result, default=str)


@mcp.tool()
async def list_ai_recommendations(entity: str = "", recommendation_type: str = "", status: str = "") -> str:
    """List all AI recommendations."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if recommendation_type:
        conditions.append(f"recommendation_type=${idx}"); params.append(recommendation_type); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT recommendation_id, recommendation_type, entity, tokens_used, latency_ms, model_used, status, feedback, plan_id, created_at FROM ai_recommendation WHERE {where} ORDER BY created_at DESC LIMIT 20", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def submit_ai_feedback(recommendation_id: int, status: str, feedback: str = "") -> str:
    """Accept or reject an AI recommendation. status: 'accepted' or 'rejected'."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE ai_recommendation SET status=$2, feedback=$3 WHERE recommendation_id=$1", recommendation_id, status, feedback)
    return f"Recommendation {recommendation_id} marked as {status}."


# ---------------------------------------------------------------------------
# Run — Streamable HTTP transport
# ---------------------------------------------------------------------------

@mcp.tool()
async def ping() -> str:
    """Check if the MCP server and database are connected."""
    pool = await get_pool()
    val = await pool.fetchval("SELECT 1")
    return f"OK — DB connected (result={val})"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
