"""Restricted MCP Server — View + Limited Create access only.

Exposes 34 of 77 tools. No dangerous writes (approve, cancel, delete,
force-unlock, dispatch, reconcile, resolve). Safe for viewer/monitor roles.

Mount at /mcp-viewer/ alongside full MCP at /mcp/.
"""

import json
import logging
import os
from datetime import date

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp_viewer = FastMCP("Candor Foods Viewer (Read-Only)")

# Reuse the DB pool from the full MCP server
from mcp_server import get_pool  # noqa: E402


# ---------------------------------------------------------------------------
# Part 1: Health
# ---------------------------------------------------------------------------

@mcp_viewer.tool()
async def ping() -> str:
    """Check if the MCP server and database are connected."""
    pool = await get_pool()
    val = await pool.fetchval("SELECT 1")
    return f"OK — DB connected (result={val}), viewer mode (read-only)"


# ---------------------------------------------------------------------------
# Part 2: Fulfillment & Plans (read + safe sync)
# ---------------------------------------------------------------------------

@mcp_viewer.tool()
async def sync_fulfillment(entity: str = "") -> str:
    """Sync all FG Sales Order lines into so_fulfillment table. Safe/idempotent."""
    from mcp_server import sync_fulfillment as _fn
    return await _fn(entity)


@mcp_viewer.tool()
async def get_planning_context(entity: str, fulfillment_ids: list[int], target_date: str = "") -> str:
    """Get full planning context (demand, inventory, machines, BOMs) for reviewing production needs."""
    from mcp_server import get_planning_context as _fn
    return await _fn(entity, fulfillment_ids, target_date)


@mcp_viewer.tool()
async def get_demand_summary(entity: str = "", financial_year: str = "") -> str:
    """Get aggregated pending demand grouped by product and customer."""
    from mcp_server import get_demand_summary as _fn
    return await _fn(entity, financial_year)


@mcp_viewer.tool()
async def get_fulfillment_list(entity: str = "", status: str = "open,partial", page: int = 1, page_size: int = 50) -> str:
    """Get paginated list of fulfillment records."""
    from mcp_server import get_fulfillment_list as _fn
    return await _fn(entity, status, page, page_size)


@mcp_viewer.tool()
async def fy_review(entity: str = "", financial_year: str = "") -> str:
    """Get all unfulfilled orders for FY close review."""
    from mcp_server import fy_review as _fn
    return await _fn(entity, financial_year)


@mcp_viewer.tool()
async def list_plans(entity: str = "", status: str = "", plan_type: str = "") -> str:
    """List existing production plans."""
    from mcp_server import list_plans as _fn
    return await _fn(entity, status, plan_type)


@mcp_viewer.tool()
async def get_plan_detail(plan_id: int) -> str:
    """Get a production plan with all its lines, material check, and risk flags."""
    from mcp_server import get_plan_detail as _fn
    return await _fn(plan_id)


# ---------------------------------------------------------------------------
# Part 3: MRP & Indent (read-only)
# ---------------------------------------------------------------------------

@mcp_viewer.tool()
async def check_material_availability(material: str, qty_needed: float, entity: str) -> str:
    """Quick check if a material is available in sufficient quantity."""
    from mcp_server import check_material_availability as _fn
    return await _fn(material, qty_needed, entity)


@mcp_viewer.tool()
async def list_indents(entity: str = "", status: str = "", page: int = 1, page_size: int = 50) -> str:
    """List purchase indents."""
    from mcp_server import list_indents as _fn
    return await _fn(entity, status, page, page_size)


@mcp_viewer.tool()
async def get_indent_detail(indent_id: int) -> str:
    """Get indent detail with linked plan line info."""
    from mcp_server import get_indent_detail as _fn
    return await _fn(indent_id)


@mcp_viewer.tool()
async def list_alerts(target_team: str = "", entity: str = "", is_read: str = "") -> str:
    """List store alerts."""
    from mcp_server import list_alerts as _fn
    return await _fn(target_team, entity, is_read)


# ---------------------------------------------------------------------------
# Part 4: Job Cards (read-only)
# ---------------------------------------------------------------------------

@mcp_viewer.tool()
async def list_orders(entity: str = "", status: str = "", page: int = 1, page_size: int = 50) -> str:
    """List production orders."""
    from mcp_server import list_orders as _fn
    return await _fn(entity, status, page, page_size)


@mcp_viewer.tool()
async def get_order_detail(prod_order_id: int) -> str:
    """Get production order detail with all its job cards."""
    from mcp_server import get_order_detail as _fn
    return await _fn(prod_order_id)


@mcp_viewer.tool()
async def list_job_cards(entity: str = "", status: str = "", team_leader: str = "", floor: str = "", page: int = 1, page_size: int = 50) -> str:
    """List job cards with filters."""
    from mcp_server import list_job_cards as _fn
    return await _fn(entity, status, team_leader, floor, page, page_size)


@mcp_viewer.tool()
async def get_job_card_detail(job_card_id: int) -> str:
    """Get full job card detail — all sections and annexures."""
    from mcp_server import get_job_card_detail as _fn
    return await _fn(job_card_id)


@mcp_viewer.tool()
async def team_dashboard(team_leader: str, entity: str = "") -> str:
    """Get job cards assigned to a specific team leader, priority-sorted."""
    from mcp_server import team_dashboard as _fn
    return await _fn(team_leader, entity)


@mcp_viewer.tool()
async def floor_dashboard(floor: str, entity: str = "") -> str:
    """Get all job cards on a specific floor."""
    from mcp_server import floor_dashboard as _fn
    return await _fn(floor, entity)


# ---------------------------------------------------------------------------
# Part 5: Inventory (read-only)
# ---------------------------------------------------------------------------

@mcp_viewer.tool()
async def get_floor_inventory(entity: str, floor_location: str = "", search: str = "", page: int = 1, page_size: int = 50) -> str:
    """List floor inventory items."""
    from mcp_server import get_floor_inventory as _fn
    return await _fn(entity, floor_location, search, page, page_size)


@mcp_viewer.tool()
async def get_floor_summary(entity: str) -> str:
    """Get aggregated stock per floor location."""
    from mcp_server import get_floor_summary as _fn
    return await _fn(entity)


@mcp_viewer.tool()
async def get_movement_history(entity: str, sku_name: str = "", from_location: str = "", to_location: str = "", page: int = 1, page_size: int = 50) -> str:
    """Get floor movement audit trail."""
    from mcp_server import get_movement_history as _fn
    return await _fn(entity, sku_name, from_location, to_location, page, page_size)


@mcp_viewer.tool()
async def check_idle_materials(entity: str) -> str:
    """Check for materials idle 3+ days on any floor."""
    from mcp_server import check_idle_materials as _fn
    return await _fn(entity)


@mcp_viewer.tool()
async def list_offgrade_inventory(entity: str = "", status: str = "available", item_group: str = "") -> str:
    """List off-grade inventory."""
    from mcp_server import list_offgrade_inventory as _fn
    return await _fn(entity, status, item_group)


@mcp_viewer.tool()
async def list_offgrade_rules() -> str:
    """List all off-grade reuse rules."""
    from mcp_server import list_offgrade_rules as _fn
    return await _fn()


@mcp_viewer.tool()
async def get_loss_analysis(entity: str = "", group_by: str = "product", product_name: str = "", stage: str = "") -> str:
    """Get loss analysis with aggregation."""
    from mcp_server import get_loss_analysis as _fn
    return await _fn(entity, group_by, product_name, stage)


@mcp_viewer.tool()
async def get_loss_anomalies(entity: str = "", threshold_multiplier: float = 2.0) -> str:
    """Find batches with loss significantly above average."""
    from mcp_server import get_loss_anomalies as _fn
    return await _fn(entity, threshold_multiplier)


# ---------------------------------------------------------------------------
# Part 6: Day-End (read-only)
# ---------------------------------------------------------------------------

@mcp_viewer.tool()
async def get_day_end_summary(entity: str, target_date: str = "") -> str:
    """Get today's completed final-stage job cards with output data."""
    from mcp_server import get_day_end_summary as _fn
    return await _fn(entity, target_date)


@mcp_viewer.tool()
async def get_scan_status(entity: str, target_date: str = "") -> str:
    """Get today's balance scan submission status per floor."""
    from mcp_server import get_scan_status as _fn
    return await _fn(entity, target_date)


@mcp_viewer.tool()
async def get_scan_detail(scan_id: int) -> str:
    """Get balance scan detail with all line items."""
    from mcp_server import get_scan_detail as _fn
    return await _fn(scan_id)


@mcp_viewer.tool()
async def get_yield_summary(entity: str = "", product_name: str = "", period: str = "") -> str:
    """Get yield summary by product/period."""
    from mcp_server import get_yield_summary as _fn
    return await _fn(entity, product_name, period)


# ---------------------------------------------------------------------------
# Part 7: Discrepancy & AI (read-only)
# ---------------------------------------------------------------------------

@mcp_viewer.tool()
async def get_revision_history(plan_id: int) -> str:
    """Get the chain of revisions for a plan."""
    from mcp_server import get_revision_history as _fn
    return await _fn(plan_id)


@mcp_viewer.tool()
async def list_discrepancies(entity: str = "", status: str = "", discrepancy_type: str = "") -> str:
    """List discrepancy reports."""
    from mcp_server import list_discrepancies as _fn
    return await _fn(entity, status, discrepancy_type)


@mcp_viewer.tool()
async def get_discrepancy_detail(discrepancy_id: int) -> str:
    """Get discrepancy detail with affected job cards."""
    from mcp_server import get_discrepancy_detail as _fn
    return await _fn(discrepancy_id)


@mcp_viewer.tool()
async def list_ai_recommendations(entity: str = "", recommendation_type: str = "", status: str = "") -> str:
    """List all AI recommendations."""
    from mcp_server import list_ai_recommendations as _fn
    return await _fn(entity, recommendation_type, status)
