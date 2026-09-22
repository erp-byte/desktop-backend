"""Latest-date stock read over `new_stock_entries`.

The table name comes from business_day.ENTRIES_TABLE, which also owns the
matching IST day expression — the two cannot be chosen independently without
being silently wrong. See that module's header.

`new_stock_entries` holds the same rows under the same ids as the
`stocktake_entries` the SEPARATE Stock Take app (an Express/Lambda backend under
Stock_Take/backend_st) writes into this same AWS RDS `warehouse_db`, but with
timestamptz timestamps and canonicalised floor names. Nothing here writes to it
except the console's own adjustment rows; the counting flow stays where it is.

BECAUSE THE FLOOR APP STILL WRITES THE OLD TABLE, rows counted after the
2026-09-08 backfill do not appear here until they are copied across. That is a
deliberate, visible consequence of the switch, not a bug in this reader.

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

from .. import floors as _floors
from .business_day import ENTRIES_TABLE, ENTRY_DAY, TXN_DAY

log = logging.getLogger(__name__)

#: A row's warehouse as ONE code, whatever its spelling: 'D-39', 'd39 ' and
#: 'D39' are all D39. Both tables name the column `warehouse`, so the same text
#: serves the entries and the ledger.
#:
#: The hyphen is stripped on the COLUMN side, not only on the value. The cold
#: stores were loaded on 16 Sep 2026 as 'D-39' and 'D-514', while grants, the
#: ledger and every filter value use 'D39' -- so an exact match hid 387,773 kg of
#: Savla stock from "all warehouses" and from the D39 filter, with no error.
#: Costs the idx_nse_place_canon index on this predicate; the table is small
#: enough (under 2,000 rows) that a scan is not noticeable.
WAREHOUSE_KEY = "REPLACE(UPPER(BTRIM(warehouse)), '-', '')"

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


#: Keeps ADJUSTMENT rows out of the COUNTED side of the aggregate -- they reach
#: the figure through the ledger, so counting them here too would double them.
#: Named because `verif` needs the same scope WITHOUT it: a sign-off applies to
#: the whole line, adjustment rows included. Carries no bind parameter, so
#: removing it from a copy of the condition list cannot shift $n numbering.
COUNT_ROWS_ONLY = "(source_kind IS NULL OR source_kind = 'COUNT')"


def _place_scope_sql(place_scope: Any, floor_col: str, first: int) -> tuple[str, list[Any]]:
    """A caller's floor grants as one predicate, place by place.

    `place_scope` is router._clamp_to_read_scope's {"whole": [codes],
    "pairs": ["W202|FIRST FLOOR", ...]}: a row is readable when its warehouse is
    seen whole, or its warehouse|floor is a granted pair. A floor granted in one
    warehouse therefore opens nothing in another, and a NULL floor matches no
    pair. `first` is the $n the first of the two parameters takes.
    """
    sql = (f"({WAREHOUSE_KEY} = ANY(${first}::text[])"
           f" OR {WAREHOUSE_KEY} || '|' || UPPER(BTRIM({floor_col})) = ANY(${first + 1}::text[]))")
    return sql, [list(place_scope.get("whole") or []), list(place_scope.get("pairs") or [])]


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
    place_scope: Optional[dict[str, list[str]]] = None,
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
    # A console adjustment now also writes a row into this same table
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
    conds.append(COUNT_ROWS_ONLY)

    # Both sides are trimmed and upper-cased before comparison. The canonical
    # table no longer carries the trailing spaces the floor app writes
    # ("UPPER BASEMENT "), but the rule stays: it is what the Express backend's
    # buildEntryFilters does, so the two agree on what a floor is, and it keeps a
    # filter value typed against either table selecting the same rows.
    whs = _as_list(warehouse)
    if whs:
        # One code per warehouse, so 'D39' selects the rows stored as 'D-39'.
        add(WAREHOUSE_KEY + " = ANY(${n}::text[])",
            [_floors.normalise_warehouse(v) for v in whs], "warehouse", whs)

    if place_scope is not None:
        # The caller's floor grants. Not echoed: it is their profile, not a
        # filter they chose.
        sql, values = _place_scope_sql(place_scope, "floor_name", len(params) + 1)
        params.extend(values)
        conds.append(sql)

    for value, tmpl, key in (
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
            # Hyphen-blind on both sides, like WAREHOUSE_KEY: "D-39" and "D39"
            # must find the same Savla counts and the same D39 postings.
            f" OR REPLACE(UPPER(COALESCE(warehouse, '')), '-', '') LIKE REPLACE(${n}, '-', '') ESCAPE '\\'"
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

    whs = _as_list(filters.get("warehouse"))
    if whs:
        # The ledger holds both 'W-202' and 'W202' (see TXN_GROUP_KEY), so the
        # column is normalised here too, not only the value.
        add(WAREHOUSE_KEY + " = ANY(${n}::text[])", [_floors.normalise_warehouse(v) for v in whs])

    if filters.get("place_scope") is not None:
        sql, values = _place_scope_sql(filters["place_scope"], "location",
                                       start_index + len(params) + 1)
        params.extend(values)
        conds.append(sql)

    for value, tmpl in (
        (_as_list(filters.get("floor_name")), "UPPER(BTRIM(location)) = ANY(${n}::text[])"),
        (_as_list(filters.get("item_type")), "UPPER(BTRIM(material_type)) = ANY(${n}::text[])"),
        (_as_list(filters.get("category")), "UPPER(BTRIM(item_category)) = ANY(${n}::text[])"),
        (_as_list(filters.get("subcategory")), "UPPER(BTRIM(item_subcategory)) = ANY(${n}::text[])"),
        (_as_list(filters.get("stock_type")), "UPPER(COALESCE(stock_type, 'Fresh Stock')) = ANY(${n}::text[])"),
    ):
        if value:
            add(tmpl, [v.upper().strip() for v in value])

    search = filters.get("search")
    if search:
        params.append(_like(str(search)))
        n = start_index + len(params)
        conds.append(
            "("
            f"UPPER(item_name) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(item_category, '')) LIKE ${n} ESCAPE '\\'"
            f" OR UPPER(COALESCE(item_subcategory, '')) LIKE ${n} ESCAPE '\\'"
            # Hyphen-blind on both sides, like WAREHOUSE_KEY: "D-39" and "D39"
            # must find the same Savla counts and the same D39 postings.
            f" OR REPLACE(UPPER(COALESCE(warehouse, '')), '-', '') LIKE REPLACE(${n}, '-', '') ESCAPE '\\'"
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
                   "stale_items": 0, "never_counted_items": 0,
                   "off_grade_weight": 0.0, "off_grade_items": 0},
        "pagination": {"page": page, "page_size": page_size, "total": 0, "total_pages": 0},
        "sort": sort,
        "filters": applied,
    }


def _stock_sql(*, as_of: Optional[str], adjusted_only: bool, by_place: bool,
               filters: dict[str, Any]) -> dict[str, Any]:
    """The statement behind both stock reads: the page and the stock download.

    Returns the CTE chain ending in `merged` plus its parameters, the filters as
    applied, and the lookup for the newest count day under those filters. Each
    caller adds its own SELECT over `merged`.

    `by_place=False` (the page): one line per article and stock type, summed over
    every warehouse and floor it sits at.
    `by_place=True` (the download): the same lines kept apart per warehouse and
    floor, carrying k_wh, k_fl and floor_label. The netting is per place in both
    modes (see "SINCE IS PER PLACE" below), so an article's per-floor lines add
    up to exactly the page's figure for it -- which is what lets the download's
    totals be checked against the screen.

    Raises ValueError for an unusable asOf.
    """
    conds, params, applied = _build_filters(**filters)
    # Declared here rather than in _build_filters because it is not a predicate
    # on a column: it asks whether the ledger half of the merge produced
    # anything, which only exists once both halves are joined.
    if adjusted_only:
        applied["adjustedOnly"] = True
    adj_only = "WHERE COALESCE(t.txn_count, 0) > 0" if adjusted_only else ""

    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    # Same filters, but adjustment rows included -- see COUNT_ROWS_ONLY.
    conds_all = [c for c in conds if c != COUNT_ROWS_ONLY]
    where_all = f"WHERE {' AND '.join(conds_all)}" if conds_all else ""

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
    date_clause_all = ""
    as_of_param = None
    # Staleness is measured against the day being VIEWED, so a back-dated page
    # does not report every row as months old against today.
    ref_day = "(now() AT TIME ZONE 'Asia/Kolkata')::date"
    if as_of_norm:
        date_params.append(as_of_norm)
        as_of_param = len(date_params)
        date_clause = f"AND {ENTRY_DAY} <= ${as_of_param}::date" if conds \
            else f"WHERE {ENTRY_DAY} <= ${as_of_param}::date"
        # Its own WHERE/AND: conds_all can be empty where conds is not.
        date_clause_all = f"AND {ENTRY_DAY} <= ${as_of_param}::date" if conds_all \
            else f"WHERE {ENTRY_DAY} <= ${as_of_param}::date"
        ref_day = f"${as_of_param}::date"
        applied["asOf"] = as_of_norm.isoformat()

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

    # Place keys, carried through `counted`, `txn` and `merged` for the download
    # only. The page sums an article over its places; the download keeps them.
    if by_place:
        keys = {
            "c_keys": "\n                        k_wh, k_fl, MIN(fl_name) AS floor_label,",
            "c_group": "1, 2, 3, 4",
            "t_keys": ("\n                        COALESCE(%s, '') AS k_wh,"
                       "\n                        COALESCE(UPPER(BTRIM(location)), '') AS k_fl,"
                       "\n                        MIN(BTRIM(location)) AS floor_label," % WAREHOUSE_KEY),
            "t_group": "1, 2, 3, 4",
            "m_keys": ("\n                     COALESCE(c.k_wh, t.k_wh) AS k_wh,"
                       "\n                     COALESCE(c.k_fl, t.k_fl) AS k_fl,"
                       "\n                     COALESCE(c.floor_label, t.floor_label) AS floor_label,"),
            "m_join": " AND c.k_wh = t.k_wh AND c.k_fl = t.k_fl",
        }
    else:
        keys = {"c_keys": "", "c_group": "1, 2", "t_keys": "", "t_group": "1, 2",
                "m_keys": "", "m_join": ""}

    # Both halves key on the SAME identity the rest of the system uses:
    # UPPER(BTRIM(item_name)) plus stock_type. That is a string join, not a key --
    # item_name is free text with no FK -- so an article renamed
    # between the count and the adjustment will not net. See 098's header.
    ctes = """
        WITH scoped AS (SELECT * FROM %(entries)s %(where)s %(daycap)s),
             -- The same rows PLUS the adjustment rows, for the sign-off only.
             -- Never used for a weight: that is what `scoped` is for.
             scoped_signed AS (SELECT * FROM %(entries)s %(where_all)s %(daycap_all)s),
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
                        -- The warehouse as one code: the cold stores'
                        -- 'D-39' rows and the ledger's 'D39' are one place.
                        COALESCE(%(wh_key)s, '') AS k_wh,
                        COALESCE(UPPER(BTRIM(floor_name)), '') AS k_fl,
                        %(entry_day)s                          AS count_day,
                        MIN(BTRIM(floor_name))                 AS fl_name,
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
             -- Sign-off for the line as a whole. BOOL_AND, not MAX: one
             -- unverified row behind a figure means the figure is not signed.
             verif AS (
                 SELECT UPPER(BTRIM(item_name))                AS k_item,
                        COALESCE(stock_type, 'Fresh Stock')    AS k_stock,
                        BOOL_AND(COALESCE(verified, FALSE))    AS verified,
                        MAX(verified_by)                       AS verified_by,
                        MAX(verified_at)                       AS verified_at
                   FROM scoped_signed
                  GROUP BY 1, 2
             ),
             -- Minimal projection for the ledger join: deliberately carries no
             -- bare item_name/warehouse, so _build_txn_filters' unqualified
             -- column names cannot become ambiguous against it.
             place_base AS (
                 SELECT k_item, k_stock, k_wh, k_fl, count_day FROM place_latest
             ),
             counted AS (
                 SELECT k_item, k_stock,%(c_keys)s
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
                  GROUP BY %(c_group)s
             ),
             txn AS (
                 SELECT UPPER(BTRIM(item_name))                       AS k_item,
                        COALESCE(stock_type, 'Fresh Stock')           AS k_stock,%(t_keys)s
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
                         AND b.k_wh    = COALESCE(%(wh_key)s, '')
                         AND b.k_fl    = COALESCE(UPPER(BTRIM(location)), '')
                   %(txnwhere)s
                  GROUP BY %(t_group)s
             ),
             merged AS (
                 SELECT
                     COALESCE(c.k_item, t.k_item)                     AS k_item,
                     COALESCE(c.k_stock, t.k_stock)                   AS k_stock,%(m_keys)s
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
                     (%(refday)s - c.last_counted_date)::int           AS days_since_count,
                     -- FALSE, not NULL, for a ledger-only article: it has rows
                     -- nobody has signed, which is "not verified", not "unknown".
                     COALESCE(v.verified, FALSE)                      AS verified,
                     v.verified_by                                    AS verified_by,
                     v.verified_at                                    AS verified_at
                   FROM counted c
                   FULL OUTER JOIN txn t
                     ON c.k_item = t.k_item AND c.k_stock = t.k_stock%(m_join)s
                   LEFT JOIN verif v
                     ON v.k_item  = COALESCE(c.k_item, t.k_item)
                    AND v.k_stock = COALESCE(c.k_stock, t.k_stock)
                  %(adjonly)s
             )
    """ % {"where": where, "daycap": date_clause, "txnwhere": txn_where,
           "where_all": where_all, "daycap_all": date_clause_all,
           "adjonly": adj_only, "wh_key": WAREHOUSE_KEY,
           "entries": ENTRIES_TABLE, "entry_day": ENTRY_DAY, "refday": ref_day,
           **keys}

    return {
        "ctes": ctes,
        "params": all_params,
        "applied": applied,
        # MAX(...) as a real `date`, not to_char'd text: it is bound straight back
        # into the caller's queries, and asyncpg types a `$n::date` parameter
        # from the statement. It is formatted to YYYY-MM-DD once, at the response
        # boundary — a date has no timezone, so no conversion can shift the day.
        "day_sql": f"SELECT MAX({ENTRY_DAY}) FROM {ENTRIES_TABLE} {where} {date_clause}",
        "day_params": date_params,
    }


async def fetch_latest_stock(
    conn: asyncpg.Connection,
    *,
    as_of: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    sort_by: str = DEFAULT_SORT,
    sort_order: str = "desc",
    adjusted_only: bool = False,
    **filters: Any,
) -> dict[str, Any]:
    """Stock as counted on the most recent count date, plus that date.

    Rows are aggregated per item AND per stock type — Fresh Stock and
    Off Grade/Rejection are different stock and must never be summed together —
    across warehouses and floors, so `warehouse`/`floor_name` narrow the set
    rather than splitting it. Item keying is UPPER(TRIM(item_name)), matching the
    Express app's own grouped view so both agree on what one item is.
    """
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

    q = _stock_sql(as_of=as_of, adjusted_only=adjusted_only, by_place=False, filters=filters)
    applied = q["applied"]
    try:
        as_of_date = await conn.fetchval(q["day_sql"], *q["day_params"])
    except _MISSING_SCHEMA as exc:
        # The entries table is not part of this app's schema. Pointed at a
        # database without it (the Supabase config carries no stocktake tables),
        # an empty result is the honest answer and the console renders its empty
        # state; a 500 would just be noise.
        log.warning(
            "stock_take: %s unavailable — returning empty (%s: %s)", ENTRIES_TABLE,
            type(exc).__name__, exc,
        )
        return _empty(page, page_size, applied, sort)

    # NO SHORT CUT ON as_of_date. It is MAX(day) over `scoped`, and `scoped`
    # carries COUNT_ROWS_ONLY -- so it is NULL for a place holding adjustments
    # but no physical count, and returning empty here answered for the ledger
    # without asking it. That is what showed "No counted stock at this location
    # yet." above 33,857.62 kg on A185 / A185 Cold, every kilo of it posted,
    # attributed and correctly written back.
    #
    # The query below already handles the case: `counted` comes out empty, `txn`
    # does not, and the FULL OUTER JOIN between them yields the ledger's rows.
    # With nothing on either side it returns an empty page unaided, so the only
    # thing lost is one saved round trip on a query whose CTEs are then empty.
    # _empty() is still reached when the table itself is absent, above.

    ctes, all_params = q["ctes"], q["params"]


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
               COUNT(*) FILTER (WHERE last_counted_date IS NULL)::bigint AS never_counted_items,
               -- Off grade broken out of the same aggregate rather than queried
               -- separately, so it can never disagree with total_weight. kg
               -- throughout, so the module's "never sum quantities across uom
               -- classes" rule does not bite here.
               COALESCE(SUM(total_weight) FILTER (
                   WHERE k_stock = 'Off Grade/Rejection'), 0)         AS off_grade_weight,
               COUNT(*) FILTER (WHERE k_stock = 'Off Grade/Rejection')::bigint
                                                                      AS off_grade_items
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
               last_counted_date, days_since_count, k_stock,
               verified, verified_by, verified_at
          FROM merged
        """ + order_by_sql + """
        LIMIT $%d OFFSET $%d
        """ % (len(all_params) + 1, len(all_params) + 2),
        *all_params, page_size, (page - 1) * page_size,
    )

    return {
        # The one place the date becomes text, so every caller sees YYYY-MM-DD.
        # NULL when this place has never been counted -- its figures then come
        # from the ledger alone. Callers already type it `string | null`.
        "as_of_date": as_of_date.isoformat() if as_of_date else None,
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
                # Sign-off for the whole line. Without this the row renders
                # identically before and after a verify, which makes a working
                # button look broken.
                "verified": bool(r["verified"]),
                "verified_by": r["verified_by"],
                "verified_at": (r["verified_at"].isoformat()
                                if r["verified_at"] else None),
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
            # Off grade is a separate LINE for the same article (233 articles
            # exist as both), so it is already inside total_weight. Surfacing it
            # answers "how much of this is rejection stock" without a second read.
            "off_grade_weight": float(totals["off_grade_weight"] or 0),
            "off_grade_items": int(totals["off_grade_items"] or 0),
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


async def fetch_stock_by_place(
    conn: asyncpg.Connection,
    *,
    as_of: Optional[str] = None,
    adjusted_only: bool = False,
    **filters: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Current stock per warehouse + floor + article + stock type, for the download.

    The same figures as fetch_latest_stock, under the same filters, kept apart per
    place instead of summed over them -- so every warehouse (the cold stores
    included) and every floor comes out, including a floor whose stock exists only
    as adjustments. Unpaginated: a download that stops at a page is worse than a
    slow one, because nobody can tell it is partial.

    Rows are ordered warehouse, floor, then heaviest first. Returns the rows and
    the filters as applied. Raises ValueError for an unusable asOf.
    """
    q = _stock_sql(as_of=as_of, adjusted_only=adjusted_only, by_place=True, filters=filters)
    try:
        rows = await conn.fetch(
            q["ctes"] + """
            SELECT k_wh, k_fl, floor_label, k_item, item_name, item_type, item_category,
                   item_subcategory, k_stock AS stock_type,
                   total_quantity, total_weight, counted_weight, net_adjustment_kg,
                   entry_count, txn_count, last_counted_date, days_since_count
              FROM merged
             ORDER BY k_wh, k_fl, total_weight DESC, item_name, k_stock
            """,
            *q["params"],
        )
    except _MISSING_SCHEMA as exc:
        log.warning("stock_take: %s unavailable — empty stock download (%s: %s)",
                    ENTRIES_TABLE, type(exc).__name__, exc)
        return [], q["applied"]

    return [
        {
            "warehouse": r["k_wh"] or "",
            # The item KEY, so a total line can count an item once however many
            # floors it sits on -- the way the page's Items figure counts it.
            "item_key": r["k_item"] or "",
            # The floor KEY is what identifies the place: one floor can come back
            # spelled "STORE" by its counts and "Store" by its adjustments.
            "floor_key": r["k_fl"] or "",
            "floor": r["floor_label"] or "",
            "item_name": r["item_name"],
            "item_type": r["item_type"],
            "item_category": r["item_category"],
            "item_subcategory": r["item_subcategory"],
            "stock_type": r["stock_type"],
            "total_quantity": float(r["total_quantity"] or 0),
            "counted_weight": float(r["counted_weight"] or 0),
            "net_adjustment_kg": float(r["net_adjustment_kg"] or 0),
            "total_weight": float(r["total_weight"] or 0),
            "entry_count": int(r["entry_count"] or 0),
            "transaction_count": int(r["txn_count"] or 0),
            "last_counted_date": r["last_counted_date"],
            "days_since_count": (int(r["days_since_count"])
                                 if r["days_since_count"] is not None else None),
        }
        for r in rows
    ], q["applied"]


async def fetch_filter_options(conn: asyncpg.Connection) -> dict[str, list[str]]:
    """Distinct values for the console's filter controls, built from live data.

    Scoped to non-draft rows so the dropdowns cannot offer a warehouse that only
    ever appears in someone's unsubmitted draft.
    """
    try:
        rows = await conn.fetch(
            # floor_name is returned AS SPELLED, not upper-cased. In this table it
            # is already canonical ("First Floor", not "1 ST FLOOR "), and that
            # spelling is the whole point of the dropdown. Matching is unaffected:
            # _build_filters compares UPPER(TRIM(floor_name)) against values this
            # module upper-cases in Python, so either casing selects the same rows.
            # The warehouse, by contrast, is the one normalised CODE (D39, not
            # D-39): the code the grants, the ledger and the filter all use.
            f"""
            SELECT DISTINCT
                {WAREHOUSE_KEY}                               AS warehouse,
                BTRIM(floor_name)                             AS floor_name,
                UPPER(TRIM(COALESCE(item_type, '')))          AS item_type,
                COALESCE(stock_type, 'Fresh Stock')           AS stock_type
            FROM {ENTRIES_TABLE}
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


async def fetch_places(conn: asyncpg.Connection) -> dict[str, list[str]]:
    """Which floors each warehouse actually holds stock on, {W202: [...], ...}.

    fetch_filter_options flattens floors across every warehouse, which is right
    for a filter bar — you may want "everything on a First Floor" — but wrong for
    a form that posts to one place: it offers A185's floors to someone who picked
    W202. This keeps them apart.

    Only consulted for warehouses that declare no floors of their own (F53, A68).
    A declared warehouse is offered what the ERP profile says it has, never what
    somebody once typed — see app/modules/stock_take/floors.py.

    Keyed by the normalised code, so the cold stores' 'D-39' rows land under the
    'D39' a grant names. Keyed as spelled, a caller granted D39 was offered no
    floors there and "all warehouses" left the Savla stock out entirely.
    """
    try:
        rows = await conn.fetch(
            f"""
            SELECT DISTINCT {WAREHOUSE_KEY} AS wh, BTRIM(floor_name) AS fl
              FROM {ENTRIES_TABLE}
             WHERE (status IS NULL OR status != 'draft')
               AND warehouse IS NOT NULL AND BTRIM(COALESCE(floor_name, '')) <> ''
             ORDER BY 1, 2
            """
        )
    except _MISSING_SCHEMA as exc:
        log.warning("stock_take: places unavailable — returning empty (%s: %s)",
                    type(exc).__name__, exc)
        return {}

    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["wh"], []).append(r["fl"])
    return out
