# Production Planning — Datapoint Gap Analysis

Chat transcript mapping a standard production-planning framework against the current Candor Foods backend (`D:\Consumption\New\Backend`).

Date: 2026-04-17

---

## User

1. **Demand inputs**

   - Confirmed customer POs with committed delivery dates
   - Rolling forecast from key accounts (4–8 weeks)
   - Sales pipeline with probability weights
   - Historical seasonality (Diwali, Rakhi, winter, Ramadan)
   - SKU-wise breakup (retail pouch vs bulk pack — different line hours)
   - Customer priority tiering (strategic vs flexible)

2. **BOM & recipe data**

   - Per-SKU raw material composition and blend ratios
   - Packaging BOM (pouch, laminate, carton, inner tray, label, stickers)
   - Stage-wise yield factors (sorting, roasting, moisture, packaging giveaway)
   - Quarterly review of yield norms vs actuals

3. **Inventory position (real-time)**

   - RM by lot and grade (W240 ≠ W320 cashew for planning)
   - WIP and semi-finished roasted/processed stock
   - Finished goods stock
   - Packaging material stock
   - QC hold stock
   - Stock in transit

4. **Raw material procurement data**

   - Import lead times (6–10 weeks for almonds, cashew, pistachio)
   - Indian harvest seasonality (groundnut, raisin, walnut)
   - Supplier MOQs and lead times
   - Price volatility and buying strategy
   - Shelf life and FIFO constraints

5. **Capacity & process**

   - Machine capacity per line (sortex, roaster, flavor drum, filler, cartoner)
   - Shifts available and manpower skill availability
   - Changeover time between SKUs
   - Allergen changeover / line clearance (tree nut ↔ peanut)
   - Bottleneck resource identification

6. **Quality & compliance**

   - QC hold time (aflatoxin 24–48 hrs)
   - Microbiological clearance time
   - FSSAI / BRCGS batch documentation
   - Traceability lot coding
   - Customer-specific spec approvals

7. **Shelf life & dispatch**

   - Minimum shelf life on dispatch (typically 75–80%)
   - FG buildup limits working backward from expiry
   - Export vs domestic customer requirements

8. **Dry fruit industry specifics**

   - Moisture content of incoming RM (plan on standard moisture basis)
   - Grade mix of RM drives SKU feasibility
   - Customer-specific roast and salt profiles
   - Nitrogen flushing / MAP gas availability
   - Fumigation / pest control cycles
   - Oil separation / rancidity risk on roasted/flavored FG

9. **Planning horizon (bucketed)**

   - 3–6 months: RM procurement plan, capacity commitment
   - 4–8 weeks: Master Production Schedule (MPS)
   - 1–2 weeks: Detailed daily schedule with SKU sequencing
   - Daily: execution + replan

10. **Feedback loop (accuracy driver)**

    - Weekly plan vs actual tracking
    - RM consumption variance
    - Yield variance
    - Schedule adherence and OEE
    - Root cause every plan miss
    - Periodic refresh of yield norms, changeover times, lead time assumptions

From these, what all datapoints are there in this backend?

---

## Assistant

Legend: **P** = present, **p** = partial, **—** = absent. Evidence cites schema table/column or service file.

### 1. Demand inputs

- **P** Confirmed customer POs + delivery dates — `so_fulfillment.delivery_deadline`, `so_id`, `so_number`; `fulfillment.get_demand_summary()`
- **—** Rolling 4–8 week forecast
- **—** Sales pipeline with probability weights
- **—** Historical seasonality (Diwali/Rakhi/winter/Ramadan) — no festival/seasonality calendar
- **p** SKU retail-pouch vs bulk-pack — `bom_header.pack_size_kg` exists, but no `customer_type`/category flag
- **P** Customer priority tiering — `so_fulfillment.priority` (1–10), used by `ai_planner.py`

### 2. BOM & recipe

- **P** RM blend ratios — `bom_header` + `bom_line.quantity_per_unit/uom`
- **P** Packaging BOM — `bom_line.item_type='pm'` (pouch, label, desiccant, carton, tape…)
- **p** Stage-wise yield factors — `bom_line.loss_pct`, `bom_process_route.loss_pct`, `job_card_output.yield_pct`; no proactive norm per stage (moisture/giveaway)
- **p** Yield norm review — `yield_summary`, `job_card_loss_reconciliation` (budgeted vs actual) exist; no norm-update workflow

### 3. Inventory (real-time)

- **P** RM by lot AND grade — `inventory_batch.lot_number`, distinct W240/W320 rows; status machine (AVAILABLE/BLOCKED/FLAGGED/ISSUED/IN_TRANSIT/INTERNAL_HOLD/SCRAPPED)
- **p** WIP/semi-finished — `floor_inventory.item_type='wip'`, `floor_location='production_floor'`; no process-stage tag on WIP
- **P** Finished goods — `floor_inventory.item_type='fg'`, `fg_store`
- **P** Packaging stock — `item_type='pm'`, `pm_store`
- **P** QC hold — `inventory_batch.status='INTERNAL_HOLD'`, `batch_block_history`, `qc_inspection`
- **P** In transit — `status='IN_TRANSIT'`, `inventory_event_log`

### 4. RM procurement

- **—** Import lead times — `po_header` only has free-text `vendor_supplier_name`; no supplier master
- **—** Indian harvest seasonality calendar
- **—** Supplier MOQs / lead times
- **—** Price volatility / buying strategy — `po_line.rate` stored per-txn only, no history table
- **p** Shelf life & FIFO on RM — `inventory_batch.expiry_date`, FIFO via `get_available_batches()` order by `inward_date ASC`; no first-to-expire alerts in procurement

### 5. Capacity & process

- **P** Machine capacity per line — `machine_capacity(machine_id, stage, item_group, capacity_kg_per_hr)`; `machine.capable_stages[]`
- **p** Shifts/manpower — `production_plan_line.shift`, `job_card.assigned_to_team_leader`, `team_members[]`; no skill matrix or shift roster
- **—** Changeover time between SKUs
- **p** Allergen changeover — contamination flags (magnet/metal detector/roasting_pasteurization); no allergen_group or tree-nut↔peanut segregation
- **—** Bottleneck resource identification — `ai_planner.py` enforces 8-hr capacity but no bottleneck report

### 6. Quality & compliance

- **p** QC hold time (aflatoxin 24–48 h) — `qc_inspection.checkpoint_type/result` exist; no hold-duration timer
- **—** Microbiological clearance tracking
- **p** FSSAI/BRCGS batch docs — `job_card.best_before/batch_number/article_code/mrp/ean`, `job_card_sign_off`; no FSSAI license# or cert status per batch
- **P** Traceability lot coding — `job_card.batch_number` (B2026-042), `inventory_batch.lot_number`, full `inventory_event_log` audit
- **p** Customer spec approvals — `fulfillment_bom_override`, `bom_header.customer_name`; no approval status/date or deviation log

### 7. Shelf life & dispatch

- **—** Min shelf life on dispatch (75–80% rule)
- **—** FG buildup limits backward from expiry
- **—** Export vs domestic differentiation

### 8. Dry-fruit specifics

- **—** Incoming RM moisture content
- **P** Grade mix drives SKU feasibility — `all_sku` (W240/W320, kernel/inshell, etc.), `bom_line.can_use_offgrade`, `offgrade_max_pct`; no auto feasibility check though
- **p** Customer roast/salt profiles — `job_card.roasting_pasteurization`, `job_card_environment` (brine_salinity, temp, rpm, gas); no stored recipe master
- **P** Nitrogen/MAP gas — `job_card_environment.parameter_name='gas'`, PM SKU "Nitrogen Gas Cylinder"; no consumption/seal verification
- **p** Fumigation/pest cycles — `job_card.fumigation` boolean; no schedule/next-due table
- **—** Oil separation/rancidity risk on roasted FG

### 9. Planning horizon

- **—** 3–6 month RM procurement plan — `mrp.py` looks at pending POs only, no forward demand
- **p** 4–8 week MPS — `production_plan.date_from/date_to`, `plan_type` (daily/weekly/full); no firm/planned/forecast status or 8-wk rolling automation
- **P** 1–2 week daily schedule w/ sequencing — `production_plan_line` (fg_sku, machine, floor, shift, stage_sequence, priority, linked_so_fulfillment_ids), `job_card` chain (`prev/next_job_card_id`), `ai_planner.daily_plan_prompt`
- **P** Daily execution + replan — full `job_card` lifecycle; `production_plan.status='executed'`; `ai_recommendation` w/ `recommendation_type='revision'`

### 10. Feedback loop

- **p** Weekly plan vs actual — `production_plan_line.status`, `yield_summary.period`; no variance dashboard
- **P** RM consumption variance — `job_card_rm_indent.issued_qty` vs `reqd_qty`, `job_card_material_consumption.variance_qty`
- **P** Yield variance — `job_card_output.yield_pct`, `yield_summary`, `job_card_loss_reconciliation.variance_kg`
- **—** Schedule adherence & OEE — `job_card.start_time/end_time/total_time_min` but no scheduled vs actual / downtime
- **p** Root cause per plan miss — `discrepancy_report` (rm_grade_mismatch, qc_failure, breakdown, contamination…); not linked to plan lines
- **—** Periodic refresh of yield/changeover/lead-time norms — all `loss_pct` and `std_time_min` are static

### Summary

| Area | Score |
|------|-------|
| Inventory (real-time) | **6/6** |
| BOM & recipe | 3/4 |
| Demand inputs | 3/6 |
| Quality & compliance | 3/6 |
| Planning horizon | 2.5/4 |
| Feedback loop | 2.5/5 |
| Capacity & process | 2/5 |
| Dry-fruit specifics | 3/8 |
| Shelf life & dispatch | 1/3 |
| RM procurement | **1/5** |

### Biggest missing pieces for a proper dry-fruit MPS

1. **Supplier master + lead times + harvest seasonality** — cannot plan 3–6 month imports
2. **Forecast / rolling demand + festival seasonality** — everything is reactive off confirmed SOs
3. **Dry-fruit QC gates** — aflatoxin/micro hold timers, incoming moisture, rancidity surveillance
4. **Changeover matrix + allergen groups + OEE/bottleneck** — capacity is checked but not optimised
5. **Plan-vs-actual / schedule adherence / norm refresh** — execution data is captured but not fed back

### Already strong

Batch-level traceability, FIFO enforcement, BOM/route modelling, daily AI-generated schedule, job-card lifecycle with chaining, inventory-event audit log.

---

## User

From the BOM and Recipe which is missing?

---

## Assistant

From the BOM & Recipe section, nothing is fully absent — but 2 of 4 are **partial** with specific gaps.

### Partial (what's missing inside each)

**Stage-wise yield factors** — `bom_line.loss_pct` and `bom_process_route.loss_pct` store a single blanket loss % per line/route step, and `job_card_output.yield_pct` is computed after the fact. Missing:

- No **stage-specific norms** broken out for sorting vs roasting vs moisture-drop vs packaging giveaway
- No **moisture-loss lookup** (expected % weight-loss on roasting by product)
- No **packaging giveaway allowance** tracked separately from process loss
- No **standard yield by RM grade** (W240 vs W320 give different yields)

**Yield norm review (norm vs actual)** — `yield_summary`, `process_loss`, and `job_card_loss_reconciliation` capture what actually happened. Missing:

- No **variance-review workflow** (who reviews, when, threshold to act)
- No **norm-update mechanism** — `loss_pct`/`std_time_min` in BOM are static; nothing pulls last-4-week actuals and refreshes the norm
- No **history of norm revisions** (when a norm changed, what the old value was)
- No **per-customer / per-grade yield norm variants** (only one number on the BOM line)

### Also not in BOM but commonly considered part of "recipe"

- **Process parameters as a stored recipe** — `job_card_environment` captures temp/rpm/brine/gas/humidity **per execution**, but there's no `recipe_master` table holding the target roast temperature, time, salt %, or brine concentration **per SKU/customer**. Operators enter values job-by-job with no spec to validate against.
- **Alternate/substitution BOM** — `can_use_offgrade` + `offgrade_max_pct` exist, but no full alternate-material list (e.g., "if W240 short, use 70% W320 + 30% W450").
- **Scrap/rework routing** — loss is recorded but there's no BOM entry for what reject/scrap becomes (re-sort, re-roast, disposal).

Fully present: RM blend ratios, packaging BOM.

---

## User

Give me all the remaining points across every category.

---

## Assistant

All outstanding items (everything not fully PRESENT), grouped by category.

### BOM & recipe — 2 gaps

- **Stage-wise yield factors** (partial) — only single blanket `loss_pct` per line/route step; no moisture-loss curve, no packaging giveaway, no yield-by-grade
- **Yield norm review** (partial) — actuals captured, but no variance workflow, no auto-refresh of norms, no history of norm revisions

### Demand inputs — 4 gaps

- **Rolling 4–8 week forecast** (absent) — no forecast table or time-phased demand
- **Sales pipeline with probability weights** (absent)
- **Historical seasonality** (absent) — no festival calendar (Diwali/Rakhi/winter/Ramadan), no demand-pattern tagging
- **SKU retail-pouch vs bulk-pack** (partial) — `pack_size_kg` exists but no `customer_type` / `product_category` flag

### Quality & compliance — 4 gaps

- **QC hold time** (partial) — `qc_inspection` exists but no hold-duration target, no aflatoxin 24–48 h timer, no auto-release
- **Microbiological clearance timing** (absent) — no micro_test table, no hold-until-clearance_date
- **FSSAI / BRCGS batch documentation** (partial) — batch fields exist, but no FSSAI license #, BRCGS audit trail, or certification status per batch
- **Customer-specific spec approvals** (partial) — `fulfillment_bom_override` exists, but no approval_status/date, no deviation log

### Planning horizon — 2 gaps

- **3–6 month RM procurement plan** (absent) — MRP looks at pending POs only, no forward-looking procurement view
- **4–8 week Master Production Schedule** (partial) — daily/weekly plans exist but no MPS table, no firm/planned/forecast status, no rolling 8-wk automation

### Feedback loop — 4 gaps

- **Weekly plan vs actual tracking** (partial) — data exists, no automated variance report/dashboard
- **Schedule adherence & OEE** (absent) — no scheduled vs actual time comparison, no downtime tracking, no OEE
- **Root cause per plan miss** (partial) — `discrepancy_report` exists but not linked to plan lines; no 5-Why / fishbone structure
- **Periodic refresh of norms** (absent) — `loss_pct`, `std_time_min`, lead-time assumptions all static

### Capacity & process — 4 gaps

- **Shifts / manpower skill availability** (partial) — shift field + team_members exist; no manpower master, shift roster, or skill matrix
- **Changeover time between SKUs** (absent) — no `changeover_time_min`, no product-pair matrix
- **Allergen changeover / line clearance** (partial) — contamination flags exist; no allergen_group, no tree-nut ↔ peanut segregation rules
- **Bottleneck identification** (absent) — capacity enforced per job but no utilization % or bottleneck report

### Dry-fruit specifics — 4 gaps

- **Incoming RM moisture content** (absent) — no moisture spec or test on `inventory_batch` / PO lines
- **Customer-specific roast & salt profiles** (partial) — parameters logged per job, no recipe master (target temp/time/salt %)
- **Fumigation / pest control cycles** (partial) — boolean flag exists, no schedule / next-due-date / log table
- **Oil separation / rancidity risk** (absent) — no FFA test, no rancidity surveillance on roasted/flavored FG

### Shelf life & dispatch — 3 gaps (all absent)

- **Min shelf life on dispatch rule** (e.g., ≥ 75 %)
- **FG buildup limits working backward from expiry**
- **Export vs domestic customer differentiation** — no `order_type` / `export_flag`

### RM procurement — 5 gaps

- **Import lead times** (absent) — `po_header.vendor_supplier_name` is free text; no supplier master
- **Indian harvest seasonality calendar** (absent) — groundnut / raisin / walnut windows not modelled
- **Supplier MOQs + lead times** (absent) — no supplier_master table
- **Price volatility / buying strategy** (absent) — transaction prices only, no history / trend / buying-window optimization
- **Shelf life & FIFO constraints on RM** (partial) — FIFO enforced on issuance; no first-to-expire alerts on procurement plans

**Total: 31 items** to close — heaviest lifts are **supplier master + procurement intelligence** and **forecast/seasonality**, since those unlock several downstream categories (planning horizon, feedback norms, shelf-life-aware dispatch).

---

## User

This is a very initial level software and also these will be monitored after 2–3 months and the data will be created later, so accordingly tell me which gaps actually matter now.

---

## Assistant

Good framing — most of those 31 gaps split cleanly into **structural (needs to exist NOW so data is captured correctly)** vs **analytics (need 2–3 months of history to be meaningful)**. If you add the analytics layer today it just runs on empty.

### Do NOW — low-effort schema additions (so data accumulates correctly)

These are single-column or small-table adds. Cost is low, but skipping them means 3 months of PO/job/SO data arrives without the tag you'll need later.

| Gap | Why now | What to add |
|---|---|---|
| Supplier master | Every PO from today should point to a real supplier, not free text — otherwise lead-time / price history is unreconstructable later | `supplier_master(supplier_id, name, lead_time_days, moq_kg, is_import)` + FK from `po_header` |
| Customer order type | One-time tag per customer; unlocks export/domestic + retail/bulk logic later | `customer.order_type` (domestic/export), `customer.segment` (retail/bulk/institutional) |
| Allergen group on SKU | Tag once across ~50 SKUs; needed to prevent tree-nut ↔ peanut cross-contamination | `all_sku.allergen_group` (tree_nut / peanut / seed / dried_fruit / mixed) |
| Incoming RM moisture | Operators will enter it on GRN going forward — can't backfill | `inventory_batch.moisture_pct`, `po_line.moisture_spec_max` |
| Festival / season tag | Tag SOs as they come in; history is useless without it | `so_header.season_tag` (diwali/rakhi/winter/ramadan/regular) |
| Recipe master | Without a target, `job_card_environment` values are just numbers with no spec to validate | `recipe_master(sku, customer, target_roast_temp, target_roast_min, salt_pct, brine_conc)` |
| Min shelf-life-on-dispatch policy | Simple rule column; prevents bad dispatches immediately | `customer.min_shelf_life_pct` (default 75) |
| QC hold duration per test | Policy, not data — lock it in now | `qc_test_master(test_type, min_hold_hours)` (aflatoxin=24, micro=48…) |

**Total: ~8 small schema changes.** Do these in the next sprint so that by month 3 you have clean data to analyse.

### Build as data arrives (month 2–3)

These need baseline history to work — building them now gives you empty dashboards.

- **Yield norm review / refresh workflow** — needs 4+ weeks of `yield_summary` data to compare against
- **Plan-vs-actual weekly tracking** — needs `production_plan_line.status` history
- **OEE / schedule adherence** — needs scheduled-vs-actual job times over 30+ days
- **Price volatility tracking** — needs PO history across multiple cycles
- **Changeover time matrix** — measure it once from real job timings, then codify
- **Bottleneck identification** — needs utilization data from real schedules
- **Root cause per plan miss** — `discrepancy_report` exists; just link it to `production_plan_line_id`

**When to build:** trigger at month 2 when you have 20–30 production plans and ~60 days of job cards.

### Defer (3–6 months or later)

Business-stage dependent — not blocking MPS today:

- **Rolling 4–8 week forecast** — needs sales team + customer ordering history first
- **Sales pipeline with probability** — only if there's a CRM feeding it
- **FG buildup limits vs expiry** — only matters when FG inventory is deep enough to risk expiry
- **Fumigation / pest schedule** — Excel-fine until volume justifies
- **Microbiological clearance workflow** — informal process fine until BRCGS audit
- **FSSAI / BRCGS audit trail** — formalise before next audit, not today
- **Oil separation / rancidity surveillance** — only when roasted FG inventory + ageing becomes a real problem
- **Offgrade / alternate BOM substitution logic** — current `can_use_offgrade` is enough for V1
- **Scrap / rework routing** — manual handling fine until rework volume is measurable
- **Harvest seasonality calendar** — hardcode into supplier master notes for now

### Recommended sequence

1. **Next 2 weeks** — add the 8 structural columns/tables above. Backfill obvious values (existing vendors → supplier master, existing SKUs → allergen_group).
2. **Weeks 3–8** — start capturing data against new fields. No dashboards yet, just make sure operators enter moisture/season/supplier.
3. **Month 2** — build the first analytics (plan-vs-actual weekly, yield variance trend) once you have ~30 plans and 60 days of jobs.
4. **Month 3** — norm-refresh workflow, OEE, bottleneck reports. Now there's real data to drive them.
5. **Month 4+** — forecast module, pipeline, audit trails, rancidity surveillance — by then you'll know which of these the business actually pulls on.

The mistake to avoid: building dashboards now on empty tables. The mistake to *also* avoid: running 3 months of production without adding the structural tags, then trying to retrofit supplier / season / moisture / allergen data from memory.

---

## User

From the 8 above:

1. Customer order type can be mapped using the `sales_group` from `all_sku`.
2. Remove allergen group.
3. Remove festival / season.
4. Remove QC Hold.

---

## Assistant

Updated "Do NOW" list — **4 structural adds + 1 mapping**:

| # | Gap | What to do |
|---|---|---|
| 1 | **Supplier master** | New table `supplier_master(id, name, lead_time_days, moq_kg, is_import)`; FK from `po_header` |
| 2 | **Incoming RM moisture** | Add `inventory_batch.moisture_pct` + `po_line.moisture_spec_max`; enforce entry on GRN |
| 3 | **Recipe master** | New table `recipe_master(sku, customer, target_roast_temp, target_roast_min, salt_pct, brine_conc)`; referenced by job card |
| 4 | **Min shelf-life on dispatch** | Add `customer.min_shelf_life_pct` (default 75); gate dispatch in fulfillment logic |
| 5 | **Customer order type** | No schema change — derive retail/bulk/export from `all_sku.sales_group` in queries/views; standardise the `sales_group` values so the mapping is deterministic |

On #5, worth doing a quick audit of current `sales_group` values to confirm they actually partition cleanly into retail / bulk / export / institutional — if the column has free-text drift ("Retail", "retail", "RETAIL-HORECA"), fix that before relying on it as a derivation source.

---

## Agreed next step

Audit distinct values of `all_sku.sales_group` to confirm it can reliably stand in for a customer order-type field, then design the 4 structural schema changes (supplier master, RM moisture, recipe master, min-shelf-life-on-dispatch) for the next sprint.
