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
