"""jc_bom_changes: scope, effective BOM, records check (spec 2a).
The fake connection answers by the /* jcbc:<tag> */ comment each query carries."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.modules.production.services import jc_bom_changes as m

TAG = re.compile(r"/\* (jcbc:[a-z_]+) \*/")


class FakeConn:
    """answers: {tag: value or callable(*args)}; records (tag, args) for every call."""

    def __init__(self, **answers):
        self.answers = {k.replace("__", ":"): v for k, v in answers.items()}
        self.calls: list[tuple[str, tuple]] = []

    def _answer(self, sql, args):
        t = TAG.search(sql)
        assert t, f"untagged query: {sql[:80]}"
        tag = t.group(1)
        self.calls.append((tag, args))
        assert tag in self.answers, f"unexpected query {tag}"
        v = self.answers[tag]
        return v(*args) if callable(v) else v

    async def fetch(self, sql, *args):
        return self._answer(sql, args)

    async def fetchrow(self, sql, *args):
        return self._answer(sql, args)

    async def fetchval(self, sql, *args):
        return self._answer(sql, args)

    async def execute(self, sql, *args):
        return self._answer(sql, args)

    def tags(self):
        return [t for t, _ in self.calls]


def run(coro):
    return asyncio.run(coro)


WHEN = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def change(cid, ctype, name, itype="rm", **over):
    row = {"change_id": cid, "change_type": ctype, "material_sku_name": name, "item_type": itype,
           "sku_id": 11 if ctype == "added" else None, "required_qty": None, "required_unit": None,
           "note": None, "made_on_job_card_id": 1, "made_on_job_card_number": "PLAN-7-L1-S1",
           "changed_by": "Planner Pat", "changed_at": WHEN}
    row.update(over)
    return row


def line(bid, name, itype="rm", **over):
    row = {"bom_line_id": bid, "line_number": bid, "material_sku_name": name, "item_type": itype,
           "uom": "KGS", "quantity_per_unit": 0.5, "loss_pct": 1.0, "godown": None}
    row.update(over)
    return row


# ── pure helpers ──
def test_keys():
    assert m.article_key("  Sunflower Seeds ") == "SUNFLOWER SEEDS"
    assert m.article_key(None) == ""
    assert m.loose_key("Seeds  Roasted  100g") == "SEEDS ROASTED 100G"
    assert m.type_of(" PM ") == "pm"


def test_no_changes_returns_the_master_list_unchanged():
    master = [line(1, "Seeds"), line(2, "Pouch", "pm")]
    lines, flags = m.effective_bom_lines(master, [], m.Changes())
    assert lines == master and flags == {"not_on_bom": set(), "superseded": set()}


def test_removed_drops_every_line_with_that_article_and_added_is_appended():
    master = [line(1, "Seeds"), line(2, " seeds "), line(3, "Pouch", "pm")]
    ch = m.Changes(removed=[change(10, "removed", "SEEDS")],
                   added=[change(11, "added", "Salt", required_qty=Decimal("2.500"), required_unit="kg")])
    lines, flags = m.effective_bom_lines(master, [], ch)
    assert [l["material_sku_name"] for l in lines] == ["Pouch", "Salt"]
    salt = lines[1]
    assert salt == {"bom_line_id": None, "line_number": None, "material_sku_name": "Salt", "item_type": "rm",
                    "article_type": "rm", "uom": "KGS", "quantity_per_unit": None, "loss_pct": None,
                    "godown": None, "added": True, "change_id": 11, "sku_id": 11, "required_qty": 2.5,
                    "required_unit": "kg"}
    assert flags == {"not_on_bom": set(), "superseded": set()}


def test_kind_of_is_the_accounting_kind():
    assert [m.kind_of(t) for t in ("rm", " PM ", "fg", "SFG", None)] == ["rm", "pm", "rm", "rm", "rm"]
    assert m.ADDABLE_TYPES == ("rm", "pm", "fg", "sfg") and m.ITEM_TYPES == ("rm", "pm")
    assert (m.UNIT_FOR["fg"], m.UNIT_FOR["sfg"], m.UOM_FOR["fg"], m.UOM_FOR["sfg"]) == ("kg", "kg", "KGS", "KGS")


def test_an_added_fg_or_sfg_is_an_rm_line_that_keeps_its_real_type():
    # Accounting treats every SFG article as the previous stage's seam and consumption
    # takes only RM/PM/SFG/WIP, so an added FG/SFG is accounted as RM (spec Addendum A2).
    ch = m.Changes(added=[change(11, "added", "Healthy Choice 100 g", "fg", required_qty=Decimal("3"),
                                 required_unit="kg"),
                          change(12, "added", "Roasted Mix", "SFG"),
                          change(13, "added", "Pouch", "pm")])
    lines, _ = m.effective_bom_lines([line(1, "Seeds")], [], ch)
    fg, sfg, pm = lines[1:]
    assert (fg["item_type"], fg["article_type"], fg["uom"], fg["required_unit"]) == ("rm", "fg", "KGS", "kg")
    assert (sfg["item_type"], sfg["article_type"], sfg["uom"]) == ("rm", "sfg", "KGS")
    assert (pm["item_type"], pm["article_type"], pm["uom"]) == ("pm", "pm", "PCS")
    payload = m.changes_payload(1, ch, {"not_on_bom": set(), "superseded": set()})
    assert [a["item_type"] for a in payload["added"]] == ["fg", "sfg", "pm"]


def test_superseded_and_not_on_bom_are_flagged():
    master = [line(1, "Seeds")]
    ch = m.Changes(removed=[change(10, "removed", "Old Pouch", "pm")], added=[change(11, "added", "seeds")])
    lines, flags = m.effective_bom_lines(master, [], ch)
    assert [l["material_sku_name"] for l in lines] == ["Seeds"]
    assert flags == {"not_on_bom": {10}, "superseded": {11}}


def test_an_empty_bom_with_changes_uses_the_indent_lines():
    indents = [{"bom_line_id": None, "material_sku_name": "Seeds", "item_type": "rm", "uom": "KGS",
                "loss_pct": Decimal("1.0"), "godown": "G1"}]
    ch = m.Changes(added=[change(11, "added", "Pouch", "pm")])
    lines, _ = m.effective_bom_lines([], indents, ch)
    assert [l["material_sku_name"] for l in lines] == ["Seeds", "Pouch"]
    assert lines[0]["loss_pct"] == 1.0 and lines[0]["quantity_per_unit"] is None


def test_changes_payload_shape():
    ch = m.Changes(removed=[change(10, "removed", "Seeds")],
                   added=[change(11, "added", "Pouch", "pm", required_qty=Decimal("100"), required_unit="pcs")])
    out = m.changes_payload(5, ch, {"not_on_bom": {10}, "superseded": set()})
    assert out == {
        "scope_job_card_id": 5,
        "removed": [{"change_id": 10, "material_sku_name": "Seeds", "item_type": "rm", "note": None,
                     "changed_by": "Planner Pat", "changed_at": WHEN.isoformat(),
                     "made_on_job_card_number": "PLAN-7-L1-S1", "not_on_bom": True}],
        "added": [{"change_id": 11, "material_sku_name": "Pouch", "item_type": "pm", "note": None,
                   "changed_by": "Planner Pat", "changed_at": WHEN.isoformat(),
                   "made_on_job_card_number": "PLAN-7-L1-S1", "sku_id": 11, "required_qty": 100.0,
                   "required_unit": "pcs", "superseded": False}],
    }


# ── scope ──
def test_scope_walks_back_to_the_first_card_and_lists_the_chains_live_cards():
    conn = FakeConn(
        jcbc__card={"job_card_id": 3, "job_card_number": "PLAN-7-L1-S3", "plan_line_id": 70,
                    "bom_id": 9, "status": "locked"},
        jcbc__head=1,
        jcbc__chain=[{"job_card_id": 1, "job_card_number": "PLAN-7-L1-S1", "status": "completed"},
                     {"job_card_id": 2, "job_card_number": "PLAN-7-L1-S2", "status": "in_progress"},
                     {"job_card_id": 3, "job_card_number": "PLAN-7-L1-S3", "status": "locked"}])
    s = run(m.scope_of(conn, 3))
    assert (s.scope_job_card_id, s.plan_line_id, s.bom_id, s.card_ids) == (1, 70, 9, [1, 2, 3])
    assert s.finished is False
    assert conn.calls[1] == ("jcbc:head", (3,)) and conn.calls[2] == ("jcbc:chain", (1,))


def test_a_deleted_card_has_no_scope():
    assert run(m.scope_of(FakeConn(jcbc__card=None), 3)) is None


def test_scope_sql_stays_on_one_plan_line():
    # Partial chains and merged runs: the walk must not cross plan lines.
    assert "p.plan_line_id = b.plan_line_id" in " ".join(m._HEAD_SQL.split())
    assert "c.plan_line_id = f.plan_line_id" in " ".join(m._CHAIN_SQL.split())


def test_finished_when_every_card_is_terminal():
    s = m.Scope(1, "X", 70, 9, 1, [{"job_card_id": 1, "job_card_number": "X", "status": "closed"},
                                   {"job_card_id": 2, "job_card_number": "Y", "status": "completed"}])
    assert s.finished is True


# ── changes / detail ──
def test_without_the_table_there_are_no_changes():
    conn = FakeConn(jcbc__table=False)
    assert run(m.load_changes(conn, 1)).is_empty()
    lines, payload = run(m.detail_bom(conn, 3, [line(1, "Seeds")], [], []))
    assert lines == [line(1, "Seeds")] and payload is None


def test_detail_bom_applies_the_chains_changes():
    conn = FakeConn(
        jcbc__table=True,
        jcbc__card={"job_card_id": 3, "job_card_number": "S3", "plan_line_id": 70, "bom_id": 9, "status": "locked"},
        jcbc__head=1,
        jcbc__chain=[{"job_card_id": 1, "job_card_number": "S1", "status": "unlocked"}],
        jcbc__changes=[change(10, "removed", "Seeds")])
    lines, payload = run(m.detail_bom(conn, 3, [line(1, "Seeds"), line(2, "Pouch", "pm")], [], []))
    assert [l["material_sku_name"] for l in lines] == ["Pouch"]
    assert payload["scope_job_card_id"] == 1 and payload["removed"][0]["change_id"] == 10
    assert ("jcbc:changes", (1,)) in conn.calls


# ── records ──
def test_has_records_passes_card_ids_key_and_line_ids():
    hits = [{"kind": "consumption", "qty": Decimal("12.5"), "job_card_id": 1, "job_card_number": "PLAN-7-L1-S1",
             "batch_id": 44, "batch_number": 2, "batch_status": "closed"}]
    conn = FakeConn(jcbc__records=hits)
    scope = m.Scope(1, "PLAN-7-L1-S1", 70, 9, 1, [{"job_card_id": 1, "job_card_number": "S1", "status": "in_progress"}])
    out = run(m.has_records(conn, scope, "SEEDS", [101]))
    assert conn.calls == [("jcbc:records", ([1], "SEEDS", [101]))]
    assert out == [{"kind": "consumption", "qty": 12.5, "job_card_id": 1, "job_card_number": "PLAN-7-L1-S1",
                    "batch_id": 44, "batch_number": 2, "batch_status": "closed"}]


def test_records_sql_ignores_zero_and_soft_deleted_rows_and_counts_issued():
    sql = " ".join(m._RECORDS_SQL.split())
    assert "m.deleted_at IS NULL AND m.actual_consumed_qty > 0" in sql
    assert "b.deleted_at IS NULL AND b.qty_kg > 0" in sql
    assert "y.deleted_at IS NULL AND y.quantity > 0" in sql
    assert "job_card_rm_indent_v2" in sql and "job_card_pm_indent_v2" in sql and "issued_qty > 0" in sql


def test_records_sql_ignores_a_no_batch_row_every_batch_hides():
    # Accounting shows a batch's own row in place of a legacy no-batch twin; once every
    # batch of the card has its own row, the twin shows nowhere and can't be cleared.
    sql = " ".join(m._RECORDS_SQL.split()).replace("( ", "(")
    assert ("AND NOT (m.batch_id IS NULL "
            "AND EXISTS (SELECT 1 FROM job_card_batch_v2 bt WHERE bt.job_card_id = m.job_card_id) "
            "AND NOT EXISTS (SELECT 1 FROM job_card_batch_v2 bt WHERE bt.job_card_id = m.job_card_id "
            "AND NOT EXISTS (SELECT 1 FROM job_card_material_consumption_v2 t "
            "WHERE t.job_card_id = m.job_card_id AND t.batch_id = bt.batch_id "
            "AND (UPPER(BTRIM(t.material_sku_name)) = $2 OR t.bom_line_id = ANY($3::int[])))))") in sql
    assert ("AND NOT (y.batch_id IS NULL "
            "AND EXISTS (SELECT 1 FROM job_card_batch_v2 bt WHERE bt.job_card_id = y.job_card_id) "
            "AND NOT EXISTS (SELECT 1 FROM job_card_batch_v2 bt WHERE bt.job_card_id = y.job_card_id "
            "AND NOT EXISTS (SELECT 1 FROM job_card_byproducts_v2 t "
            "WHERE t.job_card_id = y.job_card_id AND t.batch_id = bt.batch_id AND t.category = y.category "
            "AND (UPPER(BTRIM(t.material_name)) = $2 OR t.bom_line_id = ANY($3::int[])))))") in sql


def test_records_message_names_card_batch_and_what_to_do():
    msg = m.records_message("Seeds", [
        {"kind": "consumption", "qty": 12.5, "job_card_number": "PLAN-7-L1-S1", "batch_id": 44,
         "batch_number": 2, "batch_status": "open"},
        {"kind": "returned", "qty": 1.0, "job_card_number": "PLAN-7-L1-S1", "batch_id": 45,
         "batch_number": 3, "batch_status": "closed"},
        {"kind": "offgrade", "qty": 0.5, "job_card_number": "PLAN-7-L1-S1", "batch_id": None,
         "batch_number": None, "batch_status": None},
        {"kind": "issued", "qty": 12.0, "job_card_number": "PLAN-7-L1-S1", "batch_id": None,
         "batch_number": None, "batch_status": None},
    ])
    assert msg == (
        "Seeds can't be removed yet: consumption is saved on PLAN-7-L1-S1, batch 2; "
        "returned to store is saved on PLAN-7-L1-S1, batch 3 (closed: an admin must re-open it with the override); "
        "off-grade is saved on PLAN-7-L1-S1, saved without a batch; "
        "12 was received on PLAN-7-L1-S1 (return it to store first). "
        "Clear the figures in Accounting (clearing a figure saves 0), then remove it.")


# ── locks ──
def test_lock_sql():
    assert "FOR UPDATE" in m._LOCK_UPDATE_SQL and "production_plan_line_v2" in m._LOCK_UPDATE_SQL
    assert "FOR KEY SHARE" in m._LOCK_SHARE_SQL
    assert "FOR KEY SHARE OF l" in " ".join(m._LOCK_CARD_SHARE_SQL.split())


def test_lock_line_for_card_returns_the_plan_line():
    conn = FakeConn(jcbc__lock_card_share=70)
    assert run(m.lock_line_for_card(conn, 3)) == 70
    assert conn.calls == [("jcbc:lock_card_share", (3,))]


def test_get_job_card_uses_detail_bom(monkeypatch):
    """get_job_card hands its bom_line rows and indents to detail_bom and returns
    what comes back as bom_lines + bom_changes."""
    import inspect
    from app.modules.production.services import job_card_v2 as jcv2
    src = inspect.getsource(jcv2.get_job_card)
    assert "jc_bom_changes" in src and "detail_bom(" in src
    assert re.search(r'"bom_changes":\s+bom_changes_out', src)
    assert re.search(r'"bom_lines":\s+bom_lines_out', src)
