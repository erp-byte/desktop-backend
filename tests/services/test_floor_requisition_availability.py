"""Production Indents: where a requested article is in stock, live, across places.

Pins the exact-article filter on both halves of the Stock Take read (counts and
ledger), and the shape the store screen reads: every asked-for article answered,
nothing at or below zero, largest place first, the caller's read scope applied.
"""
from __future__ import annotations

import asyncio

import pytest

from app.modules.floor_requisition.services import availability_service as av
from app.modules.stock_take.services import latest_stock_service as svc


class FakeUser:
    def __init__(self, warehouses=None, floors=None):
        self.allowed_warehouses, self.allowed_floors, self.is_admin = warehouses, floors, False


# ── the filter ──────────────────────────────────────────────────────────────

def test_item_keys_filter_the_counts_exactly():
    conds, params, applied = svc._build_filters(item_keys=["Sunflower Seeds Roasted ", "cashew 180"])
    assert "UPPER(BTRIM(item_name)) = ANY($1::text[])" in conds
    assert params == [["SUNFLOWER SEEDS ROASTED", "CASHEW 180"]]
    assert applied["itemKeys"] == ["Sunflower Seeds Roasted", "cashew 180"]


def test_item_keys_filter_the_ledger_too():
    conds, params = svc._build_txn_filters(start_index=2, item_keys=["Cashew 180"], stock_type=["Fresh Stock"])
    assert "UPPER(BTRIM(item_name)) = ANY($4::text[])" in conds
    assert params[-1] == ["CASHEW 180"]


def test_a_name_with_a_comma_stays_one_article():
    # A list is never comma-split; only a bare string is (_as_list).
    _, params, _ = svc._build_filters(item_keys=["Nuts, Roasted"])
    assert params == [["NUTS, ROASTED"]]


# ── the shape ───────────────────────────────────────────────────────────────

def _row(item, wh, floor, kg, qty=0.0):
    return {"item_key": item, "warehouse": wh, "floor": floor, "total_weight": kg, "total_quantity": qty}


@pytest.fixture
def fake_reads(monkeypatch):
    seen = {}

    async def places(conn):
        return {"A185": ["A185 Stores", "Mezzanine"], "W202": ["Store"], "D39": ["Savla"]}

    async def by_place(conn, **filters):
        seen.update(filters)
        return [
            _row("SUNFLOWER SEEDS ROASTED", "A185", "A185 Stores", 120.0),
            _row("SUNFLOWER SEEDS ROASTED", "D39", "Savla", 2000.0),
            _row("SUNFLOWER SEEDS ROASTED", "A185", "Mezzanine", 0.0),
            _row("SUNFLOWER SEEDS ROASTED", "W202", "Store", -4.5),
            _row("POUCH 100G", "W202", "Store", 0.0, qty=500),
        ], {}

    monkeypatch.setattr(svc, "fetch_places", places)
    monkeypatch.setattr(svc, "fetch_stock_by_place", by_place)
    return seen


def test_every_article_answered_largest_first_nothing_at_or_below_zero(fake_reads):
    out = asyncio.run(av.floor_availability(None, FakeUser(), [
        " Sunflower Seeds Roasted", "SUNFLOWER SEEDS ROASTED", "Pouch 100g", "Almond", ""]))
    arts = out["articles"]
    assert set(arts) == {"SUNFLOWER SEEDS ROASTED", "POUCH 100G", "ALMOND"}
    assert [(p["warehouse"], p["floor"], p["kg"]) for p in arts["SUNFLOWER SEEDS ROASTED"]] == [
        ("D39", "Savla", 2000.0), ("A185", "A185 Stores", 120.0)]
    assert arts["SUNFLOWER SEEDS ROASTED"][0]["warehouse_label"] == "Savla D-39"
    assert arts["POUCH 100G"] == [{"warehouse": "W202", "warehouse_label": "W202", "floor": "Store",
                                   "kg": 0.0, "qty": 500}], "a pcs article is kept on its quantity"
    assert arts["ALMOND"] == []


def test_reads_fresh_stock_only_for_exactly_those_articles(fake_reads):
    asyncio.run(av.floor_availability(None, FakeUser(), ["Almond", "Cashew 180"]))
    assert fake_reads["item_keys"] == ["ALMOND", "CASHEW 180"]
    assert fake_reads["stock_type"] == ["Fresh Stock"]


def test_an_unrestricted_caller_reads_every_place(fake_reads):
    asyncio.run(av.floor_availability(None, FakeUser(), ["Almond"]))
    assert fake_reads["warehouse"] is None and fake_reads["place_scope"] is None


def test_a_restricted_caller_reads_only_their_places(fake_reads):
    asyncio.run(av.floor_availability(None, FakeUser(["A185"], ["A185 Stores"]), ["Almond"]))
    assert fake_reads["warehouse"] == ["A185"]
    assert fake_reads["place_scope"] == {"whole": [], "pairs": ["A185|A185 STORES"]}


def test_no_articles_reads_nothing(fake_reads):
    assert asyncio.run(av.floor_availability(None, FakeUser(), ["", "  "])) == {"articles": {}}
    assert fake_reads == {}
