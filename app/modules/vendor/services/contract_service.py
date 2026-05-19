"""CRUD service for vendor_contract.

Symmetric to document_service:
    * s3_urls comma-separated text
    * upload + Claude extraction + insert path
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.modules.vendor import storage as vendor_storage
from app.modules.vendor.schemas import ExtractedContractFields, csv_to_list, list_to_csv
from app.modules.vendor.services import claude_extractor

logger = logging.getLogger(__name__)


_CONTRACT_COLUMNS = """
    contract_id, vendor_id, contract_type, signed_date, effective_from,
    effective_to, s3_urls, scoc_signed, value_inr, auto_renew,
    created_by, created_at
"""


def _row(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(row) if row else None


# ── manual create ────────────────────────────────────────────────────────


async def create_contract(
    pool: asyncpg.Pool,
    vendor_id: str,
    payload: dict[str, Any],
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    cols = [
        "vendor_id", "contract_type", "signed_date", "effective_from",
        "effective_to", "s3_urls", "scoc_signed", "value_inr",
        "auto_renew", "created_by",
    ]
    data = dict(payload)
    data["vendor_id"] = vendor_id
    data["created_by"] = actor_user_id
    if "s3_urls" in data:
        data["s3_urls"] = list_to_csv(csv_to_list(data.get("s3_urls") or ""))

    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    values = [data.get(c) for c in cols]
    sql = f"""
        INSERT INTO vendor_contract ({", ".join(cols)})
        VALUES ({placeholders})
        RETURNING {_CONTRACT_COLUMNS}
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *values)
    return _row(row)  # type: ignore[return-value]


# ── list / get ───────────────────────────────────────────────────────────


async def list_contracts(
    pool: asyncpg.Pool,
    vendor_id: str,
    *,
    contract_type: str | None = None,
) -> list[dict[str, Any]]:
    where = ["vendor_id = $1"]
    args: list[Any] = [vendor_id]
    if contract_type:
        args.append(contract_type)
        where.append(f"contract_type = ${len(args)}")
    sql = (
        f"SELECT {_CONTRACT_COLUMNS} FROM vendor_contract "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY created_at DESC NULLS LAST"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def get_contract(pool: asyncpg.Pool, contract_id: str) -> dict[str, Any] | None:
    sql = f"SELECT {_CONTRACT_COLUMNS} FROM vendor_contract WHERE contract_id = $1"
    async with pool.acquire() as conn:
        return _row(await conn.fetchrow(sql, contract_id))


# ── update / delete ──────────────────────────────────────────────────────


async def update_contract(
    pool: asyncpg.Pool,
    contract_id: str,
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    """Partial update. Null clears any nullable field — vendor_contract
    has no mutable NOT NULL columns once vendor_id / contract_id are popped.
    """
    patch.pop("contract_id", None)
    patch.pop("vendor_id", None)
    patch.pop("created_at", None)
    patch.pop("created_by", None)
    if "s3_urls" in patch:
        # Explicit null → store empty CSV (clears the row's file list).
        patch["s3_urls"] = list_to_csv(csv_to_list(patch["s3_urls"] or ""))
    if not patch:
        return await get_contract(pool, contract_id)

    set_clauses: list[str] = []
    args: list[Any] = []
    for col, val in patch.items():
        args.append(val)
        set_clauses.append(f"{col} = ${len(args)}")
    args.append(contract_id)
    sql = f"""
        UPDATE vendor_contract
           SET {", ".join(set_clauses)}
         WHERE contract_id = ${len(args)}
        RETURNING {_CONTRACT_COLUMNS}
    """
    async with pool.acquire() as conn:
        return _row(await conn.fetchrow(sql, *args))


async def delete_contract(pool: asyncpg.Pool, contract_id: str) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM vendor_contract WHERE contract_id = $1 RETURNING contract_id",
            contract_id,
        )
    return row is not None


# ── upload + extract ─────────────────────────────────────────────────────


async def upload_to_s3(
    settings: Any,
    *,
    supplier_code: str,
    file_bytes: bytes,
    mime_type: str,
    original_filename: str | None = None,
) -> str:
    backend = vendor_storage.get_vendor_storage(settings)
    key = vendor_storage.new_vendor_key(
        supplier_code=supplier_code,
        doc_type="CONTRACT",
        mime_type=mime_type,
        original_filename=original_filename,
    )
    return backend.put(key, file_bytes, mime_type)


async def extract_only(
    settings: Any,
    *,
    supplier_code: str,
    file_bytes: bytes,
    mime_type: str,
    original_filename: str | None = None,
) -> tuple[str, ExtractedContractFields]:
    s3_url = await upload_to_s3(
        settings,
        supplier_code=supplier_code,
        file_bytes=file_bytes,
        mime_type=mime_type,
        original_filename=original_filename,
    )
    extracted = await claude_extractor.extract_contract_fields(
        file_bytes=file_bytes,
        mime_type=mime_type,
    )
    return s3_url, extracted


def _merge_extracted(
    payload: dict[str, Any],
    extracted: ExtractedContractFields,
) -> dict[str, Any]:
    out = dict(payload)
    if not out.get("contract_type") and extracted.contract_type:
        out["contract_type"] = extracted.contract_type
    if not out.get("signed_date") and extracted.signed_date:
        out["signed_date"] = extracted.signed_date
    if not out.get("effective_from") and extracted.effective_from:
        out["effective_from"] = extracted.effective_from
    if not out.get("effective_to") and extracted.effective_to:
        out["effective_to"] = extracted.effective_to
    if not out.get("value_inr") and extracted.value_inr is not None:
        out["value_inr"] = extracted.value_inr
    return out


async def upload_and_save(
    pool: asyncpg.Pool,
    settings: Any,
    *,
    vendor_id: str,
    supplier_code: str,
    file_bytes: bytes,
    mime_type: str,
    original_filename: str | None,
    actor_user_id: str | None,
    base_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ExtractedContractFields]:
    s3_url, extracted = await extract_only(
        settings,
        supplier_code=supplier_code,
        file_bytes=file_bytes,
        mime_type=mime_type,
        original_filename=original_filename,
    )
    payload = dict(base_payload or {})
    payload["s3_urls"] = list_to_csv(csv_to_list(payload.get("s3_urls") or "") + [s3_url])
    payload = _merge_extracted(payload, extracted)
    try:
        row = await create_contract(pool, vendor_id, payload, actor_user_id=actor_user_id)
    except Exception:
        # Orphan-file cleanup after DB-insert failure.
        vendor_storage.get_vendor_storage(settings).delete(s3_url)
        raise
    return row, extracted


async def append_file(
    pool: asyncpg.Pool,
    settings: Any,
    *,
    contract_id: str,
    supplier_code: str,
    file_bytes: bytes,
    mime_type: str,
    original_filename: str | None,
) -> dict[str, Any] | None:
    s3_url = await upload_to_s3(
        settings,
        supplier_code=supplier_code,
        file_bytes=file_bytes,
        mime_type=mime_type,
        original_filename=original_filename,
    )
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchrow(
                    "SELECT s3_urls FROM vendor_contract WHERE contract_id = $1 FOR UPDATE",
                    contract_id,
                )
                if not cur:
                    vendor_storage.get_vendor_storage(settings).delete(s3_url)
                    return None
                new_urls = list_to_csv(csv_to_list(cur["s3_urls"] or "") + [s3_url])
                updated = await conn.fetchrow(
                    f"""
                    UPDATE vendor_contract
                       SET s3_urls = $1
                     WHERE contract_id = $2
                    RETURNING {_CONTRACT_COLUMNS}
                    """,
                    new_urls, contract_id,
                )
    except Exception:
        vendor_storage.get_vendor_storage(settings).delete(s3_url)
        raise
    return _row(updated)
