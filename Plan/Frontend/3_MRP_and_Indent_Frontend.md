# Part 3: MRP & Indent — Frontend Implementation Plan

---

## Context

The backend for Part 3 (MRP & Indent) is **fully implemented** with 11 API endpoints across 3 areas: MRP Run, Indent Management, and Alerts. **No frontend pages exist yet** for any of these features. This plan creates 3 new pages (MRP Results, Indent Management, Alerts Dashboard) and enhances the existing Plan Detail page to surface MRP data inline after approval.

**Tech stack:** Electron desktop app, vanilla JS/HTML/CSS, IPC-based navigation, fetch API, CSS custom properties from `variables.css`.

**Indent Status Flow:**
```
draft → raised → acknowledged → po_created → received
  ↑        ↑          ↑              ↑            ↑
 MRP    planner    purchase      purchase     PO module
creates  sends    acknowledges   links PO     receives
```

---

## Gap Analysis: Backend Endpoints vs Frontend Coverage

| # | Endpoint | Frontend Status |
|---|----------|----------------|
| 18 | `POST /mrp/run` | ❌ **NOT BUILT** |
| 19 | `GET /mrp/availability` | ❌ **NOT BUILT** |
| 20 | `GET /indents` | ❌ **NOT BUILT** |
| 21 | `GET /indents/{id}` | ❌ **NOT BUILT** |
| 22 | `PUT /indents/{id}/edit` | ❌ **NOT BUILT** |
| 23 | `PUT /indents/{id}/send` | ❌ **NOT BUILT** |
| 24 | `POST /indents/send-bulk` | ❌ **NOT BUILT** |
| 25 | `PUT /indents/{id}/acknowledge` | ❌ **NOT BUILT** |
| 26 | `PUT /indents/{id}/link-po` | ❌ **NOT BUILT** |
| 27 | `GET /alerts` | ❌ **NOT BUILT** |
| 28 | `PUT /alerts/{id}/read` | ❌ **NOT BUILT** |

**Also needed:** Plan Detail page enhancement — when plan is approved, the response includes `mrp_summary` + `draft_indents`. Currently the plan-detail page doesn't display this.

---

## Implementation Plan

### Page 1: Indent Management Page (NEW)
**Path:** `Desktop/src/modules/production/indents/`
**Nav route:** `navigate-to-indents`

The primary page for managing purchase indents across their full lifecycle. Used by both Production Planner (draft→send) and Purchase Team (acknowledge→link PO).

#### 1A. Indent List View
- Paginated table of all indents with filters: entity, status, date range, search
- Summary cards: Total Indents | Draft | Raised | Acknowledged | PO Created
- Status badge coloring per status
- Bulk select + "Send Selected" for draft indents

#### 1B. Indent Detail Modal
- Click any row to open detail modal
- Shows: indent number, material, qty, required_by_date, priority, status, plan line info (FG SKU + customer + qty)
- Action buttons change by status:
  - **draft:** Edit fields + Send button
  - **raised:** Acknowledge button (with name input)
  - **acknowledged:** Link PO button (with PO reference input)
  - **po_created / received:** Read-only view

#### 1C. Edit Draft Indent
- Inline editing in detail modal for draft indents
- Editable fields: required_qty_kg, required_by_date, priority
- Save calls `PUT /indents/{id}/edit`

#### 1D. Bulk Send
- Checkboxes on draft rows + "Send Selected" button in selection bar
- Calls `POST /indents/send-bulk`
- Shows result: "Sent X indents, Y alerts created"

---

### Page 2: Alerts Dashboard (NEW)
**Path:** `Desktop/src/modules/production/alerts/`
**Nav route:** `navigate-to-alerts`

Notification center for purchase, stores, and production teams.

#### 2A. Alert List
- Filterable by: team (purchase/stores/production), read/unread, entity
- Each alert shows: type icon, message, related entity link, timestamp, read status
- "Mark as Read" button per alert
- "Mark All Read" bulk action

#### 2B. Alert Badge in Sidebar
- Unread count badge on sidebar nav item
- Refreshes on page load and after actions

---

### Page 3: MRP Results Viewer (INLINE in Plan Detail)
**Enhancement to:** `Desktop/src/modules/production/plan-detail/`

Rather than a separate page, MRP results display inline on the Plan Detail page after approval.

#### 3A. MRP Summary Section
- Shown after plan approval (when `mrp_summary` exists)
- Summary cards: Total Materials | Sufficient | Shortage | Total Shortage (kg)
- "Re-run MRP" button (calls `POST /mrp/run`)

#### 3B. Material Breakdown Table
- Per-material rows: Material SKU | Type (RM/PM) | Gross Req | Off-grade Used | Net Req | On-hand | On-order | Available | Shortage | Status
- Color-coded: green rows (sufficient), red rows (shortage)
- Expandable detail per material

#### 3C. Quick Availability Checker
- Small utility widget: "Check Material" input + qty + entity → calls `GET /mrp/availability`
- Shows result inline: on-hand, on-order, available, shortage

#### 3D. Draft Indents Section
- After MRP runs, show generated draft indents below material table
- Each indent: material, qty, required_by, priority, status
- "Edit" button per indent (opens edit form)
- "Send All Drafts" button → calls `POST /indents/send-bulk`
- "View All Indents" link → navigates to Indent Management page

---

### Navigation & Sidebar Updates
**File:** `Desktop/main.js`

- Add routes: `indents`, `alerts`
- Add sidebar entries in production section: "Purchase Indents", "Alerts"
- Wire IPC: `navigate-to-indents`, `navigate-to-alerts`

---

## Detailed Checklist

### Page 1: Indent Management

#### 1A — Indent List View
- [ ] Create folder `Desktop/src/modules/production/indents/`
- [ ] Create `indents/index.html` with standard layout (titlebar, sidebar, content)
- [ ] Header: breadcrumbs (Production > Purchase Indents), entity selector (All/CFPL/CDPL)
- [ ] Summary cards row (5 cards):
  - [ ] Card 1: "Total Indents" — count of all indents
  - [ ] Card 2: "Draft" — count with draft status (amber accent)
  - [ ] Card 3: "Raised" — count with raised status (blue accent)
  - [ ] Card 4: "Acknowledged" — count with acknowledged status (purple accent)
  - [ ] Card 5: "PO Created" — count with po_created status (green accent)
- [ ] Toolbar row:
  - [ ] Status filter dropdown: All / Draft / Raised / Acknowledged / PO Created / Received / Cancelled
  - [ ] Date range inputs: date_from, date_to
  - [ ] Search input (debounced 300ms): searches material_sku_name
  - [ ] "Clear Filters" button
- [ ] Create `indents/indents.js`
- [ ] Write `loadIndents()` async function:
  - [ ] Build URL: `GET /api/v1/production/indents?entity=X&status=X&date_from=X&date_to=X&search=X&page=X&page_size=50`
  - [ ] Pass all active filters as query params
  - [ ] Handle loading state (show spinner overlay on table)
  - [ ] Handle error state (show toast)
  - [ ] Call `renderTable()` and `renderPagination()`
- [ ] Write `loadSummary()` async function:
  - [ ] Call `GET /api/v1/production/indents?page_size=1` for each status (or compute from loaded data if on single page)
  - [ ] Alternative: compute counts client-side from full result set if page_size allows, or add dedicated endpoint
  - [ ] Update summary card values
- [ ] Write `renderTable(results)` function:
  - [ ] Table columns: Checkbox | Indent # | Material | Qty (kg) | Required By | Priority | Plan Line | PO Ref | Status | Created | Actions
  - [ ] Indent number in monospace font
  - [ ] Material name with title tooltip for full name
  - [ ] Qty formatted to 1 decimal
  - [ ] Required By date formatted DD/MM/YY, highlight red if overdue
  - [ ] Priority as colored circle badge (1-3 red, 4-6 amber, 7-10 grey)
  - [ ] Plan Line: show FG SKU name (fetched from indent detail or embedded)
  - [ ] PO Ref: show if exists, else "—"
  - [ ] Status badge with color: draft (amber), raised (blue), acknowledged (purple), po_created (green), received (green-dark), cancelled (red)
  - [ ] Actions column: View button (eye icon) → opens detail modal
  - [ ] Checkbox column: only show for draft status rows
  - [ ] Empty state: "No indents found" with icon
- [ ] Write `renderPagination(pagination)` function:
  - [ ] Same pattern as existing pages (current ± 2 pages, prev/next)
- [ ] Wire entity selector buttons to reload with page=1
- [ ] Wire status filter dropdown `change` event to reload
- [ ] Wire date inputs `change` events to reload
- [ ] Wire search input with debounce (300ms) to reload
- [ ] Wire "Clear Filters" to reset all filters and reload
- [ ] Create `indents/styles.css` with:
  - [ ] Summary cards grid (5 columns, responsive)
  - [ ] Status badge color variants
  - [ ] Priority circle badge styles
  - [ ] Overdue date highlight (red text)
  - [ ] Table styles consistent with existing `.po-table` pattern
  - [ ] Checkbox column width
- [ ] Test: page loads with default filters (all statuses)
- [ ] Test: entity filter works
- [ ] Test: status filter works (single + multi)
- [ ] Test: date range filter works
- [ ] Test: search filters by material name
- [ ] Test: pagination works
- [ ] Test: empty state renders correctly

#### 1B — Indent Detail Modal
- [ ] Add modal HTML to `indents/index.html`:
  - [ ] Modal overlay + centered card
  - [ ] Header: Indent number + status badge + close button
  - [ ] Info grid (read-only): Material | Qty (kg) | Required By | Priority | Entity | Created
  - [ ] Plan Line info section: FG SKU | Customer | Planned Qty
  - [ ] PO Reference (if linked)
  - [ ] Acknowledged by + at (if acknowledged)
  - [ ] Action buttons area (changes by status)
- [ ] Write `openIndentDetail(indentId)` async function:
  - [ ] Call `GET /api/v1/production/indents/{indentId}`
  - [ ] Populate modal fields from response
  - [ ] Show/hide action buttons based on status:
    - [ ] draft: show Edit form + Send button
    - [ ] raised: show Acknowledge button + name input
    - [ ] acknowledged: show Link PO button + PO reference input
    - [ ] po_created: show read-only "Awaiting delivery"
    - [ ] received: show read-only "Material received"
  - [ ] Show plan_line info (fg_sku_name, customer_name, planned_qty_kg)
  - [ ] Show modal with fade-in
- [ ] Write `closeIndentDetail()` function:
  - [ ] Hide modal, clear form state
- [ ] Click outside modal or X button to close
- [ ] Style modal consistent with existing revise modal pattern
- [ ] Style action buttons per status (send=blue, acknowledge=purple, link-po=green)
- [ ] Style plan line info as subdued card
- [ ] Test: modal opens with correct data for each status
- [ ] Test: correct action buttons shown per status
- [ ] Test: modal closes on X, overlay click, and after successful action

#### 1C — Edit Draft Indent
- [ ] In detail modal (draft status), show editable fields:
  - [ ] Required Qty (kg): number input, pre-filled with current value
  - [ ] Required By Date: date input, pre-filled
  - [ ] Priority: select dropdown 1-10, pre-filled
- [ ] Write `saveIndentEdit(indentId)` async function:
  - [ ] Collect changed fields only (compare with original values)
  - [ ] Validate: qty > 0, date not in past, priority 1-10
  - [ ] Call `PUT /api/v1/production/indents/{indentId}/edit`
  - [ ] Body: `{required_qty_kg, required_by_date, priority}` (only changed fields)
  - [ ] Show toast: "Indent updated" or error
  - [ ] Reload indent list
  - [ ] Keep modal open with updated data
- [ ] Show inline validation errors below fields
- [ ] Disable Save button if no changes or validation errors
- [ ] Show loading state on Save button during API call
- [ ] Test: editing qty updates correctly
- [ ] Test: editing date updates correctly
- [ ] Test: editing priority updates correctly
- [ ] Test: validation blocks invalid input
- [ ] Test: only changed fields sent to API

#### 1D — Send Indent (Single)
- [ ] In detail modal (draft status), add "Send Indent" button
- [ ] Write `sendIndent(indentId)` async function:
  - [ ] Show confirmation: "Send indent {indent_number} to purchase team?"
  - [ ] Call `PUT /api/v1/production/indents/{indentId}/send`
  - [ ] Show toast: "Indent sent — {alerts_created} alerts created"
  - [ ] Close modal
  - [ ] Reload indent list + summary
- [ ] Show loading state on Send button
- [ ] Test: send transitions draft → raised
- [ ] Test: confirmation dialog works
- [ ] Test: list updates after send

#### 1E — Bulk Send Drafts
- [ ] Add checkboxes to draft indent rows (column 1)
- [ ] Add "Select All Drafts" checkbox in table header
- [ ] Track selected IDs in `selectedDraftIds` Set
- [ ] Add selection bar at bottom: `<div id="bulk-selection-bar">`
  - [ ] Content: "X drafts selected" + "Send Selected" button
  - [ ] Hidden when 0 selected
- [ ] Write `sendBulkIndents()` async function:
  - [ ] Validate: at least 1 selected
  - [ ] Show confirmation: "Send X indents to purchase team?"
  - [ ] Call `POST /api/v1/production/indents/send-bulk`
  - [ ] Body: `{indent_ids: [...]}`
  - [ ] Show toast: "Sent X indents, Y alerts created"
  - [ ] Clear selection
  - [ ] Reload list + summary
- [ ] Disable "Send Selected" when 0 selected
- [ ] Update selection count on checkbox change
- [ ] Show loading state on Send Selected button
- [ ] Style selection bar (fixed bottom, matches planning page pattern)
- [ ] Test: selecting drafts updates count
- [ ] Test: bulk send works
- [ ] Test: selection cleared after send
- [ ] Test: non-draft rows don't have checkboxes

#### 1F — Acknowledge Indent (Purchase Team)
- [ ] In detail modal (raised status), show:
  - [ ] "Acknowledged by" text input (required)
  - [ ] "Acknowledge" button (purple)
- [ ] Write `acknowledgeIndent(indentId)` async function:
  - [ ] Validate: name not empty
  - [ ] Call `PUT /api/v1/production/indents/{indentId}/acknowledge`
  - [ ] Body: `{acknowledged_by: "..."}`
  - [ ] Show toast: "Indent acknowledged"
  - [ ] Close modal
  - [ ] Reload list + summary
- [ ] Pre-fill name from localStorage `candor_state_user_name` if available
- [ ] Store name in localStorage after successful acknowledge
- [ ] Show loading state on button
- [ ] Test: acknowledge transitions raised → acknowledged
- [ ] Test: acknowledged_by and acknowledged_at shown in list
- [ ] Test: validation blocks empty name

#### 1G — Link PO (Purchase Team)
- [ ] In detail modal (acknowledged status), show:
  - [ ] "PO Reference" text input (required, e.g., "PO-2026-0042")
  - [ ] "Link PO" button (green)
- [ ] Write `linkIndentToPO(indentId)` async function:
  - [ ] Validate: PO reference not empty
  - [ ] Call `PUT /api/v1/production/indents/{indentId}/link-po`
  - [ ] Body: `{po_reference: "..."}`
  - [ ] Show toast: "PO linked: {po_reference}"
  - [ ] Close modal
  - [ ] Reload list + summary
- [ ] Show loading state on button
- [ ] Test: link transitions acknowledged → po_created
- [ ] Test: PO reference shown in table
- [ ] Test: validation blocks empty reference

---

### Page 2: Alerts Dashboard

#### 2A — Alert List Page
- [ ] Create folder `Desktop/src/modules/production/alerts/`
- [ ] Create `alerts/index.html` with standard layout
- [ ] Header: breadcrumbs (Production > Alerts), entity selector
- [ ] Toolbar:
  - [ ] Team filter: All / Purchase / Stores / Production / QC
  - [ ] Read filter: All / Unread / Read
  - [ ] "Mark All Read" button
- [ ] Create `alerts/alerts.js`
- [ ] Write `loadAlerts()` async function:
  - [ ] Call `GET /api/v1/production/alerts?team=X&read=X&entity=X&page=X&page_size=50`
  - [ ] Handle loading/error states
  - [ ] Call `renderAlerts()` and `renderPagination()`
- [ ] Write `renderAlerts(results)` function:
  - [ ] Each alert as a card/row:
    - [ ] Type icon: material_shortage (warning triangle), indent_raised (bell), material_received (check-circle)
    - [ ] Alert message text
    - [ ] Team badge (purchase=blue, stores=amber, production=green, qc=purple)
    - [ ] Timestamp (relative: "2 hours ago" or absolute)
    - [ ] Read/unread indicator (dot or bold text for unread)
    - [ ] "Mark Read" button (only if unread)
  - [ ] Unread alerts visually distinct (bold text, left border accent, subtle background)
  - [ ] Empty state: "No alerts" with icon
- [ ] Write `markAlertRead(alertId)` async function:
  - [ ] Call `PUT /api/v1/production/alerts/{alertId}/read`
  - [ ] Update UI inline (remove bold, hide mark-read button) without full reload
  - [ ] Update unread count
- [ ] Write `markAllRead()` async function:
  - [ ] Get all unread alert IDs from current page
  - [ ] Call mark-read for each (or batch if endpoint supports)
  - [ ] Reload page
  - [ ] Show toast: "Marked X alerts as read"
- [ ] Wire team filter to reload
- [ ] Wire read filter to reload
- [ ] Wire entity selector to reload
- [ ] Write `renderPagination()` — same pattern
- [ ] Create `alerts/styles.css`:
  - [ ] Alert card styles (unread vs read)
  - [ ] Type icon styles with color per type
  - [ ] Team badge styles
  - [ ] Timestamp muted text
  - [ ] Mark read button (small, subtle)
- [ ] Test: alerts load with default filters
- [ ] Test: team filter works
- [ ] Test: read/unread filter works
- [ ] Test: mark single alert as read
- [ ] Test: mark all read works
- [ ] Test: empty state renders
- [ ] Test: pagination works

#### 2B — Alert Badge in Sidebar
- [ ] In all production page HTML files, add unread count badge to "Alerts" sidebar item
- [ ] Write shared function `loadUnreadAlertCount()`:
  - [ ] Call `GET /api/v1/production/alerts?read=false&page_size=1` — use total from pagination
  - [ ] Return count
- [ ] On page load (in each production page): call `loadUnreadAlertCount()` and update badge
- [ ] Badge styling: red circle with white count text, positioned top-right of nav item
- [ ] Hide badge when count is 0
- [ ] Test: badge shows correct unread count
- [ ] Test: badge updates after marking alerts read
- [ ] Test: badge hidden when 0 unread

---

### Page 3: MRP Integration in Plan Detail

#### 3A — MRP Summary Section (after approval)
- [ ] In `plan-detail/index.html`, add MRP section container (hidden by default):
  - [ ] Section title: "Material Requirements (MRP)" with icon
  - [ ] Summary cards row (4 cards): Total Materials | Sufficient | Shortage | Total Shortage (kg)
  - [ ] "Re-run MRP" button
- [ ] In `plan-detail/plan-detail.js`, modify `renderPlan()`:
  - [ ] After rendering plan info, check if plan status is 'approved' or 'executed'
  - [ ] If approved: show MRP section
  - [ ] Load MRP data: either from `ai_analysis_json` (if stored there) or trigger `POST /mrp/run`
  - [ ] Populate summary cards
- [ ] Write `runMRP()` async function:
  - [ ] Call `POST /api/v1/production/mrp/run` with `{plan_id}`
  - [ ] Show loading state on "Re-run MRP" button
  - [ ] Update summary cards + material table
  - [ ] If draft indents generated, render them
  - [ ] Show toast on success/error
- [ ] Style summary cards (4-column grid, sufficient=green accent, shortage=red accent)
- [ ] Test: MRP section hidden for draft plans
- [ ] Test: MRP section shown for approved plans
- [ ] Test: Re-run MRP updates data
- [ ] Test: summary cards show correct counts

#### 3B — Material Breakdown Table
- [ ] Add material table HTML below MRP summary cards
- [ ] Table columns: Material | Type | Gross Req | Off-grade Used | Net Req | On-hand | On-order | Available | Shortage | Status
- [ ] Write `renderMaterialTable(materials)` function:
  - [ ] Each row shows full material breakdown
  - [ ] Material name in bold
  - [ ] Type badge: RM (amber), PM (blue)
  - [ ] All quantities formatted to 1 decimal with "kg" suffix
  - [ ] Status badge: SUFFICIENT (green), SHORTAGE (red)
  - [ ] Shortage rows highlighted with red-tinted background
  - [ ] Sort: shortages first, then by shortage_kg descending
- [ ] Empty state: "No material data — run MRP to analyze"
- [ ] Style material table (consistent with existing tables, color-coded rows)
- [ ] Test: table renders all materials correctly
- [ ] Test: shortage rows highlighted
- [ ] Test: sort order correct

#### 3C — Quick Availability Checker
- [ ] Add small widget below material table:
  - [ ] Material SKU input (text, with placeholder "Enter material SKU...")
  - [ ] Qty needed input (number)
  - [ ] Entity selector (auto-filled from plan entity)
  - [ ] "Check" button
  - [ ] Result display area
- [ ] Write `checkAvailability()` async function:
  - [ ] Validate: material and qty required
  - [ ] Call `GET /api/v1/production/mrp/availability?material=X&qty=Y&entity=Z`
  - [ ] Render result: on-hand | on-order | available | shortage | status
  - [ ] Color result: green (sufficient), red (shortage)
- [ ] Show loading state on Check button
- [ ] Style widget as compact card
- [ ] Test: availability check returns correct data
- [ ] Test: shortage shown in red
- [ ] Test: validation blocks empty fields

#### 3D — Draft Indents Section (after MRP)
- [ ] Add "Draft Purchase Indents" section below material table
- [ ] Section title with count: "Draft Indents (X)"
- [ ] Table: Indent # | Material | Qty (kg) | Required By | Priority | Actions
- [ ] Write `renderDraftIndents(indents)` function:
  - [ ] Each row shows indent info
  - [ ] Actions: "Edit" button (opens inline edit) + "Send" button
  - [ ] If multiple drafts: "Send All Drafts" button above table
  - [ ] "View All Indents →" link at bottom → navigate to indents page
- [ ] Write `editDraftInline(indentId)` function:
  - [ ] Convert row to editable fields (qty, date, priority)
  - [ ] Save button calls `PUT /indents/{id}/edit`
  - [ ] Cancel button reverts to read-only
- [ ] Write `sendDraftFromPlan(indentId)` function:
  - [ ] Confirmation dialog
  - [ ] Call `PUT /indents/{id}/send`
  - [ ] Toast: "Indent sent"
  - [ ] Update row status badge
- [ ] Write `sendAllDraftsFromPlan()` function:
  - [ ] Collect all draft indent IDs
  - [ ] Call `POST /indents/send-bulk`
  - [ ] Toast: "Sent X indents"
  - [ ] Re-render section
- [ ] Style indent section (subtle border, compact table)
- [ ] Test: draft indents render after MRP run
- [ ] Test: inline edit works
- [ ] Test: single send works
- [ ] Test: bulk send works
- [ ] Test: "View All Indents" navigates correctly

#### 3E — Enhance Plan Approval Response
- [ ] Modify existing approve handler in `plan-detail.js`:
  - [ ] After successful approval, check response for `mrp_summary` and `draft_indents`
  - [ ] Auto-render MRP section with returned data
  - [ ] Show toast: "Plan approved — MRP found X shortages, Y draft indents created"
  - [ ] Scroll to MRP section
- [ ] Test: approve shows MRP results inline
- [ ] Test: toast shows correct summary

---

### Navigation & Sidebar Updates

#### Main Process (main.js)
- [ ] Add page config for `'indents'`:
  - [ ] File path: `src/modules/production/indents/index.html`
  - [ ] Window dimensions: match existing production pages
- [ ] Add page config for `'alerts'`:
  - [ ] File path: `src/modules/production/alerts/index.html`
  - [ ] Window dimensions: match existing production pages
- [ ] Add IPC handler: `navigate-to-indents` (push nav stack, load indents page)
- [ ] Add IPC handler: `navigate-to-alerts` (push nav stack, load alerts page)
- [ ] Add to `PAGE_LABELS` in `navigation.js`: `'indents': 'Purchase Indents'`, `'alerts': 'Alerts'`
- [ ] Test: navigation to indents page works
- [ ] Test: navigation to alerts page works
- [ ] Test: back button returns to previous page

#### Sidebar Updates (all production pages)
- [ ] Add "Purchase Indents" nav item to production sidebar in ALL production page HTML files:
  - [ ] `planning/index.html`
  - [ ] `plan-list/index.html`
  - [ ] `plan-detail/index.html`
  - [ ] `fulfillment/index.html`
  - [ ] `so-creation/index.html`
  - [ ] `indents/index.html` (active state)
  - [ ] `alerts/index.html` (active state)
- [ ] Add "Alerts" nav item with unread badge to production sidebar in ALL pages
- [ ] Icon for indents: clipboard/document icon (inline SVG)
- [ ] Icon for alerts: bell icon (inline SVG)
- [ ] Wire click handlers: `ipcRenderer.send('navigate-to-indents')`, `ipcRenderer.send('navigate-to-alerts')`
- [ ] Test: sidebar items visible on all production pages
- [ ] Test: clicking navigates correctly
- [ ] Test: active state highlights correct page

---

### Cross-Cutting Concerns

#### Consistent Styling
- [ ] All new pages use standard layout: titlebar + sidebar + content (same as existing production pages)
- [ ] All tables use `.po-table`, `.po-row` class pattern
- [ ] All modals use overlay + centered card pattern (same as fulfillment revise modal)
- [ ] All badges use `.badge.ok`, `.badge.error`, `.badge.warning`, `.badge.info`
- [ ] All summary cards use same grid layout + accent border pattern as fulfillment page
- [ ] All buttons use existing button classes
- [ ] Status colors standardized:
  - [ ] draft = amber (`--clr-warning`)
  - [ ] raised = blue (`--clr-info`)
  - [ ] acknowledged = purple (`#7c3aed`)
  - [ ] po_created = green (`--clr-ok`)
  - [ ] received = dark green
  - [ ] cancelled = red (`--clr-mismatch`)

#### Error Handling
- [ ] All API calls wrapped in try/catch
- [ ] All errors show toast with user-friendly message
- [ ] Network errors: "Connection failed — check if server is running"
- [ ] 404 errors: "Indent not found" / "Alert not found"
- [ ] Status transition errors: "Cannot acknowledge — indent is not in 'raised' status"
- [ ] Loading states on all API calls
- [ ] Loading states cleared on success + error

#### Data Flow Integration
- [ ] Plan Detail → MRP section → "View All Indents" → Indent page (filtered by plan)
- [ ] Indent Detail → "View Plan Line" link → Plan Detail page
- [ ] Alert card → clicking navigates to related entity (indent detail, plan, etc.)
- [ ] After PO linking → PO reference clickable (navigate to PO module if available)

---

## Files to Create/Modify

| File | Action | Changes |
|------|--------|---------|
| `Desktop/src/modules/production/indents/index.html` | **Create** | Full indent management page |
| `Desktop/src/modules/production/indents/indents.js` | **Create** | List, detail, edit, send, acknowledge, link-po logic |
| `Desktop/src/modules/production/indents/styles.css` | **Create** | Indent page styles |
| `Desktop/src/modules/production/alerts/index.html` | **Create** | Alerts dashboard page |
| `Desktop/src/modules/production/alerts/alerts.js` | **Create** | Alert list, mark read, filtering |
| `Desktop/src/modules/production/alerts/styles.css` | **Create** | Alert page styles |
| `Desktop/src/modules/production/plan-detail/index.html` | Modify | Add MRP section, material table, availability checker, draft indents |
| `Desktop/src/modules/production/plan-detail/plan-detail.js` | Modify | MRP run, material rendering, indent management, approval enhancement |
| `Desktop/src/modules/production/plan-detail/styles.css` | Modify | MRP section styles, material table, availability widget |
| `Desktop/main.js` | Modify | Add indents + alerts page configs, IPC handlers |
| `Desktop/src/shared/js/navigation.js` | Modify | Add PAGE_LABELS for indents + alerts |
| `Desktop/src/modules/production/planning/index.html` | Modify | Add sidebar nav items |
| `Desktop/src/modules/production/plan-list/index.html` | Modify | Add sidebar nav items |
| `Desktop/src/modules/production/fulfillment/index.html` | Modify | Add sidebar nav items |
| `Desktop/src/modules/production/so-creation/index.html` | Modify | Add sidebar nav items |

---

## Verification Plan

1. **Indent List:** Load page → verify all indents listed with correct status badges, filters work, pagination works
2. **Indent Detail:** Click indent → verify modal shows correct data and action buttons per status
3. **Edit Draft:** Open draft indent → edit qty/date/priority → verify saved correctly
4. **Send Single:** Open draft indent → send → verify status changes to 'raised', 2 alerts created
5. **Bulk Send:** Select multiple drafts → send selected → verify all transition, toast shows count
6. **Acknowledge:** Open raised indent → enter name → acknowledge → verify status + name + timestamp
7. **Link PO:** Open acknowledged indent → enter PO ref → link → verify status + PO shown
8. **Alerts:** Load alerts page → verify correct alerts shown, filter by team, mark as read
9. **Alert Badge:** Navigate to any production page → verify unread count in sidebar badge
10. **MRP in Plan Detail:** Approve plan → verify MRP summary + material table + draft indents render
11. **Re-run MRP:** Click re-run on approved plan → verify updated material data
12. **Availability Check:** Enter material + qty → check → verify on-hand/on-order/shortage shown
13. **Draft Indents in Plan:** After MRP → edit draft inline → send from plan detail → verify
14. **Navigation:** Test all new routes (indents, alerts), back button, cross-page links
15. **Full Workflow:** Generate plan → approve → MRP runs → review draft indents → send → acknowledge → link PO → verify alert trail
