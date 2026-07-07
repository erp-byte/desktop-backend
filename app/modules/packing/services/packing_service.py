"""Packing Details — DB access.

Mirrors the job-card v2 PK convention: ``packing_id`` is an app-supplied
8-digit time-based BIGINT minted by :func:`new_short_time_id`, inserted through
:func:`insert_with_pk_retry` so a rare same-millisecond collision retries with a
fresh id inside a SAVEPOINT. See app/core/helpers.py and
app/modules/production/services/job_card_v2.py for the reference usage.

asyncpg has no JSON codec registered on this pool, so ``details`` is written as
a JSON string cast with ``$n::jsonb`` and read back as text — hence the
``_details_to_dict`` normaliser on the way out.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.helpers import insert_with_pk_retry, new_short_time_id

# Column list shared by every SELECT/RETURNING so the row->dict mapping below
# always sees the same shape.
_COLS = """packing_id, batch_code, article_name, details,
           created_by, created_at, updated_at"""


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _details_to_dict(v: Any) -> dict:
    """Normalise the stored JSONB back to a dict (asyncpg returns it as text)."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _row_to_out(row) -> dict:
    return {
        "packing_id": row["packing_id"],
        "batch_code": row["batch_code"],
        "article_name": row["article_name"],
        "details": _details_to_dict(row["details"]),
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def create_packing_detail(
    conn,
    *,
    batch_code: str,
    article_name: str,
    details: dict | None,
    created_by: str | None,
) -> dict:
    """INSERT one packing_details row with an app-supplied 8-digit PK.

    MUST run inside an open transaction — insert_with_pk_retry uses a SAVEPOINT
    to isolate the PK-collision retry. The router wraps this in
    ``async with conn.transaction()``.
    """
    details_json = json.dumps(details or {})

    async def _insert():
        # New id minted on EACH attempt so a PK collision can succeed on retry.
        return await conn.fetchrow(
            f"""
            INSERT INTO packing_details
                (packing_id, batch_code, article_name, details, created_by)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            RETURNING {_COLS}
            """,
            new_short_time_id(),
            batch_code,
            article_name,
            details_json,
            created_by,
        )

    row = await insert_with_pk_retry(conn, _insert)
    return _row_to_out(row)


async def get_packing_detail(conn, packing_id: int) -> dict | None:
    row = await conn.fetchrow(
        f"SELECT {_COLS} FROM packing_details WHERE packing_id = $1",
        packing_id,
    )
    return _row_to_out(row) if row else None


async def list_packing_details(
    conn,
    *,
    batch_code: str | None = None,
    article_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Newest-first list, optionally filtered by batch_code / article_name.

    The ``$n::text IS NULL OR col = $n`` pattern lets a single prepared query
    serve every filter combination without string-building the WHERE clause.
    """
    rows = await conn.fetch(
        f"""
        SELECT {_COLS}
          FROM packing_details
         WHERE ($1::text IS NULL OR batch_code   = $1)
           AND ($2::text IS NULL OR article_name = $2)
         ORDER BY created_at DESC, packing_id DESC
         LIMIT $3 OFFSET $4
        """,
        batch_code,
        article_name,
        limit,
        offset,
    )
    return [_row_to_out(r) for r in rows]


async def update_packing_detail(
    conn,
    packing_id: int,
    *,
    batch_code: str | None = None,
    article_name: str | None = None,
    details: dict | None = None,
    details_provided: bool = False,
) -> dict | None:
    """Patch only the provided fields and always bump updated_at.

    A ``None`` batch_code / article_name means "not provided" (skip) — the
    columns are NOT NULL, so they can never be cleared. ``details_provided``
    distinguishes an omitted body from an explicit replacement (including
    replacing with ``{}``). Returns None if the row does not exist.
    """
    sets: list[str] = []
    params: list[Any] = []
    idx = 1

    if batch_code is not None:
        sets.append(f"batch_code = ${idx}")
        params.append(batch_code)
        idx += 1
    if article_name is not None:
        sets.append(f"article_name = ${idx}")
        params.append(article_name)
        idx += 1
    if details_provided:
        sets.append(f"details = ${idx}::jsonb")
        params.append(json.dumps(details or {}))
        idx += 1

    sets.append("updated_at = NOW()")

    params.append(packing_id)
    row = await conn.fetchrow(
        f"""
        UPDATE packing_details
           SET {", ".join(sets)}
         WHERE packing_id = ${idx}
        RETURNING {_COLS}
        """,
        *params,
    )
    return _row_to_out(row) if row else None


async def delete_packing_detail(conn, packing_id: int) -> bool:
    """DELETE by id. Returns True if a row was removed, False if none matched."""
    status = await conn.execute(
        "DELETE FROM packing_details WHERE packing_id = $1",
        packing_id,
    )
    # asyncpg command tag looks like "DELETE 1" / "DELETE 0".
    return status.rsplit(" ", 1)[-1] != "0"
