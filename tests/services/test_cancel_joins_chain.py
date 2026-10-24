"""Cancelling / stopping a stage joins the chain around it (agreed 2026-10-24).

Before (audit 2026-10-23): cancel / stop only soft-deleted the card. The next
stage kept waiting on it ('awaiting_previous_stage') forever — only a force
unlock freed it — and its prev pointer led to a dead card.

Now: the stage before links straight to the stage after. The next stage is
released when it becomes the first stage, or when the stage before has already
finished (it will dispatch no more). Stop first sends on what the stopped stage
produced. Packaging can only be cancelled once no earlier stage is live, and a
stage of a merged process run can't be cancelled on its own.
"""
from __future__ import annotations

import pytest

from app.modules.production.services import job_card_v2 as jcv2

S1, S2, S3 = 101, 102, 103


def _card(jid, prev, nxt, *, status="unlocked", reason=None, kind="WIP", group=None,
          produced_dispatched=0):
    return {"job_card_id": jid, "prev_job_card_id": prev, "next_job_card_id": nxt,
            "status": status, "locked_reason": reason, "output_kind": kind,
            "process_group_id": group, "dispatched_to_next_kg": produced_dispatched,
            "deleted_at": None}


class _Conn:
    def __init__(self, cards, produced=0.0):
        self.cards = {c["job_card_id"]: dict(c) for c in cards}
        self.produced = produced
        self.dispatched: list[tuple[int, float]] = []

    def _live(self, jid):
        c = self.cards.get(jid)
        return c if c and c["deleted_at"] is None else None

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("UPDATE job_card_v2 SET status = 'cancelled'"):
            c = self.cards[args[0]]
            c["status"], c["deleted_at"] = "cancelled", "now"
            return c
        if "FROM job_card_v2" in s:
            return self._live(args[0])
        return None

    async def fetchval(self, sql, *args):
        if "FROM job_card_output_v2" in sql:
            return self.produced
        return None

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("UPDATE job_card_v2 SET next_job_card_id"):
            self.cards[args[0]]["next_job_card_id"] = args[1]
        elif s.startswith("UPDATE job_card_v2 SET prev_job_card_id"):
            c = self.cards[args[0]]
            c["prev_job_card_id"] = args[1]
            if args[2]:
                c["status"], c["locked_reason"] = "unlocked", None
        return "UPDATE 1"


@pytest.fixture(autouse=True)
def _no_snapshot(monkeypatch):
    async def _snap(conn, job_card_id): return {}
    monkeypatch.setattr(jcv2, "get_job_card", _snap)


async def _cancel(conn, jid):
    return await jcv2.cancel_job_card(conn, job_card_id=jid, reason="Wrong process", deleted_by="Ravi K")


@pytest.mark.asyncio
async def test_cancelling_a_middle_stage_links_its_neighbours():
    conn = _Conn([_card(S1, None, S2, status="in_progress"),
                  _card(S2, S1, S3, status="locked", reason="awaiting_previous_stage"),
                  _card(S3, S2, None, status="locked", reason="awaiting_previous_stage", kind="FG")])
    out = await _cancel(conn, S2)
    assert out["cancelled"] is True
    assert conn.cards[S1]["next_job_card_id"] == S3
    assert conn.cards[S3]["prev_job_card_id"] == S1
    # S1 is still running: S3 waits for S1's dispatch, which now goes to S3.
    assert conn.cards[S3]["status"] == "locked"
    assert out["unlocked_job_card_id"] is None


@pytest.mark.asyncio
async def test_cancelling_the_first_stage_releases_the_next():
    conn = _Conn([_card(S1, None, S2),
                  _card(S2, S1, S3, status="locked", reason="awaiting_previous_stage"),
                  _card(S3, S2, None, status="locked", reason="awaiting_previous_stage", kind="FG")])
    out = await _cancel(conn, S1)
    assert conn.cards[S2]["prev_job_card_id"] is None          # S2 is now the first stage
    assert conn.cards[S2]["status"] == "unlocked"
    assert out["unlocked_job_card_id"] == S2


@pytest.mark.asyncio
async def test_a_finished_stage_before_releases_the_next():
    """The stage before already completed: it won't dispatch again, so waiting on
    it would strand the next stage."""
    conn = _Conn([_card(S1, None, S2, status="completed"),
                  _card(S2, S1, S3, status="unlocked"),
                  _card(S3, S2, None, status="locked", reason="awaiting_previous_stage", kind="FG")])
    out = await _cancel(conn, S2)
    assert conn.cards[S3]["prev_job_card_id"] == S1
    assert conn.cards[S3]["status"] == "unlocked"
    assert out["unlocked_job_card_id"] == S3


@pytest.mark.asyncio
async def test_packaging_with_a_live_earlier_stage_is_refused():
    conn = _Conn([_card(S1, None, S2, status="in_progress"),
                  _card(S2, S1, None, kind="FG")])
    out = await _cancel(conn, S2)
    assert out["error"] == "packaging_has_previous_stage"
    assert conn.cards[S2]["deleted_at"] is None


@pytest.mark.asyncio
async def test_packaging_alone_can_be_cancelled():
    conn = _Conn([_card(S2, None, None, kind="FG")])
    out = await _cancel(conn, S2)
    assert out["cancelled"] is True


@pytest.mark.asyncio
async def test_a_merged_run_stage_is_refused():
    conn = _Conn([_card(S1, None, None, group=9)])
    out = await _cancel(conn, S1)
    assert out["error"] == "merged_run"
    assert conn.cards[S1]["deleted_at"] is None


@pytest.mark.asyncio
async def test_stop_sends_on_what_was_produced_then_joins(monkeypatch):
    sent = []

    async def _dispatch(conn, *, job_card_id, qty_kg, **kw):
        sent.append((job_card_id, qty_kg))
        conn.cards[S3]["status"], conn.cards[S3]["locked_reason"] = "unlocked", None
        return {"dispatched": True, "unlocked_job_card_id": S3}
    monkeypatch.setattr(jcv2, "dispatch_to_next", _dispatch)

    conn = _Conn([_card(S1, None, S2, status="completed"),
                  _card(S2, S1, S3, status="in_progress", produced_dispatched=30.0),
                  _card(S3, S2, None, status="locked", reason="awaiting_previous_stage", kind="FG")],
                 produced=50.0)
    out = await jcv2.stop_job_card(conn, job_card_id=S2, reason="Machine down", stopped_by="Ravi K")
    assert out["stopped"] is True
    assert sent == [(S2, 20.0)]                         # the 20 kg not yet dispatched
    assert conn.cards[S1]["next_job_card_id"] == S3
    assert conn.cards[S3]["prev_job_card_id"] == S1
    assert out["unlocked_job_card_id"] == S3
