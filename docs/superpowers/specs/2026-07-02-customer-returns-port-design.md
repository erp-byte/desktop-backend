# Customer-Returns Module — Port Design

**Date:** 2026-07-02
**Status:** Draft for review
**Target app:** `linux_replica/server_replica` (FastAPI + asyncpg)
**Source app:** `backend/services/ims_service/rtv_*.py` (FastAPI + SQLAlchemy)

---

## 1. Overview & Goal

Port the source **customer-returns** module (filed under "RTV" in the source, but actually a
customer-return document system that generates `CR-` ids) into the target app, faithfully
reproducing its **behavior** (the 49 functions inventoried in the source) while rewriting it
to the target app's conventions: async `asyncpg`, JWT-authenticated endpoints, hand-numbered
idempotent SQL migrations, and the target's magic-link email idiom.

This is a **behavioral port, not a file copy** — the source is sync SQLAlchemy with unauthenticated
endpoints; the target is async asyncpg with per-endpoint JWT auth. No customer-returns code exists
in the target today.

### Terminology hazard (must respect)

- The target app **already** uses "RTV" and the `/rtv/*` routes for **return-to-vendor**
  (`rtv_disposition_service.py`, `/rtv/dispositions`, `/rtv/discard`) — disposing of *rejected
  material*. That is a **different concept**.
- The source module is **customer returns** (goods coming back **from customers**), and its own id
  generator deliberately emits `CR-...` (not `RTV-...`) to avoid being misread as return-to-vendor.
- **Therefore:** this port is named **`customer_returns`**, mounts at **`/api/v1/customer-returns`**,
  and keeps the **`CR-`** id prefix. It does **not** touch `rtv_disposition_service.py`.

---

## 2. Locked Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Route/module naming | **`/api/v1/customer-returns`**, module `app/modules/customer_returns/`, keep `CR-` ids |
| 2 | Company partitioning | **Keep `cfpl_`/`cdpl_` table-name prefixes**; physical tables named `{prefix}_customer_return_*` |
| 2b | Keys / PK model | **No sequential `id`.** Header PK = `rtv_id` (`CR-...`); lines/boxes link by `rtv_id`; line PK `(rtv_id, item_description)`, box PK `(rtv_id, article_description, box_number)`; `{cr_id}` path param is the `CR-` string |
| 3 | Auth | **JWT-gated**; actor identity (`created_by`/`approved_by`/`deleted_by`/`actioned_by`) comes from the token, never from request params |
| 4 | Email approval | **Magic-link Accept/Reject/Hold buttons**, upgraded to a **signed `cr_action` token** (no IMAP) |
| 5 | WhatsApp-on-create | **In scope** (reuse target's `whatsapp_service` idiom; no-ops until Meta templates exist) |
| 6 | Realtime webhook/WS events | **In scope** (net-new `customer_returns.*` events + broadcaster role mapping) |
| 7 | Cold-stock mirror | **In scope** (own phase; writes into existing `cfpl_/cdpl_cold_stocks`) |

---

## 3. Module Structure

Mirrors the target's `transfer`/`packing` module layout (thin router, fat services, flat `schemas.py`).

```
app/modules/customer_returns/
  __init__.py             # docstring only
  router.py               # APIRouter(prefix="/api/v1/customer-returns", tags=["Customer Returns"])
  schemas.py              # flat Pydantic models (see §6)
  tables.py               # cr_table_names(company) whitelist helper -> {"header","lines","boxes"}
  action_token.py         # signed cr_action token: mint/verify (reuses app JWT secret) (see §10)
  services/
    __init__.py           # docstring only
    query_service.py      # list_crs, get_cr, export dataset + _map_* row mappers
    create_service.py     # create_cr, update_cr, update_cr_lines, delete_cr
    box_service.py        # upsert_box (print), bulk_save_boxes, box summary/short-weight, log_box_edits
    save_service.py       # save_cr (consolidated header+lines+boxes with change diffing)
    approval_service.py   # approve_cr, set_status, apply_email_action
    cold_sync_service.py  # sync_cold_stocks_from_cr
    notify_service.py     # email (magic-link), WhatsApp, realtime events — all best-effort
  FRONTEND_API_DOC.md     # ported/updated from source RTV_README.md
```

Registered in `app/main.py`:
```python
from app.modules.customer_returns.router import router as customer_returns_router
...
app.include_router(customer_returns_router)   # near the other include_router calls
```

DB migration: `app/db/070_customer_returns.sql`, appended to the ordered `SQL_FILES` list in
`scripts/migrate.py` (the runner is an explicit list, **not** a glob — a file not added there never runs).

---

## 4. Data Model & Migration (`070_customer_returns.sql`)

**Convention chosen:** per-company prefixed tables (`cfpl_`/`cdpl_`), matching the source and the
target's existing prefixed inventory tables. Physical table names (per decision, OQ-1 resolved):
**`{prefix}_customer_return_header`**, **`{prefix}_customer_return_lines`**,
**`{prefix}_customer_return_boxes`** (×2 companies, `{prefix}` ∈ `cfpl`/`cdpl`), plus one **global**
`box_edit_logs`. The `*_customer_return_*` names avoid any collision with pre-existing `cfpl_rtv_*`
tables in a shared DB.

### Key model (changed): natural keys, no sequential `id`

- **No sequential `id` column on any of the three tables.** The header's **primary key is `rtv_id`**
  (the `CR-YYYYMMDDHHMMSS` string). Lines and boxes depend on the header through `rtv_id`.
- **URL consequence:** the `{cr_id}` path param now carries the **`CR-...` string**, not an integer
  (this removes the source's confusing "int `id` masquerading as `rtv_id`" duality).
- **Line identity:** composite PK **`(rtv_id, item_description)`** — one line per article per return.
  (Slight tightening vs source, which allowed duplicate `item_description` and matched `LIMIT 1`;
  now the box↔line match is deterministic. See §16 note.)
- **Box identity:** composite PK **`(rtv_id, article_description, box_number)`**. The stored integer
  `rtv_line_id` is **dropped** — a box links to its line logically by `article_description =
  item_description` within the same `rtv_id` (resolved at query/mirror time).

**Idempotency:** the migration is written idempotently — `CREATE TABLE IF NOT EXISTS` for the base
shape plus `ADD COLUMN IF NOT EXISTS` (in a `DO $$` block) for later-added logistics/cold/sales-poc
columns — so it is safe to re-run. Because the `*_customer_return_*` names are new, there is no
pre-existing table to reconcile against (unlike the old `cfpl_rtv_*` names).

### 4.1 `{prefix}_customer_return_header` (cfpl & cdpl)

| column | type | notes |
|---|---|---|
| **rtv_id** | **TEXT PRIMARY KEY** | business id string; new = `CR-YYYYMMDDHHMMSS` (IST); legacy `RTV-...` accepted on read |
| rtv_date | TIMESTAMPTZ | NOW() at creation |
| factory_unit | TEXT NOT NULL | stored via canonical warehouse fold |
| customer | TEXT NOT NULL | |
| invoice_number, challan_no, dn_no | TEXT | nullable |
| conversion | DOUBLE PRECISION | nullable; FE sends str, stored float (0 if empty) |
| sales_poc, sales_poc_email | TEXT | nullable (sales_poc_email added via source migration) |
| business_head | TEXT | nullable |
| remark | TEXT | nullable |
| vehicle_number, transporter_name, driver_name, inward_manager | TEXT | nullable (logistics migration) |
| status | TEXT NOT NULL DEFAULT 'Pending' | values: `Pending` / `Submitted` / `Approved` / `Rejected` / `On Hold` (`Submitted` set by bulk box save, §7.3) |
| created_by | TEXT | JWT user.email (was a spoofable query param in source) |
| created_ts | TIMESTAMPTZ | NOW() at creation |
| updated_at | TIMESTAMPTZ | NOW() on update |

Index: `idx_{prefix}_cr_header_status` on `status`. (`rtv_id` is already the PK, so it is indexed.)

### 4.2 `{prefix}_customer_return_lines` (cfpl & cdpl)

| column | type | notes |
|---|---|---|
| **rtv_id** | **TEXT NOT NULL** | FK → `{prefix}_customer_return_header(rtv_id)` ON DELETE CASCADE |
| **item_description** | **TEXT NOT NULL** | box↔line match key; **PK = `(rtv_id, item_description)`** |
| material_type, item_category, sub_category, uom | TEXT NOT NULL | (uom auto-uppercased) |
| qty | INT DEFAULT 0 | |
| rate, value, net_weight, carton_weight | DOUBLE PRECISION DEFAULT 0 | `value` = qty*rate when not supplied |
| lot_number, item_mark, spl_remarks, vakkal | TEXT | nullable (cold-article migration) |
| created_at, updated_at | TIMESTAMPTZ | |

PK: `(rtv_id, item_description)`. Index: `idx_{prefix}_cr_lines_rtv` on `rtv_id`.

### 4.3 `{prefix}_customer_return_boxes` (cfpl & cdpl)

| column | type | notes |
|---|---|---|
| **rtv_id** | **TEXT NOT NULL** | FK → `{prefix}_customer_return_header(rtv_id)` ON DELETE CASCADE |
| **article_description** | **TEXT NOT NULL** | matches a line's `item_description`; part of PK |
| **box_number** | **INT NOT NULL** | ≥1; **PK = `(rtv_id, article_description, box_number)`** |
| box_id | TEXT | **NULL until Print**; never regenerated once set (formats in §7.3) |
| uom | TEXT | nullable |
| conversion | TEXT/NUMERIC | round-tripped as string |
| lot_number, item_mark, spl_remarks, vakkal | TEXT | nullable |
| net_weight, gross_weight | NUMERIC(18,3) DEFAULT 0 | |
| count | INT | nullable (per-box label piece-count, **not** a carton multiplier) |
| created_at, updated_at | TIMESTAMPTZ | |

PK: `(rtv_id, article_description, box_number)`. Index: `idx_{prefix}_cr_boxes_rtv` on `rtv_id`.
No stored `rtv_line_id` — the line is resolved logically (§7.3). A composite FK
`(rtv_id, article_description) → {prefix}_customer_return_lines(rtv_id, item_description)` is
**optional** and intentionally **omitted**, because a box may be entered before its line exists and the
source tolerates an unmatched box (link resolves to NULL); enforcing it would reject such boxes.

### 4.4 `box_edit_logs` (global, shared)

Flat audit shape (matches source; **not** the target's JSONB audit style). Shared with the source's
inward module — keep column names exact. Per the "remove id from all tables" directive this is an
**append-only log with no surrogate PK** (rows link by the `transaction_no`/`box_id` strings).

| column | type | notes |
|---|---|---|
| email_id | TEXT | editor (from JWT `user.email`) |
| description | TEXT | auto: `Changed {field} from '{old}' to '{new}'` |
| transaction_no | TEXT | the `rtv_id` string |
| box_id | TEXT | printed box id |
| field_name | TEXT | |
| old_value, new_value | TEXT | |
| edited_at | TIMESTAMPTZ | UTC |

Index: `idx_box_edit_logs_box` on `(box_id, field_name)` — used by the Excel edited-cell highlight.
(If you'd prefer a surrogate PK here after all, say so — it's the one table outside the CR dependency chain.)

### 4.5 `cold_stocks` — **NOT created here**

`cfpl_cold_stocks` / `cdpl_cold_stocks` are **externally managed** (no DDL in the target repo). The
mirror (§12) only `INSERT`s/`DELETE`s into them, guarded by `to_regclass` so it no-ops where absent
(dev/test). Their canonical columns + `BEFORE INSERT` trigger are owned by the source cold-storage
migration.

---

## 5. DB Access Conventions (applies to every service)

- All service functions: `async def fn(conn, ...)`, `conn` as first positional arg. **No** SQLAlchemy.
- Placeholders: positional `$1, $2, …`; dynamic WHERE builds a `clauses`/`args` list interpolating only
  the `$N` index, never the value.
- `conn.fetch` (many) / `conn.fetchrow` (one|None) / `conn.fetchval` (scalar) / `conn.execute` (writes).
- Rows → `dict(record)` → hand-written `_map_*` mappers using `.get()`.
- Transactions: `async with conn.transaction():` in the service; **no explicit commit**. Post-write
  read-back happens **after** the transaction so best-effort enrichment can't poison the write.
- Router pattern: `pool = request.app.state.db_pool; async with pool.acquire() as conn: return await service.fn(conn, ...)`.
- Errors: `HTTPException(status, detail={"error": <code>, "message": <human>, "details": {...}})`
  (per `MEMORY: server-error-envelope-convention` — `error`/`message` keys, never `code`).
- asyncpg type strictness: numeric → `Decimal`, dates → `date`/`datetime`, booleans → real `bool`.

---

## 6. Schemas (`schemas.py`)

Port every Pydantic model from source `rtv_models.py` (names may keep the `RTV`-prefix internally or
be renamed to `CR`-prefix — see OQ-2; default: rename request/response classes to `CR*`, keep field
names identical for FE compatibility). Field lists are reproduced verbatim from the source contract:

- **Requests:** `CRHeaderCreate`, `CRLineCreate` (with `material_type`/`uom` uppercase validator),
  `CRCreate` (`company`, `header`, `lines` min_length=1), `CRHeaderUpdate` (all optional),
  `CRBoxUpsertRequest`, `CRLinesUpdateRequest`, `CRApprovalHeaderFields`, `CRApprovalLineFields`
  (`item_description` match key), `CRApprovalBoxFields`, `CRApprovalRequest`, `CRBoxEditLogEntry`,
  `CRBoxEditLogRequest`, `CRBulkBoxItem`, `CRBulkBoxUpdateRequest`, `CRSaveRequest`, `CRActionRequest`.
- **Responses:** `CRLineResponse`, `CRBoxResponse`, `CRBoxUpsertResponse`, `CRBulkBoxUpdateResponse`,
  `CRHeaderResponse`, `CRWithDetails`, `CRListItem` (+ `items_count`, `boxes_count`, `total_qty`,
  `total_net_weight`), `CRListResponse`, `CRDeleteResponse`, `SendForApprovalResponse`,
  `CRLinesUpdateResponse`, `CRApprovalResponse`, `CRActionResponse`.
- Aliases: `Decimal18_2`, `Decimal18_3`. `Company` becomes a `Literal["CFPL","CDPL"]` local to this module.
- Numeric fields stay **as strings** in responses to match the production API contract exactly.
- **Key-model changes (no sequential id):** response models drop the integer `id`/`header_id`/`rtv_line_id`
  fields. `CRHeaderResponse`/`CRWithDetails`/`CRListItem` are keyed by `rtv_id` (already present).
  `CRLineResponse` carries `rtv_id` (+ its `item_description`) instead of `id`/`header_id`.
  `CRBoxResponse` carries `rtv_id` (+ `article_description`, `box_number`) instead of
  `id`/`header_id`/`rtv_line_id`. `CRWithDetails` still nests `lines[]` and `boxes[]`.

---

## 7. Service Layer — source→target function map

The 28 source `rtv_tools.py` functions map to async services as follows (all `async`, `conn` first).

> **Note:** with the key-model change, service functions take the **`rtv_id` string** (`cr_id`), not an
> integer db id. All header/lines/boxes joins are on `rtv_id`.

### 7.1 `query_service.py`
- `cr_table_names(company)` → in `tables.py`; whitelist `{"CFPL": "cfpl", "CDPL": "cdpl"}` → returns
  `{"header": f"{p}_customer_return_header", "lines": f"{p}_customer_return_lines", "boxes": f"{p}_customer_return_boxes"}`
  (never f-string raw input into a table name).
- `list_crs(conn, *, company, page, per_page, status, factory_unit, customer, from_date, to_date, sort_by, sort_order)`
  — filtered/whitelisted-sort/paginated with correlated subquery aggregates (items_count, boxes_count,
  total_qty, total_net_weight), aggregates joined on `rtv_id`. `sort_by` allow-listed; invalid → `created_ts`. `DD-MM-YYYY` date parse.
- `get_cr(conn, company, cr_id)` — header by `rtv_id` (404 if missing) + `_fetch_lines(rtv_id)` + `_fetch_boxes(rtv_id)`.
- `export_cr_records(conn, company, filters…)` — LEFT JOIN header⋈lines⋈boxes **on `rtv_id`** (boxes→lines
  additionally on `article_description = item_description`) flattened for Excel.
- Row mappers `_map_header_row`, `_map_line_row`, `_map_box_row`, `_fetch_lines`, `_fetch_boxes`,
  `_convert_date`, `_to_float`, `_norm`.

### 7.2 `create_service.py`
- `create_cr(conn, company, data, created_by)` — gen `CR-` id (becomes the PK), insert header (`Pending`, NOW()),
  insert lines (value = qty*rate when absent). Fires best-effort WhatsApp (§11). `created_by = user.email`.
- `update_cr(conn, company, cr_id, data)` — partial header update (400 if empty; SET only non-None; WHERE rtv_id).
- `update_cr_lines(conn, company, cr_id, data)` — delete-all lines for `rtv_id` then insert new set.
- `delete_cr(conn, company, cr_id)` — count lines/boxes, cascade delete boxes→lines→header by `rtv_id`, return meta.

### 7.3 `box_service.py`
- `upsert_box(conn, company, cr_id, payload)` — **Print** action; box keyed by `(rtv_id, article_description, box_number)`.
  Box→line link resolved logically by **exact** (case-sensitive, no trim/lower) `line.item_description ==
  box.article_description` within the same `rtv_id` (now unique, so no `LIMIT 1` needed). box_id gen
  (single-print format): `f"{str(int(time*1000))[-8:]}-{box_number}"`. Existing+has box_id → COALESCE-update
  weights/lot/count, preserve box_id.
- `bulk_save_boxes(conn, company, cr_id, data, notify_discrepancy=True)` — dedupe by
  `(article, box_number)`, insert/update/delete diff, flip status to `Submitted` **only** from
  `Approved`/`Submitted`, mirror cold boxes (§12). Bulk box_id format is **three-part**:
  `f"{base}-{box_number}-{inserted}"`. Preserve both formats exactly.
- `cr_box_summary_and_short(detail)` — per-article box summary + short-weight breakdown.
- `log_box_edits(conn, payload)` — insert `box_edit_logs` rows; auto `description`.

### 7.4 `save_service.py`
- `save_cr(conn, company, cr_id, data)` — one-transaction consolidated header+lines+boxes save with
  header/line/box diffing → returns `(detail, summary)` for a single "Updated" email.

### 7.5 `approval_service.py`
- `approve_cr(conn, company, cr_id, payload, approved_by)` — status→`Approved`, approver+timestamp,
  optional header overrides, partial line update by `item_description`, box upsert (no box_id on
  approval-only inserts), cold mirror.
- `set_status(conn, cr_id, actor_email, action)` — cross-company lookup by `rtv_id`
  (404 none / 409 ambiguous), maps action→status, updates. Used by the programmatic POST endpoint.
- `apply_email_action(conn, cr_id, actor_email, action)` — validated: cross-company lookup by `rtv_id`,
  **business-head ownership** check, terminal-status no-op (`already_actioned`), guarded update
  `WHERE rtv_id=$ AND status IN ('Pending','On Hold')`, concurrent-click race handling.
  `ACTION_TO_STATUS = {approve:Approved, reject:Rejected, hold:On Hold}`. Buttons live while
  `Pending` or `On Hold`.

### 7.6 `cold_sync_service.py`
- `sync_cold_stocks_from_cr(conn, company, cr_id)` — see §12.

### 7.7 `notify_service.py` — see §11.

---

## 8. Router & Endpoints (`router.py`)

15 endpoints, **declaration order preserved** so literal routes (`export`, `box-edit-log`, `action`)
are declared **before** `/{company}` and `/{company}/{cr_id}` (FastAPI matches in order). All take
`user: AuthUser = Depends(get_current_user)`; `company` is a `str` path/query param (mapped to a
prefix via the whitelist helper). The `{cr_id}` path param is the **`rtv_id` string** (`CR-...`), which
is the header PK — there is no integer id.

| # | Method + path (under `/api/v1/customer-returns`) | Service | Identity source |
|---|---|---|---|
| 1 | `GET /export` | `export_cr_records` | — |
| 2 | `POST /box-edit-log` | `log_box_edits` | `user.email` → `email_id` |
| 3 | `GET /email-action` (HTML) | `apply_email_action` | signed token (§10) |
| 4 | `POST /action` | `set_status` | `user.email` (JWT) |
| 5 | `POST /{company}` (201) | `create_cr` | `user.email` → `created_by` |
| 6 | `GET /{company}` | `list_crs` | — |
| 7 | `GET /{company}/{cr_id}` | `get_cr` | — |
| 8 | `POST /{company}/{cr_id}/send-for-approval` | notify only | — |
| 9 | `PUT /{company}/{cr_id}` | `update_cr` | — |
| 10 | `DELETE /{company}/{cr_id}` | `delete_cr` | `user.email` → `deleted_by` |
| 11 | `PUT /{company}/{cr_id}/lines` | `update_cr_lines` | — |
| 12 | `PUT /{company}/{cr_id}/approve` | `approve_cr` | `user.email` → `approved_by` |
| 13 | `PUT /{company}/{cr_id}/box` | `upsert_box` | — |
| 14 | `PUT /{company}/{cr_id}/boxes` | `bulk_save_boxes` | — |
| 15 | `PUT /{company}/{cr_id}/save` | `save_cr` | — |

**Route renamed:** source `GET /rtv/action` → **`GET /email-action`** (matches the signed-token design;
`/action` alone is reserved for the programmatic POST #4). `GET /export` builds a styled `.xlsx`
(openpyxl `StreamingResponse`) with edited-box cells highlighted by cross-referencing `box_edit_logs`.

---

## 9. Auth & Authorization

- Every endpoint: `Depends(get_current_user)` (JWT). No blanket middleware exists — an endpoint without
  the dependency is fully open, so none may be omitted.
- **Actor identity from the token**, never from request params: `created_by`, `deleted_by`,
  `approved_by`, `actioned_by` all = `user.email`. (Source took these from spoofable query/body params.)
- **Ownership on approve/reject/hold:** enforced in `approval_service` by matching the acting identity
  (JWT `user.email`, or the signed token's `he` claim on the email-link path) against the RTV's stored
  `business_head` email. No separate DB permission seed is required (we chose token-identity auth, not
  the hierarchical RBAC path).
- `GET /email-action` is the **one** endpoint reachable without a session (clicked from an email in a
  mail client). It is protected by the signed `cr_action` token instead (§10).

---

## 10. Magic-Link Approval (signed `cr_action` token) — `action_token.py`

Refinement over the source (whose live link was **unsigned** plaintext query params; its JWT file was
dead code). We adopt the intended **signed** design, reusing the target's existing JWT plumbing:

- **Secret/alg/issuer:** reuse `jwt_service`'s `JWT_SECRET` / `JWT_ALGORITHM` (HS256) / `JWT_ISSUER`.
  Do **not** call `verify_access`/`verify_refresh` (they hard-require `type ∈ {access,refresh}`).
- **New token type** `cr_action` with claims: `{cr: <rtv_id str, the PK>, co: <company>,
  he: <business_head_email lower>, act: <approve|reject|hold>, type: "cr_action", iat, exp, jti, iss}`.
  (No integer `rid` claim — the `rtv_id` string in `cr` is the identifier.)
- **TTL:** new setting `CR_ACTION_TOKEN_TTL_SECONDS` (hours/days; access-token's 15 min is too short).
- **Link:** `{PUBLIC_BACKEND_URL}/api/v1/customer-returns/email-action?token=<jwt>`.
  Reject/Hold portal deep-links use `{WEB_APP_URL}` (mirrors `sample_mail_service`).
- **`GET /email-action`** verifies the token, matches `he` against the stored business head, then runs
  `apply_email_action` (guarded, idempotent, `already_actioned` handling) and renders an HTML result page
  (success / already-actioned / failed) — same UX as the source's confirm-and-render page.

Refactor `jwt_service`'s private `_secret()/_alg()/_iss()` into a shared helper (or import them) so the
CR signer inherits the prod-boot secret safety without duplicating it.

---

## 11. Notifications (`notify_service.py`) — all best-effort, never raise into the request

### 11.1 Email (magic-link)
Port onto the target's `sample_mail_service` idiom (HTML card, daemon-thread SMTP, deterministic
per-recipient Message-ID + constant subject for Gmail threading, `_emails_for_role` recipient
resolution). Reuse `SMTP_*`, `PUBLIC_BACKEND_URL`, `WEB_APP_URL` from `Settings`. Notifications to port:
created (with Accept/Reject/Hold buttons), approved, rejected, held, status-changed, header-updated,
lines-updated, updated (save summary), deleted, weight-discrepancy.

### 11.2 WhatsApp (on create) — in scope
Reuse `whatsapp_service` (`_post`, `_send_template`, disabled/no-op gate). Requires **new Meta UTILITY
template(s)** (`WHATSAPP_TPL_CR_*`) + a CR language var. Note: template names / verify-token / app-secret
are read from `os.environ` (not `Settings`); either add CR fields to `Settings` **and** extend the
`main.py` hydration loop, or export them via the deploy env. No-ops until templates are registered.

### 11.3 Realtime webhook/WS events — in scope
Net-new: add typed `customer_returns.*` functions to `app/webhooks/events.py` with `target_roles`
(e.g. `inventory_manager`, `admin`, `business_head`); add a `customer_returns.` (or `cr.`) prefix to the
relevant roles in `broadcaster.py` `ROLE_EVENT_MAP` (else events are filtered out). Emit from router
endpoints wrapped in `async with deferred_events(): async with conn.transaction():` so events fire only
after commit. Events: `customer_returns.created`, `.approved`, `.status_changed`, `.updated`, `.deleted`.

---

## 12. Cold-Stock Mirror (`sync_cold_stocks_from_cr`) — load-bearing idempotency

Mirrors printed boxes of a CR into `{prefix}_cold_stocks` so returned lots appear in cold inventory.
Runs inside the caller's transaction; guarded by `to_regclass` (no-op where the table is absent).

**Algorithm (must preserve exactly):**
1. Read header (`factory_unit`, `customer`, `rtv_id`, `rtv_date`); return 0 if missing.
2. `wh = canonical_factory_unit(factory_unit)`; `unit = _CR_COLD_UNIT_MAP.get(wh)`
   (`Savla D-39→D-39`, `Savla D-514→D-514`, `Rishi`, `Supreme`, `Eskimo`).
3. **Delete own rows first** (idempotent / re-submit safe):
   `DELETE FROM {prefix}_cold_stocks WHERE inward_transaction_no = $1 AND auto_created_from_inward = true`
   with `$1 = rtv_id`.
4. If `unit` is None (not a cold warehouse) → return 0 (stale auto-rows already cleared).
5. `INSERT … SELECT` **one row per printed box** (`WHERE b.box_id IS NOT NULL AND b.rtv_id = $cr_id`),
   joining each box to its matching line via
   `LATERAL (… WHERE l2.rtv_id=b.rtv_id AND l2.item_description=b.article_description LIMIT 1)`
   (the line is now unique per `(rtv_id, item_description)`).

**INSERT the 22 non-canonical columns; OMIT the 3 trigger-filled `canonical_*` columns** (a `BEFORE
INSERT` trigger overwrites them from `unit`/`storage_location`/`item_description`/`group_name`/`item_subgroup`).
Also OMIT `created_at`/`updated_at` (defaults).

Columns + source expressions:
`inward_dt←rtv_date, unit←unit, inward_no←rtv_id, cold_item_mark←COALESCE(b.item_mark,l.item_mark),
vakkal←COALESCE(b.vakkal,l.vakkal), lot_no←COALESCE(b.lot_number,l.lot_number), no_of_cartons←1,
weight_kg←b.net_weight, total_inventory_kgs←b.net_weight, group_name←l.item_category,
item_description←l.item_description, storage_location←wh, exporter←customer, last_purchase_rate←l.rate,
box_id←b.box_id, transaction_no←rtv_id, item_subgroup←l.sub_category, item_mark←COALESCE(b.item_mark,l.item_mark),
value←ROUND(COALESCE(b.net_weight,0)*COALESCE(l.rate,0),2), inward_transaction_no←rtv_id,
auto_created_from_inward←true, spl_remarks←COALESCE(b.spl_remarks,l.spl_remarks)`.

> **Critical:** `inward_transaction_no` **and** `auto_created_from_inward` MUST be written — they are the
> ownership key/flag the delete-own-rows step matches on. The target's existing transfer inserts omit both;
> if this port copies that omission, every re-submit **duplicates** returned lots into cold inventory.
> asyncpg: coerce `net_weight`/`rate`/`value` to `Decimal`, `rtv_date` to `date`, pass real `True`.

---

## 13. Config Additions (`app/config.py` `Settings`)

Present & reused: `JWT_SECRET`, `JWT_ALGORITHM`, `JWT_ISSUER`, `SMTP_HOST/PORT/EMAIL/APP_PASSWORD`,
`PUBLIC_BACKEND_URL`, `WEB_APP_URL`, `WHATSAPP_ENABLED/ACCESS_TOKEN/PHONE_NUMBER_ID/GRAPH_BASE`.

**Add:**
- `CR_ACTION_TOKEN_TTL_SECONDS` (magic-link TTL).
- `WHATSAPP_TPL_CR_*` template name(s) + `WHATSAPP_CR_LANG` — and extend the `main.py` os.environ
  hydration loop if the WhatsApp send should read them from `Settings` rather than raw env.
- (Only if CR must run on a different host) `APP_BASE_URL`; otherwise reuse `PUBLIC_BACKEND_URL`/`WEB_APP_URL`.

---

## 14. Phasing (implementation order)

| Phase | Deliverable | Verifiable by |
|---|---|---|
| **1 — Data + core CRUD** | `070_*.sql` + migrate wiring; `tables.py`, `schemas.py`; `query_service` + `create_service`; router (CRUD endpoints) + JWT; `main.py` registration | create/list/get/update/delete a CR end-to-end |
| **2 — Boxes + export** | `box_service` (upsert/print, bulk_save, summary, edit-log); `GET /export` with highlight; `box-edit-log` endpoint | print boxes, bulk save, export xlsx with highlights |
| **3 — Approval + email + WhatsApp + events** | `save_service`, `approval_service`, `action_token.py`, `notify_service` (email magic-link + WhatsApp + realtime events); endpoints 3/4/8/12/15 | approve via signed link; emails/WhatsApp/events fire |
| **4 — Cold-stock mirror** | `cold_sync_service`; wire into approve + bulk_save | approve/submit a cold-warehouse CR → rows appear once in `*_cold_stocks`, re-submit does not duplicate |

Each phase is its own plan → execute cycle.

---

## 15. Testing Strategy

- Unit: `rtv_id`/`box_id` generators (both formats), `value = qty*rate`, box↔line exact-match resolver,
  short-weight summary, date parsing, sort_by allow-list.
- Service (against a test DB where the tables exist via the migration): CRUD, bulk-box diff
  (insert/update/delete/unchanged counts), status guards, `already_actioned`, cross-company 404/409.
- Cold mirror idempotency: submit twice → row count stable; warehouse change → old auto-rows cleared.
- Auth: endpoints reject missing/invalid JWT; identity fields come from token; approve ownership enforced.
- Magic-link: token verify (good/expired/tampered), `he` mismatch → 403, terminal-status → already-actioned.
- Follow the target's existing module test layout under `tests/`.

---

## 16. Open Questions

- **OQ-1 (RESOLVED):** Physical tables renamed to `{prefix}_customer_return_header/_lines/_boxes`; header
  PK is `rtv_id`, lines/boxes link by `rtv_id`, no sequential `id`. (See §4.)
- **OQ-2:** Rename Pydantic/service symbols to `CR*` (recommended for clarity) or keep source `RTV*` names
  internally? Field names stay identical regardless (FE contract). Default: `CR*`.
- **OQ-6:** The line PK `(rtv_id, item_description)` forbids duplicate `item_description` within one return
  (source allowed dupes + matched first). Confirm returns never legitimately repeat an article line; if they
  can, switch to the per-return `line_number` model instead.
- **OQ-3:** WhatsApp Meta template registration — who owns creating the approved `WHATSAPP_TPL_CR_*`
  UTILITY templates, and what are their names/params? Sends no-op until these exist.
- **OQ-4:** Confirm `PUBLIC_BACKEND_URL` / `WEB_APP_URL` values are correct for the deploy target (wrong
  base = dead approval links).
- **OQ-5:** Confirm the target DB already has the `cold_stocks` canonical columns + `sync_canonical` trigger
  (true if running against prod RDS); the mirror deliberately omits `canonical_*` to stay safe if not.

---

## 17. Out of Scope

- The existing return-to-vendor disposition module (`rtv_disposition_service.py`) — untouched.
- An IMAP email-reply auto-approve listener — replaced by the magic-link flow (no target infra for it).
- Any frontend work beyond the ported `FRONTEND_API_DOC.md`.
- Renaming/re-modeling the shared `box_edit_logs` or `cold_stocks` schemas.

---

## Appendix A — Source function inventory (reference)

The source module comprises 49 functions across `rtv_tools.py` (28), `rtv_server.py` (16),
`rtv_models.py` (1 validator), `rtv_approval_token.py` (4), plus supporting notification functions in
`shared/email_notifier.py`, `shared/email_reply_listener.py`, `shared/whatsapp.py`. This port reproduces
the behavior of the `rtv_tools.py` + `rtv_server.py` + `rtv_models.py` surface; the `shared/*` notifier
behavior is re-implemented on the target's email/WhatsApp/event idioms (§11). The source's IMAP
`email_reply_listener.py` is intentionally **not** ported (§17).
