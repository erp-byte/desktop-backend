"""Customer-return WhatsApp: the Approve / Reject / Hold tap decides only from the
number the card was sent to, as the ERP user on that number, through the same
apply_cr_action rules as the app and the mail link; the head-approval send reports
what actually went out.

Graph is never called: _post_message / _upload_media / _send_text are stubbed, and
the articles PNG is not rendered. The fakes and fixtures are
test_cr_approval_decisions'.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_approval_whatsapp.py
"""
from __future__ import annotations

import asyncio
import types

import pytest

from app.modules.customer_returns.services import wa_notify as wa
from tests.services.test_cr_approval_decisions import (  # noqa: F401 — fixtures
    BH_EMAIL, BH_PHONE, COMPANY, CR, DEPUTY_EMAIL, NO_DEPUTIES, USERS, FakeConn, get_cr, header, mail,
    no_guard_errors, tokens,
)

WAMID = "wamid.HBgMOTE5ODAwMDAwMDExFQIAERgS"
DEPUTY_PHONE = "919800000012"


def _sent_to(phone, wamid=WAMID):
    return {"wamid": wamid, "company": COMPANY, "rtv_id": CR, "wa_phone": phone}


@pytest.fixture
def replies(monkeypatch):
    box: list[tuple[str, str]] = []

    async def _send_text(to, text):
        box.append((to, text))

    monkeypatch.setattr(wa, "_send_text", _send_text)
    return box


def tap(conn, phone, text="Approve", context_id=WAMID):
    return asyncio.run(wa.handle_return_button_tap(conn, phone, text, context_id))


# ── INBOUND: the tap ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("phone", ["919999999999", "9800000012", ""])
def test_a_tap_from_another_number_changes_nothing(replies, mail, get_cr, phone):
    conn = FakeConn(header(), wa_messages=[_sent_to(BH_PHONE)])
    res = tap(conn, phone)
    assert res == {"ok": False, "reason": "cr_wrong_phone", "rtv_id": CR}
    assert replies == [(phone, "This approval was sent to a different number.")]
    assert conn.updates() == [] and mail.sent == []
    assert not [c for c in conn.calls if "auth_user" in c[1] or "_rtv_header" in c[1]]


def test_a_card_whose_number_was_not_recorded_is_never_decided(replies, mail, get_cr):
    conn = FakeConn(header(), wa_messages=[_sent_to(None)])
    assert tap(conn, BH_PHONE)["reason"] == "cr_wrong_phone"
    assert conn.updates() == []


@pytest.mark.parametrize("phone", [BH_PHONE, "+91 98000 00011", "9800000011"])
def test_a_tap_from_the_number_it_was_sent_to_decides_as_that_users_email(replies, mail, get_cr, phone):
    conn = FakeConn(header(), wa_messages=[_sent_to(BH_PHONE)])
    res = tap(conn, phone, "Reject")
    assert res == {"ok": True, "rtv_id": CR, "status": "Rejected", "actor": BH_EMAIL}
    (update,) = conn.updates()
    assert update[2][:3] == (CR, "Rejected", BH_EMAIL)
    assert replies == [(phone, f"✓ Customer return {CR} is now “Rejected”.")]
    assert f"marked “Rejected” by {BH_EMAIL}." in mail.sent[0]["text"]


def test_a_deputys_tap_on_their_own_card_decides(replies, mail, get_cr):
    conn = FakeConn(header(status="On Hold"), wa_messages=[_sent_to(DEPUTY_PHONE)])
    assert tap(conn, DEPUTY_PHONE, "APPROVED")["status"] == "Approved"
    assert conn.headers[CR]["approved_by"] == DEPUTY_EMAIL


def test_a_tap_by_someone_no_longer_an_approver_is_refused_with_the_reason(replies, mail, get_cr):
    # The card went to the deputy; the deputy has since been taken off the routing.
    conn = FakeConn(header(), routing=NO_DEPUTIES, wa_messages=[_sent_to(DEPUTY_PHONE)])
    res = tap(conn, DEPUTY_PHONE)
    assert res == {"ok": False, "reason": "cr_refused", "error": "not_an_approver", "status_code": 403,
                   "rtv_id": CR}
    assert replies == [(DEPUTY_PHONE, f"Customer return {CR} was not changed. Only Prashant Pal can "
                                      f"approve, reject or hold customer return {CR}.")]
    assert conn.updates() == [] and mail.sent == []


def test_a_tap_by_the_person_who_raised_the_return_is_refused(replies, mail, get_cr):
    conn = FakeConn(header(created_by=BH_EMAIL), wa_messages=[_sent_to(BH_PHONE)])
    res = tap(conn, BH_PHONE, "Hold")
    assert (res["reason"], res["error"]) == ("cr_refused", "own_return")
    assert f"You raised customer return {CR}" in replies[0][1]
    assert conn.updates() == []


def test_a_tap_from_a_number_with_no_active_erp_user_is_refused(replies, mail, get_cr):
    # The BH's account was deactivated after the card went out: no e-mail to decide as.
    users = [u if u["user_id"] != 11 else {**u, "is_active": False} for u in USERS]
    conn = FakeConn(header(), users=users, wa_messages=[_sent_to(BH_PHONE)])
    res = tap(conn, BH_PHONE)
    assert (res["reason"], res["error"]) == ("cr_refused", "not_an_approver")
    assert "has no e-mail address" in replies[0][1]
    assert conn.updates() == []


def test_a_tap_from_a_number_whose_user_has_no_email_is_refused(replies, mail, get_cr):
    users = [u if u["user_id"] != 12 else {**u, "email": ""} for u in USERS]
    conn = FakeConn(header(), users=users, wa_messages=[_sent_to(DEPUTY_PHONE)])
    res = tap(conn, DEPUTY_PHONE)
    assert (res["reason"], res["error"]) == ("cr_refused", "not_an_approver")
    assert "has no e-mail address" in replies[0][1]
    assert conn.updates() == []


@pytest.mark.parametrize("status, label", [("Submitted", "Approved (boxes saved)"), ("Approved", "Approved"),
                                           ("Rejected", "Rejected")])
def test_a_tap_on_a_decided_return_is_answered_with_its_status_and_changes_nothing(replies, mail, get_cr,
                                                                                   status, label):
    conn = FakeConn(header(status=status), wa_messages=[_sent_to(BH_PHONE)])
    res = tap(conn, BH_PHONE, "Reject")
    assert res == {"ok": True, "already_actioned": True, "rtv_id": CR, "status": status}
    assert replies == [(BH_PHONE, f"Customer return {CR} is already “{label}”.")]
    assert conn.updates() == [] and mail.sent == []


def test_an_unparsed_reply_is_answered(replies, get_cr):
    conn = FakeConn(header(), wa_messages=[_sent_to(BH_PHONE)])
    assert tap(conn, BH_PHONE, "ok thanks")["reason"] == "cr_unparsed"
    assert replies == [(BH_PHONE, f"Reply Approve / Reject / Hold on customer return {CR}.")]


def test_a_tap_that_is_not_a_customer_return_message_falls_through(replies):
    conn = FakeConn(header())
    assert tap(conn, BH_PHONE, context_id="wamid.someone-else") is None
    assert tap(conn, BH_PHONE, context_id=None) is None
    assert replies == []


def test_a_tap_never_raises_and_says_it_was_not_recorded(replies, mail, get_cr):
    conn = FakeConn(header(), wa_messages=[_sent_to(BH_PHONE)])
    conn.fail_on = "UPDATE"
    res = tap(conn, BH_PHONE)
    assert res == {"ok": False, "reason": "cr_exception", "rtv_id": CR}
    assert "could not be recorded" in replies[-1][1]
    assert conn.headers[CR]["status"] == "Pending"


# ── OUTBOUND: the head-approval card ─────────────────────────────────────────
@pytest.fixture
def graph(monkeypatch, tmp_path):
    """Enable the WABA and stand in for Graph. `graph.refuse` = numbers Graph refuses.
    The articles image is a stub file in tmp_path (the sender deletes it after use)."""
    g = types.SimpleNamespace(posted=[], refuse=set(), uploads=0, media_id="media-1")

    def _render(customer, lines):
        png = tmp_path / "articles.png"
        png.write_bytes(b"\x89PNG stub")
        return str(png)

    async def _upload(path):
        g.uploads += 1
        return g.media_id

    async def _post(payload):
        g.posted.append(payload)
        if payload["to"] in g.refuse:
            return {}
        return {"messages": [{"id": f"wamid.{payload['to']}"}]}

    monkeypatch.setattr(wa, "_wa_enabled", lambda: True)
    monkeypatch.setattr(wa, "render_articles_png", _render)
    monkeypatch.setattr(wa, "_upload_media", _upload)
    monkeypatch.setattr(wa, "_post_message", _post)
    return g


def send(conn, **over):
    return asyncio.run(wa.notify_cr_head_approval(conn, {**header(**over), "company": COMPANY, "lines": []}))


def test_every_approver_with_a_number_gets_the_card_and_it_is_reported_sent(graph):
    conn = FakeConn(header())
    res = send(conn)
    # R M Patil has no ERP account, so no number: he is mail-only, which is not a failure.
    assert [p["to"] for p in graph.posted] == [BH_PHONE, DEPUTY_PHONE]
    assert (res["status"], res["sent"], res["failed"]) == ("sent", 2, 0) and "reason" not in res
    assert [r["name"] for r in res["recipients"]] == ["Prashant Pal", "Satyendra Kumar Garg"]
    assert conn.wa == {f"wamid.{p}": _sent_to(p, f"wamid.{p}") for p in (BH_PHONE, DEPUTY_PHONE)}
    assert graph.uploads == 1


def test_a_refused_message_is_counted_as_failed_and_the_rest_still_go(graph):
    graph.refuse = {BH_PHONE}
    conn = FakeConn(header())
    res = send(conn)
    assert (res["status"], res["sent"], res["failed"], res["reason"]) == ("partial", 1, 1, "send_failed")
    assert list(conn.wa) == [f"wamid.{DEPUTY_PHONE}"]


def test_every_message_refused_is_reported_failed_not_sent(graph):
    graph.refuse = {BH_PHONE, DEPUTY_PHONE}
    res = send(FakeConn(header()))
    assert (res["status"], res["sent"], res["failed"], res["reason"]) == ("failed", 0, 2, "send_failed")


def test_a_mapping_that_cannot_be_stored_stops_the_run_and_is_reported(graph):
    conn = FakeConn(header(), wa_store_error=True)
    res = send(conn)
    assert (res["status"], res["sent"], res["failed"], res["reason"]) == ("failed", 0, 2, "mapping_store_failed")
    assert len(graph.posted) == 1     # no more cards whose taps could not find the return


def test_a_failed_image_upload_is_reported_failed(graph):
    graph.media_id = None
    res = send(FakeConn(header()))
    assert (res["status"], res["sent"], res["failed"], res["reason"]) == ("failed", 0, 2, "media_upload_failed")
    assert graph.posted == []


def test_no_whatsapp_configured_or_no_number_on_file_is_skipped(graph, monkeypatch):
    users = [u for u in USERS if u["user_id"] not in (11, 12)]
    res = send(FakeConn(header(), users=[*users, {**USERS[0], "phone": ""}]))
    assert res == {"status": "skipped", "reason": "no_approver_phone", "sent": 0, "failed": 0}
    monkeypatch.setattr(wa, "_wa_enabled", lambda: False)
    assert send(FakeConn(header()))["status"] == "skipped"
    assert graph.posted == []


def test_the_person_who_raised_the_return_gets_no_card(graph):
    # The BH raised it: only the deputies can decide, so only they are sent the buttons.
    res = send(FakeConn(header()), created_by=" Prashant.Pal@candorfoods.in")
    assert [p["to"] for p in graph.posted] == [DEPUTY_PHONE]
    assert (res["status"], res["sent"], res["failed"]) == ("sent", 1, 0)
    # As its only approver, nobody is messaged at all.
    graph.posted.clear()
    res = send(FakeConn(header(), routing=NO_DEPUTIES), created_by=BH_EMAIL)
    assert res["status"] == "skipped" and graph.posted == []


def test_the_card_goes_to_the_approvers_account_not_a_namesake(graph):
    namesake = {"user_id": 9, "full_name": "Prashant Pal", "email": "p.pal@vendor.example",
                "phone": "9811111111", "is_active": True, "roles": []}
    conn = FakeConn(header(), users=[namesake, *USERS], routing=NO_DEPUTIES)
    send(conn)
    assert [p["to"] for p in graph.posted] == [BH_PHONE]
    # A BH whose name resolves to no e-mail is no approver: nobody is messaged.
    graph.posted.clear()
    res = send(FakeConn(header(business_head="Someone Else"), routing=NO_DEPUTIES), business_head="Someone Else")
    assert res["status"] == "skipped" and graph.posted == []


def test_an_unexpected_error_mid_run_is_reported_not_raised(graph, monkeypatch):
    async def _post(payload):
        raise ConnectionError("graph unreachable")

    monkeypatch.setattr(wa, "_post_message", _post)
    res = send(FakeConn(header()))
    assert (res["status"], res["sent"], res["failed"], res["reason"]) == ("failed", 0, 2, "exception")
