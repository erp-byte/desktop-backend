# Job Card CRUD Enhancement — Design

**Date:** 2026-05-07
**Module:** `app/modules/production`
**Status:** Approved

## 1. Background

The current job-card API (21 endpoints, all under `/api/v1/production`) covers list, detail, lifecycle (start/complete/close), assignment, output, and append-only annexures. The Android client documented in `docs/JobCard-Endpoints.pdf` consumes all 21.

**Two gaps:**

1. **No partial-update endpoint** for the job_card record. Operators cannot fix a typo in `customer_name`, reassign a `machine_id`, or move a card to a different `floor` without going through specific lifecycle endpoints (`assign` only covers team).
2. **No delete / cancel endpoint.** Job cards created in error stay in the system forever.
3. **Annexure rows are append-only.** A mistyped weight-check sample, a wrong metal-detection check_type, or an outdated remark can only be "corrected" by appending another row with a corrective note.

This spec adds **12 new endpoints** (1 main PATCH + 1 main DELETE + 5 annexure PATCH + 5 annexure DELETE) with strict partial-update semantics: only fields the client supplies are written, all other columns stay intact.

## 2. Scope

### In scope
- New `PATCH /job-cards/{id}` for editable header fields
- New `DELETE /job-cards/{id}` (soft delete with cancellation reason)
- New `PATCH` + `DELETE` for each of the 5 annexure tables
- Audit columns (`updated_at`, `updated_by`, `deleted_at`, `deleted_by`) on all 6 tables
- Webhook events for every PATCH/DELETE
- New service module `services/jc_editor.py` separating "fix-up" semantics from `job_card_engine.py` (which keeps owning lifecycle)
- Soft-delete filter on existing list endpoints (`/job-cards`, `/job-cards/all`, `/job-cards/team-dashboard`, `/job-cards/floor-dashboard`) plus opt-in `include_cancelled=true`

### Out of scope
- Concurrency control (optimistic locking via `version` or `If-Match`) — last-write-wins is acceptable for the volume of edits expected
- Dedicated audit-log table — webhook events + stamped rows are the audit trail
- Repurposing or deprecating any existing endpoint
- Backfilling `updated_at` for existing rows — they stay NULL until first edit
- Field-level permission checks (e.g., "operator cannot edit `customer_name`") — out of scope for v1

## 3. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| **PATCH scope** | Editable header only: `machine_id`, `assigned_to_team_leader`, `team_members`, `factory`, `floor`, `customer_name`, `batch_number`, `batch_size_kg`, `bom_id`, `process_name`, `stage` | Excludes lifecycle status (managed by start/complete/close), identity (`job_card_number`, `prod_order_id`), and chain pointers (system-managed). Keeps state-machine invariants intact. |
| **DELETE model** | Soft delete: row stays, `deleted_at` + `deleted_by` + `cancellation_reason` set, `status='cancelled'`. Filtered from list endpoints by default. | Production traceability requires audit retention. Hard delete loses the why. |
| **Annexure CRUD** | Full PATCH + DELETE for all 5 types | Operators need to correct quality / measurement entries before sign-off. Append-only-with-corrective-note is too clumsy for routine typo fixes. |
| **PATCH lifecycle gate** | `status ∈ {locked, unlocked, assigned, material_received, in_progress}` | Once `completed`/`closed`/`cancelled`, the card is immutable. |
| **DELETE lifecycle gate** | `status ∈ {locked, unlocked, assigned}` | Once material has been received the card has consumed inventory; cancellation has to go through `force-unlock` + `close` for a proper paper trail. |
| **Audit** | `updated_at`/`updated_by` columns on the row + webhook event with `changed_fields` list | Consistent with the existing event-emitting pattern (every lifecycle endpoint already emits). No separate audit table. |
| **Code structure** | New file `app/modules/production/services/jc_editor.py` with private `_apply_partial_update` helper | Separation: `job_card_engine.py` owns lifecycle, `jc_editor.py` owns edits. Mirrors the existing per-concern split (`qc_service`, `floor_tracker`, `discrepancy_manager`, etc.). |

## 4. Endpoint catalog

All 12 endpoints under `/api/v1/production`. Response shape: `{"ok": true, ...row}` or the standard FastAPI error envelope on failure.

| # | Method | Path | Purpose |
|---|---|---|---|
| 1 | `PATCH`  | `/job-cards/{id}` | Partial update of editable header fields |
| 2 | `DELETE` | `/job-cards/{id}` | Soft-cancel with reason |
| 3 | `PATCH`  | `/job-cards/{id}/environment/{env_id}` | Edit single env reading |
| 4 | `DELETE` | `/job-cards/{id}/environment/{env_id}` | Remove env reading |
| 5 | `PATCH`  | `/job-cards/{id}/metal-detection/{detection_id}` | Edit metal-detection check |
| 6 | `DELETE` | `/job-cards/{id}/metal-detection/{detection_id}` | Remove check |
| 7 | `PATCH`  | `/job-cards/{id}/weight-checks/{check_id}` | Edit weight-check sample |
| 8 | `DELETE` | `/job-cards/{id}/weight-checks/{check_id}` | Remove sample |
| 9 | `PATCH`  | `/job-cards/{id}/loss-reconciliation/{recon_id}` | Edit loss row |
| 10 | `DELETE` | `/job-cards/{id}/loss-reconciliation/{recon_id}` | Remove loss row |
| 11 | `PATCH`  | `/job-cards/{id}/remarks/{remark_id}` | Edit remark |
| 12 | `DELETE` | `/job-cards/{id}/remarks/{remark_id}` | Remove remark |

**Annexure URL contract:** the path includes both `{id}` (parent job card) and the annexure's natural primary key. The lookup query enforces both — passing a mismatched pair returns 404.

## 5. Pydantic request schemas

New file: `app/modules/production/schemas/job_card_edit.py`.

```python
from pydantic import BaseModel, Field
from typing import Optional, List


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
    updated_by:              str                                  # required


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

**Partial-update guarantee:** routers call `body.model_dump(exclude_unset=True)` — only fields the client actually sent appear in the dict. This is what enforces the "10 fields filled, 1 updated, others intact" requirement: any column the client omitted is never referenced in the generated SQL `UPDATE` statement, so the database keeps its existing value untouched.

**Explicit null:** sending `{"floor": null}` writes `NULL` to the column (intentional clear). Sending `{}` returns `422 No editable fields supplied`.

## 6. Service layer — `app/modules/production/services/jc_editor.py`

```python
"""Partial-update + soft-delete logic for job cards and their annexures.

Owns 'fix-up' semantics — distinct from job_card_engine.py which owns
lifecycle transitions (start, complete, sign-off, etc.).
"""

from typing import Any, Mapping
from fastapi import HTTPException
import asyncpg


# Per-table allow-lists. Keys not in this set are silently dropped before
# we build the SQL — defends against client-supplied junk fields and also
# prevents updates to identity / system-managed columns.
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

    set_parts, params = [], []
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
    set_parts = ["deleted_at = NOW()", f"deleted_by = $1"]
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


# Main job card --------------------------------------------------------------

async def patch_job_card(conn, job_card_id: int, payload: dict) -> tuple[dict, list[str]]:
    jc = await conn.fetchrow(
        "SELECT status, entity, job_card_number FROM job_card "
        "WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
    if jc is None:
        raise HTTPException(404, "Job card not found")
    if jc["status"] not in EDITABLE_STATUSES:
        raise HTTPException(409, f"Job card status '{jc['status']}' is not editable")

    updated_by = payload.pop("updated_by")
    return await _apply_partial_update(
        conn, table="job_card", pk_col="job_card_id", pk_val=job_card_id,
        payload=payload, allowed_cols=JOB_CARD_EDITABLE_COLS, updated_by=updated_by,
    )


async def cancel_job_card(conn, job_card_id: int, *, reason: str, deleted_by: str) -> dict:
    jc = await conn.fetchrow(
        "SELECT status, entity, job_card_number FROM job_card "
        "WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
    if jc is None:
        raise HTTPException(404, "Job card not found")
    if jc["status"] not in CANCELLABLE_STATUSES:
        raise HTTPException(409, f"Cannot cancel — status '{jc['status']}'. Use force-unlock + close instead.")
    return await _apply_soft_delete(
        conn, table="job_card", pk_col="job_card_id", pk_val=job_card_id,
        deleted_by=deleted_by, reason=reason,
    )


# Annexure entry points ------------------------------------------------------
# All 5 follow this exact shape; only the table, pk_col, allow-list change.

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


async def patch_environment(conn, job_card_id, env_id, payload):
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_environment", pk_col="env_id", pk_val=env_id,
        payload=payload, allowed_cols=ENVIRONMENT_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_environment(conn, job_card_id, env_id, deleted_by):
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_environment", pk_col="env_id", pk_val=env_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row


# patch_metal_detection / delete_metal_detection
# patch_weight_check    / delete_weight_check
# patch_loss_reconciliation / delete_loss_reconciliation
# patch_remarks         / delete_remarks
# All follow the same template — table, pk_col, allow-list differ.
```

**SQL injection note:** column names interpolated into the SQL string come exclusively from frozen `allowed_cols` sets — never from the client. Values always go through `$N` parameters. Placeholder indices are computed from `len(params) + 1` after each append, so adding/removing optional clauses (audit stamps, parent-JC enforcement) doesn't shift the offsets.

## 7. Lifecycle gating reference

| Action | Allowed when status ∈ | Returns on violation |
|---|---|---|
| `PATCH /job-cards/{id}` | `locked`, `unlocked`, `assigned`, `material_received`, `in_progress` | `409 — Job card status 'X' is not editable` |
| `DELETE /job-cards/{id}` | `locked`, `unlocked`, `assigned` | `409 — Cannot cancel; use force-unlock + close` |
| `PATCH /job-cards/{id}/<annexure>/{row_id}` | parent JC same as PATCH gate above | `409` |
| `DELETE /job-cards/{id}/<annexure>/{row_id}` | parent JC same as PATCH gate above | `409` |

**Row-level guards (404):**
- Job card row must exist and have `deleted_at IS NULL`.
- Annexure row must exist, belong to the named `job_card_id` (URL match enforced via `AND job_card_id = $N`), and have `deleted_at IS NULL`.

**Idempotency:**
- `DELETE` on already-soft-deleted row → `404`.
- `PATCH` with empty body or all non-editable fields → `422`.

## 8. Webhook events

Append to `app/webhooks/events.py`:

```python
async def job_card_updated(entity: str, *, job_card_id: int, job_card_number: str,
                           changed_fields: list[str], updated_by: str) -> None:
    await event_bus.publish(Event(
        event_type="job_card.updated",
        entity=_validate_entity(entity, "job_card.updated"),
        target_roles=["admin", "production_manager", "supervisor"],
        payload={"job_card_id": job_card_id, "job_card_number": job_card_number,
                 "changed_fields": changed_fields, "updated_by": updated_by},
    ))


async def job_card_cancelled(entity: str, *, job_card_id: int, job_card_number: str,
                             cancellation_reason: str, deleted_by: str) -> None:
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

**Router emission pattern** (matches existing endpoints):

```python
@router.patch("/job-cards/{job_card_id}")
async def update_jc(request: Request, job_card_id: int, body: JobCardPatchRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        jc = await conn.fetchrow(
            "SELECT entity, job_card_number FROM job_card WHERE job_card_id = $1 AND deleted_at IS NULL",
            job_card_id,
        )
        if jc is None:
            raise HTTPException(404, "Job card not found")
        async with deferred_events():
            async with conn.transaction():
                row, changed_fields = await jc_editor.patch_job_card(
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

## 9. Error response matrix

| Scenario | HTTP | Body |
|---|---|---|
| JC / annexure not found, or already soft-deleted | `404` | `{"detail": "Job card not found"}` / `"<table> row not found or already deleted"` |
| Annexure row exists but belongs to a different JC | `404` | (same — don't leak existence) |
| Status not editable for PATCH | `409` | `{"detail": "Job card status 'completed' is not editable"}` |
| Status not cancellable for DELETE | `409` | `{"detail": "Cannot cancel — status 'in_progress'. Use force-unlock + close instead."}` |
| Empty PATCH body / no editable fields | `422` | `{"detail": "No editable fields supplied"}` |
| `cancellation_reason` < 3 chars | `422` | (Pydantic-generated) |
| `batch_size_kg ≤ 0`, `failed_units < 0`, etc. | `422` | (Pydantic-generated) |
| Unauthenticated | `401` | (existing middleware) |

## 10. List-endpoint filter changes

`GET /job-cards`, `/job-cards/all`, `/job-cards/team-dashboard`, `/job-cards/floor-dashboard` all gain an implicit `WHERE deleted_at IS NULL` clause. Add a new query param to each:

```python
include_cancelled: bool = Query(False)
```

When `true`, the filter is dropped — soft-deleted cards appear in the result set. Same for `GET /job-cards/{id}/output` (filters loss-reconciliation and other annexure rows).

## 11. Testing approach

**Service-layer unit tests** (asyncpg fixture, no HTTP):
- `test_patch_preserves_unspecified_columns` — set 5 fields, PATCH 1, verify other 4 unchanged. **Headline test for the partial-update requirement.**
- `test_patch_rejects_immutable_columns` — supply `job_card_number` or `prod_order_id`, verify silently dropped.
- `test_patch_empty_body_returns_422`.
- `test_patch_blocked_after_completion` — set status `completed`, expect 409.
- `test_explicit_null_clears_field` — `{"floor": null}` writes NULL.
- `test_soft_delete_marks_row` — verify all 4 audit columns + status='cancelled'.
- `test_soft_delete_blocked_after_material_received`.
- `test_double_delete_returns_404`.
- `test_annexure_patch_filters_by_parent_jc` — create 2 JCs, try to PATCH JC-A's env_id while passing JC-B in URL → 404.
- One per annexure type × (PATCH happy + PATCH wrong-parent + DELETE happy + DELETE already-deleted) = 20 tests.

**Router integration tests** (FastAPI TestClient, real DB):
- One round-trip per endpoint (12 tests) confirming wiring + status codes + response shape.
- Webhook emission test per family (PATCH, DELETE, annexure-PATCH, annexure-DELETE) using existing event-bus capture fixture.

**Filter regression tests:**
- `GET /job-cards` no longer returns soft-deleted cards (new behavior — must lock down).
- `GET /job-cards?include_cancelled=true` includes them.

## 12. Files to create / modify

**New files:**
- `app/modules/production/services/jc_editor.py` (service)
- `app/modules/production/schemas/job_card_edit.py` (Pydantic models)
- `tests/test_job_card_crud.py` (or wherever existing production tests live — match the pattern at write time)

**Modified files:**
- `app/modules/production/router.py` — add 12 endpoint handlers
- `app/webhooks/events.py` — add 3 event functions
- `app/db/production_schema.sql` — update column definitions for source-of-truth (DDL is run manually, see §13)

## 13. Database queries to run manually

Run these against the target PostgreSQL database before deploying. Idempotent — safe to re-run.

```sql
-- ==========================================================================
-- Job Card CRUD migration (2026-05-07)
-- Adds soft-delete + audit columns to job_card and 5 annexure tables
-- ==========================================================================

-- 1. Main job_card table: add audit + cancellation columns
ALTER TABLE job_card
    ADD COLUMN IF NOT EXISTS updated_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_by          TEXT,
    ADD COLUMN IF NOT EXISTS deleted_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by          TEXT,
    ADD COLUMN IF NOT EXISTS cancellation_reason TEXT;

-- 2. Annexure: environment
ALTER TABLE job_card_environment
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_by TEXT,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT;

-- 3. Annexure: metal detection
ALTER TABLE job_card_metal_detection
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_by TEXT,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT;

-- 4. Annexure: weight checks
ALTER TABLE job_card_weight_check
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_by TEXT,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT;

-- 5. Annexure: loss reconciliation
ALTER TABLE job_card_loss_reconciliation
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_by TEXT,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT;

-- 6. Annexure: remarks
ALTER TABLE job_card_remarks
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_by TEXT,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT;

-- 7. Filter index — make list endpoints fast when hiding soft-deleted rows
CREATE INDEX IF NOT EXISTS idx_job_card_not_deleted
    ON job_card(deleted_at) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_jc_env_not_deleted
    ON job_card_environment(job_card_id) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_jc_metal_not_deleted
    ON job_card_metal_detection(job_card_id) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_jc_wc_not_deleted
    ON job_card_weight_check(job_card_id) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_jc_loss_not_deleted
    ON job_card_loss_reconciliation(job_card_id) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_jc_remarks_not_deleted
    ON job_card_remarks(job_card_id) WHERE deleted_at IS NULL;

-- 8. Verify (optional)
-- \d+ job_card
-- SELECT column_name FROM information_schema.columns
--  WHERE table_name = 'job_card' AND column_name IN
--    ('updated_at','updated_by','deleted_at','deleted_by','cancellation_reason');
```

**Note on `status='cancelled'`:** the `status` column is already `TEXT` (not an enum), so no DDL is required to allow the new value — the application code writes `'cancelled'` directly.
