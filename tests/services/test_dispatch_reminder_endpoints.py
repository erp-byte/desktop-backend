"""Auth on the two dispatch-reminder email actions, plus what happens after auth passes.

These are PUBLIC endpoints: no session, reachable by anyone with the URL. Cancel is
terminal, so the token check is the only thing between a guessed 8-digit request_id and
a destroyed request. The first block pins the rejections, not the happy path — the token
guard itself. The later blocks (added on review) cover what the guard hands off to:
`set_expected_dispatch_date`'s status gate, and the two endpoints' post-auth ordering
(a not-the-BH resolution, a blank reason, and PK-vs-request_id) with the DB and the
lifecycle services faked out.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_reminder_endpoints.py
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.sample import router, schemas
from app.modules.sample.services import requisition_service
from app.modules.sample.services.email_link_token import sign, verify

RID = 25495623
BH = "bh@candorfoods.in"


def test_a_cancel_token_does_not_authorise_a_redate():
    """Distinct bindings — a leaked date-change link must not become a cancel."""
    t = sign("req_redate", RID, BH)
    assert verify(t, "req_redate", RID, BH)
    assert not verify(t, "req_cancel", RID, BH)


def test_a_token_is_bound_to_its_request():
    t = sign("req_cancel", RID, BH)
    assert not verify(t, "req_cancel", 11111111, BH)


def test_a_token_is_bound_to_its_recipient():
    t = sign("req_cancel", RID, BH)
    assert not verify(t, "req_cancel", RID, "someone@else.in")


def test_an_absent_token_is_rejected():
    assert not verify("", "req_cancel", RID, BH)
    assert not verify(None, "req_cancel", RID, BH)


def test_guard_rejects_a_bad_token():
    from app.modules.sample.router import _assert_req_action_token
    with pytest.raises(HTTPException) as e:
        _assert_req_action_token("req_cancel", RID, BH, "deadbeef")
    assert e.value.status_code == 403


def test_guard_rejects_a_blank_email():
    from app.modules.sample.router import _assert_req_action_token
    with pytest.raises(HTTPException) as e:
        _assert_req_action_token("req_cancel", RID, "", sign("req_cancel", RID, ""))
    assert e.value.status_code == 403


def test_guard_accepts_a_good_token():
    from app.modules.sample.router import _assert_req_action_token
    _assert_req_action_token("req_cancel", RID, BH, sign("req_cancel", RID, BH))


# ── set_expected_dispatch_date: the redate button's status gate ─────────────────────
#
# update_requisition refuses anything past SUBMITTED/BH_REJECTED — fine for the general
# edit form, wrong for a date slip, which is exactly what happens to requisitions already
# in production or ready to ship. set_expected_dispatch_date is the narrow, date-only
# mutator the redate endpoint actually calls; these tests pin that it accepts every status
# the overdue reminder chases (dispatch_reminder_service.OPEN_STATUSES) and refuses a
# terminal one, without touching anything but the one column.

class _ReqConn:
    """Minimal stand-in for what set_expected_dispatch_date + its trailing
    get_requisition() call need: one requisition row (kept in sync by the UPDATE this
    function issues) plus every nested read get_requisition performs (npd targets, the
    dispatch-ledger migration probe, articles, approvals, audit) answered with nothing —
    this test is about the status gate, not the reassembled detail payload."""

    def __init__(self, row: dict):
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
        # get_requisition's nested reads (npd targets, the npd_dev_dispatch column
        # probe, articles, approvals, audit) — none of them bear on the status gate.
        return []

    async def execute(self, query, *args):
        if "UPDATE sample_requisitions" in query and "expected_dispatch_date" in query:
            req_id, new_date, updated_by = args
            self.row["expected_dispatch_date"] = new_date
            self.row["updated_by"] = updated_by
            return "UPDATE 1"
        if "INSERT INTO sample_audit_log" in query:
            self.audit_writes.append(args)
            return "INSERT 1"
        raise AssertionError(f"unexpected execute: {query[:60]}")


def _req_row(**over) -> dict:
    base = {"id": 42, "request_id": RID, "status": "BH_APPROVED",
            "sample_type": "NPD", "expected_dispatch_date": date(2026, 9, 1)}
    base.update(over)
    return base


def _bh_user() -> SimpleNamespace:
    return SimpleNamespace(user_id=7, role_name="business_head")


def test_the_two_open_status_lists_stay_in_step():
    """The reminder mails a Change-date button for every status it chases, and the
    endpoint behind that button accepts exactly OPEN_STATUSES_EDITABLE_DATE. The two
    lists are deliberately duplicated (no cross-service import for one constant), so
    nothing but this assertion stops them drifting — and drift means a mailed button
    that 409s, which is a bug this branch already had to fix once."""
    from app.modules.sample.services import dispatch_reminder_service as drs
    assert tuple(drs.OPEN_STATUSES) == requisition_service.OPEN_STATUSES_EDITABLE_DATE


@pytest.mark.parametrize("status", list(requisition_service.OPEN_STATUSES_EDITABLE_DATE))
def test_set_expected_dispatch_date_accepts_every_open_status(status):
    """Every status in OPEN_STATUSES_EDITABLE_DATE must be redateable. That this is
    also every status dispatch_reminder_service.OPEN_STATUSES chases is pinned
    separately by test_the_two_open_status_lists_stay_in_step above."""
    conn = _ReqConn(_req_row(status=status))
    new_date = date(2026, 9, 20)
    out = asyncio.run(requisition_service.set_expected_dispatch_date(
        conn, 42, new_date=new_date, user=_bh_user()))
    assert out["expected_dispatch_date"] == new_date
    assert conn.audit_writes, "the change must be audited"


@pytest.mark.parametrize("status", ["CLOSED", "CANCELLED"])
def test_set_expected_dispatch_date_rejects_a_terminal_status(status):
    conn = _ReqConn(_req_row(status=status))
    with pytest.raises(HTTPException) as e:
        asyncio.run(requisition_service.set_expected_dispatch_date(
            conn, 42, new_date=date(2026, 9, 20), user=_bh_user()))
    assert e.value.status_code == 409
    assert conn.audit_writes == [], "a rejected redate must not write an audit row"


# ── The two endpoints' post-auth ordering ────────────────────────────────────────────
#
# All the tests above (and Task 6's) stop at _assert_req_action_token. Deleting the
# _resolve_req_bh 403 branch entirely would leave every one of them green while opening
# the endpoint to anyone holding any valid token — and CANCELLED is terminal. These call
# the router coroutine directly (a plain async function, no Depends() on it — nothing
# about testing it needs a running app) against a faked pool/connection, and monkeypatch
# the lifecycle service to prove it either never ran or ran with the right id.

class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    """`request.app.state.db_pool` — the two endpoints only ever do
    `async with pool.acquire() as conn`."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class _BhLookupConn:
    """Stands in for the connection `_resolve_req_bh` reads from. `row` is whatever
    that resolution should yield — a dict for a match, None for 'not this request's BH'."""

    def __init__(self, row):
        self.row = row

    async def fetchrow(self, query, *args):
        return self.row


def _fake_request(pool) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))


def _cancel_body(**over) -> "schemas.RequisitionEmailCancel":
    fields = {"request_id": RID, "email": BH, "t": sign("req_cancel", RID, BH),
              "reason": "wrong article"}
    fields.update(over)
    return schemas.RequisitionEmailCancel(**fields)


def test_endpoint_403s_when_the_caller_is_not_the_bound_bh_and_never_calls_the_service(monkeypatch):
    """A valid token only proves the link came from us — _resolve_req_bh proves the
    clicker is the person it was issued to. When that lookup misses, cancel_requisition
    must never run."""
    calls = []

    async def _fake_cancel(conn, req_id, *, reason, user):
        calls.append((req_id, reason, user))
        return {}

    monkeypatch.setattr(requisition_service, "cancel_requisition", _fake_cancel)
    pool = _FakePool(_BhLookupConn(row=None))          # no matching business head
    request = _fake_request(pool)

    with pytest.raises(HTTPException) as e:
        asyncio.run(router.email_requisition_cancel(request, _cancel_body()))
    assert e.value.status_code == 403
    assert calls == []


def test_endpoint_422s_on_a_blank_reason_before_touching_the_db(monkeypatch):
    """A blank cancellation reason is a 422, not a silent cancel — and it is caught
    before the pool is even acquired, so cancel_requisition never runs either."""
    calls = []

    async def _fake_cancel(conn, req_id, *, reason, user):
        calls.append((req_id, reason, user))
        return {}

    monkeypatch.setattr(requisition_service, "cancel_requisition", _fake_cancel)

    def _boom_acquire():
        raise AssertionError("pool.acquire() must not run before the reason check")
    request = _fake_request(SimpleNamespace(acquire=_boom_acquire))

    with pytest.raises(HTTPException) as e:
        asyncio.run(router.email_requisition_cancel(request, _cancel_body(reason="   ")))
    assert e.value.status_code == 422
    assert calls == []


def test_endpoint_passes_the_pk_to_the_service_not_the_mailed_request_id(monkeypatch):
    """_resolve_req_bh returns req_pk (sr.id) precisely so callers don't reach for
    body.request_id (the 8-digit mailed id) by mistake — cancel_requisition's req_id
    parameter is a PK. Pins that the two never get swapped."""
    PK = 999
    assert PK != RID
    calls = []

    async def _fake_cancel(conn, req_id, *, reason, user):
        calls.append(req_id)
        return {}

    monkeypatch.setattr(requisition_service, "cancel_requisition", _fake_cancel)
    row = {"user_id": 7, "role_name": "business_head", "req_pk": PK}
    pool = _FakePool(_BhLookupConn(row=row))
    request = _fake_request(pool)

    asyncio.run(router.email_requisition_cancel(request, _cancel_body()))
    assert calls == [PK]


# ── redate survives a release_overdue failure ────────────────────────────────────────
#
# set_expected_dispatch_date commits the date change in its own transaction; only THEN
# does the redate endpoint clear the overdue-chase log rows. A failure in that clear must
# not surface as a 500 on a request whose edit already succeeded — the BH would retry an
# already-applied change forever against a server that keeps refusing it.

class _RedateConn:
    """Enough surface for the full redate path: _resolve_req_bh's lookup,
    set_expected_dispatch_date's row + UPDATE + audit write (via get_requisition's nested
    reads, all answered with nothing), and has_log_table/release_overdue — whose DELETE
    can be made to raise, to prove the endpoint tolerates that."""

    def __init__(self, bh_row: dict, req_row: dict, *, boom_on_release: bool = False):
        self.bh_row = bh_row
        self.row = dict(req_row)
        self.boom_on_release = boom_on_release

    def transaction(self):
        class _T:
            async def __aenter__(self_inner): return None
            async def __aexit__(self_inner, *a): return False
        return _T()

    async def fetchrow(self, query, *args):
        if "sr.id AS req_pk" in query:
            return self.bh_row
        if "FROM sample_requisitions WHERE id = $1" in query:
            return dict(self.row)
        raise AssertionError(f"unexpected fetchrow: {query[:60]}")

    async def fetchval(self, query, *args):
        if "information_schema.tables" in query:
            return 1                       # has_log_table -> True, release_overdue runs
        raise AssertionError(f"unexpected fetchval: {query[:60]}")

    async def fetch(self, query, *args):
        return []                          # get_requisition's nested reads — irrelevant here

    async def execute(self, query, *args):
        if "UPDATE sample_requisitions" in query and "expected_dispatch_date" in query:
            _req_id, new_date, _updated_by = args
            self.row["expected_dispatch_date"] = new_date
            return "UPDATE 1"
        if "INSERT INTO sample_audit_log" in query:
            return "INSERT 1"
        if "DELETE FROM sample_dispatch_reminder_log" in query:
            if self.boom_on_release:
                raise RuntimeError("db exploded clearing the overdue guard")
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {query[:60]}")


def test_redate_endpoint_survives_a_release_overdue_failure():
    bh_row = {"user_id": 7, "role_name": "business_head", "req_pk": 42}
    req_row = _req_row(status="BH_APPROVED")           # id=42, matches req_pk above
    conn = _RedateConn(bh_row, req_row, boom_on_release=True)
    request = _fake_request(_FakePool(conn))
    new_date = date(2026, 9, 20)
    body = schemas.RequisitionEmailRedate(
        request_id=RID, email=BH, t=sign("req_redate", RID, BH),
        expected_dispatch_date=new_date)

    out = asyncio.run(router.email_requisition_redate(request, body))

    assert out["expected_dispatch_date"] == new_date, (
        "the date change must have committed despite release_overdue blowing up")
