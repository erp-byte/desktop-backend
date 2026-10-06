"""Every production route that can unlock a job card schedules the unlock notice
as a background task, AFTER its transaction has committed, for the card it
UNLOCKED — the next card, which can now start — never for the card whose batch
closed or whose material was dispatched. Services are faked; no database, SMTP
server or Graph API.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.modules.production import router as R
from app.modules.production.services import job_card_batch_v2 as batch_svc
from app.modules.production.services import job_card_unlock_notify
from app.modules.production.services import job_card_v2 as jcv2

USER = SimpleNamespace(is_admin=True, allowed_floors=[], allowed_warehouses=[], full_name="Ravi K",
                       email="r@candorfoods.in", phone="9876543210", user_id=8)
UPSTREAM, DOWNSTREAM = 7001, 7002


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


class _WatchedTasks(BackgroundTasks):
    """Records whether the transaction had committed when the notice was scheduled."""

    def __init__(self, conn):
        super().__init__()
        self.conn, self.committed_when_added = conn, None

    def add_task(self, func, *args, **kwargs):
        self.committed_when_added = self.conn.committed
        super().add_task(func, *args, **kwargs)


@pytest.fixture
def scheduled(monkeypatch):
    conn = _Conn()
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn), conn=conn)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    ran: list[tuple] = []

    async def notifier(pool_, unlocks, unlocked_by=None, actor_user_id=None):
        ran.append((pool_, unlocks, unlocked_by, actor_user_id))

    async def writable(conn_, **kw):
        return None

    monkeypatch.setattr(job_card_unlock_notify, "notify_floor_of_unlocked_job_cards", notifier)
    monkeypatch.setattr(R, "_assert_jc_writable_by_user", writable)
    return SimpleNamespace(request=request, pool=pool, conn=conn,
                           tasks=_WatchedTasks(conn), notifier=notifier, ran=ran)


def _notified(s) -> list[dict]:
    """The unlocks the single scheduled task hands the notice."""
    assert len(s.tasks.tasks) == 1, s.tasks.tasks
    task = s.tasks.tasks[0]
    assert task.func is s.notifier
    assert task.args[0] is s.pool
    assert task.kwargs.get("unlocked_by") == "Ravi K" and task.kwargs.get("actor_user_id") == 8
    assert s.tasks.committed_when_added is True      # scheduled only after the commit
    assert s.ran == []                               # and not run inside the request
    asyncio.run(s.tasks())
    return task.args[1]


DISPATCH = {"dispatch_id": 1, "from_job_card_id": UPSTREAM, "to_job_card_id": DOWNSTREAM, "qty_kg": 149.8}


# ── batch close (auto-dispatch) ────────────────────────────────────────────

def _close_body():
    return R.BatchCloseRequest(produced_qty_kg=149.8)


def test_closing_a_batch_notifies_the_next_card_it_unlocked_not_its_own(scheduled, monkeypatch):
    async def close(conn, **kw):
        assert kw["job_card_id"] == UPSTREAM
        return {"closed": True, "dispatch": DISPATCH, "downstream_unlocked": True,
                "unlocked_job_card_id": DOWNSTREAM}

    monkeypatch.setattr(batch_svc, "close_batch", close)
    asyncio.run(R.batch_close_v2(scheduled.request, UPSTREAM, 55, _close_body(), scheduled.tasks, user=USER))
    unlocks = _notified(scheduled)
    assert unlocks == [{"job_card_id": DOWNSTREAM, "from_job_card_id": UPSTREAM, "qty_kg": 149.8,
                        "how": "dispatch"}]
    assert UPSTREAM not in [u["job_card_id"] for u in unlocks]


def test_closing_a_batch_that_unlocked_nothing_notifies_nobody(scheduled, monkeypatch):
    async def close(conn, **kw):
        return {"closed": True, "dispatch": DISPATCH, "downstream_unlocked": False,
                "unlocked_job_card_id": None}

    monkeypatch.setattr(batch_svc, "close_batch", close)
    asyncio.run(R.batch_close_v2(scheduled.request, UPSTREAM, 55, _close_body(), scheduled.tasks, user=USER))
    assert scheduled.tasks.tasks == []


def test_a_refused_batch_close_notifies_nobody(scheduled, monkeypatch):
    async def close(conn, **kw):
        return {"error": "batch_unbalanced", "unlocked_job_card_id": DOWNSTREAM}

    monkeypatch.setattr(batch_svc, "close_batch", close)
    with pytest.raises(HTTPException):
        asyncio.run(R.batch_close_v2(scheduled.request, UPSTREAM, 55, _close_body(), scheduled.tasks, user=USER))
    assert scheduled.tasks.tasks == []


# ── dispatch to next ───────────────────────────────────────────────────────

def test_dispatching_notifies_the_next_card_it_unlocked(scheduled, monkeypatch):
    async def dispatch(conn, **kw):
        return {"dispatched": True, "dispatch": DISPATCH, "unlocked_job_card_id": DOWNSTREAM}

    monkeypatch.setattr(jcv2, "dispatch_to_next", dispatch)
    asyncio.run(R.dispatch_to_next_v2(scheduled.request, UPSTREAM, R.DispatchToNextRequest(qty_kg=149.8),
                                      scheduled.tasks, user=USER))
    assert _notified(scheduled) == [{"job_card_id": DOWNSTREAM, "from_job_card_id": UPSTREAM,
                                     "qty_kg": 149.8, "how": "dispatch"}]


@pytest.mark.parametrize("result", [
    {"dispatched": True, "dispatch": DISPATCH, "unlocked_job_card_id": None},     # next card already open
    {"error": "over_dispatch", "message": "too much"},                           # refused (comes back 200)
])
def test_a_dispatch_that_unlocked_nothing_notifies_nobody(scheduled, monkeypatch, result):
    async def dispatch(conn, **kw):
        return result

    monkeypatch.setattr(jcv2, "dispatch_to_next", dispatch)
    asyncio.run(R.dispatch_to_next_v2(scheduled.request, UPSTREAM, R.DispatchToNextRequest(qty_kg=149.8),
                                      scheduled.tasks, user=USER))
    assert scheduled.tasks.tasks == []


# ── merged process group ───────────────────────────────────────────────────

def test_a_group_dispatch_notifies_each_packaging_card_it_unlocked(scheduled, monkeypatch):
    async def group(conn, **kw):
        return {"dispatched": True, "process_group_id": 9, "produced_kg": 100.0,
                "results": [{"packaging_job_card_id": 8001, "qty_kg": 50.0, "unlocked": True},
                            {"packaging_job_card_id": 8002, "qty_kg": 50.0, "unlocked": False}],
                "unlocked_job_card_ids": [8001]}

    monkeypatch.setattr(jcv2, "dispatch_process_group", group)
    asyncio.run(R.dispatch_process_group_v2(scheduled.request, 8000, scheduled.tasks, user=USER))
    assert _notified(scheduled) == [{"job_card_id": 8001, "from_job_card_id": 8000, "qty_kg": 50.0,
                                     "how": "dispatch"}]


# ── force unlock ───────────────────────────────────────────────────────────

def test_a_force_unlock_notifies_that_card_with_the_reason(scheduled, monkeypatch):
    async def force(conn, **kw):
        return {"force_unlocked": True, "job_card": {"job_card_id": kw["job_card_id"]}}

    monkeypatch.setattr(jcv2, "force_unlock", force)
    asyncio.run(R.force_unlock_v2(scheduled.request, DOWNSTREAM,
                                  R.ForceUnlockV2Request(authority="Plant head", reason="QC cleared by hand"),
                                  scheduled.tasks, user=USER))
    assert _notified(scheduled) == [{"job_card_id": DOWNSTREAM, "how": "force_unlock",
                                     "reason": "QC cleared by hand"}]


def test_a_refused_force_unlock_notifies_nobody(scheduled, monkeypatch):
    async def force(conn, **kw):
        return {"error": "not_locked", "message": "JC is already unlocked; nothing to force"}

    monkeypatch.setattr(jcv2, "force_unlock", force)
    with pytest.raises(HTTPException):
        asyncio.run(R.force_unlock_v2(scheduled.request, DOWNSTREAM,
                                      R.ForceUnlockV2Request(authority="Plant head", reason="x"),
                                      scheduled.tasks, user=USER))
    assert scheduled.tasks.tasks == []


# ── live chain edit ────────────────────────────────────────────────────────

def test_a_chain_edit_that_released_a_new_head_notifies_it(scheduled, monkeypatch):
    async def apply(conn, plan_line_id, **kw):
        return {"plan_id": 1, "plan_line_id": plan_line_id, "job_card_ids": [9101, 9102],
                "created_job_card_ids": [], "unlocked_job_card_ids": [9101]}

    monkeypatch.setattr(jcv2, "apply_live_job_card_edits", apply)
    asyncio.run(R.apply_line_job_card_edits_v2(
        scheduled.request, 145549, R.JobCardLineApplyEdits(qty_kg=64, steps=[], pkg_floor="Packing Floor"),
        scheduled.tasks, user=USER))
    assert _notified(scheduled) == [{"job_card_id": 9101, "how": "chain_edit"}]
