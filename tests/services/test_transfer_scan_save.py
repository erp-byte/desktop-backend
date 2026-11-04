"""Scan & send saves on scan (scan_service), against the SQL-evaluating fake database of
test_transfer_box_lookup_cold, with the approved request of test_transfer_dispatch_lock
(its FOR UPDATE / ORDER BY / request-lock support) and the stock of
test_transfer_box_rules.

What is pinned:
  - the first scan of an Approved request creates its transfer with that box, as
    create_transfer does: Partial while the request asks for more, Dispatch when the box
    completes it; the request becomes Transferred; the box's in-transit row is a copy of
    its stock row and no stock table is written;
  - each later scan adds its box to that transfer, which turns Dispatch on the box that
    completes the request; the transport details stay the first scan's;
  - B0: a transfer from a request has no LINE- rows, and loses any it has on its next save
    (scan or edit); a transfer without a request keeps them (_settle parks those a line
    lacks and drops a covered line's, however often it runs) and its status formula; a
    request line without a quantity asks for no box (as R1 reads it), so it does not hold
    the transfer Partial — once the request is deleted, its stored lines count;
  - INVARIANT S: after each scan, a remove and a weight edit, the transfer's rows (header,
    lines, box rows, in-transit rows) and the request's status are what create_transfer
    writes for the same request with the same boxes in the same order — from a warehouse
    and from a cold store; for weight edits also when the stock row (cold, boxes_v2, bulk
    entry) has no weight of its own, and the box was scanned with one or at 0;
  - every refusal, with nothing written: transport missing on the first scan (before the
    box is looked up), a box the request does not ask for (another article, another lot,
    over the quantity), a box that cannot go from the From site, a box already on the
    transfer, a request or transfer stored without a From or a To site (409, not the
    dispatch header's refusal as a 500), a received transfer, another warehouse's user
    (403), a transfer without a request, a deleted request, an unknown box, bad weights,
    blank transport, a request in another status, a malformed body;
  - removing the last box, or all of them, deletes the transfer and puts the request back
    to Approved (its decisions as they were); a later scan starts a new transfer;
  - a weight edit re-parks the box from its own copy: the stock snapshot stays even when
    the stock row has gone;
  - every save locks the request row FOR UPDATE before it reads or writes anything else,
    and checks for a receipt with the transfer's header row locked, as a receipt is begun;
    an edit (PUT) and a delete of a request's transfer lock its request row too, right
    after the read that finds the transfer (one without a request locks none), and are
    404 when the scan screen cancelled the transfer while they waited;
  - undoing a receipt (re-open, delete) puts a request's transfer back Partial or
    Dispatch, as its boxes make it;
  - the request reads (get / list) give its newest transfer (by created_ts, then id);
  - the four routes as a client sees them: status codes and the messages.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_scan_save.py
"""
from __future__ import annotations

import asyncio
import copy
import re
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.core.middleware.request_context import AuthError
from app.modules.transfer import schemas
from app.modules.transfer.services import (
    create_service,
    delete_service,
    lookup_service,
    query_service,
    receive_service,
    reversal_service,
    scan_service,
)
from tests.services import test_transfer_box_lookup_cold as cold
from tests.services import test_transfer_box_rules as br
from tests.services import test_transfer_dispatch_lock as dl
from tests.services import test_transfer_record_only as ro
from tests.services import test_transfer_request_decisions as trd
from tests.services.test_transfer_request_decisions import client  # noqa: F401  (the HTTP fixture)

RID, USER, TNO = dl.RID, dl.USER, dl.TNO
REQ = dl._request()["request_no"]
A1, A2, CASHEW = br.A1, br.A2, br.CASHEW              # the request's boxes, in its cold stock
PISTA, ALMOND_3, CASHEW_L9 = br.PISTA, br.ALMOND_3, br.CASHEW_L9
CHALLAN = "TRANS-SCAN"
TRANSPORT = {"vehicle_no": "MH43BP6885", "driver_name": "RAMESH PATIL", "approved_by": "SUNIL JASORIA"}
WAREHOUSE, COLD_STORE = ("A-185", "W202"), ("Rishi", "W202")
STORE = trd.STORE                                     # locked to A185, the request's From site
ANYONE = trd._user(USER)                              # the same account, no warehouse lock
ELSEWHERE = trd._user("store.w202@candorfoods.in", warehouses=["W202"])
URL = "/api/v1/transfer"


@pytest.fixture(autouse=True)
def _fresh_existence_cache(monkeypatch):
    # lookup_service caches to_regclass answers for the process; each test starts clean.
    monkeypatch.setattr(lookup_service, "_EXISTS", {})


@pytest.fixture(autouse=True)
def no_readback(monkeypatch):
    async def get_transfer(conn, transfer_id):
        return {"id": transfer_id}
    monkeypatch.setattr(query_service, "get_transfer", get_transfer)


# ── The fake database ────────────────────────────────────────────────────────
def _db(route=WAREHOUSE, *, lines=dl.REQUEST_LINES, status="Approved", stock=None) -> dl.DispatchDB:
    """dl's fake database (it reads the lock on a transfer's request too) with dl's
    request, `status`, on `route` (with every column the request reads return), its cold
    stock plus the other boxes these tests scan, then `stock` applied to it, and empty
    transfer and receipt tables."""
    request = trd._req(id=RID, request_no=REQ, from_site=route[0], to_site=route[1], reason_code=dl.REASON,
                       remarks=dl.REASON, status=status)
    db = br._stocked(dl.DispatchDB(
        interunit_transfer_requests=[request],
        interunit_transfer_request_lines=[{**l, "created_at": None, "updated_at": None} for l in lines],
        interunit_transfers_header=[], interunit_transfers_lines=[], interunit_transfer_boxes=[],
        interunit_transfer_in_header=[], interunit_transfer_in_boxes=[],
        cfpl_cold_stocks=dl._cold_rows(),
    ))
    if stock:
        stock(db)
    return db


def _stock(db) -> dict:
    return copy.deepcopy({t: db.tables[t] for t in cold.STOCK_TABLES if t in db.tables})


# ── Saves ────────────────────────────────────────────────────────────────────
def _scan(db, box, *, user=STORE, request_id=RID, **header) -> dict:
    body = schemas.ScanBox(header=schemas.ScanHeader(**{"challan_no": CHALLAN, "stock_trf_date": "15-10-2026",
                                                        **TRANSPORT, **header}), box=box)
    return asyncio.run(scan_service.scan_box(db, request_id, body, user))


def _transfer(db) -> dict:
    """The request's transfer (there is one)."""
    (h,) = [h for h in db.tables["interunit_transfers_header"] if h.get("request_id") == RID]
    return h


def _remove(db, *boxes, every=None, user=STORE, transfer_id=None) -> dict:
    keys = [schemas.BoxKey(box_id=b.box_id, transaction_no=b.transaction_no) for b in boxes]
    body = schemas.RemoveBoxes(boxes=keys or None, all=every)
    return asyncio.run(scan_service.remove_boxes(db, transfer_id or _transfer(db)["id"], body, user))


def _weights(db, box, net, gross, *, user=STORE, transfer_id=None) -> dict:
    body = schemas.BoxWeights(box_id=box.box_id, transaction_no=box.transaction_no, net_weight=net, gross_weight=gross)
    return asyncio.run(scan_service.update_box_weights(db, transfer_id or _transfer(db)["id"], body, user))


def _transport(db, *, user=STORE, transfer_id=None, **values) -> dict:
    body = schemas.TransportUpdate(**{**TRANSPORT, **values})
    return asyncio.run(scan_service.update_transport(db, transfer_id or _transfer(db)["id"], body, user))


def _settle(db, transfer_id) -> None:
    async def go():
        async with db.transaction():
            await create_service._settle(db, transfer_id)
    asyncio.run(go())


def _refused(db, call, status=409) -> str:
    """The refusal of `call` (its status asserted) — a 4xx with nothing written."""
    before = copy.deepcopy(db.tables)
    with pytest.raises((HTTPException, AuthError)) as exc:
        call()
    assert exc.value.status_code == status
    assert db.tables == before
    return getattr(exc.value, "detail", None) or exc.value.message


def _parked(db) -> dict:
    return ro._parked(db, _transfer(db)["id"])


# ── INVARIANT S: what create_transfer writes ────────────────────────────────
def _created(route, boxes, *, stock=None, lines=dl.REQUEST_LINES) -> dl.DispatchDB:
    """A fresh database (_db, with `stock` and `lines`) whose request create_transfer
    dispatched at once with `boxes`, with the first scan's header."""
    db = _db(route, stock=stock, lines=lines)
    payload = schemas.TransferCreate(
        header=schemas.TransferHeaderCreate(challan_no=CHALLAN, stock_trf_date="15-10-2026", from_warehouse=route[0],
                                            to_warehouse=route[1], **TRANSPORT),
        lines=[dl._line("WHATEVER THE CLIENT SENDS")], boxes=list(boxes), request_id=RID)
    asyncio.run(create_service.create_transfer(db, payload, USER))
    return db


def _without(row: dict, *keys) -> dict:
    return {k: v for k, v in row.items() if k not in keys}


def _rows(db) -> dict:
    """The request's transfer as INVARIANT S compares it — header, lines, box rows in the
    order they were saved, in-transit rows — ids, created_ts and each box's created_at
    (when it joined the transfer) aside (a box's line and a LINE- row's by the line's
    place), and the request's status."""
    h = _transfer(db)
    lines = sorted((l for l in db.tables["interunit_transfers_lines"] if l["header_id"] == h["id"]),
                   key=lambda l: l["id"])
    place = {l["id"]: n for n, l in enumerate(lines)}
    boxes = sorted((b for b in db.tables["interunit_transfer_boxes"] if b["header_id"] == h["id"]),
                   key=lambda b: b["id"])

    def line_row(box_id):
        m = re.fullmatch(r"LINE-(\d+)-(\d+)", box_id)
        return f"LINE-{place[int(m[1])]}-{m[2]}" if m else box_id

    pending = [{**_without(p, "id", "transfer_out_id"), "box_id": line_row(p["box_id"])}
               for p in db.tables["pending_transfer_stock"] if p["transfer_out_id"] == h["id"]]
    return {
        "header": _without(h, "id", "created_ts"),
        "lines": [_without(l, "id", "header_id") for l in lines],
        "boxes": [{**_without(b, "id", "header_id", "created_at"), "transfer_line_id": place.get(b["transfer_line_id"])}
                  for b in boxes],
        "pending": sorted(pending, key=lambda p: p["box_id"]),
        "request": db.tables["interunit_transfer_requests"][0]["status"],
    }


def _with_weights(box, net, gross) -> schemas.BoxCreate:
    return box.model_copy(update={"net_weight": net, "gross_weight": gross})


@pytest.mark.parametrize("route", [WAREHOUSE, COLD_STORE], ids=["warehouse", "cold-store"])
def test_every_save_leaves_what_create_transfer_writes_for_the_same_boxes(route):
    db = _db(route)
    scanned = [A2, CASHEW, A1]
    for n, box in enumerate(scanned, 1):
        _scan(db, box, user=ANYONE)
        assert _rows(db) == _rows(_created(route, scanned[:n])), f"after scan {n}"
    h = _transfer(db)
    if route == COLD_STORE:
        assert h["from_cold_unit"] == "Savla D-39" and "unallocated_boxes" not in h
    else:
        assert h["unallocated_boxes"] == 0 and "from_cold_unit" not in h

    _remove(db, CASHEW, user=ANYONE)
    assert _rows(db) == _rows(_created(route, [A2, A1]))
    _weights(db, A1, 24.1, 24.9, user=ANYONE)
    assert _rows(db) == _rows(_created(route, [A2, _with_weights(A1, "24.1", "24.9")]))
    _weights(db, A2, 24, 0, user=ANYONE)
    assert _rows(db) == _rows(_created(route, [_with_weights(A2, "24", "0"), _with_weights(A1, "24.1", "24.9")]))
    assert cold.stock_writes(db) == []


# park_boxes gives a box its stock row's own weight, else the box's own: a weight edit
# changes the in-transit copy's weight only where it was the box's own.
def _no_cold_weight(db):
    """A1's cold row without a weight_kg."""
    (row,) = [r for r in db.tables["cfpl_cold_stocks"] if r["box_id"] == A1.box_id]
    row["weight_kg"] = None


def _in_boxes_v2(weight):
    """A1 in boxes_v2 (cfpl; its lot on its article row) instead of the cold stock, its
    row's net and gross weight `weight` (None: none)."""
    def stock(db):
        db.tables["cfpl_cold_stocks"] = [r for r in db.tables["cfpl_cold_stocks"] if r["box_id"] != A1.box_id]
        db.tables.update(cfpl_boxes_v2=[cold._v2(box_id=A1.box_id, net_weight=weight, gross_weight=weight)],
                         cfpl_articles_v2=[cold._art()], cdpl_boxes_v2=[], cdpl_articles_v2=[])
    return stock


def _in_bulk_entry(weight):
    """CASHEW in bulk entry (cdpl) instead of the cold stock, its row's net and gross
    weight `weight` (None: none)."""
    def stock(db):
        db.tables["cfpl_cold_stocks"] = [r for r in db.tables["cfpl_cold_stocks"] if r["box_id"] != CASHEW.box_id]
        db.tables["cdpl_bulk_entry_boxes"] = [cold._bulk(box_id=CASHEW.box_id, transaction_no=TNO, net_weight=weight,
                                                         gross_weight=weight)]
    return stock


@pytest.mark.parametrize("scanned_at", ["24.75", "0"], ids=["scanned-with-a-weight", "scanned-at-0"])
@pytest.mark.parametrize("route, stock, box", [
    (COLD_STORE, _no_cold_weight, A1),
    (WAREHOUSE, _no_cold_weight, A1),
    (WAREHOUSE, _in_boxes_v2(None), A1),
    (WAREHOUSE, _in_boxes_v2(Decimal("25.000")), A1),
    (WAREHOUSE, _in_bulk_entry(None), CASHEW),
    (WAREHOUSE, _in_bulk_entry(Decimal("10.000")), CASHEW),
], ids=["cold-row-without-a-weight", "cold-only-box-without-a-weight-at-a-warehouse", "boxes_v2-without-a-weight",
        "boxes_v2-with-a-weight", "bulk-entry-without-a-weight", "bulk-entry-with-a-weight"])
def test_weight_edits_leave_what_create_transfer_writes_whatever_weight_the_stock_row_has(route, stock, box,
                                                                                          scanned_at):
    db = _db(route, stock=stock)
    scanned = [A2, _with_weights(box, scanned_at, scanned_at)]
    for b in scanned:
        _scan(db, b, user=ANYONE)
    assert _rows(db) == _rows(_created(route, scanned, stock=stock))
    for net, gross in (("24.1", "24.9"), ("23", "24")):
        _weights(db, box, float(net), float(gross), user=ANYONE)
        assert _rows(db) == _rows(_created(route, [A2, _with_weights(box, net, gross)], stock=stock)), (net, gross)
    assert cold.stock_writes(db) == []


# ── The first scan ───────────────────────────────────────────────────────────
def test_the_first_scan_creates_the_transfer_with_its_box_and_marks_the_request_transferred():
    db = _db()
    stock = _stock(db)
    out = _scan(db, A1)

    h = _transfer(db)
    # Stored as the box's own record says (article, lot), with the scanned weights.
    assert out == {"transfer_id": h["id"], "challan_no": CHALLAN, "status": "Partial", "box_count": 1,
                   "created": True,
                   "box": {"box_number": 1, "box_id": A1.box_id, "transaction_no": TNO, "article": "ALMOND 25KG",
                           "lot_number": "LOT-9", "batch_number": "", "net_weight": "24.75", "gross_weight": "25"}}
    schemas.ScanResult(**out)
    # The request's sites, by their canonical names ('A-185' is A185).
    assert (h["status"], h["from_site"], h["to_site"], h["remark"]) == ("Partial", "A185", "W202", dl.REASON)
    assert {k: h[k] for k in TRANSPORT} == TRANSPORT
    assert [l["item_desc_raw"] for l in db.tables["interunit_transfers_lines"]] == ["ALMOND 25KG", "CASHEW W320"]
    assert db.tables["interunit_transfer_requests"][0]["status"] == "Transferred"
    # In transit as a copy of its cold stock row, which stays as it was; nothing else.
    p = _parked(db)
    assert set(p) == {A1.box_id} and dl._phantoms(db) == []
    assert (p[A1.box_id]["source_table"], p[A1.box_id]["source_row_id"], p[A1.box_id]["cold_storage_data"],
            p[A1.box_id]["status"]) == ("cfpl_cold_stocks", 7101, cold.SNAPSHOT, "In Transit")
    assert _stock(db) == stock and cold.stock_writes(db) == []


def test_a_first_scan_that_completes_the_request_is_dispatch():
    db = _db(lines=[dl._rline(7301, qty=Decimal("1.000"))])
    out = _scan(db, A1)
    assert (out["status"], out["box_count"]) == ("Dispatch", 1)
    assert _transfer(db)["status"] == "Dispatch"


def test_the_first_scan_without_a_challan_or_date_gets_them_as_create_does():
    db = _db()
    out = _scan(db, A1, challan_no=None, stock_trf_date=None)
    h = _transfer(db)
    assert out["challan_no"] == h["challan_no"] and h["challan_no"].startswith("TRANS")
    assert h["stock_trf_date"] == datetime.now().date()


# ── Later scans ──────────────────────────────────────────────────────────────
def test_later_scans_add_to_the_transfer_and_the_box_that_completes_it_makes_it_dispatch():
    db = _db()
    stock = _stock(db)
    first = _scan(db, A1)
    # A later scan's transport details are not used, blank or not.
    second = _scan(db, A2, vehicle_no="KA01AB1234", driver_name="SOMEONE", approved_by="ELSE")
    assert (second["transfer_id"], second["created"], second["status"], second["box_count"]) == (
        first["transfer_id"], False, "Partial", 2)
    third = _scan(db, CASHEW, vehicle_no="", driver_name=None, approved_by=" ")
    assert (third["status"], third["box_count"]) == ("Dispatch", 3)
    assert third["box"] == {"box_number": 3, "box_id": CASHEW.box_id, "transaction_no": TNO,
                            "article": "CASHEW W320", "lot_number": "L-3", "batch_number": "",
                            "net_weight": "24.75", "gross_weight": "25"}

    h = _transfer(db)
    assert len(db.tables["interunit_transfers_header"]) == 1 and h["status"] == "Dispatch"
    assert {k: h[k] for k in TRANSPORT} == TRANSPORT
    assert set(_parked(db)) == {A1.box_id, A2.box_id, CASHEW.box_id} and dl._phantoms(db) == []
    assert db.tables["interunit_transfer_requests"][0]["status"] == "Transferred"
    assert _stock(db) == stock and cold.stock_writes(db) == []


def test_the_request_read_shows_its_transfer_once_a_box_is_saved():
    db = _db()
    assert asyncio.run(query_service.newest_transfers(db, [RID])) == {}
    out = _scan(db, A1)
    got = asyncio.run(query_service.get_request(db, RID))
    assert (got["status"], got["transfer_id"], got["transfer_challan_no"], got["transfer_status"]) == (
        "Transferred", out["transfer_id"], CHALLAN, "Partial")


# ── B0: a request transfer has only its boxes in transit ────────────────────
def _line_rows(db, line_article):
    """Give the request's transfer the LINE- rows of its `line_article` line, as a
    request transfer had them before only its boxes were in transit."""
    h = _transfer(db)
    (line,) = [l for l in db.tables["interunit_transfers_lines"]
               if l["header_id"] == h["id"] and l["item_desc_raw"] == line_article]

    async def go():
        async with db.transaction():
            await reversal_service.park_lines(
                db, transfer_out_id=h["id"], challan_no=h["challan_no"], from_site=h["from_site"],
                to_site=h["to_site"], lines=[line], dispatched_at=datetime(2026, 10, 15), dispatched_by=USER)
    asyncio.run(go())
    assert dl._phantoms(db)


def test_a_request_transfer_has_no_line_rows_and_loses_any_it_had_on_its_next_save():
    db = _db()
    _scan(db, A1)
    assert dl._phantoms(db) == [] and _transfer(db)["status"] == "Partial"   # 1 box < 3 ordered

    _line_rows(db, "CASHEW W320")
    _scan(db, A2)
    assert dl._phantoms(db) == [] and set(_parked(db)) == {A1.box_id, A2.box_id}

    _line_rows(db, "ALMOND 25KG")
    dl._update(db, dl._payload(boxes=[A1, A2]))
    assert dl._phantoms(db) == [] and _transfer(db)["status"] == "Partial"   # 2 boxes < 3 ordered


def test_a_transfer_without_a_request_keeps_its_line_rows_and_its_status_formula():
    db = _db()
    lines = [dl._line("ALMOND 25KG", qty="2"), dl._line("CASHEW W320", lot="L-3")]
    dl._create(db, dl._payload(request_id=None, from_wh="A185", to_wh="W202", lines=lines, boxes=[A1]))
    (h,) = db.tables["interunit_transfers_header"]
    almond, cashew = sorted(db.tables["interunit_transfers_lines"], key=lambda l: l["id"])
    assert dl._phantoms(db) == [("CASHEW W320", "L-3")] and h["status"] == "Partial"   # 1 box + 1 line < 3

    def box(b, line, n):
        return {"id": 500 + n, "header_id": h["id"], "transfer_line_id": line["id"], "box_number": n,
                "box_id": b.box_id, "article": b.article, "lot_number": b.lot_number, "batch_number": "",
                "transaction_no": b.transaction_no, "net_weight": Decimal("10"), "gross_weight": Decimal("10")}

    # Every line covered now: the cashew line's rows go.
    db.tables["interunit_transfer_boxes"] += [box(A2, almond, 2), box(CASHEW, cashew, 3)]
    _settle(db, h["id"])
    assert dl._phantoms(db) == [] and h["status"] == "Dispatch"                         # 3 boxes
    # Uncovered again: its rows come back once, however often it is settled; the line counts.
    db.tables["interunit_transfer_boxes"] = [b for b in db.tables["interunit_transfer_boxes"]
                                             if b["box_id"] != CASHEW.box_id]
    _settle(db, h["id"])
    _settle(db, h["id"])
    assert dl._phantoms(db) == [("CASHEW W320", "L-3")] and h["status"] == "Dispatch"   # 2 boxes + 1 line


def test_a_request_line_without_a_quantity_asks_for_no_box_so_it_does_not_hold_the_transfer_partial():
    # An older request line with no qty: R1 gives it no box (it is stored as qty 1), so
    # the status counts it as R1 does, and the transfer is Dispatch once the rest is in.
    lines = [dl._rline(7301, qty=None), dl.REQUEST_LINES[0]]
    db = _db(lines=lines)
    out = _scan(db, CASHEW)
    assert (out["status"], out["box_count"]) == ("Dispatch", 1)
    assert _rows(db) == _rows(_created(WAREHOUSE, [CASHEW], lines=lines))
    assert _refused(db, lambda: _scan(db, A1)) == br._over(0, "ALMOND 25KG")
    # Once the request is deleted, the transfer is held to its own lines, as R1 then is:
    # the almond line as stored asks for 1 box. (Older data: a Transferred request can no
    # longer be deleted.)
    dl._drop_request(db)
    dl._update(db, dl._payload(boxes=[CASHEW]))
    assert _transfer(db)["status"] == "Partial"
    dl._update(db, dl._payload(boxes=[CASHEW, A1]))
    assert _transfer(db)["status"] == "Dispatch"


# ── Refusals ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("missing", [{"vehicle_no": ""}, {"driver_name": "  "}, {"approved_by": None},
                                     {"vehicle_no": None, "driver_name": None, "approved_by": None}],
                         ids=["vehicle", "driver", "approver", "all"])
def test_a_first_scan_without_the_transport_details_is_409_before_the_box_is_looked_up(missing):
    db = _db()
    assert _refused(db, lambda: _scan(db, A1, **missing)) == scan_service.FILL_TRANSPORT
    assert db.box_locks == [] and db.unions == [] and not db.statements(ro.GUARD)
    assert db.tables["interunit_transfer_requests"][0]["status"] == "Approved"


@pytest.mark.parametrize("scanned, box, detail", [
    ([], PISTA, f"Box P-1 is PISTA, which is not on request {REQ}."),
    ([A1], PISTA, f"Box P-1 is PISTA, which is not on request {REQ}."),
    ([A1], CASHEW_L9, f"Box {CASHEW_L9.box_id} is lot L-9; request {REQ} asks for lot L-3 of CASHEW W320."),
    ([A1, A2], ALMOND_3, br._over(2, "ALMOND 25KG")),
    # Judged by its own record, not by what the client says it is.
    ([A1], br._box("P-2", "almond 25kg", "LOT-9", n=8), f"Box P-2 is PISTA, which is not on request {REQ}."),
], ids=["first-not-on-the-request", "not-on-the-request", "other-lot", "over-the-quantity", "its-own-record"])
def test_a_box_the_request_does_not_ask_for_is_409(scanned, box, detail):
    db = _db()
    for b in scanned:
        _scan(db, b)
    assert _refused(db, lambda: _scan(db, box)) == detail


@pytest.mark.parametrize("scanned", [[], [A1]], ids=["first-scan", "later-scan"])
def test_a_box_that_cannot_go_from_the_from_site_is_409(scanned):
    db = _db()
    # A2 is in transit on a transfer without a request.
    dl._create(db, dl._payload(request_id=None, from_wh="A185", to_wh="W303", boxes=[A2]))
    (other,) = [h["challan_no"] for h in db.tables["interunit_transfers_header"] if h["request_id"] is None]
    for b in scanned:
        _scan(db, b)
    assert _refused(db, lambda: _scan(db, A2)) == f"Box {A2.box_id} is already on challan {other} (in transit)"


def test_a_box_already_on_the_transfer_is_409():
    db = _db()
    _scan(db, A1)
    padded = A1.model_copy(update={"box_id": f" {A1.box_id} ", "transaction_no": f"{TNO} "})
    for box in (A1, padded):
        assert _refused(db, lambda: _scan(db, box)) == f"Box {A1.box_id} is already on transfer {CHALLAN}."


@pytest.mark.parametrize("route, missing", [(("", "W202"), "From"), (("A-185", " "), "To"), (("-", ""), "From or To")],
                         ids=["no-from", "no-to", "neither"])
def test_a_request_stored_without_a_from_or_a_to_is_409_on_its_first_scan(route, missing):
    # Older code stored such a request; the dispatch header now refuses the route (422),
    # so the scan screen says so plainly instead of failing on the header it builds.
    db = _db(route)
    assert _refused(db, lambda: _scan(db, A1, user=ANYONE)) == (
        f"Request {REQ} has no {missing} site; it can't be dispatched.")


def test_a_transfer_stored_without_a_to_is_409_on_a_later_scan():
    db = _db()
    _scan(db, A1)
    _transfer(db)["to_site"] = ""
    assert _refused(db, lambda: _scan(db, A2)) == f"Transfer {CHALLAN} has no To site; no box can be added to it."


def _received(db, how):
    if how == "status":
        _transfer(db)["status"] = "Received"
    else:   # a receipt (GRN) begun, the transfer still Dispatch / Partial
        db.tables["interunit_transfer_in_header"].append({"id": 7, "transfer_out_id": _transfer(db)["id"],
                                                          "status": "Pending"})


RECEIVED = f"Transfer {CHALLAN} has already been received at W202; it can't be changed here."
CHANGES = {
    "scan": lambda db, **kw: _scan(db, CASHEW, **kw),
    "remove": lambda db, **kw: _remove(db, A1, **kw),
    "weights": lambda db, **kw: _weights(db, A1, 24, 25, **kw),
    "transport": lambda db, **kw: _transport(db, driver_name="SURESH", **kw),
}


@pytest.mark.parametrize("how", ["status", "receipt"])
@pytest.mark.parametrize("change", CHANGES)
def test_a_received_transfer_is_not_changed_here(change, how):
    db = _db()
    _scan(db, A1)
    _scan(db, A2)
    _received(db, how)
    assert _refused(db, lambda: CHANGES[change](db)) == RECEIVED


@pytest.mark.parametrize("change", ["first-scan", *CHANGES])
def test_a_user_of_another_warehouse_is_403(change):
    db = _db()
    if change != "first-scan":
        _scan(db, A1)
        _scan(db, A2)
    run = (lambda: _scan(db, A1, user=ELSEWHERE)) if change == "first-scan" else (
        lambda: CHANGES[change](db, user=ELSEWHERE))
    assert "assigned warehouse" in _refused(db, run, 403)


@pytest.mark.parametrize("change", ["remove", "weights", "transport"])
def test_a_transfer_without_a_request_is_changed_on_the_edit_page_only(change):
    db = _db()
    dl._create(db, dl._payload(request_id=None, from_wh="A185", to_wh="W202", boxes=[A1, A2]))
    (h,) = db.tables["interunit_transfers_header"]
    assert _refused(db, lambda: CHANGES[change](db, transfer_id=h["id"])) == scan_service.USE_EDIT


@pytest.mark.parametrize("change", ["remove", "weights", "transport"])
def test_a_transfer_whose_request_was_deleted_is_changed_on_the_edit_page_only(change):
    db = _db()
    _scan(db, A1)
    _scan(db, A2)
    dl._drop_request(db)  # older data: a Transferred request can no longer be deleted
    (h,) = db.tables["interunit_transfers_header"]
    assert _refused(db, lambda: CHANGES[change](db, transfer_id=h["id"])) == (
        f"The request of transfer {CHALLAN} no longer exists; use Edit.")
    assert _refused(db, lambda: _scan(db, CASHEW), 404) == "Request not found"


@pytest.mark.parametrize("change", ["remove", "weights", "transport"])
def test_an_unknown_transfer_is_404(change):
    db = _db()
    assert _refused(db, lambda: CHANGES[change](db, transfer_id=404), 404) == "Transfer not found"


def test_an_unknown_box_is_404_naming_it():
    db = _db()
    _scan(db, A1)
    _scan(db, A2)
    assert _refused(db, lambda: _remove(db, A2, PISTA), 404) == f"Box P-1 is not on transfer {CHALLAN}."
    assert _refused(db, lambda: _remove(db, PISTA, CASHEW), 404) == (
        f"Box P-1 is not on transfer {CHALLAN}. Box {CASHEW.box_id} is not on transfer {CHALLAN}.")
    assert _refused(db, lambda: _weights(db, CASHEW, 24, 25), 404) == (
        f"Box {CASHEW.box_id} is not on transfer {CHALLAN}.")


@pytest.mark.parametrize("net, gross, detail", [
    (-1, 25, f"Box {A1.box_id}: a weight can't be negative"),
    (24, -0.5, f"Box {A1.box_id}: a weight can't be negative"),
    (25.5, 25, f"Box {A1.box_id}: net weight exceeds gross"),
])
def test_bad_weights_are_422(net, gross, detail):
    db = _db()
    _scan(db, A1)
    assert _refused(db, lambda: _weights(db, A1, net, gross), 422) == detail


@pytest.mark.parametrize("values, detail", [
    ({"vehicle_no": " "}, "Fill in vehicle number."),
    ({"driver_name": "", "approved_by": None}, "Fill in driver name and approval authority."),
    ({"vehicle_no": None, "driver_name": None, "approved_by": "\t"},
     "Fill in vehicle number, driver name and approval authority."),
])
def test_blank_transport_details_are_422(values, detail):
    db = _db()
    _scan(db, A1)
    assert _refused(db, lambda: _transport(db, **values), 422) == detail


@pytest.mark.parametrize("status", ["Pending", "Accepted", "Rejected"])
def test_a_request_not_approved_is_409_as_create_refuses_it(status):
    db = _db(status=status)
    assert _refused(db, lambda: _scan(db, A1)) == f"Request is '{status}'; only an Approved request can be dispatched"


def test_a_request_in_the_wrong_state_for_its_transfer_is_409_and_an_unknown_one_404():
    db = _db(status="Transferred")             # its transfer deleted from the dashboard
    assert _refused(db, lambda: _scan(db, A1)) == f"Request {REQ} has no transfer to add boxes to."
    db = _db()
    _scan(db, A1)
    db.tables["interunit_transfer_requests"][0]["status"] = "Approved"
    assert _refused(db, lambda: _scan(db, A2)) == (
        f"Request {REQ} already has transfer {CHALLAN}; it can't be dispatched again.")
    assert _refused(db, lambda: _scan(db, A2, request_id=RID + 9), 404) == "Request not found"


def test_malformed_bodies_are_422():
    db = _db()
    for direct in (br._box("", tno="DIRECT"), br._box("X-1", tno="DIRECT"), br._box(" ", tno=TNO)):
        assert _refused(db, lambda: _scan(db, direct), 422) == "A scanned box needs its box id and transaction number."
    _scan(db, A1)
    for kw in ({}, {"every": False}, {"every": True, "boxes": True}):
        boxes = (A1,) if kw.pop("boxes", False) else ()
        assert _refused(db, lambda: _remove(db, *boxes, **kw), 422) == (
            "Name the boxes to remove, or send all: true (one of the two).")


# ── Remove ───────────────────────────────────────────────────────────────────
def test_removing_a_box_takes_it_off_the_transfer_and_settles_it():
    db = _db()
    for b in (A1, A2, CASHEW):
        _scan(db, b)
    assert _transfer(db)["status"] == "Dispatch"
    out = _remove(db, CASHEW, CASHEW)
    assert out == {"cancelled": False, "transfer_id": _transfer(db)["id"], "status": "Partial", "box_count": 2}
    assert {b["box_id"] for b in db.tables["interunit_transfer_boxes"]} == {A1.box_id, A2.box_id}
    assert set(_parked(db)) == {A1.box_id, A2.box_id} and dl._phantoms(db) == []
    # It is free again: it can be scanned back on.
    assert _scan(db, CASHEW)["status"] == "Dispatch"


@pytest.mark.parametrize("how", ["the-last-box", "all"])
def test_removing_every_box_cancels_the_transfer_and_puts_the_request_back_to_approved(how):
    db = _db()
    _scan(db, A1)
    if how == "all":
        _scan(db, A2)
    request = dict(db.tables["interunit_transfer_requests"][0])
    stock = _stock(db)

    out = _remove(db, A1) if how == "the-last-box" else _remove(db, every=True)
    assert out == {"cancelled": True, "request_status": "Approved"}
    schemas.RemoveBoxesResult(**out)
    for t in ("interunit_transfers_header", "interunit_transfers_lines", "interunit_transfer_boxes",
              "pending_transfer_stock"):
        assert db.tables[t] == [], t
    after = db.tables["interunit_transfer_requests"][0]
    assert after["status"] == "Approved" and isinstance(after["updated_at"], datetime)
    assert _without(after, "status", "updated_at") == _without(request, "status", "updated_at")
    assert _stock(db) == stock and cold.stock_writes(db) == []

    # Ready to send again: the next scan starts a new transfer with the freed box.
    again = _scan(db, A1)
    assert again["created"] is True and again["box_count"] == 1
    assert db.tables["interunit_transfer_requests"][0]["status"] == "Transferred"


# ── Weights ──────────────────────────────────────────────────────────────────
def test_a_weight_edit_reparks_the_box_from_its_own_copy_of_the_stock_row():
    db = _db()
    _scan(db, A1)
    _scan(db, A2)
    before = _parked(db)
    # IMS has moved the box out of the cold stock since it was scanned: its copy stays.
    db.tables["cfpl_cold_stocks"] = [r for r in db.tables["cfpl_cold_stocks"] if r["box_id"] != A1.box_id]
    stock = _stock(db)

    out = _weights(db, A1, "24.5", "25.1")
    assert out == {"box_number": 1, "box_id": A1.box_id, "transaction_no": TNO, "article": "ALMOND 25KG",
                   "lot_number": "LOT-9", "batch_number": "", "net_weight": "24.5", "gross_weight": "25.1"}
    (row,) = [b for b in db.tables["interunit_transfer_boxes"] if b["box_id"] == A1.box_id]
    assert (row["net_weight"], row["gross_weight"]) == (Decimal("24.5"), Decimal("25.1"))
    after = _parked(db)
    assert after[A1.box_id] == {**before[A1.box_id], "net_weight": Decimal("24.5"), "gross_weight": Decimal("25.1")}
    assert after[A1.box_id]["source_row_id"] == 7101 and after[A1.box_id]["cold_storage_data"] == cold.SNAPSHOT
    assert after[A2.box_id] == before[A2.box_id]
    # Net above gross is fine while gross is not set.
    assert _weights(db, A2, 30, 0)["net_weight"] == "30.0"
    assert _stock(db) == stock and cold.stock_writes(db) == []


# ── Transport ────────────────────────────────────────────────────────────────
def test_the_transport_details_are_changed_alone():
    db = _db()
    _scan(db, A1)
    before = dict(_transfer(db))
    pending = copy.deepcopy(db.tables["pending_transfer_stock"])
    out = _transport(db, vehicle_no=" KA01AB1234 ", driver_name="SURESH", approved_by="YASH")
    assert out == {"transfer_id": before["id"], "vehicle_no": "KA01AB1234", "driver_name": "SURESH",
                   "approved_by": "YASH"}
    assert _transfer(db) == {**before, **_without(out, "transfer_id")}
    assert db.tables["pending_transfer_stock"] == pending


# ── The request row is locked first ──────────────────────────────────────────
SAVES = {
    "first-scan": (lambda db: _scan(db, A1), lambda db: RID),
    "later-scan": (lambda db: _scan(db, CASHEW), lambda db: RID),
    "remove": (lambda db: _remove(db, A2), lambda db: _transfer(db)["id"]),
    "weights": (lambda db: _weights(db, A1, 24, 25), lambda db: _transfer(db)["id"]),
    "transport": (lambda db: _transport(db, driver_name="SURESH"), lambda db: _transfer(db)["id"]),
}


@pytest.mark.parametrize("save", SAVES)
def test_every_save_locks_the_request_row_before_it_reads_or_writes_anything_else(save):
    db = _db()
    if save != "first-scan":
        _scan(db, A1)
        _scan(db, A2)
    run, key = SAVES[save]
    arg = key(db)
    n, k = len(db.calls), len(db.locked)
    run(db)
    lock, args = db.locked[k]
    assert "FROM interunit_transfer_requests" in lock and lock.endswith(" FOR UPDATE") and args == (arg,)
    assert db.calls[n][1] in (lock, lock.removesuffix(" FOR UPDATE"))


@pytest.mark.parametrize("save", [s for s in SAVES if s != "first-scan"])
def test_every_change_checks_for_a_receipt_with_the_transfer_row_locked(save):
    # A receipt begins under the same lock (receive_service.create_pending_transfer_in).
    db = _db()
    _scan(db, A1)
    _scan(db, A2)
    tid = _transfer(db)["id"]
    n, k = len(db.calls), len(db.locked)
    SAVES[save][0](db)
    header = " ".join(scan_service._TRANSFER.split())
    assert db.locked[k + 1] == (header, (tid,))
    sent = [s for _, s, _ in db.calls[n:]]
    assert sent.index(header.removesuffix(" FOR UPDATE")) < sent.index(
        "SELECT COUNT(*) FROM interunit_transfer_in_header WHERE transfer_out_id = $1")


REQUEST_LOCK = " ".join(create_service._LOCK_REQUEST_OF.split())
ELSEWHERE_SAVES = {
    "edit": lambda db, tid: dl._update(db, dl._payload(boxes=[A1, A2])),
    "delete": lambda db, tid: asyncio.run(delete_service.delete_transfer(db, tid)),
}


@pytest.mark.parametrize("save", ELSEWHERE_SAVES)
def test_an_edit_or_a_delete_of_a_request_transfer_locks_its_request_row_as_the_scan_screen_does(save):
    db = _db()
    _scan(db, A1)
    tid = _transfer(db)["id"]
    n, k = len(db.calls), len(db.locked)
    ELSEWHERE_SAVES[save](db, tid)
    # Only the read that finds the transfer comes first; nothing is judged or written before.
    assert db.locked[k] == (REQUEST_LOCK, (tid,))
    assert [s for _, s, _ in db.calls[n:]].index(REQUEST_LOCK) == 1


@pytest.mark.parametrize("save", ELSEWHERE_SAVES)
def test_an_edit_or_a_delete_of_a_transfer_without_a_request_locks_no_request(save):
    db = _db()
    dl._create(db, dl._payload(request_id=None, from_wh="A185", to_wh="W202", boxes=[A1]))
    (h,) = db.tables["interunit_transfers_header"]
    n = len(db.calls)
    ELSEWHERE_SAVES[save](db, h["id"])
    assert REQUEST_LOCK not in [s for _, s, _ in db.calls[n:]]


@pytest.mark.parametrize("save", ELSEWHERE_SAVES)
def test_an_edit_or_a_delete_that_waited_while_the_scan_screen_cancelled_the_transfer_is_404(save):
    db = _db()
    _scan(db, A1)
    tid = _transfer(db)["id"]
    run = db._run

    def cancelled_while_waiting(kind, sql, args):
        out = run(kind, sql, args)
        if " ".join(sql.split()) == REQUEST_LOCK:
            # The scan screen's removal of the last box commits while this waits.
            for t in ("interunit_transfers_header", "interunit_transfers_lines", "interunit_transfer_boxes",
                      "pending_transfer_stock"):
                db.tables[t] = []
            db.tables["interunit_transfer_requests"][0]["status"] = "Approved"
        return out

    db._run = cancelled_while_waiting
    with pytest.raises(HTTPException) as exc:
        ELSEWHERE_SAVES[save](db, tid)
    assert (exc.value.status_code, exc.value.detail) == (404, "Transfer not found")
    assert all(db.tables[t] == [] for t in ("interunit_transfers_header", "interunit_transfers_lines",
                                            "interunit_transfer_boxes", "pending_transfer_stock"))


# ── Receipts ─────────────────────────────────────────────────────────────────
def test_a_receipt_is_begun_with_the_transfer_row_locked_before_its_receipts_are_read():
    # The lock the scan screen checks for a receipt under (above), so the two never cross.
    db = _db()
    _scan(db, A1)
    tid = _transfer(db)["id"]
    ro._receipt(db, tid)                  # begun on the receiving screen: opening it again returns it
    data = schemas.PendingTransferInCreate(transfer_out_id=tid, grn_number="GRN-0002", receiving_warehouse="W202",
                                           received_by=USER)
    n, k = len(db.calls), len(db.locked)
    assert asyncio.run(receive_service.create_pending_transfer_in(db, data, ANYONE))["id"] == ro.TI
    lock = "SELECT id, challan_no, to_site FROM interunit_transfers_header WHERE id = $1 FOR UPDATE"
    assert db.locked[k] == (lock, (tid,)) and db.calls[n][1] == lock.removesuffix(" FOR UPDATE")


@pytest.mark.parametrize("undo", ["reopen", "delete"])
@pytest.mark.parametrize("boxes, status", [([A1, A2], "Partial"), ([A1, A2, CASHEW], "Dispatch")],
                         ids=["short", "complete"])
def test_undoing_the_receipt_of_a_request_transfer_puts_it_back_as_its_boxes_make_it(undo, boxes, status):
    db = _db()
    for b in boxes:
        _scan(db, b)
    h = _transfer(db)
    assert h["status"] == status
    ro._receipt(db, h["id"])
    ro._finalize(db)
    assert h["status"] == "Received" and _parked(db) == {}
    if undo == "reopen":
        asyncio.run(receive_service.reopen_transfer_in(db, ro.TI, USER))
    else:
        asyncio.run(delete_service.delete_transfer_in(db, ro.TI, USER))
    assert h["status"] == status and set(_parked(db)) == {b.box_id for b in boxes}


# ── The request reads: its newest transfer ───────────────────────────────────
def _header_row(id, challan, status, created_ts, request_id=RID) -> dict:
    return {"id": id, "challan_no": challan, "status": status, "request_id": request_id, "stock_trf_date": None,
            "from_site": "A185", "to_site": "W202", "remark": None, "vehicle_no": "", "driver_name": None,
            "approved_by": None, "created_by": USER, "created_ts": created_ts}


def test_the_request_reads_give_its_newest_transfer():
    # Ids are time-based and wrap: the newest is the latest created, then the larger id.
    transfers = [_header_row(99000001, "T-OLD", "Received", datetime(2026, 10, 1)),
                 _header_row(10000002, "T-NEW", "Partial", datetime(2026, 10, 5)),
                 _header_row(99000009, "T-LEGACY", "Dispatch", None),
                 _header_row(5, "T-OTHER-1", "Dispatch", None, request_id=RID + 1),
                 _header_row(6, "T-OTHER-2", "Dispatch", None, request_id=RID + 1)]
    conn = trd.FakeConn(trd._req(status="Transferred"), trd._req(id=RID + 1), trd._req(id=RID + 2),
                        transfers=transfers)
    got = asyncio.run(query_service.get_request(conn, RID))
    assert (got["transfer_id"], got["transfer_challan_no"], got["transfer_status"]) == (10000002, "T-NEW", "Partial")
    schemas.RequestWithLines(**got)

    listed = {r["id"]: r for r in asyncio.run(query_service.list_requests(conn, page=1, per_page=15))["records"]}
    assert {i: (r["transfer_id"], r["transfer_challan_no"], r["transfer_status"]) for i, r in listed.items()} == {
        RID: (10000002, "T-NEW", "Partial"), RID + 1: (6, "T-OTHER-2", "Dispatch"), RID + 2: (None, None, None)}
    reads = [a for k, s, a in conn.calls if "FROM interunit_transfers_header" in s]
    assert reads == [([RID],), ([RID, RID + 1, RID + 2],)]


# ── HTTP: the four routes ────────────────────────────────────────────────────
def _box_json(box) -> dict:
    return box.model_dump(mode="json")


def test_the_scan_route_as_a_client_sees_it(client):  # noqa: F811
    db = _db()
    c = client(db)
    header = {"challan_no": CHALLAN, "stock_trf_date": "15-10-2026"}
    res = c.post(f"{URL}/requests/{RID}/scan", json={"header": header, "box": _box_json(A1)})
    assert res.status_code == 409 and res.json()["message"] == scan_service.FILL_TRANSPORT

    res = c.post(f"{URL}/requests/{RID}/scan", json={"header": {**header, **TRANSPORT}, "box": _box_json(A1)})
    assert res.status_code == 200
    body = res.json()
    assert (body["challan_no"], body["status"], body["box_count"], body["created"]) == (CHALLAN, "Partial", 1, True)
    assert body["box"]["box_id"] == A1.box_id and body["box"]["article"] == "ALMOND 25KG"

    res = c.post(f"{URL}/requests/{RID}/scan", json={"header": {}, "box": _box_json(A2)})
    assert res.status_code == 200
    later = res.json()
    assert (later["transfer_id"], later["box_count"], later["created"]) == (body["transfer_id"], 2, False)

    for box, message in ((A2, f"Box {A2.box_id} is already on transfer {CHALLAN}."),
                         (PISTA, f"Box P-1 is PISTA, which is not on request {REQ}.")):
        res = c.post(f"{URL}/requests/{RID}/scan", json={"header": {}, "box": _box_json(box)})
        assert res.status_code == 409 and res.json()["message"] == message
    res = c.post(f"{URL}/requests/{RID + 9}/scan", json={"header": {}, "box": _box_json(CASHEW)})
    assert res.status_code == 404 and res.json()["message"] == "Request not found"
    assert c.post(f"{URL}/requests/{RID}/scan", json={"box": _box_json(CASHEW)}).status_code == 422

    # The request read shows it.
    res = c.get(f"{URL}/requests/{RID}")
    assert res.status_code == 200
    assert {k: res.json()[k] for k in ("status", "transfer_id", "transfer_challan_no", "transfer_status")} == {
        "status": "Transferred", "transfer_id": body["transfer_id"], "transfer_challan_no": CHALLAN,
        "transfer_status": "Partial"}

    # (Last: the client fixture signs every client in as the latest one's user.)
    res = client(db, ELSEWHERE).post(f"{URL}/requests/{RID}/scan", json={"header": {}, "box": _box_json(CASHEW)})
    assert res.status_code == 403 and res.json()["error"] == "forbidden"


def test_the_transfer_routes_as_a_client_sees_them(client):  # noqa: F811
    db = _db()
    _scan(db, A1)
    _scan(db, A2)
    tid = _transfer(db)["id"]
    c = client(db)
    key = {"box_id": A1.box_id, "transaction_no": TNO}

    res = c.patch(f"{URL}/transfers/{tid}/boxes", json={**key, "net_weight": "24.5", "gross_weight": 25.1})
    assert res.status_code == 200
    assert (res.json()["net_weight"], res.json()["gross_weight"]) == ("24.5", "25.1")
    res = c.patch(f"{URL}/transfers/{tid}/boxes", json={**key, "net_weight": 26, "gross_weight": 25})
    assert res.status_code == 422 and res.json()["message"] == f"Box {A1.box_id}: net weight exceeds gross"
    res = c.patch(f"{URL}/transfers/{tid}/boxes", json={"box_id": "P-1", "transaction_no": TNO,
                                                         "net_weight": 1, "gross_weight": 1})
    assert res.status_code == 404 and res.json()["message"] == f"Box P-1 is not on transfer {CHALLAN}."

    res = c.patch(f"{URL}/transfers/{tid}/transport", json={**TRANSPORT, "driver_name": "SURESH"})
    assert res.status_code == 200 and res.json() == {"transfer_id": tid, **TRANSPORT, "driver_name": "SURESH"}
    res = c.patch(f"{URL}/transfers/{tid}/transport", json={**TRANSPORT, "driver_name": " "})
    assert res.status_code == 422 and res.json()["message"] == "Fill in driver name."

    for body in ({}, {"all": True, "boxes": [key]}):
        res = c.post(f"{URL}/transfers/{tid}/remove-boxes", json=body)
        assert res.status_code == 422
        assert res.json()["message"] == "Name the boxes to remove, or send all: true (one of the two)."
    res = c.post(f"{URL}/transfers/{tid}/remove-boxes", json={"boxes": [{"box_id": A2.box_id, "transaction_no": TNO}]})
    assert res.status_code == 200
    assert res.json() == {"cancelled": False, "transfer_id": tid, "status": "Partial", "box_count": 1}
    for path, method, body in ((f"{URL}/transfers/{tid}/transport", "patch", TRANSPORT),
                               (f"{URL}/transfers/{tid}/boxes", "patch", {**key, "net_weight": 1, "gross_weight": 1}),
                               (f"{URL}/transfers/{tid}/remove-boxes", "post", {"all": True})):
        res = getattr(client(db, ELSEWHERE), method)(path, json=body)
        assert res.status_code == 403 and res.json()["error"] == "forbidden"
    c = client(db)                     # signed in as the store again (see above)
    res = c.post(f"{URL}/transfers/{tid}/remove-boxes", json={"all": True})
    assert res.status_code == 200 and res.json() == {"cancelled": True, "request_status": "Approved"}
    res = c.post(f"{URL}/transfers/{tid}/remove-boxes", json={"all": True})
    assert res.status_code == 404 and res.json()["message"] == "Transfer not found"


def test_a_received_or_plain_transfer_is_refused_as_a_client_sees_it(client):  # noqa: F811
    db = _db()
    _scan(db, A1)
    _received(db, "status")
    c = client(db)
    res = c.patch(f"{URL}/transfers/{_transfer(db)['id']}/transport", json=TRANSPORT)
    assert res.status_code == 409 and res.json()["message"] == RECEIVED
    res = c.post(f"{URL}/requests/{RID}/scan", json={"header": {}, "box": _box_json(A2)})
    assert res.status_code == 409 and res.json()["message"] == RECEIVED

    dl._create(db, dl._payload(request_id=None, from_wh="A185", to_wh="W202", boxes=[CASHEW]))
    (plain,) = [h["id"] for h in db.tables["interunit_transfers_header"] if h["request_id"] is None]
    res = c.post(f"{URL}/transfers/{plain}/remove-boxes", json={"all": True})
    assert res.status_code == 409 and res.json()["message"] == scan_service.USE_EDIT
