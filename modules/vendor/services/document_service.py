"""CRUD service for vendor_document.

`s3_urls` is stored as comma-separated text in the DB (after the DDL
migration). Inside this service we operate on the CSV string directly;
the public router uses `csv_to_list` / `list_to_csv` helpers from
schemas.py when callers want list shapes.

Upload-and-save flow:
    1. Upload bytes to S3 via `modules.vendor.storage.get_vendor_storage()`
    2. Run `claude_extractor.extract_document_fields(...)`
    3. Insert row populating extracted scalar fields + s3 url
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from modules.vendor import storage as vendor_storage
from modules.vendor.schemas import ExtractedDocFields, csv_to_list, list_to_csv
from modules.vendor.services import claude_extractor
from modules.vendor.services.vendor_service import NotNullPatchError

logger = logging.getLogger(__name__)

#: vendor_document columns that cannot be cleared via PATCH.
_DOC_NOT_NULL: frozenset[str] = frozenset({"doc_type"})


_DOC_COLUMNS = """
    doc_id, vendor_id, doc_type, doc_number, s3_urls, issued_on,
    valid_from, valid_to, status_id, uploaded_by, uploaded_at
"""


def _row(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(row) if row else None


# ── manual create ────────────────────────────────────────────────────────


async def create_document(
    pool: asyncpg.Pool,
    vendor_id: str,
    payload: dict[str, Any],
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    cols = [
        "vendor_id", "doc_type", "doc_number", "s3_urls", "issued_on",
        "valid_from", "valid_to", "status_id", "uploaded_by",
    ]
    data = dict(payload)
    data["vendor_id"] = vendor_id
    data["uploaded_by"] = actor_user_id
    # Normalise CSV (strip whitespace, drop empty entries).
    if "s3_urls" in data:
        data["s3_urls"] = list_to_csv(csv_to_list(data.get("s3_urls") or ""))

    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    values = [data.get(c) for c in cols]
    sql = f"""
        INSERT INTO vendor_document ({", ".join(cols)})
        VALUES ({placeholders})
        RETURNING {_DOC_COLUMNS}
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *values)
    return _row(row)  # type: ignore[return-value]


# ── list / get ───────────────────────────────────────────────────────────


async def list_documents(
    pool: asyncpg.Pool,
    vendor_id: str,
    *,
    doc_type: str | None = None,
    expiring_within_days: int | None = None,
) -> list[dict[str, Any]]:
    where = ["vendor_id = $1"]
    args: list[Any] = [vendor_id]
    if doc_type:
        args.append(doc_type)
        where.append(f"doc_type = ${len(args)}")
    if expiring_within_days is not None:
        args.append(expiring_within_days)
        where.append(
            f"valid_to IS NOT NULL "
            f"AND valid_to <= (current_date + (${len(args)}::int) * interval '1 day') "
            f"AND valid_to >= current_date"
        )
    sql = (
        f"SELECT {_DOC_COLUMNS} FROM vendor_document "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY uploaded_at DESC NULLS LAST"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def get_document(pool: asyncpg.Pool, doc_id: str) -> dict[str, Any] | None:
    sql = f"SELECT {_DOC_COLUMNS} FROM vendor_document WHERE doc_id = $1"
    async with pool.acquire() as conn:
        return _row(await conn.fetchrow(sql, doc_id))


# ── update / delete ──────────────────────────────────────────────────────


async def update_document(
    pool: asyncpg.Pool,
    doc_id: str,
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    """Partial update. Null on a nullable field clears it; null on a
    NOT NULL field (currently only `doc_type`) raises NotNullPatchError.
    """
    patch.pop("doc_id", None)
    patch.pop("vendor_id", None)
    patch.pop("uploaded_at", None)
    if "s3_urls" in patch:
        # Explicit null → store empty CSV (clears the row's file list).
        patch["s3_urls"] = list_to_csv(csv_to_list(patch["s3_urls"] or ""))
    if not patch:
        return await get_document(pool, doc_id)
    for col in _DOC_NOT_NULL:
        if col in patch and patch[col] is None:
            raise NotNullPatchError(col)

    set_clauses: list[str] = []
    args: list[Any] = []
    for col, val in patch.items():
        args.append(val)
        set_clauses.append(f"{col} = ${len(args)}")
    args.append(doc_id)
    sql = f"""
        UPDATE vendor_document
           SET {", ".join(set_clauses)}
         WHERE doc_id = ${len(args)}
        RETURNING {_DOC_COLUMNS}
    """
    async with pool.acquire() as conn:
        return _row(await conn.fetchrow(sql, *args))


async def delete_document(pool: asyncpg.Pool, doc_id: str) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM vendor_document WHERE doc_id = $1 RETURNING doc_id",
            doc_id,
        )
    return row is not None


# ── upload + extract ─────────────────────────────────────────────────────


async def upload_to_s3(
    settings: Any,
    *,
    supplier_code: str,
    doc_type: str,
    file_bytes: bytes,
    mime_type: str,
    original_filename: str | None = None,
) -> str:
    """Push bytes to S3 (or local fallback) and return the stored URL."""
    backend = vendor_storage.get_vendor_storage(settings)
    key = vendor_storage.new_vendor_key(
        supplier_code=supplier_code,
        doc_type=doc_type,
        mime_type=mime_type,
        original_filename=original_filename,
    )
    return backend.put(key, file_bytes, mime_type)


async def extract_only(
    settings: Any,
    *,
    supplier_code: str,
    doc_type: str,
    file_bytes: bytes,
    mime_type: str,
    original_filename: str | None = None,
) -> tuple[str, ExtractedDocFields]:
    """Push to S3, run extraction, return (s3_url, extracted) — DOES NOT SAVE."""
    s3_url = await upload_to_s3(
        settings,
        supplier_code=supplier_code,
        doc_type=doc_type,
        file_bytes=file_bytes,
        mime_type=mime_type,
        original_filename=original_filename,
    )
    extracted = await claude_extractor.extract_document_fields(
        file_bytes=file_bytes,
        mime_type=mime_type,
        doc_type=doc_type,
    )
    return s3_url, extracted


def _merge_extracted(payload: dict[str, Any], extracted: ExtractedDocFields) -> dict[str, Any]:
    """Populate empty payload fields from extracted values."""
    out = dict(payload)
    if not out.get("doc_number") and extracted.doc_number:
        out["doc_number"] = extracted.doc_number
    if not out.get("issued_on") and extracted.issued_on:
        out["issued_on"] = extracted.issued_on
    if not out.get("valid_from") and extracted.valid_from:
        out["valid_from"] = extracted.valid_from
    if not out.get("valid_to") and extracted.valid_to:
        out["valid_to"] = extracted.valid_to
    return out


async def upload_and_save(
    pool: asyncpg.Pool,
    settings: Any,
    *,
    vendor_id: str,
    supplier_code: str,
    doc_type: str,
    file_bytes: bytes,
    mime_type: str,
    original_filename: str | None,
    actor_user_id: str | None,
    base_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ExtractedDocFields]:
    """One-shot: upload to S3 + Claude extract + DB insert.

    Failure handling: if the DB insert raises, the S3 object we just
    uploaded becomes an orphan — we delete it best-effort so the caller
    doesn't pay storage for a row that never existed.
    """
    s3_url, extracted = await extract_only(
        settings,
        supplier_code=supplier_code,
        doc_type=doc_type,
        file_bytes=file_bytes,
        mime_type=mime_type,
        original_filename=original_filename,
    )
    payload = dict(base_payload or {})
    payload["doc_type"] = doc_type
    payload["s3_urls"] = list_to_csv(csv_to_list(payload.get("s3_urls") or "") + [s3_url])
    payload = _merge_extracted(payload, extracted)
    try:
        row = await create_document(pool, vendor_id, payload, actor_user_id=actor_user_id)
    except Exception:
        # Orphan-file cleanup. delete() never raises.
        vendor_storage.get_vendor_storage(settings).delete(s3_url)
        raise
    return row, extracted


async def append_file(
    pool: asyncpg.Pool,
    settings: Any,
    *,
    doc_id: str,
    supplier_code: str,
    doc_type: str,
    file_bytes: bytes,
    mime_type: str,
    original_filename: str | None,
) -> dict[str, Any] | None:
    """Add a file to an existing vendor_document row's CSV s3_urls.

    If the row doesn't exist or the UPDATE fails after the S3 upload,
    the orphan object is deleted best-effort.
    """
    s3_url = await upload_to_s3(
        settings,
        supplier_code=supplier_code,
        doc_type=doc_type,
        file_bytes=file_bytes,
        mime_type=mime_type,
        original_filename=original_filename,
    )
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchrow(
                    "SELECT s3_urls FROM vendor_document WHERE doc_id = $1 FOR UPDATE",
                    doc_id,
                )
                if not cur:
                    # Doc row doesn't exist — clean up the orphan upload.
                    vendor_storage.get_vendor_storage(settings).delete(s3_url)
                    return None
                new_urls = list_to_csv(csv_to_list(cur["s3_urls"] or "") + [s3_url])
                updated = await conn.fetchrow(
                    f"""
                    UPDATE vendor_document
                       SET s3_urls = $1
                     WHERE doc_id = $2
                    RETURNING {_DOC_COLUMNS}
                    """,
                    new_urls, doc_id,
                )
    except Exception:
        vendor_storage.get_vendor_storage(settings).delete(s3_url)
        raise
    return _row(updated)
