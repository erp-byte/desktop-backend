"""Customer-Returns approval log (cr_approval_log, app/db/137_cr_approval_log.sql):
each approval request sent ('sent', with the figures fingerprint it carried) and each
save that changed a return's figures ('changed').

Read by the decision rules for a link or WhatsApp card sent before requests carried a
fingerprint — it decides only while nothing is recorded after it — and by a save,
which sends a return again when its figures change once it has been sent. Every
helper works without the table (the migration not applied yet): nothing is recorded,
a warning is logged, and the reads find nothing.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import asyncpg

logger = logging.getLogger(__name__)

LOG = "cr_approval_log"


async def _exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1::text) IS NOT NULL", f"public.{table}"))


async def record(conn, company: str, cr_id: str, event: str, figures: str, actor: str | None,
                 outcome: dict | None = None) -> None:
    """Add one entry. In a SAVEPOINT, so a missing table can't abort a caller's
    transaction (a save records its 'changed' entry inside its own)."""
    try:
        async with conn.transaction():
            await conn.execute(
                f"INSERT INTO {LOG} (company, rtv_id, event, figures, actor, outcome) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                company.upper(), cr_id, event, figures, actor or None,
                json.dumps(outcome, default=str) if outcome is not None else None)
    except asyncpg.UndefinedTableError:
        logger.warning("[cr] %s not recorded for %s %s (%s missing; migration 137)",
                       event, company.upper(), cr_id, LOG)


async def recorded_since(conn, company: str, cr_id: str, since: datetime | None) -> bool:
    """True when a request was sent, or the figures changed, after ``since`` (None:
    at all) — so a link or card sent at ``since`` no longer shows the latest figures."""
    if not await _exists(conn, LOG):
        return False
    return bool(await conn.fetchval(
        f"SELECT EXISTS (SELECT 1 FROM {LOG} WHERE company = $1 AND rtv_id = $2 "
        "AND ($3::timestamptz IS NULL OR at > $3))", company.upper(), cr_id, since))


async def was_sent(conn, company: str, cr_id: str) -> bool:
    """Was the return sent for approval: a request recorded here, or (one sent before
    requests were recorded) a WhatsApp card for it."""
    if await _exists(conn, LOG) and await conn.fetchval(
            f"SELECT EXISTS (SELECT 1 FROM {LOG} WHERE company = $1 AND rtv_id = $2 AND event = 'sent')",
            company.upper(), cr_id):
        return True
    return await _exists(conn, "wa_return_message") and bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM wa_return_message WHERE company = $1 AND rtv_id = $2)",
        company.upper(), cr_id))


async def last_sent(conn, company: str, cr_id: str) -> dict | None:
    """The latest request recorded for the return: {figures, sent_by, sent_at}, or None."""
    if not await _exists(conn, LOG):
        return None
    row = await conn.fetchrow(
        f"SELECT figures, actor, at FROM {LOG} WHERE company = $1 AND rtv_id = $2 AND event = 'sent' "
        "ORDER BY at DESC, id DESC LIMIT 1", company.upper(), cr_id)
    return {"figures": row["figures"], "sent_by": row["actor"], "sent_at": row["at"]} if row else None
