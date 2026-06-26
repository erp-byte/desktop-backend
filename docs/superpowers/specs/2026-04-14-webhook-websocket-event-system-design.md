# Webhook & WebSocket Event System — Design Spec

**Date:** 2026-04-14
**Status:** Approved
**Approach:** Unified Event Bus + Dual Delivery (in-process asyncio)

---

## Goal

Add a real-time event system to the Candor Foods Consumption Backend that:

1. **WebSocket** — pushes role-scoped events to the Android app in real-time
2. **Webhooks** — delivers signed HTTP callbacks to external systems (Tally, supplier portal, IMS backend, Slack/Teams)
3. **MCP Bridge** — allows MCP servers on separate Render instances to inject events into the same bus

## Constraints

- Single main FastAPI server (`:8000`)
- MCP servers (`mcp_server.py`, `mcp_planner.py`, `mcp_tracker.py`) run on separate free Render instances
- 100-150 users — no external broker needed
- No changes to existing behavior — events are fire-and-forget additions

---

## 1. Event Bus & Event Model

A singleton `EventBus` wrapping per-subscriber `asyncio.Queue` instances (fan-out pattern). Every event is a dataclass:

```python
@dataclass
class Event:
    event_type: str          # "plan.approved"
    entity: str              # "candor_foods"
    payload: dict            # domain-specific data
    event_id: str            # UUID — idempotency key
    timestamp: str           # ISO 8601
    actor: str               # who triggered it ("kaushal", "system", "mcp")
    target_roles: list[str]  # ["planner", "store_manager"] — for WebSocket filtering
```

Two methods:
- `publish(event)` — called by services and the `/internal/events` endpoint
- `subscribe()` — returns an async iterator; both webhook dispatcher and WebSocket broadcaster subscribe independently

Each subscriber gets its own `asyncio.Queue`, so consumers are independent and don't block each other.

---

## 2. WebSocket Layer

### Token Flow

1. Android client calls `POST /api/v1/ws/token` with its existing Bearer session token
2. Server validates the session, generates a short-lived JWT (5-minute expiry) containing `user_id`, `role_name`, `entity`
3. Client connects to `ws://host:8000/ws?token=<ws_token>`
4. Server validates JWT on connect, registers the connection under `(user_id, role_name, entity)`

### Role-Scoped Event Routing

| Role | Receives events matching |
|------|--------------------------|
| `planner` | `plan.*`, `mrp.*`, `fulfillment.*` |
| `store_manager` | `indent.*`, `material.*`, `store_alert.*` |
| `floor_supervisor` | `job_card.*`, `qc.*`, `dayend.*` |
| `purchase` | `indent.*`, `material.dispatched` |
| `admin` | `*` (everything) |

### Broadcaster

Background `asyncio.Task` that reads from its event bus subscription, checks each connected client's role against `target_roles` on the event, and sends matching events as JSON frames.

### Disconnection

Client auto-reconnects. No message buffering on server — if the client was disconnected, it catches up via REST endpoints on reconnect. Keeps the server stateless and simple.

---

## 3. Webhook Dispatcher (Server-to-Server)

### Database Tables

**`webhook_endpoint`** — registered external URL:
- `id` SERIAL PK
- `entity` TEXT NOT NULL
- `url` TEXT NOT NULL
- `secret` TEXT NOT NULL (HMAC signing key)
- `description` TEXT
- `is_active` BOOLEAN DEFAULT TRUE
- `created_by` TEXT NOT NULL
- `created_at` / `updated_at` TIMESTAMPTZ

**`webhook_subscription`** — links endpoint to event types:
- `id` SERIAL PK
- `endpoint_id` INT FK → webhook_endpoint
- `event_type` TEXT NOT NULL (e.g., `plan.approved`, `*`)
- `filter_jsonb` JSONB DEFAULT `{}` (optional payload filter)
- `is_active` BOOLEAN DEFAULT TRUE
- UNIQUE (endpoint_id, event_type)

**`webhook_delivery`** — log of every delivery attempt:
- `id` BIGSERIAL PK
- `endpoint_id` INT FK → webhook_endpoint
- `event_type` TEXT NOT NULL
- `event_id` UUID NOT NULL (idempotency key)
- `payload` JSONB NOT NULL
- `status` TEXT DEFAULT `pending` (pending, delivered, failed, exhausted)
- `attempts` INT DEFAULT 0
- `last_attempt_at` TIMESTAMPTZ
- `next_retry_at` TIMESTAMPTZ
- `response_code` INT
- `response_body` TEXT

Indexes:
- `idx_delivery_status` on `status` WHERE status IN ('pending', 'failed')
- `idx_delivery_event` on `event_id`

### Delivery Flow

1. Dispatcher background task reads from its event bus subscription
2. Queries `webhook_subscription` for matching `event_type` + `entity`
3. For each match, fires `httpx.AsyncClient.post()` with headers:
   - `X-Webhook-Signature: sha256=<hmac>` (HMAC-SHA256 of raw body with endpoint secret)
   - `X-Webhook-Event: <event_type>`
   - `X-Webhook-Id: <event_id>`
   - `X-Webhook-Timestamp: <iso_timestamp>`
4. Logs result to `webhook_delivery` table
5. On failure: 3 retries with exponential backoff (10s, 40s, 90s), then marks `exhausted`

### Payload Envelope

```json
{
  "event_id": "a1b2c3d4-...",
  "event_type": "indent.sent",
  "timestamp": "2026-04-13T10:30:00Z",
  "actor": "kaushal",
  "data": { "indent_id": 42, "material": "Sugar", "qty": 500.0 }
}
```

### Management API (`/api/v1/webhooks/`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/endpoints` | Register endpoint |
| GET | `/endpoints` | List endpoints |
| PUT | `/endpoints/{id}` | Update endpoint |
| DELETE | `/endpoints/{id}` | Deactivate endpoint |
| POST | `/subscriptions` | Subscribe to event type |
| DELETE | `/subscriptions/{id}` | Unsubscribe |
| GET | `/deliveries` | Filterable delivery log |
| POST | `/deliveries/{id}/retry` | Replay a failed delivery |
| POST | `/test` | Send ping event to verify endpoint |

---

## 4. MCP Server Bridge

### Main Server Endpoint

```
POST /internal/events
Header: Authorization: Bearer <INTERNAL_WEBHOOK_TOKEN>
Body: { event_type, entity, payload, actor, target_roles }
```

`INTERNAL_WEBHOOK_TOKEN` is a shared secret set as an env var on all Render instances. The endpoint validates the token (simple string comparison, no DB lookup), constructs an `Event`, and publishes to the bus.

### MCP-Side Helper

A small `emit_event()` async function added to `mcp_server.py` and `mcp_planner.py`:

```python
async def emit_event(event_type, entity, payload, actor="mcp", target_roles=None):
    url = os.environ.get("MAIN_SERVER_URL", "") + "/internal/events"
    token = os.environ.get("INTERNAL_WEBHOOK_TOKEN", "")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={...}, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        logger.warning(f"emit_event failed: {e}")
```

Fire-and-forget — MCP tool call succeeds regardless of event delivery. `mcp_tracker.py` gets no emit calls (read-only).

### MCP Environment Variables

- `MAIN_SERVER_URL` — base URL of the main FastAPI server
- `INTERNAL_WEBHOOK_TOKEN` — shared secret matching the main server

---

## 5. Event Taxonomy & Emission Points

### FastAPI Services (`app/modules/production/services/`)

| Service | Function | Event | target_roles |
|---------|----------|-------|--------------|
| `fulfillment.py` | `sync_fulfillment()` | `fulfillment.synced` | planner, admin |
| `fulfillment.py` | `revise_fulfillment()` | `fulfillment.revised` | planner, admin |
| `indent_manager.py` | `generate_draft_indents()` | `indent.drafted` | planner, admin |
| `indent_manager.py` | `send_indent()` | `indent.sent` | store_manager, purchase, admin |
| `mrp.py` | `run_mrp()` | `mrp.completed` | planner, admin |
| `mrp.py` | `run_mrp()` (shortage found) | `mrp.shortage_detected` | planner, store_manager, admin |
| `job_card_engine.py` | `create_job_cards()` | `job_card.created` | floor_supervisor, admin |
| `qc_service.py` | QC pass | `qc.passed` | floor_supervisor, admin |
| `qc_service.py` | QC fail | `qc.failed` | floor_supervisor, admin |
| `floor_tracker.py` | `move_material()` | `material.moved` | store_manager, admin |
| `day_end.py` | reconciliation | `dayend.reconciled` | planner, admin |
| `discrepancy_manager.py` | discrepancy found | `dayend.discrepancy_found` | planner, store_manager, admin |
| `store_controller.py` | alert created | `store_alert.created` | store_manager, admin |

### MCP Servers (via `emit_event()` HTTP bridge)

| Server | Tool | Event |
|--------|------|-------|
| `mcp_server.py` | `approve_plan()` | `plan.approved` |
| `mcp_server.py` | `send_indent()` | `indent.sent` |
| `mcp_server.py` | `send_bulk_indents()` | `indent.bulk_sent` |
| `mcp_server.py` | `move_material()` | `material.moved` |
| `mcp_planner.py` | `approve_plan()` | `plan.approved` |
| `mcp_planner.py` | `send_indent()` | `indent.sent` |

---

## 6. File Structure & Config

### New Files

```
app/
  webhooks/
    __init__.py
    event_bus.py        # EventBus singleton, Event dataclass
    dispatcher.py       # Webhook HTTP delivery + retry logic
    broadcaster.py      # WebSocket role-scoped push
    signer.py           # HMAC-SHA256 signing
    router.py           # Webhook management API (/api/v1/webhooks/)
    ws_router.py        # WebSocket endpoint + token issuance (/api/v1/ws/)

app/db/
  002_webhooks.sql      # Migration: 3 new tables
```

### Config Additions (`app/config.py`)

```python
INTERNAL_WEBHOOK_TOKEN: str = ""       # Shared secret for MCP bridge
WS_TOKEN_SECRET: str = ""             # JWT signing key for WebSocket tokens
WS_TOKEN_EXPIRY_MINUTES: int = 5      # Short-lived WS token TTL
```

### New Dependency

- `PyJWT` — for WebSocket token generation/validation

### Changes to Existing Files

- `app/main.py` — import webhook/ws routers, start dispatcher + broadcaster in lifespan, add `/internal/events` endpoint
- `app/config.py` — 3 new settings fields
- `mcp_server.py` — add `emit_event()` helper + calls after state-changing tools
- `mcp_planner.py` — same `emit_event()` helper + calls
- `requirements.txt` / `pyproject.toml` — add `PyJWT`, `websockets`
- 12 service files — one-line `publish()` calls at emission points

### Behavioral Impact

None. Events are fire-and-forget additions. If no webhooks are registered and no WebSocket clients are connected, events publish into the bus and get consumed with no side effects.

---

## Consumer Integration Notes

### Tally (Accounting)

Subscribe to: `indent.sent`, `material.dispatched`
Payload includes financial data (quantities, SKU codes) for ledger sync.

### Supplier Portal

Subscribe to: `indent.sent`, `indent.bulk_sent`
Payload includes material name, quantity, urgency for vendor notification.

### IMS Backend

Subscribe to: `material.moved`, `material.dispatched`, `fulfillment.synced`
Payload includes SKU, location, quantity for inventory sync.

### Slack/Teams (Monitoring)

Subscribe to: `mrp.shortage_detected`, `qc.failed`, `dayend.discrepancy_found`
Payload formatted for alert display.
