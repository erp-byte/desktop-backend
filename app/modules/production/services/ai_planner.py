"""Claude AI Plan Generation — collects context, calls Claude, creates production plans.

Flow:
  1. collect_planning_context() — gathers demand, inventory, machines, BOMs
  2. call_claude() — sends context to Claude, gets structured schedule
  3. create_plan_from_ai() — inserts production_plan + plan_lines from response
"""

import json
import logging
import time
from datetime import date

import anthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

DAILY_PLAN_PROMPT = """You are a production planner for Candor Foods Pvt. Ltd., a dry fruits, nuts, and snacks processing company.

Given the pending demand, available inventory, machine capacity, and BOMs, create a daily production schedule.

RULES:
1. Prioritize by delivery_deadline (earliest first), then by priority number (1=highest).
2. For PRODUCTION type orders: schedule the full process route (e.g. sorting → roasting → packaging).
3. For REPACKAGING type orders: schedule packaging-only route, source FG from fg_store inventory.
4. Respect machine capacity — do not schedule more kg than a machine can handle in one shift (8 hours).
5. Flag any material shortages (RM, PM, or source FG for repackaging).
6. Prefer assigning the same product to the same machine (minimize changeover time).
7. Include carryforward orders with higher priority.
8. If demand exceeds capacity, schedule what fits and flag the overflow.

IMPORTANT: Return ONLY valid JSON, no markdown, no explanation outside JSON.

Return this exact JSON structure:
{
  "schedule": [
    {
      "fg_sku_name": "Product Name",
      "customer_name": "Customer",
      "qty_kg": 500,
      "qty_units": 2000,
      "bom_id": 15,
      "production_type": "production",
      "machine_name": "Machine Name",
      "priority": 1,
      "shift": "day",
      "stage_sequence": ["sorting", "roasting", "packaging"],
      "estimated_hours": 5.0,
      "linked_fulfillment_ids": [42],
      "reasoning": "Brief explanation of why this was scheduled this way"
    }
  ],
  "material_check": [
    {
      "material": "Material Name",
      "type": "rm",
      "needed_kg": 525,
      "available_kg": 2000,
      "status": "SUFFICIENT"
    }
  ],
  "risk_flags": [
    {
      "flag": "Description",
      "severity": "warning",
      "details": "More details"
    }
  ]
}
"""

WEEKLY_PLAN_PROMPT = """You are a production planner for Candor Foods Pvt. Ltd., a dry fruits, nuts, and snacks processing company.

Given the pending demand, available inventory, machine capacity, and BOMs, create a WEEKLY production schedule spanning multiple days.

RULES:
1. Spread production across the week to balance machine load.
2. Prioritize by delivery_deadline (earliest first), then by priority number.
3. For PRODUCTION type: schedule full process route. For REPACKAGING: packaging-only.
4. Respect daily machine capacity (8 hours per shift per machine).
5. Flag material shortages and suggest procurement timeline.
6. Group similar products on same machine on same day to minimize changeover.

IMPORTANT: Return ONLY valid JSON, no markdown.

Return this JSON structure:
{
  "schedule": [
    {
      "date": "2026-04-01",
      "fg_sku_name": "Product Name",
      "customer_name": "Customer",
      "qty_kg": 500,
      "qty_units": 2000,
      "bom_id": 15,
      "production_type": "production",
      "machine_name": "Machine Name",
      "priority": 1,
      "shift": "day",
      "stage_sequence": ["sorting", "roasting", "packaging"],
      "estimated_hours": 5.0,
      "linked_fulfillment_ids": [42],
      "reasoning": "Brief explanation"
    }
  ],
  "material_check": [
    {"material": "Name", "type": "rm", "needed_kg": 525, "available_kg": 2000, "status": "SUFFICIENT"}
  ],
  "risk_flags": [
    {"flag": "Description", "severity": "warning", "details": "Details"}
  ]
}
"""

PLAN_REVISION_PROMPT = """You are a production planner for Candor Foods Pvt. Ltd., a dry fruits, nuts, and snacks processing company.

You are revising an EXISTING approved production plan due to a change event. You will receive:
1. The current plan with all line statuses (which are in_progress, completed, locked, etc.)
2. Active job cards on the floor
3. The change event (new SO, material arrival, shortage, machine breakdown, etc.)
4. Current inventory and machine availability

RULES:
1. Do NOT reschedule lines that are already in_progress or completed — those stay as-is.
2. You CAN reschedule/cancel lines that are still 'planned' (not yet started).
3. Add new lines for adhoc orders if needed.
4. Respect machine capacity (8 hours per shift).
5. Update priorities based on the change event.
6. Flag any new material shortages.
7. Provide clear reasoning for each change.

IMPORTANT: Return ONLY valid JSON, no markdown.

Return this JSON structure:
{
  "revised_schedule": [
    {
      "action": "keep",
      "plan_line_id": 1,
      "reasoning": "Already in progress, no change needed"
    },
    {
      "action": "reschedule",
      "plan_line_id": 2,
      "new_priority": 3,
      "new_machine_name": "Machine B",
      "new_shift": "night",
      "reasoning": "Moved to night shift to accommodate adhoc order"
    },
    {
      "action": "cancel",
      "plan_line_id": 3,
      "reasoning": "Material no longer available due to QC failure"
    },
    {
      "action": "add",
      "fg_sku_name": "New Product 500g",
      "customer_name": "Customer",
      "qty_kg": 300,
      "bom_id": 42,
      "machine_name": "Machine A",
      "priority": 1,
      "shift": "day",
      "stage_sequence": ["sorting", "packaging"],
      "estimated_hours": 3.0,
      "linked_fulfillment_ids": [99],
      "reasoning": "Adhoc order from customer, urgent deadline"
    }
  ],
  "material_check": [
    {"material": "Name", "type": "rm", "needed_kg": 300, "available_kg": 500, "status": "SUFFICIENT"}
  ],
  "risk_flags": [
    {"flag": "Description", "severity": "warning", "details": "Details"}
  ]
}
"""


# ---------------------------------------------------------------------------
# Context collection
# ---------------------------------------------------------------------------

async def collect_planning_context(conn, entity: str, target_date: date,
                                    fulfillment_ids: list[int]) -> dict:
    """Gather all data Claude needs for plan generation."""

    # 1. Demand — only selected fulfillment records
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
        # Find BOM
        bom = await conn.fetchrow(
            "SELECT bom_id, process_category FROM bom_header WHERE fg_sku_name = $1 AND is_active = TRUE LIMIT 1",
            r['fg_sku_name'],
        )
        if not bom:
            no_bom.append({"fulfillment_id": r['fulfillment_id'], "fg_sku_name": r['fg_sku_name']})
            continue

        bom_id = bom['bom_id']

        # Check BOM structure — RM present = production, only PM/FG = repackaging
        rm_count = await conn.fetchval(
            "SELECT COUNT(*) FROM bom_line WHERE bom_id = $1 AND item_type = 'rm'", bom_id,
        )
        production_type = 'production' if rm_count > 0 else 'repackaging'

        # Get process route
        route_rows = await conn.fetch(
            "SELECT process_name, stage FROM bom_process_route WHERE bom_id = $1 ORDER BY step_number",
            bom_id,
        )
        process_route = [r2['stage'] for r2 in route_rows]

        # Get materials
        mat_rows = await conn.fetch(
            "SELECT material_sku_name, item_type, quantity_per_unit, uom, loss_pct FROM bom_line WHERE bom_id = $1",
            bom_id,
        )
        qty_kg = float(r['pending_qty_kg'])
        materials = []
        for m in mat_rows:
            need = qty_kg * float(m['quantity_per_unit'])
            loss = float(m['loss_pct'] or 0)
            gross = need / (1 - loss / 100) if loss < 100 else need
            materials.append({
                "name": m['material_sku_name'],
                "type": m['item_type'],
                "need_qty": round(gross, 3),
                "uom": m['uom'],
                "loss_pct": loss,
            })

        demand.append({
            "fulfillment_id": r['fulfillment_id'],
            "fg_sku_name": r['fg_sku_name'],
            "customer": r['customer_name'],
            "qty_kg": qty_kg,
            "deadline": str(r['delivery_deadline']),
            "priority": r['priority'],
            "production_type": production_type,
            "bom_id": bom_id,
            "process_route": process_route or ['packaging'],
            "materials": materials,
        })

    # 2. Inventory
    inv_rows = await conn.fetch(
        "SELECT sku_name, item_type, floor_location, SUM(quantity_kg) as qty_kg "
        "FROM floor_inventory WHERE entity = $1 GROUP BY sku_name, item_type, floor_location",
        entity,
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
        FROM machine m
        LEFT JOIN machine_capacity mc ON m.machine_id = mc.machine_id
        WHERE m.entity = $1 AND m.status = 'active'
        ORDER BY m.machine_name
        """,
        entity,
    )
    machines_map: dict[int, dict] = {}
    for r in machine_rows:
        mid = r['machine_id']
        if mid not in machines_map:
            machines_map[mid] = {
                "name": r['machine_name'],
                "floor": r['floor'],
                "allocation": r['allocation'],
                "capacity": [],
            }
        if r['stage']:
            machines_map[mid]["capacity"].append({
                "group": r['item_group'],
                "stage": r['stage'],
                "kg_hr": float(r['capacity_kg_per_hr']),
            })

    # 4. In-progress job cards
    jobs = await conn.fetch(
        """
        SELECT job_card_number, fg_sku_name, stage, status, batch_size_kg
        FROM job_card WHERE entity = $1 AND status IN ('in_progress', 'unlocked', 'assigned')
        """,
        entity,
    )

    # 5. Pending indents
    indents = await conn.fetch(
        """
        SELECT material_sku_name, required_qty_kg, required_by_date, status
        FROM purchase_indent WHERE entity = $1 AND status IN ('raised', 'acknowledged')
        """,
        entity,
    )

    context = {
        "date": str(target_date),
        "entity": entity,
        "demand": demand,
        "no_bom_items": no_bom,
        "inventory": inventory,
        "machines": list(machines_map.values()),
        "in_progress_jobs": [dict(j) for j in jobs],
        "pending_indents": [dict(i) for i in indents],
    }

    logger.info("Planning context: %d demand items, %d no-BOM, %d machines",
                len(demand), len(no_bom), len(machines_map))
    return context


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

async def call_claude(settings, system_prompt: str, user_data: dict) -> dict:
    """Call Claude API with planning context, parse JSON response."""

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    user_msg = json.dumps(user_data, default=str)

    start = time.time()
    response = await client.messages.create(
        model=settings.CLAUDE_MODEL,
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    latency_ms = int((time.time() - start) * 1000)

    # Extract text content
    text = response.content[0].text
    tokens_used = response.usage.input_tokens + response.usage.output_tokens

    # Parse JSON — strip markdown fences if present
    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Failed to parse Claude response as JSON: %s", text[:500])
        parsed = {"schedule": [], "material_check": [], "risk_flags": [
            {"flag": "AI response parse error", "severity": "error", "details": text[:500]}
        ]}

    logger.info("Claude call: %d tokens, %dms latency", tokens_used, latency_ms)
    return {
        "parsed": parsed,
        "raw_text": text,
        "tokens_used": tokens_used,
        "latency_ms": latency_ms,
    }


# ---------------------------------------------------------------------------
# Plan creation from AI response
# ---------------------------------------------------------------------------

async def create_plan_from_ai(conn, entity: str, plan_type: str,
                               date_from: date, date_to: date,
                               ai_result: dict, settings) -> dict:
    """Create production_plan + plan_lines from Claude's response. Log to ai_recommendation."""

    parsed = ai_result["parsed"]
    schedule = parsed.get("schedule", [])

    # Insert plan header
    plan_id = await conn.fetchval(
        """
        INSERT INTO production_plan (
            plan_name, entity, plan_type, plan_date, date_from, date_to,
            status, ai_generated, ai_analysis_json
        ) VALUES ($1, $2, $3, $4, $5, $6, 'draft', TRUE, $7)
        RETURNING plan_id
        """,
        f"{plan_type.title()} Plan — {date_from}",
        entity, plan_type, date_from, date_from, date_to,
        json.dumps(parsed, default=str),
    )

    # Build machine name → id lookup
    machines = await conn.fetch("SELECT machine_id, machine_name FROM machine WHERE entity = $1", entity)
    machine_lookup = {r['machine_name'].strip().lower(): r['machine_id'] for r in machines}

    # Insert plan lines
    lines_created = 0
    for item in schedule:
        fg_name = item.get("fg_sku_name", "")
        machine_name = item.get("machine_name", "")
        machine_id = machine_lookup.get(machine_name.strip().lower())

        # Find bom_id
        bom_id = item.get("bom_id")
        if not bom_id:
            bom_id = await conn.fetchval(
                "SELECT bom_id FROM bom_header WHERE fg_sku_name = $1 AND is_active = TRUE LIMIT 1",
                fg_name,
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

    # Log to ai_recommendation
    await conn.execute(
        """
        INSERT INTO ai_recommendation (
            recommendation_type, entity, prompt_text, response_text, response_json,
            tokens_used, latency_ms, model_used, plan_id
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        f"{plan_type}_plan", entity,
        f"Planning context for {date_from} to {date_to}",
        ai_result["raw_text"], json.dumps(parsed, default=str),
        ai_result["tokens_used"], ai_result["latency_ms"],
        settings.CLAUDE_MODEL, plan_id,
    )

    logger.info("Created plan %d with %d lines", plan_id, lines_created)
    return {
        "plan_id": plan_id,
        "status": "draft",
        "lines": lines_created,
        "material_check": parsed.get("material_check", []),
        "risk_flags": parsed.get("risk_flags", []),
        "no_bom_items": [],
    }


# ---------------------------------------------------------------------------
# Plan Revision
# ---------------------------------------------------------------------------

async def collect_revision_context(conn, plan_id: int, change_event: str, entity: str) -> dict:
    """Collect current plan state + change event for revision."""

    plan = await conn.fetchrow("SELECT * FROM production_plan WHERE plan_id = $1", plan_id)
    plan_lines = await conn.fetch(
        "SELECT * FROM production_plan_line WHERE plan_id = $1 ORDER BY priority", plan_id,
    )

    # Current job card statuses for this plan's orders
    job_cards = await conn.fetch(
        """
        SELECT jc.job_card_id, jc.job_card_number, jc.fg_sku_name, jc.stage,
               jc.status, jc.is_locked, jc.batch_size_kg, jc.step_number
        FROM job_card jc
        JOIN production_order po ON jc.prod_order_id = po.prod_order_id
        JOIN production_plan_line pl ON po.plan_line_id = pl.plan_line_id
        WHERE pl.plan_id = $1
        ORDER BY jc.job_card_number
        """,
        plan_id,
    )

    # Inventory
    inv_rows = await conn.fetch(
        "SELECT sku_name, floor_location, SUM(quantity_kg) as qty_kg "
        "FROM floor_inventory WHERE entity = $1 GROUP BY sku_name, floor_location",
        entity,
    )
    inventory = {}
    for r in inv_rows:
        loc = r['floor_location']
        if loc not in inventory:
            inventory[loc] = []
        inventory[loc].append({"sku": r['sku_name'], "qty_kg": float(r['qty_kg'])})

    # Machines
    machines = await conn.fetch(
        "SELECT machine_name, floor, allocation FROM machine WHERE entity = $1 AND status = 'active'",
        entity,
    )

    return {
        "plan_id": plan_id,
        "plan_status": plan['status'],
        "plan_date": str(plan['plan_date']),
        "current_lines": [
            {
                "plan_line_id": pl['plan_line_id'],
                "fg_sku_name": pl['fg_sku_name'],
                "customer_name": pl['customer_name'],
                "planned_qty_kg": float(pl['planned_qty_kg']),
                "priority": pl['priority'],
                "status": pl['status'],
            }
            for pl in plan_lines
        ],
        "active_job_cards": [dict(j) for j in job_cards],
        "change_event": change_event,
        "inventory": inventory,
        "machines": [dict(m) for m in machines],
    }


async def create_revised_plan(conn, old_plan_id: int, entity: str,
                                ai_result: dict, settings) -> dict:
    """Create a new revised plan from Claude's revision response."""

    old_plan = await conn.fetchrow("SELECT * FROM production_plan WHERE plan_id = $1", old_plan_id)
    parsed = ai_result["parsed"]
    revised_schedule = parsed.get("revised_schedule", [])

    # Create new plan header
    new_plan_id = await conn.fetchval(
        """
        INSERT INTO production_plan (
            plan_name, entity, plan_type, plan_date, date_from, date_to,
            status, ai_generated, ai_analysis_json,
            revision_number, previous_plan_id
        ) VALUES ($1, $2, $3, $4, $5, $6, 'draft', TRUE, $7, $8, $9)
        RETURNING plan_id
        """,
        f"Revised Plan — {old_plan['plan_date']} (rev {(old_plan['revision_number'] or 1) + 1})",
        entity, old_plan['plan_type'], old_plan['plan_date'],
        old_plan['date_from'], old_plan['date_to'],
        json.dumps(parsed, default=str),
        (old_plan['revision_number'] or 1) + 1,
        old_plan_id,
    )

    # Mark old plan as revised
    await conn.execute(
        "UPDATE production_plan SET status = 'revised' WHERE plan_id = $1", old_plan_id,
    )

    # Machine lookup
    machines = await conn.fetch("SELECT machine_id, machine_name FROM machine WHERE entity = $1", entity)
    machine_lookup = {r['machine_name'].strip().lower(): r['machine_id'] for r in machines}

    lines_kept = 0
    lines_added = 0
    lines_cancelled = 0

    for item in revised_schedule:
        action = item.get("action", "keep")

        if action == "keep":
            # Copy existing line to new plan
            old_line_id = item.get("plan_line_id")
            if old_line_id:
                old_line = await conn.fetchrow(
                    "SELECT * FROM production_plan_line WHERE plan_line_id = $1", old_line_id,
                )
                if old_line:
                    await conn.execute(
                        """
                        INSERT INTO production_plan_line (
                            plan_id, fg_sku_name, customer_name, bom_id,
                            planned_qty_kg, planned_qty_units, machine_id,
                            priority, shift, stage_sequence, estimated_hours,
                            linked_so_fulfillment_ids, reasoning, status
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                        """,
                        new_plan_id, old_line['fg_sku_name'], old_line['customer_name'],
                        old_line['bom_id'], old_line['planned_qty_kg'], old_line['planned_qty_units'],
                        old_line['machine_id'], old_line['priority'], old_line['shift'],
                        old_line['stage_sequence'], old_line['estimated_hours'],
                        old_line['linked_so_fulfillment_ids'],
                        item.get("reasoning", old_line['reasoning']),
                        old_line['status'],
                    )
                    lines_kept += 1

        elif action == "reschedule":
            old_line_id = item.get("plan_line_id")
            if old_line_id:
                old_line = await conn.fetchrow(
                    "SELECT * FROM production_plan_line WHERE plan_line_id = $1", old_line_id,
                )
                if old_line:
                    new_machine = item.get("new_machine_name", "")
                    machine_id = machine_lookup.get(new_machine.strip().lower(), old_line['machine_id'])
                    await conn.execute(
                        """
                        INSERT INTO production_plan_line (
                            plan_id, fg_sku_name, customer_name, bom_id,
                            planned_qty_kg, planned_qty_units, machine_id,
                            priority, shift, stage_sequence, estimated_hours,
                            linked_so_fulfillment_ids, reasoning
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                        """,
                        new_plan_id, old_line['fg_sku_name'], old_line['customer_name'],
                        old_line['bom_id'], old_line['planned_qty_kg'], old_line['planned_qty_units'],
                        machine_id,
                        item.get("new_priority", old_line['priority']),
                        item.get("new_shift", old_line['shift']),
                        old_line['stage_sequence'], old_line['estimated_hours'],
                        old_line['linked_so_fulfillment_ids'],
                        item.get("reasoning", "Rescheduled"),
                    )
                    lines_kept += 1

        elif action == "cancel":
            lines_cancelled += 1

        elif action == "add":
            fg_name = item.get("fg_sku_name", "")
            machine_name = item.get("machine_name", "")
            machine_id = machine_lookup.get(machine_name.strip().lower())
            bom_id = item.get("bom_id")
            if not bom_id:
                bom_id = await conn.fetchval(
                    "SELECT bom_id FROM bom_header WHERE fg_sku_name = $1 AND is_active = TRUE LIMIT 1",
                    fg_name,
                )
            await conn.execute(
                """
                INSERT INTO production_plan_line (
                    plan_id, fg_sku_name, customer_name, bom_id,
                    planned_qty_kg, planned_qty_units, machine_id,
                    priority, shift, stage_sequence, estimated_hours,
                    linked_so_fulfillment_ids, reasoning
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                new_plan_id, fg_name, item.get("customer_name"),
                bom_id, item.get("qty_kg", 0), item.get("qty_units"),
                machine_id, item.get("priority", 5), item.get("shift", "day"),
                item.get("stage_sequence"), item.get("estimated_hours"),
                item.get("linked_fulfillment_ids"),
                item.get("reasoning", "Added in revision"),
            )
            lines_added += 1

    # Log to ai_recommendation
    await conn.execute(
        """
        INSERT INTO ai_recommendation (
            recommendation_type, entity, prompt_text, response_text, response_json,
            tokens_used, latency_ms, model_used, plan_id
        ) VALUES ('plan_revision', $1, $2, $3, $4, $5, $6, $7, $8)
        """,
        entity, f"Revision of plan {old_plan_id}",
        ai_result["raw_text"], json.dumps(parsed, default=str),
        ai_result["tokens_used"], ai_result["latency_ms"],
        settings.CLAUDE_MODEL, new_plan_id,
    )

    logger.info("Revised plan %d → %d: %d kept, %d added, %d cancelled",
                old_plan_id, new_plan_id, lines_kept, lines_added, lines_cancelled)

    return {
        "old_plan_id": old_plan_id,
        "new_plan_id": new_plan_id,
        "status": "draft",
        "revision_number": (old_plan['revision_number'] or 1) + 1,
        "lines_kept": lines_kept,
        "lines_added": lines_added,
        "lines_cancelled": lines_cancelled,
        "material_check": parsed.get("material_check", []),
        "risk_flags": parsed.get("risk_flags", []),
    }
