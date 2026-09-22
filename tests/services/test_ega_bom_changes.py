"""Extra giveaway checks the job card's BOM changes (spec 2d)."""
from __future__ import annotations

import asyncio

from app.modules.production.services import jc_bom_changes as m
from app.modules.production.services import job_card_v2 as jcv2


class Conn:
    async def fetchrow(self, sql, *a):
        # jc_meta: a packing-stage card
        return {"bom_id": 9, "stage": "packaging", "process_name": "Packaging", "output_kind": "FG"}

    async def fetchval(self, sql, *a):
        if "FROM bom_line" in sql:
            return None                     # not on the BOM module's list
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        return "DELETE 0"


def _resolver(removed=(), added=()):
    ch = m.Changes(removed=[{"change_id": 1, "change_type": "removed", "material_sku_name": n, "item_type": "rm"}
                            for n in removed],
                   added=[{"change_id": 2, "change_type": "added", "material_sku_name": n, "item_type": t}
                          for n, t in added])
    return m.ArticleResolver.build([{"bom_line_id": 101, "material_sku_name": "Seeds", "item_type": "rm"}], ch)


def _run(monkeypatch, res, name, bom_line_id=None):
    async def not_locked(c, jc):
        return None

    async def get_res(c, jc):
        return res

    async def insert(c, fn):
        return {"balance_id": 1}
    monkeypatch.setattr(jcv2, "assert_not_locked", not_locked)
    monkeypatch.setattr(jcv2, "insert_with_pk_retry", insert)
    monkeypatch.setattr(m, "resolver_for", get_res)
    rows = [{"balance_type": "extra_given", "material_name": name, "qty_kg": 1.0, "bom_line_id": bom_line_id}]
    return asyncio.run(jcv2.replace_balance_materials(Conn(), job_card_id=3, rows=rows, batch_id=None))


def test_added_rm_passes_the_ega_check(monkeypatch):
    out = _run(monkeypatch, _resolver(added=[("Salt", "rm")]), "Salt")
    assert out.get("error") is None


def test_added_fg_and_sfg_pass_the_ega_check_as_rm(monkeypatch):
    assert _run(monkeypatch, _resolver(added=[("Healthy Choice 100 g", "fg")]), "healthy choice 100 g") \
        .get("error") is None
    assert _run(monkeypatch, _resolver(added=[("Roasted Mix", "SFG")]), "Roasted Mix").get("error") is None


def test_added_pm_is_refused_as_non_rm(monkeypatch):
    assert _run(monkeypatch, _resolver(added=[("Tape", "pm")]), "Tape")["error"] == "ega_non_rm_material"


def test_removed_article_is_not_in_the_bom(monkeypatch):
    assert _run(monkeypatch, _resolver(removed=["Seeds"]), "Seeds", 101)["error"] == "ega_material_not_in_bom"
