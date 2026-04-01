# Part 3: MRP & Indent ✅ COMPLETED

---

## 6. MRP Run ✅

For each approved plan line: look up BOM -> calculate gross requirement with loss allowance -> check off-grade reuse -> compare against floor_inventory + pending POs -> output shortage/surplus per material.

**Implementation:** `services/mrp.py`

### Checklist

- [x] Create `services/mrp.py`
- [x] `run_mrp(conn, plan_id, entity)`:
  - [x] Load all production_plan_line WHERE plan_id AND status='planned'
  - [x] For each plan_line, load BOM lines (bom_id)
  - [x] For each bom_line:
    - [x] Calculate gross_requirement: planned_qty_kg * quantity_per_unit / (1 - loss_pct/100)
    - [x] Check off-grade reuse: IF can_use_offgrade, query offgrade_inventory + offgrade_reuse_rule
    - [x] Calculate net_requirement = gross - offgrade_used
    - [x] Check floor_inventory: on_hand WHERE sku_name ILIKE match AND floor_location IN (rm_store, pm_store)
    - [x] Check pending POs: on_order = SUM(po_line.po_weight WHERE status='pending')
    - [x] Calculate: available = on_hand + on_order, shortage = MAX(0, net - available)
    - [x] Return per-material status (SUFFICIENT / SHORTAGE)
  - [x] Return summary: total_materials, sufficient count, shortage count, total_shortage_kg
- [x] `check_availability(conn, material_sku, qty_needed, entity)`: Quick single-material check
- [x] MRP auto-triggers on plan approval (wired into approve endpoint)
- [x] Router: `POST /api/v1/production/mrp/run`
- [x] Router: `GET /api/v1/production/mrp/availability`

---

## 7. Indent Management ✅

Draft indents created by MRP for shortages. Planner reviews/edits, then sends. Purchase team acknowledges and links to PO.

**Implementation:** `services/indent_manager.py`

### Flow
```
MRP detects shortage
  → Draft indent created (status='draft')
  → Planner reviews/edits qty/date/priority
  → Planner sends (status='raised', alerts created)
  → Purchase acknowledges (status='acknowledged')
  → Purchase links PO (status='po_created')
  → Material received (status='received', production alerted)
```

### Checklist

- [x] Create `services/indent_manager.py`
- [x] `generate_draft_indents(conn, mrp_result, plan_id, entity)`:
  - [x] For each SHORTAGE material: create purchase_indent with status='draft'
  - [x] Generate indent_number: "IND-{YYYYMMDD}-{seq:03d}"
  - [x] Get earliest delivery_deadline from linked so_fulfillment records
  - [x] Return list of draft indents for planner review
- [x] `edit_indent(conn, indent_id, updates)`: Edit draft indent (qty, date, priority). Only when status='draft'
- [x] `send_indent(conn, indent_id)`: Draft → raised, creates 2 alerts (purchase + stores)
- [x] `send_bulk_indents(conn, indent_ids)`: Send multiple drafts at once
- [x] `acknowledge_indent(conn, indent_id, user)`: Raised → acknowledged
- [x] `link_indent_to_po(conn, indent_id, po_reference)`: Acknowledged → po_created
- [x] `on_material_received(conn, material_sku, received_qty, entity)`: Updates indents, alerts production
- [x] Router: `GET /api/v1/production/indents` (list with filters: entity, status, dates, pagination)
- [x] Router: `GET /api/v1/production/indents/{id}` (detail with plan_line info)
- [x] Router: `PUT /api/v1/production/indents/{id}/edit` (edit draft)
- [x] Router: `PUT /api/v1/production/indents/{id}/send` (send draft → raised)
- [x] Router: `POST /api/v1/production/indents/send-bulk` (bulk send)
- [x] Router: `PUT /api/v1/production/indents/{id}/acknowledge` (purchase acknowledges)
- [x] Router: `PUT /api/v1/production/indents/{id}/link-po` (link to PO)
- [x] Router: `GET /api/v1/production/alerts` (list with filters: team, read, entity, pagination)
- [x] Router: `PUT /api/v1/production/alerts/{id}/read` (mark as read)
- [x] Plan approval wired to MRP: approve → run_mrp → generate_draft_indents → return in response

---

## Files Created/Modified

| File | Status |
|------|--------|
| `app/modules/production/services/mrp.py` | ✅ Done — run_mrp (BOM→gross req→offgrade→inventory→PO→shortage), check_availability |
| `app/modules/production/services/indent_manager.py` | ✅ Done — generate_draft, edit, send, send_bulk, acknowledge, link_po, on_received |
| `app/modules/production/router.py` | ✅ Done — 11 new endpoints added, MRP wired into approve |

## Endpoints Summary (11 new, 28 total)

| # | Method | Endpoint | Description |
|---|--------|----------|-------------|
| 18 | POST | `/mrp/run` | Run MRP for a plan → material check + draft indents |
| 19 | GET | `/mrp/availability` | Quick single-material availability check |
| 20 | GET | `/indents` | List indents (paginated, filterable) |
| 21 | GET | `/indents/{id}` | Indent detail with plan line info |
| 22 | PUT | `/indents/{id}/edit` | Edit draft indent (qty, date, priority) |
| 23 | PUT | `/indents/{id}/send` | Send draft → raised, creates alerts |
| 24 | POST | `/indents/send-bulk` | Send multiple drafts at once |
| 25 | PUT | `/indents/{id}/acknowledge` | Purchase acknowledges (raised → acknowledged) |
| 26 | PUT | `/indents/{id}/link-po` | Link to PO (acknowledged → po_created) |
| 27 | GET | `/alerts` | List alerts (paginated, filterable by team/read/entity) |
| 28 | PUT | `/alerts/{id}/read` | Mark alert as read |

## Tables Involved

| Table | Usage |
|-------|-------|
| `production_plan` | Read — get plan entity, verify status |
| `production_plan_line` | Read — approved plan lines with bom_id, planned_qty_kg |
| `bom_header` | Read — item_group for off-grade lookup |
| `bom_line` | Read — materials, qty_per_unit, loss_pct, can_use_offgrade |
| `floor_inventory` | Read — on-hand stock (rm_store, pm_store) |
| `po_line` + `po_header` | Read — on-order stock (pending POs) |
| `offgrade_inventory` | Read — available off-grade stock |
| `offgrade_reuse_rule` | Read — max substitution % rules |
| `so_fulfillment` | Read — delivery deadlines for indent priority |
| `purchase_indent` | Write — draft/raised/acknowledged/po_created/received |
| `store_alert` | Write — shortage alerts to purchase + stores teams |
