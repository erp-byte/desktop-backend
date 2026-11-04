"""Record only, at its edges: the stock row a box was copied from may be gone or moved,
two dispatches may race for one box, a box id may carry whitespace, and the cold box
pick must move past the boxes already on a transfer. Against the SQL-evaluating fake
database of test_transfer_box_lookup_cold, with the helpers of test_transfer_record_only.

What is pinned:
  - re-open (unpick_to_pending) matches the stock row on box_id AND transaction_no only,
    also for a cold From site: with only another inward's row of the same box_id the box
    parks from its own received values, never from that row;
  - an edit keeps each kept box's own copy (source_table, source_row_id, cold snapshot,
    cold item / lot / weight) even when its stock row is gone, applies the new route and
    the box's edited weights, parks an added box from its stock row, looks a kept box
    parked without a source up again, and drops a removed box's row;
  - interunit_transfer_boxes stores box_id / transaction_no trimmed, so a padded id
    is matched by the box guard afterwards;
  - POST / PUT /transfers take one advisory lock per real box (trimmed, sorted, quantity-
    only entries left out) inside the transaction, before the box guard reads and
    before anything is written; none without real boxes;
  - the cold box pick (FIFO) judges each box as the box guard does (R3, the real
    lookup_service.unavailable_boxes): a box in transit is skipped from every site, a
    box received and acknowledged at the From site is picked there and nowhere else,
    a box of the transfer being edited may be picked again, the boxes already on the
    list are skipped; a padded cold box_id matches as trimmed; it writes nothing;
    without the transfer tables it is the plain FIFO pick;
  - migration 123 (static): the index, to_regclass-guarded, registered after 122.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_record_only_edges.py
"""
from __future__ import annotations

import asyncio
import copy
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.modules.cold_storage.services import search_service
from app.modules.transfer.services import create_service, lookup_service, query_service, receive_service
from tests.services import test_transfer_box_lookup_cold as cold
from tests.services import test_transfer_record_only as ro

USER, BOX, TNO = ro.USER, ro.BOX, ro.TNO
V2, BULK, COLD, FREE = ro.V2, ro.BULK, ro.COLD, ro.FREE


@pytest.fixture(autouse=True)
def _fresh_existence_cache(monkeypatch):
    # lookup_service caches to_regclass answers for the process; each test starts clean.
    monkeypatch.setattr(lookup_service, "_EXISTS", {})


@pytest.fixture(autouse=True)
def no_readback(monkeypatch):
    async def get_transfer(conn, transfer_id):
        return {"id": transfer_id}
    monkeypatch.setattr(query_service, "get_transfer", get_transfer)


# ── Re-open at a cold From site: exact match only ───────────────────────────
OLD = 5
# Another inward's box that shares the label: box_id is not unique.
NAMESAKE = cold._cold(id=9999, transaction_no="TR-OTHER-INWARD", item_description="CASHEW W240",
                      lot_no="LOT-OTHER", weight_kg=Decimal("10"), unit="Supreme Cold")


def _received_from(db, from_site):
    """A transfer from `from_site`, received in full: its records and its GRN, no
    in-transit rows (the receipt cleared them)."""
    db.tables["interunit_transfers_header"].append(
        {"id": OLD, "challan_no": "TRANS-OLD", "from_site": from_site, "to_site": "W202", "status": "Received",
         "request_id": None, "created_by": USER, "created_ts": datetime(2026, 10, 1)})
    db.tables["interunit_transfer_boxes"].append(
        {"id": 6, "header_id": OLD, "box_id": BOX, "article": "ALMOND 25KG", "batch_number": "", "lot_number": "LOT-9",
         "transaction_no": TNO, "net_weight": Decimal("24.75"), "gross_weight": Decimal("24.75")})
    ro._receipt(db, OLD)
    (grn,) = db.tables["interunit_transfer_in_header"]
    grn["status"] = "Received"


@pytest.mark.parametrize("exact", [False, True], ids=["only-a-namesake", "exact-row"])
def test_reopen_at_a_cold_from_site_never_copies_another_inwards_row(exact):
    db = ro._db()
    db.tables["cfpl_boxes_v2"].clear()          # the box's only stock row would be its cold one
    db.tables["cfpl_cold_stocks"] = [NAMESAKE] + ([cold._cold(id=7101)] if exact else [])
    stock = copy.deepcopy({t: db.tables[t] for t in cold.STOCK_TABLES})
    _received_from(db, "Rishi")

    asyncio.run(receive_service.reopen_transfer_in(db, ro.TI, USER))
    p = ro._parked(db, OLD)[BOX]
    if exact:
        assert (p["source_table"], p["source_row_id"], p["item_description"], p["lot_no"]) == (
            "cfpl_cold_stocks", 7101, "ALMOND 25KG", "LOT-9")
        assert p["cold_storage_data"] == cold.SNAPSHOT
    else:
        # Its own received values; a cold From's guessed source; no cold snapshot.
        assert (p["source_table"], p["source_row_id"], p["cold_storage_data"]) == ("cdpl_cold_stocks", None, None)
        assert (p["item_description"], p["lot_no"], p["weight_kg"]) == ("ALMOND 25KG", "LOT-9", Decimal("24.75"))
    assert (p["from_storage_type"], p["status"]) == ("cold", "In Transit")
    # box_id alone is never looked up, and no stock table is written.
    assert not [s for s in db.statements("SELECT *") if s.endswith("WHERE box_id = $1 LIMIT 1")]
    assert {t: db.tables[t] for t in cold.STOCK_TABLES} == stock and cold.stock_writes(db) == []


# ── Edit: a kept box keeps its own copy ─────────────────────────────────────
def _edit(db, transfer_id, *boxes, to_wh="W202", date="15-10-2026"):
    payload = ro._transfer(*boxes, challan="TRANS-A", to_wh=to_wh)
    payload.header.stock_trf_date = date
    return asyncio.run(create_service.update_transfer(db, transfer_id, payload))


def test_an_edit_keeps_each_kept_boxs_copy_even_when_its_stock_row_is_gone():
    db = ro._db()
    a = ro._create(db, V2, COLD, FREE, BULK)
    before = ro._parked(db, a)
    assert before[FREE.box_id]["source_row_id"] is None          # no stock row at dispatch
    # The stock rows go (a transfer dispatched before record only had them deleted; or
    # IMS moved them since), and FREE's appears.
    db.tables["cfpl_boxes_v2"].clear()
    db.tables["cdpl_cold_stocks"].clear()
    db.tables["cfpl_bulk_entry_boxes"].append(
        cold._bulk(id=41, box_id=FREE.box_id, transaction_no=FREE.transaction_no, article_description="ALMOND 25KG"))
    stock = copy.deepcopy({t: db.tables[t] for t in cold.STOCK_TABLES})
    edited_cold = ro._bx(4, COLD.box_id, COLD.transaction_no, net="24.5", gross="25.0")

    _edit(db, a, V2, edited_cold, FREE, to_wh="W303", date="20-10-2026")     # BULK removed
    after = ro._parked(db, a)
    assert set(after) == {V2.box_id, COLD.box_id, FREE.box_id}
    # Kept: the dispatch's copy, the new route, and (cold) the box's edited weights.
    assert after[V2.box_id] == {**before[V2.box_id], "to_site": "W303"}
    assert after[COLD.box_id] == {**before[COLD.box_id], "to_site": "W303",
                                  "gross_weight": Decimal("25.0"), "net_weight": Decimal("24.5")}
    assert after[COLD.box_id]["cold_storage_data"]["unit"] == "Savla D-39"
    # Parked without a source at dispatch: looked up again, and found now.
    assert (after[FREE.box_id]["source_table"], after[FREE.box_id]["source_row_id"]) == ("cfpl_bulk_entry_boxes", 41)
    assert {p["dispatched_at"] for p in db.tables["pending_transfer_stock"]} == {datetime(2026, 10, 20)}
    assert {t: db.tables[t] for t in cold.STOCK_TABLES} == stock and cold.stock_writes(db) == []


def test_an_edit_parks_an_added_box_from_its_stock_row_and_drops_a_removed_ones_row():
    db = ro._db()
    a = ro._create(db, V2, COLD)
    _edit(db, a, V2, BULK)
    assert {k: (p["source_table"], p["source_row_id"]) for k, p in ro._parked(db, a).items()} == {
        V2.box_id: ("cfpl_boxes_v2", 11), BULK.box_id: ("cdpl_bulk_entry_boxes", 31)}
    ro._assert_stock_untouched(db)


# ── Box ids are stored trimmed ──────────────────────────────────────────────
def test_a_padded_box_id_is_stored_trimmed_and_the_guard_matches_it_afterwards():
    db = ro._db()
    a = ro._create(db, ro._bx(5, f" {BOX} ", f"{TNO} "), challan="TRANS-A")
    (row,) = db.tables["interunit_transfer_boxes"]
    assert (row["box_id"], row["transaction_no"]) == (BOX, TNO)
    assert set(ro._parked(db, a)) == {BOX}

    with pytest.raises(HTTPException) as exc:
        ro._create(db, V2, challan="TRANS-B")
    assert exc.value.status_code == 409 and exc.value.detail == ro._refused(V2, "TRANS-A")
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, "tr")
    assert exc.value.status_code == 409
    assert [h["challan_no"] for h in db.tables["interunit_transfers_header"]] == ["TRANS-A"]


# ── The boxes are locked before the guard reads ─────────────────────────────
def _key(box) -> str:
    return f"interunit_transfer_box|{box.box_id}|{box.transaction_no}"


def _order(db) -> list[str]:
    """The lock, the guard's read and the first write, as sent."""
    out = []
    for _, s, _ in db.calls:
        kind = ("lock" if s == cold._BOX_LOCK else "guard" if s.startswith(ro.GUARD)
                else "write" if s.startswith(("INSERT", "UPDATE", "DELETE")) else None)
        if kind and kind not in out:
            out.append(kind)
    return out


def test_a_dispatch_locks_its_real_boxes_in_order_before_the_guard_reads():
    db = ro._db()
    ro._create(db, V2, ro._bx(2, "", "DIRECT"), ro._bx(9, f"{FREE.box_id} ", FREE.transaction_no), COLD)
    assert db.box_locks == [sorted([_key(V2), _key(FREE), _key(COLD)])]
    assert _order(db) == ["lock", "guard", "write"]


def test_an_edit_locks_its_boxes_and_a_refused_dispatch_writes_nothing():
    db = ro._db()
    a = ro._create(db, V2)
    _edit(db, a, V2, BULK)
    assert db.box_locks[-1] == sorted([_key(V2), _key(BULK)])

    db.calls.clear()
    before = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        ro._create(db, BULK, challan="TRANS-B")
    assert exc.value.status_code == 409
    assert _order(db) == ["lock", "guard"] and db.tables == before


def test_no_lock_without_a_real_box():
    db = ro._db()
    ro._create(db, ro._bx(1, "", "DIRECT"), ro._bx(2, None, None))
    assert db.box_locks == [] and not db.statements(cold._BOX_LOCK)


# ── The cold box pick judges each box by R3 ─────────────────────────────────
_PICK = re.compile(r"(SELECT .+? WHERE .+?)(?: AND id > \$(\d+))? ORDER BY id ASC LIMIT \$(\d+)")


class PickDB(cold.FakeDB):
    """The fake database plus pick_boxes' page statement: its WHERE is evaluated by the
    fake's parser, then the keyset (id > $n), ORDER BY id and LIMIT $m are applied. The
    box guard's reads (R3) are the fake's own."""

    def _run(self, kind, sql, args):
        s = " ".join(sql.split())
        m = _PICK.fullmatch(s)
        if not m:
            return super()._run(kind, sql, args)
        self.calls.append((kind, s, args))
        rows = self._rows(cold._Parser(m[1]).statement(), args)
        if m[2]:
            rows = [r for r in rows if r["id"] > args[int(m[2]) - 1]]
        return sorted(rows, key=lambda r: r["id"])[:args[int(m[3]) - 1]], None


LOT_BOXES = ["CS-1", "CS-2", "CS-3", "CS-4", "CS-5 ", "CS-6"]


def _pick_db(*, transfers=True) -> PickDB:
    """Six boxes of one cold lot (ids 101..106, stored out of order), one of another lot,
    and boxes on transfers from Rishi to A185: CS-1 in transit (Dispatch), CS-3 received
    at A185 and acknowledged there, CS-5 in transit (Partial, its cold box_id padded);
    CS-2 only under another transaction; CS-4 on a transfer that is gone."""
    rows = [cold._cold(id=101 + n, box_id=b, transaction_no="CS-TXN") for n, b in enumerate(LOT_BOXES)]
    tables = {"cfpl_cold_stocks": rows[::-1] + [cold._cold(id=99, box_id="OTHER-1", lot_no="LOT-1")]}
    if not transfers:
        return PickDB(**tables)
    tables["interunit_transfers_header"] = [
        {"id": h, "challan_no": f"T-{h}", "status": st, "from_site": "Rishi", "to_site": "A185",
         "request_id": None, "created_by": USER, "created_ts": datetime(2026, 10, h)}
        for h, st in ((1, "Dispatch"), (2, "Received"), (3, "Partial"))]
    tables["interunit_transfer_boxes"] = [
        {"id": 10 + n, "header_id": h, "box_id": b, "transaction_no": t, "article": "ALMOND 25KG",
         "batch_number": "", "lot_number": "LOT-9", "net_weight": Decimal("24.75"),
         "gross_weight": Decimal("24.75"), "created_at": None}
        for n, (h, b, t) in enumerate(((1, "CS-1", "CS-TXN"), (2, "CS-3", "CS-TXN"), (3, "CS-5", "CS-TXN"),
                                       (1, "CS-2", "OTHER-TXN"), (404, "CS-4", "CS-TXN")), 1)]
    tables["interunit_transfer_in_header"], tables["interunit_transfer_in_boxes"] = [], []
    db = PickDB(**tables)
    ro._receipt(db, 2)                         # CS-3 acknowledged on transfer 2's GRN
    db.tables["interunit_transfer_in_header"][0]["status"] = "Received"
    return db


def _pick(db, qty, **kw) -> list[str]:
    out = asyncio.run(search_service.pick_boxes(db, company="cfpl", item_description="ALMOND 25KG", lot_no="LOT-9",
                                                inward_no="INW-881", qty=qty, **kw))
    return [b["box_id"] for b in out["boxes"]]


@pytest.mark.parametrize("kw, picked", [
    # No From site: as strict as before — a box with any transfer history is skipped.
    ({}, ["CS-2", "CS-4", "CS-6"]),
    # CS-3 was received at A185: picked there (in any spelling), not from anywhere else.
    ({"from_warehouse": "A185"}, ["CS-2", "CS-3", "CS-4", "CS-6"]),
    ({"from_warehouse": "Warehouse A185"}, ["CS-2", "CS-3", "CS-4", "CS-6"]),
    ({"from_warehouse": "W202"}, ["CS-2", "CS-4", "CS-6"]),
    # An edit of transfer 1: its own CS-1 may be picked again; CS-5 stays in transit.
    ({"from_warehouse": "A185", "transfer_id": 1}, ["CS-1", "CS-2", "CS-3", "CS-4", "CS-6"]),
    # The boxes already on the list are skipped.
    ({"from_warehouse": "A185", "exclude": "CS-2|CS-TXN,CS-6|CS-TXN"}, ["CS-3", "CS-4"]),
])
def test_the_cold_pick_judges_each_box_by_where_its_transfers_left_it(kw, picked):
    db = _pick_db()
    before = copy.deepcopy(db.tables)
    assert _pick(db, 9, **kw) == picked
    assert db.tables == before and cold.stock_writes(db) == []


@pytest.mark.parametrize("qty, picked", [(1, ["CS-2"]), (2, ["CS-2", "CS-3"]), (3, ["CS-2", "CS-3", "CS-4"])])
def test_the_cold_pick_moves_past_the_boxes_it_may_not_take(qty, picked):
    assert _pick(_pick_db(), qty, from_warehouse="A185") == picked


def test_without_the_transfer_tables_the_cold_pick_is_plain_fifo():
    assert _pick(_pick_db(transfers=False), 2) == ["CS-1", "CS-2"]


# ── Migration 123 (a static check; the SQL is applied by the user, not by tests) ──
ROOT = Path(__file__).resolve().parents[2]
SQL_123 = ROOT / "app" / "db" / "123_interunit_transfer_boxes_box_index.sql"


def test_123_indexes_the_transfer_boxes_only_where_the_table_exists():
    raw = SQL_123.read_bytes()
    lines = [l for l in raw.decode("utf-8").splitlines() if not l.strip().startswith("--")]
    body = re.sub(r"\s+", " ", "\n".join(lines))
    guard = body.index("IF to_regclass('public.interunit_transfer_boxes') IS NOT NULL THEN")
    index = body.index("CREATE INDEX IF NOT EXISTS ix_interunit_transfer_boxes_box_id_tno "
                       "ON public.interunit_transfer_boxes (box_id, transaction_no);")
    assert guard < index < body.index("END IF;")
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1 and "CONCURRENTLY" not in body
    assert raw.count(b"\r\n") == raw.count(b"\n"), "app/db SQL files are CRLF"
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i122 = text.index('DB_DIR / "122_transfer_reason_optional.sql"')
    i123 = text.index('DB_DIR / "123_interunit_transfer_boxes_box_index.sql"')
    assert i122 < i123 < text.index("# ── Optional: drop the v1 legacy")
