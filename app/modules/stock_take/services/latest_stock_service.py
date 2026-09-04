"""Latest-date stock read over `stocktake_entries`.

`stocktake_entries` is written by the SEPARATE Stock Take app (an Express/Lambda
backend under Stock_Take/backend_st) into the same AWS RDS `warehouse_db` this
service connects to. Nothing here writes: the console is a reader of that app's
data, and the counting flow stays where it is.

The table is NOT part of this app's schema management — production_schema.sql
does not declare it — so a missing table is a plausible environment state rather
than a bug, and is reported as an empty result. In particular the Supabase
schema this repo can be pointed at carries no stocktake tables at all.

"Latest" is resolved UNDER THE FILTERS, not globally: ?warehouse=CFPL answers
"what did CFPL's last count find", which is a different day whenever that
warehouse was skipped in the newest session. Resolving globally and then
filtering would return an empty page for exactly those cases.

Read-only. Nothing in this module writes.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Optional, Sequence

import asyncpg

from .business_day import ENTRY_DAY, TXN_DAY

log = logging.getLogger(__name__)

# A missing table/column is an environment state here, not a bug — see the module
# docstring. Same posture as modules/ledger/services/leaves_service.py.
_MISSING_SCHEMA = (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# camelCase API key -> output alias of the aggregate query. Hardcoded allowlist:
# request input never reaches SQL interpolation, and an unknown key falls back to
# the default rather than being passed through.
SORT_COLUMNS: dict[str, str] = {
    "itemName": "item_name",
    "itemType": "item_type",
    "category": "item_category",
    "subcategory": "item_subcategory",
    "stockType": "stock_type",
    "totalQuantity": "total_quantity",
    "totalWeight": "total_weight",
    "entryCount": "entry_count",
    # Staleness is a first-class sort now: the page mixes counts from today with
    # counts from eight months ago, so "which of these do I not trust" has to be
    # orderable.
    "lastCounted": "last_counted_date",
    "daysSinceCount": "days_since_count",
}
DEFAULT_SORT = "totalWeight"


def normalise_date(value: Optional[str]) -> Optional[date]:
    """`YYYY-MM-DD` (or the date half of an ISO timestamp) as a `date`, else None.

    Returns a real `date` rather than the string, because asyncpg infers the type
    of a `$n::date` parameter from the statement and rejects a str with
    "'str' object has no attribute 'toordinal'". Formatting back to text happens
    once, at the response boundary.
    """
    if not value:
        return None
    text = str(value).strip()
    if not _DATE_RE.match(text):
        text = text[:10]
        if not _DATE_RE.match(text):
            return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        # Shape matched but the day does not exist (e.g. 2026-02-31).
        return None


def _like(value: str) -> str:
    """Escape LIKE wildcards so a user typing "50%" searches for a literal "50%"."""
    return "%" + value.upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, Sequence):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _build_filters(
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
    include_drafts: bool = False,
) -> tuple[list[str], list[Any], dict[str, Any]]:
    """WHERE fragments + positional params + an echo of what was applied.

    Every value is bound as an asyncpg parameter; nothing is interpolated.
    """
    conds: list[str] = []
    params: list[Any] = []
    applied: dict[str, Any] = {"includeDrafts": include_drafts}

    def add(sql_tmpl: str, value: Any, key: str, echo: Any) -> None:
        params.append(value)
        conds.append(sql_tmpl.format(n=len(params)))
        applied[key] = echo

    if not include_drafts:
        conds.append("(status IS NULL OR status != 'draft')")

    # COUNTED STOCK MEANS PHYSICAL COUNTS ONLY — and this is unconditional, NOT
    # inside the include_drafts branch, because it is a correctness rule rather
    # than a user-facing filter.
    #
    # A console adjustment now also writes a row into stocktake_entries
    # (source_kind='ADJUSTMENT', see 101_stocktake_entries_adjustment_rows.sql).
    # Without this predicate two things break at once:
    #   1. The baseline is MAX(day) over this same WHERE, so an adjustment posted
    #      on a day nobody counted becomes the newest "count day" and the view
    #      collapses to that single article. Measured on live data: items 2 -> 1,
    #      weight 5.03 -> 42.50, with every counted article gone.
    #   2. The figure is counted + net(ledger); an adjustment present on BOTH
    #      sides would be added twice.
    # The ledger stays the single source of adjustments for this view. The
    # entries row exists for the Stock Take app and for anything reading that
    # table directly.
    conds.append("(source_kind IS NULL OR source_kind = 'COUNT')")

    # Floor names in this dataset carry trailing spaces ("UPPER BASEMENT "), so
    # both sides are trimmed before comparison — same rule the Express backend's
    # buildEntryFilters uses, so the two agree on what a floor is.
    for value, tmpl, key in (
        (_as_list(warehouse), "UPPER(TRIM(warehouse)) = ANY(${n}::text[])", "warehouse"),
        (_as_list(floor_name), "UPPER(TRIM(floor_name)) = ANY(${n}::text[])", "floorName"),
        (_as_list(item_type), "UPPER(TRIM(COALESCE(item_type, ''))) = ANY(${n}::text[])", "itemType"),
        (_as_list(category), "UPPER(TRIM(COALESCE(item_category, ''))) = ANY(${n}::text[])", "category"),
        (_as_list(subcategory), "UPPER(TRIM(COALESCE(item_subcategory, ''))) = ANY(${n}::text[])", "subcategory"),
        (_as_list(stock_type), "UPPER(COALESCE(stock_type, 'Fresh Stock')) = ANY(${n}::text[])", "stockType"),
    ):
        if value:
            add(tmpl, [v.upper().strip() for v in value], key, value)

    if entered_by:
        add("UPPER(COALESCE(entered_by, '')) LIKE ${n} ESCAPE '\\'", _like(entered_by), "enteredBy", entered_by)

    if search:
        params.append(_like(search))
        n = len(params)
        conds.append(
            "("
            f"UPPER(item_name) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(item_category, '')) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(item_subcategory, '')) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(warehouse, '')) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(floor_name, '')) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(entered_by, '')) LIKE ${n} ESCAPE '\\'"
            ")"
        )
        applied["search"] = search

    if verified is not None:
        conds.append("verified = true" if verified else "COALESCE(verified, false) = false")
        applied["verified"] = verified

    return conds, params, applied


def _build_txn_filters(
    start_index: int = 0, **filters: Any
) -> tuple[list[str], list[Any]]:
    """The subset of the entry filters that also apply to `stocktake_transactions`.

    Only filters with a real counterpart column are mirrored:

        warehouse    -> warehouse        item_type   -> material_type
        floor_name   -> location         category    -> item_category
        stock_type   -> stock_type       subcategory -> item_subcategory
        search       -> item name / category / sub-category

    Entry-only filters have NO counterpart and are deliberately not applied:
    entered_by, verified, include_drafts (a ledger row is never a draft), and the
    shift/hours filters. Applying them would silently drop adjustments; ignoring
    them means a narrow entry filter can still surface a broadly-scoped
    adjustment, which is the safer of the two errors for a stock figure.

    `start_index` is how many parameters the caller has already bound, so the
    $n placeholders continue that sequence.
    """
    conds: list[str] = []
    params: list[Any] = []

    def add(sql_tmpl: str, value: Any) -> None:
        params.append(value)
        conds.append(sql_tmpl.format(n=start_index + len(params)))

    for value, tmpl in (
        (_as_list(filters.get("warehouse")), "UPPER(BTRIM(warehouse)) = ANY(${n}::text[])"),
        (_as_list(filters.get("floor_name")), "UPPER(BTRIM(location)) = ANY(${n}::text[])"),
        (_as_list(filters.get("item_type")), "UPPER(BTRIM(material_type)) = ANY(${n}::text[])"),
        (_as_list(filters.get("category")), "UPPER(BTRIM(item_category)) = ANY(${n}::text[])"),
        (_as_list(filters.get("subcategory")), "UPPER(BTRIM(item_subcategory)) = ANY(${n}::text[])"),
        (_as_list(filters.get("stock_type")), "UPPER(COALESCE(stock_type, 'Fresh Stock')) = ANY(${n}::text[])"),
    ):
        if value:
            # Warehouse codes are stored unhyphenated on both sides; normalise so a
            # 'W-202' filter still matches ledger rows written as 'W202'.
            add(tmpl, [v.upper().strip().replace("-", "") if "warehouse" in tmpl else v.upper().strip()
                       for v in value])

    search = filters.get("search")
    if search:
        params.append(_like(str(search)))
        n = start_index + len(params)
        conds.append(
            "("
            f"UPPER(item_name) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(item_category, '')) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(item_subcategory, '')) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(warehouse, '')) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(location, '')) LIKE ${n} ESCAPE '\\'"
            ")"
        )
    return conds, params


def _empty(page: int, page_size: int, applied: dict[str, Any], sort: dict[str, str]) -> dict[str, Any]:
    return {
        "as_of_date": None,
        "items": [],
        "totals": {"items": 0, "entries": 0, "total_quantity": 0.0, "total_weight": 0.0,
                   "counted_weight": 0.0, "net_adjustment_kg": 0.0, "transactions": 0,
                   "oldest_counted_date": None, "newest_counted_date": None,
                   "stale_items": 0, "never_counted_items": 0},
        "pagination": {"page": page, "page_size": page_size, "total": 0, "total_pages": 0},
        "sort": sort,
        "filters": applied,
    }


async def fetch_latest_stock(
    conn: asyncpg.Connection,
    *,
    as_of: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    sort_by: str = DEFAULT_SORT,
    sort_order: str = "desc",
    **filters: Any,
) -> dict[str, Any]:
    """Stock as counted on the most recent count date, plus that date.

    Rows are aggregated per item AND per stock type — Fresh Stock and
    Off Grade/Rejection are different stock and must never be summed together —
    across warehouses and floors, so `warehouse`/`floor_name` narrow the set
    rather than splitting it. Item keying is UPPER(TRIM(item_name)), matching the
    Express app's own grouped view so both agree on what one item is.
    """
    conds, params, applied = _build_filters(**filters)

    sort_key = sort_by if sort_by in SORT_COLUMNS else DEFAULT_SORT
    direction = "ASC" if str(sort_order).lower() == "asc" else "DESC"
    sort = {"sort_by": sort_key, "sort_order": direction.lower()}
    # Both halves come from this module's own literals, so no request text
    # reaches the statement. NULLS LAST keeps never-counted articles (ledger-only
    # rows, last_counted_date NULL) off the head of a "most recently counted"
    # sort. k_stock joins the tie-break because one item_name legitimately
    # appears twice, once per stock type — item_name alone is not a total order,
    # and paging across a weight tie could then repeat or skip a row.
    order_by_sql = (f"ORDER BY {SORT_COLUMNS[sort_key]} {direction} NULLS LAST,"
                    " item_name ASC, k_stock ASC")

    where = f"WHERE {' AND '.join(conds)}" if conds else ""

    as_of_norm = normalise_date(as_of)
    if as_of and not as_of_norm:
        # Caller asked for a back-dated view and the value was unusable. Silently
        # ignoring it would hand back the NEWEST data instead, which is
        # indistinguishable from success — so this is rejected by the router.
        raise ValueError(f"Invalid asOf date {as_of!r}; expected YYYY-MM-DD")

    # asOf caps which counts are ELIGIBLE and is applied to `scoped`, so it caps
    # every article/place's own max rather than one global date: a floor counted
    # in January and again in July reports its JANUARY figure under asOf=June.
    date_params = list(params)
    date_clause = ""
    as_of_param = None
    # Staleness is measured against the day being VIEWED, so a back-dated page
    # does not report every row as months old against today.
    ref_day = "(now() AT TIME ZONE 'Asia/Kolkata')::date"
    if as_of_norm:
        date_params.append(as_of_norm)
        as_of_param = len(date_params)
        date_clause = f"AND {ENTRY_DAY} <= ${as_of_param}::date" if conds \
            else f"WHERE {ENTRY_DAY} <= ${as_of_param}::date"
        ref_day = f"${as_of_param}::date"
        applied["asOf"] = as_of_norm.isoformat()

    try:
        # MAX(...) as a real `date`, not to_char'd text: it is bound straight back
        # into the two queries below, and asyncpg types a `$n::date` parameter
        # from the statement. It is formatted to YYYY-MM-DD once, at the response
        # boundary — a date has no timezone, so no conversion can shift the day.
        as_of_date = await conn.fetchval(
            f"""
            SELECT MAX({ENTRY_DAY})
            FROM stocktake_entries
            {where}
            {date_clause}
            """,
            *date_params,
        )
    except _MISSING_SCHEMA as exc:
        # stocktake_entries belongs to the Stock Take app, not this one. Pointed
        # at a database without it (the Supabase config carries no stocktake
        # tables), an empty result is the honest answer and the console renders
        # its empty state; a 500 would just be noise.
        log.warning(
            "stock_take: stocktake_entries unavailable — returning empty (%s: %s)",
            type(exc).__name__, exc,
        )
        return _empty(page, page_size, applied, sort)

    # Nothing matched. That is an answer, not a failure.
    if not as_of_date:
        return _empty(page, page_size, applied, sort)

    # `scoped` carries the filters AND the asOf cap; there is no longer a single
    # day parameter, because there is no longer a single day.
    day_params = list(date_params)

    # -- Ledger side ---------------------------------------------------------
    # "SINCE" IS PER PLACE, not per article and not global. Each article/floor is
    # netted against adjustments posted on or after THAT floor's own count day.
    # 662 of 1422 articles have their floors counted on different days (worst
    # spread 216 days) and they hold 88% of all stock, so collapsing to one date
    # per article is wrong in both directions: MAX(baselines) silently drops
    # adjustments made at a floor counted earlier, and MIN(baselines) re-applies
    # adjustments that a later recount already absorbed.
    #
    # This is also the rule current_balance has always used
    # (transactions_service.current_balance), so the page and the overdraw
    # warning now agree by construction rather than by coincidence.
    txn_conds, txn_params = _build_txn_filters(start_index=len(day_params), **filters)
    # baseline_day IS NULL means "never counted at that place". Left unbounded on
    # purpose — it is what makes an adjusted-but-never-counted article appear.
    txn_conds.append("(b.count_day IS NULL OR " + TXN_DAY + " >= b.count_day)")
    if as_of_param:
        # A back-dated view must not fold in adjustments made after that date, or
        # it reports January's count against September's movements.
        txn_conds.append(TXN_DAY + " <= $%d::date" % as_of_param)
    txn_where = "WHERE " + " AND ".join(txn_conds)
    all_params = day_params + txn_params

    # Both halves key on the SAME identity the rest of the system uses:
    # UPPER(BTRIM(item_name)) plus stock_type. That is a string join, not a key --
    # stocktake_entries.item_name is free text with no FK -- so an article renamed
    # between the count and the adjustment will not net. See 098's header.
    ctes = """
        WITH scoped AS (SELECT * FROM stocktake_entries %(where)s %(daycap)s),
             -- Sum duplicates FIRST, then pick the latest day. Order matters: a
             -- naive DISTINCT ON over raw rows returns the right 3292
             -- article/place combinations but only 577,465 kg of 895,396 —
             -- roughly a third of all stock silently lost to the 1168 groups holding more
             -- than one count row for the same article/place/day.
             place_day AS (
                 SELECT UPPER(BTRIM(item_name))                AS k_item,
                        COALESCE(stock_type, 'Fresh Stock')    AS k_stock,
                        -- COALESCE, not a bare UPPER(BTRIM(...)): floor_name is
                        -- nullable (8 live rows) and NULL = NULL is false in the
                        -- join below, so an un-coalesced key drops those rows
                        -- and their articles entirely — no error, just a smaller
                        -- number.
                        COALESCE(UPPER(BTRIM(warehouse)), '')  AS k_wh,
                        COALESCE(UPPER(BTRIM(floor_name)), '') AS k_fl,
                        %(entry_day)s                          AS count_day,
                        MIN(item_name)                         AS item_name,
                        MIN(item_type)                         AS item_type,
                        MIN(item_category)                     AS item_category,
                        MIN(item_subcategory)                  AS item_subcategory,
                        COALESCE(SUM(total_quantity), 0)       AS q,
                        COALESCE(SUM(total_weight), 0)         AS w,
                        COUNT(*)::bigint                       AS n
                   FROM scoped
                  GROUP BY 1, 2, 3, 4, 5
             ),
             -- Each article/place carried forward from ITS OWN newest count.
             place_latest AS (
                 SELECT * FROM (
                     SELECT pd.*,
                            ROW_NUMBER() OVER (PARTITION BY k_item, k_stock, k_wh, k_fl
                                                   ORDER BY count_day DESC) AS rn
                       FROM place_day pd) ranked
                  WHERE rn = 1
             ),
             -- Minimal projection for the ledger join: deliberately carries no
             -- bare item_name/warehouse, so _build_txn_filters' unqualified
             -- column names cannot become ambiguous against it.
             place_base AS (
                 SELECT k_item, k_stock, k_wh, k_fl, count_day FROM place_latest
             ),
             counted AS (
                 SELECT k_item, k_stock,
                        MIN(item_name)                                AS item_name,
                        MIN(item_type)                                AS item_type,
                        MIN(item_category)                            AS item_category,
                        MIN(item_subcategory)                         AS item_subcategory,
                        COALESCE(SUM(q), 0)                           AS counted_quantity,
                        COALESCE(SUM(w), 0)                           AS counted_weight,
                        COALESCE(SUM(n), 0)::bigint                   AS entry_count,
                        COUNT(DISTINCT k_wh)::bigint                  AS warehouse_count,
                        COUNT(DISTINCT k_fl)::bigint                  AS floor_count,
                        MAX(count_day)                                AS last_counted_date,
                        MIN(count_day)                                AS oldest_counted_date
                   FROM place_latest
                  GROUP BY 1, 2
             ),
             txn AS (
                 SELECT UPPER(BTRIM(item_name))                       AS k_item,
                        COALESCE(stock_type, 'Fresh Stock')           AS k_stock,
                        MIN(item_name)                                AS item_name,
                        MIN(material_type)                            AS item_type,
                        MIN(item_category)                            AS item_category,
                        MIN(item_subcategory)                         AS item_subcategory,
                        COALESCE(SUM(CASE WHEN operation = 'ADDITION'
                                          THEN qty_kg ELSE -qty_kg END), 0)   AS net_kg,
                        COALESCE(SUM(CASE WHEN operation = 'ADDITION'
                                          THEN units ELSE -units END), 0)     AS net_units,
                        COUNT(*)::bigint                              AS txn_count
                   FROM stocktake_transactions
                   LEFT JOIN place_base b
                          ON b.k_item  = UPPER(BTRIM(item_name))
                         AND b.k_stock = COALESCE(stock_type, 'Fresh Stock')
                         AND b.k_wh    = COALESCE(UPPER(BTRIM(warehouse)), '')
                         AND b.k_fl    = COALESCE(UPPER(BTRIM(location)), '')
                   %(txnwhere)s
                  GROUP BY 1, 2
             ),
             merged AS (
                 SELECT
                     COALESCE(c.k_item, t.k_item)                     AS k_item,
                     COALESCE(c.k_stock, t.k_stock)                   AS k_stock,
                     COALESCE(c.item_name, t.item_name)               AS item_name,
                     COALESCE(c.item_type, t.item_type)               AS item_type,
                     COALESCE(c.item_category, t.item_category)       AS item_category,
                     COALESCE(c.item_subcategory, t.item_subcategory) AS item_subcategory,
                     COALESCE(c.counted_quantity, 0)                  AS counted_quantity,
                     COALESCE(c.counted_weight, 0)                    AS counted_weight,
                     COALESCE(t.net_kg, 0)                            AS net_adjustment_kg,
                     COALESCE(t.net_units, 0)                         AS net_adjustment_units,
                     COALESCE(c.counted_weight, 0) + COALESCE(t.net_kg, 0)      AS total_weight,
                     COALESCE(c.counted_quantity, 0) + COALESCE(t.net_units, 0) AS total_quantity,
                     COALESCE(c.entry_count, 0)                       AS entry_count,
                     COALESCE(t.txn_count, 0)                         AS txn_count,
                     COALESCE(c.warehouse_count, 0)                   AS warehouse_count,
                     COALESCE(c.floor_count, 0)                       AS floor_count,
                     c.last_counted_date                              AS last_counted_date,
                     c.oldest_counted_date                            AS oldest_counted_date,
                     -- NULL for a ledger-only article: "never counted" is not
                     -- the same as "counted zero days ago".
                     (%(refday)s - c.last_counted_date)::int           AS days_since_count
                   FROM counted c
                   FULL OUTER JOIN txn t
                     ON c.k_item = t.k_item AND c.k_stock = t.k_stock
             )
    """ % {"where": where, "daycap": date_clause, "txnwhere": txn_where,
           "entry_day": ENTRY_DAY, "refday": ref_day}

    totals = await conn.fetchrow(
        ctes + """
        SELECT COUNT(*)::bigint                        AS items,
               COALESCE(SUM(entry_count), 0)::bigint   AS entries,
               COALESCE(SUM(txn_count), 0)::bigint     AS transactions,
               COALESCE(SUM(counted_weight), 0)        AS counted_weight,
               COALESCE(SUM(net_adjustment_kg), 0)     AS net_adjustment_kg,
               COALESCE(SUM(total_weight), 0)          AS total_weight,
               COALESCE(SUM(total_quantity), 0)        AS total_quantity,
               MIN(last_counted_date)                  AS oldest_counted_date,
               MAX(last_counted_date)                  AS newest_counted_date,
               COUNT(*) FILTER (WHERE days_since_count > 30)::bigint  AS stale_items,
               COUNT(*) FILTER (WHERE last_counted_date IS NULL)::bigint AS never_counted_items
          FROM merged
        """,
        *all_params,
    )
    # Group count, so pagination reports pages of ITEMS rather than of raw rows.
    total_items = int(totals["items"] or 0)

    rows = await conn.fetch(
        ctes + """
        SELECT item_name, item_type, item_category, item_subcategory,
               k_stock AS stock_type,
               total_quantity, total_weight,
               counted_weight, net_adjustment_kg, net_adjustment_units,
               entry_count, txn_count, warehouse_count, floor_count,
               last_counted_date, days_since_count, k_stock
          FROM merged
        """ + order_by_sql + """
        LIMIT $%d OFFSET $%d
        """ % (len(all_params) + 1, len(all_params) + 2),
        *all_params, page_size, (page - 1) * page_size,
    )

    return {
        # The one place the date becomes text, so every caller sees YYYY-MM-DD.
        "as_of_date": as_of_date.isoformat(),
        "items": [
            {
                "item_name": r["item_name"],
                "item_type": r["item_type"],
                "item_category": r["item_category"],
                "item_subcategory": r["item_subcategory"],
                "stock_type": r["stock_type"],
                # total_* are the NETTED figures: counted plus adjustments since.
                "total_quantity": float(r["total_quantity"] or 0),
                "total_weight": float(r["total_weight"] or 0),
                # Both halves are kept so a reader can always see what was counted
                # versus what has moved, not only the derived number.
                "counted_weight": float(r["counted_weight"] or 0),
                "net_adjustment_kg": float(r["net_adjustment_kg"] or 0),
                "net_adjustment_units": float(r["net_adjustment_units"] or 0),
                "entry_count": int(r["entry_count"] or 0),
                "transaction_count": int(r["txn_count"] or 0),
                "warehouse_count": int(r["warehouse_count"] or 0),
                "floor_count": int(r["floor_count"] or 0),
                # The date THIS article was last physically counted. Rows on one
                # page now come from many different days, so the figure is not
                # interpretable without it.
                "last_counted_date": (r["last_counted_date"].isoformat()
                                      if r["last_counted_date"] else None),
                "days_since_count": (int(r["days_since_count"])
                                     if r["days_since_count"] is not None else None),
            }
            for r in rows
        ],
        "totals": {
            "items": total_items,
            "entries": int(totals["entries"] or 0),
            "transactions": int(totals["transactions"] or 0),
            "total_quantity": float(totals["total_quantity"] or 0),
            "total_weight": float(totals["total_weight"] or 0),
            "counted_weight": float(totals["counted_weight"] or 0),
            "net_adjustment_kg": float(totals["net_adjustment_kg"] or 0),
            # The page now spans many count dates, so the span itself is part of
            # the answer: without it, one total silently mixes a count from today
            # with one from eight months ago and looks equally authoritative.
            "oldest_counted_date": (totals["oldest_counted_date"].isoformat()
                                    if totals["oldest_counted_date"] else None),
            "newest_counted_date": (totals["newest_counted_date"].isoformat()
                                    if totals["newest_counted_date"] else None),
            "stale_items": int(totals["stale_items"] or 0),
            "never_counted_items": int(totals["never_counted_items"] or 0),
        },
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total_items,
            "total_pages": (total_items + page_size - 1) // page_size if total_items else 0,
        },
        "sort": sort,
        "filters": applied,
    }


async def fetch_filter_options(conn: asyncpg.Connection) -> dict[str, list[str]]:
    """Distinct values for the console's filter controls, built from live data.

    Scoped to non-draft rows so the dropdowns cannot offer a warehouse that only
    ever appears in someone's unsubmitted draft.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT
                UPPER(TRIM(warehouse))                        AS warehouse,
                UPPER(TRIM(floor_name))                       AS floor_name,
                UPPER(TRIM(COALESCE(item_type, '')))          AS item_type,
                COALESCE(stock_type, 'Fresh Stock')           AS stock_type
            FROM stocktake_entries
            WHERE (status IS NULL OR status != 'draft')
            """
        )
    except _MISSING_SCHEMA as exc:
        log.warning(
            "stock_take: filter options unavailable — returning empty (%s: %s)",
            type(exc).__name__, exc,
        )
        return {"warehouses": [], "floors": [], "item_types": [], "stock_types": []}

    def uniq(key: str) -> list[str]:
        return sorted({r[key] for r in rows if r[key]})

    return {
        "warehouses": uniq("warehouse"),
        "floors": uniq("floor_name"),
        "item_types": uniq("item_type"),
        "stock_types": uniq("stock_type"),
    }
