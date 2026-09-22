"""PDF rows, rebuild carry-over and merge refusal (spec 2h, 2i)."""
from __future__ import annotations

import asyncio
import inspect
import re

from app.modules.production.services import jc_bom_changes as m
from app.modules.production.services import job_card_pdf as pdf
from app.modules.production.services import job_card_v2 as jcv2

TAG = re.compile(r"/\* (jcbc:[a-z_]+) \*/")


def test_pdf_rows_drop_removed_rm_and_list_added_rm():
    jc = {"section_2a_rm_indent": [
              {"material_sku_name": "Seeds", "reqd_qty": 10, "issued_qty": 0, "batch_no": "B1", "uom": "KGS"},
              {"material_sku_name": "Old Salt", "reqd_qty": 1, "issued_qty": 0, "batch_no": "B1", "uom": "KGS"}],
          "bom_changes": {"removed": [{"material_sku_name": " old salt "}],
                          "added": [{"material_sku_name": "Sugar", "item_type": "rm", "required_qty": 2.5,
                                     "superseded": False},
                                    {"material_sku_name": "Tape", "item_type": "pm", "required_qty": 5,
                                     "superseded": False},
                                    {"material_sku_name": "Seeds", "item_type": "rm", "required_qty": 1,
                                     "superseded": True}]}}
    rows = pdf.bom_rows(jc)
    assert [r["material_sku_name"] for r in rows] == ["Seeds", "Sugar"]
    assert rows[1] == {"material_sku_name": "Sugar", "reqd_qty": 2.5, "issued_qty": None,
                       "batch_no": "", "uom": "Kgs"}


def test_pdf_lists_every_added_type_but_pm_on_stage_1():
    changes = {"removed": [], "added": [
        {"material_sku_name": "Healthy Choice 100 g", "item_type": "fg", "required_qty": 3, "superseded": False},
        {"material_sku_name": "Roasted Mix", "item_type": "sfg", "required_qty": None, "superseded": False},
        {"material_sku_name": "Tape", "item_type": "pm", "required_qty": 5, "superseded": False}]}
    first = {"job_card_id": 4, "step_number": 1, "prev_job_card_id": None, "section_2a_rm_indent": [],
             "bom_changes": changes}
    rows = pdf.bom_rows(first)
    assert [r["material_sku_name"] for r in rows] == ["Healthy Choice 100 g", "Roasted Mix"]
    assert rows[0] == {"material_sku_name": "Healthy Choice 100 g", "reqd_qty": 3, "issued_qty": None,
                       "batch_no": "", "uom": "Kgs"}
    later = {"job_card_id": 5, "step_number": 2, "prev_job_card_id": 4, "section_2a_rm_indent": [],
             "bom_changes": changes}
    assert pdf.bom_rows(later) == []


def test_pdf_rows_without_changes_are_the_indent_rows():
    jc = {"section_2a_rm_indent": [{"material_sku_name": "Seeds"}]}
    assert pdf.bom_rows(jc) == [{"material_sku_name": "Seeds"}]


def test_pdf_prints_added_rm_only_on_the_stage_that_opens_on_rm():
    # bom_changes covers the whole chain, so every stage card carries it; the added
    # RM belongs on stage 1 only (the web's keepArticle rule), not on the packing card.
    changes = {"removed": [], "added": [{"material_sku_name": "Salt", "item_type": "rm", "required_qty": 2.5,
                                         "superseded": False}]}
    later = {"job_card_id": 5, "step_number": 3, "prev_job_card_id": 4, "section_2a_rm_indent": [],
             "bom_changes": changes}
    assert pdf.bom_rows(later) == []
    member_pkg = {"job_card_id": 8, "step_number": 1, "prev_job_card_id": 4,   # a merged run's packing card
                  "section_2a_rm_indent": [], "bom_changes": changes}
    assert pdf.bom_rows(member_pkg) == []
    first = {"job_card_id": 4, "step_number": 1, "prev_job_card_id": None,
             "section_2a_rm_indent": [{"material_sku_name": "Seeds"}], "bom_changes": changes}
    assert [r["material_sku_name"] for r in pdf.bom_rows(first)] == ["Seeds", "Salt"]


class Conn:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def fetchval(self, sql, *a):
        self.calls.append((TAG.search(sql).group(1), a))
        return True

    async def fetch(self, sql, *a):
        self.calls.append((TAG.search(sql).group(1), a))
        return self.rows

    async def execute(self, sql, *a):
        self.calls.append((TAG.search(sql).group(1), a))
        return "UPDATE 1"


def test_rebuild_repoints_and_undoes_duplicates():
    conn = Conn([{"change_id": 1, "k": "SEEDS"}, {"change_id": 2, "k": "SALT"}, {"change_id": 3, "k": "SEEDS"}])
    assert asyncio.run(m.repoint_after_rebuild(conn, plan_line_id=70, new_head=900, old_heads=[21, 31])) == 2
    tags = [t for t, _ in conn.calls]
    assert tags.index("jcbc:rebuild_undo") < tags.index("jcbc:repoint")
    # Only the changes of chains that were live (their first cards) are read and moved.
    assert ("jcbc:line_changes", (70, [21, 31, 900])) in conn.calls
    assert ("jcbc:rebuild_undo", ([3],)) in conn.calls and ("jcbc:repoint", ([1, 2], 900)) in conn.calls


def test_rebuild_with_no_live_chain_carries_nothing():
    conn = Conn([{"change_id": 1, "k": "SEEDS"}])
    assert asyncio.run(m.repoint_after_rebuild(conn, plan_line_id=70, new_head=900, old_heads=[])) == 0
    assert conn.calls == []


def test_carry_over_sql_is_limited_to_the_given_chains():
    s = " ".join(m._LINE_CHANGES_SQL.split())
    assert "plan_line_id = $1 AND scope_job_card_id = ANY($2::bigint[]) AND undone_at IS NULL" in s
    assert "WHERE change_id = ANY($1::bigint[])" in " ".join(m._REPOINT_SQL.split())


def test_merge_counts_only_changes_of_chains_with_a_live_card():
    # A cancelled chain's changes can't be undone any more (its cards answer 404), so
    # they must not block a merge for good.
    s = " ".join(m._LINES_HELD_SQL.split())
    assert "FROM job_card_v2 WHERE plan_line_id = ANY($1::bigint[]) AND deleted_at IS NULL" in s
    assert "c.scope_job_card_id IN (SELECT job_card_id FROM heads)" in s
    heads = " ".join(m._HEADS_SQL.split())
    assert "FROM job_card_v2 WHERE job_card_id = ANY($1::bigint[])" in heads
    # Both walk back exactly like _HEAD_SQL: same plan line, depth-capped.
    for sql in (s, heads):
        assert "JOIN job_card_v2 p ON p.job_card_id = b.prev_job_card_id" in sql
        assert "WHERE p.plan_line_id = b.plan_line_id AND b.depth < 50" in sql


def test_chain_heads_before_migration_115_is_empty():
    class NoTable(Conn):
        async def fetchval(self, sql, *a):
            self.calls.append((TAG.search(sql).group(1), a))
            return False
    conn = NoTable([{"job_card_id": 1}])
    assert asyncio.run(m.chain_heads(conn, [1, 2])) == []
    assert [t for t, _ in conn.calls] == ["jcbc:table"]
    conn = Conn([{"job_card_id": 1}])
    assert asyncio.run(m.chain_heads(conn, [3, 2])) == [1]
    assert ("jcbc:heads", ([3, 2],)) in conn.calls


class _NullCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ScriptConn:
    """Answers the plain SQL of replace_job_cards_for_line / apply_live_job_card_edits by
    fragment and the jc_bom_changes queries by tag. `log` keeps every statement in order."""

    def __init__(self, cards, heads, changes=()):
        self.cards = {c["job_card_id"]: c for c in cards}
        self.order = [c["job_card_id"] for c in cards]
        self.heads, self.changes = heads, list(changes)
        self.log: list[tuple[str, tuple]] = []

    def _s(self, sql, a):
        s = " ".join(sql.split())
        self.log.append((s, a))
        return s

    async def fetch(self, sql, *a):
        s = self._s(sql, a)
        if "jcbc:heads" in s:
            return [{"job_card_id": h} for h in self.heads]
        if "jcbc:line_changes" in s:
            return self.changes
        if "FROM job_card_v2 WHERE plan_line_id" in s and "deleted_at IS NULL" in s:
            return [self.cards[i] for i in self.order]
        if "FROM job_card_v2 WHERE job_card_id = ANY($1)" in s:
            return [self.cards[i] for i in a[0]]
        if "SELECT step_id FROM production_plan_step_v2" in s:
            return []
        raise AssertionError(s)

    async def fetchrow(self, sql, *a):
        s = self._s(sql, a)
        if "FROM production_plan_line_v2 l JOIN production_plan_v2 p" in s:
            return {"bom_id": 9, "fg_sku_name": "FG", "customer_name": "C", "entity": "cfpl", "warehouse": "W202"}
        raise AssertionError(s)

    async def fetchval(self, sql, *a):
        s = self._s(sql, a)
        if "jcbc:table" in s:
            return True
        if "MAX(step_order)" in s:
            return 3
        if "INSERT INTO production_plan_step_v2" in s:
            return 5001
        if "INSERT INTO job_card_v2" in s:
            self.cards[900] = card(900, 0, "locked", plan_step_id=5001)
            return 900
        raise AssertionError(s)

    async def execute(self, sql, *a):
        self._s(sql, a)
        return "UPDATE 1"

    def transaction(self):
        return _NullCtx()

    def is_in_transaction(self):
        return True

    def index(self, fragment, args=None):
        for i, (s, a) in enumerate(self.log):
            if fragment in s and (args is None or a == args):
                return i
        raise AssertionError(f"{fragment} {args} not run")


def card(jid, step, status, **over):
    row = {"job_card_id": jid, "step_number": step, "process_name": f"P{jid}", "stage": f"p{jid}",
           "floor": "F1", "status": status, "planned_qty_kg": 100, "planned_qty_units": None,
           "output_code": None, "plan_step_id": 5000 + jid, "plan_id": 7}
    row.update(over)
    return row


LIVE_CHANGES = [{"change_id": 61, "k": "SALT"}, {"change_id": 62, "k": "SEEDS"}]


def _edit(conn, steps):
    return asyncio.run(jcv2.apply_live_job_card_edits(conn, 70, qty_kg=100, steps=steps, pkg_floor="F2",
                                                      user="Planner Pat"))


def test_live_edit_locks_the_line_first():
    conn = ScriptConn([card(1, 1, "unlocked"), card(2, 2, "locked"), card(3, 3, "locked")], heads=[1])
    _edit(conn, [{"job_card_id": 1, "process": "P1", "floor": "F1"},
                 {"job_card_id": 2, "process": "P2", "floor": "F1"}])
    first = conn.log[0][0]
    assert "FROM production_plan_line_v2 WHERE plan_line_id = $1 FOR UPDATE" in first and conn.log[0][1] == (70,)


def test_live_edit_removing_stage_1_carries_the_changes_to_the_new_first_card(monkeypatch):
    # S1 Sorting (in progress) -> S2 Roasting (locked) -> PKG; the planner removes Sorting.
    async def _cancel(conn, *, job_card_id, reason, deleted_by):
        return {"removed": True, "snapshot": {}}
    monkeypatch.setattr(jcv2, "_force_record_and_cancel_jc", _cancel)
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked"), card(3, 3, "locked")],
                      heads=[1], changes=LIVE_CHANGES)
    out = _edit(conn, [{"job_card_id": 2, "process": "Roasting", "floor": "F1"}])
    assert out["job_card_ids"] == [2, 3]
    heads_at = conn.index("jcbc:heads", ([1, 2, 3],))
    relink = conn.index("UPDATE job_card_v2 SET step_number=$1, prev_job_card_id=$2")
    assert heads_at < relink < conn.index("jcbc:repoint", ([61, 62], 2))
    assert conn.index("jcbc:line_changes", (70, [1, 2])) > relink


def test_live_edit_inserting_a_step_in_front_carries_the_changes():
    conn = ScriptConn([card(1, 1, "unlocked"), card(2, 2, "locked"), card(3, 3, "locked")],
                      heads=[1], changes=LIVE_CHANGES)
    out = _edit(conn, [{"job_card_id": None, "process": "Cleaning", "floor": "F1"},
                       {"job_card_id": 1, "process": "P1", "floor": "F1"},
                       {"job_card_id": 2, "process": "P2", "floor": "F1"}])
    assert out["job_card_ids"] == [900, 1, 2, 3]
    conn.index("jcbc:repoint", ([61, 62], 900))


def test_live_edit_keeping_the_first_card_moves_nothing():
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked"), card(3, 3, "locked")],
                      heads=[1], changes=LIVE_CHANGES)
    _edit(conn, [{"job_card_id": 1, "process": "P1", "floor": "F1"},
                 {"job_card_id": 2, "process": "P2", "floor": "F9"}])
    assert not any("jcbc:line_changes" in s or "jcbc:repoint" in s for s, _ in conn.log)


def test_replace_locks_the_line_first_and_repoints():
    src = inspect.getsource(jcv2.replace_job_cards_for_line)
    body = src[src.index('"""', src.index('"""') + 3) + 3:]           # after the docstring
    first_await = body.index("await ")
    assert body[first_await:].startswith("await conn.execute(") and "FOR UPDATE" in body[first_await:first_await + 200]
    assert "repoint_after_rebuild(" in src


def test_replace_carries_only_the_chains_that_were_live(monkeypatch):
    async def _create(conn, plan_line_id, **kw):
        return {"job_card_ids": [900, 901]}
    monkeypatch.setattr(jcv2, "create_job_cards_for_line", _create)
    conn = ScriptConn([card(21, 1, "unlocked"), card(22, 2, "locked")], heads=[21], changes=LIVE_CHANGES)
    out = asyncio.run(jcv2.replace_job_cards_for_line(conn, 70, qty_kg=100, wip_steps=[{"process": "P"}],
                                                      pkg_floor="F2"))
    assert out["bom_changes_carried"] == 2
    # Heads are read before the cards are deleted; a cancelled chain's head is not
    # among them, so its changes stay where they are.
    assert conn.index("jcbc:heads", ([21, 22],)) < conn.index("DELETE FROM job_card_v2")
    conn.index("jcbc:line_changes", (70, [21, 900]))
    conn.index("jcbc:repoint", ([61, 62], 900))


def test_merge_is_refused_while_lines_hold_changes():
    src = inspect.getsource(jcv2.create_merged_process_run)
    assert "lines_with_live_changes(" in src and '"bom_changes_on_merged_lines"' in src
    assert src.index("FOR UPDATE OF l") < src.index("lines_with_live_changes(") < src.index("DELETE FROM job_card_v2")
