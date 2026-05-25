# Job Card Module — Frontend Integration Spec

> Companion to `docs/JobCard-Endpoints.pdf`. Adds full request/response shapes, nullability rules, and the new PATCH/DELETE endpoints introduced 2026-05-07.

**Base path:** `/api/v1/production`
**Auth:** every request must carry `Authorization: Bearer <token>`
**Content-Type:** `application/json` for all bodies
**Conventions:**
- `R` = required (must be present, non-null)
- `O-null` = optional, may be omitted OR explicitly `null` (PATCH endpoints only — `null` writes NULL to the column)
- `O` = optional, may be omitted (will use server default)
- All timestamps are ISO 8601 in UTC unless noted
- All quantities are `kg` unless the field name says otherwise
- A `4xx` response body is always `{"detail": "<message>"}` from FastAPI

---

## Section 1 — Job Card List Screen
*Activity:* `JobCardListActivity.java` (Android) — paginated list with filter dropdowns

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/job-cards` | Paginated list with filters |
| GET | `/job-cards/all` | Same filters, no pagination (use sparingly — heavy) |
| GET | `/job-cards/team-dashboard` | Cards filtered by team-leader, priority-sorted |
| GET | `/job-cards/floor-dashboard` | Cards on a floor, filtered by stage |

### `GET /job-cards`

**Query params** (all optional):

| Param | Type | Notes |
|---|---|---|
| `entity` | string | `cfpl` or `cdpl` |
| `status` | string | Comma-separated list, e.g. `locked,assigned,in_progress` |
| `team_leader` | string | ILIKE substring match |
| `floor` | string | ILIKE substring match |
| `factory` | string | ILIKE substring match |
| `stage` | string | Exact match |
| `search` | string | Searches job_card_number, fg_sku_name, customer_name, batch_number |
| `customer` | string | ILIKE substring on customer_name |
| `article` | string | ILIKE substring on fg_sku_name |
| `date_from` | string (YYYY-MM-DD) | Filter by `created_at` |
| `date_to` | string (YYYY-MM-DD) | Filter by `created_at` |
| `page` | int | Default 1, ≥1 |
| `page_size` | int | Default 200, max 500 |
| `size` | int | Alias of page_size (backward compat) |
| `include_cancelled` | bool | **NEW.** Default `false` — hides soft-cancelled cards. Pass `true` for admin views |

**Response** `200`:

```json
{
  "results": [
    {
      "job_card_id": 42,
      "job_card_number": "PO-2026-0042/1",
      "prod_order_id": 17,
      "step_number": 1,
      "process_name": "Sorting",
      "stage": "sorting",
      "fg_sku_name": "Sliced Cranberries 100g",
      "customer_name": "Walmart India",
      "batch_number": "B-2026-0042",
      "batch_size_kg": 250.0,
      "assigned_to_team_leader": "Ramesh",
      "team_members": ["Suresh", "Mahesh"],
      "is_locked": false,
      "force_unlocked": false,
      "status": "in_progress",
      "start_time": "2026-05-07T10:30:00+00:00",
      "end_time": null,
      "total_time_min": null,
      "factory": "CFPL Plant 1",
      "floor": "Production Floor A",
      "entity": "cfpl",
      "store_allocation_status": "approved",
      "created_at": "2026-05-07T09:00:00+00:00"
    }
  ],
  "pagination": { "page": 1, "page_size": 200, "total": 542, "total_pages": 3 },
  "filter_options": {
    "customers":    ["Walmart India", "DMart", ...],
    "team_leaders": ["Ramesh", "Suresh", ...],
    "floors":       ["Production Floor A", ...],
    "factories":    ["CFPL Plant 1", ...],
    "stages":       ["sorting", "weighing", ...]
  }
}
```

**Nullability of result rows:**

| Field | Nullable in response? |
|---|---|
| `job_card_id`, `job_card_number`, `prod_order_id`, `step_number`, `process_name`, `stage`, `fg_sku_name`, `batch_number`, `status`, `created_at` | NEVER NULL |
| `customer_name`, `batch_size_kg`, `assigned_to_team_leader`, `team_members`, `factory`, `floor`, `start_time`, `end_time`, `total_time_min`, `entity`, `store_allocation_status` | MAY be null |
| `is_locked`, `force_unlocked` | NEVER NULL (boolean) |

### Frontend prompt — Section 1

> Build a paginated job-card list screen using `GET /api/v1/production/job-cards`. The screen has two regions: a filter bar (status multi-select, team_leader autocomplete, floor/factory selects, stage select, customer/article search, date-from/date-to range, search box for free-text, an "Include cancelled" toggle that adds `include_cancelled=true`), and a results table with columns: Job Card #, Stage, Customer, Batch #, FG SKU, Floor, Status, Created At. Populate the filter dropdowns from `filter_options` in the response. Each row click navigates to the Detail screen. Handle empty `results` with a "No matching job cards" empty state. Show `pagination.total` and standard pager controls. Status field renders as a colored chip (green=in_progress, blue=assigned, yellow=locked, grey=cancelled, etc.). Cells `customer_name`, `floor`, `assigned_to_team_leader` may be null — render as `—`. Bearer token in header.

---

## Section 2 — Job Card Detail – Header Actions
*Activity:* `JobCardDetailActivity.java` — the umbrella screen with all tabs

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/job-cards/{job_card_id}` | Full detail (drives every tab) |
| PUT | `/job-cards/{job_card_id}/start` | Mark production started (no body) |
| PUT | `/job-cards/{job_card_id}/complete` | Mark production completed (no body) |
| PUT | `/job-cards/{job_card_id}/close` | Final close after sign-offs (no body) |
| PUT | `/job-cards/{job_card_id}/force-unlock` | Override stage-chain lock |
| PATCH | `/job-cards/{job_card_id}` | **NEW.** Partial update of editable header fields |
| DELETE | `/job-cards/{job_card_id}` | **NEW.** Soft cancel with reason |

### `GET /job-cards/{job_card_id}`

**Path params:** `job_card_id: int`
**Query params:** none
**Request body:** none

**Response** `200` — large nested object structured into 6 sections + 5 annexures:

```json
{
  "job_card_id": 42,
  "job_card_number": "PO-2026-0042/1",
  "prod_order_id": 17,
  "step_number": 1,
  "total_stages": 4,
  "process_name": "Sorting",
  "stage": "sorting",

  "section_1_product": {
    "customer_name": "Walmart India",
    "fg_sku_name": "Sliced Cranberries 100g",
    "bu": "DryFruits",
    "quantity_units": 2500,
    "batch_number": "B-2026-0042",
    "article_code": "WMT-CR-100",
    "mrp": 199.0,
    "ean": "8901234567890",
    "best_before": "2027-05-07",
    "factory": "CFPL Plant 1",
    "floor": "Production Floor A",
    "batch_size_kg": 250.0,
    "net_wt_per_unit": 0.1,
    "expected_units": 2500,
    "shelf_life_days": 365,
    "sales_order_ref": "CF-SO/26-27/130"
  },

  "section_2a_rm_indent": [
    {
      "rm_indent_id": 101,
      "job_card_id": 42,
      "material_sku_name": "Cranberry whole 5kg",
      "required_qty_kg": 270.0,
      "issued_qty_kg": 0.0,
      "acknowledged_qty_kg": 0.0,
      "status": "pending",
      "available_batches": [
        { "batch_id": "B-IB-001", "lot_number": "L-001", "inward_date": "2026-04-01",
          "expiry_date": "2027-04-01", "current_qty_kg": 500.0, "warehouse_id": "WH-1",
          "floor_id": "rm_store", "status": "AVAILABLE", "ownership": "company" }
      ]
    }
  ],

  "section_2b_pm_indent": [/* same shape as 2a, for PM materials */],

  "section_3_team": {
    "team_leader": "Ramesh",
    "team_members": ["Suresh", "Mahesh"],
    "batch_number": "B-2026-0042",
    "start_time": "2026-05-07T10:30:00+00:00",
    "end_time": null,
    "total_time_min": null,
    "fumigation": false,
    "metal_detector_used": true,
    "roasting_pasteurization": false,
    "control_sample_gm": 50.0,
    "magnets_used": false
  },

  "section_4_process_steps": [
    { "step_id": 1, "job_card_id": 42, "step_number": 1, "step_name": "Initial sort",
      "operator_name": "Suresh", "qc_passed": null, "status": "in_progress",
      "started_at": "...", "completed_at": null }
  ],

  "section_5_output": null,            /* or a job_card_output row once recorded */
  "section_6_sign_offs": [/* signoff rows */],

  "annexure_a_b_metal_detection": [/* job_card_metal_detection rows, deleted hidden */],
  "annexure_b_weight_checks":     [/* job_card_weight_check rows, deleted hidden */],
  "annexure_c_environment":       [/* job_card_environment rows, deleted hidden */],
  "annexure_d_loss_reconciliation":[/* job_card_loss_reconciliation rows, deleted hidden */],
  "annexure_e_remarks":           [/* job_card_remarks rows, deleted hidden */],

  "status": "in_progress",
  "is_locked": false,
  "locked_reason": null,
  "force_unlocked": false,
  "store_allocation_status": "approved",
  "entity": "cfpl",
  "created_at": "2026-05-07T09:00:00+00:00",

  "batch_size_kg": 250.0,
  "prev_job_card_id": null,
  "next_job_card_id": 43,
  "carried_qty_kg": 0.0,
  "dispatched_to_next_kg": 0.0
}
```

**Nullability inside the detail response:**

- `section_1_product`: `customer_name`, `bu`, `article_code`, `mrp`, `ean`, `best_before`, `floor`, `factory`, `net_wt_per_unit`, `shelf_life_days`, `sales_order_ref` may be null. Others non-null.
- `section_3_team`: `team_leader`, `team_members`, `start_time`, `end_time`, `total_time_min`, `control_sample_gm` may be null. Booleans (`fumigation`, etc.) default to `false`.
- `section_5_output` is `null` until recorded.
- All annexure arrays default to `[]` (empty array, never null).
- `prev_job_card_id`, `next_job_card_id`, `locked_reason` may be null.

**Errors:** `404` if not found OR soft-cancelled.

### `PUT /job-cards/{job_card_id}/start` and `/complete` and `/close`

**Request:** no body. Just `Authorization` header.
**Response** `200`: `{"ok": true, ...}` with state update info. Backend emits `job_card.started`/`job_card.completed`/etc events.
**Errors:** `404` not found, `409` if state transition is invalid.

### `PUT /job-cards/{job_card_id}/force-unlock`

**Request body:**
```json
{
  "authority": "string (R)",
  "reason":    "string (R)"
}
```
**Response** `200`: `{"ok": true, "status": "..."}`

### `PATCH /job-cards/{job_card_id}` *(NEW)*

**Partial update of editable header fields.** Only fields supplied are written; all other columns retain their values. Sending `{}` returns `422`.

**Request body** — all field-level fields are optional, `updated_by` is required:

| Field | Type | Required? | Notes |
|---|---|---|---|
| `machine_id` | int | O-null | FK to machine table |
| `assigned_to_team_leader` | string | O-null | |
| `team_members` | string[] | O-null | Replaces the entire array |
| `factory` | string | O-null | |
| `floor` | string | O-null | |
| `customer_name` | string | O-null | |
| `batch_number` | string | O-null | |
| `batch_size_kg` | number | O-null | Must be > 0 if supplied |
| `bom_id` | int | O-null | |
| `process_name` | string | O-null | |
| `stage` | string | O-null | |
| `updated_by` | string | **R** | Audit field — who is making the edit |

```json
{
  "floor": "Production Floor B",
  "team_members": ["Suresh", "Mahesh", "Naresh"],
  "updated_by": "alice"
}
```

**Response** `200`:
```json
{
  "ok": true,
  "job_card": { /* full updated job_card row */ },
  "changed_fields": ["floor", "team_members"]
}
```

**Errors:**
- `404` — JC not found / already cancelled
- `409` — status is `completed`, `closed`, or `cancelled` (not editable)
- `422` — body has no editable fields, OR `batch_size_kg ≤ 0`, OR `updated_by` missing

**Important:** sending `{"floor": null, "updated_by": "alice"}` is DIFFERENT from `{"updated_by": "alice"}`:
- The first writes `NULL` to `floor` (intentional clear)
- The second leaves `floor` untouched

### `DELETE /job-cards/{job_card_id}` *(NEW)*

**Soft-delete with cancellation reason.** Allowed only when status ∈ `{locked, unlocked, assigned}`.

**Request body:**

| Field | Type | Required? | Notes |
|---|---|---|---|
| `cancellation_reason` | string | **R** | Min 3 chars |
| `deleted_by` | string | **R** | |

```json
{
  "cancellation_reason": "Wrong customer assigned at creation",
  "deleted_by": "alice"
}
```

**Response** `200`:
```json
{
  "ok": true,
  "job_card": { /* row with deleted_at, deleted_by, cancellation_reason set, status='cancelled' */ }
}
```

**Errors:**
- `404` — JC not found / already cancelled
- `409` — status is past pre-start (e.g. `material_received`, `in_progress`). Use force-unlock + close instead
- `422` — `cancellation_reason` < 3 chars or missing

### Frontend prompt — Section 2

> Build the Detail screen umbrella. On mount, GET `/job-cards/{id}` and store the response. Render header strip with: job_card_number (large), Stage X of total_stages, status chip, customer_name, fg_sku_name, batch_number. Show action buttons that gate on `status` and `is_locked`:
> - **Start** (PUT `/start`) — visible when `status='assigned'` AND `is_locked=false`
> - **Complete** (PUT `/complete`) — visible when `status='in_progress'`
> - **Close** (PUT `/close`) — visible when sign-offs are recorded and status='completed'
> - **Force unlock** (PUT `/force-unlock`, prompts for authority + reason) — visible only to admin role when `is_locked=true`
> - **Edit header** (PATCH `/job-cards/{id}`) — opens a dialog to change `machine_id`, `team_leader`, `team_members`, `factory`, `floor`, `customer_name`, `batch_number`, `batch_size_kg`, `bom_id`, `process_name`, `stage`. Disabled when status ∈ {completed, closed, cancelled}. Send only the changed fields plus `updated_by` (current user).
> - **Cancel** (DELETE `/job-cards/{id}`) — opens a confirmation dialog asking for cancellation_reason. Only visible when status ∈ {locked, unlocked, assigned}.
>
> Cache the response in app state — every tab reads from this single source. Refetch on tab activation OR after any mutation. On 404, show "Job card not found"; on 409 from any action, show the response detail in a toast.

---

## Section 3 — Overview Tab
*Fragment:* `OverviewFragment.java`

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| PUT | `/job-cards/{job_card_id}/assign` | Assign team leader + members |
| PATCH | `/job-cards/{job_card_id}` | **NEW.** Edit header (covered in Section 2) |

### `PUT /job-cards/{job_card_id}/assign`

**Request body:**

| Field | Type | Required? | Notes |
|---|---|---|---|
| `team_leader` | string | **R** | |
| `team_members` | string[] | O | Defaults to `[]` if omitted |

```json
{
  "team_leader": "Ramesh",
  "team_members": ["Suresh", "Mahesh"]
}
```

**Response** `200`: `{"ok": true, "status": "assigned"}` (status transitions from `unlocked` → `assigned`).
**Errors:** `404` not found.

Backend emits `job_card.team_assigned` event with `team_leader` and `member_count`.

### Frontend prompt — Section 3

> Build the Overview tab content area: rendered fields from `section_1_product` and `section_3_team`. Two action buttons:
> - **Assign Team** (PUT `/assign`): opens a form with team_leader (autocomplete from a users API), team_members (multi-select chips). Disable button when status is past `unlocked` (already assigned/in-progress/etc).
> - **Edit Details** (PATCH `/job-cards/{id}`, see Section 2): opens an edit dialog for non-team header fields like floor, factory, customer_name, batch_size_kg.
>
> Display fields: customer_name, fg_sku_name, batch_number, factory, floor, batch_size_kg, expected_units, MRP, EAN, article_code, sales_order_ref, best_before. Render booleans (fumigation, metal_detector_used, roasting_pasteurization, magnets_used) as small toggle-icons. Show `team_leader` and `team_members` as chips below action buttons.

---

## Section 4 — Stage Chain Tab
*Fragment:* `StageChainFragment.java`

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/orders/{prod_order_id}/job-card-chain` | Stage progression for a production order |

### `GET /orders/{prod_order_id}/job-card-chain`

**Path params:** `prod_order_id: int`

**Response** `200`:

```json
{
  "job_cards": [
    {
      "job_card_id": 42,
      "job_card_number": "PO-2026-0042/1",
      "step_number": 1,
      "process_name": "Sorting",
      "stage": "sorting",
      "status": "in_progress",
      "is_locked": false,
      "carried_qty_kg": 250.0,
      "dispatched_to_next_kg": 100.0,
      "prev_job_card_id": null,
      "next_job_card_id": 43
    }
    /* ... ordered by step_number */
  ]
}
```

**Errors:** `404` if no job cards for that order.

### Frontend prompt — Section 4

> Build a horizontal stage-chain visualization using GET `/orders/{prod_order_id}/job-card-chain` (the parent prod_order_id comes from the cached job-card detail). Render as a connected timeline: one node per step, showing step_number, process_name, stage, status (color-coded), and small qty indicator: `carried_qty_kg → dispatched_to_next_kg`. Highlight the current job_card_id with a thick border. Tap any other node to navigate to that job card. Show `is_locked=true` nodes with a lock icon. Use prev/next links to draw arrows between nodes.

---

## Section 5 — Materials Tab
*Fragment:* `MaterialsFragment.java`

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/orders/{prod_order_id}/job-card-chain` | Determine "next stage" for dispatch |
| POST | `/job-cards/{job_card_id}/receive-material` | QR scan receive |
| POST | `/job-cards/{job_card_id}/acknowledge-material` | Manual acknowledge (no QR) |
| POST | `/indents/raise` | Raise an indent for short material |
| GET | `/job-cards/{job_card_id}/floor-stock-status` | Floor-stock balance per material |
| POST | `/job-cards/{job_card_id}/dispatch-to-next` | Push qty to next stage (?entity=) |
| GET | `/job-cards/{job_card_id}/dispatch-log` | Outbound dispatch log |
| GET | `/job-cards/{job_card_id}/allocations` | Store allocation records |

### `POST /job-cards/{job_card_id}/receive-material`

**Request body:**
```json
{ "box_ids": ["BX-001", "BX-002"] }
```

| Field | Type | Required? | Notes |
|---|---|---|---|
| `box_ids` | string[] | **R** | At least 1 box ID |

**Response** `200`:
```json
{
  "ok": true,
  "boxes_accepted": 2,
  "total_kg": 50.0,
  "matched_indent_lines": [/* updated rm_indent / pm_indent rows */]
}
```

### `POST /job-cards/{job_card_id}/acknowledge-material`

**Request body:**
```json
{
  "indent_lines": null,
  "acknowledged_by": "alice"
}
```

| Field | Type | Required? | Notes |
|---|---|---|---|
| `indent_lines` | int[] OR null | **R (may be explicit null)** | `null` means "acknowledge all pending indents". Otherwise: list of rm_indent_id / pm_indent_id values |
| `acknowledged_by` | string | **R** | |

**Response** `200`: `{"ok": true, "lines_acknowledged": <int>}`

### `POST /indents/raise`

**Request body:**

| Field | Type | Required? | Notes |
|---|---|---|---|
| `material_sku_name` | string | **R** | |
| `item_category` | string | O | |
| `material_type` | string | **R** | `"RM"` or `"PM"` |
| `required_qty_kg` | number | **R** | > 0 |
| `uom` | string | O | Defaults to `"kg"` |
| `job_card_id` | int OR string | O | Source job card if applicable |
| `customer_name` | string | O | |
| `so_reference` | string | O | |
| `trigger_reason` | string | O | Defaults to `"Insufficient stock"` |
| `entity` | string | **R** | `cfpl` or `cdpl` |

**Response** `200`: `{"ok": true, "indent_id": <int>}`. Backend emits `indent.raised` event.

### `GET /job-cards/{job_card_id}/floor-stock-status`

**Response** `200`:
```json
{
  "job_card_id": 42,
  "fg_sku_name": "Sliced Cranberries 100g",
  "materials": [
    {
      "material_sku_name": "Cranberry whole 5kg",
      "material_type": "RM",
      "required_qty_kg": 270.0,
      "available_qty_kg": 320.0,
      "shortage_kg": 0.0,
      "indent_status": "fulfilled"
    }
  ]
}
```

### `POST /job-cards/{job_card_id}/dispatch-to-next`

**Query params:** `entity=cfpl` (or `cdpl`) — required

**Request body:**
```json
{
  "qty_kg": 50.0,
  "dispatched_by": "alice"
}
```

| Field | Type | Required? | Notes |
|---|---|---|---|
| `qty_kg` | number | **R** | > 0 |
| `dispatched_by` | string | O | |

**Response** `200`:
```json
{ "ok": true, "dispatch_id": 17, "from_job_card_id": 42, "to_job_card_id": 43 }
```

Backend emits `job_card.dispatched_to_next`.

### `GET /job-cards/{job_card_id}/dispatch-log`

**Response** `200`:
```json
{
  "log": [
    {
      "dispatch_id": 17,
      "from_job_card_id": 42,
      "to_job_card_id": 43,
      "qty_kg": 50.0,
      "dispatched_by": "alice",
      "dispatched_at": "2026-05-07T11:30:00+00:00"
    }
  ]
}
```

### Frontend prompt — Section 5

> Build the Materials tab. Show two parallel sections:
>
> **RM Indent table** (from `section_2a_rm_indent`): columns: Material, Required, Issued, Acknowledged, Status. Each row has an action: "Raise Indent" (POST `/indents/raise` with material_type='RM', required_qty_kg=row.shortfall, entity from auth) when shortage > 0; "Acknowledge" (POST `/acknowledge-material` with that indent line id) when issued > 0 and acknowledged < issued.
>
> **PM Indent table**: same as RM but for `section_2b_pm_indent` and `material_type='PM'`.
>
> **Floor Stock panel** (right side): GET `/floor-stock-status` and render shortage indicators (red bar for negative, green for positive).
>
> **Header actions:**
> - **QR Scan** button — opens camera, scans box codes, accumulates a list, submits POST `/receive-material` with `{box_ids}`. Show toast with `boxes_accepted` and `total_kg`.
> - **Acknowledge All** button — POST `/acknowledge-material` with `indent_lines: null` and `acknowledged_by` = current user.
> - **Dispatch to Next** button — opens a numeric input for qty_kg (max = `batch_size_kg - dispatched_to_next_kg` from job card detail). Submits POST `/dispatch-to-next?entity=<entity>` with `{qty_kg, dispatched_by}`. Disable when `next_job_card_id` is null. After success, refetch detail and show in dispatch log.
> - **Dispatch Log** drawer — opens a side panel listing GET `/dispatch-log` rows.
>
> Block all material actions when `is_locked=true` unless force_unlocked is true (show a banner explaining why locked).

---

## Section 6 — Output Accounting Tab
*Fragment:* `OutputAccountingFragment.java`

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/job-cards/{job_card_id}/output` | Read previously saved V2 output |
| POST | `/job-cards/{job_card_id}/output` | Save full V2 output (FG + byproducts + balance materials + QC) |

### `GET /job-cards/{job_card_id}/output`

**Response** `200`:
```json
{
  "output": {
    "output_id": 1,
    "job_card_id": 42,
    "fg_actual_kg": 240.0,
    "fg_actual_units": 2400,
    "fg_expected_kg": 250.0,
    "fg_expected_units": 2500,
    "rm_consumed_kg": 270.0,
    "process_loss_kg": 30.0,
    "yield_pct": 96.0,
    "recorded_at": "2026-05-07T13:00:00+00:00"
  },
  "byproducts": [
    { "category": "off_grade", "quantity_kg": 5.0, "uom": "kg", "remarks": "Sorted out" }
  ],
  "balance_materials": [
    { "material_id": 7, "material_name": "Cranberry whole 5kg",
      "balance_type": "left_over", "qty_kg": 10.0, "remarks": "Returned to RM store" }
  ],
  "loss_reconciliation": [
    { "recon_id": 1, "loss_category": "sorting_rejection", "budgeted_loss_pct": 5.0,
      "budgeted_loss_kg": 13.5, "actual_loss_kg": 12.0, "variance_kg": -1.5, "remarks": null }
  ],
  "qc": {
    "result": "pass",
    "findings": null,
    "corrective_action": null,
    "inspector_user": "qc_alice",
    "inspection_date": "2026-05-07T13:00:00+00:00"
  }
}
```

`output` is `null` until first save. `byproducts`, `balance_materials`, `loss_reconciliation` default to `[]`. `qc` is null until QC is recorded.

### `POST /job-cards/{job_card_id}/output`

**Request body** — saves the entire output payload in one call:

| Field | Type | Required? | Notes |
|---|---|---|---|
| `fg_actual_kg` | number | **R** | ≥ 0 |
| `fg_actual_units` | int | **R** | ≥ 0 |
| `fg_expected_kg` | number | O | Defaults from BOM |
| `fg_expected_units` | int | O | |
| `rm_consumed_kg` | number | O | |
| `process_loss_kg` | number | O | |
| `byproducts` | array of `{category, qty_kg, uom?, remarks?}` | O | Defaults to `[]` |
| `balance_materials` | array of `{material_id, material_name, balance_type, qty_kg, remarks?}` | O | `balance_type` ∈ `{left_over, extra_given, ...}` |
| `qc` | `{passed: bool, remarks?, corrective_action?, inspector?}` | O | Recorded if present |

```json
{
  "fg_actual_kg": 240.0,
  "fg_actual_units": 2400,
  "fg_expected_kg": 250.0,
  "fg_expected_units": 2500,
  "rm_consumed_kg": 270.0,
  "process_loss_kg": 30.0,
  "byproducts": [
    { "category": "off_grade", "qty_kg": 5.0, "uom": "kg", "remarks": "Sorted out" }
  ],
  "balance_materials": [
    { "material_id": 7, "material_name": "Cranberry whole 5kg",
      "balance_type": "left_over", "qty_kg": 10.0, "remarks": "Returned to RM store" }
  ],
  "qc": {
    "passed": true, "remarks": "All within spec",
    "corrective_action": "", "inspector": "qc_alice"
  }
}
```

**Response** `200`:
```json
{
  "ok": true,
  "fg_actual_kg": 240.0,
  "yield_pct": 96.0,
  "output_id": 1
}
```

Backend emits `job_card.output_saved`.

### Frontend prompt — Section 6

> Build the Output Accounting tab using the V2 single-call save pattern. On mount, GET `/job-cards/{id}/output` to load any prior data. Render four collapsible sections:
>
> 1. **FG Output** — numeric inputs: `fg_actual_kg` (R, ≥0), `fg_actual_units` (R, ≥0). Show calculated `yield_pct` = `fg_actual_kg / batch_size_kg * 100`. Show diff vs `fg_expected_kg`/`fg_expected_units`.
> 2. **Byproducts** — repeating list, "Add row" button. Each row: category (R, dropdown of off_grade/spoilage/sample/etc), qty_kg (R, ≥0), uom (default "kg"), remarks (optional).
> 3. **Balance Materials** — repeating list. Each row: material picker (R, autocomplete), balance_type (R, dropdown: left_over | extra_given | scrap | other), qty_kg (R, ≥0), remarks.
> 4. **Embedded QC** — single block: passed (toggle, R), remarks, corrective_action, inspector (R when passed=false).
>
> Single **Save Output** button — collects all four sections into one payload and POSTs `/job-cards/{id}/output`. Show success toast with yield_pct on response. Disable if `status` is `completed`, `closed`, or `cancelled`. After save, transition status to `completed` is server-managed.

---

## Section 7 — Quality Tab
*Fragment:* `QualityFragment.java` — metal detection, weight checks, environment, QC inspection, loss reconciliation

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/job-cards/{job_card_id}/metal-detection` | Append a metal-detection check |
| PATCH | `/job-cards/{job_card_id}/metal-detection/{detection_id}` | **NEW.** Edit a check |
| DELETE | `/job-cards/{job_card_id}/metal-detection/{detection_id}` | **NEW.** Remove a check |
| POST | `/job-cards/{job_card_id}/weight-checks` | Append weight-check samples (full set) |
| PATCH | `/job-cards/{job_card_id}/weight-checks/{check_id}` | **NEW.** Edit one sample row |
| DELETE | `/job-cards/{job_card_id}/weight-checks/{check_id}` | **NEW.** Remove one sample row |
| POST | `/job-cards/{job_card_id}/environment` | Append environment readings |
| PATCH | `/job-cards/{job_card_id}/environment/{env_id}` | **NEW.** Edit a reading |
| DELETE | `/job-cards/{job_card_id}/environment/{env_id}` | **NEW.** Remove a reading |
| POST | `/job-cards/{job_card_id}/loss-reconciliation` | Append loss reconciliation entries |
| PATCH | `/job-cards/{job_card_id}/loss-reconciliation/{recon_id}` | **NEW.** Edit a row |
| DELETE | `/job-cards/{job_card_id}/loss-reconciliation/{recon_id}` | **NEW.** Remove a row |
| PUT | `/qc/inspections/{inspection_id}` | Update / close out a QC inspection |

### `POST /job-cards/{job_card_id}/metal-detection`

**Request body:**

| Field | Type | Required? | Notes |
|---|---|---|---|
| `check_type` | string | **R** | e.g., `"pre_packaging"`, `"post_packaging"` |
| `fe_pass` | bool | O | |
| `nfe_pass` | bool | O | |
| `ss_pass` | bool | O | |
| `failed_units` | int | O | ≥ 0 |
| `seal_check` | bool | O | (PDF only — backend currently ignores) |
| `wt_check` | bool | O | (PDF only) |
| `seal_failed_units` | int | O | (PDF only) |
| `wt_failed_units` | int | O | (PDF only) |
| `dough_temp_c`, `oven_temp_c`, `baking_temp_c` | number | O | (PDF only) |
| `remarks` | string | O | |

**Response** `200`: `{"ok": true, "detection_id": 5}`

### `PATCH /job-cards/{job_card_id}/metal-detection/{detection_id}` *(NEW)*

**Request body** (all fields optional except `updated_by`):

| Field | Type | Required? |
|---|---|---|
| `check_type` | string | O-null |
| `fe_pass` | bool | O-null |
| `nfe_pass` | bool | O-null |
| `ss_pass` | bool | O-null |
| `failed_units` | int (≥0) | O-null |
| `remarks` | string | O-null |
| `updated_by` | string | **R** |

```json
{ "failed_units": 2, "updated_by": "qc_alice" }
```

**Response** `200`:
```json
{
  "ok": true,
  "row": { /* full updated job_card_metal_detection row */ },
  "changed_fields": ["failed_units"]
}
```

**Errors:**
- `404` — detection_id not found, OR belongs to a different job_card_id, OR already deleted
- `409` — parent JC status is non-editable (`completed/closed/cancelled`)
- `422` — empty body (no editable fields)

### `DELETE /job-cards/{job_card_id}/metal-detection/{detection_id}` *(NEW)*

**Request body:**
```json
{ "deleted_by": "qc_alice" }
```

| Field | Type | Required? |
|---|---|---|
| `deleted_by` | string | **R** |

**Response** `200`: `{"ok": true, "row": { /* row with deleted_at + deleted_by set */ }}`
**Errors:** `404` (already deleted / wrong parent), `409` (parent JC non-editable).

### `POST /job-cards/{job_card_id}/weight-checks`

**Request body:**

```json
{
  "target_wt_g":       100.0,
  "tolerance_g":         2.0,
  "accept_range_min":   98.0,
  "accept_range_max":  102.0,
  "samples": [
    { "sample_number": 1, "net_weight": 100.5, "gross_weight": 110.0, "leak_test_pass": true }
  ]
}
```

| Field | Type | Required? | Notes |
|---|---|---|---|
| `target_wt_g`, `tolerance_g`, `accept_range_min`, `accept_range_max` | number | O | (PDF only — backend ignores) |
| `samples` | array | **R** | At least 1 |
| `samples[].sample_number` | int | **R** | > 0 |
| `samples[].net_weight` | number | O | ≥ 0 |
| `samples[].gross_weight` | number | O | ≥ 0 |
| `samples[].leak_test_pass` | bool | O | |

**Response** `200`: `{"ok": true, "samples_recorded": <int>}`

### `PATCH /job-cards/{job_card_id}/weight-checks/{check_id}` *(NEW)*

**Request body:**

| Field | Type | Required? |
|---|---|---|
| `sample_number` | int (>0) | O-null |
| `net_weight` | number (≥0) | O-null |
| `gross_weight` | number (≥0) | O-null |
| `leak_test_pass` | bool | O-null |
| `updated_by` | string | **R** |

**Response/errors:** identical pattern to metal-detection PATCH.

### `DELETE /job-cards/{job_card_id}/weight-checks/{check_id}` *(NEW)*

Same pattern as metal-detection DELETE — body `{"deleted_by": "<user>"}`.

### `POST /job-cards/{job_card_id}/environment`

**Request body:**
```json
{
  "parameters": [
    { "parameter_name": "temperature_c", "value": "22.5" },
    { "parameter_name": "humidity_pct", "value": "65" }
  ]
}
```

| Field | Type | Required? |
|---|---|---|
| `parameters` | array | **R** |
| `parameters[].parameter_name` | string | **R** |
| `parameters[].value` | string | O-null | (TEXT in DB — store any unit) |

**Response** `200`: `{"ok": true, "rows_inserted": <int>}`

### `PATCH /job-cards/{job_card_id}/environment/{env_id}` *(NEW)*

**Request body:**

| Field | Type | Required? |
|---|---|---|
| `parameter_name` | string | O-null |
| `value` | string | O-null |
| `updated_by` | string | **R** |

```json
{ "value": "23.0", "updated_by": "qc_alice" }
```

### `DELETE /job-cards/{job_card_id}/environment/{env_id}` *(NEW)*
Body: `{"deleted_by": "<user>"}`.

### `POST /job-cards/{job_card_id}/loss-reconciliation`

**Request body:**
```json
{
  "entries": [
    {
      "loss_category": "sorting_rejection",
      "budgeted_loss_pct": 5.0,
      "budgeted_loss_kg": 13.5,
      "actual_loss_kg": 12.0,
      "remarks": "Below budget"
    }
  ]
}
```

| Field | Type | Required? |
|---|---|---|
| `entries` | array | **R** |
| `entries[].loss_category` | string | **R** |
| `entries[].budgeted_loss_pct` | number | O |
| `entries[].budgeted_loss_kg` | number | O |
| `entries[].actual_loss_kg` | number | O |
| `entries[].remarks` | string | O-null |

**Response** `200`: `{"ok": true, "entries_recorded": <int>}`

### `PATCH /job-cards/{job_card_id}/loss-reconciliation/{recon_id}` *(NEW)*

**Request body:**

| Field | Type | Required? |
|---|---|---|
| `loss_category` | string | O-null |
| `budgeted_loss_pct` | number (≥0) | O-null |
| `budgeted_loss_kg` | number (≥0) | O-null |
| `actual_loss_kg` | number (≥0) | O-null |
| `variance_kg` | number | O-null |
| `remarks` | string | O-null |
| `updated_by` | string | **R** |

### `DELETE /job-cards/{job_card_id}/loss-reconciliation/{recon_id}` *(NEW)*
Body: `{"deleted_by": "<user>"}`.

### `PUT /qc/inspections/{inspection_id}`

**Request body:**
```json
{
  "result":            "pass",
  "findings":          "All weights within tolerance",
  "corrective_action": "",
  "inspector_user":    "qc_alice"
}
```

| Field | Type | Required? | Notes |
|---|---|---|---|
| `result` | string | **R** | `"pass"` or `"fail"` |
| `findings` | string | O-null | |
| `corrective_action` | string | O-null | |
| `inspector_user` | string | **R** | |

**Response** `200`: `{"ok": true, "inspection_id": "...", "result": "pass"}`. Emits `qc.passed` or `qc.failed`.

### Frontend prompt — Section 7

> Build the Quality tab as four sub-sections, each with its own list view + Add/Edit/Delete CRUD:
>
> **Metal Detection** — table of `annexure_a_b_metal_detection`. Columns: check_type, fe/nfe/ss pass icons, failed_units, remarks, recorded_at. Row actions: Edit (PATCH), Delete (DELETE — confirmation dialog asking deleted_by). Add button opens a form for POST.
>
> **Weight Checks** — table of `annexure_b_weight_checks` showing each sample row. Same Edit/Delete row actions (PATCH/DELETE per sample). Add opens a form to bulk-insert samples (sample_number, net_weight, gross_weight, leak_test_pass).
>
> **Environment** — table of `annexure_c_environment` (parameter_name, value). Edit/Delete per row. Add accepts an array of {parameter_name, value} pairs.
>
> **Loss Reconciliation** — table of `annexure_d_loss_reconciliation`. Color-code variance_kg (green if negative, red if positive). Edit/Delete per row. Add allows multiple entries.
>
> **QC Inspection** — single panel below the four tables. If a `qc_inspection` row exists, show its result/findings; offer Update via PUT `/qc/inspections/{id}`. The chained "Quality save" pattern (PDF section 7) is: POST environment, POST metal-detection, POST weight-checks, then PUT qc/inspections — sequence the calls and only mark the page "saved" after all four succeed.
>
> All Edit/Delete actions disabled when `status` ∈ `{completed, closed, cancelled}`. PATCH/DELETE bodies always include `updated_by` / `deleted_by` = current user. After Edit/Delete, refetch the parent JC detail to update annexure arrays.

---

## Section 8 — Store Tab
*Fragment:* `StoreFragment.java`

**No dedicated endpoints** — this tab is a read-only projection of `section_2a_rm_indent` and `section_2b_pm_indent` from the cached JC detail.

### Frontend prompt — Section 8

> Build a read-only Store tab. From the cached JC detail, render `section_2a_rm_indent` and `section_2b_pm_indent` together as a single table with a "Type" column (RM / PM). Columns: Type, Material SKU, Required (kg), Issued (kg), Acknowledged (kg), Status, Available batches preview (first batch's lot_number + qty). Tap a row to expand and show the full `available_batches` list (lot_number, inward_date, expiry_date, current_qty_kg, warehouse_id, ownership). No buttons or write actions on this tab — direct the user to the Materials tab to take action. Refresh by re-fetching the parent detail.

---

## Section 9 — Sign-offs Tab
*Fragment:* `SignoffsFragment.java`

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| PUT | `/job-cards/{job_card_id}/sign-off` | Record a sign-off |

### `PUT /job-cards/{job_card_id}/sign-off`

**Request body:**
```json
{ "sign_off_type": "operator", "name": "Suresh" }
```

| Field | Type | Required? | Notes |
|---|---|---|---|
| `sign_off_type` | string | **R** | One of `operator`, `supervisor`, `qc`, `production_manager`, etc. |
| `name` | string | **R** | The signing person's name |

**Response** `200`: `{"ok": true, "signoff_id": <int>, "signed_at": "..."}`. Emits `job_card.signed_off`.

### Frontend prompt — Section 9

> Build the Sign-offs tab. Render `section_6_sign_offs` from the cached JC detail as a list of cards: sign_off_type (badge), name, signed_at. Show empty slots for required sign-offs not yet recorded (operator, supervisor, qc — read this list from a config or hardcode for now). Each empty slot has a "Sign" button — opens a dialog asking the user's name (default = current user), with sign_off_type pre-filled, and PUTs `/sign-off`. Disable when status is `cancelled`. After all required sign-offs are present, the parent Detail screen's **Close** button (Section 2) becomes enabled.

---

## Section 10 — Remarks Tab
*Fragment:* `RemarksFragment.java`

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/job-cards/{job_card_id}/remarks` | Append a new remark |
| PATCH | `/job-cards/{job_card_id}/remarks/{remark_id}` | **NEW.** Edit existing remark |
| DELETE | `/job-cards/{job_card_id}/remarks/{remark_id}` | **NEW.** Remove a remark |

### `POST /job-cards/{job_card_id}/remarks`

**Request body:**
```json
{
  "remark_type": "observation",
  "content":     "Started 30 minutes late due to RM delay",
  "recorded_by": "alice"
}
```

| Field | Type | Required? | Notes |
|---|---|---|---|
| `remark_type` | string | **R** | e.g. `observation`, `deviation`, `corrective_action` |
| `content` | string | O-null | The actual remark text |
| `recorded_by` | string | O-null | Who logged it |

**Response** `200`: `{"ok": true, "remark_id": <int>}`

### `PATCH /job-cards/{job_card_id}/remarks/{remark_id}` *(NEW)*

**Request body:**

| Field | Type | Required? |
|---|---|---|
| `remark_type` | string | O-null |
| `content` | string | O-null |
| `updated_by` | string | **R** |

```json
{ "content": "Started 45 minutes late — RM unloading delay", "updated_by": "alice" }
```

**Response** `200`:
```json
{
  "ok": true,
  "row": { /* updated job_card_remarks row */ },
  "changed_fields": ["content"]
}
```

**Errors:**
- `404` — remark_id not found / belongs to different JC / already deleted
- `409` — parent JC non-editable
- `422` — empty body

### `DELETE /job-cards/{job_card_id}/remarks/{remark_id}` *(NEW)*

**Request body:** `{ "deleted_by": "alice" }`

| Field | Type | Required? |
|---|---|---|
| `deleted_by` | string | **R** |

**Response** `200`: `{"ok": true, "row": { /* row with deleted_at, deleted_by set */ }}`

### Frontend prompt — Section 10

> Build the Remarks tab as a chronological feed (newest first). Each remark is a card with: type badge (color-coded: observation=blue, deviation=yellow, corrective_action=green), content text, recorded_by author chip, recorded_at timestamp. Show updated_at + updated_by inline if non-null ("edited by X on Y").
>
> Card actions:
> - **Edit** (visible only to author or admin) — inline editor for content + remark_type. PATCH `/remarks/{remark_id}` with updated_by = current user. Disable when JC status ∉ editable.
> - **Delete** (admin-only) — confirmation dialog. DELETE with deleted_by = current user. Soft-deleted remarks are hidden from the feed.
>
> Top-of-page "Add Remark" form — POST `/remarks`. remark_type select, content textarea, recorded_by = current user (auto). After post, refetch detail and prepend new remark to feed.
>
> If status is `cancelled`, show the entire tab read-only with a banner "This job card was cancelled — remarks are historical."

---

## Cross-cutting concerns

### Status colors (suggested palette for the entire module)
| Status | Color | Meaning |
|---|---|---|
| `locked` | grey | Awaiting previous stage / material |
| `unlocked` | light blue | Ready to assign |
| `assigned` | blue | Team assigned, ready to start |
| `material_received` | teal | Materials in floor stock |
| `in_progress` | green | Production active |
| `completed` | dark green | Output recorded |
| `closed` | navy | Sign-offs done, locked for archive |
| `cancelled` | red (or strikethrough) | Soft-cancelled, hidden by default |

### When to refetch the JC detail
- After any successful POST/PATCH/PUT/DELETE on this JC or its annexures
- On tab activation (cheap re-fetch is fine)
- After receiving a relevant WebSocket event (`job_card.*`, `job_card.annexure.*`) — the WebSocket payload includes `job_card_id`; refetch only if it matches the current JC

### Common error handling
| Status | What to show |
|---|---|
| `401` | Redirect to login |
| `403` | Toast: "Permission denied" (role doesn't allow this action) |
| `404` | If on detail screen: "Job card not found, may have been cancelled" — navigate back to list. Otherwise just a toast. |
| `409` | Toast with the response `detail` field — typically a state-transition violation. Disable the action until the state is acceptable. |
| `422` | Inline form-validation errors. The response body has Pydantic-formatted error info: parse `errors[]` and map field paths to your form fields. |
| `5xx` | Toast: "Server error — please try again" + log to crash reporter |

### Authentication
All requests must include `Authorization: Bearer <token>`. On `401`, refresh the token via your auth flow; do not retry indefinitely.

### Empty-state defaults
- Annexure arrays in the detail response default to `[]`, never `null`. Render an empty-state message or "Add" button when length is 0.
- `section_5_output` is `null` until first save — render an "Output not recorded yet" placeholder.
- `pagination.total = 0` → "No matching job cards" empty state in list screen.

### NEW PATCH/DELETE field-rule summary

For every `PATCH` endpoint on this module:
- **Send only the fields you want to change.** Omitted fields stay as-is.
- **`updated_by`** is always required (audit field).
- **Sending `null` explicitly** writes `NULL` to that column — useful for clearing optional fields like `floor` or `customer_name`.
- **Empty body (no editable fields supplied)** → `422 No editable fields supplied`.
- **`PATCH /job-cards/{id}` is blocked** when status ∈ `{completed, closed, cancelled}`.
- **PATCH on annexure rows** is blocked under the same status rule on the parent JC.

For every `DELETE` endpoint:
- **`deleted_by`** is always required.
- **`DELETE /job-cards/{id}` requires `cancellation_reason`** (≥ 3 chars) and only works pre-start (status ∈ `{locked, unlocked, assigned}`).
- **Annexure DELETEs** require only `deleted_by` and follow the parent JC's editable-status rule.
- **All deletes are soft** — rows stay in DB with `deleted_at` set; list/detail endpoints filter them out by default.
