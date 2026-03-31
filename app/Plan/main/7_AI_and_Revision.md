# Part 7: AI & Revision ✅ COMPLETED

---

## 21. Plan Revision (Adhoc Orders) ✅

**Implementation:** `services/ai_planner.py` (extended with revision functions)

### Checklist

- [x] PLAN_REVISION_SYSTEM_PROMPT template:
  - [x] Rules: don't touch in_progress/completed, can reschedule planned, add new, cancel
  - [x] JSON output: action per line (keep/reschedule/cancel/add)
- [x] `collect_revision_context(plan_id, change_event, entity)`:
  - [x] Current plan + all plan_line statuses
  - [x] Active job cards for this plan's orders
  - [x] Current inventory + machine availability
  - [x] Change event description
- [x] `create_revised_plan(old_plan_id, entity, ai_result, settings)`:
  - [x] New plan with revision_number = old + 1, previous_plan_id = old
  - [x] Process Claude's revised_schedule: keep/reschedule/cancel/add
  - [x] Old plan status = 'revised'
  - [x] Log to ai_recommendation
  - [x] New plan → same approval → MRP → job card flow
- [x] Router: `POST /plans/revise` (body: plan_id, change_event)
- [x] Router: `GET /plans/{id}/revision-history` (chain of revisions bidirectional)

---

## 22. Internal Discrepancy Plan Revision ✅

**Implementation:** `services/discrepancy_manager.py`

### Checklist

- [x] **Step 1 — Reporting**:
  - [x] `report_discrepancy()` — creates discrepancy_report record
  - [x] Types: rm_grade_mismatch, rm_qc_failure, rm_expired, machine_breakdown, contamination, short_delivery
  - [x] Severity: critical, major, minor
  - [x] Auto-identify impact: find affected job_cards + plan_lines
  - [x] Calculate total_affected_qty_kg + customer_impact
  - [x] Create store_alert for production team

- [x] **Step 2 — Auto-Hold**:
  - [x] unlocked/assigned/material_received → locked with reason='discrepancy_hold'
  - [x] in_progress → alert team leader (don't auto-lock)

- [x] **Step 3 — Resolution** (via `resolve_discrepancy()`):
  - [x] material_substituted → unlock held JCs
  - [x] machine_rescheduled → unlock held JCs
  - [x] deferred → keep locked, raise indent
  - [x] cancelled_replanned → cancel affected JCs
  - [x] proceed_with_deviation → unlock, record deviation

- [x] **Step 4 — Audit**:
  - [x] Update discrepancy_report with resolution + resolved_by
  - [x] Create alert on resolution
  - [x] Unlock/cancel affected JCs based on resolution type

- [x] Router: `POST /discrepancy/report`
- [x] Router: `GET /discrepancy` (list with filters)
- [x] Router: `GET /discrepancy/{id}` (detail with affected JCs)
- [x] Router: `PUT /discrepancy/{id}/resolve`

---

## 23. Loss Anomaly Detection ✅

Already built in Part 5:
- [x] `GET /loss/anomalies` — statistical anomaly detection (> Nx average)
- [x] Loss analysis by product/stage/month/machine

### AI Insights Endpoints ✅

- [x] `GET /ai/recommendations` — list all AI recommendations (paginated)
- [x] `PUT /ai/recommendations/{id}/feedback` — accept/reject with feedback

---

## Files Created/Modified

| File | Status |
|------|--------|
| `services/ai_planner.py` | ✅ Extended — PLAN_REVISION_PROMPT, collect_revision_context, create_revised_plan |
| `services/discrepancy_manager.py` | ✅ Done — report, auto-hold, resolve with 5 paths |
| `router.py` | ✅ Done — 8 new endpoints (revision, discrepancy, AI) |

## New Endpoints (8, total: 78)

| # | Method | Endpoint | Description |
|---|--------|----------|-------------|
| 71 | POST | `/plans/revise` | AI-driven plan revision |
| 72 | GET | `/plans/{id}/revision-history` | Revision chain |
| 73 | POST | `/discrepancy/report` | Report discrepancy, auto-hold JCs |
| 74 | GET | `/discrepancy` | List discrepancies (filtered) |
| 75 | GET | `/discrepancy/{id}` | Discrepancy detail + affected JCs |
| 76 | PUT | `/discrepancy/{id}/resolve` | Resolve with 5 resolution types |
| 77 | GET | `/ai/recommendations` | List AI recommendations |
| 78 | PUT | `/ai/recommendations/{id}/feedback` | Accept/reject AI recommendation |
