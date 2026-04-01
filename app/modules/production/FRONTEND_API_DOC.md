# Production Planning Module — Frontend API Documentation

**Base URL:** `http://localhost:8000`
**All production endpoints prefixed with:** `/api/v1/production`

---

## Table of Contents

1. [Health Check](#1-health-check)
2. [SO Fulfillment](#2-so-fulfillment)
   - 2.1 Sync Fulfillment
   - 2.2 List Fulfillment (Paginated)
   - 2.3 Demand Summary
   - 2.4 FY Close Review
   - 2.5 Carryforward Orders
   - 2.6 Revise Order
3. [Plan Generation (AI)](#3-plan-generation-ai)
   - 3.1 Generate Daily Plan
   - 3.2 Generate Weekly Plan
4. [Plan CRUD](#4-plan-crud)
   - 4.1 Create Manual Plan
   - 4.2 List Plans (Paginated)
   - 4.3 Get Plan Detail
   - 4.4 Edit Plan Line
   - 4.5 Add Plan Line
   - 4.6 Delete Plan Line
   - 4.7 Approve Plan
   - 4.8 Cancel Plan
5. [MCP Tools (Claude Desktop)](#5-mcp-tools)

---

## 1. Health Check

### `GET /api/v1/production/health`

Returns module status and row counts per table.

**Request:** No parameters.

**Response:**
```json
{
  "status": "ok",
  "module": "production",
  "tables": {
    "bom_header": 1084,
    "bom_line": 4440,
    "bom_process_route": 2300,
    "machine": 86,
    "machine_capacity": 188,
    "so_fulfillment": 0,
    "production_plan": 0
  }
}
```

---

## 2. SO Fulfillment

### 2.1 Sync Fulfillment

**`POST /api/v1/production/fulfillment/sync`**

Syncs all FG Sales Order lines into the `so_fulfillment` table. Idempotent — skips already synced records.

**Request Body:**
```json
{
  "entity": "cfpl"       // optional — filter by entity, or null for all
}
```

**Response:**
```json
{
  "synced": 150,
  "skipped": 30,
  "total": 180
}
```

---

### 2.2 List Fulfillment (Paginated)

**`GET /api/v1/production/fulfillment`**

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `entity` | string | null | `cfpl` or `cdpl` |
| `status` | string | null | Comma-separated: `open,partial,fulfilled,carryforward,cancelled` |
| `financial_year` | string | null | e.g. `2025-26` |
| `customer` | string | null | Partial match (ILIKE) |
| `search` | string | null | Searches fg_sku_name and customer_name |
| `page` | int | 1 | Page number |
| `page_size` | int | 50 | Items per page (max 200) |

**Response:**
```json
{
  "results": [
    {
      "fulfillment_id": 1,
      "so_line_id": 42,
      "so_id": 10,
      "financial_year": "2025-26",
      "fg_sku_name": "Cashew W320 250g",
      "customer_name": "D-Mart",
      "original_qty_kg": 500.0,
      "revised_qty_kg": null,
      "pending_qty_kg": 500.0,
      "produced_qty_kg": 0.0,
      "dispatched_qty_kg": 0.0,
      "order_status": "open",
      "delivery_deadline": "2026-04-05",
      "priority": 5,
      "carryforward_from_id": null,
      "entity": "cfpl",
      "created_at": "2026-03-27T15:00:00Z",
      "updated_at": "2026-03-27T15:00:00Z",
      "so_number": "SO-2026-001",
      "so_date": "2026-03-28"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 50,
    "total": 150,
    "total_pages": 3
  }
}
```

---

### 2.3 Demand Summary

**`GET /api/v1/production/fulfillment/demand-summary`**

Aggregated pending demand grouped by product + customer.

**Query Parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `entity` | string | optional |
| `financial_year` | string | optional |

**Response:**
```json
[
  {
    "fg_sku_name": "Cashew W320 250g",
    "customer_name": "D-Mart",
    "total_qty_kg": 1500.0,
    "order_count": 3,
    "earliest_deadline": "2026-04-03"
  },
  {
    "fg_sku_name": "Dates Arabian 500g",
    "customer_name": "Amazon",
    "total_qty_kg": 800.0,
    "order_count": 2,
    "earliest_deadline": "2026-04-05"
  }
]
```

---

### 2.4 FY Close Review

**`GET /api/v1/production/fulfillment/fy-review`**

All unfulfilled orders for FY transition review.

**Query Parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `entity` | string | optional |
| `financial_year` | string | optional — defaults to current FY |

**Response:**
```json
[
  {
    "fulfillment_id": 1,
    "fg_sku_name": "Cashew W320 250g",
    "customer_name": "D-Mart",
    "original_qty_kg": 500.0,
    "pending_qty_kg": 200.0,
    "produced_qty_kg": 300.0,
    "delivery_deadline": "2026-03-30",
    "order_status": "partial",
    "financial_year": "2025-26",
    "so_number": "SO-2026-001"
  }
]
```

---

### 2.5 Carryforward Orders

**`POST /api/v1/production/fulfillment/carryforward`**

Bulk carry forward selected unfulfilled orders to a new FY.

**Request Body:**
```json
{
  "fulfillment_ids": [1, 5, 12],
  "new_fy": "2026-27",
  "revised_by": "Planner Name"
}
```

**Response:**
```json
{
  "carried": 3,
  "total_requested": 3,
  "new_fy": "2026-27"
}
```

---

### 2.6 Revise Order

**`PUT /api/v1/production/fulfillment/{fulfillment_id}/revise`**

Revise qty and/or deadline. Creates audit trail in `so_revision_log`.

**Request Body:**
```json
{
  "new_qty": 600.0,             // optional — new quantity in kg
  "new_date": "2026-04-10",    // optional — new delivery deadline
  "reason": "Customer increased order",
  "revised_by": "Planner Name"
}
```

**Response:**
```json
{
  "fulfillment_id": 1,
  "revised": true
}
```

**Errors:** `404` if fulfillment_id not found.

---

## 3. Plan Generation (AI)

### 3.1 Generate Daily Plan

**`POST /api/v1/production/plans/generate-daily`**

Sends selected fulfillment orders to Claude AI, gets back a production schedule. Creates a draft plan.

**Request Body:**
```json
{
  "entity": "cfpl",
  "date": "2026-04-01",
  "fulfillment_ids": [1, 2, 5, 12]
}
```

**Response (success):**
```json
{
  "plan_id": 1,
  "status": "draft",
  "lines": 3,
  "material_check": [
    {
      "material": "Cashew 320 Raw",
      "type": "rm",
      "needed_kg": 525,
      "available_kg": 2000,
      "status": "SUFFICIENT"
    },
    {
      "material": "Almond California",
      "type": "rm",
      "needed_kg": 700,
      "available_kg": 300,
      "status": "SHORTAGE"
    }
  ],
  "risk_flags": [
    {
      "flag": "Almond shortage: 400 kg gap",
      "severity": "warning",
      "details": "Indent recommended"
    }
  ],
  "no_bom_items": [
    {
      "fulfillment_id": 99,
      "fg_sku_name": "Custom Product XYZ"
    }
  ]
}
```

**Response (no demand):**
```json
{
  "error": "no_demand",
  "message": "No valid demand items found for the selected fulfillment IDs",
  "no_bom_items": []
}
```

**Errors:** `500` if ANTHROPIC_API_KEY not configured.

---

### 3.2 Generate Weekly Plan

**`POST /api/v1/production/plans/generate-weekly`**

Same as daily but spans multiple days.

**Request Body:**
```json
{
  "entity": "cfpl",
  "date_from": "2026-04-01",
  "date_to": "2026-04-07",
  "fulfillment_ids": [1, 2, 5, 12, 15, 20]
}
```

**Response:** Same structure as daily plan.

---

## 4. Plan CRUD

### 4.1 Create Manual Plan

**`POST /api/v1/production/plans/create`**

Create an empty plan without AI (add lines manually).

**Request Body:**
```json
{
  "entity": "cfpl",
  "plan_name": "April Week 1 Plan",
  "plan_type": "daily",          // "daily" or "weekly"
  "date_from": "2026-04-01",
  "date_to": "2026-04-01"
}
```

**Response:**
```json
{
  "plan_id": 2,
  "status": "draft"
}
```

---

### 4.2 List Plans (Paginated)

**`GET /api/v1/production/plans`**

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `entity` | string | null | `cfpl` or `cdpl` |
| `status` | string | null | `draft`, `approved`, `executed`, `cancelled` |
| `plan_type` | string | null | `daily` or `weekly` |
| `date_from` | string | null | Plan date >= (YYYY-MM-DD) |
| `date_to` | string | null | Plan date <= (YYYY-MM-DD) |
| `page` | int | 1 | |
| `page_size` | int | 50 | Max 200 |

**Response:**
```json
{
  "results": [
    {
      "plan_id": 1,
      "plan_name": "Daily Plan — 2026-04-01",
      "entity": "cfpl",
      "plan_type": "daily",
      "plan_date": "2026-04-01",
      "date_from": "2026-04-01",
      "date_to": "2026-04-01",
      "status": "draft",
      "ai_generated": true,
      "revision_number": 1,
      "approved_by": null,
      "approved_at": null,
      "created_at": "2026-03-27T16:00:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 50,
    "total": 5,
    "total_pages": 1
  }
}
```

---

### 4.3 Get Plan Detail

**`GET /api/v1/production/plans/{plan_id}`**

Returns the full plan with all lines, material check, and risk flags.

**Response:**
```json
{
  "plan_id": 1,
  "plan_name": "Daily Plan — 2026-04-01",
  "entity": "cfpl",
  "plan_type": "daily",
  "plan_date": "2026-04-01",
  "date_from": "2026-04-01",
  "date_to": "2026-04-01",
  "status": "draft",
  "ai_generated": true,
  "ai_analysis_json": { "..." : "..." },
  "revision_number": 1,
  "previous_plan_id": null,
  "approved_by": null,
  "approved_at": null,
  "created_at": "2026-03-27T16:00:00Z",

  "lines": [
    {
      "plan_line_id": 1,
      "plan_id": 1,
      "fg_sku_name": "Cashew W320 250g",
      "customer_name": "D-Mart",
      "bom_id": 15,
      "planned_qty_kg": 500.0,
      "planned_qty_units": 2000,
      "machine_id": 3,
      "priority": 1,
      "shift": "day",
      "stage_sequence": ["sorting", "roasting", "packaging"],
      "estimated_hours": 5.0,
      "linked_so_fulfillment_ids": [1],
      "reasoning": "D-Mart deadline Apr 5, RM available, machine idle",
      "status": "planned",
      "created_at": "2026-03-27T16:00:00Z"
    },
    {
      "plan_line_id": 2,
      "fg_sku_name": "Dates Arabian 500g",
      "customer_name": "Amazon",
      "bom_id": 88,
      "planned_qty_kg": 200.0,
      "planned_qty_units": 400,
      "machine_id": 5,
      "priority": 2,
      "shift": "day",
      "stage_sequence": ["packaging"],
      "estimated_hours": 2.0,
      "linked_so_fulfillment_ids": [2],
      "reasoning": "Repackaging — source FG available in fg_store",
      "status": "planned"
    }
  ],

  "material_check": [
    { "material": "Cashew 320 Raw", "type": "rm", "needed_kg": 525, "available_kg": 2000, "status": "SUFFICIENT" },
    { "material": "PL Dates 1kg", "type": "fg", "needed_kg": 200, "available_kg": 800, "status": "SUFFICIENT" }
  ],

  "risk_flags": [
    { "flag": "Roasting Machine at 90% capacity", "severity": "warning", "details": "Consider splitting across 2 shifts" }
  ]
}
```

---

### 4.4 Edit Plan Line (Partial Update)

**`PUT /api/v1/production/plans/{plan_id}/lines/{line_id}`**

Edit any field on a plan line. **Only fields explicitly included in the request body are updated** — omitted fields remain untouched.

**Constraint:** Plan must be in `draft` status.

**Request Body** (all fields optional — send only what you want to change):
```json
{
  "fg_sku_name": "Almond California 1kg",
  "customer_name": "Retail",
  "bom_id": 22,
  "planned_qty_kg": 300.0,
  "planned_qty_units": 300,
  "machine_id": 7,
  "priority": 2,
  "shift": "night",
  "stage_sequence": ["sorting", "blanching", "packaging"],
  "estimated_hours": 4.5,
  "reasoning": "Changed to night shift due to machine conflict",
  "status": "planned"
}
```

**Example — only change priority and machine:**
```json
{
  "priority": 1,
  "machine_id": 5
}
```

**Response:**
```json
{
  "plan_line_id": 2,
  "updated": true,
  "fields_changed": ["priority", "machine_id"]
}
```

**Errors:**
- `404` — Plan or line not found
- `400` — Plan is not draft, or no fields provided

---

### 4.5 Add Plan Line

**`POST /api/v1/production/plans/{plan_id}/lines`**

Add a manual line to a draft plan.

**Constraint:** Plan must be in `draft` status.

**Request Body:**
```json
{
  "fg_sku_name": "Raisin Golden 500g",
  "customer_name": "BigBasket",
  "bom_id": null,                    // optional — auto-resolved from fg_sku_name
  "planned_qty_kg": 250.0,
  "planned_qty_units": 500,          // optional
  "machine_id": null,                // optional
  "priority": 5,                     // default: 5
  "shift": "day"                     // default: "day"
}
```

**Response:**
```json
{
  "plan_line_id": 5,
  "plan_id": 1
}
```

---

### 4.6 Delete Plan Line

**`DELETE /api/v1/production/plans/{plan_id}/lines/{line_id}`**

Remove a line from a draft plan.

**Constraint:** Plan must be in `draft` status.

**Response:**
```json
{
  "deleted": true
}
```

---

### 4.7 Approve Plan

**`PUT /api/v1/production/plans/{plan_id}/approve?approved_by=John`**

Approve a draft plan. Sets status to `approved`.

**Query Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `approved_by` | string | Yes | Name of the approver |

**Constraint:** Plan must be in `draft` status.

**Response:**
```json
{
  "plan_id": 1,
  "status": "approved",
  "approved_by": "John"
}
```

---

### 4.8 Cancel Plan

**`PUT /api/v1/production/plans/{plan_id}/cancel`**

Cancel a plan (draft or approved).

**Response:**
```json
{
  "plan_id": 1,
  "status": "cancelled"
}
```

---

## 5. MCP Tools (Claude Desktop)

These tools are exposed via the MCP stdio server (`mcp_server.py`) for use directly in Claude Desktop. They mirror the API endpoints but are called by Claude as tools during conversation.

| # | Tool Name | Description | Mirrors Endpoint |
|---|-----------|-------------|-----------------|
| 1 | `sync_fulfillment` | Sync SO lines to fulfillment | `POST /fulfillment/sync` |
| 2 | `get_planning_context` | Get demand + inventory + machines + BOMs for plan generation | Internal (used by generate-daily) |
| 3 | `get_demand_summary` | Aggregated pending demand | `GET /fulfillment/demand-summary` |
| 4 | `get_fulfillment_list` | Paginated fulfillment list | `GET /fulfillment` |
| 5 | `save_production_plan` | Save Claude's plan to DB | `POST /plans/generate-daily` (save step) |
| 6 | `list_plans` | List production plans | `GET /plans` |
| 7 | `get_plan_detail` | Plan detail with lines | `GET /plans/{id}` |

### MCP Flow (in Claude Desktop):

```
1. User: "Sync my sales orders"
   → Claude calls: sync_fulfillment(entity="cfpl")
   → Returns: "Synced 150 records"

2. User: "Show pending demand"
   → Claude calls: get_fulfillment_list(entity="cfpl")
   → Returns: list with fulfillment_ids, products, customers, deadlines

3. User: "Plan production for IDs 1, 2, 5"
   → Claude calls: get_planning_context(entity="cfpl", fulfillment_ids=[1,2,5])
   → Returns: full context JSON (demand with BOMs, inventory, machines)

4. Claude analyzes context and creates schedule (no API credits — uses Claude Desktop subscription)

5. Claude calls: save_production_plan(entity="cfpl", plan_type="daily", ...)
   → Returns: "Plan saved! plan_id=1, 3 lines. Status: draft"

6. User approves via: PUT /api/v1/production/plans/1/approve?approved_by=Name
```

---

## Frontend Page → Endpoint Mapping

| Page | Endpoints Used |
|------|----------------|
| **Production Dashboard** | `GET /health`, `GET /fulfillment/demand-summary`, `GET /plans` |
| **Fulfillment List** | `GET /fulfillment`, `POST /fulfillment/sync` |
| **FY Close Review** | `GET /fulfillment/fy-review`, `POST /fulfillment/carryforward`, `PUT /fulfillment/{id}/revise` |
| **Plan Generation** | `GET /fulfillment` (select orders), `POST /plans/generate-daily`, `POST /plans/generate-weekly` |
| **Plan List** | `GET /plans` |
| **Plan Detail / Edit** | `GET /plans/{id}`, `PUT /plans/{id}/lines/{lid}`, `POST /plans/{id}/lines`, `DELETE /plans/{id}/lines/{lid}` |
| **Plan Approval** | `PUT /plans/{id}/approve`, `PUT /plans/{id}/cancel` |

---

## Status Values Reference

### Fulfillment Status (`so_fulfillment.order_status`)
| Status | Description | Color |
|--------|-------------|-------|
| `open` | New, not yet produced | White |
| `partial` | Partially produced | Yellow |
| `fulfilled` | Fully produced + dispatched | Green |
| `carryforward` | Carried to next FY | Blue |
| `cancelled` | Cancelled | Grey |

### Plan Status (`production_plan.status`)
| Status | Description | Color |
|--------|-------------|-------|
| `draft` | Editable, not yet approved | Blue |
| `approved` | Locked for execution | Green |
| `executed` | Production orders created | Dark Green |
| `cancelled` | Cancelled | Grey |
| `revised` | Superseded by a newer revision | Orange |

### Plan Line Status (`production_plan_line.status`)
| Status | Description |
|--------|-------------|
| `planned` | Default, not yet started |
| `in_progress` | Production order created |
| `completed` | All job cards done |
| `cancelled` | Removed from plan |
| `deferred` | Waiting for material |

---

## Error Response Format

All errors return:
```json
{
  "detail": "Human-readable error message"
}
```

| HTTP Code | Meaning |
|-----------|---------|
| `400` | Bad request (e.g., editing non-draft plan, no fields to update) |
| `404` | Resource not found (plan, line, fulfillment record) |
| `500` | Server error (e.g., API key missing) |
