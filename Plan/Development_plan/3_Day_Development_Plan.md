**Date:** 2026-03-26
**Time:** 00:36 IST

---

# 3-DAY DEVELOPMENT PLAN
## Production Planning Module - Candor Foods
### Total: 3 Days x 8 Hours = 24 Hours (Backend + Frontend)

---

## PREREQUISITES (Before Day 1 starts)

- [ ] PostgreSQL database running with existing SO/PO tables
- [ ] Backend repo (`e:\Consumption\Backend`) set up with FastAPI running
- [ ] Frontend repo ready (React/Next.js assumed based on existing frontend)
- [ ] `anthropic` and `qrcode` Python packages installed
- [ ] All Excel data files in `app/data/`
- [ ] Updated Asset Master Excel with per-hour machine capacity columns

---

## DAY 1 (8 Hours) - FOUNDATION + CORE ENGINE

**Goal:** Database tables live, BOM + Machine data ingested, Production Order + Job Card engine working end-to-end

---

### Hour 1 (09:00 - 10:00) — Database Schema + Module Skeleton

**Backend Tasks:**

- [ ] **Create `app/db/production_schema.sql`** — All 23 CREATE TABLE statements
  - machine, machine_capacity
  - bom_header, bom_line, bom_process_route
  - so_fulfillment, so_revision_log
  - production_plan, production_plan_line
  - production_order
  - job_card, job_card_rm_indent, job_card_pm_indent, job_card_process_step
  - job_card_output, job_card_environment, job_card_metal_detection
  - job_card_weight_check, job_card_loss_reconciliation, job_card_remarks
  - floor_inventory, floor_movement
  - offgrade_inventory, offgrade_reuse_rule, offgrade_consumption
  - process_loss, quality_inspection, yield_summary
  - purchase_indent, store_alert
  - ai_recommendation
  - All indexes

- [ ] **Create `app/db/production_migrate.sql`** — Idempotent migrations (ALTER TABLE IF NOT EXISTS pattern)

- [ ] **Create module directory structure:**
  ```
  app/modules/production/
    __init__.py
    router.py
    schemas/__init__.py
    services/__init__.py
  ```

- [ ] **Update `app/main.py`:**
  - Import production router
  - Execute production_schema.sql in lifespan
  - Execute production_migrate.sql in lifespan
  - Include production router

**Verification:** Server starts, all 23 tables created in PostgreSQL

---

### Hour 2 (10:00 - 11:00) — Pydantic Schemas (All)

**Backend Tasks:**

- [ ] **`schemas/bom.py`** — BOMHeaderCreate, BOMHeaderOut, BOMLineOut, BOMProcessRouteOut
- [ ] **`schemas/machine.py`** — MachineCreate, MachineOut, MachineCapacityOut, CapacityMatrixOut
- [ ] **`schemas/plan.py`** — PlanCreate, PlanOut, PlanLineOut, DemandSummaryOut
- [ ] **`schemas/production_order.py`** — ProdOrderCreate, ProdOrderOut
- [ ] **`schemas/job_card.py`** — JobCardOut, JobCardDetailOut, RMIndentOut, PMIndentOut, ProcessStepOut, OutputRecord, EnvironmentRecord, MetalDetectionRecord, WeightCheckRecord, LossReconciliationRecord, RemarksRecord
- [ ] **`schemas/floor_inventory.py`** — FloorInventoryOut, FloorMovementCreate, FloorMovementOut
- [ ] **`schemas/loss.py`** — ProcessLossOut, LossAnalysisOut
- [ ] **`schemas/offgrade.py`** — OffgradeInventoryOut, ReuseRuleCreate, ReuseRuleOut, OffgradeConsumptionOut
- [ ] **`schemas/quality.py`** — QualityInspectionCreate, QualityInspectionOut
- [ ] **`schemas/indent.py`** — IndentOut, StoreAlertOut
- [ ] **`schemas/fulfillment.py`** — SOFulfillmentOut, FYReviewOut, RevisionCreate, CarryforwardRequest
- [ ] **`schemas/ai.py`** — AIRecommendationOut, ForecastRequest, PlanGenerateRequest
- [ ] **`schemas/response.py`** — UploadSummary, PaginatedResponse, FilterOptions
- [ ] **`schemas/__init__.py`** — Re-export all

**Pattern:** Follow existing `app/modules/purchase/schemas/` conventions. Use `Decimal3` from `app/core/types.py` for all numeric fields.

---

### Hour 3 (11:00 - 12:00) — BOM + Machine Parsers & Ingest

**Backend Tasks:**

- [ ] **`services/bom_ingest.py`** — Parse `BOM VS Actual Apr-mar.xlsx`
  - State-machine parser (detect product header rows, extract lines)
  - Fuzzy match material names using existing `match_sku()` from `app/modules/so/services/item_matcher.py`
  - Create bom_header + bom_line + bom_process_route records
  - Return upload summary (matched/unmatched counts)

- [ ] **`services/asset_ingest.py`** — Parse Asset Master Excel
  - Extract machine name, type, category, capable_stages
  - Extract per-product per-stage capacity from new columns
  - Create machine + machine_capacity records
  - Return upload summary

- [ ] **Router endpoints:**
  - `POST /api/v1/production/bom/upload` — Upload BOM Excel
  - `POST /api/v1/production/machines/upload` — Upload Asset Master Excel
  - `GET /api/v1/production/bom/view` — Paginated BOM list
  - `GET /api/v1/production/bom/{bom_id}` — BOM detail
  - `GET /api/v1/production/machines` — Machine list
  - `GET /api/v1/production/machines/{id}` — Machine detail + capacity

**Verification:** Upload both Excel files, verify BOM and machine data in DB

---

### Hour 4 (12:00 - 13:00) — SO Fulfillment Sync + Production Plan CRUD

**Backend Tasks:**

- [ ] **`services/fulfillment.py`** (part of queries.py or separate)
  - `sync_fulfillment()`: Read so_header + so_line, create so_fulfillment records
  - `get_demand_summary()`: Aggregate pending demand grouped by product/customer
  - `get_fy_review()`: All unfulfilled orders for FY close review
  - `carryforward_orders()`: Bulk carry forward selected fulfillment IDs
  - `revise_order()`: Update qty/date with revision log

- [ ] **Production Plan CRUD:**
  - `POST /api/v1/production/plans/create` — Manual plan creation
  - `GET /api/v1/production/plans` — List with filters (entity, status, date, type)
  - `GET /api/v1/production/plans/{id}` — Plan detail with lines
  - `PUT /api/v1/production/plans/{id}/approve` — Approve plan
  - `GET /api/v1/production/plans/demand-summary` — Demand summary

- [ ] **Fulfillment endpoints:**
  - `POST /api/v1/production/fulfillment/sync`
  - `GET /api/v1/production/fulfillment`
  - `GET /api/v1/production/fulfillment/fy-review`
  - `POST /api/v1/production/fulfillment/carryforward`
  - `PUT /api/v1/production/fulfillment/{id}/revise`

**Verification:** Sync fulfillment from existing SOs, view demand summary

---

### Hour 5 (13:00 - 14:00) — LUNCH BREAK + MRP Algorithm

**Backend Tasks (after lunch):**

- [ ] **`services/mrp.py`** — Material Requirements Planning
  - `run_mrp(plan_id)`:
    - Load plan lines + BOMs
    - Calculate gross requirements per material (with loss %)
    - Check off-grade reuse possibilities
    - Compare against floor_inventory + pending POs
    - Return shortage/surplus per material
  - `check_availability(material_sku, qty_needed)`: Quick check

- [ ] **`services/indent_manager.py`** — Indent & Alert system
  - `auto_raise_indents(mrp_results, plan_id)`:
    - Create purchase_indent for each shortage
    - Create store_alert for purchase + stores teams
  - `acknowledge_indent(indent_id, user)`
  - `link_indent_to_po(indent_id, po_reference)`

- [ ] **Router endpoints:**
  - `POST /api/v1/production/mrp/run` — Run MRP
  - `GET /api/v1/production/mrp/availability` — Check availability
  - `GET /api/v1/production/indent` — List indents
  - `GET /api/v1/production/alerts` — List alerts

**Verification:** Run MRP on a plan, see shortage/surplus output, indents auto-created

---

### Hour 6 (14:00 - 15:00) — Production Order + Job Card Engine

**This is the most critical hour.** The job card engine is the core of the system.

**Backend Tasks:**

- [ ] **`services/job_card_engine.py`:**

  - `create_production_order(plan_line_id)`:
    - Generate prod_order_number ("PO-{YYYY}-{seq:04d}")
    - Generate batch_number ("B{YYYY}-{seq:03d}")
    - Populate from BOM + plan_line
    - INSERT production_order

  - `create_job_cards(prod_order_id)`:
    - Load BOM process route
    - FOR each step: create job_card
    - First stage = unlocked, rest = locked
    - Create RM indent lines (first stage)
    - Create PM indent lines (packaging stage)
    - Create process steps

  - `on_job_card_complete(job_card_id)`:
    - Mark completed
    - Unlock next stage (if exists)
    - Move SFG in floor_inventory
    - If last stage: complete production_order, update fulfillment

  - `force_unlock(job_card_id, authority_name, reason)`:
    - Validate authority
    - Unlock with audit trail

- [ ] **Router endpoints:**
  - `POST /api/v1/production/orders/create-from-plan` — Create prod orders from plan
  - `GET /api/v1/production/orders` — List orders
  - `GET /api/v1/production/orders/{id}` — Order detail with job cards
  - `POST /api/v1/production/job-cards/generate` — Generate job cards
  - `GET /api/v1/production/job-cards` — List job cards
  - `GET /api/v1/production/job-cards/{id}` — Full job card detail

**Verification:** Create production order → see 3 sequential job cards, first unlocked, rest locked

---

### Hour 7 (15:00 - 16:00) — Job Card Operational Endpoints

**Backend Tasks:**

- [ ] **Job card lifecycle endpoints:**
  - `PUT /job-cards/{id}/assign` — Assign to team leader
  - `PUT /job-cards/{id}/unlock` — Manual unlock (after previous completes)
  - `PUT /job-cards/{id}/force-unlock` — Force unlock (requires authority + reason)
  - `PUT /job-cards/{id}/start` — Start production
  - `PUT /job-cards/{id}/complete-step` — Complete a process step
  - `PUT /job-cards/{id}/record-output` — Record Section 5 (FG + offgrade + waste)
  - `PUT /job-cards/{id}/day-end-dispatch` — Day-end dispatch update
  - `PUT /job-cards/{id}/sign-off` — Sign-off (production/QC/warehouse)
  - `PUT /job-cards/{id}/close` — Close after all sign-offs

- [ ] **Job card annexure endpoints:**
  - `POST /job-cards/{id}/environment` — Annexure C
  - `POST /job-cards/{id}/metal-detection` — Annexure A/B
  - `POST /job-cards/{id}/weight-checks` — Annexure B samples
  - `POST /job-cards/{id}/loss-reconciliation` — Annexure D
  - `POST /job-cards/{id}/remarks` — Annexure E

- [ ] **Dashboard endpoints:**
  - `GET /job-cards/team-dashboard` — Job cards for a team leader
  - `GET /job-cards/floor-dashboard` — All job cards on a floor

**Verification:** Full lifecycle test: assign → start → complete steps → record output → sign-off → close → next card auto-unlocks

---

### Hour 8 (16:00 - 17:00) — Floor Inventory + QR Scan + Off-Grade

**Backend Tasks:**

- [ ] **`services/floor_tracker.py`:**
  - `move_material()`: Validate transition, debit/credit in transaction
  - `get_floor_summary()`: Current stock per floor
  - State machine validation (allowed transitions)

- [ ] **`services/qr_service.py`:**
  - `receive_material_via_qr(job_card_id, scanned_box_ids)`:
    - Look up po_box by box_id
    - Validate not already consumed
    - Match against job card indent lines
    - Deduct from floor_inventory
    - Create floor_movement
    - Update job card indent (issued_qty, scanned_box_ids)

- [ ] **Off-grade basic CRUD:**
  - `GET /offgrade/inventory`
  - `POST /offgrade/rules/create`
  - `GET /offgrade/rules`

- [ ] **Floor inventory endpoints:**
  - `GET /floor-inventory`
  - `GET /floor-inventory/summary`
  - `POST /floor-inventory/move`
  - `GET /floor-inventory/movements`
  - `POST /floor-inventory/scan-receive` (QR scan)

- [ ] **Process loss auto-recording:**
  - `auto_record_process_loss(job_card_id)`: Create process_loss from job_card_output

**Verification:** QR scan boxes → inventory deducted → floor movement recorded. Complete job card → off-grade auto-created → process loss auto-recorded.

---

### DAY 1 CHECKPOINT

By end of Day 1, the backend should support:
- All 23 tables created
- BOM + Machine data uploaded and queryable
- SO fulfillment synced from existing SOs
- Production plan created (manual) and approved
- MRP run → shortage detection → indent creation
- Production orders → sequential job cards (lock/unlock)
- QR scan material receipt
- Job card full lifecycle (start → complete → next unlocks)
- Floor inventory movements
- Day-end dispatch
- Process loss recording

**What's NOT done yet:** Claude AI integration, Excel parsers for loss/offgrade, frontend, PDF generation

---

## DAY 2 (8 Hours) - AI INTEGRATION + DATA LOADING + FRONTEND START

**Goal:** Claude AI generating plans, historical data loaded, frontend core screens working

---

### Hour 9 (09:00 - 10:00) — Claude AI Service Layer

**Backend Tasks:**

- [ ] **`services/ai_planner.py`:**

  - `call_claude(settings, system_prompt, user_prompt)`:
    - Create anthropic.AsyncAnthropic client
    - Call messages.create with structured prompt
    - Parse JSON response
    - Log to ai_recommendation table (prompt, response, tokens, latency)
    - Return parsed result

  - `collect_planning_context(entity, plan_type, date_range)`:
    - Query so_fulfillment (pending demand)
    - Query floor_inventory (RM/PM stock)
    - Query machines + machine_capacity
    - Query BOM headers + lines
    - Query process_loss (historical averages)
    - Query offgrade_inventory + reuse rules
    - Query in-progress job cards
    - Return structured JSON context

  - System prompt templates:
    - DAILY_PLAN_SYSTEM_PROMPT
    - WEEKLY_PLAN_SYSTEM_PROMPT
    - FULL_ORDER_PLAN_SYSTEM_PROMPT
    - PLAN_REVISION_SYSTEM_PROMPT

**Verification:** Call Claude with test data, get structured JSON response, log saved to DB

---

### Hour 10 (10:00 - 11:00) — AI Plan Generation Endpoints

**Backend Tasks:**

- [ ] **`POST /plans/generate-daily`:**
  - Collect planning context for today
  - Build Claude prompt with all data
  - Call Claude → get daily schedule
  - Create production_plan (ai_generated=TRUE)
  - Create plan_lines from Claude's output
  - Store ai_analysis_json
  - Return plan for review

- [ ] **`POST /plans/generate-weekly`:**
  - Same flow but for 7-day horizon
  - More detailed machine allocation

- [ ] **`POST /plans/generate-full`:**
  - Full order plan covering all pending demand
  - Week-by-week allocation
  - Procurement plan
  - Carryforward closure strategy

- [ ] **`POST /plans/revise`:**
  - Accept: plan_id + change_event (new SO / material arrival / QC failure)
  - Send current plan + change to Claude
  - Get revised plan
  - Create new plan with revision_number++, previous_plan_id set

- [ ] **AI analysis endpoints:**
  - `POST /ai/forecast-demand`
  - `POST /ai/loss-anomaly-scan`
  - `POST /ai/plan-review`
  - `GET /ai/recommendations`
  - `PUT /ai/recommendations/{id}/feedback`

**Verification:** Generate daily plan via Claude, approve, create job cards end-to-end

---

### Hour 11 (11:00 - 12:00) — Historical Data Parsers

**Backend Tasks:**

- [ ] **`services/loss_ingest.py`** — Parse `Process Loss Apr to Mar.xlsx`
  - Extract product-wise, month-wise loss records
  - Map products to item_group using match_sku()
  - Calculate loss percentages
  - Insert into process_loss table

- [ ] **`services/offgrade_ingest.py`** — Parse 12 Off-Grade Excel files
  - Process all 12 files (Almonds through Raisin)
  - Extract off-grade records with categories
  - Insert into offgrade_inventory
  - Auto-suggest reuse rules based on product compatibility

- [ ] **Router endpoints:**
  - `POST /loss/upload` — Upload Process Loss Excel
  - `POST /offgrade/upload` — Upload Off-Grade Excel files (multi-file)
  - `GET /loss/analysis` — Loss analysis by product/machine/season
  - `GET /loss/anomalies` — AI-flagged anomalies
  - `POST /offgrade/recommend-reuse` — AI off-grade reuse recommendation

- [ ] **Off-grade reuse optimizer:**
  - `services/offgrade_optimizer.py`:
    - Build compatibility matrix
    - Greedy allocation by cost saving
    - Return allocations

**Verification:** Upload all Excel files, verify data in DB, run loss analysis

---

### Hour 12 (12:00 - 13:00) — LUNCH + Quality + Remaining Backend

**Backend Tasks:**

- [ ] **Quality inspection endpoints:**
  - `POST /quality/inspect` — Record inspection
  - `GET /quality/inspections` — List with filters
  - `GET /quality/summary` — Pass/fail rates

- [ ] **Yield summary computation:**
  - Auto-compute yield_summary from process_loss data
  - `GET /yield/summary` — Yield by product/period

- [ ] **Missing CRUD and query endpoints:**
  - `services/queries.py` — WHERE clause builder (follow purchase module pattern)
  - Pagination for all list endpoints
  - Filter options dropdowns
  - Export endpoints (if time permits)

- [ ] **Integration test:** Full cycle
  1. Sync SO fulfillment
  2. Generate daily plan via Claude
  3. Approve → Create production orders → Job cards
  4. QR scan → Start → Complete → Next unlocks
  5. Day-end dispatch → Close
  6. Verify: fulfillment updated, loss recorded, inventory correct

**Verification:** Full end-to-end test passes

---

### Hour 13-14 (13:00 - 15:00) — Frontend Setup + Dashboard + Plan Screens

**Frontend Tasks:**

- [ ] **Project setup:**
  - Create production module folder in frontend
  - Install dependencies (QR scanner library, chart library)
  - Set up API client/hooks for production endpoints
  - Define TypeScript types matching backend schemas

- [ ] **Production Dashboard (`/production`)**
  - Today's plan summary card (total orders, kg, units)
  - Job card status breakdown (pie chart: locked/unlocked/in-progress/completed)
  - Active alerts panel (material shortages, anomalies)
  - Quick actions: "Generate Plan", "View Job Cards", "Day-End Update"
  - Carryforward orders count badge

- [ ] **Plan Generation Screen (`/production/plans/generate`)**
  - Button: "Generate Daily Plan" (calls POST /plans/generate-daily)
  - Loading state while Claude processes (~10-20s)
  - Plan preview table:
    - Columns: Product, Customer, Qty (kg), Units, Machine, Priority, Shift, Status
    - Color-coded: carryforward = yellow, adhoc = orange, normal = white
  - Material check panel: green (sufficient) / red (shortage)
  - Risk flags panel
  - "Edit" ability on quantities/priorities
  - "Approve" button → creates production orders + job cards
  - "Revise" button → triggers plan revision flow

- [ ] **Plan List Screen (`/production/plans`)**
  - Table: Plan Name, Type, Date Range, Status, AI-Generated badge
  - Filters: entity, status, date range, plan type
  - Click → Plan detail with all lines

**Verification:** Dashboard loads, plan generation works end-to-end on frontend

---

### Hour 15-16 (15:00 - 17:00) — Job Card Frontend Screens

**Frontend Tasks:**

- [ ] **Job Card List (`/production/job-cards`)**
  - Table: JC Number, Product, Stage, Customer, Status, Team Leader, Date
  - Status badges: locked (grey), unlocked (blue), in-progress (yellow), completed (green), closed (dark green)
  - Filters: status, team leader, date, floor, stage
  - Click → Job Card Detail

- [ ] **Job Card Detail (`/production/job-cards/{id}`)**
  - Header: JC number, "Stage X of Y", prod order ref
  - **Section 1** — Product Details card (all fields from PDF)
  - **Section 2A** — RM Indent table (material, UOM, reqd, loss%, gross, issued, batch, godown, var)
  - **Section 2B** — PM Indent table (same columns)
  - **Section 3** — Team & Process (team leader, members, start/end time, process flags checkboxes)
  - **Section 4** — Process Steps table (step, process, machine, std time, QC check, loss%, operator sign, QC sign, time done)
  - **Section 5** — Output form (expected/actual/variance/var%/reject reason for FG units, FG kg, RM consumed)
  - **Section 6** — Sign-offs (production incharge, QC, warehouse incharge)
  - Lock indicator (locked/unlocked/force-unlocked with reason)

- [ ] **Job Card Action Buttons:**
  - "Receive Material" → QR scanner modal
  - "Start Production" → sets status to in_progress
  - "Complete Step" → marks process step done
  - "Record Output" → output form modal
  - "Day-End Dispatch" → dispatch qty input
  - "Sign Off" → sign-off form
  - "Force Unlock" → requires authority name + reason (modal with warning)

- [ ] **Team Dashboard (`/production/team-dashboard`)**
  - "My Job Cards" — filtered to logged-in team leader
  - Priority-sorted queue
  - Quick status indicators

**Verification:** Job card detail screen shows all sections matching the PDF sample, actions work

---

### DAY 2 CHECKPOINT

By end of Day 2:
- Claude AI generating daily/weekly/full plans
- All historical data (loss, off-grade) loaded
- Frontend: Dashboard, Plan generation, Job card list + detail with all sections
- Full end-to-end flow working: SO → Claude plan → approve → job cards → frontend views

**What's NOT done yet:** QR scanner UI, day-end screen, FY review, floor inventory, off-grade, alerts, charts, PDF generation

---

## DAY 3 (8 Hours) - FRONTEND COMPLETION + POLISH + TESTING

**Goal:** All frontend screens complete, QR scanning working, full production cycle testable

---

### Hour 17 (09:00 - 10:00) — QR Scanner + Material Receipt UI

**Frontend Tasks:**

- [ ] **QR Scanner Component:**
  - Use `html5-qrcode` or `react-qr-reader` library
  - Camera access permission handling
  - Scan feedback (success beep, box info display)
  - Batch scanning mode (scan multiple boxes sequentially)

- [ ] **Material Receipt Screen (within Job Card)**
  - "Receive Material" button opens QR scanner overlay
  - After scan: shows box details (box_id, net_weight, lot_number)
  - Validation messages (box not found, already consumed, wrong material)
  - Running total: "Received X / Y kg" progress bar
  - "Confirm Receipt" → calls POST /job-cards/{id}/receive-material
  - Status update: "Material Received - Ready to Start"

- [ ] **Floor Manager Material Receive (from PO):**
  - `POST /floor-inventory/scan-receive`
  - QR scan to receive PO boxes into rm_store/pm_store
  - Auto-updates floor_inventory
  - Triggers re-check of waiting job cards

**Verification:** Scan a test QR code, see material received on job card, inventory deducted

---

### Hour 18 (10:00 - 11:00) — Day-End + Dispatch + Annexures UI

**Frontend Tasks:**

- [ ] **Day-End Dashboard (`/production/day-end`)**
  - Shows all completed final-stage job cards for today
  - For each: product, customer, expected FG, actual FG, dispatch qty input
  - "Submit Day-End" button
  - Sign-off section: 3 signature slots (Production, QC, Warehouse)
  - Summary: total dispatched today, total loss, total off-grade

- [ ] **Job Card Annexures (tabs within Job Card Detail):**
  - **Annexure A/B Tab** — Metal Detection form
    - Pre-packaging: Fe/Nfe/SS pass/fail, failed units
    - Post-packaging: same + weight/leak check table (20 samples)
  - **Annexure C Tab** — Environmental Parameters form
    - Brine salinity, temps, humidity, fan%, RPM, gas, magnet, clean room
  - **Annexure D Tab** — Loss Reconciliation table
    - 6 categories: budgeted loss %, budgeted kg, actual kg, variance, remarks
    - Auto-calculate totals
  - **Annexure E Tab** — Remarks
    - Text areas: observations/deviations, corrective action

- [ ] **Loss Reconciliation Auto-Fill:**
  - Pre-fill budgeted loss % from BOM process route
  - Calculate budgeted loss kg from batch size
  - Highlight variance cells red if actual > budgeted

**Verification:** Day-end flow complete, annexures save correctly

---

### Hour 19 (11:00 - 12:00) — Floor Inventory + Indent + Alerts UI

**Frontend Tasks:**

- [ ] **Floor Inventory Screen (`/production/floor-inventory`)**
  - Visual floor map (cards for each floor location)
  - Each card: floor name, total RM kg, total PM pcs, total WIP kg, total FG units
  - Click floor → detailed inventory list
  - Movement history table with filters

- [ ] **Indent Dashboard (`/production/indents`)**
  - Table: Indent #, Material, Qty, Required By, Priority, Status
  - Status badges: raised (red), acknowledged (yellow), po_created (blue), received (green)
  - Action: "Acknowledge" button for purchase team
  - Link to PO when created

- [ ] **Alerts Panel (sidebar or notification center)**
  - Bell icon with unread count badge
  - Alert list: timestamp, type icon, message, read/unread
  - Click to mark read
  - Filter by team (purchase, stores, production)
  - Alert types: material_shortage, indent_raised, material_received, force_unlock, anomaly

**Verification:** Floor inventory shows stock per floor, indents created from MRP visible, alerts appearing

---

### Hour 20 (12:00 - 13:00) — LUNCH + FY Review + Fulfillment Tracker

**Frontend Tasks:**

- [ ] **FY Close Review Dashboard (`/production/fy-review`)**
  - Table: Customer, Product, Original Qty, Produced, Remaining, Delivery Date, Status
  - Grouped by customer (expandable sections)
  - For each order: 3 action buttons:
    - "Carry Forward" → creates carryforward record
    - "Revise" → opens quantity/date edit modal
    - "Cancel" → confirmation dialog
  - Batch actions: "Carry Forward All Selected" checkbox + button
  - Summary panel: total orders, total remaining kg, carryforward count

- [ ] **Fulfillment Tracker (`/production/fulfillment`)**
  - Table: SO#, Customer, Product, Original, Revised, Produced, Dispatched, Pending, Status
  - Status color: open (white), partial (yellow), fulfilled (green), carryforward (blue), cancelled (grey)
  - Filters: FY, customer, status, product
  - Running orders highlight

**Verification:** FY review shows unfulfilled orders, carry forward works, fulfillment tracker updates

---

### Hour 21 (13:00 - 14:00) — Off-Grade + AI Recommendations UI

**Frontend Tasks:**

- [ ] **Off-Grade Dashboard (`/production/offgrade`)**
  - Current off-grade inventory table:
    - Source product, Category (broken/undersized/etc.), Grade, Available Qty, Production Date, Expiry
  - Reuse rules table (source → target, max substitution %)
  - "Add Rule" form
  - "Get AI Recommendation" button:
    - Calls POST /offgrade/recommend-reuse
    - Shows Claude's recommendations in a card layout
    - "Accept" / "Reject" buttons for each recommendation

- [ ] **AI Recommendations Panel (`/production/ai`)**
  - History of all AI recommendations
  - Table: Type, Date, Status (generated/accepted/rejected), Confidence
  - Click → full recommendation detail with Claude's analysis
  - Feedback form: accept/reject + free-text feedback

- [ ] **BOM Management Screen (`/production/bom`)**
  - BOM list table with filters (entity, product, customer, active)
  - BOM detail: header + lines table + process route table
  - "Upload BOM Excel" button
  - Version history

**Verification:** Off-grade dashboard shows stock, AI recommendations display, BOM viewable

---

### Hour 22 (14:00 - 15:00) — Machine Management + Loss Analytics

**Frontend Tasks:**

- [ ] **Machine Management (`/production/machines`)**
  - Machine list: Name, Category, Stages, Status, Floor
  - Machine detail: info card + capacity matrix table
    - Capacity matrix: rows = stages, columns = products, cells = kg/hr
  - "Upload Asset Master" button
  - Schedule view: Gantt-like view of job cards per machine per day

- [ ] **Loss Analytics (`/production/loss`)**
  - Loss trend chart (line chart: loss % over time, by product)
  - Loss by category breakdown (bar chart: sorting, roasting, packaging, etc.)
  - Product comparison (bar chart: avg loss % per product)
  - Anomaly list: flagged batches with AI explanations
  - Filters: date range, product, machine, entity
  - Use chart library (Chart.js or Recharts)

**Verification:** Machine capacity matrix displays, loss charts render with data

---

### Hour 23 (15:00 - 16:00) — Integration Testing + Bug Fixes

**Full End-to-End Test:**

- [ ] **Test 1: Complete Daily Production Cycle**
  1. Ensure SOs exist (from existing SO module)
  2. Sync fulfillment: `POST /fulfillment/sync`
  3. Generate daily plan: `POST /plans/generate-daily` (Claude AI)
  4. Review plan on frontend → Approve
  5. Create production orders + job cards
  6. On frontend: open Job Card 1 (sorting) — unlocked
  7. QR scan material receipt → status = material_received
  8. Start production → complete steps → record output → complete
  9. Verify: Job Card 2 auto-unlocked on frontend
  10. Complete Job Card 2 → Job Card 3 unlocks
  11. Complete Job Card 3 (packaging, final stage)
  12. Day-end: enter dispatch qty → sign-offs → close
  13. Verify: so_fulfillment updated, process_loss recorded, floor_inventory correct

- [ ] **Test 2: Adhoc Order Revision**
  1. Add new SO mid-day
  2. Trigger plan revision: `POST /plans/revise`
  3. Claude produces revised plan
  4. Approve → new job cards created alongside existing

- [ ] **Test 3: Material Shortage Flow**
  1. Set up plan requiring material not in stock
  2. MRP detects shortage → indent auto-raised
  3. Verify indent visible on frontend
  4. Simulate PO receipt → inventory increases → job cards can proceed

- [ ] **Test 4: Force Unlock**
  1. Attempt to unlock locked job card without authority → rejected
  2. Force unlock with authority name + reason → succeeds
  3. Verify audit trail

- [ ] **Test 5: FY Transition**
  1. Create unfulfilled orders in FY 2025-26
  2. Open FY review → carry forward selected → new FY records created
  3. Generate plan in new FY → carryforward orders included

- [ ] **Bug fixes** from testing

**Verification:** All 5 tests pass

---

### Hour 24 (16:00 - 17:00) — Polish + Navigation + Final Review

**Tasks:**

- [ ] **Frontend navigation:**
  - Sidebar menu with all production screens
  - Breadcrumbs
  - Role-based menu visibility (team leader sees only their screens)
  - Mobile responsive for team leader / floor manager screens

- [ ] **UI Polish:**
  - Loading states on all API calls
  - Error handling with toast notifications
  - Empty states ("No job cards yet")
  - Confirmation dialogs for destructive actions
  - Print-friendly job card view (for paper printout matching CFC/PRD/JC/V3.0)

- [ ] **Data validation:**
  - Required field validation on all forms
  - Numeric range validation (loss % can't be > 100)
  - Date validation (plan end >= plan start)

- [ ] **Final smoke test:**
  - Start fresh: upload BOMs, machines
  - Generate plan, approve, create job cards
  - Execute one job card fully
  - Check all screens render correctly

- [ ] **Code cleanup:**
  - Remove console.logs
  - Consistent error messages
  - API response format consistency

---

### DAY 3 CHECKPOINT (FINAL)

By end of Day 3:
- Complete backend with 23 tables, 60+ endpoints, Claude AI integration
- Complete frontend with 17+ screens
- QR code scanning working
- Full daily production cycle testable end-to-end
- FY transition, adhoc orders, off-grade reuse all functional
- Loss analytics with charts

---

## FILE SUMMARY

### Backend Files Created (in `app/modules/production/`)

| File | Purpose | Est. Lines |
|------|---------|-----------|
| `router.py` | All REST endpoints | ~1200 |
| `schemas/bom.py` | BOM schemas | ~80 |
| `schemas/machine.py` | Machine schemas | ~60 |
| `schemas/plan.py` | Plan schemas | ~80 |
| `schemas/production_order.py` | Prod order schemas | ~50 |
| `schemas/job_card.py` | Job card schemas (largest) | ~200 |
| `schemas/floor_inventory.py` | Floor schemas | ~40 |
| `schemas/loss.py` | Loss schemas | ~40 |
| `schemas/offgrade.py` | Off-grade schemas | ~60 |
| `schemas/quality.py` | Quality schemas | ~40 |
| `schemas/indent.py` | Indent schemas | ~30 |
| `schemas/fulfillment.py` | Fulfillment schemas | ~50 |
| `schemas/ai.py` | AI schemas | ~40 |
| `schemas/response.py` | Response wrappers | ~30 |
| `services/bom_ingest.py` | BOM Excel parser | ~150 |
| `services/asset_ingest.py` | Asset Master parser | ~100 |
| `services/loss_ingest.py` | Process Loss parser | ~100 |
| `services/offgrade_ingest.py` | Off-grade parser | ~120 |
| `services/mrp.py` | MRP algorithm | ~150 |
| `services/scheduler.py` | Production scheduler | ~120 |
| `services/job_card_engine.py` | Job card core logic | ~300 |
| `services/floor_tracker.py` | Floor inventory state machine | ~100 |
| `services/indent_manager.py` | Indent + alerts | ~80 |
| `services/ai_planner.py` | Claude AI integration | ~250 |
| `services/qr_service.py` | QR scan logic | ~80 |
| `services/queries.py` | SQL query builders | ~200 |
| `services/offgrade_optimizer.py` | Off-grade reuse | ~80 |
| `services/loss_analyzer.py` | Loss analytics | ~80 |
| **Total Backend** | | **~3,590 lines** |

### Frontend Screens Created

| Screen | Route | Priority |
|--------|-------|----------|
| Production Dashboard | `/production` | Day 2 |
| Plan Generation | `/production/plans/generate` | Day 2 |
| Plan List | `/production/plans` | Day 2 |
| Job Card List | `/production/job-cards` | Day 2 |
| Job Card Detail | `/production/job-cards/{id}` | Day 2 |
| Team Dashboard | `/production/team-dashboard` | Day 2 |
| QR Scanner | Component within Job Card | Day 3 |
| Day-End Dashboard | `/production/day-end` | Day 3 |
| Floor Inventory | `/production/floor-inventory` | Day 3 |
| Indent Dashboard | `/production/indents` | Day 3 |
| Alerts Panel | Sidebar component | Day 3 |
| FY Close Review | `/production/fy-review` | Day 3 |
| Fulfillment Tracker | `/production/fulfillment` | Day 3 |
| Off-Grade Dashboard | `/production/offgrade` | Day 3 |
| AI Recommendations | `/production/ai` | Day 3 |
| BOM Management | `/production/bom` | Day 3 |
| Machine Management | `/production/machines` | Day 3 |
| Loss Analytics | `/production/loss` | Day 3 |

### Database Files

| File | Contents |
|------|----------|
| `app/db/production_schema.sql` | 23 CREATE TABLE statements + indexes |
| `app/db/production_migrate.sql` | Idempotent ALTER TABLE migrations |

---

## RISK MITIGATION

| Risk | Mitigation |
|------|-----------|
| Claude AI latency (10-20s) | Show loading spinner with "Generating plan..." message. Frontend doesn't block. |
| BOM Excel format unknown | Spend 15 min examining the Excel structure before parsing. Use flexible state-machine parser. |
| QR scanner on desktop | Fallback: manual box_id text input if camera not available |
| 24 hours tight for everything | Prioritize: DB + Job Card Engine + Plan Generation (Day 1) > Frontend core (Day 2) > Polish (Day 3). Cut: PDF generation, export endpoints, advanced charts |
| Frontend framework mismatch | Adapt component patterns to existing frontend structure |

---

## WHAT TO CUT IF BEHIND SCHEDULE

**Priority 1 (Must have):**
- DB tables, BOM/Machine upload, Plan generation, Job card lifecycle, QR scan, Day-end

**Priority 2 (Should have):**
- Claude AI integration, Floor inventory, Indents/alerts, Loss recording

**Priority 3 (Nice to have, cut if behind):**
- Loss analytics charts, Off-grade AI recommendations, Yield summary
- PDF job card generation, Export endpoints, Advanced machine scheduling
- FY review dashboard (can be done post-launch)
