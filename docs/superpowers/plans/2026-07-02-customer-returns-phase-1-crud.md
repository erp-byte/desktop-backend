# Customer-Returns Phase 1 (Data + Core CRUD) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the `customer_returns` module in `linux_replica/server_replica` with its DB tables and full header+lines CRUD (create / list / get / update-header / update-lines / delete), JWT-gated, end-to-end.

**Architecture:** Thin FastAPI router → fat async service, on raw `asyncpg` (no SQLAlchemy), following the `packing` module. Per-company `cfpl_/cdpl_` tables named `*_customer_return_*`; the header PK is the `rtv_id` string (`CR-YYYYMMDDHHMMSS`), lines/boxes link by `rtv_id`, no sequential integer id. Boxes/approval/email/WhatsApp/events/export/cold-mirror are later phases.

**Tech Stack:** Python 3.11, FastAPI 0.135, asyncpg 0.31, Pydantic 2.12, PostgreSQL. Tests are standalone scripts (repo has no pytest) run via `python tests/...`.

**Spec:** `docs/superpowers/specs/2026-07-02-customer-returns-port-design.md` (§4 data model, §6 schemas, §7 services, §8 endpoints, §9 auth).

## Global Constraints

- **Runtime:** build/run against the project `.venv` (Python **3.11**, not 3.14). POSIX: `.venv/bin/python`; Windows: `.venv/Scripts/python.exe`.
- **DB access:** `asyncpg` only. Positional `$1, $2, …` params. Dynamic WHERE interpolates only the `$N` index, never a value. **No SQLAlchemy.**
- **Service signature:** every service fn is `async def fn(conn, ...)`, `conn` first. Transactions via `async with conn.transaction():`; **no explicit commit**; read-back after the transaction.
- **Error envelope:** `raise HTTPException(status, detail={"error": <machine_code>, "message": <human>, "details": {...}})` — `error`/`message` keys, never `code`.
- **Identity from JWT:** `created_by`/`deleted_by`/etc. come from `user.email` (or `user.full_name or user.email or str(user.user_id)`), never from request params.
- **Numeric response fields are strings** (match the production API contract).
- **Company** is `CFPL`/`CDPL`, mapped to the `cfpl`/`cdpl` prefix through a whitelist helper — never f-string raw input into a table name.
- **Tables** are `{prefix}_customer_return_header/_lines/_boxes` (+ global `box_edit_logs`); PKs are natural keys (§4). No sequential `id`.
- **Migrations** are idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`) and MUST be appended to the `SQL_FILES` list in `scripts/migrate.py` or they never run.
- **Out of Phase 1:** boxes/print, bulk-save, approval, magic-link, notifications, realtime events, Excel export, cold-stock mirror.

**Test run convention** (from `tests/services/test_create_transfer_rollback.py`): DB tests connect to `Settings().DATABASE_URL`, do all writes **inside a transaction that is always rolled back** (safe even against prod), and `print("ASSERTIONS PASSED")` on success. Run with `PYTHONPATH=. python tests/services/<file>.py`.

---

### Task 1: DB migration `070_customer_returns.sql` + runner wiring

**Files:**
- Create: `app/db/070_customer_returns.sql`
- Modify: `scripts/migrate.py` (append to `SQL_FILES`, after `069_packing_details.sql`)
- Test: `tests/services/test_cr_migration.py`

**Interfaces:**
- Produces (physical tables): `cfpl_customer_return_header`, `cfpl_customer_return_lines`, `cfpl_customer_return_boxes`, and the `cdpl_` equivalents; global `box_edit_logs`. Header PK `rtv_id`; line PK `(rtv_id, item_description)`; box PK `(rtv_id, article_description, box_number)`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_migration.py`:

```python
"""Verifies migration 070 created the customer-returns tables with the expected
natural-key primary keys. Read-only; safe against any DB. Run:

    PYTHONPATH=. python tests/services/test_cr_migration.py
"""
import asyncio
import asyncpg
from app.config import Settings

EXPECTED_PK = {
    "cfpl_customer_return_header": ["rtv_id"],
    "cdpl_customer_return_header": ["rtv_id"],
    "cfpl_customer_return_lines": ["rtv_id", "item_description"],
    "cdpl_customer_return_lines": ["rtv_id", "item_description"],
    "cfpl_customer_return_boxes": ["rtv_id", "article_description", "box_number"],
    "cdpl_customer_return_boxes": ["rtv_id", "article_description", "box_number"],
}


async def _pk_cols(conn, table: str) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT a.attname
          FROM pg_index i
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
         WHERE i.indrelid = to_regclass($1) AND i.indisprimary
         ORDER BY array_position(i.indkey, a.attnum)
        """,
        table,
    )
    return [r["attname"] for r in rows]


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    try:
        for table, expected in EXPECTED_PK.items():
            assert await conn.fetchval("SELECT to_regclass($1)", table) is not None, \
                f"missing table {table} — run scripts/migrate.py"
            got = await _pk_cols(conn, table)
            assert got == expected, f"{table} PK expected {expected}, got {got}"
        assert await conn.fetchval("SELECT to_regclass('box_edit_logs')") is not None, \
            "missing box_edit_logs"
        print("ASSERTIONS PASSED")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_migration.py`
Expected: FAIL — `AssertionError: missing table cfpl_customer_return_header — run scripts/migrate.py`

- [ ] **Step 3: Write the migration**

Create `app/db/070_customer_returns.sql`:

```sql
-- 070_customer_returns.sql
-- Customer-Returns module (source "RTV" = customer returns, CR- ids).
-- Per-company header/lines/boxes (cfpl_/cdpl_) + a GLOBAL box_edit_logs audit
-- table. Natural keys: header PK = rtv_id ('CR-YYYYMMDDHHMMSS'); lines keyed by
-- (rtv_id, item_description); boxes keyed by (rtv_id, article_description,
-- box_number). No sequential id. Additive + idempotent (safe to re-run).

-- ── CFPL ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cfpl_customer_return_header (
    rtv_id           TEXT PRIMARY KEY,               -- 'CR-YYYYMMDDHHMMSS'
    rtv_date         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    factory_unit     TEXT NOT NULL,
    customer         TEXT NOT NULL,
    invoice_number   TEXT,
    challan_no       TEXT,
    dn_no            TEXT,
    conversion       DOUBLE PRECISION DEFAULT 0,
    sales_poc        TEXT,
    sales_poc_email  TEXT,
    business_head    TEXT,
    remark           TEXT,
    vehicle_number   TEXT,
    transporter_name TEXT,
    driver_name      TEXT,
    inward_manager   TEXT,
    status           TEXT NOT NULL DEFAULT 'Pending', -- Pending/Submitted/Approved/Rejected/On Hold
    created_by       TEXT,
    created_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cfpl_cr_header_status ON cfpl_customer_return_header(status);

CREATE TABLE IF NOT EXISTS cfpl_customer_return_lines (
    rtv_id           TEXT NOT NULL REFERENCES cfpl_customer_return_header(rtv_id) ON DELETE CASCADE,
    item_description TEXT NOT NULL,
    material_type    TEXT NOT NULL,
    item_category    TEXT NOT NULL,
    sub_category     TEXT NOT NULL,
    uom              TEXT NOT NULL,
    qty              INTEGER NOT NULL DEFAULT 0,
    rate             DOUBLE PRECISION NOT NULL DEFAULT 0,
    value            DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_weight       DOUBLE PRECISION NOT NULL DEFAULT 0,
    carton_weight    DOUBLE PRECISION NOT NULL DEFAULT 0,
    lot_number       TEXT,
    item_mark        TEXT,
    spl_remarks      TEXT,
    vakkal           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ,
    PRIMARY KEY (rtv_id, item_description)
);
CREATE INDEX IF NOT EXISTS idx_cfpl_cr_lines_rtv ON cfpl_customer_return_lines(rtv_id);

CREATE TABLE IF NOT EXISTS cfpl_customer_return_boxes (
    rtv_id              TEXT NOT NULL REFERENCES cfpl_customer_return_header(rtv_id) ON DELETE CASCADE,
    article_description TEXT NOT NULL,
    box_number          INTEGER NOT NULL,
    box_id              TEXT,                          -- NULL until Print
    uom                 TEXT,
    conversion          TEXT,
    lot_number          TEXT,
    item_mark           TEXT,
    spl_remarks         TEXT,
    vakkal              TEXT,
    net_weight          NUMERIC(18,3) NOT NULL DEFAULT 0,
    gross_weight        NUMERIC(18,3) NOT NULL DEFAULT 0,
    count               INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ,
    PRIMARY KEY (rtv_id, article_description, box_number)
);
CREATE INDEX IF NOT EXISTS idx_cfpl_cr_boxes_rtv ON cfpl_customer_return_boxes(rtv_id);

-- ── CDPL (identical shape) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cdpl_customer_return_header (
    rtv_id           TEXT PRIMARY KEY,
    rtv_date         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    factory_unit     TEXT NOT NULL,
    customer         TEXT NOT NULL,
    invoice_number   TEXT,
    challan_no       TEXT,
    dn_no            TEXT,
    conversion       DOUBLE PRECISION DEFAULT 0,
    sales_poc        TEXT,
    sales_poc_email  TEXT,
    business_head    TEXT,
    remark           TEXT,
    vehicle_number   TEXT,
    transporter_name TEXT,
    driver_name      TEXT,
    inward_manager   TEXT,
    status           TEXT NOT NULL DEFAULT 'Pending',
    created_by       TEXT,
    created_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cdpl_cr_header_status ON cdpl_customer_return_header(status);

CREATE TABLE IF NOT EXISTS cdpl_customer_return_lines (
    rtv_id           TEXT NOT NULL REFERENCES cdpl_customer_return_header(rtv_id) ON DELETE CASCADE,
    item_description TEXT NOT NULL,
    material_type    TEXT NOT NULL,
    item_category    TEXT NOT NULL,
    sub_category     TEXT NOT NULL,
    uom              TEXT NOT NULL,
    qty              INTEGER NOT NULL DEFAULT 0,
    rate             DOUBLE PRECISION NOT NULL DEFAULT 0,
    value            DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_weight       DOUBLE PRECISION NOT NULL DEFAULT 0,
    carton_weight    DOUBLE PRECISION NOT NULL DEFAULT 0,
    lot_number       TEXT,
    item_mark        TEXT,
    spl_remarks      TEXT,
    vakkal           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ,
    PRIMARY KEY (rtv_id, item_description)
);
CREATE INDEX IF NOT EXISTS idx_cdpl_cr_lines_rtv ON cdpl_customer_return_lines(rtv_id);

CREATE TABLE IF NOT EXISTS cdpl_customer_return_boxes (
    rtv_id              TEXT NOT NULL REFERENCES cdpl_customer_return_header(rtv_id) ON DELETE CASCADE,
    article_description TEXT NOT NULL,
    box_number          INTEGER NOT NULL,
    box_id              TEXT,
    uom                 TEXT,
    conversion          TEXT,
    lot_number          TEXT,
    item_mark           TEXT,
    spl_remarks         TEXT,
    vakkal              TEXT,
    net_weight          NUMERIC(18,3) NOT NULL DEFAULT 0,
    gross_weight        NUMERIC(18,3) NOT NULL DEFAULT 0,
    count               INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ,
    PRIMARY KEY (rtv_id, article_description, box_number)
);
CREATE INDEX IF NOT EXISTS idx_cdpl_cr_boxes_rtv ON cdpl_customer_return_boxes(rtv_id);

-- ── Global box-edit audit log (append-only, no surrogate PK) ─────────────
CREATE TABLE IF NOT EXISTS box_edit_logs (
    email_id       TEXT,
    description    TEXT,
    transaction_no TEXT,   -- the rtv_id string
    box_id         TEXT,
    field_name     TEXT,
    old_value      TEXT,
    new_value      TEXT,
    edited_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_box_edit_logs_box ON box_edit_logs(box_id, field_name);
```

- [ ] **Step 4: Wire the migration into the runner**

In `scripts/migrate.py`, find the last entry of the `SQL_FILES` list (the highest-numbered `DB_DIR / "069_packing_details.sql",` line) and add immediately after it:

```python
    # 070 creates the Customer-Returns module tables: per-company
    # cfpl_/cdpl_customer_return_header/_lines/_boxes (natural keys, rtv_id PK)
    # plus the global box_edit_logs audit table. Idempotent CREATE IF NOT EXISTS.
    DB_DIR / "070_customer_returns.sql",
```

- [ ] **Step 5: Apply the migration**

Run: `PYTHONPATH=. python scripts/migrate.py`
Expected: log lines ending with the 070 file applied, no errors.

- [ ] **Step 6: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_migration.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 7: Commit**

```bash
git add app/db/070_customer_returns.sql scripts/migrate.py tests/services/test_cr_migration.py
git commit -m "feat(customer-returns): add 070 migration (header/lines/boxes + box_edit_logs)"
```

---

### Task 2: Module scaffold + company→table whitelist (`tables.py`)

**Files:**
- Create: `app/modules/customer_returns/__init__.py`, `app/modules/customer_returns/services/__init__.py`, `app/modules/customer_returns/tables.py`
- Test: `tests/services/test_cr_tables.py`

**Interfaces:**
- Produces: `cr_table_names(company: str) -> dict` returning `{"header","lines","boxes"}` → physical table names; raises `HTTPException(400, error="invalid_company")` for anything but `CFPL`/`CDPL` (case-insensitive).

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_tables.py`:

```python
"""Pure-logic test for the company->table whitelist. Run:
    PYTHONPATH=. python tests/services/test_cr_tables.py
"""
from fastapi import HTTPException
from app.modules.customer_returns.tables import cr_table_names


def main() -> None:
    assert cr_table_names("CFPL") == {
        "header": "cfpl_customer_return_header",
        "lines": "cfpl_customer_return_lines",
        "boxes": "cfpl_customer_return_boxes",
    }
    assert cr_table_names("cdpl")["header"] == "cdpl_customer_return_header"  # case-insensitive
    for bad in ("", None, "XYZ", "cfpl; DROP TABLE"):
        try:
            cr_table_names(bad)
            raise AssertionError(f"expected 400 for {bad!r}")
        except HTTPException as e:
            assert e.status_code == 400 and e.detail["error"] == "invalid_company"
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_tables.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.modules.customer_returns'`

- [ ] **Step 3: Create the package files**

Create `app/modules/customer_returns/__init__.py`:

```python
"""Customer-Returns module (`/api/v1/customer-returns`).

Ported from the source `ims_service` RTV module (customer returns, CR- ids).
Per-company cfpl_/cdpl_ tables; header PK is the rtv_id string; boxes/approval/
notifications/export/cold-mirror arrive in later phases.
"""
```

Create `app/modules/customer_returns/services/__init__.py`:

```python
"""Customer-Returns service layer (async asyncpg)."""
```

Create `app/modules/customer_returns/tables.py`:

```python
"""Company -> physical table-name resolution for the customer-returns module.

The company string is whitelisted to a fixed prefix; table names are never
f-strings of raw input (SQL-injection guard, mirrors transfer/stock_service).
"""
from __future__ import annotations

from fastapi import HTTPException

_PREFIX = {"CFPL": "cfpl", "CDPL": "cdpl"}


def cr_table_names(company: str) -> dict:
    prefix = _PREFIX.get((company or "").strip().upper())
    if not prefix:
        raise HTTPException(
            400,
            detail={
                "error": "invalid_company",
                "message": "company must be CFPL or CDPL",
                "details": {"company": company},
            },
        )
    return {
        "header": f"{prefix}_customer_return_header",
        "lines": f"{prefix}_customer_return_lines",
        "boxes": f"{prefix}_customer_return_boxes",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_tables.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/__init__.py app/modules/customer_returns/services/__init__.py app/modules/customer_returns/tables.py tests/services/test_cr_tables.py
git commit -m "feat(customer-returns): module scaffold + company->table whitelist"
```

---

### Task 3: Pydantic schemas (`schemas.py`)

**Files:**
- Create: `app/modules/customer_returns/schemas.py`
- Test: `tests/services/test_cr_schemas.py`

**Interfaces:**
- Produces: `Company` (Literal), `CRHeaderCreate`, `CRLineCreate` (uppercases `material_type`/`uom`), `CRCreate` (`lines` min_length=1), `CRHeaderUpdate` (all optional), `CRLinesUpdateRequest`, and response models `CRLineResponse`, `CRBoxResponse`, `CRHeaderResponse`, `CRWithDetails`, `CRListItem`, `CRListResponse`, `CRDeleteResponse`, `CRLinesUpdateResponse`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_schemas.py`:

```python
"""Pure-logic tests for customer-returns schemas. Run:
    PYTHONPATH=. python tests/services/test_cr_schemas.py
"""
from pydantic import ValidationError
from app.modules.customer_returns import schemas


def main() -> None:
    # material_type/uom auto-uppercase; numeric string defaults present.
    line = schemas.CRLineCreate(
        material_type="rm", item_category="Nuts", sub_category="Almond",
        item_description="ALMOND W-320", uom="kg",
    )
    assert line.material_type == "RM" and line.uom == "KG"
    assert line.qty == "0" and line.value == "0" and line.net_weight == "0"

    # CRCreate requires >=1 line.
    try:
        schemas.CRCreate(company="CFPL",
                         header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME"),
                         lines=[])
        raise AssertionError("expected min_length validation error")
    except ValidationError:
        pass

    # Company literal rejects junk.
    try:
        schemas.CRCreate(company="XYZ",
                         header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME"),
                         lines=[line])
        raise AssertionError("expected company literal error")
    except ValidationError:
        pass

    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_schemas.py`
Expected: FAIL — `ModuleNotFoundError: ... customer_returns.schemas`

- [ ] **Step 3: Write the schemas**

Create `app/modules/customer_returns/schemas.py`:

```python
"""Pydantic request/response models for /api/v1/customer-returns.

Field names mirror the source RTV contract verbatim (FE compatibility);
numeric fields are kept as strings in responses to match the production API.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Company = Literal["CFPL", "CDPL"]


# ── requests ────────────────────────────────────────────────────────────
class CRHeaderCreate(BaseModel):
    factory_unit: str
    customer: str
    invoice_number: Optional[str] = None
    challan_no: Optional[str] = None
    dn_no: Optional[str] = None
    conversion: Optional[str] = "0"
    sales_poc: Optional[str] = None
    sales_poc_email: Optional[str] = None
    business_head: Optional[str] = None
    remark: Optional[str] = None
    vehicle_number: Optional[str] = None
    transporter_name: Optional[str] = None
    driver_name: Optional[str] = None
    inward_manager: Optional[str] = None


class CRLineCreate(BaseModel):
    material_type: str
    item_category: str
    sub_category: str
    item_description: str
    uom: str
    qty: str = "0"
    rate: str = "0"
    value: str = "0"
    net_weight: Optional[str] = "0"
    carton_weight: Optional[str] = "0"
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None

    @field_validator("material_type", "uom")
    @classmethod
    def uppercase_codes(cls, v: str) -> str:
        return v.upper() if v else v


class CRCreate(BaseModel):
    company: Company
    header: CRHeaderCreate
    lines: List[CRLineCreate] = Field(..., min_length=1)


class CRHeaderUpdate(BaseModel):
    factory_unit: Optional[str] = None
    customer: Optional[str] = None
    invoice_number: Optional[str] = None
    challan_no: Optional[str] = None
    dn_no: Optional[str] = None
    conversion: Optional[str] = None
    sales_poc: Optional[str] = None
    sales_poc_email: Optional[str] = None
    business_head: Optional[str] = None
    remark: Optional[str] = None
    status: Optional[str] = None
    vehicle_number: Optional[str] = None
    transporter_name: Optional[str] = None
    driver_name: Optional[str] = None
    inward_manager: Optional[str] = None


class CRLinesUpdateRequest(BaseModel):
    lines: List[CRLineCreate] = Field(..., min_length=1)


# ── responses ───────────────────────────────────────────────────────────
class CRLineResponse(BaseModel):
    rtv_id: str
    item_description: str
    material_type: str
    item_category: str
    sub_category: str
    uom: str
    qty: str
    rate: str
    value: str
    net_weight: str
    carton_weight: str
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CRBoxResponse(BaseModel):
    rtv_id: str
    article_description: str
    box_number: int
    box_id: Optional[str] = None
    uom: Optional[str] = None
    conversion: Optional[str] = None
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None
    net_weight: str
    gross_weight: str
    count: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CRHeaderResponse(BaseModel):
    rtv_id: str
    rtv_date: Optional[datetime] = None
    factory_unit: str
    customer: str
    invoice_number: Optional[str] = None
    challan_no: Optional[str] = None
    dn_no: Optional[str] = None
    conversion: Optional[str] = None
    sales_poc: Optional[str] = None
    sales_poc_email: Optional[str] = None
    business_head: Optional[str] = None
    remark: Optional[str] = None
    vehicle_number: Optional[str] = None
    transporter_name: Optional[str] = None
    driver_name: Optional[str] = None
    inward_manager: Optional[str] = None
    status: str
    created_by: Optional[str] = None
    created_ts: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CRWithDetails(CRHeaderResponse):
    lines: List[CRLineResponse] = []
    boxes: List[CRBoxResponse] = []


class CRListItem(CRHeaderResponse):
    items_count: int = 0
    boxes_count: int = 0
    total_qty: int = 0
    total_net_weight: float = 0


class CRListResponse(BaseModel):
    records: List[CRListItem] = []
    total: int = 0
    page: int = 1
    per_page: int = 10
    total_pages: int = 0


class CRDeleteResponse(BaseModel):
    success: bool
    message: str
    rtv_id: Optional[str] = None
    lines_count: int = 0
    boxes_count: int = 0


class CRLinesUpdateResponse(BaseModel):
    status: str
    rtv_id: str
    lines_count: int
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_schemas.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/schemas.py tests/services/test_cr_schemas.py
git commit -m "feat(customer-returns): pydantic schemas (crud subset)"
```

---

### Task 4: Query-service helpers, mappers & column constants

**Files:**
- Create: `app/modules/customer_returns/services/query_service.py`
- Test: `tests/services/test_cr_helpers.py`

**Interfaces:**
- Produces: constants `HEADER_COLS`, `LINE_COLS`, `BOX_COLS` (SELECT column strings). Pure fns `_to_float(v)->float|None`, `_line_value(qty:int, rate:float, raw)->float`, `_num_str(v)->str`, `_convert_date(s:str|None)->date|None`, mappers `_map_header_row(dict)->dict`, `_map_line_row(dict)->dict`, `_map_box_row(dict)->dict`. (Async `get_cr`/`list_crs`/`_fetch_*` added in Tasks 5–6.)

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_helpers.py`:

```python
"""Pure-logic tests for query_service helpers/mappers. Run:
    PYTHONPATH=. python tests/services/test_cr_helpers.py
"""
from datetime import date
from fastapi import HTTPException
from app.modules.customer_returns.services import query_service as q


def main() -> None:
    assert q._to_float("3.5") == 3.5
    assert q._to_float(None) is None and q._to_float("x") is None

    # value = qty*rate unless a positive value is supplied.
    assert q._line_value(4, 10.0, "0") == 40.0
    assert q._line_value(4, 10.0, "") == 40.0
    assert q._line_value(4, 10.0, "55") == 55.0

    assert q._convert_date("09-06-2026") == date(2026, 6, 9)
    assert q._convert_date(None) is None
    try:
        q._convert_date("2026/06/09")
        raise AssertionError("expected 400 on bad date")
    except HTTPException as e:
        assert e.status_code == 400

    # numeric->string with '0' default; mapper produces string numerics.
    assert q._num_str(None) == "0" and q._num_str(12) == "12"
    row = {"rtv_id": "CR-1", "item_description": "A", "material_type": "RM",
           "item_category": "N", "sub_category": "S", "uom": "KG",
           "qty": 4, "rate": 10, "value": 40, "net_weight": 25, "carton_weight": 0,
           "lot_number": None, "item_mark": None, "spl_remarks": None, "vakkal": None,
           "created_at": None, "updated_at": None}
    m = q._map_line_row(row)
    assert m["qty"] == "4" and m["value"] == "40" and m["rtv_id"] == "CR-1"
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_helpers.py`
Expected: FAIL — `ModuleNotFoundError: ... query_service`

- [ ] **Step 3: Write the helpers + mappers**

Create `app/modules/customer_returns/services/query_service.py`:

```python
"""Customer-Returns read side: column constants, row mappers, pure helpers.
Async list/get functions are added in later tasks of the same module.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

HEADER_COLS = (
    "rtv_id, rtv_date, factory_unit, customer, invoice_number, challan_no, dn_no, "
    "conversion, sales_poc, sales_poc_email, business_head, remark, vehicle_number, "
    "transporter_name, driver_name, inward_manager, status, created_by, created_ts, updated_at"
)
LINE_COLS = (
    "rtv_id, item_description, material_type, item_category, sub_category, uom, qty, rate, "
    "value, net_weight, carton_weight, lot_number, item_mark, spl_remarks, vakkal, created_at, updated_at"
)
BOX_COLS = (
    "rtv_id, article_description, box_number, box_id, uom, conversion, lot_number, item_mark, "
    "spl_remarks, vakkal, net_weight, gross_weight, count, created_at, updated_at"
)


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _line_value(qty: int, rate: float, raw: Any) -> float:
    """Use the supplied value when > 0, else compute qty*rate (source rule)."""
    v = _to_float(raw)
    return v if (v is not None and v > 0) else qty * rate


def _num_str(v: Any) -> str:
    """Serialize a numeric DB value as a string, defaulting to '0'.

    Integral floats/Decimals render without a trailing '.0' ("40", not "40.0")
    and never use scientific notation.
    """
    if v is None:
        return "0"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return ("%f" % v).rstrip("0").rstrip(".")
    if isinstance(v, Decimal):
        s = format(v, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


def _convert_date(s: Optional[str]) -> Optional[date]:
    """Parse a DD-MM-YYYY filter string; 400 on bad format; None passes through."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d-%m-%Y").date()
    except ValueError:
        raise HTTPException(
            400,
            detail={"error": "invalid_date", "message": "date must be DD-MM-YYYY",
                    "details": {"value": s}},
        )


def _map_header_row(r: dict) -> dict:
    return {
        "rtv_id": r.get("rtv_id"),
        "rtv_date": r.get("rtv_date"),
        "factory_unit": r.get("factory_unit") or "",
        "customer": r.get("customer") or "",
        "invoice_number": r.get("invoice_number"),
        "challan_no": r.get("challan_no"),
        "dn_no": r.get("dn_no"),
        "conversion": _num_str(r.get("conversion")),
        "sales_poc": r.get("sales_poc"),
        "sales_poc_email": r.get("sales_poc_email"),
        "business_head": r.get("business_head"),
        "remark": r.get("remark"),
        "vehicle_number": r.get("vehicle_number"),
        "transporter_name": r.get("transporter_name"),
        "driver_name": r.get("driver_name"),
        "inward_manager": r.get("inward_manager"),
        "status": r.get("status") or "Pending",
        "created_by": r.get("created_by"),
        "created_ts": r.get("created_ts"),
        "updated_at": r.get("updated_at"),
    }


def _map_line_row(r: dict) -> dict:
    return {
        "rtv_id": r.get("rtv_id"),
        "item_description": r.get("item_description") or "",
        "material_type": r.get("material_type") or "",
        "item_category": r.get("item_category") or "",
        "sub_category": r.get("sub_category") or "",
        "uom": r.get("uom") or "",
        "qty": _num_str(r.get("qty")),
        "rate": _num_str(r.get("rate")),
        "value": _num_str(r.get("value")),
        "net_weight": _num_str(r.get("net_weight")),
        "carton_weight": _num_str(r.get("carton_weight")),
        "lot_number": r.get("lot_number"),
        "item_mark": r.get("item_mark"),
        "spl_remarks": r.get("spl_remarks"),
        "vakkal": r.get("vakkal"),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def _map_box_row(r: dict) -> dict:
    return {
        "rtv_id": r.get("rtv_id"),
        "article_description": r.get("article_description") or "",
        "box_number": r.get("box_number"),
        "box_id": r.get("box_id"),
        "uom": r.get("uom"),
        "conversion": None if r.get("conversion") is None else str(r.get("conversion")),
        "lot_number": r.get("lot_number"),
        "item_mark": r.get("item_mark"),
        "spl_remarks": r.get("spl_remarks"),
        "vakkal": r.get("vakkal"),
        "net_weight": _num_str(r.get("net_weight")),
        "gross_weight": _num_str(r.get("gross_weight")),
        "count": r.get("count"),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_helpers.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/services/query_service.py tests/services/test_cr_helpers.py
git commit -m "feat(customer-returns): query-service helpers, mappers, column constants"
```

---

### Task 5: `create_cr` + `get_cr` (create & read-back)

**Files:**
- Create: `app/modules/customer_returns/services/create_service.py`
- Modify: `app/modules/customer_returns/services/query_service.py` (add `get_cr`, `_fetch_lines`, `_fetch_boxes`)
- Test: `tests/services/test_cr_create_rollback.py`

**Interfaces:**
- Consumes: `cr_table_names`, mappers/constants from Task 4, schemas from Task 3.
- Produces: `create_service.create_cr(conn, company, data: CRCreate, created_by: str) -> dict` (returns the full `CRWithDetails`-shaped dict; owns its transaction; generates the `CR-` id). `query_service.get_cr(conn, company, cr_id) -> dict` (404 if missing). `query_service._fetch_lines/_fetch_boxes(conn, tables, cr_id) -> list`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_create_rollback.py`:

```python
"""Rollback integration test: create_cr + get_cr against the real DB. All writes
are rolled back — safe against prod. Run:
    PYTHONPATH=. python tests/services/test_cr_create_rollback.py
"""
import asyncio
import asyncpg
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        payload = schemas.CRCreate(
            company="CFPL",
            header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME FOODS",
                                          conversion="1.5", business_head="Head One"),
            lines=[
                schemas.CRLineCreate(material_type="rm", item_category="Nuts",
                                     sub_category="Almond", item_description="ALMOND W-320",
                                     uom="kg", qty="4", rate="10"),  # value auto = 40
                schemas.CRLineCreate(material_type="rm", item_category="Nuts",
                                     sub_category="Cashew", item_description="CASHEW W-240",
                                     uom="kg", qty="2", rate="20", value="45"),
            ],
        )
        created = await create_service.create_cr(conn, "CFPL", payload,
                                                  "tester@candorfoods.in")
        cr_id = created["rtv_id"]
        assert cr_id.startswith("CR-"), cr_id
        assert created["status"] == "Pending"
        assert created["created_by"] == "tester@candorfoods.in"
        assert len(created["lines"]) == 2 and created["boxes"] == []

        fetched = await query_service.get_cr(conn, "CFPL", cr_id)
        assert fetched["rtv_id"] == cr_id
        by_desc = {l["item_description"]: l for l in fetched["lines"]}
        assert by_desc["ALMOND W-320"]["value"] == "40"   # computed qty*rate
        assert by_desc["CASHEW W-240"]["value"] == "45"   # supplied
        assert by_desc["ALMOND W-320"]["material_type"] == "RM"  # uppercased
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_create_rollback.py`
Expected: FAIL — `ModuleNotFoundError: ... create_service`

- [ ] **Step 3: Add `get_cr` + fetch helpers to query_service**

Append to `app/modules/customer_returns/services/query_service.py`:

```python
from app.modules.customer_returns.tables import cr_table_names


async def _fetch_lines(conn, tables: dict, cr_id: str) -> list:
    rows = await conn.fetch(
        f"SELECT {LINE_COLS} FROM {tables['lines']} WHERE rtv_id = $1 ORDER BY item_description",
        cr_id,
    )
    return [_map_line_row(dict(r)) for r in rows]


async def _fetch_boxes(conn, tables: dict, cr_id: str) -> list:
    rows = await conn.fetch(
        f"SELECT {BOX_COLS} FROM {tables['boxes']} WHERE rtv_id = $1 "
        "ORDER BY article_description, box_number",
        cr_id,
    )
    return [_map_box_row(dict(r)) for r in rows]


async def get_cr(conn, company: str, cr_id: str) -> dict:
    tables = cr_table_names(company)
    hdr = await conn.fetchrow(
        f"SELECT {HEADER_COLS} FROM {tables['header']} WHERE rtv_id = $1", cr_id
    )
    if not hdr:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}",
                    "details": {"rtv_id": cr_id}},
        )
    result = _map_header_row(dict(hdr))
    result["lines"] = await _fetch_lines(conn, tables, cr_id)
    result["boxes"] = await _fetch_boxes(conn, tables, cr_id)
    return result
```

- [ ] **Step 4: Write `create_service.create_cr`**

Create `app/modules/customer_returns/services/create_service.py`:

```python
"""Customer-Returns write side: create/update/delete (header + lines).

Owns its own transaction (mirrors transfer/create_service). The rtv_id string is
the header PK; a same-second collision retries inside a SAVEPOINT with a numeric
suffix so the common id stays 'CR-YYYYMMDDHHMMSS'.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import HTTPException

from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import query_service as q
from app.modules.customer_returns.tables import cr_table_names

_IST = ZoneInfo("Asia/Kolkata")


def _generate_cr_id() -> str:
    return "CR-" + datetime.now(_IST).strftime("%Y%m%d%H%M%S")


async def _insert_line(conn, tables: dict, cr_id: str, line: schemas.CRLineCreate) -> None:
    qty = int(q._to_float(line.qty) or 0)
    rate = q._to_float(line.rate) or 0.0
    value = q._line_value(qty, rate, line.value)
    net_weight = q._to_float(line.net_weight) or 0.0
    carton_weight = q._to_float(line.carton_weight) or 0.0
    await conn.execute(
        f"""
        INSERT INTO {tables['lines']}
            (rtv_id, item_description, material_type, item_category, sub_category, uom,
             qty, rate, value, net_weight, carton_weight, lot_number, item_mark, spl_remarks, vakkal)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        ON CONFLICT (rtv_id, item_description) DO UPDATE SET
            material_type=EXCLUDED.material_type, item_category=EXCLUDED.item_category,
            sub_category=EXCLUDED.sub_category, uom=EXCLUDED.uom, qty=EXCLUDED.qty,
            rate=EXCLUDED.rate, value=EXCLUDED.value, net_weight=EXCLUDED.net_weight,
            carton_weight=EXCLUDED.carton_weight, lot_number=EXCLUDED.lot_number,
            item_mark=EXCLUDED.item_mark, spl_remarks=EXCLUDED.spl_remarks,
            vakkal=EXCLUDED.vakkal, updated_at=NOW()
        """,
        cr_id, line.item_description, line.material_type, line.item_category,
        line.sub_category, line.uom, qty, rate, value, net_weight, carton_weight,
        line.lot_number, line.item_mark, line.spl_remarks, line.vakkal,
    )


async def _insert_header(conn, tables: dict, header: schemas.CRHeaderCreate,
                         created_by: str) -> str:
    base = _generate_cr_id()
    conversion = q._to_float(header.conversion) or 0.0
    for attempt in range(6):
        cand = base if attempt == 0 else f"{base}-{attempt}"
        try:
            async with conn.transaction():  # SAVEPOINT — isolates PK-collision retry
                await conn.execute(
                    f"""
                    INSERT INTO {tables['header']}
                        (rtv_id, factory_unit, customer, invoice_number, challan_no, dn_no,
                         conversion, sales_poc, sales_poc_email, business_head, remark,
                         vehicle_number, transporter_name, driver_name, inward_manager,
                         status, created_by)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,'Pending',$16)
                    """,
                    cand, header.factory_unit, header.customer, header.invoice_number,
                    header.challan_no, header.dn_no, conversion, header.sales_poc,
                    header.sales_poc_email, header.business_head, header.remark,
                    header.vehicle_number, header.transporter_name, header.driver_name,
                    header.inward_manager, created_by,
                )
            return cand
        except asyncpg.UniqueViolationError:
            continue
    raise HTTPException(
        500,
        detail={"error": "cr_id_generation_failed",
                "message": "Could not allocate a unique CR id"},
    )


async def create_cr(conn, company: str, data: schemas.CRCreate, created_by: str) -> dict:
    tables = cr_table_names(company)
    async with conn.transaction():
        cr_id = await _insert_header(conn, tables, data.header, created_by)
        for line in data.lines:
            await _insert_line(conn, tables, cr_id, line)
    # Read back AFTER commit so the response matches get/list exactly.
    return await q.get_cr(conn, company, cr_id)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_create_rollback.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 6: Commit**

```bash
git add app/modules/customer_returns/services/create_service.py app/modules/customer_returns/services/query_service.py tests/services/test_cr_create_rollback.py
git commit -m "feat(customer-returns): create_cr + get_cr with rollback integration test"
```

---

### Task 6: `list_crs` (filter / paginate / sort / aggregates)

**Files:**
- Modify: `app/modules/customer_returns/services/query_service.py` (add `list_crs`, `_SORTABLE`)
- Test: `tests/services/test_cr_list_rollback.py`

**Interfaces:**
- Produces: `query_service.list_crs(conn, *, company, page, per_page, status=None, factory_unit=None, customer=None, from_date=None, to_date=None, sort_by="created_ts", sort_order="desc") -> dict` shaped like `CRListResponse` (records/total/page/per_page/total_pages), each record carrying `items_count/boxes_count/total_qty/total_net_weight`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_list_rollback.py`:

```python
"""Rollback integration test: list_crs filtering/pagination/aggregates. Run:
    PYTHONPATH=. python tests/services/test_cr_list_rollback.py
"""
import asyncio
import asyncpg
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service


def _mk(customer: str) -> schemas.CRCreate:
    return schemas.CRCreate(
        company="CFPL",
        header=schemas.CRHeaderCreate(factory_unit="A-185", customer=customer),
        lines=[schemas.CRLineCreate(material_type="RM", item_category="N", sub_category="S",
                                    item_description="ALMOND W-320", uom="KG", qty="3", rate="10")],
    )


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        a = await create_service.create_cr(conn, "CFPL", _mk("ZZ_TEST_ACME"), "t@x.in")
        b = await create_service.create_cr(conn, "CFPL", _mk("ZZ_TEST_BETA"), "t@x.in")

        # customer ILIKE filter finds only ACME.
        res = await query_service.list_crs(conn, company="CFPL", page=1, per_page=10,
                                           customer="zz_test_acme")
        ids = {r["rtv_id"] for r in res["records"]}
        assert a["rtv_id"] in ids and b["rtv_id"] not in ids, ids
        row = next(r for r in res["records"] if r["rtv_id"] == a["rtv_id"])
        assert row["items_count"] == 1 and row["total_qty"] == 3 and row["boxes_count"] == 0
        assert res["total"] >= 1 and res["page"] == 1 and res["per_page"] == 10

        # per_page pagination math.
        res2 = await query_service.list_crs(conn, company="CFPL", page=1, per_page=1,
                                            customer="zz_test_")
        assert len(res2["records"]) == 1 and res2["total"] >= 2 and res2["total_pages"] >= 2
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_list_rollback.py`
Expected: FAIL — `AttributeError: module 'query_service' has no attribute 'list_crs'`

- [ ] **Step 3: Add `list_crs`**

Append to `app/modules/customer_returns/services/query_service.py`:

```python
# Whitelisted sort columns -> real column names (invalid falls back to created_ts).
_SORTABLE = {
    "created_ts": "created_ts",
    "rtv_date": "rtv_date",
    "customer": "customer",
    "factory_unit": "factory_unit",
    "status": "status",
    "rtv_id": "rtv_id",
}


async def list_crs(conn, *, company: str, page: int, per_page: int,
                   status: Optional[str] = None, factory_unit: Optional[str] = None,
                   customer: Optional[str] = None, from_date: Optional[str] = None,
                   to_date: Optional[str] = None, sort_by: str = "created_ts",
                   sort_order: str = "desc") -> dict:
    tables = cr_table_names(company)
    clauses: list[str] = ["1=1"]
    args: list[Any] = []
    if status:
        args.append(status); clauses.append(f"h.status = ${len(args)}")
    if factory_unit:
        args.append(factory_unit); clauses.append(f"h.factory_unit = ${len(args)}")
    if customer:
        args.append(f"%{customer}%"); clauses.append(f"h.customer ILIKE ${len(args)}")
    df = _convert_date(from_date)
    if df:
        args.append(df); clauses.append(f"h.rtv_date >= ${len(args)}")
    dt = _convert_date(to_date)
    if dt:
        args.append(dt); clauses.append(f"h.rtv_date < (${len(args)}::date + 1)")
    where = " AND ".join(clauses)

    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM {tables['header']} h WHERE {where}", *args
    )

    col = _SORTABLE.get(sort_by, "created_ts")
    direction = "ASC" if str(sort_order).lower() == "asc" else "DESC"
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    offset = (page - 1) * per_page

    rows = await conn.fetch(
        f"""
        SELECT {HEADER_COLS},
               (SELECT COUNT(*) FROM {tables['lines']} l WHERE l.rtv_id = h.rtv_id) AS items_count,
               (SELECT COUNT(*) FROM {tables['boxes']} b WHERE b.rtv_id = h.rtv_id) AS boxes_count,
               (SELECT COALESCE(SUM(l.qty),0) FROM {tables['lines']} l WHERE l.rtv_id = h.rtv_id) AS total_qty,
               (SELECT COALESCE(SUM(b.net_weight),0) FROM {tables['boxes']} b WHERE b.rtv_id = h.rtv_id) AS total_net_weight
          FROM {tables['header']} h
         WHERE {where}
         ORDER BY h.{col} {direction}
         LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, per_page, offset,
    )
    records = []
    for r in rows:
        d = dict(r)
        item = _map_header_row(d)
        item["items_count"] = int(d.get("items_count") or 0)
        item["boxes_count"] = int(d.get("boxes_count") or 0)
        item["total_qty"] = int(d.get("total_qty") or 0)
        item["total_net_weight"] = float(d.get("total_net_weight") or 0)
        records.append(item)

    total = int(total or 0)
    total_pages = (total + per_page - 1) // per_page
    return {"records": records, "total": total, "page": page,
            "per_page": per_page, "total_pages": total_pages}
```

> Note: the header columns in the SELECT are unqualified — with a single `{tables['header']} h` in the FROM they resolve against `h`; the four correlated subqueries add the aggregate columns.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_list_rollback.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/services/query_service.py tests/services/test_cr_list_rollback.py
git commit -m "feat(customer-returns): list_crs with filters, pagination, aggregates"
```

---

### Task 7: `update_cr` + `update_cr_lines` + `delete_cr`

**Files:**
- Modify: `app/modules/customer_returns/services/create_service.py`
- Test: `tests/services/test_cr_update_delete_rollback.py`

**Interfaces:**
- Produces:
  - `update_cr(conn, company, cr_id, data: CRHeaderUpdate) -> dict` — partial header update (400 if no fields; 404 if missing); returns the mapped header dict.
  - `update_cr_lines(conn, company, cr_id, data: CRLinesUpdateRequest) -> dict` — replaces all lines; returns `{"status":"updated","rtv_id":cr_id,"lines_count":n}` (404 if CR missing).
  - `delete_cr(conn, company, cr_id) -> dict` — cascade delete; returns `{"success":True,"message":...,"rtv_id":cr_id,"lines_count":x,"boxes_count":y}` (404 if missing).

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_update_delete_rollback.py`:

```python
"""Rollback integration test: update_cr, update_cr_lines, delete_cr. Run:
    PYTHONPATH=. python tests/services/test_cr_update_delete_rollback.py
"""
import asyncio
import asyncpg
from fastapi import HTTPException
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        created = await create_service.create_cr(
            conn, "CFPL",
            schemas.CRCreate(company="CFPL",
                             header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME"),
                             lines=[schemas.CRLineCreate(material_type="RM", item_category="N",
                                     sub_category="S", item_description="ALMOND W-320",
                                     uom="KG", qty="1", rate="1")]),
            "t@x.in")
        cr_id = created["rtv_id"]

        # header update
        upd = await create_service.update_cr(conn, "CFPL", cr_id,
                                             schemas.CRHeaderUpdate(remark="edited", status="Submitted"))
        assert upd["remark"] == "edited" and upd["status"] == "Submitted"

        # empty update -> 400
        try:
            await create_service.update_cr(conn, "CFPL", cr_id, schemas.CRHeaderUpdate())
            raise AssertionError("expected 400 empty update")
        except HTTPException as e:
            assert e.status_code == 400

        # replace lines
        res = await create_service.update_cr_lines(
            conn, "CFPL", cr_id,
            schemas.CRLinesUpdateRequest(lines=[
                schemas.CRLineCreate(material_type="RM", item_category="N", sub_category="S",
                                     item_description="CASHEW W-240", uom="KG", qty="2", rate="5"),
            ]))
        assert res["lines_count"] == 1
        fetched = await query_service.get_cr(conn, "CFPL", cr_id)
        assert [l["item_description"] for l in fetched["lines"]] == ["CASHEW W-240"]

        # delete (cascades lines)
        d = await create_service.delete_cr(conn, "CFPL", cr_id)
        assert d["success"] and d["lines_count"] == 1
        try:
            await query_service.get_cr(conn, "CFPL", cr_id)
            raise AssertionError("expected 404 after delete")
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

Run: `PYTHONPATH=. python tests/services/test_cr_update_delete_rollback.py`
Expected: FAIL — `AttributeError: module 'create_service' has no attribute 'update_cr'`

- [ ] **Step 3: Add update/delete functions**

Append to `app/modules/customer_returns/services/create_service.py`:

```python
_HEADER_UPDATABLE = [
    "factory_unit", "customer", "invoice_number", "challan_no", "dn_no", "conversion",
    "sales_poc", "sales_poc_email", "business_head", "remark", "status",
    "vehicle_number", "transporter_name", "driver_name", "inward_manager",
]


async def update_cr(conn, company: str, cr_id: str, data: schemas.CRHeaderUpdate) -> dict:
    tables = cr_table_names(company)
    provided = data.model_dump(exclude_none=True)
    if not provided:
        raise HTTPException(
            400,
            detail={"error": "empty_update", "message": "Provide at least one field to update"},
        )
    sets: list[str] = []
    args: list = []
    for col in _HEADER_UPDATABLE:
        if col in provided:
            val = provided[col]
            if col == "conversion":
                val = q._to_float(val) or 0.0
            args.append(val); sets.append(f"{col} = ${len(args)}")
    sets.append("updated_at = NOW()")
    args.append(cr_id)
    row = await conn.fetchrow(
        f"UPDATE {tables['header']} SET {', '.join(sets)} WHERE rtv_id = ${len(args)} "
        f"RETURNING {q.HEADER_COLS}",
        *args,
    )
    if not row:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    return q._map_header_row(dict(row))


async def update_cr_lines(conn, company: str, cr_id: str,
                          data: schemas.CRLinesUpdateRequest) -> dict:
    tables = cr_table_names(company)
    exists = await conn.fetchval(
        f"SELECT 1 FROM {tables['header']} WHERE rtv_id = $1", cr_id
    )
    if not exists:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    async with conn.transaction():
        await conn.execute(f"DELETE FROM {tables['lines']} WHERE rtv_id = $1", cr_id)
        for line in data.lines:
            await _insert_line(conn, tables, cr_id, line)
    return {"status": "updated", "rtv_id": cr_id, "lines_count": len(data.lines)}


async def delete_cr(conn, company: str, cr_id: str) -> dict:
    tables = cr_table_names(company)
    hdr = await conn.fetchrow(
        f"SELECT rtv_id FROM {tables['header']} WHERE rtv_id = $1", cr_id
    )
    if not hdr:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    lines_count = await conn.fetchval(
        f"SELECT COUNT(*) FROM {tables['lines']} WHERE rtv_id = $1", cr_id)
    boxes_count = await conn.fetchval(
        f"SELECT COUNT(*) FROM {tables['boxes']} WHERE rtv_id = $1", cr_id)
    async with conn.transaction():
        # FK ON DELETE CASCADE removes lines/boxes with the header.
        await conn.execute(f"DELETE FROM {tables['header']} WHERE rtv_id = $1", cr_id)
    return {"success": True, "message": f"Customer return {cr_id} deleted",
            "rtv_id": cr_id, "lines_count": int(lines_count or 0),
            "boxes_count": int(boxes_count or 0)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_update_delete_rollback.py`
Expected: `ASSERTIONS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/modules/customer_returns/services/create_service.py tests/services/test_cr_update_delete_rollback.py
git commit -m "feat(customer-returns): update_cr, update_cr_lines, delete_cr"
```

---

### Task 8: Router + main.py registration

**Files:**
- Create: `app/modules/customer_returns/router.py`
- Modify: `app/main.py` (import near line 35; `include_router` near line 148)
- Test: `tests/services/test_cr_routes.py`

**Interfaces:**
- Consumes: all services + schemas above; `AuthUser`, `get_current_user` from `app.modules.auth.middleware`.
- Produces (routes under `/api/v1/customer-returns`): `POST /{company}`, `GET /{company}`, `GET /{company}/{cr_id}`, `PUT /{company}/{cr_id}`, `PUT /{company}/{cr_id}/lines`, `DELETE /{company}/{cr_id}`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_cr_routes.py`:

```python
"""Verifies the customer-returns routes are registered on the app. No DB. Run:
    PYTHONPATH=. python tests/services/test_cr_routes.py
"""
from app.main import app

EXPECTED = {
    ("POST", "/api/v1/customer-returns/{company}"),
    ("GET", "/api/v1/customer-returns/{company}"),
    ("GET", "/api/v1/customer-returns/{company}/{cr_id}"),
    ("PUT", "/api/v1/customer-returns/{company}/{cr_id}"),
    ("PUT", "/api/v1/customer-returns/{company}/{cr_id}/lines"),
    ("DELETE", "/api/v1/customer-returns/{company}/{cr_id}"),
}


def main() -> None:
    present = {(m, r.path) for r in app.routes for m in getattr(r, "methods", set()) or set()}
    missing = {e for e in EXPECTED if e not in present}
    assert not missing, f"missing routes: {sorted(missing)}"
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python tests/services/test_cr_routes.py`
Expected: FAIL — `assert not missing` (all six routes missing; the module isn't registered yet).

- [ ] **Step 3: Write the router**

Create `app/modules/customer_returns/router.py`:

```python
"""/api/v1/customer-returns/* — Customer-Returns module (Phase 1: header+lines CRUD).

Thin router; every endpoint requires a valid access token and derives the actor
from the JWT (never from request params). Company is a CFPL/CDPL path segment
mapped to a table prefix by the service layer.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service

router = APIRouter(prefix="/api/v1/customer-returns", tags=["Customer Returns"])


def _actor(user: AuthUser) -> str:
    return user.email or user.full_name or str(user.user_id)


@router.post("/{company}", status_code=201, response_model=schemas.CRWithDetails)
async def create_customer_return(
    company: str,
    body: schemas.CRCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.create_cr(conn, company, body, _actor(user))


@router.get("/{company}", response_model=schemas.CRListResponse)
async def list_customer_returns(
    company: str,
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    status: Optional[str] = Query(None),
    factory_unit: Optional[str] = Query(None),
    customer: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    to_date: Optional[str] = Query(None, description="DD-MM-YYYY"),
    sort_by: str = Query("created_ts"),
    sort_order: str = Query("desc"),
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.list_crs(
            conn, company=company, page=page, per_page=per_page, status=status,
            factory_unit=factory_unit, customer=customer, from_date=from_date,
            to_date=to_date, sort_by=sort_by, sort_order=sort_order,
        )


@router.get("/{company}/{cr_id}", response_model=schemas.CRWithDetails)
async def get_customer_return(
    company: str,
    cr_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await query_service.get_cr(conn, company, cr_id)


@router.put("/{company}/{cr_id}", response_model=schemas.CRHeaderResponse)
async def update_customer_return(
    company: str,
    cr_id: str,
    body: schemas.CRHeaderUpdate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.update_cr(conn, company, cr_id, body)


@router.put("/{company}/{cr_id}/lines", response_model=schemas.CRLinesUpdateResponse)
async def update_customer_return_lines(
    company: str,
    cr_id: str,
    body: schemas.CRLinesUpdateRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.update_cr_lines(conn, company, cr_id, body)


@router.delete("/{company}/{cr_id}", response_model=schemas.CRDeleteResponse)
async def delete_customer_return(
    company: str,
    cr_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await create_service.delete_cr(conn, company, cr_id)
```

- [ ] **Step 4: Register the router in `app/main.py`**

Add the import beside the other module-router imports (right after the `packing` import, ~line 35):

```python
from app.modules.customer_returns.router import router as customer_returns_router
```

Add the registration beside the other `include_router` calls (right after `app.include_router(packing_router)`, ~line 148):

```python
app.include_router(customer_returns_router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. python tests/services/test_cr_routes.py`
Expected: `ASSERTIONS PASSED`

(If importing `app.main` requires env vars, run with the project's `.env` present — `python-dotenv` loads it. The Phase-1 rollback tests already exercise behavior; this test only checks route registration.)

- [ ] **Step 6: Commit**

```bash
git add app/modules/customer_returns/router.py app/main.py tests/services/test_cr_routes.py
git commit -m "feat(customer-returns): CRUD router + register in main.py"
```

---

## Phase 1 Done — Verification

Run the full Phase-1 test set (DB tests roll back; safe against prod):

```bash
PYTHONPATH=. python tests/services/test_cr_migration.py
PYTHONPATH=. python tests/services/test_cr_tables.py
PYTHONPATH=. python tests/services/test_cr_schemas.py
PYTHONPATH=. python tests/services/test_cr_helpers.py
PYTHONPATH=. python tests/services/test_cr_create_rollback.py
PYTHONPATH=. python tests/services/test_cr_list_rollback.py
PYTHONPATH=. python tests/services/test_cr_update_delete_rollback.py
PYTHONPATH=. python tests/services/test_cr_routes.py
```

Each must print `ASSERTIONS PASSED`. Phase 1 deliverable: a customer can be created, listed (with aggregates), fetched with lines, header-updated, lines-replaced, and deleted — all JWT-gated, identity from the token.

**Next:** Phase 2 (boxes/print, bulk-save, box-edit-log, Excel export) — separate plan.
