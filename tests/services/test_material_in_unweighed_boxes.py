"""Material In box saves and the stock they book.

- Boxes saved before they are weighed: inventory_batch.original_qty_kg is NOT
  NULL, and POST /boxes used to insert one batch per box with the box's net
  weight, so one unweighed box failed the whole save (HTTP 500). Now POST books
  stock only for weighed boxes, and PUT /boxes books it once the weight arrives.
- Corrections after save: PUT /boxes used to change only po_box, so a corrected
  weight, lot or date never reached the box's stock. Now it does, keeping what
  has already been drawn from the batch, and refuses to rewrite used-up stock.
- Retries: each POST section payload carries the browser's key; a payload whose
  key was already applied (a retry after a lost reply) is skipped.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.purchase import router as PR

TXN = "TR-20261019103000"
HEADER = {"transaction_no": TXN, "warehouse": "W202", "entity": "cfpl", "po_date": date(2026, 10, 1)}


class FakeConn:
    def __init__(self, *, unbatched=(), batches=None, after=None, section_dates=None,
                 keys_table=True, applied=()):
        self.unbatched = list(unbatched)   # weighed boxes that have no batch yet
        self.batches = batches or {}       # box_id -> box + batch row before the edit
        self.after = after or {}           # box_id -> box row after the edit
        self.section_dates = section_dates
        self.keys_table = keys_table
        self.applied = set(applied)        # request keys already applied
        self.many: list[tuple[str, list]] = []
        self.executed: list[tuple[str, tuple]] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if "to_regclass('public.po_box_request_key')" in s:
            return "po_box_request_key" if self.keys_table else None
        if s.startswith("INSERT INTO po_box_request_key"):
            if args[1] in self.applied:
                return None
            self.applied.add(args[1])
            return 1
        return None if "SELECT box_id FROM po_box" in s else 0

    async def fetchrow(self, sql, *args):
        if "FROM po_section" in sql:
            return self.section_dates
        return {"sku_name": "Cashew W320"}

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "FOR UPDATE OF b, ib" in s:
            return [self.batches[b] for b in args[1] if b in self.batches]
        if s.startswith("SELECT box_id, net_weight, lot_number FROM po_box"):
            return [self.after[b] for b in args[1] if b in self.after]
        if "NOT EXISTS (SELECT 1 FROM inventory_batch" in s:
            return self.unbatched
        raise AssertionError(f"unexpected fetch: {s}")

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))

    async def executemany(self, sql, rows):
        self.many.append((" ".join(sql.split()), list(rows)))

    def rows(self, prefix):
        return [r for s, rows in self.many if s.startswith(prefix) for r in rows]


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    async def fetchrow(self, sql, *args):
        return HEADER

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture(autouse=True)
def _stub_reads(monkeypatch):
    async def no_status(conn, txn):
        pass

    async def details(pool, txns, rows):
        return [{}]

    monkeypatch.setattr(PR, "_refresh_receiving_status", no_status)
    monkeypatch.setattr(PR, "fetch_po_details", details)
    monkeypatch.setattr(PR, "POHeaderOut", lambda **kw: kw)
    monkeypatch.setattr(PR, "_request_keys_ready", False)


def _request(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=FakePool(conn))))


USER = SimpleNamespace(full_name="Stores Clerk")


def _add(conn, boxes, **section):
    body = PR.AddSectionsRequest(sections=[PR.SectionInput(line_number=1, box_count=len(boxes), boxes=boxes, **section)])
    return asyncio.run(PR.stores_add_boxes(_request(conn), TXN, body, user=USER))


def _update(conn, boxes, **section):
    body = PR.UpdateSectionsRequest(sections=[PR.SectionUpdate(line_number=1, section_number=1, boxes=boxes, **section)])
    return asyncio.run(PR.stores_update_boxes(_request(conn), TXN, body, user=USER))


# ── Boxes saved before weighing ──────────────────────────────────────────────

def test_unweighed_boxes_save_without_a_batch():
    conn = FakeConn()
    _add(conn, [PR.BoxInput(box_id="23456789-1", box_number=1),
                PR.BoxInput(box_id="23456789-2", box_number=2, net_weight=12.5)])
    assert [r[0] for r in conn.rows("INSERT INTO po_box")] == ["23456789-1", "23456789-2"]
    assert [(r[0], r[7]) for r in conn.rows("INSERT INTO inventory_batch")] == [("23456789-2", 12.5)]


def test_a_section_of_only_unweighed_boxes_books_no_stock():
    conn = FakeConn()
    _add(conn, [PR.BoxInput(box_id="23456789-1", box_number=1), PR.BoxInput(box_id="23456789-2", box_number=2)])
    assert len(conn.rows("INSERT INTO po_box")) == 2
    assert conn.rows("INSERT INTO inventory_batch") == []


def test_weighing_a_saved_box_books_its_batch():
    conn = FakeConn(unbatched=[{"box_id": "23456789-1", "lot_number": "L-7", "net_weight": Decimal("10.500"),
                                "sku_name": "Cashew W320", "manufacturing_date": "2026-09-01", "expiry_date": ""}])
    _update(conn, [PR.BoxUpdate(box_id="23456789-1", net_weight=10.5), PR.BoxUpdate(box_id="23456789-2")])
    assert conn.rows("INSERT INTO inventory_batch") == [(
        "23456789-1", "Cashew W320", TXN, "L-7",
        date(2026, 10, 1), date(2026, 9, 1), None,
        Decimal("10.500"), "W202", "cfpl",
    )]


def test_no_batch_when_nothing_newly_weighed():
    conn = FakeConn()
    _update(conn, [PR.BoxUpdate(box_id="23456789-1", gross_weight=11.0)])
    assert conn.rows("INSERT INTO inventory_batch") == []


def test_box_updates_touch_only_this_po():
    conn = FakeConn()
    _update(conn, [PR.BoxUpdate(box_id="23456789-1", gross_weight=11.0)])
    sql, rows = next((s, r) for s, r in conn.many if s.startswith("UPDATE po_box"))
    assert "WHERE box_id = $1 AND transaction_no = $7" in sql
    assert rows[0][-1] == TXN


# ── Corrections reach stock ──────────────────────────────────────────────────

def _batch(net, original, current, status="AVAILABLE", lot="L-7"):
    return {"box_id": "23456789-1", "line_number": 1, "box_number": 4, "net_weight": Decimal(net),
            "lot_number": lot, "status": status, "original_qty_kg": Decimal(original),
            "current_qty_kg": Decimal(current)}


def _after(net, lot="L-7"):
    return {"box_id": "23456789-1", "net_weight": Decimal(net), "lot_number": lot}


def test_weight_correction_keeps_what_was_already_issued():
    # 10 kg received, 2 kg issued, then the box is re-weighed at 9.5 kg: 7.5 kg is left.
    conn = FakeConn(batches={"23456789-1": _batch("10.000", "10.000", "8.000")}, after={"23456789-1": _after("9.500")})
    _update(conn, [PR.BoxUpdate(box_id="23456789-1", net_weight=9.5)])
    assert conn.rows("UPDATE inventory_batch SET original_qty_kg") == [("23456789-1", Decimal("9.500"), Decimal("7.500"))]
    (event,) = conn.rows("INSERT INTO inventory_event_log")
    assert event[:4] == ("23456789-1", "AVAILABLE", Decimal("-0.500"), "Stores Clerk")


def test_correction_below_what_was_issued_is_refused():
    conn = FakeConn(batches={"23456789-1": _batch("10.000", "10.000", "1.000")}, after={"23456789-1": _after("8.000")})
    with pytest.raises(HTTPException) as err:
        _update(conn, [PR.BoxUpdate(box_id="23456789-1", net_weight=8.0)])
    assert err.value.status_code == 409
    assert "line 1 box 4" in err.value.detail["message"]
    assert conn.rows("UPDATE inventory_batch") == []


def test_used_up_box_cannot_be_reweighed():
    conn = FakeConn(batches={"23456789-1": _batch("10.000", "10.000", "0.000", status="ISSUED")},
                    after={"23456789-1": _after("10.500")})
    with pytest.raises(HTTPException) as err:
        _update(conn, [PR.BoxUpdate(box_id="23456789-1", net_weight=10.5)])
    assert err.value.status_code == 409


def test_saving_a_used_up_box_unchanged_is_fine():
    conn = FakeConn(batches={"23456789-1": _batch("10.000", "10.000", "0.000", status="ISSUED")},
                    after={"23456789-1": _after("10.000")})
    _update(conn, [PR.BoxUpdate(box_id="23456789-1", net_weight=10.0)])
    assert conn.rows("UPDATE inventory_batch") == []


def test_lot_correction_reaches_stock_unless_used_up():
    conn = FakeConn(batches={"23456789-1": _batch("10.000", "10.000", "10.000")},
                    after={"23456789-1": _after("10.000", lot="L-8")})
    _update(conn, [PR.BoxUpdate(box_id="23456789-1", lot_number="L-8")])
    assert conn.rows("UPDATE inventory_batch SET lot_number") == [("23456789-1", "L-8")]

    used = FakeConn(batches={"23456789-1": _batch("10.000", "10.000", "0.000", status="ISSUED")},
                    after={"23456789-1": _after("10.000", lot="L-8")})
    _update(used, [PR.BoxUpdate(box_id="23456789-1", lot_number="L-8")])
    assert used.rows("UPDATE inventory_batch") == []


def test_section_dates_reach_every_box_of_the_section():
    conn = FakeConn(section_dates={"manufacturing_date": "2026-09-01", "expiry_date": "2027-03-01"})
    _update(conn, [], manufacturing_date="2026-09-01", expiry_date="2027-03-01")
    sql, args = next((s, a) for s, a in conn.executed if s.startswith("UPDATE inventory_batch ib"))
    assert "b.section_number = $3" in sql
    assert args[:5] == (TXN, 1, 1, date(2026, 9, 1), date(2027, 3, 1))
    assert set(args[5]) == {"ISSUED", "SCRAPPED", "DISCARDED"}


def test_no_date_sync_when_the_save_carries_no_dates():
    conn = FakeConn(section_dates={"manufacturing_date": "2026-09-01", "expiry_date": None})
    _update(conn, [PR.BoxUpdate(box_id="23456789-1", gross_weight=11.0)])
    assert not any(s.startswith("UPDATE inventory_batch ib") for s, _ in conn.executed)


# ── Retries ──────────────────────────────────────────────────────────────────

def test_first_attempt_records_its_key_and_saves():
    conn = FakeConn()
    _add(conn, [PR.BoxInput(box_id="23456789-1", box_number=1, net_weight=5.0)], client_key="k-1")
    assert "k-1" in conn.applied
    assert len(conn.rows("INSERT INTO po_box")) == 1


def test_a_retry_of_an_applied_payload_adds_nothing():
    conn = FakeConn(applied={"k-1"})
    _add(conn, [PR.BoxInput(box_id="23456789-1", box_number=1, net_weight=5.0)], client_key="k-1")
    assert conn.rows("INSERT INTO po_box") == []
    assert conn.rows("INSERT INTO inventory_batch") == []
    assert not any(s.startswith("INSERT INTO po_section") for s, _ in conn.executed)


def test_without_the_key_table_boxes_still_save():
    conn = FakeConn(keys_table=False)
    _add(conn, [PR.BoxInput(box_id="23456789-1", box_number=1, net_weight=5.0)], client_key="k-1")
    assert len(conn.rows("INSERT INTO po_box")) == 1
