"""Edit Job Card (live edit) after review, 2026-10-27.

Before:
  * removing a later stage that already held material (released by the stage
    before's dispatch, or the latest running stage) cancelled it with that
    material on it, and the next stage stayed locked for good: the stage before
    had sent everything on already;
  * an 'assigned' first stage was refused with "Use Stop process" — which
    refuses 'assigned' cards — and then sent to Cancel, which only admins have:
    it has drawn nothing, so it is removed in place like an unlocked one;
  * the first card could be removed after the floor had asked for material for
    it, and a new first card (stage 1 removed, or a step added in front) got no
    RM / PM list; a qty change on an un-started chain left the list at the old qty;
    a cancelled requisition still kept it first, so "cancel that requisition"
    never unblocked it;
  * Add process in front of a released first card said it "has already received
    material" when it had received nothing;
  * an add-only edit locked every card before the plan row its INSERT needs, so
    it could deadlock with Cancel plan (plan row, then cards).
Services are scripted (ScriptConn); no database.
"""
from __future__ import annotations

import asyncio

import pytest

from app.modules.production.services import job_card_v2 as jcv2
from app.modules.production.services.refusals import refusal_status
from tests.services.test_bom_changes_pdf_rebuild import ScriptConn, card

try:
    import pglast
except ImportError:  # pragma: no cover - exercised only on a box without pglast
    pglast = None

requires_pglast = pytest.mark.skipif(pglast is None, reason="pglast gives real PostgreSQL grammar validation")

PLAN_LOCK = ("SELECT 1 FROM production_plan_v2 WHERE plan_id = (SELECT plan_id FROM "
             "production_plan_line_v2 WHERE plan_line_id = $1) FOR KEY SHARE")
UNLOCK = "SET status='unlocked', is_locked=FALSE, locked_reason=NULL"


def writes(conn):
    return [s for s, _ in conn.log if s.split()[0] in ("UPDATE", "INSERT", "DELETE")]


def args_of(conn, fragment):
    return [a for s, a in conn.log if fragment in s]


@pytest.fixture
def so_sync(monkeypatch):
    calls = []

    async def sync(conn, plan_line_id, *, delta_kg, delta_units, user=None, reason=None):
        calls.append(delta_kg)
        return {"synced": True}
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


@pytest.fixture
def indents(monkeypatch):
    """_materialise_indents, recorded: (job_card_id, kg, units, first stage, bom, fg)."""
    calls = []

    async def materialise(conn, **kw):
        conn._s("-- materialise", (kw["job_card_id"],))
        calls.append((kw["job_card_id"], kw["planned_qty_kg"], kw["planned_qty_units"],
                      kw["is_first_stage"], kw["bom_id"], kw["fg_sku_name"]))
        return (2, 1)
    monkeypatch.setattr(jcv2, "_materialise_indents", materialise)
    return calls


def edit(conn, steps, *, qty_kg=100, qty_units=None, pkg_floor="F1", pkg_job_card_id=None):
    return asyncio.run(jcv2.apply_live_job_card_edits(
        conn, 70, qty_kg=qty_kg, qty_units=qty_units, steps=steps, pkg_floor=pkg_floor,
        pkg_job_card_id=pkg_job_card_id, user="Planner Pat"))


def step(jid, floor="F1", process=None):
    return {"job_card_id": jid, "process": process or (f"P{jid}" if jid else "Sorting"), "floor": floor}


def indent_deletes(conn):
    return [(s.split()[2], a) for s, a in conn.log if s.startswith("DELETE FROM job_card_")]


# ── a later stage that holds material is not removed (CC-1, CD-4) ──────────

@pytest.mark.parametrize("status", ["unlocked", "assigned"])
def test_a_later_stage_holding_what_the_stage_before_sent_is_not_removed(so_sync, removed, indents, status):
    """Roasting finished and sent its 80 kg on to Sorting; removing Sorting cancelled
    it with the 80 kg on it, and packing waited on Roasting for good."""
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, status, carried_qty_kg=80.5),
                       card(3, 3, "locked")], heads=[1])
    out = edit(conn, [step(1)], qty_kg=150, pkg_job_card_id=3)
    assert out["error"] == "cannot_remove_holding_material" and refusal_status(out["error"]) == 409
    assert out["job_card_id"] == 2
    assert out["message"] == (
        "P2 (job card PLAN-7-L70-S2) has already received 80.5 kg from the stage before, so it "
        "can't be removed: that material would be left on a cancelled card. Run that material "
        "through P2 first.")
    assert writes(conn) == [] and removed == [] and so_sync == []


def test_a_later_stage_with_output_on_it_is_not_removed(so_sync, removed, indents):
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, "unlocked", has_output=True),
                       card(3, 3, "locked")], heads=[1])
    out = edit(conn, [step(1)], pkg_job_card_id=3)
    assert out["error"] == "cannot_remove_holding_material"
    assert out["message"] == (
        "P2 (job card PLAN-7-L70-S2) already has output recorded on it, so it can't be removed: "
        "that material would be left on a cancelled card. Run that material through P2 first.")
    assert writes(conn) == [] and removed == []


@pytest.mark.parametrize("status", ["material_received", "in_progress"])
def test_a_started_later_stage_is_stopped_not_removed(so_sync, removed, indents, status):
    """The latest running stage used to be removable here: cancelled without sending
    on what it had made."""
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, status, carried_qty_kg=80),
                       card(3, 3, "locked")], heads=[1])
    out = edit(conn, [step(1)], pkg_job_card_id=3)
    assert out["error"] == "cannot_remove_started_midchain" and refusal_status(out["error"]) == 409
    assert out["job_card_id"] == 2
    assert out["message"] == ("P2 (job card PLAN-7-L70-S2) has already started. Use Stop process "
                              "on it — it sends on what was made first.")
    assert writes(conn) == [] and removed == []


def test_a_waiting_stage_with_nothing_on_it_is_still_removed(so_sync, removed, indents):
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked"), card(3, 3, "locked")], heads=[1])
    out = edit(conn, [step(1)], pkg_job_card_id=3)
    assert out["removed"] == 1 and removed == [2] and out["job_card_ids"] == [1, 3]
    # Roasting is still running: packing waits for its dispatch.
    assert out["unlocked_job_card_ids"] == [] and args_of(conn, UNLOCK) == []


def test_material_is_read_on_the_locked_card_rows(so_sync):
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    edit(conn, [step(1)])
    sql = conn.log[conn.index("ORDER BY step_number FOR UPDATE")][0]
    assert "carried_qty_kg" in sql
    assert ("EXISTS (SELECT 1 FROM job_card_output_v2 o WHERE o.job_card_id = job_card_v2.job_card_id "
            "AND o.deleted_at IS NULL) AS has_output") in sql


@pytest.mark.parametrize("before", ["completed", "closed"])
def test_the_stage_after_a_removed_one_is_released_when_the_stage_before_has_finished(
        so_sync, removed, indents, before):
    """Sorting was released with nothing (Roasting sent nothing on): removing it left
    packing waiting on a stage that dispatches no more."""
    conn = ScriptConn([card(1, 1, before), card(2, 2, "unlocked"), card(3, 3, "locked")], heads=[1])
    out = edit(conn, [step(1)], pkg_job_card_id=3)
    assert out["job_card_ids"] == [1, 3] and removed == [2]
    assert out["unlocked_job_card_ids"] == [3]
    assert args_of(conn, UNLOCK) == [(3,)]
    assert conn.index(UNLOCK) > conn.index("UPDATE job_card_v2 SET step_number=$1, prev_job_card_id=$2")


def test_a_card_held_for_another_reason_is_not_released(so_sync, removed, indents):
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, "locked"),
                       card(3, 3, "locked", locked_reason="discrepancy_hold")], heads=[1])
    out = edit(conn, [step(1)], pkg_job_card_id=3)
    assert out["unlocked_job_card_ids"] == [] and args_of(conn, UNLOCK) == []


# ── the first stage (CD-5, CD-1) ───────────────────────────────────────────

def test_an_assigned_first_stage_is_removed_like_an_unlocked_one(so_sync, removed, indents):
    """Its team is set but nothing has been drawn on it. It was sent to Stop process
    (which refuses an 'assigned' card), then to Cancel, which only admins have —
    and which left the next card as stage 1 with no RM / PM list."""
    conn = ScriptConn([card(1, 1, "assigned", planned_qty_units=12), card(2, 2, "locked", planned_qty_units=12),
                       card(3, 3, "locked", planned_qty_units=12)], heads=[1])
    out = edit(conn, [step(2)], pkg_job_card_id=3)
    assert "error" not in out
    assert removed == [1] and out["job_card_ids"] == [2, 3] and out["unlocked_job_card_ids"] == [2]
    # The new first card draws the raw material, from a list of its own.
    relink = args_of(conn, "SET step_number=$1, prev_job_card_id=$2, next_job_card_id=$3, floor=$4, process_name")
    assert relink[0][:2] == (1, None) and relink[0][6] == "RM"
    assert indent_deletes(conn) == [("job_card_rm_indent_v2", ([2],)), ("job_card_pm_indent_v2", ([2],))]
    assert indents == [(2, 100.0, 12.0, True, 9, "FG")]


@pytest.mark.parametrize("status", ["unlocked", "assigned"])
@pytest.mark.parametrize("holding,what", [
    ({"carried_qty_kg": 40}, "has already received 40 kg from the stage before"),
    ({"has_output": True}, "already has output recorded on it")])
def test_a_first_stage_holding_material_is_not_removed(so_sync, removed, indents, status, holding, what):
    """A card that became stage 1 when the one before it was cancelled can hold
    what that stage sent it; removing it would leave that on a cancelled card."""
    conn = ScriptConn([card(1, 1, status, **holding), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [], pkg_job_card_id=2)
    assert out["error"] == "cannot_remove_holding_material" and out["job_card_id"] == 1
    assert out["message"] == (f"P1 (job card PLAN-7-L70-S1) {what}, so it can't be removed: that "
                              "material would be left on a cancelled card. Run that material through "
                              "P1 first.")
    assert writes(conn) == [] and removed == []


def test_an_assigned_first_stage_the_floor_asked_material_for_stays(so_sync, removed, indents):
    conn = ScriptConn([card(1, 1, "assigned", has_requisition=True), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [], pkg_job_card_id=2)
    assert out["error"] == "first_stage_requested"
    assert writes(conn) == [] and removed == []


def test_a_qty_change_rebuilds_an_assigned_first_cards_list(so_sync, indents):
    """Nothing has been drawn on it, so its list follows the new qty as an
    unlocked card's does."""
    conn = ScriptConn([card(1, 1, "assigned"), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [step(1)], qty_kg=150, qty_units=15, pkg_job_card_id=2)
    assert out["qty_changed"] is True
    assert indent_deletes(conn) == [("job_card_rm_indent_v2", ([1],)), ("job_card_pm_indent_v2", ([1],))]
    assert indents == [(1, 150.0, 15.0, True, 9, "FG")]


@pytest.mark.parametrize("status", ["material_received", "in_progress"])
def test_a_started_first_stage_is_still_sent_to_stop(so_sync, removed, indents, status):
    conn = ScriptConn([card(1, 1, status), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [], pkg_job_card_id=2)
    assert out["error"] == "cannot_remove_started_first"
    assert out["message"] == ("P1 is the first stage and has started. Use Stop process on "
                              "PLAN-7-L70-S1 — it sends on what was made first.")


def test_the_first_card_the_floor_asked_material_for_is_not_removed(so_sync, removed, indents):
    """The requisition (and anything issued on it) stayed on the cancelled card."""
    conn = ScriptConn([card(1, 1, "unlocked", has_requisition=True), card(2, 2, "locked")], heads=[1],
                      blockers={"requested_on": "P1 (job card PLAN-7-L70-S1)"})
    out = edit(conn, [], pkg_job_card_id=2)
    assert out["error"] == "first_stage_requested" and refusal_status(out["error"]) == 409
    assert out["job_card_id"] == 1
    # What unblocks it: a raised requisition can be cancelled; once store has
    # issued on it, only an admin's Cancel JC (DELETE /job-cards-v2/{id}) can.
    assert out["message"] == ("The floor has already asked for material for P1 (job card "
                              "PLAN-7-L70-S1), so it can't be removed. Cancel that requisition "
                              "if store hasn't issued on it yet, or ask an admin to cancel the "
                              "job card.")
    assert writes(conn) == [] and removed == [] and indents == []


def test_no_step_goes_in_front_of_a_first_card_the_floor_asked_material_for(so_sync, indents):
    conn = ScriptConn([card(1, 1, "locked", has_requisition=True), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [step(None, process="Cleaning"), step(1)], pkg_job_card_id=2)
    assert out["error"] == "first_stage_requested" and out["job_card_id"] == 1
    assert out["message"] == ("The floor has already asked for material for P1 (job card "
                              "PLAN-7-L70-S1), so no process can go before it. Cancel that "
                              "requisition if store hasn't issued on it yet, or leave it as the "
                              "first stage.")
    assert writes(conn) == [] and indents == []


class NoRequisitions(ScriptConn):
    async def fetchval(self, sql, *a):
        if "to_regclass('public.floor_requisition')" in sql:
            self._s(sql, a)
            return False
        return await super().fetchval(sql, *a)


def test_requisitions_are_not_read_before_their_migration(so_sync):
    conn = NoRequisitions([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    edit(conn, [step(1)])
    sql = conn.log[conn.index("ORDER BY step_number FOR UPDATE")][0]
    assert "FALSE AS has_requisition" in sql and "floor_requisition r" not in sql
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    edit(conn, [step(1)])
    sql = conn.log[conn.index("ORDER BY step_number FOR UPDATE")][0]
    # A cancelled requisition no longer counts: cancelling it is what unblocks the
    # edit (the row stays, status 'cancelled').
    assert ("EXISTS (SELECT 1 FROM floor_requisition r WHERE r.job_card_id = job_card_v2.job_card_id "
            "AND r.status <> 'cancelled') AS has_requisition") in sql


def test_a_rebuild_still_counts_every_requisition():
    """A rebuild hard-deletes the cards: any requisition row, cancelled too, holds
    its card (ON DELETE RESTRICT)."""
    assert jcv2._REQUISITION_ON_CARD == ("EXISTS (SELECT 1 FROM floor_requisition r "
                                         "WHERE r.job_card_id = j.job_card_id)")


# ── the first card's RM / PM list (CD-1, CD-2) ─────────────────────────────

def test_a_qty_change_on_an_unstarted_chain_rebuilds_the_first_cards_list(so_sync, indents):
    """A chain with cancelled history is edited in place: 100 -> 150 kg left stage 1's
    list at 100 kg, which the floor's requisitions and the store then used."""
    conn = ScriptConn([card(1, 1, "unlocked"), card(2, 2, "locked")], heads=[1], blockers={"cancelled": 2})
    out = edit(conn, [step(1)], qty_kg=150, qty_units=15, pkg_job_card_id=2)
    assert out["qty_changed"] is True
    assert indent_deletes(conn) == [("job_card_rm_indent_v2", ([1],)), ("job_card_pm_indent_v2", ([1],))]
    assert indents == [(1, 150.0, 15.0, True, 9, "FG")]
    qty_at = conn.index("UPDATE job_card_v2 SET planned_qty_kg=$1")
    assert qty_at < conn.index("DELETE FROM job_card_rm_indent_v2") < conn.index("-- materialise")


def test_removing_an_unstarted_first_card_gives_the_new_one_its_list(so_sync, removed, indents):
    conn = ScriptConn([card(1, 1, "unlocked", planned_qty_units=12), card(2, 2, "locked", planned_qty_units=12),
                       card(3, 3, "locked", planned_qty_units=12)], heads=[1])
    out = edit(conn, [step(2)], pkg_job_card_id=3)
    assert out["job_card_ids"] == [2, 3] and out["unlocked_job_card_ids"] == [2]
    # The cancelled first card keeps its list (its history); the new one gets one.
    assert indent_deletes(conn) == [("job_card_rm_indent_v2", ([2],)), ("job_card_pm_indent_v2", ([2],))]
    assert indents == [(2, 100.0, 12.0, True, 9, "FG")]
    assert conn.index("-- materialise") > conn.index("UPDATE job_card_v2 SET step_number=$1")


def test_a_step_added_in_front_takes_the_list_from_the_old_first_card(so_sync, indents):
    conn = ScriptConn([card(1, 1, "locked"), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [step(None, process="Cleaning"), step(1)], qty_units=8, pkg_job_card_id=2)
    assert out["job_card_ids"] == [900, 1, 2]
    assert indent_deletes(conn) == [("job_card_rm_indent_v2", ([900, 1],)),
                                    ("job_card_pm_indent_v2", ([900, 1],))]
    assert indents == [(900, 100.0, 8.0, True, 9, "FG")]


def test_a_started_first_card_keeps_its_list(so_sync, indents):
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [step(1)], qty_kg=150, pkg_job_card_id=2)
    assert out["qty_changed"] is True
    assert indent_deletes(conn) == [] and indents == []


def test_a_floor_only_edit_leaves_the_list_alone(so_sync, indents):
    conn = ScriptConn([card(1, 1, "unlocked"), card(2, 2, "locked")], heads=[1])
    out = edit(conn, [step(1, floor="F2")], pkg_job_card_id=2)
    assert out["floors_changed"] == 1
    assert indent_deletes(conn) == [] and indents == []


# ── adding in front of a released first card (CD-3) ────────────────────────

@pytest.mark.parametrize("status", ["unlocked", "assigned"])
def test_adding_before_a_released_first_card_gives_the_real_reason(so_sync, indents, status):
    """One card on a line with cancelled history: the card had received nothing."""
    conn = ScriptConn([card(1, 1, status, process_name="Packing")], heads=[1], blockers={"cancelled": 2})
    out = edit(conn, [step(None, process="Roasting")], pkg_job_card_id=1)
    assert out["error"] == "cannot_add_before_released" and out["job_card_id"] == 1
    assert out["message"] == (
        "Packing (job card PLAN-7-L70-S1) is the chain's first stage and is already released to "
        "its floor, so a process can't be added before it here. Ask an admin to cancel that job "
        "card, then create the job cards again with the new process.")
    assert writes(conn) == []


def test_adding_before_a_card_that_received_material_keeps_its_reason(so_sync):
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, "unlocked", carried_qty_kg=80)], heads=[1])
    out = edit(conn, [step(1), step(None)], pkg_job_card_id=2)
    assert out["error"] == "cannot_add_before_released"
    assert out["message"] == ("P2 (job card PLAN-7-L70-S2) has already received material — a "
                              "process can't be added before it.")


# ── lock order: the plan row before any card (CC-3) ────────────────────────

def test_the_plan_row_is_taken_before_the_line_and_the_cards(so_sync):
    """Cancel plan takes the plan row FOR UPDATE, then the plan's cards; an add's
    INSERT needs the plan row (its foreign key) — taken after the cards, they deadlocked."""
    conn = ScriptConn([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    edit(conn, [step(1), step(None)], pkg_job_card_id=2)
    assert conn.log[0] == (PLAN_LOCK, (70,))
    assert "FOR UPDATE OF l" in conn.log[1][0]
    assert conn.index("ORDER BY step_number FOR UPDATE") < conn.index("INSERT INTO job_card_v2")


# ── the Edit pop-up is told what each card holds ───────────────────────────

class ConfigConn(ScriptConn):
    def __init__(self, cards, steps=(), **kw):
        super().__init__(cards, heads=[c["job_card_id"] for c in cards[:1]], **kw)
        self.steps = list(steps)

    async def fetch(self, sql, *a):
        if "FROM production_plan_step_v2" in sql and "ORDER BY step_order" in sql:
            self._s(sql, a)
            return self.steps
        return await super().fetch(sql, *a)


@pytest.fixture
def no_canon(monkeypatch):
    async def none(conn, fg, entity):
        return None
    monkeypatch.setattr("app.modules.production.services.sfg_canonical.resolve_canonical_sfg_db", none)


def test_the_config_says_which_cards_hold_material_or_were_asked_for(no_canon):
    conn = ConfigConn([card(1, 1, "unlocked", has_requisition=True),
                       card(2, 2, "unlocked", carried_qty_kg=40),
                       card(3, 3, "assigned", has_output=True),
                       card(4, 4, "locked", carried_qty_kg=0.0004),        # under half a gram
                       card(5, 5, "locked")])
    cfg = asyncio.run(jcv2.get_line_job_card_config(conn, 70))
    assert [(s["job_card_id"], s["holds_material"], s["has_requisition"]) for s in cfg["wip_steps"]] == [
        (1, False, True), (2, True, False), (3, True, False), (4, False, False)]
    sql = [s for s, _ in conn.log if "ORDER BY step_number" in s and "AS has_output" in s]
    assert len(sql) == 1 and "AND r.status <> 'cancelled') AS has_requisition" in sql[0]


def test_the_config_without_requisitions_and_with_no_cards(no_canon):
    class NoReqConfig(ConfigConn):
        async def fetchval(self, sql, *a):
            if "to_regclass('public.floor_requisition')" in sql:
                self._s(sql, a)
                return False
            return await super().fetchval(sql, *a)

    conn = NoReqConfig([card(1, 1, "unlocked"), card(2, 2, "locked")])
    cfg = asyncio.run(jcv2.get_line_job_card_config(conn, 70))
    assert [s["has_requisition"] for s in cfg["wip_steps"]] == [False]
    sql = [s for s, _ in conn.log if "AS has_output" in s][0]
    assert "FALSE AS has_requisition" in sql and "floor_requisition r" not in sql
    # No cards yet: the plan's own route prefills Create, as before.
    conn = NoReqConfig([], steps=[{"step_order": 1, "process_name": "Roasting", "floor": "F1"},
                                  {"step_order": 2, "process_name": "Packing", "floor": "F2"}])
    cfg = asyncio.run(jcv2.get_line_job_card_config(conn, 70))
    assert cfg["exists"] is False and cfg["pkg_floor"] == "F2"
    assert cfg["wip_steps"] == [{"process": "Roasting", "floor": "F1", "sfg_output": None,
                                 "job_card_id": None, "started": False}]


# ── the new SQL is valid PostgreSQL ────────────────────────────────────────

@requires_pglast
def test_the_new_sql_parses(so_sync, removed, no_canon):
    runs = []
    conn = ScriptConn([card(1, 1, "completed"), card(2, 2, "unlocked"), card(3, 3, "locked")], heads=[1])
    edit(conn, [step(1)], pkg_job_card_id=3)                        # release after a finished stage
    runs.append(conn)
    conn = ScriptConn([card(1, 1, "unlocked"), card(2, 2, "locked")], heads=[1])
    edit(conn, [step(1)], qty_kg=150, pkg_job_card_id=2)            # the list rebuilt
    runs.append(conn)
    conn = NoRequisitions([card(1, 1, "in_progress"), card(2, 2, "locked")], heads=[1])
    edit(conn, [step(1)])
    runs.append(conn)
    for c in (ConfigConn([card(1, 1, "unlocked"), card(2, 2, "locked")]),):
        asyncio.run(jcv2.get_line_job_card_config(c, 70))
        runs.append(c)
    sqls = {s for c in runs for s, _ in c.log if "jcbc:" not in s and not s.startswith("--")}
    assert PLAN_LOCK in sqls
    assert any(s.startswith("DELETE FROM job_card_pm_indent_v2") for s in sqls)
    for sql in sqls:
        pglast.parse_sql(sql)
