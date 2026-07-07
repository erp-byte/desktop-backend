"""Narrow admin seed: ensure ONE admin role + ONE admin user (bcrypt password).

Scope is deliberately minimal — it does NOT seed the full permission catalog,
because permission_service.check_permission() bypasses all checks when the
user's role has is_admin=TRUE. Password is bcrypt-hashed via the app's own
password_rules.hash_password (rounds=12), so login's password_rules.verify
accepts it and needs_rehash() is False.

Usage (default = the documented seed admin):
    uv run python scripts/seed_admin.py
    uv run python scripts/seed_admin.py --phone 9004464207 --password 'Candor@123*'

Idempotent: re-running updates the existing user's password/role in place.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root for `app`

import asyncpg
from dotenv import dotenv_values

from app.modules.auth.services import password_rules
from app.modules.auth.services.phone import lookup_keys

ENV_PATH = "/home/kaushal/CANDOR_ERP/server_replica/.env"


async def main(phone: str, password: str, full_name: str) -> None:
    url = (dotenv_values(ENV_PATH).get("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL not found in .env")

    hashed = password_rules.hash_password(password)
    keys = lookup_keys(phone)  # ['+91…', bare-10-digit, raw]

    conn = await asyncpg.connect(url)
    try:
        async with conn.transaction():
            # 1) admin role (FK parent) — create or force is_admin=TRUE
            role_id = await conn.fetchval(
                """
                INSERT INTO auth_role (role_name, description, is_admin)
                VALUES ('admin', 'Full unrestricted access', TRUE)
                ON CONFLICT (role_name) DO UPDATE SET is_admin = TRUE
                RETURNING role_id
                """
            )

            # 2) upsert the user — match any stored phone variant
            existing = await conn.fetchrow(
                "SELECT user_id, phone FROM auth_user WHERE phone = ANY($1::text[]) LIMIT 1",
                keys,
            )
            if existing:
                user_id = existing["user_id"]
                await conn.execute(
                    """
                    UPDATE auth_user
                       SET password_encrypted = $2,
                           full_name = $3,
                           role_id = $4,
                           is_active = TRUE,
                           status = 'active',
                           must_change_password = FALSE,
                           failed_login_count = 0,
                           locked_until = NULL,
                           password_changed_at = NOW()
                     WHERE user_id = $1
                    """,
                    user_id, hashed, full_name, role_id,
                )
                action = "updated"
            else:
                user_id = await conn.fetchval(
                    """
                    INSERT INTO auth_user
                        (phone, password_encrypted, full_name, role_id, is_active,
                         status, must_change_password, password_changed_at)
                    VALUES ($1, $2, $3, $4, TRUE, 'active', FALSE, NOW())
                    RETURNING user_id
                    """,
                    phone, hashed, full_name, role_id,
                )
                action = "created"

            # 3) mirror into the multi-role join table
            await conn.execute(
                "INSERT INTO auth_user_role (user_id, role_id) VALUES ($1, $2) "
                "ON CONFLICT (user_id, role_id) DO NOTHING",
                user_id, role_id,
            )

        # 4) verify with the SAME function login uses
        stored = await conn.fetchval(
            "SELECT password_encrypted FROM auth_user WHERE user_id = $1", user_id
        )
        ok = password_rules.verify(password, stored)
        bcrypt_ok = not password_rules.needs_rehash(stored)
        print(f"[{action}] user_id={user_id} phone={phone!r} role_id={role_id} (admin)")
        print(f"  hash prefix : {stored[:7]}…   (bcrypt={bcrypt_ok})")
        print(f"  verify('{password}') -> {ok}")
        if not (ok and bcrypt_ok):
            raise SystemExit("VERIFICATION FAILED — password does not verify as bcrypt")
        print("  OK — login will accept these credentials.")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="9004464207")
    ap.add_argument("--password", default="Candor@123*")
    ap.add_argument("--full-name", default="Admin")
    a = ap.parse_args()
    asyncio.run(main(a.phone, a.password, a.full_name))
