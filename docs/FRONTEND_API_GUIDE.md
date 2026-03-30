# SO Intake — Frontend API Guide

## Base URL

```
http://localhost:8000
```

Swagger UI: `http://localhost:8000/docs`

---

## Endpoints

| # | Method | Path | Purpose |
|---|---|---|---|
| 1 | `POST` | `/api/v1/so/upload` | Upload Sales Register Excel |
| 2 | `GET` | `/api/v1/so/{so_id}` | Get SO with all lines |
| 3 | `GET` | `/api/v1/so/{so_id}/gst-reconciliation` | GST reconciliation for one SO |
| 4 | `GET` | `/api/v1/so/gst-reconciliation/summary` | Global GST summary |
| 5 | `GET` | `/health` | Health check |

---

## 1. Upload Excel — `POST /api/v1/so/upload`

### Request

```
Content-Type: multipart/form-data
```

| Field | Type | Required | Constraints |
|---|---|---|---|
| `file` | File | Yes | `.xlsx` only, max 50 MB |

### Frontend Example (fetch)

```javascript
const formData = new FormData();
formData.append("file", fileInput.files[0]);

const res = await fetch("/api/v1/so/upload", {
  method: "POST",
  body: formData,
});
const data = await res.json();
```

### Response — `201 Created`

```json
{
  "summary": {
    "total_sos": 453,
    "total_lines": 1716,
    "matched_lines": 1716,
    "unmatched_lines": 0,
    "gst_ok": 1373,
    "gst_mismatch": 187,
    "gst_warning": 156
  },
  "sales_orders": [
    {
      "so_id": 1,
      "so_number": "CF-SO/02334",
      "so_date": "2025-02-24",
      "customer_name": "AJFAN INTERNATIONAL RETAILS LLP (Telangana)",
      "common_customer_name": "AJFAN INTERNATIONAL",
      "company": "CFPL",
      "voucher_type": "HO Sales",
      "total_lines": 2,
      "gst_ok": 1,
      "gst_mismatch": 0,
      "gst_warning": 1,
      "lines": [
        {
          "line": {
            "so_line_id": 101,
            "line_number": 1,
            "sku_name": "MEDJOUL DATES JUMBO",
            "item_category": "DATES",
            "sub_category": "Medjoul-Jumbo",
            "uom": "1.0",
            "grp_code": "DATES BULK",
            "quantity": 500.000,
            "quantity_units": 500,
            "rate_inr": 1250.000,
            "rate_type": "per_kg",
            "amount_inr": 625000.000,
            "igst_amount": 31250.000,
            "sgst_amount": 0.000,
            "cgst_amount": 0.000,
            "total_amount_inr": 656250.000,
            "item_type": "rm",
            "item_description": "medjoul dates jumbo",
            "sales_group": "dates bulk",
            "match_score": 1.0,
            "match_source": "all_sku",
            "status": "pending"
          },
          "gst_recon": {
            "so_line_id": 101,
            "line_number": 1,
            "sku_name": "MEDJOUL DATES JUMBO",
            "expected_gst_rate": 0.050,
            "actual_gst_rate": 0.050,
            "expected_gst_amount": 31250.000,
            "actual_gst_amount": 31250.000,
            "gst_difference": 0.000,
            "gst_type": "IGST",
            "gst_type_valid": true,
            "sgst_cgst_equal": null,
            "total_with_gst_valid": true,
            "uom_match": true,
            "item_type_flag": "RM_SOLD",
            "rate_type": "per_kg",
            "matched_item_description": "medjoul dates jumbo",
            "matched_item_type": "rm",
            "matched_item_category": "dates",
            "matched_sub_category": "medjoul-jumbo",
            "matched_sales_group": "dates bulk",
            "matched_uom": 1.000,
            "match_score": 1.000,
            "status": "warning",
            "notes": "Raw Material being sold"
          }
        },
        {
          "line": {
            "so_line_id": 102,
            "line_number": 2,
            "sku_name": "Delmonte Royal Fard Dates 200g",
            "item_category": "DATES",
            "sub_category": "Fard",
            "uom": "0.2",
            "grp_code": "VA",
            "quantity": 180.000,
            "quantity_units": 36,
            "rate_inr": 1985.040,
            "rate_type": "per_unit",
            "amount_inr": 357307.200,
            "igst_amount": 0.000,
            "sgst_amount": 21438.432,
            "cgst_amount": 21438.432,
            "total_amount_inr": 400184.064,
            "item_type": "fg",
            "item_description": "delmonte royal fard dates 200g",
            "sales_group": "va",
            "match_score": 1.000,
            "match_source": "all_sku",
            "status": "pending"
          },
          "gst_recon": {
            "so_line_id": 102,
            "line_number": 2,
            "sku_name": "Delmonte Royal Fard Dates 200g",
            "expected_gst_rate": 0.120,
            "actual_gst_rate": 0.120,
            "expected_gst_amount": 42876.864,
            "actual_gst_amount": 42876.864,
            "gst_difference": 0.000,
            "gst_type": "SGST_CGST",
            "gst_type_valid": true,
            "sgst_cgst_equal": true,
            "total_with_gst_valid": true,
            "uom_match": true,
            "item_type_flag": null,
            "rate_type": "per_unit",
            "matched_item_description": "delmonte royal fard dates 200g",
            "matched_item_type": "fg",
            "matched_item_category": "dates",
            "matched_sub_category": "fard",
            "matched_sales_group": "va",
            "matched_uom": 0.200,
            "match_score": 1.000,
            "status": "ok",
            "notes": null
          }
        }
      ]
    }
  ]
}
```

### Error Responses

| HTTP | Scenario | Body |
|---|---|---|
| `400` | No file attached | `{ "detail": "No file attached." }` |
| `400` | Not `.xlsx` | `{ "detail": "Only Excel files (.xlsx) are accepted." }` |
| `400` | File > 50 MB | `{ "detail": "File too large. Maximum 50 MB." }` |
| `400` | Missing header row | `{ "detail": "Could not find 'Sales Order No.' header in Excel file" }` |
| `500` | Processing error | `{ "detail": "Failed to process Excel file." }` |

---

## 2. Get SO Detail — `GET /api/v1/so/{so_id}`

### Request

```
GET /api/v1/so/1
```

### Response — `200 OK`

```json
{
  "so_id": 1,
  "so_number": "CF-SO/02334",
  "so_date": "2025-02-24",
  "customer_name": "AJFAN INTERNATIONAL RETAILS LLP (Telangana)",
  "common_customer_name": "AJFAN INTERNATIONAL",
  "company": "CFPL",
  "voucher_type": "HO Sales",
  "total_lines": 2,
  "lines": [
    {
      "so_line_id": 101,
      "line_number": 1,
      "sku_name": "MEDJOUL DATES JUMBO",
      "item_category": "DATES",
      "sub_category": "Medjoul-Jumbo",
      "uom": "1.0",
      "grp_code": "DATES BULK",
      "quantity": 500.000,
      "quantity_units": 500,
      "rate_inr": 1250.000,
      "rate_type": "per_kg",
      "amount_inr": 625000.000,
      "igst_amount": 31250.000,
      "sgst_amount": 0.000,
      "cgst_amount": 0.000,
      "total_amount_inr": 656250.000,
      "item_type": "rm",
      "item_description": "medjoul dates jumbo",
      "sales_group": "dates bulk",
      "match_score": 1.000,
      "match_source": "all_sku",
      "status": "pending"
    }
  ]
}
```

### Error

| HTTP | Scenario | Body |
|---|---|---|
| `404` | SO not found | `{ "detail": "SO not found." }` |

---

## 3. GST Reconciliation — `GET /api/v1/so/{so_id}/gst-reconciliation`

### Request

```
GET /api/v1/so/1/gst-reconciliation
```

### Response — `200 OK`

```json
{
  "so_id": 1,
  "total_lines": 2,
  "ok_count": 1,
  "mismatch_count": 0,
  "warning_count": 1,
  "lines": [
    {
      "recon_id": 501,
      "so_line_id": 101,
      "line_number": 1,
      "sku_name": "MEDJOUL DATES JUMBO",
      "expected_gst_rate": 0.050,
      "actual_gst_rate": 0.050,
      "expected_gst_amount": 31250.000,
      "actual_gst_amount": 31250.000,
      "gst_difference": 0.000,
      "gst_type": "IGST",
      "gst_type_valid": true,
      "sgst_cgst_equal": null,
      "total_with_gst_valid": true,
      "uom_match": true,
      "item_type_flag": "RM_SOLD",
      "rate_type": "per_kg",
      "matched_item_description": "medjoul dates jumbo",
      "matched_item_type": "rm",
      "matched_item_category": "dates",
      "matched_sub_category": "medjoul-jumbo",
      "matched_sales_group": "dates bulk",
      "matched_uom": 1.000,
      "match_score": 1.000,
      "status": "warning",
      "notes": "Raw Material being sold"
    }
  ]
}
```

### Error

| HTTP | Scenario | Body |
|---|---|---|
| `404` | SO not found | `{ "detail": "SO not found." }` |

---

## 4. Global GST Summary — `GET /api/v1/so/gst-reconciliation/summary`

### Response — `200 OK`

```json
{
  "total_sos": 453,
  "total_lines": 1716,
  "ok_count": 1373,
  "mismatch_count": 187,
  "warning_count": 156
}
```

---

## 5. Health Check — `GET /health`

### Response — `200 OK`

```json
{ "status": "ok" }
```

---

## Field Reference

### `summary` object (in upload response)

| Field | Type | Description |
|---|---|---|
| `total_sos` | int | Number of unique Sales Orders created |
| `total_lines` | int | Total article lines across all SOs |
| `matched_lines` | int | Lines matched against `all_sku` master |
| `unmatched_lines` | int | Lines with no master match (no item_type, no quantity_units) |
| `gst_ok` | int | Lines where all GST checks passed |
| `gst_mismatch` | int | Lines with GST errors (rate mismatch, total mismatch, etc.) |
| `gst_warning` | int | Lines with non-critical issues (RM/PM sold, UOM mismatch) |

### `line` object (SOLineOut)

| Field | Type | Nullable | Description |
|---|---|---|---|
| `so_line_id` | int | No | DB primary key |
| `line_number` | int | No | Position in SO (1-based) |
| `sku_name` | string | Yes | Article name from Excel |
| `item_category` | string | Yes | Main GRP from Excel |
| `sub_category` | string | Yes | Sub-Group from Excel |
| `uom` | string | Yes | UOM multiplier from Excel (as string) |
| `grp_code` | string | Yes | GRP code from Excel |
| `quantity` | float | Yes | Raw quantity from Excel |
| `quantity_units` | int | Yes | Computed: `quantity × master_uom`. NULL if unmatched |
| `rate_inr` | float | Yes | Rate per unit/kg from Excel |
| `rate_type` | string | Yes | `"per_kg"` if master UOM=1, `"per_unit"` otherwise. NULL if unmatched |
| `amount_inr` | float | Yes | Without GST amount from Excel |
| `igst_amount` | float | Yes | IGST from Excel (0 if intra-state) |
| `sgst_amount` | float | Yes | SGST from Excel (0 if inter-state) |
| `cgst_amount` | float | Yes | CGST from Excel (0 if inter-state) |
| `total_amount_inr` | float | Yes | With GST amount from Excel |
| `item_type` | string | Yes | `"fg"` / `"rm"` / `"pm"` from master match. NULL if unmatched |
| `item_description` | string | Yes | Canonical name from `all_sku`. NULL if unmatched |
| `sales_group` | string | Yes | Sales group from master match. NULL if unmatched |
| `match_score` | float | Yes | 0.0–1.0 fuzzy match confidence. NULL if unmatched |
| `match_source` | string | Yes | Always `"all_sku"` if matched, NULL otherwise |
| `status` | string | No | Always `"pending"` at creation |

### `gst_recon` object (GSTReconLineOut)

| Field | Type | Nullable | Description |
|---|---|---|---|
| `so_line_id` | int | No | Links to the line item |
| `line_number` | int | Yes | Same as line's line_number |
| `sku_name` | string | Yes | Article name for display |
| `expected_gst_rate` | float | Yes | From `all_sku.gst` (e.g. 0.05 = 5%). NULL if unmatched |
| `actual_gst_rate` | float | Yes | Computed: `(igst+sgst+cgst) / amount_inr` |
| `expected_gst_amount` | float | Yes | `amount_inr × expected_gst_rate`. NULL if no expected rate |
| `actual_gst_amount` | float | Yes | `igst + sgst + cgst` from Excel |
| `gst_difference` | float | Yes | `actual - expected`. NULL if no expected rate |
| `gst_type` | string | Yes | `"IGST"` or `"SGST_CGST"` or null |
| `gst_type_valid` | bool | Yes | `true` if not mixed IGST + SGST/CGST |
| `sgst_cgst_equal` | bool | Yes | `true` if SGST == CGST. `null` if IGST type |
| `total_with_gst_valid` | bool | Yes | `true` if `amount + gst == total` (±₹1) |
| `uom_match` | bool | Yes | `true` if Excel UOM == master UOM |
| `item_type_flag` | string | Yes | `null`=FG (ok), `"RM_SOLD"`, `"PM_SOLD"` |
| `rate_type` | string | Yes | `"per_kg"` or `"per_unit"` |
| `matched_item_description` | string | Yes | Matched name from `all_sku` |
| `matched_item_type` | string | Yes | `"fg"` / `"rm"` / `"pm"` from master |
| `matched_item_category` | string | Yes | Group from master |
| `matched_sub_category` | string | Yes | Sub-group from master |
| `matched_sales_group` | string | Yes | Sale group from master |
| `matched_uom` | float | Yes | UOM from master |
| `match_score` | float | Yes | 0.0–1.0 |
| `status` | string | No | `"ok"` / `"mismatch"` / `"warning"` |
| `notes` | string | Yes | Semicolon-separated failure details. NULL if ok |

---

## GST Reconciliation Checks

| # | Check | Status if fails | Example `notes` |
|---|---|---|---|
| 1 | Expected GST rate matches actual | `mismatch` | `"GST amount mismatch: expected 5000.0, actual 12000.0"` |
| 2 | IGST and SGST/CGST not both present | `mismatch` | `"Both IGST and SGST/CGST are non-zero"` |
| 3 | SGST equals CGST (intra-state only) | `mismatch` | `"SGST (3000) != CGST (9000)"` |
| 4 | With GST Amt = Without GST + taxes | `mismatch` | `"With GST Amt (115000) != Without GST (100000) + GST (12000)"` |
| 5 | UOM matches master | `warning` | `"UOM mismatch: Excel=1.0, Master=0.5"` |
| 6 | Item is FG (not RM/PM) | `warning` | `"Raw Material being sold"` or `"Packaging Material being sold"` |

**Priority:** `mismatch` > `warning` > `ok`. If any mismatch check fails, status is `mismatch` regardless of warnings.

---

## Scenarios for Frontend

### Scenario 1: Successful Upload (all OK)

```
summary.gst_ok = 50, gst_mismatch = 0, gst_warning = 0
→ Show green banner: "All 50 lines passed GST checks"
```

### Scenario 2: Upload with Mismatches

```
summary.gst_ok = 40, gst_mismatch = 5, gst_warning = 5
→ Show red badge: "5 GST mismatches"
→ Show orange badge: "5 warnings"
→ Filter/sort lines by gst_recon.status to show issues first
```

### Scenario 3: Unmatched Articles

```
summary.matched_lines = 45, unmatched_lines = 5
→ Unmatched lines have: item_type=null, quantity_units=null, rate_type=null, match_score=null
→ gst_recon for unmatched: expected_gst_rate=null (can't compare)
→ Show yellow indicator: "5 articles not found in master"
```

### Scenario 4: Multiple SOs in One Upload

```
summary.total_sos = 453
→ Each SO is a separate object in sales_orders[]
→ Each SO has its own gst_ok/gst_mismatch/gst_warning counts
→ Show SO list with per-SO status badges
→ Click SO to expand and see lines
```

### Scenario 5: Mismatch Detail Drill-down

When `gst_recon.status = "mismatch"`:

```
notes = "SGST (3000) != CGST (9000); With GST Amt (115000) != Without GST (100000) + GST (12000)"
```

Parse `notes` by `;` to show each issue as a separate row:
- ❌ SGST (3000) != CGST (9000)
- ❌ With GST Amt (115000) != Without GST (100000) + GST (12000)

Also use boolean fields for icons:
- `gst_type_valid: false` → ❌ GST type
- `sgst_cgst_equal: false` → ❌ SGST/CGST
- `total_with_gst_valid: false` → ❌ Total
- `uom_match: false` → ⚠️ UOM
- `item_type_flag: "RM_SOLD"` → ⚠️ Item type

### Scenario 6: Comparing Excel vs Master

For each line, the frontend can show side-by-side:

| Field | From Excel (line) | From Master (gst_recon.matched_*) |
|---|---|---|
| Article name | `line.sku_name` | `gst_recon.matched_item_description` |
| Category | `line.item_category` | `gst_recon.matched_item_category` |
| Sub-category | `line.sub_category` | `gst_recon.matched_sub_category` |
| UOM | `line.uom` | `gst_recon.matched_uom` |
| Sales group | `line.sales_group` | `gst_recon.matched_sales_group` |
| GST rate | `gst_recon.actual_gst_rate` | `gst_recon.expected_gst_rate` |
| Item type | — | `gst_recon.matched_item_type` |
| Match confidence | — | `gst_recon.match_score` (0–1) |

### Scenario 7: Low Match Confidence

```
match_score < 0.85 → Show orange "Low confidence" badge
match_score >= 0.85 and < 1.0 → Show yellow "Partial match"
match_score == 1.0 → Show green "Exact match"
match_score == null → Show red "No match"
```

---

## Data Flow Diagram

```
┌──────────────────────┐
│  Frontend: File      │
│  Upload (.xlsx)      │
└──────────┬───────────┘
           │ POST /api/v1/so/upload
           ▼
┌──────────────────────┐
│  Parse Excel         │
│  (openpyxl, memory)  │
└──────────┬───────────┘
           │ Group by Sales Order No.
           ▼
┌──────────────────────┐     ┌─────────────────┐
│  For each SO:        │     │  all_sku table   │
│  ├─ INSERT so_header │     │  (3654 master    │
│  ├─ For each line:   │     │   items in       │
│  │  ├─ Fuzzy match ──┼────►│   memory)        │
│  │  │  article name  │     └─────────────────┘
│  │  ├─ Compute       │
│  │  │  quantity_units │
│  │  │  rate_type      │
│  │  ├─ INSERT so_line │
│  │  └─ GST recon ────┼────► INSERT so_gst_reconciliation
│  └─ Collect results  │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Response:           │
│  {                   │
│    summary: {...},   │
│    sales_orders: [   │
│      { lines: [      │
│        { line, recon}│
│      ]}              │
│    ]                 │
│  }                   │
└──────────────────────┘
           │
           ▼
┌──────────────────────┐
│  Frontend: Display   │
│  ├─ Summary cards    │
│  ├─ SO list          │
│  ├─ Line details     │
│  ├─ GST check status │
│  └─ Mismatch details │
└──────────────────────┘
```

---

## Database Schema

```
all_sku (3654 rows, read-only master)
┌─────────────────────┐
│ sku_id (PK)         │
│ particulars          │ ◄── fuzzy match key
│ item_type            │
│ item_group           │
│ sub_group            │
│ uom  NUMERIC(15,3)  │
│ sale_group           │
│ gst  NUMERIC(15,3)  │
└─────────────────────┘

so_header
┌─────────────────────┐
│ so_id (PK)          │◄──────────────────┐
│ so_number            │                   │
│ so_date              │                   │
│ customer_name        │                   │
│ common_customer_name │                   │
│ company              │                   │
│ voucher_type         │                   │
│ extraction_status    │                   │
│ created_at           │                   │
└─────────────────────┘                   │
                                          │
so_line                                   │
┌─────────────────────┐                   │
│ so_line_id (PK)     │◄──────┐           │
│ so_id (FK) ─────────┼───────┼───────────┘
│ line_number          │       │
│ sku_name             │       │
│ item_category        │       │
│ sub_category         │       │
│ uom                  │       │
│ grp_code             │       │
│ quantity             │       │
│ quantity_units       │       │
│ rate_inr             │       │
│ rate_type            │       │
│ amount_inr           │       │
│ igst/sgst/cgst_amount│       │
│ total_amount_inr     │       │
│ item_type            │       │
│ item_description     │       │
│ sales_group          │       │
│ match_score          │       │
│ match_source         │       │
│ status               │       │
└─────────────────────┘       │
                              │
so_gst_reconciliation         │
┌─────────────────────┐       │
│ recon_id (PK)       │       │
│ so_line_id (FK) ────┼───────┘
│ so_id (FK)          │
│ expected_gst_rate    │
│ actual_gst_rate      │
│ expected/actual_amt  │
│ gst_difference       │
│ gst_type             │
│ gst_type_valid       │
│ sgst_cgst_equal      │
│ total_with_gst_valid │
│ uom_match            │
│ item_type_flag       │
│ rate_type            │
│ matched_*  (7 fields)│
│ status               │
│ notes                │
└─────────────────────┘
```

All numeric fields use `NUMERIC(15,3)` — 3 decimal places, padded with zeros.
