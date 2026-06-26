# Sample Issuing Module — Design & Reconciliation Doc

> **File type:** Internal design doc (precedes code)
> **Module:** `app/modules/sample/` (to be created)
> **Target repo:** `d:\Consumption\New\server_replica` (Python + FastAPI + asyncpg + PostgreSQL)
> **Status:** Draft v1 — schema and flows reconciled against actual codebase; awaiting decisions in §13
> **Last updated:** 2026-05-25
> **Branch context:** `feat/job-card-crud` is mid-flight (75 commits diverged); any `ALTER TABLE job_card` must coordinate with that branch
> **Source spec:** `SAMPLE_MODULE_PROMPT.md` (provided by developer) — this doc supersedes its §8 names with actual table/column names from the repo

---

## Table of Contents

1. [Executive summary](#1-executive-summary)
2. [Pre-read findings — what exists in the repo](#2-pre-read-findings--what-exists-in-the-repo)
3. [Spec → actual naming deltas](#3-spec--actual-naming-deltas)
4. [Final schema — new tables](#4-final-schema--new-tables)
5. [Final schema — extensions to existing tables](#5-final-schema--extensions-to-existing-tables)
6. [Movement-type plan (SAP discipline)](#6-movement-type-plan-sap-discipline)
7. [Role mapping & approval matrix config](#7-role-mapping--approval-matrix-config)
8. [Status state machine (corrected)](#8-status-state-machine-corrected)
9. [Flow management — accuracy & integrity improvements](#9-flow-management--accuracy--integrity-improvements)
10. [Gate pass template extension](#10-gate-pass-template-extension)
11. [Notifications mapping](#11-notifications-mapping)
12. [Integration points (with real file paths)](#12-integration-points-with-real-file-paths)
13. [Open decisions blocking implementation](#13-open-decisions-blocking-implementation)
14. [Implementation sequencing](#14-implementation-sequencing)
15. [Acceptance criteria](#15-acceptance-criteria)

---

## 1. Executive summary

The Sample Issuing sub-module under the Consumption module handles four flows:

- **Basis RM** — raw material sample, direct outward, gate pass out
- **Basis FG** — finished-good sample, jobcard-driven production, gate pass out
- **NPD** — new product development sample, draft BOM, jobcard, gate pass out
- **Internal** — internal-only dispatch, no gate pass, optional later conversion to external

All flows originate from a `sample_requisition` and either resolve to a gate pass (external) or an `INTERNALLY_DISPATCHED` state with optional redirect to a gate pass later (full or partial qty).

The module **extends** existing infrastructure — `all_sku`, `bom_header`/`bom_line`, `job_card`, `material_document`, `auth_user`/`auth_role`, `store_alert`, FPDF rendering — and adds six new tables for sample-specific entities.

---

## 2. Pre-read findings — what exists in the repo

### 2.1 SKU master — `all_sku` ([app/db/schema.sql:46-56](app/db/schema.sql))

| Column | Type | Notes |
|---|---|---|
| `sku_id` | SERIAL PK | |
| `particulars` | TEXT NOT NULL | **The canonical article name** (not `name`) — indexed |
| `uom` | NUMERIC | Stored as numeric, not text. Cast on display. |
| `item_type` | TEXT | Top-level category |
| `item_group` | TEXT | e.g. cashew, almond, dates |
| `sub_group` | TEXT | |
| `sale_group` | TEXT | |
| `gst` | NUMERIC | |
| `batch_strategy` | TEXT | `FIFO` \| `FEFO` (from sap_mm_align.sql) |
| `min_shelf_life_days` | INT | |

**Dropdown source:** `GET /api/v1/so/sku-lookup` ([app/modules/so/router.py:719-806](app/modules/so/router.py)) — supports cascading filter by `item_type`, `item_group`, `sub_group`, `sale_group`. **This is the only sanctioned source for article selection — do not query `all_sku` directly from sample-module UIs.**

### 2.2 BOM — `bom_header` + `bom_line` + `bom_process_route` ([app/db/production_schema.sql:38-89](app/db/production_schema.sql))

- `bom_header.bom_id` PK
- `bom_header.fg_sku_name` → references `all_sku.particulars` (text, not FK)
- `bom_header.customer_name`, `pack_size_kg`, `version`, `is_active`, `effective_from`/`to`
- `bom_line.bom_id` FK
- `bom_line.material_sku_name` → references `all_sku.particulars`
- `bom_line.item_type` `rm` \| `pm`
- `bom_line.quantity_per_unit`, `uom`, `loss_pct`, `offgrade_max_pct`, `godown`
- `bom_process_route` — manufacturing steps per BOM

**BOM reader:** `create_job_cards()` at [app/modules/production/services/job_card_engine.py:95-198](app/modules/production/services/job_card_engine.py).

### 2.3 Jobcards — `job_card` ([app/db/production_schema.sql:211-257](app/db/production_schema.sql))

- `job_card_id` PK, `job_card_number` UNIQUE (`PO-2026-0042/1` shape)
- `prod_order_id`, `bom_id`, `step_number`, `process_name`, `stage`
- `status` enum: `locked → unlocked → assigned → material_received → in_progress → completed → closed`
- Chained partial dispatch: `next_job_card_id`, `prev_job_card_id`, `carried_qty_kg`, `dispatched_to_next_kg`
- Soft-delete: `deleted_at`, `deleted_by`
- Force-unlock: `is_locked`, `locked_reason`, `force_unlocked`, `force_unlock_*`
- **No existing source/requisition FK** → must add.

### 2.4 Outward / consumption — `material_document` + `material_document_line` ([app/db/sap_mm_align.sql:6-42](app/db/sap_mm_align.sql))

SAP-style movement document, NOT a flat outward table.

- `mat_doc_id` (`MATDOC-YYYYMMDD-SEQ`)
- `movement_type` — 101 GR, 261 GI-to-Prod, 262 return, 301 plant xfer, 311 location xfer, 321/322 QC, 531 FG receipt, 551 scrap, 561 legacy
- `reference_type` — `PO` \| `JOB_CARD` \| `TRANSFER` \| `QC` \| `RTV` \| `ISN`
- `reference_id`
- `reversal_of`, `is_reversal`
- Service: `create_material_document()` at [app/modules/production/services/material_document_service.py:28-72](app/modules/production/services/material_document_service.py)

Secondary outward tables:
- `issue_note` + `issue_note_line` ([app/db/ims_new_schema.sql:75-111](app/db/ims_new_schema.sql)) — D4 issuance, JC-to-material binding
- `internal_issue_note` ([app/db/production_schema.sql:810-826](app/db/production_schema.sql)) — internal floor transfers with approval

### 2.5 Users & roles — `auth_user` + `auth_role` ([app/db/auth_schema.sql](app/db/auth_schema.sql))

Seeded roles (8):
`admin`, `planner`, `stores_manager`, `team_leader`, `qc_inspector`, `floor_manager`, `purchase_manager`, `viewer`

User scoping: `entity` (multi-entity), `allowed_warehouses` (TEXT[]), phone-based login.

**No `business_head`, `business_user`, or `npd_team` role.** Decision needed in §13.

### 2.6 Notifications — `store_alert` ([app/db/production_schema.sql:608-623](app/db/production_schema.sql)) + webhooks ([app/db/002_webhooks.sql](app/db/002_webhooks.sql))

`store_alert` columns:
- `alert_type` (TEXT) — open vocabulary; existing values: `material_shortage`, `indent_raised`, `material_received`, `force_unlock`, `anomaly`, `plan_ready`
- `target_team` — `purchase` \| `stores` \| `production` \| `qc` (extend)
- `message`, `related_id`, `related_type` (`fulfillment` \| `indent` \| `job_card` \| `plan` — extend)
- `is_read`, `entity`

Event bus: `app/webhooks/events.py` — used by production services. Pattern: `await events.<event_name>(entity=..., ...)`.

### 2.7 Gate pass — **does not exist as a dedicated table**

- No `gate_pass` table.
- Existing screenshots ("CANDOR FOODS - GATE PASS") render a **warehouse-to-warehouse transfer pass** paired with a delivery challan ("✂ CUT HERE" + "See Delivery Challan above"). Likely generated from `material_document` of `movement_type ∈ {301, 311}` (transfers).
- PDF infra: FPDF subclass pattern at [app/modules/production/services/job_card_pdf.py](app/modules/production/services/job_card_pdf.py). **No HTML→PDF (WeasyPrint / Playwright).** The screenshots look HTML-styled but are rendered via FPDF (confirm when code is provided).

### 2.8 Module convention

Each module under `app/modules/<name>/`:
- `router.py` — FastAPI APIRouter
- `schemas.py` or `schemas/` — Pydantic models
- `services/<domain>_service.py` — granular service files (best example: `app/modules/vendor/services/`)
- `__init__.py`

Migrations: raw SQL in `app/db/` numbered `NNN_<name>.sql`. **Next free number: `031`** (current highest in-flight: `030_vendor_history.sql`).

DB access: asyncpg via `request.app.state.db_pool`.

### 2.9 No frontend in this repo

Backend-only. UI consumed via `FRONTEND_API_DOC.md` per module. Per the project's frontend-first rule, UI mocks must be settled on the frontend repo before any sample-module API is hardened.

---

## 3. Spec → actual naming deltas

| Spec assumed | Actual | Used in this doc |
|---|---|---|
| `all_sku` | `all_sku` | ✓ same |
| SKU `name` column | `particulars` | use `particulars` |
| `boms` | `bom_header` (+ `bom_line`, `bom_process_route`) | use `bom_header` |
| `jobcards` | `job_card` | use `job_card` |
| `[outward_table]` | `material_document` (+ `material_document_line`) | use `material_document` |
| `gate_passes` (existing) | does not exist | net-new `sample_gate_passes` |
| `users` | `auth_user` | use `auth_user` |
| roles: business_head, store_team, production_team, inventory_manager, npd_team, business_user | see §7 mapping | config-driven, not hardcoded |
| outward `transaction_type` discriminator | use `material_document.reference_type` + new `SAMPLE_REQ` value | see §6 |

---

## 4. Final schema — new tables

All new tables go in **one migration**: `app/db/031_sample_module.sql`.

### 4.1 `sample_requisitions`

```sql
CREATE TABLE sample_requisitions (
  id                        SERIAL UNIQUE NOT NULL,          -- internal FK target
  request_id                BIGINT PRIMARY KEY,              -- 8-digit app-supplied id (new_short_time_id); the surfaced identifier (migrations 055/057). The SMP-YYYY-NNNN requisition_number was dropped in migration 068.
  sample_type               TEXT NOT NULL
                            CHECK (sample_type IN ('BASIS_RM','BASIS_FG','NPD','INTERNAL')),
  status                    TEXT NOT NULL DEFAULT 'DRAFT'
                            CHECK (status IN (
                              'DRAFT','SUBMITTED','BH_APPROVED','BH_REJECTED',
                              'IN_PRODUCTION','PACKING','READY_FOR_DISPATCH',
                              'INTERNALLY_DISPATCHED','PARTIALLY_CONVERTED',
                              'GATE_PASS_ISSUED','CLOSED','CANCELLED'
                            )),
  requestor_user_id         INT NOT NULL REFERENCES auth_user(user_id),
  requestor_team            TEXT,
  business_head_user_id     INT REFERENCES auth_user(user_id),   -- target approver; resolved at submission
  purpose_tag               TEXT
                            CHECK (purpose_tag IN (
                              'CUSTOMER_DISPLAY','CUSTOMER_ISSUE','TASTING_SENSORY',
                              'PHYSICAL_PARAMETERS','INTERNAL_OTHER'
                            )),
  purpose_note              TEXT,
  base_bom_id               INT REFERENCES bom_header(bom_id),    -- for NPD with base BOM
  npd_draft_bom_id          INT,                                  -- FK added after npd_draft_boms exists
  linked_job_card_id        INT REFERENCES job_card(job_card_id), -- for FG/NPD paths (latest jobcard in chain)
  linked_gate_pass_id       INT,                                  -- FK added after sample_gate_passes exists
  converted_from_id         INT REFERENCES sample_requisitions(id), -- redirect chain
  internal_override         BOOLEAN NOT NULL DEFAULT FALSE,
  converted_to_external     BOOLEAN NOT NULL DEFAULT FALSE,
  entity                    TEXT NOT NULL,                        -- multi-entity scoping (matches auth_user.entity)
  cancellation_reason       TEXT,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by                INT REFERENCES auth_user(user_id),
  updated_by                INT REFERENCES auth_user(user_id),
  deleted_at                TIMESTAMPTZ,                          -- soft-delete (matches job_card convention)
  deleted_by                INT REFERENCES auth_user(user_id)
);

CREATE INDEX idx_sample_req_status ON sample_requisitions(status) WHERE deleted_at IS NULL;
CREATE INDEX idx_sample_req_type ON sample_requisitions(sample_type) WHERE deleted_at IS NULL;
CREATE INDEX idx_sample_req_entity ON sample_requisitions(entity);
CREATE INDEX idx_sample_req_requestor ON sample_requisitions(requestor_user_id);
CREATE INDEX idx_sample_req_created_at ON sample_requisitions(created_at);

CREATE SEQUENCE sample_requisition_seq;  -- per spec §12.7: collision-safe
```

> **Numbering:** generate in app: `SELECT 'SMP-' || EXTRACT(YEAR FROM NOW()) || '-' || LPAD(nextval('sample_requisition_seq')::text, 4, '0')`. Reset annually via a yearly cron or by encoding year+seq differently if cross-year collisions matter.

### 4.2 `sample_requisition_articles`

```sql
CREATE TABLE sample_requisition_articles (
  id                SERIAL PRIMARY KEY,
  requisition_id    INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
  sku_id            INT NOT NULL REFERENCES all_sku(sku_id),
  sku_name          TEXT NOT NULL,                            -- denormalized snapshot of all_sku.particulars at request time
  required_qty      NUMERIC NOT NULL CHECK (required_qty > 0),
  issued_qty        NUMERIC CHECK (issued_qty >= 0),
  uom               TEXT NOT NULL,                            -- snapshot; all_sku.uom is numeric, see §9.6
  article_role      TEXT NOT NULL
                    CHECK (article_role IN ('RM','FG','NPD_INPUT','NPD_OUTPUT')),
  pack_size_kg      NUMERIC,                                  -- captured at request time for FG/NPD
  notes             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sample_req_articles_req ON sample_requisition_articles(requisition_id);
CREATE INDEX idx_sample_req_articles_sku ON sample_requisition_articles(sku_id);
```

> **Snapshot vs. live:** `sku_name` and `uom` are snapshotted to protect historical accuracy if `all_sku.particulars` is later renamed.

### 4.3 `sample_approvals`

```sql
CREATE TABLE sample_approvals (
  id                SERIAL PRIMARY KEY,
  requisition_id    INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
  approval_stage    TEXT NOT NULL
                    CHECK (approval_stage IN (
                      'BH_APPROVAL','PRODUCTION_ACK','INV_MGR_VERIFICATION',
                      'INV_MGR_SIGNOFF','CONVERSION_APPROVAL','CONVERSION_INV_MGR_SIGNOFF'
                    )),
  approver_user_id  INT NOT NULL REFERENCES auth_user(user_id),
  role_at_action    TEXT NOT NULL,                            -- snapshot of role_name at time of action
  action            TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (action IN ('PENDING','APPROVED','REJECTED')),
  remarks           TEXT,
  sequence_no       INT NOT NULL,                             -- ordering of stages on a requisition
  actioned_at       TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- one PENDING/APPROVED stage at a time per requisition+stage; rejections do not block re-submission
  CONSTRAINT uq_active_approval UNIQUE (requisition_id, approval_stage, sequence_no)
);

CREATE INDEX idx_sample_approvals_req ON sample_approvals(requisition_id);
CREATE INDEX idx_sample_approvals_approver ON sample_approvals(approver_user_id);
```

### 4.4 `npd_draft_boms`

```sql
CREATE TABLE npd_draft_boms (
  id                SERIAL PRIMARY KEY,
  requisition_id    INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
  base_bom_id       INT REFERENCES bom_header(bom_id),
  fg_sku_id         INT REFERENCES all_sku(sku_id),
  fg_sku_name       TEXT,                                     -- snapshot
  description       TEXT,
  status            TEXT NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT','USED','PROMOTED','ARCHIVED')),
  promoted_bom_id   INT REFERENCES bom_header(bom_id),        -- set when promoted to live BOM
  promoted_at       TIMESTAMPTZ,
  promoted_by       INT REFERENCES auth_user(user_id),
  promotion_approval_id INT REFERENCES sample_approvals(id),  -- separate explicit approval
  created_by        INT REFERENCES auth_user(user_id),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_npd_draft_boms_req ON npd_draft_boms(requisition_id);
CREATE INDEX idx_npd_draft_boms_status ON npd_draft_boms(status);
```

> **Isolation:** `npd_draft_boms` are deliberately a separate table from `bom_header` so production reports, MRP, and BOM dropdowns never see them. They become a real BOM only via `promoted_bom_id`.

### 4.5 `npd_draft_bom_lines`

```sql
CREATE TABLE npd_draft_bom_lines (
  id                SERIAL PRIMARY KEY,
  draft_bom_id      INT NOT NULL REFERENCES npd_draft_boms(id) ON DELETE CASCADE,
  sku_id            INT NOT NULL REFERENCES all_sku(sku_id),
  sku_name          TEXT NOT NULL,                            -- snapshot
  qty               NUMERIC NOT NULL CHECK (qty >= 0),
  uom               TEXT NOT NULL,
  item_type         TEXT CHECK (item_type IN ('rm','pm')),    -- matches bom_line.item_type
  delta_type        TEXT NOT NULL DEFAULT 'UNCHANGED'
                    CHECK (delta_type IN ('UNCHANGED','ADDED','MODIFIED','REMOVED')),
  original_qty      NUMERIC,                                  -- only for MODIFIED
  line_order        INT NOT NULL DEFAULT 0,
  notes             TEXT
);

CREATE INDEX idx_npd_draft_bom_lines_draft ON npd_draft_bom_lines(draft_bom_id);
```

### 4.6 `sample_gate_passes`

```sql
CREATE TABLE sample_gate_passes (
  id                      SERIAL PRIMARY KEY,
  gate_pass_number        TEXT UNIQUE NOT NULL,                 -- GP-SMP-YYYY-NNNN
  requisition_id          INT NOT NULL REFERENCES sample_requisitions(id),
  original_requisition_id INT REFERENCES sample_requisitions(id), -- for redirected gate passes (§6)
  material_document_id    INT,                                  -- FK to material_document.id (after id type confirmed)
  issued_by               INT NOT NULL REFERENCES auth_user(user_id),
  issued_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  bh_approver_user_id     INT REFERENCES auth_user(user_id),    -- snapshot for print
  bh_approval_at          TIMESTAMPTZ,
  inv_mgr_user_id         INT REFERENCES auth_user(user_id),
  inv_mgr_signoff_at      TIMESTAMPTZ,
  recipient_name          TEXT,
  recipient_contact       TEXT,
  vehicle_carrier         TEXT,
  driver_name             TEXT,                                 -- when applicable
  converted_from_internal BOOLEAN NOT NULL DEFAULT FALSE,
  conversion_qty          NUMERIC,                              -- for partial conversion bookkeeping
  print_count             INT NOT NULL DEFAULT 0,
  last_printed_at         TIMESTAMPTZ,
  voided                  BOOLEAN NOT NULL DEFAULT FALSE,
  voided_at               TIMESTAMPTZ,
  voided_by               INT REFERENCES auth_user(user_id),
  void_reason             TEXT,
  entity                  TEXT NOT NULL,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sample_gp_req ON sample_gate_passes(requisition_id);
CREATE INDEX idx_sample_gp_issued_at ON sample_gate_passes(issued_at);
CREATE INDEX idx_sample_gp_voided ON sample_gate_passes(voided);

CREATE SEQUENCE sample_gate_pass_seq;
```

### 4.7 `sample_audit_log` (new — for accuracy)

> Spec §9.3 demands "Audit log at the bottom: every status change, who triggered it, when." There is no shared audit infra in the repo, so add a dedicated table.

```sql
CREATE TABLE sample_audit_log (
  id              BIGSERIAL PRIMARY KEY,
  requisition_id  INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
  event_type      TEXT NOT NULL,                              -- 'STATUS_CHANGE','APPROVAL','ARTICLE_EDIT','CONVERSION','GATE_PASS_PRINT','VOID', etc.
  old_value       JSONB,
  new_value       JSONB,
  actor_user_id   INT REFERENCES auth_user(user_id),
  actor_role      TEXT,
  remarks         TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sample_audit_req ON sample_audit_log(requisition_id);
CREATE INDEX idx_sample_audit_created_at ON sample_audit_log(created_at);
```

### 4.8 `sample_approval_role_map` (new — for config-driven approval matrix)

> Spec §7.4: "Approval matrix role assignments must be stored in a config table or settings file — not hardcoded." This is that table.

```sql
CREATE TABLE sample_approval_role_map (
  id                  SERIAL PRIMARY KEY,
  approval_stage      TEXT NOT NULL
                      CHECK (approval_stage IN (
                        'BH_APPROVAL','PRODUCTION_ACK','INV_MGR_VERIFICATION',
                        'INV_MGR_SIGNOFF','CONVERSION_APPROVAL','CONVERSION_INV_MGR_SIGNOFF'
                      )),
  sample_type         TEXT
                      CHECK (sample_type IN ('BASIS_RM','BASIS_FG','NPD','INTERNAL','*')),
  entity              TEXT NOT NULL DEFAULT '*',              -- '*' = all entities
  required_role       TEXT NOT NULL REFERENCES auth_role(role_name),
  is_active           BOOLEAN NOT NULL DEFAULT TRUE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (approval_stage, sample_type, entity, required_role)
);

-- Seed (uses existing roles; adjust per §13.2)
INSERT INTO sample_approval_role_map (approval_stage, sample_type, required_role) VALUES
  ('BH_APPROVAL',                  '*',         'planner'),         -- standing in as Business Head
  ('PRODUCTION_ACK',               'BASIS_FG',  'floor_manager'),
  ('PRODUCTION_ACK',               'NPD',       'floor_manager'),
  ('INV_MGR_VERIFICATION',         '*',         'stores_manager'),
  ('INV_MGR_SIGNOFF',              '*',         'stores_manager'),
  ('CONVERSION_APPROVAL',          '*',         'planner'),
  ('CONVERSION_INV_MGR_SIGNOFF',   '*',         'stores_manager');
```

### 4.9 `sample_config` (new — system flags)

```sql
CREATE TABLE sample_config (
  config_key    TEXT PRIMARY KEY,
  config_value  TEXT NOT NULL,
  description   TEXT,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by    INT REFERENCES auth_user(user_id)
);

INSERT INTO sample_config (config_key, config_value, description) VALUES
  ('SAMPLE_SAME_DAY_REAPPROVAL_REQUIRED', 'true',
     'If false, conversion of an internal sample to gate pass within the same day skips fresh BH approval (still records a re-confirm action).'),
  ('SAMPLE_INTERNAL_OTHER_CONVERSION_ALLOWED', 'false',
     'If true, INTERNAL_OTHER purpose can be converted to gate pass without BH override.'),
  ('SAMPLE_AUTO_CLOSE_INTERNAL_AFTER_DAYS', '30',
     'Auto-CLOSE INTERNALLY_DISPATCHED records after N days with no conversion.');
```

---

## 5. Final schema — extensions to existing tables

> **Coordination warning:** `job_card` is being actively modified on `feat/job-card-crud`. Land the migration on top of that branch — do not push to `main` before it merges.

### 5.1 `job_card` additions

```sql
ALTER TABLE job_card
  ADD COLUMN IF NOT EXISTS sample_requisition_id INT REFERENCES sample_requisitions(id),
  ADD COLUMN IF NOT EXISTS jobcard_type TEXT NOT NULL DEFAULT 'REGULAR'
    CHECK (jobcard_type IN ('REGULAR','BASIS_FG_SAMPLE','NPD_SAMPLE','INTERNAL_FG_OVERRIDE'));

CREATE INDEX IF NOT EXISTS idx_job_card_sample_req ON job_card(sample_requisition_id)
  WHERE sample_requisition_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_job_card_type ON job_card(jobcard_type)
  WHERE jobcard_type != 'REGULAR';
```

### 5.2 `material_document` additions

```sql
ALTER TABLE material_document
  ADD COLUMN IF NOT EXISTS sample_requisition_id INT REFERENCES sample_requisitions(id),
  ADD COLUMN IF NOT EXISTS sample_gate_pass_id   INT REFERENCES sample_gate_passes(id),
  ADD COLUMN IF NOT EXISTS converted_to_external BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_matdoc_sample_req
  ON material_document(sample_requisition_id)
  WHERE sample_requisition_id IS NOT NULL;
```

> The spec asked for a `transaction_type` discriminator on the outward table. The repo already uses `material_document.reference_type` for the same purpose — we extend its allowed values instead of adding a parallel column. See §6.

### 5.3 Back-fill the deferred FKs on `sample_requisitions`

```sql
ALTER TABLE sample_requisitions
  ADD CONSTRAINT fk_sample_req_npd_draft
    FOREIGN KEY (npd_draft_bom_id) REFERENCES npd_draft_boms(id),
  ADD CONSTRAINT fk_sample_req_gate_pass
    FOREIGN KEY (linked_gate_pass_id) REFERENCES sample_gate_passes(id);
```

---

## 6. Movement-type plan (SAP discipline)

The existing `material_document.movement_type` codes are SAP-aligned. Introducing samples casually would corrupt production reports. **Add two new codes, scoped to samples:**

| New code | Meaning | Used in flow |
|---|---|---|
| `265` | Goods Issue to Sample | All sample outwards (RM, FG, NPD, Internal) |
| `266` | Reversal of Sample Goods Issue | Voided gate pass + manual reversal |

And extend `reference_type` to include:

| New value | Meaning |
|---|---|
| `SAMPLE_REQ` | The `reference_id` points at `sample_requisitions.id` |

**Result:** every sample movement is uniquely identifiable by `movement_type IN ('265','266') AND reference_type = 'SAMPLE_REQ'`, fully filterable in existing stock-movement reports without polluting `261` (GI-to-Prod).

**Action:** add seed rows in `movement_type_ref` (if such a registry table exists — confirm during code phase) and update [material_document_service.py](app/modules/production/services/material_document_service.py) constants:

```python
MVT_GI_SAMPLE = '265'
MVT_REVERSE_SAMPLE = '266'
REF_SAMPLE_REQ = 'SAMPLE_REQ'
```

---

## 7. Role mapping & approval matrix config

The spec's role vocabulary doesn't map 1:1 to `auth_role`. Two options:

### Option A — Config-driven (recommended)

Keep the 8 existing roles. Map each spec role to an existing role via `sample_approval_role_map` (§4.8). Default seed:

| Spec role | Existing `auth_role` |
|---|---|
| Business head | `planner` |
| Business team member | `planner` (or any non-`viewer` non-`admin` — looser) |
| Store team | `stores_manager` |
| Production team | `floor_manager` (+ `team_leader` for execution) |
| Inventory manager | `stores_manager` (same person — explicit in seed) |
| NPD team | `planner` (no NPD-specific role exists; consider adding `npd` role later) |

**Pros:** zero auth_schema changes; one config table change to remap.
**Cons:** `stores_manager` plays both "store team" and "inventory manager" — same human, but if you ever need to split them, you'll add a role.

### Option B — Add new roles

`INSERT INTO auth_role` four new rows: `business_head`, `business_user`, `inventory_manager`, `npd_team`. Permissions wired in `auth_role_permission` per existing convention.

**Pros:** clean separation; matches the spec's mental model.
**Cons:** requires reseeding test data, retraining users, and explaining the difference between `stores_manager` and `inventory_manager`.

**Decision required — see §13.2.**

---

## 8. Status state machine (corrected)

The spec's state machine is sound. One refinement for safer transitions:

```
DRAFT
  ──[Submit, validates articles + entity + base_bom for NPD]──► SUBMITTED
                                                                  │
SUBMITTED ──[BH approve, writes sample_approvals row]──► BH_APPROVED
SUBMITTED ──[BH reject, requires remarks]────────────► BH_REJECTED
BH_REJECTED ──[Edit + Resubmit]──► SUBMITTED

BH_APPROVED ──┬──[BASIS_RM | INTERNAL: material_document created]──► READY_FOR_DISPATCH
              └──[BASIS_FG | NPD: job_card created via engine]──► IN_PRODUCTION
                                                                   │
IN_PRODUCTION ──[jobcard.status = in_progress → completed]──► PACKING
PACKING ──[jobcard.status = closed]──► READY_FOR_DISPATCH

READY_FOR_DISPATCH (gate pass path)
  ──[Inv Mgr verifies (sample_approvals: INV_MGR_VERIFICATION)]──►
  ──[Inv Mgr signs off (INV_MGR_SIGNOFF) + sample_gate_passes row + PDF]──►
                                                                   GATE_PASS_ISSUED
                                                                   │
GATE_PASS_ISSUED ──[manual close, or auto after gate pass print]──► CLOSED

READY_FOR_DISPATCH (INTERNAL path, no gate pass)
  ──[Store dispatches]──► INTERNALLY_DISPATCHED
                              │
INTERNALLY_DISPATCHED
  ├──[No conversion, auto-close after SAMPLE_AUTO_CLOSE_INTERNAL_AFTER_DAYS]──► CLOSED
  ├──[Full conversion (§6 redirect)]──► GATE_PASS_ISSUED
  └──[Partial conversion]──► PARTIALLY_CONVERTED
                                ├──[Remaining converted later]──► GATE_PASS_ISSUED
                                └──[Remaining closed manually]──► CLOSED

Any non-terminal status ──[Cancel with reason]──► CANCELLED
```

**Guarded transitions (enforced in service layer + checked by DB constraints):**

| From → To | Guard |
|---|---|
| `DRAFT → SUBMITTED` | ≥1 article line, valid sku_id, qty > 0, entity matches requestor |
| `SUBMITTED → BH_APPROVED` | active `sample_approvals` row with `action='APPROVED'` and `approval_stage='BH_APPROVAL'` |
| `SUBMITTED → BH_REJECTED` | rejecting approval row has non-null `remarks` |
| `BH_APPROVED → IN_PRODUCTION` | `linked_job_card_id` set and `job_card.sample_requisition_id = self` |
| `READY_FOR_DISPATCH → GATE_PASS_ISSUED` | `INV_MGR_SIGNOFF` approval = APPROVED and `sample_gate_passes` row exists |
| `INTERNALLY_DISPATCHED → GATE_PASS_ISSUED` | conversion approval chain present and original material_document exists (no double deduction) |
| `INTERNALLY_DISPATCHED → PARTIALLY_CONVERTED` | conversion qty < issued qty and a `converted_from_id` child requisition exists |
| `* → CANCELLED` | non-null `cancellation_reason` |

---

## 9. Flow management — accuracy & integrity improvements

This is the substance of "better accuracy and flow management." The original spec describes *what* should happen; this section pins down *how* to make it correct under load, partial failure, and concurrent operation.

### 9.1 Atomicity boundaries (which operations are one transaction)

| Operation | Single transaction must contain |
|---|---|
| Submit requisition | INSERT `sample_requisitions` (DRAFT→SUBMITTED) + INSERT first `sample_approvals` (PENDING BH) + INSERT `sample_audit_log` + emit `store_alert` *outside* txn (eventual) |
| BH approve | UPDATE `sample_approvals` (PENDING→APPROVED) + UPDATE `sample_requisitions.status` + INSERT next-stage `sample_approvals` (PENDING) + INSERT audit |
| Outward record (RM/Internal) | INSERT `material_document` + lines + UPDATE `sample_requisitions.status` + UPDATE `sample_requisition_articles.issued_qty` + INSERT audit |
| Gate pass issuance | UPDATE INV_MGR_SIGNOFF approval + INSERT `sample_gate_passes` (number from sequence) + UPDATE `sample_requisitions.status='GATE_PASS_ISSUED'` + UPDATE `material_document.sample_gate_pass_id` + INSERT audit. **PDF rendering is OUTSIDE this txn** (§9.4) |
| Internal → external conversion (full) | INSERT new gate pass + UPDATE original `material_document.converted_to_external = true` + UPDATE `sample_requisitions.status='GATE_PASS_ISSUED'` + INSERT audit |
| Partial conversion | INSERT child `sample_requisitions` (with `converted_from_id`) + INSERT new gate pass + INSERT new `material_document` only if qty deduction differs + UPDATE parent status |

### 9.2 Idempotency for write endpoints

Every state-changing POST/PATCH endpoint accepts an optional `Idempotency-Key` header. The router stores `(idempotency_key, response_payload)` in a lightweight cache (Redis if available, else a small table) for 24h. Replays return the cached response without re-running side effects. This protects against:
- Double-submit from a flaky network
- Repeated BH-approve clicks
- Repeated gate-pass-print presses

### 9.3 Concurrency control

| Risk | Mitigation |
|---|---|
| Two approvers click "Approve" simultaneously | `SELECT ... FOR UPDATE` on `sample_requisitions` row during approval write; second approver sees a "stage already actioned" 409 |
| Two store team members issue outward for same requisition | Same row lock; service checks `status = BH_APPROVED` inside the txn |
| Sequence collision (`SMP-2026-0001`) | Postgres `nextval(seq)` is atomic; no `MAX(id) + 1` |
| Gate pass print count race | `UPDATE sample_gate_passes SET print_count = print_count + 1 ... RETURNING print_count` (atomic) |
| Material_document write races with stock_ledger reads | Existing `material_document_service.create_material_document` already handles this; we reuse, not parallel |

### 9.4 PDF rendering failure recovery

Issue: if the gate pass row is INSERTed and then FPDF rendering throws, the row is "born" but the PDF doesn't exist. On reload, the user sees a gate pass with no PDF.

**Approach:**
- Gate pass row is inserted in transaction T1 → `sample_requisitions.status` becomes `GATE_PASS_ISSUED`.
- PDF rendered in a separate call (T2). PDF written to disk/object store with a deterministic key: `gatepass-{gate_pass_number}.pdf`.
- `sample_gate_passes.last_printed_at` is set only when the PDF generation succeeds.
- "Print" button always re-renders from current data, increments `print_count`, and writes the PDF again. This means PDF is never the source of truth — the DB row is.

### 9.5 Inventory double-deduction prevention (Scenario A & B, §6)

**Scenario A** (full conversion, customer takes already-internally-dispatched sample):
- The original `material_document` of `movement_type=265` is **not** reversed.
- A new `sample_gate_passes` row references the same `material_document` via `sample_gate_passes.material_document_id`.
- `material_document.converted_to_external = true` and `material_document.sample_gate_pass_id = new_gate_pass_id`.
- **No second deduction.**

**Scenario B partial conversion:**
- Original `material_document` deduction stands.
- A new child `sample_requisitions` is created with `converted_from_id = parent.id` and `converted_to_external = true`.
- The conversion qty is bookkeeping (no new movement) **if the partial qty was already part of the original deduction** — which it always is for internal samples.
- If for some reason additional qty is added during conversion (e.g. recipient asks for more than what was internally taken), that delta requires a *new* `material_document` with `movement_type=265` — but this should not happen given the spec. Block at service layer with an error.

### 9.6 UOM hardening

`all_sku.uom` is NUMERIC in the schema. Two reasonable hypotheses:
- It's a units-per-pack ratio (e.g. `1.0`, `0.5`) — possible but unconventional.
- It's a legacy field; the real text UOM lives elsewhere or is implied by `item_type`.

**Action:** in `sample_requisition_articles.uom`, store the **display string** (kg, g, pcs, box, etc.) determined by the SKU dropdown service (`/api/v1/so/sku-lookup` already deals with this — inspect its output format and follow). Never compare against `all_sku.uom` directly without casting.

### 9.7 Soft-delete and re-use

- `sample_requisitions.deleted_at` makes the row invisible to queues but preserves history for audit.
- Hard delete only via `npd_draft_boms` (cascades from requisition) when explicitly archived.
- Requisition numbers are never re-issued — even on cancel/delete, the sequence advances.

### 9.8 Audit log standardization

Every mutating service writes to `sample_audit_log` with `(event_type, old_value, new_value, actor_user_id, actor_role)`. Status changes, article edits, approvals, conversions, prints, and voids all produce one row. Build a single decorator/helper in `app/modules/sample/services/audit_service.py` so no service writes audit lines manually.

### 9.9 Status integrity check (background)

A nightly job (or pre-commit consistency check) verifies:
- Every `GATE_PASS_ISSUED` requisition has exactly one non-voided `sample_gate_passes` row.
- Every `IN_PRODUCTION` requisition has a `linked_job_card_id` whose `jobcard_type` is not `REGULAR`.
- Every `PARTIALLY_CONVERTED` parent has at least one child via `converted_from_id`.
- Every `INTERNALLY_DISPATCHED` requisition has exactly one `material_document` with `movement_type='265'`.

Mismatches surface in `store_alert` for `target_team='stores'` with `alert_type='sample_integrity_drift'`.

### 9.10 Time accuracy

All timestamps are `TIMESTAMPTZ` (timezone-aware). The repo mixes `TIMESTAMP` and `TIMESTAMPTZ` historically — for the sample module, use `TIMESTAMPTZ` uniformly. Display in user's local timezone in the API layer, never in the database.

---

## 10. Gate pass template extension

Based on the existing Candor Foods gate pass screenshots (warehouse-to-warehouse transfer pass paired with a delivery challan via "✂ CUT HERE").

### 10.1 What the existing format provides

- Logo + red title "CANDOR FOODS - GATE PASS"
- Meta row: `Transfer No` · `Date` · `Vehicle` · `Driver`
- Route row: `From` · `To`
- Items table: `S.No | Item Description | Boxes | Qty | Net Wt (Kg)` (+ `Count` for PM variant)
- Totals + status pill `COMPLETE`
- Signature row: `Security Sign` · `Driver Sign`
- Footer: `Present this gate pass at security gate · Authorized by: <name>`

### 10.2 Extensions for sample variant (per spec §13 Step 4 — do not redesign)

**Header additions (added as new rows above the route row, only when applicable):**
- Always: `Sample Req. No: SMP-YYYY-NNNN`
- Always: `Purpose: <Customer Display / Tasting / Physical Parameters / Customer Issue / Other (note)>`
- Conditional: `Converted from Internal Sample: SMP-YYYY-NNNN` (only if `converted_from_internal = true`)
- Conditional: `[NPD SAMPLE]` red badge in the title band (only if sample_type = NPD)

**Route row change for customer recipient (not warehouse-to-warehouse):**
- Left cell: `From: Candor Foods - Warehouse <code>` (unchanged)
- Right cell: when recipient is a customer, `To: <recipient_name> (<contact>)` instead of warehouse code

**Driver row treatment:**
- If `driver_name` is set: `Driver: <name> (<phone>)`
- If hand-carried: replace with `Recipient: <name> (<phone>)`

**Signature row extension (from 2 cells to 3):**
- `Business Head Sign` (BH name printed below)
- `Inventory Manager Sign` (IM name printed below)
- `Recipient Sign` (or `Driver Sign` if vehicle dispatch)

**Footer extension:**
- `Printed: <ts> · Print #N · Present this gate pass at security gate · Authorized by: <BH name>`

**Voided overlay:**
- If `voided = true`: large diagonal red `VOIDED` watermark across items table + `Void reason: <reason>` line in footer.

### 10.3 Numbering

- `GP-SMP-YYYY-NNNN` from `nextval('sample_gate_pass_seq')`, formatted in app.
- Existing `Transfer No: TRANS<YYYYMMDD><seq>` shape is preserved for transfer pass — sample variant uses the new shape.

### 10.4 Renderer

To be confirmed when the developer shares the existing code. Hypotheses:
- If FPDF: extend the existing `JobCardPDF`-style class with a `SampleGatePassPDF` subclass in `app/modules/sample/services/sample_gate_pass_pdf.py`.
- If HTML→PDF: use the existing template engine and add a new template file mirroring the existing one with the extension fields above.

Either way: do not introduce a new PDF library.

---

## 11. Notifications mapping

All sample notifications use the existing `store_alert` table. Extend `target_team` and `related_type` vocab.

### 11.1 New `target_team` values (extend whitelist in code/check)

| New value | Maps to |
|---|---|
| `business` | Business team members & business head |
| `inventory` | Inventory manager (alias of `stores_manager` if Option A in §7) |
| `npd` | NPD team |

### 11.2 New `related_type` value

| Value | Meaning |
|---|---|
| `sample_requisition` | `related_id` points at `sample_requisitions.id` |
| `sample_gate_pass` | `related_id` points at `sample_gate_passes.id` |

### 11.3 New `alert_type` values

| `alert_type` | Triggered when | `target_team` |
|---|---|---|
| `sample_submitted` | requisition status → SUBMITTED | `business` (business head) |
| `sample_bh_approved` | status → BH_APPROVED, type RM/INTERNAL | `stores` |
| `sample_bh_approved` | status → BH_APPROVED, type FG/NPD | `production` |
| `sample_bh_rejected` | status → BH_REJECTED | requestor (individual, via existing pipeline) |
| `sample_jobcard_created` | job_card row inserted with sample_requisition_id | `production` |
| `sample_jobcard_completed` | job_card.status → closed for a sample JC | `inventory` |
| `sample_rm_outward_done` | material_document inserted with `movement_type=265` and `sample_type=BASIS_RM` | `inventory` |
| `sample_inv_mgr_verified` | INV_MGR_VERIFICATION approval set | `business` (business head, for info) |
| `sample_gate_pass_issued` | sample_gate_passes row inserted (not voided) | requestor + `business` |
| `sample_conversion_initiated` | conversion action started on INTERNALLY_DISPATCHED record | `inventory` + `business` |
| `sample_conversion_approved` | CONVERSION_APPROVAL → APPROVED | `inventory` |
| `sample_gate_pass_voided` | gate pass voided | `business` + `stores` |
| `sample_integrity_drift` | nightly check found inconsistency | `stores` |

Event bus: register handlers in `app/modules/sample/services/notification_service.py` that wrap `INSERT INTO store_alert` and emit via `app/webhooks/events.py` (matches existing pattern).

---

## 12. Integration points (with real file paths)

### 12.1 v1 vs v2 — hard rule for this module

This repo uses internal "v2" versioning on **specific service functions and endpoints** (URL prefix is uniformly `/api/v1/...` — the "v2" is in the function name and behavior, not the URL). The pattern is:

- **v2** = the current, consolidated, canonical implementation. New code must use these.
- **v1** = legacy delegator kept only for downstream FK compatibility. Existing call sites point at v1 only because they predate v2.

Example: [app/modules/production/services/job_card_engine.py:470](app/modules/production/services/job_card_engine.py#L470)
```python
async def record_output(...):
    """Record Section 5 output data (v1 legacy — delegates to v2)."""
    return await record_output_v2(conn, job_card_id, data)
```

**Per the project's "Planning v2 full parity" rule, the SO Fulfillment page is 100% on v2 endpoints; v1 still exists only because some FKs hang off it but it is NOT called from any page.** The sample module must follow the same discipline.

**Rule for this module:**

| Concern | Use this (v2) | Never call |
|---|---|---|
| Record job card output (FG actuals, byproducts, balance materials, QC) | `record_output_v2()` ([job_card_engine.py:474](app/modules/production/services/job_card_engine.py#L474)) via endpoint `POST /api/v1/production/job-cards/{id}/output` ([router.py:3577](app/modules/production/router.py#L3577)) | `record_output()` v1 wrapper at [job_card_engine.py:~470](app/modules/production/services/job_card_engine.py#L470) |
| Read job card output | `GET /api/v1/production/job-cards/{id}/output` ([router.py:3599](app/modules/production/router.py#L3599)) — v2 consolidated read | any older per-table fetches |
| Production planning / fulfillment lookups | `/api/v1/production/fulfillment/*` and `/api/v1/production/plans/*` endpoints ([router.py:66 onward](app/modules/production/router.py#L66)) — these are the v2-implementation set | the legacy v1 fulfillment service (still on disk for FK fidelity but not called by any page) |
| Movement document creation | `create_material_document()` ([material_document_service.py:28](app/modules/production/services/material_document_service.py#L28)) — already the canonical, consolidated SAP-style writer | any direct INSERTs into `material_document` from the sample module |

**Enforcement:** code review will reject any sample-module import that targets a v1-named function or a legacy fulfillment service when a v2 equivalent exists.

### 12.2 Integration matrix

| Integration | Source (v2 / canonical) | Approach |
|---|---|---|
| SKU dropdown | [app/modules/so/router.py:719](app/modules/so/router.py#L719) — `/api/v1/so/sku-lookup` | Reuse endpoint as-is. Sample module's article-pick UI calls this. |
| BOM reader | [app/modules/production/services/job_card_engine.py:95](app/modules/production/services/job_card_engine.py#L95) — `create_job_cards()` reads BOM internally | Factor the BOM-read step out into a pure helper `read_bom_for_jobcards(bom_id)` (no behavior change for existing callers) so the sample module can consume it without invoking jobcard generation. |
| Job card creation | [app/modules/production/services/job_card_engine.py:create_job_cards](app/modules/production/services/job_card_engine.py#L95) | Extend `create_job_cards()` to accept optional `sample_requisition_id` + `jobcard_type` kwargs and stamp them on every JC it generates. **Do not build a parallel jobcard creator.** |
| Job card output recording (FG actuals, byproducts, balance, QC) | **`record_output_v2()` only** ([job_card_engine.py:474](app/modules/production/services/job_card_engine.py#L474)) — used via `POST /api/v1/production/job-cards/{id}/output` ([router.py:3577](app/modules/production/router.py#L3577)) | When a sample jobcard (`BASIS_FG_SAMPLE` / `NPD_SAMPLE` / `INTERNAL_FG_OVERRIDE`) is closed, the same v2 endpoint records output. Sample module reads back via `GET /api/v1/production/job-cards/{id}/output`. |
| Job card lifecycle endpoints (assign, start, complete, sign-off, close, force-unlock) | `/api/v1/production/job-cards/{id}/...` ([router.py:1941-2108](app/modules/production/router.py#L1941)) | Reuse as-is. Sample requisition status reacts to jobcard lifecycle events via the existing event bus, not via parallel state machine. |
| Production planning / fulfillment integration (e.g. linking an NPD jobcard back to a fulfillment record if requested) | `/api/v1/production/fulfillment/*` v2 endpoints | Use only the v2 routes. Never reach into the legacy fulfillment service. |
| Outward / consumption entry | [material_document_service.py:create_material_document](app/modules/production/services/material_document_service.py#L28) | Already generic. Pass `movement_type='265'`, `reference_type='SAMPLE_REQ'`, `reference_id=requisition.id`. No code change in the function — just add new constants. |
| Gate pass | new file `app/modules/sample/services/sample_gate_pass_service.py` + PDF service | Net-new. Becomes system-wide gate pass source going forward, per spec §10. |
| Notifications | [app/webhooks/events.py](app/webhooks/events.py) + `store_alert` | Wrap existing pipeline. See §11. |
| Roles / permissions | [app/db/auth_schema.sql](app/db/auth_schema.sql) + `auth_role_permission` | Config-driven via `sample_approval_role_map`. No new roles unless §13.2 decides otherwise. |
| Reports / dashboards | existing production report queries | Add `WHERE jobcard_type != 'NPD_SAMPLE'` filter to NPD-excludable reports; add a sample-included toggle. Stock-movement reports filter by `movement_type` — they pick up `265`/`266` automatically; exclude with `AND movement_type NOT IN ('265','266')` where samples should not appear. |
| PDF export | TBD (confirm fpdf vs HTML→PDF when developer shares code) | Reuse existing infra. |

---

## 13. Open decisions blocking implementation

1. **Movement-type approach** — confirm new codes `265` (GI to Sample) and `266` (reversal) on `material_document`, with `reference_type='SAMPLE_REQ'`. Default: yes.
2. **Role strategy** — Option A (config-driven, reuse 8 existing roles) or Option B (add 4 new roles `business_head`, `business_user`, `inventory_manager`, `npd_team`). Default: Option A.
3. **Gate pass scope** — sample-only `sample_gate_passes` (per §8.6), or build a generic `gate_passes` table now and use it as the system's single gate pass source (per spec §10). Default: sample-only first, generalize later only if a second gate pass use case appears.
4. **Coordination with `feat/job-card-crud`** — wait for that branch to merge before pushing `ALTER TABLE job_card`, or branch from it. Default: branch from it, rebase as it evolves.
5. **NPD draft BOM promotion** — does promotion to a live BOM require additional sign-off beyond `CONVERSION_APPROVAL` (e.g. a QC approver)? Default: just BH for now; extensible via `sample_approval_role_map`.
6. **Auto-close for INTERNALLY_DISPATCHED** — confirm 30 days (`SAMPLE_AUTO_CLOSE_INTERNAL_AFTER_DAYS=30`) or different.
7. **Per-customer recipient registry** — sample gate passes go to customers. Should there be a `customer_contacts` lookup, or is free-text recipient acceptable? Default: free-text in v1, lookup in a later version.
8. **Frontend handoff** — per the frontend-first project rule, UI mocks for: sample requisition form (4 steps), queue views (5), requisition detail page, and gate pass print preview must be approved on the frontend side before the backend API is finalized. Confirm whether this doc should be paired with a UI spec or if the frontend team is producing their own.

---

## 14. Implementation sequencing

**Not** a green light to start coding — listed for reviewability.

1. Get decisions on §13.
2. Land UI mocks (frontend repo) — golden path screens for all 4 sample types + redirect + queue views + detail page + gate pass preview.
3. Write `app/db/031_sample_module.sql` with all 9 new tables + extensions (no FK on `job_card.sample_requisition_id` yet — that lands as `032` after `feat/job-card-crud` settles).
4. Factor BOM read out of `job_card_engine.create_job_cards()` into a pure helper `read_bom_for_jobcards(bom_id)` (no behavior change for existing callers).
5. Add `MVT_GI_SAMPLE` / `MVT_REVERSE_SAMPLE` / `REF_SAMPLE_REQ` constants in `material_document_service.py`. Update any movement-type registry table.
6. Scaffold `app/modules/sample/` with `router.py`, `schemas.py`, `services/{requisition_service, approval_service, gate_pass_service, npd_service, conversion_service, notification_service, audit_service}.py`.
7. Implement Basis RM end-to-end (simplest path: req → BH approve → store outward → IM verify → IM signoff → gate pass).
8. Implement Basis FG (adds jobcard creation).
9. Implement NPD (adds draft BOM editor).
10. Implement Internal (no gate pass).
11. Implement Internal→External redirect Scenario A (full).
12. Implement Internal→External redirect Scenario B (partial — bookkeeping table for split).
13. Implement gate pass PDF renderer (extend existing template per §10).
14. Wire notifications (§11) and integrity check job (§9.9).
15. Reports & dashboards filters (`NPD_SAMPLE` exclusion, `265`/`266` movement filtering).
16. End-to-end UAT against UI golden paths.
17. `032_job_card_sample_fk.sql` lands after `feat/job-card-crud` merges.

Estimated migration count: 2 (`031_sample_module.sql`, `032_job_card_sample_fk.sql`).
Estimated new module files: ~12 (1 router + 1 schemas + ~7 services + 1 PDF + 2 helpers).
Estimated extended existing files: 2 (`material_document_service.py` constants, `job_card_engine.py` jobcard_type stamping).

---

## 15. Acceptance criteria

A given sample requisition is considered "correctly implemented" when:

1. It can be created with articles sourced only from `/api/v1/so/sku-lookup`. Free-text articles are rejected at the API boundary with a 422.
2. Its state transitions follow §8 strictly. Illegal transitions return 409 with the offending current state and target state in the error body.
3. Its approval chain is fully readable from `sample_approvals` ordered by `sequence_no`, with `approver_user_id`, `role_at_action`, `action`, `actioned_at`, `remarks`.
4. Every state change writes one row to `sample_audit_log`.
5. For RM/Internal flows, exactly one `material_document` with `movement_type='265'` and `reference_type='SAMPLE_REQ'` is written and is visible in stock movement history.
6. For FG/NPD flows, exactly one chain of `job_card` rows with `jobcard_type IN ('BASIS_FG_SAMPLE','NPD_SAMPLE','INTERNAL_FG_OVERRIDE')` and `sample_requisition_id = self.id` is created. These jobcards are excluded from regular production reports.
7. For NPD: draft BOM lives only in `npd_draft_boms` and `npd_draft_bom_lines` until explicit `PROMOTED` action sets `promoted_bom_id` and inserts into `bom_header`.
8. Gate pass generation writes a `sample_gate_passes` row + renders a PDF matching the extended Candor template (§10). Print increments `print_count` atomically and re-renders.
9. Redirect Scenario A: no second `material_document` is written; existing one is marked `converted_to_external=true` with `sample_gate_pass_id` set.
10. Redirect Scenario B partial: parent requisition reaches `PARTIALLY_CONVERTED`, child requisition with `converted_from_id` reaches `GATE_PASS_ISSUED`, no double deduction.
11. Conversion always inserts a fresh approval row in `sample_approvals` (`approval_stage='CONVERSION_APPROVAL'`) — even when the approver and the date match the original, per spec §6.3.
12. Voided gate passes do not auto-reverse `material_document`. Manual reversal entry is a `movement_type='266'` row referencing the voided gate pass.
13. Sequence numbers `SMP-YYYY-NNNN` and `GP-SMP-YYYY-NNNN` are never duplicated under concurrent insert load (verified by a 100-parallel-insert stress test).
14. Nightly integrity check (§9.9) reports zero drift on a clean dataset.
15. **No sample-module code path imports or invokes a v1 legacy function when a v2 equivalent exists.** Specifically: job card output must go through `record_output_v2` / `POST /api/v1/production/job-cards/{id}/output`; production planning / fulfillment integration must use only the v2-implementation endpoints under `/api/v1/production/fulfillment/*` and `/api/v1/production/plans/*`. A grep audit at PR time confirms zero references to `record_output(` (v1 wrapper) and to the legacy fulfillment service in `app/modules/sample/`.

---

## Appendix A — Table summary at a glance

### New tables (9)
| # | Table | Purpose |
|---|---|---|
| 1 | `sample_requisitions` | Central requisition record |
| 2 | `sample_requisition_articles` | Article line items per requisition |
| 3 | `sample_approvals` | Approval chain per requisition stage |
| 4 | `npd_draft_boms` | NPD draft BOM (isolated from live BOMs) |
| 5 | `npd_draft_bom_lines` | NPD draft BOM line items |
| 6 | `sample_gate_passes` | Sample-specific gate pass record |
| 7 | `sample_audit_log` | Full audit trail per requisition |
| 8 | `sample_approval_role_map` | Config-driven approval role assignments |
| 9 | `sample_config` | System flags (same-day reapproval, auto-close, etc.) |

### Extended tables (2)
| # | Table | Added columns |
|---|---|---|
| 1 | `job_card` | `sample_requisition_id`, `jobcard_type` |
| 2 | `material_document` | `sample_requisition_id`, `sample_gate_pass_id`, `converted_to_external` |

### Movement-type registrations (2 new codes + 1 new reference_type)
| Code/value | Purpose |
|---|---|
| `265` | Goods Issue to Sample |
| `266` | Reversal of Sample Goods Issue |
| `reference_type='SAMPLE_REQ'` | Points `reference_id` at `sample_requisitions.id` |

### Migrations
| File | Lands |
|---|---|
| `app/db/031_sample_module.sql` | All 9 new tables + `material_document` extensions |
| `app/db/032_job_card_sample_fk.sql` | `job_card` extensions (after `feat/job-card-crud` merges) |

---

*End of design doc. Any change agreed during implementation should be back-noted into this file with a date stamp referencing the affected section.*
