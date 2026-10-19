"""Customer-return dates on the read side, against an in-memory fake connection.

On RDS the legacy {p}_rtv_header / _rtv_lines / _rtv_boxes timestamps are
`timestamp without time zone` holding UTC: IMS and the ERP both write NOW() in a
UTC session, so asyncpg hands them back naive. These tests pin that every
timestamp the CR API returns leaves as IST with its offset
("2026-10-19T02:00:00+05:30"), and that the from/to date filters are IST calendar
days, so a return raised at 02:00 IST on 19 Oct (stored 2026-10-18 20:30) is
listed under the 19th, the day its CR id names.

The fake keeps the three tables as dicts and answers each statement by what it
reads: a SELECT returns the columns it names, and the WHERE's date bounds are
applied to the stored naive values the way Postgres compares a naive column with
a naive bound (NULL matches nothing). Any other statement is an AssertionError.

The HTTP half goes through the real router and its auth dependency (the token
round trip and the permission lookup are stubbed one level lower, as
test_transfer_request_decisions does), so the JSON is checked as a client sees it.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_dates_read.py
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.middleware import request_context
from app.modules.auth.middleware import AuthUser
from app.modules.customer_returns import router as R
from app.modules.customer_returns.services import query_service as q
from app.modules.customer_returns.tables import cr_table_names

HID = 7001
CR_ID = "CR-20261019020000"
#: 02:00 IST on 19 Oct 2026, as a naive-UTC column holds it.
RAISED_UTC = datetime(2026, 10, 18, 20, 30)
#: What NOW() stores in a naive column at that instant (the session TimeZone is UTC).
NOW_UTC = RAISED_UTC
HEADER_NAMES = [c.strip() for c in q.HEADER_COLS.split(",")]


def _hdr(**over) -> dict:
    """A _rtv_header row as asyncpg returns it on RDS: naive timestamps holding UTC."""
    row = {
        "id": HID, "rtv_id": CR_ID, "rtv_date": RAISED_UTC,
        "factory_unit": "W202", "customer": "ACME FOODS", "invoice_number": "INV-1",
        "challan_no": None, "dn_no": None, "conversion": Decimal("0"), "sales_poc": None,
        "sales_poc_email": None, "business_head": "bh@candorfoods.in", "remark": None,
        "vehicle_number": None, "transporter_name": None, "driver_name": None,
        "inward_manager": None, "status": "Pending", "created_by": "maker@candorfoods.in",
        "created_ts": RAISED_UTC + timedelta(seconds=5, microseconds=123456),
        "updated_at": None, "approved_by": None, "approved_at": None,
    }
    row.update(over)
    return row


def _selected(h: dict) -> dict:
    """The header row as `SELECT {HEADER_COLS}` returns it."""
    return {c: h[c] for c in HEADER_NAMES}


def _line(**over) -> dict:
    row = {
        "id": 1, "header_id": HID, "item_description": "ALMOND W-320", "material_type": "RM",
        "item_category": "Nuts", "sub_category": "Almond", "sale_group": None, "uom": "KG",
        "qty": 4, "rate": Decimal("10"), "value": Decimal("40"), "conversion": None,
        "net_weight": Decimal("25"), "carton_weight": Decimal("0"), "lot_number": None,
        "item_mark": None, "spl_remarks": None, "vakkal": None,
        "created_at": RAISED_UTC + timedelta(seconds=6), "updated_at": datetime(2026, 10, 19, 6, 0),
    }
    row.update(over)
    return row


def _box(**over) -> dict:
    row = {
        "id": 1, "header_id": HID, "rtv_line_id": 1, "article_description": "ALMOND W-320",
        "box_number": 1, "box_id": "50123456-1", "uom": "KG", "conversion": None,
        "lot_number": "L1", "item_mark": None, "spl_remarks": None, "vakkal": None,
        "net_weight": Decimal("25"), "gross_weight": Decimal("25.6"), "count": 1,
        "created_at": RAISED_UTC + timedelta(minutes=10), "updated_at": None,
    }
    row.update(over)
    return row


# ── Fakes ────────────────────────────────────────────────────────────────────
class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.tx_depth += 1
        return self

    async def __aexit__(self, *exc):
        self.conn.tx_depth -= 1
        return False


def _project(columns: str, **rows) -> dict:
    """A SELECT list ('h.rtv_id AS rtv_id, l.qty, b.count AS box_count') resolved
    against the joined rows; a LEFT JOIN miss (None) gives NULLs. A column the
    table does not have is a KeyError."""
    out = {}
    for item in columns.split(","):
        expr, _, alias = item.strip().partition(" AS ")
        table, _, column = expr.partition(".")
        src = rows[table]
        out[alias or column] = None if src is None else src[column]
    return out


class FakeConn:
    """One company's _rtv_header / _rtv_lines / _rtv_boxes, answered by statement."""

    def __init__(self, *headers, lines=(), boxes=(), company="CFPL", now=NOW_UTC):
        self.t = cr_table_names(company)
        self.headers = [dict(h) for h in headers]
        self.lines = [dict(l) for l in lines]
        self.boxes = [dict(b) for b in boxes]
        self.now = now
        self.tx_depth = 0
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return _Tx(self)

    def _log(self, kind, sql, args):
        s = " ".join(sql.split())
        self.calls.append((kind, s, args))
        return s

    def _header(self, rtv_id):
        return next((h for h in self.headers if h["rtv_id"] == rtv_id), None)

    def _dated(self, s: str, args: tuple) -> list:
        """The headers the statement's WHERE keeps. Only the IST day bounds are
        supported; each is bound as a naive datetime (asyncpg encodes a ::timestamp
        parameter from one) and compared with the stored naive-UTC rtv_date."""
        where = s.rsplit(" WHERE ", 1)[1].split(" ORDER BY ")[0]
        terms = where.split(" AND ")
        assert terms[0] == "1=1", where
        kept = list(self.headers)
        for term in terms[1:]:
            m = re.fullmatch(r"h\.rtv_date (>=|<) \$(\d+)::timestamp", term)
            if not m:
                raise AssertionError(f"unexpected filter: {term}")
            bound = args[int(m[2]) - 1]
            assert isinstance(bound, datetime) and bound.tzinfo is None, bound
            if m[1] == ">=":
                kept = [h for h in kept if h["rtv_date"] is not None and h["rtv_date"] >= bound]
            else:
                kept = [h for h in kept if h["rtv_date"] is not None and h["rtv_date"] < bound]
        return kept

    def _insert_header(self, s: str, args: tuple) -> dict:
        """Store the row the INSERT describes. NOW() is what a UTC session writes into
        a naive column; a column the INSERT leaves out is NULL (no legacy default)."""
        m = re.fullmatch(r"INSERT INTO \S+ \((.+?)\) VALUES \((.+?)\) RETURNING id", s)
        assert m, s
        cols = [c.strip() for c in m[1].split(",")]
        vals = [v.strip() for v in m[2].split(",")]
        assert len(cols) == len(vals), (cols, vals)
        row = {c: None for c in HEADER_NAMES}
        row.update(id=HID + len(self.headers), approved_by=None, approved_at=None)
        for col, val in zip(cols, vals):
            row[col] = (self.now if val == "NOW()"
                        else args[int(val[1:]) - 1] if val.startswith("$")
                        else val.strip("'"))
        self.headers.append(row)
        return {"id": row["id"]}

    async def fetchrow(self, sql, *args):
        s = self._log("fetchrow", sql, args)
        if s == f"SELECT {q.HEADER_COLS} FROM {self.t['header']} WHERE rtv_id = $1":
            h = self._header(args[0])
            return None if h is None else _selected(h)
        if s.startswith(f"INSERT INTO {self.t['header']} "):
            return self._insert_header(s, args)
        raise AssertionError(f"unexpected fetchrow: {s[:100]}")

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        t = self.t
        if s == (f"SELECT {q._RTV_LINE_SELECT} FROM {t['lines']} l JOIN {t['header']} h "
                 "ON l.header_id = h.id WHERE h.rtv_id = $1 ORDER BY l.item_description"):
            h = self._header(args[0])
            return [_project(q._RTV_LINE_SELECT, h=h, l=l)
                    for l in self.lines if h and l["header_id"] == h["id"]]
        if s == (f"SELECT {q._RTV_BOX_SELECT} FROM {t['boxes']} b JOIN {t['header']} h "
                 "ON b.header_id = h.id WHERE h.rtv_id = $1 ORDER BY b.article_description, b.box_number"):
            h = self._header(args[0])
            return [_project(q._RTV_BOX_SELECT, h=h, b=b)
                    for b in self.boxes if h and b["header_id"] == h["id"]]
        if s.startswith(f"SELECT {q.HEADER_COLS}, (SELECT COUNT(*) FROM {t['lines']} l "):
            rows = []
            for h in self._dated(s, args):
                ls = [l for l in self.lines if l["header_id"] == h["id"]]
                bs = [b for b in self.boxes if b["header_id"] == h["id"]]
                rows.append({**_selected(h),
                             "items_count": len(ls), "boxes_count": len(bs),
                             "total_qty": sum(l["qty"] for l in ls),
                             "total_net_weight": sum(b["net_weight"] or 0 for b in bs)})
            return rows
        if s.startswith("SELECT h.rtv_id, h.rtv_date,") and f" FROM {t['header']} h LEFT JOIN " in s:
            cols = s[len("SELECT "):s.index(" FROM ")]
            rows = []
            for h in self._dated(s, args):
                for l in [l for l in self.lines if l["header_id"] == h["id"]] or [None]:
                    boxes = [b for b in self.boxes
                             if l is not None and b["header_id"] == h["id"]
                             and b["article_description"] == l["item_description"]
                             and b["rtv_line_id"] in (l["id"], None)] or [None]
                    rows += [_project(cols, h=h, l=l, b=b) for b in boxes]
            return rows
        if s == "SELECT box_id, field_name FROM box_edit_logs WHERE transaction_no = ANY($1)":
            return []
        raise AssertionError(f"unexpected fetch: {s[:100]}")

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        if s.startswith(f"SELECT COUNT(*) FROM {self.t['header']} h WHERE "):
            return len(self._dated(s, args))
        raise AssertionError(f"unexpected fetchval: {s[:100]}")

    async def execute(self, sql, *args):
        s = self._log("execute", sql, args)
        m = re.fullmatch(rf"INSERT INTO {self.t['lines']} \((.+?)\) VALUES \((.+?)\)", s)
        if not m:
            raise AssertionError(f"unexpected execute: {s[:100]}")
        row = dict(zip([c.strip() for c in m[1].split(",")], args))
        row.update(id=len(self.lines) + 1, created_at=None, updated_at=None)
        self.lines.append(row)
        return "INSERT 0 1"


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


MAKER = _user("maker@candorfoods.in")


@pytest.fixture
def client(monkeypatch):
    """(conn, user, allowed) -> TestClient on the real CR router behind the real auth
    dependency. `client.asked` lists any permission a route checked."""
    import app.modules.auth.services.auth_service as auth_service
    import app.modules.auth.services.permission_service as permission_service

    asked: list[tuple] = []

    def _make(conn, user=MAKER, *, allowed=True):
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


def _list(conn, **filters) -> dict:
    return asyncio.run(q.list_crs(conn, company="CFPL", page=1, per_page=50, **filters))


# ── _ist: one conversion for every stored form ───────────────────────────────
def test_a_naive_timestamp_is_read_as_utc_and_given_back_in_ist():
    t = q._ist(RAISED_UTC)
    assert t.utcoffset() == timedelta(hours=5, minutes=30)
    assert t.isoformat() == "2026-10-19T02:00:00+05:30"
    assert t == RAISED_UTC.replace(tzinfo=timezone.utc)          # the same instant


def test_an_aware_timestamp_is_converted_from_its_own_zone():
    as_utc = datetime(2026, 10, 18, 20, 30, tzinfo=timezone.utc)   # a timestamptz column
    in_tokyo = datetime(2026, 10, 19, 5, 30, tzinfo=timezone(timedelta(hours=9)))
    already_ist = datetime(2026, 10, 19, 2, 0, tzinfo=q.IST)
    for v in (as_utc, in_tokyo, already_ist):
        assert q._ist(v).isoformat() == "2026-10-19T02:00:00+05:30", v


def test_ims_text_is_parsed_and_unreadable_text_is_kept():
    # IMS's approved_at: datetime.utcnow() as TEXT, with or without microseconds.
    assert q._ist("2026-10-18 20:30:00").isoformat() == "2026-10-19T02:00:00+05:30"
    assert q._ist("2026-10-18 20:30:00.250000").isoformat() == "2026-10-19T02:00:00.250000+05:30"
    # NOW() written into a text column renders with the session's +00 offset.
    assert q._ist("2026-10-18 20:30:00+00").isoformat() == "2026-10-19T02:00:00+05:30"
    for raw in ("not a date", "19/10/2026 02:00", ""):
        assert q._ist(raw) == raw
    assert q._ist(None) is None
    assert q._ist_text("2026-10-18 20:30:00") == "2026-10-19T02:00:00+05:30"
    assert q._ist_text("not a date") == "not a date" and q._ist_text(None) is None


def test_a_bare_date_is_that_ist_calendar_day():
    assert q._ist(date(2026, 10, 19)).isoformat() == "2026-10-19T00:00:00+05:30"


# ── the row mappers ──────────────────────────────────────────────────────────
def test_every_mapper_returns_its_timestamps_in_ist():
    h = q._map_header_row(_selected(_hdr(updated_at=datetime(2026, 10, 19, 6, 0))))
    assert h["rtv_date"].isoformat() == "2026-10-19T02:00:00+05:30"
    assert h["created_ts"].isoformat() == "2026-10-19T02:00:05.123456+05:30"
    assert h["updated_at"].isoformat() == "2026-10-19T11:30:00+05:30"
    assert "approved_at" not in h                        # not selected, so not returned
    assert q._map_header_row(_selected(_hdr(rtv_date=None)))["rtv_date"] is None

    line = q._map_line_row(_project(q._RTV_LINE_SELECT, h=_hdr(), l=_line()))
    assert line["created_at"].isoformat() == "2026-10-19T02:00:06+05:30"
    assert line["updated_at"].isoformat() == "2026-10-19T11:30:00+05:30"
    box = q._map_box_row(_project(q._RTV_BOX_SELECT, h=_hdr(), b=_box()))
    assert box["created_at"].isoformat() == "2026-10-19T02:10:00+05:30" and box["updated_at"] is None


def test_approved_at_when_selected_is_ist_text():
    assert q._map_header_row(_hdr(approved_at="2026-10-19 04:00:00"))["approved_at"] == "2026-10-19T09:30:00+05:30"
    assert q._map_header_row(_hdr(approved_at=datetime(2026, 10, 19, 4, 0)))["approved_at"] == "2026-10-19T09:30:00+05:30"
    assert q._map_header_row(_hdr(approved_at="garbled"))["approved_at"] == "garbled"
    assert q._map_header_row(_hdr(approved_at=None))["approved_at"] is None


# ── IST calendar days in the filters ─────────────────────────────────────────
def test_an_ist_day_starts_at_1830_utc_the_evening_before():
    assert q._ist_day_start_utc(date(2026, 10, 19)) == datetime(2026, 10, 18, 18, 30)
    assert q._ist_day_start_utc(date(2026, 10, 20)) == datetime(2026, 10, 19, 18, 30)
    assert q._ist_day_start_utc(date(2027, 1, 1)) == datetime(2026, 12, 31, 18, 30)


@pytest.mark.parametrize("read", ["list", "export"])
def test_the_filter_binds_the_utc_instants_of_ist_midnight(read):
    conn = FakeConn(_hdr())
    if read == "list":
        _list(conn, from_date="19-10-2026", to_date="19-10-2026")
        reads = [c for c in conn.calls if c[0] in ("fetchval", "fetch")]
        assert len(reads) == 2                                # the count and the page
    else:
        asyncio.run(q.export_cr_records(conn, company="CFPL", from_date="19-10-2026", to_date="19-10-2026"))
        reads = [c for c in conn.calls if c[0] == "fetch"]
    for _kind, sql, args in reads:
        assert "h.rtv_date >= $1::timestamp AND h.rtv_date < $2::timestamp" in sql
        assert "::date" not in sql
        assert args[:2] == (datetime(2026, 10, 18, 18, 30), datetime(2026, 10, 19, 18, 30))
        assert all(a.tzinfo is None for a in args[:2])


def test_a_night_shift_return_is_listed_on_the_ist_day_its_id_names():
    stored = {                                     # naive UTC, as the _rtv_ columns hold it
        "CR-20261018235900": datetime(2026, 10, 18, 18, 29),       # 23:59 IST, 18 Oct
        "CR-20261019000000": datetime(2026, 10, 18, 18, 30),       # 00:00 IST, 19 Oct
        "CR-20261019020000": datetime(2026, 10, 18, 20, 30),       # 02:00 IST, 19 Oct
        "CR-20261019235959": datetime(2026, 10, 19, 18, 29, 59),   # 23:59:59 IST, 19 Oct
        "CR-20261020000000": datetime(2026, 10, 19, 18, 30),       # 00:00 IST, 20 Oct
        "CR-UNDATED": None,
    }
    conn = FakeConn(*[_hdr(id=HID + n, rtv_id=k, rtv_date=v) for n, (k, v) in enumerate(stored.items())])

    def ids(**f):
        return [r["rtv_id"] for r in _list(conn, **f)["records"]]

    on_19 = ["CR-20261019000000", "CR-20261019020000", "CR-20261019235959"]
    assert ids(from_date="19-10-2026", to_date="19-10-2026") == on_19
    assert _list(conn, from_date="19-10-2026", to_date="19-10-2026")["total"] == 3
    assert ids(to_date="18-10-2026") == ["CR-20261018235900"]
    assert ids(from_date="19-10-2026") == on_19 + ["CR-20261020000000"]
    assert "CR-UNDATED" not in ids(from_date="01-01-2000")      # NULL never matches a bound
    assert "CR-UNDATED" in ids()                                # and no filter keeps it


def test_a_malformed_date_is_still_a_400():
    with pytest.raises(HTTPException) as e:
        _list(FakeConn(), from_date="2026-10-19")
    assert e.value.status_code == 400 and e.value.detail["error"] == "invalid_date"


#: Days that parse as DD-MM-YYYY but whose bound is no Python datetime: IST midnight
#: on 1 Jan 0001 is in year 0 UTC, and the day after 31 Dec 9999 is in year 10000.
OUT_OF_RANGE = [({"from_date": "01-01-0001"}, "01-01-0001"),
                ({"to_date": "31-12-9999"}, "31-12-9999"),
                ({"from_date": "19-10-2026", "to_date": "31-12-9999"}, "31-12-9999")]


@pytest.mark.parametrize("read", ["list", "export"])
@pytest.mark.parametrize("filters, bad", OUT_OF_RANGE)
def test_a_day_with_no_bound_in_range_is_a_400_not_a_500(read, filters, bad):
    conn = FakeConn(_hdr())
    with pytest.raises(HTTPException) as e:
        if read == "list":
            _list(conn, **filters)
        else:
            asyncio.run(q.export_cr_records(conn, company="CFPL", **filters))
    assert e.value.status_code == 400
    assert e.value.detail == {"error": "invalid_date", "message": "date out of range",
                              "details": {"value": bad}}
    assert conn.calls == []                                     # refused before any query


def test_the_last_days_that_have_a_bound_still_filter():
    clauses, args = ["1=1"], []
    q._add_date_filters(clauses, args, "31-12-9999", "01-01-0001")
    assert clauses[1:] == ["h.rtv_date >= $1::timestamp", "h.rtv_date < $2::timestamp"]
    assert args[0] == datetime(9999, 12, 30, 18, 30)
    assert isinstance(args[1], datetime) and args[1].tzinfo is None and args[1].year == 1


# ── HTTP: what a client receives ─────────────────────────────────────────────
@pytest.mark.parametrize("path, params", [("/api/v1/customer-returns/CFPL", {}),
                                          ("/api/v1/customer-returns/export", {"company": "CFPL"})])
def test_the_list_and_export_answer_an_out_of_range_day_with_a_400(client, path, params):
    for filters, bad in OUT_OF_RANGE:
        res = client(FakeConn(_hdr())).get(path, params={**params, **filters})
        assert res.status_code == 400, res.text
        body = res.json()
        assert (body["error"], body["message"], body["details"]) == ("invalid_date", "date out of range", {"value": bad})


def test_the_detail_json_carries_ist_offsets(client):
    conn = FakeConn(_hdr(), lines=[_line()], boxes=[_box()])
    res = client(conn).get(f"/api/v1/customer-returns/CFPL/{CR_ID}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rtv_date"] == "2026-10-19T02:00:00+05:30"
    assert body["created_ts"] == "2026-10-19T02:00:05.123456+05:30"
    assert body["updated_at"] is None
    assert body["lines"][0]["created_at"] == "2026-10-19T02:00:06+05:30"
    assert body["lines"][0]["updated_at"] == "2026-10-19T11:30:00+05:30"
    assert body["boxes"][0]["created_at"] == "2026-10-19T02:10:00+05:30"


def test_the_list_json_carries_ist_offsets_and_filters_on_ist_days(client):
    conn = FakeConn(_hdr(), _hdr(id=HID + 1, rtv_id="CR-20261018235900", rtv_date=datetime(2026, 10, 18, 18, 29)))
    res = client(conn).get("/api/v1/customer-returns/CFPL",
                           params={"from_date": "19-10-2026", "to_date": "19-10-2026"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 1 and [r["rtv_id"] for r in body["records"]] == [CR_ID]
    assert body["records"][0]["rtv_date"] == "2026-10-19T02:00:00+05:30"
    assert body["records"][0]["created_ts"] == "2026-10-19T02:00:05.123456+05:30"
