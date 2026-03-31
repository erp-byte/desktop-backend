# CANDOR FOODS — Production Planning Module
## Complete Technical Reference Document
### Version 1.0 | March 2026

---

**Base URL:** `/api/v1/production`
**Total Endpoints:** 78
**Total Database Tables:** 34 (+ 7 migrations)
**Total Service Files:** 11
**Total Lines of Code:** 5,955

---

# TABLE OF CONTENTS

1. [Part 1: Master Data Setup](#part-1-master-data-setup)
2. [Part 2: Planning Engine](#part-2-planning-engine)
3. [Part 3: MRP & Indent](#part-3-mrp--indent)
4. [Part 4: Job Card Engine](#part-4-job-card-engine)
5. [Part 5: Inventory & Tracking](#part-5-inventory--tracking)
6. [Part 6: Day-End & Fulfillment](#part-6-day-end--fulfillment)
7. [Part 7: AI & Revision](#part-7-ai--revision)
8. [Complete Endpoint Registry](#complete-endpoint-registry)
9. [Complete Database Schema](#complete-database-schema)

---

# PART 1: MASTER DATA SETUP

## 1.1 Purpose

Ingest BOM (Bill of Materials), machines, machine capacity, and physical stock from Excel files directly into the database at server startup. No manual endpoints — runs once, idempotent.

## 1.2 Data Sources

| File | Sheet | Rows | Target Table |
|------|-------|------|-------------|
| FG_Master_Completion.xlsx | FG_Master_Fill | 1,085 | bom_header + bom_process_route |
| BOM_Enrichment.xlsx | BOM_Enrichment | 4,442 | bom_line |
| Floorwise utility dada.xlsx | floorwise machine list | 86 | machine |
| FG_Master_Completion.xlsx | (derived) | varies | machine_capacity |
| Physical Stock.xlsx | CFPL | 1,945 | floor_inventory |
| A-185 Stock Take.xlsx | Consolidated | varies | floor_inventory |

## 1.3 Database Tables

### bom_header (FG product recipes)

| Column | Type | Description |
|--------|------|-------------|
| bom_id | SERIAL PK | Auto-increment ID |
| fg_sku_name | TEXT NOT NULL | Finished good name |
| customer_name | TEXT | NULL = generic BOM |
| pack_size_kg | NUMERIC(15,3) | Net weight per unit |
| version | INT DEFAULT 1 | BOM version |
| is_active | BOOLEAN DEFAULT TRUE | Active flag |
| effective_from | DATE | Effective start date |
| effective_to | DATE | Effective end date (NULL = no expiry) |
| item_group | TEXT | Product group (CASHEW, DATES, etc.) |
| entity | TEXT | 'cfpl' or 'cdpl' |
| notes | TEXT | Notes |
| sub_group | TEXT | Sub-group (migration added) |
| process_category | TEXT | "Sorting + Roasting + Packaging" |
| business_unit | TEXT | "VA - Nuts & Mixes" |
| factory | TEXT | "W202" / "A185" |
| floors | TEXT[] | Array of floor names |
| machines | TEXT[] | Array of machine names |
| shelf_life_days | INT | Days shelf life |
| gst_rate | NUMERIC(5,3) | GST rate |
| hsn_sac | TEXT | HSN/SAC code |
| inventory_group | TEXT | Inventory group |
| customer_code | TEXT | Customer code |

### bom_line (Materials per BOM)

| Column | Type | Description |
|--------|------|-------------|
| bom_line_id | SERIAL PK | |
| bom_id | INT FK | Links to bom_header |
| line_number | INT | Sequence within BOM |
| material_sku_name | TEXT NOT NULL | Material name |
| item_type | TEXT NOT NULL | 'rm' or 'pm' |
| quantity_per_unit | NUMERIC(15,3) | Qty per 1 unit of FG |
| uom | TEXT | 'Kg' or 'Pcs' |
| loss_pct | NUMERIC(5,3) | Expected loss % |
| godown | TEXT | 'Factory' or 'PM Store' |
| can_use_offgrade | BOOLEAN | Allow off-grade substitution |
| offgrade_max_pct | NUMERIC(5,3) | Max substitution % |
| unit_rate_inr | NUMERIC(15,3) | Cost per unit (migration added) |
| process_stage | TEXT | "Sorting & Packing" (migration added) |

### bom_process_route (Sequential manufacturing steps)

| Column | Type | Description |
|--------|------|-------------|
| route_id | SERIAL PK | |
| bom_id | INT FK | Links to bom_header |
| step_number | INT | 1, 2, 3... |
| process_name | TEXT | "Sorting", "Roasting", "Packaging" |
| stage | TEXT | Lowercase normalized |
| std_time_min | NUMERIC(10,2) | Standard time in minutes |
| loss_pct | NUMERIC(5,3) | Loss at this stage |
| qc_check | TEXT | QC requirement |
| machine_type | TEXT | Machine type needed |

### machine (Physical equipment)

| Column | Type | Description |
|--------|------|-------------|
| machine_id | SERIAL PK | |
| machine_name | TEXT NOT NULL | |
| machine_type | TEXT | sorting_table, sealer, etc. |
| category | TEXT | processing, packaging, quality |
| capable_stages | TEXT[] | Array of stages |
| floor | TEXT | Floor name |
| factory | TEXT | W-202, A-185 |
| status | TEXT DEFAULT 'active' | active/maintenance/retired |
| entity | TEXT | 'cfpl'/'cdpl' |
| allocation | TEXT DEFAULT 'idle' | idle/occupied/maintenance |

### machine_capacity (Per-product per-stage capacity)

| Column | Type | Description |
|--------|------|-------------|
| capacity_id | SERIAL PK | |
| machine_id | INT FK | Links to machine |
| stage | TEXT | sorting, roasting, packaging |
| item_group | TEXT | CASHEW, DATES, SEEDS |
| capacity_kg_per_hr | NUMERIC(15,3) | Throughput rate |
| UNIQUE | | (machine_id, stage, item_group) |

## 1.4 Ingested Data Summary

| Table | Records |
|-------|---------|
| bom_header | 1,084 |
| bom_process_route | 2,300 |
| bom_line | 4,440 |
| machine | 86 |
| machine_capacity | 188 |
| floor_inventory | 923 |

## 1.5 Service File

**File:** `services/master_ingest.py` (630 lines)

| Function | Purpose |
|----------|---------|
| ingest_fg_master() | FG_Master_Fill → bom_header + bom_process_route |
| ingest_bom_lines() | BOM_Enrichment → bom_line (with fuzzy match) |
| ingest_machines() | Floorwise machine list → machine |
| derive_machine_capacity() | FG Master machine x stage x group → machine_capacity |
| ingest_stock() | Physical Stock + A-185 → floor_inventory |
| run_master_ingest() | Orchestrator — idempotent, runs at startup |

## 1.6 Endpoint

| # | Method | Path | Description |
|---|--------|------|-------------|
| 1 | GET | /health | Module health + table counts |

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

# PART 2: PLANNING ENGINE

## 2.1 Purpose

Bridge Sales Orders to production planning. Sync SO lines into fulfillment tracking, generate AI-powered production plans via Claude, and manage plan lifecycle (create, edit, approve, cancel).

## 2.2 Database Tables

### so_fulfillment (Demand tracking)

| Column | Type | Description |
|--------|------|-------------|
| fulfillment_id | SERIAL PK | |
| so_line_id | INT | FK to so_line |
| so_id | INT | FK to so_header |
| financial_year | TEXT | "2025-26" |
| fg_sku_name | TEXT | FG product name |
| customer_name | TEXT | Customer |
| original_qty_kg | NUMERIC(15,3) | Original order qty |
| revised_qty_kg | NUMERIC(15,3) | Revised qty |
| pending_qty_kg | NUMERIC(15,3) | Remaining to produce |
| produced_qty_kg | NUMERIC(15,3) | Produced so far |
| dispatched_qty_kg | NUMERIC(15,3) | Dispatched so far |
| order_status | TEXT | open/partial/fulfilled/carryforward/cancelled |
| delivery_deadline | DATE | Delivery date |
| priority | INT | 1=highest, 10=lowest |
| carryforward_from_id | INT FK | Linked to old FY record |
| entity | TEXT | cfpl/cdpl |
| UNIQUE | | (so_line_id, financial_year) |

### so_revision_log (Audit trail)

| Column | Type | Description |
|--------|------|-------------|
| revision_id | SERIAL PK | |
| fulfillment_id | INT FK | |
| revision_type | TEXT | qty_change/date_change/carryforward/cancel |
| old_value | TEXT | Previous value |
| new_value | TEXT | New value |
| reason | TEXT | Reason for change |
| revised_by | TEXT | Who changed it |

### production_plan (Plan header)

| Column | Type | Description |
|--------|------|-------------|
| plan_id | SERIAL PK | |
| plan_name | TEXT | |
| entity | TEXT | cfpl/cdpl |
| plan_type | TEXT | daily/weekly/full |
| plan_date | DATE | When plan was created |
| date_from | DATE | Plan covers from |
| date_to | DATE | Plan covers to |
| status | TEXT | draft/approved/executed/cancelled/revised |
| ai_generated | BOOLEAN | TRUE if Claude generated |
| ai_analysis_json | JSONB | Full Claude response |
| revision_number | INT | 1, 2, 3... |
| previous_plan_id | INT FK | Chain of revisions |
| approved_by | TEXT | |
| approved_at | TIMESTAMPTZ | |

### production_plan_line (Plan line items)

| Column | Type | Description |
|--------|------|-------------|
| plan_line_id | SERIAL PK | |
| plan_id | INT FK | |
| fg_sku_name | TEXT | FG product |
| customer_name | TEXT | Customer |
| bom_id | INT FK | BOM to use |
| planned_qty_kg | NUMERIC(15,3) | Planned quantity |
| planned_qty_units | INT | Planned units |
| machine_id | INT FK | Assigned machine |
| priority | INT | Production priority |
| shift | TEXT | day/night |
| stage_sequence | TEXT[] | Process stages |
| estimated_hours | NUMERIC(10,2) | Estimated time |
| linked_so_fulfillment_ids | INT[] | Linked fulfillment IDs |
| reasoning | TEXT | AI reasoning |
| status | TEXT | planned/in_progress/completed/cancelled/deferred |

### ai_recommendation (Claude interaction log)

| Column | Type | Description |
|--------|------|-------------|
| recommendation_id | SERIAL PK | |
| recommendation_type | TEXT | daily_plan/weekly_plan/plan_revision/loss_anomaly |
| entity | TEXT | |
| prompt_text | TEXT | |
| response_text | TEXT | |
| response_json | JSONB | |
| tokens_used | INT | |
| latency_ms | INT | |
| model_used | TEXT | |
| status | TEXT | generated/accepted/rejected |
| feedback | TEXT | User feedback |
| plan_id | INT FK | Linked plan |

## 2.3 Service Files

**File:** `services/fulfillment.py` (281 lines)

| Function | Purpose |
|----------|---------|
| sync_fulfillment(conn, entity) | Sync SO lines → so_fulfillment |
| get_demand_summary(conn, entity, fy) | Aggregate demand by product+customer |
| get_fulfillment_list(conn, filters) | Paginated list with filters |
| get_fy_review(conn, entity, fy) | Unfulfilled orders for FY close |
| carryforward_orders(conn, ids, new_fy) | Bulk carry forward to new FY |
| revise_order(conn, id, qty, date) | Revise with audit log |

**File:** `services/ai_planner.py` (700 lines)

| Function | Purpose |
|----------|---------|
| collect_planning_context(conn, entity, date, ids) | Gather demand, inventory, machines, BOMs |
| call_claude(settings, prompt, data) | Call Claude API, parse JSON response |
| create_plan_from_ai(conn, entity, type, dates, result) | Create plan + lines from AI response |
| DAILY_PLAN_PROMPT | System prompt for daily plans |
| WEEKLY_PLAN_PROMPT | System prompt for weekly plans |
| PLAN_REVISION_PROMPT | System prompt for revisions (Part 7) |
| collect_revision_context() | Context for plan revision (Part 7) |
| create_revised_plan() | Create revised plan (Part 7) |

## 2.4 Endpoints (16)

### Fulfillment (6)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 2 | POST | /fulfillment/sync | `{"entity":"cfpl"}` | `{"synced":150,"skipped":30,"total":180}` |
| 3 | GET | /fulfillment?entity=cfpl&status=open&page=1 | query params | `{"results":[...],"pagination":{...}}` |
| 4 | GET | /fulfillment/demand-summary?entity=cfpl | query params | `[{"fg_sku_name":"...","total_qty_kg":1500,"order_count":3}]` |
| 5 | GET | /fulfillment/fy-review?entity=cfpl | query params | `[{"fulfillment_id":1,"fg_sku_name":"...","pending_qty_kg":200}]` |
| 6 | POST | /fulfillment/carryforward | `{"fulfillment_ids":[1,5],"new_fy":"2026-27","revised_by":"Name"}` | `{"carried":2,"new_fy":"2026-27"}` |
| 7 | PUT | /fulfillment/{id}/revise | `{"new_qty":600,"new_date":"2026-04-10","reason":"Increased","revised_by":"Name"}` | `{"fulfillment_id":1,"revised":true}` |

### Plans (10)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 8 | POST | /plans/generate-daily | `{"entity":"cfpl","date":"2026-04-01","fulfillment_ids":[1,2,5]}` | `{"plan_id":1,"status":"draft","lines":3,"material_check":[...],"risk_flags":[...]}` |
| 9 | POST | /plans/generate-weekly | `{"entity":"cfpl","date_from":"2026-04-01","date_to":"2026-04-07","fulfillment_ids":[...]}` | Same as daily |
| 10 | POST | /plans/create | `{"entity":"cfpl","plan_name":"April Plan","plan_type":"daily","date_from":"2026-04-01","date_to":"2026-04-01"}` | `{"plan_id":2,"status":"draft"}` |
| 11 | GET | /plans?entity=cfpl&status=draft&page=1 | query params | `{"results":[...],"pagination":{...}}` |
| 12 | GET | /plans/{plan_id} | - | Full plan with lines + material_check + risk_flags |
| 13 | PUT | /plans/{id}/lines/{lid} | `{"priority":1,"machine_id":5}` (partial update) | `{"plan_line_id":2,"updated":true,"fields_changed":["priority","machine_id"]}` |
| 14 | POST | /plans/{id}/lines | `{"fg_sku_name":"Cashew","planned_qty_kg":500}` | `{"plan_line_id":5,"plan_id":1}` |
| 15 | DELETE | /plans/{id}/lines/{lid} | - | `{"deleted":true}` |
| 16 | PUT | /plans/{id}/approve?approved_by=Name | query param | `{"plan_id":1,"status":"approved","mrp_summary":{...},"draft_indents":[...]}` |
| 17 | PUT | /plans/{id}/cancel | - | `{"plan_id":1,"status":"cancelled"}` |

## 2.5 Key Flow

```
1. POST /fulfillment/sync  →  Sync all SO lines to so_fulfillment
2. GET /fulfillment         →  Planner views pending demand
3. POST /plans/generate-daily  →  Claude AI creates schedule
4. GET /plans/{id}          →  Planner reviews plan + material check
5. PUT /plans/{id}/lines/{lid}  →  Planner edits priority/machine/qty
6. PUT /plans/{id}/approve  →  Approves → MRP runs → draft indents created
```

---

# PART 3: MRP & INDENT

## 3.1 Purpose

Material Requirements Planning — checks material availability against BOMs, generates draft purchase indents for shortages, manages indent lifecycle from draft through PO linkage to material receipt.

## 3.2 Database Tables

### purchase_indent

| Column | Type | Description |
|--------|------|-------------|
| indent_id | SERIAL PK | |
| indent_number | TEXT UNIQUE | "IND-20260401-001" |
| material_sku_name | TEXT | Material needed |
| required_qty_kg | NUMERIC(15,3) | Shortage quantity |
| required_by_date | DATE | Earliest deadline |
| priority | INT | 1-10 |
| plan_line_id | INT FK | Linked plan line |
| po_reference | TEXT | Linked PO number |
| status | TEXT | draft/raised/acknowledged/po_created/received/cancelled |
| acknowledged_by | TEXT | |
| acknowledged_at | TIMESTAMPTZ | |
| entity | TEXT | |

### store_alert

| Column | Type | Description |
|--------|------|-------------|
| alert_id | SERIAL PK | |
| alert_type | TEXT | material_shortage/indent_raised/material_received/force_unlock/internal_discrepancy/material_idle_warning/balance_variance/balance_scan_missing |
| target_team | TEXT | purchase/stores/production/qc |
| message | TEXT | Alert message |
| related_id | INT | Generic FK |
| related_type | TEXT | indent/job_card/plan/inventory/balance_scan/discrepancy |
| is_read | BOOLEAN | |
| entity | TEXT | |

## 3.3 MRP Calculation Formula

```
FOR each plan_line:
  FOR each bom_line:
    net_need = planned_qty_kg * quantity_per_unit
    gross_req = net_need / (1 - loss_pct / 100)

    IF can_use_offgrade:
      offgrade_use = MIN(offgrade_available, gross_req * offgrade_max_pct / 100)
      final_need = gross_req - offgrade_use
    ELSE:
      final_need = gross_req

    on_hand = SUM(floor_inventory WHERE sku ILIKE match AND location IN (rm_store, pm_store))
    on_order = SUM(po_line.po_weight WHERE sku ILIKE match AND status = 'pending')
    available = on_hand + on_order
    shortage = MAX(0, final_need - available)
    status = 'SUFFICIENT' if shortage == 0 else 'SHORTAGE'
```

## 3.4 Indent Lifecycle

```
MRP detects shortage → Draft indent created (status='draft')
  → Planner reviews/edits qty/date/priority
  → Planner sends (status='raised', alerts created for purchase + stores)
  → Purchase acknowledges (status='acknowledged')
  → Purchase links PO (status='po_created')
  → Material received (status='received', production alerted)
```

## 3.5 Service Files

**File:** `services/mrp.py` (184 lines)

| Function | Purpose |
|----------|---------|
| run_mrp(conn, plan_id, entity) | Full MRP run for approved plan |
| check_availability(conn, material, qty, entity) | Quick single-material check |

**File:** `services/indent_manager.py` (295 lines)

| Function | Purpose |
|----------|---------|
| generate_draft_indents(conn, mrp_result, plan_id, entity) | Create draft indents from MRP shortages |
| edit_indent(conn, indent_id, updates) | Edit draft indent |
| send_indent(conn, indent_id) | Draft → raised + create alerts |
| send_bulk_indents(conn, indent_ids) | Bulk send |
| acknowledge_indent(conn, indent_id, user) | Raised → acknowledged |
| link_indent_to_po(conn, indent_id, po_ref) | Acknowledged → po_created |
| on_material_received(conn, material, qty, entity) | Close indent + alert production |

## 3.6 Endpoints (11)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 18 | POST | /mrp/run | `{"plan_id":1}` | `{"plan_id":1,"materials":[{"material_sku_name":"Cashew 320","status":"SHORTAGE","shortage_kg":163.3}],"summary":{"sufficient":2,"shortage":4},"draft_indents":[...]}` |
| 19 | GET | /mrp/availability?material=Cashew&qty=163&entity=cfpl | query | `{"material":"Cashew","needed_kg":163,"on_hand_kg":0,"shortage_kg":163,"status":"SHORTAGE"}` |
| 20 | GET | /indents?entity=cfpl&status=draft | query | `{"results":[...],"pagination":{...}}` |
| 21 | GET | /indents/{id} | - | Indent detail + plan_line info |
| 22 | PUT | /indents/{id}/edit | `{"required_qty_kg":200,"priority":3}` | `{"indent_id":1,"updated":true,"fields_changed":["required_qty_kg","priority"]}` |
| 23 | PUT | /indents/{id}/send | - | `{"indent_id":1,"status":"raised","alerts_created":2}` |
| 24 | POST | /indents/send-bulk | `{"indent_ids":[1,2,3]}` | `{"sent":3,"alerts_created":6,"failed":0}` |
| 25 | PUT | /indents/{id}/acknowledge | `{"acknowledged_by":"Purchase Manager"}` | `{"indent_id":1,"status":"acknowledged"}` |
| 26 | PUT | /indents/{id}/link-po | `{"po_reference":"CFPL-CF/PO/2025-26/03500"}` | `{"indent_id":1,"status":"po_created"}` |
| 27 | GET | /alerts?target_team=purchase&entity=cfpl | query | `{"results":[...],"pagination":{...}}` |
| 28 | PUT | /alerts/{id}/read | - | `{"alert_id":1,"is_read":true}` |

---

# PART 4: JOB CARD ENGINE

## 4.1 Purpose

Core production execution — create production orders from approved plans, generate sequential job cards with lock/unlock mechanics, handle QR material receipt, record output, sign-offs, and annexures matching the CFC/PRD/JC/V3.0 PDF format.

## 4.2 Database Tables

### production_order

| Column | Type | Description |
|--------|------|-------------|
| prod_order_id | SERIAL PK | |
| prod_order_number | TEXT UNIQUE | "PRD-2026-0001" |
| plan_line_id | INT FK | |
| bom_id | INT FK | |
| fg_sku_name | TEXT | |
| customer_name | TEXT | |
| batch_number | TEXT UNIQUE | "B2026-0001" |
| batch_size_kg | NUMERIC(15,3) | |
| net_wt_per_unit | NUMERIC(15,3) | Pack size |
| best_before | DATE | |
| total_stages | INT | |
| status | TEXT | created/job_cards_issued/in_progress/completed/cancelled |
| entity | TEXT | |
| factory | TEXT | |
| floor | TEXT | |

### job_card (30 columns)

| Column | Type | Description |
|--------|------|-------------|
| job_card_id | SERIAL PK | |
| job_card_number | TEXT UNIQUE | "PRD-2026-0001/1" |
| prod_order_id | INT FK | |
| bom_id | INT FK | |
| step_number | INT | 1, 2, 3... |
| process_name | TEXT | Sorting, Roasting, etc. |
| stage | TEXT | Lowercase stage |
| fg_sku_name | TEXT | Denormalized |
| customer_name | TEXT | |
| batch_number | TEXT | |
| batch_size_kg | NUMERIC(15,3) | |
| machine_id | INT FK | |
| assigned_to_team_leader | TEXT | |
| team_members | TEXT[] | |
| is_locked | BOOLEAN | TRUE = locked |
| locked_reason | TEXT | awaiting_previous_stage/material_pending/discrepancy_hold |
| force_unlocked | BOOLEAN | |
| force_unlock_by | TEXT | Authority name |
| force_unlock_reason | TEXT | |
| force_unlock_at | TIMESTAMPTZ | |
| status | TEXT | locked/unlocked/assigned/material_received/in_progress/completed/closed |
| start_time | TIMESTAMPTZ | |
| end_time | TIMESTAMPTZ | |
| total_time_min | NUMERIC(10,2) | |
| factory | TEXT | |
| floor | TEXT | |
| entity | TEXT | |
| sales_order_ref | TEXT | "SO-2026-0118" |
| article_code | TEXT | "CFC-128" |
| mrp | NUMERIC(15,3) | MRP price |
| ean | TEXT | Barcode |
| bu | TEXT | Business unit |
| fumigation | BOOLEAN | Checkbox |
| metal_detector_used | BOOLEAN | Checkbox |
| roasting_pasteurization | BOOLEAN | Checkbox |
| control_sample_gm | NUMERIC(10,2) | Sample weight |
| magnets_used | BOOLEAN | Checkbox |

### job_card child tables (9)

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| job_card_rm_indent | material_sku_name, reqd_qty, loss_pct, gross_qty, issued_qty, batch_no, scanned_box_ids, variance, status | RM requirements |
| job_card_pm_indent | Same as RM | PM requirements |
| job_card_process_step | step_number, process_name, machine_name, std_time_min, qc_check, loss_pct, operator_name, qc_sign_at, time_done, status | Process steps |
| job_card_output | fg_expected/actual (units+kg), rm_consumed_kg, material_return_kg, rejection_kg/reason, process_loss_kg/pct, offgrade_kg/category, dispatch_qty | Section 5 output |
| job_card_environment | parameter_name, value | Annexure C |
| job_card_metal_detection | check_type, fe/nfe/ss_pass, failed_units, seal_check, seal/wt_failed_units, dough/oven/baking_temp_c | Annexure A/B |
| job_card_weight_check | sample_number, net/gross_weight, leak_test_pass, target_wt_g, tolerance_g, accept_range_min/max | Annexure B |
| job_card_loss_reconciliation | loss_category, budgeted_loss_pct/kg, actual_loss_kg, variance_kg, remarks | Annexure D |
| job_card_remarks | remark_type, content, recorded_by | Annexure E |
| job_card_sign_off | sign_off_type, name, signed_at | Section 6 + Annexure page |

## 4.3 Job Card Locking Logic

```
Production Order → Job Cards created:
  JC #1 (step 1): is_locked=FALSE, status='unlocked'     ← FIRST STAGE
  JC #2 (step 2): is_locked=TRUE,  status='locked'
  JC #3 (step 3): is_locked=TRUE,  status='locked'

When JC #1 completes:
  → JC #2 automatically unlocked (status='unlocked')
  → Floor movement created (SFG transfer)
  → Alert: "JC #2 unlocked"

When JC #3 (last stage) completes:
  → Production order completed
  → FG moved to fg_store
  → so_fulfillment.produced_qty_kg updated
  → process_loss auto-recorded
  → offgrade_inventory auto-created
```

## 4.4 Service Files

**File:** `services/job_card_engine.py` (676 lines)

| Function | Purpose |
|----------|---------|
| create_production_orders(conn, plan_id, entity) | Plan lines → production orders |
| create_job_cards(conn, prod_order_id) | Production order → sequential job cards |
| assign_job_card() | Assign team leader |
| start_job_card() | Start production |
| complete_process_step() | Complete a step with QC sign |
| record_output() | Record Section 5 data |
| complete_job_card() | Complete → auto-unlock next |
| sign_off() | Record sign-off |
| close_job_card() | Close after all sign-offs |
| force_unlock() | Bypass lock with authority |
| get_job_card_detail() | Full detail matching PDF |

**File:** `services/qr_service.py` (205 lines)

| Function | Purpose |
|----------|---------|
| receive_material_via_qr(conn, jc_id, box_ids, entity) | Scan po_box QR codes against indent lines |

## 4.5 Endpoints (21)

### Production Orders (3)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 29 | POST | /orders/create-from-plan | `{"plan_id":1}` | `{"orders_created":3,"orders":[{"prod_order_id":1,"prod_order_number":"PRD-2026-0001","batch_number":"B2026-0001","total_stages":3}]}` |
| 30 | GET | /orders?entity=cfpl&status=created | query | Paginated list |
| 31 | GET | /orders/{id} | - | Order detail + job cards list |

### Job Cards (3)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 32 | POST | /job-cards/generate | `{"prod_order_id":1}` | `{"prod_order_id":1,"job_cards":[{"job_card_id":1,"job_card_number":"PRD-2026-0001/1","status":"unlocked","rm_indent_lines":2,"pm_indent_lines":0}]}` |
| 33 | GET | /job-cards?status=unlocked&entity=cfpl | query | Paginated list |
| 34 | GET | /job-cards/{id} | - | Full detail: section_1_product, section_2a_rm_indent, section_2b_pm_indent, section_3_team, section_4_process_steps, section_5_output, section_6_sign_offs, annexure_a_b, annexure_b, annexure_c, annexure_d, annexure_e |

### Lifecycle (8)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 35 | PUT | /job-cards/{id}/assign | `{"team_leader":"Ramesh","team_members":["Sunil","Arun"]}` | `{"status":"assigned"}` |
| 36 | POST | /job-cards/{id}/receive-material | `{"box_ids":["97598567-1","97598567-2"]}` | `{"boxes_accepted":2,"total_issued_kg":20.0,"material_status":"partial","indent_summary":[...]}` |
| 37 | PUT | /job-cards/{id}/start | - | `{"status":"in_progress","start_time":"..."}` |
| 38 | PUT | /job-cards/{id}/complete-step | `{"step_number":1,"operator_name":"Sunil","qc_passed":true}` | `{"step_number":1,"status":"completed"}` |
| 39 | PUT | /job-cards/{id}/record-output | `{"fg_actual_units":2480,"fg_actual_kg":1240.0,"rm_consumed_kg":1275.0,"rejection_kg":10.0,"process_loss_kg":25.0,"offgrade_kg":15.0,"offgrade_category":"broken"}` | `{"output_id":1,"saved":true}` |
| 40 | PUT | /job-cards/{id}/complete | - | `{"status":"completed","next_unlocked":{"job_card_id":2,"status":"unlocked"},"order_completed":false}` |
| 41 | PUT | /job-cards/{id}/sign-off | `{"sign_off_type":"production_incharge","name":"Ramesh"}` | `{"signed":true}` |
| 42 | PUT | /job-cards/{id}/force-unlock | `{"authority":"Plant Head","reason":"Urgent delivery"}` | `{"status":"unlocked","force_unlocked":true,"warning":"Previous stage has no output"}` |

### Annexures (5)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 43 | POST | /job-cards/{id}/environment | `{"parameters":[{"parameter_name":"humidity_pct","value":"45"},{"parameter_name":"rpm","value":"1200"}]}` | `{"saved":2}` |
| 44 | POST | /job-cards/{id}/metal-detection | `{"check_type":"pre_packaging","fe_pass":true,"nfe_pass":true,"ss_pass":true,"seal_check":true}` | `{"detection_id":1}` |
| 45 | POST | /job-cards/{id}/weight-checks | `{"target_wt_g":500,"tolerance_g":2,"samples":[{"sample_number":1,"net_weight":500.5,"leak_test_pass":true}]}` | `{"saved":1}` |
| 46 | POST | /job-cards/{id}/loss-reconciliation | `{"entries":[{"loss_category":"sorting_rejection","budgeted_loss_pct":2.0,"budgeted_loss_kg":25.5,"actual_loss_kg":22.0}]}` | `{"saved":1,"total_budgeted_kg":25.5,"total_actual_kg":22.0}` |
| 47 | POST | /job-cards/{id}/remarks | `{"remark_type":"observation","content":"Slight discoloration","recorded_by":"Ramesh"}` | `{"remark_id":1}` |

### Dashboards (2)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 48 | GET | /job-cards/team-dashboard?team_leader=Ramesh&entity=cfpl | query | Priority-sorted job cards for team leader |
| 49 | GET | /job-cards/floor-dashboard?floor=tarraes&entity=cfpl | query | All job cards on a floor |

---

# PART 5: INVENTORY & TRACKING

## 5.1 Purpose

Track stock across floor locations with validated transitions, detect idle materials, manage off-grade stock and reuse rules, record and analyze process losses.

## 5.2 Database Tables

### floor_inventory

| Column | Type | Description |
|--------|------|-------------|
| inventory_id | SERIAL PK | |
| sku_name | TEXT | Material name |
| item_type | TEXT | rm/pm/wip/fg |
| floor_location | TEXT | rm_store/pm_store/production_floor/fg_store |
| quantity_kg | NUMERIC(15,3) | Current quantity |
| lot_number | TEXT | Lot tracking |
| entity | TEXT | |
| last_updated | TIMESTAMPTZ | For idle detection |
| UNIQUE | | (sku_name, floor_location, lot_number, entity) |

### floor_movement (Audit trail)

| Column | Type | Description |
|--------|------|-------------|
| movement_id | SERIAL PK | |
| sku_name | TEXT | |
| from_location | TEXT | |
| to_location | TEXT | |
| quantity_kg | NUMERIC(15,3) | |
| reason | TEXT | production/return/receipt/dispatch/balance_adjustment |
| job_card_id | INT FK | |
| scanned_qr_codes | TEXT[] | |
| entity | TEXT | |
| moved_by | TEXT | |

### offgrade_inventory, offgrade_reuse_rule, offgrade_consumption

Used for off-grade tracking and reuse. See Part 1 schema for full columns.

### process_loss

| Column | Type | Description |
|--------|------|-------------|
| loss_id | SERIAL PK | |
| job_card_id | INT FK | |
| product_name | TEXT | |
| stage | TEXT | |
| loss_kg | NUMERIC(15,3) | |
| loss_pct | NUMERIC(5,3) | |
| loss_category | TEXT | |
| batch_number | TEXT | |
| production_date | DATE | |
| entity | TEXT | |

## 5.3 Floor Transition Rules

| From | To | When |
|------|----|------|
| rm_store | production_floor | Job card material receipt |
| pm_store | production_floor | Packaging stage |
| production_floor | fg_store | FG output |
| production_floor | offgrade | Off-grade captured |
| production_floor | rm_store | Material return (unused) |
| offgrade | production_floor | Off-grade reuse |

## 5.4 Service Files

**File:** `services/floor_tracker.py` (191 lines)

| Function | Purpose |
|----------|---------|
| move_material() | Validated movement with audit trail |
| get_floor_summary() | Aggregated stock per floor |
| get_floor_detail() | Items on a floor (paginated) |
| get_movement_history() | Filtered audit trail |

**File:** `services/idle_checker.py` (127 lines)

| Function | Purpose |
|----------|---------|
| check_idle_materials() | Flag 3-day warning / 5-day critical |

## 5.5 Endpoints (13)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 50 | GET | /floor-inventory?entity=cfpl&floor_location=rm_store | query | Paginated inventory list |
| 51 | GET | /floor-inventory/summary?entity=cfpl | query | `[{"floor_location":"rm_store","item_count":73,"total_kg":10586}]` |
| 52 | POST | /floor-inventory/move | `{"sku_name":"Cashew 320","from_location":"rm_store","to_location":"production_floor","quantity_kg":100,"entity":"cfpl"}` | `{"movement_id":1,"moved":true}` |
| 53 | GET | /floor-inventory/movements?entity=cfpl | query | Paginated movement history |
| 54 | POST | /floor-inventory/check-idle?entity=cfpl | query | `{"warnings":5,"criticals":2,"alerts_created":7}` |
| 55 | GET | /offgrade/inventory?entity=cfpl | query | Paginated off-grade list |
| 56 | GET | /offgrade/rules | - | All reuse rules |
| 57 | POST | /offgrade/rules/create | `{"source_item_group":"CASHEW","target_item_group":"TRAIL MIX","max_substitution_pct":15}` | `{"rule_id":1}` |
| 58 | PUT | /offgrade/rules/{id} | `{"max_substitution_pct":20}` | `{"rule_id":1,"updated":true}` |
| 59 | GET | /loss/analysis?entity=cfpl&group_by=product | query | `[{"group_key":"Cashew","batch_count":10,"avg_loss_pct":2.1,"total_loss_kg":150}]` |
| 60 | GET | /loss/anomalies?entity=cfpl | query | Batches with loss > 2x average |
| 61 | GET | /yield/summary?entity=cfpl | query | Yield by product/period |

---

# PART 6: DAY-END & FULFILLMENT

## 6.1 Purpose

End-of-day operations: record dispatch quantities, physical balance scans per floor, variance reconciliation, and fulfillment cancellation.

## 6.2 Database Tables

### day_end_balance_scan

| Column | Type | Description |
|--------|------|-------------|
| scan_id | SERIAL PK | |
| floor_location | TEXT | rm_store/pm_store/production_floor/fg_store |
| scan_date | DATE | |
| submitted_by | TEXT | |
| submitted_at | TIMESTAMPTZ | |
| reviewed_by | TEXT | |
| reviewed_at | TIMESTAMPTZ | |
| total_system_qty | NUMERIC(15,3) | |
| total_scanned_qty | NUMERIC(15,3) | |
| total_variance | NUMERIC(15,3) | |
| status | TEXT | pending/submitted/variance_flagged/reconciled |
| entity | TEXT | |
| UNIQUE | | (floor_location, scan_date, entity) |

### day_end_balance_scan_line

| Column | Type | Description |
|--------|------|-------------|
| scan_line_id | SERIAL PK | |
| scan_id | INT FK | |
| sku_name | TEXT | |
| system_qty_kg | NUMERIC(15,3) | System inventory |
| scanned_qty_kg | NUMERIC(15,3) | Physical count |
| variance_kg | NUMERIC(15,3) | scanned - system |
| variance_pct | NUMERIC(5,3) | variance/system * 100 |
| scanned_box_ids | TEXT[] | QR codes scanned |
| variance_reason | TEXT | |
| corrective_action | TEXT | |
| status | TEXT | ok/variance_detected/reconciled |

## 6.3 Balance Scan Flow

```
4 floors must submit daily (rm_store, pm_store, production_floor, fg_store)

SUBMIT: Physical count per floor
  → System auto-calculates variance per item
  → |variance| > 2%: status='variance_detected', alert created
  → All items OK: status='submitted'
  → Any variance: status='variance_flagged'

RECONCILE: Manager reviews + approves
  → floor_inventory adjusted to match physical count
  → floor_movement created (reason='balance_adjustment')
  → status='reconciled'

CHECK MISSING: End of day
  → Alert for each floor that hasn't submitted
  → Escalation to production incharge
```

## 6.4 Service File

**File:** `services/day_end.py` (364 lines)

| Function | Purpose |
|----------|---------|
| get_day_end_summary() | Completed orders + totals for today |
| bulk_dispatch() | Bulk update dispatch_qty + fulfillment + inventory |
| submit_balance_scan() | Physical count submission with variance detection |
| get_scan_status() | Per-floor submission status |
| get_scan_detail() | Scan detail with all lines |
| reconcile_scan() | Adjust inventory to match physical |
| check_missing_scans() | Alert for missing scans |

## 6.5 Endpoints (8)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 62 | GET | /day-end/summary?entity=cfpl | query | `{"completed_orders":8,"total_fg_output_kg":6420,"total_dispatch_kg":6400,"items":[...]}` |
| 63 | PUT | /day-end/dispatch | `{"dispatches":[{"job_card_id":1,"dispatch_qty":1240}],"entity":"cfpl"}` | `{"updated":3}` |
| 64 | POST | /balance-scan/submit | `{"floor_location":"rm_store","entity":"cfpl","submitted_by":"Store Team","scan_lines":[{"sku_name":"Cashew","scanned_qty_kg":100}]}` | `{"scan_id":1,"variance_flags":2,"status":"variance_flagged"}` |
| 65 | GET | /balance-scan/status?entity=cfpl | query | `[{"floor_location":"rm_store","submitted":true,"status":"submitted"},{"floor_location":"pm_store","submitted":false,"status":"pending"}]` |
| 66 | GET | /balance-scan/{id} | - | Scan detail with all line items |
| 67 | PUT | /balance-scan/{id}/reconcile | `{"reviewed_by":"Manager"}` | `{"scan_id":1,"status":"reconciled","adjustments":3}` |
| 68 | POST | /balance-scan/check-missing?entity=cfpl | query | `{"missing":["pm_store","fg_store"],"alerts_created":4}` |
| 69 | POST | /fulfillment/cancel | `{"fulfillment_ids":[1,5],"reason":"Customer cancelled","cancelled_by":"Planner"}` | `{"cancelled":2}` |

---

# PART 7: AI & REVISION

## 7.1 Purpose

Plan revision (adhoc orders, material changes), internal discrepancy management (RM grade mismatch, machine breakdown, QC failure), and AI recommendation tracking.

## 7.2 Database Tables

### discrepancy_report

| Column | Type | Description |
|--------|------|-------------|
| discrepancy_id | SERIAL PK | |
| discrepancy_type | TEXT | rm_grade_mismatch/rm_qc_failure/rm_expired/machine_breakdown/contamination/short_delivery |
| severity | TEXT | critical/major/minor |
| affected_material | TEXT | |
| affected_machine_id | INT FK | |
| affected_job_card_ids | INT[] | |
| affected_plan_line_ids | INT[] | |
| details | TEXT | |
| total_affected_qty_kg | NUMERIC(15,3) | |
| customer_impact | TEXT | |
| resolution_type | TEXT | material_substituted/machine_rescheduled/deferred/cancelled_replanned/proceed_with_deviation |
| resolution_details | TEXT | |
| reported_by | TEXT | |
| resolved_by | TEXT | |
| resolved_at | TIMESTAMPTZ | |
| status | TEXT | open/investigating/resolved/closed |
| entity | TEXT | |

## 7.3 Discrepancy Flow

```
1. REPORT: QC/Stores/Floor Manager reports discrepancy
   → Auto-identify impacted job cards + plan lines
   → Auto-hold affected JCs (locked with 'discrepancy_hold')
   → Alert production team

2. AUTO-HOLD:
   - unlocked/assigned/material_received → locked
   - in_progress → alert only (don't auto-lock)

3. RESOLVE (5 options):
   A. Substitute Material → unlock with new material
   B. Reschedule Machine → unlock with new machine
   C. Defer → keep locked, raise indent
   D. Cancel & Re-Plan → cancel JCs, trigger Claude revision
   E. Proceed with Deviation → unlock, record in Annexure E
```

## 7.4 Plan Revision Flow

```
1. Planner triggers revision with change_event description
2. System collects: current plan + job card statuses + inventory + machines
3. Claude analyzes and returns per-line actions: keep/reschedule/cancel/add
4. New plan created (revision_number++, previous_plan_id linked)
5. Old plan status = 'revised'
6. New plan goes through same approve → MRP → job card flow
```

## 7.5 Service Files

**File:** `services/ai_planner.py` (extended, 700 lines total)

| Function | Purpose |
|----------|---------|
| PLAN_REVISION_PROMPT | System prompt for revisions |
| collect_revision_context() | Current plan + change context |
| create_revised_plan() | Process Claude's revision |

**File:** `services/discrepancy_manager.py` (240 lines)

| Function | Purpose |
|----------|---------|
| report_discrepancy() | Report + auto-hold + alert |
| get_discrepancy_detail() | Detail with affected JCs |
| resolve_discrepancy() | Resolve with 5 resolution types |

## 7.6 Endpoints (8)

| # | Method | Path | Request | Response |
|---|--------|------|---------|----------|
| 71 | POST | /plans/revise | `{"plan_id":1,"change_event":"New urgent SO from D-Mart for 500kg Cashew"}` | `{"old_plan_id":1,"new_plan_id":2,"revision_number":2,"lines_kept":2,"lines_added":1,"lines_cancelled":0}` |
| 72 | GET | /plans/{id}/revision-history | - | `{"plan_id":1,"revision_chain":[{"plan_id":1,"revision_number":1},{"plan_id":2,"revision_number":2}]}` |
| 73 | POST | /discrepancy/report | `{"discrepancy_type":"rm_grade_mismatch","severity":"major","affected_material":"Cashew 320","details":"Received Grade B instead of A","reported_by":"QC Inspector","entity":"cfpl"}` | `{"discrepancy_id":1,"affected_job_cards":3,"job_cards_held":2,"job_cards_alerted":1}` |
| 74 | GET | /discrepancy?entity=cfpl&status=open | query | Paginated list |
| 75 | GET | /discrepancy/{id} | - | Detail with affected JC details |
| 76 | PUT | /discrepancy/{id}/resolve | `{"resolution_type":"material_substituted","resolution_details":"Used off-grade at 15%","resolved_by":"Planner"}` | `{"discrepancy_id":1,"status":"resolved","job_cards_unlocked":2}` |
| 77 | GET | /ai/recommendations?entity=cfpl | query | Paginated AI recommendation log |
| 78 | PUT | /ai/recommendations/{id}/feedback | `{"status":"accepted","feedback":"Good plan"}` | `{"recommendation_id":1,"status":"accepted"}` |

---

# COMPLETE ENDPOINT REGISTRY

## All 78 Endpoints

| # | Method | Full Path | Part | Description |
|---|--------|-----------|------|-------------|
| 1 | GET | /api/v1/production/health | 1 | Module health + table counts |
| 2 | POST | /api/v1/production/fulfillment/sync | 2 | Sync SO lines |
| 3 | GET | /api/v1/production/fulfillment | 2 | List fulfillment |
| 4 | GET | /api/v1/production/fulfillment/demand-summary | 2 | Demand summary |
| 5 | GET | /api/v1/production/fulfillment/fy-review | 2 | FY close review |
| 6 | POST | /api/v1/production/fulfillment/carryforward | 2 | Carry forward |
| 7 | PUT | /api/v1/production/fulfillment/{id}/revise | 2 | Revise order |
| 8 | POST | /api/v1/production/plans/generate-daily | 2 | AI daily plan |
| 9 | POST | /api/v1/production/plans/generate-weekly | 2 | AI weekly plan |
| 10 | POST | /api/v1/production/plans/create | 2 | Manual plan |
| 11 | GET | /api/v1/production/plans | 2 | List plans |
| 12 | GET | /api/v1/production/plans/{id} | 2 | Plan detail |
| 13 | PUT | /api/v1/production/plans/{id}/lines/{lid} | 2 | Edit plan line |
| 14 | POST | /api/v1/production/plans/{id}/lines | 2 | Add plan line |
| 15 | DELETE | /api/v1/production/plans/{id}/lines/{lid} | 2 | Remove plan line |
| 16 | PUT | /api/v1/production/plans/{id}/approve | 2 | Approve plan |
| 17 | PUT | /api/v1/production/plans/{id}/cancel | 2 | Cancel plan |
| 18 | POST | /api/v1/production/mrp/run | 3 | Run MRP |
| 19 | GET | /api/v1/production/mrp/availability | 3 | Material availability |
| 20 | GET | /api/v1/production/indents | 3 | List indents |
| 21 | GET | /api/v1/production/indents/{id} | 3 | Indent detail |
| 22 | PUT | /api/v1/production/indents/{id}/edit | 3 | Edit draft indent |
| 23 | PUT | /api/v1/production/indents/{id}/send | 3 | Send indent |
| 24 | POST | /api/v1/production/indents/send-bulk | 3 | Bulk send |
| 25 | PUT | /api/v1/production/indents/{id}/acknowledge | 3 | Acknowledge |
| 26 | PUT | /api/v1/production/indents/{id}/link-po | 3 | Link to PO |
| 27 | GET | /api/v1/production/alerts | 3 | List alerts |
| 28 | PUT | /api/v1/production/alerts/{id}/read | 3 | Mark read |
| 29 | POST | /api/v1/production/orders/create-from-plan | 4 | Create orders |
| 30 | GET | /api/v1/production/orders | 4 | List orders |
| 31 | GET | /api/v1/production/orders/{id} | 4 | Order detail |
| 32 | POST | /api/v1/production/job-cards/generate | 4 | Generate job cards |
| 33 | GET | /api/v1/production/job-cards | 4 | List job cards |
| 34 | GET | /api/v1/production/job-cards/{id} | 4 | Full JC detail |
| 35 | PUT | /api/v1/production/job-cards/{id}/assign | 4 | Assign team |
| 36 | POST | /api/v1/production/job-cards/{id}/receive-material | 4 | QR scan receipt |
| 37 | PUT | /api/v1/production/job-cards/{id}/start | 4 | Start production |
| 38 | PUT | /api/v1/production/job-cards/{id}/complete-step | 4 | Complete step |
| 39 | PUT | /api/v1/production/job-cards/{id}/record-output | 4 | Record output |
| 40 | PUT | /api/v1/production/job-cards/{id}/complete | 4 | Complete JC |
| 41 | PUT | /api/v1/production/job-cards/{id}/sign-off | 4 | Sign-off |
| 42 | PUT | /api/v1/production/job-cards/{id}/force-unlock | 4 | Force unlock |
| 43 | POST | /api/v1/production/job-cards/{id}/environment | 4 | Annexure C |
| 44 | POST | /api/v1/production/job-cards/{id}/metal-detection | 4 | Annexure A/B |
| 45 | POST | /api/v1/production/job-cards/{id}/weight-checks | 4 | Annexure B |
| 46 | POST | /api/v1/production/job-cards/{id}/loss-reconciliation | 4 | Annexure D |
| 47 | POST | /api/v1/production/job-cards/{id}/remarks | 4 | Annexure E |
| 48 | GET | /api/v1/production/job-cards/team-dashboard | 4 | Team dashboard |
| 49 | GET | /api/v1/production/job-cards/floor-dashboard | 4 | Floor dashboard |
| 50 | GET | /api/v1/production/floor-inventory | 5 | List inventory |
| 51 | GET | /api/v1/production/floor-inventory/summary | 5 | Floor summary |
| 52 | POST | /api/v1/production/floor-inventory/move | 5 | Move material |
| 53 | GET | /api/v1/production/floor-inventory/movements | 5 | Movement history |
| 54 | POST | /api/v1/production/floor-inventory/check-idle | 5 | Idle check |
| 55 | GET | /api/v1/production/offgrade/inventory | 5 | Off-grade list |
| 56 | GET | /api/v1/production/offgrade/rules | 5 | Reuse rules |
| 57 | POST | /api/v1/production/offgrade/rules/create | 5 | Create rule |
| 58 | PUT | /api/v1/production/offgrade/rules/{id} | 5 | Update rule |
| 59 | GET | /api/v1/production/loss/analysis | 5 | Loss analysis |
| 60 | GET | /api/v1/production/loss/anomalies | 5 | Anomaly detection |
| 61 | GET | /api/v1/production/yield/summary | 5 | Yield summary |
| 62 | GET | /api/v1/production/day-end/summary | 6 | Day-end summary |
| 63 | PUT | /api/v1/production/day-end/dispatch | 6 | Bulk dispatch |
| 64 | POST | /api/v1/production/balance-scan/submit | 6 | Submit scan |
| 65 | GET | /api/v1/production/balance-scan/status | 6 | Scan status |
| 66 | GET | /api/v1/production/balance-scan/{id} | 6 | Scan detail |
| 67 | PUT | /api/v1/production/balance-scan/{id}/reconcile | 6 | Reconcile |
| 68 | POST | /api/v1/production/balance-scan/check-missing | 6 | Missing check |
| 69 | POST | /api/v1/production/fulfillment/cancel | 6 | Cancel fulfillment |
| 70 | PUT | /api/v1/production/job-cards/{id}/close | 4 | Close JC |
| 71 | POST | /api/v1/production/plans/revise | 7 | AI plan revision |
| 72 | GET | /api/v1/production/plans/{id}/revision-history | 7 | Revision chain |
| 73 | POST | /api/v1/production/discrepancy/report | 7 | Report discrepancy |
| 74 | GET | /api/v1/production/discrepancy | 7 | List discrepancies |
| 75 | GET | /api/v1/production/discrepancy/{id} | 7 | Discrepancy detail |
| 76 | PUT | /api/v1/production/discrepancy/{id}/resolve | 7 | Resolve |
| 77 | GET | /api/v1/production/ai/recommendations | 7 | AI recommendations |
| 78 | PUT | /api/v1/production/ai/recommendations/{id}/feedback | 7 | AI feedback |

---

# COMPLETE DATABASE SCHEMA

## All 34 Tables

| # | Table | Part | Purpose | Key Columns |
|---|-------|------|---------|-------------|
| 1 | machine | 1 | Physical equipment | machine_name, floor, status, allocation |
| 2 | machine_capacity | 1 | Per-product per-stage throughput | machine_id, stage, item_group, capacity_kg_per_hr |
| 3 | bom_header | 1 | FG recipe header | fg_sku_name, pack_size_kg, process_category |
| 4 | bom_line | 1 | BOM materials (RM+PM) | material_sku_name, quantity_per_unit, loss_pct |
| 5 | bom_process_route | 1 | Sequential process steps | step_number, process_name, stage |
| 6 | so_fulfillment | 2 | Demand tracking | fg_sku_name, pending_qty_kg, order_status |
| 7 | so_revision_log | 2 | Fulfillment audit trail | revision_type, old_value, new_value |
| 8 | production_plan | 2 | Plan header | plan_type, status, ai_generated, ai_analysis_json |
| 9 | production_plan_line | 2 | Plan line items | planned_qty_kg, machine_id, linked_so_fulfillment_ids |
| 10 | production_order | 4 | Created from plan lines | prod_order_number, batch_number, batch_size_kg |
| 11 | job_card | 4 | One per process stage | job_card_number, step_number, is_locked, status |
| 12 | job_card_rm_indent | 4 | RM requirements | material_sku_name, gross_qty, issued_qty, scanned_box_ids |
| 13 | job_card_pm_indent | 4 | PM requirements | Same as RM |
| 14 | job_card_process_step | 4 | Steps within JC | process_name, qc_check, operator_name, time_done |
| 15 | job_card_output | 4 | Section 5 output | fg_expected/actual, rm_consumed, process_loss, offgrade |
| 16 | job_card_environment | 4 | Annexure C | parameter_name, value |
| 17 | job_card_metal_detection | 4 | Annexure A/B | fe/nfe/ss_pass, seal_check, temps |
| 18 | job_card_weight_check | 4 | Annexure B | sample_number, net_weight, leak_test_pass |
| 19 | job_card_loss_reconciliation | 4 | Annexure D | loss_category, budgeted/actual_loss_kg |
| 20 | job_card_remarks | 4 | Annexure E | remark_type, content |
| 21 | job_card_sign_off | 4 | Section 6 + Annex | sign_off_type, name, signed_at |
| 22 | floor_inventory | 5 | Stock per floor | sku_name, floor_location, quantity_kg |
| 23 | floor_movement | 5 | Movement audit | from/to_location, quantity_kg, reason |
| 24 | offgrade_inventory | 5 | Off-grade stock | source_product, available_qty_kg, status |
| 25 | offgrade_reuse_rule | 5 | Reuse rules | source/target_item_group, max_substitution_pct |
| 26 | offgrade_consumption | 5 | Reuse tracking | offgrade_id, job_card_id, qty_used_kg |
| 27 | process_loss | 5 | Loss records | product_name, loss_kg, loss_pct, loss_category |
| 28 | quality_inspection | 5 | QC records | inspection_type, result, inspector_name |
| 29 | yield_summary | 5 | Yield metrics | total_input/output_kg, yield_pct |
| 30 | purchase_indent | 3 | Material indents | indent_number, required_qty_kg, status |
| 31 | store_alert | 3 | Notifications | alert_type, target_team, message |
| 32 | ai_recommendation | 2 | Claude log | recommendation_type, tokens_used, status |
| 33 | day_end_balance_scan | 6 | Balance scan header | floor_location, scan_date, total_variance |
| 34 | day_end_balance_scan_line | 6 | Scan line items | system_qty_kg, scanned_qty_kg, variance_pct |
| 35 | discrepancy_report | 7 | Discrepancy tracking | discrepancy_type, severity, resolution_type |

---

*Document generated: March 2026*
*Candor Foods Pvt. Ltd. — Production Planning Module v1.0*
*Total: 78 endpoints | 35 tables | 11 services | 5,955 lines of code*
