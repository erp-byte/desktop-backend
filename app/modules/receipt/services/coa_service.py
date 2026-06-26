"""COA upload / list / detail / replace / delete.

Tables touched:
    coa_document — primary
    qc_inspection_intimation — coa_received flip (skipped if table absent)

The qc-intimation flip is best-effort: when the table doesn't exist (current
state of this DB) the service logs a warning and continues. Once the table
lands the existing code starts working without further changes.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.config import Settings
from app.core.middleware.request_context import AuthError
from app.modules.receipt import storage as storage_mod

logger = logging.getLogger(__name__)


_MAX_FILE_BYTES = 10 * 1024 * 1024
_PRESIGN_TTL_SECONDS = 300  # 5 min — spec
_REPLACE_WINDOW_HOURS = 24  # operator can replace own upload within 24h


# ── helpers ──────────────────────────────────────────────────────────────


def _gen_coa_id() -> str:
    """Match the existing gen_short_id() default — text PK, URL-safe."""
    return secrets.token_urlsafe(8)


def _iso_ts(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _iso_date(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _parse_json_obj(raw: str | None) -> dict | None:
    if raw is None or raw == "":
        return None
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        raise AuthError("invalid_json", "parsed_params_json is not valid JSON", 422)
    if not isinstance(v, dict):
        raise AuthError("invalid_json", "parsed_params_json must be a JSON object", 422)
    return v


def _parse_json_field(v: Any) -> dict | None:
    """asyncpg JSONB column → dict (asyncpg returns str by default here)."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _validate_anchors(*, transaction_no, dock_intimation_id, qc_intimation_id,
                      sku_id, sku_name_raw) -> None:
    if not any([transaction_no, dock_intimation_id, qc_intimation_id,
                sku_id, sku_name_raw]):
        raise AuthError(
            "missing_reference",
            "At least one of transaction_no, dock_intimation_id, "
            "qc_intimation_id, sku_id, or sku_name_raw is required.",
            400,
        )


def _validate_file(file_name: str | None, content: bytes,
                   declared_mime: str | None) -> tuple[str, str]:
    """Run size/mime/empty checks. Returns (effective_mime, normalised_filename)."""
    if not file_name:
        raise AuthError("invalid_request", "file is missing a filename", 400)
    if not content:
        raise AuthError("empty_file", "Uploaded file is empty.", 400)
    if len(content) > _MAX_FILE_BYTES:
        raise AuthError(
            "file_too_large",
            f"File exceeds the {_MAX_FILE_BYTES // (1024*1024)} MB limit.",
            413,
        )
    declared = (declared_mime or "").lower().split(";", 1)[0].strip() or None
    if declared and declared not in storage_mod.ALLOWED_RECEIPT_MIMES:
        raise AuthError(
            "unsupported_mime_type",
            f"Mime type {declared!r} is not supported. Allowed: pdf, jpeg, png.",
            415,
        )
    sniffed, mismatch = storage_mod.sniff_mime(content, declared)
    if sniffed not in storage_mod.ALLOWED_RECEIPT_MIMES:
        raise AuthError(
            "unsupported_mime_type",
            f"File contents are not a supported type ({sniffed!r}).",
            415,
        )
    if mismatch:
        raise AuthError(
            "mime_type_mismatch",
            f"Declared mime {declared!r} does not match file contents ({sniffed!r}).",
            415,
        )
    return sniffed, file_name


async def _resolve_entity(conn, *, transaction_no, dock_intimation_id,
                          qc_intimation_id, supplier_id) -> str | None:
    """Try anchors in order until one resolves to an entity. None = unresolved."""
    if transaction_no:
        row = await conn.fetchrow(
            "SELECT entity FROM po_header WHERE transaction_no = $1", transaction_no,
        )
        if row and row["entity"]:
            return row["entity"]
    # qc_inspection_intimation / dock_arrival_intimation aren't created yet —
    # skip them silently. Once they land they can be probed here too.
    return None


async def _maybe_flip_coa_received(conn, qc_intimation_id: int | None) -> None:
    if qc_intimation_id is None:
        return
    try:
        await conn.execute(
            "UPDATE qc_inspection_intimation SET coa_received = TRUE WHERE qc_intimation_id = $1",
            qc_intimation_id,
        )
    except asyncpg.UndefinedTableError:
        # Table doesn't exist yet — flip is best-effort.
        logger.info(
            "coa.upload.qc_intimation_flip_skipped reason=table_missing qc_intimation_id=%s",
            qc_intimation_id,
        )
    except Exception as e:
        logger.warning(
            "coa.upload.qc_intimation_flip_failed qc_intimation_id=%s err=%r",
            qc_intimation_id, e,
        )


async def _safe_storage_delete(storage, s3_key: str) -> None:
    """Best-effort blob cleanup after a DB rollback. WR-09: leftover blobs
    waste storage but don't break correctness. Log on failure for ops."""
    try:
        if hasattr(storage, "delete"):
            storage.delete(s3_key)
        else:
            # LocalStorage doesn't define delete in this build; fall back to
            # filesystem unlink for the local case.
            from app.modules.receipt.storage import LocalStorage
            if isinstance(storage, LocalStorage):
                from pathlib import Path
                p = (storage.base / s3_key).resolve()
                if str(p).startswith(str(storage.base)) and p.is_file():
                    p.unlink()
    except Exception as e:
        logger.warning("coa.storage.cleanup_failed key=%s err=%r", s3_key, e)


# ── 1. UPLOAD ────────────────────────────────────────────────────────────


async def upload_coa(
    pool, *,
    file_name: str | None,
    file_bytes: bytes,
    declared_mime: str | None,
    transaction_no: str | None,
    line_number: int | None,
    dock_intimation_id: int | None,
    qc_intimation_id: int | None,
    sku_id: int | None,
    sku_name_raw: str | None,
    supplier_id: int | None,
    lot_number: str | None,
    vendor_coa_date: str | None,
    parsed_params_json_raw: str | None,
    remarks: str | None,
    actor_user_id: str,
    settings: Settings,
) -> dict:
    """Upload a fresh COA. WR-06: requires the anchors to resolve to an
    entity (matches invoice_service behaviour) so list/detail scope filtering
    can be strict."""
    _validate_anchors(
        transaction_no=transaction_no, dock_intimation_id=dock_intimation_id,
        qc_intimation_id=qc_intimation_id, sku_id=sku_id, sku_name_raw=sku_name_raw,
    )
    effective_mime, file_name = _validate_file(file_name, file_bytes, declared_mime)
    parsed_params = _parse_json_obj(parsed_params_json_raw)

    # Resolve entity BEFORE the storage write so we don't orphan a blob on
    # entity_unresolved 400s.
    async with pool.acquire() as conn:
        entity = await _resolve_entity(
            conn, transaction_no=transaction_no,
            dock_intimation_id=dock_intimation_id,
            qc_intimation_id=qc_intimation_id, supplier_id=supplier_id,
        )

    if entity is None:
        # WR-06 + CR-02: refuse to write rows the scope filter can't enforce.
        raise AuthError(
            "entity_unresolved",
            "Could not resolve entity from supplied references.",
            400,
        )

    storage = storage_mod.get_storage(settings)
    s3_key = storage_mod.new_storage_key("coa", effective_mime)

    try:
        storage.put(s3_key, file_bytes, effective_mime)
    except Exception as e:
        logger.error("coa.upload.s3_failed key=%s err=%r", s3_key, e)
        raise AuthError("s3_upload_failed", "Storage write failed.", 502)

    coa_id = _gen_coa_id()
    now = datetime.now(timezone.utc)

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO coa_document (
                        coa_id, transaction_no, line_number, dock_intimation_id,
                        qc_intimation_id, sku_id, sku_name_raw, supplier_id,
                        lot_number, vendor_coa_date, parsed_params_json,
                        s3_key, file_name, file_size_bytes, mime_type,
                        scan_status, coa_status, uploaded_by, uploaded_at,
                        remarks, entity
                    )
                    VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10::date, $11::jsonb,
                        $12, $13, $14, $15,
                        'pending', 'active', $16, $17,
                        $18, $19
                    )
                    """,
                    coa_id, transaction_no, line_number, dock_intimation_id,
                    qc_intimation_id, sku_id, sku_name_raw, supplier_id,
                    lot_number, vendor_coa_date,
                    json.dumps(parsed_params) if parsed_params is not None else None,
                    s3_key, file_name, len(file_bytes), effective_mime,
                    actor_user_id, now,
                    remarks, entity,
                )
                await _maybe_flip_coa_received(conn, qc_intimation_id)
    except Exception:
        # WR-09: orphaned blob cleanup on DB failure
        await _safe_storage_delete(storage, s3_key)
        raise

    return {
        "coa_id": coa_id,
        "s3_key": s3_key,
        "file_name": file_name,
        "file_size_bytes": len(file_bytes),
        "mime_type": effective_mime,
        "scan_status": "pending",
        "coa_status": "active",
        "uploaded_at": _iso_ts(now),
    }


# ── 2. LIST ──────────────────────────────────────────────────────────────


async def list_coa(
    pool, *,
    transaction_no: str | None,
    dock_intimation_id: int | None,
    qc_intimation_id: int | None,
    po_number: str | None,
    sku_id: int | None,
    supplier_id: int | None,
    lot_number: str | None,
    coa_status: str,
    from_date: str | None,
    to_date: str | None,
    page: int,
    page_size: int,
    entity_scope: list[str] | None,
    settings: Settings,
) -> dict:
    conditions: list[str] = []
    params: list[Any] = []
    idx = 0

    def add(sql: str, *vals: Any) -> None:
        nonlocal idx
        out = sql
        for v in vals:
            idx += 1
            out = out.replace("${i}", f"${idx}", 1)
            params.append(v)
        conditions.append(out)

    if transaction_no:
        add("transaction_no = ${i}", transaction_no)
    if dock_intimation_id is not None:
        add("dock_intimation_id = ${i}", dock_intimation_id)
    if qc_intimation_id is not None:
        add("qc_intimation_id = ${i}", qc_intimation_id)
    if sku_id is not None:
        add("sku_id = ${i}", sku_id)
    if supplier_id is not None:
        add("supplier_id = ${i}", supplier_id)
    if lot_number:
        add("lot_number = ${i}", lot_number)
    if po_number:
        add(
            "transaction_no IN (SELECT transaction_no FROM po_header WHERE po_number = ${i})",
            po_number,
        )
    if coa_status:
        if coa_status not in ("active", "superseded", "deleted"):
            raise AuthError(
                "invalid_request",
                "coa_status must be active|superseded|deleted",
                400,
            )
        add("coa_status = ${i}", coa_status)
    if from_date:
        add("uploaded_at >= ${i}::date", from_date)
    if to_date:
        add("uploaded_at <  (${i}::date + INTERVAL '1 day')", to_date)
    if entity_scope is not None:
        # CR-02: strict scope. NO `entity IS NULL` loophole — uploads now
        # require a resolved entity (WR-06), so any NULL row is a legacy
        # artefact that admins can still see by virtue of `is_admin → None`.
        if not entity_scope:
            conditions.append("FALSE")
        else:
            idx += 1
            conditions.append(f"entity = ANY(${idx}::text[])")
            params.append(list(entity_scope))

    where_sql = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = int(await conn.fetchval(
            f"SELECT COUNT(*) FROM coa_document WHERE {where_sql}", *params,
        ) or 0)
        rows = await conn.fetch(
            f"""
            SELECT * FROM coa_document
             WHERE {where_sql}
             ORDER BY uploaded_at DESC NULLS LAST, coa_id DESC
             LIMIT ${idx + 1} OFFSET ${idx + 2}
            """,
            *params, page_size, offset,
        )

    storage = storage_mod.get_storage(settings)
    items = [_row_to_listitem(r, storage, settings) for r in rows]
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def _row_to_listitem(row, storage, settings: Settings) -> dict:
    download_url = ""
    if row["s3_key"]:
        try:
            download_url = storage.presigned_get(
                row["s3_key"], _PRESIGN_TTL_SECONDS, settings,
            )
        except Exception as e:
            logger.warning("coa.presign_failed coa_id=%s err=%r", row["coa_id"], e)
    return {
        "coa_id": row["coa_id"],
        "transaction_no": row["transaction_no"],
        "line_number": row["line_number"],
        "po_number": None,        # caller can join in po_header.po_number if needed
        "dock_intimation_id": row["dock_intimation_id"],
        "qc_intimation_id": row["qc_intimation_id"],
        "sku_id": row["sku_id"],
        "sku_name": row["sku_name_raw"],
        "supplier_id": row["supplier_id"],
        "supplier_name": None,    # supplier_master not modelled here
        "lot_number": row["lot_number"],
        "vendor_coa_date": _iso_date(row["vendor_coa_date"]),
        "file_name": row["file_name"],
        "file_size_bytes": row["file_size_bytes"],
        "mime_type": row["mime_type"],
        "scan_status": row["scan_status"] or "pending",
        "coa_status": row["coa_status"] or "active",
        "uploaded_by_name": row["uploaded_by"],   # display name lookup elided
        "uploaded_at": _iso_ts(row["uploaded_at"]),
        "download_url": download_url or None,
    }


# ── 3. DETAIL ────────────────────────────────────────────────────────────


async def get_coa_detail(
    pool, *, coa_id: str, entity_scope: list[str] | None,
    include_deleted: bool, settings: Settings,
) -> dict:
    """WR-04: replacement_history walks BOTH ancestors and descendants of
    the requested coa_id so the full chain is visible regardless of which
    link the caller looked up.
    WR-05: deleted COAs return 404 unless include_deleted=true."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM coa_document WHERE coa_id = $1", coa_id,
        )
        if row is None:
            raise AuthError("coa_not_found", "COA not found", 404)
        if entity_scope is not None and row["entity"] and \
                row["entity"].lower() not in entity_scope:
            raise AuthError("coa_not_found", "COA not found", 404)
        if not include_deleted and row["coa_status"] == "deleted":
            raise AuthError("coa_not_found", "COA not found", 404)

        chain = await conn.fetch(
            """
            WITH RECURSIVE
              descendants(id) AS (
                  SELECT $1::text
                  UNION ALL
                  SELECT c.coa_id
                    FROM coa_document c JOIN descendants d ON c.replaces_coa_id = d.id
              ),
              ancestors(id) AS (
                  SELECT $1::text
                  UNION ALL
                  SELECT c.replaces_coa_id
                    FROM coa_document c JOIN ancestors a ON c.coa_id = a.id
                   WHERE c.replaces_coa_id IS NOT NULL
              ),
              all_ids AS (
                  SELECT id FROM descendants
                  UNION
                  SELECT id FROM ancestors
              )
            SELECT replaces_coa_id AS old_coa_id, coa_id AS new_coa_id,
                   replaced_at, replaced_reason
              FROM coa_document
             WHERE replaces_coa_id IS NOT NULL
               AND coa_id IN (SELECT id FROM all_ids)
             ORDER BY replaced_at NULLS LAST
            """,
            coa_id,
        )

    storage = storage_mod.get_storage(settings)
    payload = _row_to_listitem(row, storage, settings)
    payload["parsed_params_json"] = _parse_json_field(row["parsed_params_json"])
    payload["replaces_coa_id"] = row["replaces_coa_id"]
    payload["remarks"] = row["remarks"]
    payload["replacement_history"] = [
        {
            "old_coa_id": r["old_coa_id"],
            "new_coa_id": r["new_coa_id"],
            "replaced_at": _iso_ts(r["replaced_at"]),
            "replaced_reason": r["replaced_reason"],
        }
        for r in chain
    ]
    return payload


# ── 4. REPLACE (PUT) ─────────────────────────────────────────────────────


def _resolve_inherited_params(
    new_params: dict | None, old_value: Any,
) -> str | None:
    """WR-03: caller's explicit `{}` overwrites the old value. None means
    'inherit from old'. Returns a JSON string or None for the INSERT."""
    if new_params is not None:
        return json.dumps(new_params)
    if old_value is None:
        return None
    if isinstance(old_value, str):
        return old_value
    parsed = _parse_json_field(old_value)
    return json.dumps(parsed) if parsed is not None else None


async def replace_coa(
    pool, *,
    coa_id: str,
    file_name: str | None,
    file_bytes: bytes,
    declared_mime: str | None,
    replaced_reason: str,
    vendor_coa_date: str | None,
    parsed_params_json_raw: str | None,
    actor_user_id: str,
    actor_is_admin: bool,
    settings: Settings,
) -> dict:
    """CR-01: SELECT FOR UPDATE + checks + INSERT + UPDATE all run inside
    one transaction so the row lock is durable. Storage write happens AFTER
    the lock is held but BEFORE the DB writes; on DB failure the blob is
    swept (WR-09)."""
    if not replaced_reason or not replaced_reason.strip():
        raise AuthError("reason_required", "replaced_reason is required", 400)

    effective_mime, file_name = _validate_file(file_name, file_bytes, declared_mime)
    parsed_params = _parse_json_obj(parsed_params_json_raw)

    storage = storage_mod.get_storage(settings)
    s3_key: str | None = None
    new_coa_id = _gen_coa_id()
    now = datetime.now(timezone.utc)

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                old = await conn.fetchrow(
                    "SELECT * FROM coa_document WHERE coa_id = $1 FOR UPDATE", coa_id,
                )
                if old is None:
                    raise AuthError("coa_not_found", "COA not found", 404)
                if old["coa_status"] != "active":
                    raise AuthError(
                        "coa_not_active",
                        f"COA is {old['coa_status']!r}; only active COAs can be replaced.",
                        409,
                    )
                if not actor_is_admin and old["uploaded_by"] != actor_user_id:
                    raise AuthError(
                        "only_uploader_can_replace",
                        "Only the original uploader (or an admin) can replace this COA.",
                        403,
                    )
                if not actor_is_admin and old["uploaded_at"] is not None:
                    elapsed_h = (datetime.now(timezone.utc) - old["uploaded_at"]).total_seconds() / 3600
                    if elapsed_h > _REPLACE_WINDOW_HOURS:
                        raise AuthError(
                            "replace_window_expired",
                            f"Replace window of {_REPLACE_WINDOW_HOURS}h has elapsed.",
                            403,
                        )

                # Lock acquired and gates passed — write to storage now,
                # while still inside the txn so a DB failure rolls everything
                # back AND we sweep the blob below.
                s3_key = storage_mod.new_storage_key("coa", effective_mime)
                try:
                    storage.put(s3_key, file_bytes, effective_mime)
                except Exception as e:
                    logger.error("coa.replace.s3_failed coa_id=%s err=%r", coa_id, e)
                    raise AuthError("s3_upload_failed", "Storage write failed.", 502)

                inherited_params = _resolve_inherited_params(
                    parsed_params, old["parsed_params_json"],
                )
                await conn.execute(
                    """
                    INSERT INTO coa_document (
                        coa_id, transaction_no, line_number, dock_intimation_id,
                        qc_intimation_id, sku_id, sku_name_raw, supplier_id,
                        lot_number, vendor_coa_date, parsed_params_json,
                        s3_key, file_name, file_size_bytes, mime_type,
                        scan_status, coa_status, uploaded_by, uploaded_at,
                        remarks, entity, replaces_coa_id
                    )
                    VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, COALESCE($10::date, $11), $12::jsonb,
                        $13, $14, $15, $16,
                        'pending', 'active', $17, $18,
                        $19, $20, $21
                    )
                    """,
                    new_coa_id, old["transaction_no"], old["line_number"],
                    old["dock_intimation_id"], old["qc_intimation_id"],
                    old["sku_id"], old["sku_name_raw"], old["supplier_id"],
                    old["lot_number"], vendor_coa_date, old["vendor_coa_date"],
                    inherited_params,
                    s3_key, file_name, len(file_bytes), effective_mime,
                    actor_user_id, now,
                    old["remarks"], old["entity"], coa_id,
                )
                await conn.execute(
                    """
                    UPDATE coa_document
                       SET coa_status      = 'superseded',
                           replaced_at     = $2,
                           replaced_reason = $3,
                           replaced_by     = $4
                     WHERE coa_id = $1
                    """,
                    coa_id, now, replaced_reason.strip(), actor_user_id,
                )
    except Exception:
        # WR-09: if storage.put succeeded but the txn rolled back, sweep blob.
        if s3_key:
            await _safe_storage_delete(storage, s3_key)
        raise

    return {
        "old_coa_id": coa_id,
        "new_coa_id": new_coa_id,
        "replaced_at": _iso_ts(now),
    }


# ── 5. DELETE ────────────────────────────────────────────────────────────


async def soft_delete_coa(
    pool, *, coa_id: str, reason: str, actor_user_id: str,
    entity_scope: list[str] | None,
) -> None:
    if not reason or not reason.strip():
        raise AuthError("reason_required", "reason is required", 400)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT coa_status, entity FROM coa_document WHERE coa_id = $1 FOR UPDATE",
                coa_id,
            )
            if row is None:
                raise AuthError("coa_not_found", "COA not found", 404)
            if entity_scope is not None and row["entity"] and \
                    row["entity"].lower() not in entity_scope:
                raise AuthError("coa_not_found", "COA not found", 404)
            if row["coa_status"] == "deleted":
                raise AuthError("already_deleted", "COA already deleted.", 409)
            await conn.execute(
                """
                UPDATE coa_document
                   SET coa_status    = 'deleted',
                       deleted_at    = NOW(),
                       deleted_by    = $2,
                       delete_reason = $3
                 WHERE coa_id = $1
                """,
                coa_id, actor_user_id, reason.strip(),
            )
