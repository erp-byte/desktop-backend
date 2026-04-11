"""Auth service — login, session management, AES-256 password encryption."""

import logging
import os
import uuid
from datetime import datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

SESSION_DURATION_HOURS = 24


def _get_cipher():
    key = os.environ.get("AUTH_ENCRYPTION_KEY")
    if not key:
        # Fallback: load from .env file
        from pathlib import Path
        for env_path in [Path(__file__).parents[3] / ".env", Path.cwd() / ".env"]:
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.strip().startswith("AUTH_ENCRYPTION_KEY="):
                        key = line.strip().split("=", 1)[1]
                        break
            if key:
                break
    if not key:
        raise RuntimeError("AUTH_ENCRYPTION_KEY not set. Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_password(plain: str) -> str:
    return _get_cipher().encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    return _get_cipher().decrypt(encrypted.encode()).decode()


def verify_password(plain: str, encrypted: str) -> bool:
    try:
        return decrypt_password(encrypted) == plain
    except (InvalidToken, Exception):
        return False


async def login(conn, phone: str, password: str, ip_address: str = None, user_agent: str = None) -> dict:
    """Authenticate user and create session."""

    user = await conn.fetchrow(
        "SELECT * FROM auth_user WHERE phone = $1 AND is_active = TRUE", phone,
    )
    if not user:
        return {"error": "invalid_credentials", "message": "Invalid phone number or password"}

    if not verify_password(password, user['password_encrypted']):
        return {"error": "invalid_credentials", "message": "Invalid phone number or password"}

    # Get role
    role = await conn.fetchrow("SELECT * FROM auth_role WHERE role_id = $1", user['role_id'])

    # Create session
    token = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(hours=SESSION_DURATION_HOURS)

    session_id = await conn.fetchval(
        """
        INSERT INTO auth_session (user_id, token, ip_address, user_agent, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING session_id
        """,
        user['user_id'], token, ip_address, user_agent, expires_at,
    )

    # Update last login
    await conn.execute(
        "UPDATE auth_user SET last_login_at = NOW() WHERE user_id = $1", user['user_id'],
    )

    # Load permissions
    permissions = await get_user_permissions(conn, user['role_id'])

    logger.info("Login: user=%s phone=%s role=%s", user['full_name'], phone, role['role_name'] if role else 'none')

    return {
        "token": token,
        "session_id": session_id,
        "expires_at": str(expires_at),
        "user": {
            "user_id": user['user_id'],
            "phone": user['phone'],
            "full_name": user['full_name'],
            "email": user['email'],
            "entity": user['entity'],
            "role": {
                "role_id": role['role_id'],
                "role_name": role['role_name'],
                "is_admin": role['is_admin'],
            } if role else None,
        },
        "permissions": permissions,
    }


async def validate_session(conn, token: str) -> dict | None:
    """Validate a session token. Returns user+role+permissions or None."""

    session = await conn.fetchrow(
        """
        SELECT s.*, u.user_id, u.phone, u.full_name, u.email, u.entity, u.role_id, u.is_active,
               r.role_name, r.is_admin
        FROM auth_session s
        JOIN auth_user u ON s.user_id = u.user_id
        LEFT JOIN auth_role r ON u.role_id = r.role_id
        WHERE s.token = $1 AND s.is_active = TRUE AND s.expires_at > NOW()
        """,
        token,
    )

    if not session:
        return None

    if not session['is_active']:
        return None

    # Update last activity
    await conn.execute(
        "UPDATE auth_session SET last_activity_at = NOW() WHERE session_id = $1",
        session['session_id'],
    )

    return {
        "user_id": session['user_id'],
        "phone": session['phone'],
        "full_name": session['full_name'],
        "email": session['email'],
        "entity": session['entity'],
        "role_id": session['role_id'],
        "role_name": session['role_name'],
        "is_admin": session['is_admin'],
        "session_id": session['session_id'],
    }


async def logout(conn, token: str) -> dict:
    """Deactivate a session."""
    result = await conn.execute(
        "UPDATE auth_session SET is_active = FALSE WHERE token = $1", token,
    )
    return {"logged_out": result != 'UPDATE 0'}


async def get_user_permissions(conn, role_id: int) -> list[dict]:
    """Get all permissions for a role."""
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


async def create_user(conn, phone: str, password: str, full_name: str,
                       role_id: int, email: str = None, entity: str = None) -> dict:
    """Create a new user with encrypted password."""
    encrypted = encrypt_password(password)

    user_id = await conn.fetchval(
        """
        INSERT INTO auth_user (phone, password_encrypted, full_name, email, role_id, entity)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING user_id
        """,
        phone, encrypted, full_name, email, role_id, entity,
    )

    return {"user_id": user_id, "phone": phone, "full_name": full_name, "role_id": role_id}


async def change_password(conn, user_id: int, old_password: str, new_password: str) -> dict:
    """Change user's password."""
    user = await conn.fetchrow("SELECT password_encrypted FROM auth_user WHERE user_id = $1", user_id)
    if not user:
        return {"error": "not_found"}

    if not verify_password(old_password, user['password_encrypted']):
        return {"error": "wrong_password", "message": "Current password is incorrect"}

    new_encrypted = encrypt_password(new_password)
    await conn.execute(
        "UPDATE auth_user SET password_encrypted = $2 WHERE user_id = $1", user_id, new_encrypted,
    )

    # Invalidate all other sessions
    await conn.execute(
        "UPDATE auth_session SET is_active = FALSE WHERE user_id = $1", user_id,
    )

    return {"changed": True, "message": "Password changed. All sessions invalidated."}
