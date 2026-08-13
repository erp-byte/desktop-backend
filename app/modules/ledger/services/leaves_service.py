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

_TX_COLS = (
    "transaction_no::text AS transaction_no, "
    "warehouse::text      AS warehouse"
)


def build_leaves_sql(prefix: str) -> str:
    """Union SQL for one entity prefix. Aggregates by RAW warehouse; the caller
    canonicalises and merges.

    The rtv/service predicate sits inside the v2 branch on purpose — those columns
    do not exist on {p}_bulk_entry_transactions, so referencing them after the
    UNION fails with `column "rtv" does not exist`.
    """
    if prefix not in ENTITIES:
        raise ValueError(f"unknown entity prefix: {prefix!r}")

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


def _leaf_key(r: dict[str, Any], godown: str, entity: str) -> tuple:
    """Merge key. Must carry every field _to_leaf() emits as an identity column —
    category included, or two rows differing only in category silently collapse
    into one leaf that keeps whichever the (unordered) scan returned first.

    Normalisation must match _to_leaf() exactly: a NULL material_type and an
    empty-string one are the same leaf, so they must produce the same key.
    """
    return (entity, r.get("sku_id"), _label(r.get("item_description"), r.get("sku_id")),
            _item_type(r.get("material_type")),
            _category(r.get("item_category")), _category(r.get("sub_category")),
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


async def fetch_leaves(conn, entity: str = "both") -> list[dict[str, Any]]:
    """Leaf rows for one entity or both, godowns canonicalised and merged.

    Each entity is fetched independently: a missing legacy table or column for
    one entity degrades that entity to zero rows and is logged, rather than
    discarding rows already collected for the other.
    """
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
        try:
            rows = await conn.fetch(build_leaves_sql(prefix))
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
