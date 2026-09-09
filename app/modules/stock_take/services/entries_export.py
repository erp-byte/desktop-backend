"""Raw floor-count rows from `new_stock_entries`, for the Excel download.

The table and its IST day expression both come from business_day, which explains
why they must be imported together rather than written out here.

WHAT THIS IS, AND HOW IT DIFFERS FROM THE OTHER TWO READS
    /latest-stock          aggregated: one row per article+place, netted
    /transactions/export   the adjustment ledger
    /entries/export        THIS — every individual count row, unaggregated

A count row is ONE WEIGHING, not an article's stock: a single article can
contribute dozens of rows in an afternoon (38 rows for PUMPKIN SEEDS ROASTED on
one floor, 10-20s apart). That is exactly why the floor app exports raw rows —
the sheet is the audit trail behind the aggregate, so the two are not
interchangeable and neither replaces the other.

PORTED FROM backend_st/routes/exports.ts::exportStocktakeEntries, with three
deliberate corrections; see fetch_entries and export_xlsx.build_entries_workbook.
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg

from .business_day import ENTRIES_TABLE, ENTRY_DAY
from .latest_stock_service import _build_filters, normalise_date

# Columns the Express export reads, in its own SELECT order. The three
# timestamps are converted to IST *in SQL* rather than formatted in Python:
# the column is naive-holding-UTC (see business_day), and the Express version
# formats with JS `new Date().getHours()`, which renders in the SERVER's zone —
# UTC on Lambda. Every date/time in that spreadsheet is therefore 5h30m early.
_SELECT = f"""
    id,
    entry_id,
    item_name,
    item_type,
    item_category,
    item_subcategory,
    floor_name,
    warehouse,
    total_quantity,
    unit_uom,
    total_weight,
    entered_by,
    entered_by_email,
    authority,
    COALESCE(stock_type, 'Fresh Stock') AS stock_type,
    status,
    verified,
    verified_by,
    remark,
    -- ONE step. These are timestamptz here, so `AT TIME ZONE 'Asia/Kolkata'`
    -- turns an absolute instant into IST wall-clock, which is what belongs in the
    -- sheet. The extra `AT TIME ZONE 'UTC'` this used to carry was there for the
    -- floor app's NAIVE column; against a tz-aware one it strips the zone off an
    -- already-absolute instant and puts the sheet 5:30 BEHIND UTC instead of 5:30
    -- ahead -- an 11-hour error that still renders as a plausible timestamp.
    created_at  AT TIME ZONE 'Asia/Kolkata' AS created_at,
    updated_at  AT TIME ZONE 'Asia/Kolkata' AS updated_at,
    verified_at AT TIME ZONE 'Asia/Kolkata' AS verified_at
"""

# Express's ORDER BY, kept verbatim so a row-by-row diff against the floor app's
# sheet lines up.
_ORDER = ("ORDER BY item_type DESC, stock_type ASC, created_at DESC,"
          " warehouse, floor_name, item_name")


def _day_bounds(date_from: Optional[str], date_to: Optional[str]) -> tuple[Any, Any]:
    """Validate the range, rejecting rather than dropping an unusable value.

    Silently ignoring a bad `dateFrom` would widen the export to every row ever
    counted, which is indistinguishable from success in a downloaded file.
    """
    lo = normalise_date(date_from)
    hi = normalise_date(date_to)
    if date_from and not lo:
        raise ValueError(f"Invalid dateFrom {date_from!r}; expected YYYY-MM-DD")
    if date_to and not hi:
        raise ValueError(f"Invalid dateTo {date_to!r}; expected YYYY-MM-DD")
    if lo and hi and lo > hi:
        raise ValueError(f"dateFrom {lo} is after dateTo {hi}")
    return lo, hi


async def fetch_entries(
    conn: asyncpg.Connection,
    *,
    warehouse: Any = None,
    floor_name: Any = None,
    item_type: Any = None,
    category: Any = None,
    subcategory: Any = None,
    stock_type: Any = None,
    entered_by: Optional[str] = None,
    search: Optional[str] = None,
    verified: Optional[bool] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """Rows, an echo of the filters applied, and the count of drafts excluded.

    THREE CORRECTIONS TO THE EXPRESS QUERY
    1. The date window is cut on the IST calendar day. Express compares the raw
       naive column against a bare date literal, which cuts the day at 05:30 IST;
       140 of 8364 live rows (1.7%) fall on a different day under the two rules,
       so a single-day export silently gains one night's rows and loses another.
    2. The draft count honours EVERY filter. Express applies only `warehouse` to
       it, so a one-floor, one-day export is footnoted with the draft count for
       the whole warehouse across all time.
    3. `source_kind = 'COUNT'` (via _build_filters) keeps console adjustments out.
       They are the ledger's business and are exported by /transactions/export;
       a row appearing in both would be double-counted by anyone summing the two.

    _build_filters is this module's neighbour and is reused deliberately, private
    name and all: it IS the definition of "a filtered set of count rows", and a
    second copy here would drift from the screen the export is launched from.
    """
    filters = dict(
        warehouse=warehouse, floor_name=floor_name, item_type=item_type,
        category=category, subcategory=subcategory, stock_type=stock_type,
        entered_by=entered_by, search=search, verified=verified,
    )
    lo, hi = _day_bounds(date_from, date_to)

    conds, params, applied = _build_filters(include_drafts=False, **filters)
    if lo:
        params.append(lo)
        conds.append(f"{ENTRY_DAY} >= ${len(params)}")
        applied["dateFrom"] = lo.isoformat()
    if hi:
        params.append(hi)
        conds.append(f"{ENTRY_DAY} <= ${len(params)}")
        applied["dateTo"] = hi.isoformat()
    applied.pop("includeDrafts", None)  # drafts are never exported; not a choice

    where = " AND ".join(conds)
    rows = [dict(r) for r in await conn.fetch(
        f"SELECT {_SELECT} FROM {ENTRIES_TABLE} WHERE {where} {_ORDER}", *params)]

    # Same filters, drafts only — see correction 2 above.
    d_conds, d_params, _ = _build_filters(include_drafts=True, **filters)
    d_conds.append("status = 'draft'")
    if lo:
        d_params.append(lo)
        d_conds.append(f"{ENTRY_DAY} >= ${len(d_params)}")
    if hi:
        d_params.append(hi)
        d_conds.append(f"{ENTRY_DAY} <= ${len(d_params)}")
    drafts = await conn.fetchval(
        f"SELECT COUNT(*)::int FROM {ENTRIES_TABLE} WHERE {' AND '.join(d_conds)}",
        *d_params)

    return rows, applied, int(drafts or 0)
