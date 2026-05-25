# Candor Foods — Complete API Reference

**Base URL:** `https://desktop-backend-vhf0.onrender.com` (or `http://localhost:8000`)
**Auth:** All endpoints require `Authorization: Bearer <session_token>` unless noted.
**Total Endpoints:** 149

---

## Table of Contents

1. [Fulfillment (18)](#1-fulfillment)
2. [Plans (5)](#2-plans)
3. [MRP (1)](#3-mrp)
4. [Indents (5)](#4-indents)
5. [Alerts (2)](#5-alerts)
6. [Production Orders (5)](#6-production-orders)
7. [Store Control (7)](#7-store-control)
8. [Job Cards — Listing (7)](#8-job-cards--listing)
9. [Job Cards — Lifecycle (12)](#9-job-cards--lifecycle)
10. [Job Cards — Annexure (5)](#10-job-cards--annexure)
11. [Job Cards — Output (3)](#11-job-cards--output)
12. [Inventory Batches (19)](#12-inventory-batches)
13. [Floor Inventory (6)](#13-floor-inventory)
14. [Off-Grade (4)](#14-off-grade)
15. [Loss & Yield (3)](#15-loss--yield)
16. [Day-End & Balance Scan (7)](#16-day-end--balance-scan)
17. [Discrepancy (4)](#17-discrepancy)
18. [AI Insights (2)](#18-ai-insights)
19. [Production Indents FG/SFG (8)](#19-production-indents-fgsfg)
20. [Lot Picker & Issuance (6)](#20-lot-picker--issuance)
21. [QC & RTV (5)](#21-qc--rtv)
22. [Amendments & Material Docs (5)](#22-amendments--material-docs)
23. [Webhooks (10)](#23-webhooks)
24. [WebSocket (2)](#24-websocket)

---

## 1. Fulfillment

### `GET /api/v1/production/health`
Health check for production module.

**Response:**
```json
{"status": "ok", "module": "production", "tables": {"so_fulfillment": 150, "production_plan": 12}}
```

---

### `POST /api/v1/production/fulfillment/sync`
Sync SO lines into fulfillment table. Idempotent.

**Request Body:**
```json
{"entity": "cfpl"}
```

**Response:**
```json
{"synced": 5, "skipped": 2, "total": 7}
```

---

### `GET /api/v1/production/fulfillment`
Paginated list with filters.

| Query Param | Type | Default | Description |
|-------------|------|---------|-------------|
| `entity` | string | null | Filter by entity |
| `status` | string | null | Comma-separated: `open,partial,fulfilled,cancelled` |
| `financial_year` | string | null | e.g. `2025-26` |
| `customer` | string | null | Partial match |
| `so_number` | string | null | Exact match |
| `article` | string | null | Partial match on fg_sku_name |
| `search` | string | null | Searches across multiple fields |
| `page` | int | 1 | Page number |
| `page_size` | int | 200 | Max 500 |

**Response:**
```json
{
  "results": [
    {
      "fulfillment_id": 1,
      "so_line_id": 10,
      "so_id": 5,
      "so_number": "5901310301",
      "so_date": "2026-04-10",
      "financial_year": "2025-26",
      "fg_sku_name": "Roasted Cashew 500g",
      "customer_name": "ABC Traders",
      "original_qty_kg": 500.0,
      "pending_qty_kg": 300.0,
      "produced_qty_kg": 200.0,
      "entity": "cfpl",
      "delivery_deadline": "2026-04-20",
      "priority": 5,
      "order_status": "partial"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 200,
    "total": 150,
    "total_pages": 1
  }
}
```

---

### `GET /api/v1/production/fulfillment/all`
Same filters, no pagination. Returns flat array.

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `status` | string | null |
| `financial_year` | string | null |
| `customer` | string | null |
| `so_number` | string | null |
| `article` | string | null |
| `search` | string | null |

**Response:** `[ {...fulfillment}, {...fulfillment}, ... ]`

---

### `GET /api/v1/production/fulfillment/demand-summary`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `financial_year` | string | null |

**Response:**
```json
[
  {
    "fg_sku_name": "Roasted Cashew 500g",
    "customer_name": "ABC Traders",
    "total_pending_kg": 1200.0,
    "order_count": 3,
    "earliest_deadline": "2026-04-15"
  }
]
```

---

### `GET /api/v1/production/fulfillment/chart-summary`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `financial_year` | string | null |
| `customer` | string | null |
| `so_number` | string | null |
| `article` | string | null |
| `status` | string | null |

**Response:** Aggregated chart data object.

---

### `GET /api/v1/production/fulfillment/filter-options`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `financial_year` | string | null |

**Response:**
```json
{
  "customers": ["ABC Traders", "XYZ Corp"],
  "so_numbers": ["5901310301", "5901310302"],
  "articles": ["Roasted Cashew 500g", "Almond 250g"]
}
```

---

### `GET /api/v1/production/fulfillment/customer-view`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `financial_year` | string | null |
| `customer` | string | null |

**Response:** Customer-grouped fulfillment with BOM details, process routes, floors, and inventory status.

---

### `GET /api/v1/production/fulfillment/fy-review`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `financial_year` | string | null |

**Response:** All unfulfilled orders for FY close review.

---

### `GET /api/v1/production/fulfillment/{fulfillment_id}/detail`

**Response:** Full detail with BOM lines, inventory, machines, SO link, revision log.

---

### `GET /api/v1/production/fulfillment/{fulfillment_id}/bom-override`

**Response:** Current overrides vs master BOM values.

---

### `PUT /api/v1/production/fulfillment/{fulfillment_id}/bom-override`

**Request Body:**
```json
{
  "overrides": [
    {
      "bom_line_id": 5,
      "material_sku_name": "Sugar",
      "quantity_per_unit": 0.5,
      "loss_pct": 2.0,
      "uom": "kg",
      "godown": "rm_store",
      "is_removed": false,
      "override_reason": "Customer spec change"
    }
  ],
  "overridden_by": "kaushal"
}
```

**Response:** `{saved: true, overrides_count: 1}`

---

### `GET /api/v1/production/fulfillment/{fulfillment_id}/floor-stock`

**Response:** Array of floor stock entries for this fulfillment.

---

### `PUT /api/v1/production/fulfillment/{fulfillment_id}/floor-stock`

**Request Body:**
```json
{
  "entries": [
    {
      "material_sku_name": "Box 500g",
      "item_type": "pm",
      "quantity_kg": 100.0,
      "unit": "KG",
      "floor_location": "pm_store",
      "notes": ""
    }
  ],
  "added_by": "kaushal"
}
```

---

### `GET /api/v1/production/floors`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |

**Response:** `["rm_store", "pm_store", "production_floor", "fg_store"]`

---

### `POST /api/v1/production/fulfillment/carryforward`

**Request Body:**
```json
{
  "fulfillment_ids": [1, 2, 3],
  "new_fy": "2026-27",
  "revised_by": "kaushal"
}
```

---

### `PUT /api/v1/production/fulfillment/{fulfillment_id}/revise`

**Request Body:**
```json
{
  "new_qty": 450.0,
  "new_date": "2026-04-25",
  "reason": "Customer revised order",
  "revised_by": "kaushal"
}
```

All fields optional (send only what changed).

---

### `POST /api/v1/production/fulfillment/cancel`

**Request Body:**
```json
{
  "fulfillment_ids": [1, 2],
  "reason": "Customer cancelled order",
  "cancelled_by": "kaushal"
}
```

**Response:** `{"cancelled": 2, "total_requested": 2}`

---

## 2. Plans

### `POST /api/v1/production/plans/create-with-ai`

**Request Body:**
```json
{
  "entity": "cfpl",
  "plan_type": "daily",
  "plan_date": "2026-04-15",
  "plan_name": "Daily Plan — Apr 15",
  "created_by": "kaushal",
  "selected_items": [
    {
      "fulfillment_id": 42,
      "custom_qty_kg": 500.0,
      "bom_overrides": [],
      "floors": ["Floor 1", "Floor 2"],
      "machines": {
        "Floor 1": ["Roaster A", "Packer B"],
        "Floor 2": ["Packer C"]
      }
    }
  ]
}
```

**Response:**
```json
{
  "plan_id": 10,
  "status": "draft",
  "lines": 3,
  "material_check": [...],
  "risk_flags": [...],
  "schedule": [...]
}
```

---

### `GET /api/v1/production/plans`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `status` | string | null |
| `plan_type` | string | null |
| `date_from` | string | null | YYYY-MM-DD |
| `date_to` | string | null | YYYY-MM-DD |
| `page` | int | 1 |
| `page_size` | int | 200 |

**Response:**
```json
{
  "results": [
    {
      "plan_id": 10,
      "plan_name": "Daily Plan — 2026-04-15",
      "entity": "cfpl",
      "plan_type": "daily",
      "plan_date": "2026-04-15",
      "status": "draft",
      "ai_generated": true,
      "approved_by": null,
      "created_at": "2026-04-15T10:00:00+00:00"
    }
  ],
  "pagination": {"page": 1, "page_size": 200, "total": 12, "total_pages": 1}
}
```

---

### `GET /api/v1/production/plans/all`
Same filters, no pagination. Returns flat array.

---

### `GET /api/v1/production/plans/{plan_id}`

**Response:** Plan detail with all lines, material check, risk flags.

---

### `GET /api/v1/production/plans/{plan_id}/revision-history`

**Response:** Chain of plan revisions.

---

## 3. MRP

### `GET /api/v1/production/mrp/availability`

| Query Param | Type | Required |
|-------------|------|----------|
| `material` | string | Yes |
| `qty` | float | Yes |
| `entity` | string | Yes |

**Response:**
```json
{
  "material": "Sugar",
  "needed_kg": 500.0,
  "on_hand_kg": 350.0,
  "on_order_kg": 200.0,
  "available_kg": 550.0,
  "shortage_kg": 0.0,
  "status": "SUFFICIENT"
}
```

---

## 4. Indents

### `GET /api/v1/production/indents`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `status` | string | null | Comma-separated: `draft,raised,acknowledged,po_created,received,cancelled` |
| `source` | string | null |
| `search` | string | null |
| `date_from` | string | null |
| `date_to` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

**Response:** Paginated indent list with `{results, pagination}`.

---

### `GET /api/v1/production/indents/all`
Same filters, no pagination. Returns flat array.

---

### `GET /api/v1/production/indents/{indent_id}`

**Response:** Indent detail with linked plan line info.

---

### `POST /api/v1/production/indents/raise`
Raise a purchase indent from the floor.

**Request Body:**
```json
{
  "material_sku_name": "Sugar",
  "material_type": "rm",
  "required_qty_kg": 200.0,
  "uom": "kg",
  "job_card_id": "45",
  "trigger_reason": "Insufficient stock",
  "entity": "cfpl"
}
```

**Response:** Created indent with ID.

---

## 5. Alerts

### `GET /api/v1/production/alerts`

| Query Param | Type | Default |
|-------------|------|---------|
| `target_team` | string | null | `purchase`, `stores`, `production`, `qc` |
| `is_read` | bool | null |
| `entity` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

**Response:** Paginated alerts with `{results, pagination}`.

---

### `PUT /api/v1/production/alerts/{alert_id}/read`

**Response:** `{"alert_id": 5, "is_read": true}`

---

## 6. Production Orders

### `POST /api/v1/production/orders/create-from-plan`

**Request Body:**
```json
{"plan_id": 10}
```

**Response:** `{"orders_created": 3, "orders": [...]}`

---

### `GET /api/v1/production/orders`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `status` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

**Response:** Paginated orders with `{results, pagination}`.

---

### `GET /api/v1/production/orders/all`
Same filters, no pagination. Returns flat array.

---

### `GET /api/v1/production/orders/{prod_order_id}`

**Response:** Production order detail with job cards.

---

### `GET /api/v1/production/orders/{prod_order_id}/job-card-chain`

**Response:**
```json
[
  {
    "job_card_id": 101,
    "job_card_number": "PRD-2026-0001/1",
    "step_number": 1,
    "process_name": "Sorting",
    "stage": "sorting",
    "status": "completed",
    "floor": "Floor 1",
    "carried_in_kg": 0,
    "dispatched_kg": 480.0
  }
]
```

---

## 7. Store Control

### `GET /api/v1/production/store/pending-allocations`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `job_card_id` | int | null |
| `material` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

**Response:** Paginated pending allocations.

---

### `GET /api/v1/production/store/pending-allocations/all`
Same filters, no pagination. Returns flat array.

---

### `GET /api/v1/production/store/dashboard`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Response:** Aggregated store dashboard stats.

---

### `POST /api/v1/production/store/decide`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Request Body:**
```json
{
  "decisions": [
    {
      "allocation_id": 45,
      "decision": "approved",
      "approved_qty": 500.0
    },
    {
      "allocation_id": 46,
      "decision": "rejected",
      "rejection_reason": "Stock not available",
      "raise_purchase_indent": true
    },
    {
      "allocation_id": 47,
      "decision": "partial",
      "approved_qty": 300.0,
      "rejected_qty": 200.0,
      "rejection_reason": "Only 300 kg available"
    }
  ],
  "decided_by": "store_manager"
}
```

**Response:** `{"processed": 3, "indents_raised": 1}`

---

### `POST /api/v1/production/store/verify-floor-stock`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Request Body:**
```json
{
  "job_card_id": 101,
  "verifications": [
    {"allocation_id": 45, "verified_qty": 500.0, "condition_notes": ""}
  ],
  "verified_by": "store_manager"
}
```

---

### `POST /api/v1/production/store/suggest-alternative`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Request Body:**
```json
{
  "allocation_id": 46,
  "offgrade_id": 12,
  "qty": 200.0,
  "suggested_by": "store_manager"
}
```

---

### `GET /api/v1/production/job-cards/{job_card_id}/allocations`

**Response:** Store allocation records for a job card.

---

## 8. Job Cards — Listing

### `GET /api/v1/production/job-cards`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `status` | string | null | Comma-separated: `locked,unlocked,assigned,material_received,in_progress,completed,closed` |
| `team_leader` | string | null |
| `floor` | string | null |
| `factory` | string | null |
| `stage` | string | null |
| `search` | string | null |
| `customer` | string | null |
| `article` | string | null |
| `date_from` | string | null |
| `date_to` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

**Response:**
```json
{
  "results": [
    {
      "job_card_id": 101,
      "job_card_number": "PRD-2026-0001/1",
      "fg_sku_name": "Roasted Cashew 500g",
      "customer_name": "ABC Traders",
      "batch_number": "B2026-0001",
      "batch_size_kg": 500.0,
      "step_number": 1,
      "process_name": "Sorting",
      "stage": "sorting",
      "status": "in_progress",
      "floor": "Floor 1",
      "factory": "W202",
      "entity": "cfpl",
      "machine_id": 5,
      "assigned_to_team_leader": "Ramesh",
      "start_time": "2026-04-15T08:00:00+00:00",
      "created_at": "2026-04-15T07:00:00+00:00"
    }
  ],
  "pagination": {"page": 1, "page_size": 200, "total": 45, "total_pages": 1},
  "filter_options": {
    "statuses": ["locked", "unlocked", "in_progress", "completed"],
    "floors": ["Floor 1", "Floor 2"],
    "stages": ["sorting", "roasting", "packaging"],
    "factories": ["W202"]
  }
}
```

---

### `GET /api/v1/production/job-cards/all`
Same filters, no pagination. Returns flat array.

---

### `GET /api/v1/production/job-cards/team-dashboard`

| Query Param | Type | Required |
|-------------|------|----------|
| `team_leader` | string | Yes |
| `entity` | string | No |

**Response:** Priority-sorted job cards for team leader.

---

### `GET /api/v1/production/job-cards/floor-dashboard`

| Query Param | Type | Required |
|-------------|------|----------|
| `floor` | string | Yes |
| `entity` | string | No |

**Response:** All job cards on a specific floor.

---

### `GET /api/v1/production/job-cards/{job_card_id}`

**Response:** Full job card detail including RM/PM indents, store allocations, team, output, QC, sign-offs, chain info.

---

### `GET /api/v1/production/job-cards/{job_card_id}/floor-stock-status`

**Response:**
```json
{
  "job_card_id": 101,
  "materials": [
    {
      "material_sku_name": "Raw Cashew",
      "item_type": "rm",
      "gross_requirement_kg": 525.0,
      "on_floor_kg": 600.0,
      "shortfall_kg": 0.0,
      "indent_status": "floor_available"
    }
  ],
  "all_available": true
}
```

---

### `GET /api/v1/production/job-cards/{job_card_id}/dispatch-log`

**Response:**
```json
[
  {
    "dispatch_id": 1,
    "qty_kg": 95.0,
    "dispatched_by": "Ramesh",
    "dispatched_at": "2026-04-15T14:30:00+00:00"
  }
]
```

---

## 9. Job Cards — Lifecycle

### `PUT /api/v1/production/job-cards/{job_card_id}/assign`

**Request Body:**
```json
{
  "team_leader": "Ramesh",
  "team_members": ["Suresh", "Mahesh"]
}
```

---

### `POST /api/v1/production/job-cards/{job_card_id}/receive-material`

**Request Body:**
```json
{
  "box_ids": ["BOX-001", "BOX-002", "BOX-003"]
}
```

**Response:** Material receipt result with total_kg.

---

### `POST /api/v1/production/job-cards/{job_card_id}/acknowledge-material`

**Request Body:**
```json
{
  "indent_lines": [
    {"indent_type": "rm", "indent_id": 5}
  ],
  "acknowledged_by": "store_manager"
}
```

Or acknowledge all (omit `indent_lines`):
```json
{
  "indent_lines": null,
  "acknowledged_by": "store_manager"
}
```

---

### `PUT /api/v1/production/job-cards/{job_card_id}/start`

**Request Body:** None.

**Response:** `{"job_card_id": 101, "status": "in_progress", "start_time": "..."}`

---

### `PUT /api/v1/production/job-cards/{job_card_id}/complete-step`

**Request Body:**
```json
{
  "step_number": 1,
  "operator_name": "Ramesh",
  "qc_passed": true
}
```

---

### `PUT /api/v1/production/job-cards/{job_card_id}/complete`

**Request Body:** None.

**Response:** `{"job_card_id": 101, "status": "completed", "duration_minutes": 145.5}`

---

### `PUT /api/v1/production/job-cards/{job_card_id}/sign-off`

**Request Body:**
```json
{
  "sign_off_type": "production_incharge",
  "name": "Kaushal"
}
```

`sign_off_type`: `production_incharge` | `quality_analysis` | `warehouse_incharge`

---

### `PUT /api/v1/production/job-cards/{job_card_id}/close`

**Request Body:** None. Requires all 3 sign-offs.

**Response:** `{"job_card_id": 101, "status": "closed"}`

---

### `PUT /api/v1/production/job-cards/{job_card_id}/force-unlock`

**Request Body:**
```json
{
  "authority": "Floor Manager",
  "reason": "Previous stage partial dispatch"
}
```

---

### `POST /api/v1/production/job-cards/{job_card_id}/dispatch-to-next`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Request Body:**
```json
{
  "qty_kg": 95.0,
  "dispatched_by": "Ramesh"
}
```

---

### `POST /api/v1/production/job-cards/generate`

**Request Body:**
```json
{"prod_order_id": 15}
```

**Response:** `{"prod_order_id": 15, "job_cards": [...]}`

---

## 10. Job Cards — Annexure

### `POST /api/v1/production/job-cards/{job_card_id}/environment`

**Request Body:**
```json
{
  "parameters": [
    {"parameter_name": "Brine Salinity", "value": "22%"},
    {"parameter_name": "Temperature", "value": "28C"},
    {"parameter_name": "Humidity", "value": "65%"}
  ]
}
```

**Response:** `{"saved": 3}`

---

### `POST /api/v1/production/job-cards/{job_card_id}/metal-detection`

**Request Body:**
```json
{
  "check_type": "pre_packaging",
  "fe_pass": true,
  "nfe_pass": true,
  "ss_pass": true,
  "failed_units": 0,
  "seal_check": true,
  "wt_check": true,
  "dough_temp_c": 28.5,
  "oven_temp_c": 180.0,
  "baking_temp_c": 165.0,
  "remarks": ""
}
```

**Response:** `{"detection_id": 12}`

---

### `POST /api/v1/production/job-cards/{job_card_id}/weight-checks`

**Request Body:**
```json
{
  "target_wt_g": 500.0,
  "tolerance_g": 10.0,
  "samples": [
    {"sample_number": 1, "net_weight": 502.3, "gross_weight": 515.0, "leak_test_pass": true},
    {"sample_number": 2, "net_weight": 498.1, "gross_weight": 511.0, "leak_test_pass": true}
  ]
}
```

**Response:** `{"saved": 2}`

---

### `POST /api/v1/production/job-cards/{job_card_id}/loss-reconciliation`

**Request Body:**
```json
{
  "entries": [
    {
      "loss_category": "process_loss",
      "budgeted_loss_pct": 2.0,
      "budgeted_loss_kg": 10.0,
      "actual_loss_kg": 8.5,
      "remarks": ""
    }
  ]
}
```

**Response:** `{"saved": 1, "total_budgeted_kg": 10.0, "total_actual_kg": 8.5}`

---

### `POST /api/v1/production/job-cards/{job_card_id}/remarks`

**Request Body:**
```json
{
  "remark_type": "observation",
  "content": "Machine running slow after 2 hours",
  "recorded_by": "Ramesh"
}
```

`remark_type`: `observation` | `deviation` | `corrective_action`

**Response:** `{"remark_id": 5}`

---

## 11. Job Cards — Output

### `POST /api/v1/production/job-cards/{job_card_id}/output`
V2 consolidated: FG output + byproducts + balance materials + QC in one call.

**Request Body:**
```json
{
  "fg_actual_kg": 480.0,
  "fg_actual_units": 960,
  "fg_expected_kg": 500.0,
  "fg_expected_units": 1000,
  "rm_consumed_kg": 525.0,
  "process_loss_kg": 20.0,
  "byproducts": [
    {"category": "tukda", "qty_kg": 5.0, "uom": "kg", "remarks": ""},
    {"category": "dust", "qty_kg": 2.0, "uom": "kg", "remarks": ""}
  ],
  "balance_materials": [
    {"material_name": "Raw Cashew", "balance_type": "RM", "qty_kg": 15.0, "remarks": "Returned"}
  ],
  "qc": {
    "passed": true,
    "remarks": "All parameters OK",
    "corrective_action": null,
    "inspector": "QC Team"
  }
}
```

**Response:**
```json
{
  "job_card_id": 101,
  "fg_actual_kg": 480.0,
  "yield_pct": 96.0,
  "net_output_kg": 473.0,
  "byproducts_saved": 2,
  "balance_materials_saved": 1
}
```

---

### `GET /api/v1/production/job-cards/{job_card_id}/output`

**Response:** Full output summary with byproducts, balance materials, loss recon, QC.

---

### `PUT /api/v1/production/job-cards/{job_card_id}/record-output`
(Deprecated V1 — use POST output instead)

**Request Body:**
```json
{
  "fg_expected_units": 1000,
  "fg_expected_kg": 500.0,
  "fg_actual_units": 960,
  "fg_actual_kg": 480.0,
  "rm_consumed_kg": 525.0,
  "process_loss_kg": 20.0
}
```

---

## 12. Inventory Batches

### `GET /api/v1/production/inventory/batches`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |
| `sku_name` | string | No |
| `status` | string | No |
| `floor_id` | string | No |
| `warehouse_id` | string | No |

---

### `GET /api/v1/production/inventory/batch/{batch_id}`

**Response:** Batch detail with history.

---

### `POST /api/v1/production/inventory/batch/{batch_id}/flag`

**Request Body:**
```json
{"reason": "Quality issue", "detail": "Moisture content high", "performed_by": "QC"}
```

---

### `POST /api/v1/production/inventory/batch/{batch_id}/block`

**Request Body:**
```json
{"so_id": 5, "blocked_by": "planner", "block_reason": "Reserved for SO"}
```

---

### `POST /api/v1/production/inventory/batch/{batch_id}/force-reassign`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Request Body:**
```json
{"new_so_id": 10, "override_by": "admin", "override_note": "Priority change"}
```

---

### `POST /api/v1/production/inventory/batch/{batch_id}/reject`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Request Body:**
```json
{
  "rejected_by": "QC",
  "reason_code": "QUALITY_ISSUE",
  "reason_text": "Failed moisture test"
}
```

`reason_code`: `QUALITY_ISSUE` | `CONTAMINATION` | `DAMAGED` | `PENDING_QC` | `OTHER`

---

### `POST /api/v1/production/inventory/batch/{batch_id}/resolve`

**Request Body:**
```json
{"resolution": "AVAILABLE", "resolved_by": "QC", "notes": "Re-tested, passed"}
```

`resolution`: `AVAILABLE` | `SCRAPPED`

---

### `POST /api/v1/production/inventory/batch/{batch_id}/return`

**Request Body:**
```json
{"qty_kg": 50.0, "return_reason": "Excess issued", "returned_by": "floor", "destination_floor": "rm_store"}
```

---

### `GET /api/v1/production/inventory/batch/{batch_id}/rejections`

**Response:** Array of rejection records.

---

### `POST /api/v1/production/inventory/legacy-import`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Request Body:**
```json
{
  "items": [
    {"sku_name": "Sugar", "item_type": "rm", "qty_kg": 1000.0, "warehouse_id": "W202", "floor_id": "rm_store"}
  ],
  "performed_by": "admin"
}
```

---

### `POST /api/v1/production/inventory/internal-issue`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Request Body:**
```json
{
  "sku_name": "Sugar",
  "batch_id": "B-001",
  "qty_kg": 100.0,
  "source_floor": "rm_store",
  "destination_floor": "production_floor",
  "purpose": "Production use",
  "requested_by": "floor_supervisor"
}
```

---

### `POST /api/v1/production/inventory/internal-issue/{note_id}/approve`

**Request Body:** `{"approved_by": "store_manager"}`

---

### `POST /api/v1/production/inventory/internal-issue/{note_id}/approve-constrained`

| Query Param | Type | Default |
|-------------|------|---------|
| `space_constrained` | bool | false |

**Request Body:** `{"approved_by": "store_manager"}`

---

### `POST /api/v1/production/inventory/internal-issue/{note_id}/reject`

**Request Body:** `{"rejected_by": "store_manager", "reason": "Not in stock"}`

---

### `GET /api/v1/production/inventory/internal-issues`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |
| `status` | string | No |

---

### `GET /api/v1/production/inventory/shortfall`

| Query Param | Type | Required |
|-------------|------|----------|
| `sku_name` | string | Yes |
| `required_qty` | float | Yes |
| `entity` | string | Yes |
| `so_id` | int | No |
| `job_card_id` | int | No |

---

### `GET /api/v1/production/inventory/reconcile`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

---

### `GET /api/v1/production/inventory/legacy-log`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

---

### `GET /api/v1/production/inventory/reconciliation-failures`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

---

## 13. Floor Inventory

### `GET /api/v1/production/floor-inventory`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |
| `floor_location` | string | No |
| `search` | string | No |
| `page` | int | 1 |
| `page_size` | int | 200 |

---

### `GET /api/v1/production/floor-inventory/summary`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

**Response:**
```json
[
  {"floor_location": "rm_store", "item_count": 25, "total_kg": 5000.0},
  {"floor_location": "production_floor", "item_count": 8, "total_kg": 1200.0}
]
```

---

### `POST /api/v1/production/floor-inventory/seed`

**Request Body:**
```json
{
  "entity": "cfpl",
  "items": [
    {"sku_name": "Sugar", "item_type": "rm", "floor_location": "rm_store", "quantity_kg": 1000.0}
  ],
  "overwrite": false
}
```

**Response:** `{"inserted": 5, "updated": 2, "total": 7}`

---

### `POST /api/v1/production/floor-inventory/move`

**Request Body:**
```json
{
  "sku_name": "Sugar",
  "from_location": "rm_store",
  "to_location": "production_floor",
  "quantity_kg": 250.0,
  "entity": "cfpl",
  "reason": "Production use",
  "moved_by": "store_manager"
}
```

---

### `GET /api/v1/production/floor-inventory/movements`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |
| `sku_name` | string | No |
| `from_location` | string | No |
| `to_location` | string | No |
| `date_from` | string | No |
| `date_to` | string | No |
| `job_card_id` | int | No |
| `page` | int | 1 |
| `page_size` | int | 200 |

---

### `POST /api/v1/production/floor-inventory/check-idle`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |

---

## 14. Off-Grade

### `GET /api/v1/production/offgrade/inventory`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `status` | string | `available` |
| `item_group` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

---

### `GET /api/v1/production/offgrade/rules`

**Response:** All reuse rules.

---

### `POST /api/v1/production/offgrade/rules/create`

**Request Body:**
```json
{"source_item_group": "cashew", "target_item_group": "cashew", "max_substitution_pct": 15.0, "notes": ""}
```

---

### `PUT /api/v1/production/offgrade/rules/{rule_id}`

**Request Body:**
```json
{"max_substitution_pct": 20.0, "is_active": true, "notes": "Updated"}
```

---

## 15. Loss & Yield

### `GET /api/v1/production/loss/analysis`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `product_name` | string | null |
| `stage` | string | null |
| `date_from` | string | null |
| `date_to` | string | null |
| `group_by` | string | `product` | `product` / `stage` / `month` / `machine` |

---

### `GET /api/v1/production/loss/anomalies`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `threshold_multiplier` | float | 2.0 |

---

### `GET /api/v1/production/yield/summary`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `product_name` | string | null |
| `period` | string | null |

---

## 16. Day-End & Balance Scan

### `GET /api/v1/production/day-end/summary`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |
| `target_date` | string | No | Defaults to today |

---

### `PUT /api/v1/production/day-end/dispatch`

**Request Body:**
```json
{
  "dispatches": [
    {"job_card_id": 101, "dispatch_qty": 480.0}
  ],
  "entity": "cfpl"
}
```

---

### `POST /api/v1/production/balance-scan/submit`

**Request Body:**
```json
{
  "floor_location": "rm_store",
  "entity": "cfpl",
  "submitted_by": "store_manager",
  "scan_lines": [
    {
      "sku_name": "Sugar",
      "item_type": "rm",
      "scanned_qty_kg": 980.0,
      "scanned_box_ids": ["B-001", "B-002"],
      "variance_reason": ""
    }
  ]
}
```

---

### `GET /api/v1/production/balance-scan/status`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |
| `target_date` | string | No |

**Response:**
```json
[
  {"floor_location": "rm_store", "submitted": true, "status": "submitted", "scan_id": 88},
  {"floor_location": "pm_store", "submitted": false, "status": "pending", "scan_id": null}
]
```

---

### `GET /api/v1/production/balance-scan/{scan_id}`

**Response:** Scan detail with all line items.

---

### `PUT /api/v1/production/balance-scan/{scan_id}/reconcile`

**Request Body:**
```json
{"reviewed_by": "store_manager"}
```

---

### `POST /api/v1/production/balance-scan/check-missing`

| Query Param | Type | Required |
|-------------|------|----------|
| `entity` | string | Yes |
| `target_date` | string | No |

---

## 17. Discrepancy

### `POST /api/v1/production/discrepancy/report`

**Request Body:**
```json
{
  "discrepancy_type": "rm_grade_mismatch",
  "severity": "major",
  "affected_material": "Sugar",
  "details": "Grade B received instead of Grade A",
  "reported_by": "QC",
  "entity": "cfpl"
}
```

`discrepancy_type`: `rm_grade_mismatch` | `qc_failure` | `machine_breakdown` | `contamination` | `short_delivery`
`severity`: `minor` | `major` | `critical`

---

### `GET /api/v1/production/discrepancy`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `status` | string | null |
| `discrepancy_type` | string | null |
| `severity` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

---

### `GET /api/v1/production/discrepancy/{discrepancy_id}`

**Response:** Discrepancy detail with affected job cards.

---

### `PUT /api/v1/production/discrepancy/{discrepancy_id}/resolve`

**Request Body:**
```json
{
  "resolution_type": "material_substituted",
  "resolution_details": "Used alternative supplier batch",
  "resolved_by": "production_manager"
}
```

`resolution_type`: `material_substituted` | `machine_rescheduled` | `deferred` | `cancelled_replanned` | `proceed_with_deviation`

---

## 18. AI Insights

### `GET /api/v1/production/ai/recommendations`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `recommendation_type` | string | null |
| `status` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

---

### `PUT /api/v1/production/ai/recommendations/{rec_id}/feedback`

**Request Body:**
```json
{"status": "accepted", "feedback": "Good suggestion"}
```

`status`: `accepted` | `rejected`

---

## 19. Production Indents FG/SFG

### `GET /api/v1/production/production-indents`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `status` | string | null |
| `search` | string | null |
| `date_from` | string | null |
| `date_to` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

---

### `GET /api/v1/production/production-indents/{indent_id}`

---

### `POST /api/v1/production/production-indents`

**Request Body:**
```json
{
  "item_description": "Roasted Cashew 500g",
  "material_type": "FG",
  "uom": "kg",
  "required_qty": 500.0,
  "available_qty": 200.0,
  "shortfall_qty": 300.0,
  "triggered_by_job_card": "PRD-2026-0001/3",
  "customer_name": "ABC Traders",
  "maker_user": "kaushal",
  "entity": "cfpl"
}
```

---

### `PUT /api/v1/production/production-indents/{indent_id}/submit`

---

### `PUT /api/v1/production/production-indents/{indent_id}/approve`

**Request Body:** `{"checker_user": "manager", "checker_comment": "Approved"}`

---

### `PUT /api/v1/production/production-indents/{indent_id}/return`

**Request Body:** `{"checker_user": "manager", "checker_comment": "Needs revision"}`

---

### `PUT /api/v1/production/production-indents/{indent_id}/cancel`

**Request Body:** `{"cancel_reason": "No longer needed"}`

---

### `POST /api/v1/production/production-indents/{indent_id}/create-internal-order`

---

## 20. Lot Picker & Issuance

### `GET /api/v1/production/lots`

| Query Param | Type | Default |
|-------------|------|---------|
| `item_description` | string | "" |
| `warehouse` | string | null |
| `job_card_id` | string | null |
| `entity` | string | "cfpl" |

---

### `GET /api/v1/production/lots/other-warehouses`

| Query Param | Type | Default |
|-------------|------|---------|
| `item_description` | string | "" |
| `exclude_warehouse` | string | null |
| `entity` | string | "cfpl" |

---

### `POST /api/v1/production/lots/fifo-skip`

**Request Body:**
```json
{
  "batch_id": "B-001",
  "job_card_id": "101",
  "reason": "Quality concern",
  "disposition": "leave_available",
  "skipped_by": "floor_supervisor"
}
```

`disposition`: `leave_available` | `block_for_so` | `flag_for_review`

---

### `POST /api/v1/production/lots/force-assign`

**Request Body:**
```json
{"batch_id": "B-001", "new_so_id": "SO-100", "override_comment": "Priority", "force_assigned_by": "admin"}
```

---

### `GET /api/v1/production/boxes/{box_id}`

**Response:** Box detail with batch info.

---

### `POST /api/v1/production/issue-notes`

**Request Body:**
```json
{
  "job_card_id": "101",
  "so_id": "SO-100",
  "customer_name": "ABC Traders",
  "issued_by": "store_manager",
  "lines": [
    {
      "sku": "Raw Cashew",
      "material_type": "RM",
      "lot_number": "L001",
      "lot_id": "B-001",
      "warehouse": "W202",
      "net_wt_issued": 250.0,
      "box_id": "BOX-001"
    }
  ]
}
```

---

## 21. QC & RTV

### `GET /api/v1/production/qc/queue`

**Response:** QC inspection queue with job card details.

---

### `PUT /api/v1/production/qc/inspections/{inspection_id}`

**Request Body:**
```json
{
  "result": "pass",
  "findings": "All parameters within range",
  "corrective_action": null,
  "inspector_user": "QC Inspector"
}
```

`result`: `pass` | `conditional_pass` | `fail`

---

### `GET /api/v1/production/rtv/dispositions`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | null |
| `status` | string | null |

---

### `POST /api/v1/production/rtv/dispositions`

**Request Body:**
```json
{"rtv_id": "RTV-001", "disposition_type": "return_to_vendor", "decided_by": "QC", "qc_remarks": "Failed specs"}
```

---

### `POST /api/v1/production/rtv/discard`

**Request Body:**
```json
{"rtv_id": "RTV-001", "reason": "Contaminated", "authorised_by": "manager"}
```

---

## 22. Amendments & Material Docs

### `GET /api/v1/production/amendments`

| Query Param | Type | Required |
|-------------|------|----------|
| `record_id` | string | Yes |
| `record_type` | string | Yes |
| `field` | string | No |

---

### `GET /api/v1/production/amendments/count`

| Query Param | Type | Required |
|-------------|------|----------|
| `record_id` | string | Yes |
| `record_type` | string | Yes |

**Response:** `{"count": 5}`

---

### `GET /api/v1/production/material-documents`

| Query Param | Type | Default |
|-------------|------|---------|
| `reference_type` | string | null |
| `reference_id` | string | null |
| `movement_type` | string | null |
| `date_from` | string | null |
| `date_to` | string | null |
| `page` | int | 1 |
| `page_size` | int | 200 |

---

### `GET /api/v1/production/material-documents/{mat_doc_id}/reconcile`

---

### `GET /api/v1/production/movement-types`

**Response:** Array of SAP-aligned movement type codes.

---

## 23. Webhooks

### `POST /api/v1/webhooks/endpoints`
Register a webhook URL. **Permission:** `production:webhooks:create`

**Request Body:**
```json
{"entity": "cfpl", "url": "https://example.com/webhook", "description": "Supplier notifications"}
```

**Response:**
```json
{"id": 1, "entity": "cfpl", "url": "https://example.com/webhook", "description": "Supplier notifications", "is_active": true, "created_at": "...", "secret": "a1b2c3...64hex"}
```

Note: `secret` only returned on creation.

---

### `GET /api/v1/webhooks/endpoints`
**Permission:** `production:webhooks:view`

| Query Param | Type | Default |
|-------------|------|---------|
| `entity` | string | "" |

---

### `PUT /api/v1/webhooks/endpoints/{endpoint_id}`
**Permission:** `production:webhooks:edit`

**Request Body:**
```json
{"url": "https://new-url.com/hook", "description": "Updated", "is_active": false}
```

---

### `DELETE /api/v1/webhooks/endpoints/{endpoint_id}`
Soft-delete. **Permission:** `production:webhooks:delete`

**Response:** `{"deactivated": true}`

---

### `POST /api/v1/webhooks/subscriptions`
**Permission:** `production:webhooks:create`

**Request Body:**
```json
{"endpoint_id": 1, "event_type": "indent.sent"}
```

Available event types:
```
fulfillment.synced, fulfillment.revised, plan.approved,
mrp.completed, mrp.shortage_detected,
indent.drafted, indent.sent, indent.bulk_sent, indent.raised,
job_card.created, job_card.started, job_card.completed,
job_card.team_assigned, job_card.material_received,
job_card.material_acknowledged, job_card.dispatched_to_next,
job_card.output_saved, job_card.signed_off, job_card.force_unlocked,
qc.passed, qc.failed, material.moved,
dayend.reconciled, dayend.discrepancy_found, store_alert.created,
* (wildcard)
```

---

### `GET /api/v1/webhooks/subscriptions`
**Permission:** `production:webhooks:view`

| Query Param | Type | Default |
|-------------|------|---------|
| `endpoint_id` | int | 0 |

---

### `DELETE /api/v1/webhooks/subscriptions/{sub_id}`
**Permission:** `production:webhooks:delete`

---

### `GET /api/v1/webhooks/deliveries`
**Permission:** `production:webhooks:view`

| Query Param | Type | Default |
|-------------|------|---------|
| `endpoint_id` | int | null |
| `event_type` | string | null |
| `status` | string | null | `pending`, `delivered`, `failed`, `exhausted` |
| `page` | int | 1 |
| `page_size` | int | 50 |

---

### `POST /api/v1/webhooks/deliveries/{delivery_id}/retry`
**Permission:** `production:webhooks:edit`

**Response:** `{"retried": true, "delivery_id": 1001}`

---

### `POST /api/v1/webhooks/test`
**Permission:** `production:webhooks:create`

| Query Param | Type | Description |
|-------------|------|-------------|
| `endpoint_id` | int | Test registered endpoint |
| `url` | string | Test arbitrary URL |

**Response:** `{"status_code": 200, "body": "OK"}` or `{"error": "ConnectTimeout"}`

---

## 24. WebSocket

### `POST /api/v1/ws/token`
Get a short-lived JWT for WebSocket connection.

**Response:**
```json
{"token": "eyJhbG...", "expires_in": 300}
```

---

### `WS /ws?token=<jwt>`
WebSocket connection for real-time events.

**Event frame format:**
```json
{
  "event_id": "uuid",
  "event_type": "job_card.started",
  "timestamp": "2026-04-15T10:30:00+00:00",
  "actor": "system",
  "data": { ... }
}
```

**Close codes:** `4001` = Invalid token, `4002` = Expired, `4003` = Not configured

**Role filtering:**

| Role | Receives |
|------|----------|
| `planner` | `plan.*`, `mrp.*`, `fulfillment.*` |
| `store_manager` | `indent.*`, `material.*`, `store_alert.*` |
| `floor_supervisor` | `job_card.*`, `qc.*`, `dayend.*` |
| `purchase` | `indent.*` |
| `admin` | All 25 events |
