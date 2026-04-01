# Part 4: Job Card Engine — Frontend Implementation Plan

---

## Context

The backend for Part 4 (Job Card Engine) is **fully implemented** with 22 API endpoints covering Production Orders, Sequential Job Cards, QR Material Receipt, Job Card Execution, Annexures (A-E), Completion/Unlock, Sign-offs, Force Unlock, and Dashboards. **No frontend pages exist yet** (only a "Coming Soon" stub at `work-orders/`). This is the largest frontend module — it mirrors the physical CFC/PRD/JC/V3.0 job card form used on the factory floor.

**Tech stack:** Electron desktop app, vanilla JS/HTML/CSS, IPC-based navigation, fetch API, CSS custom properties from `variables.css`.

**Job Card Status Flow:**
```
locked → unlocked → assigned → material_received → in_progress → completed → closed
                ↑                                                      ↓
          force_unlock                                          auto-unlock next
```

**Job Card Sections (matches physical form):**
- Section 1: Product Details
- Section 2A: RM Indent (Raw Materials)
- Section 2B: PM Indent (Packaging Materials)
- Section 3: Team & Process Flags
- Section 4: Process Steps (operator + QC sign-off per step)
- Section 5: Output & Losses
- Section 6: Sign-offs (Production / QC / Warehouse)
- Annexure A/B: Metal Detection
- Annexure B: Weight/Leak Checks (20 samples)
- Annexure C: Environmental Monitoring
- Annexure D: Loss Reconciliation (6 categories)
- Annexure E: Remarks & Deviations

---

## Gap Analysis: All 22 Backend Endpoints — None Built

| # | Endpoint | Description |
|---|----------|-------------|
| 1 | `POST /orders/create-from-plan` | ❌ Create production orders from approved plan |
| 2 | `GET /orders` | ❌ List production orders |
| 3 | `GET /orders/{id}` | ❌ Order detail with job cards |
| 4 | `POST /job-cards/generate` | ❌ Generate sequential job cards |
| 5 | `GET /job-cards` | ❌ List job cards (filtered) |
| 6 | `GET /job-cards/team-dashboard` | ❌ Team leader's job cards |
| 7 | `GET /job-cards/floor-dashboard` | ❌ Floor-level job card view |
| 8 | `GET /job-cards/{id}` | ❌ Full job card detail (6 sections + 5 annexures) |
| 9 | `PUT /job-cards/{id}/assign` | ❌ Assign to team leader |
| 10 | `POST /job-cards/{id}/receive-material` | ❌ QR material receipt |
| 11 | `PUT /job-cards/{id}/start` | ❌ Start production |
| 12 | `PUT /job-cards/{id}/complete-step` | ❌ Complete process step |
| 13 | `PUT /job-cards/{id}/record-output` | ❌ Record Section 5 output |
| 14 | `PUT /job-cards/{id}/complete` | ❌ Complete job card + auto-unlock |
| 15 | `PUT /job-cards/{id}/sign-off` | ❌ Section 6 sign-offs |
| 16 | `PUT /job-cards/{id}/close` | ❌ Close after all sign-offs |
| 17 | `PUT /job-cards/{id}/force-unlock` | ❌ Force unlock with audit |
| 18 | `POST /job-cards/{id}/environment` | ❌ Annexure C data |
| 19 | `POST /job-cards/{id}/metal-detection` | ❌ Annexure A/B data |
| 20 | `POST /job-cards/{id}/weight-checks` | ❌ Annexure B data |
| 21 | `POST /job-cards/{id}/loss-reconciliation` | ❌ Annexure D data |
| 22 | `POST /job-cards/{id}/remarks` | ❌ Annexure E data |

---

## Implementation Plan

### Page 1: Production Orders Page (NEW)
**Path:** `Desktop/src/modules/production/orders/`
**Nav route:** `navigate-to-orders`

List and manage production orders. Entry point for generating job cards from approved plans.

### Page 2: Job Card List Page (NEW)
**Path:** `Desktop/src/modules/production/job-cards/`
**Nav route:** `navigate-to-job-cards`

Filterable list of all job cards with status, assignment, and quick actions.

### Page 3: Job Card Detail Page (NEW)
**Path:** `Desktop/src/modules/production/job-card-detail/`
**Nav route:** `navigate-to-job-card-detail`

The main workhorse page — full job card form with 6 sections + 5 annexure tabs, matching the physical CFC/PRD/JC/V3.0 form. This is where floor operators, team leaders, and QC inspectors interact.

### Page 4: Team Dashboard (NEW)
**Path:** `Desktop/src/modules/production/team-dashboard/`
**Nav route:** `navigate-to-team-dashboard`

Team leader's personal view of assigned job cards, sorted by action priority.

### Page 5: Floor Dashboard (NEW)
**Path:** `Desktop/src/modules/production/floor-dashboard/`
**Nav route:** `navigate-to-floor-dashboard`

Floor manager's bird's eye view of all job cards on a specific floor.

---

## Detailed Checklist

### Page 1: Production Orders

#### 1A — Order List View
- [ ] Create folder `Desktop/src/modules/production/orders/`
- [ ] Create `orders/index.html` with standard layout (titlebar, sidebar, content)
- [ ] Header: breadcrumbs (Production > Production Orders), entity selector (All/CFPL/CDPL)
- [ ] Toolbar:
  - [ ] Status filter dropdown: All / Created / Job Cards Issued / Completed
  - [ ] "Create from Plan" button (primary action)
- [ ] Create `orders/orders.js`
- [ ] Write `loadOrders()` async function:
  - [ ] Call `GET /api/v1/production/orders?entity=X&status=X&page=X&page_size=50`
  - [ ] Handle loading/error states
  - [ ] Call `renderTable()` and `renderPagination()`
- [ ] Write `renderTable(results)` function:
  - [ ] Columns: Order # | Batch # | FG SKU | Customer | Batch Size (kg) | Stages | Status | Created | Actions
  - [ ] Order # in monospace font
  - [ ] Batch # in monospace font
  - [ ] Status badge: created (amber), job_cards_issued (blue), completed (green)
  - [ ] Actions: View (eye icon) → navigate to order detail, Generate JCs (if status=created)
  - [ ] Empty state: "No production orders. Create from an approved plan."
- [ ] Write `renderPagination()` — standard pattern
- [ ] Wire filters and entity selector
- [ ] Create `orders/styles.css`
- [ ] Test: orders load with filters
- [ ] Test: pagination works
- [ ] Test: empty state renders

#### 1B — Create from Plan
- [ ] Add "Create from Plan" button in toolbar
- [ ] On click: show modal with plan selector
  - [ ] Fetch approved plans: `GET /api/v1/production/plans?status=approved&entity=X`
  - [ ] Render as selectable list: Plan Name | Date | Entity | Lines count
  - [ ] "Create Orders" button
- [ ] Write `createFromPlan(planId)` async function:
  - [ ] Show confirmation: "Create production orders for all lines in this plan?"
  - [ ] Call `POST /api/v1/production/orders/create-from-plan` with `{plan_id}`
  - [ ] Show toast: "Created X production orders"
  - [ ] Reload order list
  - [ ] Close modal
- [ ] Show loading state during creation
- [ ] Test: modal shows approved plans
- [ ] Test: orders created successfully
- [ ] Test: new orders appear in list

#### 1C — Order Detail View
- [ ] On order row click or view button: navigate to detail (or expand inline)
- [ ] Call `GET /api/v1/production/orders/{id}`
- [ ] Display order header: Order #, Batch #, FG SKU, Customer, Batch Size, Status, Total Stages
- [ ] Display job cards list (embedded in response):
  - [ ] Each card: JC # | Step | Process | Stage | Status | Lock | Team Leader | Actions
  - [ ] Lock icon: locked (red padlock), unlocked (green open padlock), force_unlocked (orange warning)
  - [ ] Status badges per job card status
  - [ ] Click row → navigate to job-card-detail
- [ ] "Generate Job Cards" button (only if status='created'):
  - [ ] Call `POST /api/v1/production/job-cards/generate` with `{prod_order_id}`
  - [ ] Show toast: "Generated X job cards"
  - [ ] Reload detail
- [ ] Test: order detail loads with job cards
- [ ] Test: generate job cards works
- [ ] Test: job card navigation works

---

### Page 2: Job Card List

#### 2A — Job Card List View
- [ ] Create folder `Desktop/src/modules/production/job-cards/`
- [ ] Create `job-cards/index.html` with standard layout
- [ ] Header: breadcrumbs (Production > Job Cards), entity selector
- [ ] Summary cards row (6 cards):
  - [ ] Locked (red) | Unlocked (amber) | Assigned (blue) | In Progress (purple) | Completed (green) | Closed (grey)
- [ ] Toolbar:
  - [ ] Status filter: All / Locked / Unlocked / Assigned / Material Received / In Progress / Completed / Closed
  - [ ] Team Leader filter: text input (search by name)
  - [ ] Floor filter: dropdown (factory floors)
  - [ ] Stage filter: dropdown (sorting, roasting, packaging, etc.)
  - [ ] Search: job card number or FG SKU
- [ ] Create `job-cards/job-cards.js`
- [ ] Write `loadJobCards()` async function:
  - [ ] Call `GET /api/v1/production/job-cards?entity=X&status=X&team_leader=X&floor=X&stage=X&page=X&page_size=50`
  - [ ] Handle loading/error
  - [ ] Call `renderTable()` and `renderPagination()`
- [ ] Write `renderTable(results)` function:
  - [ ] Columns: JC # | FG SKU | Customer | Step | Process | Stage | Status | Lock | Team | Floor | Actions
  - [ ] JC number in monospace
  - [ ] Lock column: icon indicator (locked/unlocked/force-unlocked)
  - [ ] Status badge with color per status
  - [ ] Stage pill (e.g., "sorting", "roasting", "packaging")
  - [ ] Actions: View (→ detail), Assign, Start, Force Unlock (conditional on status)
  - [ ] Quick action buttons only shown when applicable:
    - [ ] Assign: only if unlocked
    - [ ] Start: only if material_received/unlocked/assigned
    - [ ] Force Unlock: only if locked
- [ ] Write `renderSummaryCards()` — count by status from results or separate call
- [ ] Wire all filters with reload
- [ ] Create `job-cards/styles.css`
  - [ ] Lock icon styles (3 states)
  - [ ] Stage pill colors (per stage type)
  - [ ] Summary cards with status-colored accents
  - [ ] Quick action button styles
- [ ] Test: list loads with all filters
- [ ] Test: status filter works (comma-separated multi-select)
- [ ] Test: team leader search works
- [ ] Test: floor and stage filters work
- [ ] Test: quick actions show conditionally
- [ ] Test: pagination works

#### 2B — Quick Actions from List
- [ ] Write `quickAssign(jobCardId)` function:
  - [ ] Show small modal: Team Leader input + Team Members input (comma-separated)
  - [ ] Call `PUT /job-cards/{id}/assign`
  - [ ] Toast + reload
- [ ] Write `quickStart(jobCardId)` function:
  - [ ] Confirmation dialog: "Start production on JC {number}?"
  - [ ] Call `PUT /job-cards/{id}/start`
  - [ ] Toast + reload
- [ ] Write `quickForceUnlock(jobCardId)` function:
  - [ ] Modal: Authority Name input (required) + Reason textarea (required)
  - [ ] Call `PUT /job-cards/{id}/force-unlock`
  - [ ] Show warning if response contains `warning` field
  - [ ] Toast + reload
- [ ] Test: assign from list works
- [ ] Test: start from list works
- [ ] Test: force unlock with warning works

---

### Page 3: Job Card Detail (Main Form)

This is the most complex page — it mirrors the physical job card form with 6 sections + 5 annexure tabs.

#### 3A — Page Structure & Navigation
- [ ] Create folder `Desktop/src/modules/production/job-card-detail/`
- [ ] Create `job-card-detail/index.html`:
  - [ ] Standard layout (titlebar, sidebar)
  - [ ] Job card header bar: JC # | Status badge | Lock indicator | FG SKU | Customer
  - [ ] Action buttons bar (status-dependent): Assign | Start | Complete | Close | Force Unlock
  - [ ] Tab navigation for sections:
    - [ ] Tab 1: "Product & Materials" (Sections 1 + 2A + 2B)
    - [ ] Tab 2: "Team & Process" (Sections 3 + 4)
    - [ ] Tab 3: "Output & Losses" (Section 5)
    - [ ] Tab 4: "Sign-offs" (Section 6)
    - [ ] Tab 5: "Metal Detection" (Annexure A/B)
    - [ ] Tab 6: "Weight Checks" (Annexure B)
    - [ ] Tab 7: "Environment" (Annexure C)
    - [ ] Tab 8: "Loss Reconciliation" (Annexure D)
    - [ ] Tab 9: "Remarks" (Annexure E)
  - [ ] Tab content containers (one per tab, show/hide)
- [ ] Create `job-card-detail/job-card-detail.js`
- [ ] Write `initPage()`:
  - [ ] Get jobCardId from IPC: `await ipcRenderer.invoke('get-job-card-id')`
  - [ ] Call `loadJobCard()`
- [ ] Write `loadJobCard()` async function:
  - [ ] Call `GET /api/v1/production/job-cards/{jobCardId}`
  - [ ] Store full response in `currentJC` state
  - [ ] Call `renderHeader()`, `renderActionButtons()`, `renderAllSections()`
  - [ ] Handle loading/error states
- [ ] Tab switching logic: click tab → show content, hide others, update active tab style
- [ ] Register IPC handle `get-job-card-id` in `main.js`
- [ ] Create `job-card-detail/styles.css`
- [ ] Test: page loads with correct job card data
- [ ] Test: tabs switch correctly
- [ ] Test: action buttons show per status

#### 3B — Header & Action Buttons
- [ ] Write `renderHeader()`:
  - [ ] JC number (large, monospace)
  - [ ] Status badge (colored per status)
  - [ ] Lock indicator: locked (red padlock + reason text), unlocked (green), force_unlocked (orange + "Force unlocked by X")
  - [ ] FG SKU name + Customer name
  - [ ] Batch # + Batch Size (kg)
  - [ ] Production Order # (clickable → navigate to order)
- [ ] Write `renderActionButtons()`:
  - [ ] Show/hide buttons based on `currentJC.status`:
    - [ ] **locked:** "Force Unlock" button (orange)
    - [ ] **unlocked:** "Assign" button (blue)
    - [ ] **assigned:** "Start Production" button (green)
    - [ ] **material_received:** "Start Production" button (green)
    - [ ] **in_progress:** "Complete" button (green) — only if all steps done
    - [ ] **completed:** "Close" button (green) — only if all 3 sign-offs present
  - [ ] All action buttons open confirmation or input modal
- [ ] Style header bar with prominent JC number + status
- [ ] Style lock indicator with icon + text
- [ ] Test: correct buttons per status
- [ ] Test: buttons disabled when not applicable

#### 3C — Section 1: Product Details (Read-only)
- [ ] Render in Tab 1 content area
- [ ] Info grid (2-column layout):
  - [ ] Customer Name | FG SKU Name
  - [ ] Business Unit (BU) | Article Code
  - [ ] Batch Number | Batch Size (kg)
  - [ ] Quantity (units) | Net Wt per Unit (kg)
  - [ ] Expected Units | MRP
  - [ ] EAN Code | Best Before Date
  - [ ] Factory | Floor
  - [ ] Sales Order Ref | Shelf Life (days)
- [ ] All fields read-only (populated from `section_1_product`)
- [ ] Monospace font for codes (batch #, EAN, article code)
- [ ] Style as clean info card with labels + values
- [ ] Test: all product details render correctly
- [ ] Test: handles null/missing fields gracefully

#### 3D — Section 2A: RM Indent (Raw Materials)
- [ ] Render in Tab 1 below product details
- [ ] Section title: "Raw Material Indent" with count
- [ ] Table columns: Material | UOM | Reqd Qty | Loss % | Gross Qty | Issued Qty | Batch # | Godown | Variance | Status
- [ ] Write `renderRMIndent(rm_lines)`:
  - [ ] Each row from `section_2a_rm_indent`
  - [ ] Qty formatted to 1 decimal
  - [ ] Status badge: pending (amber), partial (blue), fulfilled (green)
  - [ ] Variance column: color red if negative (under-issued), green if 0 or positive
  - [ ] Scanned box IDs as small chips (if any)
- [ ] "Receive Material" button (only if job card status allows):
  - [ ] Opens QR scanning interface (Section 3F below)
- [ ] Test: RM indent table renders correctly
- [ ] Test: status badges show correctly
- [ ] Test: variance coloring works

#### 3E — Section 2B: PM Indent (Packaging Materials)
- [ ] Render in Tab 1 below RM indent
- [ ] Section title: "Packaging Material Indent" with count
- [ ] Same table structure as RM indent
- [ ] Write `renderPMIndent(pm_lines)`:
  - [ ] Same rendering logic as RM
  - [ ] UOM may be "pcs" instead of "kg"
- [ ] Test: PM indent table renders correctly

#### 3F — QR Material Receipt
- [ ] Add "Receive Material (QR)" button in Section 2A/2B
- [ ] On click: open QR scanning modal
  - [ ] Input field: "Scan or enter box ID" (text input, auto-focus)
  - [ ] Scanned list: shows all scanned box IDs with material name + weight
  - [ ] "Add" button or Enter key to add box ID to list
  - [ ] "Submit All" button to send to API
  - [ ] "Clear" button to reset scanned list
- [ ] Write `receiveMultipleMaterials()` async function:
  - [ ] Collect all scanned box IDs
  - [ ] Call `POST /api/v1/production/job-cards/{id}/receive-material`
  - [ ] Body: `{box_ids: [...]}`
  - [ ] Handle per-box validation errors from API (box not found, already consumed, material mismatch)
  - [ ] Show results: X boxes received, Y failed
  - [ ] Reload job card to update indent statuses
- [ ] Visual feedback per scan: green check (valid), red X (invalid) with error reason
- [ ] Running total of received qty vs required qty
- [ ] Auto-close modal when all indent lines fulfilled
- [ ] Style: full-width modal, large font for scanning context, clear visual feedback
- [ ] Test: scanning adds box to list
- [ ] Test: submit processes all boxes
- [ ] Test: validation errors shown per box
- [ ] Test: indent status updates after receipt
- [ ] Test: job card status transitions to 'material_received' when all received

#### 3G — Section 3: Team & Process Flags
- [ ] Render in Tab 2 content area
- [ ] Team info card:
  - [ ] Team Leader: name (editable if status=unlocked/assigned)
  - [ ] Team Members: list of names (editable if status=unlocked/assigned)
  - [ ] Start Time: auto-set, read-only
  - [ ] End Time: auto-set, read-only
  - [ ] Total Time: calculated, read-only
- [ ] Process flags (checkboxes, editable during in_progress):
  - [ ] Fumigation required
  - [ ] Metal detector used
  - [ ] Roasting/Pasteurization
  - [ ] Control sample (gm): number input
  - [ ] Magnets used
- [ ] "Assign Team" button (if unlocked):
  - [ ] Team Leader text input (required)
  - [ ] Team Members text area (comma-separated)
  - [ ] Call `PUT /job-cards/{id}/assign`
- [ ] "Start Production" button (if material_received/unlocked/assigned):
  - [ ] Confirmation dialog
  - [ ] Call `PUT /job-cards/{id}/start`
  - [ ] Shows start_time after success
- [ ] Test: team info displays correctly
- [ ] Test: assign works
- [ ] Test: start sets status and time

#### 3H — Section 4: Process Steps
- [ ] Render in Tab 2 below team info
- [ ] Section title: "Process Steps" with progress indicator (e.g., "3 of 5 completed")
- [ ] Process step timeline/table:
  - [ ] Each step as a card or row:
    - [ ] Step # | Process Name | Machine | Std Time (min) | QC Check | Loss %
    - [ ] Operator: name + sign time (or "Pending" if not done)
    - [ ] QC: sign time + pass/fail (or "Pending")
    - [ ] Status badge: pending / in_progress / completed
    - [ ] Time Done: timestamp or "—"
- [ ] "Complete Step" action per step (only for next incomplete step):
  - [ ] Operator Name input (required)
  - [ ] QC Passed checkbox
  - [ ] Call `PUT /job-cards/{id}/complete-step`
  - [ ] Body: `{step_number, operator_name, qc_passed}`
  - [ ] Update step UI inline
- [ ] Visual progress: completed steps have green check, current step highlighted, future steps greyed
- [ ] Step timeline connector line (vertical line connecting steps)
- [ ] Style: timeline layout with step cards connected by vertical line
- [ ] Test: all steps render with correct data
- [ ] Test: complete step updates correctly
- [ ] Test: only next incomplete step is actionable
- [ ] Test: progress indicator updates

#### 3I — Section 5: Output & Losses
- [ ] Render in Tab 3 content area
- [ ] Form layout (editable during in_progress, read-only after):
  - [ ] FG Output section:
    - [ ] Expected Units (read-only, from plan)
    - [ ] Expected Kg (read-only)
    - [ ] Actual Units (number input)
    - [ ] Actual Kg (number input)
  - [ ] Materials section:
    - [ ] RM Consumed (kg) (number input)
    - [ ] Material Return (kg) (number input)
  - [ ] Rejection section:
    - [ ] Rejection (kg) (number input)
    - [ ] Rejection Reason (text input)
  - [ ] Loss section:
    - [ ] Process Loss (kg) (number input)
    - [ ] Process Loss % (auto-calculated: loss_kg / rm_consumed_kg * 100)
  - [ ] Off-grade section:
    - [ ] Off-grade (kg) (number input)
    - [ ] Off-grade Category (text input)
  - [ ] Dispatch:
    - [ ] Dispatch Qty (number input)
- [ ] "Save Output" button:
  - [ ] Call `PUT /job-cards/{id}/record-output`
  - [ ] Body: all Section 5 fields
  - [ ] Toast: "Output recorded"
  - [ ] Reload section
- [ ] Auto-calculate process_loss_pct when kg values change
- [ ] Highlight variances: actual vs expected (red if under, green if match/over)
- [ ] Summary card at top: Yield % = (actual_fg_kg / rm_consumed_kg) * 100
- [ ] Style: form grid with clear section dividers
- [ ] Test: form loads with existing data (if previously saved)
- [ ] Test: save works (UPSERT)
- [ ] Test: auto-calculation of loss %
- [ ] Test: variance highlighting works

#### 3J — Section 6: Sign-offs
- [ ] Render in Tab 4 content area
- [ ] Three sign-off cards (one per type):
  - [ ] Production Incharge: name + signed_at (or "Not signed")
  - [ ] Quality Analysis: name + signed_at (or "Not signed")
  - [ ] Warehouse Incharge: name + signed_at (or "Not signed")
- [ ] Each card has "Sign Off" button (if not yet signed):
  - [ ] Name input (required)
  - [ ] Call `PUT /job-cards/{id}/sign-off`
  - [ ] Body: `{sign_off_type: "production_incharge", name: "..."}`
  - [ ] Toast: "Signed off"
  - [ ] Update card inline
- [ ] Signed cards: green check icon + name + timestamp
- [ ] Unsigned cards: grey dashed border + "Awaiting signature"
- [ ] Progress indicator: "X of 3 sign-offs complete"
- [ ] "Close Job Card" button (only when all 3 signed + status=completed):
  - [ ] Confirmation dialog
  - [ ] Call `PUT /job-cards/{id}/close`
  - [ ] Validate all sign-offs present (client-side + server-side)
  - [ ] Toast: "Job card closed"
  - [ ] Reload page
- [ ] If close fails with `missing_sign_offs`: show which are missing
- [ ] Style: sign-off cards in row, green/grey states
- [ ] Test: sign-off works for each type
- [ ] Test: close only available when all 3 signed
- [ ] Test: missing sign-off error handled

#### 3K — Annexure A/B: Metal Detection
- [ ] Render in Tab 5 content area
- [ ] Form for recording metal detection checks:
  - [ ] Check Type: select (pre_packaging / post_packaging)
  - [ ] Metal Detection Results:
    - [ ] Fe (Ferrous): pass/fail toggle
    - [ ] NFe (Non-Ferrous): pass/fail toggle
    - [ ] SS (Stainless Steel): pass/fail toggle
    - [ ] Failed Units: number input
    - [ ] Remarks: text input
  - [ ] Seal Check: pass/fail toggle + Failed Units
  - [ ] Weight Check: pass/fail toggle + Failed Units
  - [ ] Temperature readings (optional):
    - [ ] Dough Temp (°C)
    - [ ] Oven Temp (°C)
    - [ ] Baking Temp (°C)
- [ ] "Save" button:
  - [ ] Call `POST /job-cards/{id}/metal-detection`
  - [ ] Toast: "Metal detection recorded"
- [ ] Previously recorded checks shown as read-only cards above the form
- [ ] Multiple checks allowed (pre and post packaging)
- [ ] Pass = green indicator, Fail = red indicator
- [ ] Style: toggle switches for pass/fail, grouped sections
- [ ] Test: save pre-packaging check
- [ ] Test: save post-packaging check
- [ ] Test: multiple checks display correctly

#### 3L — Annexure B: Weight/Leak Checks (20 samples)
- [ ] Render in Tab 6 content area
- [ ] Header inputs:
  - [ ] Target Weight (g)
  - [ ] Tolerance (g)
  - [ ] Accept Range Min (g) — auto-calculated: target - tolerance
  - [ ] Accept Range Max (g) — auto-calculated: target + tolerance
- [ ] 20-row sample table:
  - [ ] Columns: Sample # | Net Weight (g) | Gross Weight (g) | Leak Test | Status
  - [ ] Each row: number inputs for weights, pass/fail toggle for leak
  - [ ] Status: auto-calculated — "PASS" if weight within range + leak pass, else "FAIL"
  - [ ] Row coloring: green (pass), red (fail)
- [ ] Pre-populate 20 empty rows (sample_number 1-20)
- [ ] "Save All" button:
  - [ ] Call `POST /job-cards/{id}/weight-checks`
  - [ ] Body: `{target_wt_g, tolerance_g, accept_range_min, accept_range_max, samples: [...]}`
  - [ ] Only send rows with data entered
  - [ ] Toast: "Weight checks saved"
- [ ] Summary bar: Total samples | Pass count | Fail count | Pass rate %
- [ ] Style: compact table with small inputs, color-coded rows
- [ ] Test: 20 rows render
- [ ] Test: auto-range calculation works
- [ ] Test: pass/fail status calculates correctly
- [ ] Test: save with partial data works (not all 20 filled)
- [ ] Test: summary counts correct

#### 3M — Annexure C: Environmental Monitoring
- [ ] Render in Tab 7 content area
- [ ] Parameter form with fields for each environmental parameter:
  - [ ] Brine Salinity
  - [ ] Temperature (°C)
  - [ ] Humidity (%)
  - [ ] Fan Speed (%)
  - [ ] RPM
  - [ ] Gas
  - [ ] Magnet check
- [ ] Each parameter: label + value input
- [ ] "Save" button:
  - [ ] Call `POST /job-cards/{id}/environment`
  - [ ] Body: `{parameters: [{parameter_name: "brine_salinity", value: "3.5"}, ...]}`
  - [ ] Toast: "Environment data recorded"
- [ ] Previously recorded readings shown with timestamp
- [ ] Multiple readings allowed (different times of day)
- [ ] Style: simple form grid, readings history below
- [ ] Test: save environment data
- [ ] Test: previous readings display

#### 3N — Annexure D: Loss Reconciliation
- [ ] Render in Tab 8 content area
- [ ] 6-row table (one per loss category):
  - [ ] Categories: Sorting Rejection | Roasting/Process Loss | Packaging Rejection | Metal Detector | Spillage/Handling | QC Sample Consumed
  - [ ] Columns per row: Category | Budgeted Loss % | Budgeted Loss (kg) | Actual Loss (kg) | Variance (kg) | Remarks
  - [ ] Budgeted values: number inputs (or pre-filled from BOM if available)
  - [ ] Actual values: number inputs
  - [ ] Variance: auto-calculated (actual - budgeted), colored red if positive (over budget), green if negative/zero
- [ ] "Save" button:
  - [ ] Call `POST /job-cards/{id}/loss-reconciliation`
  - [ ] Body: `{entries: [{loss_category, budgeted_loss_pct, budgeted_loss_kg, actual_loss_kg, remarks}, ...]}`
  - [ ] Toast: "Loss reconciliation saved"
- [ ] Summary row at bottom: Total budgeted | Total actual | Total variance
- [ ] Style: form table with colored variance cells
- [ ] Test: all 6 categories render
- [ ] Test: variance auto-calculates
- [ ] Test: save works
- [ ] Test: summary totals correct

#### 3O — Annexure E: Remarks & Deviations
- [ ] Render in Tab 9 content area
- [ ] Remark type selector: Observation / Deviation / Corrective Action
- [ ] Content textarea (required)
- [ ] Recorded By input (pre-fill from localStorage)
- [ ] "Add Remark" button:
  - [ ] Call `POST /job-cards/{id}/remarks`
  - [ ] Body: `{remark_type, content, recorded_by}`
  - [ ] Toast: "Remark added"
  - [ ] Prepend to remarks list
- [ ] Previously recorded remarks shown as cards:
  - [ ] Type badge (observation=blue, deviation=red, corrective_action=green)
  - [ ] Content text
  - [ ] Recorded by + timestamp
  - [ ] Sorted newest first
- [ ] Empty state: "No remarks recorded"
- [ ] Style: remark cards with type-colored left border
- [ ] Test: add remark of each type
- [ ] Test: remarks list displays correctly
- [ ] Test: validation blocks empty content

#### 3P — Job Card Completion
- [ ] "Complete Job Card" button (shown when status=in_progress and all steps completed):
  - [ ] Validate: all process steps status='completed'
  - [ ] Validate: output recorded (Section 5 saved)
  - [ ] Show confirmation: "Complete JC {number}? This will auto-unlock the next stage."
  - [ ] Call `PUT /job-cards/{id}/complete`
  - [ ] Handle response:
    - [ ] Show `total_time_min` in toast
    - [ ] If `next_unlocked`: show "Next job card {number} has been unlocked"
    - [ ] If `order_completed`: show "Production order completed! FG moved to store."
    - [ ] If `process_loss_recorded`: note in toast
    - [ ] If `offgrade_created`: note in toast
  - [ ] Reload page
- [ ] Validation warnings if output not yet recorded (allow override)
- [ ] Test: complete transitions status
- [ ] Test: next stage auto-unlocked
- [ ] Test: last stage completes order
- [ ] Test: fulfillment updated

#### 3Q — Force Unlock
- [ ] "Force Unlock" button (shown when status=locked):
  - [ ] Modal: Authority Name input + Reason textarea (both required)
  - [ ] Authority dropdown or text: "Production Incharge" / "Plant Head"
  - [ ] Call `PUT /job-cards/{id}/force-unlock`
  - [ ] Body: `{authority, reason}`
  - [ ] If response contains `warning`: show warning banner prominently
    - [ ] "No SFG available. Production may produce defective output."
  - [ ] Toast: "Job card force unlocked"
  - [ ] Reload page
- [ ] Force unlock indicator in header (orange badge: "Force unlocked by {name}")
- [ ] Show force unlock history: who, when, why
- [ ] Test: force unlock works
- [ ] Test: warning displayed when previous stage has no output
- [ ] Test: audit trail visible

---

### Page 4: Team Dashboard

#### 4A — Team View
- [ ] Create folder `Desktop/src/modules/production/team-dashboard/`
- [ ] Create `team-dashboard/index.html` with standard layout
- [ ] Header: "Team Dashboard" + Team Leader name input/selector
- [ ] Entity selector
- [ ] Create `team-dashboard/team-dashboard.js`
- [ ] Write `loadTeamDashboard(teamLeader)` async:
  - [ ] Call `GET /api/v1/production/job-cards/team-dashboard?team_leader=X&entity=Y`
  - [ ] Render job cards grouped by priority/status
- [ ] Write `renderTeamCards(jobCards)`:
  - [ ] Cards sorted by action priority: in_progress → material_received → assigned → unlocked
  - [ ] Each card: JC # | FG SKU | Status | Step/Stage | Start Time | Action button
  - [ ] Action button context-aware: "Start" / "Continue" / "Receive Material" / "Complete Step"
  - [ ] Click card → navigate to job-card-detail
- [ ] Color-coded card borders by status
- [ ] Summary: X cards total | Y in progress | Z pending action
- [ ] Auto-refresh option (poll every 60s)
- [ ] Create `team-dashboard/styles.css`
- [ ] Test: dashboard loads for team leader
- [ ] Test: cards sorted by priority
- [ ] Test: action buttons work
- [ ] Test: navigation to detail works

---

### Page 5: Floor Dashboard

#### 5A — Floor View
- [ ] Create folder `Desktop/src/modules/production/floor-dashboard/`
- [ ] Create `floor-dashboard/index.html` with standard layout
- [ ] Header: "Floor Dashboard" + Floor selector dropdown + Entity selector
- [ ] Create `floor-dashboard/floor-dashboard.js`
- [ ] Write `loadFloorDashboard(floor)` async:
  - [ ] Call `GET /api/v1/production/job-cards/floor-dashboard?floor=X&entity=Y`
  - [ ] Render all job cards on floor
- [ ] Write `renderFloorView(jobCards)`:
  - [ ] Group by status in columns (Kanban-style) or as filtered list:
    - [ ] Column 1: Locked (red header)
    - [ ] Column 2: Unlocked/Assigned (amber header)
    - [ ] Column 3: In Progress (blue header)
    - [ ] Column 4: Completed (green header)
  - [ ] Each card: JC # | FG SKU | Team Leader | Stage | Time elapsed (for in_progress)
  - [ ] Click → navigate to detail
- [ ] Status count summary bar at top
- [ ] Force unlock actions available for locked cards (floor manager authority)
- [ ] Auto-refresh option (poll every 30s)
- [ ] Create `floor-dashboard/styles.css`
  - [ ] Kanban column layout
  - [ ] Card styles per status
  - [ ] Time elapsed formatting
- [ ] Test: floor dashboard loads
- [ ] Test: Kanban columns render correctly
- [ ] Test: cards grouped by status
- [ ] Test: navigation works

---

### Navigation & Sidebar Updates

#### Main Process (main.js)
- [ ] Add page config for `'orders'`: path + dimensions
- [ ] Add page config for `'job-cards'`: path + dimensions
- [ ] Add page config for `'job-card-detail'`: path + dimensions
- [ ] Add page config for `'team-dashboard'`: path + dimensions
- [ ] Add page config for `'floor-dashboard'`: path + dimensions
- [ ] Add IPC handler: `navigate-to-orders`
- [ ] Add IPC handler: `navigate-to-job-cards`
- [ ] Add IPC handler: `navigate-to-job-card-detail` (stores jobCardId)
- [ ] Add IPC handle: `get-job-card-id` (returns stored jobCardId)
- [ ] Add IPC handler: `navigate-to-team-dashboard`
- [ ] Add IPC handler: `navigate-to-floor-dashboard`
- [ ] Add to `PAGE_LABELS` in `navigation.js`
- [ ] Test: all navigation routes work
- [ ] Test: back button works from all pages
- [ ] Test: job card ID passed correctly

#### Sidebar Updates (all production pages)
- [ ] Add nav items to sidebar in ALL production page HTML files:
  - [ ] "Production Orders" (with clipboard icon)
  - [ ] "Job Cards" (with card icon)
  - [ ] "Team Dashboard" (with people icon)
  - [ ] "Floor Dashboard" (with factory icon)
- [ ] Wire click handlers for all new nav items
- [ ] Active state on correct page
- [ ] Test: nav items visible on all pages
- [ ] Test: active states correct

---

### Cross-Cutting Concerns

#### Consistent Styling
- [ ] All new pages use standard layout pattern
- [ ] All tables use `.po-table` / `.po-row` pattern
- [ ] All modals use overlay + centered card pattern
- [ ] All badges use existing badge classes
- [ ] Job card status colors standardized:
  - [ ] locked = red (`--clr-mismatch`)
  - [ ] unlocked = amber (`--clr-warning`)
  - [ ] assigned = blue (`--clr-info`)
  - [ ] material_received = purple (`#7c3aed`)
  - [ ] in_progress = blue-dark (`#2563eb`)
  - [ ] completed = green (`--clr-ok`)
  - [ ] closed = grey (`--text-secondary`)
- [ ] Lock icons: locked (red padlock SVG), unlocked (green open padlock), force_unlocked (orange warning)
- [ ] Tab navigation styles consistent across all pages

#### Error Handling
- [ ] All API calls wrapped in try/catch
- [ ] All errors show toast with message
- [ ] Status transition errors: "Cannot start — job card is locked"
- [ ] Validation errors shown inline
- [ ] Loading states on all API calls
- [ ] Loading cleared on success + error

#### Data Flow Integration
- [ ] Plan Detail → "Create Orders" → Orders page
- [ ] Order Detail → "Generate JCs" → Job Card list
- [ ] Job Card List → click → Job Card Detail
- [ ] Job Card Detail → "View Order" → Order Detail
- [ ] Job Card Completion → auto-updates fulfillment (visible on fulfillment page)
- [ ] Force Unlock → creates alert (visible on alerts page)
- [ ] QR receipt → updates floor inventory

---

## Files to Create/Modify

| File | Action | Changes |
|------|--------|---------|
| `Desktop/src/modules/production/orders/index.html` | **Create** | Production orders page |
| `Desktop/src/modules/production/orders/orders.js` | **Create** | Order list, create-from-plan, detail |
| `Desktop/src/modules/production/orders/styles.css` | **Create** | Order page styles |
| `Desktop/src/modules/production/job-cards/index.html` | **Create** | Job card list page |
| `Desktop/src/modules/production/job-cards/job-cards.js` | **Create** | List, filters, quick actions |
| `Desktop/src/modules/production/job-cards/styles.css` | **Create** | Job card list styles |
| `Desktop/src/modules/production/job-card-detail/index.html` | **Create** | Full job card form (6 sections + 5 annexures) |
| `Desktop/src/modules/production/job-card-detail/job-card-detail.js` | **Create** | All sections, tabs, actions, annexures |
| `Desktop/src/modules/production/job-card-detail/styles.css` | **Create** | Form styles, tabs, timeline, sign-offs |
| `Desktop/src/modules/production/team-dashboard/index.html` | **Create** | Team leader dashboard |
| `Desktop/src/modules/production/team-dashboard/team-dashboard.js` | **Create** | Team card view, actions |
| `Desktop/src/modules/production/team-dashboard/styles.css` | **Create** | Dashboard card styles |
| `Desktop/src/modules/production/floor-dashboard/index.html` | **Create** | Floor Kanban dashboard |
| `Desktop/src/modules/production/floor-dashboard/floor-dashboard.js` | **Create** | Kanban view, floor filter |
| `Desktop/src/modules/production/floor-dashboard/styles.css` | **Create** | Kanban column styles |
| `Desktop/main.js` | Modify | 5 page configs, 5 IPC handlers, 1 invoke handle |
| `Desktop/src/shared/js/navigation.js` | Modify | Add PAGE_LABELS for 5 new pages |
| All production page HTML files (7+) | Modify | Add sidebar nav items |

---

## Verification Plan

1. **Production Orders:** Create orders from approved plan → verify order list populates
2. **Generate Job Cards:** From order detail → generate → verify sequential JCs with lock/unlock
3. **Job Card List:** Verify all filters work (status, team, floor, stage), pagination, summary cards
4. **Job Card Detail:** Load full detail → verify all 6 sections + 5 annexures render
5. **Assign & Start:** Assign team → start production → verify status transitions + timestamps
6. **QR Material Receipt:** Scan box IDs → verify indent lines update, floor inventory deducted
7. **Process Steps:** Complete steps sequentially → verify operator + QC sign, progress updates
8. **Record Output:** Enter Section 5 data → save → verify UPSERT (update on second save)
9. **Metal Detection:** Record pre + post packaging checks → verify both saved
10. **Weight Checks:** Enter 20 samples → verify pass/fail calculation, save with partial data
11. **Environment:** Record parameters → verify saved with timestamp
12. **Loss Reconciliation:** Enter 6 categories → verify variance calculation, save
13. **Remarks:** Add observations + deviations → verify listed with type badges
14. **Complete Job Card:** Complete → verify next JC auto-unlocked, or order completed on last stage
15. **Sign-offs:** Sign all 3 types → close job card → verify status=closed
16. **Force Unlock:** Force unlock locked JC → verify warning if no previous output, audit trail
17. **Team Dashboard:** Enter team leader → verify cards sorted by priority, actions work
18. **Floor Dashboard:** Select floor → verify Kanban columns, card grouping
19. **Full Workflow:** Plan → Orders → Job Cards → Assign → Material → Start → Steps → Output → Sign-offs → Close
20. **Navigation:** Test all 5 new routes, back button, cross-page links, sidebar items
