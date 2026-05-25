---
phase: auth-rebuild
reviewed: 2026-05-05T00:00:00Z
pass: 2 (verification)
depth: deep
files_reviewed: 12
files_reviewed_list:
  - app/db/004_auth_rebuild.sql
  - app/config.py
  - requirements.txt
  - app/main.py
  - app/core/middleware/request_context.py
  - app/modules/auth/router.py
  - app/modules/auth/middleware.py
  - app/modules/auth/services/auth_service.py
  - app/modules/auth/services/jwt_service.py
  - app/modules/auth/services/password_rules.py
  - app/modules/auth/services/phone.py
  - app/modules/auth/services/rate_limiter.py
findings:
  closed: 14
  partial: 2
  open: 4
  new_critical: 1
  new_high: 1
  new_medium: 2
status: regression_found
---

# Auth Rebuild — Verification Pass 2

**Reviewed:** 2026-05-05 (second pass)
**Depth:** deep — focus on the highest-risk fix areas (CR-01 rotation, CR-03 timing, HI-02 atomicity, HI-05 cache, HI-06 envelope, MD-08 logout, middleware ordering)
**Status:** REGRESSION FOUND — one Critical, one High, two Medium new issues introduced by the fixes; CR-01 is functionally OPEN due to the regression.

## TL;DR

Most fixes are correctly applied. **The CR-01 rewrite has a fatal regression**: the chain-wide revoke inside the rotation transaction rolls back when the function subsequently raises, because asyncpg's `Transaction.__aexit__` rolls back on exception. The two reuse-detect branches (race-loss + unknown-jti-with-active-sibling) UPDATE the chain inside the same transaction that then raises `AuthError`, so the UPDATE is rolled back and the attacker's reuse leaves the chain *not* revoked. This defeats the entire reuse-detection mechanism. See **NEW-CR-01** below — it must be fixed before ship.

CR-02, CR-03, HI-02, HI-03, HI-04, HI-05, HI-06, HI-07, MD-01, MD-04, MD-05, MD-08, NI-05 are correctly closed. HI-01 is partially closed (the active-sibling defence is implemented but is also affected by the rollback bug). MD-02, MD-03, MD-06, MD-07, MD-09, LO-01..LO-07 were noted as deferred/spec-pending in the original — most are still applicable but appropriately scoped.

---

## Findings closed (each previous CR/HI/MD verified against current code)

### Criticals

- **CR-01: Refresh-rotation atomicity** — **PARTIAL (OPEN due to regression).**
  The guarded `UPDATE … WHERE jti=$1 AND rotated_at IS NULL AND revoked_at IS NULL RETURNING …` at `auth_service.py:346-356` correctly serializes concurrent `/refresh` calls via the row lock. Two concurrent calls now race for the UPDATE, the loser sees `rotated=None`, re-fetches, and is correctly classified as reuse. The locking primitive itself is correct.
  **However**: the chain-wide revoke at lines 415-422 (and the `unknown_jti` revoke at lines 386-393) happens inside `async with conn.transaction():` and is followed by `raise AuthError(...)`. asyncpg's `Transaction.__aexit__` rolls back on any exception (verified against asyncpg 0.31 source). So the `UPDATE auth_refresh_token SET revoked_at = NOW() WHERE chain_root = $1` runs, then the raise propagates, then the transaction's `__aexit__` sees `extype != None` and issues `ROLLBACK`. Net effect: the row that was about to be revoked stays active and the attacker keeps refreshing. This is more dangerous than the original bug because it *appears* to detect reuse (the AuthError fires, the log line is written) while silently leaving the chain unrevoked. Filed as **NEW-CR-01** below.

- **CR-02: JWT secret dev fallback** — **CLOSED.**
  `jwt_service._secret()` (lines 49-72) now requires `s.APP_ENV == "dev"` before falling back to the AUTH_ENCRYPTION_KEY-derived secret, and `Settings._require_jwt_secret_in_prod` (config.py:43-52) refuses to construct Settings outside dev when `JWT_SECRET` is empty. APP_ENV is normalized to {dev, staging, prod} via `_normalise_env`. Defence-in-depth is genuine: even if Settings is bypassed (tests, scripts), `_secret()` re-checks. Good.

- **CR-03: Phone enumeration via timing** — **CLOSED.**
  `_DUMMY_BCRYPT_HASH` is precomputed at module import (auth_service.py:91), and `_equalise_login_timing(password)` is called before each pre-bcrypt raise:
    - not-found (line 195) ✓
    - suspended (line 203) ✓
    - disabled (line 206) ✓
    - locked (line 215) ✓
  Note: `_find_user_by_phone` at lines 121-127 still does up-to-3 sequential SELECTs through `lookup_keys`. For an unregistered phone, `lookup_keys()` returns `[normalized, bare-10-digit, raw]` (3 keys), so 3 round-trips before the not-found bcrypt. For a registered phone (first lookup hits), 1 round-trip. **This is a residual timing leak** of ~1-2 ms × N round-trips, much smaller than the bcrypt difference but still measurable over the network. Filed as **NEW-MD-01** below — not a blocker but worth noting.

### Highs

- **HI-01: Reuse defence on unknown jti** — **PARTIAL (defence implemented, but rollback regression nullifies it).**
  When the JWT verifies but no row exists, the code at lines 372-402 checks for an active sibling on the same `chain_root` + `user_id` and revokes the chain if found. The discrimination between `revoked_at` (lines 406-411) and `rotated_at` cases is also implemented with distinct log lines. **However**, the `UPDATE … SET revoked_at = NOW() WHERE chain_root = $1` at lines 386-393 is also inside the rotation transaction and is also rolled back when the subsequent `raise AuthError` propagates. Same root cause as NEW-CR-01.

- **HI-02: Failed-login counter atomicity** — **CLOSED.**
  `_record_failed_login` now wraps both writes in `async with conn.transaction()` (lines 305-324). The asyncpg semantic question (does `conn.transaction()` nest correctly with autocommit?) resolves cleanly: outside an enclosing transaction, `conn.transaction()` issues a fresh BEGIN; on `__aexit__` it COMMITs (no exception path here since the increment+lock are deterministic) and the connection returns to autocommit. The router-level comment at lines 62-66 also explicitly forbids wrapping the call in a transaction. Pattern is correct.

- **HI-03: `_extract_token` case-sensitivity** — **CLOSED.**
  router.py:249-255 splits on whitespace, lowercases the scheme, and trims. Handles `Bearer`, `bearer`, `BEARER`, leading/trailing whitespace. Returns `None` for empty token (the `or None` after `.strip()`).

- **HI-04: Dynamic SQL builder** — **CLOSED.**
  `_EDITABLE_USER_COLUMNS` and `_EDITABLE_PERMISSION_COLUMNS` (router.py:238-243) are module-level frozensets. The loop at line 350 iterates `body.model_fields_set` (only fields explicitly sent), gates each through the allowlist with `if field not in _EDITABLE_USER_COLUMNS: continue`, and quotes the identifier with `f'"{field}"'`. Same pattern at edit_permission. Defensive and clear.

- **HI-05: `request.state.user_dict` cache** — **CLOSED with caveats.**
  `_require_auth` at router.py:258-275 reads `request.state.user_dict` first and writes it on miss. **Cross-cutting check**: I greped for `request.state.user_dict` and `request.state.user` across the entire backend — only the auth router writes `user_dict`, and `request.state.user` is never used. No name collisions. The middleware's `_extract_user` (middleware.py:68-97) does NOT populate this cache — but it doesn't need to, because FastAPI's `Depends` deduplicates within a request. The legacy `_require_admin` calls (which bypass `Depends`) are the only path that benefits from the cache, and they DO call `_require_auth` first, so the cache wins on the second call within the same request. Net behaviour is correct.
  Caveat: any future endpoint that mixes `Depends(get_current_user)` + `Depends(_require_admin)` in the same request will end up with TWO `validate_session` calls (one from each path), since the JWT-aware path doesn't share the cache key. Not a bug today; flag for future maintainers in **NEW-MD-02** below.

- **HI-06: Admin create_user weak-password + unique-violation** — **CLOSED.**
  router.py:288-318 calls `password_rules.evaluate(body.password, norm_phone)` BEFORE `_create()` and raises `HTTPException(400, detail={...})` with the spec envelope. `http_exception_handler` (request_context.py:155-172) inspects `isinstance(detail, dict)` and pulls `error`, `message`, `details` out — confirmed at lines 158-162. The on-wire response will be:
  ```json
  {
    "error": "weak_password",
    "message": "Password does not meet strength requirements",
    "request_id": "...",
    "timestamp": "...",
    "details": {"rules": ["..."]}
  }
  ```
  Matches the spec. UniqueViolationError handling (lines 314-318) is correctly typed via `asyncpg.UniqueViolationError`.

- **HI-07: Phone-substring rule** — **CLOSED.**
  `password_rules.evaluate` at lines 60-93 normalizes the phone via lazy `from app.modules.auth.services.phone import normalize as _norm_phone`, derives both `phone_digits` (from normalized) and `raw_digits` (from the unnormalized input), and explicitly adds the bare-10-digit form when the normalized form is `91XXXXXXXXXX`. Test cases I mentally walked:
    - `phone="+919876543210", password="Welcome9876543210"` → `phone_digits="919876543210"`, candidates include suffix `"9876543210"` (length 10) AND the explicit bare-10 → matches → rule fails ✓
    - `phone="9876543210", password="9876543210abc"` → `phone_digits="919876543210"` (after normalize), bare-10 added → matches ✓
    - `phone="1234567" (7 digits)` → `range(7,7)` empty but `phone_digits` itself is added → exact-substring match works ✓
  The lazy import is safe (no circular import — `phone.py` has no auth_service deps) and the import resolves once per call. Negligible perf impact (Python caches `sys.modules`); no actionable concern.

### Mediums (verified)

- **MD-01: `_now_iso()` racy datetime calls** — **CLOSED.** request_context.py:88-92 uses a single `n = datetime.now(...)`.
- **MD-04: Security headers on error handlers** — **CLOSED.** `_error_headers()` at lines 118-124 unions `_SECURITY_HEADERS` with caller extras, with `X-Request-ID` placed last (also fixes LO-01).
- **MD-05: JWT `iss` requirement** — **CLOSED.** jwt_service.py:157 includes `"iss"` in `options={"require": [...]}`.
- **MD-08: Logout sub-check + idempotent silent-ignore** — **CLOSED.** `auth_service.logout(conn, *, refresh_jwt, user_id)` at lines 479-511 verifies `int(payload["sub"]) == user_id` and `return`s before the UPDATE on mismatch (line 502 → no UPDATE runs). Logs a warning. Router at lines 95-109 passes `user_id=user.user_id`. I greped `auth_service.logout` across the codebase — the only caller is the router. No legacy callers using the old positional signature. Cross-user attempt does NOT revoke (verified by reading the control flow: line 497 `if token_user_id != user_id: ... return` precedes the UPDATE at line 504).

### Nits

- **NI-05: 408 entry in `_DEFAULT_HTTP_CODES`** — **CLOSED.** request_context.py:142.

---

## New issues introduced by the fixes

### NEW-CR-01 (CRITICAL): Reuse-detect chain-revoke is rolled back by the enclosing transaction

**File:** `app/modules/auth/services/auth_service.py:345-431`

**Issue.** The CR-01 rewrite places both reuse-detect chain-revokes inside the rotation `async with conn.transaction()` block:

1. **Race-loss branch** (lines 413-431): UPDATE chain → `raise AuthError("token_reuse_detected")`.
2. **Unknown-jti-with-active-sibling branch** (lines 385-402): UPDATE chain → `raise AuthError("token_reuse_detected")`.

Both `raise` statements propagate through `async with conn.transaction()`'s `__aexit__`. asyncpg's `Transaction.__aexit__` (verified in source — `asyncpg/transaction.py`):

```python
async def __aexit__(self, extype, ex, tb):
    ...
    if extype is not None:
        await self.__rollback()
    else:
        await self.__commit()
```

So the chain-revoke UPDATE is rolled back. The attacker's reuse fires the AuthError (good) but the chain stays live (bad — they can refresh again). Worse, the original token row's `rotated_at = NOW()` from the guarded UPDATE at line 348 — wait, no, that one only fires on the happy path (`if not rotated:` is the failure branch, so `rotated` is `None`). So in the reuse-race case, **no** revoke and **no** rotation marker persists. The attacker can replay indefinitely.

In the unknown-jti branch, there's also no row to mark, so the only state change attempted (the chain revoke) is rolled back.

This is a regression from the original bug: pre-fix, two concurrent refreshes both succeeded silently. Post-fix, reuse is *detected and logged* but *not enforced*. Operators will see warnings in logs and assume the system is defending; it isn't.

**Fix.** The chain-revoke must commit independently of the AuthError raise. Options:

1. **Commit-then-raise pattern**: open a sub-transaction (savepoint via `async with conn.transaction():` nested) that the revoke runs in, exit it cleanly, *then* raise. asyncpg supports nested transactions as savepoints — but the savepoint also rolls back when you raise out of the outer.

2. **Raise AFTER the `async with` exits successfully**: refactor to set a `revoke_chain_then_raise` flag inside the with-block, exit the block, then perform the revoke in autocommit and raise. Simplest and most explicit:

```python
async with conn.transaction():
    rotated = await conn.fetchrow(...)
    if rotated:
        # happy path: insert child, etc.
        ...
        return result_payload  # exits with-block via return → COMMIT

    # failure path: classify but DO NOT raise yet
    post = await conn.fetchrow("SELECT rotated_at, revoked_at, chain_root FROM ...", jti)
    if post is None:
        # check sibling
        sibling = await conn.fetchrow("SELECT 1 FROM ... WHERE chain_root=$1 AND user_id=$2 AND revoked_at IS NULL LIMIT 1", chain_root, user_id)
        failure = ("unknown_with_sibling", chain_root) if sibling else ("unknown", None)
    elif post["revoked_at"] is not None:
        failure = ("revoked", None)
    else:
        failure = ("reuse_race", post["chain_root"])

# transaction has now COMMITTED (no exception raised inside)
# perform any chain-revoke in autocommit, then raise
if failure[0] in ("unknown_with_sibling", "reuse_race"):
    await conn.execute(
        "UPDATE auth_refresh_token SET revoked_at=NOW(), revoke_reason=$2 WHERE chain_root=$1::uuid AND revoked_at IS NULL",
        failure[1], _REVOKE_REUSE,
    )
    logger.warning("auth.refresh.reuse_detected ...")
    raise AuthError("token_reuse_detected", "...", 401)
elif failure[0] == "revoked":
    raise AuthError("invalid_refresh_token", "Refresh token revoked", 401)
elif failure[0] == "unknown":
    raise AuthError("invalid_refresh_token", "Refresh token unknown", 401)
```

3. Alternative: use a separate connection for the chain-revoke. Acquire from pool, run UPDATE, release, then raise. More code, no atomicity risk.

I recommend option 2 — it preserves the "everything in one connection" model and the rotation transaction still gives you the row-lock atomicity for the happy path. The only thing the failure path needs is a UPDATE + raise; it doesn't need the surrounding transaction.

Severity: **Critical**. The entire reuse-detection mechanism is silently disabled. The fix introduces a worse failure mode than the original bug (logs lie about defence). This must be addressed before ship.

---

### NEW-HI-01 (HIGH): `_record_failed_login` runs in its own transaction — but called from inside login's success transaction-free zone, which is correct, BUT the same pattern fails if any future caller wraps `login()` in a transaction

**File:** `app/modules/auth/services/auth_service.py:226-228, 297-324`

**Issue.** The fix wraps `_record_failed_login`'s two writes in `conn.transaction()`. Today this works because the surrounding context is autocommit. But if a future maintainer adds an enclosing transaction (e.g. wraps the login route in a transaction-decorator, or calls `login()` from inside `change_password` for a re-auth flow), the inner `conn.transaction()` becomes a savepoint (asyncpg supports nested as SAVEPOINT). The inner savepoint COMMITs to the outer, then `raise AuthError("invalid_credentials")` propagates out and rolls back the OUTER transaction — including the failed-login counter increment. Lockout is then defeated.

The router comment at lines 62-66 forbids wrapping, which is good documentation. But the service-layer code provides no defence. The same root cause as NEW-CR-01: relying on the caller to manage transactions correctly is fragile.

**Fix.** Either (a) explicitly assert no enclosing transaction inside `_record_failed_login` (asyncpg exposes `conn.is_in_transaction()`), and raise a clear error if the caller violates the contract; or (b) acquire a fresh connection from the pool just for the counter UPDATE so it's truly independent of the outer connection's transaction state. (a) is cheap and catches the regression at runtime; (b) is more invasive but fully decouples.

Minimum: add `assert not conn.is_in_transaction(), "_record_failed_login must run outside any transaction"` at the top of the function. The error message will save the next person hours of debugging.

Severity: **High**. Latent footgun in security-critical code.

---

### NEW-MD-01 (MEDIUM): `_find_user_by_phone` does N sequential SELECTs — residual timing leak after CR-03 fix

**File:** `app/modules/auth/services/auth_service.py:121-127`

**Issue.** CR-03 closed the bcrypt-cost timing leak (~250 ms), but `_find_user_by_phone` still does up to 3 sequential round-trips through `lookup_keys()`. For a registered user (first key hits), one round-trip; for an unregistered phone, three round-trips. At ~1-2 ms each from the app server to PG (LAN) or 5-10 ms (cross-AZ), that's a 5-30 ms wall-time difference between "registered (early hit)" and "not registered (3 misses)". Smaller than bcrypt by an order of magnitude, but still measurable with statistical timing analysis over many requests.

**Fix.** Single `WHERE phone = ANY($1::text[]) ORDER BY array_position($1::text[], phone) LIMIT 1` query (originally suggested as LO-06 in the prior review):

```python
async def _find_user_by_phone(conn, raw_phone: str) -> dict | None:
    keys = lookup_keys(raw_phone)
    if not keys:
        return None
    row = await conn.fetchrow(
        """
        SELECT * FROM auth_user
         WHERE phone = ANY($1::text[])
         ORDER BY array_position($1::text[], phone)
         LIMIT 1
        """,
        keys,
    )
    return dict(row) if row else None
```

Single round-trip → constant timing for all branches. This was MD/LO before CR-03; now with CR-03 in place this is the next-largest enumeration vector.

---

### NEW-MD-02 (MEDIUM): `request.state.user_dict` cache key is not shared with the JWT-aware `_extract_user` path

**File:** `app/modules/auth/middleware.py:68-97`; `app/modules/auth/router.py:258-275`

**Issue.** The HI-05 fix caches the user-dict on `request.state.user_dict` from `_require_auth`. But `_extract_user` (the new spec path used by `Depends(get_current_user)`) does NOT read or write this cache. Today there's no double-call because:
- Spec endpoints use `Depends(get_current_user)` → FastAPI dedupes within the request.
- Legacy admin endpoints use `_require_admin` → which calls `_require_auth` → cache hit on second call.

**But** any new endpoint that mixes both paths (e.g. a `Depends(get_current_user)` on the route + an admin helper that internally calls `_require_admin`) will trigger TWO `validate_session` round-trips per request because the two paths use different cache keys (or rather, one uses the cache and the other doesn't).

**Fix.** Make `_extract_user` read/write the same key:

```python
async def _extract_user(request, credentials):
    cached = getattr(request.state, "user_dict", None)
    if cached:
        return AuthUser(**_to_authuser_kwargs(cached))
    if not credentials:
        raise AuthError(...)
    ...
    request.state.user_dict = session
    return AuthUser(...)
```

Severity: **Medium** — not a bug today, but the cache is half-finished and the perf benefit is partially defeated.

---

## Mediums still applicable

These were mostly noted as deferred / spec-pending in the original review. Spot-checked against current code:

- **MD-02: `RequestContextMiddleware` unhandled-exception path renders an envelope that bypasses the registered handlers.** Still applicable. Code at request_context.py:69-77 catches `Exception` in the middleware and renders its own envelope. As noted in the original review this is correct as a last-resort but the comment is misleading. **Worth tightening the comment.** Code itself is fine.

- **MD-03: `details: {}` always emitted.** Still applicable — `_envelope` at line 95-102 always emits `details`. Spec is silent on whether to omit when empty. No change needed unless API consumer asks.

- **MD-06: PyJWT 1.x vs 2.x return type.** Now hardened: requirements.txt:17 pins `PyJWT[crypto]>=2.10.1,<3.0`. The upper bound prevents a 3.x surprise; the lower bound stays in 2.x. **CLOSED via dependency pin.**

- **MD-07: `me()` semantic.** The fix took Option A from my recommendation (auth_service.py:551-603) — `entities`/`warehouses`/`floors` come from `auth_user` user-level fields only, with a docstring explaining the deliberate choice. `floors` is intentionally `[]` until the schema adds a user-level column. **CLOSED.** This is a clean resolution; the docstring at lines 552-559 captures the rationale.

- **MD-09: Migration cost notes.** Now documented in the SQL header (004_auth_rebuild.sql:14-30). **CLOSED via documentation.**

## Lows / Nits status (spot-check)

- **LO-01:** CLOSED (X-Request-ID placed last in `_error_headers`).
- **LO-02:** Still applicable — `_blocklist()` LRU survives uvicorn reload. Acceptable.
- **LO-07:** CLOSED — `_get_cipher` is now `@lru_cache(maxsize=1)` (auth_service.py:48-66).
- **LO-04, LO-03, LO-05, LO-06:** Still applicable as noted in original. LO-06 is now subsumed by NEW-MD-01 (timing as well as perf).
- **NI-01..NI-04:** Cosmetic, unchanged.

## Cross-cutting verifications

- **Middleware order.** main.py:74-81 now adds CORS first then `request_context.install(app)` last. Comment at lines 69-73 explains: last-added is outermost. So the runtime stack is RequestContext (outermost) → CORS → routes. CORS preflight OPTIONS hits CORSMiddleware *after* RequestContext sees it, so:
  - Browser sends OPTIONS → RequestContext.dispatch fires, generates `rid`, sets `request.state.request_id` → calls next → CORS handles the preflight, returns a 200 with CORS headers (no inner route runs) → RequestContext's `dispatch` resumes, adds `X-Request-ID` and security headers → response sent.
  - **Verified correct**: preflight responses now carry X-Request-ID. The reordering achieves the stated goal.

- **`evaluate(password, phone)` lazy import.** `from app.modules.auth.services.phone import normalize as _norm_phone` inside `evaluate` (password_rules.py:65). No circular import (phone.py imports nothing from auth_service). The import resolves via `sys.modules` after first call (Python's module cache), so per-call cost is a dict lookup + name binding (sub-microsecond). Negligible.

- **Logout cross-user silent-ignore.** Verified the control flow at auth_service.py:497-511 — `if token_user_id != user_id: ... return` precedes the UPDATE. The UPDATE only runs after the equality check passes. No accidental revoke.

---

_Reviewed: 2026-05-05_
_Reviewer: Claude (gsd-code-reviewer, second pass)_
_Depth: deep_
