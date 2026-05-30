"""Production Planning v2 — manual workflow.

Scope:
  * create_plan         — insert header + lines, snapshot steps from BOM
  * list_plans          — paginated list
  * get_plan            — full nested plan (header + lines + ordered steps)
  * update_plan         — edit header (dates, type, status)
  * approve_plan        — stamp approved_by/at, set status
  * cancel_plan         — set status='cancelled'
  * reorder_steps       — re-sequence steps on one plan line
  * update_step         — edit floor / notes / std_time_min / loss_pct
  * add_step            — append a custom step
  * delete_step         — remove + resequence

Targets:
  production_plan_v2 + production_plan_line_v2 (created in 009)
  production_plan_step_v2                       (created in 012)

There is NO machine reference at this layer. Per-step `floor` is free-text.
"""

import logging
from datetime import date, datetime
from decimal import Decimal

from asyncpg.exceptions import CheckViolationError

from app.core.helpers import insert_with_pk_retry, new_short_time_id  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialize_row(row) -> dict:
    """Coerce asyncpg Record to JSON-safe values (Decimal→float, date→ISO)."""
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _parse_date(v):
    """Coerce a date payload value (string 'YYYY-MM-DD' or date object) to a
    Python `date`. Returns None for None/empty/invalid so asyncpg writes NULL.
    """
    if v is None or v == '':
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return datetime.strptime(v[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Plan CRUD
# ---------------------------------------------------------------------------

async def create_plan(conn, payload: dict) -> dict:
    """Create a plan header, its lines, and snapshot steps from bom_process_route.

    payload shape:
        {
          "entity": "cfpl",
          "warehouse": "W-202",
          "plan_type": "daily",
          "plan_date": "2026-05-22",
          "date_from": "2026-05-22",
          "date_to":   "2026-05-22",
          "lines": [
            {
              "fg_sku_name": "...",
              "customer_name": "...",
              "bom_id": 123,
              "planned_qty_kg": 500.0,
              "planned_qty_units": 18000,
              "shift": "A",                                   # optional
              "area": "...",                                  # optional
              "estimated_hours": 4.0,                          # optional
              "linked_so_fulfillment_ids": [87234561],
              "steps": [                                       # optional override
                {"process_name": "...", "stage": "...", "floor": "...",
                 "std_time_min": 10, "loss_pct": 0.5, "notes": "..."},
                ...
              ]
            }
          ]
        }

    If `steps` is omitted for a line, the service snapshots them from
    bom_process_route ordered by step_number. floor is left NULL unless
    the caller supplies it explicitly.
    """
    entity        = payload['entity']
    warehouse     = payload['warehouse']
    plan_type     = payload.get('plan_type', 'daily')
    plan_date     = _parse_date(payload['plan_date'])
    date_from     = _parse_date(payload.get('date_from')) or plan_date
    date_to       = _parse_date(payload.get('date_to'))   or plan_date
    lines         = payload.get('lines', [])

    if not lines:
        return {"error": "no_lines", "message": "At least one plan line is required"}

    # plan_id is app-supplied since migration 016 — 8-digit time-based id
    # (same pattern as so_fulfillment_v2.so_fulfillment_id). PK retry handles
    # the rare clock collision (two creates in the same millisecond burst).
    async def _insert_plan_header():
        candidate = new_short_time_id()
        return await conn.fetchval(
            """
            INSERT INTO production_plan_v2 (
                plan_id, entity, warehouse, plan_type, plan_date, date_from, date_to,
                status, revision_number
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'draft', 0)
            RETURNING plan_id
            """,
            candidate, entity, warehouse, plan_type, plan_date, date_from, date_to,
        )
    plan_id = await insert_with_pk_retry(conn, _insert_plan_header)

    line_ids: list[int] = []
    for ln in lines:
        bom_id = ln.get('bom_id')
        if not bom_id:
            # Derive bom_id from fg_sku_name + active master. Avoids forcing
            # the frontend to look it up before POSTing.
            bom = await conn.fetchrow(
                """
                SELECT bom_id FROM bom_header
                WHERE fg_sku_name ILIKE $1 AND is_active = TRUE
                ORDER BY version DESC LIMIT 1
                """,
                ln.get('fg_sku_name', ''),
            )
            if not bom:
                return {
                    "error": "no_bom",
                    "message": f"No active BOM found for fg_sku_name='{ln.get('fg_sku_name')}'",
                }
            bom_id = bom['bom_id']
        async def _insert_line(_bom_id=bom_id, _ln=ln):
            return await conn.fetchval(
                """
                INSERT INTO production_plan_line_v2 (
                    plan_line_id,
                    plan_id, fg_sku_name, customer_name, bom_id,
                    planned_qty_kg, planned_qty_units, area, deadline_date,
                    shift, estimated_hours, linked_so_fulfillment_ids, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'planned')
                RETURNING plan_line_id
                """,
                new_short_time_id(),
                plan_id, _ln['fg_sku_name'], _ln.get('customer_name'), _bom_id,
                _ln['planned_qty_kg'], _ln['planned_qty_units'],
                _ln.get('area'), _parse_date(_ln.get('deadline_date')),
                _ln.get('shift'), _ln.get('estimated_hours'),
                _ln.get('linked_so_fulfillment_ids') or [],
            )
        plan_line_id = await insert_with_pk_retry(conn, _insert_line)
        line_ids.append(plan_line_id)

        # Snapshot steps — use caller-supplied list if present, else copy
        # from bom_process_route. Order is preserved as given / step_number.
        custom_steps = ln.get('steps')
        if custom_steps:
            step_rows = [
                (i + 1,
                 s['process_name'],
                 s.get('stage'),
                 s.get('floor'),
                 s.get('std_time_min'),
                 s.get('loss_pct'),
                 s.get('notes'))
                for i, s in enumerate(custom_steps)
            ]
        else:
            bom_steps = await conn.fetch(
                """
                SELECT step_number, process_name, stage, std_time_min, loss_pct
                FROM bom_process_route
                WHERE bom_id = $1
                ORDER BY step_number
                """,
                bom_id,
            )
            step_rows = [
                (i + 1,
                 s['process_name'],
                 s['stage'],
                 None,                # floor — user fills in
                 s['std_time_min'],
                 s['loss_pct'],
                 None)
                for i, s in enumerate(bom_steps)
            ]

        for order, name, stage, floor, std_t, loss, notes in step_rows:
            async def _insert_step(
                _plan_line_id=plan_line_id, _order=order, _name=name, _stage=stage,
                _floor=floor, _std_t=std_t, _loss=loss, _notes=notes,
            ):
                return await conn.execute(
                    """
                    INSERT INTO production_plan_step_v2 (
                        step_id,
                        plan_line_id, step_order, process_name, stage,
                        floor, std_time_min, loss_pct, notes
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    new_short_time_id(),
                    _plan_line_id, _order, _name, _stage, _floor, _std_t, _loss, _notes,
                )
            await insert_with_pk_retry(conn, _insert_step)

        # ── Bump planned_qty on each linked so_fulfillment_v2 row ──────────
        # When successful, pending_qty_kg / pending_qty_units on the
        # fulfillment auto-decrement (they're GENERATED ALWAYS columns).
        # The DB enforces non-negative pending via chk_pending_*_nonneg;
        # if the user tried to over-allocate, that throws and we surface
        # a friendly error.
        fids = ln.get('linked_so_fulfillment_ids') or []
        planned_kg    = float(ln.get('planned_qty_kg', 0) or 0)
        planned_units = float(ln.get('planned_qty_units', 0) or 0)
        if fids and (planned_kg > 0 or planned_units > 0):
            try:
                await conn.execute(
                    """
                    UPDATE so_fulfillment_v2
                       SET planned_qty_kg    = planned_qty_kg    + $1,
                           planned_qty_units = planned_qty_units + $2
                     WHERE so_fulfillment_id = ANY($3)
                    """,
                    planned_kg, planned_units, fids,
                )
            except CheckViolationError as exc:
                # chk_pending_kg_nonneg / chk_pending_units_nonneg fires
                # when this allocation would push effective pending below
                # zero. Roll the whole create_plan tx back via raising —
                # the router wraps us in conn.transaction() so any prior
                # inserts in this run get reverted automatically.
                raise ValueError(
                    f"Would over-allocate fulfillment {fids}: "
                    f"requested {planned_kg} kg / {planned_units} pcs but "
                    f"pending isn't enough. ({exc.constraint_name})"
                ) from exc

    logger.info("Plan v2 created: plan_id=%d with %d line(s)", plan_id, len(line_ids))
    return {"plan_id": plan_id, "plan_line_ids": line_ids}


async def get_plan(conn, plan_id: int) -> dict | None:
    """Full nested plan: header + lines + ordered steps."""
    header = await conn.fetchrow(
        "SELECT * FROM production_plan_v2 WHERE plan_id = $1",
        plan_id,
    )
    if not header:
        return None

    line_rows = await conn.fetch(
        """
        SELECT * FROM production_plan_line_v2
        WHERE plan_id = $1
        ORDER BY plan_line_id ASC
        """,
        plan_id,
    )
    line_ids = [r['plan_line_id'] for r in line_rows]

    steps_by_line: dict[int, list[dict]] = {i: [] for i in line_ids}
    if line_ids:
        step_rows = await conn.fetch(
            """
            SELECT * FROM production_plan_step_v2
            WHERE plan_line_id = ANY($1)
            ORDER BY plan_line_id, step_order
            """,
            line_ids,
        )
        for s in step_rows:
            steps_by_line.setdefault(s['plan_line_id'], []).append(_serialize_row(s))

    lines = []
    for r in line_rows:
        d = _serialize_row(r)
        d['steps'] = steps_by_line.get(r['plan_line_id'], [])
        lines.append(d)

    # Flatten so frontend reads plan fields at top level (matches v1 shape)
    # and per-line `steps` array is the new v2 enrichment.
    return {**_serialize_row(header), "lines": lines}


async def list_plans(conn, *, entity=None, warehouse=None, plan_type=None,
                      status=None, date_from=None, date_to=None,
                      page=1, page_size=50,
                      user_scope_warehouses: list[str] | None = None) -> dict:
    """Paginated plan list with simple filters.

    `user_scope_warehouses` is the caller's user-level factory lock. When
    non-None and non-empty, restricts results to those warehouses regardless
    of the `warehouse` arg. Admins should pass None to bypass the lock.
    """
    conditions: list[str] = []
    params: list = []
    idx = 1

    if entity:
        conditions.append(f"p.entity = ${idx}"); params.append(entity); idx += 1
    if warehouse:
        conditions.append(f"p.warehouse = ${idx}"); params.append(warehouse); idx += 1
    elif user_scope_warehouses:
        # Implicit lock filter — no explicit `warehouse` arg, but the caller
        # is restricted at the user level. Use ANY($) so we get the full
        # union of their assigned warehouses, not just one.
        conditions.append(f"p.warehouse = ANY(${idx}::text[])")
        params.append(list(user_scope_warehouses)); idx += 1
    if plan_type:
        conditions.append(f"p.plan_type = ${idx}"); params.append(plan_type); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',') if s.strip()]
        if statuses:
            placeholders = ', '.join(f'${idx + i}' for i in range(len(statuses)))
            conditions.append(f"p.status IN ({placeholders})")
            params.extend(statuses); idx += len(statuses)
    if date_from:
        conditions.append(f"p.plan_date >= ${idx}"); params.append(date_from); idx += 1
    if date_to:
        conditions.append(f"p.plan_date <= ${idx}"); params.append(date_to); idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"
    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM production_plan_v2 p WHERE {where}",
        *params,
    )
    offset = (page - 1) * page_size
    rows = await conn.fetch(
        f"""
        SELECT
            p.*,
            COALESCE(l.line_count, 0)          AS line_count,
            COALESCE(l.total_planned_kg, 0)    AS total_planned_kg,
            COALESCE(l.total_planned_units, 0) AS total_planned_units
        FROM production_plan_v2 p
        LEFT JOIN (
            SELECT plan_id,
                   COUNT(*)               AS line_count,
                   SUM(planned_qty_kg)    AS total_planned_kg,
                   SUM(planned_qty_units) AS total_planned_units
            FROM production_plan_line_v2
            GROUP BY plan_id
        ) l ON l.plan_id = p.plan_id
        WHERE {where}
        ORDER BY p.plan_date DESC, p.plan_id DESC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params, page_size, offset,
    )

    return {
        "results": [_serialize_row(r) for r in rows],
        "pagination": {
            "page": page, "page_size": page_size,
            "total": total or 0,
            "total_pages": ((total or 0) + page_size - 1) // page_size if total else 0,
        },
    }


async def update_plan_line(conn, plan_line_id: int, fields: dict) -> dict:
    """Partial update on a single plan line. Mirrors update_plan's shape.
    Allowed: planned_qty_kg, planned_qty_units, area, deadline_date.

    Zero/negative qty surfaces a ValueError so the router can map it to a
    clean 400 instead of bubbling the raw asyncpg CHECK violation.
    """
    allowed = {'planned_qty_kg', 'planned_qty_units', 'area', 'deadline_date'}
    sets, params, idx = [], [], 1
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ('planned_qty_kg', 'planned_qty_units') and v is not None and float(v) <= 0:
            raise ValueError(f"{k} must be > 0")
        sets.append(f"{k} = ${idx}")
        params.append(v); idx += 1
    if not sets:
        return {"error": "no_change", "message": "No editable fields supplied"}
    params.append(plan_line_id)
    result = await conn.fetchrow(
        f"UPDATE production_plan_line_v2 SET {', '.join(sets)} "
        f"WHERE plan_line_id = ${idx} RETURNING *",
        *params,
    )
    if not result:
        return {"error": "not_found"}
    return {"updated": True, "line": _serialize_row(result)}


async def update_plan(conn, plan_id: int, fields: dict) -> dict:
    """Update header fields. Allowed: plan_date, date_from, date_to,
    plan_type. Status changes go through approve/cancel."""
    allowed = {'plan_date', 'date_from', 'date_to', 'plan_type'}
    sets, params, idx = [], [], 1
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ${idx}")
        params.append(v); idx += 1
    if not sets:
        return {"error": "no_change", "message": "No editable fields supplied"}
    params.append(plan_id)
    result = await conn.fetchrow(
        f"UPDATE production_plan_v2 SET {', '.join(sets)} "
        f"WHERE plan_id = ${idx} RETURNING *",
        *params,
    )
    if not result:
        return {"error": "not_found"}
    return {"updated": True, "plan": _serialize_row(result)}


async def approve_plan(conn, plan_id: int, approved_by: str) -> dict:
    """Mark plan as approved with audit stamps, then fan out per-floor job
    cards from the plan's step chain.

    Plan-approve is the SOLE entry point for job-card creation in v2 — the
    old `production_order → generate_job_cards` flow is bypassed entirely.
    Re-approving an already-approved plan is a no-op for the JC fan-out
    (the generator guards against duplicates).
    """
    if not approved_by:
        return {"error": "missing_approver", "message": "approved_by is required"}
    result = await conn.fetchrow(
        """
        UPDATE production_plan_v2
        SET status      = 'approved',
            approved_by = $2,
            approved_at = NOW()
        WHERE plan_id = $1 AND status IN ('draft','approved')
        RETURNING *
        """,
        plan_id, approved_by,
    )
    if not result:
        return {"error": "not_found_or_invalid_status"}

    # Generate job cards. Failure here rolls back the approval — the router
    # wraps us in a transaction.
    from app.modules.production.services.job_card_v2 import create_job_cards_from_plan
    jc_result = await create_job_cards_from_plan(conn, plan_id)

    return {
        "approved": True,
        "plan": _serialize_row(result),
        "job_cards": jc_result,
    }


async def cancel_plan(conn, plan_id: int, reason: str = "") -> dict:
    """Set plan status to cancelled and release any planned_qty back to the
    linked so_fulfillment_v2 rows so the same qty can be re-planned.
    """
    # Read line allocations BEFORE flipping the status so we know what to
    # release. (We could read after — same data — but reading first lets
    # the release UPDATE run inside the same transaction without races.)
    lines = await conn.fetch(
        """
        SELECT planned_qty_kg, planned_qty_units, linked_so_fulfillment_ids
        FROM production_plan_line_v2
        WHERE plan_id = $1
        """,
        plan_id,
    )

    result = await conn.fetchrow(
        """
        UPDATE production_plan_v2
        SET status = 'cancelled'
        WHERE plan_id = $1 AND status <> 'cancelled'
        RETURNING *
        """,
        plan_id,
    )
    if not result:
        return {"error": "not_found_or_already_cancelled"}

    # Release planned counters. GREATEST(0, ...) is defensive — if some
    # earlier bug or manual edit left a fulfillment with a lower planned
    # counter than this line, we don't want negative numbers in the table.
    for ln in lines:
        fids = ln['linked_so_fulfillment_ids'] or []
        if not fids:
            continue
        kg    = float(ln['planned_qty_kg']    or 0)
        units = float(ln['planned_qty_units'] or 0)
        if kg <= 0 and units <= 0:
            continue
        await conn.execute(
            """
            UPDATE so_fulfillment_v2
               SET planned_qty_kg    = GREATEST(0, planned_qty_kg    - $1),
                   planned_qty_units = GREATEST(0, planned_qty_units - $2)
             WHERE so_fulfillment_id = ANY($3)
            """,
            kg, units, fids,
        )

    return {"cancelled": True, "plan": _serialize_row(result)}


async def delete_plan(conn, plan_id: int, *, reason: str, deleted_by: str) -> dict:
    """Hard-deactivate an APPROVED plan.

    Differs from cancel_plan in three ways:
      1. Only valid when the plan is currently `approved` (cancel_plan
         allows draft and approved both — but the operator-facing UX maps
         draft→Cancel and approved→Delete so the intent is clearer).
      2. Tags the audit row with the `deleted_by` operator + reason. We
         reuse the existing status='cancelled' enum value (no schema
         change) but record the action in cancellation_reason so the
         distinction stays in the row's history.
      3. Caller is expected to fan out a notification email to all admin
         users — that's the router's job since this service stays pure
         and the mail helper lives in services/mail_service.

    Returns:
      {"deleted": True, "plan": {...serialized row...}}
      on success; otherwise {"error": "not_found_or_invalid_status"}.

    Plan-level approved → deleted is *destructive*: the job cards that
    were auto-generated at approve-time are NOT cascaded automatically.
    Callers (web replica) should explicitly warn the operator that this
    step doesn't void downstream JCs — those still need to be cancelled
    individually if the plan is being retracted for real.
    """
    # Read line allocations BEFORE flipping status so the planned-counter
    # release runs against pre-delete data inside the same transaction.
    lines = await conn.fetch(
        """
        SELECT planned_qty_kg, planned_qty_units, linked_so_fulfillment_ids
        FROM production_plan_line_v2
        WHERE plan_id = $1
        """,
        plan_id,
    )

    result = await conn.fetchrow(
        """
        UPDATE production_plan_v2
        SET status              = 'cancelled',
            cancellation_reason = $2
        WHERE plan_id = $1 AND status = 'approved'
        RETURNING *
        """,
        plan_id, f"[deleted by {deleted_by or 'unknown'}] {reason}".strip(),
    )
    if not result:
        return {"error": "not_found_or_invalid_status"}

    # Release planned counters back to so_fulfillment_v2. Same defensive
    # GREATEST(0, ...) pattern cancel_plan uses.
    for ln in lines:
        fids = ln['linked_so_fulfillment_ids'] or []
        if not fids:
            continue
        kg    = float(ln['planned_qty_kg']    or 0)
        units = float(ln['planned_qty_units'] or 0)
        if kg <= 0 and units <= 0:
            continue
        await conn.execute(
            """
            UPDATE so_fulfillment_v2
               SET planned_qty_kg    = GREATEST(0, planned_qty_kg    - $1),
                   planned_qty_units = GREATEST(0, planned_qty_units - $2)
             WHERE so_fulfillment_id = ANY($3)
            """,
            kg, units, fids,
        )

    return {"deleted": True, "plan": _serialize_row(result)}


# ---------------------------------------------------------------------------
# Step-level CRUD
# ---------------------------------------------------------------------------

async def reorder_steps(conn, plan_line_id: int, step_ids: list[int]) -> dict:
    """Reassign step_order to match the given step_ids sequence (1..N).
    Two-pass swap so the UNIQUE(plan_line_id, step_order) constraint isn't
    violated by intermediate states.
    """
    # Validate: all step_ids belong to plan_line_id
    rows = await conn.fetch(
        "SELECT step_id FROM production_plan_step_v2 WHERE plan_line_id = $1",
        plan_line_id,
    )
    valid_ids = {r['step_id'] for r in rows}
    if set(step_ids) != valid_ids:
        return {
            "error": "step_set_mismatch",
            "message": f"Provided step_ids must exactly match the {len(valid_ids)} "
                       f"steps belonging to plan_line_id={plan_line_id}",
        }

    # Pass 1: set to negative ints (avoids UNIQUE violation)
    for i, sid in enumerate(step_ids, start=1):
        await conn.execute(
            "UPDATE production_plan_step_v2 SET step_order = $2 WHERE step_id = $1",
            sid, -i,
        )
    # Pass 2: set to final positive ints
    for i, sid in enumerate(step_ids, start=1):
        await conn.execute(
            "UPDATE production_plan_step_v2 SET step_order = $2 WHERE step_id = $1",
            sid, i,
        )

    return {"reordered": True, "plan_line_id": plan_line_id, "count": len(step_ids)}


async def update_step(conn, step_id: int, fields: dict) -> dict:
    """Patch a step. Editable: floor, notes, std_time_min, loss_pct,
    process_name, stage. step_order goes through reorder_steps()."""
    allowed = {'floor', 'notes', 'std_time_min', 'loss_pct',
               'process_name', 'stage'}
    sets, params, idx = [], [], 1
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ${idx}")
        params.append(v); idx += 1
    if not sets:
        return {"error": "no_change", "message": "No editable fields supplied"}
    params.append(step_id)
    result = await conn.fetchrow(
        f"UPDATE production_plan_step_v2 SET {', '.join(sets)} "
        f"WHERE step_id = ${idx} RETURNING *",
        *params,
    )
    if not result:
        return {"error": "not_found"}
    return {"updated": True, "step": _serialize_row(result)}


async def add_step(conn, plan_line_id: int, step_data: dict) -> dict:
    """Append a new step at end (step_order = MAX+1)."""
    next_order = await conn.fetchval(
        "SELECT COALESCE(MAX(step_order), 0) + 1 FROM production_plan_step_v2 "
        "WHERE plan_line_id = $1",
        plan_line_id,
    )
    process_name = (step_data.get('process_name') or '').strip()
    if not process_name:
        return {"error": "missing_process_name",
                "message": "process_name is required"}
    async def _insert_step():
        return await conn.fetchrow(
            """
            INSERT INTO production_plan_step_v2 (
                step_id,
                plan_line_id, step_order, process_name, stage,
                floor, std_time_min, loss_pct, notes
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            new_short_time_id(),
            plan_line_id, next_order,
            process_name,
            step_data.get('stage'),
            step_data.get('floor'),
            step_data.get('std_time_min'),
            step_data.get('loss_pct'),
            step_data.get('notes'),
        )
    result = await insert_with_pk_retry(conn, _insert_step)
    return {"added": True, "step": _serialize_row(result)}


async def delete_step(conn, step_id: int) -> dict:
    """Delete a step and compact the remaining steps' ordering."""
    old = await conn.fetchrow(
        "SELECT plan_line_id, step_order FROM production_plan_step_v2 WHERE step_id = $1",
        step_id,
    )
    if not old:
        return {"error": "not_found"}

    plan_line_id = old['plan_line_id']
    removed_order = old['step_order']

    await conn.execute(
        "DELETE FROM production_plan_step_v2 WHERE step_id = $1",
        step_id,
    )
    # Compact subsequent steps' order so we don't leave gaps.
    await conn.execute(
        """
        UPDATE production_plan_step_v2
        SET step_order = step_order - 1
        WHERE plan_line_id = $1 AND step_order > $2
        """,
        plan_line_id, removed_order,
    )
    return {"deleted": True, "step_id": step_id, "plan_line_id": plan_line_id}
