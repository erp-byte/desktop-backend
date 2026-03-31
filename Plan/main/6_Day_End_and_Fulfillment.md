# Part 6: Day-End & Fulfillment ✅ COMPLETED

---

## 18. Day-End Dispatch ✅

**Implementation:** `services/day_end.py`

### Checklist

- [x] `get_day_end_summary(entity, date)`: Query completed final-stage job cards for the day
  - [x] Joins job_card + production_order + job_card_output
  - [x] Returns: items list + totals (fg_output, dispatch, loss, offgrade)
- [x] `bulk_dispatch(dispatches, entity)`: Bulk update dispatch_qty
  - [x] Update job_card_output.dispatch_qty
  - [x] Update so_fulfillment.dispatched_qty_kg
  - [x] Create floor_movement (fg_store → dispatched)
  - [x] Deduct from floor_inventory (fg_store)
- [x] Sign-off flow: already built in Part 4 (job_card_sign_off table + PUT /job-cards/{id}/sign-off)
- [x] Router: `GET /day-end/summary`
- [x] Router: `PUT /day-end/dispatch`

---

## 19. Day-End Balance Scan ✅

**Implementation:** `services/day_end.py`

### Checklist

- [x] `submit_balance_scan(floor, entity, submitted_by, scan_lines)`:
  - [x] Get system quantities from floor_inventory for the floor
  - [x] For each scanned item: calculate variance_kg, variance_pct
  - [x] Create day_end_balance_scan header (status='submitted' or 'variance_flagged')
  - [x] Create day_end_balance_scan_line records
  - [x] Flag items with |variance_pct| > 2% as 'variance_detected'
  - [x] Create store_alert if any variances found
- [x] `get_scan_status(entity, date)`: Per-floor submission status for today
  - [x] Checks all 4 required floors (rm_store, pm_store, production_floor, fg_store)
  - [x] Returns submitted/pending per floor
- [x] `get_scan_detail(scan_id)`: Full scan with all line items
- [x] `reconcile_scan(scan_id, reviewed_by)`:
  - [x] Adjust floor_inventory to match physical count
  - [x] Create floor_movement with reason='balance_adjustment'
  - [x] Update scan status to 'reconciled'
- [x] `check_missing_scans(entity, date)`:
  - [x] Check which floors haven't submitted
  - [x] Create alerts for stores + production (escalation)
  - [x] De-duplicate alerts (don't re-alert same day)
- [x] Router: `POST /balance-scan/submit`
- [x] Router: `GET /balance-scan/status`
- [x] Router: `GET /balance-scan/{scan_id}`
- [x] Router: `PUT /balance-scan/{scan_id}/reconcile`
- [x] Router: `POST /balance-scan/check-missing`

---

## 20. FY Transition ✅

**Implementation:** Already built in Part 2 (`services/fulfillment.py`) + new cancel endpoint

### Checklist

- [x] `GET /fulfillment/fy-review` — unfulfilled orders for current FY (Part 2)
- [x] `POST /fulfillment/carryforward` — bulk carry forward to new FY (Part 2)
- [x] `PUT /fulfillment/{id}/revise` — revise qty/date with audit log (Part 2)
- [x] `POST /fulfillment/cancel` — cancel with reason + audit log (NEW)
  - [x] Validates order not already cancelled/fulfilled
  - [x] Creates so_revision_log entry with old status + reason

---

## Files Created/Modified

| File | Status |
|------|--------|
| `services/day_end.py` | ✅ Done — dispatch summary, bulk dispatch, balance scan (submit/status/detail/reconcile/check-missing) |
| `router.py` | ✅ Done — 8 new endpoints (day-end, balance scan, fulfillment cancel) |

## New Endpoints (8, total: 70)

| # | Method | Endpoint | Description |
|---|--------|----------|-------------|
| 62 | GET | `/day-end/summary` | Completed orders + totals for today |
| 63 | PUT | `/day-end/dispatch` | Bulk dispatch qty update |
| 64 | POST | `/balance-scan/submit` | Submit physical count for a floor |
| 65 | GET | `/balance-scan/status` | Per-floor scan status for today |
| 66 | GET | `/balance-scan/{id}` | Scan detail with all lines |
| 67 | PUT | `/balance-scan/{id}/reconcile` | Approve adjustment, fix inventory |
| 68 | POST | `/balance-scan/check-missing` | Alert for floors that haven't scanned |
| 69 | POST | `/fulfillment/cancel` | Cancel fulfillment records with reason |
