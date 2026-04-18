"""Webhook HTTP dispatcher — background task that delivers events to registered endpoints."""

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from .event_bus import Event, event_bus
from .signer import sign_payload

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_CONCURRENT_DELIVERIES = 20
BACKOFF_SECONDS = [10, 40, 90]

_delivery_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DELIVERIES)

# CR-02: cap per-event fan-out so a burst of events with many subscribers
# cannot spawn thousands of concurrent tasks competing for the DB pool.
_dispatch_concurrency = asyncio.Semaphore(8)

# HI-01: strong refs for in-flight delivery tasks. asyncio.create_task only
# holds weak refs, so without this tasks can be GC'd mid-flight. The set is
# also used by the lifespan shutdown path (if wired up) to drain cleanly.
_inflight_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Track the task so it can't be GC'd mid-flight (HI-01)."""
    t = asyncio.create_task(coro)
    _inflight_tasks.add(t)
    t.add_done_callback(_inflight_tasks.discard)
    return t


async def dispatcher_loop(pool) -> None:
    """Main loop — subscribe to event bus, match subscriptions, deliver."""
    sub = await event_bus.subscribe()
    logger.info("Webhook dispatcher started")

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            while True:
                event = await sub.get()
                try:
                    await _dispatch_event(client, pool, event)
                except Exception:
                    logger.exception("Dispatcher error for event %s", event.event_id)
        except asyncio.CancelledError:
            await sub.close()
            logger.info("Webhook dispatcher stopped")
            raise


async def _dispatch_event(client: httpx.AsyncClient, pool, event: Event) -> None:
    """Find matching subscriptions and deliver to each."""
    # CR-02: bound concurrent dispatch so a burst of events cannot flood the pool.
    async with _dispatch_concurrency:
        async with pool.acquire() as conn:
            subs = await conn.fetch(
                """
                SELECT e.id AS endpoint_id, e.url, e.secret, s.event_type
                FROM webhook_subscription s
                JOIN webhook_endpoint e ON e.id = s.endpoint_id
                WHERE s.event_type IN ($1, '*')
                  AND e.entity = $2
                  AND e.is_active = TRUE
                  AND s.is_active = TRUE
                """,
                event.event_type, event.entity,
            )

        for sub in subs:
            # HI-01: use _spawn so the task is strong-ref'd until completion.
            _spawn(_throttled_deliver(client, pool, sub, event))


async def _throttled_deliver(client, pool, sub, event: Event) -> None:
    async with _delivery_semaphore:
        await _deliver(client, pool, sub, event)


async def _deliver(client: httpx.AsyncClient, pool, sub, event: Event,
                   *, delivery_id: int | None = None) -> None:
    """Deliver with retries and exponential backoff.

    If delivery_id is provided (retry case), updates the existing row.
    Otherwise creates a new delivery row.
    """
    body = json.dumps({
        "event_id": event.event_id,
        "event_type": event.event_type,
        "timestamp": event.timestamp,
        "actor": event.actor,
        "data": event.payload,
    })
    signature = sign_payload(sub["secret"], body)
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
        "X-Webhook-Event": event.event_type,
        "X-Webhook-Id": event.event_id,
        "X-Webhook-Timestamp": event.timestamp,
    }

    # Create delivery row if not retrying an existing one
    if delivery_id is None:
        try:
            async with pool.acquire() as conn:
                delivery_id = await conn.fetchval(
                    """
                    INSERT INTO webhook_delivery
                        (endpoint_id, event_type, event_id, payload, target_roles,
                         status, attempts, last_attempt_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5, 'pending', 0, $6)
                    RETURNING id
                    """,
                    sub["endpoint_id"], event.event_type, event.event_id,
                    body, list(event.target_roles or []),
                    datetime.now(timezone.utc),
                )
        except Exception:
            logger.exception("Failed to create delivery record")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp_code = None
        resp_body = None
        status = "failed"

        try:
            resp = await client.post(sub["url"], content=body, headers=headers)
            resp_code = resp.status_code
            resp_body = resp.text[:500]
            if resp.status_code < 400:
                status = "delivered"
        except Exception as exc:
            resp_body = str(exc)[:500]

        # Update the delivery row
        if delivery_id:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE webhook_delivery
                        SET status = $2, attempts = $3, last_attempt_at = $4,
                            response_code = $5, response_body = $6
                        WHERE id = $1
                        """,
                        delivery_id, status, attempt,
                        datetime.now(timezone.utc), resp_code, resp_body,
                    )
            except Exception:
                logger.exception("Failed to update delivery record")

        if status == "delivered":
            return

        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(BACKOFF_SECONDS[attempt - 1])

    # Exhausted all retries
    if delivery_id:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE webhook_delivery SET status = 'exhausted' WHERE id = $1",
                    delivery_id,
                )
        except Exception:
            logger.exception("Failed to mark delivery as exhausted")


async def retry_single_delivery(pool, sub, event: Event, delivery_id: int) -> None:
    """Retry a specific failed delivery. Creates its own httpx client."""
    async with _delivery_semaphore:
        async with httpx.AsyncClient(timeout=10) as client:
            await _deliver(client, pool, sub, event, delivery_id=delivery_id)
