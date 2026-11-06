"""Edit Job Card: one-card chains, and every refusal before the first write
(agreed with the user 2026-10-27).

Before:
  * a chain carded as ONE process (raw material straight to finished goods) could
    be created but not edited: the pop-up needed two rows, and the server refused
    an edit with no WIP step (no_wip_steps) after it had already written the qty,
    the SO move, removals and additions;
  * Add process in front of a card that already had its material (packing
    released by the last stage's dispatch, or a started one-card chain) made a
    card that never unlocked, or a second raw-material card beside the running one;
  * a part chain's qty edit set the whole plan line to the chain's qty, and after
    stage 1 finished the next edit diffed from its stale qty and moved the SO again;
  * executed plans stayed editable; added cards reused ADD numbers (UNIQUE: a 500);
    removing a running first stage stranded its material on a cancelled card;
  * replace hard-deleted cancelled cards, and 500'd once the floor had asked for
    material.
Services are scripted (ScriptConn); no database.
"""
from __future__ import annotations

import asyncio

import pytest
from asyncpg.exceptions import ForeignKeyViolationError

from app.modules.production.services import job_card_v2 as jcv2
from app.modules.production.services.refusals import ServiceRefusal, refusal_status
from tests.services.test_bom_changes_pdf_rebuild import ScriptConn, card

try:
    import pglast
except ImportError:  # pragma: no cover - exercised only on a box without pglast
    pglast = None

requires_pglast = pytest.mark.skipif(pglast is None, reason="pglast gives real PostgreSQL grammar validation")


def writes(conn):
    return [s for s, _ in conn.log if s.split()[0] in ("UPDATE", "INSERT", "DELETE")]


def args_of(conn, fragment):
    return [a for s, a in conn.log if fragment in s]


@pytest.fixture
def so_sync(monkeypatch):
    """The SO move, faked: records each delta and how many writes came before it."""
    calls = []

    async def sync(conn, plan_line_id, *, delta_kg, delta_units, user=None, reason=None):
        calls.append({"kg": delta_kg, "units": delta_units, "writes_before": len(writes(conn))})
        return {"synced": True, "delta_kg": delta_kg}
    monkeypatch.setattr(jcv2, "sync_so_from_qty_delta", sync)
    return calls


@pytest.fixture
def removed(monkeypatch):
    calls = []

    async def cancel(conn, *, job_card_id, reason, deleted_by):
        calls.append(job_card_id)
        return {"removed": True, "snapshot": {}}
    monkeypatch.setattr(jcv2, "_force_record_and_cancel_jc", cancel)
    return calls


def edit(conn, steps, *, qty_kg=100, qty_units=None, pkg_floor="F1", pkg_job_card_id=None, reasons=None):
    return asyncio.run(jcv2.apply_live_job_card_edits(
        conn, 70, qty_kg=qty_kg, qty_units=qty_units, steps=steps, pkg_floor=pkg_floor,
        pkg_job_card_id=pkg_job_card_id, user="Planner Pat", remove_reasons=reasons))


def step(jid, floor="F1", process=None):
    return {"job_card_id": jid, "process": process or (f"P{jid}" if jid else "Sorting"), "floor": floor}


def one_card(status="in_progress", **over):
    """A one-card chain: its only card goes from raw material to finished goods."""
    return [card(1, 1, status, process_name="Sorting + Packing", stage="sorting", **over)]


# ── one-card chains ────────────────────────────────────────────────────────

def test_a_running_one_card_chain_takes_a_quantity_change(so_sync):
    conn = ScriptConn(one_card(), heads=[1], line={"planned_qty_kg": 100, "planned_qty_units": 10})
    out = edit(conn, [], qty_kg=110, pkg_job_card_id=1)
    assert out["job_card_ids"] == [1] and out["created_job_card_ids"] == [] and out["removed"] == 0
    assert out["qty_changed"] is True
    assert [(c["kg"], c["units"]) for c in so_sync] == [(10.0, 0.0)]
    assert args_of(conn, "UPDATE job_card_v2 SET planned_qty_kg=$1") == [(110.0, None, "Planner Pat", 70)]
    # The line moves by the change (not to the chain's qty).
    assert args_of(conn, "SET planned_qty_kg = planned_qty_kg + $1") == [(10.0, 0.0, 70)]
    assert not any(s.startswith("INSERT INTO job_card_v2") for s in writes(conn))
    # Its card is both first and last: no previous or next stage.
    relink = args_of(conn, "UPDATE job_card_v2 SET step_number=$1, prev_job_card_id=$2")
    assert relink == [(1, None, None, "F1", "Planner Pat", 1)]
    log = [a for s, a in conn.log if s.startswith("INSERT INTO job_card_edit_log_v2")]
    assert [a[4] for a in log] == ["qty_change"]


def test_a_running_one_card_chain_moves_floor(so_sync):
    conn = ScriptConn(one_card(), heads=[1])
    out = edit(conn, [], pkg_floor="F2", pkg_job_card_id=1)
    assert out["floors_changed"] == 1 and so_sync == []


@pytest.mark.parametrize("status", ["material_received", "in_progress"])
def test_a_process_cant_go_before_a_one_card_chain_that_was_released(so_sync, status):
    """Its card already holds the raw material: the new card would draw it again.
    (Released but nothing received: test_review_fixes_be2 — the refusal says so.)"""
    conn = ScriptConn(one_card(status), heads=[1])
    out = edit(conn, [step(None)], qty_kg=120, pkg_job_card_id=1)
    assert out["error"] == "cannot_add_before_released" and refusal_status(out["error"]) == 409
    assert out["message"] == ("Sorting + Packing (job card PLAN-7-L70-S1) has already received "
                              "material — a process can't be added before it.")
    assert writes(conn) == [] and so_sync == []


@pytest.mark.parametrize("pkg_status", ["unlocked", "material_received", "in_progress"])
def test_a_process_cant_go_before_packing_once_the_last_stage_sent_it_material(so_sync, pkg_status):
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, pkg_status)], heads=[1])
    out = edit(conn, [step(1), step(None)], pkg_job_card_id=2)
    assert out["error"] == "cannot_add_before_released" and out["job_card_id"] == 2
    assert writes(conn) == []


def test_a_process_still_goes_before_packing_that_waits_and_gets_a_fresh_add_number(so_sync):
    # ADD1 is live on the line, ADD2 was cancelled: the next two are ADD3 and ADD4.
    conn = ScriptConn([card(1, 1, "in_progress"), card(4, 2, "locked", job_card_number="PLAN-7-L70-ADD1"),
                       card(3, 3, "locked")], heads=[1], cancelled_numbers=["PLAN-7-L70-ADD2"])
    out = edit(conn, [step(1), step(4), step(None, process="Cleaning"), step(None, process="Grading")],
               pkg_job_card_id=3)
    assert out["job_card_ids"] == [1, 4, 900, 901, 3] and out["created_job_card_ids"] == [900, 901]
    numbers = [a[1] for s, a in conn.log if s.startswith("INSERT INTO job_card_v2")]
    assert numbers == ["PLAN-7-L70-ADD3", "PLAN-7-L70-ADD4"]


def test_next_add_no_is_past_every_add_number():
    assert jcv2.next_add_no([]) == 1
    assert jcv2.next_add_no(["PLAN-1-L2-S1", "PLAN-1-L2-S2-B2"]) == 1
    assert jcv2.next_add_no(["PLAN-1-L2-ADD2", "PLAN-1-L2-ADD10", "PLAN-1-L2-ADD3", None]) == 11


# ── the chain the screen sends must be this line's ─────────────────────────

def test_removing_the_packing_row_is_refused(so_sync):
    """[Stage 1 running, Packing]: with packing's row removed the screen sends stage 1
    as packaging — the server cancelled the running stage and renamed packing."""
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [], pkg_job_card_id=1)
    assert out["error"] == "terminal_mismatch" and refusal_status(out["error"]) == 409
    assert out["message"].startswith("P2 (job card PLAN-7-L70-S2) must stay the last process")
    assert writes(conn) == []


@pytest.mark.parametrize("steps", [
    [step(1), step(99)],          # another line's card, or one cancelled since
    [step(1), step(3)],           # packing sent as a process
    [step(1), step(2), step(2)],  # sent twice
], ids=["foreign", "packing", "twice"])
def test_a_card_that_isnt_one_of_the_lines_processes_is_refused(so_sync, steps):
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked"), card(3, 3, "locked")], heads=[1])
    out = edit(conn, steps, qty_kg=150)
    assert out["error"] == "unknown_job_card" and refusal_status(out["error"]) == 409
    assert writes(conn) == [] and so_sync == []


def test_a_new_process_needs_a_floor(so_sync):
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [step(1), {"job_card_id": None, "process": "Grading", "floor": " "}])
    assert out == {"error": "missing_floor", "message": "Pick a floor for Grading."}
    assert writes(conn) == []


# ── plan status ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["cancelled", "executed"])
def test_a_closed_plan_is_not_edited(so_sync, status):
    conn = ScriptConn([card(1, 1, "closed"), card(2, 2, "closed")], heads=[1],
                      line={"plan_status": status, "fg_sku_name": "Roasted Peanuts 500 g"})
    out = edit(conn, [step(1)], qty_kg=150)
    assert out["error"] == "plan_closed" and refusal_status(out["error"]) == 409
    assert "Roasted Peanuts 500 g" in out["message"]
    assert len(conn.log) == 2 and writes(conn) == []       # nothing past the plan + line locks


# ── removals ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("second", ["locked", "in_progress"])
def test_a_started_first_stage_is_stopped_not_removed(so_sync, removed, second):
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, second), card(3, 3, "locked")], heads=[1])
    out = edit(conn, [step(2)], qty_kg=150)
    assert out["error"] == "cannot_remove_started_first" and refusal_status(out["error"]) == 409
    assert out["message"] == ("P1 is the first stage and has started. Use Stop process on "
                              "PLAN-7-L70-S1 — it sends on what was made first.")
    assert writes(conn) == [] and removed == [] and so_sync == []


def test_the_latest_running_later_stage_is_stopped_not_removed(so_sync, removed):
    """It used to be removable: cancelled without sending on what it had made, with
    packing left waiting on a stage that had already sent everything on."""
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, "in_progress"), card(3, 3, "locked")], heads=[1])
    out = edit(conn, [step(1)], pkg_job_card_id=3, reasons={"2": "wrong process"})
    assert out["error"] == "cannot_remove_started_midchain"
    assert out["message"].endswith("Use Stop process on it — it sends on what was made first.")
    assert writes(conn) == [] and removed == []


def test_a_later_stage_waiting_with_nothing_on_it_can_be_removed(so_sync, removed):
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked"), card(3, 3, "locked")], heads=[1])
    out = edit(conn, [step(1)], pkg_job_card_id=3, reasons={"2": "wrong process"})
    assert out["removed"] == 1 and removed == [2] and out["job_card_ids"] == [1, 3]


def test_a_removal_the_locked_check_missed_is_raised_not_returned(so_sync, monkeypatch):
    """Backstop: it would come after the qty / SO writes, so it must roll them back
    even for a caller that never checks the result."""
    async def cancel(conn, *, job_card_id, reason, deleted_by):
        return {"error": "cannot_remove_terminal", "message": "Sorting is completed."}
    monkeypatch.setattr(jcv2, "_force_record_and_cancel_jc", cancel)
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked"), card(3, 3, "locked")], heads=[1])
    with pytest.raises(ServiceRefusal) as exc:
        edit(conn, [step(1)], qty_kg=150)
    assert exc.value.result["error"] == "cannot_remove_terminal"


# ── quantity ───────────────────────────────────────────────────────────────

def test_a_part_chain_moves_the_line_by_its_change(so_sync):
    """Line 450 kg carded as 225 + (225 to card later): 225 -> 230 makes the line 455."""
    conn = ScriptConn([card(1, 1, "in_progress", planned_qty_kg=225), card(2, 2, "locked", planned_qty_kg=225)],
                      heads=[1], line={"planned_qty_kg": 450, "planned_qty_units": 200})
    edit(conn, [step(1)], qty_kg=230)
    assert args_of(conn, "SET planned_qty_kg = planned_qty_kg + $1") == [(5.0, 0.0, 70)]
    assert [c["kg"] for c in so_sync] == [5.0]


def test_the_so_is_moved_before_anything_else_is_written(so_sync):
    # An increase the SO has no room for raises there, before this edit wrote anything.
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    edit(conn, [step(1)], qty_kg=130, qty_units=12)
    assert so_sync == [{"kg": 30.0, "units": 12.0, "writes_before": 0}]


def test_resaving_the_same_total_after_stage_1_finished_moves_nothing(so_sync):
    # Stage 1 finished at 225; the last edit set the rest to 230.
    chain = [card(1, 1, "completed", planned_qty_kg=225), card(2, 2, "in_progress", planned_qty_kg=230),
             card(3, 3, "locked", planned_qty_kg=230)]
    conn = ScriptConn(chain, heads=[1], line={"planned_qty_kg": 230})
    out = edit(conn, [step(1), step(2)], qty_kg=230)
    assert out["qty_changed"] is False and so_sync == []
    assert args_of(conn, "UPDATE production_plan_line_v2") == []
    conn = ScriptConn(chain, heads=[1], line={"planned_qty_kg": 230})
    edit(conn, [step(1), step(2)], qty_kg=240)
    assert [c["kg"] for c in so_sync] == [10.0]               # not 15, from the stale 225


def test_the_edit_pop_up_prefills_the_same_qty(monkeypatch):
    async def no_canon(conn, fg, entity):
        return None
    monkeypatch.setattr("app.modules.production.services.sfg_canonical.resolve_canonical_sfg_db", no_canon)
    chain = [card(1, 1, "completed", planned_qty_kg=225), card(2, 2, "in_progress", planned_qty_kg=230)]
    cfg = asyncio.run(jcv2.get_line_job_card_config(ScriptConn(chain, heads=[1]), 70))
    assert cfg["qty_kg"] == 230.0 and cfg["started"] is True


def test_a_finished_chain_takes_no_quantity_change(so_sync):
    conn = ScriptConn(one_card("completed"), heads=[1], line={"fg_sku_name": "Roasted Peanuts 500 g"})
    out = edit(conn, [], qty_kg=150, pkg_job_card_id=1)
    assert out["error"] == "chain_finished" and refusal_status(out["error"]) == 409
    assert out["message"].startswith("Every stage of Roasted Peanuts 500 g has finished")
    assert writes(conn) == []


def test_a_finished_card_cant_move_floor(so_sync):
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, "in_progress"), card(3, 3, "locked")], heads=[1])
    out = edit(conn, [step(1, floor="F9"), step(2)], qty_kg=150)
    assert out["error"] == "card_finished" and refusal_status(out["error"]) == 409
    assert out["message"].startswith("P1 (job card PLAN-7-L70-S1) is completed, so it stays on F1.")
    assert writes(conn) == [] and so_sync == []
    # Sent back as it is (any case), it is no change.
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, "in_progress"), card(3, 3, "locked")], heads=[1])
    assert "error" not in edit(conn, [step(1, floor="f1"), step(2)])


# ── every refusal comes before the first write ─────────────────────────────

REFUSALS = [
    ("plan_closed", [card(1, 1, "in_progress"), card(2, 2, "locked")], {"line": {"plan_status": "executed"}},
     [step(1)], {}),
    ("multiple_chains", [card(1, 1, "in_progress"), card(2, 1, "locked")], {"heads": [1, 2]}, [step(1)], {}),
    ("no_job_cards", [], {"heads": []}, [], {}),
    ("terminal_mismatch", [card(1, 1, "in_progress"), card(2, 2, "locked")], {}, [], {"pkg_job_card_id": 1}),
    ("unknown_job_card", [card(1, 1, "in_progress"), card(2, 2, "locked")], {}, [step(1), step(42)], {}),
    ("missing_floor", [card(1, 1, "in_progress"), card(2, 2, "locked")], {},
     [step(1), step(None, floor="")], {}),
    ("cannot_remove_terminal", [card(1, 1, "completed"), card(2, 2, "in_progress"), card(3, 3, "locked")], {},
     [step(2)], {}),
    ("cannot_remove_started_first", [card(1, 1, "in_progress"), card(2, 2, "locked")], {}, [], {}),
    ("cannot_remove_started_midchain", [card(1, 1, "completed"), card(2, 2, "in_progress"),
                                        card(3, 3, "in_progress"), card(4, 4, "locked")], {},
     [step(1), step(3)], {}),
    ("cannot_reorder_started_region", [card(1, 1, "completed"), card(2, 2, "in_progress"),
                                       card(3, 3, "locked")], {}, [step(2), step(1)], {}),
    ("cannot_add_before_released", one_card(), {}, [step(None)], {}),
    ("cannot_add_before_released", one_card("unlocked"), {}, [step(None)], {}),
    ("cannot_remove_holding_material", [card(1, 1, "assigned", has_output=True), card(2, 2, "locked")], {},
     [], {}),
    ("cannot_remove_holding_material", [card(1, 1, "completed"), card(2, 2, "unlocked", carried_qty_kg=80),
                                        card(3, 3, "locked")], {}, [step(1)], {}),
    ("first_stage_requested", [card(1, 1, "unlocked", has_requisition=True), card(2, 2, "locked")], {},
     [], {}),
    ("card_finished", [card(1, 1, "completed"), card(2, 2, "locked")], {}, [step(1, floor="F7")], {}),
    ("invalid_qty", [card(1, 1, "in_progress"), card(2, 2, "locked")], {}, [step(1)], {"qty_kg": 0}),
    ("invalid_qty", [card(1, 1, "in_progress"), card(2, 2, "locked")], {"line": {"planned_qty_kg": 30}},
     [step(1)], {"qty_kg": 20}),                       # the line would go to nothing
    ("chain_finished", [card(1, 1, "completed"), card(2, 2, "closed")], {}, [step(1)], {"qty_kg": 120}),
]


@pytest.mark.parametrize("code,cards,conn_kw,steps,kw", REFUSALS,
                         ids=[f"{r[0]}-{i}" for i, r in enumerate(REFUSALS)])
def test_every_refusal_comes_before_the_first_write(so_sync, removed, code, cards, conn_kw, steps, kw):
    conn_kw = {"heads": [cards[0]["job_card_id"]] if cards else [], **conn_kw}
    conn = ScriptConn(cards, **conn_kw)
    out = edit(conn, steps, **{"qty_kg": 150, **kw})
    assert out["error"] == code, out
    assert out.get("message")
    assert writes(conn) == [] and so_sync == [] and removed == []


def test_the_cards_are_locked_in_step_order_after_the_line(so_sync):
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    edit(conn, [step(1)])
    plan_at = conn.index("FROM production_plan_v2 WHERE plan_id = (")
    line_at = conn.index("FOR UPDATE OF l")
    cards_at = conn.index("ORDER BY step_number FOR UPDATE")
    assert plan_at == 0 and line_at == 1 < conn.index("AS heads, MAX(process_group_id) AS group_id") < cards_at


# ── replace (rebuild before anything starts) ───────────────────────────────

@pytest.fixture
def created(monkeypatch):
    calls = []

    async def create(conn, plan_line_id, **kw):
        calls.append(kw)
        return {"job_card_ids": [900], "count": 1}
    monkeypatch.setattr(jcv2, "create_job_cards_for_line", create)
    return calls


def _replace(conn, wip_steps=()):
    return asyncio.run(jcv2.replace_job_cards_for_line(conn, 70, qty_kg=100, wip_steps=list(wip_steps),
                                                       pkg_floor="F2", pkg_process="Sorting + Packing"))


@pytest.mark.parametrize("blockers,said", [
    ({"requested_on": "P21 (job card PLAN-7-L70-S1)"},
     "The floor has already asked for material for P21 (job card PLAN-7-L70-S1)"),
    ({"amended_on": "P22 (job card PLAN-7-L70-S2)"}, "A BOM amendment has been raised on P22"),
    ({"cancelled": 2}, "This article has cancelled job cards whose records are kept"),
], ids=["requisition", "amendment", "cancelled-history"])
def test_replace_refuses_cards_it_cant_delete_before_any_write(created, blockers, said):
    conn = ScriptConn([card(21, 1, "unlocked"), card(22, 2, "locked")], heads=[21], blockers=blockers)
    out = _replace(conn)
    assert out["error"] == "rebuild_blocked" and refusal_status(out["error"]) == 409
    assert out["message"].startswith(said)
    assert out["message"].endswith("Edit it as a running chain instead: close this and open Edit again.")
    assert writes(conn) == [] and created == []


def test_replace_deletes_only_live_cards_and_can_end_as_one_card(created):
    """An un-started two-card chain edited to one row: one card, raw material to FG."""
    conn = ScriptConn([card(21, 1, "unlocked"), card(22, 2, "locked")], heads=[21])
    out = _replace(conn)
    assert out["job_card_ids"] == [900] and created[0]["wip_steps"] == []
    assert created[0]["pkg_process"] == "Sorting + Packing"
    assert [s for s in writes(conn) if s.startswith("DELETE")] == [
        "DELETE FROM job_card_v2 WHERE plan_line_id = $1 AND deleted_at IS NULL"]


def test_a_requisition_raised_meanwhile_is_a_409_not_a_500(created):
    class FkConn(ScriptConn):
        async def execute(self, sql, *a):
            if sql.lstrip().startswith("DELETE FROM job_card_v2"):
                raise ForeignKeyViolationError("violates foreign key constraint floor_requisition_job_card_id_fkey")
            return await super().execute(sql, *a)

    conn = FkConn([card(21, 1, "unlocked"), card(22, 2, "locked")], heads=[21])
    with pytest.raises(ServiceRefusal) as exc:
        _replace(conn)
    assert exc.value.result["error"] == "rebuild_blocked" and created == []


def test_the_edit_pop_up_is_told_to_edit_a_blocked_chain_in_place(monkeypatch):
    async def no_canon(conn, fg, entity):
        return None
    monkeypatch.setattr("app.modules.production.services.sfg_canonical.resolve_canonical_sfg_db", no_canon)
    conn = ScriptConn(one_card("unlocked"), heads=[1], blockers={"requested_on": "Sorting + Packing (job card X)"})
    cfg = asyncio.run(jcv2.get_line_job_card_config(conn, 70))
    assert cfg["editable"] is False and cfg["started"] is False
    assert cfg["rebuild_blocked"] == ("The floor has already asked for material for Sorting + Packing "
                                      "(job card X), so this chain can't be rebuilt.")
    # A one-card chain: no WIP; its card is the packaging row.
    assert cfg["wip_steps"] == [] and cfg["pkg_job_card_id"] == 1 and cfg["pkg_status"] == "unlocked"
    conn = ScriptConn(one_card("unlocked"), heads=[1])
    cfg = asyncio.run(jcv2.get_line_job_card_config(conn, 70))
    assert cfg["editable"] is True and cfg["rebuild_blocked"] is None


def test_requisitions_are_not_looked_up_before_their_migration():
    class NoRequisitions(ScriptConn):
        async def fetchval(self, sql, *a):
            if "to_regclass('public.floor_requisition')" in sql:
                self._s(sql, a)
                return False
            return await super().fetchval(sql, *a)

    conn = NoRequisitions([card(21, 1, "unlocked")], heads=[21])
    assert asyncio.run(jcv2.rebuild_refusal(conn, 70)) is None
    sql = [s for s, _ in conn.log if "AS cancelled" in s][0]
    assert "floor_requisition" not in sql and "bom_amendment_request_v2" in sql


# ── the new and changed SQL is valid PostgreSQL ────────────────────────────

@requires_pglast
def test_the_new_sql_parses(so_sync, removed):
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    edit(conn, [step(1), step(None)], qty_kg=130, qty_units=12, pkg_floor="F2")
    sqls = [s for s, _ in conn.log if "jcbc:" not in s]
    sqls += [jcv2._REBUILD_BLOCKERS_SQL.format(requisition=jcv2._REQUISITION_ON_CARD),
             jcv2._REBUILD_BLOCKERS_SQL.format(requisition="FALSE"),
             "SELECT to_regclass('public.floor_requisition') IS NOT NULL",
             "DELETE FROM job_card_v2 WHERE plan_line_id = $1 AND deleted_at IS NULL"]
    for sql in sqls:
        pglast.parse_sql(sql)
