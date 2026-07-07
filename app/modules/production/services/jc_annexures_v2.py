"""Quality annexure CRUD for v2 job cards.

Five annexure types, each backed by its own v2 table (migration 020):

    Annexure A  metal_detection      pre-/post-packaging Fe/NFe/SS scans
    Annexure B  weight_check          per-sample net/gross + leak test
    Annexure C  environment           parameter-name + value (free-form)
    Annexure D  loss_reconciliation   budgeted vs actual loss per event
    Annexure E  remarks               observation / deviation / corrective_action

All five expose the same CRUD shape:
    add_<annexure>(conn, *, job_card_id, payload, recorded_by)         → POST
    patch_<annexure>(conn, *, row_id, fields, updated_by)              → PATCH
    delete_<annexure>(conn, *, row_id, deleted_by, deletion_reason)   → DELETE (soft)
    list_<annexure>(conn, job_card_id, include_deleted=False)          → GET list

Soft-delete is the only delete path — `deleted_at` stamp leaves the row
queryable for audits but excluded from default reads.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.core.warehouse_scope import user_has_warehouse
from app.modules.production.services.job_card_v2 import assert_not_locked

logger = logging.getLogger(__name__)


def _serialize(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _filter_allowed(fields: dict, allowed: frozenset[str]) -> tuple[dict, list[str]]:
    """Split a patch payload into accepted vs ignored keys.

    Returns (accepted_dict, ignored_keys_list). None values on allowed
    keys are kept (explicit clear). Unknown keys end up in the second
    list so callers can surface them — silently dropping typos used to
    return `no_change` with no hint why nothing applied.
    """
    accepted: dict = {}
    ignored: list[str] = []
    for k, v in (fields or {}).items():
        if k in allowed:
            accepted[k] = v
        else:
            ignored.append(k)
    if ignored:
        logger.warning("Ignored unknown patch keys: %s (allowed=%s)",
                       ignored, sorted(allowed))
    return accepted, ignored


def _build_patch_sql(table: str, fields: dict, row_id_col: str, row_id: int,
                     updated_by: str | None,
                     job_card_id: int | None = None) -> tuple[str, list]:
    """Build an UPDATE for a row identified by `row_id`. When
    `job_card_id` is given, the WHERE clause also requires the row's
    job_card_id to match — this closes the "wrong-JC-in-URL" bypass
    where a caller could patch a row by passing a JC they have access
    to in the URL but a row_id from a different JC.
    """
    sets: list[str] = []
    params: list = []
    idx = 1
    for k, v in fields.items():
        sets.append(f'"{k}" = ${idx}')
        params.append(v); idx += 1
    sets.append(f'"updated_by" = ${idx}'); params.append(updated_by); idx += 1
    sets.append('"updated_at" = NOW()')

    where_clauses = [f'{row_id_col} = ${idx}', 'deleted_at IS NULL']
    params.append(row_id); idx += 1
    if job_card_id is not None:
        where_clauses.append(f'job_card_id = ${idx}')
        params.append(job_card_id); idx += 1

    sql = (
        f'UPDATE {table} SET {", ".join(sets)} '
        f'WHERE {" AND ".join(where_clauses)} RETURNING *'
    )
    return sql, params


async def assert_jc_in_scope(conn, *, job_card_id: int,
                              allowed_warehouses: list[str] | None,
                              allowed_floors: list[str] | None) -> dict:
    """Floor/factory scope guard for annexure writes.

    Returns:
        {"ok": True}                        when the caller may write.
        {"error": "out_of_scope", ...}      when the JC's floor/factory
                                            is outside the caller's lock.
        {"error": "job_card_not_found"}    when the JC row doesn't exist.

    Empty allowed_* lists are treated as "no lock" and pass through.
    Admins should call this with both lists set to None so the function
    short-circuits without a DB hit — see the router for the admin bypass.
    """
    if not (allowed_warehouses or allowed_floors):
        return {"ok": True}
    jc = await conn.fetchrow(
        "SELECT factory, floor FROM job_card_v2 WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if allowed_warehouses and not user_has_warehouse(allowed_warehouses, jc["factory"]):
        return {"error": "out_of_scope", "scope": "factory",
                "value": jc["factory"], "allowed": allowed_warehouses}
    if allowed_floors and jc["floor"] and jc["floor"] not in allowed_floors:
        return {"error": "out_of_scope", "scope": "floor",
                "value": jc["floor"], "allowed": allowed_floors}
    return {"ok": True}


async def _soft_delete(conn, *, table: str, row_id_col: str, row_id: int,
                       deleted_by: str | None, reason: str | None,
                       job_card_id: int | None = None) -> dict:
    """Soft-delete with optional job_card_id binding. Same rationale as
    `_build_patch_sql`: when the JC ID from the URL is supplied, the row
    must belong to that JC or the operation 404s — prevents the
    "wrong-JC-in-URL" cross-row mutation bypass.

    Concurrency: a `SELECT … FOR UPDATE` precedes the UPDATE so a
    concurrent PATCH on the same row blocks until this transaction
    commits. Without the lock, a PATCH that began before the DELETE
    could overwrite `updated_at` / data fields on the
    already-logically-deleted row (the PATCH's `WHERE deleted_at IS NULL`
    check happens before our `SET deleted_at = NOW()` lands).
    """
    if job_card_id is None:
        existing = await conn.fetchrow(
            f'SELECT 1 FROM {table} '
            f'WHERE {row_id_col} = $1 AND deleted_at IS NULL FOR UPDATE',
            row_id,
        )
        if not existing:
            return {"error": "not_found_or_already_deleted"}
        row = await conn.fetchrow(
            f'UPDATE {table} SET deleted_at = NOW(), deleted_by = $2, '
            f'deletion_reason = $3 '
            f'WHERE {row_id_col} = $1 AND deleted_at IS NULL RETURNING *',
            row_id, deleted_by, reason,
        )
    else:
        existing = await conn.fetchrow(
            f'SELECT 1 FROM {table} '
            f'WHERE {row_id_col} = $1 AND job_card_id = $2 AND deleted_at IS NULL '
            f'FOR UPDATE',
            row_id, job_card_id,
        )
        if not existing:
            return {"error": "not_found_or_already_deleted"}
        row = await conn.fetchrow(
            f'UPDATE {table} SET deleted_at = NOW(), deleted_by = $2, '
            f'deletion_reason = $3 '
            f'WHERE {row_id_col} = $1 AND job_card_id = $4 AND deleted_at IS NULL '
            f'RETURNING *',
            row_id, deleted_by, reason, job_card_id,
        )
    if not row:
        return {"error": "not_found_or_already_deleted"}
    return {"deleted": True, "row": _serialize(row)}


# ═══════════════════════════════════════════════════════════════════════════
#  Annexure A — Metal Detection
# ═══════════════════════════════════════════════════════════════════════════

_MD_ALLOWED = frozenset({'check_type', 'fe_pass', 'nfe_pass', 'ss_pass',
                          'failed_units', 'remarks'})


async def add_metal_detection(conn, *, job_card_id: int,
                              check_type: str,
                              fe_pass: bool | None = None,
                              nfe_pass: bool | None = None,
                              ss_pass: bool | None = None,
                              failed_units: int = 0,
                              remarks: str | None = None,
                              recorded_by: str | None = None) -> dict:
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
    if check_type not in ('pre_packaging', 'post_packaging'):
        return {"error": "invalid_check_type", "value": check_type}

    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_metal_detection_v2
                (detection_id, job_card_id, check_type, fe_pass, nfe_pass,
                 ss_pass, failed_units, remarks, recorded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, check_type, fe_pass, nfe_pass, ss_pass,
            failed_units or 0, remarks, recorded_by,
        )
    row = await insert_with_pk_retry(conn, _insert)
    return {"added": True, "row": _serialize(row)}


async def patch_metal_detection(conn, *, detection_id: int,
                                fields: dict, updated_by: str | None = None,
                                job_card_id: int | None = None) -> dict:
    f, ignored = _filter_allowed(fields, _MD_ALLOWED)
    # failed_units is a counter — add() defaults it to 0 and the column is
    # NOT NULL semantically. Allowing PATCH to clear it would create a
    # value `add()` never produces. Drop explicit-null patches on this
    # field rather than corrupting the contract.
    if 'failed_units' in f and f['failed_units'] is None:
        del f['failed_units']
        ignored.append('failed_units (null not allowed on counter)')
    if not f:
        return {"error": "no_change", "ignored_keys": ignored} if ignored else {"error": "no_change"}
    sql, params = _build_patch_sql(
        'job_card_metal_detection_v2', f, 'detection_id', detection_id,
        updated_by, job_card_id=job_card_id,
    )
    row = await conn.fetchrow(sql, *params)
    if not row:
        return {"error": "not_found"}
    out = {"updated": True, "row": _serialize(row)}
    if ignored: out["ignored_keys"] = ignored
    return out


async def delete_metal_detection(conn, *, detection_id: int,
                                  deleted_by: str | None = None,
                                  reason: str | None = None,
                                  job_card_id: int | None = None) -> dict:
    return await _soft_delete(
        conn, table='job_card_metal_detection_v2', row_id_col='detection_id',
        row_id=detection_id, deleted_by=deleted_by, reason=reason,
        job_card_id=job_card_id,
    )


async def list_metal_detection(conn, job_card_id: int,
                                include_deleted: bool = False) -> list[dict]:
    where = "job_card_id = $1" + ("" if include_deleted else " AND deleted_at IS NULL")
    rows = await conn.fetch(
        f"SELECT * FROM job_card_metal_detection_v2 WHERE {where} ORDER BY recorded_at",
        job_card_id,
    )
    return [_serialize(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
#  Annexure B — Weight Checks
# ═══════════════════════════════════════════════════════════════════════════

_WC_ALLOWED = frozenset({'sample_number', 'net_weight', 'gross_weight',
                          'leak_test_pass', 'remarks'})


async def add_weight_check(conn, *, job_card_id: int,
                           sample_number: int,
                           net_weight: float | None = None,
                           gross_weight: float | None = None,
                           leak_test_pass: bool | None = None,
                           remarks: str | None = None,
                           recorded_by: str | None = None) -> dict:
    """Insert a weight-check sample. If an active row already exists for
    this (job_card_id, sample_number) — e.g. the client is retrying a
    fan-out that previously partially succeeded — the existing row is
    UPDATED in place instead of creating a duplicate. Soft-deleted rows
    are excluded from the conflict target by the partial unique index
    (`uq_jcwc_v2_jc_sample_active`), so delete + re-add still works.
    """
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
    if sample_number is None or sample_number <= 0:
        return {"error": "invalid_sample_number"}

    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_weight_check_v2
                (check_id, job_card_id, sample_number, net_weight, gross_weight,
                 leak_test_pass, remarks, recorded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (job_card_id, sample_number) WHERE deleted_at IS NULL
            DO UPDATE SET
                net_weight     = EXCLUDED.net_weight,
                gross_weight   = EXCLUDED.gross_weight,
                leak_test_pass = EXCLUDED.leak_test_pass,
                remarks        = EXCLUDED.remarks,
                updated_by     = EXCLUDED.recorded_by,
                updated_at     = NOW()
            RETURNING *, (xmax = 0) AS inserted
            """,
            new_short_time_id(),
            job_card_id, sample_number, net_weight, gross_weight,
            leak_test_pass, remarks, recorded_by,
        )
    row = await insert_with_pk_retry(conn, _insert)
    # xmax = 0 ⇒ a brand-new row; otherwise we updated an existing one.
    # Surface this so the client knows when its retry hit an existing row.
    was_insert = bool(row.get("inserted")) if row is not None else True
    payload = _serialize({k: v for k, v in dict(row).items() if k != 'inserted'})
    return {("added" if was_insert else "updated"): True, "row": payload}


async def patch_weight_check(conn, *, check_id: int,
                             fields: dict, updated_by: str | None = None,
                             job_card_id: int | None = None) -> dict:
    f, ignored = _filter_allowed(fields, _WC_ALLOWED)
    if not f:
        return {"error": "no_change", "ignored_keys": ignored} if ignored else {"error": "no_change"}
    sql, params = _build_patch_sql(
        'job_card_weight_check_v2', f, 'check_id', check_id,
        updated_by, job_card_id=job_card_id,
    )
    row = await conn.fetchrow(sql, *params)
    if not row:
        return {"error": "not_found"}
    out = {"updated": True, "row": _serialize(row)}
    if ignored: out["ignored_keys"] = ignored
    return out


async def delete_weight_check(conn, *, check_id: int,
                              deleted_by: str | None = None,
                              reason: str | None = None,
                              job_card_id: int | None = None) -> dict:
    return await _soft_delete(
        conn, table='job_card_weight_check_v2', row_id_col='check_id',
        row_id=check_id, deleted_by=deleted_by, reason=reason,
        job_card_id=job_card_id,
    )


async def list_weight_check(conn, job_card_id: int,
                             include_deleted: bool = False) -> list[dict]:
    where = "job_card_id = $1" + ("" if include_deleted else " AND deleted_at IS NULL")
    rows = await conn.fetch(
        f"SELECT * FROM job_card_weight_check_v2 WHERE {where} ORDER BY sample_number",
        job_card_id,
    )
    return [_serialize(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
#  Annexure C — Environment
# ═══════════════════════════════════════════════════════════════════════════

_ENV_ALLOWED = frozenset({'parameter_name', 'value', 'unit', 'remarks'})


async def add_environment(conn, *, job_card_id: int,
                          parameter_name: str,
                          value: str | None = None,
                          unit: str | None = None,
                          remarks: str | None = None,
                          recorded_by: str | None = None) -> dict:
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
    if not parameter_name or not parameter_name.strip():
        return {"error": "missing_parameter_name"}

    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_environment_v2
                (env_id, job_card_id, parameter_name, value, unit, remarks, recorded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, parameter_name.strip(), value, unit, remarks, recorded_by,
        )
    row = await insert_with_pk_retry(conn, _insert)
    return {"added": True, "row": _serialize(row)}


async def patch_environment(conn, *, env_id: int,
                            fields: dict, updated_by: str | None = None,
                            job_card_id: int | None = None) -> dict:
    f, ignored = _filter_allowed(fields, _ENV_ALLOWED)
    if not f:
        return {"error": "no_change", "ignored_keys": ignored} if ignored else {"error": "no_change"}
    sql, params = _build_patch_sql(
        'job_card_environment_v2', f, 'env_id', env_id,
        updated_by, job_card_id=job_card_id,
    )
    row = await conn.fetchrow(sql, *params)
    if not row:
        return {"error": "not_found"}
    out = {"updated": True, "row": _serialize(row)}
    if ignored: out["ignored_keys"] = ignored
    return out


async def delete_environment(conn, *, env_id: int,
                              deleted_by: str | None = None,
                              reason: str | None = None,
                              job_card_id: int | None = None) -> dict:
    return await _soft_delete(
        conn, table='job_card_environment_v2', row_id_col='env_id',
        row_id=env_id, deleted_by=deleted_by, reason=reason,
        job_card_id=job_card_id,
    )


async def list_environment(conn, job_card_id: int,
                            include_deleted: bool = False) -> list[dict]:
    where = "job_card_id = $1" + ("" if include_deleted else " AND deleted_at IS NULL")
    rows = await conn.fetch(
        f"SELECT * FROM job_card_environment_v2 WHERE {where} ORDER BY recorded_at",
        job_card_id,
    )
    return [_serialize(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
#  Annexure D — Loss Reconciliation (per-event)
# ═══════════════════════════════════════════════════════════════════════════

_LOSS_ALLOWED = frozenset({'loss_category', 'budgeted_loss_pct', 'budgeted_loss_qty',
                            'actual_loss_qty', 'variance_qty', 'uom', 'remarks'})

VALID_LOSS_CATEGORIES = (
    'sorting_rejection', 'roasting_loss', 'packaging_rejection',
    'metal_detector', 'spillage', 'qc_sample', 'other',
)


async def add_loss_reconciliation(conn, *, job_card_id: int,
                                   loss_category: str,
                                   budgeted_loss_pct: float | None = None,
                                   budgeted_loss_qty: float | None = None,
                                   actual_loss_qty: float | None = None,
                                   uom: str | None = None,
                                   remarks: str | None = None,
                                   recorded_by: str | None = None) -> dict:
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
    if loss_category not in VALID_LOSS_CATEGORIES:
        return {"error": "invalid_loss_category", "value": loss_category}

    # variance = actual - budgeted, when both are known
    variance = None
    if actual_loss_qty is not None and budgeted_loss_qty is not None:
        variance = round(actual_loss_qty - budgeted_loss_qty, 3)

    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_loss_reconciliation_v2
                (recon_id, job_card_id, loss_category,
                 budgeted_loss_pct, budgeted_loss_qty, actual_loss_qty,
                 variance_qty, uom, remarks, recorded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, loss_category,
            budgeted_loss_pct, budgeted_loss_qty, actual_loss_qty,
            variance, uom, remarks, recorded_by,
        )
    row = await insert_with_pk_retry(conn, _insert)
    return {"added": True, "row": _serialize(row)}


async def patch_loss_reconciliation(conn, *, recon_id: int,
                                    fields: dict, updated_by: str | None = None,
                                    job_card_id: int | None = None) -> dict:
    f, ignored = _filter_allowed(fields, _LOSS_ALLOWED)
    if not f:
        return {"error": "no_change", "ignored_keys": ignored} if ignored else {"error": "no_change"}
    # Recompute variance when actual or budgeted is being patched. If the
    # caller explicitly nulled either side, variance is also cleared so
    # it doesn't sit stale; the previous version left it pointing at the
    # old delta.
    if 'actual_loss_qty' in f or 'budgeted_loss_qty' in f:
        # FOR UPDATE so a concurrent PATCH on the same row can't sneak
        # a stale variance through between this read and our UPDATE.
        cur = await conn.fetchrow(
            "SELECT actual_loss_qty, budgeted_loss_qty FROM job_card_loss_reconciliation_v2 "
            "WHERE recon_id = $1 AND deleted_at IS NULL FOR UPDATE",
            recon_id,
        )
        if cur:
            actual = f.get('actual_loss_qty', cur['actual_loss_qty'])
            budget = f.get('budgeted_loss_qty', cur['budgeted_loss_qty'])
            if actual is not None and budget is not None:
                f['variance_qty'] = round(float(actual) - float(budget), 3)
            else:
                # One side cleared → variance becomes undefined.
                f['variance_qty'] = None
    sql, params = _build_patch_sql(
        'job_card_loss_reconciliation_v2', f, 'recon_id', recon_id,
        updated_by, job_card_id=job_card_id,
    )
    row = await conn.fetchrow(sql, *params)
    if not row:
        return {"error": "not_found"}
    out = {"updated": True, "row": _serialize(row)}
    if ignored: out["ignored_keys"] = ignored
    return out


async def delete_loss_reconciliation(conn, *, recon_id: int,
                                      deleted_by: str | None = None,
                                      reason: str | None = None,
                                      job_card_id: int | None = None) -> dict:
    return await _soft_delete(
        conn, table='job_card_loss_reconciliation_v2', row_id_col='recon_id',
        row_id=recon_id, deleted_by=deleted_by, reason=reason,
        job_card_id=job_card_id,
    )


async def list_loss_reconciliation(conn, job_card_id: int,
                                    include_deleted: bool = False) -> list[dict]:
    where = "job_card_id = $1" + ("" if include_deleted else " AND deleted_at IS NULL")
    rows = await conn.fetch(
        f"SELECT * FROM job_card_loss_reconciliation_v2 WHERE {where} ORDER BY recorded_at",
        job_card_id,
    )
    return [_serialize(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
#  Annexure E — Remarks
# ═══════════════════════════════════════════════════════════════════════════

_REM_ALLOWED = frozenset({'remark_type', 'content'})

VALID_REMARK_TYPES = ('observation', 'deviation', 'corrective_action')


async def add_remark(conn, *, job_card_id: int,
                     remark_type: str,
                     content: str,
                     recorded_by: str | None = None) -> dict:
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
    if remark_type not in VALID_REMARK_TYPES:
        return {"error": "invalid_remark_type", "value": remark_type}
    if not content or not content.strip():
        return {"error": "empty_content"}

    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_remarks_v2
                (remark_id, job_card_id, remark_type, content, recorded_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, remark_type, content.strip(), recorded_by,
        )
    row = await insert_with_pk_retry(conn, _insert)
    return {"added": True, "row": _serialize(row)}


async def patch_remark(conn, *, remark_id: int,
                       fields: dict, updated_by: str | None = None,
                       job_card_id: int | None = None) -> dict:
    f, ignored = _filter_allowed(fields, _REM_ALLOWED)
    if not f:
        return {"error": "no_change", "ignored_keys": ignored} if ignored else {"error": "no_change"}
    sql, params = _build_patch_sql(
        'job_card_remarks_v2', f, 'remark_id', remark_id,
        updated_by, job_card_id=job_card_id,
    )
    row = await conn.fetchrow(sql, *params)
    if not row:
        return {"error": "not_found"}
    out = {"updated": True, "row": _serialize(row)}
    if ignored: out["ignored_keys"] = ignored
    return out


async def delete_remark(conn, *, remark_id: int,
                        deleted_by: str | None = None,
                        reason: str | None = None,
                        job_card_id: int | None = None) -> dict:
    return await _soft_delete(
        conn, table='job_card_remarks_v2', row_id_col='remark_id',
        row_id=remark_id, deleted_by=deleted_by, reason=reason,
        job_card_id=job_card_id,
    )


async def list_remarks(conn, job_card_id: int,
                        include_deleted: bool = False) -> list[dict]:
    where = "job_card_id = $1" + ("" if include_deleted else " AND deleted_at IS NULL")
    rows = await conn.fetch(
        f"SELECT * FROM job_card_remarks_v2 WHERE {where} ORDER BY recorded_at",
        job_card_id,
    )
    return [_serialize(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
#  Aggregate read — attach all annexures to a JC detail payload
# ═══════════════════════════════════════════════════════════════════════════

async def get_annexures_for_jc(conn, job_card_id: int) -> dict:
    """Returns all five annexure lists for a JC. Used by get_job_card to
    populate the legacy section keys so the existing UI keeps decoding."""
    return {
        "metal_detection":     await list_metal_detection(conn, job_card_id),
        "weight_check":        await list_weight_check(conn, job_card_id),
        "environment":         await list_environment(conn, job_card_id),
        "loss_reconciliation": await list_loss_reconciliation(conn, job_card_id),
        "remarks":             await list_remarks(conn, job_card_id),
    }
