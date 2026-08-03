"""One NPD transaction must produce ONE mail trail, and action buttons must never
broadcast.

Guards the four invariants the NPD mail rework exists to hold (no DB/network — a fake
conn plus a fake SMTP transport):
  1. every mail of a transaction shares ONE subject (Gmail breaks a thread otherwise);
  2. exactly ONE mail roots the thread, and every other mail replies to that root;
  3. a signed action URL never appears in a mail with more than one recipient;
  4. a standalone dev job card (no source requisition) roots its OWN thread — a
     different transaction never shares a trail.

If these break, the symptom is the bug this replaced: a requisition, a review, an
acceptance and an approval arriving as four unrelated mails.
"""
import asyncio
import types

import pytest

from app.modules.sample.services import email_link_token
from app.modules.sample.services import sample_mail_service as m

BH, IM, NPD1, NPD2 = ("bh@x.in", "inv@x.in", "npd1@x.in", "npd2@x.in")
POC = "sales.poc@x.in"

REQ = {"id": 7, "request_id": 88881111, "sample_type": "NPD", "requestor_user_id": 100,
       "npd_target_name": "Peri Peri Fries", "warehouse": "W202", "quantity": 12,
       "company_name": "Candor", "customer_name": "ACME", "requestor_team": "Ravi",
       "sales_poc_user_id": 300, "sales_poc_name": "Sana Sales", "sales_poc_email": POC,
       "returnable": True, "non_returnable": False, "paid": False, "amount": 0}
JC = {"id": 55550000, "title": "Peri Peri Fries", "fg_sku_name": "Peri Peri Fries",
      "target_qty": 12, "uom": "kg", "warehouse": "W202", "company_name": "Candor",
      "customer_name": "ACME", "source_requisition_id": 7,
      "returnable": True, "non_returnable": False, "paid": False, "amount": 0}

USERS = {100: BH, 200: IM, 300: POC}
NAMES = {100: "Ravi Menon", 200: "Asha Pillai", 300: "Sana Sales"}
ROLES = {"npd_team": [NPD1, NPD2], "inventory_manager": [IM]}

BUTTON_MARKERS = ("/email/npd-action", "/email/promote-action", "promote_reject=")


class _Conn:
    """Only the three query shapes sample_mail_service issues."""

    def __init__(self, linked: bool = True):
        self.linked = linked

    async def fetch(self, sql, *p):
        if "auth_role" in sql and "role_name = $1" in sql:
            return [{"email": e} for e in ROLES.get(p[0], [])]
        return []

    async def fetchval(self, sql, *p):
        if "SELECT email FROM auth_user" in sql:
            return USERS.get(p[0])
        if "SELECT full_name FROM auth_user" in sql:
            return NAMES.get(p[0])
        return None

    async def fetchrow(self, sql, *p):
        if "JOIN sample_requisitions" in sql:
            return dict(REQ) if self.linked else None
        if "FROM npd_dev_job_cards" in sql:
            return dict(JC)
        return None


@pytest.fixture
def sent(monkeypatch):
    """Capture what would go on the wire; run sends inline instead of on a daemon thread."""
    box = []

    class _SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, msg, from_addr=None, to_addrs=None):
            box.append({
                "subject": msg["Subject"], "msgid": msg["Message-ID"],
                "irt": msg["In-Reply-To"], "refs": msg["References"],
                "rcpts": to_addrs,
                "html": msg.get_payload()[1].get_payload(decode=True).decode(),
            })

    class _Thread:
        def __init__(self, target=None, daemon=None): self._t = target
        def start(self): self._t()

    monkeypatch.setattr(m.smtplib, "SMTP", _SMTP)
    monkeypatch.setattr(m.threading, "Thread", _Thread)
    monkeypatch.setattr(m, "Settings", lambda: types.SimpleNamespace(
        SMTP_HOST="smtp.test", SMTP_PORT=587, SMTP_EMAIL="erp@x.in",
        SMTP_APP_PASSWORD="pw", PUBLIC_BACKEND_URL="https://api.test",
        WEB_APP_URL="https://app.test"))
    monkeypatch.setattr(email_link_token, "sign", lambda *a: "TOKEN")
    return box


async def _full_trail(conn):
    """Every mail one NPD transaction emits, in lifecycle order."""
    await m.notify_requisition_event(conn, REQ, event="created")
    await m.notify_npd_review_email(conn, REQ)
    await m.notify_requisition_event(conn, REQ, event="accepted", reason="feasible")
    await m.notify_promote_review_email(conn, dev_jc_id=55550000, requestor_uid=100)
    await m.notify_promote_status_email(conn, dev_jc_id=55550000, gate="INV_MGR",
                                        action="ACCEPT", actor_user_id=200,
                                        result={"status": "PENDING_APPROVAL", "remaining": 1})
    await m.notify_promote_status_email(conn, dev_jc_id=55550000, gate="REQUESTOR_BH",
                                        action="ACCEPT", actor_user_id=100,
                                        result={"status": "PROMOTED"})
    await m.notify_dev_dispatch_email(conn, dev_jc_id=55550000, dispatch_id=99990000, seq=1,
                                      qty=12, uom="kg", recipient="ACME", actor_user_id=100)


def test_whole_transaction_is_one_thread(sent):
    asyncio.run(_full_trail(_Conn()))
    assert len(sent) > 1
    # ONE subject across the trail is the invariant; the exact literal is not, because the
    # mail-identity layer prefixes a constant module glyph (app/core/mail_identity.py).
    subjects = {s["subject"] for s in sent}
    assert len(subjects) == 1, f"trail split across subjects: {subjects}"
    assert subjects.pop().endswith("NPD Sample Request 88881111")

    root_key = m._thread_key(88881111)
    roots = [s for s in sent if s["irt"] is None]
    assert len(roots) == 1, "a transaction must have exactly one thread root"
    assert roots[0]["msgid"] == root_key
    for s in sent:
        if s["irt"] is not None:
            assert s["irt"] == root_key and s["refs"] == root_key
            assert s["msgid"] != root_key, "a reply reused the root Message-ID"


def test_action_buttons_never_reach_more_than_one_recipient(sent):
    asyncio.run(_full_trail(_Conn()))
    buttoned = [s for s in sent if any(mark in s["html"] for mark in BUTTON_MARKERS)]
    # Guard against a vacuous pass: the trail MUST contain buttoned mails (NPD review +
    # both promote gates), otherwise "no mail broke the rule" would mean "no mail had
    # buttons at all" — the opposite of working.
    assert len(buttoned) >= 3, f"expected review + 2 promote gate mails, got {len(buttoned)}"
    for s in buttoned:
        assert len(s["rcpts"]) == 1, f"action URL sent to {len(s['rcpts'])} recipients"


def test_gate_holders_get_buttons_and_everyone_else_gets_the_same_card_without(sent):
    asyncio.run(m.notify_npd_review_email(_Conn(), REQ))
    buttoned = [s for s in sent if "/email/npd-action" in s["html"]]
    plain = [s for s in sent if "/email/npd-action" not in s["html"]]
    # one buttoned copy per npd_team reviewer, addressed to them alone...
    assert sorted(s["rcpts"][0] for s in buttoned) == sorted(ROLES["npd_team"])
    # ...and the button-less copy excludes exactly those reviewers.
    assert plain and not set(ROLES["npd_team"]) & set(plain[0]["rcpts"])


def test_standalone_dev_jc_roots_its_own_thread(sent):
    asyncio.run(m.notify_promote_review_email(_Conn(linked=False), dev_jc_id=55550000))
    jc_key = m._jc_thread_key(55550000)
    subjects = {s["subject"] for s in sent}
    assert len(subjects) == 1 and subjects.pop().endswith("NPD Dev Job Card 55550000")
    assert any(s["msgid"] == jc_key for s in sent), "standalone JC never rooted its thread"
    assert all(s["irt"] in (None, jc_key) for s in sent)
    assert all(s["irt"] != m._thread_key(88881111) for s in sent), \
        "standalone JC leaked into the requisition's trail"


def test_whatsapp_stub_name_never_reaches_the_trail(sent):
    """_WaUser.full_name is the literal 'WhatsApp'; the DB lookup must win."""
    asyncio.run(m.notify_promote_status_email(
        _Conn(), dev_jc_id=55550000, gate="INV_MGR", action="ACCEPT",
        actor_user_id=200, actor_name="WhatsApp",
        result={"status": "PENDING_APPROVAL", "remaining": 1}))
    assert sent and "Asha Pillai" in sent[-1]["html"]
    assert "WhatsApp" not in sent[-1]["html"]


def test_sales_poc_is_cced_on_every_mail_and_named_in_the_card(sent):
    """The sales POC follows the whole trail but approves nothing, so they must appear on
    the Cc of the button-less mails and never receive a buttoned copy."""
    asyncio.run(_full_trail(_Conn()))
    broadcasts = [s for s in sent
                  if not any(mark in s["html"] for mark in BUTTON_MARKERS)]
    assert broadcasts, "no button-less mail in the trail"
    for s in broadcasts:
        assert POC in s["rcpts"], f"sales POC missing from a trail mail: {s['subject']}"
    for s in sent:
        if any(mark in s["html"] for mark in BUTTON_MARKERS):
            assert s["rcpts"] != [POC], "sales POC received an action-buttoned mail"
    # ...and the name is rendered on EVERY card — requisition AND dev-JC. Asserting "any"
    # would pass on the requisition cards alone and never catch the dev-JC ones going
    # blank, which is exactly what happens without _with_sales_poc: the job card has no
    # POC columns, so promote/dispatch would show an em-dash mid-thread.
    for s in broadcasts:
        assert "Sana Sales" in s["html"], f"sales POC not rendered in: {s['subject']}"


def test_sales_poc_falls_back_to_the_named_user_when_no_email_stored(sent):
    """A POC chosen before the email snapshot existed still resolves via sales_poc_user_id."""
    req = {**REQ, "sales_poc_email": None}

    async def go():
        await m.notify_requisition_event(_Conn(), req, event="created")

    asyncio.run(go())
    assert sent and POC in sent[0]["rcpts"], "fell back to no POC instead of the user lookup"


# ── general sample flow (BASIS_RM / BASIS_FG / INTERNAL) ─────────────────────
BASIS = {**REQ, "id": 8, "request_id": 77772222, "sample_type": "BASIS_RM",
         "npd_target_name": None}

GENERAL_LIFECYCLE = ["submitted", "approved", "issued", "verified",
                     "gate pass issued", "closed"]


def test_general_sample_flow_threads_into_one_trail(sent):
    """The non-NPD flow must get the same single trail the NPD flow gets — it used to
    receive exactly one 'created' mail and then nothing at all."""
    async def go():
        await m.notify_requisition_event(_Conn(), BASIS, event="created")
        for ev in GENERAL_LIFECYCLE:
            await m.notify_requisition_event(_Conn(), BASIS, event=ev)

    asyncio.run(go())
    assert len(sent) == 1 + len(GENERAL_LIFECYCLE)

    subjects = {s["subject"] for s in sent}
    assert len(subjects) == 1, f"general trail split across subjects: {subjects}"
    subject = subjects.pop()
    # A raw-material sample must not announce itself as NPD.
    assert subject.endswith("Sample Request 77772222")
    assert "NPD" not in subject

    root_key = m._thread_key(77772222)
    roots = [s for s in sent if s["irt"] is None]
    assert len(roots) == 1 and roots[0]["msgid"] == root_key
    assert all(s["irt"] == root_key for s in sent if s["irt"] is not None)


def test_general_flow_excludes_npd_team_but_keeps_inventory_and_poc(sent):
    """npd_team has no business on a BASIS_RM trail; inventory and the sales POC do."""
    asyncio.run(m.notify_requisition_event(_Conn(), BASIS, event="created"))
    rcpts = set(sent[0]["rcpts"])
    assert IM in rcpts and POC in rcpts and BH in rcpts
    assert not ({NPD1, NPD2} & rcpts), "npd_team Cc'd on a raw-material sample"


def test_unknown_event_still_sends_rather_than_crashing(sent):
    """Service layers add states over time; an unmapped one must degrade to a neutral
    card, never raise into the caller's request."""
    asyncio.run(m.notify_requisition_event(_Conn(), BASIS, event="teleported"))
    assert len(sent) == 1 and "TELEPORTED" in sent[0]["html"]


def test_gate_pass_link_is_navigation_only_not_a_signed_action(sent):
    asyncio.run(m.notify_requisition_event(
        _Conn(), BASIS, event="gate pass issued",
        link="https://app.test/modules/sample/8", link_label="Open gate pass"))
    html = sent[0]["html"]
    assert "Open gate pass" in html and "/modules/sample/8" in html
    assert not any(mark in html for mark in BUTTON_MARKERS), \
        "a signed action URL leaked into the broadcast copy"


# ── rendering guards ─────────────────────────────────────────────────────────
import re as _re

_UNRENDERED = _re.compile(r"\{_[A-Z_]+\}|\{[a-z_]+\}")


def _all_cards():
    """One rendered sample of every card type, for whole-file assertions.

    Callers MUST take the `sent` fixture: it stubs Settings(), and without it these
    renderers hit the real Settings, which requires DATABASE_URL from a .env — making the
    test pass on a developer box and fail on CI or a fresh clone."""
    return {
        "review (buttoned)": m._review_html(REQ, "npd1@x.in"),
        "review (plain)": m._review_html(REQ, None),
        "requisition event": m._requisition_event_html(REQ, "approved", reason="ok"),
        "requisition + link": m._requisition_event_html(
            BASIS, "gate pass issued", link="https://app.test/x", link_label="Open gate pass"),
        "promote (buttoned)": m._promote_html(JC, "Business head", "https://a/1", "https://a/2"),
        "promote (plain)": m._promote_html(JC, None, None, None),
        "promote status": m._promote_status_html(
            JC, gate="INV_MGR", action="ACCEPT", actor_name="A", remarks="r",
            result={"status": "PROMOTED"}),
        "dispatch": m._dispatch_html(
            JC, dispatch_id=1, seq=1, qty=12, uom="kg", recipient="ACME",
            actor_name="N", dc_url="https://app.test/dc"),
    }


def test_no_unrendered_style_tokens_in_any_card(sent):
    """A design token written into a NON-f-string renders as the literal '{_T_LEAD}px' in
    the customer's mail client. Cheap to do, invisible in code review, so assert it."""
    for name, html in _all_cards().items():
        leaked = _UNRENDERED.findall(html)
        assert not leaked, f"unrendered placeholder(s) in {name}: {sorted(set(leaked))[:5]}"


def test_every_card_uses_the_shared_type_scale(sent):
    """Guards against a card drifting back to hardcoded small type: no font-size below the
    smallest token (11px eyebrow/label) may appear."""
    for name, html in _all_cards().items():
        sizes = [int(x) for x in _re.findall(r"font-size:(\d+)px", html)]
        assert sizes, f"{name} sets no font sizes"
        assert min(sizes) >= 11, f"{name} has {min(sizes)}px type — below the scale floor"
        # the request id / key figures must dominate
        assert max(sizes) >= m._T_KEY, f"{name} has no large focal type (max {max(sizes)}px)"


# ── production request (general flow, no NPD) ────────────────────────────────
def test_production_request_renders_on_the_general_trail(sent):
    """A general sample whose article must be MADE says so on its own trail. The card must
    talk about production, never about NPD — NPD runs a separate module."""
    asyncio.run(m.notify_requisition_event(
        _Conn(), {**BASIS, "sample_type": "BASIS_FG"}, event="production requested"))
    assert len(sent) == 1
    html = sent[0]["html"]
    assert "ITEM TO BE MADE FOR SAMPLING" in html
    assert "NPD" not in html, "the general production request must not mention NPD"
