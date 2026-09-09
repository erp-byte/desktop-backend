"""Inward leaf feed for the Inventory Ledger.

Unions the two legacy inward channels per entity:

    inward     -> {p}_transactions_v2        + {p}_articles_v2
    bulk_entry -> {p}_bulk_entry_transactions + {p}_bulk_entry_articles

Quantity comes off the ARTICLE union, not by joining boxes. A correct box join
exists ((transaction_no, _source, article_description)), but articles give one
uniform rule across both channels. Note the consequence: the ledger's bulk figure
will NOT match scripts/generate_inventory_report.py, which joins bulk boxes on
transaction_no alone and therefore multiplies weight by article count. That
divergence is the report's defect, not this one.

Godown canonicalisation happens in Python rather than SQL: the alias map stays in
one testable place, and rows whose raw warehouses collapse to the same canonical
godown are merged here.

Read-only. Nothing in this module writes.
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from .godown_alias import AMBIGUOUS_ALIASES, ledger_godown, normalise

log = logging.getLogger(__name__)

# Hardcoded whitelist — request input never reaches SQL interpolation.
ENTITIES: tuple[str, ...] = ("cfpl", "cdpl")

_PM = "pm"

# Placeholder for a NULL/blank item_category or sub_category. The frontend types
# these as non-nullable `string` and slugifies them (_tree.ts slug()), so a None
# here is a client-side TypeError, not a blank cell.
UNCATEGORISED = "Uncategorised"

# The legacy inward tables predate schema-verified columns, so a missing table or
# a missing column is a plausible per-entity failure rather than a bug.
_MISSING_SCHEMA = (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError)

# Only the columns the ledger actually consumes. Explicit casts so the UNION
# survives the two families storing the same field with different types.
_ART_COLS = (
    "transaction_no::text     AS transaction_no, "
    "sku_id::bigint           AS sku_id, "
    "item_description::text   AS item_description, "
    "item_category::text      AS item_category, "
    "sub_category::text       AS sub_category, "
    "material_type::text      AS material_type, "
    "net_weight::numeric      AS net_weight, "
    "quantity_units::numeric  AS quantity_units, "
    "total_amount::numeric    AS total_amount"
)

# entry_date is `timestamp` on _transactions_v2 and `date` on
# _bulk_entry_transactions, so it is cast on both sides — a UNION of the two raw
# types fails. It is always midnight (measured: 0 of 1,647 rows carry a time), so
# the cast loses nothing.
_TX_COLS = (
    "transaction_no::text AS transaction_no, "
    "warehouse::text      AS warehouse, "
    "entry_date::date     AS entry_date, "
    "created_at           AS created_at"
)

# THE SHIFT CUTOFF IS IST, AND created_at IS A NAIVE COLUMN HOLDING UTC.
#
# Same trap as stocktake_entries — see stock_take/services/business_day.py, which
# documents the two-step conversion and why the one-step form means the opposite.
# Established for THIS table from its own hour histogram: created_at runs
# 04:00-18:00 as stored, peaking at 06:00. Read as UTC that is 09:30-23:30 IST
# peaking 11:30, a warehouse day. Read as IST it would claim the bulk of goods
# receipt is keyed in before 6am.
#
# It matters enormously here: a 14:00 cutoff on the raw column puts 1,570 of
# 1,599 rows in the morning; converted, 584. The evening bucket is wrong by 35x.
def ist_hour(col: str) -> str:
    """Hour-of-day in IST for a naive column holding UTC."""
    return f"EXTRACT(HOUR FROM (({col} AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata'))"


#: Where the working day is cut. 14:00 IST, matching the Stock Take app's split.
SHIFT_CUTOFF_HOUR = 14

#: Shift predicates against the joined transaction, keyed by the API's `shift`.
SHIFTS = {
    "all": "",
    "am": f"{ist_hour('t.created_at')} < {SHIFT_CUTOFF_HOUR}",
    "pm": f"{ist_hour('t.created_at')} >= {SHIFT_CUTOFF_HOUR}",
}


def build_leaves_sql(prefix: str, *, window: str = "",
                    shift: str = "all") -> str:
    """Union SQL for one entity prefix. Aggregates by RAW warehouse; the caller
    canonicalises and merges.

    The rtv/service predicate sits inside the v2 branch on purpose — those columns
    do not exist on {p}_bulk_entry_transactions, so referencing them after the
    UNION fails with `column "rtv" does not exist`.

    `window` selects the date predicate, applied to the joined transaction BEFORE
    the GROUP BY — the leaf it produces is then the aggregate for that window
    rather than for all time:
        ""      no date predicate
        "range" $1..$2 inclusive
        "days"  $1 as a date[], for a non-contiguous selection
    `shift` narrows further on the IST hour of created_at. Every fragment is a
    literal from this module (SHIFTS), never request text.
    """
    if window not in ("", "range", "days"):
        raise ValueError(f"unknown window mode: {window!r}")
    if prefix not in ENTITIES:
        raise ValueError(f"unknown entity prefix: {prefix!r}")
    if shift not in SHIFTS:
        raise ValueError(f"unknown shift: {shift!r}")

    where = []
    if window == "range":
        where.append("t.entry_date BETWEEN $1::date AND $2::date")
    elif window == "days":
        where.append("t.entry_date = ANY($1::date[])")
    if SHIFTS[shift]:
        where.append(SHIFTS[shift])
    predicate = ("WHERE " + " AND ".join(where)) if where else ""

    return f"""
        WITH all_tx AS (
            SELECT {_TX_COLS}, 'inward'::text AS _source
              FROM {prefix}_transactions_v2
             WHERE rtv IS NOT TRUE
               AND service IS NOT TRUE
            UNION ALL
            SELECT {_TX_COLS}, 'bulk_entry'::text AS _source
              FROM {prefix}_bulk_entry_transactions
        ),
        all_art AS (
            SELECT {_ART_COLS}, 'inward'::text AS _source
              FROM {prefix}_articles_v2
            UNION ALL
            SELECT {_ART_COLS}, 'bulk_entry'::text AS _source
              FROM {prefix}_bulk_entry_articles
        )
        SELECT a.sku_id                          AS sku_id,
               a.item_description                AS item_description,
               a.item_category                   AS item_category,
               a.sub_category                    AS sub_category,
               lower(trim(a.material_type))      AS material_type,
               t.warehouse                       AS warehouse_raw,
               COALESCE(SUM(a.net_weight), 0)    AS net_weight_kg,
               COALESCE(SUM(a.quantity_units), 0) AS qty_units,
               COALESCE(SUM(a.total_amount), 0)  AS value_indicative
          FROM all_art a
          JOIN all_tx  t
            ON t.transaction_no = a.transaction_no
           AND t._source        = a._source
         {predicate}
         GROUP BY a.sku_id, a.item_description, a.item_category,
                  a.sub_category, lower(trim(a.material_type)), t.warehouse
         ORDER BY a.sku_id, a.item_category, a.sub_category,
                  lower(trim(a.material_type)), t.warehouse, a.item_description
    """


def _text(value: Any) -> str:
    """Never return None. Every field below is typed `string` on the client."""
    return "" if value is None else str(value).strip()


def _category(value: Any) -> str:
    return _text(value) or UNCATEGORISED


def _item_type(value: Any) -> str:
    return _text(value).lower()


def _label(value: Any, sku_id: Any) -> str:
    """A blank label would render an unclickable empty row; identify it instead."""
    return _text(value) or f"(unnamed SKU {sku_id})"


def _fold(value: str) -> str:
    """Case-folded form, for IDENTITY only — never for what is displayed.

    The legacy tables do not spell a category the same way twice: "Packaging"
    and "packaging", "PISTA" and "pista", 21 such pairs across the live feed.
    Two rows differing only in that are one leaf, and merging them here is what
    makes their quantities add up instead of appearing as two identical rows.
    """
    return value.upper()


def _leaf_key(r: dict[str, Any], godown: str, entity: str) -> tuple:
    """Merge key. Must carry every field _to_leaf() emits as an identity column —
    category included, or two rows differing only in category silently collapse
    into one leaf that keeps whichever the (unordered) scan returned first.

    Normalisation must match _to_leaf() exactly: a NULL material_type and an
    empty-string one are the same leaf, so they must produce the same key.

    The categories are additionally CASE-FOLDED. _to_leaf keeps the original
    spelling for display — the fold decides only what counts as the same leaf.
    Without it sku 3837 in A68 emitted two rows, "Packaging" and "packaging",
    splitting one item's inward quantity in half. _item_type already lowercases,
    which is the same rule; this extends it to the two category columns.

    The label is deliberately NOT folded. No live pair differs only by the case
    of its description, and folding it would merge items on a guess rather than
    on evidence.
    """
    return (entity, r.get("sku_id"), _label(r.get("item_description"), r.get("sku_id")),
            _item_type(r.get("material_type")),
            _fold(_category(r.get("item_category"))),
            _fold(_category(r.get("sub_category"))),
            godown)


def _to_leaf(r: dict[str, Any], godown: str, entity: str) -> dict[str, Any]:
    material_type = _item_type(r.get("material_type"))
    is_pm = material_type == _PM
    qty = r.get("qty_units") if is_pm else r.get("net_weight_kg")
    return {
        "sku_id": r.get("sku_id"),
        "label": _label(r.get("item_description"), r.get("sku_id")),
        "item_type": material_type,
        "group": _category(r.get("item_category")),
        "subgroup": _category(r.get("sub_category")),
        "uom_class": "nos" if is_pm else "kg",
        "godown": godown,
        "entity": entity,
        "value_indicative": float(r.get("value_indicative") or 0),
        "inward_qty": float(qty or 0),
        # Not sourced in this pass. Closing is therefore NOT a stock figure —
        # the module renders an "Inward only" chip to say so.
        "opening_qty": 0,
        "production_qty": 0,
        "returns_qty": 0,
        "consumption_qty": 0,
        "outward_qty": 0,
        "transfer_out_qty": 0,
    }


async def fetch_leaves(conn, entity: str = "both", *,
                       date_from: Any = None, date_to: Any = None,
                       days: Any = None,
                       shift: str = "all") -> list[dict[str, Any]]:
    """Leaf rows for one entity or both, godowns canonicalised and merged.

    Each entity is fetched independently: a missing legacy table or column for
    one entity degrades that entity to zero rows and is logged, rather than
    discarding rows already collected for the other.

    date_from/date_to bound the entry_date (inclusive, both or neither). `days`
    is an exact set instead, for a non-contiguous selection; the two are mutually
    exclusive. With neither, the leaf is the all-time total — what every caller
    got before the window existed.
    """
    if (date_from is None) != (date_to is None):
        raise ValueError("date_from and date_to must be given together")
    if date_from is not None and date_from > date_to:
        raise ValueError(f"date_from {date_from} is after date_to {date_to}")
    if days is not None and date_from is not None:
        raise ValueError("pass either a range or an explicit day set, not both")
    if days is not None and not days:
        # An empty selection is not "everything" — that inversion would show the
        # whole warehouse to someone who deselected their last day.
        raise ValueError("days was given but empty")
    if shift not in SHIFTS:
        raise ValueError(f"unknown shift: {shift!r}")
    if entity == "both":
        prefixes = ENTITIES
    elif entity in ENTITIES:
        prefixes = (entity,)
    else:
        raise ValueError(f"unknown entity: {entity!r}")

    merged: dict[tuple, dict[str, Any]] = {}
    ambiguous_rows = 0
    skipped: list[str] = []

    for prefix in prefixes:
        mode = "range" if date_from is not None else ("days" if days is not None else "")
        sql = build_leaves_sql(prefix, window=mode, shift=shift)
        args = ((date_from, date_to) if mode == "range"
                else (list(days),) if mode == "days" else ())
        try:
            rows = await conn.fetch(sql, *args)
        except _MISSING_SCHEMA as exc:
            skipped.append(prefix)
            log.warning(
                "ledger: skipped entity %r — legacy inward schema is absent in "
                "this environment (%s: %s)", prefix, type(exc).__name__, exc,
            )
            continue
        for raw in rows:
            r = dict(raw)
            raw_warehouse = r.get("warehouse_raw")
            if normalise(raw_warehouse) in AMBIGUOUS_ALIASES:
                ambiguous_rows += 1
            godown = ledger_godown(raw_warehouse)
            key = _leaf_key(r, godown, prefix)
            leaf = _to_leaf(r, godown, prefix)
            if key in merged:
                merged[key]["inward_qty"] += leaf["inward_qty"]
                merged[key]["value_indicative"] += leaf["value_indicative"]
                # The key is case-folded, so the rows being merged here may spell
                # their category differently. Which spelling survives must not
                # depend on the order an unordered scan returned them in, or the
                # label flips between reloads: keep the smallest, which is
                # order-independent. Display is not decided here anyway — the UI
                # folds these names and shows the commonest spelling per group.
                for col in ("group", "subgroup"):
                    if leaf[col] < merged[key][col]:
                        merged[key][col] = leaf[col]
            else:
                merged[key] = leaf

    if ambiguous_rows:
        log.warning(
            "ledger: %d inward row(s) resolved through an ambiguous godown alias "
            "(%s) — mapping is inherited, not confirmed",
            ambiguous_rows, ", ".join(sorted(AMBIGUOUS_ALIASES)),
        )
    if skipped:
        log.warning(
            "ledger: returning %d leaf row(s) without entity %s — its legacy "
            "inward tables could not be read",
            len(merged), "/".join(skipped),
        )

    return list(merged.values())


def build_activity_sql(prefix: str) -> str:
    """Per-day inward document counts for one entity, split by shift.

    Counts TRANSACTIONS (inward documents), not leaves: a leaf is an aggregate
    over a window, so counting leaves per day would mean running the whole
    aggregation 172 times. The dots and the shift counts only need to say
    "something was received that day, and roughly when it was keyed in".

    Same rtv/service exclusion as build_leaves_sql, and for the same reason it
    sits inside the v2 branch: those columns do not exist on the bulk table.
    """
    if prefix not in ENTITIES:
        raise ValueError(f"unknown entity prefix: {prefix!r}")
    hour = ist_hour("created_at")
    return f"""
        WITH all_tx AS (
            SELECT entry_date::date AS d, created_at
              FROM {prefix}_transactions_v2
             WHERE rtv IS NOT TRUE AND service IS NOT TRUE
            UNION ALL
            SELECT entry_date::date AS d, created_at
              FROM {prefix}_bulk_entry_transactions
        )
        SELECT d,
               COUNT(*)::int AS docs,
               COUNT(*) FILTER (WHERE {hour} <  {SHIFT_CUTOFF_HOUR})::int AS am,
               COUNT(*) FILTER (WHERE {hour} >= {SHIFT_CUTOFF_HOUR})::int AS pm
          FROM all_tx
         WHERE d IS NOT NULL
         GROUP BY d
         ORDER BY d
    """


async def fetch_activity(conn, entity: str = "both") -> dict[str, Any]:
    """Which days have inward rows, and how each day splits by shift.

    Degrades the same way fetch_leaves does — a missing legacy table for one
    entity yields no rows for that entity rather than failing the whole call.
    """
    if entity == "both":
        prefixes = ENTITIES
    elif entity in ENTITIES:
        prefixes = (entity,)
    else:
        raise ValueError(f"unknown entity: {entity!r}")

    merged: dict[Any, dict[str, int]] = {}
    for prefix in prefixes:
        try:
            rows = await conn.fetch(build_activity_sql(prefix))
        except _MISSING_SCHEMA as exc:
            log.warning(
                "ledger: skipped activity for entity %r — legacy inward schema is "
                "absent in this environment (%s: %s)", prefix, type(exc).__name__, exc,
            )
            continue
        for r in rows:
            acc = merged.setdefault(r["d"], {"docs": 0, "am": 0, "pm": 0})
            acc["docs"] += r["docs"]
            acc["am"] += r["am"]
            acc["pm"] += r["pm"]

    days = [{"date": d.isoformat(), **counts} for d, counts in sorted(merged.items())]
    return {
        "days": days,
        # The pill strip needs the span even when a month in the middle is empty,
        # so these come from the data rather than from the first/last pill.
        "min_date": days[0]["date"] if days else None,
        "max_date": days[-1]["date"] if days else None,
        "shift_cutoff_hour": SHIFT_CUTOFF_HOUR,
    }
