"""POST /api/v1/receipt/invoice-upload — invoice/challan/lr/other documents."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

from app.config import Settings
from app.core.middleware.request_context import AuthError
from app.modules.receipt import storage as storage_mod

logger = logging.getLogger(__name__)


_MAX_FILE_BYTES = 10 * 1024 * 1024
_DOC_TYPES = frozenset({"invoice", "challan", "lr", "other"})


def _validate_doc_type(raw: str | None) -> str:
    if raw is None or raw == "":
        return "invoice"
    val = raw.strip().lower()
    if val not in _DOC_TYPES:
        raise AuthError(
            "invalid_document_type",
            f"document_type must be one of {sorted(_DOC_TYPES)}",
            400,
        )
    return val


def _validate_file(name: str | None, content: bytes,
                   declared: str | None) -> tuple[str, str]:
    if not name:
        raise AuthError("invalid_request", "file is missing a filename", 400)
    if not content:
        raise AuthError("empty_file", "Uploaded file is empty.", 400)
    if len(content) > _MAX_FILE_BYTES:
        raise AuthError(
            "file_too_large",
            f"File exceeds the {_MAX_FILE_BYTES // (1024*1024)} MB limit.",
            413,
        )
    declared_norm = (declared or "").lower().split(";", 1)[0].strip() or None
    if declared_norm and declared_norm not in storage_mod.ALLOWED_RECEIPT_MIMES:
        raise AuthError(
            "unsupported_mime_type",
            f"Mime type {declared_norm!r} is not supported.",
            415,
        )
    sniffed, mismatch = storage_mod.sniff_mime(content, declared_norm)
    if sniffed not in storage_mod.ALLOWED_RECEIPT_MIMES:
        raise AuthError(
            "unsupported_mime_type",
            f"File contents are not a supported type ({sniffed!r}).",
            415,
        )
    if mismatch:
        raise AuthError(
            "mime_type_mismatch",
            f"Declared mime {declared_norm!r} does not match contents ({sniffed!r}).",
            415,
        )
    return sniffed, name


async def _backfill_from_dock_intimation(conn, dock_intimation_id: int):
    """Try to fetch (transaction_no, supplier_id, entity) from the
    intimation row. Returns dict with keys present when the table exists
    AND a row matched; empty dict otherwise (best-effort)."""
    try:
        row = await conn.fetchrow(
            """
            SELECT transaction_no, supplier_id, entity
              FROM dock_arrival_intimation
             WHERE dock_intimation_id = $1
            """,
            dock_intimation_id,
        )
        return dict(row) if row else {}
    except asyncpg.UndefinedTableError:
        logger.info(
            "invoice.upload.backfill_skipped reason=table_missing dock_intimation_id=%s",
            dock_intimation_id,
        )
        return {}


async def _maybe_stamp_arrival_invoice(conn, *, dock_intimation_id: int | None,
                                       s3_key: str, document_type: str) -> None:
    if dock_intimation_id is None or document_type != "invoice":
        return
    try:
        await conn.execute(
            """
            UPDATE dock_arrival_intimation
               SET attached_invoice_url = $2
             WHERE dock_intimation_id = $1
            """,
            dock_intimation_id, s3_key,
        )
    except asyncpg.UndefinedTableError:
        logger.info(
            "invoice.upload.stamp_skipped reason=table_missing dock_intimation_id=%s",
            dock_intimation_id,
        )
    except Exception as e:
        logger.warning(
            "invoice.upload.stamp_failed dock_intimation_id=%s err=%r",
            dock_intimation_id, e,
        )


async def upload_invoice(
    pool, *,
    file_name: str | None,
    file_bytes: bytes,
    declared_mime: str | None,
    document_type: str | None,
    transaction_no: str | None,
    dock_intimation_id: int | None,
    remarks: str | None,
    actor_user_id: str,
    settings: Settings,
) -> dict:
    if not (transaction_no or dock_intimation_id is not None):
        raise AuthError(
            "missing_reference",
            "One of transaction_no or dock_intimation_id is required.",
            400,
        )

    doc_type = _validate_doc_type(document_type)
    effective_mime, file_name = _validate_file(file_name, file_bytes, declared_mime)

    storage = storage_mod.get_storage(settings)
    s3_key = storage_mod.new_storage_key(f"receipt-docs/{doc_type}", effective_mime)
    try:
        storage.put(s3_key, file_bytes, effective_mime)
    except Exception as e:
        logger.error("invoice.upload.s3_failed key=%s err=%r", s3_key, e)
        raise AuthError("s3_upload_failed", "Storage write failed.", 502)

    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        async with conn.transaction():
            backfill = {}
            if dock_intimation_id is not None:
                backfill = await _backfill_from_dock_intimation(conn, dock_intimation_id)

            txn_no = transaction_no or backfill.get("transaction_no")
            supplier_id = backfill.get("supplier_id")
            entity = backfill.get("entity")
            po_number = None
            if txn_no:
                row = await conn.fetchrow(
                    "SELECT po_number, entity FROM po_header WHERE transaction_no = $1",
                    txn_no,
                )
                if row:
                    po_number = row["po_number"]
                    entity = entity or row["entity"]

            if entity is None:
                # Spec: 400 entity_unresolved when refs don't resolve
                raise AuthError(
                    "entity_unresolved",
                    "Could not resolve entity from supplied references.",
                    400,
                )

            document_id = await conn.fetchval(
                """
                INSERT INTO receipt_document (
                    document_type, transaction_no, po_number, dock_intimation_id,
                    supplier_id, entity, s3_key, file_name, file_size_bytes,
                    mime_type, scan_status, remarks, uploaded_by, uploaded_at
                )
                VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7, $8, $9,
                    $10, 'pending', $11, $12, $13
                )
                RETURNING document_id
                """,
                doc_type, txn_no, po_number, dock_intimation_id,
                supplier_id, entity, s3_key, file_name, len(file_bytes),
                effective_mime, remarks, actor_user_id, now,
            )

            await _maybe_stamp_arrival_invoice(
                conn, dock_intimation_id=dock_intimation_id,
                s3_key=s3_key, document_type=doc_type,
            )

    return {
        "document_id": int(document_id),
        "document_type": doc_type,
        "s3_key": s3_key,
        "file_name": file_name,
        "file_size_bytes": len(file_bytes),
        "mime_type": effective_mime,
        "scan_status": "pending",
        "uploaded_at": now.isoformat().replace("+00:00", "Z"),
    }
