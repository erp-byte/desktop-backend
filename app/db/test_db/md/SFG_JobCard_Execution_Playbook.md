# Candor Foods — SFG / Job-Card Integration · Execution Playbook

> **Companion to** `SFG_JobCard_Change_Checklist.xlsx` (the exhaustive per-file ledger). This document is the **ordered execution plan**. Generated 2026-06-17; verified against live code in `server_replica/app`, `web_replica/src`, `frontend_replica/src`.
>
> **Status today:** nothing is built. Every item below is a proposed change. The SFG/WIP work is a **data + materialisation** layer on the *existing* v2 job-card chain — not a chain redesign.

---

## 0. The two governing rules (non-negotiable)

**RULE 1 — Vertical slices, never horizontal layers.**
Work is organised into **6 slices**. Each slice is a *thin vertical cut* that changes the **database, the backend service, AND both frontends (web + Electron) together**, and ends with something testable end-to-end. We do **NOT** finish all backend first and bolt the UI on last. When a DB column lands in a slice, the backend that writes/reads it and the frontend that renders it land **in the same slice / same PR**.

**RULE 2 — A tough manual code review after every change.**
Every checkbox below is followed by a **micro-review** (the author self-reviews against the acceptance check). Every slice ends with a **Slice Review Gate** — a hard stop where a second engineer manually reviews the whole vertical cut against the [Standard Review Gate](#2-the-standard-manual-code-review-gate-apply-after-every-change). No code merges, and the next slice does not start, until the gate passes. Reviews are adversarial: the reviewer's job is to *break* the change, not to approve it.

```
For each slice S in [1..6]:
    for each change C in S (DB → backend → web FE → electron FE, interleaved):
        implement C
        ── MICRO-REVIEW: author verifies C against its Acceptance line ──
    run the slice End-to-End verification
    ── SLICE REVIEW GATE: second engineer runs the Standard Review Gate ──   ← HARD STOP
    merge slice S
```

---

## 1. Lock these decisions BEFORE writing any code

These four gates change *what* several slices do. Decide them in a 30-minute kickoff and record the answer at the top of the PR.

- [ ] **G1 — BOM-input model.** Option **A** (flow-driven, recommended: SFG is *never* a `bom_line`; the chain + dispatch are the source of truth — **skip the 101 sfg lines**) vs Option **B** (add `sfg` rows to `bom_line` for costing/MRP). This toggles parts of Slice 4 and the `bom_line.consumed_at_stage` / `mrp.py` work.
- [ ] **G2 — Sorting policy.** Inline (folder/v2 model) vs its own step/JC (backend today). Lock it in the process-classification map (Slice 2) so it never defaults silently.
- [ ] **G3 — WIP shelf-life.** Pick the `WIP_SHELF_LIFE_DAYS` default constant (today everything is 0; `all_sku` has **no** `shelf_life` column to read). Used in Slice 5.
- [ ] **G4 — Bar-Line override.** For the 84 G8 FGs, does `Bar Line Process` replace `Process Category`? (Currently unused.) Affects Slice 7 data cleanup only.
- [ ] **G5 — WIP box & weighing policy (Slice 6).** How is a WIP stage's net SFG split into physical boxes/bags: **operator-weighed** per box (recommended — actual scale weight) vs **fixed box-size** auto-split? And the `box_id` format: mirror `po_box` (`transaction + position`) vs app-supplied 8-digit via `new_short_time_id`. Confirm the **deferred** scope: box now carries `box_id` + producing-JC id + `SFGxxxx` + weight only; full batch/lot genealogy is wired later with the issue-note + lot-picker. Adds one dependency: a QR-image library (`qrcode`, since none is installed).

---

## 2. The Standard Manual Code-Review Gate (apply after EVERY change)

The reviewer must tick **all** of these, with evidence, before the change/slice is accepted. Treat any "can't verify" as a FAIL.

**Correctness**
- [ ] The change does exactly what its Acceptance line states — reviewer ran the command/clicked the UI and saw it.
- [ ] Edge cases: null SFG code, single-stage (pack-only) FG, terminal FG stage, re-run/idempotency.

**The verified gotchas (these tripped the design docs — confirm each):**
- [ ] No code assumes a CHECK constraint on `item_type` (there is **none**; all four `item_type` columns are free-text `TEXT`).
- [ ] The SFG chain-code WRITE is in `job_card_v2.create_job_cards_from_plan` (~line 723), **not** `plan_v2`. No `/job-cards-v2/generate` route was invented.
- [ ] No code reads `all_sku.shelf_life` or `all_sku.batch_strategy` — **neither column exists**. WIP expiry comes from `WIP_SHELF_LIFE_DAYS`; FIFO is the `inventory_batch` default.
- [ ] No `semi_closed` status was added anywhere — partial completion is expressed via batch close + partial dispatch.
- [ ] Keys are passed through `normalise_key()` (NBSP `\xa0` / mojibake) before any exact-string join to `all_sku`.

**Security & access (multi-role auth + cost gate)**
- [ ] Any new SFG/WIP **cost/valuation** field is masked for shop-floor roles (`team_leader`, `qc_inspector`, `floor_manager`, `viewer`) — verified on the **backend** (`response_filters.strip_cost_fields` actually called on the new endpoint) **and** the **client** (`cost-gate.ts`), with matching field names.
- [ ] Role checks use the union-of-roles model (a user may hold multiple roles).

**Data integrity & migrations**
- [ ] Migration is idempotent (`IF NOT EXISTS` / `ON CONFLICT DO NOTHING`) and re-runnable; a rollback path exists.
- [ ] Ingest re-run is a no-op (no duplicate SFG rows; the 24 re-typed bulk articles keep their `sku_id`).
- [ ] The conservation identity still holds (`gross − losses = net SFG`, `is_balanced`); mass-balance predicates were re-checked, not left RM-only.

**No regression & frontend parity**
- [ ] RM/PM path is byte-for-byte unchanged for non-SFG (pack-only) job cards.
- [ ] **Both** frontends were updated in this slice — `web_replica` **and** `frontend_replica` are in sync; neither was deferred.
- [ ] `tsc --noEmit` + `eslint` clean (web); Electron renderer loads with no console errors.

**Tests**
- [ ] A test was added/updated for the change and it passes against a seeded DB.

---

## 3. Slice plan at a glance

| Slice | Capability delivered (testable end-to-end) | DB | Backend | Web FE | Electron FE |
|---|---|:--:|:--:|:--:|:--:|
| **1** | SFG#### exist as catalogued `all_sku` items; UI tags them | ✅ | ✅ | ✅ | ✅ |
| **2** | Process Category → practical operation; Create-WIP checklist on Stage-1 | ✅ | ✅ | ✅ | ✅ |
| **3** | 2-stage routing + the SFG seam (real `SFGxxxx` shown on the handoff) | ✅ | ✅ | ✅ | ✅ |
| **4** | Stage-2 consumes the SFG; mass-balance includes it | ✅* | ✅ | ✅ | ✅ |
| **5** | WIP inventory materialised on close; SFG inventory picker | ✅ | ✅ | ✅ | ✅ |
| **6** | Per-box/bag QR labels at WIP completion; scan-to-verify SFG movement between floors/stages/units | ✅ | ✅ | ✅ | ✅ |
| **7** | PDF/reporting print SFG; routing-gap data cleanup | data | ✅ | ✅ | ✅ |

\* Slice-4 DB work is Option-B only (gate **G1**). Slice 6 hooks the Slice-5 `close_batch` event and runs immediately after it.

---

## SLICE 1 — SFG identity in the catalogue

**Goal:** after this slice, `SELECT count(*) FROM all_sku WHERE item_type='sfg'` returns **343**, the ingest is idempotent, and both UIs render an `(SFG)` tag on an SFG-typed line. The foundation columns every later slice needs also land here.

**DB**
- [ ] `server_replica/app/db/050_sfg_item_type.sql` *(NEW, idempotent BEGIN/COMMIT)* — widen the `item_type` **comments** on `bom_line` & `all_sku` to include `sfg`/`fg` (no CHECK exists; do **not** add one unless you also include `'fg'`). **ADD COLUMN IF NOT EXISTS** `input_code TEXT`, `output_code TEXT` to `job_card_v2` (the `SFGxxxx` link JC1.output→JC2.input). **ADD** `practical_operation TEXT`, `stage_bucket TEXT`, `input_kind TEXT`, `output_kind TEXT`, `input_code TEXT`, `output_code TEXT` to `bom_process_route`. *Acceptance:* `\d job_card_v2` shows the two code columns; `\d bom_process_route` shows the six; existing rows still valid.
- [ ] `server_replica/app/db/051_sfg_seed_catalog.sql` *(NEW)* — load `SFG_Catalog_Plug.csv` (343) into `all_sku`: INSERT 319 net-new, UPDATE-in-place re-type the 24 existing bulk articles. Reconcile the 24-vs-25 off-by-one first. *Acceptance:* 343 `sfg` rows; 24 re-typed kept their `sku_id`; no duplicate `particulars`. *(dep: 050)*

**Backend**
- [ ] `server_replica/app/core/helpers.py` *(NEW helper)* — `normalise_key(s)`: strip NBSP `\xa0`/thin spaces, repair mojibake, collapse whitespace. Called by **all** plug loaders for both the code and the name. *Acceptance:* `normalise_key('SFG0123\xa0')=='SFG0123'`; an NBSP-bearing plug row matches the existing `all_sku` row.
- [ ] `server_replica/app/core/helpers.py` — `next_sfg_code(conn)`: allocate the next `SFG####` above the highest loaded code (seq table or `MAX(substring)`). *Acceptance:* returns a code strictly greater than the max loaded; concurrent calls never collide. *(dep: 051)*
- [ ] `server_replica/app/modules/production/services/master_ingest.py` — add `ingest_sfg_master(conn, file_path)` (bulk-load 343, `item_type='sfg'`). *Acceptance:* 343 sfg rows; re-run is a no-op. *(dep: 050)*
- [ ] `server_replica/app/modules/production/services/master_ingest.py` — `run_master_ingest` (574-630): insert `ingest_sfg_master` into the orchestration **before** route build; resolve plug-CSV paths by **absolute** path off the repo root (not CWD-relative — Workspace-subfolder-CWD gotcha). *Acceptance:* cold ingest from repo root finds all CSVs; warm start backfills only missing artifacts.
- [ ] `server_replica/app/modules/production/services/response_filters.py` — reserve the SFG/WIP cost field **names** in `COST_BEARING_FIELDS` now (enforced in Slice 5) so the gate exists before any cost value flows.

**Frontend — web (`web_replica`)**
- [ ] `web_replica/src/app/modules/job-card/[id]/page.tsx` (type `BomLine.item_type`, line 71) — document the canonical set `RM|PM|SFG|FG`; ensure the line-2763 normalizer preserves `SFG`. Render an `(SFG)` tag where `item_type==='sfg'`. *Acceptance:* an SFG line shows `(SFG)`; `tsc --noEmit` clean.
- [ ] `web_replica/src/lib/cost-gate.ts` (`COST_BEARING_FIELDS` 58-83) — reserve the same SFG/WIP cost keys as the backend (names must match exactly).

**Frontend — Electron (`frontend_replica`)**
- [ ] `frontend_replica/src/modules/production/job-card-detail/styles.css` (861-865) — add `.mat-type-chip.sfg`, `.mat-type-chip.wip`, `.fg` rules so SFG/WIP chips are styled. *Acceptance:* an SFG row's type chip shows the SFG accent colour.

**End-to-end verification (Slice 1):** run ingest on a seeded DB → 343 sfg rows; open a JC with an sfg-typed line on **both** UIs → `(SFG)` tag renders and is styled.

**🔍 Slice 1 Review Gate** — run the [Standard Gate](#2-the-standard-manual-code-review-gate-apply-after-every-change). Slice-specific focus: migration idempotency & rollback; ingest re-run no-op; NBSP normalisation actually exercised; the 24-vs-25 reconciliation is correct; both FEs styled (no deferral).

---

## SLICE 2 — Process classification & the Create-WIP operation checklist

**Goal:** each FG's `Process Category` canonicalises to a practical operation + stage bucket (Sorting=inline per **G2**), and a Create-WIP checklist renders on Stage-1 cards on **both** UIs.

**DB**
- [ ] `server_replica/app/db/050_sfg_item_type.sql` — (columns `practical_operation`, `stage_bucket` already added in Slice 1; nothing new unless split). *Acceptance:* after Slice-2 ingest a Roasting step has `practical_operation` non-null, `stage_bucket='Create WIP'`.

**Backend**
- [ ] `server_replica/app/modules/production/services/master_ingest.py` — add `ingest_process_category_rules(conn, file_path)` reading `ProcessCategory_to_Operation.csv` (9 rules) into `{token → (practical_operation, stage_bucket, inline/terminal)}`; **lock Sorting=inline, Packaging=terminal** (G2). Apply during route build (`ingest_fg_master` 84-179 / `_split_process_category` 58-66). *Acceptance:* SFG-producing FG routes carry `stage='Create WIP'` + non-null `practical_operation`; Sorting maps inline.
- [ ] Surface `practical_operation` / `stage_bucket` on the JC-detail payload so the FE checklist can read it (`job_card_v2.py` serializer).

**Frontend — web**
- [ ] `web_replica/src/lib/processCatalog.ts` (`PROCESS_OPTIONS`/`stageFromProcess` 8-68) — add the `ProcessCategory → {practical_operation, stage_bucket}` mapping; Sorting→inline, Packaging→terminal. *Acceptance:* a Sorting step resolves inline; Packaging resolves terminal.
- [ ] `web_replica/src/app/modules/job-card/[id]/page.tsx` (TabKey/TABS 280-293) — render a **NEW Create-WIP checklist** on Stage-1 (`stage_bucket==='Create WIP'`) cards listing the practical-operation steps the operator confirms; not shown on terminal Final-FG cards. *Acceptance:* WIP-producing Stage-1 shows it; terminal does not. *(dep: backend payload)*

**Frontend — Electron**
- [ ] `frontend_replica/src/modules/production/job-card-detail/job-card-detail.js` — add the Create-WIP checklist in `renderOutputTab()` (954-1068), persisted via the `btnSaveOutput` handler (2910-2959). *Acceptance:* a Create-WIP stage renders the checklist; ticks save and reload.

**End-to-end verification (Slice 2):** ingest → a roasting FG shows `stage='Create WIP'`; open it on both UIs → Create-WIP checklist appears, saves, reloads; a pack-only FG shows none.

**🔍 Slice 2 Review Gate** — Standard Gate + focus: G2 (Sorting) locked consistently across backend map **and** both FE mappings; checklist persistence round-trips; terminal vs Create-WIP branching correct.

---

## SLICE 3 — The 2-stage routing chain + the SFG seam

**Goal:** routed articles get a 2-step chain; on plan approval `JC1.output_code == JC2.input_code == SFG####`; the real `SFGxxxx` shows on the WIP handoff (replacing the anonymous `SFG from JC #N`) on **both** UIs.

**DB**
- [ ] `server_replica/app/db/052_sfg_routing_bom.sql` *(NEW)* — load `JC_Routing_Plug.csv` (1460 rows / 1085 articles) into `bom_process_route`: `stage_seq→step_number`, `stage_bucket→stage`, set `input_kind/output_kind/input_code/output_code`. *Acceptance:* `bom_process_route` gains ~1085 articles; 375 have 2 steps. *(dep: 051)*

**Backend**
- [ ] `server_replica/app/modules/production/services/master_ingest.py` — add `ingest_jc_routing(conn, file_path)` (replaces per-FG route derivation 163-175). *Acceptance:* 2-stage articles have a `step 1 / Create WIP / output_kind=SFG` row and a `step 2 / Final FG` row. *(dep: Slice-2 classification + 050 columns)*
- [ ] `server_replica/app/modules/production/services/job_card_v2.py` — **`create_job_cards_from_plan` (723-890)**: when the routed article is 2-stage, set `JC1.output_code = SFG####` and `JC2.input_code = SFG####` (resolve via `bom_process_route`/`all_sku`). **The WRITE lives here, not in `plan_v2`.** *Acceptance:* after approving a 2-stage plan, the codes match; `JC1.output_kind='SFG'`, `JC2.input_kind='SFG'`. *(dep: 050 code columns, Slice-1 catalogue, routing load)*
- [ ] `server_replica/app/modules/production/services/plan_v2.py` — `_resync_jcs_after_step_change` (949-1059) / `_spawn_jc_for_new_step` (1062-1153): mirror the code wiring so reorder/add-step keeps `SFG####` consistent. *Acceptance:* after an admin reorder, every JC keeps the correct code pairing.
- [ ] `server_replica/app/modules/production/router.py` — `job_card_chain_v2` (GET `/job-cards-v2/{id}/chain`, line 5062): add `input_code`/`output_code` to the SELECT + serialized result (cost strip stays). *Acceptance:* chain JSON carries the codes.

**Frontend — web**
- [ ] `web_replica/src/app/modules/job-card/[id]/page.tsx` — `StageChainTab` (1968-2021, esp. line 2006): render `input_code`/`output_code` (e.g. `SFG0123`) on the handoff edge; add `input_code?`/`output_code?` to the step type. *Acceptance:* the chain shows the concrete `SFGxxxx` between Stage 1 and Stage 2; falls back to `?` only when null. *(dep: chain payload)*

**Frontend — Electron**
- [ ] `frontend_replica/src/modules/production/job-card-detail/job-card-detail.js` — `renderAccountingTabV2()` carry-forward input row (1944-1976): replace `SFG from JC #N` with `${stage.input_code} · ${stage.input_name}`. *Acceptance:* a Stage-2 Inputs table shows `SFG0123 · …` not `SFG from JC #88`.
- [ ] `frontend_replica/src/modules/production/job-card-detail/job-card-detail.js` — `renderStageChainTab` (4399-4489): show `SFG: SFGxxxx` on the inter-stage connector. *Acceptance:* the chain tab shows the linking code.

**End-to-end verification (Slice 3):** approve a 2-stage plan → DB codes match; open the chain on both UIs → the real `SFG0123` shows on the handoff; pack-only chain unaffected.

**🔍 Slice 3 Review Gate** — Standard Gate + focus: the WRITE is in `job_card_v2.py:723` (not `plan_v2`); reorder/add-step parity; chain payload + both FE renders consistent; single-stage path untouched.

---

## SLICE 4 — Stage-2 consumes the SFG (BOM lines + consumption accounting)

**Goal:** the Stage-2 card consumes the SFG as its opening input and the mass-balance includes it. **Gate G1 decides the DB part.**

**DB (Option B only — gate G1)**
- [ ] `server_replica/app/db/050_sfg_item_type.sql` — *Option B only:* `ADD COLUMN IF NOT EXISTS consumed_at_stage TEXT` to `bom_line`. *Under Option A (recommended): skip — SFG is never a `bom_line`.*
- [ ] `server_replica/app/db/052_sfg_routing_bom.sql` — *Option B only:* load the 101 `sfg` lines from `JC_BOM_Plug.csv`. *Acceptance (B):* `count(*) FROM bom_line WHERE item_type='sfg' = 101`.

**Backend**
- [ ] `server_replica/app/modules/production/services/master_ingest.py` — `ingest_jc_bom(conn, file_path)` loading `JC_BOM_Plug.csv` (5017 lines; rm2784/pm1759/fg373/sfg101). *Acceptance:* counts match; the 101 sfg lines tagged `consumed_at_stage='Final FG (opening RM)'`. *(dep: G1; Slice-1 codes)*
- [ ] `server_replica/app/modules/production/services/job_card_v2.py` — `upsert_consumption_lines` (432+): accept `input_kind='SFG'`/`'WIP'` + nullable `bom_line_id` + `source_dispatch_id`. *Acceptance:* an SFG consumption row inserts keyed by `SFG####` without raising. *(dep: 050)*
- [ ] `server_replica/app/modules/production/services/mrp.py` — *Option B only:* treat `item_type='sfg'` availability from `inventory_batch` `item_type='wip'`. *Acceptance (A):* MRP unchanged; *(B):* sfg lines resolve on-hand from WIP.

**Frontend — web**
- [ ] `web_replica/src/app/modules/job-card/[id]/page.tsx` — `articles` useMemo (2751-2771) + Material-Consumption grid (4364-4369) + Returned-to-store grid (4719-4724): render the SFG line with its `SFGxxxx` code + `(SFG)` tag. **CRITICAL:** revisit the `isRmKey`/`!== 'PM'` mass-balance predicates (lines **423, 3180, 3377, 3943**) so SFG inputs are counted on the canonical-input side. *Acceptance:* the SFG input is included in is-balanced and computes correctly.
- [ ] `web_replica/src/app/modules/job-card/[id]/outputAccounting.ts` — `ConsumptionLineLike` (22-29) + `consumptionStateFromDetail` (91-103): add `input_kind?`. *Acceptance:* a saved SFG consumption qty re-hydrates keyed by `SFGxxxx` on reload (no blanking).

**Frontend — Electron**
- [ ] `frontend_replica/src/modules/production/job-card-detail/job-card-detail.js` — `btnSaveAcctV2` consumption collector (2342-2353) + `consVar` index (1880-1883): use the real `SFG####` for `data-cons-sku`/`material_sku_name`. *Acceptance:* save writes `input_kind='SFG'`, `material_sku_name=SFGxxxx`; variance chip resolves by code.
- [ ] `frontend_replica/src/modules/production/job-card-detail/job-card-detail.js` — v1 consumption handler (3080-3105): don't hardcode `item_type:'rm'` (3091); derive from the row. *(low priority)*

**End-to-end verification (Slice 4):** a Stage-2 JC shows the SFG input line; mass-balance is correct; save→reload round-trips on both UIs.

**🔍 Slice 4 Review Gate** — Standard Gate + focus: **the mass-balance predicate fix is the highest-risk change in the whole project — review lines 423/3180/3377/3943 line-by-line**; conservation identity holds; A-vs-B (G1) applied consistently DB↔backend↔FE; round-trip keying by code.

---

## SLICE 5 — WIP inventory materialisation + SFG inventory picker (the runtime heart)

**Goal:** closing/dispatching a Stage-1 batch **materialises a WIP `inventory_batch`** and a synthetic downstream SFG consumption + floor movement; Stage-2 issues from it via a **new SFG inventory picker**. This is the single genuinely-new runtime behaviour.

**DB**
- [ ] `server_replica/app/db/*` — **no schema change** (`inventory_batch`/`floor_inventory` already allow `wip`). Re-read `045_extend_batch_view.sql`: only if the `job_card_batch_v2` VIEW filters `output_kind`/`item_type` with a hardcoded list, add `SFG`. *Acceptance:* `pg_get_viewdef('job_card_batch_v2')` has no filter excluding SFG.

**Backend**
- [ ] `server_replica/app/modules/production/services/inventory_service.py` — `create_wip_batch(...)` (app-supplied 8-digit `batch_id` via `new_short_time_id`, `item_type='wip'`, `source='PRODUCTION'`, `AVAILABLE`, CREATED event) + `get_sfg_on_hand(...)`. *Acceptance:* inserts a wip batch with future `expiry_date`, app id, logged event.
- [ ] `server_replica/app/modules/production/services/inventory_service.py` — `WIP_SHELF_LIFE_DAYS` constant (G3); `expiry = manufacturing_date + WIP_SHELF_LIFE_DAYS` (no `all_sku.shelf_life` read). + optional `item_type` filter on `get_available_batches` (~116). *Acceptance:* non-zero expiry; `get_available_batches(item_type='wip')` returns only WIP.
- [ ] `server_replica/app/modules/production/services/job_card_batch_v2.py` — **`close_batch` (170-467)**: when `output_kind ∈ ('SFG','WIP')` and dispatch>0: (1) `inventory_service.create_wip_batch(...)` for net (gross−losses); (2) write synthetic downstream consumption (`input_kind='SFG'`, `bom_line_id=NULL`, `source_dispatch_id`, `material_sku_name=SFGxxxx`); (3) floor_movement. Extend the `FOR UPDATE` jc SELECT (~267) to fetch `entity`, `floor`, `output_code`. *Acceptance:* closing a Stage-1 SFG batch creates exactly one wip batch (qty=net), one floor_movement, one downstream consumption row. *(dep: Slice-3 output_code; create_wip_batch)*
- [ ] `server_replica/app/modules/production/services/job_card_v2.py` — `dispatch_to_next` (1579-1647): apply the **same** materialisation on the manual path (shared helper). *Acceptance:* manual dispatch yields identical side effects to close.
- [ ] `server_replica/app/modules/production/services/floor_tracker.py` — `move_material`/`_ALLOWED_TRANSITIONS`: add the `production_floor → wip/sfg store` transition + a WIP bucket in `get_floor_summary`. *Acceptance:* a Stage-1 close logs a WIP floor_movement; invalid transitions still rejected.
- [ ] `server_replica/app/modules/production/router.py` — **NEW** `GET /job-cards-v2/sfg-inventory` mirroring the RM/PM picker but sourcing `inventory_batch WHERE item_type='wip'` (params: entity, sku_name, floor_id). *Acceptance:* returns only WIP rows for the SKU/entity.
- [ ] `server_replica/app/modules/production/services/response_filters.py` — add the SFG/WIP cost keys to `COST_BEARING_FIELDS` **and** ensure the new endpoint + `/chain` route their response through `strip_cost_fields(role)` explicitly. *Acceptance:* a `team_leader` GET returns no cost key; a unit test asserts the strip is invoked.

**Frontend — web**
- [ ] `web_replica/src/app/modules/job-card/[id]/page.tsx` — **NEW `SfgInventoryPicker`** (mirror the additive/RM-issue picker, 3603-3640) sourced from `/api/v1/production/sfg-inventory`; selecting a WIP batch records the SFG input on the Stage-2 JC. Add the `api/v1` proxy route if needed. *Acceptance:* picker lists WIP batches w/ qty; selection records the input; `tsc`+`eslint` clean.
- [ ] `web_replica/src/lib/cost-gate.ts` — confirm the reserved keys (Slice 1) match the final backend names.

**Frontend — Electron**
- [ ] `frontend_replica/src/modules/production/job-card-detail/job-card-detail.js` — `renderProductTab()` (617-716)/`renderIndentTable` (718-839): add an **SFG indent section + picker** (`section_2c_sfg_indent`) sourced from the wip-batch endpoint. *Acceptance:* Stage-2 Product tab shows an "SFG Indent" table with a "Select Lots" picker listing WIP batches.
- [ ] `frontend_replica/src/modules/production/job-card-detail/job-card-detail.js` — `renderAccountingTabV2()` stage banner (1893-1905) + output title (1980-1982): append `SFGxxxx · name` for non-last stages. *Acceptance:* intermediate stages show the SFG code; last stage reads FG.

**End-to-end verification (Slice 5):** open a Stage-1 batch → close it → assert exactly one `inventory_batch(item_type='wip', sku=SFGxxxx, qty=net)`, one floor_movement, one downstream consumption; open Stage-2 on both UIs → SFG picker lists that batch; a `team_leader` sees **no** cost field.

**🔍 Slice 5 Review Gate** *(the heaviest)* — Standard Gate + focus: the cost gate is enforced on the backend **and** client for the new endpoint (security); the WIP batch is created exactly once (no double materialisation across close **and** dispatch paths — shared helper); FIFO/expiry correct; conservation holds; both pickers parity.

---

## SLICE 6 — WIP-completion QR box labels & physical SFG movement verification

> **Additional step (your request).** Runs **immediately after Slice 5** — it hooks the same `close_batch` / `dispatch_to_next` event. Scope **now**: at every WIP-stage completion (one stage, or many across different floors/units, before final packing) split the produced SFG into weighed boxes/bags, print a QR label per box, and scan those QRs at the next floor/stage/unit to physically cross-verify the SFG movement. **Deferred (your call):** full batch/lot genealogy (box→box→lot) — wired later when the issue-note + lot-picker align. The `box_id` + transaction-JC-id logic **mirrors the existing `po_box` flow** (`qr_service.receive_material_via_qr`, `scanned_box_ids`, the transfer module's Scan-Time Box-ID Reconciliation).

**Goal:** closing a WIP stage that produced net SFG lets the operator split the output into weighed boxes, mint a `box_id` + QR per box (carrying the **producing job-card id** + `SFGxxxx` + weight), and **print labels**; at the next floor/stage/unit those QRs are scanned to receive the SFG, **rejecting any box whose SFG / source-JC doesn't match** — exactly mirroring the inbound `po_box` scan. Per-box weights sum back to the Slice-5 WIP batch qty.

**DB**
- [ ] `server_replica/app/db/053_sfg_box.sql` *(NEW)* — table `sfg_box`, the production-side mirror of `po_box`: `box_id TEXT PK`, `job_card_id` (the producing WIP JC = the transaction JC id), `job_card_number`, `sfg_code`, `entity`, `floor`, `stage_bucket`, `box_number INT`, `total_boxes INT`, `net_weight NUMERIC(15,3)`, `gross_weight NUMERIC(15,3) NULL`, `status TEXT` (`PRINTED→DISPATCHED→RECEIVED→CONSUMED`), `source_inventory_batch_id`, `received_into_job_card_id NULL`, `created_at`. **Leave `lot_number` / `parent_box_id` nullable now (batch-traceability deferred — do not wire them).** Indexes on `(job_card_id)`, `(sfg_code,status)`. *Acceptance:* insert/select works; `SUM(net_weight)` per `job_card_id` reconciles to that JC's WIP `inventory_batch` qty.
- [ ] (reuse) `inventory_batch` already holds the WIP qty (Slice 5) — `sfg_box` rows are the *physical sub-split* of one WIP batch; **no change** to `inventory_batch`.

**Backend**
- [ ] `server_replica/app/modules/production/services/sfg_box_service.py` *(NEW)* — `create_wip_boxes(conn, job_card_id, boxes)`: mint `box_id`s (format `SFGB-{job_card_number}-{n}` or app-supplied via `new_short_time_id` — **gate G5**), validate `Σ box weights ≈ net SFG` within tolerance, insert `sfg_box` rows `status='PRINTED'`, link `source_inventory_batch_id`. *Acceptance:* N rows; weights sum to net within tolerance; re-run is idempotent.
- [ ] `server_replica/app/modules/production/services/sfg_box_service.py` — `scan_receive_sfg_box(conn, downstream_job_card_id, box_ids)`: **mirror `qr_service.receive_material_via_qr`** but against `sfg_box`: look up the box → verify `sfg_code` matches the downstream JC's expected input SFG **and** the producing JC is the chain predecessor → mark `RECEIVED/CONSUMED`, set `received_into_job_card_id`, append to the downstream JC's scanned-SFG ids, debit the WIP `inventory_batch`. Reject `wrong-SFG` / `wrong-source-JC` / `already-consumed` with the same reject shape as the `po_box` path. *Acceptance:* a correct box is accepted and debits WIP; a mismatched box is rejected with a reason.
- [ ] `server_replica/app/modules/production/services/job_card_batch_v2.py` `close_batch` (+ `job_card_v2.dispatch_to_next`) — after the Slice-5 WIP materialisation, call `create_wip_boxes` when the operator supplies per-box weights (or auto-split by box-size, gate G5). *Acceptance:* closing a WIP stage with box weights produces `sfg_box` rows tied to that JC, summing to net.
- [ ] `server_replica/app/modules/production/services/label_service.py` *(NEW, or extend `job_card_pdf.py`)* — `wip_box_labels_pdf(boxes)`: fpdf2 label sheet, **one label per box** with `QR(box_id)`, `SFGxxxx`, JC number, net weight, floor, date. Render the QR with the new `qrcode` lib (+ `Pillow`, already present). Use fpdf2 `output(dest="S")` (the pyfpdf 1.7.2 gotcha). *Acceptance:* returns non-empty PDF bytes with one scannable QR per box.
- [ ] `server_replica/app/modules/production/router.py` — NEW endpoints: `POST /job-cards-v2/{id}/wip-boxes` (create + return label PDF), `GET /job-cards-v2/{id}/wip-boxes`, `POST /job-cards-v2/{id}/scan-sfg-boxes` (receive-verify), `GET /sfg-boxes/{box_id}` (lookup, mirror `GET /boxes/{box_id}`). Route responses through `strip_cost_fields` (labels carry no cost). *Acceptance:* a print→scan round-trip works across two JCs.
- [ ] `server_replica/requirements.txt` + `pyproject.toml` — add `qrcode` (Pillow already pinned). *Acceptance:* `import qrcode` works; QR renders in the label PDF.

**Frontend — web**
- [ ] `web_replica/src/app/modules/job-card/[id]/page.tsx` — on a Create-WIP / intermediate JC at completion, a **"Boxes & Labels"** panel: operator enters per-box weights (or accepts the auto-split), POSTs `wip-boxes`, opens/prints the label PDF. On a downstream JC, a **"Scan SFG boxes"** action (reuse the existing RM box-scan UI) listing accepted/rejected boxes. *Acceptance:* print labels from Stage-1; scan them into the next stage; a foreign box is rejected.
- [ ] (reuse) the existing box-scan component already used for RM boxes — no new scanner library.

**Frontend — Electron**
- [ ] `frontend_replica/src/modules/production/job-card-detail/job-card-detail.js` — same two surfaces: a **"Print Box Labels"** section on the WIP output/accounting tab (per-box weight rows → print), and a **"Scan SFG Boxes"** picker on the downstream Product tab (mirror `renderIndentTable` + reuse the `balance-scan` module's scanner pattern). *Acceptance:* parity with web.
- [ ] `frontend_replica/src/modules/production/job-card-detail/styles.css` — box/label chip styles.

**End-to-end verification (Slice 6):** close a WIP stage on floor A → enter 3 box weights → print 3 QR labels; physically move boxes to floor B → scan the 3 QRs into the next JC → all accepted, WIP debited, `Σ weights` reconciles; scan a box from a *different* SFG → rejected. For a 2-WIP-stage chain across units, repeat the print→move→scan at each stage.

**🔍 Slice 6 Review Gate** — Standard Gate + focus: `Σ box weights` reconciles to the WIP batch (no phantom/short weight); **a box can be received exactly once** (no double-consume across floors/units); the reject path covers wrong-SFG / wrong-source-JC / already-consumed; the QR PDF is non-empty under the *installed* fpdf; both FEs print **and** scan; **the deferred batch/lot fields are left nullable and untouched — not half-wired.**

---

## SLICE 7 — Reporting, PDF & data-quality close-out

**Goal:** documents print SFG correctly and the routing-gap data is cleaned so more SKUs can spawn routed JCs.

**Data / DB**
- [ ] Clear the **403** `CATALOG_FG_NOT_IN_FG_MASTER` and **238** `BOM_PARENT_NO_ROUTING` gaps by promoting those articles into FG Master with a `Process Category` (dates ⇒ Sorting + Packing). Re-run reconciliation. *(See `Article_Reconciliation_Loop.md`.)*

**Backend**
- [ ] `server_replica/app/modules/production/services/job_card_pdf.py` — `generate_job_card_pdf` (58) / BOM section (98-147): include `input_kind='SFG'` consumption rows and label them; print `SFGxxxx`. *Acceptance:* a Stage-2 PDF lists the SFG input line + qty.
- [ ] `server_replica/tests/...` — *(NEW, but write per-slice too)* close-batch WIP materialisation, synthetic consumption, cost-gate strip, ingest counts, terminal-stage-creates-zero-wip. *Acceptance:* pass on a seeded DB.

**Frontend — web**
- [ ] `web_replica/src/app/modules/job-card/page.tsx` — `PlanMergedCard` stage list (1160-1214) / `JobCardTable` Stage column: optionally surface `SFGxxxx` on a Create-WIP row. **Do not** add a `semi_closed` status. *(needs list endpoint to return `output_code`)*

**Frontend — Electron**
- [ ] `frontend_replica/src/modules/production/job-cards/job-cards.js` — optional: show the SFG code on the list Stage column (parity with web).

**End-to-end verification (Slice 7):** generate a Stage-2 PDF → SFG line prints; re-run reconciliation → 403/238 cleared; no `semi_closed` status anywhere.

**🔍 Slice 7 Review Gate** — Standard Gate + focus: data cleanup is reversible/audited; PDF renders under the installed fpdf (use `output(dest="S")` — pyfpdf 1.7.2 gotcha); list view parity; full regression pass.

---

## 4. Cross-cutting tasks (fold into the slice noted — do NOT batch at the end)

| Task | File | Land in |
|---|---|---|
| `normalise_key()` NBSP/mojibake helper | `server_replica/app/core/helpers.py` | Slice 1 |
| `next_sfg_code()` numbering reservation | `server_replica/app/core/helpers.py` | Slice 1 |
| Plug-CSV absolute-path resolution | `master_ingest.run_master_ingest` | Slice 1 |
| Cost-field **reservation** (names) | `response_filters.py` + `cost-gate.ts` | Slice 1 |
| Cost-gate **enforcement** (strip on new endpoint) | `response_filters.py` | Slice 5 |
| `WIP_SHELF_LIFE_DAYS` constant | `inventory_service.py` | Slice 5 |
| Add `qrcode` dependency (QR-image render) | `requirements.txt` + `pyproject.toml` | Slice 6 |
| Backend tests (one per behaviour) | `server_replica/tests` | every slice + Slice 5/6/7 |

---

## 5. Traceability & Definition of Done

- **Source of truth for changes:** `SFG_JobCard_Change_Checklist.xlsx` (100 rows / 60 files; this playbook executes its 57 actionable rows **plus** the Slice-6 QR-box additional step). The 7 design steps map to slices: STEP 0→Slice 1 DB; STEP 1→Slice 1; STEP 2→Slice 2; STEP 3→Slice 3; STEP 4→Slice 4; STEP 5→Slice 5; **Slice 6 = the additive WIP-QR step (hooks Slice 5's close event; not one of the original 7 design steps)**; STEP 6→Slice 7.
- **Definition of Done (whole project):** all **7** slice gates passed; a plan approved for a 2-stage article produces a working `RM → Create-WIP(SFG####) → Final-FG` chain with materialised WIP inventory, correct mass-balance, cost masked from shop-floor roles, **per-box QR labels printed at each WIP completion and scan-verified at the next floor/stage/unit**, on **both** web and Electron; ingest is idempotent; tests green; the 403/238 routing gaps cleared.
- **Never:** finish all backend then start frontend; merge a slice with a failing gate; add a `semi_closed` status; trust a CHECK on `item_type` (there is none); read `all_sku.shelf_life`/`batch_strategy` (they don't exist); half-wire the deferred box batch/lot genealogy in Slice 6.
