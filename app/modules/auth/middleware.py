"""RBAC Middleware — FastAPI dependencies for permission enforcement.

Usage in endpoints:
    from app.modules.auth.middleware import require_permission, get_current_user

    # Just require authentication
    @router.get("/something")
    async def something(request: Request, user=Depends(get_current_user)):
        ...

    # Require specific permission (module/sub_module/action + scope)
    @router.get("/job-cards")
    async def list_jc(request: Request,
                      user=Depends(require_permission("production", "job_cards", action="view"))):
        # user.entity, user.floor are already validated against allowed_entities/floors
        ...
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


class AuthUser:
    """Authenticated user context, available in endpoint handlers."""
    def __init__(self, user_id: int, phone: str, full_name: str, email: str,
                 entity: str, role_id: int, role_name: str, is_admin: bool):
        self.user_id = user_id
        self.phone = phone
        self.full_name = full_name
        self.email = email
        self.entity = entity
        self.role_id = role_id
        self.role_name = role_name
        self.is_admin = is_admin


async def _extract_user(request: Request, credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> AuthUser:
    """Extract and validate user from Bearer token. Raises 401 if invalid."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")

    token = credentials.credentials
    pool = request.app.state.db_pool

    from app.modules.auth.services.auth_service import validate_session

    async with pool.acquire() as conn:
        session = await validate_session(conn, token)

    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return AuthUser(
        user_id=session['user_id'],
        phone=session['phone'],
        full_name=session['full_name'] or '',
        email=session['email'] or '',
        entity=session['entity'] or '',
        role_id=session['role_id'],
        role_name=session.get('role_name', ''),
        is_admin=session.get('is_admin', False),
    )


# Simple auth dependency — just validates token, no permission check
get_current_user = _extract_user


def require_permission(module: str, sub_module: str = None,
                       sub_sub_module: str = None, action: str = "view"):
    """Factory that returns a FastAPI dependency checking a specific permission.

    Checks:
    1. Valid session (token)
    2. Role has the required permission
    3. Entity/floor scope restrictions pass

    Usage:
        @router.get("/plans")
        async def list_plans(request: Request,
                             user=Depends(require_permission("production", "plans"))):
            ...
    """
    async def _dependency(request: Request,
                          credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> AuthUser:
        user = await _extract_user(request, credentials)

        # Admin bypasses all checks
        if user.is_admin:
            return user

        # Extract entity/floor from query params or body for scope checking
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
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: {module}/{sub_module or '*'}/{action}"
            )

        return user

    return _dependency
