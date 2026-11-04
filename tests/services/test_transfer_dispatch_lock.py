"""A dispatch from an approved transfer request is locked to the request, against the
SQL-evaluating fake database of test_transfer_box_lookup_cold.

What is pinned:
  - create_transfer with a request: the route must be the request's (sites compared as
    sites, so "a 185" == "A-185"), else 409 naming the approved route, with nothing
    written; the request's sites are stored by their canonical names; the lines are the
    request's lines in id order with batch / vakkal empty, whatever lines the client
    sends; remark is the request's reason text and reason_code NULL, whatever the client
    sends; stock_trf_date is the client's; a request without lines is 409;
  - update_transfer never changes request_id. On a transfer from a request it applies the
    same rules, and a route other than the request's is refused before anything is
    rewritten; one without a request keeps the client's route and lines; one
    whose request has since been deleted stays locked to what it dispatched (its stored
    route, else 409, its stored lines and remark, no reason_code) instead of failing 404;
  - a blank reason_code is stored as NULL on a transfer without a request;
  - which lines count as boxed (_settle), on a transfer without a request: a line without
    a lot is covered by a scanned box of its article (any case) with any lot, a line with a
    lot only by a box of its article and lot; a box covers one line, the lot-matched lines
    first; an uncovered line parks as LINE- rows; Dispatch / Partial keeps its formula. A
    transfer from a request has no LINE- rows: only its boxes are in transit;
  - migration 122 (static): reason_code loses NOT NULL, to_regclass-guarded, after 121.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_dispatch_lock.py
"""
from __future__ import annotations

import asyncio
import copy
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.modules.transfer import schemas
from app.modules.transfer.services import create_service, delete_service, query_service
from tests.services import test_transfer_box_lookup_cold as cold

RID = 41250001
USER = "store.a185@candorfoods.in"
REASON = "STOCK REQUIREMENT FOR W202"
TNO = cold.TNO
ROUTE_409 = "This request was approved for A185 -> W202; a dispatch from it has to use the same route."


class DispatchDB(cold.FakeDB):
    """The fake database plus the two trailing clauses the request reads use: FOR UPDATE
    (a row lock, only inside a transaction; recorded in `locked`) and ORDER BY <column>;
    and the lock on a transfer's request (create_service._LOCK_REQUEST_OF), whose scalar
    sub-select is evaluated here before the request row is read and recorded as locked
    like any FOR UPDATE read (the statement as sent is the call recorded)."""

    _BY_TRANSFER = re.compile(r"(SELECT .+ FROM interunit_transfer_requests WHERE id = )"
                              r"\(SELECT request_id FROM interunit_transfers_header WHERE id = \$1\) FOR UPDATE")

    def __init__(self, **tables):
        super().__init__(**tables)
        self.locked: list[tuple] = []

    def _run(self, kind, sql, args):
        s = " ".join(sql.split())
        m = self._BY_TRANSFER.fullmatch(s)
        if m:
            assert self.tx_depth, "row locked outside a transaction"
            self.calls.append((kind, s, args))
            self.locked.append((s, args))
            rid = next((h["request_id"] for h in self._table("interunit_transfers_header") if h["id"] == args[0]),
                       None)
            return self._rows(cold._Parser(m[1] + "$1").statement(), (rid,)), None
        if s.endswith(" FOR UPDATE"):
            assert self.tx_depth, "row locked outside a transaction"
            self.locked.append((s, args))
            return super()._run(kind, s.removesuffix(" FOR UPDATE"), args)
        m = re.fullmatch(r"(SELECT .+) ORDER BY (\w+)", s)
        if m:
            rows, tag = super()._run(kind, m[1], args)
            return sorted(rows, key=lambda r: r[m[2]]), tag
        return super()._run(kind, sql, args)


# ── Rows ─────────────────────────────────────────────────────────────────────
def _request(**over):
    row = {"id": RID, "request_no": "REQ202610151000", "from_site": "A-185", "to_site": "W202",
           "reason_code": REASON, "remarks": REASON, "status": "Approved", "updated_at": None}
    row.update(over)
    return row


def _rline(id, **over):
    """A request line as the database returns it (numeric columns as Decimal)."""
    row = {"id": id, "request_id": RID, "rm_pm_fg_type": "RM", "item_category": "NUTS",
           "sub_category": "ALMOND", "item_desc_raw": "ALMOND 25KG", "qty": Decimal("2.000"), "uom": "KG",
           "pack_size": Decimal("25.000"), "unit_pack_size": Decimal("0.000"),
           "net_weight": Decimal("50.000"), "total_weight": Decimal("50.000"), "lot_number": None}
    row.update(over)
    return row


# Stored out of id order (the dispatch reads them ORDER BY id), with another request's line.
REQUEST_LINES = [
    _rline(7302, item_desc_raw="CASHEW W320", sub_category="CASHEW", qty=Decimal("1.000"),
           pack_size=Decimal("10.000"), net_weight=Decimal("10.000"), total_weight=Decimal("10.000"),
           lot_number="L-3"),
    _rline(7301),
    _rline(7300, request_id=RID + 1, item_desc_raw="PISTA"),
]

# (rm_pm_fg_type, item_category, sub_category, item_desc_raw, qty, uom, pack_size, unit_pack_size,
#  net_weight, total_weight, batch_number, lot_number, vakkal) of the transfer lines they become.
DISPATCHED_LINES = [
    ("RM", "NUTS", "ALMOND", "ALMOND 25KG", 2, "KG", Decimal("25"), 0, Decimal("50"), Decimal("50"), "", "", ""),
    ("RM", "NUTS", "CASHEW", "CASHEW W320", 1, "KG", Decimal("10"), 0, Decimal("10"), Decimal("10"), "", "L-3", ""),
]
LINE_COLUMNS = ("rm_pm_fg_type", "item_category", "sub_category", "item_desc_raw", "qty", "uom", "pack_size",
                "unit_pack_size", "net_weight", "total_weight", "batch_number", "lot_number", "vakkal")

ALMOND_BOXES = ("34732254-1", "34732254-2")
CASHEW_BOX = "88120417-3"


def _cold_rows():
    return [cold._cold(id=7101, box_id=ALMOND_BOXES[0]), cold._cold(id=7102, box_id=ALMOND_BOXES[1]),
            cold._cold(id=7103, box_id=CASHEW_BOX, item_description="CASHEW W320", lot_no="L-3")]


def _db(*, request=None, lines=REQUEST_LINES) -> DispatchDB:
    return DispatchDB(
        interunit_transfer_requests=[request or _request()],
        interunit_transfer_request_lines=list(lines),
        interunit_transfers_header=[], interunit_transfers_lines=[], interunit_transfer_boxes=[],
        cfpl_cold_stocks=_cold_rows(),
    )


def _box(box_id, *, article="almond 25kg", lot="LOT-9", n=1):
    # The scanner's article text need not match the line's case.
    return schemas.BoxCreate(box_number=n, box_id=box_id, article=article, lot_number=lot, transaction_no=TNO,
                             net_weight="24.75", gross_weight="0")


ALL_BOXES = [_box(ALMOND_BOXES[0], n=1), _box(ALMOND_BOXES[1], n=2),
             _box(CASHEW_BOX, article="CASHEW W320", lot="L-3", n=3)]


def _payload(*, from_wh="a 185", to_wh="w-202", boxes=ALL_BOXES, request_id=RID, lines=None,
             remark="typed on the screen", reason_code="DAMAGE") -> schemas.TransferCreate:
    """What a client sends. Its lines, remark and reason are not the request's."""
    return schemas.TransferCreate(
        header=schemas.TransferHeaderCreate(stock_trf_date="15-10-2026", from_warehouse=from_wh,
                                            to_warehouse=to_wh, vehicle_no="MH43BP6885",
                                            remark=remark, reason_code=reason_code),
        lines=lines or [schemas.TransferLineCreate(material_type="RM", item_category="NUTS", sub_category="PISTA",
                                                   item_description="PISTA", quantity="9", pack_size="25")],
        boxes=list(boxes) if boxes is not None else None,
        request_id=request_id,
    )


@pytest.fixture(autouse=True)
def no_readback(monkeypatch):
    async def get_transfer(conn, transfer_id):
        return {"id": transfer_id}
    monkeypatch.setattr(query_service, "get_transfer", get_transfer)


def _create(db, payload):
    return asyncio.run(create_service.create_transfer(db, payload, USER))


def _update(db, payload):
    (header,) = db.tables["interunit_transfers_header"]
    return asyncio.run(create_service.update_transfer(db, header["id"], payload))


def _header(db) -> dict:
    (header,) = db.tables["interunit_transfers_header"]
    return header


def _lines(db) -> list[tuple]:
    return [tuple(l[c] for c in LINE_COLUMNS) for l in db.tables["interunit_transfers_lines"]]


def _phantoms(db) -> list[tuple]:
    """(article, lot) of each LINE- tracking row (a line no scanned box covered)."""
    return [(p["item_description"], p["lot_no"]) for p in db.tables["pending_transfer_stock"]
            if p["box_id"].startswith("LINE-")]


def _parked_boxes(db) -> set:
    return {p["box_id"] for p in db.tables["pending_transfer_stock"] if not p["box_id"].startswith("LINE-")}


# ── create_transfer from a request ─────────────────────────────────────────
def test_a_dispatch_from_a_request_is_the_request_as_approved_plus_the_scanned_boxes():
    db = _db()
    assert _create(db, _payload()) == {"id": _header(db)["id"]}

    h = _header(db)
    # The request's sites, by their canonical names ('A-185' is A185).
    assert (h["from_site"], h["to_site"], h["request_id"]) == ("A185", "W202", RID)
    assert (h["remark"], h["reason_code"]) == (REASON, None)
    assert h["stock_trf_date"] == date(2026, 10, 15)
    assert _lines(db) == DISPATCHED_LINES
    assert [l["header_id"] for l in db.tables["interunit_transfers_lines"]] == [h["id"]] * 2
    # Every scanned box parked (copied; record only, the cold rows stay as they were); the
    # request lines are covered by them.
    assert _parked_boxes(db) == {*ALMOND_BOXES, CASHEW_BOX} and _phantoms(db) == []
    assert db.tables["cfpl_cold_stocks"] == _cold_rows() and cold.stock_writes(db) == []
    assert (h["status"], h["unallocated_boxes"]) == ("Dispatch", 0)
    assert {p["from_site"] for p in db.tables["pending_transfer_stock"]} == {"A185"}

    (req,) = db.tables["interunit_transfer_requests"]
    assert req["status"] == "Transferred"
    assert db.locked == [("SELECT status FROM interunit_transfer_requests WHERE id = $1 FOR UPDATE", (RID,))]


@pytest.mark.parametrize("from_wh, to_wh", [("W202", "A185"), ("A185", "W203"), ("B12", "W202")],
                         ids=["swapped", "other-receiver", "other-sender"])
def test_a_dispatch_off_the_approved_route_is_409_and_writes_nothing(from_wh, to_wh):
    db = _db()
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _create(db, _payload(from_wh=from_wh, to_wh=to_wh))
    assert exc.value.status_code == 409 and exc.value.detail == ROUTE_409
    assert db.tables == before


def test_a_request_without_lines_is_409_and_writes_nothing():
    db = _db(lines=[_rline(7300, request_id=RID + 1)])
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _create(db, _payload())
    assert exc.value.status_code == 409 and exc.value.detail == "Request REQ202610151000 has no items to dispatch"
    assert db.tables == before


@pytest.mark.parametrize("reason_code, stored", [("DAMAGE", "DAMAGE"), ("", None), ("  ", None), (None, None)])
def test_without_a_request_the_clients_route_lines_and_optional_reason_are_kept(reason_code, stored):
    db = _db()
    _create(db, _payload(request_id=None, from_wh="A185", to_wh="W202", boxes=None, reason_code=reason_code))

    h = _header(db)
    assert (h["from_site"], h["to_site"], h["request_id"]) == ("A185", "W202", None)
    assert (h["remark"], h["reason_code"]) == ("typed on the screen", stored)
    assert [l[3] for l in _lines(db)] == ["PISTA"]
    assert db.locked == [] and db.tables["interunit_transfer_requests"] == [_request()]


# ── update_transfer ────────────────────────────────────────────────────────
def test_an_edit_of_a_dispatch_from_a_request_keeps_its_route_lines_and_reason():
    db = _db()
    _create(db, _payload())
    challan, created = _header(db)["challan_no"], _header(db)["created_ts"]

    # The edit drops the second almond box and the cashew box, and tries to change the
    # rest: other lines, a remark, a reason, another request.
    other = schemas.TransferLineCreate(material_type="RM", item_category="NUTS", sub_category="ALMOND",
                                       item_description="ALMOND 25KG", quantity="1", pack_size="25")
    _update(db, _payload(from_wh="A185", to_wh="W202", boxes=[_box(ALMOND_BOXES[0])], lines=[other],
                         request_id=RID + 1, remark="edited", reason_code="EXCESS"))

    h = _header(db)
    assert (h["from_site"], h["to_site"], h["request_id"]) == ("A185", "W202", RID)
    assert (h["remark"], h["reason_code"]) == (REASON, None)
    assert (h["challan_no"], h["created_ts"]) == (challan, created) and h["edited_at"] is not None
    assert _lines(db) == DISPATCHED_LINES
    # The dropped boxes are off the transfer (their pending rows gone; cold never changed);
    # only the box left is in transit: the cashew line, now without a box, has no row.
    assert db.tables["cfpl_cold_stocks"] == _cold_rows() and cold.stock_writes(db) == []
    assert _parked_boxes(db) == {ALMOND_BOXES[0]}
    assert _phantoms(db) == []
    assert h["status"] == "Partial"     # 1 box < 3 ordered


def test_an_edit_off_the_approved_route_is_409_before_anything_is_rewritten():
    db = _db()
    _create(db, _payload())
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _update(db, _payload(from_wh="A185", to_wh="W203", boxes=[_box(ALMOND_BOXES[0])]))
    assert exc.value.status_code == 409 and exc.value.detail == ROUTE_409
    assert db.tables == before


def test_an_edit_never_attaches_a_request_to_a_transfer_without_one():
    db = _db()
    _create(db, _payload(request_id=None, from_wh="A185", to_wh="W202", boxes=None))
    line = schemas.TransferLineCreate(material_type="RM", item_category="NUTS", sub_category="CASHEW",
                                      item_description="CASHEW W240", quantity="3", pack_size="10")
    _update(db, _payload(request_id=RID, from_wh="A185", to_wh="W203", boxes=None, lines=[line],
                         remark="edited", reason_code=""))

    h = _header(db)
    assert (h["from_site"], h["to_site"], h["request_id"]) == ("A185", "W203", None)
    assert (h["remark"], h["reason_code"]) == ("edited", None)
    assert [(l[3], l[4]) for l in _lines(db)] == [("CASHEW W240", 3)]
    assert db.tables["interunit_transfer_requests"] == [_request()]


def _drop_request(db, rid=RID) -> None:
    """A request deleted after its dispatch, as older data has it (delete_request now
    refuses a Transferred request): its rows go, its transfer keeps request_id."""
    for table, key in (("interunit_transfer_requests", "id"), ("interunit_transfer_request_lines", "request_id")):
        db.tables[table] = [r for r in db.tables[table] if r[key] != rid]


def _dispatch_then_delete_the_request() -> DispatchDB:
    db = _db()
    _create(db, _payload())
    _drop_request(db)
    assert db.tables["interunit_transfer_requests"] == [] and _header(db)["request_id"] == RID
    return db


def test_an_edit_after_the_request_was_deleted_stays_locked_to_what_was_dispatched():
    db = _dispatch_then_delete_the_request()
    other = schemas.TransferLineCreate(material_type="RM", item_category="NUTS", sub_category="PISTA",
                                       item_description="PISTA", quantity="4", pack_size="25")
    _update(db, _payload(from_wh="a185", to_wh="W-202", boxes=[_box(ALMOND_BOXES[0])], lines=[other],
                         request_id=None, remark="edited", reason_code="EXCESS"))

    h = _header(db)
    assert (h["from_site"], h["to_site"], h["request_id"]) == ("A185", "W202", RID)
    assert (h["remark"], h["reason_code"]) == (REASON, None) and h["edited_at"] is not None
    assert _lines(db) == DISPATCHED_LINES
    assert _parked_boxes(db) == {ALMOND_BOXES[0]}
    assert _phantoms(db) == []
    assert h["status"] == "Partial"     # 1 box < 3 ordered


def test_an_edit_off_the_dispatched_route_after_the_request_was_deleted_is_409():
    db = _dispatch_then_delete_the_request()
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _update(db, _payload(from_wh="A185", to_wh="W203", boxes=[_box(ALMOND_BOXES[0])]))
    assert exc.value.status_code == 409
    assert exc.value.detail == "This transfer was dispatched for A185 -> W202; an edit has to keep the same route."
    assert db.tables == before


# ── which lines a scanned box covers ───────────────────────────────────────
def _line(article, *, lot=None, qty="1"):
    return schemas.TransferLineCreate(material_type="RM", item_category="NUTS", sub_category="X",
                                      item_description=article, quantity=qty, pack_size="25", lot_number=lot)


@pytest.mark.parametrize("lines, boxes, phantoms, status", [
    # A line without a lot: any lot of its article covers it (the article in any case).
    ([_line("ALMOND 25KG", qty="2")], [_box("B-1", n=1), _box("B-2", lot="LOT-7", n=2)], [], "Dispatch"),
    ([_line("ALMOND 25KG", qty="2")], [_box("B-1")], [], "Partial"),
    # A line with a lot needs its lot.
    ([_line("ALMOND 25KG", lot="LOT-1")], [_box("B-1")], [("ALMOND 25KG", "LOT-1")], "Dispatch"),
    ([_line("ALMOND 25KG", lot="LOT-9")], [_box("B-1")], [], "Dispatch"),
    # ...and its article.
    ([_line("CASHEW W320")], [_box("B-1")], [("CASHEW W320", None)], "Dispatch"),
    # A box covers one line.
    ([_line("ALMOND 25KG"), _line("ALMOND 25KG")], [_box("B-1")], [("ALMOND 25KG", None)], "Dispatch"),
    # The lot-matched line takes its box first, whatever the line order.
    ([_line("ALMOND 25KG"), _line("ALMOND 25KG", lot="LOT-9")], [_box("B-1")], [("ALMOND 25KG", None)],
     "Dispatch"),
    ([_line("ALMOND 25KG"), _line("ALMOND 25KG", lot="LOT-9")], [_box("B-1", n=1), _box("B-2", lot="LOT-2", n=2)],
     [], "Dispatch"),
], ids=["no-lot-any-lot", "no-lot-short", "lot-mismatch", "lot-match", "other-article", "one-line-per-box",
        "lot-first", "both-covered"])
def test_which_lines_the_scanned_boxes_cover(lines, boxes, phantoms, status):
    db = _db()
    _create(db, _payload(request_id=None, from_wh="A185", to_wh="W202", lines=lines, boxes=boxes))
    assert _phantoms(db) == phantoms
    assert _parked_boxes(db) == {b.box_id for b in boxes}
    assert _header(db)["status"] == status


# ── migration 122 (a static check; the SQL is applied by the user, not by tests) ──
ROOT = Path(__file__).resolve().parents[2]
SQL_122 = ROOT / "app" / "db" / "122_transfer_reason_optional.sql"


def test_122_drops_not_null_from_reason_code_only_where_the_table_exists():
    raw = SQL_122.read_bytes()
    lines = [l for l in raw.decode("utf-8").splitlines() if not l.strip().startswith("--")]
    body = re.sub(r"\s+", " ", "\n".join(lines))
    guard = body.index("IF to_regclass('public.interunit_transfers_header') IS NOT NULL THEN")
    alter = body.index("ALTER TABLE public.interunit_transfers_header ALTER COLUMN reason_code DROP NOT NULL;")
    assert guard < alter < body.index("END IF;")
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1 and "ROLLBACK" not in body
    assert raw.count(b"\r\n") == raw.count(b"\n"), "app/db SQL files are CRLF"
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i121 = text.index('DB_DIR / "121_transfer_request_decisions.sql"')
    i122 = text.index('DB_DIR / "122_transfer_reason_optional.sql"')
    assert i121 < i122 < text.index("# ── Optional: drop the v1 legacy")
