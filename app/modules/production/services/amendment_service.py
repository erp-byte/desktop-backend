"""Amendment tracking service — Section I2."""


# ── Get amendment history for a record+field ──
async def get_amendments(conn, *, record_id, record_type, field=None):
    conditions = ["record_id = $1", "record_type = $2"]
    params = [record_id, record_type]
    idx = 3

    if field:
        conditions.append(f"field_name = ${idx}")
        params.append(field)
        idx += 1

    where = " AND ".join(conditions)

    rows = await conn.fetch(f"""
        SELECT * FROM amendment_log
        WHERE {where}
        ORDER BY changed_at DESC
        LIMIT 50
    """, *params)

    return {"results": [dict(r) for r in rows]}


# ── Get amendment count for a record ──
async def get_amendment_count(conn, *, record_id, record_type):
    count = await conn.fetchval("""
        SELECT COUNT(*) FROM amendment_log
        WHERE record_id = $1 AND record_type = $2
    """, record_id, record_type)
    return {"count": count}


# ── Log an amendment (called by other services on field changes) ──
async def log_amendment(conn, *, record_id, record_type, field_name,
                         previous_value, new_value, changed_by, reason=None):
    if str(previous_value) == str(new_value):
        return  # No actual change

    await conn.execute("""
        INSERT INTO amendment_log
            (record_id, record_type, field_name, previous_value,
             new_value, changed_by, reason)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
    """, record_id, record_type, field_name,
         str(previous_value) if previous_value is not None else None,
         str(new_value) if new_value is not None else None,
         changed_by, reason)


# ── Job-card edit log (whole-JC audit: header/tabs + box data) ──────────────
# The whole job card's change history lives in amendment_log keyed by
# record_id = str(job_card_id): record_type 'job_card' for JC/tab fields,
# 'job_card_box' for per-box edits (field_name = "box:<box_id>.<field>"). The
# frontend reads this to paint every ever-edited value light red.
_JC_LOG_RECORD_TYPES = ("job_card", "job_card_box")


async def log_jc_field_changes(conn, *, job_card_id, record_type, field_prefix,
                               before, after, changed_by, reason=None):
    """Diff two field dicts (same keys) and log one amendment row per CHANGED
    field. field_name = f"{field_prefix}{key}". No-ops are skipped by
    log_amendment. Runs in the caller's txn."""
    for key, new_val in after.items():
        await log_amendment(
            conn, record_id=str(job_card_id), record_type=record_type,
            field_name=f"{field_prefix}{key}", previous_value=before.get(key),
            new_value=new_val, changed_by=changed_by, reason=reason,
        )


async def list_jc_edit_log(conn, job_card_id):
    """Every recorded change for one job card (header/tabs + box data), newest
    first. Drives the frontend's light-red 'this was edited' markers."""
    rows = await conn.fetch(
        """
        SELECT record_type, field_name, previous_value, new_value,
               changed_by, changed_at, reason
          FROM amendment_log
         WHERE record_id = $1 AND record_type = ANY($2::text[])
         ORDER BY changed_at DESC
         LIMIT 5000
        """,
        str(job_card_id), list(_JC_LOG_RECORD_TYPES),
    )
    return [dict(r) for r in rows]
