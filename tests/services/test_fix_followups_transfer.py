"""Follow-ups of the site / box audit fixes on the transfer side, found in review
(2026-10-16), against the SQL-evaluating fake databases of the transfer tests.

What is pinned:
  - Scan & send: a request, or its transfer, stored from and to one site in two
    spellings (older code accepted 'A185' -> 'Warehouse A185') is refused with a 409
    that says so, nothing written — not the dispatch header's validation error (a 500);
  - a box that went on a newer transfer since it joined this one (this one's receipt
    undone after the round trip): a weight edit on the scan screen is refused (409), so
    the newer transfer keeps its in-transit row; an edit that keeps it re-joins it now,
    so R3 reads this transfer as its latest, as the edit parks it, and no third dispatch
    takes it;
  - the receipt's own steps (acknowledge, the batch, un-acknowledge, finalize, close
    with shortage, re-open) are locked to the transfer's stored To site, in any
    spelling, before anything is written; admins and users with no lock pass without a
    read; the routes pass their user, and un-acknowledge the box's transaction;
  - the in-transit list shows a transfer without in-transit rows when R3 has it in
    transit: by R3's statuses (IMS's In Transit / Completed too, any case) and with no
    Received receipt in either ledger;
  - the Transfer Summary of a user whose grants give no site key (a blank grant) is
    empty, as every other list is, never everything;
  - the customer-return list and export filter factory_unit as a site;
  - from_cold_unit takes a snapshot's storage location when its unit names no store
    (sites.cold_row_site);
  - the stock-take floor profile parity test finds the frontend (it skipped before).

Run:  PYTHONPATH=. python -m pytest tests/services/test_fix_followups_transfer.py
"""
from __future__ import annotations

import asyncio
import copy
import re

import pytest
from fastapi import HTTPException

from app.core.middleware.request_context import AuthError
from app.core.sites import scope_keys, site_key
from app.core.warehouse_scope import WAREHOUSE_NORM_ANY_SQL
from app.modules.customer_returns.services import query_service as cr_query
from app.modules.transfer import permissions, schemas
from app.modules.transfer.services import (
    create_service,
    dashboard_service,
    delete_service,
    lookup_service,
    pending_service,
    query_service,
    receive_service,
)
from tests.services import test_transfer_box_lookup_cold as cold
from tests.services import test_transfer_box_rules as br
from tests.services import test_transfer_r3_sites as r3s
from tests.services import test_transfer_record_only as ro
from tests.services import test_transfer_request_decisions as trd
from tests.services import test_transfer_scan_save as ss
from tests.services.test_transfer_request_decisions import client  # noqa: F401  (the HTTP fixture)

BOX, TNO, USER = ro.BOX, ro.TNO, ro.USER
URL = "/api/v1/transfer"
RECEIVER = r3s.RECEIVER                                                # may receive; locked to A185
W202_STORE = trd._user("store.w202@candorfoods.in", warehouses=["W202"])
TO_SITE_READ = "SELECT to_site FROM interunit_transfers_header WHERE id = $1"


@pytest.fixture(autouse=True)
def _fresh_existence_cache(monkeypatch):
    # lookup_service caches to_regclass answers for the process; each test starts clean.
    monkeypatch.setattr(lookup_service, "_EXISTS", {})


@pytest.fixture(autouse=True)
def no_readback(monkeypatch):
    async def get_transfer(conn, transfer_id, **kw):
        return {"id": transfer_id}
    monkeypatch.setattr(query_service, "get_transfer", get_transfer)


# ── Scan & send: a stored route that is one site twice ───────────────────────
def test_a_legacy_request_from_and_to_one_site_is_refused_on_its_first_scan():
    db = ss._db(("A185", "Warehouse A185"))
    refusal = ss._refused(db, lambda: ss._scan(db, ss.A1, user=ss.ANYONE))
    assert refusal == f"Request {ss.REQ} is from and to the same site (A185); it can't be dispatched."


def test_a_legacy_transfer_from_and_to_one_site_takes_no_more_boxes():
    db = ss._db()                                           # A-185 -> W202
    ss._scan(db, ss.A1, user=ss.ANYONE)
    ss._transfer(db)["to_site"] = "Warehouse A185"          # as older code could store it
    refusal = ss._refused(db, lambda: ss._scan(db, ss.A2, user=ss.ANYONE))
    assert refusal == f"Transfer {ss.CHALLAN} is from and to the same site (A185); no box can be added to it."


def test_the_scan_route_answers_a_one_site_request_with_a_409_not_a_500(client):  # noqa: F811
    db = ss._db(("Supreme", "Supreme Cold"))
    header = {"challan_no": ss.CHALLAN, "stock_trf_date": "15-10-2026", **ss.TRANSPORT}
    res = client(db, trd._user(USER)).post(f"{URL}/requests/{ss.RID}/scan",
                                           json={"header": header, "box": ss._box_json(ss.A1)})
    assert res.status_code == 409
    assert res.json()["message"] == f"Request {ss.REQ} is from and to the same site (Supreme); it can't be dispatched."


# ── A box that went on and came back: weights and edits ─────────────────────
def _round_trip_on_the_scan_screen():
    """A1 scanned onto the request's transfer T1 (A-185 -> W202) and received at W202,
    then sent on from W202 on TRANS-ON (in transit), then T1's receipt deleted: T1 is in
    transit again without A1, which is TRANS-ON's."""
    db = ss._db(ss.WAREHOUSE)
    ss._scan(db, ss.A1, user=ss.ANYONE)
    t1 = ss._transfer(db)["id"]
    ti = br._receive(db, t1)
    t2 = br._send(db, ss.A1, challan="TRANS-ON", frm="W202", to="F53", at=br._oct(20))
    asyncio.run(delete_service.delete_transfer_in(db, ti, USER))
    return db, t1, t2


def _owners(db, box_id) -> set:
    return {p["transfer_out_id"] for p in db.tables["pending_transfer_stock"] if p["box_id"] == box_id}


def test_a_weight_edit_of_a_box_sent_on_since_is_refused_and_the_newer_transfer_keeps_it():
    db, t1, t2 = _round_trip_on_the_scan_screen()
    assert _owners(db, ss.A1.box_id) == {t2}
    refusal = ss._refused(db, lambda: ss._weights(db, ss.A1, 24.0, 24.5, user=ss.ANYONE, transfer_id=t1))
    assert refusal == (f"Box {ss.A1.box_id} has since been sent on challan TRANS-ON, so its weights on transfer "
                       f"{ss.CHALLAN} can't be changed.")
    assert _owners(db, ss.A1.box_id) == {t2}


def test_a_weight_edit_of_a_box_still_on_its_transfer_is_saved():
    db = ss._db(ss.WAREHOUSE)
    ss._scan(db, ss.A1, user=ss.ANYONE)
    out = ss._weights(db, ss.A1, 24.0, 24.5, user=ss.ANYONE)
    assert out["box_id"] == ss.A1.box_id and float(out["net_weight"]) == 24.0
    assert _owners(db, ss.A1.box_id) == {ss._transfer(db)["id"]}


def test_an_edit_re_joins_a_box_that_went_on_and_came_back_so_r3_has_it_in_transit_there():
    db = ro._db()
    t1 = br._send(db, ro.V2, challan="TRANS-1", frm="W202", to="A185", at=br._oct(5))
    r3s._joined_at(db, t1, BOX, br._oct(5))
    ti1 = br._receive(db, t1)                                   # at A185
    t2 = br._send(db, ro.V2, challan="TRANS-2", frm="A185", to="W202", at=br._oct(7))
    r3s._joined_at(db, t2, BOX, br._oct(7))
    br._receive(db, t2)                                         # back at W202
    asyncio.run(delete_service.delete_transfer_in(db, ti1, USER))
    br._edit(db, t1, ro.V2, frm="W202", to="A185")              # keeps V2, which the guard lets go from W202

    assert r3s._box_row(db, t1)["created_at"] > br._oct(7)     # it joined TRANS-1 again, at the edit
    assert _owners(db, BOX) == {t1}
    in_transit = f"Box {BOX} is already on challan TRANS-1 (in transit)"
    assert r3s._where(db, "W202") == ("in_transit", "TRANS-1", in_transit)
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        br._send(db, ro.V2, challan="TRANS-3", frm="W202", to="F53", at=br._oct(9))
    assert (exc.value.status_code, exc.value.detail) == (409, in_transit)
    assert db.tables == before


def test_an_edit_keeps_the_join_time_of_a_box_that_never_left():
    db = ro._db()
    t1 = br._send(db, ro.V2, challan="TRANS-1", frm="W202", to="A185", at=br._oct(5))
    r3s._joined_at(db, t1, BOX, br._oct(5))
    br._edit(db, t1, ro.V2, ro.BULK, frm="W202", to="A185")
    assert r3s._box_row(db, t1)["created_at"] == br._oct(5)


# ── The receipt's own steps: the To site's lock ─────────────────────────────
def _pending_receipt(status="Pending"):
    """TRANS-IN, W202 -> 'Warehouse A185' (A185 in a legacy spelling), with V2 on it and a
    receipt in `status` that records V2."""
    db = r3s._db()
    t = r3s._transfer(db, 99000001, "TRANS-IN", frm="W202", to="Warehouse A185", status="Dispatch",
                      created=br._oct(1))
    rid = r3s._grn(db, t, status=status, acked=({},))
    return db, t, rid


def _ack():
    return schemas.PendingBoxAcknowledge(box_id=BOX, transaction_no=TNO)


STEPS = {
    "acknowledge": ("Pending", lambda db, rid, u: receive_service.acknowledge_pending_box(db, rid, _ack(), user=u)),
    "batch": ("Pending", lambda db, rid, u: receive_service.acknowledge_pending_boxes_batch(db, rid, [_ack()], user=u)),
    "unacknowledge": ("Pending", lambda db, rid, u: receive_service.unacknowledge_pending_box(
        db, rid, BOX, transaction_no=TNO, user=u)),
    "finalize": ("Pending", lambda db, rid, u: receive_service.finalize_transfer_in(
        db, rid, schemas.FinalizeTransferIn(box_condition="Good"), user=u)),
    "close": ("Pending", lambda db, rid, u: receive_service.close_with_shortage(db, rid, "short", USER, user=u)),
    "reopen": ("Received", lambda db, rid, u: receive_service.reopen_transfer_in(db, rid, USER, user=u)),
}


@pytest.mark.parametrize("step", STEPS)
def test_each_step_of_a_receipt_is_locked_to_its_transfers_to_site(step):
    status, call = STEPS[step]
    db, _, rid = _pending_receipt(status)
    before = copy.deepcopy(db.tables)
    with pytest.raises(AuthError) as exc:
        asyncio.run(call(db, rid, W202_STORE))
    assert exc.value.status_code == 403
    assert exc.value.message == ("Transfer IN must be received into your assigned warehouse (W202); this transfer "
                                 "is to A185.")
    assert db.tables == before
    asyncio.run(call(db, rid, RECEIVER))                       # A185's user: the To site in another spelling


@pytest.mark.parametrize("user", [trd.ADMIN, trd._user(USER), None], ids=["admin", "no-lock", "no-user"])
def test_admins_and_users_with_no_lock_pass_the_receipt_lock_without_a_read(user):
    db, _, rid = _pending_receipt()
    asyncio.run(receive_service.acknowledge_pending_box(db, rid, _ack(), user=user))
    assert not db.statements(TO_SITE_READ)


def test_the_receipt_step_routes_pass_their_user_and_the_boxs_transaction(client, monkeypatch):  # noqa: F811
    seen: dict = {}

    def fake(name):
        async def service(conn, *args, **kw):
            seen[name] = kw
            return {}
        return service

    for name in ("acknowledge_pending_box", "acknowledge_pending_boxes_batch", "unacknowledge_pending_box",
                 "finalize_transfer_in", "reopen_transfer_in", "close_with_shortage"):
        monkeypatch.setattr(receive_service, name, fake(name))
    hrithik = trd._user("b.hrithik@candorfoods.in", warehouses=["A185"])      # may acknowledge and re-open
    c = client(r3s._db(), hrithik)
    box = {"box_id": BOX, "transaction_no": TNO}
    for method, path, body in (("post", "acknowledge", box), ("post", "acknowledge-batch", [box]),
                               ("delete", f"acknowledge/{BOX}?transaction_no={TNO}", None),
                               ("post", "finalize", {"box_condition": "Good"}), ("post", "reopen", {}),
                               ("post", "close-with-shortage", {"shortage_reason": "short"})):
        kw = {} if body is None else {"json": body}
        assert getattr(c, method)(f"{URL}/transfer-in/7/{path}", **kw).status_code == 200, path
    assert {name: kw["user"].email for name, kw in seen.items()} == dict.fromkeys(seen, hrithik.email)
    assert len(seen) == 6 and seen["unacknowledge_pending_box"]["transaction_no"] == TNO
    c.delete(f"{URL}/transfer-in/7/acknowledge/{BOX}")
    assert seen["unacknowledge_pending_box"]["transaction_no"] is None


# ── The in-transit list: R3's statuses ───────────────────────────────────────
class _Recorder:
    """Records every statement; reads return nothing; to_regclass answers by `tables`."""

    def __init__(self, tables=("pending_transfer_stock", "cold_transfer_in_headers", "warehouse_sites")):
        self.tables = set(tables)
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append((s, args))
        if s == "SELECT to_regclass($1)":
            return args[0] if args[0].removeprefix("public.") in self.tables else None
        return 0

    async def fetch(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return None


def _ws(sql: str) -> str:
    """A statement as _Recorder records it: whitespace collapsed."""
    return " ".join(sql.split())


def _in_transit_statuses(sql: str) -> set:
    m = re.search(r"LOWER\(BTRIM\(COALESCE\(h\.status, ''\)\)\) IN \(([^)]*)\)", sql)
    return {s.strip().strip("'") for s in m[1].split(",")}


@pytest.mark.parametrize("cold_ledger", [True, False])
def test_the_in_transit_list_shows_a_transfer_without_rows_by_r3s_statuses(cold_ledger):
    tables = ["pending_transfer_stock", "warehouse_sites"] + (["cold_transfer_in_headers"] if cold_ledger else [])
    conn = _Recorder(tables)
    asyncio.run(pending_service.list_pending_transfers(conn))
    reads = [s for s, _ in conn.calls if "FROM interunit_transfers_header h WHERE" in s]
    assert len(reads) == 2                                   # the list, and its chips
    for sql in reads:
        assert "h.status IN ('Dispatch', 'Partial')" not in sql
        assert _in_transit_statuses(sql) == set(lookup_service._IN_TRANSIT)
        assert ("NOT EXISTS (SELECT 1 FROM interunit_transfer_in_header ti WHERE ti.transfer_out_id = h.id "
                "AND LOWER(BTRIM(COALESCE(ti.status, ''))) = 'received')") in sql
        assert ("FROM cold_transfer_in_headers ch" in sql) is cold_ledger
    # The statuses as SQL lowers and trims them, against R3's own reading.
    for status in ("Dispatch", "Partial", "Completed", " In Transit ", "Received", "Partially Received", None):
        listed = (status or "").strip().lower() in _in_transit_statuses(reads[0])
        assert listed is lookup_service.is_in_transit_status(status), status


# ── The Transfer Summary of a blank grant ───────────────────────────────────
def test_a_blank_grant_scopes_the_transfer_summary_to_nothing():
    user = trd._user("role.admin@candorfoods.in", warehouses=[""])
    scope = permissions.scope_warehouses(user)
    assert scope == [""] and permissions.has_warehouse_lock(user)
    clause = _ws(dashboard_service._scope_clause(1))

    conn = _Recorder()
    asyncio.run(dashboard_service.get_all_data(conn, scope=scope))
    sql, args = conn.calls[0]
    assert clause in sql and args == ([],)

    conn = _Recorder()
    asyncio.run(dashboard_service.get_filter_options(conn, scope=scope))
    assert len(conn.calls) == 6 and all(clause in s and a == ([],) for s, a in conn.calls)

    conn = _Recorder()                                       # unrestricted: no clause, no argument
    asyncio.run(dashboard_service.get_filter_options(conn, scope=[]))
    assert all(clause not in s and a == () for s, a in conn.calls)


# ── Customer returns: factory_unit as a site ─────────────────────────────────
@pytest.mark.parametrize("read", ["list", "export"])
def test_the_customer_return_factory_unit_filter_compares_sites(read):
    conn = _Recorder()
    if read == "list":
        asyncio.run(cr_query.list_crs(conn, company="cfpl", page=1, per_page=10, factory_unit="Savla D-39"))
    else:
        asyncio.run(cr_query.export_cr_records(conn, company="cfpl", factory_unit="Savla D-39"))
    sql, args = conn.calls[0]
    assert _ws(WAREHOUSE_NORM_ANY_SQL("h.factory_unit", "$1")) in sql and "h.factory_unit = $1" not in sql
    assert args[0] == scope_keys("Savla D-39")
    keys = set(args[0])
    for stored in ("D-39", "Savla D-39", "Old Savla", "Savla D-39 Cold Storage", "savla-d39"):
        assert site_key(stored) in keys, stored
    for stored in ("Savla Bond", "Savla D-514", "Savla", "W202"):
        assert site_key(stored) not in keys, stored


# ── from_cold_unit: a row's store, unit first ────────────────────────────────
def test_from_cold_unit_takes_the_storage_location_when_the_unit_names_no_store():
    db = r3s._db()
    rows = (("Savla", "Savla D-514"), ("", "Rishi Cold Storage"), ("D-39", "Savla Bond"), ("A185 Cold", "Chamber 2"))
    db.tables["cdpl_cold_stocks"] = [cold._cold(id=7400 + n, box_id=f"CU-{n}", unit=u, storage_location=loc)
                                     for n, (u, loc) in enumerate(rows)]
    boxes = [ro._bx(n + 1, f"CU-{n}", TNO) for n in range(len(rows))]
    t = asyncio.run(create_service.create_transfer(db, br._plain(*boxes, frm="Cold Storage", to="W202"), USER))["id"]
    assert ro._header(db, t)["from_cold_unit"] == "Rishi, Savla D-39, Savla D-514"


# ── Stock take: the floor profile parity test runs ───────────────────────────
def test_the_floor_profile_parity_test_finds_the_frontend():
    from tests.services import test_stock_take_floor_profile as floor_profile
    assert floor_profile.TS.exists(), floor_profile.TS
