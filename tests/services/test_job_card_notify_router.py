"""Every production route that creates a job card schedules the floor notice as a
background task, AFTER its transaction has committed and only for the cards it
actually created. Services are faked; no database, SMTP server or Graph API.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from app.modules.production import router as R
from app.modules.production.services import job_card_notify, job_card_v2 as jcv2, plan_v2

USER = SimpleNamespace(is_admin=True, allowed_floors=[], full_name="Ravi K",
                       email="r@candorfoods.in", phone="9876543210", user_id=8)


class _Ctx:
    def __init__(self, value, on_exit=None):
        self.value, self.on_exit = value, on_exit

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        if self.on_exit:
            self.on_exit(exc[0] is None)
        return False


class _Conn:
    def __init__(self):
        self.committed = False

    def transaction(self):
        return _Ctx(self, on_exit=lambda ok: setattr(self, "committed", ok))

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        return "UPDATE 0"


class _WatchedTasks(BackgroundTasks):
    """Records whether the creating transaction had already committed when the
    notice was scheduled."""

    def __init__(self, conn):
        super().__init__()
        self.conn, self.committed_when_added = conn, None

    def add_task(self, func, *args, **kwargs):
        self.committed_when_added = self.conn.committed
        super().add_task(func, *args, **kwargs)


@pytest.fixture
def scheduled(monkeypatch):
    """The request under test, its background tasks, and what the notice was told."""
    conn = _Conn()
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn), conn=conn)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    ran: list[tuple] = []

    async def notifier(pool_, job_card_ids, created_by=None):
        ran.append((pool_, job_card_ids, created_by))

    monkeypatch.setattr(job_card_notify, "notify_floor_of_new_job_cards", notifier)
    return SimpleNamespace(request=request, pool=pool, conn=conn,
                           tasks=_WatchedTasks(conn), notifier=notifier, ran=ran)


def _notified(s) -> list[int]:
    """The job card ids the single scheduled task will hand the notice."""
    assert len(s.tasks.tasks) == 1, s.tasks.tasks
    task = s.tasks.tasks[0]
    assert task.func is s.notifier
    assert task.args[0] is s.pool and task.kwargs.get("created_by") == "Ravi K"
    assert s.tasks.committed_when_added is True      # scheduled only after the commit
    assert s.ran == []                               # and it has not run inside the request
    asyncio.run(s.tasks())
    assert [r[1] for r in s.ran] == [task.args[1]]
    return task.args[1]


# ── plan approve → create_job_cards_from_plan ──────────────────────────────

def test_approving_a_plan_notifies_every_card_it_fanned_out(scheduled, monkeypatch):
    async def approve(conn, plan_id, approved_by):
        return {"approved": True, "job_cards_created": True, "plan": {"plan_id": plan_id},
                "job_cards": {"plan_id": plan_id, "lines": [
                    {"plan_line_id": 1, "job_card_ids": [101, 102]},
                    {"plan_line_id": 2, "job_card_ids": [103]}]}}

    monkeypatch.setattr(plan_v2, "approve_plan", approve)
    asyncio.run(R.approve_plan_v2(scheduled.request, 145504, R.PlanV2Approve(approved_by="Ravi K"),
                                  scheduled.tasks, user=USER))
    assert _notified(scheduled) == [101, 102, 103]


def test_a_re_approve_that_created_nothing_notifies_nobody(scheduled, monkeypatch):
    async def approve(conn, plan_id, approved_by):
        return {"approved": True, "job_cards_created": False, "job_cards_error": "job_cards_already_exist",
                "plan": {"plan_id": plan_id}, "job_cards": {"error": "job_cards_already_exist", "count": 3}}

    monkeypatch.setattr(plan_v2, "approve_plan", approve)
    asyncio.run(R.approve_plan_v2(scheduled.request, 145504, R.PlanV2Approve(approved_by="Ravi K"),
                                  scheduled.tasks, user=USER))
    assert scheduled.tasks.tasks == []


# ── add a plan step → _spawn_jc_for_new_step ───────────────────────────────

def test_adding_a_step_notifies_the_card_it_spawned(scheduled, monkeypatch):
    async def add_step(conn, plan_line_id, step_data):
        return {"added": True, "step": {"step_id": 5001}, "spawned_job_card_id": 204,
                "jc_sync": {"applied": True}}

    monkeypatch.setattr(plan_v2, "add_step", add_step)
    asyncio.run(R.add_step_v2(scheduled.request, 145549, R.StepV2Add(process_name="Roasting", floor="F1"),
                              scheduled.tasks, user=USER))
    assert _notified(scheduled) == [204]


def test_a_step_on_an_uncarded_line_notifies_nobody(scheduled, monkeypatch):
    async def add_step(conn, plan_line_id, step_data):
        return {"added": True, "step": {"step_id": 5001}}

    monkeypatch.setattr(plan_v2, "add_step", add_step)
    asyncio.run(R.add_step_v2(scheduled.request, 145549, R.StepV2Add(process_name="Roasting", floor="F1"),
                              scheduled.tasks, user=USER))
    assert scheduled.tasks.tasks == []


# ── the Create / Edit job card wizard ──────────────────────────────────────

def _line_body():
    return R.JobCardLineCreate(qty_kg=64, wip_steps=[R.JobCardLineCreateStep(process="Flavouring", floor="F1")],
                               pkg_floor="Packing Floor")


def test_creating_a_lines_job_cards_notifies_the_chain(scheduled, monkeypatch):
    async def create(conn, plan_line_id, **kw):
        return {"plan_id": 145504, "plan_line_id": plan_line_id, "job_card_ids": [301, 302], "count": 2}

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(jcv2, "create_job_cards_for_line", create)
    monkeypatch.setattr(jcv2, "consolidate_plan_lines_for_merge", nothing)
    monkeypatch.setattr(jcv2, "maybe_release_plan_from_jcs", nothing)
    asyncio.run(R.create_line_job_cards_v2(scheduled.request, 145549, _line_body(),
                                           scheduled.tasks, user=USER))
    assert _notified(scheduled) == [301, 302]


def test_replacing_a_lines_job_cards_notifies_the_rebuilt_chain(scheduled, monkeypatch):
    async def replace(conn, plan_line_id, **kw):
        return {"plan_id": 145504, "plan_line_id": plan_line_id, "job_card_ids": [401, 402],
                "count": 2, "replaced": 2}

    monkeypatch.setattr(jcv2, "replace_job_cards_for_line", replace)
    asyncio.run(R.replace_line_job_cards_v2(scheduled.request, 145549, _line_body(),
                                            scheduled.tasks, user=USER))
    # The cards this request replaced are gone; only the ones it created are told about,
    # each exactly once.
    assert _notified(scheduled) == [401, 402]


def test_a_refused_create_notifies_nobody(scheduled, monkeypatch):
    async def create(conn, plan_line_id, **kw):
        return {"error": "job_cards_already_exist", "count": 3}

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(jcv2, "create_job_cards_for_line", create)
    monkeypatch.setattr(jcv2, "consolidate_plan_lines_for_merge", nothing)
    monkeypatch.setattr(jcv2, "maybe_release_plan_from_jcs", nothing)
    with pytest.raises(Exception):
        asyncio.run(R.create_line_job_cards_v2(scheduled.request, 145549, _line_body(),
                                               scheduled.tasks, user=USER))
    assert scheduled.tasks.tasks == []


# ── merged process run ─────────────────────────────────────────────────────

def test_a_merged_process_run_notifies_process_and_packaging_cards(scheduled, monkeypatch):
    async def merge(conn, **kw):
        return {"process_group_id": 9, "process_job_card_ids": [501, 502],
                "packaging": [{"plan_line_id": 1, "job_card_id": 503},
                              {"plan_line_id": 2, "job_card_id": 504}], "count": 4}

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(jcv2, "create_merged_process_run", merge)
    monkeypatch.setattr(jcv2, "maybe_release_plan_from_jcs", nothing)
    body = R.ProcessMergeCreateRequest(
        plan_line_ids=[1, 2],
        wip_steps=[R.ProcessMergeStepIn(process="Roasting", floor="F1")],
        per_member=[R.ProcessMergeMemberIn(plan_line_id=1, pkg_floor="P1"),
                    R.ProcessMergeMemberIn(plan_line_id=2, pkg_floor="P2")])
    asyncio.run(R.create_merged_process_run_v2(scheduled.request, body, scheduled.tasks, user=USER))
    assert _notified(scheduled) == [501, 502, 503, 504]


# ── live edit: only the cards the edit created ─────────────────────────────

def test_a_live_edit_notifies_only_the_cards_it_created(scheduled, monkeypatch):
    async def edit(conn, plan_line_id, **kw):
        return {"plan_id": 145504, "plan_line_id": plan_line_id,
                "job_card_ids": [601, 602, 603], "created_job_card_ids": [602],
                "removed": 0, "added": 1, "floors_changed": 0, "qty_changed": False, "so_sync": {}}

    monkeypatch.setattr(jcv2, "apply_live_job_card_edits", edit)
    body = R.JobCardLineApplyEdits(
        qty_kg=64, steps=[R.JobCardLineEditStep(job_card_id=601, process="Flavouring", floor="F1"),
                          R.JobCardLineEditStep(process="Roasting", floor="F1")],
        pkg_floor="Packing Floor")
    asyncio.run(R.apply_line_job_card_edits_v2(scheduled.request, 145549, body,
                                               scheduled.tasks, user=USER))
    assert _notified(scheduled) == [602]


def test_an_edit_that_creates_nothing_notifies_nobody(scheduled, monkeypatch):
    async def edit(conn, plan_line_id, **kw):
        return {"plan_id": 145504, "plan_line_id": plan_line_id, "job_card_ids": [601, 603],
                "created_job_card_ids": [], "removed": 0, "added": 0, "floors_changed": 1,
                "qty_changed": False, "so_sync": {}}

    monkeypatch.setattr(jcv2, "apply_live_job_card_edits", edit)
    body = R.JobCardLineApplyEdits(
        qty_kg=64, steps=[R.JobCardLineEditStep(job_card_id=601, process="Flavouring", floor="F2")],
        pkg_floor="Packing Floor")
    asyncio.run(R.apply_line_job_card_edits_v2(scheduled.request, 145549, body,
                                               scheduled.tasks, user=USER))
    assert scheduled.tasks.tasks == []


# ── the services hand the router the ids it needs ──────────────────────────

def test_a_live_edit_reports_the_ids_it_created():
    """apply_live_job_card_edits returns the added cards separately from the whole
    chain — without that the route could not tell a new card from an untouched one."""
    from tests.services.test_bom_changes_pdf_rebuild import ScriptConn, card

    conn = ScriptConn([card(1, 1, "unlocked"), card(2, 2, "locked"), card(3, 3, "locked")], heads=[1])
    out = asyncio.run(jcv2.apply_live_job_card_edits(
        conn, 70, qty_kg=100, pkg_floor="F2", user="Planner Pat",
        steps=[{"job_card_id": None, "process": "Cleaning", "floor": "F1"},
               {"job_card_id": 1, "process": "P1", "floor": "F1"},
               {"job_card_id": 2, "process": "P2", "floor": "F1"}]))
    assert out["job_card_ids"] == [900, 1, 2, 3] and out["created_job_card_ids"] == [900]


_ON_LINE = {"job_card_id": 601, "plan_step_id": 41, "step_number": 1, "status": "unlocked",
            "prev_job_card_id": None, "next_job_card_id": None}


class _StepConn:
    """asyncpg-shaped enough for plan_v2.add_step: the next step_order, the
    INSERT ... RETURNING, and the job cards already on the line."""

    def __init__(self, jcs=()):
        self.jcs = list(jcs)

    def is_in_transaction(self):
        return True                       # insert_with_pk_retry only savepoints

    def transaction(self):
        return _Ctx(self)

    async def fetchval(self, sql, *args):
        assert "MAX(step_order)" in sql, sql
        return 3

    async def fetchrow(self, sql, *args):
        assert "INSERT INTO production_plan_step_v2" in sql, sql
        return {"step_id": args[0], "plan_line_id": args[1], "step_order": args[2],
                "process_name": args[3], "stage": args[4], "floor": args[5]}

    async def fetch(self, sql, *args):
        assert "FROM job_card_v2" in sql, sql
        return self.jcs


def test_add_step_reports_the_card_it_spawned(monkeypatch):
    """plan_v2.add_step names the spawned card so the route can notify its floor."""
    spawned = []

    async def spawn(conn, *, plan_line_id, step_id):
        spawned.append((plan_line_id, step_id))
        return 39582699

    async def resync(conn, plan_line_id, reason=None):
        return {"applied": True}

    monkeypatch.setattr(plan_v2, "_spawn_jc_for_new_step", spawn)
    monkeypatch.setattr(plan_v2, "_resync_jcs_after_step_change", resync)

    out = asyncio.run(plan_v2.add_step(_StepConn(jcs=[_ON_LINE]), 145549,
                                       {"process_name": "Flavouring + Mixing", "floor": "Roasting Area"}))
    assert out["spawned_job_card_id"] == 39582699
    assert spawned == [(145549, out["step"]["step_id"])]      # the card for THIS new step


def test_add_step_names_no_card_when_none_was_spawned(monkeypatch):
    """An unapproved line has no chain to extend, and a refused spawn creates
    nothing — either way the route must find no id to notify about."""
    out = asyncio.run(plan_v2.add_step(_StepConn(), 145549, {"process_name": "Cleaning"}))
    assert "spawned_job_card_id" not in out

    async def no_spawn(conn, *, plan_line_id, step_id):
        return None

    monkeypatch.setattr(plan_v2, "_spawn_jc_for_new_step", no_spawn)
    out = asyncio.run(plan_v2.add_step(_StepConn(jcs=[_ON_LINE]), 145549, {"process_name": "Cleaning"}))
    assert "spawned_job_card_id" not in out
    assert out["jc_sync"]["skipped_reason"] == "spawn_returned_none"
