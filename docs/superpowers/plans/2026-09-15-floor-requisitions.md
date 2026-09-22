# Floor Requisitions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the floor request a BOM article it is short of from the job card's Material allocation tab, store issue it from a new Floor Requisitions screen, and the floor confirm receipt — one tracked record per request.

**Architecture:** A new `floor_requisition` table (migration 111) whose primary key is the 8-digit time number. A new server module `app/modules/floor_requisition` (pure `rules.py`, DB `services/requisition_service.py`, `router.py`) that derives place, unit and the stock snapshot from the database itself, reusing the stock-take floor-stock service and a place-scope helper extracted from the stock-take router. On the web: a pure form module, an API client, shared modal/status/cancel UI, a Request column + dialog + requisitions section on the tab, and a store list screen.

**Tech Stack:** FastAPI + asyncpg (Python, pytest); Next.js 16 / React 19 / Tailwind (TypeScript); Node 24 native TS stripping for web helper tests.

**Spec:** `server_replica/docs/superpowers/specs/2026-09-15-floor-requisitions-design.md`

## Global Constraints

- **No git commits** unless the user asks. Each task ends with a checkpoint (list changed files), not a commit.
- **Live RDS `warehouse_db` is read-only for this work.** Never run `scripts/migrate.py`. Migration 111 is applied by the user, or by the controller on explicit user instruction, that one file only.
- Server paths are relative to `C:\Candor\Consumption\server_replica`; web paths to `C:\Candor\Consumption\web_replica`.
- New server `.py` / `.sql` files use **CRLF** (the stock_take module's convention); `app/modules/stock_take/router.py` is CRLF — preserve it. New web files use **LF**. `src/app/modules/job-card/[id]/page.tsx` is **CRLF** — preserve it.
- Server test command: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q <files>` from `server_replica`. Known pre-existing failures: 2 in `tests/services/test_sku_lookup_permission.py` — not ours.
- Web checks from `web_replica`: `npx tsc --noEmit -p . 2>&1 | grep -v '^\.next/'` must print nothing (the stale `.next/types/validator.ts` error is pre-existing); `npx eslint <changed files>` 0 problems on new files; `page.tsx` stays at its baseline of 11 ESLint problems.
- Permission tuple: `('production', 'floor_requisitions', NULL, <action>)`, actions `view`, `create`, `issue`, `receive`, `cancel`. Grants: admin all; floor_manager view/create/receive/cancel; store_head view/issue/cancel.
- Units are exactly `'kg'` or `'pcs'`. Quantities `NUMERIC(14,3)`, > 0; kg at most 3 decimals; pcs whole numbers; below 100,000,000,000.
- Requisition number = `requisition_id BIGINT PRIMARY KEY`, minted by `app.core.helpers.new_short_time_id`, inserted via `insert_with_pk_retry`. Lists order by `raised_at DESC, requisition_id DESC`, never by number alone.
- The actor of every step comes from the access token (full name → email → phone → `user:<id>`), never from the request body.
- Server errors: `HTTPException(status, detail={"error": code, "message": text, "details": {...}})` — the stock-take convention the web's `readApiErrorMessage` reads.
- Status words shown to people: Raised, Issued, Received, Cancelled.

## File Map

| File | Responsibility |
|---|---|
| `server_replica/app/db/111_floor_requisition.sql` | Table, indexes, permission catalog + grants |
| `server_replica/scripts/migrate.py` | Registers 111 after 110 |
| `server_replica/app/modules/stock_take/place_scope.py` | Grants-based warehouse/floor check, shared |
| `server_replica/app/modules/stock_take/router.py` | `_floor_stock_place` delegates to place_scope |
| `server_replica/app/modules/floor_requisition/__init__.py`, `services/__init__.py` | Package markers |
| `server_replica/app/modules/floor_requisition/rules.py` | Pure: units, quantity parsing, snapshot, actor |
| `server_replica/app/modules/floor_requisition/services/requisition_service.py` | DB: raise, list, issue, receive, cancel |
| `server_replica/app/modules/floor_requisition/router.py` | HTTP + permissions |
| `server_replica/app/main.py` | Includes the router |
| `server_replica/tests/services/test_floor_requisition_*.py`, `test_place_scope.py` | Tests |
| `web_replica/src/lib/floor-requisition-form.ts` (+ `.test.ts`) | Pure: unit, default qty, qty check, formatting, per-article state |
| `web_replica/src/lib/floor-requisitions.ts` | API client + types |
| `web_replica/src/components/floor-requisitions/RequisitionUi.tsx` | Modal shell, StatusTag, CancelRequisitionDialog, button/field classes |
| `web_replica/src/app/modules/job-card/[id]/_RequestDialog.tsx` | Raise dialog |
| `web_replica/src/app/modules/job-card/[id]/_JobCardRequisitions.tsx` | Tab section: list, receive, cancel |
| `web_replica/src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx` | Request column, loads requisitions |
| `web_replica/src/app/modules/job-card/[id]/page.tsx` | Passes `jobCardId` |
| `web_replica/src/app/modules/production/floor-requisitions/page.tsx`, `_IssueDialog.tsx` | Store screen |
| `web_replica/src/app/modules/production/page.tsx`, `web_replica/src/lib/modules.tsx` | Tile + store_head scope |

---

### Task 1: Migration 111 — table, indexes, permissions

**Files:**
- Create: `server_replica/app/db/111_floor_requisition.sql`
- Modify: `server_replica/scripts/migrate.py` (after the `110_stocktake_txn_verification.sql` entry, before the closing `]` of `SQL_FILES`)
- Test: `server_replica/tests/services/test_floor_requisition_migration.py`

**Interfaces:**
- Produces: table `floor_requisition` (columns exactly as below), unique index `uq_floor_requisition_open`, catalog rows `production.floor_requisitions.{view,create,issue,receive,cancel}`.

- [ ] **Step 1: Write the failing test**

```python
"""Migration 111 — floor_requisition. A static check of the file and its
registration; the SQL itself is applied to RDS by the user, not by tests."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQL_PATH = ROOT / "app" / "db" / "111_floor_requisition.sql"


def _sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def test_registered_after_110_in_the_runner():
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i110 = text.index('"110_stocktake_txn_verification.sql"')
    i111 = text.index('"111_floor_requisition.sql"')
    end = text.index("# ── Optional: drop the v1 legacy")
    assert i110 < i111 < end


def test_the_requisition_number_is_the_primary_key():
    assert re.search(r"requisition_id\s+BIGINT\s+PRIMARY KEY", _sql())


def test_every_quantity_is_stored_with_its_unit():
    sql = _sql()
    for q in ("requested", "required", "available", "shortage", "issued"):
        assert re.search(rf"\b{q}_qty\s+NUMERIC\(14,3\)", sql), q
        assert re.search(rf"\b{q}_unit\s+TEXT", sql), q
    assert "CHECK (requested_unit IN ('kg','pcs'))" in sql
    assert "CHECK (issued_unit   IS NULL OR issued_unit   = requested_unit)" in sql


def test_one_open_request_per_job_card_and_article():
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_open\s+"
        r"ON floor_requisition \(job_card_id, UPPER\(BTRIM\(material_sku_name\)\)\)\s+"
        r"WHERE status = 'raised'",
        _sql(),
    )


def test_catalog_insert_is_null_safe():
    sql = _sql()
    assert "IS NOT DISTINCT FROM v.sub_module" in sql
    assert "p.sub_sub_module IS NULL" in sql


GRANTS = {
    "admin": {"view", "create", "issue", "receive", "cancel"},
    "floor_manager": {"view", "create", "receive", "cancel"},
    "store_head": {"view", "issue", "cancel"},
}


def test_grants_match_the_spec_and_nothing_more():
    found = set(re.findall(r"\('(admin|floor_manager|store_head)',\s*'(\w+)'\)", _sql()))
    want = {(role, action) for role, actions in GRANTS.items() for action in actions}
    assert found == want
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_floor_requisition_migration.py`
Expected: FAIL — `FileNotFoundError` for `111_floor_requisition.sql` / `ValueError: substring not found`.

- [ ] **Step 3: Write the migration (CRLF)**

`server_replica/app/db/111_floor_requisition.sql`:

```sql
-- 111_floor_requisition.sql — floor requisitions: the floor asks store for a BOM
-- article it is short of, store issues it, the floor confirms receipt.
--
-- Spec: docs/superpowers/specs/2026-09-15-floor-requisitions-design.md
--
-- TRACKING ONLY. Nothing here writes new_stock_entries or stocktake_transactions;
-- floor stock still changes only through stock take.
--
-- requisition_id IS the requisition number: an app-supplied 8-digit time id
-- (app.core.helpers.new_short_time_id) inserted through insert_with_pk_retry.
-- It must be the PRIMARY KEY — that retry only catches collisions on a *_pkey
-- constraint. The numbers wrap about every 28 hours and are not in date order, so
-- every list orders by raised_at.
--
-- Every quantity is stored with its unit, and every unit on a row is the
-- requested one (kg or pcs).
--
-- Idempotent: IF NOT EXISTS throughout; the catalog insert uses the NULL-safe
-- NOT EXISTS guard from 084 (auth_permission's UNIQUE treats NULL
-- sub_sub_module as distinct, so ON CONFLICT alone would duplicate rows).

CREATE TABLE IF NOT EXISTS floor_requisition (
    requisition_id     BIGINT PRIMARY KEY,
    job_card_id        BIGINT NOT NULL REFERENCES job_card_v2(job_card_id) ON DELETE RESTRICT,
    warehouse          TEXT   NOT NULL,
    floor              TEXT   NOT NULL,
    material_sku_name  TEXT   NOT NULL,
    item_type          TEXT,

    requested_qty      NUMERIC(14,3) NOT NULL CHECK (requested_qty > 0),
    requested_unit     TEXT          NOT NULL CHECK (requested_unit IN ('kg','pcs')),

    required_qty       NUMERIC(14,3),
    required_unit      TEXT,
    available_qty      NUMERIC(14,3) NOT NULL,
    available_unit     TEXT          NOT NULL,
    shortage_qty       NUMERIC(14,3),
    shortage_unit      TEXT,

    issued_qty         NUMERIC(14,3) CHECK (issued_qty > 0),
    issued_unit        TEXT,

    status             TEXT NOT NULL DEFAULT 'raised'
                       CHECK (status IN ('raised','issued','received','cancelled')),
    note               TEXT,
    issue_note         TEXT,
    cancel_reason      TEXT,

    raised_by          TEXT NOT NULL,
    raised_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    issued_by          TEXT,
    issued_at          TIMESTAMPTZ,
    received_by        TEXT,
    received_at        TIMESTAMPTZ,
    cancelled_by       TEXT,
    cancelled_at       TIMESTAMPTZ,

    CHECK (available_unit = requested_unit),
    CHECK (required_unit IS NULL OR required_unit = requested_unit),
    CHECK (shortage_unit IS NULL OR shortage_unit = requested_unit),
    CHECK (issued_unit   IS NULL OR issued_unit   = requested_unit),
    CHECK ((required_qty IS NULL) = (required_unit IS NULL)),
    CHECK ((shortage_qty IS NULL) = (shortage_unit IS NULL)),
    CHECK ((issued_qty   IS NULL) = (issued_unit   IS NULL)),
    CHECK (status NOT IN ('issued','received') OR (issued_qty IS NOT NULL AND issued_by IS NOT NULL)),
    CHECK (status <> 'received'  OR received_by IS NOT NULL),
    CHECK (status <> 'cancelled' OR (cancelled_by IS NOT NULL AND BTRIM(COALESCE(cancel_reason, '')) <> ''))
);

-- One open request per job card + article.
CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_open
    ON floor_requisition (job_card_id, UPPER(BTRIM(material_sku_name)))
    WHERE status = 'raised';

CREATE INDEX IF NOT EXISTS idx_floor_requisition_place
    ON floor_requisition (warehouse, floor, status, raised_at DESC);

CREATE INDEX IF NOT EXISTS idx_floor_requisition_job_card
    ON floor_requisition (job_card_id, raised_at DESC);

-- ── Permission catalog ──────────────────────────────────────────────────────
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT v.module, v.sub_module, NULL, v.action, v.description
  FROM (VALUES
      ('production', 'floor_requisitions', 'view',    'View floor requisitions'),
      ('production', 'floor_requisitions', 'create',  'Raise a floor requisition from a job card'),
      ('production', 'floor_requisitions', 'issue',   'Issue material against a floor requisition'),
      ('production', 'floor_requisitions', 'receive', 'Confirm a floor requisition was received'),
      ('production', 'floor_requisitions', 'cancel',  'Cancel a raised floor requisition')
  ) AS v(module, sub_module, action, description)
 WHERE NOT EXISTS (
     SELECT 1
       FROM auth_permission p
      WHERE p.module         = v.module
        AND p.sub_module     IS NOT DISTINCT FROM v.sub_module
        AND p.sub_sub_module IS NULL
        AND p.action         = v.action
 );

-- ── Grants ──────────────────────────────────────────────────────────────────
-- floor_manager raises and receives; store_head issues; both may cancel a
-- request still waiting (floor: raised by mistake; store: cannot supply).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM (VALUES
      ('admin', 'view'), ('admin', 'create'), ('admin', 'issue'), ('admin', 'receive'), ('admin', 'cancel'),
      ('floor_manager', 'view'), ('floor_manager', 'create'), ('floor_manager', 'receive'), ('floor_manager', 'cancel'),
      ('store_head', 'view'), ('store_head', 'issue'), ('store_head', 'cancel')
  ) AS g(role_name, action)
  JOIN auth_role r       ON r.role_name = g.role_name
  JOIN auth_permission p ON p.module = 'production'
                        AND p.sub_module = 'floor_requisitions'
                        AND p.sub_sub_module IS NULL
                        AND p.action = g.action
ON CONFLICT DO NOTHING;
```

Then in `scripts/migrate.py`, directly after the line `    DB_DIR / "110_stocktake_txn_verification.sql",` add:

```python
    # 111 creates floor_requisition (the job card tab's Request → store Issue →
    # floor Received record; tracking only, touches no stock-take table), its
    # one-open-request-per-article index, and production.floor_requisitions.*
    # permissions granted to admin / floor_manager / store_head. MUST follow 017
    # (job_card_v2) and 085 (store_head). Idempotent.
    DB_DIR / "111_floor_requisition.sql",
```

Convert the new SQL file to CRLF:
`.venv/Scripts/python.exe -c "import pathlib; p=pathlib.Path(r'C:\Candor\Consumption\server_replica\app\db\111_floor_requisition.sql'); p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))"`

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_floor_requisition_migration.py`
Expected: 6 passed.

- [ ] **Step 5: Checkpoint** — changed: `app/db/111_floor_requisition.sql` (new), `scripts/migrate.py`, `tests/services/test_floor_requisition_migration.py` (new). No commit.

---

### Task 2: Shared place scope

**Files:**
- Create: `server_replica/app/modules/stock_take/place_scope.py`
- Modify: `server_replica/app/modules/stock_take/router.py` — imports block (line ~51) and `_floor_stock_place` (lines ~712-742)
- Test: `server_replica/tests/services/test_place_scope.py`; existing `tests/services/test_stock_take_floor_stock.py` must stay green

**Interfaces:**
- Produces:
  - `place_scope.granted(user) -> tuple[list[str], list[str]]` — (sorted normalised warehouse codes, sorted upper-cased floors); an empty list means no limit on that axis.
  - `place_scope.assert_place_allowed(user, warehouse: str, floor: str) -> None` — raises `HTTPException(403)` with `detail["error"]` `warehouse_not_allowed` / `floor_not_allowed`.

- [ ] **Step 1: Write the failing test**

```python
"""place_scope — which warehouse + floor a caller may act on, read off grants."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.stock_take import place_scope as P


class U:
    def __init__(self, warehouses, floors):
        self.allowed_warehouses = warehouses
        self.allowed_floors = floors


def test_no_grants_means_anywhere():
    assert P.assert_place_allowed(U([], []), "A185", "Ground Floor") is None
    assert P.assert_place_allowed(U(None, None), "W202", "Terrace") is None


def test_both_warehouse_spellings_are_the_same_building():
    P.assert_place_allowed(U(["W-202"], []), "W202", "Terrace")
    P.assert_place_allowed(U(["W202"], []), "W-202", "Terrace")


def test_another_warehouse_is_refused():
    with pytest.raises(HTTPException) as exc:
        P.assert_place_allowed(U(["A185"], []), "W-202", "First Floor")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "warehouse_not_allowed"


def test_a_floor_grant_matches_ignoring_case_and_padding():
    user = U(["W202"], [" Terrace "])
    P.assert_place_allowed(user, "W202", "terrace")
    with pytest.raises(HTTPException) as exc:
        P.assert_place_allowed(user, "W202", "First Floor")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "floor_not_allowed"


def test_granted_normalises_and_dedupes():
    assert P.granted(U(["W-202", "W202", " "], [" terrace", "First Floor"])) == (
        ["W202"], ["FIRST FLOOR", "TERRACE"])
    assert P.granted(U(None, None)) == ([], [])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_place_scope.py`
Expected: FAIL — `ImportError: cannot import name 'place_scope'`.

- [ ] **Step 3: Implement (CRLF)**

`server_replica/app/modules/stock_take/place_scope.py`:

```python
"""Which warehouse + floor a caller may act on.

Read straight off the caller's GRANTS (auth_user.allowed_warehouses /
allowed_floors), not off the places that already hold counts: a job card's
floor can hold nothing yet, and refusing it would report a permissions problem
where the truth is "nothing recorded here". Empty grants mean "no restriction"
(auth_schema.sql:35).

Shared by the stock-take floor-stock read and floor requisitions, so a floor is
open or closed to someone the same way on both screens.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.stock_take.services.transactions_service import _normalise_warehouse


def granted(user) -> tuple[list[str], list[str]]:
    """(warehouses, floors) the caller is limited to: normalised warehouse codes
    and upper-cased floors, sorted. An empty list means no limit on that axis."""
    warehouses = sorted({_normalise_warehouse(w)
                         for w in (user.allowed_warehouses or []) if str(w).strip()})
    floors = sorted({str(f).strip().upper()
                     for f in (user.allowed_floors or []) if str(f).strip()})
    return warehouses, floors


def assert_place_allowed(user, warehouse: str, floor: str) -> None:
    """Raise 403 unless the caller's grants cover this warehouse + floor."""
    wh = _normalise_warehouse(warehouse)
    fl = (floor or "").strip()
    granted_w, granted_f = granted(user)
    if granted_w and wh not in granted_w:
        raise HTTPException(403, detail={
            "error": "warehouse_not_allowed",
            "message": f"You are not assigned to warehouse {wh!r}.",
            "details": {"requested": wh, "allowed_warehouses": granted_w}})
    if granted_f and fl.upper() not in granted_f:
        raise HTTPException(403, detail={
            "error": "floor_not_allowed",
            "message": f"You are not assigned to floor {fl!r}.",
            "details": {"requested": fl, "allowed_floors": granted_f}})
```

In `app/modules/stock_take/router.py`, after `from app.modules.stock_take import floors as _floors` add:

```python
from app.modules.stock_take import place_scope
```

and replace the body of `_floor_stock_place` (keep its docstring) from `wh = transactions_service._normalise_warehouse(warehouse)` through `return wh, fl` with:

```python
    wh = transactions_service._normalise_warehouse(warehouse)
    fl = (floor or "").strip()
    if not wh or not fl:
        raise HTTPException(400, detail={
            "error": "place_required",
            "message": "Pick a warehouse and a floor to see its stock.",
            "details": {"warehouse": warehouse, "floorName": floor}})
    place_scope.assert_place_allowed(user, wh, fl)
    return wh, fl
```

Edit `router.py` preserving CRLF (e.g. read bytes, replace, write bytes via the venv python), and write `place_scope.py` then convert it to CRLF the same way as Task 1.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_place_scope.py tests/services/test_stock_take_floor_stock.py tests/services/test_stock_take_rbac.py`
Expected: all pass (5 new + 10 floor-stock + the RBAC tests).

- [ ] **Step 5: Checkpoint** — changed: `app/modules/stock_take/place_scope.py` (new), `app/modules/stock_take/router.py`, `tests/services/test_place_scope.py` (new). No commit.

---

### Task 3: Pure requisition rules

**Files:**
- Create: `server_replica/app/modules/floor_requisition/__init__.py` (empty), `server_replica/app/modules/floor_requisition/rules.py`
- Test: `server_replica/tests/services/test_floor_requisition_rules.py`

**Interfaces:**
- Produces (all in `app.modules.floor_requisition.rules`):
  - `KG = "kg"`, `PCS = "pcs"`, `FRESH = "Fresh Stock"`
  - `article_key(name: str | None) -> str` — trimmed, upper-cased
  - `normalise_unit(uom: str | None) -> str | None` — `'kg'`, `'pcs'` or `None` when unrecognised
  - `unit_for(indent_uom: str | None, item_type: str | None) -> str`
  - `class QtyError(ValueError)` — message is user-facing
  - `parse_qty(value: Any, unit: str) -> Decimal` — quantised to 0.001; raises `QtyError`
  - `@dataclass(frozen=True) Snapshot(unit: str, required: Decimal | None, available: Decimal, shortage: Decimal | None)`
  - `snapshot(*, unit: str, indent_lines: Iterable[tuple[uom, gross_qty, reqd_qty]], stock: Iterable[Mapping]) -> Snapshot`
  - `actor_name(user) -> str`

- [ ] **Step 1: Write the failing test**

```python
"""floor_requisition.rules — units, quantities, the snapshot, the actor."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.floor_requisition import rules as R


@pytest.mark.parametrize("uom, item_type, want", [
    ("KGS", "RM", "kg"),
    ("PCS", "PM", "pcs"),
    ("Nos", None, "pcs"),
    (None, "pm", "pcs"),
    (None, "RM", "kg"),
    ("bags", "PM", "pcs"),   # unrecognised uom falls back to the type
    ("", None, "kg"),
])
def test_unit_for(uom, item_type, want):
    assert R.unit_for(uom, item_type) == want


@pytest.mark.parametrize("value, unit, want", [
    ("88.2", "kg", Decimal("88.200")),
    (88.2, "kg", Decimal("88.200")),
    ("0.001", "kg", Decimal("0.001")),
    ("1000", "pcs", Decimal("1000.000")),
    ("10.000", "pcs", Decimal("10.000")),
    ("99999999999.999", "kg", Decimal("99999999999.999")),
])
def test_parse_qty_accepts(value, unit, want):
    assert R.parse_qty(value, unit) == want


@pytest.mark.parametrize("value, unit, message", [
    ("1.2345", "kg", "Kilograms go to 3 decimals at most."),
    ("10.5", "pcs", "Pieces are whole numbers."),
    ("0", "kg", "The quantity must be more than 0."),
    ("-3", "pcs", "The quantity must be more than 0."),
    ("abc", "kg", "Enter a quantity as a number."),
    (None, "kg", "Enter a quantity as a number."),
    (True, "pcs", "Enter a quantity as a number."),
    ("NaN", "kg", "Enter a quantity as a number."),
    ("1e11", "kg", "That quantity is too large."),
    ("1e40", "pcs", "That quantity is too large."),
])
def test_parse_qty_refuses(value, unit, message):
    with pytest.raises(R.QtyError) as exc:
        R.parse_qty(value, unit)
    assert str(exc.value) == message


def _stock(kg, stock_type="Fresh Stock", pcs=0):
    return {"stock_type": stock_type, "available_kg": kg, "available_quantity": pcs}


def test_snapshot_kg_against_fresh_stock_only():
    s = R.snapshot(unit="kg", indent_lines=[("KGS", "250.000", "240.000")],
                   stock=[_stock(161.8), _stock(2.89, "Off Grade/Rejection")])
    assert s == R.Snapshot("kg", Decimal("250.000"), Decimal("161.800"), Decimal("88.200"))


def test_snapshot_pcs_reads_the_piece_count():
    s = R.snapshot(unit="pcs", indent_lines=[("PCS", 1000, 1000)], stock=[_stock(120, pcs=1200)])
    assert s == R.Snapshot("pcs", Decimal("1000.000"), Decimal("1200.000"), Decimal("0.000"))


def test_snapshot_without_an_indent_line_has_no_requirement_or_shortage():
    s = R.snapshot(unit="kg", indent_lines=[], stock=[_stock(5)])
    assert s == R.Snapshot("kg", None, Decimal("5.000"), None)


def test_snapshot_nothing_on_the_floor():
    s = R.snapshot(unit="pcs", indent_lines=[("PCS", 25, 25)], stock=[])
    assert s == R.Snapshot("pcs", Decimal("25.000"), Decimal("0.000"), Decimal("25.000"))


def test_snapshot_sums_lines_skips_other_units_and_falls_back_to_reqd():
    s = R.snapshot(unit="kg", indent_lines=[
        ("KGS", "10", "9"), ("kgs", None, "5.5"), ("PCS", "400", "400"), ("KGS", "x", None),
    ], stock=[])
    assert s.required == Decimal("15.500")


def test_snapshot_rounds_to_three_places():
    s = R.snapshot(unit="kg", indent_lines=[("KGS", "0.3", None)], stock=[_stock(0.1), _stock(0.2)])
    assert (s.available, s.shortage) == (Decimal("0.300"), Decimal("0.000"))


class FakeUser:
    def __init__(self, full_name="", email="", phone="", user_id=7):
        self.full_name, self.email, self.phone, self.user_id = full_name, email, phone, user_id


def test_actor_name_prefers_name_then_email_then_phone_then_id():
    assert R.actor_name(FakeUser(full_name="Ravi K", email="r@x")) == "Ravi K"
    assert R.actor_name(FakeUser(email="r@x", phone="98")) == "r@x"
    assert R.actor_name(FakeUser(phone="98")) == "98"
    assert R.actor_name(FakeUser()) == "user:7"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_floor_requisition_rules.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.modules.floor_requisition'`.

- [ ] **Step 3: Implement (CRLF)**

`server_replica/app/modules/floor_requisition/__init__.py`: empty file.

`server_replica/app/modules/floor_requisition/rules.py`:

```python
"""Floor requisitions — the rules that need no database.

The unit a request carries, what quantity may be stored, and the snapshot of
what the floor was looking at when it asked. These mirror the job card tab's
arithmetic (web_replica/src/lib/floorStock.ts): the requirement is the article's
indent lines' gross_qty (falling back to reqd_qty), compared against the floor's
FRESH STOCK only, in the requirement's own unit.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

KG = "kg"
PCS = "pcs"
FRESH = "Fresh Stock"

_KG = {"kg", "kgs", "kilogram", "kilograms"}
_PCS = {"pc", "pcs", "piece", "pieces", "no", "nos", "unit", "units"}
_THOUSANDTH = Decimal("0.001")
# NUMERIC(14,3) holds up to 99,999,999,999.999.
_LIMIT = Decimal("100000000000")


def article_key(name: Optional[str]) -> str:
    """An article's identity: the stock-take module's UPPER(BTRIM(name))."""
    return (name or "").strip().upper()


def normalise_unit(uom: Optional[str]) -> Optional[str]:
    """'KGS' -> 'kg', 'PCS' / 'NOS' -> 'pcs'; None when it is neither."""
    s = (uom or "").strip().lower()
    if s in _KG:
        return KG
    if s in _PCS:
        return PCS
    return None


def unit_for(indent_uom: Optional[str], item_type: Optional[str]) -> str:
    """The request's unit: its indent line's, else pieces for PM, else kg."""
    return normalise_unit(indent_uom) or (PCS if article_key(item_type) == "PM" else KG)


class QtyError(ValueError):
    """A quantity that cannot be stored. The message is shown to the person as-is."""


def parse_qty(value: Any, unit: str) -> Decimal:
    """A positive quantity in `unit`: whole for pieces, at most 3 decimals for kg."""
    if isinstance(value, bool):
        raise QtyError("Enter a quantity as a number.")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise QtyError("Enter a quantity as a number.") from None
    if not d.is_finite():
        raise QtyError("Enter a quantity as a number.")
    if d <= 0:
        raise QtyError("The quantity must be more than 0.")
    if d >= _LIMIT:
        raise QtyError("That quantity is too large.")
    if unit == PCS and d != d.to_integral_value():
        raise QtyError("Pieces are whole numbers.")
    if d != d.quantize(_THOUSANDTH, rounding=ROUND_DOWN):
        raise QtyError("Kilograms go to 3 decimals at most.")
    return d.quantize(_THOUSANDTH)


def _num(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _round3(d: Decimal) -> Decimal:
    # `+ Decimal(0)` turns -0.000 into 0.000.
    return d.quantize(_THOUSANDTH, rounding=ROUND_HALF_UP) + Decimal(0)


@dataclass(frozen=True)
class Snapshot:
    unit: str
    required: Optional[Decimal]
    available: Decimal
    shortage: Optional[Decimal]


def snapshot(
    *,
    unit: str,
    indent_lines: Iterable[tuple[Any, Any, Any]],
    stock: Iterable[Mapping[str, Any]],
) -> Snapshot:
    """What the floor saw when it asked, for ONE article.

    indent_lines: (uom, gross_qty, reqd_qty) of the article's indent lines.
    stock: the article's floor-stock rows (floor_stock_service items).
    A line in another recognised unit cannot be added to this one and is skipped;
    a line with an unrecognised uom is counted. No usable line = no requirement,
    so no shortage either.
    """
    required: Optional[Decimal] = None
    for uom, gross, reqd in indent_lines:
        line_unit = normalise_unit(uom)
        if line_unit is not None and line_unit != unit:
            continue
        q = _num(gross)
        if q is None:
            q = _num(reqd)
        if q is None:
            continue
        required = q if required is None else required + q

    field = "available_kg" if unit == KG else "available_quantity"
    available = _round3(sum(
        (_num(s.get(field)) or Decimal(0) for s in stock if s.get("stock_type") == FRESH),
        Decimal(0),
    ))
    if required is None:
        return Snapshot(unit, None, available, None)
    required = _round3(required)
    return Snapshot(unit, required, available, _round3(max(required - available, Decimal(0))))


def actor_name(user) -> str:
    """Who did it, from the access token — never from the request body.

    The production router's _actor_name rule: a session may carry an empty
    full_name, so fall back to email, then phone, then the numeric id.
    """
    return user.full_name or user.email or user.phone or f"user:{user.user_id}"
```

Convert both new files to CRLF as in Task 1.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_floor_requisition_rules.py`
Expected: all pass (7 + 6 + 10 + 7 = 30).

- [ ] **Step 5: Checkpoint** — changed: `app/modules/floor_requisition/__init__.py`, `app/modules/floor_requisition/rules.py`, `tests/services/test_floor_requisition_rules.py` (all new). No commit.

---

### Task 4: Requisition service (database)

**Files:**
- Create: `server_replica/app/modules/floor_requisition/services/__init__.py` (empty), `server_replica/app/modules/floor_requisition/services/requisition_service.py`
- Test: `server_replica/tests/services/test_floor_requisition_service.py`

**Interfaces:**
- Consumes: Task 2 `place_scope.granted`, `place_scope.assert_place_allowed`; Task 3 `rules.*`; `floor_stock_service.fetch_floor_stock(conn, *, warehouse, floor) -> {"items": [...]}`; `app.core.helpers.new_short_time_id`, `insert_with_pk_retry`.
- Produces (module `app.modules.floor_requisition.services.requisition_service`):
  - `class RequisitionError(Exception)` with `.status: int`, `.error: str`, `.message: str`, `.details: dict`
  - `COLS: str`, `STATUSES = ("raised", "issued", "received", "cancelled")`, `MAX_PAGE_SIZE = 500`
  - `row_out(row) -> dict` — quantities as `float | None`, timestamps as ISO strings
  - `async raise_requisition(conn, user, *, job_card_id: int, material_sku_name: str, requested_qty: Any, note: str | None) -> dict`
  - `async list_requisitions(conn, user, *, status=None, warehouse=None, floor=None, job_card_id=None, search=None, page=1, page_size=100) -> {"items", "total", "page", "page_size"}`
  - `async issue_requisition(conn, user, requisition_id: int, *, issued_qty: Any, issue_note: str | None) -> dict`
  - `async receive_requisition(conn, user, requisition_id: int) -> dict`
  - `async cancel_requisition(conn, user, requisition_id: int, *, reason: str | None) -> dict`
  - Writes must be called inside an open transaction (the router provides it).

- [ ] **Step 1: Write the failing test**

```python
"""requisition_service against a scripted fake connection.

The fake answers each statement by what it reads from, so these tests pin the
decisions the service makes (place, unit, snapshot, scope, refusals, guarded
transitions) without a database. The SQL runs for real only on RDS.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
import pytest
from fastapi import HTTPException

from app.modules.floor_requisition.services import requisition_service as svc

COLS = [c.strip() for c in svc.COLS.split(",")]
RAISED_AT = datetime(2026, 9, 15, 8, 35, tzinfo=timezone.utc)
INSERT_COLS = ["requisition_id", "job_card_id", "warehouse", "floor", "material_sku_name",
               "item_type", "requested_qty", "requested_unit", "required_qty", "required_unit",
               "available_qty", "available_unit", "shortage_qty", "shortage_unit",
               "note", "raised_by"]
PISTA = "California Pista Inshell Roasted and Salted"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, *, job_card=None, bom=None, indents=(), insert_error=None, open_id=None,
                 row=None, update_row=None, status=None, total=0, rows=()):
        self.job_card, self.bom, self.indents = job_card, bom, list(indents)
        self.insert_error, self.open_id = insert_error, open_id
        self.row, self.update_row, self.status = row, update_row, status
        self.total, self.rows = total, list(rows)
        self.calls: list[tuple[str, str, tuple]] = []

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
        if s.startswith("SELECT job_card_id, factory, floor, bom_id FROM job_card_v2"):
            return self.job_card
        if "FROM bom_line" in s:
            return self.bom
        if s.startswith("INSERT INTO floor_requisition"):
            if self.insert_error:
                raise self.insert_error
            row = dict.fromkeys(COLS)
            row.update(zip(INSERT_COLS, args))
            row.update(status="raised", raised_at=RAISED_AT)
            return row
        if s.startswith("UPDATE floor_requisition"):
            return self.update_row
        if "FROM floor_requisition" in s:
            return self.row
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        if "job_card_rm_indent_v2" in s:
            return self.indents
        if "FROM floor_requisition" in s:
            return self.rows
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        if s.startswith("SELECT COUNT(*) FROM floor_requisition"):
            return self.total
        if s.startswith("SELECT requisition_id FROM floor_requisition"):
            return self.open_id
        if s.startswith("SELECT status FROM floor_requisition"):
            return self.status
        raise AssertionError(f"unexpected fetchval: {s[:90]}")


class User:
    def __init__(self, full_name="Ravi K", warehouses=(), floors=()):
        self.full_name, self.email, self.phone, self.user_id = full_name, "", "", 7
        self.allowed_warehouses, self.allowed_floors = list(warehouses), list(floors)


def _jc(**over):
    return {"job_card_id": 12345678, "factory": "W-202", "floor": " First Floor ", "bom_id": 9, **over}


def _row(**over):
    row = dict.fromkeys(COLS)
    row.update(requisition_id=87654321, job_card_id=12345678, warehouse="W202", floor="First Floor",
               material_sku_name="PM24-Pouch 250 gm", item_type="PM",
               requested_qty=Decimal("1000.000"), requested_unit="pcs",
               available_qty=Decimal("0.000"), available_unit="pcs",
               status="raised", raised_by="Floor F", raised_at=RAISED_AT)
    row.update(over)
    return row


@pytest.fixture
def floor_stock(monkeypatch):
    seen = {}

    async def fake(conn, *, warehouse, floor):
        seen["place"] = (warehouse, floor)
        return {"warehouse": warehouse, "floor": floor, "items": seen.get("items", [])}

    monkeypatch.setattr(svc.floor_stock_service, "fetch_floor_stock", fake)
    return seen


def _raise(conn, user=None, **kw):
    kw.setdefault("job_card_id", 12345678)
    kw.setdefault("material_sku_name", PISTA)
    kw.setdefault("requested_qty", "88.2")
    kw.setdefault("note", None)
    return asyncio.run(svc.raise_requisition(conn, user or User(), **kw))


def _refusal(fn):
    with pytest.raises(svc.RequisitionError) as exc:
        fn()
    return exc.value


# ── raise ──────────────────────────────────────────────────────────────────

def test_raise_takes_place_unit_and_snapshot_from_the_database(floor_stock):
    floor_stock["items"] = [
        {"item_name": PISTA.upper() + " ", "stock_type": "Fresh Stock", "available_kg": 161.8, "available_quantity": 0},
        {"item_name": PISTA, "stock_type": "Off Grade/Rejection", "available_kg": 2.89, "available_quantity": 0},
        {"item_name": "Walnut", "stock_type": "Fresh Stock", "available_kg": 999, "available_quantity": 0},
    ]
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": PISTA, "item_type": "rm"},
                    indents=[{"item_type": "RM", "material_sku_name": PISTA, "uom": "KGS",
                              "gross_qty": Decimal("250.000"), "reqd_qty": Decimal("240"), "line_id": 1}])
    out = _raise(conn, material_sku_name=f"  {PISTA.lower()} ", note="  urgent ")

    assert floor_stock["place"] == ("W202", "First Floor")
    assert out["warehouse"] == "W202" and out["floor"] == "First Floor"
    assert out["material_sku_name"] == PISTA and out["item_type"] == "RM"
    assert (out["requested_qty"], out["requested_unit"]) == (88.2, "kg")
    assert (out["required_qty"], out["required_unit"]) == (250.0, "kg")
    assert (out["available_qty"], out["available_unit"]) == (161.8, "kg")
    assert (out["shortage_qty"], out["shortage_unit"]) == (88.2, "kg")
    assert out["note"] == "urgent" and out["raised_by"] == "Ravi K"
    assert out["status"] == "raised" and out["raised_at"] == RAISED_AT.isoformat()
    bom_call = next(c for c in conn.calls if "FROM bom_line" in c[1])
    assert bom_call[2] == (9, PISTA.upper())


def test_raise_mints_the_8_digit_number_through_the_pk_retry(floor_stock, monkeypatch):
    used = []

    async def spy(conn, insert):
        used.append(insert)
        return await insert()

    monkeypatch.setattr(svc, "insert_with_pk_retry", spy)
    monkeypatch.setattr(svc, "new_short_time_id", lambda: 12349876)
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": PISTA, "item_type": "RM"})
    assert _raise(conn)["requisition_id"] == 12349876
    assert len(used) == 1


def test_raise_for_a_missing_job_card_is_404(floor_stock):
    err = _refusal(lambda: _raise(FakeConn(job_card=None)))
    assert (err.status, err.error) == (404, "job_card_not_found")


def test_raise_on_a_job_card_without_a_floor_is_422(floor_stock):
    err = _refusal(lambda: _raise(FakeConn(job_card=_jc(floor=None))))
    assert (err.status, err.error) == (422, "job_card_has_no_place")


def test_raise_outside_the_callers_warehouses_is_403(floor_stock):
    with pytest.raises(HTTPException) as exc:
        _raise(FakeConn(job_card=_jc()), User(warehouses=["A185"]))
    assert exc.value.status_code == 403


def test_raise_for_an_article_not_on_the_job_card_is_422_and_reads_no_stock(floor_stock):
    err = _refusal(lambda: _raise(FakeConn(job_card=_jc(), bom=None, indents=[])))
    assert (err.status, err.error) == (422, "article_not_on_job_card")
    assert "place" not in floor_stock


def test_raise_pm_without_an_indent_line_is_in_pieces(floor_stock):
    floor_stock["items"] = [{"item_name": "PM24-Pouch 250 gm", "stock_type": "Fresh Stock",
                             "available_kg": 6.0, "available_quantity": 120}]
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": "PM24-Pouch 250 gm", "item_type": "PM"})
    err = _refusal(lambda: _raise(conn, material_sku_name="PM24-Pouch 250 gm", requested_qty="10.5"))
    assert (err.status, err.error) == (400, "qty_invalid")
    assert err.message == "Pieces are whole numbers."

    out = _raise(conn, material_sku_name="PM24-Pouch 250 gm", requested_qty="1000")
    assert (out["requested_qty"], out["requested_unit"]) == (1000.0, "pcs")
    assert (out["available_qty"], out["available_unit"]) == (120.0, "pcs")
    assert out["required_qty"] is None and out["required_unit"] is None
    assert out["shortage_qty"] is None and out["shortage_unit"] is None


def test_a_second_open_request_for_the_article_is_409_naming_the_first(floor_stock):
    clash = asyncpg.UniqueViolationError(
        'duplicate key value violates unique constraint "uq_floor_requisition_open"')
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": PISTA, "item_type": "RM"},
                    insert_error=clash, open_id=87654321)
    err = _refusal(lambda: _raise(conn))
    assert (err.status, err.error) == (409, "open_requisition_exists")
    assert err.details["requisition_id"] == 87654321


def test_any_other_unique_violation_is_not_swallowed(floor_stock):
    other = asyncpg.UniqueViolationError('duplicate key value violates unique constraint "some_other_idx"')
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": PISTA, "item_type": "RM"}, insert_error=other)
    with pytest.raises(asyncpg.UniqueViolationError):
        _raise(conn)


# ── issue / receive / cancel ───────────────────────────────────────────────

def _update_call(conn):
    return next(c for c in conn.calls if c[1].startswith("UPDATE floor_requisition"))


def test_issue_moves_raised_to_issued_in_the_requested_unit():
    conn = FakeConn(row=_row(), update_row=_row(status="issued", issued_qty=Decimal("500.000"),
                                                issued_unit="pcs", issued_by="Store S"))
    out = asyncio.run(svc.issue_requisition(conn, User(full_name="Store S"), 87654321,
                                            issued_qty="500", issue_note=" part "))
    assert out["status"] == "issued" and out["issued_qty"] == 500.0
    sql, args = _update_call(conn)[1:]
    assert "issued_unit = requested_unit" in sql and "status = $2" in sql
    assert args == (87654321, "raised", Decimal("500.000"), "part", "Store S")


def test_issue_quantity_follows_the_unit_rules():
    err = _refusal(lambda: asyncio.run(svc.issue_requisition(
        FakeConn(row=_row()), User(), 87654321, issued_qty="12.5", issue_note=None)))
    assert (err.status, err.error) == (400, "qty_invalid")


def test_a_request_someone_already_moved_on_is_409_with_its_status():
    conn = FakeConn(row=_row(), update_row=None, status="issued")
    err = _refusal(lambda: asyncio.run(svc.issue_requisition(conn, User(), 87654321, issued_qty="5", issue_note=None)))
    assert (err.status, err.error) == (409, "status_changed")
    assert err.details["status"] == "issued"


def test_a_missing_requisition_is_404():
    err = _refusal(lambda: asyncio.run(svc.receive_requisition(FakeConn(row=None), User(), 1)))
    assert (err.status, err.error) == (404, "not_found")


def test_acting_on_another_floors_request_is_403():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.receive_requisition(FakeConn(row=_row()), User(floors=["Terrace"]), 87654321))
    assert exc.value.status_code == 403


def test_receive_moves_issued_to_received():
    conn = FakeConn(row=_row(status="issued"), update_row=_row(status="received", received_by="Ravi K"))
    out = asyncio.run(svc.receive_requisition(conn, User(), 87654321))
    assert out["status"] == "received"
    assert _update_call(conn)[2] == (87654321, "issued", "Ravi K")


def test_cancel_needs_a_reason():
    err = _refusal(lambda: asyncio.run(svc.cancel_requisition(FakeConn(row=_row()), User(), 87654321, reason="   ")))
    assert (err.status, err.error) == (400, "reason_required")


def test_cancel_moves_raised_to_cancelled():
    conn = FakeConn(row=_row(), update_row=_row(status="cancelled", cancel_reason="wrong article"))
    asyncio.run(svc.cancel_requisition(conn, User(), 87654321, reason=" wrong article "))
    assert _update_call(conn)[2] == (87654321, "raised", "wrong article", "Ravi K")


# ── list ───────────────────────────────────────────────────────────────────

def test_list_is_limited_to_the_callers_places_and_filters():
    conn = FakeConn(total=1, rows=[_row()])
    out = asyncio.run(svc.list_requisitions(conn, User(warehouses=["W-202"], floors=["first floor"]),
                                            status="raised", search="pouch 100%", page_size=9999))
    count_sql, count_args = next((c[1], c[2]) for c in conn.calls if "COUNT(*)" in c[1])
    assert "warehouse = ANY($1)" in count_sql and "UPPER(floor) = ANY($2)" in count_sql
    assert "status = $3" in count_sql
    assert count_args == (["W202"], ["FIRST FLOOR"], "raised", "%POUCH%", "%100\\%%")
    list_sql, list_args = next((c[1], c[2]) for c in conn.calls if c[0] == "fetch")
    assert "ORDER BY raised_at DESC, requisition_id DESC" in list_sql
    assert list_args[-2:] == (500, 0)
    assert out["total"] == 1 and out["page"] == 1 and out["page_size"] == 500
    assert out["items"][0]["requested_qty"] == 1000.0


def test_list_for_one_job_card_without_limits():
    conn = FakeConn(total=0, rows=[])
    asyncio.run(svc.list_requisitions(conn, User(), job_card_id=12345678, page=3, page_size=100))
    count_sql, count_args = next((c[1], c[2]) for c in conn.calls if "COUNT(*)" in c[1])
    assert count_sql.endswith("WHERE job_card_id = $1") and count_args == (12345678,)
    assert next(c[2] for c in conn.calls if c[0] == "fetch")[-2:] == (100, 200)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_floor_requisition_service.py`
Expected: FAIL — `ModuleNotFoundError: ...floor_requisition.services`.

- [ ] **Step 3: Implement (CRLF)**

`server_replica/app/modules/floor_requisition/services/__init__.py`: empty.

`server_replica/app/modules/floor_requisition/services/requisition_service.py`:

```python
"""Floor requisitions — raise, list, issue, receive, cancel.

One row of floor_requisition (app/db/111_floor_requisition.sql) per request for
one article. Raised by the floor from the job card tab, issued by store, received
by the floor; cancelled while still raised. TRACKING ONLY: nothing here touches
new_stock_entries or stocktake_transactions.

The browser sends only the job card, the article, the quantity and a note. The
place, the unit and the snapshot of what the floor was looking at are read from
the database here, so a request cannot claim a floor, unit or shortage the job
card does not have.

Every write must run inside the caller's transaction. Refusals are raised as
RequisitionError (the router turns them into HTTP errors); a place outside the
caller's grants raises place_scope's HTTPException directly.
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.floor_requisition import rules
from app.modules.stock_take import place_scope
from app.modules.stock_take.services import floor_stock_service
from app.modules.stock_take.services.transactions_service import _normalise_warehouse

OPEN_INDEX = "uq_floor_requisition_open"
STATUSES = ("raised", "issued", "received", "cancelled")
MAX_PAGE_SIZE = 500
_TEXT_LIMIT = 500

COLS = """requisition_id, job_card_id, warehouse, floor, material_sku_name, item_type,
          requested_qty, requested_unit, required_qty, required_unit,
          available_qty, available_unit, shortage_qty, shortage_unit,
          issued_qty, issued_unit, status, note, issue_note, cancel_reason,
          raised_by, raised_at, issued_by, issued_at, received_by, received_at,
          cancelled_by, cancelled_at"""

_QTY = ("requested_qty", "required_qty", "available_qty", "shortage_qty", "issued_qty")
_TS = ("raised_at", "issued_at", "received_at", "cancelled_at")

# The article's indent lines on this job card, RM before PM, in line order.
_INDENTS_SQL = """
    SELECT 'RM' AS item_type, material_sku_name, uom, gross_qty, reqd_qty, rm_indent_id AS line_id
      FROM job_card_rm_indent_v2
     WHERE job_card_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2
    UNION ALL
    SELECT 'PM', material_sku_name, uom, gross_qty, reqd_qty, pm_indent_id
      FROM job_card_pm_indent_v2
     WHERE job_card_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2
     ORDER BY item_type DESC, line_id
"""


class RequisitionError(Exception):
    """A refusal: the router answers HTTP `status` with {error, message, details}."""

    # The HTTP status is `http_status`, not `status`: a detail key named
    # `status` (the requisition's current status, on a 409) would otherwise
    # collide with it in **details.
    def __init__(self, http_status: int, error: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = http_status
        self.error = error
        self.message = message
        self.details = details


def row_out(row) -> dict[str, Any]:
    out = dict(row)
    for k in _QTY:
        out[k] = float(out[k]) if out.get(k) is not None else None
    for k in _TS:
        out[k] = out[k].isoformat() if out.get(k) is not None else None
    return out


def _clean(text: Optional[str]) -> Optional[str]:
    t = (text or "").strip()
    return t[:_TEXT_LIMIT] or None


def _not_found(requisition_id: int) -> RequisitionError:
    return RequisitionError(404, "not_found", f"Requisition {requisition_id} does not exist.",
                            requisition_id=requisition_id)


def _qty_or_refuse(value: Any, unit: str) -> Any:
    try:
        return rules.parse_qty(value, unit)
    except rules.QtyError as exc:
        raise RequisitionError(400, "qty_invalid", str(exc), unit=unit) from None


def _is_open_clash(exc: asyncpg.UniqueViolationError) -> bool:
    return (getattr(exc, "constraint_name", None) or "") == OPEN_INDEX or OPEN_INDEX in str(exc)


async def raise_requisition(conn, user, *, job_card_id: int, material_sku_name: str,
                            requested_qty: Any, note: Optional[str]) -> dict[str, Any]:
    jc = await conn.fetchrow(
        "SELECT job_card_id, factory, floor, bom_id FROM job_card_v2 WHERE job_card_id = $1",
        job_card_id)
    if not jc:
        raise RequisitionError(404, "job_card_not_found", f"Job card {job_card_id} does not exist.",
                               job_card_id=job_card_id)
    warehouse = _normalise_warehouse(jc["factory"])
    floor = (jc["floor"] or "").strip()
    if not warehouse or not floor:
        raise RequisitionError(422, "job_card_has_no_place",
                               "This job card has no plant or floor, so material cannot be requested to it.",
                               job_card_id=job_card_id)
    place_scope.assert_place_allowed(user, warehouse, floor)

    key = rules.article_key(material_sku_name)
    bom = None
    indents: list = []
    if key:
        if jc["bom_id"] is not None:
            bom = await conn.fetchrow(
                "SELECT material_sku_name, item_type FROM bom_line "
                "WHERE bom_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2 "
                "ORDER BY line_number LIMIT 1",
                jc["bom_id"], key)
        indents = list(await conn.fetch(_INDENTS_SQL, job_card_id, key))
    if bom is None and not indents:
        raise RequisitionError(422, "article_not_on_job_card",
                               f"{(material_sku_name or '').strip()!r} is not on this job card's BOM or indents.",
                               material_sku_name=material_sku_name)

    first = bom if bom is not None else indents[0]
    article = first["material_sku_name"].strip()
    item_type = rules.article_key(first["item_type"]) or None
    unit = rules.unit_for(indents[0]["uom"] if indents else None, item_type)
    qty = _qty_or_refuse(requested_qty, unit)

    stock = await floor_stock_service.fetch_floor_stock(conn, warehouse=warehouse, floor=floor)
    snap = rules.snapshot(
        unit=unit,
        indent_lines=[(line["uom"], line["gross_qty"], line["reqd_qty"]) for line in indents],
        stock=[s for s in stock["items"] if rules.article_key(s["item_name"]) == key],
    )
    actor = rules.actor_name(user)

    async def _insert():
        # A fresh number on EACH attempt, so a same-millisecond clash can retry.
        return await conn.fetchrow(
            f"""
            INSERT INTO floor_requisition (
                requisition_id, job_card_id, warehouse, floor, material_sku_name, item_type,
                requested_qty, requested_unit, required_qty, required_unit,
                available_qty, available_unit, shortage_qty, shortage_unit,
                note, raised_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            RETURNING {COLS}
            """,
            new_short_time_id(), job_card_id, warehouse, floor, article, item_type,
            qty, unit,
            snap.required, unit if snap.required is not None else None,
            snap.available, unit,
            snap.shortage, unit if snap.shortage is not None else None,
            _clean(note), actor,
        )

    try:
        row = await insert_with_pk_retry(conn, _insert)
    except asyncpg.UniqueViolationError as exc:
        if not _is_open_clash(exc):
            raise
        open_id = await conn.fetchval(
            "SELECT requisition_id FROM floor_requisition "
            "WHERE job_card_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2 AND status = 'raised'",
            job_card_id, key)
        raise RequisitionError(409, "open_requisition_exists",
                               f"Requisition {open_id} for this article is still waiting for store.",
                               requisition_id=open_id) from None
    return row_out(row)


async def _load(conn, user, requisition_id: int):
    row = await conn.fetchrow(f"SELECT {COLS} FROM floor_requisition WHERE requisition_id = $1",
                              requisition_id)
    if not row:
        raise _not_found(requisition_id)
    place_scope.assert_place_allowed(user, row["warehouse"], row["floor"])
    return row


async def _transition(conn, requisition_id: int, from_status: str, set_sql: str,
                      *args: Any) -> dict[str, Any]:
    """One guarded UPDATE: it moves the row only if it is still in `from_status`,
    so two people acting at once cannot both win. set_sql's parameters start at $3."""
    row = await conn.fetchrow(
        f"UPDATE floor_requisition SET {set_sql} "
        f"WHERE requisition_id = $1 AND status = $2 RETURNING {COLS}",
        requisition_id, from_status, *args)
    if row:
        return row_out(row)
    current = await conn.fetchval("SELECT status FROM floor_requisition WHERE requisition_id = $1",
                                  requisition_id)
    if current is None:
        raise _not_found(requisition_id)
    raise RequisitionError(409, "status_changed",
                           f"Requisition {requisition_id} is already {current}. Reload to see it.",
                           requisition_id=requisition_id, status=current)


async def issue_requisition(conn, user, requisition_id: int, *, issued_qty: Any,
                            issue_note: Optional[str]) -> dict[str, Any]:
    row = await _load(conn, user, requisition_id)
    qty = _qty_or_refuse(issued_qty, row["requested_unit"])
    return await _transition(
        conn, requisition_id, "raised",
        "status = 'issued', issued_qty = $3, issued_unit = requested_unit, "
        "issue_note = $4, issued_by = $5, issued_at = now()",
        qty, _clean(issue_note), rules.actor_name(user))


async def receive_requisition(conn, user, requisition_id: int) -> dict[str, Any]:
    await _load(conn, user, requisition_id)
    return await _transition(
        conn, requisition_id, "issued",
        "status = 'received', received_by = $3, received_at = now()",
        rules.actor_name(user))


async def cancel_requisition(conn, user, requisition_id: int, *,
                             reason: Optional[str]) -> dict[str, Any]:
    await _load(conn, user, requisition_id)
    why = _clean(reason)
    if not why:
        raise RequisitionError(400, "reason_required", "Give a reason for cancelling.",
                               requisition_id=requisition_id)
    return await _transition(
        conn, requisition_id, "raised",
        "status = 'cancelled', cancel_reason = $3, cancelled_by = $4, cancelled_at = now()",
        why, rules.actor_name(user))


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def list_requisitions(conn, user, *, status: Optional[str] = None,
                            warehouse: Optional[str] = None, floor: Optional[str] = None,
                            job_card_id: Optional[int] = None, search: Optional[str] = None,
                            page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """Newest raised first, only inside the caller's granted places."""
    where: list[str] = []
    args: list[Any] = []

    def add(sql: str, value: Any) -> None:
        args.append(value)
        where.append(sql.format(f"${len(args)}"))

    granted_w, granted_f = place_scope.granted(user)
    if granted_w:
        add("warehouse = ANY({})", granted_w)
    if granted_f:
        add("UPPER(floor) = ANY({})", granted_f)
    if status:
        add("status = {}", status)
    if warehouse and warehouse.strip():
        add("warehouse = {}", _normalise_warehouse(warehouse))
    if floor and floor.strip():
        add("UPPER(floor) = {}", floor.strip().upper())
    if job_card_id is not None:
        add("job_card_id = {}", job_card_id)
    for word in (search or "").upper().split():
        add("UPPER(material_sku_name) LIKE {}", _like(word))

    clause = f" WHERE {' AND '.join(where)}" if where else ""
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), MAX_PAGE_SIZE)
    total = await conn.fetchval(f"SELECT COUNT(*) FROM floor_requisition{clause}", *args)
    rows = await conn.fetch(
        f"SELECT {COLS} FROM floor_requisition{clause} "
        f"ORDER BY raised_at DESC, requisition_id DESC "
        f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
        *args, page_size, (page - 1) * page_size)
    return {"items": [row_out(r) for r in rows], "total": int(total or 0),
            "page": page, "page_size": page_size}
```

Convert both new files to CRLF as in Task 1.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_floor_requisition_service.py`
Expected: 19 passed.

- [ ] **Step 5: Checkpoint** — changed: `app/modules/floor_requisition/services/__init__.py`, `.../requisition_service.py`, `tests/services/test_floor_requisition_service.py` (all new). No commit.

---

### Task 5: Router, permissions wiring, app registration

**Files:**
- Create: `server_replica/app/modules/floor_requisition/router.py`
- Modify: `server_replica/app/main.py` — import next to `from app.modules.stock_take.router import router as stock_take_router` (line ~41) and `app.include_router(...)` next to `app.include_router(stock_take_router)` (line ~185). Preserve the file's line endings.
- Test: `server_replica/tests/services/test_floor_requisition_router.py`

**Interfaces:**
- Consumes: Task 4 `requisition_service` (all functions, `RequisitionError`, `MAX_PAGE_SIZE`).
- Produces (HTTP, used by Tasks 6–9):
  - `GET  /api/v1/floor-requisitions?status=&warehouse=&floorName=&job_card_id=&search=&page=&page_size=` → `{items, total, page, page_size}` (view)
  - `POST /api/v1/floor-requisitions` body `{job_card_id: int, material_sku_name: str, requested_qty: number|string, note?: string}` → row (create)
  - `POST /api/v1/floor-requisitions/{requisition_id}/issue` body `{issued_qty: number|string, issue_note?: string}` → row (issue)
  - `POST /api/v1/floor-requisitions/{requisition_id}/receive` → row (receive)
  - `POST /api/v1/floor-requisitions/{requisition_id}/cancel` body `{reason: string}` → row (cancel)
  - Errors: `{"detail": {"error", "message", "details"}}`; codes 400 `qty_invalid` / `reason_required`, 403, 404 `job_card_not_found` / `not_found`, 409 `open_requisition_exists` / `status_changed`, 422 `job_card_has_no_place` / `article_not_on_job_card`.

- [ ] **Step 1: Write the failing test**

```python
"""/api/v1/floor-requisitions — permission wiring and HTTP shape.

The wiring half walks the mounted routes and reads the permission each one
actually resolved (the test_stock_take_rbac approach): adding an endpoint means
adding a row to EXPECTED, deliberately.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.modules.floor_requisition import router as R
from app.modules.floor_requisition.services import requisition_service as svc

EXPECTED = {
    "/api/v1/floor-requisitions": {"GET": "view", "POST": "create"},
    "/api/v1/floor-requisitions/{requisition_id}/issue": {"POST": "issue"},
    "/api/v1/floor-requisitions/{requisition_id}/receive": {"POST": "receive"},
    "/api/v1/floor-requisitions/{requisition_id}/cancel": {"POST": "cancel"},
}
ROUTES = [r for r in R.router.routes if isinstance(r, APIRoute)]


def _permission_of(route: APIRoute):
    for dep in route.dependant.dependencies:
        fn = dep.call
        code, cells = getattr(fn, "__code__", None), getattr(fn, "__closure__", None)
        if not code or not cells:
            continue
        names = dict(zip(code.co_freevars, (c.cell_contents for c in cells)))
        if "module" in names and "action" in names:
            return names["module"], names.get("sub_module"), names["action"]
    return None


def test_every_route_is_accounted_for():
    seen = {(r.path, m) for r in ROUTES for m in r.methods if m != "HEAD"}
    want = {(p, m) for p, ms in EXPECTED.items() for m in ms}
    assert seen == want


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{sorted(r.methods)}{r.path}")
def test_route_requires_its_floor_requisitions_permission(route):
    got = _permission_of(route)
    assert got is not None, f"{route.path} has no require_permission dependency"
    for method in route.methods - {"HEAD"}:
        assert got == ("production", "floor_requisitions", EXPECTED[route.path][method])


def test_the_router_is_mounted_in_the_app():
    main = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text(encoding="utf-8")
    assert "from app.modules.floor_requisition.router import router as floor_requisition_router" in main
    assert "app.include_router(floor_requisition_router)" in main


def test_the_body_cannot_name_who_did_it():
    for model in (R.RaiseBody, R.IssueBody, R.CancelBody):
        assert not {"raised_by", "issued_by", "received_by", "cancelled_by"} & set(model.model_fields)


class _Ctx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def transaction(self):
        return _Ctx(self)


def _request():
    conn = _Conn()
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))


def test_a_refusal_becomes_error_message_details(monkeypatch):
    async def refuse(conn, user, **kw):
        raise svc.RequisitionError(409, "open_requisition_exists",
                                   "Requisition 87654321 for this article is still waiting for store.",
                                   requisition_id=87654321)

    monkeypatch.setattr(svc, "raise_requisition", refuse)
    body = R.RaiseBody(job_card_id=12345678, material_sku_name="Pista", requested_qty="5")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.raise_floor_requisition(_request(), body, user=object()))
    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "error": "open_requisition_exists",
        "message": "Requisition 87654321 for this article is still waiting for store.",
        "details": {"requisition_id": 87654321},
    }


def test_issue_passes_the_path_id_and_body_through(monkeypatch):
    seen = {}

    async def issue(conn, user, requisition_id, **kw):
        seen.update(requisition_id=requisition_id, user=user, **kw)
        return {"requisition_id": requisition_id, "status": "issued"}

    monkeypatch.setattr(svc, "issue_requisition", issue)
    user = object()
    out = asyncio.run(R.issue_floor_requisition(
        _request(), 87654321, R.IssueBody(issued_qty=500, issue_note="part"), user=user))
    assert out["status"] == "issued"
    assert seen == {"requisition_id": 87654321, "user": user, "issued_qty": 500.0, "issue_note": "part"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_floor_requisition_router.py`
Expected: FAIL — `ImportError: cannot import name 'router' from 'app.modules.floor_requisition'`.

- [ ] **Step 3: Implement (CRLF)**

`server_replica/app/modules/floor_requisition/router.py`:

```python
"""/api/v1/floor-requisitions/* — the floor asks store for material.

    GET  /api/v1/floor-requisitions                          list (view)
    POST /api/v1/floor-requisitions                          raise (create)
    POST /api/v1/floor-requisitions/{requisition_id}/issue   store issues (issue)
    POST /api/v1/floor-requisitions/{requisition_id}/receive floor confirms (receive)
    POST /api/v1/floor-requisitions/{requisition_id}/cancel  while raised (cancel)

Raised from the job card's "Material allocation and requisition" tab; issued from
Production → Floor Requisitions. Gated on production.floor_requisitions.*
(app/db/111_floor_requisition.sql): floor_manager raises / receives, store_head
issues, both may cancel. Every step is also limited to the caller's granted
warehouses and floors (stock_take.place_scope).

The actor of each step is the access token's user; no body field can name one.

A NEW module rather than more routes on production/router.py, which is past 7k
lines — the reasoning the stock_take and BOM modules record.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.modules.auth.middleware import AuthUser, require_permission
from app.modules.floor_requisition.services import requisition_service as svc

router = APIRouter(prefix="/api/v1/floor-requisitions", tags=["Floor Requisitions"])


def _perm(action: str):
    return require_permission("production", "floor_requisitions", action=action)


class RaiseBody(BaseModel):
    job_card_id: int
    material_sku_name: str = Field(..., min_length=1, max_length=500)
    # A number or its text; the unit rules are applied by the service.
    requested_qty: Union[float, str]
    note: Optional[str] = Field(None, max_length=500)


class IssueBody(BaseModel):
    issued_qty: Union[float, str]
    issue_note: Optional[str] = Field(None, max_length=500)


class CancelBody(BaseModel):
    reason: str = Field("", max_length=500)


def _http(exc: svc.RequisitionError) -> HTTPException:
    return HTTPException(exc.status, detail={
        "error": exc.error, "message": exc.message, "details": exc.details})


@router.get("")
async def list_floor_requisitions(
    request: Request,
    status: Optional[Literal["raised", "issued", "received", "cancelled"]] = Query(None),
    warehouse: Optional[str] = Query(None, description="W-202 and W202 both work"),
    floor_name: Optional[str] = Query(None, alias="floorName"),
    job_card_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=svc.MAX_PAGE_SIZE),
    user: AuthUser = Depends(_perm("view")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await svc.list_requisitions(
            conn, user, status=status, warehouse=warehouse, floor=floor_name,
            job_card_id=job_card_id, search=search, page=page, page_size=page_size)


@router.post("")
async def raise_floor_requisition(
    request: Request, body: RaiseBody, user: AuthUser = Depends(_perm("create")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await svc.raise_requisition(
                    conn, user, job_card_id=body.job_card_id,
                    material_sku_name=body.material_sku_name,
                    requested_qty=body.requested_qty, note=body.note)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.post("/{requisition_id}/issue")
async def issue_floor_requisition(
    request: Request, requisition_id: int, body: IssueBody,
    user: AuthUser = Depends(_perm("issue")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await svc.issue_requisition(
                    conn, user, requisition_id,
                    issued_qty=body.issued_qty, issue_note=body.issue_note)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.post("/{requisition_id}/receive")
async def receive_floor_requisition(
    request: Request, requisition_id: int, user: AuthUser = Depends(_perm("receive")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await svc.receive_requisition(conn, user, requisition_id)
    except svc.RequisitionError as exc:
        raise _http(exc) from None


@router.post("/{requisition_id}/cancel")
async def cancel_floor_requisition(
    request: Request, requisition_id: int, body: CancelBody,
    user: AuthUser = Depends(_perm("cancel")),
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await svc.cancel_requisition(conn, user, requisition_id, reason=body.reason)
    except svc.RequisitionError as exc:
        raise _http(exc) from None
```

In `app/main.py`, after `from app.modules.stock_take.router import router as stock_take_router` add:

```python
from app.modules.floor_requisition.router import router as floor_requisition_router
```

and after `app.include_router(stock_take_router)` add:

```python
app.include_router(floor_requisition_router)
```

Convert `router.py` to CRLF as in Task 1.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_floor_requisition_router.py tests/services/test_floor_requisition_service.py tests/services/test_floor_requisition_rules.py tests/services/test_floor_requisition_migration.py tests/services/test_place_scope.py tests/services/test_stock_take_floor_stock.py tests/services/test_stock_take_rbac.py`
Expected: all pass.

Then the full server suite: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q`
Expected: only the 2 pre-existing failures in `tests/services/test_sku_lookup_permission.py`.

- [ ] **Step 5: Checkpoint** — changed: `app/modules/floor_requisition/router.py` (new), `app/main.py`, `tests/services/test_floor_requisition_router.py` (new). No commit.

---

### Task 6: Web — pure form rules and the API client

**Files:**
- Create: `web_replica/src/lib/floor-requisition-form.ts`, `web_replica/src/lib/floor-requisition-form.test.ts`, `web_replica/src/lib/floor-requisitions.ts`

**Interfaces:**
- Consumes: Task 5 HTTP API.
- Produces:
  - `floor-requisition-form.ts`: `type RequisitionUnit = "kg" | "pcs"`; `type RequisitionStatus = "raised" | "issued" | "received" | "cancelled"`; `REQUISITION_STATUSES`; `STATUS_LABEL: Record<RequisitionStatus, string>`; `requisitionUnit(reqUnit, itemType): RequisitionUnit`; `defaultRequestQty(balance: number | null | undefined, unit): string`; `type QtyCheck`; `checkQty(text: string, unit): QtyCheck`; `formatQty(n: number, unit: string): string`; `formatWhen(iso: string | null | undefined): string`; `type RequisitionLike`; `type ArticleRequests<T> = { open: T | null; latest: T | null }`; `requestStateByArticle<T>(rows): Map<string, ArticleRequests<T>>` keyed by trimmed upper-cased article name.
  - `floor-requisitions.ts`: `interface FloorRequisition` (every column of the row); `interface FloorRequisitionPage { items; total; page; page_size }`; `interface RequisitionQuery { status?; warehouse?; floor?; jobCardId?; search?; page?; pageSize? }`; `class RequisitionConflictError extends Error { code: string }`; `listFloorRequisitions(q, signal?)`, `raiseFloorRequisition(body)`, `issueFloorRequisition(id, body)`, `receiveFloorRequisition(id)`, `cancelFloorRequisition(id, reason)`.

- [ ] **Step 1: Write the failing test**

`web_replica/src/lib/floor-requisition-form.test.ts`:

```ts
// Exercises lib/floor-requisition-form. No test runner is configured in this
// project, so this runs directly on Node's native TypeScript stripping:
//
//     node src/lib/floor-requisition-form.test.ts

import {
  checkQty, defaultRequestQty, formatQty, formatWhen, requestStateByArticle, requisitionUnit, STATUS_LABEL,
  type RequisitionStatus,
} from "./floor-requisition-form.ts";

let failures = 0;
function check(name: string, got: unknown, want: unknown) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { failures++; console.error(`FAIL ${name}\n  got  ${g}\n  want ${w}`); }
}

// ── unit ──
check("unit follows the requirement", requisitionUnit("pcs", "RM"), "pcs");
check("kg requirement stays kg", requisitionUnit("kg", "PM"), "kg");
check("PM without a requirement is pieces", requisitionUnit(undefined, " pm "), "pcs");
check("anything else without a requirement is kg", requisitionUnit(null, "SFG"), "kg");

// ── the quantity a dialog opens with ──
check("short kg opens with the shortage", defaultRequestQty(-88.2, "kg"), "88.2");
check("kg shortage rounds to 3 places", defaultRequestQty(-0.1 - 0.2, "kg"), "0.3");
check("short pieces open whole", defaultRequestQty(-1000, "pcs"), "1000");
check("a fractional piece shortage rounds up", defaultRequestQty(-24.2, "pcs"), "25");
check("covered opens empty", defaultRequestQty(12, "kg"), "");
check("exactly covered opens empty", defaultRequestQty(0, "kg"), "");
check("no requirement opens empty", defaultRequestQty(null, "pcs"), "");

// ── checkQty: the server's messages ──
check("kg 88.2", checkQty(" 88.2 ", "kg"), { ok: true, value: 88.2 });
check("kg 3 places", checkQty("0.001", "kg"), { ok: true, value: 0.001 });
check("trailing zeros are not decimals", checkQty("1.2000", "kg"), { ok: true, value: 1.2 });
check("kg 4 places", checkQty("1.2345", "kg"), { ok: false, message: "Kilograms go to 3 decimals at most." });
check("pcs whole", checkQty("1000", "pcs"), { ok: true, value: 1000 });
check("pcs 10.000", checkQty("10.000", "pcs"), { ok: true, value: 10 });
check("pcs fraction", checkQty("10.5", "pcs"), { ok: false, message: "Pieces are whole numbers." });
check("zero", checkQty("0", "kg"), { ok: false, message: "The quantity must be more than 0." });
check("negative", checkQty("-3", "pcs"), { ok: false, message: "The quantity must be more than 0." });
check("text", checkQty("abc", "kg"), { ok: false, message: "Enter a quantity as a number." });
check("empty", checkQty("", "kg"), { ok: false, message: "Enter a quantity as a number." });
check("no exponents", checkQty("1e3", "kg"), { ok: false, message: "Enter a quantity as a number." });
check("too large", checkQty("100000000000", "kg"), { ok: false, message: "That quantity is too large." });

// ── how things read ──
check("kg to 3 places", formatQty(88.2, "kg"), "88.200 kg");
check("pieces whole, grouped", formatQty(1000, "pcs"), "1,000 pcs");
check("India time", formatWhen("2026-09-15T08:35:00+00:00"), "15 Sep 2026, 14:05");
check("India time past midnight", formatWhen("2026-09-15T20:00:00+00:00"), "16 Sep 2026, 01:30");
check("no time", formatWhen(null), "—");
check("status words", STATUS_LABEL.issued, "Issued");

// ── which request belongs to which article ──
const req = (requisition_id: number, material_sku_name: string, status: RequisitionStatus, raised_at: string) =>
  ({ requisition_id, material_sku_name, status, raised_at });
const state = requestStateByArticle([
  req(1, "Pista", "received", "2026-09-10T08:00:00+00:00"),
  req(2, " PISTA ", "raised", "2026-09-15T08:00:00+00:00"),
  req(3, "Pouch", "cancelled", "2026-09-15T09:00:00+00:00"),
  req(4, "Pouch", "issued", "2026-09-14T09:00:00+00:00"),
  req(5, "Carton", "cancelled", "2026-09-15T09:00:00+00:00"),
]);
check("open request, matched ignoring case and padding", state.get("PISTA")?.open?.requisition_id, 2);
check("latest is the newest not cancelled", state.get("PISTA")?.latest?.requisition_id, 2);
check("no open request", state.get("POUCH")?.open ?? null, null);
check("a newer cancelled request does not hide the issued one", state.get("POUCH")?.latest?.requisition_id, 4);
check("only cancelled requests show nothing",
  [state.get("CARTON")?.open ?? null, state.get("CARTON")?.latest ?? null], [null, null]);

if (failures) {
  console.error(`${failures} failure(s)`);
  process.exit(1);
}
console.log("floor-requisition-form: all checks passed");
```

- [ ] **Step 2: Run it to verify it fails**

Run (from `web_replica`): `node src/lib/floor-requisition-form.test.ts`
Expected: FAIL — `ERR_MODULE_NOT_FOUND` for `floor-requisition-form.ts`.

- [ ] **Step 3: Implement**

`web_replica/src/lib/floor-requisition-form.ts`:

```ts
// Floor requisitions — the pure half shared by the job card tab and the store
// screen: the unit a request carries, the quantity a dialog opens with and
// accepts, how quantities, times and statuses read, and which request belongs to
// which BOM article.
//
// The quantity rules and messages match the server's
// (server_replica app/modules/floor_requisition/rules.py), so a person sees before
// sending the same refusal the server would give.
//
// No React / Next imports, so this runs under plain Node for its test:
//     node src/lib/floor-requisition-form.test.ts

export type RequisitionUnit = "kg" | "pcs";
export type RequisitionStatus = "raised" | "issued" | "received" | "cancelled";

export const REQUISITION_STATUSES: readonly RequisitionStatus[] = ["raised", "issued", "received", "cancelled"];

export const STATUS_LABEL: Record<RequisitionStatus, string> = {
  raised: "Raised",
  issued: "Issued",
  received: "Received",
  cancelled: "Cancelled",
};

/** The unit a request for this article carries — the server's unit_for: the
 *  requirement's unit, else pieces for PM, else kg. */
export function requisitionUnit(
  reqUnit: string | null | undefined,
  itemType: string | null | undefined,
): RequisitionUnit {
  if (reqUnit === "kg" || reqUnit === "pcs") return reqUnit;
  return (itemType ?? "").trim().toUpperCase() === "PM" ? "pcs" : "kg";
}

/** What the Quantity field opens with: the shortage when short, otherwise empty.
 *  `balance` is Coverage.balance from lib/floorStock (negative = short by that). */
export function defaultRequestQty(balance: number | null | undefined, unit: RequisitionUnit): string {
  if (balance == null || !(balance < 0)) return "";
  const short = -balance;
  return unit === "pcs" ? String(Math.ceil(short)) : String(Math.round(short * 1000) / 1000);
}

export type QtyCheck = { ok: true; value: number } | { ok: false; message: string };

// Plain decimal text only — no exponents, no hex, no grouping commas.
const NUMBER_TEXT = /^[+-]?(\d+\.?\d*|\.\d+)$/;

/** A typed quantity, checked in the same order and words as the server. */
export function checkQty(text: string, unit: RequisitionUnit): QtyCheck {
  const s = text.trim();
  if (!NUMBER_TEXT.test(s)) return { ok: false, message: "Enter a quantity as a number." };
  const value = Number(s);
  if (!(value > 0)) return { ok: false, message: "The quantity must be more than 0." };
  if (value >= 1e11) return { ok: false, message: "That quantity is too large." };
  const decimals = (s.split(".")[1] ?? "").replace(/0+$/, "").length;
  if (unit === "pcs" && decimals > 0) return { ok: false, message: "Pieces are whole numbers." };
  if (decimals > 3) return { ok: false, message: "Kilograms go to 3 decimals at most." };
  return { ok: true, value };
}

/** "88.200 kg", "1,000 pcs": kg to 3 decimals, pieces whole, Indian grouping. */
export function formatQty(n: number, unit: string): string {
  const dp = unit === "pcs" ? 0 : 3;
  return `${n.toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp })} ${unit}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const IST_OFFSET_MS = 330 * 60 * 1000;

/** An ISO timestamp as India time, "15 Sep 2026, 14:05". Built from parts, so it
 *  reads the same in every browser and locale. */
export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  const d = new Date(ms + IST_OFFSET_MS);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}, ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

export type RequisitionLike = {
  requisition_id: number;
  material_sku_name: string;
  status: RequisitionStatus;
  raised_at: string;
};

export type ArticleRequests<T> = { open: T | null; latest: T | null };

// The tab's articleKey (lib/floorStock), repeated here so this module stays
// importable under plain Node without a path alias.
const keyOf = (name: string | null | undefined) => (name ?? "").trim().toUpperCase();

/** Per article (trimmed, upper-cased name): its open — raised — request, and its
 *  newest request that was not cancelled. Requisition numbers are not in date
 *  order, so "newest" is by raised_at. */
export function requestStateByArticle<T extends RequisitionLike>(
  rows: readonly T[],
): Map<string, ArticleRequests<T>> {
  const newestFirst = [...rows].sort((a, b) =>
    Date.parse(b.raised_at) - Date.parse(a.raised_at) || b.requisition_id - a.requisition_id);
  const out = new Map<string, ArticleRequests<T>>();
  for (const r of newestFirst) {
    const k = keyOf(r.material_sku_name);
    const cur = out.get(k) ?? { open: null, latest: null };
    if (r.status === "raised" && !cur.open) cur.open = r;
    if (r.status !== "cancelled" && !cur.latest) cur.latest = r;
    out.set(k, cur);
  }
  return out;
}
```

`web_replica/src/lib/floor-requisitions.ts`:

```ts
// Floor requisitions API client — /api/v1/floor-requisitions
// (server_replica app/modules/floor_requisition/router.py).
//
// A 409 from a write is not a red error: either someone already moved the request
// on (status_changed) or an open request for the article exists
// (open_requisition_exists). It surfaces as RequisitionConflictError, so a screen
// can reload or show the server's message inline.

import { apiFetch, readApiErrorMessage } from "./auth";
import type { RequisitionStatus, RequisitionUnit } from "./floor-requisition-form";

const BASE = "/api/v1/floor-requisitions";

export interface FloorRequisition {
  /** The requisition number: an 8-digit time-based id. Not in date order. */
  requisition_id: number;
  job_card_id: number;
  warehouse: string;
  floor: string;
  material_sku_name: string;
  item_type: string | null;
  requested_qty: number;
  requested_unit: RequisitionUnit;
  /** Snapshot when raised. null = the job card had no indent line for it. */
  required_qty: number | null;
  required_unit: RequisitionUnit | null;
  available_qty: number;
  available_unit: RequisitionUnit;
  shortage_qty: number | null;
  shortage_unit: RequisitionUnit | null;
  issued_qty: number | null;
  issued_unit: RequisitionUnit | null;
  status: RequisitionStatus;
  note: string | null;
  issue_note: string | null;
  cancel_reason: string | null;
  raised_by: string;
  raised_at: string;
  issued_by: string | null;
  issued_at: string | null;
  received_by: string | null;
  received_at: string | null;
  cancelled_by: string | null;
  cancelled_at: string | null;
}

export interface FloorRequisitionPage {
  items: FloorRequisition[];
  total: number;
  page: number;
  page_size: number;
}

export interface RequisitionQuery {
  status?: RequisitionStatus | "";
  warehouse?: string;
  floor?: string;
  jobCardId?: number;
  search?: string;
  page?: number;
  pageSize?: number;
}

export class RequisitionConflictError extends Error {
  readonly code: string;
  constructor(message: string, code: string) {
    super(message);
    this.name = "RequisitionConflictError";
    this.code = code;
  }
}

async function readOrThrow<T>(res: Response, fallback: string): Promise<T> {
  if (res.ok) return (await res.json()) as T;
  if (res.status === 409) {
    const body = (await res.clone().json().catch(() => null)) as { detail?: { error?: string } } | null;
    throw new RequisitionConflictError(await readApiErrorMessage(res, fallback), body?.detail?.error ?? "conflict");
  }
  throw new Error(await readApiErrorMessage(res, fallback));
}

function post(path: string, body?: unknown): Promise<Response> {
  return apiFetch(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
}

export async function listFloorRequisitions(
  q: RequisitionQuery,
  signal?: AbortSignal,
): Promise<FloorRequisitionPage> {
  const p = new URLSearchParams();
  if (q.status) p.set("status", q.status);
  if (q.warehouse) p.set("warehouse", q.warehouse);
  if (q.floor) p.set("floorName", q.floor);
  if (q.jobCardId != null) p.set("job_card_id", String(q.jobCardId));
  if (q.search?.trim()) p.set("search", q.search.trim());
  p.set("page", String(q.page ?? 1));
  p.set("page_size", String(q.pageSize ?? 100));
  return readOrThrow(await apiFetch(`${BASE}?${p}`, { signal }), "Failed to load floor requisitions");
}

export async function raiseFloorRequisition(body: {
  job_card_id: number;
  material_sku_name: string;
  requested_qty: number;
  note?: string;
}): Promise<FloorRequisition> {
  return readOrThrow(await post(BASE, body), "Failed to raise the request");
}

export async function issueFloorRequisition(
  id: number,
  body: { issued_qty: number; issue_note?: string },
): Promise<FloorRequisition> {
  return readOrThrow(await post(`${BASE}/${id}/issue`, body), "Failed to issue");
}

export async function receiveFloorRequisition(id: number): Promise<FloorRequisition> {
  return readOrThrow(await post(`${BASE}/${id}/receive`), "Failed to mark received");
}

export async function cancelFloorRequisition(id: number, reason: string): Promise<FloorRequisition> {
  return readOrThrow(await post(`${BASE}/${id}/cancel`, { reason }), "Failed to cancel");
}
```

- [ ] **Step 4: Run the checks to verify they pass**

Run (from `web_replica`):
- `node src/lib/floor-requisition-form.test.ts` → prints `floor-requisition-form: all checks passed`, exit 0
- `npx tsc --noEmit -p . 2>&1 | grep -v '^\.next/'` → no output
- `npx eslint src/lib/floor-requisition-form.ts src/lib/floor-requisition-form.test.ts src/lib/floor-requisitions.ts` → 0 problems

- [ ] **Step 5: Checkpoint** — changed: the three new `src/lib` files. No commit.

---

### Task 7: Web — shared requisition UI, Request dialog, Request column

**Files:**
- Create: `web_replica/src/components/floor-requisitions/RequisitionUi.tsx`, `web_replica/src/app/modules/job-card/[id]/_RequestDialog.tsx`
- Modify: `web_replica/src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx` (LF), `web_replica/src/app/modules/job-card/[id]/page.tsx` (CRLF — the `case "allocation"` block, line ~2046)

**Interfaces:**
- Consumes: Task 6 (`floor-requisition-form.ts`, `floor-requisitions.ts`); `Coverage` from `@/lib/floorStock`; `friendlyApiError` from `@/lib/apiErrors`; `useHasPermission(module, sub_module, sub_sub_module, action)` from `@/lib/user`.
- Produces:
  - `RequisitionUi.tsx`: class strings `FIELD`, `TEXTAREA`, `BTN`, `BTN_PRIMARY`, `BTN_LINK`; `RequisitionModal({ title, onClose, initialFocus: RefObject<HTMLElement | null>, children })`; `StatusTag({ status })`; `RequisitionFacts({ r: FloorRequisition })`; `CancelRequisitionDialog({ requisition, onClose, onDone: () => void })`.
  - `_RequestDialog.tsx`: `RequestDialog({ jobCardId, place, article, itemType, unit, cover, onClose, onRaised })`.
  - `MaterialAllocationTab` gains required prop `jobCardId: number` and internal state `reqs: FloorRequisition[] | null`, `reqsErr`, `reloadReqs()` — Task 8 renders its section from these.

There is no component test runner in `web_replica`; this task's automated gate is `tsc` + ESLint + the Task 6 node test, and the screens are checked by hand in Task 10.

- [ ] **Step 1: Create the shared UI**

`web_replica/src/components/floor-requisitions/RequisitionUi.tsx`:

```tsx
"use client";

// Shared pieces of the floor-requisition UI. The job card's Material allocation
// tab and Production → Floor Requisitions both use them: the modal shell, the
// status tag, the facts of one request, the cancel dialog, and the field /
// button classes.
//
// The modal follows the job card Amendments dialog: full-screen below md, a
// centred box from md, Esc or a backdrop click closes it, Tab stays inside, and
// focus returns to whatever opened it.

import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode, type RefObject } from "react";
import { friendlyApiError } from "@/lib/apiErrors";
import { formatQty, formatWhen, STATUS_LABEL, type RequisitionStatus } from "@/lib/floor-requisition-form";
import { cancelFloorRequisition, RequisitionConflictError, type FloorRequisition } from "@/lib/floor-requisitions";

export const FIELD =
  "h-8 px-2 text-[13px] rounded-[2px] bg-white border border-[var(--aws-border-strong)] outline-none " +
  "focus:border-[var(--aws-navy)] text-[var(--text-primary)]";
export const TEXTAREA =
  "px-2 py-1.5 text-[13px] rounded-[2px] bg-white border border-[var(--aws-border-strong)] outline-none " +
  "focus:border-[var(--aws-navy)] text-[var(--text-primary)] resize-y";
export const BTN =
  "h-8 px-3 rounded-[2px] border border-[var(--aws-border-strong)] bg-white text-[13px] text-[var(--text-primary)] " +
  "hover:border-[var(--aws-navy)] disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap";
export const BTN_PRIMARY =
  "h-8 px-3 rounded-[2px] border border-[var(--aws-navy)] bg-[var(--aws-navy)] text-white text-[13px] font-semibold " +
  "disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap";
export const BTN_LINK =
  "text-[12px] text-[var(--aws-link)] underline disabled:opacity-50 disabled:cursor-not-allowed";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function RequisitionModal({
  title, onClose, initialFocus, children,
}: {
  title: string;
  onClose: () => void;
  /** Focused when the dialog opens. */
  initialFocus: RefObject<HTMLElement | null>;
  children: ReactNode;
}) {
  const titleId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  // The latest onClose, without re-running the open effect — re-running it would
  // pull focus back to the first field on every parent render.
  const closeRef = useRef(onClose);
  useEffect(() => { closeRef.current = onClose; }, [onClose]);

  useEffect(() => {
    const trigger = document.activeElement;
    queueMicrotask(() => { initialFocus.current?.focus(); });
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        closeRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const root = rootRef.current;
      if (!root) return;
      const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !root.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (trigger instanceof HTMLElement) trigger.focus();
    };
  }, [initialFocus]);

  return (
    <div
      ref={rootRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 bg-black/40 flex items-stretch md:items-center justify-center md:p-4"
      onClick={(e) => { if (e.target === e.currentTarget) closeRef.current(); }}
    >
      <div className="bg-white w-full md:max-w-[520px] md:rounded-md md:shadow-xl flex flex-col max-h-screen md:max-h-[90vh] overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--aws-border)]">
          <h3 id={titleId} className="text-[14px] font-semibold text-[var(--text-primary)]">{title}</h3>
          <button
            type="button"
            onClick={() => closeRef.current()}
            aria-label="Close"
            className="px-1 text-[18px] leading-none text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
          >
            ×
          </button>
        </div>
        <div className="overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}

const TAG: Record<RequisitionStatus, string> = {
  raised: "bg-[#fff4e0] text-[#8a5300]",
  issued: "bg-[#e8f1fb] text-[#0b5cad]",
  received: "bg-[#eaf6ed] text-[var(--text-success)]",
  cancelled: "bg-[#f2f3f3] text-[var(--text-secondary)]",
};

export function StatusTag({ status }: { status: RequisitionStatus }) {
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold whitespace-nowrap ${TAG[status]}`}>
      {STATUS_LABEL[status]}
    </span>
  );
}

const DT = "text-[var(--text-muted)]";

/** One request's facts, for the top of the Issue and Cancel dialogs. */
export function RequisitionFacts({ r }: { r: FloorRequisition }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px] text-[var(--text-primary)]">
      <dt className={DT}>Number</dt>
      <dd className="font-mono">#{r.requisition_id}</dd>
      <dt className={DT}>Article</dt>
      <dd className="break-words">{r.material_sku_name}{r.item_type ? ` · ${r.item_type}` : ""}</dd>
      <dt className={DT}>Place</dt>
      <dd className="break-words">{r.warehouse} · {r.floor} · Job card {r.job_card_id}</dd>
      <dt className={DT}>Requested</dt>
      <dd className="font-mono tabular-nums">{formatQty(r.requested_qty, r.requested_unit)}</dd>
      {r.shortage_qty != null ? (
        <>
          <dt className={DT}>Short when raised</dt>
          <dd className="font-mono tabular-nums">{formatQty(r.shortage_qty, r.requested_unit)}</dd>
        </>
      ) : null}
      <dt className={DT}>Raised</dt>
      <dd className="break-words">{r.raised_by} · {formatWhen(r.raised_at)}</dd>
      {r.note ? (
        <>
          <dt className={DT}>Note</dt>
          <dd className="break-words">{r.note}</dd>
        </>
      ) : null}
    </dl>
  );
}

/** Cancel a raised request, with a reason. `onDone` also fires when someone had
 *  already moved the request on (409): the caller reloads, which shows why. */
export function CancelRequisitionDialog({
  requisition, onClose, onDone,
}: {
  requisition: FloorRequisition;
  onClose: () => void;
  onDone: () => void;
}) {
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!reason.trim()) {
      setError("Give a reason for cancelling.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await cancelFloorRequisition(requisition.requisition_id, reason.trim());
      onDone();
    } catch (err) {
      if (err instanceof RequisitionConflictError) onDone();
      else setError(friendlyApiError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <RequisitionModal title="Cancel request" onClose={onClose} initialFocus={reasonRef}>
      <form onSubmit={submit} className="flex flex-col gap-3 p-4">
        <RequisitionFacts r={requisition} />
        <label className="flex flex-col gap-1 text-[12px] text-[var(--text-secondary)]">
          Reason
          <textarea
            ref={reasonRef}
            rows={3}
            maxLength={500}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className={TEXTAREA}
          />
        </label>
        {error ? <p role="alert" className="text-[12px] text-[var(--aws-error)]">{error}</p> : null}
        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" className={BTN} onClick={onClose}>Keep request</button>
          <button type="submit" className={BTN_PRIMARY} disabled={saving}>
            {saving ? "Cancelling…" : "Cancel request"}
          </button>
        </div>
      </form>
    </RequisitionModal>
  );
}
```

- [ ] **Step 2: Create the Request dialog**

`web_replica/src/app/modules/job-card/[id]/_RequestDialog.tsx`:

```tsx
"use client";

// Request dialog — raise a floor requisition for one BOM article, from the
// Request column of the Material allocation tab.
//
// Quantity opens with the shortage (empty when the article is not short), in the
// unit the request will carry. It is checked here with the server's own rules and
// words (lib/floor-requisition-form) before sending; the server still decides.
// A 409 — an open request for this article already exists — shows its message
// inline instead of closing, so the operator sees which request is waiting.

import { useRef, useState, type FormEvent } from "react";
import { BTN, BTN_PRIMARY, FIELD, RequisitionModal, TEXTAREA } from "@/components/floor-requisitions/RequisitionUi";
import { friendlyApiError } from "@/lib/apiErrors";
import { checkQty, defaultRequestQty, formatQty, type RequisitionUnit } from "@/lib/floor-requisition-form";
import { raiseFloorRequisition, RequisitionConflictError, type FloorRequisition } from "@/lib/floor-requisitions";
import type { Coverage } from "@/lib/floorStock";

const DT = "text-[var(--text-muted)]";

export function RequestDialog({
  jobCardId, place, article, itemType, unit, cover, onClose, onRaised,
}: {
  jobCardId: number;
  /** "W202 · First Floor" */
  place: string;
  article: string;
  itemType: string;
  unit: RequisitionUnit;
  /** Fresh stock against the requirement; null when the job card has no requirement for it. */
  cover: Coverage | null;
  onClose: () => void;
  onRaised: (r: FloorRequisition) => void;
}) {
  const qtyRef = useRef<HTMLInputElement>(null);
  const [qty, setQty] = useState(() => defaultRequestQty(cover?.balance, unit));
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const short = cover != null && cover.balance < 0;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const checked = checkQty(qty, unit);
    if (!checked.ok) {
      setError(checked.message);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const r = await raiseFloorRequisition({
        job_card_id: jobCardId,
        material_sku_name: article,
        requested_qty: checked.value,
        note: note.trim() || undefined,
      });
      onRaised(r);
    } catch (err) {
      setError(err instanceof RequisitionConflictError ? err.message : friendlyApiError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <RequisitionModal title="Request material" onClose={onClose} initialFocus={qtyRef}>
      <form onSubmit={submit} className="flex flex-col gap-3 p-4">
        <div>
          <p className="text-[13px] font-semibold text-[var(--text-primary)] break-words">{article}</p>
          <p className="text-[12px] text-[var(--text-secondary)]">{itemType} · {place}</p>
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px] text-[var(--text-primary)]">
          <dt className={DT}>Required</dt>
          <dd className="font-mono tabular-nums">{cover ? formatQty(cover.required, cover.unit) : "— (no indent line)"}</dd>
          <dt className={DT}>Fresh stock here</dt>
          <dd className="font-mono tabular-nums">{cover ? formatQty(cover.available, cover.unit) : "—"}</dd>
          <dt className={DT}>Shortage</dt>
          <dd className={`font-mono tabular-nums ${short ? "text-[var(--text-danger)] font-semibold" : ""}`}>
            {cover ? (short ? formatQty(-cover.balance, cover.unit) : "None") : "—"}
          </dd>
        </dl>
        <label className="flex flex-col gap-1 text-[12px] text-[var(--text-secondary)]">
          Quantity
          <span className="flex items-center gap-2">
            <input
              ref={qtyRef}
              inputMode="decimal"
              autoComplete="off"
              value={qty}
              onChange={(e) => setQty(e.target.value)}
              className={`${FIELD} min-w-0 flex-1 font-mono`}
            />
            <span className="text-[13px] text-[var(--text-primary)]">{unit}</span>
          </span>
        </label>
        <label className="flex flex-col gap-1 text-[12px] text-[var(--text-secondary)]">
          Note (optional)
          <textarea
            rows={2}
            maxLength={500}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            className={TEXTAREA}
          />
        </label>
        {error ? <p role="alert" className="text-[12px] text-[var(--aws-error)]">{error}</p> : null}
        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" className={BTN} onClick={onClose}>Cancel</button>
          <button type="submit" className={BTN_PRIMARY} disabled={saving}>
            {saving ? "Raising…" : "Raise request"}
          </button>
        </div>
      </form>
    </RequisitionModal>
  );
}
```

- [ ] **Step 3: Wire the Request column into the tab**

Make these edits to `web_replica/src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx`:

(a) Header comment — replace the line
`// Read-only: nothing here allocates or requests material yet.`
with
```tsx
// The Request column raises a floor requisition for an article
// (lib/floor-requisitions): store issues it from Production → Floor Requisitions
// and the floor marks it received. Nothing here moves stock.
```

(b) Imports — replace `import { useCallback, useEffect, useId, useMemo, useState } from "react";` with
```tsx
import { useCallback, useEffect, useId, useMemo, useState, type ReactNode } from "react";
```
and after `import { normaliseWarehouseCode } from "@/lib/warehouseScope";` add
```tsx
import { BTN, StatusTag } from "@/components/floor-requisitions/RequisitionUi";
import {
  formatQty, requestStateByArticle, requisitionUnit, type ArticleRequests,
} from "@/lib/floor-requisition-form";
import { listFloorRequisitions, type FloorRequisition } from "@/lib/floor-requisitions";
import { RequestDialog } from "./_RequestDialog";
```

(c) `ArticleCard` — add an `action` prop. Replace
```tsx
  title, itemType, stock, pieces, requirement,
}: {
```
with
```tsx
  title, itemType, stock, pieces, requirement, action,
}: {
```
replace
```tsx
  requirement?: { req: Requirement | undefined; cover: Coverage | null };
}) {
```
with
```tsx
  requirement?: { req: Requirement | undefined; cover: Coverage | null };
  /** The Request control, for BOM articles when the viewer may raise requests. */
  action?: ReactNode;
}) {
```
and replace the card's closing
```tsx
      )}
    </li>
```
with
```tsx
      )}
      {action ? <div className="mt-2 border-t border-[var(--aws-border)] pt-2">{action}</div> : null}
    </li>
```

(d) Add `RequestCell` directly above `const isPm = ...`:
```tsx
/** The Request column: the article's open request, or a Request button with the
 *  newest earlier request's status above it. */
function RequestCell({
  state, disabled, onRequest,
}: {
  state: ArticleRequests<FloorRequisition> | undefined;
  disabled: boolean;
  onRequest: () => void;
}) {
  const open = state?.open ?? null;
  if (open) {
    return (
      <span className="inline-flex flex-col gap-1">
        <span className="font-mono text-[12px] text-[var(--text-primary)]">#{open.requisition_id}</span>
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <StatusTag status="raised" />
          <span className="font-mono tabular-nums whitespace-nowrap">{formatQty(open.requested_qty, open.requested_unit)}</span>
        </span>
      </span>
    );
  }
  const latest = state?.latest ?? null;
  return (
    <span className="inline-flex flex-col items-start gap-1">
      {latest ? (
        <span className="inline-flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--text-secondary)]">
          <span className="font-mono">#{latest.requisition_id}</span>
          <StatusTag status={latest.status} />
          <span className="font-mono tabular-nums whitespace-nowrap">
            {formatQty(latest.issued_qty ?? latest.requested_qty, latest.requested_unit)}
          </span>
        </span>
      ) : null}
      <button type="button" className={BTN} disabled={disabled} onClick={onRequest}>Request</button>
    </span>
  );
}

```

(e) Props — replace
```tsx
export function MaterialAllocationTab({
  warehouse,
```
with
```tsx
export function MaterialAllocationTab({
  jobCardId,
  warehouse,
```
and replace
```tsx
  /** The job card's plant as the job card spells it ("W-202"). */
```
with
```tsx
  /** The job card's 8-digit id — what its floor requisitions are raised against. */
  jobCardId: number;
  /** The job card's plant as the job card spells it ("W-202"). */
```

(f) State — after `  const [otherPage, setOtherPage] = useState(1);` add
```tsx
  // Floor requisitions this job card has raised. The Request column shows each
  // article's open one; the requisitions section lists them all.
  const canViewReqs = useHasPermission("production", "floor_requisitions", null, "view");
  const canRaise = useHasPermission("production", "floor_requisitions", null, "create");
  const [reqs, setReqs] = useState<FloorRequisition[] | null>(null);
  const [reqsErr, setReqsErr] = useState<string | null>(null);
  const [reqsAttempt, setReqsAttempt] = useState(0); // bumped after every change
  const [requestingKey, setRequestingKey] = useState<string | null>(null);
```

(g) Loading — directly after the floor-stock effect's closing line `  }, [canView, wh, fl, load, attempt]);` add
```tsx

  useEffect(() => {
    if (!canViewReqs) return;
    const ctrl = new AbortController();
    queueMicrotask(() => {
      listFloorRequisitions({ jobCardId, pageSize: 500 }, ctrl.signal)
        .then((p) => { if (!ctrl.signal.aborted) { setReqs(p.items); setReqsErr(null); } })
        .catch((e: unknown) => { if (!ctrl.signal.aborted) setReqsErr(friendlyApiError(e)); });
    });
    return () => ctrl.abort();
  }, [canViewReqs, jobCardId, reqsAttempt]);
  const reloadReqs = useCallback(() => setReqsAttempt((n) => n + 1), []);
  const closeRequest = useCallback(() => setRequestingKey(null), []);
  const reqState = useMemo(() => requestStateByArticle(reqs ?? []), [reqs]);
```

(h) Derived values — directly after `  const shortCount = bomRows.filter((r) => r.cover && r.cover.balance < 0).length;` add
```tsx
  // Wait for this job card's requisitions before offering Request, so an open one
  // shows instead of a second button. If that load failed, Request stays usable:
  // the server's one-open-request rule still stops a duplicate.
  const requestDisabled = canViewReqs && reqs === null && reqsErr === null;
  const requestingRow = requestingKey ? bomRows.find((r) => r.key === requestingKey) ?? null : null;
  const requestCell = (key: string) => (
    <RequestCell state={reqState.get(key)} disabled={requestDisabled} onRequest={() => setRequestingKey(key)} />
  );
```

(i) Table header — replace
```tsx
                        <th className={TH}>Against requirement</th>
```
with
```tsx
                        <th className={TH}>Against requirement</th>
                        {canRaise ? <th className={TH}>Request</th> : null}
```

(j) Table rows — replace
```tsx
                        const verdict = <td rowSpan={span} className={TD}><CoverageNote c={r.cover} /></td>;
```
with
```tsx
                        const verdict = <td rowSpan={span} className={TD}><CoverageNote c={r.cover} /></td>;
                        const request = canRaise ? <td rowSpan={span} className={TD}>{requestCell(r.key)}</td> : null;
```
replace
```tsx
                              {verdict}
                            </tr>
```
with
```tsx
                              {verdict}
                              {request}
                            </tr>
```
and replace
```tsx
                            {i === 0 ? verdict : null}
```
with
```tsx
                            {i === 0 ? verdict : null}
                            {i === 0 ? request : null}
```

(k) Phone cards — replace
```tsx
                      requirement={{ req: r.req, cover: r.cover }}
                    />
```
with
```tsx
                      requirement={{ req: r.req, cover: r.cover }}
                      action={canRaise ? requestCell(r.key) : undefined}
                    />
```

(l) The dialog — replace the component's last lines
```tsx
      ) : null}
    </div>
  );
}
```
(the end of the file) with
```tsx
      ) : null}

      {requestingRow ? (
        <RequestDialog
          jobCardId={jobCardId}
          place={`${wh} · ${fl}`}
          article={requestingRow.article}
          itemType={requestingRow.itemType}
          unit={requisitionUnit(requestingRow.req?.unit, requestingRow.itemType)}
          cover={requestingRow.cover}
          onClose={closeRequest}
          onRaised={() => { setRequestingKey(null); reloadReqs(); }}
        />
      ) : null}
    </div>
  );
}
```

- [ ] **Step 4: Pass the job card id from the page**

In `web_replica/src/app/modules/job-card/[id]/page.tsx` (CRLF — edit preserving `\r\n`), replace
```tsx
      <MaterialAllocationTab
        warehouse={detail.factory}
```
with
```tsx
      <MaterialAllocationTab
        jobCardId={detail.job_card_id}
        warehouse={detail.factory}
```

- [ ] **Step 5: Run the checks**

From `web_replica`:
- `npx tsc --noEmit -p . 2>&1 | grep -v '^\.next/'` → no output
- `npx eslint src/components/floor-requisitions/RequisitionUi.tsx "src/app/modules/job-card/[id]/_RequestDialog.tsx" "src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx"` → 0 problems
- `npx eslint "src/app/modules/job-card/[id]/page.tsx"` → still 11 problems (baseline)
- `node src/lib/floor-requisition-form.test.ts` → all checks passed
- `git diff --stat` is not available for `web_replica` (not a git repo); confirm `page.tsx` still has CRLF: `.venv`-free check `node -e "const b=require('fs').readFileSync('src/app/modules/job-card/[id]/page.tsx');console.log(b.includes('\r\n'), /[^\r]\n/.test(b.toString()))"` → `true false`

- [ ] **Step 6: Checkpoint** — changed: `RequisitionUi.tsx`, `_RequestDialog.tsx` (new), `_MaterialAllocationTab.tsx`, `page.tsx`. No commit.

---

### Task 8: Web — "Requisitions for this job card" section

**Files:**
- Create: `web_replica/src/app/modules/job-card/[id]/_JobCardRequisitions.tsx`
- Modify: `web_replica/src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx`

**Interfaces:**
- Consumes: Task 6 (`formatQty`, `formatWhen`, `receiveFloorRequisition`, `RequisitionConflictError`, `FloorRequisition`); Task 7 (`BTN`, `BTN_LINK`, `StatusTag`, `CancelRequisitionDialog`; the tab's `canViewReqs`, `reqs`, `reqsErr`, `reloadReqs`).
- Produces: `JobCardRequisitions({ rows: FloorRequisition[] | null, error: string | null, onRetry: () => void, onChanged: () => void })`.

- [ ] **Step 1: Create the section**

`web_replica/src/app/modules/job-card/[id]/_JobCardRequisitions.tsx`:

```tsx
"use client";

// "Requisitions for this job card" — on the Material allocation tab, below the
// BOM articles. Every floor requisition this job card has raised, newest first:
// Mark received on issued ones, Cancel on raised ones, each only with its
// permission. Bordered table from md up; stacked cards below it.
//
// The tab owns the list (it also drives the Request column), so this renders
// what it is given and asks the tab to reload after any change. A 409 — someone
// already moved the request on — reloads too, which shows what happened.

import { useState } from "react";
import { BTN, BTN_LINK, CancelRequisitionDialog, StatusTag } from "@/components/floor-requisitions/RequisitionUi";
import { friendlyApiError } from "@/lib/apiErrors";
import { formatQty, formatWhen } from "@/lib/floor-requisition-form";
import { receiveFloorRequisition, RequisitionConflictError, type FloorRequisition } from "@/lib/floor-requisitions";
import { useHasPermission } from "@/lib/user";

// The tab's own card and cell classes, so the section reads as part of it.
const CARD =
  "bg-white border border-[var(--aws-border)] rounded-md shadow-[0_1px_1px_rgba(0,28,36,0.18)] p-3 sm:p-4 mb-4";
const HEADING = "text-[12px] uppercase tracking-wide font-semibold text-[var(--text-secondary)]";
const TABLE = "w-full text-[12px] border-collapse";
const TH =
  "border border-[var(--aws-border)] bg-[#fafafa] px-2.5 py-2 text-left text-[10px] font-bold uppercase " +
  "tracking-wide text-[var(--text-secondary)] whitespace-nowrap";
const TD = "border border-[var(--aws-border)] px-2.5 py-2 align-top";
const HINT = "text-[12px] text-[var(--text-muted)] italic";
const SUB = "text-[11px] text-[var(--text-muted)] break-words";

function Issued({ r }: { r: FloorRequisition }) {
  if (r.issued_qty == null) return <span className="text-[var(--text-muted)]">—</span>;
  return (
    <span className="inline-flex flex-col gap-0.5">
      <span className="font-mono tabular-nums whitespace-nowrap">{formatQty(r.issued_qty, r.requested_unit)}</span>
      <span className={SUB}>{r.issued_by} · {formatWhen(r.issued_at)}</span>
      {r.issue_note ? <span className={SUB}>{r.issue_note}</span> : null}
    </span>
  );
}

function Status({ r }: { r: FloorRequisition }) {
  const detail =
    r.status === "received" ? `${r.received_by ?? ""} · ${formatWhen(r.received_at)}`
    : r.status === "cancelled" ? `${r.cancel_reason ?? ""} — ${r.cancelled_by ?? ""}`
    : null;
  return (
    <span className="inline-flex flex-col items-start gap-0.5">
      <StatusTag status={r.status} />
      {detail ? <span className={SUB}>{detail}</span> : null}
    </span>
  );
}

function Raised({ r }: { r: FloorRequisition }) {
  return (
    <span className="inline-flex flex-col gap-0.5">
      <span className="break-words">{r.raised_by}</span>
      <span className={SUB}>{formatWhen(r.raised_at)}</span>
      {r.note ? <span className={SUB}>{r.note}</span> : null}
    </span>
  );
}

export function JobCardRequisitions({
  rows, error, onRetry, onChanged,
}: {
  /** null while loading. */
  rows: FloorRequisition[] | null;
  error: string | null;
  onRetry: () => void;
  onChanged: () => void;
}) {
  const canReceive = useHasPermission("production", "floor_requisitions", null, "receive");
  const canCancel = useHasPermission("production", "floor_requisitions", null, "cancel");
  const [busyId, setBusyId] = useState<number | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState<FloorRequisition | null>(null);
  const hasActions = canReceive || canCancel;

  async function receive(r: FloorRequisition) {
    setBusyId(r.requisition_id);
    setActionErr(null);
    try {
      await receiveFloorRequisition(r.requisition_id);
      onChanged();
    } catch (e) {
      if (e instanceof RequisitionConflictError) onChanged();
      else setActionErr(friendlyApiError(e));
    } finally {
      setBusyId(null);
    }
  }

  function action(r: FloorRequisition) {
    if (r.status === "issued" && canReceive) {
      const busy = busyId === r.requisition_id;
      return (
        <button type="button" className={BTN} disabled={busy} onClick={() => void receive(r)}>
          {busy ? "Saving…" : "Mark received"}
        </button>
      );
    }
    if (r.status === "raised" && canCancel) {
      return <button type="button" className={BTN_LINK} onClick={() => setCancelling(r)}>Cancel</button>;
    }
    return null;
  }

  return (
    <div className={CARD}>
      <h4 className={`${HEADING} mb-2`}>
        Requisitions for this job card{rows ? ` (${rows.length})` : ""}
      </h4>
      {error ? (
        <p className="text-[12px] text-[var(--aws-error)]">
          {error}{" "}
          <button type="button" onClick={onRetry} className="underline">Retry</button>
        </p>
      ) : rows === null ? (
        <p className={HINT}>Loading requisitions…</p>
      ) : rows.length === 0 ? (
        <p className={HINT}>No requisitions raised for this job card yet.</p>
      ) : (
        <>
          {actionErr ? <p role="alert" className="mb-2 text-[12px] text-[var(--aws-error)]">{actionErr}</p> : null}
          <div className="hidden md:block overflow-x-auto">
            <table className={TABLE}>
              <thead>
                <tr>
                  <th className={TH}>Number</th>
                  <th className={TH}>Article</th>
                  <th className={`${TH} text-right`}>Requested</th>
                  <th className={TH}>Issued</th>
                  <th className={TH}>Status</th>
                  <th className={TH}>Raised</th>
                  {hasActions ? <th className={TH}>Action</th> : null}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.requisition_id}>
                    <td className={`${TD} font-mono whitespace-nowrap`}>#{r.requisition_id}</td>
                    <td className={`${TD} text-[var(--text-primary)]`}>
                      {r.material_sku_name}
                      {r.item_type ? <span className="text-[var(--text-muted)]"> · {r.item_type}</span> : null}
                    </td>
                    <td className={`${TD} text-right font-mono tabular-nums whitespace-nowrap`}>
                      {formatQty(r.requested_qty, r.requested_unit)}
                    </td>
                    <td className={TD}><Issued r={r} /></td>
                    <td className={TD}><Status r={r} /></td>
                    <td className={TD}><Raised r={r} /></td>
                    {hasActions ? <td className={TD}>{action(r)}</td> : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="md:hidden space-y-2">
            {rows.map((r) => {
              const act = action(r);
              return (
                <li key={r.requisition_id} className="border border-[var(--aws-border)] rounded-[2px] bg-white p-2.5">
                  <div className="flex items-start justify-between gap-2">
                    <span className="min-w-0 break-words text-[13px] font-medium text-[var(--text-primary)]">
                      {r.material_sku_name}
                    </span>
                    <span className="shrink-0 font-mono text-[12px] text-[var(--text-secondary)]">#{r.requisition_id}</span>
                  </div>
                  <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px]">
                    <dt className="text-[var(--text-muted)]">Status</dt>
                    <dd><Status r={r} /></dd>
                    <dt className="text-[var(--text-muted)]">Requested</dt>
                    <dd className="font-mono tabular-nums">{formatQty(r.requested_qty, r.requested_unit)}</dd>
                    <dt className="text-[var(--text-muted)]">Issued</dt>
                    <dd><Issued r={r} /></dd>
                    <dt className="text-[var(--text-muted)]">Raised</dt>
                    <dd><Raised r={r} /></dd>
                  </dl>
                  {act ? <div className="mt-2 border-t border-[var(--aws-border)] pt-2">{act}</div> : null}
                </li>
              );
            })}
          </ul>
        </>
      )}
      {cancelling ? (
        <CancelRequisitionDialog
          requisition={cancelling}
          onClose={() => setCancelling(null)}
          onDone={() => { setCancelling(null); onChanged(); }}
        />
      ) : null}
    </div>
  );
}
```

- [ ] **Step 2: Render it on the tab**

In `web_replica/src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx`:

(a) After `import { RequestDialog } from "./_RequestDialog";` add
```tsx
import { JobCardRequisitions } from "./_JobCardRequisitions";
```

(b) Between the BOM articles card and the Other stock card, replace
```tsx
          </div>

          <div className={CARD}>
            <button
              type="button"
              onClick={() => setOtherOpen((v) => !v)}
```
with
```tsx
          </div>

          {canViewReqs ? (
            <JobCardRequisitions rows={reqs} error={reqsErr} onRetry={reloadReqs} onChanged={reloadReqs} />
          ) : null}

          <div className={CARD}>
            <button
              type="button"
              onClick={() => setOtherOpen((v) => !v)}
```

(c) In the header comment, after the three lines added in Task 7 (a), add
```tsx
// Requests this job card has raised are listed under the BOM articles
// (_JobCardRequisitions), where the floor marks issued ones received.
```

- [ ] **Step 3: Run the checks**

From `web_replica`:
- `npx tsc --noEmit -p . 2>&1 | grep -v '^\.next/'` → no output
- `npx eslint "src/app/modules/job-card/[id]/_JobCardRequisitions.tsx" "src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx"` → 0 problems

- [ ] **Step 4: Checkpoint** — changed: `_JobCardRequisitions.tsx` (new), `_MaterialAllocationTab.tsx`. No commit.

---

### Task 9: Web — store screen (Production → Floor Requisitions)

**Files:**
- Create: `web_replica/src/app/modules/production/floor-requisitions/page.tsx`, `web_replica/src/app/modules/production/floor-requisitions/_IssueDialog.tsx`
- Modify: `web_replica/src/app/modules/production/page.tsx` (tile + gate), `web_replica/src/lib/modules.tsx` (store_head scope, line ~334)

**Interfaces:**
- Consumes: Task 6 (`listFloorRequisitions`, `issueFloorRequisition`, `RequisitionConflictError`, `FloorRequisition`, `FloorRequisitionPage`, `checkQty`, `formatQty`, `formatWhen`, `REQUISITION_STATUSES`, `STATUS_LABEL`, `RequisitionStatus`); Task 7 (`RequisitionModal`, `RequisitionFacts`, `StatusTag`, `CancelRequisitionDialog`, `FIELD`, `TEXTAREA`, `BTN`, `BTN_PRIMARY`, `BTN_LINK`); `FLOORS_BY_WAREHOUSE` from `@/lib/admin-api`; `useRequireAuth`, `useRequireModuleAccess(key, redirect)`, `useHasPermission`, `useMe`, `useUserInitial` from `@/lib/user`; `BrandMark`, `BackLink` from `@/components`.
- Produces: route `/modules/production/floor-requisitions`; `IssueDialog({ requisition, onClose, onDone: () => void })`.

- [ ] **Step 1: Record the ESLint baseline of the two files this task edits**

From `web_replica`: `npx eslint src/app/modules/production/page.tsx src/lib/modules.tsx` — note the problem count per file. Step 5 must show the same counts.

- [ ] **Step 2: Create the Issue dialog**

`web_replica/src/app/modules/production/floor-requisitions/_IssueDialog.tsx`:

```tsx
"use client";

// Issue dialog — store records what it sent against a raised floor requisition.
// Issued quantity opens with the requested quantity, in the request's unit. It
// may differ: less (the floor raises another request for the rest) or more.
// A 409 — someone already issued or cancelled it — closes and reloads the list.

import { useRef, useState, type FormEvent } from "react";
import {
  BTN, BTN_PRIMARY, FIELD, RequisitionFacts, RequisitionModal, TEXTAREA,
} from "@/components/floor-requisitions/RequisitionUi";
import { friendlyApiError } from "@/lib/apiErrors";
import { checkQty } from "@/lib/floor-requisition-form";
import { issueFloorRequisition, RequisitionConflictError, type FloorRequisition } from "@/lib/floor-requisitions";

export function IssueDialog({
  requisition, onClose, onDone,
}: {
  requisition: FloorRequisition;
  onClose: () => void;
  onDone: () => void;
}) {
  const qtyRef = useRef<HTMLInputElement>(null);
  const unit = requisition.requested_unit;
  const [qty, setQty] = useState(() => String(requisition.requested_qty));
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const checked = checkQty(qty, unit);
    if (!checked.ok) {
      setError(checked.message);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await issueFloorRequisition(requisition.requisition_id, {
        issued_qty: checked.value,
        issue_note: note.trim() || undefined,
      });
      onDone();
    } catch (err) {
      if (err instanceof RequisitionConflictError) onDone();
      else setError(friendlyApiError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <RequisitionModal title="Issue material" onClose={onClose} initialFocus={qtyRef}>
      <form onSubmit={submit} className="flex flex-col gap-3 p-4">
        <RequisitionFacts r={requisition} />
        <label className="flex flex-col gap-1 text-[12px] text-[var(--text-secondary)]">
          Issued quantity
          <span className="flex items-center gap-2">
            <input
              ref={qtyRef}
              inputMode="decimal"
              autoComplete="off"
              value={qty}
              onChange={(e) => setQty(e.target.value)}
              className={`${FIELD} min-w-0 flex-1 font-mono`}
            />
            <span className="text-[13px] text-[var(--text-primary)]">{unit}</span>
          </span>
        </label>
        <label className="flex flex-col gap-1 text-[12px] text-[var(--text-secondary)]">
          Note (optional)
          <textarea rows={2} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} className={TEXTAREA} />
        </label>
        {error ? <p role="alert" className="text-[12px] text-[var(--aws-error)]">{error}</p> : null}
        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" className={BTN} onClick={onClose}>Cancel</button>
          <button type="submit" className={BTN_PRIMARY} disabled={saving}>{saving ? "Issuing…" : "Issue"}</button>
        </div>
      </form>
    </RequisitionModal>
  );
}
```

- [ ] **Step 3: Create the store screen**

`web_replica/src/app/modules/production/floor-requisitions/page.tsx`:

```tsx
"use client";

// Production → Floor Requisitions — the store's side of floor requisitions.
//
// Material the floor asked for from a job card's Material allocation tab
// (server_replica app/modules/floor_requisition). Store issues each raised request
// with the quantity it actually sent, or cancels it with a reason; the floor then
// marks it received on the job card. Nothing here moves stock.
//
// Filtered and paged on the server, PAGE_SIZE rows a page — requests pile up over
// time. Opens on Raised, the ones waiting for store. The server also limits the
// list to the viewer's granted warehouses and floors.
//
// Layout: bordered table from md up, stacked cards below it.

import Link from "next/link";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { BackLink } from "@/components/BackLink";
import { BrandMark } from "@/components/BrandMark";
import {
  BTN_LINK, BTN_PRIMARY, CancelRequisitionDialog, FIELD, StatusTag,
} from "@/components/floor-requisitions/RequisitionUi";
import { FLOORS_BY_WAREHOUSE } from "@/lib/admin-api";
import { friendlyApiError } from "@/lib/apiErrors";
import {
  formatQty, formatWhen, REQUISITION_STATUSES, STATUS_LABEL, type RequisitionStatus,
} from "@/lib/floor-requisition-form";
import { listFloorRequisitions, type FloorRequisition, type FloorRequisitionPage } from "@/lib/floor-requisitions";
import { useHasPermission, useMe, useRequireAuth, useRequireModuleAccess, useUserInitial } from "@/lib/user";
import { IssueDialog } from "./_IssueDialog";

const PAGE_SIZE = 100;
const PLANTS = Object.keys(FLOORS_BY_WAREHOUSE);

const CARD =
  "bg-white border border-[var(--aws-border)] rounded-md shadow-[0_1px_1px_rgba(0,28,36,0.18)] p-3 sm:p-4 mb-4";
const TABLE = "w-full text-[12px] border-collapse";
const TH =
  "border border-[var(--aws-border)] bg-[#fafafa] px-2.5 py-2 text-left text-[10px] font-bold uppercase " +
  "tracking-wide text-[var(--text-secondary)] whitespace-nowrap";
const TD = "border border-[var(--aws-border)] px-2.5 py-2 align-top";
const HINT = "text-[12px] text-[var(--text-muted)] italic";
const SUB = "text-[11px] text-[var(--text-muted)] break-words";
const PAGE_BTN =
  "h-7 px-2.5 rounded-[2px] border border-[var(--aws-border-strong)] bg-white text-[12px] " +
  "hover:border-[var(--aws-navy)] disabled:opacity-50 disabled:cursor-not-allowed";

function Chrome({ children }: { children: ReactNode }) {
  const router = useRouter();
  const initial = useUserInitial();
  return (
    <div className="min-h-screen flex flex-col bg-[var(--background)]">
      <header className="bg-[var(--aws-navy)] h-[45px] flex items-center px-4 sm:px-6 gap-4">
        <BrandMark />
        <span className="text-[#d5dbdb] text-[13px] hidden sm:inline">Console</span>
        <nav className="text-[12px] text-[#d5dbdb] hidden md:flex items-center gap-2 ml-2">
          <button onClick={() => router.push("/modules")} className="hover:underline">Modules</button>
          <span>/</span>
          <button onClick={() => router.push("/modules/production")} className="hover:underline">Production</button>
          <span>/</span>
          <span className="text-white">Floor Requisitions</span>
        </nav>
        <div className="flex-1" />
        <button
          onClick={() => router.push("/modules/profile")}
          aria-label="Open profile"
          title="Profile"
          className="w-8 h-8 rounded-full bg-[var(--aws-orange)] text-white text-[13px] font-bold flex items-center justify-center hover:bg-[var(--aws-orange-hover)]"
        >
          {initial}
        </button>
      </header>
      <main className="flex-1 max-w-[1280px] w-full mx-auto px-4 sm:px-6 py-6">
        <div className="mb-3">
          <BackLink parentHref="/modules/production" label="production" />
        </div>
        {children}
      </main>
    </div>
  );
}

function Issued({ r }: { r: FloorRequisition }) {
  if (r.issued_qty == null) return <span className="text-[var(--text-muted)]">—</span>;
  return (
    <span className="inline-flex flex-col gap-0.5">
      <span className="font-mono tabular-nums whitespace-nowrap">{formatQty(r.issued_qty, r.requested_unit)}</span>
      <span className={SUB}>{r.issued_by} · {formatWhen(r.issued_at)}</span>
    </span>
  );
}

export default function FloorRequisitionsPage() {
  const router = useRouter();
  useRequireAuth(router.replace);
  useRequireModuleAccess("production/floor-requisitions", router.replace);
  const me = useMe();
  const canView = useHasPermission("production", "floor_requisitions", null, "view");
  const canIssue = useHasPermission("production", "floor_requisitions", null, "issue");
  const canCancel = useHasPermission("production", "floor_requisitions", null, "cancel");

  const [status, setStatus] = useState<RequisitionStatus | "">("raised");
  const [plant, setPlant] = useState("");
  const [floor, setFloor] = useState("");
  const [search, setSearch] = useState("");
  const [searchApplied, setSearchApplied] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<FloorRequisitionPage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [attempt, setAttempt] = useState(0); // bumped by Retry and after every change
  const [issuing, setIssuing] = useState<FloorRequisition | null>(null);
  const [cancelling, setCancelling] = useState<FloorRequisition | null>(null);

  // Search is sent a moment after typing stops, not on every key.
  useEffect(() => {
    const t = setTimeout(() => {
      setSearchApplied(search.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [search]);

  useEffect(() => {
    if (!canView) return;
    const ctrl = new AbortController();
    queueMicrotask(() => {
      setLoading(true);
      listFloorRequisitions(
        { status, warehouse: plant, floor, search: searchApplied, page, pageSize: PAGE_SIZE },
        ctrl.signal,
      )
        .then((p) => { if (!ctrl.signal.aborted) { setData(p); setErr(null); } })
        .catch((e: unknown) => { if (!ctrl.signal.aborted) setErr(friendlyApiError(e)); })
        .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
    });
    return () => ctrl.abort();
  }, [canView, status, plant, floor, searchApplied, page, attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  const pageCount = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  // Issuing the last row of the last page shrinks the list under the reader:
  // step back to the page that now exists.
  useEffect(() => {
    if (data && page > pageCount) queueMicrotask(() => setPage(pageCount));
  }, [data, page, pageCount]);

  if (!me) {
    return <Chrome><div className={CARD}><p className={HINT}>Loading…</p></div></Chrome>;
  }
  if (!canView) {
    return (
      <Chrome>
        <div className={CARD}>
          <p className={HINT}>
            You don&apos;t have access to floor requisitions. Ask an admin for the Floor Requisitions view permission.
          </p>
        </div>
      </Chrome>
    );
  }

  const floors = plant
    ? FLOORS_BY_WAREHOUSE[plant] ?? []
    : [...new Set(Object.values(FLOORS_BY_WAREHOUSE).flat())];
  const filtering = status !== "raised" || plant !== "" || floor !== "" || search.trim() !== "";
  const showActions = canIssue || canCancel;

  function action(r: FloorRequisition) {
    if (r.status !== "raised") return null;
    return (
      <span className="inline-flex flex-wrap items-center gap-2">
        {canIssue ? <button type="button" className={BTN_PRIMARY} onClick={() => setIssuing(r)}>Issue</button> : null}
        {canCancel ? <button type="button" className={BTN_LINK} onClick={() => setCancelling(r)}>Cancel</button> : null}
      </span>
    );
  }

  const pager = data && pageCount > 1 ? (
    <nav aria-label="Requisition pages" className="flex flex-wrap items-center justify-between gap-2 text-[12px]">
      <span className="text-[var(--text-secondary)]">
        {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, data.total)} of {data.total}
      </span>
      <div className="flex items-center gap-1.5">
        <button type="button" className={PAGE_BTN} disabled={page <= 1} onClick={() => setPage(page - 1)}>‹ Prev</button>
        <span className="px-1 whitespace-nowrap text-[var(--text-secondary)]">Page {page} of {pageCount}</span>
        <button type="button" className={PAGE_BTN} disabled={page >= pageCount} onClick={() => setPage(page + 1)}>Next ›</button>
      </div>
    </nav>
  ) : null;

  return (
    <Chrome>
      <div className="flex flex-wrap items-baseline justify-between gap-3 mb-4">
        <h1 className="text-[20px] leading-[24px] font-semibold text-[var(--text-primary)]">Floor Requisitions</h1>
        {data ? (
          <span className="text-[12px] text-[var(--text-secondary)]">
            {data.total} request{data.total !== 1 ? "s" : ""}{loading ? " · refreshing…" : ""}
          </span>
        ) : null}
      </div>

      <div className={CARD}>
        {/* Filters: stacked full-width on a phone, one wrapping row from sm up. */}
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search article"
            aria-label="Search article"
            className={`${FIELD} w-full sm:w-auto sm:flex-1 sm:min-w-[220px]`}
          />
          <div className="flex flex-wrap gap-2">
            <select
              value={status}
              onChange={(e) => { setStatus(e.target.value as RequisitionStatus | ""); setPage(1); }}
              aria-label="Filter by status"
              className={`${FIELD} flex-1 sm:flex-none`}
            >
              <option value="">Status: all</option>
              {REQUISITION_STATUSES.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
            </select>
            <select
              value={plant}
              onChange={(e) => {
                const next = e.target.value;
                setPlant(next);
                if (next && !(FLOORS_BY_WAREHOUSE[next] ?? []).includes(floor)) setFloor("");
                setPage(1);
              }}
              aria-label="Filter by plant"
              className={`${FIELD} flex-1 sm:flex-none`}
            >
              <option value="">Plant: all</option>
              {PLANTS.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            <select
              value={floor}
              onChange={(e) => { setFloor(e.target.value); setPage(1); }}
              aria-label="Filter by floor"
              className={`${FIELD} flex-1 sm:flex-none`}
            >
              <option value="">Floor: all</option>
              {floors.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </div>
          {filtering ? (
            <button
              type="button"
              className={BTN_LINK}
              onClick={() => { setStatus("raised"); setPlant(""); setFloor(""); setSearch(""); setPage(1); }}
            >
              Reset
            </button>
          ) : null}
        </div>
      </div>

      <div className={CARD}>
        {err ? (
          <p className="text-[12px] text-[var(--aws-error)]">
            {err}{" "}
            <button type="button" onClick={reload} className="underline">Retry</button>
          </p>
        ) : !data ? (
          <p className={HINT}>Loading requisitions…</p>
        ) : data.items.length === 0 ? (
          <p className={HINT}>
            {status === "raised" && !filtering ? "Nothing is waiting for store." : "No requisitions match these filters."}
          </p>
        ) : (
          <>
            {pager ? <div className="mb-2">{pager}</div> : null}
            <div className="hidden md:block overflow-x-auto">
              <table className={TABLE}>
                <thead>
                  <tr>
                    <th className={TH}>Number</th>
                    <th className={TH}>Job card</th>
                    <th className={TH}>Place</th>
                    <th className={TH}>Article</th>
                    <th className={`${TH} text-right`}>Requested</th>
                    <th className={`${TH} text-right`}>Short when raised</th>
                    <th className={TH}>Status</th>
                    <th className={TH}>Raised</th>
                    <th className={TH}>Issued</th>
                    {showActions ? <th className={TH}>Action</th> : null}
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((r) => (
                    <tr key={r.requisition_id}>
                      <td className={`${TD} font-mono whitespace-nowrap`}>#{r.requisition_id}</td>
                      <td className={`${TD} whitespace-nowrap`}>
                        <Link href={`/modules/job-card/${r.job_card_id}`} className="text-[var(--aws-link)] underline">
                          {r.job_card_id}
                        </Link>
                      </td>
                      <td className={TD}>{r.warehouse} · {r.floor}</td>
                      <td className={`${TD} text-[var(--text-primary)]`}>
                        {r.material_sku_name}
                        {r.item_type ? <span className="text-[var(--text-muted)]"> · {r.item_type}</span> : null}
                        {r.note ? <span className={`block ${SUB}`}>{r.note}</span> : null}
                      </td>
                      <td className={`${TD} text-right font-mono tabular-nums whitespace-nowrap`}>
                        {formatQty(r.requested_qty, r.requested_unit)}
                      </td>
                      <td className={`${TD} text-right font-mono tabular-nums whitespace-nowrap`}>
                        {r.shortage_qty != null ? formatQty(r.shortage_qty, r.requested_unit) : "—"}
                      </td>
                      <td className={TD}>
                        <StatusTag status={r.status} />
                        {r.status === "cancelled" && r.cancel_reason ? <span className={`block ${SUB}`}>{r.cancel_reason}</span> : null}
                      </td>
                      <td className={TD}>
                        <span className="block break-words">{r.raised_by}</span>
                        <span className={SUB}>{formatWhen(r.raised_at)}</span>
                      </td>
                      <td className={TD}><Issued r={r} /></td>
                      {showActions ? <td className={TD}>{action(r) ?? <span className="text-[var(--text-muted)]">—</span>}</td> : null}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ul className="md:hidden space-y-2">
              {data.items.map((r) => {
                const act = showActions ? action(r) : null;
                return (
                  <li key={r.requisition_id} className="border border-[var(--aws-border)] rounded-[2px] bg-white p-2.5">
                    <div className="flex items-start justify-between gap-2">
                      <span className="min-w-0 break-words text-[13px] font-medium text-[var(--text-primary)]">
                        {r.material_sku_name}
                      </span>
                      <StatusTag status={r.status} />
                    </div>
                    <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px]">
                      <dt className="text-[var(--text-muted)]">Number</dt>
                      <dd className="font-mono">#{r.requisition_id}</dd>
                      <dt className="text-[var(--text-muted)]">Job card</dt>
                      <dd>
                        <Link href={`/modules/job-card/${r.job_card_id}`} className="text-[var(--aws-link)] underline">
                          {r.job_card_id}
                        </Link>
                      </dd>
                      <dt className="text-[var(--text-muted)]">Place</dt>
                      <dd className="break-words">{r.warehouse} · {r.floor}</dd>
                      <dt className="text-[var(--text-muted)]">Requested</dt>
                      <dd className="font-mono tabular-nums">{formatQty(r.requested_qty, r.requested_unit)}</dd>
                      <dt className="text-[var(--text-muted)]">Raised</dt>
                      <dd className="break-words">{r.raised_by} · {formatWhen(r.raised_at)}</dd>
                      <dt className="text-[var(--text-muted)]">Issued</dt>
                      <dd><Issued r={r} /></dd>
                    </dl>
                    {act ? <div className="mt-2 border-t border-[var(--aws-border)] pt-2">{act}</div> : null}
                  </li>
                );
              })}
            </ul>
            {pager ? <div className="mt-3">{pager}</div> : null}
          </>
        )}
      </div>

      {issuing ? (
        <IssueDialog
          requisition={issuing}
          onClose={() => setIssuing(null)}
          onDone={() => { setIssuing(null); reload(); }}
        />
      ) : null}
      {cancelling ? (
        <CancelRequisitionDialog
          requisition={cancelling}
          onClose={() => setCancelling(null)}
          onDone={() => { setCancelling(null); reload(); }}
        />
      ) : null}
    </Chrome>
  );
}
```

- [ ] **Step 4: The Production tile and the store_head scope**

In `web_replica/src/app/modules/production/page.tsx`:

(a) After the `Production Indents` entry in `SUB_MODULES` add
```tsx
  { group: "Inventory",  title: "Floor Requisitions", description: "Material the floor requested from job cards · issue it, or cancel it with a reason.",                                                   route: "/modules/production/floor-requisitions", implemented: true },
```

(b) After `  const canSeePurchIndents = useHasPermission("production", "indents", null, "view");` add
```tsx
  // Floor Requisitions: store issues what the floor requested from a job card.
  const canSeeFloorReqs = useHasPermission("production", "floor_requisitions", null, "view");
```

(c) After `    if (route === "/modules/production/prod-indents") return canSeeProdIndents || canSeePurchIndents;` add
```tsx
    if (route === "/modules/production/floor-requisitions") return canSeeFloorReqs;
```

In `web_replica/src/lib/modules.tsx`, replace
```tsx
  store_head:    ["purchase/material-in"],
```
with
```tsx
  // …plus Floor Requisitions, where stores issues what the floor asked for from a
  // job card (production.floor_requisitions.{view,issue,cancel}, app/db/111).
  store_head:    ["purchase/material-in", "production/floor-requisitions"],
```

- [ ] **Step 5: Run the checks**

From `web_replica`:
- `npx tsc --noEmit -p . 2>&1 | grep -v '^\.next/'` → no output
- `npx eslint src/app/modules/production/floor-requisitions/page.tsx src/app/modules/production/floor-requisitions/_IssueDialog.tsx` → 0 problems
- `npx eslint src/app/modules/production/page.tsx src/lib/modules.tsx` → the Step 1 counts, unchanged

- [ ] **Step 6: Checkpoint** — changed: `floor-requisitions/page.tsx`, `floor-requisitions/_IssueDialog.tsx` (new), `production/page.tsx`, `lib/modules.tsx`. No commit.

---

### Task 10: Whole-feature verification and hand-over

**Files:** none changed — checks only (plus the migration, applied by the user).

**Interfaces:**
- Consumes: everything from Tasks 1–9.

- [ ] **Step 1: Full server suite**

From `server_replica`: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q`
Expected: every new test passes; the only failures are the 2 pre-existing ones in `tests/services/test_sku_lookup_permission.py`.

- [ ] **Step 2: All web checks**

From `web_replica`:
- `node src/lib/floor-requisition-form.test.ts` → all checks passed
- `npx tsc --noEmit -p . 2>&1 | grep -v '^\.next/'` → no output
- `npx eslint src/lib/floor-requisition-form.ts src/lib/floor-requisition-form.test.ts src/lib/floor-requisitions.ts src/components/floor-requisitions/RequisitionUi.tsx "src/app/modules/job-card/[id]/_RequestDialog.tsx" "src/app/modules/job-card/[id]/_JobCardRequisitions.tsx" "src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx" src/app/modules/production/floor-requisitions/page.tsx src/app/modules/production/floor-requisitions/_IssueDialog.tsx` → 0 problems
- `npx eslint "src/app/modules/job-card/[id]/page.tsx"` → 11 (baseline)

- [ ] **Step 3: Hand the migration to the user — do NOT apply it yourself**

Tell the user migration `app/db/111_floor_requisition.sql` must be applied to the RDS `warehouse_db` before the API is restarted, e.g.
`psql "<warehouse_db url>" -v ON_ERROR_STOP=1 -f app/db/111_floor_requisition.sql`
— that one file only, never `scripts/migrate.py`. Apply it only if the user explicitly says to.

Once applied, confirm with READ-ONLY queries (session set `READ ONLY` first):

```sql
SELECT to_regclass('public.floor_requisition') IS NOT NULL AS table_exists;          -- true
SELECT indexname FROM pg_indexes WHERE tablename = 'floor_requisition' ORDER BY 1;
-- floor_requisition_pkey, idx_floor_requisition_job_card,
-- idx_floor_requisition_place, uq_floor_requisition_open
SELECT action, COUNT(*) FROM auth_permission
 WHERE module = 'production' AND sub_module = 'floor_requisitions' AND sub_sub_module IS NULL
 GROUP BY action ORDER BY action;                                                     -- 5 rows, each 1
SELECT r.role_name, p.action
  FROM auth_role_permission rp
  JOIN auth_role r USING (role_id)
  JOIN auth_permission p USING (permission_id)
 WHERE p.module = 'production' AND p.sub_module = 'floor_requisitions'
 ORDER BY 1, 2;                                                                       -- 12 rows, as the grants table
```

- [ ] **Step 4: Manual check in the browser (after migration, API restart and web rebuild)**

1. As a `floor_manager` (or admin), open a job card whose BOM article is short (e.g. "Short by 88.200 kg") → Material allocation and requisition. The BOM table has a **Request** column.
2. Click **Request**: the dialog shows the article, plant · floor, Required / Fresh stock / Shortage, and **Quantity = 88.2 kg**. Type `1.2345` → "Kilograms go to 3 decimals at most."; set a valid quantity → **Raise request**.
3. The cell now shows `#<8 digits> · Raised · <qty>`; **Requisitions for this job card** lists it. A PM article shows its quantity in pcs.
4. As `store_head`: /modules shows Production; **Floor Requisitions** lists the request under Raised. **Issue** opens with the requested quantity; issue less → it leaves the Raised list; under Status: all it reads Issued.
5. Back on the job card as the floor: the row shows **Mark received** → Received. The Request button is available again for that article.
6. Raise another and **Cancel** it with a reason from either screen → Cancelled with the reason shown.
7. Two tabs, same raised request: issue in one, cancel in the other → the second just reloads showing Issued, no red error.
8. At ~400px width both screens show stacked cards, the dialogs are full-screen, nothing scrolls sideways.

- [ ] **Step 5: Report**

Report to the user what was built, the test results with their output counts, that the migration is theirs to apply (or was applied on their instruction and verified), what was checked by hand and what was not, and that nothing is committed.

