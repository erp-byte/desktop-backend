# Candor Foods — SFG / Job-Card Integration Pack (Index)

Reference/design only — generated from `all_sku`, `BOM Details`, `FG Master`, and the SFG folder.
**No database or application code was modified.** Snapshot date: 2026-06-14.

## A. Design & mapping documents (read in this order)
1. **SFG_DESIGN_REFERENCE.md** — how SFG/WIP maps onto the existing v2 job-card chain; keep/drop, naming, gaps.
2. **SFG_JobCard_Cycle_Map.md** — the 2-stage cycle (Create WIP → Final FG), existing-bulk reuse + practical combined operations (Roast & Flavour/Salt, etc.).
3. **Article_Reconciliation_Loop.md** — cross-file reconciliation: missing articles + missed process routing, with caveats.

## B. SFG / cycle master data
4. **JC_Stage1_Create_WIP_Master.xlsx** — workbook (SFG_Master, SFG_Resolution_Map, Final_FG_Stage, FG_Cycle_Chain, Practical_Op_Model, Stats).
5. **JC_Stage1_Create_WIP_Master.csv** — 343 canonical SFGs (origin, operation, reuse, possible-dup flag).
6. **JC_Stage2_Final_FG_Packing.csv** — per-FG Final-FG stage (input = SFG or RM).
7. **FG_JobCard_Cycle_Chain.csv** — full new-model cycle per FG.
8. **SFG_Resolution_Map.csv** — how each recipe group resolved (existing reuse vs derived).

## C. Reconciliation master
9. **Article_Master_FINAL.xlsx** — unified article master + gap sheets (G1–G8) + Loop_Summary.
10. **Article_Master_FINAL.csv** — 3,725 articles with presence + routing_status + gap_flags.

## D. Job-card module PLUG (the "new plug" — makes the cycle functional)
11. **JC_Module_Plug.xlsx** — workbook with ReadMe + all four plug tables below + Stats.
12. **ProcessCategory_to_Operation.csv** — config lookup: Process-Category token → practical operation / stage bucket.
13. **JC_Routing_Plug.csv** — per article: raw Process Category → ordered job-card stages (Create WIP / Final FG) with input/output item + kind. → feeds `bom_process_route` + `job_card_v2` chain.
14. **JC_BOM_Plug.csv** — per parent: each component classified rm/pm/**sfg** + the stage that consumes it. → feeds `bom_line` (item_type extended to sfg) + consumption.
15. **SFG_Catalog_Plug.csv** — the 343 SFG rows to add to `all_sku` (item_type=sfg). → all_sku delta.

## E. Visual blueprints
17. **Candor_System_Wiring.html** — whole-**system** interactive map (**FULL DEPTH**): System Overview of all 11 modules + a major tab per module with **Graph / Flow / Relationships / Endpoints** sub-tabs, fully cross-navigated. Coverage: **165 tables · 2,130 columns · 441 endpoints · 158 FK edges** parsed from the schema + routers. Plus **`Candor_Module_<name>.html` × 11** standalone single-module files (production, purchase, receipt, so, transfer, qc, ncr, sample, vendor, auth, shared) whose tabs link to each other.
16. **Candor_SFG_JobCard_Map.html** — self-contained interactive page: **Functional Flowchart** tab (what feeds what → where it lands), **Table Wiring & Network** tab (lineage, plug→backend map, SFG seam, op lookup, archetypes), **Files & Metrics** tab. Open in any browser.

## How to enable (summary)
1. Load `SFG_Catalog_Plug` into `all_sku` (item_type=sfg).
2. For each FG, create 1–2 plan steps from `JC_Routing_Plug` (Create WIP, Final FG).
3. Load `JC_BOM_Plug`; SFG-typed lines become the carried/consumed input of Final FG.
4. The job-card engine builds the 2-link chain: `output_kind='SFG'` (Stage 1) → `input_kind='SFG'` (Stage 2).

Key rules: roasting is combined with flavouring/salting (one operation); sorting is inline; bulk articles already in BOMs are reused as the SFG (not re-created).
