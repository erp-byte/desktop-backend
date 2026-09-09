"""BOM aggregate + detail reads backing /api/v1/bom/*.

Two read shapes over the three BOM tables:

  * `list_bom_aggregate` — ONE ROW PER BOM, with every figure rolled up IN SQL
    (never in Python), so the page rows and the pagination total can never
    disagree about what a filter means.
  * `get_bom_detail`     — one BOM's header plus its lines and its process
    route as two INDEPENDENT, separately ordered collections.

CRITICAL MODELLING FACT — do not "optimise" this away:
    bom_header → bom_line          is a REAL FK on bom_id.
    bom_header → bom_process_route is a REAL FK on bom_id.
    bom_line  ↔  bom_process_route have NO FK AND NO RELIABLE JOIN KEY.
    They relate only through free text written by two different ingest paths:
    `bom_line.consumed_at_stage` holds values like 'Final FG (opening RM)'
    (production/services/master_ingest.py), while
    `bom_process_route.practical_operation` holds values like
    'Roast & Flavour/Salt' (production/services/bar_line_service.py) and
    `bom_process_route.stage` holds slugs like 'packing' / 'create_wip'.
    Joining the two on any of those silently DROPS or MIS-BUCKETS lines.
    So they are never joined here: `consumed_at_stage` / `process_stage` are
    surfaced as plain COLUMNS on the line, and the route is returned as its own
    ordered strip. The two LATERALs below are deliberately disjoint — one reads
    bom_line only, the other bom_process_route only.

WHY `LEFT JOIN LATERAL` AND NOT A JOIN + GROUP BY:
    This screen exists to surface BROKEN BOMs — the ones with no lines at all,
    or with no process route. `FROM bom_header h JOIN bom_line ... GROUP BY`
    (or an inner lateral) drops precisely those headers, so the screen would
    hide the only records anybody opens it to find. Each lateral is a
    self-contained aggregate correlated on bom_id and attached `ON TRUE`, so a
    header with zero children still yields exactly one row, and the COALESCEs
    turn its NULL aggregates into zeros. Row multiplicity is impossible: an
    aggregate subquery with no GROUP BY always returns exactly one row.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal


def _serialize_row(row) -> dict:
    """Coerce asyncpg Record types (Decimal, date, datetime) to JSON-safe values.

    Local by necessity: `app/core/` carries no shared row normaliser (helpers.py
    has scalar coercers only), so the same eight lines are hand-rolled at every
    call site in this repo — see fulfillment_v2._serialize_row, plan_v2.
    _serialize_row, job_card_batch_v2._serialize. This is that idiom, not a new
    one. It matters here because the pool sets no asyncpg type codecs, so every
    NUMERIC (quantity_per_unit, loss_pct, pack_size_kg, std_time_min, ...)
    arrives as Decimal and every DATE/TIMESTAMPTZ as date/datetime — neither of
    which a consumer of a plain dict can serialise.
    """
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ── Aggregate list ───────────────────────────────────────────────────────────
#
# `{where}` is only ever filled with generated `$N` predicates — user input goes
# into the `params` list and reaches Postgres as bind parameters, never as SQL
# text. `{limit_idx}` / `{offset_idx}` are integers computed from the filter
# count, not from anything a caller sends.
#
# avg_line_loss_pct is a SIMPLE (unweighted) mean of bom_line.loss_pct across
# the BOM's lines. It is deliberately NOT weighted by quantity_per_unit: RM
# lines are quantified in kg and PM lines in pcs, so a quantity weighting would
# be summing kilograms and pouches into one meaningless denominator. NULL (no
# lines) is left as NULL rather than coalesced to 0 — "no lines" is not "0% loss".
_AGGREGATE_SQL = """
SELECT
    h.bom_id,
    h.fg_sku_name,
    h.customer_name,
    h.version,
    h.is_active,
    h.entity,
    h.item_group,
    h.sub_group,
    h.pack_size_kg,
    h.output_uom,
    h.effective_from,
    h.effective_to,
    COALESCE(l.line_count, 0)                     AS line_count,
    COALESCE(l.rm_count, 0)                       AS rm_count,
    COALESCE(l.pm_count, 0)                       AS pm_count,
    COALESCE(l.other_count, 0)                    AS other_count,
    COALESCE(l.total_qty_per_unit, 0)             AS total_qty_per_unit,
    COALESCE(l.total_qty_rm, 0)                   AS total_qty_rm,
    COALESCE(l.total_qty_pm, 0)                   AS total_qty_pm,
    l.avg_line_loss_pct                           AS avg_line_loss_pct,
    COALESCE(l.distinct_godowns, ARRAY[]::text[]) AS distinct_godowns,
    COALESCE(l.has_offgrade_lines, FALSE)         AS has_offgrade_lines,
    COALESCE(r.step_count, 0)                     AS step_count,
    COALESCE(r.total_std_time_min, 0)             AS total_std_time_min,
    COALESCE(r.total_route_loss_pct, 0)           AS total_route_loss_pct
FROM bom_header h
LEFT JOIN LATERAL (
    SELECT
        COUNT(*)                                                         AS line_count,
        COUNT(*) FILTER (WHERE lower(bl.item_type) = 'rm')               AS rm_count,
        COUNT(*) FILTER (WHERE lower(bl.item_type) = 'pm')               AS pm_count,
        COUNT(*) FILTER (WHERE bl.item_type IS NULL
                            OR lower(bl.item_type) NOT IN ('rm', 'pm'))  AS other_count,
        COALESCE(SUM(bl.quantity_per_unit), 0)                           AS total_qty_per_unit,
        COALESCE(SUM(bl.quantity_per_unit)
                 FILTER (WHERE lower(bl.item_type) = 'rm'), 0)           AS total_qty_rm,
        COALESCE(SUM(bl.quantity_per_unit)
                 FILTER (WHERE lower(bl.item_type) = 'pm'), 0)           AS total_qty_pm,
        AVG(bl.loss_pct)                                                 AS avg_line_loss_pct,
        ARRAY_AGG(DISTINCT bl.godown ORDER BY bl.godown)
            FILTER (WHERE bl.godown IS NOT NULL AND bl.godown <> '')     AS distinct_godowns,
        BOOL_OR(COALESCE(bl.can_use_offgrade, FALSE))                    AS has_offgrade_lines
    FROM bom_line bl
    WHERE bl.bom_id = h.bom_id
) l ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*)                          AS step_count,
        COALESCE(SUM(pr.std_time_min), 0) AS total_std_time_min,
        COALESCE(SUM(pr.loss_pct), 0)     AS total_route_loss_pct
    FROM bom_process_route pr
    WHERE pr.bom_id = h.bom_id
) r ON TRUE
WHERE {where}
ORDER BY h.fg_sku_name ASC, h.version DESC, h.bom_id ASC
LIMIT ${limit_idx} OFFSET ${offset_idx}
"""

# The count MUST see byte-identical predicates and byte-identical params to the
# page query, or "3 of 47" would be a lie. Every filter below is a predicate on
# bom_header (or a correlated EXISTS), never a join, so the same `{where}` drops
# straight into this simpler FROM with no laterals needed.
_COUNT_SQL = """
SELECT COUNT(*)
FROM bom_header h
WHERE {where}
"""


def _build_filters(*, search, entity, item_group, customer_name, is_active,
                   item_type):
    """Return (where_sql, params, next_idx).

    Positional `$N` with a manual `idx` counter and a `params` list — the exact
    idiom in production_indent_service.list_production_indents. Nothing a caller
    sends is ever interpolated into the SQL string; only the generated `$N`
    tokens are.
    """
    conditions = []
    params = []
    idx = 1

    if search:
        conditions.append(
            f"(h.fg_sku_name ILIKE ${idx} OR h.customer_name ILIKE ${idx}"
            f" OR h.item_group ILIKE ${idx} OR h.sub_group ILIKE ${idx}"
            f" OR h.bom_id::text ILIKE ${idx})"
        )
        params.append(f"%{search}%")
        idx += 1
    if entity:
        conditions.append(f"h.entity = ${idx}")
        params.append(entity)
        idx += 1
    # Both of these are wired to FREE-TEXT boxes in the UI, not pickers, so
    # they must behave the way a free-text box looks like it behaves:
    # case-insensitive substring. Exact matching (`=`, or a wildcard-less
    # ILIKE, which is just case-insensitive equality) returns an empty page for
    # "snacks" when the column holds "Snacks", or for "Britan" when it holds
    # "Britannia Industries Ltd" — with nothing on screen to say that only the
    # verbatim stored value works. That is indistinguishable from "no such
    # BOMs". The Tally export that feeds bom_header writes both columns with
    # inconsistent casing and trailing detail, which makes exact matching a
    # near-guaranteed empty result rather than a rare one.
    if item_group:
        conditions.append(f"h.item_group ILIKE ${idx}")
        params.append(f"%{item_group}%")
        idx += 1
    if customer_name:
        conditions.append(f"h.customer_name ILIKE ${idx}")
        params.append(f"%{customer_name}%")
        idx += 1
    if is_active is not None:
        conditions.append(f"h.is_active = ${idx}")
        params.append(is_active)
        idx += 1
    if item_type:
        # "BOMs that HAVE at least one line of this type". A correlated EXISTS,
        # NOT a join: a join would both multiply the header row per matching
        # line and — worse — silently convert the whole query into an inner join
        # against bom_line, hiding every zero-line BOM.
        conditions.append(
            f"EXISTS (SELECT 1 FROM bom_line f_bl"
            f" WHERE f_bl.bom_id = h.bom_id"
            f" AND lower(f_bl.item_type) = lower(${idx}))"
        )
        params.append(item_type)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"
    return where, params, idx


async def list_bom_aggregate(conn, *, search=None, entity=None, item_group=None,
                             customer_name=None, is_active=None, item_type=None,
                             page=1, page_size=50):
    """One row per BOM with rolled-up line + route figures.

    Returns the house pagination envelope:
        { "results": [...], "pagination": {page, page_size, total, total_pages} }
    """
    where, params, idx = _build_filters(
        search=search, entity=entity, item_group=item_group,
        customer_name=customer_name, is_active=is_active, item_type=item_type,
    )

    total = await conn.fetchval(_COUNT_SQL.format(where=where), *params)
    total = total or 0

    rows = await conn.fetch(
        _AGGREGATE_SQL.format(where=where, limit_idx=idx, offset_idx=idx + 1),
        *params, page_size, (page - 1) * page_size,
    )

    return {
        "results": [_serialize_row(r) for r in rows],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
        },
    }


# ── Single BOM detail ────────────────────────────────────────────────────────
_HEADER_SQL = "SELECT * FROM bom_header h WHERE h.bom_id = $1"

# Two separate statements against two separate tables, ON PURPOSE. See the
# CRITICAL MODELLING FACT in the module docstring: there is no key to join these
# on, so they are fetched and returned side by side and the frontend renders the
# route as its own numbered strip rather than nesting lines under steps.
_LINES_SQL = "SELECT * FROM bom_line WHERE bom_id = $1 ORDER BY line_number ASC"
_ROUTE_SQL = ("SELECT * FROM bom_process_route WHERE bom_id = $1"
              " ORDER BY step_number ASC")


async def get_bom_detail(conn, bom_id: int):
    """Full detail for one BOM, or None when `bom_id` does not exist."""
    header = await conn.fetchrow(_HEADER_SQL, bom_id)
    if not header:
        return None

    lines = await conn.fetch(_LINES_SQL, bom_id)
    route = await conn.fetch(_ROUTE_SQL, bom_id)

    lines = [_serialize_row(r) for r in lines]
    route = [_serialize_row(r) for r in route]

    # Counted off the rows already in hand rather than a fourth round trip: the
    # detail payload is a single BOM, so the whole population is local. The
    # buckets match the aggregate query's FILTER clauses exactly (lower()d,
    # NULL/unknown item_type falls into `other`) so a row's collapsed counts and
    # its expanded counts agree.
    def _n(kind):
        return sum(1 for r in lines
                   if (r.get("item_type") or "").lower() == kind)

    return {
        "header": _serialize_row(header),
        "lines": lines,
        "route": route,
        "counts": {
            "line_count": len(lines),
            "rm_count": _n("rm"),
            "pm_count": _n("pm"),
            "other_count": sum(
                1 for r in lines
                if (r.get("item_type") or "").lower() not in ("rm", "pm")
            ),
            "step_count": len(route),
        },
    }
