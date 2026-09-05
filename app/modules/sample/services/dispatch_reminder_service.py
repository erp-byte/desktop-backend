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
# The D-1 warning also goes to the NPD team over WhatsApp. It claims under its OWN kind
# rather than sharing DUE_TOMORROW_NPD: the claim is per (requisition, kind, day), so a
# shared kind would let a successful email consume the day and leave a failed WhatsApp
# with nothing to retry against.
KIND_DUE_NPD_WA = "DUE_TOMORROW_NPD_WA"
KIND_DUE_OWNER_WA = "DUE_TOMORROW_OWNER_WA"
# The overdue chase reaches the BH on WhatsApp too — the only copy that carries the
# Change-date / Cancel buttons on that channel.
KIND_OVERDUE_NPD_WA = "OVERDUE_NPD_WA"
KIND_OVERDUE_OWNER_WA = "OVERDUE_OWNER_WA"

# A requisition past these has shipped (INTERNALLY_DISPATCHED / GATE_PASS_ISSUED /
# CLOSED) or is dead (BH_REJECTED / CANCELLED). Mirrors OPEN_STATUSES in the web app's
# dashboard/_build.ts — keep the two in step.
OPEN_STATUSES = ("DRAFT", "SUBMITTED", "BH_APPROVED", "ON_HOLD",
                 "IN_PRODUCTION", "PACKING", "READY_FOR_DISPATCH", "PARTIALLY_CONVERTED")


def ist_today() -> date:
    """Today in IST. Every date comparison in this module goes through here."""
    return datetime.now(IST).date()


def _is_truthy(s: str | None) -> bool:
    """Mirrors whatsapp_service._is_truthy — the house precedent for a config gate, so
    TRUE / yes / on read as ON here too, not just the narrower "1"/"true"/"True"."""
    return (s or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Parse an int env var, logging and falling back to `default` on anything that does
    not parse. Deliberately never raises: this used to sit as a bare int(os.environ[...])
    outside the loop's try, so a malformed value (e.g. "60m") raised before the loop's
    first iteration — the task then just sits in bg_tasks and the exception is swallowed
    at shutdown by asyncio.gather(..., return_exceptions=True), with nothing ever logged."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.error("[dispatch-reminder] %s=%r is not an integer — falling back to %d",
                     name, raw, default)
        return default


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


from app.modules.sample.services.sample_mail_service import (      # noqa: E402
    notify_dispatch_due_tomorrow, notify_dispatch_overdue)
from app.modules.sample.services.whatsapp_service import (        # noqa: E402
    notify_dispatch_due_tomorrow as wa_notify_dispatch_due_tomorrow,
    notify_dispatch_overdue as wa_notify_dispatch_overdue)


async def scan_and_send(conn, *, today: date, dry_run: bool = False) -> dict:
    """Resolve both buckets and send whatever has not gone out today. Returns per-kind
    counts of what was sent (or, under dry_run, what WOULD be).

    The claim comes before the send and is released if the send found no recipient: a row
    left behind for a mail nobody received would mark it sent for the rest of the day.

    dry_run's count is the bucket size, not a count of addressable mails: it returns before
    claiming or resolving recipients, so it can include a requisition an earlier tick already
    sent today, or one whose business head has no address on file. That is the safe direction
    for sizing a first batch — treat the number as an upper bound, not a promise.
    """
    if not await has_log_table(conn):
        logger.info("[dispatch-reminder] 087 not applied — nothing to do")
        return {}
    buckets = await due_buckets(conn, today)
    counts = {KIND_DUE_NPD: 0, KIND_DUE_OWNER: 0,
              KIND_DUE_NPD_WA: 0, KIND_DUE_OWNER_WA: 0,
              KIND_OVERDUE_NPD: 0, KIND_OVERDUE_OWNER: 0,
              KIND_OVERDUE_NPD_WA: 0, KIND_OVERDUE_OWNER_WA: 0}

    async def _one(req, kind, audience, send) -> None:
        if dry_run:
            counts[kind] += 1
            return
        if not await claim(conn, req["id"], kind, today):
            return                                   # already sent today, or lost the race
        try:
            ok = await send()
        except Exception:                            # noqa: BLE001
            logger.exception("[dispatch-reminder] send failed for req %s kind %s",
                             req["id"], kind)
            ok = False
        if ok:
            counts[kind] += 1
        else:
            # Undo the claim so the next tick retries rather than recording a phantom send.
            await conn.execute(
                "DELETE FROM sample_dispatch_reminder_log "
                " WHERE requisition_id = $1 AND kind = $2 AND sent_on = $3",
                req["id"], kind, today)

    for req in buckets["due_tomorrow"]:
        await _one(req, KIND_DUE_NPD, "npd",
                   lambda r=req: notify_dispatch_due_tomorrow(conn, r, audience="npd"))
        await _one(req, KIND_DUE_OWNER, "owner",
                   lambda r=req: notify_dispatch_due_tomorrow(conn, r, audience="owner"))
        await _one(req, KIND_DUE_NPD_WA, "npd",
                   lambda r=req: wa_notify_dispatch_due_tomorrow(conn, r, audience="npd"))
        await _one(req, KIND_DUE_OWNER_WA, "owner",
                   lambda r=req: wa_notify_dispatch_due_tomorrow(conn, r, audience="owner"))
    for req in buckets["overdue"]:
        d = req["overdue_days"]
        await _one(req, KIND_OVERDUE_NPD, "npd",
                   lambda r=req, d=d: notify_dispatch_overdue(conn, r, days=d, audience="npd"))
        await _one(req, KIND_OVERDUE_OWNER, "owner",
                   lambda r=req, d=d: notify_dispatch_overdue(conn, r, days=d, audience="owner"))
        await _one(req, KIND_OVERDUE_NPD_WA, "npd",
                   lambda r=req, d=d: wa_notify_dispatch_overdue(conn, r, days=d,
                                                                 audience="npd"))
        await _one(req, KIND_OVERDUE_OWNER_WA, "owner",
                   lambda r=req, d=d: wa_notify_dispatch_overdue(conn, r, days=d,
                                                                 audience="owner"))
    return counts


async def dispatch_reminder_loop(pool) -> None:
    """In-process background loop: hourly, send the day's dispatch reminders.

    Hourly rather than a single daily alarm because this loop lives and dies with the web
    process — a fixed alarm would be missed outright by a restart at the wrong minute.
    With the send-once guard, ticking often just means "the first tick after the app is up
    on a given day sends, the rest no-op", which turns a restart into a delay instead of a
    silent miss.

    NOTE: like dispatcher_loop / broadcaster_loop / promote_reminder_loop, this only ticks
    under a persistent server (uvicorn/ECS) — NOT on the Lambda/Mangum path. scan_and_send
    is deliberately callable on its own so that deployment can drive it externally.

    Unlike promote_reminder_loop, several instances running at once is SAFE here: the
    guard's unique index decides which one sends.

    Unlike promote_reminder_loop (and the other loops), this one ticks BEFORE its first
    sleep. There, sleeping first only delays a resend if the process recycles early; here it
    would silently disable the whole feature — a process that recycles faster than the tick
    (deploy loops, health-check flapping, `uvicorn --reload`) would never reach a single
    scan. An immediate first tick is safe: the send-once guard makes it idempotent, and the
    hour gate still stops a 02:00 restart from mailing anyone.
    """
    tick_min = _env_int("SAMPLE_REMINDER_TICK_MIN", 60)
    tick_s = max(15 * 60, tick_min * 60)
    hour = _env_int("SAMPLE_REMINDER_HOUR", 7)
    if not 0 <= hour <= 23:
        logger.error("[dispatch-reminder] SAMPLE_REMINDER_HOUR=%r out of range 0-23 — "
                     "falling back to default 7", hour)
        hour = 7
    enabled = _is_truthy(os.environ.get("SAMPLE_REMINDER_ENABLED", "1"))
    logger.info("Dispatch reminder loop started (enabled=%s, tick=%ds, from %02d:00 IST)",
                enabled, tick_s, hour)
    try:
        while True:
            try:
                if enabled and datetime.now(IST).hour >= hour:
                    async with pool.acquire() as conn:
                        counts = await scan_and_send(conn, today=ist_today())
                    if any(counts.values()):
                        logger.info("Dispatch reminder: sent %s", counts)
            except Exception:                         # noqa: BLE001 — a bad tick must never kill the loop
                logger.exception("Dispatch reminder loop tick failed")
            # Outside the inner try so a cancel here is never swallowed as "a bad tick".
            await asyncio.sleep(tick_s)
    except asyncio.CancelledError:
        logger.info("Dispatch reminder loop stopped")
        raise
