"""Send for approval: allowed only while a return is undecided (it doubles as the
re-send), and the response says what actually went out — the mail (every copy, or
why not), the WhatsApp card (sent / partial / failed / skipped) and an overall
sent / partial / failed — instead of always "sent". Only approvers who can decide
count: the person who raised the return gets a copy, never the buttons. A re-send
never repeats a Message-ID (a mail provider would drop it as a duplicate).

Through the real router with SMTP and Graph stubbed. The fakes and fixtures are
test_cr_approval_decisions'.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_approval_send.py
"""
from __future__ import annotations

import re

import pytest

from app.modules.customer_returns import approval_token
from app.modules.customer_returns.services import wa_notify
from tests.services.test_cr_approval_decisions import (  # noqa: F401 — fixtures
    BH, BH_EMAIL, COMPANY, CR, DEPUTY2_EMAIL, DEPUTY_EMAIL, NO_DEPUTIES, NOTIFY_TO, FakeConn, client,
    get_cr, header, mail, no_guard_errors, tokens,
)
from tests.services.test_cr_approval_whatsapp import DEPUTY_PHONE, graph  # noqa: F401 — fixture

URL = f"/api/v1/customer-returns/{COMPANY}/{CR}/send-for-approval"
APPROVERS = [BH_EMAIL, DEPUTY_EMAIL, DEPUTY2_EMAIL]
ROOT = f"<RTV-{CR}@candorfoods.in>"          # the thread id every copy replies into
REVIEW = f"https://erp.test/modules/customer-returns/{CR}/approve?company={COMPANY}"


def _buttoned(mail):
    return [m for m in mail.sent if "email-action" in m["text"]]


@pytest.fixture
def no_whatsapp(monkeypatch):
    monkeypatch.setattr(wa_notify, "_wa_enabled", lambda: False)


def _send(client, conn):
    return client(conn, BH).post(URL)


@pytest.mark.parametrize("status, label", [("Approved", "Approved"), ("Submitted", "Approved (boxes saved)"),
                                           ("Rejected", "Rejected")])
def test_a_decided_return_cannot_be_sent_for_approval_again(client, mail, get_cr, graph, status, label):
    conn = FakeConn(header(status=status))
    res = _send(client, conn)
    assert res.status_code == 409
    body = res.json()
    assert body["error"] == "already_decided"
    assert body["message"] == f"{CR} is already “{label}”, so it can't be sent for approval again."
    assert body["details"] == {"rtv_id": CR, "status": status}
    assert mail.sent == [] and graph.posted == []


@pytest.mark.parametrize("status", ["Pending", "On Hold"])
def test_everything_delivered_is_sent(client, mail, get_cr, no_whatsapp, status):
    conn = FakeConn(header(status=status))
    res = _send(client, conn)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "sent" and "reason" not in body
    assert body["mail"] == {"sent": True, "to": [*APPROVERS, NOTIFY_TO], "cc_count": 3, "approvers_mailed": 3}
    assert body["whatsapp"] == {"status": "skipped", "sent": 0, "failed": 0, "reason": "wa_disabled",
                                "recipients": []}
    assert body["approver"] == {"name": "Prashant Pal", "email": BH_EMAIL}
    assert [a["email"] for a in body["approvers"]] == APPROVERS
    assert (body["to"], body["cc_count"]) == ([*APPROVERS, NOTIFY_TO], 3)
    # One button copy per approver, then the button-less copy to notify_to + CC.
    assert [m["rcpts"] for m in mail.sent[:3]] == [[a] for a in APPROVERS]
    assert mail.sent[3]["rcpts"][0] == NOTIFY_TO
    assert conn.updates() == []


def test_smtp_not_configured_is_reported_as_failed(client, mail, get_cr, no_whatsapp):
    mail.smtp_host = ""
    body = _send(client, FakeConn(header())).json()
    assert body["status"] == "failed"
    assert body["mail"]["sent"] is False and body["mail"]["reason"] == "smtp_not_configured"
    assert body["mail"]["approvers_mailed"] == 0 and mail.sent == []


def test_an_smtp_error_is_reported_with_the_addresses_not_mailed(client, mail, get_cr, no_whatsapp):
    mail.fail_to = {BH_EMAIL, DEPUTY_EMAIL, DEPUTY2_EMAIL, NOTIFY_TO}
    body = _send(client, FakeConn(header())).json()
    assert body["status"] == "failed"
    assert (body["mail"]["sent"], body["mail"]["reason"]) == (False, "smtp_error")
    assert body["mail"]["failed"][:3] == APPROVERS


def test_some_approvers_mailed_is_partial(client, mail, get_cr, no_whatsapp):
    mail.fail_to = {DEPUTY_EMAIL}
    body = _send(client, FakeConn(header())).json()
    assert body["status"] == "partial"
    assert body["mail"]["approvers_mailed"] == 2 and body["mail"]["failed"] == [DEPUTY_EMAIL]


def test_mail_delivered_but_whatsapp_refused_is_partial_and_no_numbers_leak(client, mail, get_cr, graph):
    graph.refuse = {"919800000011", "919800000012"}
    body = _send(client, FakeConn(header())).json()
    assert body["status"] == "partial" and body["mail"]["sent"] is True
    assert body["whatsapp"] == {"status": "failed", "sent": 0, "failed": 2, "reason": "send_failed",
                                "recipients": [{"name": "Prashant Pal", "sent": False},
                                               {"name": "Satyendra Kumar Garg", "sent": False}]}
    assert not re.search(r"9198000000|wamid", str(body))


def test_whatsapp_delivered_but_mail_down_is_partial(client, mail, get_cr, graph):
    mail.smtp_host = ""
    body = _send(client, FakeConn(header())).json()
    assert body["status"] == "partial"
    assert (body["whatsapp"]["status"], body["whatsapp"]["sent"]) == ("sent", 2)


def test_both_channels_working_is_sent(client, mail, get_cr, graph):
    body = _send(client, FakeConn(header())).json()
    assert body["status"] == "sent" and body["whatsapp"]["status"] == "sent"


def test_a_return_nobody_can_decide_is_failed_with_the_reason(client, mail, get_cr, no_whatsapp):
    conn = FakeConn(header(business_head="Someone Else"), routing=NO_DEPUTIES)
    body = _send(client, conn).json()
    assert (body["status"], body["reason"]) == ("failed", "no_approver")
    assert body["approver"] is None and body["approvers"] == []
    # The button-less notice still went to notify_to, as before — in the thread, with its own id.
    assert body["mail"]["sent"] is True and mail.sent[0]["rcpts"][0] == NOTIFY_TO
    assert "email-action" not in mail.sent[0]["text"]
    assert mail.sent[0]["irt"] == ROOT and mail.sent[0]["msgid"] != ROOT


# ── the person who raised the return is never a decider ─────────────────────
def test_a_business_head_who_raised_the_return_gets_a_copy_and_the_deputies_the_buttons(
        client, mail, get_cr, graph):
    deputies = [DEPUTY_EMAIL, DEPUTY2_EMAIL]
    body = _send(client, FakeConn(header(created_by=BH_EMAIL))).json()
    assert body["status"] == "sent" and "reason" not in body
    assert body["approver"] == {"name": "Prashant Pal", "email": BH_EMAIL}   # still shown as the BH
    assert [a["email"] for a in body["approvers"]] == deputies              # but not one who can decide
    assert body["to"] == [*deputies, NOTIFY_TO] and body["mail"]["approvers_mailed"] == 2
    assert [m["rcpts"] for m in _buttoned(mail)] == [[d] for d in deputies]
    for m in _buttoned(mail):   # neither deputy is told they are the BH, and both learn why they decide
        assert "assigned Business Head" not in m["text"] and "You are a standing deputy approver." in m["text"]
        assert "Prashant Pal raised this return, so cannot decide it." in m["text"]
    (copy,) = [m for m in mail.sent if m not in _buttoned(mail)]
    assert BH_EMAIL in copy["rcpts"] and BH_EMAIL in copy["cc"]               # copied, not asked
    # WhatsApp: the deputy with a number on file, never the BH.
    assert [p["to"] for p in graph.posted] == [DEPUTY_PHONE]
    assert (body["whatsapp"]["status"], body["whatsapp"]["sent"]) == ("sent", 1)


def test_a_return_whose_only_approver_raised_it_is_failed_not_sent(client, mail, get_cr, graph):
    body = _send(client, FakeConn(header(created_by=BH_EMAIL), routing=NO_DEPUTIES)).json()
    assert (body["status"], body["reason"]) == ("failed", "only_creator")
    assert body["approvers"] == [] and body["mail"]["approvers_mailed"] == 0
    # Nobody got buttons that could only refuse them; everyone, the BH too, got the notice.
    assert mail.sent and _buttoned(mail) == []
    assert BH_EMAIL in mail.sent[0]["rcpts"] and NOTIFY_TO in mail.sent[0]["rcpts"]
    assert graph.posted == [] and body["whatsapp"]["status"] == "skipped"


def test_a_deputy_who_raised_the_return_does_not_count_as_an_approver_reached(client, mail, get_cr,
                                                                               no_whatsapp):
    # The two approvers who can decide both bounce; the creator's own copy has no buttons.
    mail.fail_to = {BH_EMAIL, DEPUTY2_EMAIL}
    body = _send(client, FakeConn(header(created_by=DEPUTY_EMAIL))).json()
    assert body["status"] == "failed"
    assert [a["email"] for a in body["approvers"]] == [BH_EMAIL, DEPUTY2_EMAIL]
    assert body["mail"]["approvers_mailed"] == 0 and body["mail"]["failed"][:2] == [BH_EMAIL, DEPUTY2_EMAIL]
    assert _buttoned(mail) == [] and DEPUTY_EMAIL in mail.sent[0]["rcpts"]


# ── re-send: never a repeated Message-ID ────────────────────────────────────
def test_a_resend_never_reuses_a_message_id_and_every_copy_replies_into_the_thread(client, mail, get_cr,
                                                                                    no_whatsapp):
    conn = FakeConn(header())
    assert _send(client, conn).json()["status"] == "sent"
    assert _send(client, conn).json()["status"] == "sent"     # the Re-send, e.g. from the detail screen
    ids = [m["msgid"] for m in mail.sent]
    assert len(ids) == 8 and len(set(ids)) == 8               # 3 approver copies + 1 copy, twice
    assert ROOT not in ids and all(i.endswith("@candorfoods.in>") for i in ids)
    assert {(m["irt"], m["refs"]) for m in mail.sent} == {(ROOT, ROOT)}
    assert len({m["subject"] for m in mail.sent}) == 1        # the anchored subject keeps one conversation


def test_each_decider_copy_also_links_to_the_in_app_review(client, mail, get_cr, no_whatsapp):
    _send(client, FakeConn(header()))
    assert all(f"Or review it in the ERP (sign-in needed): {REVIEW}" in m["text"] for m in _buttoned(mail))
    assert all(REVIEW not in m["text"] for m in mail.sent if m not in _buttoned(mail))


def test_each_approver_copy_links_to_the_confirmation_page_for_that_approver(client, mail, get_cr, no_whatsapp):
    _send(client, FakeConn(header()))
    for copy, approver in zip(mail.sent[:3], APPROVERS):
        assert "each opens a page to confirm" in copy["text"]
        links = re.findall(r"/api/v1/customer-returns/email-action\?token=([\w.-]+)", copy["text"])
        claims = [approval_token.verify_action_token(t) for t in links]
        assert [(c["action"], c["approver"]) for c in claims] == \
            [("approve", approver), ("reject", approver), ("hold", approver)]


def test_with_no_mapped_bh_the_first_deputy_is_not_called_the_business_head(client, mail, get_cr, no_whatsapp):
    _send(client, FakeConn(header(business_head="Someone Else")))
    assert "You are a standing deputy approver." in mail.sent[0]["text"]
    assert "assigned Business Head" not in mail.sent[0]["text"]
