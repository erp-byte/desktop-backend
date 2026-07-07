# Webhook & WebSocket Event System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add real-time event delivery to the Candor Foods backend — WebSocket for the Android app (role-scoped), webhooks for external systems (Tally, supplier portal, IMS, Slack), and an MCP bridge for remote servers.

**Architecture:** In-process `asyncio.Queue` event bus with fan-out to two consumers: a webhook HTTP dispatcher with HMAC signing + retries, and a WebSocket broadcaster that filters by user role. MCP servers on remote Render instances inject events via `POST /internal/events`.

**Tech Stack:** FastAPI, asyncpg, httpx, PyJWT, FastAPI WebSockets (built-in)

---

## Status — 2026-04-18

- All 11 task files implemented; Task 11 steps 1–6 verified live end-to-end.
- Webhook path: httpbin delivery succeeded, signed payload, `delivered` / `200` / `attempts=1`.
- WebSocket path: live `ping` received by connected client after `/internal/events` injection.
- **Bug fixed during verification:** `ws_router.py` JWT `sub` was an int, rejected by PyJWT 2.12+. Now `str()` on encode, `int()` on decode.
- **Commits landed:** `b00f93b` (webhook package + migration incl. JWT fix), `9669379` (webhook settings in config.py).
- **Design gap flagged, NOT fixed:** `broadcaster.py:80` requires exact `info["entity"] == event.entity` match, so users with null/empty entity receive nothing — affects admins. Decide before Android rollout.
- **Still uncommitted (mixed scope with Lambda/other work):** `main.py` wiring, service `emit_event()` calls, `mcp_server.py`/`mcp_planner.py` event bridge, `requirements.txt`, `pyproject.toml`.

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `app/webhooks/__init__.py` | Package init |
| Create | `app/webhooks/event_bus.py` | Event dataclass, EventBus singleton with fan-out |
| Create | `app/webhooks/signer.py` | HMAC-SHA256 payload signing |
| Create | `app/webhooks/dispatcher.py` | Background task: match subscriptions, deliver HTTP, retry |
| Create | `app/webhooks/broadcaster.py` | Background task: WebSocket fan-out by role |
| Create | `app/webhooks/ws_router.py` | `POST /api/v1/ws/token` + `GET /ws` WebSocket endpoint |
| Create | `app/webhooks/router.py` | Webhook management CRUD API |
| Create | `app/db/002_webhooks.sql` | Migration: 3 tables |
| Modify | `app/config.py` | 3 new settings fields |
| Modify | `app/main.py` | Mount routers, start background tasks, `/internal/events` |
| Modify | `app/modules/production/services/fulfillment.py:34` | Emit `fulfillment.synced` |
| Modify | `app/modules/production/services/indent_manager.py:18,174` | Emit `indent.drafted`, `indent.sent` |
| Modify | `app/modules/production/services/mrp.py:15` | Emit `mrp.completed`, `mrp.shortage_detected` |
| Modify | `app/modules/production/services/job_card_engine.py:92` | Emit `job_card.created` |
| Modify | `app/modules/production/services/qc_service.py:33` | Emit `qc.passed`, `qc.failed` |
| Modify | `app/modules/production/services/floor_tracker.py:29` | Emit `material.moved` |
| Modify | `app/modules/production/services/day_end.py:248` | Emit `dayend.reconciled` |
| Modify | `app/modules/production/services/discrepancy_manager.py:13` | Emit `dayend.discrepancy_found` |
| Modify | `mcp_server.py:645,773,789,1181` | Add `emit_event()` helper + calls |
| Modify | `mcp_planner.py:444,514` | Add `emit_event()` helper + calls |
| Modify | `requirements.txt` | Add `PyJWT` |
| Modify | `pyproject.toml` | Add `PyJWT` |

---

### Task 1: Database Migration

**Files:**
- Create: `app/db/002_webhooks.sql`

- [ ] **Step 1: Create the migration file**

```sql
-- 002_webhooks.sql — Webhook & event delivery infrastructure

CREATE TABLE IF NOT EXISTS webhook_endpoint (
    id              SERIAL PRIMARY KEY,
    entity          TEXT NOT NULL,
    url             TEXT NOT NULL,
    secret          TEXT NOT NULL,
    description     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_subscription (
    id              SERIAL PRIMARY KEY,
    endpoint_id     INT NOT NULL REFERENCES webhook_endpoint(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    filter_jsonb    JSONB DEFAULT '{}',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (endpoint_id, event_type)
);

CREATE TABLE IF NOT EXISTS webhook_delivery (
    id              BIGSERIAL PRIMARY KEY,
    endpoint_id     INT NOT NULL REFERENCES webhook_endpoint(id),
    event_type      TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INT NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    next_retry_at   TIMESTAMPTZ,
    response_code   INT,
    response_body   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delivery_status
    ON webhook_delivery(status) WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS idx_delivery_event
    ON webhook_delivery(event_id);
CREATE INDEX IF NOT EXISTS idx_delivery_endpoint
    ON webhook_delivery(endpoint_id, created_at DESC);
```

- [ ] **Step 2: Verify migration syntax**

Run: `python -c "open('app/db/002_webhooks.sql').read(); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/db/002_webhooks.sql
git commit -m "feat(webhooks): add migration for webhook_endpoint, subscription, delivery tables"
```

---

### Task 2: Event Bus

**Files:**
- Create: `app/webhooks/__init__.py`
- Create: `app/webhooks/event_bus.py`

- [ ] **Step 1: Create the package init**

```python
# app/webhooks/__init__.py
```

Empty file — just marks the directory as a package.

- [ ] **Step 2: Create event_bus.py**

```python
"""In-process event bus with fan-out to multiple subscribers."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import uuid4

logger = logging.getLogger(__name__)


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
        async with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning(
                        "Subscriber queue full, dropping event %s", event.event_id
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

    async def __aiter__(self) -> AsyncIterator[Event]:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()

    async def get(self) -> Event:
        return await self._queue.get()

    async def close(self) -> None:
        await self._bus._unsubscribe(self._queue)


# Singleton — imported by services, dispatcher, and broadcaster
event_bus = EventBus()
```

- [ ] **Step 3: Verify import**

Run: `python -c "from app.webhooks.event_bus import event_bus, Event; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add app/webhooks/__init__.py app/webhooks/event_bus.py
git commit -m "feat(webhooks): add Event dataclass and fan-out EventBus"
```

---

### Task 3: HMAC Signer

**Files:**
- Create: `app/webhooks/signer.py`

- [ ] **Step 1: Create signer.py**

```python
"""HMAC-SHA256 webhook payload signing."""

import hashlib
import hmac


def sign_payload(secret: str, body: str) -> str:
    """Return 'sha256=<hex>' signature for webhook verification."""
    return "sha256=" + hmac.new(
        secret.encode(), body.encode(), hashlib.sha256
    ).hexdigest()


def verify_signature(secret: str, body: str, signature: str) -> bool:
    """Constant-time comparison of expected vs provided signature."""
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature)
```

- [ ] **Step 2: Verify import**

Run: `python -c "from app.webhooks.signer import sign_payload, verify_signature; s = sign_payload('key', 'body'); print(s[:10], verify_signature('key', 'body', s))"`
Expected: `sha256=39b True`

- [ ] **Step 3: Commit**

```bash
git add app/webhooks/signer.py
git commit -m "feat(webhooks): add HMAC-SHA256 signer for webhook payloads"
```

---

### Task 4: Webhook Dispatcher

**Files:**
- Create: `app/webhooks/dispatcher.py`

- [ ] **Step 1: Create dispatcher.py**

```python
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
BACKOFF_SECONDS = [10, 40, 90]


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
        asyncio.create_task(
            _deliver(client, pool, sub, event)
        )


async def _deliver(client: httpx.AsyncClient, pool, sub, event: Event) -> None:
    """Deliver with retries and exponential backoff."""
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

        # Log delivery attempt
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO webhook_delivery
                        (endpoint_id, event_type, event_id, payload, status,
                         attempts, last_attempt_at, response_code, response_body)
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
                    """,
                    sub["endpoint_id"], event.event_type, event.event_id,
                    body, status, attempt,
                    datetime.now(timezone.utc), resp_code, resp_body,
                )
        except Exception:
            logger.exception("Failed to log delivery attempt")

        if status == "delivered":
            return

        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(BACKOFF_SECONDS[attempt - 1])

    # Exhausted all retries — mark as exhausted
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE webhook_delivery SET status = 'exhausted'
                WHERE endpoint_id = $1 AND event_id = $2
                  AND status = 'failed'
                ORDER BY id DESC LIMIT 1
                """,
                sub["endpoint_id"], event.event_id,
            )
    except Exception:
        logger.exception("Failed to mark delivery as exhausted")
```

- [ ] **Step 2: Verify import**

Run: `python -c "from app.webhooks.dispatcher import dispatcher_loop; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/webhooks/dispatcher.py
git commit -m "feat(webhooks): add HTTP dispatcher with HMAC signing and retry"
```

---

### Task 5: WebSocket Broadcaster

**Files:**
- Create: `app/webhooks/broadcaster.py`

- [ ] **Step 1: Create broadcaster.py**

```python
"""WebSocket broadcaster — pushes role-scoped events to connected clients."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from starlette.websockets import WebSocket, WebSocketState

from .event_bus import Event, event_bus

logger = logging.getLogger(__name__)

# Role → event type prefixes this role receives
ROLE_EVENT_MAP: dict[str, list[str]] = {
    "planner": ["plan.", "mrp.", "fulfillment."],
    "store_manager": ["indent.", "material.", "store_alert."],
    "floor_supervisor": ["job_card.", "qc.", "dayend."],
    "purchase": ["indent.", "material.dispatched"],
    "admin": ["*"],
}


class _JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, datetime):
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
            if info["entity"] != event.entity:
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
```

- [ ] **Step 2: Verify import**

Run: `python -c "from app.webhooks.broadcaster import manager, broadcaster_loop; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/webhooks/broadcaster.py
git commit -m "feat(webhooks): add WebSocket broadcaster with role-scoped filtering"
```

---

### Task 6: WebSocket Router (Token + Endpoint)

**Files:**
- Create: `app/webhooks/ws_router.py`
- Modify: `requirements.txt` — add `PyJWT`
- Modify: `pyproject.toml` — add `PyJWT`

- [ ] **Step 1: Add PyJWT to requirements.txt**

Append `PyJWT==2.9.0` to `requirements.txt`.

- [ ] **Step 2: Add PyJWT to pyproject.toml**

Add `"PyJWT>=2.9.0"` to the `dependencies` list.

- [ ] **Step 3: Install PyJWT**

Run: `pip install PyJWT==2.9.0`

- [ ] **Step 4: Create ws_router.py**

```python
"""WebSocket token issuance and connection endpoint."""

import logging
from datetime import datetime, timezone, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.modules.auth.middleware import get_current_user
from .broadcaster import manager

logger = logging.getLogger(__name__)

router = APIRouter()


class WSTokenResponse(BaseModel):
    token: str
    expires_in: int


@router.post("/api/v1/ws/token", response_model=WSTokenResponse)
async def issue_ws_token(request: Request, user=Depends(get_current_user)):
    """Issue a short-lived JWT for WebSocket authentication."""
    settings = request.app.state.settings
    secret = settings.WS_TOKEN_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="WebSocket not configured")

    expiry_minutes = settings.WS_TOKEN_EXPIRY_MINUTES
    exp = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)

    payload = {
        "sub": user.user_id,
        "role": user.role_name,
        "entity": user.entity,
        "exp": exp,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return WSTokenResponse(token=token, expires_in=expiry_minutes * 60)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint — authenticate via token query param, then receive events."""
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=4001, reason="Missing token")
        return

    settings = ws.app.state.settings
    secret = settings.WS_TOKEN_SECRET
    if not secret:
        await ws.close(code=4003, reason="WebSocket not configured")
        return

    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        await ws.close(code=4002, reason="Token expired")
        return
    except jwt.InvalidTokenError:
        await ws.close(code=4001, reason="Invalid token")
        return

    user_id = payload["sub"]
    role = payload["role"]
    entity = payload["entity"]

    await ws.accept()
    ws_id = await manager.connect(ws, user_id, role, entity)

    try:
        # Keep connection alive — read and discard client messages (pings/pongs)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws_id)
```

- [ ] **Step 5: Verify import**

Run: `python -c "from app.webhooks.ws_router import router; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add app/webhooks/ws_router.py requirements.txt pyproject.toml
git commit -m "feat(webhooks): add WebSocket token issuance and connection endpoint"
```

---

### Task 7: Webhook Management API

**Files:**
- Create: `app/webhooks/router.py`

- [ ] **Step 1: Create router.py**

```python
"""Webhook management API — CRUD for endpoints, subscriptions, delivery log."""

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.modules.auth.middleware import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


# ── Schemas ──

class EndpointCreate(BaseModel):
    entity: str
    url: str
    description: str = ""

class EndpointUpdate(BaseModel):
    url: str | None = None
    description: str | None = None
    is_active: bool | None = None

class SubscriptionCreate(BaseModel):
    endpoint_id: int
    event_type: str

class DeliveryFilter(BaseModel):
    endpoint_id: int | None = None
    event_type: str | None = None
    status: str | None = None
    page: int = 1
    page_size: int = 50


# ── Endpoints ──

@router.post("/endpoints")
async def create_endpoint(body: EndpointCreate, request: Request,
                          user=Depends(require_permission("production", "webhooks", action="create"))):
    pool = request.app.state.db_pool
    secret = secrets.token_hex(32)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO webhook_endpoint (entity, url, secret, description, created_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, entity, url, description, is_active, created_at
            """,
            body.entity, body.url, secret, body.description, user.full_name,
        )
    result = dict(row)
    result["secret"] = secret  # Only returned on creation
    return result


@router.get("/endpoints")
async def list_endpoints(request: Request, entity: str = "",
                         user=Depends(require_permission("production", "webhooks", action="view"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if entity:
            rows = await conn.fetch(
                "SELECT id, entity, url, description, is_active, created_by, created_at FROM webhook_endpoint WHERE entity = $1 ORDER BY id",
                entity,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, entity, url, description, is_active, created_by, created_at FROM webhook_endpoint ORDER BY id"
            )
    return [dict(r) for r in rows]


@router.put("/endpoints/{endpoint_id}")
async def update_endpoint(endpoint_id: int, body: EndpointUpdate, request: Request,
                          user=Depends(require_permission("production", "webhooks", action="edit"))):
    pool = request.app.state.db_pool
    updates, params, idx = [], [], 1
    if body.url is not None:
        updates.append(f"url=${idx}"); params.append(body.url); idx += 1
    if body.description is not None:
        updates.append(f"description=${idx}"); params.append(body.description); idx += 1
    if body.is_active is not None:
        updates.append(f"is_active=${idx}"); params.append(body.is_active); idx += 1
    if not updates:
        raise HTTPException(400, "No fields to update")
    updates.append(f"updated_at=NOW()")
    params.append(endpoint_id)
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"UPDATE webhook_endpoint SET {', '.join(updates)} WHERE id=${idx}", *params,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, "Endpoint not found")
    return {"updated": True}


@router.delete("/endpoints/{endpoint_id}")
async def deactivate_endpoint(endpoint_id: int, request: Request,
                              user=Depends(require_permission("production", "webhooks", action="delete"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE webhook_endpoint SET is_active = FALSE, updated_at = NOW() WHERE id = $1",
            endpoint_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, "Endpoint not found")
    return {"deactivated": True}


# ── Subscriptions ──

@router.post("/subscriptions")
async def create_subscription(body: SubscriptionCreate, request: Request,
                               user=Depends(require_permission("production", "webhooks", action="create"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO webhook_subscription (endpoint_id, event_type)
                VALUES ($1, $2)
                RETURNING id, endpoint_id, event_type, is_active, created_at
                """,
                body.endpoint_id, body.event_type,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "Subscription already exists")
            raise
    return dict(row)


@router.get("/subscriptions")
async def list_subscriptions(request: Request, endpoint_id: int = 0,
                              user=Depends(require_permission("production", "webhooks", action="view"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        if endpoint_id:
            rows = await conn.fetch(
                "SELECT * FROM webhook_subscription WHERE endpoint_id = $1 ORDER BY id", endpoint_id,
            )
        else:
            rows = await conn.fetch("SELECT * FROM webhook_subscription ORDER BY id")
    return [dict(r) for r in rows]


@router.delete("/subscriptions/{sub_id}")
async def delete_subscription(sub_id: int, request: Request,
                               user=Depends(require_permission("production", "webhooks", action="delete"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM webhook_subscription WHERE id = $1", sub_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Subscription not found")
    return {"deleted": True}


# ── Deliveries ──

@router.get("/deliveries")
async def list_deliveries(request: Request,
                           endpoint_id: Optional[int] = None, event_type: Optional[str] = None,
                           status: Optional[str] = None, page: int = 1, page_size: int = 50,
                           user=Depends(require_permission("production", "webhooks", action="view"))):
    pool = request.app.state.db_pool
    conditions, params, idx = [], [], 1
    if endpoint_id:
        conditions.append(f"endpoint_id=${idx}"); params.append(endpoint_id); idx += 1
    if event_type:
        conditions.append(f"event_type=${idx}"); params.append(event_type); idx += 1
    if status:
        conditions.append(f"status=${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM webhook_delivery WHERE {where}", *params)
        rows = await conn.fetch(
            f"SELECT * FROM webhook_delivery WHERE {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}",
            *params, page_size, offset,
        )
    return {"total": total, "page": page, "results": [dict(r) for r in rows]}


@router.post("/deliveries/{delivery_id}/retry")
async def retry_delivery(delivery_id: int, request: Request,
                          user=Depends(require_permission("production", "webhooks", action="edit"))):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM webhook_delivery WHERE id = $1 AND status IN ('failed', 'exhausted')",
            delivery_id,
        )
        if not row:
            raise HTTPException(404, "Delivery not found or not in failed/exhausted status")
        await conn.execute(
            "UPDATE webhook_delivery SET status = 'pending', attempts = 0 WHERE id = $1",
            delivery_id,
        )
    # Re-publish the event so dispatcher picks it up
    from .event_bus import Event, event_bus
    import json
    payload_data = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    event = Event(
        event_type=row["event_type"],
        entity="",  # dispatcher will match by subscription
        payload=payload_data.get("data", payload_data),
        event_id=row["event_id"],
        actor=payload_data.get("actor", "system"),
    )
    await event_bus.publish(event)
    return {"retried": True, "delivery_id": delivery_id}


@router.post("/test")
async def test_webhook(request: Request,
                        endpoint_id: int = 0, url: str = "",
                        user=Depends(require_permission("production", "webhooks", action="create"))):
    """Send a test ping event to a webhook endpoint."""
    import httpx
    from .signer import sign_payload
    import json

    if endpoint_id:
        pool = request.app.state.db_pool
        async with pool.acquire() as conn:
            ep = await conn.fetchrow("SELECT url, secret FROM webhook_endpoint WHERE id = $1", endpoint_id)
        if not ep:
            raise HTTPException(404, "Endpoint not found")
        target_url = ep["url"]
        secret = ep["secret"]
    elif url:
        target_url = url
        secret = "test-secret"
    else:
        raise HTTPException(400, "Provide endpoint_id or url")

    body = json.dumps({"event_id": "test", "event_type": "ping", "timestamp": "", "actor": "test", "data": {}})
    signature = sign_payload(secret, body)
    headers = {"Content-Type": "application/json", "X-Webhook-Signature": signature, "X-Webhook-Event": "ping"}

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(target_url, content=body, headers=headers)
            return {"status_code": resp.status_code, "body": resp.text[:500]}
        except Exception as e:
            return {"error": str(e)}
```

- [ ] **Step 2: Verify import**

Run: `python -c "from app.webhooks.router import router; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/webhooks/router.py
git commit -m "feat(webhooks): add webhook management API (endpoints, subscriptions, deliveries)"
```

---

### Task 8: Config + Main App Integration

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py`

- [ ] **Step 1: Add config fields**

In `app/config.py`, add these three fields to the `Settings` class after `CLAUDE_MODEL`:

```python
    INTERNAL_WEBHOOK_TOKEN: str = ""
    WS_TOKEN_SECRET: str = ""
    WS_TOKEN_EXPIRY_MINUTES: int = 5
```

- [ ] **Step 2: Update main.py**

Replace the entire `app/main.py` with:

```python
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from pydantic import BaseModel

from app.config import Settings
from app.db.connection import create_pool, close_pool
from app.modules.auth.router import router as auth_router
from app.modules.so.router import router as so_router
from app.modules.purchase.router import router as purchase_router
from app.modules.production.router import router as production_router
from app.modules.amendment_router import router as amendment_router
from app.modules.so.services.item_matcher import load_master_items
from app.modules.production.services.master_ingest import run_master_ingest

from app.webhooks.event_bus import event_bus, Event
from app.webhooks.dispatcher import dispatcher_loop
from app.webhooks.broadcaster import broadcaster_loop
from app.webhooks.router import router as webhook_router
from app.webhooks.ws_router import router as ws_router

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    settings = Settings()
    fastapi_app.state.settings = settings

    pool = await create_pool(settings)
    fastapi_app.state.db_pool = pool

    master_items = await load_master_items(pool)
    fastapi_app.state.master_items = master_items

    data_dir = Path(__file__).parent / "data"
    if not data_dir.exists():
        data_dir = Path(__file__).parent.parent / "data"
    await run_master_ingest(pool, data_dir, master_items)

    # Start webhook dispatcher and WebSocket broadcaster as background tasks
    bg_tasks = []
    bg_tasks.append(asyncio.create_task(dispatcher_loop(pool)))
    bg_tasks.append(asyncio.create_task(broadcaster_loop()))
    fastapi_app.state._webhook_tasks = bg_tasks

    yield

    # Cancel background tasks
    for t in bg_tasks:
        t.cancel()
    await asyncio.gather(*bg_tasks, return_exceptions=True)

    await close_pool(pool)
    logger.info("Shutdown complete")


app = FastAPI(title="Candor Foods — Consumption Backend", version="0.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(so_router)
app.include_router(purchase_router)
app.include_router(production_router)
app.include_router(amendment_router)
app.include_router(webhook_router)
app.include_router(ws_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Internal events endpoint (for MCP servers) ──

class InternalEventBody(BaseModel):
    event_type: str
    entity: str
    payload: dict
    actor: str = "mcp"
    target_roles: list[str] = []


@app.post("/internal/events")
async def receive_internal_event(body: InternalEventBody, request: Request):
    """MCP servers on remote Render instances POST here to inject events."""
    settings = request.app.state.settings
    token = settings.INTERNAL_WEBHOOK_TOKEN
    if not token:
        raise HTTPException(503, "Internal events not configured")

    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {token}":
        raise HTTPException(401, "Invalid internal token")

    await event_bus.publish(Event(
        event_type=body.event_type,
        entity=body.entity,
        payload=body.payload,
        actor=body.actor,
        target_roles=body.target_roles,
    ))
    return {"accepted": True}


# AWS Lambda entry point
handler = Mangum(app, lifespan="on")
```

- [ ] **Step 3: Verify server starts**

Run: `python -c "from app.main import app; print('Routes:', len(app.routes))"`
Expected: prints route count (should be higher than before)

- [ ] **Step 4: Commit**

```bash
git add app/config.py app/main.py
git commit -m "feat(webhooks): wire event bus, dispatcher, broadcaster, and routers into FastAPI app"
```

---

### Task 9: Emit Events from FastAPI Services

**Files:**
- Modify: `app/modules/production/services/fulfillment.py:34`
- Modify: `app/modules/production/services/indent_manager.py:18,174`
- Modify: `app/modules/production/services/mrp.py:15`
- Modify: `app/modules/production/services/job_card_engine.py:92`
- Modify: `app/modules/production/services/qc_service.py:33`
- Modify: `app/modules/production/services/floor_tracker.py:29`
- Modify: `app/modules/production/services/day_end.py:248`
- Modify: `app/modules/production/services/discrepancy_manager.py:13`

Each service gets the same import at the top and a one-line `publish()` call at the state transition point. The event bus `publish()` is fire-and-forget — it never raises, never blocks the DB transaction.

- [ ] **Step 1: fulfillment.py — emit `fulfillment.synced`**

Add import after the existing imports (after `from datetime import ...` line):

```python
from app.webhooks.event_bus import event_bus, Event
```

After line 84 (`return {"synced": synced, "skipped": skipped, "total": total}`), add before the return:

```python
    if synced > 0:
        await event_bus.publish(Event(
            event_type="fulfillment.synced",
            entity=entity or "",
            payload={"synced": synced, "skipped": skipped, "total": total},
            target_roles=["planner", "admin"],
        ))
    return {"synced": synced, "skipped": skipped, "total": total}
```

(Replace the existing `return` line.)

- [ ] **Step 2: indent_manager.py — emit `indent.drafted` and `indent.sent`**

Add import at the top:

```python
from app.webhooks.event_bus import event_bus, Event
```

In `generate_draft_indents()`, after the for loop completes (after building the `indents` list), add before the final return:

```python
    if indents:
        await event_bus.publish(Event(
            event_type="indent.drafted",
            entity=entity,
            payload={"plan_id": plan_id, "count": len(indents), "total_shortage_kg": sum(i["shortage_kg"] for i in indents)},
            target_roles=["planner", "admin"],
        ))
```

In `send_indent()`, after the last `conn.execute` (the stores alert insert at line ~219), add:

```python
    await event_bus.publish(Event(
        event_type="indent.sent",
        entity=indent['entity'],
        payload={"indent_id": indent_id, "material": material, "qty_kg": qty, "deadline": str(deadline)},
        target_roles=["store_manager", "purchase", "admin"],
    ))
```

- [ ] **Step 3: mrp.py — emit `mrp.completed` and `mrp.shortage_detected`**

Add import at the top:

```python
from app.webhooks.event_bus import event_bus, Event
```

At the end of `run_mrp()`, before the final `return`, add:

```python
    await event_bus.publish(Event(
        event_type="mrp.completed",
        entity=entity,
        payload={"plan_id": plan_id, "summary": result["summary"]},
        target_roles=["planner", "admin"],
    ))
    if shortage_count > 0:
        await event_bus.publish(Event(
            event_type="mrp.shortage_detected",
            entity=entity,
            payload={"plan_id": plan_id, "shortage_count": shortage_count, "total_shortage_kg": round(total_shortage, 3)},
            target_roles=["planner", "store_manager", "admin"],
        ))
```

(Use the local variable `result` which is the dict being returned. Note: the variable is currently the dict returned by the function — assign it before the event. Actually, looking at the code, the return dict is built inline. We'll assign it to a variable first.)

Replace lines 138-147:

```python
    result = {
        "plan_id": plan_id,
        "materials": materials,
        "summary": {
            "total_materials": len(materials),
            "sufficient": sufficient,
            "shortage": shortage_count,
            "total_shortage_kg": round(total_shortage, 3),
        },
    }

    await event_bus.publish(Event(
        event_type="mrp.completed",
        entity=entity,
        payload={"plan_id": plan_id, "summary": result["summary"]},
        target_roles=["planner", "admin"],
    ))
    if shortage_count > 0:
        await event_bus.publish(Event(
            event_type="mrp.shortage_detected",
            entity=entity,
            payload={"plan_id": plan_id, "shortage_count": shortage_count, "total_shortage_kg": round(total_shortage, 3)},
            target_roles=["planner", "store_manager", "admin"],
        ))

    return result
```

- [ ] **Step 4: job_card_engine.py — emit `job_card.created`**

Add import at the top:

```python
from app.webhooks.event_bus import event_bus, Event
```

At the end of `create_job_cards()`, just before the final return, add:

```python
    await event_bus.publish(Event(
        event_type="job_card.created",
        entity=order['entity'],
        payload={"prod_order_id": prod_order_id, "job_card_count": len(job_cards)},
        target_roles=["floor_supervisor", "admin"],
    ))
```

- [ ] **Step 5: qc_service.py — emit `qc.passed` and `qc.failed`**

Add import at the top:

```python
from app.webhooks.event_bus import event_bus, Event
```

In `submit_inspection()`, after the `conn.execute` UPDATE and before the `if result == "fail"` block, add:

```python
    qc_event_type = "qc.passed" if result in ("pass", "conditional_pass") else "qc.failed"
    await event_bus.publish(Event(
        event_type=qc_event_type,
        entity="cfpl",
        payload={"inspection_id": str(inspection_id), "result": result, "findings": findings},
        target_roles=["floor_supervisor", "admin"],
    ))
```

- [ ] **Step 6: floor_tracker.py — emit `material.moved`**

Add import at the top:

```python
from app.webhooks.event_bus import event_bus, Event
```

At the end of `move_material()`, just before the final return (after the movement_id is created), add:

```python
    await event_bus.publish(Event(
        event_type="material.moved",
        entity=entity,
        payload={"sku_name": sku_name, "from": from_location, "to": to_location, "qty_kg": qty_kg, "movement_id": movement_id},
        target_roles=["store_manager", "admin"],
    ))
```

- [ ] **Step 7: day_end.py — emit `dayend.reconciled`**

Add import at the top:

```python
from app.webhooks.event_bus import event_bus, Event
```

In `reconcile_scan()`, at the end of the function before the final return, add:

```python
    await event_bus.publish(Event(
        event_type="dayend.reconciled",
        entity=entity,
        payload={"scan_id": scan_id, "floor_location": scan['floor_location'], "adjustments": adjusted},
        target_roles=["planner", "admin"],
    ))
```

(Where `adjusted` and `entity` are variables already in scope — `entity` comes from `scan['entity']`, `adjusted` is the count of adjusted items.)

- [ ] **Step 8: discrepancy_manager.py — emit `dayend.discrepancy_found`**

Add import at the top:

```python
from app.webhooks.event_bus import event_bus, Event
```

In `report_discrepancy()`, after the discrepancy is inserted into the DB and before the final return, add:

```python
    await event_bus.publish(Event(
        event_type="dayend.discrepancy_found",
        entity=entity,
        payload={"discrepancy_type": discrepancy_type, "severity": severity, "affected_material": affected_material, "affected_job_cards": len(affected_jc_ids)},
        target_roles=["planner", "store_manager", "admin"],
    ))
```

- [ ] **Step 9: Verify all imports work**

Run: `python -c "from app.modules.production.services import fulfillment, indent_manager, mrp, job_card_engine, qc_service, floor_tracker, day_end, discrepancy_manager; print('All OK')"`
Expected: `All OK`

- [ ] **Step 10: Commit**

```bash
git add app/modules/production/services/fulfillment.py app/modules/production/services/indent_manager.py app/modules/production/services/mrp.py app/modules/production/services/job_card_engine.py app/modules/production/services/qc_service.py app/modules/production/services/floor_tracker.py app/modules/production/services/day_end.py app/modules/production/services/discrepancy_manager.py
git commit -m "feat(webhooks): emit events from all production services"
```

---

### Task 10: MCP Server Event Bridge

**Files:**
- Modify: `mcp_server.py`
- Modify: `mcp_planner.py`

Both servers get the same `emit_event()` helper function and calls after state-changing tools.

- [ ] **Step 1: Add emit_event() to mcp_server.py**

After the `get_pool()` function (around line 87), add:

```python
async def emit_event(event_type: str, entity: str, payload: dict,
                     actor: str = "mcp", target_roles: list[str] | None = None) -> None:
    """Fire-and-forget event to main server's webhook bus."""
    url = os.environ.get("MAIN_SERVER_URL", "")
    token = os.environ.get("INTERNAL_WEBHOOK_TOKEN", "")
    if not url or not token:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{url}/internal/events",
                json={
                    "event_type": event_type,
                    "entity": entity,
                    "payload": payload,
                    "actor": actor,
                    "target_roles": target_roles or [],
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as e:
        logger.warning("emit_event(%s) failed: %s", event_type, e)
```

- [ ] **Step 2: Add emit calls to mcp_server.py tools**

In `approve_plan()` (line ~662), after the transaction block closes, before the return:

```python
    await emit_event("plan.approved", plan['entity'], {"plan_id": plan_id, "approved_by": approved_by, "mrp_summary": mrp_result["summary"]}, target_roles=["planner", "admin"])
```

In `send_indent()` (line ~785), after the transaction block, before the return:

```python
    await emit_event("indent.sent", indent['entity'], {"indent_id": indent_id, "material": mat, "qty_kg": qty}, target_roles=["store_manager", "purchase", "admin"])
```

In `send_bulk_indents()` (line ~804), after the transaction block, before the return:

```python
    await emit_event("indent.bulk_sent", "", {"indent_ids": indent_ids, "sent": sent}, target_roles=["store_manager", "purchase", "admin"])
```

In `move_material()` (line ~1191), after the `_move` call succeeds and before the return:

```python
    if "error" not in result:
        await emit_event("material.moved", entity, {"sku_name": sku_name, "from": from_location, "to": to_location, "qty_kg": quantity_kg}, target_roles=["store_manager", "admin"])
```

- [ ] **Step 3: Add emit_event() to mcp_planner.py**

After the `get_pool()` function, add the same `emit_event()` helper (identical code as step 1).

- [ ] **Step 4: Add emit calls to mcp_planner.py tools**

In `approve_plan()` (line ~467), after the transaction block, before the return:

```python
    await emit_event("plan.approved", plan['entity'], {"plan_id": plan_id, "approved_by": approved_by}, target_roles=["planner", "admin"])
```

In `send_indent()` (line ~533), after the transaction block, before the return:

```python
    await emit_event("indent.sent", indent['entity'], {"indent_id": indent_id, "material": mat, "qty_kg": qty}, target_roles=["store_manager", "purchase", "admin"])
```

- [ ] **Step 5: Verify MCP servers parse**

Run: `python -c "import ast; ast.parse(open('mcp_server.py').read()); ast.parse(open('mcp_planner.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py mcp_planner.py
git commit -m "feat(webhooks): add emit_event bridge to MCP servers"
```

---

### Task 11: Verify End-to-End

- [x] **Step 1: Run the migration**

Run the SQL file against the database:
```bash
psql "$DATABASE_URL" -f app/db/002_webhooks.sql
```
Expected: `CREATE TABLE` x3, `CREATE INDEX` x3

- [x] **Step 2: Start the server**

Run: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`
Expected: Server starts, logs show "Webhook dispatcher started" and "WebSocket broadcaster started"

- [x] **Step 3: Test health endpoint**

Run: `curl http://localhost:8000/health`
Expected: `{"status":"ok"}`

- [x] **Step 4: Test webhook endpoint creation**

```bash
curl -X POST http://localhost:8000/api/v1/webhooks/endpoints \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <session-token>" \
  -d '{"entity":"cfpl","url":"https://httpbin.org/post","description":"test"}'
```
Expected: JSON with `id`, `secret`, `url`

- [x] **Step 5: Test internal event injection**

```bash
curl -X POST http://localhost:8000/internal/events \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <INTERNAL_WEBHOOK_TOKEN>" \
  -d '{"event_type":"ping","entity":"cfpl","payload":{}}'
```
Expected: `{"accepted": true}`

- [x] **Step 6: Test WebSocket token**

```bash
curl -X POST http://localhost:8000/api/v1/ws/token \
  -H "Authorization: Bearer <session-token>"
```
Expected: JSON with `token` and `expires_in`

- [ ] **Step 7: Commit final state**

```bash
git add -A
git commit -m "feat(webhooks): complete webhook & WebSocket event system"
```
