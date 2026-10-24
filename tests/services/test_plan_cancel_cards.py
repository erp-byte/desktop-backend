"""Cancelling / deleting a plan takes its job cards with it (agreed 2026-10-24).

Before (audit 2026-10-23): cancel_plan flipped the plan's status and gave its SO
qty back, but left every job card live — the floors went on making an order the
SO already showed as pending again. The reason typed in was thrown away.

Now: refused, before anything is written, when work has started on any card
(stop or finish it first) or a card is part of a merged process run; otherwise
every card is cancelled with a snapshot and the reason, the lines give their SO
qty back (lines tied to several SO rows first), and the reason is kept on the plan.
"""
from __future__ import annotations

import pytest

from app.modules.production.services import job_card_v2 as jcv2
from app.modules.production.services import plan_v2
from app.modules.production.services import so_reservation

PLAN = 9001


def _card(jid, status="unlocked", *, group=None):
    return {"job_card_id": jid, "job_card_number": f"PLAN-{PLAN}-L1-S{jid % 10}",
            "status": status, "process_group_id": group}


def _line(lid, fids, kg=100.0, units=400.0):
    return {"plan_line_id": lid, "planned_qty_kg": kg, "planned_qty_units": units,
            "linked_so_fulfillment_ids": fids}


class _Conn:
    def __init__(self, *, status="approved", cards=(), lines=()):
        self.status = status
        self.cards = [dict(c) for c in cards]
        self.lines = [dict(ln) for ln in lines]
        self.cancelled_cards: list[tuple] = []
        self.plan_update: tuple | None = None

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT status FROM production_plan_v2"):
            return None if self.status is None else {"status": self.status}
        if s.startswith("UPDATE production_plan_v2"):
            self.plan_update = args
            return {"plan_id": args[0], "status": "cancelled", "cancellation_reason": args[1]}
        raise AssertionError(f"unexpected fetchrow: {s[:70]}")

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM job_card_v2" in s:
            return self.cards
        if "FROM production_plan_line_v2" in s:
            return self.lines
        raise AssertionError(f"unexpected fetch: {s[:70]}")

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        assert s.startswith("UPDATE job_card_v2 SET status = 'cancelled'"), s[:70]
        self.cancelled_cards.append(args)
        return "UPDATE 1"


@pytest.fixture
def released(monkeypatch):
    calls: list[tuple] = []

    async def release(conn, plan_line_id, fids, *, kg, units):
        calls.append((plan_line_id, list(fids or []), kg, units))
        return {f: (kg, units) for f in fids[:1]} if fids else {}
    monkeypatch.setattr(so_reservation, "release", release)

    async def snapshot(conn, job_card_id): return {"job_card_id": job_card_id}
    monkeypatch.setattr(jcv2, "get_job_card", snapshot)
    return calls


@pytest.mark.asyncio
async def test_unstarted_cards_go_with_the_plan(released):
    conn = _Conn(cards=[_card(1), _card(2, "locked"), _card(3, "material_received")],
                 lines=[_line(51, [11])])
    out = await plan_v2.cancel_plan(conn, PLAN, "  Customer cancelled the order ",
                                    cancelled_by="Asha P")
    assert out["cancelled"] is True and out["job_cards_cancelled"] == 3
    assert [a[0] for a in conn.cancelled_cards] == [1, 2, 3]
    jid, actor, reason, snapshot = conn.cancelled_cards[0]
    assert actor == "Asha P"
    assert reason == "[PLAN_CANCEL] Customer cancelled the order"
    assert '"job_card_id": 1' in snapshot
    assert released == [(51, [11], 100.0, 400.0)]
    assert out["released"] == {"kg": 100.0, "units": 400.0}
    # The reason is kept on the plan now (it used to be thrown away).
    assert conn.plan_update == (PLAN, "[cancelled by Asha P] Customer cancelled the order")


@pytest.mark.parametrize("status", ["in_progress", "paused", "completed", "closed"])
@pytest.mark.asyncio
async def test_started_work_refuses_the_cancel_before_anything_is_written(released, status):
    conn = _Conn(cards=[_card(1, status), _card(2, "locked")], lines=[_line(51, [11])])
    out = await plan_v2.cancel_plan(conn, PLAN, "Wrong article", cancelled_by="Asha P")
    assert out["error"] == "plan_has_started_cards"
    assert f"PLAN-{PLAN}-L1-S1 ({status.replace('_', ' ')})" in out["message"]
    assert "Stop or finish" in out["message"]
    assert out["job_cards"] == [{"job_card_id": 1, "job_card_number": f"PLAN-{PLAN}-L1-S1",
                                 "status": status}]
    assert conn.cancelled_cards == [] and released == [] and conn.plan_update is None


@pytest.mark.asyncio
async def test_a_merged_run_card_refuses_the_cancel(released):
    conn = _Conn(cards=[_card(1, group=44), _card(2)], lines=[_line(51, [11])])
    out = await plan_v2.cancel_plan(conn, PLAN, "Wrong article")
    assert out["error"] == "merged_run"
    assert conn.cancelled_cards == [] and conn.plan_update is None


@pytest.mark.asyncio
async def test_a_reason_is_required(released):
    conn = _Conn(cards=[_card(1)])
    out = await plan_v2.cancel_plan(conn, PLAN, "   ")
    assert out["error"] == "missing_reason"
    assert conn.cancelled_cards == [] and conn.plan_update is None


@pytest.mark.parametrize("status", [None, "cancelled"])
@pytest.mark.asyncio
async def test_missing_or_already_cancelled(released, status):
    out = await plan_v2.cancel_plan(_Conn(status=status), PLAN, "Wrong article")
    assert out["error"] == "not_found_or_already_cancelled"


@pytest.mark.asyncio
async def test_a_draft_with_no_cards_still_gives_its_qty_back(released):
    conn = _Conn(status="draft", lines=[_line(51, [11]), _line(52, [])])
    out = await plan_v2.cancel_plan(conn, PLAN, "Duplicate plan")
    assert out["job_cards_cancelled"] == 0
    assert [c[0] for c in released] == [51, 52]


@pytest.mark.asyncio
async def test_lines_tied_to_several_so_rows_give_back_first(released):
    """Their share of a row is worked out against the other lines' qty while
    those still hold it."""
    conn = _Conn(lines=[_line(51, [11]), _line(52, [11, 12]), _line(53, [13])])
    await plan_v2.cancel_plan(conn, PLAN, "Wrong article")
    assert [c[0] for c in released] == [52, 51, 53]


@pytest.mark.asyncio
async def test_delete_takes_the_cards_too(released):
    conn = _Conn(cards=[_card(1), _card(2, "locked")], lines=[_line(51, [11])])
    out = await plan_v2.delete_plan(conn, PLAN, reason="Entered twice", deleted_by="Asha P")
    assert out["deleted"] is True and out["job_cards_cancelled"] == 2
    assert conn.cancelled_cards[0][2] == "[PLAN_DELETE] Entered twice"
    assert conn.plan_update == (PLAN, "[deleted by Asha P] Entered twice")


@pytest.mark.asyncio
async def test_delete_is_refused_once_work_started(released):
    conn = _Conn(cards=[_card(1, "in_progress")], lines=[_line(51, [11])])
    out = await plan_v2.delete_plan(conn, PLAN, reason="Entered twice", deleted_by="Asha P")
    assert out["error"] == "plan_has_started_cards"
    assert conn.plan_update is None


@pytest.mark.asyncio
async def test_delete_only_takes_approved_plans(released):
    out = await plan_v2.delete_plan(_Conn(status="draft"), PLAN, reason="x", deleted_by="Asha P")
    assert out["error"] == "not_found_or_invalid_status"
