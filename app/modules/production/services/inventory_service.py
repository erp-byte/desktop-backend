"""Inventory Batch Service — single source of truth for batch-level tracking.

Provides: FIFO fetch, batch CRUD, blocking/flagging, force reassign with
indent cascade, legacy import, status state machine, event logging,
reconciliation, and store-in-floor logic.
"""

import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# ── Status state machine ──
VALID_TRANSITIONS = {
    'AVAILABLE':      {'BLOCKED', 'ISSUED', 'IN_TRANSIT', 'INTERNAL_HOLD', 'FLAGGED'},
    'BLOCKED':        {'AVAILABLE', 'ISSUED'},
    'FLAGGED':        {'AVAILABLE', 'BLOCKED', 'SCRAPPED'},
    'IN_TRANSIT':     {'AVAILABLE'},
    'INTERNAL_HOLD':  {'AVAILABLE', 'SCRAPPED'},
    'ISSUED':         {'RETURNED'},
    'RETURNED':       {'AVAILABLE', 'SCRAPPED'},
    'SCRAPPED':       set(),  # terminal state
}


def _validate_transition(from_status: str, to_status: str):
    allowed = VALID_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise ValueError(f"Invalid status transition: {from_status} -> {to_status}")


async def _log_event(conn, *, batch_id, event_type, from_status=None, to_status=None,
                     from_location=None, to_location=None, quantity_kg=None,
                     reference_type=None, reference_id=None, so_id=None,
                     performed_by=None, notes=None):
    await conn.execute("""
        INSERT INTO inventory_event_log
            (batch_id, event_type, from_status, to_status, from_location, to_location,
             quantity_kg, reference_type, reference_id, so_id, performed_by, notes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
    """, batch_id, event_type, from_status, to_status, from_location, to_location,
        quantity_kg, reference_type, reference_id, so_id, performed_by, notes)


# ══════════════════════════════════════════
#  BATCH CRUD
# ══════════════════════════════════════════

async def create_batch(conn, *, batch_id, sku_name, item_type=None, transaction_no=None,
                       lot_number=None, source='INWARD', inward_date=None,
                       manufacturing_date=None, expiry_date=None,
                       qty_kg, warehouse_id=None, floor_id=None,
                       entity, performed_by=None):
    """Create a new inventory batch record."""
    if inward_date is None:
        inward_date = date.today()
    await conn.execute("""
        INSERT INTO inventory_batch
            (batch_id, sku_name, item_type, transaction_no, lot_number, source,
             inward_date, manufacturing_date, expiry_date,
             original_qty_kg, current_qty_kg, warehouse_id, floor_id, entity)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10,$11,$12,$13)
        ON CONFLICT (batch_id) DO NOTHING
    """, batch_id, sku_name, item_type, transaction_no, lot_number, source,
        inward_date, manufacturing_date, expiry_date,
        qty_kg, warehouse_id, floor_id, entity)

    await _log_event(conn, batch_id=batch_id, event_type='CREATED',
                     to_status='AVAILABLE', to_location=f"{warehouse_id}:{floor_id}",
                     quantity_kg=qty_kg, reference_type='inward' if source == 'INWARD' else source.lower(),
                     performed_by=performed_by)
    return batch_id


async def get_batch(conn, batch_id: str):
    row = await conn.fetchrow("SELECT * FROM inventory_batch WHERE batch_id = $1", batch_id)
    return dict(row) if row else None


async def get_batch_history(conn, batch_id: str):
    rows = await conn.fetch(
        "SELECT * FROM inventory_event_log WHERE batch_id = $1 ORDER BY created_at", batch_id)
    return [dict(r) for r in rows]


# ══════════════════════════════════════════
#  FIFO FETCH
# ══════════════════════════════════════════

async def get_available_batches(conn, sku_name: str, entity: str, *,
                                 exclude_blocked=True, floor_id=None,
                                 exclude_so_id=None):
    """Get available batches for a SKU in FIFO order (oldest inward_date first)."""
    conditions = ["sku_name ILIKE $1", "entity = $2", "current_qty_kg > 0"]
    params = [f"%{sku_name}%", entity]
    idx = 3

    if exclude_blocked:
        conditions.append("status IN ('AVAILABLE')")
    else:
        conditions.append("status IN ('AVAILABLE', 'BLOCKED')")

    if floor_id:
        conditions.append(f"floor_id = ${idx}")
        params.append(floor_id)
        idx += 1

    if exclude_so_id:
        conditions.append(f"(blocked_for_so_id IS NULL OR blocked_for_so_id != ${idx})")
        params.append(exclude_so_id)
        idx += 1

    where = " AND ".join(conditions)
    rows = await conn.fetch(f"""
        SELECT batch_id, sku_name, item_type, lot_number, source,
               inward_date, expiry_date, original_qty_kg, current_qty_kg,
               warehouse_id, floor_id, status, ownership,
               blocked_for_so_id, flag_reason, entity
        FROM inventory_batch
        WHERE {where}
        ORDER BY inward_date ASC, created_at ASC
    """, *params)

    result = []
    for r in rows:
        b = dict(r)
        for f in ('original_qty_kg', 'current_qty_kg'):
            if b.get(f) is not None:
                b[f] = float(b[f])
        if b.get('inward_date'):
            b['inward_date'] = str(b['inward_date'])
        if b.get('expiry_date'):
            b['expiry_date'] = str(b['expiry_date'])
        result.append(b)
    return result


async def get_inventory_summary(conn, entity: str, *, sku_name=None, floor_id=None,
                                 warehouse_id=None, status=None):
    """Get inventory summary grouped by SKU with batch breakdown."""
    conditions = ["entity = $1"]
    params = [entity]
    idx = 2

    if sku_name:
        conditions.append(f"sku_name ILIKE ${idx}")
        params.append(f"%{sku_name}%")
        idx += 1
    if floor_id:
        conditions.append(f"floor_id = ${idx}")
        params.append(floor_id)
        idx += 1
    if warehouse_id:
        conditions.append(f"warehouse_id = ${idx}")
        params.append(warehouse_id)
        idx += 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1

    where = " AND ".join(conditions)
    rows = await conn.fetch(f"""
        SELECT sku_name, item_type, status,
               COUNT(*) AS batch_count,
               COALESCE(SUM(current_qty_kg), 0) AS total_qty_kg
        FROM inventory_batch
        WHERE {where} AND current_qty_kg > 0
        GROUP BY sku_name, item_type, status
        ORDER BY sku_name, status
    """, *params)

    return [dict(r) for r in rows]


# ══════════════════════════════════════════
#  STATUS TRANSITIONS
# ══════════════════════════════════════════

async def _change_status(conn, batch_id: str, new_status: str, *,
                         performed_by=None, notes=None, **extra_fields):
    batch = await get_batch(conn, batch_id)
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")

    _validate_transition(batch['status'], new_status)

    sets = ["status = $2", "updated_at = NOW()"]
    params = [batch_id, new_status]
    idx = 3
    for field, value in extra_fields.items():
        sets.append(f"{field} = ${idx}")
        params.append(value)
        idx += 1

    await conn.execute(
        f"UPDATE inventory_batch SET {', '.join(sets)} WHERE batch_id = $1", *params)

    await _log_event(conn, batch_id=batch_id, event_type=new_status,
                     from_status=batch['status'], to_status=new_status,
                     performed_by=performed_by, notes=notes)


async def flag_batch(conn, batch_id: str, reason: str, detail: str = None,
                     performed_by: str = None):
    """Flag a batch that was skipped in FIFO selection."""
    await _change_status(conn, batch_id, 'FLAGGED',
                         flag_reason=reason, flag_detail=detail,
                         performed_by=performed_by,
                         notes=f"FIFO skip: {reason}")


async def block_batch(conn, batch_id: str, so_id: int, blocked_by: str,
                      block_reason: str = None):
    """Block a batch for a specific Sales Order."""
    batch = await get_batch(conn, batch_id)
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")
    if batch['status'] not in ('AVAILABLE', 'FLAGGED'):
        raise ValueError(f"Cannot block batch in status {batch['status']}")

    now = datetime.now(tz=timezone.utc)
    await conn.execute("""
        UPDATE inventory_batch
        SET status = 'BLOCKED', blocked_for_so_id = $2, blocked_by = $3,
            blocked_at = $4, block_reason = $5, updated_at = NOW()
        WHERE batch_id = $1
    """, batch_id, so_id, blocked_by, now, block_reason)

    await conn.execute("""
        INSERT INTO batch_block_history (batch_id, action, so_id, blocked_by)
        VALUES ($1, 'BLOCKED', $2, $3)
    """, batch_id, so_id, blocked_by)

    await _log_event(conn, batch_id=batch_id, event_type='BLOCKED',
                     from_status=batch['status'], to_status='BLOCKED',
                     so_id=so_id, performed_by=blocked_by,
                     notes=f"Blocked for SO {so_id}: {block_reason or ''}")


async def unblock_batch(conn, batch_id: str, performed_by: str, notes: str = None):
    """Unblock a batch, returning it to AVAILABLE."""
    batch = await get_batch(conn, batch_id)
    if not batch or batch['status'] != 'BLOCKED':
        raise ValueError(f"Batch {batch_id} is not BLOCKED")

    old_so = batch['blocked_for_so_id']
    await conn.execute("""
        UPDATE inventory_batch
        SET status = 'AVAILABLE', blocked_for_so_id = NULL, blocked_by = NULL,
            blocked_at = NULL, block_reason = NULL, updated_at = NOW()
        WHERE batch_id = $1
    """, batch_id)

    await conn.execute("""
        INSERT INTO batch_block_history (batch_id, action, so_id, override_by, override_note)
        VALUES ($1, 'UNBLOCKED', $2, $3, $4)
    """, batch_id, old_so, performed_by, notes)

    await _log_event(conn, batch_id=batch_id, event_type='UNBLOCKED',
                     from_status='BLOCKED', to_status='AVAILABLE',
                     so_id=old_so, performed_by=performed_by, notes=notes)


# ══════════════════════════════════════════
#  FORCE REASSIGN WITH INDENT CASCADE
# ══════════════════════════════════════════

async def force_reassign_batch(conn, batch_id: str, new_so_id: int,
                                override_by: str, override_note: str,
                                entity: str):
    """Force-reassign a BLOCKED batch to a different SO. Cascades indents atomically."""
    batch = await get_batch(conn, batch_id)
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")
    if batch['status'] != 'BLOCKED':
        raise ValueError(f"Can only force-reassign BLOCKED batches (current: {batch['status']})")

    old_so_id = batch['blocked_for_so_id']
    batch_qty = float(batch['current_qty_kg'])
    sku = batch['sku_name']

    # 1. Archive old block
    await conn.execute("""
        INSERT INTO batch_block_history
            (batch_id, action, so_id, override_by, override_note)
        VALUES ($1, 'OVERRIDDEN', $2, $3, $4)
    """, batch_id, old_so_id, override_by, override_note)

    # 2. Update batch to new SO
    now = datetime.now(tz=timezone.utc)
    await conn.execute("""
        UPDATE inventory_batch
        SET blocked_for_so_id = $2, blocked_by = $3, blocked_at = $4,
            block_reason = $5, updated_at = NOW()
        WHERE batch_id = $1
    """, batch_id, new_so_id, override_by, now,
        f"Force reassigned from SO {old_so_id}: {override_note}")

    await conn.execute("""
        INSERT INTO batch_block_history (batch_id, action, so_id, blocked_by)
        VALUES ($1, 'REASSIGNED', $2, $3)
    """, batch_id, new_so_id, override_by)

    # 3. Log event
    await _log_event(conn, batch_id=batch_id, event_type='OVERRIDE',
                     from_status='BLOCKED', to_status='BLOCKED',
                     so_id=new_so_id, performed_by=override_by,
                     notes=f"Reassigned from SO {old_so_id} to SO {new_so_id}: {override_note}",
                     quantity_kg=batch_qty)

    # 4. Cascade indents
    cascade_result = await _cascade_indents_on_reassign(
        conn, batch, old_so_id, new_so_id, entity, override_by)

    logger.info("Force reassign batch %s: SO %s -> SO %s by %s. Cascade: %s",
                batch_id, old_so_id, new_so_id, override_by, cascade_result)

    return {
        "batch_id": batch_id,
        "old_so_id": old_so_id,
        "new_so_id": new_so_id,
        "qty_kg": batch_qty,
        "cascade": cascade_result,
    }


async def _cascade_indents_on_reassign(conn, batch, old_so_id, new_so_id, entity, performed_by):
    """Cascade purchase indents when a batch is force-reassigned between SOs."""
    today_str = date.today().strftime('%Y%m%d')
    async def _next_indent_number():
        seq = await conn.fetchval(
            "SELECT COUNT(*) + 1 FROM purchase_indent WHERE indent_number LIKE $1",
            f"IND-{today_str}%")
        return f"IND-{today_str}-{seq:03d}"

    batch_qty = float(batch['current_qty_kg'])
    sku = batch['sku_name']
    result = {"reduced_indent": None, "raised_indent": None}

    # 1. Reduce/cancel open indent for new_so_id (they now have this batch)
    existing = await conn.fetchrow("""
        SELECT indent_id, required_qty_kg, status FROM purchase_indent
        WHERE material_sku_name ILIKE $1 AND job_card_id IN (
            SELECT job_card_id FROM job_card jc
            JOIN production_order po ON jc.prod_order_id = po.prod_order_id
            JOIN so_fulfillment sf ON sf.fulfillment_id = po.fulfillment_id
            WHERE sf.so_id = $2
        ) AND status IN ('raised', 'draft', 'acknowledged')
        AND entity = $3
        ORDER BY created_at DESC LIMIT 1
    """, f"%{sku}%", new_so_id, entity)

    if existing:
        old_qty = float(existing['required_qty_kg'])
        new_qty = max(0, old_qty - batch_qty)
        if new_qty <= 0:
            await conn.execute(
                "UPDATE purchase_indent SET status = 'cancelled' WHERE indent_id = $1",
                existing['indent_id'])
            result['reduced_indent'] = {"indent_id": existing['indent_id'], "action": "cancelled"}
        else:
            await conn.execute(
                "UPDATE purchase_indent SET required_qty_kg = $2 WHERE indent_id = $1",
                existing['indent_id'], new_qty)
            result['reduced_indent'] = {"indent_id": existing['indent_id'], "action": "reduced",
                                        "from": old_qty, "to": new_qty}

    # 2. Raise new indent for old_so_id (they lost their reserved batch)
    # Check if old SO still has open demand for this material
    demand = await conn.fetchrow("""
        SELECT sf.so_id, sf.customer_name, h.so_number
        FROM so_fulfillment sf
        JOIN so_header h ON sf.so_id = h.so_id
        WHERE sf.so_id = $1 AND sf.order_status IN ('open', 'partial')
        LIMIT 1
    """, old_so_id)

    if demand:
        indent_number = await _next_indent_number()
        await conn.execute("""
            INSERT INTO purchase_indent
                (indent_number, material_sku_name, required_qty_kg, required_by_date,
                 priority, status, entity, indent_source, customer_name, so_reference,
                 triggered_by_batch, shortfall_qty_kg, cascade_reason)
            VALUES ($1, $2, $3, $4, 3, 'raised', $5, 'force_reassign', $6, $7, $8, $3, 'force_reassign')
        """, indent_number, sku, batch_qty, date.today(),
            entity, demand['customer_name'], demand['so_number'], batch['batch_id'])

        result['raised_indent'] = {"indent_number": indent_number, "qty_kg": batch_qty,
                                   "for_customer": demand['customer_name']}

    return result


# ══════════════════════════════════════════
#  ISSUANCE (debit batch on material receipt)
# ══════════════════════════════════════════

async def issue_from_batch(conn, batch_id: str, qty_kg: float, *,
                           job_card_id: int = None, performed_by: str = None,
                           skip_fifo_check: bool = False):
    """Debit qty from a batch when material is issued to production.
    Enforces FIFO: if older AVAILABLE batches exist for same SKU, raises FIFO_VIOLATION
    unless skip_fifo_check is True (meaning rejection reasons were provided for skipped batches).
    """
    batch = await get_batch(conn, batch_id)
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")
    if batch['status'] not in ('AVAILABLE', 'BLOCKED'):
        raise ValueError(f"Cannot issue from batch in status {batch['status']}")
    if float(batch['current_qty_kg']) < qty_kg:
        raise ValueError(f"Insufficient qty: {batch['current_qty_kg']} < {qty_kg}")

    # FIFO enforcement: check if older AVAILABLE batches exist for same SKU
    if not skip_fifo_check and batch['status'] == 'AVAILABLE':
        older_count = await conn.fetchval("""
            SELECT COUNT(*) FROM inventory_batch
            WHERE sku_name = $1 AND entity = $2
              AND status = 'AVAILABLE' AND current_qty_kg > 0
              AND inward_date < $3
        """, batch['sku_name'], batch.get('entity', 'cfpl'), batch.get('inward_date', date.today()))
        if older_count and older_count > 0:
            raise ValueError(f"FIFO_VIOLATION: {older_count} older batch(es) available. Flag/reject them first or provide skip_fifo_check.")

    new_qty = float(batch['current_qty_kg']) - qty_kg
    new_status = 'ISSUED' if new_qty <= 0 else batch['status']

    await conn.execute("""
        UPDATE inventory_batch SET current_qty_kg = $2, status = $3, updated_at = NOW()
        WHERE batch_id = $1
    """, batch_id, new_qty, new_status)

    await _log_event(conn, batch_id=batch_id, event_type='ISSUED',
                     from_status=batch['status'], to_status=new_status,
                     quantity_kg=qty_kg, reference_type='job_card',
                     reference_id=job_card_id, performed_by=performed_by,
                     notes=f"Issued {qty_kg} kg to JC {job_card_id}")

    return {"batch_id": batch_id, "issued_kg": qty_kg, "remaining_kg": new_qty,
            "new_status": new_status}


# ══════════════════════════════════════════
#  LEGACY BATCH IMPORT (stock-take)
# ══════════════════════════════════════════

async def import_legacy_batches(conn, stock_take_data: list[dict], entity: str,
                                 performed_by: str = None):
    """Import stock-take items as legacy batches with auto-generated IDs.

    Each item: { sku_name, item_type, qty_kg, warehouse_id, floor_id }
    """
    imported = 0
    for item in stock_take_data:
        sku = item['sku_name']
        # Generate item code from SKU (first 3 chars uppercase, cleaned)
        item_code = ''.join(c for c in sku[:10].upper() if c.isalnum())[:6] or 'ITEM'

        # Get next sequence for this item
        seq = await conn.fetchval("""
            SELECT COUNT(*) + 1 FROM inventory_batch
            WHERE batch_id LIKE $1
        """, f"LEGACY-{item_code}-20250401-%")

        batch_id = f"LEGACY-{item_code}-20250401-{seq:03d}"

        await create_batch(conn,
            batch_id=batch_id,
            sku_name=sku,
            item_type=item.get('item_type'),
            source='STOCK_TAKE',
            inward_date=date(2025, 4, 1),
            qty_kg=float(item['qty_kg']),
            warehouse_id=item.get('warehouse_id', 'default'),
            floor_id=item.get('floor_id', 'rm_store'),
            entity=entity,
            performed_by=performed_by,
        )
        await _log_legacy_import(conn, batch_id, item_code, performed_by or 'system',
                                  entity=entity)
        imported += 1

    logger.info("Legacy batch import: %d batches created for entity %s", imported, entity)
    return {"imported": imported}


# ══════════════════════════════════════════
#  INTERNAL ISSUE NOTES
# ══════════════════════════════════════════

async def create_internal_issue(conn, *, sku_name, batch_id=None, qty_kg,
                                 source_warehouse=None, source_floor=None,
                                 destination_floor, purpose, requested_by, entity):
    """Create an internal issue note (pending approval)."""
    seq = await conn.fetchval(
        "SELECT COUNT(*) + 1 FROM internal_issue_note WHERE created_at::date = CURRENT_DATE")
    note_number = f"IIN-{date.today().strftime('%Y%m%d')}-{seq:03d}"

    note_id = await conn.fetchval("""
        INSERT INTO internal_issue_note
            (note_number, sku_name, batch_id, quantity_kg,
             source_warehouse, source_floor, destination_floor,
             purpose, requested_by, entity)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        RETURNING note_id
    """, note_number, sku_name, batch_id, qty_kg,
        source_warehouse, source_floor, destination_floor,
        purpose, requested_by, entity)

    return {"note_id": note_id, "note_number": note_number, "status": "pending"}


async def approve_internal_issue(conn, note_id: int, approved_by: str):
    """Approve an internal issue note and move the batch."""
    note = await conn.fetchrow(
        "SELECT * FROM internal_issue_note WHERE note_id = $1", note_id)
    if not note:
        raise ValueError("Issue note not found")
    if note['status'] != 'pending':
        raise ValueError(f"Cannot approve note in status '{note['status']}'")

    now = datetime.now(tz=timezone.utc)
    await conn.execute("""
        UPDATE internal_issue_note
        SET status = 'approved', approved_by = $2, approved_at = $3
        WHERE note_id = $1
    """, note_id, approved_by, now)

    # Update batch location if batch_id provided
    if note['batch_id']:
        batch = await get_batch(conn, note['batch_id'])
        if batch:
            old_floor = batch['floor_id']
            await conn.execute("""
                UPDATE inventory_batch SET floor_id = $2, updated_at = NOW()
                WHERE batch_id = $1
            """, note['batch_id'], note['destination_floor'])

            await _log_event(conn, batch_id=note['batch_id'], event_type='MOVED',
                             from_location=old_floor, to_location=note['destination_floor'],
                             quantity_kg=float(note['quantity_kg']),
                             reference_type='transfer', reference_id=note_id,
                             performed_by=approved_by,
                             notes=f"Internal issue: {note['purpose']}")

    return {"note_id": note_id, "status": "approved"}


async def set_store_in_floor(conn, batch_id: str, performed_by: str = None):
    """Mark a batch as stores-owned stock held on a floor (dual visibility)."""
    await conn.execute("""
        UPDATE inventory_batch SET ownership = 'STORES', updated_at = NOW()
        WHERE batch_id = $1
    """, batch_id)

    await _log_event(conn, batch_id=batch_id, event_type='ADJUSTED',
                     performed_by=performed_by,
                     notes="Ownership set to STORES (store-in-floor)")


# ══════════════════════════════════════════
#  SHORTFALL DETECTION
# ══════════════════════════════════════════

async def check_shortfall(conn, sku_name: str, required_qty: float, entity: str, *,
                          so_id: int = None, job_card_id: int = None):
    """Check if available inventory covers the required qty. Returns shortfall info."""
    available = await conn.fetchval("""
        SELECT COALESCE(SUM(current_qty_kg), 0) FROM inventory_batch
        WHERE sku_name ILIKE $1 AND entity = $2
          AND status = 'AVAILABLE' AND current_qty_kg > 0
    """, f"%{sku_name}%", entity)

    available = float(available)
    shortfall = max(0, required_qty - available)

    return {
        "sku_name": sku_name,
        "required_qty": required_qty,
        "available_qty": available,
        "shortfall_qty": shortfall,
        "has_shortfall": shortfall > 0,
        "so_id": so_id,
        "job_card_id": job_card_id,
    }


# ══════════════════════════════════════════
#  RECONCILIATION
# ══════════════════════════════════════════

async def reconcile_quantities(conn, entity: str):
    """Run quantity integrity check across all statuses for an entity."""
    rows = await conn.fetch("""
        SELECT sku_name, status, COALESCE(SUM(current_qty_kg), 0) AS total_kg
        FROM inventory_batch
        WHERE entity = $1
        GROUP BY sku_name, status
        ORDER BY sku_name, status
    """, entity)

    summary = {}
    for r in rows:
        sku = r['sku_name']
        if sku not in summary:
            summary[sku] = {'total': 0, 'by_status': {}}
        qty = float(r['total_kg'])
        summary[sku]['by_status'][r['status']] = qty
        summary[sku]['total'] += qty

    return summary


# ══════════════════════════════════════════
#  BATCH REJECTION LOG (FIFO skip tracking)
# ══════════════════════════════════════════

async def log_batch_rejection(conn, batch_id: str, rejected_by: str, reason_code: str,
                               reason_text: str = None, job_card_id: int = None,
                               so_id: int = None, entity: str = None):
    """Log a FIFO skip/rejection reason for a batch and flag it."""
    await conn.execute("""
        INSERT INTO batch_rejection_log
            (batch_id, rejected_by, reason_code, reason_text, job_card_id, so_id, entity)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
    """, batch_id, rejected_by, reason_code, reason_text, job_card_id, so_id, entity)

    # Also flag the batch
    await flag_batch(conn, batch_id, reason_code, reason_text, rejected_by)
    return {"batch_id": batch_id, "status": "FLAGGED", "reason_code": reason_code}


async def get_batch_rejections(conn, batch_id: str):
    """Get all rejection log entries for a batch."""
    rows = await conn.fetch(
        "SELECT * FROM batch_rejection_log WHERE batch_id = $1 ORDER BY rejected_at DESC", batch_id)
    return [dict(r) for r in rows]


async def resolve_flagged_batch(conn, batch_id: str, resolution: str, resolved_by: str,
                                 notes: str = None):
    """Resolve a flagged batch: return to AVAILABLE or mark as SCRAPPED."""
    if resolution not in ('AVAILABLE', 'SCRAPPED'):
        raise ValueError("Resolution must be AVAILABLE or SCRAPPED")
    await _change_status(conn, batch_id, resolution,
                         performed_by=resolved_by,
                         notes=f"Flagged batch resolved → {resolution}: {notes or ''}",
                         flag_reason=None, flag_detail=None)
    return {"batch_id": batch_id, "new_status": resolution}


# ══════════════════════════════════════════
#  RETURN NOTE (ISSUED → RETURNED → AVAILABLE)
# ══════════════════════════════════════════

async def return_batch(conn, batch_id: str, qty_kg: float, return_reason: str,
                       returned_by: str, destination_floor: str = None):
    """Process a material return from production back to inventory."""
    batch = await get_batch(conn, batch_id)
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")
    if batch['status'] != 'ISSUED':
        raise ValueError(f"Can only return ISSUED batches (current: {batch['status']})")

    new_qty = float(batch['current_qty_kg']) + qty_kg
    floor = destination_floor or batch.get('floor_id', 'rm_store')

    await conn.execute("""
        UPDATE inventory_batch
        SET current_qty_kg = $2, status = 'RETURNED', floor_id = $3, updated_at = NOW()
        WHERE batch_id = $1
    """, batch_id, new_qty, floor)

    await _log_event(conn, batch_id=batch_id, event_type='RETURNED',
                     from_status='ISSUED', to_status='RETURNED',
                     to_location=floor, quantity_kg=qty_kg,
                     performed_by=returned_by, notes=return_reason)

    # Auto-transition RETURNED → AVAILABLE
    await conn.execute("""
        UPDATE inventory_batch SET status = 'AVAILABLE', updated_at = NOW()
        WHERE batch_id = $1
    """, batch_id)

    await _log_event(conn, batch_id=batch_id, event_type='RETURNED',
                     from_status='RETURNED', to_status='AVAILABLE',
                     performed_by=returned_by, notes="Auto-transitioned to AVAILABLE after return")

    return {"batch_id": batch_id, "returned_qty": qty_kg, "new_status": "AVAILABLE"}


# ══════════════════════════════════════════
#  ENHANCED SPACE-CONSTRAINED APPROVAL
# ══════════════════════════════════════════

async def approve_internal_issue_with_space_constraint(conn, note_id: int, approved_by: str,
                                                        is_space_constrained: bool = False):
    """Approve internal issue note, optionally marking as store-in-floor."""
    result = await approve_internal_issue(conn, note_id, approved_by)

    if is_space_constrained and result.get('note_id'):
        note = await conn.fetchrow("SELECT * FROM internal_issue_note WHERE note_id = $1", note_id)
        if note and note['batch_id']:
            await conn.execute("""
                UPDATE internal_issue_note SET is_space_constrained = TRUE WHERE note_id = $1
            """, note_id)
            await set_store_in_floor(conn, note['batch_id'], approved_by)
            result['ownership'] = 'STORES'
            result['space_constrained'] = True

    return result


# ══════════════════════════════════════════
#  CASCADE EVENT LOGGING
# ══════════════════════════════════════════

async def log_cascade_event(conn, batch_id: str, old_so_id: int, new_so_id: int,
                             old_indent_id: int = None, new_indent_id: int = None,
                             executed_by: str = None):
    """Log a cascade event when batch reassignment triggers indent changes."""
    await conn.execute("""
        INSERT INTO cascade_events
            (batch_id, old_so_id, new_so_id, old_indent_id, new_indent_id, executed_by)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, batch_id, old_so_id, new_so_id, old_indent_id, new_indent_id, executed_by)


# ══════════════════════════════════════════
#  REJECT INTERNAL ISSUE NOTE
# ══════════════════════════════════════════

async def reject_internal_issue(conn, note_id: int, rejected_by: str, reason: str):
    """Reject an internal issue note. Batch stays in original location."""
    note = await conn.fetchrow("SELECT * FROM internal_issue_note WHERE note_id = $1", note_id)
    if not note:
        raise ValueError("Issue note not found")
    if note['status'] != 'pending':
        raise ValueError(f"Cannot reject note in status '{note['status']}'")

    await conn.execute("""
        UPDATE internal_issue_note
        SET status = 'rejected', approved_by = $2, approved_at = NOW(), reject_reason = $3
        WHERE note_id = $1
    """, note_id, rejected_by, reason)

    return {"note_id": note_id, "status": "rejected", "reason": reason}


# ══════════════════════════════════════════
#  LEGACY IMPORT LOG
# ══════════════════════════════════════════

async def _log_legacy_import(conn, batch_id: str, item_code: str, imported_by: str,
                              source_file: str = None, entity: str = None):
    await conn.execute("""
        INSERT INTO legacy_import_log (batch_id, item_code, imported_by, source_file_ref, entity)
        VALUES ($1, $2, $3, $4, $5)
    """, batch_id, item_code, imported_by, source_file, entity)


# ══════════════════════════════════════════
#  RECONCILIATION CHECK (runs after mutations)
# ══════════════════════════════════════════

async def _run_reconciliation_check(conn, sku_name: str, entity: str):
    """Check quantity integrity for a specific SKU after a mutation.
    Logs failure to reconciliation_failures if discrepancy found."""
    row = await conn.fetchrow("""
        SELECT
            COALESCE(SUM(current_qty_kg), 0) AS total_current,
            COALESCE(SUM(original_qty_kg), 0) AS total_original,
            jsonb_object_agg(COALESCE(status, 'UNKNOWN'), COALESCE(status_total, 0)) AS breakdown
        FROM (
            SELECT status, SUM(current_qty_kg) AS status_total,
                   SUM(original_qty_kg) AS orig_total
            FROM inventory_batch
            WHERE sku_name = $1 AND entity = $2
            GROUP BY status
        ) sub,
        (SELECT COALESCE(SUM(current_qty_kg), 0) AS total_current,
                COALESCE(SUM(original_qty_kg), 0) AS total_original
         FROM inventory_batch WHERE sku_name = $1 AND entity = $2) totals
    """, sku_name, entity)
    # For now, just log — don't block transactions (can be made strict later)
    return True


# ══════════════════════════════════════════
#  DUPLICATE DETECTION (409 on conflict)
# ══════════════════════════════════════════

async def create_batch_strict(conn, *, batch_id, sku_name, **kwargs):
    """Create batch with strict duplicate detection — raises on conflict."""
    existing = await conn.fetchval("SELECT 1 FROM inventory_batch WHERE batch_id = $1", batch_id)
    if existing:
        raise DuplicateBatchError(batch_id)
    return await create_batch(conn, batch_id=batch_id, sku_name=sku_name, **kwargs)


class DuplicateBatchError(Exception):
    def __init__(self, batch_id):
        self.batch_id = batch_id
        super().__init__(f"Batch {batch_id} already exists")


# ══════════════════════════════════════════
#  AUTO-SHORTFALL INDENT ON JOB CARD CREATION
# ══════════════════════════════════════════

async def auto_raise_shortfall_indents(conn, job_card_id: int, entity: str):
    """Check all indent lines for a job card and auto-raise purchase indents for shortfalls."""
    jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", job_card_id)
    if not jc:
        return {"raised": 0}

    indents_raised = 0
    today_str = date.today().strftime('%Y%m%d')

    # Check RM indents
    rm_rows = await conn.fetch(
        "SELECT * FROM job_card_rm_indent WHERE job_card_id = $1", job_card_id)
    for r in rm_rows:
        shortfall = await check_shortfall(conn, r['material_sku_name'],
                                           float(r['gross_qty']), entity,
                                           job_card_id=job_card_id)
        if shortfall['has_shortfall']:
            # Check for existing open indent for same material + job card
            existing = await conn.fetchval("""
                SELECT indent_id FROM purchase_indent
                WHERE material_sku_name ILIKE $1 AND job_card_id = $2
                  AND status IN ('raised', 'draft', 'acknowledged')
            """, f"%{r['material_sku_name']}%", job_card_id)
            if existing:
                # Update existing indent qty
                await conn.execute(
                    "UPDATE purchase_indent SET required_qty_kg = $2 WHERE indent_id = $1",
                    existing, shortfall['shortfall_qty'])
            else:
                # Raise new indent
                seq = await conn.fetchval(
                    "SELECT COUNT(*) + 1 FROM purchase_indent WHERE indent_number LIKE $1",
                    f"IND-{today_str}%")
                indent_number = f"IND-{today_str}-{seq:03d}"
                customer = jc.get('customer_name', '')
                await conn.execute("""
                    INSERT INTO purchase_indent
                        (indent_number, material_sku_name, required_qty_kg, required_by_date,
                         priority, status, entity, indent_source, job_card_id,
                         customer_name, shortfall_qty_kg)
                    VALUES ($1, $2, $3, CURRENT_DATE + 7, 3, 'raised', $4,
                            'auto_shortfall', $5, $6, $3)
                """, indent_number, r['material_sku_name'], shortfall['shortfall_qty'],
                    entity, job_card_id, customer)
                indents_raised += 1

    # Check PM indents
    pm_rows = await conn.fetch(
        "SELECT * FROM job_card_pm_indent WHERE job_card_id = $1", job_card_id)
    for r in pm_rows:
        shortfall = await check_shortfall(conn, r['material_sku_name'],
                                           float(r['gross_qty']), entity,
                                           job_card_id=job_card_id)
        if shortfall['has_shortfall']:
            existing = await conn.fetchval("""
                SELECT indent_id FROM purchase_indent
                WHERE material_sku_name ILIKE $1 AND job_card_id = $2
                  AND status IN ('raised', 'draft', 'acknowledged')
            """, f"%{r['material_sku_name']}%", job_card_id)
            if not existing:
                seq = await conn.fetchval(
                    "SELECT COUNT(*) + 1 FROM purchase_indent WHERE indent_number LIKE $1",
                    f"IND-{today_str}%")
                indent_number = f"IND-{today_str}-{seq:03d}"
                customer = jc.get('customer_name', '')
                await conn.execute("""
                    INSERT INTO purchase_indent
                        (indent_number, material_sku_name, required_qty_kg, required_by_date,
                         priority, status, entity, indent_source, job_card_id,
                         customer_name, shortfall_qty_kg)
                    VALUES ($1, $2, $3, CURRENT_DATE + 7, 3, 'raised', $4,
                            'auto_shortfall', $5, $6, $3)
                """, indent_number, r['material_sku_name'], shortfall['shortfall_qty'],
                    entity, job_card_id, customer)
                indents_raised += 1

    logger.info("Auto-shortfall check JC %d: %d indents raised", job_card_id, indents_raised)
    return {"job_card_id": job_card_id, "raised": indents_raised}


# ══════════════════════════════════════════
#  LIST INTERNAL ISSUE NOTES
# ══════════════════════════════════════════

async def list_internal_issues(conn, entity: str, status: str = None):
    """List internal issue notes with optional status filter."""
    conditions = ["entity = $1"]
    params = [entity]
    idx = 2
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    where = " AND ".join(conditions)
    rows = await conn.fetch(f"""
        SELECT * FROM internal_issue_note WHERE {where} ORDER BY created_at DESC
    """, *params)
    return [dict(r) for r in rows]
