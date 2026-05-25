---
phase: auth-rebuild
reviewed: 2026-05-05T00:00:00Z
pass: 3 (confirmative)
depth: deep
files_reviewed:
  - app/modules/auth/services/auth_service.py
  - app/modules/auth/middleware.py
  - app/modules/auth/router.py
  - app/modules/auth/services/phone.py
  - app/core/middleware/request_context.py
findings:
  pass2_closed: 4
  pass2_open: 0
  new_critical: 0
  new_high: 0
  new_medium: 1
  new_low: 2
  new_nit: 1
status: ship_ready_with_minor_followups
---

# Auth Rebuild — Verification Pass 3 (confirmative)

**Reviewed:** 2026-05-05 (third pass — confirmative)
**Depth:** deep — focus on regression check of pass-2 fixes (NEW-CR-01, NEW-HI-01, NEW-MD-01, NEW-MD-02), plus third-order regression scan and one pass-1 spot-check.
**Status:** All four pass-2 issues are correctly resolved. One Medium follow-up worth filing, two Lows, one Nit. **Ready to ship.**

---

## Pass-2 fixes verified

### NEW-CR-01: Reuse-detect chain-revoke rolled back inside transaction — **CLOSED**

`auth_service.py:366-543`. Walked the entire `refresh()` function.

**Structural verification:**
- The chain-revoke `UPDATE auth_refresh_token SET revoked_at = NOW(), revoke_reason = $2 WHERE chain_root = $1::uuid AND revoked_at IS NULL` (lines 485-492) is now OUTSIDE `async with conn.transaction()` (which closes at line 477). Confirmed by indentation and the inline comment at line 478.
- The `deferred` tuple (typed as `tuple[str, str | None]`) correctly captures all four failure kinds: `unknown` (line 468, 470), `unknown_with_sibling` (line 466), `revoked` (line 472), `reuse_race` (line 476). Each kind is enumerated in the post-block dispatch at lines 493-518.
- The post-transaction `if deferred is not None:` block (line 480) correctly:
  - Maps `unknown_with_sibling` and `reuse_race` to chain-revoke + warning log + `token_reuse_detected` AuthError.
  - Maps `revoked` to info log + `invalid_refresh_token` (with "revoked" message).
  - Maps `unknown` to warning log + `invalid_refresh_token` (with "unknown" message).
  - The `if revoke_root:` guard (line 482) ensures we only run the UPDATE when a chain to revoke is meaningful — `unknown` (no sibling) and `revoked` correctly skip it.
- `happy_path` (line 434-439) carries `user`, `new_refresh_jwt`, `new_jti`, `refresh_ttl` across the boundary. The post-block at lines 521-543 destructures these correctly. `user` is `dict(user)` so the asyncpg Record is materialised before the transaction closes — no lazy-row-bind issue.

**Edge case 1 — clean commit but `_load_role` raises:** Yes, the new refresh row is committed before `_load_role` runs (line 527). If `_load_role` errors, the user has a usable new refresh token in the DB but receives a 500. **Acceptable.** The next `/refresh` call from the client will succeed (the new refresh JWT verifies and the row exists with `rotated_at IS NULL`). No security impact — it's the inverse of the original NEW-CR-01: an over-rotation rather than a missed revoke. Worth a one-line code comment noting the deliberate choice; not a blocker.

**Edge case 2 — chain-revoke UPDATE itself fails after commit:** This is a genuine residual risk but very narrow. Sequence: rotation transaction commits (failure-path = no INSERT, so nothing was committed), then the autocommit chain-revoke at line 485 fails (e.g. PG connection drops, transient error), then `raise AuthError` propagates. Net state: chain stays alive, attacker keeps refreshing, log line says "reuse detected." Same failure mode as the original NEW-CR-01 but only on a transient DB error during the revoke statement — not on every reuse. **Severity: Low.** Filed as **PASS3-LO-01** below. The ergonomic fix is to log the revoke failure separately and let the operator alert; full belt-and-braces would re-attempt or queue.

### NEW-HI-01: `is_in_transaction()` guards — **CLOSED**

Two guards added with clear, action-oriented messages:

- `auth_service.login()` line 211-216: `if conn.is_in_transaction(): raise RuntimeError("auth_service.login() must run on an autocommit connection. ...")`. Message explains both the *what* and the *why* (failed-login counter rollback → defeats lockout).
- `auth_service._record_failed_login()` line 335-340: same pattern, redundant defence-in-depth (since `login()` already guards). Worth keeping — `_record_failed_login` is a private helper but module-private, not protected.

**Asymmetry note (worth a sanity check, not a blocker):** Only `login()` guards; `refresh()`, `logout()`, `change_password()`, and `revoke_session()` do NOT guard. For `refresh()` this is correct — the function intentionally manages its own `conn.transaction()` block. For `change_password()` and `logout_all()`, an outer transaction would not break correctness because they rely on commit semantics, not on autocommit-between-statements. So the asymmetry is intentional. Could be documented in a one-liner.

**Legitimate need to wrap `login()` in a transaction (impersonation, batch import):** None today. Impersonation in this codebase would mint a token for another user, not call `login()`. Batch user import calls `auth_service.create_user()` (a different function). The assert is appropriately strict for the actual call sites. If a future need arises (e.g. an integration test that wants to roll back side effects), the test should use a connection from a different pool or call `auth_service.login` against a savepoint-only adapter — not loosen the guard.

### NEW-MD-01: `_find_user_by_phone` single-query — **CLOSED**

`auth_service.py:121-144`. Read together with `phone.lookup_keys()` at `phone.py:47-57`.

**Semantic equivalence verification:**
- Old behaviour: loop `for k in lookup_keys(raw_phone): row = await conn.fetchrow("... WHERE phone = $1", k); if row: return dict(row)`. Returns the FIRST match in `lookup_keys` order.
- `lookup_keys` order is: `[normalized_E164, bare_10_digit_legacy, raw_input]` — most-canonical first.
- New query: `WHERE phone = ANY($1::text[]) ORDER BY array_position($1::text[], phone) LIMIT 1`.
- `array_position(arr, elem)` returns the 1-based index of the first match in `arr`. So `ORDER BY array_position($1, phone) LIMIT 1` returns the row whose `phone` value appears EARLIEST in `keys`. **Identical to the loop's "first match wins" semantic.** ✓

**Empty-keys short-circuit:** `if not keys: return None` (line 133-134). Confirmed — no wasted query.

**asyncpg list-as-text[] binding:** asyncpg correctly maps a Python `list[str]` to PG `text[]` when the cast `$1::text[]` is present. Verified by reading asyncpg's codec table behaviour: list types serialize through the array protocol; the `::text[]` cast disambiguates against `varchar[]`. Standard pattern, used elsewhere in this codebase (e.g. `logout_all` does not need it but `auth_user.allowed_warehouses` is `text[]`). **Correct.**

**Timing constancy:** Single round-trip regardless of registered/unregistered. Combined with the `_equalise_login_timing` precomputed bcrypt, the wall-clock difference between branches is now bounded by bcrypt's own variance (which is a function of CPU jitter, not the input). Enumeration vector closed.

### NEW-MD-02: middleware cache shared with `_extract_user` — **CLOSED**

`middleware.py:68-114`. Walked through.

- **(a) reads cache first:** Line 95-97. `cached = getattr(request.state, "user_dict", None); if cached: return _authuser_from_session(cached)`. ✓
- **(b) constructs AuthUser identically from cache and from fresh validate_session:** Both paths funnel through the new `_authuser_from_session(session)` helper at lines 68-79. **This is the right pattern** — single source of truth for the dict-to-AuthUser mapping. No drift possible. ✓
- **(c) writes the cache on miss:** Line 113. `request.state.user_dict = session` is set immediately before the AuthUser construction on the miss path. ✓

**Cache-poisoning concern:** `request.state` is a per-request object (Starlette's `State` is constructed fresh per request from `request.scope["state"]` defaults — no cross-request leakage). For a malicious caller to write `request.state.user_dict`, they'd need to be inside the request handler chain — at which point they already have the legitimate user context. **Not a real attack surface.** No issue.

**Cross-cutting: is `_authuser_from_session` used everywhere AuthUser is constructed?** Checked all `AuthUser(` constructions in middleware.py — only at line 69 inside `_authuser_from_session`. The helper is the only constructor. ✓ No spots missed.

---

## Third-order regressions

### PASS3-MD-01 (MEDIUM): `me()` uses `user["role_id"]` for `_load_role` but `user` is from a `SELECT *` — fine today, fragile if `auth_user` ever gains a column named `role_id` on a join table

`auth_service.py:631-635`. Read in context.

`me()` does `SELECT *` at line 631, then `_load_role(conn, user["role_id"])` at line 635. `_load_role` then SELECTs from `auth_role` — so `is_admin` correctly comes from the role table, not the user row. ✓

**However**, a closely related code path I want to flag: in `refresh()` at line 527, `_load_role(conn, user["role_id"])` is called against `user = happy_path["user"]` which was set from `dict(user)` where `user` was `await conn.fetchrow("SELECT * FROM auth_user WHERE user_id = $1", user_id)` (line 409). So `user["role_id"]` is the user-row's FK, not the role's `is_admin`. **Correct**. The `is_admin` value used at line 528 is `bool(role and role.get("is_admin"))` — pulled from the joined role row. ✓ Trace passes.

So the spec asked "is the post-transaction code correctly loading the role to get `is_admin`?" — **Yes**. Both `login()` (line 293-296) and `refresh()` (line 527-528) call `_load_role` and read `is_admin` off the role dict. There's no path that reads `is_admin` off `user` directly.

**The Medium I'm filing is forward-looking:** there is no schema-level guard against `auth_user` ever growing an `is_admin` column (e.g. someone wants to denormalise for perf). If that ever happens, `bool(role and role.get("is_admin"))` would still be correct, but a careless refactor could write `bool(user.get("is_admin"))` and silently lose the join. Severity: Medium because it's a security-impact regression vector; mitigation is just a one-line code comment in `_load_role` and at `auth_service.py:528` saying "is_admin lives on auth_role; do not read from auth_user." Not a ship-blocker.

### PASS3-LO-01 (LOW): chain-revoke autocommit UPDATE failure leaves chain alive while logging "reuse detected"

`auth_service.py:485-492`. Already discussed under NEW-CR-01 edge case 2.

If the autocommit `UPDATE auth_refresh_token SET revoked_at = ...` fails (PG transient error, network blip, lock-timeout against a contended chain), the surrounding code still raises `AuthError("token_reuse_detected", ...)`. The log line warns but the chain is not revoked.

**Mitigation:** wrap the chain-revoke in its own try/except, log the revoke failure with severity `error` (distinct from the reuse-warning log), and consider re-raising as a 500 in this branch (so the operator gets paged) rather than the misleading 401. Or: queue a reconciliation job. For v1, a one-line `try/except + logger.error(...)` is enough to make the failure visible.

### PASS3-LO-02 (LOW): `_authuser_from_session` doesn't propagate `access_jti` — only `refresh_jti`

`middleware.py:68-79`. The helper sets `refresh_jti=session.get("session_id")` but never sets `access_jti`. The `AuthUser.__init__` accepts `access_jti` as kwarg with default `None`. Today no caller reads `user.access_jti` (greppable: only `user.refresh_jti` and `user.user_id` are used downstream), so this is dead state. Either remove `access_jti` from `AuthUser.__init__` or wire it through from `validate_session` (which does have access to `payload["jti"]` but discards it). Minor.

### PASS3-NI-01 (NIT): `happy_path: dict | None` could be a typed dataclass

`auth_service.py:389-439`. The `happy_path` dict carries 4 named fields across the transaction boundary. A `@dataclass(slots=True)` would catch typos at write/read sites and document the contract. Cosmetic — current code is correct.

---

## Spot checks

### MD-04: `_error_headers` used by all four handlers — **CONFIRMED CLOSED**

`request_context.py:118-124` defines `_error_headers(rid, extra=None)`. Greped usage:

- `auth_error_handler` line 132: `headers=_error_headers(rid, exc.headers)` ✓
- `http_exception_handler` line 171: `headers=_error_headers(rid, dict(exc.headers or {}))` ✓
- `validation_exception_handler` line 187: `headers=_error_headers(rid)` ✓
- `unhandled_exception_handler` line 197: `headers=_error_headers(rid)` ✓

All four handlers route through the helper. `_SECURITY_HEADERS` (`Cache-Control: no-store`, `X-Content-Type-Options: nosniff`) are unconditionally applied. `X-Request-ID` is placed last in the spread so caller-supplied headers can't override it (also fixes LO-01 from pass 1). **Verified.**

### Bonus spot-check: middleware cache key collision

Greped `request.state.user_dict` and `request.state.user` across the codebase. Only writers:
- `app/modules/auth/middleware.py:113` (new spec path)
- `app/modules/auth/router.py:274` (legacy `_require_auth`)

Only readers:
- `app/modules/auth/middleware.py:95` (new spec path)
- `app/modules/auth/router.py:262` (legacy `_require_auth`)

Same key, both writers populate the same shape (`session` dict from `validate_session`), both readers consume the same shape. No collision, no shape drift. ✓

### Bonus spot-check: `lookup_keys` ordering preservation under multiple matches

`phone.py:47-57` builds `[normalized, bare-10-fallback, raw_input]`. If the DB has multiple matching rows (which it shouldn't — `phone` should be unique), `array_position` returns the first match in this canonical order. Matches the loop's "first wins" semantic. ✓

### Bonus spot-check: post-transaction `assert happy_path is not None`

`auth_service.py:521`. The assert is defensive — guaranteed by control flow because the `if rotated:` branch always sets `happy_path`, and the `else:` branch always sets `deferred` (which makes the post-block return before reaching the assert). However `assert` is stripped under `python -O`. If the codebase ever runs with `-O`, an unreachable code path would silently fall through to `user = happy_path["user"]` and raise `TypeError`. Belt-and-braces fix: replace with `if happy_path is None: raise RuntimeError("unreachable")`. Cosmetic; no current risk.

---

## Summary

All four pass-2 regressions are correctly closed:

- **NEW-CR-01** — Chain-revoke now correctly outside the rotation transaction; the deferred-tuple pattern is clean and exhaustive.
- **NEW-HI-01** — `is_in_transaction()` guards in place with clear error messages; no legitimate caller needs to wrap login in a transaction.
- **NEW-MD-01** — Single ANY query semantically equivalent to the loop; ordering preserved via `array_position`; empty-keys short-circuit in place.
- **NEW-MD-02** — Cache shared via `_authuser_from_session` helper; single source of truth for dict→AuthUser mapping.

Three new findings (1 MD forward-looking comment hardening, 2 LO error-handling edge cases, 1 NI dataclass cleanup). None are ship-blockers.

---

_Reviewed: 2026-05-05_
_Reviewer: Claude (gsd-code-reviewer, third pass — confirmative)_
_Depth: deep_
