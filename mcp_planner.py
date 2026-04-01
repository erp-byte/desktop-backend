"""MCP Planner Server — Planning tools only (view + create for production plans).

Standalone server. Runs on its own port/service.
Tools: 18 (plan generation, fulfillment, MRP, indents — view + create only)
"""

import json
import logging
import os
from datetime import date, timedelta

import asyncpg
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_port = int(os.environ.get("PORT", "8002"))

mcp = FastMCP(
    "Candor Planner",
    host="0.0.0.0",
    port=_port,
    streamable_http_path="/",
)

_pool = None


async def get_pool():
    global _pool
    if _pool is None:
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            from pathlib import Path
            env_path = Path(__file__).parent / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.strip().startswith("DATABASE_URL="):
                        db_url = line.strip().split("=", 1)[1]
        if not db_url:
            raise RuntimeError("DATABASE_URL not set")
        _pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
    return _pool


# ── Health ────────────────────────────────────────────────────────────────

@mcp.tool()
async def ping() -> str:
    """Check connection to database."""
    pool = await get_pool()
    await pool.fetchval("SELECT 1")
    return "OK — Planner MCP connected"


# ── Fulfillment (view + sync) ────────────────────────────────────────────

@mcp.tool()
async def sync_fulfillment(entity: str = "") -> str:
    """Sync all FG Sales Order lines into fulfillment table. Safe and idempotent."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            query = "SELECT h.so_id, h.so_date, h.customer_name, h.company, l.so_line_id, l.sku_name, l.quantity FROM so_header h JOIN so_line l ON h.so_id = l.so_id WHERE l.so_line_id NOT IN (SELECT so_line_id FROM so_fulfillment)"
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
                    "INSERT INTO so_fulfillment (so_line_id, so_id, financial_year, fg_sku_name, customer_name, original_qty_kg, pending_qty_kg, entity, delivery_deadline, priority, order_status) VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8,5,'open') ON CONFLICT (so_line_id, financial_year) DO NOTHING",
                    r['so_line_id'], r['so_id'], fy, r['sku_name'], r['customer_name'], qty, ent, so_date + timedelta(days=7)
                )
                synced += 1
    return f"Synced {synced} fulfillment records."


@mcp.tool()
async def get_fulfillment_list(entity: str = "", status: str = "open,partial", page: int = 1, page_size: int = 50) -> str:
    """List fulfillment records with filters."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',')]
        ph = ','.join(f'${idx+i}' for i in range(len(statuses)))
        conditions.append(f"order_status IN ({ph})"); params.extend(statuses); idx += len(statuses)
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM so_fulfillment WHERE {where}", *params)
        rows = await conn.fetch(
            f"SELECT fulfillment_id, fg_sku_name, customer_name, pending_qty_kg, delivery_deadline, priority, order_status, financial_year FROM so_fulfillment WHERE {where} ORDER BY delivery_deadline, priority LIMIT ${idx} OFFSET ${idx+1}",
            *params, page_size, offset
        )
    return json.dumps({"total": total, "page": page, "results": [dict(r) for r in rows]}, default=str, indent=2)


@mcp.tool()
async def get_demand_summary(entity: str = "", financial_year: str = "") -> str:
    """Aggregated pending demand by product and customer."""
    pool = await get_pool()
    query = "SELECT fg_sku_name, customer_name, SUM(pending_qty_kg) AS total_qty_kg, COUNT(*) AS order_count, MIN(delivery_deadline) AS earliest_deadline FROM so_fulfillment WHERE order_status IN ('open','partial')"
    params = []; idx = 1
    if entity:
        query += f" AND entity=${idx}"; params.append(entity); idx += 1
    if financial_year:
        query += f" AND financial_year=${idx}"; params.append(financial_year); idx += 1
    query += " GROUP BY fg_sku_name, customer_name ORDER BY MIN(delivery_deadline)"
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ── Planning Context + Save ──────────────────────────────────────────────

@mcp.tool()
async def get_planning_context(entity: str, fulfillment_ids: list[int], target_date: str = "") -> str:
    """Get full planning context: demand with BOMs, inventory, machines, capacity. Use this data to create a production schedule."""
    pool = await get_pool()
    t_date = target_date or str(date.today())
    async with pool.acquire() as conn:
        demand_rows = await conn.fetch(
            "SELECT fulfillment_id, fg_sku_name, customer_name, pending_qty_kg, delivery_deadline, priority FROM so_fulfillment WHERE fulfillment_id = ANY($1) AND order_status IN ('open','partial') ORDER BY delivery_deadline, priority",
            fulfillment_ids
        )
        demand, no_bom = [], []
        for r in demand_rows:
            bom = await conn.fetchrow("SELECT bom_id, process_category FROM bom_header WHERE fg_sku_name=$1 AND is_active=TRUE LIMIT 1", r['fg_sku_name'])
            if not bom:
                no_bom.append({"fulfillment_id": r['fulfillment_id'], "fg_sku_name": r['fg_sku_name']}); continue
            bom_id = bom['bom_id']
            rm_count = await conn.fetchval("SELECT COUNT(*) FROM bom_line WHERE bom_id=$1 AND item_type='rm'", bom_id)
            route_rows = await conn.fetch("SELECT stage FROM bom_process_route WHERE bom_id=$1 ORDER BY step_number", bom_id)
            mat_rows = await conn.fetch("SELECT material_sku_name, item_type, quantity_per_unit, uom, loss_pct FROM bom_line WHERE bom_id=$1", bom_id)
            qty_kg = float(r['pending_qty_kg'])
            materials = []
            for m in mat_rows:
                need = qty_kg * float(m['quantity_per_unit']); loss = float(m['loss_pct'] or 0)
                gross = need / (1 - loss/100) if loss < 100 else need
                materials.append({"name": m['material_sku_name'], "type": m['item_type'], "need_qty": round(gross, 3), "uom": m['uom'], "loss_pct": loss})
            demand.append({
                "fulfillment_id": r['fulfillment_id'], "fg_sku_name": r['fg_sku_name'],
                "customer": r['customer_name'], "qty_kg": qty_kg, "deadline": str(r['delivery_deadline']),
                "priority": r['priority'], "production_type": 'production' if rm_count > 0 else 'repackaging',
                "bom_id": bom_id, "process_route": [r2['stage'] for r2 in route_rows] or ['packaging'],
                "materials": materials
            })
        inv_rows = await conn.fetch("SELECT sku_name, floor_location, SUM(quantity_kg) as qty_kg FROM floor_inventory WHERE entity=$1 GROUP BY sku_name, floor_location", entity)
        inventory = {"rm_store": [], "pm_store": [], "fg_store": []}
        for r in inv_rows:
            if r['floor_location'] in inventory:
                inventory[r['floor_location']].append({"sku": r['sku_name'], "qty_kg": float(r['qty_kg'])})
        machine_rows = await conn.fetch(
            "SELECT m.machine_name, m.floor, m.allocation, mc.stage, mc.item_group, mc.capacity_kg_per_hr FROM machine m LEFT JOIN machine_capacity mc ON m.machine_id=mc.machine_id WHERE m.entity=$1 AND m.status='active'",
            entity
        )
        machines_map = {}
        for r in machine_rows:
            name = r['machine_name']
            if name not in machines_map:
                machines_map[name] = {"name": name, "floor": r['floor'], "allocation": r['allocation'], "capacity": []}
            if r['stage']:
                machines_map[name]["capacity"].append({"group": r['item_group'], "stage": r['stage'], "kg_hr": float(r['capacity_kg_per_hr'])})
    context = {
        "date": t_date, "entity": entity, "demand": demand, "no_bom_items": no_bom,
        "inventory": inventory, "machines": [m for m in machines_map.values() if m['capacity']],
        "in_progress_jobs": [], "pending_indents": []
    }
    return json.dumps(context, default=str, indent=2)


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
    """Save a production plan to the database. Pass the schedule as JSON array."""
    schedule = json.loads(schedule_json)
    full = {
        "schedule": schedule,
        "material_check": json.loads(material_check_json),
        "risk_flags": json.loads(risk_flags_json),
    }

    # FIX: parse date strings into Python date objects before passing to asyncpg.
    # asyncpg calls .toordinal() internally when serialising date parameters, so
    # raw strings must be converted first. The ::date SQL casts are also removed
    # since asyncpg handles native date objects without them.
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to) if date_to else d_from

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            plan_id = await conn.fetchval(
                "INSERT INTO production_plan (plan_name, entity, plan_type, plan_date, date_from, date_to, status, ai_generated, ai_analysis_json) "
                "VALUES ($1,$2,$3,$4,$5,$6,'draft',TRUE,$7) RETURNING plan_id",
                f"{plan_type.title()} Plan — {d_from}",
                entity,
                plan_type,
                d_from,   # plan_date
                d_from,   # date_from
                d_to,     # date_to
                json.dumps(full, default=str),
            )
            machines = await conn.fetch("SELECT machine_id, machine_name FROM machine WHERE entity=$1", entity)
            ml = {r['machine_name'].strip().lower(): r['machine_id'] for r in machines}
            created = 0
            for item in schedule:
                fg = item.get("fg_sku_name", "")
                mn = item.get("machine_name", "")
                mid = ml.get(mn.strip().lower())
                bom_id = item.get("bom_id") or await conn.fetchval(
                    "SELECT bom_id FROM bom_header WHERE fg_sku_name=$1 AND is_active=TRUE LIMIT 1", fg
                )
                await conn.execute(
                    "INSERT INTO production_plan_line "
                    "(plan_id, fg_sku_name, customer_name, bom_id, planned_qty_kg, planned_qty_units, "
                    "machine_id, priority, shift, stage_sequence, estimated_hours, "
                    "linked_so_fulfillment_ids, reasoning) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                    plan_id,
                    fg,
                    item.get("customer_name"),
                    bom_id,
                    item.get("qty_kg", 0),
                    item.get("qty_units"),
                    mid,
                    item.get("priority", 5),
                    item.get("shift", "day"),
                    item.get("stage_sequence") or None,
                    item.get("estimated_hours"),
                    item.get("linked_fulfillment_ids") or None,
                    item.get("reasoning"),
                )
                created += 1
    return f"Plan saved! plan_id={plan_id}, {created} lines. Status: draft."


@mcp.tool()
async def list_plans(entity: str = "", status: str = "", plan_type: str = "") -> str:
    """List production plans."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    if plan_type:
        conditions.append(f"plan_type=${idx}"); params.append(plan_type); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT plan_id, plan_name, entity, plan_type, plan_date, date_from, date_to, status, ai_generated, revision_number, created_at FROM production_plan WHERE {where} ORDER BY created_at DESC LIMIT 20",
            *params
        )
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def get_plan_detail(plan_id: int) -> str:
    """Get plan with all lines, material check, and risk flags."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT * FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan:
            return "Plan not found."
        lines = await conn.fetch("SELECT * FROM production_plan_line WHERE plan_id=$1 ORDER BY priority", plan_id)
    result = dict(plan)
    result["lines"] = [dict(l) for l in lines]
    ai = result.get("ai_analysis_json")
    if ai:
        if isinstance(ai, str):
            ai = json.loads(ai)
        result["material_check"] = ai.get("material_check", [])
        result["risk_flags"] = ai.get("risk_flags", [])
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def approve_plan(plan_id: int, approved_by: str) -> str:
    """Approve a draft plan. Triggers MRP and creates draft indents."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT status, entity FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan or plan['status'] != 'draft':
            return "Plan not found or not in draft status."
        async with conn.transaction():
            await conn.execute(
                "UPDATE production_plan SET status='approved', approved_by=$2, approved_at=NOW() WHERE plan_id=$1",
                plan_id, approved_by
            )
            import sys
            sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
            from app.modules.production.services.mrp import run_mrp
            from app.modules.production.services.indent_manager import generate_draft_indents
            mrp = await run_mrp(conn, plan_id, plan['entity'])
            indents = await generate_draft_indents(conn, mrp, plan_id, plan['entity'])
    return json.dumps({
        "plan_id": plan_id,
        "status": "approved",
        "mrp_summary": mrp["summary"],
        "draft_indents": indents["indents"],
    }, default=str, indent=2)


# ── Indents (view + send) ────────────────────────────────────────────────

@mcp.tool()
async def list_indents(entity: str = "", status: str = "") -> str:
    """List purchase indents."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM purchase_indent WHERE {where} ORDER BY created_at DESC LIMIT 50",
            *params
        )
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def edit_indent(indent_id: int, required_qty_kg: float = 0, required_by_date: str = "", priority: int = 0) -> str:
    """Edit a draft indent before sending."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        indent = await conn.fetchrow("SELECT status FROM purchase_indent WHERE indent_id=$1", indent_id)
        if not indent or indent['status'] != 'draft':
            return "Indent not found or not draft."
        updates, params, idx = [], [], 1
        if required_qty_kg > 0:
            updates.append(f"required_qty_kg=${idx}"); params.append(required_qty_kg); idx += 1
        if required_by_date:
            # FIX: parse to date object for asyncpg compatibility
            updates.append(f"required_by_date=${idx}"); params.append(date.fromisoformat(required_by_date)); idx += 1
        if priority > 0:
            updates.append(f"priority=${idx}"); params.append(priority); idx += 1
        if not updates:
            return "No fields to update."
        params.append(indent_id)
        await conn.execute(f"UPDATE purchase_indent SET {','.join(updates)} WHERE indent_id=${idx}", *params)
    return f"Indent {indent_id} updated."


@mcp.tool()
async def send_indent(indent_id: int) -> str:
    """Send a draft indent to purchase team."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        indent = await conn.fetchrow("SELECT * FROM purchase_indent WHERE indent_id=$1 AND status='draft'", indent_id)
        if not indent:
            return "Indent not found or not draft."
        async with conn.transaction():
            await conn.execute("UPDATE purchase_indent SET status='raised' WHERE indent_id=$1", indent_id)
            mat = indent['material_sku_name']
            qty = float(indent['required_qty_kg'])
            await conn.execute(
                "INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity) VALUES ('material_shortage','purchase',$1,$2,'indent',$3)",
                f"SHORTAGE: {mat} — {qty:.1f} kg", indent_id, indent['entity']
            )
            await conn.execute(
                "INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity) VALUES ('indent_raised','stores',$1,$2,'indent',$3)",
                f"Indent for {mat} — {qty:.1f} kg", indent_id, indent['entity']
            )
    return f"Indent {indent['indent_number']} sent. 2 alerts created."


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
                mat = indent['material_sku_name']
                qty = float(indent['required_qty_kg'])
                await conn.execute(
                    "INSERT INTO store_alert (alert_type,target_team,message,related_id,related_type,entity) VALUES ('material_shortage','purchase',$1,$2,'indent',$3)",
                    f"SHORTAGE: {mat} — {qty:.1f} kg", iid, indent['entity']
                )
                await conn.execute(
                    "INSERT INTO store_alert (alert_type,target_team,message,related_id,related_type,entity) VALUES ('indent_raised','stores',$1,$2,'indent',$3)",
                    f"Indent for {mat} — {qty:.1f} kg", iid, indent['entity']
                )
                sent += 1
    return f"Sent {sent} of {len(indent_ids)} indents."


@mcp.tool()
async def check_material_availability(material: str, qty_needed: float, entity: str) -> str:
    """Quick check if a material is available."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        on_hand = float(await conn.fetchval(
            "SELECT COALESCE(SUM(quantity_kg),0) FROM floor_inventory WHERE sku_name ILIKE $1 AND floor_location IN ('rm_store','pm_store') AND entity=$2",
            f"%{material}%", entity
        ) or 0)
        on_order = float(await conn.fetchval(
            "SELECT COALESCE(SUM(l.po_weight),0) FROM po_line l JOIN po_header h ON l.transaction_no=h.transaction_no WHERE l.sku_name ILIKE $1 AND h.status='pending'",
            f"%{material}%"
        ) or 0)
    avail = on_hand + on_order
    shortage = max(0, qty_needed - avail)
    return json.dumps({
        "material": material,
        "needed_kg": qty_needed,
        "on_hand_kg": on_hand,
        "on_order_kg": on_order,
        "available_kg": avail,
        "shortage_kg": shortage,
        "status": "SUFFICIENT" if shortage == 0 else "SHORTAGE",
    })


@mcp.tool()
async def get_machine_master(entity: str, floor: str = "", stage: str = "") -> str:
    """Get machine master with capacity info. Filter by floor or production stage."""
    pool = await get_pool()
    conditions = ["m.entity=$1"]; params = [entity]; idx = 2
    if floor:
        conditions.append(f"m.floor ILIKE ${idx}"); params.append(f"%{floor}%"); idx += 1
    if stage:
        conditions.append(f"mc.stage ILIKE ${idx}"); params.append(f"%{stage}%"); idx += 1
    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT m.machine_id, m.machine_name, m.floor, m.allocation, m.status, "
            f"mc.stage, mc.item_group, mc.capacity_kg_per_hr "
            f"FROM machine m LEFT JOIN machine_capacity mc ON m.machine_id=mc.machine_id "
            f"WHERE {where} ORDER BY m.floor, m.machine_name",
            *params
        )
    machines: dict = {}
    for r in rows:
        mid = r['machine_id']
        if mid not in machines:
            machines[mid] = {"machine_id": mid, "name": r['machine_name'], "floor": r['floor'],
                             "allocation": r['allocation'], "status": r['status'], "capacity": []}
        if r['stage']:
            machines[mid]["capacity"].append({"stage": r['stage'], "item_group": r['item_group'],
                                               "kg_per_hr": float(r['capacity_kg_per_hr'])})
    return json.dumps(list(machines.values()), default=str, indent=2)


@mcp.tool()
async def get_inventory(entity: str, floor_location: str = "", search: str = "") -> str:
    """Get current floor inventory. floor_location: rm_store, pm_store, fg_store, production_floor."""
    pool = await get_pool()
    conditions = ["entity=$1", "quantity_kg > 0"]; params = [entity]; idx = 2
    if floor_location:
        conditions.append(f"floor_location=${idx}"); params.append(floor_location); idx += 1
    if search:
        conditions.append(f"sku_name ILIKE ${idx}"); params.append(f"%{search}%"); idx += 1
    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT sku_name, floor_location, quantity_kg, uom, last_updated "
            f"FROM floor_inventory WHERE {where} ORDER BY floor_location, sku_name",
            *params
        )
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def get_bom_detail(fg_sku_name: str) -> str:
    """Get Bill of Materials for a finished good — materials, quantities, process route."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        header = await conn.fetchrow(
            "SELECT bom_id, fg_sku_name, process_category, output_uom, is_active FROM bom_header WHERE fg_sku_name ILIKE $1 AND is_active=TRUE LIMIT 1",
            f"%{fg_sku_name}%"
        )
        if not header:
            return json.dumps({"error": f"No active BOM found for '{fg_sku_name}'"})
        bom_id = header['bom_id']
        lines = await conn.fetch(
            "SELECT material_sku_name, item_type, quantity_per_unit, uom, loss_pct FROM bom_line WHERE bom_id=$1 ORDER BY item_type, material_sku_name",
            bom_id
        )
        route = await conn.fetch(
            "SELECT step_number, stage, process_name FROM bom_process_route WHERE bom_id=$1 ORDER BY step_number",
            bom_id
        )
    return json.dumps({
        "bom_id": header['bom_id'],
        "fg_sku_name": header['fg_sku_name'],
        "process_category": header['process_category'],
        "output_uom": header['output_uom'],
        "materials": [dict(l) for l in lines],
        "process_route": [dict(r) for r in route],
    }, default=str, indent=2)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
