# CS01 Create Material BOM: Fit-Gap vs Candor ERP

**Date:** 2026-10-30  
**Benchmark:** `CS01_Create_Material_BOM.md` (§ numbers below are that spec's)  
**Our system, source of truth:** code in `wt-jc-be` (backend, HEAD `6c91f04` = origin/main, 2026-10-27) and `wt-jc-fe` (frontend, HEAD `99f67ed`). Migrations under `app/db/` are read as the intended schema. **The live RDS database was not queried.** Role grants made through the admin UI are not visible in code; only seed grants are counted.  
**Scope note:** only the CS01 spec was supplied. Change (CS02) and display (CS03) behaviour is scored only where CS01 itself requires it (§6.3, §12, §13, §15). The CS02 change-path fit-gap is in `2026-10-30-cs02-bom-change-fit-gap.md` in this folder; it was read against `backend/` at `bec27e5`, an older checkout.  
**Interactive version:** https://claude.ai/artifact/8Xe5hfaaX7VeFTXENmwiVV (private until shared)

## BOM write paths found (evidence inventory)

| # | Path | Permission | Mode | Audit |
|---|---|---|---|---|
| W1 | POST /api/v1/bom (`bom/router.py:156`; `bom_write_service.py:250`) | bom.create, admin only (095) | Strict create; 409 if an active BOM exists. No UI caller. | None; created_by returned, not stored |
| W2 | POST /plans-v2/bom (`production/router.py:5790`; `plan_v2.py:292-353`) | production.plans.create (planner) | Supersede: deactivates by ILIKE, inserts 7 header cols, rm/pm lines, no route. Called by the plan builder's “Add BOM”. | App log line only |
| W3 | Amendment approve (`amendments_v2.py:710-861`) | Hard-coded role_name matrix (`router_amendments.py:41-82`) | New version; copies 7 header cols, partial lines, no route. Tab hidden in UI. | Request row (maker/checkers) |
| W4 | NPD draft promote (`npd_service.py:115-184`) | sample.npd.promote.create | Supersede by exact name | sample_audit_log |
| W5 | NPD dev promote (`npd_dev_service.py:858-884`) | Close endpoint + INV_MGR gate | Supersede; entity hard-coded cfpl | Promote request/approval rows |
| W6 | Startup ingest (`master_ingest.py:1493-1655`) | None (server start) | Insert header/lines; route delete + upsert | None |
| W7 | `bar_line_service.py:190-206`; `seam_mint_service.py:466-478` | CLI scripts | Route delete/reinsert, update in place | None |
| W8 | `scripts/sync_fg_master.py`, `promote_fg_master_gaps.py`, `apply_bom_balance_tolerance.py` | CLI | Header insert / update in place | stdout only |
| W9 | `refresh_bom_from_tally.py` (not in any checkout or git history) | — | Per comments: deletes rm/pm lines the sheet lacks | Not evidenced |

---

## 1. Executive Summary

**Fit score (70 checkpoints in §2):**

| Status | Count | % |
|---|---|---|
| Fully met | 6 | 8.6% |
| Partial | 23 | 32.9% |
| Missing | 32 | 45.7% |
| Not evidenced | 1 | 1.4% |
| Our advantage | 4 | 5.7% |
| Acceptable deviation | 4 | 5.7% |

Against the benchmark points only, excluding Our advantage and Acceptable deviation (62): **9.7% fully met, 37.1% partial, 51.6% missing, 1.6% not evidenced.** 6 of the 32 Missing are rated Skip (SAP features not worth copying). Severity: 13 Critical, 15 High, 16 Medium, 9 Low, 7 Skip.

**Top 5 critical gaps**

| # | Gap | Business risk |
|---|---|---|
| 1 (G-25 / G-06) | The planner's “Save BOM” (POST /plans-v2/bom) deactivates the live recipe and inserts a thinner one. It needs only the planning permission, has no approval, and drops the route, SFG lines and tolerance. | An unauthorized formulation change reaches the floor. |
| 2 (G-43) | quantity_per_unit is per kg on the planning board and in MRP, but per pack in job-card indents whenever all_sku.uom ≠ 1. There is no base quantity. | Wrong production quantities: the two screens differ by the pack factor (2× for a 0.5 kg pack). |
| 3 (G-65 / G-33 / G-26) | Twelve code paths write BOM tables. None stores who made the change, and no audit log or trigger exists. Ingest and scripts edit in place without a new version. | Loss of audit trail on recipes. |
| 4 (G-01 / G-23) | Components and FGs are free-text names with no all_sku check. Three different name matchers are in use, one of them a wildcard ILIKE. | Ghost materials get indented, and a rename orphans lines or deactivates the wrong FG's BOM. |
| 5 (G-49 / G-31) | Planner BOM overrides never reach job-card indents. Approved amendments drop the route and reset the balance tolerance to 0.001. | Wrong quantities issued, and approved changes silently degrade the BOM. |

**Verdict.** The BOM tables hold the core of a production BOM: header, lines, loss % and process route. In places they do more than SAP, with the route inside the BOM, a per-batch mass-balance gate and per-job-card BOM changes with history. The weakness is control, not data: twelve paths write BOMs, only the hidden Amendments tab has approval and segregation of duties, nothing records who changed what, and the path planners actually use can replace a live recipe. The maths also differs by screen, because quantity_per_unit means per kg in one place and per pack in another. Fix control and the quantity basis first. Most of SAP's remaining surface (usages, variable-size, document and non-stock items, sub-items, lead-time offsets) can be skipped.

---

## 2. Gap Register

| # | SAP section | SAP rule / field / behaviour | Our current state | Status | Business risk if not fixed | Severity | Recommendation |
|---|---|---|---|---|---|---|---|
| G-01 | CS01 §2 P1/P5, §10 V-02/V-15 | Header material and every component must exist in the material master. | No write path checks all_sku. POST /bom only checks the name is non-empty (`bom_write_service.py:144-160`). plans-v2 strips only (`plan_v2.py:300-311`). Ingest inserts unmatched lines anyway (`master_ingest.py:917-937`). The planner UI allows free text 'for materials not yet in the all_sku master' (`planBuilder.tsx:1868-1871`). | Missing | A typo or unknown SKU becomes a live BOM line. Stores are indented for a material they can never issue, and stock never reconciles. | Critical | Resolve FG and component names to all_sku ids at write time and reject unknown names. Store sku_id beside the name. |
| G-02 | CS01 §2 P2/P3, §10 V-03/V-04 | Header extended to the plant; header material type allowed as a BOM header. | entity checked against cfpl/cdpl (`bom_write_service.py:147-151`). factory and floors are free text (`bom/router.py:132-134`). The FG's own type in all_sku is never checked. | Partial | An RM or PM SKU can be given a BOM. A BOM can name a factory that does not exist. | High | Require the header SKU's all_sku type to be fg or sfg. Make factory/site a foreign key. |
| G-03 | CS01 §2 P4/P7, §10 V-05/V-17 | Material status (blocked, discontinued) blocks use as BOM header or item. | No status check on any path. all_sku has no status column in `schema.sql:48-58` or the Supabase dump; whether production RDS has one is not evidenced. | Missing | Discontinued RM stays on live BOMs and keeps being indented. | Medium | Add a status to all_sku, then block or warn at BOM write (Q-03). |
| G-04 | CS01 §2 P8, §9.1 UoM step, §10 V-22 | Item UoM must convert to the component's base UoM; explosion converts before MRP. | uom is free text on every path. No conversion table: `job_card_v2.py:259-288` is an alias map only; `jc_accounting_v2.py:37-39` says 'cross-unit conversion is out of scope'; variance skips rows whose UoM differs (220-227). | Missing | A line entered in g or boxes against a kg-stocked RM is indented in the wrong unit. Its variance is skipped without notice. | High | Add a uom master and a uom_conversion table. Validate at write and convert in the explosion helper. |
| G-05 | CS01 §2 P10, §4 (Activate: A = Plant Head), §7.2 step 18 | Formulation approved by R&D and QA, and activation approved by the plant head, before production use. | Only NPD promote has a gate: INV_MGR acceptance (`promote_approval_service.py:60-66`), or business-head approval of the requisition for draft promote (`npd_service.py:122-130`). POST /bom, plans-v2, ingest and scripts make a BOM live on insert (is_active TRUE). | Partial | A recipe nobody approved runs on the floor. QA and the plant head have no sign-off record. This is regulatory exposure for food and agrochem. | Critical | Insert new BOMs as draft and release them only through an approval record. Reuse the amendment maker-checker engine. |
| G-06 | CS01 §3 R3 vs R4, §4 | The BOM specialist (R3) creates BOMs; the planner (R4) consumes them. | Planners hold production/plans/create (`auth_schema.sql:241-249`), which is the only permission on POST /plans-v2/bom. The planning screen offers 'Add BOM' (`planBuilder.tsx:1945-1961`). | Missing | The consumer of the BOM can rewrite it to fit a plan. | High | Gate BOM writes on bom.* permissions, not production/plans/create. Grant those to a BOM-owner role. |
| G-07 | CS01 §3 R6, §4 (Define structure: A = QA) | QA is accountable for formulation compliance. | No QA step in any BOM path. qc_inspector holds view permissions only (`auth_schema.sql:233-314`). | Missing | No compliance sign-off on allergen- or label-claim-relevant recipe changes. | High | Add QA as a checker on BOM release (Q-13). |
| G-08 | CS01 §3 R5 | Cost accountant validates costing flags and runs the cost estimate. | No costing role or step exists. | Missing | Nothing today, because no BOM costing exists. | Skip | Revisit with G-67. |
| G-09 | CS01 §3 R11 | Read-only auditor role. | No auditor role. The BOM module is admin-only (`095_bom_module_rbac.sql:49-72`). | Missing | Audits need an admin login. | Low | Add a read-only role with bom.view plus history. |
| G-10 | CS01 §5.1 C_STUE_BER-ACTVT | Separate create, change and display activities on one authorization object. | Only bom.view and bom.create exist (095:33-35). Master BOMs are changed under four other families: production/plans/create, a hard-coded role_name matrix (`router_amendments.py:41-82`), sample/npd/promote/create, and server-side ingest and scripts. | Partial | Nobody can tell from the role screen who is able to change a recipe. | Critical | Use one family (bom.view, bom.create, bom.change, bom.release) and check it on every write path. |
| G-11 | CS01 §5.1 C_STUE_WRK, §5.4 #1 | Plant-level restriction on create and display. | RBAC scopes are entity, warehouse and floor only (`auth_schema.sql:64-71`). BOM grants seed no scope (095:67-72). The middleware reads entity from query params only, never the request body (`middleware.py:163-165`). BOM reads are not filtered by the user's entities. | Missing | A CDPL user can create or view CFPL BOMs once given the permission. | High | Check the body entity against allowed_entities in the BOM write services, and filter reads. |
| G-12 | CS01 §5.1 C_STUE_BER-STLAN | Restriction by BOM usage. | No usage concept. | Missing | None while there is only one usage. | Skip | Not needed. |
| G-13 | CS01 §5.1 BEGRU, §5.4 #3 | Authorization group for confidential formulations; restricted display. | No per-BOM restriction. GET /api/v1/bom/{id} returns unit_rate_inr unfiltered (`bom_aggregate_service.py:252`). strip_cost_fields does not list it (`response_filters.py:57-119`). | Missing | Fine while the module is admin-only. Rates and recipes leak the day bom.view is granted more widely. | Medium | Add unit_rate_inr to the cost deny-list now. Add an auth group only if Q-10 says so. |
| G-14 | CS01 §5.1 (checked at entry and at save) | Authorization enforced by the server on every action. | Every BOM endpoint runs a server-side require_permission dependency (`bom/router.py:57,71,160`). Amendment maker and checker ids come from the token, not the body (`router.py:68-75`). | Fully met | — | — | Keep. |
| G-15 | CS01 §5.3 SoD-1 | The BOM creator cannot approve their own change. | The amendment path enforces maker ≠ checker1 ≠ checker2 by user id (`amendments_v2.py:447-486`). POST /bom, plans-v2 and NPD draft promote let the same user create and activate. NPD dev promote has no requester ≠ approver check, and admin can fill any gate (`promote_approval_service.py:99-114`). | Partial | Segregation exists only on the path nobody uses: the Amendments tab is hidden (`job-card/[id]/page.tsx:354-357`). | Critical | Make the change-request path the only way to change a live BOM. Enforce requester ≠ approver on NPD gates. |
| G-16 | CS01 §5.3 SoD-2 | The BOM creator cannot release cost estimates. | No costing exists. | Missing | None today. | Skip | With G-67. |
| G-17 | CS01 §5.3 SoD-3 | BOM creation separated from material master maintenance. | all_sku is loaded by ingest. Who can edit it, and whether they can also create BOMs, is not evidenced. | Not evidenced | Fictitious components (see G-01). | Low | Answer Q-11. |
| G-18 | CS01 §5.3 SoD-5 | The security administrator must not hold BOM create or change roles. | admin is the only seeded holder of bom.create (095:49-72). is_admin bypasses every permission check (`middleware.py:160-161`) and fills any amendment slot (`router_amendments.py:80-82`). | Missing | Whoever administers roles can also change recipes unchecked. | High | Split a BOM-owner role from the system admin. Do not let is_admin bypass bom.release. |
| G-19 | CS01 §6.1 MAST key, §15.2 | Unique BOM per material + plant + usage + alternative. | uq_bom_header_active_fg is UNIQUE(fg_sku_name) WHERE is_active (`032_b11_polish.sql:39-41`). Entity, site and customer are not in the key. The schema comment 'one per FG-customer-pack combination' is not enforced (`production_schema.sql:37`). | Partial | CFPL and CDPL cannot hold different recipes for the same FG name. A customer-specific BOM displaces the generic one. | High | Key on (fg_sku_id, site, customer_id, alternative) WHERE status = 'released'. |
| G-20 | CS01 §6.3 STLNR / STLAL | Several alternatives under one BOM number, selected by lot size or production version. | Version lineage only, with one active per FG. | Missing | Cannot hold 'Line 1' and 'bar line' recipes for the same FG at once. | Medium | Add an alternative column and a selection rule, if Q-07 says yes. |
| G-21 | CS01 §6.3 STLKN / STPOZ | An item keeps a permanent identity across versions. | bom_line_id is SERIAL and new on every version, with no predecessor link. The amendment copy keeps line_number, but the create paths renumber. Amendment remove and qty-change pick the target by the old line's line_number without checking it belongs to the source BOM (`amendments_v2.py:844-855`). | Missing | Cannot trace a line's change across versions. An amendment can hit the wrong line. | High | Add item_node_id, carried across versions. Validate line ownership in the amendment apply. |
| G-22 | CS01 §6.3 validity, §9.2 Valid From | Valid-from and valid-to decide which BOM is exploded on a date. | Stored only. Every resolver uses fg_sku_name ILIKE $1 AND is_active (`plan_v2.py:171-176`; `fulfillment_v2.py:816,909`). No path sets effective_to when superseding. | Missing | A recipe change cannot be scheduled for a date. The dates on screen are misleading. | High | Resolve by status plus date range. Set effective_to on supersede. Add an EXCLUDE constraint against overlapping ranges. |
| G-23 | CS01 §6.1 (IDNRK → MARA) | Components and header keyed by material number. | Keyed by name text, with three different matchers: normalised (`bom_write_service.py:289-293`), ILIKE (`plan_v2.py:317-326`), exact (`amendments_v2.py:770-774`). ILIKE treats _ and % as wildcards. | Missing | A SKU rename or an invisible character orphans BOM lines. plans-v2 can deactivate a different FG's BOM through a wildcard match. | Critical | Add sku_id foreign keys and one shared resolver. |
| G-24 | CS01 §1 (BOM + routing linked by production version C223) | Routing kept separate from the BOM, joined by a production version. | bom_process_route is part of the BOM and written in the same transaction (`bom_write_service.py:355-370`). | Our advantage | — | — | Keep. There is no key between bom_line and route step (`bom_aggregate_service.py:11-25`): add route_step_id to bom_line when that table is next touched. |
| G-25 | CS01 §7.1 (H1: BOM exists → error), §10 V-08 | Create refuses when the BOM already exists. Changes go through CS02 and change control. | POST /api/v1/bom refuses with 409, including a concurrent race (`bom_write_service.py:269-335`). POST /plans-v2/bom deactivates every active BOM whose name ILIKE-matches and inserts a new one with 7 header columns, rm/pm lines only and no route (`plan_v2.py:317-348`; `router.py:246-251`). The UI shows 'Add BOM' only when the plan card has no lines (`planBuilder.tsx:1945-1961`), but the API does not check. | Partial | A planner's 'Save BOM' on a mis-resolved or empty card, or any API client, replaces the floor's recipe. The new version has no route, no SFG lines and the default tolerance. | Critical | Make plans-v2/bom strict (409) like POST /bom, or retire it and send the UI to POST /bom. |
| G-26 | CS01 §1 upstream, §12 | One controlled write path for BOM content. | Startup ingest (`master_ingest.py:1493-1655`) and five CLI scripts insert or update BOM rows in place, without a new version or an actor (`sync_fg_master.py:264-306`; `promote_fg_master_gaps.py:225-232`; `bar_line_service.py:190-206`; `seam_mint_service.py:466-478`; `apply_bom_balance_tolerance.py:83-85`). Code comments describe a Tally refresh that deletes ERP-created rm/pm lines (`bom_write_service.py:20-33`), but that script is in no checkout or git history. | Partial | Recipe content changes with no version, no actor and no reason. | Critical | Route every writer through one service that versions and logs. Confirm whether the Tally refresh runs (Q-01). |
| G-27 | CS01 §7.2 step 7 | Create with reference: copy from an existing BOM. | NPD only ('clone base BOM' in `_npd-section.tsx:52-55`; Base BOM picker in `_article-editor.tsx:110-131`). | Partial | Near-identical BOMs are re-keyed by hand. | Medium | Add copy-from on the master create. |
| G-28 | CS01 §7.2 step 13 | Run a consistency check before saving. | Validation runs only on save. There is no dry run. | Partial | Minor. | Low | Add ?dry_run=true returning errors and warnings without writing. |
| G-29 | CS01 §7.2 step 15 (verify in CS03/CS11) | Creator, planner and stores can view the master BOM. | /modules/bom is admin-only (`modules.tsx:298-313`; 095:49-72). Planners see BOM materials only inside plan cards. | Partial | Planners, QA and stores cannot review the recipe they work to. | Medium | Grant bom.view to planner, QA, stores and inventory manager. |
| G-30 | CS02 (sister transaction, §1) | A screen to change a master BOM with history. | No master BOM change screen. The Amendments tab is hidden, and its payload is raw JSON (`_AmendmentsTab.tsx:634-655`). | Missing | Changes flow through the uncontrolled plans-v2 path or through scripts. | High | Build a change screen on the amendment engine. |
| G-31 | CS02 history (§6.3, §12) | A new version carries the whole BOM forward. | The amendment copy keeps 7 of 21 header columns and drops the rest, including allowed_balance_tolerance_pct, which falls back to the 0.001 default. It drops unit_rate_inr, process_stage, staging_method and consumed_at_stage, and copies no route (`amendments_v2.py:787-821`). | Partial | An approved qty change silently strips the route and moves the tolerance from the agreed 0.0001 to 0.001. | Critical | Copy every header and line column plus the route. Add a regression test. |
| G-32 | CS01 §8.2 #9 | Base quantity on the header. | None. Quantity is per 1 unit or 1 kg of FG. | Missing | See G-43. | Critical | Add base_qty and base_uom. |
| G-33 | CS01 §8.2 #13 | Created by and created on. | created_at only. created_by is built from the token and echoed in the response but never stored (`bom_write_service.py:402`; _HEADER_COLS 75-81). | Partial | No record of who created any BOM. | Critical | Add created_by (user FK) plus changed_by and changed_at. |
| G-34 | CS01 §8.2 #14, §11 BOM status | BOM status with release gating per consumer. | is_active boolean. | Partial | No draft or blocked state. An incomplete BOM is live. | High | Status lifecycle: draft, in_review, released, obsolete. |
| G-35 | CS01 §8.3 #26 | Component quantity with enough precision for the base quantity. | NUMERIC(15,3) per 1 unit, minimum 0.001 (`bom_write_service.py:44-49,178-185`). | Partial | An ingredient below 1 g per kg cannot be entered (0.05% preservative = 0.0005 kg/kg). It must be rounded or changed to another unit, and unit conversion does not exist. | High | base_qty (e.g. 1,000 kg) fixes it. Widen to NUMERIC(18,6) as well. |
| G-36 | CS01 §8.3 #23, §9.3 | Item categories L / N / R / T / D / K. | rm / pm / sfg material class. | Acceptable deviation | — | Skip | Only stock items are used. |
| G-37 | CS01 §8.4 #32 | Fixed quantity per order. | None. | Missing | Per-batch items (retention sample, batch label) scale with batch size. | Medium | Add a fixed_qty flag. |
| G-38 | CS01 §8.4 #39 | Material provision indicator (customer-supplied). | Not on bom_line. NPD draft lines have OWN/CUSTOMER. | Missing | Co-pack BOMs indent customer-owned packaging as own stock. | Medium | Add a provision column (Q-09). |
| G-39 | CS01 §8.4 #40-41, §9.2 Bulk | Bulk and production storage location drive issue and requirements. | staging_method and godown are stored. No explosion or issue code reads staging_method, and the amendment copy resets it to 'pick'. | Partial | Floor-stock items are still indented and picked. The field gives false comfort. | Medium | Wire staging_method into indent creation, or drop it. |
| G-40 | CS01 §8.4 #43 | Lead-time offset per item. | None. | Missing | None for same-day batches. | Skip | Not needed. |
| G-41 | CS01 §8.9 #69-72, §9.2 | Substitutes with priority, strategy and a usage % that sums to 100. | can_use_offgrade and offgrade_max_pct. offgrade_max_pct is never read. Off-grade logic exists only in run_mrp, which has no caller (`mrp.py:53-84`). | Partial | Substitution happens off-system. | Medium | Decide after Q-08. |
| G-42 | CS01 §8.6-8.8, §8.10 | Non-stock, variable-size and document items; sub-items. | None. | Missing | None. | Skip | Not used in food or agrochem process manufacturing. |
| G-43 | CS01 §9.1 (÷ base qty), §8.2 #10 | Requirement = order qty ÷ base qty × item qty, in a declared base UoM. | No base quantity, and quantity_per_unit means different things by path: per kg in the planning board, MRP and v1 job cards (`fulfillment_v2.py:1531`; `mrp.py:57`; `job_card_engine.py:182`); per unit in v2 job-card indents when all_sku.uom ≠ 1 (`job_card_v2.py:291-355`). all_sku.uom is a number (kg per pack), not a unit code (`schema.sql:54`). | Missing | For every SKU whose all_sku.uom ≠ 1, the planning board and the job-card indent disagree by that factor. | Critical | Use one explosion helper everywhere, with an explicit base_qty and base_uom on the header. |
| G-44 | CS01 §9.1 component scrap | Req × (1 + scrap %). | gross = net ÷ (1 − loss %) everywhere (`job_card_v2.py:451`; `fulfillment_v2.py:1531`; `mrp.py:58`). | Acceptable deviation | Issues more than SAP: +0.04% at 2% loss, +1.0% at 10%, +6.7% at 25%. Correct if loss_pct means '% of input lost'. | Low | Write the definition on the BOM screen and in the source sheet (Q-05). |
| G-45 | CS01 §9.1 assembly / operation scrap | Header or operation scrap inflates component requirements. | bom_process_route.loss_pct is stored, displayed and summed on the list (`bom_aggregate_service.py:129`), but never applied to a requirement. | Partial | If roasting or drying loss sits only on the route, RM is under-indented by that %. If it sits on both, the list's 'Loss %' double-counts. | High | Decide after Q-04. |
| G-46 | CS01 §9.1 rounding | Round to the component's rounding value (whole pieces). | round(x, 3) only. PM pieces are never rounded up; there is no ceil anywhere. | Missing | 2,538.071 pouches are indented, but floor requisitions only accept whole pieces (`rules.py:64-65`). | Medium | Ceil for piece-type UoMs at the indent. |
| G-47 | CS01 §9.1 (target incl. scrap) | Consumption is measured against the scrap-inclusive target. | bom_prescribed_qty = qpu × multiplier, with no loss (`jc_accounting_v2.py:209`). Indents use the gross. | Partial | A batch that consumes exactly the BOM gross shows over-consumption equal to the loss %. | High | Measure against the same gross as the indent, or report both figures. |
| G-48 | CS01 §9.2 phantom (SOBSL 50) | A phantom assembly explodes through to its components. | SFG lines are skipped in indents (`job_card_v2.py:443`). The SFG is made as an earlier stage of the same job-card chain, with RM indented on stage 1. | Acceptable deviation | — | Low | Keep. Note that run_mrp still demands RM for SFG already on hand. |
| G-49 | CS01 §13 (orders hold their own component list) | A per-order change made by the planner is what the order explodes. | fulfillment_bom_override_v2 applies in planning views (`fulfillment_v2.py:1157-1189`, 1524-1527) but not in job-card indents (`job_card_v2.py:406-415`). An override of 0 falls back to the master value (Python `or`). | Partial | The planner sees the overridden quantity; the floor is indented the master quantity. | Critical | Apply overrides in _materialise_indents, or remove them (Q-06). |
| G-50 | CS01 §13 (existing orders not affected) | Existing orders keep their component snapshot when a new BOM is created. | job_card_v2.bom_id and indent bom_line_id are fixed at creation. Variance stores bom_version (`017_job_card_v2.sql:53`; 028:143-150). | Fully met | — | — | Keep. |
| G-51 | Beyond CS01 (SAP does this in CO02) | Per-order component add/remove with history. | job_card_bom_change: add and remove per job card, undo kept as history, refused once records exist (`115_job_card_bom_change.sql`; `jc_bom_changes.py:504-562`). | Our advantage | — | — | changed_by is a display name. Store the user_id too. |
| G-52 | CS01 §10 V-01, V-14, V-20, V-21 | Header material, component and quantity mandatory; no zero or negative quantity. | no_sku, no_material, bad_qty (`bom_write_service.py:144-172`). | Fully met | — | — | Keep. |
| G-53 | CS01 §10 V-20 (extended) | Quantity must survive the column's precision. | qty_below_precision refuses any value that would round to 0.000 (`bom_write_service.py:173-185`). | Our advantage | — | — | Keep. |
| G-54 | CS01 §10 V-18/V-19 | Direct and indirect recursion check. | None on any path. | Missing | An FG listed as its own component is indented to itself. Harmless today because nothing explodes multi-level. | Low | Reject component sku_id = header sku_id. |
| G-55 | CS01 §10 V-23/V-24 | Duplicate item numbers and duplicate components. | line_number is server-assigned. Duplicate materials raise a warning on POST /bom (`bom_write_service.py:377-393`); plans-v2 accepts them silently. | Fully met | — | — | Keep the warning on whichever path survives. |
| G-56 | CS01 §10 V-27 | Scrap % between 0 and under 100. | No range check at write. NUMERIC(5,3) caps at 99.999 and accepts negatives. Explosion guards loss < 100. | Partial | A negative loss_pct shrinks the indent. | Medium | CHECK (loss_pct >= 0 AND loss_pct < 100) on bom_line and bom_process_route. |
| G-57 | CS01 §10 V-28, §8.4 #41 | Storage location exists at the plant. | godown is free text. | Missing | Issue location typos. | Low | Foreign key to the godown master. |
| G-58 | CS01 §10 V-55, §15.3 record lock | Lock while the BOM is being created or changed. | POST /bom relies on the unique index and returns 409. plans-v2 takes no lock. Amendments use FOR UPDATE plus an advisory lock (`amendments_v2.py:742-764`). No optimistic concurrency anywhere. | Partial | Two planners saving at once: the second supersede wins without notice. | Medium | Take an advisory lock per FG on every write path. |
| G-59 | CS01 §10 V-60 | Success message names the BOM. | 201 with bom_id, version and warnings (`bom_write_service.py:395-404`). | Fully met | — | — | Keep. |
| G-60 | Beyond CS01 | Mass balance per batch. | allowed_balance_tolerance_pct per BOM, validated as a fraction (bad_tolerance), and used by the R9 unbalanced-close gate (`jc_accounting_v2.py:994-1021`). | Our advantage | — | — | Keep. The live value of 0.0001 is intentional; G-31 and G-25 are what put it at risk. |
| G-61 | CS01 §11 | Item categories, statuses, usages and allowed material types held as configuration. | Hard-coded constants and CHECK constraints (`bom_write_service.py:58-69`). | Acceptable deviation | — | Low | Fine at this scale. Move to tables only once they start changing. |
| G-62 | CS01 §11 history requirement, §8.1 #5 | A change number is mandatory and stamped on every record it creates. | None. bom_header holds no request id. The amendment reason is free text (`router_amendments.py:94`). | Missing | The BOM cannot say why a recipe changed or who approved it. | High | Add change_ref on bom_header pointing to bom_amendment_request_v2. Make it mandatory for any version above 1. |
| G-63 | CS01 §12 rows 2-7 | Header, items and sub-records written atomically. | One transaction (`bom/router.py:174-176`). | Fully met | — | — | Keep. |
| G-64 | CS01 §12 row 1 | Consistent number assignment. | bom_id is serial. version is max+1 across the whole lineage on POST /bom (`bom_write_service.py:285-293`). Ingest and scripts insert the default version 1, and each path matches names differently. | Partial | Two rows can both claim version 1 of one FG (the unique index covers active rows only). | Medium | Assign versions in the single write service. |
| G-65 | CS01 §12 row 10, §15.3 | Change document written on create and on every change. | No audit table or trigger on any BOM table. amendment_log exists but no BOM path uses it (`ims_new_schema.sql:238-249`). Only NPD draft promote writes sample_audit_log (`npd_service.py:179-183`). | Missing | Loss of audit trail across 12 write paths. | Critical | Write bom_audit_log from the single BOM write service, with a trigger as the backstop. |
| G-66 | CS01 §12 row 9 | Low-level codes recalculated. | None. | Missing | None without multi-level MRP. | Skip | Not needed. |
| G-67 | CS01 §13 costing (CK11N) | Cost roll-up from costing-relevant items. | None. unit_rate_inr is ingested but nothing reads it. The variance cost columns stay NULL (028:156-158). | Missing | No BOM-based standard cost or margin check per FG. | Medium | Decide after Q-12. |
| G-68 | CS01 §1 CS15 where-used | Where-used for any component. | SFG only (sfg/where-used page). Nothing for RM or PM. | Partial | When a supplier changes an ingredient's spec or allergen status, the ERP cannot list the affected FGs. | Medium | Add a where-used endpoint on bom_line; it needs sku_id (G-01). |
| G-69 | CS01 §1 CS14 comparison | Compare two BOMs or versions. | None. Rows can only be expanded side by side. | Missing | Approvers cannot see what changed. | Medium | Version diff view. Needed for G-05 approvals. |
| G-70 | CS01 §1 CS11-13, export | Multi-level explosion reports and export. | None. | Missing | Minor. | Low | Later. |

---

## 3. Field-Level Mapping

All 76 fields in CS01 §8, in spec order. “Type match” compares data type and meaning; “Mandatory/validation match” compares mandatoriness and checks.

| # | SAP field label | SAP technical | Our field/column | Data type match? | Mandatory/validation match? | Gap / action |
|---|---|---|---|---|---|---|
| 1 | Material (header) | `RC29N-MATNR` | bom_header.fg_sku_name TEXT | Partial | Partial | Name, not a key; no FK to all_sku. Existence not checked (`bom_write_service.py:142-216`). Add fg_sku_id FK and reject unknown SKUs. |
| 2 | Plant | `WERKS` | bom_header.entity (cfpl/cdpl) + factory TEXT + floors TEXT[] | Partial | No | entity is a legal entity, nullable; factory is free text. Make site/plant a required FK and part of the unique key. |
| 3 | BOM Usage | `STLAN` | — | — | — | Skip. One usage (production) is enough until a separate costing or engineering BOM is needed. |
| 4 | Alternative BOM | `STLAL` | — (version is a sequential lineage, one active per FG) | — | — | Add alternative SMALLINT DEFAULT 1 only if parallel recipes per line/batch size are needed (Open Q-07). |
| 5 | Change Number | `AENNR` | — (bom_amendment_request_v2.request_id exists but is not stamped on the BOM) | — | No | Add bom_header.change_ref → bom_amendment_request_v2(request_id). |
| 6 | Valid From | `DATUV` | bom_header.effective_from DATE | Yes | Partial | Defaulted to today on both create paths, but nullable and never read when a BOM is resolved (resolvers use is_active only). |
| 7 | Revision Level | `REVLV` | — | — | — | Skip. Version number covers it. |
| 8 | Copy-from (Create with reference) | `—` | NPD 'clone base BOM' and 'Base BOM' only (`_npd-section.tsx:52-55`, `_article-editor.tsx:110-131`) | — | Partial | Add copy-from-existing to the master create screen. |
| 9 | Base Quantity | `STKO-BMENG` | — (implicit: 1 kg or 1 unit, depends on all_sku.uom) | No | No | Add base_qty NUMERIC(15,3) NOT NULL DEFAULT 1. Critical: see G-32, G-43. |
| 10 | Base Unit of Measure | `STKO-BMEIN` | bom_header.output_uom TEXT DEFAULT 'kg' | Partial | No | Stored but explosion ignores it and reads all_sku.uom instead (`job_card_v2.py:291-355`). Make it the single source of truth. |
| 11 | Lot Size From | `MAST-LOSVN` | — | — | — | Skip unless alternatives are introduced. |
| 12 | Lot Size To | `MAST-LOSBS` | — | — | — | Skip unless alternatives are introduced. |
| 13 | Created by / on | `MAST-ANNAM / ANDAT` | bom_header.created_at only. created_by is echoed in the API response but never stored | Partial | No | Add created_by INT FK users NOT NULL, plus changed_by/changed_at. |
| 14 | BOM Status | `STKO-STLST` | bom_header.is_active BOOLEAN | Partial | Partial | Two states only, active on insert. Replace with a status lifecycle (draft → in_review → released → obsolete). |
| 15 | Deletion Indicator | `STKO-LKENZ` | is_active = FALSE (soft) | Partial | Yes | Fold into the status lifecycle ('obsolete'). |
| 16 | Laboratory / Design Office | `STKO-LABOR` | — | — | — | Skip. |
| 17 | Authorization Group | `BEGRU` | — | — | — | Add only if some formulations must be hidden from other BOM viewers (Open Q-10). |
| 18 | Alternative BOM Text | `STKO-STKTX` | bom_header.notes TEXT | Yes | Yes | None. |
| 19 | Header Long Text | `text object` | bom_header.notes TEXT | Yes | Yes | None. |
| 20 | BOM Group | `STZU` | item_group / sub_group / inventory_group / process_category | Yes | Yes | Acceptable deviation: classification columns do the grouping job. |
| 21 | Technical Type | `STZU` | — | — | — | Skip. |
| 22 | Item Number | `STPO-POSNR` | bom_line.line_number INT, UNIQUE(bom_id, line_number), assigned 1..N server-side | Yes | Yes | Fine. Increments of 1 leave no room to insert between lines; harmless because numbers are reassigned per version. |
| 23 | Item Category | `STPO-POSTP` | bom_line.item_type rm / pm / sfg | Partial | Yes | Material class rather than item category. Acceptable: only stock items are used. |
| 24 | Component | `STPO-IDNRK` | bom_line.material_sku_name TEXT | Partial | Partial | Required, but free text. The planner UI explicitly allows names not in all_sku (`planBuilder.tsx:1868-1871`). Add material_sku_id FK. |
| 25 | Component Description | `MAKT-MAKTX` | — (the name is the identifier) | — | — | Comes free once components are keyed by id. |
| 26 | Component Quantity | `STPO-MENGE` | bom_line.quantity_per_unit NUMERIC(15,3), > 0 and ≥ 0.001 | Partial | Yes | Three decimals per ONE unit/kg cannot hold minor ingredients (< 1 g/kg). Fix with base_qty, not more decimals alone. |
| 27 | Unit of Measure | `STPO-MEINS` | bom_line.uom TEXT (free, nullable) | Partial | No | No conversion table; no check against the component's stock UoM. Add uom FK + uom_conversion. |
| 28 | Assembly indicator | `—` | item_type = 'sfg' | Partial | — | Acceptable. |
| 29 | Sort String | `STPO-SORTF` | — | — | — | Skip. consumed_at_stage orders by process. |
| 30 | Item Valid From / To | `STPO-DATUV / VALID_TO` | — (lines inherit the header version) | — | — | Acceptable deviation: whole-BOM versioning. |
| 31 | Item Change Number | `STPO-AENNR` | — | — | — | Covered by header change_ref. |
| 32 | Fixed Quantity | `STPO-FMENG` | — | — | — | Add fixed_qty BOOLEAN (QC retention samples, labels per batch). |
| 33 | Component Scrap (%) | `STPO-AUSCH` | bom_line.loss_pct NUMERIC(5,3) | Yes | Partial | Formula differs (÷(1−loss) vs ×(1+scrap)). No 0 ≤ x < 100 check at write. |
| 34 | Operation Scrap (%) | `STPO-AVOAU` | bom_process_route.loss_pct (route step, not line) | Partial | — | Stored, never applied to any requirement. Decide (Open Q-04). |
| 35 | Net ID | `STPO-NETAU` | — | — | — | Skip. There is no assembly-scrap concept to switch off. |
| 36 | Item Text Line 1/2 | `STPO-POTX1/2` | — | — | — | Low. Add line_note TEXT if shop instructions are needed. |
| 37 | Co-product | `STPO-KZKUP` | — (by-products live in job_card_byproducts_v2, not on the BOM) | — | — | Acceptable deviation. |
| 38 | Recursive allowed | `STPO-REKRS` | — | — | — | No recursion check exists either (V-18). Low. |
| 39 | Material Provision Indicator | `STPO-BEIKZ` | — on bom_line; NPD draft lines have ownership OWN/CUSTOMER | — | — | Add provision TEXT CHECK (own, customer) to bom_line if co-packing BOMs use customer-supplied PM (Open Q-09). |
| 40 | Bulk Material | `STPO-SCHGT` | bom_line.staging_method = 'floor_stock' | Partial | — | Stored but no explosion or issue code reads it. Wire it up or drop it. |
| 41 | Production Storage Location | `STPO-LGORT` | bom_line.godown TEXT | Partial | No | Free text; not validated against a godown master. |
| 42 | Production Supply Area | `STPO-PRVBE` | consumed_at_stage / process_stage TEXT; header floors[] | Partial | — | Free-text link to route; see G-24. |
| 43 | Lead-time Offset | `STPO-NLFZT` | — | — | — | Skip. Batches run within a day; indents carry no date. |
| 44 | Spare Part Indicator | `STPO-ERSKZ` | — | — | — | Skip. |
| 45 | Special procurement / Explosion type | `STPO-ITSOB` | — (SFG made as an earlier stage of the FG's JC chain) | — | — | Acceptable deviation. |
| 46 | Engineering/Design relevant | `STPO-SANIN` | — | — | — | Skip. |
| 47 | Production relevant | `STPO-SANFE` | — (implicit: rm/pm indented, sfg skipped, `job_card_v2.py:443`) | — | — | Acceptable. |
| 48 | Plant Maintenance relevant | `STPO PM flag` | — | — | — | Skip. |
| 49 | Sales relevant | `STPO-SANVS` | — | — | — | Skip. |
| 50 | Costing relevant | `STPO-SANKA` | — | — | — | Add only when a cost roll-up exists (G-67). |
| 51 | Item Long Text | `text object` | — | — | — | Low. |
| 52 | Purchasing Group | `STPO-EKGRP` | — | — | — | Skip. No non-stock items. |
| 53 | Price | `STPO-PREIS` | bom_line.unit_rate_inr NUMERIC(15,3) | Partial | — | Stored, never used; amendments drop it when copying lines (`amendments_v2.py:807-821`). |
| 54 | Price Unit | `STPO-PEINH` | — | — | — | Skip. |
| 55 | Currency | `STPO-WAERS` | — (INR implied) | — | — | Skip. |
| 56 | Cost Element / GL | `STPO-SAKTO` | — | — | — | Skip. |
| 57 | Vendor | `STPO-LIFNR` | — | — | — | Skip. |
| 58 | Material Group | `STPO-MATKL` | — | — | — | Skip. |
| 59 | Delivery time | `STPO-LIFZT` | — | — | — | Skip. |
| 60 | Size 1/2/3 | `STPO-ROMS1/2/3` | — | — | — | Skip. Not relevant to food/agrochem. |
| 61 | Size Unit | `STPO-ROMEI` | — | — | — | Skip. |
| 62 | Number of variable-size items | `STPO-ROANZ` | — | — | — | Skip. |
| 63 | Formula | `STPO-RFORM` | — | — | — | Skip. |
| 64 | Variable-size qty | `STPO-ROMEN` | — | — | — | Skip. |
| 65 | Document number | `STPO-DOKNR` | — | — | — | Low. Link specs/MSDS at SKU level instead. |
| 66 | Document type | `STPO-DOKAR` | — | — | — | Skip. |
| 67 | Document part | `STPO-DOKTL` | — | — | — | Skip. |
| 68 | Document version | `STPO-DOKVR` | — | — | — | Skip. |
| 69 | Alternative Item Group | `STPO-ALPGR` | bom_line.can_use_offgrade (off-grade of the same item group only) | Partial | — | Different concept. Add alt_group only if real substitutes exist (Open Q-08). |
| 70 | Priority | `STPO-ALPRF` | — | — | — | With alt_group. |
| 71 | Strategy | `STPO-ALPST` | — | — | — | With alt_group. |
| 72 | Usage Probability (%) | `STPO-EWAHR` | bom_line.offgrade_max_pct | Partial | — | Stored, never read; MRP uses offgrade_reuse_rule.max_substitution_pct instead (`mrp.py:53-84`), and run_mrp has no caller. |
| 73 | Sub-item number | `STPU-UPOSZ` | — | — | — | Skip. |
| 74 | Installation point | `STPU-EBORT` | consumed_at_stage TEXT | Partial | — | Acceptable deviation. |
| 75 | Sub-item quantity | `STPU-UPMNG` | — | — | — | Skip. |
| 76 | Sub-item text | `STPU-UPTXT` | — | — | — | Skip. |

---

## 4. Roles & Access Control Gaps

### 4.1 SAP persona → our role

| SAP persona | Our role | Evidence / gap |
|---|---|---|
| R1 MDG Lead | No equivalent evidenced | all_sku is loaded by startup ingest (`master_ingest.py:1493-1655`). No owner of material-master quality. |
| R2 R&D / Formulation | npd_team | Creates NPD drafts and trial recipes and promotes them (`035_sample_roles.sql:80-91`; `sample-roles.ts:96`). |
| R3 BOM Specialist | admin (bom.create) · planner (via plans-v2) | No dedicated role. bom.create is seeded to admin only and has no UI; planners create through production/plans/create. |
| R4 Planner / MRP | planner | Consumes the BOM and can also create or supersede it (G-06). |
| R5 Cost Accountant | No equivalent | No costing step (G-08). |
| R6 QA / Regulatory | qc_inspector (view only) | Not part of any BOM approval (G-07). |
| R7 Change Coordinator | Amendment checker1 = planner, checker2 = admin | Hard-coded role_name matrix (`router_amendments.py:41-82`). Tab hidden. No ECN. |
| R8 Plant Head | Business head (NPD draft promote) · admin (amendment checker2) | No approver on plans-v2 or POST /bom. |
| R9 Buyer | purchase_manager (view) | Nothing to do: there are no non-stock items. |
| R10 Stores | inventory_manager | INV_MGR gate on NPD dev promote (`promote_approval_service.py:60-66`). Cannot open /modules/bom. |
| R11 Auditor | No equivalent | G-09. |
| R12 Security Admin | admin | Also the only bom.create holder and bypasses all checks (G-18). |

### 4.2 Permission dimensions

| Dimension | SAP object | Ours | State |
|---|---|---|---|
| Plant | C_STUE_WRK-WERKS | Entity/warehouse/floor scopes exist (`auth_schema.sql:64-71`), but BOM grants are unscoped and the body entity is never checked (`middleware.py:163-165`). | Missing |
| Activity | C_STUE_BER-ACTVT | bom.view and bom.create only. No change or release activity; changes ride on other modules' permissions. | Partial |
| BOM usage | C_STUE_BER-STLAN | No usage concept. | Skip |
| Authorization group | C_STUE_BER-BEGRU | None. unit_rate_inr is returned unfiltered by GET /api/v1/bom/{id}. | Missing |
| Change-number control | C_AENR_BGR / C_AENR_RV1 | Amendment roles checked by primary role_name only (`router_amendments.py:80-82`), not by permission. The production/amendments permissions in 028 have no grants. | Partial |
| Maintain without history | C_STUE_NOH | Anyone who can deploy or run scripts: ingest and five CLI scripts edit BOMs in place with no actor. | Uncontrolled |
| Export | — | No BOM export exists, so there is nothing to control. | n/a |

### 4.3 SoD conflicts present today

- **Planner creates and consumes.** production/plans/create opens POST /plans-v2/bom (`production/router.py:5790-5794`) to the same role that plans against the BOM.
- **Security admin is the BOM owner.** admin is the only seeded bom.create holder, bypasses every permission check (`middleware.py:160-161`), and can fill any amendment slot. The amendment path still needs three distinct users.
- **NPD dev promote can be self-approved.** There is no requester ≠ approver check, and admin qualifies for any gate (`promote_approval_service.py:99-114`).
- **NPD draft promote.** The promoter is not checked against the business-head approver of the requisition (`npd_service.py:126-130`).
- **Deployers change recipes invisibly.** Startup ingest and CLI scripts write BOM tables with no actor recorded. This is the effective C_STUE_NOH holder.

---

## 5. Process Flow Differences

“Ours” follows the path planners actually use: the inline “Add BOM” in the plan builder, which calls POST /plans-v2/bom. POST /api/v1/bom is stricter but has no UI caller. NPD promote is the only gated path.

| SAP step (§7.2) | SAP action | Our step | Difference |
|---|---|---|---|
| 1 | Raise BOM request (R2/R4) | Only for new products: the NPD sample requisition. No request for master BOMs. | No request trail for changes to existing FGs. |
| 2 | Verify materials exist (MM03) | Skipped. Free text is accepted. | G-01 |
| 3 | Approve formulation (R2 + R6) | NPD only: business-head approval of the requisition, or the INV_MGR gate. Nothing on other paths. | QA approval missing everywhere (G-05, G-07) |
| 4 | Create change number (CC01) | None. amendment request_id exists on the hidden path only. | G-62 |
| 5 | Open CS01 (S_TCODE) | The planner opens Planning or SO Creation; the BOM editor is inline in the plan card (`planBuilder.tsx:1844-2080`). POST /api/v1/bom has no UI. | Creating a BOM is a side activity of planning. |
| 6 | Initial screen: material, plant, usage, alt, change no., valid from | FG and entity are taken from the SO row. Nothing else is entered (`planBuilder.tsx:613-622`). effective_from = today. | No plant, date or change reference |
| 7 | Copy from reference (optional) | Not on the planner path. NPD has clone base BOM. | G-27 |
| 8 | Authorization + existence check | Permission is checked. On existence, plans-v2 supersedes instead of refusing. | G-25 |
| 9 | Item overview | Material (typeahead, free text allowed), rm/pm, qty per unit, UoM text, loss %. | No sfg, godown, stage or staging on this path |
| 10 | Item detail | None on the planner path. POST /bom accepts godown, staging, stage and rate, but has no UI. | — |
| 11 | Header: base qty, status, lot size, auth group | None. | G-32, G-34 |
| 12 | Alternative items, sub-items | None. | G-41 |
| 13 | Check BOM | Server validation on save only. | G-28 |
| 14 | Save | Deactivates the old BOM, inserts the new one with 7 header columns and no route, and writes only an application log line (`plan_v2.py:317-352`). | No audit (G-65), route lost (G-25) |
| 15 | Verify in CS03/CS11 | /modules/bom, admin only. | G-29 |
| 16 | Cost estimate (CK11N) | None. | G-67 |
| 17 | Production version (C223) | Not needed: the route sits in the BOM. But a plans-v2 BOM is created without a route. | G-24 advantage lost on this path |
| 18 | Approve activation (R8) | None. The BOM is live on insert. | G-05 |

**Steps we add**

- Per-job-card BOM add/remove with undo history (job_card_bom_change). SAP does this in CO02, outside CS01.
- Per-BOM mass-balance tolerance and the R9 unbalanced-close gate.
- Startup ingest that loads FG master, BOM lines and routing from sheets (`master_ingest.py:1493-1655`).
- Maker / checker / final-checker amendments with user-id segregation, stronger than standard CS02. The tab is hidden, and the apply step drops columns (G-31).
- NPD promote gate: inventory manager acceptance before a trial recipe becomes a live BOM.

**Approval points missing**

- QA formulation sign-off (all paths).
- Plant-head activation (POST /bom, plans-v2, ingest, scripts).
- Change approval for supersede on plans-v2 and for in-place script edits.
- Cost review (no costing).

---

## 6. Calculation & Logic Checks

| Factor | SAP (CS01 §9.1–9.2) | Ours | Same result? |
|---|---|---|---|
| Base quantity | Order qty ÷ BMENG × MENGE | qty × qpu. qpu is per kg in the planning board and MRP, per pack in v2 JC indents when all_sku.uom ≠ 1 (`job_card_v2.py:291-355`). | No |
| Fixed quantity | MENGE, independent of order size | Not supported. | No |
| Component scrap | × (1 + AUSCH) | ÷ (1 − loss_pct) (`job_card_v2.py:451`). | Higher than SAP |
| Assembly / operation scrap, Net ID | × (1 + AUSSS) or × (1 + AVOAU) | bom_process_route.loss_pct is never applied. | No |
| UoM conversion | MEINS → base UoM via MARM | None. Unknown UoM falls back to 'KGS' (`job_card_v2.py:275-288`). | No |
| Rounding | Rounding profile / value | round(x, 3). No ceiling for pieces. | No |
| Lead-time offset | Start date + NLFZT | Indents carry no date. | n/a |
| Substitutes | Split by EWAHR; Σ = 100% | None. Off-grade logic only in run_mrp, which has no caller. | No |
| Phantom | Explodes through, no planned order | SFG made as an earlier stage of the same JC chain; RM indented on stage 1. | Same outcome |
| Bulk | No dependent requirement | staging_method = floor_stock is ignored, so the item is still indented. | No |
| Order-level change | Order component list is what is issued | Planning overrides shown in planning views only, never indented (G-49). | No |
| Variance target | Target includes planned scrap | Baseline excludes loss (`jc_accounting_v2.py:209`). | No |

### Numeric cases (CS01 §14 worked example, 2,500 kg order)

| Case | Input | SAP result | Our result | Difference |
|---|---|---|---|---|
| TC-A Scrap formula | RM-101: 2,500 kg order, 250 kg per 1,000 kg, 2% scrap | 2.5 × 250 × 1.02 = 637.500 kg | qpu 0.25, loss 2: 625 ÷ 0.98 = 637.755 kg | +0.255 kg (+0.04%). At 10%: 687.500 vs 694.444 (+1.0%). At 25%: 781.250 vs 833.333 (+6.7%). |
| TC-B Quantity basis | Same line; FG sold in 0.5 kg packs (all_sku.uom = 0.5) | Base qty and base UoM are explicit: one answer, 637.5 kg | Planning board 2,500 × 0.25 ÷ 0.98 = 637.755. JC indent: 2,500 ÷ 0.5 = 5,000 units × 0.25 ÷ 0.98 = 1,275.510 | The same plan shows 637.755 kg on the board and indents 1,275.510 kg (2×). Authoring qpu per pack instead gives 318.878 vs 637.755: still 2×. |
| TC-C Piece rounding | PM-201 pouch: 1 per kg, 1.5% scrap | 2,537.5 → 2,538 EA | 2,500 ÷ 0.985 = 2,538.071 pcs, stored as is | Fractional pouches indented; floor requisition rejects non-whole pieces (`rules.py:64-65`). |
| TC-D Fixed qty | QC-001 retention jar, 1 per batch | 1 EA at any order size | qpu 1 ÷ 2,500 = 0.0004 is refused (qty_below_precision). 0.001 gives 2.5 jars at 2,500 kg and 10 at 10,000 kg | Cannot be modelled. |
| TC-E Substitutes | RM-103 / RM-104, group 01, 100% / 0% | 125 kg + 0 kg | Two plain lines, both indented: 125 + 125 kg | +125 kg of wetting agent indented. |
| TC-F Planner override | Override RM-101 qpu 0.25 → 0.24 on the fulfillment | Order uses the changed component list | Planning board 612.245 kg; JC indent 637.755 kg | Floor receives 25.51 kg more than the planner approved. |
| TC-G Variance | Batch consumes exactly the indented 637.755 kg | Variance 0 (target includes scrap) | Baseline 625.000, so +12.755 kg (+2.04%) over-consumption | Every batch shows a variance equal to the loss %. |

---

## 7. Data Model Changes Required

PostgreSQL. Names follow the existing tables. Apply in backlog order. The status default of 'released' keeps existing rows live during migration.

```sql
-- 7.1 Keys to the material master (all_sku.sku_id) and a unit master
ALTER TABLE bom_header ADD COLUMN fg_sku_id INT REFERENCES all_sku(sku_id);
ALTER TABLE bom_line   ADD COLUMN material_sku_id INT REFERENCES all_sku(sku_id);
-- backfill with app.core.helpers.normalise_key matching, review misses, then SET NOT NULL

CREATE TABLE uom (
  code      TEXT PRIMARY KEY,                       -- KGS, GMS, PCS, LTR ...
  kind      TEXT NOT NULL CHECK (kind IN ('mass','count','volume')),
  decimals  SMALLINT NOT NULL DEFAULT 3             -- 0 for PCS: drives rounding (G-46)
);
CREATE TABLE uom_conversion (
  sku_id    INT REFERENCES all_sku(sku_id),         -- NULL = global (KGS↔GMS)
  from_uom  TEXT NOT NULL REFERENCES uom(code),
  to_uom    TEXT NOT NULL REFERENCES uom(code),
  factor    NUMERIC(18,9) NOT NULL CHECK (factor > 0),
  UNIQUE NULLS NOT DISTINCT (sku_id, from_uom, to_uom)   -- PG15+; else a COALESCE unique index
);

-- 7.2 Header: quantity basis, status, provenance
ALTER TABLE bom_header
  ADD COLUMN base_qty     NUMERIC(15,3) NOT NULL DEFAULT 1 CHECK (base_qty > 0),
  ADD COLUMN base_uom     TEXT REFERENCES uom(code),          -- replaces output_uom + all_sku.uom guessing
  ADD COLUMN status       TEXT NOT NULL DEFAULT 'released'
        CHECK (status IN ('draft','in_review','released','obsolete')),
  ADD COLUMN site_code    TEXT,                               -- FK to a site/factory master
  ADD COLUMN alternative  SMALLINT NOT NULL DEFAULT 1,        -- only if Q-07 = yes
  ADD COLUMN change_ref   BIGINT REFERENCES bom_amendment_request_v2(request_id),
  ADD COLUMN created_by   INT REFERENCES auth_user(user_id),
  ADD COLUMN released_by  INT REFERENCES auth_user(user_id),
  ADD COLUMN released_at  TIMESTAMPTZ,
  ADD COLUMN supersedes_bom_id INT REFERENCES bom_header(bom_id);
-- keep is_active for existing readers as a generated column during migration:
--   is_active = (status = 'released')

-- one released BOM per FG/site/customer/alternative, and no overlapping validity
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE bom_header ADD CONSTRAINT ex_bom_validity EXCLUDE USING gist (
  fg_sku_id WITH =, site_code WITH =, (COALESCE(customer_name,'')) WITH =, alternative WITH =,
  daterange(effective_from, effective_to, '[]') WITH &&
) WHERE (status = 'released');

-- 7.3 Lines: stable identity, quantity semantics, flags that are actually read
ALTER TABLE bom_line
  ADD COLUMN item_node_id BIGINT NOT NULL DEFAULT nextval('bom_item_node_seq'),  -- copied across versions
  ADD COLUMN fixed_qty    BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN provision    TEXT NOT NULL DEFAULT 'own' CHECK (provision IN ('own','customer')),
  ADD COLUMN route_step_no INT,                                                   -- replaces free-text consumed_at_stage link
  ADD CONSTRAINT ck_bom_line_loss CHECK (loss_pct >= 0 AND loss_pct < 100);
ALTER TABLE bom_line ALTER COLUMN quantity_per_unit TYPE NUMERIC(18,6);
ALTER TABLE bom_process_route ADD CONSTRAINT ck_route_loss CHECK (loss_pct >= 0 AND loss_pct < 100);
CREATE INDEX idx_bom_line_material ON bom_line(material_sku_id);     -- where-used (G-68)

-- 7.4 Audit
CREATE TABLE bom_audit_log (
  audit_id     BIGSERIAL PRIMARY KEY,
  bom_id       INT NOT NULL REFERENCES bom_header(bom_id),
  bom_line_id  INT,
  action       TEXT NOT NULL CHECK (action IN ('create','supersede','update','status','delete_line')),
  field        TEXT,
  old_value    JSONB,
  new_value    JSONB,
  actor_user_id INT REFERENCES auth_user(user_id),       -- NULL only for source = 'ingest' / 'script'
  source       TEXT NOT NULL CHECK (source IN ('bom_api','plans_v2','amendment','npd_promote','ingest','script')),
  change_ref   BIGINT,
  at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_bom_audit_bom ON bom_audit_log(bom_id, at);
```

### Versioning and history design

- A version is a new bom_header row. The old row moves to **obsolete** and gets `effective_to = new.effective_from − 1`; it is never edited.
- Lines copy **every** column into the new version and keep `item_node_id`. Item history is then `WHERE item_node_id = x ORDER BY version`, which is the STLKN/STPOZ equivalent.
- `change_ref` is mandatory when version > 1. Reuse `bom_amendment_request_v2` as the change record rather than adding an ECN table.
- Resolvers select `status = 'released' AND explosion_date BETWEEN effective_from AND COALESCE(effective_to, 'infinity')`, through one shared function, by sku_id instead of ILIKE.
- Every writer, including ingest and scripts, calls one service that takes `pg_advisory_xact_lock` per FG, assigns the version, writes the audit row and copies the route.

---

## 8. Implementation Backlog

Ordered by severity, then dependency. Effort: S ≤ 2 days, M ≤ 1 week, L > 1 week.

| Priority | ID | Change | Type | Effort | Depends on | Closes |
|---|---|---|---|---|---|---|
| Critical | B-01 | Make POST /plans-v2/bom strict (409 when an active BOM exists) and gate it on bom.create; point the planner's “Add BOM” at POST /api/v1/bom | Backend rule · Permission | S | — | G-25, G-06 |
| Critical | B-02 | Store created_by; add bom_audit_log written by every BOM write path, with a trigger as backstop | DB · Backend | M | — | G-33, G-65 |
| Critical | B-03 | Amendment apply: copy every header and line column plus the route; check line ownership | Backend rule | S | — | G-31, G-21 |
| Critical | B-04 | Apply fulfillment BOM overrides in _materialise_indents, or retire overrides | Backend rule | M | Q-06 | G-49 |
| Critical | B-05 | sku_id FKs on header and lines; backfill; reject unknown names; one shared resolver | DB · Backend | L | Name clean-up | G-01, G-02, G-23 |
| Critical | B-06 | base_qty + base_uom; one explosion helper for planning board, JC indents, MRP, AI planner and variance | DB · Backend | L | B-05, Q-16 | G-32, G-43, G-35 |
| Critical | B-07 | Single BOM write service used by API, amendments, NPD, ingest and scripts | Backend | M | B-02 | G-26, G-64 |
| Critical | B-08 | Status lifecycle draft → released; release through approval (QA + plant head, both ≠ creator); resolvers read released only | DB · Workflow · UI | L | B-01, B-02 | G-05, G-15, G-34 |
| Critical | B-09 | bom.change and bom.release permissions; no is_admin bypass on release | Permission | M | B-08 | G-10, G-18 |
| High | B-10 | change_ref on bom_header, mandatory for version > 1 | DB | S | B-08 | G-62 |
| High | B-11 | Master BOM change screen on the amendment engine (form, not JSON) | UI · Workflow | L | B-03, B-08 | G-30 |
| High | B-12 | UoM master and conversion; validate at write; convert in the explosion | DB · Backend | M | B-05 | G-04 |
| High | B-13 | Resolve BOMs by date; set effective_to on supersede; EXCLUDE overlapping ranges | DB · Backend | M | B-08 | G-22 |
| High | B-14 | Variance baseline = indented gross, or report both | Backend rule | S | Q-05 | G-47 |
| High | B-15 | Decide and document whether route loss enters requirements | Backend rule | S | Q-04 | G-45 |
| High | B-16 | Unique key on FG + site + customer (+ alternative) | DB | M | B-05 | G-19 |
| High | B-17 | Check the body entity against allowed_entities; filter BOM reads | Permission | S | — | G-11 |
| High | B-18 | item_node_id carried across versions | DB | M | B-07 | G-21 |
| High | B-19 | QA role as BOM release checker | Permission · Workflow | S | B-08, Q-13 | G-07 |
| Medium | B-20 | Grant bom.view to planner, QA, stores; strip unit_rate_inr for non-cost roles | Permission | S | — | G-29, G-13 |
| Medium | B-21 | Version diff view | UI · Report | M | B-18 | G-69 |
| Medium | B-22 | RM/PM where-used | Report | S | B-05 | G-68 |
| Medium | B-23 | Copy-from-existing on create | UI | S | — | G-27 |
| Medium | B-24 | fixed_qty flag | DB · Backend | S | B-06 | G-37 |
| Medium | B-25 | Round pieces up at the indent | Backend rule | S | B-12 | G-46 |
| Medium | B-26 | provision own/customer on bom_line | DB · Backend | S | Q-09 | G-38 |
| Medium | B-27 | Read staging_method in indent creation, or drop it | Backend rule | S | — | G-39 |
| Medium | B-28 | CHECK 0 ≤ loss_pct < 100 on lines and route | DB | S | — | G-56 |
| Medium | B-29 | Advisory lock per FG on every write path | Backend | S | B-07 | G-58 |
| Medium | B-30 | Material status block or warning | DB · Backend | S | Q-03 | G-03 |
| Medium | B-31 | Substitute groups with priority and share | DB · Backend | M | Q-08 | G-41 |
| Medium | B-32 | Cost roll-up from component rates | Backend · Report | M | Q-12 | G-67 |
| Low | B-33 | Dry-run check, recursion check, godown FK, auditor role, export | Mixed | S each | — | G-28, G-54, G-57, G-09, G-70 |

---

## 9. Test Cases

| ID | Gap | Precondition | Action | Expected result |
|---|---|---|---|---|
| T-01 | G-25 | FG X has active BOM v3 with 5 route steps. User holds the planner role only. | POST /plans-v2/bom for FG X with 2 lines. | 409 bom_exists (or 403). v3 stays active with its 5 steps and tolerance. |
| T-02 | G-10 | User has production/plans/create but not bom.create or bom.change. | Call each BOM write endpoint. | 403 on every one. |
| T-03 | G-01 | Component 'CASHEW W320 XYZ' does not exist in all_sku. | POST /api/v1/bom with that line. | 400 unknown_material naming line 1. No rows written. |
| T-04 | G-02 | FG name resolves to an all_sku row with item_type rm. | POST /api/v1/bom. | 400: header SKU is not an FG/SFG. |
| T-05 | G-43 | FG with all_sku.uom = 0.5; BOM base 1 kg; line 0.25 kg, loss 2%. | Plan 2,500 kg; open the planning board; create the job card. | Board, JC indent and MRP all show 637.755 kg. |
| T-06 | G-49 | Fulfillment F has an override qpu 0.24 for RM-101 (master 0.25, loss 2%). | Plan 2,500 kg for F and create job cards. | Indent reqd 600.000 and gross 612.245, matching the planning board. |
| T-07 | G-31 | BOM with 5 route steps, tolerance 0.0001, staging floor_stock on one line. | Approve a permanent_bom_qty_change through all three users. | New version has 5 route steps, tolerance 0.0001, the same staging, rate and stage values. |
| T-08 | G-65 / G-33 | None. | Create or supersede a BOM through each path (API, plans-v2, amendment, NPD promote, ingest). | bom_header.created_by is set (NULL only for ingest). One bom_audit_log row per change with source and actor. |
| T-09 | G-05 / G-15 | New BOM created by user A. | Try to create a job card; then A approves; then QA user B and plant head C approve. | JC refused while draft. A's approval refused. Released after B and C; JC creation then works. |
| T-10 | G-15 | NPD dev promote requested by user A. | User A accepts the INV_MGR gate. | 403: requester cannot approve. |
| T-11 | G-22 | v1 released; v2 released with effective_from = tomorrow. | Explode for today and for tomorrow. | Today uses v1, tomorrow uses v2. v1.effective_to = today. |
| T-12 | G-04 | RM stocked in KGS; line entered as 500 GMS; global conversion GMS→KGS = 0.001. | Create the job card. | Indent 0.5 kg per base unit. With no conversion row, the BOM save fails with 400. |
| T-13 | G-47 | BOM loss 2%; JC consumes exactly the indented 637.755 kg. | Close the job card. | Consumption variance 0. |
| T-14 | G-11 | User scoped to cdpl only, holding bom.create and bom.view. | POST /api/v1/bom with entity cfpl; GET /aggregate. | 403 on the POST; the list shows only cdpl BOMs. |
| T-15 | G-21 | Amendment payload names a bom_line_id from another BOM. | Approve it. | 400: line does not belong to the source BOM. Nothing applied. |

---

## 10. Open Questions

| # | Question | Closes / affects |
|---|---|---|
| Q-01 | Does a Tally → ERP BOM refresh still run anywhere? Comments in `bom_write_service.py:20-33` describe it, but the script is not in any checkout or git history. If it runs, is Tally or the ERP the system of record for recipes? | G-26 |
| Q-02 | Has the planner's “Add BOM” ever replaced a BOM that already had lines (for example after a name mismatch left the card empty)? | G-25 |
| Q-03 | Does all_sku on production RDS carry a status (active / blocked / discontinued)? | G-03 |
| Q-04 | Is roasting, drying or other process loss already inside bom_line.loss_pct, or only on bom_process_route.loss_pct? | G-45 |
| Q-05 | In the source sheet, does loss % mean '% of input lost' (our ÷ formula) or '% added on top' (SAP's × formula)? | G-44, G-47 |
| Q-06 | Are fulfillment BOM overrides created today, for example from the Electron client? | G-49 |
| Q-07 | Do you need two live recipes for one FG at the same time (by line, batch size or customer)? | G-19, G-20 |
| Q-08 | Are real substitutes used (RM A or RM B), beyond off-grade material of the same group? | G-41 |
| Q-09 | Do co-pack BOMs contain customer-supplied packaging or RM? | G-38 |
| Q-10 | Must any formulation be hidden from other BOM viewers (customer-confidential recipes)? | G-13 |
| Q-11 | Who maintains all_sku, and can those users also create BOMs? | G-17 |
| Q-12 | Where is product costing done today, and should the ERP roll up BOM cost? | G-08, G-16, G-67 |
| Q-13 | Should QA sign off every BOM release, or only new FGs and NPD? | G-07, G-05 |
| Q-14 | Have roles been granted through the admin Roles & Permissions UI beyond the seed SQL (for example bom.view or bom.create to non-admins)? | G-06, G-09, G-18, G-29 |
| Q-15 | Is the Amendments tab hidden for good, or should it become the BOM change path? | G-15, G-30 |
| Q-16 | For SKUs where all_sku.uom ≠ 1, is quantity_per_unit in the source sheet per kg or per pack? | G-43 |

---

## Findings that would change with different answers

| If the answer is | Then |
|---|---|
| Q-01 = no refresh runs | G-26 drops from Critical to High: ingest and scripts remain uncontrolled, but nothing deletes ERP lines. |
| Q-01 = Tally is master | B-07 changes shape: ERP edits must flow back to Tally, or be blocked. G-25 and G-30 matter less. |
| Q-02 = never | G-25 stays Critical by design (any API client can supersede) but has no known incident. |
| Q-04 = loss only on route | G-45 becomes Critical: every RM indent is under by the route loss. |
| Q-05 = '% added on top' | G-44 becomes High: we over-issue by loss² ÷ (1 − loss), +6.7% at 25%. |
| Q-06 = not used | G-49 drops to Low: retire the override tables instead of fixing them (B-04 becomes S). |
| Q-07 = yes | G-20 rises to High; the alternative column and selection rule move into B-06. |
| Q-09 = yes | G-38 rises to High: customer stock indented as own. |
| Q-10 = yes | G-13 rises to High; add an auth group with B-09. |
| Q-12 = Tally does costing | G-67 and G-08 stay Skip; only unit_rate_inr exposure (G-13) matters. |
| Q-14 = wider grants exist | G-06, G-18 and G-29 change; re-run the role mapping against live auth_role_permission. |
| Q-16 = per kg everywhere | G-43's fix becomes B-06 without data migration; per pack means re-basing every affected BOM. |
