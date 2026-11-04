"""Deciding a transfer request from its notice: the WhatsApp buttons and reject reason
(services/request_wa_reply.py), the emailed links (transfer/request_email_router.py)
and their signed tokens (services/request_action_token.py).

Every decision goes through the real request_decision_service, against
test_transfer_request_decisions' in-memory fake connection taught the user lookups
and the two WhatsApp tables; the role permission is check_permission stubbed one level
down (as the decisions tests stub it), replies are recorded, and the next notice is
recorded instead of sent. No database, Graph API or SMTP server is touched.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_request_replies.py
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.auth.services import permission_service
from app.modules.sample.services import whatsapp_service as ws
from app.modules.transfer import request_email_router as E
from app.modules.transfer.services import request_action_token as T
from app.modules.transfer.services import request_decision_service as decisions
from app.modules.transfer.services import request_notify as N
from app.modules.transfer.services import request_wa_reply as R
from tests.services import test_transfer_request_decisions as D

RID = D.RID
TEST_KEY = "transfer-request-link-test-key-0123456789"
STORE_HEAD, INV_MGR, CLERK, BOTH = 10, 20, 30, 40
GRANTS = {STORE_HEAD: {"accept"}, INV_MGR: {"approve"}, BOTH: {"accept", "approve"}}


class UndefinedTableError(Exception):
    """Named as asyncpg names it; request_wa_reply tells a missing table by the name."""


def _account(uid, email, phone, role_id, *, warehouses=(), active=True, status="active", is_admin=False):
    return {"user_id": uid, "phone": phone, "full_name": email.split("@")[0], "email": email, "entity": "cfpl",
            "role_id": role_id, "is_active": active, "status": status, "allowed_warehouses": list(warehouses),
            "allowed_floors": [], "allowed_entities": ["cfpl"], "role_name": "", "is_admin": is_admin}


STORE = _account(101, "store.a185@candorfoods.in", "+919000000101", STORE_HEAD, warehouses=["A185"])
STORE_W202 = _account(104, "store.w202@candorfoods.in", "9000000104", STORE_HEAD, warehouses=["W202"])
MANAGER = _account(201, "inventory.manager@candorfoods.in", "9000000201", INV_MGR)
RAISER_MANAGER = _account(202, D.RAISER, "9000000202", INV_MGR)        # a manager who raised the request
CLERK_USER = _account(301, "clerk@candorfoods.in", "9000000301", CLERK)
GONE = _account(401, "gone@candorfoods.in", "9000000401", STORE_HEAD, warehouses=["A185"], active=False)
# Holds both permissions, and is the account that accepted D._accepted() for the store.
ACCEPTOR = _account(205, D.STORE.email, "9000000205", BOTH)
USERS = (STORE, STORE_W202, MANAGER, RAISER_MANAGER, CLERK_USER, GONE, ACCEPTOR)
WA = {u["user_id"]: "91" + u["phone"][-10:] for u in USERS}            # as handle_inbound formats a sender


class ActorConn(D.FakeConn):
    """The decisions fake plus the user lookups and the two WhatsApp tables."""

    def __init__(self, *requests, users=USERS, pending=None, notices=(), tables=True):
        super().__init__(*requests)
        self.users = [dict(u) for u in users]
        self.pending = {k: dict(v) for k, v in (pending or {}).items()}
        self.notices = {wamid: {"request_id": rid, "stage": stage} for wamid, rid, stage in notices}
        self.tables = tables

    def _wa_table(self):
        if not self.tables:
            raise UndefinedTableError('relation "transfer_request_wa_pending" does not exist')

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM auth_user u LEFT JOIN auth_role r ON u.role_id = r.role_id" in s:
            self._log("fetchrow", sql, args)
            if "WHERE u.user_id = $1" in s:
                return next((dict(u) for u in self.users if u["user_id"] == args[0]), None)
            return next((dict(u) for key in args[0] for u in self.users if u["phone"] == key), None)
        if "FROM transfer_request_wa_pending" in s:
            self._wa_table()
            row = self.pending.get(args[0])
            return dict(row) if row else None
        if "FROM transfer_request_wa_message" in s:
            self._wa_table()
            return self.notices.get(args[0])
        return await super().fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        if "FROM auth_user_role ur" in " ".join(sql.split()):
            return []                       # the primary role only (auth_service._effective_roles)
        return await super().fetch(sql, *args)

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("DELETE FROM transfer_request_wa_pending"):
            self._wa_table()
            row = self.pending.get(args[0])
            if row and row["request_id"] == args[1]:
                del self.pending[args[0]]
                return args[1]
            return None
        return await super().fetchval(sql, *args)

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        if "transfer_request_wa_pending" not in s:
            return await super().execute(sql, *args)
        self._wa_table()
        if s.startswith("INSERT INTO transfer_request_wa_pending"):
            self.pending[args[0]] = {"request_id": args[1], "action": args[2], "user_id": args[3],
                                     "prompt_wamid": None, "created_at": datetime.now(timezone.utc)}
        elif s.startswith("UPDATE transfer_request_wa_pending SET prompt_wamid"):
            self.pending[args[0]]["prompt_wamid"] = args[1]
        elif s.startswith("DELETE FROM transfer_request_wa_pending"):
            self.pending.pop(args[0], None)
        else:
            raise AssertionError(s)


@pytest.fixture
def world(monkeypatch):
    """Role permission by role id, recorded replies (each with its own wamid), recorded
    notices, and a test signing key."""
    out = SimpleNamespace(asked=[], replies=[], notified=[])

    async def check(conn, role_ids, is_admin, module, sub_module, sub_sub_module, action, **scope):
        out.asked.append((module, sub_module, sub_sub_module, action))
        return is_admin or any(action in GRANTS.get(r, ()) for r in role_ids)

    async def send_text(to, text):
        out.replies.append((to, text))
        return {"messages": [{"id": f"wamid.reply{len(out.replies)}"}]}

    monkeypatch.setattr(permission_service, "check_permission", check)
    monkeypatch.setattr(ws, "_send_text", send_text)
    monkeypatch.setattr(N, "notify", lambda event, request_id: out.notified.append((event, request_id)))
    monkeypatch.setattr(T, "_secret", lambda settings=None: TEST_KEY)
    monkeypatch.setattr(T, "_alg", lambda settings=None: "HS256")
    out.text = lambda: out.replies[-1][1]
    return out


def _tap(conn, payload, user=STORE, *, text=None, context_id=None):
    raw = {"type": "button", "button": {"payload": payload, "text": text or "Accept"}}
    return asyncio.run(R.handle_reply(conn, WA[user["user_id"]], text or "Accept", context_id, raw))


def _say(conn, body, user=STORE, *, context_id=None, kind="text"):
    raw = {"type": kind, "text": {"body": body}} if kind == "text" else {"type": kind}
    return asyncio.run(R.handle_reply(conn, WA[user["user_id"]], body, context_id, raw))


# ── which messages are ours ─────────────────────────────────────────────────

@pytest.mark.parametrize("payload, parsed", [
    (f"trq:accept:{RID}", ("accept", RID)), (f" trq:store_hold:{RID} ", ("store_hold", RID)),
    ("trq:approve:1", ("approve", 1)), ("trq:hold:1", ("hold", 1)), ("trq:reject:12", ("reject", 12)),
    # The store's template has no Reject button, so no tap sends store_reject.
    ("trq:store_reject:1", None), ("trq:pause:1", None), ("trq:accept:", None), ("trq:accept:12x", None),
    ("floor_req:accept:1", None), ("approve_20260728053356", None), ("Accept", None), (None, None), ("", None),
])
def test_only_our_payloads_are_recognised(payload, parsed):
    assert R.parse_payload(payload) == parsed


def test_messages_that_are_not_ours_are_left_to_the_other_flows(world):
    conn = ActorConn(D._req())
    assert _tap(conn, "floor_req:accept:87654321") is None
    assert _say(conn, "hello") is None
    assert _say(conn, "approve", context_id="wamid.someone-elses-card") is None
    assert asyncio.run(R.handle_reply(conn, WA[101], "Approve", None,
                                      {"type": "interactive", "interactive": {"button_reply": {"id": "approve_1"}}})) is None
    assert world.replies == [] and conn.requests[RID]["status"] == "Pending"


def test_the_shared_webhook_hands_our_taps_to_this_flow_first(world):
    conn = ActorConn(D._req())
    res = asyncio.run(ws.handle_inbound(conn, from_phone="+91 90000 00101", text="Accept", context_id="wamid.notice",
                                        raw={"type": "button", "button": {"payload": f"trq:accept:{RID}", "text": "Accept"}}))
    assert res["flow"] == "transfer_request" and res["ok"] is True
    assert conn.requests[RID]["status"] == "Accepted"


# ── a tap makes the decision as the tapping user, with the ERP's checks ──────

def test_a_store_accept_tap_accepts_as_the_store_user_and_tells_the_managers(world):
    conn = ActorConn(D._req())
    res = _tap(conn, f"trq:accept:{RID}")
    row = conn.requests[RID]
    assert res["ok"] is True and row["status"] == "Accepted" and row["store_decided_by"] == STORE["email"]
    assert world.asked == [("transfer", "requests", None, "accept")]
    assert world.text() == ("Done. Transfer request REQ202610121030 is accepted. "
                            "It now goes to the inventory managers for approval.")
    assert world.replies[0][0] == "919000000101"
    assert world.notified == [("accepted", RID)]


def test_a_store_held_request_can_still_be_accepted_from_whatsapp(world):
    conn = ActorConn(D._req(store_decision="Held", store_decided_by=STORE["email"], store_hold_reason="count"))
    assert _tap(conn, f"trq:accept:{RID}")["ok"] is True
    row = conn.requests[RID]
    assert (row["status"], row["store_decision"], row["store_hold_reason"]) == ("Accepted", "Accepted", "count")


@pytest.mark.parametrize("status", ["Accepted", "On Hold"])
def test_a_manager_approve_tap_approves_as_the_manager(world, status):
    conn = ActorConn(D._accepted(status=status))
    res = _tap(conn, f"trq:approve:{RID}", MANAGER, text="Approve")
    assert res["ok"] is True and conn.requests[RID]["status"] == "Approved"
    assert conn.requests[RID]["manager_decided_by"] == MANAGER["email"]
    assert world.asked == [("transfer", "requests", None, "approve")] and world.notified == [("approved", RID)]
    assert "is approved" in world.text() and "A185 can now send it" in world.text()


@pytest.mark.parametrize("phone", ["919999999999", WA[401]], ids=["unknown", "inactive-account"])
def test_a_number_that_is_not_an_active_erp_user_changes_nothing(world, phone):
    conn = ActorConn(D._req())
    raw = {"type": "button", "button": {"payload": f"trq:accept:{RID}", "text": "Accept"}}
    res = asyncio.run(R.handle_reply(conn, phone, "Accept", None, raw))
    assert res["reason"] == "unknown_number" and conn.requests[RID]["status"] == "Pending"
    assert world.text() == R.NOT_REGISTERED and "REQ" not in world.text()
    assert world.notified == []


@pytest.mark.parametrize("payload, user, row, kind, words", [
    (f"trq:accept:{RID}", CLERK_USER, D._req(), "not_permitted", "not allowed to answer transfer requests for the store"),
    (f"trq:approve:{RID}", STORE, D._accepted(), "not_permitted", "not allowed to approve, hold or reject"),
    (f"trq:accept:{RID}", STORE_W202, D._req(), "forbidden",
     "Only the store of the supplying warehouse can accept or reject this request"),
    (f"trq:approve:{RID}", RAISER_MANAGER, D._accepted(), "forbidden", "You raised this request"),
    (f"trq:approve:{RID}", ACCEPTOR, D._accepted(), "forbidden", "You accepted this request for the store"),
    (f"trq:approve:{RID}", RAISER_MANAGER, D._accepted(status="On Hold"), "forbidden", "You raised this request"),
], ids=["wrong-role", "store-cannot-approve", "wrong-warehouse", "raised-it", "accepted-it", "raised-it-on-hold"])
def test_a_tap_the_erp_would_refuse_is_refused_and_changes_nothing(world, payload, user, row, kind, words):
    conn = ActorConn(row)
    before = dict(conn.requests[RID])
    res = _tap(conn, payload, user)
    assert res["ok"] is False and res["reason"] == kind
    assert conn.requests[RID] == before and world.notified == []
    assert words in world.text()


@pytest.mark.parametrize("payload, user, row, words", [
    (f"trq:accept:{RID}", STORE, D._accepted(status="Approved"), "is already approved"),
    (f"trq:accept:{RID}", STORE, D._accepted(status="Rejected"), "is already rejected"),
    (f"trq:accept:{RID}", STORE, D._accepted(status="On Hold"), "is already on hold by an inventory manager"),
    (f"trq:approve:{RID}", MANAGER, D._accepted(status="Transferred"), "is already dispatched"),
    (f"trq:approve:{RID}", MANAGER, D._req(), "is still waiting for the store's answer"),
    (f"trq:approve:{RID}", MANAGER, D._req(store_decision="Held"), "is already on hold by the store"),
])
def test_a_tap_on_a_request_that_has_moved_on_says_so_and_does_nothing(world, payload, user, row, words):
    conn = ActorConn(row)
    before = dict(conn.requests[RID])
    res = _tap(conn, payload, user)
    assert res["reason"] == "moved_on" and res["status"] == row["status"]
    assert world.text() == f"Nothing was done: transfer request REQ202610121030 {words}."
    assert conn.requests[RID] == before and world.notified == []


def test_a_tap_on_a_deleted_request_says_so(world):
    res = _tap(ActorConn(), f"trq:accept:{RID}")
    assert res["reason"] == "not_found" and "no longer exists" in world.text()


def test_a_tap_that_fails_unexpectedly_is_answered_and_never_raises(world, monkeypatch):
    async def broken(conn, request_id, user):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(decisions, "accept_request", broken)
    res = _tap(ActorConn(D._req()), f"trq:accept:{RID}")
    assert res["reason"] == "error" and "could not be recorded, so nothing was done" in world.text()


# ── hold and reject: the reason is the next text ─────────────────────────────

def test_a_store_hold_asks_for_the_reason_and_the_next_text_holds_with_it(world):
    conn = ActorConn(D._req())
    res = _tap(conn, f"trq:store_hold:{RID}", text="Hold")
    assert res == {"ok": True, "flow": "transfer_request", "request_id": RID, "action": "store_hold",
                   "awaiting": "reason"}
    assert conn.requests[RID]["store_decision"] is None and world.notified == []
    assert world.text() == ("You are putting transfer request REQ202610121030 (A185 to W202) on hold. Please type "
                            "the reason and send it within 30 minutes. Nothing changes until you do.")
    assert conn.pending[WA[101]]["prompt_wamid"] == "wamid.reply1"

    res = _say(conn, "  counting the stock first ")
    row = conn.requests[RID]
    assert res["ok"] is True and (row["status"], row["store_decision"], row["store_hold_reason"]) == (
        "Pending", "Held", "counting the stock first")
    assert row["store_decided_by"] == STORE["email"] and conn.pending == {}
    assert world.text() == ("Done. Transfer request REQ202610121030 is on hold. Reason: counting the stock first. "
                            "The requester and the inventory managers will be told.")
    assert world.notified == [("store_held", RID)]
    # The same text again is not a second reason: it is left to the other flows.
    assert _say(conn, "counting the stock first") is None


def test_a_manager_hold_asks_for_the_reason_and_puts_the_request_on_hold(world):
    conn = ActorConn(D._accepted())
    _tap(conn, f"trq:hold:{RID}", MANAGER, text="Hold")
    assert "You are putting transfer request REQ202610121030 (A185 to W202) on hold" in world.text()
    _say(conn, "budget review", MANAGER)
    row = conn.requests[RID]
    assert (row["status"], row["manager_decision"], row["manager_hold_reason"], row["manager_decided_by"]) == (
        "On Hold", "Held", "budget review", MANAGER["email"])
    assert world.text() == ("Done. Transfer request REQ202610121030 is on hold. Reason: budget review. "
                            "The requester will be told.")
    assert world.notified == [("manager_held", RID)]


@pytest.mark.parametrize("status", ["Accepted", "On Hold"])
def test_a_manager_reject_takes_the_reason_too(world, status):
    conn = ActorConn(D._accepted(status=status))
    _tap(conn, f"trq:reject:{RID}", MANAGER, text="Reject")
    assert "You are rejecting transfer request REQ202610121030" in world.text()
    _say(conn, "not needed any more", MANAGER)
    row = conn.requests[RID]
    assert (row["status"], row["manager_reject_reason"], row["manager_decided_by"]) == (
        "Rejected", "not needed any more", MANAGER["email"])
    assert "rejected by the inventory manager" in world.text()


@pytest.mark.parametrize("payload, user, row", [
    (f"trq:store_hold:{RID}", STORE_W202, D._req()),                           # wrong warehouse
    (f"trq:store_hold:{RID}", STORE, D._req(store_decision="Held")),           # already held
    (f"trq:store_hold:{RID}", STORE, D._accepted()),                           # moved on
    (f"trq:hold:{RID}", MANAGER, D._accepted(status="On Hold")),               # already on hold
    (f"trq:hold:{RID}", RAISER_MANAGER, D._accepted()),                        # own request
    (f"trq:reject:{RID}", RAISER_MANAGER, D._accepted()),                      # own request
    (f"trq:reject:{RID}", CLERK_USER, D._accepted()),                          # wrong role
])
def test_a_hold_or_reject_the_erp_would_refuse_is_refused_before_a_reason_is_asked(world, payload, user, row):
    conn = ActorConn(row)
    before = dict(conn.requests[RID])
    res = _tap(conn, payload, user, text="Hold")
    assert res["ok"] is False and conn.pending == {}
    assert "nothing was done" in world.text().lower() and "type the reason" not in world.text()
    assert conn.requests[RID] == before


def test_a_reason_sent_after_the_window_is_not_taken(world):
    late = datetime.now(timezone.utc) - timedelta(minutes=R.REASON_MINUTES + 1)
    conn = ActorConn(D._req(), pending={WA[101]: {"request_id": RID, "action": "store_hold", "user_id": 101,
                                                  "prompt_wamid": "wamid.prompt", "created_at": late}})
    assert _say(conn, "counting") is None
    assert conn.pending == {} and conn.requests[RID]["store_decision"] is None and world.replies == []


def test_a_message_without_text_asks_again_and_keeps_waiting(world):
    conn = ActorConn(D._req())
    _tap(conn, f"trq:store_hold:{RID}", text="Hold")
    res = _say(conn, "", kind="image")
    assert res["reason"] == "empty_reason" and WA[101] in conn.pending
    assert world.text() == "Please type the reason as a text message."
    _say(conn, "damaged boxes")
    assert conn.requests[RID]["store_hold_reason"] == "damaged boxes"


def test_while_waiting_a_reply_to_some_other_message_is_left_alone(world):
    conn = ActorConn(D._req())
    _tap(conn, f"trq:store_hold:{RID}", text="Hold")
    assert _say(conn, "approve", context_id="wamid.customer-return-card") is None
    assert WA[101] in conn.pending and conn.requests[RID]["store_decision"] is None


@pytest.mark.parametrize("quote", ["wamid.reply1", "wamid.notice"], ids=["the-prompt", "the-notice"])
def test_a_reason_sent_as_a_reply_to_the_prompt_or_the_notice_is_taken(world, quote):
    conn = ActorConn(D._req(), notices=[("wamid.notice", RID, "store")])
    _tap(conn, f"trq:store_hold:{RID}", text="Hold")
    _say(conn, "wrong lot requested", context_id=quote)
    assert conn.requests[RID]["store_hold_reason"] == "wrong lot requested"


def test_a_new_tap_replaces_a_hold_that_was_waiting_for_its_reason(world):
    conn = ActorConn(D._req())
    _tap(conn, f"trq:store_hold:{RID}", text="Hold")
    _tap(conn, f"trq:accept:{RID}")
    assert conn.pending == {} and conn.requests[RID]["status"] == "Accepted"
    assert _say(conn, "counting") is None


@pytest.mark.parametrize("stage, text, row, phone, key, value", [
    ("manager", "Approve", D._accepted(), WA[201], "status", "Approved"),
    ("store", "Accept", D._req(), WA[101], "status", "Accepted"),
    ("store", "Hold", D._req(), WA[101], "awaiting", "reason"),
])
def test_a_tap_that_only_quotes_a_notice_is_matched_by_the_sent_message_record(world, stage, text, row, phone, key, value):
    conn = ActorConn(row, notices=[("wamid.notice", RID, stage)])
    raw = {"type": "button", "button": {"text": text}}                 # no payload
    res = asyncio.run(R.handle_reply(conn, phone, text, "wamid.notice", raw))
    assert (res.get("awaiting") if key == "awaiting" else conn.requests[RID]["status"]) == value


@pytest.mark.parametrize("stage, buttons", [("manager", "Approve, Hold or Reject"), ("store", "Accept or Hold")])
def test_other_text_quoting_a_notice_is_not_a_decision(world, stage, buttons):
    row = D._accepted() if stage == "manager" else D._req()
    conn = ActorConn(row, notices=[("wamid.notice", RID, stage)])
    # "Reject" quoting the store's notice is not a store reject: that template has no
    # Reject button (the store rejects in the ERP or from the email).
    for text in ("ok", "Reject" if stage == "store" else "maybe"):
        res = asyncio.run(R.handle_reply(conn, WA[201 if stage == "manager" else 101], text, "wamid.notice",
                                         {"type": "text"}))
        assert res["reason"] == "unparsed"
        assert world.text() == f"Tap {buttons} on the transfer request message to answer it."
    assert conn.requests[RID]["status"] == row["status"]


def test_without_migrations_126_127_accept_works_and_hold_points_to_the_erp(world):
    conn = ActorConn(D._req(), tables=False)
    res = _tap(conn, f"trq:store_hold:{RID}", text="Hold")
    assert res["reason"] == "reason_capture_not_installed" and "Please do it in the ERP" in world.text()
    assert "/modules/transfer/request/41250001" in world.text()
    assert _say(conn, "hello") is None
    assert _tap(conn, f"trq:accept:{RID}")["ok"] is True and conn.requests[RID]["status"] == "Accepted"


# ── signed email links ───────────────────────────────────────────────────────

def _token(action="accept", user=STORE, request_id=RID):
    return T.make_token(request_id, action, user["user_id"], user["email"])


def test_a_link_token_round_trips_and_names_the_request_action_and_reader(world):
    claims = T.verify_token(_token("store_reject"))
    assert (claims["rid"], claims["act"], claims["uid"], claims["em"]) == (RID, "store_reject", 101, STORE["email"])
    assert claims["exp"] - claims["iat"] == T.TTL_DAYS * 86400


def test_a_tampered_expired_or_foreign_token_is_refused(world):
    good = _token()
    head, body, sig = good.split(".")
    assert T.verify_token(f"{head}.{body}.{sig[::-1]}") is None
    now = datetime.now(timezone.utc)
    base = {"type": T.TOKEN_TYPE, "rid": RID, "act": "accept", "uid": 101, "em": STORE["email"],
            "iat": int(now.timestamp())}
    sign = lambda **kw: pyjwt.encode({**base, "exp": int((now + timedelta(days=1)).timestamp()), **kw}, TEST_KEY,
                                     algorithm="HS256")
    assert T.verify_token(sign()) is not None
    assert T.verify_token(sign(exp=int((now - timedelta(seconds=5)).timestamp()))) is None
    assert T.verify_token(sign(type="cr_action")) is None             # a customer-return link
    assert T.verify_token(sign(act="pause")) is None
    assert all(T.verify_token(sign(act=a)) for a in ("store_hold", "hold", "store_reject", "approve"))
    assert T.verify_token(sign(rid=True)) is None and T.verify_token(sign(uid="101")) is None
    assert T.verify_token(sign(em="")) is None
    assert T.verify_token(pyjwt.encode({**base, "exp": base["iat"] + 60}, "another-key-0123456789-0123456789",
                                       algorithm="HS256")) is None
    assert T.verify_token("") is None and T.verify_token(None) is None
    with pytest.raises(ValueError):
        T.make_token(RID, "pause", 101, STORE["email"])


URL = "/api/v1/transfer/request-email-action"


@pytest.fixture
def web(world):
    def make(conn, *, with_transfer_router=False):
        app = FastAPI()
        if with_transfer_router:          # as app/main.py registers them
            app.include_router(D.R.router)
        app.include_router(E.router)
        app.state.db_pool = D._Pool(conn)
        return TestClient(app)
    return make


def test_opening_an_accept_link_asks_to_confirm_and_changes_nothing(web):
    conn = ActorConn(D._req())
    token = _token()
    res = web(conn, with_transfer_router=True).get(URL, params={"token": token})
    assert res.status_code == 200 and "Accept this transfer request?" in res.text
    assert f"name='token' value='{token}'" in res.text and "Confirm accept" in res.text
    assert f"action='{URL}'" in res.text and "<textarea" not in res.text
    assert conn.requests[RID]["status"] == "Pending" and not conn.writes()


def test_confirming_accepts_as_the_reader_and_a_second_use_only_reports_the_state(web, world):
    conn = ActorConn(D._req())
    client = web(conn)
    res = client.post(URL, data={"token": _token()})
    assert res.status_code == 200 and "Transfer request accepted" in res.text
    row = dict(conn.requests[RID])
    assert row["status"] == "Accepted" and row["store_decided_by"] == STORE["email"]
    assert world.notified == [("accepted", RID)]

    again = client.post(URL, data={"token": _token()})
    assert again.status_code == 200 and "Nothing was done" in again.text
    assert "is already accepted by the store" in again.text
    assert conn.requests[RID] == row and world.notified == [("accepted", RID)]
    # Opening it now says so too, without a form.
    page = client.get(URL, params={"token": _token()})
    assert "Nothing was done" in page.text and "<form" not in page.text


def test_a_reject_link_asks_for_the_reason_and_rejects_with_it(web, world):
    conn = ActorConn(D._req())
    client = web(conn)
    token = _token("store_reject")
    page = client.get(URL, params={"token": token})
    assert "<textarea" in page.text and "Reason for rejecting" in page.text and "Reject request" in page.text

    blank = client.post(URL, data={"token": token, "reason": "   "})
    assert "Please type the reason." in blank.text and "<textarea" in blank.text
    assert conn.requests[RID]["status"] == "Pending"

    done = client.post(URL, data={"token": token, "reason": " no stock of this lot "})
    assert "Transfer request rejected" in done.text
    assert (conn.requests[RID]["status"], conn.requests[RID]["store_reject_reason"]) == ("Rejected", "no stock of this lot")
    assert world.notified == [("rejected", RID)]


@pytest.mark.parametrize("status", ["Accepted", "On Hold"])
def test_a_manager_link_approves(web, status):
    conn = ActorConn(D._accepted(status=status))
    res = web(conn).post(URL, data={"token": _token("approve", MANAGER)})
    assert "Transfer request approved" in res.text and conn.requests[RID]["manager_decided_by"] == MANAGER["email"]


@pytest.mark.parametrize("action, user, row, column, done", [
    ("store_hold", STORE, D._req(), "store_hold_reason", ("Pending", "store_decision", "Held", "store_held")),
    ("hold", MANAGER, D._accepted(), "manager_hold_reason", ("On Hold", "manager_decision", "Held", "manager_held")),
])
def test_a_hold_link_asks_for_the_reason_and_holds_with_it(web, world, action, user, row, column, done):
    conn = ActorConn(row)
    client = web(conn)
    token = _token(action, user)
    page = client.get(URL, params={"token": token})
    assert "Put this transfer request on hold?" in page.text and "Reason for the hold" in page.text
    assert "<textarea" in page.text and "Put on hold" in page.text and not conn.writes()

    blank = client.post(URL, data={"token": token, "reason": " "})
    assert "Please type the reason." in blank.text and conn.requests[RID]["status"] == row["status"]

    res = client.post(URL, data={"token": token, "reason": " waiting for the count "})
    status, decision_column, decision, event = done
    got = conn.requests[RID]
    assert "Transfer request on hold" in res.text
    assert (got["status"], got[decision_column], got[column]) == (status, decision, "waiting for the count")
    assert world.notified == [(event, RID)]
    # A second use: the request is held already.
    again = client.post(URL, data={"token": token, "reason": "again"})
    assert "Nothing was done" in again.text and "already on hold" in again.text
    assert world.notified == [(event, RID)]


def test_a_store_held_request_can_still_be_accepted_or_rejected_from_the_mail(web):
    held = dict(store_decision="Held", store_decided_by=STORE["email"], store_hold_reason="count")
    conn = ActorConn(D._req(**held))
    assert "Transfer request accepted" in web(conn).post(URL, data={"token": _token()}).text
    conn = ActorConn(D._req(**held))
    res = web(conn).post(URL, data={"token": _token("store_reject"), "reason": "no stock"})
    assert "Transfer request rejected" in res.text
    assert (conn.requests[RID]["store_reject_reason"], conn.requests[RID]["store_hold_reason"]) == ("no stock", "count")


@pytest.mark.parametrize("action, user, row, words", [
    ("accept", STORE_W202, D._req(), "Only the store of the supplying warehouse"),
    ("approve", RAISER_MANAGER, D._accepted(), "You raised this request"),
    ("accept", CLERK_USER, D._req(), "not allowed to answer transfer requests"),
    ("approve", MANAGER, D._req(status="Rejected"), "is already rejected"),
    ("store_hold", STORE, D._req(store_decision="Held"), "is already on hold by the store"),
    ("hold", MANAGER, D._accepted(status="On Hold"), "is already on hold by an inventory manager"),
    ("hold", ACCEPTOR, D._accepted(), "You accepted this request for the store"),
])
def test_a_link_the_erp_would_refuse_does_nothing(web, action, user, row, words):
    conn = ActorConn(row)
    client = web(conn)
    before = dict(conn.requests[RID])
    page = client.get(URL, params={"token": _token(action, user)})
    assert "Nothing was done" in page.text and words in page.text and "<form" not in page.text
    res = client.post(URL, data={"token": _token(action, user), "reason": "x"})
    assert "Nothing was done" in res.text and words in res.text
    assert conn.requests[RID] == before


def test_a_bad_link_or_a_link_to_an_account_that_changed_is_refused(web, world):
    conn = ActorConn(D._req())
    client = web(conn)
    assert client.get(URL, params={"token": "not-a-token"}).status_code == 400
    assert client.post(URL, data={"token": "not-a-token"}).status_code == 400
    gone = client.post(URL, data={"token": _token("accept", GONE)})
    assert gone.status_code == 403 and "no longer active" in gone.text
    moved = T.make_token(RID, "accept", STORE["user_id"], "old.address@candorfoods.in")
    assert client.post(URL, data={"token": moved}).status_code == 403
    assert conn.requests[RID]["status"] == "Pending" and world.notified == []


def test_the_page_escapes_what_people_typed(web):
    conn = ActorConn(D._req(reason_code="<script>alert(1)</script>"))
    page = web(conn).get(URL, params={"token": _token()})
    assert "<script>alert" not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
