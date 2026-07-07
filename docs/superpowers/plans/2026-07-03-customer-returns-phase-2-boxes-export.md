# Customer-Returns Phase 2 (Boxes + Excel Export) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add box management (single-box "Print" upsert, bulk box sync, box-edit audit log) and a styled Excel export with edited-cell highlighting to the `customer_returns` module.

**Architecture:** Thin FastAPI router → async asyncpg services, building on Phase 1. New `box_service.py` (upsert/bulk/log), export data + edited-cell lookup added to `query_service.py`, a pure openpyxl workbook builder in new `export_xlsx.py`, and 4 new router endpoints. Boxes are keyed by the natural key `(rtv_id, article_description, box_number)`; no integer ids.

**Tech Stack:** Python 3.14 venv, FastAPI, asyncpg, Pydantic 2, openpyxl 3.1.5 (already pinned), PostgreSQL. Tests are standalone scripts (no pytest).

**Spec:** `docs/superpowers/specs/2026-07-02-customer-returns-port-design.md` (§4 boxes table, §6 schemas, §7.3 box_service, §8 endpoints 1/2/13/14).
**Phase-2 contracts (exact source logic):** `.superpowers/sdd/phase2-contracts.md`.
**Phase 1 (done, merged):** migration `070`, `tables.py`, `schemas.py`, `query_service.py` (helpers/mappers/`get_cr`/`list_crs`/`_SORTABLE`/`_like_escape`), `create_service.py`, `router.py` (6 CRUD endpoints).

## Global Constraints

- **Runtime:** project `.venv` (Windows: `.venv/Scripts/python.exe`; POSIX: `.venv/bin/python`), `PYTHONPATH=.` from repo root.
- **DB access:** asyncpg only; `async def fn(conn, ...)` (conn first); positional `$1,$2…`; `conn.fetch/fetchrow/fetchval/execute`; `async with conn.transaction():` in the service, no explicit commit. No SQLAlchemy.
- **Error envelope:** `HTTPException(status, detail={"error": <machine_code>, "message": <human>, "details": {...}})` — `error`/`message` keys, never `code`.
- **Identity from JWT:** actor fields (`box_edit_logs.email_id`) come from `user.email`, never from request body/query.
- **Keys:** no integer `id`/`header_id`/`rtv_line_id`. Header PK `rtv_id` (`CR-` string); box PK `(rtv_id, article_description, box_number)`; `box_edit_logs.transaction_no` holds the `rtv_id` string. Box→line link resolved logically by `article_description == item_description` (never stored).
- **Company** resolved via `cr_table_names(company)` (Phase 1 whitelist) — never f-string raw company into SQL.
- **box_id formats (do NOT unify):** single-print (`upsert_box`) = `f"{base8}-{box_number}"`; bulk (`bulk_save_boxes`) = `f"{base8}-{box_number}-{inserted}"` where `base8 = str(int(time.time()*1000))[-8:]` and `inserted` is the 0-based running insert counter for the call. `box_id` is NULL until first print and is never regenerated once set.
- **Numeric response fields are strings** via `query_service._num_str`.
- **Route ordering:** literal routes (`/export`, `/box-edit-log`) MUST be declared **before** `/{company}` (FastAPI matches in declaration order, else `/export` binds as a company).
- **Out of Phase 2:** cold-stock mirror (Phase 4), `cr_box_summary_and_short` + save/approval flows (Phase 3), notifications, magic-link.

**Test convention:** DB tests connect to `Settings().DATABASE_URL` (live Supabase) and wrap all writes in an always-rolled-back transaction (safe). Each test prints `ASSERTIONS PASSED`. Run `PYTHONPATH=. <venv-python> tests/services/<file>.py`.

---

### Task 1: Box Pydantic schemas (7 models + Decimal18_3 alias)

**Files:**
- Modify: `app/modules/customer_returns/schemas.py`
- Test: `tests/services/test_cr_box_schemas.py`

**Interfaces:**
- Produces: `Decimal18_3` alias; `CRBoxUpsertRequest`, `CRBoxUpsertResponse`, `CRBulkBoxItem`, `CRBulkBoxUpdateRequest`, `CRBulkBoxUpdateResponse`, `CRBoxEditLogEntry`, `CRBoxEditLogRequest`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_box_schemas.py`:

```python
"""Pure-logic tests for the Phase-2 box schemas. Run:
    PYTHONPATH=. python tests/services/test_cr_box_schemas.py
"""
from decimal import Decimal
from pydantic import ValidationError
from app.modules.customer_returns import schemas


def main() -> None:
    # box_number must be >= 1
    try:
        schemas.CRBoxUpsertRequest(article_description="ALMOND W-320", box_number=0)
        raise AssertionError("expected box_number ge=1 error")
    except ValidationError:
        pass

    # Decimal18_3 accepts a 3dp weight; optionals default None
    up = schemas.CRBoxUpsertRequest(
        article_description="ALMOND W-320", box_number=1,
        net_weight=Decimal("25.750"), gross_weight=Decimal("26.000"), count=40,
    )
    assert up.net_weight == Decimal("25.750") and up.lot_number is None

    bulk = schemas.CRBulkBoxUpdateRequest(boxes=[
        schemas.CRBulkBoxItem(article_description="A", box_number=1, net_weight=Decimal("1.5")),
    ])
    assert len(bulk.boxes) == 1

    log = schemas.CRBoxEditLogRequest(
        email_id="x@y.in", box_id="50123456-1", rtv_id="CR-20260703120000",
        changes=[schemas.CRBoxEditLogEntry(field_name="net_weight", old_value="25", new_value="24")],
    )
    assert log.changes[0].field_name == "net_weight"

    resp = schemas.CRBulkBoxUpdateResponse(status="synced", rtv_id="CR-1")
    assert resp.inserted == 0 and resp.deleted == 0
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_box_schemas.py`
Expected: FAIL — `AttributeError: module ... has no attribute 'CRBoxUpsertRequest'`

- [ ] **Step 3: Add the alias imports**

At the top of `app/modules/customer_returns/schemas.py`, ensure these imports exist (add what's missing — `from __future__ import annotations` stays first):

```python
from decimal import Decimal
from typing import Annotated, List, Literal, Optional
```

(The file already imports `datetime`, `BaseModel`, `Field`, `field_validator` from Phase 1. `Annotated` and `Decimal` are the additions.)

- [ ] **Step 4: Append the models**

Append to `app/modules/customer_returns/schemas.py`:

```python
# ── box models (Phase 2) ────────────────────────────────────────────────
Decimal18_3 = Annotated[Decimal, Field(max_digits=18, decimal_places=3)]


class CRBoxUpsertRequest(BaseModel):
    article_description: str
    box_number: int = Field(..., ge=1)
    uom: Optional[str] = None
    conversion: Optional[str] = None
    net_weight: Optional[Decimal18_3] = None
    gross_weight: Optional[Decimal18_3] = None
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None
    count: Optional[int] = None


class CRBoxUpsertResponse(BaseModel):
    status: str
    box_id: str
    rtv_id: str
    article_description: str
    box_number: int


class CRBulkBoxItem(BaseModel):
    article_description: str
    box_number: int = Field(..., ge=1)
    uom: Optional[str] = None
    conversion: Optional[str] = None
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None
    net_weight: Optional[Decimal18_3] = None
    gross_weight: Optional[Decimal18_3] = None
    count: Optional[int] = None


class CRBulkBoxUpdateRequest(BaseModel):
    boxes: List[CRBulkBoxItem] = Field(default_factory=list)


class CRBulkBoxUpdateResponse(BaseModel):
    status: str
    rtv_id: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0


class CRBoxEditLogEntry(BaseModel):
    field_name: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None


class CRBoxEditLogRequest(BaseModel):
    email_id: str  # accepted for FE compat; the SERVICE ignores it and uses user.email (JWT)
    box_id: str
    rtv_id: str
    changes: List[CRBoxEditLogEntry]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_box_schemas.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 6: Commit**

```bash
git add app/modules/customer_returns/schemas.py tests/services/test_cr_box_schemas.py
git commit -m "feat(customer-returns): phase-2 box pydantic schemas"
```

---

### Task 2: `upsert_box` (single-box Print)

**Files:**
- Create: `app/modules/customer_returns/services/box_service.py`
- Test: `tests/services/test_cr_upsert_box_rollback.py`

**Interfaces:**
- Consumes: `cr_table_names` (Phase 1), schemas from Task 1.
- Produces: `box_service.upsert_box(conn, company, cr_id, payload: CRBoxUpsertRequest) -> dict` returning `{"status","box_id","rtv_id","article_description","box_number"}`; helper `_gen_single_box_id(box_number) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_upsert_box_rollback.py`:

```python
"""Rollback integration test for box_service.upsert_box. Run:
    PYTHONPATH=. python tests/services/test_cr_upsert_box_rollback.py
"""
import asyncio
import asyncpg
from decimal import Decimal
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service, box_service


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        created = await create_service.create_cr(
            conn, "CFPL",
            schemas.CRCreate(company="CFPL",
                header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME"),
                lines=[schemas.CRLineCreate(material_type="RM", item_category="N", sub_category="S",
                        item_description="ALMOND W-320", uom="KG", qty="1", rate="1")]),
            "t@x.in")
        cr_id = created["rtv_id"]

        # 1) insert a new box → box_id 2-part, status inserted
        r1 = await box_service.upsert_box(conn, "CFPL", cr_id, schemas.CRBoxUpsertRequest(
            article_description="ALMOND W-320", box_number=1,
            net_weight=Decimal("25.000"), gross_weight=Decimal("26.000"), lot_number="LOT1"))
        assert r1["status"] == "inserted"
        assert r1["box_id"].endswith("-1") and r1["box_id"].count("-") == 1, r1["box_id"]
        first_box_id = r1["box_id"]

        # 2) re-upsert same box → box_id preserved, COALESCE keeps lot when None passed
        r2 = await box_service.upsert_box(conn, "CFPL", cr_id, schemas.CRBoxUpsertRequest(
            article_description="ALMOND W-320", box_number=1, net_weight=Decimal("24.500")))
        assert r2["status"] == "updated" and r2["box_id"] == first_box_id
        got = await query_service.get_cr(conn, "CFPL", cr_id)
        b = next(x for x in got["boxes"] if x["box_number"] == 1)
        assert b["net_weight"] == "24.5"          # updated
        assert b["lot_number"] == "LOT1"          # preserved (None in payload didn't clear it)
        assert b["box_id"] == first_box_id

        # 3) new box_number → separate insert
        r3 = await box_service.upsert_box(conn, "CFPL", cr_id, schemas.CRBoxUpsertRequest(
            article_description="ALMOND W-320", box_number=2, net_weight=Decimal("25.000")))
        assert r3["status"] == "inserted" and r3["box_id"].endswith("-2")

        # 4) missing CR → 404
        from fastapi import HTTPException
        try:
            await box_service.upsert_box(conn, "CFPL", "CR-DOESNOTEXIST",
                schemas.CRBoxUpsertRequest(article_description="X", box_number=1))
            raise AssertionError("expected 404")
        except HTTPException as e:
            assert e.status_code == 404
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_upsert_box_rollback.py`
Expected: FAIL — `ModuleNotFoundError: ... box_service`

- [ ] **Step 3: Write `box_service.upsert_box`**

Create `app/modules/customer_returns/services/box_service.py`:

```python
"""Customer-Returns box operations: single-box Print upsert, bulk box sync,
and the box-edit audit log. Boxes are keyed by (rtv_id, article_description,
box_number); box_id is NULL until Print and never regenerated once set.
"""
from __future__ import annotations

import time

from fastapi import HTTPException

from app.modules.customer_returns import schemas
from app.modules.customer_returns.tables import cr_table_names


def _base8() -> str:
    """Last 8 digits of epoch-milliseconds — the box_id prefix."""
    return str(int(time.time() * 1000))[-8:]


def _gen_single_box_id(box_number: int) -> str:
    """Single-print box_id: '{base8}-{box_number}' (two parts)."""
    return f"{_base8()}-{box_number}"


async def _assert_cr_exists(conn, header_table: str, cr_id: str) -> None:
    exists = await conn.fetchval(f"SELECT 1 FROM {header_table} WHERE rtv_id = $1", cr_id)
    if not exists:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )


async def upsert_box(conn, company: str, cr_id: str,
                     payload: schemas.CRBoxUpsertRequest) -> dict:
    """Print/print-edit a single box. 3-way: existing+printed → COALESCE-update
    (preserve box_id); existing+unprinted → gen id + update; absent → insert."""
    tables = cr_table_names(company)
    await _assert_cr_exists(conn, tables["header"], cr_id)

    existing = await conn.fetchrow(
        f"SELECT box_id FROM {tables['boxes']} "
        "WHERE rtv_id = $1 AND article_description = $2 AND box_number = $3",
        cr_id, payload.article_description, payload.box_number,
    )

    if existing is not None:
        box_id = existing["box_id"] or _gen_single_box_id(payload.box_number)
        async with conn.transaction():
            await conn.execute(
                f"""
                UPDATE {tables['boxes']} SET
                    box_id = $4,
                    uom = COALESCE($5, uom),
                    conversion = COALESCE($6, conversion),
                    net_weight = COALESCE($7::numeric, net_weight),
                    gross_weight = COALESCE($8::numeric, gross_weight),
                    lot_number = COALESCE($9, lot_number),
                    item_mark = COALESCE($10, item_mark),
                    spl_remarks = COALESCE($11, spl_remarks),
                    vakkal = COALESCE($12, vakkal),
                    count = COALESCE($13::int, count),
                    updated_at = NOW()
                WHERE rtv_id = $1 AND article_description = $2 AND box_number = $3
                """,
                cr_id, payload.article_description, payload.box_number, box_id,
                payload.uom, payload.conversion, payload.net_weight, payload.gross_weight,
                payload.lot_number, payload.item_mark, payload.spl_remarks,
                payload.vakkal, payload.count,
            )
        status = "updated"
    else:
        box_id = _gen_single_box_id(payload.box_number)
        async with conn.transaction():
            await conn.execute(
                f"""
                INSERT INTO {tables['boxes']}
                    (rtv_id, article_description, box_number, box_id, uom, conversion,
                     net_weight, gross_weight, lot_number, item_mark, spl_remarks, vakkal, count)
                VALUES ($1,$2,$3,$4,$5,$6,
                        COALESCE($7::numeric, 0), COALESCE($8::numeric, 0),
                        $9,$10,$11,$12,$13)
                """,
                cr_id, payload.article_description, payload.box_number, box_id,
                payload.uom, payload.conversion, payload.net_weight, payload.gross_weight,
                payload.lot_number, payload.item_mark, payload.spl_remarks,
                payload.vakkal, payload.count,
            )
        status = "inserted"

    return {"status": status, "box_id": box_id, "rtv_id": cr_id,
            "article_description": payload.article_description, "box_number": payload.box_number}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_upsert_box_rollback.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/services/box_service.py tests/services/test_cr_upsert_box_rollback.py
git commit -m "feat(customer-returns): upsert_box single-box print"
```

---

### Task 3: `bulk_save_boxes` (full box-set sync)

**Files:**
- Modify: `app/modules/customer_returns/services/box_service.py`
- Test: `tests/services/test_cr_bulk_boxes_rollback.py`

**Interfaces:**
- Produces: `box_service.bulk_save_boxes(conn, company, cr_id, data: CRBulkBoxUpdateRequest, notify_discrepancy: bool = True) -> dict` returning `{"status":"synced","rtv_id",inserted,updated,unchanged,deleted}`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_bulk_boxes_rollback.py`:

```python
"""Rollback integration test for box_service.bulk_save_boxes. Run:
    PYTHONPATH=. python tests/services/test_cr_bulk_boxes_rollback.py
"""
import asyncio
import asyncpg
from decimal import Decimal
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service, box_service


def _item(art, num, nw):
    return schemas.CRBulkBoxItem(article_description=art, box_number=num, net_weight=Decimal(nw))


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        created = await create_service.create_cr(
            conn, "CFPL",
            schemas.CRCreate(company="CFPL",
                header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME"),
                lines=[schemas.CRLineCreate(material_type="RM", item_category="N", sub_category="S",
                        item_description="ALMOND W-320", uom="KG", qty="1", rate="1")]),
            "t@x.in")
        cr_id = created["rtv_id"]

        # first sync: 2 boxes inserted, 3-part box_ids
        r1 = await box_service.bulk_save_boxes(conn, "CFPL", cr_id,
            schemas.CRBulkBoxUpdateRequest(boxes=[
                _item("ALMOND W-320", 1, "25.0"), _item("ALMOND W-320", 2, "25.0")]))
        assert r1 == {"status": "synced", "rtv_id": cr_id, "inserted": 2,
                      "updated": 0, "unchanged": 0, "deleted": 0}, r1
        got = await query_service.get_cr(conn, "CFPL", cr_id)
        b1 = next(x for x in got["boxes"] if x["box_number"] == 1)
        assert b1["box_id"].count("-") == 2, b1["box_id"]   # three-part
        b1_id = b1["box_id"]

        # second sync: box1 kept+changed (update, box_id preserved), box2 dropped (delete), box3 new (insert)
        r2 = await box_service.bulk_save_boxes(conn, "CFPL", cr_id,
            schemas.CRBulkBoxUpdateRequest(boxes=[
                _item("ALMOND W-320", 1, "24.0"), _item("ALMOND W-320", 3, "25.0")]))
        assert r2["inserted"] == 1 and r2["updated"] == 1 and r2["deleted"] == 1, r2
        got2 = await query_service.get_cr(conn, "CFPL", cr_id)
        nums = sorted(x["box_number"] for x in got2["boxes"])
        assert nums == [1, 3], nums
        b1b = next(x for x in got2["boxes"] if x["box_number"] == 1)
        assert b1b["box_id"] == b1_id and b1b["net_weight"] == "24"   # preserved id, updated weight

        # status flip only from Approved/Submitted: Pending CR stays Pending
        assert got2["status"] == "Pending"
        await conn.execute(
            "UPDATE cfpl_customer_return_header SET status='Approved' WHERE rtv_id=$1", cr_id)
        await box_service.bulk_save_boxes(conn, "CFPL", cr_id,
            schemas.CRBulkBoxUpdateRequest(boxes=[_item("ALMOND W-320", 1, "24.0")]))
        st = await conn.fetchval(
            "SELECT status FROM cfpl_customer_return_header WHERE rtv_id=$1", cr_id)
        assert st == "Submitted", st
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_bulk_boxes_rollback.py`
Expected: FAIL — `AttributeError: module 'box_service' has no attribute 'bulk_save_boxes'`

- [ ] **Step 3: Append `bulk_save_boxes`**

Append to `app/modules/customer_returns/services/box_service.py`:

```python
async def bulk_save_boxes(conn, company: str, cr_id: str,
                          data: schemas.CRBulkBoxUpdateRequest,
                          notify_discrepancy: bool = True) -> dict:
    """State-aware full sync of the CR's box set: insert new, update existing
    (preserving box_id), delete boxes no longer present. Flips header status to
    'Submitted' ONLY from Approved/Submitted. `notify_discrepancy` is a reserved
    no-op (kept for signature parity). Cold-stock mirror is wired in Phase 4."""
    tables = cr_table_names(company)
    await _assert_cr_exists(conn, tables["header"], cr_id)

    # dedupe incoming by (article, box_number), keep last occurrence
    seen: dict = {}
    for b in data.boxes:
        seen[(b.article_description, b.box_number)] = b
    incoming = seen  # key -> item, insertion order preserved
    incoming_keys = set(incoming.keys())

    existing_rows = await conn.fetch(
        f"SELECT article_description, box_number, box_id FROM {tables['boxes']} WHERE rtv_id = $1",
        cr_id,
    )
    existing_keys = {(r["article_description"], r["box_number"]) for r in existing_rows}

    inserted = updated = deleted = 0
    async with conn.transaction():
        for (art, num), b in incoming.items():
            if (art, num) in existing_keys:
                await conn.execute(
                    f"""
                    UPDATE {tables['boxes']} SET
                        uom = COALESCE($4, uom),
                        conversion = COALESCE($5, conversion),
                        net_weight = COALESCE($6::numeric, net_weight),
                        gross_weight = COALESCE($7::numeric, gross_weight),
                        lot_number = COALESCE($8, lot_number),
                        item_mark = COALESCE($9, item_mark),
                        spl_remarks = COALESCE($10, spl_remarks),
                        vakkal = COALESCE($11, vakkal),
                        count = COALESCE($12::int, count),
                        updated_at = NOW()
                    WHERE rtv_id = $1 AND article_description = $2 AND box_number = $3
                    """,
                    cr_id, art, num, b.uom, b.conversion, b.net_weight, b.gross_weight,
                    b.lot_number, b.item_mark, b.spl_remarks, b.vakkal, b.count,
                )
                updated += 1
            else:
                box_id = f"{_base8()}-{num}-{inserted}"
                await conn.execute(
                    f"""
                    INSERT INTO {tables['boxes']}
                        (rtv_id, article_description, box_number, box_id, uom, conversion,
                         net_weight, gross_weight, lot_number, item_mark, spl_remarks, vakkal, count)
                    VALUES ($1,$2,$3,$4,$5,$6,
                            COALESCE($7::numeric, 0), COALESCE($8::numeric, 0),
                            $9,$10,$11,$12,$13)
                    """,
                    cr_id, art, num, box_id, b.uom, b.conversion, b.net_weight, b.gross_weight,
                    b.lot_number, b.item_mark, b.spl_remarks, b.vakkal, b.count,
                )
                inserted += 1

        for (art, num) in existing_keys - incoming_keys:
            await conn.execute(
                f"DELETE FROM {tables['boxes']} "
                "WHERE rtv_id = $1 AND article_description = $2 AND box_number = $3",
                cr_id, art, num,
            )
            deleted += 1

        await conn.execute(
            f"UPDATE {tables['header']} SET status = 'Submitted', updated_at = NOW() "
            "WHERE rtv_id = $1 AND status IN ('Approved', 'Submitted')",
            cr_id,
        )

    return {"status": "synced", "rtv_id": cr_id, "inserted": inserted,
            "updated": updated, "unchanged": 0, "deleted": deleted}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_bulk_boxes_rollback.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/services/box_service.py tests/services/test_cr_bulk_boxes_rollback.py
git commit -m "feat(customer-returns): bulk_save_boxes state sync"
```

---

### Task 4: `log_box_edits` (box-edit audit log)

**Files:**
- Modify: `app/modules/customer_returns/services/box_service.py`
- Test: `tests/services/test_cr_box_edit_log_rollback.py`

**Interfaces:**
- Produces: `box_service.log_box_edits(conn, payload: CRBoxEditLogRequest, email_id: str) -> dict` returning `{"status":"logged","entries": <n>}`. Writes to the global `box_edit_logs` table; `email_id` is the JWT actor (not `payload.email_id`).

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_box_edit_log_rollback.py`:

```python
"""Rollback integration test for box_service.log_box_edits. Run:
    PYTHONPATH=. python tests/services/test_cr_box_edit_log_rollback.py
"""
import asyncio
import asyncpg
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import box_service


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        payload = schemas.CRBoxEditLogRequest(
            email_id="ignored@spoof.in", box_id="50123456-1", rtv_id="CR-TESTLOG",
            changes=[
                schemas.CRBoxEditLogEntry(field_name="net_weight", old_value="25", new_value="24"),
                schemas.CRBoxEditLogEntry(field_name="lot_number", old_value="L1", new_value="L2"),
            ])
        res = await box_service.log_box_edits(conn, payload, email_id="real@candorfoods.in")
        assert res == {"status": "logged", "entries": 2}, res

        rows = await conn.fetch(
            "SELECT email_id, description, transaction_no, box_id, field_name, old_value, new_value "
            "FROM box_edit_logs WHERE transaction_no = $1 ORDER BY field_name", "CR-TESTLOG")
        assert len(rows) == 2
        nw = next(r for r in rows if r["field_name"] == "net_weight")
        assert nw["email_id"] == "real@candorfoods.in"          # JWT actor, not payload.email_id
        assert nw["transaction_no"] == "CR-TESTLOG" and nw["box_id"] == "50123456-1"
        assert nw["description"] == "Changed net_weight from '25' to '24'"
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_box_edit_log_rollback.py`
Expected: FAIL — `AttributeError: module 'box_service' has no attribute 'log_box_edits'`

- [ ] **Step 3: Append `log_box_edits`**

Add `from datetime import datetime, timezone` to the imports at the top of `box_service.py`, then append:

```python
async def log_box_edits(conn, payload: schemas.CRBoxEditLogRequest, email_id: str) -> dict:
    """Append one audit row per change to the global box_edit_logs table.
    `email_id` is the JWT actor (payload.email_id is ignored — hardening).
    No CR/box existence check (append-only log, matches source)."""
    edited_at = datetime.now(timezone.utc)  # one shared timestamp per call
    async with conn.transaction():
        for ch in payload.changes:
            description = f"Changed {ch.field_name} from '{ch.old_value}' to '{ch.new_value}'"
            await conn.execute(
                """
                INSERT INTO box_edit_logs
                    (email_id, description, transaction_no, box_id, field_name,
                     old_value, new_value, edited_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                email_id, description, payload.rtv_id, payload.box_id,
                ch.field_name, ch.old_value, ch.new_value, edited_at,
            )
    return {"status": "logged", "entries": len(payload.changes)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_box_edit_log_rollback.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/services/box_service.py tests/services/test_cr_box_edit_log_rollback.py
git commit -m "feat(customer-returns): log_box_edits audit trail"
```

---

### Task 5: `export_cr_records` + `get_edited_cells` (export data)

**Files:**
- Modify: `app/modules/customer_returns/services/query_service.py`
- Test: `tests/services/test_cr_export_rollback.py`

**Interfaces:**
- Consumes: `cr_table_names`, `_SORTABLE`, `_like_escape`, `_convert_date` (all Phase 1).
- Produces:
  - `EXPORT_COLUMNS: list[str]` (33 header names, canonical order).
  - `export_cr_records(conn, *, company, status=None, customer=None, factory_unit=None, from_date=None, to_date=None, sort_by="created_ts", sort_order="desc") -> list[dict]` — one dict per header⋈line⋈(matched box) row, keys == `EXPORT_COLUMNS`.
  - `get_edited_cells(conn, rtv_ids: list[str]) -> set[tuple[str, str]]` — `(box_id, field_name)` pairs from `box_edit_logs`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_export_rollback.py`:

```python
"""Rollback integration test for export data. Run:
    PYTHONPATH=. python tests/services/test_cr_export_rollback.py
"""
import asyncio
import asyncpg
from decimal import Decimal
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, box_service, query_service


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        created = await create_service.create_cr(
            conn, "CFPL",
            schemas.CRCreate(company="CFPL",
                header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ZZ_EXPORT_CO"),
                lines=[schemas.CRLineCreate(material_type="RM", item_category="N", sub_category="S",
                        item_description="ALMOND W-320", uom="KG", qty="3", rate="10")]),
            "t@x.in")
        cr_id = created["rtv_id"]
        r = await box_service.upsert_box(conn, "CFPL", cr_id, schemas.CRBoxUpsertRequest(
            article_description="ALMOND W-320", box_number=1,
            net_weight=Decimal("25.000"), gross_weight=Decimal("26.000"), lot_number="LOTX", count=40))
        box_id = r["box_id"]

        rows = await query_service.export_cr_records(conn, company="CFPL", customer="zz_export_co")
        assert rows, "expected at least one export row"
        row = rows[0]
        assert list(row.keys()) == query_service.EXPORT_COLUMNS       # exact 33-col contract
        assert row["RTV ID"] == cr_id and row["Customer"] == "ZZ_EXPORT_CO"
        assert row["Item Description"] == "ALMOND W-320" and row["Qty"] == 3.0
        assert row["Box ID"] == box_id and row["Box Net Weight"] == 25.0 and row["Box Count"] == 40

        # edited-cells lookup
        await box_service.log_box_edits(conn,
            schemas.CRBoxEditLogRequest(email_id="x", box_id=box_id, rtv_id=cr_id,
                changes=[schemas.CRBoxEditLogEntry(field_name="net_weight", old_value="25", new_value="24")]),
            email_id="e@e.in")
        edited = await query_service.get_edited_cells(conn, [cr_id])
        assert (box_id, "net_weight") in edited
        assert await query_service.get_edited_cells(conn, []) == set()
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_export_rollback.py`
Expected: FAIL — `AttributeError: module 'query_service' has no attribute 'export_cr_records'`

- [ ] **Step 3: Append export code to `query_service.py`**

Append to `app/modules/customer_returns/services/query_service.py`:

```python
# Canonical export column order (33). export_cr_records builds dicts with exactly
# these keys; export_xlsx uses this for the header row.
EXPORT_COLUMNS = [
    "RTV ID", "RTV Date", "Factory Unit", "Customer", "Invoice Number", "Challan No",
    "DN No", "Conversion", "Sales POC", "Business Head", "Remark", "Status",
    "Created By", "Created At",
    "Material Type", "Item Category", "Sub Category", "Item Description", "UOM",
    "Qty", "Rate", "Value", "Line Net Weight", "Line Carton Weight",
    "Box ID", "Box Article", "Box Number", "Box UOM", "Box Conversion",
    "Box Net Weight", "Box Gross Weight", "Box Lot Number", "Box Count",
]


def _export_row(r: dict) -> dict:
    """Flatten one joined header/line/box record into the 33-col export dict."""
    return {
        "RTV ID": r.get("rtv_id") or "",
        "RTV Date": str(r.get("rtv_date") or ""),
        "Factory Unit": r.get("factory_unit") or "",
        "Customer": r.get("customer") or "",
        "Invoice Number": r.get("invoice_number") or "",
        "Challan No": r.get("challan_no") or "",
        "DN No": r.get("dn_no") or "",
        "Conversion": str(r.get("conversion")) if r.get("conversion") is not None else "",
        "Sales POC": r.get("sales_poc") or "",
        "Business Head": r.get("business_head") or "",
        "Remark": r.get("remark") or "",
        "Status": r.get("status") or "",
        "Created By": r.get("created_by") or "",
        "Created At": str(r.get("created_ts") or ""),
        "Material Type": r.get("material_type") or "",
        "Item Category": r.get("item_category") or "",
        "Sub Category": r.get("sub_category") or "",
        "Item Description": r.get("item_description") or "",
        "UOM": r.get("uom") or "",
        "Qty": float(r["qty"]) if r.get("qty") is not None else "",
        "Rate": float(r["rate"]) if r.get("rate") is not None else "",
        "Value": float(r["value"]) if r.get("value") is not None else "",
        "Line Net Weight": float(r["line_net_weight"]) if r.get("line_net_weight") is not None else "",
        "Line Carton Weight": float(r["line_carton_weight"]) if r.get("line_carton_weight") is not None else "",
        "Box ID": r.get("box_id") or "",
        "Box Article": r.get("box_article") or "",
        "Box Number": r.get("box_number") if r.get("box_number") is not None else "",
        "Box UOM": r.get("box_uom") or "",
        "Box Conversion": r.get("box_conversion") or "",
        "Box Net Weight": float(r["box_net_weight"]) if r.get("box_net_weight") is not None else "",
        "Box Gross Weight": float(r["box_gross_weight"]) if r.get("box_gross_weight") is not None else "",
        "Box Lot Number": r.get("box_lot_number") or "",
        "Box Count": int(r["box_count"]) if r.get("box_count") is not None else "",
    }


async def export_cr_records(conn, *, company: str, status=None, customer=None,
                            factory_unit=None, from_date=None, to_date=None,
                            sort_by="created_ts", sort_order="desc") -> list:
    """Flattened header⋈line⋈box rows for Excel export. Boxes are scoped to their
    matching line (article_description = item_description) per the design; a box
    with no matching line does not appear."""
    tables = cr_table_names(company)
    clauses, args = ["1=1"], []
    if status:
        args.append(status); clauses.append(f"h.status = ${len(args)}")
    if factory_unit:
        args.append(factory_unit); clauses.append(f"h.factory_unit = ${len(args)}")
    if customer:
        args.append(f"%{_like_escape(customer)}%")
        clauses.append(f"h.customer ILIKE ${len(args)} ESCAPE '\\'")
    df = _convert_date(from_date)
    if df:
        args.append(df); clauses.append(f"h.rtv_date >= ${len(args)}")
    dt = _convert_date(to_date)
    if dt:
        args.append(dt); clauses.append(f"h.rtv_date < (${len(args)}::date + 1)")
    where = " AND ".join(clauses)

    col = _SORTABLE.get(sort_by, "created_ts")
    direction = "ASC" if str(sort_order).lower() == "asc" else "DESC"

    rows = await conn.fetch(
        f"""
        SELECT h.rtv_id, h.rtv_date, h.factory_unit, h.customer, h.invoice_number,
               h.challan_no, h.dn_no, h.conversion, h.sales_poc, h.business_head,
               h.remark, h.status, h.created_by, h.created_ts,
               l.material_type, l.item_category, l.sub_category, l.item_description, l.uom,
               l.qty, l.rate, l.value,
               l.net_weight AS line_net_weight, l.carton_weight AS line_carton_weight,
               b.box_id, b.article_description AS box_article, b.box_number,
               b.uom AS box_uom, b.conversion AS box_conversion,
               b.net_weight AS box_net_weight, b.gross_weight AS box_gross_weight,
               b.lot_number AS box_lot_number, b.count AS box_count
          FROM {tables['header']} h
          LEFT JOIN {tables['lines']} l ON l.rtv_id = h.rtv_id
          LEFT JOIN {tables['boxes']} b
                 ON b.rtv_id = h.rtv_id AND b.article_description = l.item_description
         WHERE {where}
         ORDER BY h.{col} {direction}, l.item_description ASC, b.box_number ASC
        """,
        *args,
    )
    return [_export_row(dict(r)) for r in rows]


async def get_edited_cells(conn, rtv_ids: list) -> set:
    """(box_id, field_name) pairs edited for the given CRs, for export highlighting."""
    if not rtv_ids:
        return set()
    rows = await conn.fetch(
        "SELECT box_id, field_name FROM box_edit_logs WHERE transaction_no = ANY($1)",
        rtv_ids,
    )
    return {(r["box_id"], r["field_name"]) for r in rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_export_rollback.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/services/query_service.py tests/services/test_cr_export_rollback.py
git commit -m "feat(customer-returns): export_cr_records + edited-cell lookup"
```

---

### Task 6: `export_xlsx.py` — pure openpyxl workbook builder

**Files:**
- Create: `app/modules/customer_returns/services/export_xlsx.py`
- Test: `tests/services/test_cr_export_xlsx.py`

**Interfaces:**
- Consumes: `query_service.EXPORT_COLUMNS`.
- Produces: `FIELD_TO_HEADER: dict`; `build_export_workbook(rows: list[dict], edited_cells: set[tuple[str,str]]) -> BytesIO` — a saved `.xlsx` stream (header row = `EXPORT_COLUMNS`; the 4 box cells highlighted where `(row["Box ID"], field_name) in edited_cells`).

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_export_xlsx.py`:

```python
"""Pure test: build the export workbook and read it back. Run:
    PYTHONPATH=. python tests/services/test_cr_export_xlsx.py
"""
from openpyxl import load_workbook
from app.modules.customer_returns.services import export_xlsx
from app.modules.customer_returns.services.query_service import EXPORT_COLUMNS


def _row(**over):
    base = {c: "" for c in EXPORT_COLUMNS}
    base.update({"RTV ID": "CR-1", "Box ID": "50123456-1",
                 "Box Net Weight": 25.0, "Box Lot Number": "L1"})
    base.update(over)
    return base


def main() -> None:
    rows = [_row()]
    edited = {("50123456-1", "net_weight")}   # highlight Box Net Weight only
    buf = export_xlsx.build_export_workbook(rows, edited)
    wb = load_workbook(buf)
    ws = wb.active
    assert ws.title == "Customer Returns"
    header = [c.value for c in ws[1]]
    assert header == EXPORT_COLUMNS and len(header) == 33

    nw_col = EXPORT_COLUMNS.index("Box Net Weight") + 1
    lot_col = EXPORT_COLUMNS.index("Box Lot Number") + 1
    nw_cell = ws.cell(row=2, column=nw_col)
    lot_cell = ws.cell(row=2, column=lot_col)
    assert nw_cell.value == 25.0
    # highlighted cell has the light-red fill; unedited box field does not
    assert nw_cell.fill.start_color.rgb.endswith("FEE2E2"), nw_cell.fill.start_color.rgb
    assert not (lot_cell.fill.fill_type == "solid" and lot_cell.fill.start_color.rgb.endswith("FEE2E2"))

    # empty export still writes a valid header-only sheet
    buf2 = export_xlsx.build_export_workbook([], set())
    ws2 = load_workbook(buf2).active
    assert [c.value for c in ws2[1]] == EXPORT_COLUMNS
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_export_xlsx.py`
Expected: FAIL — `ModuleNotFoundError: ... export_xlsx`

- [ ] **Step 3: Write `export_xlsx.py`**

Create `app/modules/customer_returns/services/export_xlsx.py`:

```python
"""Pure openpyxl builder for the customer-returns Excel export (no DB access).
Header row + styling + edited-cell highlighting; returns a BytesIO stream."""
from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.modules.customer_returns.services.query_service import EXPORT_COLUMNS

# DB field_name -> export header for the 4 highlightable box columns.
FIELD_TO_HEADER = {
    "net_weight": "Box Net Weight",
    "gross_weight": "Box Gross Weight",
    "lot_number": "Box Lot Number",
    "count": "Box Count",
}

_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_HEADER_FILL = PatternFill(start_color="29417A", end_color="29417A", fill_type="solid")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center")
_EDITED_FILL = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
_THIN = Side(style="thin")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def build_export_workbook(rows: list, edited_cells: set) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Customer Returns"

    # header row
    for col_idx, name in enumerate(EXPORT_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=name)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN
        cell.border = _BORDER

    # data rows
    for row_idx, row in enumerate(rows, start=2):
        box_id = row.get("Box ID") or ""
        for col_idx, name in enumerate(EXPORT_COLUMNS, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=row.get(name, ""))
            cell.border = _BORDER
        if box_id:
            for field_name, header in FIELD_TO_HEADER.items():
                if (box_id, field_name) in edited_cells:
                    ws.cell(row=row_idx, column=EXPORT_COLUMNS.index(header) + 1).fill = _EDITED_FILL

    # column widths
    for col_idx, name in enumerate(EXPORT_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(len(name) + 4, 14)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_export_xlsx.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/services/export_xlsx.py tests/services/test_cr_export_xlsx.py
git commit -m "feat(customer-returns): openpyxl export workbook builder"
```

---

### Task 7: Router — 4 endpoints + literal-route ordering

**Files:**
- Modify: `app/modules/customer_returns/router.py`
- Test: `tests/services/test_cr_box_routes.py`

**Interfaces:**
- Consumes: `box_service`, `query_service` (`export_cr_records`, `get_edited_cells`), `export_xlsx`, schemas from Task 1.
- Produces (under `/api/v1/customer-returns`): `GET /export`, `POST /box-edit-log` (declared **before** the `/{company}` routes), `PUT /{company}/{cr_id}/box`, `PUT /{company}/{cr_id}/boxes`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_box_routes.py`:

```python
"""Verifies the Phase-2 routes are registered AND /export precedes /{company}. Run:
    PYTHONPATH=. python tests/services/test_cr_box_routes.py
"""
from app.main import app

PREFIX = "/api/v1/customer-returns"


def _routes():
    return [(m, r.path) for r in app.routes
            for m in (getattr(r, "methods", set()) or set())]


def main() -> None:
    routes = _routes()
    present = set(routes)
    for m, p in [
        ("GET", f"{PREFIX}/export"),
        ("POST", f"{PREFIX}/box-edit-log"),
        ("PUT", f"{PREFIX}/{{company}}/{{cr_id}}/box"),
        ("PUT", f"{PREFIX}/{{company}}/{{cr_id}}/boxes"),
    ]:
        assert (m, p) in present, f"missing route {m} {p}"

    # /export must be declared before GET /{company} (FastAPI matches in order)
    ordered = [p for (m, p) in routes if m == "GET"]
    assert ordered.index(f"{PREFIX}/export") < ordered.index(f"{PREFIX}/{{company}}"), \
        "GET /export must be declared before GET /{company}"

    # POST /box-edit-log before POST /{company}
    posts = [p for (m, p) in routes if m == "POST"]
    assert posts.index(f"{PREFIX}/box-edit-log") < posts.index(f"{PREFIX}/{{company}}"), \
        "POST /box-edit-log must be declared before POST /{company}"
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_box_routes.py`
Expected: FAIL — `AssertionError: missing route GET /api/v1/customer-returns/export`

- [ ] **Step 3: Restructure imports + add the literal routes at the top**

In `app/modules/customer_returns/router.py`, update the imports block to:

```python
from __future__ import annotations

from io import BytesIO
from typing import Optional
from zoneinfo import ZoneInfo
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import (
    create_service, query_service, box_service, export_xlsx,
)

router = APIRouter(prefix="/api/v1/customer-returns", tags=["Customer Returns"])
_IST = ZoneInfo("Asia/Kolkata")
```

Then, immediately AFTER the `router = APIRouter(...)` line and the existing `_actor` helper, and **BEFORE** the existing `@router.post("/{company}", ...)` handler, insert the two literal routes:

```python
@router.get("/export")
async def export_customer_returns(
    request: Request,
    company: str = Query(..., description="CFPL or CDPL"),
    status: Optional[str] = Query(None),
    customer: Optional[str] = Query(None),
    factory_unit: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    to_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    sort_by: str = Query("created_ts"),
    sort_order: str = Query("desc"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await query_service.export_cr_records(
            conn, company=company, status=status, customer=customer,
            factory_unit=factory_unit, from_date=from_date, to_date=to_date,
            sort_by=sort_by, sort_order=sort_order,
        )
        rtv_ids = list({r["RTV ID"] for r in rows if r["RTV ID"]})
        edited = await query_service.get_edited_cells(conn, rtv_ids)
    buf: BytesIO = export_xlsx.build_export_workbook(rows, edited)
    stamp = datetime.now(_IST).strftime("%Y%m%d")
    filename = f"customer_returns_{company}_{stamp}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/box-edit-log", response_model=schemas.CRBoxEditLogResponse)
async def log_customer_return_box_edits(
    body: schemas.CRBoxEditLogRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.log_box_edits(conn, body, email_id=_actor(user))
```

- [ ] **Step 4: Add the box-edit-log response model**

`CRBoxEditLogResponse` is referenced above; add it to `app/modules/customer_returns/schemas.py` (append near the other box models):

```python
class CRBoxEditLogResponse(BaseModel):
    status: str
    entries: int
```

- [ ] **Step 5: Add the two box endpoints after the existing `/{company}/{cr_id}` routes**

At the END of `app/modules/customer_returns/router.py` (after the existing `delete_customer_return` handler), append:

```python
@router.put("/{company}/{cr_id}/box", response_model=schemas.CRBoxUpsertResponse)
async def upsert_customer_return_box(
    company: str,
    cr_id: str,
    body: schemas.CRBoxUpsertRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.upsert_box(conn, company, cr_id, body)


@router.put("/{company}/{cr_id}/boxes", response_model=schemas.CRBulkBoxUpdateResponse)
async def bulk_save_customer_return_boxes(
    company: str,
    cr_id: str,
    body: schemas.CRBulkBoxUpdateRequest,
    request: Request,
    notify_discrepancy: bool = Query(True),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await box_service.bulk_save_boxes(
            conn, company, cr_id, body, notify_discrepancy=notify_discrepancy)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_box_routes.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 7: Commit**

```bash
git add app/modules/customer_returns/router.py app/modules/customer_returns/schemas.py tests/services/test_cr_box_routes.py
git commit -m "feat(customer-returns): box + export endpoints with literal-route ordering"
```

---

## Phase 2 Done — Verification

Run the Phase-2 test set plus the Phase-1 suite (all DB tests roll back; safe against the live DB):

```bash
# Phase 2
PYTHONPATH=. python tests/services/test_cr_box_schemas.py
PYTHONPATH=. python tests/services/test_cr_upsert_box_rollback.py
PYTHONPATH=. python tests/services/test_cr_bulk_boxes_rollback.py
PYTHONPATH=. python tests/services/test_cr_box_edit_log_rollback.py
PYTHONPATH=. python tests/services/test_cr_export_rollback.py
PYTHONPATH=. python tests/services/test_cr_export_xlsx.py
PYTHONPATH=. python tests/services/test_cr_box_routes.py
# Phase 1 regression
PYTHONPATH=. python tests/services/test_cr_routes.py
PYTHONPATH=. python tests/services/test_cr_create_rollback.py
PYTHONPATH=. python tests/services/test_cr_list_rollback.py
PYTHONPATH=. python tests/services/test_cr_update_delete_rollback.py
```

Each must print `ASSERTIONS PASSED`. Phase-2 deliverable: print boxes (single + bulk with delete-diff and status flip), audit box edits, and download a styled `.xlsx` export with edited cells highlighted — all JWT-gated.

**Deferred / next:** cold-stock mirror is NOT called by `bulk_save_boxes` yet (Phase 4 wires it). `cr_box_summary_and_short` and the save/approval/notification flows are Phase 3. A box with no matching line does not appear in the export (documented caveat).
