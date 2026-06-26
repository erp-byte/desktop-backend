# Part 7: AI & Revision — Frontend Implementation Plan

---

## Context

The backend for Part 7 (AI & Revision) is **fully implemented** with 8 new API endpoints across 3 areas: AI-Driven Plan Revision, Discrepancy Management, and AI Recommendations/Feedback. **No frontend pages exist** for any of these features. This plan creates 3 new pages (Plan Revision, Discrepancy Manager, AI Recommendations Dashboard) and enhances the existing Plan Detail page with revision history and revision trigger.

**Tech stack:** Electron desktop app, vanilla JS/HTML/CSS, IPC-based navigation, fetch API, CSS custom properties from `variables.css`.

**Plan Revision Flow:**
```
Change event occurs (material shortage, machine breakdown, adhoc orders)
  → User describes change_event + selects plan
  → Claude evaluates: which lines to keep/reschedule/cancel/add
  → New plan created (revision_number + 1, previous_plan_id linked)
  → Old plan status = 'revised'
  → New plan → same approval → MRP → job card flow
```

**Discrepancy Flow:**
```
Report discrepancy (rm_grade_mismatch, machine_breakdown, contamination, etc.)
  → Auto-hold: affected job cards locked (reason='discrepancy_hold')
  → Impact analysis: affected qty + customer impact calculated
  → Alert created for production team
  → Resolution: material_substituted | machine_rescheduled | deferred | cancelled_replanned | proceed_with_deviation
  → Affected job cards unlocked/cancelled based on resolution
```

**AI Recommendation Lifecycle:**
```
generated → accepted (user approved plan)
         → rejected (user declined, with feedback)
```

---

## Gap Analysis: All 8 Backend Endpoints — None Built

| # | Endpoint | Description | Frontend Status |
|---|----------|-------------|-----------------|
| 71 | `POST /plans/revise` | AI-driven plan revision | ❌ **NOT BUILT** |
| 72 | `GET /plans/{id}/revision-history` | Revision chain (v1→v2→v3) | ❌ **NOT BUILT** |
| 73 | `POST /discrepancy/report` | Report discrepancy, auto-hold JCs | ❌ **NOT BUILT** |
| 74 | `GET /discrepancy` | List discrepancies (filtered) | ❌ **NOT BUILT** |
| 75 | `GET /discrepancy/{id}` | Discrepancy detail + affected JCs | ❌ **NOT BUILT** |
| 76 | `PUT /discrepancy/{id}/resolve` | Resolve with 5 resolution types | ❌ **NOT BUILT** |
| 77 | `GET /ai/recommendations` | List AI recommendations | ❌ **NOT BUILT** |
| 78 | `PUT /ai/recommendations/{id}/feedback` | Accept/reject recommendation | ❌ **NOT BUILT** |

**Also needed:** Plan Detail page enhancements — revision history panel, "Revise Plan" button, AI recommendation link.

---

## Implementation Plan

### Page 1: Discrepancy Manager (NEW)
**Path:** `Desktop/src/modules/production/discrepancy/`
**Nav route:** `navigate-to-discrepancy`

Report, track, and resolve production discrepancies with auto-hold of affected job cards.

### Page 2: AI Recommendations Dashboard (NEW)
**Path:** `Desktop/src/modules/production/ai-recommendations/`
**Nav route:** `navigate-to-ai-recommendations`

View all AI interactions (plan generation, revision, etc.) with accept/reject feedback workflow.

### Page 3: Plan Detail Enhancements (EXISTING)
**Path:** `Desktop/src/modules/production/plan-detail/` (existing)

Add revision trigger, revision history timeline, and AI feedback inline.

---

## Detailed Checklist

### Page 1: Discrepancy Manager

#### 1A — Discrepancy List View
- [ ] Create folder `Desktop/src/modules/production/discrepancy/`
- [ ] Create `discrepancy/index.html` with standard layout (titlebar, sidebar, content)
- [ ] Header: breadcrumbs (Production > Discrepancies), entity selector (All/CFPL/CDPL)
- [ ] Summary cards row (3 cards):
  - [ ] Card 1: "Open" — count of unresolved discrepancies (red accent)
  - [ ] Card 2: "Resolved" — count of resolved (green accent)
  - [ ] Card 3: "Critical" — count of severity=critical (dark red accent)
- [ ] Toolbar:
  - [ ] Status filter: All / Open / Resolved
  - [ ] Severity filter: All / Critical / Major / Minor
  - [ ] Type filter: All / RM Grade Mismatch / RM QC Failure / RM Expired / Machine Breakdown / Contamination / Short Delivery
  - [ ] Search: job card number, SKU, or description
- [ ] Create `discrepancy/discrepancy.js`
- [ ] Write `loadDiscrepancies()` async function:
  - [ ] Call `GET /api/v1/production/discrepancy?entity=X&status=X&severity=X&type=X&page=X&page_size=50`
  - [ ] Handle loading/error
  - [ ] Call `renderTable()` and `renderPagination()`
- [ ] Write `renderTable(results)` function:
  - [ ] Columns: ID | Type | Severity | Description | Affected Qty (kg) | Affected JCs | Status | Reported At | Actions
  - [ ] Type as descriptive badge (machine_breakdown → "Machine Breakdown")
  - [ ] Severity badge: critical (red), major (orange), minor (amber)
  - [ ] Affected Qty formatted to 1 decimal
  - [ ] Affected JCs: count with expandable list
  - [ ] Status badge: open (red), resolved (green)
  - [ ] Actions: View (→ detail modal), Resolve (if open)
  - [ ] Empty state: "No discrepancies reported"
- [ ] Write `renderSummaryCards()` — count by status/severity
- [ ] Wire filters + entity + pagination
- [ ] Create `discrepancy/styles.css`
- [ ] Test: list loads with all filters
- [ ] Test: severity filter works
- [ ] Test: type filter works
- [ ] Test: pagination works

#### 1B — Report Discrepancy Modal
- [ ] "Report Discrepancy" button (prominent, red accent) in toolbar
- [ ] Modal HTML:
  - [ ] Title: "Report Production Discrepancy"
  - [ ] Discrepancy Type: select dropdown (required)
    - [ ] Options: RM Grade Mismatch | RM QC Failure | RM Expired | Machine Breakdown | Contamination | Short Delivery
  - [ ] Severity: select (required)
    - [ ] Options: Critical | Major | Minor
  - [ ] Description: textarea (required) — detailed description of the issue
  - [ ] Entity: select (CFPL/CDPL, required)
  - [ ] Affected Material/SKU: text input (optional)
  - [ ] Affected Job Card IDs: text input (comma-separated, optional)
  - [ ] Reported By: text input (pre-fill from localStorage)
  - [ ] Buttons: Cancel | Report
- [ ] Write `reportDiscrepancy()` async function:
  - [ ] Validate required fields
  - [ ] Call `POST /api/v1/production/discrepancy/report`
  - [ ] Handle response:
    - [ ] Show toast: "Discrepancy reported — {affected_job_cards} job cards held, {total_affected_qty_kg} kg affected"
    - [ ] If auto-hold occurred: show warning banner "Job cards have been placed on hold"
    - [ ] Close modal
    - [ ] Reload list
- [ ] Show loading state on Report button
- [ ] Style: modal with warning-themed header (red/orange), clear field layout
- [ ] Test: report creates discrepancy
- [ ] Test: auto-hold applies to affected job cards
- [ ] Test: alert created
- [ ] Test: validation blocks empty required fields

#### 1C — Discrepancy Detail Modal
- [ ] Click row or View button → open detail modal
- [ ] Write `loadDiscrepancyDetail(id)` async:
  - [ ] Call `GET /api/v1/production/discrepancy/{id}`
  - [ ] Render detail view
- [ ] Detail layout:
  - [ ] Header: Discrepancy #{id} + Type badge + Severity badge + Status badge
  - [ ] Info grid: Description | Entity | Reported By | Reported At | Total Affected Qty | Customer Impact
  - [ ] Affected Job Cards section:
    - [ ] Table: JC # | FG SKU | Customer | Status | Hold Status
    - [ ] JC # clickable → navigate to job-card-detail
    - [ ] Hold Status: "On Hold" (red lock icon) or "Released" (green)
  - [ ] Resolution section (if resolved):
    - [ ] Resolution Type badge
    - [ ] Resolution Notes
    - [ ] Resolved By + Resolved At
  - [ ] Action buttons (if open):
    - [ ] "Resolve" button → opens resolution form
- [ ] Style: detail modal with severity-colored header border
- [ ] Test: detail loads with correct data
- [ ] Test: affected JCs listed with links
- [ ] Test: resolution shown if resolved

#### 1D — Resolution Workflow
- [ ] "Resolve" button (in detail modal, only for open discrepancies):
  - [ ] Resolution Type: select (required)
    - [ ] material_substituted → "Material Substituted" (unlock held JCs)
    - [ ] machine_rescheduled → "Machine Rescheduled" (unlock held JCs)
    - [ ] deferred → "Deferred" (keep locked, raise indent)
    - [ ] cancelled_replanned → "Cancelled & Replanned" (cancel affected JCs)
    - [ ] proceed_with_deviation → "Proceed with Deviation" (unlock, record deviation)
  - [ ] Resolution Notes: textarea (required)
  - [ ] Resolved By: text input (pre-fill from localStorage)
  - [ ] Buttons: Cancel | Resolve
- [ ] Write `resolveDiscrepancy(id)` async function:
  - [ ] Validate required fields
  - [ ] Call `PUT /api/v1/production/discrepancy/{id}/resolve`
  - [ ] Body: `{resolution_type, resolution_notes, resolved_by}`
  - [ ] Handle response:
    - [ ] Toast with resolution type + affected JC actions
    - [ ] If material_substituted/machine_rescheduled: "X job cards unlocked"
    - [ ] If deferred: "Job cards remain on hold — indent raised"
    - [ ] If cancelled_replanned: "X job cards cancelled"
    - [ ] If proceed_with_deviation: "X job cards unlocked — deviation recorded"
  - [ ] Reload detail + list
- [ ] Show explanation text per resolution type (help the user understand consequences)
- [ ] Confirmation dialog: "Resolve as '{type}'? This will affect X job cards."
- [ ] Show loading state
- [ ] Style: resolution form with type-specific color hints
- [ ] Test: each resolution type works correctly
- [ ] Test: affected JCs updated per resolution
- [ ] Test: alert created on resolution
- [ ] Test: cannot resolve already-resolved discrepancy

---

### Page 2: AI Recommendations Dashboard

#### 2A — Recommendations List
- [ ] Create folder `Desktop/src/modules/production/ai-recommendations/`
- [ ] Create `ai-recommendations/index.html` with standard layout
- [ ] Header: breadcrumbs (Production > AI Recommendations), entity selector
- [ ] Summary cards row (4 cards):
  - [ ] Card 1: "Total" — total recommendation count (blue accent)
  - [ ] Card 2: "Generated" — awaiting feedback (amber accent)
  - [ ] Card 3: "Accepted" — user approved (green accent)
  - [ ] Card 4: "Rejected" — user declined (red accent)
- [ ] Toolbar:
  - [ ] Type filter: All / Daily Plan / Weekly Plan / Plan Revision
  - [ ] Status filter: All / Generated / Accepted / Rejected
  - [ ] Date range: date_from, date_to (optional)
- [ ] Create `ai-recommendations/ai-recommendations.js`
- [ ] Write `loadRecommendations()` async function:
  - [ ] Call `GET /api/v1/production/ai/recommendations?entity=X&recommendation_type=X&status=X&page=X&page_size=20`
  - [ ] Handle loading/error
  - [ ] Call `renderTable()` and `renderPagination()`
- [ ] Write `renderTable(results)` function:
  - [ ] Columns: ID | Type | Entity | Model | Tokens | Latency | Plan | Status | Feedback | Created | Actions
  - [ ] Type badge: daily_plan (blue), weekly_plan (purple), plan_revision (orange)
  - [ ] Model: truncated (e.g., "claude-3.5-sonnet")
  - [ ] Tokens: formatted number
  - [ ] Latency: formatted as "X.Xs"
  - [ ] Plan: clickable plan_id → navigate to plan-detail
  - [ ] Status badge: generated (amber), accepted (green), rejected (red)
  - [ ] Feedback: truncated text with tooltip (or "—" if none)
  - [ ] Actions: Feedback button (if status=generated)
  - [ ] Empty state: "No AI recommendations yet"
- [ ] Wire filters + pagination
- [ ] Create `ai-recommendations/styles.css`
- [ ] Test: list loads with all filters
- [ ] Test: type filter works
- [ ] Test: status filter works
- [ ] Test: plan link navigates correctly
- [ ] Test: pagination works

#### 2B — Feedback Modal
- [ ] Click "Feedback" button on generated recommendation → open modal
- [ ] Modal HTML:
  - [ ] Title: "AI Recommendation Feedback"
  - [ ] Read-only info: Type | Entity | Tokens | Latency | Model | Created
  - [ ] Plan link (if exists): clickable → navigate
  - [ ] Decision: two large buttons
    - [ ] "Accept" (green, check icon)
    - [ ] "Reject" (red, X icon)
  - [ ] Feedback text: textarea (required for reject, optional for accept)
  - [ ] Submit button
- [ ] Write `submitFeedback(recId, status, feedback)` async function:
  - [ ] Validate: if rejecting, feedback is required
  - [ ] Call `PUT /api/v1/production/ai/recommendations/{recId}/feedback`
  - [ ] Body: `{status: "accepted"|"rejected", feedback: "..."}`
  - [ ] Toast: "Recommendation {accepted/rejected}"
  - [ ] Close modal
  - [ ] Reload list + summary cards
- [ ] Visual emphasis on accept/reject choice (selected state highlighted)
- [ ] Show loading state on submit
- [ ] Style: modal with accept/reject button cards, info grid, feedback area
- [ ] Test: accept works (with optional feedback)
- [ ] Test: reject requires feedback
- [ ] Test: status updates in list
- [ ] Test: cannot re-feedback already-decided recommendation

#### 2C — AI Usage Analytics
- [ ] Summary section below cards (optional enhancement):
  - [ ] Total tokens used this month
  - [ ] Average latency
  - [ ] Accept rate %
  - [ ] Most common recommendation type
- [ ] Computed client-side from loaded data (or add server aggregation later)
- [ ] Style: small analytics bar with key metrics
- [ ] Test: analytics compute correctly from data

---

### Page 3: Plan Detail Enhancements

#### 3A — Revision History Panel
- [ ] Add "Revision History" section to plan-detail/index.html (below plan info, above lines)
- [ ] Only show if plan has `revision_number > 1` OR has `previous_plan_id`
- [ ] Write `loadRevisionHistory(planId)` async function:
  - [ ] Call `GET /api/v1/production/plans/{planId}/revision-history`
  - [ ] Render revision timeline
- [ ] Write `renderRevisionTimeline(chain)` function:
  - [ ] Horizontal or vertical timeline of revision chain:
    - [ ] Each node: plan_id | revision # | plan_name | status badge | created_at
    - [ ] Current plan highlighted (bold border, "You are here" label)
    - [ ] Connector lines between nodes (→ arrows)
    - [ ] Each node clickable → navigate to that plan's detail
  - [ ] Revision 1 (original) on left/top → latest revision on right/bottom
  - [ ] Status per revision: draft (amber), approved (green), revised (grey strikethrough), cancelled (red)
- [ ] Show revision_number prominently in plan header (e.g., "Rev 3")
- [ ] Style: timeline with nodes, connector lines, status coloring
- [ ] Test: revision chain loads correctly
- [ ] Test: all revisions displayed in order
- [ ] Test: current plan highlighted
- [ ] Test: clicking other revision navigates to it
- [ ] Test: single-revision plan shows "Original (no revisions)"

#### 3B — Revise Plan Button & Flow
- [ ] Add "Revise Plan" button to plan-detail action buttons (only for approved/executed plans)
- [ ] On click: open revision modal
  - [ ] Modal title: "Revise Plan — {plan_name}"
  - [ ] Current plan info: entity, date, # lines, # in_progress
  - [ ] Warning: "X lines are in_progress and cannot be rescheduled"
  - [ ] Change Event: textarea (required) — describe what changed
    - [ ] Placeholder: "e.g., Material shortage on cashew raw nuts, Machine B breakdown..."
  - [ ] Add New Fulfillment IDs: optional multi-input for adhoc orders
    - [ ] Text input or fetch open fulfillments to select from
  - [ ] "Generate Revision" button (calls Claude AI)
- [ ] Write `revisePlan()` async function:
  - [ ] Validate: change_event not empty
  - [ ] Show multi-step progress (like planning page):
    - [ ] Step 1: "Collecting current plan context..." (0s)
    - [ ] Step 2: "Claude is evaluating changes..." (2s)
    - [ ] Step 3: "Building revised schedule..." (6s)
  - [ ] Call `POST /api/v1/production/plans/revise`
  - [ ] Body: `{plan_id, change_event, new_fulfillment_ids: [...]}`
  - [ ] Handle response:
    - [ ] Show revision summary:
      - [ ] "New plan created: Rev {revision_number}"
      - [ ] "Lines kept: X | Added: Y | Cancelled: Z"
      - [ ] Material check + risk flags (same as plan generation)
    - [ ] "View New Plan" button → navigate to new plan's detail
    - [ ] Toast: "Revision created — review and approve when ready"
  - [ ] Handle errors:
    - [ ] "Plan not found" / "Plan cannot be revised"
    - [ ] Claude API errors with retry
- [ ] Preserve plan context during loading (disable interactions)
- [ ] Style: revision modal with progress indicator, warning section, results preview
- [ ] Test: revision creates new plan with incremented revision_number
- [ ] Test: old plan marked as 'revised'
- [ ] Test: in_progress lines not rescheduled
- [ ] Test: new fulfillment IDs added as new lines
- [ ] Test: error handling works (no change_event, API failure)
- [ ] Test: navigation to new plan works

#### 3C — AI Recommendation Link
- [ ] In plan detail header area, if plan is `ai_generated=true`:
  - [ ] Show "AI Generated" badge (already exists)
  - [ ] Add small "View AI Recommendation" link next to it
  - [ ] On click: navigate to ai-recommendations page filtered to this plan's recommendation
  - [ ] OR: open inline panel showing recommendation details (tokens, latency, model, feedback status)
- [ ] If recommendation has no feedback yet: show "Give Feedback" button inline
  - [ ] Opens same feedback modal as ai-recommendations page
- [ ] Test: AI badge + link shown for AI-generated plans
- [ ] Test: link navigates to recommendation
- [ ] Test: inline feedback works

#### 3D — Revision Diff (Optional Enhancement)
- [ ] When viewing a revised plan (revision_number > 1):
  - [ ] "Compare with Previous" button
  - [ ] Shows side-by-side or inline diff:
    - [ ] Lines kept: normal row (grey)
    - [ ] Lines added (new in this revision): green highlighted
    - [ ] Lines cancelled (in previous, not in this): red strikethrough
    - [ ] Lines rescheduled (changed priority/machine/shift): amber highlighted with change indicators
  - [ ] Diff computed client-side: load current plan lines + previous plan lines, match by fg_sku_name + customer
- [ ] Write `loadPreviousPlan(previousPlanId)` async:
  - [ ] Call `GET /api/v1/production/plans/{previousPlanId}`
  - [ ] Return lines for comparison
- [ ] Write `renderPlanDiff(currentLines, previousLines)`:
  - [ ] Match lines by fg_sku_name + customer_name
  - [ ] Classify each: kept, added, cancelled, rescheduled
  - [ ] Render with appropriate coloring
- [ ] Style: diff view with green/red/amber row highlights
- [ ] Test: diff shows correct changes between revisions
- [ ] Test: all 4 action types displayed correctly

---

### Navigation & Sidebar Updates

#### Main Process (main.js)
- [ ] Add page config for `'discrepancy'`: path + dimensions
- [ ] Add page config for `'ai-recommendations'`: path + dimensions
- [ ] Add IPC handler: `navigate-to-discrepancy`
- [ ] Add IPC handler: `navigate-to-ai-recommendations`
- [ ] Add to `PAGE_LABELS` in `navigation.js`: `'discrepancy': 'Discrepancies'`, `'ai-recommendations': 'AI Recommendations'`
- [ ] Test: navigation to both new pages works
- [ ] Test: back button works from both pages

#### Sidebar Updates (all production pages)
- [ ] Add nav items to sidebar in ALL production page HTML files:
  - [ ] "Discrepancies" (with warning/alert icon) — under "Operations" or "Quality" group
  - [ ] "AI Recommendations" (with sparkle/brain icon) — under "AI & Analytics" group
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
- [ ] Discrepancy severity colors:
  - [ ] critical = dark red (`#991b1b`)
  - [ ] major = orange (`--clr-warning`)
  - [ ] minor = amber (`#d4850a` lighter)
- [ ] Discrepancy status: open (red), resolved (green)
- [ ] Resolution type colors:
  - [ ] material_substituted = blue
  - [ ] machine_rescheduled = purple
  - [ ] deferred = amber
  - [ ] cancelled_replanned = red
  - [ ] proceed_with_deviation = grey
- [ ] AI recommendation type colors:
  - [ ] daily_plan = blue
  - [ ] weekly_plan = purple
  - [ ] plan_revision = orange
- [ ] Recommendation status: generated (amber), accepted (green), rejected (red)
- [ ] Revision timeline: nodes connected by lines, current node highlighted

#### Error Handling
- [ ] All API calls wrapped in try/catch
- [ ] All errors show toast
- [ ] Claude API timeouts: "AI is taking longer than usual — please wait or retry"
- [ ] Discrepancy errors: "Cannot resolve — discrepancy already resolved"
- [ ] Revision errors: "Cannot revise — plan not in revisable status"
- [ ] Loading states on all API calls (especially Claude calls which can take 10-30s)
- [ ] Loading cleared on success + error

#### Data Flow Integration
- [ ] Plan Detail → "Revise Plan" → creates new plan → navigate to new plan detail
- [ ] Plan Detail → Revision History → click revision → navigate to that plan
- [ ] Plan Detail → "View AI Recommendation" → ai-recommendations page
- [ ] Discrepancy → affected JCs → click JC # → navigate to job-card-detail
- [ ] Discrepancy → resolve (cancelled_replanned) → affected JCs cancelled
- [ ] AI Recommendations → plan link → navigate to plan-detail
- [ ] Discrepancy report → creates alert → visible on alerts page

---

## Files to Create/Modify

| File | Action | Changes |
|------|--------|---------|
| `Desktop/src/modules/production/discrepancy/index.html` | **Create** | Discrepancy list + report modal + detail modal + resolve form |
| `Desktop/src/modules/production/discrepancy/discrepancy.js` | **Create** | List, report, detail, resolve logic |
| `Desktop/src/modules/production/discrepancy/styles.css` | **Create** | Severity badges, resolution styles, detail layout |
| `Desktop/src/modules/production/ai-recommendations/index.html` | **Create** | AI recommendations list + feedback modal |
| `Desktop/src/modules/production/ai-recommendations/ai-recommendations.js` | **Create** | List, filter, feedback submission |
| `Desktop/src/modules/production/ai-recommendations/styles.css` | **Create** | Type badges, feedback modal, analytics bar |
| `Desktop/src/modules/production/plan-detail/index.html` | Modify | Add revision history section, revise button, AI recommendation link, diff view |
| `Desktop/src/modules/production/plan-detail/plan-detail.js` | Modify | Revision history load/render, revise flow with Claude progress, diff computation, feedback inline |
| `Desktop/src/modules/production/plan-detail/styles.css` | Modify | Revision timeline styles, diff view highlights, revise modal |
| `Desktop/main.js` | Modify | 2 page configs, 2 IPC handlers |
| `Desktop/src/shared/js/navigation.js` | Modify | Add PAGE_LABELS for 2 new pages |
| All production page HTML files (14+) | Modify | Add sidebar nav items |

---

## Verification Plan

1. **Report Discrepancy:** Report machine_breakdown → verify discrepancy created, affected JCs auto-held, alert created
2. **Discrepancy List:** Verify filters by status, severity, type all work with pagination
3. **Discrepancy Detail:** View detail → verify affected JCs listed with hold status, impact analysis shown
4. **Resolve (material_substituted):** Resolve → verify held JCs unlocked, resolution logged
5. **Resolve (cancelled_replanned):** Resolve → verify affected JCs cancelled
6. **Resolve (deferred):** Resolve → verify JCs remain locked, indent raised
7. **AI Recommendations List:** Verify filters by type + status, pagination, plan links work
8. **Accept Recommendation:** Accept with optional feedback → verify status updated
9. **Reject Recommendation:** Reject with required feedback → verify status updated, feedback saved
10. **Revise Plan:** Open approved plan → enter change event → generate revision → verify new plan created with revision_number + 1, old plan marked 'revised'
11. **Revision History:** View plan with revisions → verify timeline shows all versions, current highlighted, clickable
12. **Revision Diff:** Compare with previous → verify lines classified as kept/added/cancelled/rescheduled
13. **AI Recommendation Link:** View AI-generated plan → click "View Recommendation" → verify navigates correctly
14. **Full Revision Workflow:** Approve plan → report discrepancy → auto-hold → resolve as cancelled_replanned → revise plan → approve revision → verify MRP runs on new plan
15. **Navigation:** Test discrepancy + ai-recommendations routes, back button, sidebar items, cross-page links
