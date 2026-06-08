"""Job-card additive consumption — data-keeping bucket.

These rows track seasoning (salt, sugar, citric acid, gum powder,
edible oils, cayenne pepper, etc.) that goes into a batch but produces
no measurable output. They are 100 % consumed by intent, so they would
create artificial unbalance if they fed the conservation identity.

Persisted to job_card_additive_consumption_v2 (035_jc_additives.sql).

The Accounting Summary surfaces a rolled-up total but excludes the
value from is_balanced / balance_difference checks — see
jc_accounting_v2.save_accounting which does NOT read this table.

Public surface:
    save_additives(conn, *, job_card_id, rows, recorded_by) -> dict
        Replace-all semantics — wipes the prior rows for the JC then
        inserts the supplied ones. Mirrors save_byproducts / replace_
        balance_materials in jc_accounting_v2 so the POST /outputs
        body can hand all three lists over to identical "wipe + write"
        helpers and the JC ends up in one consistent state.

    list_additives(conn, job_card_id) -> list[dict]
        Read-side helper returned in the JC detail payload's
        `additives` field.
"""

import logging

from asyncpg.exceptions import UndefinedColumnError, UndefinedTableError

from app.core.helpers import insert_with_pk_retry, new_short_time_id

logger = logging.getLogger(__name__)

# One-shot flag so the "table missing" log only fires once per process
# instead of on every JC detail load. Future migrations flip the table
# into existence; the next pool connection re-prepares the statement
# and the flag is moot.
_MISSING_TABLE_LOGGED = False


def _warn_table_missing_once() -> None:
    """Log a single warning when migration 035 hasn't been applied yet.
    Subsequent calls in the same process stay quiet so the log isn't
    flooded by polling clients."""
    global _MISSING_TABLE_LOGGED
    if _MISSING_TABLE_LOGGED:
        return
    _MISSING_TABLE_LOGGED = True
    logger.warning(
        "job_card_additive_consumption_v2 does not exist — apply "
        "migration 035_jc_additives.sql to enable the additives feature. "
        "The JC detail / save flow will silently no-op until then."
    )


async def list_additives(conn, job_card_id: int) -> list[dict]:
    """Read the additive rows for a job card, ordered oldest-first so
    the operator sees them in the same sequence they were entered.

    Falls back to an empty list when migration 035 hasn't been applied
    yet — surfaces a single warning per process so the operator can
    see they need to run the migration, but doesn't 500 every JC
    detail load while the DB catches up.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT additive_id, batch_id, sku_name, material_name, qty_kg,
                   remarks, recorded_by, recorded_at
            FROM job_card_additive_consumption_v2
            WHERE job_card_id = $1
            ORDER BY recorded_at ASC, additive_id ASC
            """,
            job_card_id,
        )
    except UndefinedTableError:
        _warn_table_missing_once()
        return []
    except UndefinedColumnError:
        # Stage 2: batch_id added by migration 038.  Same fallback
        # pattern as the consumption / byproducts / balance SELECTs in
        # job_card_v2.get_job_card: project NULL until psql has run.
        logger.warning(
            "job_card_additive_consumption_v2.batch_id does not exist — "
            "apply migration 038_jc_batch_per_record.sql.  Additive reads "
            "will return batch_id=NULL until then."
        )
        rows = await conn.fetch(
            """
            SELECT additive_id, NULL::BIGINT AS batch_id, sku_name,
                   material_name, qty_kg, remarks, recorded_by, recorded_at
            FROM job_card_additive_consumption_v2
            WHERE job_card_id = $1
            ORDER BY recorded_at ASC, additive_id ASC
            """,
            job_card_id,
        )
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        # NUMERIC + TIMESTAMPTZ → JSON-safe primitives.  The detail
        # endpoint that nests this list runs through FastAPI's default
        # encoder which doesn't know Decimal.
        if d.get("qty_kg") is not None:
            d["qty_kg"] = float(d["qty_kg"])
        if d.get("recorded_at") is not None:
            d["recorded_at"] = d["recorded_at"].isoformat()
        out.append(d)
    return out


async def save_additives(
    conn,
    *,
    job_card_id: int,
    rows: list[dict],
    recorded_by: str | None = None,
    batch_id: int | None = None,
) -> dict:
    """Replace the additive rows for a job card.

    Wipes the prior rows for the JC then inserts the supplied list.
    Each row dict accepts:
        sku_name      str | None  — picked from the all_sku dropdown
        material_name str | None  — free-text "Others" path
        qty_kg        float       — required, ≥ 0
        remarks       str | None

    Returns {"saved": int, "deleted_prior": int}.  Silently drops any
    row whose qty_kg is None / <= 0 — those are blank UI rows the
    operator left in place but didn't fill in.

    The (sku_name, material_name) pair must satisfy "at least one is
    non-empty" — same constraint as the CHECK on the table.  Rows that
    fail this validation are dropped with a logger.warning so a partial
    upload doesn't 500 the whole POST /outputs save.
    """

    # Wipe prior rows first.  CASCADE on the FK to job_card_v2 already
    # handles the JC-cancelled path; this is the per-save wipe so a
    # re-save doesn't accumulate stale rows.
    #
    # Same defensive fallback as list_additives — when migration 035
    # hasn't been applied, swallow the UndefinedTableError so the
    # operator's other Save Output work (consumption, off-grade,
    # balance) still lands. The single warning + empty result mirrors
    # the read-side semantics.
    # Stage 2: scope the wholesale wipe to the SAME batch we're about
    # to write into.  Two batches on the same JC can carry independent
    # additive sets; when batch_id is None we wipe only NULL-batched
    # rows so existing batched data is preserved.
    try:
        if batch_id is None:
            prior_count = await conn.fetchval(
                "SELECT COUNT(*) FROM job_card_additive_consumption_v2 "
                "WHERE job_card_id = $1 AND batch_id IS NULL",
                job_card_id,
            )
            await conn.execute(
                "DELETE FROM job_card_additive_consumption_v2 "
                "WHERE job_card_id = $1 AND batch_id IS NULL",
                job_card_id,
            )
        else:
            prior_count = await conn.fetchval(
                "SELECT COUNT(*) FROM job_card_additive_consumption_v2 "
                "WHERE job_card_id = $1 AND batch_id = $2",
                job_card_id, batch_id,
            )
            await conn.execute(
                "DELETE FROM job_card_additive_consumption_v2 "
                "WHERE job_card_id = $1 AND batch_id = $2",
                job_card_id, batch_id,
            )
    except UndefinedTableError:
        _warn_table_missing_once()
        return {"saved": 0, "deleted_prior": 0, "skipped": "table_missing"}

    if not rows:
        return {"saved": 0, "deleted_prior": int(prior_count or 0)}

    saved = 0
    for raw in rows:
        try:
            qty = float(raw.get("qty_kg") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue

        sku_name = (raw.get("sku_name") or "").strip() or None
        material_name = (raw.get("material_name") or "").strip() or None
        if not sku_name and not material_name:
            logger.warning(
                "save_additives: dropping row for job_card_id=%s — "
                "neither sku_name nor material_name supplied (qty=%s)",
                job_card_id, qty,
            )
            continue

        remarks = (raw.get("remarks") or "").strip() or None

        async def _insert(_batch=batch_id):
            return await conn.execute(
                """
                INSERT INTO job_card_additive_consumption_v2
                    (additive_id, job_card_id, batch_id, sku_name, material_name,
                     qty_kg, remarks, recorded_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                new_short_time_id(),
                job_card_id,
                _batch,
                sku_name,
                material_name,
                qty,
                remarks,
                recorded_by,
            )

        await insert_with_pk_retry(conn, _insert)
        saved += 1

    return {"saved": saved, "deleted_prior": int(prior_count or 0)}
