"""The four dispatch-reminder mail cards.

What matters here is not layout but three invariants: the business head's copy is the
only one carrying action buttons, the destructive one is signed, and every value that
came from a user is escaped before it reaches the HTML.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_reminder_mail.py
"""
from __future__ import annotations

from datetime import date

from app.modules.sample.services import sample_mail_service as m

REQ = {
    "id": 42, "request_id": 25495623, "sample_type": "NPD",
    "npd_target_name": "Date Powder", "quantity": 3, "customer_name": "BigBasket",
    "expected_dispatch_date": date(2026, 9, 5), "warehouse": "W202",
}
BH = "bh@candorfoods.in"


def test_due_tomorrow_cards_carry_no_buttons():
    """The warning is informational on both copies — nothing to act on yet."""
    for html in (m._due_tomorrow_npd_html(REQ), m._due_tomorrow_owner_html(REQ)):
        assert "req_cancel" not in html and "req_redate" not in html


def test_overdue_npd_card_is_informatory_only():
    html = m._overdue_npd_html(REQ, days=3)
    assert "req_cancel" not in html and "req_redate" not in html
    assert "3 days overdue" in html


def test_overdue_owner_card_carries_both_actions():
    html = m._overdue_owner_html(REQ, days=3, bh_email=BH)
    assert "req_cancel=25495623" in html
    assert "req_redate=25495623" in html
    assert "Cancel request" in html and "Change expected date" in html


def test_action_links_address_the_page_by_its_pk_not_the_request_id():
    """The web route /modules/sample/<id> is keyed on the PK (42) while the query
    carries the 8-digit request_id (25495623) — the split _bh_signoff_reject_url
    already uses. Putting the request_id in the path opens a different requisition,
    or none at all."""
    html = m._overdue_owner_html(REQ, days=3, bh_email=BH)
    assert "/modules/sample/42?req_cancel=25495623" in html
    assert "/modules/sample/42?req_redate=25495623" in html
    assert "/modules/sample/25495623" not in html


def test_overdue_owner_trail_copy_has_the_buttons_stripped():
    """Everyone else on the trail sees the same card without a way to act — the links
    are bound to the BH's address, so a stray click could not work anyway."""
    html = m._overdue_owner_html(REQ, days=3, bh_email=None)
    assert "req_cancel" not in html and "req_redate" not in html


def test_action_links_are_signed():
    html = m._overdue_owner_html(REQ, days=3, bh_email=BH)
    from app.modules.sample.services.email_link_token import sign
    assert f"t={sign('req_cancel', 25495623, BH)}" in html
    assert f"t={sign('req_redate', 25495623, BH)}" in html


def test_one_day_overdue_reads_singular():
    assert "1 day overdue" in m._overdue_npd_html(REQ, days=1)


def test_user_text_is_escaped():
    """Customer names are free text and land in the mail — an unescaped one would
    inject markup into every recipient's inbox."""
    evil = {**REQ, "customer_name": '<script>alert(1)</script>'}
    html = m._due_tomorrow_npd_html(evil)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_every_card_renders_without_a_date():
    """expected_dispatch_date can be absent on a row that reached the builder by a
    path other than the scan; the card must degrade, not raise."""
    bare = {"id": 1, "request_id": 2, "sample_type": "NPD"}
    m._due_tomorrow_npd_html(bare)
    m._due_tomorrow_owner_html(bare)
    m._overdue_npd_html(bare, days=1)
    m._overdue_owner_html(bare, days=1, bh_email=BH)


# --- senders ----------------------------------------------------------------
import asyncio


class _MailConn:
    pass


def _patch(monkeypatch, *, npd, requestor, poc):
    async def _rec(conn, req):
        return {"to": [requestor] if requestor else list(npd), "cc": [poc] if poc else [],
                "npd": list(npd), "inventory": [], "production": [],
                "requestor": requestor, "sales_poc": poc}
    sent: list[dict] = []
    monkeypatch.setattr(m, "resolve_recipients", _rec)
    monkeypatch.setattr(m, "_send", lambda subj, html, to, **kw: sent.append(
        {"to": list(to), "cc": list(kw.get("cc") or []), "html": html}) or "mid")
    monkeypatch.setattr(m, "_broadcast", lambda subj, html, rec, **kw: sent.append(
        {"to": ["<broadcast>"], "cc": [], "html": html}))
    return sent


def test_npd_audience_goes_to_the_team_pool(monkeypatch):
    sent = _patch(monkeypatch, npd=["npd@x.in"], requestor="bh@x.in", poc="poc@x.in")
    ok = asyncio.run(m.notify_dispatch_due_tomorrow(_MailConn(), REQ, audience="npd"))
    assert ok is True
    assert sent[0]["to"] == ["npd@x.in"]


def test_owner_audience_addresses_bh_and_poc_together(monkeypatch):
    sent = _patch(monkeypatch, npd=["npd@x.in"], requestor="bh@x.in", poc="poc@x.in")
    ok = asyncio.run(m.notify_dispatch_due_tomorrow(_MailConn(), REQ, audience="owner"))
    assert ok is True
    assert sorted(sent[0]["to"]) == ["bh@x.in", "poc@x.in"]


def test_no_npd_recipient_reports_false_so_the_guard_is_not_claimed(monkeypatch):
    """Claiming the row on a mail that reached nobody would mark it sent forever."""
    sent = _patch(monkeypatch, npd=[], requestor="bh@x.in", poc=None)
    assert asyncio.run(m.notify_dispatch_due_tomorrow(_MailConn(), REQ, audience="npd")) is False
    assert sent == []


def test_no_business_head_reports_false(monkeypatch):
    sent = _patch(monkeypatch, npd=["npd@x.in"], requestor=None, poc=None)
    assert asyncio.run(m.notify_dispatch_overdue(
        _MailConn(), REQ, days=2, audience="owner")) is False


def test_overdue_owner_send_carries_the_buttons_and_the_poc_gets_a_button_less_copy(monkeypatch):
    """Design §4: T4's Cc is the sales POC alone — the button-less copy must NOT fan out
    to the full trail (that would be npd_team + inventory + production for an NPD/TRIAL
    request, mailed daily). Only _send is used here, never _broadcast."""
    sent = _patch(monkeypatch, npd=["npd@x.in"], requestor="bh@x.in", poc="poc@x.in")
    asyncio.run(m.notify_dispatch_overdue(_MailConn(), REQ, days=2, audience="owner"))
    assert not any(s["to"] == ["<broadcast>"] for s in sent), (
        "the button-less T4 copy must not go through _broadcast")
    bh_mail = [s for s in sent if s["to"] == ["bh@x.in"]]
    poc_mail = [s for s in sent if s["to"] == ["poc@x.in"]]
    assert bh_mail and "req_cancel" in bh_mail[0]["html"] and "req_redate" in bh_mail[0]["html"]
    assert poc_mail and "req_cancel" not in poc_mail[0]["html"]
    assert len(sent) == 2, "only the BH's buttoned copy and the POC's button-less copy"


def test_overdue_owner_skips_the_poc_copy_when_there_is_no_poc(monkeypatch):
    """No full-trail fallback when the POC is unresolved — the button-less copy is just
    skipped, same policy as the guard on the buttoned BH copy above it."""
    sent = _patch(monkeypatch, npd=["npd@x.in"], requestor="bh@x.in", poc=None)
    ok = asyncio.run(m.notify_dispatch_overdue(_MailConn(), REQ, days=2, audience="owner"))
    assert ok is True
    assert len(sent) == 1
    assert sent[0]["to"] == ["bh@x.in"]
