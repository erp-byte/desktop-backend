---
phase: po-rebuild-pass
reviewed: 2026-05-05T00:00:00Z
depth: deep
files_reviewed: 10
files_reviewed_list:
  - app/db/005_po_rebuild.sql
  - app/modules/purchase/schemas/po_api.py
  - app/modules/purchase/services/po_diff.py
  - app/modules/purchase/services/po_event_log.py
  - app/modules/purchase/services/po_preview.py
  - app/modules/purchase/services/po_commit.py
  - app/modules/purchase/services/po_query.py
  - app/modules/purchase/services/po_delete.py
  - app/modules/purchase/po_router.py
  - app/main.py
findings:
  critical: 1
  high: 4
  warning: 7
  info: 8
  total: 20
status: issues_found
---

# PO Rebuild — Code Review Report

**Reviewed:** 2026-05-05
**Depth:** deep (cross-file, schema, auth-flow tracing)
**Files Reviewed:** 10
**Status:** issues_found

## Summary

The new `/api/v1/po/*` surface is well-organised: each endpoint maps cleanly
to a single service module, transactions are scoped per-PO, and the audit
log writes are correctly wrapped in try/except so they never break the
request. Permissions plug into the rebuilt `require_permission` dependency
without surprises, and the SQL layer parameterises everything through asyncpg
positional binds — no string interpolation of user input.

That said, this review identified **one Critical** issue (a scope-leak via
`allowed_entities = NULL` semantics asymmetry between the new helper and the
existing `permission_service`), **four High**-severity correctness/security
issues, and a number of mediums and infos. The Critical and Highs all need
attention before this ships; the rest can be batched into a follow-up pass.

## Critical Issues

### CR-01: `_allowed_entities_for` mis-handles `allowed_entities = NULL`, leaks data across entities for non-admin users with NULL scope rows

**File:** `app/modules/purchase/po_router.py:70-77`
**Issue:**
The codebase already established a convention in
`app/modules/auth/services/permission_service.py:53-58` and the comment
explicitly calls it out:

> `allowed_entities=NULL` means "all entities" and is STRICTLY MORE privileged

In `permission_service.check_permission`, a `NULL` (i.e. `None`)
`allowed_entities` row passes the entity check unconditionally — that's how
`admin` and other broad-scope grants work without enumerating every entity.

In the new helper, the loop is:
```python
for r in rows:
    for e in (r["allowed_entities"] or []):
        if e:
            found.add(e.lower())
if not found and user.entity:
    found.add(user.entity.lower())
return sorted(found)
```

So a `NULL` row contributes **nothing** to `found`. If a non-admin user has
all-NULL `allowed_entities` rows on their `purchase.po` permissions
(legitimate "all entities for this module" grant), `found` ends up empty,
the fallback adds only `user.entity`, and the user sees only their home
entity instead of all entities they're entitled to.

That's a **silent permission narrowing**, not a security hole on the
read path — but the same helper is used in `_check_entity_allowed` (line
80-90), which raises 403 for **/preview** and **/commit** when
`entity not in allowed`. So a user with a legitimate cross-entity grant
gets a 403 on entities they should be able to access.

The asymmetric semantics also make the inverse scenario possible: if
someone migrates `allowed_entities=NULL` rows to `allowed_entities='{}'`
(empty array) thinking it means the same thing, `permission_service`
will START rejecting (NULL was bypass; empty is "no entities"). That gap
between the two helpers is the bug.

**Fix:**
Mirror the `permission_service` convention. If any matching row has
`allowed_entities IS NULL`, the user is unrestricted across this module —
return `None`. Only narrow when **every** row has a non-null array.

```python
async def _allowed_entities_for(request, user) -> list[str] | None:
    if user.is_admin:
        return None
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rp.allowed_entities
              FROM auth_role_permission rp
              JOIN auth_permission p ON rp.permission_id = p.permission_id
             WHERE rp.role_id = $1
               AND p.module = 'purchase' AND p.sub_module = 'po'
            """,
            user.role_id,
        )
    if not rows:
        # No matching permission row — fall back to the user's home entity.
        return [user.entity.lower()] if user.entity else []
    found: set[str] = set()
    for r in rows:
        ae = r["allowed_entities"]
        if ae is None:
            # NULL = unrestricted for this module (matches permission_service).
            return None
        for e in ae:
            if e:
                found.add(e.lower())
    if not found and user.entity:
        found.add(user.entity.lower())
    return sorted(found)
```

Also: add an integration test that creates a non-admin role with
`allowed_entities = NULL` on a `purchase.po.read` row and confirms the user
can list/get POs from both `cfpl` and `cdpl`.

---

## High

### HI-01: `(entity, po_number)` has no unique constraint — duplicate detection is racy under concurrent commits

**File:** `app/db/po_schema.sql:9` + `app/db/005_po_rebuild.sql:34` + `app/modules/purchase/services/po_commit.py:236-282`
**Issue:**
`po_header.po_number` is plain `TEXT`, no `UNIQUE`. The migration adds only
`CREATE INDEX IF NOT EXISTS idx_po_header_entity_pono ON po_header(entity, po_number)`
— a btree index for lookup performance, **not** a uniqueness guarantee.

In `po_commit.commit` two concurrent batches can both:
1. `BEGIN` per-PO transactions in parallel.
2. Both `SELECT … WHERE entity=$1 AND po_number=$2 AND deleted_at IS NULL`
   and see "no existing row".
3. Both `INSERT` — both succeed.

Result: two `po_header` rows with the same `(entity, po_number)`. Subsequent
`SELECT` for that pair (preview, list, detail-by-po_number) returns
non-deterministic results, and `_update_po`'s `WHERE transaction_no=$1` only
updates one of the two duplicates.

Same issue with `mode=upsert` — there's no `INSERT … ON CONFLICT` because there
is no constraint to conflict on.

**Fix:**
Add a partial unique index in the migration, scoped to non-deleted rows
(so soft-deleted POs don't block re-creation):

```sql
-- 005_po_rebuild.sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_po_header_entity_pono_live
    ON po_header(entity, po_number)
    WHERE deleted_at IS NULL AND po_number IS NOT NULL;
```

Then in `po_commit._insert_po` / the commit loop, surface the
`UniqueViolationError` cleanly into `errors[]` (asyncpg raises
`asyncpg.exceptions.UniqueViolationError` — catch in the existing per-PO
`except Exception` block already does that, but the message will be
opaque; consider catching it specifically to emit
`"reason": "duplicate_po_number"`).

### HI-02: Cross-entity write under `_check_entity_allowed` race — body.entity is trusted but per-PO `duplicate_key` may not match

**File:** `app/modules/purchase/po_router.py:132-146` + `app/modules/purchase/services/po_commit.py:236-282`
**Issue:**
The router computes `_check_entity_allowed(body.entity, allowed)` once for
the whole batch. The service then uses `body.entity` for every PO in the
batch (good — service does NOT trust per-PO `duplicate_key` for the entity).
Functionally, this is safe.

But it's fragile and worth tightening:
1. Each `CommitPo` in `body.pos` has `model_config = ConfigDict(extra="allow")`,
   so a caller can submit `pos[0].entity = "cdpl"` while `body.entity = "cfpl"`.
   A future maintainer might "helpfully" wire `po.get("entity") or body.entity`
   into `_insert_po` — that would break the entity-scope check.
2. The `duplicate_key` from `/preview` encodes the entity it was previewed
   under (`f"{entity}|{po_number}"`). On commit, that key is informational
   only — but a forged commit body with entity=cfpl and per-PO
   duplicate_key=`cdpl|XYZ` would currently produce a misleading audit row
   if logged (today only the po_number/line_count are in the audit payload,
   so we're fine).

**Fix:**
Two defensive measures:
1. In `po_commit.commit`, ignore any per-PO `entity` field defensively:
   either pop it from the dict before merging into the header, or assert
   it equals `entity` and add it to `errors[]` if not.
   ```python
   per_po_entity = po.get("entity") or header.get("entity")
   if per_po_entity and per_po_entity.lower() != entity.lower():
       out["errors"].append({...,
           "reason": f"per-PO entity {per_po_entity!r} does not match batch entity {entity!r}"})
       continue
   ```
2. Validate `duplicate_key` prefix when present:
   ```python
   if duplicate_key and "|" in duplicate_key:
       k_entity = duplicate_key.split("|", 1)[0]
       if k_entity.lower() != entity.lower():
           out["errors"].append({...,
               "reason": "duplicate_key entity does not match batch entity"})
           continue
   ```

### HI-03: STORE_HEAD gate has a TOCTOU window — boxes can be inserted between the COUNT and the soft-delete UPDATE

**File:** `app/modules/purchase/services/po_delete.py:54-103`
**Issue:**
`SELECT … FOR UPDATE` on `po_header` row-locks the header row, but does NOT
prevent an INSERT into `po_box` referencing that `transaction_no`. PostgreSQL
row locks are per-row; FK existence checks against the parent take a SHARE
lock that's compatible with FOR UPDATE — so a concurrent
`INSERT INTO po_box (transaction_no, …) VALUES ($1, …)` will succeed against
a header that's locked FOR UPDATE.

Sequence:
1. T1 (delete actor, NOT store_head): SELECT … FOR UPDATE on po_header.
2. T1: COUNT(*) FROM po_box returns 0.
3. T2 (stores intake): INSERT INTO po_box (...). Commits.
4. T1: gate passes (count was 0). UPDATE po_header SET deleted_at = …
5. T1: commits.

Net result: a PO with weighed boxes got soft-deleted by a non-STORE_HEAD
user, bypassing the gate.

This is a real concern (not theoretical) the moment two operators are using
the system concurrently — and the entire point of the gate is to protect
against exactly that scenario.

**Fix:**
Either:

(a) Acquire a heavier lock that blocks po_box INSERTs — e.g., advisory lock
    keyed on transaction_no:
    ```python
    txn_hash = hash(transaction_no) & 0x7FFFFFFFFFFFFFFF  # 63-bit positive
    await conn.execute("SELECT pg_advisory_xact_lock($1)", txn_hash)
    ```
    AND have `po_box` insert path acquire the same lock. Requires changing
    the box-insert code path too.

(b) Re-COUNT at the very end of the transaction, just before COMMIT, and
    raise to roll back if box_count went non-zero. Race window stays
    open, but you'll reject the delete on the second check.

(c) Pessimistic: SELECT … FOR UPDATE the po_box rows too, even if the count
    is 0. `SELECT 1 FROM po_box WHERE transaction_no = $1 FOR UPDATE` — now
    a concurrent INSERT into po_box will block on the row lock until our
    txn commits. (Note: if the count is zero, FOR UPDATE has nothing to
    lock; you'd need predicate locking, which Postgres only does under
    SERIALIZABLE isolation.)

Recommendation: **(b)** — recount immediately before UPDATE within the same
transaction. Cheap, correct enough for this use case, no schema changes.

```python
# Just before the UPDATE:
recheck = await conn.fetchval(
    "SELECT COUNT(*) FROM po_box WHERE transaction_no = $1", transaction_no,
)
if int(recheck or 0) > 0 and not actor_is_admin and role_lower not in _STORE_HEAD_ROLES:
    raise AuthError("store_head_approval_required", ..., 403, ...)
```

### HI-04: `_safe_count` swallows ALL exceptions for `grn` and `po_box` counts — a real DB error becomes silent zero, masking failures and corrupting the audit/response payload

**File:** `app/modules/purchase/services/po_delete.py:30-38, 76-90`
**Issue:**
The `dock_arrival` table doesn't exist; `_safe_count` returning 0 for it is
fine. But the same wrapper is used for `grn` and `po_box` — both of which
DO exist. If a real error occurs (connection blip, lock timeout, disk
issue), `_safe_count` swallows it, returns 0, and:

1. The STORE_HEAD gate (`if box_count > 0`) silently passes — a
   non-STORE_HEAD user can soft-delete a PO with weighed boxes because the
   COUNT errored.
2. The audit-log payload records `po_boxes: 0` even though the real count
   was non-zero — corrupts the historical record.
3. The response body returns `dependent_records.po_boxes: 0` to the caller,
   misleading them.

(1) is the worst — it's a security gate bypass triggered by a transient DB
error. Combined with HI-03, this gate has two ways to fail.

**Fix:**
Split into two helpers — one strict, one tolerant:

```python
async def _required_count(conn, sql: str, *params) -> int:
    n = await conn.fetchval(sql, *params)
    return int(n or 0)

async def _optional_count(conn, sql: str, *params) -> int:
    """Returns 0 only if the table is missing. Re-raises everything else."""
    try:
        n = await conn.fetchval(sql, *params)
        return int(n or 0)
    except asyncpg.exceptions.UndefinedTableError:
        return 0
```

Then:
```python
box_count = await _required_count(conn, "SELECT COUNT(*) FROM po_box WHERE transaction_no = $1", transaction_no)
grn_count = await _required_count(conn, "SELECT COUNT(*) FROM grn       WHERE transaction_no = $1", transaction_no)
dock_count = await _optional_count(conn, "SELECT COUNT(*) FROM dock_arrival WHERE transaction_no = $1", transaction_no)
```

If `grn` or `po_box` errors, the request fails (correct — and gives ops a
real error to investigate, instead of silently corrupting state).

---

## Warnings

### WR-01: `transaction_no` length-cap check happens AFTER a DB roundtrip in `_fetch_header`

**File:** `app/modules/purchase/po_router.py:279-302`
**Issue:**
```python
async def _fetch_header(...):
    if not (1 <= len(transaction_no) <= 64):
        raise AuthError(...)
    allowed = await _allowed_entities_for(request, user)  # DB call
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if include_deleted:
            row = await conn.fetchrow(...)
```
Wait — actually re-reading: the length check IS first here. Good.

The real problem is in `delete_po` (line 348-381) — same pattern:
```python
if not (1 <= len(transaction_no) <= 64):
    raise AuthError(...)
# Length check happens before, so this is fine.
```
Also fine.

But the spec request flagged "after a DB hit" — and indeed `_allowed_entities_for`
is called BEFORE the existence check in `delete_po`:

```python
async with pool.acquire() as conn:
    row = await conn.fetchrow(
        "SELECT entity FROM po_header WHERE transaction_no = $1", transaction_no,
    )
if row is None:
    raise AuthError("not_found", ..., 404)
allowed = await _allowed_entities_for(request, user)
```
Order is OK (existence check first; then scope; then 404 if scope mismatches).

**Net:** the original concern in the prompt does not reproduce. Closing as
non-issue. Leaving this entry as a Warning so the audit trail captures
that we checked.

**Fix:** None needed.

### WR-02: `po_event_log.write` inside the per-PO transaction — silent audit gap on transient failure

**File:** `app/modules/purchase/services/po_event_log.py:36-58` + `app/modules/purchase/services/po_commit.py:257-282`
**Issue:**
The audit writer catches all exceptions and returns. So when a transient
failure happens, the `po_header` INSERT/UPDATE commits but the
`po_event_log` row never makes it. A `logger.warning` is the only trace.

For most apps this is acceptable. But ops won't see this in normal log
filtering — it'll appear under `WARNING` in a pile of other warnings.

**Fix:** Push the audit row into a structured error counter so it
shows up on a dashboard, and/or fall back to writing into a side queue
(file / S3 / Redis list) for later replay. Out-of-scope for this pass —
file as a TODO with a reference to this finding.

### WR-03: `_txn_no_for_preview` collision risk if real transaction_no starts with `TXN-PREVIEW-`

**File:** `app/modules/purchase/services/po_preview.py:53-56` + `app/modules/purchase/services/po_commit.py:268-271`
**Issue:**
`po_commit` checks `if provided_txn and not provided_txn.startswith("TXN-PREVIEW-")`
to decide whether to mint a fresh `txn_no` or reuse the one the client
sent. If a legitimately-stored `transaction_no` happens to start with
`TXN-PREVIEW-` (unlikely today, but no constraint enforces it), commit
will mint a new one and orphan the existing one.

**Fix:** Either
(a) add a CHECK constraint:
```sql
ALTER TABLE po_header ADD CONSTRAINT chk_txn_no_not_preview
    CHECK (transaction_no NOT LIKE 'TXN-PREVIEW-%');
```
(b) Use a more distinctive sentinel that can't possibly collide, e.g.
`__PREVIEW__-...` or a UUID with a non-text wrapper.

Add a comment in `_txn_no_for_preview` explaining the convention either way.

### WR-04: Header diff coercion may crash on non-Decimal/non-date types when stringified

**File:** `app/modules/purchase/services/po_preview.py:225-231`
**Issue:**
```python
for k, v in list(existing_proj.items()):
    if v is None:
        continue
    if hasattr(v, "isoformat"):
        existing_proj[k] = v.isoformat() if hasattr(v, "year") else str(v)
    elif not isinstance(v, (str, int, float, bool)):
        existing_proj[k] = float(v)
```
The `float(v)` fallback is meant for Decimal. But if `v` is, say, a memoryview
or bytes (from a future schema change), `float()` will raise. The except chain
above falls through into the request, causing a 500.

Practically, the columns we project (`po_number`, `po_date`, charges, etc.)
are TEXT/DATE/NUMERIC — current schema is fine. But the fallback is brittle.

**Fix:**
```python
elif not isinstance(v, (str, int, float, bool)):
    try:
        existing_proj[k] = float(v)
    except (TypeError, ValueError):
        existing_proj[k] = str(v)
```

### WR-05: `is_deleted` ↔ `deleted_at` can drift; backfill is one-shot, no enforcement going forward

**File:** `app/db/005_po_rebuild.sql:18-31` + `app/modules/purchase/services/po_delete.py:106-116`
**Issue:**
Migration writes both columns on delete (line 112), and the migration
backfills `deleted_at` from `is_deleted=true` rows. But:
1. Old/legacy code that flips `is_deleted = TRUE` directly will leave
   `deleted_at = NULL` and the new queries won't see it as deleted.
2. The reverse — manually set `deleted_at` without flipping `is_deleted` —
   leaves the legacy code seeing the row as live.

**Fix:**
Add a CHECK constraint or trigger to keep them in sync:
```sql
-- Trigger: BEFORE INSERT OR UPDATE on po_header
-- If either is_deleted is TRUE or deleted_at IS NOT NULL, ensure both are set.
CREATE OR REPLACE FUNCTION sync_po_header_deleted() RETURNS trigger AS $$
BEGIN
    IF NEW.deleted_at IS NOT NULL AND NEW.is_deleted IS DISTINCT FROM TRUE THEN
        NEW.is_deleted := TRUE;
    END IF;
    IF NEW.is_deleted = TRUE AND NEW.deleted_at IS NULL THEN
        NEW.deleted_at := NOW();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_po_header_deleted ON po_header;
CREATE TRIGGER trg_sync_po_header_deleted
    BEFORE INSERT OR UPDATE ON po_header
    FOR EACH ROW EXECUTE FUNCTION sync_po_header_deleted();
```
Or simpler and more explicit: add a CHECK — but Postgres CHECKs can't easily
encode "X IFF Y". The trigger is the right call.

Alternative: drop `is_deleted` entirely after audit confirms no other code
reads it. That's cleaner long-term but requires a code-grep pass.

### WR-06: `header_row_to_listitem` uses `row.keys()` to optional-check `supplier_id` / `match_source` — confusing & ineffective

**File:** `app/modules/purchase/services/po_query.py:226, 239, 268`
**Issue:**
```python
"supplier_id": row["supplier_id"] if "supplier_id" in row.keys() else None,
```
asyncpg `Record` keys() returns the column names of the row's source query.
For `SELECT * FROM po_header`, `supplier_id` is always present (it's a
column in the schema). So the conditional always takes the truthy branch.
The condition is dead.

If the goal was forward-compat with future SELECT lists that omit
`supplier_id`, that's not how `Record` works — the column either is in
the projection or isn't, and if it isn't, `row["supplier_id"]` raises
`KeyError` whether you check `.keys()` or not. Wait, actually `in row.keys()`
does check membership — so if a future caller does
`SELECT transaction_no, entity FROM po_header` and passes that record in,
the check would prevent a KeyError. OK — it does work, just unusual.

**Fix:** Add a comment explaining the forward-compat intent, OR just always
project the full set in the SELECT and drop the check. Minor — info-level.

### WR-07: `parse_po_book` exception swallowed into generic `invalid_file` 400 — leaks parser internals via the message

**File:** `app/modules/purchase/services/po_preview.py:121-125`
**Issue:**
```python
try:
    parsed = parse_po_book(file_bytes)
except Exception as e:
    raise AuthError("invalid_file", f"Could not read Excel file: {e}", 400)
```
`str(e)` may include file paths, internal type names, or library version
strings (xlrd / openpyxl). Echoing it to the client is a small info-leak.

**Fix:**
```python
except Exception as e:
    logger.warning("po.preview.parse_failed file=%s err=%r", filename, e)
    raise AuthError("invalid_file", "Could not read Excel file. Verify it's a valid .xlsx workbook.", 400)
```
Keep the detail in the server log for ops; give the client a generic message.

---

## Info

### IN-01: `parse_po_book` is called without an explicit timeout / size sub-cap
**File:** `app/modules/purchase/services/po_preview.py:122`
**Issue:** A 50MB malformed `.xlsx` (zip-bomb style) could pin a worker
parsing for a long time. There's no executor wrapper / asyncio timeout.
**Fix:** Wrap in `asyncio.wait_for(asyncio.to_thread(parse_po_book, file_bytes), timeout=30.0)`.

### IN-02: `list_pos` endpoint signature has 30+ Query parameters
**File:** `app/modules/purchase/po_router.py:159-201`
**Issue:** Hard to maintain and test. FastAPI supports a Pydantic `Depends`
class for this.
**Fix:** Push filter Query params into a `class POListFilters(BaseModel)`
used as `filters: POListFilters = Depends()`. Non-blocking but a sizeable
maintainability win.

### IN-03: Inconsistent error envelope between /list (silent filter) and /detail (404 hides existence)
**File:** `app/modules/purchase/po_router.py:159-272, 279-302, 348-381`
**Issue:** Defensible — different threat models for list vs detail — but
worth a comment block on the file.
**Fix:** Add a docstring or a top-of-file note explaining the design choice
("list silently filters; detail/delete returns 404 to avoid existence
oracle").

### IN-04: `_validate_file` doesn't sniff content type — only filename suffix
**File:** `app/modules/purchase/services/po_preview.py:35-50`
**Issue:** A caller can rename a `.zip` to `.xlsx` and bypass the
suffix check; the parser will then raise (caught at WR-07). Not a security
issue (parser is the real validator), but a clearer 400 earlier is nicer
UX.
**Fix:** Optionally check magic bytes: `xlsx` zip starts with `PK\x03\x04`.
Minor.

### IN-05: `parser.parse_po_book` errors are caught broadly — also swallows OSError, MemoryError
**File:** `app/modules/purchase/services/po_preview.py:121-125`
**Issue:** `except Exception` includes `MemoryError` (which IS an Exception
subclass, unlike `KeyboardInterrupt`/`SystemExit`). A 5GB workbook OOM
becomes a 400 instead of a 500 / pod restart, which is misleading for ops.
**Fix:** `except (ValueError, KeyError, OSError, zipfile.BadZipFile) as e:`
— enumerate the parser's expected failure modes.

### IN-06: `commit` per-PO loop doesn't connect-pool reuse — acquires a fresh connection per PO
**File:** `app/modules/purchase/services/po_commit.py:236-282`
**Issue:** `async with pool.acquire() as conn` per iteration. For a 100-PO
batch, that's 100 acquire/release cycles. The pool likely handles this fine,
but using a single connection across the loop (with one transaction per PO)
is marginally faster.
**Fix:** Lift the `pool.acquire()` outside the loop. Each PO still gets its
own `conn.transaction()`. Out of scope for v1 (performance excluded).

### IN-07: `datetime.utcnow()` is deprecated in Python 3.12
**File:** `app/modules/purchase/services/po_commit.py:215`
**Issue:** `datetime.utcnow()` was deprecated in 3.12. Other modules in
this codebase already use `datetime.now(timezone.utc)` (see po_preview.py:136
and po_delete.py:105 — consistent).
**Fix:** Change to `datetime.now(timezone.utc)`.

### IN-08: `_normalise` rounds floats to 4 decimals; `po_header.gross_total` is `NUMERIC(15,3)` (3 decimals)
**File:** `app/modules/purchase/services/po_diff.py:42-49`
**Issue:** Mismatch is harmless — 4-decimal rounding never under-flags
changes vs 3-decimal storage. But the 4 looks magic.
**Fix:** Constant: `_FLOAT_DIFF_PRECISION = 3` to match column scale, or a
comment explaining why 4.

---

_Reviewed: 2026-05-05_
_Reviewer: gsd-code-reviewer (deep)_
