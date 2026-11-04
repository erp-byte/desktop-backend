# CS02 Change Material BOM: Fit-Gap vs Candor ERP

**Date:** 2026-10-30
**Benchmark:** `CS02_Change_Material_BOM.md` (§ numbers below are that spec's; "CS01" means its referenced CS01 rules)
**Our system, source of truth:** code in `backend/` and `frontend/` (branch `fix/merge-floor-name`, HEAD `bec27e5`). Migrations under `backend/app/db/` are read as the intended schema. **The live RDS database was not queried.** Live grants, index existence and data are listed as Open Questions.
**Scope note:** `scripts/refresh_bom_from_tally.py` is described in `bom_write_service.py:20-33` but is **not in the repo**. Its behaviour is taken from that docstring only.

## 0. What "our system" is (evidence inventory)

| Object | Where | Notes |
|---|---|---|
| `bom_header` | `production_schema.sql:38`, `production_migrate.sql:9-104`, `028:29`, `031`, `058`, `061` | One row **per version**. `bom_id` is new per version. `version`, `is_active`, `effective_from/to` (never read by resolvers), `entity`, `factory`, `allowed_balance_tolerance_pct` and 15 classification columns. No `created_by`, no change ref. |
| `bom_line` | `production_schema.sql:58`, `production_migrate.sql:28-29`, `050:40`, `sap_mm_align.sql:91` | `material_sku_name` TEXT (no FK), `item_type`, `quantity_per_unit NUMERIC(15,3)`, `uom` TEXT, `loss_pct`, `godown`, `can_use_offgrade`, `offgrade_max_pct`, `unit_rate_inr`, `process_stage`, `staging_method`, `consumed_at_stage` |
| `bom_process_route` | `production_schema.sql:77`, `050:31-36` | Steps per `bom_id`. No key to `bom_line` (`bom_aggregate_service.py:11-25`) |
| `bom_amendment_request_v2` | `028:80`, `032` (checker notes) | Maker-checker ledger. 13 request types, 3 of them master-BOM changes |
| `job_card_bom_change` | `115_job_card_bom_change.sql:33` | Per-job-card add/remove. Never writes `bom_line` |
| Unique indexes | `032_b11_polish.sql:39,58`; `041:133-154` | One active header per literal `fg_sku_name`. 041 **skips** creation if duplicates exist |

**Every path that writes BOM master data:**

| # | Path | Permission | Mode | Audit |
|---|---|---|---|---|
| W1 | `POST /api/v1/bom` (`bom/router.py:156`, `bom_write_service.py:250`) | `bom.create`, admin only (`095`) | Strict create, 409 if an active BOM exists | None (`created_by` returned, not stored) |
| W2 | `POST /production/plans-v2/bom` (`router.py:5638`, `plan_v2.py:317`) | `production.plans.create` (planner as seeded, `auth_schema.sql:240-248`) | **Supersede**: deactivates the active BOM, inserts a new one without a route | None |
| W3 | Amendments `permanent_bom_add/remove/qty_change` (`amendments_v2.py:710`) | Hard-coded roles (`router_amendments.py:40-77`) | New version per approved request | Request row only |
| W4 | NPD promote/convert (`npd_service.py:152`, `npd_dev_service.py:864`) | `sample.npd.promote` / `sample.convert` | Supersede by exact name | None |
| W5 | `master_ingest.py` (`:159`, `:660`, `:827`, `:927`) | Ingest job | Insert. Route delete + reinsert | None |
| W6 | `bar_line_service.py:191-206`, `seam_mint_service.py:466-473` | Service code | Route delete + reinsert / update in place | None |
| W7 | `scripts/sync_fg_master.py:303`, `scripts/promote_fg_master_gaps.py:193-241` | Shell | Header overwrite in place | Log line only |
| W8 | `refresh_bom_from_tally.py` (not in repo) | Shell | Deletes rm/pm lines the sheet lacks | Not evidenced |

---

## 1. Executive Summary

**Fit score (64 checkpoints in §2):**

| Status | Count | % |
|---|---|---|
| Fully met | 7 | 10.9% |
| Partial | 16 | 25.0% |
| Missing | 29 | 45.3% |
| Not evidenced | 2 | 3.1% |
| Our advantage | 2 | 3.1% |
| Acceptable deviation | 8 | 12.5% |

Against the benchmark points only, excluding Our advantage and Acceptable deviation (54): **13.0% fully met, 29.6% partial, 53.7% missing, 3.7% not evidenced.** Severity: 13 Critical, 13 High, 18 Medium, 8 Low, 3 Skip.

**Top 5 critical gaps**

| # | Gap | Business risk |
|---|---|---|
| 1 | **The approved path is not the only path.** At least 7 other code paths (W2, W4–W8) replace or overwrite BOMs with no request, no approval and no audit row. Nothing logs BOM changes at field level. (#7, #9, #53, #54) | Unauthorised formulation changes cannot be detected or reconstructed. |
| 2 | **An amendment is applied to a stale base and is not checked against its BOM.** Apply copies from the `bom_id` captured when the request was raised. `bom_line_id` is never checked against that BOM. (#22, #39) | Two pending changes on one FG: the second silently undoes the first. A foreign line id edits the wrong line. An unknown line id bumps the version with no change and is marked "applied". |
| 3 | **Applying an amendment strips the BOM.** The new version has no process route. Lines lose `staging_method` (fall back to `pick`), `consumed_at_stage`, `process_stage` and `unit_rate_inr`. The header loses 15 columns, including `allowed_balance_tolerance_pct` (reset to the column default). (#19–21) | Wrong issuance mode, SFG stage consumption lost, job-card wizard has no steps, R9 close-gate tolerance changes with no one approving it. |
| 4 | **The controlled path cannot be used from the UI.** The form is a raw JSON box. The client requires `material_code/qty`, the server requires `bom_line_id/new_qty`, and `bom_id` is never sent, so final approval fails with `missing_bom_id`. No tests cover it. (#27) | Users are pushed to the uncontrolled paths in gap 1. |
| 5 | **Approved changes don't reach approved plans.** Plan lines store `bom_id` at plan creation, and job cards take it from the plan line. New job cards keep exploding the superseded recipe. (#47) | Wrong RM/PM issued after a correction, for as long as the plan line is open. |

Also Critical: a qty that rounds to 0.000 is accepted by amendments and then never issued (#34). QA/regulatory is not in the approval chain (#1). The Tally refresh can undo an approved add (#53).

**Verdict.** The maker-checker ledger is a sound skeleton. It has the right states, enforces maker ≠ checker1 ≠ checker2, and applies in a single transaction. That is better than SAP's split between CS02 and the change master, because what was approved is exactly what gets applied. Everything around it fails that control. The apply step corrupts the version it creates, the UI cannot drive it, and seven other paths change BOMs with no trail. Old versions are kept, but nothing records when each one was live, so "the BOM as of 12 Sep" can only be guessed from `created_at`. Fix the apply step, close the bypasses and add an audit trigger before building any SAP-style effectivity. ECR/ECO, auth groups, lot sizes, usages and alternatives are overhead at this scale.

---

## 2. Gap Register

| # | SAP section | SAP rule / field / behaviour | Our current state | Status | Business risk if not fixed | Severity | Recommendation |
|---|---|---|---|---|---|---|---|
| 1 | CS02 §3, §4 | R&D requests a formulation change. QA/Regulatory (R6) and Plant Head (R8) approve. | Maker `floor_manager` → checker1 `planner` → checker2 `admin` (`router_amendments.py:40-77`). No QA, R&D or plant-head role in the chain. | Partial | Recipe or label-relevant change goes live with no QA/FSSAI check | Critical | Maker `npd_team`/`planner`. Checker1 `qc_manager` (role exists). Checker2 `production_manager` (role exists, `028`). |
| 2 | §3 R7, §15 CC01 | Change number carries reason, valid-from, status and release | `request_id` + `reason`. No valid-from, no title, no grouping | Partial | Cannot answer "why/when" for a group of changes | Medium | Extend `bom_amendment_request_v2`. Don't build a CC01 clone. |
| 3 | §3 R4, §8.2 step 4 | Impact review of open orders and outgoing-component stock before approval | Nothing shown to checkers | Missing | Approver can't see which plans/JCs or stock are affected | High | Approval panel lists open plan lines/JCs on the version and on-hand of removed materials |
| 4 | §3 R5, §8.2 step 5 | Cost-impact review | No costing. `028` defers costing to a later workstream | Missing | Cost drift invisible | Medium | Show Σ(qty × `unit_rate_inr`) delta once rates are reliable |
| 5 | §4 (R3 executes) | Approved change is keyed in CS02 by a BOM specialist | Approved payload applied automatically in the approval txn (`amendments_v2.py:509-531`) | Our advantage | — | — | Keep. It removes the re-keying gap between approval and execution. |
| 6 | §5.1 `C_STUE_BER`/`C_STUE_WRK` | Change authorisation by plant + usage | `role_name` only, hard-coded. No entity/factory scope (`router_amendments.py:80-82`) | Partial | A cdpl floor manager can amend a cfpl BOM | High | Scope maker/checker by `entity` (and `factory`) |
| 7 | §5.1 `C_STUE_NOH`, §6.1 | Change without history only via a restricted firefighter role | W2, W4–W8 replace/overwrite BOMs with no request, approval or audit | Missing | Silent, unattributable recipe changes | Critical | One write service for all BOM writes. Require a request id or an `override` permission, and log every call. |
| 8 | §5.3 SoD-1 | Changer ≠ releaser | Maker ≠ checker and checker1 ≠ checker2 enforced (`amendments_v2.py:447-486`) | Fully met | — | — | — |
| 9 | §5.3 SoD-2 | No permanent bypass of history/approval | Planner is checker1 for BOM changes **and**, as seeded, holds `production.plans.create`, which lets them supersede a live BOM via `POST /plans-v2/bom` (`router.py:5638-5641`, `plan_v2.py:342-345`). The UI offers it only when the SKU shows 0 lines (`planBuilder.tsx:1887-1950`); the API has no such guard. | Missing | A checker can replace a recipe without a maker or second checker | Critical | `plans-v2/bom` must 409 when an active BOM with lines exists, same as W1 |
| 10 | §5.3 SoD-4 | Change and consumption separated | `floor_manager` raises permanent BOM changes and runs/closes JCs (also maker of `unbalanced_close_override`) | Partial | Change BOM, then consume to match (two checkers mitigate) | Medium | Accept with two checkers. Report changes by makers who closed JCs on that FG within 7 days. |
| 11 | §5.2 | Role templates are configuration | Matrix hard-coded twice (`router_amendments.py:40-77`; `_AmendmentsTab.tsx:42-72`) | Partial | Drift between UI and server. A role change needs a deploy. | Medium | Move to `auth_permission` rows `production.amendments.<type>.{make,check1,check2}` |
| 12 | §5.1 ACTVT 06 | Delete is a separate activity | No delete endpoint. Supersede only. W1's 409 tells users to "Deactivate it" (`bom_write_service.py:276-283`), but no deactivate endpoint exists. | Acceptable deviation | Users can't retire a BOM through a controlled path | Low | Add `deactivate_bom` as an amendment type |
| 13 | §5.1 display | Display authorisation | BOM module admin-only (`095`). Planners read via `GET /plans-v2/bom/{id}` | Acceptable deviation | — | Low | Open `bom.view` to planner/QA when needed |
| 14 | §6.2, §7.1 | Old record kept and delimited at valid-from − 1 | Old header + lines kept with `is_active=FALSE` (`amendments_v2.py:778-782`). `effective_to` not set. No superseded timestamp. | Partial | Cannot prove which recipe was live on a date | High | On supersede, set `effective_to = new.effective_from − 1` and stamp `superseded_at/by` |
| 15 | §6.2 | Future-dated changes | Applied at final approval, immediately | Missing | Packaging/label switch dates handled outside the system | Medium | Add `effective_from` to the request and resolve by date (#58) |
| 16 | §6.3 rule 3 | Many edits under one change number make one version | One request = one edit = one version. A component swap is two requests and two versions, and the live BOM lacks the component between them. | Missing | Plans created in the gap miss a material | High | Payload = list of line changes, applied as one version |
| 17 | §6.3 rules 4–5 | Insert before a later change. Valid-from ≥ creation | No dates, so not applicable | Acceptable deviation | — | Skip | Revisit only if #15 is built |
| 18 | §7.1 | Stable item node across versions | `line_number` copied and acts as a node (`amendments_v2.py:814`). `bom_line_id` changes per version. | Partial | Cross-version compare and where-used are fragile | Medium | Add `bom_line.item_node_id` |
| 19 | §7.1 | New version carries all item data | Line copy omits `unit_rate_inr`, `process_stage`, `staging_method` (→ default `pick`), `consumed_at_stage` (`amendments_v2.py:807-821`) | Missing | Backflush/floor-stock lines become pick. SFG stage assignment lost. | Critical | Copy every column, generically (`INSERT … SELECT` from `information_schema` or an explicit full list with a test) |
| 20 | §7.3 | New header version carries all header data | Copies 10 columns. Drops `sub_group, process_category, business_unit, factory, floors, machines, shelf_life_days, gst_rate, hsn_sac, inventory_group, customer_code, output_uom` (→ `kg`), `bar_line_process, bar_line_routed, allowed_balance_tolerance_pct` (→ column default) (`amendments_v2.py:787-801`) | Missing | R9 close-gate tolerance changes unapproved; routing flags lost | Critical | Same as #19 |
| 21 | §7, CS01 | Routing stays with the BOM version | `bom_process_route` not copied. New version has **zero** steps. | Missing | JC wizard reads the per-step route (`plans.ts:172-204`); bar-line stage logic loses it | Critical | Copy route rows in the same txn |
| 22 | §7, §8.2 step 10 | Change applies to the BOM valid now | Apply copies from the `bom_id` stored at request time, with no base-version check (`amendments_v2.py:742-821`) | Missing | Lost updates. A request raised on an old version restores the old recipe. | Critical | Store `base_bom_id`. At apply, refuse if base ≠ current active, or rebase and re-check. |
| 23 | §7.4 | Non-date-effective fields overwritten **with** change docs | `sync_fg_master.py:293-305` force-overwrites header fields. `master_ingest.py:660` and `bar_line_service.py:191` delete and re-insert routes. No change docs. | Missing | Unexplained header/route changes | High | Audit trigger (#54). Scripts set `app.source`/`app.user`. |
| 24 | §8.2 step 1 | Mandatory reason | UI requires ≥ 20 chars (`_AmendmentsTab.tsx:598`). API makes it optional (`router_amendments.py:96`). | Partial | API/script callers skip the reason | Medium | Make it required server-side for BOM types |
| 25 | §8.2 step 3, §15 CS15 | Where-used before a change | SFG where-used page exists (`frontend/src/app/modules/sfg/where-used`). RM/PM where-used not evidenced. | Partial | Scope of a component change unknown | Medium | RM/PM where-used across active BOMs |
| 26 | §8.1 | Approval workflow with states | `draft → pending_review → pending_final → approved → applied`, plus `rejected`, `withdrawn`. Checker notes (`amendments_v2.py:10-14`, `032`). | Fully met | — | — | — |
| 27 | §8.2 steps 8–11 | Structured change entry on the BOM | Raw JSON textarea. Client requires `material_code/qty/uom`, server requires `bom_line_id/new_qty/qty_per_unit/item_type`. UI never sends `bom_id`, so final approval fails with `missing_bom_id` (`_AmendmentsTab.tsx:602-665`; `router_amendments.py:139-147`; `amendments_v2.py:734-736`). No test covers `permanent_bom_*`. | Missing | Controlled path unusable, so users take #7/#9 | Critical | Line-picker form launched from the BOM detail page. Send `bom_id`. Add tests. |
| 28 | §8.2 step 14 | Consistency check before save | Amendment checks qty > 0 only. No material-master, `item_type`, precision or duplicate check (`amendments_v2.py:224-243, 823-842`). W1 has these (`bom_write_service.py:142-216`). | Partial | Typo'd material or bad type saved. Indent ignores non-rm/pm types (`job_card_v2.py:436`). | High | Reuse W1's `_validate` for amendment payloads |
| 29 | §8.2 step 15 | Atomic save | Apply in the approval txn. Failure raises and rolls back (`amendments_v2.py:394-403, 509-517`). | Fully met | — | — | — |
| 30 | §8.2 step 19, §15 CS14 | Before/after compare | Versions are listed (`bom_aggregate_service.py`). No compare. | Partial | Checkers approve blind | Medium | Diff view between `base_bom_id` and the proposed result |
| 31 | §9 | Key fields immutable | Amendment carries `fg_sku_name`/`entity` forward unchanged | Fully met | — | — | — |
| 32 | §9, §12 | A change touches only its BOM | W2 deactivates with `fg_sku_name ILIKE $1` (`plan_v2.py:342-345`). `_` and `%` in an FG name act as wildcards and deactivate other SKUs' BOMs. | Missing | Unrelated FG loses its active BOM | High | Use the normalised `=` fold from `bom_write_service.py:136-139` |
| 33 | §9, CT-2 | Component on an existing item: delete + add | No replace type | Fully met | — | — | (See #16 for the gap this creates) |
| 34 | §9, CS01 | Quantity change keeps precision | Amendment accepts any `new_qty > 0`. `NUMERIC(15,3)` stores 0.0004 as 0.000, and the indent skips `qpu <= 0` (`job_card_v2.py:440`). The line stays on the BOM and is never issued. W1 refuses this (`bom_write_service.py:173-185`). | Missing | Material silently never issued | Critical | Apply W1's precision rule, and see #61/§7 for base qty |
| 35 | §9 | UoM change with conversion | No amendment type. `uom` is free text. | Missing | UoM fixed only by ingest | Medium | Add `line_field_change` type (UoM in allow-list, validated) |
| 36 | §9 | Scrap %, storage location, staging, substitutes, stage changeable under control | No amendment type for `loss_pct, godown, staging_method, offgrade_*, consumed_at_stage`. Only ingest/scripts change them. | Missing | `loss_pct` drives gross qty, so it changes with no trail | High | `line_field_change` with an allow-list, like `plan_field_change` |
| 37 | §9 | Header base qty / status change | No base qty. Status = `is_active`, flipped only by supersede. | Acceptable deviation | — | Low | — |
| 38 | §9, §18.1 | BOM key = material + plant + usage + alternative | One active BOM per `fg_sku_name` across entity and customer (`032:39`; `plan_v2.py:338-341`) | Partial | cfpl and cdpl cannot run different recipes, and a customer-specific BOM cannot coexist with the generic one | Medium | Only if needed (Q10): add `entity` and `customer_code` to the active key |
| 39 | §11 C-01 | BOM/line existence checked up front | `create_amendment` checks neither `bom_id` nor that `bom_line_id` belongs to it. A foreign line id changes the line with that `line_number` here. An unknown id updates nothing, yet the version bumps and status is `applied` (`amendments_v2.py:114-166, 843-855`). | Missing | Wrong line changed, or a phantom "applied" change | Critical | Validate at create. At apply, assert rows-affected = 1. |
| 40 | §11 C-03, §12 | Concurrent saves serialised | `pg_advisory_xact_lock(hashtext(fg))` + unique indexes (`amendments_v2.py:756-774`) | Fully met | — | — | (Stale base is #22) |
| 41 | §11 C-09/C-13 | Warn about other pending/later changes | None | Missing | Checkers unaware of conflicting requests | Medium | Show pending requests on the same BOM. Block apply on conflict (#22). |
| 42 | §11 C-20 | Recursion check | Not found. `sfg` lines can name SFG codes. | Not evidenced | A cycle loops MRP/indent | Medium | Cycle check on SFG references at save |
| 43 | §11 C-22 | "No data changed" | A qty change to the same value creates a new version | Missing | Version noise | Low | Reject no-op payloads |
| 44 | §11, §18.2 | Exactly one valid BOM resolved | Partial unique index on the literal name. W1 folds case/NBSP, while W3/W4 use exact `=`. `041` skips the index if duplicates exist. `fulfillment_v2.py:816,909` uses `LIMIT 1` with no `ORDER BY`. | Partial | Two active BOMs: an arbitrary one is used | High | Resolve by `sku_id` FK. Confirm the index on RDS (Q5). |
| 45 | §16 | Change behaviour in config | `REQUEST_TYPES`, `TWO_CHECKER_TYPES` hard-coded | Acceptable deviation | — | Low | — |
| 46 | §12 | Pessimistic edit lock + admin unlock | Changes are requests, not edit sessions | Acceptable deviation | — | Skip | Not needed with request-based changes |
| 47 | §13 | Unfirmed planned orders re-explode from the new BOM | Plan lines store `bom_id` at creation (`plan_v2.py:165-185`). JCs take it from the plan line (`job_card_v2.py:1207`). New JCs on existing plan lines keep the superseded version, with no warning and no list. | Missing | Old recipe issued after a correction | Critical | On apply, list open plan lines on the old version. Option to repoint lines with no JC yet. Warn at JC creation if `bom_id` is inactive. |
| 48 | §13 | Production orders keep their own component list | Indents written at JC creation from `bom_line` (`job_card_v2.py:360-445`). In-flight JCs untouched. | Fully met | — | — | — |
| 49 | §13 (CO02) | Order-level component change | `job_card_bom_change`: add/remove per JC chain, `changed_by/at`, undo kept as history, master untouched (`115`) | Our advantage | — | — | Keep |
| 50 | §13 | Cost estimate rerun | No costing | Missing | — | Medium | With #4 |
| 51 | §13, CT-2 | Outgoing-component stock / discontinuation | None | Missing | PM artwork change leaves dead stock | Medium | Show on-hand of removed materials in the approval panel (#3) |
| 52 | §13 | Low-level codes | Not found | Not evidenced | — | Low | Only needed if multi-level MRP is built |
| 53 | §13 | One system of record for composition | The Tally refresh deletes rm/pm lines the sheet lacks unless a JC references them (`bom_write_service.py:20-33`), so an approved `permanent_bom_add` can be undone on the next run. Script not in repo. | Missing | An approved change silently reverts | Critical | Decide the master (Q1). If ERP: refresh writes amendment requests instead of rows. If Tally: disable W3 for rm/pm. |
| 54 | §14 | Field-level change log for every write (CS80) | No audit table or trigger on `bom_header/bom_line/bom_process_route`. Only amendment requests are logged. | Missing | No trail for W2, W4–W8 | Critical | Trigger-based `bom_audit_log` (§7) |
| 55 | §14 | Who created each version | No `created_by` on `bom_header`/`bom_line`. W1 returns `created_by` but never stores it (`bom_write_service.py:308-321, 395-404`). | Missing | Authorship unprovable | High | Add `created_by`, `source` |
| 56 | §14 | Version ↔ change number link | Request stores the source `bom_id` + `applied_version`. `new_bom_id` is returned but not persisted. `bom_header` has no request ref. | Partial | Hard to trace a version to its approval | High | Persist `new_bom_id`. Add `bom_header.change_request_id`. |
| 57 | §14, CS03 | BOM as of a past date | Not possible (no validity resolution) | Missing | Audits and complaints can't be answered from the system | High | With #14 and #58 |
| 58 | §18.2 | Explosion takes a key date | Resolvers use `is_active` only. `effective_from/to` are never read (`plan_v2.py:172-177`; `fulfillment_v2.py:816,909`). | Missing | Validity dates are decorative | High | `resolve_bom(fg, entity, key_date)` |
| 59 | §15 CS20 | Controlled mass change | None. Bulk changes run through unaudited scripts (W7/W8). | Missing | Bulk errors untraceable | Medium | Bulk amendment request (list payload, #16) |
| 60 | CS01 explosion | Component scrap: `qty × (1 + scrap%)` | `reqd / (1 − loss%)` (`job_card_v2.py:444`; `mrp.py:58`) | Acceptable deviation | Over-issue if the source sheets mean SAP-style scrap (Q7) | Medium | Document `loss_pct` = "% of input lost". Confirm with Q7. |
| 61 | CS01 | Fixed quantity (not scaled) | Every line scales with the multiplier (`job_card_v2.py:442`) | Missing | Per-run items (start-up film, labels per batch) are mis-issued | Medium | `bom_line.is_fixed_qty` |
| 62 | CS01 | Operation scrap / Net ID | Route `loss_pct` not used in explosion. The aggregate **sums** step losses instead of compounding them (`bom_aggregate_service.py:129`). | Partial | Display only | Low | Show `1 − Π(1 − lossᵢ)` |
| 63 | CS01 | Alternative item / usage probability | `can_use_offgrade` + `offgrade_max_pct` on the line. v1 MRP uses `offgrade_reuse_rule.max_substitution_pct` by item group and ignores the line value (`mrp.py:64-82`). | Partial | Line setting has no effect | Low | Pick one source |
| 64 | CS01 | Lead-time offset | All RM+PM issued at stage 1 by ops decision (`job_card_v2.py:364-371`) | Acceptable deviation | — | Skip | — |

> Count rule: 64 rows. Statuses tallied from the Status column above.

---

## 3. Field-Level Mapping

| SAP field label | SAP technical | Our field/column | Data type match? | Mandatory/validation match? | Gap / action |
|---|---|---|---|---|---|
| Material | MAST-MATNR | `bom_header.fg_sku_name` TEXT | No: free text, no FK to `all_sku` | Required. Normalised on W1 only. | Add `fg_sku_id` FK |
| Plant | MAST-WERKS | `entity` (cfpl/cdpl CHECK) + `factory` TEXT | Partial | `entity` optional | Make `entity` mandatory |
| BOM usage | MAST-STLAN | — | — | — | Skip (production BOMs only) |
| Alternative | MAST-STLAL | — (one active per FG) | — | — | Only if Q10 = yes |
| BOM number | MAST/STZU-STLNR | `bom_id` SERIAL, new per version | Partial: no stable id | System-assigned | Add `bom_master_id` (§7) |
| Header counter | STKO-STKOZ | `version` INT | Yes | max+1, server-side | — |
| Base quantity | STKO-BMENG | — (implicit 1 FG unit or 1 kg via `all_sku.uom`) | — | — | Add `base_qty`, or widen precision (§6) |
| Base UoM | STKO-BMEIN | `output_uom` TEXT default 'kg' | Partial | None | Copy on amendment (#20) |
| BOM status | STKO-STLST | `is_active` BOOLEAN | Partial | — | Enough for now |
| Lot size from / to | MAST-LOSVN/LOSBS | — | — | — | Skip |
| Lab / design office | STKO-LABOR | — | — | — | Skip |
| Alternative text | STKO-STKTX | `notes` | Partial | — | — |
| Authorization group | STKO/STZU-BEGRU | — | — | — | Low |
| Header long text | Text object | `notes` TEXT | Partial | — | — |
| Valid from | STKO/STPO-DATUV | `effective_from` DATE (header only) | Yes | Defaulted on W1; never read | Use in resolver (#58) |
| Valid to | derived | `effective_to` DATE (header only) | Yes | Never set on supersede | Set on delimit (#14) |
| Change number | AENNR | — (request id exists, not on BOM rows) | — | — | `bom_header.change_request_id` |
| Item node | STPO-STLKN | `line_number` (copied across versions) | Partial | Server-assigned | `item_node_id` |
| Item counter | STPO-STPOZ | `bom_line_id` (new per version) | Partial | — | — |
| Item number | STPO-POSNR | `line_number` INT | Partial (INT vs CHAR4) | UNIQUE(bom_id, line_number) | — |
| Item category | STPO-POSTP | `item_type` rm/pm/sfg | No: material class, not item category | W1 validates; amendment accepts any string | Validate. Add DB CHECK. |
| Component | STPO-IDNRK | `material_sku_name` TEXT | No: no FK | Required; not checked against master | Add `sku_id` FK |
| Component quantity | STPO-MENGE | `quantity_per_unit` NUMERIC(15,3) | Partial: 3 dp with no base qty | > 0. Precision check on W1 only. | NUMERIC(18,6) or base qty |
| Component UoM | STPO-MEINS | `uom` TEXT | No: free text | None | Constrain to a UoM list |
| Fixed quantity | STPO-FMENG | — | — | — | `is_fixed_qty` (#61) |
| Component scrap % | STPO-AUSCH | `loss_pct` NUMERIC(5,3) | Type yes, formula differs | Default 0 | Document (#60) |
| Operation scrap % | STPO-AVOAU | `bom_process_route.loss_pct` (route level) | Partial | — | — |
| Net ID | STPO-NETAU | — | — | — | Skip |
| Production / costing relevance | STPO-SANKA/SANFE | — (indent filters `item_type`) | — | — | Skip |
| Bulk material | STPO-SCHGT | `staging_method = 'floor_stock'` | Partial | CHECK pick/backflush/floor_stock | Copy on amendment (#19) |
| Material provision ind. | STPO-BEIKZ | — | — | — | Skip |
| Production storage location | STPO-LGORT | `godown` TEXT | Partial: free text | None. Amendment add leaves it NULL. | FK to godown list |
| Supply area | STPO-PRVBE | `process_stage` / `consumed_at_stage` TEXT | Partial | None | Copy on amendment |
| Lead-time offset | STPO-NLFZT | — | — | — | Skip (#64) |
| Alternative item group | STPO-ALPGR | `can_use_offgrade` | Partial | — | — |
| Priority / strategy / usage prob. | STPO-ALPRF/ALPST/EWAHR | `offgrade_max_pct` | Partial | Default 0 | Line value ignored by v1 MRP (#63) |
| Discontinuation / follow-up group | STPO-NFEAG/NFGRP | — | — | — | Medium, with #51 |
| Co-product | STPO-KZKUP | — (by-products recorded on JC) | — | — | Not evidenced on BOM |
| Recursive allowed | STPO-REKRS | — | — | — | Cycle check (#42) |
| Purchasing data (N items) | STPO-EKGRP etc. | — | — | — | Skip |
| Variable-size data (R items) | STPO-ROMS1… | — | — | — | Skip |
| Document link (D items) | STPO-DOKNR | — | — | — | Low |
| Item text | STPO-POTX1 | — | — | — | Low |
| Sub-items | STPU | — | — | — | Skip |
| Deletion flag (per version) | STPO deletion ind. | — (line omitted in the new version) | — | — | Adequate under versioning |
| Created by / changed by | STKO-ANNAM/AENAM | — | — | — | Add (#55) |
| History requirement | per-BOM flag | — | — | — | Implicit once all writes go through W3 |
| (no SAP field) | — | `unit_rate_inr` | — | Dropped on amendment | Our extra. Copy it (#19). |

---

## 4. Roles & Access Control Gaps

**Persona mapping**

| SAP persona | Our role | Evidence / note |
|---|---|---|
| R1 MDG Lead | No equivalent in the BOM flow | SKU master via ingest. Not evidenced as a role. |
| R2 R&D / Formulation | `npd_team` | Creates BOMs via NPD promote (W4). Not in the amendment chain. |
| R3 BOM specialist | None (apply is automatic). `admin` for W1. | — |
| R4 Planner | `planner` | Checker1 for BOM amendments. Also supersedes via W2. |
| R5 Cost accountant | No equivalent | — |
| R6 QA / Regulatory | `qc_manager`, `qc_inspector` exist | Not in the BOM chain |
| R7 ECM coordinator | No equivalent | — |
| R8 Plant Head | `production_manager` exists (`028`) | Not in the BOM chain. `admin` is checker2. |
| R9 Procurement | `purchase_manager` | Not consulted |
| R10 Stores | `store_head`, `inventory_manager` | Not consulted |
| R11 Internal auditor | No equivalent | No change report exists |
| R12 Security admin | `admin` | Roles & Permissions UI |
| (none in SAP) | `floor_manager` as **maker** of master changes | The shop floor initiates formulation changes |

**Permission dimensions we lack:** plant/entity scope on changes; activity split (we have `bom.view` and `bom.create`, but no `bom.change` or `bom.delete`, and amendment rights live in code); change-number authorisation group; a controlled override-without-history (instead, uncontrolled paths); auth group / confidentiality; export (not evidenced).

**SoD conflicts present**

| Conflict | Where |
|---|---|
| Checker can bypass the chain | `planner` = checker1 + W2 supersede (#9) |
| Changer = consumer | `floor_manager` = BOM-change maker + JC operator + `unbalanced_close_override` maker (#10) |
| Admin is unrestricted | `_has_role` returns True for every admin (`router_amendments.py:80-82`). Two admins can approve any change. |
| Recipe creation with no checker | NPD promote (W4). Who holds `sample.npd.promote` on RDS is not evidenced (Q3). |

---

## 5. Process Flow Differences

| SAP step (§8.2) | Our step | Difference |
|---|---|---|
| 1 Raise request with reason | JC → Amendments tab → JSON payload + reason (UI ≥ 20 chars) | Raised from a **job card**, not the BOM. JSON entry. |
| 2 Create change number (valid-from) | Request row = change id | No valid-from |
| 3 Where-used | SFG only | RM/PM missing |
| 4 Open orders / stock | — | **Skipped** |
| 5 Cost simulation | — | **Skipped** |
| 6 Approve R2 / R6 / R8 | Checker1 planner → checker2 admin | **QA and plant head missing** |
| 7 New materials | — | Material not checked on add |
| 8–13 Execute in CS02 | Auto-apply on final approval | **Our advantage** (no re-keying), but apply is defective (#19–22, #39) |
| 14 Check BOM | qty > 0 only | Weaker than W1 |
| 15 Save | Same txn, rollback on error | Equal |
| 16 Release change number | Final approval = release | Merged step. Acceptable. |
| 17 Re-explode / re-read orders | — | **Skipped**. Old `bom_id` stays on plan lines (#47). |
| 18 Rerun costing | — | Skipped |
| 19 Verify before/after | — | Skipped |
| — | **Added:** W2/W4–W8 change BOMs outside this flow | Uncontrolled side doors |
| — | **Added:** JC-level BOM change (`115`) | Equivalent to CO02 component edit, with history. Good. |

---

## 6. Calculation & Logic Checks

| Element | SAP | Ours | Verdict |
|---|---|---|---|
| Base qty | `qty / base_qty × order_qty` | `qpu × multiplier`. Multiplier = planned units if `all_sku.uom ≠ 1`, else planned kg (`job_card_v2.py:284-320, 442`) | No base qty, so precision is capped at 0.001 per FG unit |
| Component scrap | `× (1 + scrap%)` | `÷ (1 − loss%)` (`job_card_v2.py:444`, `mrp.py:58`) | Different definition (#60) |
| Fixed qty | Not scaled | Always scaled | Missing (#61) |
| Assembly / operation scrap, Net ID | Header scrap compounds; Net ID swaps assembly for operation scrap | Route `loss_pct` not applied | Partial (#62) |
| UoM conversion | Via material conversion factors | `all_sku.uom` (kg per FG unit) for the FG only. Line `uom` free text, canonicalised by `_canonical_uom`. | Partial / not evidenced for g↔kg |
| Rounding | Rounding qty / UoM decimals | `NUMERIC(15,3)` on storage, half-up | Silent (test 2) |
| Lead-time offset | Shifts requirement date | Stage-1 issuance | Acceptable (#64) |
| Substitutes % | Usage probability, Σ = 100 | Off-grade cap per item-group rule; line % ignored | Partial (#63) |
| Phantom | Exploded through | SFG lines are real WIP stock (`mrp.py` SFG branch) | Acceptable |
| Bulk | Not staged, expensed | `staging_method floor_stock/backflush` | Exists, lost on amendment (#19) |

**Test 1: scrap formula.** FG 1,000 kg, RM 0.250 kg/kg, loss/scrap 5%.
SAP: 250 × 1.05 = **262.500 kg**. Ours: 250 ÷ 0.95 = **263.158 kg** (+0.658 kg, +0.25%). At 20%: SAP 300.000 kg vs ours 312.500 kg (+4.2%). Fine if `loss_pct` means "% of input lost". It over-issues if the Tally/Excel source means "% added on top" (Q7).

**Test 2: precision (Critical).** FG 100 g pouch (`all_sku.uom = 0.1`), plan 10,000 pouches.
- Spice 0.4 g/pouch = 0.0004 kg. SAP (base 1,000 PC, 0.400 KG): **4.000 kg** issued. Ours via amendment: stored **0.000** and skipped by the indent (`job_card_v2.py:440`), so **0 kg issued**. W1 refuses it with `qty_below_precision`.
- Seasoning 1.5 g/pouch = 0.0015 kg. Stored **0.002**, so ours issues **20.000 kg** against an intended 15.000 kg (+33%). **W1 accepts this too**, because its check is only `round(qty,3) ≥ 0.001`.

---

## 7. Data Model Changes Required

Design choice: **keep version-per-header** (full copy per version). It is simpler than SAP's per-item delimiting, `bom_id` is already FK'd from 16 tables, and BOMs number in the thousands. Make the copy complete and the dates real.

```sql
-- 7.1 Provenance and validity on every version
ALTER TABLE bom_header
  ADD COLUMN bom_master_key    TEXT,          -- normalised fg (+entity) lineage key; later -> fg_sku_id FK
  ADD COLUMN change_request_id BIGINT REFERENCES bom_amendment_request_v2(request_id),
  ADD COLUMN created_by        TEXT,
  ADD COLUMN source            TEXT CHECK (source IN ('ui','amendment','plans_v2','npd','ingest','tally_refresh','script')),
  ADD COLUMN superseded_at     TIMESTAMPTZ,
  ADD COLUMN superseded_by_bom_id INT REFERENCES bom_header(bom_id);
-- on supersede: old.effective_to = new.effective_from - 1, old.superseded_at = now()

-- 7.2 Line identity, master FK, precision, fixed qty
ALTER TABLE bom_line
  ADD COLUMN item_node_id  BIGINT,            -- stable across versions; copied by the amendment
  ADD COLUMN sku_id        INT REFERENCES all_sku(sku_id),
  ADD COLUMN is_fixed_qty  BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN created_by    TEXT;
ALTER TABLE bom_line ALTER COLUMN quantity_per_unit TYPE NUMERIC(18,6);  -- check every reader casts safely
ALTER TABLE bom_line ADD CONSTRAINT chk_bom_line_item_type CHECK (item_type IN ('rm','pm','sfg'));

-- 7.3 Request: base version, effectivity, result, multi-line
ALTER TABLE bom_amendment_request_v2
  ADD COLUMN base_bom_id     INT REFERENCES bom_header(bom_id),   -- optimistic concurrency (#22)
  ADD COLUMN effective_from  DATE,                                -- #15
  ADD COLUMN new_bom_id      INT REFERENCES bom_header(bom_id);   -- #56
-- payload_jsonb for BOM types becomes {"changes":[{op:add|remove|qty|field, item_node_id|line_number, ...}]}  (#16)

-- 7.4 Field-level audit for ALL writers (#54)
CREATE TABLE bom_audit_log (
  audit_id    BIGSERIAL PRIMARY KEY,
  table_name  TEXT NOT NULL,                  -- bom_header | bom_line | bom_process_route
  row_pk      BIGINT NOT NULL,
  bom_id      INT NOT NULL,
  op          CHAR(1) NOT NULL CHECK (op IN ('I','U','D')),
  old_row     JSONB,
  new_row     JSONB,
  changed_by  TEXT NOT NULL DEFAULT COALESCE(current_setting('app.user', true), session_user),
  source      TEXT NOT NULL DEFAULT COALESCE(current_setting('app.source', true), 'unknown'),
  request_id  BIGINT,                         -- current_setting('app.request_id', true)
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_bom_audit_bom ON bom_audit_log (bom_id, changed_at DESC);
-- AFTER INSERT/UPDATE/DELETE row trigger on the three tables; each write path does SET LOCAL app.user / app.source.

-- 7.5 One valid BOM per lineage per date (after #58 resolver exists)
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE bom_header ADD CONSTRAINT ex_bom_validity
  EXCLUDE USING gist (bom_master_key WITH =,
                      daterange(effective_from, COALESCE(effective_to,'infinity'), '[]') WITH &&)
  WHERE (is_active OR superseded_at IS NOT NULL);
```

Resolver (replaces every `fg_sku_name ILIKE $1 AND is_active` lookup):
`resolve_bom(fg_key, entity, key_date) → bom_id WHERE bom_master_key = $1 AND effective_from <= $3 AND (effective_to IS NULL OR effective_to >= $3)`.

---

## 8. Implementation Backlog

| Priority | Change | Type | Effort | Depends on |
|---|---|---|---|---|
| P0-1 | Apply step copies **all** header, line and route columns. Regression test compares every column of v(n) and v(n+1). (#19–21) | Backend rule | S | — |
| P0-2 | Validate `bom_id` exists/active and that `bom_line_id` belongs to it, at create and apply. Assert rows-affected = 1. (#39) | Backend rule | S | — |
| P0-3 | `base_bom_id` + refuse apply when base ≠ current active (#22) | DB + Backend | S | — |
| P0-4 | Reuse W1 `_validate` (type, material, precision) for amendment payloads (#28, #34) | Backend rule | S | — |
| P0-5 | `POST /plans-v2/bom` → 409 when an active BOM with lines exists. Fix the ILIKE deactivation. (#9, #32) | Backend rule | S | — |
| P0-6 | `bom_audit_log` trigger + `SET LOCAL app.user/app.source` in W1–W7 (#54, #55) | DB + Backend | M | — |
| P0-7 | Amendment UI: structured form on the BOM detail page that sends `bom_id`. Align keys. Add tests. (#27) | UI + Backend | M | P0-2 |
| P0-8 | Decide the composition master (Tally vs ERP) and disable the other writer for rm/pm (#53) | Workflow | S (decision) / M | Q1, Q2 |
| P0-9 | QA (`qc_manager`) as checker1, `production_manager` as checker2 for BOM types (#1) | Permission | S | — |
| P1-1 | On apply: list open plan lines on the old version. Repoint lines with no JC. Warn at JC create if `bom_id` is inactive. (#47) | Backend + UI | M | P0-1 |
| P1-2 | Multi-change payload → one version (#16, #59) | Backend + UI | M | P0-7 |
| P1-3 | Delimit on supersede (`effective_to`, `superseded_at/by`). Persist `new_bom_id` and `change_request_id`. (#14, #56) | DB + Backend | S | P0-1 |
| P1-4 | Entity scope on maker/checker. Matrix moved to `auth_permission`. (#6, #11) | Permission | M | — |
| P1-5 | `line_field_change` type: `loss_pct, uom, godown, staging_method, offgrade_*, consumed_at_stage` (#35, #36) | Backend + UI | M | P0-7 |
| P1-6 | Impact panel for checkers: open plans/JCs, on-hand of removed materials, pending requests on the same BOM (#3, #41, #51) | UI + Report | M | P1-1 |
| P1-7 | Date resolver `resolve_bom(..., key_date)` + "BOM as of date" view (#57, #58) | Backend + Report | M | P1-3 |
| P1-8 | Server-side mandatory reason for BOM types (#24) | Backend rule | S | — |
| P2-1 | `quantity_per_unit` NUMERIC(18,6) **or** header `base_qty` (#34 root cause) | DB | M | P0-6 |
| P2-2 | `sku_id` FK on lines and header. Resolve by FK. (#44) | DB + Backend | L | ingest changes |
| P2-3 | Future-dated `effective_from` on requests (#15) | Backend | M | P1-7 |
| P2-4 | Before/after diff view (#30) | UI | S | P1-3 |
| P2-5 | `is_fixed_qty` (#61), SFG cycle check (#42), no-op reject (#43) | DB + Backend | S | — |
| P2-6 | `deactivate_bom` amendment type (#12) | Backend | S | P0-7 |
| P3 | RM/PM where-used (#25), cost delta (#4, #50), compounded route loss (#62), single off-grade source (#63) | Report | S–M | — |

---

## 9. Test Cases

| # | Gap | Precondition | Action | Expected result |
|---|---|---|---|---|
| T1 | #22 | FG X v3 active. Requests A (qty line 1 → 0.30) and B (remove line 2), both raised on v3. | Approve A fully, then B | B refused with `stale_base` (or rebased): v5 holds both changes. Never v5 = v3 − line 2 with the qty back at its old value. |
| T2 | #39 | BOM 101 and BOM 202 both active. `bom_line_id` 9001 belongs to 202. | `permanent_bom_qty_change` on `bom_id` 101 with line 9001 | Create rejected with `line_not_on_bom`. No version created. |
| T3 | #39 | BOM 101 active | qty change with `bom_line_id` 999999 | Rejected at create. Never `applied` with no row changed. |
| T4 | #19, #20 | v3 line with `staging_method='backflush'`, `consumed_at_stage='Final FG'`, `unit_rate_inr=120`. Header with `factory`, `floors`, tolerance 0.0001. | Apply any qty change | v4 matches v3 on every column except the changed qty, version, `bom_id`, `bom_line_id`, timestamps |
| T5 | #21 | v3 has 5 route steps | Apply any amendment | v4 has the same 5 steps. JC wizard shows them. |
| T6 | #34 | — | qty change to 0.0004, then to 0.0015 | Both rejected with `qty_below_precision` (or stored exactly after P2-1) |
| T7 | #9 | FG X active with 8 lines; user is planner | `POST /plans-v2/bom` for FG X | 409 `bom_exists`. FG X still active. |
| T8 | #32 | Active BOMs `ALMOND 100G` and `ALMOND_100G` | `POST /plans-v2/bom` for `ALMOND_100G` (with P0-5 allowing it because it has 0 lines) | Only `ALMOND_100G` is affected |
| T9 | #54 | Audit trigger live | Run W2, W3, W5 and `sync_fg_master.py --apply` on one FG | One `bom_audit_log` row per changed row with correct `source` and a non-null `changed_by` |
| T10 | #27 | floor_manager on a JC for FG X | Raise `permanent_bom_qty_change` from the UI, approved by planner then admin | Status `applied`, `new_bom_id` set, v(n+1) active with the new qty |
| T11 | #1, #8 | Maker M = floor_manager | M approves own request. A planner approves as checker2. | Both refused. Only `qc_manager` → `production_manager` (P0-9) can complete it. |
| T12 | #47 | Plan line L on v3 with no JC. v4 applied. | Create a JC from L | JC uses v4 (repointed) or warns "BOM v3 superseded by v4". Never silently v3. |
| T13 | #6 | cdpl floor manager | Raise an amendment on a cfpl BOM | 403 `entity_scope` |
| T14 | #16 | Request with `changes:[remove line 3, add RM-105 0.05]` | Approve fully | Exactly one new version with both changes. No intermediate active version. |
| T15 | #14, #57 | v3 created 2026-09-01, v4 applied 2026-10-15 | Query BOM as of 2026-10-01 and as of 2026-10-20 | Returns v3 and v4. v3 `effective_to` = 2026-10-14. |

---

## 10. Open Questions

Each comes with the query that closes it where one exists. Run against **RDS** (production).

| # | Question | How to close |
|---|---|---|
| Q1 | Is the Tally BOM export still the system of record for RM/PM composition, and does `refresh_bom_from_tally.py` still run? Where does it live, and how often does it run? | Owner answer |
| Q2 | Does that refresh also overwrite `quantity_per_unit`/`loss_pct` from the sheet, or only delete missing lines? | Script source |
| Q3 | Which roles hold `production.plans.create`, `sample.npd.promote` and `sample.convert` on RDS today? | `SELECT r.role_name, p.module, p.sub_module, p.sub_sub_module, p.action FROM auth_role_permission rp JOIN auth_role r USING (role_id) JOIN auth_permission p USING (permission_id) WHERE (p.module,p.sub_module)=('production','plans') AND p.action='create' OR p.module='sample';` |
| Q4 | Has any `permanent_bom_*` request ever reached `applied`? | `SELECT request_type, status, count(*) FROM bom_amendment_request_v2 WHERE request_type LIKE 'permanent_bom%' GROUP BY 1,2;` |
| Q5 | Do `uq_bom_header_active_fg` and `uq_bom_header_fg_version` exist on RDS? | `SELECT indexname FROM pg_indexes WHERE tablename='bom_header';` |
| Q6 | What is the RDS default and spread of `allowed_balance_tolerance_pct`? Is it 0.0001 per the 2026-10-24 decision, while the `028` default is 0.001? | `SELECT column_default FROM information_schema.columns WHERE table_name='bom_header' AND column_name='allowed_balance_tolerance_pct'; SELECT allowed_balance_tolerance_pct, count(*) FROM bom_header WHERE is_active GROUP BY 1;` |
| Q7 | In the source sheets, is `loss_pct` "% of input lost" (our formula) or "% added on top" (SAP)? | Owner answer |
| Q8 | Is v1 `mrp.py` (`production_plan_line`) still called anywhere, or only the v2 indent path? | Route/log check |
| Q9 | Do you need future-dated effectivity (e.g. PM artwork switch on a date), or is "effective on approval" enough? | Owner answer |
| Q10 | Must cfpl and cdpl, or generic and customer-specific BOMs, coexist for one FG name? | Owner answer |
| Q11 | Is a standard cost per FG kept anywhere (Tally)? Should BOM changes show a cost delta? | Owner answer |
| Q12 | Can a user hold more than one role? The server checks a single `role_name`; the UI uses a role set. | `\d auth_user` / user-role table |
| Q13 | Is there any off-system QA/regulatory sign-off for formulation changes today? | Owner answer |
| Q14 | Do any active FG names contain `_` or `%`? | `SELECT fg_sku_name FROM bom_header WHERE is_active AND fg_sku_name ~ '[_%]';` |
| Q15 | Are there active FGs with more than one active header after normalisation (case/NBSP)? | `SELECT lower(btrim(regexp_replace(replace(fg_sku_name, chr(160),' '),'\s+',' ','g'))) k, count(*) FROM bom_header WHERE is_active GROUP BY 1 HAVING count(*)>1;` |
| Q16 | Is there an RM/PM where-used report outside the SFG page? | Owner answer |
| Q17 | Are BOM changes exported or reported anywhere (audit pack)? | Owner answer |

## Findings that change with the answers

| If… | Then |
|---|---|
| Q1 = Tally is master and the refresh runs | #53 stays Critical. W3 for rm/pm should be **disabled** rather than fixed; P0-1…P0-7 shrink to SFG lines and header fields. |
| Q1 = refresh retired | #53 drops to Low. W8 is removed from #7. |
| Q2 = refresh overwrites qty | An approved qty change (#34, #22) is also reverted by Tally, which strengthens Q1 |
| Q3: planners lack `production.plans.create` on RDS | #9 falls to High (the API exposure remains for whoever holds it) |
| Q4 = zero applied | #19–#22 and #39 have not hurt data yet. Fix before P0-7 makes the path usable. |
| Q4 > 0 | Audit those versions for missing route, staging and tolerance now |
| Q5: indexes missing | #44 rises to Critical (duplicate active BOMs possible) |
| Q6 = 0.0001 on RDS | #20 is concrete: every amendment loosens the close gate 10× |
| Q7 = "% added" | #60 becomes Missing / High: over-issue of 0.25% at 5% loss, 4.2% at 20% |
| Q8 = v1 MRP retired | #63 drops to Skip |
| Q9 = no | #15 and P2-3 become Skip. #58 is still needed for history. |
| Q10 = yes | #38 rises to High. Key becomes (fg, entity, customer). |
| Q12 = multi-role | `_has_role` checks the wrong field. #6/#11 rise. |
| Q13 = yes, off-system | #1 drops to High (process exists, not enforced) |
| Q14 or Q15 non-empty | #32 / #44 are live defects, not latent ones |
