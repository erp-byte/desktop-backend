# CANDOR FOODS — SFG / WIP Design Reference for the Job Card Module

> **Status:** Reference / design analysis only. **Nothing here is to be coded or migrated yet.** This document re-interprets the contents of `SFG Folder.zip` against the *actual* backend (`backend/app`) and frontend (`frontend/src`) so that a future engineer can wire Semi-Finished Goods (SFG / WIP) into the existing **v2 job-card chain** without redesigning anything.
>
> **Author note:** Produced by reading every file in the SFG folder, the production DB migrations (`backend/app/db/*.sql`), the production services (`backend/app/modules/production/services/*`), and the frontend job-card module (`frontend/src/app/modules/job-card/*`).

---

## 0. The one-paragraph answer

The SFG folder is an **offline, second derivation of a stage chain that the backend already builds.** Both start from the *same* source column — `Process Category` in `FG_Master_Completion`. The backend turns it into `bom_process_route → production_plan_step_v2 → one job_card_v2 per step`, and it **already has SFG/WIP as first-class job-card concepts** (`input_kind`/`output_kind ∈ RM/SFG/WIP/FG`, `prev/next_job_card_id`, `carried_qty_kg`, `dispatched_to_next_kg`, `job_card_partial_dispatch_v2`). What the backend is **missing**, and what the SFG folder actually delivers, is a **named, coded SFG *item master*** (489 SFG items, deduped across pack sizes) so that the WIP flowing between stages stops being an anonymous "SFG from JC #…" string and becomes a real catalogued article. The folder's *data* (the FINAL CSV/XLSX) is worth keeping after cleanup; the folder's *scripts* (`build_sfg.py`, `patch.py`) are stale and should be discarded in favour of a small ingest aligned to the backend's existing master-ingest path.

---

## 1. Executive summary & decision table

| # | Finding | Decision |
|---|---------|----------|
| F1 | Backend **already models the SFG stage chain** at the job-card level (v2). It is *not* a redesign — it is a *data + materialisation* gap. | Build **on top of** the v2 chain; do not invent a new chain model. |
| F2 | SFG is **not a catalogued item** anywhere (`all_sku`, `bom_header`, `bom_line` have no `sfg` rows; `bom_line.item_type` only allows `'rm'`/`'pm'`). | The SFG folder's `SFG_Item_Master` fills exactly this gap. **Keep it as the seed for an SFG catalogue.** |
| F3 | `inventory_batch.item_type` and `floor_inventory.item_type` **already allow `'wip'`** but **nothing ever creates a WIP batch.** | Materialise SFG batches here when an intermediate stage records output — no schema change needed for inventory. |
| F4 | The folder's **scripts are stale**: `build_sfg.py` is an *intermediate* version; the real generator (`sfg_final.py`) is **absent**; `patch.py` patches that missing file. | **Discard** `build_sfg.py` + `patch.py`. Keep only the logic notes captured in §3. |
| F5 | **`*_FINAL.csv` files are byte-identical** to their non-FINAL twins; the non-FINAL `SFG_WIP_Item_List.xlsx` is a **reduced** earlier cut. | Keep one canonical copy each; delete the duplicates (§2). |
| F6 | SFG names are **verbose** (up to 76 chars, marketing text leaks in) and **33 carry `_v2/_v3` recipe-clash suffixes**. | Adopt the **`SFG####` code as the key**; treat the long name as a description; fix the variant smell (§8, §10). |
| F7 | "**Sorting**" appears in 707 FGs but is **deliberately not an SFG stage** in the FINAL output — it is folded into *"RM (sorted in-line)"*. The backend, by contrast, makes Sorting its own step/JC. | Define a **canonical stage taxonomy** (§6) that both ends agree on: which `+`-tokens become buffered SFG vs inline vs terminal FG. |
| F8 | There is **no `semi_closed` status** in the system. Job-card status ∈ `locked, unlocked, assigned, material_received, in_progress, completed, closed, cancelled`. "Semi-closed" is *expressed* via **per-batch closure + partial dispatch**. | Map the user's lifecycle vocabulary onto real states (§7) — do not add a status. |
| F9 | Stage **ordering is whatever Ops typed** in `Process Category` (e.g. `Barbeque Cashew Bulk = "Flavouring + Roasting + Bulk Packaging"` — flavour-before-roast is backwards). | Add a **stage-order validation pass** before any SFG load (§10). |

---

## 2. The SFG Folder — file-by-file verdict

Extracted contents of `SFG Folder.zip` (13 files):

| File | What it is | Useful? | Keep / Drop |
|------|-----------|---------|-------------|
| **`SFG_WIP_Item_List_FINAL.xlsx`** | **The master deliverable.** 15 sheets: `ReadMe`, `SFG_Item_Master` (489 items, full attributes inc. `VA_Article`, `Primary_BU`), `FG_Jobcard_Chain`, `FG_Coverage`, `SFG_Where_Used`, and 10 per-stage tabs (`Stage_Roasting`, `Stage_Blending`, …). | ⭐ Highest | **KEEP — canonical source** |
| `SFG_Item_Master_FINAL.csv` | Flat export of the `SFG_Item_Master` sheet (489 rows). | ✅ | KEEP (CSV mirror) |
| `FG_Jobcard_Chain_FINAL.csv` | The FG→stage→SFG linkage (1,595 rows). The wiring table. | ✅ | KEEP (CSV mirror) |
| `SFG_Where_Used_FINAL.csv` | Reverse index: SFG → which FGs consume it (509 rows). | ✅ | KEEP (CSV mirror) |
| `SFG_Item_Master_FINAL.csv` ↔ `SFG_WIP_Item_List.csv` | **Byte-identical** (`diff` = empty). | — | **DROP duplicate** (`SFG_WIP_Item_List.csv`) |
| `FG_Jobcard_Chain.csv` ↔ `FG_Jobcard_Chain_FINAL.csv` | **Byte-identical.** | — | **DROP** the non-FINAL |
| `SFG_Where_Used.csv` ↔ `SFG_Where_Used_FINAL.csv` | **Byte-identical.** | — | **DROP** the non-FINAL |
| `SFG_WIP_Item_List.xlsx` | **Stale earlier cut** — only 4 sheets, missing `VA_Article`/`Primary_BU`/per-stage tabs. | — | **DROP** |
| **`FG_Master_Completion (1).xlsx`** | The *source of truth* (FG → `Process Category`, `Bar Line Process`, group, GST, HSN…). Also present at `backend/data/FG_Master_Completion (1).xlsx`. | ⭐ Source | KEEP (already in repo) |
| **`BOM Details CFPL 2nd June.xlsx`** | Source BOM (`Stock Item → Raw Material`) used to compute recipe signatures. | ⭐ Source | KEEP |
| `all_sku.csv` | A **100-row sample** of the live `all_sku` catalogue (`rm`/`pm`/`fg` only — **no `sfg`**). Proof of F2. | ℹ️ Evidence | KEEP for reference, not for load |
| `build_sfg.py` | **Stale intermediate** generator. Logic is instructive but superseded by the missing `sfg_final.py`. | ⚠️ Stale | **DROP** (notes captured §3) |
| `patch.py` | A one-off `str.replace` patch against `/tmp/sfg_final.py` — **the file it patches is not in the folder.** Dead. | ❌ | **DROP** |

**Net:** of 13 files, **4 are canonical** (FINAL xlsx + 3 FINAL CSVs), **2 are sources** (FG Master, BOM), **1 is evidence** (all_sku sample), and **6 are stale/duplicate** (drop).

---

## 3. How the SFG set was built (logic, captured before discarding the scripts)

`build_sfg.py` documents the derivation. The FINAL CSVs were actually produced by a later `sfg_final.py` (absent) that `patch.py` was meant to fix — but the algorithm is the same in spirit:

1. **Source of truth = `FG_Master_Fill!Process Category`.** Split on `+`.
2. **Token → stage classification** (`canon_stage`):
   - **WIP stage** (becomes an SFG) if the token matches the canonical map: `de-seeding, slicing/dicing/slivering, bar forming, roasting, blending, flavouring, blanching, stuffing, enrobing, chocolate`.
   - **Packaging** (terminal, dropped → produces the FG) for: `packaging, packing, bulk packaging, master carton, monocarton, flow wrap, krugger, x-ray, receiving, weighing`.
   - **Sorting** → in the FINAL output it is **folded into the RM input** (`"RM (sorted in-line)"`), *not* a buffered SFG. (This is the main divergence from the backend — see §6.)
3. **Recipe signature** = the `frozenset` of **non-PM** ingredients from the BOM (a `PM_PAT` regex strips cartons/pouches/labels/jars/etc.). Used to decide when two FGs truly share the same upstream SFG.
4. **Base name** = FG name with **pack-size descriptors stripped** (`100gm`, `1*1`, `48 gms`, ASINs, etc.). This is what makes "5 Grain Cereal 35gm" and "5 Grain Cereal 50 GM" share the base recipe `5 Grain Cereal`.
5. **Dedup key = `(base_name, stage, recipe_signature)`.** Same base + same stage + same recipe ⇒ **one shared SFG** across all pack-size variants. On a recipe clash (same base+stage, *different* recipe) it appends `_v2`, `_v3`…
6. **SFG name** = `{base_name}_SFG_{Stage}` (e.g. `5 Grain Cereal_SFG_Blending`).
7. Each FG's chain row sequence becomes: `RM → SFG_stage1 → SFG_stage2 → … → FG (Packaging)`.

**Result of that pipeline (verified against the FINAL data):**
- 1,086 FG rows → **1,085 unique FGs** (one duplicate name).
- **380 FGs need ≥1 SFG**; **705 are packing-only** (`#WIP_Stages = 0`).
- 509 WIP stage instances → **deduped to 489 unique SFG items.**
- Stage breakdown of the 489 SFGs: `Flavouring 206, Blending 71, Bar-Forming 71, Roasting 69, De-Seeding 23, Chocolate-Coating 15, Stuffing 15, Blanching 9, Slicing 8, Enrobing 2`.
- **Only 18 SFGs are shared by >1 FG** (max 3×, all `Carnival …_SFG_Flavouring`). ⇒ dedup is *correct* but the practical sharing is small; most value is **pack-size collapsing**, not cross-product sharing.
- **33 SFGs carry `_v2/_v3`** recipe-clash suffixes — a naming smell to resolve (§10).

---

## 4. Existing backend reality — the v2 chain already *is* the SFG engine

The production module runs **two generations**. **v1** (`production_schema.sql: job_card`) is legacy. **v2** (migrations `009`–`047`, services `job_card_v2.py`, `job_card_engine.py`, `job_card_batch_v2.py`) is the live, plan-driven, multi-stage model. SFG belongs entirely to v2.

### 4.1 The chain is built from the same column the SFG folder uses

```
FG_Master.Process_Category
   └─(master_ingest.py, split on '+')→ bom_process_route(step_number, process_name, stage)
        └─(plan creation)→ production_plan_step_v2(step_order, process_name, stage, floor)
             └─(plan approval, job_card_engine.py)→ ONE job_card_v2 PER STEP
                  └─ chained via prev_job_card_id / next_job_card_id
```

So **the stage chain already exists** — the SFG folder is a *parallel, offline computation of the very same graph*, plus the item names the backend never assigned.

### 4.2 SFG/WIP is already a load-bearing concept on `job_card_v2`

| Column (`job_card_v2`, mig 017) | Role in the SFG flow |
|---|---|
| `input_kind ∈ ('RM','SFG','WIP')` | Stage 1 = `RM`; downstream stages = `SFG`/`WIP`. |
| `output_kind ∈ ('SFG','WIP','FG')` | Last stage = `FG`; intermediate stages = `SFG`/`WIP`. |
| `step_number` | Position in the chain (mirrors `production_plan_step_v2.step_order`). |
| `process_name` / `stage` | The stage label (e.g. "Roasting"). |
| `prev_job_card_id` / `next_job_card_id` | The chain pointers (self-FK, with anti-self-loop CHECKs from mig 001). |
| `carried_qty_kg` | SFG **received** from the previous stage. |
| `dispatched_to_next_kg` | SFG **sent** to the next stage. |

**Handoff is recorded in `job_card_partial_dispatch_v2`** (`from_job_card_id, to_job_card_id, qty_kg, batch_id`). The receiving stage then gets a synthetic consumption row in **`job_card_material_consumption_v2`** with `input_kind='SFG'`, `material_sku_name='SFG from JC #… (Stage)'`, `bom_line_id = NULL` (by design, mig 024), and `source_dispatch_id` → the dispatch. Output/yield per stage lands in **`job_card_output_v2`** (`output_kind ∈ SFG/WIP/FG`, `process_loss_kg`).

### 4.3 What's missing (the precise gap the SFG folder closes)

1. **No SFG item in any catalogue.** `bom_line.item_type` only allows `'rm'/'pm'`; `all_sku` has no `sfg` rows. ⇒ the WIP between stages is an **anonymous string**, not a referenceable article.
2. **No SFG inventory.** `inventory_batch`/`floor_inventory` *support* `item_type='wip'` but **nothing inserts WIP** — so SFG has no on-hand stock, no FIFO lot, no shelf-life, no traceable batch.
3. **No SFG ↔ stage binding in masters.** The chain knows "stage 2 consumes the previous output", but there is no master statement that *"`{FG} stage Roasting` produces SFG `SFG0123`"*. That binding is exactly the SFG folder's `FG_Jobcard_Chain` + `SFG_Where_Used`.

**Conclusion:** the SFG folder is the **missing master layer** for a chain engine that is otherwise complete.

---

## 5. The reconciliation model (how the two worlds line up)

| Concept | SFG Folder term | Backend (v2) term | Reconciliation |
|---|---|---|---|
| The product being made | `FG_Name` / `Base_Recipe` | `job_card_v2.fg_sku_name`, `bom_header.fg_sku_name` | `Base_Recipe` (pack-size-stripped) ↔ recipe family; the FG SKU ↔ `bom_header`. |
| A transformation step | `Process_Stage` / `Stage_Name` | `production_plan_step_v2.stage` / `job_card_v2.stage` | Same vocabulary needed (§6). |
| The intermediate item | `SFG_Code` / `SFG_Name` | *(none — only `output_kind='SFG'`)* | **The folder supplies the missing item identity.** |
| Step ordering | `Stage_No` | `step_number` / `step_order` | Same integer; **but validate order (§10).** |
| "Where consumed" | `SFG_Where_Used` | `job_card_material_consumption_v2 (input_kind='SFG')` | Master ↔ transactional pairing. |
| Input to a stage | `Input_Material (Required)` | `carried_qty_kg` + RM/PM indents | Stage 1 input = RM/BOM; stage N input = prior SFG. |
| Output of a stage | `Output_Item` / `Output_SFG` | `job_card_output_v2 (output_kind)` | Intermediate=SFG, final=FG. |

---

## 6. Canonical stage taxonomy (the contract both ends must share)

The single most important design decision: **which `Process Category` tokens become a buffered SFG, which are inline, which are terminal.** Today the SFG folder and the backend disagree on *Sorting*. Lock this table:

| Token(s) in `Process Category` | Class | Produces an SFG item? | Job-card representation |
|---|---|---|---|
| `De-Seeding` | **Transform** | ✅ `…_SFG_De-Seeding` | own stage JC, `output_kind=SFG` |
| `Slicing / Dicing / Slivering` | **Transform** | ✅ `…_SFG_Slicing` | own stage JC |
| `Blanching` | **Transform** | ✅ `…_SFG_Blanching` | own stage JC |
| `Roasting` | **Transform** | ✅ `…_SFG_Roasting` | own stage JC |
| `Blending` | **Transform** | ✅ `…_SFG_Blending` | own stage JC |
| `Bar Forming` | **Transform** | ✅ `…_SFG_Bar-Forming` | own stage JC |
| `Flavouring` | **Transform** | ✅ `…_SFG_Flavouring` | own stage JC |
| `Stuffing` | **Transform** | ✅ `…_SFG_Stuffing` | own stage JC |
| `Enrobing` | **Transform** | ✅ `…_SFG_Enrobing` | own stage JC |
| `Chocolate (Coating)` | **Transform** | ✅ `…_SFG_Chocolate-Coating` | own stage JC |
| `Sorting` | **Inline** (folder) ↔ **own step** (backend today) | ⚠️ **DECIDE** | Recommend: **inline** for RPC/bulk (no buffer), **own JC** only where sorted output is physically stocked. Flag both behaviours; do not let it default silently. |
| `Receiving`, `Weighing` | **Inline / non-stage** | ❌ | not a JC stage (or a zero-output prep step). |
| `Packaging`, `Bulk Packaging`, `Master Carton`, `Mono Carton`, `Flow Wrap`, `Krugger`, `X-Ray` | **Terminal** | ❌ (produces the **FG**) | last JC, `output_kind=FG`. |

**Recommendation:** make this table a **seed/config table** (e.g. `stage_catalog(token, canonical_stage, klass IN ('transform','inline','terminal'), produces_sfg bool)`) so `master_ingest` classifies tokens *once*, consistently, instead of the regex living in a throwaway script. The 10 transforms above are the **complete observed set** (every other token is inline or terminal).

---

## 7. Job-card lifecycle scan (open / in-process / semi-closed / closed) + attributes

### 7.1 Real status values (there is no `semi_closed`)

`job_card_v2.status` CHECK (mig 017) and the frontend `STATUS_OPTIONS` (`job-card/page.tsx`):

```
locked → unlocked → assigned → material_received → in_progress → completed → closed
                                                                          └→ cancelled
```

Mapping the **operational vocabulary** the request used onto real states:

| Requested term | Real representation | How to detect it |
|---|---|---|
| **Open** | `locked` / `unlocked` / `assigned` / `material_received` — created, not yet started. (`isLifecycleLocked()` in the FE treats these four as read-only/pre-start.) | `status IN (locked,unlocked,assigned,material_received)` |
| **In process** | `in_progress` | `status='in_progress'`; has ≥1 open batch (`job_card_batch_v2.status='open'`). |
| **Semi-closed** | *No native status.* It is **partial completion**, expressed two ways: (a) some `job_card_batch_v2` rows `closed` while the JC is still `in_progress`; and/or (b) `dispatched_to_next_kg > 0 AND < output` (partial hand-off); and/or (c) `status='completed'` but not yet `closed` (awaiting sign-offs). | `in_progress` with mixed batch states **or** `completed` w/o all `job_card_sign_off_v2` roles. |
| **Closed** | `closed` (all required sign-offs present) | `status='closed'`. |
| **Cancelled** | `cancelled` (soft-delete; `cancellation_reason`, snapshot in mig 043) | `status='cancelled'`. |

> **Implication for SFG:** an SFG batch's availability tracks the *producing* stage's batch closure + dispatch, **not** the whole JC's `closed` status. A stage can hand off SFG (the "semi-closed" feel) long before its JC is `closed`. So SFG inventory should be driven by **`job_card_batch_v2` close + `job_card_partial_dispatch_v2`**, not by `job_card_v2.status='closed'`.

### 7.2 Attributes already captured on a job card (what an SFG must slot into)

A v2 job card already carries, end-to-end: identity (`job_card_id` 8-digit, `job_card_number = PLAN-{plan}-L{line}-S{step}`), plan lineage (`plan_id/plan_line_id/plan_step_id`), product (`fg_sku_name`, `customer_name`, `batch_number`, `planned_qty_kg`, `planned_qty_units`, `uom`), stage (`step_number`, `process_name`, `stage`, **`input_kind`/`output_kind`**), location (`factory ∈ W-202/A-185`, `floor`, `entity ∈ cfpl/cdpl`, `machine_id`), people (`assigned_to_team_leader`, `team_members[]`), time (`start_time/end_time/total_time_min`), **chain** (`prev/next_job_card_id`, `carried_qty_kg`, `dispatched_to_next_kg`), locking (`is_locked`, `locked_reason ∈ awaiting_previous_stage/material_pending`, `force_unlocked`+audit), and the satellite tables: RM/PM indents, consumption, output, byproducts (11 categories), balance material, additives, accounting (conservation identity + `is_balanced`), QC + 5 annexures, sign-offs, batches/phases. **No new job-card attribute is required to host SFG** — only the *item identity* of what flows on `input_kind/output_kind`.

---

## 8. The NEW SFG set — canonical schema, naming & codes

Keep the folder's 489-item set as the **seed**, but normalise it into a catalogue that matches backend conventions.

### 8.1 Canonical SFG item record (proposed master shape)

Aligns with `all_sku` + `bom_header` conventions so it ingests through the same path:

| Field | Source in folder | Notes / rule |
|---|---|---|
| `sfg_code` (**PK / business key**) | `SFG_Code` (`SFG0001`…) | **Stable, sequential, the join key everywhere.** Never key on the long name. |
| `item_type = 'sfg'` | `Item_Type` | New value for `all_sku.item_type`; **does not** need to be added to `bom_line` unless SFG becomes a BOM input (see §9.4). |
| `particulars` (display name) | `SFG_Name` | `{Base_Recipe}_SFG_{Stage}`. Treat as description, not key. |
| `base_recipe` | `Base_Recipe` | pack-size-stripped family; ties pack variants together. |
| `process_stage` + `stage_no` | `Process_Stage`,`Stage_No` | from the §6 taxonomy. |
| `item_group` / `sub_group` | `Item_Group`,`Sub_Group` | inherited from the FG. |
| `uom = 'kg'` | `UOM` | all 489 are `kg` (consistent). |
| `sale_group = 'wip'` | `sale_group` | matches `inventory_batch`/`floor_inventory` `item_type='wip'`. |
| `gst` | `GST_Rate` | inherited (intra-factory SFG is non-saleable; GST is informational). |
| `va_article`, `primary_bu` | `VA_Article`,`Primary_BU` | 231 are VA articles; `Primary_BU` (e.g. `VA- Bar Line`, `Dates RPC`, `VA - Nuts & Mixes`, `RPC`) routes the floor. |
| `min_shelf_life_days` | `min_shelf_life_days` | currently `0` — **set realistic WIP shelf life before go-live** (a roasted/blended intermediate is perishable). |
| `batch_strategy = 'FIFO'` | — | matches RM/FG FIFO. |
| `produced_by_stage_ref` *(new)* | derive from `FG_Jobcard_Chain` | link SFG ↔ the (FG, stage) that produces it. |

### 8.2 Naming convention (fix the smell)

- **Key:** `SFG####` only.
- **Display:** `{Base_Recipe} · {Stage}` (e.g. `5 Grain Cereal · Blending`). Cap length; strip marketing fluff that leaked into `Base_Recipe` (e.g. *"…, Flavourful & Crispy"*, *"…200gm-style"*).
- **Recipe variants:** the 33 `_v2/_v3` items mean *same base + same stage but a genuinely different recipe signature.* Replace the opaque `_vN` with an explicit `recipe_variant` column (`A/B/C`) and keep them as distinct `SFG####` — **do not merge** (they are physically different intermediates).

### 8.3 What to keep vs drop from the 489

- **Keep all 489** as candidates — they are the deduped WIP universe.
- **Re-examine** the **18 cross-FG-shared** ones (real sharing; confirm the recipe truly matches before treating one batch as fungible across products).
- **Quarantine** any whose `Base_Recipe` still contains pack tokens or marketing text (naming hygiene), and the `_vN` set (recipe-variant review).
- **Out of scope (no SFG):** the **705 packing-only FGs** — they stay single-stage `RM/components → FG (Packaging)` job cards. Don't manufacture SFGs for them.

---

## 9. Wiring plan — linking the stage chain across modules

How an SFG flows, end to end, reusing what exists. **No new chain engine; only an item layer + inventory materialisation.**

### 9.1 Master ingest (one new, idempotent step)

Add an `ingest_sfg_master` alongside `master_ingest.ingest_fg_master` that loads `SFG_Item_Master` into `all_sku` (`item_type='sfg'`) — same `ON CONFLICT DO NOTHING` idempotency as the existing ingests. Classify `Process Category` tokens via the §6 `stage_catalog` instead of an ad-hoc regex.

### 9.2 Bind SFG to the stage (the new master link)

Load `FG_Jobcard_Chain` into a binding such that each `bom_process_route` / `production_plan_step_v2` transform step **names its output SFG** (`produces_sfg_code`) and each downstream step **names its input SFG** (`consumes_sfg_code`). This turns today's anonymous `"SFG from JC #…"` into `SFG####`.

### 9.3 Runtime flow (unchanged mechanics, now itemised)

```
Stage N (transform) closes a batch  (job_card_batch_v2.close)
   ├─ job_card_output_v2: output_kind='SFG', qty, yield, process_loss_kg
   ├─ (NEW) INSERT inventory_batch(item_type='wip', sku_name=SFG####,
   │         source='PRODUCTION', job_card_id=N, lot=…, status=IN_TRANSIT/AVAILABLE)
   ├─ job_card_partial_dispatch_v2(from=N, to=N+1, qty_kg, batch_id)
   ├─ job_card_v2[N].dispatched_to_next_kg += qty
   └─ job_card_v2[N+1].carried_qty_kg += qty ; unlock N+1
Stage N+1 consumes
   ├─ job_card_material_consumption_v2(input_kind='SFG',
   │         material_sku_name=SFG#### , source_dispatch_id=…)   ← now a real code
   └─ (NEW) decrement the SFG inventory_batch; floor_movement audit row
```

Everything except the two **(NEW)** lines already runs today (`job_card_batch_v2.py` close → auto-dispatch → synthetic consumption). The additions only *materialise inventory* and *swap the string for `SFG####`.*

### 9.4 BOM question (explicit decision needed)

Today `bom_line.item_type ∈ {'rm','pm'}` and SFG consumption is **flow-driven** (via dispatch), not BOM-driven (`bom_line_id=NULL` by design, mig 024). **Two options:**
- **(A) Keep flow-driven** (recommended, least change): SFG never appears as a `bom_line`; the chain + dispatch remain the source of truth. The SFG catalogue is for inventory/traceability/reporting.
- **(B) Make SFG a BOM input:** add `'sfg'` to the `bom_line.item_type` CHECK and let stage N+1's BOM literally list `SFG####`. More "correct" for costing/MRP, but touches the BOM contract and master ingest. Defer unless costing demands it.

### 9.5 Frontend (read-only surfacing first)

The FE already renders the chain (`StageChainTab`, `ChainStep.input_kind/output_kind`, `carried_qty_kg`, `dispatched_to_next_kg`). Minimal lift: **show `SFG####`/name** where it currently shows `RM → SFG`, and add an **SFG inventory lookup** (mirror the RM/PM indent picker, sourced from `inventory_batch where item_type='wip'`). `item_type` in the FE BomLine type would extend to `'RM'|'PM'|'SFG'` only if option (B) is chosen.

---

## 10. Data-quality issues to resolve before any load

1. **Stage order** — ordering follows whatever Ops typed in `Process Category`. Confirmed offender: `Barbeque Cashew Bulk = "Flavouring + Roasting + Bulk Packaging"` (flavour-before-roast is backwards; 127 FGs have ≥2 transform stages, so this matters). **Add a canonical stage-precedence check** (e.g. Roasting precedes Flavouring; Blending precedes Bar-Forming) and report violations.
2. **`Sorting` policy** — pick inline vs own-stage per BU (§6) and apply it uniformly; the folder and backend currently disagree.
3. **`_v2/_v3` recipe variants (33)** — replace with explicit `recipe_variant`; verify each is a genuine recipe difference, not a base-name normalisation artdefact.
4. **Marketing text in `Base_Recipe`** — strip (names up to 76 chars; e.g. *"Tasties Coated Peanuts - Tandoori Masala, Flavourful & Crispy"*).
5. **Shelf life = 0** for all SFGs — set real WIP shelf life (perishability gates FIFO/expiry).
6. **Duplicate FG name** — 1,086 rows → 1,085 unique FG names; dedupe the one collision.
7. **`84 Bar Line Process` rows** — only 84 FGs have the richer `Bar Line Process` filled (all bar-line VA). Decide whether `Bar Line Process` *overrides* `Process Category` for those (it is more detailed: `Receiving + Sorting + Weighing + Roasting…`). Currently neither the folder nor `master_ingest` uses it; mig 031 only *stores* `bom_header.bar_line_process` as raw text.

---

## 11. Keep / drop / rename — consolidated

**Files (folder):** keep `SFG_WIP_Item_List_FINAL.xlsx` + 3 `*_FINAL.csv` + the two source xlsx; drop `build_sfg.py`, `patch.py`, `SFG_WIP_Item_List.xlsx`, and the 3 non-FINAL CSVs (duplicates); keep `all_sku.csv` as evidence only.

**SFG items:** keep all 489 as the seed; **re-key to `SFG####`**; rename display to `{Base_Recipe} · {Stage}`; resolve the 33 `_vN`; out-of-scope = the 705 packing-only FGs.

**Schema:** **no chain redesign.** Add (a) `all_sku` rows `item_type='sfg'`, (b) a `stage_catalog`, (c) an SFG↔stage binding, (d) WIP `inventory_batch` materialisation. Optional/deferred: add `'sfg'` to `bom_line.item_type` (option B), `Bar Line Process` override.

**Status:** do **not** add `semi_closed`; express it via batch closure + partial dispatch (§7).

---

## 12. Appendix — worked examples (from the FINAL data)

**A. Multi-stage VA bar (2 transforms, shared across pack sizes)**
```
FG: "5 Grain Cereal 35gm"  &  "5 Grain Cereal 50 GM"   (Base_Recipe = "5 Grain Cereal")
 Process Category: Sorting + Blending + Bar Forming + Packaging   (35gm)
                   Roasting + Packaging                            (50 GM)
 Chain (35gm):
   RM (sorted in-line) → SFG: 5 Grain Cereal_SFG_Blending (SFG0003)
                       → SFG: 5 Grain Cereal_SFG_Bar-Forming (SFG0004)
                       → FG : 5 Grain Cereal 35gm  (Packaging)
 Note: the 50 GM variant uses a different stage (Roasting → SFG0005); same Base_Recipe, different SFG.
```

**B. Dates de-seeding (1 transform, pack variants collapse)**
```
Base_Recipe: "Al Barakah Dates Oman Seedless"
 SFG: Al Barakah Dates Oman Seedless_SFG_De-Seeding (SFG0006)
 Consumed by FG: "Al Barakah Dates Oman Seedless 1kg"  (and any other pack size of the same recipe)
```

**C. Cross-FG shared SFG (the 18 real-sharing cases)**
```
SFG: Carnival Cheese Cashew_SFG_Flavouring   → used by 3 distinct FG pack/customer variants
 ⇒ one physical flavoured-cashew intermediate batch can fulfil 3 FGs (verify recipe identity first).
```

**D. Packing-only FG (no SFG — 705 of these)**
```
FG: "3 Jar Jute Bag Gift Hamper"
 Process Category: Flavouring + Packaging  → classified assembly/packing-only ⇒ #SFG_Stages = 0
 Chain: RM / components per BOM → FG (Packaging).   No SFG item created.
```

---

### Cross-reference index (where to verify each claim in code)
- SFG chain columns & CHECKs: `backend/app/db/017_job_card_v2.sql`, `001_job_card_chain.sql`
- Consumption `input_kind='SFG'`, `source_dispatch_id`, `bom_line_id NULL`: `018_jc_accounting_v2.sql`, `024_consumption_bom_line_id.sql`, `services/jc_accounting_v2.py`
- Batch close → auto-dispatch → synthetic SFG consumption: `services/job_card_batch_v2.py`, `029_jc_phase_v2.sql`, `036_jc_batch_compat_shim.sql`
- WIP-capable inventory (unused): `production_schema.sql` (`inventory_batch`, `floor_inventory`, `floor_movement`)
- `bom_line.item_type ∈ rm/pm` only: `production_schema.sql`
- `Process Category → bom_process_route` ingest: `services/master_ingest.py`, `031_bom_bar_line_process.sql`
- Statuses & lifecycle: `017_job_card_v2.sql`, `services/job_card_v2.py`, `frontend/src/app/modules/job-card/page.tsx` & `[id]/page.tsx`
