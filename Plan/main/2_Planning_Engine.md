# Part 2: Planning Engine ✅ COMPLETED

---

## 3. SO Fulfillment Sync ✅

Read so_header + so_line, auto-create so_fulfillment records. Track financial year (Apr-Mar), pending/produced/dispatched qty. Support carryforward + revision with audit log.

**Implementation:** `services/fulfillment.py`

### Checklist

- [x] Create `services/fulfillment.py`
- [x] `sync_fulfillment()`:
  - [x] Read all so_line not already in so_fulfillment
  - [x] For each so_line not already in so_fulfillment: create record
  - [x] Set financial_year based on so_date (Apr-Mar -> "2025-26", "2026-27")
  - [x] Set original_qty_kg from so_line.quantity
  - [x] Set pending_qty_kg = original_qty_kg
  - [x] Set order_status = 'open'
  - [x] Set delivery_deadline = so_date + 7 days
  - [x] Set priority = 5 (default, adjustable)
  - [x] Derive entity from so_header.company
- [x] `get_demand_summary()`: Aggregate pending demand grouped by product/customer
- [x] `get_fulfillment_list()`: Paginated list with filters (entity, status, fy, customer, search)
- [x] `get_fy_review()`: All unfulfilled orders for FY close review
- [x] `carryforward_orders(fulfillment_ids)`: Bulk carry forward selected IDs to new FY
  - [x] Create new so_fulfillment with carryforward_from_id linked to old
  - [x] Set old record order_status = 'carryforward'
  - [x] Log to so_revision_log
- [x] `revise_order(fulfillment_id, new_qty, new_date, reason)`:
  - [x] Update revised_qty_kg, pending_qty_kg, delivery_deadline
  - [x] Create so_revision_log entry (old_value, new_value, reason, revised_by)
- [x] Router: `POST /api/v1/production/fulfillment/sync`
- [x] Router: `GET /api/v1/production/fulfillment` (list with filters + pagination)
- [x] Router: `GET /api/v1/production/fulfillment/demand-summary`
- [x] Router: `GET /api/v1/production/fulfillment/fy-review`
- [x] Router: `POST /api/v1/production/fulfillment/carryforward`
- [x] Router: `PUT /api/v1/production/fulfillment/{id}/revise`

---

## 4. Claude AI Plan Generation ✅

Collect context (pending demand, inventory, machines, BOMs), send to Claude, get back daily schedule + material check + risk flags. Store as production_plan + plan_lines. Planner selects specific fulfillment IDs to include in each plan.

**Implementation:** `services/ai_planner.py`

### Key design decisions:
- **BOM determines production type**: RM components → full production, only PM/FG → repackaging
- **Planner selects orders**: auto-sync all SOs, but planner picks which fulfillment_ids go into each plan
- **Production vs Repackaging**: Claude schedules full route for production, packaging-only for repackaging

### Checklist

- [x] Create `services/ai_planner.py`
- [x] `collect_planning_context(entity, target_date, fulfillment_ids)`:
  - [x] Query so_fulfillment (only planner-selected IDs)
  - [x] BOM validation: match fg_sku_name → bom_header, flag no_bom items
  - [x] Classify production type: RM in BOM → 'production', only PM/FG → 'repackaging'
  - [x] Query floor_inventory (RM/PM/FG stock per store)
  - [x] Query machine + machine_capacity (available, not maintenance)
  - [x] Query active job_cards (in-progress work)
  - [x] Query purchase_indent (pending material)
  - [x] Return structured JSON context
- [x] `call_claude(settings, system_prompt, user_data)`:
  - [x] Create anthropic.AsyncAnthropic client
  - [x] Call messages.create with structured prompt
  - [x] Parse JSON response (handle markdown fences)
  - [x] Log to ai_recommendation table (prompt, response, tokens, latency)
  - [x] Return parsed result
- [x] System prompt templates:
  - [x] DAILY_PLAN_PROMPT (single-day schedule)
  - [x] WEEKLY_PLAN_PROMPT (7-day horizon)
- [x] `create_plan_from_ai()`: create production_plan (status='draft', ai_generated=TRUE) + production_plan_line records
- [x] Store full Claude analysis in ai_analysis_json (JSONB)
- [x] Match machine_name → machine_id, fg_sku_name → bom_id
- [x] Router: `POST /api/v1/production/plans/generate-daily` (body: entity, date, fulfillment_ids[])
- [x] Router: `POST /api/v1/production/plans/generate-weekly` (body: entity, date_from, date_to, fulfillment_ids[])
- [x] Added `anthropic` to requirements.txt

---

## 5. Plan Approve & CRUD ✅

Planner reviews, edits any field, approves. Status: draft -> approved. Uses `model_fields_set` for partial updates — only explicitly sent fields are changed, unsent fields stay untouched.

**Implementation:** `router.py` (plan CRUD endpoints)

### Checklist

- [x] Plan CRUD endpoints:
  - [x] `POST /api/v1/production/plans/create` (manual plan creation, no AI)
  - [x] `GET /api/v1/production/plans` (list with filters: entity, status, date, type + pagination)
  - [x] `GET /api/v1/production/plans/{id}` (plan detail with lines + material_check + risk_flags)
  - [x] `PUT /api/v1/production/plans/{id}/lines/{lid}` (edit any field: fg, customer, bom, qty, machine, priority, shift, stages, hours, reasoning, status)
  - [x] `POST /api/v1/production/plans/{id}/lines` (add manual line to draft plan)
  - [x] `DELETE /api/v1/production/plans/{id}/lines/{lid}` (remove line from draft plan)
  - [x] `PUT /api/v1/production/plans/{id}/approve` (set status='approved', approved_by, approved_at)
  - [x] `PUT /api/v1/production/plans/{id}/cancel` (set status='cancelled')
- [x] Validation: only draft plans can be edited/approved
- [x] Partial update: uses `model_fields_set` — only sent fields are updated, old data preserved
- [x] On approve: trigger MRP run (Part 3) — implemented in Part 3, calls run_mrp + generate_draft_indents

---

## Files Created/Modified

| File | Status |
|------|--------|
| `app/modules/production/services/fulfillment.py` | ✅ Done — sync, demand summary, list, fy review, carryforward, revise |
| `app/modules/production/services/ai_planner.py` | ✅ Done — context collection, BOM validation, Claude call, plan creation |
| `app/modules/production/router.py` | ✅ Done — 17 endpoints (health + 6 fulfillment + 10 plan) |
| `requirements.txt` | ✅ Done — added `anthropic` |

## Endpoints Summary (17 total)

| # | Method | Endpoint | Description |
|---|--------|----------|-------------|
| 1 | GET | `/health` | Module health + table counts |
| 2 | POST | `/fulfillment/sync` | Sync SO lines → so_fulfillment |
| 3 | GET | `/fulfillment` | Paginated list with filters |
| 4 | GET | `/fulfillment/demand-summary` | Aggregated demand by product/customer |
| 5 | GET | `/fulfillment/fy-review` | Unfulfilled orders for FY close |
| 6 | POST | `/fulfillment/carryforward` | Bulk carry forward to new FY |
| 7 | PUT | `/fulfillment/{id}/revise` | Revise qty/date with audit log |
| 8 | POST | `/plans/generate-daily` | Claude AI daily plan |
| 9 | POST | `/plans/generate-weekly` | Claude AI weekly plan |
| 10 | POST | `/plans/create` | Manual plan creation |
| 11 | GET | `/plans` | Paginated plan list with filters |
| 12 | GET | `/plans/{id}` | Plan detail + lines + material check |
| 13 | PUT | `/plans/{id}/lines/{lid}` | Edit plan line (partial update) |
| 14 | POST | `/plans/{id}/lines` | Add manual line to draft |
| 15 | DELETE | `/plans/{id}/lines/{lid}` | Remove line from draft |
| 16 | PUT | `/plans/{id}/approve` | Approve plan |
| 17 | PUT | `/plans/{id}/cancel` | Cancel plan |
