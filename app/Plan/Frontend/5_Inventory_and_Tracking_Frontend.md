# Part 5: Inventory & Tracking — Frontend Implementation Plan

---

## Context

The backend for Part 5 (Inventory & Tracking) is **fully implemented** with 13 API endpoints across 4 areas: Floor Inventory State Machine, Idle Material Alerts, Off-Grade Capture & Reuse, and Process Loss/Yield Analytics. **No dedicated frontend pages exist** for any of these features. This plan creates 4 new pages (Floor Inventory, Off-Grade Management, Loss Analytics, Movement History) and adds a manual movement workflow.

**Tech stack:** Electron desktop app, vanilla JS/HTML/CSS, IPC-based navigation, fetch API, CSS custom properties from `variables.css`.

**Floor Locations (State Machine):**
```
rm_store ──────→ production_floor ──────→ fg_store
pm_store ──────→ production_floor ──────→ offgrade
                 production_floor ──────→ rm_store (return)
                 offgrade ──────────────→ production_floor (reuse)
```

**Allowed Transitions Only:**
- `rm_store → production_floor` (material receipt)
- `pm_store → production_floor` (packaging stage)
- `production_floor → fg_store` (FG output)
- `production_floor → offgrade` (off-grade capture)
- `production_floor → rm_store` (material return)
- `offgrade → production_floor` (off-grade reuse)

---

## Gap Analysis: All 13 Backend Endpoints — None Built

| # | Endpoint | Description |
|---|----------|-------------|
| 50 | `GET /floor-inventory` | ❌ List inventory items (filterable, paginated) |
| 51 | `GET /floor-inventory/summary` | ❌ Aggregated stock per floor |
| 52 | `POST /floor-inventory/move` | ❌ Manual material movement |
| 53 | `GET /floor-inventory/movements` | ❌ Movement audit trail |
| 54 | `POST /floor-inventory/check-idle` | ❌ Trigger idle material check |
| 55 | `GET /offgrade/inventory` | ❌ List off-grade stock |
| 56 | `GET /offgrade/rules` | ❌ List reuse rules |
| 57 | `POST /offgrade/rules/create` | ❌ Create reuse rule |
| 58 | `PUT /offgrade/rules/{id}` | ❌ Update reuse rule |
| 59 | `GET /loss/analysis` | ❌ Loss analysis (group by product/stage/month/machine) |
| 60 | `GET /loss/anomalies` | ❌ Anomaly detection (> Nx avg) |
| 61 | `GET /yield/summary` | ❌ Yield by product/period |

**Note:** Endpoint 62 (`GET /job-cards/floor-dashboard`) already has a frontend page (floor-dashboard for job cards) — this is separate from floor inventory.

---

## Implementation Plan

### Page 1: Floor Inventory Page (NEW)
**Path:** `Desktop/src/modules/production/floor-inventory/`
**Nav route:** `navigate-to-floor-inventory`

Central page for viewing real-time stock across all floor locations, manual material movements, and idle material checks.

### Page 2: Movement History Page (NEW)
**Path:** `Desktop/src/modules/production/movements/`
**Nav route:** `navigate-to-movements`

Full audit trail of all material movements with advanced filtering.

### Page 3: Off-Grade Management Page (NEW)
**Path:** `Desktop/src/modules/production/offgrade/`
**Nav route:** `navigate-to-offgrade`

View off-grade inventory, manage reuse rules, track consumption.

### Page 4: Loss & Yield Analytics Page (NEW)
**Path:** `Desktop/src/modules/production/loss-analytics/`
**Nav route:** `navigate-to-loss-analytics`

Process loss analysis with grouping, anomaly detection, and yield summaries.

---

## Detailed Checklist

### Page 1: Floor Inventory

#### 1A — Floor Summary Dashboard
- [ ] Create folder `Desktop/src/modules/production/floor-inventory/`
- [ ] Create `floor-inventory/index.html` with standard layout (titlebar, sidebar, content)
- [ ] Header: breadcrumbs (Production > Floor Inventory), entity selector (All/CFPL/CDPL)
- [ ] Floor summary cards row (4 cards, one per location):
  - [ ] Card 1: "RM Store" — item count + total kg (with warehouse icon)
  - [ ] Card 2: "PM Store" — item count + total kg (with package icon)
  - [ ] Card 3: "Production Floor" — item count + total kg (with factory icon)
  - [ ] Card 4: "FG Store" — item count + total kg (with box icon)
  - [ ] Each card: colored top border, clickable to filter table below
  - [ ] Active card highlighted when filtering by that floor
- [ ] Create `floor-inventory/floor-inventory.js`
- [ ] Write `loadSummary()` async function:
  - [ ] Call `GET /api/v1/production/floor-inventory/summary?entity=X`
  - [ ] Populate 4 summary cards with item_count + total_kg
  - [ ] Handle loading/error states
- [ ] Style summary cards: 4-column grid, colored accents per location:
  - [ ] rm_store = amber, pm_store = blue, production_floor = purple, fg_store = green
- [ ] Test: summary loads with correct data per entity
- [ ] Test: entity switch reloads summary
- [ ] Test: empty floor shows "0 items, 0 kg"

#### 1B — Inventory List View
- [ ] Toolbar row below summary:
  - [ ] Floor Location filter: All / RM Store / PM Store / Production Floor / FG Store (buttons or dropdown)
  - [ ] Search input (debounced 300ms): search by SKU name
  - [ ] "Move Material" button (primary action)
  - [ ] "Check Idle" button (secondary)
- [ ] Inventory table:
  - [ ] Columns: SKU Name | Item Type | Floor Location | Qty (kg) | Lot # | Last Updated | Actions
  - [ ] SKU name with title tooltip
  - [ ] Item type badge: RM (amber), PM (blue), WIP (purple), FG (green)
  - [ ] Qty formatted to 1 decimal with "kg" suffix
  - [ ] Lot number in monospace (or "—" if null)
  - [ ] Last Updated: relative time ("3h ago", "2d ago") + absolute on hover
  - [ ] Idle indicator: yellow warning icon if last_updated > 3 days, red if > 5 days
  - [ ] Actions: "Move" button per row (opens move modal pre-filled with this SKU)
- [ ] Write `loadInventory()` async function:
  - [ ] Call `GET /api/v1/production/floor-inventory?entity=X&floor_location=X&search=X&page=X&page_size=50`
  - [ ] Handle loading/error
  - [ ] Call `renderTable()` and `renderPagination()`
- [ ] Write `renderTable(results)` function:
  - [ ] Render all columns as described
  - [ ] Calculate idle days from `last_updated` for warning indicators
  - [ ] Empty state: "No inventory items found"
- [ ] Write `renderPagination()` — standard pattern
- [ ] Clicking summary card filters table to that floor location
- [ ] Wire floor filter, search, entity selector to reload
- [ ] Create `floor-inventory/styles.css`
- [ ] Test: inventory loads with all filters
- [ ] Test: floor card click filters table
- [ ] Test: search filters by SKU
- [ ] Test: idle indicators show correctly
- [ ] Test: pagination works
- [ ] Test: empty state renders

#### 1C — Manual Material Movement Modal
- [ ] Add "Move Material" button in toolbar
- [ ] Modal HTML structure:
  - [ ] Title: "Move Material"
  - [ ] Form fields:
    - [ ] SKU Name: text input (required, pre-filled if opened from row action)
    - [ ] From Location: select dropdown (rm_store / pm_store / production_floor / fg_store / offgrade) — required
    - [ ] To Location: select dropdown (same options) — required
    - [ ] Quantity (kg): number input (required, > 0)
    - [ ] Entity: select (CFPL/CDPL) — pre-filled from page entity
    - [ ] Reason: select dropdown (production / return / receipt / dispatch / other)
    - [ ] Job Card ID: number input (optional, for linking)
    - [ ] Moved By: text input (pre-fill from localStorage)
  - [ ] Buttons: Cancel | Move
- [ ] Write `showMoveModal(prefill?)` function:
  - [ ] If opened from row action: pre-fill SKU, from_location, entity
  - [ ] Show modal with fade-in
- [ ] Write `submitMove()` async function:
  - [ ] Validate required fields
  - [ ] Validate transition is allowed (client-side check before API):
    - [ ] Only allow: rm_store→production_floor, pm_store→production_floor, production_floor→fg_store, production_floor→offgrade, production_floor→rm_store, offgrade→production_floor
    - [ ] Show inline error for invalid transition: "Cannot move from {X} to {Y}"
  - [ ] Call `POST /api/v1/production/floor-inventory/move`
  - [ ] Body: `{sku_name, from_location, to_location, quantity_kg, entity, reason, job_card_id, moved_by}`
  - [ ] Handle errors:
    - [ ] `invalid_transition`: "This movement path is not allowed"
    - [ ] `insufficient_stock`: "Not enough stock. Available: X kg"
  - [ ] On success: toast "Moved {qty} kg of {sku} from {from} to {to}"
  - [ ] Close modal, reload summary + inventory table
- [ ] Dynamic "To Location" options based on selected "From Location":
  - [ ] rm_store → only show production_floor
  - [ ] pm_store → only show production_floor
  - [ ] production_floor → show fg_store, offgrade, rm_store
  - [ ] offgrade → only show production_floor
- [ ] Show available stock for selected SKU + from_location (quick inline query or from loaded data)
- [ ] Style: modal consistent with existing patterns
- [ ] Test: modal opens with/without pre-fill
- [ ] Test: to_location options filter based on from_location
- [ ] Test: valid movement succeeds
- [ ] Test: invalid transition shows error
- [ ] Test: insufficient stock shows error
- [ ] Test: summary and table reload after move

#### 1D — Idle Material Check
- [ ] "Check Idle Materials" button in toolbar (secondary style)
- [ ] Write `checkIdleMaterials()` async function:
  - [ ] Show confirmation: "Check for idle materials? This will create alerts for items idle > 3 days."
  - [ ] Call `POST /api/v1/production/floor-inventory/check-idle?entity=X`
  - [ ] Show results toast: "Checked {total} items — {warnings} warnings, {criticals} critical, {skipped} allocated"
  - [ ] If warnings + criticals > 0: suggest navigating to Alerts page
- [ ] Show loading state on button during check
- [ ] Test: idle check runs and returns counts
- [ ] Test: alerts created (verifiable on alerts page)

---

### Page 2: Movement History

#### 2A — Movement Audit Trail
- [ ] Create folder `Desktop/src/modules/production/movements/`
- [ ] Create `movements/index.html` with standard layout
- [ ] Header: breadcrumbs (Production > Movement History), entity selector
- [ ] Toolbar with advanced filters:
  - [ ] SKU Name: text input (search)
  - [ ] From Location: select dropdown (All + 4 locations + offgrade)
  - [ ] To Location: select dropdown (same)
  - [ ] Date From: date input
  - [ ] Date To: date input
  - [ ] Job Card ID: number input (optional)
  - [ ] "Clear Filters" button
- [ ] Create `movements/movements.js`
- [ ] Write `loadMovements()` async function:
  - [ ] Call `GET /api/v1/production/floor-inventory/movements?entity=X&sku_name=X&from_location=X&to_location=X&date_from=X&date_to=X&job_card_id=X&page=X&page_size=50`
  - [ ] Handle loading/error
  - [ ] Call `renderTable()` and `renderPagination()`
- [ ] Write `renderTable(results)` function:
  - [ ] Columns: Movement ID | SKU | From | To | Qty (kg) | Reason | Job Card | Moved By | QR Codes | Timestamp
  - [ ] Movement ID in monospace
  - [ ] From/To as location badges (colored per location)
  - [ ] Qty formatted to 1 decimal
  - [ ] Reason badge: production (blue), return (amber), receipt (green), dispatch (purple)
  - [ ] Job Card: clickable link → navigate to job-card-detail (if present, else "—")
  - [ ] QR Codes: count badge "3 boxes" (expandable to see list)
  - [ ] Moved By: name or "System"
  - [ ] Timestamp: relative time + absolute on hover
  - [ ] Empty state: "No movements found"
- [ ] Write `renderPagination()` — standard pattern
- [ ] Wire all filters to reload with page=1
- [ ] Create `movements/styles.css`:
  - [ ] Location badges with per-location colors
  - [ ] Reason badges
  - [ ] QR codes expandable chip
  - [ ] Compact table layout for many columns
- [ ] Test: movements load with default entity
- [ ] Test: SKU filter works
- [ ] Test: location filters work
- [ ] Test: date range filters work
- [ ] Test: job card filter works
- [ ] Test: job card ID clickable (navigates)
- [ ] Test: pagination works
- [ ] Test: empty state renders

---

### Page 3: Off-Grade Management

#### 3A — Off-Grade Inventory List
- [ ] Create folder `Desktop/src/modules/production/offgrade/`
- [ ] Create `offgrade/index.html` with standard layout
- [ ] Header: breadcrumbs (Production > Off-Grade), entity selector
- [ ] Tab navigation: "Inventory" (active) | "Reuse Rules"
- [ ] Toolbar (Inventory tab):
  - [ ] Status filter: All / Available / Reserved / Consumed / Expired
  - [ ] Item Group filter: All / Cashew / Almond / Dates / Seeds / Raisin
  - [ ] Search: SKU/product name
- [ ] Create `offgrade/offgrade.js`
- [ ] Write `loadOffgradeInventory()` async function:
  - [ ] Call `GET /api/v1/production/offgrade/inventory?entity=X&status=X&item_group=X&page=X&page_size=50`
  - [ ] Handle loading/error
  - [ ] Call `renderInventoryTable()` and `renderPagination()`
- [ ] Write `renderInventoryTable(results)` function:
  - [ ] Columns: Source Product | Item Group | Category | Grade | Available Qty (kg) | Production Date | Expiry Date | Job Card | Status
  - [ ] Item Group badge (colored per group)
  - [ ] Category text (broken, undersized, discolored)
  - [ ] Grade badge: A (green), B (amber), C (red)
  - [ ] Qty formatted to 1 decimal
  - [ ] Production/Expiry dates: formatted DD/MM/YY, expiry highlighted red if past/near
  - [ ] Job Card: clickable link to job-card-detail
  - [ ] Status badge: available (green), reserved (blue), consumed (grey), expired (red)
  - [ ] Empty state: "No off-grade inventory"
- [ ] Wire filters + entity selector + pagination
- [ ] Test: inventory loads with filters
- [ ] Test: status filter works
- [ ] Test: item group filter works
- [ ] Test: pagination works

#### 3B — Reuse Rules Tab
- [ ] Tab 2: "Reuse Rules" content area
- [ ] Write `loadRules()` async function:
  - [ ] Call `GET /api/v1/production/offgrade/rules`
  - [ ] Render rules table
- [ ] Write `renderRulesTable(rules)` function:
  - [ ] Columns: Source Group | Target Group | Max Substitution % | Active | Notes | Actions
  - [ ] Source/Target as item group badges
  - [ ] Max %: formatted with "%" suffix
  - [ ] Active: green check or red X toggle
  - [ ] Notes: truncated with tooltip
  - [ ] Actions: Edit button
  - [ ] Empty state: "No reuse rules configured"
- [ ] "Add Rule" button above table:
  - [ ] Opens modal with form:
    - [ ] Source Item Group: select (cashew/almond/dates/seeds/raisin)
    - [ ] Target Item Group: select (same options)
    - [ ] Max Substitution %: number input (required, 0-100)
    - [ ] Notes: textarea (optional)
  - [ ] On submit: `POST /api/v1/production/offgrade/rules/create`
  - [ ] Body: `{source_item_group, target_item_group, max_substitution_pct, notes}`
  - [ ] Toast: "Rule created" or "Rule updated" (UPSERT)
  - [ ] Reload rules
- [ ] Edit rule (click row or edit button):
  - [ ] Opens same modal pre-filled with current values
  - [ ] Additional field: Active toggle (checkbox)
  - [ ] On submit: `PUT /api/v1/production/offgrade/rules/{rule_id}`
  - [ ] Body: `{max_substitution_pct, is_active, notes}` (only changed fields)
  - [ ] Toast: "Rule updated"
  - [ ] Reload rules
- [ ] Style: rules table, add/edit modal, group badges
- [ ] Test: rules list loads
- [ ] Test: add rule creates new
- [ ] Test: add existing source+target pair updates (UPSERT)
- [ ] Test: edit rule updates
- [ ] Test: toggle active/inactive works
- [ ] Test: validation blocks % > 100 or negative

#### 3C — Off-Grade Summary Cards
- [ ] Summary row above inventory table:
  - [ ] Card 1: "Available" — total available kg across all groups
  - [ ] Card 2: "Reserved" — total reserved kg
  - [ ] Card 3: "Consumed" — total consumed kg
  - [ ] Card 4: "Item Groups" — distinct group count
- [ ] Compute from loaded data or separate aggregation
- [ ] Test: summary shows correct totals

---

### Page 4: Loss & Yield Analytics

#### 4A — Loss Analysis View
- [ ] Create folder `Desktop/src/modules/production/loss-analytics/`
- [ ] Create `loss-analytics/index.html` with standard layout
- [ ] Header: breadcrumbs (Production > Loss & Yield Analytics), entity selector
- [ ] Tab navigation: "Loss Analysis" (active) | "Anomalies" | "Yield Summary"
- [ ] Toolbar (Loss Analysis tab):
  - [ ] Group By: select (Product / Stage / Month / Machine)
  - [ ] Product filter: text input (search)
  - [ ] Stage filter: select (All / Sorting / Weighing / Sealing / Metal Detection / Roasting / Packaging)
  - [ ] Date From: date input
  - [ ] Date To: date input
- [ ] Create `loss-analytics/loss-analytics.js`
- [ ] Write `loadLossAnalysis()` async function:
  - [ ] Call `GET /api/v1/production/loss/analysis?entity=X&group_by=X&product_name=X&stage=X&date_from=X&date_to=X`
  - [ ] Call `renderLossTable()`
- [ ] Write `renderLossTable(data)` function:
  - [ ] Columns: Group Key | Batch Count | Avg Loss % | Total Loss (kg) | Min Loss % | Max Loss %
  - [ ] Group Key: product name / stage name / month / machine name (depends on group_by)
  - [ ] Avg Loss %: formatted to 2 decimals, color-coded (green < 2%, amber 2-5%, red > 5%)
  - [ ] Total Loss: formatted to 1 decimal with "kg" suffix
  - [ ] Min/Max range displayed as inline bar or text
  - [ ] Sort: by total_loss_kg descending (server-side)
  - [ ] Empty state: "No loss data for selected filters"
- [ ] Summary cards above table:
  - [ ] Total Batches | Overall Avg Loss % | Total Loss (kg) | Highest Loss Product
- [ ] Wire all filters to reload
- [ ] Test: loss analysis loads with group_by product
- [ ] Test: switching group_by changes columns
- [ ] Test: product and stage filters work
- [ ] Test: date range works

#### 4B — Anomaly Detection View
- [ ] Tab 2: "Anomalies" content area
- [ ] Toolbar:
  - [ ] Threshold Multiplier: number input (default 2.0, range 1.0-5.0)
  - [ ] Entity selector (inherited from page)
- [ ] Write `loadAnomalies()` async function:
  - [ ] Call `GET /api/v1/production/loss/anomalies?entity=X&threshold_multiplier=X`
  - [ ] Call `renderAnomalies()`
- [ ] Write `renderAnomalies(data)` function:
  - [ ] Each anomaly as a card or table row:
    - [ ] Product Name | Stage | Batch # | Loss % | Avg Loss % | Std Dev | Deviation
    - [ ] Deviation = (loss_pct - avg_pct) formatted as "+X.X%" in red
    - [ ] Highlight severity: higher deviation = darker red background
  - [ ] Sort by deviation descending (server-side)
  - [ ] Card format: product+stage header, batch details, visual deviation bar
  - [ ] Empty state: "No anomalies detected at {X}x threshold"
- [ ] Summary: "X anomalies found across Y products"
- [ ] Adjusting threshold reloads in real-time (debounced)
- [ ] Style: anomaly cards with severity gradient, deviation visualization
- [ ] Test: anomalies load with default threshold
- [ ] Test: adjusting threshold changes results
- [ ] Test: empty state when no anomalies

#### 4C — Yield Summary View
- [ ] Tab 3: "Yield Summary" content area
- [ ] Toolbar:
  - [ ] Product filter: text input
  - [ ] Period filter: text input or dropdown (e.g., "2026-03", "2026-W12")
- [ ] Write `loadYieldSummary()` async function:
  - [ ] Call `GET /api/v1/production/yield/summary?entity=X&product_name=X&period=X`
  - [ ] Call `renderYield()`
- [ ] Write `renderYield(data)` function:
  - [ ] Table columns: Product | Item Group | Period | Input (kg) | Output (kg) | Yield % | Loss (kg) | Off-grade (kg) | Computed At
  - [ ] Yield %: color-coded (green > 90%, amber 80-90%, red < 80%)
  - [ ] Input/Output/Loss formatted to 1 decimal
  - [ ] Period displayed as-is (month or week format)
  - [ ] Sort: by period descending, then product
  - [ ] Empty state: "No yield data available"
- [ ] Summary cards:
  - [ ] Overall Yield % | Total Input (kg) | Total Output (kg) | Total Loss (kg) | Total Off-grade (kg)
- [ ] Wire filters to reload
- [ ] Style: yield table with color-coded yield %, summary cards
- [ ] Test: yield data loads
- [ ] Test: product filter works
- [ ] Test: period filter works
- [ ] Test: yield % coloring correct

---

### Navigation & Sidebar Updates

#### Main Process (main.js)
- [ ] Add page config for `'floor-inventory'`: path + dimensions
- [ ] Add page config for `'movements'`: path + dimensions
- [ ] Add page config for `'offgrade'`: path + dimensions
- [ ] Add page config for `'loss-analytics'`: path + dimensions
- [ ] Add IPC handler: `navigate-to-floor-inventory`
- [ ] Add IPC handler: `navigate-to-movements`
- [ ] Add IPC handler: `navigate-to-offgrade`
- [ ] Add IPC handler: `navigate-to-loss-analytics`
- [ ] Add to `PAGE_LABELS` in `navigation.js`: all 4 new pages
- [ ] Test: all navigation routes work
- [ ] Test: back button works from all pages

#### Sidebar Updates (all production pages)
- [ ] Add nav items to sidebar in ALL production page HTML files:
  - [ ] "Floor Inventory" (with warehouse/stock icon)
  - [ ] "Movement History" (with arrows icon)
  - [ ] "Off-Grade" (with recycle icon)
  - [ ] "Loss & Yield" (with chart icon)
- [ ] Group under "Inventory & Tracking" section label in sidebar
- [ ] Wire click handlers
- [ ] Active state on correct page
- [ ] Test: nav items visible on all pages
- [ ] Test: active states correct

---

### Cross-Cutting Concerns

#### Consistent Styling
- [ ] All new pages use standard layout pattern
- [ ] All tables use `.po-table` / `.po-row` class pattern
- [ ] All modals use overlay + centered card pattern
- [ ] Floor location colors standardized:
  - [ ] rm_store = amber (`--clr-warning`)
  - [ ] pm_store = blue (`--clr-info`)
  - [ ] production_floor = purple (`#7c3aed`)
  - [ ] fg_store = green (`--clr-ok`)
  - [ ] offgrade = red-tinted (`--clr-mismatch`)
- [ ] Item type badge colors: RM=amber, PM=blue, WIP=purple, FG=green
- [ ] All quantity formatting: 1 decimal, "kg" suffix
- [ ] Tab navigation styles consistent (pill buttons or underline tabs)

#### Error Handling
- [ ] All API calls wrapped in try/catch
- [ ] All errors show toast
- [ ] Move errors: clear messages for invalid_transition + insufficient_stock
- [ ] Loading states on all API calls
- [ ] Loading cleared on success + error

#### Data Flow Integration
- [ ] Floor Inventory row "Move" → opens move modal pre-filled
- [ ] Movement History → Job Card ID clickable → navigate to job-card-detail
- [ ] Off-Grade Inventory → Job Card clickable → navigate to job-card-detail
- [ ] Loss Analysis → drill down from group to individual batches (future enhancement)
- [ ] Idle check → alerts created → visible on Alerts page
- [ ] Summary cards on floor inventory update after every move

---

## Files to Create/Modify

| File | Action | Changes |
|------|--------|---------|
| `Desktop/src/modules/production/floor-inventory/index.html` | **Create** | Floor inventory page with summary + table + move modal |
| `Desktop/src/modules/production/floor-inventory/floor-inventory.js` | **Create** | Summary, list, move, idle check logic |
| `Desktop/src/modules/production/floor-inventory/styles.css` | **Create** | Floor inventory styles |
| `Desktop/src/modules/production/movements/index.html` | **Create** | Movement history page |
| `Desktop/src/modules/production/movements/movements.js` | **Create** | Movement list, filters |
| `Desktop/src/modules/production/movements/styles.css` | **Create** | Movement history styles |
| `Desktop/src/modules/production/offgrade/index.html` | **Create** | Off-grade page with tabs (inventory + rules) |
| `Desktop/src/modules/production/offgrade/offgrade.js` | **Create** | Inventory list, rules CRUD, add/edit modal |
| `Desktop/src/modules/production/offgrade/styles.css` | **Create** | Off-grade styles |
| `Desktop/src/modules/production/loss-analytics/index.html` | **Create** | Loss & yield page with 3 tabs |
| `Desktop/src/modules/production/loss-analytics/loss-analytics.js` | **Create** | Loss analysis, anomalies, yield summary |
| `Desktop/src/modules/production/loss-analytics/styles.css` | **Create** | Analytics styles |
| `Desktop/main.js` | Modify | 4 page configs, 4 IPC handlers |
| `Desktop/src/shared/js/navigation.js` | Modify | Add PAGE_LABELS for 4 new pages |
| All production page HTML files (10+) | Modify | Add sidebar nav items under "Inventory & Tracking" group |

---

## Verification Plan

1. **Floor Summary:** Load page → verify 4 cards show correct counts + totals per floor location
2. **Floor Inventory List:** Filter by location, search by SKU → verify correct items shown
3. **Idle Indicators:** Items with last_updated > 3d show warning, > 5d show critical icon
4. **Manual Move:** Open move modal → select valid transition → verify stock debited/credited
5. **Invalid Move:** Attempt invalid transition (e.g., fg_store→rm_store) → verify error message
6. **Insufficient Stock:** Move more than available → verify "insufficient stock" error
7. **Idle Check:** Trigger check → verify alert counts → verify alerts on Alerts page
8. **Movement History:** Load movements → filter by SKU, date, location, job card → verify results
9. **Off-Grade Inventory:** Filter by status, item group → verify correct off-grade items shown
10. **Reuse Rules:** Add new rule → verify in list → edit max % → verify updated → toggle active/inactive
11. **Loss Analysis by Product:** Group by product → verify avg/total/min/max per product
12. **Loss Analysis by Stage:** Switch group_by to stage → verify regrouped data
13. **Loss Anomalies:** Set threshold 2x → verify anomalous batches flagged → adjust to 3x → fewer results
14. **Yield Summary:** Filter by product/period → verify input/output/yield%/loss columns
15. **Navigation:** Test all 4 new routes, back button, sidebar items, cross-page links
