# Part 1: Master Data Setup ✅ COMPLETED

---

## 1. BOM Ingest ✅

Parse BOM Excel, create bom_header + bom_line + bom_process_route. Fuzzy match materials via `match_sku()`. Customer-specific vs generic BOM lookup logic.

**Implementation:** `services/master_ingest.py` — direct DB ingest at startup from `FG_Master_Completion.xlsx` + `BOM_Enrichment.xlsx`

### Results

| Table | Count |
|-------|-------|
| `bom_header` | 1,084 FG products |
| `bom_process_route` | 2,300 process steps |
| `bom_line` | 4,440 material lines (4,438 matched to all_sku, 2 unmatched) |

### Checklist

- [x] Create `services/master_ingest.py`
- [x] Parse `FG_Master_Completion.xlsx` (FG_Master_Fill sheet) using openpyxl
- [x] Parse `BOM_Enrichment.xlsx` (BOM_Enrichment sheet) using openpyxl
- [x] For each FG entry:
  - [x] Create `bom_header` record (fg_sku_name, item_group, sub_group, process_category, business_unit, factory, floors, machines, pack_size_kg, shelf_life_days, gst_rate, hsn_sac, inventory_group, customer_code, entity)
  - [x] Create `bom_process_route` records (split Process Category by "+" → step_number, process_name, stage)
- [x] For each BOM line:
  - [x] Create `bom_line` records (material_sku_name, item_type rm/pm, quantity_per_unit, uom, loss_pct, godown, unit_rate_inr, process_stage)
- [x] Fuzzy match all material names against `all_sku` using `match_sku()` (75% threshold, token_sort_ratio)
- [x] BOM lookup logic: customer-specific BOM (variant) first, fallback to generic (customer_name IS NULL)
- [x] Idempotent: skip if bom_header already has data
- [x] Schema migration: added 11 columns to bom_header, 2 to bom_line via `production_migrate.sql`
- [ ] Router endpoint: `GET /api/v1/production/bom/view` (paginated list) — deferred to later
- [ ] Router endpoint: `GET /api/v1/production/bom/{bom_id}` (detail with lines + route) — deferred to later

---

## 2. Machine & Capacity Ingest ✅

Parse floorwise machine list, create machine records. Derive capacity from FG Master machine-to-group-to-stage mapping with industry-default kg/hr rates.

**Implementation:** `services/master_ingest.py` → `ingest_machines()` + `derive_machine_capacity()` from `Floorwise utility dada.xlsx` + `FG_Master_Completion.xlsx`

### Results

| Table | Count |
|-------|-------|
| `machine` | 86 machines (all floors) |
| `machine_capacity` | 188 entries (machine x stage x group) |

477 capacity entries skipped — FG Master machine names (e.g. "Roasting Machine") don't exactly match floorwise physical names (e.g. "try roaster"). Mapping can be refined later.

### Checklist

- [x] Parse `Floorwise utility dada.xlsx` (floorwise machine list sheet)
- [x] Extract: machine_name, floor (from area sections), factory (W202), entity (cfpl)
- [x] Insert ALL machines (user removes unwanted manually)
- [x] Added `allocation` column (idle/occupied/maintenance) to machine table
- [x] Schema migration: added allocation column via `production_migrate.sql`
- [x] Machine capacity derived from FG Master (machine × stage × group → default kg/hr rates)
- [x] Default rates: base rate by stage × product group multiplier (editable later)
- [x] Idempotent: backfills capacity on restart if table empty
- [ ] Router endpoint: `GET /api/v1/production/machines` (list) — deferred to later
- [ ] Router endpoint: `GET /api/v1/production/machines/{id}` (detail + capacity matrix) — deferred to later

---

## Files Created/Modified

| File | Status |
|------|--------|
| `app/db/production_migrate.sql` | ✅ Done — 3 migration blocks (bom_header +11 cols, bom_line +2 cols, machine +1 col) |
| `app/modules/production/services/master_ingest.py` | ✅ Done — 5 functions: ingest_fg_master, ingest_bom_lines, ingest_machines, derive_machine_capacity, run_master_ingest |
| `app/main.py` | ✅ Done — wired run_master_ingest in lifespan after load_master_items |
