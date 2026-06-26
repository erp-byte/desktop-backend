# Part 5: Inventory & Tracking ✅ COMPLETED

---

## 14. Floor Inventory State Machine ✅

**Implementation:** `services/floor_tracker.py`

### Checklist

- [x] Create `services/floor_tracker.py`
- [x] `move_material()` with validated transitions:
  - [x] rm_store → production_floor
  - [x] pm_store → production_floor
  - [x] production_floor → fg_store
  - [x] production_floor → offgrade
  - [x] production_floor → rm_store
  - [x] offgrade → production_floor
  - [x] Reject invalid transitions with clear error message
  - [x] Debit source, credit destination in single transaction
  - [x] Validate sufficient stock (no negative inventory)
  - [x] Create floor_movement audit record
  - [x] Update last_updated on both records
- [x] `get_floor_summary(entity)`: Aggregated stock per floor
- [x] `get_floor_detail(floor_location, entity)`: All items on a floor (paginated, searchable)
- [x] `get_movement_history(filters)`: Audit trail with sku/floor/date/job_card filters
- [x] Router: `GET /floor-inventory` (list with filters + pagination)
- [x] Router: `GET /floor-inventory/summary` (aggregated per floor)
- [x] Router: `POST /floor-inventory/move` (manual movement)
- [x] Router: `GET /floor-inventory/movements` (movement history)

---

## 15. Idle Material Alert ✅

**Implementation:** `services/idle_checker.py`

### Checklist

- [x] Create `services/idle_checker.py`
- [x] `check_idle_materials(entity)`:
  - [x] Query floor_inventory WHERE last_updated < NOW() - 3 days AND quantity_kg > 0
  - [x] Filter to: production_floor, rm_store, pm_store
  - [x] Check if active job card references material → skip if allocated
  - [x] 3-day warning (material_idle_warning), 5-day critical (material_idle_critical)
  - [x] De-duplicate: skip if alert exists within last 24h
  - [x] Returns: total_checked, warnings, criticals, skipped counts
- [x] Router: `POST /floor-inventory/check-idle` (manual trigger)

---

## 16. Off-Grade Capture & Reuse ✅

### Checklist

- [x] Auto-capture: already done in `job_card_engine.py` → `complete_job_card()` creates offgrade_inventory on last stage
- [x] Off-grade reuse rules CRUD:
  - [x] `POST /offgrade/rules/create`
  - [x] `GET /offgrade/rules`
  - [x] `PUT /offgrade/rules/{id}`
- [x] Off-grade consumption: already handled in MRP (`mrp.py` checks offgrade availability)
- [x] Router: `GET /offgrade/inventory` (list with filters: entity, status, item_group)

---

## 17. Process Loss Recording ✅

### Checklist

- [x] Auto-record: already done in `job_card_engine.py` → `complete_job_card()` creates process_loss on last stage
- [x] Loss analysis: `GET /loss/analysis` with group_by (product, stage, month, machine)
- [x] Anomaly detection: `GET /loss/anomalies` — batches with loss > Nx average (configurable threshold)
- [x] Yield summary: `GET /yield/summary` by product/period

---

## Files Created/Modified

| File | Status |
|------|--------|
| `services/floor_tracker.py` | ✅ Done — move_material (state machine), summary, detail, movements |
| `services/idle_checker.py` | ✅ Done — check_idle_materials with 3/5-day thresholds |
| `router.py` | ✅ Done — 13 new endpoints (floor inv, offgrade, loss, yield) |

## New Endpoints (13, total: 62)

| # | Method | Endpoint | Description |
|---|--------|----------|-------------|
| 50 | GET | `/floor-inventory` | List inventory items (filterable, paginated) |
| 51 | GET | `/floor-inventory/summary` | Aggregated stock per floor |
| 52 | POST | `/floor-inventory/move` | Manual material movement |
| 53 | GET | `/floor-inventory/movements` | Movement audit trail |
| 54 | POST | `/floor-inventory/check-idle` | Trigger idle material check |
| 55 | GET | `/offgrade/inventory` | List off-grade stock |
| 56 | GET | `/offgrade/rules` | List reuse rules |
| 57 | POST | `/offgrade/rules/create` | Create reuse rule |
| 58 | PUT | `/offgrade/rules/{id}` | Update reuse rule |
| 59 | GET | `/loss/analysis` | Loss analysis (group by product/stage/month) |
| 60 | GET | `/loss/anomalies` | Anomaly detection (>Nx avg) |
| 61 | GET | `/yield/summary` | Yield by product/period |
