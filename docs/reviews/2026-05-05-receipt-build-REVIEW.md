---
phase: receipt-build
reviewed: 2026-05-05T00:00:00Z
depth: deep
files_reviewed: 9
files_reviewed_list:
  - app/db/006_receipt_documents.sql
  - app/modules/receipt/__init__.py
  - app/modules/receipt/services/__init__.py
  - app/modules/receipt/storage.py
  - app/modules/receipt/schemas.py
  - app/modules/receipt/services/coa_service.py
  - app/modules/receipt/services/invoice_service.py
  - app/modules/receipt/router.py
  - app/main.py
findings:
  critical: 2
  warning: 9
  info: 7
  total: 18
status: issues_found
---

# Receipt-Build Code Review Report

**Reviewed:** 2026-05-05
**Depth:** deep
**Files Reviewed:** 9 (7 new, 1 modified, 2 empty `__init__.py` package markers)
**Status:** issues_found

## Summary

The receipt build is well-structured: it cleanly mirrors the PO module's
patterns (entity-scope helper, structured `AuthError` envelope, allowlist
validation), the storage abstraction is the right shape, and the migration
is appropriately additive/idempotent. Permission seeds, role-name casing,
and self-FK typing are all correct.

Two issues are **Critical** because they directly weaken the security
posture the spec asks for:

1. **`SELECT FOR UPDATE` outside a transaction in `replace_coa`** — the
   row lock is silently a no-op under asyncpg's autocommit-per-statement
   model, exposing the documented "two concurrent replace" race the spec
   explicitly intended to close.
2. **`list_coa` allows unscoped (`entity IS NULL`) rows for every
   non-admin user** — this is silent cross-entity leakage of any COA whose
   anchor failed to resolve at upload time (which is most non-PO uploads
   today, since `qc_inspection_intimation` and `dock_arrival_intimation`
   don't exist yet, so `_resolve_entity` returns `None` for them).

There are also several **High/Warning** findings around mime sniffing
fall-through, the replacement-chain CTE direction, soft-delete vs. status
drift, the JWT `iss` claim verification, and the `parsed_params_json`
inheritance ternary.

---

## Critical Issues

### CR-01: `SELECT FOR UPDATE` runs outside any transaction in `replace_coa` — row lock is a no-op

**File:** `app/modules/receipt/services/coa_service.py:473-499`
**Issue:**
The flow is:

```python
async with pool.acquire() as conn:
    old = await conn.fetchrow(
        "SELECT * FROM coa_document WHERE coa_id = $1 FOR UPDATE", coa_id,
    )                                       # ← line 474, NOT in a txn
    ...checks, storage.put...
    async with conn.transaction():          # ← line 512, txn starts here
        await conn.execute("INSERT ...")
        await conn.execute("UPDATE ... SET coa_status='superseded' ...")
```

asyncpg defaults to autocommit-per-statement when there is no active
transaction. `SELECT … FOR UPDATE` outside a transaction acquires the row
lock and **immediately releases it** when the implicit single-statement
transaction commits. By the time the INSERT/UPDATE block opens its
transaction (line 512), the lock is gone.

Concrete race:

1. Operator A and B both PUT `coa/{id}` simultaneously.
2. Both `fetchrow ... FOR UPDATE` succeed without blocking.
3. Both pass the `coa_status='active'`, ownership, and 24h checks.
4. Both upload to storage (two orphaned blobs survive).
5. Both open their transaction, INSERT a fresh `coa_id`, and UPDATE the
   old row. The two UPDATEs serialize — but the outcome is two
   independent supersedes of the same predecessor. The second
   replacement overwrites `replaced_at/by/reason` of the first; the first
   "new" row is now orphaned in the chain (pointing at a row that says
   it was replaced by the *second* new row).

Spec comment in the file ("Two concurrent replace calls … the second
blocks on the row lock, then sees `coa_status='superseded'` and 409s")
relies on the lock surviving — it does not.

**Fix:** Open the transaction BEFORE the `FOR UPDATE`, and either move
the `storage.put` inside it (cheap; a failed COMMIT means an orphan blob,
which is the same risk that already exists) or hold a separate "claim"
state in the DB before uploading:

```python
async with pool.acquire() as conn:
    async with conn.transaction():
        old = await conn.fetchrow(
            "SELECT * FROM coa_document WHERE coa_id = $1 FOR UPDATE",
            coa_id,
        )
        # ...all checks here...
        # storage.put here — orphan blob on rollback is acceptable, same as upload_coa
        storage.put(s3_key, file_bytes, effective_mime)
        await conn.execute("INSERT ...")
        await conn.execute("UPDATE ... SET coa_status='superseded' ...")
```

If holding the connection across an HTTP-bound `storage.put` is a concern
(S3 latency × pool slot), introduce a transient `coa_status='replacing'`
state under a short transaction first, then upload, then a second
transaction to flip to `superseded` only if the row is still in
`replacing` and owned by this request id.

---

### CR-02: `list_coa` leaks `entity IS NULL` rows to every non-admin caller (cross-entity privilege escalation)

**File:** `app/modules/receipt/services/coa_service.py:335-341`
**Issue:**

```python
if entity_scope is not None:
    if not entity_scope:
        conditions.append("FALSE")
    else:
        idx += 1
        conditions.append(f"(entity IS NULL OR entity = ANY(${idx}::text[]))")
        params.append(list(entity_scope))
```

Today, `_resolve_entity` only resolves entity from `po_header`. Both
`qc_inspection_intimation` and `dock_arrival_intimation` are documented
as not yet existing, so any COA uploaded with only a qc/dock anchor (or
sku/sku_name_raw/supplier_id) lands in the table with `entity = NULL`.
The `entity IS NULL OR …` clause then makes that row visible to every
authenticated user with `receipt.coa.read`, irrespective of their entity
allowlist.

This contradicts:
- The spec direction quoted in the prompt ("entity-scoping").
- The detail/replace/delete paths, which use a stricter check —
  `if entity_scope is not None and row["entity"] and row["entity"].lower() not in entity_scope: 404`.
  That check does not 404 on NULL either, so the inconsistency is two-way,
  but list compounds it by exposing rows the user could not have
  enumerated otherwise.

Until the missing intimation tables land, the practical effect is that
any non-admin can list COAs uploaded by any other entity as long as the
uploader chose a non-PO anchor.

**Fix:** Decide one semantic and apply it everywhere. Two options:

Option A (strict — recommended): NULL entity is admin-only.

```python
conditions.append(f"entity = ANY(${idx}::text[])")
```

In detail/replace/delete, change to:

```python
if entity_scope is not None and (row["entity"] is None
        or row["entity"].lower() not in entity_scope):
    raise AuthError("coa_not_found", "COA not found", 404)
```

Option B (lenient — only if the spec really wants legacy/unanchored rows
visible to all): keep `entity IS NULL OR ...` in list AND make
detail/replace/delete also allow NULL. Document the security implication
in CLAUDE.md.

The current half-and-half state should not ship.

---

## Warnings

### WR-01: `sniff_mime` fall-through accepts unsniffable bodies under any allowed declared mime (PDF spoofing of a ZIP, etc.)

**File:** `app/modules/receipt/storage.py:52-76`, called from
`app/modules/receipt/services/coa_service.py:133-145` and
`app/modules/receipt/services/invoice_service.py:53-65`
**Issue:** The sniffer only matches PDF / JPEG / PNG by leading bytes.
For anything else it returns `(declared, False)` (no mismatch). The
caller then passes `sniffed in ALLOWED_RECEIPT_MIMES` — which is true
because `sniffed == declared` and `declared` was already allowlist-checked.

So a client that POSTs `Content-Type: application/pdf` with a body that
is actually a ZIP, a Word doc, an HTML file with embedded `<script>`, or
arbitrary binary, passes validation and is stored as `mime_type =
'application/pdf'`. When a downstream user clicks the download URL
(local backend), it is served with `Content-Type: application/pdf` —
some browsers will sniff and execute the real type anyway, and an
attacker can target the smaller surface of "what gets opened in this
viewer."

**Fix:** Reject bodies whose magic bytes don't match the allowlist. Two
ways:

```python
def sniff_mime(content, declared):
    ...
    if sniffed is None:
        return (declared or "application/octet-stream"), True   # treat as mismatch
```

Or in the service `_validate_file`, after calling `sniff_mime`:

```python
sniffed, mismatch = storage_mod.sniff_mime(content, declared)
if sniffed not in storage_mod.ALLOWED_RECEIPT_MIMES or sniffed != declared:
    raise AuthError("mime_type_mismatch", ..., 415)
```

Whichever, the contract should be: sniffed type MUST be one of
`{pdf, jpeg, png}` and MUST equal declared. No fall-through.

---

### WR-02: `verify_local_token` may not enforce the `iss` claim

**File:** `app/modules/receipt/storage.py:101-116`
**Issue:**

```python
payload = pyjwt.decode(
    token,
    _secret(settings),
    algorithms=[_alg(settings)],
    issuer=settings.JWT_ISSUER,
    options={"require": ["exp", "sub", "purpose"]},
)
```

PyJWT's behaviour: passing `issuer=` enables verification of the `iss`
claim, but only if `iss` is present in the payload. If the token has no
`iss` claim at all, PyJWT will not raise `MissingRequiredClaimError`
unless `iss` is in the `require` list. With the current `require=["exp",
"sub", "purpose"]`, a token signed with the same secret but missing the
`iss` claim entirely will pass — which means any other consumer of the
JWT secret that mints tokens without `iss` could be replayed here.

In practice, today the only minter is `_local_token` which always sets
`iss`, so the risk is theoretical. But the code is defence-in-depth, and
this defence has a gap.

**Fix:**

```python
options={"require": ["exp", "sub", "purpose", "iss"], "verify_iss": True},
```

Add `"iss"` to `require`, and explicitly set `verify_iss: True` for
clarity (default is True when `issuer=` is passed, but explicit beats
implicit).

---

### WR-03: `parsed_params_json` inheritance ternary in `replace_coa` is unreadable and has at least one unreachable branch

**File:** `app/modules/receipt/services/coa_service.py:537-539`
**Issue:**

```python
json.dumps(parsed_params) if parsed_params else
    (old["parsed_params_json"] if isinstance(old["parsed_params_json"], str)
     else json.dumps(_parse_json_field(old["parsed_params_json"]) or {}) if old["parsed_params_json"] else None),
```

Tracing the cases:

| caller | old type        | result                                     |
|--------|-----------------|--------------------------------------------|
| dict   | anything        | `json.dumps(new)`                          |
| None   | str             | old (already a JSON string — OK)           |
| None   | dict            | `json.dumps(_parse_json_field(dict) or {})` — but `_parse_json_field(dict)` returns the dict, so `json.dumps(dict)` (OK) |
| None   | None / falsy    | `None`                                     |

Behaviourally OK, but:

1. If `parsed_params` is an empty dict `{}` from the caller (valid JSON
   object), the truthiness check `if parsed_params` evaluates False and
   silently falls through to inheriting the OLD value. That's a bug — an
   explicit `{}` from the caller should overwrite, not inherit.
2. The branch `json.dumps(_parse_json_field(...) or {})` swallows
   parse failures and substitutes `{}`, which is not what the caller
   would expect after a successful "replace inherits old" semantic.
3. The fallback `if old["parsed_params_json"] else None` will treat an
   old empty-dict-stored-as-`'{}'` string as truthy (string `"{}"` is
   truthy) but an empty dict object as falsy — inconsistent.

**Fix:** Extract a helper and check `is None`, not truthiness:

```python
def _resolve_parsed_params(new_dict, old_value):
    if new_dict is not None:
        return json.dumps(new_dict)
    if old_value is None:
        return None
    if isinstance(old_value, str):
        return old_value
    if isinstance(old_value, dict):
        return json.dumps(old_value)
    return None  # unknown type — log
```

And call it once: `_resolve_parsed_params(parsed_params, old["parsed_params_json"])`.

---

### WR-04: Replacement-chain CTE only walks downstream; predecessors of the requested COA are missed

**File:** `app/modules/receipt/services/coa_service.py:414-432`
**Issue:** The recursive CTE seeds with `$1::text` (the requested
`coa_id`) and joins `c.replaces_coa_id = chain.id`. That direction means
"find rows whose `replaces_coa_id` is in the chain" — i.e. successors of
the seed.

So `replacement_history` only contains rows where `new_coa_id`'s
ancestor is reachable downstream from the requested coa. If the user
requests:

- The original (oldest) COA → walks forward, finds all successors. OK.
- A middle COA → finds successors only, misses the predecessor that this
  one replaced.
- The latest (active) COA → finds nothing (no successors), even though
  there is rich history backwards.

The detail response includes `replaces_coa_id` for the requested row
(line 437), so the client gets one hop backwards, but the array is
expected to be the full chain.

**Fix:** Walk both directions, then filter out the seed:

```sql
WITH RECURSIVE
  fwd(id) AS (
    SELECT $1::text
    UNION ALL
    SELECT c.coa_id FROM coa_document c JOIN fwd ON c.replaces_coa_id = fwd.id
  ),
  bwd(id) AS (
    SELECT $1::text
    UNION ALL
    SELECT c.replaces_coa_id FROM coa_document c JOIN bwd ON c.coa_id = bwd.id
      WHERE c.replaces_coa_id IS NOT NULL
  ),
  chain AS (SELECT id FROM fwd UNION SELECT id FROM bwd)
SELECT replaces_coa_id AS old_coa_id, coa_id AS new_coa_id,
       replaced_at, replaced_reason
  FROM coa_document
 WHERE replaces_coa_id IS NOT NULL
   AND coa_id IN (SELECT id FROM chain)
 ORDER BY replaced_at NULLS LAST;
```

---

### WR-05: COA delete uses `coa_status='deleted'` AND `deleted_at` simultaneously — same drift surface as po_header

**File:** `app/modules/receipt/services/coa_service.py:586-596`,
`app/modules/receipt/services/coa_service.py:323-330` (list filter)
**Issue:** `soft_delete_coa` writes both `coa_status='deleted'` and
`deleted_at = NOW()`. `list_coa` filters on `coa_status` (when supplied)
and `get_coa_detail` does no soft-delete filter at all (it returns
deleted rows happily, gated only by entity).

Drift conditions:

- Manual SQL fix-ups that set `coa_status='deleted'` without `deleted_at`
  (or vice versa) leave the row in an inconsistent state — no app-level
  invariant enforces "exactly one or the other."
- The active/deleted check in `replace_coa` (line 479) and
  `soft_delete_coa` (line 584) only inspects `coa_status`. A row with
  `deleted_at IS NOT NULL` but `coa_status='active'` would still be
  replaceable.

**Fix:** Pick one source of truth. Recommended: `coa_status` is the
single canonical state, `deleted_at` is purely audit metadata. Add a DB
check constraint or trigger:

```sql
ALTER TABLE coa_document ADD CONSTRAINT coa_document_deleted_at_consistent
    CHECK ((coa_status = 'deleted') = (deleted_at IS NOT NULL));
```

(Note: this requires a one-time data fixup; safe today since row count
was 0 at migration time.) And in `get_coa_detail`, default to filtering
out `coa_status='deleted'` unless an `include_deleted=true` flag is
passed.

---

### WR-06: COA upload silently allows `entity = NULL` while invoice upload rejects it (`entity_unresolved` 400)

**File:** `app/modules/receipt/services/coa_service.py:225-258`
(no entity gate) vs. `app/modules/receipt/services/invoice_service.py:167-173`
**Issue:** Inconsistency that compounds CR-02. Two callers, same
spec-shaped problem ("we couldn't resolve which entity owns this
upload"), two different policies:

- COA upload: writes `entity = None`, returns 201. The row then becomes
  visible to all per CR-02.
- Invoice upload: 400 `entity_unresolved`.

Pick one. If the COA path needs to support unresolved-entity uploads
intentionally (because the qc/dock tables don't exist yet), make that
explicit in the schema (e.g. `entity = '<unresolved>'`) and special-case
visibility, or add a backfill job.

**Fix:** Mirror the invoice service: raise `entity_unresolved` (400)
unless an explicit `allow_unanchored=true` form flag is set, restricted
to admin or a dedicated permission. Or, conversely, soften
`invoice_service` to also write NULL — but only after CR-02 is fixed.

---

### WR-07: `LocalStorage.open_for_read` `startswith` check is fragile on Windows and after symlink resolution

**File:** `app/modules/receipt/storage.py:133-142`
**Issue:**

```python
path = self.base / key
if not path.is_file():
    raise FileNotFoundError(key)
path = path.resolve()
if not str(path).startswith(str(self.base)):
    raise PermissionError("path traversal")
```

Three concerns:

1. `Path.resolve()` follows symlinks. If the storage base contains a
   symlink (placed by an admin, a backup tool, or accidentally) that
   targets outside the base, `path.resolve()` returns the symlink target
   and the `startswith` check fails — but only as a denial-of-read of
   legitimate files. Worse: if a symlink target's resolved path happens
   to be a prefix-extension of `self.base` (e.g. `self.base =
   C:\storage` and target `C:\storage_other\foo`), `startswith` returns
   True, and a file outside the intended dir is served. On Windows in
   particular, case sensitivity is sometimes handled by Path's resolve
   and sometimes not, and a stem-collision attack (`C:\storageX` vs
   `C:\storage`) is the concrete failure mode.
2. Today, the key is server-generated (`new_storage_key` uses uuid4 and
   a fixed prefix), so the attack surface is admin/ops error rather
   than client input. Defence-in-depth still matters because S3 mode
   does NOT have this issue (S3 keys are flat strings), so the bug
   would only surface in dev/local.
3. `path.is_file()` happens BEFORE the resolve+check. A traversal
   attempt to a real file outside the base would pass `is_file()` and
   only fail on `startswith` — fine, but the order signals confused
   intent.

**Fix:** Use `Path.is_relative_to` (Python 3.9+) on the resolved path,
and disallow symlinks explicitly:

```python
candidate = (self.base / key).resolve(strict=False)
try:
    candidate.relative_to(self.base)            # raises ValueError if outside
except ValueError:
    raise PermissionError("path traversal")
if candidate.is_symlink():
    raise PermissionError("symlinked storage entries are not allowed")
if not candidate.is_file():
    raise FileNotFoundError(key)
return candidate.read_bytes()
```

Same fix in `LocalStorage.put` for the parent-dir mkdir (currently could
in principle escape the base if `key` contained `..` or absolute path
prefixes — server-generated today, but the validation is one line and
removes the implicit trust):

```python
parent = (self.base / key).resolve(strict=False).parent
parent.relative_to(self.base)
parent.mkdir(parents=True, exist_ok=True)
```

---

### WR-08: 10 MB upload limit is enforced AFTER the entire body is read into memory

**File:** `app/modules/receipt/router.py:100`,
`app/modules/receipt/router.py:207`,
`app/modules/receipt/router.py:255`,
`app/modules/receipt/services/coa_service.py:120-125`
**Issue:** All three upload endpoints call `await file.read()` (which
buffers the whole body into memory) and only then check
`len(content) > _MAX_FILE_BYTES`. A malicious or buggy client uploading
a 100 MB file forces 100 MB of allocation per request before the 413 is
returned. With concurrent uploads this is a trivial DoS surface on a
small instance.

**Fix:** Either add a Starlette middleware that rejects on
`Content-Length` > limit (cheap, works even before any bytes are read),
or stream-read with a running counter:

```python
buf = bytearray()
while chunk := await file.read(64 * 1024):
    buf.extend(chunk)
    if len(buf) > _MAX_FILE_BYTES:
        raise AuthError("file_too_large", ..., 413)
contents = bytes(buf)
```

Also add Starlette `Content-Length`-based middleware as a first line of
defence. The prompt notes this is acceptable for v1; flagging as Warning
because the right fix is small and local.

---

### WR-09: `replace_coa` storage write happens even if the row is already superseded by a parallel request

**File:** `app/modules/receipt/services/coa_service.py:501-507`
**Issue:** Related to CR-01 but worth calling out separately. The
storage write (`storage.put`) happens at lines 503-507, BEFORE the
transaction block opens. Even with CR-01 fixed, if `storage.put`
succeeds and the subsequent UPDATE fails (DB unavailable, constraint
violation, etc.), the orphaned blob remains in storage forever. There
is no compensating delete.

This is the same pattern as `upload_coa` (also leaves orphans on DB
failure), so the bug is consistent with prior art — but worth a janitor
job spec note.

**Fix:** Either accept the orphan (log it with the key for a future
sweeper), OR write to a temp key first, update DB, then rename:

```python
tmp_key = f"_pending/{uuid.uuid4()}"
storage.put(tmp_key, file_bytes, mime)
try:
    # ...DB writes referencing the final key...
    storage.rename(tmp_key, final_key)
except Exception:
    storage.delete(tmp_key)
    raise
```

For v1, log the orphan key on DB failure so a later sweeper can clean.

---

## Info

### IN-01: `_PRESIGN_TTL_SECONDS = 300` hardcoded; should live in `Settings`

**File:** `app/modules/receipt/services/coa_service.py:30`
**Fix:** Move to `Settings.RECEIPT_PRESIGN_TTL_SECONDS`, default 300.

---

### IN-02: `_REPLACE_WINDOW_HOURS = 24` hardcoded

**File:** `app/modules/receipt/services/coa_service.py:31`
**Fix:** Move to `Settings.RECEIPT_REPLACE_WINDOW_HOURS`, default 24.
Operations may want to tune per-environment.

---

### IN-03: `_local_token` reuses the JWT signing secret

**File:** `app/modules/receipt/storage.py:88-98`
**Issue:** Anyone who learns the JWT secret can mint download tokens for
any `s3_key`. The `purpose` claim disambiguates against the access/refresh
token flows, so a stolen access token can't be replayed as a download
token, but the attack surface is "trust boundary === JWT secret" rather
than the narrower "trust boundary === download-signing key."
**Fix:** Acceptable trade-off for v1 given the secret is already trusted.
Worth a separate `Settings.RECEIPT_DOWNLOAD_SECRET` (default to JWT
secret if unset) so a future operator can rotate the download secret
without rotating all access tokens.

---

### IN-04: Form-field empty-string vs None semantics for optional ints

**File:** `app/modules/receipt/router.py:86-96`,
`app/modules/receipt/router.py:248-251`
**Issue:** `Form(None)` for `int | None` fields. If a multipart form
sends `dock_intimation_id=` (empty string), Pydantic v2 will raise
`int_parsing` and FastAPI returns 422 — which is correct, but the
422 envelope from FastAPI's default validation handler is NOT the
project's structured `AuthError` envelope (it's the FastAPI default
`{detail: [...]}` shape). Inconsistent with the rest of the API.
**Fix:** Either accept the FastAPI 422 (acceptable; mirrored by other
modules) or wire a global validation-exception handler that wraps
RequestValidationError into the structured envelope. Probably out of
scope for this PR.

---

### IN-05: Token-bearing URLs (>500 chars) for local storage

**File:** `app/modules/receipt/storage.py:144-146`
**Issue:** A signed JWT plus the `/api/v1/receipt/files/` prefix is
typically 250-400 chars, which is fine for browsers and most CDNs, but
some corporate proxies enforce 256-char URL limits. Acceptable trade-off
for v1; worth documenting in the storage backend's README that local
mode is intended for dev / single-tenant deploys, not for
proxy-fronted production.

---

### IN-06: `S3Storage.presigned_get` swallows boto3 errors and returns `""`

**File:** `app/modules/receipt/storage.py:180-189`
**Issue:** On a transient S3 throttle or credentials issue, the list
endpoint returns a row with `download_url = None` (because of `_row_to_listitem`
line 394 `download_url or None`). The user has no way to tell whether
the file is missing, the URL signing failed, or scanning is pending.
**Fix:** Either let the exception bubble (the list endpoint will still
respond — `_row_to_listitem` already wraps in a try/except at lines 367-373
in the local case, so wrap the S3 case the same way and return None
explicitly with a logged correlation ID), or return a structured error in
the row (`download_status: "presign_failed"`).

---

### IN-07: `coa_document_anchor_present` check constraint is whole-table validating

**File:** `app/db/006_receipt_documents.sql:60-69`
**Issue:** Migration adds a CHECK that requires at least one of the new
anchors. PostgreSQL validates the constraint against existing rows. If
any row in `coa_document` had all-NULL anchors at migration time
(impossible historically because `transaction_no` was NOT NULL pre-005,
but a partially-applied migration could create the gap), the migration
fails.

Audit dump shows row count = 0, so safe today. Document the
prerequisite: `006` must run on an empty table OR after a backfill that
ensures `anchor_present` is satisfied for every row.

**Fix:** Add a `\echo` in the migration:

```sql
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM coa_document
             WHERE transaction_no IS NULL
               AND dock_intimation_id IS NULL
               AND qc_intimation_id IS NULL
               AND sku_id IS NULL
               AND sku_name_raw IS NULL) THEN
    RAISE EXCEPTION 'Cannot apply 006: rows exist with no anchors. Backfill first.';
  END IF;
END$$;
```

Run BEFORE the `ADD CONSTRAINT`, so the failure mode is explicit.

---

## Items Verified Clean (no findings)

- Role-name casing in `006_receipt_documents.sql` matches `auth_schema.sql`
  (`admin`, `store_head`, `stores_manager`, `viewer` — all lowercase). Note:
  `005_po_rebuild.sql` mentions `STORE_HEAD` only in a header comment; the
  actual `INSERT INTO auth_role` uses `'store_head'`. Consistent.
- Self-FK type: `coa_document.coa_id` is TEXT (gen_short_id default),
  `replaces_coa_id` is TEXT — matches.
- Migration ALTERs (`DROP CONSTRAINT IF EXISTS`, `DROP NOT NULL`,
  `ADD COLUMN IF NOT EXISTS`) are instant on PG ≥ 11.
- Permission seeds correctly grant the four `coa.*` actions and both
  `invoice.*` actions to admin and `store_head`; `stores_manager` gets
  `coa.{read,create,update}` (no delete) plus full invoice; `viewer` gets
  read-only on both.
- The download endpoint correctly does not gate on `require_permission` —
  the token IS the auth, mirroring real S3 presigned-URL semantics. Good
  trade-off, well-commented.
- `_validate_anchors` correctly rejects upload with all five anchors
  null, satisfying the DB CHECK at the application layer.
- The `_resolve_entity` helper properly catches `asyncpg.UndefinedTableError`
  for the missing intimation tables.

---

_Reviewed: 2026-05-05_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
