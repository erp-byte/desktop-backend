"""Auth module router — login, users, roles, permissions."""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    phone: str
    password: str


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


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


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
    action: str  # view, create, edit, delete
    description: str = ""


class EditPermissionRequest(BaseModel):
    module: str | None = None
    sub_module: str | None = None
    sub_sub_module: str | None = None
    action: str | None = None
    description: str | None = None


class CreateModuleRequest(BaseModel):
    module: str
    sub_modules: list[str] | None = None  # optional: auto-create sub_modules with default actions


# ---------------------------------------------------------------------------
# Auth endpoints (no auth required)
# ---------------------------------------------------------------------------

@router.post("/login")
async def login(request: Request, body: LoginRequest):
    """Login with phone + password. Returns session token."""
    from app.modules.auth.services.auth_service import login as _login
    pool = request.app.state.db_pool
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await _login(conn, body.phone, body.password, ip, ua)
    if "error" in result:
        raise HTTPException(status_code=401, detail=result["message"])
    return result


@router.post("/logout")
async def logout(request: Request):
    """Logout — deactivate session. Requires Authorization header."""
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="No token provided")
    from app.modules.auth.services.auth_service import logout as _logout
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        return await _logout(conn, token)


@router.get("/me")
async def get_me(request: Request):
    """Get current user, role, and permissions."""
    user = await _require_auth(request)
    from app.modules.auth.services.auth_service import get_user_permissions
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        permissions = await get_user_permissions(conn, user['role_id'])
    user["permissions"] = permissions
    return user


@router.post("/change-password")
async def change_password(request: Request, body: ChangePasswordRequest):
    """Change own password."""
    user = await _require_auth(request)
    from app.modules.auth.services.auth_service import change_password as _change
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await _change(conn, user['user_id'], body.old_password, body.new_password)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@router.post("/users")
async def create_user(request: Request, body: CreateUserRequest):
    """Create a new user. Admin only."""
    await _require_admin(request)
    from app.modules.auth.services.auth_service import create_user as _create
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            return await _create(conn, body.phone, body.password, body.full_name,
                                  body.role_id, body.email, body.entity, body.allowed_warehouses)
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(status_code=409, detail="Phone number already registered")
            raise


@router.get("/users")
async def list_users(request: Request):
    """List all users. Admin only."""
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.phone, u.full_name, u.email, u.entity, u.allowed_warehouses,
                   u.is_active, u.created_at, u.last_login_at,
                   r.role_id, r.role_name, r.is_admin
            FROM auth_user u
            LEFT JOIN auth_role r ON u.role_id = r.role_id
            ORDER BY u.created_at DESC
            """
        )
    return [dict(r) for r in rows]


@router.put("/users/{user_id}")
async def edit_user(request: Request, user_id: int, body: EditUserRequest):
    """Edit a user. Admin only."""
    await _require_admin(request)
    pool = request.app.state.db_pool

    sent = body.model_fields_set
    updates, params, idx = [], [], 1
    for field in ['full_name', 'email', 'role_id', 'entity', 'is_active', 'allowed_warehouses']:
        if field in sent:
            updates.append(f"{field} = ${idx}")
            params.append(getattr(body, field))
            idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    params.append(user_id)
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"UPDATE auth_user SET {', '.join(updates)} WHERE user_id = ${idx}", *params,
        )
        if result == 'UPDATE 0':
            raise HTTPException(status_code=404, detail="User not found")

    return {"user_id": user_id, "updated": True}


@router.delete("/users/{user_id}")
async def deactivate_user(request: Request, user_id: int):
    """Deactivate a user and invalidate all sessions. Admin only."""
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE auth_user SET is_active = FALSE WHERE user_id = $1", user_id)
            await conn.execute("UPDATE auth_session SET is_active = FALSE WHERE user_id = $1", user_id)
    return {"user_id": user_id, "deactivated": True}


# ---------------------------------------------------------------------------
# Role & Permission management (admin only)
# ---------------------------------------------------------------------------

@router.get("/roles")
async def list_roles(request: Request):
    """List all roles with permission counts."""
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
    """Create a new role. Admin only."""
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
    """Set permissions for a role. Replaces existing permissions. Admin only."""
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Clear existing
            await conn.execute("DELETE FROM auth_role_permission WHERE role_id = $1", role_id)
            # Insert new
            for pid in body.permission_ids:
                await conn.execute(
                    "INSERT INTO auth_role_permission (role_id, permission_id, allowed_entities, allowed_warehouses, allowed_floors) VALUES ($1, $2, $3, $4, $5)",
                    role_id, pid, body.allowed_entities, body.allowed_warehouses, body.allowed_floors,
                )
    return {"role_id": role_id, "permissions_set": len(body.permission_ids)}


@router.get("/permissions")
async def list_permissions(request: Request, module: str = Query(None)):
    """List all available permissions. Optional filter by module. Admin only."""
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
    """Get permissions organized as a tree: module → sub_module → sub_sub_module → actions. Admin only."""
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM auth_permission ORDER BY module, sub_module, sub_sub_module, action")

    tree = {}
    for r in rows:
        mod = r['module']
        sub = r['sub_module'] or '_root'
        subsub = r['sub_sub_module'] or '_root'

        if mod not in tree:
            tree[mod] = {}
        if sub not in tree[mod]:
            tree[mod][sub] = {}
        if subsub not in tree[mod][sub]:
            tree[mod][sub][subsub] = []

        tree[mod][sub][subsub].append({
            "permission_id": r['permission_id'],
            "action": r['action'],
            "description": r['description'],
        })

    return tree


@router.get("/modules")
async def list_modules(request: Request):
    """List all distinct modules and their sub-modules. Admin only."""
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
    """Create a new module with optional sub-modules. Auto-creates view/create/edit/delete permissions for each. Admin only."""
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
                # Create root-level permissions for the module
                for action in default_actions:
                    await conn.execute(
                        "INSERT INTO auth_permission (module, action, description) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                        body.module, action, f"{action.title()} {body.module}",
                    )
                    created += 1

    return {"module": body.module, "sub_modules": body.sub_modules or [], "permissions_created": created}


@router.post("/permissions/create")
async def create_permission(request: Request, body: CreatePermissionRequest):
    """Create a single custom permission. Admin only."""
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            pid = await conn.fetchval(
                "INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description) VALUES ($1, $2, $3, $4, $5) RETURNING permission_id",
                body.module, body.sub_module, body.sub_sub_module, body.action, body.description,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(status_code=409, detail="Permission already exists")
            raise
    return {"permission_id": pid, "module": body.module, "sub_module": body.sub_module, "action": body.action}


@router.put("/permissions/{permission_id}")
async def edit_permission(request: Request, permission_id: int, body: EditPermissionRequest):
    """Edit a permission. Admin only."""
    await _require_admin(request)
    pool = request.app.state.db_pool

    sent = body.model_fields_set
    updates, params, idx = [], [], 1
    for field in ['module', 'sub_module', 'sub_sub_module', 'action', 'description']:
        if field in sent:
            updates.append(f"{field} = ${idx}")
            params.append(getattr(body, field))
            idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    params.append(permission_id)
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"UPDATE auth_permission SET {', '.join(updates)} WHERE permission_id = ${idx}", *params,
        )
        if result == 'UPDATE 0':
            raise HTTPException(status_code=404, detail="Permission not found")

    return {"permission_id": permission_id, "updated": True}


@router.delete("/permissions/{permission_id}")
async def delete_permission(request: Request, permission_id: int):
    """Delete a permission and remove it from all roles. Admin only."""
    await _require_admin(request)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Remove from role mappings first
            await conn.execute("DELETE FROM auth_role_permission WHERE permission_id = $1", permission_id)
            result = await conn.execute("DELETE FROM auth_permission WHERE permission_id = $1", permission_id)
            if result == 'DELETE 0':
                raise HTTPException(status_code=404, detail="Permission not found")
    return {"permission_id": permission_id, "deleted": True}


@router.get("/roles/{role_id}/permissions")
async def get_role_permissions(request: Request, role_id: int):
    """Get all permissions assigned to a role with scope restrictions. Admin only."""
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
    return {
        "role": dict(role),
        "permissions": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


async def _require_auth(request: Request) -> dict:
    """Extract and validate auth token. Returns user dict or raises 401."""
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header required")

    from app.modules.auth.services.auth_service import validate_session
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        user = await validate_session(conn, token)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return user


async def _require_admin(request: Request) -> dict:
    """Require admin role."""
    user = await _require_auth(request)
    if not user.get('is_admin'):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
