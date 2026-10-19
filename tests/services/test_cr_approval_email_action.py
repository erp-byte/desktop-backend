"""The emailed Approve / Reject / Hold link: GET /email-action only shows a
confirmation page and changes nothing; POST /email-action decides, as the address
the link was minted for, through the same apply_cr_action rules as the app.

Both routes are public (a click from a mail app carries no login), so the client
here sends no Authorization header. The fakes and fixtures are
test_cr_approval_decisions'.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_approval_email_action.py
"""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from app.modules.customer_returns import approval_token
from app.modules.customer_returns import router as R
from app.modules.customer_returns.services import query_service
from tests.services.test_cr_approval_decisions import (  # noqa: F401 — fixtures
    BH_EMAIL, COMPANY, CR, CREATOR_EMAIL, DEPUTY_EMAIL, NO_DEPUTIES, FakeConn, client, get_cr, header,
    mail, no_guard_errors, tokens,
)

URL = "/api/v1/customer-returns/email-action"


def _page(client, conn, token):
    return client(conn, auth=False).get(URL, params={"token": token})


def _confirm(client, conn, token, remark=None):
    data = {"token": token, **({"remark": remark} if remark is not None else {})}
    return client(conn, auth=False).post(URL, data=data)


def _hidden_token(html):
    return re.search(r"name='token' value='([^']+)'", html)[1]


# ── GET: a confirmation page, nothing changes ────────────────────────────────
@pytest.mark.parametrize("action, button", [("approve", "Confirm approve"), ("reject", "Confirm reject"),
                                            ("hold", "Confirm hold")])
def test_the_link_opens_a_confirmation_page_and_changes_nothing(client, tokens, mail, action, button):
    conn = FakeConn(header())
    token = tokens(COMPANY, CR, action, BH_EMAIL)
    res = _page(client, conn, token)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    html = res.text
    for shown in (CR, "Sample Foods Pvt Ltd", "W202", button):
        assert shown in html
    assert "<form method='post' action='/api/v1/customer-returns/email-action'" in html
    assert _hidden_token(html) == token
    assert "noindex" in html and "window.close" not in html       # the form must stay open
    assert ("name='remark'" in html) == (action != "approve")      # a remark box for reject / hold
    # Nothing was written and no mail went: only reads.
    assert conn.updates() == [] and mail.sent == []
    assert {kind for kind, _, _ in conn.calls} <= {"fetchrow", "fetch"}
    assert conn.headers[CR]["status"] == "Pending"


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c"])
def test_a_missing_or_broken_link_is_a_400_card(client, tokens, token):
    conn = FakeConn(header())
    res = _page(client, conn, token)
    assert res.status_code == 400 and "Link invalid or expired" in res.text
    assert conn.calls == []


def test_a_link_signed_with_another_key_or_without_an_approver_is_refused(client, tokens, monkeypatch):
    conn = FakeConn(header())
    no_approver = tokens(COMPANY, CR, "approve", "")
    assert _page(client, conn, no_approver).status_code == 400
    good = tokens(COMPANY, CR, "approve", BH_EMAIL)
    monkeypatch.setattr(approval_token, "_secret", lambda settings=None: "another-signing-key-0123456789-abcdef")
    assert _page(client, conn, good).status_code == 400
    assert conn.calls == []


@pytest.mark.parametrize("status, label", [("Approved", "Approved"), ("Submitted", "Approved (boxes saved)"),
                                           ("Rejected", "Rejected")])
def test_the_page_for_a_decided_return_says_it_is_already_actioned(client, tokens, status, label):
    conn = FakeConn(header(status=status))
    res = _page(client, conn, tokens(COMPANY, CR, "reject", BH_EMAIL))
    assert res.status_code == 200 and "Already actioned" in res.text
    assert f"already “{label}”" in res.text and "<form" not in res.text
    assert conn.updates() == []


def test_the_page_for_someone_who_may_not_decide_says_so_at_once(client, tokens):
    # A deputy removed from the routing since the mail went out, and the creator.
    conn = FakeConn(header(), routing=NO_DEPUTIES)
    res = _page(client, conn, tokens(COMPANY, CR, "approve", DEPUTY_EMAIL))
    assert res.status_code == 403 and "Action not available" in res.text
    assert "Only Prashant Pal can approve, reject or hold" in res.text and "<form" not in res.text
    conn = FakeConn(header(created_by=BH_EMAIL))
    res = _page(client, conn, tokens(COMPANY, CR, "approve", BH_EMAIL))
    assert res.status_code == 403 and "You raised customer return" in res.text
    assert conn.updates() == []


# ── POST: the Confirm button decides ─────────────────────────────────────────
def test_confirm_decides_as_the_address_the_link_was_minted_for(client, tokens, mail, get_cr):
    conn = FakeConn(header())
    token = tokens(COMPANY, CR, "approve", DEPUTY_EMAIL)
    res = _confirm(client, conn, _hidden_token(_page(client, conn, token).text))
    assert res.status_code == 200 and "Customer return Approved" in res.text
    (update,) = conn.updates()
    assert update[2][:3] == (CR, "Approved", DEPUTY_EMAIL)
    assert conn.headers[CR]["approved_by"] == DEPUTY_EMAIL
    (status_mail,) = mail.sent
    assert f"marked “Approved” by {DEPUTY_EMAIL}." in status_mail["text"]


def test_confirm_carries_the_remark_for_a_reject(client, tokens, mail, get_cr):
    conn = FakeConn(header())
    res = _confirm(client, conn, tokens(COMPANY, CR, "reject", BH_EMAIL), remark="  wrong\nbatch  ")
    assert res.status_code == 200 and "Customer return Rejected" in res.text
    assert conn.updates()[0][2][3] == "wrong\nbatch"
    assert "Remark: “wrong batch”" in mail.sent[0]["text"]


def test_confirm_is_idempotent(client, tokens, mail, get_cr):
    conn = FakeConn(header())
    token = tokens(COMPANY, CR, "hold", BH_EMAIL)
    assert "Customer return On Hold" in _confirm(client, conn, token).text
    again = _confirm(client, conn, tokens(COMPANY, CR, "approve", DEPUTY_EMAIL))   # On Hold: still open
    assert "Customer return Approved" in again.text
    third = _confirm(client, conn, token)
    assert third.status_code == 200 and "Already actioned" in third.text and "already “Approved”" in third.text
    assert len(conn.updates()) == 2 and len(mail.sent) == 2


@pytest.mark.parametrize("actor, routing, error_text", [
    (CREATOR_EMAIL, None, "You raised customer return"),
    (DEPUTY_EMAIL, NO_DEPUTIES, "Only Prashant Pal can approve, reject or hold"),
])
def test_confirm_by_someone_who_may_not_decide_changes_nothing(client, tokens, mail, get_cr,
                                                               actor, routing, error_text):
    conn = FakeConn(header(), **({"routing": routing} if routing else {}))
    res = _confirm(client, conn, tokens(COMPANY, CR, "approve", actor))
    assert res.status_code == 403 and "Nothing was done" in res.text and error_text in res.text
    assert conn.updates() == [] and mail.sent == []


def test_confirm_with_a_broken_link_is_a_400_card(client, tokens):
    conn = FakeConn(header())
    res = _confirm(client, conn, "garbage")
    assert res.status_code == 400 and "Link invalid or expired" in res.text
    assert conn.calls == []


def test_confirm_reports_success_even_when_the_status_mail_fails(client, tokens, mail, monkeypatch):
    async def _boom(conn, company, cr_id):
        raise TimeoutError("statement timeout")

    monkeypatch.setattr(query_service, "get_cr", _boom)
    conn = FakeConn(header())
    res = _confirm(client, conn, tokens(COMPANY, CR, "approve", BH_EMAIL))
    assert res.status_code == 200 and "Customer return Approved" in res.text


def test_a_database_failure_gets_a_page_not_a_raw_500(client, tokens, mail, get_cr):
    conn = FakeConn(header())
    conn.fail_on = "UPDATE"
    res = _confirm(client, conn, tokens(COMPANY, CR, "approve", BH_EMAIL))
    assert res.status_code == 500 and "could not be recorded" in res.text
    assert conn.headers[CR]["status"] == "Pending"
    conn = FakeConn(header())
    conn.fail_on = "_rtv_header"
    res = _page(client, conn, tokens(COMPANY, CR, "approve", BH_EMAIL))
    assert res.status_code == 500 and "Something went wrong" in res.text


def test_the_kill_switch_pauses_both_steps(client, tokens, monkeypatch):
    monkeypatch.setattr(R, "CR_READ_ONLY", True)
    conn = FakeConn(header())
    token = tokens(COMPANY, CR, "approve", BH_EMAIL)
    for res in (_page(client, conn, token), _confirm(client, conn, token)):
        assert res.status_code == 403 and "Approvals paused" in res.text
    assert conn.calls == []


def test_both_steps_are_public_and_declared_before_the_company_routes():
    routes = [(m, r.path, r) for r in R.router.routes if isinstance(r, APIRoute) for m in r.methods]
    paths = {m: [p for mm, p, _ in routes if mm == m] for m in ("GET", "POST")}
    for m in ("GET", "POST"):
        assert paths[m].index(URL) < paths[m].index("/api/v1/customer-returns/{company}")
    for m, p, r in routes:
        if p == URL:
            assert not r.dependant.dependencies or all(
                d.call.__name__ != "_extract_user" for d in r.dependant.dependencies)
