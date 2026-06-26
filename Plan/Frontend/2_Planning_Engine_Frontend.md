# Part 2: Planning Engine — Frontend Implementation Plan

---

## Context

The backend for Part 2 (Planning Engine) is **fully implemented** with 17 API endpoints across 3 modules: SO Fulfillment Sync, Claude AI Plan Generation, and Plan Approve & CRUD. The Electron desktop frontend already has 4 pages built (fulfillment, planning, plan-list, plan-detail), but several backend features have **no frontend coverage**. This plan closes those gaps and polishes the existing pages to fully match the backend capabilities.

**Tech stack:** Electron desktop app, vanilla JS/HTML/CSS, IPC-based navigation, fetch API, CSS custom properties from `variables.css`.

---

## Gap Analysis: Backend Endpoints vs Frontend Coverage

| Endpoint | Frontend Status |
|----------|----------------|
| `POST /fulfillment/sync` | ✅ Built |
| `GET /fulfillment` | ✅ Built |
| `GET /fulfillment/demand-summary` | ✅ Built |
| `GET /fulfillment/fy-review` | ❌ **NOT BUILT** |
| `POST /fulfillment/carryforward` | ❌ **NOT BUILT** |
| `PUT /fulfillment/{id}/revise` | ✅ Built |
| `POST /plans/generate-daily` | ✅ Built |
| `POST /plans/generate-weekly` | ✅ Built |
| `POST /plans/create` | ❌ **NOT BUILT** (manual plan creation) |
| `GET /plans` | ✅ Built |
| `GET /plans/{id}` | ✅ Built |
| `PUT /plans/{id}/lines/{lid}` | ✅ Built |
| `POST /plans/{id}/lines` | ✅ Built |
| `DELETE /plans/{id}/lines/{lid}` | ✅ Built |
| `PUT /plans/{id}/approve` | ✅ Built |
| `PUT /plans/{id}/cancel` | ✅ Built |

**Missing filters on existing pages:**
- Fulfillment page: no Financial Year filter (API supports it)
- Fulfillment page: no FY review / carryforward workflow
- Plan list: no date range filter (API supports `date_from`, `date_to`)
- Plan detail: `approved_by` is hardcoded as "User" — should prompt for name

---

## Implementation Plan

### Module A: Fulfillment Page Enhancements
**File:** `Desktop/src/modules/production/fulfillment/`

#### A1. Add Financial Year Filter
- Add FY dropdown to toolbar (e.g., "2024-25", "2025-26", "2026-27")
- Pass `financial_year` param to `GET /fulfillment` and `GET /fulfillment/demand-summary`
- Auto-detect current FY (Apr-Mar logic) as default selection

#### A2. FY Review Section
- Add "FY Review" tab/button in toolbar area
- When active, call `GET /fulfillment/fy-review?entity=X&financial_year=Y`
- Render grouped-by-customer view of unfulfilled orders
- Show summary: total unfulfilled qty, order count per customer
- Each row shows: SKU, original qty, pending qty, deadline, status

#### A3. Carryforward Workflow
- Add multi-select checkboxes to FY Review table rows
- Selection bar at bottom: "X orders selected — Carry Forward to FY [dropdown]"
- On confirm: `POST /fulfillment/carryforward` with `{fulfillment_ids, new_fy, revised_by}`
- Show result toast: "Carried forward X orders to FY 2026-27"
- Reload table after carryforward

#### A4. Revision Audit Trail
- In the revise modal, after loading, fetch revision history for the selected fulfillment
- Display previous revisions below the form (date, type, old→new value, reason, who)
- Note: This requires a new backend endpoint or embedding revision_log in fulfillment detail (not currently exposed — may need backend addition or skip for now)

---

### Module B: Plan List Page Enhancements
**File:** `Desktop/src/modules/production/plan-list/`

#### B1. Date Range Filter
- Add date_from and date_to input fields to toolbar
- Pass to `GET /plans?date_from=X&date_to=Y`
- Clear button to reset date filters

#### B2. Manual Plan Creation Modal
- Change "New Plan" button behavior: show a dropdown or modal with two options:
  - "Generate AI Plan" → navigate to planning page (existing)
  - "Create Manual Plan" → open modal
- Manual plan modal fields: Plan Name*, Entity* (CFPL/CDPL), Plan Type (daily/weekly), Date From*, Date To*
- On submit: `POST /plans/create` with `{entity, plan_name, plan_type, date_from, date_to}`
- On success: navigate to plan-detail with new plan_id

#### B3. Approve Confirmation
- Before calling approve endpoint, show confirmation dialog (Electron `dialog.showMessageBoxSync`)
- Prompt for `approved_by` name instead of hardcoding "User"
- Same for cancel — confirm before proceeding

---

### Module C: Plan Detail Page Enhancements
**File:** `Desktop/src/modules/production/plan-detail/`

#### C1. Approve/Cancel with Name Prompt
- On approve click: prompt for approver name via small inline input or Electron dialog
- Pass to `PUT /plans/{id}/approve?approved_by=<name>`
- On cancel: show confirmation dialog before calling endpoint

#### C2. Line Edit UX Improvements
- Currently edit fields are raw inputs at bottom of expanded line
- Add inline validation: qty must be > 0, priority 1-10, shift must be "day" or "night"
- Show save confirmation if values changed
- Disable save button if no changes detected

#### C3. Machine & BOM Dropdowns
- For edit fields `machine_id` and `bom_id`: currently raw number inputs
- Fetch available machines and BOMs on page load (would need new endpoints or a lookup mechanism)
- For now: keep as numeric inputs but add helpful labels showing resolved names from plan data

---

### Module D: Planning (AI Generation) Page Enhancements
**File:** `Desktop/src/modules/production/planning/`

#### D1. Loading State & Progress
- During AI generation (can take 10-30s), show a more informative loading state
- Animated progress indicator with "Claude is analyzing your orders..." message
- Disable all interactions while generating

#### D2. Error Recovery
- If generation fails, show detailed error with retry button
- Preserve selected orders so user doesn't have to re-select

---

## Detailed Checklist

### Module A: Fulfillment Enhancements

#### A1 — Financial Year Filter
- [ ] Add `<select id="fy-filter">` dropdown to toolbar in `fulfillment/index.html`
- [ ] Populate FY options dynamically (current FY ± 2 years) in `fulfillment.js`
- [ ] Write `_getCurrentFY()` helper: if month >= 4 → `"YYYY-YY+1"`, else `"YYYY-1-YY"`
- [ ] Set default FY selection to current FY on page load
- [ ] Add "All" option to show all FYs (no filter)
- [ ] Pass `financial_year` query param in `loadTable()` fetch URL
- [ ] Pass `financial_year` query param in `loadSummary()` fetch URL
- [ ] Wire `change` event on dropdown to reset page=1 and reload table+summary
- [ ] Style the FY dropdown consistent with existing status filter (same `.filter-select` class)
- [ ] Test: changing FY filter updates both summary cards and table
- [ ] Test: "All" option returns unfiltered results

#### A2 — FY Review Section
- [ ] Add "FY Review" toggle button next to status filter in `fulfillment/index.html`
- [ ] Add `<div id="fy-review-section">` container (hidden by default) in HTML
- [ ] Add `fyReviewMode` boolean state variable in `fulfillment.js`
- [ ] On "FY Review" click: toggle `fyReviewMode`, show review section, hide main table
- [ ] Write `loadFYReview()` async function:
  - [ ] Call `GET /api/v1/production/fulfillment/fy-review?entity=X&financial_year=Y`
  - [ ] Handle loading state (show spinner)
  - [ ] Handle error state (show toast)
- [ ] Write `renderFYReview(data)` function:
  - [ ] Group results by `customer_name`
  - [ ] For each customer group: render collapsible section header with customer name + order count + total pending qty
  - [ ] Inside each group: render table rows with SKU, original_qty, pending_qty, deadline, status, priority
  - [ ] Show summary bar: total customers, total orders, total pending kg
- [ ] Add "Back to List" button in FY review section to toggle back
- [ ] Style FY review section: customer group headers, nested table, summary bar
- [ ] Add CSS for `.fy-review-section`, `.customer-group`, `.customer-group-header`
- [ ] Test: FY review loads correct data for selected entity and FY
- [ ] Test: toggling back to list view restores original table
- [ ] Test: empty state shows "No unfulfilled orders for this FY"

#### A3 — Carryforward Workflow
- [ ] Add checkboxes to each row in FY review table (column 1)
- [ ] Add "Select All" checkbox in FY review table header
- [ ] Track selected IDs in `carryforwardIds` Set
- [ ] Add selection bar at bottom of FY review: `<div id="cf-selection-bar">`
- [ ] Selection bar content: "X orders selected" + FY target dropdown + "Carry Forward" button
- [ ] FY target dropdown: next FY from current (e.g., if reviewing 2025-26, default target is 2026-27)
- [ ] Write `doCarryforward()` async function:
  - [ ] Validate: at least 1 order selected
  - [ ] Show confirmation dialog: "Carry forward X orders to FY 2026-27?"
  - [ ] Call `POST /api/v1/production/fulfillment/carryforward`
  - [ ] Body: `{fulfillment_ids: [...], new_fy: "2026-27", revised_by: "User"}`
  - [ ] Show result toast: "Carried forward X of Y orders"
  - [ ] Reload FY review data
  - [ ] Clear selection
- [ ] Disable "Carry Forward" button when 0 selected
- [ ] Show loading state on button during API call
- [ ] Style selection bar (fixed at bottom, matches planning page pattern)
- [ ] Style checkboxes consistent with planning page order selection
- [ ] Test: selecting orders updates count in selection bar
- [ ] Test: carryforward creates new records and marks old as 'carryforward'
- [ ] Test: after carryforward, old orders disappear from FY review (status changed)
- [ ] Test: error handling if API fails

#### A4 — Revise Modal Enhancement
- [ ] After opening revise modal, show current values (original_qty, current pending, current deadline) as read-only context
- [ ] Add "Revised by" text input field to modal (instead of hardcoding "User")
- [ ] Pre-fill "Revised by" from localStorage `candor_state_user_name` if available
- [ ] Validate: at least one of new_qty or new_date must be provided
- [ ] Validate: reason is required (already implemented)
- [ ] Show validation error messages inline below fields
- [ ] Test: modal shows current values for context
- [ ] Test: submit works with only qty change, only date change, or both

---

### Module B: Plan List Enhancements

#### B1 — Date Range Filter
- [ ] Add date inputs to toolbar in `plan-list/index.html`: `<input type="date" id="filter-date-from">` and `<input type="date" id="filter-date-to">`
- [ ] Add "Clear dates" button (small X icon) next to date inputs
- [ ] Wire `change` events on both inputs to call `loadPlans()` with reset page=1
- [ ] In `loadPlans()`: append `&date_from=X&date_to=Y` to URL if set
- [ ] Style date inputs consistent with existing filter elements
- [ ] Test: date range filter returns only plans within range
- [ ] Test: clearing dates returns all plans
- [ ] Test: setting only date_from works (open-ended range)
- [ ] Test: setting only date_to works (open-ended range)

#### B2 — Manual Plan Creation
- [ ] Replace "New Plan" button with split action: primary "Generate AI Plan" + secondary "Create Manual"
  - OR: "New Plan" button opens a small dropdown menu with two options
- [ ] Add manual plan creation modal HTML to `plan-list/index.html`:
  - [ ] Form field: Plan Name (text input, required)
  - [ ] Form field: Entity (select: CFPL/CDPL, required)
  - [ ] Form field: Plan Type (select: daily/weekly, required)
  - [ ] Form field: Date From (date input, required)
  - [ ] Form field: Date To (date input, required)
  - [ ] Buttons: Cancel | Create Plan
- [ ] Write `showManualPlanModal()` / `hideManualPlanModal()` toggle functions
- [ ] Write `createManualPlan()` async function:
  - [ ] Validate all required fields
  - [ ] Call `POST /api/v1/production/plans/create`
  - [ ] Body: `{entity, plan_name, plan_type, date_from, date_to}`
  - [ ] On success: navigate to plan-detail with returned plan_id
  - [ ] On error: show toast with error message
- [ ] Add modal overlay styling (consistent with fulfillment revise modal)
- [ ] Style form fields consistent with shared form patterns
- [ ] Default date_from to today, date_to to today (daily) or today+6 (weekly)
- [ ] Auto-update date_to when plan_type changes (daily: same day, weekly: +6 days)
- [ ] Test: modal opens and closes correctly
- [ ] Test: validation prevents empty required fields
- [ ] Test: successful creation navigates to plan detail
- [ ] Test: created plan appears in list on return

#### B3 — Approve/Cancel Confirmation
- [ ] Before approve: show Electron `dialog.showMessageBoxSync` confirmation
  - [ ] Message: "Approve plan: {plan_name}?"
  - [ ] Buttons: Cancel | Approve
- [ ] Before approve: prompt for approver name (add small input dialog or use Electron dialog with input)
  - [ ] If Electron dialog doesn't support input: add inline prompt in table row
  - [ ] Alternative: use a small modal with name input + confirm button
- [ ] Before cancel: show confirmation dialog
  - [ ] Message: "Cancel plan: {plan_name}? This cannot be undone."
  - [ ] Buttons: No | Yes, Cancel Plan
- [ ] Pass real `approved_by` name to API instead of hardcoded "User"
- [ ] Store last-used approver name in localStorage for convenience
- [ ] Test: approve with confirmation works
- [ ] Test: canceling the confirmation dialog does NOT call the API
- [ ] Test: cancel plan with confirmation works

---

### Module C: Plan Detail Enhancements

#### C1 — Approve/Cancel with Name
- [ ] On approve button click: show small inline form or modal with "Approved by" input
- [ ] Validate name is not empty
- [ ] Pass to `PUT /plans/{id}/approve?approved_by=<name>`
- [ ] On cancel: show confirmation dialog before calling endpoint
- [ ] Store approver name in localStorage for next time
- [ ] Test: approve passes correct name
- [ ] Test: cancel shows confirmation first

#### C2 — Line Edit Validation
- [ ] Add client-side validation to line edit form:
  - [ ] `planned_qty_kg`: must be > 0, numeric
  - [ ] `planned_qty_units`: must be >= 0 if provided, integer
  - [ ] `priority`: must be 1-10, integer
  - [ ] `shift`: must be "day" or "night" (use select dropdown instead of text input)
  - [ ] `estimated_hours`: must be > 0 if provided, numeric
- [ ] Show inline validation errors below each field (red text)
- [ ] Disable "Save Changes" button if validation errors exist
- [ ] Track original values; disable "Save Changes" if no changes detected
- [ ] Highlight changed fields with subtle border color change
- [ ] Convert `shift` input from text to `<select>` with options: day, night
- [ ] Convert `priority` input to `<select>` with options 1-10
- [ ] Test: invalid values show error messages
- [ ] Test: save button disabled when no changes
- [ ] Test: save button enabled when values change
- [ ] Test: successful save reloads plan and shows toast

#### C3 — Display Improvements
- [ ] Show machine name (not just ID) in line details — use machine data from plan's ai_analysis_json if available
- [ ] Show BOM FG name alongside BOM ID
- [ ] Format linked_so_fulfillment_ids as clickable chips (navigate to fulfillment page filtered)
- [ ] Add "Copy plan summary" button (copies plan name + lines summary to clipboard for sharing)
- [ ] Test: machine names display correctly
- [ ] Test: BOM names display correctly

---

### Module D: Planning Page Enhancements

#### D1 — Improved Loading State
- [ ] Replace simple spinner with multi-step progress indicator during AI generation
- [ ] Step 1: "Collecting planning context..." (shown immediately)
- [ ] Step 2: "Claude is analyzing demand & inventory..." (shown after 2s)
- [ ] Step 3: "Building production schedule..." (shown after 6s)
- [ ] Step 4: "Finalizing plan..." (shown after 10s)
- [ ] Add animated dots or pulse effect to current step
- [ ] Disable all page interactions during generation (entity buttons, checkboxes, date inputs)
- [ ] Style progress steps with icons and connecting line
- [ ] Test: progress steps advance on timer
- [ ] Test: all interactions disabled during generation
- [ ] Test: interactions re-enabled after completion or error

#### D2 — Error Recovery
- [ ] On generation failure: show error banner with message + "Retry" button
- [ ] Preserve `selectedIds` Set after error (don't clear selection)
- [ ] Preserve entity and date selections after error
- [ ] "Retry" button re-triggers generation with same parameters
- [ ] Add "Change Selection" link in error state to scroll back to table
- [ ] Test: error preserves all selections
- [ ] Test: retry button works with same parameters
- [ ] Test: user can modify selection after error and retry

---

### Cross-Cutting Concerns

#### Navigation Updates
- [ ] Register `navigate-to-fy-review` IPC event in `main.js` (if FY review is a separate page — or handle as in-page toggle)
- [ ] Ensure back navigation works correctly from all new modals/states
- [ ] Test: back button returns to correct previous page from all states

#### Consistent Styling
- [ ] All new modals match existing revise modal pattern (overlay, centered card, form grid)
- [ ] All new buttons use existing CSS classes (`.btn-primary`, `.btn-secondary`, `.btn-danger`)
- [ ] All new badges use existing badge classes (`.badge.ok`, `.badge.error`, `.badge.warning`, `.badge.info`)
- [ ] All new tables use existing table classes (`.po-table`, `.po-row`)
- [ ] FY filter dropdown matches status filter styling
- [ ] Date inputs match existing date input styling from planning page

#### Error Handling
- [ ] All new API calls wrapped in try/catch
- [ ] All errors show toast notification with user-friendly message
- [ ] Network errors show "Connection failed — check if server is running"
- [ ] 404 errors show "Resource not found"
- [ ] Loading states shown during all API calls
- [ ] Loading states cleared on both success and error

---

## Files to Create/Modify

| File | Action | Changes |
|------|--------|---------|
| `Desktop/src/modules/production/fulfillment/index.html` | Modify | Add FY filter, FY review section, carryforward UI |
| `Desktop/src/modules/production/fulfillment/fulfillment.js` | Modify | FY filter logic, FY review, carryforward, revise enhancements |
| `Desktop/src/modules/production/fulfillment/styles.css` | Modify | FY review styles, carryforward selection bar |
| `Desktop/src/modules/production/plan-list/index.html` | Modify | Date filters, manual plan modal, split new-plan button |
| `Desktop/src/modules/production/plan-list/plan-list.js` | Modify | Date filter logic, manual plan creation, approve confirmation |
| `Desktop/src/modules/production/plan-list/plan-list.css` | Modify | Modal styles, date filter styles |
| `Desktop/src/modules/production/plan-detail/index.html` | Modify | Approve name input, validation messages |
| `Desktop/src/modules/production/plan-detail/plan-detail.js` | Modify | Approve name prompt, line validation, display improvements |
| `Desktop/src/modules/production/plan-detail/styles.css` | Modify | Validation styles, name input styles |
| `Desktop/src/modules/production/planning/index.html` | Modify | Progress indicator HTML |
| `Desktop/src/modules/production/planning/planning.js` | Modify | Progress steps, error recovery |
| `Desktop/src/modules/production/planning/planning.css` | Modify | Progress indicator styles |
| `Desktop/main.js` | Modify | Only if FY review becomes a separate page (unlikely) |

---

## Verification Plan

1. **Fulfillment FY Filter:** Change FY dropdown → verify table and summary cards update with correct data
2. **FY Review:** Click FY Review → verify grouped customer data loads → toggle back to list
3. **Carryforward:** Select orders in FY review → carry forward → verify old orders marked 'carryforward', new orders created in target FY
4. **Manual Plan:** Create manual plan from plan-list → verify navigates to plan-detail with empty draft plan
5. **Date Filter:** Set date range on plan-list → verify only matching plans shown
6. **Approve with Name:** Approve plan → verify name prompt appears → verify name stored in API
7. **Line Validation:** Edit plan line with invalid values → verify errors shown, save disabled
8. **AI Progress:** Generate plan → verify progress steps animate → verify result renders
9. **Error Recovery:** Disconnect server → generate plan → verify error shown with retry, selections preserved
10. **Navigation:** Test back button from every new state/modal
