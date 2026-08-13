"""Inward leaf feed for the Inventory Ledger.

The ledger unions two legacy inward channels per entity. The traps these pin:
  - referencing rtv/service after the UNION (columns absent on the bulk table)
  - joining header to lines on transaction_no alone (cross-mixes the families)
  - a GROUP BY that omits a selected column (query does not compile)
  - summing PM pieces into RM kilograms

Run:  PYTHONPATH=. python -m pytest tests/services/test_ledger_leaves.py -v
"""
from __future__ import annotations

import pytest

from app.modules.ledger.services import leaves_service as S

pglast = pytest.importorskip(
    "pglast", reason="pglast gives real PostgreSQL grammar validation; optional")


class FakeConn:
    """Captures SQL and replays canned rows."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return list(self.rows)


def parses(sql: str) -> bool:
    pglast.parse_sql(sql)
    return True


def row(**kw):
    base = dict(
        sku_id=1, item_description="Cashew 320", item_category="Cashew Kernal",
        sub_category="Cashew", material_type="rm", warehouse_raw="d39",
        net_weight_kg=100.0, qty_units=0.0, value_indicative=1000.0,
    )
    base.update(kw)
    return base


# ── SQL shape ──────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ["cfpl", "cdpl"])
def test_sql_parses(prefix):
    """Guards the defect the design review caught: a GROUP BY that omits a
    selected column does not compile."""
    assert parses(S.build_leaves_sql(prefix))


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
    assert sql.count("rtv") == 1


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
