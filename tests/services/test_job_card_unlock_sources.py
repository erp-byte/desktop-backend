"""Every service that can unlock a job card reports WHICH card it unlocked — the
downstream card that was waiting on the previous stage, never the card whose
batch closed or whose material was dispatched — and only when that card really
went from locked to unlocked. The unlock notice is addressed from these ids.

No database: each fake connection answers only the statements its service sends.
"""
from __future__ import annotations

import asyncio

import pytest

from app.modules.production.services import job_card_batch_v2 as batch_svc
from app.modules.production.services import job_card_v2 as jcv2

UPSTREAM, DOWNSTREAM = 7001, 7002


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class _InTx:
    """The services run inside the route's transaction; say so to the helpers."""

    def transaction(self):
        return _Tx()

    def is_in_transaction(self):
        return True


@pytest.fixture(autouse=True)
def _no_wip_materialisation(monkeypatch):
    """WIP stock materialisation is its own concern; stub it out here."""
    async def _materialise(conn, **kw):
        return None
    monkeypatch.setattr(jcv2, "materialise_wip_dispatch", _materialise)


# ── close_batch: the batch's own card dispatches, the NEXT card unlocks ─────

class _CloseConn(_InTx):
    def __init__(self, downstream_was_waiting: bool):
        self.waiting = downstream_was_waiting

    async def fetchval(self, sql, *args):
        s = _norm(sql)
        if s.startswith("SELECT job_card_id FROM job_card_batch_v2"):
            return UPSTREAM
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    async def fetchrow(self, sql, *args):
        s = _norm(sql)
        if s.startswith("SELECT batch_id, job_card_id, batch_number, status, notes"):
            return {"batch_id": 55, "job_card_id": UPSTREAM, "batch_number": 1, "status": "open", "notes": None}
        if "FROM job_card_v2" in s and s.startswith("SELECT job_card_id, next_job_card_id"):
            return {"job_card_id": UPSTREAM, "next_job_card_id": DOWNSTREAM, "output_kind": "WIP",
                    "uom": "kg", "dispatched_to_next_kg": 0, "entity": "cfpl", "output_code": None,
                    "fg_sku_name": "Roasted Almonds 200g"}
        if s.startswith("UPDATE job_card_batch_v2"):
            return {"batch_id": 55, "job_card_id": UPSTREAM, "status": "closed"}
        if "INSERT INTO job_card_output_v2" in s:
            return {"output_id": 1, "job_card_id": UPSTREAM}
        if "INSERT INTO job_card_partial_dispatch_v2" in s:
            return {"dispatch_id": args[0], "from_job_card_id": args[1], "to_job_card_id": args[2],
                    "qty_kg": args[3]}
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def execute(self, sql, *args):
        s = _norm(sql)
        if "locked_reason = 'awaiting_previous_stage'" in s and "SET is_locked = FALSE" in s:
            return "UPDATE 1" if self.waiting else "UPDATE 0"
        return "UPDATE 1"


def _close(conn):
    return asyncio.run(batch_svc.close_batch(
        conn, batch_id=55, job_card_id=UPSTREAM, produced_qty_kg=149.8, rm_consumed_kg=0.0,
        allow_unbalanced=True, closed_by="Ravi K"))


@pytest.fixture
def _unlocked_upstream(monkeypatch):
    async def _not_locked(conn, job_card_id):
        return None
    monkeypatch.setattr(batch_svc, "assert_not_locked", _not_locked)


def test_closing_a_batch_reports_the_downstream_card_it_unlocked(_unlocked_upstream):
    result = _close(_CloseConn(downstream_was_waiting=True))
    assert result["downstream_unlocked"] is True
    assert result["unlocked_job_card_id"] == DOWNSTREAM        # the card that can now start
    assert result["unlocked_job_card_id"] != UPSTREAM          # not the one whose batch closed


def test_closing_a_batch_when_the_next_card_was_already_open_reports_no_unlock(_unlocked_upstream):
    result = _close(_CloseConn(downstream_was_waiting=False))
    assert result["downstream_unlocked"] is False
    assert result["unlocked_job_card_id"] is None


# ── dispatch_to_next ────────────────────────────────────────────────────────

class _DispatchConn(_InTx):
    def __init__(self, downstream_status: str, downstream_reason):
        self.cards = {
            UPSTREAM: {"job_card_id": UPSTREAM, "next_job_card_id": DOWNSTREAM, "planned_qty_kg": 150,
                       "dispatched_to_next_kg": 0, "output_kind": "WIP", "status": "in_progress",
                       "entity": "cfpl", "output_code": None, "fg_sku_name": "Roasted Almonds 200g"},
            DOWNSTREAM: {"job_card_id": DOWNSTREAM, "status": downstream_status,
                         "is_locked": downstream_status == "locked", "locked_reason": downstream_reason},
        }

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        s = _norm(sql)
        if "INSERT INTO job_card_partial_dispatch_v2" in s:
            return {"dispatch_id": args[0], "from_job_card_id": args[1], "to_job_card_id": args[2],
                    "qty_kg": args[3]}
        if "FROM job_card_v2" in s:
            return self.cards.get(args[0])
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = _norm(sql)
        if "FROM job_card_output_v2" in s:
            return 149.8
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    async def execute(self, sql, *args):
        return "UPDATE 1"


def _dispatch(conn):
    return asyncio.run(jcv2.dispatch_to_next(conn, job_card_id=UPSTREAM, qty_kg=149.8, dispatched_by="Ravi K"))


def test_dispatching_reports_the_downstream_card_it_unlocked():
    result = _dispatch(_DispatchConn("locked", "awaiting_previous_stage"))
    assert result["dispatched"] is True
    assert result["unlocked_job_card_id"] == DOWNSTREAM


@pytest.mark.parametrize("status, reason", [
    ("unlocked", None),                       # already released by an earlier dispatch
    ("in_progress", None),                    # already running
    ("locked", "discrepancy"),                # locked for another reason: dispatch does not release it
])
def test_dispatching_into_a_card_that_was_not_waiting_reports_no_unlock(status, reason):
    result = _dispatch(_DispatchConn(status, reason))
    assert result["dispatched"] is True
    assert result["unlocked_job_card_id"] is None


# ── dispatch_process_group: one process card feeds several packaging cards ──

class _GroupConn(_InTx):
    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        s = _norm(sql)
        if "INSERT INTO job_card_partial_dispatch_v2" in s:
            return {"dispatch_id": args[0], "from_job_card_id": args[1], "to_job_card_id": args[2],
                    "qty_kg": args[3]}
        if "FROM job_card_v2" in s:
            return {"job_card_id": 8000, "output_kind": "WIP", "output_code": None,
                    "fg_sku_name": "Roasted Almonds", "entity": "cfpl", "status": "in_progress",
                    "dispatched_to_next_kg": 0, "process_group_id": 9}
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetchval(self, sql, *args):
        return 100.0

    async def fetch(self, sql, *args):
        return [
            {"job_card_id": 8001, "plan_line_id": 1, "planned_qty_kg": 50, "planned_qty_units": None,
             "carried_qty_kg": 0, "status": "locked", "locked_reason": "awaiting_previous_stage"},
            {"job_card_id": 8002, "plan_line_id": 2, "planned_qty_kg": 50, "planned_qty_units": None,
             "carried_qty_kg": 0, "status": "unlocked", "locked_reason": None},
        ]

    async def execute(self, sql, *args):
        return "UPDATE 1"


def test_a_group_dispatch_reports_only_the_packaging_cards_it_unlocked():
    result = asyncio.run(jcv2.dispatch_process_group(_GroupConn(), process_job_card_id=8000,
                                                     dispatched_by="Ravi K"))
    assert result["unlocked_job_card_ids"] == [8001]
    assert [(r["packaging_job_card_id"], r["unlocked"]) for r in result["results"]] == [
        (8001, True), (8002, False)]


# ── live chain edit: a locked card that became the head is released ─────────

class _HeadConn:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((_norm(sql), args))
        return "UPDATE 1"


def test_a_locked_card_that_becomes_the_head_is_unlocked_and_reported():
    conn = _HeadConn()
    assert asyncio.run(jcv2._unlock_new_head(conn, 9101, "locked")) == 9101
    assert len(conn.executed) == 1 and conn.executed[0][1] == (9101,)


def test_a_head_that_was_already_running_is_left_alone():
    conn = _HeadConn()
    assert asyncio.run(jcv2._unlock_new_head(conn, 9101, "in_progress")) is None
    assert conn.executed == []
