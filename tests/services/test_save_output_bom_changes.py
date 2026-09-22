"""Save Output honours the job card's BOM changes (spec 2d)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.production import router as PR
from app.modules.production.services import jc_bom_changes as m

MASTER = [{"bom_line_id": 101, "material_sku_name": "Seeds", "item_type": "rm"},
          {"bom_line_id": 102, "material_sku_name": "Pouch", "item_type": "pm"}]


def resolver(removed=(), added=()):
    ch = m.Changes(removed=[{"change_id": 1, "change_type": "removed", "material_sku_name": n, "item_type": "rm"}
                            for n in removed],
                   added=[{"change_id": 2, "change_type": "added", "material_sku_name": n, "item_type": t,
                           "required_qty": None} for n, t in added])
    return m.ArticleResolver.build(MASTER, ch)


# ── the resolver ──
def test_states():
    r = resolver(removed=["Pouch"], added=[("Salt", "rm")])
    assert (r.state("seeds"), r.state("POUCH"), r.state(" salt "), r.state("Sugar"), r.state("")) == \
        ("bom", "removed", "added", "none", "none")
    assert r.is_removed(bom_line_id=102) and r.is_removed(name="pouch") and not r.is_removed(bom_line_id=101)
    assert r.canonical_name("SALT") == "Salt" and r.item_type("salt") == "rm" and r.item_type("POUCH") == "pm"


def test_a_superseded_add_counts_as_the_bom_line():
    r = resolver(added=[("seeds", "rm")])
    assert r.state("Seeds") == "bom" and r.added_row("Seeds") is None


def test_check_consumption():
    r = resolver(removed=["Pouch"], added=[("Salt", "rm")])
    assert r.check_consumption([{"bom_line_id": 101, "material_sku_name": "Seeds"},
                                {"bom_line_id": None, "material_sku_name": "salt"}]) is None
    assert r.check_consumption([{"bom_line_id": 999, "material_sku_name": "X"}])["error"] == "invalid_bom_line"
    assert r.check_consumption([{"bom_line_id": 102, "material_sku_name": "Pouch"}])["error"] \
        == "article_removed_from_job_card"
    assert r.check_consumption([{"bom_line_id": None, "material_sku_name": "pouch"}])["error"] \
        == "article_removed_from_job_card"
    assert r.check_consumption([{"bom_line_id": None, "material_sku_name": "Sugar"}])["error"] \
        == "article_not_on_job_card"


def test_check_rows_only_cares_about_quantities_above_zero():
    r = resolver(removed=["Pouch"])
    assert r.check_rows(balance=[{"material_name": "Pouch", "qty_kg": 0, "bom_line_id": 102}], byproducts=[]) is None
    assert r.check_rows(balance=[{"material_name": "CONSOLIDATED", "qty_kg": 3}], byproducts=[]) is None
    assert r.check_rows(balance=[{"material_name": "Pouch", "qty_kg": 2}], byproducts=[])["error"] \
        == "article_removed_from_job_card"
    assert r.check_rows(balance=[], byproducts=[{"material_name": "pouch", "qty_kg": 1}])["error"] \
        == "article_removed_from_job_card"


def test_stored_entry_uses_the_canonical_spelling_for_lines_without_an_id():
    r = resolver(added=[("Salt", "rm")])
    assert r.stored_entry({"bom_line_id": None, "material_sku_name": " SALT "})["material_sku_name"] == "Salt"
    e = {"bom_line_id": 101, "material_sku_name": "seeds"}
    assert r.stored_entry(e) is e


def test_an_added_fg_or_sfg_is_accounted_as_rm():
    r = resolver(added=[("Healthy Choice 100 g", "fg"), ("Roasted Mix", "sfg"), ("Tape", "pm")])
    assert (r.item_type("healthy choice 100 g"), r.item_type("ROASTED MIX"), r.item_type("tape")) == \
        ("rm", "rm", "pm")
    # Stored as RM consumption even when a client sends the real type.
    out = r.stored_entry({"bom_line_id": None, "material_sku_name": " healthy choice 100 g ", "input_kind": "FG"})
    assert (out["material_sku_name"], out["input_kind"]) == ("Healthy Choice 100 g", "RM")
    assert r.stored_entry({"bom_line_id": None, "material_sku_name": "roasted mix"})["input_kind"] == "RM"
    assert r.stored_entry({"bom_line_id": None, "material_sku_name": "tape", "input_kind": "RM"})["input_kind"] \
        == "PM"
    # A line that is not an added article keeps what the client sent.
    assert r.stored_entry({"bom_line_id": None, "material_sku_name": "Seeds", "input_kind": "SFG"})["input_kind"] \
        == "SFG"


# ── id coercion on the /outputs models ──
@pytest.mark.parametrize("v,want", [(0, None), (-3, None), ("", None), (None, None), (101, 101), ("101", 101)])
def test_line_ids_at_or_below_zero_are_none(v, want):
    assert PR.ConsumedLineV2(bom_line_id=v, material_sku_name="X", consumed_qty=1).bom_line_id == want
    assert PR.BalanceMaterialV2(bom_line_id=v, material_name="X", balance_type="returned", qty_kg=0).bom_line_id == want
    assert PR.ByproductLineV2(bom_line_id=v, category="offgrade", qty_kg=1).bom_line_id == want


def test_consumed_line_id_is_optional():
    assert PR.ConsumedLineV2(material_sku_name="Salt", consumed_qty=0).bom_line_id is None


# ── the route ──
class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *exc):
        return False


class Conn:
    def __init__(self):
        self.order: list[str] = []

    def transaction(self):
        return _Ctx(self)

    async def fetchrow(self, sql, *args):
        self.order.append("batch")
        return {"status": "open", "job_card_id": 3}

    async def fetch(self, sql, *args):
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        raise AssertionError(sql)


def _patch(monkeypatch, conn, res):
    from app.modules.production.services import job_card_v2 as jcv2
    written: list[tuple[str, list[dict]]] = []

    async def not_locked(c, jc):
        return None

    async def lock(c, jc):
        conn.order.append("lock")
        return 70

    async def get_resolver(c, jc):
        conn.order.append("resolver")
        return res

    async def upsert(c, *, job_card_id, entries, input_kind, recorded_by, batch_id=None):
        written.append((input_kind, entries))
        return len(entries)
    monkeypatch.setattr(jcv2, "assert_not_locked", not_locked)
    monkeypatch.setattr(jcv2, "upsert_consumption_lines", upsert)
    monkeypatch.setattr(m, "lock_line_for_card", lock)
    monkeypatch.setattr(m, "resolver_for", get_resolver)
    return written


def _call(conn, body):
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    user = SimpleNamespace(full_name="Op", phone=None, is_admin=False)
    return asyncio.run(PR.record_output_v2(req, 3, PR.RecordOutputV2Request(**body), user=user))


def test_a_save_with_only_added_articles_is_stored_under_the_change_rows_spelling(monkeypatch):
    conn = Conn()
    written = _patch(monkeypatch, conn, resolver(added=[("Salt", "rm")]))
    _call(conn, {"batch_id": 44, "rm_consumed": [{"bom_line_id": None, "material_sku_name": "salt",
                                                  "consumed_qty": 2.5}]})
    assert written[0] == ("RM", [{"bom_line_id": None, "material_sku_name": "Salt", "consumed_qty": 2.5,
                                  "remarks": None, "input_kind": "RM", "source_dispatch_id": None}])
    assert conn.order[:2] == ["lock", "resolver"]      # lock before the batch row is read or updated


def test_an_added_fg_is_saved_as_rm_consumption(monkeypatch):
    conn = Conn()
    written = _patch(monkeypatch, conn, resolver(added=[("Healthy Choice 100 g", "fg")]))
    _call(conn, {"batch_id": 44, "rm_consumed": [{"bom_line_id": None, "material_sku_name": "HEALTHY CHOICE 100 G",
                                                  "consumed_qty": 4, "input_kind": "FG"}]})
    entry = written[0][1][0]
    assert (entry["material_sku_name"], entry["input_kind"]) == ("Healthy Choice 100 g", "RM")


def test_a_cleared_figure_is_saved_as_zero(monkeypatch):
    conn = Conn()
    written = _patch(monkeypatch, conn, resolver())
    _call(conn, {"batch_id": 44, "rm_consumed": [{"bom_line_id": 101, "material_sku_name": "Seeds",
                                                  "consumed_qty": 0}]})
    assert written[0][1][0]["consumed_qty"] == 0


def test_removed_articles_are_refused_before_anything_is_written(monkeypatch):
    conn = Conn()
    written = _patch(monkeypatch, conn, resolver(removed=["Pouch"]))
    with pytest.raises(HTTPException) as e:
        _call(conn, {"batch_id": 44, "pm_consumed": [{"bom_line_id": 102, "material_sku_name": "Pouch",
                                                      "consumed_qty": 5}]})
    assert e.value.status_code == 400 and e.value.detail["error"] == "article_removed_from_job_card"
    assert written == [] and "batch" not in conn.order
    with pytest.raises(HTTPException) as e:
        _call(conn, {"batch_id": 44, "balance_materials": [{"material_name": "Pouch", "balance_type": "returned",
                                                            "qty_kg": 1}]})
    assert e.value.detail["error"] == "article_removed_from_job_card"


def test_unknown_names_are_refused(monkeypatch):
    conn = Conn()
    _patch(monkeypatch, conn, resolver())
    with pytest.raises(HTTPException) as e:
        _call(conn, {"batch_id": 44, "rm_consumed": [{"bom_line_id": None, "material_sku_name": "Sugar",
                                                      "consumed_qty": 1}]})
    assert e.value.detail["error"] == "article_not_on_job_card"
