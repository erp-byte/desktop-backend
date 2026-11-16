"""SO upload kg + article matching.

Rite Bite SO lines (RB Date Bites 20g / Monocarton) showed 0 kg because:
  - all_sku had no kg per unit (uom NULL) for those articles, and the upload saved
    kg = qty x uom = no kg, ignoring the kg the Tally SO Book reports (Alt. Units);
  - the matcher compared a lower-cased SO name with mixed-case master names, so an
    identical name scored 72.6 < 75 and the line came in UNMATCHED;
  - the master list was cached at startup, so fixing all_sku needed a restart.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.modules.so.services import item_matcher
from app.modules.so.services.ingest import _compute_quantity_units
from app.modules.so.services.item_matcher import MasterItem, current_master_items, match_sku


def _item(name, uom):
    return MasterItem(particulars=name, item_type="fg", group=None, sub_group=None,
                      uom=uom, sale_group=None, gst=None)


# ── kg ────────────────────────────────────────────────────────────────────────

def test_kg_comes_from_master_uom_when_the_master_has_one():
    assert _compute_quantity_units(_item("x", 0.24), 2900, alt_kg=580) == (696.0, "per_unit")


def test_kg_falls_back_to_excel_alt_units_when_master_has_no_uom():
    assert _compute_quantity_units(_item("x", None), 2900, alt_kg=696) == (696.0, None)


def test_kg_falls_back_to_excel_alt_units_when_master_uom_is_zero():
    assert _compute_quantity_units(_item("x", 0.0), 2364, alt_kg=567.36) == (567.36, "per_unit")


def test_kg_falls_back_to_excel_alt_units_for_an_unmatched_line():
    assert _compute_quantity_units(None, 2900, alt_kg="580") == (580.0, None)


@pytest.mark.parametrize("alt", [None, 0, -1, "", "n/a"])
def test_no_usable_kg_from_either_side_stays_none(alt):
    assert _compute_quantity_units(_item("x", None), 2900, alt_kg=alt) == (None, None)


# ── matching ──────────────────────────────────────────────────────────────────

def test_match_is_case_blind_against_mixed_case_master_names():
    items = [
        _item("RB Date Bites Classic Monocarton (Date Bite 20 g x 12 pcs)", None),
        _item("RB Date Bites Choco fudge Monocarton (Date Bite 20 g x 12 pcs)", None),
    ]
    hit, score = match_sku("RB Date Bites Choco fudge Monocarton (Date Bite 20 g x 12 pcs)", items)
    assert hit is items[1]
    assert score == 1.0


def test_double_space_in_the_so_name_still_matches_fully():
    items = [_item("RB Date Bites Classic Monocarton (Date Bite 20 g x 12 pcs)", None)]
    hit, score = match_sku("RB Date Bites Classic Monocarton  (Date Bite 20 g x 12 pcs)", items)
    assert hit is items[0] and score == 1.0


# ── master list refresh ───────────────────────────────────────────────────────

def _app(cached, loaded_at):
    return SimpleNamespace(state=SimpleNamespace(master_items=cached, master_items_loaded_at=loaded_at,
                                                 db_pool=object()))


def test_master_list_is_reloaded_once_stale(monkeypatch):
    fresh = [_item("new article", 0.1)]

    async def _load(pool):
        return fresh

    monkeypatch.setattr(item_matcher, "load_master_items", _load)
    monkeypatch.setattr(item_matcher.time, "monotonic", lambda: 1000.0)
    app = _app([_item("old", None)], loaded_at=1000.0 - item_matcher.MASTER_ITEMS_TTL_S - 1)
    assert asyncio.run(current_master_items(app)) is fresh
    assert app.state.master_items_loaded_at == 1000.0


def test_master_list_is_not_reloaded_while_fresh(monkeypatch):
    async def _load(pool):
        raise AssertionError("should not reload")

    monkeypatch.setattr(item_matcher, "load_master_items", _load)
    monkeypatch.setattr(item_matcher.time, "monotonic", lambda: 1000.0)
    cached = [_item("cached", 0.1)]
    assert asyncio.run(current_master_items(_app(cached, loaded_at=990.0))) is cached


def test_a_failed_reload_keeps_the_cached_list(monkeypatch):
    async def _load(pool):
        raise RuntimeError("db down")

    monkeypatch.setattr(item_matcher, "load_master_items", _load)
    cached = [_item("cached", 0.1)]
    assert asyncio.run(current_master_items(_app(cached, loaded_at=None))) is cached
