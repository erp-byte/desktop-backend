"""Everything recorded on ONE warehouse + floor, netted — for the job card's material tab.

The job card's "Material allocation and requisition" tab shows what is available
on the job card's own floor. `latest_stock_service.fetch_latest_stock` answers a
different question: it totals each article ACROSS the places a filter selects,
one row per article. Asked for a single floor it would give the same figures, but
through a paginated aggregate built for the Stock Take screen. This is the direct
form of that read for exactly one place.

THE FIGURES MUST AGREE WITH THE STOCK TAKE SCREEN, so the rules are its rules:
  * physical counts only (COUNT_ROWS_ONLY), drafts excluded;
  * duplicates on the same article/place/day are SUMMED before the latest day is
    picked — see latest_stock_service, where skipping that step lost a third of
    all stock;
  * each article is carried forward from its own newest count at this place, and
    netted with the ledger rows posted at this place on or after that day;
  * an article adjusted here but never counted still appears (FULL OUTER JOIN);
  * Fresh Stock and Off Grade/Rejection stay separate rows, never summed.
tests/services/test_stock_take_floor_stock_live_sql.py checks every place in the
live table against fetch_latest_stock filtered to that place.

Read-only. Nothing in this module writes.
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from .business_day import ENTRIES_TABLE, ENTRY_DAY, TXN_DAY
from .latest_stock_service import COUNT_ROWS_ONLY
from .transactions_service import _norm, _normalise_warehouse

log = logging.getLogger(__name__)

# A missing table/column is an environment state, not a bug: the entries table is
# not part of this app's schema (see latest_stock_service's module docstring).
_MISSING_SCHEMA = (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError)

# $1 = warehouse (unhyphenated, upper-case), $2 = floor (upper-case, trimmed).
# The column side drops hyphens too: the cold stores' counts are stored 'D-39'.
# Keys are COALESCEd exactly as latest_stock_service keys them, so a NULL floor
# or warehouse can never silently drop out of a join.
_FLOOR_STOCK_SQL = f"""
    WITH scoped AS (
        SELECT * FROM {ENTRIES_TABLE}
         WHERE (status IS NULL OR status != 'draft')
           AND {COUNT_ROWS_ONLY}
           AND COALESCE(REPLACE(UPPER(BTRIM(warehouse)), '-', ''), '') = $1
           AND COALESCE(UPPER(BTRIM(floor_name)), '') = $2
    ),
    -- Sum duplicates FIRST, then pick the latest day.
    place_day AS (
        SELECT UPPER(BTRIM(item_name))             AS k_item,
               COALESCE(stock_type, 'Fresh Stock') AS k_stock,
               {ENTRY_DAY}                         AS count_day,
               MIN(item_name)                      AS item_name,
               MIN(item_type)                      AS item_type,
               MIN(item_category)                  AS item_category,
               MIN(item_subcategory)               AS item_subcategory,
               MIN(unit_uom)                       AS unit_uom,
               COALESCE(SUM(total_quantity), 0)    AS q,
               COALESCE(SUM(total_weight), 0)      AS w,
               COUNT(*)::bigint                    AS n
          FROM scoped
         GROUP BY 1, 2, 3
    ),
    -- Each article carried forward from ITS OWN newest count at this place.
    counted AS (
        SELECT * FROM (
            SELECT pd.*,
                   ROW_NUMBER() OVER (PARTITION BY k_item, k_stock
                                          ORDER BY count_day DESC) AS rn
              FROM place_day pd) ranked
         WHERE rn = 1
    ),
    -- Ledger rows at this place, on or after that article's own count day.
    -- Columns are qualified: `counted` also carries item_name and friends.
    -- TXN_DAY's bare created_at is unambiguous — only the ledger has one.
    txn AS (
        SELECT UPPER(BTRIM(t.item_name))                          AS k_item,
               COALESCE(t.stock_type, 'Fresh Stock')              AS k_stock,
               MIN(t.item_name)                                   AS item_name,
               MIN(t.material_type)                               AS item_type,
               MIN(t.item_category)                               AS item_category,
               MIN(t.item_subcategory)                            AS item_subcategory,
               COALESCE(SUM(CASE WHEN t.operation = 'ADDITION'
                                 THEN t.qty_kg ELSE -t.qty_kg END), 0) AS net_kg,
               COALESCE(SUM(CASE WHEN t.operation = 'ADDITION'
                                 THEN t.units ELSE -t.units END), 0)   AS net_units,
               COUNT(*)::bigint                                   AS txn_count
          FROM stocktake_transactions t
          LEFT JOIN counted c
                 ON c.k_item  = UPPER(BTRIM(t.item_name))
                AND c.k_stock = COALESCE(t.stock_type, 'Fresh Stock')
         WHERE COALESCE(REPLACE(UPPER(BTRIM(t.warehouse)), '-', ''), '') = $1
           AND COALESCE(UPPER(BTRIM(t.location)), '')  = $2
           AND (c.count_day IS NULL OR {TXN_DAY} >= c.count_day)
         GROUP BY 1, 2
    )
    SELECT COALESCE(c.item_name, t.item_name)               AS item_name,
           COALESCE(c.item_type, t.item_type)               AS item_type,
           COALESCE(c.item_category, t.item_category)       AS item_category,
           COALESCE(c.item_subcategory, t.item_subcategory) AS item_subcategory,
           COALESCE(c.k_stock, t.k_stock)                   AS stock_type,
           c.unit_uom                                       AS unit_uom,
           COALESCE(c.q, 0)                                 AS counted_quantity,
           COALESCE(c.w, 0)                                 AS counted_weight,
           COALESCE(t.net_kg, 0)                            AS net_adjustment_kg,
           COALESCE(t.net_units, 0)                         AS net_adjustment_units,
           COALESCE(c.q, 0) + COALESCE(t.net_units, 0)      AS available_quantity,
           COALESCE(c.w, 0) + COALESCE(t.net_kg, 0)         AS available_kg,
           -- NULL = adjusted here but never counted here.
           c.count_day                                      AS last_counted_date,
           COALESCE(c.n, 0)                                 AS entry_count,
           COALESCE(t.txn_count, 0)                         AS txn_count
      FROM counted c
      FULL OUTER JOIN txn t
        ON c.k_item = t.k_item AND c.k_stock = t.k_stock
     ORDER BY available_kg DESC, item_name ASC, stock_type ASC
"""

_NUMERIC = (
    "counted_quantity", "counted_weight", "net_adjustment_kg", "net_adjustment_units",
    "available_quantity", "available_kg",
)


def _row(r: Any) -> dict[str, Any]:
    out = dict(r)
    for k in _NUMERIC:
        out[k] = float(out[k] or 0)
    # unit_uom is the floor app's pack weight (kg per unit: 0.050, 1.000; 0 = not
    # recorded), NOT a unit name — a number like every other figure.
    if out.get("unit_uom") is not None:
        out["unit_uom"] = float(out["unit_uom"])
    for k in ("entry_count", "txn_count"):
        out[k] = int(out[k] or 0)
    d = out.get("last_counted_date")
    # The one place the date becomes text, as everywhere else in this module.
    out["last_counted_date"] = d.isoformat() if d else None
    return out


async def fetch_floor_stock(conn: asyncpg.Connection, *, warehouse: str, floor: str) -> dict[str, Any]:
    """Every article recorded at `warehouse` + `floor`, netted, largest first.

    `warehouse` may be hyphenated ('W-202', as job cards spell it); `floor` is
    matched case- and padding-insensitively, like every stock-take floor filter.
    """
    wh = _normalise_warehouse(warehouse)
    fl = (floor or "").strip()
    try:
        rows = await conn.fetch(_FLOOR_STOCK_SQL, wh, _norm(fl))
    except _MISSING_SCHEMA as exc:
        log.warning("stock_take: floor stock unavailable — returning empty (%s: %s)",
                    type(exc).__name__, exc)
        return {"warehouse": wh, "floor": fl, "items": []}
    return {"warehouse": wh, "floor": fl, "items": [_row(r) for r in rows]}
