# Frontend Integration Guide — Webhook & WebSocket Event System

**Base URL:** `https://desktop-backend-vhf0.onrender.com` (or `http://localhost:8000` for local dev)
**Auth:** All REST endpoints require `Authorization: Bearer <session_token>` header (same token used for all existing API calls)

---

## Table of Contents

1. [WebSocket — Real-Time Events for Android App](#1-websocket--real-time-events-for-android-app)
2. [WebSocket Event Catalog (all 15 events)](#2-websocket-event-catalog)
3. [Role-Based Event Filtering](#3-role-based-event-filtering)
4. [Webhook Management API (Admin Dashboard)](#4-webhook-management-api)
5. [Error Codes & Handling](#5-error-codes--handling)

---

## 1. WebSocket — Real-Time Events for Android App

### Flow Overview

```
Android App                         Backend Server
    │                                      │
    ├── POST /api/v1/ws/token ──────────>  │  (1) Get short-lived WS token
    │  <── { token, expires_in } ──────────┤
    │                                      │
    ├── WS /ws?token=<jwt> ─────────────>  │  (2) Open WebSocket connection
    │  <── Connection accepted ────────────┤
    │                                      │
    │  <── { event_type, data } ───────────┤  (3) Receive real-time events
    │  <── { event_type, data } ───────────┤      (server pushes, client listens)
    │  <── { event_type, data } ───────────┤
    │                                      │
    ├── Connection dropped ─────────────>  │  (4) Auto-reconnect
    ├── POST /api/v1/ws/token ──────────>  │      (get new token, reconnect)
    └── WS /ws?token=<new_jwt> ─────────>  │
```

### Step 1 — Get WebSocket Token

The Android app calls this BEFORE opening the WebSocket. The token is a JWT valid for 5 minutes.

```
POST /api/v1/ws/token
Authorization: Bearer <session_token>
```

**Request:** No body required.

**Response (200):**
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOjEsInJvbGUiOiJwbGFubmVyIiwiZW50aXR5IjoiY2ZwbCIsImV4cCI6MTcxMzEwMDAwMH0.abc123",
  "expires_in": 300
}
```

| Field | Type | Description |
|-------|------|-------------|
| `token` | string | JWT token to use as WebSocket query param |
| `expires_in` | int | Token validity in seconds (300 = 5 minutes) |

**Error Responses:**
| Status | Body | When |
|--------|------|------|
| 401 | `{"detail": "Authentication required"}` | Missing or invalid session token |
| 503 | `{"detail": "WebSocket not configured"}` | Server not configured (WS_TOKEN_SECRET not set) |

### Step 2 — Open WebSocket Connection

```
WS wss://desktop-backend-vhf0.onrender.com/ws?token=<jwt_from_step_1>
```

For local dev:
```
WS ws://localhost:8000/ws?token=<jwt_from_step_1>
```

**On Success:** Server sends no initial message. Connection stays open. Events arrive as JSON text frames when they occur.

**On Failure (server closes immediately with code):**

| Close Code | Reason | Action |
|------------|--------|--------|
| 4001 | `Missing token` | Pass `?token=` query param |
| 4001 | `Invalid token` | Token is malformed — get a new one |
| 4002 | `Token expired` | Token TTL exceeded (>5 min) — get a new one |
| 4003 | `WebSocket not configured` | Backend issue — retry later |

### Step 3 — Receive Events

Every event arrives as a JSON text frame with this exact envelope:

```json
{
  "event_id": "a1b2c3d4-5678-9abc-def0-123456789abc",
  "event_type": "indent.sent",
  "timestamp": "2026-04-14T10:30:00.123456+00:00",
  "actor": "kaushal",
  "data": {
    "indent_id": 42,
    "material": "Sugar",
    "qty_kg": 500.0,
    "deadline": "2026-04-20"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `event_id` | string (UUID) | Unique ID for deduplication |
| `event_type` | string | Dot-separated event name (see catalog below) |
| `timestamp` | string (ISO 8601) | When the event was created |
| `actor` | string | Who triggered it (`"system"`, `"mcp"`, or a username) |
| `data` | object | Event-specific payload (varies by event_type) |

### Step 4 — Reconnection Logic

The WebSocket will disconnect if:
- Server restarts / deploys
- Network interruption
- Token expires (server won't kick you, but you should refresh proactively)

**Recommended Android reconnection strategy:**

```kotlin
// Pseudocode
var retryDelay = 1000L // 1 second

fun connect() {
    val token = api.getWsToken()  // POST /api/v1/ws/token
    val ws = OkHttpClient().newWebSocket(
        Request.Builder().url("wss://host/ws?token=$token").build(),
        object : WebSocketListener() {
            override fun onMessage(ws: WebSocket, text: String) {
                retryDelay = 1000L  // reset on success
                val event = Json.parseToJsonElement(text)
                handleEvent(event)
            }
            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                if (code == 4002) {
                    // Token expired — get new token and reconnect immediately
                    connect()
                } else {
                    // Exponential backoff: 1s, 2s, 4s, 8s, max 30s
                    retryDelay = min(retryDelay * 2, 30000L)
                    handler.postDelayed(::connect, retryDelay)
                }
            }
            override fun onFailure(ws: WebSocket, t: Throwable, resp: Response?) {
                retryDelay = min(retryDelay * 2, 30000L)
                handler.postDelayed(::connect, retryDelay)
            }
        }
    )
}
```

**Important:** On reconnect, the app should refresh its data from REST endpoints (e.g., re-fetch job cards, indents) to catch any events missed during disconnection. The server does NOT buffer missed events.

### Step 5 — Keeping Connection Alive

The client can send any text message (e.g., `"ping"`) to keep the connection active. The server reads and discards client messages. OkHttp/Retrofit handle WebSocket ping/pong at the protocol level automatically.

---

## 2. WebSocket Event Catalog

Every event type, its `data` payload, and which screen/feature it's relevant to.

### 2.1 Fulfillment Events

#### `fulfillment.synced`
**Triggered when:** SO lines are synced into the fulfillment pipeline.
**Relevant screen:** Fulfillment dashboard / demand summary.

```json
{
  "event_type": "fulfillment.synced",
  "data": {
    "synced": 5,
    "skipped": 2,
    "total": 7
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `synced` | int | Number of new fulfillment records created |
| `skipped` | int | Already existed or invalid (skipped) |
| `total` | int | Total SO lines processed |

**Frontend action:** Refresh fulfillment list if on that screen.

---

#### `fulfillment.revised`
**Triggered when:** A fulfillment record's quantity or deadline is changed.
**Relevant screen:** Fulfillment detail / order revisions.

```json
{
  "event_type": "fulfillment.revised",
  "data": {
    "fulfillment_id": 123,
    "new_qty": 450.0,
    "new_date": "2026-04-25",
    "revised_by": "kaushal"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `fulfillment_id` | int | Which fulfillment was revised |
| `new_qty` | float or null | New quantity (null if unchanged) |
| `new_date` | string or null | New deadline ISO date (null if unchanged) |
| `revised_by` | string | Who made the revision |

**Frontend action:** If viewing this fulfillment, refresh detail. Show toast notification.

---

### 2.2 Plan Events

#### `plan.approved`
**Triggered when:** A production plan is approved (triggers MRP + indent generation).
**Relevant screen:** Plan list / plan detail.

```json
{
  "event_type": "plan.approved",
  "data": {
    "plan_id": 42,
    "approved_by": "kaushal",
    "mrp_summary": {
      "total_materials": 8,
      "sufficient": 5,
      "shortage": 3,
      "total_shortage_kg": 125.5
    }
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `plan_id` | int | Approved plan ID |
| `approved_by` | string | Who approved |
| `mrp_summary` | object or absent | MRP results (may not be present if from mcp_planner) |

**Frontend action:** Update plan status to "approved" in list. Show notification with MRP summary.

---

### 2.3 MRP Events

#### `mrp.completed`
**Triggered when:** Material Requirements Planning finishes for a plan.
**Relevant screen:** Plan detail / material availability.

```json
{
  "event_type": "mrp.completed",
  "data": {
    "plan_id": 42,
    "summary": {
      "total_materials": 8,
      "sufficient": 5,
      "shortage": 3,
      "total_shortage_kg": 125.5
    }
  }
}
```

**Frontend action:** If viewing this plan, refresh material availability section.

---

#### `mrp.shortage_detected`
**Triggered when:** MRP finds material shortages (always follows `mrp.completed` when shortages exist).
**Relevant screen:** Plan detail / indent list / store dashboard.

```json
{
  "event_type": "mrp.shortage_detected",
  "data": {
    "plan_id": 42,
    "shortage_count": 3,
    "total_shortage_kg": 125.5
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `plan_id` | int | Which plan has shortages |
| `shortage_count` | int | Number of materials short |
| `total_shortage_kg` | float | Total shortage in kg |

**Frontend action:** Show alert/banner: "3 materials short (125.5 kg) for Plan #42". Navigate to indent list if tapped.

---

### 2.4 Indent Events

#### `indent.drafted`
**Triggered when:** Draft purchase indents are auto-generated from MRP shortages.
**Relevant screen:** Indent list.

```json
{
  "event_type": "indent.drafted",
  "data": {
    "plan_id": 42,
    "count": 3,
    "total_shortage_kg": 125.5
  }
}
```

**Frontend action:** Refresh indent list. Show badge: "3 new draft indents".

---

#### `indent.sent`
**Triggered when:** A single indent is sent (draft -> raised) to the purchase team.
**Relevant screen:** Indent list / store dashboard / purchase dashboard.

```json
{
  "event_type": "indent.sent",
  "data": {
    "indent_id": 99,
    "material": "Sugar",
    "qty_kg": 500.0,
    "deadline": "2026-04-20"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `indent_id` | int | Indent ID |
| `material` | string | Material SKU name |
| `qty_kg` | float | Required quantity |
| `deadline` | string or null | Required by date |

**Frontend action:** Update indent status in list. Store/purchase team: show push notification "New indent: Sugar 500 kg".

---

#### `indent.bulk_sent`
**Triggered when:** Multiple indents are sent at once.
**Relevant screen:** Indent list.

```json
{
  "event_type": "indent.bulk_sent",
  "data": {
    "indent_ids": [99, 100, 101],
    "sent": 3
  }
}
```

**Frontend action:** Refresh indent list. Show toast: "3 indents sent".

---

#### `indent.raised`
**Triggered when:** A purchase indent is raised manually from the floor (Materials tab → "Raise Purchase Indent" button).
**Relevant screen:** Indent list / store dashboard.

```json
{
  "event_type": "indent.raised",
  "data": {
    "indent_id": 105,
    "material": "Sugar",
    "qty_kg": 200.0,
    "source": "floor",
    "job_card_id": "45"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `indent_id` | int | New indent ID |
| `material` | string | Material name |
| `qty_kg` | float | Quantity requested |
| `source` | string | `"floor"` (manual raise) or `"manual"` |
| `job_card_id` | string or null | Job card that triggered it |

**Frontend action:** Store/purchase: show notification "New indent from floor: Sugar 200 kg". Refresh indent list.

---

### 2.5 Job Card Events

#### `job_card.created`
**Triggered when:** Job cards are generated from a production order.
**Relevant screen:** Job card list / floor dashboard.

```json
{
  "event_type": "job_card.created",
  "data": {
    "prod_order_id": 15,
    "job_card_count": 4
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `prod_order_id` | int | Production order that generated the cards |
| `job_card_count` | int | How many job cards were created |

**Frontend action:** Refresh job card list. Show notification: "4 new job cards created".

---

#### `job_card.started`
**Triggered when:** Floor supervisor taps "START PRODUCTION" on a job card.
**Relevant screen:** Job card list / job card detail / floor dashboard.

```json
{
  "event_type": "job_card.started",
  "data": {
    "job_card_id": 101,
    "job_card_number": "PO-001/1",
    "fg_sku_name": "Roasted Cashew 500g",
    "floor": "production_floor"
  }
}
```

**Frontend action:** Update job card status chip to "in_progress" (green pulse). If on list, move card to "In Progress" section.

---

#### `job_card.completed`
**Triggered when:** Floor supervisor taps "COMPLETE" on a job card.
**Relevant screen:** Job card list / job card detail / floor dashboard / planner dashboard.

```json
{
  "event_type": "job_card.completed",
  "data": {
    "job_card_id": 101,
    "job_card_number": "PO-001/1",
    "fg_sku_name": "Roasted Cashew 500g",
    "duration_minutes": 145.5
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `duration_minutes` | float or null | Total production time |

**Frontend action:** Update status chip to "completed". Show duration. Move card to completed section.

---

#### `job_card.team_assigned`
**Triggered when:** Team leader + members are assigned to a job card (Overview tab → "Assign Team").
**Relevant screen:** Job card detail (Overview tab).

```json
{
  "event_type": "job_card.team_assigned",
  "data": {
    "job_card_id": 101,
    "job_card_number": "PO-001/1",
    "team_leader": "Ramesh",
    "member_count": 3
  }
}
```

**Frontend action:** Update team section in Overview tab. Show toast: "Team assigned to PO-001/1".

---

#### `job_card.material_received`
**Triggered when:** Material is scanned/received via QR codes (Materials tab → "Submit Scan").
**Relevant screen:** Job card detail (Materials tab) / store dashboard.

```json
{
  "event_type": "job_card.material_received",
  "data": {
    "job_card_id": 101,
    "job_card_number": "PO-001/1",
    "boxes_scanned": 5,
    "total_kg": 125.0
  }
}
```

**Frontend action:** Update RM indent progress bars. Update floor stock status. Store: show notification.

---

#### `job_card.material_acknowledged`
**Triggered when:** Material is manually acknowledged (Materials tab → "Manual Acknowledge All").
**Relevant screen:** Job card detail (Materials tab) / store dashboard.

```json
{
  "event_type": "job_card.material_acknowledged",
  "data": {
    "job_card_id": 101,
    "job_card_number": "PO-001/1"
  }
}
```

**Frontend action:** Update all indent statuses to "floor_available" in Materials tab.

---

#### `job_card.dispatched_to_next`
**Triggered when:** Output is dispatched to the next production stage (Materials tab → "Dispatch").
**Relevant screen:** Job card detail (Materials tab) / stage chain.

```json
{
  "event_type": "job_card.dispatched_to_next",
  "data": {
    "job_card_id": 101,
    "job_card_number": "PO-001/1",
    "qty_kg": 95.0,
    "dispatched_by": "Ramesh"
  }
}
```

**Frontend action:** Update dispatch log. Update "Carried in" on next stage. Show toast: "95 kg dispatched".

---

#### `job_card.output_saved`
**Triggered when:** FG output is recorded (Output tab → "Save Output").
**Relevant screen:** Job card detail (Output tab) / planner dashboard.

```json
{
  "event_type": "job_card.output_saved",
  "data": {
    "job_card_id": 101,
    "job_card_number": "PO-001/1",
    "fg_actual_kg": 92.5,
    "yield_pct": 95.2
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `fg_actual_kg` | float | Actual FG output in kg |
| `yield_pct` | float or null | Yield percentage |

**Frontend action:** Update output section. Show yield badge. Planner: update production summary.

---

#### `job_card.signed_off`
**Triggered when:** A sign-off is submitted (Sign-offs tab → "Sign").
**Relevant screen:** Job card detail (Sign-offs tab).

```json
{
  "event_type": "job_card.signed_off",
  "data": {
    "job_card_id": 101,
    "job_card_number": "PO-001/1",
    "sign_off_type": "production_incharge",
    "signed_by": "Kaushal"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `sign_off_type` | string | `production_incharge`, `quality_analysis`, `warehouse_incharge` |
| `signed_by` | string | Who signed |

**Frontend action:** Turn sign-off card green. Show name + timestamp. If all 3 signed, enable "Close" action.

---

#### `job_card.force_unlocked`
**Triggered when:** A locked job card is force-unlocked by authority.
**Relevant screen:** Job card detail.

```json
{
  "event_type": "job_card.force_unlocked",
  "data": {
    "job_card_id": 102,
    "job_card_number": "PO-001/2",
    "reason": "Previous stage partial dispatch"
  }
}
```

**Frontend action:** Update job card status from "locked" to "unlocked". Show notification with reason.

---

### 2.6 QC Events

#### `qc.passed`
**Triggered when:** A QC inspection passes.
**Relevant screen:** QC queue / job card detail.

```json
{
  "event_type": "qc.passed",
  "data": {
    "inspection_id": "QCI-20260414-0001",
    "result": "pass",
    "findings": "All parameters within range"
  }
}
```

**Frontend action:** Update QC status indicator to green. Remove from pending QC queue.

---

#### `qc.failed`
**Triggered when:** A QC inspection fails (auto-holds affected job cards).
**Relevant screen:** QC queue / job card detail / floor dashboard.

```json
{
  "event_type": "qc.failed",
  "data": {
    "inspection_id": "QCI-20260414-0003",
    "result": "fail",
    "findings": "Moisture content 14.2% exceeds max 12%"
  }
}
```

**Frontend action:** Show red alert. Update QC indicator. Floor supervisor: push notification "QC FAIL: [findings]".

---

### 2.7 Material Movement Events

#### `material.moved`
**Triggered when:** Material is moved between floor locations (RM store -> production floor, etc.).
**Relevant screen:** Floor inventory / movement history.

```json
{
  "event_type": "material.moved",
  "data": {
    "sku_name": "Sugar",
    "from": "rm_store",
    "to": "production_floor",
    "qty_kg": 250.0,
    "movement_id": 567
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `sku_name` | string | Material name |
| `from` | string | Source floor location |
| `to` | string | Destination floor location |
| `qty_kg` | float | Quantity moved |
| `movement_id` | int or null | Audit trail ID |

**Frontend action:** Update floor inventory quantities if viewing that floor. Show toast.

---

### 2.8 Day-End Events

#### `dayend.reconciled`
**Triggered when:** A balance scan is reconciled (physical count adjustments applied).
**Relevant screen:** Day-end dashboard / balance scan list.

```json
{
  "event_type": "dayend.reconciled",
  "data": {
    "scan_id": 88,
    "floor_location": "rm_store"
  }
}
```

**Frontend action:** Update scan status to "reconciled" in day-end dashboard.

---

#### `dayend.discrepancy_found`
**Triggered when:** An internal discrepancy is reported (quality issue, machine breakdown, contamination, etc.).
**Relevant screen:** Discrepancy list / floor dashboard.

```json
{
  "event_type": "dayend.discrepancy_found",
  "data": {
    "discrepancy_type": "rm_grade_mismatch",
    "severity": "major",
    "affected_material": "Sugar",
    "affected_job_cards": 2
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `discrepancy_type` | string | `rm_grade_mismatch`, `qc_failure`, `machine_breakdown`, `contamination`, `short_delivery` |
| `severity` | string | `minor`, `major`, `critical` |
| `affected_material` | string or null | Material involved (null for machine issues) |
| `affected_job_cards` | int | Number of job cards auto-held |

**Frontend action:** Show red alert with severity badge. If critical: push notification to all relevant roles.

---

### 2.9 Store Alert Events

#### `store_alert.created`
**Triggered when:** Store team makes an allocation decision (approve/reject/partial) for a job card material request.
**Relevant screen:** Store allocation dashboard / job card detail.

```json
{
  "event_type": "store_alert.created",
  "data": {
    "allocation_id": 45,
    "decision": "partial",
    "material": "Sugar",
    "approved_qty": 350.0
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `allocation_id` | int | Store allocation record ID |
| `decision` | string | `approved`, `rejected`, `partial` |
| `material` | string | Material name |
| `approved_qty` | float | Quantity approved (0 if rejected) |

**Frontend action:** Update allocation status. If partial/rejected: show alert to production team.

---

## 3. Role-Based Event Filtering

The server automatically filters events based on the user's role (from the JWT token). Each role only receives events relevant to their work.

| Role | Events Received | Typical Screens |
|------|-----------------|-----------------|
| `planner` | `plan.*`, `mrp.*`, `fulfillment.*` | Plan dashboard, MRP, fulfillment |
| `inventory_manager` | `indent.*`, `material.*`, `store_alert.*` | Store dashboard, inventory, indents |
| `floor_supervisor` | `job_card.*` (all 11 events), `qc.*`, `dayend.*` | Job card list/detail, QC queue, day-end |
| `purchase` | `indent.*` (drafted, sent, bulk_sent, raised) | Purchase dashboard, PO tracking |
| `admin` | **ALL 25 events** | Admin dashboard |

**The frontend does NOT need to filter** — the server only sends events the user's role should see. The `event_type` prefix determines routing.

---

## 4. Webhook Management API

These endpoints are for the **admin dashboard** to manage external webhook integrations (Tally, supplier portal, IMS, Slack). Requires `production/webhooks` permission.

### 4.1 Create Webhook Endpoint

Register a new external URL to receive webhook deliveries.

```
POST /api/v1/webhooks/endpoints
Authorization: Bearer <session_token>
Content-Type: application/json
```

**Request Body:**
```json
{
  "entity": "cfpl",
  "url": "https://supplier-portal.example.com/webhook",
  "description": "Supplier indent notifications"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity` | string | Yes | Entity filter (`"cfpl"` or `"cdpl"`) |
| `url` | string | Yes | HTTPS URL to receive POST callbacks |
| `description` | string | No | Human-readable label |

**Response (200):**
```json
{
  "id": 1,
  "entity": "cfpl",
  "url": "https://supplier-portal.example.com/webhook",
  "description": "Supplier indent notifications",
  "is_active": true,
  "created_at": "2026-04-14T10:00:00+00:00",
  "secret": "a1b2c3d4e5f6...64_hex_chars"
}
```

**IMPORTANT:** The `secret` field is only returned on creation. Store it securely — it's the HMAC signing key for verifying webhook payloads. It cannot be retrieved later.

---

### 4.2 List Webhook Endpoints

```
GET /api/v1/webhooks/endpoints?entity=cfpl
Authorization: Bearer <session_token>
```

| Query Param | Type | Required | Description |
|-------------|------|----------|-------------|
| `entity` | string | No | Filter by entity (omit for all) |

**Response (200):**
```json
[
  {
    "id": 1,
    "entity": "cfpl",
    "url": "https://supplier-portal.example.com/webhook",
    "description": "Supplier indent notifications",
    "is_active": true,
    "created_by": "Kaushal Patel",
    "created_at": "2026-04-14T10:00:00+00:00"
  }
]
```

Note: `secret` is NOT returned in list view.

---

### 4.3 Update Webhook Endpoint

```
PUT /api/v1/webhooks/endpoints/{endpoint_id}
Authorization: Bearer <session_token>
Content-Type: application/json
```

**Request Body (all fields optional):**
```json
{
  "url": "https://new-url.example.com/webhook",
  "description": "Updated description",
  "is_active": false
}
```

**Response (200):**
```json
{ "updated": true }
```

---

### 4.4 Deactivate Webhook Endpoint

Soft-delete — sets `is_active = false`. No deliveries will be sent.

```
DELETE /api/v1/webhooks/endpoints/{endpoint_id}
Authorization: Bearer <session_token>
```

**Response (200):**
```json
{ "deactivated": true }
```

---

### 4.5 Create Subscription

Subscribe an endpoint to a specific event type. Wildcard `*` subscribes to all events.

```
POST /api/v1/webhooks/subscriptions
Authorization: Bearer <session_token>
Content-Type: application/json
```

**Request Body:**
```json
{
  "endpoint_id": 1,
  "event_type": "indent.sent"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `endpoint_id` | int | Yes | Which webhook endpoint |
| `event_type` | string | Yes | Event type to subscribe to (e.g., `"indent.sent"`, `"*"`) |

**Available event types for subscription:**
```
fulfillment.synced
fulfillment.revised
plan.approved
mrp.completed
mrp.shortage_detected
indent.drafted
indent.sent
indent.bulk_sent
indent.raised
job_card.created
job_card.started
job_card.completed
job_card.team_assigned
job_card.material_received
job_card.material_acknowledged
job_card.dispatched_to_next
job_card.output_saved
job_card.signed_off
job_card.force_unlocked
qc.passed
qc.failed
material.moved
dayend.reconciled
dayend.discrepancy_found
store_alert.created
*                        (wildcard — all events)
```

**Response (200):**
```json
{
  "id": 1,
  "endpoint_id": 1,
  "event_type": "indent.sent",
  "is_active": true,
  "created_at": "2026-04-14T10:05:00+00:00"
}
```

**Error (409):**
```json
{ "detail": "Subscription already exists" }
```

---

### 4.6 List Subscriptions

```
GET /api/v1/webhooks/subscriptions?endpoint_id=1
Authorization: Bearer <session_token>
```

| Query Param | Type | Required | Description |
|-------------|------|----------|-------------|
| `endpoint_id` | int | No | Filter by endpoint (omit for all) |

**Response (200):**
```json
[
  {
    "id": 1,
    "endpoint_id": 1,
    "event_type": "indent.sent",
    "filter_jsonb": {},
    "is_active": true,
    "created_at": "2026-04-14T10:05:00+00:00"
  }
]
```

---

### 4.7 Delete Subscription

```
DELETE /api/v1/webhooks/subscriptions/{sub_id}
Authorization: Bearer <session_token>
```

**Response (200):**
```json
{ "deleted": true }
```

---

### 4.8 List Deliveries (Delivery Log)

Paginated log of all webhook delivery attempts.

```
GET /api/v1/webhooks/deliveries?endpoint_id=1&status=failed&page=1&page_size=50
Authorization: Bearer <session_token>
```

| Query Param | Type | Required | Description |
|-------------|------|----------|-------------|
| `endpoint_id` | int | No | Filter by endpoint |
| `event_type` | string | No | Filter by event type |
| `status` | string | No | Filter: `pending`, `delivered`, `failed`, `exhausted` |
| `page` | int | No | Page number (default 1) |
| `page_size` | int | No | Items per page (default 50) |

**Response (200):**
```json
{
  "total": 150,
  "page": 1,
  "results": [
    {
      "id": 1001,
      "endpoint_id": 1,
      "event_type": "indent.sent",
      "event_id": "a1b2c3d4-...",
      "status": "delivered",
      "attempts": 1,
      "last_attempt_at": "2026-04-14T10:30:01+00:00",
      "response_code": 200,
      "response_body": "OK",
      "created_at": "2026-04-14T10:30:00+00:00"
    }
  ]
}
```

| Status | Meaning |
|--------|---------|
| `pending` | Just created, not yet attempted |
| `delivered` | Successfully delivered (HTTP < 400) |
| `failed` | Failed on last attempt (will retry) |
| `exhausted` | Failed after 3 attempts — gave up |

---

### 4.9 Retry Failed Delivery

Re-attempt delivery of a failed/exhausted webhook.

```
POST /api/v1/webhooks/deliveries/{delivery_id}/retry
Authorization: Bearer <session_token>
```

**Response (200):**
```json
{
  "retried": true,
  "delivery_id": 1001
}
```

Retry happens asynchronously in the background. Check delivery status via the list endpoint.

---

### 4.10 Test Webhook Endpoint

Send a test `ping` event to verify an endpoint is reachable.

```
POST /api/v1/webhooks/test?endpoint_id=1
Authorization: Bearer <session_token>
```

Or test a URL directly (without creating an endpoint):
```
POST /api/v1/webhooks/test?url=https://httpbin.org/post
Authorization: Bearer <session_token>
```

**Response (200):**
```json
{
  "status_code": 200,
  "body": "{\"success\": true}"
}
```

Or on failure:
```json
{
  "error": "ConnectTimeout: timed out"
}
```

---

## 5. Error Codes & Handling

### REST API Errors

| Status | When | Response Body |
|--------|------|---------------|
| 400 | Invalid request body | `{"detail": "No fields to update"}` |
| 401 | Missing/invalid session token | `{"detail": "Authentication required"}` |
| 403 | No `production/webhooks` permission | `{"detail": "Permission denied: production/webhooks/view"}` |
| 404 | Resource not found | `{"detail": "Endpoint not found"}` |
| 409 | Duplicate subscription | `{"detail": "Subscription already exists"}` |
| 503 | Server not configured | `{"detail": "WebSocket not configured"}` |

### WebSocket Close Codes

| Code | Reason | Client Action |
|------|--------|---------------|
| 4001 | `Missing token` / `Invalid token` | Fix the token and reconnect |
| 4002 | `Token expired` | Call `POST /api/v1/ws/token` again, reconnect |
| 4003 | `WebSocket not configured` | Server issue — retry with backoff |
| 1000 | Normal closure | Server shutting down — reconnect with backoff |
| 1006 | Abnormal closure | Network issue — reconnect with backoff |

### Webhook Delivery Headers

When the server delivers to a registered webhook endpoint, it sends these headers:

```
POST <registered_url>
Content-Type: application/json
X-Webhook-Signature: sha256=<hmac_hex>
X-Webhook-Event: indent.sent
X-Webhook-Id: a1b2c3d4-5678-9abc-def0-123456789abc
X-Webhook-Timestamp: 2026-04-14T10:30:00.123456+00:00
```

**To verify the signature (in the receiving system):**
```python
import hmac, hashlib

def verify(secret: str, raw_body: bytes, signature_header: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

---

## Quick Reference — All Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/ws/token` | Session | Get WebSocket JWT token |
| `WS` | `/ws?token=<jwt>` | JWT | WebSocket connection |
| `POST` | `/api/v1/webhooks/endpoints` | Permission | Register webhook URL |
| `GET` | `/api/v1/webhooks/endpoints` | Permission | List webhook URLs |
| `PUT` | `/api/v1/webhooks/endpoints/{id}` | Permission | Update webhook URL |
| `DELETE` | `/api/v1/webhooks/endpoints/{id}` | Permission | Deactivate webhook URL |
| `POST` | `/api/v1/webhooks/subscriptions` | Permission | Subscribe to event type |
| `GET` | `/api/v1/webhooks/subscriptions` | Permission | List subscriptions |
| `DELETE` | `/api/v1/webhooks/subscriptions/{id}` | Permission | Unsubscribe |
| `GET` | `/api/v1/webhooks/deliveries` | Permission | Delivery log (paginated) |
| `POST` | `/api/v1/webhooks/deliveries/{id}/retry` | Permission | Retry failed delivery |
| `POST` | `/api/v1/webhooks/test` | Permission | Test webhook endpoint |
