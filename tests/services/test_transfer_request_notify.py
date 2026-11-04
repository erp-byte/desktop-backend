"""Transfer request notices (services/request_notify.py): who is told at each step,
what the email and the WhatsApp message carry - checked against the four Meta-approved
templates - that a failed send never fails or undoes the action, and that the request
services schedule the notice only once the action has committed.

The connection is test_transfer_request_decisions' in-memory fake, taught the few
reads a notice makes (the permission holders, the requester, display names) and the
sent-message record. Mail and the Graph API are replaced by recorders; nothing touches
a database, an SMTP server or Meta.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_request_notify.py
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.transfer import schemas
from app.modules.transfer.services import request_action_token as T
from app.modules.transfer.services import request_decision_service as svc
from app.modules.transfer.services import request_notify as N
from app.modules.transfer.services import request_service, scan_service
from tests.services import test_transfer_request_decisions as D

RID = D.RID
RAISER = D.RAISER
TEST_KEY = "transfer-request-link-test-key-0123456789"
NO = "REQ202610121030"


def _person(uid, email, phone, warehouses=(), name=None):
    return {"user_id": uid, "full_name": name or email.split("@")[0].title(), "email": email,
            "phone": phone, "allowed_warehouses": list(warehouses)}


STORE_A185 = _person(101, "store.a185@candorfoods.in", "9000000101", ["A185"], "Kaushal Patil")
STORE_SPELLED = _person(102, "store.two@candorfoods.in", "+91 90000 00102", ["a-185"])
STORE_ANYWHERE = _person(103, "store.central@candorfoods.in", "9000000103")
STORE_W202 = _person(104, "store.w202@candorfoods.in", "9000000104", ["W202"])
RAISER_HOLDS_IT = _person(105, "Floor.W202@CandorFoods.in", "9000000105", ["A185"])
MANAGER = _person(201, "inventory.manager@candorfoods.in", "9000000201", name="Ravi Kumar")
MANAGER_2 = _person(202, "inventory.sub@candorfoods.in", "", name="Sub Manager")
REQUESTER = _person(301, RAISER, "9000000301", name="Floor W202")

GRANTS = {"accept": [STORE_A185, STORE_SPELLED, STORE_ANYWHERE, STORE_W202, RAISER_HOLDS_IT],
          # the store user who accepted / held holds approve too; the raiser holds it as well
          "approve": [MANAGER, MANAGER_2, {**STORE_A185}, RAISER_HOLDS_IT]}

# The approved templates as fetched from Meta: (header variables, body variables, buttons).
TEMPLATES = {
    "interunit_transfer_request_store": (1, 7, ("Accept", "Hold")),
    "interunit_request_approval": (1, 7, ("Approve", "Hold", "Reject")),
    "interunit_transfer_request_held": (1, 8, ()),
    "interunit_request_decision": (1, 7, ()),
}


class NoticeConn(D.FakeConn):
    """The decisions fake plus what a notice reads and records."""

    def __init__(self, *requests, lines=(), transfers=(), grants=None, users=(REQUESTER, MANAGER, STORE_A185)):
        super().__init__(*requests, lines=lines, transfers=transfers)
        self.grants = GRANTS if grants is None else grants
        self.users = [dict(u) for u in users]
        self.recorded: list[tuple] = []
        self.asked: list[str] = []

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT user_id, full_name, phone FROM auth_user WHERE lower(btrim(email)) = lower($1)"):
            self._log("fetchrow", sql, args)
            u = next((u for u in self.users if u["email"].lower() == args[0].strip().lower()), None)
            return {"user_id": u["user_id"], "full_name": u["full_name"], "phone": u["phone"]} if u else None
        if s.startswith("SELECT box_number, box_id, transaction_no"):
            self._log("fetchrow", sql, args)
            return {"box_number": 1, "box_id": args[1], "transaction_no": args[2], "article": "PISTA",
                    "lot_number": None, "batch_number": None, "net_weight": "25", "gross_weight": "25.2"}
        return await super().fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM auth_role_permission rp" in s:
            self._log("fetch", sql, args)
            self.asked.append(args[0])
            return [dict(p) for p in self.grants.get(args[0], [])]
        if s.startswith("SELECT lower(btrim(email)) AS email, full_name FROM auth_user"):
            self._log("fetch", sql, args)
            return [{"email": u["email"].lower(), "full_name": u["full_name"]}
                    for u in self.users if u["email"].lower() in args[0]]
        return await super().fetch(sql, *args)

    async def executemany(self, sql, rows):
        s = " ".join(sql.split())
        assert s.startswith("INSERT INTO transfer_request_wa_message"), s
        self.recorded.extend(rows)


class _BrokenPool:
    def acquire(self):
        raise ConnectionError("pool closed")


@pytest.fixture
def sent(monkeypatch):
    """Record mails and WhatsApp messages instead of sending them; sign links with a test key."""
    out = SimpleNamespace(mails=[], whatsapp=[], settings=SimpleNamespace(
        SMTP_HOST="smtp.test", SMTP_PORT=587, SMTP_EMAIL="erp@candorfoods.in", SMTP_APP_PASSWORD="pw",
        PUBLIC_BACKEND_URL="http://backend.test", WEB_APP_URL="https://erp.test",
        WHATSAPP_ENABLED=True, WHATSAPP_ACCESS_TOKEN="token", WHATSAPP_PHONE_NUMBER_ID="123",
        WHATSAPP_GRAPH_BASE="https://graph.test/v21.0"))
    out.fail_mail_to, out.fail_wa_to = set(), set()

    async def send_mail(subject, plain, html, to_addr, **kw):
        if to_addr in out.fail_mail_to:
            raise OSError("SMTP down")
        out.mails.append({"subject": subject, "plain": plain, "html": html, "to": to_addr, **kw})
        return True

    async def post_template(settings, message):
        if message["to"] in out.fail_wa_to:
            raise RuntimeError("HTTP 400 - bad number")
        out.whatsapp.append(message)
        return f"wamid.{message['to']}"

    monkeypatch.setattr(N, "current_settings", lambda: out.settings)
    monkeypatch.setattr(N, "_send_mail", send_mail)
    monkeypatch.setattr(N, "_post_template", post_template)
    monkeypatch.setattr(T, "_secret", lambda settings=None: TEST_KEY)
    monkeypatch.setattr(T, "_alg", lambda settings=None: "HS256")
    return out


def _deliver(conn, event):
    return asyncio.run(N.deliver(D._Pool(conn), event, RID))


def _links(text):
    return [T.verify_token(t) for t in re.findall(r"request-email-action\?token=([\w.-]+)", text)]


def _parts(message):
    """(template name, header texts, body texts, [(index, payload)]) of a sent template."""
    comps = message["template"]["components"]
    texts = lambda kind: [p["text"] for c in comps if c["type"] == kind for p in c["parameters"]]
    buttons = [(c["index"], c["parameters"][0]["payload"]) for c in comps if c["type"] == "button"]
    assert all(c["sub_type"] == "quick_reply" and c["parameters"][0]["type"] == "payload"
               for c in comps if c["type"] == "button")
    return message["template"]["name"], texts("header"), texts("body"), buttons


def _store_held(**over):
    return D._req(**{"store_decision": "Held", "store_decided_by": STORE_A185["email"],
                     "store_decided_at": datetime(2026, 10, 12, 11, 15), "store_hold_reason": "counting the stock",
                     **over})


def _decided(**over):
    return D._accepted(**{"status": "Approved", "manager_decision": "Approved", "manager_decided_by": MANAGER["email"],
                          "manager_decided_at": datetime(2026, 10, 12, 12, 0), **over})


TRANSFER = {"id": D.TRANSFER_ID, "challan_no": "CH-2026-0042", "status": "Dispatch", "request_id": RID,
            "stock_trf_date": None, "from_site": "A185", "to_site": "W202", "remark": None,
            "vehicle_no": "MH43BP6885", "driver_name": "Ramesh", "approved_by": "Yash",
            "created_by": STORE_A185["email"], "created_ts": datetime(2026, 10, 12, 13, 0)}


# ── raised -> the store of the supplying site ─────────────────────────────────

def test_a_raised_request_goes_to_the_store_users_who_can_answer_for_the_from_site(sent):
    conn = NoticeConn(D._req(), lines=[D._req_line()])
    report = _deliver(conn, "raised")

    assert conn.asked == ["accept"]
    # Locked to A185 (in any spelling) or not locked at all; not W202; not the raiser.
    assert [m["to"] for m in sent.mails] == [STORE_A185["email"], STORE_SPELLED["email"], STORE_ANYWHERE["email"]]
    assert sorted(m["to"] for m in sent.whatsapp) == ["919000000101", "919000000102", "919000000103"]
    assert report["error"] is None and report["email"]["failed"] == [] and report["whatsapp"]["failed"] == []


def test_the_store_whatsapp_is_the_approved_store_template_with_accept_and_hold(sent):
    conn = NoticeConn(D._req(), lines=[D._req_line(), D._req_line(id=61250004, item_desc_raw="CASHEW W320",
                                                                  qty=Decimal("10.000"))])
    _deliver(conn, "raised")
    msg = next(m for m in sent.whatsapp if m["to"] == "919000000101")
    assert msg["template"]["language"] == {"code": "en"}
    name, header, body, buttons = _parts(msg)
    assert name == "interunit_transfer_request_store" and header == [NO]
    assert body == [NO, "A185", "W202", "PISTA (2), CASHEW W320 (10)", "Floor W202", "12 Oct 2026, 10:30",
                    "STOCK REQUIREMENT"]
    assert buttons == [("0", f"trq:accept:{RID}"), ("1", f"trq:store_hold:{RID}")]
    # Each message sent is recorded, so a tap that only quotes it is matched.
    assert sorted(conn.recorded) == sorted(
        (f"wamid.{to}", RID, "store", uid, to)
        for to, uid in (("919000000101", 101), ("919000000102", 102), ("919000000103", 103)))


def test_the_store_mail_carries_accept_hold_and_reject_links_signed_for_its_reader(sent):
    _deliver(NoticeConn(D._req(), lines=[D._req_line()]), "raised")
    mail = sent.mails[0]
    claims = _links(mail["plain"])
    assert [(c["rid"], c["act"], c["uid"], c["em"]) for c in claims] == [
        (RID, "accept", 101, STORE_A185["email"]), (RID, "store_hold", 101, STORE_A185["email"]),
        (RID, "store_reject", 101, STORE_A185["email"])]
    assert _links(mail["html"].replace("&amp;", "&")) == claims
    assert "http://backend.test/api/v1/transfer/request-email-action?token=" in mail["plain"]
    for text in (NO, "W202 has asked A185", "PISTA - qty 2 KG, 50 kg net", "Reason: STOCK REQUIREMENT",
                 "Floor W202 (floor.w202@candorfoods.in)", "Hold and Reject open a page that asks for the reason",
                 "https://erp.test/modules/transfer/request/41250001"):
        assert text in mail["plain"], text
    assert mail["subject"] == "Transfer request REQ202610121030: A185 to W202"
    # Every mail refers to the request's thread; none claims it as its own Message-ID (a
    # request raised again after an edit would repeat it, and a mailbox drops a repeat).
    assert all(m["thread"] == "TRQ-41250001@candorfoods.in" and "message_id" not in m for m in sent.mails)


@pytest.mark.parametrize("off, reason", [({"WHATSAPP_ENABLED": False}, "whatsapp_disabled"),
                                         ({"WHATSAPP_ACCESS_TOKEN": ""}, "whatsapp_credentials_missing"),
                                         ({"SMTP_HOST": ""}, None)])
def test_a_channel_that_is_off_is_skipped_without_stopping_the_other(sent, off, reason):
    for k, v in off.items():
        setattr(sent.settings, k, v)
    report = _deliver(NoticeConn(D._req(), lines=[D._req_line()]), "raised")
    if reason:
        assert sent.whatsapp == [] and report["whatsapp"]["skipped"] == reason and len(sent.mails) == 3
    else:
        assert sent.mails == [] and report["email"]["skipped"] == "smtp_not_configured" and len(sent.whatsapp) == 3


def test_the_template_names_are_fixed_not_settings(sent):
    assert (N.TPL_STORE, N.TPL_APPROVAL, N.TPL_HELD, N.TPL_DECISION, N.TEMPLATE_LANG) == (
        "interunit_transfer_request_store", "interunit_request_approval", "interunit_transfer_request_held",
        "interunit_request_decision", "en")
    from app.config import Settings
    assert not [f for f in Settings.model_fields if "TRANSFER_REQUEST" in f]


def test_nobody_who_could_answer_for_the_site_means_nothing_is_sent(sent):
    report = _deliver(NoticeConn(D._req(from_site="Rishi"), grants={"accept": [STORE_W202]}), "raised")
    assert sent.mails == sent.whatsapp == [] and report["email"]["skipped"] == "no_recipients"


def test_a_store_notice_for_a_request_that_has_moved_on_is_not_sent(sent):
    report = _deliver(NoticeConn(D._accepted()), "raised")
    assert sent.mails == sent.whatsapp == [] and report["email"]["skipped"] == "request_moved_on"


# ── accepted -> the inventory managers ──────────────────────────────────────

def test_an_accepted_request_goes_to_the_managers_on_the_approval_template(sent):
    conn = NoticeConn(D._accepted(), lines=[D._req_line()])
    _deliver(conn, "accepted")
    assert conn.asked == ["approve"]
    # Not the raiser, not the store user who accepted it.
    assert [m["to"] for m in sent.mails] == [MANAGER["email"], MANAGER_2["email"]]
    assert [m["to"] for m in sent.whatsapp] == ["919000000201"]       # MANAGER_2 has no phone
    claims = _links(sent.mails[0]["plain"])
    assert [(c["act"], c["uid"]) for c in claims] == [("approve", 201), ("hold", 201), ("reject", 201)]
    name, header, body, buttons = _parts(sent.whatsapp[0])
    assert name == "interunit_request_approval" and header == [NO]
    assert body == [NO, "A185", "W202", "PISTA (2)", "Floor W202", "Kaushal Patil", "12 Oct 2026, 11:00"]
    assert buttons == [("0", f"trq:approve:{RID}"), ("1", f"trq:hold:{RID}"), ("2", f"trq:reject:{RID}")]
    assert [r[2] for r in conn.recorded] == ["manager"]
    assert "The store at A185 has accepted it (Kaushal Patil)" in sent.mails[0]["plain"]


def test_a_store_held_request_that_is_accepted_goes_to_the_managers_too(sent):
    row = D._accepted(store_hold_reason="counting the stock")       # held first, accepted since
    _deliver(NoticeConn(row, lines=[D._req_line()]), "accepted")
    assert [m["to"] for m in sent.mails] == [MANAGER["email"], MANAGER_2["email"]]
    assert _parts(sent.whatsapp[0])[0] == "interunit_request_approval"


@pytest.mark.parametrize("event, row", [("raised", D._req()), ("accepted", D._accepted())])
def test_a_message_to_decide_never_goes_to_the_person_who_raised_the_request(sent, event, row):
    _deliver(NoticeConn(row, grants={"accept": [RAISER_HOLDS_IT], "approve": [RAISER_HOLDS_IT]},
                        users=(MANAGER, STORE_A185)), event)
    assert sent.mails == [] and sent.whatsapp == []


def test_a_raiser_who_is_also_a_manager_gets_the_hold_notice_once_as_the_requester(sent):
    _deliver(NoticeConn(_store_held(), grants={"approve": [RAISER_HOLDS_IT]}, users=(MANAGER, STORE_A185)),
             "store_held")
    assert [m["to"] for m in sent.mails] == [RAISER]


# ── store_held -> the requester and the managers ───────────────────────────

def test_a_store_hold_tells_the_requester_and_the_managers_but_not_the_store_user(sent):
    conn = NoticeConn(_store_held(), lines=[D._req_line()])
    _deliver(conn, "store_held")
    # The requester first, then the managers; the store user who held it holds approve
    # too and is left out.
    assert [m["to"] for m in sent.mails] == [RAISER, MANAGER["email"], MANAGER_2["email"]]
    assert [m["to"] for m in sent.whatsapp] == ["919000000301", "919000000201"]
    name, header, body, buttons = _parts(sent.whatsapp[0])
    assert name == "interunit_transfer_request_held" and header == [NO] and buttons == []
    assert body == [NO, "A185", "W202", "PISTA (2)", "Floor W202", "Kaushal Patil", "12 Oct 2026, 11:15",
                    "counting the stock"]
    mail = sent.mails[0]
    assert "request-email-action" not in mail["plain"] + mail["html"]
    assert "is on hold by the store" in mail["plain"] and "Reason: counting the stock" in mail["plain"]
    assert "Please follow up with the store team" in mail["plain"]
    assert conn.recorded == []                               # nothing to tap


def test_a_hold_notice_for_a_request_the_store_has_answered_since_is_not_sent(sent):
    report = _deliver(NoticeConn(D._accepted(store_hold_reason="counting the stock")), "store_held")
    assert sent.mails == sent.whatsapp == [] and report["email"]["skipped"] == "request_moved_on"


# ── the decision template -> the requester ─────────────────────────────────

@pytest.mark.parametrize("event, row, body_tail, sentence", [
    ("approved", _decided(), ["approved", "Ravi Kumar", "12 Oct 2026, 12:00", "-"],
     "Approved by Ravi Kumar. The store at A185 can now send it."),
    ("rejected", D._req(status="Rejected", store_decision="Rejected", store_decided_by=STORE_A185["email"],
                        store_decided_at=datetime(2026, 10, 12, 11, 30), store_reject_reason="no stock of this lot"),
     ["rejected", "Kaushal Patil", "12 Oct 2026, 11:30", "no stock of this lot"],
     "Rejected by the store at A185 (Kaushal Patil). Reason: no stock of this lot"),
    ("rejected", _decided(status="Rejected", manager_decision="Rejected", manager_reject_reason="not needed"),
     ["rejected", "Ravi Kumar", "12 Oct 2026, 12:00", "not needed"],
     "Rejected by the inventory manager Ravi Kumar. Reason: not needed"),
    ("manager_held", _decided(status="On Hold", manager_decision="Held", manager_hold_reason="budget review"),
     ["put on hold", "Ravi Kumar", "12 Oct 2026, 12:00", "budget review"],
     "Put on hold by the inventory manager Ravi Kumar. Reason: budget review"),
], ids=["approved", "store-rejected", "manager-rejected", "manager-held"])
def test_the_requester_is_told_each_decision_on_the_decision_template(sent, event, row, body_tail, sentence):
    conn = NoticeConn(row, lines=[D._req_line()])
    _deliver(conn, event)
    assert conn.asked == []                                  # no permission holders read
    assert [m["to"] for m in sent.mails] == [RAISER] and [m["to"] for m in sent.whatsapp] == ["919000000301"]
    name, header, body, buttons = _parts(sent.whatsapp[0])
    assert name == "interunit_request_decision" and header == [NO] and buttons == []
    assert body == [NO, "A185", "W202", *body_tail]
    mail = sent.mails[0]
    assert "request-email-action" not in mail["plain"] + mail["html"]
    assert f"has been {body_tail[0]}" in mail["plain"] and sentence in mail["plain"]
    assert conn.recorded == []


def test_the_requester_is_told_the_challan_and_vehicle_once_scan_and_send_has_dispatched_it(sent):
    _deliver(NoticeConn(_decided(status="Transferred"), lines=[D._req_line()], transfers=[TRANSFER]), "dispatched")
    name, header, body, buttons = _parts(sent.whatsapp[0])
    assert name == "interunit_request_decision" and buttons == []
    assert body[3:] == ["scanned and sent", "Kaushal Patil", "12 Oct 2026, 13:00",
                        "Challan CH-2026-0042, vehicle MH43BP6885"]
    assert "Challan no: CH-2026-0042" in sent.mails[0]["plain"] and "has been scanned and sent" in sent.mails[0]["plain"]


def test_a_dispatch_without_a_vehicle_names_the_challan_only(sent):
    _deliver(NoticeConn(_decided(status="Transferred"), transfers=[{**TRANSFER, "vehicle_no": " "}]), "dispatched")
    assert _parts(sent.whatsapp[0])[2][-1] == "Challan CH-2026-0042"


def test_a_requester_without_an_email_address_is_not_told(sent):
    report = _deliver(NoticeConn(_decided(created_by="unknown")), "approved")
    assert sent.mails == sent.whatsapp == [] and report["email"]["skipped"] == "no_recipients"


@pytest.mark.parametrize("event, row, extra", [
    ("raised", D._req(), {}), ("accepted", D._accepted(), {}), ("store_held", _store_held(), {}),
    ("approved", _decided(), {}), ("manager_held", _decided(status="On Hold", manager_decision="Held",
                                                            manager_hold_reason="x"), {}),
    ("dispatched", _decided(status="Transferred"), {"transfers": [TRANSFER]}),
])
def test_every_message_matches_its_approved_template(sent, event, row, extra):
    _deliver(NoticeConn(row, lines=[D._req_line()], **extra), event)
    assert sent.whatsapp
    for msg in sent.whatsapp:
        name, header, body, buttons = _parts(msg)
        n_header, n_body, labels = TEMPLATES[name]
        assert (len(header), len(body), len(buttons)) == (n_header, n_body, len(labels)), name
        assert [i for i, _ in buttons] == [str(i) for i in range(len(labels))]
        # Meta refuses a blank variable or one with a new line, a tab or 4+ spaces.
        assert all(p.strip() and not re.search(r"[\n\t]| {4,}", p) for p in header + body)


def test_typed_text_is_made_safe_for_meta(sent):
    row = _store_held(store_hold_reason="line one\nline two\t\ttabbed     spaced", reason_code="   ")
    _deliver(NoticeConn(row, lines=[D._req_line(item_desc_raw="")]), "store_held")
    body = _parts(sent.whatsapp[0])[2]
    assert body[-1] == "line one line two tabbed spaced" and body[3] == "- (2)"


# ── a send never breaks anything ─────────────────────────────────────────────

def test_one_failed_send_does_not_stop_the_others(sent):
    sent.fail_mail_to.add(STORE_A185["email"])
    sent.fail_wa_to.add("919000000102")
    report = _deliver(NoticeConn(D._req(), lines=[D._req_line()]), "raised")
    assert report["email"]["failed"] == [STORE_A185["email"]] and len(report["email"]["sent"]) == 2
    assert [f["phone"] for f in report["whatsapp"]["failed"]] == ["919000000102"]
    assert sorted(report["whatsapp"]["sent"]) == ["919000000101", "919000000103"]


@pytest.mark.parametrize("pool", [_BrokenPool(), D._Pool(NoticeConn())], ids=["pool-down", "request-gone"])
def test_deliver_never_raises(sent, pool):
    report = asyncio.run(N.deliver(pool, "raised", RID))
    assert report["error"] and sent.mails == sent.whatsapp == []


def test_a_missing_sent_message_table_costs_only_the_record(sent):
    class NoTable(NoticeConn):
        async def executemany(self, sql, rows):
            raise RuntimeError('relation "transfer_request_wa_message" does not exist')

    report = _deliver(NoTable(D._req(), lines=[D._req_line()]), "raised")
    assert report["error"] is None and len(sent.whatsapp) == 3


def test_notify_without_a_bound_pool_sends_nothing_and_never_raises(monkeypatch):
    monkeypatch.setattr(N, "_pool", None)
    called = []
    monkeypatch.setattr(N, "deliver", lambda *a: called.append(a))
    N.notify("raised", RID)
    N.notify("no-such-event", RID)
    assert called == []


def test_notify_schedules_the_notice_in_the_background(monkeypatch):
    ran = []

    async def deliver(pool, event, request_id):
        await asyncio.sleep(0)
        ran.append((pool, event, request_id))

    monkeypatch.setattr(N, "_pool", "the-pool")
    monkeypatch.setattr(N, "deliver", deliver)

    async def go():
        N.notify("approved", RID)
        assert ran == []            # not awaited by the caller
        await N.drain()

    asyncio.run(go())
    assert ran == [("the-pool", "approved", RID)]


def test_a_notice_that_cannot_even_be_scheduled_does_not_fail_the_decision(monkeypatch):
    def boom(*a):
        raise RuntimeError("cannot build the notice")

    monkeypatch.setattr(N, "_pool", "the-pool")
    monkeypatch.setattr(N, "deliver", boom)
    out = asyncio.run(svc.accept_request(D.FakeConn(D._req()), RID, D.STORE))
    assert out["status"] == "Accepted"


# ── the hooks: after the commit, and only then ─────────────────────────────

@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(N, "notify", lambda event, request_id: calls.append((event, request_id)))
    return calls


HOOKS = {   # call, the row it starts from, the event, the status it leaves
    "accept": (lambda c: svc.accept_request(c, RID, D.STORE), D._req(), "accepted", "Accepted"),
    "store-reject": (lambda c: svc.store_reject_request(c, RID, D.STORE, "no"), D._req(), "rejected", "Rejected"),
    "store-hold": (lambda c: svc.store_hold_request(c, RID, D.STORE, "count"), D._req(), "store_held", "Pending"),
    "approve": (lambda c: svc.approve_request(c, RID, D.MANAGER), D._accepted(), "approved", "Approved"),
    "reject": (lambda c: svc.reject_request(c, RID, D.MANAGER, "no"), D._accepted(), "rejected", "Rejected"),
    "hold": (lambda c: svc.hold_request(c, RID, D.MANAGER, "wait"), D._accepted(), "manager_held", "On Hold"),
}


@pytest.mark.parametrize("action", HOOKS)
def test_each_decision_tells_the_next_person_once_it_has_committed(monkeypatch, action):
    call, row, event, status = HOOKS[action]
    conn = D.FakeConn(row)
    calls = []
    monkeypatch.setattr(N, "notify", lambda e, rid: calls.append((e, rid, conn.tx_depth, conn.requests[rid]["status"])))
    asyncio.run(call(conn))
    assert calls == [(event, RID, 0, status)]


@pytest.mark.parametrize("action", HOOKS)
def test_a_refused_decision_tells_nobody(notified, action):
    with pytest.raises(HTTPException):
        asyncio.run(HOOKS[action][0](D.FakeConn(D._req(status="Transferred"))))
    assert notified == []


def test_a_new_request_tells_the_store(notified):
    body = schemas.RequestCreate(
        form_data=schemas.FormDataCreate(request_date="12-10-2026", from_warehouse="A185",
                                         to_warehouse="W202", reason_description="stock requirement"),
        article_data=[schemas.ArticleDataCreate(material_type="RM", item_category="NUTS", sub_category="PISTA",
                                                item_description="PISTA", quantity="2", pack_size="25")])
    out = asyncio.run(request_service.create_request(D.FakeConn(), body, RAISER))
    assert notified == [("raised", out["id"])]


def _scan(conn):
    body = schemas.ScanBox(header=schemas.ScanHeader(vehicle_no="MH43BP6885", driver_name="Ramesh", approved_by="Yash"),
                           box=schemas.BoxCreate(box_number=1, box_id="P-1", article="PISTA",
                                                 transaction_no="TR-2026-0300"))
    return asyncio.run(scan_service.scan_box(conn, RID, body, D.STORE))


def test_the_first_scanned_box_tells_the_requester_and_later_ones_do_not(notified, monkeypatch):
    async def first_box(conn, req, h, box, user):
        conn.requests[RID]["status"] = "Transferred"
        conn.transfers.append(dict(TRANSFER, challan_no="CH-1", created_by=D.STORE.email))
        return D.TRANSFER_ID

    async def add_box(conn, transfer_id, box, key):
        return transfer_id

    async def summary(conn, transfer_id):
        return {"transfer_id": transfer_id, "challan_no": "CH-1", "status": "Dispatch", "box_count": 1}

    monkeypatch.setattr(scan_service, "_first_box", first_box)
    monkeypatch.setattr(scan_service, "_add_box", add_box)
    monkeypatch.setattr(scan_service, "_summary", summary)
    conn = NoticeConn(D._accepted(status="Approved"))
    assert _scan(conn)["created"] is True
    assert notified == [("dispatched", RID)]
    assert _scan(conn)["created"] is False
    assert notified == [("dispatched", RID)]


# ── formatting ───────────────────────────────────────────────────────────────

def test_the_items_are_listed_as_the_templates_show_them_and_counted_when_cut():
    lines = [{"item_description": f"ARTICLE {i:02d}", "quantity": "10.000"} for i in range(30)]
    text = N.items_line(lines, 60)
    assert len(text) <= 60 and text.startswith("ARTICLE 00 (10), ARTICLE 01 (10)")
    shown = text.count("ARTICLE")
    assert text.endswith(f" +{30 - shown} more")
    assert N.items_line(lines[:2], 60) == "ARTICLE 00 (10), ARTICLE 01 (10)"
    assert N.items_line([{"item_description": "LOOSE", "quantity": None}], 60) == "LOOSE"
    assert N.items_line([], 50) == "-"


def test_times_are_shown_as_the_erp_shows_them():
    assert N.when(datetime(2026, 10, 1, 11, 20)) == "01 Oct 2026, 11:20"            # stored without a zone
    assert N.when(datetime(2026, 10, 1, 5, 50, tzinfo=timezone.utc)) == "01 Oct 2026, 11:20"   # in IST
    assert N.when("2026-10-01T11:20:00") == "01 Oct 2026, 11:20"
    assert N.when(None) == "-" and N.when("") == "-"
    assert N.when(datetime(2026, 10, 1, 11, 20) + timedelta(days=1)) == "02 Oct 2026, 11:20"


def test_the_mail_escapes_what_people_typed(sent):
    row = D._req(reason_code="<script>alert(1)</script>")
    _deliver(NoticeConn(row, lines=[D._req_line(item_desc_raw="<b>PISTA</b>")]), "raised")
    html = sent.mails[0]["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;PISTA&lt;/b&gt;" in html


# ── migration 126 (static; the SQL is applied by the user, never by tests) ──
ROOT = Path(__file__).resolve().parents[2]
SQL_126 = ROOT / "app" / "db" / "126_transfer_request_notifications.sql"


def test_126_creates_the_two_tables_idempotently_in_one_transaction():
    raw = SQL_126.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "app/db SQL files are CRLF"
    body = D._body(raw.decode("utf-8"))
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1 and "ROLLBACK" not in body
    for table in ("transfer_request_wa_message", "transfer_request_wa_pending"):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in body
    assert "CREATE INDEX IF NOT EXISTS ix_transfer_request_wa_message_request" in body
    assert "CHECK (action IN ('store_reject', 'reject', 'store_hold', 'hold'))" in body
    assert "CHECK (stage IN ('store', 'manager'))" in body
    # No foreign key to the interunit_* tables, which are managed outside this repo.
    assert "REFERENCES" not in body and "DROP" not in body


def test_126_is_registered_after_125():
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i125 = text.index('DB_DIR / "125_interunit_transfer_boxes_trimmed_index.sql"')
    i126 = text.index('DB_DIR / "126_transfer_request_notifications.sql"')
    assert i125 < i126 < text.index("# ── Optional: drop the v1 legacy")
