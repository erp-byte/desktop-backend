# SO ↔ Planning Convergence — Remediation Report

> **Date:** 2026-06-21
> **Companion to:** `SO_Planning_Convergence_Audit.md` (the audit this remediates)
> **Scope decided with the requester:** apply **all decision-free fixes now** + a **safe SFG-corruption backstop**; deliver the per-article **Create-Job-Card backend as a design** (not built) for sign-off. JC model = **chained-per-process**; plan steps = **mutable after approval** (⇒ the N-step SFG seam generalization is required by the design).
> **Method:** every audit finding re-verified against the live code, then fixed surgically. The planning→shared-planBuilder migration was executed under a tsc/eslint gate. **Two of the audit's recommended fixes were found unsafe on verification and corrected** (see §3).

---

## 0) Verification status (all green)

| Gate | Command | Result |
|---|---|---|
| Type-check (whole frontend) | `tsc --noEmit` | **exit 0** |
| Lint (6 changed FE files) | `eslint …` | **exit 0** |
| Backend syntax (4 changed PY files) | `python -m py_compile …` | **OK** |

No automated test suite is present in the server venv (no `pytest`/`ruff`/`mypy`), so backend changes were verified by `py_compile` + manual review + tracing every caller of each changed function. Frontend behavioural parity for the large planning migration rests on the shared module being byte-equivalent (already proven in the audit) plus tsc/eslint.

---

## 1) What changed — file by file

### Frontend

| File | Finding(s) | Change |
|---|---|---|
| `web_replica/src/app/modules/production/so-creation/page.tsx` | **SO-1**, SO-2, SO-3, C2-001 | entity-flip selection clear; post-sync pending refresh; entity-scoped Sync; selection cache persist + rehydrate |
| `web_replica/src/lib/so.ts` | C2-002 | removed the duplicate entity-less `syncFulfillment` client (consolidated on `syncFulfillmentNow`) |
| `web_replica/src/lib/so-list-cache.ts` | C2-001 | added `selectedLineIds` + `lineToFulfillment` to the cache shape |
| `web_replica/src/lib/planBuilder.tsx` | PB-PATCH-ISEMPTY, PB-UNITS-FAB | isEmpty now respects factory/steps; units-fab guarded + documented (see §3) |
| `web_replica/src/app/modules/production/plan-list/page.tsx` | PL-LINE-IDENTITY, PL-QTY-PREFILL, PL-WIP-MERGE, PL-QTY-RECON | canonical `getLineId`; qty reset on article switch; merge preserves SFG outputs; soft over-qty hint |
| `web_replica/src/app/modules/production/planning/page.tsx` | DUP-PLANBUILDER | migrated to `@/lib/planBuilder`; **3016 → 1465 lines (~1551 deleted)** |

### Backend

| File | Finding(s) | Change |
|---|---|---|
| `server_replica/app/modules/so/router.py` | C5-SO-1, C5-SO-2 | `/export` `LIMIT` (default 10000, max 50000); `filter_options` cached on app.state (60 s TTL) instead of two full-table scans per `/view` |
| `server_replica/app/modules/production/services/plan_v2.py` | APPROVE-ENVELOPE, DERIVE-STAGE-STRING | approve envelope surfaces `job_cards_created`/`job_cards_error` at top level; derive-stage brittleness documented |
| `server_replica/app/modules/production/services/fulfillment_v2.py` | FULFILLMENT-SCOPE | splits not-found ids into truly-`missing_so_line_ids` vs new `out_of_scope_so_line_ids` |
| `server_replica/app/modules/production/services/job_card_v2.py` | **NULL-OUTPUT-CODE-CORRUPTION** (Blocker), DERIVE-STAGE-STRING | hard-fail a declared-SFG producer with NULL `output_code`; loud warning on the WIP-under-FG fallback; `is_packing_stage` contract documented |

---

## 2) Findings resolved — detail

### §1.1 SO Creation

**SO-1 — Entity flip persists a cross-entity plan (High).** `EntitySelector.onChange` did only `setCompany/setPage`. A new `onEntityChange` now also calls `clearLineSelection()` + `pb.clearAllSelection()`, so flipping CFPL↔CDPL can never POST the new entity linked to the old entity's fulfillment rows. *(Note: this is a deliberate divergence from the planning page, which intentionally keeps selection across entity flips to compose multi-entity plans. SO Creation has no such requirement and the cross-entity link is a real corruption risk, so clearing is correct here.)*

**SO-2 — Sync didn't refresh expanded pending (Medium).** Added a page-level `syncVersion` counter, bumped in `onSync`'s `finally`, threaded through `SoTable → SoMobileCard/SoTableRow → SoLineDetail` and added to the per-line pending effect deps. A Sync now re-fetches the pending kg/pcs shown in open rows instead of leaving them stale until collapse/re-expand.

**SO-3 / C2-002 — All-entity Sync + duplicate client (Low).** The manual Sync is now entity-scoped (`syncFulfillmentNow(company…)`); the post-upload auto-sync stays entity-wide (a fresh upload can span entities). The duplicate `syncFulfillment()` in `so.ts` was deleted; both pages now use the single `syncFulfillmentNow(entity?)` client in `fulfillment.ts`.

**C2-001 — Selection not persisted (Medium).** `SoListCache` gained `selectedLineIds` + `lineToFulfillment` (Sets/Maps dehydrated to arrays). The page hydrates them in the lazy `useState` initialisers, persists them in the save effect (new `selectionKey` fingerprint dep), and a new mount effect **re-feeds the cached rows into the plan-builder** (`fetchFulfillmentsBySoLines` → `pb.selectRow`) so a refresh/back-nav restores both the ticked boxes *and* the panel cards.

### §1.2 Plan-List wizard (frontend-only fixes; the write path is the §4 design)

**PL-LINE-IDENTITY (Medium).** One `getLineId(line, index) = plan_line_id ?? index` helper replaces the three divergent fallbacks (`?? 0` / `?? i` / `?? null`) at all four sites, including the parent `onContinue` lookup — so a null `plan_line_id` can't resolve the wrong line.

**PL-QTY-PREFILL (Medium).** Switching the article radio now clears `qtyKg`/`qtyUnits`, so A→Next→Back→B no longer inherits A's quantity (`goNext` re-prefills from the new line).

**PL-WIP-MERGE (Low).** Merge now keeps **every distinct** `sfgOutput` (`[...new Set(...)].join(" + ")`) instead of silently dropping all but the first — output identity is load-bearing for the SFG seam.

**PL-QTY-RECON (Low, was `reproduces=no`).** Added a **soft, non-blocking** over-qty hint when `qtyKg > planned_qty_kg`. A hard clamp was deliberately avoided: catch-up/rework can legitimately exceed planned, and reconciliation against live pending belongs server-side at write time (see §4).

### §2.1 / §2.2 Code & backend

**PB-PATCH-ISEMPTY (Low).** `isEmpty` now also checks `factory == null && steps` empty, so clearing qty doesn't discard a card that already has a factory or a hand-edited route. *(This fix now lives in one place — see DUP-PLANBUILDER below — so it no longer needs mirroring into planning.)*

**DUP-PLANBUILDER (Medium) — the primary code-quality debt.** `planning/page.tsx` no longer holds a ~900-line inline copy of the plan-builder; it imports `usePlanBuilder` + `SelectedArticlesPanel` from `@/lib/planBuilder` and deletes the inline constants/types/helpers, the five inline UI components (`SelectedArticlesPanel`/`SelectedCard`/`StepsSection`/`BomMaterialsSection`/`NumberField`), and all inline state/handlers. The one model difference — planning's combined `toggleSelection(id)` vs the hook's `selectRow`/`deselect` — is bridged by a thin local adapter. **This also fixes a latent planning bug**: the hook's `onCreatePlan` reads selected rows from the snapshot cache, so a filter change can no longer silently drop a selected card from the created plan (planning's old `rows.filter` could). The deliberate-duplication contract in the audit is now retired — there is a single source of truth.

**C5-SO-1 — `/export` unbounded (Medium).** Added `limit` (default 10000, hard max 50000), bound positionally and applied as `LIMIT`. Prevents an unfiltered export from materialising the entire SO tree through Pydantic.

**C5-SO-2 — `filter_options` scanned every page (Medium).** Extracted `_get_filter_options(request)` which caches the (global, unfiltered) dropdown lists on `app.state` with a 60 s TTL. Deep pagination no longer re-runs two full-table `DISTINCT` scans per call.

**APPROVE-ENVELOPE (Low).** `approve_plan` now returns `job_cards_created: bool` and, on soft-fail (e.g. `job_cards_already_exist` on idempotent re-approve), `job_cards_error` at the top level. `approved: true` and the nested `job_cards` are preserved, so the change is additive and the router's error mapping is untouched.

**FULFILLMENT-SCOPE (Low).** `get_fulfillment_by_so_lines` now distinguishes **truly missing** so_lines (no row in any scope → Sync helps) from **out-of-scope** ones (a row exists outside the requested entity/FY → Sync's `ON CONFLICT … DO NOTHING` won't help). `missing_so_line_ids` is now precise; a new `out_of_scope_so_line_ids` lets callers avoid prompting a futile Sync. *(Verified safe: `missing_so_line_ids` was a declared field but consumed by no logic.)*

### Verified — no action needed (confirmed against live code)

- **SO-4** checkbox/expand decoupling — already correct by design.
- **SO-5** `showSteps={false}` on SO plans — intentional (stage-driven routing).
- **C5-SO-3 / Injection** — SQL bindings confirmed safe (positional params; regex-constrained `sort_by`/`sort_order`/`status`; static GST order; correct param-index accounting). The new `/export` `LIMIT` follows the same positional-binding convention.

---

## 3) ⚠️ Two audit recommendations corrected on verification

The audit's findings reproduced, but two of its **recommended fixes were unsafe** and were not applied as written:

**PB-UNITS-FAB — "send `null` for `planned_qty_units`" would crash plan creation.**
`production_plan_line_v2.planned_qty_units` is **`NOT NULL CHECK (planned_qty_units > 0)`** (`009_planning_v2.sql:160`), and `create_plan` inserts the value directly (`plan_v2.py:197`). Sending `null`/`0` violates the constraint and rejects the whole plan. The kg-derived fallback exists **precisely to satisfy that constraint**. The genuinely-correct fix — let the backend derive `qty_kg / all_sku.uom` (which `resolve_bom_multiplier` *already* does, but only when `qty_units` is `NULL`) — requires making the column nullable and is a **schema change**, tracked in §4. Applied now: a guard comment so the fab isn't "fixed" into a crash.

**SFG-SEAM-2STEP — the 2-step guard is a safety mechanism, not a naive bug.**
`_resolve_sfg_seam_code` returns **one** SFG code per BOM (the G2-locked 2-stage model). The `step_count == 2` guard intentionally *skips* the seam (leaving codes NULL + warning) rather than mis-positioning it when a plan diverges from 2 steps. A true N-step generalization presumes multi-SFG chains that **don't exist yet** — a product decision tied to the locked gates. It is therefore **part of the §4 design** (required because you chose *mutable steps*), not a standalone edit. What *was* applied now is the **decision-free defensive backstop** for the coupled Blocker:

**NULL-OUTPUT-CODE-CORRUPTION (Blocker) — backstop applied.** `materialise_wip_dispatch` did `sku = sfg_code or fg_sku_name`, so a NULL seam silently minted WIP under the **finished-goods** name (invisible to the Stage-2 picker / `get_sfg_on_hand`). Now: `dispatch_to_next` **hard-fails** when a step declared `output_kind='SFG'` has a NULL `output_code` (unambiguous contract violation), and `materialise_wip_dispatch` emits a **loud warning** on the ambiguous WIP-with-no-code fallback. An unconditional raise was deliberately avoided — it would break legitimate non-SFG multi-step WIP chains. The full elimination of NULL seams comes with the N-step write in §4.

**DERIVE-STAGE-STRING (Low, partial).** `is_packing_stage` substring matching gates EGA inside the **locked SFG vertical**. Changing the matcher would shift EGA behaviour across many JCs, so behaviour was left as-is and the brittle contract was **documented** in both `derive_stage_from_process` and `is_packing_stage`, pointing at the canonical classifier (`processCatalog.classifySteps` / `master_ingest.classify_route_steps`) for the future catalogue-lookup hardening.

---

## 4) Create-Job-Card backend — design (for sign-off, not yet built)

Per your decisions: **chained-per-process JC** + **mutable plan steps**. Because steps are mutable, the **N-step SFG seam generalization is required** (a 2→3-step edit must not leave NULL seams). The sequencing keeps every destructive change LAST.

### 4.1 Target JC model (chained-per-process)
One `job_card_v2` per WIP process → a terminating Packaging JC, reusing the proven chain machinery:
- `prev_job_card_id`/`next_job_card_id` bi-directional chain;
- stage-1 `unlocked`, the rest `locked` + `awaiting_previous_stage`, released by `dispatch_to_next`;
- **RM + PM materialised on stage 1 only**, derived from `bom_id` read from `production_plan_line_v2` — **not** from the wizard (the wizard supplies zero material data);
- `input_kind` first=`RM`, middles=`SFG`, `output_kind` last=`FG`, others=`SFG`/`WIP`.

### 4.2 Sequenced plan (additive first, destructive last)

1. **Additive write path (coexists with Approve).** `createJobCard` helper in `lib/plans.ts` + a `POST` endpoint. Server derives `stage` (validated against the stage catalogue, not raw string-munging), `input/output_kind` by chain position, and the SFG seam per adjacent producing pair. Wrap in a transaction with a **per-line idempotency guard** (NOT plan-level — critic #5: a partial wizard-create then Approve must not block the remaining lines).
2. **N-step SFG seam generalization (required by mutable steps).** Replace the `step_count == 2` guards in `create_job_cards_from_plan` and `_resync_jcs_after_step_change`: stamp `output_code` on **every** producing JC and `input_code` from the previous producer. This needs per-step SFG identity — extend `_resolve_sfg_seam_code` to return per-step codes (or mint them). The `materialise_wip_dispatch` hard-fail (already added) then guarantees no NULL seam can ship.
3. **Step reconciliation (mutable).** `create_plan` already auto-snapshots a `production_plan_step_v2` row per BOM step (critic #2), so the wizard must **edit** those rows, not double-create. `add_step`/`reorder`/`delete_step` re-sync JCs via the now-N-step-aware `_resync_jcs_after_step_change`.
4. **Wizard payload + UI.** Extend `WipStep` and have `WipProcessList` adopt the existing catalogue classifier (`processCatalog.classifySteps`) instead of free-text, and **validate `output_code` against the catalogue** (replace the free-text `"SFG…"`). Wire `onContinue` to `createJobCard`. Add the qty-vs-pending reconciliation server-side (PL-QTY-RECON's hard check).
5. **Dual-creation guard.** Once the wizard writes, `approve_plan`'s auto-create must skip lines that already have wizard JCs (per-line guard from step 1). Keep Approve live until parity is proven.
6. **PB-UNITS-FAB proper fix (schema).** Make `production_plan_line_v2.planned_qty_units` nullable, default `create_plan` to `NULL` when units are unknown, and let `resolve_bom_multiplier` derive `kg/uom`. Removes the kg→units fabrication at the source.
7. **Destructive LAST.** Only after the new path is proven: remove Approve→auto-JC; stop `create_plan` snapshotting `process_name/stage/floor`; drop `production_plan_step_v2.process_name/stage/floor` then `production_plan_line_v2.area` (and remove `area` from `amendments_v2` PATCHABLE / `update_line` / serialize).

### 4.3 Downstream surfaces to cover when building
- `sfg_genealogy` / `sfg_box` — a NULL-seam chain pollutes the trace (`059_sfg_genealogy.sql`).
- `floor_movement.job_card_id` — v2 dispatch writes NULL and stashes the id in reason text; reports keyed on `job_card_id` won't see v2 chains.
- Annexure/PDF read `floor` off the **JC** — the new write path must keep populating `job_card_v2.floor`.
- `amendments_v2` PATCHABLE still includes `area` — drop it from the allowlist in lock-step with the column drop.

### 4.4 Phase-11 documentation (recommended)
The phase docs (`SFG_Phases_1-5`, `6-10`) predate this convergence work. A new **"Phase 11 — SO/Planning convergence & direct-JC-entry"** section should record: the SO-creation parity features + `POST /fulfillment-v2/by-so-lines`, the planBuilder extraction (now de-duplicated — the deliberate-duplication contract is retired), the `showSteps={false}` design, the lightweight-plan/direct-JC pivot, and the built-then-reverted migration 063.

---

## 5) Net result

- **All 14 High/Medium findings** addressed: SO-1 (High) fixed; both Blockers have the safe backstop now + a sequenced design for the full fix; every Medium fixed; the primary code-quality debt (planBuilder duplication) retired.
- **All Low findings** fixed, documented-as-deferred (PB-UNITS-FAB, DERIVE-STAGE-STRING — with the reasons above), or confirmed safe.
- **Two unsafe audit recommendations** caught and corrected before they could regress production.
- Frontend `tsc`/`eslint` green; backend `py_compile` green.
- The only deferred work is the **Create-Job-Card backend build** (your explicit choice), fully specified in §4.
