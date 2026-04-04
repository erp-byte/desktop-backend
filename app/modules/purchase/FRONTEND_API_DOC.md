# Purchase Module — Frontend API Documentation

**Base URL:** `/api/v1/purchase`

---

## Two Teams, One Module

| Team | Role | Endpoints | Fields They Own |
|------|------|-----------|-----------------|
| **Purchase Team** | Uploads PO Book Excel | `/upload`, `/view`, `/export`, `/summary`, `/{transaction_no}` | All Excel-extracted fields + SKU-matched fields |
| **Stores Team** | Fills receiving data when material arrives | `/{transaction_no}/receive`, `/{transaction_no}/boxes` | Logistics, dates, weights, boxes |

**Rule:** Stores Team endpoints NEVER modify Purchase Team fields. Purchase Team fields are immutable after upload.

---

## ID Generation (Frontend Responsibility)

### transaction_no

**Format:** `TR-YYYYMMDDHHMMSS`

```javascript
function generateTransactionNo() {
  const now = new Date();
  const pad = (n, len = 2) => String(n).padStart(len, '0');
  return `TR-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}
// Example: "TR-20260322143052"
```

### line_number

Sequential integer `1` to `N`, resets to `1` for each new transaction.

```javascript
const lines = articles.map((article, index) => ({
  ...article,
  line_number: index + 1,
}));
```

### box_id

**Format:** `{last 8 digits of epoch ms}-{counter}`

```javascript
function generateBoxIds(boxes) {
  const base = String(Date.now()).slice(-8);
  return boxes.map((box, i) => ({
    ...box,
    box_id: `${base}-${i + 1}`,
  }));
}
// Example: "23456789-1", "23456789-2", "23456789-3"
```

**How it works:**
1. **Base:** `String(Date.now()).slice(-8)` → last 8 digits of epoch milliseconds (e.g. `1711123456789` → `23456789`)
2. **Counter:** Sequential starting from 1
3. **Format:** `{base}-{counter}` → `23456789-1`, `23456789-2`, etc.

---

## Calculations

### po_weight (pre-receiving weight estimate)

```
po_weight = pack_count × uom
```

- `pack_count` = number of packs (boxes/cartons/bags) from Excel "Quantity" column
- `uom` = unit weight from `all_sku` master table (e.g. 1.0 = 1 kg per unit)
- Computed at upload time by backend
- Example: 100 cartons × 1.0 kg/unit = 100.0 kg

### total_tax (in summary)

```
total_tax = SUM(sgst_amount + cgst_amount + igst_amount) across all filtered POs
```

### total_net_weight (in summary)

```
total_net_weight = SUM(po_weight) across all filtered PO lines
```

---

## Endpoints

---

### 1. POST /upload — Upload PO Book Excel

**Who:** Purchase Team

Upload a Purchase Order Book Excel file. Backend parses header/line items, extracts vendor name from header rows, and matches article names against the `all_sku` master table.

**Request:** `multipart/form-data`

| Param | Location | Type | Required | Notes |
|-------|----------|------|----------|-------|
| file | body | .xlsx/.xls | Yes | Max 50 MB |
| entity | query | string | Yes | `cfpl` or `cdpl` |

**What gets extracted from Excel:**

| Level | Field | Excel Column | Coverage |
|-------|-------|-------------|----------|
| Header | po_date | Col A (Date) | 100% |
| Header | vendor_supplier_name | Col B (Particulars on header row) | 100% |
| Header | voucher_type | Col C (Voucher Type) | 100% |
| Header | po_number | Col D (Voucher No.) | 100% |
| Header | order_reference_no | Col E (Order Reference No.) | 100% |
| Header | narration | Col F (Narration) | ~1% |
| Header | total_amount | Col M (Value on header row) | 100% |
| Header | gross_total | Col N (Gross Total) | 100% |
| Header | sgst_amount | Col P (SGST ITC) | ~80% |
| Header | cgst_amount | Col Q (CGST ITC) | ~80% |
| Header | round_off | Col R (Round Off) | ~1% |
| Header | igst_amount | Col U (IGST ITC) | ~15% |
| Header | packing_charges | Col X (Packing Charges) | ~8% |
| Header | freight_transport_local | Col AA (Freight Local) | ~1.5% |
| Header | apmc_tax | Col AB (APMC Tax) | ~6% |
| Header | other_charges_non_gst | Col AC (Other Charges) | ~1% |
| Header | freight_transport_charges | Col AO (Freight & Transport) | ~0.1% |
| Header | loading_unloading_charges | Col AQ (Loading/Unloading) | ~1% |
| Line | sku_name | Col B (Particulars on line row) | 100% |
| Line | pack_count | Col J (Quantity) | 100% |
| Line | uom | Col K (Alt. Units) | ~1.5% |
| Line | rate | Col L (Rate) | 100% |
| Line | amount | Col M (Value) | 100% |

**What gets matched from all_sku (at upload time):**

| po_line Field | all_sku Source | Notes |
|---------------|---------------|-------|
| particulars | `all_sku.particulars` | Matched item description |
| item_category | `all_sku.item_group` | e.g. "seeds", "cashew", "dates" |
| sub_category | `all_sku.sub_group` | e.g. "chia", "cashew - kernel" |
| item_type | `all_sku.item_type` | `rm` (Raw Material), `pm` (Packing), `fg` (Finished Good) |
| sales_group | `all_sku.sale_group` | e.g. "bulk", "retail" |
| gst_rate | `all_sku.gst` | e.g. 0.05 (5%) |
| match_score | computed | 0.0 to 1.0 fuzzy match confidence |
| match_source | computed | `"all_sku"` if matched, `null` if not |
| po_weight | computed | `pack_count × all_sku.uom` |

**Response (201):**
```json
{
  "summary": {
    "total_transactions": 751,
    "total_lines": 1502,
    "total_boxes": 0,
    "total_amount": 598710350.14,
    "total_tax": null,
    "total_net_weight": null
  },
  "transactions": [
    {
      "transaction_no": "CFPL-CF/PO/2025-26/03257",
      "entity": "cfpl",
      "po_date": "2026-01-01",
      "voucher_type": "HO Purchase Order",
      "po_number": "CF/PO/2025-26/03257",
      "order_reference_no": "CF/PO/2025-26/03257",
      "narration": null,
      "vendor_supplier_name": "Platinum",
      "gross_total": 48025.0,
      "total_amount": 41350.0,
      "sgst_amount": 3662.93,
      "cgst_amount": 3662.93,
      "igst_amount": null,
      "round_off": 0.01,
      "freight_transport_local": null,
      "apmc_tax": null,
      "packing_charges": null,
      "freight_transport_charges": null,
      "loading_unloading_charges": null,
      "other_charges_non_gst": null,
      "customer_party_name": null,
      "vehicle_number": null,
      "transporter_name": null,
      "lr_number": null,
      "source_location": null,
      "destination_location": null,
      "challan_number": null,
      "invoice_number": null,
      "grn_number": null,
      "system_grn_date": null,
      "purchased_by": null,
      "inward_authority": null,
      "warehouse": null,
      "status": "pending",
      "approved_by": null,
      "approved_at": null,
      "total_lines": 17,
      "total_boxes": 0,
      "lines": [
        {
          "transaction_no": "CFPL-CF/PO/2025-26/03257",
          "line_number": 1,
          "sku_name": "Black Chia Seeds",
          "uom": "1.0",
          "pack_count": 10000,
          "po_weight": 10000.0,
          "rate": 247.0,
          "amount": 2470000.0,
          "particulars": "black chia seeds",
          "item_category": "seeds",
          "sub_category": "chia",
          "item_type": "rm",
          "sales_group": "bulk",
          "gst_rate": 0.05,
          "match_score": 1.0,
          "match_source": "all_sku",
          "carton_weight": null,
          "status": "pending",
          "total_sections": 0,
          "total_boxes": 0,
          "sections": []
        }
      ]
    }
  ]
}
```

**Errors:**

| Code | Condition |
|------|-----------|
| 400 | No file / not Excel / too large |
| 409 | Duplicate transaction_no (PO already uploaded) |
| 500 | Parse/DB failure |

---

### 2. GET /view — Paginated View

**Who:** Both teams

**Query Parameters:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| page | int (>=1) | 1 | |
| page_size | int (1-100) | 50 | |
| search | string | null | Searches: transaction_no, vendor, customer, po_number, invoice_number |
| entity | string | null | `cfpl` or `cdpl` |
| sort_by | string | `po_date` | One of: `transaction_no`, `po_date`, `vendor_supplier_name`, `customer_party_name`, `gross_total`, `warehouse` |
| sort_order | string | `desc` | `asc` or `desc` |
| vendor | string | null | Comma-separated for multi-select |
| customer | string | null | Comma-separated |
| date_from | string | null | `YYYY-MM-DD` (auto-swaps if reversed) |
| date_to | string | null | `YYYY-MM-DD` |
| status | string | null | Comma-separated |
| warehouse | string | null | Comma-separated |
| item_category | string | null | Comma-separated (line-level filter via EXISTS) |
| sub_category | string | null | Comma-separated (line-level filter) |
| item_type | string | null | Comma-separated (line-level filter, e.g. `rm,pm`) |

**Response (200):**
```json
{
  "page": 1,
  "page_size": 50,
  "total": 751,
  "total_pages": 16,
  "summary": {
    "total_transactions": 751,
    "total_lines": 1502,
    "total_boxes": 0,
    "total_amount": 598710350.14,
    "total_tax": 45230120.5,
    "total_net_weight": 125000.0
  },
  "filter_options": {
    "entities": ["cfpl"],
    "voucher_types": ["HO Purchase Order", "Purchase Order"],
    "vendors": ["Platinum", "Star Foods", "..."],
    "customers": [],
    "warehouses": [],
    "statuses": ["pending"],
    "item_categories": ["almond", "cashew", "dates", "seeds", "..."],
    "sub_categories": ["almond - kernels", "cashew - broken", "chia", "..."],
    "item_types": ["fg", "pm", "rm"]
  },
  "transactions": [ /* array of POHeaderOut — same shape as upload response */ ]
}
```

---

### 3. GET /export — Export All (No Pagination)

**Who:** Both teams

Same filters as `/view`, but returns ALL matching records.

**Query Parameters:** Same as `/view` minus `page` and `page_size`.

**Response (200):**
```json
{
  "total": 751,
  "transactions": [ /* full array of POHeaderOut */ ]
}
```

---

### 4. GET /summary — Aggregate Stats

**Who:** Both teams

**Query Parameters:** Same filter params as `/view` minus pagination/sorting.

**Response (200):**
```json
{
  "total_transactions": 751,
  "total_lines": 1502,
  "total_boxes": 0,
  "total_amount": 598710350.14,
  "total_tax": 45230120.5,
  "total_net_weight": 125000.0
}
```

---

### 5. GET /{transaction_no} — Single PO Detail

**Who:** Both teams

**Path Parameters:**

| Param | Type | Example |
|-------|------|---------|
| transaction_no | string | `CFPL-CF/PO/2025-26/03257` |

**Response (200):** Full `POHeaderOut` object (same shape as items in the transactions array).

**Error (404):**
```json
{ "detail": "Transaction not found." }
```

---

### 6. PUT /{transaction_no}/receive — Stores Team: Fill Receiving Data

**Who:** Stores Team only

Updates ONLY Stores-owned fields on the header and lines. Uses `COALESCE` — only overwrites fields you provide, leaves everything else untouched.

**Safety guarantees:**
- Never touches Purchase Team fields (po_date, vendor, amounts, SKU matches, etc.)
- If you send `null` for a field, it keeps the existing value
- If you send a value, it overwrites (Stores correcting their own data)

**Request Body:**
```json
{
  "header": {
    "customer_party_name": "DMART",
    "vehicle_number": "MH12AB1234",
    "transporter_name": "Blue Dart Logistics",
    "lr_number": "LR-2026-00123",
    "source_location": "Mumbai",
    "destination_location": "Pune Warehouse",
    "challan_number": "CH-456",
    "invoice_number": "INV-2026-789",
    "grn_number": "GRN-001",
    "system_grn_date": "2026-03-25T10:30:00Z",
    "purchased_by": "Rajesh Kumar",
    "inward_authority": "Amit Shah",
    "warehouse": "W202"
  },
  "lines": [
    {
      "line_number": 1,
      "carton_weight": 25.5
    },
    {
      "line_number": 2,
      "carton_weight": 30.0
    }
  ]
}
```

**All header fields are optional.** Send only what you have:
```json
{
  "header": {
    "vehicle_number": "MH12AB1234",
    "warehouse": "W202"
  },
  "lines": []
}
```

**Request Schema:**

`header` (all optional):

| Field | Type | Notes |
|-------|------|-------|
| customer_party_name | string | Buyer party |
| vehicle_number | string | |
| transporter_name | string | |
| lr_number | string | Lorry receipt number |
| source_location | string | |
| destination_location | string | |
| challan_number | string | |
| invoice_number | string | |
| grn_number | string | Goods Receipt Note |
| system_grn_date | string | ISO datetime |
| purchased_by | string | |
| inward_authority | string | Who authorized the inward |
| warehouse | string | |

`lines` (array, each requires `line_number`):

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| line_number | int | Yes | Must match existing line |
| carton_weight | float | No | Weight per carton in kg |

> **Note:** `manufacturing_date` and `expiry_date` have moved to the **section level** (per lot). See the boxes endpoint below.

**Response (200):** Full updated `POHeaderOut` with all lines and boxes.

**Error (404):** Transaction not found.

---

### 7. PUT /{transaction_no}/boxes — Stores Team: Update Existing Sections & Boxes

**Who:** Stores Team only

Updates existing sections and their boxes. Uses `COALESCE` — only overwrites fields you provide, leaves others untouched. Identifies sections by `(line_number, section_number)` and boxes by `box_id`.

**When to call:** Step 2a of Save — before creating new sections (POST). Send only sections/boxes that have changed.

**Request Body:**
```json
{
  "sections": [
    {
      "line_number": 1,
      "section_number": 1,
      "box_count": 100,
      "lot_number": "LOT-2026-001",
      "manufacturing_date": "2026-03-01",
      "expiry_date": "2027-03-01",
      "boxes": [
        {
          "box_id": "97598567-1",
          "box_number": 1,
          "net_weight": 10.5,
          "gross_weight": 11.2,
          "lot_number": "LOT-2026-001",
          "count": 52
        }
      ]
    }
  ]
}
```

**Section fields:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| line_number | int | Yes | Identifies which line |
| section_number | int | Yes | Identifies which section (from GET response) |
| box_count | int | No | Updated expected box count |
| lot_number | string | No | Updated lot number |
| manufacturing_date | string | No | Updated manufacturing date |
| expiry_date | string | No | Updated expiry date |
| boxes | array | No | Boxes to update (only changed ones) |

**Box fields (inside each section):**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| box_id | string | Yes | Identifies which box to update |
| box_number | int | No | |
| net_weight | float | No | |
| gross_weight | float | No | |
| lot_number | string | No | |
| count | int | No | |

**Response (200):** Full updated `POHeaderOut` with lines → sections → boxes nested.

**Error (404):** Transaction not found.
**Error (400):** Empty sections array.

---

### 8. POST /{transaction_no}/boxes — Stores Team: Add New Sections with Boxes

**Who:** Stores Team only

Append **new** sections (lot groupings) with box weight records. **Never deletes existing sections/boxes** — only inserts new ones. Each section represents a lot for an article line, containing `manufacturing_date`, `expiry_date`, and the boxes belonging to that lot.

**When to call:** Step 2b of Save — after updating existing sections (PUT). Only send sections that are brand new.

**Data Hierarchy:**
```
sections[] → each section groups boxes by lot for one article line
  ├── line_number    (which article)
  ├── lot_number     (manufacturer lot/batch)
  ├── box_count      (expected total boxes for this lot)
  ├── manufacturing_date
  ├── expiry_date
  └── boxes[]        (individual box weights)
```

Multiple sections can reference the **same `line_number`** — this is how you record multiple lots for one article.

**Request Body:**
```json
{
  "sections": [
    {
      "line_number": 1,
      "box_count": 100,
      "lot_number": "LOT-2026-001",
      "manufacturing_date": "2026-03-01",
      "expiry_date": "2027-03-01",
      "boxes": [
        {
          "box_id": "97598567-1",
          "box_number": 1,
          "net_weight": 10.0,
          "gross_weight": 10.7,
          "lot_number": "LOT-2026-001",
          "count": 50
        },
        {
          "box_id": "97598567-2",
          "box_number": 2,
          "net_weight": 9.8,
          "gross_weight": 10.5,
          "lot_number": "LOT-2026-001",
          "count": 50
        }
      ]
    },
    {
      "line_number": 1,
      "box_count": 300,
      "lot_number": "LOT-2026-002",
      "manufacturing_date": "2026-04-15",
      "expiry_date": "2027-04-15",
      "boxes": [
        {
          "box_id": "97598567-101",
          "box_number": 101,
          "net_weight": 12.0,
          "gross_weight": 12.8,
          "lot_number": "LOT-2026-002",
          "count": 60
        }
      ]
    }
  ]
}
```

**Section fields:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| line_number | int | Yes | Must match existing po_line |
| box_count | int | No | Expected total boxes for this lot |
| lot_number | string | No | Manufacturer lot/batch number |
| manufacturing_date | string | No | e.g. `"2026-03-01"` |
| expiry_date | string | No | e.g. `"2027-03-01"` |
| boxes | array | No | Box records for this section (can be empty) |

**Box fields (inside each section):**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| box_id | string | Yes | Frontend-generated: `{epoch_last8}-{i}` |
| box_number | int | Yes | Sequence within article (1, 2, 3...) |
| net_weight | float | No | Net weight in kg |
| gross_weight | float | No | Gross weight in kg |
| lot_number | string | No | Same as section lot_number |
| count | int | No | Number of items in box |

> **Note:** `line_number` is on the section, not on each box. All boxes in a section inherit the section's `line_number`.

**Response (201):** Full updated `POHeaderOut` with lines → sections → boxes nested.

**Response shape (sections nested in lines):**
```json
{
  "lines": [
    {
      "line_number": 1,
      "sku_name": "Black Chia Seeds",
      "total_sections": 2,
      "total_boxes": 3,
      "sections": [
        {
          "transaction_no": "TR-20260324...",
          "line_number": 1,
          "section_number": 1,
          "lot_number": "LOT-2026-001",
          "box_count": 100,
          "manufacturing_date": "2026-03-01",
          "expiry_date": "2027-03-01",
          "total_boxes": 2,
          "boxes": [
            {
              "box_id": "97598567-1",
              "box_number": 1,
              "section_number": 1,
              "net_weight": 10.0,
              "gross_weight": 10.7,
              "lot_number": "LOT-2026-001",
              "count": 50
            }
          ]
        },
        {
          "section_number": 2,
          "lot_number": "LOT-2026-002",
          "box_count": 300,
          "manufacturing_date": "2026-04-15",
          "expiry_date": "2027-04-15",
          "total_boxes": 1,
          "boxes": [...]
        }
      ]
    }
  ]
}
```

**`section_number`** is auto-assigned by the backend (1, 2, 3... per line). Frontend does NOT send it.

**Error (404):** Transaction not found.
**Error (400):** Empty sections array.

---

### 9. GET /api/v1/so/sku-lookup — SKU Master Lookup

**Who:** Both teams (for manual line item selection)

Returns filtered dropdown options from the `all_sku` master table (3,654 items). Supports cascading: selecting one field filters others.

**Query Parameters:**

| Param | Type | Notes |
|-------|------|-------|
| item_type | string | Exact match (case/space insensitive) |
| item_group | string | Exact match |
| sub_group | string | Exact match |
| sales_group | string | Exact match |
| search | string | Space-tolerant search on particulars |
| particulars | string | Returns full SKU detail when provided |

**Response (200):**
```json
{
  "options": {
    "item_types": ["fg", "pm", "rm"],
    "particulars": ["black chia seeds", "cashew 320", "..."],
    "item_groups": ["almond", "cashew", "dates", "seeds"],
    "sub_groups": ["almond - kernels", "cashew - broken", "chia"],
    "sales_groups": ["bulk", "dates bulk", "retail"]
  },
  "selected_item": null
}
```

When `particulars` is provided:
```json
{
  "options": { /* filtered */ },
  "selected_item": {
    "sku_id": 42,
    "particulars": "black chia seeds",
    "item_type": "rm",
    "item_group": "seeds",
    "sub_group": "chia",
    "uom": 1.0,
    "sale_group": "bulk",
    "gst": 0.05
  }
}
```

**Cascading flow:**
1. Select `item_type=rm` → API returns only RM items in all dropdowns
2. Select `item_group=seeds` → further filtered
3. Select `particulars=black chia seeds` → `selected_item` returned with full detail
4. Use `selected_item.uom` to compute `po_weight = pack_count × uom`

---

## Complete Data Flow

### Flow 1: Purchase Team — Excel Upload

```
┌─────────────┐     POST /upload?entity=cfpl      ┌─────────────────┐
│  Frontend    │ ──── (multipart: Excel file) ───→ │     Backend      │
│  (Purchase)  │                                    │                  │
│              │                                    │  1. Parse Excel  │
│              │                                    │     - Headers    │
│              │                                    │     - Lines      │
│              │                                    │     - GL amounts │
│              │                                    │  2. Extract      │
│              │                                    │     vendor from  │
│              │                                    │     Particulars  │
│              │                                    │  3. Match each   │
│              │                                    │     sku_name vs  │
│              │                                    │     all_sku      │
│              │                                    │  4. Compute      │
│              │                                    │     po_weight =  │
│              │                                    │     pack_count   │
│              │                                    │     × uom        │
│              │                                    │  5. INSERT       │
│              │  ←── 201 { summary, transactions } │     po_header +  │
│              │                                    │     po_line      │
└─────────────┘                                    └─────────────────┘
```

### Flow 2: Stores Team — Material Receiving

```
┌─────────────┐                                    ┌─────────────────┐
│  Frontend    │  GET /{transaction_no}             │     Backend      │
│  (Stores)    │ ─────────────────────────────────→ │                  │
│              │  ←── 200 { current PO state }      │                  │
│              │                                    │                  │
│  Fill in:    │                                    │                  │
│  - vehicle   │  Step 1: PUT /{txn}/receive        │                  │
│  - transport │ ─────────────────────────────────→ │  UPDATE header   │
│  - GRN       │  { header: {...}, lines: [...] }   │  + line fields   │
│  - warehouse │  ←── 200 { updated PO }            │  via COALESCE    │
│  - weights   │                                    │                  │
│              │  Step 2a: PUT /{txn}/boxes          │                  │
│  Edit        │ ─────────────────────────────────→ │  UPDATE existing │
│  existing    │  { sections: [{section_number,     │  sections + boxes│
│  sections    │     boxes: [{box_id, ...}]}] }     │  via COALESCE    │
│  + boxes     │  ←── 200 { updated PO }            │                  │
│              │                                    │                  │
│  Generate    │  Step 2b: POST /{txn}/boxes        │                  │
│  box_ids:    │ ─────────────────────────────────→ │  INSERT new      │
│  base = last │  { sections: [{lot, boxes}, ...] } │  sections + boxes│
│  8 of epoch  │  ←── 201 { updated PO }            │  (append only)   │
└─────────────┘                                    └─────────────────┘
```

### Flow 3: Manual PO Creation (Future)

```
┌─────────────┐                                    ┌─────────────────┐
│  Frontend    │  GET /api/v1/so/sku-lookup         │     Backend      │
│              │ ─────────────────────────────────→ │                  │
│  Cascading   │  ←── { options, selected_item }    │  Query all_sku   │
│  dropdowns:  │                                    │                  │
│  item_type   │                                    │                  │
│  → item_group│  Generate:                         │                  │
│  → sub_group │  - transaction_no = TR-YYYYMMDD... │                  │
│  → particular│  - line_numbers = 1, 2, 3...      │                  │
│              │  - po_weight = pack_count × uom    │                  │
│              │                                    │                  │
│              │  POST /create (future endpoint)    │                  │
│              │ ─────────────────────────────────→ │                  │
└─────────────┘                                    └─────────────────┘
```

---

## Field Ownership Matrix

### po_header

| Field | Purchase Team | Stores Team | Editable By |
|-------|:---:|:---:|-------------|
| transaction_no | ✓ (from Excel) | | Immutable |
| entity | ✓ | | Immutable |
| po_date | ✓ | | Immutable |
| voucher_type | ✓ | | Immutable |
| po_number | ✓ | | Immutable |
| order_reference_no | ✓ | | Immutable |
| narration | ✓ | | Immutable |
| vendor_supplier_name | ✓ | | Immutable |
| gross_total | ✓ | | Immutable |
| total_amount | ✓ | | Immutable |
| sgst/cgst/igst_amount | ✓ | | Immutable |
| round_off | ✓ | | Immutable |
| GL accounts (6 fields) | ✓ | | Immutable |
| customer_party_name | | ✓ | Stores only |
| vehicle_number | | ✓ | Stores only |
| transporter_name | | ✓ | Stores only |
| lr_number | | ✓ | Stores only |
| source/dest_location | | ✓ | Stores only |
| challan_number | | ✓ | Stores only |
| invoice_number | | ✓ | Stores only |
| grn_number | | ✓ | Stores only |
| system_grn_date | | ✓ | Stores only |
| purchased_by | | ✓ | Stores only |
| inward_authority | | ✓ | Stores only |
| warehouse | | ✓ | Stores only |

### po_line

| Field | Purchase Team | Stores Team | Editable By |
|-------|:---:|:---:|-------------|
| sku_name | ✓ | | Immutable |
| uom | ✓ | | Immutable |
| pack_count | ✓ | | Immutable |
| po_weight | ✓ (computed) | | Immutable |
| rate | ✓ | | Immutable |
| amount | ✓ | | Immutable |
| particulars | ✓ (matched) | | Immutable |
| item_category | ✓ (matched) | | Immutable |
| sub_category | ✓ (matched) | | Immutable |
| item_type | ✓ (matched) | | Immutable |
| sales_group | ✓ (matched) | | Immutable |
| gst_rate | ✓ (matched) | | Immutable |
| match_score | ✓ (computed) | | Immutable |
| match_source | ✓ (computed) | | Immutable |
| carton_weight | | ✓ | Stores only |

### po_section (Stores Team only — lot grouping)

| Field | Editable By | Notes |
|-------|-------------|-------|
| section_number | Auto-assigned | 1, 2, 3... per line |
| lot_number | Stores only | Manufacturer batch number |
| box_count | Stores only | Expected boxes for this lot |
| manufacturing_date | Stores only | Moved from po_line |
| expiry_date | Stores only | Moved from po_line |

### po_box (Stores Team only)

| Field | Editable By | Notes |
|-------|-------------|-------|
| All fields | Stores only | Append-only, no delete |
| section_number | Auto-inherited | From parent section |

---

## Decimal Precision

All numeric fields use `Decimal3` — rounded to **3 decimal places**.

| Type | DB Type | Frontend Display |
|------|---------|-----------------|
| Monetary amounts | `NUMERIC(15,3)` | 2 decimal places |
| Weights (kg) | `NUMERIC(15,3)` | 3 decimal places |
| GST rates | `NUMERIC(15,3)` | Percentage (× 100) |
| Match scores | `NUMERIC(5,3)` | 0.000 to 1.000 |

---

## Null Handling

- All numeric fields can be `null` (not yet filled)
- `null` = "not yet provided" — do NOT convert to `0` on the frontend
- When sending Stores data, omit fields or send `null` to keep existing values (COALESCE behavior)
- Backend preserves `null` in all response payloads

---

## Error Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 201 | Created (upload, boxes) |
| 400 | Bad request (invalid file, invalid date format, empty boxes) |
| 404 | Transaction not found |
| 409 | Duplicate transaction_no |
| 500 | Server error |
