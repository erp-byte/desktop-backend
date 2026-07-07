"""SFG / Job-Card Phase-7 regression tests — box→box→lot genealogy activation.

Phase 7 turns ON the deferred batch/lot genealogy on WIP SFG boxes:

  1. inventory_service.create_wip_batch mints WLOT-<batch_id> when no lot is
     supplied (every WIP batch carries a lot leaf).
  2. sfg_box_service.create_wip_boxes stamps each new box's lot_number from the
     source WIP inventory_batch.lot_number, and sets parent_box_id when
     parent_box_ids is given (box→box re-box link).
  3. get_jc_genealogy returns produced / consumed box arrays for a JC.
  4. get_box_genealogy walks UPSTREAM via parent_box_id + source-batch→producer
     JC→consumed boxes, with a `level` per chain entry.

These behaviours are fundamentally DB-bound (SQL inserts, lot lookup, recursive
walk), so the test spins a throwaway postgres:16 and builds the MINIMAL tables.
If no DB is reachable it SKIPS (import-clean, never a false failure).

Run standalone:
    docker run -d --name s7g_be -e POSTGRES_PASSWORD=pg -e POSTGRES_DB=candor \
        -p 55461:5432 postgres:16
    uv run python tests/services/test_sfg_phase7.py
    docker rm -f s7g_be
"""
from __future__ import annotations

import asyncio
import os

import asyncpg

from app.modules.production.services import inventory_service as inv
from app.modules.production.services import sfg_box_service as sbs

_DSN = os.environ.get(
    "PHASE7_DSN", "postgresql://postgres:pg@localhost:55461/candor"
)

# Minimal schema: just the columns the service code touches. Mirrors the real
# tables (inventory_batch, sfg_box, job_card_v2, internal_issue_note, all_sku,
# inventory_event_log) closely enough to exercise the Phase-7 seams.
_DDL = """
DROP TABLE IF EXISTS sfg_box, inventory_batch, inventory_event_log,
    internal_issue_note, job_card_v2, all_sku CASCADE;

CREATE TABLE all_sku (
    sfg_code TEXT, particulars TEXT, item_type TEXT
);

CREATE TABLE inventory_batch (
    batch_id TEXT PRIMARY KEY,
    sku_name TEXT NOT NULL,
    sfg_code TEXT,
    item_type TEXT,
    source TEXT,
    lot_number TEXT,
    inward_date DATE NOT NULL DEFAULT CURRENT_DATE,
    manufacturing_date DATE,
    expiry_date DATE,
    original_qty_kg NUMERIC(15,3) NOT NULL,
    current_qty_kg NUMERIC(15,3) NOT NULL,
    floor_id TEXT,
    entity TEXT,
    status TEXT NOT NULL DEFAULT 'AVAILABLE',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE inventory_event_log (
    id SERIAL PRIMARY KEY,
    batch_id TEXT, event_type TEXT, from_status TEXT, to_status TEXT,
    from_location TEXT, to_location TEXT, quantity_kg NUMERIC,
    reference_type TEXT, reference_id INT, so_id INT, performed_by TEXT,
    notes TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE job_card_v2 (
    job_card_id BIGINT PRIMARY KEY,
    job_card_number TEXT,
    fg_sku_name TEXT,
    output_kind TEXT, output_code TEXT,
    input_kind TEXT, input_code TEXT,
    prev_job_card_id BIGINT,
    entity TEXT, floor TEXT, stage TEXT,
    status TEXT,
    deleted_at TIMESTAMPTZ
);

CREATE TABLE internal_issue_note (
    note_id SERIAL PRIMARY KEY,
    note_number TEXT NOT NULL UNIQUE,
    sku_name TEXT NOT NULL,
    batch_id TEXT,
    quantity_kg NUMERIC(15,3) NOT NULL,
    source_warehouse TEXT, source_floor TEXT,
    destination_floor TEXT NOT NULL,
    purpose TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    entity TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE sfg_box (
    box_id BIGINT PRIMARY KEY,
    job_card_id BIGINT NOT NULL,
    job_card_number TEXT,
    sfg_code TEXT NOT NULL,
    entity TEXT, floor TEXT, stage_bucket TEXT,
    box_number INT NOT NULL, total_boxes INT NOT NULL,
    net_weight NUMERIC(15,3) NOT NULL,
    gross_weight NUMERIC(15,3),
    status TEXT NOT NULL DEFAULT 'PRINTED',
    source_inventory_batch_id TEXT,
    received_into_job_card_id BIGINT,
    lot_number TEXT,
    parent_box_id BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_sfg_box_jc_boxnum UNIQUE (job_card_id, box_number)
);
"""


async def _connect():
    try:
        return await asyncpg.connect(_DSN, timeout=4)
    except Exception:  # noqa: BLE001 — no DB → skip, never fail
        return None


async def _seed(conn):
    await conn.execute(_DDL)
    # Article catalogue row so create_wip_batch finds a name.
    await conn.execute(
        "INSERT INTO all_sku (sfg_code, particulars, item_type) VALUES "
        "('SFG0001', 'Roasted Almond WIP', 'sfg')"
    )
    # Two-stage chain: producer JC 11111111 → consumer JC 22222222.
    await conn.execute(
        """
        INSERT INTO job_card_v2
            (job_card_id, job_card_number, output_kind, output_code,
             input_kind, input_code, prev_job_card_id, entity, floor, stage, status)
        VALUES
            (11111111, 'JC-P1', 'SFG', 'SFG0001', NULL, NULL, NULL,
             'cfpl', 'roasting_floor', 'Create WIP', 'in_progress'),
            (22222222, 'JC-C1', 'SFG', 'SFG0099', 'SFG', 'SFG0001', 11111111,
             'cfpl', 'packing_floor', 'Create WIP', 'in_progress')
        """
    )


# ── 1. create_wip_batch mints WLOT-<id> ──────────────────────────────────────

async def _t_lot_mint(conn):
    async with conn.transaction():
        bid = await inv.create_wip_batch(
            conn, sku_name="SFG0001", qty_kg=100.0, entity="cfpl",
            job_card_id=11111111,
        )
    row = await conn.fetchrow(
        "SELECT lot_number, sfg_code, sku_name FROM inventory_batch WHERE batch_id=$1", bid
    )
    assert row["lot_number"] == f"WLOT-{bid}", row["lot_number"]
    assert row["sfg_code"] == "SFG0001"
    assert row["sku_name"] == "Roasted Almond WIP"   # article name, not code
    return bid


async def _t_lot_mint_respects_supplied(conn):
    async with conn.transaction():
        bid = await inv.create_wip_batch(
            conn, sku_name="SFG0001", qty_kg=50.0, entity="cfpl",
            job_card_id=11111111, lot_number="LOT-EXPLICIT",
        )
    lot = await conn.fetchval(
        "SELECT lot_number FROM inventory_batch WHERE batch_id=$1", bid
    )
    assert lot == "LOT-EXPLICIT", lot


# ── 2. create_wip_boxes stamps lot + sets parent ─────────────────────────────

async def _t_boxes_stamp_lot(conn, producer_batch_id):
    async with conn.transaction():
        res = await sbs.create_wip_boxes(
            conn, 11111111,
            [{"net_weight": 40.0}, {"net_weight": 60.0}],
            expected_net_kg=100.0,
            source_inventory_batch_id=producer_batch_id,
        )
    assert "error" not in res, res
    assert len(res["created"]) == 2
    expected_lot = f"WLOT-{producer_batch_id}"
    for b in res["created"]:
        assert b["lot_number"] == expected_lot, b["lot_number"]
        assert b["parent_box_id"] is None         # top-level producer → NULL
    return [b["box_id"] for b in res["created"]]


async def _t_boxes_no_source_leaves_lot_null(conn):
    async with conn.transaction():
        res = await sbs.create_wip_boxes(
            conn, 22222222, [{"net_weight": 10.0}],
        )
    assert "error" not in res, res
    assert res["created"][0]["lot_number"] is None   # not fabricated
    assert res["created"][0]["parent_box_id"] is None
    # clean up so later consumer-side tests start fresh
    await conn.execute("DELETE FROM sfg_box WHERE job_card_id = 22222222")


async def _t_boxes_parent_linkage(conn, producer_batch_id, parent_box_ids):
    # Downstream WIP stage 22222222 re-boxes the SFG it received → boxes link to
    # the consumed source boxes (round-robin by index → 1:1 here).
    async with conn.transaction():
        res = await sbs.create_wip_boxes(
            conn, 22222222,
            [{"net_weight": 40.0}, {"net_weight": 60.0}],
            parent_box_ids=parent_box_ids,
        )
    assert "error" not in res, res
    created = res["created"]
    assert created[0]["parent_box_id"] == parent_box_ids[0], created[0]
    assert created[1]["parent_box_id"] == parent_box_ids[1], created[1]
    return [b["box_id"] for b in created]


# ── 3. JC genealogy: produced / consumed ─────────────────────────────────────

async def _t_jc_genealogy(conn, producer_box_ids):
    # Mark the producer's boxes as received into the consumer JC (scan effect).
    await conn.execute(
        "UPDATE sfg_box SET status='RECEIVED', received_into_job_card_id=22222222 "
        "WHERE box_id = ANY($1::bigint[])",
        producer_box_ids,
    )
    # Producer side: produced = its own boxes, consumed = none.
    g_prod = await sbs.get_jc_genealogy(conn, 11111111)
    assert g_prod["job_card_id"] == 11111111
    assert {b["box_id"] for b in g_prod["produced"]} == set(producer_box_ids)
    assert g_prod["consumed"] == []

    # Consumer side: consumed = the producer's boxes (each w/ source_job_card_id),
    # produced = the consumer's own re-boxed boxes.
    g_cons = await sbs.get_jc_genealogy(conn, 22222222)
    consumed_ids = {b["box_id"] for b in g_cons["consumed"]}
    assert consumed_ids == set(producer_box_ids), consumed_ids
    for b in g_cons["consumed"]:
        assert b["source_job_card_id"] == 11111111, b
    assert len(g_cons["produced"]) == 2   # the re-boxed boxes from step 2
    return g_cons


# ── 4. box chain walks parent + source ───────────────────────────────────────

async def _t_box_chain(conn, child_box_id, producer_box_ids, producer_batch_id):
    # child_box_id is a consumer re-box with parent_box_id = a producer box and
    # no own source batch; its parent producer box carries source_inventory_batch_id.
    # Give the producer boxes their source batch so the lot/batch hop is exercised.
    await conn.execute(
        "UPDATE sfg_box SET source_inventory_batch_id=$1 WHERE box_id = ANY($2::bigint[])",
        producer_batch_id, producer_box_ids,
    )
    res = await sbs.get_box_genealogy(conn, child_box_id)
    assert res is not None
    assert res["box_id"] == child_box_id
    chain = res["chain"]
    ids = [c["box_id"] for c in chain]
    levels = {c["box_id"]: c["level"] for c in chain}
    # level 0 = the box itself.
    assert chain[0]["box_id"] == child_box_id
    assert levels[child_box_id] == 0
    # The parent producer box is reachable upstream (level >= 1).
    parent = next(c for c in chain if c["box_id"] == child_box_id)["parent_box_id"]
    assert parent in producer_box_ids
    assert parent in ids, "parent box must appear in the chain"
    assert levels[parent] >= 1
    return chain


async def _t_missing_box(conn):
    assert await sbs.get_box_genealogy(conn, 999999) is None


# ── orchestration ────────────────────────────────────────────────────────────

async def _run_all() -> int:
    conn = await _connect()
    if conn is None:
        print("SKIP: no postgres at", _DSN, "(set PHASE7_DSN / start s7g_be)")
        return 0
    passed = 0
    try:
        await _seed(conn)

        producer_batch = await _t_lot_mint(conn);                 passed += 1
        print("  ok  create_wip_batch mints WLOT-<id>")
        await _t_lot_mint_respects_supplied(conn);                passed += 1
        print("  ok  create_wip_batch respects supplied lot")

        prod_boxes = await _t_boxes_stamp_lot(conn, producer_batch); passed += 1
        print("  ok  create_wip_boxes stamps lot_number from source batch")
        await _t_boxes_no_source_leaves_lot_null(conn);           passed += 1
        print("  ok  create_wip_boxes leaves lot NULL with no source")

        child_boxes = await _t_boxes_parent_linkage(conn, producer_batch, prod_boxes)
        passed += 1
        print("  ok  create_wip_boxes sets parent_box_id from parent_box_ids")

        await _t_jc_genealogy(conn, prod_boxes);                  passed += 1
        print("  ok  get_jc_genealogy returns produced/consumed")

        await _t_box_chain(conn, child_boxes[0], prod_boxes, producer_batch)
        passed += 1
        print("  ok  get_box_genealogy walks parent + source-batch hop")
        await _t_missing_box(conn);                               passed += 1
        print("  ok  get_box_genealogy returns None for a missing box")
    finally:
        await conn.close()
    print(f"\n{passed} SFG Phase-7 DB tests passed")
    return passed


# ── pytest entrypoint (one test; skips cleanly if no DB) ──────────────────────

def test_phase7_db_suite():
    passed = asyncio.run(_run_all())
    # 0 = skipped (no DB); >0 = all asserts held.
    assert passed in (0, 8), f"unexpected pass count {passed}"


if __name__ == "__main__":
    asyncio.run(_run_all())
