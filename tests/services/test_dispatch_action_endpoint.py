"""POST /sample/requisitions/{id}/dispatch-action — the two overdue actions as an API.

The WhatsApp buttons and the emailed links each have their own entry point (a webhook, a
signed public link). This is the third: an authenticated endpoint taking the SAME two
actions, so the app and any operator tooling reach the same service calls instead of
re-implementing them.

The reason is mandatory on both legs — cancel is terminal, and a date that moved without a
recorded why is exactly what the chase exists to prevent.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_action_endpoint.py
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.sample import router, schemas
from app.modules.sample.services import requisition_service

REQ_PK = 42


def _user():
    return SimpleNamespace(user_id=7, role_name="business_head", is_admin=False,
                           full_name="BH")


# --- the reason reaches the audit trail -------------------------------------

class _ReqConn:
    """One requisition row plus the audit writes; every nested get_requisition read
    answers empty, since these tests are about the remark, not the detail payload."""

    def __init__(self, row):
        self.row = dict(row)
        self.audit_writes: list[tuple] = []

    def transaction(self):
        class _T:
            async def __aenter__(self_inner): return None
            async def __aexit__(self_inner, *a): return False
        return _T()

    async def fetchrow(self, query, *args):
        if "FROM sample_requisitions WHERE id = $1" in query:
            return dict(self.row)
        raise AssertionError(f"unexpected fetchrow: {query[:60]}")

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        if "UPDATE sample_requisitions" in query:
            return "UPDATE 1"
        if "INSERT INTO sample_audit_log" in query:
            self.audit_writes.append(args)
            return "INSERT 1"
        raise AssertionError(f"unexpected execute: {query[:60]}")


def _row(**over):
    base = {"id": REQ_PK, "request_id": 25495623, "status": "IN_PRODUCTION",
            "sample_type": "NPD", "expected_dispatch_date": date(2026, 9, 1)}
    base.update(over)
    return base


def test_the_reason_is_recorded_against_the_date_change():
    """"Why did this slip?" is the whole point of asking; a remark that says only "changed
    from the overdue reminder" answers nothing a month later."""
    conn = _ReqConn(_row())
    asyncio.run(requisition_service.set_expected_dispatch_date(
        conn, REQ_PK, new_date=date(2026, 9, 20), user=_user(),
        reason="Raw material delayed"))
    assert any("Raw material delayed" in str(a) for a in conn.audit_writes)


def test_a_date_change_with_no_reason_still_audits():
    """The emailed link has no reason field, so the reason stays optional at this layer —
    it is the API and the WhatsApp flow that insist on one."""
    conn = _ReqConn(_row())
    asyncio.run(requisition_service.set_expected_dispatch_date(
        conn, REQ_PK, new_date=date(2026, 9, 20), user=_user()))
    assert conn.audit_writes


# --- the endpoint -----------------------------------------------------------

class _Req:
    def __init__(self, conn):
        self.app = SimpleNamespace(state=SimpleNamespace(db_pool=_Pool(conn)))


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self_inner): return conn
            async def __aexit__(self_inner, *a): return False
        return _Acq()


@pytest.fixture
def calls(monkeypatch):
    out = {"redate": [], "cancel": [], "released": []}

    async def _redate(conn, req_id, *, new_date, user, reason=None):
        out["redate"].append((req_id, new_date, reason))
        return {"id": req_id, "expected_dispatch_date": new_date}

    async def _cancel(conn, req_id, *, reason, user):
        out["cancel"].append((req_id, reason))
        return {"id": req_id, "status": "CANCELLED"}

    async def _release(conn, req_id):
        out["released"].append(req_id)

    monkeypatch.setattr(requisition_service, "set_expected_dispatch_date", _redate)
    monkeypatch.setattr(requisition_service, "cancel_requisition", _cancel)
    monkeypatch.setattr(router, "_release_overdue_best_effort", _release)
    return out


def _post(body):
    return asyncio.run(router.requisition_dispatch_action(
        _Req(object()), REQ_PK, body, user=_user()))


def test_change_date_moves_it_and_records_the_reason(calls):
    out = _post(schemas.DispatchActionBody(
        action="CHANGE_DATE", reason="Raw material delayed",
        expected_dispatch_date="20-09-2026"))
    assert calls["redate"] == [(REQ_PK, date(2026, 9, 20), "Raw material delayed")]
    assert out["expected_dispatch_date"] == date(2026, 9, 20)


def test_change_date_clears_the_overdue_chase(calls):
    """Otherwise the rows from yesterday's chase silence the new date's warning."""
    _post(schemas.DispatchActionBody(action="CHANGE_DATE", reason="Slipped",
                                     expected_dispatch_date="20-09-2026"))
    assert calls["released"] == [REQ_PK]


def test_an_iso_date_is_accepted_too(calls):
    """The WhatsApp leg speaks dd-mm-yyyy because that is what the prompt asks a human
    for; an API caller will send ISO. Both reach the same service call."""
    _post(schemas.DispatchActionBody(action="CHANGE_DATE", reason="Slipped",
                                     expected_dispatch_date="2026-09-20"))
    assert calls["redate"][0][1] == date(2026, 9, 20)


def test_cancel_cancels_with_the_reason(calls):
    out = _post(schemas.DispatchActionBody(action="CANCEL",
                                           reason="Customer withdrew the enquiry"))
    assert calls["cancel"] == [(REQ_PK, "Customer withdrew the enquiry")]
    assert out["status"] == "CANCELLED"


def test_cancel_does_not_touch_the_chase_rows(calls):
    """A cancelled requisition leaves OPEN_STATUSES, so the scan stops selecting it —
    deleting its history would only lose the record that it was chased."""
    _post(schemas.DispatchActionBody(action="CANCEL", reason="Withdrawn"))
    assert calls["released"] == []


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_blank_reason_is_refused_on_both_legs(calls, reason):
    for action in ("CANCEL", "CHANGE_DATE"):
        with pytest.raises(HTTPException) as e:
            _post(schemas.DispatchActionBody(action=action, reason=reason,
                                             expected_dispatch_date="20-09-2026"))
        assert e.value.status_code == 422
    assert calls["cancel"] == [] and calls["redate"] == []


def test_change_date_without_a_date_is_refused(calls):
    with pytest.raises(HTTPException) as e:
        _post(schemas.DispatchActionBody(action="CHANGE_DATE", reason="Slipped"))
    assert e.value.status_code == 422
    assert calls["redate"] == []


def test_an_unreadable_date_is_refused_rather_than_guessed(calls):
    with pytest.raises(HTTPException) as e:
        _post(schemas.DispatchActionBody(action="CHANGE_DATE", reason="Slipped",
                                         expected_dispatch_date="next friday"))
    assert e.value.status_code == 422
    assert calls["redate"] == []


def test_a_past_date_is_refused(calls):
    """Same rule the WhatsApp prompt enforces: a date already gone re-arms the chase."""
    with pytest.raises(HTTPException) as e:
        _post(schemas.DispatchActionBody(action="CHANGE_DATE", reason="Slipped",
                                         expected_dispatch_date="01-01-2020"))
    assert e.value.status_code == 422
    assert calls["redate"] == []
