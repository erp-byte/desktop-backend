"""Editing, deleting and raising a transfer request, and a deleted transfer freeing its
request, against the in-memory fakes of the other transfer tests.

- delete_request: only a Pending or Rejected request, its row locked first (409 for the
  rest, and for one that still has a transfer).
- delete_transfer: the request goes back from Transferred to Approved (Ready to Send),
  unless another transfer of it is left.
- update_request (PUT /requests/{id}): while Pending only, by its raiser or an admin,
  with the same To-site lock as create; the lines are replaced as a whole.
- create (POST /requests): a warehouse-locked user raises requests for their own site
  only (any spelling of it).

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_request_edit_delete.py
"""
from __future__ import annotations

import asyncio
import re
from datetime import date

import pytest
from fastapi import HTTPException

from app.core.middleware.request_context import AuthError
from app.modules.transfer import permissions, schemas
from app.modules.transfer.services import delete_service, request_service
from tests.services import test_transfer_request_decisions as trd
from tests.services import test_transfer_scan_save as ss
from tests.services.test_transfer_request_decisions import client  # noqa: F401  (the HTTP fixture)

RID, RAISER = trd.RID, trd.RAISER
FLOOR = trd._user(RAISER, warehouses=["W202"])          # raised the requests below (To W202)
OTHER = trd._user("someone.else@candorfoods.in", warehouses=["W202"])
NO_EMAIL = trd._user("", warehouses=["W202"])
URL = f"/api/v1/transfer/requests/{RID}"


class Conn(trd.FakeConn):
    """trd.FakeConn plus what delete_request and update_request send: deleting the
    request and its lines, the has-a-transfer probe, the header UPDATE ... RETURNING and
    the replacement lines (kept in `inserted`)."""

    def __init__(self, *requests, lines=(), transfers=()):
        super().__init__(*requests, lines=lines, transfers=transfers)
        self.inserted: list[tuple] = []

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        m = re.fullmatch(r"UPDATE interunit_transfer_requests SET (.+) WHERE id = \$(\d+) RETURNING (.+)", s)
        if m:
            self._log("fetchrow", sql, args)
            request_id = args[int(m[2]) - 1]
            assert request_id in self.locked, "request written without its row locked"
            row = self.requests[request_id]
            for column, value in re.findall(r"(\w+) = \$(\d+)", m[1]):
                assert column in row, f"no such column: {column}"
                row[column] = args[int(value) - 1]
            return self._named(m[3], row)
        if s.startswith("INSERT INTO interunit_transfer_request_lines"):
            self.inserted.append(args)
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if s == "SELECT 1 FROM interunit_transfers_header WHERE request_id = $1 LIMIT 1":
            self._log("fetchval", sql, args)
            return 1 if any(t["request_id"] == args[0] for t in self.transfers) else None
        return await super().fetchval(sql, *args)

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        m = re.fullmatch(r"DELETE FROM (interunit_transfer_requests|interunit_transfer_request_lines) "
                         r"WHERE (id|request_id) = \$1", s)
        if not m:
            return await super().execute(sql, *args)
        self._log("execute", sql, args)
        assert args[0] in self.locked, "request deleted without its row locked"
        if m[1] == "interunit_transfer_requests":
            self.requests.pop(args[0], None)
        else:
            self.lines = [l for l in self.lines if l["request_id"] != args[0]]


def _refused(call, code=409):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call())
    assert exc.value.status_code == code
    return exc.value.detail


def _forbidden(call):
    with pytest.raises(AuthError) as exc:
        asyncio.run(call())
    return exc.value


# ── delete_request ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["Pending", "Rejected", None])
def test_a_pending_or_rejected_request_is_deleted_with_its_lines(status):
    conn = Conn(trd._req(status=status), lines=[trd._req_line()])
    assert asyncio.run(delete_service.delete_request(conn, RID))["success"] is True
    assert conn.requests == {} and conn.lines == []
    assert conn.calls[0][1].endswith("FOR UPDATE")   # locked before anything else


@pytest.mark.parametrize("status", ["Accepted", "Approved", "Transferred"])
def test_a_request_someone_acted_on_is_not_deleted(status):
    conn = Conn(trd._req(status=status), lines=[trd._req_line()])
    assert _refused(lambda: delete_service.delete_request(conn, RID)) == (
        f"Request REQ202610121030 is {status}; only a Pending or Rejected request can be deleted.")
    assert RID in conn.requests and conn.lines and not conn.writes()


def test_a_request_that_still_has_a_transfer_is_not_deleted():
    conn = Conn(trd._req(status="Rejected"), transfers=[{"id": 1, "request_id": RID}])
    assert _refused(lambda: delete_service.delete_request(conn, RID)) == (
        "Request REQ202610121030 has a transfer; delete the transfer first.")
    assert RID in conn.requests and not conn.writes()


def test_an_unknown_request_is_404_on_delete():
    assert _refused(lambda: delete_service.delete_request(Conn(), RID), 404) == "Request not found"


# ── delete_transfer frees its request ─────────────────────────────────────────

def _request_status(db):
    (req,) = db.tables["interunit_transfer_requests"]
    return req["status"]


def test_deleting_a_request_transfer_puts_the_request_back_in_ready_to_send():
    db = ss._db()
    ss._scan(db, ss.A1)
    assert _request_status(db) == "Transferred"
    (h,) = db.tables["interunit_transfers_header"]
    out = asyncio.run(delete_service.delete_transfer(db, h["id"]))
    assert db.tables["interunit_transfers_header"] == []
    assert _request_status(db) == "Approved"
    assert out["message"].endswith(f"Request {ss.REQ} is back in Ready to Send")


def test_a_request_with_another_transfer_left_stays_transferred():
    db = ss._db()
    ss._scan(db, ss.A1)
    (h,) = db.tables["interunit_transfers_header"]
    db.tables["interunit_transfers_header"].append({**h, "id": h["id"] + 1, "challan_no": "OLDER"})
    out = asyncio.run(delete_service.delete_transfer(db, h["id"]))
    assert _request_status(db) == "Transferred" and "Ready to Send" not in out["message"]


# ── who may delete / sync / receive / re-open: admins and inventory managers ──

ADMIN_OR_MANAGER_ROUTES = [
    ("DELETE", "/api/v1/transfer/requests/{request_id}"),
    ("DELETE", "/api/v1/transfer/transfers/{transfer_id}"),
    ("DELETE", "/api/v1/transfer/transfer-in/{transfer_in_id}"),
    ("DELETE", "/api/v1/transfer/inner-transfer/{challan_no}"),
    ("POST", "/api/v1/transfer/pending-stock/backfill"),
    ("POST", "/api/v1/transfer/transfer-in"),
    ("POST", "/api/v1/transfer/transfer-in/pending"),
    ("POST", "/api/v1/transfer/transfer-in/{header_id}/acknowledge"),
    ("POST", "/api/v1/transfer/transfer-in/{header_id}/acknowledge-batch"),
    ("DELETE", "/api/v1/transfer/transfer-in/{header_id}/acknowledge/{box_id:path}"),
    ("POST", "/api/v1/transfer/transfer-in/{header_id}/finalize"),
    ("POST", "/api/v1/transfer/transfer-in/{header_id}/close-with-shortage"),
    ("POST", "/api/v1/transfer/transfer-in/{header_id}/reopen"),
    ("PUT", "/api/v1/transfer/transfer-in/{transfer_in_id}"),
]


@pytest.mark.parametrize("method, path", ADMIN_OR_MANAGER_ROUTES)
def test_each_gated_route_takes_the_admin_or_manager_permission(method, path):
    from fastapi.routing import APIRoute
    from app.modules.transfer import router as R
    (route,) = [r for r in R.router.routes
                if isinstance(r, APIRoute) and r.path == path and method in r.methods]
    assert R._ADMIN_OR_MANAGER in [d.call for d in route.dependant.dependencies]


def test_the_permission_module_names_nobody_by_email():
    from app.modules.transfer import permissions as P
    src = open(P.__file__, encoding="utf-8").read()
    assert "@candorfoods" not in src and not [n for n in dir(P) if n.endswith("_EMAILS")]


def test_a_delete_needs_the_approve_permission(client):
    conn = Conn(trd._req())
    res = client(conn, FLOOR, allowed=False).delete(URL)
    assert res.status_code == 403 and conn.calls == []
    assert client.asked[-1] == ("transfer", "requests", None, "approve")
    # An inventory manager (the permission granted) deletes a Pending request.
    res = client(Conn(trd._req()), trd.MANAGER, allowed=True).delete(URL)
    assert res.status_code == 200


# ── the maintenance relay leaves transfer-request taps alone ──────────────────

@pytest.mark.parametrize("payload, ours", [("trq:approve:41250001", True), ("TRQ:reject:7", True),
                                           ("hello", False)])
def test_a_transfer_request_tap_is_not_relayed_to_the_maintenance_bot(payload, ours):
    from app.modules.sample.services import whatsapp_service as wa
    tap = {"type": "button", "button": {"payload": payload, "text": "Approve"}}
    assert wa.is_erp_or_visitor_message(tap) is ours
    assert wa.is_maintenance_message(tap) is (not ours)


# ── update_request ────────────────────────────────────────────────────────────

def _edit(**form):
    data = {"request_date": "14-10-2026", "from_warehouse": "Warehouse A185", "to_warehouse": "W-202",
            "reason_description": "urgent", **form}
    return schemas.RequestCreate(
        form_data=schemas.FormDataCreate(**data),
        article_data=[schemas.ArticleDataCreate(material_type="RM", item_category="NUTS", sub_category="CASHEW",
                                                item_description="CASHEW W240", quantity="3", pack_size="10"),
                      schemas.ArticleDataCreate(material_type="RM", item_category="NUTS", sub_category="ALMOND",
                                                item_description="ALMOND 25KG", quantity="1", pack_size="25")])


def _update(conn, user, body=None):
    return lambda: request_service.update_request(conn, RID, body or _edit(), user)


@pytest.mark.parametrize("user", [FLOOR, trd.ADMIN, trd._user(RAISER.upper())])
def test_the_raiser_or_an_admin_edits_a_pending_request(user):
    conn = Conn(trd._req(), lines=[trd._req_line()])
    out = asyncio.run(_update(conn, user)())
    row = conn.requests[RID]
    # Sites stored by their canonical names; reason upper-cased as on create; number,
    # raiser and status unchanged.
    assert (row["from_site"], row["to_site"], row["reason_code"], row["remarks"]) == (
        "A185", "W202", "URGENT", "URGENT")
    assert row["request_date"] == date(2026, 10, 14) and row["updated_at"] is not None
    assert (row["request_no"], row["created_by"], row["status"]) == ("REQ202610121030", RAISER, "Pending")
    # The old lines are dropped and the form's inserted in their place.
    assert conn.lines == [] and [a[4] for a in conn.inserted] == ["CASHEW W240", "ALMOND 25KG"]
    assert [a[6] for a in conn.inserted] == [3, 1]
    assert out["status"] == "Pending" and out["from_warehouse"] == "A185" and len(out["lines"]) == 2
    assert conn.calls[0][1].endswith("FOR UPDATE")


@pytest.mark.parametrize("from_site, told", [("Warehouse A185", False), ("F53", True)])
def test_an_edit_that_changes_the_supplying_site_tells_its_store(monkeypatch, from_site, told):
    sent = []
    monkeypatch.setattr(request_service.request_notify, "notify", lambda event, rid: sent.append((event, rid)))
    conn = Conn(trd._req(), lines=[trd._req_line()])   # supplied from A185
    asyncio.run(_update(conn, FLOOR, _edit(from_warehouse=from_site))())
    assert sent == ([("raised", RID)] if told else [])


@pytest.mark.parametrize("status", ["Accepted", "Approved", "Rejected", "Transferred"])
def test_only_a_pending_request_is_edited(status):
    conn = Conn(trd._req(status=status), lines=[trd._req_line()])
    assert _refused(_update(conn, FLOOR)) == (
        f"Request REQ202610121030 is {status}; only a Pending request can be edited, before the store accepts it.")
    assert not conn.writes() and conn.lines


@pytest.mark.parametrize("user", [OTHER, NO_EMAIL])
def test_nobody_else_edits_a_request(user):
    conn = Conn(trd._req())
    err = _forbidden(_update(conn, user))
    assert err.status_code == 403 and "Only the person who raised this request" in err.message
    assert not conn.writes()


def test_an_edit_keeps_the_raiser_to_their_own_site():
    conn = Conn(trd._req())
    err = _forbidden(_update(conn, FLOOR, _edit(from_warehouse="W202", to_warehouse="A185")))
    assert err.status_code == 403 and err.message == (
        "A request can only be raised for your assigned warehouse (W202); this one is for A185.")
    assert not conn.writes()


def test_an_unknown_request_is_404_on_edit():
    assert _refused(_update(Conn(), FLOOR), 404) == "Request not found"


# ── who may raise a request for which site ────────────────────────────────────

@pytest.mark.parametrize("user, to_site, ok", [
    (FLOOR, "W202", True), (FLOOR, "Warehouse W-202", True), (FLOOR, "A185", False),
    (trd._user("d39@candorfoods.in", warehouses=["D-39"]), "Savla D-39", True),
    (trd.ADMIN, "A185", True), (trd._user("free@candorfoods.in"), "A185", True),
])
def test_a_locked_user_raises_requests_for_their_own_site_only(user, to_site, ok):
    if ok:
        permissions.assert_can_request_for(user, to_site)
    else:
        with pytest.raises(AuthError):
            permissions.assert_can_request_for(user, to_site)


def _body(**form):
    return _edit(**form).model_dump(mode="json")


def test_raising_a_request_for_another_site_is_403_before_the_database(client):
    conn = Conn()
    res = client(conn, FLOOR).post("/api/v1/transfer/requests", json=_body(from_warehouse="W202", to_warehouse="A185"))
    assert res.status_code == 403 and res.json()["error"] == "forbidden"
    assert conn.calls == []


def test_the_edit_route(client):
    conn = Conn(trd._req(), lines=[trd._req_line()])
    res = client(conn, FLOOR).put(URL, json=_body())
    assert res.status_code == 200 and res.json()["to_warehouse"] == "W202"
    assert set(res.json()) == set(schemas.RequestWithLines.model_fields)

    res = client(Conn(trd._req(status="Accepted")), FLOOR).put(URL, json=_body())
    assert res.status_code == 409 and "only a Pending request can be edited" in res.json()["message"]
    res = client(Conn(trd._req()), OTHER).put(URL, json=_body())
    assert res.status_code == 403
    same_site = _body()
    same_site["form_data"]["to_warehouse"] = "A-185"   # the From is 'Warehouse A185'
    res = client(Conn(trd._req()), FLOOR).put(URL, json=same_site)
    assert res.status_code == 422   # From and To are one site


def test_the_delete_route_refuses_an_approved_request(client):
    conn = Conn(trd._req(status="Approved"))
    res = client(conn, trd.ADMIN).delete(URL)
    assert res.status_code == 409 and res.json()["message"] == (
        "Request REQ202610121030 is Approved; only a Pending or Rejected request can be deleted.")
    res = client(Conn(trd._req()), trd.ADMIN).delete(URL)
    assert res.status_code == 200
