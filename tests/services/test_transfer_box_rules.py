"""The three box rules of a dispatch (Scan & send: POST /transfers; the edit form: PUT
/transfers/{id}; the box lookups), against the SQL-evaluating fake database of
test_transfer_box_lookup_cold, with the approved request of test_transfer_dispatch_lock
and the stock and receipt helpers of test_transfer_record_only.

What is pinned:
  R1 — a dispatch from a request, and an edit of one, take only the boxes the request
    asks for. The boxes are matched to its lines as the screens' tracker matches them
    (article names with whitespace collapsed, in upper case; the lot trimmed; lines with a
    lot take boxes of their article and lot first, then lines without one take boxes of
    their article with any lot; a box counts once, a line takes up to its qty, earlier
    boxes first; a line without a quantity takes none, as the screens read it). Every
    box left over is refused — not on the request, another lot, or over the quantity —
    in one 409 (ten named, then a count), before any box is locked or anything is
    written. Each real box is judged, and stored, by its own article and lot, not the
    client's: an edit's own boxes as saved, any other as the lookups find it in stock; a
    box found in neither is 409. A transfer without a request takes any box; an edit
    after its request was deleted is matched to its own stored lines; the lines a box
    covers (and the line it is stored against) compare articles the same way.
  R2 — a dispatch from a request, and an edit of one (its request deleted or not), carry
    at least one real box (box_id + transaction_no, not 'DIRECT'): else 409 "Scan at
    least one box before sending.", before anything is locked or written.
  R3 — where a box is: its LATEST transfer that exists, by when the box joined it (here,
    box rows without a created_at: the header's created_ts; not by id; no created_ts =
    oldest; an edit leaves itself out), decides. In transit (Dispatch / Partial, a
    Received one with no receipt, a re-opened receipt): refused from every site. Received
    and acknowledged on its receipt: free from that transfer's To site (sites compared as
    sites), refused from any other and from no site at all. Received without it (written
    off as short): refused from every site. A box sent on is in transit again; received
    again, it is at its new site. POST / PUT apply it with the dispatch's own From site,
    every refused box listed; the three lookups with `from_warehouse`, which the router
    passes through. The receipt read asks only for the receipts of the boxes' latest
    transfers (a padded receipt box_id still counts). Undoing an earlier receipt (re-open,
    delete) does not put back in transit a box that went on since. (test_transfer_r3_sites
    pins the rest: box created_at, sites, IMS receipts and statuses.)

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_box_rules.py
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.modules.transfer import schemas
from app.modules.transfer.services import (
    create_service,
    delete_service,
    lookup_service,
    query_service,
    receive_service,
)
from tests.services import test_transfer_box_lookup_cold as cold
from tests.services import test_transfer_dispatch_lock as dl
from tests.services import test_transfer_record_only as ro
from tests.services.test_transfer_request_decisions import client  # noqa: F401  (the HTTP fixture)

USER, BOX, TNO = ro.USER, ro.BOX, ro.TNO
V2, BULK, COLD, FREE = ro.V2, ro.BULK, ro.COLD, ro.FREE
REQ = dl._request()["request_no"]
SCAN_ONE = "Scan at least one box before sending."


@pytest.fixture(autouse=True)
def _fresh_existence_cache(monkeypatch):
    # lookup_service caches to_regclass answers for the process; each test starts clean.
    monkeypatch.setattr(lookup_service, "_EXISTS", {})


@pytest.fixture(autouse=True)
def no_readback(monkeypatch):
    async def get_transfer(conn, transfer_id):
        return {"id": transfer_id}
    monkeypatch.setattr(query_service, "get_transfer", get_transfer)


def _box(box_id, article="almond 25kg", lot="LOT-9", tno=TNO, n=1) -> schemas.BoxCreate:
    return schemas.BoxCreate(box_number=n, box_id=box_id, article=article, lot_number=lot, transaction_no=tno,
                             net_weight="24.75", gross_weight="25")


A1, A2 = (_box(b, n=n) for n, b in enumerate(dl.ALMOND_BOXES, 1))
CASHEW = _box(dl.CASHEW_BOX, "CASHEW W320", "L-3", n=3)
PISTA = _box("P-1", "PISTA", "LOT-1", n=9)
ALMOND_3 = _box("34732254-3")                                   # a third almond box
CASHEW_L9 = _box("88120417-9", "CASHEW W320", "L-9", n=4)        # cashew of another lot
CASHEW_NO_LOT = _box("88120417-0", "CASHEW W320", "", n=5)       # cashew without a lot


def _stocked(db):
    """`db` (dl's) with the other boxes these tests scan in its cold stock, as the
    boxes are: twelve pista boxes, a third almond box, and cashew boxes of lot L-9 and of
    no lot (dl's own: the two almond boxes and the L-3 cashew box)."""
    db.tables["cfpl_cold_stocks"] += [
        *(cold._cold(id=7200 + n, box_id=f"P-{n}", item_description="PISTA", lot_no="LOT-1") for n in range(1, 13)),
        cold._cold(id=7213, box_id=ALMOND_3.box_id),
        cold._cold(id=7214, box_id=CASHEW_L9.box_id, item_description="CASHEW W320", lot_no="L-9"),
        cold._cold(id=7215, box_id=CASHEW_NO_LOT.box_id, item_description="CASHEW W320", lot_no=None),
    ]
    return db


def _over(qty, article, request_no=REQ) -> str:
    return f"Request {request_no} asks for {qty} box(es) of {article}; all are already on this dispatch."


def _refused(db, payload, *, update=False, locks=True) -> str:
    """The 409 detail of a dispatch (or, `update`, an edit of the one transfer) that is
    refused; asserts nothing was written and, unless `locks`, nothing locked or read
    about the boxes' transfers either."""
    before, n = copy.deepcopy(db.tables), len(db.calls)
    with pytest.raises(HTTPException) as exc:
        (dl._update if update else dl._create)(db, payload)
    assert exc.value.status_code == 409
    assert db.tables == before
    if not locks:
        assert not [s for _, s, _ in db.calls[n:] if s == cold._BOX_LOCK or s.startswith(ro.GUARD)]
    return exc.value.detail


# ── R1: only the boxes the request asks for ──────────────────────────────────
@pytest.mark.parametrize("boxes, detail", [
    ([A1, A2, CASHEW, PISTA], f"Box P-1 is PISTA, which is not on request {REQ}."),
    ([A1, A2, CASHEW_L9], f"Box {CASHEW_L9.box_id} is lot L-9; request {REQ} asks for lot L-3 of CASHEW W320."),
    ([A1, A2, CASHEW_NO_LOT],
     f"Box {CASHEW_NO_LOT.box_id} is lot (none); request {REQ} asks for lot L-3 of CASHEW W320."),
    ([A1, A2, CASHEW, ALMOND_3], _over(2, "ALMOND 25KG")),
], ids=["not-on-the-request", "other-lot", "no-lot-for-a-lot-line", "over-the-quantity"])
def test_a_dispatch_from_a_request_with_a_box_it_does_not_ask_for_is_409_before_any_lock(boxes, detail):
    db = _stocked(dl._db())
    assert _refused(db, dl._payload(boxes=boxes), locks=False) == detail
    assert db.tables["interunit_transfer_requests"][0]["status"] == "Approved"


def test_every_refused_box_is_listed_in_scan_order_ten_then_a_count():
    db = _stocked(dl._db())
    mixed = [PISTA, A1, A2, ALMOND_3, CASHEW_L9]
    assert _refused(db, dl._payload(boxes=mixed)) == (
        f"Box P-1 is PISTA, which is not on request {REQ}. {_over(2, 'ALMOND 25KG')} "
        f"Box {CASHEW_L9.box_id} is lot L-9; request {REQ} asks for lot L-3 of CASHEW W320.")

    many = [_box(f"P-{n}", "PISTA", n=n) for n in range(1, 13)]
    assert _refused(db, dl._payload(boxes=[A1, *many])) == " ".join(
        [f"Box P-{n} is PISTA, which is not on request {REQ}." for n in range(1, 11)] + ["And 2 more."])


def test_the_request_boxes_are_matched_whatever_their_article_spacing_or_case():
    # The boxes' own records (cold stock here) spell their article and lot their own way.
    db = dl._db()
    almond, _, cashew = db.tables["cfpl_cold_stocks"]
    almond["item_description"] = " Almond \t 25kg "
    cashew["item_description"], cashew["lot_no"] = "cashew  w320", " L-3 "
    dl._create(db, dl._payload())
    assert dl._parked_boxes(db) == {*dl.ALMOND_BOXES, dl.CASHEW_BOX} and dl._phantoms(db) == []
    assert dl._header(db)["status"] == "Dispatch"


def test_each_box_is_judged_and_stored_by_its_own_record_not_by_what_the_client_says():
    db = _stocked(dl._db())
    # A pista box sent as the request's almond is refused as what it is.
    assert _refused(db, dl._payload(boxes=[A1, _box("P-1", "almond 25kg", "LOT-9")]), locks=False) == (
        f"Box P-1 is PISTA, which is not on request {REQ}.")
    assert _refused(db, dl._payload(boxes=[A1, A2, _box(CASHEW_L9.box_id, "CASHEW W320", "L-3")]), locks=False) == (
        f"Box {CASHEW_L9.box_id} is lot L-9; request {REQ} asks for lot L-3 of CASHEW W320.")
    # The request's own boxes sent as something else: taken, and stored, as what they are.
    dl._create(db, dl._payload(boxes=[_box(b, "PISTA", "LOT-1") for b in (*dl.ALMOND_BOXES, dl.CASHEW_BOX)]))
    assert {(b["box_id"], b["article"], b["lot_number"]) for b in db.tables["interunit_transfer_boxes"]} == {
        (dl.ALMOND_BOXES[0], "ALMOND 25KG", "LOT-9"), (dl.ALMOND_BOXES[1], "ALMOND 25KG", "LOT-9"),
        (dl.CASHEW_BOX, "CASHEW W320", "L-3")}
    assert dl._phantoms(db) == [] and dl._header(db)["status"] == "Dispatch"


def test_a_request_dispatch_with_a_box_found_nowhere_is_409_before_any_lock():
    db = _stocked(dl._db())
    ghosts = [_box("G-1"), _box("G-2", tno="TR-2026-9999")]
    assert _refused(db, dl._payload(boxes=[A1, *ghosts]), locks=False) == (
        f"Box G-1 with transaction {TNO} was not found. Box G-2 with transaction TR-2026-9999 was not found.")
    # Its real box found nowhere, a dispatch has none either way.
    assert _refused(db, dl._payload(boxes=ghosts[:1]), locks=False) == f"Box G-1 with transaction {TNO} was not found."


def test_an_edit_judges_the_boxes_it_keeps_by_their_saved_record_and_added_ones_by_stock():
    db = _stocked(dl._db())
    dl._create(db, dl._payload(boxes=[A1, CASHEW]))
    # The first almond box has left the stock tables (IMS) since its dispatch.
    db.tables["cfpl_cold_stocks"] = [r for r in db.tables["cfpl_cold_stocks"] if r["box_id"] != A1.box_id]

    # Kept (sent relabelled): its saved article and lot; added: its stock record.
    dl._update(db, dl._payload(boxes=[_box(A1.box_id, "PISTA", "LOT-1"), _box(A2.box_id, "PISTA", "LOT-1"), CASHEW]))
    assert {b["box_id"]: (b["article"], b["lot_number"]) for b in db.tables["interunit_transfer_boxes"]} == {
        A1.box_id: ("ALMOND 25KG", "LOT-9"), A2.box_id: ("ALMOND 25KG", "LOT-9"), CASHEW.box_id: ("CASHEW W320", "L-3")}
    assert dl._parked_boxes(db) == {A1.box_id, A2.box_id, CASHEW.box_id} and dl._header(db)["status"] == "Dispatch"
    # A box neither on the transfer nor in stock is refused.
    assert _refused(db, dl._payload(boxes=[A1, _box("G-1")]), update=True, locks=False) == (
        f"Box G-1 with transaction {TNO} was not found.")


def _l(article, lot=None, qty="1"):
    return dl._line(article, lot=lot, qty=qty)


def _b(box_id, article="A", lot="L-1", tno=TNO):
    return _box(box_id, article, lot, tno)


@pytest.mark.parametrize("lines, boxes, refused", [
    ([_l("ALMOND 25KG")], [_b("B-1", " almond \t25kg\n")], []),
    ([_l("ALMOND  25KG")], [_b("B-1", "Almond 25kg")], []),
    ([_l("A", lot="X")], [_b("B-1", lot=" X ")], []),
    ([_l("A", lot="X")], [_b("B-1", lot="x")], ["Box B-1 is lot x; request R asks for lot X of A."]),
    # The lot line takes its box first, whatever the order of the lines and of the scans.
    ([_l("A"), _l("A", lot="X")], [_b("B-1", lot="Y"), _b("B-2", lot="X")], []),
    ([_l("A"), _l("A", lot="X")], [_b("B-1", lot="Y"), _b("B-2", lot="Y")], [_over(1, "A", "R")]),
    # Over: the total of the lines that could take the box (its lot's and the any-lot ones).
    ([_l("A", lot="X"), _l("A")], [_b(f"B-{n}", lot="X") for n in (1, 2, 3)], [_over(2, "A", "R")]),
    ([_l("A", lot="X"), _l("A", lot="Z")], [_b("B-1", lot="X"), _b("B-2", lot="X")], [_over(1, "A", "R")]),
    ([_l("A", lot="X", qty="2"), _l("A", lot="Z"), _l("A", lot="X")], [_b("B-1", lot="Y")],
     ["Box B-1 is lot Y; request R asks for lot X, Z of A."]),
    # Earlier boxes are taken first; the refusals come in scan order.
    ([_l("A", qty="2")], [_b("B-1"), _b("B-2"), _b("P-1", "PISTA"), _b("B-3")],
     ["Box P-1 is PISTA, which is not on request R.", _over(2, "A", "R")]),
    # Every entry counts, a quantity-only one too; one without a box_id is named by number.
    ([_l("A")], [_b("", tno="DIRECT"), _b("B-1")], [_over(1, "A", "R")]),
    ([_l("A")], [_b(None, "PISTA", tno=None)], ["Box #1 is PISTA, which is not on request R."]),
    ([_l("A")], [_b("B-1", "  ")], ["Box B-1 is (no article), which is not on request R."]),
    # A line without a quantity (or below 1) takes no box, as the screens read it.
    ([_l("A", qty=None), _l("A", lot="L-1", qty="")], [_b("B-1")], [_over(0, "A", "R")]),
    ([_l("A", qty="0"), _l("A", qty="-2")], [_b("B-1")], [_over(0, "A", "R")]),
], ids=["box-article-spacing-case", "line-article-spacing", "lot-trimmed", "lot-case-matters", "lot-line-first",
        "other-lot-over-the-any-lot-line", "over-counts-every-line-that-could-take-it", "over-counts-only-its-lot",
        "several-lots-asked", "earlier-boxes-first", "quantity-only-entry-counts", "entry-without-a-box-id",
        "no-article", "no-quantity", "quantity-below-one"])
def test_how_boxes_are_matched_to_the_request_lines(lines, boxes, refused):
    assert create_service._off_request_boxes(lines, boxes, "R") == refused


@pytest.mark.parametrize("raw, key", [("ALMOND 25KG", "ALMOND 25KG"), ("  almond \t 25kg\n", "ALMOND 25KG"),
                                      ("Almond  25 Kg", "ALMOND 25 KG"), ("", ""), (None, "")])
def test_an_article_is_compared_with_whitespace_collapsed_in_upper_case(raw, key):
    assert create_service._article_key(raw) == key


def test_the_lines_a_box_covers_and_is_stored_against_compare_articles_the_same_way():
    db = dl._db(lines=[dl._rline(7301, item_desc_raw="ALMOND  25KG"), dl.REQUEST_LINES[0]])
    dl._create(db, dl._payload(boxes=[_box(dl.ALMOND_BOXES[0], "almond 25kg "), _box(dl.ALMOND_BOXES[1], "ALMOND\t25KG"),
                                      CASHEW]))
    assert dl._phantoms(db) == [] and dl._header(db)["status"] == "Dispatch"
    line_of = {l["item_desc_raw"]: l["id"] for l in db.tables["interunit_transfers_lines"]}
    assert {b["box_id"]: b["transfer_line_id"] for b in db.tables["interunit_transfer_boxes"]} == {
        dl.ALMOND_BOXES[0]: line_of["ALMOND  25KG"], dl.ALMOND_BOXES[1]: line_of["ALMOND  25KG"],
        dl.CASHEW_BOX: line_of["CASHEW W320"]}


def test_an_edit_of_a_request_transfer_takes_only_the_boxes_the_request_asks_for():
    db = _stocked(dl._db())
    dl._create(db, dl._payload())
    assert _refused(db, dl._payload(boxes=[A1, PISTA]), update=True, locks=False) == (
        f"Box P-1 is PISTA, which is not on request {REQ}.")
    assert _refused(db, dl._payload(boxes=[A1, A2, ALMOND_3]), update=True) == _over(2, "ALMOND 25KG")
    # Its own boxes, fewer of them, are fine.
    dl._update(db, dl._payload(boxes=[A2, CASHEW]))
    assert dl._parked_boxes(db) == {dl.ALMOND_BOXES[1], dl.CASHEW_BOX}


def test_after_its_request_was_deleted_an_edit_is_matched_to_the_transfers_own_lines():
    db = _stocked(dl._dispatch_then_delete_the_request())
    assert _refused(db, dl._payload(boxes=[A1, PISTA]), update=True) == (
        f"Box P-1 is PISTA, which is not on request {dl.RID}.")
    assert _refused(db, dl._payload(boxes=[A1, A2, ALMOND_3]), update=True) == (
        _over(2, "ALMOND 25KG", dl.RID))
    dl._update(db, dl._payload(boxes=[_box(dl.ALMOND_BOXES[0], "Almond  25KG")]))
    assert dl._parked_boxes(db) == {dl.ALMOND_BOXES[0]}


def test_a_request_line_without_a_quantity_takes_no_box_and_is_still_dispatched_as_a_line():
    # An older request line with no qty: the screens show it as 0 boxes and refuse its
    # boxes; so does the server. The line is still dispatched (stored as qty 1), with no
    # in-transit row: only a request transfer's boxes are in transit.
    db = _stocked(dl._db(lines=[dl._rline(7301, qty=None), dl.REQUEST_LINES[0]]))
    assert _refused(db, dl._payload(boxes=[A1, CASHEW]), locks=False) == _over(0, "ALMOND 25KG")
    dl._create(db, dl._payload(boxes=[CASHEW]))
    assert dl._phantoms(db) == []
    assert [(l["item_desc_raw"], l["qty"]) for l in db.tables["interunit_transfers_lines"]] == [
        ("ALMOND 25KG", 1), ("CASHEW W320", 1)]


def test_a_transfer_without_a_request_takes_any_box_and_may_have_none():
    db = dl._db()
    boxes = [A1, A2, CASHEW, PISTA, _box("34732254-3")]
    dl._create(db, dl._payload(request_id=None, from_wh="A185", to_wh="W202", boxes=boxes))
    assert len(db.tables["interunit_transfer_boxes"]) == 5
    dl._update(db, dl._payload(request_id=None, from_wh="A185", to_wh="W202", boxes=None))
    assert db.tables["interunit_transfer_boxes"] == []


# ── R2: at least one box ───────────────────────────────────────────────────
NO_REAL_BOX = [
    None, [],
    [_box("", tno="DIRECT"), _box("X-1", tno="DIRECT"), _box(None, tno=None), _box("B-1", tno="  "), _box(" ", tno=TNO)],
]


@pytest.mark.parametrize("boxes", NO_REAL_BOX, ids=["none", "empty", "quantity-only"])
def test_a_dispatch_from_a_request_without_a_real_box_is_409_before_any_lock(boxes):
    db = dl._db()
    assert _refused(db, dl._payload(boxes=boxes), locks=False) == SCAN_ONE
    assert db.tables["interunit_transfer_requests"][0]["status"] == "Approved"


@pytest.mark.parametrize("deleted", [False, True], ids=["request", "request-deleted"])
@pytest.mark.parametrize("boxes", NO_REAL_BOX, ids=["none", "empty", "quantity-only"])
def test_an_edit_of_a_request_transfer_keeps_at_least_one_real_box(boxes, deleted):
    if deleted:
        db = dl._dispatch_then_delete_the_request()
    else:
        db = dl._db()
        dl._create(db, dl._payload())
    assert _refused(db, dl._payload(boxes=boxes), update=True, locks=False) == SCAN_ONE


def test_one_real_box_is_enough_and_only_it_is_in_transit():
    db = dl._db()
    dl._create(db, dl._payload(boxes=[A1]))
    assert dl._parked_boxes(db) == {dl.ALMOND_BOXES[0]}
    assert dl._phantoms(db) == [] and dl._header(db)["status"] == "Partial"     # 1 box < 3 ordered


# ── R3: where the box is ─────────────────────────────────────────────────────
def _oct(day, hour=9) -> datetime:
    return datetime(2026, 10, day, hour)


def _plain(*boxes, frm, to) -> schemas.TransferCreate:
    """A transfer without a request (ro's lines) for `boxes`, `frm` -> `to`. Built on ro's
    route (A185 -> W202) and then re-routed: ro's From with a `to` of 'A-185' would be one
    site twice, which the header refuses."""
    payload = ro._transfer(*boxes, challan="TRANS-X")
    payload.header.from_warehouse, payload.header.to_warehouse = frm, to
    return payload


def _send(db, *boxes, challan, at, frm="W202", to="A-185") -> int:
    """Dispatch `boxes` on `challan`, created at `at` (the history is ordered by it)."""
    payload = _plain(*boxes, frm=frm, to=to)
    payload.header.challan_no = challan
    tid = asyncio.run(create_service.create_transfer(db, payload, USER))["id"]
    ro._header(db, tid)["created_ts"] = at
    return tid


def _edit(db, transfer_id, *boxes, frm, to):
    return asyncio.run(create_service.update_transfer(db, transfer_id, _plain(*boxes, frm=frm, to=to)))


def _receive(db, transfer_id, *, only=None) -> int:
    """A GRN for the transfer at its To site: every box acknowledged and finalized, or
    only `only` acknowledged and the rest written off (close with shortage)."""
    ti = 71250001 + len(db.tables["interunit_transfer_in_header"])
    h = ro._header(db, transfer_id)
    db.tables["interunit_transfer_in_header"].append({
        "id": ti, "transfer_out_id": transfer_id, "transfer_out_no": h["challan_no"], "grn_number": f"GRN-{ti}",
        "grn_date": ro.RECEIVED_AT, "receiving_warehouse": h["to_site"], "received_by": USER,
        "received_at": ro.RECEIVED_AT, "box_condition": "Good", "condition_remarks": None, "status": "Pending",
        "created_at": ro.RECEIVED_AT, "updated_at": None})
    out = [b for b in db.tables["interunit_transfer_boxes"] if b["header_id"] == transfer_id]
    for n, b in enumerate(b for b in out if only is None or b["box_id"] in only):
        db.tables["interunit_transfer_in_boxes"].append({
            "id": ti * 100 + n, "header_id": ti, "box_id": b["box_id"], "article": b["article"],
            "batch_number": b["batch_number"], "lot_number": b["lot_number"], "transaction_no": b["transaction_no"],
            "net_weight": b["net_weight"], "gross_weight": b["gross_weight"],
            "scanned_at": ro.RECEIVED_AT + timedelta(minutes=n), "is_matched": True, "transfer_out_box_id": None,
            "issue": None, "line_index": None, "inward_box_id": None})
    if only is None:
        asyncio.run(receive_service.finalize_transfer_in(db, ti, schemas.FinalizeTransferIn(box_condition="Good")))
    else:
        asyncio.run(receive_service.close_with_shortage(db, ti, "not on the truck", USER))
    assert ro._header(db, transfer_id)["status"] == "Received"
    return ti


def _at_a185(db) -> int:
    """V2, BULK and COLD sent W202 -> A-185 on TRANS-IN and received there in full."""
    t1 = _send(db, V2, BULK, COLD, challan="TRANS-IN", at=_oct(1))
    _receive(db, t1)
    return t1


# The three lookups, each for a box it finds: (path, its keyword arguments, the box_id).
LOOKUPS = {
    "manual-boxes_v2": ("manual", {"box_number": 5, "tno": TNO}, BOX),
    "tr-boxes_v2": ("tr", {"box_id": BOX, "tno": TNO}, BOX),
    "be-bulk": ("be", {"box_id": BULK.box_id, "tno": BULK.transaction_no}, BULK.box_id),
    "manual-cold": ("manual", {"box_number": 4, "tno": COLD.transaction_no}, COLD.box_id),
    "tr-cold": ("tr", {"box_id": COLD.box_id, "tno": COLD.transaction_no}, COLD.box_id),
}


def _lookup_refusal(db, name, from_wh) -> str:
    path, kw, _ = LOOKUPS[name]
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, path, from_wh=from_wh, **kw)
    assert exc.value.status_code == 409
    return exc.value.detail


def _received_at(box_id, site="A185", challan="TRANS-IN") -> str:
    # The refusal names the site by its canonical name (a transfer to 'A-185' is at A185).
    return f"Box {box_id} was received at {site} on challan {challan}; send it from {site}."


def _in_transit(box_id, challan) -> str:
    return f"Box {box_id} is already on challan {challan} (in transit)"


def _short(box_id, challan="TRANS-IN") -> str:
    return f"Box {box_id} did not arrive on challan {challan} (written off as short)."


@pytest.mark.parametrize("name", LOOKUPS)
def test_a_box_received_at_a_site_is_found_from_there_and_refused_from_elsewhere_or_nowhere(name):
    db = ro._db()
    _at_a185(db)
    path, kw, box_id = LOOKUPS[name]
    for here in ("A-185", "A185", "a 185"):
        assert cold._lookup(db, path, from_wh=here, **kw)["box_id"] == box_id
    # Another site, and no From site at all (every transfer in the history blocks then).
    for here in ("W202", None, "", "  "):
        assert _lookup_refusal(db, name, here) == _received_at(box_id)
    assert cold.stock_writes(db) == []


def test_a_box_received_at_a_site_is_sent_on_from_there_and_is_then_in_transit_again():
    db = ro._db()
    _at_a185(db)

    # From another site: refused, every such box named, nothing written.
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _send(db, FREE, V2, COLD, challan="TRANS-OUT", frm="W202", to="W303", at=_oct(5))
    assert exc.value.status_code == 409
    assert exc.value.detail == f"{_received_at(BOX)} {_received_at(COLD.box_id)}"
    assert db.tables == before

    # From A185 (A-185 as a warehouse code): sent, copied to in transit, no stock written.
    t2 = _send(db, V2, COLD, challan="TRANS-OUT", frm="A185", to="W303", at=_oct(5))
    assert {k: p["from_site"] for k, p in ro._parked(db, t2).items()} == {BOX: "A185", COLD.box_id: "A185"}
    ro._assert_stock_untouched(db)
    # Now in transit on TRANS-OUT, from every site, A185 included; BULK stayed at A185.
    for here in ("A185", "W303", None):
        assert _lookup_refusal(db, "tr-boxes_v2", here) == _in_transit(BOX, "TRANS-OUT")
    assert cold._lookup(db, "be", box_id=BULK.box_id, tno=BULK.transaction_no, from_wh="A185")

    # Received at W303: it may go on from there, and only from there.
    _receive(db, t2)
    assert cold._lookup(db, "tr", from_wh="W303")["box_id"] == BOX
    for here in ("A185", "W202"):
        assert _lookup_refusal(db, "tr-boxes_v2", here) == _received_at(BOX, "W303", "TRANS-OUT")
    _send(db, V2, challan="TRANS-ON", frm="W303", to="W202", at=_oct(9))


def test_the_receipt_read_asks_only_for_the_boxes_being_judged():
    db = ro._db()
    t1 = _at_a185(db)
    # A receipt stores box_id as the client sent it: a padded one still counts.
    (acked,) = [b for b in db.tables["interunit_transfer_in_boxes"] if b["box_id"] == BOX]
    acked["box_id"] = f" {BOX} "
    db.calls.clear()
    assert cold._lookup(db, "tr", from_wh="A185")["box_id"] == BOX
    # The receipts read are those of the box's latest transfer only, and their box rows
    # those of its receipts only.
    (grn,) = db.tables["interunit_transfer_in_header"]
    sent = {s: a for _, s, a in db.calls}
    assert sent[" ".join(lookup_service._RECEIPTS.split())] == ([t1],)
    assert sent[" ".join(lookup_service._RECEIPT_BOXES.split())] == ([grn["id"]],)


@pytest.mark.parametrize("undo", ["reopen", "delete"])
def test_undoing_an_earlier_receipt_does_not_put_back_in_transit_a_box_that_went_on(undo):
    db = ro._db()
    t1 = _send(db, V2, BULK, challan="TRANS-IN", at=_oct(1))              # W202 -> A-185
    ti = _receive(db, t1)
    t2 = _send(db, V2, challan="TRANS-OUT", frm="A185", to="W303", at=_oct(5))
    _receive(db, t2)

    if undo == "reopen":
        asyncio.run(receive_service.reopen_transfer_in(db, ti, USER))
    else:
        asyncio.run(delete_service.delete_transfer_in(db, ti, USER))
    # Back in transit on TRANS-IN: the box that stayed at A-185, not the one sent on.
    assert set(ro._parked(db, t1)) == {BULK.box_id}
    # That one is at W303 and goes on from there, in transit on its new challan.
    t3 = _send(db, V2, challan="TRANS-ON", frm="W303", to="W202", at=_oct(9))
    assert set(ro._parked(db, t3)) == {BOX}
    ro._assert_stock_untouched(db)


def test_a_box_written_off_as_short_is_refused_from_every_site():
    db = ro._db()
    t1 = _send(db, V2, BULK, COLD, challan="TRANS-IN", at=_oct(1))
    _receive(db, t1, only={BULK.box_id, COLD.box_id})          # V2 never arrived
    for here in ("A-185", "W202", None):
        assert _lookup_refusal(db, "tr-boxes_v2", here) == _short(BOX)
        assert _lookup_refusal(db, "manual-boxes_v2", here) == _short(BOX)
    # The boxes that did arrive are at A-185.
    assert cold._lookup(db, "be", box_id=BULK.box_id, tno=BULK.transaction_no, from_wh="A185")

    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _send(db, COLD, V2, challan="TRANS-OUT", frm="A185", to="W303", at=_oct(5))
    assert exc.value.status_code == 409 and exc.value.detail == _short(BOX)
    assert db.tables == before
    _send(db, COLD, BULK, challan="TRANS-OUT", frm="A185", to="W303", at=_oct(5))


def test_a_received_transfer_without_its_receipt_still_has_its_boxes_in_transit():
    db = ro._db()
    t1 = _send(db, V2, challan="TRANS-IN", at=_oct(1))
    ro._header(db, t1)["status"] = "Received"                   # no GRN row
    for here in ("A-185", "W202", None):
        assert _lookup_refusal(db, "tr-boxes_v2", here) == _in_transit(BOX, "TRANS-IN")
    with pytest.raises(HTTPException) as exc:
        _send(db, V2, challan="TRANS-OUT", frm="A185", to="W303", at=_oct(5))
    assert exc.value.detail == _in_transit(BOX, "TRANS-IN")


def test_without_the_receipt_tables_a_received_transfer_is_in_transit():
    db = dl._db()
    dl._create(db, dl._payload())                               # A-185 -> W202
    dl._header(db)["status"] = "Received"
    challan = dl._header(db)["challan_no"]
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, "tr", box_id=dl.ALMOND_BOXES[0], from_wh="W202")
    assert exc.value.status_code == 409 and exc.value.detail == _in_transit(dl.ALMOND_BOXES[0], challan)


def test_a_box_whose_receipt_was_reopened_is_in_transit_again():
    db = ro._db()
    t1 = _send(db, V2, challan="TRANS-IN", at=_oct(1))
    ti = _receive(db, t1)
    assert cold._lookup(db, "tr", from_wh="A185")["box_id"] == BOX
    asyncio.run(receive_service.reopen_transfer_in(db, ti, USER))
    assert _lookup_refusal(db, "tr-boxes_v2", "A185") == _in_transit(BOX, "TRANS-IN")


def _history_db(*transfers) -> ro.ReceiveDB:
    """ro._db() plus hand-made transfers of V2's box: (id, challan, to_site, status,
    created_ts, acknowledged — None: no receipt; False: a receipt without the box)."""
    db = ro._db()
    for tid, challan, to_site, status, created, acked in transfers:
        db.tables["interunit_transfers_header"].append(
            {"id": tid, "challan_no": challan, "from_site": "W909", "to_site": to_site, "status": status,
             "created_ts": created})
        db.tables["interunit_transfer_boxes"].append({"id": tid + 1, "header_id": tid, "box_id": BOX, "transaction_no": TNO})
        if acked is not None:
            db.tables["interunit_transfer_in_header"].append({"id": tid + 2, "transfer_out_id": tid, "status": "Received"})
        if acked:
            db.tables["interunit_transfer_in_boxes"].append(
                {"id": tid + 3, "header_id": tid + 2, "box_id": BOX, "transaction_no": f" {TNO} "})
    return db


# Ids are time-based short ids and wrap, so a newer transfer can have the smaller id.
OLD_AT_A185 = (99000001, "TRANS-OLD", "A-185", "Received", _oct(1), True)


@pytest.mark.parametrize("transfers, here, refusal", [
    ([OLD_AT_A185, (10000002, "TRANS-NEW", "W303", "Dispatch", _oct(5), None)], "A185",
     _in_transit(BOX, "TRANS-NEW")),
    ([OLD_AT_A185, (10000002, "TRANS-NEW", "W303", "Received", _oct(5), True)], "W303", None),
    ([OLD_AT_A185, (10000002, "TRANS-NEW", "W303", "Received", _oct(5), True)], "A185",
     _received_at(BOX, "W303", "TRANS-NEW")),
    ([OLD_AT_A185, (10000002, "TRANS-NEW", "W303", "Received", _oct(5), False)], "W303", _short(BOX, "TRANS-NEW")),
    ([(10000002, "TRANS-OLD", "W303", "Dispatch", _oct(1), None), (99000001, "TRANS-NEW", "A-185", "Received",
                                                                       _oct(5), True)], "A185", None),
    # Without created_ts (a legacy row) a transfer is the oldest; between two such, the id decides.
    ([OLD_AT_A185, (99000009, "TRANS-LEGACY", "W303", "Dispatch", None, None)], "A185", None),
    ([(10000002, "TRANS-LEGACY", "W303", "Dispatch", None, None),
      (99000001, "TRANS-LEGACY-2", "A-185", "Received", None, True)], "A185", None),
], ids=["newer-smaller-id-in-transit", "newer-smaller-id-received-there", "newer-smaller-id-received-elsewhere",
        "newer-smaller-id-short", "newer-bigger-id-received-there", "no-created_ts-is-oldest",
        "no-created_ts-either-by-id"])
def test_the_latest_transfer_is_the_newest_by_created_ts_not_by_id(transfers, here, refusal):
    db = _history_db(*transfers)
    if refusal is None:
        assert cold._lookup(db, "tr", from_wh=here)["box_id"] == BOX
        _send(db, V2, challan="TRANS-3", frm=here, to="W808", at=_oct(9))
        return
    assert _lookup_refusal(db, "tr-boxes_v2", here) == refusal
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _send(db, V2, challan="TRANS-3", frm=here, to="W808", at=_oct(9))
    assert exc.value.detail == refusal and db.tables == before


def test_an_edit_judges_its_boxes_without_its_own_transfer():
    db = ro._db()
    t1 = _at_a185(db)
    t2 = _send(db, V2, challan="TRANS-OUT", frm="A185", to="W303", at=_oct(5))

    # Its own box is judged by where it was before this transfer: received at A-185, its
    # From. Kept, and a box also at A-185 added.
    _edit(db, t2, V2, COLD, frm="A185", to="W303")
    assert set(ro._parked(db, t2)) == {BOX, COLD.box_id}

    # Sent from another site instead: refused, as the boxes are at A-185.
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _edit(db, t2, V2, frm="W202", to="W303")
    assert exc.value.detail == _received_at(BOX)
    # The older transfer has been received, so it is not edited at all (before its boxes
    # are judged: the newer one has the box in transit).
    with pytest.raises(HTTPException) as exc:
        _edit(db, t1, V2, BULK, frm="W202", to="A-185")
    assert exc.value.detail == "Transfer TRANS-IN has already been received at A185; it can't be edited."
    assert db.tables == before


def test_a_dispatch_lists_each_box_it_may_not_take_with_its_reason():
    db = ro._db()
    t1 = _send(db, V2, BULK, challan="TRANS-IN", at=_oct(1))
    _receive(db, t1, only={BULK.box_id})                        # V2 written off
    _send(db, COLD, challan="TRANS-MOVE", frm="W202", to="W303", at=_oct(2))
    before = copy.deepcopy(db.tables)

    with pytest.raises(HTTPException) as exc:
        _send(db, FREE, COLD, BULK, V2, challan="TRANS-X", frm="W202", to="W909", at=_oct(3))
    assert exc.value.status_code == 409
    assert exc.value.detail == (f"{_in_transit(COLD.box_id, 'TRANS-MOVE')}. {_received_at(BULK.box_id)} "
                                f"{_short(BOX)}")
    assert db.tables == before


def test_a_request_dispatch_takes_a_box_received_at_its_from_site():
    # Received at A-185 on an earlier transfer, then sent A-185 -> W202 for the request.
    db = dl._db()
    db.tables.update(interunit_transfer_in_header=[], interunit_transfer_in_boxes=[])
    db.tables["interunit_transfers_header"].append(
        {"id": 5, "challan_no": "TRANS-IN", "from_site": "W909", "to_site": "A185", "status": "Received",
         "created_ts": _oct(1)})
    db.tables["interunit_transfer_boxes"] += [
        {"id": 6 + n, "header_id": 5, "box_id": b, "transaction_no": TNO} for n, b in enumerate(dl.ALMOND_BOXES)]
    db.tables["interunit_transfer_in_header"].append({"id": 8, "transfer_out_id": 5, "status": "Received"})
    db.tables["interunit_transfer_in_boxes"].append(
        {"id": 9, "header_id": 8, "box_id": dl.ALMOND_BOXES[0], "transaction_no": TNO})

    # The second almond box was not acknowledged on TRANS-IN: written off.
    assert _refused(db, dl._payload()) == _short(dl.ALMOND_BOXES[1])
    dl._create(db, dl._payload(boxes=[A1, CASHEW]))
    (t,) = [h for h in db.tables["interunit_transfers_header"] if h.get("request_id") == dl.RID]
    assert {b["box_id"] for b in db.tables["interunit_transfer_boxes"] if b["header_id"] == t["id"]} == {
        dl.ALMOND_BOXES[0], dl.CASHEW_BOX}


# ── The lookup endpoints pass the From site through ──────────────────────────
ENDPOINTS = [
    ("box-lookup/cfpl", {"box_number": 5, "transaction_no": TNO}, BOX),
    ("box-lookup-by-id/cfpl", {"box_id": BOX, "transaction_no": TNO}, BOX),
    ("bulk-entry-box-lookup/cdpl", {"box_id": BULK.box_id, "transaction_no": BULK.transaction_no}, BULK.box_id),
]


@pytest.mark.parametrize("path, params, box_id", ENDPOINTS, ids=["manual", "tr-qr", "be-qr"])
def test_each_lookup_endpoint_takes_the_from_warehouse(client, path, params, box_id):  # noqa: F811
    db = ro._db()
    _at_a185(db)
    c = client(db)
    url = f"/api/v1/transfer/{path}"
    for here in ("A185", "a-185"):
        res = c.get(url, params={**params, "from_warehouse": here})
        assert res.status_code == 200 and res.json()["box"]["box_id"] == box_id
    for other in ({"from_warehouse": "W202"}, {}):
        res = c.get(url, params={**params, **other})
        assert res.status_code == 409 and res.json()["message"] == _received_at(box_id)
