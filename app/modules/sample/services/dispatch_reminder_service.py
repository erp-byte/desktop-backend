"""NPD dispatch-date reminders — the scan, the send-once guard, and the loop.

A sample requisition carries an expected_dispatch_date set by BD, and nothing used to
watch it. This warns the NPD team, the sales POC and the business head the day before,
then chases NPD and the BH every day it stays past due until the BH cancels the request
or moves the date.

Design doc: docs/2026-09-04-npd-dispatch-reminders-design.md
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone

import asyncpg

from app.core.helpers import new_short_time_id

logger = logging.getLogger(__name__)

# Fixed +05:30 rather than ZoneInfo("Asia/Kolkata"): India has no DST, so the offset is
# exact, and it avoids depending on system tzdata (absent on Windows without the `tzdata`
# package, which is deliberately not a dependency).
IST = timezone(timedelta(hours=5, minutes=30))

KIND_DUE_NPD = "DUE_TOMORROW_NPD"
KIND_DUE_OWNER = "DUE_TOMORROW_OWNER"
KIND_OVERDUE_NPD = "OVERDUE_NPD"
KIND_OVERDUE_OWNER = "OVERDUE_OWNER"

# A requisition past these has shipped (INTERNALLY_DISPATCHED / GATE_PASS_ISSUED /
# CLOSED) or is dead (BH_REJECTED / CANCELLED). Mirrors OPEN_STATUSES in the web app's
# dashboard/_build.ts — keep the two in step.
OPEN_STATUSES = ("DRAFT", "SUBMITTED", "BH_APPROVED", "ON_HOLD",
                 "IN_PRODUCTION", "PACKING", "READY_FOR_DISPATCH", "PARTIALLY_CONVERTED")


def ist_today() -> date:
    """Today in IST. Every date comparison in this module goes through here."""
    return datetime.now(IST).date()


async def has_log_table(conn) -> bool:
    """Whether migration 087 is applied. samples/ migrations are hand-applied, so an
    unmigrated environment must no-op rather than raise on every tick."""
    return bool(await conn.fetchval(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'sample_dispatch_reminder_log'"))


async def claim(conn, req_id: int, kind: str, day: date) -> bool:
    """Claim the right to send `kind` for `req_id` on `day`. True only for the caller
    that won the row — everyone else (a retried tick, a second replica) gets False and
    must not send. The INSERT *is* the lock; there is no read-then-write to race."""
    for _attempt in range(5):
        cand = new_short_time_id()
        try:
            async with conn.transaction():          # savepoint for the id retry
                got = await conn.fetchval(
                    """INSERT INTO sample_dispatch_reminder_log
                           (id, requisition_id, kind, sent_on)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (requisition_id, kind, sent_on) DO NOTHING
                       RETURNING id""",
                    cand, req_id, kind, day)
            return got is not None
        except asyncpg.UniqueViolationError:
            # The PK collided (not the send-once key, which is handled by ON CONFLICT).
            # Retry with a fresh id — treating this as "already sent" would silently
            # swallow the mail. new_short_time_id() is millisecond-resolution, so retry
            # immediately and the next candidate can be identical to this one; sleep
            # briefly so the epoch-ms counter advances (same convention as
            # PK_RETRY_DELAY_S in app.core.helpers.insert_with_pk_retry).
            await asyncio.sleep(0.002)
            continue
    logger.warning("[dispatch-reminder] could not mint an id for req %s kind %s", req_id, kind)
    return False


async def release_overdue(conn, req_id: int) -> None:
    """Forget this requisition's overdue chase — called when the BH moves the date, so
    the new one earns a fresh warning instead of being silenced by yesterday's rows."""
    await conn.execute(
        "DELETE FROM sample_dispatch_reminder_log "
        " WHERE requisition_id = $1 AND kind LIKE 'OVERDUE%'", req_id)


async def due_buckets(conn, today: date) -> dict:
    """Split the chaseable requisitions into the two buckets, against an IST `today`.

    One query, bucketed in Python: the row set is small (open requisitions with a date),
    and doing it here keeps the boundary rules in one readable place instead of two
    near-identical SQL predicates.
    """
    placeholders = ", ".join(f"'{s}'" for s in OPEN_STATUSES)   # module constants, not input
    rows = await conn.fetch(
        f"""SELECT * FROM sample_requisitions
             WHERE deleted_at IS NULL
               AND expected_dispatch_date IS NOT NULL
               AND status IN ({placeholders})
               AND expected_dispatch_date <= $1 + 1
             ORDER BY expected_dispatch_date, id""", today)
    due, over = [], []
    for r in rows:
        d = dict(r)
        exp = d["expected_dispatch_date"]
        if exp == today + timedelta(days=1):
            d["overdue_days"] = 0
            due.append(d)
        elif exp < today:
            d["overdue_days"] = (today - exp).days
            over.append(d)
        # exp == today falls through: the warning is D-1, the chase D+1.
    return {"due_tomorrow": due, "overdue": over}
