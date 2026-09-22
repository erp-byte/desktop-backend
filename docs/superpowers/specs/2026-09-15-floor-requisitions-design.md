# Floor requisitions — design

Date: 2026-09-15 · Status: awaiting review

## Purpose

On the job card's **Material allocation and requisition** tab, the floor sees each
BOM article's Fresh Stock on its own floor against the job card's requirement
("Short by 88.200 kg"). This adds the next step: the floor **requests** the
material it is short of, store **issues** it, and the floor confirms it was
**received** — recorded, with who did what and when.

It is a tracking record only. It never writes `new_stock_entries` or
`stocktake_transactions`; floor stock still changes only through stock take.

## Decisions (agreed in chat)

| Question | Decision |
|---|---|
| What confirming a request does | Creates a new floor requisition record (not a purchase indent) |
| Lifecycle | Raised (floor) → Issued (store) → Received (floor); Cancel while Raised |
| Where store works | A new "Floor Requisitions" list screen under Production |
| Quantity limits | Any positive quantity; defaults to the shortage |
| Articles per request | One |
| Requisition number | The 8-digit time-based number (`new_short_time_id`), as the primary key |
| Quantities | Every quantity stored together with its unit |

## 1. Data — migration `111_floor_requisition.sql`

Lives in the RDS `warehouse_db` (where `job_card_v2` is). Idempotent
(`IF NOT EXISTS`, `NOT EXISTS` guards). Registered in `scripts/migrate.py` after 110.

```sql
CREATE TABLE IF NOT EXISTS floor_requisition (
    requisition_id     BIGINT PRIMARY KEY,          -- the requisition number: app-supplied
                                                   -- 8-digit time id (new_short_time_id)
    job_card_id        BIGINT NOT NULL REFERENCES job_card_v2(job_card_id) ON DELETE RESTRICT,
    warehouse          TEXT   NOT NULL,             -- normalised: 'W202', 'A185'
    floor              TEXT   NOT NULL,             -- the job card's floor, trimmed
    material_sku_name  TEXT   NOT NULL,             -- as the job card's BOM spells it
    item_type          TEXT,                        -- RM / PM / SFG …, upper-cased

    requested_qty      NUMERIC(14,3) NOT NULL CHECK (requested_qty > 0),
    requested_unit     TEXT          NOT NULL CHECK (requested_unit IN ('kg','pcs')),

    -- Snapshot when raised: what the floor was looking at.
    required_qty       NUMERIC(14,3),               -- NULL = job card has no indent line for it
    required_unit      TEXT,
    available_qty      NUMERIC(14,3) NOT NULL,      -- Fresh Stock on this floor (0 if none)
    available_unit     TEXT          NOT NULL,
    shortage_qty       NUMERIC(14,3),               -- max(required - available, 0); NULL with required
    shortage_unit      TEXT,

    issued_qty         NUMERIC(14,3) CHECK (issued_qty > 0),
    issued_unit        TEXT,

    status             TEXT NOT NULL DEFAULT 'raised'
                       CHECK (status IN ('raised','issued','received','cancelled')),
    note               TEXT,                        -- floor's note when raising
    issue_note         TEXT,                        -- store's note when issuing
    cancel_reason      TEXT,

    raised_by    TEXT NOT NULL, raised_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    issued_by    TEXT,          issued_at    TIMESTAMPTZ,
    received_by  TEXT,          received_at  TIMESTAMPTZ,
    cancelled_by TEXT,          cancelled_at TIMESTAMPTZ,

    -- One unit per request: every stored unit is the requested one.
    CHECK (available_unit = requested_unit),
    CHECK (required_unit IS NULL OR required_unit = requested_unit),
    CHECK (shortage_unit IS NULL OR shortage_unit = requested_unit),
    CHECK (issued_unit   IS NULL OR issued_unit   = requested_unit),
    -- A quantity and its unit are present together.
    CHECK ((required_qty IS NULL) = (required_unit IS NULL)),
    CHECK ((shortage_qty IS NULL) = (shortage_unit IS NULL)),
    CHECK ((issued_qty   IS NULL) = (issued_unit   IS NULL)),
    -- Each status carries its own facts.
    CHECK (status NOT IN ('issued','received') OR (issued_qty IS NOT NULL AND issued_by IS NOT NULL)),
    CHECK (status <> 'received'  OR received_by  IS NOT NULL),
    CHECK (status <> 'cancelled' OR (cancelled_by IS NOT NULL AND BTRIM(COALESCE(cancel_reason,'')) <> ''))
);

-- One open request per job card + article.
CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_open
    ON floor_requisition (job_card_id, UPPER(BTRIM(material_sku_name)))
    WHERE status = 'raised';
CREATE INDEX IF NOT EXISTS idx_floor_requisition_place
    ON floor_requisition (warehouse, floor, status, raised_at DESC);
CREATE INDEX IF NOT EXISTS idx_floor_requisition_job_card
    ON floor_requisition (job_card_id, raised_at DESC);
```

**The number.** `requisition_id` is minted by `app.core.helpers.new_short_time_id`
and inserted through `insert_with_pk_retry`. It must be the primary key: that
retry only catches collisions on a `_pkey` constraint. The numbers wrap roughly
every 28 hours and are not in date order — a clash with an older row simply
retries with a fresh number — so every list sorts by `raised_at`, never by number.

**Permissions** (same migration). Catalog rows `('production', 'floor_requisitions',
NULL, action)` for `view`, `create`, `issue`, `receive`, `cancel`, inserted with the
`NOT EXISTS … IS NOT DISTINCT FROM` guard (084's NULL-safe pattern). Grants:

| Role | view | create | issue | receive | cancel |
|---|---|---|---|---|---|
| admin | ✓ | ✓ | ✓ | ✓ | ✓ |
| floor_manager | ✓ | ✓ | | ✓ | ✓ |
| store_head | ✓ | | ✓ | | ✓ |

## 2. Server — `app/modules/floor_requisition/`

`router.py` (prefix `/api/v1/floor-requisitions`, included in `app/main.py`) and
`services/requisition_service.py`. Every write runs in one transaction. The actor
of every step is the access token's user (the production router's `_actor_name`
rule: full name → email → phone → `user:<id>`), never a body field.

### Endpoints

| Method + path | Permission | Does |
|---|---|---|
| `GET /floor-requisitions` | view | List. Filters: `status`, `warehouse`, `floorName`, `job_card_id`, `search` (article words); `page`, `page_size` (default 100). Newest raised first. Returns `{items, total, page, page_size}`. |
| `POST /floor-requisitions` | create | Raise. Body `{job_card_id, material_sku_name, requested_qty, note?}`. |
| `POST /floor-requisitions/{id}/issue` | issue | Raised → Issued. Body `{issued_qty, issue_note?}`. |
| `POST /floor-requisitions/{id}/receive` | receive | Issued → Received. No body. |
| `POST /floor-requisitions/{id}/cancel` | cancel | Raised → Cancelled. Body `{reason}`. |

Each write returns the full updated row.

### Raising — what the server works out itself

The browser sends only the job card, the article, the quantity and a note.
Everything else comes from the database:

1. **The job card** (`job_card_v2`): 404 `job_card_not_found`; 422
   `job_card_has_no_place` when it has no factory or floor. `warehouse` is the
   factory through `stock_take.floors.normalise_warehouse` ('W-202' → 'W202');
   `floor` is trimmed.
2. **The article** must be on the job card — a `bom_line` row of the job card's
   `bom_id` (the query behind the detail's `bom_lines`, `job_card_v2.py`), or a
   `job_card_rm_indent_v2` / `job_card_pm_indent_v2` row of the job card, matched
   on `UPPER(BTRIM(material_sku_name))`; else 422 `article_not_on_job_card`.
   `item_type` comes from the BOM line, else RM / PM from the indent table.
3. **The unit**: the article's indent-line uom normalised (KGS → `kg`,
   PCS / NOS → `pcs`); with no indent line, `pcs` for PM and `kg` otherwise.
4. **The snapshot**, the same figures the tab shows:
   - `required_qty` = the article's indent lines' `gross_qty` summed (falling back
     to `reqd_qty`), NULL with no indent line;
   - `available_qty` = Fresh Stock rows of `floor_stock_service.fetch_floor_stock`
     for this warehouse + floor — `available_kg` for kg, `available_quantity` for pcs;
   - `shortage_qty` = `max(required − available, 0)`, NULL when required is NULL.
   All rounded to 3 dp.
5. **The quantity**: > 0; at most 3 decimals for kg, a whole number for pcs; else
   400 `qty_invalid`. No upper cap.
6. **Scope**: the caller's `allowed_warehouses` / `allowed_floors` must cover the
   job card's place — the stock-take `_floor_stock_place` rule, moved to a shared
   helper both modules call. Else 403 `warehouse_not_allowed` / `floor_not_allowed`.
7. **Insert** with a fresh 8-digit id. A hit on `uq_floor_requisition_open` →
   409 `open_requisition_exists`, carrying the open requisition's number.

### Moving a request on

Each step is one guarded update, so two people acting at once cannot both win:

```sql
UPDATE floor_requisition SET status = 'issued', issued_qty = $2, issued_unit = requested_unit,
       issue_note = $3, issued_by = $4, issued_at = now()
 WHERE requisition_id = $1 AND status = 'raised'
RETURNING *;
```

No row back → 404 `not_found` if the id does not exist, else 409 `status_changed`
with the current status (the screen reloads rather than showing an error).
`issued_qty` follows the same unit rules as raising and may differ from the
requested quantity. A cancel needs a non-blank reason (400 `reason_required`).
Scope is checked against the row's warehouse + floor on every step, and the list
returns only rows inside the caller's scope.

## 3. Web

### Job card → Material allocation and requisition tab

- **BOM articles table** gains a last column, **Request**, spanning the article's
  stock-type rows like the verdict does; on a phone the article card gets the same
  control.
  - No open request: a **Request** button.
  - Open (raised) request: `#94618273 · Raised · 88.200 kg` instead of the button.
  - The latest request is issued / received: its status line, plus the button
    (another request can be raised).
- **Request dialog** (`role="dialog"`, the Amendments modal pattern: full-screen
  below md, centred box from md): article, type, plant · floor; Required /
  Fresh stock / Shortage read-only; **Quantity** pre-filled with the shortage
  (blank when not short), unit shown beside it; optional note; **Cancel** /
  **Raise request**. Quantity is validated as the server does before sending.
  Esc and a backdrop click close it; focus starts in Quantity. A 409 shows its
  message inline.
- **New section "Requisitions for this job card"**: bordered table (number,
  article, requested, issued, status, raised by / at) with **Mark received** on
  issued rows and **Cancel** (reason prompt) on raised rows, each shown only with
  its permission. Phone: stacked cards. Hidden without `floor_requisitions.view`;
  the Request column is hidden without `create`.

### Production → Floor Requisitions (store)

- Route `/modules/production/floor-requisitions`; tile under **Inventory**, gated
  on `floor_requisitions.view` in the landing page's `tileAllowed`.
- `ROLE_MODULE_SCOPE.store_head` gains `"production/floor-requisitions"`.
- Filters: status (default Raised), plant, floor, article search. Bordered table,
  server-paged 100 per page with the same pager as the tab. Phone: stacked cards.
- **Issue** dialog: the request's facts; Issued quantity pre-filled with the
  requested quantity, unit shown; optional note. **Cancel** with a reason.

### Files

| File | Role |
|---|---|
| `src/lib/floor-requisitions.ts` | API client + types |
| `src/lib/floor-requisition-form.ts` | Pure helpers: default quantity, quantity parse/validate per unit, status label. No React, node-testable. |
| `src/app/modules/job-card/[id]/_RequestDialog.tsx` | Request dialog |
| `src/app/modules/job-card/[id]/_JobCardRequisitions.tsx` | The tab's requisitions section |
| `src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx` | Request column; loads the job card's requisitions |
| `src/app/modules/job-card/[id]/page.tsx` | Passes `job_card_id` to the tab |
| `src/app/modules/production/floor-requisitions/page.tsx` + `_IssueDialog.tsx` | Store screen |
| `src/app/modules/production/page.tsx`, `src/lib/modules.tsx` | Tile + store_head scope |

## 4. Testing

- **Server, no database** (pytest): unit derivation; snapshot arithmetic and
  rounding; quantity rules per unit; scope; actor from the token, never the body;
  a guarded update that returns no row → 404 / 409; open-request unique violation
  → 409; PK collision retries; route → permission map (the `test_stock_take_rbac`
  pattern).
- **Web**: node tests for `floor-requisition-form.ts`; `tsc` clean outside `.next`;
  ESLint 0 on new files and no new problems in `page.tsx`.
- **Live database**: the migration is applied by the user (or by me on explicit
  instruction, that one file only — never a full `migrate.py` run). Any live test
  that writes, even inside a rolled-back transaction, runs only with the user's
  go-ahead.

## Out of scope

Moving stock or posting stock-take adjustments; notifications (mail / WhatsApp);
several articles in one request; several partial issues against one request;
printing; the Android client.
