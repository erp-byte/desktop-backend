"""Store Control: Material allocation approval/rejection for job card indents.

Flow:
  1. Job card created → store_allocation records created (pending)
  2. Store reviews pending allocations → decide_allocation()
     - approve: full qty approved, QR scan enabled
     - partial: approved_qty set, indent for remainder
     - reject: rejection reason logged, optional purchase indent raised
     - alternative_offered: off-grade suggested, awaits production acceptance
  3. QR scan gated on store_decision = approved/partial
  4. Floor stock verification for material already on production floor
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pending Allocations (Store Dashboard)
# ---------------------------------------------------------------------------

async def get_pending_allocations(conn, *, entity=None, job_card_id=None,
                                   material=None, page=1, page_size=50) -> dict:
    """List all indent lines awaiting store decision, enriched with inventory info."""

    conditions = ["sa.decision = 'pending'"]
    params = []
    idx = 1

    if entity:
        conditions.append(f"sa.entity = ${idx}"); params.append(entity); idx += 1
    if job_card_id:
        conditions.append(f"sa.job_card_id = ${idx}"); params.append(job_card_id); idx += 1
    if material:
        conditions.append(f"sa.material_sku_name ILIKE ${idx}"); params.append(f"%{material}%"); idx += 1

    where = " AND ".join(conditions)

    count = await conn.fetchval(
        f"SELECT COUNT(*) FROM store_allocation sa WHERE {where}", *params,
    )

    offset = (page - 1) * page_size
    rows = await conn.fetch(
        f"""
        SELECT sa.*, jc.job_card_number, jc.fg_sku_name, jc.customer_name,
               jc.batch_number, jc.process_name, jc.floor, jc.factory
        FROM store_allocation sa
        JOIN job_card jc ON sa.job_card_id = jc.job_card_id
        WHERE {where}
        ORDER BY sa.created_at ASC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params, page_size, offset,
    )

    results = []
    for r in rows:
        row = dict(r)
        # Enrich with inventory availability (single ledger: inventory_batch)
        ent = r['entity'] or entity
        inv = await conn.fetchrow(
            "SELECT COALESCE(SUM(current_qty_kg), 0) AS on_hand "
            "FROM inventory_batch WHERE sku_name ILIKE $1 AND entity = $2 "
            "AND status IN ('AVAILABLE','BLOCKED') AND current_qty_kg > 0",
            f"%{r['material_sku_name']}%", ent,
        )
        row['on_hand_kg'] = float(inv['on_hand']) if inv else 0.0

        # Floor stock (production_floor location)
        floor_inv = await conn.fetchrow(
            "SELECT COALESCE(SUM(current_qty_kg), 0) AS floor_qty "
            "FROM inventory_batch WHERE sku_name ILIKE $1 AND entity = $2 "
            "AND floor_id = 'production_floor' AND status = 'AVAILABLE' AND current_qty_kg > 0",
            f"%{r['material_sku_name']}%", ent,
        )
        row['floor_qty'] = float(floor_inv['floor_qty']) if floor_inv else 0.0

        # FIFO batches from inventory_batch
        fifo_batches = await conn.fetch("""
            SELECT batch_id, lot_number, inward_date, expiry_date,
                   current_qty_kg, warehouse_id, floor_id, status, ownership
            FROM inventory_batch
            WHERE sku_name ILIKE $1 AND entity = $2
              AND status = 'AVAILABLE' AND current_qty_kg > 0
            ORDER BY inward_date ASC, created_at ASC
            LIMIT 10
        """, f"%{r['material_sku_name']}%", r['entity'] or entity)
        row['fifo_batches'] = []
        for fb in fifo_batches:
            b = dict(fb)
            b['current_qty_kg'] = float(b['current_qty_kg']) if b['current_qty_kg'] else 0
            if b.get('inward_date'): b['inward_date'] = str(b['inward_date'])
            if b.get('expiry_date'): b['expiry_date'] = str(b['expiry_date'])
            row['fifo_batches'].append(b)

        # Off-grade alternatives
        offgrade = await conn.fetch(
            """
            SELECT oi.offgrade_id, oi.source_product, oi.category, oi.grade,
                   oi.available_qty_kg, oi.expiry_date,
                   orr.max_substitution_pct
            FROM offgrade_inventory oi
            JOIN offgrade_reuse_rule orr ON oi.item_group = orr.source_item_group
            WHERE oi.status = 'available' AND oi.entity = $1
              AND oi.available_qty_kg > 0
            LIMIT 5
            """,
            r['entity'] or entity,
        )
        row['offgrade_options'] = [dict(o) for o in offgrade]
        row['allocation_id'] = r['allocation_id']
        results.append(row)

    return {
        "results": results,
        "pagination": {
            "page": page, "page_size": page_size,
            "total": count,
            "total_pages": (count + page_size - 1) // page_size if count else 0,
        },
    }


# ---------------------------------------------------------------------------
# Store Dashboard Summary
# ---------------------------------------------------------------------------

async def get_store_dashboard(conn, entity: str) -> dict:
    """Aggregated store dashboard stats."""

    today = date.today()

    pending = await conn.fetchval(
        "SELECT COUNT(*) FROM store_allocation WHERE decision = 'pending' AND entity = $1",
        entity,
    )
    approved_today = await conn.fetchval(
        "SELECT COUNT(*) FROM store_allocation WHERE decision IN ('approved', 'partial') "
        "AND decided_at::date = $1 AND entity = $2",
        today, entity,
    )
    rejected_today = await conn.fetchval(
        "SELECT COUNT(*) FROM store_allocation WHERE decision = 'rejected' "
        "AND decided_at::date = $1 AND entity = $2",
        today, entity,
    )
    indents_raised = await conn.fetchval(
        "SELECT COUNT(*) FROM purchase_indent WHERE indent_source = 'store_rejection' "
        "AND created_at::date = $1 AND entity = $2",
        today, entity,
    )

    return {
        "pending": pending or 0,
        "approved_today": approved_today or 0,
        "rejected_today": rejected_today or 0,
        "indents_raised_today": indents_raised or 0,
    }


# ---------------------------------------------------------------------------
# Decide Allocation (core)
# ---------------------------------------------------------------------------

async def decide_allocation(conn, decisions: list[dict], decided_by: str,
                             entity: str) -> dict:
    """Process store decisions for one or more allocation records."""

    processed = 0
    indents_raised = 0

    for d in decisions:
        alloc_id = d['allocation_id']
        decision = d['decision']  # approved, rejected, partial

        alloc = await conn.fetchrow(
            "SELECT * FROM store_allocation WHERE allocation_id = $1", alloc_id,
        )
        if not alloc:
            continue

        approved_qty = d.get('approved_qty', float(alloc['reqd_qty']) if decision == 'approved' else 0)
        rejected_qty = d.get('rejected_qty', 0)
        if decision == 'approved':
            approved_qty = float(alloc['reqd_qty'])
            rejected_qty = 0
        elif decision == 'rejected':
            approved_qty = 0
            rejected_qty = float(alloc['reqd_qty'])
        elif decision == 'partial':
            rejected_qty = float(alloc['reqd_qty']) - approved_qty

        # Update store_allocation record
        await conn.execute(
            """
            UPDATE store_allocation SET
                decision = $2, approved_qty = $3, rejected_qty = $4,
                rejection_reason = $5, rejection_detail = $6,
                reserved_for_customer = $7,
                quality_grade_available = $8, quality_grade_required = $9,
                expiry_date = $10,
                decided_by = $11, decided_at = NOW()
            WHERE allocation_id = $1
            """,
            alloc_id, decision, approved_qty, rejected_qty,
            d.get('rejection_reason'), d.get('rejection_detail'),
            d.get('reserved_for_customer'),
            d.get('quality_grade_available'), d.get('quality_grade_required'),
            d.get('expiry_date'),
            decided_by,
        )

        # Update the indent table (rm or pm)
        indent_table = 'job_card_rm_indent' if alloc['indent_type'] == 'rm' else 'job_card_pm_indent'
        indent_pk = 'rm_indent_id' if alloc['indent_type'] == 'rm' else 'pm_indent_id'
        await conn.execute(
            f"""
            UPDATE {indent_table} SET
                store_decision = $2, store_approved_qty = $3,
                store_decided_by = $4, store_decided_at = NOW()
            WHERE {indent_pk} = $1
            """,
            alloc['indent_id'], decision, approved_qty, decided_by,
        )

        # Sync status on job_card_rm_indent / job_card_pm_indent to reflect store decision
        if decision == 'approved':
            new_indent_status = 'store_allocated'
        elif decision == 'rejected':
            new_indent_status = 'store_rejected'
        else:  # partial
            new_indent_status = 'partial_allocated'

        await conn.execute(
            f"UPDATE {indent_table} SET status = $1 WHERE {indent_pk} = $2",
            new_indent_status, alloc['indent_id'],
        )

        # Raise purchase indent if requested on rejection/partial
        if d.get('raise_purchase_indent') and rejected_qty > 0:
            indent_id = await _raise_store_rejection_indent(
                conn, alloc, rejected_qty, decided_by, entity,
            )
            await conn.execute(
                "UPDATE store_allocation SET purchase_indent_id = $1 WHERE allocation_id = $2",
                indent_id, alloc_id,
            )
            indents_raised += 1

        # Create alert for production
        alert_type = f"allocation_{decision}"
        await conn.execute(
            """
            INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
            VALUES ($1, 'production', $2, $3, 'job_card', $4)
            """,
            alert_type,
            f"Store {decision}: {alloc['material_sku_name']} — {approved_qty} kg approved"
            + (f", {rejected_qty} kg rejected ({d.get('rejection_reason', '')})" if rejected_qty else ""),
            alloc['job_card_id'], entity,
        )

        processed += 1

    # Update job card store_allocation_status (aggregate of all its allocations)
    if decisions:
        jc_ids = list({d.get('_job_card_id') or (await conn.fetchval(
            "SELECT job_card_id FROM store_allocation WHERE allocation_id = $1",
            d['allocation_id'],
        )) for d in decisions})
        for jc_id in jc_ids:
            if jc_id:
                await _update_jc_allocation_status(conn, jc_id)

    return {"processed": processed, "indents_raised": indents_raised}


async def _update_jc_allocation_status(conn, job_card_id: int):
    """Recompute job card store_allocation_status from its allocation records."""
    rows = await conn.fetch(
        "SELECT decision FROM store_allocation WHERE job_card_id = $1", job_card_id,
    )
    if not rows:
        return
    decisions = [r['decision'] for r in rows]
    if all(d == 'approved' for d in decisions):
        status = 'approved'
    elif all(d == 'rejected' for d in decisions):
        status = 'rejected'
    elif any(d == 'pending' for d in decisions):
        status = 'pending'
    else:
        status = 'partial'

    await conn.execute(
        "UPDATE job_card SET store_allocation_status = $1 WHERE job_card_id = $2",
        status, job_card_id,
    )


async def _raise_store_rejection_indent(conn, alloc, rejected_qty, decided_by, entity):
    """Create a purchase indent from store rejection."""
    today_str = date.today().strftime('%Y%m%d')
    seq = await conn.fetchval(
        "SELECT COUNT(*) + 1 FROM purchase_indent WHERE indent_number LIKE $1",
        f"IND-{today_str}-%",
    )
    indent_number = f"IND-{today_str}-{seq:03d}"

    # Get deadline from job card's fulfillment
    deadline = await conn.fetchval(
        """
        SELECT MIN(sf.delivery_deadline)
        FROM so_fulfillment sf
        JOIN production_order po ON sf.fg_sku_name = po.fg_sku_name AND sf.customer_name = po.customer_name
        JOIN job_card jc ON jc.prod_order_id = po.prod_order_id
        WHERE jc.job_card_id = $1
        """,
        alloc['job_card_id'],
    )

    indent_id = await conn.fetchval(
        """
        INSERT INTO purchase_indent (
            indent_number, material_sku_name, required_qty_kg, required_by_date,
            priority, status, entity, indent_source, store_allocation_id, job_card_id
        ) VALUES ($1, $2, $3, $4, 3, 'raised', $5, 'store_rejection', $6, $7)
        RETURNING indent_id
        """,
        indent_number, alloc['material_sku_name'], rejected_qty,
        deadline or date.today(), entity, alloc['allocation_id'], alloc['job_card_id'],
    )

    # Alert purchase team
    await conn.execute(
        """
        INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
        VALUES ('store_rejection_indent', 'purchase', $1, $2, 'job_card', $3)
        """,
        f"Purchase indent {indent_number} raised: {alloc['material_sku_name']} {rejected_qty} kg "
        f"(Store rejected — {alloc.get('rejection_reason', 'unspecified')})",
        alloc['job_card_id'], entity,
    )

    return indent_id


# ---------------------------------------------------------------------------
# Floor Stock Verification
# ---------------------------------------------------------------------------

async def verify_floor_stock(conn, job_card_id: int, verifications: list[dict],
                              verified_by: str, entity: str) -> dict:
    """Store verifies material already on production floor."""

    verified = 0
    for v in verifications:
        alloc = await conn.fetchrow(
            "SELECT * FROM store_allocation WHERE allocation_id = $1 AND job_card_id = $2",
            v['allocation_id'], job_card_id,
        )
        if not alloc:
            continue

        verified_qty = v.get('verified_qty', 0)
        decision = 'approved' if verified_qty >= float(alloc['reqd_qty']) else 'partial'

        await conn.execute(
            """
            UPDATE store_allocation SET
                floor_stock_verified = TRUE, floor_stock_qty = $2,
                decision = $3, approved_qty = $4,
                source_location = 'production_floor',
                decided_by = $5, decided_at = NOW()
            WHERE allocation_id = $1
            """,
            v['allocation_id'], verified_qty, decision, verified_qty, verified_by,
        )

        # Update indent table
        indent_table = 'job_card_rm_indent' if alloc['indent_type'] == 'rm' else 'job_card_pm_indent'
        indent_pk = 'rm_indent_id' if alloc['indent_type'] == 'rm' else 'pm_indent_id'
        await conn.execute(
            f"UPDATE {indent_table} SET store_decision = $2, store_approved_qty = $3, "
            f"store_decided_by = $4, store_decided_at = NOW(), source_location = 'production_floor' "
            f"WHERE {indent_pk} = $1",
            alloc['indent_id'], decision, verified_qty, verified_by,
        )
        verified += 1

    await _update_jc_allocation_status(conn, job_card_id)
    return {"verified": verified}


# ---------------------------------------------------------------------------
# Suggest Alternative (off-grade)
# ---------------------------------------------------------------------------

async def suggest_alternative(conn, allocation_id: int, offgrade_id: int,
                               qty: float, suggested_by: str, entity: str) -> dict:
    """Store suggests off-grade alternative for a material."""

    alloc = await conn.fetchrow(
        "SELECT * FROM store_allocation WHERE allocation_id = $1", allocation_id,
    )
    if not alloc:
        return {"error": "not_found"}

    offgrade = await conn.fetchrow(
        "SELECT * FROM offgrade_inventory WHERE offgrade_id = $1 AND status = 'available'",
        offgrade_id,
    )
    if not offgrade:
        return {"error": "offgrade_not_found"}

    await conn.execute(
        """
        UPDATE store_allocation SET
            decision = 'alternative_offered',
            suggested_alternative_id = $2, suggested_alternative_qty = $3,
            decided_by = $4, decided_at = NOW()
        WHERE allocation_id = $1
        """,
        allocation_id, offgrade_id, qty, suggested_by,
    )

    # Alert production
    await conn.execute(
        """
        INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
        VALUES ('alternative_offered', 'production', $1, $2, 'job_card', $3)
        """,
        f"Store suggests alternative for {alloc['material_sku_name']}: "
        f"{offgrade['source_product']} grade {offgrade['grade']} ({qty} kg)",
        alloc['job_card_id'], entity,
    )

    return {"allocation_id": allocation_id, "decision": "alternative_offered"}


# ---------------------------------------------------------------------------
# Get Allocation Summary for a Job Card
# ---------------------------------------------------------------------------

async def get_allocation_summary(conn, job_card_id: int) -> dict:
    """Full allocation state for a job card."""

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

    allocations = []
    for r in rows:
        a = dict(r)
        # Convert decimals
        for f in ('reqd_qty', 'approved_qty', 'rejected_qty', 'floor_stock_qty', 'suggested_alternative_qty'):
            if a.get(f) is not None:
                a[f] = float(a[f])
        allocations.append(a)

    return {"job_card_id": job_card_id, "allocations": allocations}


# ---------------------------------------------------------------------------
# Create Pending Allocations (called from job_card_engine)
# ---------------------------------------------------------------------------

async def create_pending_allocations(conn, job_card_id: int, entity: str):
    """Create store_allocation records for all indent lines of a job card."""

    jc = await conn.fetchrow(
        "SELECT job_card_id FROM job_card WHERE job_card_id = $1", job_card_id,
    )
    if not jc:
        return

    # RM indents
    rm_rows = await conn.fetch(
        "SELECT rm_indent_id, material_sku_name, gross_qty FROM job_card_rm_indent WHERE job_card_id = $1",
        job_card_id,
    )
    for r in rm_rows:
        await conn.execute(
            """
            INSERT INTO store_allocation (job_card_id, indent_type, indent_id, material_sku_name, reqd_qty, entity)
            VALUES ($1, 'rm', $2, $3, $4, $5)
            ON CONFLICT DO NOTHING
            """,
            job_card_id, r['rm_indent_id'], r['material_sku_name'],
            float(r['gross_qty'] or 0), entity,
        )

    # PM indents
    pm_rows = await conn.fetch(
        "SELECT pm_indent_id, material_sku_name, gross_qty FROM job_card_pm_indent WHERE job_card_id = $1",
        job_card_id,
    )
    for r in pm_rows:
        await conn.execute(
            """
            INSERT INTO store_allocation (job_card_id, indent_type, indent_id, material_sku_name, reqd_qty, entity)
            VALUES ($1, 'pm', $2, $3, $4, $5)
            ON CONFLICT DO NOTHING
            """,
            job_card_id, r['pm_indent_id'], r['material_sku_name'],
            float(r['gross_qty'] or 0), entity,
        )

    # Fire alert to stores
    await conn.execute(
        """
        INSERT INTO store_alert (alert_type, target_team, message, related_id, related_type, entity)
        VALUES ('allocation_request', 'stores', 'New job card requires material allocation', $1, 'job_card', $2)
        """,
        job_card_id, entity,
    )
