# Production Planning Module — Frontend API Documentation

**Base URL:** `https://desktop-backend-el31.onrender.com`
**All production endpoints prefixed with:** `/api/v1/production`

> **Architecture note:** All planning actions (plan generation, approval, MRP, indents, revision) are performed exclusively via Claude Desktop through the MCP Planner server. The REST API is read-only for planning data — the frontend displays results; Claude creates them.

---

## Table of Contents

1. [Health Check](#1-health-check)
2. [SO Fulfillment (read + order management)](#2-so-fulfillment)
3. [Plans (read-only)](#3-plans)
4. [MRP Availability Check](#4-mrp-availability)
5. [Indents (read-only)](#5-indents)
6. [Alerts](#6-alerts)
7. [Production Orders & Job Cards](#7-production-orders--job-cards)
8. [Floor Inventory](#8-floor-inventory)
9. [Off-Grade](#9-off-grade)
10. [Loss & Yield](#10-loss--yield)
11. [Day-End & Balance Scan](#11-day-end--balance-scan)
12. [Discrepancy](#12-discrepancy)
13. [AI Recommendations](#13-ai-recommendations)
14. [MCP Planner Tools (Claude Desktop)](#14-mcp-planner-tools)
15. [Frontend Page → Endpoint Mapping](#15-frontend-page--endpoint-mapping)

---

## 1. Health Check

### `GET /health`

**Response:**
```json
{
  "status": "ok",
  "module": "production",
  "tables": {
    "bom_header": 1084,
    "bom_line": 4440,
    "machine": 86,
    "machine_capacity": 188,
    "so_fulfillment": 150,
    "production_plan": 5
  }
}
```

---

## 2. SO Fulfillment

### `GET /fulfillment`

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `entity` | string | null | `cfpl` or `cdpl` |
| `status` | string | null | `open,partial,fulfilled,carryforward,cancelled` |
| `financial_year` | string | null | e.g. `2025-26` |
| `customer` | string | null | Partial match |
| `search` | string | null | Searches sku_name + customer |
| `page` | int | 1 | |
| `page_size` | int | 50 | Max 200 |

**Response:**
```json
{
  "results": [
    {
      "fulfillment_id": 1,
      "fg_sku_name": "Cashew W320 250g",
      "customer_name": "D-Mart",
      "original_qty_kg": 500.0,
      "pending_qty_kg": 500.0,
      "produced_qty_kg": 0.0,
      "order_status": "open",
      "delivery_deadline": "2026-04-05",
      "priority": 5,
      "financial_year": "2025-26",
      "entity": "cfpl"
    }
  ],
  "pagination": { "page": 1, "page_size": 50, "total": 150, "total_pages": 3 }
}
```

---

### `GET /fulfillment/customer-view`

Customer-grouped view with BOM details, process route + floor mapping, and live inventory status. This is the **main planning screen** — each customer expands to articles, each article expands to show processes and materials.

**Query Parameters:** `entity`, `financial_year`, `customer` (partial match)

**Response:**
```json
{
  "customers": [
    {
      "customer_name": "D-Mart",
      "total_pending_kg": 2500.0,
      "order_count": 5,
      "earliest_deadline": "2026-04-05",
      "articles": [
        {
          "fulfillment_id": 1,
          "fg_sku_name": "Cashew W320 250g",
          "pending_qty_kg": 500.0,
          "delivery_deadline": "2026-04-05",
          "priority": 3,
          "bom_id": 17,
          "bom_found": true,
          "process_route": [
            { "step_number": 1, "process_name": "Sorting", "stage": "sorting", "floor": "W202 Ground", "std_time_min": 30.0, "loss_pct": 2.0 },
            { "step_number": 2, "process_name": "Packaging", "stage": "packaging", "floor": "W202 1st Floor", "std_time_min": 15.0, "loss_pct": 0.5 }
          ],
          "materials": [
            {
              "bom_line_id": 101,
              "material_sku_name": "Raw Cashew W320",
              "item_type": "rm",
              "quantity_per_unit": 1.05,
              "loss_pct": 2.0,
              "uom": "kg",
              "gross_requirement_kg": 535.7,
              "on_hand_kg": 2000.0,
              "status": "SUFFICIENT",
              "is_overridden": false
            }
          ],
          "has_overrides": false
        }
      ]
    }
  ],
  "summary": { "total_customers": 15, "total_articles": 42, "materials_with_shortage": 3 }
}
```

---

### `GET /fulfillment/{fulfillment_id}/bom-override`

Get current BOM overrides for a fulfillment. Shows master values alongside override values for comparison.

**Response:**
```json
{
  "fulfillment_id": 1,
  "bom_id": 17,
  "overrides": [
    {
      "bom_line_id": 101,
      "item_type": "rm",
      "master": { "material_sku_name": "Raw Cashew W320", "quantity_per_unit": 1.05, "loss_pct": 2.0, "uom": "kg" },
      "override": { "material_sku_name": null, "quantity_per_unit": 1.10, "loss_pct": 3.0, "uom": null, "is_removed": false, "reason": "Higher loss" },
      "has_override": true
    }
  ]
}
```

---

### `PUT /fulfillment/{fulfillment_id}/bom-override`

Save per-fulfillment BOM overrides. **Does NOT change the master BOM** — only this fulfillment uses the overridden values.

**Request Body:**
```json
{
  "overrides": [
    { "bom_line_id": 101, "quantity_per_unit": 1.10, "loss_pct": 3.0, "override_reason": "Higher loss expected" },
    { "bom_line_id": 102, "is_removed": true, "override_reason": "Not needed for this batch" }
  ],
  "overridden_by": "Planner Name"
}
```

**Response:** `{ "fulfillment_id": 1, "overrides_applied": 2 }`

**Override fields** (all optional — `null` = keep master value):
- `material_sku_name` — substitute material
- `quantity_per_unit` — adjusted quantity
- `loss_pct` — adjusted loss percentage
- `uom`, `godown` — adjusted unit/location
- `is_removed: true` — exclude this BOM line for this fulfillment only

---

### `GET /fulfillment/demand-summary`

Aggregated pending demand by product + customer.

**Query Parameters:** `entity`, `financial_year`

**Response:**
```json
[
  {
    "fg_sku_name": "Cashew W320 250g",
    "customer_name": "D-Mart",
    "total_qty_kg": 1500.0,
    "order_count": 3,
    "earliest_deadline": "2026-04-03"
  }
]
```

---

### `GET /fulfillment/fy-review`

All unfulfilled orders for FY transition review.

**Query Parameters:** `entity`, `financial_year`

---

### `POST /fulfillment/carryforward`

Bulk carry forward selected orders to a new FY.

**Request Body:**
```json
{
  "fulfillment_ids": [1, 5, 12],
  "new_fy": "2026-27",
  "revised_by": "Planner Name"
}
```

---

### `PUT /fulfillment/{fulfillment_id}/revise`

Revise qty or deadline.

**Request Body:**
```json
{
  "new_qty": 600.0,
  "new_date": "2026-04-10",
  "reason": "Customer increased order",
  "revised_by": "Planner Name"
}
```

---

### `POST /fulfillment/cancel`

Cancel selected fulfillment records.

**Request Body:**
```json
{
  "fulfillment_ids": [3, 7],
  "reason": "Customer cancelled",
  "cancelled_by": "Manager"
}
```

---

## 3. Plans

> Plans are **created and approved via MCP only**. These endpoints are for display.

### `GET /plans`

**Query Parameters:** `entity`, `status`, `plan_type`, `date_from`, `date_to`, `page`, `page_size`

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
      "status": "draft",
      "ai_generated": true,
      "revision_number": 1,
      "approved_by": null,
      "created_at": "2026-04-01T08:00:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 50, "total": 5, "total_pages": 1 }
}
```

---

### `GET /plans/{plan_id}`

Full plan with all lines, material check, and risk flags.

**Response:**
```json
{
  "plan_id": 1,
  "plan_name": "Daily Plan — 2026-04-01",
  "status": "draft",
  "lines": [
    {
      "plan_line_id": 1,
      "fg_sku_name": "Cashew W320 250g",
      "customer_name": "D-Mart",
      "planned_qty_kg": 500.0,
      "planned_qty_units": 2000,
      "machine_id": 3,
      "priority": 1,
      "shift": "day",
      "stage_sequence": ["sorting", "roasting", "packaging"],
      "estimated_hours": 5.0,
      "reasoning": "D-Mart deadline Apr 5, RM sufficient"
    }
  ],
  "material_check": [
    { "material": "Cashew 320 Raw", "needed_kg": 525, "available_kg": 2000, "status": "SUFFICIENT" }
  ],
  "risk_flags": [
    { "flag": "Roasting Machine at 90% capacity", "severity": "warning" }
  ]
}
```

---

### `GET /plans/{plan_id}/revision-history`

Chain of all revisions for a plan.

**Response:**
```json
{
  "plan_id": 3,
  "revision_chain": [
    { "plan_id": 1, "revision_number": 1, "status": "revised", "created_at": "..." },
    { "plan_id": 2, "revision_number": 2, "status": "revised", "created_at": "..." },
    { "plan_id": 3, "revision_number": 3, "status": "draft",   "created_at": "..." }
  ]
}
```

---

## 4. MRP Availability

### `GET /mrp/availability?material=Cashew+320+Raw&qty=500&entity=cfpl`

Quick single-material stock check.

**Response:**
```json
{
  "material": "Cashew 320 Raw",
  "required_kg": 500.0,
  "available_kg": 2000.0,
  "status": "SUFFICIENT",
  "gap_kg": 0
}
```

---

## 5. Indents

> Indents are **created, edited, and sent via MCP only**. These endpoints are for display.

### `GET /indents`

**Query Parameters:** `entity`, `status`, `date_from`, `date_to`, `page`, `page_size`

**Status values:** `draft`, `raised`, `acknowledged`, `po_created`, `received`

**Response:**
```json
{
  "results": [
    {
      "indent_id": 1,
      "material_name": "Cashew 320 Raw",
      "required_qty_kg": 500.0,
      "required_by_date": "2026-04-03",
      "status": "raised",
      "priority": 1,
      "entity": "cfpl",
      "created_at": "2026-04-01T09:00:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 50, "total": 8, "total_pages": 1 }
}
```

---

### `GET /indents/{indent_id}`

Indent detail with linked plan line info.

---

## 6. Alerts

### `GET /alerts`

**Query Parameters:** `target_team`, `is_read`, `entity`, `page`, `page_size`

**Response:**
```json
{
  "results": [
    {
      "alert_id": 1,
      "alert_type": "shortage",
      "message": "Cashew 320 Raw: 400 kg gap for plan #1",
      "target_team": "purchase",
      "is_read": false,
      "entity": "cfpl",
      "created_at": "2026-04-01T09:05:00Z"
    }
  ]
}
```

---

### `PUT /alerts/{alert_id}/read`

Mark an alert as read. **Response:** `{ "alert_id": 1, "is_read": true }`

---

## 7. Production Orders & Job Cards

### `POST /orders/create-from-plan`

Create production orders from an approved plan.

```json
{ "plan_id": 1 }
```

---

### `GET /orders`

**Query Parameters:** `entity`, `status`, `page`, `page_size`

---

### `GET /orders/{prod_order_id}`

Order detail with its job cards.

---

### `POST /job-cards/generate`

Generate sequential job cards for a production order.

```json
{ "prod_order_id": 1 }
```

---

### `GET /job-cards`

**Query Parameters:** `entity`, `status`, `team_leader`, `floor`, `stage`, `page`, `page_size`

---

### `GET /job-cards/team-dashboard?team_leader=Ravi&entity=cfpl`

Active job cards for a team leader, priority-sorted.

---

### `GET /job-cards/floor-dashboard?floor=W202+Ground&entity=cfpl`

All job cards on a specific floor.

---

### `GET /job-cards/{job_card_id}`

Full job card with all sections: RM/PM indent, process steps, output, sign-offs, annexures.

---

### Job Card Lifecycle

| Endpoint | Description |
|----------|-------------|
| `PUT /job-cards/{id}/assign` | Assign team leader + members |
| `POST /job-cards/{id}/receive-material` | Receive material via QR box scan |
| `PUT /job-cards/{id}/start` | Start production |
| `PUT /job-cards/{id}/complete-step` | Complete a process step |
| `PUT /job-cards/{id}/record-output` | Record FG output + losses |
| `PUT /job-cards/{id}/complete` | Mark job card complete |
| `PUT /job-cards/{id}/sign-off` | Add sign-off (QC, TL, FM) |
| `PUT /job-cards/{id}/close` | Close after all sign-offs |
| `PUT /job-cards/{id}/force-unlock` | Authority override to unlock next stage |

---

### Job Card Annexures

| Endpoint | Description |
|----------|-------------|
| `POST /job-cards/{id}/environment` | Annexure C — temperature/humidity |
| `POST /job-cards/{id}/metal-detection` | Annexure A/B — metal detection |
| `POST /job-cards/{id}/weight-checks` | Annexure B — 20-sample weight checks |
| `POST /job-cards/{id}/loss-reconciliation` | Annexure D — loss by category |
| `POST /job-cards/{id}/remarks` | Annexure E — deviations/actions |

---

## 8. Floor Inventory

### `GET /floor-inventory`

**Query Parameters:** `entity` (required), `floor_location`, `search`, `page`, `page_size`

**floor_location values:** `rm_store`, `pm_store`, `fg_store`, `production_floor`

---

### `GET /floor-inventory/summary`

Aggregated kg and item count per floor location.

**Response:**
```json
[
  { "floor_location": "rm_store", "item_count": 73, "total_kg": 244500.0 },
  { "floor_location": "pm_store", "item_count": 0,  "total_kg": 0.0 }
]
```

---

### `POST /floor-inventory/seed`

Manually seed opening stock (use for PM/FG stores not in Excel ingest).

```json
{
  "entity": "cfpl",
  "overwrite": false,
  "items": [
    { "sku_name": "Pouch 200g",    "item_type": "pm", "floor_location": "pm_store", "quantity_kg": 500, "uom": "pcs" },
    { "sku_name": "Carton 24x200g","item_type": "pm", "floor_location": "pm_store", "quantity_kg": 200, "uom": "pcs" }
  ]
}
```

---

### `POST /floor-inventory/move`

Manual material movement between locations.

```json
{
  "sku_name": "Cashew 320 Raw",
  "from_location": "rm_store",
  "to_location": "production_floor",
  "quantity_kg": 100.0,
  "entity": "cfpl",
  "reason": "production",
  "job_card_id": 5
}
```

---

### `GET /floor-inventory/movements`

Movement audit trail. **Query:** `entity`, `sku_name`, `from_location`, `to_location`, `date_from`, `date_to`, `job_card_id`, `page`, `page_size`

---

### `POST /floor-inventory/check-idle?entity=cfpl`

Trigger idle material check. Creates alerts for materials idle 3–5 days.

---

## 9. Off-Grade

| Endpoint | Description |
|----------|-------------|
| `GET /offgrade/inventory` | List off-grade stock. Query: `entity`, `status`, `item_group` |
| `GET /offgrade/rules` | List substitution rules |
| `POST /offgrade/rules/create` | Create substitution rule |
| `PUT /offgrade/rules/{id}` | Update rule (partial) |

---

## 10. Loss & Yield

### `GET /loss/analysis`

**Query Parameters:** `entity`, `product_name`, `stage`, `date_from`, `date_to`, `group_by` (`product`/`stage`/`month`/`machine`)

**Response:**
```json
[
  { "group_key": "Cashew W320 250g", "batch_count": 12, "avg_loss_pct": 2.1, "total_loss_kg": 45.2 }
]
```

---

### `GET /loss/anomalies?entity=cfpl&threshold_multiplier=2.0`

Batches with loss > 2× average.

---

### `GET /yield/summary`

**Query Parameters:** `entity`, `product_name`, `period`

---

## 11. Day-End & Balance Scan

### `GET /day-end/summary?entity=cfpl&target_date=2026-04-01`

Today's completed final-stage job cards with dispatch data.

---

### `PUT /day-end/dispatch`

Bulk update dispatch quantities.

```json
{
  "entity": "cfpl",
  "dispatches": [
    { "job_card_id": 5, "dispatch_qty": 480.0 }
  ]
}
```

---

### `POST /balance-scan/submit`

Submit a day-end physical count for a floor.

```json
{
  "floor_location": "rm_store",
  "entity": "cfpl",
  "submitted_by": "Store Manager",
  "scan_lines": [
    { "sku_name": "Cashew 320 Raw", "scanned_qty_kg": 1850.0, "variance_reason": null }
  ]
}
```

---

### `GET /balance-scan/status?entity=cfpl`

Scan submission status per floor for today.

---

### `GET /balance-scan/{scan_id}`

Scan detail with all line items and variance flags.

---

### `PUT /balance-scan/{scan_id}/reconcile`

Adjust floor_inventory to match physical count.

```json
{ "reviewed_by": "Stores Manager" }
```

---

### `POST /balance-scan/check-missing?entity=cfpl`

Check which floors haven't submitted scans. Creates alerts.

---

## 12. Discrepancy

| Endpoint | Description |
|----------|-------------|
| `POST /discrepancy/report` | Report a discrepancy. Auto-holds affected job cards |
| `GET /discrepancy` | List reports. Query: `entity`, `status`, `discrepancy_type`, `severity` |
| `GET /discrepancy/{id}` | Detail with affected job cards |
| `PUT /discrepancy/{id}/resolve` | Resolve with one of 5 resolution types |

**Resolution types:** `material_substituted`, `machine_rescheduled`, `deferred`, `cancelled_replanned`, `proceed_with_deviation`

---

## 13. AI Recommendations

### `GET /ai/recommendations`

**Query Parameters:** `entity`, `recommendation_type`, `status`, `page`, `page_size`

---

### `PUT /ai/recommendations/{rec_id}/feedback`

Accept or reject a recommendation.

```json
{ "status": "accepted", "feedback": "Implemented as suggested" }
```

---

## 14. MCP Planner Tools

All planning actions happen in Claude Desktop via the **Candor Planner MCP** server.

### Workflow

```
1. sync_fulfillment(entity="cfpl")
   → Pulls SO lines into so_fulfillment table

2. get_fulfillment_list(entity="cfpl", status="open,partial")
   → Returns open orders with fulfillment_ids

3. get_planning_context(entity="cfpl", fulfillment_ids=[1,2,5], target_date="2026-04-02")
   → Returns demand + BOMs + inventory + machines in one call

4. Claude analyzes context and builds schedule

5. save_production_plan(entity="cfpl", plan_type="daily", date_from="2026-04-02", ...)
   → Saves draft plan to DB → plan_id returned

6. get_plan_detail(plan_id=1)
   → Review lines, material check, risk flags

7. approve_plan(plan_id=1, approved_by="Planner Name")
   → Runs MRP, generates draft indents, sets status=approved

8. list_indents(entity="cfpl", status="draft")
   → Review auto-generated indents

9. edit_indent(indent_id=3, required_qty_kg=600, required_by_date="2026-04-05")
   → Adjust before sending

10. send_bulk_indents(indent_ids=[1,2,3])
    → Status: draft → raised. Creates purchase alerts.
```

### Full Tool List (Planner MCP)

| Tool | Action |
|------|--------|
| `ping` | Health check |
| `sync_fulfillment` | Sync SO lines to fulfillment |
| `get_fulfillment_list` | View open/partial orders |
| `get_demand_summary` | Aggregated demand by product |
| `get_planning_context` | Full context: demand + BOM + inventory + machines |
| `save_production_plan` | Save AI-generated plan to DB |
| `approve_plan` | Approve plan → triggers MRP + indent generation |
| `list_plans` | View plans list |
| `get_plan_detail` | View plan with lines |
| `check_material_availability` | Quick stock check for a material |
| `list_indents` | View purchase indents |
| `edit_indent` | Edit draft indent qty/date |
| `send_indent` | Send one indent (draft → raised) |
| `send_bulk_indents` | Send multiple indents at once |
| `get_bom_detail` | BOM materials + process route |
| `get_inventory` | Current floor stock |
| `get_machine_master` | Machine list with capacity |
| `get_fulfillment_list` | Open orders for planning |

---

## 15. Frontend Page → Endpoint Mapping

| Page | Endpoints |
|------|-----------|
| **Production Dashboard** | `GET /health`, `GET /fulfillment/demand-summary`, `GET /plans` |
| **Fulfillment List** | `GET /fulfillment`, `PUT /fulfillment/{id}/revise`, `POST /fulfillment/carryforward`, `POST /fulfillment/cancel` |
| **Customer View (Planning Screen)** | `GET /fulfillment/customer-view`, `GET /fulfillment/{id}/bom-override`, `PUT /fulfillment/{id}/bom-override` |
| **FY Close Review** | `GET /fulfillment/fy-review`, `POST /fulfillment/carryforward` |
| **Plan List** | `GET /plans` |
| **Plan Detail** | `GET /plans/{id}`, `GET /plans/{id}/revision-history` |
| **Indent List** | `GET /indents`, `GET /indents/{id}` |
| **Alerts Panel** | `GET /alerts`, `PUT /alerts/{id}/read` |
| **Order List** | `GET /orders`, `POST /orders/create-from-plan` |
| **Job Card List** | `GET /job-cards`, `POST /job-cards/generate` |
| **Job Card Detail** | `GET /job-cards/{id}` + all lifecycle + annexure endpoints |
| **Team Dashboard** | `GET /job-cards/team-dashboard` |
| **Floor Dashboard** | `GET /job-cards/floor-dashboard`, `GET /floor-inventory` |
| **Inventory** | `GET /floor-inventory`, `GET /floor-inventory/summary`, `POST /floor-inventory/seed`, `POST /floor-inventory/move` |
| **Loss Analysis** | `GET /loss/analysis`, `GET /loss/anomalies`, `GET /yield/summary` |
| **Day-End** | `GET /day-end/summary`, `PUT /day-end/dispatch`, `POST /balance-scan/submit`, `PUT /balance-scan/{id}/reconcile` |
| **Discrepancy** | `POST /discrepancy/report`, `GET /discrepancy`, `PUT /discrepancy/{id}/resolve` |
| **AI Insights** | `GET /ai/recommendations`, `PUT /ai/recommendations/{id}/feedback` |

---

## Status Reference

### Plan Status
| Status | Description |
|--------|-------------|
| `draft` | Created by MCP, awaiting approval |
| `approved` | Approved via MCP, MRP run, indents generated |
| `executed` | Production orders created |
| `cancelled` | Cancelled |
| `revised` | Superseded by a newer revision |

### Fulfillment Status
| Status | Description |
|--------|-------------|
| `open` | Not yet produced |
| `partial` | Partially produced |
| `fulfilled` | Fully produced + dispatched |
| `carryforward` | Moved to next FY |
| `cancelled` | Cancelled |

### Indent Status
| Status | Description |
|--------|-------------|
| `draft` | Auto-generated, not yet reviewed |
| `raised` | Sent to purchase team |
| `acknowledged` | Purchase confirmed |
| `po_created` | PO issued |
| `received` | Material received |

---

## Error Format

```json
{ "detail": "Human-readable error message" }
```

| Code | Meaning |
|------|---------|
| `400` | Bad request |
| `404` | Resource not found |
| `500` | Server error |
