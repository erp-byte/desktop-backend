"""Shared helper functions used across modules."""

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

from asyncpg.exceptions import UniqueViolationError


# Defaults for the time-ID savepoint-retry pattern. Inserts colliding on a
# time-based PK retry with a fresh ID after a small sleep so the epoch-ms
# counter advances. Tunables exported so callers can override per site.
MAX_PK_RETRIES = 3
PK_RETRY_DELAY_S = 0.002


def new_short_time_id() -> int:
    """8-digit time-based short ID (last 8 digits of epoch-ms).

    Used as the application-supplied PK for tables where we want a stable,
    short, non-sequential identifier (e.g. so_line.so_line_id,
    so_fulfillment_v2.so_fulfillment_id). Equivalent to:
        int(str(int(time.time() * 1000))[-8:])

    Collisions are possible if two inserts land in the same millisecond —
    callers should use insert_with_pk_retry() to handle them gracefully.
    """
    return int(str(int(time.time() * 1000))[-8:])


T = TypeVar("T")


async def insert_with_pk_retry(
    conn,
    insert_callable: Callable[[], Awaitable[T]],
    *,
    max_retries: int = MAX_PK_RETRIES,
    delay_s: float = PK_RETRY_DELAY_S,
) -> T:
    """Run an async INSERT inside a SAVEPOINT, retrying on PK collisions.

    `insert_callable` is a no-arg async callable that performs the INSERT and
    returns whatever asyncpg returns (e.g. the row from fetchrow, or the
    status string from execute). It MUST generate a fresh time-based ID on
    each call so retries can succeed:

        async def _insert():
            return await conn.fetchrow(
                "INSERT INTO foo (id, ...) VALUES ($1, ...) RETURNING id",
                new_short_time_id(), ...
            )
        row = await insert_with_pk_retry(conn, _insert)

    Behaviour:
      - On UniqueViolationError where constraint name contains '_pkey':
        sleep `delay_s` and retry, up to `max_retries` total attempts.
      - On any other UniqueViolationError (e.g. (so_line_id, fy) collision
        not handled by ON CONFLICT): re-raise immediately — non-PK conflicts
        are NOT silently swallowed.
      - After exhausting retries on a PK collision: re-raise the final
        exception so the caller decides what to do.

    Must be called inside an outer transaction; `conn.transaction()` here
    creates a SAVEPOINT so rollback affects only this row.
    """
    # Defensive precondition: without an outer txn, conn.transaction() starts
    # a real transaction and a PK-retry would commit partial state.
    if not conn.is_in_transaction():
        raise RuntimeError(
            "insert_with_pk_retry must be called inside an outer transaction "
            "(use `async with conn.transaction(): ...` at the caller level)"
        )
    for attempt in range(max_retries):
        try:
            async with conn.transaction():  # savepoint
                return await insert_callable()
        except UniqueViolationError as exc:
            constraint = getattr(exc, "constraint_name", "") or ""
            if "pkey" not in constraint:
                raise  # B3: non-PK violations propagate
            if attempt < max_retries - 1:
                await asyncio.sleep(delay_s)
                continue
            raise  # PK collision after all retries


def safe_float(val) -> float | None:
    """Parse value to float rounded to 3dp, return None on failure."""
    if val is None:
        return None
    try:
        return round(float(val), 3)
    except (ValueError, TypeError):
        return None


def safe_float_zero(val) -> float:
    """Parse value to float rounded to 3dp, return 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return round(float(val), 3)
    except (ValueError, TypeError):
        return 0.0


def safe_str(val) -> str | None:
    """Strip string, return None if empty."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None
