# SFG / Job-Card Integration — Phases 6–10 Change Log (DB · Backend · Frontend)

> Companion to `SFG_Phases_1-5_Changes.md`. Phases 1–5 built the foundational
> SFG vertical (identity → classification → routing/seam → consumption → WIP
> materialisation). Phases 6–10 build the **presentation, traceability,
> routing-completion and hardening layers** on top of it.
>
> **Phase mapping** (anchored to the project's recorded labels — memory marks
> genealogy = "Phase 7", bar-line = "Phase 8", seam-minting = "Phase 9" — and to
> the in-session numbering). Stated up front so it can be corrected:

| Phase | Theme | Migrations | Gate |
|------|------|-----------|------|
| 6 | SFG catalogue presentation + enrichment + structural key + dedicated FE screens + reporting/PDF + FG-master gap promotion | 055, 056, 057, 058 | **G3** → 0 (WIP no shelf life) |
| 7 | Batch/lot genealogy (box→box→lot) + Routing-Gap resolution workflow | 059, 060 *(+053 box scaffold)* | **G5** 8-digit box_id |
| 8 | G4 Bar-Line Process override | 061 | **G4** opt-in richer routing |
| 9 | SFG-seam minting for transform chains | 062 | — |
| 10 | Functional + manual code-review & hardening pass | — (no DDL) | — |

### New conventions / primitives in this range
- **Derived VIEWS, not physical copies** — the catalogue artifacts (`sfg_master`,
  `fg_sfg_binding`, `sfg_where_used`, `sfg_genealogy`, `fg_routing_status`,
  `bar_line_fg`, `sfg_seam_pending`) are views over `all_sku` /
  `bom_process_route` / `sfg_box`, so there is zero duplication/drift.
- **View-shrink gotcha** — `CREATE OR REPLACE VIEW` can only *append* columns; a
  view redefined across migrations must have its **earliest definer DROP it
  first** (the H1 fix in 055). See Phase 10.
- **New advisory locks:** `BAR_LINE_LOCK = 0x5F6A` (Phase 8),
  `SEAM_LOCK = 0x5F6B` (Phase 9) — both `pg_advisory_xact_lock`.
- **Opt-in, script-only capabilities** — bar-line override, seam-minting and gap
  promotion mutate existing data, so they are **not** wired into startup ingest;
  they run via `scripts/*.py --apply` after a dry-run, and are **not applied to
  prod** automatically.
- **`migrate.py` SFG track** runs 050,051,052,053,055,056,057,058,059,060,061,062
  in dependency order; `app/db/rollback/sfg_integration_down.sql` tears down in
  exact reverse.

---

## Phase 6 — Catalogue presentation, enrichment & reporting

**Goal:** turn the raw SFG data into **standalone catalogue artifacts** (Design-Ref
§8.1/§9.2), give it dedicated screens, enrich the master with recipe metadata,
make WIP keyed on a real column, surface SFGs in reporting, and promote the
rule-clearable FG-master gaps.

### Database
- **`055_sfg_catalogue_views.sql`** — three **derived VIEWS** + the two reverse
  partial indexes 052 deferred:
  - `sfg_master` — the SFG catalogue (from `all_sku item_type='sfg'`; surfaces
    `uom='kg'` literal, `produced_at_stage`, `consumed_by_fg_count`).
  - `fg_sfg_binding` — FG ↔ stage ↔ `SFG####` per routing step (`binding_role`
    produces/consumes).
  - `sfg_where_used` — reverse index from `bom_process_route.input_code`.
  - reverse partial indexes on `input_code` / `output_code`.
- **`056_sfg_master_enrichment.sql`** — four **reference/reconciliation tables**
  (SERIAL/natural keys, not 8-digit ids):
  `sfg_attributes` (343: base_recipe / create_wip_operation / sfg_origin /
  item_group / consumed_by_fgs_count), `stage_catalog` (9 token→op rules),
  `fg_sfg_input_map` (1085 FG→input rows), `sfg_resolution_map` (332 dedup
  provenance). `sfg_master` re-defined (append-only) to LEFT JOIN `sfg_attributes`
  → now surfaces base_recipe / create_wip_operation / sfg_origin.
- **`057_sfg_inventory_key_and_attrs.sql`** — **structural tightening**:
  `inventory_batch.sfg_code` (the canonical WIP key — stop overloading
  `sku_name` with the code) + partial index + backfill; `sfg_attributes`
  `.va_article` / `.primary_bu`; `sfg_master` re-def appends them.
- **`058_fg_master_gap_promotion.sql`** — `bom_header.promoted_from_gap` audit
  flag + index.

### Backend
- **`sfg_catalog_service.py`** — `list_sfg_master`
  ([:19](../app/modules/production/services/sfg_catalog_service.py#L19)),
  `get_sfg_where_used`, `get_fg_sfg_binding`, `list_wip_stock`
  ([:86](../app/modules/production/services/sfg_catalog_service.py#L86), grouped
  by `sfg_code`, entity-scoped).
- **`master_ingest.py`** — four new idempotent ingests
  (`ingest_sfg_attributes` / `_stage_catalog` / `_fg_sfg_input_map` /
  `_sfg_resolution_map`, each `to_regclass`-guarded) wired into
  `run_master_ingest` (cold + warm).
- **WIP reads switched to `sfg_code`** — `create_wip_batch` now stores
  `sku_name = SFG article name` + `sfg_code = code`; `get_sfg_on_hand` /
  `get_available_batches` gain a `by_sfg_code` param; `mrp` sfg branch and the
  sfg-inventory endpoint follow.
- **Reporting (Slice 7):** `job_card_pdf.generate_job_card_pdf` prints
  `input_kind` SFG/WIP consumption rows labelled `[SFG] SFGxxxx` (and a latent
  fix — v2 PDFs were reading only the v1 `material_consumption` key, dropping
  ALL v2 `consumption_lines`); `list_job_cards` + `search_job_cards` now SELECT
  `input_code` / `output_code`.
- **Endpoints** (declared **before** `/{job_card_id}`): `GET /job-cards-v2/sfg-master`,
  `/sfg-where-used`, `/sfg-binding`, `/sfg-wip-stock` (cost-gated, entity-scoped).
- **`scripts/promote_fg_master_gaps.py`** — promotes rule-clearable gap articles
  (`promote_one`). **Applied to prod:** 108 net-new `bom_header`s
  (`process_category='Sorting + Packing'`, `promoted_from_gap=TRUE`, 216 routes).
- **Gate G3 changed to 0** here — `WIP_SHELF_LIFE_DAYS = 0` (WIP carries no shelf
  life; NULL expiry treated as in-date).

### Frontend
- **New `web_replica/src/app/modules/sfg/` module** + tile in `src/lib/modules.tsx`
  ("SFG / WIP", route `sfg`).
  - **Landing** (`sfg/page.tsx`) — card grid (3 cards in Phase 6: **SFG Master**,
    **Where Used**, **WIP Stock**; the 4th *Routing Gaps* card lands in Phase 7).
  - **Shell** (`sfg/shell.tsx`) — navy header + breadcrumb `Modules / SFG/WIP /
    {crumb}` + title/subtitle, reused by every SFG page.
  - **SFG Master** (`sfg/master/page.tsx`) — paginated, searchable table:
    `SFG Code · Name · Base Recipe · Create-WIP Op · Stage · Item Group · # FGs`
    (the *Origin* column is added in Phase 9).
  - **Where Used** (`sfg/where-used/page.tsx`) — enter `SFG####` → table of
    consuming FGs (`Finished Good · Entity · Consumed at Step · Stage · BOM ID`).
  - **WIP Stock** (`sfg/wip-stock/page.tsx`) — entity toggle + search + table:
    `SFG Code · Name · On Hand (kg) · Batches · Oldest Lot · Floors`.
- **`web_replica/src/lib/sfg.ts`** — typed `apiFetch` clients + types.
- **SFG#### chip on the job-card LIST** (`job-card/page.tsx`) — a mono navy chip
  (`Produces SFG0001`) on the plan/stage list + the JC table Stage column, keyed
  on `output_code` (defensive: only when present).

```
SFG / WIP   (landing)
┌────────────────────┐ ┌────────────────────┐ ┌────────────────────┐
│ SFG Master         │ │ Where Used         │ │ WIP Stock          │
│ the SFG catalogue… │ │ reverse index…     │ │ on-hand by SFG#### │
└────────────────────┘ └────────────────────┘ └────────────────────┘

SFG Master
┌─────────┬───────────────┬─────────────┬──────────────┬───────┬───────────┬──────┐
│SFG Code │ Name          │ Base Recipe │ Create-WIP Op│ Stage │ Item Group│ # FGs│
├─────────┼───────────────┼─────────────┼──────────────┼───────┼───────────┼──────┤
│SFG0001  │ Almond Bar WIP│ almond bar  │ Blend & Form │ Create│ Bars      │   3  │
└─────────┴───────────────┴─────────────┴──────────────┴───────┴───────────┴──────┘

WIP Stock                         [cfpl|cdpl]  [search…]   12 SFGs · 340 kg
┌─────────┬───────────────┬────────────┬─────────┬───────────┬─────────────┐
│SFG Code │ Name          │ On Hand kg │ Batches │ Oldest Lot│ Floors      │
├─────────┼───────────────┼────────────┼─────────┼───────────┼─────────────┤
│SFG0001  │ Almond Bar    │   125.500  │    3    │ WLOT-…123 │ F1, F2      │
└─────────┴───────────────┴────────────┴─────────┴───────────┴─────────────┘
```

**Functional outcome:** a dedicated SFG catalogue UI (master / where-used /
WIP-stock), an enriched master (recipe family + create-WIP op), WIP keyed on a
real column, SFG-aware PDFs + seam codes on the JC list, and 108 date FGs routed.
Tests: `tests/services/test_sfg_slice7.py` (10). **055/056/057/058 not applied to
prod by us** except 058 + the 108 promotions (live).

---

## Phase 7 — Batch/lot genealogy + Routing-Gap resolution

**Goal (A):** activate the deferred box→box→lot traceability on WIP SFG boxes.
**Goal (B):** close the remaining ~342 of the 403/238 reconciliation gap as a
**self-service** workflow (correct routings are a production decision).

### Database
- **`053_sfg_box.sql`** *(box scaffold, created in the foundation numbering)* —
  one row per physical SFG box (QR payload = 8-digit `box_id`, **gate G5**),
  `lot_number` / `parent_box_id` deferred-then-activated here.
- **`059_sfg_genealogy.sql`** — `sfg_genealogy` VIEW (box ⋈ source
  `inventory_batch`) + `idx_sfg_box_parent` / `idx_sfg_box_lot`.
- **`060_routing_gap_status.sql`** — `fg_routing_status` VIEW (`bom_header` LEFT
  JOIN `bom_process_route` → `has_routing` / `needs_routing` / `route_step_count`).

### Backend (genealogy)
- **`sfg_box_service.py`** — `create_wip_boxes` (stamps `lot_number` from the
  source WIP batch + optional round-robin `parent_box_ids` for re-split),
  `scan_receive_sfg_box` (writes an `internal_issue_note(purpose='wip_transfer')`
  in a savepoint, records `scanned_by`), `get_jc_genealogy`
  ([:474](../app/modules/production/services/sfg_box_service.py#L474)) and
  `get_box_genealogy` ([:502](../app/modules/production/services/sfg_box_service.py#L502))
  — BFS upstream, depth-cap 25, cycle-safe; batch→producer-JC hop via
  `sfg_box.job_card_id`.
- **`inventory_service.create_wip_batch`** mints `lot_number = WLOT-{batch_id}`
  when none is supplied (every WIP batch carries a lot leaf).
- **Endpoints:** `GET /job-cards-v2/{id}/sfg-genealogy`
  (`{produced[], consumed[](+source_job_card_id)}`),
  `GET /sfg-boxes/{box_id}/genealogy` (`{box_id, chain[](level, …,
  producer_job_card_id)}`), plus the box CRUD/scan endpoints.

### Backend (routing-gap)
- **`routing_gap_service.py`** — `classify_family` + `FAMILY_TEMPLATES`
  (Pack-only→`Sorting + Packaging`, Roasted→`Roasting + Packaging`, Flavoured→
  `Roasting + Flavouring + Packaging`, Coated→`Enrobing + Packaging`, Mix→
  `Blending + Packaging`, Chocolate→`Enrobing + Packaging`, Date-transform→`''`
  needs_review, Other→`Sorting + Packaging` needs_review); `get_routing_gaps`,
  `promote_articles` (reuses `promote_fg_master_gaps.promote_one`),
  `build_worksheet_csv` ([:426](../app/modules/production/services/routing_gap_service.py#L426)).
- **Endpoints:** `GET /routing-gaps`, `POST /routing-gaps/apply`
  (`require_permission('production','plans','create')`),
  `GET /routing-gaps/worksheet.csv`.

### Frontend
- **`SfgBoxesTab` + `SfgGenealogyPanel` + `BoxTrace`** (`job-card/[id]/page.tsx`)
  — the boxes tab gains **Lot** + **Parent** columns; a "Lot & box genealogy"
  panel with **Produced here** / **Consumed here** groups; and a per-box **Trace**
  toggle rendering the indented upstream chain.
- **`sfg/routing-gaps/page.tsx`** + the 4th landing card — families
  grouped/collapsible, each with an editable **Process Category** input prefilled
  from the template, per-family + global **Apply**, and a **worksheet CSV**
  download; a yellow *Needs review* badge where the family has no confident
  template.

```
Job Card ▸ SFG Boxes
┌──────────┬────┬────────┬─────────┬─────────┬────────┐
│ Box ID   │ #  │ Net kg │ Lot     │ Parent  │ Status │
├──────────┼────┼────────┼─────────┼─────────┼────────┤
│ 12345678 │1/5 │ 2.150  │ WLOT-…1 │ 8765432 │ active │
└──────────┴────┴────────┴─────────┴─────────┴────────┘
Lot & box genealogy
  Produced here · 5     Consumed here · 2
  • 12345678  SFG0001  WLOT-1  2.150 kg · active     [Trace]
    ↳ 87654321 SFG0001 WLOT-1  batch 5679 · JC 100
      ↳ …                                    (level-indented chain)

Routing Gaps                 [Download worksheet (CSV)]  [Apply (12)]  [Refresh]
▾ Pack-only · 228 articles            → Sorting + Packaging   [Apply all (228)]
   ┌──────────────┬────────┬─────────┬───────────────────────┬──────────┐
   │ Article      │ in_sku │ Current │ Process Category (edit)│ Result   │
   ├──────────────┼────────┼─────────┼───────────────────────┼──────────┤
   │ Salted Cashew│   ✓    │   —     │ [Sorting + Packaging ] │ applied  │
   └──────────────┴────────┴─────────┴───────────────────────┴──────────┘
▸ Date-transform · 27 articles   ⚠ Needs review
```

**Functional outcome:** full WIP box traceability (box → lot → source batch →
producing JC) and an operator-driven routing-gap queue. Tests:
`test_sfg_phase7.py` (8), `test_routing_gaps.py` (8). 059/060 **not applied to
prod by us**.

---

## Phase 8 — G4 Bar-Line Process override

**Goal:** 84 bar-line value-added FGs carry a **richer** routing string in
`bom_header.bar_line_process` (filled by mig 031) that was used nowhere. G4 lets
those FGs derive their `bom_process_route` from it — as an opt-in, audited action.

### Database
- **`061_bar_line_routing.sql`** — `bom_header.bar_line_routed` BOOLEAN audit flag
  + index + `bar_line_fg` VIEW (the 84 FGs).

### Backend
- **Classifier extended** (`master_ingest`) — added `'mixing' → 'Blend & Form'`
  (transform) and `'flow' → terminal` so both bar-line strings classify with
  **zero** unclassified steps (avoids the re-classify loop bug class).
- **`bar_line_service.apply_bar_line_override(conn, dry_run, only_bom_id)`**
  ([:74](../app/modules/production/services/bar_line_service.py#L74)) — REPLACES a
  bom's `bom_process_route` from `bar_line_process` (DELETE+INSERT like
  `ingest_jc_routing`), sets `bar_line_routed`, idempotent, under `BAR_LINE_LOCK`,
  column-guarded. *(Phase 10 adds a `force` flag + a transaction guard.)*
- **`get_job_card`** exposes `bar_line_process` + `bar_line_routed`
  (`UndefinedColumnError` fallback for pre-061 DBs).
- **`scripts/apply_bar_line_override.py`** (`--apply`) — opt-in, not startup-wired.

### Frontend
- **`BarLineBadge`** (`job-card/[id]/page.tsx`) — a filled navy **Bar Line** chip
  when `bar_line_routed`, else a dashed-outline **Bar Line · not applied** chip;
  the `title` shows the full `bar_line_process` string. Plus a chain-tab token
  reference (split on `/[+>→,]+/` — `bar_line_process` is `+`-separated).

```
Job Card header:  …  [ Bar Line ]            (routed — filled navy)
                  …  [ Bar Line · not applied ]   (dashed outline)
```

**Functional outcome:** the 84 bar-line FGs can adopt their richer multi-step
routing on demand. Tests: `test_bar_line.py`. 061 **not applied to prod**.

---

## Phase 9 — SFG-seam minting for transform chains

**Goal:** bar-line-routed / gap-promoted FGs grow Create-WIP steps with **no**
`SFG####` seam (`output_code` NULL) — so they can never spawn a real WIP. This
mints (or **reuses**) an `SFG####` per such step and stamps the seam.

### Database
- **`062_sfg_seam_pending.sql`** — `sfg_seam_pending` VIEW (Create-WIP steps with
  NULL `output_code` = the work queue) + `idx_bom_route_seam_pending` (NULL-side,
  distinct from 055's NOT-NULL index).

### Backend
- **`seam_mint_service.mint_sfg_seam_for_chains(conn, dry_run)`**
  ([:305](../app/modules/production/services/seam_mint_service.py#L305)) — the
  **dedup contract** is the whole point: `key = (normalise_key(_base_recipe(
  fg_name)), normalise_key(operation))`; build an index of existing catalogue
  SFGs once, **reuse before mint** (so two pack variants of one recipe share ONE
  SFG), only mint when no match (`next_sfg_code` + 8-digit `all_sku` row +
  `sfg_attributes`, `sfg_origin='SEAM_MINTED'`); stamp producer `output_code` +
  consumer `input_code`. `SEAM_LOCK = 0x5F6B`. No recipe-signature split (these
  FGs lack ingredient data — documented).
- **`scripts/mint_sfg_seam.py`** (`--apply`) — opt-in, run AFTER
  `apply_bar_line_override`. On current prod = **0 rows** (forward-looking).

### Frontend
- **Origin column + `OriginChip`** on SFG Master (`sfg/master/page.tsx`):
  `SEAM_MINTED` → amber **Minted**, `DERIVED_INLINE` → neutral **Derived**,
  null → `—`.

```
SFG Master  (Origin column added)
┌─────────┬────────────┬──────────────┬─────────┬──────────┐
│SFG Code │ Base Recipe│ Create-WIP Op│ Origin  │  # FGs   │
├─────────┼────────────┼──────────────┼─────────┼──────────┤
│SFG0007  │ almond bar │ Blend & Form │ [Minted]│    2     │  ← amber
│SFG0003  │ cashew     │ Roasting     │ [Derived│    1     │
└─────────┴────────────┴──────────────┴─────────┴──────────┘
```

**Functional outcome:** derived transform chains get dedup-correct SFG seams
(verified on Docker: 2 same-base FGs → 1 SFG, re-run mints 0). Tests:
`test_seam_mint.py`. 062 **not applied to prod**.

---

## Phase 10 — Functional + manual code-review & hardening pass

**Goal:** a two-lens review (functional + manual) of everything in Phases 6–9,
across six areas, with **every finding independently verified** before fixing.
No new feature/DDL — corrections only.

### What changed (verified-real findings, fixed)
**Security / access-control**
- **Cross-entity genealogy leak** — `get_box_genealogy` walked upstream ancestors
  with no entity filter; now scope-filtered via `allowed_entities`
  ([sfg_box_service.py:502](../app/modules/production/services/sfg_box_service.py#L502)).
- **`/sfg-boxes/{box_id}`** had **no** entity check (every sibling had one) →
  added ([router.py:7569](../app/modules/production/router.py#L7569)).
- **`/job-cards-v2/{id}/pdf`** skipped the factory/floor scope the JSON detail
  enforces → added (a 403'd user could pull the JC as PDF).
- **`get_jc_genealogy`** consumed-box list not entity-filtered → scoped.

**Correctness / robustness**
- **Bar-line re-run wiped minted seams** — the DELETE+re-INSERT dropped the
  `output_code`/`input_code` a later seam-mint stamped; now skips already-routed
  headers by default with a `--force` flag, + an `is_in_transaction()` guard
  ([bar_line_service.py](../app/modules/production/services/bar_line_service.py)).
- **Genealogy** — deterministic producer-JC resolution (`ORDER BY`) + a
  `truncated` flag when the depth cap is hit.
- **PDF** `created_at[:10]` crash on present-but-None → guarded.
- **CSV formula injection** in `worksheet.csv` → leading `= + - @` neutralised.
- **Seam dedup** made order-independent via a pre-pass seeding the index from
  already-seamed steps.
- **Pagination clamp** in `sfg_catalog_service`; **where-used** in-flight guard;
  `statement_cache_size=0` added to 3 scripts for pooler consistency.

> Also re-confirmed clean: the DB migrations (idempotent, correct view-shrink
> handling via the **055 H1 fix** = DROP-before-CREATE, reverse-ordered rollback).
> Two agent ratings were **downgraded** after verification: the
> `statement_cache_size` "HIGH" (the session pooler tolerates prepared
> statements — the app itself doesn't set it) and the seam-reuse "MEDIUM" (needs
> a pre-existing duplicate-key state the dedup design prevents).

### Verification
`py_compile` clean (10 backend files) · **tsc exit 0** · test suites green:
**seam 5/5 · bar-line 6/6 · phase-7 genealogy 8/8 · routing-gaps 8/8 · slice-7
10/10** (throwaway Docker pg16, never prod). The bar-line test was updated to the
new contract (default re-run = no-op; `force=True` re-derives).

**Functional outcome:** closed the cross-entity genealogy/box/PDF leaks, stopped
bar-line re-runs from orphaning minted SFGs, hardened CSV/PDF/pagination. **No
prod writes** — source changes only.

---

## Cross-cutting summary (Phases 6–10)

### Gates
| Gate | Decision | Phase |
|------|----------|------|
| **G3** | `WIP_SHELF_LIFE_DAYS` → **0** (WIP carries no shelf life) | 6 |
| **G5** | 8-digit app-supplied `sfg_box.box_id` (QR payload) | 7 |
| **G4** | Bar-line override is **opt-in** (overrides working routing) | 8 |

### Advisory locks added
`BAR_LINE_LOCK 0x5F6A` (Phase 8) · `SEAM_LOCK 0x5F6B` (Phase 9).

### Migrations & opt-in scripts
| Migration | Phase | Opt-in script |
|-----------|------|---------------|
| 055 / 056 / 057 / 058 | 6 | `scripts/promote_fg_master_gaps.py` (108 applied) |
| 059 / 060 | 7 | routing-gaps applied via the UI/endpoint |
| 061 | 8 | `scripts/apply_bar_line_override.py --apply` |
| 062 | 9 | `scripts/mint_sfg_seam.py --apply` |
| — | 10 | (review/fixes; no migration) |

**Run order to realize the opt-in chain:** `migrate.py` → `apply_bar_line_override
--apply` → `mint_sfg_seam --apply`. The Routing-Gap and gap-promotion paths are
independent (apply via the screen / `promote_fg_master_gaps.py`).

### File index
| Layer | File | Phase(s) |
|------|------|---------|
| DB | `app/db/055…058_*.sql` | 6 |
| DB | `app/db/059_sfg_genealogy.sql`, `060_routing_gap_status.sql` | 7 |
| DB | `app/db/061_bar_line_routing.sql` | 8 |
| DB | `app/db/062_sfg_seam_pending.sql` | 9 |
| DB | `app/db/rollback/sfg_integration_down.sql` | 6–9 |
| Backend | `services/sfg_catalog_service.py` | 6 |
| Backend | `services/master_ingest.py` (4 ingests + classifier ext) | 6, 8 |
| Backend | `services/job_card_pdf.py`, `job_card_v2.py` (list/search seam) | 6 |
| Backend | `services/sfg_box_service.py` | 7, 10 |
| Backend | `services/routing_gap_service.py` | 7, 10 |
| Backend | `services/bar_line_service.py` | 8, 10 |
| Backend | `services/seam_mint_service.py` | 9, 10 |
| Backend | `router.py` (SFG endpoints + scope fixes) | 6–10 |
| Backend | `scripts/promote_fg_master_gaps.py`, `apply_bar_line_override.py`, `mint_sfg_seam.py`, `relink_sfg_codes.py` | 6–10 |
| Frontend | `web_replica/src/app/modules/sfg/` (landing, shell, master, where-used, wip-stock, routing-gaps) | 6, 7, 9 |
| Frontend | `web_replica/src/lib/sfg.ts`, `src/lib/modules.tsx` | 6 |
| Frontend | `web_replica/src/app/modules/job-card/[id]/page.tsx` (SfgBoxesTab, genealogy, BoxTrace, BarLineBadge) | 7, 8 |
| Frontend | `web_replica/src/app/modules/job-card/page.tsx` (SFG#### chip) | 6 |

### Tests
`test_sfg_slice7.py` (10) · `test_sfg_phase7.py` (8) · `test_routing_gaps.py` (8)
· `test_bar_line.py` (6) · `test_seam_mint.py` (5) — all green on Docker pg16.
