"""Route-level tests for the purchase-indent lifecycle endpoints and for the
server-side maker/checker identity fix on the production-indent endpoints.

Two things are covered here that nothing else covers:

1. `indent_manager.edit_indent / send_indent / acknowledge_indent /
   link_indent_to_po` existed for months with no route reaching them. These
   tests pin the four new PUT /indents/* endpoints: happy path, the
   wrong-state result coming back as HTTP 200 (a stale client view is not a
   malformed request), 404 for a missing row, and the permission gate.

2. THE SECURITY FIX. `maker_user`, `checker_user` and `acknowledged_by` used
   to be client-supplied strings, so any authenticated user could post
   anybody's name as both maker AND checker. The three
   `*_ignores_the_body_*` tests assert the value that reaches the database
   is the token holder's name and that the spoofed string never appears in
   the SQL arguments — they fail against the old handlers.

Auth is faked without touching the database: the router-level
`Depends(get_current_user)` is overridden with a dependency that seeds
`request.state.user_dict`, which is the same cache `_extract_user` consults,
so the `require_permission(...)` dependency on each endpoint resolves the
user without a `validate_session` round trip.

Run:  PYTHONPATH=. python -m pytest tests/services/test_indent_lifecycle_route.py
"""
from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.main import app
from app.modules.auth.middleware import (
    AuthUser, _authuser_from_session, get_current_user,
)
from app.modules.production.router import _actor_name
from app.webhooks.event_bus import event_bus

BASE = "/api/v1/production"

# The one identity the server is allowed to persist in these tests.
ACTOR = "Priya Checker"
SPOOF = "Somebody Else"

SESSION = {
    "user_id": 7,
    "phone": "9876500000",
    "full_name": ACTOR,
    "email": "priya@candorfoods.in",
    "entity": "cfpl",
    "role_id": 3,
    "role_name": "planner",
    "is_admin": True,          # short-circuits require_permission, no DB call
    "role_ids": [3],
}


def _session(**over) -> dict:
    s = dict(SESSION)
    s.update(over)
    return s


# ── Fakes ────────────────────────────────────────────────────────────────────
class FakeConn:
    """Records every SQL call. `row` is what fetchrow returns for
    purchase_indent; `plan_line` for the production_plan_line lookup;
    `fetchvals` is popped in call order; `execute_result` is the asyncpg
    status tag ("UPDATE 1" / "UPDATE 0")."""

    def __init__(self, row=None, *, plan_line=None, execute_result="UPDATE 1",
                 fetchvals=None):
        self.row = row
        self.plan_line = plan_line
        self.execute_result = execute_result
        self.fetchvals = list(fetchvals or [])
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "production_plan_line" in sql:
            return self.plan_line
        return self.row

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.fetchvals.pop(0) if self.fetchvals else None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return self.execute_result

    def transaction(self):
        class _Tx:
            async def __aenter__(_s): return None
            async def __aexit__(_s, *exc): return False
        return _Tx()

    # -- assertions helpers --
    def sql_of(self, *needles) -> list[tuple[str, tuple]]:
        return [c for c in self.calls
                if all(n in c[0] for n in needles)]

    @property
    def all_args(self) -> list:
        return [a for _, args in self.calls for a in args]


class _FakePool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


DRAFT_ROW = {
    "indent_id": 42,
    "indent_number": "IND-20260824-0042",
    "status": "draft",
    "material_sku_name": "REFINED SUGAR",
    "required_qty_kg": 500.0,
    "required_by_date": "2026-09-01",
    "plan_line_id": None,
    "entity": "cfpl",
    "acknowledged_at": "2026-08-24T10:00:00+00:00",
}


def _row(**over) -> dict:
    r = dict(DRAFT_ROW)
    r.update(over)
    return r


@pytest.fixture
def client_with():
    """(conn, session) -> TestClient with auth seeded and the pool stubbed."""
    def _make(conn, session=None):
        sess = session or SESSION

        def _dep(request: Request) -> AuthUser:
            # Same cache `_extract_user` reads, so the require_permission
            # dependency that runs next needs no database.
            request.state.user_dict = sess
            return _authuser_from_session(sess)

        app.dependency_overrides[get_current_user] = _dep
        app.state.db_pool = _FakePool(conn)
        # No `with` block: the lifespan would try to reach the real database.
        return TestClient(app)
    yield _make
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def captured_events(monkeypatch):
    seen = []

    async def _fan_out(event):
        seen.append(event)

    monkeypatch.setattr(event_bus, "_fan_out", _fan_out)
    return seen


# ── Registration + auth floor ────────────────────────────────────────────────
@pytest.mark.parametrize("path", [
    f"{BASE}/indents/{{indent_id}}",
    f"{BASE}/indents/{{indent_id}}/send",
    f"{BASE}/indents/{{indent_id}}/acknowledge",
    f"{BASE}/indents/{{indent_id}}/link-po",
])
def test_new_routes_are_registered_for_put(path):
    methods = set()
    for r in app.routes:
        if getattr(r, "path", None) == path:
            methods |= set(getattr(r, "methods", ()) or ())
    assert "PUT" in methods, f"no PUT route for {path}"


def test_new_routes_require_authentication():
    """No dependency override here — the real gate must reject."""
    client = TestClient(app)
    assert client.put(f"{BASE}/indents/42", json={}).status_code in (401, 403)
    assert client.put(f"{BASE}/indents/42/send").status_code in (401, 403)
    assert client.put(f"{BASE}/indents/42/acknowledge").status_code in (401, 403)
    assert client.put(f"{BASE}/indents/42/link-po",
                      json={"po_reference": "PO-1"}).status_code in (401, 403)


def test_android_raise_endpoint_is_untouched():
    """POST /indents/raise is the live Android contract (JobCardService #12)."""
    methods = set()
    for r in app.routes:
        if getattr(r, "path", None) == f"{BASE}/indents/raise":
            methods |= set(getattr(r, "methods", ()) or ())
    assert "POST" in methods and "PUT" not in methods


# ── PUT /indents/{id}  (edit) ────────────────────────────────────────────────
def test_edit_draft_indent_updates_only_the_supplied_fields(client_with):
    conn = FakeConn(_row())
    res = client_with(conn).put(f"{BASE}/indents/42",
                                json={"required_qty_kg": 620.5, "priority": 2})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["updated"] is True
    assert body["indent_id"] == 42
    assert body["fields_changed"] == ["required_qty_kg", "priority"]

    upd = conn.sql_of("UPDATE purchase_indent")
    assert len(upd) == 1
    sql, args = upd[0]
    assert "required_qty_kg" in sql and "priority" in sql
    assert "required_by_date" not in sql          # not sent, not touched
    assert args == (620.5, 2, 42)


def test_edit_non_draft_indent_is_200_not_an_exception(client_with):
    """Wrong state means the caller's view is stale, not that the request was
    malformed — the service dict comes back at HTTP 200 and nothing is written."""
    conn = FakeConn(_row(status="raised"))
    res = client_with(conn).put(f"{BASE}/indents/42", json={"priority": 1})

    assert res.status_code == 200, res.text
    assert res.json() == {"updated": False, "reason": "not_draft",
                          "message": "Can only edit draft indents"}
    assert conn.sql_of("UPDATE") == []


def test_edit_with_no_fields_is_200_no_fields(client_with):
    conn = FakeConn(_row())
    res = client_with(conn).put(f"{BASE}/indents/42", json={})
    assert res.status_code == 200
    assert res.json() == {"updated": False, "reason": "no_fields",
                          "message": "No fields to update"}
    assert conn.sql_of("UPDATE") == []


def test_edit_missing_indent_is_404(client_with):
    conn = FakeConn(None)
    res = client_with(conn).put(f"{BASE}/indents/999", json={"priority": 1})
    assert res.status_code == 404
    payload = res.json()
    assert payload["error"] == "not_found"
    assert payload["message"] == "Indent not found"


# ── PUT /indents/{id}/send ───────────────────────────────────────────────────
def test_send_draft_indent_raises_it_and_writes_both_alerts(client_with, captured_events):
    conn = FakeConn(_row())
    res = client_with(conn).put(f"{BASE}/indents/42/send")

    assert res.status_code == 200, res.text
    # updated:True is stamped by _indent_result so every one of these four
    # routes answers in the same shape the production-indent family uses.
    assert res.json() == {"indent_id": 42, "indent_number": "IND-20260824-0042",
                          "status": "raised", "alerts_created": 2,
                          "updated": True}

    assert conn.sql_of("UPDATE purchase_indent", "status = 'raised'")
    alerts = conn.sql_of("INSERT INTO store_alert")
    assert len(alerts) == 2
    assert "'material_shortage', 'purchase'" in alerts[0][0]
    assert "'indent_raised', 'stores'" in alerts[1][0]


def test_send_emits_indent_sent_through_the_deferred_bus(client_with, captured_events):
    conn = FakeConn(_row())
    assert client_with(conn).put(f"{BASE}/indents/42/send").status_code == 200
    assert [e.event_type for e in captured_events] == ["indent.sent"]
    assert captured_events[0].payload["indent_id"] == 42
    assert captured_events[0].payload["material"] == "REFINED SUGAR"


def test_send_non_draft_indent_is_200_not_an_exception(client_with, captured_events):
    conn = FakeConn(_row(status="acknowledged"))
    res = client_with(conn).put(f"{BASE}/indents/42/send")

    assert res.status_code == 200, res.text
    assert res.json()["updated"] is False
    assert res.json()["reason"] == "not_draft"
    assert conn.sql_of("INSERT INTO store_alert") == []
    assert captured_events == []


def test_send_missing_indent_is_404(client_with, captured_events):
    conn = FakeConn(None)
    res = client_with(conn).put(f"{BASE}/indents/999/send")
    assert res.status_code == 404
    assert res.json()["message"] == "Indent not found"
    assert captured_events == [], "a discarded transaction must not fan out"


# ── PUT /indents/{id}/acknowledge ────────────────────────────────────────────
def test_acknowledge_persists_the_token_identity_not_the_body(client_with):
    """SECURITY: the endpoint takes no body at all. Even if the client sends
    an acknowledged_by, the name written to the column is the token holder's."""
    conn = FakeConn(_row(status="raised"))
    res = client_with(conn).put(f"{BASE}/indents/42/acknowledge",
                                json={"acknowledged_by": SPOOF})

    assert res.status_code == 200, res.text
    assert res.json()["acknowledged_by"] == ACTOR
    assert res.json()["status"] == "acknowledged"

    sql, args = conn.sql_of("UPDATE purchase_indent", "acknowledged_by")[0]
    assert args == (42, ACTOR)
    assert SPOOF not in conn.all_args


def test_acknowledge_wrong_state_is_200_not_an_exception(client_with):
    conn = FakeConn(_row(status="draft"))
    res = client_with(conn).put(f"{BASE}/indents/42/acknowledge")

    assert res.status_code == 200, res.text
    assert res.json() == {"updated": False, "reason": "invalid_status",
                          "message": "Can only acknowledge raised indents"}
    assert conn.sql_of("UPDATE") == []


def test_acknowledge_missing_indent_is_404(client_with):
    conn = FakeConn(None)
    res = client_with(conn).put(f"{BASE}/indents/999/acknowledge")
    assert res.status_code == 404
    assert res.json()["message"] == "Indent not found"


def test_acknowledge_falls_back_to_email_when_full_name_is_blank(client_with):
    """A session may legitimately carry full_name="" — the column must still
    get something that identifies a human."""
    conn = FakeConn(_row(status="raised"))
    res = client_with(conn, _session(full_name="")).put(
        f"{BASE}/indents/42/acknowledge")

    assert res.status_code == 200
    assert res.json()["acknowledged_by"] == "priya@candorfoods.in"


# ── PUT /indents/{id}/link-po ────────────────────────────────────────────────
def test_link_po_moves_acknowledged_to_po_created(client_with):
    conn = FakeConn(_row(status="acknowledged"))
    res = client_with(conn).put(f"{BASE}/indents/42/link-po",
                                json={"po_reference": "PO-2026-0917"})

    assert res.status_code == 200, res.text
    assert res.json() == {"indent_id": 42, "status": "po_created",
                          "po_reference": "PO-2026-0917", "updated": True}
    sql, args = conn.sql_of("UPDATE purchase_indent", "po_created")[0]
    assert args == (42, "PO-2026-0917")


def test_link_po_wrong_state_is_200_not_an_exception(client_with):
    conn = FakeConn(_row(status="raised"))
    res = client_with(conn).put(f"{BASE}/indents/42/link-po",
                                json={"po_reference": "PO-1"})
    assert res.status_code == 200, res.text
    assert res.json()["updated"] is False
    assert res.json()["reason"] == "invalid_status"
    assert conn.sql_of("UPDATE") == []


def test_link_po_missing_indent_is_404(client_with):
    conn = FakeConn(None)
    res = client_with(conn).put(f"{BASE}/indents/999/link-po",
                                json={"po_reference": "PO-1"})
    assert res.status_code == 404


def test_link_po_requires_a_po_reference(client_with):
    conn = FakeConn(_row(status="acknowledged"))
    res = client_with(conn).put(f"{BASE}/indents/42/link-po", json={})
    assert res.status_code == 422
    assert conn.calls == []


# ── Permission gate ──────────────────────────────────────────────────────────
def test_send_is_refused_without_the_send_permission(client_with, monkeypatch,
                                                     captured_events):
    """Non-admin whose roles do not carry production.indents.send.create."""
    import app.modules.auth.services.permission_service as perm_svc

    async def _deny(*a, **k):
        return False

    monkeypatch.setattr(perm_svc, "check_permission", _deny)

    conn = FakeConn(_row())
    res = client_with(conn, _session(is_admin=False)).put(f"{BASE}/indents/42/send")

    assert res.status_code == 403
    assert res.json()["error"] == "forbidden"
    assert res.json()["details"]["sub_module"] == "indents"
    assert conn.sql_of("UPDATE") == [], "nothing may be written when the gate rejects"
    assert captured_events == []


def test_acknowledge_is_allowed_when_the_permission_check_passes(client_with,
                                                                 monkeypatch):
    import app.modules.auth.services.permission_service as perm_svc

    async def _allow(*a, **k):
        return True

    monkeypatch.setattr(perm_svc, "check_permission", _allow)

    conn = FakeConn(_row(status="raised"))
    res = client_with(conn, _session(is_admin=False)).put(
        f"{BASE}/indents/42/acknowledge")
    assert res.status_code == 200, res.text
    assert res.json()["acknowledged_by"] == ACTOR


# ── SECURITY FIX: production indents (FG/SFG) maker / checker ────────────────
def test_create_production_indent_ignores_a_body_supplied_maker_user(client_with):
    """Fails against the old handler, which took maker_user straight off the
    request body and wrote it to the column."""
    conn = FakeConn(fetchvals=[None, 77])       # no duplicate, then nextval
    res = client_with(conn).post(f"{BASE}/production-indents", json={
        "item_description": "MASALA PEANUT 200G",
        "material_type": "FG",
        "required_qty": 250,
        "triggered_by_so": "SO-4411",
        "maker_user": SPOOF,
    })

    assert res.status_code == 200, res.text
    sql, args = conn.sql_of("INSERT INTO production_indent")[0]
    assert args[10] == ACTOR, "maker_user column must hold the token identity"
    assert SPOOF not in args
    assert SPOOF not in conn.all_args


def test_production_indent_create_model_has_no_maker_user_field():
    from app.modules.production.router import ProductionIndentCreate
    assert "maker_user" not in ProductionIndentCreate.model_fields


def test_approve_production_indent_ignores_a_body_supplied_checker_user(client_with):
    """Fails against the old handler: checker_user came off the body, so one
    person could be both maker and checker just by typing another name."""
    conn = FakeConn(execute_result="UPDATE 1")
    res = client_with(conn).put(f"{BASE}/production-indents/PRDI-1/approve",
                                json={"checker_user": SPOOF,
                                      "checker_comment": "looks fine"})

    assert res.status_code == 200, res.text
    assert res.json() == {"updated": True}
    sql, args = conn.sql_of("UPDATE production_indent", "'approved'")[0]
    assert args == ("PRDI-1", ACTOR, "looks fine")
    assert SPOOF not in conn.all_args


def test_return_production_indent_ignores_a_body_supplied_checker_user(client_with):
    conn = FakeConn(execute_result="UPDATE 1")
    res = client_with(conn).put(f"{BASE}/production-indents/PRDI-1/return",
                                json={"checker_user": SPOOF,
                                      "checker_comment": "wrong SO"})

    assert res.status_code == 200, res.text
    sql, args = conn.sql_of("UPDATE production_indent", "'draft'")[0]
    assert args == ("PRDI-1", ACTOR, "wrong SO")
    assert SPOOF not in conn.all_args


def test_checker_action_model_has_no_checker_user_field():
    from app.modules.production.router import CheckerAction
    assert "checker_user" not in CheckerAction.model_fields
    assert "checker_comment" in CheckerAction.model_fields


def test_wrong_state_submit_returns_updated_false_not_an_exception(client_with):
    """A row that has already left 'draft' produces UPDATE 0 — HTTP 200 with
    {"updated": false}, which the UI treats as a stale view and refetches."""
    conn = FakeConn(execute_result="UPDATE 0")
    res = client_with(conn).put(f"{BASE}/production-indents/PRDI-1/submit")
    assert res.status_code == 200, res.text
    assert res.json() == {"updated": False}


def test_wrong_state_approve_returns_updated_false_not_an_exception(client_with):
    conn = FakeConn(execute_result="UPDATE 0")
    res = client_with(conn).put(f"{BASE}/production-indents/PRDI-1/approve",
                                json={"checker_comment": ""})
    assert res.status_code == 200, res.text
    assert res.json() == {"updated": False}


# ── The identity helper itself ───────────────────────────────────────────────
def _u(**over) -> AuthUser:
    kw = dict(user_id=7, phone="9876500000", full_name=ACTOR,
              email="priya@candorfoods.in", entity="cfpl", role_id=3,
              role_name="planner", is_admin=False)
    kw.update(over)
    return AuthUser(**kw)


def test_actor_name_prefers_full_name():
    assert _actor_name(_u()) == ACTOR


def test_actor_name_falls_back_through_email_then_phone_then_id():
    assert _actor_name(_u(full_name="")) == "priya@candorfoods.in"
    assert _actor_name(_u(full_name="", email="")) == "9876500000"
    assert _actor_name(_u(full_name="", email="", phone="")) == "user:7"


# ── Date filters must be cast, or the list 500s ──────────────────────────────
# created_at is TIMESTAMPTZ. An uncast $N is resolved to timestamptz from the
# other operand, and asyncpg then refuses to encode the plain 'YYYY-MM-DD'
# string the query param carries ("expected a datetime.date or
# datetime.datetime instance") -- so the whole tab 500s the moment an operator
# picks a date. date_to always had the ::date cast; date_from did not, and
# nothing called this endpoint before the prod-indents page, so it had never
# been exercised. These pin the cast on BOTH bounds.
def _list_sql_for(conn) -> str:
    """The SELECT ... FROM production_indent the list endpoint issued."""
    rows = [sql for sql, _ in conn.calls
            if "FROM production_indent" in sql and sql.strip().upper().startswith("SELECT")]
    assert rows, f"no production_indent SELECT was issued; calls={conn.calls}"
    return " ".join(rows)


def test_production_indent_list_casts_both_date_bounds(client_with):
    conn = FakeConn(None, fetchvals=[0])
    res = client_with(conn).get(
        f"{BASE}/production-indents?date_from=2026-08-01&date_to=2026-08-24")
    assert res.status_code == 200, res.text

    sql = _list_sql_for(conn)
    assert "created_at >=" in sql, sql
    # The regression: `created_at >= $3` with no cast. Any date comparison that
    # binds a bare parameter is the bug, so assert on the cast explicitly
    # rather than on the absence of an error.
    import re
    for m in re.finditer(r"created_at\s*(>=|<=)\s*(\$\d+)(::date)?", sql):
        assert m.group(3) == "::date", (
            f"date bound {m.group(0)!r} binds an uncast parameter against a "
            f"TIMESTAMPTZ column -- asyncpg will reject the str and 500")


def test_production_indent_list_date_from_binds_the_raw_string(client_with):
    """The cast is what makes passing the raw query-param string legal."""
    conn = FakeConn(None, fetchvals=[0])
    client_with(conn).get(f"{BASE}/production-indents?date_from=2026-08-01")
    args = [a for sql, a in conn.calls if "FROM production_indent" in sql]
    assert any("2026-08-01" in a for a in args), args


# ── The second write path to production_indent.maker_user ───────────────────
# POST /rtv/dispositions with disposition_type='reprocess' calls
# create_production_indent(maker_user=decided_by, status='submitted') -- it
# files an indent straight into the checker's queue. decided_by used to come
# from the request body, so any holder of production.rtv.create could file one
# under someone else's name: the same impersonation the /production-indents
# fix closed, through a different door. This pins the identity to the token.
def test_rtv_disposition_ignores_a_body_supplied_decided_by(client_with, monkeypatch):
    import app.modules.production.services.rtv_disposition_service as rtv
    seen = {}

    async def _fake_assign(conn, **kw):
        seen.update(kw)
        return {"disposition_id": "RTVD-1"}

    monkeypatch.setattr(rtv, "assign_disposition", _fake_assign)

    conn = FakeConn(None)
    res = client_with(conn).post(f"{BASE}/rtv/dispositions", json={
        "rtv_id": "RTV-1",
        "disposition_type": "reprocess",
        # The attack: name someone else as the decider.
        "decided_by": "Someone Else",
    })

    assert res.status_code == 200, res.text
    assert seen["decided_by"] == ACTOR, (
        "decided_by must come from the access token, not the request body")
    assert "Someone Else" not in seen.values()


def test_rtv_disposition_body_no_longer_declares_decided_by():
    """Dropping the field is what makes the spoof impossible rather than
    merely overridden -- a future refactor that re-adds **body.model_dump()
    ordering would otherwise silently reopen it."""
    from app.modules.production.router import RtvDispositionBody
    assert "decided_by" not in RtvDispositionBody.model_fields
