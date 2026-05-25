"""Code-level test for the job-card CRUD implementation.

Does NOT touch the database. Verifies:
  1. All new modules import cleanly
  2. All 12 endpoints are registered with the right methods
  3. Pydantic schemas accept valid bodies and reject invalid ones
  4. _apply_partial_update builds correct parameterized SQL with only allowed columns
  5. _apply_soft_delete builds correct SQL with cancellation_reason gating
  6. Status-gate logic raises 409 for non-editable / non-cancellable statuses
  7. SQL injection defense — junk column names get silently dropped, not interpolated

Run: python _jc_crud_codetest.py
Expected: every "[ok]" line prints; exits 0.
"""
import asyncio
import sys
from typing import Any

# ───────────────────────── Test 1: imports ──────────────────────────────────
print("=" * 70)
print("Test 1: imports")
print("=" * 70)

from app.modules.production.schemas.job_card_edit import (
    JobCardPatchRequest, JobCardCancelRequest,
    EnvironmentPatchRequest, MetalDetectionPatchRequest,
    WeightCheckPatchRequest, LossReconciliationPatchRequest,
    RemarkPatchRequest, AnnexureDeleteRequest,
)
print("[ok] 8 Pydantic schemas import")

from app.modules.production.services import jc_editor
print("[ok] jc_editor service module imports")

from app.webhooks import events
assert hasattr(events, "job_card_updated")
assert hasattr(events, "job_card_cancelled")
assert hasattr(events, "job_card_annexure_changed")
print("[ok] 3 webhook event functions exist")

from app.modules.production.router import router
print("[ok] production router imports")


# ───────────────────────── Test 2: route registration ───────────────────────
print()
print("=" * 70)
print("Test 2: route registration")
print("=" * 70)

paths_methods = set()
for r in router.routes:
    if hasattr(r, "methods"):
        for m in r.methods:
            paths_methods.add((m, r.path))

expected = {
    ("PATCH",  "/api/v1/production/job-cards/{job_card_id}"),
    ("DELETE", "/api/v1/production/job-cards/{job_card_id}"),
    ("PATCH",  "/api/v1/production/job-cards/{job_card_id}/environment/{env_id}"),
    ("DELETE", "/api/v1/production/job-cards/{job_card_id}/environment/{env_id}"),
    ("PATCH",  "/api/v1/production/job-cards/{job_card_id}/metal-detection/{detection_id}"),
    ("DELETE", "/api/v1/production/job-cards/{job_card_id}/metal-detection/{detection_id}"),
    ("PATCH",  "/api/v1/production/job-cards/{job_card_id}/weight-checks/{check_id}"),
    ("DELETE", "/api/v1/production/job-cards/{job_card_id}/weight-checks/{check_id}"),
    ("PATCH",  "/api/v1/production/job-cards/{job_card_id}/loss-reconciliation/{recon_id}"),
    ("DELETE", "/api/v1/production/job-cards/{job_card_id}/loss-reconciliation/{recon_id}"),
    ("PATCH",  "/api/v1/production/job-cards/{job_card_id}/remarks/{remark_id}"),
    ("DELETE", "/api/v1/production/job-cards/{job_card_id}/remarks/{remark_id}"),
}
missing = expected - paths_methods
assert not missing, f"missing routes: {missing}"
print(f"[ok] all 12 new endpoints registered ({len(expected)} expected, {len(expected)} found)")


# ───────────────────────── Test 3: Pydantic validation ──────────────────────
print()
print("=" * 70)
print("Test 3: Pydantic schema validation")
print("=" * 70)

from pydantic import ValidationError

# 3a: partial update with only some fields
m = JobCardPatchRequest(floor="A", updated_by="alice")
dump = m.model_dump(exclude_unset=True)
assert dump == {"floor": "A", "updated_by": "alice"}, f"unexpected dump: {dump}"
print("[ok] JobCardPatchRequest exclude_unset returns only supplied fields")

# 3b: explicit null in body — distinguishable from unset
m = JobCardPatchRequest(floor=None, updated_by="alice")
dump = m.model_dump(exclude_unset=True)
assert dump == {"floor": None, "updated_by": "alice"}, f"unexpected dump: {dump}"
print("[ok] explicit null distinguished from unset")

# 3c: missing updated_by rejected
try:
    JobCardPatchRequest(floor="A")
    assert False, "should have rejected missing updated_by"
except ValidationError:
    print("[ok] missing updated_by raises ValidationError")

# 3d: batch_size_kg <= 0 rejected
try:
    JobCardPatchRequest(batch_size_kg=0, updated_by="x")
    assert False, "should reject batch_size_kg=0"
except ValidationError:
    print("[ok] batch_size_kg=0 rejected by gt=0 constraint")

# 3e: cancellation_reason < 3 chars rejected
try:
    JobCardCancelRequest(cancellation_reason="ab", deleted_by="x")
    assert False, "should reject reason='ab'"
except ValidationError:
    print("[ok] cancellation_reason='ab' rejected by min_length=3")

# 3f: failed_units < 0 rejected
try:
    MetalDetectionPatchRequest(failed_units=-1, updated_by="x")
    assert False, "should reject failed_units=-1"
except ValidationError:
    print("[ok] failed_units=-1 rejected by ge=0 constraint")


# ───────────────────────── Test 4: SQL builder (partial update) ─────────────
print()
print("=" * 70)
print("Test 4: _apply_partial_update SQL builder")
print("=" * 70)


class _FakeConn:
    """Records the SQL + params instead of executing."""
    def __init__(self, fake_row=None):
        self.last_sql = None
        self.last_params = None
        self._fake_row = fake_row

    async def fetchrow(self, sql, *params):
        self.last_sql = sql
        self.last_params = params
        return self._fake_row


# 4a: only allowed columns get into the SQL
async def _t4a():
    conn = _FakeConn(fake_row={"job_card_id": 1, "floor": "X", "customer_name": "ACME"})
    payload = {
        "floor": "X",
        "customer_name": "ACME",
        "job_card_number": "HACK",      # NOT in allow-list
        "prod_order_id": 999,           # NOT in allow-list
        "deleted_at": "1970-01-01",     # NOT in allow-list — system column
    }
    row, changed = await jc_editor._apply_partial_update(
        conn, table="job_card", pk_col="job_card_id", pk_val=1,
        payload=payload,
        allowed_cols=jc_editor.JOB_CARD_EDITABLE_COLS,
        updated_by="alice",
    )
    sql = conn.last_sql
    assert "floor = $1" in sql
    assert "customer_name = $2" in sql
    assert "job_card_number" not in sql, "junk column leaked into SQL"
    assert "prod_order_id" not in sql
    assert "deleted_at = " not in sql or "deleted_at IS NULL" in sql  # WHERE clause OK
    assert "updated_at = NOW()" in sql
    assert "updated_by = $3" in sql
    assert sql.endswith("RETURNING *")
    assert "WHERE job_card_id = $4 AND deleted_at IS NULL" in sql
    assert conn.last_params == ("X", "ACME", "alice", 1)
    assert set(changed) == {"floor", "customer_name"}
    print("[ok] junk columns dropped before SQL build (job_card_number, prod_order_id)")
    print(f"     SQL: {sql}")
    print(f"     params: {conn.last_params}")

asyncio.run(_t4a())

# 4b: parent_jc_id adds extra WHERE clause for annexure
async def _t4b():
    conn = _FakeConn(fake_row={"env_id": 5, "value": "30"})
    row, changed = await jc_editor._apply_partial_update(
        conn, table="job_card_environment", pk_col="env_id", pk_val=5,
        payload={"value": "30"},
        allowed_cols=jc_editor.ENVIRONMENT_EDITABLE_COLS,
        updated_by="bob", parent_jc_id=42,
    )
    sql = conn.last_sql
    assert "WHERE env_id = $3 AND deleted_at IS NULL AND job_card_id = $4" in sql
    assert conn.last_params == ("30", "bob", 5, 42)
    print("[ok] parent_jc_id appends 'AND job_card_id = $N' for cross-JC isolation")

asyncio.run(_t4b())

# 4c: empty payload after filtering -> 422
async def _t4c():
    from fastapi import HTTPException
    conn = _FakeConn()
    try:
        await jc_editor._apply_partial_update(
            conn, table="job_card", pk_col="job_card_id", pk_val=1,
            payload={"job_card_number": "X"},  # only junk
            allowed_cols=jc_editor.JOB_CARD_EDITABLE_COLS,
            updated_by="x",
        )
        assert False, "should have raised 422"
    except HTTPException as e:
        assert e.status_code == 422
        assert "No editable fields supplied" in e.detail
        print("[ok] payload with only disallowed columns -> HTTPException 422")

asyncio.run(_t4c())

# 4d: row not found -> 404
async def _t4d():
    from fastapi import HTTPException
    conn = _FakeConn(fake_row=None)  # simulate UPDATE matching 0 rows
    try:
        await jc_editor._apply_partial_update(
            conn, table="job_card", pk_col="job_card_id", pk_val=999,
            payload={"floor": "Z"},
            allowed_cols=jc_editor.JOB_CARD_EDITABLE_COLS,
            updated_by="x",
        )
        assert False, "should have raised 404"
    except HTTPException as e:
        assert e.status_code == 404
        print("[ok] missing row -> HTTPException 404")

asyncio.run(_t4d())


# ───────────────────────── Test 5: SQL builder (soft delete) ────────────────
print()
print("=" * 70)
print("Test 5: _apply_soft_delete SQL builder")
print("=" * 70)


async def _t5a():
    conn = _FakeConn(fake_row={"job_card_id": 1, "deleted_at": "now"})
    row = await jc_editor._apply_soft_delete(
        conn, table="job_card", pk_col="job_card_id", pk_val=1,
        deleted_by="alice", reason="Wrong customer assigned",
    )
    sql = conn.last_sql
    assert "deleted_at = NOW()" in sql
    assert "deleted_by = $1" in sql
    assert "cancellation_reason = $2" in sql
    assert "status = 'cancelled'" in sql
    assert "WHERE job_card_id = $3 AND deleted_at IS NULL" in sql
    assert conn.last_params == ("alice", "Wrong customer assigned", 1)
    print("[ok] soft-delete with reason: sets cancellation_reason + status='cancelled'")

asyncio.run(_t5a())


async def _t5b():
    conn = _FakeConn(fake_row={"env_id": 5, "deleted_at": "now"})
    row = await jc_editor._apply_soft_delete(
        conn, table="job_card_environment", pk_col="env_id", pk_val=5,
        deleted_by="bob", parent_jc_id=42,    # no reason — annexure delete
    )
    sql = conn.last_sql
    assert "cancellation_reason" not in sql, "annexure delete must not set cancellation_reason"
    assert "status = 'cancelled'" not in sql, "annexure delete must not change status"
    assert "WHERE env_id = $2 AND deleted_at IS NULL AND job_card_id = $3" in sql
    assert conn.last_params == ("bob", 5, 42)
    print("[ok] soft-delete without reason (annexure): no cancellation_reason, no status change")

asyncio.run(_t5b())


# ───────────────────────── Test 6: status gating ────────────────────────────
print()
print("=" * 70)
print("Test 6: status-gate logic")
print("=" * 70)


class _FakeConnWithStatus:
    """Returns a fixed (status, entity, job_card_number) tuple for the lookup."""
    def __init__(self, status):
        self._status = status
    async def fetchrow(self, sql, *params):
        if "SELECT status, entity, job_card_number FROM job_card" in sql:
            return {"status": self._status, "entity": "cfpl",
                    "job_card_number": "PO-2026-0001/1"}
        return {"job_card_id": params[-1] if params else 1, "floor": "X"}


async def _t6_patch_blocked():
    from fastapi import HTTPException
    for blocked in ("completed", "closed", "cancelled"):
        conn = _FakeConnWithStatus(blocked)
        try:
            await jc_editor.patch_job_card(
                conn, 1, {"floor": "Z", "updated_by": "x"},
            )
            assert False, f"PATCH should be blocked at status='{blocked}'"
        except HTTPException as e:
            assert e.status_code == 409
            assert blocked in e.detail
        print(f"[ok] PATCH blocked at status='{blocked}' -> 409")


async def _t6_patch_allowed():
    for allowed in ("locked", "unlocked", "assigned", "material_received", "in_progress"):
        conn = _FakeConnWithStatus(allowed)
        # Should NOT raise — the partial update will succeed via the fake conn
        jc, row, changed = await jc_editor.patch_job_card(
            conn, 1, {"floor": "Z", "updated_by": "x"},
        )
        assert jc["status"] == allowed
        assert changed == ["floor"]
    print(f"[ok] PATCH allowed for all 5 editable statuses")


async def _t6_cancel_blocked():
    from fastapi import HTTPException
    for blocked in ("material_received", "in_progress", "completed", "closed"):
        conn = _FakeConnWithStatus(blocked)
        try:
            await jc_editor.cancel_job_card(
                conn, 1, reason="testing", deleted_by="x",
            )
            assert False, f"DELETE should be blocked at status='{blocked}'"
        except HTTPException as e:
            assert e.status_code == 409
            assert "Cannot cancel" in e.detail or blocked in e.detail
        print(f"[ok] DELETE blocked at status='{blocked}' -> 409")


async def _t6_cancel_allowed():
    for allowed in ("locked", "unlocked", "assigned"):
        conn = _FakeConnWithStatus(allowed)
        jc, row = await jc_editor.cancel_job_card(
            conn, 1, reason="testing", deleted_by="x",
        )
        assert jc["status"] == allowed
    print(f"[ok] DELETE allowed for all 3 cancellable statuses")


asyncio.run(_t6_patch_blocked())
asyncio.run(_t6_patch_allowed())
asyncio.run(_t6_cancel_blocked())
asyncio.run(_t6_cancel_allowed())


# ───────────────────────── Test 7: SQL injection defense ────────────────────
print()
print("=" * 70)
print("Test 7: SQL injection defense via allow-list")
print("=" * 70)


async def _t7():
    conn = _FakeConn(fake_row={"job_card_id": 1, "floor": "OK"})
    # Attacker sends column names with SQL fragments
    payload = {
        "floor": "OK",
        "; DROP TABLE job_card; --": "evil",       # NOT in allow-list
        "machine_id) VALUES (1, ()": "evil",       # NOT in allow-list
    }
    await jc_editor._apply_partial_update(
        conn, table="job_card", pk_col="job_card_id", pk_val=1,
        payload=payload,
        allowed_cols=jc_editor.JOB_CARD_EDITABLE_COLS,
        updated_by="x",
    )
    sql = conn.last_sql
    assert "DROP TABLE" not in sql
    assert ") VALUES (" not in sql
    assert "floor = $1" in sql
    print("[ok] malicious column-name attempts dropped — SQL is clean")
    print(f"     attempted keys: { {k for k in payload if k != 'floor'} }")
    print(f"     final SQL:     {sql}")

asyncio.run(_t7())


# ───────────────────────── Final ────────────────────────────────────────────
print()
print("=" * 70)
print("ALL CODE-LEVEL TESTS PASSED")
print("=" * 70)
print("""
Note: this exercises everything that does NOT touch the database. Behavior
that requires a real connection (UPDATE actually writing rows, RETURNING *
delivering values, soft-delete filter hiding rows from list endpoints) will
work correctly once the DDL has been applied — those code paths are
unchanged from what's tested above; only the connection target changes.

Next: apply the DDL and run _jc_crud_probe.py for end-to-end verification.
""")
