"""MCP Tracker Server — View-only access to all modules.

Standalone server. Runs on its own port/service.
Tools: 28 (all read-only across production, inventory, job cards, day-end, etc.)
"""

import json
import logging
import os
from datetime import date

import asyncpg
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_port = int(os.environ.get("PORT", "8003"))

mcp = FastMCP(
    "Candor Tracker (View Only)",
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


@mcp.tool()
async def ping() -> str:
    """Check connection."""
    pool = await get_pool()
    await pool.fetchval("SELECT 1")
    return "OK — Tracker MCP connected (view-only)"


# ── Fulfillment ───────────────────────────────────────────────────────────

@mcp.tool()
async def get_fulfillment_list(entity: str = "", status: str = "open,partial", page: int = 1, page_size: int = 50) -> str:
    """List fulfillment records."""
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
        rows = await conn.fetch(f"SELECT fulfillment_id, fg_sku_name, customer_name, pending_qty_kg, produced_qty_kg, dispatched_qty_kg, delivery_deadline, priority, order_status, financial_year FROM so_fulfillment WHERE {where} ORDER BY delivery_deadline LIMIT ${idx} OFFSET ${idx+1}", *params, page_size, offset)
    return json.dumps({"total": total, "page": page, "results": [dict(r) for r in rows]}, default=str, indent=2)


@mcp.tool()
async def get_demand_summary(entity: str = "") -> str:
    """Aggregated pending demand by product and customer."""
    pool = await get_pool()
    query = "SELECT fg_sku_name, customer_name, SUM(pending_qty_kg) AS total_qty_kg, COUNT(*) AS order_count, MIN(delivery_deadline) AS earliest_deadline FROM so_fulfillment WHERE order_status IN ('open','partial')"
    params = []; idx = 1
    if entity:
        query += f" AND entity=${idx}"; params.append(entity)
    query += " GROUP BY fg_sku_name, customer_name ORDER BY MIN(delivery_deadline)"
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ── Plans ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_plans(entity: str = "", status: str = "") -> str:
    """List production plans."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT plan_id, plan_name, entity, plan_type, plan_date, status, ai_generated, revision_number, created_at FROM production_plan WHERE {where} ORDER BY created_at DESC LIMIT 20", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def get_plan_detail(plan_id: int) -> str:
    """Get plan with all lines."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchrow("SELECT * FROM production_plan WHERE plan_id=$1", plan_id)
        if not plan: return "Plan not found."
        lines = await conn.fetch("SELECT * FROM production_plan_line WHERE plan_id=$1 ORDER BY priority", plan_id)
    result = dict(plan); result["lines"] = [dict(l) for l in lines]
    ai = result.get("ai_analysis_json")
    if ai:
        if isinstance(ai, str): ai = json.loads(ai)
        result["material_check"] = ai.get("material_check", [])
        result["risk_flags"] = ai.get("risk_flags", [])
    return json.dumps(result, default=str, indent=2)


# ── Indents & Alerts ──────────────────────────────────────────────────────

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
        rows = await conn.fetch(f"SELECT * FROM purchase_indent WHERE {where} ORDER BY created_at DESC LIMIT 50", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def list_alerts(target_team: str = "", entity: str = "") -> str:
    """List store alerts."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if target_team:
        conditions.append(f"target_team=${idx}"); params.append(target_team); idx += 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM store_alert WHERE {where} ORDER BY created_at DESC LIMIT 50", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ── Orders & Job Cards ────────────────────────────────────────────────────

@mcp.tool()
async def list_orders(entity: str = "", status: str = "") -> str:
    """List production orders."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM production_order WHERE {where} ORDER BY created_at DESC LIMIT 50", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def get_order_detail(prod_order_id: int) -> str:
    """Get order with job cards."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM production_order WHERE prod_order_id=$1", prod_order_id)
        if not order: return "Order not found."
        jcs = await conn.fetch("SELECT job_card_id, job_card_number, step_number, process_name, stage, status, is_locked FROM job_card WHERE prod_order_id=$1 ORDER BY step_number", prod_order_id)
    result = dict(order); result["job_cards"] = [dict(j) for j in jcs]
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def list_job_cards(entity: str = "", status: str = "", team_leader: str = "", floor: str = "") -> str:
    """List job cards."""
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
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM job_card WHERE {where}", *params)
        rows = await conn.fetch(f"SELECT job_card_id, job_card_number, step_number, process_name, fg_sku_name, customer_name, batch_number, batch_size_kg, assigned_to_team_leader, status, is_locked, factory, floor, entity FROM job_card WHERE {where} ORDER BY created_at DESC LIMIT 50", *params)
    return json.dumps({"total": total, "results": [dict(r) for r in rows]}, default=str, indent=2)


@mcp.tool()
async def get_job_card_detail(job_card_id: int) -> str:
    """Get full job card detail with all sections and annexures."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id=$1", job_card_id)
        if not jc: return "Job card not found."
        rm = await conn.fetch("SELECT * FROM job_card_rm_indent WHERE job_card_id=$1", job_card_id)
        pm = await conn.fetch("SELECT * FROM job_card_pm_indent WHERE job_card_id=$1", job_card_id)
        steps = await conn.fetch("SELECT * FROM job_card_process_step WHERE job_card_id=$1 ORDER BY step_number", job_card_id)
        output = await conn.fetchrow("SELECT * FROM job_card_output WHERE job_card_id=$1", job_card_id)
        sign_offs = await conn.fetch("SELECT * FROM job_card_sign_off WHERE job_card_id=$1", job_card_id)
        env = await conn.fetch("SELECT * FROM job_card_environment WHERE job_card_id=$1", job_card_id)
        metal = await conn.fetch("SELECT * FROM job_card_metal_detection WHERE job_card_id=$1", job_card_id)
        weight = await conn.fetch("SELECT * FROM job_card_weight_check WHERE job_card_id=$1 ORDER BY sample_number", job_card_id)
        loss_recon = await conn.fetch("SELECT * FROM job_card_loss_reconciliation WHERE job_card_id=$1", job_card_id)
        remarks = await conn.fetch("SELECT * FROM job_card_remarks WHERE job_card_id=$1", job_card_id)
    result = dict(jc)
    result["rm_indent"] = [dict(r) for r in rm]
    result["pm_indent"] = [dict(r) for r in pm]
    result["process_steps"] = [dict(r) for r in steps]
    result["output"] = dict(output) if output else None
    result["sign_offs"] = [dict(r) for r in sign_offs]
    result["environment"] = [dict(r) for r in env]
    result["metal_detection"] = [dict(r) for r in metal]
    result["weight_checks"] = [dict(r) for r in weight]
    result["loss_reconciliation"] = [dict(r) for r in loss_recon]
    result["remarks"] = [dict(r) for r in remarks]
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
async def team_dashboard(team_leader: str, entity: str = "") -> str:
    """Job cards for a team leader."""
    pool = await get_pool()
    conditions = ["assigned_to_team_leader ILIKE $1"]; params = [f"%{team_leader}%"]
    if entity:
        conditions.append("entity=$2"); params.append(entity)
    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM job_card WHERE {where} AND status NOT IN ('closed') ORDER BY CASE status WHEN 'in_progress' THEN 1 WHEN 'material_received' THEN 2 WHEN 'assigned' THEN 3 ELSE 4 END", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def floor_dashboard(floor: str, entity: str = "") -> str:
    """All job cards on a floor."""
    pool = await get_pool()
    conditions = ["floor ILIKE $1"]; params = [f"%{floor}%"]
    if entity:
        conditions.append("entity=$2"); params.append(entity)
    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM job_card WHERE {where} ORDER BY status", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ── Inventory ─────────────────────────────────────────────────────────────

@mcp.tool()
async def get_floor_inventory(entity: str, floor_location: str = "", search: str = "") -> str:
    """List floor inventory."""
    pool = await get_pool()
    conditions = ["entity=$1", "quantity_kg > 0"]; params = [entity]; idx = 2
    if floor_location:
        conditions.append(f"floor_location=${idx}"); params.append(floor_location); idx += 1
    if search:
        conditions.append(f"sku_name ILIKE ${idx}"); params.append(f"%{search}%"); idx += 1
    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM floor_inventory WHERE {where} ORDER BY quantity_kg DESC LIMIT 100", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def get_floor_summary(entity: str) -> str:
    """Aggregated stock per floor."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT floor_location, COUNT(DISTINCT sku_name) as items, COALESCE(SUM(quantity_kg),0) as total_kg FROM floor_inventory WHERE entity=$1 AND quantity_kg>0 GROUP BY floor_location", entity)
    return json.dumps([{"floor": r['floor_location'], "items": r['items'], "total_kg": float(r['total_kg'])} for r in rows])


@mcp.tool()
async def get_movement_history(entity: str, sku_name: str = "", page: int = 1, page_size: int = 50) -> str:
    """Floor movement audit trail."""
    pool = await get_pool()
    conditions = ["entity=$1"]; params = [entity]; idx = 2
    if sku_name:
        conditions.append(f"sku_name ILIKE ${idx}"); params.append(f"%{sku_name}%"); idx += 1
    where = " AND ".join(conditions)
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM floor_movement WHERE {where} ORDER BY moved_at DESC LIMIT ${idx} OFFSET ${idx+1}", *params, page_size, offset)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def list_offgrade_inventory(entity: str = "", status: str = "available") -> str:
    """List off-grade inventory."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM offgrade_inventory WHERE {where} ORDER BY created_at DESC LIMIT 100", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


# ── Loss & Day-End ────────────────────────────────────────────────────────

@mcp.tool()
async def get_loss_analysis(entity: str = "", group_by: str = "product") -> str:
    """Loss analysis grouped by product, stage, month, or machine."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    col = {"product": "product_name", "stage": "stage", "month": "TO_CHAR(production_date,'YYYY-MM')", "machine": "machine_name"}.get(group_by, "product_name")
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {col} AS group_key, COUNT(*) AS batches, ROUND(AVG(loss_pct)::numeric,3) AS avg_loss_pct, ROUND(SUM(loss_kg)::numeric,3) AS total_loss_kg FROM process_loss WHERE {where} GROUP BY {col} ORDER BY SUM(loss_kg) DESC", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def get_day_end_summary(entity: str, target_date: str = "") -> str:
    """Today's completed orders with dispatch data."""
    pool = await get_pool()
    d = target_date or str(date.today())
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT jc.job_card_id, jc.job_card_number, jc.fg_sku_name, jc.customer_name, jc.batch_size_kg, o.fg_actual_kg, o.process_loss_kg, o.offgrade_kg, o.dispatch_qty FROM job_card jc JOIN production_order po ON jc.prod_order_id=po.prod_order_id LEFT JOIN job_card_output o ON jc.job_card_id=o.job_card_id WHERE jc.entity=$1 AND jc.step_number=po.total_stages AND jc.status IN ('completed','closed') AND DATE(jc.end_time)=$2::date", entity, d)
    items = [dict(r) for r in rows]
    return json.dumps({"date": d, "completed_orders": len(items), "total_fg_kg": sum(float(r.get('fg_actual_kg') or 0) for r in items), "total_dispatch_kg": sum(float(r.get('dispatch_qty') or 0) for r in items), "items": items}, default=str, indent=2)


@mcp.tool()
async def get_scan_status(entity: str, target_date: str = "") -> str:
    """Balance scan status per floor for today."""
    pool = await get_pool()
    d = target_date or str(date.today())
    floors = ['rm_store', 'pm_store', 'production_floor', 'fg_store']
    results = []
    async with pool.acquire() as conn:
        for f in floors:
            scan = await conn.fetchrow("SELECT scan_id, status, submitted_by, submitted_at FROM day_end_balance_scan WHERE floor_location=$1 AND scan_date=$2::date AND entity=$3", f, d, entity)
            results.append({"floor": f, "submitted": scan is not None, "status": scan['status'] if scan else "pending", "submitted_by": scan['submitted_by'] if scan else None})
    return json.dumps(results, default=str, indent=2)


# ── Discrepancy ───────────────────────────────────────────────────────────

@mcp.tool()
async def list_discrepancies(entity: str = "", status: str = "") -> str:
    """List discrepancy reports."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM discrepancy_report WHERE {where} ORDER BY created_at DESC LIMIT 50", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


@mcp.tool()
async def list_ai_recommendations(entity: str = "") -> str:
    """List AI recommendations."""
    pool = await get_pool()
    conditions, params, idx = [], [], 1
    if entity:
        conditions.append(f"entity=${idx}"); params.append(entity); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT recommendation_id, recommendation_type, entity, tokens_used, status, feedback, plan_id, created_at FROM ai_recommendation WHERE {where} ORDER BY created_at DESC LIMIT 20", *params)
    return json.dumps([dict(r) for r in rows], default=str, indent=2)


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
