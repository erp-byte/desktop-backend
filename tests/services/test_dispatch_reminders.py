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
from datetime import date, datetime, timedelta

import asyncpg
import pytest
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


# --- the scan ---------------------------------------------------------------

class _ScanConn:
    """Returns canned requisition rows and records the SQL + args it was given."""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.queries: list[str] = []
        self.args: tuple = ()

    async def fetch(self, query, *args):
        self.queries.append(query)
        self.args = args
        return self.rows


def _req(**kw):
    base = {"id": REQ, "request_id": REQ, "status": "BH_APPROVED",
            "expected_dispatch_date": DAY + timedelta(days=1), "sample_type": "NPD"}
    base.update(kw)
    return base


def test_scan_asks_only_for_open_requisitions_with_a_date():
    conn = _ScanConn([])
    asyncio.run(svc.due_buckets(conn, DAY))
    sql = conn.queries[0]
    assert "expected_dispatch_date IS NOT NULL" in sql
    assert "deleted_at IS NULL" in sql
    for st in svc.OPEN_STATUSES:
        assert st in sql
    # The five non-chased statuses must not be selectable.
    for st in ("INTERNALLY_DISPATCHED", "GATE_PASS_ISSUED", "CLOSED",
               "BH_REJECTED", "CANCELLED"):
        assert st not in sql


def test_scan_passes_the_ist_day_as_the_comparison_date():
    conn = _ScanConn([])
    asyncio.run(svc.due_buckets(conn, DAY))
    assert conn.args == (DAY,)


def test_due_tomorrow_bucket():
    conn = _ScanConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    out = asyncio.run(svc.due_buckets(conn, DAY))
    assert [r["id"] for r in out["due_tomorrow"]] == [REQ]
    assert out["overdue"] == []


def test_due_today_is_in_neither_bucket():
    """The warning is D-1 and the chase is D+1; the day itself is deliberately silent."""
    conn = _ScanConn([_req(expected_dispatch_date=DAY)])
    out = asyncio.run(svc.due_buckets(conn, DAY))
    assert out["due_tomorrow"] == [] and out["overdue"] == []


def test_overdue_bucket_carries_its_age():
    conn = _ScanConn([_req(expected_dispatch_date=DAY - timedelta(days=3))])
    out = asyncio.run(svc.due_buckets(conn, DAY))
    assert [r["overdue_days"] for r in out["overdue"]] == [3]


def test_far_future_is_in_neither_bucket():
    conn = _ScanConn([_req(expected_dispatch_date=DAY + timedelta(days=9))])
    out = asyncio.run(svc.due_buckets(conn, DAY))
    assert out["due_tomorrow"] == [] and out["overdue"] == []


# --- orchestration ----------------------------------------------------------

class _FullConn(_Conn):
    """Guard behaviour from _Conn, plus the scan's fetch and the per-kind row release
    scan_and_send uses to undo a claim whose mail reached nobody. _Conn.execute only
    knows the OVERDUE-prefix delete, so that second form is handled here."""

    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.scan_rows = [dict(r) for r in rows]

    async def fetch(self, query, *args):
        return self.scan_rows

    async def execute(self, query, *args):
        if "kind = $2 AND sent_on = $3" in query:
            req_id, kind, day = args
            self.rows = [r for r in self.rows
                         if not (r["requisition_id"] == req_id and r["kind"] == kind
                                 and r["sent_on"] == day)]
            return "DELETE"
        return await super().execute(query, *args)


def _stub_mail(monkeypatch, *, ok=True, raises=False):
    calls: list[tuple] = []

    async def _due(conn, req, *, audience):
        calls.append(("due", req["id"], audience))
        if raises:
            raise RuntimeError("mailer exploded")
        return ok

    async def _over(conn, req, *, days, audience):
        calls.append(("over", req["id"], audience, days))
        if raises:
            raise RuntimeError("mailer exploded")
        return ok

    async def _wa(conn, req):
        calls.append(("wa", req["id"], "npd"))
        if raises:
            raise RuntimeError("mailer exploded")
        return ok

    monkeypatch.setattr(svc, "notify_dispatch_due_tomorrow", _due)
    monkeypatch.setattr(svc, "notify_dispatch_overdue", _over)
    # The WhatsApp leg shares scan_and_send; stub it too so these mail tests neither
    # reach the Graph API nor resolve phones out of a connection that only knows
    # requisitions. Its own behaviour is covered in test_dispatch_reminder_whatsapp.py.
    monkeypatch.setattr(svc, "wa_notify_dispatch_due_tomorrow", _wa)
    return calls


def test_a_due_request_mails_both_audiences_once(monkeypatch):
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    out = asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert sorted(c[2] for c in calls if c[0] == "due") == ["npd", "owner"]
    assert out[svc.KIND_DUE_NPD] == 1 and out[svc.KIND_DUE_OWNER] == 1


def test_running_twice_in_a_day_sends_once(monkeypatch):
    """The hourly tick must not re-mail. This is the whole point of the guard."""
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert len(calls) == 2          # npd + owner, not four


def test_the_next_day_chases_again(monkeypatch):
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    asyncio.run(svc.scan_and_send(conn, today=DAY + timedelta(days=1)))
    assert len(calls) == 4


def test_a_failed_send_does_not_consume_the_day(monkeypatch):
    """notify_* returning False means nobody was addressed — it must be retried, so the
    guard row must not survive."""
    _stub_mail(monkeypatch, ok=False)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert conn.rows == []


def test_a_release_touches_only_the_failed_kind(monkeypatch):
    """The DELETE scan_and_send issues to undo a claim must target the exact
    (requisition_id, kind, sent_on) triple, not just the requisition — otherwise a release
    for one kind would also wipe out a sibling kind that legitimately sent."""
    async def _over(conn, req, *, days, audience):
        return audience == "npd"          # npd succeeds, owner fails
    monkeypatch.setattr(svc, "notify_dispatch_overdue", _over)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert [r["kind"] for r in conn.rows] == [svc.KIND_OVERDUE_NPD]


def test_a_raising_send_does_not_consume_the_day(monkeypatch):
    """A bug in the mailer must not permanently silence the chase: an exception out of
    notify_* is treated exactly like a False return — the guard row is released."""
    calls = _stub_mail(monkeypatch, raises=True)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    out = asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert calls          # the send was attempted
    assert conn.rows == []
    assert all(v == 0 for v in out.values())


def test_dry_run_sends_nothing_and_claims_nothing(monkeypatch):
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    out = asyncio.run(svc.scan_and_send(conn, today=DAY, dry_run=True))
    assert calls == [] and conn.rows == []
    assert out[svc.KIND_OVERDUE_NPD] == 1      # still reports what it WOULD send


def test_unmigrated_sends_nothing(monkeypatch):
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))], has_table=False)
    out = asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert calls == [] and out == {}


# --- the loop -----------------------------------------------------------------

class _FrozenDateTime(datetime):
    """A fixed IST clock (05:00) so the loop's hour gate is deterministic instead of
    depending on the real time the suite happens to run at."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 4, 5, 0, tzinfo=tz)


class _AcquireCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _FakePool:
    """dispatch_reminder_loop only ever does `async with pool.acquire() as conn`."""

    def acquire(self):
        return _AcquireCtx()


def _stub_scan(monkeypatch):
    """Replace scan_and_send itself: these tests are about the loop's own control flow
    (gating, ticking, resilience), not the scan, which has its own tests above."""
    calls: list[date] = []

    async def _scan(conn, *, today):
        calls.append(today)
        return {}

    monkeypatch.setattr(svc, "scan_and_send", _scan)
    return calls


def _sleep_raises_cancelled(monkeypatch):
    """Make the loop's own sleep the exit: it records the duration it was asked to sleep
    for, then raises CancelledError so the (otherwise infinite) loop stops after one pass —
    exactly what a real task cancellation on shutdown looks like from inside the loop."""
    sleeps: list[float] = []

    async def _sleep(s):
        sleeps.append(s)
        raise asyncio.CancelledError()

    monkeypatch.setattr(svc.asyncio, "sleep", _sleep)
    return sleeps


def test_loop_does_not_scan_before_the_configured_hour(monkeypatch):
    monkeypatch.setattr(svc, "datetime", _FrozenDateTime)   # frozen at 05:00 IST
    monkeypatch.setenv("SAMPLE_REMINDER_HOUR", "23")
    calls = _stub_scan(monkeypatch)
    _sleep_raises_cancelled(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.dispatch_reminder_loop(_FakePool()))
    assert calls == []


def test_loop_scans_immediately_on_startup(monkeypatch):
    """Regression for sleep-first: the loop must tick BEFORE its first sleep, or a process
    that recycles faster than the tick interval would never scan at all."""
    monkeypatch.setattr(svc, "datetime", _FrozenDateTime)   # frozen at 05:00 IST
    monkeypatch.setenv("SAMPLE_REMINDER_HOUR", "5")          # already past the gate
    calls = _stub_scan(monkeypatch)
    sleeps = _sleep_raises_cancelled(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.dispatch_reminder_loop(_FakePool()))
    assert len(calls) == 1        # scanned before the sleep that stopped the loop ran
    assert sleeps                 # and still reached the sleep afterwards


def test_loop_stops_cleanly_on_cancellation(monkeypatch):
    """CancelledError out of asyncio.sleep must propagate out of the loop rather than being
    swallowed by the per-tick except Exception, so task cancellation on shutdown works."""
    monkeypatch.setattr(svc, "datetime", _FrozenDateTime)
    monkeypatch.setenv("SAMPLE_REMINDER_HOUR", "0")
    _stub_scan(monkeypatch)
    _sleep_raises_cancelled(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.dispatch_reminder_loop(_FakePool()))


def test_loop_honours_the_tick_floor(monkeypatch):
    """A misconfigured tiny SAMPLE_REMINDER_TICK_MIN must not turn this into a tight poll
    loop — the floor is 15 minutes regardless of what the env asks for."""
    monkeypatch.setenv("SAMPLE_REMINDER_TICK_MIN", "1")
    monkeypatch.setattr(svc, "datetime", _FrozenDateTime)
    monkeypatch.setenv("SAMPLE_REMINDER_HOUR", "0")
    _stub_scan(monkeypatch)
    sleeps = _sleep_raises_cancelled(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.dispatch_reminder_loop(_FakePool()))
    assert sleeps == [15 * 60]


def test_loop_respects_the_kill_switch(monkeypatch):
    monkeypatch.setenv("SAMPLE_REMINDER_ENABLED", "0")
    monkeypatch.setattr(svc, "datetime", _FrozenDateTime)
    monkeypatch.setenv("SAMPLE_REMINDER_HOUR", "0")   # would scan if it weren't disabled
    calls = _stub_scan(monkeypatch)
    _sleep_raises_cancelled(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.dispatch_reminder_loop(_FakePool()))
    assert calls == []


def test_loop_survives_an_exception_in_the_tick(monkeypatch):
    """A bad tick (e.g. scan_and_send hitting a DB error) must be caught and logged, not
    left to kill the loop — it should still reach its next sleep."""
    async def _boom(conn, *, today):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(svc, "scan_and_send", _boom)
    monkeypatch.setattr(svc, "datetime", _FrozenDateTime)
    monkeypatch.setenv("SAMPLE_REMINDER_HOUR", "0")
    sleeps = _sleep_raises_cancelled(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.dispatch_reminder_loop(_FakePool()))
    assert sleeps          # reached the sleep despite the exception
