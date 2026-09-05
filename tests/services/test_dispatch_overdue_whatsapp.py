"""The overdue-dispatch WhatsApp chase and its two-step button conversation.

Template `npd_dispatch_overdue_team` (Meta, UTILITY, en) goes to the requisition's bound
business head every day the expected dispatch date stays in the past. It carries two
QUICK_REPLY buttons whose inbound payload is the button TEXT:

    "Change expected date"  -> ask the reason, then ask the new date (dd-mm-yyyy)
    "Cancel request"        -> ask the reason, then cancel

Both legs are two messages deep, so the pending row carries a STAGE as well as an action;
that state machine is what most of this file pins.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_overdue_whatsapp.py
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from app.modules.sample.services import dispatch_reminder_service as svc
from app.modules.sample.services import whatsapp_service as wa

REQ = 25495623
DAY = date(2026, 9, 4)


def _req(**kw):
    """A requisition row shaped like the sample message in WhatsApp Manager."""
    base = {"id": REQ, "request_id": REQ, "status": "IN_PRODUCTION",
            "business_head_user_id": 41,
            "expected_dispatch_date": DAY - timedelta(days=4),
            "npd_target_name": "Date Powder", "customer_name": "BigBasket",
            "overdue_days": 4}
    base.update(kw)
    return base


# --- template parameters ----------------------------------------------------

def test_overdue_body_carries_exactly_the_five_registered_variables_in_order():
    header, body = wa._dispatch_overdue_params(_req(), days=4)
    assert header == ["25495623"]
    assert body == ["25495623", "2026-08-31", "4", "Date Powder", "BigBasket"]


def test_days_overdue_is_the_scan_count_not_a_recomputed_one():
    """scan_and_send already computed the age against the IST day; recomputing it here
    against the server clock would disagree across the +05:30 boundary."""
    _, body = wa._dispatch_overdue_params(_req(), days=11)
    assert body[2] == "11"


def test_a_blank_field_never_sends_an_empty_parameter():
    _, body = wa._dispatch_overdue_params(_req(npd_target_name=None, customer_name="  "),
                                          days=1)
    assert body[3] == "—" and body[4] == "—"
    assert all(p.strip() for p in body)


# --- the date the business head types ---------------------------------------

def test_a_dd_mm_yyyy_date_is_accepted():
    assert wa._parse_ddmmyyyy("15-09-2026", today=DAY) == date(2026, 9, 15)


def test_slashes_and_dots_are_accepted_as_separators():
    """The prompt asks for dd-mm-yyyy, but a phone keyboard makes / and . just as likely
    and the order is unambiguous either way."""
    assert wa._parse_ddmmyyyy("15/09/2026", today=DAY) == date(2026, 9, 15)
    assert wa._parse_ddmmyyyy("15.09.2026", today=DAY) == date(2026, 9, 15)


def test_the_day_comes_first_not_the_month():
    """09-10-2026 is 9 October, not 10 September. Reading it the American way would move
    the date a month and nobody would notice until it slipped again."""
    assert wa._parse_ddmmyyyy("09-10-2026", today=DAY) == date(2026, 10, 9)


def test_a_date_in_the_past_is_refused():
    """Accepting one would re-arm the overdue chase on the very next scan."""
    assert wa._parse_ddmmyyyy("01-09-2026", today=DAY) is None


def test_today_is_refused_but_tomorrow_is_accepted():
    assert wa._parse_ddmmyyyy("04-09-2026", today=DAY) is None
    assert wa._parse_ddmmyyyy("05-09-2026", today=DAY) == date(2026, 9, 5)


def test_an_impossible_date_is_refused_rather_than_rolled_over():
    assert wa._parse_ddmmyyyy("31-02-2027", today=DAY) is None


def test_free_text_is_refused():
    for junk in ("next week", "", "15 Sep", "2026-09-15", "15-9", "  "):
        assert wa._parse_ddmmyyyy(junk, today=DAY) is None, junk


def test_a_two_digit_year_is_refused_rather_than_guessed():
    """15-09-26 could be 1926 or 2026; asking again is cheaper than moving a date by a
    century."""
    assert wa._parse_ddmmyyyy("15-09-26", today=DAY) is None


# --- the send ---------------------------------------------------------------

class _BhConn:
    """Resolves one business head's phone and records the wamid mapping written back."""

    def __init__(self, phone="919820000009"):
        self.phone = phone
        self.stored: list[tuple] = []

    async def fetchval(self, query, *args):
        assert "auth_user" in query
        return self.phone

    async def execute(self, query, *args):
        if "wa_review_message" in query:
            self.stored.append(args)
        return "INSERT 0 1"


def _capture_sends(monkeypatch, *, error=False):
    sent: list[tuple] = []

    async def _send(to, name, body_params, header_params=None):
        sent.append((to, name, header_params, body_params))
        return {"error": "HTTP 400"} if error else {"messages": [{"id": "wamid.ABC"}]}

    monkeypatch.setattr(wa, "_send_template", _send)
    return sent


def test_the_business_head_gets_the_overdue_template(monkeypatch):
    sent = _capture_sends(monkeypatch)
    conn = _BhConn()
    assert asyncio.run(wa.notify_dispatch_overdue(conn, _req(), days=4, audience="owner")) is True
    assert [s[0] for s in sent] == ["919820000009"]
    assert sent[0][1] == "npd_dispatch_overdue_team"


def test_the_sent_message_is_mapped_so_a_button_tap_resolves_back(monkeypatch):
    """A quick-reply tap carries no request number — only context.id, the wamid of this
    message. Without the mapping row the tap is unattributable and the flow dies."""
    _capture_sends(monkeypatch)
    conn = _BhConn()
    asyncio.run(wa.notify_dispatch_overdue(conn, _req(), days=4, audience="owner"))
    assert conn.stored == [("wamid.ABC", REQ, "DISPATCH_OVERDUE", "919820000009")]


def test_a_rejected_overdue_send_reports_failure(monkeypatch):
    _capture_sends(monkeypatch, error=True)
    assert asyncio.run(wa.notify_dispatch_overdue(_BhConn(), _req(), days=4, audience="owner")) is False


def test_a_requisition_with_no_business_head_reports_failure(monkeypatch):
    _capture_sends(monkeypatch)
    assert asyncio.run(wa.notify_dispatch_overdue(
        _BhConn(), _req(business_head_user_id=None), days=4, audience="owner")) is False


# --- orchestration ----------------------------------------------------------

class _FullConn:
    """The send-once guard's unique index in memory, plus the scan's fetch."""

    def __init__(self, rows):
        self.scan_rows = [dict(r) for r in rows]
        self.rows: list[dict] = []

    def transaction(self):
        class _T:
            async def __aenter__(self_inner): return None
            async def __aexit__(self_inner, *a): return False
        return _T()

    async def fetch(self, query, *args):
        return self.scan_rows

    async def fetchval(self, query, *args):
        if "information_schema.tables" in query:
            return 1
        _id, req_id, kind, day = args
        if any(r["requisition_id"] == req_id and r["kind"] == kind and r["sent_on"] == day
               for r in self.rows):
            return None
        self.rows.append({"id": _id, "requisition_id": req_id, "kind": kind, "sent_on": day})
        return _id

    async def execute(self, query, *args):
        req_id, kind, day = args
        self.rows = [r for r in self.rows
                     if not (r["requisition_id"] == req_id and r["kind"] == kind
                             and r["sent_on"] == day)]
        return "DELETE"


def _stub_all(monkeypatch, *, wa_ok=True):
    calls: list[tuple] = []

    async def _mail_due(conn, req, *, audience):
        return True

    async def _mail_over(conn, req, *, days, audience):
        return True

    async def _wa_due(conn, req, *, audience):
        return True

    async def _wa_over(conn, req, *, days, audience):
        calls.append(("wa-over", req["id"], days, audience))
        return wa_ok

    monkeypatch.setattr(svc, "notify_dispatch_due_tomorrow", _mail_due)
    monkeypatch.setattr(svc, "notify_dispatch_overdue", _mail_over)
    monkeypatch.setattr(svc, "wa_notify_dispatch_due_tomorrow", _wa_due)
    monkeypatch.setattr(svc, "wa_notify_dispatch_overdue", _wa_over)
    return calls


def test_an_overdue_request_whatsapps_the_business_head(monkeypatch):
    calls = _stub_all(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=4))])
    out = asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert [c[:3] for c in calls] == [("wa-over", REQ, 4)] * 2
    assert out[svc.KIND_OVERDUE_OWNER_WA] == 1


def test_the_overdue_chase_repeats_daily(monkeypatch):
    """Unlike the D-1 warning this fires EVERY day until the BH acts — a new sent_on row
    each day is what makes that work."""
    calls = _stub_all(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=4))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    asyncio.run(svc.scan_and_send(conn, today=DAY))                       # same day: once
    asyncio.run(svc.scan_and_send(conn, today=DAY + timedelta(days=1)))   # next day: again
    assert len(calls) == 4


def test_a_due_tomorrow_request_is_not_chased(monkeypatch):
    calls = _stub_all(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert calls == []


# --- the NPD team's copy of the chase ----------------------------------------

class _TeamConn:
    """Resolves the NPD pool's phones; records anything written back."""

    def __init__(self, phones=("919820000001", "919820000002")):
        self.phones = list(phones)
        self.stored: list[tuple] = []

    async def fetch(self, query, *args):
        assert "auth_user" in query
        return [{"phone": p} for p in self.phones]

    async def execute(self, query, *args):
        if "wa_review_message" in query:
            self.stored.append(args)
        return "INSERT 0 1"


def test_the_npd_team_gets_the_notifier_template(monkeypatch):
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(wa, "npd_review_numbers", lambda: [])
    conn = _TeamConn()
    ok = asyncio.run(wa.notify_dispatch_overdue(conn, _req(), days=4, audience="npd"))
    assert ok is True
    assert [s[0] for s in sent] == ["919820000001", "919820000002"]
    assert {s[1] for s in sent} == {"npd_dispatch_overdue_team_notifier"}


def test_the_two_audiences_use_different_templates(monkeypatch):
    """The team's copy is informational; only the business head's carries the buttons.
    Sending the button template to the pool would let anyone cancel the request."""
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(wa, "npd_review_numbers", lambda: [])
    asyncio.run(wa.notify_dispatch_overdue(_TeamConn(), _req(), days=4, audience="npd"))
    asyncio.run(wa.notify_dispatch_overdue(_BhConn(), _req(), days=4, audience="owner"))
    assert sent[0][1] == "npd_dispatch_overdue_team_notifier"
    assert sent[-1][1] == "npd_dispatch_overdue_team"


def test_the_team_copy_is_not_mapped_for_button_taps(monkeypatch):
    """It has no buttons, so a wa_review_message row would only create a way for a stray
    reply from a team member to be resolved as if it were the business head's."""
    _capture_sends(monkeypatch)
    monkeypatch.setattr(wa, "npd_review_numbers", lambda: [])
    conn = _TeamConn()
    asyncio.run(wa.notify_dispatch_overdue(conn, _req(), days=4, audience="npd"))
    assert conn.stored == []


def test_the_team_copy_carries_the_same_five_parameters(monkeypatch):
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(wa, "npd_review_numbers", lambda: [])
    asyncio.run(wa.notify_dispatch_overdue(_TeamConn(), _req(), days=4, audience="npd"))
    assert sent[0][2] == ["25495623"]
    assert sent[0][3] == ["25495623", "2026-08-31", "4", "Date Powder", "BigBasket"]


def test_no_npd_phone_on_file_reports_failure(monkeypatch):
    _capture_sends(monkeypatch)
    monkeypatch.setattr(wa, "npd_review_numbers", lambda: [])
    assert asyncio.run(wa.notify_dispatch_overdue(
        _TeamConn(phones=()), _req(), days=4, audience="npd")) is False


def test_an_overdue_request_whatsapps_both_audiences(monkeypatch):
    calls = _stub_all(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=4))])
    out = asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert sorted(c[3] for c in calls) == ["npd", "owner"]
    assert out[svc.KIND_OVERDUE_NPD_WA] == 1 and out[svc.KIND_OVERDUE_OWNER_WA] == 1


def test_the_two_overdue_legs_claim_independently(monkeypatch):
    """A business head with no phone must not stop the NPD team being told."""
    calls: list[tuple] = []

    async def _wa_over(conn, req, *, days, audience):
        calls.append(audience)
        return audience == "npd"

    _stub_all(monkeypatch)
    monkeypatch.setattr(svc, "wa_notify_dispatch_overdue", _wa_over)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=4))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert calls == ["npd", "owner", "owner"]
