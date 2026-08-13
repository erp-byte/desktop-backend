# Inventory Ledger — Inward Wiring Design

- **Date:** 2026-08-13
- **Status:** Approved (design) — pending spec review
- **Author:** investigation + design with ai.1@candorfoods.in
- **Scope:** `server_replica` (new read-only `app/modules/ledger/`) + small additive changes in `web_replica`. No write paths are touched.
- **Revision:** v2 — rewritten after adversarial verification (114 claims checked, 39 corrections applied). Findings are noted inline as **[V]**.

## 1. Problem

The Inventory Ledger frontend (`web_replica/src/app/modules/inventory-ledger/`) is fully built but renders entirely from `_fixtures.ts`. `TASKS.md` §9.1 records the backend as not started; there is no `/api/v1/ledger/*` router in `server_replica`.

Every screen in the module derives from one flat leaf feed (`_LedgerData.tsx`), so a single endpoint returning `LeafItem[]` lights up the whole module.

This design wires **the Inward column only**. The other six movement columns stay unsourced.

### 1.1 Where inward lives

| Candidate | Verdict |
|---|---|
| `material_document` / `_line` (SAP-aligned) | **Rejected.** Movement type `101` is defined in `movement_type_ref` but never written. The only movement types actually posted are `261` (`lot_issuance_service.py:252`), `265/266` and `531` — all from the sample/NPD module. **[V]** `531` is direction `IN`, but it mints FG trial samples into the R&D location, not procurement receipts. |
| `inventory_batch` (`source='INWARD'`) | **Rejected.** Written by the *new* PO-receipt path. **[V]** Three writers exist — `purchase/router.py:911-920`, the `production_migrate.sql:221-240` backfill, and the `source='INWARD'` default on `inventory_service.create_batch` — all PO-box-shaped, none the historical inward book. |
| `{cfpl,cdpl}_*_v2` + `{cfpl,cdpl}_bulk_entry_*` | **Chosen.** The legacy IMS inward families. |

**[V] Evidence limit:** these tables exist only in the live DB — there is no `CREATE TABLE` for them anywhere in the repo. Every column claim below is sourced from query-code usage, primarily the union projections in `legacy_backend/services/ims_service/inward_tools.py`. Treat column-level assertions as high-confidence-but-inferred, not schema-verified.

## 2. Goal

`GET /api/v1/ledger/leaves` returns real inward quantities per SKU per godown per entity, across **both** inward channels, with godowns canonicalised so one warehouse is one row.

The frontend defaults to Live. Fixtures and the Sample/Live toggle stay as a reference for comparison against the Tally screens.

## 3. Source tables

Per entity, `{p}` ∈ `{cfpl, cdpl}`:

| `_source` | Header | Article lines |
|---|---|---|
| `inward` | `{p}_transactions_v2` | `{p}_articles_v2` |
| `bulk_entry` | `{p}_bulk_entry_transactions` | `{p}_bulk_entry_articles` |

Mirrors `union_source_ctes()` (`inward_tools.py:572`), whose projections `_TX_UNION_PROJ` (`:507`) and `_ART_UNION_PROJ` (`:537`) enumerate the columns proven present on **both** families.

**[V] The `{p}sku` master is NOT joined.** `cfplsku` and `cdplsku` contain **one row each** (`EXISTING_CODEBASE_AUDIT.md:209,234`). A `LEFT JOIN {p}sku s ON s.id = a.sku_id` returns NULL for effectively every row, so the originally-planned `s.material_type` fallback was dead code. `item_type` now comes from `a.material_type` alone. The populated master is `all_sku` (3,685 rows) but it is **text**-keyed on `particulars`, not keyed by `a.sku_id`; joining it is a fuzzy name match and is out of scope (§11).

### 3.1 Joining header to lines

Join on **`(transaction_no, _source)`**, never `transaction_no` alone.

**[V] Sourcing correction:** the `union_source_ctes` docstring mandates the two-key join and names the consequence ("cross-mixing records"); it does *not* state that transaction numbers collide across families. The rule is confirmed by every legacy call site (`inward_tools.py:777, 780, 991, 1037`), which is the real authority.

### 3.2 Quantity comes from articles, not boxes — and what that costs

`inward_qty` is summed off the **article** union, not by joining boxes.

**[V] Three corrections to the original rationale:**

1. **A correct box join is available.** `{p}_bulk_entry_boxes` lacks `line_number`, but `(transaction_no, _source, article_description)` is the legacy fallback key and is what the repo's own recalc uses on the bulk side. Using articles is a **uniformity decision, not a forced one**.
2. **The fan-out belongs to the report, not to the tables.** `server_replica/scripts/generate_inventory_report.py:314` joins bulk boxes on `transaction_no` alone, omitting the available article key, so its bulk figure multiplies by article count on multi-article transactions.
3. **Consequence — the ledger will disagree with that report on the bulk channel.** The divergence is the report's bug, not the ledger's. The v2/inward channel ties (both use `a.net_weight`). This must be said out loud before anyone puts the two side by side.

**[V] Reliability caveat on `a.net_weight` (the largest open risk).** `recalc_article_aggregates` sets article `net_weight = SUM(box.net_weight)`, but only on box mutation or via the one-shot `legacy_backend/run_backfill_article_aggregates.py`, which is **dry-run unless `--confirm`** and has no record in the repo of having been applied. Rows created via the cold-storage bulk-sticker page and never box-edited hold a **hand-typed declared weight**. The bulk service's own detail read path distrusts the column and overrides it with the box-derived sum (`bulk_entry_service/tools.py:447`).

**Mitigation, required before the numbers are trusted:** run `run_backfill_article_aggregates.py` in dry-run, review the delta, then `--confirm`. Until that is done the ledger may show figures that no existing screen shows.

## 4. Quantity and UOM split by material type

**[V]/user requirement.** `a.uom` is unusable as a unit-class source — real values include `kg`, `kgs`, `box`, `pcs`, `ctn`, `bag` **and bare numbers** (`"12"`, `"10"`, `"0"`), consistent with `all_sku.uom` being `NUMERIC`. The split is driven by `a.material_type`, whose values are `rm` / `pm` / `fg`.

| `material_type` | `uom_class` | `inward_qty` |
|---|---|---|
| `pm` (packing material) | `nos` | `SUM(a.quantity_units)` |
| `rm`, `fg`, anything else / NULL | `kg` | `SUM(a.net_weight)` |

Comparison is case-insensitive on the trimmed value. The module never cross-sums UOM classes — `LedgerNode.uom_subtotals` already carries per-class breakdowns for mixed nodes — so a kg leaf and a nos leaf coexisting under one group is handled natively.

**Open risk:** `a.quantity_units` has not been verified for population the way `net_weight` has. PM rows with NULL `quantity_units` will contribute `0`. The first implementation task is a read-only count of NULL/zero `quantity_units` on PM rows per entity; if coverage is poor, PM inward is reported as unavailable rather than as zero.

## 5. Query shape

```
WITH all_tx AS (
    SELECT <tx cols>, 'inward'::text AS _source
      FROM {p}_transactions_v2
     WHERE (rtv     IS NULL OR rtv     = false)     -- v2 only; see §5.1
       AND (service IS NULL OR service = false)
    UNION ALL
    SELECT <tx cols>, 'bulk_entry'::text AS _source
      FROM {p}_bulk_entry_transactions
),
all_art AS (
    SELECT <art cols>, 'inward'::text     AS _source FROM {p}_articles_v2
    UNION ALL
    SELECT <art cols>, 'bulk_entry'::text AS _source FROM {p}_bulk_entry_articles
)
SELECT a.sku_id,
       a.item_description,
       a.item_category,
       a.sub_category,
       lower(trim(a.material_type))              AS material_type,
       ledger_godown(t.warehouse)                AS godown,
       CASE WHEN lower(trim(a.material_type)) = 'pm'
            THEN SUM(a.quantity_units) ELSE SUM(a.net_weight) END AS inward_qty,
       SUM(a.total_amount)                       AS value_indicative
  FROM all_art a
  JOIN all_tx  t ON t.transaction_no = a.transaction_no
                AND t._source        = a._source
 GROUP BY a.sku_id, a.item_description, a.item_category, a.sub_category,
          lower(trim(a.material_type)), ledger_godown(t.warehouse)
```

**[V] GROUP BY correction (was a compile error).** The v1 draft grouped on five columns while selecting `material_type` and `uom`-derived expressions ungrouped. Every non-aggregated column now appears in `GROUP BY`; `material_type` is normalised once and grouped in its normalised form so `PM`/`pm` do not split a leaf.

Prefixes resolve from a hardcoded whitelist `{"cfpl","cdpl"}` before interpolation — never from raw request input.

**No date bound.** The endpoint reads all history. **[V]** Note the report is not a comparison baseline here either: its v2 branch is cut at `CUTOFF_DATE = 2026-04-01` while its bulk branch is unbounded, so its two channels are not on the same time base.

### 5.1 The rtv / service filter belongs inside the inward branch

`rtv` and `service` exist on `{p}_transactions_v2` and exclude return-to-vendor and service-only invoices (`generate_inventory_report.py:272-274`). They do **not** exist on `{p}_bulk_entry_transactions` (`inward_tools.py:3276`).

**[V] My original reasoning was wrong and is corrected here.** I claimed that filtering the unioned result would evaluate `NULL = false` and silently drop all bulk rows. It would not: the guard is `(rtv IS NULL OR rtv = false)`, and `NULL IS NULL` is TRUE, so bulk rows would be **retained**. The actual reason to keep the predicate inside the inward branch is that **the columns do not exist on the bulk table at all**, so a unioned reference fails with a hard `column "rtv" does not exist` error unless the bulk branch projects an explicit `NULL::boolean` placeholder. The hazard is a loud error, not a silent drop.

## 6. Field mapping to `LeafItem`

Wire type: `LeafItem` (`web_replica/src/lib/ledger.ts:78-87`), which extends `MovementCols` (`:64-72`) for the seven movement columns.

| `LeafItem` field | Source |
|---|---|
| `inward_qty` | per §4 — `quantity_units` for PM, `net_weight` otherwise |
| `uom_class` | `nos` for PM, `kg` otherwise |
| `sku_id` | `a.sku_id` |
| `label` | `a.item_description` |
| `item_type` | `a.material_type` (no fallback — see §3) |
| `group` | `a.item_category` |
| `subgroup` | `a.sub_category` |
| `godown` | `ledger_godown(t.warehouse)` — §7 |
| `value_indicative` | `SUM(a.total_amount)` |
| `entity` | **new additive field** — `'cfpl'` \| `'cdpl'`, see §9 |
| `opening_qty`, `production_qty`, `returns_qty`, `consumption_qty`, `outward_qty`, `transfer_out_qty` | `0` — not sourced in this pass |

## 7. Godown canonicalisation

Raw `warehouse` values are inconsistent; grouping on the raw column fragments one physical godown across many rows.

**[V] Source correction — I was porting the weaker of five copies.** The alias map is duplicated across `legacy_backend`, `legacy_frontend`, `web_replica` and `server_replica`, with real content drift. The **authoritative** copy is `legacy_backend/shared/canonicalize.py:24-82`, which the v1 draft missed. It carries **11** canonical warehouses to `inward_tools.py`'s 6:

```
Savla D-39, Savla D-514, Rishi, Supreme, Eskimo,
W202, A101, A185, A68, F53, Dev Int
```

The ledger map is built from `canonicalize.py`, plus:

1. **`savla bond` → `Savla Bond`** — a new canonical godown. Both legacy copies fold it into `Savla D-39`; per requirement it is now separate. `old savla → Savla D-39` and `new savla → Savla D-514` are confirmed correct and unchanged.
2. **Hyphen variants merged in from `inward_tools.py`** (`savla-d39`, `savla-d-39`, `savla-d514`, `savla-d-514`, `savla d514 cold`) — present in that copy, absent from `canonicalize.py`.
3. **`a-185`, `a-185 cold` added.** **[V]** `canonicalize.py:74-75` has only `a185` / `warehouse a185`; the hyphenated form appears in real inventory data and currently matches nothing.
4. **Underscore normalisation retained** — matching is `strip().lower().replace("_", " ")`, per `canonicalize.py:93`. **[V]** Dropping it (as the v1 draft did) would break `new_savla`, `savla_bond`, `dev_int`.

### 7.1 Naming and null handling

**[V] Collision avoided.** The new function is `ledger_godown(warehouse)` — arity 1. The existing `canonical_warehouse(unit, storage_location)` is arity 2 and returns `None` for unrecognised values, leaving bucketing to the caller. Reusing that name with different arity and null semantics would be a trap.

`ledger_godown` never returns null-ish:

- `NULL` / empty → `'Unassigned'`
- recognised alias → canonical name
- unrecognised non-empty → passed through title-cased, never dropped

**[V] `t.warehouse` may well be NULL on the inward channel** — neither `{p}_transactions_v2` INSERT path (`inward_tools.py:1746-1756, 1908-1918`) writes it; it lands only via the edit path. The `Unassigned` bucket is therefore a live path, not a defensive edge case, and the service logs its row count.

### 7.2 Known ambiguities, logged not guessed

- Bare `savla` → `Savla D-39`, inherited from `inward_tools.py` (absent from `canonicalize.py`). With `Savla Bond` split out this is an assumption; the service logs how many rows resolve through it.
- **[V]** The `savla bond` split makes the ledger disagree with the transfer dashboard on the same raw value, because the other four copies still fold it into D-39. Either propagate the split or accept the divergence knowingly. **This spec accepts it** and records it here.

## 8. Backend module

```
server_replica/app/modules/ledger/
  __init__.py
  router.py                     # APIRouter(prefix="/api/v1/ledger")
  services/leaves_service.py    # union query, aggregation, entity fan-out
  services/godown_alias.py      # ledger_godown() + the merged alias map
```

Follows `lookups_router.py`: `AuthUser = Depends(get_current_user)`, pool via `request.app.state.db_pool`, `asyncpg.UndefinedTableError` caught and returned as empty rather than 500 — which also covers the case where a legacy table is absent in a given environment. Registered in `app/main.py` beside `lookups_router`.

**[V] Swagger tagging:** `tags=["Ledger"]` passed to `APIRouter` is discarded at import time and the operation is retagged automatically. To get a curated group, add `"ledger": "Ledger"` to `MODULES` and `"ledger"` to `MODULE_ORDER` in `app/core/openapi_tags.py`.

**Endpoint:** `GET /api/v1/ledger/leaves?entity=cfpl|cdpl|both` (default `both`) → `{"data": LeafItem[]}`. Read-only; no POST/PATCH/DELETE.

## 9. Frontend changes

**[V] The v1 claim of "no frontend type changes" was wrong** — three small additive changes are needed:

1. **`_LedgerData.tsx:20`** — `ENV_LIVE` becomes `process.env.NEXT_PUBLIC_LEDGER_LIVE !== "0"`. **[V]** The flip is at line 20, not line 34; line 34 already derives from `ENV_LIVE`. The comment at lines 5-12 needs updating to match.
2. **`LeafItem` gains `entity: "cfpl" | "cdpl"`**, and `LedgerApi.leaves()` gains an optional entity argument. **[V]** Without this, `entity=both` merely doubles the row count invisibly: the entity toggle at `page.tsx:40` (`CFPL | CDPL | Both`) is **currently decorative** — it is never passed to the data layer and neither `_tree.ts` nor `_company.ts` reads it. Stamping `entity` on each row lets the existing toggle filter client-side, keeping the single-leaf-feed architecture.
3. **`Inward only` chip in `_chrome.tsx`**, not beside the toggle. **[V]** The toggle renders only on the Stock Summary landing page; the chip must live in the module chrome to cover the drill, item and grain routes too.

### 9.1 Why the chip matters

With six movement columns hardcoded to `0`, derived Closing equals cumulative Inward. Every screen will show a column that looks like a stock balance and is not one. The chip is the guard against that being read as stock in a review.

## 10. Testing

`server_replica/tests/services/test_ledger_leaves.py`:

| Test | Guards against |
|---|---|
| Aggregation groups per `sku_id × godown × material_type` | Fragmented or over-merged leaves |
| Both channels present in output | Silently dropping bulk entry |
| PM rows yield `uom_class='nos'` from `quantity_units` | §4 regression |
| RM/FG rows yield `uom_class='kg'` from `net_weight` | §4 regression |
| `PM` / `pm` / ` Pm ` collapse to one leaf | Case-split leaves |
| `rtv=true` / `service=true` v2 rows excluded; bulk rows retained | §5.1 |
| Query compiles — every selected column grouped or aggregated | The v1 GROUP BY defect |
| Join uses both `transaction_no` and `_source` | Cross-family mixing |
| `old savla`, `savla d39`, `savla-d-39`, `SAVLA_D39` collapse to `Savla D-39` | Alias and underscore regression |
| `savla bond` → `Savla Bond` | §7 correction |
| `a-185 cold` → `A185` | §7 addition |
| NULL / empty warehouse → `Unassigned` | §7.1 live path |
| Unknown warehouse passes through, is not dropped | Silent total loss |
| `entity` filter isolates cfpl from cdpl | Cross-entity leakage |

## 11. Explicitly out of scope

- The six unsourced movement columns. **Closing is not a real stock figure until they land.**
- Box-level joins and box counts.
- The six new ledger tables from `TASKS.md` §9.2. This pass reads existing tables only; **no migration is added**.
- Per-view endpoints (`searchItems`, `stockSummary`, `lotsAvailable`, …). The single leaf feed drives every view.
- Joining `all_sku` by text to recover `item_type` / group data for rows where `a.material_type` is NULL.
- Propagating the `savla bond` split to the other four alias-map copies (§7.2).
- Any write path. Nothing here inserts, updates or deletes — **with one operational exception**: §3.2 recommends running `run_backfill_article_aggregates.py --confirm`, which does mutate legacy article aggregates. That is a separate, explicitly-approved operational step, not part of this implementation.
