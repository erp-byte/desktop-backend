"""Job Card Engine — core production execution logic.

Creates production orders from approved plans, generates sequential job cards
with lock/unlock mechanics, handles lifecycle (assign → start → complete → unlock next),
sign-offs, and auto-records process loss + off-grade on completion.
"""

import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Production Order Creation
# ---------------------------------------------------------------------------

async def create_production_orders(conn, plan_id: int, entity: str) -> dict:
    """Create production orders for all lines in an approved plan."""

    plan = await conn.fetchrow("SELECT status, entity FROM production_plan WHERE plan_id = $1", plan_id)
    if not plan or plan['status'] != 'approved':
        return {"error": "Plan not found or not approved"}

    plan_lines = await conn.fetch(
        "SELECT * FROM production_plan_line WHERE plan_id = $1 AND status = 'planned'", plan_id,
    )

    orders = []
    for pl in plan_lines:
        bom_id = pl['bom_id']
        if not bom_id:
            continue

        bom = await conn.fetchrow("SELECT * FROM bom_header WHERE bom_id = $1", bom_id)
        if not bom:
            continue

        # Generate numbers
        seq = await conn.fetchval("SELECT COUNT(*) + 1 FROM production_order")
        year = date.today().year
        prod_order_number = f"PRD-{year}-{seq:04d}"
        batch_number = f"B{year}-{seq:04d}"

        # Count stages from process route
        total_stages = await conn.fetchval(
            "SELECT COUNT(*) FROM bom_process_route WHERE bom_id = $1", bom_id,
        )
        total_stages = max(total_stages or 1, 1)

        pack_size = float(bom['pack_size_kg'] or 0)
        shelf_life = bom.get('shelf_life_days') or 180
        best_before = date.today() + timedelta(days=shelf_life)
        factory = bom.get('factory') or 'W202'
        floor_val = None
        if bom.get('floors'):
            floor_val = bom['floors'][0] if bom['floors'] else None

        prod_order_id = await conn.fetchval(
            """
            INSERT INTO production_order (
                prod_order_number, plan_line_id, bom_id, fg_sku_name, customer_name,
                batch_number, batch_size_kg, net_wt_per_unit, best_before,
                total_stages, entity, factory, floor, status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'created')
            RETURNING prod_order_id
            """,
            prod_order_number, pl['plan_line_id'], bom_id,
            pl['fg_sku_name'], pl['customer_name'],
            batch_number, pl['planned_qty_kg'], pack_size, best_before,
            total_stages, entity, factory, floor_val,
        )

        orders.append({
            "prod_order_id": prod_order_id,
            "prod_order_number": prod_order_number,
            "batch_number": batch_number,
            "fg_sku_name": pl['fg_sku_name'],
            "batch_size_kg": float(pl['planned_qty_kg']),
            "total_stages": total_stages,
            "status": "created",
        })

    logger.info("Created %d production orders for plan %d", len(orders), plan_id)
    return {"orders_created": len(orders), "orders": orders}


# ---------------------------------------------------------------------------
# Job Card Generation
# ---------------------------------------------------------------------------

async def create_job_cards(conn, prod_order_id: int) -> dict:
    """Generate sequential job cards for a production order."""

    order = await conn.fetchrow("SELECT * FROM production_order WHERE prod_order_id = $1", prod_order_id)
    if not order:
        return {"error": "Production order not found"}

    bom_id = order['bom_id']
    batch_size = float(order['batch_size_kg'])

    # Get process route
    route_steps = await conn.fetch(
        "SELECT * FROM bom_process_route WHERE bom_id = $1 ORDER BY step_number", bom_id,
    )
    if not route_steps:
        route_steps = [{"step_number": 1, "process_name": "Packaging", "stage": "packaging",
                        "std_time_min": 60, "loss_pct": 0, "qc_check": None, "machine_type": None}]

    # Get BOM lines
    rm_lines = await conn.fetch("SELECT * FROM bom_line WHERE bom_id = $1 AND item_type = 'rm'", bom_id)
    pm_lines = await conn.fetch("SELECT * FROM bom_line WHERE bom_id = $1 AND item_type = 'pm'", bom_id)

    # Get BOM header for extra fields
    bom_header = await conn.fetchrow("SELECT * FROM bom_header WHERE bom_id = $1", bom_id)

    total_steps = len(route_steps)
    job_cards = []

    for step in route_steps:
        step_num = step['step_number'] if hasattr(step, '__getitem__') else step.get('step_number', 1)
        process_name = step['process_name'] if hasattr(step, '__getitem__') else step.get('process_name', 'Processing')
        stage = step['stage'] if hasattr(step, '__getitem__') else step.get('stage', 'processing')

        is_first = step_num == 1
        is_last = step_num == total_steps

        jc_number = f"{order['prod_order_number']}/{step_num}"

        job_card_id = await conn.fetchval(
            """
            INSERT INTO job_card (
                job_card_number, prod_order_id, bom_id, step_number, process_name, stage,
                fg_sku_name, customer_name, batch_number, batch_size_kg,
                machine_id, is_locked, locked_reason, status,
                factory, floor, entity, bu,
                article_code, sales_order_ref
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                      $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
            RETURNING job_card_id
            """,
            jc_number, prod_order_id, bom_id, step_num, process_name, stage,
            order['fg_sku_name'], order['customer_name'], order['batch_number'], batch_size,
            None,  # machine_id assigned later
            not is_first,  # is_locked
            None if is_first else 'awaiting_previous_stage',
            'unlocked' if is_first else 'locked',
            order['factory'], order['floor'], order['entity'],
            bom_header.get('business_unit') if bom_header else None,
            bom_header.get('customer_code') if bom_header else None,
            None,  # sales_order_ref — filled when linked to SO
        )

        # Create RM indent on first stage
        rm_count = 0
        if is_first:
            for i, bl in enumerate(rm_lines, 1):
                reqd = batch_size * float(bl['quantity_per_unit'])
                loss = float(bl['loss_pct'] or 0)
                gross = reqd / (1 - loss / 100) if loss < 100 else reqd
                await conn.execute(
                    """
                    INSERT INTO job_card_rm_indent (
                        job_card_id, material_sku_name, uom, reqd_qty, loss_pct,
                        gross_qty, godown, status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
                    """,
                    job_card_id, bl['material_sku_name'], bl['uom'],
                    round(reqd, 3), loss, round(gross, 3), bl['godown'],
                )
                rm_count += 1

        # Create PM indent on last stage
        pm_count = 0
        if is_last:
            for bl in pm_lines:
                reqd = batch_size * float(bl['quantity_per_unit'])
                loss = float(bl['loss_pct'] or 0)
                gross = reqd / (1 - loss / 100) if loss < 100 else reqd
                await conn.execute(
                    """
                    INSERT INTO job_card_pm_indent (
                        job_card_id, material_sku_name, uom, reqd_qty, loss_pct,
                        gross_qty, godown, status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
                    """,
                    job_card_id, bl['material_sku_name'], bl['uom'],
                    round(reqd, 3), loss, round(gross, 3), bl['godown'],
                )
                pm_count += 1

        # Create store allocation records for material indents
        if rm_count > 0 or pm_count > 0:
            from app.modules.production.services.store_controller import create_pending_allocations
            await create_pending_allocations(conn, job_card_id, order['entity'])

            # Auto-raise purchase indents for any material shortfalls
            from app.modules.production.services.inventory_service import auto_raise_shortfall_indents
            await auto_raise_shortfall_indents(conn, job_card_id, order['entity'])

        # Create process step
        std_time = step['std_time_min'] if hasattr(step, '__getitem__') else step.get('std_time_min')
        qc_check = step['qc_check'] if hasattr(step, '__getitem__') else step.get('qc_check')
        step_loss = step['loss_pct'] if hasattr(step, '__getitem__') else step.get('loss_pct', 0)
        machine_type = step['machine_type'] if hasattr(step, '__getitem__') else step.get('machine_type')

        await conn.execute(
            """
            INSERT INTO job_card_process_step (
                job_card_id, step_number, process_name, machine_name,
                std_time_min, qc_check, loss_pct, status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
            """,
            job_card_id, 1, process_name, machine_type,
            float(std_time) if std_time else None,
            qc_check, float(step_loss or 0),
        )

        job_cards.append({
            "job_card_id": job_card_id,
            "job_card_number": jc_number,
            "step_number": step_num,
            "process_name": process_name,
            "stage": stage,
            "status": "unlocked" if is_first else "locked",
            "is_locked": not is_first,
            "locked_reason": None if is_first else "awaiting_previous_stage",
            "rm_indent_lines": rm_count,
            "pm_indent_lines": pm_count,
            "process_steps": 1,
        })

    # Update production order
    await conn.execute(
        "UPDATE production_order SET status = 'job_cards_issued', total_stages = $2 WHERE prod_order_id = $1",
        prod_order_id, total_steps,
    )

    logger.info("Created %d job cards for order %s", len(job_cards), order['prod_order_number'])
    return {"prod_order_id": prod_order_id, "job_cards": job_cards}


# ---------------------------------------------------------------------------
# Job Card Lifecycle
# ---------------------------------------------------------------------------

async def assign_job_card(conn, job_card_id: int, team_leader: str, team_members: list[str] | None = None) -> dict:
    """Assign a job card to a team leader."""
    jc = await conn.fetchrow("SELECT status FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}
    if jc['status'] not in ('unlocked', 'assigned'):
        return {"error": "invalid_status", "message": f"Cannot assign job card in status '{jc['status']}'"}
    # No store approval check — floor manager raises indent, store provides material directly

    await conn.execute(
        "UPDATE job_card SET assigned_to_team_leader = $2, team_members = $3, status = 'assigned' WHERE job_card_id = $1",
        job_card_id, team_leader, team_members,
    )
    return {"job_card_id": job_card_id, "status": "assigned", "team_leader": team_leader}


async def start_job_card(conn, job_card_id: int) -> dict:
    """Start production on a job card. Requires material_received status."""
    jc = await conn.fetchrow("SELECT status, store_allocation_status FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}
    if jc['status'] != 'material_received':
        return {"error": "invalid_status", "message": f"Cannot start — material must be received first (current: '{jc['status']}')"}

    await conn.execute(
        "UPDATE job_card SET status = 'in_progress', start_time = NOW() WHERE job_card_id = $1",
        job_card_id,
    )
    row = await conn.fetchrow("SELECT start_time FROM job_card WHERE job_card_id = $1", job_card_id)
    return {"job_card_id": job_card_id, "status": "in_progress", "start_time": str(row['start_time'])}


async def complete_process_step(conn, job_card_id: int, step_number: int,
                                 operator_name: str | None = None, qc_passed: bool = False) -> dict:
    """Complete a process step within a job card."""
    step = await conn.fetchrow(
        "SELECT * FROM job_card_process_step WHERE job_card_id = $1 AND step_number = $2",
        job_card_id, step_number,
    )
    if not step:
        return {"error": "not_found", "message": "Step not found"}

    await conn.execute(
        """
        UPDATE job_card_process_step SET
            operator_name = COALESCE($3, operator_name),
            operator_sign_at = NOW(),
            qc_sign_at = CASE WHEN $4 THEN NOW() ELSE qc_sign_at END,
            time_done = NOW(),
            status = 'completed'
        WHERE job_card_id = $1 AND step_number = $2
        """,
        job_card_id, step_number, operator_name, qc_passed,
    )
    return {"job_card_id": job_card_id, "step_number": step_number, "status": "completed"}


async def record_output(conn, job_card_id: int, data: dict) -> dict:
    """Record Section 5 output data."""
    jc = await conn.fetchrow("SELECT batch_size_kg FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}

    batch_kg = float(jc['batch_size_kg'])
    fg_expected_units = data.get('fg_expected_units') or int(batch_kg / 0.5) if batch_kg else None
    fg_expected_kg = data.get('fg_expected_kg') or batch_kg

    output_id = await conn.fetchval(
        """
        INSERT INTO job_card_output (
            job_card_id, fg_expected_units, fg_actual_units, fg_expected_kg, fg_actual_kg,
            rm_consumed_kg, material_return_kg, rejection_kg, rejection_reason,
            process_loss_kg, process_loss_pct, offgrade_kg, offgrade_category, dispatch_qty
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        ON CONFLICT (job_card_id) DO UPDATE SET
            fg_actual_units = $3, fg_actual_kg = $5,
            rm_consumed_kg = $6, material_return_kg = $7,
            rejection_kg = $8, rejection_reason = $9,
            process_loss_kg = $10, process_loss_pct = $11,
            offgrade_kg = $12, offgrade_category = $13, dispatch_qty = $14
        RETURNING output_id
        """,
        job_card_id, fg_expected_units, data.get('fg_actual_units'),
        fg_expected_kg, data.get('fg_actual_kg'),
        data.get('rm_consumed_kg'), data.get('material_return_kg', 0),
        data.get('rejection_kg', 0), data.get('rejection_reason'),
        data.get('process_loss_kg', 0), data.get('process_loss_pct', 0),
        data.get('offgrade_kg', 0), data.get('offgrade_category'),
        data.get('dispatch_qty'),
    )
    return {"output_id": output_id, "saved": True}


async def complete_job_card(conn, job_card_id: int, entity: str) -> dict:
    """Complete a job card. Auto-unlocks next stage or completes the production order."""
    jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}
    if jc['status'] != 'in_progress':
        return {"error": "invalid_status", "message": "Can only complete in_progress job cards"}

    now = datetime.now(tz=timezone.utc)
    start = jc['start_time']
    total_min = round((now - start).total_seconds() / 60, 2) if start else None

    await conn.execute(
        "UPDATE job_card SET status = 'completed', end_time = $2, total_time_min = $3 WHERE job_card_id = $1",
        job_card_id, now, total_min,
    )

    result = {"job_card_id": job_card_id, "status": "completed", "total_time_min": total_min}

    # Find next job card
    next_jc = await conn.fetchrow(
        """
        SELECT job_card_id, job_card_number, step_number FROM job_card
        WHERE prod_order_id = $1 AND step_number = $2
        """,
        jc['prod_order_id'], jc['step_number'] + 1,
    )

    if next_jc:
        # Unlock next stage
        await conn.execute(
            "UPDATE job_card SET is_locked = FALSE, locked_reason = NULL, status = 'unlocked' WHERE job_card_id = $1",
            next_jc['job_card_id'],
        )

        # Create alert
        await conn.execute(
            """
            INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
            VALUES ('plan_ready', 'production', $1, $2, 'job_card', $3)
            """,
            f"Job card {next_jc['job_card_number']} unlocked — ready for production",
            next_jc['job_card_id'], entity,
        )

        result["next_unlocked"] = {
            "job_card_id": next_jc['job_card_id'],
            "job_card_number": next_jc['job_card_number'],
            "status": "unlocked",
        }
        result["order_completed"] = False
    else:
        # Last stage — complete production order
        await conn.execute(
            "UPDATE production_order SET status = 'completed' WHERE prod_order_id = $1",
            jc['prod_order_id'],
        )

        # Update so_fulfillment
        output = await conn.fetchrow(
            "SELECT fg_actual_kg, offgrade_kg, offgrade_category, process_loss_kg, process_loss_pct FROM job_card_output WHERE job_card_id = $1",
            job_card_id,
        )
        if output and output['fg_actual_kg']:
            order = await conn.fetchrow(
                "SELECT plan_line_id FROM production_order WHERE prod_order_id = $1", jc['prod_order_id'],
            )
            if order and order['plan_line_id']:
                pl = await conn.fetchrow(
                    "SELECT linked_so_fulfillment_ids FROM production_plan_line WHERE plan_line_id = $1",
                    order['plan_line_id'],
                )
                if pl and pl['linked_so_fulfillment_ids']:
                    for fid in pl['linked_so_fulfillment_ids']:
                        await conn.execute(
                            """
                            UPDATE so_fulfillment SET
                                produced_qty_kg = produced_qty_kg + $2,
                                pending_qty_kg = GREATEST(0, pending_qty_kg - $2),
                                order_status = CASE
                                    WHEN pending_qty_kg - $2 <= 0 THEN 'fulfilled'
                                    ELSE 'partial'
                                END,
                                updated_at = NOW()
                            WHERE fulfillment_id = $1
                            """,
                            fid, float(output['fg_actual_kg']),
                        )

            # Auto-record process loss
            if output['process_loss_kg'] and float(output['process_loss_kg']) > 0:
                await conn.execute(
                    """
                    INSERT INTO process_loss (
                        job_card_id, product_name, item_group, machine_name, stage,
                        loss_kg, loss_pct, batch_number, production_date, entity
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    job_card_id, jc['fg_sku_name'], None, None, jc['stage'],
                    float(output['process_loss_kg']), float(output['process_loss_pct'] or 0),
                    jc['batch_number'], date.today(), entity,
                )

            # Auto-create offgrade inventory
            if output['offgrade_kg'] and float(output['offgrade_kg']) > 0:
                await conn.execute(
                    """
                    INSERT INTO offgrade_inventory (
                        source_product, category, available_qty_kg,
                        production_date, job_card_id, entity
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    jc['fg_sku_name'], output['offgrade_category'],
                    float(output['offgrade_kg']), date.today(), job_card_id, entity,
                )

        result["next_unlocked"] = None
        result["order_completed"] = True
        result["fulfillment_updated"] = True
        result["process_loss_recorded"] = bool(output and output['process_loss_kg'])
        result["offgrade_created"] = bool(output and output['offgrade_kg'])

    logger.info("Job card %s completed. Next: %s", jc['job_card_number'],
                "unlocked" if next_jc else "order complete")
    return result


# ---------------------------------------------------------------------------
# Sign-offs & Close
# ---------------------------------------------------------------------------

async def sign_off(conn, job_card_id: int, sign_off_type: str, name: str) -> dict:
    """Record a sign-off on a job card."""
    await conn.execute(
        """
        INSERT INTO job_card_sign_off (job_card_id, sign_off_type, name, signed_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (job_card_id, sign_off_type) DO UPDATE SET name = $3, signed_at = NOW()
        """,
        job_card_id, sign_off_type, name,
    )
    return {"job_card_id": job_card_id, "signed": True, "sign_off_type": sign_off_type, "name": name}


# ---------------------------------------------------------------------------
# Material Accounting (pre-close)
# ---------------------------------------------------------------------------

async def save_material_consumption(conn, job_card_id: int, lines: list[dict]) -> dict:
    """Save per-BOM-line actual consumption data."""
    jc = await conn.fetchrow("SELECT status FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}
    if jc['status'] not in ('in_progress', 'completed'):
        return {"error": "invalid_status", "message": "Can only record consumption for in-progress or completed job cards"}

    # Get RM indent lines for cross-reference
    rm_indents = await conn.fetch(
        "SELECT rm_indent_id, material_sku_name, uom, reqd_qty, issued_qty FROM job_card_rm_indent WHERE job_card_id = $1",
        job_card_id,
    )
    indent_map = {r['material_sku_name'].strip().lower(): dict(r) for r in rm_indents}

    saved = []
    for line in lines:
        sku = line['material_sku_name']
        item_type = line.get('item_type', 'rm')
        actual = float(line.get('actual_consumed_qty') or 0)
        return_qty = float(line.get('return_qty') or 0)

        # Look up BOM / indent data
        indent = indent_map.get(sku.strip().lower(), {})
        bom_reqd = float(indent.get('reqd_qty') or 0)
        issued = float(indent.get('issued_qty') or 0)
        rm_indent_id = indent.get('rm_indent_id')
        uom = indent.get('uom') or line.get('uom', 'kg')
        variance = round(actual - bom_reqd, 3)

        await conn.execute(
            """
            INSERT INTO job_card_material_consumption (
                job_card_id, rm_indent_id, material_sku_name, item_type, uom,
                bom_reqd_qty, issued_qty, actual_consumed_qty, return_qty, variance_qty, remarks
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (job_card_id, material_sku_name, item_type) DO UPDATE SET
                actual_consumed_qty = EXCLUDED.actual_consumed_qty,
                return_qty = EXCLUDED.return_qty,
                variance_qty = EXCLUDED.variance_qty,
                remarks = EXCLUDED.remarks
            """,
            job_card_id, rm_indent_id, sku, item_type, uom,
            bom_reqd, issued, actual, return_qty, variance,
            line.get('remarks'),
        )
        saved.append({
            "material_sku_name": sku, "item_type": item_type,
            "bom_reqd_qty": bom_reqd, "issued_qty": issued,
            "actual_consumed_qty": actual, "return_qty": return_qty,
            "variance_qty": variance,
        })

    return {"job_card_id": job_card_id, "saved": len(saved), "lines": saved}


async def save_byproducts(conn, job_card_id: int, lines: list[dict]) -> dict:
    """Save by-product / off-grade category entries."""
    jc = await conn.fetchrow("SELECT status FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}
    if jc['status'] not in ('in_progress', 'completed'):
        return {"error": "invalid_status", "message": "Can only record byproducts for in-progress or completed job cards"}

    saved = []
    for line in lines:
        cat = line['category']
        qty = float(line.get('quantity_kg') or 0)
        await conn.execute(
            """
            INSERT INTO job_card_byproduct (job_card_id, category, quantity_kg, remarks)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (job_card_id, category) DO UPDATE SET
                quantity_kg = EXCLUDED.quantity_kg, remarks = EXCLUDED.remarks
            """,
            job_card_id, cat, qty, line.get('remarks'),
        )
        saved.append({"category": cat, "quantity_kg": qty})

    return {"job_card_id": job_card_id, "saved": len(saved), "lines": saved}


async def save_material_accounting(conn, job_card_id: int, data: dict) -> dict:
    """Save and validate the material accounting summary for a job card."""
    jc = await conn.fetchrow("SELECT status FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}
    if jc['status'] not in ('in_progress', 'completed'):
        return {"error": "invalid_status", "message": "Can only save accounting for in-progress or completed job cards"}

    # Auto-compute total issued from RM indent if not overridden
    total_issued = data.get('total_material_issued_kg')
    if not total_issued:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(issued_qty), 0) AS total FROM job_card_rm_indent WHERE job_card_id = $1",
            job_card_id,
        )
        total_issued = float(row['total']) if row else 0
    else:
        total_issued = float(total_issued)

    fg = float(data.get('fg_output_kg') or 0)
    process_loss = float(data.get('process_loss_kg') or 0)
    extra_give_away = float(data.get('extra_give_away_kg') or 0)
    balance_material = float(data.get('balance_material_kg') or 0)
    offgrade_total = float(data.get('offgrade_total_kg') or 0)
    rejection = float(data.get('rejection_kg') or 0)
    wastage = float(data.get('wastage_kg') or 0)
    control_sample = float(data.get('control_sample_kg') or 0)

    total_accounted = fg + process_loss + extra_give_away + balance_material + offgrade_total + rejection + wastage + control_sample
    difference = round(total_issued - total_accounted, 3)
    is_balanced = abs(difference) < 0.01

    # Loss percentages
    if total_issued > 0:
        process_loss_pct = round((process_loss / total_issued) * 100, 3)
        other_loss_pct = round(((extra_give_away + wastage) / total_issued) * 100, 3)
        total_loss_pct = round(((offgrade_total + process_loss + extra_give_away + wastage) / total_issued) * 100, 3)
    else:
        process_loss_pct = other_loss_pct = total_loss_pct = 0

    import json as _json
    sub_cats = data.get('process_loss_sub_categories')
    sub_cats_json = _json.dumps(sub_cats) if sub_cats else None

    await conn.execute(
        """
        INSERT INTO job_card_material_accounting (
            job_card_id, total_material_issued_kg, fg_output_kg,
            process_loss_kg, process_loss_sub_categories,
            extra_give_away_kg, balance_material_kg, offgrade_total_kg,
            rejection_kg, wastage_kg,
            process_loss_pct, other_loss_pct, total_loss_pct,
            balance_difference_kg, is_balanced, balanced_by, balanced_at,
            control_sample_kg
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16, NOW(), $17)
        ON CONFLICT (job_card_id) DO UPDATE SET
            total_material_issued_kg = EXCLUDED.total_material_issued_kg,
            fg_output_kg = EXCLUDED.fg_output_kg,
            process_loss_kg = EXCLUDED.process_loss_kg,
            process_loss_sub_categories = EXCLUDED.process_loss_sub_categories,
            extra_give_away_kg = EXCLUDED.extra_give_away_kg,
            balance_material_kg = EXCLUDED.balance_material_kg,
            offgrade_total_kg = EXCLUDED.offgrade_total_kg,
            rejection_kg = EXCLUDED.rejection_kg,
            wastage_kg = EXCLUDED.wastage_kg,
            process_loss_pct = EXCLUDED.process_loss_pct,
            other_loss_pct = EXCLUDED.other_loss_pct,
            total_loss_pct = EXCLUDED.total_loss_pct,
            balance_difference_kg = EXCLUDED.balance_difference_kg,
            is_balanced = EXCLUDED.is_balanced,
            balanced_by = EXCLUDED.balanced_by,
            balanced_at = EXCLUDED.balanced_at,
            control_sample_kg = EXCLUDED.control_sample_kg
        """,
        job_card_id, total_issued, fg,
        process_loss, sub_cats_json,
        extra_give_away, balance_material, offgrade_total,
        rejection, wastage,
        process_loss_pct, other_loss_pct, total_loss_pct,
        difference, is_balanced, data.get('balanced_by', ''),
        control_sample,
    )

    return {
        "job_card_id": job_card_id,
        "total_material_issued_kg": total_issued,
        "fg_output_kg": fg,
        "process_loss_kg": process_loss,
        "extra_give_away_kg": extra_give_away,
        "balance_material_kg": balance_material,
        "offgrade_total_kg": offgrade_total,
        "rejection_kg": rejection,
        "wastage_kg": wastage,
        "control_sample_kg": control_sample,
        "total_accounted_kg": round(total_accounted, 3),
        "balance_difference_kg": difference,
        "is_balanced": is_balanced,
        "process_loss_pct": process_loss_pct,
        "other_loss_pct": other_loss_pct,
        "total_loss_pct": total_loss_pct,
    }


async def close_job_card(conn, job_card_id: int) -> dict:
    """Close a job card after all sign-offs and material accounting."""
    jc = await conn.fetchrow("SELECT status FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}
    if jc['status'] != 'completed':
        return {"error": "invalid_status", "message": "Can only close completed job cards"}

    missing_sections = []

    # Check required sign-offs
    sign_offs = await conn.fetch(
        "SELECT sign_off_type FROM job_card_sign_off WHERE job_card_id = $1", job_card_id,
    )
    signed_types = {s['sign_off_type'] for s in sign_offs}
    required = {'floor_incharge', 'qc_inspector', 'production_manager'}
    missing_signoffs = required - signed_types
    if missing_signoffs:
        missing_sections.append({"section": "sign_offs", "detail": list(missing_signoffs)})

    # Check output recorded
    output = await conn.fetchrow(
        "SELECT fg_actual_kg FROM job_card_output WHERE job_card_id = $1", job_card_id,
    )
    if not output or not output['fg_actual_kg'] or float(output['fg_actual_kg']) <= 0:
        missing_sections.append({"section": "output", "detail": "FG actual output not recorded"})

    # Check material consumption
    rm_count = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_rm_indent WHERE job_card_id = $1", job_card_id,
    )
    consumption_count = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_material_consumption WHERE job_card_id = $1 AND actual_consumed_qty IS NOT NULL",
        job_card_id,
    )
    if rm_count and rm_count > 0 and consumption_count < rm_count:
        missing_sections.append({"section": "material_consumption", "detail": f"{consumption_count}/{rm_count} materials have actuals"})

    # Check material accounting balanced
    accounting = await conn.fetchrow(
        "SELECT is_balanced, balance_difference_kg FROM job_card_material_accounting WHERE job_card_id = $1",
        job_card_id,
    )
    if not accounting:
        missing_sections.append({"section": "material_accounting", "detail": "Material accounting not saved"})
    elif not accounting['is_balanced']:
        missing_sections.append({"section": "material_accounting", "detail": f"Not balanced (diff: {accounting['balance_difference_kg']} kg)"})

    if missing_sections:
        return {"error": "incomplete_accounting", "missing_sections": missing_sections}

    await conn.execute("UPDATE job_card SET status = 'closed' WHERE job_card_id = $1", job_card_id)
    return {"job_card_id": job_card_id, "status": "closed"}


# ---------------------------------------------------------------------------
# Force Unlock
# ---------------------------------------------------------------------------

async def force_unlock(conn, job_card_id: int, authority: str, reason: str, entity: str) -> dict:
    """Force unlock a locked job card with authority and reason."""
    jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"error": "not_found"}
    if not jc['is_locked']:
        return {"error": "not_locked", "message": "Job card is already unlocked"}

    await conn.execute(
        """
        UPDATE job_card SET
            is_locked = FALSE, locked_reason = NULL, status = 'unlocked',
            force_unlocked = TRUE, force_unlock_by = $2,
            force_unlock_reason = $3, force_unlock_at = NOW()
        WHERE job_card_id = $1
        """,
        job_card_id, authority, reason,
    )

    # Create alert
    await conn.execute(
        """
        INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
        VALUES ('force_unlock', 'production', $1, $2, 'job_card', $3)
        """,
        f"Force unlock: {jc['job_card_number']} by {authority}. Reason: {reason}",
        job_card_id, entity,
    )

    # Warn if previous stage has no output
    prev_jc = await conn.fetchrow(
        "SELECT job_card_id FROM job_card WHERE prod_order_id = $1 AND step_number = $2",
        jc['prod_order_id'], jc['step_number'] - 1,
    )
    warning = None
    if prev_jc:
        prev_output = await conn.fetchrow(
            "SELECT fg_actual_kg FROM job_card_output WHERE job_card_id = $1", prev_jc['job_card_id'],
        )
        if not prev_output or not prev_output['fg_actual_kg']:
            warning = "Previous stage has no recorded output. Production may produce defective output."

    return {
        "job_card_id": job_card_id,
        "status": "unlocked",
        "force_unlocked": True,
        "authority": authority,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# Full Job Card Detail (GET /job-cards/{id})
# ---------------------------------------------------------------------------

async def get_job_card_detail(conn, job_card_id: int) -> dict | None:
    """Get full job card detail matching CFC/PRD/JC/V3.0 PDF structure."""
    jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return None

    # Get production order for extra fields
    order = await conn.fetchrow(
        "SELECT * FROM production_order WHERE prod_order_id = $1", jc['prod_order_id'],
    )

    bom = await conn.fetchrow("SELECT * FROM bom_header WHERE bom_id = $1", jc['bom_id']) if jc['bom_id'] else None

    # Section 1 — Product Details
    section_1 = {
        "customer_name": jc['customer_name'],
        "fg_sku_name": jc['fg_sku_name'],
        "bu": jc.get('bu'),
        "quantity_units": int(float(jc['batch_size_kg']) / float(order['net_wt_per_unit'])) if order and order['net_wt_per_unit'] and float(order['net_wt_per_unit']) > 0 else None,
        "batch_number": jc['batch_number'],
        "article_code": jc.get('article_code'),
        "mrp": float(jc['mrp']) if jc.get('mrp') else None,
        "ean": jc.get('ean'),
        "best_before": str(order['best_before']) if order and order['best_before'] else None,
        "factory": jc['factory'],
        "floor": jc['floor'],
        "batch_size_kg": float(jc['batch_size_kg']),
        "net_wt_per_unit": float(order['net_wt_per_unit']) if order and order['net_wt_per_unit'] else None,
        "expected_units": int(float(jc['batch_size_kg']) / float(order['net_wt_per_unit'])) if order and order['net_wt_per_unit'] and float(order['net_wt_per_unit']) > 0 else None,
        "shelf_life_days": bom.get('shelf_life_days') if bom else None,
        "sales_order_ref": jc.get('sales_order_ref'),
    }

    # Section 2A — RM Indent (enriched with FIFO batches)
    rm_rows = await conn.fetch(
        "SELECT * FROM job_card_rm_indent WHERE job_card_id = $1 ORDER BY rm_indent_id", job_card_id,
    )
    section_2a = []
    for r in rm_rows:
        row = dict(r)
        # Fetch FIFO batches for this material
        batches = await conn.fetch("""
            SELECT batch_id, lot_number, inward_date, expiry_date,
                   current_qty_kg, warehouse_id, floor_id, status, ownership
            FROM inventory_batch
            WHERE sku_name ILIKE $1 AND entity = $2
              AND status IN ('AVAILABLE', 'BLOCKED') AND current_qty_kg > 0
            ORDER BY inward_date ASC, created_at ASC LIMIT 10
        """, f"%{r['material_sku_name']}%", jc.get('entity', 'cfpl'))
        row['available_batches'] = []
        for b in batches:
            bd = dict(b)
            bd['current_qty_kg'] = float(bd['current_qty_kg']) if bd['current_qty_kg'] else 0
            if bd.get('inward_date'): bd['inward_date'] = str(bd['inward_date'])
            if bd.get('expiry_date'): bd['expiry_date'] = str(bd['expiry_date'])
            row['available_batches'].append(bd)
        section_2a.append(row)

    # Section 2B — PM Indent (enriched with FIFO batches)
    pm_rows = await conn.fetch(
        "SELECT * FROM job_card_pm_indent WHERE job_card_id = $1 ORDER BY pm_indent_id", job_card_id,
    )
    section_2b = []
    for r in pm_rows:
        row = dict(r)
        batches = await conn.fetch("""
            SELECT batch_id, lot_number, inward_date, expiry_date,
                   current_qty_kg, warehouse_id, floor_id, status, ownership
            FROM inventory_batch
            WHERE sku_name ILIKE $1 AND entity = $2
              AND status IN ('AVAILABLE', 'BLOCKED') AND current_qty_kg > 0
            ORDER BY inward_date ASC, created_at ASC LIMIT 10
        """, f"%{r['material_sku_name']}%", jc.get('entity', 'cfpl'))
        row['available_batches'] = []
        for b in batches:
            bd = dict(b)
            bd['current_qty_kg'] = float(bd['current_qty_kg']) if bd['current_qty_kg'] else 0
            if bd.get('inward_date'): bd['inward_date'] = str(bd['inward_date'])
            if bd.get('expiry_date'): bd['expiry_date'] = str(bd['expiry_date'])
            row['available_batches'].append(bd)
        section_2b.append(row)

    # Section 3 — Team & Process
    section_3 = {
        "team_leader": jc['assigned_to_team_leader'],
        "team_members": jc['team_members'],
        "batch_number": jc['batch_number'],
        "start_time": str(jc['start_time']) if jc['start_time'] else None,
        "end_time": str(jc['end_time']) if jc['end_time'] else None,
        "total_time_min": float(jc['total_time_min']) if jc['total_time_min'] else None,
        "fumigation": jc.get('fumigation', False),
        "metal_detector_used": jc.get('metal_detector_used', False),
        "roasting_pasteurization": jc.get('roasting_pasteurization', False),
        "control_sample_gm": float(jc['control_sample_gm']) if jc.get('control_sample_gm') else None,
        "magnets_used": jc.get('magnets_used', False),
    }

    # Section 4 — Process Steps
    steps = await conn.fetch(
        "SELECT * FROM job_card_process_step WHERE job_card_id = $1 ORDER BY step_number", job_card_id,
    )
    section_4 = [dict(s) for s in steps]

    # Section 5 — Output
    output = await conn.fetchrow("SELECT * FROM job_card_output WHERE job_card_id = $1", job_card_id)
    section_5 = dict(output) if output else None

    # Section 6 — Sign-offs (main card)
    sign_offs = await conn.fetch(
        "SELECT * FROM job_card_sign_off WHERE job_card_id = $1", job_card_id,
    )
    section_6 = [dict(s) for s in sign_offs]

    # Annexures
    metal = await conn.fetch(
        "SELECT * FROM job_card_metal_detection WHERE job_card_id = $1 ORDER BY detection_id", job_card_id,
    )
    weight = await conn.fetch(
        "SELECT * FROM job_card_weight_check WHERE job_card_id = $1 ORDER BY sample_number", job_card_id,
    )
    env = await conn.fetch(
        "SELECT * FROM job_card_environment WHERE job_card_id = $1", job_card_id,
    )
    loss_recon = await conn.fetch(
        "SELECT * FROM job_card_loss_reconciliation WHERE job_card_id = $1 ORDER BY recon_id", job_card_id,
    )
    remarks = await conn.fetch(
        "SELECT * FROM job_card_remarks WHERE job_card_id = $1 ORDER BY remark_id", job_card_id,
    )

    # Material Accounting sections
    mat_consumption = await conn.fetch(
        "SELECT * FROM job_card_material_consumption WHERE job_card_id = $1 ORDER BY consumption_id",
        job_card_id,
    )
    byproducts = await conn.fetch(
        "SELECT * FROM job_card_byproduct WHERE job_card_id = $1 ORDER BY byproduct_id",
        job_card_id,
    )
    mat_accounting = await conn.fetchrow(
        "SELECT * FROM job_card_material_accounting WHERE job_card_id = $1",
        job_card_id,
    )

    return {
        "job_card_id": jc['job_card_id'],
        "job_card_number": jc['job_card_number'],
        "prod_order_id": jc['prod_order_id'],
        "step_number": jc['step_number'],
        "total_stages": order['total_stages'] if order else 1,
        "process_name": jc['process_name'],
        "stage": jc['stage'],

        "section_1_product": section_1,
        "section_2a_rm_indent": section_2a,
        "section_2b_pm_indent": section_2b,
        "section_3_team": section_3,
        "section_4_process_steps": section_4,
        "section_5_output": section_5,
        "section_6_signoffs": {s['sign_off_type']: dict(s) for s in section_6},

        "annexure_ab_metal": [dict(m) for m in metal],
        "annexure_b_weight": _build_weight_obj(weight),
        "annexure_c_env": [dict(e) for e in env],
        "annexure_d_loss": [dict(l) for l in loss_recon],
        "annexure_e_remarks": [dict(r) for r in remarks],

        "status": jc['status'],
        "is_locked": jc['is_locked'],
        "locked_reason": jc['locked_reason'],
        "force_unlocked": jc.get('force_unlocked', False),
        "store_allocation_status": jc.get('store_allocation_status', 'pending'),
        "entity": jc.get('entity', 'cfpl'),
        "created_at": str(jc['created_at']),

        "material_consumption": [dict(mc) for mc in mat_consumption],
        "byproducts": [dict(bp) for bp in byproducts],
        "material_accounting": dict(mat_accounting) if mat_accounting else None,

        # Store allocations
        "store_allocations": await _get_store_allocations(conn, job_card_id),
    }


def _build_weight_obj(weight_rows):
    """Convert weight check rows into the object format the frontend expects."""
    if not weight_rows:
        return {}
    samples = [dict(w) for w in weight_rows]
    first = samples[0] if samples else {}
    return {
        "target_wt_g": first.get('target_wt_g'),
        "tolerance_g": first.get('tolerance_g'),
        "accept_range_min": first.get('accept_range_min'),
        "accept_range_max": first.get('accept_range_max'),
        "samples": samples,
    }


async def _get_store_allocations(conn, job_card_id):
    rows = await conn.fetch(
        """
        SELECT sa.*, pi.indent_number AS purchase_indent_number
        FROM store_allocation sa
        LEFT JOIN purchase_indent pi ON sa.purchase_indent_id = pi.indent_id
        WHERE sa.job_card_id = $1
        ORDER BY sa.indent_type, sa.created_at
        """,
        job_card_id,
    )
    results = []
    for r in rows:
        a = dict(r)
        for f in ('reqd_qty', 'approved_qty', 'rejected_qty', 'floor_stock_qty', 'suggested_alternative_qty'):
            if a.get(f) is not None:
                a[f] = float(a[f])
        if a.get('decided_at'):
            a['decided_at'] = str(a['decided_at'])
        if a.get('created_at'):
            a['created_at'] = str(a['created_at'])
        if a.get('expiry_date'):
            a['expiry_date'] = str(a['expiry_date'])
        results.append(a)
    return results
