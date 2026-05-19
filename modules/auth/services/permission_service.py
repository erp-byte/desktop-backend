"""Permission service — hierarchical permission checks with scope restrictions."""

import logging

logger = logging.getLogger(__name__)


async def check_permission(conn, role_id: int, is_admin: bool,
                            module: str, sub_module: str = None,
                            sub_sub_module: str = None, action: str = "view",
                            entity: str = None, warehouse: str = None,
                            floor: str = None) -> bool:
    """Check if a role has permission for a specific action.

    Permission matching is hierarchical:
    - Exact match on (module, sub_module, sub_sub_module, action) checked first
    - Falls back to (module, sub_module, NULL, action) if sub_sub_module not found
    - Falls back to (module, NULL, NULL, action) if sub_module not found

    Scope restrictions (entity, warehouse, floor) are checked after permission match.
    """

    # Admin bypasses all checks
    if is_admin:
        return True

    # Try exact match first, then progressively broader
    queries = []
    if sub_sub_module:
        queries.append((module, sub_module, sub_sub_module, action))
    if sub_module:
        queries.append((module, sub_module, None, action))
    queries.append((module, None, None, action))

    for mod, sub, subsub, act in queries:
        result = await conn.fetchrow(
            """
            SELECT rp.allowed_entities, rp.allowed_warehouses, rp.allowed_floors
            FROM auth_role_permission rp
            JOIN auth_permission p ON rp.permission_id = p.permission_id
            WHERE rp.role_id = $1
              AND p.module = $2
              AND p.sub_module IS NOT DISTINCT FROM $3
              AND p.sub_sub_module IS NOT DISTINCT FROM $4
              AND p.action = $5
            """,
            role_id, mod, sub, subsub, act,
        )

        if result:
            # HIGH-5: on a scope mismatch, return False immediately. Falling
            # through to broader permission rows is wrong because a broader row
            # with allowed_entities=NULL means "all entities" and is STRICTLY
            # MORE privileged, not less — so it would silently override the
            # narrower scoped restriction.
            if entity and result['allowed_entities']:
                if entity not in result['allowed_entities']:
                    return False
            if warehouse and result['allowed_warehouses']:
                if warehouse not in result['allowed_warehouses']:
                    return False
            if floor and result['allowed_floors']:
                if floor not in result['allowed_floors']:
                    return False
            return True

    return False


# MCP tool → permission mapping
MCP_TOOL_PERMISSIONS = {
    # Part 1
    "ping":                     ("production", None,          None,             "view"),
    # Part 2 — Fulfillment
    "sync_fulfillment":         ("production", "fulfillment", None,             "create"),
    "get_fulfillment_list":     ("production", "fulfillment", None,             "view"),
    "get_demand_summary":       ("production", "fulfillment", None,             "view"),
    "fy_review":                ("production", "fulfillment", None,             "view"),
    "carryforward_orders":      ("production", "fulfillment", "carryforward",   "create"),
    "revise_fulfillment":       ("production", "fulfillment", None,             "edit"),
    "cancel_fulfillment":       ("production", "fulfillment", None,             "delete"),
    # Part 2 — Plans
    "get_planning_context":     ("production", "plans",       None,             "view"),
    "save_production_plan":     ("production", "plans",       None,             "create"),
    "list_plans":               ("production", "plans",       None,             "view"),
    "get_plan_detail":          ("production", "plans",       None,             "view"),
    "create_manual_plan":       ("production", "plans",       None,             "create"),
    "edit_plan_line":           ("production", "plans",       None,             "edit"),
    "add_plan_line":            ("production", "plans",       None,             "edit"),
    "delete_plan_line":         ("production", "plans",       None,             "delete"),
    "approve_plan":             ("production", "plans",       "approve",        "create"),
    "cancel_plan":              ("production", "plans",       "cancel",         "create"),
    # Part 3 — MRP & Indent
    "run_mrp":                  ("production", "mrp",         None,             "create"),
    "check_material_availability": ("production", "mrp",     None,             "view"),
    "list_indents":             ("production", "indents",     None,             "view"),
    "get_indent_detail":        ("production", "indents",     None,             "view"),
    "edit_indent":              ("production", "indents",     None,             "edit"),
    "send_indent":              ("production", "indents",     "send",           "create"),
    "send_bulk_indents":        ("production", "indents",     "send",           "create"),
    "acknowledge_indent":       ("production", "indents",     "acknowledge",    "create"),
    "link_indent_to_po":        ("production", "indents",     "link_po",        "create"),
    "list_alerts":              ("production", "alerts",      None,             "view"),
    "mark_alert_read":          ("production", "alerts",      None,             "edit"),
    # Part 4 — Orders & Job Cards
    "create_production_orders": ("production", "orders",      None,             "create"),
    "list_orders":              ("production", "orders",      None,             "view"),
    "get_order_detail":         ("production", "orders",      None,             "view"),
    "generate_job_cards":       ("production", "job_cards",   "generate",       "create"),
    "list_job_cards":           ("production", "job_cards",   None,             "view"),
    "get_job_card_detail":      ("production", "job_cards",   None,             "view"),
    "team_dashboard":           ("production", "job_cards",   None,             "view"),
    "floor_dashboard":          ("production", "job_cards",   None,             "view"),
    "assign_job_card":          ("production", "job_cards",   "lifecycle",      "edit"),
    "receive_material_qr":      ("production", "job_cards",   "receive_material","create"),
    "start_job_card":           ("production", "job_cards",   "lifecycle",      "edit"),
    "complete_process_step":    ("production", "job_cards",   "lifecycle",      "edit"),
    "record_output":            ("production", "job_cards",   "output",         "create"),
    "complete_job_card":        ("production", "job_cards",   "lifecycle",      "edit"),
    "sign_off_job_card":        ("production", "job_cards",   "sign_offs",      "create"),
    "close_job_card":           ("production", "job_cards",   "close",          "create"),
    "force_unlock_job_card":    ("production", "job_cards",   "force_unlock",   "create"),
    "add_environment_data":     ("production", "job_cards",   "annexures",      "create"),
    "add_metal_detection":      ("production", "job_cards",   "annexures",      "create"),
    "add_weight_checks":        ("production", "job_cards",   "annexures",      "create"),
    "add_loss_reconciliation":  ("production", "job_cards",   "annexures",      "create"),
    "add_remarks":              ("production", "job_cards",   "annexures",      "create"),
    # Part 5 — Inventory
    "get_floor_inventory":      ("production", "inventory",   None,             "view"),
    "get_floor_summary":        ("production", "inventory",   None,             "view"),
    "move_material":            ("production", "inventory",   "move",           "create"),
    "get_movement_history":     ("production", "inventory",   None,             "view"),
    "check_idle_materials":     ("production", "inventory",   "idle",           "create"),
    "list_offgrade_inventory":  ("production", "offgrade",    None,             "view"),
    "list_offgrade_rules":      ("production", "offgrade",    None,             "view"),
    "create_offgrade_rule":     ("production", "offgrade",    "rules",          "create"),
    "get_loss_analysis":        ("production", "loss",        None,             "view"),
    "get_loss_anomalies":       ("production", "loss",        None,             "view"),
    # Part 6 — Day-End
    "get_day_end_summary":      ("production", "day_end",     None,             "view"),
    "submit_dispatch":          ("production", "day_end",     "dispatch",       "create"),
    "submit_balance_scan":      ("production", "day_end",     "scan",           "create"),
    "get_scan_status":          ("production", "day_end",     None,             "view"),
    "get_scan_detail":          ("production", "day_end",     None,             "view"),
    "reconcile_scan":           ("production", "day_end",     "reconcile",      "create"),
    "check_missing_scans":      ("production", "day_end",     "missing",        "create"),
    "get_yield_summary":        ("production", "yield",       None,             "view"),
    # Part 7 — Revision & Discrepancy
    "revise_plan":              ("production", "plans",       "revise",         "create"),
    "get_revision_history":     ("production", "plans",       None,             "view"),
    "report_discrepancy":       ("production", "discrepancy", None,             "create"),
    "list_discrepancies":       ("production", "discrepancy", None,             "view"),
    "get_discrepancy_detail":   ("production", "discrepancy", None,             "view"),
    "resolve_discrepancy":      ("production", "discrepancy", "resolve",        "create"),
    "list_ai_recommendations":  ("production", "ai",          None,             "view"),
    "submit_ai_feedback":       ("production", "ai",          None,             "edit"),
}


async def check_mcp_tool_permission(conn, role_id: int, is_admin: bool, tool_name: str) -> bool:
    """Check if a role can use a specific MCP tool."""
    perm = MCP_TOOL_PERMISSIONS.get(tool_name)
    if not perm:
        return is_admin  # Unknown tools: admin only

    module, sub_module, sub_sub_module, action = perm
    return await check_permission(conn, role_id, is_admin, module, sub_module, sub_sub_module, action)
