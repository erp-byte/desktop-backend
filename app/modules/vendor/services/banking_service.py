"""CRUD service for vendor_banking.

Application-level invariant: at most one row per vendor_id has
`is_primary = true`. There's no partial unique index, so we enforce it
in code — the create / update / set-primary paths run inside a single
transaction that unsets any prior primary before flipping the new one.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.modules.vendor.services import history_service
from app.modules.vendor.services.vendor_service import NotNullPatchError

#: vendor_banking columns that cannot be cleared via PATCH.
_BANK_NOT_NULL: frozenset[str] = frozenset({"bank_name", "account_no", "account_name"})

logger = logging.getLogger(__name__)


_BANK_COLUMNS = """
    bank_id, vendor_id, bank_name, account_no, account_name, branch,
    ifsc, swift, account_type_id, is_primary, is_active, valid_from,
    valid_to, created_at
"""


# ── create ───────────────────────────────────────────────────────────────


async def create_banking(
    pool: asyncpg.Pool,
    vendor_id: str,
    payload: dict[str, Any],
    *,
    actor_user_id: str | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    data = dict(payload)
    cols = [
        "vendor_id", "bank_name", "account_no", "account_name", "branch",
        "ifsc", "swift", "account_type_id", "is_primary", "is_active",
        "valid_from", "valid_to",
    ]
    data["vendor_id"] = vendor_id
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    values = [data.get(c) for c in cols]
    sql = f"""
        INSERT INTO vendor_banking ({", ".join(cols)})
        VALUES ({placeholders})
        RETURNING {_BANK_COLUMNS}
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            if data.get("is_primary"):
                await _demote_primary(conn, vendor_id, skip_bank_id=None)
            row = await conn.fetchrow(sql, *values)
            result = dict(row)
            await history_service.record_history(
                conn, "banking",
                operation="create",
                parent_id=str(result["bank_id"]),
                vendor_id=str(vendor_id),
                previous_state=None,
                new_state=result,
                actor_user_id=actor_user_id,
                source=source,
            )
    return result


async def _demote_primary(
    conn: asyncpg.Connection,
    vendor_id: str,
    skip_bank_id: str | None,
) -> None:
    if skip_bank_id is None:
        await conn.execute(
            "UPDATE vendor_banking SET is_primary = false "
            " WHERE vendor_id = $1 AND is_primary = true",
            vendor_id,
        )
    else:
        await conn.execute(
            "UPDATE vendor_banking SET is_primary = false "
            " WHERE vendor_id = $1 AND is_primary = true AND bank_id <> $2",
            vendor_id, skip_bank_id,
        )


# ── read ─────────────────────────────────────────────────────────────────


async def list_banking(
    pool: asyncpg.Pool,
    vendor_id: str,
    *,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {_BANK_COLUMNS} FROM vendor_banking WHERE vendor_id = $1"
    )
    if active_only:
        sql += " AND is_active = true"
    sql += " ORDER BY is_primary DESC, created_at DESC"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, vendor_id)
    return [dict(r) for r in rows]


async def get_banking(pool: asyncpg.Pool, bank_id: str) -> dict[str, Any] | None:
    sql = f"SELECT {_BANK_COLUMNS} FROM vendor_banking WHERE bank_id = $1"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, bank_id)
    return dict(row) if row else None


# ── update / delete ──────────────────────────────────────────────────────


async def update_banking(
    pool: asyncpg.Pool,
    bank_id: str,
    patch: dict[str, Any],
    *,
    vendor_id: str | None = None,
    actor_user_id: str | None = None,
    source: str = "manual",
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Partial update. Pass `vendor_id` to scope the UPDATE so the row
    can only be touched if it belongs to that vendor — the router uses
    this to prevent cross-tenant mutation given a known bank_id.

    Null semantics: a field present as None clears the column. Explicit
    None on a NOT NULL column raises NotNullPatchError.
    """
    # Pop server-managed keys; null clears for nullable columns.
    patch.pop("bank_id", None)
    patch.pop("vendor_id", None)
    patch.pop("created_at", None)
    inline_reason = patch.pop("_reason", None) or patch.pop("reason", None)
    effective_reason = reason if reason is not None else inline_reason
    if not patch:
        return await get_banking(pool, bank_id)
    for col in _BANK_NOT_NULL:
        if col in patch and patch[col] is None:
            raise NotNullPatchError(col)

    set_clauses: list[str] = []
    args: list[Any] = []
    for col, val in patch.items():
        args.append(val)
        set_clauses.append(f"{col} = ${len(args)}")
    args.append(bank_id)
    where_clause = f"bank_id = ${len(args)}"
    if vendor_id is not None:
        args.append(vendor_id)
        where_clause += f" AND vendor_id = ${len(args)}"

    sql = f"""
        UPDATE vendor_banking
           SET {", ".join(set_clauses)}
         WHERE {where_clause}
        RETURNING {_BANK_COLUMNS}
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            prev_row = await conn.fetchrow(
                f"SELECT {_BANK_COLUMNS} FROM vendor_banking "
                f"WHERE bank_id = $1 FOR UPDATE",
                bank_id,
            )
            if prev_row is None:
                return None
            # If the patch flips is_primary on, demote siblings first.
            if patch.get("is_primary") is True:
                owner = vendor_id or prev_row["vendor_id"]
                if owner:
                    await _demote_primary(conn, owner, skip_bank_id=bank_id)
            updated = await conn.fetchrow(sql, *args)
            if updated is None:
                return None
            new_dict = dict(updated)
            await history_service.record_history(
                conn, "banking",
                operation="update",
                parent_id=str(bank_id),
                vendor_id=str(new_dict["vendor_id"]),
                previous_state=dict(prev_row),
                new_state=new_dict,
                actor_user_id=actor_user_id,
                source=source,
                reason=effective_reason,
            )
            return new_dict


async def delete_banking(
    pool: asyncpg.Pool,
    bank_id: str,
    *,
    actor_user_id: str | None = None,
    reason: str | None = None,
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            prev_row = await conn.fetchrow(
                f"SELECT {_BANK_COLUMNS} FROM vendor_banking "
                f"WHERE bank_id = $1 FOR UPDATE",
                bank_id,
            )
            if prev_row is None:
                return False
            row = await conn.fetchrow(
                "DELETE FROM vendor_banking WHERE bank_id = $1 RETURNING bank_id",
                bank_id,
            )
            if row is None:
                return False
            await history_service.record_history(
                conn, "banking",
                operation="delete",
                parent_id=str(bank_id),
                vendor_id=str(prev_row["vendor_id"]),
                previous_state=dict(prev_row),
                new_state={"bank_id": str(bank_id), "deleted": True},
                actor_user_id=actor_user_id,
                source="manual",
                reason=reason,
            )
    return True


async def set_primary(
    pool: asyncpg.Pool,
    vendor_id: str,
    bank_id: str,
    *,
    actor_user_id: str | None = None,
) -> dict[str, Any] | None:
    """Atomic primary swap."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Verify ownership + capture prev state in one shot.
            prev_row = await conn.fetchrow(
                f"SELECT {_BANK_COLUMNS} FROM vendor_banking "
                f"WHERE bank_id = $1 FOR UPDATE",
                bank_id,
            )
            if prev_row is None or prev_row["vendor_id"] != vendor_id:
                return None
            await _demote_primary(conn, vendor_id, skip_bank_id=bank_id)
            row = await conn.fetchrow(
                f"""
                UPDATE vendor_banking
                   SET is_primary = true,
                       is_active = true
                 WHERE bank_id = $1
                RETURNING {_BANK_COLUMNS}
                """,
                bank_id,
            )
            if row is None:
                return None
            new_dict = dict(row)
            await history_service.record_history(
                conn, "banking",
                operation="set_primary",
                parent_id=str(bank_id),
                vendor_id=str(vendor_id),
                previous_state=dict(prev_row),
                new_state=new_dict,
                actor_user_id=actor_user_id,
            )
            return new_dict
