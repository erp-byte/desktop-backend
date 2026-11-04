"""Customer-return decisions: who may decide and when, against an in-memory fake
connection.

The fake keeps the _rtv_header rows, the auth_user rows and the cr_email_routing
config in dicts and answers each statement by what it reads or writes (a SELECT
returns only the columns it names; the guarded UPDATE is applied only from the
states it lists, reading the stored status as PostgreSQL does — _sql_status — not
with Python's truthiness), raising on any statement it does not know. So these
tests pin what approval_service.apply_cr_action decides for every channel — the BH
and the standing deputies may decide, nobody else (no admin override), never the
person who raised the return, and only from Pending / On Hold — without a database.

The HTTP half goes through the real router for the in-app POST /approve and the
header PUT (the token round trip is stubbed one level lower, as
test_transfer_request_decisions does), so the 403 / 409 envelopes are checked as the
screen sees them, and the PUT is shown not to be a fourth way to decide. The other
test_cr_approval_* files import the fakes from here.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_approval_decisions.py
"""
from __future__ import annotations

import asyncio
import logging
import re
import types
from datetime import datetime, timezone

import asyncpg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.middleware import request_context
from app.modules.auth.middleware import AuthUser
from app.modules.customer_returns import approval_token, schemas
from app.modules.customer_returns import router as R
from app.modules.customer_returns.services import approval_service as svc
from app.modules.customer_returns.services import mail_service, query_service

COMPANY = "CFPL"
CR = "CR-20261019103000"
TOKEN_KEY = "test-cr-approval-signing-key-0123456789"

BH_EMAIL = "prashant.pal@candorfoods.in"
DEPUTY_EMAIL = "satyendra@candorfoods.in"
DEPUTY2_EMAIL = "rmpatil@candorfoods.in"          # a deputy with no auth_user row (mail-only)
CREATOR_EMAIL = "warehouse.w202@candorfoods.in"
ADMIN_EMAIL = "admin@candorfoods.in"
NOTIFY_TO = "pooja.parkar@candorfoods.in"
BH_PHONE = "919800000011"                          # as wa_return_message stores it

USERS = [
    {"user_id": 11, "full_name": "Prashant Pal", "email": BH_EMAIL, "phone": "+919800000011",
     "is_active": True, "roles": ["business_head"]},
    {"user_id": 12, "full_name": "Satyendra Kumar Garg", "email": DEPUTY_EMAIL, "phone": "9800000012",
     "is_active": True, "roles": []},
    {"user_id": 13, "full_name": "W202 Stores", "email": CREATOR_EMAIL, "phone": "9800000013",
     "is_active": True, "roles": []},
    {"user_id": 14, "full_name": "ERP Admin", "email": ADMIN_EMAIL, "phone": "9800000014",
     "is_active": True, "roles": []},
    {"user_id": 15, "full_name": "Shubham Seth", "email": "shubham.seth@candorfoods.in",
     "phone": "9800000015", "is_active": True, "roles": ["sales"]},
]

ROUTING = [
    {"kind": "deputy_approver", "match_key": "Satyendra Kumar Garg", "match_type": "name",
     "email": DEPUTY_EMAIL, "sort_order": 1},
    {"kind": "deputy_approver", "match_key": "R M Patil", "match_type": "name",
     "email": DEPUTY2_EMAIL, "sort_order": 2},
    {"kind": "notify_to", "match_key": "", "match_type": "const", "email": NOTIFY_TO, "sort_order": 1},
    {"kind": "constant_cc", "match_key": "", "match_type": "const",
     "email": "billing@candorfoods.in", "sort_order": 1},
]
NO_DEPUTIES = [r for r in ROUTING if r["kind"] != "deputy_approver"]

LINE = {"rtv_id": CR, "item_description": "CASHEW W320 10KG", "qty": "4", "rate": "950",
        "value": "3800", "net_weight": "40", "uom": "10"}


def header(**over) -> dict:
    """A cfpl_rtv_header row, as the columns the services read hold it."""
    row = {"id": 501, "rtv_id": CR, "status": "Pending", "business_head": "Prashant Pal",
           "created_by": CREATOR_EMAIL, "customer": "Sample Foods Pvt Ltd", "factory_unit": "W202",
           "invoice_number": "INV-2044", "challan_no": None, "sales_poc": "Shubham Seth",
           "sales_poc_email": None, "approved_by": None, "approved_at": None, "approval_remark": None}
    row.update(over)
    return row


# ── Fakes ────────────────────────────────────────────────────────────────────
_UPDATE = ("UPDATE {t} SET status = $2, approved_by = $3, approved_at = NOW(), "
           "approval_remark = COALESCE($4, approval_remark), updated_at = NOW() "
           "WHERE rtv_id = $1 AND COALESCE(NULLIF(btrim(status), ''), 'Pending') = ANY($5::text[]) "
           "RETURNING rtv_id, status")
# create_service.update_cr's statement: the provided columns, then updated_at, on the
# header row its undecided-check locked (by surrogate id).
_HEADER_PUT = re.compile(r"UPDATE (\w+_rtv_header) SET (.+?) WHERE id = \$(\d+) RETURNING ([\w, ]+)")
NOW = datetime(2026, 10, 19, 5, 0)      # what NOW() writes on RDS: naive UTC
LOGGED_AT = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)   # what a cr_approval_log entry is stamped
# query_service._fetch_lines: the return's lines, by its rtv_id.
_LINES = re.compile(r"SELECT h\.rtv_id AS rtv_id, l\.item_description, .* FROM (\w+)_rtv_lines l "
                    r"JOIN (\w+)_rtv_header h ON l\.header_id = h\.id WHERE h\.rtv_id = \$1 ORDER BY l\.item_description")
_LINE_COLS = ("item_description", "material_type", "item_category", "sub_category", "sale_group", "uom", "qty",
              "rate", "value", "conversion", "net_weight", "carton_weight", "lot_number", "item_mark",
              "spl_remarks", "vakkal", "created_at", "updated_at")


def _sql_status(status):
    """COALESCE(NULLIF(btrim(status), ''), 'Pending') as PostgreSQL evaluates it:
    btrim drops spaces only, and only NULL or '' becomes Pending."""
    if status is None:
        return "Pending"
    return status.strip(" ") or "Pending"


class FakeConn:
    def __init__(self, *headers, users=USERS, routing=ROUTING, wa_messages=(),
                 routing_error=False, wa_store_error=False, lines=None, approval_log=(),
                 has_approval_log=True):
        self.headers = {h["rtv_id"]: dict(h) for h in headers}
        # Each return's lines (LINE unless given): what the figures fingerprint reads.
        self.lines = {h["rtv_id"]: [dict(x) for x in (lines if lines is not None else [LINE])]
                      for h in headers}
        # cr_approval_log entries {company, rtv_id, event, figures, actor, at, outcome};
        # has_approval_log=False: migration 137 not applied.
        self.approval_log = [dict(e) for e in approval_log]
        self.has_approval_log = has_approval_log
        self.users = [dict(u) for u in users]
        self.routing = [dict(r) for r in routing]
        self.wa = {m["wamid"]: dict(m) for m in wa_messages}
        self.routing_error = routing_error
        self.wa_store_error = wa_store_error
        self.before_update = None      # a hook: another approver decides in between
        self.fail_on: str | None = None  # raise on the first statement containing this
        self.calls: list[tuple[str, str, tuple]] = []
        self.deletion_log: list[tuple] = []

    def _log(self, kind, sql, args):
        s = " ".join(sql.split())
        self.calls.append((kind, s, args))
        if self.fail_on and self.fail_on in s:
            raise ConnectionError("connection lost")
        return s

    def transaction(self):
        class _Tx:  # one connection, statements in order: nothing to isolate
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return _Tx()

    def _header_for(self, table):
        assert table in ("cfpl_rtv_header", "cdpl_rtv_header"), table
        return self.headers

    async def fetchrow(self, sql, *args):
        s = self._log("fetchrow", sql, args)
        m = re.fullmatch(r"SELECT ([\w, ]+) FROM (\w+_rtv_header) WHERE rtv_id = \$1(?: FOR UPDATE)?", s)
        if m:
            row = self._header_for(m[2]).get(args[0])
            return {c.strip(): row[c.strip()] for c in m[1].split(",")} if row else None
        m = re.fullmatch(r"UPDATE (\w+_rtv_header) SET status = \$2, approved_by = \$3, .*", s)
        if m:
            assert s == _UPDATE.format(t=m[1]), f"the decision UPDATE changed: {s}"
            if self.before_update:
                self.before_update(self)
            row = self._header_for(m[1]).get(args[0])
            if not row or _sql_status(row["status"]) not in args[4]:
                return None
            row.update(status=args[1], approved_by=args[2], approved_at="NOW()",
                       approval_remark=args[3] if args[3] is not None else row["approval_remark"])
            return {"rtv_id": row["rtv_id"], "status": row["status"]}
        m = _HEADER_PUT.fullmatch(s)
        if m:  # the header PUT: apply each `col = $n`, return the RETURNING columns
            row = next((r for r in self._header_for(m[1]).values() if r["id"] == args[int(m[3]) - 1]), None)
            if not row:
                return None
            for assignment in m[2].split(", "):
                col, value = assignment.split(" = ")
                assert col in schemas.CRHeaderUpdate.model_fields or col == "updated_at", col
                row[col] = NOW if value == "NOW()" else args[int(value.lstrip("$")) - 1]
            return {c: row.get(c) for c in m[4].split(", ")}
        if s == ("SELECT company, rtv_id, wa_phone, created_at, to_jsonb(m) ->> 'figures' AS figures "
                 "FROM wa_return_message m WHERE wamid = $1"):
            msg = self.wa.get(args[0])
            if not msg:
                return None
            return {**{k: msg[k] for k in ("company", "rtv_id", "wa_phone")},
                    "created_at": msg.get("created_at", NOW.replace(tzinfo=timezone.utc)),
                    "figures": msg.get("figures")}
        if s.startswith("SELECT figures, actor, at FROM cr_approval_log WHERE company = $1 AND rtv_id = $2 "
                        "AND event = 'sent' ORDER BY at DESC"):
            sent = [e for e in self._logged(*args) if e["event"] == "sent"]
            return ({"figures": sent[-1]["figures"], "actor": sent[-1]["actor"], "at": sent[-1]["at"]}
                    if sent else None)
        if s.startswith("SELECT email FROM auth_user WHERE phone = ANY($1::text[]) AND COALESCE(is_active, TRUE)"):
            hits = sorted((u for u in self.users if u.get("is_active", True) and u.get("phone") in args[0]),
                          key=lambda u: (not (u.get("email") or "").strip(), u["user_id"]))
            return {"email": hits[0].get("email")} if hits else None
        raise AssertionError(f"unexpected fetchrow: {s[:100]}")

    def _logged(self, company, rtv_id):
        return [e for e in self.approval_log if (e["company"], e["rtv_id"]) == (company, rtv_id)]

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        m = _LINES.fullmatch(s)
        if m:
            return [{"rtv_id": args[0], **{c: x.get(c) for c in _LINE_COLS}} for x in self.lines.get(args[0], [])]
        if s.startswith("SELECT DISTINCT u.full_name AS name, COALESCE(u.email, '') AS email, r.role_name "
                        "FROM auth_user u JOIN auth_role r ON r.role_name IN ('business_head', 'sales')"):
            return sorted(({"name": u["full_name"], "email": u.get("email") or "", "role_name": role}
                           for u in self.users if u.get("is_active", True)
                           for role in u.get("roles", []) if role in ("business_head", "sales")),
                          key=lambda r: r["name"])
        if s.startswith("SELECT kind, match_key, match_type, email FROM cr_email_routing WHERE active"):
            if self.routing_error:
                raise RuntimeError('relation "cr_email_routing" does not exist')
            return [{k: r[k] for k in ("kind", "match_key", "match_type", "email")}
                    for r in sorted(self.routing, key=lambda r: (r["kind"], r["sort_order"], r["match_key"]))]
        raise AssertionError(f"unexpected fetch: {s[:100]}")

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        m = re.fullmatch(r"SELECT (factory_unit|status) FROM (\w+_rtv_header) WHERE rtv_id = \$1", s)
        if m:  # permissions: the return's unit, for a warehouse lock; send_service: its status
            row = self._header_for(m[2]).get(args[0])
            return row[m[1]] if row else None
        if s == "SELECT to_regclass($1::text) IS NOT NULL":
            return {"public.cr_approval_log": self.has_approval_log, "public.wa_return_message": True}[args[0]]
        if s.startswith("SELECT EXISTS (SELECT 1 FROM cr_approval_log WHERE company = $1 AND rtv_id = $2 "
                        "AND ($3::timestamptz IS NULL OR at > $3))"):
            return any(args[2] is None or e["at"] > args[2] for e in self._logged(args[0], args[1]))
        if s == ("SELECT EXISTS (SELECT 1 FROM cr_approval_log WHERE company = $1 AND rtv_id = $2 "
                 "AND event = 'sent')"):
            return any(e["event"] == "sent" for e in self._logged(*args))
        if s == "SELECT EXISTS (SELECT 1 FROM wa_return_message WHERE company = $1 AND rtv_id = $2)":
            return any((m["company"], m["rtv_id"]) == tuple(args) for m in self.wa.values())
        if s.startswith("SELECT phone FROM auth_user WHERE lower(btrim(email)) = lower(btrim($1))"):
            for u in sorted(self.users, key=lambda u: u["user_id"]):
                if (u.get("is_active", True) and (u.get("email") or "").strip().lower() == args[0].strip().lower()
                        and (u.get("phone") or "").strip()):
                    return u["phone"]
            return None
        raise AssertionError(f"unexpected fetchval: {s[:100]}")

    async def execute(self, sql, *args):
        s = self._log("execute", sql, args)
        if s.startswith("INSERT INTO wa_return_message (wamid, company, rtv_id, wa_phone"):
            if self.wa_store_error:
                raise RuntimeError('relation "wa_return_message" does not exist')
            self.wa[args[0]] = {"wamid": args[0], "company": args[1], "rtv_id": args[2], "wa_phone": args[3],
                                "figures": args[4] if len(args) > 4 else None}
            return "INSERT 0 1"
        if s.startswith("INSERT INTO cr_approval_log (company, rtv_id, event, figures, actor, outcome)"):
            if not self.has_approval_log:
                raise asyncpg.UndefinedTableError('relation "cr_approval_log" does not exist')
            company, rtv_id, event, figures, actor, outcome = args
            self.approval_log.append({"company": company, "rtv_id": rtv_id, "event": event, "figures": figures,
                                      "actor": actor, "at": LOGGED_AT, "outcome": outcome})
            return "INSERT 0 1"
        if s.startswith("INSERT INTO cr_deletion_log"):     # create_service.delete_cr's record
            self.deletion_log.append(args)
            return "INSERT 0 1"
        m = re.fullmatch(r"DELETE FROM (\w+_rtv_header) WHERE rtv_id = \$1", s)
        if m:
            return f"DELETE {int(self._header_for(m[1]).pop(args[0], None) is not None)}"
        raise AssertionError(f"unexpected execute: {s[:100]}")

    def updates(self):
        return [c for c in self.calls if c[1].startswith("UPDATE")]


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


def _user(email, *, is_admin=False, user_id=7):
    return AuthUser(user_id=user_id, phone="9000000007", full_name="Some One", email=email, entity="cfpl",
                    role_id=3, role_name="", is_admin=is_admin, allowed_warehouses=[])


BH = _user(BH_EMAIL, user_id=11)
DEPUTY = _user(DEPUTY_EMAIL, user_id=12)
CREATOR = _user(CREATOR_EMAIL, user_id=13)
ADMIN = _user(ADMIN_EMAIL, is_admin=True, user_id=14)


# ── Fixtures every test_cr_approval_* file uses ──────────────────────────────
@pytest.fixture(autouse=True)
def no_guard_errors(caplog):
    """The services swallow and log channel failures by design. A guarded call (a real
    .env read, a socket, an SMTP session) would be swallowed the same way, so fail the
    test if one was logged."""
    caplog.set_level(logging.INFO)
    yield
    # At teardown caplog.records is the teardown phase's; the test ran in setup + call.
    records = [*caplog.get_records("setup"), *caplog.get_records("call")]
    hits = [r for r in records
            if r.exc_info and r.exc_info[0] is not None and r.exc_info[0].__name__ == "GuardError"]
    assert not hits, f"a guarded call was swallowed: {hits[0].getMessage()}"


@pytest.fixture
def tokens(monkeypatch):
    """Sign approval links with a test key; Settings() is never constructed."""
    monkeypatch.setattr(approval_token, "Settings", lambda: types.SimpleNamespace(CR_ACTION_TOKEN_TTL_DAYS=14))
    monkeypatch.setattr(approval_token, "_secret", lambda settings=None: TOKEN_KEY)
    monkeypatch.setattr(approval_token, "_alg", lambda settings=None: "HS256")
    return approval_token.make_action_token


@pytest.fixture
def mail(monkeypatch, tokens):
    """Capture what would go on the wire. `mail.smtp_host = ""` turns SMTP off,
    `mail.fail_to` makes a send to those addresses raise, like a refused RCPT."""
    box = types.SimpleNamespace(sent=[], smtp_host="smtp.test", fail_to=set())

    class _SMTP:
        def __init__(self, host, port, timeout=None):
            assert host == "smtp.test"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            if set(to_addrs) & box.fail_to:
                raise OSError("550 mailbox unavailable")
            box.sent.append({"to": msg["To"], "cc": msg["Cc"], "subject": msg["Subject"],
                             "rcpts": list(to_addrs), "msgid": msg["Message-ID"],
                             "irt": msg["In-Reply-To"], "refs": msg["References"],
                             "text": msg.get_body(("plain",)).get_content()})

    monkeypatch.setattr(mail_service.smtplib, "SMTP", _SMTP)
    monkeypatch.setattr(mail_service, "Settings", lambda: types.SimpleNamespace(
        SMTP_HOST=box.smtp_host, SMTP_PORT=587, SMTP_EMAIL="erp@candorfoods.in",
        SMTP_APP_PASSWORD="pw", PUBLIC_BACKEND_URL="https://api.test", WEB_APP_URL="https://erp.test"))
    return box


@pytest.fixture
def get_cr(monkeypatch):
    """query_service.get_cr over the fake's header rows (its own SQL is another file's)."""
    async def _get_cr(conn, company, cr_id):
        row = conn.headers.get(cr_id)
        if not row:
            raise HTTPException(404, detail={"error": "customer_return_not_found",
                                             "message": f"No customer return {cr_id}"})
        lines = [dict(x) for x in conn.lines.get(cr_id, [])]
        detail = {**row, "status": row["status"] or "Pending", "lines": lines, "boxes": []}
        return {**detail, "figures_version": query_service.figures_version(detail)}

    monkeypatch.setattr(query_service, "get_cr", _get_cr)
    return _get_cr


@pytest.fixture
def client(monkeypatch):
    """(conn, user, allowed) -> TestClient on the real customer-returns router behind
    the real get_current_user (the session lookup is stubbed one level lower). A write
    route's customer_returns permission answers `allowed` (granted by default, so
    these tests reach the decision rules); `client.asked` lists what was checked."""
    import app.modules.auth.services.auth_service as auth_service
    import app.modules.auth.services.permission_service as permission_service

    asked: list[tuple] = []

    def _make(conn, user=BH, *, auth=True, allowed=True, allowed_entities=None, allowed_warehouses=None):
        async def _validate(_conn, _token):
            return {"user_id": user.user_id, "phone": user.phone, "email": user.email,
                    "role_id": user.role_id, "is_admin": user.is_admin,
                    "allowed_warehouses": allowed_warehouses or [], "allowed_entities": allowed_entities}

        async def _check(_conn, _role_ids, _is_admin, module, sub_module, sub_sub_module, action, **scope):
            asked.append((module, sub_module, action, scope.get("entity"), scope.get("warehouse")))
            return allowed

        monkeypatch.setattr(auth_service, "validate_session", _validate)
        monkeypatch.setattr(permission_service, "check_permission", _check)
        app = FastAPI()
        request_context.install(app)
        app.include_router(R.router)
        app.state.db_pool = _Pool(conn)
        return TestClient(app, headers={"Authorization": "Bearer test-token"} if auth else {})

    _make.asked = asked
    return _make


def decide(conn, actor, action="approve", remark=None, cr_id=CR):
    return asyncio.run(svc.apply_cr_action(conn, COMPANY, cr_id, action, actor, remark))


def refusal(conn, actor, action="approve", cr_id=CR) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        decide(conn, actor, action, cr_id=cr_id)
    return exc.value


# ── status labels ────────────────────────────────────────────────────────────
def test_status_labels_and_the_undecided_states():
    assert svc.status_label("Submitted") == "Approved (boxes saved)"
    assert svc.status_label(None) == "Pending" and svc.status_label("") == "Pending"
    assert svc.status_label("  ") == "Pending" and svc.status_label(" Submitted ") == "Approved (boxes saved)"
    assert [svc.status_label(s) for s in ("Pending", "On Hold", "Approved", "Rejected")] == \
        ["Pending", "On Hold", "Approved", "Rejected"]
    assert svc._NON_TERMINAL == ["Pending", "On Hold"]
    assert [svc.is_undecided(s) for s in (None, "", "  ", "Pending", "On Hold", "Approved", "Submitted",
                                          "Rejected", " Approved ")] == \
        [True, True, True, True, True, False, False, False, False]
    # The Python reading and the guarded UPDATE's SQL reading agree on every one.
    for s in (None, "", "  ", " Pending ", "On Hold", "Approved", "Submitted", " Rejected"):
        assert svc._status(s) == _sql_status(s), s


# ── WHEN: only from Pending / On Hold ────────────────────────────────────────
@pytest.mark.parametrize("status", ["Approved", "Submitted", "Rejected"])
@pytest.mark.parametrize("action", ["approve", "reject", "hold"])
def test_a_decided_return_is_already_actioned_and_nothing_is_written(status, action):
    conn = FakeConn(header(status=status, approved_by=DEPUTY_EMAIL))
    res = decide(conn, BH_EMAIL, action)
    assert res == {"rtv_id": CR, "status": status, "already_actioned": True}
    assert conn.updates() == []
    assert conn.headers[CR]["status"] == status and conn.headers[CR]["approved_by"] == DEPUTY_EMAIL


@pytest.mark.parametrize("status", ["Pending", "On Hold", None])
@pytest.mark.parametrize("action, new_status", [("approve", "Approved"), ("reject", "Rejected"), ("hold", "On Hold")])
def test_an_undecided_return_is_decided_once_from_the_undecided_states_only(status, action, new_status):
    conn = FakeConn(header(status=status))
    res = decide(conn, BH_EMAIL, action, remark="  short by 2 cartons ")
    assert res == {"rtv_id": CR, "status": new_status, "already_actioned": False, "decided_by": BH_EMAIL}
    (call,) = conn.updates()
    assert call[2] == (CR, new_status, BH_EMAIL, "short by 2 cartons", ["Pending", "On Hold"])
    assert conn.headers[CR]["approved_by"] == BH_EMAIL


@pytest.mark.parametrize("status", ["", "   ", " Pending ", "On Hold  "])
def test_a_blank_or_padded_undecided_status_is_decided_not_reported_as_already_actioned(status):
    # The fake reads the stored status as PostgreSQL does, so if the guard and the
    # Python check disagreed (COALESCE alone keeps '' as ''), the UPDATE would match
    # no row and this would come back already_actioned.
    conn = FakeConn(header(status=status))
    res = decide(conn, DEPUTY_EMAIL, "reject")
    assert res == {"rtv_id": CR, "status": "Rejected", "already_actioned": False, "decided_by": DEPUTY_EMAIL}
    assert conn.headers[CR]["status"] == "Rejected" and len(conn.updates()) == 1


def test_a_padded_decided_status_stays_decided_and_reads_clean():
    conn = FakeConn(header(status=" Approved "))
    assert decide(conn, BH_EMAIL, "hold") == {"rtv_id": CR, "status": "Approved", "already_actioned": True}
    assert conn.updates() == []


def test_a_return_decided_between_the_check_and_the_write_is_reported_as_already_actioned():
    conn = FakeConn(header())
    conn.before_update = lambda c: c.headers[CR].update(status="Approved", approved_by=DEPUTY_EMAIL)
    res = decide(conn, BH_EMAIL, "reject")
    assert res == {"rtv_id": CR, "status": "Approved", "already_actioned": True}
    assert conn.headers[CR]["approved_by"] == DEPUTY_EMAIL


def test_an_unknown_return_is_404_and_a_bad_action_is_400():
    exc = refusal(FakeConn(), BH_EMAIL)
    assert exc.status_code == 404 and exc.detail["error"] == "customer_return_not_found"
    conn = FakeConn(header())
    exc = refusal(conn, BH_EMAIL, action="approved")
    assert exc.status_code == 400 and exc.detail["error"] == "invalid_action"
    assert conn.updates() == []


# ── WHO: the BH and the standing deputies, nobody else ───────────────────────
@pytest.mark.parametrize("actor", [BH_EMAIL, "  Prashant.Pal@CandorFoods.in ", DEPUTY_EMAIL, DEPUTY2_EMAIL.upper()])
def test_the_business_head_and_each_deputy_can_decide(actor):
    conn = FakeConn(header())
    res = decide(conn, actor)
    assert res["status"] == "Approved" and not res["already_actioned"]
    assert conn.updates()[0][2][2] == actor.strip()


@pytest.mark.parametrize("actor", [ADMIN_EMAIL, "shubham.seth@candorfoods.in", "someone@example.com"])
def test_anyone_else_is_refused_and_told_who_can_decide(actor):
    conn = FakeConn(header())
    exc = refusal(conn, actor)
    assert exc.status_code == 403 and exc.detail["error"] == "not_an_approver"
    assert exc.detail["message"] == (f"Only Prashant Pal, Satyendra Kumar Garg or R M Patil can approve, "
                                     f"reject or hold customer return {CR}.")
    assert [a["email"] for a in exc.detail["details"]["approvers"]] == [BH_EMAIL, DEPUTY_EMAIL, DEPUTY2_EMAIL]
    assert conn.updates() == [] and conn.headers[CR]["status"] == "Pending"


@pytest.mark.parametrize("actor", [None, "", "   "])
def test_an_account_without_an_email_cannot_decide(actor):
    conn = FakeConn(header())
    exc = refusal(conn, actor)
    assert exc.status_code == 403 and exc.detail["error"] == "not_an_approver"
    assert exc.detail["message"].startswith("Your account has no e-mail address")
    assert conn.updates() == []


def test_the_person_who_raised_the_return_never_decides_it_even_as_its_business_head():
    conn = FakeConn(header(created_by=" PRASHANT.PAL@candorfoods.in"))
    exc = refusal(conn, BH_EMAIL)
    assert exc.status_code == 403 and exc.detail["error"] == "own_return"
    assert exc.detail["message"] == (f"You raised customer return {CR}, so you can't decide it yourself. "
                                     f"Satyendra Kumar Garg or R M Patil must decide it.")
    assert conn.updates() == []
    # ... another approver still can.
    assert decide(conn, DEPUTY_EMAIL)["status"] == "Approved"


def test_a_deputy_who_raised_the_return_cannot_decide_it_either():
    conn = FakeConn(header(created_by=DEPUTY_EMAIL))
    exc = refusal(conn, DEPUTY_EMAIL, action="hold")
    assert exc.status_code == 403 and exc.detail["error"] == "own_return"
    assert conn.updates() == []


def test_the_creator_who_is_not_an_approver_is_told_it_is_their_own_return():
    conn = FakeConn(header())
    exc = refusal(conn, CREATOR_EMAIL)
    assert exc.status_code == 403 and exc.detail["error"] == "own_return"


def test_a_sole_approver_who_raised_the_return_is_told_nobody_else_can_decide_it():
    conn = FakeConn(header(created_by=BH_EMAIL), routing=NO_DEPUTIES)
    exc = refusal(conn, BH_EMAIL)
    assert exc.detail["error"] == "own_return"
    assert exc.detail["message"].endswith("There is no other approver for it.")


@pytest.mark.parametrize("bh, why", [("Someone Else", "its Business Head “Someone Else” has no e-mail on file"),
                                     (None, "it has no Business Head")])
def test_no_approver_is_409_when_the_bh_cannot_be_resolved_and_there_are_no_deputies(bh, why):
    conn = FakeConn(header(business_head=bh), routing=NO_DEPUTIES)
    exc = refusal(conn, BH_EMAIL)
    assert exc.status_code == 409 and exc.detail["error"] == "no_approver"
    assert exc.detail["message"] == (f"Nobody can decide customer return {CR}: {why} and no deputy "
                                     f"approver is configured.")
    assert conn.updates() == []


def test_with_an_unmapped_bh_the_deputies_still_decide():
    conn = FakeConn(header(business_head="Someone Else"))
    assert refusal(conn, BH_EMAIL).detail["error"] == "not_an_approver"
    assert decide(conn, DEPUTY_EMAIL)["status"] == "Approved"


def test_a_business_head_namesake_without_the_role_is_not_an_approver():
    users = [*USERS, {"user_id": 30, "full_name": "Prashant Pal", "email": "p.pal@vendor.example",
                      "phone": "9800000030", "is_active": True, "roles": []}]
    conn = FakeConn(header(), users=users)
    assert refusal(conn, "p.pal@vendor.example").detail["error"] == "not_an_approver"


def test_an_unreadable_routing_table_drops_the_deputies_and_says_so(caplog):
    conn = FakeConn(header(), routing_error=True)
    assert refusal(conn, DEPUTY_EMAIL).detail["error"] == "not_an_approver"
    assert decide(conn, BH_EMAIL)["status"] == "Approved"
    assert any("cr_email_routing unreadable" in r.getMessage() for r in caplog.records)


def test_the_read_only_check_applies_the_same_rules_and_writes_nothing():
    conn = FakeConn(header(status="On Hold"))
    cr = asyncio.run(svc.check_cr_action(conn, COMPANY, CR, DEPUTY_EMAIL))
    assert (cr["rtv_id"], cr["status"], cr["decided"], cr["customer"], cr["factory_unit"]) == \
        (CR, "On Hold", False, "Sample Foods Pvt Ltd", "W202")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.check_cr_action(conn, COMPANY, CR, CREATOR_EMAIL))
    assert exc.value.detail["error"] == "own_return"
    conn.headers[CR]["status"] = "Submitted"
    assert asyncio.run(svc.check_cr_action(conn, COMPANY, CR, BH_EMAIL))["decided"] is True
    assert conn.updates() == []


# ── HTTP: the in-app decision as the screen sees it ──────────────────────────
URL = f"/api/v1/customer-returns/{COMPANY}/{CR}/approve"


def test_in_app_the_logged_in_business_head_decides_as_their_email(client, mail, get_cr):
    conn = FakeConn(header())
    res = client(conn, BH).post(URL, json={"action": "reject", "remark": "wrong invoice"})
    assert res.status_code == 200
    assert res.json() == {"rtv_id": CR, "status": "Rejected", "already_actioned": False, "decided_by": BH_EMAIL}
    assert conn.updates()[0][2][2:4] == (BH_EMAIL, "wrong invoice")
    (status_mail,) = mail.sent
    assert f"marked “Rejected” by {BH_EMAIL}. Remark: “wrong invoice”" in status_mail["text"]
    assert status_mail["irt"] == f"<RTV-{CR}@candorfoods.in>"


def test_in_app_a_deputy_decides_too(client, mail, get_cr):
    conn = FakeConn(header(status="On Hold"))
    res = client(conn, DEPUTY).post(URL, json={"action": "approve"})
    assert res.status_code == 200 and res.json()["status"] == "Approved"


@pytest.mark.parametrize("user, error", [(ADMIN, "not_an_approver"), (CREATOR, "own_return"),
                                         (_user(""), "not_an_approver")])
def test_in_app_admins_creators_and_users_without_an_email_are_refused(client, mail, get_cr, user, error):
    conn = FakeConn(header())
    res = client(conn, user).post(URL, json={"action": "approve"})
    assert res.status_code == 403
    body = res.json()
    assert body["error"] == error
    assert "Prashant Pal" in body["message"]          # the message names who can decide
    assert conn.updates() == [] and mail.sent == []


def test_in_app_no_approver_is_409(client, mail, get_cr):
    conn = FakeConn(header(business_head=""), routing=NO_DEPUTIES)
    res = client(conn, BH).post(URL, json={"action": "approve"})
    assert res.status_code == 409 and res.json()["error"] == "no_approver"


@pytest.mark.parametrize("status", ["Approved", "Submitted", "Rejected"])
def test_in_app_a_decided_return_is_reported_and_left_alone(client, mail, get_cr, status):
    conn = FakeConn(header(status=status))
    res = client(conn, BH).post(URL, json={"action": "hold"})
    assert res.status_code == 200 and res.json() == {"rtv_id": CR, "status": status, "already_actioned": True}
    assert conn.updates() == [] and mail.sent == []


def test_in_app_a_failing_status_mail_does_not_fail_a_recorded_decision(client, mail, monkeypatch):
    async def _boom(conn, company, cr_id):
        raise TimeoutError("statement timeout")

    monkeypatch.setattr(query_service, "get_cr", _boom)
    conn = FakeConn(header())
    res = client(conn, BH).post(URL, json={"action": "approve"})
    assert res.status_code == 200 and res.json()["status"] == "Approved"
    assert conn.headers[CR]["status"] == "Approved"


# ── HTTP: the header edit is not a fourth way to decide ─────────────────────
PUT_URL = f"/api/v1/customer-returns/{COMPANY}/{CR}"


def test_the_header_edit_model_has_no_status():
    assert "status" not in schemas.CRHeaderUpdate.model_fields
    assert schemas.CRHeaderUpdate(status="Approved", remark="x").model_dump(exclude_none=True) == {"remark": "x"}


@pytest.mark.parametrize("status", ["Approved", "Rejected", "On Hold", "Pending", ""])
@pytest.mark.parametrize("user", [CREATOR, ADMIN, BH])
def test_a_header_edit_carrying_only_a_status_changes_nothing(client, user, status):
    conn = FakeConn(header())
    res = client(conn, user).put(PUT_URL, json={"status": status})
    assert res.status_code == 400 and res.json()["error"] == "empty_update"
    assert conn.updates() == []
    assert (conn.headers[CR]["status"], conn.headers[CR]["approved_by"]) == ("Pending", None)


def test_a_header_edit_neither_decides_a_pending_return_nor_reopens_a_decided_one(client):
    conn = FakeConn(header())
    res = client(conn, CREATOR).put(PUT_URL, json={"status": "Approved", "remark": "all OK"})
    assert res.status_code == 200
    assert (res.json()["status"], res.json()["remark"]) == ("Pending", "all OK")
    (update,) = conn.updates()
    assert update[1].startswith("UPDATE cfpl_rtv_header SET remark = $1, updated_at = NOW() WHERE")
    assert (conn.headers[CR]["status"], conn.headers[CR]["approved_by"]) == ("Pending", None)

    # A decided return's header can't be edited at all (not even by an admin), so a
    # PUT can't reopen it either.
    conn = FakeConn(header(status="Submitted", approved_by=DEPUTY_EMAIL))
    res = client(conn, ADMIN).put(PUT_URL, json={"status": "Pending", "remark": "re-check"})
    assert res.status_code == 409 and res.json()["error"] == "already_decided"
    assert conn.updates() == []
    assert (conn.headers[CR]["status"], conn.headers[CR]["approved_by"]) == ("Submitted", DEPUTY_EMAIL)
    # ... so it stays decided: a later decision is already actioned, nothing written.
    assert decide(conn, BH_EMAIL) == {"rtv_id": CR, "status": "Submitted", "already_actioned": True}
    assert conn.updates() == []
