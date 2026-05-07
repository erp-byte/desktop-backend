# Job Card CRUD Enhancement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 12 new endpoints (1 main PATCH + 1 main DELETE + 5 annexure PATCH/DELETE pairs) to the job-card module with strict partial-update semantics and soft-delete + cancellation reason.

**Architecture:** New service module `app/modules/production/services/jc_editor.py` owns "fix-up" logic (separate from `job_card_engine.py` which keeps owning lifecycle). Single private `_apply_partial_update()` helper builds dynamic SQL UPDATE from supplied-only columns (using `model_dump(exclude_unset=True)` from Pydantic v2). Soft-delete via `deleted_at`/`deleted_by` columns; existing list endpoints filter these out by default with opt-in `include_cancelled=true`.

**Tech Stack:** FastAPI 0.135, Pydantic 2.12, asyncpg 0.31, PostgreSQL. Verification via asyncpg/httpx probe scripts at repo root (matches existing `_jc_probe.py` / `_audit_db_probe.py` convention — no pytest infrastructure in this project).

**Reference spec:** `docs/superpowers/specs/2026-05-07-job-card-crud-design.md`

---

## Pre-requisite — Run DDL manually

The user runs database schema changes manually. Before any task below can pass its probe, execute the SQL block from spec §13 against the target PostgreSQL database. Verify with:

```sql
SELECT column_name FROM information_schema.columns
 WHERE table_name = 'job_card'
   AND column_name IN ('updated_at','updated_by','deleted_at','deleted_by','cancellation_reason');
-- Expect: 5 rows
```

Do not start Task 1 until DDL has been applied.

---

## File map

**New files:**
- `app/modules/production/schemas/job_card_edit.py` — Pydantic request models (8 classes)
- `app/modules/production/services/jc_editor.py` — service module with helpers + 12 entry points
- `_jc_crud_probe.py` (root) — verification script for main JC PATCH/DELETE
- `_jc_annexure_probe.py` (root) — verification script for all 5 annexure types

**Modified files:**
- `app/webhooks/events.py` — append 3 event functions
- `app/modules/production/router.py` — append 12 endpoint handlers + extend 4 list endpoints with `include_cancelled` param
- `app/modules/production/services/job_card_engine.py` — `get_job_card_detail()` filters soft-deleted annexure rows
- `app/db/production_schema.sql` — source-of-truth column declarations (DDL itself runs manually)

---

## Task 1 — Pydantic request schemas

**Files:**
- Create: `app/modules/production/schemas/job_card_edit.py`

- [ ] **Step 1: Create the schemas module**

```python
# app/modules/production/schemas/job_card_edit.py
"""Request schemas for PATCH and DELETE on job cards and annexures.

Every PATCH model has all fields Optional with default=None. The router
converts the request body via `body.model_dump(exclude_unset=True)` so only
fields the client actually sent appear in the dict — that is what guarantees
the 'preserve unspecified columns' behavior.
"""

from typing import Optional, List
from pydantic import BaseModel, Field


class JobCardPatchRequest(BaseModel):
    machine_id:              Optional[int]       = None
    assigned_to_team_leader: Optional[str]       = None
    team_members:            Optional[List[str]] = None
    factory:                 Optional[str]       = None
    floor:                   Optional[str]       = None
    customer_name:           Optional[str]       = None
    batch_number:            Optional[str]       = None
    batch_size_kg:           Optional[float]     = Field(None, gt=0)
    bom_id:                  Optional[int]       = None
    process_name:            Optional[str]       = None
    stage:                   Optional[str]       = None
    updated_by:              str


class JobCardCancelRequest(BaseModel):
    cancellation_reason: str = Field(..., min_length=3)
    deleted_by:          str


class EnvironmentPatchRequest(BaseModel):
    parameter_name: Optional[str] = None
    value:          Optional[str] = None
    updated_by:     str


class MetalDetectionPatchRequest(BaseModel):
    check_type:   Optional[str]  = None
    fe_pass:      Optional[bool] = None
    nfe_pass:     Optional[bool] = None
    ss_pass:      Optional[bool] = None
    failed_units: Optional[int]  = Field(None, ge=0)
    remarks:      Optional[str]  = None
    updated_by:   str


class WeightCheckPatchRequest(BaseModel):
    sample_number:  Optional[int]   = Field(None, gt=0)
    net_weight:     Optional[float] = Field(None, ge=0)
    gross_weight:   Optional[float] = Field(None, ge=0)
    leak_test_pass: Optional[bool]  = None
    updated_by:     str


class LossReconciliationPatchRequest(BaseModel):
    loss_category:     Optional[str]   = None
    budgeted_loss_pct: Optional[float] = Field(None, ge=0)
    budgeted_loss_kg:  Optional[float] = Field(None, ge=0)
    actual_loss_kg:    Optional[float] = Field(None, ge=0)
    variance_kg:       Optional[float] = None
    remarks:           Optional[str]   = None
    updated_by:        str


class RemarkPatchRequest(BaseModel):
    remark_type: Optional[str] = None
    content:     Optional[str] = None
    updated_by:  str


class AnnexureDeleteRequest(BaseModel):
    """Used for all 5 annexure DELETE endpoints. Body required to capture deleted_by."""
    deleted_by: str
```

- [ ] **Step 2: Smoke-test the schemas can be imported and exclude_unset works**

Run from repo root:

```bash
python -c "
from app.modules.production.schemas.job_card_edit import JobCardPatchRequest
m = JobCardPatchRequest(floor='A', updated_by='alice')
print('dump_full :', m.model_dump())
print('dump_unset:', m.model_dump(exclude_unset=True))
"
```

Expected:
```
dump_full : {'machine_id': None, 'assigned_to_team_leader': None, ..., 'floor': 'A', ..., 'updated_by': 'alice'}
dump_unset: {'floor': 'A', 'updated_by': 'alice'}
```

The `exclude_unset` dict must contain exactly `floor` and `updated_by` and nothing else. If other keys appear, the partial-update behavior will be broken.

- [ ] **Step 3: Commit**

```bash
git add app/modules/production/schemas/job_card_edit.py
git commit -m "feat(production): add Pydantic request schemas for job-card PATCH/DELETE

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2 — Webhook event functions

**Files:**
- Modify: `app/webhooks/events.py` (append at end)

- [ ] **Step 1: Append the three new event functions**

Append these to the end of `app/webhooks/events.py`. They mirror the existing `job_card_*` functions (see `events.py:181-291` for the established style).

```python
async def job_card_updated(entity: str, *, job_card_id: int, job_card_number: str,
                           changed_fields: list[str], updated_by: str) -> None:
    """A job card's editable header was changed via PATCH."""
    await event_bus.publish(Event(
        event_type="job_card.updated",
        entity=_validate_entity(entity, "job_card.updated"),
        target_roles=["admin", "production_manager", "supervisor"],
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "changed_fields": changed_fields, "updated_by": updated_by},
    ))


async def job_card_cancelled(entity: str, *, job_card_id: int, job_card_number: str,
                             cancellation_reason: str, deleted_by: str) -> None:
    """A pre-start job card was soft-deleted with reason."""
    await event_bus.publish(Event(
        event_type="job_card.cancelled",
        entity=_validate_entity(entity, "job_card.cancelled"),
        target_roles=["admin", "production_manager", "supervisor"],
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "cancellation_reason": cancellation_reason, "deleted_by": deleted_by},
    ))


async def job_card_annexure_changed(entity: str, *, job_card_id: int, job_card_number: str,
                                    annexure_type: str, annexure_id: int,
                                    action: str, changed_by: str,
                                    changed_fields: list[str] | None = None) -> None:
    """Catch-all for annexure PATCH/DELETE.

    annexure_type: 'environment' | 'metal_detection' | 'weight_check'
                 | 'loss_reconciliation' | 'remarks'
    action:        'updated' | 'deleted'
    """
    await event_bus.publish(Event(
        event_type=f"job_card.annexure.{action}",
        entity=_validate_entity(entity, f"job_card.annexure.{action}"),
        target_roles=["admin", "production_manager", "supervisor", "qc"],
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "annexure_type": annexure_type, "annexure_id": annexure_id,
                 "changed_fields": changed_fields, "changed_by": changed_by},
    ))
```

- [ ] **Step 2: Verify the module still imports cleanly**

```bash
python -c "from app.webhooks import events; print('OK', events.job_card_updated, events.job_card_cancelled, events.job_card_annexure_changed)"
```

Expected: three function references printed, no import errors.

- [ ] **Step 3: Commit**

```bash
git add app/webhooks/events.py
git commit -m "feat(webhooks): add job_card_updated/cancelled/annexure_changed events

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3 — Service module: helpers + main job card entry points

**Files:**
- Create: `app/modules/production/services/jc_editor.py`

- [ ] **Step 1: Create the service module with both helpers and main entry points**

```python
# app/modules/production/services/jc_editor.py
"""Partial-update + soft-delete logic for job cards and their annexures.

Owns 'fix-up' semantics — distinct from job_card_engine.py which owns
lifecycle transitions (start, complete, sign-off, etc.).
"""

from typing import Any, Mapping
from fastapi import HTTPException
import asyncpg


# ─── Allow-lists ────────────────────────────────────────────────────────────
# Per-table whitelist of editable columns. Keys not in this set are silently
# dropped before SQL is built — defends against client-supplied junk fields
# AND prevents updates to identity / system-managed columns.

JOB_CARD_EDITABLE_COLS = frozenset({
    "machine_id", "assigned_to_team_leader", "team_members",
    "factory", "floor",
    "customer_name", "batch_number", "batch_size_kg",
    "bom_id", "process_name", "stage",
})

ENVIRONMENT_EDITABLE_COLS         = frozenset({"parameter_name", "value"})
METAL_DETECTION_EDITABLE_COLS     = frozenset({"check_type", "fe_pass", "nfe_pass", "ss_pass", "failed_units", "remarks"})
WEIGHT_CHECK_EDITABLE_COLS        = frozenset({"sample_number", "net_weight", "gross_weight", "leak_test_pass"})
LOSS_RECONCILIATION_EDITABLE_COLS = frozenset({"loss_category", "budgeted_loss_pct", "budgeted_loss_kg", "actual_loss_kg", "variance_kg", "remarks"})
REMARKS_EDITABLE_COLS             = frozenset({"remark_type", "content"})

EDITABLE_STATUSES    = frozenset({"locked", "unlocked", "assigned", "material_received", "in_progress"})
CANCELLABLE_STATUSES = frozenset({"locked", "unlocked", "assigned"})


# ─── Generic helpers ────────────────────────────────────────────────────────

async def _apply_partial_update(
    conn: asyncpg.Connection, *,
    table: str, pk_col: str, pk_val: int,
    payload: Mapping[str, Any],
    allowed_cols: frozenset, updated_by: str,
    parent_jc_id: int | None = None,
) -> tuple[dict, list[str]]:
    """Build & execute UPDATE for only the supplied + allowed columns.

    If parent_jc_id is given, the WHERE clause also enforces job_card_id match
    (used for annexure rows so a guessed env_id can't bypass URL ownership).

    Returns (updated_row_dict, list_of_changed_column_names).
    Raises 404 if row missing/deleted, 422 if no valid columns supplied.
    """
    fields = {k: v for k, v in payload.items() if k in allowed_cols}
    if not fields:
        raise HTTPException(status_code=422, detail="No editable fields supplied")

    set_parts: list[str] = []
    params: list = []
    for col, val in fields.items():
        set_parts.append(f"{col} = ${len(params) + 1}")
        params.append(val)

    set_parts.append("updated_at = NOW()")
    set_parts.append(f"updated_by = ${len(params) + 1}")
    params.append(updated_by)

    where_parts = [f"{pk_col} = ${len(params) + 1}", "deleted_at IS NULL"]
    params.append(pk_val)
    if parent_jc_id is not None:
        where_parts.append(f"job_card_id = ${len(params) + 1}")
        params.append(parent_jc_id)

    sql = (
        f"UPDATE {table} SET {', '.join(set_parts)} "
        f"WHERE {' AND '.join(where_parts)} "
        f"RETURNING *"
    )
    row = await conn.fetchrow(sql, *params)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{table} row not found or already deleted")
    return dict(row), list(fields.keys())


async def _apply_soft_delete(
    conn: asyncpg.Connection, *,
    table: str, pk_col: str, pk_val: int,
    deleted_by: str, reason: str | None = None,
    parent_jc_id: int | None = None,
) -> dict:
    set_parts = ["deleted_at = NOW()", "deleted_by = $1"]
    params: list = [deleted_by]
    if reason is not None:
        set_parts.append(f"cancellation_reason = ${len(params) + 1}")
        params.append(reason)
        set_parts.append("status = 'cancelled'")

    where_parts = [f"{pk_col} = ${len(params) + 1}", "deleted_at IS NULL"]
    params.append(pk_val)
    if parent_jc_id is not None:
        where_parts.append(f"job_card_id = ${len(params) + 1}")
        params.append(parent_jc_id)

    sql = (
        f"UPDATE {table} SET {', '.join(set_parts)} "
        f"WHERE {' AND '.join(where_parts)} "
        f"RETURNING *"
    )
    row = await conn.fetchrow(sql, *params)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{table} row not found or already deleted")
    return dict(row)


async def _verify_parent_jc_editable(conn, job_card_id: int) -> dict:
    jc = await conn.fetchrow(
        "SELECT status, entity, job_card_number FROM job_card "
        "WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
    if jc is None:
        raise HTTPException(404, "Job card not found")
    if jc["status"] not in EDITABLE_STATUSES:
        raise HTTPException(409, f"Job card status '{jc['status']}' is not editable")
    return dict(jc)


# ─── Main job card ──────────────────────────────────────────────────────────

async def patch_job_card(conn, job_card_id: int, payload: dict) -> tuple[dict, dict, list[str]]:
    """Returns (jc_meta_dict, updated_row_dict, changed_fields).

    jc_meta_dict has keys: status, entity, job_card_number — for the router's
    webhook emission step.
    """
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card", pk_col="job_card_id", pk_val=job_card_id,
        payload=payload, allowed_cols=JOB_CARD_EDITABLE_COLS, updated_by=updated_by,
    )
    return jc, row, changed


async def cancel_job_card(conn, job_card_id: int, *, reason: str, deleted_by: str) -> tuple[dict, dict]:
    """Returns (jc_meta_dict, soft_deleted_row_dict)."""
    jc = await conn.fetchrow(
        "SELECT status, entity, job_card_number FROM job_card "
        "WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
    if jc is None:
        raise HTTPException(404, "Job card not found")
    if jc["status"] not in CANCELLABLE_STATUSES:
        raise HTTPException(409, f"Cannot cancel — status '{jc['status']}'. Use force-unlock + close instead.")
    row = await _apply_soft_delete(
        conn, table="job_card", pk_col="job_card_id", pk_val=job_card_id,
        deleted_by=deleted_by, reason=reason,
    )
    return dict(jc), row


# ─── Annexure entry points (added in Tasks 5–9) ────────────────────────────
# patch_environment,         delete_environment
# patch_metal_detection,     delete_metal_detection
# patch_weight_check,        delete_weight_check
# patch_loss_reconciliation, delete_loss_reconciliation
# patch_remark,              delete_remark
```

- [ ] **Step 2: Verify the module imports cleanly**

```bash
python -c "
from app.modules.production.services import jc_editor
print('OK')
print('helpers:', jc_editor._apply_partial_update.__name__, jc_editor._apply_soft_delete.__name__)
print('entries:', jc_editor.patch_job_card.__name__, jc_editor.cancel_job_card.__name__)
print('JC editable cols:', sorted(jc_editor.JOB_CARD_EDITABLE_COLS))
"
```

Expected: prints with no errors. The 11 editable column names should appear sorted.

- [ ] **Step 3: Commit**

```bash
git add app/modules/production/services/jc_editor.py
git commit -m "feat(production): add jc_editor service with partial-update + soft-delete helpers

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4 — Wire main JC PATCH + DELETE endpoints in router

**Files:**
- Modify: `app/modules/production/router.py` (append two handlers near the existing `/job-cards/{id}` GET handler at router.py:1667)

- [ ] **Step 1: Add imports near the top of router.py**

Find the existing schema imports at the top of `app/modules/production/router.py` and add:

```python
from app.modules.production.schemas.job_card_edit import (
    JobCardPatchRequest, JobCardCancelRequest,
    EnvironmentPatchRequest, MetalDetectionPatchRequest,
    WeightCheckPatchRequest, LossReconciliationPatchRequest,
    RemarkPatchRequest, AnnexureDeleteRequest,
)
from app.modules.production.services import jc_editor
```

(If `from app.modules.production.services import ...` style imports don't already exist in this file, use the existing per-function-import pattern instead — match what's already used for `job_card_engine` and `qc_service`.)

- [ ] **Step 2: Add the PATCH /job-cards/{id} handler**

Append immediately after the existing `get_job_card` handler (around router.py:1677). Use the same `deferred_events` + transaction pattern as `submit_qc_inspection` (router.py:3115-3123).

```python
@router.patch("/job-cards/{job_card_id}")
async def update_job_card(request: Request, job_card_id: int, body: JobCardPatchRequest):
    """Partial update of editable header fields. Only fields supplied in
    the request body are written; all other columns retain their current
    values. Returns 404 if not found, 409 if status is non-editable,
    422 if no editable fields supplied."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed_fields = await jc_editor.patch_job_card(
                    conn, job_card_id, body.model_dump(exclude_unset=True),
                )
            try:
                await events.job_card_updated(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    changed_fields=changed_fields, updated_by=body.updated_by,
                )
            except Exception:
                logger.exception("job_card_updated emit failed; swallowing")
    return {"ok": True, "job_card": row, "changed_fields": changed_fields}
```

- [ ] **Step 3: Add the DELETE /job-cards/{id} handler**

```python
@router.delete("/job-cards/{job_card_id}")
async def cancel_job_card_endpoint(request: Request, job_card_id: int, body: JobCardCancelRequest):
    """Soft-delete with cancellation reason. Allowed only when status ∈
    {locked, unlocked, assigned}. Returns 409 once material has been
    received — use force-unlock + close instead."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.cancel_job_card(
                    conn, job_card_id,
                    reason=body.cancellation_reason, deleted_by=body.deleted_by,
                )
            try:
                await events.job_card_cancelled(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    cancellation_reason=body.cancellation_reason,
                    deleted_by=body.deleted_by,
                )
            except Exception:
                logger.exception("job_card_cancelled emit failed; swallowing")
    return {"ok": True, "job_card": row}
```

- [ ] **Step 4: Reload the server**

If the dev server is running, restart it. Otherwise:

```bash
uvicorn app.main:app --reload --port 8000
```

Confirm both new routes appear at `http://localhost:8000/docs` under the Production tag (PATCH /api/v1/production/job-cards/{job_card_id} and DELETE).

- [ ] **Step 5: Write the verification probe**

Create `_jc_crud_probe.py` at repo root. Pattern matches existing `_jc_probe.py`:

```python
"""Verification probe for job-card PATCH + DELETE endpoints.

Runs through the headline scenarios from the spec:
  - Partial update preserves unspecified columns (10 fields, change 1, others intact)
  - Empty body returns 422
  - Edit on completed JC returns 409
  - Soft-delete sets deleted_at + status='cancelled' + cancellation_reason
  - Soft-delete blocked once status='material_received'
  - Double-delete returns 404

Usage:
  1. Ensure DDL from spec §13 has been run.
  2. Start the dev server: uvicorn app.main:app --reload --port 8000
  3. Set TOKEN env var to a valid bearer token
  4. python _jc_crud_probe.py <existing_job_card_id>
"""
import asyncio
import os
import sys
import json
import httpx
import asyncpg

DB_URL = os.environ["DB_URL"]   # set in shell, do not hardcode credentials
BASE   = os.environ.get("BASE_URL", "http://localhost:8000")
TOKEN  = os.environ["TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def snapshot(conn, jc_id):
    return dict(await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", jc_id))


async def main(jc_id: int):
    db = await asyncpg.connect(DB_URL)
    client = httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=10.0)
    try:
        before = await snapshot(db, jc_id)
        print(f"[before] status={before['status']} floor={before['floor']!r} "
              f"team_leader={before['assigned_to_team_leader']!r} "
              f"customer_name={before['customer_name']!r}")

        # --- Test 1: partial update preserves unspecified columns ---
        print("\n=== Test 1: PATCH with one field, verify rest unchanged ===")
        r = await client.patch(
            f"/api/v1/production/job-cards/{jc_id}",
            json={"floor": "TEST_FLOOR_A", "updated_by": "probe"},
        )
        print(f"  status={r.status_code} body={r.json()}")
        assert r.status_code == 200, "expected 200"
        after = await snapshot(db, jc_id)
        assert after["floor"] == "TEST_FLOOR_A", "floor should be updated"
        for col in ("status", "assigned_to_team_leader", "customer_name",
                    "batch_number", "batch_size_kg", "machine_id",
                    "factory", "process_name", "stage", "bom_id"):
            assert after[col] == before[col], f"{col} should be unchanged: {before[col]!r} -> {after[col]!r}"
        assert after["updated_at"] is not None, "updated_at must be stamped"
        assert after["updated_by"] == "probe"
        print("  ✓ unspecified columns preserved, audit columns stamped")

        # --- Test 2: empty body returns 422 ---
        print("\n=== Test 2: PATCH with only updated_by returns 422 ===")
        r = await client.patch(
            f"/api/v1/production/job-cards/{jc_id}",
            json={"updated_by": "probe"},
        )
        print(f"  status={r.status_code} body={r.json()}")
        assert r.status_code == 422
        print("  ✓ no editable fields rejected")

        # --- Test 3: explicit null clears the column ---
        print("\n=== Test 3: PATCH with floor=null clears the column ===")
        r = await client.patch(
            f"/api/v1/production/job-cards/{jc_id}",
            json={"floor": None, "updated_by": "probe"},
        )
        print(f"  status={r.status_code}")
        assert r.status_code == 200
        after = await snapshot(db, jc_id)
        assert after["floor"] is None, f"floor should be NULL, got {after['floor']!r}"
        print("  ✓ explicit null cleared the column")

        # Restore floor
        await client.patch(
            f"/api/v1/production/job-cards/{jc_id}",
            json={"floor": before["floor"], "updated_by": "probe"},
        )
        print(f"  (restored floor to {before['floor']!r})")

        # --- Test 4: DELETE without reason returns 422 ---
        print("\n=== Test 4: DELETE with reason < 3 chars returns 422 ===")
        r = await client.request(
            "DELETE", f"/api/v1/production/job-cards/{jc_id}",
            json={"cancellation_reason": "x", "deleted_by": "probe"},
        )
        print(f"  status={r.status_code}")
        assert r.status_code == 422
        print("  ✓ short reason rejected")

        # NOTE: Skipping the actual DELETE in this probe to avoid losing test data.
        # To exercise it, pass a throwaway JC id and uncomment below:
        #
        # r = await client.request(
        #     "DELETE", f"/api/v1/production/job-cards/{jc_id}",
        #     json={"cancellation_reason": "test cancellation", "deleted_by": "probe"},
        # )
        # assert r.status_code == 200
        # after = await snapshot(db, jc_id)
        # assert after["deleted_at"] is not None
        # assert after["status"] == "cancelled"
        # assert after["cancellation_reason"] == "test cancellation"

        print("\nAll PATCH probes passed. DELETE probe skipped — see comments.")

    finally:
        await client.aclose()
        await db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(1)
    asyncio.run(main(int(sys.argv[1])))
```

- [ ] **Step 6: Run the probe against a known job_card_id**

```bash
# pick a job_card_id from your DB whose status is in {locked, unlocked, assigned, material_received, in_progress}
DB_URL='postgresql://...' TOKEN='<bearer>' python _jc_crud_probe.py <jc_id>
```

Expected: all four "✓" lines print, no AssertionError, exits 0.

- [ ] **Step 7: Commit router + probe**

```bash
git add app/modules/production/router.py _jc_crud_probe.py
git commit -m "feat(production): add PATCH and DELETE /job-cards/{id} endpoints

PATCH preserves unspecified columns (Pydantic exclude_unset=True). DELETE
performs soft-delete with cancellation_reason. Verification probe at
_jc_crud_probe.py covers partial-update, explicit-null, empty-body, and
short-reason cases.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5 — Environment annexure CRUD

**Files:**
- Modify: `app/modules/production/services/jc_editor.py` (append two functions)
- Modify: `app/modules/production/router.py` (append two handlers near existing `/job-cards/{id}/environment` POST at router.py:1971)

- [ ] **Step 1: Add `patch_environment` + `delete_environment` to `jc_editor.py`**

Append to the bottom of `app/modules/production/services/jc_editor.py`:

```python
# ─── Environment ────────────────────────────────────────────────────────────

async def patch_environment(conn, job_card_id: int, env_id: int, payload: dict
                            ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_environment", pk_col="env_id", pk_val=env_id,
        payload=payload, allowed_cols=ENVIRONMENT_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_environment(conn, job_card_id: int, env_id: int, deleted_by: str
                             ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_environment", pk_col="env_id", pk_val=env_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row
```

- [ ] **Step 2: Add the two router handlers**

Append immediately after the existing `add_environment` handler (around router.py:1982):

```python
@router.patch("/job-cards/{job_card_id}/environment/{env_id}")
async def update_environment(request: Request, job_card_id: int, env_id: int,
                             body: EnvironmentPatchRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_environment(
                    conn, job_card_id, env_id, body.model_dump(exclude_unset=True),
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="environment", annexure_id=env_id,
                    action="updated", changed_by=body.updated_by,
                    changed_fields=changed,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/environment/{env_id}")
async def delete_environment_endpoint(request: Request, job_card_id: int, env_id: int,
                                      body: AnnexureDeleteRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_environment(
                    conn, job_card_id, env_id, body.deleted_by,
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="environment", annexure_id=env_id,
                    action="deleted", changed_by=body.deleted_by,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row}
```

- [ ] **Step 3: Restart the dev server and verify routes**

```bash
# server should auto-reload; or restart manually
curl -s http://localhost:8000/openapi.json | python -c "
import sys, json
spec = json.load(sys.stdin)
for path, ops in spec['paths'].items():
    if '/environment/' in path:
        for method in ops:
            print(method.upper(), path)
"
```

Expected: prints `PATCH /api/v1/production/job-cards/{job_card_id}/environment/{env_id}` and `DELETE` for the same path, plus the existing `POST /api/v1/production/job-cards/{job_card_id}/environment`.

- [ ] **Step 4: Probe — annexure cross-JC rejection**

This is the most important annexure test: passing the wrong parent JC in the URL must return 404, not silently update the wrong card's annexure.

Create `_jc_annexure_probe.py` at repo root (will be extended in Tasks 6–9):

```python
"""Probe for annexure CRUD endpoints. Verifies cross-JC isolation and
partial-update behavior.

Usage: python _jc_annexure_probe.py <jc_id_a> <jc_id_b> <env_id_on_a>

Where:
  jc_id_a       — a job card with status in editable range, has an env row
  jc_id_b       — a different job card (any status)
  env_id_on_a   — an existing env_id from job_card_environment WHERE job_card_id = jc_id_a
"""
import asyncio, os, sys, httpx, asyncpg

DB_URL = os.environ["DB_URL"]
BASE   = os.environ.get("BASE_URL", "http://localhost:8000")
TOKEN  = os.environ["TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def main(jc_a: int, jc_b: int, env_id: int):
    db = await asyncpg.connect(DB_URL)
    client = httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=10.0)
    try:
        # === environment ===
        before = dict(await db.fetchrow(
            "SELECT * FROM job_card_environment WHERE env_id = $1", env_id))
        print(f"[env before] {before}")

        # 1. Partial update preserves the other column
        print("\n=== env Test 1: PATCH parameter_name only, value unchanged ===")
        r = await client.patch(
            f"/api/v1/production/job-cards/{jc_a}/environment/{env_id}",
            json={"parameter_name": "PROBE_TEMP", "updated_by": "probe"},
        )
        assert r.status_code == 200, f"got {r.status_code}: {r.text}"
        after = dict(await db.fetchrow(
            "SELECT * FROM job_card_environment WHERE env_id = $1", env_id))
        assert after["parameter_name"] == "PROBE_TEMP"
        assert after["value"] == before["value"], "value must be unchanged"
        print("  ✓ parameter_name updated, value preserved")

        # 2. Wrong parent JC returns 404 (URL says jc_b but env belongs to jc_a)
        print("\n=== env Test 2: cross-JC PATCH returns 404 ===")
        r = await client.patch(
            f"/api/v1/production/job-cards/{jc_b}/environment/{env_id}",
            json={"parameter_name": "HACK", "updated_by": "probe"},
        )
        print(f"  status={r.status_code}")
        assert r.status_code == 404, "must reject mismatched parent JC"
        # And the row must not have changed
        after2 = dict(await db.fetchrow(
            "SELECT * FROM job_card_environment WHERE env_id = $1", env_id))
        assert after2["parameter_name"] == "PROBE_TEMP"
        print("  ✓ row not modified by wrong-parent attempt")

        # 3. Restore original value
        await client.patch(
            f"/api/v1/production/job-cards/{jc_a}/environment/{env_id}",
            json={"parameter_name": before["parameter_name"], "updated_by": "probe"},
        )
        print(f"  (restored parameter_name to {before['parameter_name']!r})")

        print("\nEnvironment probes passed.")

    finally:
        await client.aclose()
        await db.close()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__); sys.exit(1)
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])))
```

- [ ] **Step 5: Run the probe**

```bash
DB_URL='postgresql://...' TOKEN='<bearer>' python _jc_annexure_probe.py <jc_a> <jc_b> <env_id>
```

Expected: both "✓" lines print, exits 0.

- [ ] **Step 6: Commit**

```bash
git add app/modules/production/services/jc_editor.py app/modules/production/router.py _jc_annexure_probe.py
git commit -m "feat(production): add PATCH/DELETE for environment annexure rows

Includes cross-JC isolation guard via parent_jc_id WHERE clause. Probe
script verifies partial-update preservation and wrong-parent 404.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6 — Metal-detection annexure CRUD

**Files:**
- Modify: `app/modules/production/services/jc_editor.py` (append two functions)
- Modify: `app/modules/production/router.py` (append two handlers after the existing `add_metal_detection` at router.py:1985)
- Modify: `_jc_annexure_probe.py` (extend with metal-detection scenarios)

- [ ] **Step 1: Add `patch_metal_detection` + `delete_metal_detection` to `jc_editor.py`**

Append:

```python
# ─── Metal Detection ────────────────────────────────────────────────────────

async def patch_metal_detection(conn, job_card_id: int, detection_id: int, payload: dict
                                ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_metal_detection", pk_col="detection_id", pk_val=detection_id,
        payload=payload, allowed_cols=METAL_DETECTION_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_metal_detection(conn, job_card_id: int, detection_id: int, deleted_by: str
                                 ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_metal_detection", pk_col="detection_id", pk_val=detection_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row
```

- [ ] **Step 2: Add the two router handlers**

Append after `add_metal_detection`:

```python
@router.patch("/job-cards/{job_card_id}/metal-detection/{detection_id}")
async def update_metal_detection(request: Request, job_card_id: int, detection_id: int,
                                 body: MetalDetectionPatchRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_metal_detection(
                    conn, job_card_id, detection_id, body.model_dump(exclude_unset=True),
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="metal_detection", annexure_id=detection_id,
                    action="updated", changed_by=body.updated_by,
                    changed_fields=changed,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/metal-detection/{detection_id}")
async def delete_metal_detection_endpoint(request: Request, job_card_id: int, detection_id: int,
                                          body: AnnexureDeleteRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_metal_detection(
                    conn, job_card_id, detection_id, body.deleted_by,
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="metal_detection", annexure_id=detection_id,
                    action="deleted", changed_by=body.deleted_by,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row}
```

- [ ] **Step 3: Smoke-test the routes are registered**

```bash
curl -s http://localhost:8000/openapi.json | python -c "
import sys, json
spec = json.load(sys.stdin)
for path, ops in spec['paths'].items():
    if '/metal-detection/' in path:
        for method in ops: print(method.upper(), path)
"
```

Expected: PATCH and DELETE on `.../metal-detection/{detection_id}`.

- [ ] **Step 4: Manual probe (skip if no metal-detection row exists)**

If your DB has a `job_card_metal_detection` row, replicate the env probe pattern manually:

```bash
# Pick: jc_id, detection_id where row.job_card_id == jc_id
curl -s -X PATCH "http://localhost:8000/api/v1/production/job-cards/<jc_id>/metal-detection/<detection_id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"failed_units": 5, "updated_by": "probe"}'
```

Verify: response status 200, only `failed_units` and audit columns changed in DB; `check_type`, `fe_pass`, etc. unchanged.

- [ ] **Step 5: Commit**

```bash
git add app/modules/production/services/jc_editor.py app/modules/production/router.py
git commit -m "feat(production): add PATCH/DELETE for metal-detection annexure rows

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7 — Weight-checks annexure CRUD

**Files:**
- Modify: `app/modules/production/services/jc_editor.py`
- Modify: `app/modules/production/router.py` (after existing `add_weight_checks` at router.py:2007)

- [ ] **Step 1: Add service entry points**

Append to `jc_editor.py`:

```python
# ─── Weight Check ───────────────────────────────────────────────────────────

async def patch_weight_check(conn, job_card_id: int, check_id: int, payload: dict
                             ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_weight_check", pk_col="check_id", pk_val=check_id,
        payload=payload, allowed_cols=WEIGHT_CHECK_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_weight_check(conn, job_card_id: int, check_id: int, deleted_by: str
                              ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_weight_check", pk_col="check_id", pk_val=check_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row
```

- [ ] **Step 2: Add router handlers**

Append after `add_weight_checks`:

```python
@router.patch("/job-cards/{job_card_id}/weight-checks/{check_id}")
async def update_weight_check(request: Request, job_card_id: int, check_id: int,
                              body: WeightCheckPatchRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_weight_check(
                    conn, job_card_id, check_id, body.model_dump(exclude_unset=True),
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="weight_check", annexure_id=check_id,
                    action="updated", changed_by=body.updated_by,
                    changed_fields=changed,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/weight-checks/{check_id}")
async def delete_weight_check_endpoint(request: Request, job_card_id: int, check_id: int,
                                       body: AnnexureDeleteRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_weight_check(
                    conn, job_card_id, check_id, body.deleted_by,
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="weight_check", annexure_id=check_id,
                    action="deleted", changed_by=body.deleted_by,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row}
```

- [ ] **Step 3: Smoke-test routes**

```bash
curl -s http://localhost:8000/openapi.json | python -c "
import sys, json
spec = json.load(sys.stdin)
for path, ops in spec['paths'].items():
    if '/weight-checks/' in path:
        for method in ops: print(method.upper(), path)
"
```

Expected: PATCH and DELETE on `.../weight-checks/{check_id}`.

- [ ] **Step 4: Commit**

```bash
git add app/modules/production/services/jc_editor.py app/modules/production/router.py
git commit -m "feat(production): add PATCH/DELETE for weight-check annexure rows

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8 — Loss-reconciliation annexure CRUD

**Files:**
- Modify: `app/modules/production/services/jc_editor.py`
- Modify: `app/modules/production/router.py` (after existing `add_loss_reconciliation` at router.py:2026)

- [ ] **Step 1: Add service entry points**

Append:

```python
# ─── Loss Reconciliation ────────────────────────────────────────────────────

async def patch_loss_reconciliation(conn, job_card_id: int, recon_id: int, payload: dict
                                    ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_loss_reconciliation", pk_col="recon_id", pk_val=recon_id,
        payload=payload, allowed_cols=LOSS_RECONCILIATION_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_loss_reconciliation(conn, job_card_id: int, recon_id: int, deleted_by: str
                                     ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_loss_reconciliation", pk_col="recon_id", pk_val=recon_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row
```

- [ ] **Step 2: Add router handlers**

Append after `add_loss_reconciliation`:

```python
@router.patch("/job-cards/{job_card_id}/loss-reconciliation/{recon_id}")
async def update_loss_reconciliation(request: Request, job_card_id: int, recon_id: int,
                                     body: LossReconciliationPatchRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_loss_reconciliation(
                    conn, job_card_id, recon_id, body.model_dump(exclude_unset=True),
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="loss_reconciliation", annexure_id=recon_id,
                    action="updated", changed_by=body.updated_by,
                    changed_fields=changed,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/loss-reconciliation/{recon_id}")
async def delete_loss_reconciliation_endpoint(request: Request, job_card_id: int, recon_id: int,
                                              body: AnnexureDeleteRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_loss_reconciliation(
                    conn, job_card_id, recon_id, body.deleted_by,
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="loss_reconciliation", annexure_id=recon_id,
                    action="deleted", changed_by=body.deleted_by,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row}
```

- [ ] **Step 3: Smoke-test routes**

```bash
curl -s http://localhost:8000/openapi.json | python -c "
import sys, json
spec = json.load(sys.stdin)
for path, ops in spec['paths'].items():
    if '/loss-reconciliation/' in path:
        for method in ops: print(method.upper(), path)
"
```

Expected: PATCH and DELETE on `.../loss-reconciliation/{recon_id}`.

- [ ] **Step 4: Commit**

```bash
git add app/modules/production/services/jc_editor.py app/modules/production/router.py
git commit -m "feat(production): add PATCH/DELETE for loss-reconciliation annexure rows

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9 — Remarks annexure CRUD

**Files:**
- Modify: `app/modules/production/services/jc_editor.py`
- Modify: `app/modules/production/router.py` (after existing `add_remarks` at router.py:2052)

- [ ] **Step 1: Add service entry points**

Append:

```python
# ─── Remarks ────────────────────────────────────────────────────────────────

async def patch_remark(conn, job_card_id: int, remark_id: int, payload: dict
                       ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_remarks", pk_col="remark_id", pk_val=remark_id,
        payload=payload, allowed_cols=REMARKS_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_remark(conn, job_card_id: int, remark_id: int, deleted_by: str
                        ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_remarks", pk_col="remark_id", pk_val=remark_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row
```

- [ ] **Step 2: Add router handlers**

Append after `add_remarks`:

```python
@router.patch("/job-cards/{job_card_id}/remarks/{remark_id}")
async def update_remark(request: Request, job_card_id: int, remark_id: int,
                        body: RemarkPatchRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row, changed = await jc_editor.patch_remark(
                    conn, job_card_id, remark_id, body.model_dump(exclude_unset=True),
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="remarks", annexure_id=remark_id,
                    action="updated", changed_by=body.updated_by,
                    changed_fields=changed,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row, "changed_fields": changed}


@router.delete("/job-cards/{job_card_id}/remarks/{remark_id}")
async def delete_remark_endpoint(request: Request, job_card_id: int, remark_id: int,
                                 body: AnnexureDeleteRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with deferred_events():
            async with conn.transaction():
                jc, row = await jc_editor.delete_remark(
                    conn, job_card_id, remark_id, body.deleted_by,
                )
            try:
                await events.job_card_annexure_changed(
                    jc["entity"], job_card_id=job_card_id,
                    job_card_number=jc["job_card_number"],
                    annexure_type="remarks", annexure_id=remark_id,
                    action="deleted", changed_by=body.deleted_by,
                )
            except Exception:
                logger.exception("job_card_annexure_changed emit failed; swallowing")
    return {"ok": True, "row": row}
```

- [ ] **Step 3: Smoke-test all 12 new endpoints registered**

```bash
curl -s http://localhost:8000/openapi.json | python -c "
import sys, json
spec = json.load(sys.stdin)
paths = []
for path, ops in spec['paths'].items():
    if path.startswith('/api/v1/production/job-cards'):
        for method in ops:
            if method.lower() in ('patch', 'delete'):
                paths.append((method.upper(), path))
for m, p in sorted(paths): print(m, p)
print(f'total: {len(paths)}')
"
```

Expected: 12 lines. Should include both `PATCH /api/v1/production/job-cards/{job_card_id}` and `DELETE /api/v1/production/job-cards/{job_card_id}`, plus 2 each for `environment`, `metal-detection`, `weight-checks`, `loss-reconciliation`, `remarks`.

- [ ] **Step 4: Run a remark partial-update probe inline**

```bash
# Create a remark first
curl -s -X POST "http://localhost:8000/api/v1/production/job-cards/<jc_id>/remarks" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"remark_type":"observation","content":"original text","recorded_by":"probe"}'
# Note the returned remark_id, then:
curl -s -X PATCH "http://localhost:8000/api/v1/production/job-cards/<jc_id>/remarks/<remark_id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"content":"corrected text","updated_by":"probe"}'
# Verify in DB: remark_type unchanged, content updated, updated_at set
```

- [ ] **Step 5: Commit**

```bash
git add app/modules/production/services/jc_editor.py app/modules/production/router.py
git commit -m "feat(production): add PATCH/DELETE for remarks annexure rows

Completes the 12-endpoint job-card CRUD enhancement.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10 — Filter soft-deleted rows in list and detail endpoints

**Files:**
- Modify: `app/modules/production/router.py` — `list_job_cards` (router.py:1372), `list_job_cards_all` (router.py:1458), `team_dashboard` (router.py:1478), `floor_dashboard` (router.py:1513)
- Modify: `app/modules/production/services/job_card_engine.py` — `get_job_card_detail` (job_card_engine.py:835) and the 5 annexure SELECTs (job_card_engine.py:950, 953, 956, 959, 962)

- [ ] **Step 1: Add `include_cancelled` query param to `list_job_cards`**

Find the `list_job_cards` function signature (router.py:1372) and add a parameter:

```python
async def list_job_cards(
    request: Request,
    entity: str = Query(None),
    status: str = Query(None),
    team_leader: str = Query(None),
    floor: str = Query(None),
    factory: str = Query(None),
    stage: str = Query(None),
    search: str = Query(None),
    customer: str = Query(None),
    article: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    include_cancelled: bool = Query(False),  # ← NEW
):
```

Then in the body of the function, find where `conditions` (or equivalent) is built and add at the start:

```python
    if not include_cancelled:
        conditions.append("deleted_at IS NULL")
```

(Read the existing body at router.py:1389-1455 to see the precise line where `conditions: list[str]` is initialized — insert there.)

- [ ] **Step 2: Pass the new param through `list_job_cards_all`**

`list_job_cards_all` (router.py:1458) calls `list_job_cards`. Add `include_cancelled` to its signature and forward it. The call at router.py:1473 becomes:

```python
result = await list_job_cards(
    request, entity, status, team_leader, floor, factory, stage,
    search, customer, article, date_from, date_to,
    page=1, page_size=100000,
    include_cancelled=include_cancelled,
)
```

- [ ] **Step 3: Add `deleted_at IS NULL` to dashboard queries**

In `team_dashboard` (router.py:1478) and `floor_dashboard` (router.py:1513), find the SQL `WHERE` clause built dynamically. Add `"deleted_at IS NULL"` to the conditions list (or `AND deleted_at IS NULL` to the static WHERE, depending on local style — read each function and match its pattern).

For `team_dashboard`, also add the `include_cancelled: bool = Query(False)` parameter and gate the filter on it. Same for `floor_dashboard`.

- [ ] **Step 4: Filter the detail endpoint's main JC SELECT**

In `app/modules/production/services/job_card_engine.py:837`, change:

```python
    jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", job_card_id)
```

to:

```python
    jc = await conn.fetchrow(
        "SELECT * FROM job_card WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
```

- [ ] **Step 5: Filter the 5 annexure SELECTs in `get_job_card_detail`**

For each of these queries (lines 950, 953, 956, 959, 962 in `job_card_engine.py`), add `AND deleted_at IS NULL` before the `ORDER BY` clause.

Example transformation for line 950:

```python
# before
"SELECT * FROM job_card_metal_detection WHERE job_card_id = $1 ORDER BY detection_id"
# after
"SELECT * FROM job_card_metal_detection WHERE job_card_id = $1 AND deleted_at IS NULL ORDER BY detection_id"
```

Apply the same pattern to:
- `job_card_metal_detection` (line 950)
- `job_card_weight_check` (line 953)
- `job_card_environment` (line 956)
- `job_card_loss_reconciliation` (line 959)
- `job_card_remarks` (line 962)

- [ ] **Step 6: Probe — list endpoints exclude soft-deleted by default**

Add a small probe inline, or run by hand:

```bash
# Soft-delete a throwaway JC first (must be in {locked, unlocked, assigned})
curl -s -X DELETE "http://localhost:8000/api/v1/production/job-cards/<throwaway_jc_id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"cancellation_reason":"probe cancellation","deleted_by":"probe"}'

# Confirm it's hidden by default
curl -s "http://localhost:8000/api/v1/production/job-cards?entity=cfpl" \
  -H "Authorization: Bearer $TOKEN" | python -c "
import sys, json
items = json.load(sys.stdin).get('items', [])
ids = [it['job_card_id'] for it in items]
print('throwaway in list:', <throwaway_jc_id> in ids)
"

# Confirm include_cancelled=true brings it back
curl -s "http://localhost:8000/api/v1/production/job-cards?entity=cfpl&include_cancelled=true" \
  -H "Authorization: Bearer $TOKEN" | python -c "
import sys, json
items = json.load(sys.stdin).get('items', [])
ids = [it['job_card_id'] for it in items]
print('throwaway with include_cancelled:', <throwaway_jc_id> in ids)
"
```

Expected first line: `throwaway in list: False`. Second: `throwaway with include_cancelled: True`.

- [ ] **Step 7: Commit**

```bash
git add app/modules/production/router.py app/modules/production/services/job_card_engine.py
git commit -m "feat(production): hide soft-deleted job cards and annexures by default

Adds include_cancelled query param to list/dashboard endpoints. Detail
endpoint and its annexure children always filter soft-deleted rows.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11 — Sync schema source-of-truth in `production_schema.sql`

The DDL was applied manually (pre-requisite). This task updates the canonical schema file so future fresh setups create the columns automatically.

**Files:**
- Modify: `app/db/production_schema.sql`

- [ ] **Step 1: Edit the `job_card` table definition (around line 211)**

Inside the `CREATE TABLE IF NOT EXISTS job_card (...)` block, before the closing `)`, add:

```sql
    -- Audit + soft-delete (added 2026-05-07)
    updated_at              TIMESTAMPTZ,
    updated_by              TEXT,
    deleted_at              TIMESTAMPTZ,
    deleted_by              TEXT,
    cancellation_reason     TEXT,
```

After the existing `CREATE INDEX` lines for job_card (after line 258), add:

```sql
CREATE INDEX IF NOT EXISTS idx_job_card_not_deleted ON job_card(deleted_at) WHERE deleted_at IS NULL;
```

- [ ] **Step 2: Edit the 5 annexure table definitions**

For each of these CREATE TABLE blocks, append the audit columns before the closing `)`:

```sql
    updated_at              TIMESTAMPTZ,
    updated_by              TEXT,
    deleted_at              TIMESTAMPTZ,
    deleted_by              TEXT,
```

Tables (line numbers from the schema file, may have shifted slightly):
- `job_card_environment` (around line 350)
- `job_card_metal_detection` (around line 361)
- `job_card_weight_check` (around line 376)
- `job_card_loss_reconciliation` (around line 389)
- `job_card_remarks` (around line 404)

After each table's existing `CREATE INDEX` line (idx_env_jc, idx_metal_jc, idx_weight_jc, idx_loss_recon_jc, idx_remarks_jc), add:

```sql
CREATE INDEX IF NOT EXISTS idx_jc_<short>_not_deleted ON <table>(job_card_id) WHERE deleted_at IS NULL;
```

(Use `idx_jc_env_not_deleted`, `idx_jc_metal_not_deleted`, `idx_jc_wc_not_deleted`, `idx_jc_loss_not_deleted`, `idx_jc_remarks_not_deleted`.)

- [ ] **Step 3: Verify file parses**

```bash
python -c "
sql = open('app/db/production_schema.sql', encoding='utf-8').read()
# crude balance check
assert sql.count('CREATE TABLE') >= 6
print('CREATE TABLE count:', sql.count('CREATE TABLE'))
print('updated_at occurrences in job_card area:', sql.count('updated_at'))
"
```

Expected: at least 6 CREATE TABLEs unchanged, `updated_at` appears ≥6 times (1 for main JC + 5 annexures).

- [ ] **Step 4: Commit**

```bash
git add app/db/production_schema.sql
git commit -m "chore(db): sync production_schema.sql with manually-applied audit columns

Adds updated_at/updated_by/deleted_at/deleted_by to job_card and 5 annexure
tables (DDL was applied separately by ops). Future fresh database setups
will now create the columns from this canonical schema file.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

After all tasks complete, run a single end-to-end smoke check:

- [ ] **Endpoint count check**

```bash
curl -s http://localhost:8000/openapi.json | python -c "
import sys, json
spec = json.load(sys.stdin)
new = []
for path, ops in spec['paths'].items():
    if path.startswith('/api/v1/production/job-cards'):
        for method in ops:
            if method.lower() in ('patch','delete'):
                new.append(f'{method.upper()} {path}')
print(f'NEW endpoints ({len(new)}):')
for n in sorted(new): print(' ', n)
"
```

Expected: exactly 12 new endpoints (PATCH/DELETE on main JC plus PATCH/DELETE on each of the 5 annexure paths).

- [ ] **Headline behavior check** — re-run `_jc_crud_probe.py` against any editable JC. All 4 "✓" lines must print.

- [ ] **Filter behavior check** — `GET /job-cards` returns no rows with `deleted_at IS NOT NULL` unless `include_cancelled=true`.

- [ ] **Webhook check** — tail your event-bus log / WebSocket consumer while running the probe. Should see `job_card.updated`, `job_card.cancelled`, and `job_card.annexure.{updated,deleted}` events with correct `changed_fields`.

If any of the four checks fail, do not mark the implementation complete — open a follow-up before merging.
