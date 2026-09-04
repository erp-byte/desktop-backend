"""Auth on the two dispatch-reminder email actions.

These are PUBLIC endpoints: no session, reachable by anyone with the URL. Cancel is
terminal, so the token check is the only thing between a guessed 8-digit request_id and
a destroyed request. These tests pin the rejections, not the happy path.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_reminder_endpoints.py
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

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
