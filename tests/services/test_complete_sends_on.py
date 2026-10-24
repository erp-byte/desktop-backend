"""Completing / closing a stage sends its remaining output on (agreed 2026-10-24).

Before: complete and close only changed the status; only an explicit Dispatch
moved material. A stage finished without dispatching left the next stage locked
('awaiting_previous_stage') for good — 45 live cards on 2026-10-23.
"""
from __future__ import annotations

import pytest

from app.modules.production.services import job_card_v2 as jcv2

JC = 301


class _Conn:
    def __init__(self, *, produced, dispatched, nxt=302, kind="WIP", group=None):
        self.card = {"next_job_card_id": nxt, "output_kind": kind,
                     "dispatched_to_next_kg": dispatched, "process_group_id": group}
        self.produced = produced

    async def fetchrow(self, sql, *args):
        return self.card if "next_job_card_id, output_kind, dispatched_to_next_kg" in sql else None

    async def fetchval(self, sql, *args):
        return self.produced if "FROM job_card_output_v2" in sql else None


@pytest.fixture
def calls(monkeypatch):
    log = []

    async def _to_next(conn, *, job_card_id, qty_kg, dispatched_by=None, notes=None, **kw):
        log.append(("next", job_card_id, qty_kg, notes))
        return {"dispatched": True, "unlocked_job_card_id": 302}

    async def _group(conn, *, process_job_card_id, dispatched_by=None):
        log.append(("group", process_job_card_id))
        return {"dispatched": True, "unlocked_job_card_ids": [401, 402]}

    monkeypatch.setattr(jcv2, "dispatch_to_next", _to_next)
    monkeypatch.setattr(jcv2, "dispatch_process_group", _group)
    return log


@pytest.mark.asyncio
async def test_the_undispatched_output_goes_to_the_next_stage(calls):
    out = await jcv2.send_on_remaining_output(_Conn(produced=150, dispatched=100), JC,
                                              sent_by="Ravi K", why="Sent on when the stage was completed")
    assert calls == [("next", JC, 50.0, "Sent on when the stage was completed")]
    assert out["unlocked_job_card_ids"] == [302]


@pytest.mark.asyncio
async def test_nothing_left_sends_nothing(calls):
    assert await jcv2.send_on_remaining_output(_Conn(produced=150, dispatched=150), JC,
                                               sent_by=None, why="x") is None
    assert calls == []


@pytest.mark.asyncio
async def test_packaging_has_nowhere_to_send(calls):
    assert await jcv2.send_on_remaining_output(_Conn(produced=150, dispatched=0, nxt=None, kind="FG"), JC,
                                               sent_by=None, why="x") is None
    assert calls == []


@pytest.mark.asyncio
async def test_a_merged_runs_process_card_shares_it_out(calls):
    out = await jcv2.send_on_remaining_output(_Conn(produced=120, dispatched=100, nxt=None, group=9), JC,
                                              sent_by="Ravi K", why="x")
    assert calls == [("group", JC)]
    assert out["unlocked_job_card_ids"] == [401, 402]


@pytest.mark.asyncio
async def test_a_refused_send_does_not_block_completion(monkeypatch):
    async def _refuse(conn, **kw):
        return {"error": "chain_broken", "message": "The next stage of this job card was cancelled"}
    monkeypatch.setattr(jcv2, "dispatch_to_next", _refuse)
    out = await jcv2.send_on_remaining_output(_Conn(produced=150, dispatched=100), JC, sent_by=None, why="x")
    assert out["error"] == "chain_broken"
    assert out["unlocked_job_card_ids"] == []
