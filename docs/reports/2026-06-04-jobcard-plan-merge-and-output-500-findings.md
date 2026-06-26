# Job-Card "merged plans" + live output-500 — findings & plan

**Date:** 2026-06-04
**Author:** investigation for the "different plans merged in a single SO" report
**Status:** findings only — no code changed yet, awaiting approval
**Subject plan:** `production_plan_v2.plan_id = 58795071` · W-202 · internal SO `CF-SO/26-27/326` · deadline 2026-06-08

---

## 0. Executive summary

The screenshot ("PLAN #58795071 · 33 stages") is **one daily plan that bundles 15 different
finished-good products**, each as its own plan-line + `Sorting→Packaging` chain, all sharing the
same deadline and one internal SO. The operator sees 15 products' stages interleaved as a flat list
and can't follow any single product's sequence.

Investigation surfaced **three distinct issues**, two of which are independent of the UX complaint:

| # | Issue | Severity | Layer |
|---|-------|----------|-------|
| A | `POST /job-cards-v2/{id}/outputs` returns **500 for every call** — missing unique index | 🔴 Live, blocking | DB / migrations |
| B | One plan-line has its stage order **inverted** (`Packaging` before `Sorting`) | 🟠 Functional | Plan-step snapshot |
| C | A daily plan bundles 15 products into one flat stage list (the original report) | 🟡 UX / model | Frontend grouping + optional plan-split |

**Decision recorded:** for Issue C, do **both** — frontend per-product grouping now (fast relief),
backend plan-splitter as the durable fix.

**Recommended sequence:** A (unblock production) → B (cheap, isolated correctness) → C-group → C-split.

### Correction to the initial theory
The first pass assumed the 33 stages were *one SKU duplicated* (driven by the screenshot title) and
pointed at SO re-upload reconciliation creating duplicate same-SKU lines. **The data disproves that.**
The 15 lines are distinct SKUs, all with the same `deadline_date`. Therefore:
- The "reconciler duplicate-SKU" fix is **not relevant to this plan**.
- "Split per `deadline_date`" would **not** split anything (all share 2026-06-08).
- The meaningful split unit is **per product / per plan-line**.

---

## 1. Evidence — what plan 58795071 actually contains

Query run against prod (`production_plan_line_v2` ⋈ `production_plan_step_v2`, plan_id = 58795071):

| qty_kg | fg_sku_name | deadline | steps (in step_order) |
|--------|-------------|----------|------------------------|
| 2610.0 | Popular Raisins, 500 g (B07GR3S1RR) | 2026-06-08 | Sorting, Packaging |
| 2256.0 | Vedaka Fard Dates (Omani) 500g×2 | 2026-06-08 | Sorting, Packaging |
| 2220.0 | Vedaka Popular Raisins, 1 kg | 2026-06-08 | Sorting, Packaging |
| 708.0  | Solimo Dates 500g | 2026-06-08 | Sorting, Packaging |
| 600.0  | Solimo Fard Dates 500gm | 2026-06-08 | Sorting, Packaging |
| 160.0  | Raisins 200G (B07GRBFFMR) FBA | 2026-06-08 | Sorting, Packaging |
| 144.0  | Vedaka Almonds/Raisins/Cashews/Pista 4×200g | 2026-06-08 | Sorting, Packaging |
| 72.0   | Vedaka Dry Dates powder 250gm | 2026-06-08 | **Packaging** (single step) |
| 52.0   | Vedaka Premium Dry Fruits Combo 400g Gift Box | 2026-06-08 | Sorting, Roasting, Packaging |
| 47.8   | Vedaka Dryfruits & Berries Mix 200g | 2026-06-08 | Sorting, Roasting, Packaging |
| 24.0   | Vedaka Popular Raisins, 100 g | 2026-06-08 | Sorting, Packaging |
| 9.6    | Vedaka Healthy Seed Mix 200gm | 2026-06-08 | **Flavouring**, Packaging |
| 9.0    | Solimo Premium Pine Nuts, 250g | 2026-06-08 | **Packaging, Sorting** ← INVERTED |
| 4.8    | Vedaka Dryfruits & Nut Mix 200g | 2026-06-08 | Sorting, Roasting, Packaging |
| 4.8    | Nuts Berries & Seed Mix 200g | 2026-06-08 | Sorting, Roasting, Packaging |

15 lines, 15 distinct SKUs, mixed route shapes (2–3 steps), one shared deadline. This is a normal
daily plan — not corrupt data.

---

## 2. Issue A — live 500 on output recording (🔴 fix first)

### Symptom
```
POST /api/v1/production/job-cards-v2/54196018/outputs → 500
asyncpg.exceptions.InvalidColumnReferenceError:
  there is no unique or exclusion constraint matching the ON CONFLICT specification
```
Repeating in prod logs (2026-06-04 08:50–08:51). Operators cannot record consumption/output.

### Root cause (confirmed by code)
- `upsert_consumption_lines` issues `ON CONFLICT (job_card_id, material_sku_name)`
  — `app/modules/production/services/job_card_v2.py:451`.
- That requires the unique index `uq_jcmc_v2_jc_material`, created in
  `app/db/018_jc_accounting_v2.sql:72-73`.
- The index is **absent on production**. Why:
  1. `scripts/migrate.py` `SQL_FILES` (lines 28–63) jumps `001_job_card_chain.sql` → `030_vendor_history.sql`
     — migrations **016–029 are not in the runner** and aren't folded into `production_schema.sql`
     / `production_migrate.sql` either (grep: no hits for the table in any aggregate file).
  2. So 018 was applied manually. `CREATE UNIQUE INDEX IF NOT EXISTS` fails if duplicate
     `(job_card_id, material_sku_name)` rows already exist, and `IF NOT EXISTS` means it is never retried
     → index silently missing.

### Fix plan
1. **Corrective migration** `app/db/039_fix_jcmc_unique_index.sql` (idempotent):
   - Collapse any duplicate `(job_card_id, material_sku_name)` rows — keep the most-recent
     `recorded_at`, sum/!choose consumed qty per business rule (needs confirmation: sum vs keep-latest),
     delete the rest.
   - `CREATE UNIQUE INDEX IF NOT EXISTS uq_jcmc_v2_jc_material ON job_card_material_consumption_v2(job_card_id, material_sku_name);`
   - Verification SELECTs (index present; zero dup groups).
2. **Stop the recurrence:** add the orphaned migrations (016–029, at minimum 016/017/018/022/023/024)
   to `scripts/migrate.py` `SQL_FILES` in dependency order, *and/or* switch the runner to a tracked,
   sorted-glob applier with a `schema_migrations` ledger so a hand-applied file can't drift again.
3. **Defensive option (smaller blast radius):** make `upsert_consumption_lines` degrade to a
   SELECT-then-INSERT/UPDATE if the constraint is missing — but this is a band-aid; the index is the
   real fix. Recommend index-first, not this.

### Risks
- Dedup is data-mutating. Must run inside a transaction with a pre-count and a dry-run SELECT
  reviewed before delete. Need the business rule for merging duplicate consumption qty.
- Adding orphaned migrations to the runner means they'll execute on next deploy — they're `IF NOT EXISTS`,
  but each should be re-read to confirm idempotency before wiring in.

### Verification
- Re-run the failing call (or a scripted equivalent) → 200.
- `SELECT indexname FROM pg_indexes WHERE indexname='uq_jcmc_v2_jc_material';` returns the row.

---

## 3. Issue B — inverted stage order on one plan-line (🟠)

### Symptom
Screenshot stages 9–10 and the query both show `Solimo Premium Pine Nuts, 250g` with
`Packaging` at step_order 1 (Unlocked) and `Sorting` at step_order 2 (Locked) — every other
2-step line is `Sorting, Packaging`. Lock state is derived purely from position
(`job_card_v2.py:666-678`: step 1 → unlocked, steps 2+ → locked), so the inversion makes the
operator pack before sorting.

### Root cause
Steps are snapshotted in `create_plan` from either the client-supplied `steps[]` (preserved in given
order) or `bom_process_route` ordered by `step_number` — `app/modules/production/services/plan_v2.py:179-210`.
For this line the snapshot landed reversed: either the planning client posted the steps reversed, or
this SKU's `bom_process_route` has `Packaging.step_number < Sorting.step_number`. **Need to check the
BOM route for the Pine Nuts SKU to know which.**

### Fix plan
1. **Diagnose source:** inspect `bom_process_route` for the Pine Nuts BOM. If the route itself is
   reversed → data fix on the route (and likely other SKUs share it). If the route is correct → the
   planning client sent steps out of order.
2. **Data repair:** for the affected plan-line(s), swap `step_order` and rebuild the JC chain's
   `is_locked` / `status` / `prev/next_job_card_id` so step 1 = first process. Must respect any stage
   already started (don't unlock/relock a JC mid-run without operator awareness).
3. **Guard (prevent recurrence):** in `create_plan`, validate the snapshot against a canonical stage
   ordering (e.g. Sorting → Roasting/Flavouring → Packaging) or against `bom_process_route.step_number`,
   and reject/normalize an out-of-order `steps[]`. Optionally a DB `CHECK`/trigger that the last step is
   a packaging-class stage.

### Risks
- Reordering steps on an **already-approved** plan touches live job cards (locks, indents materialised
  on "first" stage). If the wrong stage was treated as first, its indents may be misplaced — repair must
  re-evaluate `_materialise_indents` for the corrected first stage.
- A blanket "Sorting must be first" rule is wrong for legit shapes (`{Packaging}` only; `{Flavouring,Packaging}`).
  The guard must encode the real allowed orderings, not a naive rule.

---

## 4. Issue C — 15 products in one daily plan (🟡, original report)

Decision: **both** — group now, split later.

### 4a. Group now (frontend; fast relief)
The backend already returns everything needed to group without interleaving:
`list_job_cards_v2` rows carry `plan_id`, `plan_line_id`, `step_number`, `process_name`,
`fg_sku_name`, `status` (`app/modules/production/router.py:4774`+).

- **Likely zero backend change.** The job-cards view should group by `plan_line_id`
  (one collapsible section per product), ordered by `step_number` within each, instead of a flat list.
- Optional backend enrichment if the frontend wants server-side grouping: add a `group_by=plan_line`
  response shape or a per-line summary (product name, total kg, stage count, completion). Cheap, additive.
- **This is the lowest-risk lever and addresses the stated confusion directly.** It does not fragment
  the planning model.

### 4b. Split into separate plans (backend; durable)
Split unit = **per plan-line (per product)**: each line + its steps becomes its own
`production_plan_v2`, moving the line's `job_card_v2` rows (`plan_id`) with it.

Two very different cases:
- **Draft plan (not yet approved):** clean. Create N new plan headers, repoint plan-lines + steps,
  done. No MRP/indents to reconcile.
- **Approved plan (this one — JCs already exist):** harder. MRP + draft indents are derived per plan
  (`approve_plan` → `run_mrp(plan_id)` → `generate_draft_indents`, `mcp_planner.py:547`+), and JC indents
  are materialised on the first stage (`create_job_cards_from_plan` → `_materialise_indents`,
  `job_card_v2.py:743`). A post-approval split must repoint the existing JCs and re-attribute / re-derive
  MRP + indents per new plan, and recompute the SO-fulfillment `planned_qty` bookkeeping
  (`plan_v2.create_plan` bumps `so_fulfillment_v2.planned_qty_*`).

Proposed API: `POST /plans-v2/{plan_id}/split` with a mode:
- `mode=per_line` (default) — one plan per plan-line.
- `mode=by_key` — group lines by a provided key (sku / customer / floor) for coarser splits.
Behaviour gated on plan status: draft = pure repoint; approved = repoint + MRP/indent re-derivation
(or refuse with a clear error if re-derivation is out of scope for v1).

### Risks
- Splitting an approved plan is genuinely complex; safest v1 is **split only draft plans** + the
  frontend grouping for already-approved ones. Confirm whether approved-plan split is in scope.
- Per-product plans multiply plan rows and indent docs — confirm downstream consumers (reports,
  fulfillment sync, day-end) tolerate many small plans per SO/day.

---

## 5. Open questions (block parts of the work)
1. **Issue A dedup rule:** when collapsing duplicate consumption rows, sum the consumed qty or keep
   the latest? (Need before writing the corrective migration's delete.)
2. **Issue A runner:** approve adding orphaned migrations 016–029 to `scripts/migrate.py` (and/or moving
   to a tracked sorted-glob runner)?
3. **Issue B source:** is the Pine Nuts `bom_process_route` itself reversed (route data fix, possibly
   wider) or did the planning client post reversed steps (guard at `create_plan`)?
4. **Issue C split scope:** v1 = draft plans only, or must we support splitting already-approved plans
   (with MRP/indent re-derivation)?

## 6. Proposed sequencing
1. **A** — corrective migration + runner fix → restore output recording (unblocks prod today).
2. **B** — diagnose route source, repair the inverted line, add the snapshot guard.
3. **C-group** — frontend grouping contract (+ optional additive summary endpoint).
4. **C-split** — `POST /plans-v2/{plan_id}/split`, draft-only first; approved-plan split scoped separately.
