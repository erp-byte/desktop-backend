"""jc_bom_changes writes + the bom-changes routes (spec 2c)."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.production import router as PR
from app.modules.production.services import jc_bom_changes as m

TAG = re.compile(r"/\* (jcbc:[a-z_]+) \*/")
WHEN = datetime(2026, 9, 21, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self, **answers):
        self.answers = {"jcbc:table": True, "jcbc:lock_update": 70, "jcbc:head": 1,
                        "jcbc:undo": "UPDATE 1", "jcbc:insert": 999, "jcbc:req_table": True,
                        "jcbc:open_reqs": [], "jcbc:records": [], "jcbc:indents": []}
        self.answers.update({k.replace("__", ":"): v for k, v in answers.items()})
        self.calls: list[tuple[str, tuple]] = []

    def _answer(self, sql, args):
        tag = TAG.search(sql).group(1)
        self.calls.append((tag, args))
        assert tag in self.answers, f"unexpected query {tag}"
        v = self.answers[tag]
        return v(*args) if callable(v) else v

    fetch = fetchrow = fetchval = execute = lambda self, sql, *a: _aw(self._answer(sql, a))

    def transaction(self):
        return _NullCtx()

    def is_in_transaction(self):
        # insert_with_pk_retry refuses to run outside an outer transaction.
        return True

    def tags(self):
        return [t for t, _ in self.calls]


async def _aw(v):
    return v


class _NullCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


CARD = {"job_card_id": 3, "job_card_number": "PLAN-7-L1-S3", "plan_line_id": 70, "bom_id": 9, "status": "locked"}
CHAIN = [{"job_card_id": 1, "job_card_number": "PLAN-7-L1-S1", "status": "unlocked"},
         {"job_card_id": 3, "job_card_number": "PLAN-7-L1-S3", "status": "locked"}]
MASTER = [{"bom_line_id": 101, "line_number": 1, "material_sku_name": "Seeds", "item_type": "rm", "uom": "KGS",
           "quantity_per_unit": Decimal("0.5"), "loss_pct": Decimal("1"), "godown": None},
          {"bom_line_id": 102, "line_number": 2, "material_sku_name": "Pouch", "item_type": "pm", "uom": "NOS",
           "quantity_per_unit": Decimal("1"), "loss_pct": None, "godown": None},
          {"bom_line_id": 103, "line_number": 3, "material_sku_name": "SFG0001", "item_type": "sfg", "uom": "KGS",
           "quantity_per_unit": Decimal("1"), "loss_pct": None, "godown": None}]


def conn_with(changes=(), **over):
    # `over` may replace the card / chain / master defaults, so merge, not re-pass.
    answers = dict(jcbc__card=CARD, jcbc__chain=CHAIN, jcbc__master=MASTER, jcbc__changes=list(changes))
    answers.update(over)
    return FakeConn(**answers)


def change(cid, ctype, name, itype="rm", **over):
    row = {"change_id": cid, "change_type": ctype, "material_sku_name": name, "item_type": itype,
           "sku_id": 55 if ctype == "added" else None, "required_qty": None, "required_unit": None,
           "note": None, "made_on_job_card_id": 1, "made_on_job_card_number": "PLAN-7-L1-S1",
           "changed_by": "Pat", "changed_at": WHEN}
    row.update(over)
    return row


def run(coro):
    return asyncio.run(coro)


def refused(coro) -> m.BomChangeError:
    with pytest.raises(m.BomChangeError) as e:
        run(coro)
    return e.value


# ── remove ──
def test_remove_a_bom_line_inserts_a_removed_row_scoped_to_the_chains_first_card():
    conn = conn_with()
    out = run(m.remove_article(conn, actor="Pat", job_card_id=3, material_sku_name=" seeds ", note=" not used "))
    ins = [a for t, a in conn.calls if t == "jcbc:insert"][0]
    # change_id, scope, plan_line, type, name, item_type, sku_id, qty, unit, note, made_on id, made_on number, actor
    assert ins[1:] == (1, 70, "removed", "Seeds", "rm", None, None, None, "not used", 3, "PLAN-7-L1-S3", "Pat")
    assert out["action"] == "removed" and out["restored"] is False
    # The plan line is locked before anything else is read about the chain.
    tags = conn.tags()
    assert tags.index("jcbc:lock_update") < tags.index("jcbc:head")


def test_remove_refuses_an_article_not_on_the_list_and_sfg_lines():
    assert refused(m.remove_article(conn_with(), actor="P", job_card_id=3, material_sku_name="Salt")).code \
        == "article_not_on_job_card"
    e = refused(m.remove_article(conn_with(), actor="P", job_card_id=3, material_sku_name="SFG0001"))
    assert (e.http_status, e.code) == (422, "not_rm_or_pm")


def test_remove_refuses_while_figures_are_saved():
    hits = [{"kind": "consumption", "qty": Decimal("3"), "job_card_id": 1, "job_card_number": "PLAN-7-L1-S1",
             "batch_id": 4, "batch_number": 1, "batch_status": "open"}]
    conn = conn_with(jcbc__records=hits)
    e = refused(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="Seeds"))
    assert (e.http_status, e.code) == (409, "article_has_records")
    assert e.details["hits"][0]["job_card_number"] == "PLAN-7-L1-S1"
    assert ("jcbc:records", ([1, 3], "SEEDS", [101])) in conn.calls
    assert "jcbc:insert" not in conn.tags()


def test_remove_an_added_article_undoes_its_add():
    conn = conn_with([change(11, "added", "Salt")])
    out = run(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="salt"))
    assert ("jcbc:undo", (11, "P")) in conn.calls and "jcbc:insert" not in conn.tags()
    assert out["action"] == "add_undone"


def test_remove_an_added_sfg_article_undoes_its_add():
    # Only BOM-module lines are limited to RM/PM; an added article of any type can go.
    conn = conn_with([change(12, "added", "Roasted Mix", "sfg")])
    out = run(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name=" roasted mix "))
    assert ("jcbc:undo", (12, "P")) in conn.calls and "jcbc:insert" not in conn.tags()
    assert out["action"] == "add_undone"


def test_remove_a_superseded_add_undoes_it_then_removes_the_bom_line():
    conn = conn_with([change(11, "added", "seeds")])
    run(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="Seeds"))
    tags = conn.tags()
    assert tags.index("jcbc:undo") < tags.index("jcbc:insert")


def test_remove_reports_open_requests_and_leaves_them():
    conn = conn_with(jcbc__open_reqs=[{"requisition_id": 27385955}])
    out = run(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="Pouch"))
    assert out["open_requisition_ids"] == [27385955]
    assert ("jcbc:open_reqs", ([1, 3], "POUCH")) in conn.calls


def test_nothing_changes_once_the_job_card_is_finished():
    chain = [dict(c, status="completed") for c in CHAIN]
    e = refused(m.remove_article(conn_with(jcbc__chain=chain), actor="P", job_card_id=3, material_sku_name="Seeds"))
    assert (e.http_status, e.code) == (409, "job_card_finished")


def test_a_stage_waiting_for_the_previous_one_is_not_refused():
    # CARD.status is 'locked' (awaiting_previous_stage): the change goes through.
    run(m.remove_article(conn_with(), actor="P", job_card_id=3, material_sku_name="Seeds"))


def test_before_migration_115_writes_answer_503():
    e = refused(m.remove_article(conn_with(jcbc__table=False), actor="P", job_card_id=3, material_sku_name="Seeds"))
    assert (e.http_status, e.code) == (503, "bom_changes_not_available")


def test_missing_card_is_404():
    e = refused(m.remove_article(conn_with(jcbc__card=None), actor="P", job_card_id=3, material_sku_name="Seeds"))
    assert (e.http_status, e.code) == (404, "job_card_not_found")


# ── add ──
SKU_SALT = {"sku_id": 55, "particulars": "  Salt ", "item_type": "RM"}


def test_add_inserts_an_added_row_with_the_required_qty():
    conn = conn_with(jcbc__sku=SKU_SALT)
    out = run(m.add_article(conn, actor="P", job_card_id=3, sku_id=55, required_qty="2.5"))
    ins = [a for t, a in conn.calls if t == "jcbc:insert"][0]
    assert ins[3:10] == ("added", "Salt", "rm", 55, Decimal("2.5"), "kg", None)
    assert out["action"] == "added"


def test_add_an_fg_by_sku_id_stores_its_real_type_in_kg():
    fg = {"sku_id": 2, "particulars": "Healthy Choice 100 g", "item_type": "FG"}
    conn = conn_with(jcbc__sku=fg)
    out = run(m.add_article(conn, actor="P", job_card_id=3, sku_id=2, required_qty="3.25"))
    ins = [a for t, a in conn.calls if t == "jcbc:insert"][0]
    assert ins[3:10] == ("added", "Healthy Choice 100 g", "fg", 2, Decimal("3.25"), "kg", None)
    assert out["action"] == "added"


def test_add_refuses_unknown_skus_and_other_types():
    e = refused(m.add_article(conn_with(jcbc__sku=None), actor="P", job_card_id=3, sku_id=1))
    assert (e.http_status, e.code) == (404, "sku_not_found")
    ega = {"sku_id": 2, "particulars": "Carton Tape", "item_type": "pl/ega"}
    conn = conn_with(jcbc__sku=ega)
    e = refused(m.add_article(conn, actor="P", job_card_id=3, sku_id=2))
    assert (e.http_status, e.code) == (422, "type_not_addable")
    assert e.message == "Carton Tape is PL/EGA; only RM, PM, FG and SFG articles can be added."
    assert "jcbc:insert" not in conn.tags()


# ── add by name (Use on other floor stock) ──
PAIR = [{"sku_id": 31, "particulars": "Healthy Choice 100 g", "item_type": "FG"},
        {"sku_id": 32, "particulars": "Healthy Choice 100 g ", "item_type": "sfg"}]


def _add_by_name(rows, name, hint=None, changes=(), **over):
    conn = conn_with(changes, jcbc__sku_by_name=rows, **over)
    out = run(m.add_article(conn, actor="P", job_card_id=3, material_sku_name=name, item_type_hint=hint))
    return conn, out


def _inserted(conn):
    return [a for t, a in conn.calls if t == "jcbc:insert"][0]


def test_add_by_name_looks_the_sku_up_by_its_article_key():
    conn, out = _add_by_name(PAIR, "  healthy choice 100 g ", "fg")
    assert ("jcbc:sku_by_name", ("HEALTHY CHOICE 100 G",)) in conn.calls
    assert "jcbc:sku" not in conn.tags()
    assert _inserted(conn)[3:7] == ("added", "Healthy Choice 100 g", "fg", 31)
    assert out["action"] == "added"


def test_add_by_name_the_hint_picks_between_an_fg_and_an_sfg():
    conn, _ = _add_by_name(PAIR, "Healthy Choice 100 g", " SFG ")
    assert _inserted(conn)[3:7] == ("added", "Healthy Choice 100 g", "sfg", 32)


def test_add_by_name_without_a_hint_prefers_rm_pm_sfg_fg():
    rows = [{"sku_id": 40, "particulars": "Mix", "item_type": "fg"},
            {"sku_id": 41, "particulars": "Mix", "item_type": "sfg"},
            {"sku_id": 42, "particulars": "Mix", "item_type": "pl/ega"}]
    conn, _ = _add_by_name(rows, "Mix")
    assert _inserted(conn)[5:7] == ("sfg", 41)
    conn, _ = _add_by_name(rows + [{"sku_id": 43, "particulars": "Mix", "item_type": "pm"}], "Mix", "pl/ega")
    assert _inserted(conn)[5:7] == ("pm", 43)       # a hint that is not addable does not pick
    conn, _ = _add_by_name([{"sku_id": 51, "particulars": "Mix", "item_type": "sfg"},
                            {"sku_id": 50, "particulars": "Mix", "item_type": "sfg"}], "Mix")
    assert _inserted(conn)[5:7] == ("sfg", 50)      # same type: then by sku_id


def test_add_by_name_refusals():
    e = refused(m.add_article(conn_with(jcbc__sku_by_name=[]), actor="P", job_card_id=3,
                              material_sku_name=" Unknown Thing "))
    assert (e.http_status, e.code) == (404, "sku_not_found")
    assert e.message == "Unknown Thing is not in the SKU master."
    ega = [{"sku_id": 60, "particulars": "Carton Tape", "item_type": "PL/EGA"}]
    conn = conn_with(jcbc__sku_by_name=ega)
    e = refused(m.add_article(conn, actor="P", job_card_id=3, material_sku_name="carton tape"))
    assert (e.http_status, e.code) == (422, "type_not_addable")
    assert e.message == "Carton Tape is PL/EGA; only RM, PM, FG and SFG articles can be added."
    assert "jcbc:insert" not in conn.tags()


LINER = [{"sku_id": 77, "particulars": "Poly Liner", "item_type": "pm"}]


def test_add_by_name_refuses_a_required_qty_the_sku_masters_type_would_put_in_another_unit():
    # Use asks for the qty in the floor row's unit (kg for an RM row). The only SKU
    # of that name is PM (pcs): storing 3 as 3 pcs would change the unit silently.
    conn = conn_with(jcbc__sku_by_name=LINER)
    e = refused(m.add_article(conn, actor="P", job_card_id=3, material_sku_name="Poly Liner",
                              item_type_hint="rm", required_qty=3))
    assert (e.http_status, e.code) == (409, "type_mismatch")
    assert e.message == ("Poly Liner is PM in the SKU master, not RM, so its required qty is in pcs, "
                         "not kg. Leave Required qty empty, or add it with + Add article.")
    assert e.details == {"sku_item_type": "pm", "required_unit": "pcs"}
    assert "jcbc:insert" not in conn.tags()
    # A fraction is the same refusal, not "Pieces are whole numbers."
    e = refused(m.add_article(conn_with(jcbc__sku_by_name=LINER), actor="P", job_card_id=3,
                              material_sku_name="Poly Liner", item_type_hint=" RM ", required_qty="2.5"))
    assert e.code == "type_mismatch"
    # And the other way round: a PM row whose only SKU is RM.
    salt = [{"sku_id": 55, "particulars": "Salt", "item_type": "RM"}]
    e = refused(m.add_article(conn_with(jcbc__sku_by_name=salt), actor="P", job_card_id=3,
                              material_sku_name="Salt", item_type_hint="pm", required_qty=100))
    assert e.message == ("Salt is RM in the SKU master, not PM, so its required qty is in kg, "
                         "not pcs. Leave Required qty empty, or add it with + Add article.")


def test_add_by_name_without_a_required_qty_adds_the_sku_masters_type():
    # No qty, no unit to get wrong: the SKU master's type is added.
    conn, out = _add_by_name(LINER, "Poly Liner", "rm")
    assert _inserted(conn)[3:10] == ("added", "Poly Liner", "pm", 77, None, None, None)
    assert out["action"] == "added"


def test_add_by_name_a_fallback_of_the_same_unit_keeps_the_required_qty():
    # RM, FG and SFG are all kg: an SFG row whose only SKU is FG adds the FG in kg.
    conn = conn_with(jcbc__sku_by_name=PAIR[:1])
    run(m.add_article(conn, actor="P", job_card_id=3, material_sku_name="Healthy Choice 100 g",
                      item_type_hint="sfg", required_qty="1.5"))
    assert _inserted(conn)[3:10] == ("added", "Healthy Choice 100 g", "fg", 31, Decimal("1.5"), "kg", None)


def test_using_a_removed_bom_article_by_name_restores_it():
    conn, out = _add_by_name([{"sku_id": 9, "particulars": "Seeds", "item_type": "RM"}], " seeds ", "rm",
                             changes=[change(10, "removed", "Seeds")])
    assert ("jcbc:undo", (10, "P")) in conn.calls and "jcbc:insert" not in conn.tags()
    assert out["restored"] is True and out["action"] == "restored"


def test_sku_by_name_sql():
    s = " ".join(m._SKU_BY_NAME_SQL.split())
    assert s == ("/* jcbc:sku_by_name */ SELECT sku_id, particulars, item_type FROM all_sku "
                 "WHERE UPPER(BTRIM(particulars)) = $1 ORDER BY sku_id")
    assert m._TYPE_ORDER == ("rm", "pm", "sfg", "fg")


def test_add_refuses_an_article_already_on_the_list_even_with_odd_spaces():
    dup = {"sku_id": 9, "particulars": "Seeds", "item_type": "rm"}
    e = refused(m.add_article(conn_with(jcbc__sku=dup), actor="P", job_card_id=3, sku_id=9))
    assert (e.code, e.details["bom_spelling"]) == ("already_on_job_card", "Seeds")
    master = [dict(MASTER[0], material_sku_name="Roasted  Seeds")]
    nbsp = {"sku_id": 9, "particulars": "Roasted Seeds", "item_type": "rm"}
    e = refused(m.add_article(conn_with(jcbc__sku=nbsp, jcbc__master=master), actor="P", job_card_id=3, sku_id=9))
    assert e.details["bom_spelling"] == "Roasted  Seeds"


def test_adding_a_removed_bom_article_restores_it():
    conn = conn_with([change(10, "removed", "Seeds")], jcbc__sku={"sku_id": 9, "particulars": "Seeds", "item_type": "rm"})
    out = run(m.add_article(conn, actor="P", job_card_id=3, sku_id=9, required_qty="4"))
    assert ("jcbc:undo", (10, "P")) in conn.calls and "jcbc:insert" not in conn.tags()
    assert out["restored"] is True and out["action"] == "restored"


def test_adding_back_a_removed_article_the_bom_no_longer_has_adds_it():
    # Seeds was removed, then a Tally refresh dropped it from bom_line (not_on_bom):
    # there is nothing to restore, so the stale removal is undone and Seeds is added
    # with its required qty, instead of answering 'restored' with nothing added.
    conn = conn_with([change(10, "removed", "Seeds")], jcbc__master=MASTER[1:],
                     jcbc__sku={"sku_id": 9, "particulars": "Seeds", "item_type": "rm"})
    out = run(m.add_article(conn, actor="P", job_card_id=3, sku_id=9, required_qty="4"))
    tags = conn.tags()
    assert ("jcbc:undo", (10, "P")) in conn.calls
    assert tags.index("jcbc:undo") < tags.index("jcbc:insert")
    ins = [a for t, a in conn.calls if t == "jcbc:insert"][0]
    assert ins[3:9] == ("added", "Seeds", "rm", 9, Decimal("4"), "kg")
    assert out["action"] == "added" and out["restored"] is False


def test_adding_back_a_stale_removal_still_checks_the_required_qty_first():
    conn = conn_with([change(10, "removed", "Seeds")], jcbc__master=MASTER[1:],
                     jcbc__sku={"sku_id": 9, "particulars": "Seeds", "item_type": "rm"})
    assert refused(m.add_article(conn, actor="P", job_card_id=3, sku_id=9, required_qty="-1")).code \
        == "required_qty_invalid"
    assert "jcbc:undo" not in conn.tags() and "jcbc:insert" not in conn.tags()


@pytest.mark.parametrize("value,itype,code_or_result", [
    (None, "rm", (None, None)), ("", "pm", (None, None)), ("2.500", "rm", (Decimal("2.500"), "kg")),
    ("100", "pm", (Decimal("100"), "pcs")), ("0", "rm", "required_qty_invalid"),
    ("-1", "rm", "required_qty_invalid"), ("1.5", "pm", "required_qty_invalid"),
    ("1.2345", "rm", "required_qty_invalid"), ("abc", "rm", "required_qty_invalid"),
    (True, "rm", "required_qty_invalid"), ("1e12", "rm", "required_qty_invalid"),
])
def test_parse_required(value, itype, code_or_result):
    if isinstance(code_or_result, str):
        with pytest.raises(m.BomChangeError) as e:
            m.parse_required(value, itype)
        assert e.value.code == code_or_result
    else:
        assert m.parse_required(value, itype) == code_or_result


# ── undo ──
def test_undo_restores_a_removed_article():
    conn = conn_with([change(10, "removed", "Seeds")],
                     jcbc__change={"change_id": 10, "change_type": "removed", "material_sku_name": "Seeds",
                                   "undone_at": None})
    out = run(m.undo_change(conn, actor="P", job_card_id=3, change_id=10))
    assert ("jcbc:change", (10, 1)) in conn.calls and out["restored"] is True


def test_undo_of_an_added_article_checks_records():
    hits = [{"kind": "offgrade", "qty": Decimal("1"), "job_card_id": 3, "job_card_number": "S3",
             "batch_id": 7, "batch_number": 1, "batch_status": "open"}]
    conn = conn_with([change(11, "added", "Salt")], jcbc__records=hits,
                     jcbc__change={"change_id": 11, "change_type": "added", "material_sku_name": "Salt",
                                   "undone_at": None})
    assert refused(m.undo_change(conn, actor="P", job_card_id=3, change_id=11)).code == "article_has_records"


def test_undo_refusals():
    assert refused(m.undo_change(conn_with(jcbc__change=None), actor="P", job_card_id=3, change_id=5)).code \
        == "change_not_found"
    done = {"change_id": 5, "change_type": "removed", "material_sku_name": "Seeds", "undone_at": WHEN}
    assert refused(m.undo_change(conn_with(jcbc__change=done), actor="P", job_card_id=3, change_id=5)).code \
        == "change_already_undone"


def test_a_lost_race_on_the_live_index_is_409():
    class Clash(Exception):
        constraint_name = m.LIVE_INDEX

    import asyncpg

    def boom(*a):
        raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint "
                                           f'"{m.LIVE_INDEX}"')
    conn = conn_with(jcbc__insert=boom)
    assert refused(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="Seeds")).code \
        == "bom_changed_concurrently"


@pytest.mark.parametrize("constraint", ["chk_jcbc_item_type", "chk_jcbc_required"])
def test_before_migration_116_an_fg_add_answers_503(constraint):
    import asyncpg

    def boom(*a):
        raise asyncpg.CheckViolationError('new row for relation "job_card_bom_change" violates check '
                                          f'constraint "{constraint}"')
    conn = conn_with(jcbc__insert=boom, jcbc__sku={"sku_id": 2, "particulars": "Healthy Choice", "item_type": "fg"})
    e = refused(m.add_article(conn, actor="P", job_card_id=3, sku_id=2, required_qty="1"))
    assert (e.http_status, e.code) == (503, "bom_changes_need_116")
    assert e.message == ("Adding FG or SFG articles needs database migration 116, "
                         "which has not been applied yet.")


def test_other_check_violations_are_not_mapped_to_116():
    import asyncpg

    def boom(*a):
        raise asyncpg.CheckViolationError('new row for relation "job_card_bom_change" violates check '
                                          'constraint "chk_jcbc_required_pair"')
    conn = conn_with(jcbc__insert=boom, jcbc__sku=SKU_SALT)
    with pytest.raises(asyncpg.CheckViolationError):
        run(m.add_article(conn, actor="P", job_card_id=3, sku_id=55))


# ── routes ──
class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *exc):
        return False


def _request(conn):
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))


USER = SimpleNamespace(full_name="Planner Pat", email=None, phone=None, user_id=7)


def test_routes_exist_with_the_job_card_edit_permission():
    paths = {(r.path, tuple(sorted(r.methods))) for r in PR.router.routes if hasattr(r, "methods")}
    assert any(p.endswith("/job-cards-v2/{job_card_id}/bom-changes") and "POST" in ms for p, ms in paths)
    assert any(p.endswith("/job-cards-v2/{job_card_id}/bom-changes/{change_id}") and "DELETE" in ms
               for p, ms in paths)


def test_route_maps_a_refusal_to_its_status(monkeypatch):
    async def refuse(conn, **kw):
        raise m.BomChangeError(409, "article_has_records", "Seeds can't be removed yet", hits=[])
    monkeypatch.setattr(m, "remove_article", refuse)
    conn = FakeConn()
    with pytest.raises(HTTPException) as e:
        run(PR.create_bom_change(_request(conn), 3, PR.BomChangeBody(action="remove", material_sku_name="Seeds"),
                                 user=USER))
    assert e.value.status_code == 409
    assert e.value.detail == {"error": "article_has_records", "message": "Seeds can't be removed yet", "hits": []}


def test_route_add_passes_the_fields(monkeypatch):
    seen = {}

    async def add(conn, **kw):
        seen.update(kw)
        return {"action": "added"}
    monkeypatch.setattr(m, "add_article", add)
    out = run(PR.create_bom_change(_request(FakeConn()), 3,
                                   PR.BomChangeBody(action="add", sku_id=55, material_sku_name="Salt",
                                                    required_qty="2.5", note="x"), user=USER))
    assert out == {"action": "added"}
    # A sku_id wins: the name is not passed on.
    assert seen == {"actor": "Planner Pat", "job_card_id": 3, "sku_id": 55, "material_sku_name": None,
                    "item_type_hint": None, "required_qty": "2.5", "note": "x"}


def test_route_add_by_name_passes_the_name_and_type_hint(monkeypatch):
    seen = {}

    async def add(conn, **kw):
        seen.update(kw)
        return {"action": "added"}
    monkeypatch.setattr(m, "add_article", add)
    run(PR.create_bom_change(_request(FakeConn()), 3,
                             PR.BomChangeBody(action="add", material_sku_name="Healthy Choice 100 g",
                                              item_type="FG", required_qty=None), user=USER))
    assert seen == {"actor": "Planner Pat", "job_card_id": 3, "sku_id": None,
                    "material_sku_name": "Healthy Choice 100 g", "item_type_hint": "FG",
                    "required_qty": None, "note": None}


def test_route_add_needs_a_sku_and_remove_needs_a_name():
    with pytest.raises(HTTPException) as e:
        run(PR.create_bom_change(_request(FakeConn()), 3, PR.BomChangeBody(action="add"), user=USER))
    assert e.value.status_code == 422 and e.value.detail["error"] == "sku_required"
    with pytest.raises(HTTPException) as e:
        run(PR.create_bom_change(_request(FakeConn()), 3,
                                 PR.BomChangeBody(action="add", material_sku_name="   ", item_type="fg"), user=USER))
    assert e.value.status_code == 422 and e.value.detail["error"] == "sku_required"
    with pytest.raises(HTTPException) as e:
        run(PR.create_bom_change(_request(FakeConn()), 3, PR.BomChangeBody(action="remove"), user=USER))
    assert e.value.status_code == 422
