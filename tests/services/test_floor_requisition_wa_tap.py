"""wa_tap: a store user's Accept / Hold tap on the floor_requisition_raised_store
message. Scripted connection, replies captured; no database or Graph API."""
from __future__ import annotations

import asyncio

import pytest

from app.modules.floor_requisition.services import wa_tap as T
from app.modules.sample.services import whatsapp_service as ws

KAUSHAL = {"user_id": 8, "full_name": "Kaushal Patil", "email": "k@candorfoods.in", "phone": "+919876543210",
           "allowed_warehouses": ["W202", "A185"], "allowed_floors": None}
REQ = {"requisition_id": 87654321, "status": "raised", "material_sku_name": "California Pista",
       "warehouse": "W202", "floor": "First Floor"}


class _Conn:
    def __init__(self, *, req=REQ, users=(KAUSHAL,), installed=True, updated=True, status_after="issued", boom=None):
        self.req, self.users, self.installed = req, list(users), installed
        self.updated, self.status_after, self.boom = updated, status_after, boom
        self.writes: list[tuple] = []
        self.request_reads = 0

    async def fetchrow(self, sql, *args):
        assert "FROM floor_requisition" in sql
        self.request_reads += 1
        return self.req

    async def fetch(self, sql, *args):
        if self.boom:
            raise self.boom
        assert "FROM auth_user u" in sql
        return self.users

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if "information_schema.columns" in s:
            return self.installed
        if s.startswith("UPDATE floor_requisition"):
            self.writes.append(args)
            return args[2] if self.updated else None
        if s.startswith("SELECT status FROM floor_requisition"):
            return self.status_after
        raise AssertionError(f"unexpected fetchval: {s[:80]}")


@pytest.fixture
def replies(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send_text(to, text):
        sent.append((to, text))
        return {}

    monkeypatch.setattr(ws, "_send_text", fake_send_text)
    return sent


def _tap(conn, payload, wa="919876543210", sent_at=None):
    return asyncio.run(T.handle_store_tap(conn, wa, payload, sent_at=sent_at))


@pytest.mark.parametrize("payload, parsed", [
    ("floor_req:accept:87654321", ("accept", 87654321)),
    (" floor_req:hold:12 ", ("hold", 12)),
    ("Accept", None), ("approve_20260728053356", None), ("floor_req:issue:1", None),
    ("floor_req:accept:", None), ("floor_req:accept:12x", None), (None, None), ("", None),
])
def test_only_our_payloads_are_recognised(payload, parsed):
    assert T.parse_payload(payload) == parsed


def test_a_tap_that_is_not_ours_is_left_to_the_other_flows(replies):
    conn = _Conn(boom=AssertionError("must not touch the database"))
    assert _tap(conn, "Accept") is None and _tap(conn, None) is None
    assert replies == []


def test_accept_records_taken_up_and_confirms(replies):
    conn = _Conn()
    res = _tap(conn, "floor_req:accept:87654321")
    assert res["ok"] is True and res["store_response"] == "accepted" and res["by"] == "Kaushal Patil"
    assert conn.writes == [("accepted", "Kaushal Patil", 87654321, None)]
    assert len(replies) == 1 and "taken up by Kaushal Patil" in replies[0][1]
    assert "Stores > Production Indents" in replies[0][1]


def test_hold_records_on_hold_and_explains(replies):
    conn = _Conn()
    res = _tap(conn, "floor_req:hold:87654321")
    assert res["store_response"] == "on_hold" and conn.writes == [("on_hold", "Kaushal Patil", 87654321, None)]
    assert "is on hold" in replies[0][1] and "Tap Accept" in replies[0][1]


def test_a_number_that_is_not_a_store_user_changes_nothing(replies):
    conn = _Conn(users=[], req=None)
    res = _tap(conn, "floor_req:accept:87654321")
    assert res["ok"] is False and res["reason"] == "not_a_store_user" and conn.writes == []
    assert "not registered to a store user" in replies[0][1]
    # ...and learns nothing about which request numbers exist
    assert conn.request_reads == 0 and "87654321" not in replies[0][1]


def test_a_store_user_for_another_place_changes_nothing(replies):
    conn = _Conn(users=[{**KAUSHAL, "allowed_warehouses": ["A185"]}])
    res = _tap(conn, "floor_req:accept:87654321")
    assert res["reason"] == "place_not_allowed" and conn.writes == []
    assert "not assigned to W202 · First Floor" in replies[0][1]


def test_the_phone_must_match_after_normalising(replies):
    conn = _Conn(users=[{**KAUSHAL, "phone": "98765 43210"}])
    assert _tap(conn, "floor_req:accept:87654321", wa="919876543210")["ok"] is True
    conn = _Conn(users=[{**KAUSHAL, "phone": "9123456789"}])
    assert _tap(conn, "floor_req:accept:87654321", wa="919876543210")["reason"] == "not_a_store_user"


@pytest.mark.parametrize("status", ["issued", "received", "cancelled"])
def test_a_request_that_moved_on_is_not_changed(replies, status):
    conn = _Conn(req={**REQ, "status": status})
    res = _tap(conn, "floor_req:hold:87654321")
    assert res["ok"] is True and res["reason"] == f"already_{status}" and conn.writes == []
    assert f"already {status}" in replies[0][1]


def test_a_request_that_moves_on_during_the_tap_is_not_changed(replies):
    conn = _Conn(updated=False, status_after="issued")
    res = _tap(conn, "floor_req:accept:87654321")
    assert res["reason"] == "already_issued" and "already issued" in replies[0][1]


def test_the_taps_own_send_time_is_recorded_and_orders_replies(replies):
    conn = _Conn()
    _tap(conn, "floor_req:hold:87654321", sent_at="1789649593")
    assert conn.writes == [("on_hold", "Kaushal Patil", 87654321, 1789649593)]
    sql = " ".join(T._RECORD_SQL.split())
    assert "store_response_at = COALESCE(to_timestamp($4::bigint), now())" in sql
    assert "store_response_at < COALESCE(to_timestamp($4::bigint), now())" in sql
    assert "store_response IS DISTINCT FROM $1" in sql      # the other button in the same second still counts
    assert _tap(_Conn(), "floor_req:hold:87654321", sent_at="not-a-number")["ok"] is True


def test_a_redelivered_or_older_tap_changes_nothing_and_is_not_answered_again(replies):
    conn = _Conn(updated=False, status_after="raised")
    res = _tap(conn, "floor_req:hold:87654321", sent_at="1789649000")
    assert res["ok"] is True and res["reason"] == "stale_or_duplicate"
    assert replies == []


def test_unknown_request(replies):
    res = _tap(_Conn(req=None), "floor_req:accept:1")
    assert res["reason"] == "not_found" and "not found" in replies[0][1]


def test_before_migration_112_the_tap_is_owned_but_not_recorded(replies):
    conn = _Conn(installed=False)
    res = _tap(conn, "floor_req:accept:87654321")
    assert res["reason"] == "store_response_not_installed" and conn.writes == []
    assert "could not be recorded yet" in replies[0][1]


def test_any_failure_is_owned_and_answered_never_raised(replies):
    res = _tap(_Conn(boom=RuntimeError("db down")), "floor_req:accept:87654321")
    assert res["ok"] is False and res["reason"] == "error"
    assert "could not be recorded" in replies[0][1]


def test_a_failed_reply_does_not_undo_the_recorded_response(monkeypatch):
    async def reply_down(to, text):
        raise RuntimeError("graph down")

    monkeypatch.setattr(ws, "_send_text", reply_down)
    conn = _Conn()
    res = _tap(conn, "floor_req:accept:87654321")
    assert res["ok"] is True and conn.writes == [("accepted", "Kaushal Patil", 87654321, None)]


# ── the shared webhook claims these taps before any other flow ──────────────

def test_handle_inbound_routes_a_floor_requisition_tap_before_every_other_flow(monkeypatch, replies):
    seen = {}

    async def fake_tap(conn, wa, payload, sent_at=None):
        seen.update(wa=wa, payload=payload, sent_at=sent_at)
        return {"ok": True, "flow": "floor_requisition"}

    async def must_not_run(*a, **k):
        raise AssertionError("another WhatsApp flow ran for a floor-requisition tap")

    monkeypatch.setattr(T, "handle_store_tap", fake_tap)
    from app.modules.customer_returns.services import wa_notify
    monkeypatch.setattr(wa_notify, "handle_return_button_tap", must_not_run)
    raw = {"id": "wamid.9", "from": "919876543210", "type": "button", "timestamp": "1789649593",
           "button": {"payload": "floor_req:accept:87654321", "text": "Accept"}}
    res = asyncio.run(ws.handle_inbound(object(), from_phone="919876543210", text="Accept",
                                        context_id="wamid.sent", raw=raw))
    assert res == {"ok": True, "flow": "floor_requisition"}
    assert seen == {"wa": "919876543210", "payload": "floor_req:accept:87654321", "sent_at": "1789649593"}


def test_handle_inbound_passes_other_taps_on(monkeypatch):
    calls = []

    async def fake_tap(conn, wa, payload, sent_at=None):
        calls.append(payload)
        return None

    class _Stop(Exception):
        pass

    async def next_flow(*a, **k):
        raise _Stop()

    monkeypatch.setattr(T, "handle_store_tap", fake_tap)
    from app.modules.customer_returns.services import wa_notify
    monkeypatch.setattr(wa_notify, "handle_return_button_tap", next_flow)
    raw = {"id": "wamid.3", "from": "919876543210", "type": "button",
           "button": {"payload": "Approve", "text": "Approve"}}
    with pytest.raises(_Stop):
        asyncio.run(ws.handle_inbound(object(), from_phone="919876543210", text="Approve",
                                      context_id="wamid.x", raw=raw))
    assert calls == ["Approve"]
