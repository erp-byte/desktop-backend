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

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

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
    SessionsResponse,
)
from app.modules.auth.services import auth_service, password_rules, rate_limiter
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
    phone: str
    password: str
    full_name: str
    role_id: int
    email: str | None = None
    entity: str | None = None
    allowed_warehouses: list[str] | None = None


class EditUserRequest(BaseModel):
    full_name: str | None = None
    email: str | None = None
    role_id: int | None = None
    entity: str | None = None
    is_active: bool | None = None
    allowed_warehouses: list[str] | None = None


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
    "full_name", "email", "role_id", "entity", "is_active", "allowed_warehouses",
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
        try:
            return await _create(
                conn, body.phone, body.password, body.full_name,
                body.role_id, body.email, body.entity, body.allowed_warehouses,
            )
        except asyncpg.UniqueViolationError as e:
            cname = e.constraint_name or ""
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
            SELECT u.user_id, u.phone, u.full_name, u.email, u.entity, u.allowed_warehouses,
                   u.is_active, u.status, u.created_at, u.last_login_at,
                   r.role_id, r.role_name, r.is_admin
            FROM auth_user u
            LEFT JOIN auth_role r ON u.role_id = r.role_id
            ORDER BY u.created_at DESC
            """
        )
    return [dict(r) for r in rows]


@router.put("/users/{user_id}")
async def edit_user(request: Request, user_id: int, body: EditUserRequest):
    """HI-04: only fields in `_EDITABLE_USER_COLUMNS` can be set; identifier
    quoting is defensive — values come from the allowlist, not user input."""
    await _require_admin(request)
    pool = request.app.state.db_pool

    sent = body.model_fields_set
    updates: list[str] = []
    params: list = []
    idx = 1
    for field in sent:
        if field not in _EDITABLE_USER_COLUMNS:
            # Silently ignore unknown / disallowed fields (e.g. password_encrypted)
            continue
        updates.append(f'"{field}" = ${idx}')
        params.append(getattr(body, field))
        idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No editable fields supplied")

    params.append(user_id)
    sql = f'UPDATE auth_user SET {", ".join(updates)} WHERE user_id = ${idx}'
    async with pool.acquire() as conn:
        result = await conn.execute(sql, *params)
        if result == 'UPDATE 0':
            raise HTTPException(status_code=404, detail="User not found")

    return {"user_id": user_id, "updated": True}


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
