# SO Upload & Extraction Pipeline — Complete Flow

## Overview

This system takes customer Sales Order PDFs, extracts structured data via Claude Vision, fuzzy-matches line items against Candor's master inventory, and stores everything in PostgreSQL. Upload returns immediately; extraction runs async via a background worker.

---

## Architecture

```
Client (PDF upload)
  │
  ▼
FastAPI App ── POST /api/v1/so/upload
  │
  ├─ 1. Validate PDF (6 checks)
  ├─ 2. Save file to local storage
  ├─ 3. INSERT so_header (status = pending)
  ├─ 4. Enqueue ExtractionMessage
  └─ 5. Return { so_id, poll_url }

Background Worker (runs in-process via asyncio.Task)
  │
  ├─ 6. Dequeue message
  ├─ 7. Read PDF from storage
  ├─ 8. Convert PDF pages → JPEG images (PyMuPDF, 200 DPI)
  ├─ 9. Call Claude API (vision) → raw JSON text
  ├─ 10. Parse + clean JSON response
  ├─ 11. Fuzzy-match each line against categorial_inv
  ├─ 12. Compute quantity_units = quantity × UOM
  ├─ 13. UPDATE so_header (status = extracted)
  ├─ 14. INSERT so_line rows
  └─ 15. INSERT log_edit audit rows

Client (poll)
  │
  ▼
FastAPI App ── GET /api/v1/so/{id}/extraction
  │
  └─ Returns: pending | extracted (with lines) | failed
```

---

## API Endpoints

### 1. Upload — `POST /api/v1/so/upload`

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | File (multipart) | Yes | PDF file, max 20 MB |
| `customer_hint` | string (form) | No | Free-text hint for Claude (e.g. "D-Mart") |

**Response (201):**
```json
{
  "so_id": 142,
  "status": "pending",
  "filename": "SO-DM-Mar2026.pdf",
  "stored_path": "so_pdfs/2026/03/142/a3f1c2d8_SO-DM-Mar2026.pdf",
  "file_size_kb": 384.0,
  "queued_at": "2026-03-16T10:32:00Z",
  "poll_url": "/api/v1/so/142/extraction",
  "message": "File uploaded. Extraction queued. Poll poll_url for result."
}
```

**Error Responses:**

| Scenario | HTTP | Error Code |
|---|---|---|
| No file attached | 400 | `SO_FILE_MISSING` |
| Not a PDF (MIME/extension/magic bytes) | 400 | `SO_FILE_INVALID` |
| File > 20 MB | 400 | `SO_FILE_TOO_LARGE` |
| File < 1 KB | 400 | `SO_FILE_INVALID` |

### 2. Poll — `GET /api/v1/so/{so_id}/extraction`

Returns one of three shapes based on `extraction_status`:

**Pending:**
```json
{
  "so_id": 142,
  "status": "pending",
  "message": "Extraction is still in progress.",
  "queued_at": "2026-03-16T10:32:00Z",
  "elapsed_seconds": 4
}
```

**Extracted:**
```json
{
  "so_id": 142,
  "status": "extracted",
  "so_number": "CF-SO_01486",
  "so_date": "2026-03-15",
  "customer_name_extracted": "D-Mart (Avenue Supermarts Ltd.)",
  "customer_id": null,
  "extracted_at": "2026-03-16T10:32:08Z",
  "extraction_confidence": "high",
  "extraction_notes": null,
  "total_lines_extracted": 3,
  "lines": [ ... ]
}
```

**Failed:**
```json
{
  "so_id": 142,
  "status": "failed",
  "error_code": "EXTRACTION_FAILED",
  "error_detail": "Claude API returned invalid JSON after 3 attempts.",
  "failed_at": "2026-03-16T10:34:22Z",
  "can_retry": true,
  "retry_url": "POST /api/v1/so/142/extract"
}
```

### 3. Re-trigger — `POST /api/v1/so/{so_id}/extract`

Re-runs extraction. Deletes existing `so_line` rows, resets status to `pending`, re-enqueues.

| Guard | HTTP | Error |
|---|---|---|
| SO already approved | 409 | `SO_ALREADY_APPROVED` |
| Extraction already running | 409 | `EXTRACTION_ALREADY_RUNNING` |

---

## Line Item Fields (SOLineOut)

Each line in the poll response contains:

### From Claude Extraction (PDF)

| Field | Type | Description |
|---|---|---|
| `so_line_id` | int | Auto-generated DB primary key |
| `line_number` | int | Sequential position (1-based) |
| `article_code` | string? | Candor's SKU code (e.g. `CFC-128`) |
| `customer_article_code` | string? | Buyer's internal code |
| `sku_name` | string? | Product name verbatim from PDF |
| `quantity` | float? | Raw quantity extracted |
| `uom` | string? | Unit of measure (PCS, CTN, KG, etc.) |
| `mrp_inr` | float? | Max retail price per unit |
| `rate_inr` | float? | Candor's selling price per unit |
| `amount_inr` | float? | Line total (qty × rate) |
| `gst_rate` | float? | GST as decimal (0.05 = 5%) |
| `hsn_code` | string? | HSN/SAC tax code |
| `ean` | string? | 13-digit EAN barcode |
| `delivery_date` | string? | YYYY-MM-DD |
| `remarks` | string? | Line-specific notes |

### From Master Matching (categorial_inv)

| Field | Type | Source Column | Description |
|---|---|---|---|
| `item_type` | string? | `fg/rm/pm` | FG (finished good), RM (raw material), or PM (packaging) |
| `item_category` | string? | `group` | Category (e.g. "dates", "packaging") |
| `sub_category` | string? | `sub_group` | Sub-category (e.g. "ajwa", "pouch roll") |
| `item_description` | string? | `particulars` | Canonical matched name from master |
| `sales_group` | string? | `sale_group` | Sales group (e.g. "va", "bulk") |
| `match_score` | float? | (computed) | Fuzzy match confidence, 0.0–1.0 |
| `match_source` | string? | (hardcoded) | Always `"categorial_inv"` |

### Computed / Set Later

| Field | Type | Description |
|---|---|---|
| `quantity_units` | int? | `quantity × UOM` (from master match) |
| `fg_id` | int? | NULL — set during FG matching (Module 1B) |
| `status` | string | Always `"pending"` at extraction time |

---

## Database Tables

### `so_header`

| Column | Type | Nullable | Default | Set At |
|---|---|---|---|---|
| `so_id` | SERIAL PK | No | auto | Upload |
| `so_number` | TEXT | Yes | — | Extraction |
| `so_date` | DATE | Yes | — | Extraction |
| `customer_id` | INT | Yes | — | Module 1B |
| `source_pdf_path` | TEXT | No | — | Upload |
| `original_filename` | TEXT | No | — | Upload |
| `file_size_bytes` | BIGINT | Yes | — | Upload |
| `customer_hint` | TEXT | Yes | — | Upload |
| `extraction_status` | TEXT | No | `'pending'` | Upload → Extraction |
| `extraction_error` | TEXT | Yes | — | On failure |
| `raw_extraction` | JSONB | Yes | — | Extraction |
| `extracted_at` | TIMESTAMPTZ | Yes | — | Extraction |
| `created_by` | INT | Yes | — | Upload |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Upload |

**Status transitions:** `pending` → `extracted` or `failed`

### `so_line`

| Column | Type | Nullable | Default | Set At |
|---|---|---|---|---|
| `so_line_id` | SERIAL PK | No | auto | Extraction |
| `so_id` | INT FK | No | — | Extraction |
| `line_number` | INT | No | — | Extraction |
| `fg_id` | INT | Yes | — | Module 1B |
| `quantity` | NUMERIC | Yes | — | Extraction |
| `article_code` | TEXT | Yes | — | Extraction |
| `customer_article_code` | TEXT | Yes | — | Extraction |
| `sku_name` | TEXT | Yes | — | Extraction |
| `quantity_units` | INT | Yes | — | Master match |
| `uom` | TEXT | Yes | — | Extraction |
| `mrp_inr` | NUMERIC | Yes | — | Extraction |
| `rate_inr` | NUMERIC | Yes | — | Extraction |
| `amount_inr` | NUMERIC | Yes | — | Extraction |
| `gst_rate` | NUMERIC | Yes | — | Extraction |
| `hsn_code` | TEXT | Yes | — | Extraction |
| `ean` | TEXT | Yes | — | Extraction |
| `delivery_date` | DATE | Yes | — | Extraction |
| `release_mode` | TEXT | Yes | `'all_upfront'` | Extraction |
| `remarks` | TEXT | Yes | — | Extraction |
| `item_type` | TEXT | Yes | — | Master match |
| `item_category` | TEXT | Yes | — | Master match |
| `sub_category` | TEXT | Yes | — | Master match |
| `item_description` | TEXT | Yes | — | Master match |
| `sales_group` | TEXT | Yes | — | Master match |
| `match_score` | NUMERIC | Yes | — | Master match |
| `match_source` | TEXT | Yes | — | Master match |
| `status` | TEXT | No | `'pending'` | Extraction |
| `created_at` | TIMESTAMPTZ | No | `NOW()` | Extraction |

**Unique constraint:** `(so_id, line_number)`

### `log_edit`

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `log_id` | SERIAL PK | No | auto | — |
| `table_name` | TEXT | No | — | `so_header` or `so_line` |
| `record_id` | INT | No | — | `so_id` or `so_line_id` |
| `field_name` | TEXT | Yes | — | NULL for INSERT actions |
| `action` | TEXT | No | — | `INSERT` / `UPDATE` / `DELETE` |
| `old_value` | TEXT | Yes | — | Previous value |
| `new_value` | TEXT | Yes | — | New value |
| `changed_by` | INT | Yes | — | User ID (NULL for system) |
| `changed_at` | TIMESTAMPTZ | No | `NOW()` | — |
| `request_id` | TEXT | Yes | — | Job ID for traceability |
| `module` | TEXT | No | `'so_intake'` | — |

### `categorial_inv` (read-only master data)

| Column | Type | Description |
|---|---|---|
| `particulars` | TEXT | Product name (lowercase) — match key |
| `fg/rm/pm` | TEXT | Item type: fg, rm, pm |
| `group` | TEXT | Category (e.g. dates, packaging) |
| `sub_group` | TEXT | Sub-category (e.g. ajwa, pouch roll) |
| `uom` | DOUBLE PRECISION | UOM multiplier for quantity_units |
| `sale_group` | TEXT | Sales group (e.g. va, bulk) |
| `inventory_group` | TEXT | Inventory group (e.g. grp 1) |

**~4002 rows.** CFPL and CDPL merged. All values lowercase.

---

## File Structure

```
app/
├── __init__.py
├── main.py                       # FastAPI app, lifespan, worker startup
├── config.py                     # Settings (env-based)
├── routers/
│   └── so.py                     # 3 endpoints: upload, poll, re-trigger
├── services/
│   ├── so_upload.py              # PDF validation + upload orchestration
│   ├── so_extraction.py          # PDF→images→Claude→parse→match→store
│   └── item_matcher.py           # Fuzzy matching against categorial_inv
├── storage/
│   ├── base.py                   # StorageBackend ABC
│   └── local.py                  # LocalStorageBackend (filesystem)
├── queue/
│   ├── base.py                   # ExtractionQueue ABC + ExtractionMessage
│   └── memory.py                 # InMemoryQueue (asyncio.Queue)
├── prompts/
│   ├── so_system.py              # Claude system prompt
│   └── so_user.py                # User prompt + JSON schema
├── schemas/
│   └── so.py                     # Pydantic request/response models
├── db/
│   ├── connection.py             # asyncpg pool create/close
│   ├── schema.sql                # CREATE TABLE IF NOT EXISTS
│   ├── migrate.sql               # Idempotent ALTER TABLE migrations
│   └── queries/
│       └── so.py                 # All SQL query functions
├── data/
│   └── GST Rate Setup *.xlsx     # Source Excel (loaded into categorial_inv)
workers/
└── so_extractor.py               # Worker loop with retry logic
```

---

## Configuration (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `ANTHROPIC_API_KEY` | Yes | — | Claude API key |
| `STORAGE_BACKEND` | No | `local` | Storage backend type |
| `STORAGE_LOCAL_BASE_DIR` | No | `./so_pdfs` | Local file storage root |
| `QUEUE_BACKEND` | No | `memory` | Queue backend type |
| `POPPLER_PATH` | No | — | Poppler path (not needed with PyMuPDF) |
| `SYSTEM_USER_ID` | No | `0` | System user for audit logs |
| `MAX_PDF_SIZE_MB` | No | `20` | Max upload size |
| `EXTRACTION_MAX_RETRIES` | No | `3` | Max extraction retry attempts |
| `CLAUDE_MODEL` | No | `claude-sonnet-4-20250514` | Claude model ID |

---

## Worker Retry Logic

| Attempt | Delay | Action |
|---|---|---|
| 1 | 0s | First try — immediate |
| 2 | 30s | Second try |
| 3 | 120s | Third try |
| 4+ | — | Mark as `failed` |

**Retryable errors:** Claude API timeout, rate limit (429), server error (5xx), JSON parse failure

**Non-retryable errors:** PDF unreadable, auth error (401), max retries exceeded

---

## Master Item Matching

After Claude extracts line items, each line's `sku_name` is fuzzy-matched against `categorial_inv.particulars`:

1. Normalize `sku_name` to lowercase
2. Use `rapidfuzz.fuzz.token_sort_ratio` scorer
3. Threshold: 75% — below this, no match
4. If matched:
   - Copy `item_type`, `item_category`, `sub_category`, `item_description`, `sales_group`
   - Compute `quantity_units = quantity × UOM`
   - Record `match_score` (0.0–1.0) and `match_source = "categorial_inv"`
5. If no match: fields left NULL, warning logged

---

## Startup Sequence

1. Load `Settings` from `.env`
2. Create asyncpg connection pool
3. Run `schema.sql` (create tables if not exist)
4. Run `migrate.sql` (add missing columns, fix constraints)
5. Load ~4002 master items from `categorial_inv` into memory
6. Create `InMemoryQueue`
7. Start extraction worker as `asyncio.Task`
8. FastAPI app ready to serve requests
