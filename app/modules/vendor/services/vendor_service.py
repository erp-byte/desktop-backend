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

from app.modules.vendor.services import history_service

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
    scoc_status_id, kyc_status_id, doc_status_id,
    supplier_type_other, firm_status_other, business_type_other,
    approved_by, approved_at,
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
    *,
    source: str = "manual",
    reason: str | None = None,
) -> dict[str, Any]:
    """Insert a new vendor_master row; auto-mint supplier_code if absent.

    Also writes a `create` row into vendor_master_history in the same txn
    so the audit trail starts the moment the vendor exists. `source` lets
    extract-driven creates be tagged ('extraction') vs manual ones.
    """
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
                    result = _row_to_dict(row)
                    if result is not None:
                        await history_service.record_history(
                            conn, "vendor",
                            operation="create",
                            parent_id=str(result["vendor_id"]),
                            new_state=result,
                            previous_state=None,
                            actor_user_id=actor_user_id,
                            source=source,
                            reason=reason,
                        )
                    return result  # type: ignore[return-value]
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
        "scoc_status_id", "kyc_status_id", "doc_status_id",
        "supplier_type_other", "firm_status_other", "business_type_other",
        "approved_by", "approved_at", "created_by", "updated_by",
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


def _build_vendor_where(
    *,
    status: str | None,
    category_code_id: str | None,
    search: str | None,
    approval: str | None,
) -> tuple[str, list[Any]]:
    """Shared WHERE-builder for list and search.

    `approval` is 'approved' (approved_at IS NOT NULL) or 'pending'
    (approved_at IS NULL). The trigram `idx_vendor_name_trgm` index
    makes ILIKE %term% cheap even on no-prefix searches."""
    where = ["is_deleted = false"]
    args: list[Any] = []
    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    if category_code_id:
        args.append(category_code_id)
        where.append(f"category_code_id = ${len(args)}")
    if search:
        args.append(f"%{search}%")
        where.append(f"name ILIKE ${len(args)}")
    if approval == "approved":
        where.append("approved_at IS NOT NULL")
    elif approval == "pending":
        where.append("approved_at IS NULL")
    return " AND ".join(where), args


# Enrichment join applied to a page / search result. Aggregates per
# vendor are computed via LATERAL subqueries scoped to the result set,
# so the work is bounded by the page size rather than the whole table.
# `has_primary_banking` mirrors the approval pre-condition (active +
# primary banking row required) so the UI can flag pending vendors that
# are blocked on banking setup without re-implementing the check.
_VENDOR_ENRICH_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT count(*)::int AS cnt
          FROM vendor_document
         WHERE vendor_id = base.vendor_id
    ) d ON true
    LEFT JOIN LATERAL (
        SELECT count(*)::int AS cnt
          FROM vendor_contract
         WHERE vendor_id = base.vendor_id
    ) c ON true
    LEFT JOIN LATERAL (
        SELECT count(*)::int AS cnt,
               coalesce(bool_or(is_primary AND is_active), false) AS has_primary
          FROM vendor_banking
         WHERE vendor_id = base.vendor_id
    ) b ON true
"""


# Columns the enrichment query emits. Kept as a string so callers can
# template it into the outer SELECT alongside _VENDOR_COLUMNS.
_VENDOR_ENRICH_COLUMNS = """
    (base.approved_at IS NOT NULL) AS is_approved,
    coalesce(d.cnt, 0) AS documents_count,
    coalesce(c.cnt, 0) AS contracts_count,
    coalesce(b.cnt, 0) AS banking_count,
    coalesce(b.has_primary, false) AS has_primary_banking
"""


# Sort key for list / search. Approved vendors surface first so the UI
# can render two visual buckets without re-grouping in the client.
# `lower(name)` keeps the secondary sort case-insensitive so "Acme
# Foods" and "acme foods" cluster together instead of splitting across
# the uppercase / lowercase boundary.
_VENDOR_ENRICH_ORDER = (
    "ORDER BY (base.approved_at IS NOT NULL) DESC, "
    "lower(base.name) ASC NULLS LAST, base.vendor_id DESC"
)


def _row_to_enriched(row: asyncpg.Record) -> dict[str, Any]:
    """Reshape a flat asyncpg row from the enrichment query into the
    nested `VendorListRow` shape (counts grouped under `counts`)."""
    d = dict(row)
    counts = {
        "documents": d.pop("documents_count", 0) or 0,
        "contracts": d.pop("contracts_count", 0) or 0,
        "banking":   d.pop("banking_count", 0) or 0,
    }
    d["counts"] = counts
    return d


async def list_vendors(
    pool: asyncpg.Pool,
    *,
    status: str | None = None,
    category_code_id: str | None = None,
    search: str | None = None,
    approval: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """Paginated vendor list with per-vendor enrichment.

    Rows include `is_approved`, document/contract/banking counts, and
    `has_primary_banking`. Sort: approved first, then alphabetical by
    name, then vendor_id (deterministic tie-break for paging stability).
    Default page_size is 50; the paged-200 endpoint passes 200.
    """
    page = max(1, page)
    page_size = max(1, min(500, page_size))
    where_sql, args = _build_vendor_where(
        status=status, category_code_id=category_code_id,
        search=search, approval=approval,
    )

    count_sql = f"SELECT count(*) FROM vendor_master WHERE {where_sql}"
    # Two-step query: page the vendor_master rows first, then enrich
    # the page. Putting the LIMIT inside the inner SELECT bounds the
    # LATERAL work to page_size rows instead of the full result set.
    list_sql = (
        f"SELECT base.*, {_VENDOR_ENRICH_COLUMNS} "
        f"FROM (SELECT {_VENDOR_COLUMNS} FROM vendor_master "
        f"      WHERE {where_sql} "
        f"      ORDER BY (approved_at IS NOT NULL) DESC, "
        f"               lower(name) ASC NULLS LAST, vendor_id DESC "
        f"      LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}) base "
        f"{_VENDOR_ENRICH_LATERAL} "
        f"{_VENDOR_ENRICH_ORDER}"
    )

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_sql, *args)
        rows = await conn.fetch(list_sql, *args, page_size, (page - 1) * page_size)
    return [_row_to_enriched(r) for r in rows], int(total or 0)


async def search_vendors_all(
    pool: asyncpg.Pool,
    *,
    query: str,
    status: str | None = None,
    category_code_id: str | None = None,
    approval: str | None = None,
    limit: int = 1000,
) -> tuple[list[dict[str, Any]], bool]:
    """Direct-to-DB vendor search with NO pagination.

    Returns up to `limit` matches in a single round-trip. The hard
    ceiling guards against runaway responses if the operator types a
    two-letter common prefix; the response carries `truncated=True` when
    we hit it so the UI can prompt for a narrower query.

    The enrichment + sort match `list_vendors` so a vendor row looks
    identical regardless of which endpoint surfaced it.
    """
    if not query or not query.strip():
        return [], False
    limit = max(1, min(2000, limit))
    where_sql, args = _build_vendor_where(
        status=status, category_code_id=category_code_id,
        search=query, approval=approval,
    )

    # Fetch limit + 1 so we can detect truncation without a separate count.
    list_sql = (
        f"SELECT base.*, {_VENDOR_ENRICH_COLUMNS} "
        f"FROM (SELECT {_VENDOR_COLUMNS} FROM vendor_master "
        f"      WHERE {where_sql} "
        f"      ORDER BY (approved_at IS NOT NULL) DESC, "
        f"               lower(name) ASC NULLS LAST, vendor_id DESC "
        f"      LIMIT ${len(args) + 1}) base "
        f"{_VENDOR_ENRICH_LATERAL} "
        f"{_VENDOR_ENRICH_ORDER}"
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(list_sql, *args, limit + 1)
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    return [_row_to_enriched(r) for r in rows], truncated


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
    *,
    source: str = "manual",
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Partial update.

    Semantics:
        * Field absent from `patch` → column not touched.
        * Field present as None → column set to NULL (clears nullable fields).
        * Field present as a value → column updated.
        * Explicit None on a NOT NULL column → `NotNullPatchError`.

    Writes a `vendor_master_history` row in the same transaction. The
    `_reason` key (if the router didn't strip it) is consumed here as the
    history reason and never stored on `vendor_master`.
    """
    # Strip keys we should never let the client touch directly.
    patch.pop("vendor_id", None)
    patch.pop("created_at", None)
    patch.pop("created_by", None)
    patch.pop("approved_by", None)
    patch.pop("approved_at", None)
    patch.pop("is_deleted", None)
    # Consume client-supplied reason (the UI ships it inside the same
    # JSON body to avoid a second roundtrip). Accept both the JSON-on-
    # the-wire key (`_reason`) and the Pydantic field name (`reason`)
    # since `model_dump()` produces the latter when not using by_alias.
    inline_reason = patch.pop("_reason", None) or patch.pop("reason", None)
    effective_reason = reason if reason is not None else inline_reason

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
        async with conn.transaction():
            prev_row = await conn.fetchrow(
                f"SELECT {_VENDOR_COLUMNS} FROM vendor_master "
                f"WHERE vendor_id = $1 AND is_deleted = false FOR UPDATE",
                vendor_id,
            )
            if prev_row is None:
                return None
            new_row = await conn.fetchrow(sql, *args)
            new_dict = _row_to_dict(new_row)
            if new_dict is not None:
                await history_service.record_history(
                    conn, "vendor",
                    operation="update",
                    parent_id=str(vendor_id),
                    previous_state=dict(prev_row),
                    new_state=new_dict,
                    actor_user_id=actor_user_id,
                    source=source,
                    reason=effective_reason,
                )
            return new_dict


async def soft_delete_vendor(
    pool: asyncpg.Pool,
    vendor_id: str,
    actor_user_id: str | None = None,
    *,
    reason: str | None = None,
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            prev_row = await conn.fetchrow(
                f"SELECT {_VENDOR_COLUMNS} FROM vendor_master "
                f"WHERE vendor_id = $1 AND is_deleted = false FOR UPDATE",
                vendor_id,
            )
            if prev_row is None:
                return False
            row = await conn.fetchrow(
                f"""
                UPDATE vendor_master
                   SET is_deleted = true,
                       updated_at = now(),
                       updated_by = $2
                 WHERE vendor_id = $1 AND is_deleted = false
                RETURNING {_VENDOR_COLUMNS}
                """,
                vendor_id, actor_user_id,
            )
            if row is None:
                return False
            await history_service.record_history(
                conn, "vendor",
                operation="delete",
                parent_id=str(vendor_id),
                previous_state=dict(prev_row),
                new_state=dict(row),
                actor_user_id=actor_user_id,
                source="manual",
                reason=reason,
            )
    return True


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
            prev_row = await conn.fetchrow(
                f"SELECT {_VENDOR_COLUMNS} FROM vendor_master "
                f"WHERE vendor_id = $1 AND is_deleted = false FOR UPDATE",
                vendor_id,
            )
            if not prev_row:
                return None
            if not prev_row["kyc_status_id"]:
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
            if row is not None:
                await history_service.record_history(
                    conn, "vendor",
                    operation="approve",
                    parent_id=str(vendor_id),
                    previous_state=dict(prev_row),
                    new_state=dict(row),
                    actor_user_id=approver_user_id,
                    source="approve",
                )
    return _row_to_dict(row)
