"""Auth service — JWT login/refresh/logout, sessions, password change, lockout.

Implements the documented end-user auth contract:

    POST /api/v1/auth/login          → login(...)
    POST /api/v1/auth/refresh        → refresh(...)
    POST /api/v1/auth/logout         → logout(...)
    POST /api/v1/auth/logout-all     → logout_all(...)
    GET  /api/v1/auth/me             → me(...)
    POST /api/v1/auth/password/change→ change_password(...)
    GET  /api/v1/auth/sessions       → list_sessions(...)
    DELETE …/sessions/{token_id}     → revoke_session(...)

Backward compat:
    `validate_session(conn, token)` is preserved as the bridge other modules
    use via `app.modules.auth.middleware.require_permission`. It now verifies
    a JWT access token and hydrates the user from the DB on each call (no
    extra session row lookup needed).

    `_get_cipher`, `encrypt_password`, `decrypt_password`, `verify_password`
    are kept so legacy callers (and the password_rules fallback) keep working.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.core.middleware.request_context import AuthError
from app.modules.auth.services import jwt_service, password_rules
from app.modules.auth.services.phone import lookup_keys, normalize as normalize_phone

logger = logging.getLogger(__name__)


# ── legacy Fernet support (kept for backward compat) ─────────────────────


@lru_cache(maxsize=1)
def _get_cipher() -> Fernet:
    """LO-07: cached. Resolving the key reads .env files from disk; do it once per process."""
    key = os.environ.get("AUTH_ENCRYPTION_KEY")
    if not key:
        for env_path in [Path(__file__).parents[3] / ".env", Path.cwd() / ".env"]:
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.strip().startswith("AUTH_ENCRYPTION_KEY="):
                        key = line.strip().split("=", 1)[1]
                        break
            if key:
                break
    if not key:
        raise RuntimeError(
            "AUTH_ENCRYPTION_KEY not set. Generate with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_password(plain: str) -> str:
    """Legacy Fernet-encrypt. New code should call password_rules.hash_password."""
    return _get_cipher().encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    return _get_cipher().decrypt(encrypted.encode()).decode()


def verify_password(plain: str, stored: str) -> bool:
    """Compat shim. Prefers bcrypt, falls back to Fernet via password_rules.verify."""
    return password_rules.verify(plain, stored)


# ── timing equaliser (CR-03) ─────────────────────────────────────────────
#
# Precomputed at module import so the not-found / suspended / locked
# branches can run a real bcrypt verify before raising — equalising
# wall-clock time with the "user exists, wrong password" branch and
# closing the phone-enumeration vector.
#
# Uses bcrypt rounds=12 to match `password_rules.hash_password`.
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"dummy-for-timing-only", bcrypt.gensalt(rounds=12))


def _equalise_login_timing(password: str) -> None:
    try:
        bcrypt.checkpw(password.encode("utf-8"), _DUMMY_BCRYPT_HASH)
    except (ValueError, TypeError):
        # Never let the dummy raise — its only purpose is wall-clock cost.
        pass


# ── helpers ──────────────────────────────────────────────────────────────


_REVOKE_LOGOUT = "logout"
_REVOKE_LOGOUT_ALL = "logout_all"
_REVOKE_REUSE = "reuse"
_REVOKE_PASSWORD_CHANGE = "password_change"
_REVOKE_USER_REVOKED = "user_revoked"
_REVOKE_ADMIN = "admin_revoked"


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _find_user_by_phone(conn, raw_phone: str) -> dict | None:
    """Look up by normalized E.164 + legacy stored phone variants in ONE query.

    NEW-MD-01: a previous loop did N=1..3 sequential SELECTs through
    `lookup_keys`, which left a residual 5-30 ms timing leak (registered
    user → 1 round-trip; unregistered phone → 3 round-trips). Single ANY
    query gives constant timing for all branches, closing the residual
    enumeration vector that CR-03's bcrypt equaliser couldn't cover.
    `array_position` preserves the lookup_keys preference order if any
    edge case ever returns multiple matching rows (phone is unique today).
    """
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


async def _load_role(conn, role_id: int | None) -> dict | None:
    if role_id is None:
        return None
    r = await conn.fetchrow("SELECT * FROM auth_role WHERE role_id = $1", role_id)
    return dict(r) if r else None


def _role_payload(role: dict | None) -> list[dict]:
    """LO-05: schema exposes `roles` as a list to allow many-to-many later;
    today the user table has a single role_id FK so this returns 0-or-1 element."""
    if not role:
        return []
    return _roles_payload([role])


def _roles_payload(roles: list[dict]) -> list[dict]:
    """Spec-shaped `roles` list for an arbitrary set of roles (multi-role).

    Each element mirrors `_role_payload`'s single-role shape. Order is
    caller-controlled (callers put the primary role first)."""
    return [{
        "role_id": str(r["role_id"]),
        "code": r["role_name"],
        "label": (r.get("description") or r["role_name"]).strip() or r["role_name"],
        "is_admin": bool(r.get("is_admin")),
    } for r in roles]


async def _effective_roles(conn, user_id: int, primary_role: dict | None = None) -> list[dict]:
    """Every role a user holds (multi-role), primary role first.

    `auth_user_role` is the source of truth; we fall back to the user's single
    primary role (`auth_user.role_id`) for accounts not yet represented there
    so resolution never returns empty for a user who has a role. Each dict has
    role_id / role_name / description / is_admin.
    """
    rows = await conn.fetch(
        """
        SELECT r.role_id, r.role_name, r.description, r.is_admin
          FROM auth_user_role ur
          JOIN auth_role r ON ur.role_id = r.role_id
         WHERE ur.user_id = $1
        """,
        user_id,
    )
    roles = [dict(r) for r in rows]
    if not roles and primary_role:
        roles = [dict(primary_role)]
    if primary_role is not None:
        pid = primary_role.get("role_id")
        # Stable order: primary role first, then the rest by role_id.
        roles.sort(key=lambda x: (x["role_id"] != pid, x["role_id"]))
    else:
        roles.sort(key=lambda x: x["role_id"])
    return roles


def _json_or_none(d: dict | None) -> str | None:
    if not d:
        return None
    return json.dumps(d, default=str)


def _parse_device_info(raw: Any) -> dict | None:
    """asyncpg returns JSONB as str by default — parse it back to a dict."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None
    return None


# ── login ────────────────────────────────────────────────────────────────


async def login(
    conn,
    *,
    raw_phone: str,
    password: str,
    ip: str | None,
    user_agent: str | None,
    device_info: dict[str, Any] | None,
    settings: Settings,
) -> dict:
    """Verify credentials, mint JWT pair, persist refresh row. Raises AuthError.

    Caller (router) MUST NOT wrap this in `conn.transaction()` — the
    failed-login counter UPDATE relies on autocommit semantics so it persists
    when this function raises. See HI-02.

    NEW-HI-01: defence-in-depth — if a future caller wraps us, fail loudly
    here instead of silently breaking lockout when the outer transaction
    rolls back on AuthError.
    """
    if conn.is_in_transaction():
        raise RuntimeError(
            "auth_service.login() must run on an autocommit connection. "
            "An enclosing conn.transaction() would roll back the failed-login "
            "counter when login raises AuthError, defeating account lockout."
        )

    user = await _find_user_by_phone(conn, raw_phone)
    if not user:
        # CR-03: equalise wall-time so attackers can't enumerate registered
        # phones from the latency difference between "no user" (microseconds)
        # and "wrong password" (~250ms bcrypt cost).
        _equalise_login_timing(password)
        raise AuthError("invalid_credentials", "Invalid phone or password", 401)

    # Status gates run before lockout / password check. Each gate also burns
    # a dummy bcrypt cycle so the timing of "exists+suspended" looks like
    # "exists+wrong-password" to an outside observer.
    status = user.get("status") or ("active" if user.get("is_active") else "disabled")
    if status == "suspended":
        _equalise_login_timing(password)
        raise AuthError("account_suspended", "Account is suspended. Contact admin.", 403)
    if status == "disabled" or not user.get("is_active", True):
        _equalise_login_timing(password)
        raise AuthError("account_disabled", "Account is disabled.", 403)

    locked_until = user.get("locked_until")
    now = datetime.now(timezone.utc)
    if locked_until:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > now:
            _equalise_login_timing(password)
            raise AuthError(
                "account_locked",
                "Account temporarily locked due to repeated failed attempts.",
                423,
                details={
                    "locked_until": _iso(locked_until),
                    "failed_login_count": user.get("failed_login_count", 0),
                },
            )

    if not password_rules.verify(password, user["password_encrypted"]):
        await _record_failed_login(conn, user["user_id"], settings)
        raise AuthError("invalid_credentials", "Invalid phone or password", 401)

    # Mint refresh first so its jti can be the access token's parent_jti
    refresh_jwt, refresh_jti, chain_root, refresh_exp, refresh_ttl = jwt_service.issue_refresh_token(
        user_id=user["user_id"], parent_jti=None, chain_root=None, settings=settings,
    )

    # Wrap success-path writes in one transaction so partial failures don't
    # leave a rehashed-but-no-session state. The earlier failed-login update
    # is intentionally OUTSIDE this block (already committed by autocommit).
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE auth_user
               SET failed_login_count = 0,
                   locked_until = NULL,
                   last_login_at = NOW()
             WHERE user_id = $1
            """,
            user["user_id"],
        )
        if password_rules.needs_rehash(user["password_encrypted"]):
            new_hash = password_rules.hash_password(password)
            await conn.execute(
                "UPDATE auth_user SET password_encrypted = $2 WHERE user_id = $1",
                user["user_id"], new_hash,
            )
        await conn.execute(
            """
            INSERT INTO auth_refresh_token
                (jti, user_id, parent_jti, chain_root, expires_at, ip, user_agent, device_info)
            VALUES ($1::uuid, $2, NULL, $3::uuid, $4, $5, $6, $7::jsonb)
            """,
            refresh_jti, user["user_id"], chain_root, refresh_exp, ip, user_agent,
            _json_or_none(device_info),
        )

    role = await _load_role(conn, user.get("role_id"))
    # Multi-role: a user can hold several roles. is_admin is TRUE if ANY of
    # them is admin; `roles` lists them all (primary first). The access token's
    # is_admin claim is advisory — validate_session re-derives it per request.
    roles = await _effective_roles(conn, user["user_id"], role)

    must_change = bool(user.get("must_change_password"))
    is_admin = any(r.get("is_admin") for r in roles)
    access_jwt, _access_jti, access_ttl = jwt_service.issue_access_token(
        user_id=user["user_id"],
        parent_jti=refresh_jti,
        is_admin=is_admin,
        must_change_password=must_change,
        settings=settings,
    )

    logger.info("Login: user_id=%s role=%s", user["user_id"], role and role.get("role_name"))

    # Surface user-level scope on the login payload so the renderer can lock
    # entity/factory/floor dropdowns on first paint without waiting for /me.
    # Multi-entity comes from allowed_entities; fall back to [entity] for
    # records created before that column existed.
    entities   = list(user.get("allowed_entities")
                      or ([user["entity"]] if user.get("entity") else []))
    warehouses = list(user.get("allowed_warehouses") or [])
    floors     = list(user.get("allowed_floors")     or [])

    return {
        "access_token": access_jwt,
        "refresh_token": refresh_jwt,
        "token_type": "Bearer",
        "expires_in": access_ttl,
        "refresh_expires_in": refresh_ttl,
        "must_change_password": must_change,
        "user": {
            "user_id": str(user["user_id"]),
            "phone": user["phone"],
            "full_name": user.get("full_name"),
            "email": user.get("email"),
            "is_admin": is_admin,
            "roles": _roles_payload(roles),
            "entities":   entities,
            "warehouses": warehouses,
            "floors":     floors,
        },
    }


async def _record_failed_login(conn, user_id: int, settings: Settings) -> None:
    """Increment failed counter; lock the account when threshold is crossed.

    HI-02: wraps both writes in its own transaction so the counter+lockout
    pair is atomic to each other. NEW-HI-01: also asserts the connection is
    not already in a transaction — if it is, the inner conn.transaction()
    becomes a SAVEPOINT that the caller's later raise will roll back (the
    outer transaction is what gets rolled back, taking the savepoint with
    it). That would silently disable account lockout. Refuse to run.
    """
    if conn.is_in_transaction():
        raise RuntimeError(
            "_record_failed_login must run on an autocommit connection. "
            "Caller has an enclosing transaction that would roll back the "
            "counter increment when login() raises AuthError."
        )
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            UPDATE auth_user
               SET failed_login_count = failed_login_count + 1
             WHERE user_id = $1
            RETURNING failed_login_count
            """,
            user_id,
        )
        count = (row and row["failed_login_count"]) or 0
        if count >= settings.LOGIN_LOCKOUT_THRESHOLD:
            await conn.execute(
                """
                UPDATE auth_user
                   SET locked_until = NOW() + ($2 || ' minutes')::interval
                 WHERE user_id = $1
                """,
                user_id, str(settings.LOGIN_LOCKOUT_MINUTES),
            )


# ── refresh (rotate + reuse detection) ────────────────────────────────────


async def refresh(conn, *, refresh_jwt: str, ip: str | None, user_agent: str | None,
                  settings: Settings) -> dict:
    """Rotate a refresh token. Reuse detection revokes the entire chain.

    CR-01: the rotation uses a guarded UPDATE (`WHERE rotated_at IS NULL`)
    as the locking primitive. Two concurrent /refresh calls race for the
    UPDATE; the loser sees zero rows affected and is correctly classified
    as reuse.

    NEW-CR-01: The rotation transaction handles ONLY the rotation-and-INSERT
    happy path. Failure classification happens inside the transaction (cheap
    SELECTs against the row lock we already won/lost), but the
    chain-revoke + AuthError are deferred to AFTER the transaction commits.
    Otherwise the AuthError propagating through `async with conn.transaction()`
    would roll back the chain-revoke we just attempted, defeating reuse
    detection while still firing the misleading log line.
    """
    payload = jwt_service.verify_refresh(refresh_jwt)
    jti = payload["jti"]
    user_id = int(payload["sub"])
    chain_root = payload.get("chain_root") or jti

    # Fields populated inside the transaction; consumed after it commits.
    happy_path: dict | None = None     # contains refresh_jwt + access_jwt + ttls on success
    deferred: tuple[str, str | None] | None = None
    # ↑ (failure_kind, chain_to_revoke). failure_kind ∈ {"unknown",
    #   "unknown_with_sibling", "revoked", "reuse_race"}.

    async with conn.transaction():
        rotated = await conn.fetchrow(
            """
            UPDATE auth_refresh_token
               SET rotated_at = NOW()
             WHERE jti = $1::uuid
               AND rotated_at IS NULL
               AND revoked_at IS NULL
            RETURNING jti, chain_root, user_id, device_info
            """,
            jti,
        )

        if rotated:
            # ── Happy path: we won the rotation lock ────────────────────────
            user = await conn.fetchrow("SELECT * FROM auth_user WHERE user_id = $1", user_id)
            if not user:
                # Raise here is fine — rolling back the rotation marker for a
                # non-existent user is the right behaviour.
                raise AuthError("invalid_refresh_token", "User not found", 401)

            # Pre-004 schemas don't have `status` / `must_change_password`.
            # Treat missing columns as the migration's NOT NULL DEFAULTs.
            status = (user.get("status") or
                      ("active" if user.get("is_active", True) else "disabled"))
            if status == "suspended":
                raise AuthError("account_suspended", "Account is suspended.", 403)
            if status == "disabled" or not user.get("is_active", True):
                raise AuthError("account_disabled", "Account is disabled.", 403)

            new_jwt, new_jti, _root, new_exp, refresh_ttl = jwt_service.issue_refresh_token(
                user_id=user_id, parent_jti=jti, chain_root=chain_root, settings=settings,
            )
            await conn.execute(
                """
                INSERT INTO auth_refresh_token
                    (jti, user_id, parent_jti, chain_root, expires_at, ip, user_agent, device_info)
                VALUES ($1::uuid, $2, $3::uuid, $4::uuid, $5, $6, $7, $8::jsonb)
                """,
                new_jti, user_id, jti, chain_root, new_exp, ip, user_agent,
                _json_or_none(_parse_device_info(rotated["device_info"])),
            )
            # Stash payload for after-transaction return (need is_admin from role)
            happy_path = {
                "user": dict(user),
                "new_refresh_jwt": new_jwt,
                "new_jti": new_jti,
                "refresh_ttl": refresh_ttl,
            }
        else:
            # ── Failure path: classify only. Defer revoke + raise. ─────────
            post = await conn.fetchrow(
                """
                SELECT rotated_at, revoked_at, chain_root
                  FROM auth_refresh_token
                 WHERE jti = $1::uuid
                """,
                jti,
            )
            if post is None:
                # JWT verifies but no row. Suspicious if a chain sibling is
                # still active for the same sub → treat as reuse.
                if chain_root:
                    sibling = await conn.fetchrow(
                        """
                        SELECT 1
                          FROM auth_refresh_token
                         WHERE chain_root = $1::uuid
                           AND user_id = $2
                           AND revoked_at IS NULL
                         LIMIT 1
                        """,
                        chain_root, user_id,
                    )
                    if sibling is not None:
                        deferred = ("unknown_with_sibling", chain_root)
                    else:
                        deferred = ("unknown", None)
                else:
                    deferred = ("unknown", None)
            elif post["revoked_at"] is not None:
                deferred = ("revoked", None)
            else:
                # rotated_at became non-NULL between snapshot and UPDATE →
                # real reuse race.
                deferred = ("reuse_race", str(post["chain_root"]))

    # ── Transaction has committed (or rolled back via raise above). ─────────

    if deferred is not None:
        kind, revoke_root = deferred
        if revoke_root:
            # Chain-revoke runs in autocommit so it persists even though we
            # raise immediately after. This is the entire point of NEW-CR-01.
            # PASS3-LO-01: if the revoke UPDATE itself fails, log loudly so
            # ops can detect "we logged reuse but the chain is still alive."
            try:
                await conn.execute(
                    """
                    UPDATE auth_refresh_token
                       SET revoked_at = NOW(), revoke_reason = $2
                     WHERE chain_root = $1::uuid AND revoked_at IS NULL
                    """,
                    revoke_root, _REVOKE_REUSE,
                )
            except Exception as revoke_err:
                logger.error(
                    "auth.refresh.chain_revoke_failed user_id=%s chain_root=%s err=%r — "
                    "REUSE WAS DETECTED BUT CHAIN MAY STILL BE ACTIVE",
                    user_id, revoke_root, revoke_err,
                )
        if kind == "unknown_with_sibling":
            logger.warning(
                "auth.refresh.unknown_jti_with_active_chain user_id=%s chain_root=%s",
                user_id, chain_root,
            )
            raise AuthError(
                "token_reuse_detected",
                "Refresh token reuse detected. All sessions revoked.",
                401,
            )
        if kind == "reuse_race":
            logger.warning(
                "auth.refresh.reuse_detected user_id=%s jti=%s chain_root=%s",
                user_id, jti, revoke_root,
            )
            raise AuthError(
                "token_reuse_detected",
                "Refresh token reuse detected. All sessions revoked.",
                401,
            )
        if kind == "revoked":
            logger.info("auth.refresh.revoked user_id=%s jti=%s", user_id, jti)
            raise AuthError("invalid_refresh_token", "Refresh token revoked", 401)
        # kind == "unknown"
        logger.warning("auth.refresh.unknown_jti user_id=%s jti=%s", user_id, jti)
        raise AuthError("invalid_refresh_token", "Refresh token unknown", 401)

    # Happy path continuation
    assert happy_path is not None
    user = happy_path["user"]
    new_jwt = happy_path["new_refresh_jwt"]
    new_jti = happy_path["new_jti"]
    refresh_ttl = happy_path["refresh_ttl"]

    # PASS3-MD-01: `is_admin` lives on auth_role, NOT auth_user. Don't be
    # tempted to short-circuit by reading user["is_admin"] — that column
    # does not exist on auth_user; the role row is the source of truth.
    role = await _load_role(conn, user["role_id"])
    # Multi-role: is_admin is advisory on the access token (validate_session
    # re-derives per request), but keep it consistent — TRUE if ANY role admin.
    roles = await _effective_roles(conn, user_id, role)
    is_admin = any(r.get("is_admin") for r in roles)
    access_jwt, _, access_ttl = jwt_service.issue_access_token(
        user_id=user_id,
        parent_jti=new_jti,
        is_admin=is_admin,
        must_change_password=bool(user.get("must_change_password") or False),
        settings=settings,
    )

    return {
        "access_token": access_jwt,
        "refresh_token": new_jwt,
        "token_type": "Bearer",
        "expires_in": access_ttl,
        "refresh_expires_in": refresh_ttl,
    }


# ── logout / logout-all / revoke ─────────────────────────────────────────


async def logout(conn, *, refresh_jwt: str, user_id: int) -> None:
    """Revoke this refresh token. Access token will expire on its own.

    MD-08: logs invalid-token logout attempts (so we can alert on probing)
    and verifies the refresh token's `sub` matches the authenticated user
    (so a stolen access token can't revoke someone else's session).
    Returns silently in both cases to keep the endpoint idempotent.
    """
    try:
        payload = jwt_service.verify_refresh(refresh_jwt)
    except AuthError as e:
        logger.warning("auth.logout.invalid_token user_id=%s reason=%s", user_id, e.code)
        return
    try:
        token_user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        logger.warning("auth.logout.malformed_sub user_id=%s", user_id)
        return
    if token_user_id != user_id:
        logger.warning(
            "auth.logout.cross_user_attempt actor_user_id=%s token_user_id=%s",
            user_id, token_user_id,
        )
        return
    jti = payload["jti"]
    await conn.execute(
        """
        UPDATE auth_refresh_token
           SET revoked_at = NOW(), revoke_reason = $2
         WHERE jti = $1::uuid AND revoked_at IS NULL
        """,
        jti, _REVOKE_LOGOUT,
    )


async def logout_all(conn, *, user_id: int) -> int:
    row = await conn.fetchrow(
        """
        WITH revoked AS (
            UPDATE auth_refresh_token
               SET revoked_at = NOW(), revoke_reason = $2
             WHERE user_id = $1
               AND revoked_at IS NULL
               AND rotated_at IS NULL
            RETURNING jti
        )
        SELECT COUNT(*) AS n FROM revoked
        """,
        user_id, _REVOKE_LOGOUT_ALL,
    )
    return int(row["n"]) if row else 0


async def revoke_session(conn, *, user_id: int, token_id: str) -> bool:
    """Revoke one of *this user's* refresh tokens. Returns False if not found."""
    row = await conn.fetchrow(
        """
        UPDATE auth_refresh_token
           SET revoked_at = NOW(), revoke_reason = $3
         WHERE jti = $1::uuid
           AND user_id = $2
           AND revoked_at IS NULL
        RETURNING jti
        """,
        token_id, user_id, _REVOKE_USER_REVOKED,
    )
    return row is not None


# ── /me ──────────────────────────────────────────────────────────────────


async def me(conn, *, user_id: int) -> dict:
    """Return the spec-shaped /me payload.

    MD-07: `entities`, `warehouses`, `floors` reflect the user's
    *user-level* default scope (auth_user.entity / auth_user.allowed_warehouses).
    Per-permission scope is intentionally NOT aggregated here — the frontend
    treats these as a default-scope hint; the server enforces real scope via
    `check_permission` per-request. Floors have no user-level field today so
    the list is always empty (placeholder for future schema extension).
    """
    user = await conn.fetchrow("SELECT * FROM auth_user WHERE user_id = $1", user_id)
    if not user:
        raise AuthError("unauthorized", "User no longer exists", 401)

    role = await _load_role(conn, user["role_id"])
    # Multi-role: aggregate across every role the user holds. is_admin is TRUE
    # if ANY role is admin; `permissions` is the UNION across all roles; `roles`
    # lists them all (primary first).
    roles = await _effective_roles(conn, user_id, role)
    is_admin = any(r.get("is_admin") for r in roles)
    role_ids = [r["role_id"] for r in roles]

    perms_rows = []
    if role_ids:
        perms_rows = await conn.fetch(
            """
            SELECT DISTINCT p.module, p.sub_module, p.action
              FROM auth_role_permission rp
              JOIN auth_permission p ON rp.permission_id = p.permission_id
             WHERE rp.role_id = ANY($1)
             ORDER BY p.module, p.sub_module, p.action
            """,
            role_ids,
        )

    # Multi-entity assignment lives in allowed_entities (TEXT[]). Fall back
    # to the legacy single-value `entity` column when the array is unset so
    # users created before the column existed still see their assignment.
    entities = list(user.get("allowed_entities")
                    or ([user["entity"]] if user.get("entity") else []))
    warehouses = list(user.get("allowed_warehouses") or [])
    floors     = list(user.get("allowed_floors")     or [])

    return {
        "user_id": str(user["user_id"]),
        "phone": user["phone"],
        "full_name": user.get("full_name"),
        "email": user.get("email"),
        # Pre-004 schemas don't have `status` / `must_change_password`.
        # Treat missing columns as the migration's NOT NULL DEFAULTs.
        "status": (user.get("status") or
                   ("active" if user.get("is_active", True) else "disabled")),
        "must_change_password": bool(user.get("must_change_password") or False),
        "is_admin": is_admin,
        "roles": _roles_payload(roles),
        "permissions": [
            {"module": r["module"], "sub_module": r["sub_module"], "action": r["action"]}
            for r in perms_rows
        ],
        "entities": entities,
        "warehouses": warehouses,
        "floors": floors,
        "last_login_at": _iso(user.get("last_login_at")),
        "password_changed_at": _iso(user.get("password_changed_at")),
    }


# ── /password/change ──────────────────────────────────────────────────────


async def change_password(
    conn, *,
    user_id: int,
    old_password: str,
    new_password: str,
    confirm_password: str,
    current_refresh_jti: str | None,
) -> dict:
    if new_password != confirm_password:
        raise AuthError(
            "password_mismatch",
            "new_password does not match confirm_password",
            400,
        )

    user = await conn.fetchrow(
        "SELECT user_id, phone, password_encrypted FROM auth_user WHERE user_id = $1",
        user_id,
    )
    if not user:
        raise AuthError("unauthorized", "User no longer exists", 401)

    if not password_rules.verify(old_password, user["password_encrypted"]):
        raise AuthError("invalid_old_password", "Current password is incorrect", 401)

    failed = password_rules.evaluate(new_password, user["phone"])
    if failed:
        raise AuthError(
            "weak_password",
            "Password does not meet strength requirements",
            400,
            details={"rules": failed},
        )

    new_hash = password_rules.hash_password(new_password)
    revoked_count = 0
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE auth_user
               SET password_encrypted   = $2,
                   password_changed_at  = NOW(),
                   must_change_password = FALSE
             WHERE user_id = $1
            """,
            user_id, new_hash,
        )
        # Revoke every active refresh token EXCEPT the current one
        if current_refresh_jti:
            row = await conn.fetchrow(
                """
                WITH revoked AS (
                    UPDATE auth_refresh_token
                       SET revoked_at = NOW(), revoke_reason = $3
                     WHERE user_id = $1
                       AND jti <> $2::uuid
                       AND revoked_at IS NULL
                       AND rotated_at IS NULL
                    RETURNING jti
                )
                SELECT COUNT(*) AS n FROM revoked
                """,
                user_id, current_refresh_jti, _REVOKE_PASSWORD_CHANGE,
            )
        else:
            row = await conn.fetchrow(
                """
                WITH revoked AS (
                    UPDATE auth_refresh_token
                       SET revoked_at = NOW(), revoke_reason = $2
                     WHERE user_id = $1
                       AND revoked_at IS NULL
                       AND rotated_at IS NULL
                    RETURNING jti
                )
                SELECT COUNT(*) AS n FROM revoked
                """,
                user_id, _REVOKE_PASSWORD_CHANGE,
            )
        revoked_count = int(row["n"]) if row else 0

    return {
        "message": "Password changed successfully. All sessions revoked.",
        "revoked_count": revoked_count,
    }


# ── /sessions ─────────────────────────────────────────────────────────────


async def list_sessions(conn, *, user_id: int, current_refresh_jti: str | None) -> dict:
    rows = await conn.fetch(
        """
        SELECT jti, issued_at, expires_at, ip, user_agent, device_info
          FROM auth_refresh_token
         WHERE user_id = $1
           AND revoked_at IS NULL
           AND rotated_at IS NULL
           AND expires_at > NOW()
         ORDER BY issued_at DESC
        """,
        user_id,
    )
    sessions = [
        {
            "token_id": str(r["jti"]),
            "token_type": "refresh",
            "issued_at": _iso(r["issued_at"]),
            "expires_at": _iso(r["expires_at"]),
            "ip": r["ip"],
            "user_agent": r["user_agent"],
            "device_info": _parse_device_info(r["device_info"]),
            "is_current": current_refresh_jti is not None and str(r["jti"]) == current_refresh_jti,
        }
        for r in rows
    ]
    return {"sessions": sessions, "total": len(sessions)}


# ── shared validate_session for require_permission middleware ─────────────


async def validate_session(conn, token: str) -> dict | None:
    """Validate a JWT access token and hydrate the user.

    Returns a user-dict shaped the same way the legacy implementation did so
    `app.modules.auth.middleware.require_permission` keeps working.
    Returns None on any failure (the middleware turns that into 401).

    HI-05 trade-off: re-loads the user row on EVERY protected request. This is
    deliberate — it lets revocation (admin demote, account disable) take
    effect immediately rather than waiting for the access token's 15-min
    expiry. The cost is one round-trip per protected call. If you change
    that, update the security model documented in `permission_service`.
    """
    if not token:
        return None
    try:
        payload = jwt_service.verify_access(token)
    except AuthError:
        return None

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        return None

    row = await conn.fetchrow(
        """
        SELECT u.user_id, u.phone, u.full_name, u.email, u.entity, u.role_id,
               u.is_active, u.status, u.must_change_password,
               u.allowed_warehouses, u.allowed_floors, u.allowed_entities,
               r.role_name, r.is_admin
          FROM auth_user u
          LEFT JOIN auth_role r ON u.role_id = r.role_id
         WHERE u.user_id = $1
        """,
        user_id,
    )
    if not row or not row["is_active"]:
        return None
    if row["status"] and row["status"] != "active":
        return None

    # Multi-role: a user can hold several roles at once. The full set lives in
    # auth_user_role; auth_user.role_id is the primary/display role. is_admin
    # is TRUE if ANY assigned role is admin, and `role_ids` carries the whole
    # set so the permission layer can grant on ANY role. Resolved here (not in
    # the JWT) so role changes take effect immediately — same HI-05 rationale.
    primary_role = (
        {"role_id": row["role_id"], "role_name": row["role_name"],
         "is_admin": row["is_admin"], "description": None}
        if row["role_id"] is not None else None
    )
    roles = await _effective_roles(conn, row["user_id"], primary_role)
    role_ids = [r["role_id"] for r in roles]
    is_admin = any(r["is_admin"] for r in roles) or bool(row["is_admin"])

    return {
        "user_id": row["user_id"],
        "phone": row["phone"],
        "full_name": row["full_name"],
        "email": row["email"],
        "entity": row["entity"],
        "role_id": row["role_id"],
        "role_name": row["role_name"],
        # Full role set (multi-role) + aggregated admin flag. Middleware /
        # permission_service gate on `role_ids` (grant if ANY role allows).
        "role_ids": role_ids,
        "is_admin": is_admin,
        # User-level scope defaults. Middleware uses these to gate any
        # request that carries an ?entity= / ?warehouse= / ?floor= query param.
        # `allowed_entities` falls back to [entity] for users created before
        # the multi-entity column existed, so legacy single-entity assignments
        # keep working without backfill.
        "allowed_entities":   list(row["allowed_entities"] or ([row["entity"]] if row["entity"] else [])),
        "allowed_warehouses": list(row["allowed_warehouses"] or []),
        "allowed_floors":     list(row["allowed_floors"]     or []),
        # Legacy callers used `session_id`. Keep the key but fill with the
        # refresh token jti that minted this access — closest equivalent.
        "session_id": payload.get("parent_jti"),
    }


# ── shared get_user_permissions (kept for legacy callers) ────────────────


async def get_user_permissions(conn, role_id: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT p.module, p.sub_module, p.sub_sub_module, p.action, p.description,
               rp.allowed_entities, rp.allowed_warehouses, rp.allowed_floors
          FROM auth_role_permission rp
          JOIN auth_permission p ON rp.permission_id = p.permission_id
         WHERE rp.role_id = $1
         ORDER BY p.module, p.sub_module, p.action
        """,
        role_id,
    )
    return [dict(r) for r in rows]


# ── shared create_user (used by admin /users POST) ───────────────────────


async def create_user(conn, phone: str, password: str, full_name: str,
                      role_id: int, email: str | None = None,
                      entity: str | None = None,
                      allowed_warehouses: list[str] | None = None,
                      allowed_floors:     list[str] | None = None,
                      allowed_entities:   list[str] | None = None,
                      must_change_password: bool = False,
                      role_ids: list[int] | None = None) -> dict:
    """Create a user with a bcrypt-hashed password, normalized phone.

    HI-06: callers MUST validate `password` via `password_rules.evaluate`
    before invoking — the router does this. This helper does NOT re-validate
    so admin tooling can still seed accounts deliberately, but any new HTTP
    surface that reaches this function should run the rules first.

    If `allowed_entities` is given, the legacy single-value `entity` column
    is set to the first element so older queries that read it stay happy.

    `must_change_password=True` flags the new account so the next login
    redirects to the force-change-password screen (mig 004 column).
    """
    norm = normalize_phone(phone) or phone
    hashed = password_rules.hash_password(password)
    # Keep the legacy single-value `entity` in sync with allowed_entities[0]
    # so reads against the old column still resolve a sensible default.
    effective_entity = entity
    if allowed_entities:
        effective_entity = allowed_entities[0]
    # Multi-role: `role_id` is the primary/display role; `role_ids` is the full
    # set to mirror into auth_user_role. Default the set to just the primary so
    # single-role callers (seed tooling) populate the join consistently. Filter
    # None (incl. a None primary) so a role-less user still creates cleanly —
    # auth_user_role.role_id is NOT NULL.
    full_role_ids = list(dict.fromkeys(
        r for r in ([role_id, *(role_ids or [])]) if r is not None
    ))
    async with conn.transaction():
        user_id = await conn.fetchval(
            """
            INSERT INTO auth_user
                (phone, password_encrypted, full_name, email, role_id, entity,
                 allowed_warehouses, allowed_floors, allowed_entities,
                 must_change_password, password_changed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            RETURNING user_id
            """,
            norm, hashed, full_name, email, role_id, effective_entity,
            allowed_warehouses, allowed_floors, allowed_entities,
            must_change_password,
        )
        for rid in full_role_ids:
            await conn.execute(
                "INSERT INTO auth_user_role (user_id, role_id) VALUES ($1, $2) "
                "ON CONFLICT (user_id, role_id) DO NOTHING",
                user_id, rid,
            )
    return {
        "user_id": user_id, "phone": norm, "full_name": full_name,
        "role_id": role_id, "role_ids": full_role_ids,
        "must_change_password": must_change_password,
    }


# ── admin reset-password (POST /users/{user_id}/reset-password) ──────────


async def admin_reset_password(
    pool, *, target_user_id: int, new_password: str, actor_user_id: str,
) -> dict:
    """Admin-only password reset for another user.

    Side-effects (atomic):
        1. Validate `new_password` against the same policy as /password/change.
           Failure → 400 weak_password with details.rules.
        2. Update auth_user: hash new password, password_changed_at = NOW(),
           must_change_password = TRUE, status='active', failed_login_count=0,
           locked_until = NULL.
        3. Revoke ALL the target user's live refresh tokens with
           revoke_reason='admin_revoked'. Returns the count.

    The caller must be admin; the router enforces that via _require_admin.
    """
    async with pool.acquire() as conn:
        target = await conn.fetchrow(
            "SELECT user_id, phone FROM auth_user WHERE user_id = $1",
            target_user_id,
        )
        if target is None:
            raise AuthError("user_not_found", "User not found", 404)

        failed = password_rules.evaluate(new_password, target["phone"])
        if failed:
            raise AuthError(
                "weak_password",
                "Password does not meet strength requirements",
                400,
                details={"rules": failed},
            )

        new_hash = password_rules.hash_password(new_password)

        async with conn.transaction():
            await conn.execute(
                """
                UPDATE auth_user
                   SET password_encrypted    = $2,
                       password_changed_at   = NOW(),
                       must_change_password  = TRUE,
                       status                = 'active',
                       failed_login_count    = 0,
                       locked_until          = NULL
                 WHERE user_id = $1
                """,
                target_user_id, new_hash,
            )
            row = await conn.fetchrow(
                """
                WITH revoked AS (
                    UPDATE auth_refresh_token
                       SET revoked_at = NOW(), revoke_reason = $2
                     WHERE user_id = $1
                       AND revoked_at IS NULL
                       AND rotated_at IS NULL
                    RETURNING jti
                )
                SELECT COUNT(*) AS n FROM revoked
                """,
                target_user_id, _REVOKE_ADMIN,
            )
            revoked_count = int(row["n"]) if row else 0

    logger.info(
        "auth.admin_reset_password actor_user_id=%s target_user_id=%s revoked_count=%s self_reset=%s",
        actor_user_id, target_user_id, revoked_count,
        str(actor_user_id) == str(target_user_id),
    )
    return {
        "user_id": str(target_user_id),
        "message": "Password reset. User must change on next login.",
        "revoked_count": revoked_count,
        "temp_password_set": True,
    }


# ── OTP-based self-service reset (POST /password/reset/*) ────────────────
#
# Replaces the admin-only reset path for end-users who forgot their password.
# We deliberately do NOT call password_rules.evaluate here — the product
# decision (2026-05-30) is that anything the user types is accepted, including
# the previous password. Validation surface is the OTP, not the password.


async def reset_password_with_otp(
    conn, *, user_id: int, new_password: str,
) -> int:
    """Set a new password on `user_id`, clear lockout state, and revoke
    every live refresh token. Returns the revoked-token count. The OTP row
    deletion is left to the router so a single transaction can wrap both.
    """
    new_hash = password_rules.hash_password(new_password)
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE auth_user
               SET password_encrypted    = $2,
                   password_changed_at   = NOW(),
                   must_change_password  = FALSE,
                   status                = 'active',
                   failed_login_count    = 0,
                   locked_until          = NULL
             WHERE user_id = $1
            """,
            user_id, new_hash,
        )
        row = await conn.fetchrow(
            """
            WITH revoked AS (
                UPDATE auth_refresh_token
                   SET revoked_at = NOW(), revoke_reason = $2
                 WHERE user_id = $1
                   AND revoked_at IS NULL
                   AND rotated_at IS NULL
                RETURNING jti
            )
            SELECT COUNT(*) AS n FROM revoked
            """,
            user_id, _REVOKE_PASSWORD_CHANGE,
        )
        # Erase the OTP row inside the same transaction so a crash between
        # password-write and OTP-delete can't leave the just-used OTP usable.
        await conn.execute("DELETE FROM auth_password_reset_otp WHERE user_id = $1", user_id)
    return int(row["n"]) if row else 0


async def find_user_by_phone_public(conn, raw_phone: str) -> dict | None:
    """Public wrapper around the private _find_user_by_phone for the OTP
    router — kept private internally so the public surface stays minimal.
    """
    return await _find_user_by_phone(conn, raw_phone)
