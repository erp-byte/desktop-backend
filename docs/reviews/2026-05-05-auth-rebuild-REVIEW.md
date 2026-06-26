---
phase: auth-rebuild
reviewed: 2026-05-05T00:00:00Z
depth: deep
files_reviewed: 12
files_reviewed_list:
  - app/db/004_auth_rebuild.sql
  - app/core/middleware/request_context.py
  - app/modules/auth/schemas.py
  - app/modules/auth/services/jwt_service.py
  - app/modules/auth/services/password_rules.py
  - app/modules/auth/services/phone.py
  - app/modules/auth/services/rate_limiter.py
  - app/modules/auth/services/auth_service.py
  - app/modules/auth/middleware.py
  - app/modules/auth/router.py
  - app/main.py
  - app/config.py
findings:
  critical: 3
  high: 7
  medium: 9
  low: 7
  nit: 5
  total: 31
status: issues_found
---

# Auth Rebuild — Code Review Report

**Reviewed:** 2026-05-05
**Depth:** deep (cross-file: jwt_service ↔ auth_service ↔ middleware ↔ router; SQL contract verification)
**Files Reviewed:** 12
**Status:** issues_found — 3 Critical, 7 High, 9 Medium, 7 Low, 5 Nit

## Summary

The rebuild is well-structured and the contract maps cleanly to the spec — refresh-token rotation, chain-root revocation, lockout, and a clean JSON envelope are all in the right shape. However, the implementation has **three correctness-or-security defects that must be fixed before this ships**:

1. The refresh-rotation transaction (review concern #1) does **not** serialize correctly under READ COMMITTED. Two concurrent `/refresh` calls with the same token can both succeed and produce two divergent valid children, defeating reuse detection.
2. The JWT-secret dev fallback (concern #4) silently activates in production when `JWT_SECRET` is unset and `AUTH_ENCRYPTION_KEY` is set — which is exactly the production deployment pattern. This means production servers may be signing tokens with a derived key without the operator realizing.
3. Phone enumeration (concern #5) is trivially observable: the "user not found" branch returns in microseconds while the bcrypt-verify branch costs ~250 ms at rounds=12. An attacker can enumerate registered phones from latency alone.

The other High-severity findings cluster around (a) lockout-counter atomicity guarantees that depend on undocumented asyncpg behaviour, (b) reuse-detection blast-radius on a token whose row was never inserted, (c) a dynamic SQL builder in `edit_user` / `edit_permission` that filters fields against a hardcoded allowlist (safe today, but fragile), and (d) `_extract_token` accepting case-sensitive `Bearer ` only.

The migration is mostly safe but the `ADD COLUMN ... NOT NULL DEFAULT` on `auth_user` is **only** instant-metadata in PG ≥ 11 with non-volatile defaults; verify your prod PG version explicitly. The full set of findings follows.

---

## Critical Issues

### CR-01: Refresh-token rotation is not atomic — concurrent /refresh races leak two valid children

**File:** `app/modules/auth/services/auth_service.py:285-347` (function `refresh`)
**Concern referenced:** #1

**Issue.** Under PostgreSQL's default READ COMMITTED isolation, the rotation flow is:

```
T1: SELECT * FROM auth_refresh_token WHERE jti = $1   -- sees rotated_at = NULL
T2: SELECT * FROM auth_refresh_token WHERE jti = $1   -- sees rotated_at = NULL
T1: BEGIN
T1:   UPDATE auth_refresh_token SET rotated_at = NOW() WHERE jti = $1   -- locks row
T1:   INSERT INTO auth_refresh_token (jti, parent_jti=$1, ...) VALUES (...)
T1: COMMIT
T2: BEGIN
T2:   UPDATE auth_refresh_token SET rotated_at = NOW() WHERE jti = $1   -- BLOCKS, then re-reads, then succeeds
T2:   INSERT INTO auth_refresh_token (jti, parent_jti=$1, ...) VALUES (...)
T2: COMMIT
```

Both T1 and T2 succeed. Two valid children are now in the chain, both with the same `parent_jti`. The whole point of rotation+reuse-detection — that exactly one rotation can succeed per token — is defeated. An attacker who races a legitimate refresh wins quietly with no reuse signal raised.

The `WHERE jti = $1` predicate on the UPDATE re-evaluates after the row lock is acquired (PG's `EvalPlanQual` re-check), but the predicate doesn't include `rotated_at IS NULL`, so the re-check passes either way. The earlier in-Python `if row["rotated_at"] is not None:` guard was based on a stale READ COMMITTED snapshot.

**Fix.** Make the UPDATE itself the atomicity primitive — guard with `rotated_at IS NULL`, and reuse-detect inside the transaction if the UPDATE matched zero rows:

```python
async with conn.transaction():
    rotated = await conn.fetchrow(
        """
        UPDATE auth_refresh_token
           SET rotated_at = NOW()
         WHERE jti = $1::uuid
           AND rotated_at IS NULL
           AND revoked_at IS NULL
        RETURNING jti, chain_root, user_id
        """,
        jti,
    )
    if not rotated:
        # Either already rotated (reuse) or already revoked. Re-fetch to disambiguate.
        post = await conn.fetchrow(
            "SELECT rotated_at, revoked_at, chain_root FROM auth_refresh_token WHERE jti = $1::uuid",
            jti,
        )
        if post is None:
            raise AuthError("invalid_refresh_token", "Refresh token unknown", 401)
        if post["revoked_at"] is not None:
            raise AuthError("invalid_refresh_token", "Refresh token revoked", 401)
        # rotated_at became non-NULL between our SELECT and UPDATE → reuse race
        await conn.execute(
            """UPDATE auth_refresh_token
                  SET revoked_at = NOW(), revoke_reason = $2
                WHERE chain_root = $1::uuid AND revoked_at IS NULL""",
            post["chain_root"], _REVOKE_REUSE,
        )
        raise AuthError("token_reuse_detected", "...", 401)
    # …then INSERT the new child as before
```

The conditional `WHERE rotated_at IS NULL` makes the UPDATE the locking-point. The losing concurrent transaction's UPDATE will return zero rows (after the row lock releases and the predicate re-evaluates against the now-rotated row), and we correctly classify it as reuse.

Severity rationale: this is a quiet correctness bug in a security-critical primitive. Failure mode is silent token duplication.

---

### CR-02: JWT_SECRET dev fallback silently activates in production

**File:** `app/modules/auth/services/jwt_service.py:45-55`
**Concern referenced:** #4

**Issue.** `_secret()` falls back to `f"dev-jwt::{AUTH_ENCRYPTION_KEY}"` whenever `JWT_SECRET` is empty. There is no environment gate. In production, `AUTH_ENCRYPTION_KEY` is virtually guaranteed to be set (it's required for the legacy Fernet path), so a forgotten `JWT_SECRET` env-var produces a working but **derived** signing key — the operator gets no error, no warning, and silently runs with a key that's:

- known to anyone who has read access to the legacy secret, and
- cross-derivable: anyone who learns `AUTH_ENCRYPTION_KEY` (e.g. via a logged exception, a backup, the `.env` files that `_get_cipher()` happily reads at lines 49-56) can mint arbitrary access + refresh tokens.

The docstring claims "in prod JWT_SECRET must be set explicitly (validated at app boot)" — but the code does no boot-time validation. There is no `Settings` validator that rejects an empty `JWT_SECRET` outside dev.

**Fix.** Add an explicit env-mode setting (e.g. `APP_ENV: str = "dev"`) and refuse the fallback unless dev:

```python
def _secret(settings: Settings | None = None) -> str:
    s = settings or Settings()
    if s.JWT_SECRET:
        return s.JWT_SECRET
    if s.APP_ENV != "dev":
        raise RuntimeError("JWT_SECRET is required in non-dev environments")
    fallback = os.environ.get("AUTH_ENCRYPTION_KEY", "")
    if fallback:
        logger.warning("Using derived dev JWT secret — DO NOT deploy this way")
        return f"dev-jwt::{fallback}"
    raise RuntimeError("JWT_SECRET unset and no AUTH_ENCRYPTION_KEY for dev fallback")
```

Also add a Pydantic validator on `Settings` that runs at app boot, so the failure is "FastAPI refuses to start" rather than "first login request 500s."

Severity rationale: the failure mode is silent privilege escalation if `AUTH_ENCRYPTION_KEY` is ever exposed.

---

### CR-03: Phone enumeration via timing — "user not found" returns ~250ms faster than "wrong password"

**File:** `app/modules/auth/services/auth_service.py:139-141, 167-169`
**Concern referenced:** #5

**Issue.** `login()` returns `invalid_credentials` immediately when `_find_user_by_phone()` returns `None`, with no bcrypt work done. When the user exists, `password_rules.verify(password, user["password_encrypted"])` runs `bcrypt.checkpw` at `rounds=12`, which is engineered to take ~200-300 ms. The wall-clock difference between "phone not registered" and "phone registered, wrong password" is therefore on the order of 250 ms — easily measurable over the network even with jitter.

An attacker can enumerate which phones in a list are registered with a single bad-password attempt each. The lockout doesn't help (5 attempts per phone before lockout, but each "phone not in DB" attempt costs the attacker nothing on the victim side and never increments the counter).

**Fix.** When the user is missing, do a dummy bcrypt compare so timing is symmetrical:

```python
_DUMMY_HASH = bcrypt.hashpw(b"dummy-for-timing-only", bcrypt.gensalt(rounds=12)).decode()
# computed once at module import

if not user:
    # Equalise wall-time so attacker can't enumerate registered phones
    bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH.encode("utf-8"))
    raise AuthError("invalid_credentials", "Invalid phone or password", 401)
```

Same hash-cost regardless of which branch you fall into. (This won't help if an attacker correlates many requests over time and observes the *variance* of the bcrypt path vs. the more-jittery dummy path, but the median-difference enumeration vector is closed.)

Note: the same timing leak exists for `account_suspended` / `account_disabled` / `account_locked` — these all return before bcrypt. That's a smaller class (an attacker would have to *also* know the phone is registered to hit those branches), but if you're hardening, run a dummy bcrypt before raising those too.

Severity rationale: trivially exploitable enumeration of the user list, which is a violation of the implicit privacy contract for a phone-based login system.

---

## High Severity

### HI-01: Reuse-detection has no defence against a stolen token whose row was never inserted

**File:** `app/modules/auth/services/auth_service.py:295-297`
**Concern referenced:** #2

**Issue.** If a refresh JWT verifies (signature + expiry valid) but its `jti` has no row in `auth_refresh_token`, the code raises `invalid_refresh_token` and returns. **No chain-wide revocation happens.** This is the right call for "the DB lost the row" but it's the wrong call for an attacker scenario: imagine the attacker stole a refresh JWT from the wire, but the legitimate user has already used it once (so the original row exists, but is now rotated → has a new `jti`). The attacker presents the stolen JWT — the `jti` *does* exist, it's the original, and `rotated_at IS NOT NULL` triggers the chain revoke. Fine.

But a more interesting case: the attacker mints (somehow — say a leaked secret in a non-prod env) a refresh JWT with a forged `jti` that doesn't exist in the DB. Today they get `invalid_refresh_token` and that's that. **But** if the attacker also forges `chain_root` to match a legitimate chain, no defence triggers. There's no signal to log/alert on either: the response code/message for "unknown jti" is identical to "expired token" or "wrong issuer."

**Fix.** Distinguish the cases in the code/log message at minimum. If you want defence-in-depth: when a JWT verifies but the `jti` is unknown, *also* check whether `chain_root` (which is in the JWT) matches any active chain owned by the same `sub` — if so, that's suspicious enough to revoke that chain, since a legitimate refresh would always have its row written at issuance. Otherwise, log a counter (`auth.refresh.unknown_jti`) and alert if it spikes.

Also: distinguish `revoked_at IS NOT NULL` (line 299-300) from `rotated_at IS NOT NULL` (line 302-318) in error code:

- `revoked_at` non-null → `refresh_token_revoked` (401)
- `rotated_at` non-null → `token_reuse_detected` (401, side-effect: chain revoke)
- `jti` not in DB → `invalid_refresh_token` (401)

Currently the first two share the same client-facing `invalid_refresh_token` code (one says "revoked", the other "reuse detected") — fine for client UX, but make sure the server-side logs distinguish them so you can track the real-attack signal.

---

### HI-02: Failed-login counter atomicity assumes asyncpg autocommit semantics that are not asserted anywhere

**File:** `app/modules/auth/services/auth_service.py:167-169, 238-258`; `app/modules/auth/router.py:62-72`
**Concern referenced:** #3

**Issue.** The router-level handler does *not* wrap `auth_service.login` in `conn.transaction()`. The intent (documented in the router comment at lines 59-61 and the service comment at lines 177-178) is that `_record_failed_login`'s `UPDATE` runs in autocommit mode and commits before the surrounding `AuthError` is raised, so the failed counter persists.

This is **correct** for asyncpg by default — outside a `conn.transaction()` block, each statement runs as its own implicit transaction and commits on success. `await conn.execute(...)` is autocommit. Verified: asyncpg's `Connection.execute()` with no enclosing `transaction()` issues `Q`/`X` directly.

**However**, this only holds if (a) no decorator/middleware wraps the request in a transaction (none does in this codebase), and (b) the asyncpg connection is not in an explicit transaction state inherited from a previous request. (a) is fine. (b) is also fine because connections are acquired fresh from the pool at `pool.acquire()` and the pool resets connection state on release.

**The bug surface is on the success path.** `auth_service.login` lines 179-205 wrap success-path writes in `async with conn.transaction()`. But the failed-login UPDATE at line 168 (`await _record_failed_login(...)`) runs *before* this block, so it's autocommit — correct. **Except**: if the bcrypt verify *succeeds*, `_record_failed_login` is never called — instead the counter is reset at line 183 inside the transaction block. That works for the success case. ✓

But: `_record_failed_login` itself does *two* `UPDATE`s (counter increment + lockout). These two are not wrapped in their own `conn.transaction()`, so if the second one fails (e.g. interval cast error), the counter increment commits but the lockout doesn't. A malicious client can force errors on the lockout branch by other means? Probably not — the cast error is deterministic — but the principle is wrong.

**Fix:**
1. Add an explicit unit-test or integration test that asserts the failed counter *does* persist when login raises `AuthError`. This is the kind of thing that silently breaks if someone later wraps the login route in a transaction-decorator.
2. Wrap `_record_failed_login` in its own `async with conn.transaction()` so the count+lock pair is atomic.
3. Add a code comment at `router.py:62` explicitly forbidding the wrapping of this call in a transaction, with a one-liner explaining why ("counter must persist through AuthError raise").

---

### HI-03: `_extract_token` is case-sensitive on `Bearer ` and trims nothing — RFC violation

**File:** `app/modules/auth/router.py:226-230`
**Concern referenced:** general

**Issue.** RFC 6750 §2.1 says the scheme is case-insensitive: `bearer foo` / `BEARER foo` / `Bearer foo` all valid. The current parser only matches `Bearer ` exactly:

```python
def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None
```

A client (or an HTTP intermediary that normalizes case) sending `bearer eyJ…` gets a 401 from `_require_auth` / `_require_admin`. The new JWT-aware path via `HTTPBearer(auto_error=False)` does the right thing (`fastapi.security.HTTPBearer` is case-insensitive), so the spec endpoints work — but the legacy admin block (lines 256+) uses `_extract_token` and will reject lowercase scheme.

**Fix:**
```python
def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "").strip()
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None
```

---

### HI-04: `edit_user` / `edit_permission` build SQL via f-string interpolation of column names

**File:** `app/modules/auth/router.py:296-315, 482-506`
**Concern referenced:** #7

**Issue.** Both endpoints construct the `SET` clause via:

```python
for field in ['full_name', 'email', 'role_id', 'entity', 'is_active', 'allowed_warehouses']:
    if field in sent:
        updates.append(f"{field} = ${idx}")
        ...
sql = f"UPDATE auth_user SET {', '.join(updates)} WHERE user_id = ${idx}"
```

This is **safe today** because `field` is iterated from a hardcoded allowlist — no user input ever reaches `field`. But the pattern is fragile: any future maintainer who adds `for field in body.dict()` or `field = body.model_fields_set` (which is exactly what `sent` is, and is *only* safe because of the subsequent allowlist filter) will introduce SQL injection. The allowlist + dynamic-SQL combo is the right pattern, but the f-string makes it visually indistinguishable from an injection bug.

Also, the two routes' allowlists are **not** the same as the corresponding columns in `auth_user` — `password_encrypted`, `status`, `failed_login_count`, etc. are all updatable by accident if anyone widens the loop.

**Fix.** Move the column allowlist to a module-level constant explicitly named `_EDITABLE_USER_COLUMNS = frozenset({...})`, gate the loop with an explicit `assert field in _EDITABLE_USER_COLUMNS` inside, and add a comment explaining why this isn't injection.

Better: use `psycopg.sql.Identifier` equivalent — asyncpg doesn't have a clean one, but you can use `sqlalchemy.sql.quoted_name` or just hand-write:

```python
_VALID_FIELDS = {"full_name", "email", "role_id", "entity", "is_active", "allowed_warehouses"}

for field in body.model_fields_set:
    if field not in _VALID_FIELDS:
        continue   # ignore unknown fields silently
    updates.append(f'"{field}" = ${idx}')
    ...
```

Quoting the identifier makes the intent explicit even if a future reviewer doesn't read upstream.

---

### HI-05: `validate_session` re-loads the user on every protected request — N+1 by design, document it

**File:** `app/modules/auth/services/auth_service.py:609-656`; `app/modules/auth/middleware.py:81-83`
**Concern referenced:** #6

**Issue.** Every protected endpoint runs:

```
pool.acquire()    →  validate_session(token)    →   1× SELECT auth_user JOIN auth_role
require_permission → _extract_user → check_permission → 1× SELECT auth_role_permission JOIN auth_permission
```

Two pool-acquires, two queries per protected call. For a chatty UI (e.g. job-card list refresh hitting 5-6 endpoints), this is 10-12 round-trips per page load. This is a deliberate trade-off for revocation freshness (the access token's `is_admin` claim is overridden by DB on each call, so an admin demotion takes effect on the next request, not on the next 15-min token expiry), and it's the right call for security. But it is genuinely going to hurt under load.

**Fix.** Document the trade-off in the docstring of `validate_session` and `require_permission` explicitly, and add a `# TODO(perf): cache user-row in request.state for the duration of one request` marker. Two tweaks would help:

1. Cache the user-dict on `request.state.user` in `_extract_user` so multiple `Depends(get_current_user)` in the same request don't re-query (FastAPI deduplicates `Depends` per request, so this is moot for the default case — but `_require_auth` / `_require_admin` in the legacy block bypass `Depends`, so they *do* re-query).
2. For `check_permission`: since access-token TTL is 15 min, you could embed permissions in the access token claim and only re-verify role-membership periodically. But that's a bigger change. For now, a single connection (acquire once in middleware, reuse for `check_permission`) would halve the pool-acquire overhead.

Severity rationale: not a bug, but I'm flagging it as High because pool exhaustion under load (`max_size=10` in `connection.py:7`) will be the first symptom users see.

---

### HI-06: `auth_service.create_user` brittle uniqueness-check via string match

**File:** `app/modules/auth/router.py:267-270`
**Concern referenced:** #10

**Issue.** The router catches generic `Exception` and uses `if "unique" in str(e).lower()` to discriminate uniqueness violations. This is brittle:

- If the asyncpg/Postgres error message is ever localised (it can be, with `client_encoding` and `lc_messages`), the substring won't match.
- If a different unique constraint fails (not just `phone`), the response misleadingly says "Phone number already registered".
- It silently masks all other errors as 500s with no log, because `raise` re-raises and FastAPI returns 500 — but no log line is emitted from this layer.

**Fix.** Catch the asyncpg-specific exception:

```python
import asyncpg
try:
    return await _create(...)
except asyncpg.UniqueViolationError as e:
    if e.constraint_name and "phone" in e.constraint_name:
        raise HTTPException(status_code=409, detail="Phone number already registered")
    raise HTTPException(status_code=409, detail=f"Conflict: {e.constraint_name}")
```

Same fix applies to `create_permission` (router.py:475-478).

Also note: `create_user` does **not** validate the password against `password_rules.evaluate()` — admin can create users with weak passwords. The spec rules say enforce at create+change. **This is a real spec gap, not just brittleness.** Add:

```python
failed = password_rules.evaluate(body.password, normalize_phone(body.phone) or body.phone)
if failed:
    raise HTTPException(status_code=400, detail={"error": "weak_password", "rules": failed})
```

before the `_create` call.

---

### HI-07: `password_rules.evaluate` phone-substring suffix loop is off-by-one (skips full-length variant when phone is exactly 7 digits, and over-blocks short common substrings)

**File:** `app/modules/auth/services/password_rules.py:67-72`
**Concern referenced:** related to #5/spec compliance

**Issue.** The candidate generator is:

```python
candidates.add(phone_digits)  # full digits
for n in range(7, len(phone_digits)):
    candidates.add(phone_digits[-n:])
```

`range(7, len(phone_digits))` is `[7, 8, …, len-1]`. Notable behaviours:

- For `phone_digits = "9876543210"` (10 digits): adds suffixes of length 7, 8, 9. Plus the full 10. **OK.**
- For `phone_digits = "919876543210"` (12 digits, E.164 with country code): adds suffixes 7-11. The 7-char suffix is `"6543210"`. If a user has a password `"6543210Welcome!"`, it'll be rejected even though `6543210` is a generic number string and the user's phone is ambiguously `+91 98765 43210`. Probably fine — strict is OK here.
- For `phone_digits = ""` (empty after normalization): the `if phone_digits:` guard catches it. **OK.**
- **Bug:** For `phone_digits = "1234567"` (7 digits), `range(7, 7)` is empty, so only the full 7-digit string is added. A password `"234567abc"` (using a 6-char suffix) would pass. That's *probably* the intended behaviour ("≥7 digit suffix"), but the comment on line 69 says "last-7..15 suffixes" which implies inclusive of 7. So the off-by-one is in the comment, not the code. Document it: "suffixes of length 7 through len-1, plus the full string."

Also: the full `phone` may include the `+91` prefix when passed to `evaluate(plain, user["phone"])` from `change_password` (line 512). `phone_digits` strips non-digits, so `+919876543210` becomes `919876543210` — and a password `9876543210xyz` (the user's own bare 10-digit number) would NOT match because the candidate set has 12-digit prefixes/suffixes, none of which is the bare 10-digit form.

**Fix.** Normalize `phone` through `phone.normalize()` first, *and* always also include the bare 10-digit form when present:

```python
from app.modules.auth.services.phone import normalize as _norm
norm = _norm(phone) or phone
phone_digits_full = "".join(ch for ch in (norm or "") if ch.isdigit())
# Always add the bare-10 form for Indian mobiles
if phone_digits_full.startswith("91") and len(phone_digits_full) == 12:
    candidates.add(phone_digits_full[2:])
```

Severity rationale: spec compliance bug — the rule "must not contain the user's phone" is currently bypassable for users registered with the country code.

---

## Medium Severity

### MD-01: `_now_iso()` calls `datetime.now()` twice — racy and non-monotonic across the call

**File:** `app/core/middleware/request_context.py:88-90`

```python
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
           f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
```

Two separate `datetime.now()` calls. The seconds and milliseconds can disagree at a tick boundary (very rare, but possible), producing e.g. `"2026-05-05T12:00:00.999Z"` where the actual instant was `12:00:01.000`. **Fix:**

```python
def _now_iso() -> str:
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"
```

Or just use `n.isoformat(timespec='milliseconds').replace('+00:00', 'Z')`.

---

### MD-02: `RequestContextMiddleware` exception handler path renders an envelope that never reaches the registered exception handlers

**File:** `app/core/middleware/request_context.py:69-77`

**Issue.** If something inside `await call_next(request)` raises uncaught, the middleware catches it at line 71 and renders a 500 envelope. But this *bypasses* the registered `unhandled_exception_handler` / `auth_error_handler` etc. The registered handlers fire when the exception propagates *up to the FastAPI exception layer* — but BaseHTTPMiddleware sits *outside* that layer (Starlette wraps it). So:

- `AuthError` raised in a route → propagates → caught by `auth_error_handler` → response returned → middleware sees the response (not an exception) → adds headers. **OK.**
- An unexpected `KeyError` raised in a route → propagates → caught by `unhandled_exception_handler` → 500 envelope returned → middleware sees response → adds headers. **OK.**
- Exception raised *in a registered exception handler itself* → propagates past FastAPI → caught at line 71 → rendered as `internal_error`. This is the rare case. **OK, last-resort.**

So the code is correct, but the comment at line 72 ("handlers below should have already rendered the envelope") implies a fallback that's almost never hit. Worth a code comment explaining the layering more clearly so future maintainers don't think this is dead code.

---

### MD-03: `_envelope` includes `"details": {}` for all responses — leaks "no details" vs "empty details" ambiguity

**File:** `app/core/middleware/request_context.py:93-100`

Minor API-contract issue: the spec example shows `details` as present-and-populated. Always emitting `details: {}` even when there's nothing to add is fine, but combine with auth_error_handler at line 114, which passes `exc.details` (defaults to `{}` from line 54). Result: the client always sees a `details` key. That's good consistency, but the spec is silent on whether `details` is omitted when empty. Confirm with the API consumer; if they expect `details` to be absent in the empty case, add a conditional in `_envelope`.

---

### MD-04: `auth_error_handler` returns `Cache-Control` and `X-Content-Type-Options` only via the middleware second-pass — NOT directly

**File:** `app/core/middleware/request_context.py:110-116`

The handler sets `X-Request-ID` directly but relies on `RequestContextMiddleware` to add `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. This works because the middleware runs after the handler returns. But if the middleware ever fails / is removed / is reordered, the security headers silently disappear from error responses. Add them defensively here and in the other handlers:

```python
headers={"X-Request-ID": rid, "Cache-Control": "no-store",
         "X-Content-Type-Options": "nosniff", **exc.headers}
```

---

### MD-05: JWT verification doesn't enforce `iss` matches the configured issuer — actually wait, it does, but only if claim is present

**File:** `app/modules/auth/services/jwt_service.py:131-139`

`pyjwt.decode(..., issuer=_iss())` validates `iss` claim equals `_iss()` **and** raises if `iss` is missing from the payload. The `options={"require": [...]}` list does NOT include `iss`, so a token without an `iss` claim *might* slip past — but in practice, `pyjwt` raises `InvalidIssuerError` if `iss` is missing when `issuer=` is passed. Verify this with the installed PyJWT version (`>=2.10.1`); pre-2.0 had laxer behaviour.

**Fix:** add `"iss"` to the require list explicitly:

```python
options={"require": ["exp", "iat", "sub", "jti", "type", "iss"]},
```

---

### MD-06: `pyjwt.encode` returns `bytes` on PyJWT 1.x and `str` on PyJWT 2.x — code assumes `str`

**File:** `app/modules/auth/services/jwt_service.py:93, 124`

`requirements.txt` pins `PyJWT[crypto]>=2.10.1`, so this is fine in practice (2.x returns `str`). But the lower-bound `>=` means a future resolver could pull anything; if you ever pin a 1.x for some reason, the returned bytes object will silently break JSON serialization. **Fix:** wrap `.encode()` results in `str()` defensively, or pin upper bound `<3.0`.

---

### MD-07: `me()` mixes per-permission scope with user-level scalars — semantic is "union", which conflicts with the spec example

**File:** `app/modules/auth/services/auth_service.py:434-481`
**Concern referenced:** #13

The user asked specifically about this. The current logic:

1. Collect `entities`, `warehouses`, `floors` as the *union* of `allowed_entities` from every role-permission row.
2. If `entities` is empty, fall back to `[user["entity"]]`.
3. If `warehouses` is empty, fall back to `user["allowed_warehouses"]`.

This is a *reasonable* model: "the set of entities/warehouses/floors this user is allowed to see anywhere." But it conflicts with the spec example (which the concern calls out as showing scalar lists at the user level, not aggregated). Two semantic problems:

- **Lossy.** If permission A grants `entities=["entityX"]` for `module=so` and permission B grants `entities=["entityY"]` for `module=production`, the union `["entityX", "entityY"]` reads as "user can do everything in both" but the user actually can't do `so` in `entityY` or `production` in `entityX`. The frontend can't tell.
- **Inconsistent fallback.** `entities` falls back to user-row only if union is empty; same for warehouses; but `floors` has no fallback. Why?

**Fix.** Decide one model and stick to it:

- **Option A:** Keep the spec contract — `entities`/`warehouses`/`floors` are the *user-level* lists from `auth_user.entity` / `auth_user.allowed_warehouses` (and there's no user-level `floors`). Don't aggregate from permissions. The frontend uses these as a "default scope" UI hint; the actual enforcement happens server-side via `check_permission`.
- **Option B:** Aggregate, but expose the union *and* also expose the per-permission scope inside the `permissions[]` items. Then the frontend can render either view.

Currently you have a hybrid: the union is exposed but the per-permission scope is hidden. Pick one.

---

### MD-08: Logout idempotency hides revoked-token-replay attack signal

**File:** `app/modules/auth/services/auth_service.py:371-386`
**Concern referenced:** #9

`logout()` swallows `AuthError` from `verify_refresh` (expired/invalid signature) and the subsequent `UPDATE` with `WHERE jti=$1 AND revoked_at IS NULL` is silent on miss. Result:

- Logout with an expired token → 204 OK, no signal.
- Logout with a never-issued token → 204 OK, no signal.
- Logout with a revoked token → 204 OK, no signal.

This is correct UX (logout should be idempotent) but it removes server-side telemetry on weird logout calls. If an attacker is probing for valid-vs-invalid refresh tokens via the logout endpoint, you'd never know.

**Fix.** Keep the 204 response but log a `warning` when `verify_refresh` fails with a code (not the token itself), and increment a metric `auth.logout.invalid_token` so you can alert on spikes.

Also: `logout()` does NOT verify that the refresh token belongs to the authenticated user (`Depends(get_current_user)`). An attacker who steals user A's access token *and* user B's refresh token can logout B's session. Low risk (you need both, and you'd usually have your own refresh too) but worth gating:

```python
if int(payload["sub"]) != user.user_id:
    return  # silently ignore — don't 403 to keep idempotency
```

---

### MD-09: Migration `ADD COLUMN ... NOT NULL DEFAULT` on `auth_user` is *not* unconditionally an instant metadata change

**File:** `app/db/004_auth_rebuild.sql:17-22`
**Concern referenced:** #11

PostgreSQL 11+ supports instant `ADD COLUMN ... DEFAULT <constant>` when:

1. The default is a *constant or volatile-immutable* expression. `'active'`, `0`, `FALSE` — fine.
2. The column has no `NOT NULL` *and* no default → instant.
3. With `NOT NULL DEFAULT <constant>` → instant in PG 11+ (the default is stored in catalog; existing rows materialize the value lazily).

**However:**

- PG ≤ 10 → full table rewrite. (You said "modern Postgres," presumably ≥ 11. Confirm.)
- The subsequent `UPDATE auth_user SET password_changed_at = COALESCE(...)` at lines 32-34 *does* rewrite every row. On a large `auth_user` table this is the slow step, not the ALTER. Run it in batches if you have >100k users.
- The `DROP CONSTRAINT IF EXISTS ... ADD CONSTRAINT ... CHECK (...)` at lines 24-29 forces a full table scan to validate the check constraint against existing rows. With 100k+ rows this is fast but not free.

**Fix.** Document the version requirement in the migration header, and add an explicit `\timing` hint or a comment about the constraint-validation scan.

---

## Low Severity

### LO-01: `AuthError` is raised by handlers that pass `headers` containing `X-Request-ID` — the handler then overwrites it

**File:** `app/core/middleware/request_context.py:115`

`{"X-Request-ID": rid, **exc.headers}` — if `exc.headers` contains `X-Request-ID` (it shouldn't, but it could), it overrides `rid`. Reverse the spread order: `{**exc.headers, "X-Request-ID": rid}`.

---

### LO-02: `_blocklist()` LRU cache is global to the process and never invalidated

**File:** `app/modules/auth/services/password_rules.py:36-45`
**Concern referenced:** #12

In dev with hot-reload (uvicorn `--reload`), the cache survives module reload because it's at function-level. Minor — restart the server. The path resolution (`parents[3]`) does land at `app/`; verified.

---

### LO-03: `phone.normalize` accepts numbers that aren't real (e.g. `"+999999999999"` 12 digits → `"+999999999999"`)

**File:** `app/modules/auth/services/phone.py:42-44`

The "generic international: 8-15 digits → just slap a + on it" branch accepts any digit string in that range. No country-code validation. If you want stricter validation, swap in `phonenumbers` (Google's libphonenumber port) — but for this scope, OK.

---

### LO-04: `rate_limiter` key is `(ip, phone)` but `phone` may be `None` if normalize fails — falls back to `"?"`

**File:** `app/modules/auth/services/rate_limiter.py:40, 64`

When `normalize_phone` returns `None`, the router passes `body.phone` as a fallback (line 55: `norm = normalize_phone(body.phone) or body.phone`), so `phone` is the raw user input, not `None`. So `"?"` fallback in the rate_limiter is dead code — but defensive. **OK.** But: the *rate-limit bucket* is keyed on the *normalized* phone, so two attackers using `"9876543210"` and `"+919876543210"` from the same IP share the bucket (good); two using `"9876543210"` and `"09876543210"` share the bucket (good — both normalize to `+919876543210`); but `"abc"` (un-normalizable) shares bucket with all other un-normalizable inputs from the same IP (slightly bad — attackers could DoS legitimate typo'd attempts). Negligible.

---

### LO-05: `_role_payload` flattens to a single role per user — schema says `roles: list[RoleOut]` (plural)

**File:** `app/modules/auth/services/auth_service.py:114-122`

The user table has `role_id INT` (singular FK). `_role_payload` returns `[role]` or `[]`. The spec exposes `roles` as a list, presumably to allow many-to-many later. Fine for now, but document the assumption.

---

### LO-06: `_find_user_by_phone` runs N queries serially — could be one IN query

**File:** `app/modules/auth/services/auth_service.py:98-104`

`lookup_keys()` typically returns 2-3 candidates. The loop does N round-trips. Replace with `SELECT ... WHERE phone = ANY($1::text[]) ORDER BY array_position($1, phone) LIMIT 1`. Micro-optimization; current code is correct.

---

### LO-07: `_get_cipher` reads `.env` files at runtime — slow on every call, also reads files outside the app dir

**File:** `app/modules/auth/services/auth_service.py:46-62`

Iterates `Path(__file__).parents[3] / ".env"` and `Path.cwd() / ".env"`. Per call. Cache the result with `@lru_cache(maxsize=1)`. Also: reading `.env` from `cwd` is a Lambda hazard (no file at runtime, but no error either — silent fallthrough). Document.

---

## Nit

### NI-01: Inconsistent string-quote style across files (single vs double)
Multiple files. Run `ruff format` or `black` to normalize. Cosmetic.

### NI-02: `auth_service.py:268-279` `_parse_device_info` — handles `bytes` but asyncpg never returns bytes for JSONB
The `bytes` branch at line 273 is dead. Harmless. Remove for clarity.

### NI-03: `AUTH_API_DOC.md` exists alongside `router.py` but isn't mentioned in any code comment
Add `# See AUTH_API_DOC.md` in `router.py` header. (Out of strict scope but noticed.)

### NI-04: `MAX_LEN = 128` hardcoded in `password_rules.py` instead of `Settings`
Future configurability. Move to `Settings` if you ever need to relax for B2B clients with password managers that pre-fill 200-char passphrases.

### NI-05: `_DEFAULT_HTTP_CODES` in `request_context.py` doesn't have a 408 entry
Missing `408: "request_timeout"`. Falls through to `"error"` — fine, but inconsistent.

---

## Cross-File / Architectural Notes

- **JSONB device_info round-tripping (concern #8):** Verified. `_parse_device_info` is used in `list_sessions` (line 598) and `refresh` (line 346, when copying device_info from the rotated row to the new row). No raw `dict(row[...])` reads of `device_info` elsewhere. **OK.**
- **No SQL f-string injection found in the new SQL** (concern #7). The `WITH revoked AS` patterns at auth_service.py:392-401, 538-565 are all parameterized. The router-level dynamic builders at 296-315 and 482-506 are the only f-string SQL, both with hardcoded allowlists (see HI-04).
- **CORS `allow_origins=["*"]` in main.py:75** is unrelated to this rebuild but worth flagging for future tightening — wide-open CORS on an auth-issuing endpoint means any origin can mount a credentialed login (though `allow_credentials` is not set, so cookies don't apply). Auth tokens travel in `Authorization` header so this is mostly fine, but tighten to a known origin list before going public.
- **`request_context.install` order vs CORS:** main.py:71 installs request_context BEFORE CORS at line 73. The comment claims this is so `X-Request-ID` propagates through CORS — actually it's the opposite, middleware installed first runs *innermost*, and Starlette middleware ordering is LIFO. The current order means CORS runs first (outermost), then RequestContext. CORS preflight `OPTIONS` requests will NOT carry the `X-Request-ID` header because they short-circuit before RequestContext runs. If that matters for client tracing, swap the order.

---

_Reviewed: 2026-05-05_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
