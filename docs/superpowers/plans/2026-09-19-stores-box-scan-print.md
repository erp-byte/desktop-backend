# Stores Box Scan + Manual Print Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store the boxes Stores scans or prints for an issued floor requisition — printed boxes in `sfg_box` (`item_type 'rm'`), both kinds in a new `floor_requisition_box` table — and wire the Scan material dialog to it.

**Architecture:** A new `box_service.py` in the floor_requisition module resolves scanned labels through the job card scanner's tables (sfg_box → po_box+po_line → `identify_box`), mints printed boxes into `sfg_box`, and records every box in `floor_requisition_box`. Four routes on the existing `/api/v1/floor-requisitions` router expose it. The web dialog loads, scans, prints and removes through them.

**Tech Stack:** FastAPI + asyncpg (server_replica, pytest with fake connections), Next.js 16 / React 19 (web_replica, Node-run `*.test.ts`).

**Spec:** `server_replica/docs/superpowers/specs/2026-09-19-stores-box-scan-print-design.md`

## Global Constraints

- Do NOT commit; the user commits. Do NOT apply migrations to any database (the user applies 113 on Supabase and RDS).
- Line endings: keep each file's existing ones. CRLF: `app/db/111_*.sql` style (new 113 is CRLF), `scripts/migrate.py`, `app/modules/production/services/sfg_box_service.py`. LF: floor_requisition module files, server tests, all web files touched.
- Permissions: read = `production.floor_requisitions.view`, writes = `production.floor_requisitions.issue`; place scope via `requisition_service._load` (place_scope 403).
- Writes only while the request status is `issued` (409 `not_issued`).
- Errors: `RequisitionError(http_status, error, message, **details)` → router `{error, message, details}`.
- sfg_box printed box: `item_type 'rm'`, `stage_bucket 'Stores'`, `status 'PRINTED'`, `sfg_code = fg_sku_name = article`, `batch_code = LOT`, id `<new_short_time_id()>-<per-job-card counter>`.
- Sticker: Material-In `printLabels` with `transaction_no = String(requisition_id)`, `box_id = box_code`.
- Print limits: 1–500 boxes per call; net > 0; gross ≥ net when given; count whole ≥ 0; box_number ≥ 1, unique per request; LOT ≤ 100 chars; article ≤ 500 chars.
- Scan endpoint runs WITHOUT a transaction; print and remove run inside one.

---

### Task 1: Migration 113

**Files:**
- Create: `server_replica/app/db/113_floor_requisition_box.sql` (CRLF)
- Modify: `server_replica/scripts/migrate.py` (after the 112 entry, CRLF)
- Test: `server_replica/tests/services/test_floor_requisition_box_migration.py`

**Interfaces:** Produces table `floor_requisition_box` (columns listed below) and `sfg_box.chk_box_item_type` allowing `'rm'`.

- [ ] **Step 1: Write the failing test**

```python
"""Migration 113 — floor_requisition_box and sfg_box item_type 'rm'. A static check
of the file and its registration; the SQL is applied by the user, not by tests."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQL_PATH = ROOT / "app" / "db" / "113_floor_requisition_box.sql"


def _sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def test_registered_after_112_in_the_runner():
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i112 = text.index('"112_floor_requisition_store_response.sql"')
    i113 = text.index('"113_floor_requisition_box.sql"')
    end = text.index("# ── Optional: drop the v1 legacy")
    assert i112 < i113 < end


def test_sfg_box_accepts_rm_and_keeps_sfg_and_fg():
    sql = _sql()
    assert "ALTER TABLE sfg_box DROP CONSTRAINT IF EXISTS chk_box_item_type;" in sql
    assert "CHECK (item_type IN ('sfg','fg','rm'))" in sql


def test_one_row_per_box_per_request():
    sql = _sql()
    assert "PRIMARY KEY (requisition_id, box_code)" in sql
    assert "REFERENCES floor_requisition (requisition_id)" in sql


def test_box_numbers_unique_per_request_and_required_when_printed():
    sql = _sql()
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_box_number\s+"
        r"ON floor_requisition_box \(requisition_id, box_number\) WHERE box_number IS NOT NULL",
        sql,
    )
    assert "CHECK (source IN ('printed','scanned'))" in sql
    assert "CHECK (source <> 'printed' OR box_number IS NOT NULL)" in sql


def test_weights_and_count_columns():
    sql = _sql()
    for col in ("net_weight", "gross_weight"):
        assert re.search(rf"\b{col}\s+NUMERIC\(15,3\)", sql), col
    assert re.search(r"\bcount\s+INT\b", sql)


def test_is_idempotent():
    sql = _sql()
    assert "CREATE TABLE IF NOT EXISTS floor_requisition_box" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_floor_requisition_box_code" in sql
```

- [ ] **Step 2: Run it — expect FAIL** (`FileNotFoundError` for the SQL file)

Run: `python -m pytest tests/services/test_floor_requisition_box_migration.py -q`

- [ ] **Step 3: Write the migration (CRLF)**

```sql
-- ===========================================================================
-- 113_floor_requisition_box.sql
-- Stores -> Production Indents -> Scan: the boxes store sends for a floor
-- requisition (app/modules/floor_requisition/services/box_service.py).
--
--   * sfg_box.item_type gains 'rm': a box store printed a sticker for under
--     Manual print. The job card's Boxes printing list and batch caps read
--     item_type = 'sfg' only, so these never show there; the job card's Raw
--     Material scanner still finds them by carton_id.
--   * floor_requisition_box: one row per box scanned or printed for a request.
--     The floor still scans the boxes into the job card (jc_box_scan), so RM
--     issued is counted there, once.
--
-- MUST follow 067 / 073 (sfg_box) and 111 (floor_requisition). Idempotent.
-- ===========================================================================

ALTER TABLE sfg_box DROP CONSTRAINT IF EXISTS chk_box_item_type;
ALTER TABLE sfg_box ADD  CONSTRAINT chk_box_item_type CHECK (item_type IN ('sfg','fg','rm'));

CREATE TABLE IF NOT EXISTS floor_requisition_box (
    requisition_id  BIGINT        NOT NULL REFERENCES floor_requisition (requisition_id),
    box_code        TEXT          NOT NULL,                -- carton_id / box_id
    source          TEXT          NOT NULL,                -- 'printed' | 'scanned'
    box_table       TEXT          NOT NULL,                -- where the box was found
    box_number      INT,                                   -- printed only: "Box #" on the sticker
    transaction_no  TEXT,
    article         TEXT          NOT NULL,
    stock_type      TEXT,                                  -- printed only: Fresh Stock | Off Grade/Rejection
    lot_number      TEXT,
    net_weight      NUMERIC(15,3),
    gross_weight    NUMERIC(15,3),
    count           INT,
    recorded_by     TEXT          NOT NULL,
    recorded_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (requisition_id, box_code),
    CONSTRAINT chk_frb_source     CHECK (source IN ('printed','scanned')),
    CONSTRAINT chk_frb_box_number CHECK (box_number IS NULL OR box_number >= 1),
    CONSTRAINT chk_frb_printed    CHECK (source <> 'printed' OR box_number IS NOT NULL)
);

-- One sticker number per request.
CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_box_number
    ON floor_requisition_box (requisition_id, box_number) WHERE box_number IS NOT NULL;

-- "Which request was this box sent on?" from the box side.
CREATE INDEX IF NOT EXISTS idx_floor_requisition_box_code
    ON floor_requisition_box (box_code);
```

- [ ] **Step 4: Register it in `scripts/migrate.py`** — insert right after the `DB_DIR / "112_floor_requisition_store_response.sql",` line (CRLF):

```python
    # 113 lets sfg_box hold store's manually printed boxes (item_type 'rm') and
    # creates floor_requisition_box: one row per box store scanned or printed for
    # a request (Stores -> Production Indents -> Scan). MUST follow 067/073 and 111.
    # Idempotent.
    DB_DIR / "113_floor_requisition_box.sql",
```

- [ ] **Step 5: Run — expect PASS**, then check line endings (`113` and `migrate.py` all CRLF).

---

### Task 2: box_service — label parsing, lookup, list, scan

**Files:**
- Create: `server_replica/app/modules/floor_requisition/services/box_service.py` (LF)
- Test: `server_replica/tests/services/test_floor_requisition_box_service.py` (LF)

**Interfaces:**
- Consumes: `requisition_service.RequisitionError`, `requisition_service._load(conn, user, requisition_id)` (404 + place 403), `rules.actor_name(user)`, `box_identify_service.identify_box(conn, value) -> dict` (`found`, `ambiguous`, `table`, `also_in`, `box{transaction_no,item_description,lot_number,net_weight,gross_weight,count}`).
- Produces: `parse_label(raw) -> (code, tx|None)`, `resolve_box(conn, raw) -> dict`, `list_boxes(conn, user, requisition_id) -> {requisition_id, status, boxes, totals}`, `scan_box(conn, user, requisition_id, *, code) -> box dict`, `box_out(row, requested_article) -> dict` (adds `article_mismatch`), `BOX_COLS`.

- [ ] **Step 1: Write the failing tests** — a scripted fake connection answering by SQL prefix; `identify_box` monkeypatched in the module.

```python
"""box_service against a scripted fake connection: which table a scan resolves
from, what is refused, what is written. The SQL runs for real only on a DB."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.modules.floor_requisition.services import box_service as bs
from app.modules.floor_requisition.services import requisition_service as svc

REQ_COLS = [c.strip() for c in svc.COLS.split(",")]
AT = datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc)
SEEDS = "Sunflower Seeds Roasted"


class User:
    def __init__(self, warehouses=(), floors=()):
        self.full_name, self.email, self.phone, self.user_id = "Store Sam", "", "", 9
        self.allowed_warehouses, self.allowed_floors = list(warehouses), list(floors)


def _req(**over):
    base = {c: None for c in REQ_COLS}
    base.update(requisition_id=27385955, job_card_id=75009889, warehouse="A185", floor="Mezzanine",
                material_sku_name=SEEDS, status="issued")
    base.update(over)
    return base


def _box_row(**over):
    row = {"requisition_id": 27385955, "box_code": "BX-1", "source": "scanned", "box_table": "po_box",
           "box_number": None, "transaction_no": "TR-1", "article": SEEDS, "stock_type": None,
           "lot_number": None, "net_weight": Decimal("10.500"), "gross_weight": Decimal("11.000"),
           "count": 4, "recorded_by": "Store Sam", "recorded_at": AT}
    row.update(over)
    return row


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, *, req=None, dup=False, sfg=None, po=(), inserted="auto", status_after="issued",
                 rows=(), locked_status=None, source=None, jc=None, last=0, in_jc=False,
                 jc_table=True, sfg_delete="DELETE 1", sfg_still_there=False, taken=()):
        self.req = req if req is not None else _req()
        self.dup, self.sfg, self.po = dup, sfg, list(po)
        self.inserted, self.status_after, self.rows = inserted, status_after, list(rows)
        self.locked_status = locked_status
        self.source, self.jc, self.last = source, jc, last
        self.in_jc, self.jc_table = in_jc, jc_table
        self.sfg_delete, self.sfg_still_there, self.taken = sfg_delete, sfg_still_there, list(taken)
        self.calls: list[tuple[str, str, tuple]] = []
        self.sfg_inserts: list[tuple] = []
        self.box_inserts: list[tuple] = []

    def is_in_transaction(self):
        return True

    def transaction(self):
        return _Tx()

    def _log(self, kind, sql, args):
        s = " ".join(sql.split())
        self.calls.append((kind, s, args))
        return s

    async def fetchrow(self, sql, *args):
        s = self._log("fetchrow", sql, args)
        if s.startswith("SELECT requisition_id, job_card_id"):
            return self.req
        if s.startswith("SELECT carton_id, fg_sku_name"):
            return self.sfg
        if s.startswith("INSERT INTO floor_requisition_box") and "'scanned'" in s:
            if self.inserted == "auto":
                return _box_row(box_code=args[1], box_table=args[2], transaction_no=args[3],
                                article=args[4], lot_number=args[5], net_weight=args[6],
                                gross_weight=args[7], count=args[8], recorded_by=args[9])
            return self.inserted
        if s.startswith("INSERT INTO floor_requisition_box") and "'printed'" in s:
            self.box_inserts.append(args)
            return _box_row(box_code=args[1], source="printed", box_table="sfg_box", box_number=args[2],
                            transaction_no=args[3], article=args[4], stock_type=args[5], lot_number=args[6],
                            net_weight=args[7], gross_weight=args[8], count=args[9], recorded_by=args[10])
        if s.startswith("SELECT job_card_number, entity FROM job_card_v2"):
            return self.jc
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        if s.startswith("SELECT b.box_id, b.transaction_no"):
            return self.po
        if s.startswith("SELECT requisition_id, box_code"):
            return self.rows
        if s.startswith("SELECT box_number FROM floor_requisition_box"):
            return [{"box_number": n} for n in self.taken]
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        if s.startswith("SELECT 1 FROM floor_requisition_box"):
            return 1 if self.dup else None
        if s.startswith("SELECT status FROM floor_requisition WHERE requisition_id = $1 FOR UPDATE"):
            return self.locked_status or self.req["status"]
        if s.startswith("SELECT status FROM floor_requisition"):
            return self.status_after
        if s.startswith("SELECT COALESCE(MAX(CAST(split_part(carton_id"):
            return self.last
        if s.startswith("INSERT INTO sfg_box"):
            self.sfg_inserts.append(args)
            return args[0]
        if s.startswith("SELECT source FROM floor_requisition_box"):
            return self.source
        if s.startswith("SELECT to_regclass('jc_box_scan')"):
            return self.jc_table
        if s.startswith("SELECT 1 FROM jc_box_scan"):
            return 1 if self.in_jc else None
        if s.startswith("SELECT 1 FROM sfg_box"):
            return 1 if self.sfg_still_there else None
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    async def execute(self, sql, *args):
        s = self._log("execute", sql, args)
        if s.startswith("DELETE FROM sfg_box"):
            return self.sfg_delete
        if s.startswith("DELETE FROM floor_requisition_box"):
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {s[:90]}")

    def ran(self, prefix):
        return [c for c in self.calls if c[1].startswith(prefix)]


def _identify(result):
    async def fake(conn, value):
        fake.seen = value
        return result
    fake.seen = None
    return fake


NOT_FOUND = {"found": False, "box_id": "X"}


def run(coro):
    return asyncio.run(coro)


def _refusal(exc_info):
    return exc_info.value.status, exc_info.value.error


# ── label parsing ──
@pytest.mark.parametrize("raw, want", [
    ('{"tx":"TR-7","bi":"BX-9"}', ("BX-9", "TR-7")),
    ('  {"tx":" TR-7 ","bi":" BX-9 "} ', ("BX-9", "TR-7")),
    ('{"tx":27385955,"bi":"48213307-1"}', ("48213307-1", "27385955")),
    ('{"bi":"BX-9"}', ("BX-9", None)),
    ('{"tx":"  ","bi":"BX-9"}', ("BX-9", None)),
    ("  48213307-2  ", ("48213307-2", None)),
    ("42", ("42", None)),
    ('{"x":1}', ('{"x":1}', None)),
    ("", ("", None)),
])
def test_parse_label(raw, want):
    assert bs.parse_label(raw) == want


# ── lookup order ──
def test_a_bare_id_is_looked_up_in_sfg_box_first(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(sfg={"carton_id": "48213307-1", "fg_sku_name": SEEDS, "sfg_code": SEEDS,
                         "batch_code": "L-1", "net_weight": Decimal("9.770"),
                         "gross_weight": Decimal("10.000"), "units": 3, "status": "PRINTED"})
    box = run(bs.resolve_box(conn, "48213307-1"))
    assert box == {"box_code": "48213307-1", "box_table": "sfg_box", "transaction_no": None,
                   "article": SEEDS, "lot_number": "L-1", "net_weight": Decimal("9.770"),
                   "gross_weight": Decimal("10.000"), "count": 3}
    assert not conn.ran("SELECT b.box_id")


def test_a_label_with_tx_tries_po_box_first_and_matches_its_transaction(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(po=[{"box_id": "BX-9", "transaction_no": "TR-7", "lot_number": "L9",
                         "net_weight": Decimal("25.000"), "gross_weight": Decimal("25.400"),
                         "count": 1, "sku_name": SEEDS}])
    box = run(bs.resolve_box(conn, '{"tx":"TR-7","bi":"BX-9"}'))
    assert box["box_table"] == "po_box" and box["transaction_no"] == "TR-7" and box["article"] == SEEDS
    assert conn.ran("SELECT b.box_id")[0][2] == ("BX-9", "TR-7")
    assert not conn.ran("SELECT carton_id")


def test_a_bare_id_on_two_purchase_orders_is_ambiguous(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    two = [{"box_id": "BX-9", "transaction_no": t, "lot_number": None, "net_weight": 1,
            "gross_weight": None, "count": None, "sku_name": SEEDS} for t in ("TR-1", "TR-2")]
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(FakeConn(po=two), "BX-9"))
    assert _refusal(e) == (409, "ambiguous_box")


def test_the_wider_identify_is_the_last_step(monkeypatch):
    fake = _identify({"found": True, "table": "cfpl_cold_stocks", "box": {
        "box_id": "CS-1", "transaction_no": "CT-3", "item_description": "Almonds",
        "lot_number": "LX", "net_weight": 20.0, "gross_weight": None, "count": None}})
    monkeypatch.setattr(bs, "identify_box", fake)
    box = run(bs.resolve_box(FakeConn(), "CS-1"))
    assert fake.seen == "CS-1"
    assert box == {"box_code": "CS-1", "box_table": "cfpl_cold_stocks", "transaction_no": "CT-3",
                   "article": "Almonds", "lot_number": "LX", "net_weight": 20.0,
                   "gross_weight": None, "count": None}


def test_identify_ambiguity_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(
        {"found": True, "ambiguous": True, "table": "po_box", "also_in": ["cfpl_boxes_v2"], "box": {}}))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(FakeConn(), "BX-1"))
    assert _refusal(e) == (409, "ambiguous_box")
    assert e.value.details["tables"] == ["po_box", "cfpl_boxes_v2"]


def test_an_unknown_box_points_to_manual_print(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(FakeConn(), "NOPE-1"))
    assert _refusal(e) == (404, "box_not_found")
    assert "Manual print" in e.value.message


def test_a_cancelled_sfg_box_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(sfg={"carton_id": "S-1", "fg_sku_name": None, "sfg_code": "SFG0001", "batch_code": None,
                         "net_weight": 1, "gross_weight": None, "units": None, "status": "CANCELLED"})
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(conn, "S-1"))
    assert _refusal(e) == (409, "box_cancelled")


def test_a_qr_without_a_box_id_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(FakeConn(), '{"tx":"TR-1","bi":"  "}'))
    assert _refusal(e) == (400, "no_box_id")


# ── scan ──
def _po_hit():
    return [{"box_id": "BX-1", "transaction_no": "TR-1", "lot_number": None, "net_weight": Decimal("10.500"),
             "gross_weight": Decimal("11.000"), "count": 4, "sku_name": SEEDS}]


def test_scan_records_the_box_with_what_the_lookup_found(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(po=_po_hit())
    out = run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    args = conn.ran("INSERT INTO floor_requisition_box")[0][2]
    assert args == (27385955, "BX-1", "po_box", "TR-1", SEEDS, None, Decimal("10.500"),
                    Decimal("11.000"), 4, "Store Sam")
    assert out["box_code"] == "BX-1" and out["net_weight"] == 10.5 and out["article_mismatch"] is False
    assert out["recorded_at"] == AT.isoformat()


def test_scan_flags_a_different_article(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    hit = _po_hit()
    hit[0]["sku_name"] = "Pumpkin Seeds"
    out = run(bs.scan_box(FakeConn(po=hit), User(), 27385955, code="BX-1"))
    assert out["article_mismatch"] is True


def test_article_match_ignores_case_and_spacing():
    row = _box_row(article="  sunflower   SEEDS roasted ")
    assert bs.box_out(row, SEEDS)["article_mismatch"] is False


def test_scan_refuses_a_box_already_on_the_request_before_looking_it_up(monkeypatch):
    fake = _identify(NOT_FOUND)
    monkeypatch.setattr(bs, "identify_box", fake)
    conn = FakeConn(dup=True)
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(conn, User(), 27385955, code='{"tx":"TR-1","bi":"BX-1"}'))
    assert _refusal(e) == (409, "duplicate_box")
    assert conn.ran("SELECT 1 FROM floor_requisition_box")[0][2] == (27385955, "BX-1")
    assert fake.seen is None and not conn.ran("SELECT b.box_id")


def test_scan_only_while_issued(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(FakeConn(req=_req(status="received")), User(), 27385955, code="BX-1"))
    assert _refusal(e) == (409, "not_issued")


def test_scan_outside_the_callers_place_is_403(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(HTTPException) as e:
        run(bs.scan_box(FakeConn(), User(warehouses=["W202"]), 27385955, code="BX-1"))
    assert e.value.status_code == 403


def test_a_lost_insert_race_reports_the_duplicate(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(FakeConn(po=_po_hit(), inserted=None), User(), 27385955, code="BX-1"))
    assert _refusal(e) == (409, "duplicate_box")


def test_a_request_moved_on_mid_scan_reports_its_status(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(po=_po_hit(), inserted=None, status_after="received")
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    assert _refusal(e) == (409, "not_issued")


def test_a_box_without_an_article_is_recorded_as_unknown(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    hit = _po_hit()
    hit[0]["sku_name"] = None
    conn = FakeConn(po=hit)
    run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    assert conn.ran("INSERT INTO floor_requisition_box")[0][2][4] == bs.UNKNOWN_ARTICLE


def test_the_insert_is_guarded_on_issued_and_on_conflict():
    sql = " ".join(bs._INSERT_SCANNED_SQL.split())
    assert "WHERE EXISTS (SELECT 1 FROM floor_requisition WHERE requisition_id = $1 AND status = 'issued')" in sql
    assert "ON CONFLICT (requisition_id, box_code) DO NOTHING" in sql


# ── list ──
def test_list_returns_rows_and_totals_for_any_status():
    rows = [_box_row(box_code="A", net_weight=Decimal("1.250"), gross_weight=None, count=None),
            _box_row(box_code="B", net_weight=Decimal("2.000"), gross_weight=Decimal("2.5"), count=3)]
    out = run(bs.list_boxes(FakeConn(req=_req(status="received"), rows=rows), User(), 27385955))
    assert [b["box_code"] for b in out["boxes"]] == ["A", "B"]
    assert out["status"] == "received"
    assert out["totals"] == {"boxes": 2, "net_weight": 3.25, "gross_weight": 2.5, "count": 3}
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError: cannot import name 'box_service'`)

Run: `python -m pytest tests/services/test_floor_requisition_box_service.py -q`

- [ ] **Step 3: Write `box_service.py`** (scan/list half; Task 3 adds print/remove to the same file)

```python
"""Boxes store sends for a floor requisition — Stores → Production Indents → Scan.

A box joins a request (floor_requisition_box, app/db/113) one of two ways:

  * scanned — it already carries a sticker. It is looked up in the same tables as
    the job card's Raw Material scanner: sfg_box by carton_id, po_box (with
    po_line for the article) by box_id — and by transaction_no when the label
    carries one — then the universal identify over the legacy warehouse / cold
    tables (production/services/box_identify_service.py). A label's tx decides
    the order: a PO label ({"tx","bi"}) is tried against po_box first.
  * printed — it has no sticker. Manual print mints it in sfg_box as item_type
    'rm' (id "<8-digit time>-<per-job-card counter>", as create_wip_boxes does)
    and the browser prints Material-In's sticker, QR {"tx": request no, "bi": id}.

The floor still scans these boxes into the job card (jc_box_scan), so RM issued
is counted there, once; jc_box_scan is only read here, to refuse removing a
printed box the floor has already used.

Boxes change only while the request is 'issued'. Refusals are RequisitionError;
a place outside the caller's grants is place_scope's 403 (via _load).

Scanning must NOT run inside a transaction: the identify step can fail a
statement on schema drift, which would poison it (box_scan_service says the
same). Its one write is a single guarded INSERT. Print and remove must run
inside the caller's transaction.
"""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.floor_requisition import rules
from app.modules.floor_requisition.services.requisition_service import RequisitionError, _load
from app.modules.production.services.box_identify_service import identify_box

MAX_PRINT = 500
STOCK_TYPES = ("Fresh Stock", "Off Grade/Rejection")
UNKNOWN_ARTICLE = "Unknown article"
_TEXT_LIMIT = 500
_LOT_LIMIT = 100

BOX_COLS = """requisition_id, box_code, source, box_table, box_number, transaction_no, article,
              stock_type, lot_number, net_weight, gross_weight, count, recorded_by, recorded_at"""

_LIST_SQL = f"""
    SELECT {BOX_COLS} FROM floor_requisition_box
     WHERE requisition_id = $1
     ORDER BY recorded_at DESC, box_number DESC NULLS LAST, box_code
"""

_SFG_SQL = """
    SELECT carton_id, fg_sku_name, sfg_code, batch_code, net_weight, gross_weight, units, status
      FROM sfg_box WHERE carton_id = $1
"""

# LIMIT 2: box_id is in no unique key, so without the label's transaction the same
# id on two purchase orders is ambiguous, not "the first one".
_PO_SQL = """
    SELECT b.box_id, b.transaction_no, b.lot_number, b.net_weight, b.gross_weight, b.count,
           l.sku_name
      FROM po_box b
      LEFT JOIN po_line l ON l.transaction_no = b.transaction_no AND l.line_number = b.line_number
     WHERE b.box_id = $1 AND ($2::text IS NULL OR b.transaction_no = $2)
     ORDER BY b.transaction_no
     LIMIT 2
"""

# Guarded twice: only while the request is still issued, and a box already on it
# (a concurrent scan) inserts nothing rather than failing.
_INSERT_SCANNED_SQL = f"""
    INSERT INTO floor_requisition_box
           (requisition_id, box_code, source, box_table, transaction_no, article, lot_number,
            net_weight, gross_weight, count, recorded_by)
    SELECT $1, $2, 'scanned', $3, $4, $5, $6, $7, $8, $9, $10
     WHERE EXISTS (SELECT 1 FROM floor_requisition WHERE requisition_id = $1 AND status = 'issued')
    ON CONFLICT (requisition_id, box_code) DO NOTHING
    RETURNING {BOX_COLS}
"""


def _key(name: Optional[str]) -> str:
    return " ".join((name or "").split()).upper()


def _float(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def _int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def box_out(row, requested_article: str) -> dict[str, Any]:
    out = dict(row)
    out["net_weight"] = _float(out["net_weight"])
    out["gross_weight"] = _float(out["gross_weight"])
    out["recorded_at"] = out["recorded_at"].isoformat() if out.get("recorded_at") else None
    out["article_mismatch"] = _key(out["article"]) != _key(requested_article)
    return out


def totals(boxes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "boxes": len(boxes),
        "net_weight": round(sum(b["net_weight"] or 0 for b in boxes), 3),
        "gross_weight": round(sum(b["gross_weight"] or 0 for b in boxes), 3),
        "count": sum(b["count"] or 0 for b in boxes),
    }


def parse_label(raw: Optional[str]) -> tuple[str, Optional[str]]:
    """(box id, transaction no) from a scanned QR: Material-In's JSON {"tx","bi"},
    or a bare box id. An empty box id means the QR carries none."""
    text = (raw or "").strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text, None
    if isinstance(parsed, dict) and isinstance(parsed.get("bi"), str):
        tx = parsed.get("tx")
        tx_text = str(tx).strip() if tx is not None else ""
        return parsed["bi"].strip(), tx_text or None
    return text, None


def _not_issued(requisition_id: int, status: Optional[str]) -> RequisitionError:
    return RequisitionError(409, "not_issued",
                            f"Request {requisition_id} is {status}. Boxes can be changed only while it is issued.",
                            requisition_id=requisition_id, status=status)


def _duplicate(requisition_id: int, code: str) -> RequisitionError:
    return RequisitionError(409, "duplicate_box", f"Box {code} is already on request {requisition_id}.",
                            requisition_id=requisition_id, box_code=code)


def _ambiguous(code: str, tables: list) -> RequisitionError:
    return RequisitionError(409, "ambiguous_box",
                            f"Box {code} matches more than one box. Scan the sticker's QR that "
                            "carries its transaction number.",
                            box_code=code, tables=tables)


async def _load_issued(conn, user, requisition_id: int, *, lock: bool = False):
    """The request, refused unless it is issued. `lock` holds the row for the
    caller's transaction so it cannot move on mid-write."""
    row = await _load(conn, user, requisition_id)
    status = row["status"]
    if lock:
        status = await conn.fetchval(
            "SELECT status FROM floor_requisition WHERE requisition_id = $1 FOR UPDATE", requisition_id)
    if status != "issued":
        raise _not_issued(requisition_id, status)
    return row


async def _from_sfg(conn, code: str, tx: Optional[str]) -> Optional[dict[str, Any]]:
    row = await conn.fetchrow(_SFG_SQL, code)
    if row is None:
        return None
    if row["status"] == "CANCELLED":
        raise RequisitionError(409, "box_cancelled", f"Box {code} was cancelled, so it can't be sent.",
                               box_code=code)
    return {"box_code": code, "box_table": "sfg_box", "transaction_no": None,
            "article": row["fg_sku_name"] or row["sfg_code"], "lot_number": row["batch_code"],
            "net_weight": row["net_weight"], "gross_weight": row["gross_weight"], "count": row["units"]}


async def _from_po(conn, code: str, tx: Optional[str]) -> Optional[dict[str, Any]]:
    rows = await conn.fetch(_PO_SQL, code, tx)
    if not rows:
        return None
    if len(rows) > 1:
        raise _ambiguous(code, ["po_box"])
    r = rows[0]
    return {"box_code": code, "box_table": "po_box", "transaction_no": r["transaction_no"],
            "article": r["sku_name"], "lot_number": r["lot_number"],
            "net_weight": r["net_weight"], "gross_weight": r["gross_weight"], "count": r["count"]}


async def resolve_box(conn, raw: str) -> dict[str, Any]:
    """What a scanned label is: {box_code, box_table, transaction_no, article,
    lot_number, net_weight, gross_weight, count}, or a refusal."""
    code, tx = parse_label(raw)
    if not code:
        raise RequisitionError(400, "no_box_id", "That QR has no box id.")
    for find in ((_from_po, _from_sfg) if tx else (_from_sfg, _from_po)):
        box = await find(conn, code, tx)
        if box:
            return box
    ident = await identify_box(conn, raw)
    if ident.get("ambiguous"):
        raise _ambiguous(code, [ident.get("table"), *(ident.get("also_in") or [])])
    if ident.get("found"):
        b = ident.get("box") or {}
        return {"box_code": code, "box_table": ident.get("table") or "unknown",
                "transaction_no": b.get("transaction_no"), "article": b.get("item_description"),
                "lot_number": b.get("lot_number"), "net_weight": b.get("net_weight"),
                "gross_weight": b.get("gross_weight"), "count": b.get("count")}
    raise RequisitionError(404, "box_not_found",
                           f"Box {code} isn't in any box table. Print a sticker for it under Manual print.",
                           box_code=code)


async def list_boxes(conn, user, requisition_id: int) -> dict[str, Any]:
    req = await _load(conn, user, requisition_id)
    rows = await conn.fetch(_LIST_SQL, requisition_id)
    boxes = [box_out(r, req["material_sku_name"]) for r in rows]
    return {"requisition_id": requisition_id, "status": req["status"], "boxes": boxes,
            "totals": totals(boxes)}


async def scan_box(conn, user, requisition_id: int, *, code: str) -> dict[str, Any]:
    req = await _load_issued(conn, user, requisition_id)
    box_code, _ = parse_label(code)
    # A box already on the request is refused before the (slower) lookup.
    if box_code and await conn.fetchval(
            "SELECT 1 FROM floor_requisition_box WHERE requisition_id = $1 AND box_code = $2",
            requisition_id, box_code):
        raise _duplicate(requisition_id, box_code)
    box = await resolve_box(conn, code)
    article = (box["article"] or "").strip()[:_TEXT_LIMIT] or UNKNOWN_ARTICLE
    row = await conn.fetchrow(
        _INSERT_SCANNED_SQL, requisition_id, box["box_code"], box["box_table"], box["transaction_no"],
        article, box["lot_number"], box["net_weight"], box["gross_weight"], _int(box["count"]),
        rules.actor_name(user))
    if row is None:
        status = await conn.fetchval("SELECT status FROM floor_requisition WHERE requisition_id = $1",
                                     requisition_id)
        if status != "issued":
            raise _not_issued(requisition_id, status)
        raise _duplicate(requisition_id, box["box_code"])
    return box_out(row, req["material_sku_name"])
```

(`math`, `insert_with_pk_retry`, `new_short_time_id`, `MAX_PRINT`, `STOCK_TYPES`, `_LOT_LIMIT` are used by Task 3.)

- [ ] **Step 4: Run — expect PASS**

---

### Task 3: box_service — print and remove

**Files:**
- Modify: `server_replica/app/modules/floor_requisition/services/box_service.py` (append)
- Test: `server_replica/tests/services/test_floor_requisition_box_service.py` (append)

**Interfaces:**
- Consumes: Task 2's `_load_issued`, `box_out`, `RequisitionError`, the FakeConn.
- Produces: `check_print_box(box: dict) -> dict`, `print_boxes(conn, user, requisition_id, *, article, stock_type, boxes: list[dict]) -> {requisition_id, boxes}`, `remove_box(conn, user, requisition_id, box_code) -> {requisition_id, box_code, removed}`.

- [ ] **Step 1: Append the failing tests**

```python
# ── print ──
def _line(n, net=9.77, gross=10.0, count=3, lot="L-7"):
    return {"box_number": n, "net_weight": net, "gross_weight": gross, "count": count, "lot_number": lot}


def test_print_mints_rm_boxes_in_sfg_box_and_records_them(monkeypatch):
    monkeypatch.setattr(bs, "new_short_time_id", lambda: 48213307)
    conn = FakeConn(jc={"job_card_number": "PLAN-1-L1-S1", "entity": "cfpl"}, last=4)
    out = run(bs.print_boxes(conn, User(), 27385955, article="  Sunflower Seeds Roasted ",
                             stock_type="Fresh Stock", boxes=[_line(1), _line(2, lot="  ")]))
    assert [a[0] for a in conn.sfg_inserts] == ["48213307-5", "48213307-6"]
    assert conn.sfg_inserts[0] == ("48213307-5", 75009889, "PLAN-1-L1-S1", SEEDS, "cfpl", "Mezzanine",
                                   "L-7", 9.77, 10.0, 3, "Store Sam")
    assert conn.sfg_inserts[1][6] is None                     # blank LOT → NULL
    sql = " ".join(bs._INSERT_SFG_SQL.split())
    assert "VALUES ($1, 'rm', $2, $3, $4, $4, $5, $6, 'Stores', $7, $8, $9, $10, 'PRINTED', $11)" in sql
    assert conn.box_inserts[0] == (27385955, "48213307-5", 1, "27385955", SEEDS, "Fresh Stock", "L-7",
                                   9.77, 10.0, 3, "Store Sam")
    assert [b["box_code"] for b in out["boxes"]] == ["48213307-5", "48213307-6"]
    assert out["boxes"][0]["source"] == "printed" and out["boxes"][0]["box_number"] == 1
    assert conn.ran("SELECT status FROM floor_requisition WHERE requisition_id = $1 FOR UPDATE")


def test_print_without_a_readable_job_card_still_prints(monkeypatch):
    monkeypatch.setattr(bs, "new_short_time_id", lambda: 11111111)
    conn = FakeConn(jc=None)
    run(bs.print_boxes(conn, User(), 27385955, article=SEEDS, stock_type="Fresh Stock", boxes=[_line(1)]))
    assert conn.sfg_inserts[0][:5] == ("11111111-1", 75009889, None, SEEDS, None)


@pytest.mark.parametrize("line, error", [
    (_line(1, net=0), "bad_box"),
    (_line(1, net=-2), "bad_box"),
    (_line(1, net=float("inf")), "bad_box"),
    (_line(1, net=5, gross=4.99), "bad_box"),
    (_line(1, count=-1), "bad_box"),
    (_line(0), "bad_box_number"),
])
def test_print_refuses_a_bad_box(line, error):
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(FakeConn(), User(), 27385955, article=SEEDS, stock_type="Fresh Stock", boxes=[line]))
    assert _refusal(e) == (400, error)


def test_a_bad_box_message_names_the_box():
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(FakeConn(), User(), 27385955, article=SEEDS, stock_type="Fresh Stock",
                           boxes=[_line(1), _line(7, net=0)]))
    assert e.value.message == "Box 7: net wt must be more than 0."


@pytest.mark.parametrize("kw, error", [
    ({"article": "   "}, "article_required"),
    ({"article": "x" * 501}, "article_too_long"),
    ({"stock_type": "Rotten"}, "bad_stock_type"),
    ({"boxes": []}, "bad_box_count"),
    ({"boxes": [_line(n) for n in range(1, 502)]}, "bad_box_count"),
    ({"boxes": [_line(1), _line(1)]}, "duplicate_box_number"),
])
def test_print_refuses_a_bad_request(kw, error):
    args = {"article": SEEDS, "stock_type": "Fresh Stock", "boxes": [_line(1)], **kw}
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(FakeConn(), User(), 27385955, **args))
    assert _refusal(e) == (400, error)


def test_print_refuses_a_box_number_already_used_on_the_request():
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(FakeConn(taken=[3]), User(), 27385955, article=SEEDS, stock_type="Fresh Stock",
                           boxes=[_line(3), _line(4)]))
    assert _refusal(e) == (409, "box_number_taken")
    assert e.value.details["box_numbers"] == [3]


def test_print_only_while_issued_under_the_lock():
    conn = FakeConn(locked_status="received")
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(conn, User(), 27385955, article=SEEDS, stock_type="Fresh Stock", boxes=[_line(1)]))
    assert _refusal(e) == (409, "not_issued")
    assert not conn.sfg_inserts


# ── remove ──
def test_remove_a_scanned_box_deletes_only_its_row():
    conn = FakeConn(source="scanned")
    out = run(bs.remove_box(conn, User(), 27385955, " BX-1 "))
    assert out == {"requisition_id": 27385955, "box_code": "BX-1", "removed": True}
    assert not conn.ran("DELETE FROM sfg_box")
    assert conn.ran("DELETE FROM floor_requisition_box")[0][2] == (27385955, "BX-1")


def test_remove_a_printed_box_deletes_its_sfg_box_row_too():
    conn = FakeConn(source="printed")
    run(bs.remove_box(conn, User(), 27385955, "48213307-5"))
    kind, sql, args = conn.ran("DELETE FROM sfg_box")[0]
    assert "item_type = 'rm' AND status = 'PRINTED'" in sql and args == ("48213307-5",)
    assert conn.ran("DELETE FROM floor_requisition_box")


def test_a_printed_box_the_floor_scanned_cannot_be_removed():
    conn = FakeConn(source="printed", in_jc=True)
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.remove_box(conn, User(), 27385955, "48213307-5"))
    assert _refusal(e) == (409, "box_in_use")
    assert not conn.ran("DELETE")


def test_a_printed_box_that_moved_past_printed_cannot_be_removed():
    conn = FakeConn(source="printed", sfg_delete="DELETE 0", sfg_still_there=True)
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.remove_box(conn, User(), 27385955, "48213307-5"))
    assert _refusal(e) == (409, "box_in_use")
    assert not conn.ran("DELETE FROM floor_requisition_box")


def test_without_jc_box_scan_a_printed_box_is_not_in_use():
    conn = FakeConn(source="printed", jc_table=False)
    run(bs.remove_box(conn, User(), 27385955, "48213307-5"))
    assert not conn.ran("SELECT 1 FROM jc_box_scan")
    assert conn.ran("DELETE FROM sfg_box")


def test_remove_a_box_not_on_the_request_is_404():
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.remove_box(FakeConn(source=None), User(), 27385955, "BX-404"))
    assert _refusal(e) == (404, "box_not_on_request")


def test_remove_only_while_issued():
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.remove_box(FakeConn(req=_req(status="received")), User(), 27385955, "BX-1"))
    assert _refusal(e) == (409, "not_issued")
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: ... has no attribute 'print_boxes'`)

- [ ] **Step 3: Append the implementation**

```python
_COUNTER_SQL = """
    SELECT COALESCE(MAX(CAST(split_part(carton_id, '-', 2) AS INTEGER)), 0)
      FROM sfg_box
     WHERE job_card_id = $1
       AND split_part(carton_id, '-', 2) ~ '^[0-9]+$'
"""

_INSERT_SFG_SQL = """
    INSERT INTO sfg_box (carton_id, item_type, job_card_id, job_card_number, sfg_code, fg_sku_name,
                         entity, floor, stage_bucket, batch_code, net_weight, gross_weight, units,
                         status, created_by)
    VALUES ($1, 'rm', $2, $3, $4, $4, $5, $6, 'Stores', $7, $8, $9, $10, 'PRINTED', $11)
    RETURNING carton_id
"""

_INSERT_PRINTED_SQL = f"""
    INSERT INTO floor_requisition_box
           (requisition_id, box_code, source, box_table, box_number, transaction_no, article,
            stock_type, lot_number, net_weight, gross_weight, count, recorded_by)
    VALUES ($1, $2, 'printed', 'sfg_box', $3, $4, $5, $6, $7, $8, $9, $10, $11)
    RETURNING {BOX_COLS}
"""


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _whole(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_print_box(box: dict[str, Any]) -> dict[str, Any]:
    """One box of a print: the browser's checks, repeated (lib/box-scan.ts)."""
    n = box.get("box_number")
    if not _whole(n) or n < 1:
        raise RequisitionError(400, "bad_box_number", "Every box needs a box number of 1 or more.")
    net, gross, count = box.get("net_weight"), box.get("gross_weight"), box.get("count")
    if not _finite(net) or net <= 0:
        raise RequisitionError(400, "bad_box", f"Box {n}: net wt must be more than 0.", box_number=n)
    if gross is not None and (not _finite(gross) or gross < net):
        raise RequisitionError(400, "bad_box", f"Box {n}: gross wt can't be less than net wt.", box_number=n)
    if count is not None and (not _whole(count) or count < 0):
        raise RequisitionError(400, "bad_box", f"Box {n}: count must be a whole number, 0 or more.",
                               box_number=n)
    lot = (box.get("lot_number") or "").strip()[:_LOT_LIMIT] or None
    return {"box_number": n, "net_weight": round(float(net), 3),
            "gross_weight": round(float(gross), 3) if gross is not None else None,
            "count": count, "lot_number": lot}


async def print_boxes(conn, user, requisition_id: int, *, article: str, stock_type: str,
                      boxes: list[dict[str, Any]]) -> dict[str, Any]:
    """Mint one sfg_box row ('rm', PRINTED) and one request row per box. The
    browser prints the stickers with the ids returned, in the order given."""
    req = await _load_issued(conn, user, requisition_id, lock=True)
    name = (article or "").strip()
    if not name:
        raise RequisitionError(400, "article_required", "Choose the article to print.")
    if len(name) > _TEXT_LIMIT:
        raise RequisitionError(400, "article_too_long", f"The article name is over {_TEXT_LIMIT} characters.")
    if stock_type not in STOCK_TYPES:
        raise RequisitionError(400, "bad_stock_type", f"Stock type must be one of: {', '.join(STOCK_TYPES)}.")
    if not 1 <= len(boxes) <= MAX_PRINT:
        raise RequisitionError(400, "bad_box_count", f"Print between 1 and {MAX_PRINT} boxes at a time.")
    checked = [check_print_box(b) for b in boxes]
    numbers = [c["box_number"] for c in checked]
    if len(set(numbers)) != len(numbers):
        raise RequisitionError(400, "duplicate_box_number", "The same box number appears twice.")
    taken = sorted(r["box_number"] for r in await conn.fetch(
        "SELECT box_number FROM floor_requisition_box WHERE requisition_id = $1 AND box_number = ANY($2::int[])",
        requisition_id, numbers))
    if taken:
        raise RequisitionError(409, "box_number_taken",
                               f"Box number {', '.join(map(str, taken))} is already on this request. "
                               "Generate the boxes again.", box_numbers=taken)

    jc = await conn.fetchrow("SELECT job_card_number, entity FROM job_card_v2 WHERE job_card_id = $1",
                             req["job_card_id"])
    last = await conn.fetchval(_COUNTER_SQL, req["job_card_id"]) or 0
    actor, txn = rules.actor_name(user), str(requisition_id)
    out = []
    for i, c in enumerate(checked, 1):
        # "<8-digit time>-<counter>", as create_wip_boxes mints; a PK clash re-rolls the time.
        async def _insert(_c=c, _counter=last + i):
            return await conn.fetchval(
                _INSERT_SFG_SQL, f"{new_short_time_id()}-{_counter}", req["job_card_id"],
                jc["job_card_number"] if jc else None, name, jc["entity"] if jc else None, req["floor"],
                _c["lot_number"], _c["net_weight"], _c["gross_weight"], _c["count"], actor)

        carton_id = await insert_with_pk_retry(conn, _insert)
        row = await conn.fetchrow(_INSERT_PRINTED_SQL, requisition_id, carton_id, c["box_number"], txn,
                                  name, stock_type, c["lot_number"], c["net_weight"], c["gross_weight"],
                                  c["count"], actor)
        out.append(box_out(row, req["material_sku_name"]))
    return {"requisition_id": requisition_id, "boxes": out}


def _in_use(code: str) -> RequisitionError:
    return RequisitionError(409, "box_in_use",
                            f"Box {code} is already in use (scanned into a job card or received), "
                            "so it can't be removed.", box_code=code)


async def _in_job_card(conn, code: str) -> bool:
    # jc_box_scan is created out-of-band and exists only on RDS; elsewhere nothing uses the box.
    if not await conn.fetchval("SELECT to_regclass('jc_box_scan') IS NOT NULL"):
        return False
    return bool(await conn.fetchval(
        "SELECT 1 FROM jc_box_scan WHERE sfg_box_id = $1 OR box_id = $1 LIMIT 1", code))


async def remove_box(conn, user, requisition_id: int, box_code: str) -> dict[str, Any]:
    """Take a box off the request. A printed box's sfg_box row goes too, so its
    sticker stops being recognised — unless the floor has already used it."""
    await _load_issued(conn, user, requisition_id, lock=True)
    code = (box_code or "").strip()
    source = await conn.fetchval(
        "SELECT source FROM floor_requisition_box WHERE requisition_id = $1 AND box_code = $2",
        requisition_id, code)
    if source is None:
        raise RequisitionError(404, "box_not_on_request", f"Box {code} isn't on request {requisition_id}.",
                               requisition_id=requisition_id, box_code=code)
    if source == "printed":
        if await _in_job_card(conn, code):
            raise _in_use(code)
        deleted = await conn.execute(
            "DELETE FROM sfg_box WHERE carton_id = $1 AND item_type = 'rm' AND status = 'PRINTED'", code)
        if deleted.split()[-1] == "0" and await conn.fetchval("SELECT 1 FROM sfg_box WHERE carton_id = $1",
                                                              code):
            raise _in_use(code)
    await conn.execute("DELETE FROM floor_requisition_box WHERE requisition_id = $1 AND box_code = $2",
                       requisition_id, code)
    return {"requisition_id": requisition_id, "box_code": code, "removed": True}
```

- [ ] **Step 4: Run — expect PASS**

---

### Task 4: Router endpoints

**Files:**
- Modify: `server_replica/app/modules/floor_requisition/router.py` (LF)
- Modify: `server_replica/tests/services/test_floor_requisition_router.py` (LF)

**Interfaces:**
- Consumes: `box_service.list_boxes / scan_box / print_boxes / remove_box` (Tasks 2–3).
- Produces: `GET /{requisition_id}/boxes` (view), `POST /{requisition_id}/boxes/scan` (issue), `POST /{requisition_id}/boxes/print` (issue), `DELETE /{requisition_id}/boxes/{box_code}` (issue); models `ScanBoxBody{code}`, `PrintBoxLine{box_number, net_weight, gross_weight?, count?, lot_number?}`, `PrintBoxesBody{article, stock_type, boxes}`.

- [ ] **Step 1: Update the router tests**

Add to `EXPECTED`:

```python
    "/api/v1/floor-requisitions/{requisition_id}/boxes": {"GET": "view"},
    "/api/v1/floor-requisitions/{requisition_id}/boxes/scan": {"POST": "issue"},
    "/api/v1/floor-requisitions/{requisition_id}/boxes/print": {"POST": "issue"},
    "/api/v1/floor-requisitions/{requisition_id}/boxes/{box_code}": {"DELETE": "issue"},
```

Extend `test_the_body_cannot_name_who_did_it` to also cover the box bodies:

```python
def test_the_body_cannot_name_who_did_it():
    for model in (R.RaiseBody, R.IssueBody, R.CancelBody, R.ScanBoxBody, R.PrintBoxesBody, R.PrintBoxLine):
        assert not {"raised_by", "issued_by", "received_by", "cancelled_by", "recorded_by",
                    "created_by"} & set(model.model_fields)
```

Append:

```python
from app.modules.floor_requisition.services import box_service as box_svc


class _TxConn(_Conn):
    def __init__(self):
        super().__init__()
        self.opened = 0

    def transaction(self):
        self.opened += 1
        return super().transaction()


def _box_request():
    conn = _TxConn()
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn), conn=conn)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool))), conn


def test_scan_runs_outside_a_transaction(monkeypatch):
    seen = {}

    async def scan(conn, user, requisition_id, *, code):
        seen.update(requisition_id=requisition_id, code=code)
        return {"box_code": "BX-1"}

    monkeypatch.setattr(box_svc, "scan_box", scan)
    req, conn = _box_request()
    out = asyncio.run(R.scan_requisition_box(req, 27385955, R.ScanBoxBody(code='{"tx":"T","bi":"BX-1"}'),
                                             user=object()))
    assert out == {"box_code": "BX-1"} and conn.opened == 0
    assert seen == {"requisition_id": 27385955, "code": '{"tx":"T","bi":"BX-1"}'}


def test_print_runs_in_a_transaction_and_passes_the_boxes(monkeypatch):
    seen = {}

    async def print_boxes(conn, user, requisition_id, **kw):
        seen.update(requisition_id=requisition_id, **kw)
        return {"requisition_id": requisition_id, "boxes": []}

    monkeypatch.setattr(box_svc, "print_boxes", print_boxes)
    req, conn = _box_request()
    body = R.PrintBoxesBody(article="Seeds", stock_type="Off Grade/Rejection",
                            boxes=[R.PrintBoxLine(box_number=2, net_weight=9.5, lot_number="L1")])
    asyncio.run(R.print_requisition_boxes(req, 27385955, body, user=object()))
    assert conn.opened == 1
    assert seen == {"requisition_id": 27385955, "article": "Seeds", "stock_type": "Off Grade/Rejection",
                    "boxes": [{"box_number": 2, "net_weight": 9.5, "gross_weight": None, "count": None,
                               "lot_number": "L1"}]}


def test_remove_runs_in_a_transaction(monkeypatch):
    async def remove(conn, user, requisition_id, box_code):
        return {"requisition_id": requisition_id, "box_code": box_code, "removed": True}

    monkeypatch.setattr(box_svc, "remove_box", remove)
    req, conn = _box_request()
    out = asyncio.run(R.remove_requisition_box(req, 27385955, "BX-1", user=object()))
    assert out["removed"] is True and conn.opened == 1


def test_a_box_refusal_becomes_error_message_details(monkeypatch):
    async def scan(conn, user, requisition_id, *, code):
        raise svc.RequisitionError(409, "duplicate_box", "Box BX-1 is already on request 27385955.",
                                   requisition_id=27385955, box_code="BX-1")

    monkeypatch.setattr(box_svc, "scan_box", scan)
    req, _ = _box_request()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.scan_requisition_box(req, 27385955, R.ScanBoxBody(code="BX-1"), user=object()))
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "duplicate_box"
    assert exc.value.detail["details"] == {"requisition_id": 27385955, "box_code": "BX-1"}


def test_print_body_limits():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        R.PrintBoxesBody(article="S", boxes=[])
    with pytest.raises(pydantic.ValidationError):
        R.PrintBoxesBody(article="S", stock_type="Rotten", boxes=[R.PrintBoxLine(box_number=1, net_weight=1)])
    with pytest.raises(pydantic.ValidationError):
        R.PrintBoxLine(box_number=0, net_weight=1)
```

- [ ] **Step 2: Run — expect FAIL** (routes missing / `AttributeError: ScanBoxBody`)

Run: `python -m pytest tests/services/test_floor_requisition_router.py -q`

- [ ] **Step 3: Implement** — in `router.py`: add to the docstring route list

```
    GET    /api/v1/floor-requisitions/{requisition_id}/boxes            boxes sent (view)
    POST   /api/v1/floor-requisitions/{requisition_id}/boxes/scan       scan one (issue)
    POST   /api/v1/floor-requisitions/{requisition_id}/boxes/print      manual print (issue)
    DELETE /api/v1/floor-requisitions/{requisition_id}/boxes/{box_code} remove one (issue)
```

add the import `from app.modules.floor_requisition.services import box_service as box_svc`, then after `CancelBody`:

```python
class ScanBoxBody(BaseModel):
    # The raw QR: Material-In's {"tx","bi"} or a bare box id.
    code: str = Field(..., min_length=1, max_length=2000)


class PrintBoxLine(BaseModel):
    box_number: int = Field(..., ge=1)
    net_weight: float
    gross_weight: Optional[float] = None
    count: Optional[int] = Field(None, ge=0)
    lot_number: Optional[str] = Field(None, max_length=100)


class PrintBoxesBody(BaseModel):
    article: str = Field(..., min_length=1, max_length=500)
    stock_type: Literal["Fresh Stock", "Off Grade/Rejection"] = "Fresh Stock"
    boxes: list[PrintBoxLine] = Field(..., min_length=1, max_length=500)
```

and at the end of the file:

```python
@router.get("/{requisition_id}/boxes")
async def list_requisition_boxes(
    request: Request, requisition_id: int, user: AuthUser = Depends(_perm("view")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            return await box_svc.list_boxes(conn, user, requisition_id)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.post("/{requisition_id}/boxes/scan")
async def scan_requisition_box(
    request: Request, requisition_id: int, body: ScanBoxBody,
    user: AuthUser = Depends(_perm("issue")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        # No transaction: the lookup can fail a statement on schema drift, which
        # would poison one (box_service docstring). Its one write is guarded itself.
        async with pool.acquire() as conn:
            return await box_svc.scan_box(conn, user, requisition_id, code=body.code)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.post("/{requisition_id}/boxes/print")
async def print_requisition_boxes(
    request: Request, requisition_id: int, body: PrintBoxesBody,
    user: AuthUser = Depends(_perm("issue")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await box_svc.print_boxes(
                    conn, user, requisition_id, article=body.article, stock_type=body.stock_type,
                    boxes=[b.model_dump() for b in body.boxes])
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.delete("/{requisition_id}/boxes/{box_code}")
async def remove_requisition_box(
    request: Request, requisition_id: int, box_code: str,
    user: AuthUser = Depends(_perm("issue")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await box_svc.remove_box(conn, user, requisition_id, box_code)
    except svc.RequisitionError as exc:
        raise _http(exc) from None
```

- [ ] **Step 4: Run — expect PASS**, plus the whole requisition suite: `python -m pytest tests/services -q -k floor_requisition`

---

### Task 5: Keep store boxes out of the job card genealogy

**Files:**
- Modify: `server_replica/app/modules/production/services/sfg_box_service.py` (`get_jc_genealogy`, CRLF)
- Test: `server_replica/tests/services/test_sfg_genealogy_rm.py` (LF)

**Interfaces:** Consumes `sfg_box_service.get_jc_genealogy(conn, job_card_id, allowed_entities=None)`.

- [ ] **Step 1: Write the failing test**

```python
"""get_jc_genealogy's "produced" list is what the job card made. Boxes store
printed for the job card's requisitions (sfg_box item_type 'rm', migration 113)
carry its job_card_id but were not produced by it."""
from __future__ import annotations

import asyncio

from app.modules.production.services import sfg_box_service as sbs


class _Conn:
    def __init__(self):
        self.sql: list[str] = []

    async def fetch(self, sql, *args):
        self.sql.append(" ".join(sql.split()))
        return []


def test_produced_leaves_out_store_boxes():
    conn = _Conn()
    asyncio.run(sbs.get_jc_genealogy(conn, 75009889))
    produced = next(s for s in conn.sql if "WHERE job_card_id = $1" in s)
    assert "item_type <> 'rm'" in produced


def test_consumed_is_unchanged():
    conn = _Conn()
    asyncio.run(sbs.get_jc_genealogy(conn, 75009889))
    consumed = next(s for s in conn.sql if "received_into_job_card_id = $1" in s)
    assert "item_type" not in consumed.split("WHERE", 1)[1]
```

- [ ] **Step 2: Run — expect FAIL** on the first test.

- [ ] **Step 3: Implement** (CRLF preserved) — change the produced query and the docstring line:

```python
    produced = sfg_box WHERE job_card_id = id (this JC minted them), leaving out
    store's manually printed boxes (item_type 'rm', migration 113).
```

```python
    produced_rows = await conn.fetch(
        f"SELECT {_BOX_GENEALOGY_COLS} FROM sfg_box "
        f"WHERE job_card_id = $1 AND item_type <> 'rm' ORDER BY created_at, carton_id",
        job_card_id,
    )
```

- [ ] **Step 4: Run — expect PASS.** Verify `sfg_box_service.py` is still all CRLF.

---

### Task 6: Web API client + helpers

**Files:**
- Modify: `web_replica/src/lib/floor-requisitions.ts`
- Modify: `web_replica/src/lib/box-scan.ts`, `web_replica/src/lib/box-scan.test.ts`

**Interfaces:**
- Produces (floor-requisitions.ts): `RequisitionBox`, `RequisitionBoxes`, `PrintBoxLine`, `listRequisitionBoxes(id, signal?)`, `scanRequisitionBox(id, code)`, `printRequisitionBoxes(id, {article, stock_type, boxes})`, `removeRequisitionBox(id, code)`.
- Produces (box-scan.ts): keeps `parseBoxQr`, `checkPrintDetail`, `checkBoxesForPrint`, `scanTotals`; adds `nextBoxNumber(taken)`; removes `manualBoxId`, `nextManualBoxId`, `upsertByCode`.

- [ ] **Step 1: Update `box-scan.test.ts`** — import line becomes

```ts
import {
  checkBoxesForPrint, checkPrintDetail, nextBoxNumber, parseBoxQr, scanTotals,
} from "./box-scan.ts";
```

delete the `// ── the id a printed box gets ──` block, the `manualBoxId` check and the `// ── printed boxes into the list ──` block; add:

```ts
// ── the next box number ──
check("first box", nextBoxNumber([]), 1);
check("after the highest", nextBoxNumber([3, 1, 7]), 8);
check("blanks are ignored", nextBoxNumber([null, 2, undefined]), 3);
```

- [ ] **Step 2: Run — expect FAIL** (`nextBoxNumber` not exported): `node src/lib/box-scan.test.ts`

- [ ] **Step 3: Implement** — in `box-scan.ts` replace the `manualBoxId` / `nextManualBoxId` functions and `upsertByCode` with:

```ts
/** The next "Box #" for a request: one past the highest already taken (boxes on
 *  the request and in the drafts). */
export function nextBoxNumber(taken: readonly (number | null | undefined)[]): number {
  let highest = 0;
  for (const n of taken) if (n != null && n > highest) highest = n;
  return highest + 1;
}
```

and update the file header's second paragraph to: "Stores → Production Indents' Scan material dialog uses the job card's scanner and a manual sticker print; the server stores the boxes (floor-requisitions `/boxes`). The job card's Raw Material tab reads a sticker the same way …".

In `floor-requisitions.ts` append:

```ts
/** One box store scanned or printed for a request (floor_requisition_box). */
export interface RequisitionBox {
  requisition_id: number;
  box_code: string;
  source: "printed" | "scanned";
  /** Where the box was found: sfg_box, po_box, or a legacy box table. */
  box_table: string;
  /** Printed boxes: the "Box #" on the sticker. */
  box_number: number | null;
  transaction_no: string | null;
  article: string;
  /** Printed boxes: "Fresh Stock" or "Off Grade/Rejection". */
  stock_type: string | null;
  lot_number: string | null;
  net_weight: number | null;
  gross_weight: number | null;
  count: number | null;
  recorded_by: string;
  recorded_at: string | null;
  /** The box's article is not the requested material. */
  article_mismatch: boolean;
}

export interface RequisitionBoxes {
  requisition_id: number;
  status: RequisitionStatus;
  boxes: RequisitionBox[];
  totals: { boxes: number; net_weight: number; gross_weight: number; count: number };
}

export interface PrintBoxLine {
  box_number: number;
  net_weight: number;
  gross_weight: number | null;
  count: number | null;
  lot_number: string | null;
}

export async function listRequisitionBoxes(id: number, signal?: AbortSignal): Promise<RequisitionBoxes> {
  return readOrThrow(await apiFetch(`${BASE}/${id}/boxes`, { signal }), "Failed to load the boxes");
}

/** `code` is the raw QR — the server reads {"tx","bi"} itself. */
export async function scanRequisitionBox(id: number, code: string): Promise<RequisitionBox> {
  return readOrThrow(await post(`${BASE}/${id}/boxes/scan`, { code }), "Failed to record the box");
}

export async function printRequisitionBoxes(
  id: number,
  body: { article: string; stock_type: string; boxes: PrintBoxLine[] },
): Promise<{ requisition_id: number; boxes: RequisitionBox[] }> {
  return readOrThrow(await post(`${BASE}/${id}/boxes/print`, body), "Failed to save the boxes");
}

export async function removeRequisitionBox(
  id: number,
  code: string,
): Promise<{ requisition_id: number; box_code: string; removed: boolean }> {
  return readOrThrow(
    await apiFetch(`${BASE}/${id}/boxes/${encodeURIComponent(code)}`, { method: "DELETE" }),
    "Failed to remove the box",
  );
}
```

- [ ] **Step 4: Run — expect PASS**: `node src/lib/box-scan.test.ts`

---

### Task 7: Web dialog and Manual print on the server

**Files:**
- Modify: `web_replica/src/app/modules/stores/production-indents/_ManualPrint.tsx`
- Modify: `web_replica/src/app/modules/stores/production-indents/_ScanMaterialDialog.tsx`

**Interfaces:**
- Consumes: Task 6's API functions and `nextBoxNumber`, `checkBoxesForPrint`, `parseBoxQr`, `scanTotals`; `printLabels` (Material-In); `QrScanner`; `PrinterIcon` from `_SectionEditor`.
- Produces: `ManualPrint({requisition, takenNumbers, onSaved, onMessage})`, `printStickers(requisition, boxes)`, `OffGradeTag`.

- [ ] **Step 1: `_ManualPrint.tsx` changes**
  - Header comment: numbers run on past the request's boxes; printing saves through `/boxes/print` first, then prints stickers with the returned ids; printed rows leave the draft.
  - Remove `PrintedBox`, `manualBoxId`/`nextManualBoxId` imports, `listedCodes`/`printedCodes` props, `greenIds`.
  - Props: `takenNumbers: readonly number[]`, `onSaved: (boxes: RequisitionBox[]) => void`.
  - Add exports:

```tsx
/** Material-In's sticker for a stored box: QR {"tx": request no, "bi": box id}. */
export function stickerFor(b: RequisitionBox): PrintBox {
  return {
    box_id: b.box_code,
    box_number: b.box_number ?? 0,
    net_weight: b.net_weight != null ? b.net_weight.toFixed(3) : "",
    gross_weight: b.gross_weight != null ? b.gross_weight.toFixed(3) : "",
    lot_number: b.lot_number ?? "",
    count: b.count != null ? String(b.count) : "",
    line_number: 0,
    section_number: null,
    sku_name: b.article,
  };
}

export function printStickers(r: FloorRequisition, boxes: RequisitionBox[]): Promise<void> {
  return printLabels({ entity: r.warehouse, transaction_no: String(r.requisition_id), boxes: boxes.map(stickerFor) });
}
```

  - `generate(sec)`: `const start = nextBoxNumber([...takenNumbers, ...sections.flatMap((s) => (s.boxes ?? []).map((b) => b.box_number))]);`
  - `toPrintBox(b)`: `box_id: null` (unsaved draft), rest as now.
  - `handlePrint(resolve)`:

```tsx
  async function handlePrint(resolve: PrintResolver) {
    if (printingRef.current) return;
    const picked = await resolve();
    const checked = checkBoxesForPrint(picked);
    if (!checked.ok) {
      onMessage({ kind: "err", text: checked.message });
      return;
    }
    const lines: PrintBoxLine[] = checked.boxes.map((c, i) => ({ ...c, lot_number: picked[i].lot_number.trim() || null }));
    printingRef.current = true;
    setPrinting(true);
    try {
      let saved: RequisitionBox[];
      try {
        saved = (await printRequisitionBoxes(r.requisition_id, {
          article: article.name, stock_type: article.stock_type, boxes: lines,
        })).boxes;
      } catch (e) {
        onMessage({ kind: "err", text: e instanceof Error ? e.message : String(e) });
        return;
      }
      onSaved(saved);
      dropPrinted(new Set(saved.map((b) => b.box_number ?? -1)));
      const n = saved.length;
      try {
        await printStickers(r, saved);
        onMessage({ kind: "ok", text: `Saved and printed ${n} sticker${n === 1 ? "" : "s"}.` });
      } catch (e) {
        onMessage({ kind: "err", text: `Saved ${n} box${n === 1 ? "" : "es"}, but the print window didn't open (${e instanceof Error ? e.message : String(e)}). Print them with 🖨 in the list.` });
      }
    } finally {
      printingRef.current = false;
      setPrinting(false);
    }
  }

  // Printed rows leave the draft; a section whose rows are all printed goes back
  // to its fields (LOT kept) so it can generate again.
  function dropPrinted(numbers: Set<number>) {
    setSections((prev) => prev.map((s) => {
      if (!s.boxes) return s;
      const left = s.boxes.filter((b) => !numbers.has(b.box_number));
      return left.length ? { ...s, boxes: left } : { ...s, boxes: null, box_count: "", page: 1 };
    }));
  }
```

  - `SectionCard`: drop `greenIds`; `BoxTable` without it.

- [ ] **Step 2: `_ScanMaterialDialog.tsx` changes**
  - Header comment: boxes are stored (`floor_requisition_box`; printed ones also in `sfg_box` as `'rm'`); scanned boxes are looked up in the job card scanner's tables.
  - State: `boxes: RequisitionBox[]`, `loading`, `loadErr`, `removing: string | null`, `scanningRef`.
  - Load on open:

```tsx
  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setLoadErr(null);
    try {
      const data = await listRequisitionBoxes(r.requisition_id, signal);
      if (!signal?.aborted) setBoxes(data.boxes);
    } catch (e) {
      if (!signal?.aborted) setLoadErr(e instanceof Error ? e.message : String(e));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [r.requisition_id]);
  useEffect(() => {
    const c = new AbortController();
    // Deferred past the effect body (react-hooks/set-state-in-effect), as the job card's RM tab does.
    queueMicrotask(() => { void load(c.signal); });
    return () => c.abort();
  }, [load]);
```

  - Scan:

```tsx
  const handleScan = useCallback((value: string) => {
    const raw = value.trim();
    if (!raw || scanningRef.current) return;
    setDupWarn(null);
    scanningRef.current = true;
    void (async () => {
      try {
        const box = await scanRequisitionBox(r.requisition_id, raw);
        setBoxes((prev) => [box, ...prev]);
        flashToast(box.article_mismatch
          ? { kind: "err", text: `Added ${box.box_code}, but its article (${box.article}) is not the requested material.` }
          : { kind: "ok", text: `Added ${box.box_code}` });
      } catch (e) {
        if (e instanceof RequisitionConflictError && e.code === "duplicate_box") setDupWarn(parseBoxQr(raw).code || raw);
        flashToast({ kind: "err", text: e instanceof Error ? e.message : String(e) });
      } finally {
        scanningRef.current = false;
      }
    })();
  }, [r.requisition_id, flashToast]);
```

  - Remove:

```tsx
  async function removeBox(code: string) {
    setRemoving(code);
    try {
      await removeRequisitionBox(r.requisition_id, code);
      setBoxes((prev) => prev.filter((b) => b.box_code !== code));
      setDupWarn((w) => (w === code ? null : w));
      flashToast({ kind: "ok", text: `Removed ${code}` });
    } catch (e) {
      flashToast({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setRemoving(null);
    }
  }
```

  - Reprint: `printStickers(r, [b]).then(() => flashToast({ kind: "ok", text: `Sent ${b.box_code} to print.` }), (e) => flashToast({ kind: "err", text: `Couldn't print: ${e instanceof Error ? e.message : String(e)}` }))`.
  - `takenNumbers = useMemo(() => boxes.flatMap((b) => (b.box_number != null ? [b.box_number] : [])), [boxes])`; `ManualPrint` gets `takenNumbers` and `onSaved={(saved) => setBoxes((prev) => [...saved, ...prev])}`.
  - Remove the "Not saved yet" note; list heading "Boxes for this request"; list body: `loading` → "Loading…", `loadErr` → red message + Retry (`void load()`), else rows. Each row: code, Printed/Scanned tag, amber "Different article" tag when `article_mismatch`, `article` + `OffGradeTag` + `· Box #n` + `· Lot x` + `· Txn`, weights line, 🖨 (printed rows) and ✕ (disabled while `removing === b.box_code`).

- [ ] **Step 3: Typecheck + lint**

Run (web_replica): `npx tsc --noEmit -p . 2>&1 | grep -v '^\.next/'` → no output; `npx eslint src/app/modules/stores/production-indents/_ScanMaterialDialog.tsx src/app/modules/stores/production-indents/_ManualPrint.tsx src/lib/floor-requisitions.ts src/lib/box-scan.ts src/lib/box-scan.test.ts` → exit 0.

---

### Task 8: Full verification

- [ ] `python -m pytest tests/services -q` (server_replica): all new tests pass; the only failures are the 2 pre-existing ones in `test_sku_lookup_permission.py`.
- [ ] `node src/lib/box-scan.test.ts`, `node src/lib/floor-requisition-form.test.ts` (web_replica): pass.
- [ ] tsc clean outside `.next`; eslint exit 0 on changed web files.
- [ ] Line endings: `113_*.sql`, `migrate.py`, `sfg_box_service.py` all CRLF; every other touched file LF.
- [ ] Report to the user: migration 113 to apply (Supabase to try locally, RDS on deploy), nothing committed.
