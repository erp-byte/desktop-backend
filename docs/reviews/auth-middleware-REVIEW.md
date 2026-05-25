---
reviewed: 2026-04-18
depth: standard
files_reviewed: 2
files_reviewed_list:
  - app/modules/auth/middleware.py
  - app/modules/auth/services/auth_service.py
context_files_scanned:
  - app/modules/auth/router.py
  - app/modules/auth/services/permission_service.py
  - app/webhooks/router.py
  - app/webhooks/ws_router.py
  - app/db/auth_schema.sql
findings:
  critical: 2
  high: 5
  medium: 6
  low: 4
  total: 17
status: issues_found
verdict: CHANGES_REQUIRED_BEFORE_PROD
---

# Auth Middleware & Session Duration — Code Review

**Reviewer:** Claude (gsd-code-reviewer)
**Reviewed:** 2026-04-18
**Depth:** standard
**Scope:** `app/modules/auth/middleware.py` (new, 121 lines) + `SESSION_DURATION_HOURS` change in `auth_service.py`

## Executive Summary

The new RBAC middleware is structurally reasonable — it cleanly wraps `HTTPBearer`, delegates session validation to the existing service layer, and exposes a `require_permission(...)` factory that is already used in `app/webhooks/router.py`. The admin bypass path is correctly sourced from `auth_role.is_admin` (trusted, DB-derived), not from user input. That's good.

However, as the gatekeeper for every protected endpoint, it has several security and correctness issues that must be addressed before production:

1. **Password encryption is reversible (Fernet/AES), not hashed.** This is pre-existing but is amplified by the session-duration change (24h tokens + reversible passwords + any DB leak = full credential compromise).
2. **Scope enforcement is client-trusting.** `require_permission` reads `entity`/`floor` from query params, so a non-admin user can request a scope-broadening query param to evade `allowed_entities` checks (see HIGH-1).
3. **Deactivated-user rejection relies on a fragile SQL column-name collision** in `validate_session` (CRITICAL-2). Admin edits that set `is_active=False` via `PUT /users/{id}` do NOT invalidate sessions — only `DELETE /users/{id}` does.
4. **Session tokens are not rotated on privilege change, password change invalidates only "other sessions" (actually all), and login does not invalidate prior sessions** — enabling classic session fixation / zombie sessions.
5. **`uuid4()` tokens are acceptable entropy (122 bits)** but are neither HMAC-signed nor marked `secure`, and there is no cleanup/rotation strategy.
6. **24h session duration** is defensible for a factory-floor ERP with shift workers, but combined with the issues above it widens the exploit window materially.

### Verdict

**CHANGES_REQUIRED_BEFORE_PROD.** Two CRITICAL and five HIGH findings block production deployment. The session duration change itself is a business decision (I recommend accepting it once the other issues are fixed), but do not ship the middleware as-is.

### Prioritized Remediation (fix in this order)

| # | Severity | Action |
|---|----------|--------|
| 1 | CRITICAL | Stop reading `entity`/`floor` from **query params** in `require_permission`; derive from the authenticated `AuthUser.entity` or from a trusted path/body field validated separately. (HIGH-1) |
| 2 | CRITICAL | Fix `validate_session` to explicitly select `s.is_active AS session_active, u.is_active AS user_active` and check both — remove reliance on column-name collision. (CRITICAL-2) |
| 3 | HIGH | Invalidate sessions in `PUT /users/{id}` when `is_active` transitions to false OR when `role_id` changes (privilege change must rotate session). |
| 4 | HIGH | Invalidate all prior sessions for a user on login (or at least offer a config flag); currently each login creates a new session but old ones remain valid for 24h. |
| 5 | HIGH | Replace password encryption (Fernet) with a password hash (Argon2id or bcrypt). `verify_password` does `==` comparison on decrypted plaintext — constant-time is moot because the underlying primitive is wrong. |
| 6 | HIGH | Do not log `phone` + `full_name` together at INFO on login (PII exposure in log aggregators). |
| 7 | MEDIUM | Add rate limiting on `/auth/login` to mitigate credential stuffing (now more attractive with 24h sessions). |
| 8 | MEDIUM | Add a `role_version` or cache-busting mechanism if a permission cache is introduced later. Current code has no cache (good), but document that constraint. |
| 9 | MEDIUM | `last_activity_at` update is a blind `UPDATE` with no concurrency guard — acceptable but note the race for telemetry. |
| 10 | LOW | Lock down `_extract_user` imports and miscellaneous quality items. |

---

## Critical Issues

### CRITICAL-1: Scope (entity/floor) is read from client-controlled query params

**File:** `app/modules/auth/middleware.py:99-101`
**Category:** security / authorization
**Severity:** CRITICAL

**Issue.** In `require_permission._dependency`:
```python
entity = request.query_params.get("entity") or user.entity or None
floor = request.query_params.get("floor") or None
```

`entity` and `floor` are pulled from the request **query string**, then passed to `check_permission(...)`. Combined with `permission_service.check_permission`, the scope check is:
```python
if entity and result['allowed_entities']:
    if entity not in result['allowed_entities']:
        continue  # Try broader permission
```
and — critically — if `allowed_entities` is `None` or `[]`, **no scope check runs at all** (falsy), so the permission is granted regardless of entity.

**Attack scenario.**
- A `floor_manager` at entity `cdpl` with `allowed_entities = ['cdpl']` has permission `(production, inventory, move, create)`.
- They call `POST /api/v1/production/inventory/move?entity=cfpl&floor=any` with a body that actually targets `cfpl` resources.
- `require_permission` reads `entity=cfpl` from query params, checks it against `allowed_entities=['cdpl']` → fails that row, loops to try a broader permission.
- If there is *any* broader permission row with `allowed_entities IS NULL` (the common case for seeded permissions), the request is authorized.
- Even simpler: the **endpoint handler does not have to use the query-param entity** — it uses the body — so the authorization check and the executed action are decoupled. The user passes `?entity=cdpl` (authorized) but the body says `cfpl` (executed).

This is a **confused-deputy / parameter-tampering** flaw. The authorization scope and the action scope must be the same value, and that value must come from a server-trusted source (the authenticated user's entity, or a resource lookup by ID).

**Fix.**
1. Remove `request.query_params.get("entity")` and `.get("floor")` from middleware. Use `user.entity` only.
2. For endpoints that legitimately act across entities (admin tooling), require `is_admin` explicitly — do not allow role permissions to span entities via query params.
3. For floor-scoped operations, derive `floor` from the resource being accessed (e.g., look up `job_card.floor_id` inside the handler and pass to a secondary scope check), not from the query string.
4. Document in `check_permission` that `allowed_entities = NULL` means "unrestricted" and only seed `NULL` for roles where that is intended (most non-admin roles should have an explicit array).

```python
# middleware.py – minimal fix
entity = user.entity or None   # NEVER from request
# floor must be validated per-handler against resource, not via query string
```

---

### CRITICAL-2: `validate_session` does not reliably reject deactivated users (fragile column-name collision)

**File:** `app/modules/auth/services/auth_service.py:106-131`
**Category:** security / authentication
**Severity:** CRITICAL

**Issue.** The query:
```sql
SELECT s.*, u.user_id, u.phone, u.full_name, u.email, u.entity, u.role_id, u.is_active,
       r.role_name, r.is_admin
FROM auth_session s
JOIN auth_user u ON s.user_id = u.user_id
LEFT JOIN auth_role r ON u.role_id = r.role_id
WHERE s.token = $1 AND s.is_active = TRUE AND s.expires_at > NOW()
```

The `WHERE` clause only filters **session** `is_active`. It does NOT filter `auth_user.is_active`. The Python check at line 124:
```python
if not session['is_active']:
    return None
```
*appears* to defend against this, but `session['is_active']` is ambiguous — both `auth_session.is_active` and `auth_user.is_active` are selected. In asyncpg, the last occurrence wins in the record, so this works "by accident" today (line 124 actually checks `u.is_active`).

Consequences:
- **If the SELECT column order is ever re-ordered** (e.g., `s.*` expanded, or `u.is_active` moved above `s.*`), the check silently becomes a session-active check (which WHERE already enforced), and deactivated users with live sessions slip through.
- **`PUT /users/{user_id}` with `is_active=False`** (auth/router.py:174-199) deactivates the user but **does NOT invalidate sessions** — the only reason this doesn't immediately grant access to a "deactivated" user is the fragile column collision. Only `DELETE /users/{id}` also closes sessions.
- Any maintenance engineer who "cleans up" the SELECT to be more explicit will reintroduce this vulnerability.

**Attack scenario.** Admin decides a user is no longer allowed and edits them via `PUT /auth/users/{id}` with `{"is_active": false}`. The user's 24h session token remains valid in the DB. The session passes the WHERE (session still active). Line 124 today rejects via the user-column collision; a future refactor removes that safety net, and the deactivated user has a live 24-hour token.

**Fix.**
```python
session = await conn.fetchrow(
    """
    SELECT
        s.session_id,
        s.user_id,
        s.is_active        AS session_active,
        s.expires_at,
        u.phone, u.full_name, u.email, u.entity, u.role_id,
        u.is_active        AS user_active,
        r.role_name, r.is_admin
    FROM auth_session s
    JOIN auth_user u ON s.user_id = u.user_id
    LEFT JOIN auth_role r ON u.role_id = r.role_id
    WHERE s.token = $1
      AND s.is_active = TRUE
      AND u.is_active = TRUE
      AND s.expires_at > NOW()
    """,
    token,
)
if not session:
    return None
```
Push the `u.is_active = TRUE` check into SQL; alias columns to avoid collisions.

**Also fix** `app/modules/auth/router.py:174-199`: when `is_active` transitions to `False` or `role_id` changes in `edit_user`, invalidate all that user's sessions in the same transaction.

---

## High Issues

### HIGH-1: Role / privilege change does not rotate session token

**File:** `app/modules/auth/router.py:174-199` (scope includes middleware contract)
**Category:** security / session-management
**Severity:** HIGH

**Issue.** `PUT /auth/users/{user_id}` can change `role_id`. An attacker (or insider) who obtains a low-privilege user's token, then compromises an admin account to change that user's role to something higher, retains the original 24h token — no rotation happens. Conversely, *demoting* a user leaves their elevated token valid for up to 24h after demotion.

**Attack scenario / failure mode.** Classic session-fixation-on-privilege-change. Even without a compromised admin, a user promoted from `viewer` → `planner` and subsequently demoted retains the elevated-session cached permissions in frontends / WS JWT tokens issued during the elevation window.

**Fix.** In `edit_user`, if `role_id` or `is_active` is in the change set:
```python
await conn.execute("UPDATE auth_session SET is_active = FALSE WHERE user_id = $1", user_id)
```
Require users to re-login after role change.

---

### HIGH-2: Password encryption uses reversible Fernet, not a password hash

**File:** `app/modules/auth/services/auth_service.py:33-45`
**Category:** security / credential-storage
**Severity:** HIGH

**Issue.** `encrypt_password` uses Fernet (AES-128-CBC + HMAC). `verify_password` decrypts the stored ciphertext and does `decrypted == plain`. This means:
1. **Any DB dump + encryption key leak** yields every user's plaintext password. The key is loaded from `AUTH_ENCRYPTION_KEY` env / `.env` file — a `.env` leak alone compromises every account.
2. There is no per-user salt; identical passwords produce different ciphertexts only because Fernet includes an IV, which is fine for confidentiality but not the right primitive for password verification.
3. `decrypt_password(encrypted) == plain` is not constant-time; `==` on Python strings short-circuits. In practice this doesn't matter much because the attacker needs to guess the full plaintext, but combined with #1, it's another flag.

This is **pre-existing** (not introduced by the middleware PR) but is scoped for review because the 24h session change amplifies the blast radius: a leaked DB → 24h to lateral-move across accounts before passwords can be rotated.

**Fix.** Migrate to `argon2-cffi` (recommended) or `bcrypt`:
```python
from argon2 import PasswordHasher
_ph = PasswordHasher()

def hash_password(plain: str) -> str:
    return _ph.hash(plain)

def verify_password(plain: str, stored_hash: str) -> bool:
    try:
        _ph.verify(stored_hash, plain)
        return True
    except Exception:
        return False
```
Plan a migration: on next successful login, re-hash with Argon2 if the stored value is still Fernet. Remove `decrypt_password` entirely — you should never be able to recover a plaintext password.

---

### HIGH-3: Login does not invalidate prior active sessions; 24h duration compounds this

**File:** `app/modules/auth/services/auth_service.py:48-103`
**Category:** security / session-management
**Severity:** HIGH

**Issue.** Each call to `login(...)` inserts a new `auth_session` row but does not deactivate the user's prior active sessions. Combined with 24h expiry, a user who logs in from a new device leaves old tokens live on old devices for up to 24 hours.

Scenarios:
- User loses a phone; logs in on a new phone; the old phone's token remains valid for 24h.
- Credential stuffing attacker and legitimate user each hold a valid token; legitimate user's re-login does not evict the attacker.
- No per-user session cap → unbounded growth of `auth_session` rows.

**Fix (choose one or both).**
1. **Default: single-active-session per user.** Deactivate prior sessions at login:
   ```python
   await conn.execute(
       "UPDATE auth_session SET is_active = FALSE WHERE user_id = $1 AND is_active = TRUE",
       user['user_id'],
   )
   ```
   This is the safest default for a factory ERP with shared-device scenarios.
2. **Or: sliding cap + explicit "log out everywhere" endpoint.** Keep up to N sessions, evict oldest, expose `/auth/logout-all`.

Also add a periodic job (or trigger) to purge expired sessions (`expires_at < NOW() - interval '7 days'`) to bound table growth.

---

### HIGH-4: PII (phone + full_name) logged at INFO on every login

**File:** `app/modules/auth/services/auth_service.py:84`
**Category:** security / logging / privacy
**Severity:** HIGH (regulatory — India PDPB/DPDP Act)

**Issue.**
```python
logger.info("Login: user=%s phone=%s role=%s", user['full_name'], phone, role['role_name'] if role else 'none')
```

Full name + phone number is PII under DPDP 2023 (India) and most other regimes. Logged at INFO, it lands in every log aggregator, stdout, Docker logs, cloud log groups — with 100s of logins/day producing a growing PII trove indefinitely.

Separately, good news: **no password or token is logged** anywhere in middleware or service — so the "secrets in logs" concern from the review prompt is negative on that axis.

**Fix.**
```python
logger.info("Login: user_id=%s role=%s", user['user_id'], role['role_name'] if role else 'none')
```
Log surrogate IDs, not PII. If phone is needed for abuse tracking, hash or truncate it (`XXXXXX4207`).

---

### HIGH-5: `require_permission` falls back to broader permission rows silently, bypassing scope restrictions

**File:** `app/modules/auth/services/permission_service.py:35-63` (consumed by middleware.py:107-111)
**Category:** security / authorization
**Severity:** HIGH

**Issue.** In `check_permission`, when an `allowed_entities` mismatch occurs (`continue`), the loop tries progressively broader permission rows (`(module, sub_module, sub_sub_module, action)` → `(module, sub_module, None, action)` → `(module, None, None, action)`). If a broader row happens to grant the action without scope restrictions, the narrower scoped row is effectively ignored.

**Failure mode.** Suppose a role has:
- `(production, inventory, move, create)` with `allowed_entities = ['cdpl']` (narrow, scoped)
- `(production, None, None, create)` with `allowed_entities = NULL` (broad, unrestricted — perhaps inherited from a legacy seed)

A user calls `move` with `entity=cfpl`. The narrow row rejects (scope mismatch). The loop continues, finds the broad row, allows. The scope restriction on the narrow row was pointless.

**Fix.** On a scope mismatch, return `False` immediately — do NOT fall through to broader rows for the same action:
```python
if result:
    if entity and result['allowed_entities']:
        if entity not in result['allowed_entities']:
            return False   # was: continue
    if warehouse and result['allowed_warehouses']:
        if warehouse not in result['allowed_warehouses']:
            return False
    if floor and result['allowed_floors']:
        if floor not in result['allowed_floors']:
            return False
    return True
```
Falling through to broader rows is only correct if you assume broader rows are strictly less privileged, but `allowed_entities IS NULL` means "all entities", which is *more* privileged.

---

## Medium Issues

### MEDIUM-1: Session token has no rotation / regeneration primitive

**File:** `app/modules/auth/services/auth_service.py:64, 187-206`
**Category:** security / session-management
**Severity:** MEDIUM

**Issue.** There is no `rotate_token(session_id)` helper. `change_password` invalidates all sessions (good), but there's no path to rotate a token on, e.g., role change or suspicious-activity detection. `uuid4()` itself has 122 bits of entropy (fine), but rotation hygiene is missing.

**Fix.** Add a helper and call on role change:
```python
async def rotate_session_token(conn, session_id: int) -> str:
    new_token = str(uuid.uuid4())
    await conn.execute("UPDATE auth_session SET token = $1 WHERE session_id = $2",
                       new_token, session_id)
    return new_token
```

---

### MEDIUM-2: `last_activity_at` UPDATE on every request (perf + race)

**File:** `app/modules/auth/services/auth_service.py:128-131`
**Category:** performance / concurrency
**Severity:** MEDIUM

**Issue.** Every authenticated request does:
```python
await conn.execute("UPDATE auth_session SET last_activity_at = NOW() WHERE session_id = $1", ...)
```
— a write-on-read inside the middleware. For a busy user with 100s of reqs/minute:
- DB write amplification; `auth_session` becomes a hot row.
- Not transactional with the read; two concurrent reads both update `last_activity_at`, last-writer wins. No correctness issue (idempotent), but contention on the row.
- No index issues here (UPDATE-by-PK), but bloats WAL.

**Fix.** Either:
1. Rate-limit the update to once per N minutes: check `NOW() - last_activity_at > interval '60s'` in SQL before updating.
2. Batch in-memory and flush every 30s (introduces infra complexity).

```sql
UPDATE auth_session
SET last_activity_at = NOW()
WHERE session_id = $1 AND NOW() - last_activity_at > interval '1 minute'
```

---

### MEDIUM-3: Session duration 3h → 24h: trade-off analysis (business decision)

**File:** `app/modules/auth/services/auth_service.py:12`
**Category:** design / security-posture
**Severity:** MEDIUM (informational, do not block on this alone)

**Trade-offs.**
| | 3h | 24h |
|---|----|-----|
| Shift-worker UX | Re-login mid-shift, annoying | Login once per shift / per day |
| Mobile apps (Android FCM) | Frequent refresh | Comfortable |
| Stolen-token exposure window | 3h | 24h |
| Deactivated-user access window | 3h | 24h (mitigates only with CRITICAL-2 fix + edit_user session invalidation) |
| Compliance (generic ERP norms) | Tight | Common industry default |

**Recommendation.** 24h is acceptable for a shift-based factory ERP **provided** (a) HIGH-3 is fixed (no zombie sessions), (b) CRITICAL-2 is fixed (deactivation takes effect immediately), (c) HIGH-1 is fixed (role change rotates session). Without those, 24h widens every exploit window 8×.

Consider a **sliding window with absolute cap**: 2h idle timeout + 24h absolute max (`created_at + 24h`). Update `last_activity_at` on use; reject if `NOW() - last_activity_at > 2h` OR `expires_at < NOW()`.

---

### MEDIUM-4: `validate_session` creates a write on a session that is already confirmed active — DOS via invalid tokens does not hit this path, but valid-token floods do

**File:** `app/modules/auth/services/auth_service.py:109-131`
**Category:** performance / availability
**Severity:** MEDIUM

**Issue.** Every middleware call does one SELECT + one UPDATE. A WS-heavy floor controller with a connection-per-machine could multiply this significantly. Not a correctness issue, but worth flagging.

**Fix.** See MEDIUM-2; additionally cache session metadata in Redis with a 30s TTL keyed by token hash (defer until measured).

---

### MEDIUM-5: No rate limit on `/auth/login`

**File:** `app/modules/auth/router.py:82-94`
**Category:** security / availability
**Severity:** MEDIUM

**Issue.** Login endpoint has no rate limiting. With 24h sessions, successful credential stuffing yields long-lived tokens. `verify_password` uses Fernet decrypt which is O(~ms) — tractable for brute-force at scale.

**Fix.** Add `slowapi` or equivalent: max 5 failed attempts per phone per 10 min; exponential backoff; optional account lockout after 20 failures in 1h.

---

### MEDIUM-6: `AuthUser` exposes `phone` and `email` to all handlers unnecessarily

**File:** `app/modules/auth/middleware.py:30-41`
**Category:** design / least-privilege
**Severity:** MEDIUM

**Issue.** Every endpoint handler receives a `user` with PII fields (`phone`, `email`, `full_name`). Handlers like `/job-cards/list` don't need PII. Risk: PII leaks into logs/responses through careless `user.__dict__` or `model_dump()` calls.

**Fix.** Minimal context object:
```python
class AuthUser:
    def __init__(self, user_id, entity, role_id, role_name, is_admin):
        ...
    # phone/email/full_name available via a separate fetch when needed
```
Or make those fields private (`_phone`) with a getter that logs access.

---

## Low Issues

### LOW-1: Inline imports inside request path

**File:** `app/modules/auth/middleware.py:52, 104`
**Category:** quality / performance
**Severity:** LOW

**Issue.**
```python
from app.modules.auth.services.auth_service import validate_session
...
from app.modules.auth.services.permission_service import check_permission
```
These imports happen on every request. Python caches module imports, so the cost is a dict lookup per call — small, but makes call-graph analysis and static tools noisier. Likely done to avoid circular imports.

**Fix.** Move to module top unless a circular dependency exists (it doesn't appear to, based on the files reviewed).

---

### LOW-2: `session.get('role_name', '')` and `session.get('is_admin', False)` — defensive defaults that mask bugs

**File:** `app/modules/auth/middleware.py:67-68`
**Category:** quality / robustness
**Severity:** LOW

**Issue.** `validate_session` always returns a dict with `role_name` and `is_admin` keys (or `None`). The `.get(..., default)` implies a fallback when data is missing — but if `role_name` is genuinely missing, something is broken and should raise, not silently downgrade to `is_admin=False` (which *is* a safe failure mode — good — but silent).

**Fix.** Either:
```python
role_name=session['role_name'] or '',
is_admin=bool(session['is_admin']),
```
(fail loudly on missing key), or keep `.get` and log a warning when fallback triggers.

---

### LOW-3: Docstring mentions "user.floor is already validated" but there is no `AuthUser.floor` attribute

**File:** `app/modules/auth/middleware.py:15`
**Category:** quality / documentation
**Severity:** LOW

**Issue.** Docstring: `# user.entity, user.floor are already validated against allowed_entities/floors`. `AuthUser` has no `floor` attribute (line 32-41). Floors are read from query params, not from the user record.

**Fix.** Update docstring to match reality, or add a `floor` attribute if floor-binding is intended.

---

### LOW-4: `Optional` imported but unused

**File:** `app/modules/auth/middleware.py:20`
**Category:** quality / dead-code
**Severity:** LOW

**Issue.** `from typing import Optional` — `Optional` is not referenced anywhere in the file.

**Fix.** Remove the import.

---

## Out-of-Scope Observations (flagged but not findings)

- `_bearer = HTTPBearer(auto_error=False)` is the right choice — handles missing token consistently with `raise HTTPException(401)` inside `_extract_user` rather than FastAPI's auto-403.
- 401 vs 403 distinction is correct: missing/invalid session = 401, valid session but permission denied = 403. Good.
- `uuid.uuid4()` has 122 bits of entropy (`random.SystemRandom` under the hood on CPython 3.7+) — sufficient for session tokens. Not the issue; rotation and storage are.
- `async with conn.transaction()` wrapping in `router.login` is fine; no txn in `validate_session` which is also fine (read + advisory update, no atomicity requirement).
- `checkout_permission` via `conn.fetchrow` is parameterized — no SQL injection risk observed in the middleware code path.
- The `retry_delivery` handler in `app/webhooks/router.py` eagerly deserializes JSON from the DB and passes through — not in scope, but worth a follow-up review for the webhook module specifically.

---

_Reviewed: 2026-04-18_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
