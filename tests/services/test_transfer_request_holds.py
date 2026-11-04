"""Holding a transfer request (migration 127), against the in-memory fakes of the other
transfer tests.

- store hold (POST /requests/{id}/store-hold): by the supplying store, on a Pending
  request. It stays 'Pending' with store_decision 'Held' and the reason in
  store_hold_reason; the store can still accept or reject it, not hold it again (409).
- manager hold (POST /requests/{id}/hold): by an inventory manager, on an Accepted
  request, under the own-request rule. It becomes 'On Hold' with manager_decision 'Held'
  and the reason in manager_hold_reason; it can still be approved or rejected, not held
  again (409).
- a later answer overwrites the decision, its by and its at; the hold reasons stay.
- an edit of a store-held request takes the hold off and tells the store again; an
  'On Hold' request is neither edited nor deleted.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_request_holds.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.middleware.request_context import AuthError
from app.modules.transfer import schemas
from app.modules.transfer.services import delete_service, query_service, request_service
from app.modules.transfer.services import request_decision_service as svc
from app.modules.transfer.services import request_notify
from tests.services import test_transfer_request_decisions as trd
from tests.services import test_transfer_request_edit_delete as ed
from tests.services.test_transfer_request_decisions import client  # noqa: F401  (the HTTP fixture)

RID = trd.RID
STORE, MANAGER, ADMIN = trd.STORE, trd.MANAGER, trd.ADMIN
URL = f"/api/v1/transfer/requests/{RID}"


@pytest.fixture(autouse=True)
def told(monkeypatch):
    """The notices the services schedule, recorded instead of sent."""
    calls: list[tuple] = []
    monkeypatch.setattr(request_notify, "notify", lambda event, rid: calls.append((event, rid)))
    return calls


def _store_held(**over):
    return trd._req(**{"store_decision": "Held", "store_decided_by": STORE.email,
                       "store_decided_at": datetime(2026, 10, 12, 10, 45), "store_hold_reason": "counting the stock",
                       **over})


def _on_hold(**over):
    return trd._accepted(**{"status": "On Hold", "manager_decision": "Held", "manager_decided_by": MANAGER.email,
                            "manager_decided_at": datetime(2026, 10, 12, 12, 0), "manager_hold_reason": "budget review",
                            **over})


def _run(coro):
    return asyncio.run(coro)


def _refused(coro, code=409):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    assert exc.value.status_code == code
    return exc.value.detail


def _forbidden(coro):
    with pytest.raises(AuthError) as exc:
        asyncio.run(coro)
    assert exc.value.status_code == 403
    return exc.value.message


# ── the store's hold ─────────────────────────────────────────────────────────

def test_a_store_hold_keeps_the_request_pending_and_records_who_when_and_why(told):
    conn = trd.FakeConn(trd._req())
    before = datetime.now()
    out = _run(svc.store_hold_request(conn, RID, STORE, "  counting the stock first \n"))
    row = conn.requests[RID]
    assert (row["status"], row["store_decision"], row["store_decided_by"]) == ("Pending", "Held", STORE.email)
    assert row["store_hold_reason"] == "counting the stock first" and row["store_reject_reason"] is None
    assert before <= row["store_decided_at"] <= datetime.now() and row["updated_at"] == row["store_decided_at"]
    assert out["store_hold_reason"] == "counting the stock first" and out["status"] == "Pending"
    assert told == [("store_held", RID)]
    assert conn.calls[0][1].endswith("FOR UPDATE")


def test_a_held_request_cannot_be_held_again(told):
    conn = trd.FakeConn(_store_held())
    before = dict(conn.requests[RID])
    assert "already on hold by the store" in _refused(svc.store_hold_request(conn, RID, STORE, "again"))
    assert conn.requests[RID] == before and told == []


@pytest.mark.parametrize("status", ["Accepted", "On Hold", "Approved", "Rejected", "Transferred"])
def test_only_a_pending_request_is_held_by_the_store(status, told):
    conn = trd.FakeConn(trd._req(status=status))
    assert f"'{status}'" in _refused(svc.store_hold_request(conn, RID, STORE, "count"))
    assert not conn.writes() and told == []


def test_the_store_hold_takes_the_supplying_site_lock():
    conn = trd.FakeConn(trd._req(from_site="A185"))
    msg = _forbidden(svc.store_hold_request(conn, RID, trd._user("w202@candorfoods.in", warehouses=["W202"]), "x"))
    assert "Only the store of the supplying warehouse" in msg and not conn.writes()
    # Any spelling of the site, no lock at all, or an admin: allowed.
    for user in (trd._user("s@candorfoods.in", warehouses=["a-185"]), trd._user("free@candorfoods.in"), ADMIN):
        conn = trd.FakeConn(trd._req())
        assert _run(svc.store_hold_request(conn, RID, user, "x"))["store_decision"] == "Held"


@pytest.mark.parametrize("hold", [svc.store_hold_request, svc.hold_request], ids=["store", "manager"])
@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_a_hold_needs_a_reason_before_the_database_is_touched(hold, reason):
    conn = trd.FakeConn(trd._req())
    assert _refused(hold(conn, RID, ADMIN, reason), 400) == "A reason is required to put a request on hold"
    assert conn.calls == []


def test_an_unknown_request_is_404_on_hold():
    assert _refused(svc.store_hold_request(trd.FakeConn(), RID, STORE, "x"), 404) == "Request not found"
    assert _refused(svc.hold_request(trd.FakeConn(), RID, MANAGER, "x"), 404) == "Request not found"


def test_a_store_held_request_is_still_accepted_and_the_reason_stays(told):
    conn = trd.FakeConn(_store_held())
    out = _run(svc.accept_request(conn, RID, STORE))
    assert (out["status"], out["store_decision"], out["store_decided_by"]) == ("Accepted", "Accepted", STORE.email)
    assert out["store_decided_at"] > datetime(2026, 10, 12, 10, 45)
    assert out["store_hold_reason"] == "counting the stock" and told == [("accepted", RID)]


def test_a_store_held_request_is_still_rejected_and_the_reason_stays(told):
    conn = trd.FakeConn(_store_held())
    out = _run(svc.store_reject_request(conn, RID, STORE, "no stock"))
    assert (out["status"], out["store_decision"], out["store_reject_reason"], out["store_hold_reason"]) == (
        "Rejected", "Rejected", "no stock", "counting the stock")
    assert told == [("rejected", RID)]


def test_an_inventory_manager_cannot_approve_a_store_held_request():
    conn = trd.FakeConn(_store_held())
    assert "'Pending'" in _refused(svc.approve_request(conn, RID, MANAGER))


# ── the inventory manager's hold ─────────────────────────────────────────────

def test_a_manager_hold_puts_the_request_on_hold(told):
    conn = trd.FakeConn(trd._accepted())
    out = _run(svc.hold_request(conn, RID, MANAGER, " budget review "))
    row = conn.requests[RID]
    assert (row["status"], row["manager_decision"], row["manager_decided_by"]) == ("On Hold", "Held", MANAGER.email)
    assert row["manager_hold_reason"] == "budget review" and row["manager_reject_reason"] is None
    assert row["updated_at"] == row["manager_decided_at"]
    # The store's answer stays as it was.
    assert (row["store_decision"], row["store_decided_by"]) == ("Accepted", STORE.email)
    assert out["status"] == "On Hold" and told == [("manager_held", RID)]
    schemas.RequestWithLines(**out)


@pytest.mark.parametrize("status", ["Pending", "On Hold", "Approved", "Rejected", "Transferred"])
def test_only_an_accepted_request_is_held_by_a_manager(status, told):
    conn = trd.FakeConn(trd._accepted(status=status))
    detail = _refused(svc.hold_request(conn, RID, MANAGER, "wait"))
    assert detail == f"Request is '{status}'; this can only be done while it is 'Accepted'"
    assert not conn.writes() and told == []


@pytest.mark.parametrize("row", [trd._accepted(created_by=MANAGER.email.upper()),
                                 trd._accepted(store_decided_by=MANAGER.email)], ids=["raised-it", "accepted-it"])
@pytest.mark.parametrize("is_admin", [False, True], ids=["manager", "admin"])
def test_nobody_holds_a_request_they_raised_or_accepted(row, is_admin):
    conn = trd.FakeConn(row)
    _forbidden(svc.hold_request(conn, RID, trd._user(MANAGER.email, is_admin=is_admin), "wait"))
    assert conn.requests[RID]["status"] == "Accepted" and not conn.writes()


@pytest.mark.parametrize("decide, status, column, value", [
    (lambda c: svc.approve_request(c, RID, trd._user("other.manager@candorfoods.in")), "Approved", None, None),
    (lambda c: svc.reject_request(c, RID, trd._user("other.manager@candorfoods.in"), "not needed"), "Rejected",
     "manager_reject_reason", "not needed"),
], ids=["approve", "reject"])
def test_an_on_hold_request_is_still_approved_or_rejected_and_the_reason_stays(told, decide, status, column, value):
    conn = trd.FakeConn(_on_hold())
    out = _run(decide(conn))
    assert (out["status"], out["manager_decision"]) == (status, status)
    assert out["manager_decided_by"] == "other.manager@candorfoods.in"
    assert out["manager_hold_reason"] == "budget review"
    if column:
        assert out[column] == value
    assert told == [(status.lower(), RID)]


@pytest.mark.parametrize("decide", [lambda c, u: svc.approve_request(c, RID, u),
                                    lambda c, u: svc.reject_request(c, RID, u, "no")], ids=["approve", "reject"])
def test_the_own_request_rule_holds_for_an_on_hold_request(decide):
    conn = trd.FakeConn(_on_hold(created_by="raiser.manager@candorfoods.in"))
    _forbidden(decide(conn, trd._user("raiser.manager@candorfoods.in")))
    assert conn.requests[RID]["status"] == "On Hold" and not conn.writes()


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path, permission, row, status", [
    ("store-hold", "accept", trd._req(), "Pending"), ("hold", "approve", trd._accepted(), "On Hold")])
def test_each_hold_route_is_gated_on_its_permission_and_takes_a_reason(client, path, permission, row, status):
    conn = trd.FakeConn(row)
    res = client(conn, ADMIN, allowed=False).post(f"{URL}/{path}", json={"reason": "wait"})
    assert res.status_code == 200          # admins pass the gate on their flag
    assert res.json()["status"] == status and set(res.json()) == set(schemas.RequestWithLines.model_fields)

    conn = trd.FakeConn(row)
    user = STORE if permission == "accept" else MANAGER
    assert client(conn, user, allowed=False).post(f"{URL}/{path}", json={"reason": "wait"}).status_code == 403
    assert client.asked[-1] == ("transfer", "requests", None, permission) and conn.calls == []
    c = client(conn, user)
    assert c.post(f"{URL}/{path}", json={}).status_code == 422
    assert c.post(f"{URL}/{path}", json={"reason": "  "}).status_code == 400
    res = c.post(f"{URL}/{path}", json={"reason": " wait for the count "})
    assert res.status_code == 200
    column = "store_hold_reason" if permission == "accept" else "manager_hold_reason"
    assert res.json()[column] == "wait for the count"
    again = c.post(f"{URL}/{path}", json={"reason": "again"})
    assert again.status_code == 409


# ── edits and deletes of held requests ──────────────────────────────────────

def test_editing_a_store_held_request_takes_the_hold_off_and_tells_the_store_again(told):
    conn = ed.Conn(_store_held(), lines=[trd._req_line()])
    out = _run(request_service.update_request(conn, RID, ed._edit(), ed.FLOOR))   # same supplying site
    row = conn.requests[RID]
    assert (row["store_decision"], row["store_decided_by"], row["store_decided_at"]) == (None, None, None)
    assert row["store_hold_reason"] == "counting the stock" and row["status"] == "Pending"
    assert (out["store_decision"], out["store_hold_reason"]) == (None, "counting the stock")
    assert told == [("raised", RID)]


def test_editing_a_request_nobody_held_does_not_tell_the_store_again(told):
    conn = ed.Conn(trd._req(), lines=[trd._req_line()])
    _run(request_service.update_request(conn, RID, ed._edit(), ed.FLOOR))
    assert told == [] and not [c for c in conn.calls if "store_decision = NULL" in c[1]]


def test_an_on_hold_request_is_not_edited():
    conn = ed.Conn(_on_hold(), lines=[trd._req_line()])
    assert _refused(request_service.update_request(conn, RID, ed._edit(), ed.FLOOR)) == (
        "Request REQ202610121030 is On Hold; only a Pending request can be edited, before the store accepts it.")
    assert not conn.writes()


def test_a_store_held_request_is_deleted_and_an_on_hold_one_is_not():
    conn = ed.Conn(_store_held(), lines=[trd._req_line()])
    assert _run(delete_service.delete_request(conn, RID))["success"] is True and conn.requests == {}
    conn = ed.Conn(_on_hold())
    assert _refused(delete_service.delete_request(conn, RID)) == (
        "Request REQ202610121030 is On Hold; only a Pending or Rejected request can be deleted.")
    assert RID in conn.requests and not conn.writes()


# ── the read path ────────────────────────────────────────────────────────────

def test_get_and_list_return_the_hold_reasons():
    conn = trd.FakeConn(_on_hold(store_hold_reason="counted first"))
    got = _run(query_service.get_request(conn, RID))
    listed = _run(query_service.list_requests(conn, page=1, per_page=15))["records"][0]
    for out in (got, listed):
        assert (out["store_hold_reason"], out["manager_hold_reason"]) == ("counted first", "budget review")
        schemas.RequestWithLines(**out)
    assert {"store_hold_reason", "manager_hold_reason"} <= set(schemas.RequestResponse.model_fields)
    assert "store_hold_reason, manager_hold_reason" in request_service._HEADER_COLUMNS


def test_a_new_request_returns_the_hold_reasons_empty():
    body = ed._edit()
    out = _run(request_service.create_request(trd.FakeConn(), body, trd.RAISER))
    assert out["store_hold_reason"] is None and out["manager_hold_reason"] is None


# ── migration 127 (static; the SQL is applied by the user, never by tests) ──
ROOT = Path(__file__).resolve().parents[2]
SQL_127 = ROOT / "app" / "db" / "127_transfer_request_holds.sql"


def test_127_adds_the_two_columns_only_where_the_table_exists():
    raw = SQL_127.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "app/db SQL files are CRLF"
    body = trd._body(raw.decode("utf-8"))
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1 and "ROLLBACK" not in body
    guard = body.index("IF to_regclass('public.interunit_transfer_requests') IS NOT NULL THEN")
    end = body.index("END IF;")
    for column in ("store_hold_reason", "manager_hold_reason"):
        assert guard < body.index(f"ADD COLUMN IF NOT EXISTS {column} text") < end, column


def test_127_widens_the_whatsapp_reason_check_to_the_holds_where_126_ran():
    body = trd._body(SQL_127.read_bytes().decode("utf-8"))
    guard = body.index("IF to_regclass('transfer_request_wa_pending') IS NOT NULL THEN")
    assert guard < body.index("DROP CONSTRAINT IF EXISTS transfer_request_wa_pending_action_check")
    assert guard < body.index("CHECK (action IN ('store_reject', 'reject', 'store_hold', 'hold'))")


def test_127_is_registered_after_126():
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i126 = text.index('DB_DIR / "126_transfer_request_notifications.sql"')
    i127 = text.index('DB_DIR / "127_transfer_request_holds.sql"')
    assert i126 < i127 < text.index("# ── Optional: drop the v1 legacy")
