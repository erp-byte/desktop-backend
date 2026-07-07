# SO ↔ Planning Convergence + Create-Job-Card — Functional & Code Audit

> **Date:** 2026-06-21
> **Scope:** the "merge models step by step" work — making **SO Creation** mirror **Planning**, the shared **`lib/planBuilder.tsx`** extraction, and the new per-article **Create Job Card** flow on the **Plan List** page.
> **Method:** 29-agent workflow — 8 module maps → 6 parallel audits/analyses → per-finding adversarial verification + a completeness critic. Repo is **not** a git tree, so this audits the **live file state**. Every claim was reproduced against code; severities shown are the **post-verification (corrected)** ones.

---

## TL;DR

- **SO Creation side is functionally complete and sound** — filters, entity selector, sync, article selection, and the shared plan-builder all wire through to a real `Create Plan` write.
- **Plan-list "Create Job Card" flow is UI-only.** `onContinue` (`plan-list/page.tsx:417-428`) only fires a toast (`"Creation backend wired next."`) and discards the payload; there is **no `createJobCard` helper** in `lib/plans.ts`. Nothing is broken today, but **almost every serious risk is forward-looking** — it detonates the moment the wizard is wired.
- **Backend SO changes are injection-clean** (verified), with two perf/UX rough edges.
- **14/14** High/Medium findings adversarially verified and confirmed (several severity-corrected).

Files audited:

| Area | Files |
|---|---|
| Frontend | `web_replica/src/app/modules/production/so-creation/page.tsx` (2492), `web_replica/src/lib/planBuilder.tsx` (1712, NEW), `web_replica/src/app/modules/production/plan-list/page.tsx` (1885), `web_replica/src/lib/{so,so-list-cache,fulfillment,processCatalog}.ts` |
| Reference | `web_replica/src/app/modules/production/planning/page.tsx` (3016, unchanged baseline) |
| Backend | `server_replica/app/modules/so/router.py` (+ `schemas/response.py`), `server_replica/app/modules/production/{router.py, services/fulfillment_v2.py, services/plan_v2.py}` |
| Phase docs | `server_replica/docs/SFG_Phases_1-5_Changes.md`, `SFG_Phases_6-10_Changes.md` |

---

## 1) Functional audit

### 1.1 SO Creation — works, with real edges

| Sev | Finding | Where |
|---|---|---|
| **High** | **Entity flip after selecting articles → entity/fulfillment mismatch persisted to the plan.** `EntitySelector.onChange` only does `setCompany/setPage`; it never clears `selectedLineIds` or calls `pb.clearAllSelection()`. Rows are cached at check-time scoped to the *then-current* company; `onCreatePlan` then POSTs the *new* entity linked to the *old* entity's fulfillment rows. No reconciliation guard exists. | `so-creation/page.tsx:666`, `:341-396` |
| **Medium** | **Sync doesn't refresh expanded rows.** `onSync` only sets `syncMsg` — no refetch / version-bump. The per-line pending effect is keyed solely on `lineIdsKey`, which a sync doesn't change, so displayed pending kg/pcs stays stale until collapse/re-expand. *(Correction: the checkbox itself re-resolves via a fresh fetch, so only the displayed pending is stale.)* | `so-creation/page.tsx:616-634`, `:1920-1944` |
| **Medium** | **Selection not persisted in the list cache.** `SoListCache` persists filters/sort/page/expanded but **not** `selectedLineIds`/`lineToFulfillment`; refresh/back-nav silently drops an in-progress plan while filters survive. | `so-list-cache.ts:18-37` |
| Low | SO Sync always syncs **all** entities (`syncFulfillment({entity:null})`) even when CFPL/CDPL is selected — inconsistent with the page's own entity scoping. | `so.ts:239-248` |
| Info ✓ | **Verified correct:** the article checkbox (select) and the `+/−` (expand) are properly decoupled — selecting never expands. | `so-creation/page.tsx:2040-2103` |
| Info | By design, `showSteps={false}` means SO-created plans carry **no floor/route** (`area` never set); they rely entirely on the server snapshotting `bom_process_route`. | `planBuilder.tsx:654-663`, `:818` |

### 1.2 Plan-List "Create Job Card" wizard — UI-only, several latent bugs

| Sev | Finding | Where |
|---|---|---|
| **High** | **The "Create Job Card" button is a dead-end** — primary orange CTA on every plan row, but `onContinue` only toasts and discards `{planLineId, qtyKg, qtyUnits, wipSteps, pkgFloor}`. No fetch, no helper. Can mislead operators into believing a card was created. | `plan-list/page.tsx:417-428`, `:1052-1062` |
| **Medium** *(was High)* | **Wizard payload can't populate `job_card_v2`.** `WipStep = {process, floor, sfgOutput}` + `pkgFloor` captures **no** `stage` (NOT NULL), no `input/output_kind`, no `input/output_code`. *(stage can be derived via `stageFromProcess`; the genuinely missing data is the kinds + the FG/packaging output_code.)* | `plan-list/page.tsx:1367`, `:1294`, `:1155-1157` |
| **Medium** *(was High)* | **Sticky qty prefill.** `goNext` prefills qty only when empty; picking article A → Next → Back → picking B inherits A's quantity (neither the radio nor Back clears `qtyKg/qtyUnits`). Latent until the write lands; `canCreate` passes the wrong value. | `plan-list/page.tsx:1138-1148` |
| **Medium** *(was High)* | **Three inconsistent line-identity schemes** (`plan_line_id ?? 0` / `?? i` / `?? null`). Selection keys on `?? i`; `onContinue` lookup keys on `?? null` → resolves the wrong/absent line when `plan_line_id` is null. Cosmetic now; wrong-line attachment once wired. | `plan-list/page.tsx:1120-1131`, `:418` |
| **Medium** *(was High)* | **Approve and the new wizard coexist with no mutual guard.** Approve→auto-JC is still fully live (`doApprove → approvePlan`, `:199-222`); once the wizard writes, both can create `job_card_v2` rows for the same plan. | `plan-list/page.tsx:1039-1095` |
| **Low** *(was Medium)* | **WIP merge silently drops all-but-first `sfgOutput`** (process names are joined, outputs are not) — output identity is load-bearing for the SFG seam. | `plan-list/page.tsx:1414-1430` |
| Low | No qty reconciliation vs `planned_qty_kg` (planBuilder clamps to pending; the wizard doesn't). | `plan-list/page.tsx:1138-1157` |

---

## 2) Code audit

### 2.1 Frontend

- **Duplication is the dominant code-quality risk (Medium).** `planBuilder.tsx` is the extracted shared builder, but `planning/page.tsx` **still holds a full ~1,700-line inline copy and does not import the shared module** — kept in sync by hand (header note `planBuilder.tsx:18-20`). The duplicated surface includes the subtle **packing-stage promotion** logic (`merged.stage = packingPick?.stage ?? …`, `planBuilder.tsx:417-427` ≡ `planning/page.tsx:540-546`) that governs EGA/variance attribution — a divergence there would silently corrupt cost attribution. There is now even a **third** copy of the merge/reorder pattern in `WipProcessList` (`plan-list/page.tsx:1369-1561`). No in-file pointer warns planning's editors.
- **`planned_qty_units` fabrication (Low):** when units are unknown, planBuilder writes `Math.max(1, Math.round(qtyKg))` — inventing a unit count from kg for by-weight SKUs. `planBuilder.tsx:672`
- **`patchCardOverride` `isEmpty` ignores factory/steps** — asymmetric pruning; `isEmpty` is a misnomer. `planBuilder.tsx:251-255`
- **`WipProcessList` re-implements merge/reorder** instead of reusing shared primitives; merge semantics differ (no time/loss/stage handling). `plan-list/page.tsx:1369-1561`
- React correctness otherwise clean (no ref-writes-in-render / setState-in-effect violations); `tsc`/`eslint` were green prior to this audit.

### 2.2 Backend

- **Injection review: CLEAN (verified).** `article`/`so_number` are comma-split and each value bound as a positional `$N`; the only interpolated tokens (`sort_by`, `sort_order`, `status`) are constrained by FastAPI `Query(pattern=…)` allow-lists; `_GST_STATUS_ORDER` is static; param-index accounting is correct (`/view` appends page_size/offset; `/export` passes `*params`). Also confirmed: `create_plan` is reverted to the 9-column step INSERT, **zero `063` references**, and **no service reads the seam columns off `production_plan_step_v2`** — so dropping those (never-added) plan-step columns would break nothing. `so/router.py:229-377`, `:503-744`
- **`/export` has no LIMIT (Medium / perf).** An authenticated user with zero filters materializes the entire `so_header` + `so_line` + `so_gst_reconciliation` through Pydantic. `so/router.py:718-744`
- **`filter_options` runs two full-table DISTINCT scans on every `/view`** including deep pages where the lists never change. `so/router.py:541-584`
- **`approve_plan` reports `approved:true` while the `job_cards_already_exist` error is only nested (Low).** The nested error *is* returned, but the top-level envelope misleads; the router maps only `missing_approver` (400) / `not_found_or_invalid_status` (404). `plan_v2.py:548-582`
- **SFG seam only stamps for exactly 2 steps (Medium → escalated to Blocker by the critic, see §4).** Any routed SFG article whose plan isn't exactly 2 steps gets **NULL `input/output_code`** and JCs still ship. The same positional guard exists in `_resync_jcs_after_step_change`. `job_card_v2.py:820-860`, `plan_v2.py:1032-1054`
- **`get_fulfillment_by_so_lines` (Low / UX):** an entity/FY filter can push an existing-but-different-scope row into `missing_so_line_ids`, prompting a Sync that `ON CONFLICT (so_line_id, financial_year) DO NOTHING` won't fix. `fulfillment_v2.py:421-444`

---

## 3) Q1 — What is left to migrate (Planning → SO Creation / Plan List)

**Already migrated to SO Creation:** entity selector, Sync, Customer/SO/Article MultiSelect trio, per-article selection bridge, the **shared plan-builder** (`usePlanBuilder` + `SelectedArticlesPanel`), Create-Plan CTA + its 4 validation gates, pcs↔kg interlink, factory/floor masters. The process-route editor is shared but **intentionally hidden on SO** (`showSteps={false}`).

### Remaining — by target

**SO Creation**
- **Cross-filtered filter options** *(M)* — planning's dropdowns narrow reactively (`fetchFulfillmentFilterOptions`, `planning/page.tsx:192-206`); SO uses static option lists, so operators can pick incompatible Customer/SO/Article combos yielding empty lists.
- **Entity-scoped Sync + post-sync refresh** *(S)* — switch to the parameterized `syncFulfillmentNow(entity)` (`fulfillment.ts:101`) and invalidate the per-line pending cache. *(Critic: this is a real correctness bug, not just parity.)*
- **Selection persistence** across refresh/back-nav *(S)* — add `selectedLineIds`/`lineToFulfillment` to `SoListCache`.
- *(Optional)* inline click-to-edit deadline (`DeadlineCell` → `reviseFulfillment`) *(M)*.

**Plan List**
- **Create Job Card backend write** *(L)* — *the big one*: endpoint + `createJobCard` helper + map `WipStep` → `job_card_v2` (synthesize `stage`, `input/output_kind`, `input/output_code`). See §4.
- **Dual-creation guard** *(M)* — Approve vs wizard.
- **SFG `output_code` validation** against the catalogue (replace free-text `"SFG0042"`, `:1539`) *(M)*.
- **Packaging → full FG job-card row** (process + stage + FG output_code, not just a floor) *(M)*.
- **Qty reconciliation** vs `planned_qty_kg` *(S)*; **`plan_line_id` null-collision** fix *(S)*.

### Non-issues (confirmed)
- **Export parity** — planning has *no* export; SO already has its own (`onExport`, `:584`).
- **Material-availability / indents** — **not a planning-page capability** (deferred to Plan-Detail), so nothing to migrate *from* planning.

### Shared-module debt (primary)
Migrate `planning/page.tsx` to import `lib/planBuilder.tsx` and delete its ~900-line inline copy. Risk: planning's `onCreatePlan` (`:682-819`) and the shared one (`planBuilder.tsx:568-705`) must stay **byte-equivalent** in validation/payload (LOCAL date, factory→warehouse, steps-only-when-floored, `area` = first floored step). Secondary: consolidate the two sync clients (`so.ts:239` vs `fulfillment.ts:101`). Future: promote a shared `stage/kind/output_code` derivation helper.

---

## 4) Q2 — Structural changes after current state + Job Card impact

### The load-bearing constraint
`job_card_v2.stage` is **NOT NULL** (`017_job_card_v2.sql:57`) while `production_plan_step_v2.stage` is nullable (`012_planning_v2_steps.sql:24`), and `job_card_v2` also has `plan_id/plan_line_id/`**`plan_step_id`**` NOT NULL ON DELETE RESTRICT`. So a per-article create **still needs a plan_step row per JC** unless `plan_step_id` is first made nullable. `practical_operation/stage_bucket` do **not** need adding to `job_card_v2` (they live on `bom_process_route`, JOIN-reachable); the JC already carries `input/output_kind` (017) + `input/output_code` (050).

### Recommended JC model
**One chained `job_card_v2` per WIP process → terminating Packaging JC** — preserves the existing prev/next chain (`job_card_v2.py:946-953`), stage-1-unlocked / rest-`awaiting_previous_stage` (`:864-866`), `dispatch_to_next` lock-release (`:1812-1815`), and stage-1-only RM+PM materialisation (`:935-944`). Rolling all WIP into a single JC would break that machinery.

### Ordered, safe sequencing (destructive LAST)
1. **Additive backend write path** (coexists with Approve) — chained-JC-per-process; derive non-null `stage`; set `input/output_kind` by chain position; stamp the SFG seam per adjacent pair from `bom_process_route`; build the prev/next chain; materialise RM+PM on stage 1; write `floor` per JC; wrap in a transaction + **per-line idempotency guard**.
2. **Additive frontend** — `createJobCard` helper in `lib/plans.ts` + wire `onContinue`. Keep Approve live; verify parity.
3. **Status reconciliation** — plan/line status + `so_fulfillment_v2.planned_qty` (avoid double-bump; `create_plan` already bumps it, `plan_v2.py:285-309`). Confirm `maybe_close_plan_from_jcs` still flips the plan.
4. **Remove Approve → auto-JC** (UI + the `create_job_cards_from_plan` call in `approve_plan`).
5. **Rewrite planning off plan-steps** + stop `create_plan` snapshotting `process_name/stage/floor`; remove `area` from `amendments_v2` PATCHABLE / `update_line` / serialize.
6. **Destructive drops LAST** — `production_plan_step_v2.process_name/stage/floor`, then `production_plan_line_v2.area`.

### Job Card module impact (blockers/highs)
- **`create_job_cards_from_plan` is the *sole* live JC writer** and reads `step.process_name/stage/floor` — dropping those columns or removing Approve before the new path exists breaks all JC creation. `job_card_v2.py:804-927`
- **`is_packing_stage` gates EGA** via substring match on `"packaging"/"packing"` — the Packaging JC's stage must contain that token. `job_card_v2.py:115`, `:1981`
- **`input/output_kind` are CHECK-constrained NOT NULL** — must be computed by chain position (first=RM, last=FG, middles SFG/WIP). `017:66-67`
- **`amendments_v2` PATCHABLE includes `area`** — dropping the column without editing the allowlist breaks the PATCH endpoint. `amendments_v2.py:64`
- Annexure/PDF read `floor` off the **JC** (not the step) — safe **provided** the new write path keeps populating `job_card_v2.floor`. `jc_annexures_v2.py:116-126`, `job_card_pdf.py:97`

### ⚠️ Critic escalations (these change the plan — the base analyses under-counted them)
1. **NULL `output_code` is silent corruption, not just missing codes (BLOCKER).** `materialise_wip_dispatch` does `sku = sfg_code or fg_sku_name` (`job_card_v2.py:1691`) — a NULL-seam middle WIP JC mints `inventory_batch` stock **under the finished-goods SKU name**, so the Stage-2 picker / `get_sfg_on_hand` can never find it. The multi-WIP write **must** stamp `output_code` on every producing JC, and `materialise_wip_dispatch` should be hardened to fail loudly on NULL.
2. **`create_plan` ALREADY inserts a plan_step row per `bom_process_route` step at plan creation** (`plan_v2.py:247-274`) even with no override — so "materialise a step per JC" would **double-create** divergent steps, and the wizard's free-text processes won't match the auto-created ones. Decide: wizard **edits** the existing steps, or `create_plan` **stops** auto-snapshotting (tie to step 5).
3. **`derive_stage_from_process` (`plan_v2.py:1314-1332`) is pure string-munging** with no catalogue validation → it **false-negatives** real packing names ("Pouch Filling" → EGA refused) and **false-positives** "Bulk Packaging" (EGA wrongly allowed on a non-final JC). Stamp an explicit packing constant on the Packaging JC + validate WIP stages against the stage catalog; don't trust string derivation for the EGA gate.
4. **The wizard supplies zero material data** — all RM+PM issuance is on stage 1 and derives entirely from `bom_id` (`job_card_v2.py:935-944`), which must be read from `production_plan_line_v2` (`:788`), **not** the wizard.
5. **`approve_plan` is re-runnable** (`status IN ('draft','approved')`, `plan_v2.py:565`) — a plan with *some* wizard-created lines, then Approved, gets **blocked entirely** by the plan_id idempotency guard, leaving the rest with no JCs. Land the **per-line** guard in step 1, not step 4.
6. Additional downstream surfaces for the impact checklist: `sfg_genealogy` / `sfg_box` (a NULL-seam chain pollutes the trace, `059_sfg_genealogy.sql:48-66`), and `floor_movement.job_card_id` (v2 dispatch writes NULL + stashes the id in reason text, `job_card_v2.py:1719-1731` — reports keyed on `job_card_id` won't see v2 chains).

---

## 5) Q3 — Comparison vs the phase docs

References: `SFG_Phases_1-5_Changes.md`, `SFG_Phases_6-10_Changes.md`.

The convergence work is **almost entirely outside** the documented SFG vertical — the docs predate it and never mention `production_plan_step_v2/line_v2`, the planBuilder extraction, migration 063, or a per-article JC flow. **No gate (G1-G5) is touched.**

| Phase | Theme | Affected | How much | Note |
|---|---|---|---|---|
| 1 | SFG identity | No | none | untouched |
| 2 | Process-class + Create-WIP checklist (G2) | Yes | **minor** | The wizard's `WipProcessList` is a **parallel, classifier-unaware** WIP checklist (distinct from the JC-detail `CreateWipChecklist`) — free-text, no `practical_operation/stage_bucket`. Must reconcile with the Phase-2 classifier if wired. |
| 3 | 2-stage routing + SFG seam | Yes | **moderate** | The wizard is the **intended successor** to the Phase-3 `create_job_cards_from_plan` seam path. That path is **fully intact today**; the wizard collects only free-text `sfgOutput` with no seam contract — the most consequential *future* divergence. |
| 4 | Stage-2 consumes SFG (G1=Option B) | Yes | minor | No current conflict; a future write must honor per-row `input_kind`. |
| 5 | WIP materialisation + picker (G3) | Yes | minor | No conflict now; free-text `sfgOutput` ≠ the canonical `sfg_code` a future write needs. |
| 6 | Catalogue / reporting | No | none | — |
| 7 | Genealogy / routing-gap | No | none | (but see critic #6) |
| 8 | Bar-line override (G4) | No | none | — |
| 9 | Seam minting | No | none | — |
| 10 | Hardening pass | Yes | **minor** | Phase-10's "verified clean" covers **6-9 only**; the convergence work (placeholder modal, planBuilder duplication, duplicate sync clients) is **unreviewed** and should not be assumed hardened. |

### New work that belongs in the docs (currently undocumented)
- The whole SO/Planning convergence (SO-creation parity features + `POST /fulfillment-v2/by-so-lines` + `get_fulfillment_by_so_lines`).
- The planBuilder extraction + its **deliberate-duplication contract** (contradicts the docs' zero-drift convention).
- The `showSteps={false}` design decision (routing from SFG/JC, not the plan step).
- The lightweight-plan + direct-JC-entry pivot.
- The **built-then-reverted migration 063** (no hint in the 050-062 list).

**Net:** Phase 3 (moderate), Phases 2/4/5/10 (minor) are *conceptually* affected; nothing is *broken* against the docs today. The docs need a new **"Phase 11 — SO/Planning convergence & direct-JC-entry"** section recording this as the successor to the Phase-3 approve path.

---

## 6) Recommended next moves

1. **Standalone bug fixes** (worth doing regardless of the JC backend): entity-flip selection clear (§1.1 High), post-sync refresh (§1.1 Medium), `plan_line_id` lookup unification (§1.2 Medium), qty prefill reset (§1.2 Medium).
2. **Draft the Phase-11 doc section** (§5).
3. **Build the Create-Job-Card backend** per §4 — needs two decisions first:
   - **JC model:** chained-per-process *(recommended)* vs one-WIP-JC + one-Packaging-JC.
   - **Step reconciliation:** wizard **edits** the auto-created plan_steps vs `create_plan` **stops** auto-snapshotting.

---

*Generated from a multi-agent audit; all High/Medium findings adversarially verified against the code (14/14 confirmed, severities corrected where noted).*
