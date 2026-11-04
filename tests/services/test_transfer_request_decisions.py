"""Transfer request decisions and the dispatch guard, against an in-memory fake
connection.

The fake keeps the request rows in a dict (and the transfer headers the request
reads look up) and answers each statement by what it
reads or writes: a SELECT returns only the columns it names, an UPDATE is applied
from its SET list. So these tests pin what the services decide (status gate,
warehouse lock, own-request rule, what a decision writes, what a dispatch
refuses) and what the read path returns, without a database. The SQL itself runs
for real only where the transfer tables exist, which is outside this repo.

The HTTP half goes through the real router and the real `require_permission`
gate (the token round trip and the role lookup are stubbed one level lower, as
test_bom_router does), so the status codes and the `{}` bodies the screens send
are checked as a client sees them.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_request_decisions.py
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core.middleware import request_context
from app.core.middleware.request_context import AuthError
from app.modules.auth.middleware import AuthUser
from app.modules.transfer import router as R
from app.modules.transfer import schemas
from app.modules.transfer.services import create_service, query_service, request_service
from app.modules.transfer.services import request_decision_service as svc

RID = 41250001
TRANSFER_ID = 51250002
RAISER = "floor.w202@candorfoods.in"
STATUSES = ("Pending", "Accepted", "Approved", "Rejected", "Transferred")
DECISION_COLUMNS = {
    "store_decision": "varchar", "store_decided_by": "varchar",
    "store_decided_at": "timestamp without time zone", "store_reject_reason": "text",
    "manager_decision": "varchar", "manager_decided_by": "varchar",
    "manager_decided_at": "timestamp without time zone", "manager_reject_reason": "text",
}


# ── Fakes ────────────────────────────────────────────────────────────────────
class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.tx_depth += 1
        return self

    async def __aexit__(self, *exc):
        self.conn.tx_depth -= 1
        if not self.conn.tx_depth:
            self.conn.locked.clear()
        return False


class FakeConn:
    def __init__(self, *requests, lines=(), transfers=()):
        self.requests = {r["id"]: dict(r) for r in requests}
        self.lines = [dict(l) for l in lines]   # interunit_transfer_request_lines rows
        self.transfers = [dict(t) for t in transfers]   # interunit_transfers_header rows
        self.tx_depth = 0
        self.locked: set[int] = set()
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return _Tx(self)

    def is_in_transaction(self):
        return bool(self.tx_depth)

    def _log(self, kind, sql, args):
        s = " ".join(sql.split())
        self.calls.append((kind, s, args))
        return s

    @staticmethod
    def _named(columns: str, row: dict) -> dict:
        """Only the columns the statement names, as the database would return them.
        A column the table does not have is a KeyError."""
        return {c.strip(): row[c.strip()] for c in columns.split(",")}

    async def fetchrow(self, sql, *args):
        s = self._log("fetchrow", sql, args)
        if s.startswith("INSERT INTO interunit_transfer_requests"):
            return self._named(s.split(" RETURNING ")[1], _req(id=args[-1]))
        if s.startswith("INSERT INTO interunit_transfer_request_lines"):
            return {"id": 1, "request_id": args[0]}
        if s.startswith("INSERT INTO interunit_transfers_header"):
            return {"id": TRANSFER_ID, "challan_no": args[0]}
        if "FROM interunit_transfer_requests" in s:
            if s.endswith("FOR UPDATE"):
                assert self.tx_depth, "request row locked outside a transaction"
                self.locked.add(args[0])
            row = self.requests.get(args[0])
            return self._named(s[len("SELECT "):s.index(" FROM ")], row) if row else None
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        if "FROM interunit_transfer_request_lines" in s:
            ids = args[0] if isinstance(args[0], list) else [args[0]]
            return [self._named(s[len("SELECT "):s.index(" FROM ")], l)
                    for l in sorted(self.lines, key=lambda l: l["id"]) if l["request_id"] in ids]
        if "FROM interunit_transfer_requests" in s:
            return [self._named(s[len("SELECT "):s.index(" FROM ")], r) for r in self.requests.values()]
        if s.endswith("FROM interunit_transfers_header WHERE request_id = ANY($1::bigint[])"):
            return [self._named(s[len("SELECT "):s.index(" FROM ")], t)
                    for t in self.transfers if t["request_id"] in args[0]]
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        if s.startswith("SELECT COUNT(*) FROM interunit_transfer_requests"):
            return len(self.requests)
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    async def execute(self, sql, *args):
        s = self._log("execute", sql, args)
        m = re.fullmatch(r"UPDATE interunit_transfer_requests SET (.+) WHERE id = \$(\d+)", s)
        if not m:
            raise AssertionError(f"unexpected execute: {s[:90]}")
        request_id = args[int(m[2]) - 1]
        assert request_id in self.locked, "request written without its row locked"
        row = self.requests[request_id]
        for column, value in re.findall(r"(\w+) = (\$\d+|NULL|'[^']*')", m[1]):
            assert column in row, f"no such column: {column}"
            row[column] = (args[int(value[1:]) - 1] if value[0] == "$"
                           else None if value == "NULL" else value[1:-1])

    def writes(self):
        return [c for c in self.calls if c[0] == "execute" or c[1].startswith("INSERT")]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquired:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquired()


def _user(email, *, is_admin=False, warehouses=()):
    return AuthUser(user_id=7, phone="9000000007", full_name="", email=email, entity="cfpl",
                    role_id=3, role_name="", is_admin=is_admin, allowed_warehouses=list(warehouses))


STORE = _user("store.a185@candorfoods.in", warehouses=["A185"])
MANAGER = _user("inventory.manager@candorfoods.in")
ADMIN = _user("admin@candorfoods.in", is_admin=True)


def _req(**over):
    row = {
        "id": RID, "request_no": "REQ202610121030", "request_date": date(2026, 10, 12),
        "from_site": "A185", "to_site": "W202", "reason_code": "STOCK REQUIREMENT",
        "remarks": "STOCK REQUIREMENT", "status": "Pending", "reject_reason": None,
        "created_by": RAISER, "created_ts": datetime(2026, 10, 12, 10, 30),
        "rejected_ts": None, "updated_at": None, "reason_description": None,
        **dict.fromkeys(DECISION_COLUMNS),
        # migration 127 (test_transfer_request_holds pins the holds)
        "store_hold_reason": None, "manager_hold_reason": None,
    }
    row.update(over)
    return row


def _accepted(**over):
    return _req(**{"status": "Accepted", "store_decision": "Accepted",
                   "store_decided_by": STORE.email,
                   "store_decided_at": datetime(2026, 10, 12, 11, 0), **over})


def _req_line(**over):
    """An interunit_transfer_request_lines row of RID, numbers as the database returns them."""
    row = {
        "id": 61250003, "request_id": RID, "rm_pm_fg_type": "RM", "item_category": "NUTS",
        "sub_category": "PISTA", "item_desc_raw": "PISTA", "pack_size": Decimal("25.000"),
        "qty": Decimal("2.000"), "uom": "KG", "unit_pack_size": None, "net_weight": Decimal("50.000"),
        "total_weight": Decimal("50.000"), "lot_number": None, "created_at": None, "updated_at": None,
    }
    row.update(over)
    return row


# action -> (the call, the status it needs, a user who may make it)
ACTIONS = {
    "accept": (lambda conn, user: svc.accept_request(conn, RID, user), "Pending", STORE),
    "store-reject": (lambda conn, user: svc.store_reject_request(conn, RID, user, "no stock"), "Pending", STORE),
    "approve": (lambda conn, user: svc.approve_request(conn, RID, user), "Accepted", MANAGER),
    "reject": (lambda conn, user: svc.reject_request(conn, RID, user, "not needed"), "Accepted", MANAGER),
}


def _run(action, conn, user=None):
    call, _, default_user = ACTIONS[action]
    return asyncio.run(call(conn, user or default_user))


# ── the four transitions ───────────────────────────────────────────────────

def test_accept_moves_pending_to_accepted_and_records_the_store_user():
    conn = FakeConn(_req(store_reject_reason="left over"))
    before = datetime.now()
    out = _run("accept", conn)

    assert out["status"] == "Accepted" and out["store_decision"] == "Accepted"
    assert out["store_decided_by"] == STORE.email and out["store_reject_reason"] is None
    at = out["store_decided_at"]
    assert at.tzinfo is None and before <= at <= datetime.now()
    assert out["updated_at"] == at
    assert out["manager_decision"] is None and out["manager_decided_by"] is None
    # The legacy columns are returned and never written.
    assert out["reject_reason"] is None and out["rejected_ts"] is None


def test_store_reject_moves_pending_to_rejected_with_the_trimmed_reason():
    conn = FakeConn(_req())
    out = asyncio.run(svc.store_reject_request(conn, RID, STORE, "  no stock of this lot \n"))

    assert out["status"] == "Rejected" and out["store_decision"] == "Rejected"
    assert out["store_reject_reason"] == "no stock of this lot"
    assert out["store_decided_by"] == STORE.email and out["updated_at"] == out["store_decided_at"]
    assert out["manager_decision"] is None and out["reject_reason"] is None


def test_approve_moves_accepted_to_approved_and_records_the_manager():
    row = _accepted(manager_reject_reason="left over")
    conn = FakeConn(row)
    out = _run("approve", conn)

    assert out["status"] == "Approved" and out["manager_decision"] == "Approved"
    assert out["manager_decided_by"] == MANAGER.email and out["manager_reject_reason"] is None
    assert out["manager_decided_at"].tzinfo is None and out["updated_at"] == out["manager_decided_at"]
    # The store's answer stays as it was.
    assert (out["store_decision"], out["store_decided_by"], out["store_decided_at"]) == (
        "Accepted", STORE.email, row["store_decided_at"])


def test_reject_moves_accepted_to_rejected_with_the_trimmed_reason():
    conn = FakeConn(_accepted())
    out = asyncio.run(svc.reject_request(conn, RID, MANAGER, " not needed any more "))

    assert out["status"] == "Rejected" and out["manager_decision"] == "Rejected"
    assert out["manager_reject_reason"] == "not needed any more"
    assert out["manager_decided_by"] == MANAGER.email
    assert out["store_decision"] == "Accepted" and out["store_reject_reason"] is None


def test_a_decision_returns_what_get_request_returns():
    conn = FakeConn(_req())
    out = _run("accept", conn)
    assert out == asyncio.run(query_service.get_request(conn, RID))
    assert set(out) == set(schemas.RequestWithLines.model_fields)
    schemas.RequestWithLines(**out)


def test_a_decision_locks_the_row_before_it_reads_or_writes_anything_else():
    conn = FakeConn(_req())
    _run("accept", conn)
    kind, sql, args = conn.calls[0]
    assert kind == "fetchrow" and sql.endswith("WHERE id = $1 FOR UPDATE") and args == (RID,)
    assert len(conn.writes()) == 1


def test_a_request_with_no_status_is_pending_as_the_read_path_shows_it():
    assert _run("accept", FakeConn(_req(status=None)))["status"] == "Accepted"


# ── refusals ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action, status", [
    (a, s) for a, (_, needs, _user_) in ACTIONS.items() for s in STATUSES if s != needs])
def test_a_request_in_another_status_is_409_naming_it(action, status):
    conn = FakeConn(_req(status=status))
    before = dict(conn.requests[RID])
    with pytest.raises(HTTPException) as exc:
        _run(action, conn, ADMIN)
    assert exc.value.status_code == 409 and f"'{status}'" in exc.value.detail
    assert conn.requests[RID] == before and not conn.writes()


@pytest.mark.parametrize("action", ACTIONS)
def test_an_unknown_request_is_404(action):
    with pytest.raises(HTTPException) as exc:
        _run(action, FakeConn())
    assert exc.value.status_code == 404


@pytest.mark.parametrize("reject, row", [(svc.store_reject_request, _req()), (svc.reject_request, _accepted())],
                         ids=["store-reject", "reject"])
@pytest.mark.parametrize("reason", ["", "   ", "\n\t "])
def test_a_blank_reason_is_refused_before_the_database_is_touched(reject, row, reason):
    conn = FakeConn(row)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(reject(conn, RID, ADMIN, reason))
    assert exc.value.status_code == 400
    assert conn.calls == []


# ── the store's warehouse lock ─────────────────────────────────────────────

@pytest.mark.parametrize("action", ["accept", "store-reject"])
def test_a_store_locked_to_another_warehouse_is_403(action):
    # W202 is this request's RECEIVING site; the lock is on the supplying one.
    conn = FakeConn(_req(from_site="A185", to_site="W202"))
    with pytest.raises(AuthError) as exc:
        _run(action, conn, _user("store.w202@candorfoods.in", warehouses=["W202"]))
    assert exc.value.status_code == 403
    assert conn.requests[RID]["status"] == "Pending" and not conn.writes()


@pytest.mark.parametrize("user", [
    _user("s1@candorfoods.in", warehouses=["a-185"]),                 # A-185 == A185 == "a 185"
    _user("s2@candorfoods.in", warehouses=["W202", "A 185"]),
    _user("s3@candorfoods.in"),                                       # no warehouse lock
    _user("s4@candorfoods.in", is_admin=True, warehouses=["W202"]),   # admins bypass
], ids=["normalised", "one-of-several", "unlocked", "admin"])
def test_who_may_answer_for_the_supplying_warehouse(user):
    out = _run("accept", FakeConn(_req(from_site="A185")), user)
    assert out["status"] == "Accepted" and out["store_decided_by"] == user.email


# ── nobody decides their own request ───────────────────────────────────────

@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("row", [
    _accepted(created_by="Inventory.Manager@CandorFoods.in"),
    _accepted(store_decided_by="INVENTORY.MANAGER@candorfoods.in"),
], ids=["raised-it", "accepted-it"])
@pytest.mark.parametrize("is_admin", [False, True], ids=["manager", "admin"])
def test_nobody_decides_a_request_they_raised_or_accepted(action, row, is_admin):
    conn = FakeConn(row)
    with pytest.raises(AuthError) as exc:
        _run(action, conn, _user(MANAGER.email, is_admin=is_admin))
    assert exc.value.status_code == 403
    assert conn.requests[RID]["status"] == "Accepted" and not conn.writes()


@pytest.mark.parametrize("row", [_accepted(created_by="unknown"), _accepted(store_decided_by="unknown")],
                         ids=["raised-it", "accepted-it"])
def test_an_account_without_an_email_cannot_decide_another_such_accounts_request(row):
    # Both are recorded as 'unknown', so they cannot be told apart: the refusal
    # says that, not that this account raised or accepted the request.
    conn = FakeConn(row)
    with pytest.raises(AuthError) as exc:
        _run("approve", conn, _user(""))
    assert exc.value.status_code == 403 and not conn.writes()
    assert "no email" in exc.value.message and "You " not in exc.value.message


def test_an_account_without_an_email_decides_a_request_of_accounts_that_have_one():
    out = _run("approve", FakeConn(_accepted()), _user(""))
    assert out["status"] == "Approved" and out["manager_decided_by"] == "unknown"


# ── reads ──────────────────────────────────────────────────────────────────

def _decided():
    return _req(status="Rejected", store_decision="Accepted", store_decided_by=STORE.email,
                store_decided_at=datetime(2026, 10, 12, 11, 0), store_reject_reason=None,
                manager_decision="Rejected", manager_decided_by=MANAGER.email,
                manager_decided_at=datetime(2026, 10, 12, 12, 0), manager_reject_reason="not needed")


def test_get_and_list_return_the_decision_columns_under_their_own_names():
    row = _decided()
    conn = FakeConn(row)
    got = asyncio.run(query_service.get_request(conn, RID))
    listed = asyncio.run(query_service.list_requests(conn, page=1, per_page=15))["records"][0]
    for out in (got, listed):
        assert {c: out[c] for c in DECISION_COLUMNS} == {c: row[c] for c in DECISION_COLUMNS}
        schemas.RequestWithLines(**out)


def test_a_new_request_returns_the_decision_columns_empty():
    body = schemas.RequestCreate(
        form_data=schemas.FormDataCreate(request_date="12-10-2026", from_warehouse="A185",
                                         to_warehouse="W202", reason_description="stock requirement"),
        article_data=[schemas.ArticleDataCreate(material_type="RM", item_category="NUTS",
                                                sub_category="PISTA", item_description="PISTA",
                                                quantity="2", pack_size="25")])
    conn = FakeConn()
    out = asyncio.run(request_service.create_request(conn, body, RAISER))
    insert = next(c[1] for c in conn.calls if c[1].startswith("INSERT INTO interunit_transfer_requests"))
    assert all(c in insert.split(" RETURNING ")[1] for c in DECISION_COLUMNS)
    assert set(out) == set(schemas.RequestWithLines.model_fields)
    assert all(out[c] is None for c in DECISION_COLUMNS) and out["status"] == "Pending"


# ── dispatch guard (create_service.create_transfer) ────────────────────────

def _pista_box(box_id="P-1", article="PISTA"):
    return schemas.BoxCreate(box_number=1, box_id=box_id, article=article, lot_number="LOT-1",
                             transaction_no="TR-2026-0300", net_weight="25", gross_weight="25.2")


def _transfer(request_id, boxes=None):
    return schemas.TransferCreate(
        header=schemas.TransferHeaderCreate(stock_trf_date="12-10-2026", from_warehouse="A185",
                                            to_warehouse="W202", vehicle_no="MH43BP6885"),
        lines=[schemas.TransferLineCreate(material_type="RM", item_category="NUTS", sub_category="PISTA",
                                          item_description="PISTA", quantity="1", pack_size="25")],
        boxes=boxes, request_id=request_id)


class _Steps(list):
    """What ran after the guard, in order; `persisted` is what the lines step was given."""
    persisted: dict


async def _as_sent(conn, boxes, transfer_id=None):
    """create_service._as_recorded with every box in stock as the client describes it
    (test_transfer_box_rules pins the boxes' own records)."""
    return boxes


@pytest.fixture
def dispatched(monkeypatch):
    """create_transfer with everything after the guard stubbed (the box-location check
    and the boxes' own records included: test_transfer_box_rules pins them); lists what
    ran."""
    steps = _Steps()
    steps.persisted = {}

    async def where_boxes_are(conn, boxes_in, from_site, transfer_id=None):
        steps.append("boxes")

    async def retry(conn, insert):
        steps.append("header")
        return await insert()

    async def persist(conn, **kw):
        steps.append("lines")
        steps.persisted.update(kw)

    async def get_transfer(conn, transfer_id):
        return {"id": transfer_id}

    monkeypatch.setattr(create_service, "_refuse_unavailable_boxes", where_boxes_are)
    monkeypatch.setattr(create_service, "_as_recorded", _as_sent)
    monkeypatch.setattr(create_service, "insert_with_pk_retry", retry)
    monkeypatch.setattr(create_service, "_persist_lines_boxes_and_park", persist)
    monkeypatch.setattr(query_service, "get_transfer", get_transfer)
    return steps


def _dispatch(conn, request_id, boxes=None):
    return asyncio.run(create_service.create_transfer(conn, _transfer(request_id, boxes), STORE.email))


def test_a_dispatch_against_an_approved_request_goes_through_and_marks_it_transferred(dispatched):
    conn = FakeConn(_accepted(status="Approved"), lines=[_req_line()])
    assert _dispatch(conn, RID, [_pista_box()]) == {"id": TRANSFER_ID}

    kind, sql, args = conn.calls[0]
    assert sql == "SELECT status FROM interunit_transfer_requests WHERE id = $1 FOR UPDATE" and args == (RID,)
    assert dispatched == ["boxes", "header", "lines"]
    assert conn.requests[RID]["status"] == "Transferred"
    # What is dispatched is the request's (test_transfer_dispatch_lock pins it end to end).
    (line,) = dispatched.persisted["lines_in"]
    assert (line.item_description, line.quantity, line.pack_size) == ("PISTA", "2", "25.000")


def test_a_dispatch_against_an_approved_request_without_a_box_is_409_and_writes_nothing(dispatched):
    conn = FakeConn(_accepted(status="Approved"), lines=[_req_line()])
    with pytest.raises(HTTPException) as exc:
        _dispatch(conn, RID)
    assert exc.value.status_code == 409 and exc.value.detail == "Scan at least one box before sending."
    assert dispatched == [] and not conn.writes()
    assert conn.requests[RID]["status"] == "Approved"


@pytest.mark.parametrize("status", [s for s in STATUSES if s != "Approved"])
def test_a_dispatch_against_a_request_that_is_not_approved_is_409_and_writes_nothing(dispatched, status):
    conn = FakeConn(_req(status=status))
    with pytest.raises(HTTPException) as exc:
        _dispatch(conn, RID)
    assert exc.value.status_code == 409 and f"'{status}'" in exc.value.detail
    assert dispatched == [] and not conn.writes()
    assert conn.requests[RID]["status"] == status


def test_a_dispatch_against_an_unknown_request_is_404_and_writes_nothing(dispatched):
    conn = FakeConn()
    with pytest.raises(HTTPException) as exc:
        _dispatch(conn, RID)
    assert exc.value.status_code == 404
    assert dispatched == [] and not conn.writes()


def test_a_dispatch_without_a_request_never_looks_at_the_requests(dispatched):
    conn = FakeConn(_req())
    assert _dispatch(conn, None) == {"id": TRANSFER_ID}
    assert dispatched == ["boxes", "header", "lines"]
    assert not [c for c in conn.calls if "interunit_transfer_requests" in c[1]]
    assert conn.requests[RID]["status"] == "Pending"


# ── HTTP: the routes, their gate and their bodies ──────────────────────────
URL = f"/api/v1/transfer/requests/{RID}"


@pytest.fixture
def client(monkeypatch):
    """(conn, user, allowed) -> TestClient on the real router behind the real
    require_permission gate. `client.asked` lists the permission each call checked."""
    import app.modules.auth.services.auth_service as auth_service
    import app.modules.auth.services.permission_service as permission_service

    asked: list[tuple] = []

    def _make(conn, user=STORE, *, allowed=True):
        async def _validate(_conn, _token):
            return {"user_id": user.user_id, "phone": user.phone, "email": user.email,
                    "role_id": user.role_id, "is_admin": user.is_admin,
                    "allowed_warehouses": user.allowed_warehouses}

        async def _check(_conn, _role_ids, _is_admin, module, sub_module, sub_sub_module, action, **_scope):
            asked.append((module, sub_module, sub_sub_module, action))
            return allowed

        monkeypatch.setattr(auth_service, "validate_session", _validate)
        monkeypatch.setattr(permission_service, "check_permission", _check)
        app = FastAPI()
        request_context.install(app)
        app.include_router(R.router)
        app.state.db_pool = _Pool(conn)
        return TestClient(app, headers={"Authorization": "Bearer test-token"})

    _make.asked = asked
    return _make


def test_accept_and_approve_take_the_empty_body_the_screens_send(client):
    res = client(FakeConn(_req())).post(f"{URL}/accept", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "Accepted" and body["store_decided_by"] == STORE.email
    assert datetime.fromisoformat(body["store_decided_at"]) and body["lines"] == []
    assert set(body) == set(schemas.RequestWithLines.model_fields)
    # No body at all works too.
    assert client(FakeConn(_req())).post(f"{URL}/accept").status_code == 200

    res = client(FakeConn(_accepted()), MANAGER).post(f"{URL}/approve", json={})
    assert res.status_code == 200 and res.json()["manager_decision"] == "Approved"


@pytest.mark.parametrize("path, action", [("accept", "accept"), ("store-reject", "accept"),
                                           ("approve", "approve"), ("reject", "approve")])
def test_each_decision_is_gated_on_its_role_permission(client, path, action):
    conn = FakeConn(_req())
    res = client(conn, allowed=False).post(f"{URL}/{path}", json={"reason": "no"})
    assert res.status_code == 403 and res.json()["error"] == "forbidden"
    assert client.asked == [("transfer", "requests", None, action)]
    assert conn.calls == []


def test_a_reject_needs_a_reason_in_its_body(client):
    c = client(FakeConn(_req()))
    assert c.post(f"{URL}/store-reject", json={}).status_code == 422
    assert c.post(f"{URL}/store-reject", json={"reason": "   "}).status_code == 400
    res = c.post(f"{URL}/store-reject", json={"reason": " no stock "})
    assert res.status_code == 200
    assert (res.json()["status"], res.json()["store_reject_reason"]) == ("Rejected", "no stock")

    res = client(FakeConn(_accepted()), MANAGER).post(f"{URL}/reject", json={"reason": "not needed"})
    assert res.status_code == 200
    assert (res.json()["status"], res.json()["manager_reject_reason"]) == ("Rejected", "not needed")


def test_refusals_as_a_client_sees_them(client):
    assert client(FakeConn()).post(f"{URL}/accept", json={}).status_code == 404

    res = client(FakeConn(_req(status="Approved"))).post(f"{URL}/accept", json={})
    assert res.status_code == 409 and "'Approved'" in res.json()["message"]

    # STORE is locked to A185; this request is to be sent from W202.
    res = client(FakeConn(_req(from_site="W202", to_site="A185"))).post(f"{URL}/accept", json={})
    assert res.status_code == 403 and res.json()["error"] == "forbidden"

    raised_by_the_manager = FakeConn(_accepted(created_by=MANAGER.email))
    res = client(raised_by_the_manager, MANAGER).post(f"{URL}/approve", json={})
    assert res.status_code == 403 and res.json()["error"] == "forbidden"


def test_a_dispatch_refusal_reaches_the_scan_and_send_screen_as_a_message(client, monkeypatch):
    # Scan & send shows the response's message in its banner.
    monkeypatch.setattr(create_service, "_as_recorded", _as_sent)
    body = _transfer(RID).model_dump(mode="json")
    body["header"]["to_warehouse"] = "W203"
    conn = FakeConn(_accepted(status="Approved"), lines=[_req_line()])
    res = client(conn).post("/api/v1/transfer/transfers", json=body)
    assert res.status_code == 409
    assert res.json()["message"] == ("This request was approved for A185 -> W202; "
                                     "a dispatch from it has to use the same route.")
    assert conn.requests[RID]["status"] == "Approved" and not conn.writes()

    res = client(FakeConn(_accepted())).post("/api/v1/transfer/transfers", json=_transfer(RID).model_dump(mode="json"))
    assert res.status_code == 409
    assert res.json()["message"] == "Request is 'Accepted'; only an Approved request can be dispatched"

    # On the approved route: without a box, and with a box the request does not ask for.
    conn = FakeConn(_accepted(status="Approved"), lines=[_req_line()])
    res = client(conn).post("/api/v1/transfer/transfers", json=_transfer(RID).model_dump(mode="json"))
    assert res.status_code == 409 and res.json()["message"] == "Scan at least one box before sending."
    body = _transfer(RID, [_pista_box(), _pista_box("C-1", "CASHEW W320")]).model_dump(mode="json")
    res = client(conn).post("/api/v1/transfer/transfers", json=body)
    assert res.status_code == 409
    assert res.json()["message"] == "Box C-1 is CASHEW W320, which is not on request REQ202610121030."
    assert conn.requests[RID]["status"] == "Approved" and not conn.writes()


def test_the_hold_endpoints_are_back(client):
    # Holds returned on 2026-10-17 (migration 127); test_transfer_request_holds pins them.
    paths = {r.path for r in R.router.routes if isinstance(r, APIRoute)}
    assert {p for p in paths if p.endswith("hold")} == {
        "/api/v1/transfer/requests/{request_id}/store-hold", "/api/v1/transfer/requests/{request_id}/hold"}
    res = client(FakeConn(_req())).post(f"{URL}/store-hold", json={"reason": "count first"})
    assert res.status_code == 200 and (res.json()["status"], res.json()["store_decision"]) == ("Pending", "Held")
    assert client.asked == [("transfer", "requests", None, "accept")]


# ── migration 121 (a static check; the SQL is applied by the user, not by tests) ──
ROOT = Path(__file__).resolve().parents[2]
SQL_121 = ROOT / "app" / "db" / "121_transfer_request_decisions.sql"


def _body(sql: str) -> str:
    """The statements, without comment lines, whitespace collapsed."""
    lines = [l for l in sql.splitlines() if not l.strip().startswith("--")]
    return re.sub(r"\s+", " ", "\n".join(lines))


def test_121_adds_the_eight_columns_only_where_the_table_exists():
    body = _body(SQL_121.read_text(encoding="utf-8"))
    guard = body.index("IF to_regclass('public.interunit_transfer_requests') IS NOT NULL THEN")
    end = body.index("END IF;")
    for column, typ in DECISION_COLUMNS.items():
        assert guard < body.index(f"ADD COLUMN IF NOT EXISTS {column} {typ}") < end, column
    # Nothing outside the guard names the table.
    assert all(guard <= m.start() < end for m in re.finditer("interunit_transfer_requests", body))


def test_121_creates_the_two_permissions_null_safely_and_grants_them():
    body = _body(SQL_121.read_text(encoding="utf-8"))
    assert "('transfer', 'requests', 'accept'," in body and "('transfer', 'requests', 'approve'," in body
    assert "IS NOT DISTINCT FROM v.sub_module" in body and "p.sub_sub_module IS NULL" in body
    assert set(re.findall(r"\('(\w+)', '(\w+)'\)", body)) == {
        ("store_head", "accept"), ("inventory_manager", "approve"),
        ("inventory_manager_substitute", "approve")}
    assert "ON CONFLICT DO NOTHING" in body


def test_121_is_one_transaction_registered_after_117():
    raw = SQL_121.read_bytes()
    body = _body(raw.decode("utf-8"))
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1 and "ROLLBACK" not in body
    assert raw.count(b"\r\n") == raw.count(b"\n"), "app/db SQL files are CRLF"
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i117 = text.index('DB_DIR / "117_store_head_role.sql"')
    i121 = text.index('DB_DIR / "121_transfer_request_decisions.sql"')
    assert i117 < i121 < text.index("# ── Optional: drop the v1 legacy")
