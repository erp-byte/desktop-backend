# Production Planning Module — UI Design Reference

**Date:** 2026-03-26
**Product:** Candor Foods Production Planning System (web app, desktop-first, responsive for tablet on factory floor)
**Users:** Production Planner, Team Leader, Floor Manager, QC Inspector, Stores Team, Purchase Team
**Style:** Clean enterprise dashboard. Dark sidebar navigation. White content area. Status badges with color coding. Data-dense tables with inline actions.

---

## System Actors & Responsibilities

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │                    PRODUCTION PLANNING SYSTEM                       │
  │                    Candor Foods (CFPL & CDPL)                       │
  └─────────────────────────────────────────────────────────────────────┘

  WHO DOES WHAT:

  ┌─────────────┐  ┌───────────────┐  ┌──────────────┐  ┌────────────┐
  │ SALES TEAM   │  │ PROD PLANNER  │  │ PURCHASE TEAM│  │ STORES TEAM│
  │              │  │               │  │              │  │            │
  │ Punch SOs    │  │ Generate plan │  │ Receive      │  │ QR scan    │
  │ daily via    │  │ (Claude AI)   │  │ indents      │  │ material   │
  │ SO module    │  │ Approve plan  │  │ Create POs   │  │ into store │
  │              │  │ Handle adhoc  │  │ Track procure│  │ Issue to   │
  │              │  │               │  │              │  │ production │
  └──────┬──────┘  └──────┬────────┘  └──────┬───────┘  └─────┬──────┘
         │                │                   │                │
         ▼                ▼                   ▼                ▼
  ┌─────────────┐  ┌───────────────┐  ┌──────────────┐  ┌────────────┐
  │ TEAM LEADER  │  │ FLOOR MANAGER │  │ QC INSPECTOR │  │ PROD       │
  │              │  │               │  │              │  │ INCHARGE   │
  │ Execute job  │  │ Oversee floor │  │ Inspect at   │  │            │
  │ cards        │  │ QR scan boxes │  │ each stage   │  │ Approve    │
  │ Start prod   │  │ Day-end       │  │ Pass/fail    │  │ plans      │
  │ Record output│  │ dispatch      │  │ Metal detect │  │ Sign-off   │
  │              │  │ Record losses │  │ Weight check │  │ Force-     │
  │              │  │               │  │              │  │ unlock     │
  └──────────────┘  └───────────────┘  └──────────────┘  └────────────┘
```

---

## End-to-End Process Flow

```
 SALES TEAM              PLANNER                 SYSTEM                  STORES/FLOOR
 ──────────              ───────                 ──────                  ────────────
     │                      │                       │                        │
     │  Punch SOs           │                       │                        │
     │──────────────────────┼───────>  so_header     │                        │
     │                      │         so_line        │                        │
     │                      │            │           │                        │
     │                      │    ┌───────▼────────┐  │                        │
     │                      │    │ so_fulfillment  │  │                        │
     │                      │    │ (auto-sync)     │  │                        │
     │                      │    └───────┬────────┘  │                        │
     │                      │            │           │                        │
     │                 Click│"Generate   │           │                        │
     │                 Plan"│            │           │                        │
     │                      ▼            ▼           │                        │
     │               ┌──────────────────────────┐    │                        │
     │               │     CLAUDE AI ENGINE      │    │                        │
     │               │                          │    │                        │
     │               │  Inputs:                 │    │                        │
     │               │  - Pending demand        │    │                        │
     │               │  - Floor inventory       │    │                        │
     │               │  - Machine capacity      │    │                        │
     │               │  - BOM recipes           │    │                        │
     │               │  - Historical loss %     │    │                        │
     │               │  - Off-grade stock       │    │                        │
     │               │                          │    │                        │
     │               │  Output:                 │    │                        │
     │               │  - Daily schedule        │    │                        │
     │               │  - Material check        │    │                        │
     │               │  - Risk flags            │    │                        │
     │               └────────────┬─────────────┘    │                        │
     │                            │                  │                        │
     │                            ▼                  │                        │
     │               ┌──────────────────────┐        │                        │
     │               │  production_plan      │        │                        │
     │               │  (status: draft)      │        │                        │
     │               │       │               │        │                        │
     │               │  production_plan_line  │        │                        │
     │               └──────────┬───────────┘        │                        │
     │                          │                    │                        │
     │                 Approve  │                    │                        │
     │                          ▼                    │                        │
     │               ┌──────────────────────┐        │                        │
     │               │   MRP CHECK           │        │                        │
     │               │                      │        │                        │
     │               │ For each plan line:   │        │                        │
     │               │ BOM → gross_req       │        │                        │
     │               │ + loss allowance      │        │                        │
     │               │ - off-grade reuse     │        │                        │
     │               │ vs floor_inventory    │        │                        │
     │               │ + pending POs         │        │                        │
     │               └──────┬──────┬────────┘        │                        │
     │                      │      │                 │                        │
     │              Sufficient  Shortage             │                        │
     │                      │      │                 │                        │
     │                      │      ▼                 │                        │
     │                      │  ┌────────────────┐    │                        │
     │                      │  │purchase_indent  │    │    ◄── Purchase Team
     │                      │  │store_alert      │    │        procures RM/PM
     │                      │  └────────────────┘    │                        │
     │                      │                        │                        │
     │                      ▼                        │                        │
     │               ┌──────────────────────┐        │                        │
     │               │  production_order     │        │                        │
     │               │  (batch_number,       │        │                        │
     │               │   batch_size_kg)      │        │                        │
     │               └──────────┬───────────┘        │                        │
     │                          │                    │                        │
     │                          ▼                    │                        │
     │    ┌─────────────────────────────────────────────────────────┐         │
     │    │              JOB CARD ENGINE (Sequential)                │         │
     │    │                                                         │         │
     │    │  ┌──────────┐    ┌──────────┐    ┌──────────┐          │         │
     │    │  │ JC #1    │    │ JC #2    │    │ JC #3    │          │         │
     │    │  │ SORTING  │───>│ WEIGHING │───>│ SEALING  │──> ...   │         │
     │    │  │          │    │          │    │          │          │         │
     │    │  │ UNLOCKED │    │ LOCKED   │    │ LOCKED   │          │         │
     │    │  └────┬─────┘    └──────────┘    └──────────┘          │         │
     │    │       │                                                 │         │
     │    └───────┼─────────────────────────────────────────────────┘         │
     │            │                                                           │
     │            ▼                                                           │
     │    ┌───────────────────────────────────────────────────────────────┐   │
     │    │                   JOB CARD LIFECYCLE                          │   │
     │    │                                                               │   │
     │    │  LOCKED ──> UNLOCKED ──> ASSIGNED ──> MATERIAL_RECEIVED      │   │
     │    │                                            │                  │   │
     │    │                                     QR Scan Boxes ◄──────────┼───┤
     │    │                                     (po_box.box_id)          │   │
     │    │                                            │                  │   │
     │    │                                     IN_PROGRESS               │   │
     │    │                                            │                  │   │
     │    │                                     Process Steps             │   │
     │    │                                     QC Checkpoints            │   │
     │    │                                     Metal Detection           │   │
     │    │                                     Weight Checks             │   │
     │    │                                            │                  │   │
     │    │                                     COMPLETED                 │   │
     │    │                                     ├─ Record output          │   │
     │    │                                     ├─ Record loss            │   │
     │    │                                     ├─ Off-grade captured     │   │
     │    │                                     ├─ Sign-offs              │   │
     │    │                                     └─ UNLOCK NEXT JC ──────>│   │
     │    │                                                               │   │
     │    │                                     CLOSED (all stages done)  │   │
     │    │                                     └─ so_fulfillment updated │   │
     │    └───────────────────────────────────────────────────────────────┘   │
     │                                                                        │
     │                         DAY END                                        │
     │                         ├─ Dispatch qty recorded                       │
     │                         ├─ Loss reconciliation (Annexure D)            │
     │                         ├─ Floor inventory adjusted                    │
     │                         └─ Yield summary computed                      │
```

---

## Entity Relationship Diagram

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                          EXISTING MODULES                               │
 │                                                                         │
 │  ┌─────────────┐     ┌─────────────┐     ┌──────────────┐             │
 │  │  so_header   │────>│  so_line     │     │  po_header    │             │
 │  │  (SO module) │     │  (so_line_id)│     │  (PO module)  │             │
 │  └─────────────┘     └──────┬──────┘     └──────┬───────┘             │
 │                              │                    │                     │
 │                       ┌──────┘              ┌─────┘                    │
 │  ┌──────────┐         │                     │                          │
 │  │ all_sku  │    FK   │                FK   │                          │
 │  │ (master) │◄───────┐│                     │                          │
 │  └──────────┘        ││                     │                          │
 └──────────────────────┼┼─────────────────────┼──────────────────────────┘
                        ││                     │
 ═══════════════════════╪╪═════════════════════╪══════════════════════════
                        ││                     │
 ┌──────────────────────┼┼─────────────────────┼──────────────────────────┐
 │  PRODUCTION MODULE   ││                     │                          │
 │                      ││                     │                          │
 │  ── MASTER DATA ──   ││                     │                          │
 │                      ▼│                     │                          │
 │  ┌──────────────┐    ┌┼─────────────────┐   │                          │
 │  │  bom_header   │    │  so_fulfillment  │◄──┘  (links to po_box       │
 │  │  (recipe)     │    │  (demand track)  │       via QR scan)          │
 │  │               │    └────────┬────┬───┘                              │
 │  ├──────────────┤             │    │                                   │
 │  │  bom_line     │             │    └──> so_revision_log               │
 │  │  (materials)  │             │                                       │
 │  ├──────────────┤             │                                       │
 │  │  bom_process  │             │                                       │
 │  │  _route       │             │                                       │
 │  │  (steps)      │             │                                       │
 │  └──────┬───────┘             │                                       │
 │         │                      │                                       │
 │  ┌──────┴───────┐              │                                       │
 │  │   machine     │              │                                       │
 │  ├──────────────┤              │                                       │
 │  │   machine_    │              │                                       │
 │  │   capacity    │              │                                       │
 │  └──────────────┘              │                                       │
 │                                 │                                       │
 │  ── PLANNING ──                 │                                       │
 │                                 │                                       │
 │  ┌──────────────────┐          │                                       │
 │  │ production_plan   │          │                                       │
 │  │ (daily/weekly)    │          │                                       │
 │  │                   │          │                                       │
 │  │  production_plan  │          │                                       │
 │  │  _line ───────────┼──────────┘ (linked_so_fulfillment_ids)          │
 │  └────────┬─────────┘                                                  │
 │           │                                                             │
 │  ── EXECUTION ──                                                       │
 │           │                                                             │
 │           ▼                                                             │
 │  ┌──────────────────┐                                                  │
 │  │ production_order  │                                                  │
 │  │ (batch)           │                                                  │
 │  └────────┬─────────┘                                                  │
 │           │ 1:N (one per stage)                                        │
 │           ▼                                                             │
 │  ┌──────────────────┐     ┌───────────────────────────────────┐        │
 │  │    job_card       │────>│  CHILD TABLES (all FK job_card_id) │        │
 │  │                   │     │                                   │        │
 │  │  step 1: SORTING  │     │  job_card_rm_indent    (materials) │        │
 │  │  step 2: WEIGHING │     │  job_card_pm_indent    (packaging) │        │
 │  │  step 3: SEALING  │     │  job_card_process_step (steps)     │        │
 │  │  step 4: METAL DET│     │  job_card_output       (results)   │        │
 │  │                   │     │  job_card_environment  (Annex C)   │        │
 │  │  Sequential lock: │     │  job_card_metal_detect (Annex A/B) │        │
 │  │  JC1=unlocked     │     │  job_card_weight_check (Annex B)   │        │
 │  │  JC2=locked       │     │  job_card_loss_recon   (Annex D)   │        │
 │  │  JC3=locked       │     │  job_card_remarks      (Annex E)   │        │
 │  └──────────────────┘     └───────────────────────────────────┘        │
 │                                                                         │
 │  ── INVENTORY ──                          ── ANALYTICS ──              │
 │                                                                         │
 │  ┌──────────────────┐  ┌───────────────┐  ┌─────────────────┐         │
 │  │ floor_inventory   │  │ offgrade_     │  │ process_loss     │         │
 │  │ (stock per floor) │  │ inventory     │  │ quality_inspect  │         │
 │  ├──────────────────┤  ├───────────────┤  │ yield_summary    │         │
 │  │ floor_movement    │  │ offgrade_     │  └─────────────────┘         │
 │  │ (audit trail)     │  │ reuse_rule    │                               │
 │  └──────────────────┘  ├───────────────┤  ┌─────────────────┐         │
 │                         │ offgrade_     │  │ purchase_indent  │         │
 │                         │ consumption   │  │ store_alert      │         │
 │                         └───────────────┘  │ ai_recommendation│         │
 │                                             └─────────────────┘         │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## Job Card Locking & Stage Flow

```
  Production Order: PO-2026-0042
  Product: Al Barakah Dates Arabian 500g
  Batch: B2026-042 | 1,250 kg
  BOM Process Route: 4 stages

  ┌───────────────────────────────────────────────────────────────────┐
  │                                                                   │
  │  JC PO-2026-0042/1        JC PO-2026-0042/2                     │
  │  ┌─────────────────┐      ┌─────────────────┐                   │
  │  │   SORTING        │      │   WEIGHING       │                   │
  │  │                  │      │                  │                   │
  │  │  Machine: Sort L1│      │  Machine: Scale 3│                   │
  │  │  Cap: 250 kg/hr  │      │  Cap: 200 kg/hr  │                   │
  │  │  Est: 5.1 hrs    │      │  Est: 6.3 hrs    │                   │
  │  │  Loss: 2.0%      │      │  Loss: 0.5%      │                   │
  │  │                  │      │                  │                   │
  │  │  Status: UNLOCKED│ ───> │  Status: LOCKED  │                   │
  │  │  RM indent: YES  │ done │  RM indent: NO   │                   │
  │  │  QC: visual+FM   │      │  QC: net wt ±2g  │                   │
  │  └─────────────────┘      └─────────────────┘                   │
  │                                    │                              │
  │                                    │ done                         │
  │                                    ▼                              │
  │  JC PO-2026-0042/3        JC PO-2026-0042/4                     │
  │  ┌─────────────────┐      ┌─────────────────┐                   │
  │  │   SEALING        │      │  METAL DETECTION │                   │
  │  │                  │      │                  │                   │
  │  │  Machine: Seal 2 │      │  Machine: MD-01  │                   │
  │  │  Cap: 300 kg/hr  │      │  Cap: 400 kg/hr  │                   │
  │  │  Est: 4.2 hrs    │      │  Est: 3.1 hrs    │                   │
  │  │  Loss: 0.2%      │      │  Loss: 0.1%      │                   │
  │  │                  │      │                  │                   │
  │  │  Status: LOCKED  │ ───> │  Status: LOCKED  │                   │
  │  │  PM indent: YES  │ done │  QC: Fe/Nfe/SS   │                   │
  │  │  QC: seal check  │      │  FINAL STAGE     │                   │
  │  └─────────────────┘      └─────────────────┘                   │
  │                                    │                              │
  │                                    │ done                         │
  │                                    ▼                              │
  │                           ┌─────────────────┐                    │
  │                           │  ORDER COMPLETE   │                    │
  │                           │  FG → fg_store    │                    │
  │                           │  fulfillment ++   │                    │
  │                           └─────────────────┘                    │
  └───────────────────────────────────────────────────────────────────┘
```

---

## Floor Inventory State Machine

```
  ┌─────────────┐                              ┌─────────────┐
  │  PO Receipt  │   QR Scan into stores        │  Vendor /    │
  │  (po_box)    │─────────────────────────────>│  RM Store    │
  └─────────────┘                              │  (rm_store)  │
                                                └──────┬──────┘
                                                       │
                              QR Scan for Job Card     │
                              (debit rm_store,         │
                               credit prod floor)      │
                                                       ▼
  ┌─────────────┐                              ┌─────────────┐
  │  PM Store    │   Issue PM for packaging     │  Production  │
  │  (pm_store)  │─────────────────────────────>│  Floor       │
  └─────────────┘                              │  (prod_floor)│
                                                └──────┬──────┘
                                                       │
                                        ┌──────────────┼──────────────┐
                                        │              │              │
                                        ▼              ▼              ▼
                                 ┌────────────┐ ┌────────────┐ ┌────────────┐
                                 │ FG Store    │ │ Off-Grade  │ │ Process    │
                                 │ (fg_store)  │ │ Inventory  │ │ Loss       │
                                 │             │ │ (reusable) │ │ (waste)    │
                                 └──────┬─────┘ └────────────┘ └────────────┘
                                        │
                                        ▼
                                 ┌────────────┐
                                 │  Dispatch   │
                                 │  to Customer│
                                 └────────────┘

  Allowed transitions:
    rm_store ──> production_floor      (job card material receipt)
    pm_store ──> production_floor      (packaging stage)
    production_floor ──> fg_store      (FG output)
    production_floor ──> offgrade      (off-grade captured)
    production_floor ──> rm_store      (material return, unused)
    offgrade ──> production_floor      (off-grade reuse in another batch)
```

---

## Screen Wireframes

---

### Screen 1: Production Dashboard (`/production`)

**Purpose:** Landing page — at-a-glance view of today's production status.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  SIDEBAR          │  HEADER: "Production Dashboard"    [Generate Plan ▼] │
│                   │──────────────────────────────────────────────────────│
│  Dashboard        │                                                      │
│  Plans         >  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │
│  Job Cards     >  │  │ Today's   │ │ Total    │ │ Total    │ │ Alerts │ │
│  BOM           >  │  │ Orders: 12│ │ FG: 8.5T │ │ Job Cards│ │ 3 new  │ │
│  Machines      >  │  │ Plan: Appr│ │ Target   │ │ 34 total │ │ ⚠ 2    │ │
│  Floor Inventory  │  └──────────┘ └──────────┘ └──────────┘ └────────┘ │
│  Indents       >  │                                                      │
│  Fulfillment   >  │  ┌─────────────────────────────┐ ┌────────────────┐ │
│  Off-Grade     >  │  │  JOB CARD STATUS             │ │ ACTIVE ALERTS  │ │
│  Loss Analytics>  │  │                               │ │                │ │
│  Floor Alerts  >  │  │  [Pie Chart]                 │ │ ⚠ Almond      │ │
│  AI Insights   >  │  │  ● Locked: 8 (grey)          │ │   shortage     │ │
│  Order Intel   >  │  │  ● Unlocked: 4 (blue)        │ │   400 kg       │ │
│  FY Review     >  │  │                               │ │                │ │
│                   │  │  ● In Progress: 6 (yellow)   │ │                │ │
│                   │  │  ● Completed: 12 (green)     │ │ ⚠ Force unlock │ │
│                   │  │  ● Closed: 4 (dark green)    │ │   JC-0042/2    │ │
│                   │  │                               │ │                │ │
│                   │  └─────────────────────────────┘ └────────────────┘ │
│                   │                                                      │
│                   │  ┌──────────────────────────────────────────────────┐│
│                   │  │  TODAY'S PRODUCTION LINES (from approved plan)   ││
│                   │  │                                                  ││
│                   │  │  Product          Customer  Qty(kg)  Machine  St ││
│                   │  │  ─────────────────────────────────────────────── ││
│                   │  │  Dates Arabian    D-Mart    1,250    Sort L1  🟢 ││
│                   │  │  Cashew W320      Export    800      Sort L2  🟡 ││
│                   │  │  Almond Cal 1kg   Retail    500      Pack M1  ⬜ ││
│                   │  │  Chia Seeds 250g  Amazon    2,000    Sort L1  ⬜ ││
│                   │  └──────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────┘
```

**Status colors:** ⬜ Locked (grey) | 🔵 Unlocked (blue) | 🟡 In Progress (yellow) | 🟢 Completed (green)

---

### Screen 2: Plan Generation (`/production/plans/generate`)

**Purpose:** Trigger Claude AI to generate a daily/weekly plan. Review, edit, approve.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: "Generate Production Plan"                                      │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  Plan Type: [Daily ▼]   Entity: [CFPL ▼]   Date: [2026-04-01]          │
│                                                                          │
│  [ 🤖 Generate Plan with Claude AI ]    (loading spinner ~15s)          │
│                                                                          │
│  ═══════════════════════════════════════════════════════════════════════ │
│                                                                          │
│  PLAN: Daily Plan — 2026-04-01          Status: DRAFT                   │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │  PRODUCTION SCHEDULE                                    [Approve] │ │
│  │                                                                    │ │
│  │  #  Product            Customer  Qty(kg) Units  Machine   P  Shift│ │
│  │  ── ─────────────────  ────────  ─────── ─────  ────────  ─  ─────│ │
│  │  1  Dates Arabian 500g D-Mart    1,250   2,500  Sort L1   1  Day  │ │
│  │  2  Cashew W320 250g   Export    800     3,200  Sort L2   2  Day  │ │
│  │  3  Almond Cal 1kg     Retail    500     500    Pack M1   3  Day  │ │
│  │  🟡 = Carryforward     🟠 = Adhoc     ⬜ = Normal                  │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ FLOOR & UNIT LEVEL DISTRIBUTION                        [Collapse] │ │
│  │                                                                    │ │
│  │ ▼ FLOOR 1 — Production Hall A           Capacity: 85%  Load: 🟡  │ │
│  │ ┌──────────────────────────────────────────────────────────────┐   │ │
│  │ │ Unit    Machine    Product            Qty(kg)  Shift  Status│   │ │
│  │ │ ──────  ─────────  ─────────────────  ───────  ─────  ──────│   │ │
│  │ │ U1-A    Sort L1    Dates Arabian 500g 1,250    Day    Sched │   │ │
│  │ │ U1-B    Scale 3    Dates Arabian 500g 1,250    Day    Sched │   │ │
│  │ │ U1-C    Seal 2     (available)        —        —      Free  │   │ │
│  │ │ Floor total: 2,500 kg | 2 products | 1 shift                │   │ │
│  │ └──────────────────────────────────────────────────────────────┘   │ │
│  │                                                                    │ │
│  │ ▼ FLOOR 2 — Production Hall B           Capacity: 60%  Load: 🟢  │ │
│  │ ┌──────────────────────────────────────────────────────────────┐   │ │
│  │ │ Unit    Machine    Product            Qty(kg)  Shift  Status│   │ │
│  │ │ ──────  ─────────  ─────────────────  ───────  ─────  ──────│   │ │
│  │ │ U2-A    Sort L2    Cashew W320 250g   800      Day    Sched │   │ │
│  │ │ U2-B    Pack M1    Almond Cal 1kg     500      Day    Sched │   │ │
│  │ │ U2-C    MD-01      (available)        —        —      Free  │   │ │
│  │ │ U2-D    Roast R1   (available)        —        —      Free  │   │ │
│  │ │ Floor total: 1,300 kg | 2 products | 1 shift                │   │ │
│  │ └──────────────────────────────────────────────────────────────┘   │ │
│  │                                                                    │ │
│  │ ▶ FLOOR 3 — Packaging Wing              Capacity: 0%   Load: ⬜  │ │
│  │                                                                    │ │
│  │ SUMMARY:                                                           │ │
│  │ Total floors active: 2/3 | Total units occupied: 4/10            │ │
│  │ Total planned: 3,800 kg  | Avg floor utilization: 48%            │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌──────────────────────────┐  ┌───────────────────────────────────────┐│
│  │ MATERIAL CHECK            │  │ RISK FLAGS                           ││
│  │                          │  │                                       ││
│  │ 🟢 Dates Raw    2,000 kg │  │ ⚠ Almond shortage: 400 kg gap       ││
│  │ 🟢 Cashew W320  1,200 kg │  │ ⚠ Sort L1 at 95% capacity          ││
│  │ 🔴 Almond Cal    300 kg  │  │ ℹ D-Mart deadline: Apr 3            ││
│  │    (need 700, gap: 400)  │  │                                       ││
│  │ 🟢 All PM stock OK       │  │                                       ││
│  └──────────────────────────┘  └───────────────────────────────────────┘│
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ CLAUDE'S REASONING                                                 │ │
│  │                                                                    │ │
│  │ "Prioritized D-Mart dates order (deadline Apr 3) on Sort L1 due   │ │
│  │  to higher throughput for dates. Cashew assigned to Sort L2 to    │ │
│  │  avoid changeover. Almond deferred until material arrives — indent │ │
│  │  recommended for 400 kg."                                          │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 3: Job Card Detail (`/production/job-cards/{id}`)

**Purpose:** Full job card view — matches physical CFC/PRD/JC/V3.0 PDF format. Sections 1-6 + Annexures as tabs.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: Job Card PO-2026-0042/1         Stage 1 of 4: SORTING          │
│  Status: [🟡 IN PROGRESS]    Lock: [🔓 Unlocked]                       │
│──────────────────────────────────────────────────────────────────────────│
│  TABS: [Main] [Annexure A/B] [Annexure C] [Annexure D] [Annexure E]    │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  SECTION 1 — Product Details                                            │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Prod Order: PO-2026-0042    Batch: B2026-042    Entity: CFPL      │ │
│  │ Product: Al Barakah Dates Arabian 500g                             │ │
│  │ Customer: D-Mart            Pack Size: 0.5 kg   Batch Size: 1250kg│ │
│  │ Net Wt/Unit: 500g           Best Before: 2027-04-01               │ │
│  │ Factory: W-202              Floor: Production 1                    │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  SECTION 2A — RM Indent                                                 │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Material         UOM    Reqd    Loss%  Gross   Issued  Batch  Var │ │
│  │ ────────────────────────────────────────────────────────────────── │ │
│  │ Fard Dates Std   kg     1,250   2.0%   1,275   1,280   LOT-A  +5 │ │
│  │                                                                    │ │
│  │ [ 📷 Receive Material — QR Scan ]                                 │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  SECTION 2B — PM Indent                                                 │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Material              UOM   Reqd   Loss%  Gross  Issued  Var      │ │
│  │ ────────────────────────────────────────────────────────────────── │ │
│  │ Arabian 500g Pouch    pcs   2,500  1.0%   2,525  0       -2,525  │ │
│  │ Carton 12x500g        pcs   209    0.5%   210    0       -210    │ │
│  │ Sticker Label 500g    pcs   2,500  1.0%   2,525  0       -2,525  │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  SECTION 3 — Team & Process                                             │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Team Leader: Ramesh Kumar    Members: Sunil, Arun, Priya          │ │
│  │ Start: 2026-04-01 09:15     End: —          Duration: 2h 30m      │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  SECTION 4 — Process Steps                                              │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ # Process    Machine   Std Time  QC Check        Loss%  Op   QC   │ │
│  │ ─────────────────────────────────────────────────────────────────  │ │
│  │ 1 Sorting    Sort L1   60 min    Visual+FM       2.0%   ✅   ✅   │ │
│  │ 2 Grading    Sort L1   45 min    Grade accuracy  1.0%   ⬜   ⬜   │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  SECTION 5 — Output                                                     │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │              Expected    Actual    Variance   Var%    Reason       │ │
│  │ FG (units)   2,500       —         —          —                    │ │
│  │ FG (kg)      1,250       —         —          —                    │ │
│  │ RM consumed  1,275.5     —         —          —                    │ │
│  │ Mat return   0           —                                         │ │
│  │ Rejection    0           —                    —       [Select ▼]   │ │
│  │ Off-grade    0           —                    —       [Category ▼] │ │
│  │ Process loss 25.5        —                    —                    │ │
│  │                                                                    │ │
│  │ [ Record Output ]                                                  │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  SECTION 5A — Balance Material (per JC entry level)                     │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Material         Issued   Consumed  Returned  Balance  Status     │ │
│  │ ────────────────────────────────────────────────────────────────── │ │
│  │ Fard Dates Std   1,280    1,252     0         28.0 kg  ⚠ Excess  │ │
│  │ Arabian Pouch    2,525    2,480     20        25 pcs   ⚠ Excess  │ │
│  │ Carton 12x500g   210      207       0         3 pcs    🟢 Normal │ │
│  │ Sticker Label    2,525    2,480     0         45 pcs   ⚠ Excess  │ │
│  │                                                                    │ │
│  │ Balance action: [Return to Store] [Carry to Next JC] [Write Off]  │ │
│  │                                                                    │ │
│  │ ⓘ Balance = Issued − Consumed − Returned                         │ │
│  │   Excess balance auto-flagged if > 2% of issued qty              │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  SECTION 5B — Off-Grade Segregation                                     │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Category          Qty(kg) Grade    Reusable  Destination   Action │ │
│  │ ────────────────────────────────────────────────────────────────── │ │
│  │ Broken pieces     8.5     B-Grade  ✅ Yes    Powder line   [Map]  │ │
│  │ Undersized        4.2     C-Grade  ✅ Yes    Repack 250g   [Map]  │ │
│  │ Discolored        2.1     Reject   ❌ No     Waste/Dispose [Log]  │ │
│  │ Moisture damage   1.4     Reject   ❌ No     Waste/Dispose [Log]  │ │
│  │ ──────────────────────────────────────────────────────────────     │ │
│  │ Total off-grade:  16.2 kg   Reusable: 12.7 kg   Waste: 3.5 kg   │ │
│  │                                                                    │ │
│  │ [Map] = Map to reuse BOM/batch    [Log] = Log disposal reason    │ │
│  │                                                                    │ │
│  │ ⓘ Off-grade captured at each JC stage. Reusable stock moves to   │ │
│  │   offgrade_inventory for AI to factor into future plan generation │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  SECTION 6 — Sign-offs                                                  │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Production Incharge: _________ [Sign]                              │ │
│  │ QC Inspector:        _________ [Sign]                              │ │
│  │ Warehouse Incharge:  _________ [Sign]                              │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ACTION BAR:                                                             │
│  [📷 Receive Material] [▶ Start Production] [✅ Complete Step]          │
│  [📝 Record Output]    [🚚 Day-End Dispatch] [✍ Sign Off] [🔒 Close]   │
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 4: Team Leader Dashboard (`/production/team-dashboard`)

**Purpose:** Team leader's queue — their assigned job cards, priority-sorted.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: "My Job Cards"     Team Leader: Ramesh Kumar                   │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  ┌─ PRIORITY QUEUE ──────────────────────────────────────────────────┐  │
│  │                                                                    │  │
│  │  🟡 PO-2026-0042/1  Dates Arabian — SORTING    1,250 kg  IN PROG │  │
│  │     Started 09:15 | Step 1/2 done | Est: 2h left                  │  │
│  │     [Continue →]                                                   │  │
│  │                                                                    │  │
│  │  🔵 PO-2026-0043/1  Cashew W320 — SORTING      800 kg    READY   │  │
│  │     Material received | All 12 boxes scanned                      │  │
│  │     [Start Production →]                                           │  │
│  │                                                                    │  │
│  │  ⬜ PO-2026-0044/1  Almond Cal — SORTING       500 kg    LOCKED  │  │
│  │     Awaiting material (indent raised)                             │  │
│  │     [🔒 Locked]                                                    │  │
│  │                                                                    │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  TODAY'S SUMMARY:                                                       │
│  Completed: 2 | In Progress: 1 | Ready: 1 | Locked: 1                  │
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 5: QR Scanner Modal (within Job Card)

**Purpose:** Camera-based QR scan to receive physical boxes from stores.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  MODAL: "Receive Material — QR Scan"                         [✕ Close] │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  ┌────────────────────────────────────────┐  Material: Fard Dates Std   │
│  │                                        │  Need: 1,275.5 kg           │
│  │                                        │  Received: 780.3 kg         │
│  │           📷 CAMERA VIEW               │                              │
│  │                                        │  ████████████░░░░  61%      │
│  │           [scanning...]                │                              │
│  │                                        │  Scanned Boxes:             │
│  └────────────────────────────────────────┘  ┌────────────────────────┐ │
│                                               │ ✅ 97598567-1  10.0 kg│ │
│  Last scan:                                   │ ✅ 97598567-2   9.8 kg│ │
│  ✅ Box 97598567-3                            │ ✅ 97598567-3  10.2 kg│ │
│     Net: 10.2 kg | Lot: LOT-2026-001         │ ✅ 97598567-4  10.1 kg│ │
│     Matched to indent line 1                  │ ... (76 more)         │ │
│                                               └────────────────────────┘ │
│                                                                          │
│  [ Can't scan? Enter box_id manually: [__________] ]                    │
│                                                                          │
│  [Confirm Receipt]                                                       │
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 6: Day-End Dashboard (`/production/day-end`)

**Purpose:** End-of-day wrap-up — dispatch quantities, loss reconciliation, sign-offs.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: "Day-End Summary — 2026-04-01"                                 │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐                   │
│  │ Completed │ │ FG Output│ │ Total    │ │ Total    │                   │
│  │ 8 orders  │ │ 6,420 kg │ │ Loss     │ │ Off-Grade│                   │
│  │           │ │          │ │ 142.5 kg │ │ 85.2 kg  │                   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘                   │
│                                                                          │
│  COMPLETED ORDERS (final stage done today):                              │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Product            Customer  Expected  Actual   Dispatch  Status   │ │
│  │ ────────────────────────────────────────────────────────────────── │ │
│  │ Dates Arabian 500g D-Mart    2,500     2,480    [2,480]   ✅ Done │ │
│  │ Cashew W320 250g   Export    3,200     3,150    [3,150]   ✅ Done │ │
│  │ Almond Cal 1kg     Retail    500       495      [____]    ⬜ Pend │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  SIGN-OFFS:                                                              │
│  ☑ Production Incharge: [Signed — Ramesh Kumar]                         │
│  ☑ QC Inspector: [Signed — Priya Shah]                                  │
│  ☐ Warehouse Incharge: [________] [Sign]                                │
│                                                                          │
│  [Submit Day-End Report]                                                 │
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 7: Floor Inventory (`/production/floor-inventory`)

**Purpose:** Visual map of stock across all floor locations.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: "Floor Inventory"                   [Record Movement]          │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐         │
│  │  🏪 RM STORE     │  │  📦 PM STORE     │  │  🏭 PROD FLOOR  │         │
│  │                  │  │                  │  │                  │         │
│  │  Dates: 2,000 kg │  │  Pouches: 50,000│  │  WIP: 1,250 kg  │         │
│  │  Cashew: 1,200 kg│  │  Cartons: 5,000 │  │  3 active JCs   │         │
│  │  Almond: 300 kg  │  │  Labels: 80,000 │  │                  │         │
│  │  Raisin: 800 kg  │  │                  │  │                  │         │
│  │  (12 items total)│  │  (28 items)      │  │  (4 items)       │         │
│  │  [View All →]    │  │  [View All →]    │  │  [View All →]    │         │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘         │
│                                                                          │
│  ┌─────────────────┐  ┌─────────────────┐                               │
│  │  📤 FG STORE     │  │  ♻️ OFF-GRADE    │                               │
│  │                  │  │                  │                               │
│  │  Dates 500g: 2.4k│  │  Broken cashew:  │                               │
│  │  Cashew 250g: 3.1k│ │    45 kg         │                               │
│  │  (6 items)       │  │  Undersized alm: │                               │
│  │  [View All →]    │  │    12 kg         │                               │
│  └─────────────────┘  │  [View All →]    │                               │
│                        └─────────────────┘                               │
│                                                                          │
│  RECENT MOVEMENTS:                                                       │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Time   Material      From          To            Qty     Reason   │ │
│  │ 09:15  Fard Dates    RM Store      Prod Floor    1,280   JC-0042  │ │
│  │ 11:30  Arabian Pouch PM Store      Prod Floor    2,525   JC-0042  │ │
│  │ 14:00  Dates 500g    Prod Floor    FG Store      2,480   Output   │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 8: Loss Analytics (`/production/loss`)

**Purpose:** Charts and tables showing process loss trends, anomalies.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: "Loss Analytics"    Filters: [Date ▼] [Product ▼] [Machine ▼] │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  ┌─────────────────────────────────┐  ┌─────────────────────────────┐   │
│  │  LOSS TREND (Line Chart)        │  │ LOSS BY CATEGORY (Bar)      │   │
│  │                                 │  │                             │   │
│  │  3% ─                           │  │  Sorting   ████████ 2.1%   │   │
│  │  2% ─ ────╲    ╱──             │  │  Roasting  ████ 1.0%       │   │
│  │  1% ─      ╲──╱                │  │  Packaging ██ 0.5%         │   │
│  │  0% ─────────────────          │  │  Metal Det █ 0.2%          │   │
│  │      Jan Feb Mar Apr           │  │  Spillage  █ 0.3%          │   │
│  └─────────────────────────────────┘  └─────────────────────────────┘   │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ ANOMALY FLAGS (AI-detected)                                        │ │
│  │                                                                    │ │
│  │ ⚠ 2026-03-28  Cashew W320  Sort L2  Loss: 4.2% (avg: 2.0%)      │ │
│  │   "Loss 110% above average. Check machine calibration."           │ │
│  │                                                                    │ │
│  │ ⚠ 2026-03-25  Almond Cal   Pack M1  Loss: 3.1% (avg: 1.5%)      │ │
│  │   "Packaging rejection spike. Possible seal temperature issue."   │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 8A: Floor KPI Alerts (`/production/floor-alerts`)

**Purpose:** Floor-level operational alerts — material aging, machine breakdowns, KPI breaches. Enables quick decision-making to shift plans to another floor or reschedule.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: "Floor KPI Alerts"       Floor: [All ▼]   Severity: [All ▼]  │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐                   │
│  │ 🔴 Critical│ │ ⚠ Warning│ │ ℹ Info   │ │ Resolved │                   │
│  │ 3 active  │ │ 5 active │ │ 8 today  │ │ 12 today │                   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘                   │
│                                                                          │
│  ── MATERIAL AGING ALERTS ──                                            │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ 🔴 CRITICAL — Material on Floor > 3 Days                          │ │
│  │                                                                    │ │
│  │ Floor  Material         Qty(kg)  Arrived     Days  Risk    Action  │ │
│  │ ────── ───────────────  ───────  ──────────  ────  ──────  ────── │ │
│  │ F1     Fard Dates Std   320      2026-03-23  5d    Expiry  [Move] │ │
│  │ F1     Cashew W320      85       2026-03-24  4d    Stale   [Move] │ │
│  │ F2     Almond Cal       150      2026-03-24  4d    Stale   [Move] │ │
│  │                                                                    │ │
│  │ ⚠ Warning — Material on Floor 2-3 Days                            │ │
│  │ F2     Raisin Golden    200      2026-03-25  3d    Monitor [Plan] │ │
│  │ F1     Arabian Pouch    5,000    2026-03-25  3d    Monitor [Plan] │ │
│  │                                                                    │ │
│  │ [Move] = Return to store / prioritize in next plan                │ │
│  │ [Plan] = Auto-suggest priority bump in next plan generation       │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ── MACHINE BREAKDOWN ALERTS ──                                         │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ 🔴 BREAKDOWN — Active Machine Failures                            │ │
│  │                                                                    │ │
│  │ Machine    Floor  Status       Since       Impact          Action  │ │
│  │ ─────────  ─────  ───────────  ──────────  ──────────────  ────── │ │
│  │ Sort L1    F1     🔴 DOWN      10:30 today 1,250 kg stuck  [Shift]│ │
│  │ Seal 2     F1     🟡 DEGRADED  Yesterday   30% slower      [Plan] │ │
│  │                                                                    │ │
│  │ AI RECOMMENDATION:                                                 │ │
│  │ ┌────────────────────────────────────────────────────────────────┐ │ │
│  │ │ "Sort L1 breakdown impacts Dates Arabian order (D-Mart,       │ │ │
│  │ │  deadline Apr 3). Recommend:                                   │ │ │
│  │ │  Option A: Shift to Sort L2 on Floor 2 — available after      │ │ │
│  │ │            14:00, adds 2 hrs due to lower throughput           │ │ │
│  │ │  Option B: Reschedule to tomorrow — Sort L1 ETA repair: 18:00 │ │ │
│  │ │  Option C: Split batch — 600 kg today on Sort L2, rest tmrw"  │ │ │
│  │ └────────────────────────────────────────────────────────────────┘ │ │
│  │                                                                    │ │
│  │ [Shift] = Shift plan to another floor/machine                     │ │
│  │ Quick actions: [Accept Option A] [Accept Option B] [Accept Option C]│ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ── FLOOR KPI SCORECARD ──                                              │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Floor   Yield%  Loss%  Machine↑%  OEE%   On-Time%  Status        │ │
│  │ ──────  ──────  ─────  ─────────  ─────  ────────  ───────────── │ │
│  │ F1      94.2%   2.1%   78%        72%    85%       ⚠ Below Tgt  │ │
│  │ F2      97.5%   1.2%   92%        88%    96%       🟢 On Track  │ │
│  │ F3      —       —      —          —      —         ⬜ Idle       │ │
│  │                                                                    │ │
│  │ Targets: Yield > 96% | Loss < 1.5% | OEE > 80% | On-Time > 90% │ │
│  │ ⚠ F1 breaching Loss% and OEE targets for 3 consecutive days     │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 9: BOM Management (`/production/bom`)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: "Bill of Materials"                      [Upload BOM Excel]    │
│──────────────────────────────────────────────────────────────────────────│
│  Search: [____________]  Entity: [All ▼]  Status: [Active ▼]           │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ #   FG Product            Customer  Pack   Ver  Stages  Status    │ │
│  │ ── ─────────────────────  ────────  ─────  ───  ──────  ──────── │ │
│  │ 1  Dates Arabian 500g     D-Mart    0.5kg  v3   4       Active   │ │
│  │ 2  Dates Arabian 500g     Generic   0.5kg  v2   4       Active   │ │
│  │ 3  Cashew W320 250g       Export    0.25kg v1   3       Active   │ │
│  │ 4  Almond California 1kg  Retail    1.0kg  v2   3       Active   │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ── BOM Detail (click row to expand) ──                                 │
│                                                                          │
│  Materials:                                                              │
│  │ Material             Type  Qty/Unit  UOM   Loss%  Godown  Off-Grade │
│  │ Fard Dates Standard  RM    1.25      kg    2.0%   RM-1    No        │
│  │ Arabian 500g Pouch   PM    1.00      pcs   1.0%   PM-1    N/A       │
│  │ Carton 12x500g       PM    0.083     pcs   0.5%   PM-1    N/A       │
│  │ Sticker Label 500g   PM    1.00      pcs   1.0%   PM-1    N/A       │
│                                                                          │
│  Process Route:                                                          │
│  │ Step  Process          Std Time  Loss%  QC Check           Machine  │
│  │ 1     Sorting          60 min    2.0%   Visual + FM        Sort Tbl │
│  │ 2     Weighing         30 min    0.5%   Net weight ±2g     Scale    │
│  │ 3     Sealing          45 min    0.2%   Seal integrity     Sealer   │
│  │ 4     Metal Detection  20 min    0.1%   Fe/Nfe/SS pass     MD-01    │
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 10: FY Close Review (`/production/fy-review`)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: "FY Close Review — 2025-26"                                    │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  Summary: 45 unfulfilled orders | 12,500 kg remaining | 8 customers     │
│                                                                          │
│  ☐ Select All   [Carry Forward Selected]   [Export]                     │
│                                                                          │
│  ▼ D-MART (12 orders, 4,200 kg remaining)                              │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ ☐ SO#     Product          Original  Produced  Remaining  Actions │ │
│  │ ☐ SO-118  Dates Arab 500g  2,500 kg  1,800 kg    700 kg   [CF][R]│ │
│  │ ☐ SO-122  Cashew 250g      1,000 kg    600 kg    400 kg   [CF][R]│ │
│  │ ☐ SO-130  Almond 1kg       3,100 kg  3,100 kg      0 kg   ✅ Done│ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ▼ AMAZON (8 orders, 3,800 kg remaining)                               │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ ...                                                                │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  [CF] = Carry Forward to 2026-27    [R] = Revise qty/date   [X] = Cancel│
└──────────────────────────────────────────────────────────────────────────┘
```

---

### Screen 11: AI Insights & Order Intelligence (`/production/ai-insights`)

**Purpose:** Comprehensive AI-driven dashboard covering total orders outlay, fill rates, OTIF, SLAs, capacity tracking, delayed orders, pending orders, and factory performance rating. This is the command center for management-level visibility.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  HEADER: "AI Insights & Order Intelligence"                             │
│  Filters: [Entity ▼] [Date Range ▼] [Customer ▼] [Product ▼]          │
│──────────────────────────────────────────────────────────────────────────│
│                                                                          │
│  ── TOTAL ORDERS OUTLAY ──                                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│  │ Total SOs │ │ Total Qty│ │ Fulfilled│ │ Pending  │ │ Delayed  │     │
│  │ 248       │ │ 84,500 kg│ │ 62,300 kg│ │ 18,200 kg│ │ 4,000 kg │     │
│  │ this month│ │          │ │ 73.7%    │ │ 21.5%    │ │ 4.7% ⚠  │     │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘     │
│                                                                          │
│  ── FILL RATE DASHBOARD ──                                              │
│  ┌────────────────────────────────┐  ┌────────────────────────────────┐ │
│  │ OVERALL FILL RATE              │  │ CUSTOMER-WISE FILL RATE        │ │
│  │                                │  │                                │ │
│  │        ┌───────┐               │  │ Customer     Fill%  Target Gap│ │
│  │       ╱    73.7%╲              │  │ ──────────── ────── ────── ───│ │
│  │      │  ████████ │             │  │ D-Mart       82.5%  90%  🔴 │ │
│  │      │  ████████ │             │  │ Amazon       91.2%  90%  🟢 │ │
│  │       ╲         ╱              │  │ Export Co    68.4%  85%  🔴 │ │
│  │        └───────┘               │  │ Retail Gen   76.0%  80%  🟡 │ │
│  │  Target: 85%  Gap: -11.3%     │  │ BigBasket    88.9%  85%  🟢 │ │
│  │                                │  │ Flipkart     70.1%  85%  🔴 │ │
│  │  Trend: ↗ +2.1% vs last month │  │                                │ │
│  └────────────────────────────────┘  └────────────────────────────────┘ │
│                                                                          │
│  ── ORDER SLA & OTIF TRACKING ──                                        │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ OTIF (On-Time In-Full) Score:  71.8%    Target: 90%    🔴 BELOW  │ │
│  │                                                                    │ │
│  │ Breakdown:         On-Time   In-Full   OTIF                       │ │
│  │ ──────────────── ─────────  ────────  ──────                      │ │
│  │ This week          78.5%     84.2%     71.8%                      │ │
│  │ Last week          82.1%     86.0%     74.3%                      │ │
│  │ Month avg          80.2%     85.1%     73.0%                      │ │
│  │                                                                    │ │
│  │ SLA DAYS TRACKER:                                                  │ │
│  │ SLA Window   Orders   On-Track   At-Risk   Breached              │ │
│  │ ──────────── ──────── ────────── ───────── ─────────             │ │
│  │ 0-3 days     45       38 (84%)   5 (11%)   2 (4%)               │ │
│  │ 4-7 days     32       28 (88%)   3 (9%)    1 (3%)               │ │
│  │ 8-14 days    18       16 (89%)   2 (11%)   0 (0%)               │ │
│  │ 15+ days     8        6 (75%)    1 (13%)   1 (13%)              │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ── DELAYED ORDERS TRACKER ──                                           │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ SO#      Customer   Product          Due Date    Delay  Reason    │ │
│  │ ──────── ────────── ───────────────  ──────────  ─────  ──────── │ │
│  │ SO-142   D-Mart     Dates Arab 500g  2026-03-24  3d     RM Short │ │
│  │ SO-148   Export Co  Cashew W320      2026-03-25  2d     Machine↓ │ │
│  │ SO-155   Flipkart   Almond Cal 1kg   2026-03-26  1d     Capacity │ │
│  │ SO-161   Retail     Raisin 250g      2026-03-26  1d     QC Hold  │ │
│  │                                                                    │ │
│  │ Total delayed: 12 orders | 4,000 kg | Avg delay: 2.3 days        │ │
│  │ AI suggestion: "Prioritize SO-142 (D-Mart, high-value) on F1     │ │
│  │  tomorrow. SO-148 can move to Sort L2 if L1 repair completes."   │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ── DAILY CAPACITY TRACKER ──                                           │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Date        Capacity  Planned  Actual   Usage%  Fill%   Gap      │ │
│  │ ──────────  ────────  ───────  ───────  ──────  ──────  ──────── │ │
│  │ 2026-03-27  12,000    10,800   9,200    76.7%   85.2%   1,600 kg│ │
│  │ 2026-03-26  12,000    11,200   10,500   87.5%   93.8%     700 kg│ │
│  │ 2026-03-25  12,000    11,500   10,800   90.0%   93.9%     700 kg│ │
│  │ 2026-03-24  12,000    10,000    8,400   70.0%   84.0%   1,600 kg│ │
│  │                                                                    │ │
│  │ [Bar Chart: Capacity vs Planned vs Actual — last 7 days]          │ │
│  │  12k ─  ████                                                      │ │
│  │  10k ─  ████ ▓▓▓▓                                                │ │
│  │   8k ─  ████ ▓▓▓▓ ░░░░                                           │ │
│  │   6k ─  ████ ▓▓▓▓ ░░░░                                           │ │
│  │         Mon  Tue  Wed  Thu  Fri  Sat  Sun                         │ │
│  │  ████ Capacity  ▓▓▓▓ Planned  ░░░░ Actual                        │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ── PENDING ORDERS TRACKER ──                                           │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Customer     Pending SOs  Pending Qty  Oldest Order  Urgency     │ │
│  │ ──────────── ───────────  ───────────  ────────────  ─────────── │ │
│  │ D-Mart       8            4,200 kg     2026-03-20    🔴 Critical │ │
│  │ Export Co    5            3,100 kg     2026-03-22    🔴 Critical │ │
│  │ Amazon       4            2,800 kg     2026-03-25    🟡 Medium   │ │
│  │ Retail Gen   6            3,500 kg     2026-03-24    🟡 Medium   │ │
│  │ BigBasket    3            1,800 kg     2026-03-26    🟢 Normal   │ │
│  │ Flipkart    5            2,800 kg     2026-03-23    🔴 Critical │ │
│  │                                                                    │ │
│  │ Total pending: 31 orders | 18,200 kg                              │ │
│  │ Estimated clear time: 4.2 days at current production rate         │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ── FACTORY PERFORMANCE RATING ──                                       │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │                                                                    │ │
│  │  OVERALL FACTORY SCORE:  ★★★☆☆  3.2 / 5.0    Rating: AVERAGE    │ │
│  │                                                                    │ │
│  │  Parameter          Weight  Score   Weighted  Trend               │ │
│  │  ──────────────── ──────── ──────  ────────  ─────               │ │
│  │  OTIF %             25%     3.6     0.90      ↗ Improving         │ │
│  │  Fill Rate          20%     3.7     0.74      ↗ Improving         │ │
│  │  Capacity Util.     15%     3.8     0.57      → Stable            │ │
│  │  Process Loss       15%     2.8     0.42      ↘ Worsening         │ │
│  │  Machine Uptime     10%     2.5     0.25      ↘ Worsening         │ │
│  │  Order Timeliness   10%     2.4     0.24      → Stable            │ │
│  │  Quality (QC Pass)   5%     4.2     0.21      ↗ Improving         │ │
│  │  ──────────────────────────────────────────────                   │ │
│  │  TOTAL                      —       3.33                          │ │
│  │                                                                    │ │
│  │  AI INSIGHT: "Factory score dropped 0.3 points this week due to   │ │
│  │  Sort L1 breakdown (machine uptime) and almond shortage (loss%).  │ │
│  │  Focus areas: (1) preventive maintenance schedule for Sort L1,    │ │
│  │  (2) buffer stock policy for almonds, (3) redistribute D-Mart     │ │
│  │  orders across floors to recover OTIF."                           │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Design Notes

- **Color palette:** Professional enterprise palette — deep blue sidebar, white cards, green/yellow/red/grey for status
- **Typography:** Clean sans-serif. Bold section headers. Monospace for numbers/IDs
- **Responsive:** Desktop-first but tablet-friendly for team leader / floor manager screens (larger touch targets)
- **Status badges:** Consistent across all screens: locked=grey, unlocked=blue, in_progress=yellow, completed=green, closed=dark_green, error=red
- **Tables:** Zebra-striped, sortable headers, inline action buttons
- **Cards:** Summary stat cards at top of every dashboard (4 across)
- **Modals:** QR scanner, output recording, sign-off forms
- **Navigation:** Collapsible sidebar with icons + labels, breadcrumbs on detail pages

---

## All Screens Summary

| # | Screen | Route | Users |
|---|--------|-------|-------|
| 1 | Production Dashboard | `/production` | All |
| 2 | Plan Generation (+ Floor & Unit Distribution) | `/production/plans/generate` | Planner |
| 3 | Plan List | `/production/plans` | All |
| 4 | Job Card List | `/production/job-cards` | All |
| 5 | Job Card Detail (+ Balance Material & Off-Grade Segregation) | `/production/job-cards/{id}` | All |
| 6 | Team Dashboard | `/production/team-dashboard` | Team Leader |
| 7 | QR Scanner | Modal within Job Card | Floor Manager |
| 8 | Day-End Dashboard | `/production/day-end` | Floor Manager |
| 8A | Floor KPI Alerts (material aging, machine breakdown, KPI scorecard) | `/production/floor-alerts` | Floor Manager, Planner |
| 9 | Floor Inventory | `/production/floor-inventory` | Stores |
| 10 | Indent Dashboard | `/production/indents` | Purchase |
| 11 | Alerts Panel | Sidebar component | All |
| 12 | FY Close Review | `/production/fy-review` | Planner |
| 13 | Fulfillment Tracker | `/production/fulfillment` | All |
| 14 | Off-Grade Dashboard | `/production/offgrade` | Planner |
| 15 | AI Recommendations | `/production/ai` | Planner |
| 16 | BOM Management | `/production/bom` | Planner |
| 17 | Machine Management | `/production/machines` | Planner |
| 18 | Loss Analytics | `/production/loss` | All |
| 19 | AI Insights & Order Intelligence (fill rates, OTIF, SLAs, capacity, pending orders, factory rating) | `/production/ai-insights` | Planner, Management |
