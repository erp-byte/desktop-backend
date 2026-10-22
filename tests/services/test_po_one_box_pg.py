"""Material In one box at a time -- POST /{txn}/box, PUT|DELETE /{txn}/box/{box_id}
and the whole-section POST /{txn}/boxes that follows -- against a real Postgres.

Runs only when PO_TEST_PG_URL names a throwaway database (it creates and drops
the tables it needs, in a schema of its own); skipped otherwise.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.purchase import router as PR

PG_URL = os.environ.get("PO_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="PO_TEST_PG_URL not set")

TXN = "TR-20261022120000"
DB = Path(__file__).resolve().parents[2] / "app" / "db"
ADMIN = SimpleNamespace(is_admin=True, allowed_warehouses=[], role_ids=[], full_name="Stores Clerk",
                        email="clerk@example.com", phone=None)

SCHEMA = """
CREATE TABLE po_header (transaction_no TEXT PRIMARY KEY, entity TEXT, po_date DATE, warehouse TEXT,
                        status TEXT, consignee_address TEXT, deleted_at TIMESTAMPTZ);
CREATE TABLE po_line (transaction_no TEXT, line_number INT, sku_name TEXT, particulars TEXT,
                      pack_count NUMERIC, po_weight NUMERIC, PRIMARY KEY (transaction_no, line_number));
CREATE TABLE po_section (transaction_no TEXT, line_number INT, section_number INT, lot_number TEXT,
                         box_count INT, manufacturing_date TEXT, expiry_date TEXT,
                         PRIMARY KEY (transaction_no, line_number, section_number));
CREATE TABLE po_box (box_id TEXT PRIMARY KEY, transaction_no TEXT, line_number INT, box_number INT,
                     net_weight NUMERIC, gross_weight NUMERIC, lot_number TEXT, count INT,
                     created_at TIMESTAMPTZ DEFAULT now(), section_number INT, grn_no TEXT,
                     qc_inspection_id TEXT, stage2_approval_id TEXT);
CREATE TABLE inventory_batch (batch_id TEXT PRIMARY KEY, sku_name TEXT, item_type TEXT, transaction_no TEXT,
                              lot_number TEXT, source TEXT, inward_date DATE, manufacturing_date DATE,
                              expiry_date DATE, original_qty_kg NUMERIC NOT NULL, current_qty_kg NUMERIC,
                              warehouse_id TEXT, floor_id TEXT, entity TEXT,
                              status TEXT DEFAULT 'AVAILABLE', updated_at TIMESTAMPTZ);
CREATE TABLE inventory_event_log (id SERIAL, batch_id TEXT, event_type TEXT, from_status TEXT, to_status TEXT,
                                  quantity_kg NUMERIC, reference_type TEXT, performed_by TEXT, notes TEXT);
CREATE TABLE box_edit_logs (id SERIAL, "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now(), email_id VARCHAR NOT NULL,
                            description TEXT NOT NULL, transaction_no VARCHAR NOT NULL, box_id VARCHAR NOT NULL,
                            field_name VARCHAR NOT NULL, old_value TEXT, new_value TEXT, edited_at TIMESTAMP);
"""


def _with_pool(fn):
    """Run fn(pool) on a pool of its own: each call gets its own event loop."""
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2,
                                         server_settings={"search_path": "po_one_box"})
        try:
            return await fn(pool)
        finally:
            await pool.close()
    return asyncio.run(go())


@pytest.fixture
def db(monkeypatch):
    async def setup(pool):
        async with pool.acquire() as c:
            await c.execute("DROP SCHEMA IF EXISTS po_one_box CASCADE; CREATE SCHEMA po_one_box")
            await c.execute(SCHEMA)
            for f in ("128_po_box_request_key.sql", "131_po_box_client_keys.sql"):
                await c.execute((DB / f).read_text(encoding="utf-8"))
            await c.execute(
                "INSERT INTO po_header VALUES ($1, 'cfpl', $2, 'W202', 'pending', NULL, NULL)",
                TXN, date(2026, 10, 1))
            await c.execute("INSERT INTO po_line (transaction_no, line_number, sku_name, pack_count, po_weight) "
                            "VALUES ($1, 1, 'Cashew W320', 20, 20), ($1, 2, 'Almonds', 50, 50)", TXN)

    _with_pool(setup)
    # Tables live in the test schema; the code checks information_schema /
    # to_regclass by the 'public' schema, so mark those checks as passed.
    monkeypatch.setattr(PR, "_client_keys_ready", True)
    monkeypatch.setattr(PR, "_box_log_ready", True)
    monkeypatch.setattr(PR, "_request_keys_ready", True)
    monkeypatch.setattr(PR, "fetch_po_details", _no_details)
    monkeypatch.setattr(PR, "POHeaderOut", lambda **kw: kw)
    return None


async def _no_details(pool, txns, rows, **kw):
    return [{}]


def _req(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))


def create(_db, **body):
    # Validated as the endpoint's response_model would.
    out = PR.OneBoxResponse(**_with_pool(
        lambda pool: PR.stores_add_one_box(_req(pool), TXN, PR.OneBoxCreate(**body), user=ADMIN)))
    return out.box, out.created


def update(_db, box_id, **body):
    return PR.OneBoxResponse(**_with_pool(lambda pool: PR.stores_update_one_box(
        _req(pool), TXN, box_id, PR.OneBoxUpdate(**body), user=ADMIN))).box


def delete(_db, box_id):
    return _with_pool(lambda pool: PR.stores_delete_one_box(_req(pool), TXN, box_id, user=ADMIN))


def add_sections(_db, body):
    return _with_pool(lambda pool: PR.stores_add_boxes(_req(pool), TXN, body, user=ADMIN))


def q(_db, sql, *args):
    async def go(pool):
        async with pool.acquire() as c:
            return [dict(r) for r in await c.fetch(sql, *args)]
    return _with_pool(go)


DRAFT = dict(line_number=1, section_key="sec-A", section={"lot_number": "L1", "manufacturing_date": "2026-09-01",
                                                        "expiry_date": "2027-09-01"})


def test_first_box_of_a_draft_section_makes_the_section(db):
    box, created = create(db, **DRAFT, box_number=3, gross_weight=10, net_weight=9.5, client_key="sec-A:3")
    assert created and box.section_number == 1 and box.box_number == 3
    sec = q(db, "SELECT * FROM po_section")
    assert [(s["section_number"], s["lot_number"], s["box_count"], s["client_key"]) for s in sec] == [(1, "L1", 1, "sec-A")]
    batch = q(db, "SELECT batch_id, original_qty_kg, manufacturing_date, warehouse_id FROM inventory_batch")
    assert batch == [{"batch_id": box.box_id, "original_qty_kg": 9.5,
                      "manufacturing_date": date(2026, 9, 1), "warehouse_id": "W202"}]


def test_later_boxes_of_the_draft_join_its_section_and_keep_their_numbers(db):
    a, _ = create(db, **DRAFT, box_number=3, client_key="sec-A:3")
    b, _ = create(db, **DRAFT, box_number=1, client_key="sec-A:1")
    assert (a.section_number, b.section_number) == (1, 1)
    assert (a.box_number, b.box_number) == (3, 1)
    assert a.box_id != b.box_id
    assert q(db, "SELECT box_count FROM po_section") == [{"box_count": 2}]
    assert q(db, "SELECT count(*) AS n FROM inventory_batch") == [{"n": 0}]       # unweighed: no stock yet


def test_a_retry_returns_the_same_box(db):
    a, created_a = create(db, **DRAFT, box_number=1, net_weight=5, client_key="sec-A:1")
    b, created_b = create(db, **DRAFT, box_number=1, net_weight=5, client_key="sec-A:1")
    assert created_a and not created_b and a.box_id == b.box_id
    assert q(db, "SELECT count(*) AS n FROM po_box") == [{"n": 1}]
    assert q(db, "SELECT box_count FROM po_section") == [{"box_count": 1}]


def test_a_taken_number_moves_to_the_next_free_one(db):
    create(db, **DRAFT, box_number=1, client_key="k1")
    b, _ = create(db, **DRAFT, box_number=1, client_key="k2")
    assert b.box_number == 2


def test_update_changes_only_that_box(db):
    a, _ = create(db, **DRAFT, box_number=1, gross_weight=10, net_weight=9.5, lot_number="L1", client_key="k1")
    b, _ = create(db, **DRAFT, box_number=2, gross_weight=10, net_weight=9.5, lot_number="L1", client_key="k2")
    before_b = q(db, "SELECT * FROM po_box WHERE box_id = $1", b.box_id)
    out = update(db, a.box_id, gross_weight=12, net_weight=11.5, count=4)
    assert (out.gross_weight, out.net_weight, out.count, out.lot_number) == (12, 11.5, 4, "L1")
    assert q(db, "SELECT * FROM po_box WHERE box_id = $1", b.box_id) == before_b
    assert q(db, "SELECT original_qty_kg, current_qty_kg FROM inventory_batch WHERE batch_id = $1", a.box_id) == \
        [{"original_qty_kg": 11.5, "current_qty_kg": 11.5}]
    logged = q(db, "SELECT field_name, old_value, new_value FROM box_edit_logs WHERE box_id = $1 ORDER BY id", a.box_id)
    assert [(r["field_name"], r["new_value"]) for r in logged] == \
        [("net_weight", "11.5"), ("gross_weight", "12"), ("count", "4")]


def test_update_weighs_an_unweighed_box(db):
    a, _ = create(db, **DRAFT, box_number=1, client_key="k1")
    update(db, a.box_id, gross_weight=10, net_weight=9.5)
    assert q(db, "SELECT original_qty_kg FROM inventory_batch WHERE batch_id = $1", a.box_id) == [{"original_qty_kg": 9.5}]


def test_update_of_another_pos_box_is_not_found(db):
    with pytest.raises(HTTPException) as e:
        update(db, "nope-1", net_weight=1)
    assert e.value.status_code == 404


def test_delete_removes_only_that_box(db):
    boxes = [create(db, **DRAFT, box_number=n, gross_weight=10, net_weight=9.5, client_key=f"k{n}")[0] for n in (1, 2, 3)]
    others = q(db, "SELECT box_id, box_number, net_weight FROM po_box WHERE box_id <> $1 ORDER BY box_number",
               boxes[1].box_id)
    delete(db, boxes[1].box_id)
    assert q(db, "SELECT box_id, box_number, net_weight FROM po_box ORDER BY box_number") == others
    assert q(db, "SELECT box_count FROM po_section") == [{"box_count": 2}]
    assert q(db, "SELECT status, current_qty_kg FROM inventory_batch WHERE batch_id = $1", boxes[1].box_id) == \
        [{"status": "DISCARDED", "current_qty_kg": 0}]
    assert q(db, "SELECT status FROM inventory_batch WHERE batch_id <> $1", boxes[1].box_id) == \
        [{"status": "AVAILABLE"}, {"status": "AVAILABLE"}]
    assert q(db, "SELECT field_name FROM box_edit_logs WHERE box_id = $1", boxes[1].box_id) == [{"field_name": "deleted"}]


def test_a_deleted_boxs_id_is_never_given_again(db):
    a, _ = create(db, **DRAFT, box_number=1, client_key="k1")
    delete(db, a.box_id)                                     # the PO's last (and only) box
    b, _ = create(db, **DRAFT, box_number=1, client_key="k2")
    assert b.box_id != a.box_id


def test_delete_refuses_a_box_in_use(db):
    a, _ = create(db, **DRAFT, box_number=1, net_weight=9.5, client_key="k1")
    b, _ = create(db, **DRAFT, box_number=2, net_weight=9.5, client_key="k2")
    q(db, "UPDATE po_box SET grn_no = 'GRN-1' WHERE box_id = $1 RETURNING 1", a.box_id)
    q(db, "UPDATE inventory_batch SET current_qty_kg = 4 WHERE batch_id = $1 RETURNING 1", b.box_id)
    for box_id, why in ((a.box_id, "GRN-1"), (b.box_id, "4")):
        with pytest.raises(HTTPException) as e:
            delete(db, box_id)
        assert e.value.status_code == 409 and why in e.value.detail["message"]
    assert q(db, "SELECT count(*) AS n FROM po_box") == [{"n": 2}]


def test_full_save_after_single_saves_appends_the_rest(db):
    create(db, **DRAFT, box_number=2, net_weight=9.5, client_key="sec-A:2")
    body = PR.AddSectionsRequest(sections=[PR.SectionInput(
        line_number=1, box_count=3, lot_number="L1", client_key="sec-A",
        boxes=[PR.BoxInput(box_id=f"00000000-{n}", box_number=n, net_weight=9.5, client_key=f"sec-A:{n}")
               for n in (1, 2, 3)],
    )])
    add_sections(db, body)
    rows = q(db, "SELECT section_number, box_number, client_key FROM po_box ORDER BY box_number")
    assert rows == [{"section_number": 1, "box_number": n, "client_key": f"sec-A:{n}"} for n in (1, 2, 3)]
    assert q(db, "SELECT section_number, box_count FROM po_section") == [{"section_number": 1, "box_count": 3}]
    assert q(db, "SELECT count(*) AS n FROM inventory_batch") == [{"n": 3}]


def test_add_boxes_panel_still_appends_after_the_last_box(db):
    a, _ = create(db, **DRAFT, box_number=1, client_key="k1")
    create(db, **DRAFT, box_number=2, client_key="k2")
    delete(db, a.box_id)
    body = PR.AddSectionsRequest(sections=[PR.SectionInput(
        line_number=1, section_number=1, boxes=[PR.BoxInput(box_id="x-1", box_number=2)])])   # stale guess
    add_sections(db, body)
    assert [r["box_number"] for r in q(db, "SELECT box_number FROM po_box ORDER BY box_number")] == [2, 3]


def test_status_follows_what_has_arrived(db):
    status = lambda: q(db, "SELECT status FROM po_header")[0]["status"]   # noqa: E731
    a, _ = create(db, **DRAFT, box_number=1, net_weight=10, client_key="k1")
    assert status() == "partially received"                 # line 1: 10 of 20 kg
    create(db, **DRAFT, box_number=2, net_weight=10, client_key="k2")
    assert status() == "partially received"                 # line 2 (50 kg) has nothing yet
    create(db, line_number=2, section_key="sec-B", box_number=1, net_weight=50, client_key="b1")
    assert status() == "fully received"                     # every article matched
    update(db, a.box_id, net_weight=15)
    assert status() == "extra received"                     # 75 kg of 70
    delete(db, a.box_id)
    assert status() == "partially received"                 # line 1 back to 10 of 20


def test_backfill_sql_agrees_with_the_backend_rule(db):
    """132_po_receiving_status_backfill.sql gives every PO the status the
    backend's own rule (receipt_match) gives it."""
    from app.modules.purchase.services.receipt_match import LINE_RECEIPT_SQL, line_match, receiving_status

    q(db, "INSERT INTO po_header VALUES ('TR-2', 'cfpl', NULL, 'W202', 'pending', NULL, NULL), "
          "('TR-3', 'cfpl', NULL, 'W202', 'pending', NULL, NULL), ('TR-4', 'cfpl', NULL, 'W202', 'x', NULL, NULL) "
          "RETURNING 1")
    q(db, "INSERT INTO po_line (transaction_no, line_number, pack_count, po_weight) VALUES "
          "('TR-2', 1, 7000, 7000), ('TR-3', 1, 100000, 200), ('TR-3', 2, 10, NULL), ('TR-4', 1, 5, 5) RETURNING 1")
    q(db, "INSERT INTO po_box (box_id, transaction_no, line_number, section_number, box_number, net_weight, count) "
          "SELECT 'r-' || g, 'TR-2', 1, 1, g, 25, NULL FROM generate_series(1, 280) g RETURNING 1")     # 7000 kg, no counts
    q(db, "INSERT INTO po_box (box_id, transaction_no, line_number, section_number, box_number, net_weight, count) "
          "VALUES ('p-1', 'TR-3', 1, 1, 1, 199, 90000), ('p-2', 'TR-3', 2, 1, 1, 1, NULL) RETURNING 1")  # pcs short
    create(db, **DRAFT, box_number=1, net_weight=30, client_key="k1")                            # main PO: line 2 has nothing
    backfill = (DB / "132_po_receiving_status_backfill.sql").read_text(encoding="utf-8")
    backfill = backfill.replace("BEGIN;", "").replace("COMMIT;", "").strip().rstrip(";")
    q(db, backfill + " RETURNING h.transaction_no")
    got = {r["transaction_no"]: r["status"] for r in q(db, "SELECT transaction_no, status FROM po_header")}

    async def by_rule(pool):
        async with pool.acquire() as c:
            out = {}
            for txn in got:
                out[txn] = receiving_status([line_match(r) for r in await c.fetch(LINE_RECEIPT_SQL, txn)])
            return out
    want = _with_pool(by_rule)
    assert got == want
    assert got["TR-2"] == "fully received" and got["TR-3"] == "partially received"
    assert got["TR-4"] == "pending" and got[TXN] == "partially received"   # 30 of 70 kg
