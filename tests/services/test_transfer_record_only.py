"""The transfer module is RECORD ONLY, and a box in transit is on one transfer at a time,
against the SQL-evaluating fake database of test_transfer_box_lookup_cold (with the
ORDER BY / FOR UPDATE support of test_transfer_dispatch_lock).

The stock tables ({cfpl,cdpl}_boxes_v2, _bulk_entry_boxes, _cold_stocks) are kept by
IMS. What is pinned:
  - where the box is, lookups: a box (box_id + transaction_no) in transit on a
    transfer that exists (Dispatch, Partial, or Received with no receipt yet) is
    refused (409) by each of the three lookups (manual box number, TR- QR, BE- QR)
    from every stock table that lookup reads, naming that transfer's challan — and
    found without the transfer; a box row of another transaction or of a transfer
    that is gone does not count (test_transfer_box_rules pins the received cases);
  - where the box is, POST / PUT /transfers: 409 listing each offending box and its
    challan (ten at most, then a count), before anything is written; quantity-only
    entries (no box_id / transaction_no, or 'DIRECT') pass; an edit keeps its own
    boxes and may add free ones, not another transfer's in-transit ones (nor one a
    legacy transfer still has in transit); a deleted transfer frees its boxes;
  - record only: no INSERT / UPDATE / DELETE on a stock table, and the stock rows as
    they were, on dispatch (each box copied from its row: source_table,
    source_row_id, cold snapshot), receipt (finalize, also to a cold destination and
    with the legacy cold_storage_items, which are ignored; close with shortage),
    re-open, a per-box edit of a receipt, delete receipt, edit transfer, cancel
    (delete transfer) and backfill — and what each does to the transfer records;
    re-open / delete receipt re-create the pending rows the dispatch made;
  - statically: in app/modules/transfer only the inner cold transfer writes a stock
    table (it edits cold-stock lot / location on purpose), and nothing writes ERP
    Material In's tables (po_header, po_line, po_section, po_box) or inventory_batch;
    material_in_source, which reads them, writes nothing at all.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_record_only.py
"""
from __future__ import annotations

import asyncio
import copy
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.modules.transfer import schemas
from app.modules.transfer.services import (
    create_service,
    delete_service,
    lookup_service,
    query_service,
    receive_service,
    reversal_service,
)
from tests.services import test_transfer_box_lookup_cold as cold
from tests.services.test_transfer_dispatch_lock import DispatchDB

USER = cold.USER
BOX, TNO = cold.BOX_ID, cold.TNO
TRANSFER_TABLES = ("interunit_transfers_header", "interunit_transfers_lines", "interunit_transfer_boxes",
                   "interunit_transfer_in_header", "interunit_transfer_in_boxes")
GUARD = "SELECT * FROM interunit_transfer_boxes WHERE btrim(box_id) = ANY($1::text[])"


class ReceiveDB(DispatchDB):
    """The fake database plus the one statement it cannot read: edit_transfer_in's
    correction of an in-transit row (a CASE over jsonb_set). It is applied here as the
    statement reads, so the per-box edit of a receipt can run end to end."""

    _CORRECTION = "UPDATE pending_transfer_stock SET lot_no = COALESCE($3, lot_no),"

    def _run(self, kind, sql, args):
        s = " ".join(sql.split())
        if not s.startswith(self._CORRECTION):
            return super()._run(kind, sql, args)
        self.calls.append((kind, s, args))
        tout, box_id, lot, article, net, gross = args
        n = 0
        for r in self.tables["pending_transfer_stock"]:
            if (r["transfer_out_id"], r["box_id"], r["status"]) == (tout, box_id, "In Transit"):
                r["lot_no"] = r["lot_no"] if lot is None else lot
                r["item_description"] = r["item_description"] if article is None else article
                r["net_weight"] = r["net_weight"] if net is None else net
                r["gross_weight"] = r["gross_weight"] if gross is None else gross
                r["weight_kg"] = r["weight_kg"] if net is None else net
                if net is not None and r["cold_storage_data"] is not None:
                    r["cold_storage_data"] = {**r["cold_storage_data"], "total_inventory_kgs": net}
                n += 1
        return [], f"UPDATE {n}"


@pytest.fixture(autouse=True)
def _fresh_existence_cache(monkeypatch):
    # lookup_service caches to_regclass answers for the process; each test starts clean.
    monkeypatch.setattr(lookup_service, "_EXISTS", {})


@pytest.fixture(autouse=True)
def no_readback(monkeypatch):
    async def get_transfer(conn, transfer_id):
        return {"id": transfer_id}
    monkeypatch.setattr(query_service, "get_transfer", get_transfer)


# ── Stock and boxes ──────────────────────────────────────────────────────────
COLD_ROW = {"id": 7301, "box_id": "55500001-4", "transaction_no": "TR-2026-0077"}


def _stock() -> dict:
    """One box in each kind of table: boxes_v2 (cfpl), bulk entry (cdpl), cold only (cdpl)."""
    return {"cfpl_boxes_v2": [cold._v2()], "cfpl_articles_v2": [cold._art()],
            "cdpl_bulk_entry_boxes": [cold._bulk()], "cdpl_cold_stocks": [cold._cold(**COLD_ROW)]}


def _db() -> ReceiveDB:
    return ReceiveDB(**{**{t: [] for t in (*cold.LOOKUP_TABLES, *TRANSFER_TABLES)}, **_stock()})


def _assert_stock_untouched(db):
    fresh = _db()
    assert {t: db.tables[t] for t in cold.STOCK_TABLES} == {t: fresh.tables[t] for t in cold.STOCK_TABLES}
    assert cold.stock_writes(db) == []


def _bx(n, box_id, tno, article="ALMOND 25KG", lot="LOT-9", net="25", gross="25.4"):
    return schemas.BoxCreate(box_number=n, box_id=box_id, article=article, lot_number=lot, batch_number="",
                             transaction_no=tno, net_weight=net, gross_weight=gross)


V2 = _bx(5, BOX, TNO)
BULK = _bx(3, "88120417-3", "BE-0007", article="CASHEW W320", lot="L-3", net="10", gross="10.2")
COLD = _bx(4, COLD_ROW["box_id"], COLD_ROW["transaction_no"], net="24.75", gross="24.75")
FREE = _bx(1, "99900001-1", "TR-2026-0100", gross="25")      # in no stock table


def _transfer(*boxes, challan, to_wh="W202") -> schemas.TransferCreate:
    return schemas.TransferCreate(
        header=schemas.TransferHeaderCreate(challan_no=challan, stock_trf_date="15-10-2026", from_warehouse="A185",
                                            to_warehouse=to_wh, vehicle_no="MH43BP6885"),
        lines=[schemas.TransferLineCreate(material_type="RM", item_category="NUTS", sub_category="ALMOND",
                                          item_description="ALMOND 25KG", quantity="3", pack_size="25",
                                          lot_number="LOT-9"),
               schemas.TransferLineCreate(material_type="RM", item_category="NUTS", sub_category="CASHEW",
                                          item_description="CASHEW W320", quantity="1", pack_size="10",
                                          lot_number="L-3")],
        boxes=list(boxes) or None,
    )


def _create(db, *boxes, challan="TRANS-A", to_wh="W202") -> int:
    return asyncio.run(create_service.create_transfer(db, _transfer(*boxes, challan=challan, to_wh=to_wh), USER))["id"]


def _update(db, transfer_id, *boxes, challan="TRANS-A"):
    return asyncio.run(create_service.update_transfer(db, transfer_id, _transfer(*boxes, challan=challan)))


def _header(db, transfer_id) -> dict:
    (h,) = [r for r in db.tables["interunit_transfers_header"] if r["id"] == transfer_id]
    return h


def _parked(db, transfer_id) -> dict:
    """The transfer's in-transit box rows by box_id, without their id and timestamp."""
    return {p["box_id"]: {k: v for k, v in p.items() if k not in ("id", "dispatched_at")}
            for p in db.tables["pending_transfer_stock"]
            if p["transfer_out_id"] == transfer_id and not p["box_id"].startswith("LINE-")}


def _refused(box: schemas.BoxCreate, challan) -> str:
    """The refusal of a box in transit on `challan`."""
    return f"Box {box.box_id} is already on challan {challan} (in transit)"


# ── Where the box is: the three lookups ─────────────────────────────────────
HELD, HELD_CHALLAN = 61250001, "TRANS202610140900"
SOURCE_ROWS = {
    "cfpl_boxes_v2": cold._v2, "cdpl_boxes_v2": cold._v2,
    "cfpl_bulk_entry_boxes": lambda: cold._bulk(box_id=BOX, transaction_no=TNO, box_number=5),
    "cdpl_bulk_entry_boxes": lambda: cold._bulk(box_id=BOX, transaction_no=TNO, box_number=5),
    "cfpl_cold_stocks": cold._cold, "cdpl_cold_stocks": cold._cold,
}
PATH_TABLES = {
    "manual": ("cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_cold_stocks", "cdpl_cold_stocks"),
    "tr": tuple(SOURCE_ROWS),
    "be": ("cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes", "cfpl_cold_stocks", "cdpl_cold_stocks"),
}
CASES = [(path, table) for path, tables in PATH_TABLES.items() for table in tables]


def _lookup_db(table, status=None, *, boxes=None) -> ReceiveDB:
    """The box in `table` only; with `status`, also on transfer HELD in that status
    (`boxes` overrides which transfer box rows exist)."""
    db = ReceiveDB(**{t: [] for t in (*cold.LOOKUP_TABLES, *TRANSFER_TABLES)})
    db.tables[table].append(SOURCE_ROWS[table]())
    if status:
        db.tables["interunit_transfers_header"].append(
            {"id": HELD, "challan_no": HELD_CHALLAN, "from_site": "A185", "to_site": "W202", "status": status,
             "created_ts": datetime(2026, 10, 14, 9, 0)})
        db.tables["interunit_transfer_boxes"] += boxes if boxes is not None else [
            {"id": 1, "header_id": HELD, "box_id": BOX, "transaction_no": TNO}]
    return db


# "Received" with no receipt (GRN) is still in transit; so is any other status.
@pytest.mark.parametrize("status", ["Dispatch", "Partial", "Received"])
@pytest.mark.parametrize("from_wh", [None, "W202"])
@pytest.mark.parametrize("path, table", CASES, ids=[f"{p}-{t}" for p, t in CASES])
def test_each_lookup_refuses_a_box_in_transit_from_every_table_it_reads(path, table, from_wh, status):
    db = _lookup_db(table, status)
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, path, from_wh=from_wh)
    assert exc.value.status_code == 409
    assert exc.value.detail == f"Box {BOX} is already on challan {HELD_CHALLAN} (in transit)"
    # One read of the transfers' boxes, for the box the lookup found.
    assert [(k, a) for k, s, a in db.calls if s.startswith(GUARD)] == [("fetch", ([BOX],))]
    assert cold.stock_writes(db) == []


@pytest.mark.parametrize("path, table", CASES, ids=[f"{p}-{t}" for p, t in CASES])
def test_each_lookup_finds_the_same_box_when_no_transfer_has_it(path, table):
    assert cold._lookup(_lookup_db(table), path)["box_id"] == BOX


@pytest.mark.parametrize("boxes", [
    [{"id": 1, "header_id": HELD, "box_id": BOX, "transaction_no": "TR-2026-0001"}],   # another inward's box
    [{"id": 1, "header_id": HELD + 7, "box_id": BOX, "transaction_no": TNO}],          # its transfer is gone
], ids=["other-transaction", "orphan-row"])
def test_a_lookup_ignores_a_box_row_that_is_not_this_box_on_a_transfer(boxes):
    db = _lookup_db("cfpl_boxes_v2", "Dispatch", boxes=boxes)
    assert cold._lookup(db, "tr")["box_id"] == BOX


@pytest.mark.parametrize("reason, site, text", [
    ("in_transit", "W202", "Box B-1 is already on challan (unknown) (in transit)"),
    ("elsewhere", None, "Box B-1 was received at (unknown) on challan (unknown); send it from (unknown)."),
    ("elsewhere", " A-185 ", "Box B-1 was received at A185 on challan (unknown); send it from A185."),
    ("short", "W202", "Box B-1 did not arrive on challan (unknown) (written off as short)."),
])
def test_the_refusal_text_without_a_challan_or_site(reason, site, text):
    hit = {"box_id": "B-1", "transaction_no": TNO, "reason": reason, "challan_no": None, "site": site}
    assert lookup_service.unavailable_message(hit) == text


# ── Where the box is: POST / PUT /transfers ─────────────────────────────────
@pytest.mark.parametrize("status", ["Dispatch", "Partial", "Received"])
def test_a_dispatch_with_boxes_in_transit_on_another_transfer_is_409_naming_them_and_writes_nothing(status):
    db = _db()
    a = _create(db, V2, COLD, challan="TRANS-A")
    _header(db, a)["status"] = status      # Received without a receipt: still in transit
    before = copy.deepcopy(db.tables)

    with pytest.raises(HTTPException) as exc:
        _create(db, FREE, V2, BULK, COLD, challan="TRANS-B")
    assert exc.value.status_code == 409
    assert exc.value.detail == f"{_refused(V2, 'TRANS-A')}. {_refused(COLD, 'TRANS-A')}."
    assert db.tables == before


def test_a_long_refusal_names_ten_boxes_and_counts_the_rest():
    boxes = [_bx(n, f"77700{n:03d}-{n}", "TR-2026-0200") for n in range(1, 13)]
    db = _db()
    _create(db, *boxes, challan="TRANS-A")
    with pytest.raises(HTTPException) as exc:
        _create(db, *reversed(boxes), challan="TRANS-B")
    assert exc.value.detail == " ".join(
        [f"{_refused(b, 'TRANS-A')}." for b in reversed(boxes)][:10] + ["And 2 more."])


def test_quantity_only_entries_are_never_refused():
    entries = [_bx(1, "", "DIRECT"), _bx(2, "X-1", "DIRECT"), _bx(3, None, None)]
    db = _db()
    _create(db, *entries, challan="TRANS-A")
    _create(db, *entries, challan="TRANS-B")
    assert [h["challan_no"] for h in db.tables["interunit_transfers_header"]] == ["TRANS-A", "TRANS-B"]
    assert not db.statements(GUARD)       # nothing to look up


def test_an_edit_that_adds_a_box_on_another_transfer_is_409_and_changes_nothing():
    db = _db()
    a = _create(db, V2, challan="TRANS-A")
    _create(db, COLD, challan="TRANS-B")
    before = copy.deepcopy(db.tables)

    with pytest.raises(HTTPException) as exc:
        _update(db, a, V2, COLD)
    # (TRANS-B ships 1 box of the 4 ordered: Partial, in transit.)
    assert exc.value.status_code == 409 and exc.value.detail == _refused(COLD, "TRANS-B")
    assert db.tables == before


def test_an_edit_keeps_its_own_boxes_drops_some_and_adds_free_ones_without_touching_stock():
    db = _db()
    a = _create(db, V2, COLD)
    _update(db, a, V2, BULK)

    assert {k: (p["source_table"], p["source_row_id"]) for k, p in _parked(db, a).items()} == {
        V2.box_id: ("cfpl_boxes_v2", 11), BULK.box_id: ("cdpl_bulk_entry_boxes", 31)}
    assert {b["box_id"] for b in db.tables["interunit_transfer_boxes"]} == {V2.box_id, BULK.box_id}
    _assert_stock_untouched(db)
    # The dropped cold box is free again; the kept one is not.
    assert cold._lookup(db, "be", box_id=COLD.box_id, tno=COLD.transaction_no)["box_id"] == COLD.box_id
    with pytest.raises(HTTPException):
        cold._lookup(db, "tr")


@pytest.mark.parametrize("old_status", ["Dispatch", "Received"])
def test_an_edit_is_judged_by_the_latest_other_transfer_of_its_box(old_status):
    db = _db()
    a = _create(db, V2)
    # From before the guard: another, older transfer with the same box, to A185.
    db.tables["interunit_transfers_header"].append(
        {"id": 1, "challan_no": "TRANS-OLD", "from_site": "W202", "to_site": "A-185", "status": old_status,
         "created_ts": datetime(2026, 9, 1)})
    db.tables["interunit_transfer_boxes"].append({"id": 2, "header_id": 1, "box_id": BOX, "transaction_no": TNO})
    before = copy.deepcopy(db.tables)

    if old_status == "Dispatch":
        # Still in transit there: the edit may not keep it either.
        with pytest.raises(HTTPException) as exc:
            _update(db, a, V2)
        assert exc.value.status_code == 409 and exc.value.detail == _refused(V2, "TRANS-OLD")
        assert db.tables == before
    else:
        # Received at A185 (this edit's From site) and acknowledged there: it may go on.
        db.tables["interunit_transfer_in_header"].append({"id": 3, "transfer_out_id": 1, "status": "Received"})
        db.tables["interunit_transfer_in_boxes"].append({"id": 4, "header_id": 3, "box_id": BOX, "transaction_no": TNO})
        _update(db, a, V2)
        assert set(_parked(db, a)) == {BOX}
    # A new transfer may not take it: TRANS-A is its latest transfer, and has it in transit.
    with pytest.raises(HTTPException) as exc:
        _create(db, V2, challan="TRANS-B")
    assert exc.value.detail == _refused(V2, "TRANS-A")


def test_a_deleted_transfer_frees_its_boxes():
    db = _db()
    a = _create(db, V2, COLD, challan="TRANS-A")
    first = _parked(db, a)
    with pytest.raises(HTTPException):
        cold._lookup(db, "tr")

    asyncio.run(delete_service.delete_transfer(db, a))
    assert all(db.tables[t] == [] for t in TRANSFER_TABLES) and db.tables["pending_transfer_stock"] == []
    assert cold._lookup(db, "tr")["box_id"] == BOX
    assert cold._lookup(db, "manual", box_number=4, tno=COLD.transaction_no)["box_id"] == COLD.box_id

    b = _create(db, V2, COLD, challan="TRANS-B")
    again = _parked(db, b)
    for box_id, p in first.items():
        assert {**again[box_id], "transfer_out_id": a, "transfer_out_challan_no": "TRANS-A"} == p
    _assert_stock_untouched(db)


# ── Record only: dispatch, receipt and every undo ───────────────────────────
def test_a_dispatch_copies_each_box_from_its_stock_row_and_writes_no_stock():
    db = _db()
    a = _create(db, V2, BULK, COLD, FREE)

    assert {k: (p["source_table"], p["source_row_id"], p["cold_storage_data"] is not None)
            for k, p in _parked(db, a).items()} == {
        V2.box_id: ("cfpl_boxes_v2", 11, False),
        BULK.box_id: ("cdpl_bulk_entry_boxes", 31, False),
        COLD.box_id: ("cdpl_cold_stocks", 7301, True),
        FREE.box_id: ("cfpl_bulk_entry_boxes", None, False),    # no stock row: a guessed source
    }
    _assert_stock_untouched(db)


TI = 71250001
RECEIVED_AT = datetime(2026, 10, 16, 9, 0)
# What the reference's legacy fallback turned into cold_stocks rows on finalize.
LEGACY_COLD_ITEM = {"cold_company": "cfpl", "item_description": "ALMOND 25KG", "lot_no": "LOT-9", "rate": 812.5,
                    "box_details": [{"box_id": BOX, "transaction_no": TNO, "weight_kg": 25}]}


def _receipt(db, transfer_id, *, only=None) -> int:
    """A Pending GRN for the transfer with its boxes acknowledged (all, or `only`)."""
    h = _header(db, transfer_id)
    db.tables["interunit_transfer_in_header"].append({
        "id": TI, "transfer_out_id": transfer_id, "transfer_out_no": h["challan_no"], "grn_number": "GRN-0001",
        "grn_date": RECEIVED_AT, "receiving_warehouse": h["to_site"], "received_by": USER,
        "received_at": RECEIVED_AT, "box_condition": "Good", "condition_remarks": None, "status": "Pending",
        "created_at": RECEIVED_AT, "updated_at": None})
    out = [b for b in db.tables["interunit_transfer_boxes"] if b["header_id"] == transfer_id]
    for n, b in enumerate(b for b in out if only is None or b["box_id"] in only):
        db.tables["interunit_transfer_in_boxes"].append({
            "id": TI + 1 + n, "header_id": TI, "box_id": b["box_id"], "article": b["article"],
            "batch_number": b["batch_number"], "lot_number": b["lot_number"], "transaction_no": b["transaction_no"],
            "net_weight": b["net_weight"], "gross_weight": b["gross_weight"],
            "scanned_at": RECEIVED_AT + timedelta(minutes=n), "is_matched": True, "transfer_out_box_id": None,
            "issue": None, "line_index": None, "inward_box_id": None})
    return TI


def _finalize(db):
    data = schemas.FinalizeTransferIn(box_condition="Good", cold_storage_items=[LEGACY_COLD_ITEM])
    return asyncio.run(receive_service.finalize_transfer_in(db, TI, data))


def _received(db) -> tuple[int, dict]:
    """A transfer to a cold store (where the reference posted cold_stocks rows), received
    in full; returns its id and what its dispatch parked."""
    a = _create(db, V2, BULK, COLD, to_wh="Rishi")
    parked = _parked(db, a)
    _receipt(db, a)
    _finalize(db)
    return a, parked


def test_finalize_clears_the_in_transit_rows_and_posts_nothing_even_to_a_cold_store():
    db = _db()
    a = _create(db, V2, BULK, COLD, to_wh="Rishi")
    # Rishi's cold stock is cdpl's, whichever company's table a box came from.
    assert {p["destination_table"] for p in _parked(db, a).values()} == {"cdpl_cold_stocks"}
    _receipt(db, a)

    out = _finalize(db)
    assert out["status"] == "Received" and len(out["boxes"]) == 3
    assert _header(db, a)["status"] == "Received"
    assert db.tables["pending_transfer_stock"] == []
    _assert_stock_untouched(db)          # the legacy cold_storage_items were ignored too


def test_the_legacy_cold_storage_items_are_ignored_when_nothing_is_in_transit():
    # Dispatched before pending_transfer_stock existed: no pending rows; the reference
    # inserted the client's cold_storage_items into cold_stocks here.
    db = _db()
    db.tables["interunit_transfers_header"].append(
        {"id": 5, "challan_no": "TRANS-OLD", "from_site": "A185", "to_site": "Rishi", "status": "Dispatch"})
    db.tables["interunit_transfer_boxes"].append(
        {"id": 6, "header_id": 5, "box_id": BOX, "article": "ALMOND 25KG", "batch_number": "", "lot_number": "LOT-9",
         "transaction_no": TNO, "net_weight": Decimal("25"), "gross_weight": Decimal("25.4")})
    _receipt(db, 5)

    assert _finalize(db)["status"] == "Received" and _header(db, 5)["status"] == "Received"
    _assert_stock_untouched(db)


def test_close_with_shortage_clears_the_received_boxes_writes_off_the_rest_and_posts_nothing():
    db = _db()
    a = _create(db, V2, BULK, COLD, to_wh="Rishi")
    _receipt(db, a, only={V2.box_id, COLD.box_id})

    out = asyncio.run(receive_service.close_with_shortage(db, TI, "one bag short", USER))
    assert out["shortage_written_off"] == 1
    assert (out["status"], out["box_condition"]) == ("Received", "Partial")
    assert out["condition_remarks"] == "Closed with shortage: 1 box(es) written off. Reason: one bag short"
    assert _header(db, a)["status"] == "Received" and db.tables["pending_transfer_stock"] == []
    _assert_stock_untouched(db)


def test_reopen_puts_the_boxes_back_in_transit_as_dispatched_and_takes_no_stock():
    db = _db()
    a, parked = _received(db)

    out = asyncio.run(receive_service.reopen_transfer_in(db, TI, USER))
    assert out["status"] == "Pending" and len(out["boxes"]) == 3      # the GRN and its boxes are kept
    assert _header(db, a)["status"] == "Dispatch"
    assert _parked(db, a) == parked
    _assert_stock_untouched(db)


def test_a_per_box_edit_of_a_receipt_receives_again_without_touching_stock():
    db = _db()
    a, _ = _received(db)
    edit = schemas.EditTransferIn(boxes=[schemas.EditTransferInBox(box_id=COLD.box_id, lot_number="LOT-FIX",
                                                                   net_weight=24.5)])

    out = asyncio.run(receive_service.edit_transfer_in(db, TI, edit, USER))
    assert out["status"] == "Received" and _header(db, a)["status"] == "Received"
    (fixed,) = [b for b in db.tables["interunit_transfer_in_boxes"] if b["box_id"] == COLD.box_id]
    assert (fixed["lot_number"], fixed["net_weight"]) == ("LOT-FIX", Decimal("24.5"))
    assert db.statements(ReceiveDB._CORRECTION)        # its in-transit row was corrected...
    assert db.tables["pending_transfer_stock"] == []  # ...and cleared again on the re-receipt
    _assert_stock_untouched(db)


def test_deleting_a_receipt_puts_the_boxes_back_in_transit_and_takes_no_stock():
    db = _db()
    a, parked = _received(db)

    asyncio.run(delete_service.delete_transfer_in(db, TI, USER))
    assert db.tables["interunit_transfer_in_header"] == [] and db.tables["interunit_transfer_in_boxes"] == []
    assert _header(db, a)["status"] == "Dispatch"
    assert _parked(db, a) == parked
    _assert_stock_untouched(db)
    # Still on its transfer, so still not free.
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, "tr")
    assert exc.value.status_code == 409


def test_cancelling_a_received_transfer_drops_its_records_and_frees_its_boxes():
    db = _db()
    a, _ = _received(db)

    asyncio.run(delete_service.delete_transfer(db, a))
    assert all(db.tables[t] == [] for t in TRANSFER_TABLES) and db.tables["pending_transfer_stock"] == []
    assert cold._lookup(db, "tr")["box_id"] == BOX
    _assert_stock_untouched(db)


def test_backfill_copies_the_boxes_of_old_transfers_and_writes_no_stock():
    db = _db()
    db.tables["interunit_transfers_header"].append(
        {"id": 5, "challan_no": "TRANS-OLD", "from_site": "A185", "to_site": "W202", "status": "Dispatch",
         "created_by": USER, "created_ts": datetime(2026, 9, 1)})
    db.tables["interunit_transfer_boxes"].append(
        {"id": 6, "header_id": 5, "box_id": BOX, "article": "ALMOND 25KG", "lot_number": "LOT-9",
         "batch_number": "", "transaction_no": TNO, "net_weight": Decimal("25"), "gross_weight": Decimal("25.4")})

    s = asyncio.run(reversal_service.backfill_pending_from_existing_transfers(db))
    assert s["boxes_parked_from_warehouse"] == 1
    assert {k: (p["source_table"], p["source_row_id"]) for k, p in _parked(db, 5).items()} == {
        BOX: ("cfpl_boxes_v2", 11)}
    _assert_stock_untouched(db)


# ── Statically: who may write a stock table ─────────────────────────────────
MODULE = Path(__file__).resolve().parents[2] / "app" / "modules" / "transfer"
# A write whose table is a stock table, or is interpolated ({...}): the services
# interpolate only stock tables, so a new interpolated write is flagged for a look.
_STOCK_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:\{|(?:cfpl|cdpl)_(?:boxes_v2|bulk_entry_boxes|cold_stocks)\b)")


def test_only_the_inner_cold_transfer_writes_a_stock_table():
    writers = {p.relative_to(MODULE).as_posix() for p in MODULE.rglob("*.py")
               if _STOCK_WRITE.search(p.read_text(encoding="utf-8"))}
    assert writers == {"services/inner_cold_service.py"}


# ERP Material In's tables and its inventory_batch copy are the purchase module's.
_MATERIAL_IN_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:po_(?:header|line|section|box)|inventory_batch)\b")


def test_nothing_in_the_transfer_module_writes_material_in():
    writers = {p.relative_to(MODULE).as_posix() for p in MODULE.rglob("*.py")
               if _MATERIAL_IN_WRITE.search(p.read_text(encoding="utf-8"))}
    assert writers == set()
    source = (MODULE / "services" / "material_in_source.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE)\b|\.execute\(", source)
