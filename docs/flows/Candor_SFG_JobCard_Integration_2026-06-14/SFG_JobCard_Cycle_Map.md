# CANDOR FOODS — SFG ↔ Job-Card Cycle Map (v2: ground-practical)

> **Companion to** [`SFG_DESIGN_REFERENCE.md`](SFG_DESIGN_REFERENCE.md) and [`Article_Reconciliation_Loop.md`](Article_Reconciliation_Loop.md). This is the **operational** mapping of SFG/WIP onto a two-stage job-card cycle, rewritten for **ground reality**: (1) **roasting is performed together with flavouring/salting** (one operation, even though the Process Category column lists them separately), and (2) **bulk articles already wired as SFGs inside parent BOMs are reused** — not duplicated.
>
> **Status:** Reference / design only. **No DB or app code touched.** "Past-months" reality is read from the repo snapshot `backend/scratch/_job_card_report_todate.pdf` (`warehouse_db`, 2026-06-11).
>
> **Supersedes** the v1 365-derived-WIP set. Built by overlaying two corrections on the curated v1 paths.

---

## 0. One-paragraph answer

A job card has **two operating stages**. **Stage 1 — Create WIP** runs the recipe's transformation as **practical combined operations** — most often a single *Roast & Flavour/Salt* (roasting, salting and flavouring are one operation on the floor), or *Blend & Form* for bars — and produces **one SFG**. **Stage 2 — Final FG (Packing)** consumes that SFG as its **opening "RM"** and packs it. Crucially, **where a bulk article already exists in the parent BOM** (e.g. `Roasted & Salted Cashew Bulk`, consumed by all its retail packs), that real article **is** the SFG — the cycle reuses it instead of minting a duplicate WIP. *Sorting* is never its own SFG stage: it is inline at intake or folded into packing. The result is **343 canonical SFGs** (down from 365), **25 of them existing bulk articles reused**, **66 retail packs wired to consume an existing SFG**, and **304 of 343 collapsed to a single practical operation**.

---

## 1. The two ground-reality corrections (what changed vs v1)

**Correction A — practical combined operations.** The Process Category column over-splits the floor reality. These tokens are canonicalised to one operation:

| Process Category writes… | …but on the floor it is ONE operation | practical operation |
|---|---|---|
| `Flavouring` and/or `Roasting` (+ salting) | seasoned nuts/seeds are **roasted and flavoured/salted together** | **Roast & Flavour/Salt** |
| `Roasting` only (no seasoning) | plain roast (e.g. roasted seeds) | **Roasting** |
| `Blending` + `Bar Forming` | the bar line blends then forms in one flow | **Blend & Form** |
| `Blanching` + `Slicing/Dicing/Slivering` | almond line blanches and slices together | **Blanch & Slice** |
| `Enrobing` / `Chocolate` | one coating operation | **Enrobe / Choco-Coat** |
| `De-Seeding`, `Stuffing` | dates | **De-Seeding**, **Stuffing** (may chain) |
| `Sorting` | **inline** — done at RM intake or with packing | *(not an SFG stage)* |
| `Packaging` / `Bulk Packaging` / `Master Carton` / … | terminal | *(Final FG)* |

Effect: **304 of 343** SFGs now have a **single** operation (was a 2–3 stage chain in v1). Top operations: `Roast & Flavour/Salt` **182**, `Blend & Form` **66**, `Roasting` **39**, `Roasting + Enrobe/Choco-Coat` **13**, `De-Seeding + Stuffing` **13**.

**Correction B — existing-first SFG reuse.** Many parent BOMs already carry a real bulk/intermediate article as their SFG. Verified pattern:

```
Roasted & Salted Cashew Bulk   proc = Flavouring + Roasting + Bulk Packaging   BOM = cashew + salt
   └─ IS the SFG  (Create WIP = Roast & Flavour/Salt)
Roasted & Salted Cashew 75/90/140g   proc = Sorting + Packaging   BOM = Roasted & Salted Cashew Bulk + PM
   └─ Final FG, opening RM = the bulk SFG    (no new article)
```

So the bulk article **is** the canonical SFG; v1 had also minted `Roasted & Salted Cashew · WIP …` for the same thing — a duplicate now removed. **25 existing bulk articles are reused** as SFGs and **66 retail packs are wired to consume them.**

> **Reuse guard (correctness):** reuse only applies when a product **is** the bulk (name match) or is a **pure pack of exactly one bulk**. A product that runs its **own** transform (e.g. a date bar = *Blend & Form*) keeps its own SFG even if a bulk/kernel appears in its BOM as an *ingredient*. 17 brand-line WIPs whose BOM makes from raw (e.g. `Carnival Roasted & Salted Cashew`) are kept separate but **flagged** `possible_existing_match → Roasted & Salted Cashew Bulk` for a consolidation decision.

---

## 2. The two-stage cycle and four archetypes

```
 A  WIP producer (a bulk SFG)     RM ─► [Create WIP: Roast & Flavour/Salt] ─► SFG (the bulk)          14 FGs
 B  In-line transform → pack      RM ─► [Create WIP: <op>] ─► SFG ─► [Final FG: Packing] ─► FG        361 FGs
 C  Pack of an existing SFG       existing bulk SFG ─► [Final FG: Packing] ─► FG  (no new article)      66 FGs
 D  Pack-only (no transform)      RM ─► [Final FG: Sorting + Packing] ─► FG                            644 FGs
```

- **Stage 1 — Create WIP** (A, B): `input_kind=RM`; floor manager runs the practical operation(s) and **combines intermingled processes into one accounting unit**; `output_kind=SFG`; closed here; `gross − losses = net SFG` dispatched forward.
- **Stage 2 — Final FG (Packing)** (B, C, D): the SFG (or RM for D) is the opening input; process = `Packing` or `Sorting + Packing`; `output_kind=FG`; consumes PM.

---

## 3. Where the SFG sits + backend wiring (unchanged hooks)

The SFG is the **seam**: output of Stage 1 = opening input of Stage 2.

| Cycle element | Existing v2 backend hook |
|---|---|
| 2-link chain | `job_card_v2.prev/next_job_card_id`, `step_number` (now **2** steps, not N) |
| SFG out / in | `output_kind='SFG'` (S1) → `input_kind='SFG'` (S2) |
| Net handoff | `dispatched_to_next_kg` → `carried_qty_kg`; `job_card_partial_dispatch_v2` |
| SFG consumption | `job_card_material_consumption_v2 (input_kind='SFG', source_dispatch_id)` — now names a real `sfg_code`/bulk article |
| SFG as catalogued item | `all_sku` + new `item_type='sfg'` (**all 343 need this**; 25 are existing articles to re-type, 318 new) |
| SFG stock / FIFO | materialise `inventory_batch (item_type='wip')` on S1 close (slot already exists) |
| Losses | `job_card_accounting_v2` conservation identity + `job_card_byproducts_v2` |
| **Floor-manager op selection** | new: a checklist on the Create-WIP card listing the recipe's practical operations to enable/combine |

### 3a. Endpoint audit — verified against `backend/app/modules/production/router.py` (2026-06-14)

| Step | Real endpoint / trigger | ✓ |
|---|---|---|
| Load SFG catalogue + sfg BOM lines | `master_ingest.py` **service** (`ingest_fg_master` / `ingest_bom_lines`) — **not** an HTTP route | ✓ |
| Create the 2-stage chain | **`POST /plans-v2/{plan_id}/approve`** → `create_job_cards_from_plan` (plan_v2.py:572) — *not* a `generate` route | ✓ |
| List / detail / chain | `GET /job-cards-v2` (4886) · `/{id}` (5032) · `/{id}/chain` (5062) | ✓ |
| Run & hand off the SFG | `PUT /{id}/start` (6469) · `POST /{id}/batches/open` (6046) · `/batches/{bid}/close` (6076) · `/{id}/dispatch-to-next` (5451) | ✓ |
| Record SFG/FG + losses | `POST /{id}/outputs` (5590) · `GET /{id}/accounting` (5328) · `PUT /{id}/accounting/summary` (5410) | ✓ |
| QC + close | `POST /{id}/sign-off` (6311) · `PUT /{id}/complete` (6508) · `PUT /{id}/close` (6687) | ✓ |

> All paths sit under `/api/v1/production/…` and are consumed by the frontend job-card module (`frontend/src/app/modules/job-card/[id]/page.tsx`, verified). Two earlier draft labels (`/job-cards-v2/generate`, `/master/ingest`) were **corrected** — they do not exist: chain creation is plan-approval-driven, and master load is a service. No backend column referenced in §3 is fictional — all exist in the v2 migrations.

---

## 4. Lifecycle mapping (open / in-process / semi-closed / closed)

Status enum unchanged. From the past-months snapshot (543 JCs): `locked 169, unlocked 122, in_progress 80, completed 146, closed 16, assigned/material_received/cancelled` few; **59 "filled-but-stuck"** = your **semi-closed** (output recorded, blocked by the R9 balance/open-batch gate).

| Your term | Representation |
|---|---|
| **Open** | `locked / unlocked / assigned / material_received` |
| **In process** | `in_progress` (open batch) |
| **Semi-closed** | S1 closed & SFG dispatched while S2 open; **or** the filled-but-stuck population |
| **Closed** | `closed` (all sign-offs) |

> Collapsing transforms into **one** Create-WIP operation removes the inter-stage seams that strand cards in "filled-but-stuck", so the semi-closed backlog should shrink.

---

## 5. Headline counts (v2)

| Metric | Value |
|---|---|
| **Canonical SFG items** | **343** (was 365) |
| — existing bulk articles **reused** | **25** |
| — derived in-line WIP (new) | 318 |
| — flagged `possible_existing_match` (review) | 17 |
| SFG with a **single** practical operation | **304** / 343 |
| Retail packs **wired to an existing SFG** | 66 |
| FGs — A WIP producer / B in-line / C pack-of-SFG / D pack-only | 14 / 361 / 66 / 644 |
| SFGs needing `item_type='sfg'` catalogued | all 343 |

---

## 6. Files (cycle order)

| File | Role |
|---|---|
| **`SFG_JobCard_Cycle_Map.md`** | this document |
| **`JC_Stage1_Create_WIP_Master.xlsx`** | workbook: `SFG_Master` + `SFG_Resolution_Map` + `Final_FG_Stage` + `FG_Cycle_Chain` + **`Practical_Op_Model`** + `Stats` |
| **`JC_Stage1_Create_WIP_Master.csv`** | the 343 canonical SFGs (origin, operation, reuse, possible-dup flag) |
| **`JC_Stage2_Final_FG_Packing.csv`** | per-FG Stage-2 (archetype, input = SFG or RM) |
| **`FG_JobCard_Cycle_Chain.csv`** | full new-model cycle per FG |
| **`SFG_Resolution_Map.csv`** | how each recipe group resolved (existing reuse vs derived) |

---

## 7. Worked examples (from the generated files)

```
EXISTING reused (Archetype A + C):
  SFG: Roasted & Salted Cashew Bulk   op = Roast & Flavour/Salt   consumed by 11 retail packs
     S1 Create WIP (the bulk)         RM (cashew+salt) → Roasted & Salted Cashew Bulk
     S2 Final FG  (75/90/140g, …)     Roasted & Salted Cashew Bulk → FG     (no new article)

DERIVED, single combined op (Archetype B):
  SFG: <Carnival Roasted & Salted Cashew · WIP · RoastFlav>   op = Roast & Flavour/Salt
     flagged possible_existing_match → Roasted & Salted Cashew Bulk   (brand line makes from raw)

DERIVED bar (own op, not borrowing an ingredient):
  Rhine Valley Fruit & Nut Date Bar 50gm → op = Blend & Form   (almond slices are an INGREDIENT, not the SFG)

PACK-ONLY (Archetype D):
  King Solomon Medjoul Dates 500 Gms → Final FG: Sorting + Packing   (RM → FG, no SFG)
```

---

## 8. Open items / caveats

1. **Practical-op combining is a model assumption** beyond the two you confirmed (Roast+Flavour/Salt, Sorting inline): I also combined `Blend+Bar → Blend & Form`, `Blanch+Slice → Blanch & Slice`, `Enrobe/Choco`. The `Practical_Op_Model` sheet lists every rule — veto any and I'll re-run.
2. **17 flagged possible duplicates** (brand lines that make from raw vs a generic bulk) need a business call: consolidate onto the bulk, or keep the separate line.
3. **Two-level intermediates:** kernels/tukda (graded raw) sit *upstream* of the roast&flavour SFG; they are treated as ingredients, not the product's own SFG. Mixes/assemblies (multi-component packs) are Pack-only/assembly, not single-SFG.
4. **`suggested_shelf_life_days`** is a per-operation placeholder — confirm before go-live.
5. Numbers reflect the repo's BOM ("2nd June") + FG Master; re-run `SFG_Folder_Extracted/_gen_cycle_v2.py` against fresher exports to refresh.
