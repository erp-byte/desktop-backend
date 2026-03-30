# Part 4: Job Card Engine ✅ COMPLETED

---

## 8. Create Production Order ✅

**Implementation:** `services/job_card_engine.py`

### Checklist

- [x] `create_production_orders(conn, plan_id, entity)`:
  - [x] Generate prod_order_number: "PRD-{YYYY}-{seq:04d}"
  - [x] Generate batch_number: "B{YYYY}-{seq:04d}"
  - [x] Look up BOM header → pack_size_kg, shelf_life_days
  - [x] Count stages from bom_process_route
  - [x] INSERT production_order (status='created')
- [x] Router: `POST /orders/create-from-plan`
- [x] Router: `GET /orders` (paginated, filterable)
- [x] Router: `GET /orders/{id}` (detail with job cards)

---

## 9. Create Sequential Job Cards ✅

### Checklist

- [x] `create_job_cards(conn, prod_order_id)`:
  - [x] Load BOM process route ORDER BY step_number
  - [x] FOR each step: job_card_number = "{prod_order_number}/{step_number}"
  - [x] Step 1: is_locked=FALSE, status='unlocked'
  - [x] Step 2+: is_locked=TRUE, status='locked', locked_reason='awaiting_previous_stage'
  - [x] Create RM indent lines on first stage (bom_line WHERE item_type='rm')
  - [x] Create PM indent lines on last stage (bom_line WHERE item_type='pm')
  - [x] Create process steps from bom_process_route
  - [x] UPDATE production_order → status='job_cards_issued', total_stages=count
- [x] Router: `POST /job-cards/generate`
- [x] Router: `GET /job-cards` (filterable: status, team_leader, entity, floor, stage)
- [x] Router: `GET /job-cards/{id}` (full detail matching PDF CFC/PRD/JC/V3.0)

---

## 10. QR Material Receipt ✅

**Implementation:** `services/qr_service.py`

### Checklist

- [x] `receive_material_via_qr(conn, job_card_id, box_ids, entity)`:
  - [x] Lookup po_box WHERE box_id
  - [x] Validate: box exists, not already consumed, material matches indent
  - [x] Get po_line.sku_name via transaction_no + line_number
  - [x] Match against job_card_rm_indent or job_card_pm_indent (ILIKE)
  - [x] Update indent: scanned_box_ids += box_id, issued_qty += net_weight, batch_no = lot_number
  - [x] Deduct floor_inventory (rm_store or pm_store)
  - [x] Create floor_movement (store → production_floor)
  - [x] When all indents fulfilled → status='material_received'
- [x] Router: `POST /job-cards/{id}/receive-material`

---

## 11. Job Card Execution ✅

### Checklist

- [x] `assign_job_card()`: Set team_leader, team_members, status='assigned'
- [x] `start_job_card()`: Validate status, set status='in_progress', start_time=NOW()
- [x] `complete_process_step()`: operator_name, qc_sign_at, time_done, status='completed'
- [x] Router: `PUT /job-cards/{id}/assign`
- [x] Router: `PUT /job-cards/{id}/start`
- [x] Router: `PUT /job-cards/{id}/complete-step`

---

## 12. Job Card Completion & Next Unlock ✅

### Checklist

- [x] `record_output()`: INSERT/UPSERT job_card_output (fg actual/expected, rm consumed, loss, offgrade)
- [x] `complete_job_card()`:
  - [x] Set status='completed', end_time, total_time_min
  - [x] Find next job card (prod_order_id, step_number+1)
  - [x] IF next exists → unlock, create floor_movement, create alert
  - [x] IF last stage:
    - [x] Complete production_order (status='completed')
    - [x] Update so_fulfillment.produced_qty_kg
    - [x] Auto-record process_loss
    - [x] Auto-create offgrade_inventory
- [x] `sign_off()`: INSERT into job_card_sign_off (ON CONFLICT UPDATE)
- [x] `close_job_card()`: Validate 3 sign-offs done, set status='closed'
- [x] Router: `PUT /job-cards/{id}/record-output`
- [x] Router: `PUT /job-cards/{id}/complete`
- [x] Router: `PUT /job-cards/{id}/sign-off`
- [x] Router: `PUT /job-cards/{id}/close`

---

## 13. Force Unlock ✅

### Checklist

- [x] `force_unlock()`:
  - [x] Set is_locked=FALSE, force_unlocked=TRUE
  - [x] Record force_unlock_by, force_unlock_reason, force_unlock_at
  - [x] Create alert (force_unlock)
  - [x] Warn if previous stage has no output
- [x] Router: `PUT /job-cards/{id}/force-unlock`

---

## Annexures ✅ (matching CFC/PRD/JC/V3.0 PDF)

### Checklist

- [x] Router: `POST /job-cards/{id}/environment` — Annexure C (env parameters)
- [x] Router: `POST /job-cards/{id}/metal-detection` — Annexure A/B (pre/post packaging, seal/wt check, temps)
- [x] Router: `POST /job-cards/{id}/weight-checks` — Annexure B (20-sample, target_wt, tolerance, accept range)
- [x] Router: `POST /job-cards/{id}/loss-reconciliation` — Annexure D (6 categories, budgeted vs actual)
- [x] Router: `POST /job-cards/{id}/remarks` — Annexure E (observations, deviations, corrective actions)

---

## Dashboards ✅

- [x] Router: `GET /job-cards/team-dashboard` — priority-sorted for team leader
- [x] Router: `GET /job-cards/floor-dashboard` — all JCs on a floor

---

## Schema Migrations ✅

- [x] job_card +10 columns (sales_order_ref, article_code, mrp, ean, bu, fumigation, metal_detector_used, roasting_pasteurization, control_sample_gm, magnets_used)
- [x] job_card_metal_detection +7 columns (seal_check, seal/wt_failed_units, dough/oven/baking_temp_c)
- [x] job_card_weight_check +4 columns (target_wt_g, tolerance_g, accept_range_min/max)
- [x] New job_card_sign_off table (sign_off_type: production_incharge, quality_analysis, warehouse_incharge, plant_head)

---

## Files Created/Modified

| File | Status |
|------|--------|
| `services/job_card_engine.py` | ✅ Done — 676 lines, 11 functions |
| `services/qr_service.py` | ✅ Done — 205 lines, QR material receipt |
| `app/db/production_migrate.sql` | ✅ Done — Migrations 4-7 added |
| `router.py` | ✅ Done — 21 new endpoints |

## Endpoints (21, total: 49 at this point)

| # | Method | Endpoint | Description |
|---|--------|----------|-------------|
| 29 | POST | `/orders/create-from-plan` | Create orders from plan |
| 30 | GET | `/orders` | List orders |
| 31 | GET | `/orders/{id}` | Order detail |
| 32 | POST | `/job-cards/generate` | Generate sequential JCs |
| 33 | GET | `/job-cards` | List JCs |
| 34 | GET | `/job-cards/{id}` | Full JC detail (PDF match) |
| 35 | PUT | `/job-cards/{id}/assign` | Assign team |
| 36 | POST | `/job-cards/{id}/receive-material` | QR scan receipt |
| 37 | PUT | `/job-cards/{id}/start` | Start production |
| 38 | PUT | `/job-cards/{id}/complete-step` | Complete step |
| 39 | PUT | `/job-cards/{id}/record-output` | Record Section 5 |
| 40 | PUT | `/job-cards/{id}/complete` | Complete → auto-unlock |
| 41 | PUT | `/job-cards/{id}/sign-off` | Sign-off |
| 42 | PUT | `/job-cards/{id}/force-unlock` | Force unlock |
| 43 | POST | `/job-cards/{id}/environment` | Annexure C |
| 44 | POST | `/job-cards/{id}/metal-detection` | Annexure A/B |
| 45 | POST | `/job-cards/{id}/weight-checks` | Annexure B |
| 46 | POST | `/job-cards/{id}/loss-reconciliation` | Annexure D |
| 47 | POST | `/job-cards/{id}/remarks` | Annexure E |
| 48 | GET | `/job-cards/team-dashboard` | Team dashboard |
| 49 | GET | `/job-cards/floor-dashboard` | Floor dashboard |
| 70 | PUT | `/job-cards/{id}/close` | Close JC |
