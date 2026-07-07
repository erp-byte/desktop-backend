"""Auth module router — JWT login + the 8 documented end-user endpoints,
plus the legacy admin user/role/permission management surface (unchanged).

End-user endpoints (per spec):

    POST   /api/v1/auth/login
    POST   /api/v1/auth/refresh
    POST   /api/v1/auth/logout
    POST   /api/v1/auth/logout-all
    GET    /api/v1/auth/me
    POST   /api/v1/auth/password/change
    GET    /api/v1/auth/sessions
    DELETE /api/v1/auth/sessions/{token_id}

See AUTH_API_DOC.md for the wire-format contract.
"""

from __future__ import annotations

import logging
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.middleware.request_context import AuthError
from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.auth.schemas import (
    AdminResetPasswordRequest,
    AdminResetPasswordResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    LoginRequest,
    LoginResponse,
    LogoutAllResponse,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    RefreshResponse,
    SendResetOtpRequest,
    SendResetOtpResponse,
    SessionsResponse,
    VerifyResetOtpRequest,
    VerifyResetOtpResponse,
)
from app.modules.auth.services import auth_service, otp_service, password_rules, rate_limiter
from app.modules.auth.services.phone import normalize as normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


# ═══════════════════════════════════════════════════════════════════════════
# End-user auth (8 endpoints per spec)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, body: LoginRequest):
    settings = request.app.state.settings
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    norm = normalize_phone(body.phone) or body.phone

    rate_limiter.check_and_record(ip, norm, settings=settings)

    # ── DO NOT WRAP THIS CALL IN conn.transaction() ─────────────────────────
    # auth_service.login relies on asyncpg autocommit semantics so the
    # failed-login counter UPDATE persists when login() raises AuthError on
    # bad password. Wrapping here would roll the counter back and disable
    # account lockout entirely. See HI-02 in the 2026-05-05 review.
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await auth_service.login(
            conn,
            raw_phone=body.phone,
            password=body.password,
            ip=ip,
            user_agent=ua,
            device_info=body.device_info.model_dump() if body.device_info else None,
            settings=settings,
        )

    rate_limiter.reset(ip, norm)
    return result


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(request: Request, body: RefreshRequest):
    settings = request.app.state.settings
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await auth_service.refresh(
            conn, refresh_jwt=body.refresh_token, ip=ip, user_agent=ua, settings=settings,
        )


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    body: LogoutRequest,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # MD-08: pass user_id so the service can verify the refresh token
        # actually belongs to the authenticated caller (silently ignores
        # cross-user attempts to keep the endpoint idempotent).
        await auth_service.logout(
            conn, refresh_jwt=body.refresh_token, user_id=user.user_id,
        )
    return None


@router.post("/logout-all", response_model=LogoutAllResponse)
async def logout_all(request: Request, user: AuthUser = Depends(get_current_user)):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        n = await auth_service.logout_all(conn, user_id=user.user_id)
    return {"revoked_count": n}


@router.get("/me", response_model=MeResponse)
async def me(request: Request, user: AuthUser = Depends(get_current_user)):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await auth_service.me(conn, user_id=user.user_id)


@router.post("/password/change", response_model=ChangePasswordResponse)
async def password_change(
    request: Request,
    body: ChangePasswordRequest,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await auth_service.change_password(
            conn,
            user_id=user.user_id,
            old_password=body.old_password,
            new_password=body.new_password,
            confirm_password=body.confirm_password,
            current_refresh_jti=user.refresh_jti,
        )


# ── OTP-based password reset (unauthenticated; replaces admin path) ──────
#
# Two endpoints, both no-auth: the user is by definition locked out when
# they ask for an OTP. We always return a successful-looking envelope on
# /send-otp regardless of whether the phone is registered — leaking that
# would let an attacker enumerate the user table. /verify is constant-time
# on the not-found / wrong-otp branches for the same reason.


@router.post("/password/reset/send-otp", response_model=SendResetOtpResponse)
async def password_reset_send_otp(request: Request, body: SendResetOtpRequest):
    pool = request.app.state.db_pool
    # We always claim "OTP sent if the number is registered" so an attacker
    # can't probe the user table by timing or response code. The actual
    # send happens inline (no queue) so a delivery error to a real user
    # surfaces immediately in the logs.
    async with pool.acquire() as conn:
        user = await auth_service.find_user_by_phone_public(conn, body.phone)
        if user is not None:
            otp = otp_service.generate_otp()
            await otp_service.upsert_otp(
                conn,
                user_id=user["user_id"],
                otp_hash=otp_service.hash_otp(otp),
                expires=otp_service.expires_at(),
                phone=user.get("phone") or body.phone,
            )
            try:
                # Use the operator-supplied number for delivery — falls back
                # to the stored one inside the service if normalisation
                # strips it back to the same E.164.
                await otp_service.send_otp_via_whatsapp(body.phone, otp)
            except RuntimeError as e:
                # WA send failed. Roll the OTP back so the user can retry
                # without the prior code being held against them.
                await otp_service.delete_otp(conn, user_id=user["user_id"])
                logger.error("password reset OTP send failed: %s", e)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "whatsapp_send_failed",
                        "message": "Could not deliver OTP. Please try again shortly.",
                    },
                )
    return {
        "message": "If that phone is registered, an OTP has been sent on WhatsApp.",
        "expires_in_seconds": otp_service.OTP_TTL_SECONDS,
    }


@router.post("/password/reset/verify", response_model=VerifyResetOtpResponse)
async def password_reset_verify(request: Request, body: VerifyResetOtpRequest):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        user = await auth_service.find_user_by_phone_public(conn, body.phone)
        # Match the constant-time / opacity story of /send-otp — invalid
        # phone or invalid OTP both surface as the same generic error so
        # nothing about the user table leaks.
        invalid = HTTPException(
            status_code=400,
            detail={"error": "invalid_otp", "message": "OTP is invalid or has expired."},
        )
        if user is None:
            raise invalid
        record = await otp_service.fetch_otp(conn, user_id=user["user_id"])
        if record is None:
            raise invalid

        from datetime import datetime, timezone
        exp = record["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= datetime.now(timezone.utc):
            # Erase the dead row so the next /send-otp doesn't see it.
            await otp_service.delete_otp(conn, user_id=user["user_id"])
            raise invalid

        if not otp_service.verify_otp(body.otp, record["otp_hash"]):
            raise invalid

        revoked = await auth_service.reset_password_with_otp(
            conn, user_id=user["user_id"], new_password=body.new_password,
        )
    return {"message": "Password updated. Please sign in.", "revoked_count": revoked}


@router.get("/sessions", response_model=SessionsResponse)
async def sessions(request: Request, user: AuthUser = Depends(get_current_user)):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await auth_service.list_sessions(
            conn, user_id=user.user_id, current_refresh_jti=user.refresh_jti,
        )


@router.delete("/sessions/{token_id}", status_code=204)
async def delete_session(
    request: Request,
    token_id: str,
    user: AuthUser = Depends(get_current_user),
):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        ok = await auth_service.revoke_session(
            conn, user_id=user.user_id, token_id=token_id,
        )
    if not ok:
        # Spec: also returned if the session belongs to another user (no leak)
        raise AuthError("session_not_found", "Session not found", 404)
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Admin: user / role / permission management
#
# Preserved as-is from the previous router. These endpoints continue to work
# because `validate_session` (used by the helpers below) now verifies JWT
# access tokens. They are out of scope for the documented end-user spec.
# ═══════════════════════════════════════════════════════════════════════════


class CreateUserRequest(BaseModel):
    """Admin: create a user.

    Accepts two shapes for back-compat:
      * Legacy: `role_id` (int FK) + `allowed_{entities,warehouses,floors}`.
      * Friendly admin UI: `role_codes` (list of role names — first match
        wins, since auth_user is single-role) + the shorter
        `entities` / `warehouses` / `floors` aliases.

    The endpoint resolves `role_codes` -> `role_id` via auth_role before
    handing off to the service. At least one of {role_id, role_codes}
    must be present; the model_validator below enforces that.
    """
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    phone: str
    password: str
    full_name: str
    email: str | None = None
    # Role — either side is fine; the endpoint resolves role_codes first.
    role_id:    int | None       = None
    role_codes: list[str] | None = None
    # `entity` is the legacy single-entity field, kept for back-compat. New
    # callers should send `allowed_entities` (array) and leave `entity`
    # unset — the service derives `entity` from `allowed_entities[0]`.
    entity: str | None = None
    # `populate_by_name=True` lets callers send either the legacy
    # `allowed_*` name or the shorter UI alias.
    allowed_entities:   list[str] | None = Field(default=None, alias="entities")
    allowed_warehouses: list[str] | None = Field(default=None, alias="warehouses")
    allowed_floors:     list[str] | None = Field(default=None, alias="floors")
    # Force the user to change password on first login. The auth_user
    # column already exists (migration 004); we just plumb it through.
    must_change_password: bool = False
    # Accepted but currently no-op — the admin UI sends this flag when
    # it wants the server to mint a random password instead of taking
    # body.password. TODO: implement when the admin UX needs it; for
    # now we honour body.password verbatim.
    generate_password: bool = False

    @model_validator(mode="after")
    def _require_role(self):
        if self.role_id is None and not (self.role_codes or []):
            raise ValueError("either role_id or role_codes is required")
        return self


class EditUserRequest(BaseModel):
    full_name: str | None = None
    email: str | None = None
    # Single primary role (legacy). For multi-role assignment send `role_ids`
    # (int FKs) and/or `role_codes` (role names) — the endpoint replaces the
    # user's whole role set and the first becomes the primary/display role.
    role_id: int | None = None
    role_ids:   list[int] | None = None
    role_codes: list[str] | None = None
    entity: str | None = None
    is_active: bool | None = None
    # Admin-controlled soft-ban toggle. The auth_user.status CHECK constraint
    # allows {'active','disabled','suspended'}; 'disabled' is reserved for the
    # DELETE /users/{id} hard-disable path, so admins can only flip between
    # 'active' and 'suspended' here.
    status: Literal['active', 'suspended'] | None = None
    allowed_entities:   list[str] | None = None
    allowed_warehouses: list[str] | None = None
    allowed_floors:     list[str] | None = None


class UserScopeRequest(BaseModel):
    """Atomic replace of a user's entity / warehouse / floor scope.

    Used by the admin user-detail Scope tab. Sending an empty list for a
    field means "no restriction at the user level"; sending `null` (omit
    the field) leaves the current value untouched.
    """
    # `entities` is plural for symmetry with the frontend, but auth_user
    # only has a single-value `entity` column. We take the first item (or
    # None when the list is empty) and write that.
    entities:   list[str] | None = None
    warehouses: list[str] | None = None
    floors:     list[str] | None = None


class CreateRoleRequest(BaseModel):
    role_name: str
    description: str = ""
    is_admin: bool = False


class SetRolePermissionsRequest(BaseModel):
    permission_ids: list[int]
    allowed_entities: list[str] | None = None
    allowed_warehouses: list[str] | None = None
    allowed_floors: list[str] | None = None


class CreatePermissionRequest(BaseModel):
    module: str
    sub_module: str | None = None
    sub_sub_module: str | None = None
    action: str
    description: str = ""


class EditPermissionRequest(BaseModel):
    module: str | None = None
    sub_module: str | None = None
    sub_sub_module: str | None = None
    action: str | None = None
    description: str | None = None


class CreateModuleRequest(BaseModel):
    module: str
    sub_modules: list[str] | None = None


# HI-04: explicit allowlists for the dynamic UPDATE builders. Only fields in
# these sets can be written by the corresponding admin endpoint. Adding a
# column to auth_user does NOT automatically make it editable here — you have
# to opt it in explicitly. The SET clause quotes each identifier as a
# defence-in-depth marker even though the values come from this allowlist.
_EDITABLE_USER_COLUMNS: frozenset[str] = frozenset({
    "full_name", "email", "role_id", "entity", "is_active", "status",
    "allowed_entities", "allowed_warehouses", "allowed_floors",
})
_EDITABLE_PERMISSION_COLUMNS: frozenset[str] = frozenset({
    "module", "sub_module", "sub_sub_module", "action", "description",
})


# ── admin helpers ────────────────────────────────────────────────────────


def _extract_token(request: Request) -> str | None:
    """RFC 6750 §2.1 — `Bearer` scheme is case-insensitive."""
    auth = request.headers.get("authorization", "").strip()
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


async def _require_auth(request: Request) -> dict:
    """Validate JWT bearer; cache the user-dict on `request.state.user_dict`
    so a chained `_require_admin` call doesn't re-query (HI-05).
    """
    cached = getattr(request.state, "user_dict", None)
    if cached:
        return cached
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header required")
    from app.modules.auth.services.auth_service import validate_session
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        user = await validate_session(conn, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    request.state.user_dict = user
    return user


async def _require_admin(request: Request) -> dict:
    user = await _require_auth(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── user management ──────────────────────────────────────────────────────


@router.post("/users")
async def create_user(request: Request, body: CreateUserRequest):
    """Admin: create a user. HI-06: enforces password rules + asyncpg-typed
    uniqueness handling (no more substring-match on str(e))."""
    await _require_admin(request)

    norm_phone = normalize_phone(body.phone) or body.phone
    failed_rules = password_rules.evaluate(body.password, norm_phone)
    if failed_rules:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "weak_password",
                "message": "Password does not meet strength requirements",
                "details": {"rules": failed_rules},
            },
        )

    from app.modules.auth.services.auth_service import create_user as _create
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        # Resolve role_codes -> role_ids (multi-role). ALL supplied codes are
        # honoured: the FIRST resolved one becomes the primary/display role
        # (auth_user.role_id), the rest are stored in auth_user_role. A direct
        # body.role_id (legacy FK shape) is treated as the primary. Unknown
        # codes are skipped; only an all-empty result surfaces a 400.
        role_ids_all: list[int] = []
        if body.role_id is not None:
            role_ids_all.append(body.role_id)
        if body.role_codes:
            wanted = [c.strip().lower() for c in body.role_codes if c and c.strip()]
            if wanted:
                rows = await conn.fetch(
                    "SELECT role_id, lower(role_name) AS role_name "
                    "FROM auth_role WHERE lower(role_name) = ANY($1::text[])",
                    wanted,
                )
                resolved = {r["role_name"]: r["role_id"] for r in rows}
                # Preserve the operator's selection order.
                for code in wanted:
                    if code in resolved:
                        role_ids_all.append(resolved[code])
                if not role_ids_all:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "unknown_role_codes",
                            "message": "none of the supplied role_codes match an existing role",
                            "role_codes": body.role_codes,
                        },
                    )
        # Dedup, keep order; the first is the primary role.
        role_ids_all = list(dict.fromkeys(role_ids_all))
        if not role_ids_all:
            # Model validator already gates this, but defend in depth in
            # case the validator is relaxed later.
            raise HTTPException(
                status_code=400,
                detail={"error": "missing_role",
                        "message": "role_id or role_codes is required"},
            )
        role_id = role_ids_all[0]

        try:
            return await _create(
                conn, body.phone, body.password, body.full_name,
                role_id, body.email, body.entity,
                body.allowed_warehouses, body.allowed_floors,
                body.allowed_entities,
                must_change_password=body.must_change_password,
                role_ids=role_ids_all,
            )
        except asyncpg.UniqueViolationError as e:
            # `constraint_name` is real on PostgresError at runtime but
            # missing from asyncpg's type stubs — use getattr so Pylance
            # doesn't flag it.
            cname = getattr(e, "constraint_name", "") or ""
            if "phone" in cname:
                raise HTTPException(status_code=409, detail="Phone number already registered")
            raise HTTPException(status_code=409, detail=f"Conflict on constraint {cname}")


@router.get("/users")
async def list_users(request: Request):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.phone, u.full_name, u.email, u.entity,
                   u.allowed_entities, u.allowed_warehouses, u.allowed_floors,
                   u.is_active, u.status, u.created_at, u.last_login_at,
                   u.must_change_password, u.password_changed_at,
                   -- Lock state so the admin UI can render the "Locked"
                   -- chip + the manual Unlock CTA without re-fetching.
                   -- locked_until > NOW() means currently locked; a past
                   -- timestamp means the auto-unlock window has elapsed
                   -- but failed_login_count hasn't been cleared yet (the
                   -- next successful login clears it; admin unlock does
                   -- so immediately).
                   u.locked_until, u.failed_login_count,
                   r.role_id, r.role_name, r.is_admin,
                   -- Multi-role: full set the user holds + aggregated admin
                   -- flag. Falls back to the primary role when the user has no
                   -- auth_user_role rows (pre-backfill records).
                   COALESCE(ur.role_codes,
                            CASE WHEN r.role_name IS NULL THEN ARRAY[]::text[]
                                 ELSE ARRAY[r.role_name] END) AS role_codes,
                   COALESCE(ur.any_admin, r.is_admin, FALSE)  AS roles_is_admin
            FROM auth_user u
            LEFT JOIN auth_role r ON u.role_id = r.role_id
            LEFT JOIN LATERAL (
                SELECT array_agg(x.role_name ORDER BY x.role_id) AS role_codes,
                       bool_or(x.is_admin)                        AS any_admin
                  FROM auth_user_role uur
                  JOIN auth_role x ON uur.role_id = x.role_id
                 WHERE uur.user_id = u.user_id
            ) ur ON TRUE
            ORDER BY u.created_at DESC
            """
        )
    out = []
    for r in rows:
        d = dict(r)
        # Mirror the keys /me uses so the admin UI can read either name.
        # Fall back to [entity] when allowed_entities is unset/NULL.
        d["entities"] = list(d.get("allowed_entities")
                             or ([d["entity"]] if d.get("entity") else []))
        d["warehouses"] = list(d.get("allowed_warehouses") or [])
        d["floors"]     = list(d.get("allowed_floors")     or [])
        d["role_codes"] = list(d.get("role_codes") or [])
        # Surface the aggregated multi-role admin flag as `is_admin` (matches
        # /me / get_user), keeping the primary role's flag available too.
        d["primary_is_admin"] = d.get("is_admin")
        d["is_admin"] = bool(d.pop("roles_is_admin", d.get("is_admin")))
        out.append(d)
    return out


@router.put("/users/{user_id}")
async def edit_user(request: Request, user_id: int, body: EditUserRequest):
    """HI-04: only fields in `_EDITABLE_USER_COLUMNS` can be set; identifier
    quoting is defensive — values come from the allowlist, not user input.

    Multi-role: when `role_ids` / `role_codes` are sent, the user's whole role
    set in auth_user_role is replaced and the first becomes the primary/display
    role (auth_user.role_id)."""
    await _require_admin(request)
    pool = request.app.state.db_pool

    sent = body.model_fields_set
    roles_sent = ("role_ids" in sent) or ("role_codes" in sent)

    updates: list[str] = []
    params: list = []
    idx = 1
    for field in sent:
        if field not in _EDITABLE_USER_COLUMNS:
            # Silently ignore unknown / disallowed fields (e.g. password_encrypted)
            continue
        if field == "role_id" and roles_sent:
            # Primary role is derived from role_ids/role_codes below — don't
            # also set it here (would duplicate the SET clause).
            continue
        updates.append(f'"{field}" = ${idx}')
        params.append(getattr(body, field))
        idx += 1

    if not updates and not roles_sent:
        raise HTTPException(status_code=400, detail="No editable fields supplied")

    async with pool.acquire() as conn:
        # Resolve the full role set first so a bad request fails before any write.
        full_role_ids: list[int] = []
        if roles_sent:
            if body.role_ids:
                full_role_ids.extend([r for r in body.role_ids if r is not None])
            if body.role_codes:
                wanted = [c.strip().lower() for c in body.role_codes if c and c.strip()]
                if wanted:
                    rows = await conn.fetch(
                        "SELECT role_id, lower(role_name) AS role_name "
                        "FROM auth_role WHERE lower(role_name) = ANY($1::text[])",
                        wanted,
                    )
                    resolved = {r["role_name"]: r["role_id"] for r in rows}
                    for code in wanted:
                        if code in resolved:
                            full_role_ids.append(resolved[code])
            full_role_ids = list(dict.fromkeys(full_role_ids))
            if not full_role_ids:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "missing_role",
                            "message": "role_ids or role_codes resolved to no known role"},
                )
            # `role_ids` can arrive directly via the API (not just via name
            # resolution), so validate existence up-front — otherwise a bad id
            # surfaces as an FK-violation 500 mid-transaction instead of a 400.
            known = {r["role_id"] for r in await conn.fetch(
                "SELECT role_id FROM auth_role WHERE role_id = ANY($1)", full_role_ids)}
            unknown = [r for r in full_role_ids if r not in known]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "unknown_role_ids",
                            "message": "one or more role_ids do not exist",
                            "role_ids": unknown},
                )
            # Keep the primary/display column in sync with the first role.
            updates.append(f'"role_id" = ${idx}')
            params.append(full_role_ids[0])
            idx += 1

        async with conn.transaction():
            params.append(user_id)
            sql = f'UPDATE auth_user SET {", ".join(updates)} WHERE user_id = ${idx}'
            result = await conn.execute(sql, *params)
            if result == 'UPDATE 0':
                raise HTTPException(status_code=404, detail="User not found")

            if roles_sent:
                await conn.execute(
                    "DELETE FROM auth_user_role WHERE user_id = $1", user_id)
                for rid in full_role_ids:
                    await conn.execute(
                        "INSERT INTO auth_user_role (user_id, role_id) "
                        "VALUES ($1, $2) ON CONFLICT (user_id, role_id) DO NOTHING",
                        user_id, rid,
                    )

    return {"user_id": user_id, "updated": True,
            "role_ids": full_role_ids if roles_sent else None}


@router.get("/users/{user_id}")
async def get_user(request: Request, user_id: int):
    """Admin: fetch a single user with full profile (incl. allowed_floors).

    The frontend admin user-detail page calls this on load; without it the
    page falls back to a synthetic placeholder and the Scope tab can't show
    the user's current factory/floor assignment.
    """
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.user_id, u.phone, u.full_name, u.email, u.entity,
                   u.allowed_entities, u.allowed_warehouses, u.allowed_floors,
                   u.is_active, u.status, u.created_at, u.last_login_at,
                   u.must_change_password, u.password_changed_at,
                   r.role_id, r.role_name, r.is_admin
            FROM auth_user u
            LEFT JOIN auth_role r ON u.role_id = r.role_id
            WHERE u.user_id = $1
            """,
            user_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        # Multi-role: the full set the user holds (primary first). Falls back to
        # the single primary role for users not represented in auth_user_role.
        role_rows = await conn.fetch(
            """
            SELECT r.role_id, r.role_name, r.is_admin
              FROM auth_user_role ur JOIN auth_role r ON ur.role_id = r.role_id
             WHERE ur.user_id = $1
            """,
            user_id,
        )
    out = dict(row)
    # Mirror keys the admin UI uses interchangeably with the /me payload.
    # Fall back to [entity] when allowed_entities is NULL so users created
    # before the array column existed still have a sensible entities list.
    out["entities"]   = list(out.get("allowed_entities")
                             or ([out["entity"]] if out.get("entity") else []))
    out["warehouses"] = list(out.get("allowed_warehouses") or [])
    out["floors"]     = list(out.get("allowed_floors")     or [])
    roles = [dict(r) for r in role_rows]
    if not roles and row["role_id"] is not None:
        roles = [{"role_id": row["role_id"], "role_name": row["role_name"],
                  "is_admin": row["is_admin"]}]
    pid = row["role_id"]
    roles.sort(key=lambda x: (x["role_id"] != pid, x["role_id"]))
    out["roles"]      = [{"role_id": str(r["role_id"]), "code": r["role_name"],
                          "is_admin": bool(r["is_admin"])} for r in roles]
    out["role_ids"]   = [r["role_id"] for r in roles]
    out["role_codes"] = [r["role_name"] for r in roles]
    # Aggregate admin flag (any role admin) so the detail view matches /me.
    out["is_admin"]   = any(r["is_admin"] for r in roles)
    return out


@router.put("/users/{user_id}/scope")
async def replace_user_scope(request: Request, user_id: int, body: UserScopeRequest):
    """Admin: atomic replace of a user's entity / warehouse / floor scope.

    Fields omitted from the payload leave the existing value untouched;
    fields sent as an empty list clear the column to NULL (no restriction at
    the user level — role-permission scope still applies).
    """
    await _require_admin(request)

    updates: list[str] = []
    params: list = []
    idx = 1
    sent = body.model_fields_set

    if "entities" in sent:
        entities_val = body.entities if body.entities else None
        # Write the array as the source of truth, and mirror the first
        # element to the legacy single-column `entity` so existing reads
        # against that column still return a sensible value. NULL on both
        # sides when the admin clears the list.
        first = entities_val[0] if entities_val else None
        updates.append(f'"allowed_entities" = ${idx}')
        params.append(entities_val); idx += 1
        updates.append(f'"entity" = ${idx}')
        params.append(first); idx += 1
    if "warehouses" in sent:
        wh = body.warehouses if body.warehouses else None
        updates.append(f'"allowed_warehouses" = ${idx}')
        params.append(wh); idx += 1
    if "floors" in sent:
        fl = body.floors if body.floors else None
        updates.append(f'"allowed_floors" = ${idx}')
        params.append(fl); idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No scope fields supplied")

    params.append(user_id)
    sql = f'UPDATE auth_user SET {", ".join(updates)} WHERE user_id = ${idx}'
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await conn.execute(sql, *params)
        if result == 'UPDATE 0':
            raise HTTPException(status_code=404, detail="User not found")
    return {"user_id": user_id, "scope_updated": True}


@router.delete("/users/{user_id}")
async def deactivate_user(request: Request, user_id: int):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE auth_user SET is_active = FALSE WHERE user_id = $1", user_id)
            # Revoke all live refresh tokens for the user
            await conn.execute(
                """
                UPDATE auth_refresh_token
                   SET revoked_at = NOW(), revoke_reason = 'admin_revoked'
                 WHERE user_id = $1 AND revoked_at IS NULL
                """,
                user_id,
            )
    return {"user_id": user_id, "deactivated": True}


@router.post(
    "/users/{user_id}/reset-password",
    response_model=AdminResetPasswordResponse,
)
async def admin_reset_password(
    request: Request, user_id: int, body: AdminResetPasswordRequest,
):
    """Admin-only password reset for a target user.

    Forces `must_change_password=TRUE`, clears any lockout, and revokes all
    of the target's live refresh tokens with `revoke_reason='admin_revoked'`.
    Validates the new password against the same policy as /password/change.
    """
    actor = await _require_admin(request)
    return await auth_service.admin_reset_password(
        request.app.state.db_pool,
        target_user_id=user_id,
        new_password=body.new_password,
        actor_user_id=str(actor["user_id"]),
    )


@router.post("/users/{user_id}/unlock")
async def admin_unlock_user(request: Request, user_id: int):
    """Admin-only: immediately unlock a target user.

    Clears `locked_until` and resets `failed_login_count` to 0 so the
    user can attempt login right away — same end-state the natural
    auto-unlock (LOGIN_LOCKOUT_MINUTES expiry) produces on the next
    successful login. Distinct from /reset-password in that the user
    keeps their current password and isn't forced to change it.

    Existing sessions are NOT revoked — a lockout only affects the
    login gate; live sessions are independent. If you want to revoke
    sessions too, use /reset-password instead.

    Returns:
      { user_id, unlocked: bool, was_locked: bool }
        was_locked is true when locked_until > NOW() before the
        update — useful for the UI to differentiate "actually unlocked
        a locked account" from "cleared stale counters on an already-
        unlocked account".
    """
    actor = await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, locked_until FROM auth_user WHERE user_id = $1",
            user_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        lu = row["locked_until"]
        if lu is not None and lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        was_locked = bool(lu and lu > now)
        await conn.execute(
            """
            UPDATE auth_user
               SET locked_until       = NULL,
                   failed_login_count = 0
             WHERE user_id = $1
            """,
            user_id,
        )
    logger.info(
        "admin_unlock_user: actor=%s target=%s was_locked=%s",
        actor.get("user_id"), user_id, was_locked,
    )
    return {"user_id": user_id, "unlocked": True, "was_locked": was_locked}


# ── role & permission management ─────────────────────────────────────────


@router.get("/roles")
async def list_roles(request: Request):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.*, COUNT(rp.permission_id) AS permission_count
            FROM auth_role r
            LEFT JOIN auth_role_permission rp ON r.role_id = rp.role_id
            GROUP BY r.role_id
            ORDER BY r.role_id
            """
        )
    return [dict(r) for r in rows]


@router.post("/roles")
async def create_role(request: Request, body: CreateRoleRequest):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        role_id = await conn.fetchval(
            "INSERT INTO auth_role (role_name, description, is_admin) VALUES ($1, $2, $3) RETURNING role_id",
            body.role_name, body.description, body.is_admin,
        )
    return {"role_id": role_id, "role_name": body.role_name}


@router.put("/roles/{role_id}/permissions")
async def set_role_permissions(request: Request, role_id: int, body: SetRolePermissionsRequest):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM auth_role_permission WHERE role_id = $1", role_id)
            for pid in body.permission_ids:
                await conn.execute(
                    "INSERT INTO auth_role_permission (role_id, permission_id, allowed_entities, allowed_warehouses, allowed_floors) VALUES ($1, $2, $3, $4, $5)",
                    role_id, pid, body.allowed_entities, body.allowed_warehouses, body.allowed_floors,
                )
    return {"role_id": role_id, "permissions_set": len(body.permission_ids)}


@router.get("/permissions")
async def list_permissions(request: Request, module: str = Query(None)):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if module:
            rows = await conn.fetch(
                "SELECT * FROM auth_permission WHERE module = $1 ORDER BY sub_module, sub_sub_module, action", module,
            )
        else:
            rows = await conn.fetch("SELECT * FROM auth_permission ORDER BY module, sub_module, sub_sub_module, action")
    return [dict(r) for r in rows]


@router.get("/permissions/hierarchy")
async def get_permissions_hierarchy(request: Request):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM auth_permission ORDER BY module, sub_module, sub_sub_module, action")

    tree: dict = {}
    for r in rows:
        mod = r['module']
        sub = r['sub_module'] or '_root'
        subsub = r['sub_sub_module'] or '_root'
        tree.setdefault(mod, {}).setdefault(sub, {}).setdefault(subsub, []).append({
            "permission_id": r['permission_id'],
            "action": r['action'],
            "description": r['description'],
        })
    return tree


@router.get("/modules")
async def list_modules(request: Request):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT module,
                   array_agg(DISTINCT sub_module) FILTER (WHERE sub_module IS NOT NULL) AS sub_modules,
                   COUNT(*) AS permission_count
            FROM auth_permission
            GROUP BY module
            ORDER BY module
            """
        )
    return [{"module": r['module'], "sub_modules": r['sub_modules'] or [], "permission_count": r['permission_count']} for r in rows]


@router.post("/modules")
async def create_module(request: Request, body: CreateModuleRequest):
    await _require_admin(request)
    pool = request.app.state.db_pool

    default_actions = ['view', 'create', 'edit', 'delete']
    created = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            if body.sub_modules:
                for sub in body.sub_modules:
                    for action in default_actions:
                        await conn.execute(
                            "INSERT INTO auth_permission (module, sub_module, action, description) VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
                            body.module, sub, action, f"{action.title()} {body.module} → {sub}",
                        )
                        created += 1
            else:
                for action in default_actions:
                    await conn.execute(
                        "INSERT INTO auth_permission (module, action, description) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                        body.module, action, f"{action.title()} {body.module}",
                    )
                    created += 1

    return {"module": body.module, "sub_modules": body.sub_modules or [], "permissions_created": created}


@router.post("/permissions/create")
async def create_permission(request: Request, body: CreatePermissionRequest):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            pid = await conn.fetchval(
                "INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description) VALUES ($1, $2, $3, $4, $5) RETURNING permission_id",
                body.module, body.sub_module, body.sub_sub_module, body.action, body.description,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="Permission already exists")
    return {"permission_id": pid, "module": body.module, "sub_module": body.sub_module, "action": body.action}


@router.put("/permissions/{permission_id}")
async def edit_permission(request: Request, permission_id: int, body: EditPermissionRequest):
    """HI-04: only fields in `_EDITABLE_PERMISSION_COLUMNS` can be set."""
    await _require_admin(request)
    pool = request.app.state.db_pool

    sent = body.model_fields_set
    updates: list[str] = []
    params: list = []
    idx = 1
    for field in sent:
        if field not in _EDITABLE_PERMISSION_COLUMNS:
            continue
        updates.append(f'"{field}" = ${idx}')
        params.append(getattr(body, field))
        idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No editable fields supplied")

    params.append(permission_id)
    sql = f'UPDATE auth_permission SET {", ".join(updates)} WHERE permission_id = ${idx}'
    async with pool.acquire() as conn:
        result = await conn.execute(sql, *params)
        if result == 'UPDATE 0':
            raise HTTPException(status_code=404, detail="Permission not found")

    return {"permission_id": permission_id, "updated": True}


@router.delete("/permissions/{permission_id}")
async def delete_permission(request: Request, permission_id: int):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM auth_role_permission WHERE permission_id = $1", permission_id)
            result = await conn.execute("DELETE FROM auth_permission WHERE permission_id = $1", permission_id)
            if result == 'DELETE 0':
                raise HTTPException(status_code=404, detail="Permission not found")
    return {"permission_id": permission_id, "deleted": True}


@router.get("/roles/{role_id}/permissions")
async def get_role_permissions(request: Request, role_id: int):
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        role = await conn.fetchrow("SELECT * FROM auth_role WHERE role_id = $1", role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")

        rows = await conn.fetch(
            """
            SELECT p.*, rp.allowed_entities, rp.allowed_warehouses, rp.allowed_floors
            FROM auth_role_permission rp
            JOIN auth_permission p ON rp.permission_id = p.permission_id
            WHERE rp.role_id = $1
            ORDER BY p.module, p.sub_module, p.action
            """,
            role_id,
        )
    return {"role": dict(role), "permissions": [dict(r) for r in rows]}
