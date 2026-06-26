# Accounting Summary Logic — Job Card v2

Per-JC mass-balance computation that drives the **Accounting Summary**
card on the Output tab and the persisted row in `job_card_accounting_v2`.

The summary answers: "did every kg that came in get accounted for?"
Every input has to land somewhere — as output, dispatched material,
loss, byproduct, or leftover. Anything unaccounted for is a balance
gap that the operator (or shift supervisor) needs to resolve.

---

## The balance equation

```
INPUT  =  OUTPUT  +  LOSS  +  LEFTOVER
```

```
  total_input_qty
       │
       ├── carried_in_qty          (handed forward from prev stage)
       └── rm_consumed_kg          (RM / PM / SFG actually used this stage)

  total_output_qty
       │
       ├── output_qty_kg           (FG / SFG / WIP produced)
       └── dispatched_out_qty      (material handed forward)

  total_loss_qty
       │
       ├── process_loss_qty        (operator-entered)
       ├── offgrade_total_qty      (byproducts excl. control_sample / balance_material)
       ├── rejection_qty
       ├── wastage_qty
       └── control_sample_qty

  total_leftover_qty
       │
       ├── balance_material_qty    (balance rows: "returned" + byproducts: "balance_material")
       └── extra_give_away_qty     (balance rows: "extra_given" — final stage only)

  total_accounted_qty    = total_output_qty + total_loss_qty + total_leftover_qty
  balance_difference_qty = total_input_qty  - total_accounted_qty
  is_balanced            = abs(balance_difference_qty) <= TOLERANCE_KG
```

---

## Where each value comes from (v2 tables)

| Summary field          | Source                                                                                                                                                                |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `carried_in_qty`       | `job_card_v2.carried_qty_kg`                                                                                                                                          |
| `rm_consumed_kg`       | `job_card_output_v2.rm_consumed_kg` (or `SUM(actual_consumed_qty)` from `job_card_material_consumption_v2` for per-line resolution)                                   |
| `output_qty_kg`        | `job_card_output_v2.output_qty_kg`                                                                                                                                    |
| `output_qty_units`     | `job_card_output_v2.output_qty_units` (FG stage only)                                                                                                                 |
| `dispatched_out_qty`   | `job_card_v2.dispatched_to_next_kg`                                                                                                                                   |
| `process_loss_qty`     | `job_card_output_v2.process_loss_kg` (added in migration 026)                                                                                                         |
| `offgrade_total_qty`   | `SUM(quantity) FROM job_card_byproducts_v2 WHERE category IN ('tukda','damaged','black_stained','without_shell','empty_shells','dust','other')`                       |
| `rejection_qty`        | `SUM(quantity) FROM job_card_byproducts_v2 WHERE category = 'rejection'`                                                                                              |
| `control_sample_qty`   | `SUM(quantity) FROM job_card_byproducts_v2 WHERE category = 'control_sample'` + `SUM(qty_kg) FROM job_card_balance_material_v2 WHERE balance_type = 'control_sample'` |
| `wastage_qty`          | `SUM(qty_kg) FROM job_card_balance_material_v2 WHERE balance_type = 'wastage'`                                                                                        |
| `balance_material_qty` | `SUM(qty_kg) FROM job_card_balance_material_v2 WHERE balance_type = 'returned'` + `SUM(quantity) FROM job_card_byproducts_v2 WHERE category = 'balance_material'`     |
| `extra_give_away_qty`  | `SUM(qty_kg) FROM job_card_balance_material_v2 WHERE balance_type = 'extra_given'`                                                                                    |

---

## Tolerance

A strict `balance_difference_qty == 0` is unrealistic — weighing scales
have noise, kg→pcs conversions round. Use a tiered tolerance instead
of a flat number:

```
TOLERANCE_KG = max(0.5, 0.005 * total_input_qty)
```

- 0.5 kg minimum covers small-batch scale rounding
- 0.5% of input scales with batch size (a 1-tonne batch gets 5 kg of
  slack; a 50 kg batch gets 0.5 kg)

---

## Tiered `balance_status` (better than a plain `is_balanced` boolean)

| `abs(balance_difference_qty)` | `balance_status` | UI colour |
| ----------------------------- | ---------------- | --------- |
| `<= max(0.5, 0.005 * input)`  | `balanced`       | green     |
| `<= max(2.0, 0.02  * input)`  | `near_balance`   | yellow    |
| otherwise                     | `unbalanced`     | red       |

The UI should render the signed diff next to the badge —
`"+0.8 kg over"` or `"-3.2 kg short"` — so the operator sees the
direction of the error, not just the magnitude.

---

## Percentages (for the summary card)

```
process_loss_pct = (process_loss_qty                          / total_input_qty) * 100
other_loss_pct   = ((offgrade_total_qty + rejection_qty
                     + wastage_qty)                           / total_input_qty) * 100
total_loss_pct   = (total_loss_qty                            / total_input_qty) * 100
yield_pct        = (output_qty_kg                             / rm_consumed_kg)  * 100
```

Guard each division with `if denominator > 0 else None` so an empty JC
doesn't `ZeroDivisionError`. The UI should render `—` for null pct
fields (not `0.00%` — that's misleading).

> ⚠️ `yield_pct` is stored in a `NUMERIC(6,3)` column on
> `job_card_output_v2` (max ±999.999). Implausible yields (typo'd kg
> values) are rejected upstream by `record_output()` with
> `error: "implausible_yield"` (commit `2f6cc32`); the accounting
> summary doesn't need to defend against them again.

---

## Service function shape

Suggested location: `app/modules/production/services/jc_accounting_v2.py`
(file already exists for the byproducts/accounting writes).

```python
async def compute_accounting_summary(conn, *, job_card_id: int) -> dict:
    """Pure read — does not persist. Pulls from the v2 ledger tables for
    one JC and returns a flat dict matching job_card_accounting_v2's
    columns plus the derived percentages and tiered status.

    Wire it into:
      * the /accounting save endpoint as a validation pre-flight
        (warn the operator before they persist an unbalanced row)
      * the JC-detail GET as a new `section_accounting` key so the
        Accounting Summary card renders without a second round-trip
    """
    return {
        "total_input_qty":         ...,
        "carried_in_qty":          ...,
        "rm_consumed_kg":          ...,
        "total_output_qty":        ...,
        "output_qty_kg":           ...,
        "output_qty_units":        ...,
        "dispatched_out_qty":      ...,
        "total_loss_qty":          ...,
        "process_loss_qty":        ...,
        "offgrade_total_qty":      ...,
        "rejection_qty":           ...,
        "wastage_qty":             ...,
        "control_sample_qty":      ...,
        "total_leftover_qty":      ...,
        "balance_material_qty":    ...,
        "extra_give_away_qty":     ...,
        "total_accounted_qty":     ...,
        "balance_difference_qty":  ...,
        "is_balanced":             True | False,
        "balance_status":          "balanced" | "near_balance" | "unbalanced",
        "process_loss_pct":        ...,
        "other_loss_pct":          ...,
        "total_loss_pct":          ...,
        "yield_pct":               ...,
        "breakdown": {
            "byproducts":         [...per-category sums...],
            "balance_materials":  [...per-type sums...],
        },
    }
```

---

## Reference SQL (single round-trip)

For callers that want everything in one shot — useful for the detail
GET embed:

```sql
WITH bp AS (
    SELECT
        SUM(quantity) FILTER (WHERE category IN ('tukda','damaged',
            'black_stained','without_shell','empty_shells','dust','other')) AS offgrade,
        SUM(quantity) FILTER (WHERE category = 'rejection')        AS rejection,
        SUM(quantity) FILTER (WHERE category = 'control_sample')   AS cs_bp,
        SUM(quantity) FILTER (WHERE category = 'balance_material') AS bm_bp
      FROM job_card_byproducts_v2
     WHERE job_card_id = $1
), bm AS (
    SELECT
        SUM(qty_kg) FILTER (WHERE balance_type = 'returned')        AS returned,
        SUM(qty_kg) FILTER (WHERE balance_type = 'wastage')         AS wastage,
        SUM(qty_kg) FILTER (WHERE balance_type = 'control_sample')  AS cs_bm,
        SUM(qty_kg) FILTER (WHERE balance_type = 'extra_given')     AS extra
      FROM job_card_balance_material_v2
     WHERE job_card_id = $1
), o AS (
    SELECT rm_consumed_kg, output_qty_kg, output_qty_units, process_loss_kg
      FROM job_card_output_v2
     WHERE job_card_id = $1
     ORDER BY recorded_at DESC LIMIT 1
), j AS (
    SELECT carried_qty_kg, dispatched_to_next_kg
      FROM job_card_v2 WHERE job_card_id = $1
)
SELECT
    COALESCE(j.carried_qty_kg,         0) AS carried_in_qty,
    COALESCE(o.rm_consumed_kg,         0) AS rm_consumed_kg,
    COALESCE(o.output_qty_kg,          0) AS output_qty_kg,
    o.output_qty_units,
    COALESCE(j.dispatched_to_next_kg,  0) AS dispatched_out_qty,
    COALESCE(o.process_loss_kg,        0) AS process_loss_qty,
    COALESCE(bp.offgrade,              0) AS offgrade_total_qty,
    COALESCE(bp.rejection,             0) AS rejection_qty,
    COALESCE(bm.wastage,               0) AS wastage_qty,
    COALESCE(bp.cs_bp, 0) + COALESCE(bm.cs_bm, 0) AS control_sample_qty,
    COALESCE(bm.returned, 0) + COALESCE(bp.bm_bp, 0) AS balance_material_qty,
    COALESCE(bm.extra,                 0) AS extra_give_away_qty
  FROM o, j, bp, bm;
```

The Python wrapper computes the derived fields (`total_*`,
`balance_difference_qty`, `*_pct`, `balance_status`) on top of this row.

---

## Edge cases worth documenting

1. **Empty JC (no output saved yet)** — every field is 0; `is_balanced`
   is `True` (vacuously), `balance_status` is `balanced`. The UI should
   special-case this: if `total_input_qty == 0`, render the card as
   "No data yet" instead of a misleading "balanced" badge.
2. **Multiple output rows** — `job_card_output_v2` is append-only; the
   latest row wins. Use `ORDER BY recorded_at DESC LIMIT 1` (see SQL
   above). The `yield_pct` and `process_loss_qty` always reflect the
   most recent save.
3. **Negative differences** — a positive `balance_difference_qty`
   means input > accounted (something's unaccounted for, "short"). A
   negative value means accounted > input (over-counted; usually a
   typo or double-counted byproduct). Both are flagged the same way
   by `balance_status`, but the UI should show the sign so the
   operator knows whether to look for missing or duplicate entries.
4. **Final stage with `output_qty_units` set** — `dispatched_out_qty`
   is typically 0 on the last stage (no next JC). The balance still
   holds; just the dispatched leg goes to zero.
5. **Tolerance for non-kg UOM** — if a stage's input UOM is `LTRS` or
   `NOS`, the tolerance fallback (`0.5`) still works as the same
   absolute number in that UOM. The 0.5% relative term scales
   correctly regardless of unit. Don't try to convert across UOMs in
   the summary — it's per-stage and per-UOM.

---

## Implementation status

- ✅ Underlying tables exist (migrations 017 + 018 + 026 + 027)
- ✅ `record_output()` persists `process_loss_kg`
- ✅ `save_byproducts()` and `replace_balance_materials()` persist their rows
- ✅ JC-detail GET returns `byproducts`, `balance_materials`, `qc` and
  `section_5_output.process_loss_kg`
- ⏳ `compute_accounting_summary()` not yet written
- ⏳ `section_accounting` key not yet exposed on JC-detail GET
- ⏳ Frontend Accounting Summary card still uses ad-hoc local math

Next step is wiring the service function above + embedding it in the
JC-detail GET, then the frontend can drop its local math and render
the server-computed shape directly.

---

# Planning: Replicate the SO Fulfillment Listing Page

> **Purpose** — A precise, copy-paste-able spec for re-implementing the
> SO Fulfillment listing page in another client (mobile, alternative
> web frontend, embedded view). Every endpoint, every query param,
> every response field, every UI trigger.
>
> **Source of truth** — the live behaviour in
> `D:\Consumption\New\frontend_replica\src\modules\production\fulfillment\`
> (Electron + plain JS) and the FastAPI routes in
> `D:\Consumption\New\server_replica_new\app\modules\production\router.py`.
> When this doc disagrees with the source, the source wins; raise a PR
> against this doc.
>
> **Anti-goals** — this is NOT a behavioural guide ("how the operator
> uses it"). It's a mechanical contract: given inputs X, the server
> returns Y. Keep narrative in product docs; keep this file precise.

## Endpoint inventory (canonical)

| #   | Method | Path                                                                       | Purpose                       |
| --- | ------ | -------------------------------------------------------------------------- | ----------------------------- |
| 1   | GET    | `/api/v1/production/fulfillment-v2`                                        | Paginated list of FG SO lines |
| 2   | GET    | `/api/v1/production/fulfillment-v2/filter-options`                         | Cross-filtered dropdown vals  |
| 3   | GET    | `/api/v1/production/fulfillment-v2/{so_fulfillment_id}/detail`             | Per-row inline expansion data |
| 4   | POST   | `/api/v1/production/fulfillment-v2/sync`                                   | Ingest new SO lines           |
| 5   | PUT    | `/api/v1/production/fulfillment-v2/{so_fulfillment_id}/revise`             | Revise qty / deadline         |
| 6   | POST   | `/api/v1/production/fulfillment-v2/carryforward`                           | Roll lines into next FY       |
| 7   | PUT    | `/api/v1/production/fulfillment-v2/{so_fulfillment_id}/bom-override`       | Override per-line BOM         |
| 8   | GET    | `/api/v1/production/fulfillment-v2/{so_fulfillment_id}/floor-stock`        | Read floor-stock entries      |
| 9   | PUT    | `/api/v1/production/fulfillment-v2/{so_fulfillment_id}/floor-stock`        | Add/update floor-stock        |
| 10  | POST   | `/api/v1/production/plans-v2`                                              | Create plan from selection    |

Auth on all routes: bearer JWT in `Authorization` header. 401 if absent
or expired (handled by global middleware, not per-route).

---

## 1. List fulfillment records

```
GET /api/v1/production/fulfillment-v2
```

### Query params

| Param        | Type                | Default | Notes                                                                                  |
| ------------ | ------------------- | ------- | -------------------------------------------------------------------------------------- |
| `entity`     | string              | (none)  | Scope to one entity (e.g. `cfpl`, `cdpl`). Omit for all.                               |
| `customer`   | string (CSV)        | (none)  | Comma-separated customer names. Multi-value, cross-filtered with `so_number`/`article`. |
| `so_number`  | string (CSV)        | (none)  | Comma-separated SO numbers.                                                            |
| `article`    | string (CSV)        | (none)  | Comma-separated FG SKU names.                                                          |
| `page`       | int ≥ 1             | `1`     | 1-indexed.                                                                             |
| `page_size`  | int 1–500           | `50`    | Server-pinned ceiling = 500.                                                           |

### Request body

None.

### Response body

```json
{
  "results": [
    {
      "fulfillment_id":       12345,
      "so_line_id":           98765,
      "financial_year":       "2026-27",
      "fg_sku_name":          "SKU-ABC-001",
      "customer_name":        "Acme Corp",
      "entity":               "cfpl",
      "original_qty_kg":      500.0,
      "produced_qty_kg":      100.0,
      "dispatched_qty_kg":    50.0,
      "planned_qty_kg":       300.0,
      "pending_qty_kg":       50.0,
      "original_qty_units":   18000,
      "produced_qty_units":   3600,
      "dispatched_qty_units": 1800,
      "planned_qty_units":    10800,
      "pending_qty_units":    1800,
      "delivery_deadline":    "2026-06-15",
      "order_status":         "open",
      "created_at":           "2026-05-10T10:30:00Z",
      "updated_at":           "2026-05-22T14:45:00Z",
      "so_id":                12,
      "so_number":            "SO-2026-001",
      "so_date":              "2026-05-05"
    }
  ],
  "pagination": {
    "page":        1,
    "page_size":   50,
    "total":       234,
    "total_pages": 5
  }
}
```

**Generated columns** — `pending_qty_kg` and `pending_qty_units` are
GENERATED ALWAYS in PostgreSQL as
`original - produced - dispatched - planned`. Never write to them.
`order_status` is one of `open` | `partial` | `fulfilled` | `cancelled`.

**Sort** — server-side: `ORDER BY delivery_deadline ASC NULLS LAST,
fulfillment_id ASC`. Do not re-sort client-side; respect the server
order so pagination is deterministic.

**Pagination** — load-more UI: track `currentPage`, append `results`
on each fetch, stop when `results.length < page_size` or
`page >= pagination.total_pages`.

### Triggers

- Initial page load
- Entity selector click → reset `page=1`, reload
- Any filter dropdown change → reset `page=1`, reload
- "Load more" button click → `page = currentPage + 1`, append
- After Sync / Revise / Carryforward succeeds → reset, reload

---

## 2. Filter options (cross-filtered dropdown values)

```
GET /api/v1/production/fulfillment-v2/filter-options
```

### Query params

Same shape as #1 (`entity`, `customer`, `so_number`, `article`). Pass
the *current* filter state; the response excludes the dimension you're
asking about so the user can widen, not narrow.

### Response body

```json
{
  "customers":  ["Acme Corp", "Beta Ltd"],
  "so_numbers": ["SO-2026-001", "SO-2026-002"],
  "articles":   ["SKU-ABC-001", "SKU-XYZ-999"]
}
```

**Cross-filter contract** — if `customer=Acme` is set, `so_numbers`
contains only SOs that belong to Acme; `articles` contains only SKUs
appearing on Acme orders. Implement as a single SQL pass with three
`DISTINCT` projections sharing the same WHERE clause.

### Triggers

- Initial page load (once)
- Any filter change (re-fetch so sibling dropdowns narrow correctly)

---

## 3. Fulfillment detail (inline expand)

```
GET /api/v1/production/fulfillment-v2/{so_fulfillment_id}/detail
```

### Path params

| Param                | Type | Notes                       |
| -------------------- | ---- | --------------------------- |
| `so_fulfillment_id`  | int  | The `fulfillment_id` from list #1. |

### Response body

```json
{
  "fulfillment": {
    /* All keys from list-row shape (#1), plus: */
    "is_planned":           true,
    "plan_line_id":         54321,
    "pending_qty_warning":  false,
    "status":               "open"
  },
  "bom": {
    "bom_id":   456,
    "bom_note": null,
    "process_routes": [
      {
        "step_number":   1,
        "process_name":  "Roasting",
        "stage":         "Roasting Stage",
        "std_time_min":  45.5,
        "loss_pct":      2.5,
        "machine_type":  "Industrial Roaster"
      }
    ],
    "lines": [
      {
        "bom_line_id":          7890,
        "material_sku_name":    "MAT-INP-001",
        "item_type":            "rm",
        "quantity_per_unit":    0.5,
        "loss_pct":             1.0,
        "uom":                  "KG",
        "godown":               "RM Store A",
        "is_removed":           false,
        "is_overridden":        false,
        "override_reason":      null,
        "gross_requirement_kg": 125.5,
        "on_hand_kg":           100.0,
        "inventory_status":     "SHORTAGE",
        "process_stage":        "Roasting",
        "can_use_offgrade":     false
      }
    ]
  },
  "floor_machines": [
    {
      "machine_name": "Roaster-01",
      "floor":        "Ground Floor",
      "status":       "active",
      "allocation":   "idle",
      "is_in_bom":    true,
      "capacity": [
        { "stage": "Roasting Stage", "capacity_kg_per_hr": 150.0 }
      ]
    }
  ],
  "linked_so": {
    "so_number":        "SO-2026-001",
    "so_date":          "2026-05-05",
    "customer_name":    "Acme Corp",
    "voucher_type":     "Sales",
    "sku_name":         "SKU-ABC-001",
    "quantity":         18000,
    "quantity_units":   500.0,
    "uom":              "KG",
    "rate_inr":         85.50,
    "amount_inr":       1539000.0,
    "total_amount_inr": 1539000.0,
    "item_type":        "fg"
  },
  "revision_log": [
    {
      "revised_at":    "2026-05-20T09:15:00Z",
      "revision_type": "qty",
      "old_value":     "500.0 kg",
      "new_value":     "450.0 kg",
      "reason":        "Customer revised order",
      "revised_by":    "planner@company.com"
    }
  ],
  "floor_stock": [
    {
      "material_sku_name": "MAT-INP-002",
      "item_type":         "rm",
      "quantity_kg":       25.5,
      "unit":              "KG",
      "floor_location":    "Lower Basement, Rack A3",
      "notes":             "Temporary staging"
    }
  ]
}
```

**Field semantics worth pinning**:

- `bom.bom_note` — non-null when the BOM lookup returned no rows
  (e.g., `"No BOM found"`); UI should render `bom_note` as the empty
  state instead of showing an empty `lines` array.
- `inventory_status` — one of `OK` | `SHORTAGE` | `OVERSTOCK`. Driven
  by `on_hand_kg` vs `gross_requirement_kg`. Don't recompute
  client-side.
- `is_in_bom` (on `floor_machines`) — `true` when the machine type
  matches a `bom.process_routes[].machine_type`; UI uses this to
  highlight relevant machines.
- `linked_so` — nullable (`null` when the SO row was soft-deleted
  after the fulfillment was created). Render `—` for missing values.

### Triggers

- Row click (not on checkbox / action button) → expand inline.
- **Cache** the response keyed by `so_fulfillment_id`; re-expanding a
  cached row should be O(0) network calls. Invalidate on:
  - Successful Revise / BOM Override / Floor Stock for the same id
  - Manual refresh

---

## 4. Sync fulfillment (ingest new SO lines)

```
POST /api/v1/production/fulfillment-v2/sync
```

### Request body

```json
{
  "entity": "cfpl"
}
```

`entity` is **optional / nullable** — pass `null` (or omit) to sync
across all entities. Required when the operator wants entity-scoped
behaviour.

### Response body

```json
{
  "synced":  45,
  "skipped": 12,
  "failed":  0,
  "total":   57
}
```

**Idempotency** — the backend insert uses
`ON CONFLICT (so_line_id, financial_year) DO NOTHING`. Re-syncs of the
same SO book are safe — `skipped` will go up, `synced` will be 0.

### Triggers

- "Sync" button click on the listing toolbar
- Auto-fired after a successful SO book upload (see
  [so-creation.js:87–123](../../frontend_replica/src/modules/production/so-creation/so-creation.js))

### UI contract

- Disable the Sync button + show a spinner while the request is in
  flight.
- On success: toast `"Sync complete: {synced} synced, {skipped} skipped"`,
  reload the list (#1).
- On `synced === 0 && skipped > 0`: toast `"Up to date — nothing new"`
  (do NOT double-toast if auto-fired from the SO upload flow with zero
  new rows).

---

## 5. Revise fulfillment (qty / deadline)

```
PUT /api/v1/production/fulfillment-v2/{so_fulfillment_id}/revise
```

### Request body

```json
{
  "new_qty":     480.0,
  "new_units":   17200,
  "new_date":    "2026-06-20",
  "reason":      "Customer request",
  "revised_by":  "planner@company.com"
}
```

All four data fields are **optional but at least one is required**.
`new_qty` in kg, `new_units` in pack-count, `new_date` as ISO
`YYYY-MM-DD`. `reason` and `revised_by` are required when ANY change
field is set (audit trail).

### Response body

```json
{ "updated": 1 }
```

### Client-side validation (before POST)

- `new_qty` >= `dispatched_qty_kg` of the current row (server also
  enforces, but UI should pre-empt the 400).
- `new_units` >= `dispatched_qty_units` if provided.
- `new_date` ISO format, no past dates (warn but allow override on
  confirm).

### Triggers

- "Revise Qty" form in the detail panel Summary tab.
- "Change Deadline" inline date-picker in the deadline cell.

---

## 6. Carryforward

```
POST /api/v1/production/fulfillment-v2/carryforward
```

### Request body

```json
{
  "fulfillment_ids": [12345, 12346],
  "new_fy":          "2026-27",
  "revised_by":      "planner@company.com"
}
```

`new_fy` format is `YYYY-YY` (4-digit start year, dash, 2-digit end
year). Backend rejects other formats with 400.

### Response body

```json
{
  "carried_forward": 2,
  "failed":          []
}
```

### Triggers

- Bulk action "Carry Forward" on selected rows.
- Detail-panel "Carry Forward" button for a single row.

---

## 7. BOM override

```
PUT /api/v1/production/fulfillment-v2/{so_fulfillment_id}/bom-override
```

### Request body

```json
{
  "overrides": [
    {
      "bom_line_id":         7890,
      "material_sku_name":   "MAT-INP-001",
      "quantity_per_unit":   0.48,
      "loss_pct":            1.2,
      "uom":                 "KG",
      "godown":              "RM Store B",
      "is_removed":          false,
      "override_reason":     "Density variation"
    }
  ],
  "overridden_by": "QC Manager"
}
```

**Notes**:

- Each override is keyed on `bom_line_id`. Omitting a line means "no
  override for this line" — the original BOM value still applies.
- `quantity_per_unit` and `loss_pct` are optional; null = keep original.
- `is_removed: true` drops the line from the JC's gross-requirement
  computation entirely.
- `override_reason` is required when ANY field is overridden.

### Response body

Same shape as the `bom` block on the detail GET (#3), with
`is_overridden: true` on the affected lines.

### Triggers

- "Edit BOM Overrides" button in the BOM & Inventory tab.

---

## 8. Floor stock (read)

```
GET /api/v1/production/fulfillment-v2/{so_fulfillment_id}/floor-stock
```

### Response body

```json
{
  "floor_stock": [
    {
      "material_sku_name": "MAT-INP-002",
      "item_type":         "rm",
      "quantity_kg":       25.5,
      "unit":              "KG",
      "floor_location":    "Lower Basement, Rack A3",
      "notes":             "Temporary staging"
    }
  ]
}
```

### Triggers

- Detail panel BOM tab → "Floor Stock" sub-section render.
- Already embedded in the detail GET (#3) as `floor_stock`; the
  standalone GET is for refresh-only flows (after #9).

---

## 9. Floor stock (add / update)

```
PUT /api/v1/production/fulfillment-v2/{so_fulfillment_id}/floor-stock
```

### Request body

```json
{
  "entries": [
    {
      "material_sku_name": "MAT-INP-002",
      "item_type":         "rm",
      "quantity_kg":       25.5,
      "unit":              "KG",
      "floor_location":    "Lower Basement, Rack A3",
      "notes":             "Temp staging"
    }
  ],
  "added_by": "Floor Supervisor"
}
```

`item_type` is one of `rm` | `pm` (lowercase). `unit` is one of the
v2 universal UOMs (`KG`, `GMS`, `NOS`, etc.). `floor_location` is
free-form text (no validation).

### Response body

```json
{
  "added":     1,
  "updated":   0,
  "entries":   [/* re-serialised floor_stock rows */]
}
```

### Triggers

- "+ Add floor stock" toggle in the BOM tab.

---

## 10. Create plan from selection

```
POST /api/v1/production/plans-v2
```

### Request body

```json
{
  "entity":     "cfpl",
  "warehouse":  "W-202",
  "plan_type":  "daily",
  "plan_date":  "2026-05-22",
  "date_from":  "2026-05-22",
  "date_to":    "2026-06-22",
  "lines": [
    {
      "fg_sku_name":               "SKU-ABC-001",
      "customer_name":             "Acme Corp",
      "planned_qty_kg":            480.0,
      "planned_qty_units":         17200,
      "linked_so_fulfillment_ids": [12345],
      "area":                      "Lower Basement",
      "deadline_date":             "2026-06-15",
      "steps": [
        {
          "process_name": "Roasting",
          "stage":        "Roasting Stage",
          "floor":        "Lower Basement",
          "std_time_min": 45.5,
          "loss_pct":     2.5
        }
      ]
    }
  ]
}
```

**Field-by-field rules**:

| Field                          | Required | Notes                                                                                                |
| ------------------------------ | -------- | ---------------------------------------------------------------------------------------------------- |
| `entity`                       | ✅       | Single value for the whole plan. Selection must agree.                                               |
| `warehouse`                    | ✅       | Derived from `chosenFactory` via the client-side `FACTORY_TO_WAREHOUSE` map.                          |
| `plan_type`                    | ✅       | Always `"daily"` from this flow.                                                                     |
| `plan_date`                    | ✅       | Today, ISO.                                                                                          |
| `date_from`                    | ✅       | Today, ISO.                                                                                          |
| `date_to`                      | ✅       | Latest per-line `deadline_date`, else `date_from`. Backend CHECK enforces `>= date_from`.            |
| `lines[].fg_sku_name`          | ✅       | From the fulfillment row.                                                                            |
| `lines[].planned_qty_kg`       | ✅       | User override or `pending_qty_kg`; must be `> 0` and `<= pending_qty_kg`.                            |
| `lines[].planned_qty_units`    | ✅       | Computed if user didn't override.                                                                    |
| `lines[].linked_so_fulfillment_ids` | ✅  | Array (always 1 element from this UI). Backend supports multi-SO bundling.                           |
| `lines[].area`                 | optional | First step's floor for non-step-aware readers. Derived; UI doesn't ask separately.                   |
| `lines[].deadline_date`        | optional | Per-line override. Defaults to the linked SO's `delivery_deadline`.                                  |
| `lines[].steps`                | optional | Sent verbatim. If omitted, backend snapshots from `bom_process_route`. **Merged steps stay merged.** |

### Response body

```json
{
  "plan_id":       98765432,
  "plan_line_ids": [54321, 54322, 54323]
}
```

### Client-side validation (before POST)

1. **One warehouse per plan** — if any two selected rows have
   different `factory` choices, block the POST and toast the conflict.
2. **Qty bounds** — for every line, `planned_qty_kg <= pending_qty_kg`
   AND `planned_qty_units <= pending_qty_units` (when units are
   tracked).
3. **Factory permission** — every chosen factory must be in
   `allowedFactoryCodes()` for the current user; every chosen floor
   must be in `allowedFloorsFor(factory)`.
4. **Step count** — `lines[i].steps.length >= 1` (after any merges).

### Triggers

- "Create Plan" button at the top of the listing (disabled until
  `selectedIds.size >= 1`).

### Post-success

- Clear `selectedIds` and `cardCfgMap`.
- Toast `"Plan #{plan_id} created"`.
- Navigate to plan-detail page for `plan_id`.

---

## UI behaviours summary

### Top toolbar

```
[ Entity radio ▾ ]  [ Sync ▶ ]  [ Selected: N ]  [ Create Plan ▶ ]
[ Search: "" ]  [ Customer ▾ ]  [ SO# ▾ ]  [ Article ▾ ]  [ Clear ]
```

| Control                  | Triggers                       |
| ------------------------ | ------------------------------ |
| Entity radio             | `resetAndLoad()` (#1 + #2)     |
| Sync                     | #4, then `resetAndLoad()`      |
| Multi-select dropdowns   | `loadFilterOptions()` (#2) + `resetAndLoad()` (#1) |
| Clear                    | Reset all filters, reload      |
| Create Plan              | #10, then clear selection      |

### Row interactions

| Action                     | Effect                                       |
| -------------------------- | -------------------------------------------- |
| Click row body             | Toggle inline expansion (calls #3)           |
| Check row checkbox         | Add/remove `fulfillment_id` from `selectedIds` |
| Check "Select all" header  | Toggle every visible row's checkbox          |
| Click action button (Revise / BOM / etc.) | Stop propagation, open modal/form  |

### Selection model

```
selectedIds  : Set<int>          (which rows are checked)
cardCfgMap   : Map<int, CardCfg> (per-card plan config — qty, factory, steps, deadline)

CardCfg {
  qty_kg?:        number,        // user override
  qty_units?:     number,        // user override
  factory:        string,        // required before plan POST
  steps:          Step[],        // copied from detail; user-edits floors + merge
  stepsLoaded:    bool,
  stepsLoading:   bool,
  deadline_date?: string,        // user override (ISO)
}
```

Clear selection → `selectedIds.clear()` + `cardCfgMap.clear()` +
uncheck every visible `.ful-row-cb` + reset header "Select all".

### States

| State    | Render                                                    |
| -------- | --------------------------------------------------------- |
| Loading  | Spinner overlay on `#tableLoading`                        |
| Empty    | `#emptyState` div (icon + "No fulfillment lines match")   |
| Error    | Toast with HTTP status + backend `detail` message; existing rows stay rendered if a refresh fails (don't blow them away) |
| Forbidden (401/403) | Redirect to login OR replace table body with "Access denied" empty-state |

### Detail panel states

| State            | Render                                                  |
| ---------------- | ------------------------------------------------------- |
| Loading          | Skeleton blocks for each tab section                    |
| Loaded           | Tabs: Summary · BOM & Inventory · Process Routes · Machines · Revision log · Floor stock |
| Cached re-expand | Render immediately from `detailCache`; refresh on a manual reload button only |
| Fetch failed     | Inline error message + retry button (don't toast — keeps the user's place) |

---

## Implementation checklist (for a new client)

- [ ] Wire `authFetch` (or equivalent) with bearer-token interceptor.
- [ ] Build the API client wrapper — one function per endpoint above,
      returning typed responses. Reject early on missing required
      params; don't build the URL with `undefined` interpolated.
- [ ] Listing view: render rows, wire pagination state, wire load-more.
- [ ] Top toolbar: entity radio, search, multi-selects, Sync, Create Plan.
- [ ] Filter state → re-fetch `/filter-options` on every change.
- [ ] Inline expand: cache detail by `fulfillment_id`, render tabs.
- [ ] Detail tabs: Summary (revise forms), BOM (override + floor stock),
      Process Routes (read-only), Machines (read-only), Revision log
      (read-only).
- [ ] Selection model: `Set<int>` + `Map<int, CardCfg>`.
- [ ] Create Plan flow: validate before POST, navigate on success.
- [ ] Empty / loading / error / forbidden states for both listing and
      detail.
- [ ] Toast / notification surface — used by Sync, Revise, BOM
      Override, Floor Stock, Create Plan.

### Behavioural test cases worth automating

1. Filter cross-narrowing: select customer "Acme" → `so_numbers`
   dropdown only shows Acme orders.
2. Page navigation: navigate to page 3 → URL/state survives a reload
   (if URL-driven) OR resets to page 1 (if state-only).
3. Sync idempotency: hit Sync twice in a row → second response has
   `synced: 0`.
4. Create Plan with mixed factories → button stays disabled with a
   tooltip explaining why.
5. Create Plan after merging two steps → POST body shows the merged
   step as a single `steps[]` entry with name `"A + B"`.
6. Revise qty below `dispatched_qty_kg` → client-side block; backend
   would also 400.
7. Detail cache invalidates after BOM Override → re-expanding the
   same row shows the new values.

### Mapping to the existing source

| New code area     | Reference file                                                                 |
| ----------------- | ------------------------------------------------------------------------------ |
| API client        | `frontend_replica/src/modules/production/fulfillment/fulfillment.js:1700–1730` |
| Listing render    | `fulfillment.js:276–500`                                                       |
| Filter toolbar    | `fulfillment.js:50–160`                                                        |
| Detail expand     | `fulfillment.js:528–900`                                                       |
| Selection model   | `fulfillment.js:1280–1310, 1959+`                                              |
| Create Plan POST  | `fulfillment.js:1961–2131`                                                     |
| Backend list      | `server_replica_new/app/modules/production/router.py:433–649`                  |
| Backend service   | `server_replica_new/app/modules/production/services/fulfillment_v2.py:289–401, 990–1023` |

Keep this section as the source of truth; if you change the listing
contract, update this doc in the same commit.

