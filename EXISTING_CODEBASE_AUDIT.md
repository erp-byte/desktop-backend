# EXISTING_CODEBASE_AUDIT.md

**Audit date:** 2026-04-25
**Audit author:** Production audit run by Claude (Opus 4.7, max-effort mode)
**Audit scope:** Read-only inventory of the Candor Foods backend in production, ahead of new module work (Modules 1–11). No code changes, no migrations, no deletions. Live AWS RDS Postgres was probed read-only via the project venv + asyncpg — full per-query output is preserved at `_audit_db_dump.txt` (937 KB) for re-readability.

> **Note on scope drift:** The audit prompt references a "33-table" design map. The file at `D:\Consumption\Material Recipt\Material Recipt\candor_schema_map.html` defines **15 tables** scoped to Module 4 (Material Receipt) only — not 33 across all modules. If a fuller 33-table cross-module map exists elsewhere, it was not located. Section 3 below diffs against the 15 tables that are documented; the §10 conflict matrix is consequently built from the explicit audit callouts (auth, log_edit, all_sku, po_line, customer-FK backfill, S3) plus inferences from live-DB and code, with all gaps flagged.

> **Note on database identity:** `.env` points to AWS RDS `warehouse_db` in ap-south-1 and `render.yaml` declares a Render-managed `consumption-db`. These are two separate databases. This audit was run against the AWS RDS instance because that is what `.env` references and what has 384 user tables and seeded data. The Render database was not probed (no DSN). **The "which DB is canonical for production" question is itself a top-tier blocker** (see §15).

---

## Table of contents

- [§1 Audit ground rules](#1-audit-ground-rules)
- [§2 Repository inventory](#2-repository-inventory)
- [§3 Database state vs schema map](#3-database-state-vs-schema-map)
- [§4 API surface analysis](#4-api-surface-analysis)
- [§5 Authentication & authorization](#5-authentication--authorization)
- [§6 Audit & logging mechanisms](#6-audit--logging-mechanisms)
- [§7 File storage state](#7-file-storage-state)
- [§8 External integrations inventory](#8-external-integrations-inventory)
- [§9 Dead code and tech debt](#9-dead-code-and-tech-debt)
- [§10 Conflicts with new module designs (Modules 1–11)](#10-conflicts-with-new-module-designs-modules-111)
- [§11 Performance baseline](#11-performance-baseline)
- [§12 Production data sensitivity](#12-production-data-sensitivity)
- [§13 Existing test infrastructure](#13-existing-test-infrastructure)
- [§14 Deployment topology](#14-deployment-topology)
- [§15 Top-10 blockers ranked by severity](#15-top-10-blockers-ranked-by-severity)
- [Appendix A: artifacts written by this audit](#appendix-a-artifacts-written-by-this-audit)

---

## §1 Audit ground rules

Reproduced from the prompt for record:

- Read-only. No code changes, no migrations, no deletions during audit.
- Be specific. "Exists" vs "missing" with table names, file paths, line numbers — not generalities.
- Cite sources. Every claim references a file path, table name, or DB object.
- Flag uncertainty. If something can't be determined without running code, mark `[VERIFY-RUNTIME]`.
- Do not propose fixes. Inventory and conflict-detect only.
- No caching. No Redis. All queries hit Postgres directly. Caching code in the existing codebase is documented but not replicated.

---

## §2 Repository inventory

### §2.1 Top-level layout (working dir `D:\Consumption\New\Backend\`)

| Path | Type | Purpose |
|---|---|---|
| `app/` | dir | FastAPI application (auth, so, purchase, production, amendments, webhooks) |
| `mcp_server.py` | file (83 KB) | MCP server — 77 tools, comprehensive production planning + jobs + inventory + day-end |
| `mcp_planner.py` | file (33 KB) | MCP server — 18 tools, planning subset (plan-gen + indents) |
| `mcp_tracker.py` | file (25 KB) | MCP server — 25 read-only tools mirroring mcp_server GETs |
| `mcp_viewer_server.py` | file (11 KB) | MCP server — 34 tools, restricted-read wrapper around mcp_server safe functions |
| `enrich_symbols.py` | file (13 KB) | **UNRELATED** — Indian stock-symbol enrichment (Nifty 100). Not Candor Foods. |
| `fyers_test.py` | file (13 KB) | **UNRELATED** — Fyers stockbroker symbol master downloader. Not Candor Foods. |
| `gen_pdf.py` | file (3.5 KB) | One-off doc converter (Production Module markdown → PDF) |
| `test_plan_generation.py` | file (13 KB) | Standalone Anthropic API test (gitignored) |
| `data/` | dir (~2.2 GB) | Excel reference snapshots (BOM, Physical Stock, Process Loss, SKU masters). Not imported by code. |
| `docs/` | dir (~640 KB) | API_REFERENCE, FRONTEND_WEBHOOK_INTEGRATION, FRONTEND_API_GUIDE, SO_PIPELINE_FLOW, etc. Actively maintained. |
| `Plan/` | dir | Project planning markdown (Production_Module_Complete_Reference.md ~50 KB and phase-specific subdirs). Archive — no code deps. |
| `scripts/` | dir (52 KB, 7 files) | One-off utilities: `migrate.py`, `ingest_allsku.py`, `ingest_physical_stock.py`, `verify_e2e.py`, `generate_db_diagrams_pdf.py`, `generate_inventory_report.py`, `list_so_tables.py`. Not imported by app/. |
| `WhatsApp Unknown 2026-04-23 at 11.47.08/` + `.zip` | dir + zip (415 KB) | 4 JPEG screenshots from mobile, dated 2026-04-23. Temp drop. |
| `.worktrees/feature/` | dir | Active git worktree(s); `lambda-deploy` worktree contains the 3 unit tests for Mangum/Settings/migrate that the main branch lacks. |
| `.env` | file (716 B) | **Plain-text live RDS creds, ANTHROPIC key, AUTH_ENCRYPTION_KEY, INTERNAL_WEBHOOK_TOKEN, WS_TOKEN_SECRET, MAIN_SERVER_URL.** Gitignored. |
| `.env.example` | file (423 B) | Sample env (committed) |
| `Dockerfile` | file | AWS Lambda container image (`public.ecr.aws/lambda/python:3.12`, CMD `app.main.handler`) |
| `render.yaml` | file | Render service definitions (3 web services + 1 PostgreSQL) |
| `Procfile` | file | `web: uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| `requirements.txt` / `pyproject.toml` / `uv.lock` | files | Dependency manifests (FastAPI 0.135.1, asyncpg 0.31.0, anthropic 0.86.0, mcp 1.26.0, mangum 0.19.0, boto3 1.38.0, PyJWT, cryptography). Note: pyproject lists `requires-python = ">=3.14"` — **Python 3.14 is unreleased; render.yaml pins 3.12 instead. Drift.** |
| `planning_structure.json` | file | JSON template for Claude AI plan responses (daily/weekly/revision schemas) |
| `sample_planning_prompt.txt` | file (10 KB) | Example system prompt for Claude planning calls |
| `_audit_db_dump.txt` / `_audit_db_probe.py` / `_audit_db_probe2.py` | files | **Written by this audit** — see Appendix A. Not part of repo; safe to delete. |

### §2.2 Per-file inventory (`app/`)

Domain legend: A=auth, MD=master-data (in code as state load), SO=sales-orders, PO=purchase-orders, PR=production, JC=job-cards, INV=inventory/floor, DE=day-end, DSC=discrepancy, AI=ai-planner, MRP=mrp, IND=indents, ALERT=store-alerts, AMD=amendments, WH=webhooks, INF=infra, U=utility.

| Domain | Path | Purpose | REST endpoints (sample) | DB tables touched | External deps | Tests? |
|---|---|---|---|---|---|---|
| INF | `app/main.py` (123 lines) | FastAPI app construction; lifespan with asyncpg pool, master-items load, dispatcher_loop+broadcaster_loop background tasks, Mangum handler at L122 | `/internal/events` (POST, HMAC-bearer) | (delegated) | mangum, asyncpg | NO (test_handler in worktree only) |
| INF | `app/config.py` (19 lines) | pydantic-settings Settings: DATABASE_URL, ANTHROPIC_API_KEY, STORAGE_BACKEND, STORAGE_LOCAL_BASE_DIR, QUEUE_BACKEND, SYSTEM_USER_ID, MAX_PDF_SIZE_MB, EXTRACTION_MAX_RETRIES, CLAUDE_MODEL, AUTH_ENCRYPTION_KEY, INTERNAL_WEBHOOK_TOKEN, WS_TOKEN_SECRET, WS_TOKEN_EXPIRY_MINUTES | — | — | — | NO (test_config in worktree) |
| INF | `app/db/connection.py` | asyncpg pool: `min_size=0, max_size=10`, no SSL, no keepalive | — | — | asyncpg | NO |
| INF | `app/db/*.sql` | DDL files (no Alembic): `auth_schema.sql`, `production_schema.sql`, `po_schema.sql`, `001_job_card_chain.sql`, `002_webhooks.sql`, `003_floor_machine_allocation.sql`, `production_migrate.sql`, `migrate.sql`, `ims_new_schema.sql`, `schema.sql` | (manages every table) | — | NO |
| U | `app/core/helpers.py` | `safe_float`, `safe_float_zero`, `safe_str` | — | — | — | NO |
| U | `app/core/types.py` | Pydantic types `Decimal3`, `Decimal3Z` | — | — | — | NO |
| A | `app/modules/auth/router.py` (~480 lines) | Login/logout/me/change-password + admin user/role/permission CRUD | `POST /api/v1/auth/login`, `POST /logout`, `GET /me`, `POST /change-password`, `GET/POST/PUT /users`, `GET/POST/PUT /roles`, `GET/POST/PUT /permissions` (~18 routes) | `auth_user`, `auth_role`, `auth_session`, `auth_permission`, `auth_role_permission` | cryptography (Fernet) | NO |
| A | `app/modules/auth/middleware.py` | `_extract_user`, `get_current_user`, `require_permission(module, sub_module, sub_sub_module, action)` factory | — | `auth_session`, `auth_role`, `auth_permission`, `auth_role_permission` | — | NO |
| A | `app/modules/auth/services/auth_service.py` | Fernet encrypt/decrypt, `login()`, `validate_session()`, session lifecycle | — | `auth_user`, `auth_role`, `auth_session` | cryptography | NO |
| A | `app/modules/auth/services/permission_service.py` | RBAC eval: hierarchical permission lookup, scope filtering (entities/warehouses/floors), admin bypass | — | `auth_permission`, `auth_role_permission` | — | NO |
| SO | `app/modules/so/router.py` | SO Excel/PDF upload + view + GST reconciliation | `POST /api/v1/so/upload`, `POST /upload-so-book`, `POST /update-preview`, `POST /update-confirm`, `PUT /update`, `GET /view`, `GET /export`, `GET /gst-reconciliation/summary`, `GET /{so_id}` (10 routes) | `so_header`, `so_line`, `so_gst_reconciliation`, `so_fulfillment`, `all_sku` | — | NO |
| SO | `app/modules/so/services/{ingest,updater,parser,item_matcher,gst_reconciliation,so_book_parser}.py` | Excel parsing, fuzzy item matching, GST reconcile, ingest into `so_*` | — | `so_header`, `so_line`, `so_gst_reconciliation`, `all_sku` | openpyxl, rapidfuzz | NO |
| SO | `app/modules/so/schemas/{header,line,gst,sku,update,response}.py` | Pydantic schemas | — | — | — | NO |
| PO | `app/modules/purchase/router.py` | PO Excel upload + view + receive + boxes | `POST /api/v1/purchase/upload`, `GET /view`, `GET /export`, `GET /summary`, `PUT /{transaction_no}/receive`, `PUT /{transaction_no}/boxes`, `POST /{transaction_no}/boxes` (7 routes) | `po_header`, `po_line`, `po_section`, `po_box` | — | NO |
| PO | `app/modules/purchase/services/{ingest,parser,queries}.py` | PO ingest + queries | — | `po_header`, `po_line`, `po_section`, `po_box` | openpyxl | NO |
| PR | `app/modules/production/router.py` (~3500 lines) | Production hub: fulfillment, plans, indents, orders, job-cards, inventory, day-end, discrepancies, RTV, AI recs | ~120+ routes spanning `/fulfillment/*`, `/plans/*`, `/orders/*`, `/job-cards/*`, `/inventory/*`, `/day-end/*`, `/discrepancy/*`, `/ai/*`, `/production-indents/*`, `/qc/*`, `/rtv/*`, `/material-documents/*` | `so_fulfillment`, `production_plan`, `production_plan_line`, `production_order`, `job_card` (+ 8 child tables), `floor_inventory`, `inventory_batch`, `material_document`, `purchase_indent`, `store_alert`, `production_indent`, `qc_inspection`, `discrepancy_report`, `ai_recommendation`, `offgrade_inventory`, `offgrade_reuse_rule`, `yield_summary`, `process_loss` | — | NO |
| PR | `app/modules/production/services/*` (~20 service modules: `fulfillment.py`, `ai_planner.py`, `job_card_engine.py`, `job_card_pdf.py`, `mrp.py`, `floor_tracker.py`, `day_end.py`, `discrepancy_manager.py`, `master_ingest.py`, `inventory_service.py`, `production_indent_service.py`, `store_controller.py`, `indent_manager.py`, `qc_service.py`, `rtv_disposition_service.py`, `amendment_service.py`, `material_document_service.py`, `lot_issuance_service.py`, `qr_service.py`, `idle_checker.py`) | Domain services | — | (per service) | anthropic (ai_planner.py), httpx (none directly), fpdf2 (job_card_pdf.py) | NO |
| AMD | `app/modules/amendments/...` (top-level `amendment_router.py` per main.py:75-81) | Amendment list + count | `GET /amendments`, `GET /amendments/count` | (amendment-related tables) | — | NO |
| WH | `app/webhooks/router.py` | CRUD for webhook endpoints, subscriptions, deliveries | `POST/GET/PUT/DELETE /webhooks/endpoints`, `POST/GET/DELETE /webhooks/subscriptions`, `GET /webhooks/deliveries` (~14 routes) | `webhook_endpoint`, `webhook_subscription`, `webhook_delivery` | — | NO |
| WH | `app/webhooks/event_bus.py` | In-process asyncio bus, deferred buffer in transactions, max queue=1000/subscriber | — | — | — | NO |
| WH | `app/webhooks/dispatcher.py` | Background httpx fan-out, max 3 attempts, backoff 10/40/90s, max 20 concurrent | — | `webhook_subscription`, `webhook_endpoint`, `webhook_delivery` | httpx | NO |
| WH | `app/webhooks/broadcaster.py` | Background WebSocket fan-out, role-event prefix map, admin wildcard bypasses entity filter | — | — | — | NO |
| WH | `app/webhooks/signer.py` | HMAC-SHA256 signing of outbound payloads, constant-time verify helper | — | — | — | NO |
| WH | `app/webhooks/ws_router.py` | `/api/v1/ws/token` (HS256 JWT, 5-min TTL) + WebSocket upgrade endpoint | `POST /api/v1/ws/token`, `WS /api/v1/ws/...` | `auth_session`, `auth_user` | PyJWT | NO |
| WH | `app/webhooks/events.py` | Service-facing event tools (`indent_sent`, `job_card_started`, etc.) — services call these without seeing Event/event_bus internals | — | — | — | NO |

**Files not committed in 6+ months in `app/`:** **None.** All `app/*.py` show commits since 2025-10-25. The only stale files in the repo are the unrelated root-level scripts (`enrich_symbols.py`, `fyers_test.py`) and the Backup snapshot (see §9).

### §2.3 MCP server inventory (root-level)

Each MCP server runs as its own Render web service (own Python process, own asyncpg pool). All read DATABASE_URL from env or `.env`. None enforce auth at MCP-protocol level — auth is delegated to the HTTP wrapper in front.

| Server | Tools | Pool | Mutates? | Calls into `app/`? |
|---|---|---|---|---|
| `mcp_server.py` (~1584 lines) | **77** | min 0, max 3 | Yes (~28 write ops) | Yes — imports `app.modules.production.services.{job_card_engine, mrp, indent_manager, day_end, floor_tracker, idle_checker, ai_planner, discrepancy_manager}` |
| `mcp_planner.py` (~713 lines) | **18** | min 0, max 3 | Yes (8: plan-save, approve, indent send/edit/bulk-send) | Yes — imports `app.modules.production.services.{mrp, indent_manager}` |
| `mcp_tracker.py` (~483 lines) | **25** | min 1, max 2 | No (read-only) | No |
| `mcp_viewer_server.py` (~290 lines) | **34** | reuses `mcp_server.get_pool()` | No | Indirect via mcp_server |

**Tool counts by domain (mcp_server.py):**

| Domain | Tools |
|---|---|
| Fulfillment & demand | 6 (`sync_fulfillment`, `get_planning_context`, `get_demand_summary`, `get_fulfillment_list`, `fy_review`, `carryforward_orders`) |
| Fulfillment mutations | 2 (`revise_fulfillment`, `cancel_fulfillment`) |
| Plans (view + create) | 8 (`save_production_plan`, `list_plans`, `get_plan_detail`, `create_manual_plan`, `edit_plan_line`, `add_plan_line`, `delete_plan_line`, `get_plan_template`) |
| Plan approval/cancel | 2 (`approve_plan`, `cancel_plan`) |
| MRP & material check | 2 (`run_mrp`, `check_material_availability`) |
| Indents | 7 (`list_indents`, `get_indent_detail`, `edit_indent`, `send_indent`, `send_bulk_indents`, `acknowledge_indent`, `link_indent_to_po`) |
| Alerts | 2 (`list_alerts`, `mark_alert_read`) |
| Production orders & job cards | 17 (create_orders, list/detail, generate_job_cards, list/detail/dashboards, assign, receive_material_qr, start, complete_step, record_output, complete, sign_off, close, force_unlock) |
| Job-card annexures | 5 (`add_environment_data`, `add_metal_detection`, `add_weight_checks`, `add_loss_reconciliation`, `add_remarks`) |
| Inventory & tracking | 10 (floor inventory/summary/movement, idle materials, offgrade list/rules/create, loss analysis/anomalies) |
| Day-end & balance scan | 8 (day_end_summary, submit_dispatch, balance scan submit/status/detail/reconcile, missing scans, yield_summary) |
| Discrepancy & AI | 7 (revise_plan, revision history, report/list/detail/resolve discrepancy, submit_ai_feedback) |
| Meta | 1 (`ping`) |

**MCP duplications flagged for §9:** `check_material_availability`, `list_indents`, `send_indent`, `send_bulk_indents`, `get_inventory`/`get_floor_inventory`, `get_bom_detail`, `sync_fulfillment`, `get_fulfillment_list`, `get_demand_summary`, `get_planning_context`, `save_production_plan`, `list_plans`, `get_plan_detail`, `approve_plan`, `edit_indent`, `check_material_availability`, `get_machine_master` — all duplicated 2× or 3× across mcp_server / mcp_planner / mcp_tracker.

---

## §3 Database state vs schema map

### §3.1 Live DB headline numbers

| Metric | Value |
|---|---|
| Postgres version | 17.4 on x86_64 Linux (gcc 12.4.0) |
| Probed DB | `warehouse_db` on AWS RDS `wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432` |
| Schemas (excl. system) | `public` only |
| User tables | **384** |
| Sequences | 314 |
| Indexes | 1,000+ (full list in dump §indexes) |
| Triggers | 135 (mostly `audit_capture()` triggers on the receipt-flow tables, plus utility triggers) |
| Tables without PK in `public` | 1 — `categorial_invoicewise_inv` (6.5 MB heap, 0 rows) |
| Unindexed FK columns | **89** (full list in dump §fk_no_index) |
| Extensions installed | citext 1.6, pg_trgm 1.6, pgcrypto 1.3, plpgsql 1.0, uuid-ossp 1.1 |
| `pg_stat_statements` | **NOT INSTALLED** — top-time profile unavailable |
| `pg_cron` / `timescaledb` | NOT installed |

### §3.2 Designed-vs-live diff (15 documented tables in `candor_schema_map.html`)

All 15 designed tables exist in live DB. None are MISSING. All 15 are `EXISTS_DIFFERENT_SCHEMA` — every table has additional implementation columns and minor type drifts. Receipt-flow DDL is **already deployed on RDS** with audit_capture() triggers wired; tables are empty (0 rows) except `qc_parameter_master` (11 seeded rows).

| # | Designed table | Live? | Status | Key deltas (full list in `_audit_db_dump.txt`) |
|---|---|---|---|---|
| 1 | `po_header` | Yes (44 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | +37 extra cols (entity, voucher_type, financial breakdowns, vehicle/transport, soft-delete `is_deleted`, etc.); `created_by text` (designed `uuid`); 5 indexes; audit_po_header trigger |
| 2 | `po_line` | Yes (25 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | +13 extra cols (uom, pack_count, rate, amount, particulars, item_type, sales_group, gst_rate, match_score, match_source, carton_weight, customer_id, sku_id); **`status` CHECK explicitly whitelists 12 values including legacy `'pending'` AND `'received'` ✓** |
| 3 | `coa_document` | Yes (9 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | `s3_urls jsonb DEFAULT '[]'::jsonb` instead of designed `file_url text`; `coa_id text` via `gen_short_id()` instead of UUID; idx_coa_line on (transaction_no, line_number) |
| 4 | `qc_inspection` | Yes (13 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | +3 extras: sample_size, sample_source, remarks; type drift on inspection_id/inspector_id (text not uuid); CHECKs on verdict/stage/next_action |
| 5 | `qc_parameter_master` | Yes (9 cols, **11 rows**) | EXISTS_DIFFERENT_SCHEMA | +is_active, +created_at; CHECKs on param_group/data_type; **only seeded designed table** |
| 6 | `qc_parameter_spec` | Yes (11 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | +enum_acceptable text[], +created_at; spec_id via gen_short_id() |
| 7 | `qc_inspection_parameter` | Yes (14 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | `s3_urls jsonb` instead of designed `evidence_file_url text`; reading_id text via gen_short_id() |
| 8 | `po_box` | Yes (14 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | +box_number int NOT NULL, +created_at; weighed_by text (vs uuid); idx_po_box_inspection + idx_po_box_txn |
| 9 | `ncr_record` | Yes (26 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | +product_description, +documented_date, +created_at; CHECKs on status/severity_rollup/disposition/financial_action/supplier_action_type/ncr_category |
| 10 | `ncr_parameter_detail` | Yes (10 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | All designed cols present; **PK only — no supporting indexes on FKs** ncr_no/param_id/reading_id |
| 11 | `ncr_supplier_action` | Yes (11 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | `s3_urls jsonb` instead of designed `evidence_file_url text`; **PK only — no idx on ncr_no** |
| 12 | `stage2_approval` | Yes (8 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | All designed cols present; **PK only — no idx on (transaction_no, line_number)** |
| 13 | `grn` | Yes (9 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | +posted_at; **PK only — no idx on transaction_no** |
| 14 | `receipt_event_log` | Yes (9 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | line_number is NULLable (designed expected NOT NULL); **NO FK constraint** to po_line (audit log is intentionally loosely-coupled, but FK absent); idx_event_log on (transaction_no, line_number, event_ts DESC) |
| 15 | `notification_log` | Yes (8 cols, 0 rows) | EXISTS_DIFFERENT_SCHEMA | transaction_no NULLable; CHECKs on recipient_role/channel; **PK only — no idx on transaction_no** |

**Cross-cutting design vs live drift:**
- ID strategy is `gen_short_id()`-text PKs, not UUID. Functionally equivalent but a deliberate divergence from the design's "uuid" notes.
- File-URL fields are `s3_urls jsonb DEFAULT '[]'::jsonb` (array of URLs) on coa_document, qc_inspection_parameter, ncr_supplier_action — not single `text`. Design is stale; implementation is better.
- `po_header.entity text NOT NULL CHECK` — multi-entity tenancy (cfpl/cdpl) is a fundamental concept the design doesn't model.
- `po_header.is_deleted bool` — soft-delete pattern in live DB, not in design.

### §3.3 Audit callouts confirmed exactly

| Callout | Verdict | Evidence |
|---|---|---|
| `all_sku` has `gst` (not `gst_rate`), `batch_strategy`, `min_shelf_life_days`, ~3,685 rows | **CONFIRMED ✓** | All 3 columns present and named correctly. Row count exactly **3,685**. `batch_strategy` defaults `'FIFO'` with CHECK; `min_shelf_life_days` defaults `0`. (`po_line.gst_rate` is a separate column on a different table.) |
| `log_edit` is legacy SO audit table parked for Module 10 migration | **CONFIRMED ✓** | Table exists with 12 cols and 7 indexes (idx_log_edit_changed_at, idx_log_edit_record (table_name, record_id), idx_log_edit_request, idx_log_edit_action, idx_log_edit_at, idx_log_edit_user, idx_log_edit_table), 0 rows. **No active writers/readers in current Python code** — only referenced by `app/db/migrate.sql:16-17` which drops NOT NULL on changed_by. Replacement infra (`audit_log` + `audit_policy` (30 rows seeded) + `commit_log` + generic `audit_capture()`) is live and wired on receipt-flow tables. |
| `po_line.status` has `'pending'` and `'received'` legacy values | **CONFIRMED ✓** | CHECK constraint `po_line_status_chk` whitelists 12 values: `pending, draft, dock_arrived, coa_pending, qc_stage1_pending, qc_rejected, stage2_pending, correction, approved, grn_generated, closed, received`. Default `'draft'`. Currently 0 rows — no row-level breakdown possible. |
| 8 tables with free-text customer references awaiting `customer_id` FK backfill | **CONFIRMED ✓** | The 8 in-scope tables: `so_header`, `so_fulfillment`, `bom_header`, `production_plan_line`, `production_order`, `production_indent`, `purchase_indent`, `job_card`. Only `po_line.customer_id (text) → customer_master.customer_id (text)` is FK-typed today (`po_line_customer_id_fkey`). Two competing customer masters exist: `customer_master (text PK)` is the only one FK-targeted; `mst_customer (int PK)` is parallel. |

### §3.4 Coexistence overlap (live tables overlapping designed tables)

| Designed | Overlapping legacy | Rows | Risk |
|---|---|---|---|
| `po_header`/`po_line`/`po_box` | `purchase_orders`, `po_items`, `po_item_boxes` (4.4 MB allocated, 0 rows), `po_section`, `purchase_approvals`/`purchase_approval_items`/`purchase_approval_boxes` | 0 each | Schema-only overlap (dead weight) |
| `po_header`/`po_line` | `purchase_indent` | **26 rows** | **Real overlap** — 26 indent rows live; if new design doesn't model indents, no migration target |
| `qc_inspection`/`qc_inspection_parameter` | `quality_inspection`, `qc_holds`, `qc_floors`, `qc_factories`, `qc_users`, `qc_approvers`, `qc_module_permissions` (162 rows), `qc_user_permissions` (6 rows) | mostly 0; `qc_module_permissions` 162 | Permissions overlap with new auth model — needs decision |
| `grn` | `po_header.grn_number text` + `po_header.system_grn_date` | n/a (po_header empty) | New design promotes GRN to its own table; backfill required when po_header populates |
| `all_sku` | `cfplsku` (1 row), `cdplsku` (1 row), `ipqc_sku`, `sku`, `cfplitems`, `cdplitems`, `mfg_items` | mostly 0 / 1 | Multiple SKU masters coexist; should converge on `all_sku` |
| Customer master | `customer_master` (text PK), `mst_customer` (int PK), `customers`, `cdpl_customers`, `cfpl_customers` | 0 each | Two competing canonical customer masters |
| Audit | `log_edit` legacy + `audit_log`/`audit_policy`/`commit_log` new | 0 vs 30+ rows in policy seed | Two parallel audit systems; cutover plan needed |

### §3.5 Largest live tables (top 20)

| Table | Total | Heap | Indexes | Rows |
|---|---|---|---|---|
| cdpl_cold_stocks | 25 MB | 21 MB | 4824 kB | 60,657 |
| sales_upload_template | 23 MB | 17 MB | 6600 kB | 0 |
| cfpl_cold_stocks | 18 MB | 14 MB | 3592 kB | 44,491 |
| cfpl_boxes_v2 | 16 MB | 6776 kB | 9280 kB | 48,564 |
| cfpl_boxes | 8920 kB | 3768 kB | 5112 kB | 0 |
| mis_temp_debtors | 7640 kB | 3632 kB | 3968 kB | 0 |
| categorial_invoicewise_inv | 6728 kB | 6688 kB | 0 bytes | **0 (no PK!)** |
| cdpl_boxes_v2 | 4528 kB | 1832 kB | 2656 kB | 12,645 |
| po_item_boxes | 4384 kB | 2272 kB | 2072 kB | 0 |
| mis_debtor_invoicetotals | 3632 kB | 1536 kB | 2056 kB | 0 |
| cat_inv_items | 3384 kB | 864 kB | 2480 kB | 0 |
| interunit_transfer_in_boxes | 2624 kB | 1560 kB | 1024 kB | 9,882 |
| cfpl_bulk_entry_boxes | 2520 kB | 976 kB | 1504 kB | 3,200 |
| interunit_transfers_lines | 2504 kB | 1744 kB | 720 kB | 11,051 |
| vis_visitors | 2136 kB | 1392 kB | 312 kB | 586 |
| cfplitems | 1960 kB | 720 kB | 1200 kB | 0 |
| auth_role_permission | 1936 kB | 1152 kB | 744 kB | 0 |
| cfplsku | 1896 kB | 744 kB | 1112 kB | 1 |
| cdpl_bulk_entry_boxes | 1760 kB | 584 kB | 1136 kB | 0 |
| categorial_item_fastlookup | 1744 kB | 448 kB | 1256 kB | 0 |

### §3.6 Triggers / functions / sequences / extensions

- **135 triggers** — `audit_capture()` is the dominant pattern (INSERT/UPDATE/DELETE on po_header, po_line, po_box, coa_document, qc_inspection, qc_inspection_parameter, qc_parameter_master, qc_parameter_spec, ncr_record, ncr_parameter_detail, ncr_supplier_action, stage2_approval, grn, receipt_event_log, notification_log).
- **314 sequences** — many start_value=1, increment=1; some appear orphaned (target table empty).
- **Extensions installed:** citext 1.6, pg_trgm 1.6, pgcrypto 1.3, plpgsql 1.0, uuid-ossp 1.1.
- **Extensions NOT installed:** `pg_stat_statements`, `pg_cron`, `timescaledb`, `pg_partman`.

---

## §4 API surface analysis

### §4.1 REST endpoints (FastAPI)

`app/main.py` registers these routers (lines 75–81):

| Router | Prefix | Auth gating |
|---|---|---|
| `auth_router` | `/api/v1/auth` | Per-route `_require_auth()` / `_require_admin()` |
| `so_router` | `/api/v1/so` | **NONE** — open |
| `purchase_router` | `/api/v1/purchase` | **NONE** — open |
| `production_router` | `/api/v1/production` | **NONE on most** — open |
| `amendment_router` | `/api/v1/amendments` | [VERIFY-RUNTIME] |
| `webhook_router` | `/api/v1/webhooks` | `Depends(require_permission(...))` per route (`webhooks/router.py:43`) |
| `ws_router` | (WebSocket) | HS256 JWT via `WS_TOKEN_SECRET` |

Plus one top-level POST: `/internal/events` (HMAC `INTERNAL_WEBHOOK_TOKEN`, `app/main.py:99-119`).

**Verdict labels** for each route group (for §10 conflict planning):

| Route group | Verdict |
|---|---|
| `/api/v1/auth/*` | KEEP_AS_INTEGRATION (will be replaced by Module 1 with same shape) |
| `/api/v1/so/*` | TO_REPLACE — likely Module 2 (Master/SO ingest) or Module 4 (PO chain). Currently auth-less. |
| `/api/v1/purchase/*` | TO_REPLACE — Module 4 (Material Receipt) work will own this surface. Currently auth-less. |
| `/api/v1/production/*` | KEEP — large surface, ~120 routes; modules outside Module 4 likely keep most of this |
| `/api/v1/amendments/*` | KEEP — small surface |
| `/api/v1/webhooks/*` | KEEP_AS_INTEGRATION (the new webhook bus is what other modules emit through) |
| `/internal/events` | KEEP_AS_INTEGRATION (MCP→backend bridge) |
| WebSocket `/api/v1/ws/*` | KEEP_AS_INTEGRATION |

### §4.2 GraphQL endpoints

**None.** No `strawberry`, `graphene`, `ariadne` imports; no schema files. GraphQL is not in the codebase.

### §4.3 MCP tools

3 active MCP servers (the 4th, `mcp_viewer_server.py`, is a wrapper). Total **120 unique tools** across the three servers (see §2.3 for breakdown). All MCP tools currently bypass the FastAPI HTTP-level auth — auth is delegated to whatever fronts the MCP HTTP wrapper.

### §4.4 Webhooks

**Outbound (the new internal bus, April 2026 work):** ~25 event types fired from production services. Delivered to registered `webhook_endpoint` rows via `httpx.AsyncClient.post()` with HMAC-SHA256 signature. See §8.

**Inbound from external systems:** **NONE.** No courier, payment-gateway, ERP, or banking callback endpoints found in the code.

---

## §5 Authentication & authorization

### §5.1 What protects production today (one-line answer)

**Session-based opaque-UUID tokens for HTTP, plus HS256 short-lived JWTs for WebSocket only.** RBAC via `auth_role` × `auth_permission` with `(entities[], warehouses[], floors[])` scope filters. **Admin role bypasses scope filter.** `auth/*`, `webhooks/*`, and WebSocket are gated. **`so/*`, `purchase/*`, and most `production/*` routes are unauthenticated** — a logged-out user can hit upload/view/fulfillment endpoints today.

### §5.2 Detailed table

| Concern | Current state | Evidence |
|---|---|---|
| HTTP session model | Opaque UUID tokens issued at `/api/v1/auth/login`; stored in `auth_session` table; 24-hour TTL; bearer-token transport | `app/modules/auth/services/auth_service.py:48-104, 107-144` |
| JWT issuance | **HTTP: NO**; **WebSocket: HS256 5-min TTL** | `app/webhooks/ws_router.py:27, 40, 54, 61` |
| Password storage | Fernet (AES-128 equivalent) symmetric encryption — **not a hash, decryptable with `AUTH_ENCRYPTION_KEY`** | `app/modules/auth/services/auth_service.py:8, 33-45` |
| User table | `auth_user`: user_id (PK), phone (UNIQUE), password_encrypted (Fernet), full_name, email, role_id, entity, allowed_warehouses[], is_active, created_at, last_login_at | `app/db/auth_schema.sql:19-31` |
| Seed admin | Phone `9004464207`, password hash provided in seed | `app/db/auth_schema.sql:99-107` |
| Role model | DB-backed: `auth_role` (8 seed roles: admin, planner, inventory_manager, team_leader, qc_inspector, floor_manager, purchase_manager, viewer); `is_admin` bool | `app/db/auth_schema.sql:7-13, 87-96` |
| Permission model | Hierarchical (module → sub_module → sub_sub_module × action), ~100+ seed permissions, `auth_role_permission` carries `allowed_entities[]`, `allowed_warehouses[]`, `allowed_floors[]` scope | `app/db/auth_schema.sql:41-49, 55-62, 114-195` |
| Permission eval | Admin (`is_admin=true`) bypass at top; hierarchical fallback exact → broader; **scope mismatch returns False immediately (HIGH-5 safety)** | `app/modules/auth/services/permission_service.py:8-67` |
| Per-route gate (auth_router) | `_require_auth()` (`auth/router.py:462-476`) and `_require_admin()` (`auth/router.py:479-484`) on all admin endpoints | `app/modules/auth/router.py:144,160,179,207,223,241,254,271,287,316,334,365,384,411,427` |
| Per-route gate (webhooks) | `Depends(require_permission("production","webhooks", action="create"))` etc. | `app/webhooks/router.py:43` |
| **Per-route gate (so/purchase/production)** | **NONE** — no Depends() that wraps these handlers in any auth check | `app/modules/so/router.py:42-73, 76-104, 107+`; `app/modules/purchase/router.py:33-63, 71+`; `app/modules/production/router.py:44+` |
| Service-to-service auth | `Authorization: Bearer {INTERNAL_WEBHOOK_TOKEN}` with `hmac.compare_digest()` on `/internal/events` | `app/main.py:103, 108` |
| Webhook outbound auth | Per-endpoint `secret` HMAC-SHA256 → `X-Webhook-Signature: sha256=<hex>` | `app/webhooks/signer.py:7-11, 14-17` |
| CORS | `allow_origins=["*"]`, `allow_methods=["*"]`, `allow_headers=["*"]` — fully open | `app/main.py:68-73` |
| Admin entity bypass | `if "*" not in role_prefixes and info["entity"] != event.entity: continue` — admin role wildcard waives entity scoping | `app/webhooks/broadcaster.py:80-82`; `ROLE_EVENT_MAP` (`broadcaster.py:16-22`) — only `admin` has `["*"]` |

### §5.3 Secrets in the auth path

| Secret | Source | Consumed in | Protects |
|---|---|---|---|
| `AUTH_ENCRYPTION_KEY` | `.env`/env | `app/modules/auth/services/auth_service.py:16-30` | Fernet password encryption |
| `WS_TOKEN_SECRET` | `.env`/env | `app/webhooks/ws_router.py:27, 54` | HS256 WebSocket JWT signing |
| `WS_TOKEN_EXPIRY_MINUTES` | `.env`/env (default 5) | `app/webhooks/ws_router.py:27` | WebSocket JWT TTL |
| `INTERNAL_WEBHOOK_TOKEN` | `.env`/env | `app/main.py:103, 108` | HMAC bearer for `/internal/events` |
| Per-endpoint webhook `secret` | `webhook_endpoint` table | `app/webhooks/signer.py:7-11` | Outbound HMAC payload signing |

---

## §6 Audit & logging mechanisms

### §6.1 `log_edit` (legacy SO audit)

- **Schema (`app/db/schema.sql:91-106` + live DB):** log_id BIGSERIAL PK, table_name, record_id, field_name, action enum, old_value, new_value, changed_by, changed_at, request_id, module, note. 7 supporting indexes (record, changed_at, request, action, at, user, table).
- **Writers in current Python code:** **NONE** found.
- **Readers in current Python code:** **NONE** found.
- **Live row count:** 0.
- **Status:** Empty, parked, no active code path. Replacement infrastructure is live (see §6.2).

### §6.2 New audit infrastructure (live in DB, not yet evident in Python service code)

- **`audit_log`** — generic audit row store (15 cols).
- **`audit_policy`** — policy table (30 rows seeded; defines what to capture per table).
- **`commit_log`** — commit-level audit grouping.
- **`entity_snapshot`** — periodic snapshots.
- **`audit_capture()` trigger function** wired on all 15 receipt-flow tables (audit_po_header, audit_po_line, audit_po_box, audit_coa_document, audit_qc_inspection, audit_qc_inspection_parameter, audit_qc_parameter_master, audit_qc_parameter_spec, audit_ncr_record, audit_ncr_parameter_detail, audit_ncr_supplier_action, audit_stage2_approval, audit_grn, audit_event_log, audit_notification_log — 135 triggers in DB).

**Implication:** Audit is captured at **DB-trigger level** for receipt-flow tables. Python service layer is unaware. Module 10 should design around this — either treat trigger output as the source of truth and remove `log_edit` entirely, or document the dual model.

### §6.3 Other audit-shaped tables in DDL (write/read state unknown without runtime verification)

| Table | DDL location | Purpose | Active? |
|---|---|---|---|
| `webhook_delivery` | `app/db/002_webhooks.sql:25-37` | Webhook delivery log | YES — `app/webhooks/dispatcher.py` writes status/attempts/response |
| `amendment_log` | `app/db/ims_new_schema.sql:238-245` | SO amendment audit | [VERIFY-RUNTIME] |
| `fifo_skip_log` | `app/db/ims_new_schema.sql:137-145` | FIFO batch skip decisions | [VERIFY-RUNTIME] |
| `batch_rejection_log` | `app/db/production_migrate.sql:243-256` | Batch rejection audit | [VERIFY-RUNTIME] |
| `inventory_event_log` | `app/db/production_schema.sql:743-763` | Inventory movement audit | [VERIFY-RUNTIME] |
| `so_revision_log` | `app/db/production_schema.sql:125+` | SO revision history | [VERIFY-RUNTIME] |
| `batch_block_history` | `app/db/production_schema.sql:764+` | Batch block state changes | [VERIFY-RUNTIME] |
| `cascade_events` | `app/db/production_migrate.sql:258+` | Force reassign indent cascades | [VERIFY-RUNTIME] |
| `legacy_import_log` | `app/db/production_migrate.sql:298+` | Data import audit | [VERIFY-RUNTIME] |

### §6.4 Application logging

- **Style:** `logging.getLogger()` + `basicConfig(level=INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")` (`app/main.py:30`). Plain text, not structured JSON.
- **Destination:** stdout (Render captures it; on Lambda, CloudWatch).
- **Sentry / structlog / loguru:** none.
- **Sample log sites:** `auth_service.py:85` (login user_id+role only — no PII), `event_bus.py:59-63` (queue full warnings), `dispatcher.py:42, 51` (start/stop/exceptions), `broadcaster.py:49, 111` (WS connect / errors).

### §6.5 Correlation IDs

**Not implemented.**
- `request_id` exists as a column in `log_edit` but no code populates it.
- No `X-Request-ID` middleware.
- No `contextvars` for trace propagation.
- Event IDs (`Event.event_id` UUID) exist in the webhook bus but are not joined back to HTTP request IDs.

### §6.6 Internal event/webhook event types emitted

~25 event types (sample, from `events.py` and call sites in `app/modules/production/services/`):

`fulfillment.synced`, `fulfillment.revised`, `plan.approved`, `mrp.completed`, `mrp.shortage_detected`, `indent.drafted`, `indent.sent`, `indent.bulk_sent`, `indent.raised`, `job_card.created`, `job_card.started`, `job_card.completed`, `job_card.team_assigned`, `job_card.material_received`, `job_card.material_acknowledged`, `job_card.dispatched_to_next`, `job_card.output_saved`, `job_card.signed_off`, `job_card.force_unlocked`, `qc.passed`, `qc.failed`, `material.moved`, `dayend.reconciled`, `dayend.discrepancy_found`, `store_alert.created`.

**Verification status (`docs/superpowers/plans/2026-04-14-webhook-websocket-event-system.md:13-21`):** Task 11 steps 1–6 verified (commits `b6b0cbf`, `9669379`); steps 7–N in flight (`cce90bd`, `aebcf5f`). **Open design gap:** broadcaster entity-filter for non-admin roles when user has `entity=null/""` (currently they receive nothing) — flagged "decide before Android rollout".

---

## §7 File storage state

### §7.1 Configured backend

- **`.env`:** `STORAGE_BACKEND=local`, `STORAGE_LOCAL_BASE_DIR=./so_pdfs`.
- **`app/config.py:7-8`:** Same defaults.
- **S3 references in code:** **ZERO**. No `boto3.client('s3')`, no `S3_BUCKET`, no `candor-docs-prod`, no `put_object`/`get_object`/`presigned_url`. (`boto3==1.38.0` is in `requirements.txt` — pulled in by Mangum/Lambda prep.)

### §7.2 Local disk reality

- Directory `D:\Consumption\New\Backend\so_pdfs\` **does not exist** on the audit host.
- Upload endpoints (`so/router.py:42-73`, `purchase/router.py:33-63`) call `await file.read()` and pass bytes to ingest functions in-memory. **No `open(..., 'wb').write()`, no `aiofiles.write()` in upload paths.**
- 50 MB upload size limit hardcoded at the route handlers.

### §7.3 Vendor docs / COAs / NCR evidence

| Asset | Storage today | Notes |
|---|---|---|
| Vendor docs | **NOT IMPLEMENTED** | No upload endpoint found |
| COAs | **NOT IMPLEMENTED** in code, but DB column `coa_document.s3_urls jsonb DEFAULT '[]'::jsonb` exists | DDL is ready (live), Python upload path is not |
| NCR evidence | **NOT IMPLEMENTED** in code, but DB column `ncr_supplier_action.s3_urls jsonb` exists | Same — DDL ready, code missing |
| QC reading evidence | **NOT IMPLEMENTED** in code, but `qc_inspection_parameter.s3_urls jsonb` exists | Same — DDL ready, code missing |

### §7.4 Bucket provisioning

`candor-docs-prod` (region `ap-south-1`): **Not referenced anywhere in code.** Whether the bucket exists in AWS cannot be determined from this audit. `[VERIFY-RUNTIME]` via AWS Console or `aws s3api head-bucket --bucket candor-docs-prod --region ap-south-1`.

### §7.5 Summary table

| Asset type | Storage backend | Path/bucket | Code path | Status |
|---|---|---|---|---|
| Sales Register Excel upload | Memory (no persist) | — | `app/modules/so/router.py:52` | EPHEMERAL |
| PO Book Excel upload | Memory (no persist) | — | `app/modules/purchase/router.py:45` | EPHEMERAL |
| Vendor docs | — | — | — | NOT IMPLEMENTED |
| COAs | DB-jsonb-array of URLs (DDL) / no code | `coa_document.s3_urls` | None | DDL READY, CODE MISSING |
| NCR evidence | DB-jsonb-array (DDL) / no code | `ncr_supplier_action.s3_urls` | None | DDL READY, CODE MISSING |
| Configured local disk | `STORAGE_BACKEND=local`, dir absent | `./so_pdfs/` | None | DEAD CONFIG |
| Configured S3 bucket | Not referenced | `candor-docs-prod` (ap-south-1) | None | UNREFERENCED |

---

## §8 External integrations inventory

### §8.1 Outbound integration channels

| Channel | Provider | Library | Code? | Status |
|---|---|---|---|---|
| Email | SES / SMTP / SendGrid | none | none | **NOT FOUND** |
| WhatsApp | Twilio / Meta Cloud / Wati | none | none | **NOT FOUND** |
| SMS | Twilio / MSG91 / Gupshup | none | none | **NOT FOUND** |
| Slack | webhooks / SDK | none | none | **NOT FOUND** |
| ERP push | HTTP / SOAP / file drop | none | none | **NOT FOUND** |
| Courier — Safexpress | HTTP API | none | none | **NOT FOUND** (audit prompt assumed wired) |
| Courier — DTDC | HTTP API | none | none | **NOT FOUND** |
| Courier — Rivigo | HTTP API | none | none | **NOT FOUND** |
| Courier — VRL | HTTP API | none | none | **NOT FOUND** |
| Biometric | HTTP / TCP | none | none | **NOT FOUND** |
| VMS | HTTP | none | none | **NOT FOUND** |
| CCP X-Ray | HTTP | none | none | **NOT FOUND** |
| Anthropic | Claude API | `anthropic==0.86.0` | `app/modules/production/services/ai_planner.py` (used via MCP planner via Claude Desktop, not direct in-process HTTP) | **PRESENT but indirect** |
| Outbound HTTP webhooks | — | `httpx==0.28.1` | `app/webhooks/dispatcher.py` | **PRESENT (the new internal webhook bus)** |

**Verdict:** The audit prompt's claim that couriers are "already wired up" is not true in the codebase as it stands. They may be wired up in a sister system (e.g. the Render-hosted MCP servers' HTTP wrapper, or an Android/Web app talking directly to courier APIs), but **nothing in `D:\Consumption\New\Backend\` calls a courier**.

### §8.2 Inbound webhooks (external callbacks)

**ZERO inbound webhook endpoints** for external systems (banking, payment gateway, ERP, courier callbacks). The only "internal-bridge" inbound is `/internal/events` (HMAC-bearer from MCP servers — `app/main.py:99-119`).

### §8.3 Internal webhook bus (April 2026 work)

- **Architecture:** in-process asyncio bus, dispatcher to httpx-posted endpoints, broadcaster to WebSocket clients. No external broker, no Redis, no Celery.
- **Tables:** `webhook_endpoint` (1 row live), `webhook_subscription` (1 row), `webhook_delivery` (4 rows).
- **Auth:** outbound HMAC-SHA256 per-endpoint secret; inbound from MCP via shared `INTERNAL_WEBHOOK_TOKEN`; WebSocket via HS256 JWT (5-min TTL via `WS_TOKEN_SECRET`).
- **Retry:** 3 attempts, 10/40/90s backoff (`dispatcher.py:39-79`); max 20 concurrent deliveries.
- **Buffering inside DB transactions:** `event_bus.deferred_events()` context manager flushes only on commit (`event_bus.py:104-128`).

### §8.4 Scheduled tasks

**NONE.**
- No `celery`, `apscheduler`, `crontab`, `pg_cron` in code or extensions.
- `render.yaml` defines only web services (no cron jobs).
- Lambda EventBridge: no `serverless.yml`, no `template.yaml`, no SAM/CDK config.

All time-based behaviour is request- or MCP-tool-driven.

### §8.5 Render vs Lambda — which deployment is live?

- **Render:** ACTIVE. `render.yaml` defines 3 services + 1 PostgreSQL. `MAIN_SERVER_URL=https://desktop-backend-vhf0.onrender.com` confirms the live host.
- **Lambda:** STANDBY. `Dockerfile` targets `public.ecr.aws/lambda/python:3.12`, `app/main.py:122` defines `handler = Mangum(app, lifespan="on")`, `requirements.txt` includes `mangum==0.19.0` and `boto3==1.38.0`. Recent commits (`23047bf`, `cd0f72f`) add Lambda design docs. `.worktrees/feature/lambda-deploy` carries the migration scripts and the only unit tests in the repo. **Not deployed.**

---

## §9 Dead code and tech debt

### §9.1 DELETE-CANDIDATES (informational)

| Path | Verdict | Rationale |
|---|---|---|
| `enrich_symbols.py` | DELETE_CANDIDATE | Indian stock-symbol enrichment (Nifty 100, sector mapping). 0 internal references. Unrelated to Candor Foods. |
| `fyers_test.py` | DELETE_CANDIDATE | Fyers stockbroker symbol-master downloader. 0 internal references. Unrelated. |
| `test_plan_generation.py` | DELETE_CANDIDATE | Standalone Anthropic API test. Already gitignored. Not part of any test suite. |
| `gen_pdf.py` | DELETE_CANDIDATE | One-off markdown→PDF for Production Module ref. Already gitignored. |
| `_audit_db_probe.py`, `_audit_db_probe2.py`, `_audit_db_dump.txt` | DELETE_CANDIDATE | Written by this audit (read-only DB probes). Safe to remove after review. |
| `WhatsApp Unknown 2026-04-23 at 11.47.08/` + `.zip` | DELETE_CANDIDATE | 4 JPEG screenshots from mobile, 415 KB zip. Temp drop, not referenced. |
| `c:\Candor\SSD files of Candor\Consumption\Backend\` | ARCHIVE | 25-day-old snapshot of the same repo (Backend.zip 9.6 MB included). MCP files dated Mar 28-30 vs current Apr 18. Move to cold storage or delete. |

### §9.2 NEEDS_HUMAN_REVIEW — MCP cross-server duplication

The same async function logic is reimplemented in 2 or 3 of the 3 active MCP servers:

| Tool | mcp_server.py | mcp_planner.py | mcp_tracker.py | Notes |
|---|---|---|---|---|
| `check_material_availability` | L738 | L597 | — | Duplicate |
| `list_indents` | L750 | L503 | L127 | Triplicate |
| `send_indent` | L808 | L544 | — | Duplicate |
| `send_bulk_indents` | L825 | L568 | — | Duplicate |
| `get_inventory` / `get_floor_inventory` | L1193 | L653 | L267, L421 | Triplicate (and tracker has it twice) |
| `get_bom_detail` | (in mcp_server) | L672 | L440 | Triplicate |
| `sync_fulfillment` | L120 | L110 | L58 | Triplicate |
| `get_fulfillment_list` | L327 | L139 | L58 | Triplicate |
| `get_demand_summary` | L297 | L161 | L77 | Triplicate |
| `get_planning_context` | L174 | L179 | — | Duplicate |
| `save_production_plan` | L364 | L335 | — | Duplicate |
| `list_plans` | L471 | L430 | L93 | Triplicate |
| `get_plan_detail` | L500 | L450 | L108 | Triplicate |
| `approve_plan` | L673 | L470 | — | Duplicate |
| `edit_indent` | L784 | L521 | — | Duplicate |
| `get_machine_master` | (mcp_server) | L623 | L391 | Duplicate |

Risk: a fix in one server doesn't propagate. Likely refactor target: extract shared service module that all three import.

### §9.3 RETAIN

- `.worktrees/feature/lambda-deploy` — 5 commits ahead of master, contains the only unit tests in the repo + Mangum/migrate config. Decision needed: merge or close.
- `scripts/` — 7 one-off ingest/diagram/verify utilities. Useful even though not imported by app code.

### §9.4 Files with no commits in 6+ months in `app/`

**None.** Every Python file under `app/` has commits since 2025-10-25.

### §9.5 Commented-out blocks > 20 lines

- `app/modules/so/services/so_book_parser.py:41` — 23 consecutive comment lines. Inspection suggests these are documentation headers, safe to retain.

(Full grep is in `_audit_db_dump.txt`; no other long-comment-block findings of note.)

---

## §10 Conflicts with new module designs (Modules 1–11)

> **Caveat:** The per-module prompts for Modules 1–11 are not present in this audit's input. The matrix below maps from the audit prompt's explicit callouts (Auth=M1, Material Receipt=M4 per the schema map, Audit=M10) plus inferences from live-DB state and codebase domain coverage. **Where a module assignment is inferred, it is marked `[INFER]` and the conflict is the table delta itself, regardless of which module owns it.** When the actual module prompts arrive, this section needs verification.

### §10.1 Module 1 — Auth

| Assumption made by new module | Current state | Conflict / blocker | Owner / resolution path |
|---|---|---|---|
| JWT issuance for HTTP | Opaque UUID tokens in `auth_session` (24h TTL). No HTTP JWT today. | **MEDIUM** — existing live sessions in `auth_session` need a migration plan or coexistence period. WebSocket already uses HS256 JWT — unify on HS256 JWT for HTTP too, or document why HTTP stays opaque-token. | Auth team. Decide token scheme; if cutover, run both simultaneously for a deprecation window. |
| Role + permission RBAC | DB-backed: `auth_role` (8 seed roles), `auth_permission` (~100 seed perms), `auth_role_permission` with `(allowed_entities[], allowed_warehouses[], floors[])` scope. Admin bypass at top of permission_service. | Already largely in place. Conflict only if Module 1 redefines the table shape. | Auth team. If Module 1 keeps table shape, this is not a conflict — only the JWT cutover is. |
| Per-route auth on all endpoints | `auth/*` and `webhooks/*` gated; **`so/*`, `purchase/*`, `production/*` are unauthenticated today** | **HIGH** — Module 1 must close this gap atomically with rollout, or new modules will inherit open routes. | Auth team. Plan a global `Depends()` middleware or per-router gate sweep before Module 1 ships. |
| Password storage | Fernet symmetric encryption (decryptable with `AUTH_ENCRYPTION_KEY`). Not a hash. | **HIGH** — Industry-standard is Argon2id/bcrypt hash, not symmetric encryption. If Module 1 specifies hashing, **all existing user passwords need re-encoding (force-password-reset flow)**. | Auth team. Decide hash algorithm; plan migration script + forced reset flow. |
| Single user table | `auth_user` + legacy `iam_users`, plus `qc_users`, `qc_approvers` (both used by `qc_module_permissions` 162 rows / `qc_user_permissions` 6 rows) | **MEDIUM** — Two parallel user worlds. Decide canonical table. `iam_users` is FK-targeted by `log_edit.changed_by`. | Auth team. Map iam_users → auth_user; deprecate qc_users/qc_approvers; migrate qc_module_permissions into auth_role_permission. |
| Single role/perm tables | `auth_role`/`auth_permission`/`auth_role_permission` AND legacy `iam_roles`/`iam_perms`/`iam_role_perms` | **MEDIUM** — Two parallel RBAC systems. | Auth team. Confirm `iam_*` is fully deprecated; plan removal. |
| Single session table | `auth_session` AND legacy `iam_tokens_refresh`, `iam_tokens_revoked` | **LOW** — Tokens table likely empty; verify and drop. | Auth team. |
| CORS | `allow_origins=["*"]` | **MEDIUM** — Module 1 should lock CORS to known frontends. | Auth team. Add allow-list. |

### §10.2 Module 2 (inferred — Master Data: SKU/customer/vendor)

| Assumption made by new module | Current state | Conflict / blocker | Owner / resolution path |
|---|---|---|---|
| Single SKU master `all_sku` | `all_sku` (3,685 rows) is the canonical text-keyed master with `gst`, `batch_strategy`, `min_shelf_life_days`. Coexists with `cfplsku` (1 row), `cdplsku` (1), `ipqc_sku`, `sku`, `cfplitems`, `cdplitems`, `mfg_items`. | **MEDIUM** — Decision needed: are the legacy SKU tables read by anything? `scripts/ingest_allsku.py` exists. | Master-data team. Confirm cutover; drop unreferenced tables. |
| Single customer master | Two tables: `customer_master` (text PK, the FK target of `po_line.customer_id`) and `mst_customer` (int PK, parallel). Plus `customers`, `cdpl_customers`, `cfpl_customers` (all 0 rows). | **HIGH** — Choose canonical. Backfill the 8 free-text customer columns to FK against the chosen master (see §3.3). The 8 tables: `so_header`, `so_fulfillment`, `bom_header`, `production_plan_line`, `production_order`, `production_indent`, `purchase_indent`, `job_card`. | Master-data team. Unify; add FKs to 8 tables. |
| Single vendor master | `vendor_master` is canonical (FK target of `po_header.supplier_id`, `ncr_record.supplier_id`). Coexists with `vendors` and a satellite of vendor_* tables (banking, contract, document, evaluation, evaluation_criterion 4 rows, evaluation_score, rating_band 5 rows, selection, selection_band 5 rows, selection_parameter 9 rows, selection_score, coa 2 rows). | **LOW** — Vendor master surface is busy but unified. Just confirm `vendors` is dead. | Master-data team. |
| Multi-entity tenancy | `po_header.entity` is NOT NULL with CHECK constraint. The schema map design has no entity concept. | **MEDIUM** — All new tables likely need `entity` column with the same CHECK and a partial index per entity. | Master-data team. |

### §10.3 Module 3 (inferred — Vendor Management)

| Assumption | Current state | Conflict / blocker |
|---|---|---|
| Vendor evaluation flow | Live: `vendor_evaluation`, `vendor_evaluation_criterion` (4 rows), `vendor_evaluation_score`, `vendor_rating_band` (5 rows), `vendor_selection`, `vendor_selection_band` (5 rows), `vendor_selection_parameter` (9 rows), `vendor_selection_score`, `vendor_coa` (2 rows) | **LOW** — Schemas already in place with seeded reference data. Module 3 should reuse, not redefine. |
| Vendor doc storage | `vendor_document` table exists; storage path unclear (no S3 code, no upload code) | **HIGH** — Doc storage backend (S3 vs local) needs a decision before Module 3 can ship doc upload. |

### §10.4 Module 4 — Material Receipt (the schema-map module)

This is where the single most important matrix lives — see §3.2 for full per-table column-level deltas. Summary:

| Concern | Current state | Conflict / blocker | Owner |
|---|---|---|---|
| All 15 designed tables exist as DDL | All `EXISTS_DIFFERENT_SCHEMA` (additional cols, type drift, jsonb URL arrays) | **NONE — DDL is ahead of design.** Design map is stale relative to live DDL. | Material Receipt team. Update the schema map HTML to match live DDL; freeze a v2. |
| Audit triggers wired | All 15 tables have `audit_capture()` triggers | **POSITIVE** — coexists with Module 10. | Material Receipt + Audit team coordination. |
| Tables empty (0 rows) | All 15 except `qc_parameter_master` (11 seeded rows) | **NONE** — clean cut-over possible. | Material Receipt team. |
| Legacy purchase chain coexists | `purchase_orders`, `po_items`, `po_item_boxes` (4.4 MB indexes / 0 rows), `purchase_indent` (**26 rows live**), `purchase_approvals*` | **MEDIUM** — `purchase_indent` has 26 rows. New design must either model indents or migrate the 26 rows to a different table. | Material Receipt + Production team. |
| Legacy QC chain coexists | `quality_inspection`, `qc_holds`, `qc_floors`, `qc_factories` | **LOW** — All 0 rows. Drop after confirming no readers. | QC team. |
| File storage | `coa_document.s3_urls`, `qc_inspection_parameter.s3_urls`, `ncr_supplier_action.s3_urls` (jsonb arrays) ready in DDL; **no Python upload path; no S3 client; `STORAGE_BACKEND=local`; `so_pdfs/` doesn't exist; `candor-docs-prod` not referenced anywhere** | **HIGH** — Module 4 cannot ship dock/COA/NCR-evidence flows without an actual storage backend. | Material Receipt team + Infra. |
| FK indexes | 89 unindexed FK columns include: `grn.transaction_no`, `notification_log.transaction_no`, `ncr_parameter_detail.{ncr_no, param_id, reading_id}`, `ncr_record.{transaction_no, line_number}` (composite FK only), `ncr_supplier_action.ncr_no`, `po_box.line_number`, `po_line.{current_ncr_id, sku_id}`, `qc_inspection.ncr_id`, `stage2_approval.{transaction_no, line_number}` | **MEDIUM** — Index pass needed before any meaningful row volume; currently fine because tables are empty. | Material Receipt team. |
| `customer_master` FK coverage | po_line is the only table with a typed FK; the 8 free-text-customer tables include `purchase_indent` (live 26 rows) and `production_indent` (in scope) | **MEDIUM** — Backfill / FK tightening required. | Master-data + Material Receipt. |

### §10.5 Module 5 (inferred — Production Planning / Job Cards)

| Concern | Current state | Conflict / blocker |
|---|---|---|
| `production_plan`, `production_plan_line`, `production_order`, `job_card` family | Live, actively used by `app/modules/production/router.py` (~120 routes) and `mcp_server.py` (17 tools). Schema split across `app/db/production_schema.sql`, `production_migrate.sql`, `001_job_card_chain.sql`. Job-card child tables: `job_card_environment`, `job_card_metal_detection`, `job_card_weight_check`, `job_card_loss_reconciliation`, `job_card_remarks`, `job_card_output`, `job_card_byproduct`, `job_card_balance_material`, `job_card_rm_indent`, `job_card_pm_indent`, `job_card_process_step`, `job_card_sign_off`. | **LOW if module accepts current state, HIGH if module redesigns.** This is the most active surface in the codebase. |
| `customer_name` free text on production_plan_line, production_order, production_indent, job_card | See Module 2 callout above. | **MEDIUM** — FK backfill required. |
| MCP duplication | `mcp_planner.save_production_plan` vs `mcp_server.save_production_plan` are independent implementations | **MEDIUM** — Module 5 should pick one or extract shared service. |

### §10.6 Module 6 (inferred — QC standalone)

| Concern | Current state | Conflict / blocker |
|---|---|---|
| New `qc_inspection*` family | Live (designed and DDL deployed); 11 seeded `qc_parameter_master` rows | **LOW** — DDL is in place, code path not yet exercised. |
| Legacy `quality_inspection`, `qc_holds`, `qc_floors`, `qc_factories`, `qc_users`, `qc_approvers`, `qc_module_permissions` (162 rows), `qc_user_permissions` (6 rows) | All present; 162 rows in qc_module_permissions are real production data | **MEDIUM** — Decide cutover. The 162 perm rows are what protect QC actions today; they need to migrate to `auth_role_permission` in coordination with Module 1. |

### §10.7 Module 7 (inferred — Reports)

| Concern | Current state | Conflict / blocker |
|---|---|---|
| Reporting layer | `scripts/generate_inventory_report.py`, `scripts/generate_db_diagrams_pdf.py`, `mcp_tracker.py` (25 read-only tools) | **LOW** — Read-only surface already exists. Module 7 likely consolidates / formalises. |
| Materialised views | `v_mfg_*` views exist (≥4 observed in column dump) | **LOW** — Existing views may need to be referenced or replaced. |

### §10.8 Module 8 (inferred — Notifications)

| Concern | Current state | Conflict / blocker |
|---|---|---|
| `notification_log` table | Live (DDL, 0 rows). CHECKs on recipient_role and channel | **HIGH** — Channel column exists but **no email/SMS/WhatsApp client in code** (see §8.1). Module 8 needs to introduce a sender. |
| Missing PK index on `transaction_no` (FK to po_header) | unindexed | Add idx_notification_log_transaction_no. |
| Webhook bus already in place | Yes (the April 2026 work) | **POSITIVE** — Module 8 likely sits on top of webhook events; no conflict. |

### §10.9 Module 9 (inferred — External Integrations: couriers / ERP)

| Concern | Current state | Conflict / blocker |
|---|---|---|
| Courier APIs (Safexpress / DTDC / Rivigo / VRL) | **NOT IMPLEMENTED** anywhere. Audit prompt's claim of "already wired" is not borne out by code. | **HIGH** — Module 9 starts from zero (or from another repo not visible to this audit). |
| ERP push/pull | **NOT IMPLEMENTED**. | **HIGH** — Same. |
| Biometric / VMS / CCP X-Ray | **NOT IMPLEMENTED**. | **HIGH** — Same. |
| Inbound webhooks | **NONE**. Only `/internal/events` for MCP. | **MEDIUM** — Module 9 must introduce inbound endpoints with HMAC/IP-allowlist auth. |

### §10.10 Module 10 — Audit

| Concern | Current state | Conflict / blocker |
|---|---|---|
| `log_edit` is the legacy SO audit table | Live (12 cols, 7 indexes, **0 rows**, no Python writers/readers) | **NONE** — empty, parked. Drop after Module 10 ships. |
| New `audit_log` + `audit_policy` (30 rows) + `commit_log` + `audit_capture()` triggers | Already wired on receipt-flow tables (135 triggers in DB) | **HIGH** — Module 10 design must be aware that DB-trigger-driven audit is already partially in place. Either treat triggers as canonical and build Python observability around them, or redesign and remove triggers. |
| Correlation IDs | None implemented | **MEDIUM** — Module 10 should add `X-Request-ID` middleware + propagate to events + audit rows. |
| Application logging style | Plain text to stdout, no structured JSON | **LOW** — Module 10 may want to switch to structured logging (structlog/loguru). |

### §10.11 Module 11 (inferred — possibly Workflows / Orchestration / Mobile-app sync)

Without the module prompt, the most likely Module 11 candidates from codebase signals are: **workflow/state-machine** (job-card lifecycle is implicit in code, not declarative); **mobile sync** (Android directory under `D:\Consumption\Android` exists as additional working directory); or **reconciliation/day-end** (`day_end.py`, `discrepancy_manager.py` already exist).

| Concern | Current state | Conflict / blocker |
|---|---|---|
| Day-end and discrepancy already implemented | `day_end.py`, `discrepancy_manager.py`, `day_end_balance_scan` table, MCP tools | **LOW** — Code exists. |
| Workflow state declared explicitly | No state-machine library; states encoded as text+CHECK constraints (e.g. `po_line_status_chk` 12 values) | **LOW** — Acceptable but informal. |
| Mobile (Android) sync | Listed in working directories but not in audit scope | **`[VERIFY]`** — get explicit Module 11 scope. |

---

## §11 Performance baseline

Captured read-only against the live AWS RDS warehouse_db. Numbers are baseline only — new modules must not regress them.

### §11.1 Top-time queries

**`pg_stat_statements` extension is NOT installed.** Top-time profile unavailable. Recommend `CREATE EXTENSION pg_stat_statements;` (RDS parameter group change required) before next audit cycle.

### §11.2 Largest tables

See §3.5 (top 20 by total relation size). Largest is `cdpl_cold_stocks` at 25 MB / 60,657 rows.

### §11.3 Tables without primary key

| Table | Size | Rows |
|---|---|---|
| `categorial_invoicewise_inv` | 6.7 MB heap, 0 indexes | 0 |

### §11.4 FK columns without supporting indexes

**89 total** (full list in `_audit_db_dump.txt:11226-11314`). Receipt-flow hits include:
- `grn.transaction_no` (→ po_header)
- `notification_log.transaction_no` (→ po_header)
- `ncr_parameter_detail.{ncr_no, param_id, reading_id}`
- `ncr_record.{transaction_no, line_number}` (composite FK, no supporting index)
- `ncr_supplier_action.ncr_no`
- `po_box.line_number` (transaction_no has one; line_number doesn't)
- `po_line.{current_ncr_id, sku_id}`
- `qc_inspection.ncr_id`
- `stage2_approval.{transaction_no, line_number}`

These are not painful at zero rows — they will be at any meaningful volume.

### §11.5 Connection pool

| Component | Pool config |
|---|---|
| FastAPI backend (`app/db/connection.py:7`) | min_size=0, max_size=10, no SSL, no keepalive |
| `mcp_server.py` | min_size=0, max_size=3 |
| `mcp_planner.py` | min_size=0, max_size=3 |
| `mcp_tracker.py` | min_size=1, max_size=2 |
| `mcp_viewer_server.py` | reuses `mcp_server`'s pool |
| **Combined upper bound (3 separate Render dynos)** | **~18 simultaneous DB connections** |

No PgBouncer, no RDS Proxy. RDS instance class and `max_connections` setting `[VERIFY-RUNTIME]` from AWS Console.

### §11.6 p95 / p99 latency

`[VERIFY-RUNTIME]` — not derivable from this audit. Render exposes per-service metrics; AWS RDS Performance Insights would surface DB-side latency. Neither is recorded in code.

---

## §12 Production data sensitivity

### §12.1 PII surface

| PII type | Tables/columns observed |
|---|---|
| Names | `auth_user.full_name`, `vendor_master.*name*`, `customer_master.*name*`, ~50+ `*_name` text columns across the schema |
| Emails | `auth_user.email`, vendor/customer email columns (text) |
| Phones | `auth_user.phone (UNIQUE)` |
| Passwords | `auth_user.password_encrypted` — **Fernet symmetric, decryptable with `AUTH_ENCRYPTION_KEY`** |
| GSTIN | `vendor_master`, `customer_master` (text columns observed) |
| Bank | `vendor_banking` table |
| PAN | `[VERIFY-RUNTIME]` — likely on `vendor_master`, but column-level confirmation needs grep through `_audit_db_dump.txt` |

### §12.2 Encryption

- **At rest:** AWS RDS default — `[VERIFY-RUNTIME]`. AWS RDS supports KMS-backed encryption at rest; whether enabled on `wms-postgres-db` requires AWS Console check.
- **In transit:** asyncpg pool created **without explicit `ssl=` parameter**. asyncpg defaults to no SSL unless `?sslmode=require` is in the URL. **The DATABASE_URL in `.env` does not specify sslmode.** RDS Postgres typically requires SSL by default at the parameter-group level, but this should be verified.

### §12.3 Plaintext secrets

- `.env` contains live RDS credentials, ANTHROPIC_API_KEY, AUTH_ENCRYPTION_KEY (Fernet symmetric key — knows it, decrypts every password), INTERNAL_WEBHOOK_TOKEN, WS_TOKEN_SECRET, MAIN_SERVER_URL — all in plaintext.
- `.env` is correctly gitignored (`.gitignore:13`) — not in repo history.
- Render service config uses `sync: false` for `AUTH_ENCRYPTION_KEY` (`render.yaml:18`) — the Render dashboard holds the prod value, not the repo.
- **Risk:** if any developer's local `.env` is exfiltrated, the attacker can decrypt all stored user passwords (Fernet is symmetric).

### §12.4 Backup strategy

`[VERIFY-RUNTIME]` — AWS RDS automated snapshot policy is set at the instance level; not visible from code. Default RDS backup retention is 7 days unless changed.

---

## §13 Existing test infrastructure

| Concern | Configured? | Tooling | Evidence |
|---|---|---|---|
| Test framework | **Partial** — pytest available in worktree only | pytest | `.worktrees/feature/lambda-deploy/pyproject.toml`. Main branch `pyproject.toml` has NO test deps. |
| Test files | 3 in worktree (lambda-deploy), 1 standalone at root (`test_plan_generation.py`, gitignored) | pytest | `tests/test_handler.py`, `tests/test_config.py`, `tests/test_migrate.py` in worktree |
| Test database | **NONE** — tests import `app.config.Settings` which loads `.env` (live RDS) | n/a | No testcontainers / pytest-postgresql / sqlite fixtures |
| CI pipeline | **NO** — no `.github/workflows/`, no GitLab/Jenkins/Azure config | n/a | Verified by Glob |
| Coverage | NO | n/a | No `.coveragerc`, no badge |
| Linters / formatters | NO | n/a | No ruff/black/flake8/mypy/isort/pre-commit-config |
| Local dev settings | Minimal | VSCode | `.vscode/settings.json` (~66 bytes) |
| `.claude/settings.json` | Minimal allowlist | Claude Code | Allows `where python`, `py --list` |
| `.claude/settings.local.json` | Larger allowlist (~40 entries) | Claude Code | find/grep/pytest/pip allowlists |

**Delta from new-module assumption ("pytest + testcontainers"):**
- Add `pytest`, `pytest-asyncio` to `requirements.txt` / `pyproject.toml [tool.pytest]`.
- Add `testcontainers-postgresql` (or commit to a different test-DB strategy).
- Create `conftest.py` with async DB fixtures.
- Set up CI (GitHub Actions) before relying on tests for safety.
- Tests must NOT load production `.env` — add a test-only `.env.test` and pin `ENVIRONMENT=test`.

---

## §14 Deployment topology

### §14.1 Live stack

```
                                 Internet
                                    │
                ┌───────────────────┼───────────────────┐
                │                   │                   │
                ▼                   ▼                   ▼
   desktop-backend (Render)   candor-planner-mcp    candor-tracker-mcp
   uvicorn app.main:app       python mcp_planner.py python mcp_tracker.py
   port: $PORT                (own dyno)            (own dyno)
   asyncpg pool min=0 max=10  asyncpg pool 0..3    asyncpg pool 1..2
                  │                   │                   │
                  └───────────────────┼───────────────────┘
                                      │
                                      ▼
                       Render Postgres `consumption-db`
                                  (free tier)
                                  
                                      ─OR (per .env)─
                                      
                       AWS RDS Postgres `warehouse_db`
                       wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432
                       (the actual DB with 384 tables and seed data)
```

### §14.2 Service table

| Service | Runtime | Entrypoint | DB | State |
|---|---|---|---|---|
| desktop-backend | Python 3.12 (Render) | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` | (per env) | LIVE |
| candor-planner-mcp | Python 3.12 (Render) | `python mcp_planner.py` | (per env) | LIVE |
| candor-tracker-mcp | Python 3.12 (Render) | `python mcp_tracker.py` | (per env) | LIVE |
| AWS Lambda (planned) | `public.ecr.aws/lambda/python:3.12` | `app.main.handler` (Mangum) | — | STANDBY (not deployed) |

### §14.3 Drift / contradictions

1. **DB identity drift:** `.env` → AWS RDS `warehouse_db`; `render.yaml` → Render `consumption-db`. The two databases have different schemas and different data. This audit found 384 tables in RDS; the Render DB is unprobed. **Decide which is canonical before any new module ships.**
2. **Render vs Lambda:** `Dockerfile` and `mangum` handler are present and the lambda-deploy worktree has migration scripts, but Lambda is not deployed. `MAIN_SERVER_URL=https://desktop-backend-vhf0.onrender.com` confirms Render is live. Either commit to the Lambda migration or remove the Dockerfile/Mangum/boto3 from the main branch.
3. **Python version:** `pyproject.toml` declares `requires-python = ">=3.14"` (an unreleased version as of this audit's cutoff). `render.yaml` pins Python 3.12. Pyproject is wrong / aspirational.
4. **Single environment:** No staging Render service. No `ENV`/`ENVIRONMENT`/`STAGE` settings. Every change goes straight to prod.
5. **CORS:** `allow_origins=["*"]` — wide open.

### §14.4 Secrets handling

| Path | Mechanism | Risk |
|---|---|---|
| Local dev | `.env` plaintext (gitignored). Live RDS creds. | If the file leaks, attacker has prod DB + can decrypt every user password (Fernet symmetric). |
| Render prod | Render dashboard env vars; `render.yaml` declares `AUTH_ENCRYPTION_KEY` with `sync: false` | Standard Render practice. Acceptable. |
| AWS Lambda | Not deployed — Lambda secrets path not yet defined | When/if deployed, decide between Lambda env vars vs AWS Secrets Manager. |

---

## §15 Top-10 blockers ranked by severity

> Each blocker is actionable in a sprint. The "Owner" column is a suggested team based on inference; the "Resolution path" is the next concrete step.

| # | Severity | Blocker | Owner | Resolution path | Sprint estimate |
|---|---|---|---|---|---|
| 1 | **CRITICAL** | **Database-of-record ambiguity.** `.env` points to AWS RDS `warehouse_db` (where all 384 tables and seed data live). `render.yaml` points to Render `consumption-db` (separately provisioned, not probed). Production may currently be writing to one and reading from the other depending on which dyno picked up which env var. | Infra + Backend lead | (a) Confirm which DSN actually serves `desktop-backend-vhf0.onrender.com` requests today (Render dashboard env var inspection). (b) Decide canonical DB. (c) Sync the loser to the winner if rows have diverged, otherwise drop the loser. (d) Make `.env.example` and `render.yaml` agree. | 1 sprint |
| 2 | **CRITICAL** | **`/api/v1/so/*`, `/api/v1/purchase/*`, and most `/api/v1/production/*` routes have NO authentication.** Anyone on the internet can call upload, fulfillment, plan, indent, job-card endpoints today. | Auth team (Module 1 scope) | (a) Add a global auth dependency that all routers inherit by default, with `Depends(get_current_user)` baseline. (b) Whitelist explicit public routes (login, health). (c) Replay logs to detect any unauthenticated traffic before locking down. | 1 sprint |
| 3 | **CRITICAL** | **No file-storage backend exists for COA/NCR-evidence/QC-evidence/vendor-doc upload.** DDL is ready (`s3_urls jsonb` columns on coa_document, qc_inspection_parameter, ncr_supplier_action). Code path is missing. `STORAGE_BACKEND=local` but `./so_pdfs/` doesn't exist. `candor-docs-prod` not referenced anywhere. | Material Receipt team + Infra | (a) Confirm `candor-docs-prod` bucket exists in AWS (`aws s3api head-bucket`). (b) Implement S3 client (boto3 already in requirements) wrapped behind a `Storage` interface so local-disk also works for dev. (c) Wire upload endpoints to write `s3_urls` jsonb arrays. (d) Implement signed-URL retrieval. | 1-2 sprints |
| 4 | **HIGH** | **Password storage uses Fernet symmetric encryption, not a hash.** Anyone with `AUTH_ENCRYPTION_KEY` can decrypt all stored user passwords. `.env` carries the key in plaintext for local dev. | Auth team (Module 1) | (a) Choose hash algorithm (Argon2id recommended). (b) Add `password_hash` column. (c) On next login per user, rehash + clear `password_encrypted`. (d) After cutover window, drop `password_encrypted` and `AUTH_ENCRYPTION_KEY`. | 2 sprints (with deprecation window) |
| 5 | **HIGH** | **External integrations claimed to be "already wired" do not exist in code.** No email, no WhatsApp/SMS, no ERP, no courier (Safexpress/DTDC/Rivigo/VRL), no biometric/VMS/CCP X-Ray. Module 8 (Notifications) and Module 9 (Integrations) start from zero. | Integration team (Module 9) + Notifications (Module 8) | (a) Audit-source claim with whoever wrote it — confirm the integrations live in another repo or were aspirational. (b) If new build, design provider abstraction first; (c) Pick channels by priority. | 2-3 sprints to first working channel |
| 6 | **HIGH** | **Schema map is for Module 4 only (15 tables), not the 33 modules-1-to-11 design assumed by the prompt.** Per-module conflict mapping cannot be validated without the actual per-module schema specs. | All module owners | (a) Locate or commission per-module schema specs. (b) Re-run §3 diff against full design. (c) Update §10 conflict matrix. | 1 sprint to gather, then ongoing |
| 7 | **HIGH** | **8 in-scope tables carry free-text `customer_name` columns instead of FKs to `customer_master`.** Tables: so_header, so_fulfillment, bom_header, production_plan_line, production_order, production_indent, purchase_indent, job_card. `purchase_indent` already has 26 live rows. | Master-data team (Module 2) | (a) Confirm `customer_master` is canonical (vs `mst_customer`). (b) Add `customer_id text` columns to all 8 tables. (c) Backfill from name with fuzzy match against the 26 live `purchase_indent` rows. (d) Add FK constraints once 100 % of rows are matched. | 1 sprint |
| 8 | **MEDIUM** | **89 unindexed FK columns** including most receipt-flow FKs (grn.transaction_no, ncr_parameter_detail.{ncr_no, param_id, reading_id}, ncr_record.{transaction_no, line_number}, ncr_supplier_action.ncr_no, po_box.line_number, qc_inspection.ncr_id, stage2_approval.{transaction_no, line_number}, notification_log.transaction_no). Currently fine because tables are empty. | Material Receipt team | Run a single migration that creates the missing indexes before Module 4 lights up real traffic. | 1 day |
| 9 | **MEDIUM** | **No tests, no CI, no linters in the main branch.** `pytest` is only in the lambda-deploy worktree. Tests there hit live RDS via `app.config.Settings()`. | All teams | (a) Add `pytest`, `pytest-asyncio`, `testcontainers-postgresql` to deps. (b) Add `conftest.py` with isolated test DB. (c) Add minimal GitHub Actions for lint + test on PR. (d) Forbid tests from loading prod `.env`. | 1 sprint |
| 10 | **MEDIUM** | **MCP server logic duplicated across 2-3 servers.** ~16 tool functions reimplemented independently across mcp_server / mcp_planner / mcp_tracker. Risk of divergence on any bug fix. | Production / planning team | Extract a shared async service module (e.g. `app.modules.production.services.mcp_shared`) and have all three MCP servers import from it. | 1-2 sprints |

**Honourable mentions (not in top-10 but worth tracking):**

- Render-vs-Lambda deployment drift: `Dockerfile` + `mangum` + `boto3` carried in main branch but not deployed.
- `pyproject.toml` declares `requires-python = ">=3.14"` (unreleased).
- CORS `allow_origins=["*"]`.
- `pg_stat_statements` extension not installed — top-time profile unavailable.
- 1 table without PK (`categorial_invoicewise_inv`) and 314 sequences (many likely orphaned) — schema-cleanliness debt.
- Two parallel audit systems (`log_edit` legacy, `audit_log`+`audit_capture()` new) need an explicit cutover decision.
- Two parallel customer masters (`customer_master`, `mst_customer`).
- Two parallel user/role/perm chains (`auth_*`, `iam_*`).
- Two parallel SKU masters (`all_sku` vs `cfplsku`/`cdplsku`/`sku`/`cfplitems`/`cdplitems`/`mfg_items`).

---

## Appendix A: artifacts written by this audit

These files were created in `D:\Consumption\New\Backend\` by the audit and are NOT part of the application:

| File | Purpose | Safe to delete? |
|---|---|---|
| `_audit_db_probe.py` | Read-only psql-via-asyncpg probe script (21 queries) | Yes — keep if you want to re-run the inventory |
| `_audit_db_probe2.py` | Customer-FK / po_line-status / supplier-FK probes | Yes |
| `_audit_db_dump.txt` (937 KB) | Raw output of all probe queries — full table list, column listing, FK report, index report, trigger report, etc. | Yes — keep as evidence backing the §3 diff and §11 baseline |
| `EXISTING_CODEBASE_AUDIT.md` (this file) | The deliverable | Keep |

The audit performed no schema mutations, no row writes, and no destructive operations of any kind.

---

**End of audit.**
