"""Daily floor checks (migration 142): a floor reports "checked, no movement",
an inventory manager approves or rejects it, on the screen or from WhatsApp.

The SQL itself was run against a copy of the live schema in a rolled-back
transaction (scratchpad floorcheck_dryrun.py, 23 Nov 2026); these tests cover the
rules that do not need a database.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.modules.stock_take import router as R
from app.modules.stock_take.services import floor_check_notify as N
from app.modules.stock_take.services import floor_check_service as S


class FakeUser:
    def __init__(self, warehouses, floors, is_admin=False, user_id=7):
        self.allowed_warehouses = warehouses
        self.allowed_floors = floors
        self.is_admin = is_admin
        self.user_id = user_id
        self.role_ids = [1]
        self.entity = None
        self.full_name = "Digamber Sawant"
        self.email = "d@example.com"


PLACES = {"W202": ["Second Floor", "Terrace", "STORE"],
          "A185": ["Roasting Area", "Sorting Area", "Dmart Packing Area"]}


# ── the day's state ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("entries, check, state", [
    (3, None, "entries"),
    (3, {"status": "pending"}, "entries"),        # posting stock is a stock take in itself
    (0, None, "none"),
    (0, {"status": "pending"}, "pending"),
    (0, {"status": "approved"}, "approved"),
    (0, {"status": "rejected"}, "rejected"),
])
def test_floor_state(entries, check, state):
    assert S.floor_state(entries, check) == state


def test_check_dict_names_the_floor_as_shown():
    d = S.check_dict({"check_id": 1, "warehouse": "A185", "floor_name": "Dmart Packing Area",
                      "check_day": datetime(2026, 11, 23).date(), "checked_at": None, "decided_at": None})
    assert d["floor_label"] == "Packing Area" and d["check_day"] == "2026-11-23"


# ── which floors the day's list shows ───────────────────────────────────────

def test_a_restricted_user_sees_only_their_floors_and_never_the_merged_ones():
    user = FakeUser(["A185"], ["Roasting Area", "Sorting Area", "Dmart Packing Area"])
    assert R._check_floors(user, PLACES, None) == {"A185": ["Roasting Area", "Dmart Packing Area"]}


def test_an_unrestricted_user_sees_every_declared_floor_in_walking_order():
    out = R._check_floors(FakeUser([], []), PLACES, None)
    assert list(out) == ["W202", "A185"]
    assert out["W202"][0] == "Lower Basement" and "Store" in out["W202"]
    assert not {"Sorting Area", "FFS Packing Area", "Dmart Production Area"} & set(out["A185"])


def test_a_warehouse_filter_outside_your_scope_is_refused():
    with pytest.raises(HTTPException) as e:
        R._check_floors(FakeUser(["A185"], []), PLACES, "W-202")
    assert e.value.status_code == 403
    assert list(R._check_floors(FakeUser(["A185", "W202"], []), PLACES, "w-202")) == ["W202"]


# ── the WhatsApp notice ──────────────────────────────────────────────────────

CHECK = {"check_id": 41, "check_day": "2026-11-23", "warehouse": "W202", "floor_name": "Second Floor",
         "floor_label": "Second Floor", "checked_by": "Abhishek Rane",
         "checked_at": "2026-11-23T04:45:00+00:00", "note": "All racks\n checked   twice",
         "checked_by_user_id": 12, "status": "pending"}
LAST = {"n_day": 0, "last_at": datetime(2026, 11, 17, 13, 9, tzinfo=timezone.utc), "last_by": "Abhishek Rane"}


def test_the_seven_template_variables_in_order():
    assert N.body_params(CHECK, LAST) == [
        "Second Floor", "W202", "Mon 23 Nov 2026", "Abhishek Rane", "10:15",
        "17 Nov 2026, 18:39 by Abhishek Rane", "All racks checked twice"]


def test_empty_values_are_never_sent_blank():
    params = N.body_params({**CHECK, "note": None}, None)
    assert params[5] == "none recorded" and params[6] == "-"
    assert all(p.strip() for p in params)


def test_the_message_carries_both_buttons_with_the_check_id():
    msg = N.template_message("919800000000", CHECK, LAST)
    t = msg["template"]
    assert t["name"] == "no_mov_inv_approval" and t["language"] == {"code": "en"}
    assert len(t["components"][0]["parameters"]) == 7
    assert [(b["index"], b["parameters"][0]["payload"]) for b in t["components"][1:]] == [
        ("0", "stfc:approve:41"), ("1", "stfc:reject:41")]


@pytest.mark.parametrize("payload, parsed", [
    ("stfc:approve:41", ("approve", 41)),
    (" stfc:reject:9 ", ("reject", 9)),
    ("stfc:hold:41", None),
    ("stfc:approve:x", None),
    ("trq:approve:41", None),          # a transfer request's button
    ("floor_req:accept:41", None),     # a floor requisition's button
    (None, None),
])
def test_only_our_buttons_are_claimed(payload, parsed):
    assert N.parse_payload(payload) == parsed


def test_approvers_cover_the_warehouse_and_never_include_the_reporter():
    holders = [
        {"user_id": 1, "name": "All sites", "phone": "1", "allowed_warehouses": [], "allowed_floors": []},
        {"user_id": 2, "name": "W-202 only", "phone": "2", "allowed_warehouses": ["W-202"], "allowed_floors": ["Terrace"]},
        {"user_id": 3, "name": "A185 only", "phone": "3", "allowed_warehouses": ["A185"], "allowed_floors": []},
        {"user_id": 12, "name": "The reporter", "phone": "4", "allowed_warehouses": [], "allowed_floors": []},
    ]
    names = [h["name"] for h in N.approvers_for(holders, "W202", exclude_user_id=12)]
    assert names == ["All sites", "W-202 only"], "floors are not matched, only the warehouse"


# ── the tap ──────────────────────────────────────────────────────────────────

class FakeConn:
    @asynccontextmanager
    async def transaction(self):
        yield


@pytest.fixture
def replies(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_reply(wa, text):
        sent.append((wa, text))
    monkeypatch.setattr(N, "_reply", fake_reply)
    return sent


@pytest.mark.asyncio
async def test_a_tap_that_is_not_ours_is_left_to_the_other_flows(replies):
    assert await N.handle_tap(FakeConn(), "919800000000", "trq:approve:41") is None
    assert await N.handle_tap(FakeConn(), "919800000000", None) is None
    assert replies == []


@pytest.mark.asyncio
async def test_an_unknown_number_is_told_and_nothing_is_done(monkeypatch, replies):
    from app.modules.transfer.services import request_action

    async def no_user(conn, **kw):
        return None
    monkeypatch.setattr(request_action, "load_user", no_user)
    res = await N.handle_tap(FakeConn(), "919800000000", "stfc:approve:41")
    assert res["reason"] == "unknown_number" and replies == [("919800000000", N.NOT_REGISTERED)]


def _allow(monkeypatch, *, permitted=True, user=None):
    from app.modules.auth.services import permission_service
    from app.modules.transfer.services import request_action

    async def load_user(conn, **kw):
        return user or FakeUser(["W202", "A185"], [])

    async def check_permission(*a, **kw):
        return permitted

    async def get_check(conn, check_id, for_update=False):
        return dict(CHECK)
    monkeypatch.setattr(request_action, "load_user", load_user)
    monkeypatch.setattr(permission_service, "check_permission", check_permission)
    monkeypatch.setattr(S, "get_check", get_check)


@pytest.mark.asyncio
async def test_approve_from_whatsapp_decides_as_the_tapping_user(monkeypatch, replies):
    _allow(monkeypatch)
    seen = {}

    async def decide(conn, **kw):
        seen.update(kw)
        return {**CHECK, "status": "approved"}
    monkeypatch.setattr(S, "decide", decide)
    res = await N.handle_tap(FakeConn(), "919800000000", "stfc:approve:41")
    assert res["ok"] and res["status"] == "approved"
    assert seen["approve"] is True and seen["via"] == "whatsapp" and seen["user_id"] == 7
    assert replies[0][1].startswith("Approved: Second Floor (W202)")


@pytest.mark.asyncio
async def test_reject_from_whatsapp_records_where_it_came_from(monkeypatch, replies):
    _allow(monkeypatch)
    seen = {}

    async def decide(conn, **kw):
        seen.update(kw)
        return {**CHECK, "status": "rejected"}
    monkeypatch.setattr(S, "decide", decide)
    await N.handle_tap(FakeConn(), "919800000000", "stfc:reject:41")
    assert seen["approve"] is False and seen["reason"] == N.WA_REJECT_REASON
    assert replies[0][1].startswith("Rejected: Second Floor (W202)")


@pytest.mark.asyncio
async def test_without_the_permission_nothing_is_decided(monkeypatch, replies):
    _allow(monkeypatch, permitted=False)

    async def decide(conn, **kw):
        raise AssertionError("must not decide")
    monkeypatch.setattr(S, "decide", decide)
    res = await N.handle_tap(FakeConn(), "919800000000", "stfc:approve:41")
    assert res["reason"] == "not_permitted" and "not allowed" in replies[0][1]


@pytest.mark.asyncio
async def test_a_manager_for_another_warehouse_cannot_decide(monkeypatch, replies):
    _allow(monkeypatch, user=FakeUser(["A185"], []))
    res = await N.handle_tap(FakeConn(), "919800000000", "stfc:approve:41")
    assert res["reason"] == "warehouse_not_allowed"


@pytest.mark.asyncio
async def test_a_refused_decision_is_explained(monkeypatch, replies):
    _allow(monkeypatch)

    async def decide(conn, **kw):
        raise S.FloorCheckError("already_decided", "This check was already approved by X on 23 Nov, 10:20.")
    monkeypatch.setattr(S, "decide", decide)
    res = await N.handle_tap(FakeConn(), "919800000000", "stfc:reject:41")
    assert res["reason"] == "already_decided" and "already approved" in replies[0][1]


@pytest.mark.asyncio
async def test_an_error_is_answered_never_raised(monkeypatch, replies):
    _allow(monkeypatch)

    async def decide(conn, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(S, "decide", decide)
    res = await N.handle_tap(FakeConn(), "919800000000", "stfc:approve:41")
    assert res["reason"] == "error" and "could not be recorded" in replies[0][1]
