**Date:** 2026-03-26
**Time:** 00:36 IST

---

# PRODUCTION PLANNING MODULE - COMPLETE LOGICAL PLAN
## Candor Foods Pvt. Ltd. (CFPL & CDPL)

---

## TABLE OF CONTENTS

1. [System Overview & Actors](#1-system-overview--actors)
2. [Master Data Logic](#2-master-data-logic)
3. [Daily Operational Cycle - Step by Step](#3-daily-operational-cycle)
4. [SO Fulfillment & FY Transition Logic](#4-so-fulfillment--fy-transition-logic)
5. [Claude AI Plan Generation Logic](#5-claude-ai-plan-generation-logic)
6. [MRP & Inventory Check Logic](#6-mrp--inventory-check-logic)
7. [Indent & Alert Logic](#7-indent--alert-logic)
8. [Production Order Logic](#8-production-order-logic)
9. [Job Card Engine Logic (Multi-Stage Sequential)](#9-job-card-engine-logic)
10. [QR Code Material Receipt Logic](#10-qr-code-material-receipt-logic)
11. [Job Card Execution & Completion Logic](#11-job-card-execution--completion-logic)
12. [Day-End Dispatch & Loss Reconciliation Logic](#12-day-end-dispatch--loss-reconciliation-logic)
13. [Floor-to-Floor Inventory Logic](#13-floor-to-floor-inventory-logic)
14. [Off-Grade Reuse Logic](#14-off-grade-reuse-logic)
15. [Process Loss & Anomaly Detection Logic](#15-process-loss--anomaly-detection-logic)
16. [Quality Inspection Logic](#16-quality-inspection-logic)
17. [Plan Revision Logic (Adhoc Orders)](#17-plan-revision-logic)
18. [Idle Material Alert Logic](#18-idle-material-alert-logic)
19. [Day-End Balance Scan & Reconciliation Logic](#19-day-end-balance-scan--reconciliation-logic)
20. [Internal Discrepancy Plan Revision Logic](#20-internal-discrepancy-plan-revision-logic)
21. [Frontend Logic & Screens](#21-frontend-logic--screens)
22. [Data Relationships & Entity Map](#22-data-relationships--entity-map)
23. [Error Handling & Edge Cases](#23-error-handling--edge-cases)

---

## 1. SYSTEM OVERVIEW & ACTORS

### 1.1 Actors (Users of the System)

| Actor | Role | Key Actions |
|-------|------|-------------|
| **Sales Team** | Punch SOs daily | Create/upload sales orders |
| **Production Planner** | Approve/revise plans | Trigger Claude plan generation, approve plans, handle adhoc orders |
| **Purchase Team** | Procure RM/PM | Receive indents, create POs, track procurement |
| **Stores Team** | Manage inventory | Receive materials (QR scan), issue to production, track stock |
| **Team Leader** | Execute job cards | Receive assigned job cards, start production, record output |
| **Floor Manager** | Oversee production | QR scan to receive material, update day-end dispatch, record losses |
| **QC Inspector** | Quality checks | Inspect at each checkpoint, pass/fail, sign-off |
| **Production Incharge** | Authorize | Approve plans, sign-off job cards, authorize force-unlock |
| **Plant Head** | Final authority | Force-unlock authorization, annexure sign-off |

### 1.2 System Components

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  SO Module   │────>│  Production  │────>│  Job Card       │
│  (existing)  │     │  Planning    │     │  Engine         │
└─────────────┘     │  (Claude AI) │     │  (Sequential)   │
                     └──────────────┘     └────────┬────────┘
┌─────────────┐            │                       │
│  PO Module   │────>┌─────┴──────┐          ┌─────┴──────┐
│  (existing)  │     │  MRP &     │          │  Floor     │
└─────────────┘     │  Inventory │          │  Execution │
                     └────────────┘          └────────────┘
```

---

## 2. MASTER DATA LOGIC

### 2.1 BOM (Bill of Materials) Logic

**What a BOM represents:** A recipe/formula for producing ONE finished good (FG). For white-labeling, the SAME raw material can appear in different BOMs with different packaging for different customers.

**BOM Structure:**
```
BOM Header (1 per FG-customer-pack combination)
  ├── BOM Lines (N materials: RM + PM)
  │     ├── Line 1: AL BARAKAH FARD DATES STANDARD (RM, 1.25 kg/unit, 2.0% loss)
  │     ├── Line 2: PM24-Dates Arabian 500g Pouch (PM, 1 pc/unit, 1.0% loss)
  │     ├── Line 3: PM24-Carton 12x500g (PM, 0.083 pc/unit, 0.5% loss)
  │     └── Line 4: PM24-Sticker Label 500g (PM, 1 pc/unit, 1.0% loss)
  └── Process Route (N steps in sequence)
        ├── Step 1: Sorting (60 min, 2.0% loss, QC: visual + foreign matter)
        ├── Step 2: Weighing (30 min, 0.5% loss, QC: net weight ±2g)
        ├── Step 3: Sealing (45 min, 0.2% loss, QC: seal integrity)
        └── Step 4: Metal Detection (20 min, 0.1% loss, QC: Fe/Nfe/SS pass)
```

**BOM Lookup Logic:**
```
FUNCTION: find_bom(fg_sku_name, customer_name, pack_size_kg)
  1. Search bom_header WHERE:
     - fg_sku_name matches (fuzzy with match_sku())
     - customer_name matches (exact) OR customer_name IS NULL (generic)
     - pack_size_kg matches
     - is_active = TRUE
     - effective_from <= TODAY AND (effective_to IS NULL OR effective_to >= TODAY)
  2. IF customer-specific BOM found → use it
  3. ELSE IF generic BOM found → use it
  4. ELSE → flag as "BOM not found" error
  5. Always use latest version (ORDER BY version DESC LIMIT 1)
```

**Gross Qty Calculation (from job card sample):**
```
gross_qty = reqd_qty / (1 - loss_pct / 100)

Example: Dates 1,250 kg with 2.0% loss
  gross_qty = 1250 / (1 - 0.02) = 1250 / 0.98 = 1,275.510 kg
```

### 2.2 Machine & Capacity Logic

**Machine model:** One machine can handle MULTIPLE stages, with DIFFERENT capacities per product.

```
Machine "Sorting Table L1"
  ├── capable_stages: ['sorting', 'grading']
  ├── Capacity for SORTING:
  │     ├── cashew: 200 kg/hr
  │     ├── almond: 180 kg/hr
  │     ├── dates:  250 kg/hr (softer, faster to sort)
  │     └── raisin: 150 kg/hr
  └── Capacity for GRADING:
        ├── cashew: 180 kg/hr
        └── almond: 160 kg/hr
```

**Machine Selection Logic:**
```
FUNCTION: find_best_machine(stage, item_group, required_qty_kg, date)
  1. Query machine_capacity WHERE stage AND item_group match
  2. Join machine WHERE status = 'active'
  3. Check machine is not already fully scheduled on that date
  4. Calculate: estimated_hours = required_qty_kg / capacity_kg_per_hr
  5. Check: estimated_hours <= remaining available hours for that shift
  6. Prefer machine that's already running same product (no changeover)
  7. Return: machine_id, capacity_kg_per_hr, estimated_hours
```

### 2.3 SKU Master Integration

The existing `all_sku` table (~3,654 items) is the master reference. All BOM materials are matched against it using the existing `match_sku()` fuzzy matching function (75% threshold, token_sort_ratio).

---

## 3. DAILY OPERATIONAL CYCLE

This is the complete step-by-step logic for ONE day of production:

### Step 3.1: SO Punch (Morning)

```
TRIGGER: Sales team uploads/creates SOs via existing SO module

LOGIC:
  1. SOs land in so_header + so_line (existing tables)
  2. System auto-syncs to so_fulfillment:
     FOR each new so_line WHERE item_type = 'fg':
       a. Check if so_fulfillment record exists for this so_line_id
       b. IF NOT EXISTS:
          - Create so_fulfillment record
          - Set financial_year based on so_date (Apr-Mar → "2026-27")
          - Set original_qty_kg from so_line quantity * uom
          - Set pending_qty_kg = original_qty_kg
          - Set order_status = 'open'
          - Set delivery_deadline from SO date + lead time
          - Set priority based on customer importance + delivery urgency

OUTPUT: Updated so_fulfillment table with all pending demand
```

### Step 3.2: Claude Creates/Revises Plan

```
TRIGGER: Production planner clicks "Generate Daily Plan" OR system auto-triggers

INPUT DATA GATHERED:
  1. All pending so_fulfillment WHERE order_status IN ('open', 'partial', 'carryforward')
     - Group by fg_sku_name, customer_name, delivery_deadline
     - Flag carryforward orders separately
  2. Current floor_inventory at rm_store and pm_store
  3. Available machines (not in maintenance) with per-hour capacity
  4. In-progress job cards (what's already on the floor)
  5. Historical avg loss % by product + machine (last 90 days from process_loss)
  6. Available off-grade stock with reuse rules
  7. Pending purchase indents (material expected)
  8. Today's date and shift schedule

CLAUDE PROCESSING:
  Claude receives all this as structured JSON and returns:
  {
    "daily_schedule": [
      {
        "date": "2026-04-01",
        "production_lines": [
          {
            "fg_sku_name": "Al Barakah Dates Arabian 500g",
            "customer": "D-Mart",
            "bom_id": 42,
            "qty_units": 2500,
            "qty_kg": 1250,
            "machine_id": 3,
            "stage_sequence": ["sorting", "weighing", "sealing", "metal_detection"],
            "estimated_hours": 2.6,
            "priority": 1,
            "loss_adjusted_rm_kg": {"dates_raw": 1275.51},
            "linked_so_ids": [118],
            "reasoning": "D-Mart delivery deadline Apr 3, material available"
          }
        ]
      }
    ],
    "material_check": {
      "sufficient": [...],
      "shortages": [
        {"material": "Almond California", "needed": 700, "available": 300, "gap": 400}
      ]
    },
    "indent_recommendations": [...],
    "offgrade_reuse": [...],
    "risk_flags": [...]
  }

POST-PROCESSING:
  1. Create production_plan record (status = 'draft', ai_generated = TRUE)
  2. Create production_plan_line records from Claude's output
  3. Store full Claude analysis in ai_analysis_json
  4. Log to ai_recommendation table
  5. Return plan for planner review

PLANNER ACTION:
  - Review plan on dashboard
  - Modify priorities/quantities if needed
  - Click "Approve" → plan.status = 'approved'
```

### Step 3.3: Inventory Check (MRP Run)

```
TRIGGER: Runs automatically when plan is approved

LOGIC (for each plan_line):
  1. Look up BOM by bom_id
  2. FOR each bom_line:
     a. Calculate gross_requirement:
        gross_req = plan_line.planned_qty_kg * bom_line.quantity_per_unit
     b. Add loss allowance:
        loss_adjusted = gross_req / (1 - bom_line.loss_pct / 100)
     c. Check off-grade reuse:
        IF bom_line.can_use_offgrade:
          offgrade_available = SUM(offgrade_inventory WHERE item_group matches AND status='available')
          offgrade_to_use = MIN(offgrade_available, loss_adjusted * offgrade_max_pct/100)
          net_requirement = loss_adjusted - offgrade_to_use
        ELSE:
          net_requirement = loss_adjusted
     d. Check floor_inventory:
        on_hand = floor_inventory WHERE sku_name matches AND floor_location IN ('rm_store','pm_store')
     e. Check incoming POs:
        on_order = SUM(po_line WHERE sku matches AND status='pending' AND po_date <= needed_by)
     f. Calculate:
        available = on_hand + on_order
        shortage = MAX(0, net_requirement - available)
        surplus = MAX(0, available - net_requirement)

  3. Aggregate across all plan lines

OUTPUT:
  {
    "materials": [
      {
        "sku": "AL BARAKAH FARD DATES STANDARD",
        "type": "rm",
        "total_needed_kg": 1275.51,
        "on_hand_kg": 2000,
        "on_order_kg": 0,
        "shortage_kg": 0,
        "status": "SUFFICIENT"
      },
      {
        "sku": "Almond California Raw",
        "type": "rm",
        "total_needed_kg": 1500,
        "on_hand_kg": 800,
        "on_order_kg": 300,
        "shortage_kg": 400,
        "status": "SHORTAGE"
      }
    ]
  }

ROUTING:
  IF all materials SUFFICIENT → proceed to Step 3.5 (Create Job Cards)
  IF any SHORTAGE → proceed to Step 3.4 (Indent + Alert)
  Note: Plan lines with sufficient material proceed immediately;
        lines with shortage wait for material arrival
```

### Step 3.4: Indent + Alert (When Material Unavailable)

```
TRIGGER: MRP identifies shortage

LOGIC:
  FOR each material with SHORTAGE:
    1. Create purchase_indent:
       - indent_number = "IND-{YYYYMMDD}-{seq}"
       - material_sku_name = shortage material
       - required_qty = shortage_kg
       - required_by_date = earliest delivery deadline of affected SOs
       - priority = based on SO urgency
       - status = 'raised'

    2. Create store_alert:
       - alert_type = 'material_shortage'
       - target_team = 'purchase' (for indent)
       - message = "SHORTAGE: {material} - Need {qty}kg by {date} for {customer} order"

    3. Create store_alert:
       - alert_type = 'indent_raised'
       - target_team = 'stores'
       - message = "Indent raised for {material} {qty}kg. Check existing stock."

WHEN MATERIAL ARRIVES (via PO receiving):
    4. PO module records receipt (existing po_box flow)
    5. Trigger: auto-update floor_inventory (rm_store += received qty)
    6. Trigger: re-run MRP check for affected plan lines
    7. IF now sufficient → auto-proceed to job card creation
    8. Update purchase_indent.status = 'received'
    9. Create store_alert: "Material received: {material} {qty}kg. Ready for production."
```

### Step 3.5: Create Job Cards (Multi-Stage Sequential)

```
TRIGGER: MRP confirms material available for a plan_line

LOGIC:

  STEP A - Create Production Order:
    1. Generate prod_order_number: "PO-{YYYY}-{seq:4}"
    2. Look up BOM header → populate product details
    3. Look up BOM process route → determine total_stages
    4. Calculate batch details:
       - batch_number = "B{YYYY}-{seq:3}"
       - batch_size_kg = planned_qty_kg
       - net_wt_per_unit = pack_size_kg
       - best_before = today + shelf_life_days
    5. Insert production_order record
    6. Set status = 'created'

  STEP B - Create Job Cards (one per stage):
    FOR each step in bom_process_route (ORDER BY step_number):
      1. Generate job_card_number: "{prod_order_number}/{step_number}"
         Example: "PO-2026-0042/1", "PO-2026-0042/2", "PO-2026-0042/3"

      2. Create job_card:
         - Inherit all product details from production_order
         - Set process_name from route step
         - Set factory, floor from production_order

      3. LOCKING LOGIC:
         IF step_number == 1:
           status = 'unlocked'
           is_locked = FALSE
         ELSE:
           status = 'locked'
           is_locked = TRUE
           locked_reason = 'awaiting_previous_stage'

      4. Create job_card_rm_indent lines:
         FOR each bom_line WHERE item_type = 'rm':
           - reqd_qty = planned_qty * quantity_per_unit
           - loss_pct = from bom_line
           - gross_qty = reqd_qty / (1 - loss_pct/100)
           - godown = from bom_line.godown
           - status = 'pending'
         (RM indent is only on FIRST job card usually, unless intermediate stages need more)

      5. Create job_card_pm_indent lines:
         FOR each bom_line WHERE item_type = 'pm':
           - Same calculation as RM
           - godown = 'PM Store' typically
         (PM indent is usually on the PACKAGING stage job card)

      6. Create job_card_process_step records:
         - Copy from bom_process_route for this stage
         - Include machine, std_time, qc_check, loss_pct

    STEP C - Assign first job card to team leader:
      1. Find team leader for the first stage's floor
      2. Set job_card.assigned_to_team_leader
      3. Set job_card.status = 'assigned'
      4. Create alert: "New job card {number} assigned to you: {product} {qty}"

    STEP D - Update production_order:
      1. Set status = 'job_cards_issued'
      2. Set total_stages = count of created job cards

OUTPUT: Array of created job cards, first one unlocked+assigned, rest locked
```

### Step 3.6: Floor Manager Receives Material (QR Scan)

```
TRIGGER: Floor manager scans QR code on physical boxes

LOGIC:
  1. Floor manager opens job card on mobile/tablet
  2. Navigates to "Receive Material" section
  3. Scans QR code on each physical box

  FOR each scanned QR code:
    a. Look up po_box WHERE box_id = scanned_code
    b. Validate:
       - Box exists in system
       - Box not already consumed (check if scanned in another job card)
       - Material matches what's on the indent (sku_name match)
    c. Get: net_weight, gross_weight, lot_number from po_box
    d. Match against job_card_rm_indent or job_card_pm_indent:
       - Find the indent line where material_name matches
       - Add to scanned_box_ids array
       - Accumulate issued_qty += net_weight
       - Set batch_no = lot_number
    e. Deduct from floor_inventory:
       - UPDATE floor_inventory SET quantity_kg -= net_weight
         WHERE sku_name = material AND floor_location = 'rm_store'
    f. Create floor_movement:
       - from_location = 'rm_store' (or 'pm_store')
       - to_location = job_card.floor (production floor)
       - reason = 'production'
       - scanned_qr_codes = [box_id]

  4. When all indent lines satisfied (issued_qty >= gross_qty):
     - Update job_card.status = 'material_received'
     - Job card is now ready for production start

  5. IF partial receipt:
     - Flag: "Material partially received. {X}kg still needed."
     - Allow production to start with partial (floor manager decision)
```

### Step 3.7: Job Card Execution & Completion

```
TRIGGER: Team leader clicks "Start Production" on job card

START:
  1. Validate job_card.status IN ('material_received', 'unlocked')
  2. Set status = 'in_progress'
  3. Set start_time = NOW()
  4. Set team_leader, team_members

DURING PRODUCTION (for each process step):
  FOR each job_card_process_step:
    1. Team leader records:
       - operator_name
       - Machine used
       - Start time
    2. QC inspector checks at checkpoint:
       - Records pass/fail for qc_check
       - Signs off (qc_sign_at)
    3. Mark step as completed:
       - time_done = NOW()
       - status = 'completed'

  Environmental recording (if applicable):
    - Record humidity, temperature, RPM, etc. in job_card_environment

  Metal detection (if applicable):
    - Record Fe/Nfe/SS pass/fail in job_card_metal_detection

  Weight checks (if applicable):
    - Record 20-sample weight/leak checks in job_card_weight_check

COMPLETION:
  1. Team leader clicks "Complete Job Card"
  2. Record output in job_card_output:
     - FG output: expected vs actual (units + kg)
     - RM consumed: expected vs actual
     - Material return (unused RM/PM)
     - Rejection (kg)
     - Process loss (kg and %)
     - Off-grade output (kg + category)
  3. Record loss reconciliation in job_card_loss_reconciliation:
     - Sorting Rejection: budgeted vs actual
     - Roasting/Process Loss: budgeted vs actual
     - Packaging Rejection: budgeted vs actual
     - Metal Detector Rejection: budgeted vs actual
     - Spillage/Handling: budgeted vs actual
     - QC Sample Consumed: budgeted vs actual
  4. Set job_card.status = 'completed'
  5. Set end_time = NOW(), total_time_min = end - start

  >>> TRIGGER NEXT STAGE UNLOCK (see Step 3.7.1)
```

### Step 3.7.1: Next Stage Auto-Unlock Logic

```
TRIGGER: job_card.status set to 'completed'

LOGIC:
  1. Find next job card:
     next_jc = job_card WHERE prod_order_id = same AND stage_number = current + 1

  2. IF next_jc EXISTS:
     a. Set next_jc.status = 'unlocked'
     b. Set next_jc.is_locked = FALSE
     c. Set next_jc.unlocked_at = NOW()
     d. Set next_jc.unlocked_by = 'system_auto'
     e. Set next_jc.locked_reason = NULL

     f. Move SFG (semi-finished goods) in floor_inventory:
        - Create floor_movement: from current_floor → next_floor
        - Update floor_inventory: deduct from current, add to next

     g. Assign to next team leader:
        - Set next_jc.assigned_to_team_leader = team leader for that floor
        - Set next_jc.status = 'assigned'
        - Create alert: "Job card {next_jc.number} UNLOCKED and assigned to you"

  3. IF this was the LAST stage (no next_jc):
     a. Set production_order.status = 'completed'
     b. Move FG to fg_warehouse:
        - Create floor_movement: from packaging → fg_warehouse
        - Update floor_inventory: add to fg_warehouse
     c. Update so_fulfillment:
        - produced_qty_kg += actual FG output
        - IF produced_qty_kg >= pending_qty_kg:
            order_status = 'fulfilled'
          ELSE:
            order_status = 'partial'

  4. Update production_order.completed_stages += 1

FORCE UNLOCK LOGIC:
  TRIGGER: Production incharge or plant head requests force unlock

  1. Validate requester has authority (production_incharge OR plant_head role)
  2. Set job_card.status = 'unlocked'
  3. Set is_locked = FALSE
  4. Set force_unlocked = TRUE
  5. Set force_unlock_reason = provided reason (mandatory)
  6. Set force_unlock_authority = requester name
  7. Create alert: "Job card {number} FORCE UNLOCKED by {authority}. Reason: {reason}"
  8. Log to audit trail (log_edit table, module='production')
```

### Step 3.8: Day-End Dispatch Update

```
TRIGGER: Floor manager performs day-end update (typically 5-6 PM)

LOGIC:
  1. Floor manager opens "Day-End Dashboard"
  2. System shows all completed job cards for today with FG output
  3. FOR each completed final-stage job card:
     a. Floor manager enters dispatch_qty_units and dispatch_qty_kg
        (how much of the FG produced today was dispatched/ready for dispatch)
     b. Update job_card_output:
        - dispatch_qty_units = entered value
        - dispatch_qty_kg = entered value
        - dispatched_at = NOW()
        - dispatched_by = floor_manager_name
     c. Update so_fulfillment:
        - dispatched_qty_kg += dispatch_qty_kg
        - Recalculate pending_qty_kg

  4. Sign-offs:
     - Production incharge signs: production_incharge_name, sign_at
     - QC analyst signs: quality_analyst_name, sign_at
     - Warehouse incharge signs: warehouse_incharge_name, sign_at

  5. Close job card:
     - Set status = 'closed'
     - All sign-offs must be present

  6. System auto-generates:
     - process_loss record (from job_card_output data)
     - yield_summary update (if period boundary)
     - AI anomaly check (if loss variance > threshold)
```

---

## 4. SO FULFILLMENT & FY TRANSITION LOGIC

### 4.1 Financial Year Determination

```
FUNCTION: get_financial_year(date)
  IF date.month >= 4:  # Apr onwards
    RETURN f"{date.year}-{(date.year+1) % 100:02d}"  # "2026-27"
  ELSE:  # Jan-Mar
    RETURN f"{date.year-1}-{date.year % 100:02d}"     # "2025-26"
```

### 4.2 FY Close Manual Review Workflow

```
TRIGGER: Production planner opens "FY Close Review" dashboard (end of March)

DASHBOARD SHOWS:
  All so_fulfillment WHERE:
    financial_year = current_fy (e.g., "2025-26")
    AND order_status IN ('open', 'partial')
  Grouped by customer, then by product

FOR EACH unfulfilled order, planner can:

  OPTION A - CARRY FORWARD:
    1. Create new so_fulfillment record:
       - financial_year = new_fy ("2026-27")
       - original_qty_kg = old.pending_qty_kg (remaining balance)
       - carryforward_from = old.fulfillment_id
       - order_status = 'carryforward'
       - priority = old.priority (or elevated)
    2. Update old record:
       - order_status = 'carryforward'
    3. Log in so_revision_log

  OPTION B - REVISE QUANTITY:
    1. Update so_fulfillment:
       - revised_qty_kg = new quantity
       - revision_reason = "FY close customer revision"
       - revision_date = TODAY
       - order_status = 'revised'
    2. Recalculate pending_qty_kg = revised_qty_kg - produced_qty_kg
    3. Log in so_revision_log (field='quantity', old, new, reason)

  OPTION C - CANCEL:
    1. Update so_fulfillment:
       - order_status = 'cancelled'
       - revision_reason = "FY close - order cancelled"
    2. Log in so_revision_log

AFTER REVIEW COMPLETE:
  - All remaining 'open' orders that were NOT acted on stay as-is
  - Planner can do bulk carryforward of remaining orders
  - System creates summary report of FY close actions
```

---

## 5. CLAUDE AI PLAN GENERATION LOGIC

### 5.1 Data Collection for Claude

```
FUNCTION: collect_planning_context(entity, plan_type, date_range)

  RETURNS:
  {
    "demand": {
      "pending_orders": [  // from so_fulfillment
        {
          "fulfillment_id": 42,
          "customer": "D-Mart",
          "product": "Al Barakah Dates Arabian 500g",
          "pending_qty_kg": 1250,
          "pending_units": 2500,
          "delivery_deadline": "2026-04-03",
          "priority": 1,
          "is_carryforward": false,
          "financial_year": "2026-27"
        }
      ],
      "total_demand_kg": 45000,
      "carryforward_count": 5,
      "carryforward_kg": 3200
    },
    "supply": {
      "rm_stock": [  // from floor_inventory WHERE floor_location = 'rm_store'
        {"sku": "Dates Fard Standard", "available_kg": 5000},
        {"sku": "Cashew W320 Raw", "available_kg": 3000}
      ],
      "pm_stock": [...],
      "incoming_pos": [  // from po_line WHERE status = 'pending'
        {"sku": "Almond California", "qty_kg": 1000, "expected_date": "2026-04-02"}
      ]
    },
    "machines": [  // from machine + machine_capacity
      {
        "machine_id": 3,
        "name": "Sorting Table L1",
        "capable_stages": ["sorting", "grading"],
        "capacities": {
          "sorting": {"cashew": 200, "almond": 180, "dates": 250},
          "grading": {"cashew": 180, "almond": 160}
        },
        "status": "active",
        "shift_hours": 8.0,
        "setup_time_min": 30
      }
    ],
    "boms": [...],  // relevant BOMs for demanded products
    "loss_history": {  // from process_loss, last 90 days
      "dates": {"avg_loss_pct": 2.8, "stddev": 0.5, "batches": 45},
      "cashew": {"avg_loss_pct": 5.2, "stddev": 1.3, "batches": 62}
    },
    "offgrade_stock": [...],  // from offgrade_inventory WHERE status = 'available'
    "reuse_rules": [...],     // from offgrade_reuse_rule WHERE is_active = TRUE
    "in_progress_jobs": [...],  // currently running job cards
    "maintenance_schedule": [...]
  }
```

### 5.2 Claude Prompt Structure

```
SYSTEM PROMPT:
  "You are a production planning optimizer for Candor Foods, an FMCG dry fruit
  manufacturer doing white-label production for brands like D-Mart, BigBasket, etc.

  CONSTRAINTS:
  - Each machine can only process products listed in its capacity table
  - Changeover between products costs {setup_time_min} minutes
  - Process loss varies by product (historical data provided)
  - Off-grade from one line can feed another (reuse rules provided)
  - Carryforward orders from previous FY must be prioritized
  - White label orders have specific BOMs (different packaging per customer)
  - BRCGS food safety compliance required

  OUTPUT: Return ONLY valid JSON matching the schema."

USER PROMPT:
  "Generate a {plan_type} plan for {date_range}.

  PENDING DEMAND: {json}
  MACHINES & CAPACITY: {json}
  RM/PM AVAILABILITY: {json}
  BOMs: {json}
  LOSS HISTORY: {json}
  OFF-GRADE STOCK: {json}
  REUSE RULES: {json}
  IN-PROGRESS JOBS: {json}

  OPTIMIZE FOR:
  1. Carryforward orders first (close previous FY)
  2. Meet delivery deadlines
  3. Minimize machine changeovers
  4. Use off-grade stock where possible
  5. Account for expected process loss
  6. Balance load across machines
  7. Flag shortages early

  Return JSON: { daily_schedule, material_check, indent_recommendations, ... }"
```

---

## 6. MRP & INVENTORY CHECK LOGIC

### 6.1 MRP Algorithm

```
FUNCTION: run_mrp(plan_id)

  plan_lines = SELECT * FROM production_plan_line WHERE plan_id = plan_id

  material_requirements = {}

  FOR each plan_line:
    bom = get_bom(plan_line.bom_id)

    FOR each bom_line in bom.lines:
      # Calculate requirement
      if bom_line.item_type == 'rm':
        reqd = plan_line.planned_qty_kg * bom_line.quantity_per_unit
      else:  # pm
        reqd = plan_line.planned_units * bom_line.quantity_per_unit

      gross = reqd / (1 - bom_line.loss_pct / 100)

      # Check off-grade substitution
      offgrade_use = 0
      if bom_line.can_use_offgrade:
        offgrade_available = get_offgrade_stock(bom_line.material_sku_name)
        offgrade_use = min(offgrade_available, gross * bom_line.offgrade_max_pct / 100)
        gross -= offgrade_use

      # Accumulate
      key = bom_line.material_sku_name
      material_requirements[key] = material_requirements.get(key, 0) + gross

  # Compare against inventory
  results = []
  FOR material, needed in material_requirements:
    on_hand = get_floor_inventory(material, ['rm_store', 'pm_store'])
    on_order = get_pending_po(material)
    available = on_hand + on_order
    shortage = max(0, needed - available)

    results.append({
      "material": material,
      "needed_kg": needed,
      "on_hand_kg": on_hand,
      "on_order_kg": on_order,
      "shortage_kg": shortage,
      "status": "SHORTAGE" if shortage > 0 else "SUFFICIENT"
    })

  RETURN results
```

---

## 7. INDENT & ALERT LOGIC

```
FUNCTION: auto_raise_indents(mrp_results, plan_id)

  FOR each result WHERE status == 'SHORTAGE':
    1. Check if indent already exists for this material + plan:
       existing = SELECT FROM purchase_indent WHERE material AND plan_id AND status != 'cancelled'
       IF existing: skip (don't duplicate)

    2. Create purchase_indent:
       indent_number = generate_indent_number()
       required_qty = result.shortage_kg
       required_by_date = earliest delivery deadline of affected plan lines
       priority = 'urgent' if required_by < today + 3 else 'normal'

    3. Create store_alert(target_team='purchase'):
       "INDENT #{indent_number}: Need {qty}kg of {material} by {date}"

    4. Create store_alert(target_team='stores'):
       "Check stock: {material} needed for production. {on_hand}kg on hand, {needed}kg needed."

WHEN PO IS RECEIVED (integration with existing PO module):
    5. Listen for po_box INSERT events
    6. Match received material against open purchase_indents
    7. Update indent.status = 'received'
    8. Update floor_inventory(rm_store) += received qty
    9. Re-check affected plan lines → create job cards if now sufficient
```

---

## 8. PRODUCTION ORDER LOGIC

```
FUNCTION: create_production_orders_from_plan(plan_id)

  plan_lines = approved plan lines WHERE status = 'pending'

  FOR each plan_line:
    1. Verify MRP shows sufficient material
    2. Look up BOM and process route

    3. Generate identifiers:
       prod_order_number = f"PO-{year}-{next_seq:04d}"
       batch_number = f"B{year}-{next_seq:03d}"

    4. Calculate:
       target_qty_units = plan_line.planned_units
       target_qty_kg = plan_line.planned_qty_kg
       batch_size_kg = target_qty_kg
       net_wt_per_unit = bom.pack_size_kg
       best_before = today + bom.shelf_life_days
       total_stages = COUNT(bom_process_route steps)

    5. INSERT production_order

    6. >>> TRIGGER: create_job_cards(prod_order_id)  [See Section 9]

  Update plan_line.status = 'scheduled'
```

---

## 9. JOB CARD ENGINE LOGIC

(Covered in detail in Step 3.5 above)

Key rules:
- **First job card = unlocked**, all subsequent = locked
- **Auto-unlock on previous completion** (Step 3.7.1)
- **Force unlock requires authority** (production_incharge or plant_head)
- **RM indent on first stage**, PM indent on packaging stage (configurable per BOM)
- **Each stage has its own QC checkpoints** from bom_process_route

---

## 10. QR CODE MATERIAL RECEIPT LOGIC

(Covered in detail in Step 3.6 above)

Key rules:
- QR code = po_box.box_id (already exists in PO module)
- Each box can only be consumed ONCE (idempotent check)
- Material type must match indent (cashew box can't satisfy dates indent)
- Auto-deducts from floor_inventory on scan
- Supports partial receipt (production can start with partial material)

---

## 11. JOB CARD EXECUTION & COMPLETION LOGIC

(Covered in detail in Step 3.7 above)

---

## 12. DAY-END DISPATCH & LOSS RECONCILIATION LOGIC

(Covered in detail in Step 3.8 above)

Key addition - **Loss Reconciliation Categories** (from Annexure D):

| # | Category | Description |
|---|----------|-------------|
| 1 | Sorting Rejection | Defective pieces removed during sorting |
| 2 | Roasting / Process Loss | Weight loss during roasting/processing (moisture, etc.) |
| 3 | Packaging Rejection | Seal failures, label errors, weight out-of-spec |
| 4 | Metal Detector Rejection | Units failing Fe/Nfe/SS detection |
| 5 | Spillage / Handling | Floor spillage, transfer losses |
| 6 | QC Sample Consumed | Samples taken for quality testing |

---

## 13. FLOOR-TO-FLOOR INVENTORY LOGIC

### 13.1 Valid Transitions (State Machine)

```
rm_store        → sorting          [RM issued for production via QR scan]
pm_store        → packaging        [PM issued for packaging via QR scan]
sorting         → grading          [Sorted material]
sorting         → offgrade_store   [Rejected at sorting]
grading         → processing       [Graded, ready for processing]
grading         → offgrade_store   [Off-grade at grading]
processing      → roasting         [If roasting required]
processing      → packaging        [Ready for packaging]
processing      → offgrade_store   [Off-grade at processing]
roasting        → packaging        [Roasted, ready to pack]
roasting        → offgrade_store   [Off-grade at roasting]
mixing          → packaging        [Mixed product ready]
packaging       → fg_warehouse     [Finished goods packed]
packaging       → offgrade_store   [Packaging rejects]
offgrade_store  → sorting          [Rework]
offgrade_store  → mixing           [Off-grade used in value-add: bars, trail mix]
fg_warehouse    → dispatch         [Dispatched to customer]
```

### 13.2 Movement Logic

```
FUNCTION: move_material(entity, sku, from_loc, to_loc, qty_kg, qty_units, job_card_id, reason)

  1. VALIDATE transition is allowed (check state machine above)
  2. VALIDATE from_loc has sufficient qty:
     current = SELECT quantity_kg FROM floor_inventory WHERE sku AND floor_location = from_loc
     IF current < qty_kg: RAISE "Insufficient stock at {from_loc}"

  3. BEGIN TRANSACTION:
     a. UPDATE floor_inventory SET quantity_kg -= qty_kg WHERE from_loc
     b. UPSERT floor_inventory SET quantity_kg += qty_kg WHERE to_loc
        (create record if first time material is at this location)
     c. INSERT floor_movement (full audit trail)
  4. COMMIT

  5. IF to_loc == 'offgrade_store':
     → Auto-create offgrade_inventory record (available for reuse)

  6. IF to_loc == 'fg_warehouse':
     → Update production_order.status if all stages complete
     → Update so_fulfillment.produced_qty_kg
```

---

## 14. OFF-GRADE REUSE LOGIC

### 14.1 Reuse Rules

```
Example rules:
  - Broken cashew → barline, trail_mix (max 30% substitution)
  - Undersized almond → almond_butter (max 100% substitution)
  - Discolored dates → date_syrup (max 50% substitution)
  - Mixed nuts off-grade → economy_mix_pack (max 100%)
```

### 14.2 AI-Powered Reuse Recommendation

```
FUNCTION: recommend_offgrade_reuse()
  1. Get available offgrade_inventory
  2. Get upcoming production orders whose BOMs accept off-grade
  3. Get reuse rules
  4. Send to Claude AI:
     "Match off-grade stock to production orders, maximize cost saving"
  5. Claude returns allocation recommendations
  6. Planner reviews and approves
  7. On approval:
     - Reserve offgrade_inventory (status = 'reserved')
     - Deduct from gross_qty in job card indent
```

---

## 15. PROCESS LOSS & ANOMALY DETECTION LOGIC

```
FUNCTION: auto_record_process_loss(job_card_id)
  TRIGGER: Job card completed

  1. Get job_card_output
  2. Calculate:
     input_qty = SUM(job_card_rm_indent.issued_qty)
     fg_output = job_card_output.fg_output_actual_kg
     offgrade_output = job_card_output.offgrade_output_kg
     waste = input_qty - fg_output - offgrade_output - material_return
     loss_pct = waste / input_qty * 100
     expected_loss = SUM(bom_process_route.loss_pct for this stage)
     variance = loss_pct - expected_loss

  3. INSERT process_loss record

  4. AI Anomaly Check:
     IF abs(variance) > 2 * stddev(historical_loss for this product+machine):
       - Set anomaly_flag = TRUE
       - Send to Claude: "Analyze this anomaly"
       - Claude returns root cause suggestion
       - Set anomaly_reason = Claude's suggestion
       - Create alert: "ANOMALY: {product} loss {loss_pct}% vs expected {expected}%"
```

---

## 16. QUALITY INSPECTION LOGIC

```
Types of inspection:
  1. incoming_rm: When RM arrives via PO
  2. in_process: At each QC checkpoint in process route
  3. final_fg: Before dispatch
  4. offgrade: To classify off-grade material

Inspection creates quality_inspection record.
IF overall_result = 'fail':
  - Job card cannot proceed past this step
  - Material diverted to offgrade_store
  - Alert raised to QC head
```

---

## 17. PLAN REVISION LOGIC (ADHOC ORDERS)

```
TRIGGER: Adhoc SO comes in mid-day, OR material arrives, OR QC failure

LOGIC:
  1. Production planner triggers "Revise Plan"
  2. System collects:
     - Current plan + status of all job cards
     - The change event (new SO, material arrival, etc.)
  3. Send to Claude:
     "Here is the current plan and what changed. Revise."
  4. Claude returns revised plan:
     - Which existing plan lines to reschedule
     - New plan lines for adhoc orders
     - Updated priorities
  5. Create new production_plan:
     - revision_number = old.revision_number + 1
     - previous_plan_id = old.plan_id
     - includes_adhoc = TRUE
  6. Old plan status = 'revised'
  7. New plan goes through same approval → job card creation flow
```

---

## 18. IDLE MATERIAL ALERT LOGIC

**Purpose:** If raw material or packaging material sits idle on any floor (production floor, RM store after issue, etc.) for 3-5 days without being consumed in a job card, alert the stores manager to investigate — material may be spoiling, misplaced, or from a cancelled order.

```
TRIGGER: Daily scheduled check (runs once per day, e.g., 06:00 AM)

CONFIG:
  IDLE_THRESHOLD_DAYS = 3       -- warn after 3 days
  CRITICAL_THRESHOLD_DAYS = 5   -- escalate after 5 days

LOGIC:
  1. Query floor_inventory WHERE last_updated < NOW() - IDLE_THRESHOLD_DAYS
     AND quantity_kg > 0
     AND floor_location IN ('production_floor', 'rm_store', 'pm_store')

  2. For each idle material found:
     a. Check if any active job card references this material:
        - Look up job_card_rm_indent / job_card_pm_indent
          WHERE material_sku_name = idle.sku_name
          AND job_card.status IN ('unlocked', 'assigned', 'material_received', 'in_progress')
        - IF active job card exists → SKIP (material is allocated, just not yet consumed)

     b. Calculate idle_days = NOW() - floor_inventory.last_updated

     c. IF idle_days >= CRITICAL_THRESHOLD_DAYS (5 days):
        - Create store_alert:
          alert_type = 'material_idle_critical'
          target_team = 'stores'
          message = "CRITICAL: {sku_name} ({qty} kg) idle on {floor_location} for {idle_days} days.
                     No active job card references this material. Investigate immediately."
          priority = 1

     d. ELSE IF idle_days >= IDLE_THRESHOLD_DAYS (3 days):
        - Create store_alert:
          alert_type = 'material_idle_warning'
          target_team = 'stores'
          message = "WARNING: {sku_name} ({qty} kg) on {floor_location} idle for {idle_days} days.
                     Consider returning to store or assigning to production."
          priority = 3

  3. Group alerts by floor_location for stores manager:
     - "Production Floor: 3 idle materials (total 450 kg)"
     - "RM Store: 1 idle material (200 kg, no pending job card)"

  4. De-duplicate: Do NOT re-alert for same material on same floor if alert already
     exists within last 24 hours (check store_alert.created_at)

RESOLUTION:
  When stores manager acts on alert:
  - Option A: Return material to store → floor_movement (prod_floor → rm_store)
  - Option B: Assign to upcoming job card → update job_card_rm_indent
  - Option C: Mark as off-grade → move to offgrade_inventory
  - Option D: Acknowledge as intentional hold → dismiss alert with reason
  Each action updates floor_inventory.last_updated, resetting the idle timer.
```

**Database impact:**
- Uses existing `floor_inventory.last_updated` column
- Uses existing `store_alert` table with new alert_types: `material_idle_warning`, `material_idle_critical`

---

## 19. DAY-END BALANCE SCAN & RECONCILIATION LOGIC

**Purpose:** At the end of every day, each floor must do a physical scan/count of all material present. If any floor does NOT submit their balance scan by a deadline, raise an alert. This catches discrepancies between system inventory and actual physical stock early.

```
TRIGGER: Daily at day-end deadline (e.g., 17:30 or configurable per floor)

CONFIG:
  DAY_END_DEADLINE = "17:30"    -- time by which scan must be completed
  GRACE_PERIOD_MIN = 30         -- 30 min grace before alert
  FLOORS_REQUIRING_SCAN = ['rm_store', 'pm_store', 'production_floor', 'fg_store']

--- PART A: BALANCE SCAN SUBMISSION ---

LOGIC (Floor Manager / Stores Team submits scan):
  1. Floor manager opens "Day-End Balance Scan" for their floor
  2. System shows: all items currently in floor_inventory for that floor
     - Pre-filled with system quantities as reference
  3. For each item on the floor:
     a. Scan QR codes of boxes physically present (for RM/PM)
        OR manually enter counted quantity (for WIP/FG)
     b. Record:
        - sku_name
        - system_qty_kg (from floor_inventory)
        - scanned_qty_kg (physical count)
        - variance_kg = scanned_qty - system_qty
        - variance_pct = variance_kg / system_qty * 100
  4. Submit balance scan → creates day_end_balance_scan record:
     - floor_location
     - scan_date = TODAY
     - submitted_by
     - submitted_at = NOW()
     - status = 'submitted'
     - total_system_qty, total_scanned_qty, total_variance
  5. Create day_end_balance_scan_line records for each item

--- PART B: VARIANCE HANDLING ---

  6. IF any item has |variance_pct| > 2%:
     - Flag item as 'variance_detected'
     - Create store_alert:
       alert_type = 'balance_variance'
       target_team = 'stores'
       message = "Balance variance on {floor}: {sku_name} system={X}kg actual={Y}kg
                  variance={Z}kg ({pct}%). Investigation required."

  7. IF variance is NEGATIVE (system > actual = material missing):
     - Potential causes: unrecorded consumption, spillage, theft
     - Require: reason field (mandatory) + corrective action
     - Auto-log to process_loss IF reason is spillage/handling

  8. IF variance is POSITIVE (actual > system = extra material):
     - Potential causes: unrecorded receipt, return not logged
     - Require: reason field to explain source

  9. After review + approval by stores manager:
     - Update floor_inventory to match physical count
     - Create floor_movement with reason = 'balance_adjustment'
     - Set day_end_balance_scan.status = 'reconciled'

--- PART C: MISSING SCAN ALERT ---

  10. System checks at DAY_END_DEADLINE + GRACE_PERIOD:
      FOR each floor in FLOORS_REQUIRING_SCAN:
        a. Check if day_end_balance_scan EXISTS
           WHERE floor_location = floor AND scan_date = TODAY
        b. IF NOT EXISTS:
           - Create store_alert:
             alert_type = 'balance_scan_missing'
             target_team = 'stores'
             message = "ALERT: Day-end balance scan NOT submitted for {floor}.
                        Deadline was {deadline}. Submit immediately."
             priority = 1
           - Also alert: production incharge (escalation)

  11. Track compliance:
      - day_end_balance_scan table tracks submission history
      - Dashboard shows: which floors submitted, which are pending, streak of compliance
```

**New tables needed:**
```sql
CREATE TABLE IF NOT EXISTS day_end_balance_scan (
    scan_id             SERIAL PRIMARY KEY,
    floor_location      TEXT NOT NULL,
    scan_date           DATE NOT NULL,
    submitted_by        TEXT,
    submitted_at        TIMESTAMPTZ,
    reviewed_by         TEXT,
    reviewed_at         TIMESTAMPTZ,
    total_system_qty    NUMERIC(15,3),
    total_scanned_qty   NUMERIC(15,3),
    total_variance      NUMERIC(15,3),
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending, submitted, variance_flagged, reconciled
    entity              TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (floor_location, scan_date, entity)
);

CREATE TABLE IF NOT EXISTS day_end_balance_scan_line (
    scan_line_id        SERIAL PRIMARY KEY,
    scan_id             INT NOT NULL REFERENCES day_end_balance_scan(scan_id),
    sku_name            TEXT NOT NULL,
    item_type           TEXT,
    system_qty_kg       NUMERIC(15,3),
    scanned_qty_kg      NUMERIC(15,3),
    variance_kg         NUMERIC(15,3),
    variance_pct        NUMERIC(5,3),
    scanned_box_ids     TEXT[],
    variance_reason     TEXT,
    corrective_action   TEXT,
    status              TEXT NOT NULL DEFAULT 'ok'  -- ok, variance_detected, reconciled
);
```

---

## 20. INTERNAL DISCREPANCY PLAN REVISION LOGIC

**Purpose:** Unlike adhoc order revisions (Section 17 — triggered by external demand changes), this handles INTERNAL production problems that require re-planning: wrong RM grade received, RM quality fails QC, machine breakdown, contamination, supplier sends wrong material, etc.

```
TRIGGER: Any of the following internal discrepancies:
  - RM grade mismatch (received Grade B, BOM requires Grade A)
  - RM fails incoming QC inspection (moisture, foreign matter, etc.)
  - RM/PM declared expired or damaged on floor
  - Machine breakdown affecting scheduled production
  - Contamination event requiring batch isolation
  - Supplier short-delivery (received 500 kg of 1000 kg ordered)

--- STEP 1: DISCREPANCY REPORTING ---

LOGIC:
  1. QC Inspector / Stores Team / Floor Manager reports discrepancy:
     - Create discrepancy_report:
       discrepancy_type = 'rm_grade_mismatch' | 'rm_qc_failure' | 'rm_expired' |
                          'machine_breakdown' | 'contamination' | 'short_delivery'
       affected_material = sku_name (if material issue)
       affected_machine = machine_id (if machine issue)
       affected_job_cards = [job_card_ids currently using this material/machine]
       severity = 'critical' | 'major' | 'minor'
       reported_by, reported_at
       details = free text description

  2. System auto-identifies impact:
     a. Find all active job_cards that reference the affected material/machine:
        - job_card WHERE status IN ('unlocked', 'assigned', 'material_received', 'in_progress')
          AND (material in rm_indent matches affected_material
               OR machine_id = affected_machine)
     b. Find all production_plan_lines not yet started that reference same
     c. Calculate: total_affected_qty_kg, number_of_affected_orders, customer_impact

  3. Create store_alert:
     alert_type = 'internal_discrepancy'
     target_team = 'production'
     message = "DISCREPANCY: {type}. {material/machine} affected.
                Impact: {N} job cards, {qty} kg, {customers} customers."
     priority = 1

--- STEP 2: AUTO-HOLD AFFECTED JOB CARDS ---

  4. For each affected job card:
     a. IF status = 'unlocked' or 'assigned' (not yet started):
        - Set status = 'locked'
        - Set is_locked = TRUE
        - Set locked_reason = 'discrepancy_hold'
        - Set locked_discrepancy_id = discrepancy_report.id

     b. IF status = 'in_progress':
        - Create store_alert to team leader:
          "STOP: Material/machine discrepancy reported. Await instructions."
        - Do NOT auto-lock (team leader decides to pause or complete current batch)

     c. IF status = 'material_received' (material on floor, not started):
        - Set status = 'locked'
        - locked_reason = 'discrepancy_hold'
        - IF material issue: flag the received material for return/quarantine

--- STEP 3: REVISION OPTIONS ---

  5. Production planner reviews discrepancy and chooses resolution:

     OPTION A — SUBSTITUTE MATERIAL:
       - Find alternative BOM that uses a different RM grade/source
       - OR find off-grade stock that meets minimum quality
       - Update job_card_rm_indent with substitute material
       - Re-run MRP for substitute availability
       - IF available: unlock affected job cards with new material
       - Log: discrepancy_resolution = 'material_substituted'

     OPTION B — RESCHEDULE TO DIFFERENT MACHINE:
       - Query machine_capacity for alternate machines with same capability
       - Update job_card.machine_id and production_plan_line.machine_id
       - Recalculate estimated_hours based on new machine capacity
       - Log: discrepancy_resolution = 'machine_rescheduled'

     OPTION C — DEFER PRODUCTION (wait for replacement material):
       - Keep job cards locked
       - Raise purchase_indent for replacement material
       - Set production_plan_line.status = 'deferred'
       - Update delivery_deadline impact on so_fulfillment
       - Alert customer-facing team if delivery will be delayed
       - Log: discrepancy_resolution = 'deferred'

     OPTION D — CANCEL & RE-PLAN:
       - Cancel affected job cards (status = 'cancelled')
       - Cancel production_order if all job cards cancelled
       - Trigger Claude AI plan revision:
         Send: current plan + discrepancy details + available alternatives
         Claude returns: revised plan excluding problematic material/machine
       - New plan goes through approval → job card creation flow
       - Log: discrepancy_resolution = 'cancelled_replanned'

     OPTION E — PROCEED WITH DEVIATION:
       - For minor discrepancies (e.g., Grade B instead of A, within tolerance)
       - Requires: Production Incharge sign-off + QC approval
       - Record deviation in job_card_remarks (Annexure E)
       - Flag output as 'deviation_batch' for enhanced QC
       - Log: discrepancy_resolution = 'proceed_with_deviation'

--- STEP 4: RESOLUTION & AUDIT ---

  6. Update discrepancy_report:
     - resolution_type = chosen option
     - resolution_details = what was done
     - resolved_by = planner name
     - resolved_at = NOW()
     - status = 'resolved'

  7. For all affected job cards that were held:
     - Unlock with reason = 'discrepancy_resolved'
     - OR keep cancelled if Option D

  8. Update so_fulfillment if delivery dates affected:
     - Create so_revision_log entry
     - Recalculate pending_qty_kg

  9. AI learning: Feed discrepancy + resolution to Claude for future planning:
     - "Last time Grade B almonds were received instead of Grade A,
        we substituted with off-grade stock at 15% mix ratio."
     - Claude uses this in future risk_flags and recommendations
```

**New table needed:**
```sql
CREATE TABLE IF NOT EXISTS discrepancy_report (
    discrepancy_id      SERIAL PRIMARY KEY,
    discrepancy_type    TEXT NOT NULL,         -- rm_grade_mismatch, rm_qc_failure, rm_expired,
                                               -- machine_breakdown, contamination, short_delivery
    severity            TEXT NOT NULL DEFAULT 'major',  -- critical, major, minor
    affected_material   TEXT,
    affected_machine_id INT REFERENCES machine(machine_id),
    affected_job_card_ids INT[],
    affected_plan_line_ids INT[],
    details             TEXT,
    total_affected_qty_kg NUMERIC(15,3),
    customer_impact     TEXT,
    resolution_type     TEXT,                  -- material_substituted, machine_rescheduled,
                                               -- deferred, cancelled_replanned, proceed_with_deviation
    resolution_details  TEXT,
    reported_by         TEXT,
    reported_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_by         TEXT,
    resolved_at         TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'open',  -- open, investigating, resolved, closed
    entity              TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discrepancy_status ON discrepancy_report(status);
CREATE INDEX IF NOT EXISTS idx_discrepancy_entity ON discrepancy_report(entity);
```

---

## 21. FRONTEND LOGIC & SCREENS

### 18.1 Screen List

| # | Screen | User | Purpose |
|---|--------|------|---------|
| 1 | Production Dashboard | Planner | Overview: today's plan, job card status, alerts |
| 2 | Plan Generation | Planner | Trigger Claude, review AI plan, approve |
| 3 | Plan Revision | Planner | Handle adhoc orders, re-plan |
| 4 | BOM Management | Planner | View/edit BOMs, process routes |
| 5 | Machine Management | Planner | View machines, capacity matrix |
| 6 | Job Card List | Team Leader / Floor Manager | Filter by status, floor, date |
| 7 | Job Card Detail | Team Leader | Full job card with all sections |
| 8 | Job Card Execution | Team Leader | Start, record steps, complete |
| 9 | QR Material Scan | Floor Manager | Camera QR scan interface |
| 10 | Day-End Dashboard | Floor Manager | Dispatch update, loss entry |
| 11 | Floor Inventory | Stores | Current stock at each floor |
| 12 | Indent Dashboard | Purchase Team | Pending indents, status |
| 13 | Alerts Panel | All | Notifications and alerts |
| 14 | FY Review Dashboard | Planner | FY close order review |
| 15 | Off-Grade Dashboard | Planner | Off-grade stock, reuse recommendations |
| 16 | Loss Analytics | Planner / QC | Loss trends, anomalies, charts |
| 17 | Fulfillment Tracker | Sales / Planner | SO fulfillment status, running orders |

### 18.2 Key Frontend Flows

**Plan Generation Flow:**
```
Dashboard → "Generate Plan" button → Loading (Claude processing ~10-20s)
→ Plan Preview screen (table of production lines, material check, risk flags)
→ Planner can edit priorities/quantities
→ "Approve" button → Plan approved → "Create Job Cards" button
→ Job cards created and assigned
```

**Job Card Execution Flow (Team Leader mobile):**
```
"My Job Cards" list → Select unlocked job card → Job Card Detail
→ "Receive Material" → QR Scanner opens → Scan boxes → Material received
→ "Start Production" → Timer starts
→ Complete each process step (operator sign, QC sign)
→ "Record Output" → Enter FG units, kg, rejection, loss
→ "Complete" → Next stage auto-unlocks
```

**Day-End Flow (Floor Manager):**
```
"Day-End Dashboard" → Shows all completed job cards today
→ Enter dispatch qty for each → "Submit Day-End"
→ Sign-offs: Production Incharge → QC → Warehouse
→ Job cards closed
```

---

## 22. DATA RELATIONSHIPS & ENTITY MAP

```
so_header (existing)
  └── so_line (existing)
       └── so_fulfillment (NEW)
            └── so_revision_log (NEW)

production_plan (NEW)
  └── production_plan_line (NEW)
       └── production_order (NEW)
            └── job_card (NEW) [1 per stage, sequential]
                 ├── job_card_rm_indent (NEW)
                 ├── job_card_pm_indent (NEW)
                 ├── job_card_process_step (NEW)
                 ├── job_card_output (NEW)
                 ├── job_card_environment (NEW)
                 ├── job_card_metal_detection (NEW)
                 ├── job_card_weight_check (NEW)
                 ├── job_card_loss_reconciliation (NEW)
                 └── job_card_remarks (NEW)

bom_header (NEW)
  ├── bom_line (NEW)
  └── bom_process_route (NEW)

machine (NEW)
  └── machine_capacity (NEW)

floor_inventory (NEW) ←→ floor_movement (NEW)
offgrade_inventory (NEW) ←→ offgrade_reuse_rule (NEW) ←→ offgrade_consumption (NEW)
process_loss (NEW)
quality_inspection (NEW)
yield_summary (NEW)
purchase_indent (NEW) → store_alert (NEW)
ai_recommendation (NEW)

TOTAL: 23 new tables + integration with 5 existing tables
```

---

## 23. ERROR HANDLING & EDGE CASES

| Scenario | Handling |
|----------|----------|
| BOM not found for an SO product | Flag in plan, alert planner to create BOM |
| Machine in maintenance mid-production | Reschedule to alternate machine, raise alert |
| QR code scanned doesn't match indent material | Reject scan, show error: "Expected {X}, scanned {Y}" |
| Job card force-unlocked but previous stage output is zero | Warning: "No SFG available. Production may produce defective output." |
| Partial material receipt | Allow start with warning, record actual issued vs required |
| Claude AI timeout/error | Fallback: planner creates plan manually, log AI failure |
| Power outage during job card execution | Job card stays in 'in_progress', resume from last step |
| Off-grade material expired | Auto-set offgrade_inventory.status = 'expired', exclude from reuse |
| Same box QR scanned twice | Idempotent: reject with "Box already consumed in JC-{number}" |
| Customer revises order after job card created | Cancel remaining job cards, revise production order, re-plan |
| Loss exceeds 3x expected | AI anomaly alert + mandatory investigation before next batch |
