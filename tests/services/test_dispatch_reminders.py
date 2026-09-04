"""Send-once guard for the NPD dispatch reminders.

The loop ticks hourly and may run on several replicas at once, so "did this mail
already go out today?" cannot be decided by reading a flag and then writing it —
two ticks would both read "no". The claim is the INSERT itself: the unique index
on (requisition_id, kind, sent_on) picks exactly one winner and only that caller
sends. These tests pin that, plus the hand-applied-migration no-op.

No DB: the connection is a stand-in that enforces the unique constraint in memory.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_reminders.py
"""
from __future__ import annotations

import asyncio
from datetime import date

import asyncpg
from asyncpg import exceptions as pg

from app.modules.sample.services import dispatch_reminder_service as svc

REQ = 25495623
DAY = date(2026, 9, 4)


class _Conn:
    """Stand-in enforcing UNIQUE(requisition_id, kind, sent_on) and UNIQUE(id)."""

    def __init__(self, *, has_table=True):
        self.has_table = has_table
        self.rows: list[dict] = []
        self.ids: set[int] = set()

    def transaction(self):
        class _T:
            async def __aenter__(self_inner): return None
            async def __aexit__(self_inner, *a): return False
        return _T()

    async def fetchval(self, query, *args):
        if "information_schema.tables" in query:
            return 1 if self.has_table else None
        if "INSERT INTO sample_dispatch_reminder_log" in query:
            _id, req_id, kind, day = args
            if _id in self.ids:
                raise pg.UniqueViolationError.new(
                    {"C": "23505", "M": "duplicate key",
                     "n": "sample_dispatch_reminder_log_pkey",
                     "t": "sample_dispatch_reminder_log"})
            if any(r["requisition_id"] == req_id and r["kind"] == kind and r["sent_on"] == day
                   for r in self.rows):
                return None                      # ON CONFLICT DO NOTHING
            self.ids.add(_id)
            self.rows.append({"id": _id, "requisition_id": req_id, "kind": kind, "sent_on": day})
            return _id
        raise AssertionError(f"unexpected fetchval: {query[:60]}")

    async def execute(self, query, *args):
        if "DELETE FROM sample_dispatch_reminder_log" in query:
            (req_id,) = args
            self.rows = [r for r in self.rows
                         if not (r["requisition_id"] == req_id and r["kind"].startswith("OVERDUE"))]
            return "DELETE"
        raise AssertionError(f"unexpected execute: {query[:60]}")


def test_first_claim_wins_and_second_loses_same_day():
    conn = _Conn()
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, DAY)) is True
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, DAY)) is False


def test_next_day_claims_again_so_the_chase_repeats():
    conn = _Conn()
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, DAY)) is True
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, date(2026, 9, 5))) is True


def test_kinds_are_independent():
    """The NPD copy going out must not suppress the business head's."""
    conn = _Conn()
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, DAY)) is True
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_OWNER, DAY)) is True


def test_requisitions_are_independent():
    conn = _Conn()
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_DUE_NPD, DAY)) is True
    assert asyncio.run(svc.claim(conn, 99999999, svc.KIND_DUE_NPD, DAY)) is True


def test_claim_retries_past_an_id_collision(monkeypatch):
    """id is an app-supplied 8-digit time id, not a SERIAL — a collision must retry
    with a fresh id, not be mistaken for 'already sent'."""
    conn = _Conn()
    conn.ids.add(1111)
    seq = iter([1111, 2222])
    monkeypatch.setattr(svc, "new_short_time_id", lambda: next(seq))
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_DUE_NPD, DAY)) is True
    assert conn.rows[0]["id"] == 2222


def test_unmigrated_reports_no_table():
    conn = _Conn(has_table=False)
    assert asyncio.run(svc.has_log_table(conn)) is False


def test_release_overdue_clears_only_overdue_rows():
    """A redate re-arms the chase against the NEW date; the due-tomorrow history stays."""
    conn = _Conn()
    for k in (svc.KIND_DUE_NPD, svc.KIND_OVERDUE_NPD, svc.KIND_OVERDUE_OWNER):
        asyncio.run(svc.claim(conn, REQ, k, DAY))
    asyncio.run(svc.release_overdue(conn, REQ))
    assert [r["kind"] for r in conn.rows] == [svc.KIND_DUE_NPD]


def test_ist_today_is_ahead_of_utc_across_the_boundary():
    """At 20:00 UTC it is already the next day in IST (+05:30). Using the server's
    date here would put 'tomorrow' on the wrong side for half the working day."""
    from datetime import datetime, timezone as _tz
    utc_evening = datetime(2026, 9, 4, 20, 0, tzinfo=_tz.utc)
    assert utc_evening.astimezone(svc.IST).date() == date(2026, 9, 5)
