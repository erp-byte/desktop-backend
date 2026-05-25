"""Dump every auth_user with their decrypted password (where possible).

Usage:
    .venv\\Scripts\\python _auth_dump_probe.py

Reads DATABASE_URL + AUTH_ENCRYPTION_KEY from .env. For each user:
  - Legacy Fernet-encrypted blobs (`gAAAAA...`)  → decrypted to plaintext
  - Bcrypt hashes (`$2a$/$2b$/$2y$...`)         → cannot be reversed; shown
                                                  as <bcrypt: not recoverable>.

Bcrypt-only users must be reset via the admin reset-password endpoint
(POST /api/v1/auth/users/{user_id}/reset-password) to set a known password.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
from cryptography.fernet import Fernet, InvalidToken


def _load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _is_bcrypt(stored: str) -> bool:
    return stored.startswith(("$2a$", "$2b$", "$2y$"))


async def main() -> None:
    env = _load_env(Path(__file__).parent / ".env")
    db_url = env.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
    key = env.get("AUTH_ENCRYPTION_KEY") or os.environ.get("AUTH_ENCRYPTION_KEY")
    if not db_url:
        raise SystemExit("DATABASE_URL not set in .env")
    if not key:
        raise SystemExit("AUTH_ENCRYPTION_KEY not set in .env")

    cipher = Fernet(key.encode() if isinstance(key, str) else key)

    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.phone, u.full_name, u.email, u.entity,
                   u.is_active, u.status, u.must_change_password,
                   u.last_login_at, u.password_encrypted,
                   r.role_name
              FROM auth_user u
              LEFT JOIN auth_role r ON u.role_id = r.role_id
             ORDER BY u.user_id
            """
        )
    finally:
        await conn.close()

    if not rows:
        print("No users found.")
        return

    bcrypt_users: list[int] = []
    print(
        f"{'id':<4} {'phone':<16} {'name':<22} {'role':<18} {'active':<7} "
        f"{'must_chg':<9} password"
    )
    print("-" * 110)
    for r in rows:
        stored = r["password_encrypted"] or ""
        if _is_bcrypt(stored):
            pw = "<bcrypt: not recoverable>"
            bcrypt_users.append(r["user_id"])
        else:
            try:
                pw = cipher.decrypt(stored.encode()).decode()
            except (InvalidToken, ValueError):
                pw = f"<decrypt failed; first10={stored[:10]!r}>"

        print(
            f"{r['user_id']:<4} "
            f"{(r['phone'] or '')[:16]:<16} "
            f"{(r['full_name'] or '')[:22]:<22} "
            f"{(r['role_name'] or '')[:18]:<18} "
            f"{('yes' if r['is_active'] else 'no'):<7} "
            f"{('yes' if r['must_change_password'] else 'no'):<9} "
            f"{pw}"
        )

    if bcrypt_users:
        print()
        print(
            f"NOTE: user_id(s) {bcrypt_users} are bcrypt-hashed and cannot be "
            "recovered. Reset via POST /api/v1/auth/users/{user_id}/reset-password "
            "to set a known temp password (forces must_change_password=TRUE)."
        )


if __name__ == "__main__":
    asyncio.run(main())
