# CANDOR FOODS — Article & Routing Reconciliation (Closing the Loop)

> **Purpose:** reconcile the four masters — **all_sku** (catalogue), **BOM Details** (recipes), **FG Master** (process routing), and the **SFG/WIP** set — into one final article list; surface every **missing article** and every **missed process routing**; and document, in one place, exactly **how the four files differ** so the loop is closed.
>
> **Status:** Reference / analysis only. **No DB or app code touched.** All four files were read from disk; the live `warehouse_db` was not queried.
>
> **Generated data:** [`Article_Master_FINAL.xlsx`](Article_Master_FINAL.xlsx) (11 sheets) and [`Article_Master_FINAL.csv`](Article_Master_FINAL.csv) (the unified master, one row per article with presence + routing + gap flags).

---

## 0. The loop in one paragraph

The four files **do not agree on which articles exist or how they are made**, and each is authoritative for a different thing. Reconciling all of them yields **3,725 distinct articles**. The catalogue (`all_sku`) is the widest (**3,680**) but carries **no `sfg`/WIP type at all**; the BOM holds **1,313 recipes**; the FG Master holds routing for only **1,085** FGs. The two structural gaps are: **(1) 238 articles have a BOM recipe but no process routing** (they exist as recipes but were never given a Process Category, so the SFG/job-card build never saw them), and **(2) the BOM is multi-level — 215 articles are used as ingredients *and* have their own recipe** (real intermediates/SFGs), of which **171 are absent from the 365-WIP set**. Plus **45 articles** carry a recipe/routing but are **not catalogued at all**, and **10 routed FGs have no recipe**. The final reconciled master flags all of these so each file can be brought into agreement.

---

## 1. The reconciliation matrix

| Source file | Authoritative for | Rows / keys | Coverage in universe |
|---|---|---|---|
| `allsku_cfpl.xlsx` + `allsku_cdpl.xlsx` | **what an article *is*** (item type, group, UOM) | CFPL **3,461** (pm 1,526 / fg 1,351 / rm 584), CDPL adds dairy | **3,680** distinct |
| `BOM Details CFPL 2nd June.xlsx` | **how an article is *made*** (recipe / components) | **1,313** parent stock items, **1,038** components | recipes |
| `FG_Master_Completion (1).xlsx` | **how an article is *processed*** (Process Category → stages) | **1,085** FGs (+84 with Bar-Line detail) | routing |
| SFG/WIP set (`JC_Stage1_Create_WIP_Master.csv`, `FG_JobCard_Cycle_Chain.csv`) | **derived WIP / stage chain** | **365** WIP, 380 transform FGs | derived |
| **UNION (the universe)** | — | — | **3,725 distinct articles** |

**Headline distinction:** `all_sku` knows 3,680 articles but **0** are typed `sfg`; the BOM knows 1,313 recipes; the FG Master routes only 1,085. The deltas between these three are the loop.

---

## 2. Gap register (missing articles + missed routing)

Every count below is a sheet in `Article_Master_FINAL.xlsx` and a `gap_flags` value in the CSV.

| # | Gap | Count | What it means | Action to close |
|---|---|---|---|---|
| **G1** | **Missing process routing** — BOM recipe but **no Process Category** | **238** | Article has a recipe and (191/238) is even catalogued as `fg`, but is **not in the FG Master**, so it has **no stage flow** and never entered the SFG/job-card build. *Mostly dates* (e.g. `Al Barakah Dates Ajwa 35gm`, `Al Barakah Gulf Dates 250gm`). | Add to FG Master with a Process Category (most are **Sorting + Packing**). |
| **G2** | **Multi-level SFG** — article is a **component *and* has its own BOM** | **215** | Real intermediates. Breaks down as **160 typed `fg`** (assemblies / bulk sold both ways), **50 typed `rm`** (graded/repacked, e.g. `American Almonds (23-25 Count)`, `Pista Kernel`), **5 uncatalogued**. **81** carry an explicit SFG-signal name (*Bulk / Kernel / Blanched / Roasted / Coated / Stuffed / Sliced / Paste / Spread / Syrup*). | Treat the 81 SFG-signal + 5 uncatalogued as **true SFGs**; decide per-item whether the `fg`-typed ones are assemblies (leave as FG) or should be re-typed `sfg`. |
| **G3** | **SFG missed by the WIP build** — multi-level SFG **not** in the 365-WIP set | **171** | The WIP set was derived from FG-Master Process Category only, so BOM-only intermediates were invisible. (160 are `fg`-assemblies that may be legitimately excluded; the actionable miss ≈ the 81 SFG-signal + 5 bulk that overlap here.) | Cross-map the 81/5 into the WIP master; the rest are assemblies, not WIP. |
| **G4** | **Missing articles** — recipe/routing exists but **not in `all_sku`** | **45** | Uncatalogued articles: **30 `fg?`** (e.g. `Carnival Black Currant 250 Gm`, `Carnival Date Spread 330gm`), **5 `sfg?` Bulk intermediates** (`Barbeque Coated Peanuts Bulk`, `Cheese Tomato Flavoured Coated Peanuts Bulk`, `Mexican Flavoured Coated Peanuts Bulk`, `Pizza Flavoured Coated Peanuts Bulk`, `Coated Peanuts Pudina Patakha Bulk`), 10 `fg`. | Add the 45 to `all_sku` with the right `item_type` (the 5 Bulk → new `sfg`). |
| **G5** | **Catalogue FG without routing** — `all_sku` FG **not in FG Master** | **403** | 403 catalogued FGs have **no Process Category** → no job-card flow. (238 of these also have a BOM = G1; the remaining ~165 have neither recipe nor routing.) | Route them (add to FG Master) or mark inactive/buy-sell. |
| **G6** | **FG routed but no recipe** — in FG Master, **no BOM** | **10** | Routed FGs that can't be costed/exploded (e.g. `Mac Snack … Munchies`, `Natures … 500g`). | Add their BOM. |
| **G7** | **Components uncatalogued** | **0** | ✅ Clean — every ingredient referenced in the BOM exists in `all_sku`. | none |
| **G8** | **Bar-Line routing unused** | **84** | 84 FGs carry a richer `Bar Line Process` (e.g. `Receiving + Sorting + Weighing + Roasting…`) that **neither** the SFG build nor `master_ingest` uses (mig 031 only stores it as raw text). | Decide whether Bar-Line **overrides** Process Category for these. |
| **—** | `all_sku` has `item_type = sfg` | **NO** | The catalogue has **no SFG type whatsoever** — the root cause that hides every intermediate. | Introduce `item_type='sfg'` (per `SFG_DESIGN_REFERENCE.md`). |

---

## 3. The "distinction of differences" — how the four files disagree

| Question | all_sku | BOM | FG Master | SFG/WIP set |
|---|---|---|---|---|
| Does the article exist? | ✅ 3,680 (truth) | parents+comps 1,? | 1,085 only | derived only |
| What type is it? | ✅ (but **no sfg**) | implies (parent=made, comp=used) | assumes FG | sfg/wip |
| How is it made? | ❌ | ✅ 1,313 recipes | ❌ | ❌ |
| How is it processed (stages)? | ❌ | partial (multi-level) | ✅ 1,085 routed | ✅ derived |
| Are intermediates modelled? | ❌ (no sfg) | ✅ implicitly (215 multi-level) | ❌ | ✅ 365 (but FG-Master-blind) |

**Therefore the single source of truth is composite:** *exist/type* = `all_sku` (after adding `sfg`), *recipe* = BOM (incl. its multi-level edges), *routing* = FG Master (after closing G1/G5), *stage chain* = SFG/WIP (after absorbing G3). No one file is complete alone — `Article_Master_FINAL` is the join that makes them agree.

---

## 4. Closing the loop — recommended sequence

1. **Add `item_type='sfg'` to the catalogue** (removes the root blindness).
2. **G4 → catalogue the 45 missing articles** (5 Bulk as `sfg`, the rest as `fg`).
3. **G1/G5 → route the 238 (and remaining catalogue FGs)** by adding Process Category in the FG Master (dates ⇒ *Sorting + Packing*).
4. **G2/G3 → fold the 81 SFG-signal + 5 Bulk intermediates into the WIP master**; classify the 160 `fg`-typed multi-level as assemblies (keep as FG) vs SFG case-by-case.
5. **G6 → add BOMs for the 10 routed-but-no-recipe FGs.**
6. **G8 → rule on Bar-Line override for the 84.**
7. Re-run the reconciliation → every `gap_flags` cell should clear. **Loop closed.**

---

## 5. Files in this loop

| File | Role |
|---|---|
| **`Article_Reconciliation_Loop.md`** | this document |
| **`Article_Master_FINAL.xlsx`** | 11 sheets — `Article_Master` + one sheet per gap (G1–G8) + `Loop_Summary` |
| **`Article_Master_FINAL.csv`** | unified master, 3,725 rows, with presence + routing_status + gap_flags |

**Workbook sheets:** `ReadMe · Article_Master · Gap_Missing_Articles · Gap_Missing_Routing · Gap_Multilevel_SFG · Gap_SFG_not_in_WIPset · Gap_Components_Uncatalogued · Gap_FG_No_BOM · Gap_Catalog_FG_No_Routing · Bar_Line_Process · Loop_Summary`.

Related (prior turns): [`SFG_DESIGN_REFERENCE.md`](SFG_DESIGN_REFERENCE.md), [`SFG_JobCard_Cycle_Map.md`](SFG_JobCard_Cycle_Map.md), `JC_Stage1_Create_WIP_Master.xlsx`.

---

## 6. Caveats (so the loop is honestly closed)

- **Name matching is normalized-exact** (lowercase, collapsed spaces, trimmed punctuation). Genuine spelling variants (e.g. *"Roasted and salted"* vs *"Roasted & Salted"*, the `Mac Snack …-style 4` duplicates) may show as false gaps — review the gap sheets before bulk action.
- **"Multi-level SFG" is a candidate signal, not a verdict:** 160 of the 215 are catalogued `fg` (many are true **assemblies** — hampers/combos — which should stay FG, not become SFG). The actionable SFGs are the **81 SFG-signal-named + 5 uncatalogued Bulk**.
- **CDPL catalogue is sparse/odd** (many untyped rows); CFPL is the reliable catalogue and matches the CFPL BOM.
- Counts are from the files dated in this repo (BOM "2nd June", FG Master "Completion (1)"). A fresher BOM/FG-Master export will shift the numbers; re-run `SFG_Folder_Extracted/_gen_reconcile.py` to refresh.
