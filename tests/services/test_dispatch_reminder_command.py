"""The business head's manual trigger: replying "Expired" runs the day's dispatch scan.

The 9 AM loop only sends inside its window (09:00-09:59 IST), so a server that was down or
deployed across that hour sends nothing that day. This command is the recovery: it runs the
SAME scan_and_send the loop runs, so it is bound by the same send-once guard — typing it
after a successful morning run reports what is open rather than messaging anyone twice.

Two things are load-bearing and pinned below:
  * it must not fire while the business head is mid-way through a cancellation or redate,
    where "Expired" is the reason they were asked for, not a command.
  * it is the only inbound path that can push WhatsApp to a whole team, so a number that
    is not a business head must be refused before anything is sent.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_reminder_command.py
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.modules.sample.services import dispatch_reminder_service as drs
from app.modules.sample.services import whatsapp_service as wa

BH_PHONE = "919820000009"
TODAY = date(2026, 9, 4)


class _Conn:
    """Only the user lookup — the scan itself is stubbed out per test."""

    def __init__(self, *, role="business_head", known=True):
        self.role = role
        self.known = known

    async def fetchrow(self, query, *args):
        if "auth_user" in query:
            return {"user_id": 41, "role_name": self.role} if self.known else None
        if "wa_dispatch_pending" in query:
            return None
        raise AssertionError(f"unexpected fetchrow: {query[:70]}")


def _stub(monkeypatch, *, overdue=0, due=0, sent=0, migrated=True):
    """Stand in for the whole scan. Returns the list of texts sent to the BH."""
    texts: list[str] = []

    async def _send_text(to, text):
        texts.append(text)
        return {}

    async def _has_log_table(conn):
        return migrated

    async def _due_buckets(conn, today):
        assert today == TODAY
        return {"overdue": [{"id": i} for i in range(overdue)],
                "due_tomorrow": [{"id": i} for i in range(due)]}

    async def _scan(conn, *, today):
        assert today == TODAY
        return {"OVERDUE_NPD_WA": sent}

    monkeypatch.setattr(wa, "_send_text", _send_text)
    monkeypatch.setattr(drs, "has_log_table", _has_log_table)
    monkeypatch.setattr(drs, "due_buckets", _due_buckets)
    monkeypatch.setattr(drs, "scan_and_send", _scan)
    monkeypatch.setattr(drs, "ist_today", lambda: TODAY)
    return texts


def _run(monkeypatch, text, *, conn_kw=None, **stub):
    texts = _stub(monkeypatch, **stub)
    conn = _Conn(**(conn_kw or {}))
    res = asyncio.run(wa.handle_reminder_command(conn, BH_PHONE, text))
    return res, texts


# --- what counts as the command ----------------------------------------------

def test_expired_runs_the_scan(monkeypatch):
    res, texts = _run(monkeypatch, "Expired", overdue=4, sent=8)
    assert res["ok"] is True
    assert "4 overdue" in texts[0]


def test_the_word_is_case_insensitive(monkeypatch):
    res, _ = _run(monkeypatch, "expired", overdue=1, sent=2)
    assert res["ok"] is True


def test_surrounding_whitespace_is_ignored(monkeypatch):
    res, _ = _run(monkeypatch, "  EXPIRED \n", overdue=1, sent=2)
    assert res["ok"] is True


def test_free_text_containing_the_word_is_not_the_command(monkeypatch):
    """Otherwise "this one has expired, why?" — a question — would push WhatsApp to the
    whole NPD team."""
    res, texts = _run(monkeypatch, "why has this expired?")
    assert res is None and texts == []


def test_anything_else_falls_through_untouched(monkeypatch):
    """The None contract: handle_inbound must be free to try every flow below this one."""
    res, texts = _run(monkeypatch, "Approve")
    assert res is None and texts == []


def test_an_empty_message_is_not_the_command(monkeypatch):
    assert _run(monkeypatch, "")[0] is None


# --- who may run it -----------------------------------------------------------

def test_a_non_business_head_is_refused(monkeypatch):
    """This is the only inbound path that messages a whole team, so the role gate has to
    stop the send, not just the reply."""
    res, texts = _run(monkeypatch, "Expired", conn_kw={"role": "npd_team"})
    assert res["ok"] is False and res["reason"] == "forbidden"
    assert "business head" in texts[0].lower()


def test_an_unknown_number_is_refused(monkeypatch):
    res, texts = _run(monkeypatch, "Expired", conn_kw={"known": False})
    assert res["ok"] is False and res["reason"] == "unauthorised"
    assert texts


def test_admin_may_run_it_too(monkeypatch):
    """So the flow can be exercised without holding a business_head account."""
    res, _ = _run(monkeypatch, "Expired", conn_kw={"role": "admin"}, overdue=1, sent=2)
    assert res["ok"] is True


def test_a_refusal_never_reaches_the_scan(monkeypatch):
    scanned = []

    async def _boom(conn, *, today):
        scanned.append(today)
        return {}

    _stub(monkeypatch)
    monkeypatch.setattr(drs, "scan_and_send", _boom)
    asyncio.run(wa.handle_reminder_command(_Conn(role="sales"), BH_PHONE, "Expired"))
    assert scanned == []


# --- what it reports ----------------------------------------------------------

def test_a_send_reports_both_buckets(monkeypatch):
    _, texts = _run(monkeypatch, "Expired", overdue=4, due=2, sent=12)
    assert "4 overdue" in texts[0] and "2 due tomorrow" in texts[0]


def test_an_already_sent_day_says_so_instead_of_claiming_a_send(monkeypatch):
    """The guard held: 9 AM already ran. Reporting "sent" here would have the BH believe
    a second round went out, and reporting nothing would look like a broken command."""
    res, texts = _run(monkeypatch, "Expired", overdue=4, sent=0)
    assert res["sent"] == 0
    assert "already" in texts[0].lower() and "4 overdue" in texts[0]


def test_an_empty_day_says_nothing_is_pending(monkeypatch):
    res, texts = _run(monkeypatch, "Expired", overdue=0, due=0, sent=0)
    assert res["ok"] is True and res["sent"] == 0
    assert "nothing" in texts[0].lower()


# --- failure ------------------------------------------------------------------

def test_an_unmigrated_server_says_so_rather_than_reporting_a_send(monkeypatch):
    """087 is hand-applied. Without it scan_and_send returns {} — indistinguishable from
    "already sent today" unless it is checked first."""
    res, texts = _run(monkeypatch, "Expired", migrated=False)
    assert res["ok"] is False and res["reason"] == "unmigrated"
    assert "sent" not in texts[0].lower()


def test_a_failure_replies_rather_than_raising(monkeypatch):
    """handle_inbound's contract: the webhook must always answer Meta 200, so nothing
    below it may raise."""
    texts = _stub(monkeypatch)

    async def _boom(conn, *, today):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(drs, "scan_and_send", _boom)
    res = asyncio.run(wa.handle_reminder_command(_Conn(), BH_PHONE, "Expired"))
    assert res["ok"] is False and res["reason"] == "failed"
    assert texts and "again" in texts[0].lower()


# --- ordering inside handle_inbound -------------------------------------------

def test_a_pending_reason_capture_wins_over_the_command(monkeypatch):
    """A BH asked "why are you cancelling?" who answers "Expired" means it as the REASON.
    Running the command there would both lose the answer and blast the NPD team."""
    order: list[str] = []

    async def _dispatch(conn, wa_phone, text, context_id):
        order.append("dispatch")
        return {"ok": True, "awaiting": "cancel_reason"}

    async def _command(conn, wa_phone, text):
        order.append("command")
        return {"ok": True}

    monkeypatch.setattr(wa, "handle_dispatch_action", _dispatch)
    monkeypatch.setattr(wa, "handle_reminder_command", _command)
    res = asyncio.run(wa.handle_inbound(_Conn(), from_phone=BH_PHONE, text="Expired"))
    assert order == ["dispatch"]
    assert res["awaiting"] == "cancel_reason"


def test_handle_inbound_reaches_the_command(monkeypatch):
    """And when no capture is armed, the command still runs — ahead of the BH-approval and
    NPD-review flows, which would otherwise answer a bare word as a decision."""
    async def _dispatch(conn, wa_phone, text, context_id):
        return None

    called: list[str] = []

    async def _command(conn, wa_phone, text):
        called.append(text)
        return {"ok": True, "sent": 3}

    monkeypatch.setattr(wa, "handle_dispatch_action", _dispatch)
    monkeypatch.setattr(wa, "handle_reminder_command", _command)
    res = asyncio.run(wa.handle_inbound(_Conn(), from_phone=BH_PHONE, text="Expired"))
    assert called == ["Expired"]
    assert res["sent"] == 3
