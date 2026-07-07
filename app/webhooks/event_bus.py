"""In-process event bus with fan-out to multiple subscribers.

Supports deferred publishing via `deferred_events()` context manager so that
events emitted inside a database transaction are only dispatched after the
transaction commits successfully.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import uuid4

logger = logging.getLogger(__name__)

# When set to a list, publish() buffers events instead of fanning out.
_deferred_buffer: ContextVar[list | None] = ContextVar("_deferred_buffer", default=None)


@dataclass
class Event:
    event_type: str
    entity: str
    payload: dict
    actor: str = "system"
    target_roles: list[str] = field(default_factory=list)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class EventBus:
    """Async fan-out event bus. Each subscriber gets its own queue."""

    def __init__(self):
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        buf = _deferred_buffer.get()
        if buf is not None:
            buf.append(event)
            return
        await self._fan_out(event)

    async def _fan_out(self, event: Event) -> None:
        async with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # HI-03: elevate to error — a full subscriber queue means
                    # the dispatcher (or broadcaster) is backlogged and events
                    # are being silently dropped. Ops needs visibility.
                    logger.error(
                        "Subscriber queue full, DROPPING event %s (type=%s entity=%s). "
                        "Dispatcher/broadcaster is backlogged.",
                        event.event_id, event.event_type, event.entity,
                    )

    async def subscribe(self) -> "Subscription":
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers.append(q)
        return Subscription(self, q)

    async def _unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        async with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


class Subscription:
    """Async iterator over events for one subscriber."""

    def __init__(self, bus: EventBus, queue: asyncio.Queue[Event]):
        self._bus = bus
        self._queue = queue

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()

    async def get(self) -> Event:
        return await self._queue.get()

    async def close(self) -> None:
        await self._bus._unsubscribe(self._queue)


# Singleton — imported by services, dispatcher, and broadcaster
event_bus = EventBus()


@asynccontextmanager
async def deferred_events():
    """Buffer events during a transaction, flush only on successful exit.

    Usage in router endpoints:
        async with pool.acquire() as conn:
            async with conn.transaction():
                async with deferred_events():
                    result = await some_service(conn, ...)
        # Events are published here, after transaction committed.

    If the block raises (transaction rollback), buffered events are discarded.
    """
    buf: list[Event] = []
    token = _deferred_buffer.set(buf)
    try:
        yield buf
    except BaseException:
        # Transaction rolled back — discard all buffered events
        _deferred_buffer.reset(token)
        raise
    else:
        # Transaction committed — flush buffered events
        _deferred_buffer.reset(token)
        for event in buf:
            await event_bus._fan_out(event)
