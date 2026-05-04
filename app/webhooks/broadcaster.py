"""WebSocket broadcaster — pushes role-scoped events to connected clients."""

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from starlette.websockets import WebSocket, WebSocketState

from .event_bus import Event, event_bus

logger = logging.getLogger(__name__)

# Role → event type prefixes this role receives
ROLE_EVENT_MAP: dict[str, list[str]] = {
    "planner": ["plan.", "mrp.", "fulfillment."],
    "store_manager": ["indent.", "material.", "store_alert."],
    "floor_supervisor": ["job_card.", "qc.", "dayend."],
    "purchase": ["indent."],
    "admin": ["*"],
}


class _JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


class ConnectionManager:
    """Tracks WebSocket connections grouped by (user_id, role, entity)."""

    def __init__(self):
        self._connections: dict[int, dict] = {}  # ws_id → {ws, user_id, role, entity}
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, user_id: int, role: str, entity: str) -> int:
        async with self._lock:
            ws_id = self._next_id
            self._next_id += 1
            self._connections[ws_id] = {
                "ws": ws, "user_id": user_id, "role": role, "entity": entity,
            }
        logger.info("WS connected: user=%d role=%s entity=%s (id=%d)", user_id, role, entity, ws_id)
        return ws_id

    async def disconnect(self, ws_id: int) -> None:
        async with self._lock:
            self._connections.pop(ws_id, None)
        logger.info("WS disconnected: id=%d", ws_id)

    def _should_receive(self, role: str, event: Event) -> bool:
        prefixes = ROLE_EVENT_MAP.get(role, [])
        if "*" in prefixes:
            return True
        # Also check target_roles on the event
        if event.target_roles and role not in event.target_roles:
            return False
        return any(event.event_type.startswith(p) for p in prefixes)

    async def broadcast(self, event: Event) -> None:
        msg = json.dumps({
            "event_id": event.event_id,
            "event_type": event.event_type,
            "timestamp": event.timestamp,
            "actor": event.actor,
            "data": event.payload,
        }, cls=_JSONEncoder)

        dead = []
        async with self._lock:
            targets = list(self._connections.items())

        for ws_id, info in targets:
            # "*" in a role's event prefixes also waives entity scoping (admin sees all entities).
            role_prefixes = ROLE_EVENT_MAP.get(info["role"], [])
            if "*" not in role_prefixes and info["entity"] != event.entity:
                continue
            if not self._should_receive(info["role"], event):
                continue
            try:
                ws: WebSocket = info["ws"]
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(msg)
            except Exception:
                dead.append(ws_id)

        for ws_id in dead:
            await self.disconnect(ws_id)


# Singleton
manager = ConnectionManager()


async def broadcaster_loop() -> None:
    """Main loop — subscribe to event bus, broadcast to WebSocket clients."""
    sub = await event_bus.subscribe()
    logger.info("WebSocket broadcaster started")
    try:
        while True:
            event = await sub.get()
            try:
                await manager.broadcast(event)
            except Exception:
                logger.exception("Broadcaster error for event %s", event.event_id)
    except asyncio.CancelledError:
        await sub.close()
        logger.info("WebSocket broadcaster stopped")
        raise
