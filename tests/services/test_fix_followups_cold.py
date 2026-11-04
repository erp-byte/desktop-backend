"""Follow-ups of the site / box audit fixes on the cold side, found in review
(2026-10-16): the store a cold row is at, the cold search and pick, and the Inner Cold
transfer — against the evaluating cold-table fake of test_cold_pick_and_search_r3 and
the SQL-evaluating fake of test_inner_cold_transfer.

What is pinned:
  - one rule for the store a cold-stock row is at (sites.cold_row_site): its unit when
    that names a cold store, else its storage location. The cold search's location
    filter, the Inner Cold service (_row_store) and from_cold_unit all read a row so;
    'Savla Bond' and 'Cold Storage' are never a store's spelling;
  - the search's available_here leaves out the transfer being edited, as the pick does,
    so the Edit page's maximum is what the pick may return; the route passes it;
  - the pick keeps to the picked search row's item_mark (blank included), as the search
    groups by it; without one, as before;
  - Inner Cold: without a line company, the payload's company names the picked row's
    table (ids collide across the two cold tables); a split row copies the row's own
    columns that only some cold tables have; the group's boxes are advisory-locked as a
    dispatch locks them, before R3 is asked, on the create and on the undo.

Run:  PYTHONPATH=. python -m pytest tests/services/test_fix_followups_cold.py
"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest

from app.core import sites
from app.modules.cold_storage.services import search_service
from app.modules.transfer import schemas
from app.modules.transfer.services import create_service, inner_cold_service, lookup_service
from tests.services import test_cold_pick_and_search_r3 as cp
from tests.services import test_inner_cold_transfer as ict

LOCK = "interunit_transfer_box|{}|{}"


@pytest.fixture
def judge(monkeypatch):
    """lookup_service.unavailable_boxes as cp's stand-in (cp.R3) judges it."""
    r3 = cp.R3()
    monkeypatch.setattr(lookup_service, "unavailable_boxes", r3)
    return r3


@pytest.fixture
def locked_first(monkeypatch):
    """lookup_service.unavailable_boxes, stubbed to free every box and to record, per
    call, whether each box it is asked about was advisory-locked before it was asked
    (create_service.lock_boxes on the fake's box_locks)."""
    calls: list = []

    async def unavailable_boxes(conn, boxes, *, from_warehouse=None, transfer_id=None):
        held = {k for keys in conn.box_locks for k in keys}
        calls.append({(b, t): LOCK.format(b, t) in held for b, t in boxes})
        return []

    monkeypatch.setattr(lookup_service, "unavailable_boxes", unavailable_boxes)
    return calls


# ── The store a cold row is at ───────────────────────────────────────────────
@pytest.mark.parametrize("unit, location, store", [
    ("D-39", "Savla Bond", "Savla D-39"),          # unit first, as IMS' canonical_warehouse_fn
    ("D-39", "Savla", "Savla D-39"),
    ("Savla", "Savla Bond", "Savla Bond"),          # bare 'Savla' names no store: the location does
    ("Savla", "Savla D-39 Cold Storage", "Savla D-39"),
    ("", "Rishi Cold Storage", "Rishi"),
    (None, "Supreme Cold", "Supreme"),
    ("Cold Storage", "Eskimo Cold", "Eskimo"),       # 'Cold Storage' is cold in kind, no one store
    ("Supreme Cold", None, "Supreme"),
    ("A185 Cold", "Savla D-39 Chamber 3", ""),       # a floor, and a chamber: neither is a store
    ("Savla", "Cold Storage", ""),
    (None, None, ""),
])
def test_cold_row_site_is_the_unit_else_the_storage_location(unit, location, store):
    assert sites.cold_row_site(unit, location) == store
    assert inner_cold_service._row_store({"unit": unit, "storage_location": location}) == store


ROWS = [
    cp._row(1, "K-1", unit="D-39", storage_location="Savla Bond", inward_no="I-1", inward_dt=date(2026, 9, 1)),
    cp._row(2, "K-2", unit="Savla", storage_location="Savla Bond", inward_no="I-2", inward_dt=date(2026, 9, 2)),
    cp._row(3, "K-3", unit="Savla", storage_location="Savla D-39", inward_no="I-3", inward_dt=date(2026, 9, 3)),
    cp._row(4, "K-4", unit=None, storage_location="Cold Storage", inward_no="I-4", inward_dt=date(2026, 9, 4)),
    cp._row(5, "K-5", unit="Rishi", storage_location="Cold Storage", inward_no="I-5", inward_dt=date(2026, 9, 5)),
    cp._row(6, "K-6", unit="", storage_location="Rishi Cold Storage", inward_no="I-6", inward_dt=date(2026, 9, 6)),
    cp._row(7, "K-7", unit="Old Savla", storage_location="Chamber 2", inward_no="I-7", inward_dt=date(2026, 9, 7)),
    cp._row(8, "K-8", unit="Supreme Cold", storage_location="Rishi", inward_no="I-8", inward_dt=date(2026, 9, 8)),
]


@pytest.mark.parametrize("store", [*sites.cold_sites(), "Cold Storage", "W202"])
def test_the_search_location_filter_finds_the_rows_cold_row_site_puts_there(store, judge):
    db = cp.ColdDB(cdpl_cold_stocks=ROWS, cfpl_cold_stocks=[])
    found = asyncio.run(search_service.search_cold_storage_stocks(db, storage_location=store))["results"]
    rows_there = [r["inward_no"] for r in ROWS
                  if sites.same_site(sites.cold_row_site(r["unit"], r["storage_location"]), store)
                  or (not sites.cold_row_site(r["unit"], None)
                      and not sites.cold_company(store) and sites.same_site(r["storage_location"], store))]
    assert [r["inward_no"] for r in found] == rows_there


def test_savla_bond_and_cold_storage_rows_are_found_under_their_own_names_only_when_no_unit_says_otherwise(judge):
    db = cp.ColdDB(cdpl_cold_stocks=ROWS, cfpl_cold_stocks=[])

    def at(store):
        return [r["inward_no"] for r in asyncio.run(
            search_service.search_cold_storage_stocks(db, storage_location=store))["results"]]

    assert at("Savla Bond") == ["I-2"]                     # I-1 is unit D-39's
    assert at("Savla D-39") == ["I-1", "I-3", "I-7"]
    assert at("Cold Storage") == ["I-4"]                   # I-5 is unit Rishi's
    assert at("Rishi") == ["I-5", "I-6"]
    assert at("Supreme") == ["I-8"]


# ── available_here on the Edit page ─────────────────────────────────────────
def test_available_here_leaves_out_the_transfer_being_edited_as_the_pick_does(judge):
    rows = [cp._row(101 + n, f"E-{n}") for n in range(3)]
    judge.history = {(f"E-{n}", "TR-1"): (9, "in_transit", "A185") for n in range(3)}
    db = cp.ColdDB(cdpl_cold_stocks=rows, cfpl_cold_stocks=[])

    def available(**kw):
        (lot,) = asyncio.run(search_service.search_cold_storage_stocks(
            db, lot_no="LOT-9", from_warehouse="Rishi", **kw))["results"]
        return lot["available_here"]

    picked = asyncio.run(search_service.pick_boxes(
        db, company="cdpl", item_description="ALMOND 25KG", lot_no="LOT-9", inward_no="INW-881", qty=3,
        from_warehouse="Rishi", transfer_id=9, **cp.RISHI))["boxes"]
    assert available(transfer_id=9) == len(picked) == 3
    assert available() == 0                                # on another dispatch they are in transit
    assert [(f, t) for _, f, t in judge.calls] == [("Rishi", 9), ("Rishi", 9), ("Rishi", None)]


def test_the_search_route_passes_the_edited_transfer_through(monkeypatch):
    client, seen = cp._client(monkeypatch, "search_cold_storage_stocks")
    assert client.get("/api/v1/cold-storage/stocks/search",
                      params={"q": "almond", "from_warehouse": "Rishi", "transfer_id": 51260001}).status_code == 200
    assert (seen["from_warehouse"], seen["transfer_id"]) == ("Rishi", 51260001)
    client.get("/api/v1/cold-storage/stocks/search", params={"q": "almond"})
    assert seen["transfer_id"] is None


# ── The pick keeps to the search row's mark ──────────────────────────────────
MARKED = [cp._row(1, "M-1", item_mark="RK"), cp._row(2, "M-2", item_mark="XY"), cp._row(3, "M-3", item_mark=None),
          cp._row(4, "M-4", item_mark="XY")]


@pytest.mark.parametrize("mark, boxes", [("XY", ["M-2", "M-4"]), ("RK", ["M-1"]), ("", ["M-3"]),
                                         (None, ["M-1", "M-2", "M-3", "M-4"])])
def test_the_pick_keeps_to_the_picked_rows_item_mark(mark, boxes, judge):
    db = cp.ColdDB(cdpl_cold_stocks=MARKED, cfpl_cold_stocks=[])
    assert cp._pick(db, 9, item_mark=mark, **cp.RISHI) == boxes
    ((sql, args),) = db.statements("ORDER BY id ASC")
    assert ("COALESCE(item_mark, '') = $6" in sql) is (mark is not None)
    if mark is not None:
        assert args[5] == mark


# ── Inner Cold: the picked row's table ───────────────────────────────────────
def _two_tables(cfpl_lot="L-1", cdpl_lot="L-1"):
    """Row id 10 in both cold tables, both at Rishi (a CFPL lot kept at Rishi too)."""
    return ict._db([ict._row(10, unit="Rishi", location="Rishi", lot=cfpl_lot)],
                   [ict._row(10, unit="Rishi", location="Rishi", lot=cdpl_lot)])


def _payload(company, *, line_company=None):
    payload = ict._payload(ict._line(10, 1, company=line_company), frm="Rishi")
    payload.company = company
    return payload


@pytest.mark.parametrize("company, relabelled, kept", [("cfpl", "cfpl_cold_stocks", "cdpl_cold_stocks"),
                                                       ("cdpl", "cdpl_cold_stocks", "cfpl_cold_stocks")])
def test_without_a_line_company_the_payloads_company_names_the_picked_rows_table(company, relabelled, kept,
                                                                                locked_first):
    db = _two_tables()
    assert ict._create(db, _payload(company), ict.ANYONE)["status"] == "success"
    assert ict._lots(db, relabelled) == {10: "L-2"} and ict._lots(db, kept) == {10: "L-1"}


def test_a_line_company_wins_over_the_payloads(locked_first):
    db = _two_tables()
    assert ict._create(db, _payload("cfpl", line_company="cdpl"), ict.ANYONE)["status"] == "success"
    assert ict._lots(db, "cdpl_cold_stocks") == {10: "L-2"} and ict._lots(db) == {10: "L-1"}


def test_the_payloads_company_is_only_a_preference_the_store_still_decides(locked_first):
    # The payload names cfpl, whose row 10 is at Savla D-39: the Rishi row of cdpl is the one.
    db = ict._db([ict._row(10)], [ict._row(10, unit="Rishi", location="Rishi")])
    assert ict._create(db, _payload("cfpl"), ict.ANYONE)["status"] == "success"
    assert ict._lots(db, "cdpl_cold_stocks") == {10: "L-2"} and ict._lots(db) == {10: "L-1"}


# ── Inner Cold: a split row's own columns ────────────────────────────────────
EXTRA = {"cold_item_mark": "CM-7", "spl_remarks": "keep dry", "inward_transaction_no": "TR-2026-0333",
         "auto_created_from_inward": True}


def test_a_split_row_copies_the_columns_only_some_cold_tables_have(locked_first):
    loose = ict._row(11, box="", cartons=3, **EXTRA)
    db = ict._db([loose])
    assert ict._create(db, ict._payload(ict._line(11, 2)))["status"] == "success"
    (new,) = [r for r in db.tables["cfpl_cold_stocks"] if r["id"] != 11]
    assert {c: new[c] for c in EXTRA} == EXTRA
    assert (new["lot_no"], new["no_of_cartons"], new["box_id"]) == ("L-2", 2, None)
    (kept,) = [r for r in db.tables["cfpl_cold_stocks"] if r["id"] == 11]
    assert (kept["lot_no"], kept["no_of_cartons"]) == ("L-1", Decimal(1))


def test_a_split_in_a_table_without_those_columns_names_none_of_them(locked_first):
    db = ict._db([ict._row(11, box="", cartons=3)])
    assert ict._create(db, ict._payload(ict._line(11, 2)))["status"] == "success"
    (insert,) = [s for _, s, _ in db.calls if s.startswith("INSERT INTO cfpl_cold_stocks")]
    assert not any(c in insert for c in EXTRA)


# ── Inner Cold: the boxes are locked before R3 is asked ─────────────────────
def test_the_create_and_the_undo_lock_the_groups_boxes_before_asking_r3(locked_first):
    db = ict._db([ict._row(10), ict._row(11), ict._row(12, box="", cartons=2)])
    assert ict._create(db, ict._payload(ict._line(10, 2)))["status"] == "success"
    boxes = {(f"3473{n:04d}-1", "TR-2026-0001") for n in (10, 11)}
    (asked,) = locked_first
    assert set(asked) == boxes and all(asked.values())             # every real box, locked first
    assert db.box_locks == [sorted(LOCK.format(*k) for k in boxes)]

    ict._delete(db)
    undo = locked_first[-1]
    assert set(undo) == boxes and all(undo.values())
    assert len(db.box_locks) == 2


def test_lock_boxes_takes_one_lock_per_real_box_and_none_for_no_box():
    db = ict._db()

    async def go(keys):
        async with db.transaction():
            await create_service.lock_boxes(db, keys)

    asyncio.run(go([("B-2", "T"), None, ("B-1", "T"), ("B-2", "T")]))
    asyncio.run(go([None]))
    assert db.box_locks == [[LOCK.format("B-1", "T"), LOCK.format("B-2", "T")]]


def test_the_inner_cold_line_schema_still_takes_a_company():
    line = schemas.InnerTransferLine(stock_record_id=1, company="CDPL", quantity=1, old_lot_number="a",
                                     new_lot_number="b")
    assert inner_cold_service._company(line.company) == "cdpl" and inner_cold_service._company("x") is None
