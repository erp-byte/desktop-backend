"""Save Output adopts an existing row before upserting (spec 2d 'Adopting existing rows'):
a legacy row with no batch, or a row of the same article under another spelling / with
no bom_line_id, is re-tagged instead of a second row being inserted."""
from __future__ import annotations

import asyncio

from app.modules.production.services import jc_accounting_v2 as acc
from app.modules.production.services import job_card_v2 as jcv2


class Conn:
    def __init__(self, exact=None, candidate=None):
        self.exact, self.candidate = exact, candidate
        self.sql: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        self.sql.append((s, args))
        if "adopt:exact" in s:
            return self.exact
        if "adopt:candidate" in s:
            return self.candidate
        raise AssertionError(s)

    async def execute(self, sql, *args):
        self.sql.append((" ".join(sql.split()), args))
        return "UPDATE 1"


def run(c):
    return asyncio.run(c)


def test_consumption_adopts_a_no_batch_or_differently_spelled_row():
    c = Conn(exact=None, candidate=555)
    run(jcv2._adopt_consumption_row(c, job_card_id=3, batch_id=44, name="Seeds", bom_line_id=101))
    upd = c.sql[-1]
    assert upd[0].startswith("UPDATE job_card_material_consumption_v2 SET batch_id = $2, material_sku_name = $3, bom_line_id = $4")
    assert upd[1] == (555, 44, "Seeds", 101)
    cand = [s for s in c.sql if "adopt:candidate" in s[0]][0]
    assert "UPPER(BTRIM(material_sku_name)) = UPPER(BTRIM($3))" in cand[0]
    assert "(batch_id = $2 OR batch_id IS NULL)" in cand[0]
    assert "ORDER BY (batch_id IS NULL), consumption_id" in cand[0]


def test_consumption_leaves_an_exact_row_alone_and_skips_without_a_batch():
    c = Conn(exact=9)
    run(jcv2._adopt_consumption_row(c, job_card_id=3, batch_id=44, name="Seeds", bom_line_id=101))
    assert not any(s.startswith("UPDATE") for s, _ in c.sql)
    c = Conn()
    run(jcv2._adopt_consumption_row(c, job_card_id=3, batch_id=None, name="Seeds", bom_line_id=None))
    assert c.sql == []


def test_byproducts_adopt_by_category_and_article():
    c = Conn(exact=None, candidate=777)
    run(acc._adopt_byproduct_row(c, job_card_id=3, batch_id=44, category="offgrade",
                                 material_name="Seeds", bom_line_id=101))
    assert c.sql[-1][1] == (777, 44, "Seeds", 101)
    cand = [s for s in c.sql if "adopt:candidate" in s[0]][0][0]
    assert "category = $3" in cand and "UPPER(BTRIM(material_name)) = UPPER(BTRIM($4))" in cand


def test_byproducts_without_an_article_adopt_the_no_article_row():
    c = Conn(exact=None, candidate=778)
    run(acc._adopt_byproduct_row(c, job_card_id=3, batch_id=44, category="control_sample",
                                 material_name=None, bom_line_id=None))
    cand = [s for s in c.sql if "adopt:candidate" in s[0]][0][0]
    assert "material_name IS NULL" in cand


def test_writers_call_the_adoption_before_upserting():
    import inspect
    up = inspect.getsource(jcv2.upsert_consumption_lines)
    assert up.index("_adopt_consumption_row(") < up.index("old_actual = await conn.fetchval(")
    sb = inspect.getsource(acc.save_byproducts)
    assert sb.index("_adopt_byproduct_row(") < sb.index("async def _insert(")


class SaveConn(Conn):
    """save_byproducts end to end: every row already has its exact row (no adoption)."""

    def __init__(self):
        super().__init__(exact=1)

    async def fetchrow(self, sql, *args):
        self.sql.append((" ".join(sql.split()), args))
        return {"byproduct_id": 1}

    def transaction(self):
        return _NullCtx()

    def is_in_transaction(self):
        return True

    def deletes(self):
        return [(s, a) for s, a in self.sql if s.startswith("DELETE FROM job_card_byproducts_v2")]


class _NullCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _save(monkeypatch, rows, batch_id=2):
    async def _unlocked(conn, job_card_id):
        return None
    monkeypatch.setattr(acc, "assert_not_locked", _unlocked)
    c = SaveConn()
    out = run(acc.save_byproducts(c, job_card_id=3, rows=rows, recorded_by="Op", batch_id=batch_id))
    assert out.get("saved") is True, out
    return c


def test_a_cleared_attributed_row_does_not_delete_no_article_rows(monkeypatch):
    # Batch 2 clears tukda/Seeds (the web sends it as 0): nothing was attributed, so
    # batch 1's tukda row saved without an article must survive.
    c = _save(monkeypatch, [{"category": "tukda", "quantity": 0, "uom": "KGS", "material_name": "Seeds"}])
    assert c.deletes() == []


def test_attribution_deletes_no_article_rows_of_the_saved_batch_only(monkeypatch):
    c = _save(monkeypatch, [{"category": "tukda", "quantity": 1.5, "uom": "KGS", "material_name": "Seeds"},
                            {"category": "dust", "quantity": 0, "uom": "KGS", "material_name": "Salt"}])
    [(sql, args)] = c.deletes()
    assert args == (3, ["tukda"], 2)
    assert "material_name IS NULL AND category = ANY($2::text[])" in sql
    assert "AND (COALESCE(batch_id, 0) = COALESCE($3::bigint, 0) OR batch_id IS NULL)" in sql
