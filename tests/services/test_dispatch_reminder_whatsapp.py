"""The daily WhatsApp leg of the NPD dispatch reminders.

Template `npd_dispatch_due_tomorrow_team` (Meta, UTILITY, en) is sent to the NPD team
the day before a sample requisition's expected dispatch date. Two things are pinned
here because getting either wrong is invisible until Meta rejects the send:

  - the parameter SHAPE: 1 header var + 6 body vars, in the order the registered
    template numbers them. Meta substitutes positionally, so a reordered list renders
    a plausible-looking but wrong message rather than erroring.
  - the parameter CONTENT: Meta rejects the whole message (not just the field) when a
    body param is empty or carries a newline, a tab, or a run of >4 spaces.

The claim kind is deliberately its own (DUE_TOMORROW_NPD_WA) so a WhatsApp failure
retries on the next tick instead of being suppressed by the email having succeeded.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_reminder_whatsapp.py
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
    base = {"id": REQ, "request_id": REQ, "status": "BH_APPROVED",
            "expected_dispatch_date": DAY + timedelta(days=2),
            "npd_target_name": "Date Powder", "quantity": 3.1,
            "customer_name": "BigBasket", "warehouse": "W202"}
    base.update(kw)
    return base


# --- template parameters ----------------------------------------------------

def test_body_carries_exactly_the_six_registered_variables_in_order():
    header, body = wa._dispatch_due_team_params(_req())
    assert header == ["25495623"]
    assert body == ["25495623", "2026-09-06", "Date Powder", "3.1", "BigBasket", "W202"]


def test_expected_dispatch_renders_as_a_bare_date():
    """expected_dispatch_date can come back as a datetime; the template line must read
    "Expected dispatch: 2026-09-06", not a timestamp with a time and an offset."""
    _, body = wa._dispatch_due_team_params(_req(
        expected_dispatch_date=datetime(2026, 9, 6, 14, 30)))
    assert body[1] == "2026-09-06"


def test_quantity_drops_trailing_zeros():
    """The template's own text supplies the "kg"; the parameter is just the number."""
    _, body = wa._dispatch_due_team_params(_req(quantity=3.100))
    assert body[3] == "3.1"


def test_a_blank_field_never_sends_an_empty_parameter():
    """Meta rejects the whole message on an empty body param, so a requisition with no
    customer must still deliver - reading "Customer: -"."""
    _, body = wa._dispatch_due_team_params(_req(customer_name=None, warehouse="  "))
    assert body[4] == "—" and body[5] == "—"
    assert all(p.strip() for p in body)


def test_multiline_values_are_collapsed_to_one_line():
    """Meta rejects a body param containing a newline, a tab, or a run of >4 spaces."""
    _, body = wa._dispatch_due_team_params(_req(npd_target_name="Date\nPowder\t500    g"))
    assert body[2] == "Date Powder 500 g"


# --- the send ---------------------------------------------------------------

class _WaConn:
    """Resolves NPD phones and swallows the wa_review_message bookkeeping."""

    def __init__(self, phones=("919820000001", "919820000002")):
        self.phones = list(phones)
        self.executed: list[str] = []

    async def fetch(self, query, *args):
        assert "auth_user" in query
        return [{"phone": p} for p in self.phones]

    async def execute(self, query, *args):
        self.executed.append(query)
        return "INSERT 0 1"


def _capture_sends(monkeypatch, *, error=False):
    sent: list[tuple] = []

    async def _send(to, name, body_params, header_params=None):
        sent.append((to, name, header_params, body_params))
        return {"error": "HTTP 400"} if error else {"messages": [{"id": "wamid." + to}]}

    monkeypatch.setattr(wa, "_send_template", _send)
    monkeypatch.setattr(wa, "npd_review_numbers", lambda: [])
    return sent


def test_every_npd_number_gets_the_registered_template(monkeypatch):
    sent = _capture_sends(monkeypatch)
    assert asyncio.run(wa.notify_dispatch_due_tomorrow(_WaConn(), _req(), audience="npd")) is True
    assert [s[0] for s in sent] == ["919820000001", "919820000002"]
    assert {s[1] for s in sent} == {"npd_dispatch_due_tomorrow_team"}


def test_a_rejected_send_reports_failure_so_the_day_is_not_consumed(monkeypatch):
    _capture_sends(monkeypatch, error=True)
    assert asyncio.run(wa.notify_dispatch_due_tomorrow(_WaConn(), _req(), audience="npd")) is False


def test_no_npd_phone_on_file_reports_failure(monkeypatch):
    """Returning True here would burn the claim and silence the request for the day."""
    _capture_sends(monkeypatch)
    assert asyncio.run(wa.notify_dispatch_due_tomorrow(_WaConn(phones=()), _req(), audience="npd")) is False


# --- orchestration ----------------------------------------------------------

class _FullConn:
    """The guard's unique index in memory, plus the scan's fetch."""

    def __init__(self, rows):
        self.scan_rows = [dict(r) for r in rows]
        self.rows: list[dict] = []
        self.ids: set[int] = set()

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
            return None                          # ON CONFLICT DO NOTHING
        self.ids.add(_id)
        self.rows.append({"id": _id, "requisition_id": req_id, "kind": kind, "sent_on": day})
        return _id

    async def execute(self, query, *args):
        req_id, kind, day = args
        self.rows = [r for r in self.rows
                     if not (r["requisition_id"] == req_id and r["kind"] == kind
                             and r["sent_on"] == day)]
        return "DELETE"


def _stub_all(monkeypatch, *, wa_ok=True, mail_ok=True):
    calls: list[tuple] = []

    async def _due(conn, req, *, audience):
        calls.append(("mail", audience))
        return mail_ok

    async def _over(conn, req, *, days, audience):
        calls.append(("mail-over", audience))
        return mail_ok

    async def _wa(conn, req, *, audience):
        calls.append(("wa", audience))
        return wa_ok

    monkeypatch.setattr(svc, "notify_dispatch_due_tomorrow", _due)
    monkeypatch.setattr(svc, "notify_dispatch_overdue", _over)
    monkeypatch.setattr(svc, "wa_notify_dispatch_due_tomorrow", _wa)
    return calls


def test_a_due_request_whatsapps_the_npd_team(monkeypatch):
    calls = _stub_all(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    out = asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert ("wa", "npd") in calls
    assert out[svc.KIND_DUE_NPD_WA] == 1


def test_whatsapp_goes_out_once_a_day(monkeypatch):
    calls = _stub_all(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert [c for c in calls if c == ("wa", "npd")] == [("wa", "npd")]


def test_a_failed_whatsapp_retries_without_re_mailing(monkeypatch):
    """The two channels claim separately: WhatsApp failing must not re-send the email,
    and the email succeeding must not silence WhatsApp."""
    calls = _stub_all(monkeypatch, wa_ok=False)
    conn = _FullConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert len([c for c in calls if c == ("wa", "npd")]) == 2   # retried
    assert len([c for c in calls if c[0] == "mail"]) == 2       # npd + owner, sent once


def test_an_overdue_request_is_not_whatsapped(monkeypatch):
    """Only the D-1 warning has a Meta template; the overdue chase stays email-only."""
    calls = _stub_all(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=2))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert not [c for c in calls if c[0] == "wa"]


# --- the business-head copy -------------------------------------------------

class _BhConn:
    """Resolves one business head's phone off auth_user."""

    def __init__(self, phone="919820000009"):
        self.phone = phone

    async def fetchval(self, query, *args):
        assert "auth_user" in query
        return self.phone

    async def execute(self, query, *args):
        return "INSERT 0 1"


def test_owner_body_carries_exactly_the_four_registered_variables_in_order():
    header, body = wa._dispatch_due_owner_params(_req())
    assert header == ["25495623"]
    assert body == ["25495623", "2026-09-06", "Date Powder", "BigBasket"]


def test_the_owner_copy_omits_quantity_and_warehouse():
    """npd_dispatch_due_tomorrow_owner is deliberately shorter than the team copy - 4
    body vars, not 6. Meta rejects a send whose parameter count misses the template."""
    _, body = wa._dispatch_due_owner_params(_req())
    assert len(body) == 4


def test_the_business_head_gets_the_owner_template(monkeypatch):
    sent = _capture_sends(monkeypatch)
    ok = asyncio.run(wa.notify_dispatch_due_tomorrow(
        _BhConn(), _req(business_head_user_id=7), audience="owner"))
    assert ok is True
    assert [s[0] for s in sent] == ["919820000009"]
    assert sent[0][1] == "npd_dispatch_due_tomorrow_owner"


def test_the_owner_copy_never_reaches_the_npd_pool(monkeypatch):
    """Two audiences, two templates, two recipient sets - the BH send must not fan out
    to the team numbers, or the BH copy lands on everyone."""
    sent = _capture_sends(monkeypatch)
    asyncio.run(wa.notify_dispatch_due_tomorrow(
        _BhConn(), _req(business_head_user_id=7), audience="owner"))
    assert len(sent) == 1


def test_a_requisition_with_no_business_head_reports_failure(monkeypatch):
    """Requisitions raised before the 086 sign-off feature carry no business_head_user_id.
    Reporting success would record a warning nobody received."""
    sent = _capture_sends(monkeypatch)
    ok = asyncio.run(wa.notify_dispatch_due_tomorrow(
        _BhConn(), _req(business_head_user_id=None), audience="owner"))
    assert ok is False and sent == []


def test_a_business_head_with_no_phone_reports_failure(monkeypatch):
    _capture_sends(monkeypatch)
    ok = asyncio.run(wa.notify_dispatch_due_tomorrow(
        _BhConn(phone=None), _req(business_head_user_id=7), audience="owner"))
    assert ok is False


def test_a_rejected_owner_send_reports_failure(monkeypatch):
    _capture_sends(monkeypatch, error=True)
    ok = asyncio.run(wa.notify_dispatch_due_tomorrow(
        _BhConn(), _req(business_head_user_id=7), audience="owner"))
    assert ok is False


# --- orchestration: both WhatsApp legs ---------------------------------------

def test_a_due_request_whatsapps_the_business_head(monkeypatch):
    calls = _stub_all(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    out = asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert ("wa", "owner") in calls
    assert out[svc.KIND_DUE_OWNER_WA] == 1


def test_the_two_whatsapp_legs_claim_independently(monkeypatch):
    """A business head with no phone must not stop the NPD team's message going out,
    and must retry on its own next tick."""
    calls: list[tuple] = []

    async def _mail(conn, req, *, audience):
        return True

    async def _wa(conn, req, *, audience):
        calls.append(("wa", audience))
        return audience == "npd"          # team succeeds, business head fails

    monkeypatch.setattr(svc, "notify_dispatch_due_tomorrow", _mail)
    monkeypatch.setattr(svc, "wa_notify_dispatch_due_tomorrow", _wa)
    conn = _FullConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert [c[1] for c in calls] == ["npd", "owner", "owner"]
    assert svc.KIND_DUE_NPD_WA in [r["kind"] for r in conn.rows]
    assert svc.KIND_DUE_OWNER_WA not in [r["kind"] for r in conn.rows]


def test_an_overdue_request_whatsapps_neither_audience(monkeypatch):
    """Only the D-1 warning has Meta templates; the overdue chase stays email-only."""
    calls = _stub_all(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=2))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert not [c for c in calls if c[0] == "wa"]
