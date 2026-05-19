"""RBAC middleware — FastAPI dependencies built on the JWT access token.

Usage in endpoints:

    from app.modules.auth.middleware import require_permission, get_current_user

    @router.get("/something")
    async def something(request: Request, user=Depends(get_current_user)):
        ...

    @router.get("/job-cards")
    async def list_jc(request: Request,
                      user=Depends(require_permission("production", "job_cards", action="view"))):
        # user.entity, user.floor are already validated against allowed_entities/floors
        ...

The bearer token is now a JWT access token issued by `/api/v1/auth/login` /
`/refresh`. `validate_session` lives in `services/auth_service.py` and
verifies the JWT signature + hydrates the user from the DB. Errors come
through the structured envelope (`AuthError` → request_context handler).
"""

from __future__ import annotations

import logging

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.middleware.request_context import AuthError

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


class AuthUser:
    """Authenticated user context, available in endpoint handlers."""

    def __init__(
        self,
        user_id: int,
        phone: str,
        full_name: str,
        email: str,
        entity: str,
        role_id: int,
        role_name: str,
        is_admin: bool,
        refresh_jti: str | None = None,
    ):
        self.user_id = user_id
        self.phone = phone
        self.full_name = full_name
        self.email = email
        self.entity = entity
        self.role_id = role_id
        self.role_name = role_name
        self.is_admin = is_admin
        # `refresh_jti` is the parent_jti claim from the access token — the
        # refresh token row that minted it. Used by /password/change to keep
        # the current session alive when revoking siblings.
        self.refresh_jti = refresh_jti


def _authuser_from_session(session: dict) -> AuthUser:
    return AuthUser(
        user_id=session["user_id"],
        phone=session["phone"],
        full_name=session.get("full_name") or "",
        email=session.get("email") or "",
        entity=session.get("entity") or "",
        role_id=session["role_id"],
        role_name=session.get("role_name", ""),
        is_admin=bool(session.get("is_admin")),
        refresh_jti=session.get("session_id"),
    )


async def _extract_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> AuthUser:
    """Validate JWT bearer token. Raises AuthError on failure.

    NEW-MD-02: shares the `request.state.user_dict` cache with the legacy
    admin helpers in router.py. Today FastAPI's `Depends` already dedupes
    multiple `Depends(get_current_user)` in the same request, so this cache
    only matters when an endpoint mixes the new path (`Depends`) and the
    legacy path (`_require_auth` from router.py). With the unified cache,
    such mixed usage triggers exactly one `validate_session` round-trip.
    """
    cached = getattr(request.state, "user_dict", None)
    if cached:
        return _authuser_from_session(cached)

    if not credentials:
        raise AuthError("unauthorized", "Authorization header required", 401)

    token = credentials.credentials
    pool = request.app.state.db_pool

    from app.modules.auth.services.auth_service import validate_session

    async with pool.acquire() as conn:
        session = await validate_session(conn, token)

    if not session:
        raise AuthError("invalid_access_token", "Invalid or expired access token", 401)

    request.state.user_dict = session
    return _authuser_from_session(session)


# Simple auth dependency — just validates token, no permission check
get_current_user = _extract_user


def require_permission(
    module: str,
    sub_module: str | None = None,
    sub_sub_module: str | None = None,
    action: str = "view",
):
    """Factory returning a FastAPI dependency that enforces a specific permission.

    Checks:
        1. Valid access token
        2. Role has the required permission
        3. Entity / floor scope restrictions pass
    """

    async def _dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    ) -> AuthUser:
        user = await _extract_user(request, credentials)

        if user.is_admin:
            return user

        entity = request.query_params.get("entity") or user.entity or None
        floor = request.query_params.get("floor") or None

        pool = request.app.state.db_pool
        from app.modules.auth.services.permission_service import check_permission

        async with pool.acquire() as conn:
            allowed = await check_permission(
                conn, user.role_id, user.is_admin,
                module, sub_module, sub_sub_module, action,
                entity=entity, floor=floor,
            )

        if not allowed:
            raise AuthError(
                "forbidden",
                f"Permission denied: {module}/{sub_module or '*'}/{action}",
                403,
                details={
                    "module": module,
                    "sub_module": sub_module,
                    "action": action,
                },
            )

        return user

    return _dependency
