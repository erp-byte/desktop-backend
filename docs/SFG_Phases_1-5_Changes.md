# SFG / Job-Card Integration — Phases 1–5 Change Log (DB · Backend · Frontend)

> Scope: the **foundational SFG (Semi-Finished Goods / WIP) vertical**, built as
> the first five vertical slices of the SFG/Job-Card programme. Phase numbering
> here maps 1:1 to the execution playbook's **Slices 1–5**
> (`app/db/test_db/md/SFG_JobCard_Execution_Playbook.md`). Later capabilities
> (boxes/QR & genealogy = Slice 6 / "Phase 7", reporting = Slice 7, gap
> promotion, bar-line override, seam-minting, catalogue screens) are **out of
> scope** for this document.

| Phase | Slice | Theme | Gate |
|------|------|-------|------|
| 1 | 1 | SFG identity in the catalogue | — (8-digit PK convention) |
| 2 | 2 | Process classification + Create-WIP checklist | **G2** Sorting=inline, Packaging=terminal |
| 3 | 3 | 2-stage routing + the SFG#### seam | — |
| 4 | 4 | Stage-2 consumes the SFG | **G1 = Option B** (SFG is a real `bom_line`) |
| 5 | 5 | WIP materialisation + SFG inventory picker | **G3** `WIP_SHELF_LIFE_DAYS` (shipped 7 → now **0**) |

### Conventions that apply across all five phases
- **Forward-only migrations** — every `app/db/0NN_*.sql` is idempotent
  (`ADD COLUMN IF NOT EXISTS`, `CREATE … IF NOT EXISTS`, guarded `DO` blocks).
  No down-migrations in the repo; teardown is the manual
  `app/db/rollback/sfg_integration_down.sql`.
- **8-digit app-supplied PKs** only where the codebase already does so:
  `new_short_time_id()` + `insert_with_pk_retry()` (`app/core/helpers.py`).
  New SFG `all_sku` rows and WIP `inventory_batch` rows get these; every other
  insert site keeps its `SERIAL`/existing key.
- **Advisory locks** serialise the ingest writers:
  `SFG_CODE_LOCK = 0x5F67` ([helpers.py:186](../app/core/helpers.py#L186)),
  `PROCESS_CLASS_LOCK = 0x5F68` ([helpers.py:189](../app/core/helpers.py#L189)),
  `ROUTING_LOCK = 0x5F69` ([helpers.py:193](../app/core/helpers.py#L193)).
- **Data lives in Python, schema in SQL.** Migrations prepare columns; the rows
  (343 SFGs, 1460 routing steps, 100 SFG bom-lines) are loaded by
  `run_master_ingest` at app startup so each gets the id/normalisation logic a
  static `.sql` can't express.
- **No `CHECK` on `item_type`** anywhere — `'sfg'`/`'wip'` are free-text TEXT.
- **Frontend** is a *modified* Next.js (App Router). Pages are `"use client"`
  + `useEffect`/`useState` + `apiFetch` from `@/lib/auth`. Nav is the `/modules`
  tile grid (no left sidebar). Read `web_replica/node_modules/next/dist/docs/`
  before non-trivial FE work.

---

## Phase 1 — SFG identity in the catalogue (Slice 1)

**Goal:** make a Semi-Finished Good a first-class catalogue item with a stable
business key (`SFG####`) and the schema "spine" the rest of the work rides on.

### Database
- **`050_sfg_foundation.sql`** ([file](../app/db/050_sfg_foundation.sql)) — the schema spine:
  - `bom_process_route` gains the **seam carrier columns**:
    `practical_operation`, `stage_bucket`, `input_kind`, `output_kind`,
    `input_code`, `output_code` (Phases 2–3 fill these).
  - `bom_line.consumed_at_stage` — reserved for Option B (Phase 4).
  - `job_card_v2.input_code` / `output_code` — the chain seam carriers
    (guarded with `to_regclass` because the migrate runner orphans v2).
  - `item_type` domain widened **in COMMENT only** (`rm | pm | fg | sfg`).
- **`051_sfg_seed_catalog.sql`** ([file](../app/db/051_sfg_seed_catalog.sql)) — the catalogue key:
  - `all_sku.sku_id` widened **`SERIAL` → `BIGINT`** (so 8-digit ids are
    first-class; the `nextval` default is intentionally kept for all other
    insert sites; the 6 INT FKs stay valid).
  - `all_sku.sfg_code TEXT` + partial unique index
    `uq_all_sku_sfg_code … WHERE sfg_code IS NOT NULL` (one code → one row).

### Backend
- **`app/core/helpers.py`**
  - `normalise_key()` ([:143](../app/core/helpers.py#L143)) — NBSP/mojibake/NFC
    normalisation; the canonical join-by-name primitive used by every ingest.
  - `next_sfg_code(conn, width=4)` ([:207](../app/core/helpers.py#L207)) — mints
    the next `SFG####` under `SFG_CODE_LOCK`.
- **`master_ingest.ingest_sfg_master()`** ([:222](../app/modules/production/services/master_ingest.py#L222))
  — loads **343 rows** as `item_type='sfg'`, each with an 8-digit BIGINT
  `sku_id` and an `SFG####` code; logs each re-typed row's prior `item_type` for
  a manual revert. Wired into `run_master_ingest` (cold + warm).
- **`response_filters.py`** — the backend cost-gate reserves the SFG/WIP keys so
  cost figures never leak; kept in **exact parity** with `cost-gate.ts`.

### Frontend
- **`(SFG)` catalogue tag** — `item_type` is rendered inline next to every
  material row in the Job-Card consumption / returned-to-store lists
  ([job-card/[id]/page.tsx:5164, 5519](../../web_replica/src/app/modules/job-card/[id]/page.tsx#L5164)).
  SFG/WIP render in **success green + bold**; RM/PM/FG render muted.
- **`web_replica/src/lib/cost-gate.ts`** — UI cost-gate (deny/allow role lists),
  mirrored against `response_filters.py`.

```
Material consumption
┌───────────────────────────────────────────────┬──────────────────┐
│ SKU001-Groundnuts  (RM)   ← muted grey         │ [ qty ] kg  Var  │
│ SFG0045            (SFG)  ← green + bold        │ [ qty ] kg  Var  │
└───────────────────────────────────────────────┴──────────────────┘
```

**Functional outcome:** SFGs exist in the catalogue with a stable `SFG####`
key, minted with collision-safe ids, visually distinguishable, and cost-gated.
Tests: `tests/services/test_sfg_slice1.py`.

---

## Phase 2 — Process classification + Create-WIP checklist (Slice 2)

**Goal:** classify each routing step into a *practical operation* and a *stage
bucket* (`Create WIP` vs `Final FG`), so the system knows which step produces a
WIP and the operator can confirm the combined operations.

### Database
No new migration — classification **writes into 050's** `practical_operation`
and `stage_bucket` columns on `bom_process_route`.

### Backend
- **`master_ingest.classify_route_steps()`** ([:428](../app/modules/production/services/master_ingest.py#L428))
  — maps raw step names → `practical_operation` + `stage_bucket` via the token
  maps (`_TRANSFORM_OPS`, `_SEASONING_TOKENS`, `_TERMINAL_TOKENS`,
  `_INLINE_TOKENS`).
- **`master_ingest.ingest_process_category_rules()`** ([:458](../app/modules/production/services/master_ingest.py#L458))
  — applies the rules under `PROCESS_CLASS_LOCK`; **re-entrant** (on the warm
  path it scopes only to routes with a NULL `stage_bucket`, so it never
  re-thrashes already-classified rows).
- **G2 locked:** `Sorting = inline` (not its own stage), `Packaging = terminal`
  (Final FG, kept last).

### Frontend
- **`web_replica/src/lib/processCatalog.ts`** — the token maps mirrored
  **byte-identical** to the Python classifier (`classifySteps()`, `canonProcess()`).
- **`CreateWipChecklist`** ([job-card/[id]/page.tsx:2821–2871](../../web_replica/src/app/modules/job-card/[id]/page.tsx#L2821))
  — on the JC detail **Overview** tab, shown only on producer stages
  (`output_kind` SFG/WIP and `stage_bucket = Create WIP`). Groups the chain's
  steps by practical operation and lets the operator tick each combined op.

```
┌─ Create-WIP operation: Roast & Flavour/Salt ───────────────┐
│ Confirm the practical operation(s) combined at this stage   │
│ before recording output.                                    │
│                                                             │
│   ☐ Roasting                                                │
│   ☐ Flavouring                                              │
│   ☐ Salt Application                                        │
└─────────────────────────────────────────────────────────────┘
```

**Functional outcome:** every routing step is bucketed (Create WIP vs Final FG)
and the producer-stage operator gets a checklist confirming the operations that
roll up into the WIP. Tests: `tests/services/test_sfg_slice2.py`.

---

## Phase 3 — 2-stage routing + the SFG#### seam (Slice 3)

**Goal:** bind a 2-stage article's producer step to its consumer step with a
real `SFG####` "seam" — `JC1.output_code → JC2.input_code`.

### Database
- **`052_sfg_routing_bom.sql`** ([file](../app/db/052_sfg_routing_bom.sql)) —
  **documentary** (`COMMENT`s pinning the seam-column semantics). No new index by
  design: the only seam lookup filters `bom_id` + `output_kind` and is already
  served by `idx_bom_route_bom` + `UNIQUE(bom_id, step_number)` (1–2 rows/bom).

### Backend
- **`master_ingest.ingest_jc_routing()`** ([:535](../app/modules/production/services/master_ingest.py#L535))
  — loads `JC_Routing_Plug.csv` (**1085 articles / 375 two-stage**, 1460 steps)
  into `bom_process_route`, **REPLACING** each article's route (delete non-plug
  step numbers + upsert) and stamping the seam columns. Under `ROUTING_LOCK`;
  re-entrant (warm path skips already-stamped routes via `output_kind`);
  ambiguous `(name, entity)` `bom_header` duplicates are **skipped, not guessed**.
- **The chain WRITE** lives at job-card creation, not in ingest:
  - `job_card_v2._resolve_sfg_seam_code(conn, bom_id)` ([:737](../app/modules/production/services/job_card_v2.py#L737))
    resolves the `SFG####` a 2-stage article produces.
  - `create_job_cards_from_plan` stamps a **2-step chain only** (a diverged step
    count is logged + skipped, never mis-stamped): JC1 gets
    `output_kind='SFG'` + `output_code=SFG####`; JC2 gets `input_code=SFG####`.
  - Mirrored in `plan_v2._resync_jcs_after_step_change` (reorder / add / delete).

### Frontend
- **`StageChainTab`** ([job-card/[id]/page.tsx:2130–2223](../../web_replica/src/app/modules/job-card/[id]/page.tsx#L2130))
  + the `SeamKind` helper ([:2038–2043](../../web_replica/src/app/modules/job-card/[id]/page.tsx#L2038)):
  the **chain** tab renders each stage as a card with the seam handoff
  `input → output`. A concrete code renders as a **mono navy `SFG0012`** chip;
  otherwise the kind string (`RM`/`SFG`/`FG`).

```
Stage chain · 3 steps
┌──────────────────────────────────────────────────────────┐
│ [1] Roasting                                       opened  │
│     Dry Roast ·  RM → SFG0012  · Floor A                  │
├──────────────────────────────────────────────────────────┤
│ [2] Flavouring                                   ● current │
│     Wet Pack ·  SFG0012 → SFG0015  · Floor B             │
├──────────────────────────────────────────────────────────┤
│ [3] Packaging                                    unlocked  │
│     Final Pack ·  SFG0015 → FG  · Floor C               │
└──────────────────────────────────────────────────────────┘
              ▲ the SFG#### chip is the seam between stages
```

**Functional outcome:** a 2-stage article's WIP handoff is a real, queryable
binding the chain view visualizes. Verified on Docker pg16 (46 assertions + full
dataset). Note: `output_kind 'WIP' → 'SFG'` on the producer is safe — downstream
readers accept both.

---

## Phase 4 — Stage-2 consumes the SFG (Slice 4, Gate **G1 = Option B**)

**Goal:** the Final-FG stage **consumes** the SFG as a costed input.
**G1 = Option B** = the SFG is a real `bom_line` (not just a flow-driven note),
so it participates in costing and MRP.

### Database
Uses 050's **`bom_line.consumed_at_stage`** — the SFG `bom_line` is keyed by
`SFG####` with `consumed_at_stage = 'Final FG (opening RM)'`.

### Backend
- **`master_ingest.ingest_jc_bom()`** ([:731](../app/modules/production/services/master_ingest.py#L731))
  — loads the **100 distinct SFG `bom_line`s** from `JC_BOM_Plug.csv` (101 rows,
  1 genuine dup).
- **`job_card_v2.upsert_consumption_lines()`** ([:432](../app/modules/production/services/job_card_v2.py#L432))
  — takes a **per-entry `input_kind`** (RM/PM/SFG/WIP) + `source_dispatch_id`.
- **`mrp.py`** — resolves `item_type='sfg'` on-hand from `inventory_batch`
  `item_type='wip'` (an SFG's stock IS its WIP batches).
- **`jc_accounting_v2.save_consumption()`** ([:548](../app/modules/production/services/jc_accounting_v2.py#L548))
  — `ON CONFLICT` aligned to the 3-col `uq_jcmc_v2_jc_batch_material`;
  `source_dispatch_id` is `COALESCE`-preserved on re-save.

> **Review fixes (Slice 4):** the operator/outputs save was persisting SFG
> consumption as `input_kind='RM'` (now sends per-row `input_kind`); the SFG
> `bom_line` is **suppressed** in the consumption grid on SFG-producer stages
> (via the `articles` `useMemo`) so a producer never shows its own output as an
> input.

### Frontend
- The consumption grid sends a **per-row `input_kind`** (`ConsumedLineV2`), and
  hides the SFG line on producer stages — so RM/PM/SFG/WIP are recorded with
  the correct kind rather than defaulting to RM.

**Functional outcome:** Stage-2 consumes the SFG as a first-class costed input;
MRP nets SFG demand against on-hand WIP. Tests: `tests/services/test_sfg_slice4.py`.

---

## Phase 5 — WIP materialisation + SFG inventory picker (Slice 5, Gate **G3**)

**Goal:** when a Create-WIP stage closes / dispatches, **materialise a real WIP
inventory batch**; the consumer stage's operator then **picks** an available WIP
batch to consume.

### Database
Uses `inventory_batch` with `item_type='wip'` (already permitted by the base
schema; no new migration in this slice). *(The physical-box scaffold
`053_sfg_box.sql` is **Slice 6**, not Phase 5.)*

### Backend
- **`inventory_service.create_wip_batch()`** ([:87](../app/modules/production/services/inventory_service.py#L87))
  — mints an 8-digit TEXT `batch_id` (via `insert_with_pk_retry`),
  `item_type='wip'`, `source='PRODUCTION'`, `expiry = mfg + WIP_SHELF_LIFE_DAYS`.
- **`inventory_service.get_sfg_on_hand()`** ([:146](../app/modules/production/services/inventory_service.py#L146))
  and **`get_available_batches()`** ([:185](../app/modules/production/services/inventory_service.py#L185))
  — item_type / **exact-code-match** / `exclude_expired` filters.
- **`job_card_v2.materialise_wip_dispatch()`** ([:1664](../app/modules/production/services/job_card_v2.py#L1664))
  — shared by **both** `close_batch` and `dispatch_to_next`: creates the WIP
  batch + a **synthetic audit-only** SFG consumption (`actual_consumed_qty=0`,
  `source_dispatch_id`) + a `floor_movement`. **Key model:** `carried_qty_kg` is
  THE chain input; the synthetic consumption is an audit breadcrumb only (so
  carried-in is never double-counted).
- **`floor_tracker`** — production ↔ wip/sfg_store transitions + a WIP bucket in
  `get_floor_summary`.
- **`GET /job-cards-v2/sfg-inventory`** — declared **before** `/{job_card_id}`
  so the literal path isn't captured as an id; cost-gated + entity/floor-scoped.

> **Review fixes (Slice 5):** **(HIGH)** `dispatch_to_next` now clamps
> `dispatched + qty ≤ produced` (close + manual double-dispatch was minting
> phantom WIP) and refuses terminal JCs; SFG lookups use **EXACT** code match,
> not substring `ILIKE` (`SFG001` was over-matching `SFG0012`).

### Frontend
- **`SfgInventoryPicker`** ([job-card/[id]/page.tsx:2061–2128](../../web_replica/src/app/modules/job-card/[id]/page.tsx#L2061))
  — in the Outputs/Accounting area on **consumer** stages: lists available WIP
  batches (FIFO) for the SFG to consume, each with a **Use** action that fills
  the consumption qty.

```
┌─ SFG inventory · SFG0012 ──────────────────────────────────┐
│ 3 batches · 150.5 kg available (FIFO)                       │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ B…612A01   25.0 kg   in 2025-06-12  exp 2025-07-20      │ │
│ │                                      Floor-A    [ Use ] │ │
│ ├────────────────────────────────────────────────────────┤ │
│ │ B…610A02   75.5 kg   in 2025-06-10  exp 2025-08-10      │ │
│ │                                      Floor-B    [ Use ] │ │
│ └────────────────────────────────────────────────────────┘ │
│ (empty → "materialises when the upstream Create-WIP closes")│
└─────────────────────────────────────────────────────────────┘
```

### Gate G3 — WIP shelf life
- **Shipped at `WIP_SHELF_LIFE_DAYS = 7`** (strict: `expiry = mfg + 7`,
  `exclude_expired` filtered WIP out after a week).
- **Later changed to `0`** (current value at
  [inventory_service.py:23](../app/modules/production/services/inventory_service.py#L23)):
  WIP carries **no shelf life** — `0` is falsy so `expiry` resolves to `NULL`
  and the `exclude_expired` filters treat NULL as in-date. Per-SFG
  `suggested_shelf_life_days` is deliberately **not** ingested.

**Functional outcome:** closing/dispatching a Create-WIP stage produces a real,
pickable WIP batch; the consumer operator selects it from the inventory picker.

---

## Cross-cutting summary

### Gate decisions
| Gate | Decision | Where |
|------|----------|-------|
| **G1** | Option B — SFG is a real `bom_line` input (costing + MRP) | Phase 4 |
| **G2** | `Sorting = inline`, `Packaging = terminal` | Phase 2 |
| **G3** | `WIP_SHELF_LIFE_DAYS` shipped 7 → **now 0** (no shelf life) | Phase 5 |

### Advisory locks introduced
`SFG_CODE_LOCK 0x5F67` (Phase 1) · `PROCESS_CLASS_LOCK 0x5F68` (Phase 2) ·
`ROUTING_LOCK 0x5F69` (Phase 3).

### File index
| Layer | File | Phase |
|------|------|------|
| DB | `app/db/050_sfg_foundation.sql` | 1 (+3,4) |
| DB | `app/db/051_sfg_seed_catalog.sql` | 1 |
| DB | `app/db/052_sfg_routing_bom.sql` | 3 |
| Backend | `app/core/helpers.py` (`normalise_key`, `next_sfg_code`, locks) | 1,2,3 |
| Backend | `app/modules/production/services/master_ingest.py` | 1,2,3,4 |
| Backend | `app/modules/production/services/job_card_v2.py` | 3,4,5 |
| Backend | `app/modules/production/services/inventory_service.py` | 5 |
| Backend | `app/modules/production/services/jc_accounting_v2.py` | 4 |
| Backend | `app/modules/production/services/mrp.py` | 4 |
| Backend | `app/modules/production/services/floor_tracker.py` | 5 |
| Backend | `app/modules/production/services/response_filters.py` | 1 |
| Frontend | `web_replica/src/lib/processCatalog.ts` | 2 |
| Frontend | `web_replica/src/lib/cost-gate.ts` | 1 |
| Frontend | `web_replica/src/app/modules/job-card/[id]/page.tsx` (tag, CreateWipChecklist, StageChainTab/SeamKind, SfgInventoryPicker) | 1,2,3,5 |

### Tests
`tests/services/test_sfg_slice1.py … test_sfg_slice4.py` (per-slice), plus the
Docker pg16 acceptance runs cited per phase. Slices 1–5 were each reviewed at a
manual gate before the next slice began.
