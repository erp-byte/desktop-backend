# BOM Action Impact Map — Candor ERP

**Date:** 2026-10-30
**Basis:**
- BOM Module FRD and LLD v0.2 (`2026-10-30-bom-module-lld-v0.2.md`) for the target behaviour.
- Code at `wt-jc-be` `6c91f04` (origin/main, 2026-10-27) for today's readers and writers.
- File:line references are to that commit.

**Purpose:** for every action on a BOM, or that consumes a BOM, which modules are affected, how, why and when, so each one can be analysed before build. Points that need a decision are collected in §5 as X-01 … X-14.

---

## 1. How to read this

| Code | Effect on the module |
|---|---|
| **W** | Writes or changes its data because of the action |
| **R** | Reads the BOM differently afterwards: recomputes, shows a new version, or uses new rows |
| **G** | Gated: the action is refused or blocked in this module, or it starts refusing something |
| **N** | Someone in this module is notified |
| **S** | Deliberately **not** affected: keeps its snapshot of the version it already holds |
| **R\*** | Reads it, but the reader has no status filter today, so it may show drafts or old versions (see X-04) |
| — | No effect |

"When" uses these terms:
- **at save**: inside the action's transaction.
- **next read**: the next time the screen or service loads.
- **next create**: only objects created after the action.
- **from date**: when the effective date arrives; no event fires.

---

## 2. Module inventory (everything that touches BOM data today)

| ID | Module | Code | Uses the BOM for | Writes BOM tables today? |
|---|---|---|---|---|
| M01 | BOM module | `bom/router.py`, `bom/services/bom_write_service.py`, `bom_aggregate_service.py` | List, detail, strict create | Yes (POST /api/v1/bom) |
| M02 | Plan creation | `plan_v2.py:167-197` (stores `bom_id` on the plan line), `:229-257` (route → plan steps), `:292-353` (plans-v2 BOM create); `production/router.py:5711-5762` (GET plans-v2/bom) | Pins the version a plan line uses; snapshots route steps | Yes (supersede) |
| M03 | Planning board + overrides | `fulfillment_v2.py:816,909` (resolve), `:1157-1252`, `:1520-1544` (requirements); `fulfillment_bom_override_v2` (`011:55-77`); PUT `fulfillment-v2/{id}/bom-override` (`router.py:854`) | Requirement per sales-order fulfillment; per-order overrides | No (override table only) |
| M04 | Job-card creation and indents | `job_card_v2.py:291-355` (quantity basis), `:358-491` (indents), `:703-712` (bom_id from plan line), `:856-993` (stage chain from route seam codes), `:1799-1846` (merged groups), `:2932-2943` (chain-edit re-indent); indent tables `017:213-250` | Indents (RM stage 1, PM packing), job-card stage chain | No |
| M05 | Per-job-card BOM changes | `jc_bom_changes.py`; `job_card_bom_change` (`115`, `116`) | Add or remove an article on one job card | No (own table) |
| M06 | Floor requisition | `floor_requisition/services/requisition_service.py:113-195` (reads `bom_line` at `:143`), `rules.py:94-128` | Required qty = the job card's indent gross | No |
| M07 | Job-card close | `jc_accounting_v2.py:78-271` (variance), `:994-1021` (balance gate); `balance_override.py:134` (tolerance from the JC's BOM) | Consumption variance; mass-balance tolerance | No |
| M08 | Amendments | `amendments_v2.py:710-861`, `router_amendments.py`; `bom_amendment_request_v2` | Maker/checker permanent BOM changes (tab hidden) | Yes (new version) |
| M09 | NPD / Sample | `npd_service.py:115-184`, `npd_dev_service.py:858-884`, `promote_approval_service.py`, `sample/services/jobcard_service.py:29-42`, BOM picker (sample boms endpoints) | Trial recipes promoted to live BOMs; sample job cards; "Base BOM" copy | Yes (supersede) |
| M10 | SFG and routing | Views `sfg_master`, `fg_sfg_binding`, `sfg_where_used` (`055:58-134`, **no status filter**); `sfg_catalog_service.py:77`; `seam_mint_service.py:466-478`; `bar_line_service.py:190-206`; `routing_gap_service.py:331-402` via POST `/production/routing-gaps/apply` (`router.py:9372`, permission production/plans/create); scripts `classify_process_categories.py`, `apply_bar_line_override.py`, `mint_sfg_seam.py` | SFG where-used, FG↔SFG chain seam, bar-line routes, routing gaps | Yes (route and header, in place) |
| M11 | Production indents | `production_indent_service.py:186-198` | Internal order copies the active BOM's lines | No |
| M12 | MRP and AI planner | `mrp.py:17-183` (no caller), `ai_planner.py:301-317` | Material need for the LLM planner | No |
| M13 | QC, PDF, audit endpoint | `qc_service.py:24`, `job_card_pdf.py`, `router_audit.py:77-221` (PM qty audit, assumes "per kg of FG") | Display; PM quantity sanity report | No |
| M14 | Master data and ingest | `all_sku`; `master_ingest.py` (runs at boot, `main.py:123`); scripts `sync_fg_master.py`, `promote_fg_master_gaps.py`, `apply_bom_balance_tolerance.py`; new `uom`, `uom_conversion`, `site` (LLD) | SKU and unit validation; loads FG master, BOM lines and routes | Yes (insert and update in place) |
| M15 | Auth / RBAC | `auth/middleware.py`, `auth/services/permission_service.py`, `095_bom_module_rbac.sql`, `response_filters.py` | Who may do what | No |
| M16 | Audit and notifications | New `bom_audit_log`, `bom_approval`, `bom_change_request` (LLD); mail and WhatsApp services | Trail and alerts | New |
| M17 | Ops scripts and reports | `generate_inventory_report.py:230-254` (plan line `bom_id` × qpu), `find_orphan_plans.py:78`, `repair_dispatch_double_count.py:102` | Reports and repair | No |
| M18 | Frontend | `/modules/bom` (`bom/page.tsx`), `planBuilder.tsx` (Planning, SO Creation), plan-list "Materials per step", `job-card/[id]` (material allocation, amendments tab, "BOM version"), prod-indents, sample develop, npd-development, `sfg/where-used` | Screens | Via API |

---

## 3. Action × module matrix

Actions A01–A13 are the target BOM lifecycle (LLD v0.2). A14–A19 are existing order and floor actions that consume the BOM. A20–A21 are system and admin actions.

| Action | M01 BOM | M02 Plan | M03 Board | M04 JC create | M05/M06 JC run | M07 JC close | M08 Amend | M09 NPD | M10 SFG/route | M11 Prod indent | M12 MRP/AI | M13/M17 Reports | M14 Master | M15 Auth | M16 Audit/notify | M18 UI |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A01 Raise change request | W | — | — | — | — | — | — | — | — | — | — | — | — | G | W N | R |
| A02 Create draft | W | G | G | — | — | — | — | W | R\* | G | — | — | R G | G | W | W |
| A03 Open change | W | S | S | S | S | S | W | — | R\* | S | S | S | R | G | W | R |
| A04 Edit draft / dry run | W | — | — | — | — | — | — | — | R\* | — | — | — | R G | G | W | R |
| A05 Discard draft | W | — | — | — | — | — | — | — | R\* | — | — | — | — | G | W N | R |
| A06 Submit | W | — | — | — | — | — | — | — | — | — | — | — | R | G | W N | R |
| A07 QA approve / reject | W | — | — | — | — | — | — | — | — | — | — | — | — | G | W N | R |
| A08 Release (today) | W | R | R | R/S | S | S | — | R | R\* | R | R | R | — | G | W N | R |
| A09 Release (future date) | W | R from date | R from date | R/S | S | S | — | R | R\* | R | R | R | — | G | W N | R |
| A10 Same-day replacement | W | R | R | R/S | S | S | — | — | R\* | R | R | R | — | G | W N | R |
| A11 Withdraw future release | W | — | — | — | — | — | — | — | R\* | — | — | — | — | G | W N | R |
| A12 Obsolete | W | G from date | G from date | G? | S | S | — | — | R\* | G | R | R | — | G | W N | R |
| A13 Edit attributes (released) | W | R | — | R | — | — | — | — | R | — | R | R | — | G | W | R |
| A14 Planner override | — | — | W | R | R | R | — | — | — | — | — | — | — | G | W | R |
| A15 Plan created | R | W | R | — | — | — | — | — | — | — | — | R | — | G | — | R |
| A16 Job card created | R | R | — | W | — | — | — | — | R | — | — | R | R | G | — | R |
| A17 Per-JC BOM change | — | — | — | R | W | R | — | — | — | — | — | — | R | G | W | R |
| A18 Floor requisition | R | — | — | R | W | — | — | — | — | — | — | — | — | G | — | R |
| A19 Job card closed | R | — | — | R | — | W | — | — | — | — | — | — | R | G | — | R |
| A20 Ingest / BOM script run | W | (as A02/A03/A13) | | | | | | | W | | | | W | G | W N | — |
| A21 BOM role granted / revoked | — | — | — | — | — | — | — | — | — | — | — | — | — | W G | ? | R |

---

## 4. Action details

### A01 Raise change request

- **Trigger:** planner, R&D, QA (action `request`).
- **Writes:** `bom_change_request` (open), one audit row.

| Module | Effect | Why | When |
|---|---|---|---|
| M01 BOM | W: the request appears in the BOM owner's queue | FRD §5: every change starts with a reason and a requested date | at save |
| M15 Auth | G: needs `request`; entity of the key must be in the user's entities | Planners may ask but not change (G-06) | at save |
| M16 Notify | N: BOM owner | So the request is picked up | after commit |
| M18 UI | R: change-request list, badge on the BOM | — | next read |

**Not affected:** every consumer. No version changes.

**Check:** today's permanent-BOM amendments start from a job card (`bom_amendment_request_v2.job_card_id`). The new request has only `legacy_amendment_id` and no job-card link.

### A02 Create draft BOM

- **Trigger:** BOM owner on the BOM screen (`create`), NPD promote (`npd_team`), startup ingest and scripts (`system_bom`), or legacy POST /plans-v2/bom (Phase 0: 409 if one exists; Phase 1: wrapper).
- **Writes:** `bom_header` (draft, version max+1 per key), `bom_process_route`, `bom_line` (new `item_node_id`s), audit `create`.

| Module | Effect | Why | When |
|---|---|---|---|
| M14 Master | R G: FG and every component must exist in `all_sku` by id; FG type fg/sfg; line UoM must convert to the component's `stock_uom`; site must exist. Refusals: `unknown_material`, `bad_header_type`, `no_uom_conversion` | Keys, not names (G-01, G-23) | at save |
| M01 BOM | W | One door | at save |
| M02 Plan | G: a plan for this FG is refused with `no_released_bom` until release. Today the plans-v2 BOM is live at once | Drafts are never resolved (BR-13) | next create |
| M03 Board | G: the fulfillment shows "no released BOM" | Same | next read |
| M11 Prod indent | G: an internal order can't resolve a BOM | Same | next create |
| M09 NPD | W: the promoted recipe becomes a **draft**. NPD-to-production handover now waits for QA and the plant head; today it goes live at once (`npd_service.py:151-172`) | FRD O2: every live recipe approved | at save |
| M10 SFG | R\*: the draft's route seam codes show up in `fg_sfg_binding` and `sfg_where_used` straight away, because those views don't filter by status (`055:91-134`) | The views predate versioning | at save |
| M15 Auth | G: `create`; admin refused (no bypass); body entity checked | G-10, G-18, G-11 | at save |
| M16 Audit | W: create rows with source. No notification until submit | G-65 | at save |
| M18 UI | W: create form; the plan builder's "Add BOM" becomes a link to it | — | — |

**Not affected:** open job cards, indents, variance.

**Check:**
- Ingest at boot now creates drafts. Who owns that queue, and how many drafts does one boot produce? (X-11)

### A03 Open change on a released BOM

- **Trigger:** BOM owner (`change`) against a change request. In Phase 1 also amendments approval, bar-line override and recipe-changing scripts.
- **Writes:**
  - Draft v+1: a full copy of released R (every header column, route row, line column and `item_node_id`).
  - The change request moves to `in_progress`.
  - An audit row.

| Module | Effect | Why | When |
|---|---|---|---|
| M01 BOM | W. A second change for the same key is refused (`draft_exists`) | One open draft per key (`uq_bom_one_open_draft`) | at save |
| M08 Amend | W: an approved `permanent_bom_*` request opens a change, edits it and submits it; it no longer writes the BOM directly | One door; fixes G-31 | Phase 1 |
| M10 SFG | R\*: the copied route makes the FG appear twice in where-used (R and the draft) | No status filter (X-04) | at save |
| M02, M03, M04, M06, M07, M11, M12, M17 | S: R stays released, so nothing changes for consumers | A change is invisible until release | — |
| M15, M16, M18 | G `change` · W audit · R draft view | — | — |

**Check:** only one open draft per key. Can a packaging change and a formulation change on the same FG run in parallel? (X-09)

### A04 Edit draft / dry-run check · A05 Discard draft

| Module | Effect | Why | When |
|---|---|---|---|
| M01 BOM | W: header, lines and route rewritten; `lock_version` bumped; `stale_draft` if the screen is out of date. Discard sets status `discarded` and cancels the change request | Optimistic lock; no hard deletes | at save |
| M14 Master | R G: same validation as A02 | — | at save |
| M10 SFG | R\*: draft route edits show in where-used | X-04 | at save |
| M16 Audit / notify | W: field-level diff by `item_node_id`. Discard notifies the requester | — | at save / after commit |

**Not affected:** all consumers. A dry run writes nothing.

### A06 Submit for review

| Module | Effect | Why | When |
|---|---|---|---|
| M01 BOM | W: `in_review`, `submitted_by/at`; full validation again. Lines and route are frozen (child guard: parent must be draft) | What QA approves can't change underneath them | at save |
| M14 Master | R: re-validated, because SKUs or conversions may have changed since the draft was saved | — | at save |
| M16 Notify | N: QA | — | after commit |

### A07 QA approve or reject

| Module | Effect | Why | When |
|---|---|---|---|
| M01 BOM | W: `bom_approval` row. Reject: back to `draft` | FRD FR-CH-04 | at save |
| M15 Auth | G: `release_qa`; approver ≠ creator (`sod_violation`); no admin bypass | SoD-1 | at save |
| M16 Notify | N: plant head on approve, BOM owner on reject | — | after commit |

**Not affected:** consumers.

### A08 Final approval: release effective today

- **Writes:**
  - D becomes `released` with `effective_from` = today.
  - R gets `effective_to` = yesterday and becomes `obsolete`.
  - `is_active` flips by trigger (R false, D true).
  - The change request closes; audit rows for D and R; notifications.

| Module | Effect | Why | When |
|---|---|---|---|
| M01 BOM | W | Release algorithm, LLD §6 | at save |
| M15 Auth | G: `release_final`; QA must have approved (`qa_pending`); approver ≠ creator and ≠ the QA approver; no admin bypass | SoD | at save |
| M02 Plan | R: new plan lines get D's `bom_id` (`plan_v2.py:167-184` stores it on the line). **Existing plan lines keep R.** `create_plan` also accepts a client-supplied `bom_id` (`plan_v2.py:167`) and doesn't check it | The plan line pins the version | next create |
| M04 JC create | R/S: a job card takes `bom_id` from itself or else from its plan line (`job_card_v2.py:703-712`). **Job cards created after the release from older plan lines still use R.** Open job cards keep R | Pinning at plan level, not job-card level | next create |
| M03 Board | R: open fulfillments show D's requirements on next load. **Overrides are keyed by R's `bom_line_id`** (`011:61`). D has new line ids, so the overrides stop matching unless they are remapped through `item_node_id` | Lines are copied with new ids per version | next read |
| M11 Prod indent | R: the next internal order copies D's lines (it resolves the active BOM by FG name, `production_indent_service.py:186-198`) | — | next create |
| M12 MRP/AI | R: the next AI-planner context uses D | — | next read |
| M10 SFG | R\*: where-used lists both R and D (no status filter). `bar_line_process` and `bar_line_routed` are carried in the copy | X-04 | at save |
| M13/M17 Reports | R: `router_audit` PM audit and `find_orphan_plans` see D. `generate_inventory_report` uses the plan line's `bom_id`, so old plans keep R | — | next read |
| M06 Floor req | S: reads the job card's indent lines and the job card's BOM lines (`requisition_service.py:143`) | Job-card snapshot | — |
| M07 JC close | S: variance explodes the job card's own `bom_id`; tolerance comes from the job card's BOM header (`balance_override.py:134`) | Job-card snapshot | — |
| M05 JC changes | S | Per-job-card | — |
| M09 NPD | R: an NPD-sourced change shows as released | — | next read |
| M16 Notify | N: BOM owner, requester, planners of the FG | — | after commit |
| M18 UI | R: version history. New plan cards show D's materials. Job cards created before the release still show R's "BOM version" | — | next read |

**Check:** X-01 (plan-line pinning), X-02 (overrides lost), X-06 (client-supplied `bom_id`).

### A09 Release with a future date

- **Writes:** as A08, except D is released with `effective_from` E later than today. R gets `effective_to` = E − 1 and stays `released`. Both rows have `is_active = true`.

| Module | Effect | Why | When |
|---|---|---|---|
| M02, M03, M11, M12 | R: R is used until E and D from E. **No event fires**: the switch happens at the date boundary, and the board's figures change overnight | Resolution by date | from date |
| Legacy readers (`ILIKE … AND is_active`) | G: they would find two active rows, so future releases stay off until every reader uses `bom_resolve` (LLD D-07) | Migration safety | until M1.5 is complete |
| M04 | R/S: as A08, through plan pinning | — | — |

**Check:** which date picks the version for a plan: the plan's creation date, its production date, or the SO deadline? (X-05)

### A10 Same-day replacement

- **Writes:** R becomes `obsolete` with `effective_to` = R's `effective_from` (today). D is released from today and wins today's resolution because its version is higher.

| Module | Effect | Why | When |
|---|---|---|---|
| M02, M04 | R/S: plans and job cards created earlier today keep R; later ones use D. **Two versions on the floor the same day** | Pinning and snapshots | next create |
| Others | As A08 | — | — |

**Check:** should job cards opened earlier today on R be flagged for review? (X-12)

### A11 Withdraw a future release

- **Writes:** F becomes `withdrawn`. The previous version's `effective_to` is restored from the audit row. Audit and notification.
- **Affects:** M01 only (plus M16 and M18). F never became effective, so no consumer used it. M10 where-used still lists F (X-04).

### A12 Obsolete a BOM with no successor

- **Writes:** R gets `effective_to` ≥ today.

| Module | Effect | Why | When |
|---|---|---|---|
| M02 Plan | G: after the date, plans for the key are refused (`no_released_bom`). A **customer-specific** key falls back to the **generic** BOM without warning (the resolver tries customer, then generic) | Resolver order, LLD §6 | from date |
| M03 Board / M11 Prod indent | G: no BOM for the FG | — | from date |
| M04 JC create | G?: a job card made from an older plan line still uses the pinned obsolete `bom_id`; nothing refuses it today | Plan pinning | next create |
| M10 SFG | R\*: still listed in where-used | X-04 | — |
| M06, M07 | S | Snapshot | — |

**Check:** X-07 (silent fallback to generic), X-08 (job card on an obsolete pinned version).

### A13 Edit attributes on a released version (no new version)

- **Header attributes:** classification, `process_category`, `bar_line_process`, `floors`, `machines`, `hsn_sac`, `gst_rate`, `shelf_life_days`, `notes`.
- **Route attributes:** seam codes, `practical_operation`, `stage_bucket`, `machine_type`, `qc_check`, `std_time_min`.

| Module | Effect | Why | When |
|---|---|---|---|
| M10 SFG | R: changed seam codes move `fg_sfg_binding` and `sfg_where_used` at once | The views read the route live | at save |
| M04 JC create | R: new job cards build their stage chain from the route's `output_code` and `input_code` (`job_card_v2.py:856-993`). Existing chains are unaffected | The chain is built at creation | next create |
| M02 Plan | R: new plans snapshot route steps into `production_plan_step_v2` (`plan_v2.py:229-257`). Existing plans keep their step snapshot | Same | next create |
| M12 MRP | R: off-grade rules match on `bom_header.item_group` (`mrp.py:65-71`). `run_mrp` has no caller, so there is no effect today | — | — |
| M01 / M18 | R: list filters (item group, sub group) | — | next read |
| M16 | W: `attribute_update` audit row | No version, so the audit row is the only record | at save |

**Check:**
- Is `gst_rate` or `hsn_sac` on the BOM read anywhere? No reader was found in BOM code. These may belong on the SKU master instead (X-14).
- Is `std_time_min` attribute-only, or does capacity planning use it?

### A14 Planner override on a fulfillment (existing)

- **Trigger:** PUT `/fulfillment-v2/{id}/bom-override` (production/fulfillment/edit: admin, planner).
- **Writes:** `fulfillment_bom_override_v2` (change qty, loss, UoM or godown; remove a line; add a line), plus `so_revision_log_v2`.

| Module | Effect | Why | When |
|---|---|---|---|
| M03 Board | W R | — | at save |
| M04 JC create | R: from Phase 0, indents for job cards created from this fulfillment use the override; today they ignore it (`job_card_v2.py:406-415`). Existing job cards are unaffected | G-49 | next create |
| M06 Floor req | R: required qty follows the indent gross | — | next create |
| M07 JC close | R: the LLD variance explodes the job card's `bom_id` with **no overrides**, so variance is measured against the master, not what was issued | X-03 | at close |
| M01 BOM | — : the master is unchanged | Order-level change | — |

**Check:**
- Should planner overrides need approval? A qty or loss change per order is a formulation change for that order.
- Overrides are lost on release (A08, X-02).

### A15 Plan created / plan line added (existing, target behaviour)

| Module | Effect | Why | When |
|---|---|---|---|
| M01 BOM | R: `bom_resolve(key, date)` from Phase 1. Today it is `fg_sku_name ILIKE $1 AND is_active` (`plan_v2.py:171-176`) or a client-supplied `bom_id` | One resolver | at save |
| M02 Plan | W: plan line with the pinned `bom_id`; route steps snapshotted (packing-last check) | — | at save |
| M15 | G: `no_released_bom` when nothing is released for the date | BR-13 | at save |

**Check:** X-05 (key date), X-06 (client `bom_id`).

### A16 Job card created from a plan line (existing, target behaviour)

| Module | Effect | Why | When |
|---|---|---|---|
| M04 JC create | W: `job_card_v2.bom_id` comes from the plan line. Indents are written through `explode()` (Phase 2) with `item_node_id` and `material_sku_id`; zero lines are skipped (indent CHECK `reqd_qty > 0`). The stage chain comes from the route. In merged groups, RM sits on the shared stage-1 card, one call per member BOM (`:1799-1846`) | Snapshot point | at save |
| M14 Master | R: unit conversions; pack factor `PCK → KGS` | BR-01, BR-02 | at save |
| M07 JC close | later R: the tolerance and variance base come from this `bom_id` | — | at close |

### A17 Per-job-card BOM add/remove (existing)

| Module | Effect | Why | When |
|---|---|---|---|
| M05 | W: `job_card_bom_change`; `changed_by_user_id` added in Phase 1 | — | at save |
| M04 | R W: indents for this job card's stages. An added article carries an absolute operator-entered qty (`jc_bom_changes.py:676-706`) | Order-level only | at save |
| M06 | R: the added article becomes requestable | — | next read |
| M07 | R: `explode()` doesn't know the added article, so variance has no baseline for it, and a removed article still has one | X-03 | at close |
| M14 | R: the article must exist in `all_sku` (`jc_bom_changes.py:432-437`) | — | at save |
| M01 | — : the master is unchanged | — | — |

### A18 Floor requisition raised (existing)

- **M06** W: the requisition.
- **M04** R: the job card's indent gross.
- **M01** R: `bom_line` item type for the job card's BOM (`requisition_service.py:143`).
- BOM releases never change an open requisition.

### A19 Job card closed (existing, target behaviour)

| Module | Effect | Why | When |
|---|---|---|---|
| M07 | W: variance = consumption − `explode(job card's bom_id, actual output)`.gross. The balance gate uses the tolerance from the job card's BOM header (0.0001, kept on purpose) | BR-05, LLD R15 | at save |
| M04 | R: issued and returned quantities | — | — |

### A20 Startup ingest or BOM script run

| Module | Effect | Why | When |
|---|---|---|---|
| M14 Master | W through the service as `system_bom`: new FG → draft (as A02); recipe change → change draft (as A03); classification → attribute edit (A13); unmatched SKU → exception report, nothing inserted | One door; G-26 | at boot / at run |
| M10 SFG | W: route codes and bar-line routes via attribute edits, or a change request for delete+insert | — | at run |
| M01, M16 | W N: audit with source `ingest` or `script`; BOM owner's queue | — | at save |
| M15 | G: `system_bom` has no release action | AC-05 | — |

Two writers are **not** in LLD v0.2 §12, and both need moving onto the service:
- POST `/production/routing-gaps/apply` (`production/router.py:9372`, permission production/plans/create, held by planners). It upserts `bom_header` and derives routes through `scripts/promote_fg_master_gaps.promote_one` (`routing_gap_service.py:354-395`).
- `scripts/classify_process_categories.py`, which backfills route `practical_operation` and `stage_bucket`.

**Check:** once the ERP is the recipe master, should ingest still run at every boot (Q-01)? (X-10, X-11)

### A21 BOM role granted or revoked

| Module | Effect | Why | When |
|---|---|---|---|
| M15 | W G: `auth_role_permission` / `auth_user_role`. Refused when admin would be combined with `bom_owner`, `qc_inspector` or `plant_head` | SoD-5 | at save |
| M18 | R: the BOM tile shows once `adminOnly` is dropped and `ROLE_MODULE_SCOPE` has `bom` (`095:56-62`) | — | next login |
| M16 | ?: whether role changes are audited was not found in code | — | — |

**Check:** X-13.

---

## 5. Points to analyse

| ID | Point | Actions | Evidence | Options |
|---|---|---|---|---|
| X-01 | **The plan line pins the BOM version.** Job cards created after a release from existing plan lines still use the old version. FRD §5 says "only new job cards pick up a newly released version" | A08, A09, A10, A16 | `plan_v2.py:167-197`; `job_card_v2.py:703-712` | (a) Re-resolve at job-card creation. (b) Keep plan pinning and show "newer version released" on the plan line. (c) Re-pin open plan lines on release |
| X-02 | **Overrides are lost on release.** They are keyed by `bom_line_id`, and every version has new line ids | A08, A14 | `011:61, 74-77` | Key overrides by `item_node_id`, or remap at release |
| X-03 | **Variance ignores overrides and per-job-card added or removed articles**, so it reports differences the floor was told to make | A14, A17, A19 | LLD §8 variance rule; `jc_bom_changes.py:676-706` | Store the effective component list on the job card and measure variance against it |
| X-04 | **The SFG views have no status filter**, so drafts, withdrawn and old versions appear in where-used and the FG↔SFG binding | A02–A13 | `055:91-134` | Join on `status = 'released'` and the date, or use `v_bom_current` |
| X-05 | **Which date picks the version**: plan creation, production date or SO deadline | A09, A15 | LLD §6 (`on_date` undefined for planning) | Production (plan) date recommended |
| X-06 | **`create_plan` accepts any client-supplied `bom_id`**, including a draft or obsolete one | A08, A15 | `plan_v2.py:167` | Accept only the id `bom_resolve` returns for the date |
| X-07 | **Obsoleting a customer-specific BOM silently falls back to the generic one** | A12 | LLD §6 resolver | Warn at obsolete; or an explicit "no fallback" flag per key |
| X-08 | **A job card can be created on a pinned obsolete version** | A12, A16 | `job_card_v2.py:703-712` | Refuse, or warn and require a reason |
| X-09 | **One open draft per key** blocks parallel changes (packaging vs formulation) | A03 | `uq_bom_one_open_draft` | Keep (simpler, fewer merge conflicts), or allow and merge at release |
| X-10 | **Two BOM writers missing from LLD v0.2**: POST `/production/routing-gaps/apply` (planner permission) and `classify_process_categories.py` | A20 | `router.py:9372`; `routing_gap_service.py:354-395` | Move onto the service; gate on `bom/change` |
| X-11 | **Ingest at boot creates drafts.** Who reviews them, and should it still run at boot? | A02, A20 | `main.py:123` | Run on demand; drafts assigned to the BOM owner |
| X-12 | **Same-day replacement leaves two versions on the floor** | A10 | LLD §6 | Notify the floor manager; list job cards on the replaced version |
| X-13 | **Role changes may not be audited** | A21 | Not found in code | Confirm; add if missing |
| X-14 | **`gst_rate` and `hsn_sac` on the BOM have no reader in BOM code** | A13 | Grep of BOM readers | Move to the SKU master, or confirm a reader |

---

## 6. Phase 0 changes: who notices first

| Change | Module | Who notices | What they see |
|---|---|---|---|
| Plans-v2 BOM create refuses when a BOM exists; needs `bom/create` | M02, M18 | Planners | "Add BOM" returns 409 or 403 instead of replacing the recipe |
| Amendment approval copies every column and the route | M08 | Floor managers, planners, admin | New versions keep their route and the 0.0001 tolerance (tab still hidden) |
| Overrides reach indents | M04, M06 | Stores, floor | Issued qty follows the planner's override, not the master |
| Variance baseline includes loss | M07 | Job-card close reviewers | Reported over-consumption drops by the loss % |
| One quantity basis | M04 | Stores, floor | For SKUs where `all_sku.uom` ≠ 1, indents change by the pack factor (Q-16) |
| Body entity check; unit rates hidden; admin BOM grant narrowed | M15, M01 | Admins, cross-entity users | 403 on the other entity; no rates on the BOM screen without `view_cost` |
