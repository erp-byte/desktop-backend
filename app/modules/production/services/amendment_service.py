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
