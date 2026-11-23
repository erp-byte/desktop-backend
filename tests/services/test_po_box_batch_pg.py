"""POST /api/v1/purchase/box-batch -- box-wise entry from the desktop station --
against a real Postgres.

The station reserves box ids, prints each label with its id at once and sends
the saved boxes in the background, again whenever it had no answer. Runs only
when PO_TEST_PG_URL names a throwaway database (it creates and drops the
tables it needs, in a schema of its own); skipped otherwise.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.modules.purchase import router as PR
from tests.services import test_po_one_box_pg as ONE

PG_URL = os.environ.get("PO_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="PO_TEST_PG_URL not set")

TXN, TXN2 = "TR-20261019162053", "TR-20261019170000"
ADMIN = ONE.ADMIN
CLERK = SimpleNamespace(is_admin=False, allowed_warehouses=[], role_ids=[7], full_name="Gate Clerk",
                        email="gate@example.com", phone=None)   # may create boxes, not edit them

# The one-box tests' tables, with the receiving fields of po_header and the
# po_box columns the live database has beyond app/db.
_HEADER_END = "status TEXT, consignee_address TEXT, deleted_at TIMESTAMPTZ);"
_BOX_END = "qc_inspection_id TEXT, stage2_approval_id TEXT);"
assert _HEADER_END in ONE.SCHEMA and _BOX_END in ONE.SCHEMA
SCHEMA = ONE.SCHEMA.replace(
    _HEADER_END, _HEADER_END[:-2] + ", invoice_number TEXT, challan_number TEXT, inward_authority TEXT);",
).replace(_BOX_END, _BOX_END[:-2] + ", weighed_by TEXT, weighed_at TIMESTAMPTZ);")


def _with_pool(fn):
    """Run fn(pool) on a pool of its own: each call gets its own event loop."""
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2,
                                         server_settings={"search_path": "po_box_batch"})
        try:
            return await fn(pool)
        finally:
            await pool.close()
    return asyncio.run(go())


def q(sql, *args):
    async def go(pool):
        async with pool.acquire() as c:
            return [dict(r) for r in await c.fetch(sql, *args)]
    return _with_pool(go)


@pytest.fixture
def db(monkeypatch):
    async def setup(pool):
        async with pool.acquire() as c:
            await c.execute("DROP SCHEMA IF EXISTS po_box_batch CASCADE; CREATE SCHEMA po_box_batch")
            await c.execute(SCHEMA)
            for f in ("128_po_box_request_key.sql", "131_po_box_client_keys.sql"):
                await c.execute((ONE.DB / f).read_text(encoding="utf-8"))
            await c.execute(
                "INSERT INTO po_header (transaction_no, entity, po_date, warehouse, status, challan_number) "
                "VALUES ($1, 'cfpl', $3, 'W202', 'pending', 'CH-1'), ($2, 'cfpl', $3, 'W202', 'pending', NULL)",
                TXN, TXN2, date(2026, 10, 1))
            await c.execute("INSERT INTO po_line (transaction_no, line_number, sku_name, pack_count, po_weight) "
                            "VALUES ($1, 1, 'Cashew W320', 20, 20), ($1, 2, 'Almonds', 50, 50), "
                            "($2, 1, 'Raisins', 10, 10)", TXN, TXN2)
            await c.execute("INSERT INTO po_section (transaction_no, line_number, section_number, lot_number, "
                            "box_count, manufacturing_date, expiry_date) VALUES "
                            "($1, 1, 1, 'CF280926', 0, '2026-09-28', '2027-09-27'), "
                            "($1, 1, 2, 'CF290926', 0, '2026-09-29', '2027-09-28')", TXN)

    _with_pool(setup)
    # Tables live in the test schema; the code checks information_schema /
    # to_regclass by the 'public' schema, so mark those checks as passed.
    monkeypatch.setattr(PR, "_client_keys_ready", True)
    monkeypatch.setattr(PR, "_box_log_ready", True)
    monkeypatch.setattr(PR, "_request_keys_ready", True)
    monkeypatch.setattr(PR, "_weighed_cols_ready", True)
    monkeypatch.setattr(PR, "fetch_po_details", ONE._no_details)
    monkeypatch.setattr(PR, "POHeaderOut", lambda **kw: kw)


def batch(user=ADMIN, **body) -> PR.BoxBatchResponse:
    # Validated as the endpoint's response_model would.
    return PR.BoxBatchResponse(**_with_pool(
        lambda pool: PR.stores_box_batch(ONE._req(pool), PR.BoxBatchRequest(**body), user=user)))


def reserve(count, txn=TXN) -> PR.BoxBatchReserved:
    return batch(reserve=[{"transaction_no": txn, "count": count}]).reserved[0]


def box(box_id, txn=TXN, **kw) -> dict:
    return {"box_id": box_id, "transaction_no": txn, "line_number": 1, **kw}


def one_box(**body):
    """The web client's single-box save, POST /{txn}/box."""
    return PR.OneBoxResponse(**_with_pool(lambda pool: PR.stores_add_one_box(
        ONE._req(pool), TXN, PR.OneBoxCreate(**body), user=ADMIN))).box


def saved_box(box_id, section_number=1, box_number=1, net=None):
    q("INSERT INTO po_box (box_id, transaction_no, line_number, section_number, box_number, net_weight) "
      "VALUES ($1, $2, 1, $3, $4, $5) RETURNING 1", box_id, TXN, section_number, box_number, net)


SEC_A = {"client_key": "7e1f0a2b-sec-A", "lot_number": "CF300926",
         "manufacturing_date": "2026-09-30", "expiry_date": "2027-09-29"}


# ── reserve ──────────────────────────────────────────────────────────────


def test_reserve_continues_the_pos_base_after_its_highest_counter(db):
    saved_box("69842816-140")
    r = reserve(100)
    assert (r.base, r.from_seq, r.to_seq) == ("69842816", 141, 240)
    assert q("SELECT transaction_no, box_id, field_name, old_value, new_value, email_id, description "
             "FROM box_edit_logs") == [{
        "transaction_no": TXN, "box_id": "69842816-240", "field_name": "reserved",
        "old_value": "141", "new_value": "240", "email_id": "clerk@example.com",
        "description": "Box ids 69842816-141 to 69842816-240 reserved for box-wise entry"}]


def test_two_reservations_never_overlap(db):
    saved_box("69842816-40")
    a, b = reserve(10), reserve(5)
    assert (a.base, a.from_seq, a.to_seq) == ("69842816", 41, 50)
    assert (b.base, b.from_seq, b.to_seq) == ("69842816", 51, 55)


def test_the_web_saves_mint_ids_past_a_reserved_range(db):
    saved_box("69842816-40")
    reserve(50)                                                       # 41..90
    assert one_box(line_number=1, section_number=1, box_number=9).box_id == "69842816-91"
    body = PR.AddSectionsRequest(sections=[PR.SectionInput(
        line_number=2, boxes=[PR.BoxInput(box_id="00000000-1", box_number=1)])])
    _with_pool(lambda pool: PR.stores_add_boxes(ONE._req(pool), TXN, body, user=ADMIN))
    assert q("SELECT box_id FROM po_box WHERE line_number = 2") == [{"box_id": "69842816-92"}]


def test_a_po_with_no_box_gets_a_base_and_keeps_it(db):
    a = reserve(10)
    assert len(a.base) == 8 and a.base.isdigit() and (a.from_seq, a.to_seq) == (1, 10)
    b = reserve(10)
    assert (b.base, b.from_seq, b.to_seq) == (a.base, 11, 20)


def test_reserving_for_a_po_the_user_cant_have_fails_and_reserves_nothing(db):
    with pytest.raises(PR.HTTPException) as e:
        batch(reserve=[{"transaction_no": TXN, "count": 5}, {"transaction_no": "TR-NOPE", "count": 5}])
    assert e.value.status_code == 404
    assert q("SELECT count(*) AS n FROM box_edit_logs") == [{"n": 0}]


# ── new boxes ────────────────────────────────────────────────────────────


def test_a_new_box_is_created_with_exactly_its_reserved_id(db):
    saved_box("69842816-40")
    reserve(100)
    weighed_at = datetime(2026, 11, 23, 6, 41, 7, 512000, tzinfo=timezone.utc)
    out = batch(boxes=[box("69842816-41", section_number=2, box_number=7, net_weight=30.0, gross_weight=32.6,
                           lot_number="CF290926", client_key="dev:7", weighed_by="Asha Kulkarni",
                           weighed_at=weighed_at)])
    assert [r.model_dump(exclude_none=True) for r in out.boxes] == [
        {"box_id": "69842816-41", "status": "created", "section_number": 2, "box_number": 7}]
    assert q("SELECT section_number, box_number, net_weight, client_key, weighed_by, weighed_at "
             "FROM po_box WHERE box_id = '69842816-41'") == [{
        "section_number": 2, "box_number": 7, "net_weight": 30, "client_key": "dev:7",
        "weighed_by": "Asha Kulkarni", "weighed_at": weighed_at}]
    assert q("SELECT original_qty_kg, warehouse_id, manufacturing_date, lot_number FROM inventory_batch "
             "WHERE batch_id = '69842816-41'") == [{
        "original_qty_kg": 30, "warehouse_id": "W202", "manufacturing_date": date(2026, 9, 29),
        "lot_number": "CF290926"}]
    assert q("SELECT section_number, box_count FROM po_section WHERE line_number = 1 ORDER BY 1") == [
        {"section_number": 1, "box_count": 0}, {"section_number": 2, "box_count": 1}]
    assert q("SELECT status FROM po_header WHERE transaction_no = $1", TXN) == [{"status": "partially received"}]


def test_a_box_naming_its_section_by_key_makes_it_and_the_next_joins_it(db):
    r = reserve(10)
    ids = [f"{r.base}-{n}" for n in (1, 2)]
    out = batch(boxes=[box(ids[0], line_number=2, section=SEC_A, box_number=1, client_key="sec-A:1"),
                       box(ids[1], line_number=2, section=SEC_A, box_number=2, client_key="sec-A:2")])
    assert [(b.status, b.section_number, b.box_number) for b in out.boxes] == [("created", 1, 1), ("created", 1, 2)]
    assert q("SELECT section_number, lot_number, box_count, manufacturing_date, client_key FROM po_section "
             "WHERE line_number = 2") == [{"section_number": 1, "lot_number": "CF300926", "box_count": 2,
                                           "manufacturing_date": "2026-09-30", "client_key": SEC_A["client_key"]}]


def test_an_id_not_reserved_for_this_po_is_rejected(db):
    saved_box("69842816-40")
    r = reserve(10)                                                   # 41..50 for TXN
    other = reserve(5, txn=TXN2)                                      # for the other PO
    out = batch(boxes=[
        box(f"{r.base}-51", section_number=1),                       # past the range
        box(f"{other.base}-{other.from_seq}", section_number=1),     # reserved, but for TXN2
        box("abc", section_number=1),
        box("%-45", section_number=1),                               # not a pattern
    ])
    assert {b.status for b in out.boxes} == {"rejected"}
    assert {b.message for b in out.boxes} == {"This box id was not reserved for this PO."}
    assert q("SELECT count(*) AS n FROM po_box") == [{"n": 1}]


def test_the_same_new_box_sent_twice_is_saved_once(db):
    r = reserve(10)
    sent = box(f"{r.base}-1", section_number=1, box_number=1, net_weight=9.5, client_key="dev:1")
    first, again = batch(boxes=[sent]).boxes[0], batch(boxes=[sent]).boxes[0]
    assert (first.status, again.status) == ("created", "updated")
    assert (again.section_number, again.box_number) == (1, 1)
    assert q("SELECT count(*) AS n FROM po_box") == [{"n": 1}]
    assert q("SELECT count(*) AS n FROM inventory_batch") == [{"n": 1}]
    assert q("SELECT box_count FROM po_section WHERE section_number = 1") == [{"box_count": 1}]
    assert q("SELECT count(*) AS n FROM box_edit_logs WHERE field_name <> 'reserved'") == [{"n": 0}]


def test_a_taken_box_number_moves_to_the_next_free_one(db):
    r = reserve(10)
    out = batch(boxes=[box(f"{r.base}-{n}", section_number=1, box_number=7) for n in (1, 2)])
    assert [b.box_number for b in out.boxes] == [7, 8]
    assert q("SELECT box_number FROM po_box ORDER BY box_number") == [{"box_number": 7}, {"box_number": 8}]


def test_a_full_save_after_the_batch_adds_only_the_boxes_not_saved(db):
    r = reserve(10)
    batch(boxes=[box(f"{r.base}-1", line_number=2, section=SEC_A, box_number=2, net_weight=9.5,
                     client_key="sec-A:2")])
    body = PR.AddSectionsRequest(sections=[PR.SectionInput(
        line_number=2, box_count=3, lot_number="CF300926", client_key=SEC_A["client_key"],
        boxes=[PR.BoxInput(box_id=f"00000000-{n}", box_number=n, net_weight=9.5, client_key=f"sec-A:{n}")
               for n in (1, 2, 3)],
    )])
    _with_pool(lambda pool: PR.stores_add_boxes(ONE._req(pool), TXN, body, user=ADMIN))
    rows = q("SELECT box_id, section_number, box_number, client_key FROM po_box ORDER BY box_number")
    assert [(x["section_number"], x["box_number"], x["client_key"]) for x in rows] == \
        [(1, n, f"sec-A:{n}") for n in (1, 2, 3)]
    assert rows[1]["box_id"] == f"{r.base}-1"                         # the batch's box, untouched
    assert {rows[0]["box_id"], rows[2]["box_id"]} == {f"{r.base}-11", f"{r.base}-12"}   # past the range
    assert q("SELECT box_count FROM po_section WHERE line_number = 2") == [{"box_count": 3}]


# ── saved boxes ──────────────────────────────────────────────────────────


def test_a_saved_box_is_updated_like_put_box(db):
    r = reserve(10)
    a, b = f"{r.base}-1", f"{r.base}-2"
    batch(boxes=[box(a, section_number=1, box_number=1, net_weight=9.5, gross_weight=10, lot_number="L1"),
                 box(b, section_number=1, box_number=2, net_weight=9.5, gross_weight=10, lot_number="L1")])
    before_b = q("SELECT * FROM po_box WHERE box_id = $1", b)
    out = batch(boxes=[box(a, section_number=1, box_number=1, net_weight=11.5, gross_weight=12, count=4)])
    assert [(x.status, x.section_number, x.box_number) for x in out.boxes] == [("updated", 1, 1)]
    assert q("SELECT net_weight, gross_weight, count, lot_number FROM po_box WHERE box_id = $1", a) == [
        {"net_weight": 11.5, "gross_weight": 12, "count": 4, "lot_number": "L1"}]
    assert q("SELECT * FROM po_box WHERE box_id = $1", b) == before_b
    assert q("SELECT original_qty_kg, current_qty_kg FROM inventory_batch WHERE batch_id = $1", a) == [
        {"original_qty_kg": 11.5, "current_qty_kg": 11.5}]
    logged = q("SELECT field_name, new_value FROM box_edit_logs WHERE box_id = $1 ORDER BY id", a)
    assert [(x["field_name"], x["new_value"]) for x in logged] == [
        ("net_weight", "11.5"), ("gross_weight", "12"), ("count", "4")]


def test_a_refused_update_is_rejected_and_the_other_boxes_are_saved(db):
    r = reserve(10)
    a = f"{r.base}-1"
    batch(boxes=[box(a, section_number=1, box_number=1, net_weight=10)])
    q("UPDATE inventory_batch SET current_qty_kg = 2 WHERE batch_id = $1 RETURNING 1", a)   # 8 kg issued
    out = batch(boxes=[box(f"{r.base}-2", section_number=1, box_number=2, net_weight=5),
                       box(a, section_number=1, box_number=1, net_weight=5),
                       box(f"{r.base}-3", section_number=1, box_number=3, net_weight=5)])
    assert [x.status for x in out.boxes] == ["created", "rejected", "created"]
    assert out.boxes[1].message.startswith("Can't change the weight of line 1 box 1")
    assert q("SELECT net_weight FROM po_box WHERE box_id = $1", a) == [{"net_weight": 10}]
    assert q("SELECT count(*) AS n FROM po_box") == [{"n": 3}]


def test_a_box_id_of_another_po_is_rejected(db):
    q("INSERT INTO po_box (box_id, transaction_no, line_number, section_number, box_number) "
      "VALUES ('55555555-1', $1, 1, 1, 1) RETURNING 1", TXN2)
    out = batch(boxes=[box("55555555-1", section_number=1, net_weight=4)])
    assert (out.boxes[0].status, out.boxes[0].message) == ("rejected", "This box id belongs to another PO.")
    assert q("SELECT net_weight FROM po_box") == [{"net_weight": None}]


def test_a_user_who_may_only_create_can_resend_but_not_change(db, monkeypatch):
    async def no_edit(*a, **k):
        return False
    monkeypatch.setattr(PR, "check_permission", no_edit)
    r = reserve(10)
    sent = box(f"{r.base}-1", section_number=1, box_number=1, net_weight=9.5)
    out = batch(user=CLERK, headers=[{"transaction_no": TXN, "warehouse": "Savla D-39"}], boxes=[sent])
    assert out.boxes[0].status == "created"
    assert q("SELECT warehouse FROM po_header WHERE transaction_no = $1", TXN) == [{"warehouse": "W202"}]
    assert batch(user=CLERK, boxes=[sent]).boxes[0].status == "updated"            # a resend
    changed = batch(user=CLERK, boxes=[{**sent, "net_weight": 8.0}]).boxes[0]
    assert (changed.status, changed.message) == ("rejected", "You don't have permission to change saved boxes.")
    assert q("SELECT net_weight FROM po_box") == [{"net_weight": 9.5}]


# ── headers ──────────────────────────────────────────────────────────────


def test_headers_set_only_the_fields_sent_before_the_boxes(db):
    r = reserve(10)
    batch(headers=[{"transaction_no": TXN, "warehouse": "Savla D-39", "invoice_number": "INV/77"}],
          boxes=[box(f"{r.base}-1", section_number=1, box_number=1, net_weight=9.5)])
    assert q("SELECT warehouse, invoice_number, challan_number, inward_authority FROM po_header "
             "WHERE transaction_no = $1", TXN) == [{"warehouse": "Savla D-39", "invoice_number": "INV/77",
                                                    "challan_number": "CH-1", "inward_authority": None}]
    assert q("SELECT warehouse_id FROM inventory_batch") == [{"warehouse_id": "Savla D-39"}]


def test_a_header_for_a_missing_po_is_skipped_and_its_boxes_rejected(db):
    out = batch(headers=[{"transaction_no": "TR-NOPE", "warehouse": "X"}],
                boxes=[box("12345678-1", txn="TR-NOPE", section_number=1)])
    assert (out.boxes[0].status, out.boxes[0].message) == ("rejected", "Transaction not found.")


# ── the request as a whole ───────────────────────────────────────────────


def test_an_empty_request_answers_empty_lists(db):
    assert _with_pool(lambda pool: PR.stores_box_batch(
        ONE._req(pool), PR.BoxBatchRequest(), user=ADMIN)) == {"reserved": [], "boxes": []}


def test_sending_a_request_twice_leaves_the_database_as_once(db):
    r = reserve(10)
    request = dict(
        headers=[{"transaction_no": TXN, "warehouse": "Savla D-39", "inward_authority": "Asha Kulkarni"}],
        boxes=[box(f"{r.base}-1", section_number=2, box_number=1, net_weight=30.0, gross_weight=32.6,
                   client_key="dev:1", weighed_by="Asha Kulkarni"),
               box(f"{r.base}-2", line_number=2, section=SEC_A, box_number=1, net_weight=24.9, gross_weight=25.5,
                   client_key="sec-A:1")])

    def snapshot():
        return {t: q(f"SELECT * FROM {t} ORDER BY 1, 2") for t in
                ("po_header", "po_section", "po_box", "inventory_batch", "box_edit_logs", "inventory_event_log")}

    batch(**request)
    once = snapshot()
    assert [b.status for b in batch(**request).boxes] == ["updated", "updated"]
    assert snapshot() == once


def test_without_the_weighed_columns_the_box_is_still_saved(db, monkeypatch):
    monkeypatch.setattr(PR, "_weighed_cols_ready", False)    # the check now finds no such columns
    r = reserve(10)
    out = batch(boxes=[box(f"{r.base}-1", section_number=1, weighed_by="Asha Kulkarni")])
    assert out.boxes[0].status == "created"
    assert q("SELECT weighed_by FROM po_box") == [{"weighed_by": None}]
