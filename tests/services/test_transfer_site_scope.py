"""Sites in the transfer module's lists, scopes, filters, locks and detail reads: every
spelling of a site is that site (app.core.sites), 'Cold Storage' and 'Savla Bond' are
sites of their own, and a warehouse-locked user reads only rows of their sites.

What is pinned:
  - the profile locks (permissions) compare sites, and their 403s name canonical sites;
    in_scope (the detail reads' rule) counts each unit of a from_cold_unit;
  - a request or transfer from a site to itself, in any two spellings, is refused (422),
    and so is one without a From, a To or both (blank, or no letter or digit in it);
    a new request stores its sites by their canonical names (D5);
  - the scope and filter SQL of list_requests / list_transfers / list_transfer_ins /
    list_pending_transfers / pending_by_lot / the dashboard: site_scope_sql bound to
    sites.scope_keys, evaluated here as Postgres would (both sides normalised); a cold
    dispatch is in a unit's scope, and found by a unit's filter, through each unit of
    its from_cold_unit; the Transfer In list scopes on the transfer's To and units too,
    and returns from_cold_unit;
  - the cold-unit canonicaliser (from the site table: 'Supreme', not 'Supreme Cold');
    the transfer's box rows join only their own in-transit rows (P2-09), source_unit
    and lot_origin_unit are canonical names;
  - the in-transit list: records and chips by canonical site, deduplicated, the From /
    To filters by site, storage types from the site table; the '+N in transit' read
    counts only transfers that exist and are not Received (P2-14);
  - X-04: the detail reads (transfer, request, receipt, receipt by transfer, in transit
    by lot, inner cold) 404 a row outside a locked user's sites and show it to its
    From's, To's and cold units' users; X-05: PUT /transfers checks the stored From;
    the receipt edit is locked to the transfer's To and stores its canonical name;
  - the ERP's test-DB stub of warehouse_sites has the columns the dropdown selects.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_site_scope.py
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.middleware.request_context import AuthError
from app.core.sites import scope_keys, site_key
from app.core.warehouse_scope import WAREHOUSE_NORM_ANY_SQL
from app.modules.transfer import permissions, schemas
from app.modules.transfer.services import (
    create_service,
    dashboard_service,
    inner_cold_service,
    pending_service,
    query_service,
    receive_service,
    request_service,
)
from tests.services import test_transfer_request_decisions as trd
from tests.services.test_transfer_request_decisions import client  # noqa: F401  (the HTTP fixture)

URL = "/api/v1/transfer"
ROOT = Path(__file__).resolve().parents[2]
TRANSFER = ROOT / "app" / "modules" / "transfer"
D39 = trd._user("store.d39@candorfoods.in", warehouses=["D-39"])           # the grant code of Savla D-39
A185 = trd._user("store.a185@candorfoods.in", warehouses=["A185"])
RISHI = trd._user("store.rishi@candorfoods.in", warehouses=["Rishi"])
ADMIN = trd._user("admin@candorfoods.in", is_admin=True, warehouses=["W202"])
ANYONE = trd._user("anyone@candorfoods.in")


# ── site_scope_sql, evaluated as Postgres would ──────────────────────────────
_NORM = re.compile(r"regexp_replace\(upper\(coalesce\(([\w.]+), ''\)\), .+ FROM unnest\((\$\d+)::text\[\]\) AS w\)")
_UNITS = re.compile(r"EXISTS \(SELECT 1 FROM unnest\(string_to_array\(([\w.]+), ','\)\) AS cu\(unit\) WHERE (.+)\)")


def _norm_any(part: str) -> tuple[str, int]:
    m = _NORM.fullmatch(part)
    assert m and part == WAREHOUSE_NORM_ANY_SQL(m[1], m[2]), part
    return m[1], int(m[2][1:])


def _eval(clause: str, row: dict, args) -> bool:
    """A site_scope_sql clause on `row` ({column: value}): a column of one site, or a unit
    of a comma list (string_to_array / unnest), normalised and compared with the
    normalised keys bound at the clause's parameter."""
    assert clause.startswith("(") and clause.endswith(")")
    for part in clause[1:-1].split(" OR "):
        m = _UNITS.fullmatch(part)
        if m:
            col, (inner, n) = m[1], _norm_any(m[2])
            assert inner == "cu.unit"
            values = [] if row[col] is None else row[col].split(",")
        else:
            col, n = _norm_any(part)
            values = [row[col]]
        keys = {site_key(k) for k in args[n - 1]}
        if any(site_key(v) in keys for v in values):
            return True
    return False


def test_site_scope_sql_matches_every_spelling_and_each_cold_unit():
    clause = query_service.site_scope_sql("$1", sites=("h.from_site", "h.to_site"), units=("h.from_cold_unit",))
    args = (scope_keys(["D-39"]),)

    def row(frm, to="W202", units=None):
        return {"h.from_site": frm, "h.to_site": to, "h.from_cold_unit": units}

    for stored in ("Savla D-39", "savla-d39", "Old Savla", "Savla D-39 Cold Storage", "D-39"):
        assert _eval(clause, row(stored), args), stored
    assert _eval(clause, row("W202", "Savla D-39 Cold"), args)
    assert _eval(clause, row("Cold Storage", units="Rishi, Savla D-39"), args)
    for frm, units in (("Savla Bond", None), ("Savla D-514", None), ("Cold Storage", "Rishi"),
                       ("Savla", None), ("Cold Storage", None)):
        assert not _eval(clause, row(frm, units=units), args), (frm, units)
    # 'Cold Storage' is a site of its own: a Rishi scope never takes it, nor it a unit.
    assert not _eval(clause, row("Cold Storage"), (scope_keys(["Rishi"]),))
    assert not _eval(clause, row("W202", units="Rishi"), (scope_keys(["Cold Storage"]),))


# ── The profile locks ────────────────────────────────────────────────────────
@pytest.mark.parametrize("user, site", [
    (D39, "Savla D-39"), (D39, "Old Savla"), (D39, "savla d39 cold"),
    (A185, "Warehouse A185"), (A185, "A-185"),
    (trd._user("s@candorfoods.in", warehouses=["Supreme"]), "Supreme Cold"),
    (trd._user("e@candorfoods.in", warehouses=["Eskimo"]), "ESKIMO"),
    (trd._user("b@candorfoods.in", warehouses=["Savla Bond"]), "Savla Bond"),
])
def test_a_lock_takes_every_spelling_of_its_site(user, site):
    permissions.assert_can_dispatch_from(user, site)
    permissions.assert_can_receive_into(user, site)
    permissions.assert_can_answer_request_from(user, site)
    permissions.assert_can_work_in_cold_store(user, site)


@pytest.mark.parametrize("user, site", [
    (D39, "Savla Bond"), (D39, "Savla D-514"), (D39, "Savla"), (RISHI, "Cold Storage"),
    (trd._user("c@candorfoods.in", warehouses=["Cold Storage"]), "Rishi"), (A185, "W202"), (A185, ""),
])
def test_a_lock_refuses_other_sites_and_names_them_by_their_canonical_names(user, site):
    named = {"Savla Bond": "Savla Bond", "Savla D-514": "Savla D-514", "Savla": "Savla",
             "Cold Storage": "Cold Storage", "Rishi": "Rishi", "W202": "W202", "": "—"}[site]
    mine = {"D-39": "Savla D-39"}.get(user.allowed_warehouses[0], user.allowed_warehouses[0])
    cases = {
        permissions.assert_can_dispatch_from:
            f"Transfer OUT must originate from your assigned warehouse ({mine}); this transfer is from {named}.",
        permissions.assert_can_receive_into:
            f"Transfer IN must be received into your assigned warehouse ({mine}); this transfer is to {named}.",
        permissions.assert_can_answer_request_from:
            "Only the store of the supplying warehouse can accept or reject this request: it is to be sent "
            f"from {named}, and you are assigned to {mine}.",
        permissions.assert_can_work_in_cold_store:
            f"An inner cold transfer can only be made in your assigned warehouse ({mine}); this stock is at {named}.",
    }
    for check, message in cases.items():
        with pytest.raises(AuthError) as exc:
            check(user, site)
        assert (exc.value.status_code, exc.value.message) == (403, message)


def test_a_refusal_lists_the_users_sites_once_each_by_canonical_name():
    user = trd._user("x@candorfoods.in", warehouses=["D-39", "Savla D-39 Cold", "a-185", " "])
    with pytest.raises(AuthError) as exc:
        permissions.assert_can_dispatch_from(user, "Supreme Cold")
    assert exc.value.message == ("Transfer OUT must originate from your assigned warehouse (Savla D-39, A185); "
                                 "this transfer is from Supreme.")


@pytest.mark.parametrize("user", [ADMIN, ANYONE], ids=["admin", "no-lock"])
def test_admins_and_users_without_a_lock_are_not_locked(user):
    for check in (permissions.assert_can_dispatch_from, permissions.assert_can_receive_into,
                  permissions.assert_can_answer_request_from, permissions.assert_can_work_in_cold_store):
        check(user, "Rishi")
    assert permissions.has_warehouse_lock(user) is False and permissions.has_warehouse_lock(D39) is True


def test_in_scope():
    assert permissions.in_scope([], "W202") and permissions.in_scope(None)
    assert permissions.in_scope(["D-39"], "W202", "Old Savla")
    assert permissions.in_scope(["Rishi"], "Cold Storage", "W202", "Supreme, Rishi Cold")
    assert not permissions.in_scope(["Rishi"], "Cold Storage", "W202", None)
    assert not permissions.in_scope(["D-39"], "Savla Bond", "", None)


# ── One site twice is no route ───────────────────────────────────────────────
SAME = [("A185", "Warehouse A185"), ("A185", "A-185"), ("Supreme", "Supreme Cold"), ("D-39", "Savla D-39"),
        ("A185", "A185 Cold"), ("ESKIMO", "Eskimo Cold"), ("Savla D-34", "Old Savla")]
TWO = [("Cold Storage", "Rishi"), ("Savla Bond", "Savla D-39"), ("A185", "W202"), ("Savla", "Savla D-39"),
       ("Dev Int", "W202")]


def _header(frm, to):
    return schemas.TransferHeaderCreate(stock_trf_date="16-10-2026", from_warehouse=frm, to_warehouse=to,
                                        vehicle_no="MH43BP6885")


def _form(frm, to):
    return schemas.FormDataCreate(request_date="16-10-2026", from_warehouse=frm, to_warehouse=to,
                                  reason_description="stock")


@pytest.mark.parametrize("make", [_header, _form], ids=["transfer", "request"])
@pytest.mark.parametrize("frm, to", SAME)
def test_a_route_from_a_site_to_itself_is_refused(make, frm, to):
    with pytest.raises(ValidationError) as exc:
        make(frm, to)
    assert "From warehouse and to warehouse must be different (both are " in str(exc.value)


@pytest.mark.parametrize("make", [_header, _form], ids=["transfer", "request"])
@pytest.mark.parametrize("frm, to", TWO)
def test_two_sites_are_a_route(make, frm, to):
    assert make(frm, to).from_warehouse == frm


# same_site never matches a blank leg, so the same-site refusal alone would let these through.
BLANK = [("", "W202", "From warehouse is required"), ("   ", "Rishi", "From warehouse is required"),
         ("A185", "", "To warehouse is required"), ("Savla D-39", " - ", "To warehouse is required"),
         ("", "", "From warehouse and to warehouse are required"),
         (" ", "\t", "From warehouse and to warehouse are required")]


@pytest.mark.parametrize("make", [_header, _form], ids=["transfer", "request"])
@pytest.mark.parametrize("frm, to, message", BLANK)
def test_a_route_without_a_from_or_a_to_is_refused(make, frm, to, message):
    with pytest.raises(ValidationError) as exc:
        make(frm, to)
    assert message in str(exc.value) and "must be different" not in str(exc.value)


def test_a_new_request_stores_its_sites_by_their_canonical_names():
    body = schemas.RequestCreate(
        form_data=_form("ESKIMO", "D-39"),
        article_data=[schemas.ArticleDataCreate(material_type="RM", item_category="NUTS", sub_category="PISTA",
                                                item_description="PISTA", quantity="2", pack_size="25")])
    conn = trd.FakeConn()
    asyncio.run(request_service.create_request(conn, body, "floor@candorfoods.in"))
    (insert,) = [a for k, s, a in conn.calls if s.startswith("INSERT INTO interunit_transfer_requests")]
    assert insert[2:4] == ("Eskimo", "Savla D-39")

    body.form_data = _form(" Store X ", "Warehouse A185")
    conn = trd.FakeConn()
    asyncio.run(request_service.create_request(conn, body, "floor@candorfoods.in"))
    (insert,) = [a for k, s, a in conn.calls if s.startswith("INSERT INTO interunit_transfer_requests")]
    assert insert[2:4] == ("Store X", "A185")       # an unknown site stays itself, trimmed


# ── The cold-unit canonicaliser ──────────────────────────────────────────────
@pytest.mark.parametrize("raw, unit", [
    ("D-39", "Savla D-39"), ("savla d39 cold", "Savla D-39"), ("OLD SAVLA", "Savla D-39"),
    ("New Savla", "Savla D-514"), ("Savla Bond", "Savla Bond"), ("Rishi Cold Storage", "Rishi"),
    ("Supreme Cold", "Supreme"), ("supreme", "Supreme"), ("ESKIMO", "Eskimo"), ("Eskimo Cold", "Eskimo"),
    ("Cold Storage", None), ("Savla", None), ("A185", None), ("A185 Cold", None), ("", None), (None, None),
])
def test_the_cold_unit_canonicaliser_is_the_site_tables(raw, unit):
    assert query_service._normalize_cold_unit(raw) == unit


# ── The list reads ───────────────────────────────────────────────────────────
class _Capture:
    """Records each statement and its arguments; counts are 0 and reads `rows`."""

    def __init__(self, rows=()):
        self.rows = [dict(r) for r in rows]
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return len(self.rows)

    async def fetch(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return self.rows

    async def fetchrow(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return None


def _ws(s: str) -> str:
    return " ".join(s.split())


def _where(conn, needle) -> tuple[str, tuple]:
    sql, args = next((s, a) for s, a in conn.calls if needle in s)
    return sql, args


def test_list_requests_scopes_and_filters_by_every_spelling():
    conn = _Capture()
    asyncio.run(query_service.list_requests(conn, page=1, per_page=15, from_warehouse="Warehouse A185",
                                            to_warehouse="savla d39", scope=["D-39", "A185"]))
    sql, args = _where(conn, "SELECT COUNT(*) FROM interunit_transfer_requests")
    frm = query_service.site_scope_sql("$1", sites=("r.from_site",))
    to = query_service.site_scope_sql("$2", sites=("r.to_site",))
    scope = query_service.site_scope_sql("$3", sites=("r.from_site", "r.to_site"))
    assert all(_ws(c) in sql for c in (frm, to, scope)) and "REPLACE(LOWER" not in sql
    assert args == (scope_keys("A185"), scope_keys("Savla D-39"), scope_keys(["D-39", "A185"]))
    assert _eval(frm, {"r.from_site": "A-185"}, args) and not _eval(frm, {"r.from_site": "A101"}, args)
    assert _eval(to, {"r.to_site": "Old Savla"}, args) and not _eval(to, {"r.to_site": "Savla Bond"}, args)
    assert _eval(scope, {"r.from_site": "W202", "r.to_site": "Warehouse A185"}, args)
    assert not _eval(scope, {"r.from_site": "W202", "r.to_site": "Savla Bond"}, args)


def test_list_transfers_finds_a_cold_dispatch_by_each_of_its_units():
    conn = _Capture()
    asyncio.run(query_service.list_transfers(conn, page=1, per_page=15, from_site="Supreme", to_site="A-185",
                                             scope=["Rishi"]))
    sql, args = _where(conn, "SELECT COUNT(*) FROM interunit_transfers_header h")
    frm = query_service.site_scope_sql("$1", sites=("h.from_site",), units=("h.from_cold_unit",))
    to = query_service.site_scope_sql("$2", sites=("h.to_site",))
    scope = query_service.site_scope_sql("$3", sites=("h.from_site", "h.to_site"), units=("h.from_cold_unit",))
    assert all(_ws(c) in sql for c in (frm, to, scope)) and "ILIKE" not in sql
    assert args == (scope_keys("Supreme"), scope_keys("A185"), scope_keys(["Rishi"]))

    def row(frm, units=None, to="A185"):
        return {"h.from_site": frm, "h.from_cold_unit": units, "h.to_site": to}

    # The Supreme filter: from Supreme itself, or a cold dispatch with Supreme boxes, old
    # rows' 'Supreme Cold' included; not another unit's, nor 'Cold Storage' by itself.
    assert _eval(frm, row("Supreme"), args) and _eval(frm, row("Supreme Cold"), args)
    assert _eval(frm, row("Cold Storage", "Rishi, Supreme Cold"), args)
    assert not _eval(frm, row("Cold Storage", "Rishi"), args) and not _eval(frm, row("Cold Storage"), args)
    assert _eval(to, row("W202", to="Warehouse A185"), args)
    # The Rishi lock: a dispatch from Rishi, to it, or with Rishi boxes ('Rishi, Savla D-39').
    assert _eval(scope, row("Cold Storage", "Rishi, Savla D-39"), args)
    assert _eval(scope, row("W202", to="Rishi Cold Storage Pvt Ltd"), args)
    assert not _eval(scope, row("Cold Storage", "Savla D-39"), args)


def test_list_transfer_ins_scopes_on_the_receipt_and_the_whole_transfer_and_returns_its_units():
    rec = {"id": 7, "transfer_out_id": 5, "transfer_out_no": "TRANS-5", "grn_number": "GRN-5", "grn_date": None,
           "receiving_warehouse": "A185", "received_by": "u", "received_at": None, "box_condition": "Good",
           "condition_remarks": None, "status": "Received", "created_at": None, "updated_at": None,
           "total_boxes_scanned": 3, "from_warehouse": "Cold Storage", "from_cold_unit": "Rishi"}
    conn = _Capture([rec])
    out = asyncio.run(query_service.list_transfer_ins(conn, page=1, per_page=15, receiving_warehouse="a-185",
                                                      scope=["Rishi"]))
    sql, args = _where(conn, "SELECT COUNT(*) FROM interunit_transfer_in_header h")
    recv = query_service.site_scope_sql("$1", sites=("h.receiving_warehouse",))
    scope = query_service.site_scope_sql("$2", sites=("h.receiving_warehouse", "t.from_site", "t.to_site"),
                                         units=("t.from_cold_unit",))
    assert _ws(recv) in sql and _ws(scope) in sql and args == (scope_keys("A185"), scope_keys(["Rishi"]))
    assert _eval(scope, {"h.receiving_warehouse": "A185", "t.from_site": "Cold Storage", "t.to_site": "A185",
                         "t.from_cold_unit": "Rishi"}, args)
    assert _eval(scope, {"h.receiving_warehouse": "W202", "t.from_site": "A185", "t.to_site": "Rishi Cold",
                         "t.from_cold_unit": None}, args)
    (listed,) = out["records"]
    assert listed["from_cold_unit"] == "Rishi" and schemas.TransferInListItem(**listed).from_cold_unit == "Rishi"
    select = _where(conn, "t.from_site AS from_warehouse, t.from_cold_unit")[0]
    assert "GROUP BY h.id, t.from_site, t.from_cold_unit" in select


def test_no_list_folds_sites_with_the_old_hyphen_rule_or_keeps_an_alias_table_of_its_own():
    for name in ("query_service", "pending_service", "dashboard_service", "inner_cold_service"):
        src = (TRANSFER / "services" / f"{name}.py").read_text(encoding="utf-8")
        for stale in ("REPLACE(LOWER", "_norm_wh", "_COLD_UNIT_ALIASES", "_SITE_ALIASES", "SIMILAR TO",
                      "'supreme cold'", "'savla bond'"):
            assert stale not in src, (name, stale)


# ── A transfer's box rows and their source units ─────────────────────────────
class _BoxesConn:
    def __init__(self, boxes=(), origin=()):
        self.boxes, self.origin = list(boxes), list(origin)
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append(s)
        if "FROM interunit_transfer_boxes itb" in s:
            return self.boxes
        if "GROUP BY lot_no, raw_u" in s:
            return self.origin
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append(" ".join(sql.split()))
        return {"id": 5, "challan_no": "TRANS-5", "stock_trf_date": None, "from_site": "Cold Storage",
                "to_site": "A185", "vehicle_no": "", "driver_name": None, "approved_by": None, "remark": None,
                "reason_code": None, "status": "Dispatch", "request_id": None, "created_by": None,
                "created_ts": None, "approved_ts": None, "has_variance": False, "from_cold_unit": "Supreme",
                "request_no": None}


def _box(id, unit, **over):
    row = {"id": id, "header_id": 5, "transfer_line_id": None, "box_number": id, "box_id": f"B-{id}",
           "article": "ALMOND", "lot_number": "L-1", "batch_number": None, "transaction_no": "TR-1",
           "net_weight": Decimal("25"), "gross_weight": Decimal("25"), "created_at": None, "updated_at": None,
           "source_storage": "Supreme", "source_unit": unit}
    row.update(over)
    return row


def test_a_box_row_joins_only_its_own_in_transit_row_and_names_its_unit_canonically():
    conn = _BoxesConn([_box(1, "Supreme Cold"), _box(2, "D-39"), _box(3, None), _box(4, "Savla")])
    boxes = asyncio.run(query_service._fetch_transfer_boxes(conn, 5))
    (sql,) = conn.calls
    assert ("LEFT JOIN pending_transfer_stock pts ON pts.transfer_out_id = itb.header_id "
            "AND btrim(pts.box_id) = btrim(itb.box_id) AND btrim(pts.transaction_no) = btrim(itb.transaction_no) "
            "AND pts.status = 'In Transit'") in sql
    assert "CASE" not in sql and len(boxes) == 4
    assert [b["source_unit"] for b in boxes] == ["Supreme", "Savla D-39", None, "Savla"]


def test_a_lots_origin_unit_is_its_commonest_store_by_canonical_name():
    origin = [{"lot_no": "L-1", "raw_u": "Supreme Cold", "n": 2}, {"lot_no": "L-1", "raw_u": "supreme", "n": 2},
              {"lot_no": "L-1", "raw_u": "Rishi", "n": 3}, {"lot_no": "L-2", "raw_u": "Savla D-39", "n": 1},
              {"lot_no": "L-2", "raw_u": "D-514", "n": 1}, {"lot_no": "L-3", "raw_u": "Cold Storage", "n": 9}]
    conn = _BoxesConn([_box(1, None), _box(2, None, lot_number="L-2"), _box(3, None, lot_number="L-3")], origin)
    out = asyncio.run(query_service.get_transfer(conn, 5))
    assert [b["lot_origin_unit"] for b in out["boxes"]] == ["Supreme", "Savla D-39", None]
    assert not any("CASE" in s for s in conn.calls)


# ── The in-transit list ──────────────────────────────────────────────────────
class _PendingConn:
    def __init__(self, records, site_rows, registered):
        self.records, self.site_rows, self.registered = records, site_rows, registered
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        return "exists"

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append((s, args))
        if "WITH combined AS" in s:
            return self.records
        if "FROM warehouse_sites" in s:
            return self.registered
        return self.site_rows


def _pending(tid, frm, to, *, types=(None, None), units=None):
    return {"transfer_out_id": tid, "transfer_out_challan_no": f"TRANS-{tid}", "dispatched_at": datetime(2026, 10, 15),
            "from_site": frm, "to_site": to, "from_company": None, "to_company": None,
            "from_storage_type": types[0], "to_storage_type": types[1], "total_boxes": 1, "total_cartons": 1,
            "total_kg": Decimal("25"), "dispatched_by": "u", "header_status": "Dispatch", "unallocated_boxes": None,
            "updated_ts": None, "from_cold_unit": units}


def _pending_db():
    records = [_pending(1, "A-185", "ESKIMO"), _pending(2, "Warehouse A185", "Savla Bond"),
               _pending(3, "Cold Storage", "A185 Cold", units="Rishi"),
               _pending(4, "savla", "W202"), _pending(5, "Savla", "Supreme Cold", types=("cold", "cold"))]
    site_rows = [{"from_site": r["from_site"], "to_site": r["to_site"], "n": 1} for r in records]
    registered = [{"site_code": "ESKIMO", "site_name": "Eskimo"}, {"site_code": None, "site_name": "Rishi Cold Storage"},
                  {"site_code": "dev_int", "site_name": "Dev Int"}]
    return _PendingConn(records, site_rows, registered)


def test_the_in_transit_list_names_and_counts_sites_canonically():
    conn = _pending_db()
    out = asyncio.run(pending_service.list_pending_transfers(conn))
    got = [(r["from_site"], r["to_site"], r["from_storage_type"], r["to_storage_type"]) for r in out["records"]]
    assert got == [("A185", "Eskimo", "warehouse", "cold"), ("A185", "Savla Bond", "warehouse", "cold"),
                   ("Cold Storage", "A185", "cold", "warehouse"), ("Savla", "W202", "warehouse", "warehouse"),
                   ("Savla", "Supreme", "cold", "cold")]
    options = out["filter_options"]
    assert options["from_site_counts"] == {"A185": 2, "Cold Storage": 1, "Savla": 2}
    assert options["to_site_counts"] == {"Eskimo": 1, "Savla Bond": 1, "A185": 1, "W202": 1, "Supreme": 1}
    assert options["from_sites"] == ["A185", "Cold Storage", "Dev Int", "Eskimo", "Rishi", "Savla"]
    assert options["to_sites"] == ["A185", "Dev Int", "Eskimo", "Rishi", "Savla Bond", "Supreme", "W202"]
    assert "SIMILAR TO" not in conn.calls[0][0]


@pytest.mark.parametrize("filters, ids", [({"from_site": "A185"}, [1, 2]), ({"to_site": "Eskimo Cold"}, [1]),
                                          ({"from_site": "Warehouse A185", "to_site": "savla bond"}, [2]),
                                          ({"from_site": "Rishi"}, [])])
def test_the_in_transit_filters_compare_sites(filters, ids):
    out = asyncio.run(pending_service.list_pending_transfers(_pending_db(), **filters))
    assert [r["transfer_out_id"] for r in out["records"]] == ids


def test_the_in_transit_scope_is_every_spelling_and_each_cold_unit():
    conn = _pending_db()
    asyncio.run(pending_service.list_pending_transfers(conn, scope=["Rishi"], search="TRANS"))
    sql, args = conn.calls[0]
    scope = query_service.site_scope_sql("$2", sites=("from_site", "to_site"), units=("from_cold_unit",))
    assert _ws(scope) in sql and args == ("%TRANS%", scope_keys(["Rishi"]))
    assert "MIN(ith.from_cold_unit) AS from_cold_unit" in sql and "h.from_cold_unit AS from_cold_unit" in sql
    assert _eval(scope, {"from_site": "Cold Storage", "to_site": "A185", "from_cold_unit": "Rishi"}, args)
    assert not _eval(scope, {"from_site": "Cold Storage", "to_site": "A185", "from_cold_unit": None}, args)


def test_in_transit_by_lot_counts_only_transfers_not_received_and_compares_sites():
    class Conn(_Capture):
        async def fetchval(self, sql, *args):
            return "exists"

        async def fetchrow(self, sql, *args):
            self.calls.append((" ".join(sql.split()), args))
            return {"pending_cartons": 0, "pending_kg": 0, "box_count": 0}

    conn = Conn()
    asyncio.run(pending_service.pending_by_lot(conn, lot_no="L-1", from_site="D-39", scope=["Rishi"]))
    join = ("FROM pending_transfer_stock pts JOIN interunit_transfers_header h "
            "ON h.id = pts.transfer_out_id AND h.status <> 'Received'")
    frm = query_service.site_scope_sql("$2", sites=("pts.from_site",))
    scope = query_service.site_scope_sql("$3", sites=("pts.from_site", "pts.to_site"), units=("h.from_cold_unit",))
    assert len(conn.calls) == 2
    for sql, args in conn.calls:
        assert join in sql and "LEFT JOIN" not in sql and _ws(frm) in sql and _ws(scope) in sql
        assert args == ("L-1", scope_keys("Savla D-39"), scope_keys(["Rishi"]))
    assert _eval(frm, {"pts.from_site": "Old Savla"}, conn.calls[0][1])


# ── The dashboard ────────────────────────────────────────────────────────────
def test_the_dashboard_scope_is_every_spelling_and_each_cold_unit():
    conn = _Capture()
    asyncio.run(dashboard_service.get_all_data(conn, scope=["D-39"], from_date="2026-10-01"))
    sql, args = conn.calls[0]
    scope = query_service.site_scope_sql("$1", sites=("h.from_site", "h.to_site"), units=("h.from_cold_unit",))
    assert _ws(scope) in sql and args[0] == scope_keys(["D-39"])
    conn = _Capture()
    asyncio.run(dashboard_service.get_filter_options(conn, scope=["D-39"]))
    assert all(_ws(query_service.site_scope_sql("$1", sites=("h.from_site", "h.to_site"),
                                                units=("h.from_cold_unit",))) in s and a == (scope_keys(["D-39"]),)
               for s, a in conn.calls)
    conn = _Capture()
    asyncio.run(dashboard_service.get_all_data(conn, scope=[]))
    assert conn.calls[0][1] == ()


# ── X-04: the detail reads ───────────────────────────────────────────────────
TRANSFER_ROW = {"id": 5, "challan_no": "TRANS-5", "stock_trf_date": None, "from_site": "Cold Storage",
                "to_site": "Warehouse A185", "vehicle_no": "", "driver_name": None, "approved_by": None,
                "remark": None, "reason_code": None, "status": "Dispatch", "request_id": None, "created_by": None,
                "created_ts": None, "approved_ts": None, "has_variance": False,
                "from_cold_unit": "Rishi, Savla D-39", "request_no": None}
RECEIPT_ROW = {"id": 7, "transfer_out_id": 5, "transfer_out_no": "TRANS-5", "grn_number": "GRN-5", "grn_date": None,
               "receiving_warehouse": "A185", "received_by": "u", "received_at": None, "box_condition": "Good",
               "condition_remarks": None, "status": "Pending", "inward_transaction_no": None, "created_at": None,
               "updated_at": None, "from_warehouse": "Cold Storage", "transfer_to_site": "Warehouse A185",
               "from_cold_unit": "Rishi, Savla D-39"}


class _ReadConn:
    """The detail reads' statements: the transfer header, the receipt header, a
    transfer's sites, a receipt's To; every other read is empty."""

    def __init__(self, transfer=TRANSFER_ROW, receipt=RECEIPT_ROW):
        self.transfer, self.receipt = transfer, receipt
        self.calls: list[str] = []

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append(s)
        if "FROM interunit_transfer_in_header h" in s:
            return self.receipt
        if s.startswith("SELECT from_site, to_site, from_cold_unit FROM interunit_transfers_header"):
            return None if self.transfer is None else {k: self.transfer[k] for k in
                                                       ("from_site", "to_site", "from_cold_unit")}
        if "FROM interunit_transfers_header h" in s:
            return self.transfer
        raise AssertionError(f"unexpected fetchrow: {s[:80]}")

    async def fetch(self, sql, *args):
        self.calls.append(" ".join(sql.split()))
        return []

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append(s)
        if s.startswith("SELECT t.to_site FROM interunit_transfer_in_header ih"):
            return None if self.transfer is None else self.transfer["to_site"]
        raise AssertionError(f"unexpected fetchval: {s[:80]}")


SEES = [["A185"], ["Warehouse A185"], ["Rishi"], ["D-39"], ["Cold Storage"], []]
BLIND = [["W202"], ["Savla Bond"], ["Supreme"]]


@pytest.mark.parametrize("scope", SEES + BLIND, ids=lambda s: ",".join(s) or "unscoped")
def test_a_transfer_and_its_receipt_are_read_by_their_sites_users_only(scope):
    visible = scope not in BLIND
    reads = {
        "transfer": lambda conn: query_service.get_transfer(conn, 5, scope=scope),
        "receipt": lambda conn: query_service.get_transfer_in(conn, 7, scope=scope),
    }
    for name, read in reads.items():
        if visible:
            out = asyncio.run(read(_ReadConn()))
            assert out["id"] == (5 if name == "transfer" else 7)
        else:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(read(_ReadConn()))
            assert exc.value.status_code == 404, name
    if visible:
        got = asyncio.run(query_service.get_transfer_in(_ReadConn(), 7, scope=scope))
        assert got["from_cold_unit"] == "Rishi, Savla D-39" and schemas.TransferInDetail(**got)


def test_a_receipt_is_read_by_its_transfers_to_site_whatever_receiving_warehouse_it_holds():
    # A receipt opened with another receiving warehouse (the old pages copied the To raw,
    # or a client sent another): the To's store still reads it, as list_transfer_ins lists it.
    conn = _ReadConn(transfer={**TRANSFER_ROW, "from_site": "W202", "from_cold_unit": None},
                     receipt={**RECEIPT_ROW, "receiving_warehouse": "F53", "from_warehouse": "W202",
                              "from_cold_unit": None})
    assert asyncio.run(query_service.get_transfer_in(conn, 7, scope=["A185"]))["id"] == 7
    with pytest.raises(HTTPException) as exc:
        asyncio.run(query_service.get_transfer_in(conn, 7, scope=["Rishi"]))
    assert (exc.value.status_code, exc.value.detail) == (404, "Transfer IN not found")


@pytest.mark.parametrize("scope, visible", [([], True), (["A185"], True), (["W202"], True),
                                            (["Savla D-39"], False)])
def test_a_request_is_read_by_its_sites_users_only(scope, visible):
    conn = trd.FakeConn(trd._req(from_site="A-185", to_site="W202"))
    if visible:
        assert asyncio.run(query_service.get_request(conn, trd.RID, scope=scope))["id"] == trd.RID
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(query_service.get_request(conn, trd.RID, scope=scope))
        assert (exc.value.status_code, exc.value.detail) == (404, "Request not found")


def test_the_detail_routes_pass_the_users_scope(client, monkeypatch):  # noqa: F811
    seen: dict = {}

    def spy(name, result):
        async def call(*args, **kwargs):
            seen[name] = kwargs.get("scope")
            return result
        return call

    monkeypatch.setattr(pending_service, "pending_by_lot", spy("by-lot", {"transfers": []}))
    monkeypatch.setattr(inner_cold_service, "get_inner_transfer", spy("inner", {"challan_no": "ICT-1"}))
    c = client(_ReadConn(), D39)
    assert c.get(f"{URL}/pending-stock/by-lot", params={"lot_no": "L-1"}).status_code == 200
    assert c.get(f"{URL}/inner-transfer/ICT-1").status_code == 200
    assert seen == {"by-lot": ["D-39"], "inner": ["D-39"]}
    for path, status in ((f"{URL}/transfers/5", 200), (f"{URL}/transfer-in/7", 200)):
        assert c.get(path).status_code == status, path            # D-39: one of the cold units
    blind = client(_ReadConn(), trd._user("w@candorfoods.in", warehouses=["F53"]))
    for path in (f"{URL}/transfers/5", f"{URL}/transfer-in/7"):
        res = blind.get(path)
        assert res.status_code == 404, path


def test_a_receipt_by_transfer_is_read_by_the_transfers_sites_users_only(client, monkeypatch):  # noqa: F811
    async def pending(conn, transfer_out_id):
        return {"exists": False, "header": None}
    monkeypatch.setattr(receive_service, "get_pending_by_transfer_out", pending)
    path = f"{URL}/transfer-in/pending/by-transfer-out/5"
    assert client(_ReadConn(), A185).get(path).json() == {"exists": False, "header": None}
    assert client(_ReadConn(), RISHI).get(path).status_code == 200
    res = client(_ReadConn(), trd._user("w@candorfoods.in", warehouses=["W202"])).get(path)
    assert res.status_code == 404 and res.json()["message"] == "Transfer not found"
    # No such transfer: the receipt read answers as it always has.
    assert client(_ReadConn(transfer=None), RISHI).get(path).json() == {"exists": False, "header": None}


# ── X-05: an edit is locked to the stored From too ──────────────────────────
def _put_body(frm="A185", to="W202") -> dict:
    return {"header": {"stock_trf_date": "16-10-2026", "from_warehouse": frm, "to_warehouse": to,
                       "vehicle_no": "MH43BP6885"},
            "lines": [{"material_type": "RM", "item_category": "NUTS", "sub_category": "ALMOND",
                       "item_description": "ALMOND", "quantity": "1"}]}


@pytest.mark.parametrize("stored, user, status", [
    ("W202", A185, 403),                  # a W202 transfer re-routed to the user's own site
    ("Warehouse A185", A185, 200),        # the user's site, as a legacy row spells it
    ("W202", ADMIN, 200), ("W202", ANYONE, 200),
])
def test_an_edit_checks_the_transfers_stored_from_site(client, monkeypatch, stored, user, status):  # noqa: F811
    edited: list = []

    async def update_transfer(conn, transfer_id, body, user=None):
        # The router passes its user on: the service checks the stored From again under
        # the header lock, and the route of a transfer without a request (X-05).
        assert user is not None
        edited.append(transfer_id)
        return {**query_service._map_transfer_header(TRANSFER_ROW), "lines": [], "boxes": [], "grn_records": []}
    monkeypatch.setattr(create_service, "update_transfer", update_transfer)
    conn = _ReadConn(transfer={**TRANSFER_ROW, "from_site": stored, "from_cold_unit": None})
    res = client(conn, user).put(f"{URL}/transfers/5", json=_put_body())
    assert res.status_code == status
    if status == 403:
        assert res.json()["message"] == ("Transfer OUT must originate from your assigned warehouse (A185); "
                                         "this transfer is from W202.")
        assert edited == []
    else:
        assert edited == [5]
    reads = [s for s in conn.calls if s.startswith("SELECT from_site, to_site, from_cold_unit")]
    assert len(reads) == (1 if permissions.has_warehouse_lock(user) else 0)


# ── The receipt edit: locked to the transfer's To, stored by its canonical name ──
REOPENER = trd._user("b.hrithik@candorfoods.in", warehouses=["A185"])


@pytest.mark.parametrize("sent, status, stored", [(None, 200, None), ("a-185", 200, "A185"),
                                                  ("Warehouse A185", 200, "A185"), ("W202", 409, None)])
def test_a_receipt_edit_is_for_the_transfers_to_site(client, monkeypatch, sent, status, stored):  # noqa: F811
    edits: list = []

    async def edit_transfer_in(conn, header_id, body, edited_by, user=None):
        assert user.email == REOPENER.email      # the service locks the receipt to the To site too
        edits.append(body.receiving_warehouse)
        return {"id": header_id}
    monkeypatch.setattr(receive_service, "edit_transfer_in", edit_transfer_in)
    body = {"condition_remarks": "ok"} if sent is None else {"receiving_warehouse": sent}
    res = client(_ReadConn(), REOPENER).put(f"{URL}/transfer-in/7", json=body)
    assert res.status_code == status
    if status == 409:
        assert res.json()["message"] == ("This receipt is for a transfer to A185; its receiving warehouse can't "
                                         "be W202.")
        assert edits == []
    else:
        assert edits == [stored]
    res = client(_ReadConn(transfer={**TRANSFER_ROW, "to_site": "W202"}), REOPENER).put(
        f"{URL}/transfer-in/7", json={"condition_remarks": "ok"})
    assert res.status_code == 403
    assert res.json()["message"] == ("Transfer IN must be received into your assigned warehouse (A185); this "
                                     "transfer is to W202.")


def test_the_inner_cold_routes_pass_the_user(client, monkeypatch):  # noqa: F811
    seen: list = []

    async def create(conn, body, user):
        seen.append(("create", user.email))
        return {"status": "success", "updated_records": 1, "errors": [], "challan_no": body.header.challan_no}

    async def delete(conn, challan_no, user):
        seen.append(("delete", user.email))
        return {"success": True, "message": "undone"}
    monkeypatch.setattr(inner_cold_service, "create_inner_transfer", create)
    monkeypatch.setattr(inner_cold_service, "delete_inner_cold", delete)
    body = {"header": {"challan_no": "ICT-1", "from_warehouse": "Savla D-39"},
            "lines": [{"stock_record_id": 10, "company": "cfpl", "quantity": 1, "old_lot_number": "L-1",
                       "new_lot_number": "L-2"}]}
    assert client(_ReadConn(), D39).post(f"{URL}/inner-transfer", json=body).status_code == 201
    admin = client(_ReadConn(), ADMIN)
    assert admin.delete(f"{URL}/inner-transfer/ICT-1").json() == {"success": True, "message": "undone"}
    assert seen == [("create", D39.email), ("delete", ADMIN.email)]


# ── P1-36: the test-DB stub of warehouse_sites ──────────────────────────────
def test_the_warehouse_sites_stub_has_the_columns_the_dropdown_selects():
    stub = (ROOT / "app" / "db" / "test_db" / "db" / "000_external_stub_tables.sql").read_text(encoding="utf-8")
    table = re.search(r"CREATE TABLE IF NOT EXISTS warehouse_sites \((.*?)\);", stub, re.S)[1]
    columns = [l.split()[0] for l in table.strip().splitlines()]
    assert columns == ["id", "site_code", "site_name", "is_active"]
    assert "ALTER TABLE warehouse_sites ADD COLUMN IF NOT EXISTS id BIGSERIAL;" in stub
    assert "ALTER TABLE warehouse_sites ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;" in stub
    dropdown = (TRANSFER / "services" / "dropdown_service.py").read_text(encoding="utf-8")
    assert "SELECT id, site_code, site_name, is_active" in dropdown and "FROM warehouse_sites" in dropdown
    raw = (ROOT / "app" / "db" / "test_db" / "db" / "000_external_stub_tables.sql").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
