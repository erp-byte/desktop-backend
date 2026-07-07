"""SO Fulfillment v2 — manual-planning workflow.

Scope:
  1. sync_fulfillment    — ingest FG SO lines into so_fulfillment_v2
  2. revise_order        — qty/date/units revisions with audit trail
  3. list_fulfillment    — paginated list (drop-in for v1's GET /fulfillment)
  4. get_filter_options  — distinct dropdown values (drop-in for v1)

Targets so_fulfillment_v2 and so_revision_log_v2 (app/db/009_planning_v2.sql).
v1's carryforward, BOM-override, floor-stock, enriched-view, FY-review, detail,
and chart-summary endpoints are NOT yet ported.
"""

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from asyncpg.exceptions import UniqueViolationError

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.webhooks import events

logger = logging.getLogger(__name__)


def _derive_fy(so_date: date) -> str:
    """Apr–Mar fiscal year string. 2026-04-01 → '2026-27', 2026-03-31 → '2025-26'."""
    if so_date.month >= 4:
        return f"{so_date.year}-{str(so_date.year + 1)[2:]}"
    return f"{so_date.year - 1}-{str(so_date.year)[2:]}"


def _derive_entity(company: str | None) -> str | None:
    if not company:
        return None
    c = company.strip().upper()
    has_cfpl = 'CFPL' in c or 'CANDOR FOODS' in c
    has_cdpl = 'CDPL' in c
    if has_cfpl and has_cdpl:
        # Ambiguous — record so we can investigate the upstream data.
        # CFPL wins (first-matched precedence) but the warning lets ops know.
        logger.warning(
            "_derive_entity: ambiguous company %r matched both CFPL and CDPL; defaulting to cfpl",
            company,
        )
    if has_cfpl:
        return 'cfpl'
    if has_cdpl:
        return 'cdpl'
    return None


def _serialize_row(row) -> dict:
    """Coerce asyncpg Record types (Decimal, date, datetime) to JSON-safe values."""
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


async def sync_fulfillment(conn, entity: str | None = None) -> dict:
    """Sync FG SO lines into so_fulfillment_v2. Idempotent via UNIQUE(so_line_id, financial_year).

    Rows are skipped (not failed) when required v2 fields can't be derived:
      - so_date missing
      - entity can't be derived from so_header.company
      - customer_name missing on so_header
      - sku_name missing on so_line
    """

    query = """
        SELECT h.so_id, h.so_number, h.so_date, h.customer_name, h.company,
               l.so_line_id, l.sku_name, l.quantity, l.quantity_units
        FROM so_header h
        JOIN so_line l ON h.so_id = l.so_id
        WHERE NOT EXISTS (
            SELECT 1 FROM so_fulfillment_v2 v
            WHERE v.so_line_id = l.so_line_id
        )
    """
    params: list = []
    if entity:
        query += " AND LOWER(h.company) LIKE $1"
        params.append(f"%{entity.lower()}%")

    rows = await conn.fetch(query, *params)

    synced = 0
    skipped = 0
    failed = 0

    for r in rows:
        so_date = r['so_date']
        ent = _derive_entity(r['company'])
        customer = r['customer_name']
        fg_sku = r['sku_name']

        if not so_date or not ent or not customer or not fg_sku:
            skipped += 1
            continue

        fy = _derive_fy(so_date)
        # so_line field names are misleading (see docs/FRONTEND_API_GUIDE.md):
        # - so_line.quantity        = raw Excel "Qty." (= pack count for unit-priced items)
        # - so_line.quantity_units  = computed `quantity × master_uom` (= total kg)
        # so v2 maps quantity_units -> original_qty_kg and quantity -> original_qty_units.
        qty_kg = float(r['quantity_units']) if r['quantity_units'] is not None else 0.0
        qty_units = float(r['quantity']) if r['quantity'] is not None else None
        deadline = so_date + timedelta(days=7)

        # Insert in a per-row savepoint so a PK collision (same epoch-ms ID
        # as a prior row) doesn't tank the whole sync. ON CONFLICT handles the
        # (so_line_id, financial_year) idempotency silently; insert_with_pk_retry
        # handles PK collisions (retry with fresh time-ID) and re-raises any
        # other unique-violation so unexpected constraints aren't swallowed.
        async def _do_insert():
            return await conn.execute(
                """
                INSERT INTO so_fulfillment_v2 (
                    so_fulfillment_id, so_line_id, financial_year, fg_sku_name,
                    customer_name, entity, original_qty_kg, original_qty_units,
                    deadline_date, order_status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'open')
                ON CONFLICT (so_line_id, financial_year) DO NOTHING
                """,
                new_short_time_id(),
                r['so_line_id'], fy, fg_sku, customer,
                ent, qty_kg, qty_units, deadline,
            )

        try:
            result = await insert_with_pk_retry(conn, _do_insert)
            if result == 'INSERT 0 1':
                synced += 1
            else:
                # ON CONFLICT (so_line_id, financial_year) hit — already exists
                skipped += 1
        except UniqueViolationError as exc:
            # PK collision after all retries exhausted (or a constraint
            # other than _pkey, which would have re-raised immediately).
            logger.warning(
                "so_fulfillment_v2 insert failed for so_line_id=%s (constraint=%s); skipping row",
                r['so_line_id'], getattr(exc, 'constraint_name', ''),
            )
            failed += 1

    total = synced + skipped + failed
    logger.info(
        "Fulfillment v2 sync: %d synced, %d skipped, %d failed, %d total",
        synced, skipped, failed, total,
    )

    if synced > 0:
        try:
            await events.fulfillment_synced(entity or "", synced=synced, skipped=skipped, total=total)
        except Exception:
            logger.exception("fulfillment_synced emit failed; swallowing")

    return {"synced": synced, "skipped": skipped, "failed": failed, "total": total}


async def revise_order(conn, so_fulfillment_id: int, *,
                       new_qty: float | None = None,
                       new_units: float | None = None,
                       new_date: date | None = None,
                       reason: str = "",
                       revised_by: str = "") -> dict:
    """Revise qty (kg), units, or deadline on a v2 fulfillment row with audit log.

    v2 differences from v1:
      - No revised_qty_kg column — new_qty writes to original_qty_kg directly
      - pending_qty_kg / pending_qty_units are GENERATED columns, recomputed
      - Audit goes to so_revision_log_v2; one row per field changed
    """

    old = await conn.fetchrow(
        "SELECT * FROM so_fulfillment_v2 WHERE so_fulfillment_id = $1",
        so_fulfillment_id,
    )
    if not old:
        return {"error": "not_found"}

    if new_qty is None and new_units is None and new_date is None:
        return {"error": "no_change", "message": "Provide new_qty, new_units, or new_date"}

    if new_qty is not None:
        if new_qty < 0:
            return {"error": "invalid_qty", "message": "new_qty cannot be negative"}
        if new_qty < float(old['dispatched_qty_kg'] or 0):
            return {
                "error": "invalid_qty",
                "message": "new_qty cannot be less than dispatched_qty_kg",
            }
        await conn.execute(
            "UPDATE so_fulfillment_v2 SET original_qty_kg = $2 WHERE so_fulfillment_id = $1",
            so_fulfillment_id, new_qty,
        )
        await conn.execute(
            """
            INSERT INTO so_revision_log_v2 (
                so_fulfillment_id, revision_type, old_value, new_value, reason, revised_by
            ) VALUES ($1, 'qty_change', $2, $3, $4, $5)
            """,
            so_fulfillment_id,
            str(float(old['original_qty_kg'])) if old['original_qty_kg'] is not None else None,
            str(new_qty), reason, revised_by,
        )

    if new_units is not None:
        if new_units < 0:
            return {"error": "invalid_units", "message": "new_units cannot be negative"}
        if new_units < float(old['dispatched_qty_units'] or 0):
            return {
                "error": "invalid_units",
                "message": "new_units cannot be less than dispatched_qty_units",
            }
        await conn.execute(
            "UPDATE so_fulfillment_v2 SET original_qty_units = $2 WHERE so_fulfillment_id = $1",
            so_fulfillment_id, new_units,
        )
        await conn.execute(
            """
            INSERT INTO so_revision_log_v2 (
                so_fulfillment_id, revision_type, old_value, new_value, reason, revised_by
            ) VALUES ($1, 'units_change', $2, $3, $4, $5)
            """,
            so_fulfillment_id,
            str(float(old['original_qty_units'])) if old['original_qty_units'] is not None else None,
            str(new_units), reason, revised_by,
        )

    if new_date is not None:
        await conn.execute(
            "UPDATE so_fulfillment_v2 SET deadline_date = $2 WHERE so_fulfillment_id = $1",
            so_fulfillment_id, new_date,
        )
        await conn.execute(
            """
            INSERT INTO so_revision_log_v2 (
                so_fulfillment_id, revision_type, old_value, new_value, reason, revised_by
            ) VALUES ($1, 'date_change', $2, $3, $4, $5)
            """,
            so_fulfillment_id,
            old['deadline_date'].isoformat() if old['deadline_date'] is not None else None,
            new_date.isoformat(), reason, revised_by,
        )

    try:
        await events.fulfillment_revised(
            old['entity'] or "",
            fulfillment_id=so_fulfillment_id,
            new_qty=new_qty,
            new_units=new_units,
            new_date=new_date.isoformat() if new_date else None,
            revised_by=revised_by,
        )
    except Exception:
        logger.exception("fulfillment_revised emit failed; swallowing")

    # Return the fresh row so the caller sees recomputed generated columns
    # (pending_qty_kg, pending_qty_units) and the updated_at trigger value.
    updated = await conn.fetchrow(
        "SELECT * FROM so_fulfillment_v2 WHERE so_fulfillment_id = $1",
        so_fulfillment_id,
    )
    if updated is None:
        # Should be unreachable inside an active transaction (our UPDATE
        # would have row-locked it), but guard so a race can't 500 the call.
        logger.error("revise_order: row %s vanished after update", so_fulfillment_id)
        return {"revised": True, "fulfillment": None}
    return {"revised": True, "fulfillment": _serialize_row(updated)}


# ---------------------------------------------------------------------------
# Read endpoints (v1-compatible response shapes so the frontend can drop-in)
# ---------------------------------------------------------------------------

# Field-alias select that maps v2 columns to the v1 names the frontend reads.
# - so_fulfillment_id -> fulfillment_id
# - deadline_date     -> delivery_deadline
# - h.so_number       -> so_number (joined via so_line.so_id)
# - h.so_date         -> so_date
_V2_LIST_SELECT = """
    v.so_fulfillment_id          AS fulfillment_id,
    v.so_line_id,
    v.financial_year,
    v.fg_sku_name,
    v.customer_name,
    v.entity,
    v.original_qty_kg,
    v.produced_qty_kg,
    v.dispatched_qty_kg,
    v.planned_qty_kg,
    v.pending_qty_kg,
    v.original_qty_units,
    v.produced_qty_units,
    v.dispatched_qty_units,
    v.planned_qty_units,
    v.pending_qty_units,
    v.deadline_date              AS delivery_deadline,
    v.order_status,
    v.created_at,
    v.updated_at,
    h.so_id                      AS so_id,
    h.so_number                  AS so_number,
    h.so_date                    AS so_date
"""

_V2_FROM_JOIN = """
    FROM so_fulfillment_v2 v
    LEFT JOIN so_line   l ON v.so_line_id = l.so_line_id
    LEFT JOIN so_header h ON l.so_id      = h.so_id
"""


async def list_fulfillment(conn, *, entity=None, status=None, financial_year=None,
                            customer=None, so_number=None, article=None,
                            search=None, page=1, page_size=50) -> dict:
    """Paginated list of v2 fulfillment rows. Mirrors v1's response shape:
        { "results": [...], "pagination": {page, page_size, total, total_pages} }
    """

    conditions: list[str] = []
    params: list = []
    idx = 1

    if entity:
        conditions.append(f"v.entity = ${idx}")
        params.append(entity); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',') if s.strip()]
        if statuses:
            placeholders = ', '.join(f'${idx + i}' for i in range(len(statuses)))
            conditions.append(f"v.order_status IN ({placeholders})")
            params.extend(statuses); idx += len(statuses)
    if financial_year:
        conditions.append(f"v.financial_year = ${idx}")
        params.append(financial_year); idx += 1
    if customer:
        values = [c.strip() for c in str(customer).split(',') if c.strip()]
        if values:
            clauses = []
            for c in values:
                clauses.append(f"v.customer_name ILIKE ${idx}")
                params.append(f"%{c}%"); idx += 1
            conditions.append("(" + " OR ".join(clauses) + ")")
    if so_number:
        values = [s.strip() for s in str(so_number).split(',') if s.strip()]
        if values:
            placeholders = ', '.join(f'${idx + i}' for i in range(len(values)))
            conditions.append(f"h.so_number IN ({placeholders})")
            params.extend(values); idx += len(values)
    if article:
        values = [a.strip() for a in str(article).split(',') if a.strip()]
        if values:
            clauses = []
            for a in values:
                clauses.append(f"v.fg_sku_name ILIKE ${idx}")
                params.append(f"%{a}%"); idx += 1
            conditions.append("(" + " OR ".join(clauses) + ")")
    if search:
        conditions.append(
            f"(v.fg_sku_name ILIKE ${idx} OR v.customer_name ILIKE ${idx}"
            f" OR h.so_number ILIKE ${idx} OR v.entity ILIKE ${idx}"
            f" OR v.order_status ILIKE ${idx} OR v.financial_year ILIKE ${idx})"
        )
        params.append(f"%{search}%"); idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"

    count = await conn.fetchval(
        f"SELECT COUNT(*) {_V2_FROM_JOIN} WHERE {where}", *params,
    )

    offset = (page - 1) * page_size
    rows = await conn.fetch(
        f"""
        SELECT {_V2_LIST_SELECT}
        {_V2_FROM_JOIN}
        WHERE {where}
        ORDER BY v.deadline_date ASC NULLS LAST, v.so_fulfillment_id ASC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params, page_size, offset,
    )

    return {
        "results": [_serialize_row(r) for r in rows],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": count or 0,
            "total_pages": ((count or 0) + page_size - 1) // page_size if count else 0,
        },
    }


async def get_fulfillment_by_so_lines(conn, so_line_ids, *, entity=None,
                                      financial_year=None) -> dict:
    """Resolve a set of so_line_ids to their so_fulfillment_v2 rows (list shape).

    Backs the SO-Creation "Selected for Plan" panel: the operator checks SO
    article lines (so_line rows), and the panel needs the matching fulfillment
    rows (fulfillment_id, pending qty, deadline, entity) to build a plan —
    exactly the rows the planning page works with.

    Returns the rows in the same shape as :func:`list_fulfillment` plus the
    list of so_line_ids that have NO fulfillment row yet (not synced), so the
    caller can prompt the operator to run Sync. Read-only — never creates rows.
    """
    ids = [int(i) for i in (so_line_ids or []) if i is not None]
    if not ids:
        return {"results": [], "missing_so_line_ids": []}

    conditions = [f"v.so_line_id = ANY($1::bigint[])"]
    params: list = [ids]
    idx = 2
    if entity:
        conditions.append(f"v.entity = ${idx}")
        params.append(entity); idx += 1
    if financial_year:
        conditions.append(f"v.financial_year = ${idx}")
        params.append(financial_year); idx += 1
    where = " AND ".join(conditions)

    rows = await conn.fetch(
        f"""
        SELECT {_V2_LIST_SELECT}
        {_V2_FROM_JOIN}
        WHERE {where}
        ORDER BY v.deadline_date ASC NULLS LAST, v.so_fulfillment_id ASC
        """,
        *params,
    )
    results = [_serialize_row(r) for r in rows]
    found = {r["so_line_id"] for r in results}
    not_in_scope = [i for i in ids if i not in found]

    # When an entity/FY scope is applied, a so_line whose fulfillment row exists
    # but in a DIFFERENT scope would otherwise be reported as "missing" — and the
    # Sync the caller then runs can't fix it (sync upserts ON CONFLICT
    # (so_line_id, financial_year) DO NOTHING, so the existing row is left
    # untouched and no new in-scope row appears). Split the not-found ids into
    # TRULY missing (no row in ANY scope → Sync helps) vs OUT OF SCOPE (a row
    # exists, just not in this entity/FY → Sync won't help) so the caller can
    # message accurately instead of prompting a futile Sync.
    out_of_scope: list[int] = []
    if not_in_scope and (entity or financial_year):
        existing = await conn.fetch(
            "SELECT DISTINCT so_line_id FROM so_fulfillment_v2 "
            "WHERE so_line_id = ANY($1::bigint[])",
            not_in_scope,
        )
        have_any = {r["so_line_id"] for r in existing}
        out_of_scope = [i for i in not_in_scope if i in have_any]
        missing = [i for i in not_in_scope if i not in have_any]
    else:
        missing = not_in_scope

    return {
        "results": results,
        "missing_so_line_ids": missing,
        "out_of_scope_so_line_ids": out_of_scope,
    }


async def get_filter_options(conn, *, entity=None, financial_year=None,
                              customer=None, so_number=None, article=None) -> dict:
    """Distinct dropdown values for customers / so_numbers / articles, narrowed
    by SIBLING filters (smart cross-filtering) so each list reflects what is
    selectable given the other current selections. Drop-in for v1's shape.
    """

    def build(include_customer: bool, include_so: bool, include_article: bool):
        conds: list[str] = []
        pars: list = []
        i = 1
        if entity:
            conds.append(f"v.entity = ${i}"); pars.append(entity); i += 1
        if financial_year:
            conds.append(f"v.financial_year = ${i}"); pars.append(financial_year); i += 1
        if include_customer and customer:
            values = [c.strip() for c in str(customer).split(',') if c.strip()]
            if values:
                clauses = []
                for c in values:
                    clauses.append(f"v.customer_name ILIKE ${i}")
                    pars.append(f"%{c}%"); i += 1
                conds.append("(" + " OR ".join(clauses) + ")")
        if include_so and so_number:
            values = [s.strip() for s in str(so_number).split(',') if s.strip()]
            if values:
                placeholders = ', '.join(f'${i + k}' for k in range(len(values)))
                conds.append(f"h.so_number IN ({placeholders})")
                pars.extend(values); i += len(values)
        if include_article and article:
            values = [a.strip() for a in str(article).split(',') if a.strip()]
            if values:
                clauses = []
                for a in values:
                    clauses.append(f"v.fg_sku_name ILIKE ${i}")
                    pars.append(f"%{a}%"); i += 1
                conds.append("(" + " OR ".join(clauses) + ")")
        return (" AND ".join(conds) if conds else "TRUE"), pars

    w_c, p_c = build(include_customer=False, include_so=True, include_article=True)
    customers = await conn.fetch(
        f"SELECT DISTINCT v.customer_name {_V2_FROM_JOIN} WHERE {w_c}"
        " AND v.customer_name IS NOT NULL ORDER BY v.customer_name",
        *p_c,
    )

    w_s, p_s = build(include_customer=True, include_so=False, include_article=True)
    so_numbers = await conn.fetch(
        f"SELECT DISTINCT h.so_number {_V2_FROM_JOIN} WHERE {w_s}"
        " AND h.so_number IS NOT NULL ORDER BY h.so_number",
        *p_s,
    )

    w_a, p_a = build(include_customer=True, include_so=True, include_article=False)
    articles = await conn.fetch(
        f"SELECT DISTINCT v.fg_sku_name {_V2_FROM_JOIN} WHERE {w_a}"
        " AND v.fg_sku_name IS NOT NULL ORDER BY v.fg_sku_name",
        *p_a,
    )

    return {
        "customers":  [r['customer_name'] for r in customers],
        "so_numbers": [r['so_number']     for r in so_numbers],
        "articles":   [r['fg_sku_name']   for r in articles],
    }


# ---------------------------------------------------------------------------
# Demand summary, FY review
# ---------------------------------------------------------------------------

async def get_demand_summary(conn, entity: str | None = None,
                              financial_year: str | None = None) -> list[dict]:
    """Aggregate pending demand grouped by product + customer."""
    query = """
        SELECT fg_sku_name, customer_name,
               SUM(pending_qty_kg) AS total_qty_kg,
               COUNT(*) AS order_count,
               MIN(deadline_date) AS earliest_deadline
        FROM so_fulfillment_v2
        WHERE order_status IN ('open', 'partial')
    """
    params: list = []
    idx = 1
    if entity:
        query += f" AND entity = ${idx}"
        params.append(entity); idx += 1
    if financial_year:
        query += f" AND financial_year = ${idx}"
        params.append(financial_year); idx += 1

    query += " GROUP BY fg_sku_name, customer_name ORDER BY MIN(deadline_date), SUM(pending_qty_kg) DESC"
    rows = await conn.fetch(query, *params)
    return [_serialize_row(r) for r in rows]


async def get_fy_review(conn, entity: str | None = None,
                         financial_year: str | None = None) -> list[dict]:
    """Open/partial orders for the chosen FY, grouped by customer.

    Renames so_fulfillment_id -> fulfillment_id and deadline_date ->
    delivery_deadline so the v1 frontend renderer drops in unchanged.
    """
    fy = financial_year
    if not fy:
        fy = _derive_fy(date.today())

    query = f"""
        SELECT {_V2_LIST_SELECT}
        {_V2_FROM_JOIN}
        WHERE v.financial_year = $1
          AND v.order_status IN ('open', 'partial')
    """
    params: list = [fy]
    if entity:
        query += " AND v.entity = $2"
        params.append(entity)

    query += " ORDER BY v.customer_name, v.deadline_date"
    rows = await conn.fetch(query, *params)
    return [_serialize_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Cancel + Carryforward
# ---------------------------------------------------------------------------

async def cancel_orders(conn, so_fulfillment_ids: list[int], *,
                         reason: str = "", cancelled_by: str = "") -> dict:
    """Cancel selected fulfillment rows (skips already-cancelled / fulfilled).
    Writes an audit row per cancellation. Returns counts."""
    cancelled = 0
    for fid in so_fulfillment_ids:
        old = await conn.fetchrow(
            "SELECT order_status FROM so_fulfillment_v2 WHERE so_fulfillment_id = $1",
            fid,
        )
        if not old or old['order_status'] in ('cancelled', 'fulfilled'):
            continue
        await conn.execute(
            """
            UPDATE so_fulfillment_v2
               SET order_status         = 'cancelled',
                   cancelled_by         = $2,
                   cancelled_at         = NOW(),
                   cancellation_reason  = $3
             WHERE so_fulfillment_id = $1
            """,
            fid, cancelled_by, reason,
        )
        await conn.execute(
            """
            INSERT INTO so_revision_log_v2
                (so_fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
            VALUES ($1, 'cancel', $2, 'cancelled', $3, $4)
            """,
            fid, old['order_status'], reason, cancelled_by,
        )
        cancelled += 1
    return {"cancelled": cancelled, "total_requested": len(so_fulfillment_ids)}


async def carryforward_orders(conn, so_fulfillment_ids: list[int], new_fy: str,
                                revised_by: str) -> dict:
    """Bulk carry forward selected rows to a new FY. Uses pk-retry on insert.

    For each row:
      - Insert new so_fulfillment_v2 row in new_fy with pending qty carried over
        and carryforward_from_id pointing back at the original.
      - Mark the original order_status='carryforward'.
      - Audit-log the rollover.
    """
    carried = 0
    for fid in so_fulfillment_ids:
        old = await conn.fetchrow(
            "SELECT * FROM so_fulfillment_v2 WHERE so_fulfillment_id = $1",
            fid,
        )
        if not old or old['order_status'] == 'carryforward':
            continue

        # Skip if there's nothing left to carry
        pending = float(old['pending_qty_kg'] or 0)
        if pending <= 0:
            continue

        pending_units = (float(old['pending_qty_units'])
                         if old['pending_qty_units'] is not None else None)

        async def _do_insert():
            return await conn.fetchval(
                """
                INSERT INTO so_fulfillment_v2 (
                    so_fulfillment_id, so_line_id, financial_year, fg_sku_name,
                    customer_name, entity, original_qty_kg, original_qty_units,
                    deadline_date, order_status, carryforward_from_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'open', $10)
                ON CONFLICT (so_line_id, financial_year) DO NOTHING
                RETURNING so_fulfillment_id
                """,
                new_short_time_id(),
                old['so_line_id'], new_fy, old['fg_sku_name'], old['customer_name'],
                old['entity'], pending, pending_units, old['deadline_date'],
                fid,
            )

        try:
            new_id = await insert_with_pk_retry(conn, _do_insert)
        except UniqueViolationError as exc:
            logger.warning(
                "carryforward: PK collision exhausted for so_fulfillment_id=%s (%s); skipping",
                fid, getattr(exc, 'constraint_name', ''),
            )
            continue

        if not new_id:
            # ON CONFLICT (so_line_id, financial_year) hit — already carried over
            continue

        await conn.execute(
            "UPDATE so_fulfillment_v2 SET order_status = 'carryforward' WHERE so_fulfillment_id = $1",
            fid,
        )
        await conn.execute(
            """
            INSERT INTO so_revision_log_v2
                (so_fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
            VALUES ($1, 'carryforward', $2, $3, 'FY transition', $4)
            """,
            fid, old['financial_year'], new_fy, revised_by,
        )
        carried += 1

    logger.info("Carryforward v2: %d of %d carried to %s", carried, len(so_fulfillment_ids), new_fy)
    return {"carried": carried, "total_requested": len(so_fulfillment_ids), "new_fy": new_fy}


# ---------------------------------------------------------------------------
# Chart summary — dashboard aggregates
# ---------------------------------------------------------------------------

def _build_chart_where(*, entity=None, financial_year=None, customer=None,
                        so_number=None, article=None, status=None):
    """Shared WHERE builder for chart/filter aggregates."""
    conditions: list[str] = []
    params: list = []
    idx = 1
    if entity:
        conditions.append(f"v.entity = ${idx}"); params.append(entity); idx += 1
    if financial_year:
        conditions.append(f"v.financial_year = ${idx}"); params.append(financial_year); idx += 1
    if customer:
        conditions.append(f"v.customer_name = ${idx}"); params.append(customer); idx += 1
    if so_number:
        conditions.append(f"h.so_number = ${idx}"); params.append(so_number); idx += 1
    if article:
        conditions.append(f"v.fg_sku_name ILIKE ${idx}"); params.append(f"%{article}%"); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',')]
        placeholders = ', '.join(f'${idx + i}' for i in range(len(statuses)))
        conditions.append(f"v.order_status IN ({placeholders})")
        params.extend(statuses); idx += len(statuses)
    where = " AND ".join(conditions) if conditions else "TRUE"
    return where, params


async def get_chart_summary(conn, *, entity=None, financial_year=None,
                             customer=None, so_number=None, article=None,
                             status=None) -> dict:
    """Dashboard chart aggregates: by customer / status / deadline / SO + totals."""
    where, params = _build_chart_where(
        entity=entity, financial_year=financial_year, customer=customer,
        so_number=so_number, article=article, status=status,
    )

    base = f"{_V2_FROM_JOIN} WHERE {where}"

    by_customer = await conn.fetch(
        f"SELECT v.customer_name, SUM(v.pending_qty_kg) AS qty, COUNT(*) AS cnt {base}"
        " GROUP BY v.customer_name ORDER BY qty DESC LIMIT 15", *params,
    )
    by_status = await conn.fetch(
        f"SELECT v.order_status, COUNT(*) AS cnt, SUM(v.pending_qty_kg) AS qty {base}"
        " GROUP BY v.order_status ORDER BY cnt DESC", *params,
    )
    by_deadline = await conn.fetch(
        f"SELECT DATE_TRUNC('week', v.deadline_date) AS week_start,"
        f" COUNT(*) AS cnt, SUM(v.pending_qty_kg) AS qty {base}"
        " AND v.deadline_date IS NOT NULL"
        " GROUP BY week_start ORDER BY week_start LIMIT 20", *params,
    )
    by_so = await conn.fetch(
        f"SELECT h.so_number, SUM(v.pending_qty_kg) AS qty, COUNT(*) AS cnt {base}"
        " AND h.so_number IS NOT NULL"
        " GROUP BY h.so_number ORDER BY qty DESC LIMIT 15", *params,
    )
    totals = await conn.fetchrow(
        f"SELECT COUNT(*) AS total_orders, SUM(v.pending_qty_kg) AS total_qty,"
        f" COUNT(DISTINCT v.fg_sku_name) AS unique_skus,"
        f" COUNT(DISTINCT v.customer_name) AS unique_customers,"
        f" MIN(v.deadline_date) AS earliest_deadline {base}", *params,
    )

    return {
        "by_customer": [{"name": r['customer_name'], "qty": float(r['qty'] or 0), "count": r['cnt']} for r in by_customer],
        "by_status":   [{"status": r['order_status'], "count": r['cnt'], "qty": float(r['qty'] or 0)} for r in by_status],
        "by_deadline": [{"week": str(r['week_start'])[:10] if r['week_start'] else None,
                          "count": r['cnt'], "qty": float(r['qty'] or 0)} for r in by_deadline],
        "by_so":       [{"so_number": r['so_number'], "qty": float(r['qty'] or 0), "count": r['cnt']} for r in by_so],
        "totals": {
            "total_orders": totals['total_orders'],
            "total_qty": float(totals['total_qty'] or 0),
            "unique_skus": totals['unique_skus'],
            "unique_customers": totals['unique_customers'],
            "earliest_deadline": str(totals['earliest_deadline']) if totals['earliest_deadline'] else None,
        },
    }


# ---------------------------------------------------------------------------
# BOM Override CRUD (v2)
# ---------------------------------------------------------------------------

async def save_bom_overrides(conn, so_fulfillment_id: int, overrides: list[dict],
                              overridden_by: str = "") -> dict:
    """Save per-fulfillment BOM overrides. Does NOT touch master BOM.

    overrides: each dict can be either:
      - {bom_line_id: <int>, material_sku_name?, quantity_per_unit?,
         loss_pct?, uom?, godown?, is_removed?, override_reason?}  (override existing)
      - {bom_line_id: null OR negative, material_sku_name: <str>, ...}  (added item)
    """
    ful = await conn.fetchrow(
        "SELECT so_fulfillment_id, fg_sku_name, order_status FROM so_fulfillment_v2 WHERE so_fulfillment_id = $1",
        so_fulfillment_id,
    )
    if not ful:
        return {"error": "not_found"}
    if ful['order_status'] not in ('open', 'partial'):
        return {"error": "invalid_status",
                "message": "Can only override BOM for open/partial fulfillments"}

    bom = await conn.fetchrow(
        "SELECT bom_id FROM bom_header WHERE fg_sku_name ILIKE $1 AND is_active = TRUE LIMIT 1",
        ful['fg_sku_name'],
    )
    if not bom:
        return {"error": "no_bom", "message": f"No active BOM for {ful['fg_sku_name']}"}

    valid_ids = {r['bom_line_id'] for r in await conn.fetch(
        "SELECT bom_line_id FROM bom_line WHERE bom_id = $1", bom['bom_id'],
    )}

    # Clear previously added items before re-saving (bom_line_id IS NULL rows)
    await conn.execute(
        "DELETE FROM fulfillment_bom_override_v2 WHERE so_fulfillment_id = $1 AND bom_line_id IS NULL",
        so_fulfillment_id,
    )

    applied = 0
    added = 0
    for ov in overrides:
        blid = ov.get('bom_line_id')

        # Added item — bom_line_id is None or negative (frontend's sentinel)
        if blid is None or blid < 0:
            mat_name = (ov.get('material_sku_name') or '').strip()
            if not mat_name:
                continue
            await conn.execute(
                """
                INSERT INTO fulfillment_bom_override_v2 (
                    so_fulfillment_id, bom_line_id, material_sku_name, quantity_per_unit,
                    loss_pct, uom, godown, is_removed, override_reason, overridden_by
                ) VALUES ($1, NULL, $2, $3, $4, $5, $6, FALSE, $7, $8)
                """,
                so_fulfillment_id, mat_name,
                ov.get('quantity_per_unit'), ov.get('loss_pct'),
                ov.get('uom'), ov.get('godown'),
                ov.get('override_reason', 'Added item'), overridden_by,
            )
            added += 1
            continue

        if blid not in valid_ids:
            continue

        # Partial unique index is WHERE bom_line_id IS NOT NULL; ON CONFLICT
        # arbiter must include the same predicate so Postgres can match it.
        await conn.execute(
            """
            INSERT INTO fulfillment_bom_override_v2 (
                so_fulfillment_id, bom_line_id, material_sku_name, quantity_per_unit,
                loss_pct, uom, godown, is_removed, override_reason, overridden_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (so_fulfillment_id, bom_line_id) WHERE bom_line_id IS NOT NULL
            DO UPDATE SET
                material_sku_name = $3, quantity_per_unit = $4, loss_pct = $5,
                uom = $6, godown = $7, is_removed = $8,
                override_reason = $9, overridden_by = $10, updated_at = NOW()
            """,
            so_fulfillment_id, blid,
            ov.get('material_sku_name'), ov.get('quantity_per_unit'),
            ov.get('loss_pct'), ov.get('uom'), ov.get('godown'),
            ov.get('is_removed', False), ov.get('override_reason', ''),
            overridden_by,
        )
        applied += 1

    total = applied + added
    if total > 0:
        await conn.execute(
            """
            INSERT INTO so_revision_log_v2
                (so_fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
            VALUES ($1, 'bom_override', NULL, $2, 'BOM override applied', $3)
            """,
            so_fulfillment_id,
            f"{applied} lines overridden, {added} items added",
            overridden_by,
        )

    return {"fulfillment_id": so_fulfillment_id, "overrides_applied": applied, "items_added": added}


async def get_bom_overrides(conn, so_fulfillment_id: int) -> dict:
    """Return current BOM with overrides + added items for one fulfillment.
    Frontend-compatible shape: returns fulfillment_id (v1 name)."""
    ful = await conn.fetchrow(
        "SELECT so_fulfillment_id, fg_sku_name FROM so_fulfillment_v2 WHERE so_fulfillment_id = $1",
        so_fulfillment_id,
    )
    if not ful:
        return {"error": "not_found"}

    bom = await conn.fetchrow(
        "SELECT bom_id FROM bom_header WHERE fg_sku_name ILIKE $1 AND is_active = TRUE LIMIT 1",
        ful['fg_sku_name'],
    )
    if not bom:
        return {"fulfillment_id": so_fulfillment_id, "bom_found": False, "overrides": []}

    rows = await conn.fetch(
        """
        SELECT bl.bom_line_id, bl.material_sku_name AS master_material,
               bl.quantity_per_unit AS master_qty, bl.loss_pct AS master_loss,
               bl.uom AS master_uom, bl.item_type,
               o.material_sku_name AS override_material,
               o.quantity_per_unit AS override_qty, o.loss_pct AS override_loss,
               o.uom AS override_uom, o.is_removed, o.override_reason, o.overridden_by
        FROM bom_line bl
        LEFT JOIN fulfillment_bom_override_v2 o
            ON o.bom_line_id = bl.bom_line_id AND o.so_fulfillment_id = $2
        WHERE bl.bom_id = $1
        ORDER BY bl.line_number
        """,
        bom['bom_id'], so_fulfillment_id,
    )

    overrides = []
    for r in rows:
        has_override = (r['override_material'] is not None or r['override_qty'] is not None
                        or r['override_loss'] is not None or r['is_removed'])
        overrides.append({
            "bom_line_id": r['bom_line_id'],
            "item_type": r['item_type'],
            "master": {
                "material_sku_name": r['master_material'],
                "quantity_per_unit": float(r['master_qty']) if r['master_qty'] else None,
                "loss_pct": float(r['master_loss']) if r['master_loss'] else None,
                "uom": r['master_uom'],
            },
            "override": {
                "material_sku_name": r['override_material'],
                "quantity_per_unit": float(r['override_qty']) if r['override_qty'] else None,
                "loss_pct": float(r['override_loss']) if r['override_loss'] else None,
                "uom": r['override_uom'],
                "is_removed": r['is_removed'] or False,
                "reason": r['override_reason'],
            } if has_override else None,
            "has_override": has_override,
        })

    added_rows = await conn.fetch(
        """
        SELECT override_id, material_sku_name, quantity_per_unit, loss_pct,
               uom, override_reason, overridden_by
        FROM fulfillment_bom_override_v2
        WHERE so_fulfillment_id = $1 AND bom_line_id IS NULL
        ORDER BY created_at
        """,
        so_fulfillment_id,
    )
    added_items = [{
        "override_id": r['override_id'],
        "material_sku_name": r['material_sku_name'],
        "quantity_per_unit": float(r['quantity_per_unit']) if r['quantity_per_unit'] else None,
        "loss_pct": float(r['loss_pct']) if r['loss_pct'] else None,
        "uom": r['uom'],
    } for r in added_rows]

    return {
        "fulfillment_id": so_fulfillment_id, "bom_id": bom['bom_id'],
        "overrides": overrides, "added_items": added_items,
    }


# ---------------------------------------------------------------------------
# Floor Stock CRUD (v2) — manual floor-stock entries per fulfillment
# ---------------------------------------------------------------------------

async def get_floor_stock(conn, so_fulfillment_id: int) -> dict:
    """List floor-stock entries for a fulfillment. Returns v1-shape."""
    ful = await conn.fetchrow(
        "SELECT so_fulfillment_id FROM so_fulfillment_v2 WHERE so_fulfillment_id = $1",
        so_fulfillment_id,
    )
    if not ful:
        return {"error": "not_found"}

    rows = await conn.fetch(
        """
        SELECT floor_stock_id, material_sku_name, item_type, quantity_kg,
               unit, floor_location, added_by, notes, created_at
        FROM fulfillment_floor_stock_v2
        WHERE so_fulfillment_id = $1
        ORDER BY created_at
        """,
        so_fulfillment_id,
    )
    return {
        "fulfillment_id": so_fulfillment_id,
        "entries": [{
            "floor_stock_id": r['floor_stock_id'],
            "material_sku_name": r['material_sku_name'],
            "item_type": r['item_type'],
            "quantity_kg": float(r['quantity_kg']),
            "unit": r['unit'] or 'KG',
            "floor_location": r['floor_location'],
            "added_by": r['added_by'],
            "notes": r['notes'],
        } for r in rows],
    }


async def save_floor_stock(conn, so_fulfillment_id: int, entries: list[dict],
                            added_by: str = "") -> dict:
    """Replace floor-stock entries for one fulfillment (full overwrite)."""
    ful = await conn.fetchrow(
        "SELECT so_fulfillment_id FROM so_fulfillment_v2 WHERE so_fulfillment_id = $1",
        so_fulfillment_id,
    )
    if not ful:
        return {"error": "not_found"}

    await conn.execute(
        "DELETE FROM fulfillment_floor_stock_v2 WHERE so_fulfillment_id = $1",
        so_fulfillment_id,
    )

    saved = 0
    for e in entries:
        mat = (e.get('material_sku_name') or '').strip()
        qty = e.get('quantity_kg') or 0
        floor = (e.get('floor_location') or '').strip()
        if not mat or not floor or qty <= 0:
            continue
        await conn.execute(
            """
            INSERT INTO fulfillment_floor_stock_v2
                (so_fulfillment_id, material_sku_name, item_type, quantity_kg,
                 unit, floor_location, added_by, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            so_fulfillment_id, mat, e.get('item_type', 'pm'), qty,
            e.get('unit', 'KG'), floor, added_by, e.get('notes', ''),
        )
        saved += 1

    return {"fulfillment_id": so_fulfillment_id, "entries_saved": saved}


# ---------------------------------------------------------------------------
# Single-fulfillment detail — BOM + inventory + machines + linked SO + logs
# ---------------------------------------------------------------------------

async def get_fulfillment_detail(conn, so_fulfillment_id: int) -> dict | None:
    """Full detail for one v2 fulfillment row.

    Returns a v1-shaped dict so the frontend modal renders unchanged:
        {
          "fulfillment":   {... v1-aliased fulfillment fields ..., is_planned, plan_line_id, pending_qty_warning},
          "bom":           {bom_id, bom_note, process_routes, lines, floors},
          "floor_machines":[ ... ],
          "linked_so":     {... or null},
          "revision_log":  [ ... ],
          "floor_stock":   [ ... ],
        }
    """
    # ----- Q1: fulfillment row with so_number via so_line -> so_header
    row = await conn.fetchrow(
        f"SELECT {_V2_LIST_SELECT} {_V2_FROM_JOIN} WHERE v.so_fulfillment_id = $1",
        so_fulfillment_id,
    )
    if row is None:
        return None
    fulfillment = _serialize_row(row)

    # ----- Q2: is_planned check against production_plan_line linked array
    plan_row = await conn.fetchrow(
        """
        SELECT plan_line_id FROM production_plan_line
        WHERE $1 = ANY(linked_so_fulfillment_ids) LIMIT 1
        """,
        so_fulfillment_id,
    )
    fulfillment["is_planned"]          = plan_row is not None
    fulfillment["plan_line_id"]        = plan_row["plan_line_id"] if plan_row else None
    fulfillment["pending_qty_warning"] = not bool(fulfillment.get("pending_qty_kg"))

    # ----- Q3: linked SO line (early so item_type is available for BOM check)
    so_line_id = row["so_line_id"]
    linked_so = None
    so_item_type = ""
    if so_line_id is not None:
        so_row = await conn.fetchrow(
            """
            SELECT h.so_number, h.so_date, h.customer_name, h.voucher_type,
                   l.line_number, l.sku_name, l.quantity, l.quantity_units,
                   l.uom, l.rate_inr, l.amount_inr, l.total_amount_inr, l.item_type
            FROM so_line l
            JOIN so_header h ON l.so_id = h.so_id
            WHERE l.so_line_id = $1
            """,
            so_line_id,
        )
        if so_row:
            linked_so = _serialize_row(so_row)
            so_item_type = (linked_so.get("item_type") or "")

    # ----- Q4: BOM header
    fg_sku = row["fg_sku_name"]
    bom_row = await conn.fetchrow(
        """
        SELECT bom_id, machines, floors, item_group, notes
        FROM bom_header
        WHERE fg_sku_name ILIKE $1 AND is_active = TRUE
        ORDER BY version DESC LIMIT 1
        """,
        fg_sku,
    )
    bom_id = bom_row["bom_id"] if bom_row else None
    bom_machines_raw = bom_row["machines"] if bom_row else []
    bom_machine_names_lower = {(m or "").strip().lower() for m in (bom_machines_raw or [])}

    process_routes: list = []
    lines: list = []
    bom_note: str | None = None

    if bom_id is None:
        bom_note = ("RM SO — no BOM expected"
                    if so_item_type.lower() == "rm" else "No BOM found")
    else:
        # ----- Q5: process routes
        route_rows = await conn.fetch(
            """
            SELECT step_number, process_name, stage, std_time_min, loss_pct, machine_type
            FROM bom_process_route WHERE bom_id = $1 ORDER BY step_number
            """,
            bom_id,
        )
        for r in route_rows:
            process_routes.append({
                "step_number": r["step_number"],
                "process_name": r["process_name"],
                "stage": r["stage"],
                "std_time_min": float(r["std_time_min"]) if r["std_time_min"] is not None else None,
                "loss_pct": float(r["loss_pct"]) if r["loss_pct"] is not None else 0.0,
                "machine_type": r["machine_type"],
            })

        # ----- Q6: BOM lines merged with v2 overrides
        line_rows = await conn.fetch(
            """
            SELECT
              bl.bom_line_id,
              COALESCE(o.material_sku_name, bl.material_sku_name) AS material_sku_name,
              bl.item_type,
              COALESCE(o.quantity_per_unit, bl.quantity_per_unit) AS quantity_per_unit,
              COALESCE(o.loss_pct, bl.loss_pct) AS loss_pct,
              COALESCE(o.uom, bl.uom) AS uom,
              COALESCE(o.godown, bl.godown) AS godown,
              COALESCE(o.is_removed, FALSE) AS is_removed,
              CASE WHEN o.override_id IS NOT NULL THEN TRUE ELSE FALSE END AS is_overridden,
              o.override_reason,
              bl.process_stage,
              bl.can_use_offgrade
            FROM bom_line bl
            LEFT JOIN fulfillment_bom_override_v2 o
              ON o.bom_line_id = bl.bom_line_id AND o.so_fulfillment_id = $2
            WHERE bl.bom_id = $1
            ORDER BY bl.line_number
            """,
            bom_id, so_fulfillment_id,
        )

        # ----- Q7: Added overrides (bom_line_id IS NULL)
        added_rows = await conn.fetch(
            """
            SELECT override_id, material_sku_name, quantity_per_unit, loss_pct, uom, godown,
                   override_reason, is_removed
            FROM fulfillment_bom_override_v2
            WHERE so_fulfillment_id = $1 AND bom_line_id IS NULL
            ORDER BY created_at
            """,
            so_fulfillment_id,
        )

        pending_qty_kg = float(row["pending_qty_kg"] or 0)

        def _build_line(mat_sku, item_type, qty_per_unit, loss_pct_val,
                        uom, godown, is_removed, is_overridden,
                        override_reason, process_stage, can_use_offgrade,
                        bom_line_id=None):
            qty_per = float(qty_per_unit) if qty_per_unit is not None else 0.0
            lp = float(loss_pct_val) if loss_pct_val is not None else 0.0
            if lp < 100:
                gross = pending_qty_kg * qty_per / (1 - lp / 100) if (1 - lp / 100) != 0 else pending_qty_kg * qty_per
                on_hand_kg = 0.0
                inventory_status = "SHORTAGE"
                shortage_kg = 0.0
            else:
                # loss_pct >= 100 → denominator is zero, gross undefined
                gross = None
                on_hand_kg = 0.0
                inventory_status = "INDETERMINATE"
                shortage_kg = None
            return {
                "bom_line_id": bom_line_id,
                "material_sku_name": mat_sku,
                "item_type": item_type,
                "quantity_per_unit": qty_per,
                "loss_pct": lp,
                "uom": uom,
                "godown": godown,
                "gross_requirement_kg": gross,
                "on_hand_kg": on_hand_kg,
                "inventory_status": inventory_status,
                "shortage_kg": shortage_kg,
                "is_overridden": is_overridden,
                "is_removed": is_removed,
                "override_reason": override_reason,
                "process_stage": process_stage,
                "can_use_offgrade": bool(can_use_offgrade) if can_use_offgrade is not None else False,
            }

        for r in line_rows:
            lines.append(_build_line(
                mat_sku=r["material_sku_name"], item_type=r["item_type"],
                qty_per_unit=r["quantity_per_unit"], loss_pct_val=r["loss_pct"],
                uom=r["uom"], godown=r["godown"],
                is_removed=bool(r["is_removed"]),
                is_overridden=bool(r["is_overridden"]),
                override_reason=r["override_reason"],
                process_stage=r["process_stage"],
                can_use_offgrade=r["can_use_offgrade"],
                bom_line_id=r["bom_line_id"],
            ))
        for r in added_rows:
            lines.append(_build_line(
                mat_sku=r["material_sku_name"], item_type=None,
                qty_per_unit=r["quantity_per_unit"], loss_pct_val=r["loss_pct"],
                uom=r["uom"], godown=r["godown"],
                is_removed=bool(r["is_removed"]),
                is_overridden=True,
                override_reason=r["override_reason"],
                process_stage=None,
                can_use_offgrade=False,
                bom_line_id=None,
            ))

    # ----- Q8: Inventory lookup (fuzzy match per line)
    entity_val = row["entity"] or "cfpl"
    if not row["entity"]:
        logger.warning(
            "so_fulfillment_id=%d has no entity; defaulting to 'cfpl' for inventory/machine queries",
            so_fulfillment_id,
        )
    inv_rows = await conn.fetch(
        """
        SELECT sku_name, SUM(quantity_kg) AS total_kg
        FROM floor_inventory
        WHERE entity = $1 AND quantity_kg > 0
        GROUP BY sku_name
        """,
        entity_val,
    )
    inventory = {(inv["sku_name"] or "").strip().lower(): float(inv["total_kg"]) for inv in inv_rows}

    for line in lines:
        if line["inventory_status"] == "INDETERMINATE":
            continue
        mat_lower = (line["material_sku_name"] or "").strip().lower()
        on_hand = 0.0
        for inv_name, inv_qty in inventory.items():
            if inv_name and mat_lower and (inv_name in mat_lower or mat_lower in inv_name):
                on_hand += inv_qty
        gross = line["gross_requirement_kg"]
        line["on_hand_kg"] = round(on_hand, 3)
        line["inventory_status"] = "SUFFICIENT" if on_hand >= gross else "SHORTAGE"
        line["shortage_kg"] = round(max(0.0, gross - on_hand), 3)

    # ----- Q9: Floor machines with capacity
    machine_rows = await conn.fetch(
        """
        SELECT m.machine_id, m.machine_name, m.machine_type, m.floor,
               m.status, m.allocation,
               array_agg(
                 json_build_object(
                   'stage', mc.stage,
                   'item_group', mc.item_group,
                   'capacity_kg_per_hr', mc.capacity_kg_per_hr
                 )
               ) FILTER (WHERE mc.machine_id IS NOT NULL) AS capacity
        FROM machine m
        LEFT JOIN machine_capacity mc ON m.machine_id = mc.machine_id
        WHERE m.entity = $1
        GROUP BY m.machine_id, m.machine_name, m.machine_type, m.floor, m.status, m.allocation
        ORDER BY m.floor, m.machine_name
        """,
        entity_val,
    )

    floor_machines = []
    for m in machine_rows:
        mname_lower = (m["machine_name"] or "").strip().lower()
        is_in_bom = mname_lower in bom_machine_names_lower if bom_machine_names_lower else False
        raw_cap = m["capacity"]
        capacity = []
        if raw_cap:
            for cap_item in raw_cap:
                if isinstance(cap_item, str):
                    cap_item = json.loads(cap_item)
                capacity.append({
                    "stage": cap_item.get("stage"),
                    "item_group": cap_item.get("item_group"),
                    "capacity_kg_per_hr": float(cap_item["capacity_kg_per_hr"]) if cap_item.get("capacity_kg_per_hr") is not None else None,
                })
        floor_machines.append({
            "machine_id": m["machine_id"], "machine_name": m["machine_name"],
            "machine_type": m["machine_type"], "floor": m["floor"],
            "status": m["status"], "allocation": m["allocation"],
            "is_in_bom": is_in_bom, "capacity": capacity,
        })

    # ----- Q10: Revision log + floor stock (sequential — single asyncpg conn)
    rev_rows = await conn.fetch(
        "SELECT * FROM so_revision_log_v2 WHERE so_fulfillment_id = $1 ORDER BY revised_at DESC LIMIT 10",
        so_fulfillment_id,
    )
    stock_rows = await conn.fetch(
        "SELECT * FROM fulfillment_floor_stock_v2 WHERE so_fulfillment_id = $1 ORDER BY created_at",
        so_fulfillment_id,
    )

    return {
        "fulfillment": fulfillment,
        "bom": {
            "bom_id": bom_id, "bom_note": bom_note,
            "process_routes": process_routes, "lines": lines,
            "floors": list(bom_row["floors"] or []) if bom_row else [],
        },
        "floor_machines": floor_machines,
        "linked_so": linked_so,
        "revision_log": [_serialize_row(r) for r in rev_rows],
        "floor_stock":  [_serialize_row(r) for r in stock_rows],
    }


# ---------------------------------------------------------------------------
# Customer-grouped enriched view — BOM + process route + floor + inventory
# ---------------------------------------------------------------------------

async def get_enriched_fulfillment(conn, *, entity: str | None = None,
                                    financial_year: str | None = None,
                                    customer: str | None = None) -> dict:
    """Customer-grouped fulfillment view, batched (~8 queries regardless of volume).

    Mirrors v1's response shape; per-row `fulfillment_id` is the v2
    `so_fulfillment_id` (aliased for frontend compatibility).
    """
    # 1. Open/partial fulfillments
    conditions = ["v.order_status IN ('open', 'partial')"]
    params: list = []
    idx = 1
    if entity:
        conditions.append(f"v.entity = ${idx}"); params.append(entity); idx += 1
    if financial_year:
        conditions.append(f"v.financial_year = ${idx}"); params.append(financial_year); idx += 1
    if customer:
        conditions.append(f"v.customer_name ILIKE ${idx}"); params.append(f"%{customer}%"); idx += 1
    where = " AND ".join(conditions)

    fulfillments = await conn.fetch(
        f"""
        SELECT v.so_fulfillment_id AS fulfillment_id,
               v.fg_sku_name, v.customer_name, v.pending_qty_kg,
               v.deadline_date AS delivery_deadline, v.order_status, v.entity,
               h.so_number AS so_number
        {_V2_FROM_JOIN}
        WHERE {where}
        ORDER BY v.customer_name, v.deadline_date, v.so_fulfillment_id
        """,
        *params,
    )

    if not fulfillments:
        return {"customers": [], "summary": {"total_customers": 0, "total_articles": 0, "materials_with_shortage": 0}}

    ent = entity or (fulfillments[0]['entity'] if fulfillments else 'cfpl')

    # 2. Batch-fetch all active BOMs
    all_boms = await conn.fetch(
        "SELECT bom_id, fg_sku_name, process_category, item_group, floors, machines, output_uom "
        "FROM bom_header WHERE is_active = TRUE",
    )
    bom_by_name: dict[str, dict] = {}
    for b in all_boms:
        key = (b['fg_sku_name'] or '').strip().lower()
        if key not in bom_by_name:
            bom_by_name[key] = dict(b)

    fg_names = list({r['fg_sku_name'] for r in fulfillments})
    bom_map: dict[str, dict] = {}
    for fg in fg_names:
        key = fg.strip().lower()
        if key in bom_by_name:
            bom_map[fg] = bom_by_name[key]

    # 3. Process routes for matched BOMs
    bom_ids = list({b['bom_id'] for b in bom_map.values()})
    route_map: dict[int, list[dict]] = {bid: [] for bid in bom_ids}
    if bom_ids:
        routes = await conn.fetch(
            "SELECT bom_id, step_number, process_name, stage, std_time_min, loss_pct "
            "FROM bom_process_route WHERE bom_id = ANY($1) ORDER BY bom_id, step_number",
            bom_ids,
        )
        for r in routes:
            route_map[r['bom_id']].append(dict(r))

    # 4. BOM lines for matched BOMs
    bom_lines_map: dict[int, list[dict]] = {bid: [] for bid in bom_ids}
    if bom_ids:
        all_lines = await conn.fetch(
            "SELECT bom_line_id, bom_id, line_number, material_sku_name, item_type, "
            "quantity_per_unit, uom, loss_pct, godown "
            "FROM bom_line WHERE bom_id = ANY($1) ORDER BY bom_id, line_number",
            bom_ids,
        )
        for bl in all_lines:
            bom_lines_map[bl['bom_id']].append(dict(bl))

    # 5. v2 overrides for all fulfillment_ids in one query
    fids = [f['fulfillment_id'] for f in fulfillments]
    override_map: dict[tuple[int, int], dict] = {}
    if fids:
        overrides = await conn.fetch(
            "SELECT so_fulfillment_id, bom_line_id, material_sku_name, quantity_per_unit, "
            "loss_pct, uom, godown, is_removed "
            "FROM fulfillment_bom_override_v2 WHERE so_fulfillment_id = ANY($1)",
            fids,
        )
        for o in overrides:
            override_map[(o['so_fulfillment_id'], o['bom_line_id'])] = dict(o)

    # 6. Floor + machine mapping
    floor_cache: dict[tuple[str, str | None], str | None] = {}
    machine_cache: dict[tuple[str, str | None], list[dict]] = {}
    if bom_ids:
        floor_rows = await conn.fetch(
            "SELECT mc.stage, mc.item_group, m.floor, m.factory, "
            "m.machine_name, mc.capacity_kg_per_hr "
            "FROM machine_capacity mc JOIN machine m ON mc.machine_id = m.machine_id "
            "WHERE m.entity = $1 AND m.status = 'active' "
            "ORDER BY mc.stage, m.floor, m.machine_name",
            ent,
        )
        for r in floor_rows:
            key = ((r['stage'] or '').lower(), (r['item_group'] or '').lower())
            if key not in floor_cache:
                floor_cache[key] = r['floor']
            if key not in machine_cache:
                machine_cache[key] = []
            machine_cache[key].append({
                "machine_name": r['machine_name'],
                "floor": r['floor'],
                "factory": r['factory'],
                "capacity_kg_per_hr": float(r['capacity_kg_per_hr']) if r['capacity_kg_per_hr'] else None,
            })

    # 7. Build articles (pure Python from here)
    all_material_skus: set[str] = set()
    articles_by_customer: dict[str, list[dict]] = {}

    for f in fulfillments:
        cust = f['customer_name'] or 'Unknown'
        fg = f['fg_sku_name']
        bom = bom_map.get(fg)
        pending = float(f['pending_qty_kg'] or 0)

        article: dict = {
            "fulfillment_id": f['fulfillment_id'],
            "fg_sku_name": fg,
            "pending_qty_kg": pending,
            "delivery_deadline": str(f['delivery_deadline']) if f['delivery_deadline'] else None,
            "order_status": f['order_status'],
            "so_number": f.get('so_number'),
            "bom_id": bom['bom_id'] if bom else None,
            "bom_found": bom is not None,
            "process_route": [],
            "materials": [],
            "has_overrides": False,
        }

        if bom:
            bid = bom['bom_id']
            allowed_floors = bom.get('floors') or []
            item_group = (bom.get('item_group') or '').lower()

            for i, step in enumerate(route_map.get(bid, [])):
                stage_key = ((step['stage'] or '').lower(), item_group)
                floor = floor_cache.get(stage_key)
                if not floor and allowed_floors:
                    floor = allowed_floors[min(i, len(allowed_floors) - 1)]
                machines = machine_cache.get(stage_key, [])
                article["process_route"].append({
                    "step_number": step['step_number'],
                    "process_name": step['process_name'],
                    "stage": step['stage'],
                    "floor": floor,
                    "machines": machines,
                    "std_time_min": float(step['std_time_min']) if step['std_time_min'] else None,
                    "loss_pct": float(step['loss_pct']) if step['loss_pct'] else None,
                })

            has_override = False
            for bl in bom_lines_map.get(bid, []):
                ov = override_map.get((f['fulfillment_id'], bl['bom_line_id']))
                if ov and ov.get('is_removed'):
                    continue
                mat_name = (ov or {}).get('material_sku_name') or bl['material_sku_name']
                qty_per = float((ov or {}).get('quantity_per_unit') or bl['quantity_per_unit'] or 0)
                loss = float((ov or {}).get('loss_pct') or bl['loss_pct'] or 0)
                uom = (ov or {}).get('uom') or bl['uom']
                is_overridden = ov is not None
                if is_overridden:
                    has_override = True
                gross = (pending * qty_per / (1 - loss / 100)) if loss < 100 else (pending * qty_per)
                all_material_skus.add(mat_name)
                article["materials"].append({
                    "bom_line_id": bl['bom_line_id'],
                    "material_sku_name": mat_name,
                    "item_type": bl['item_type'],
                    "quantity_per_unit": qty_per,
                    "loss_pct": loss,
                    "uom": uom,
                    "gross_requirement_kg": round(gross, 3),
                    "on_hand_kg": 0.0,
                    "status": "UNKNOWN",
                    "is_overridden": is_overridden,
                })
            article["has_overrides"] = has_override

        articles_by_customer.setdefault(cust, []).append(article)

    # 8. Batch inventory lookup
    inv_map: dict[str, float] = {}
    if all_material_skus:
        inv_rows = await conn.fetch(
            """
            SELECT sku_name, COALESCE(SUM(quantity_kg), 0) AS qty
            FROM floor_inventory
            WHERE sku_name = ANY($1) AND entity = $2
            GROUP BY sku_name
            """,
            list(all_material_skus), ent,
        )
        inv_map = {r['sku_name']: float(r['qty']) for r in inv_rows}

    shortage_count = 0
    for articles in articles_by_customer.values():
        for art in articles:
            for mat in art['materials']:
                on_hand = inv_map.get(mat['material_sku_name'], 0.0)
                mat['on_hand_kg'] = round(on_hand, 3)
                mat['status'] = 'SUFFICIENT' if on_hand >= mat['gross_requirement_kg'] else 'SHORTAGE'
                if mat['status'] == 'SHORTAGE':
                    shortage_count += 1

    customers = []
    for cust, articles in articles_by_customer.items():
        total_kg = sum(a['pending_qty_kg'] for a in articles)
        earliest = min((a['delivery_deadline'] for a in articles if a['delivery_deadline']), default=None)
        customers.append({
            "customer_name": cust,
            "total_pending_kg": round(total_kg, 3),
            "order_count": len(articles),
            "earliest_deadline": earliest,
            "articles": articles,
        })

    return {
        "customers": customers,
        "summary": {
            "total_customers": len(customers),
            "total_articles": sum(len(c['articles']) for c in customers),
            "materials_with_shortage": shortage_count,
        },
    }
