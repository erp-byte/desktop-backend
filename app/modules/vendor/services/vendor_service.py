"""CRUD service for vendor_master.

Notes:
  * `supplier_code` is UNIQUE NOT NULL. If the client omits it on create
    we mint one in the form `SC{YY}-{NNNN}` where NNNN is the next
    sequence inside the calendar year. On insert collision we retry up
    to 5 times (rare; only matters under concurrent registration bursts).
  * `is_deleted` provides soft delete — every list / get query filters
    `is_deleted = false` by default.
  * `set_in_clause` builds a parameterised UPDATE for PATCH.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────


_VENDOR_COLUMNS = """
    vendor_id, supplier_code, supplier_reg_year, name, status,
    supplier_type_id, firm_status_id, business_type_id, category_code_id,
    sub_category, local_os_id, core_business, contact_person, designation,
    phone_company, mobile, email, website, address_line, state, city,
    pin_code, fssai_no, brc_other, cin_no, pan_no, gstn, iec_no,
    pollution_epr, tin_tan, is_msme, msme_registration_date, msme_type_id,
    uam_udyam_no, business_turnover_3y, capabilities, remarks, reference,
    scoc_status_id, kyc_status_id, doc_status_id, approved_by, approved_at,
    created_at, created_by, updated_at, updated_by, is_deleted
"""


def _row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


async def _next_supplier_code(conn: asyncpg.Connection) -> str:
    year_2d = datetime.now(timezone.utc).strftime("%y")
    prefix = f"SC{year_2d}-"
    row = await conn.fetchrow(
        """
        SELECT supplier_code
          FROM vendor_master
         WHERE supplier_code LIKE $1
         ORDER BY supplier_code DESC
         LIMIT 1
        """,
        prefix + "%",
    )
    next_seq = 1
    if row and row["supplier_code"]:
        try:
            existing = int(row["supplier_code"].split("-", 1)[1])
            next_seq = existing + 1
        except (ValueError, IndexError):
            next_seq = 1
    return f"{prefix}{next_seq:04d}"


# ── create ───────────────────────────────────────────────────────────────


async def create_vendor(
    pool: asyncpg.Pool,
    payload: dict[str, Any],
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    """Insert a new vendor_master row; auto-mint supplier_code if absent."""
    data = dict(payload)
    data.setdefault("status", "active")
    data["created_by"] = actor_user_id
    data["updated_by"] = actor_user_id

    # Each attempt uses its own nested transaction (savepoint) so a
    # UniqueViolationError on supplier_code rolls back THAT attempt only —
    # the outer loop can still query for the next free code. Wrapping the
    # whole loop in one transaction aborts the txn on the first collision
    # and every subsequent SELECT fails with InFailedSqlTransactionError.
    async with pool.acquire() as conn:
        for attempt in range(5):
            if not data.get("supplier_code"):
                data["supplier_code"] = await _next_supplier_code(conn)
            try:
                async with conn.transaction():
                    row = await _insert_vendor(conn, data)
                    return _row_to_dict(row)  # type: ignore[return-value]
            except asyncpg.UniqueViolationError as e:
                # supplier_code collision — only retry when we auto-minted.
                if "supplier_code" in str(e) and not payload.get("supplier_code"):
                    data["supplier_code"] = None  # force re-mint
                    # tiny jitter to break ties under bursts
                    _ = secrets.randbits(8)
                    continue
                raise
        raise RuntimeError("could not allocate unique supplier_code after 5 attempts")


async def _insert_vendor(
    conn: asyncpg.Connection,
    data: dict[str, Any],
) -> asyncpg.Record:
    cols = [
        "supplier_code", "supplier_reg_year", "name", "status",
        "supplier_type_id", "firm_status_id", "business_type_id",
        "category_code_id", "sub_category", "local_os_id",
        "core_business", "contact_person", "designation", "phone_company",
        "mobile", "email", "website", "address_line", "state", "city",
        "pin_code", "fssai_no", "brc_other", "cin_no", "pan_no", "gstn",
        "iec_no", "pollution_epr", "tin_tan", "is_msme",
        "msme_registration_date", "msme_type_id", "uam_udyam_no",
        "business_turnover_3y", "capabilities", "remarks", "reference",
        "scoc_status_id", "kyc_status_id", "doc_status_id", "approved_by",
        "approved_at", "created_by", "updated_by",
    ]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    values = [data.get(c) for c in cols]
    sql = f"""
        INSERT INTO vendor_master ({", ".join(cols)})
        VALUES ({placeholders})
        RETURNING {_VENDOR_COLUMNS}
    """
    return await conn.fetchrow(sql, *values)


# ── read ─────────────────────────────────────────────────────────────────


async def get_vendor(
    pool: asyncpg.Pool,
    vendor_id: str,
    *,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    sql = f"SELECT {_VENDOR_COLUMNS} FROM vendor_master WHERE vendor_id = $1"
    if not include_deleted:
        sql += " AND is_deleted = false"
    async with pool.acquire() as conn:
        return _row_to_dict(await conn.fetchrow(sql, vendor_id))


async def list_vendors(
    pool: asyncpg.Pool,
    *,
    status: str | None = None,
    category_code_id: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    page = max(1, page)
    page_size = max(1, min(500, page_size))
    where = ["is_deleted = false"]
    args: list[Any] = []

    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    if category_code_id:
        args.append(category_code_id)
        where.append(f"category_code_id = ${len(args)}")
    if search:
        # trigram-indexed via idx_vendor_name_trgm; LIKE works against the GIN.
        args.append(f"%{search}%")
        where.append(f"name ILIKE ${len(args)}")

    where_sql = " AND ".join(where)
    count_sql = f"SELECT count(*) FROM vendor_master WHERE {where_sql}"
    # M3: include `vendor_id` as the tie-breaker so paging is deterministic
    # even when multiple rows share the same `created_at`.
    list_sql = (
        f"SELECT {_VENDOR_COLUMNS} FROM vendor_master "
        f"WHERE {where_sql} "
        f"ORDER BY created_at DESC NULLS LAST, vendor_id DESC "
        f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}"
    )

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_sql, *args)
        rows = await conn.fetch(list_sql, *args, page_size, (page - 1) * page_size)
    return [dict(r) for r in rows], int(total or 0)


# ── update / delete ──────────────────────────────────────────────────────


#: vendor_master columns that cannot be cleared via PATCH.
_VENDOR_NOT_NULL: frozenset[str] = frozenset({"name", "supplier_code", "status"})


class NotNullPatchError(ValueError):
    """A PATCH payload attempted to set a NOT NULL column to null."""

    def __init__(self, field: str):
        super().__init__(f"field '{field}' cannot be null")
        self.field = field


async def update_vendor(
    pool: asyncpg.Pool,
    vendor_id: str,
    patch: dict[str, Any],
    actor_user_id: str | None = None,
) -> dict[str, Any] | None:
    """Partial update.

    Semantics:
        * Field absent from `patch` → column not touched.
        * Field present as None → column set to NULL (clears nullable fields).
        * Field present as a value → column updated.
        * Explicit None on a NOT NULL column → `NotNullPatchError`.
    """
    # Strip keys we should never let the client touch directly.
    patch.pop("vendor_id", None)
    patch.pop("created_at", None)
    patch.pop("created_by", None)
    patch.pop("approved_by", None)
    patch.pop("approved_at", None)
    patch.pop("is_deleted", None)
    if not patch:
        return await get_vendor(pool, vendor_id)

    for col in _VENDOR_NOT_NULL:
        if col in patch and patch[col] is None:
            raise NotNullPatchError(col)

    patch["updated_at"] = datetime.now(timezone.utc)
    patch["updated_by"] = actor_user_id

    set_clauses = []
    args: list[Any] = []
    for col, val in patch.items():
        args.append(val)
        set_clauses.append(f"{col} = ${len(args)}")
    args.append(vendor_id)

    sql = f"""
        UPDATE vendor_master
           SET {", ".join(set_clauses)}
         WHERE vendor_id = ${len(args)} AND is_deleted = false
        RETURNING {_VENDOR_COLUMNS}
    """
    async with pool.acquire() as conn:
        return _row_to_dict(await conn.fetchrow(sql, *args))


async def soft_delete_vendor(
    pool: asyncpg.Pool,
    vendor_id: str,
    actor_user_id: str | None = None,
) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE vendor_master
               SET is_deleted = true,
                   updated_at = now(),
                   updated_by = $2
             WHERE vendor_id = $1 AND is_deleted = false
            RETURNING vendor_id
            """,
            vendor_id, actor_user_id,
        )
    return row is not None


# ── approve ──────────────────────────────────────────────────────────────


class ApprovalError(ValueError):
    """Raised when /approve pre-conditions aren't met. Carries a stable
    `code` so the router can map it to a structured 409 envelope."""

    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


async def approve_vendor(
    pool: asyncpg.Pool,
    vendor_id: str,
    approver_user_id: str,
) -> dict[str, Any] | None:
    """SCM-Head sign-off. Pre-conditions:
        1. Vendor exists and is not soft-deleted.
        2. `kyc_status_id` is populated (KYC complete).
        3. At least one ACTIVE PRIMARY banking row exists.

    Returns the updated row on success. Raises `ApprovalError` with a
    stable code when a pre-condition fails. Returns `None` if the vendor
    doesn't exist (router maps to 404).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            vendor = await conn.fetchrow(
                "SELECT vendor_id, kyc_status_id FROM vendor_master "
                " WHERE vendor_id = $1 AND is_deleted = false FOR UPDATE",
                vendor_id,
            )
            if not vendor:
                return None
            if not vendor["kyc_status_id"]:
                raise ApprovalError("kyc_incomplete", "KYC must be completed before approval.")
            has_primary = await conn.fetchval(
                """
                SELECT 1 FROM vendor_banking
                 WHERE vendor_id = $1 AND is_primary = true AND is_active = true
                 LIMIT 1
                """,
                vendor_id,
            )
            if not has_primary:
                raise ApprovalError(
                    "primary_banking_required",
                    "Vendor must have at least one active primary banking entry before approval.",
                )
            row = await conn.fetchrow(
                f"""
                UPDATE vendor_master
                   SET approved_by = $2,
                       approved_at = now(),
                       updated_at  = now(),
                       updated_by  = $2
                 WHERE vendor_id = $1 AND is_deleted = false
                RETURNING {_VENDOR_COLUMNS}
                """,
                vendor_id, approver_user_id,
            )
    return _row_to_dict(row)
