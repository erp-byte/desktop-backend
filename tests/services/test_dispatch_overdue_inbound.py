"""The overdue chase's two buttons, and the replies that answer them.

    tap "Cancel request"        -> "reply with the reason" -> cancel
    tap "Change expected date"  -> "reply with the reason" -> "reply with the date" -> move it

Both legs are two messages deep, so wa_dispatch_pending (088) carries a STAGE as well as an
action, and the redate leg has to carry the reason ACROSS a prompt. Everything below pins
that state machine, plus the two ways it can be abused: another flow's button tap being
mistaken for one of these, and somebody who is not this requisition's business head acting
on it.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_overdue_inbound.py
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.modules.sample.services import whatsapp_service as wa

REQ_PK = 4242
REQ_NO = 25495623
BH_UID = 41
BH_PHONE = "919820000009"
WAMID = "wamid.OVERDUE1"
TODAY = date(2026, 9, 4)


class _Conn:
    """wa_review_message + wa_dispatch_pending + the requisition/user lookups, in memory."""

    def __init__(self, *, wamid_kind="DISPATCH_OVERDUE", bh_uid=BH_UID, user_id=BH_UID):
        self.wamid_kind = wamid_kind
        self.bh_uid = bh_uid
        self.user_id = user_id
        self.pending: dict | None = None

    async def fetchval(self, query, *args):
        if "FROM wa_review_message" in query:
            return REQ_PK if self.wamid_kind == "DISPATCH_OVERDUE" else None
        raise AssertionError(f"unexpected fetchval: {query[:70]}")

    async def fetchrow(self, query, *args):
        if "wa_review_message" in query:
            return {"kind": self.wamid_kind, "requisition_id": REQ_PK} if self.wamid_kind else None
        if "wa_dispatch_pending" in query:
            return dict(self.pending) if self.pending else None
        if "auth_user" in query:
            return {"user_id": self.user_id, "role_name": "business_head"}
        if "wa_promote_message" in query or "wa_promote_pending" in query:
            return None
        if "sample_requisitions" in query:
            return {"id": REQ_PK, "request_id": REQ_NO, "status": "IN_PRODUCTION",
                    "business_head_user_id": self.bh_uid}
        raise AssertionError(f"unexpected fetchrow: {query[:70]}")

    async def execute(self, query, *args):
        if "INSERT INTO wa_dispatch_pending" in query:
            phone, req_id, action, stage = args
            self.pending = {"wa_phone": phone, "requisition_id": req_id,
                            "action": action, "stage": stage, "reason": None}
        elif "UPDATE wa_dispatch_pending" in query:
            stage, reason = args[0], args[1]
            self.pending = {**self.pending, "stage": stage, "reason": reason}
        elif "DELETE FROM wa_dispatch_pending" in query:
            self.pending = None
        return "OK"


@pytest.fixture
def wired(monkeypatch):
    """Capture the outbound texts and the two service calls the flow ends in."""
    out = {"texts": [], "redate": [], "cancel": [], "released": []}

    async def _text(to, body):
        out["texts"].append(body)
        return {"messages": [{"id": "wamid.reply"}]}

    async def _redate(conn, req_id, *, new_date, user, reason=None):
        out["redate"].append((req_id, new_date, reason))
        return {"id": req_id, "expected_dispatch_date": new_date}

    async def _cancel(conn, req_id, *, reason, user):
        out["cancel"].append((req_id, reason))
        return {"id": req_id, "status": "CANCELLED"}

    async def _release(conn, req_id):
        out["released"].append(req_id)

    monkeypatch.setattr(wa, "_send_text", _text)
    monkeypatch.setattr(wa, "_apply_dispatch_redate", _redate)
    monkeypatch.setattr(wa, "_apply_dispatch_cancel", _cancel)
    monkeypatch.setattr(wa, "_release_overdue_rows", _release)
    monkeypatch.setattr(wa, "_dispatch_today", lambda: TODAY)
    return out


def _run(conn, text, context_id=None):
    return asyncio.run(wa.handle_dispatch_action(conn, BH_PHONE, text, context_id))


# --- the tap ----------------------------------------------------------------

def test_change_date_tap_arms_the_reason_prompt(wired):
    conn = _Conn()
    res = _run(conn, "Change expected date", WAMID)
    assert res["awaiting"] == "redate_reason"
    assert conn.pending["action"] == "REDATE" and conn.pending["stage"] == "REASON"
    assert "reason" in wired["texts"][0].lower()


def test_cancel_tap_arms_the_reason_prompt(wired):
    conn = _Conn()
    res = _run(conn, "Cancel request", WAMID)
    assert res["awaiting"] == "cancel_reason"
    assert conn.pending["action"] == "CANCEL" and conn.pending["stage"] == "REASON"


def test_the_button_text_is_matched_case_insensitively(wired):
    conn = _Conn()
    assert _run(conn, "cancel request", WAMID)["awaiting"] == "cancel_reason"


def test_free_text_quoting_the_chase_arms_nothing(wired):
    """A question typed against the card is not a decision; arming CANCEL off it would let
    an idle reply start a cancellation."""
    conn = _Conn()
    res = _run(conn, "what is the status here?", WAMID)
    assert res["ok"] is False and conn.pending is None


def test_another_flows_button_tap_is_left_alone(wired):
    """wa_review_message is shared with the BH sign-off and NPD review cards. Resolving a
    tap without checking the kind would let this flow answer their buttons."""
    conn = _Conn(wamid_kind="BH_SIGNOFF")
    assert _run(conn, "Approve", "wamid.SOMETHINGELSE") is None
    assert wired["texts"] == []


def test_someone_who_is_not_the_business_head_is_refused(wired):
    """The chase goes to the BH's phone, but a number is not a signature - the acting user
    must be the business head bound to THIS requisition."""
    conn = _Conn(user_id=99)
    res = _run(conn, "Cancel request", WAMID)
    assert res["ok"] is False and conn.pending is None


# --- the reason -------------------------------------------------------------

def test_the_cancel_reason_cancels_the_request(wired):
    conn = _Conn()
    _run(conn, "Cancel request", WAMID)
    res = _run(conn, "Customer withdrew the enquiry")
    assert wired["cancel"] == [(REQ_PK, "Customer withdrew the enquiry")]
    assert res["ok"] is True and conn.pending is None
    assert "cancelled" in wired["texts"][-1].lower()


def test_the_redate_reason_leads_to_the_date_prompt(wired):
    conn = _Conn()
    _run(conn, "Change expected date", WAMID)
    res = _run(conn, "Raw material delayed")
    assert res["awaiting"] == "redate_date"
    assert conn.pending["stage"] == "DATE" and conn.pending["reason"] == "Raw material delayed"
    assert "dd-mm-yyyy" in wired["texts"][-1].lower()
    assert wired["redate"] == []          # nothing applied yet


def test_an_empty_reason_re_asks_instead_of_saving_a_blank(wired):
    conn = _Conn()
    _run(conn, "Cancel request", WAMID)
    res = _run(conn, "   ")
    assert res["ok"] is False
    assert conn.pending["stage"] == "REASON"     # still armed
    assert wired["cancel"] == []


# --- the date ---------------------------------------------------------------

def test_a_valid_date_moves_it_and_carries_the_reason_through(wired):
    conn = _Conn()
    _run(conn, "Change expected date", WAMID)
    _run(conn, "Raw material delayed")
    res = _run(conn, "15-09-2026")
    assert wired["redate"] == [(REQ_PK, date(2026, 9, 15), "Raw material delayed")]
    assert res["ok"] is True and conn.pending is None


def test_moving_the_date_clears_the_overdue_chase(wired):
    """Otherwise yesterday's rows silence the NEW date's warning."""
    conn = _Conn()
    _run(conn, "Change expected date", WAMID)
    _run(conn, "Raw material delayed")
    _run(conn, "15-09-2026")
    assert wired["released"] == [REQ_PK]


def test_an_unparseable_date_re_asks_and_keeps_the_reason(wired):
    """Losing the reason here would make the BH type it again for a typo in the date."""
    conn = _Conn()
    _run(conn, "Change expected date", WAMID)
    _run(conn, "Raw material delayed")
    res = _run(conn, "next friday")
    assert res["ok"] is False and wired["redate"] == []
    assert conn.pending["stage"] == "DATE" and conn.pending["reason"] == "Raw material delayed"
    assert "dd-mm-yyyy" in wired["texts"][-1].lower()


def test_a_past_date_is_refused_with_the_reason_why(wired):
    conn = _Conn()
    _run(conn, "Change expected date", WAMID)
    _run(conn, "Raw material delayed")
    res = _run(conn, "01-09-2026")
    assert res["ok"] is False and wired["redate"] == []
    assert conn.pending is not None


# --- staying out of the way -------------------------------------------------

def test_an_unrelated_message_with_nothing_armed_is_not_ours(wired):
    conn = _Conn(wamid_kind=None)
    assert _run(conn, "ACCEPT 25495623") is None
    assert wired["texts"] == []


def test_a_reply_quoting_our_own_prompt_still_answers_it(wired):
    """WhatsApp sets context.id when someone uses the reply-quote UI. Our text prompts are
    not in wa_review_message, so an unmapped quote must fall through to the armed pending
    rather than being dropped."""
    conn = _Conn()
    _run(conn, "Cancel request", WAMID)
    conn.wamid_kind = None                       # the prompt itself is not a mapped message
    res = _run(conn, "Customer withdrew", "wamid.OUR_PROMPT")
    assert res["ok"] is True and wired["cancel"] == [(REQ_PK, "Customer withdrew")]


# --- routing inside handle_inbound ------------------------------------------

def test_handle_inbound_routes_a_dispatch_tap_to_this_flow(monkeypatch):
    """Ordering matters. _req_for_wamid (the NPD review flow) does NOT filter on kind, so
    a dispatch tap that reached it first would be answered by the wrong flow entirely."""
    seen: dict = {}

    async def _dispatch(conn, phone, text, context_id):
        seen["hit"] = (phone, text, context_id)
        return {"ok": True, "awaiting": "cancel_reason"}

    async def _none(*a, **k):
        return None

    from app.modules.customer_returns.services import wa_notify as cr
    from app.modules.purchase.services import po_intimation as po
    monkeypatch.setattr(cr, "handle_return_button_tap", _none)
    monkeypatch.setattr(po, "handle_po_intimation_tap", _none)
    monkeypatch.setattr(wa, "handle_dispatch_action", _dispatch)

    res = asyncio.run(wa.handle_inbound(_Conn(), from_phone=BH_PHONE,
                                        text="Cancel request", context_id=WAMID))
    assert res == {"ok": True, "awaiting": "cancel_reason"}
    assert seen["hit"][1] == "Cancel request" and seen["hit"][2] == WAMID


def test_handle_inbound_falls_through_when_the_message_is_not_ours(monkeypatch):
    """Returning None must leave every downstream flow reachable."""
    async def _none(*a, **k):
        return None

    from app.modules.customer_returns.services import wa_notify as cr
    from app.modules.purchase.services import po_intimation as po
    monkeypatch.setattr(cr, "handle_return_button_tap", _none)
    monkeypatch.setattr(po, "handle_po_intimation_tap", _none)
    monkeypatch.setattr(wa, "handle_dispatch_action", _none)
    reached: dict = {}

    async def _bh(conn, wamid):
        reached["bh"] = True
        return None

    monkeypatch.setattr(wa, "_bh_signoff_req_for_wamid", _bh)
    asyncio.run(wa.handle_inbound(_Conn(), from_phone=BH_PHONE, text="hello",
                                  context_id=WAMID))
    assert reached.get("bh") is True


# --- an unmigrated environment ----------------------------------------------

class _NoTableConn(_Conn):
    """088 not applied yet — the pending table does not exist."""

    def __init__(self):
        super().__init__(wamid_kind=None)
        self.pending_lookups = 0

    async def fetchrow(self, query, *args):
        if "wa_dispatch_pending" in query:
            self.pending_lookups += 1
            raise RuntimeError('relation "wa_dispatch_pending" does not exist')
        return await super().fetchrow(query, *args)


def test_an_unmigrated_environment_falls_through_instead_of_raising(wired):
    """samples/ migrations are hand-applied, so a deploy can land before 088 does. Every
    inbound message reaches this handler now — raising here would break the whole webhook."""
    conn = _NoTableConn()
    assert _run(conn, "ACCEPT 25495623") is None


def test_a_missing_table_is_probed_once_not_per_message(wired, monkeypatch):
    """Without the latch every inbound WhatsApp message logs a failure until 088 is
    applied, which buries anything real in the same log."""
    monkeypatch.setattr(wa, "_DISPATCH_PENDING_READY", True)
    conn = _NoTableConn()
    for _ in range(5):
        _run(conn, "hello")
    assert conn.pending_lookups == 1
