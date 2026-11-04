"""Where a box is (R3) by site and by when the box joined its transfers, the IMS receipts and
older statuses it reads, how a receipt acknowledges a box, the in-transit rows a save takes
over, and the receipt lock on a transfer's own To site — against the SQL-evaluating fake
database of test_transfer_box_lookup_cold (with the FOR UPDATE / ORDER BY support of
test_transfer_dispatch_lock and the helpers of test_transfer_record_only and
test_transfer_box_rules).

What is pinned:
  - sites (P1-01, P1-24, D5): a box received at a site goes on from any spelling of that
    site ('Warehouse A185' / 'A185', 'Supreme Cold' / 'Supreme', 'D-39' / 'Savla D-39',
    'ESKIMO' / 'Eskimo') and from no other, the refusal naming the site canonically;
    'Cold Storage' and 'Savla Bond' are sites of their own. A request's route matches in
    any spelling; a new transfer and its in-transit rows name their sites canonically, an
    edit keeps the stored spelling of a leg it does not change;
  - when the box joined (P2-01): every box row records it (created_at), an edit keeps a
    kept box's, and a box's history is ordered by it, never earlier than its transfer's
    created_ts: a box scanned onto an older transfer is judged by that transfer, and
    undoing a receipt does not put back a box that joined another transfer since;
  - IMS (P2-04, P2-10): a box IMS's cold receive took in is at the To site whatever the
    statuses say, and counts with an ERP receipt of the rest; a box missing from it is
    written off once the transfer is received; 'Completed' / 'In Transit' are in transit
    (received with a Received receipt); 'Partially Received' has its acknowledged boxes at
    the To site and the rest in transit; such older in-transit transfers can be edited;
  - acknowledgements (P2-13, P2-17): by transfer_out_box_id, by box_id + transaction_no
    (trimmed; a row without a transaction by box_id), by IMS's original_box_id;
    acknowledge records a box of the transfer by its box row and refuses one that is not;
    two boxes sharing a box_id never overwrite each other (the second is refused where the
    database keeps one row per box_id); close with shortage and un-acknowledge go box by
    box; a box row IMS stored padded is in the history;
  - in-transit rows (P2-05, P2-11, P2-12): a live save takes over a stale row of another
    transfer, a backfill does not; a backfill parks no box that went on since or that IMS
    received, and no transfer IMS received in full; undoing a receipt re-parks every box
    of the transfer (the receipt's values for those it acknowledged) and none that is not
    on it;
  - edits (P2-06, and X-05's service half): a received transfer, or one with a receipt
    begun in either ledger, is 409 with nothing written, checked with its header row
    locked; given its user, an edit is locked to the stored From site and, for a transfer
    without a request, to its route unless an admin edits it;
  - site kind and companies (P1-10, P1-22): cold by kind ('Eskimo', 'Savla Bond', 'D-39',
    'Cold Storage'; not 'A185 Cold'), from_cold_unit by canonical names, the companies of a
    box with no stock row and of a cold To from the site table;
  - receipts (P1-13): begun only by a user of the transfer's stored To site (any spelling),
    whatever receiving_warehouse is sent, and stored with the To site's canonical name;
    the two create routes do not trust the body; an edit keeps a receipt at its
    transfer's To site.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_r3_sites.py
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from decimal import Decimal

import pytest
from asyncpg.exceptions import UniqueViolationError
from fastapi import HTTPException

from app.core.middleware.request_context import AuthError
from app.modules.transfer import schemas
from app.modules.transfer.services import (
    create_service,
    lookup_service,
    query_service,
    receive_service,
    reversal_service,
)
from tests.services import test_transfer_box_lookup_cold as cold
from tests.services import test_transfer_box_rules as br
from tests.services import test_transfer_dispatch_lock as dl
from tests.services import test_transfer_record_only as ro
from tests.services import test_transfer_request_decisions as trd
from tests.services.test_transfer_request_decisions import client  # noqa: F401  (the HTTP fixture)

USER, BOX, TNO = ro.USER, ro.BOX, ro.TNO
V2, BULK, COLD = ro.V2, ro.BULK, ro.COLD
BULK_KEY, COLD_KEY = (BULK.box_id, BULK.transaction_no), (COLD.box_id, COLD.transaction_no)
OTHER_TNO = "TR-2026-0999"               # another inward's transaction, same box id
RECEIVER = trd._user("sunil.jasoria@candorfoods.in", warehouses=["A185"])     # may receive; locked to A185
URL = "/api/v1/transfer"
_oct = br._oct


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
class _Db(ro.ReceiveDB):
    """ro's fake database plus the defaults the receipt tables give a new row
    (created_at / updated_at; a receipt box's inward_box_id), which receipt reads return."""

    _DEFAULTS = {
        "interunit_transfer_in_header": (("created_at", ("now",)), ("updated_at", ("now",))),
        "interunit_transfer_in_boxes": (("created_at", ("now",)), ("updated_at", ("now",)),
                                        ("inward_box_id", ("lit", None))),
    }

    def _insert(self, st, args):
        extra = [(c, e) for c, e in self._DEFAULTS.get(st["table"], ()) if c not in st["cols"]]
        if extra:
            st = {**st, "cols": (*st["cols"], *(c for c, _ in extra)), "vals": [*st["vals"], *(e for _, e in extra)]}
        return super()._insert(st, args)


class _OneRowPerBoxId(_Db):
    """_Db with the unique index IMS made on interunit_transfer_in_boxes (header_id, box_id)."""

    def _insert(self, st, args):
        if st["table"] == "interunit_transfer_in_boxes":
            row = {c: cold._eval(e, {}, args) for c, e in zip(st["cols"], st["vals"])}
            if any((r["header_id"], r["box_id"]) == (row["header_id"], row["box_id"])
                   for r in self.tables[st["table"]]):
                exc = UniqueViolationError('duplicate key value violates unique constraint '
                                           '"uq_transfer_in_boxes_header_box"')
                exc.constraint_name = "uq_transfer_in_boxes_header_box"
                raise exc
        return super()._insert(st, args)


def _db(cls=_Db, *, ims=False) -> _Db:
    """ro's database (its stock, empty transfer and receipt tables); with `ims`, also
    IMS's cold receipt tables."""
    db = cls(**ro._db().tables)
    if ims:
        db.tables.update(cold_transfer_in_headers=[], cold_transfer_inboxes=[])
    return db


def _transfer(db, tid, challan, *, to, status, created, frm="W909", boxes=((BOX, TNO),), joined=None) -> int:
    """A transfer as an older ERP or IMS left it: its header and a box row per (box_id,
    transaction_no) of `boxes` (`joined`: each row's created_at; none without)."""
    db.tables["interunit_transfers_header"].append({
        "id": tid, "challan_no": challan, "stock_trf_date": None, "from_site": frm, "to_site": to,
        "vehicle_no": "MH43BP6885", "driver_name": None, "approved_by": None, "remark": "",
        "reason_code": None, "status": status, "request_id": None, "created_by": USER, "created_ts": created})
    for n, (bid, tno) in enumerate(boxes, 1):
        row = {"id": tid * 10 + n, "header_id": tid, "transfer_line_id": None, "box_number": n, "box_id": bid,
               "article": "ALMOND 25KG", "lot_number": "LOT-9", "batch_number": "", "transaction_no": tno,
               "net_weight": Decimal("25"), "gross_weight": Decimal("25.4")}
        if joined is not None:
            row["created_at"] = joined
        db.tables["interunit_transfer_boxes"].append(row)
    return tid


def _box_row(db, tid, key=(BOX, TNO)) -> dict:
    (row,) = [b for b in db.tables["interunit_transfer_boxes"]
              if b["header_id"] == tid and (b["box_id"], b["transaction_no"]) == key]
    return row


def _joined_at(db, tid, box_id, when):
    """When `box_id` joined transfer `tid` (its box row's created_at), as days passing set it."""
    (row,) = [b for b in db.tables["interunit_transfer_boxes"] if b["header_id"] == tid and b["box_id"] == box_id]
    row["created_at"] = when


def _grn(db, tid, *, status="Received", acked=(), rid=None) -> int:
    """An interunit receipt of transfer `tid` in `status`, with one box row per dict of
    `acked` (V2's fields unless it says otherwise)."""
    rid = rid or 81250001 + len(db.tables["interunit_transfer_in_header"])
    h = ro._header(db, tid)
    db.tables["interunit_transfer_in_header"].append({
        "id": rid, "transfer_out_id": tid, "transfer_out_no": h["challan_no"], "grn_number": f"GRN-{rid}",
        "grn_date": ro.RECEIVED_AT, "receiving_warehouse": h["to_site"], "received_by": USER,
        "received_at": ro.RECEIVED_AT, "box_condition": "Good", "condition_remarks": None, "status": status,
        "created_at": ro.RECEIVED_AT, "updated_at": None})
    for n, fields in enumerate(acked):
        db.tables["interunit_transfer_in_boxes"].append({
            "id": rid * 100 + n, "header_id": rid, "box_id": BOX, "transaction_no": TNO, "article": "ALMOND 25KG",
            "batch_number": "", "lot_number": "LOT-9", "net_weight": Decimal("25"), "gross_weight": Decimal("25.4"),
            "scanned_at": ro.RECEIVED_AT, "is_matched": True, "transfer_out_box_id": None, "issue": None,
            "line_index": None, "inward_box_id": None, **fields})
    return rid


def _cold_grn(db, tid, *, status="Received", boxes=((BOX, TNO),)) -> int:
    """A receipt IMS's cold receive wrote for transfer `tid`, having taken in `boxes`."""
    hid = 91250001 + len(db.tables["cold_transfer_in_headers"])
    db.tables["cold_transfer_in_headers"].append({"id": hid, "transfer_out_id": tid, "status": status})
    for n, (bid, tno) in enumerate(boxes):
        db.tables["cold_transfer_inboxes"].append(
            {"id": hid * 100 + n, "header_id": hid, "box_id": bid, "transaction_no": tno})
    return hid


def _where(db, here, key=(BOX, TNO)):
    """R3's verdict on the box `key` sent from `here`: None (it may go) or (reason,
    challan, the refusal)."""
    held = asyncio.run(lookup_service.unavailable_boxes(db, [key], from_warehouse=here))
    if not held:
        return None
    (hit,) = held
    return hit["reason"], hit["challan_no"], lookup_service.unavailable_message(hit)


def _received_in(db, to, *, acked=({},)) -> int:
    """V2 on transfer TRANS-IN to `to`, received there with `acked` acknowledged."""
    tid = _transfer(db, 99000001, "TRANS-IN", to=to, status="Received", created=_oct(1))
    _grn(db, tid, acked=acked)
    return tid


# ── Sites (P1-01, P1-24, D5) ─────────────────────────────────────────────────
@pytest.mark.parametrize("stored, here", [
    ("Warehouse A185", "A185"), ("A-185", "a 185"), ("A185 Cold", "A185"), ("Supreme Cold", "Supreme"),
    ("Supreme", "Supreme Cold Storage"), ("Savla D-39 Cold", "Savla D-39"), ("Savla D-39", "D-39"),
    ("D-39", "Savla D-39"), ("Old Savla", "Savla D39"), ("ESKIMO", "Eskimo"), ("Rishi Cold Storage Pvt Ltd", "Rishi"),
])
def test_a_box_received_at_a_site_goes_on_from_any_spelling_of_it(stored, here):
    db = _db()
    _received_in(db, stored)
    assert _where(db, here) is None
    assert cold._lookup(db, "tr", from_wh=here)["box_id"] == BOX
    t = br._send(db, V2, challan="TRANS-ON", frm=here, to="W808", at=_oct(9))
    assert set(ro._parked(db, t)) == {BOX}


@pytest.mark.parametrize("stored, canonical", [
    ("Warehouse A185", "A185"), ("Supreme Cold", "Supreme"), ("D-39", "Savla D-39"), ("ESKIMO", "Eskimo"),
    ("W909", "W909"),
])
def test_a_box_received_at_a_site_is_refused_elsewhere_naming_that_site_canonically(stored, canonical):
    db = _db()
    _received_in(db, stored)
    refusal = f"Box {BOX} was received at {canonical} on challan TRANS-IN; send it from {canonical}."
    assert _where(db, "W202") == ("elsewhere", "TRANS-IN", refusal)
    assert br._lookup_refusal(db, "tr-boxes_v2", "W202") == refusal
    assert _where(db, None)[0] == "elsewhere"           # no From site: every transfer blocks


@pytest.mark.parametrize("stored, here", [
    ("Rishi", "Cold Storage"), ("Cold Storage", "Rishi"), ("Savla D-39", "Cold Storage"),
    ("Savla Bond", "Savla D-39"), ("Savla D-39", "Savla Bond"), ("Savla D-39", "Savla"),
])
def test_cold_storage_and_savla_bond_are_sites_of_their_own(stored, here):
    db = _db()
    _received_in(db, stored)
    assert _where(db, here)[0] == "elsewhere"


def test_a_request_route_matches_in_any_spelling_and_the_transfer_names_its_sites_canonically():
    db = dl._db(request=dl._request(from_site="Warehouse A185", to_site="Supreme Cold"))
    dl._create(db, dl._payload(from_wh="A-185", to_wh="Supreme"))
    h = dl._header(db)
    assert (h["from_site"], h["to_site"]) == ("A185", "Supreme")
    parked = [p for p in db.tables["pending_transfer_stock"] if not p["box_id"].startswith("LINE-")]
    # The in-transit rows too; Supreme is a cold store of cdpl's.
    assert {(p["from_site"], p["to_site"], p["to_storage_type"], p["destination_table"]) for p in parked} == {
        ("A185", "Supreme", "cold", "cdpl_cold_stocks")}


@pytest.mark.parametrize("frm, to, stored", [
    ("warehouse a185", "Rishi Cold", ("A185", "Rishi")), ("W-202", "ESKIMO", ("W202", "Eskimo")),
    ("W909", "Savla Bond", ("W909", "Savla Bond")),
])
def test_a_new_transfer_names_its_sites_canonically(frm, to, stored):
    db = _db()
    t = br._send(db, V2, challan="TRANS-NEW", frm=frm, to=to, at=_oct(5))
    h = ro._header(db, t)
    assert (h["from_site"], h["to_site"]) == stored
    assert {(p["from_site"], p["to_site"]) for p in ro._parked(db, t).values()} == {stored}


def test_an_edit_keeps_the_stored_spelling_of_a_leg_it_does_not_change():
    db = _db()
    t = _transfer(db, 99000001, "TRANS-OLD", frm="Warehouse A185", to="W-202", status="Dispatch", created=_oct(1))
    br._edit(db, t, V2, frm="A185", to="W202")
    h = ro._header(db, t)
    assert (h["from_site"], h["to_site"]) == ("Warehouse A185", "W-202")
    br._edit(db, t, V2, frm="A185", to="Supreme Cold")
    h = ro._header(db, t)
    assert (h["from_site"], h["to_site"]) == ("Warehouse A185", "Supreme")


# ── When the box joined (P2-01) ──────────────────────────────────────────────
def test_every_box_row_records_when_it_joined_and_an_edit_keeps_a_kept_boxs():
    db = _db()
    before = datetime.now()
    t = ro._create(db, V2, challan="TRANS-A")
    assert _box_row(db, t)["created_at"] >= before
    _joined_at(db, t, BOX, _oct(5, 10))
    ro._update(db, t, V2, BULK)
    assert _box_row(db, t)["created_at"] == _oct(5, 10)
    assert _box_row(db, t, BULK_KEY)["created_at"] >= before


def test_a_box_scanned_onto_an_older_transfer_is_judged_by_that_transfer():
    db = _db()
    # Monday: T1 W202 -> A185 with BULK. Tuesday: T2 A185 -> W202 with V2, received at W202.
    t1 = br._send(db, BULK, challan="TRANS-1", frm="W202", to="A185", at=_oct(5))
    _joined_at(db, t1, BULK.box_id, _oct(5))
    t2 = br._send(db, V2, challan="TRANS-2", frm="A185", to="W202", at=_oct(6))
    _joined_at(db, t2, BOX, _oct(6))
    br._receive(db, t2)
    # Thursday: V2, now at W202, is added to T1, which was created before T2.
    br._edit(db, t1, BULK, V2, frm="W202", to="A185")
    _joined_at(db, t1, BOX, _oct(8))

    # It is in transit on T1, from every site: a third dispatch from W202 cannot take it.
    for here in ("W202", "A185", None):
        assert _where(db, here)[:2] == ("in_transit", "TRANS-1")
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        br._send(db, V2, challan="TRANS-3", frm="W202", to="F53", at=_oct(9))
    assert exc.value.detail == br._in_transit(BOX, "TRANS-1") and db.tables == before
    # Received on T1, it is at A185.
    br._receive(db, t1)
    assert _where(db, "A185") is None
    assert _where(db, "W202")[2] == br._received_at(BOX, "A185", "TRANS-1")


def test_a_box_row_stamped_before_its_transfer_existed_counts_from_the_transfer():
    # A box cannot join a transfer before the transfer exists: a created_at from a clock
    # behind the header's does not make the newer transfer the older one.
    db = _db()
    _received_in(db, "A185")
    _transfer(db, 10000002, "TRANS-NEW", to="W303", status="Dispatch", created=_oct(5), joined=datetime(2026, 9, 1))
    assert _where(db, "A185")[:2] == ("in_transit", "TRANS-NEW")


def test_undoing_a_receipt_puts_back_no_box_that_joined_another_transfer_since():
    db = _db()
    t1 = br._send(db, BULK, challan="TRANS-1", frm="W202", to="A185", at=_oct(5))
    _joined_at(db, t1, BULK.box_id, _oct(5))
    t2 = br._send(db, V2, COLD, challan="TRANS-2", frm="A185", to="W202", at=_oct(6))
    for box_id in (BOX, COLD.box_id):
        _joined_at(db, t2, box_id, _oct(6))
    ti = br._receive(db, t2)
    br._edit(db, t1, BULK, V2, frm="W202", to="A185")          # V2 joins T1 on Thursday
    _joined_at(db, t1, BOX, _oct(8))

    asyncio.run(receive_service.reopen_transfer_in(db, ti, USER))
    # T2 is in transit again with COLD only; V2 stays in transit on T1, its row untouched.
    assert set(ro._parked(db, t2)) == {COLD.box_id}
    assert set(ro._parked(db, t1)) == {BULK.box_id, BOX}


# ── IMS's receipts and statuses (P2-04, P2-10) ──────────────────────────────
def _to_rishi(db, status, boxes=((BOX, TNO), BULK_KEY)) -> int:
    return _transfer(db, 99000001, "TRANS-IMS", frm="W202", to="Rishi", status=status, created=_oct(1), boxes=boxes)


def test_a_box_ims_received_into_cold_storage_goes_on_from_there_and_one_it_missed_is_short():
    db = _db(ims=True)
    t = _to_rishi(db, "Received")
    _cold_grn(db, t, boxes=((BOX, TNO), ("17601234-1", BULK.transaction_no)))    # BULK came in as a new id
    assert _where(db, "Rishi") is None and _where(db, "Rishi Cold") is None
    assert _where(db, "W202")[0] == "elsewhere"
    assert _where(db, "Rishi", BULK_KEY)[:2] == ("short", "TRANS-IMS")


def test_a_box_ims_took_in_is_at_the_to_site_while_its_receipt_is_pending():
    db = _db(ims=True)
    t = _to_rishi(db, "Dispatch")
    _cold_grn(db, t, status="Pending", boxes=((BOX, TNO),))
    assert _where(db, "Rishi") is None
    assert _where(db, "Rishi", BULK_KEY)[:2] == ("in_transit", "TRANS-IMS")


def test_a_box_ims_took_in_counts_with_an_erp_receipt_of_the_rest():
    db = _db(ims=True)
    t = _to_rishi(db, "Received")
    _cold_grn(db, t, status="Pending", boxes=((BOX, TNO),))
    _grn(db, t, acked=[{"box_id": BULK.box_id, "transaction_no": BULK.transaction_no}])
    assert _where(db, "Rishi") is None and _where(db, "Rishi", BULK_KEY) is None


def test_undoing_an_erp_receipt_leaves_a_box_ims_took_in_at_the_to_site():
    db = _db(ims=True)
    t = ro._create(db, V2, BULK, challan="TRANS-A", to_wh="Rishi")
    _cold_grn(db, t, status="Pending", boxes=((BOX, TNO),))
    ti = br._receive(db, t, only={BULK.box_id})
    asyncio.run(receive_service.reopen_transfer_in(db, ti, USER))
    assert set(ro._parked(db, t)) == {BULK.box_id}
    assert _where(db, "Rishi") is None


def test_a_backfill_parks_no_box_ims_received_and_skips_a_transfer_ims_received_in_full():
    db = _db(ims=True)
    t = _transfer(db, 5, "TRANS-OLD", frm="A185", to="Rishi", status="Dispatch", created=datetime(2026, 9, 1),
                  boxes=((BOX, TNO), BULK_KEY))
    _cold_grn(db, t, status="Pending", boxes=((BOX, TNO),))
    s = asyncio.run(reversal_service.backfill_pending_from_existing_transfers(db))
    assert (s["boxes_skipped_received"], s["boxes_parked_from_warehouse"]) == (1, 1)
    assert set(ro._parked(db, t)) == {BULK.box_id}
    # A transfer with a Received cold receipt is no candidate (the fake reads only the
    # status filter of the candidate scan, so the clause is checked as sent).
    (scan,) = db.statements("SELECT h.id, h.challan_no, h.from_site")
    assert "NOT EXISTS (SELECT 1 FROM cold_transfer_in_headers ch WHERE ch.transfer_out_id = h.id" in scan


@pytest.mark.parametrize("status, receipt, acked, here, reason", [
    ("Completed", None, False, "A185", "in_transit"),
    ("In Transit", None, False, "A185", "in_transit"),
    ("COMPLETED", "Received", True, "A185", None),            # received by its receipt
    ("In Transit", "Received", False, "A185", "short"),
    ("Partially Received", "Pending", True, "A185", None),
    ("Partially Received", "Pending", True, "W202", "elsewhere"),
    ("Partially Received", "Pending", False, "A185", "in_transit"),
    ("Dispatch", "Pending", True, "A185", "in_transit"),       # a pending receipt is no receipt yet
], ids=["completed", "in-transit", "completed-received", "in-transit-received-short", "partly-acked",
        "partly-acked-elsewhere", "partly-not-acked", "dispatch-pending-receipt"])
def test_imss_older_statuses(status, receipt, acked, here, reason):
    db = _db()
    t = _transfer(db, 99000001, "TRANS-OLD", to="A185", status=status, created=_oct(1))
    if receipt:
        _grn(db, t, status=receipt, acked=[{}] if acked else [])
    got = _where(db, here)
    assert (got[0] if got else None) == reason


@pytest.mark.parametrize("status", ["In Transit", "Completed", "completed"])
def test_a_transfer_in_transit_by_an_older_status_can_be_edited(status):
    db = _db()
    t = ro._create(db, V2, challan="TRANS-A")
    ro._header(db, t)["status"] = status
    ro._update(db, t, V2, BULK)
    assert set(ro._parked(db, t)) == {BOX, BULK.box_id}
    assert ro._header(db, t)["status"] == "Partial"


# ── Edits of a received transfer (P2-06) ────────────────────────────────────
RECEIVED_409 = "Transfer TRANS-A has already been received at W202; it can't be edited."


@pytest.mark.parametrize("how", ["received", "partly-received", "receipt-begun", "ims-receipt-begun"])
def test_an_edit_of_a_received_transfer_is_409_checked_with_its_header_locked(how):
    db = _db(ims=True)
    t = ro._create(db, V2, challan="TRANS-A")
    if how == "received":
        ro._header(db, t)["status"] = "Received"
    elif how == "partly-received":
        ro._header(db, t)["status"] = "Partially Received"
    elif how == "receipt-begun":
        _grn(db, t, status="Pending")
    else:
        _cold_grn(db, t, status="Pending", boxes=())
    before = copy.deepcopy(db.tables)
    n, k = len(db.calls), len(db.locked)
    with pytest.raises(HTTPException) as exc:
        ro._update(db, t, V2, BULK)
    assert (exc.value.status_code, exc.value.detail) == (409, RECEIVED_409)
    assert db.tables == before
    lock, args = db.locked[k]
    assert lock.startswith("SELECT id, challan_no, status,") and "FROM interunit_transfers_header" in lock
    assert args == (t,)
    sent = [s for _, s, _ in db.calls[n:]]
    assert not [s for s in sent if s.startswith(ro.GUARD) or s == cold._BOX_LOCK]     # nothing judged


def _edit_as(db, t, user, *, frm="A185", to="W202"):
    return asyncio.run(create_service.update_transfer(db, t, br._plain(V2, frm=frm, to=to), user=user))


def test_with_its_user_an_edit_is_locked_to_the_stored_from_site_and_route():
    # The router's caller: the stored From must be the user's, not only the one sent; a
    # route change is an admin's. (Without a user the service checks neither.)
    db = _db()
    t = _transfer(db, 99000001, "TRANS-W202", frm="W202", to="F53", status="Dispatch", created=_oct(1))
    before = copy.deepcopy(db.tables)
    with pytest.raises(AuthError) as exc:
        _edit_as(db, t, RECEIVER, frm="A185", to="F53")               # sends its own From
    assert exc.value.status_code == 403 and db.tables == before
    store = trd._user("store.w202@candorfoods.in", warehouses=["W-202"])
    with pytest.raises(HTTPException) as exc:
        _edit_as(db, t, store, frm="W202", to="A185")
    assert (exc.value.status_code, exc.value.detail) == (
        409, "This transfer was dispatched for W202 -> F53; only an admin can change its route.")
    assert db.tables == before
    _edit_as(db, t, store, frm="Warehouse W202", to="F53")            # the same route, in other spellings
    _edit_as(db, t, trd.ADMIN, frm="W202", to="A185")
    assert (ro._header(db, t)["from_site"], ro._header(db, t)["to_site"]) == ("W202", "A185")


# ── Acknowledgements (P2-13, P2-17) ─────────────────────────────────────────
def test_an_acknowledgement_naming_the_box_row_counts_whatever_box_id_it_carries():
    db = _db()
    t = _received_in(db, "A185", acked=())
    row_id = _box_row(db, t)["id"]
    _grn(db, t, acked=[{"box_id": "SCANNED-1", "transaction_no": "", "transfer_out_box_id": row_id}],
         rid=81250009)
    assert _where(db, "A185") is None
    db.tables["interunit_transfer_in_boxes"][-1]["transfer_out_box_id"] = row_id + 999
    assert _where(db, "A185")[0] == "short"


@pytest.mark.parametrize("fields, reason", [
    ({"box_id": f" {BOX} ", "transaction_no": f" {TNO} "}, None),                 # padded
    ({"box_id": BOX, "transaction_no": ""}, None),                                # no transaction sent
    ({"box_id": "NEW-1", "original_box_id": BOX, "transaction_no": TNO}, None),   # IMS's STBR swap
    ({"box_id": BOX, "transaction_no": OTHER_TNO}, "short"),                      # another inward's box
], ids=["padded", "no-transaction", "original-box-id", "other-transaction"])
def test_an_acknowledgement_by_box_id_and_transaction(fields, reason):
    db = _db()
    _received_in(db, "A185", acked=[fields])
    got = _where(db, "A185")
    assert (got[0] if got else None) == reason


def test_a_box_row_ims_stored_padded_is_in_the_box_history():
    db = _db()
    _transfer(db, 99000001, "TRANS-IMS", to="W202", status="Dispatch", created=_oct(1), boxes=((f" {BOX} ", f"{TNO} "),))
    assert _where(db, "A185")[:2] == ("in_transit", "TRANS-IMS")
    assert br._lookup_refusal(db, "tr-boxes_v2", "A185") == br._in_transit(BOX, "TRANS-IMS")


def _ack(db, rid, box_id, tno, **over):
    data = schemas.PendingBoxAcknowledge(**{"box_id": box_id, "transaction_no": tno, "article": "ALMOND 25KG",
                                            "lot_number": "LOT-9", "net_weight": 25, "gross_weight": 25.4, **over})
    return asyncio.run(receive_service.acknowledge_pending_box(db, rid, data))


def _receipt_rows(db, rid) -> list:
    return [r for r in db.tables["interunit_transfer_in_boxes"] if r["header_id"] == rid]


def test_acknowledging_records_a_box_of_the_transfer_by_its_box_row():
    db = _db()
    t = ro._create(db, V2, BULK, challan="TRANS-A")
    rid = _grn(db, t, status="Pending")
    out = _ack(db, rid, f" {BOX} ", TNO)
    (row,) = _receipt_rows(db, rid)
    assert row["transfer_out_box_id"] == out["transfer_out_box_id"] == _box_row(db, t)["id"]
    assert db.locked[-1][0].endswith("FROM interunit_transfer_in_header WHERE id = $1 FOR UPDATE")
    # Again: its row is rewritten, not doubled.
    _ack(db, rid, BOX, TNO, net_weight=24.5)
    (row,) = _receipt_rows(db, rid)
    assert row["net_weight"] == 24.5
    # By the box row alone (no transaction sent): the same box.
    _ack(db, rid, BOX, "", transfer_out_box_id=_box_row(db, t)["id"], net_weight=24.0)
    (row,) = _receipt_rows(db, rid)
    assert row["net_weight"] == 24.0
    # A box that is not on the transfer: 409, nothing written.
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _ack(db, rid, COLD.box_id, COLD.transaction_no)
    assert (exc.value.status_code, exc.value.detail) == (
        409, f"Box {COLD.box_id} with transaction {COLD.transaction_no} is not on transfer TRANS-A.")
    assert db.tables == before
    # A line entry (no transaction) is recorded as sent.
    _ack(db, rid, "LINE-1-1", "")
    assert {r["box_id"] for r in _receipt_rows(db, rid)} == {BOX, "LINE-1-1"}


def test_re_acknowledging_a_box_recorded_under_another_transaction_rewrites_its_row():
    # IMS recorded the box under its receipt's own new transaction (its Generate QR).
    db = _db(_OneRowPerBoxId)
    t = ro._create(db, V2, BULK, challan="TRANS-A")
    rid = _grn(db, t, status="Pending", acked=[{"transaction_no": "TR-20261016101010"}])
    _ack(db, rid, BOX, TNO)
    (row,) = _receipt_rows(db, rid)
    assert (row["transaction_no"], row["transfer_out_box_id"]) == (TNO, _box_row(db, t)["id"])


def _two_sharing_a_box_id(db) -> int:
    """TRANS-TWO, to A185, with two boxes of one box id under two transactions, in transit."""
    t = _transfer(db, 99000001, "TRANS-TWO", frm="W202", to="A185", status="Dispatch", created=_oct(1),
                  boxes=((BOX, TNO), (BOX, OTHER_TNO)))
    for n, tno in enumerate((TNO, OTHER_TNO)):
        db.tables["pending_transfer_stock"].append(
            {"id": 501 + n, "transfer_out_id": t, "box_id": BOX, "transaction_no": tno, "status": "In Transit",
             "cold_storage_data": None})
    return t


def test_two_boxes_sharing_a_box_id_never_overwrite_each_other():
    db = _db()
    t = _two_sharing_a_box_id(db)
    rid = _grn(db, t, status="Pending")
    _ack(db, rid, BOX, TNO)
    _ack(db, rid, BOX, OTHER_TNO)
    assert sorted((r["transaction_no"], r["transfer_out_box_id"]) for r in _receipt_rows(db, rid)) == sorted(
        [(TNO, _box_row(db, t)["id"]), (OTHER_TNO, _box_row(db, t, (BOX, OTHER_TNO))["id"])])
    asyncio.run(receive_service.finalize_transfer_in(db, rid, schemas.FinalizeTransferIn(box_condition="Good")))
    assert _where(db, "A185") is None and _where(db, "A185", (BOX, OTHER_TNO)) is None


def test_where_a_receipt_holds_one_row_per_box_id_the_second_such_box_is_refused():
    db = _db(_OneRowPerBoxId)
    t = _two_sharing_a_box_id(db)
    rid = _grn(db, t, status="Pending")
    _ack(db, rid, BOX, TNO)
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _ack(db, rid, BOX, OTHER_TNO)
    assert (exc.value.status_code, exc.value.detail) == (
        409, f"This receipt already records box {BOX} (transaction {TNO}); a second box with the same box id "
             "can't be recorded on it.")
    assert db.tables == before


def test_close_with_shortage_and_unacknowledge_go_box_by_box():
    db = _db()
    t = _two_sharing_a_box_id(db)
    rid = _grn(db, t, status="Pending")
    _ack(db, rid, BOX, TNO)
    _ack(db, rid, BOX, OTHER_TNO)
    out = asyncio.run(receive_service.unacknowledge_pending_box(db, rid, BOX, transaction_no=f" {OTHER_TNO}"))
    assert out == {"success": True, "deleted_box_id": BOX}
    assert [r["transaction_no"] for r in _receipt_rows(db, rid)] == [TNO]
    out = asyncio.run(receive_service.close_with_shortage(db, rid, "one short", USER))
    assert out["shortage_written_off"] == 1               # the box under OTHER_TNO, not both
    assert _where(db, "A185") is None and _where(db, "A185", (BOX, OTHER_TNO))[0] == "short"


# ── In-transit rows (P2-05, P2-11, P2-12) ───────────────────────────────────
def test_a_live_dispatch_takes_over_a_stale_in_transit_row_of_another_transfer():
    db = _db()
    t1 = br._send(db, V2, challan="TRANS-1", frm="W202", to="A185", at=_oct(1))
    (stale,) = [dict(p) for p in db.tables["pending_transfer_stock"] if p["box_id"] == BOX]
    br._receive(db, t1)
    db.tables["pending_transfer_stock"].append({**stale, "id": 999})     # left behind, as an old backfill did
    t2 = br._send(db, V2, challan="TRANS-2", frm="A185", to="W303", at=_oct(5))
    assert [p["transfer_out_id"] for p in db.tables["pending_transfer_stock"] if p["box_id"] == BOX] == [t2]
    assert ro._header(db, t2)["unallocated_boxes"] == 0


@pytest.mark.parametrize("take_over, owner, stats", [
    (False, 1, {"skipped_already_parked": 1}), (True, cold.TOUT, {"parked_from_cold": 1}),
])
def test_a_row_of_another_transfer_is_taken_over_only_by_a_live_save(take_over, owner, stats):
    db = cold._db(*cold.WAREHOUSE_TABLES, cdpl_cold_stocks=[cold._cold()],
                  pending_transfer_stock=[{"id": 1, "box_id": cold.BOX_ID, "transaction_no": cold.TNO,
                                           "transfer_out_id": 1}])

    async def go():
        async with db.transaction():
            return await reversal_service.park_boxes(
                db, transfer_out_id=cold.TOUT, challan_no=cold.CHALLAN, from_site="A185", to_site="W202",
                boxes=[cold._box()], dispatched_at=cold.DISPATCHED, dispatched_by=USER, cold_fallback=True,
                take_over=take_over)

    assert asyncio.run(go()) == {**cold.NO_STATS, **stats}
    assert [p["transfer_out_id"] for p in db.tables["pending_transfer_stock"]] == [owner]
    assert cold.stock_writes(db) == []


def test_a_backfill_parks_no_box_that_went_on_since():
    db = _db()
    t0 = _transfer(db, 5, "TRANS-OLD", frm="W909", to="A185", status="Dispatch", created=datetime(2026, 9, 1),
                   boxes=((BOX, TNO), BULK_KEY))
    t1 = _transfer(db, 6, "TRANS-LATER", frm="A185", to="W202", status="Received", created=datetime(2026, 9, 5))
    _grn(db, t1, acked=[{}])
    s = asyncio.run(reversal_service.backfill_pending_from_existing_transfers(db))
    assert (s["boxes_skipped_sent_on"], s["boxes_parked_from_warehouse"]) == (1, 1)
    assert set(ro._parked(db, t0)) == {BULK.box_id}


def test_reopening_a_receipt_closed_with_shortage_puts_every_box_back():
    db = _db()
    t = ro._create(db, V2, BULK, COLD, challan="TRANS-A")
    parked = ro._parked(db, t)
    ti = br._receive(db, t, only={BULK.box_id})                  # V2 and COLD written off
    asyncio.run(receive_service.reopen_transfer_in(db, ti, USER))
    again = ro._parked(db, t)
    assert set(again) == {BOX, BULK.box_id, COLD.box_id}
    assert {k: (p["source_table"], p["source_row_id"]) for k, p in again.items()} == {
        k: (p["source_table"], p["source_row_id"]) for k, p in parked.items()}


def test_undoing_a_receipt_parks_no_box_that_is_not_on_the_transfer_and_keeps_the_received_values():
    db = _db()
    t = ro._create(db, V2, BULK, challan="TRANS-A")
    rid = _grn(db, t, status="Pending",
               acked=[{"lot_number": "LOT-FIX"}, {"box_id": COLD.box_id, "transaction_no": COLD.transaction_no}])
    asyncio.run(receive_service.finalize_transfer_in(db, rid, schemas.FinalizeTransferIn(box_condition="Good")))
    asyncio.run(receive_service.reopen_transfer_in(db, rid, USER))
    parked = ro._parked(db, t)
    assert set(parked) == {BOX, BULK.box_id}                     # COLD was acknowledged here, not sent here
    assert parked[BOX]["lot_no"] == "LOT-FIX"


# ── Site kind and companies (P1-10, P1-22) ──────────────────────────────────
@pytest.mark.parametrize("site, kind", [
    ("Eskimo", "cold"), ("ESKIMO", "cold"), ("Savla Bond", "cold"), ("D-39", "cold"), ("Old Savla", "cold"),
    ("Supreme Cold Storage", "cold"), ("Cold Storage", "cold"), ("A185 Cold", "warehouse"), ("W202", "warehouse"),
    ("Savla", "warehouse"),
])
def test_a_site_is_cold_by_its_kind(site, kind):
    # Only a box_id match in cold stock: a cold From reads it, a warehouse From does not.
    db = cold._db(*cold.WAREHOUSE_TABLES, cfpl_cold_stocks=[cold._cold(transaction_no="TR-OTHER")])
    stats = cold._park(db, [cold._box()], from_site=site, to_site=site)
    assert stats == {**cold.NO_STATS, ("parked_from_cold" if kind == "cold" else "parked_without_source"): 1}
    p = cold._pending(db)
    assert (p["from_storage_type"], p["to_storage_type"]) == (kind, kind)


def test_from_cold_unit_names_each_cold_store_by_its_canonical_name():
    db = _db()
    units = ("Supreme Cold", "D-39", "Eskimo Cold", "Savla Bond", "A185 Cold", "Savla")
    db.tables["cdpl_cold_stocks"] = [cold._cold(id=7400 + n, box_id=f"CU-{n}", unit=u) for n, u in enumerate(units)]
    boxes = [ro._bx(n + 1, f"CU-{n}", TNO) for n in range(len(units))]
    t = asyncio.run(create_service.create_transfer(db, br._plain(*boxes, frm="Cold Storage", to="W202"), USER))["id"]
    assert ro._header(db, t)["from_cold_unit"] == "Eskimo, Savla Bond, Savla D-39, Supreme"


@pytest.mark.parametrize("frm, to, source, destination", [
    ("Supreme", "W202", "cdpl_cold_stocks", "cdpl_boxes_v2"),
    ("Eskimo Cold", "W202", "cdpl_cold_stocks", "cdpl_boxes_v2"),
    ("Savla D-39", "W202", "cfpl_cold_stocks", "cfpl_boxes_v2"),
    ("Cold Storage", "W202", "cfpl_cold_stocks", "cfpl_boxes_v2"),
    ("W202", "A185", "cfpl_bulk_entry_boxes", "cfpl_boxes_v2"),
    ("W202", "Rishi", "cfpl_bulk_entry_boxes", "cdpl_cold_stocks"),
    ("W202", "Savla Bond", "cfpl_bulk_entry_boxes", "cfpl_cold_stocks"),
])
def test_the_companies_of_a_box_with_no_stock_row_come_from_the_site_table(frm, to, source, destination):
    db = cold._db(*cold.WAREHOUSE_TABLES)
    cold._park(db, [cold._box()], from_site=frm, to_site=to)
    p = cold._pending(db)
    assert (p["source_table"], p["destination_table"]) == (source, destination)
    assert (p["from_company"], p["to_company"]) == (source[:4], destination[:4])


# ── Receipts on the transfer's own To site (P1-13) ──────────────────────────
def _begin(db, tid, user, receiving_warehouse="A185"):
    data = schemas.PendingTransferInCreate(transfer_out_id=tid, grn_number="GRN-NEW",
                                           receiving_warehouse=receiving_warehouse, received_by=USER)
    return asyncio.run(receive_service.create_pending_transfer_in(db, data, user))


def test_a_receipt_is_begun_only_by_a_user_of_the_transfers_own_to_site():
    db = _db()
    t = _transfer(db, 99000001, "TRANS-D39", frm="W202", to="Savla D-39", status="Dispatch", created=_oct(1))
    before = copy.deepcopy(db.tables)
    with pytest.raises(AuthError) as exc:
        _begin(db, t, RECEIVER, receiving_warehouse="A185")          # the client's value is not trusted
    assert exc.value.status_code == 403 and db.tables == before
    assert db.locked[-1] == ("SELECT id, challan_no, to_site FROM interunit_transfers_header WHERE id = $1 "
                             "FOR UPDATE", (t,))


@pytest.mark.parametrize("to", ["Warehouse A185", "A-185", "A185"])
def test_a_receipt_stores_the_to_sites_canonical_name_whatever_the_client_sends(to):
    db = _db()
    t = _transfer(db, 99000001, "TRANS-IN", frm="W202", to=to, status="Dispatch", created=_oct(1))
    out = _begin(db, t, RECEIVER, receiving_warehouse="W202")
    (grn,) = db.tables["interunit_transfer_in_header"]
    assert grn["receiving_warehouse"] == out["receiving_warehouse"] == "A185"


def test_the_receipt_routes_lock_on_the_transfers_to_site_not_the_body(client):  # noqa: F811
    db = _db()
    _transfer(db, 99000001, "TRANS-D39", frm="W202", to="Savla D-39", status="Dispatch", created=_oct(1))
    _transfer(db, 99000002, "TRANS-A185", frm="W202", to="Warehouse A185", status="Dispatch", created=_oct(1),
              boxes=(BULK_KEY,))
    c = client(db, RECEIVER)
    body = {"grn_number": "GRN-1", "receiving_warehouse": "A185", "received_by": USER}
    assert c.post(f"{URL}/transfer-in/pending", json={**body, "transfer_out_id": 99000001}).status_code == 403
    assert db.tables["interunit_transfer_in_header"] == []
    res = c.post(f"{URL}/transfer-in/pending", json={**body, "transfer_out_id": 99000002,
                                                    "receiving_warehouse": "W202"})
    assert res.status_code == 201 and res.json()["receiving_warehouse"] == "A185"
    # The bulk route is not wired: 501 whatever receiving_warehouse it is sent.
    assert c.post(f"{URL}/transfer-in", json={"receiving_warehouse": "W202"}).status_code == 501


def test_an_edit_keeps_a_receipt_at_its_transfers_to_site():
    db = _db()
    t = _received_in(db, "Warehouse A185")
    (grn,) = db.tables["interunit_transfer_in_header"]
    grn["receiving_warehouse"] = ""                                  # an older receipt left it blank

    def edit(rw, user=None):
        data = schemas.EditTransferIn(receiving_warehouse=rw, condition_remarks="checked")
        return asyncio.run(receive_service.edit_transfer_in(db, grn["id"], data, USER, user=user))

    with pytest.raises(HTTPException) as exc:
        edit("W202")
    assert (exc.value.status_code, exc.value.detail) == (
        409, "This receipt is at A185, its transfer's To site; it can't be moved to W202.")
    assert edit("a-185")["receiving_warehouse"] == "A185"
    assert edit(None)["receiving_warehouse"] == "A185"
    assert edit("Warehouse A185")["receiving_warehouse"] == "A185"   # names the stored site: kept
    with pytest.raises(AuthError):
        edit(None, user=trd._user("store.w202@candorfoods.in", warehouses=["W202"]))
    assert ro._header(db, t)["status"] == "Received"
