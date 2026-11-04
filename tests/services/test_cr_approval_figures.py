"""An approval is taken on the figures the approver was shown (C3).

Every approval link and WhatsApp card carries the fingerprint of the figures it was
sent with, and the review page sends the one it loaded. Once the return's figures
differ — it was edited after that — the decision is refused (409 figures_changed)
and nothing is written: the mail link's page says so before the Confirm, the
WhatsApp tap is answered with why, the review page asks for a reload. A link or
card sent before they carried one decides only while no request was sent and no
figure changed after it (cr_approval_log).

The fakes and fixtures are test_cr_approval_decisions'.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_approval_figures.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.modules.customer_returns.services import approval_service as svc
from app.modules.customer_returns.services import query_service, send_service
from app.modules.customer_returns.services import wa_notify as wa
from tests.services.test_cr_approval_decisions import (  # noqa: F401 — fixtures
    BH, BH_EMAIL, BH_PHONE, COMPANY, CR, LINE, TOKEN_KEY, FakeConn, client, get_cr, header, mail,
    no_guard_errors, tokens,
)
from tests.services.test_cr_approval_whatsapp import WAMID, graph, replies, tap  # noqa: F401 — fixtures

URL = "/api/v1/customer-returns/email-action"
FIGURES = query_service.figures_version({**header(), "lines": [LINE]})
CHANGED = [{**LINE, "qty": "5", "value": "4750"}]          # the return after an edit: one more unit


def run(coro):
    return asyncio.run(coro)


def entry(event="sent", at=None, figures=FIGURES):
    return {"company": COMPANY, "rtv_id": CR, "event": event, "figures": figures, "actor": "someone",
            "at": at or datetime.now(timezone.utc) + timedelta(days=1), "outcome": None}


def refusal(exc):
    return exc.value.detail["error"], exc.value.detail["message"]


# ── the fingerprint ──────────────────────────────────────────────────────────
def test_the_fingerprint_follows_the_figures_and_nothing_else():
    base = {**header(), "lines": [LINE, {**LINE, "item_description": "ALMOND 25KG", "qty": "2"}]}
    fp = query_service.figures_version(base)
    # Line order, number spelling and spacing aren't figures.
    assert query_service.figures_version({**base, "lines": base["lines"][::-1]}) == fp
    assert query_service.figures_version({**base, "lines": [{**LINE, "qty": "4.000", "rate": "950.00"},
                                                            base["lines"][1]]}) == fp
    assert query_service.figures_version({**base, "customer": " Sample  Foods Pvt Ltd "}) == fp
    # Remarks, transport details and the business head aren't either.
    assert query_service.figures_version({**base, "remark": "late", "vehicle_number": "MH01",
                                          "business_head": "Someone Else"}) == fp
    # Every figure is.
    for change in ({"customer": "Other Foods"}, {"factory_unit": "W101"}, {"invoice_number": "INV-1"},
                   {"challan_no": "CH-1"}):
        assert query_service.figures_version({**base, **change}) != fp
    for change in ({"qty": "5"}, {"rate": "951"}, {"value": "1"}, {"net_weight": "41"}, {"uom": "25"},
                   {"item_description": "CASHEW W240 10KG"}):
        assert query_service.figures_version({**base, "lines": [{**LINE, **change}, base["lines"][1]]}) != fp
    assert query_service.figures_version({**base, "lines": base["lines"][:1]}) != fp


def test_the_detail_carries_the_fingerprint_of_what_it_shows():
    detail = {**header(), "lines": [LINE]}
    assert query_service.figures_version(detail) == FIGURES


# ── the decision ─────────────────────────────────────────────────────────────
def test_a_decision_on_the_figures_the_return_has_goes_through():
    conn = FakeConn(header())
    res = run(svc.apply_cr_action(conn, COMPANY, CR, "approve", BH_EMAIL, seen={"figures": FIGURES}))
    assert res["status"] == "Approved" and not res["already_actioned"]
    # The figures were read with the header row held, then the decision written.
    sql = [c[1] for c in conn.calls]
    locked = next(i for i, s in enumerate(sql) if s.endswith("FOR UPDATE"))
    assert sql[locked].startswith("SELECT customer, factory_unit, invoice_number, challan_no FROM cfpl_rtv_header")
    assert "FROM cfpl_rtv_lines l" in sql[locked + 1] and sql[locked + 2].startswith("UPDATE cfpl_rtv_header")


@pytest.mark.parametrize("action", ["approve", "reject", "hold"])
def test_a_decision_on_figures_the_return_no_longer_has_is_refused_and_nothing_written(action):
    conn = FakeConn(header(), lines=CHANGED)
    with pytest.raises(Exception) as exc:
        run(svc.apply_cr_action(conn, COMPANY, CR, action, BH_EMAIL, seen={"figures": FIGURES}))
    assert exc.value.status_code == 409
    error, message = refusal(exc)
    assert error == "figures_changed"
    assert message == (f"{CR} was changed after this request was sent to you, so it doesn't show the "
                       "return's figures any more. Decide from its latest approval request, or open it in the ERP.")
    assert conn.updates() == [] and conn.headers[CR]["status"] == "Pending"


def test_a_decided_return_is_reported_decided_whatever_figures_were_seen():
    conn = FakeConn(header(status="Approved"), lines=CHANGED)
    res = run(svc.apply_cr_action(conn, COMPANY, CR, "reject", BH_EMAIL, seen={"figures": FIGURES}))
    assert res == {"rtv_id": CR, "status": "Approved", "already_actioned": True}


def test_no_fingerprint_is_no_check():
    conn = FakeConn(header(), lines=CHANGED)
    assert run(svc.apply_cr_action(conn, COMPANY, CR, "approve", BH_EMAIL))["status"] == "Approved"


# ── a link or card from before they carried a fingerprint ────────────────────
SENT = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("log, ok", [
    ([], True),                                                          # nothing since: as before
    ([entry("sent", SENT - timedelta(hours=1))], True),                  # only before it
    ([entry("sent", SENT + timedelta(hours=1))], False),                 # sent again since
    ([entry("changed", SENT + timedelta(minutes=5), "other")], False),   # edited since
])
def test_an_older_link_decides_only_while_nothing_was_recorded_after_it(log, ok):
    conn = FakeConn(header(), approval_log=log)
    coro = svc.apply_cr_action(conn, COMPANY, CR, "approve", BH_EMAIL, seen={"sent_at": SENT})
    if ok:
        assert run(coro)["status"] == "Approved"
    else:
        with pytest.raises(Exception) as exc:
            run(coro)
        assert refusal(exc)[0] == "figures_changed" and conn.updates() == []


def test_without_the_log_table_an_older_link_decides_as_before():
    conn = FakeConn(header(), has_approval_log=False)
    assert run(svc.apply_cr_action(conn, COMPANY, CR, "approve", BH_EMAIL, seen={"sent_at": SENT}))["status"] == "Approved"


# ── the mailed link ──────────────────────────────────────────────────────────
def _page(client, conn, token):
    return client(conn, auth=False).get(URL, params={"token": token})


def _confirm(client, conn, token):
    return client(conn, auth=False).post(URL, data={"token": token})


def test_the_link_carries_the_figures_and_its_page_shows_the_lines(client, tokens, mail):
    conn = FakeConn(header())
    token = tokens(COMPANY, CR, "approve", BH_EMAIL, FIGURES)
    assert jwt.decode(token, TOKEN_KEY, algorithms=["HS256"])["figures"] == FIGURES
    res = _page(client, conn, token)
    assert res.status_code == 200 and "Confirm approve" in res.text
    for shown in ("CASHEW W320 10KG", ">4<", ">950<", ">3800<"):
        assert shown in res.text
    assert _confirm(client, conn, token).status_code == 200
    assert conn.headers[CR]["status"] == "Approved"


def test_a_link_for_figures_since_changed_says_so_before_and_at_the_confirm(client, tokens, mail):
    conn = FakeConn(header(), lines=CHANGED)
    token = tokens(COMPANY, CR, "approve", BH_EMAIL, FIGURES)
    page = _page(client, conn, token)
    assert page.status_code == 409 and "Action not available" in page.text
    assert "was changed after this request was sent to you" in page.text and "Confirm" not in page.text
    done = _confirm(client, conn, token)
    assert done.status_code == 409 and "Nothing was done" in done.text
    assert conn.updates() == [] and mail.sent == []


def test_an_older_link_is_refused_once_the_return_was_sent_again(client, tokens, mail):
    conn = FakeConn(header(), approval_log=[entry("sent")])     # recorded after the link was minted
    token = tokens(COMPANY, CR, "approve", BH_EMAIL)              # no figures: minted before they were carried
    assert _confirm(client, conn, token).status_code == 409
    assert conn.updates() == []


# ── the WhatsApp card ────────────────────────────────────────────────────────
def _card(**over):
    return {"wamid": WAMID, "company": COMPANY, "rtv_id": CR, "wa_phone": BH_PHONE, **over}


def test_a_tap_on_a_card_with_the_return_s_figures_decides(replies, mail, get_cr):
    conn = FakeConn(header(), wa_messages=[_card(figures=FIGURES)])
    assert tap(conn, BH_PHONE)["status"] == "Approved"


def test_a_tap_on_a_card_whose_return_changed_is_answered_and_changes_nothing(replies, mail, get_cr):
    conn = FakeConn(header(), lines=CHANGED, wa_messages=[_card(figures=FIGURES)])
    res = tap(conn, BH_PHONE)
    assert (res["ok"], res["reason"], res["error"], res["status_code"]) == (False, "cr_refused", "figures_changed", 409)
    assert replies == [(BH_PHONE, f"Customer return {CR} was not changed. {CR} was changed after this request "
                                  "was sent to you, so it doesn't show the return's figures any more. Decide from "
                                  "its latest approval request, or open it in the ERP.")]
    assert conn.updates() == [] and mail.sent == []


def test_an_older_card_is_judged_by_when_it_was_sent(replies, mail, get_cr):
    sent = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
    conn = FakeConn(header(), wa_messages=[_card(created_at=sent)],
                    approval_log=[entry("changed", sent + timedelta(hours=2), "other")])
    assert tap(conn, BH_PHONE)["error"] == "figures_changed"
    conn = FakeConn(header(), wa_messages=[_card(created_at=sent)],
                    approval_log=[entry("changed", sent - timedelta(hours=2), "other")])
    assert tap(conn, BH_PHONE)["status"] == "Approved"


def test_a_card_is_stored_with_the_figures_it_shows(graph):
    conn = FakeConn(header())
    run(wa.notify_cr_head_approval(conn, {**header(), "company": COMPANY, "lines": [LINE]}))
    assert {m["figures"] for m in conn.wa.values()} == {FIGURES}


def test_before_migration_137_a_card_is_stored_without_them(graph):
    import asyncpg

    class _NoColumn(FakeConn):
        async def execute(self, sql, *args):
            if "wa_phone, figures)" in sql:
                raise asyncpg.UndefinedColumnError('column "figures" does not exist')
            return await super().execute(sql, *args)

    conn = _NoColumn(header())
    res = run(wa.notify_cr_head_approval(conn, {**header(), "company": COMPANY, "lines": [LINE]}))
    assert res["status"] == "sent" and {m["figures"] for m in conn.wa.values()} == {None}


# ── the review page ──────────────────────────────────────────────────────────
APPROVE = f"/api/v1/customer-returns/{COMPANY}/{CR}/approve"


def test_the_review_page_decides_on_the_figures_it_loaded(client, mail, get_cr):
    conn = FakeConn(header())
    res = client(conn, BH).post(APPROVE, json={"action": "approve", "figures": FIGURES})
    assert res.status_code == 200 and res.json()["status"] == "Approved"


def test_a_review_page_opened_before_an_edit_is_asked_to_reload(client, mail, get_cr):
    conn = FakeConn(header(), lines=CHANGED)
    res = client(conn, BH).post(APPROVE, json={"action": "approve", "figures": FIGURES})
    assert res.status_code == 409
    assert res.json()["message"] == f"{CR} was changed after you opened it. Reload it to see its figures, then decide."
    assert conn.updates() == []


def test_the_detail_says_whether_the_last_request_showed_the_current_figures(client, monkeypatch):
    async def _get_cr(conn, company, cr_id):      # a detail the response model accepts
        lines = [{**x, "rtv_id": cr_id, "material_type": "RM", "item_category": "Nuts", "sub_category": "Cashew",
                  "carton_weight": "0"} for x in conn.lines[cr_id]]
        detail = {**conn.headers[cr_id], "lines": lines, "boxes": []}
        return {**detail, "figures_version": query_service.figures_version(detail)}

    monkeypatch.setattr(query_service, "get_cr", _get_cr)
    conn = FakeConn(header(), approval_log=[entry("sent", datetime(2026, 10, 25, tzinfo=timezone.utc))])
    body = client(conn, BH).get(f"/api/v1/customer-returns/{COMPANY}/{CR}").json()
    assert body["figures_version"] == FIGURES
    assert body["approval_request"] == {"sent_at": "2026-10-25T05:30:00+05:30", "sent_by": "someone",
                                        "figures_current": True}
    conn = FakeConn(header(), lines=CHANGED, approval_log=[entry("sent")])
    assert client(conn, BH).get(f"/api/v1/customer-returns/{COMPANY}/{CR}").json()["approval_request"][
        "figures_current"] is False
    assert client(FakeConn(header()), BH).get(f"/api/v1/customer-returns/{COMPANY}/{CR}").json()[
        "approval_request"] is None


# ── sending ──────────────────────────────────────────────────────────────────
def test_every_link_sent_carries_the_figures_and_the_request_is_recorded(mail, get_cr, monkeypatch):
    monkeypatch.setattr(wa, "_wa_enabled", lambda: False)
    conn = FakeConn(header())
    res = run(send_service.send_for_approval(conn, COMPANY, CR, "warehouse@candorfoods.in"))
    assert res["status"] == "sent"
    links = [t for m in mail.sent for t in m["text"].split() if "email-action?token=" in t]
    assert links and {jwt.decode(t.split("token=")[1], TOKEN_KEY, algorithms=["HS256"])["figures"]
                      for t in links} == {FIGURES}
    (logged,) = conn.approval_log
    assert (logged["event"], logged["figures"], logged["actor"]) == ("sent", FIGURES, "warehouse@candorfoods.in")


def test_a_request_that_reached_nobody_is_not_recorded(mail, get_cr, monkeypatch):
    monkeypatch.setattr(wa, "_wa_enabled", lambda: False)
    mail.smtp_host = ""
    conn = FakeConn(header())
    assert run(send_service.send_for_approval(conn, COMPANY, CR, "x"))["status"] == "failed"
    assert conn.approval_log == []


def test_without_the_log_table_a_request_still_goes(mail, get_cr, monkeypatch, caplog):
    monkeypatch.setattr(wa, "_wa_enabled", lambda: False)
    conn = FakeConn(header(), has_approval_log=False)
    assert run(send_service.send_for_approval(conn, COMPANY, CR, "x"))["status"] == "sent"
    assert "migration 137" in caplog.text
