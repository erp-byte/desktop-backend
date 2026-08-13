"""Inward leaf feed for the Inventory Ledger.

The ledger unions two legacy inward channels per entity. The traps these pin:
  - referencing rtv/service after the UNION (columns absent on the bulk table)
  - joining header to lines on transaction_no alone (cross-mixes the families)
  - a GROUP BY that omits a selected column (query does not compile)
  - summing PM pieces into RM kilograms
  - emitting NULL for a field the TypeScript client types as `string`

pglast is OPTIONAL. Only the two tests marked `requires_pglast` parse SQL; every
other test in this file must run on a box that does not have it installed. Test
dependencies are declared in pyproject.toml under [dependency-groups] test.

Run:  PYTHONPATH=. python -m pytest tests/services/test_ledger_leaves.py -v
"""
from __future__ import annotations

import asyncpg
import pytest

from app.modules.ledger.services import leaves_service as S

try:
    import pglast
    from pglast.stream import RawStream
    from pglast.visitors import Visitor
except ImportError:  # pragma: no cover - exercised only on a box without pglast
    pglast = None

requires_pglast = pytest.mark.skipif(
    pglast is None,
    reason="pglast gives real PostgreSQL grammar validation; optional",
)

# Aggregate functions this query is allowed to use. A selected expression that
# contains one of these is exempt from the GROUP BY requirement.
AGGREGATES = {"sum", "count", "avg", "min", "max",
              "pg_catalog.sum", "pg_catalog.count", "pg_catalog.avg",
              "pg_catalog.min", "pg_catalog.max"}


class FakeConn:
    """Captures SQL and replays canned rows."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return list(self.rows)


class PerEntityConn:
    """Replays a different outcome per entity prefix — canned rows, or an
    exception standing in for a legacy table/column absent in this environment."""

    def __init__(self, outcomes: dict):
        self.outcomes = outcomes
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        for prefix, outcome in self.outcomes.items():
            if f"{prefix}_articles_v2" in sql:
                if isinstance(outcome, Exception):
                    raise outcome
                return list(outcome)
        return []


def parses(sql: str) -> bool:
    pglast.parse_sql(sql)
    return True


def _contains_aggregate(node) -> bool:
    hits: list[str] = []

    class FuncNames(Visitor):
        def visit_FuncCall(self, ancestors, node):  # noqa: N802 - pglast dispatch
            hits.append(".".join(str(p.sval) for p in node.funcname).lower())

    FuncNames()(node)
    return any(name in AGGREGATES for name in hits)


def ungrouped_selected_columns(sql: str) -> list[str]:
    """Selected expressions of the OUTER SELECT that contain no aggregate and do
    not appear verbatim in its GROUP BY. Expressions are compared after pglast
    normalises them, so `trim(x)` and `TRIM(BOTH FROM x)` compare equal."""
    stmt = pglast.parse_sql(sql)[0].stmt
    grouped = {RawStream()(g) for g in (stmt.groupClause or ())}
    return [
        RawStream()(t.val) for t in stmt.targetList
        if not _contains_aggregate(t.val) and RawStream()(t.val) not in grouped
    ]


def row(**kw):
    base = dict(
        sku_id=1, item_description="Cashew 320", item_category="Cashew Kernal",
        sub_category="Cashew", material_type="rm", warehouse_raw="d39",
        net_weight_kg=100.0, qty_units=0.0, value_indicative=1000.0,
    )
    base.update(kw)
    return base


# ── SQL shape ──────────────────────────────────────────────────────

@requires_pglast
@pytest.mark.parametrize("prefix", ["cfpl", "cdpl"])
def test_sql_parses(prefix):
    """Guarantees exactly one thing: the generated string is syntactically valid
    PostgreSQL. pglast.parse_sql is grammar-only — it accepts
    `SELECT a, b, SUM(c) FROM t GROUP BY a`, so it does NOT catch a GROUP BY that
    omits a selected column. test_every_selected_column_is_grouped does that."""
    assert parses(S.build_leaves_sql(prefix))


@requires_pglast
@pytest.mark.parametrize("prefix", ["cfpl", "cdpl"])
def test_every_selected_column_is_grouped(prefix):
    """The real guard the grammar check cannot give: walk the parsed outer SELECT
    and assert every aggregate-free selected expression is also in the GROUP BY.
    Stricter than PostgreSQL (which also permits functionally-dependent columns),
    so it can only over-report — it never passes a query that would fail to run."""
    assert ungrouped_selected_columns(S.build_leaves_sql(prefix)) == []


@requires_pglast
def test_the_group_by_check_would_actually_catch_the_defect():
    """Pins the checker itself: without it, test_sql_parses passes on SQL that
    PostgreSQL rejects."""
    bad = "SELECT a, b, SUM(c) FROM t GROUP BY a"
    assert parses(bad)                              # grammar-only: no complaint
    assert ungrouped_selected_columns(bad) == ["b"]  # structural check: caught


@pytest.mark.parametrize("prefix", ["cfpl", "cdpl"])
def test_sql_orders_deterministically(prefix):
    """Without ORDER BY, PostgreSQL row order is unspecified, so which of two
    rows the merge keeps is a coin flip across reloads."""
    sql = S.build_leaves_sql(prefix)
    assert "ORDER BY" in sql
    assert sql.index("GROUP BY") < sql.index("ORDER BY")


@pytest.mark.parametrize("prefix", ["cfpl", "cdpl"])
def test_sql_reads_both_inward_channels(prefix):
    sql = S.build_leaves_sql(prefix)
    assert f"{prefix}_articles_v2" in sql
    assert f"{prefix}_bulk_entry_articles" in sql
    assert f"{prefix}_transactions_v2" in sql
    assert f"{prefix}_bulk_entry_transactions" in sql


def test_sku_master_is_not_joined():
    """cfplsku / cdplsku hold one row each — joining them yields NULL."""
    sql = S.build_leaves_sql("cfpl")
    assert "cfplsku" not in sql


def test_rtv_service_filter_is_inside_the_v2_branch_only():
    """Those columns do not exist on the bulk table, so a post-union reference
    is a hard error. The predicate must sit before the UNION ALL."""
    sql = S.build_leaves_sql("cfpl")
    assert "rtv" in sql and "service" in sql
    assert sql.index("rtv") < sql.index("UNION ALL")


def test_header_line_join_uses_both_keys():
    """transaction_no alone cross-mixes the two families."""
    sql = S.build_leaves_sql("cfpl")
    joined = " ".join(sql.split())
    assert "t.transaction_no = a.transaction_no" in joined
    assert "t._source = a._source" in joined


def test_sql_selects_both_quantity_columns():
    """PM and RM quantities come from different columns and must never merge."""
    sql = S.build_leaves_sql("cfpl")
    assert "net_weight" in sql and "quantity_units" in sql


# ── Aggregation / merge behaviour ──────────────────────────────────

@pytest.mark.asyncio
async def test_entity_filter_queries_only_that_prefix():
    conn = FakeConn()
    await S.fetch_leaves(conn, entity="cdpl")
    assert len(conn.queries) == 1
    assert "cdpl_articles_v2" in conn.queries[0][0]
    assert "cfpl_articles_v2" not in conn.queries[0][0]


@pytest.mark.asyncio
async def test_both_queries_each_entity_and_stamps_the_row():
    conn = FakeConn([row()])
    out = await S.fetch_leaves(conn, entity="both")
    assert len(conn.queries) == 2
    assert {leaf["entity"] for leaf in out} == {"cfpl", "cdpl"}


@pytest.mark.asyncio
async def test_godown_aliases_merge_into_one_leaf():
    """'d39' and 'old savla' are the same physical godown; their quantities add."""
    conn = FakeConn([
        row(warehouse_raw="d39", net_weight_kg=100.0, value_indicative=1000.0),
        row(warehouse_raw="old savla", net_weight_kg=50.0, value_indicative=500.0),
    ])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert len(out) == 1
    assert out[0]["godown"] == "Savla D-39"
    assert out[0]["inward_qty"] == 150.0
    assert out[0]["value_indicative"] == 1500.0


@pytest.mark.asyncio
async def test_different_godowns_stay_separate():
    conn = FakeConn([
        row(warehouse_raw="d39"),
        row(warehouse_raw="savla bond"),
    ])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert sorted(leaf["godown"] for leaf in out) == ["Savla Bond", "Savla D-39"]


@pytest.mark.asyncio
async def test_pm_uses_piece_counts_and_nos_class():
    conn = FakeConn([row(material_type="pm", net_weight_kg=999.0, qty_units=42.0)])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert out[0]["uom_class"] == "nos"
    assert out[0]["inward_qty"] == 42.0


@pytest.mark.asyncio
async def test_rm_uses_weight_and_kg_class():
    conn = FakeConn([row(material_type="rm", net_weight_kg=100.0, qty_units=7.0)])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert out[0]["uom_class"] == "kg"
    assert out[0]["inward_qty"] == 100.0


@pytest.mark.asyncio
async def test_pm_and_rm_never_merge_even_for_one_sku_and_godown():
    conn = FakeConn([
        row(material_type="rm", net_weight_kg=100.0),
        row(material_type="pm", qty_units=42.0),
    ])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert len(out) == 2
    assert {leaf["uom_class"] for leaf in out} == {"kg", "nos"}


@pytest.mark.asyncio
async def test_null_warehouse_becomes_unassigned():
    conn = FakeConn([row(warehouse_raw=None)])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert out[0]["godown"] == "Unassigned"


@pytest.mark.asyncio
async def test_leaf_carries_every_field_the_frontend_reads():
    conn = FakeConn([row()])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert set(out[0]) == {
        "sku_id", "label", "item_type", "group", "subgroup", "uom_class",
        "godown", "value_indicative", "entity",
        "opening_qty", "inward_qty", "production_qty", "returns_qty",
        "consumption_qty", "outward_qty", "transfer_out_qty",
    }


@pytest.mark.asyncio
async def test_unsourced_movement_columns_are_zero():
    conn = FakeConn([row()])
    out = await S.fetch_leaves(conn, entity="cfpl")
    leaf = out[0]
    for key in ("opening_qty", "production_qty", "returns_qty",
                "consumption_qty", "outward_qty", "transfer_out_qty"):
        assert leaf[key] == 0


@pytest.mark.asyncio
async def test_unknown_entity_is_rejected():
    """Guards against a request value reaching SQL interpolation."""
    conn = FakeConn()
    with pytest.raises(ValueError):
        await S.fetch_leaves(conn, entity="cfpl; DROP TABLE x")


# ── NULL coalescing (the client types these fields as `string`) ─────

TEXT_FIELDS = ("label", "item_type", "group", "subgroup")


@pytest.mark.asyncio
async def test_null_text_fields_never_reach_the_client():
    """item_category / sub_category / material_type are NULL on legacy rows (see
    inward_tools.py:2207, :2400). The client declares them non-nullable and calls
    .toLowerCase() on group/subgroup — a None here blanks the whole module."""
    conn = FakeConn([row(item_description=None, item_category=None,
                         sub_category=None, material_type=None)])
    out = await S.fetch_leaves(conn, entity="cfpl")
    leaf = out[0]
    assert leaf["group"] == S.UNCATEGORISED
    assert leaf["subgroup"] == S.UNCATEGORISED
    assert leaf["item_type"] == ""
    assert leaf["label"] == "(unnamed SKU 1)"  # identifying, not blank
    for field in TEXT_FIELDS:
        assert isinstance(leaf[field], str)


@pytest.mark.asyncio
async def test_blank_and_whitespace_text_fields_coalesce_like_nulls():
    conn = FakeConn([row(item_description="   ", item_category="",
                         sub_category="  ", material_type="  RM  ")])
    out = await S.fetch_leaves(conn, entity="cfpl")
    leaf = out[0]
    assert leaf["group"] == S.UNCATEGORISED
    assert leaf["subgroup"] == S.UNCATEGORISED
    assert leaf["label"] == "(unnamed SKU 1)"
    assert leaf["item_type"] == "rm"


@pytest.mark.asyncio
async def test_a_null_row_still_carries_a_usable_quantity():
    """Coalescing must not swallow the figure the row exists for."""
    conn = FakeConn([row(item_category=None, sub_category=None,
                         material_type=None, net_weight_kg=250.0)])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert out[0]["inward_qty"] == 250.0
    assert out[0]["uom_class"] == "kg"  # NULL material_type is not PM


# ── Merge-key identity ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rows_differing_only_by_category_do_not_merge():
    """The key must carry category. Otherwise these two collapse into one leaf
    that keeps whichever the unordered scan happened to return first, and the
    item jumps between tree groups across reloads."""
    conn = FakeConn([
        row(item_category="Cashew Kernal", net_weight_kg=100.0),
        row(item_category="CASHEW KERNAL", net_weight_kg=40.0),
    ])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert len(out) == 2
    assert {leaf["group"] for leaf in out} == {"Cashew Kernal", "CASHEW KERNAL"}
    assert sorted(leaf["inward_qty"] for leaf in out) == [40.0, 100.0]


@pytest.mark.asyncio
async def test_rows_differing_only_by_subcategory_do_not_merge():
    conn = FakeConn([row(sub_category="Cashew"), row(sub_category="Cashew Split")])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert {leaf["subgroup"] for leaf in out} == {"Cashew", "Cashew Split"}


@pytest.mark.asyncio
async def test_null_and_blank_material_type_merge_into_one_leaf():
    """_leaf_key and _to_leaf must normalise identically — otherwise a NULL row
    and an empty-string row yield two leaves that look identical on screen."""
    conn = FakeConn([
        row(material_type=None, net_weight_kg=100.0, value_indicative=1000.0),
        row(material_type="", net_weight_kg=25.0, value_indicative=250.0),
    ])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert len(out) == 1
    assert out[0]["inward_qty"] == 125.0
    assert out[0]["value_indicative"] == 1250.0


@pytest.mark.asyncio
async def test_null_and_null_category_rows_merge_into_one_leaf():
    conn = FakeConn([
        row(item_category=None, sub_category=None, net_weight_kg=10.0),
        row(item_category=None, sub_category=None, net_weight_kg=5.0),
    ])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert len(out) == 1
    assert out[0]["inward_qty"] == 15.0


# ── Per-entity resilience ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_both_keeps_the_healthy_entity_when_the_other_table_is_missing():
    """A missing table for one entity must not discard rows already fetched for
    the other — these legacy schemas are inferred, not schema-verified."""
    conn = PerEntityConn({
        "cfpl": [row(net_weight_kg=100.0)],
        "cdpl": asyncpg.UndefinedTableError('relation "cdpl_articles_v2" does not exist'),
    })
    out = await S.fetch_leaves(conn, entity="both")
    assert len(out) == 1
    assert out[0]["entity"] == "cfpl"
    assert out[0]["inward_qty"] == 100.0


@pytest.mark.asyncio
async def test_both_survives_a_missing_column_too():
    conn = PerEntityConn({
        "cfpl": asyncpg.UndefinedColumnError('column "rtv" does not exist'),
        "cdpl": [row(net_weight_kg=60.0)],
    })
    out = await S.fetch_leaves(conn, entity="both")
    assert [leaf["entity"] for leaf in out] == ["cdpl"]


@pytest.mark.asyncio
async def test_all_entities_missing_yields_an_empty_feed_not_an_error():
    conn = PerEntityConn({
        "cfpl": asyncpg.UndefinedTableError("nope"),
        "cdpl": asyncpg.UndefinedTableError("nope"),
    })
    assert await S.fetch_leaves(conn, entity="both") == []


@pytest.mark.asyncio
async def test_an_unexpected_database_error_still_propagates():
    """Only the two missing-schema cases degrade; anything else must surface."""
    class Boom:
        async def fetch(self, sql, *args):
            raise asyncpg.PostgresSyntaxError("syntax error")

    with pytest.raises(asyncpg.PostgresSyntaxError):
        await S.fetch_leaves(Boom(), entity="cfpl")
