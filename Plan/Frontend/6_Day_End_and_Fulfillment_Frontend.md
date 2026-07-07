# Part 6: Day-End & Fulfillment — Frontend Implementation Plan

---

## Context

The backend for Part 6 (Day-End & Fulfillment) is **fully implemented** with 8 new API endpoints across 3 areas: Day-End Dispatch, Balance Scan Workflow, and FY Transition (cancel). **No frontend pages exist** for any day-end or balance-scan features. The fulfillment cancel endpoint is also not wired to the existing fulfillment page. This plan creates 2 new pages (Day-End Dashboard, Balance Scan) and enhances the existing Fulfillment page.

**Tech stack:** Electron desktop app, vanilla JS/HTML/CSS, IPC-based navigation, fetch API, CSS custom properties from `variables.css`.

**Day-End Dispatch Flow:**
```
Completed final-stage job cards
  → Manager reviews FG output summary
  → Manager enters dispatch_qty per job card
  → Bulk submit: updates job_card_output, so_fulfillment, floor_inventory
  → Creates floor_movement (fg_store → dispatched)
```

**Balance Scan Flow:**
```
4 mandatory floors (rm_store, pm_store, production_floor, fg_store)
  → Store team performs physical count per floor
  → Submit scan: system compares scanned vs system qty
  → Variances > 2% flagged → alerts created
  → Manager reviews + reconciles → floor_inventory adjusted
```

**Balance Scan Status Flow:**
```
pending → submitted → variance_flagged → reconciled
                  ↘ (if no variances) → submitted (done)
```

---

## Gap Analysis: Backend Endpoints vs Frontend Coverage

| # | Endpoint | Description | Frontend Status |
|---|----------|-------------|-----------------|
| 62 | `GET /day-end/summary` | Completed orders + totals for today | ❌ **NOT BUILT** |
| 63 | `PUT /day-end/dispatch` | Bulk dispatch qty update | ❌ **NOT BUILT** |
| 64 | `POST /balance-scan/submit` | Submit physical count for a floor | ❌ **NOT BUILT** |
| 65 | `GET /balance-scan/status` | Per-floor scan status for today | ❌ **NOT BUILT** |
| 66 | `GET /balance-scan/{id}` | Scan detail with all lines | ❌ **NOT BUILT** |
| 67 | `PUT /balance-scan/{id}/reconcile` | Approve adjustment, fix inventory | ❌ **NOT BUILT** |
| 68 | `POST /balance-scan/check-missing` | Alert for floors that haven't scanned | ❌ **NOT BUILT** |
| 69 | `POST /fulfillment/cancel` | Cancel fulfillment records with reason | ❌ **NOT BUILT** |

---

## Implementation Plan

### Page 1: Day-End Dashboard (NEW)
**Path:** `Desktop/src/modules/production/day-end/`
**Nav route:** `navigate-to-day-end`

Central page for end-of-day operations: review completed production, enter dispatch quantities, check balance scan status, and trigger missing-scan alerts.

### Page 2: Balance Scan Page (NEW)
**Path:** `Desktop/src/modules/production/balance-scan/`
**Nav route:** `navigate-to-balance-scan`

Floor-by-floor physical count submission, variance review, and reconciliation workflow.

### Fulfillment Page Enhancement
**Path:** `Desktop/src/modules/production/fulfillment/` (existing)

Add cancel workflow to existing fulfillment page.

---

## Detailed Checklist

### Page 1: Day-End Dashboard

#### 1A — Day-End Summary View
- [ ] Create folder `Desktop/src/modules/production/day-end/`
- [ ] Create `day-end/index.html` with standard layout (titlebar, sidebar, content)
- [ ] Header: breadcrumbs (Production > Day-End), entity selector (CFPL/CDPL — required, no "All"), date picker (defaults to today)
- [ ] Summary cards row (4 cards):
  - [ ] Card 1: "Completed Orders" — count of completed final-stage job cards (green accent)
  - [ ] Card 2: "FG Output" — total fg_actual_kg (blue accent)
  - [ ] Card 3: "Dispatched" — total dispatch_kg (purple accent)
  - [ ] Card 4: "Loss + Off-grade" — total process_loss_kg + offgrade_kg (red accent)
- [ ] Create `day-end/day-end.js`
- [ ] Write `loadDayEndSummary()` async function:
  - [ ] Validate entity is selected (required)
  - [ ] Call `GET /api/v1/production/day-end/summary?entity=X&target_date=Y`
  - [ ] Populate summary cards from response totals
  - [ ] Call `renderDispatchTable(items)`
  - [ ] Handle loading/error states
- [ ] Wire entity selector to reload (require selection, no "All" option)
- [ ] Wire date picker `change` to reload
- [ ] Style summary cards: 4-column grid, large value text, colored top borders
- [ ] Test: summary loads for selected entity + today
- [ ] Test: changing date loads different day's data
- [ ] Test: entity is required (show prompt if not selected)

#### 1B — Dispatch Entry Table
- [ ] Table below summary cards for entering dispatch quantities:
  - [ ] Columns: JC # | FG SKU | Customer | Batch # | Batch Size (kg) | FG Actual (kg) | Loss (kg) | Off-grade (kg) | Dispatch Qty | Status
  - [ ] JC # in monospace
  - [ ] FG Actual: read-only, from job_card_output
  - [ ] Loss + Off-grade: read-only, muted text
  - [ ] **Dispatch Qty: editable number input** (the key editable column)
  - [ ] Status badge: completed (green), closed (grey)
  - [ ] Pre-fill dispatch_qty from existing value (if previously saved)
  - [ ] Empty state: "No completed orders for this date"
- [ ] Write `renderDispatchTable(items)` function:
  - [ ] Render each item as a table row with editable dispatch_qty input
  - [ ] Default dispatch_qty to fg_actual_kg if not already set (suggest full dispatch)
  - [ ] Highlight row if dispatch_qty differs from fg_actual_kg (partial dispatch)
  - [ ] Running total row at bottom: sum of all dispatch_qty values
- [ ] Input validation per row:
  - [ ] dispatch_qty must be >= 0
  - [ ] dispatch_qty should not exceed fg_actual_kg (warn, don't block)
  - [ ] Show warning icon if dispatch_qty > fg_actual_kg
- [ ] "Save All Dispatches" button below table:
  - [ ] Collect all rows with dispatch_qty values
  - [ ] Build request: `{dispatches: [{job_card_id, dispatch_qty}, ...], entity}`
  - [ ] Call `PUT /api/v1/production/day-end/dispatch`
  - [ ] Show toast: "Updated dispatch for X orders"
  - [ ] Reload summary + table
- [ ] Show loading state on save button
- [ ] Confirmation dialog before saving: "Update dispatch quantities for X orders?"
- [ ] Style: editable input cells with subtle background, running total row bold
- [ ] Test: dispatch table renders with correct data
- [ ] Test: editing dispatch_qty updates running total
- [ ] Test: save bulk dispatch works
- [ ] Test: reload shows updated values
- [ ] Test: warning for dispatch > actual output
- [ ] Test: empty state when no completed orders

#### 1C — Balance Scan Status Widget
- [ ] Section below dispatch table: "Balance Scan Status"
- [ ] 4 floor status cards (one per required floor):
  - [ ] Floor name: RM Store | PM Store | Production Floor | FG Store
  - [ ] Status indicator:
    - [ ] Pending: grey dot + "Not submitted" text
    - [ ] Submitted: green dot + "Submitted by {name}" + time
    - [ ] Variance Flagged: orange dot + "Variances detected" + variance total
    - [ ] Reconciled: blue dot + "Reconciled by {name}" + time
  - [ ] Clickable: navigate to balance-scan page filtered to that floor
- [ ] Write `loadScanStatus()` async function:
  - [ ] Call `GET /api/v1/production/balance-scan/status?entity=X&target_date=Y`
  - [ ] Render 4 floor cards from response array
- [ ] "Check Missing Scans" button:
  - [ ] Call `POST /api/v1/production/balance-scan/check-missing?entity=X&target_date=Y`
  - [ ] Show toast: "X floors missing — Y alerts created"
  - [ ] Reload scan status
- [ ] "Go to Balance Scan" link → navigate to balance-scan page
- [ ] Style: 4-column grid of floor cards, status dot colors, clickable card hover effect
- [ ] Test: scan status loads for all 4 floors
- [ ] Test: correct status per floor
- [ ] Test: check missing creates alerts
- [ ] Test: clicking floor card navigates to balance scan

#### 1D — Quick Fulfillment Summary
- [ ] Small section at bottom: "Fulfillment Update"
- [ ] After dispatch save, show which fulfillment records were updated:
  - [ ] List: SKU | Customer | Dispatched Today | Total Dispatched | Pending | Status
  - [ ] Updated items highlighted with green flash animation
- [ ] "View All Fulfillment" link → navigate to fulfillment page
- [ ] This section is informational only (data comes from dispatch response)
- [ ] Test: fulfillment summary shows after dispatch
- [ ] Test: navigation link works

---

### Page 2: Balance Scan

#### 2A — Floor Selection & Scan Status
- [ ] Create folder `Desktop/src/modules/production/balance-scan/`
- [ ] Create `balance-scan/index.html` with standard layout
- [ ] Header: breadcrumbs (Production > Balance Scan), entity selector (required), date picker (defaults to today)
- [ ] Floor selector: 4 clickable cards arranged horizontally
  - [ ] RM Store | PM Store | Production Floor | FG Store
  - [ ] Each card shows current status (pending/submitted/variance_flagged/reconciled)
  - [ ] Active floor highlighted
  - [ ] Click to select floor and load scan interface
- [ ] Create `balance-scan/balance-scan.js`
- [ ] Write `loadScanStatus()` async:
  - [ ] Call `GET /api/v1/production/balance-scan/status?entity=X&target_date=Y`
  - [ ] Render floor cards with status
  - [ ] If coming from day-end page with floor param: auto-select that floor
- [ ] Style: horizontal card selector, status indicators, active card border
- [ ] Test: 4 floor cards render with correct status
- [ ] Test: clicking floor selects it

#### 2B — Scan Submission Form (for pending floors)
- [ ] When a pending/not-yet-submitted floor is selected, show scan entry form:
  - [ ] Title: "Physical Count — {Floor Name}"
  - [ ] Submitted By: text input (required, pre-fill from localStorage)
  - [ ] Scan lines table (editable):
    - [ ] Pre-populate with all SKUs currently in `floor_inventory` for this floor+entity
    - [ ] Columns: SKU Name | Item Type | System Qty (kg) | Scanned Qty (kg) | Box IDs | Variance Reason
    - [ ] System Qty: read-only, fetched from inventory
    - [ ] Scanned Qty: number input (required per row)
    - [ ] Box IDs: text input (comma-separated, optional)
    - [ ] Variance Reason: text input (optional, prompted if large variance)
  - [ ] "Add Item" button for items found physically but not in system
    - [ ] New row: SKU Name input + Item Type select + Scanned Qty (system qty = 0)
  - [ ] "Submit Scan" button
- [ ] Write `loadFloorInventoryForScan(floor, entity)` async:
  - [ ] Call `GET /api/v1/production/floor-inventory?entity=X&floor_location=X&page_size=200`
  - [ ] Pre-populate scan table rows with SKU names + system quantities
- [ ] Write `submitScan()` async function:
  - [ ] Validate: submitted_by not empty
  - [ ] Validate: all rows have scanned_qty >= 0
  - [ ] Build request body:
    ```json
    {
      "floor_location": "rm_store",
      "entity": "cfpl",
      "submitted_by": "Store Team A",
      "scan_lines": [
        {"sku_name": "...", "item_type": "rm", "scanned_qty_kg": 100.5, "scanned_box_ids": ["B001"], "variance_reason": "..."}
      ]
    }
    ```
  - [ ] Call `POST /api/v1/production/balance-scan/submit`
  - [ ] Handle response:
    - [ ] If status='submitted' (no variances): toast "Scan submitted — no variances"
    - [ ] If status='variance_flagged': toast "Scan submitted — {variance_flags} variances detected!"
  - [ ] Reload floor status cards
  - [ ] Switch to scan detail view for this floor
- [ ] Auto-calculate inline variance as user types: `variance = scanned - system`
  - [ ] Color: green if 0 or within 2%, amber if 2-5%, red if > 5%
  - [ ] Show variance_pct next to variance_kg
- [ ] Prompt for variance_reason if |variance_pct| > 2% and reason is empty
- [ ] Show loading state on submit button
- [ ] Style: editable table with inline calculation, variance coloring
- [ ] Test: scan form pre-populates from floor inventory
- [ ] Test: variance calculates as user types
- [ ] Test: submit works for floor with no variances
- [ ] Test: submit works for floor with variances (flags them)
- [ ] Test: add new item row works
- [ ] Test: validation blocks empty submitted_by

#### 2C — Scan Detail View (for submitted/flagged floors)
- [ ] When a submitted/variance_flagged floor is selected, show scan detail:
  - [ ] Header: "Scan — {Floor Name}" + Status badge + Submitted by + Submitted at
  - [ ] Totals row: System Total | Scanned Total | Variance Total
  - [ ] Detail table:
    - [ ] Columns: SKU | Item Type | System Qty | Scanned Qty | Variance (kg) | Variance % | Box IDs | Reason | Line Status
    - [ ] System Qty: read-only
    - [ ] Scanned Qty: read-only
    - [ ] Variance: colored (green=ok, red=variance_detected)
    - [ ] Variance %: colored same
    - [ ] Line Status badge: ok (green), variance_detected (red), reconciled (blue)
    - [ ] Rows with variance_detected highlighted with red-tinted background
  - [ ] Sort: variance_detected rows first, then by |variance_kg| descending
- [ ] Write `loadScanDetail(scanId)` async:
  - [ ] Call `GET /api/v1/production/balance-scan/{scanId}`
  - [ ] Render header + totals + detail table
- [ ] Get scan_id from floor status response (stored when loading status)
- [ ] Style: detail table with variance coloring, total row bold
- [ ] Test: scan detail loads correctly
- [ ] Test: variance rows highlighted
- [ ] Test: totals calculate correctly

#### 2D — Reconciliation Workflow
- [ ] "Reconcile" button (shown only for variance_flagged or submitted scans):
  - [ ] Reviewed By: text input (required)
  - [ ] Confirmation dialog: "Reconcile scan for {floor}? This will adjust floor inventory to match physical count."
  - [ ] Call `PUT /api/v1/production/balance-scan/{scanId}/reconcile`
  - [ ] Body: `{reviewed_by: "..."}`
  - [ ] Handle response:
    - [ ] Toast: "Reconciled — {adjustments} items adjusted"
    - [ ] Reload scan detail (status now 'reconciled')
    - [ ] Reload floor status cards
- [ ] "Reconcile" button disabled if already reconciled
- [ ] Show reviewed_by + reviewed_at after reconciliation
- [ ] Pre-fill reviewed_by from localStorage
- [ ] Show loading state on button
- [ ] Style: reconcile button prominent (blue/purple), disabled state for reconciled
- [ ] Test: reconcile adjusts inventory
- [ ] Test: scan status updates to 'reconciled'
- [ ] Test: reconcile button disabled after reconciliation
- [ ] Test: reviewed_by stored and displayed

#### 2E — Re-submit Scan
- [ ] For already-submitted floors: show "Re-submit" option
  - [ ] API supports UPSERT (ON CONFLICT DO UPDATE) so re-submission replaces previous scan
  - [ ] Show current scan data as starting point
  - [ ] Allow editing scanned quantities
  - [ ] Submit overwrites previous scan
- [ ] Test: re-submit replaces previous scan data
- [ ] Test: variance flags re-calculated

---

### Fulfillment Page Enhancement

#### 3A — Cancel Workflow
- [ ] Add multi-select checkboxes to fulfillment table rows (for open/partial orders)
- [ ] Add selection bar at bottom: "X orders selected" + "Cancel Selected" button (red)
- [ ] Write `cancelFulfillments()` async function:
  - [ ] Validate: at least 1 selected
  - [ ] Show modal with:
    - [ ] Reason textarea (required): "Why are you cancelling these orders?"
    - [ ] Cancelled By: text input (pre-fill from localStorage)
    - [ ] Buttons: "Go Back" | "Cancel Orders" (red)
  - [ ] Call `POST /api/v1/production/fulfillment/cancel`
  - [ ] Body: `{fulfillment_ids: [...], reason: "...", cancelled_by: "..."}`
  - [ ] Show toast: "Cancelled X of Y orders"
  - [ ] Clear selection
  - [ ] Reload table + summary
- [ ] Disable "Cancel Selected" when 0 selected
- [ ] Only show checkboxes for open/partial status rows (not fulfilled/carryforward/cancelled)
- [ ] Confirmation: second step in modal — "This action cannot be undone. Cancel X orders?"
- [ ] Show loading state on cancel button
- [ ] Style: selection bar with red accent, cancel modal with warning styling
- [ ] Test: selecting orders updates count
- [ ] Test: cancel with reason works
- [ ] Test: cancelled orders show 'cancelled' status in table
- [ ] Test: only open/partial rows have checkboxes
- [ ] Test: validation requires reason
- [ ] Test: already cancelled rows excluded from selection

#### 3B — Cancel Button Per Row
- [ ] Alternative to bulk: add cancel icon button in Actions column (per row, for open/partial only)
- [ ] On click: show same cancel modal but for single order
- [ ] Pre-fill modal with SKU + customer name for context
- [ ] Call same `POST /fulfillment/cancel` with single-item array
- [ ] Test: single cancel works
- [ ] Test: icon hidden for fulfilled/carryforward/cancelled rows

---

### Navigation & Sidebar Updates

#### Main Process (main.js)
- [ ] Add page config for `'day-end'`:
  - [ ] File path: `src/modules/production/day-end/index.html`
  - [ ] Window dimensions: match existing production pages
- [ ] Add page config for `'balance-scan'`:
  - [ ] File path: `src/modules/production/balance-scan/index.html`
  - [ ] Window dimensions: match existing production pages
- [ ] Add IPC handler: `navigate-to-day-end` (push nav stack, load page)
- [ ] Add IPC handler: `navigate-to-balance-scan` (push nav stack, load page)
  - [ ] Support optional `floor` param for pre-selecting a floor
- [ ] Add IPC handle: `get-balance-scan-floor` (returns stored floor param)
- [ ] Add to `PAGE_LABELS` in `navigation.js`: `'day-end': 'Day-End'`, `'balance-scan': 'Balance Scan'`
- [ ] Test: navigation to day-end page works
- [ ] Test: navigation to balance-scan page works
- [ ] Test: navigation with floor param pre-selects floor
- [ ] Test: back button returns to previous page

#### Sidebar Updates (all production pages)
- [ ] Add nav items to sidebar in ALL production page HTML files:
  - [ ] "Day-End" (with sun/clock icon) — under "Operations" group
  - [ ] "Balance Scan" (with scanner/clipboard icon) — under "Operations" group
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
- [ ] Balance scan status colors standardized:
  - [ ] pending = grey (`--text-muted`)
  - [ ] submitted = green (`--clr-ok`)
  - [ ] variance_flagged = orange (`--clr-warning`)
  - [ ] reconciled = blue (`--clr-info`)
- [ ] Variance coloring: green (within 2%), amber (2-5%), red (>5%)
- [ ] Editable table cells: subtle input background (`--bg-input`), focus border
- [ ] All quantity formatting: 1 decimal, "kg" suffix
- [ ] Date picker consistent with planning page date inputs

#### Error Handling
- [ ] All API calls wrapped in try/catch
- [ ] All errors show toast
- [ ] Entity required for all day-end/balance-scan calls (show prompt if missing)
- [ ] Reconcile errors: "Cannot reconcile — scan not in valid status"
- [ ] Dispatch errors: clear messages per job card failure
- [ ] Loading states on all API calls
- [ ] Loading cleared on success + error

#### Data Flow Integration
- [ ] Day-End → dispatch updates → floor inventory deducted (visible on floor-inventory page)
- [ ] Day-End → dispatch updates → fulfillment dispatched_qty updated (visible on fulfillment page)
- [ ] Day-End → scan status widget → click floor → balance-scan page with floor pre-selected
- [ ] Balance Scan → reconcile → floor inventory adjusted (visible on floor-inventory page)
- [ ] Balance Scan → variance alerts → visible on alerts page
- [ ] Balance Scan → missing scan alerts → visible on alerts page
- [ ] Fulfillment page → cancel → revision log created (audit trail)

---

## Files to Create/Modify

| File | Action | Changes |
|------|--------|---------|
| `Desktop/src/modules/production/day-end/index.html` | **Create** | Day-end dashboard with dispatch table + scan status + fulfillment summary |
| `Desktop/src/modules/production/day-end/day-end.js` | **Create** | Summary, dispatch entry, scan status, missing check |
| `Desktop/src/modules/production/day-end/styles.css` | **Create** | Dispatch table editable cells, scan status cards, summary cards |
| `Desktop/src/modules/production/balance-scan/index.html` | **Create** | Balance scan with floor selector + scan form + detail + reconcile |
| `Desktop/src/modules/production/balance-scan/balance-scan.js` | **Create** | Floor selection, scan submission, detail view, reconciliation |
| `Desktop/src/modules/production/balance-scan/styles.css` | **Create** | Floor selector cards, scan form, variance coloring, reconcile button |
| `Desktop/src/modules/production/fulfillment/index.html` | Modify | Add checkboxes, selection bar, cancel modal |
| `Desktop/src/modules/production/fulfillment/fulfillment.js` | Modify | Cancel workflow, multi-select, cancel modal logic |
| `Desktop/src/modules/production/fulfillment/styles.css` | Modify | Selection bar, cancel modal, checkbox styles |
| `Desktop/main.js` | Modify | 2 page configs, 2 IPC handlers, 1 invoke handle |
| `Desktop/src/shared/js/navigation.js` | Modify | Add PAGE_LABELS for 2 new pages |
| All production page HTML files (12+) | Modify | Add sidebar nav items for Day-End + Balance Scan |

---

## Verification Plan

1. **Day-End Summary:** Select entity + date → verify 4 summary cards with correct totals
2. **Dispatch Table:** Verify completed job cards listed with editable dispatch_qty inputs
3. **Bulk Dispatch:** Enter dispatch quantities → save → verify job_card_output updated, floor inventory deducted, fulfillment dispatched_qty updated
4. **Partial Dispatch:** Enter dispatch_qty < fg_actual_kg → verify warning shown, save succeeds
5. **Scan Status Widget:** Verify 4 floor cards show correct status per floor
6. **Check Missing Scans:** Click "Check Missing" → verify alerts created for unsubmitted floors
7. **Balance Scan Submit:** Select pending floor → enter scanned quantities → submit → verify scan created with correct variances
8. **Variance Detection:** Submit scan with > 2% variance → verify status='variance_flagged', alert created
9. **No-Variance Scan:** Submit scan matching system quantities → verify status='submitted', no flags
10. **Scan Detail:** View submitted scan → verify system vs scanned comparison table with variance coloring
11. **Reconcile:** Click reconcile on flagged scan → verify floor_inventory adjusted, status='reconciled'
12. **Re-submit Scan:** Re-submit for already-scanned floor → verify UPSERT replaces previous data
13. **Fulfillment Cancel (bulk):** Select multiple open orders → cancel with reason → verify status='cancelled', audit log created
14. **Fulfillment Cancel (single):** Cancel single order via row button → verify same behavior
15. **Navigation:** Test day-end + balance-scan routes, floor param passing, back button, sidebar items
16. **Full Day-End Workflow:** Complete job cards → review summary → enter dispatch → submit → check scan status → submit balance scans for all 4 floors → reconcile variances → verify all data consistent
