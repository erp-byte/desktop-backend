# Transfer Summary — `/[company]/transfer/dashboard`

| | |
|---|---|
| **File** | `d:\test\frontend-\app\[company]\transfer\dashboard\page.tsx` (1131 lines) |
| **URL / Route** | `/[company]/transfer/dashboard` (the "View Summary" dashboard, reached from the Transfer landing page) |
| **Component** | `TransferDashboard` (default export, `page.tsx:65`) |
| **Purpose** | A single-screen, read-only analytics dashboard for inter-unit stock transfers. Loads ALL transfer-line records once, then does everything (KPIs, filters, grouped tree, search, copy/Excel export, transfer-detail popup) client-side. Stale-while-revalidate cache paints instantly on open. |
| **Access gate** | Email allowlist (`DASHBOARD_ALLOWED_EMAILS`) **plus** `PermissionGuard module="transfer" action="view"`. |

---

## 1. Route & params

- Next.js App Router dynamic segment page. Props type `Props { params: { company: string } }` (`page.tsx:31`).
- `const { company } = params` (`page.tsx:66`) — the only route param. Used for:
  - The back-link target `/${company}/transfer` (`page.tsx:364`).
  - The persisted-state namespace `NS = ${company}:transfer-dashboard` (`page.tsx:88`) — every sessionStorage filter key is company-scoped.
  - The localStorage cache key (company-scoped) via `readTransferCache(company)` / `writeTransferCache(..., company)` (`page.tsx:186, 201`).
- **No query-string params** are read or written. Filters live in sessionStorage, not the URL.
- Auth/email gate (`page.tsx:23, 67-80`): `useAuthStore()` supplies `user`; access is granted only if `user.email` is in `DASHBOARD_ALLOWED_EMAILS = ["yash@candorfoods.in", "b.hrithik@candorfoods.in"]`. Otherwise an "Access Restricted" card renders and the rest of the component never mounts.
  - NOTE: the access check returns early *before* the hooks are declared (`page.tsx:70`). This is a conditional early-return-before-hooks pattern (see Gotchas).

---

## 2. Layout & structure (top → bottom)

Rendered inside `<PermissionGuard module="transfer" action="view">` → centered container `max-w-[1600px]` (`page.tsx:357-358`):

1. **Header row** (`page.tsx:360-390`): back arrow → `/${company}/transfer`; title "Transfer Summary"; subtitle "As of <today>" with conditional pills (last-updated time, "refreshing…", "refresh failed", active-filter count); right-side action buttons (Refresh, Copy, Excel, WhatsApp-disabled).
2. **Filters card** (`page.tsx:392-574`) — only when `!loading && filterOpts`. Four rows:
   - Row 1 — date presets + date range inputs (`page.tsx:397-413`).
   - Row 2 — From / To warehouse chip multi-selects with a "ship direction" divider (`page.tsx:415-502`).
   - Row 3 — Status chips + Issues-only toggle (`page.tsx:504-533`).
   - Row 4 — Category + Material chips (`page.tsx:535-561`).
   - "Clear all filters" footer (`page.tsx:563-570`).
3. **Error banner** (`page.tsx:576`) — only on first-load failure.
4. **KPI grid** (`page.tsx:578-599`) — 6-up responsive grid of `<KPI>` cards.
5. **Active-filter summary chips** (`page.tsx:601-611`) — "Showing results for:" colored pills, only when a dimension is locked.
6. **Group/View/Expand controls + Search/Sort** (`page.tsx:613-676`).
7. **Summary tree table** (`page.tsx:678-753`) — the main hierarchical grouped table inside a Card, with sticky header and a Grand Total footer row.
8. **Transfer Detail popup** (`page.tsx:755-760`) — `<TransferDetailPopup>` dialog (hidden until a transfer is clicked).

Helper sub-components defined in-file: `TransferDetailPopup` (`page.tsx:769`), `InfoCell` (`page.tsx:996`), `ItemRow` (`page.tsx:1005`), `TransferRows` (`page.tsx:1030`), `KPI` (`page.tsx:1110`), `TableSkel` (`page.tsx:1126`).

---

## 3. Dashboards / KPI cards / charts / chips

**Charts/graphs: NONE.** This dashboard has no chart/graph library (no recharts/chart.js). All "visualization" is the KPI cards + the hierarchical grouped table. The only graphical motifs are the per-transfer route indicator (two dots + connector line, `page.tsx:1055-1059`) and the colored from/to chip dots.

All KPIs derive from the single `kpis` memo (`page.tsx:256-273`), which is computed over `filtered` (the client-filtered, search-pruned record set). All aggregations are line-level reductions; transfer counts use `Set` over `transfer_id` to dedupe multiple lines per transfer.

### KPI cards (grid at `page.tsx:578-599`, card component `KPI` at `page.tsx:1110`)

| Card label | Line | Metric value shown | Exact computation | Data source |
|---|---|---|---|---|
| **Total Transfers** | 581 | `fmtN(kpis.total_transfers)` | `new Set(filtered.map(r => r.transfer_id)).size` (`page.tsx:257, 267`) | `filtered` |
| **Net / Gross Weight** (only if viewMode kgs/both) | 582-593 | `fmtN(net‖gross) + " Kgs"` and, if gross>0 and gross≠net, `" / <gross>"` | `total_net_weight = Σ r.net_weight`; `total_gross_weight = Σ r.total_weight` (`page.tsx:258-259, 269-270`). Primary = net, falls back to gross. | `filtered` |
| **Total Boxes** (only if viewMode boxes/both) | 594 | `fmtN(kpis.total_boxes)` | `Σ r.box_count` (`page.tsx:260, 271`) | `filtered` |
| **Pending / Transit** | 595 | `fmtN(kpis.pending_count)`, amber if >0 | `new Set(filtered.filter(status==="Dispatch"‖"Pending").map(transfer_id)).size` (`page.tsx:261, 271`) | `filtered` |
| **Issues** | 596 | `"<issue_transfers> TRs / <issue_items> items"`, red if issue_transfers>0 | `issue_transfers = Set(filter(has_issue).map(transfer_id)).size`; `issue_items = Σ over has_issue rows of issue_count` (`page.tsx:264-265, 271`) | `filtered` |
| **Not Received** | 597 | `fmtN(kpis.not_received)`, amber if >0 | `new Set(filtered.filter(received_status !== "Received").map(transfer_id)).size` (`page.tsx:262, 271`) | `filtered` |

Other computed KPI fields not directly carded but used elsewhere: `total_weight = totalNet ‖ totalGross` (`page.tsx:268`, used in Grand Total row and copy), `warehouses_active = Set(from+to).size` (`page.tsx:263, 271`, computed but **not surfaced in any card** — dead/unused KPI).

`fmtN` rounds and formats `en-IN` (`page.tsx:33`); `fmtR` (rupees) is declared (`page.tsx:34`) but **unused** on this page.

### Chips (filter chips, doubling as the dimensional breakdown)

- **From / To warehouse chips** — `cascadedOpts.from_warehouses` / `.to_warehouses` (`page.tsx:432-499`). Display via `getDisplayWarehouseName`. Selecting a From auto-removes that WH from To (`page.tsx:441-442`); a WH already in From is disabled in To (`page.tsx:478, 482-483`).
- **Status chips** — `cascadedOpts.statuses` (`page.tsx:510-522`) with per-status color maps (dispatch=blue, received=emerald, pending=amber, fallback=teal).
- **Issues toggle chip** — shows live count badge `kpis.issue_transfers` (`page.tsx:524-531`).
- **Category chips** (violet) / **Material chips** (orange) — `cascadedOpts.categories` / `.materials` (`page.tsx:537-560`).
- **Active-filter summary pills** (`page.tsx:602-611`) — readonly echo of currently-locked dimensions.
- **Inline tree badges**: L1 rows show `<n> pending` amber pill when `l1.pending_count > 0` (`page.tsx:705`); item rows show a material-type pill (`page.tsx:1019`); transfer rows show status / received / issue-count pills (`page.tsx:1064-1075`).

---

## 4. Tables & columns

### A) Main summary tree table (`page.tsx:688-750`)

A single `<table>` rendering a 1–3-level expandable hierarchy built by `buildSummary` (see §10). Sticky header (`thead sticky top-0 z-10`, `page.tsx:689`).

Columns:

| # | Header | Line | Content |
|---|---|---|---|
| 1 | **Category** (generic label; really the group label) | 691 | Group/item/transfer label + chevron + inline pending/material/status pills |
| 2 | **TRs** | 692 | `tx_count` (deduped transfer count) |
| 3 | **Weight (Kgs)** / **Boxes** / **Weight / Boxes** (header text switches on `viewMode`, `page.tsx:693`) | 693 | `showVal(...)` block — net (+gross if differs) and/or boxes |
| 4 | **Pending** | 694 | `pending_count` (blank if 0) |

Row tiers:
- **L1 group row** (`page.tsx:701-711`) — dark `#0f172a` bg, the `groupBy` dimension value.
- **L2 group row** (`page.tsx:717-722`) — slate bg, teal left border, the `thenBy` dimension value (only when `thenBy !== "none"`).
- **L3 item row** (`ItemRow`, `page.tsx:1005-1027`) — `item_description` + material pill.
- **L4 transfer detail rows** (`TransferRows`, `page.tsx:1030-1108`) — one card-style row per **deduped** `transfer_id` (challan, status/received/issue pills, date, from→to, vehicle, driver, lot, line count, boxes, inline issue list). Clicking the challan opens the popup.
- **Grand Total footer** (`page.tsx:743-748`) — dark bg; sums from `kpis`.

### B) Transfer Detail popup — Line Items table (`page.tsx:858-959`)

Columns: expand-chevron, `#`, **Item**, **Category** (`item_category / sub_category`), **Type** (material_type pill), **Lot**, **Qty**, **Net Wt (Kg)**, **Total Wt (Kg)** (`page.tsx:861-869`). Each row expands to inline editable Net/Gross weight inputs (`page.tsx:901-946`). Footer **Total** row auto-recomputes from any local weight overrides (`page.tsx:950-957`).

### C) Transfer Detail popup — Issues table (`page.tsx:964-990`)

Only when `hdr.has_issue && hdr.issue_details.length > 0`. Columns: **Article**, **Remarks**, **Actual Wt** (`page.tsx:973-975`), rows from `hdr.issue_details`.

---

## 5. Buttons

| Label | Line | Handler | Action / Redirect |
|---|---|---|---|
| Back arrow (`ArrowLeft`) | 363-365 | `<Link>` | Navigate to `/${company}/transfer` |
| Refresh | 377-379 | `() => fetchData({ silent: true })` | Background re-fetch (no skeleton); spinner animates while loading/refreshing |
| Copy | 380-382 | `handleCopy` (`page.tsx:329-339`) | Writes a text summary to clipboard; label flips to "Copied!" for 2s |
| Excel | 383-385 | `handleExport` (`page.tsx:341-354`) | Dynamic-imports `xlsx`, writes `Transfer_Summary_<ddMMMyyyy>.xlsx` |
| WhatsApp | 386-388 | none (`disabled`, `opacity-50`) | **Not implemented** — placeholder |
| Date preset buttons (Today / This Month / Last Month / All Time) | 401-406 | `() => { const [f,t]=p.fn(); setDateFrom(f); setDateTo(t) }` | Set date range |
| "Clear (n)" From / To | 423, 468 | `setSelFrom(new Set())` / `setSelTo(new Set())` | Reset that WH selection |
| From WH chip | 435-451 | inline toggle (also strips matching To) | Filter |
| To WH chip | 481-497 | inline toggle (disabled if same as From) | Filter |
| Status chip | 518 | `setSelStatus(chipToggle(selStatus, s))` | Filter |
| Issues toggle | 524 | `() => setShowIssuesOnly(!showIssuesOnly)` | Filter to issue rows |
| Category chip | 542 | `setSelCategory(chipToggle(...))` | Filter |
| Material chip | 554 | `setSelMaterial(chipToggle(...))` | Filter |
| Clear all filters | 566 | `clearFilters` (`page.tsx:312`) | Reset every filter |
| Group dimension buttons | 620-623 | `setGroupBy(g.value); setThenBy(DEFAULT_THEN_BY[..]); reset expand` | Change L1 grouping |
| then-by `<select>` | 628-635 | `setThenBy(...)` + reset expand | Change L2 / "None" |
| View buttons (Kgs/Boxes/Both) | 640-645 | `setViewMode(v)` | Switch metric display |
| Expand All / Collapse All | 649-651 | `toggleAll` (`page.tsx:307-310`) | Expand/collapse whole tree |
| Sort buttons (Weight/Boxes/Count/A-Z) | 670-672 | `setSortBy(s.value)` | Re-sort all tree layers |
| Search clear (X) | 659 | `setSearchQuery("")` | Clear search |
| Empty-state "Clear Filters" | 684 | `clearFilters` | Reset filters |
| Tree row click (L1/L2/L3) | 701, 717, 726, 737 | `toggle(key)` | Expand/collapse that node |
| Challan link (in TransferRows) | 1063 | `onClickTransfer(tx.transfer_id)` (stops propagation) | Open Transfer Detail popup |
| Popup line-item row | 882 | `toggleLine(i)` | Expand inline weight editor |
| Popup "Reset all weight edits" | 852 | `setWeightOverrides({})` | Clear local edits |
| Popup "Reset this line" | 935 | `resetOverride(i)` | Clear one line's edits |

---

## 6. Filters / date ranges / search

All filtering is **client-side** against `allRecords`. Filter state is persisted to **sessionStorage** (survives back/forward in-tab) via `usePersistedState` with company-namespaced keys (`page.tsx:88-100`). Set-typed filters use `setSerializers` (`page.tsx:91-95`).

Persisted filter state:
- `dateFrom`, `dateTo` (strings) (`page.tsx:89-90`)
- `selFrom`, `selTo`, `selCategory`, `selMaterial`, `selStatus` (`Set<string>`) (`page.tsx:91-95`)
- `showIssuesOnly` (bool) (`page.tsx:96`)
- Also persisted (view prefs): `groupBy`, `thenBy`, `viewMode` (`page.tsx:98-100`). NOT persisted: `expanded`, `searchQuery`, `sortBy`, `selectedTransfer` (plain `useState`).

**Date range**: presets `DATE_PRESETS` (`page.tsx:54-59`) = Today, This Month, Last Month, All Time (clears both). Manual `<input type="date">` for from/to (`page.tsx:409-411`). Comparison is plain string compare on `transfer_date` (`r.transfer_date < dateFrom` / `> dateTo`, `page.tsx:243-244`) — works because dates are ISO `YYYY-MM-DD` from the backend.

**Cascading filter options** (`cascadedOpts`, `page.tsx:138-157`): each chip group's options are recomputed by applying ALL *other* active filters first (the `fex(exclude)` helper), so chips only show values that still produce results given the rest of the selection. Sorted, deduped, blanks dropped.

**Locked dimensions** (`lockedDimensions`, `page.tsx:116-124`): a single-value selection on a dimension "locks" it so it can no longer be a group/then-by axis. `availableGroupOptions` and `thenByOptions` exclude locked dims (`page.tsx:127-135`). Two effects auto-correct `groupBy`/`thenBy` if they become locked or collide (`page.tsx:214-229`).

**Smart search** (`page.tsx:231-239, 654-660`): `makeRecordSearch(searchQuery, TRANSFER_SEARCH_FIELDS)` builds a predicate. Multi-term, AND semantics, case-insensitive substring match over 17 fields (`TRANSFER_SEARCH_FIELDS`, `page.tsx:47-52`). Search is applied inside `filtered` (`page.tsx:251`) so it drives KPIs, tree, and Grand Total uniformly. While searching, the whole tree auto-expands (`effExpanded = isSearching ? allKeys : expanded`, `page.tsx:305`) and the user's manual expansion is preserved for when search clears.

**`filtered` memo** (`page.tsx:242-253`) is the single source for everything downstream: date → from → to → category → material → status → issues-only → search.

`activeFilterCount` (`page.tsx:111-113`) counts non-empty dates + non-empty Sets + issues-only; shown in the header pill and Clear-all label.

---

## 7. Pagination

**None.** The dashboard loads ALL records in one `/transfer-dashboard/all-data` call and renders the entire grouped tree without paging, virtualization, or infinite scroll. The only "more rows" mechanism is expand/collapse of the tree.

---

## 8. Page-in-page & hover actions

- **Page-in-page**: the **Transfer Detail popup** (`TransferDetailPopup`, `page.tsx:769-994`) is a shadcn `Dialog` (`page.tsx:815`, `max-w-3xl max-h-[85vh] overflow-y-auto`). It opens when `selectedTransfer` is set by clicking a challan in `TransferRows` (`page.tsx:1063`) and is the closest thing to a sub-page. It contains its own header info grid, an expandable line-item table with **inline local weight editing** (`weightOverrides`, `page.tsx:779, 796-812`), and an issues table.
- **Hover actions**: purely visual hover styling (chips, rows change bg/border on hover, e.g. `page.tsx:701, 1014, 1063`). No hover-triggered menus or tooltips beyond the native `title` on a disabled To-chip (`page.tsx:483`). The Copy button uses a transient "Copied!" state rather than a hover tooltip.

---

## 9. Keyboard / ESC / click directions

- **ESC / overlay click**: handled by the shadcn `Dialog` primitive — `onOpenChange={o => !o && onClose()}` (`page.tsx:815`) closes the popup on ESC or backdrop click. `onClose` sets `selectedTransfer` to null (`page.tsx:759`).
- **No custom keyboard handlers** (no `onKeyDown`, arrow-key navigation, or shortcut listeners) anywhere on the page.
- **Click directions**: tree rows toggle expansion on click; the challan link uses `e.stopPropagation()` (`page.tsx:1063`) so opening the popup does NOT also toggle its row. Popup line-item rows toggle their inline editor on click (`page.tsx:882`).

---

## 10. Functionality & logic flows

**Data loading (stale-while-revalidate)** (`page.tsx:176-211`):
1. On mount, `readTransferCache(company)` (localStorage key `transfer-dashboard:<company>:cache:v1`, `transferDashboardApi.ts:48`). If a cache exists: paint immediately via `applyData`, set `lastUpdated` from cache, clear loading, then `fetchData({ silent: true })` to revalidate in the background (table stays on screen, only a "refreshing…" pill shows).
2. If no cache: `fetchData({ silent: false })` shows the `TableSkel` skeleton.
3. `fetchData` runs `Promise.all([getAllData(), getFilterOptions()])` (`page.tsx:181-184`), calls `applyData`, then `writeTransferCache(data, fopts, company)` and sets `lastUpdated = Date.now()`.
4. Silent failures keep cached data and set `refreshError`; first-load failures set `error` (banner).

**Normalization** (`applyData`, `page.tsx:161-172`): each raw record gets `from_warehouse`/`to_warehouse` → `normalizeWarehouseName` (alias/canonical folding, `warehouses.ts:179-192`) and `item_category`/`sub_category`/`material_type` → `canonicalizeCategory` (case/space/separator folding, `canonicalize.ts:44-54`). This collapses case/spacing duplicate buckets in chips and grouping. Raw records are cached (pre-normalization) so a future normalization change can't serve a stale shape (`transferDashboardApi.ts:42-47`).

**Aggregation / grouping** (`buildSummary`, `buildSummary.ts:131-169`):
- L1 = `groupBy`, L2 = `thenBy` (or none), leaf = `item_description` rows (`ItemNode`s that keep their raw `records` for the popup).
- Dimension accessors `DIM_FN` map records to bucket keys, with fallbacks ("Unknown"/"Uncategorized"/"General"/"N/A") (`buildSummary.ts:56-65`).
- Per-node metrics (`groupMetrics` / `buildItems`): `total_net_weight = Σ net_weight`, `total_gross_weight = Σ total_weight`, `total_weight = net ‖ gross`, `total_boxes = Σ box_count`, `tx_count = Set(transfer_id).size`, `pending_count` = deduped count of Dispatch/Pending transfers (`buildSummary.ts:67-129`).
- Sort (`sortNodes`, `buildSummary.ts:81-94`) applied uniformly at every layer: weight (default), boxes (tiebreak weight), count (tiebreak weight), name (localeCompare). `DEFAULT_THEN_BY` (`buildSummary.ts:45-54`) picks a sensible L2 per chosen L1.
- Search is NOT done in `buildSummary` — callers pre-filter (`filtered` already includes search) so KPIs/totals/tree all reflect the same set.

**Tree expansion** (`page.tsx:283-310`): `allKeys` enumerates every expandable node key (using `|||` delimiters). `toggleAll` expands/collapses all; while searching, `effExpanded` forces all keys open.

**Copy/share** (`handleCopy`, `page.tsx:329-339`): builds a plain-text summary (`Transfer Summary - <date>`, totals line, one line per L1 group with TRs/weight/boxes) and writes to `navigator.clipboard`. No backend call. There is **no snapshot/share-link feature**; WhatsApp is a disabled placeholder.

**Export** (`handleExport`, `page.tsx:341-354`): dynamic `import("xlsx")`, maps `filtered` to flat rows (Challan, Date, From, To, Item, Category, Material, Qty, Net Weight, Total Weight, Boxes, Status, Received), writes `Transfer_Summary_<ddMMMyyyy>.xlsx`. Exports the **filtered line-level** data, not the grouped view.

**Popup local weight editing** (`page.tsx:779-812`): `weightOverrides` keyed by line index; `effectiveNet`/`effectiveGross` apply overrides; header + footer totals recompute live. Edits are explicitly local-only — "they recompute the totals below without saving to the backend" (`page.tsx:940-942`). Overrides reset when `transferId` changes (`page.tsx:782-785`).

**Caching summary**: localStorage SWR cache for records/filter-options (company-scoped, `v1`); sessionStorage for filter/view state (company-namespaced). `clearTransferCache` exists in the API module (`transferDashboardApi.ts:83-90`) but is **not called** from this page.

---

## 11. Redirects

- The only navigation is the back arrow `<Link href={/${company}/transfer}>` (`page.tsx:363-364`) → Transfer landing page.
- No programmatic `router.push`/`router.replace`, no redirect-on-no-data, no redirect-on-error. The email gate (`page.tsx:70-80`) renders an inline "Access Restricted" panel rather than redirecting; `PermissionGuard` without `view` permission renders `null` (no redirect, `permission-gate.tsx:30-47`).

---

## 12. API calls

The page uses its **own** `transferDashboardApi` (`d:\test\frontend-\lib\api\transferDashboardApi.ts`), NOT `interunitApiService`. Base URL `process.env.NEXT_PUBLIC_API_URL` (default `http://localhost:8000`), Bearer token from `useAuthStore` (`transferDashboardApi.ts:3-10`).

| Method | Endpoint | Params | Purpose | Frontend call site |
|---|---|---|---|---|
| GET | `/transfer-dashboard/all-data` | none | Fetch ALL transfer-line records (header⋈lines) joined with box counts, transfer-in received status, and issue details | `transferDashboardApi.getAllData()` (`transferDashboardApi.ts:34-36`), invoked in `fetchData` (`page.tsx:182`) |
| GET | `/transfer-dashboard/filter-options` | none | Distinct values for filter chips (from/to warehouses, statuses, categories, materials, created_by) | `transferDashboardApi.getFilterOptions()` (`transferDashboardApi.ts:37-39`), invoked in `fetchData` (`page.tsx:183`) |

Notes:
- Both are GET, no query params; all filtering/aggregation is client-side.
- The router is mounted directly (`app.include_router(transfer_dashboard_router)`, `ims-app-backend\main.py:255`) with prefix `/transfer-dashboard` (`transfer_dashboard_server.py:19`) and **no extra global prefix**.
- `interunitApiService.getStatsSummary` → `/interunit/stats/summary` (`interunitApiService.ts:371-378`) is **NOT used** by this page (the task's "if used" caveat applies — it is not). No raw `fetch` calls exist in `page.tsx` beyond the two above (via the service module).

---

## 13. Backend & DB wiring touched

Backend file: `d:\test\ims-app-backend\services\ims_service\transfer_dashboard_server.py` (router prefix `/transfer-dashboard`).

**`GET /all-data`** (`transfer_dashboard_server.py:30-147`):
- Main query (`:34-61`): `interunit_transfers_header h INNER JOIN interunit_transfers_lines l ON h.id = l.header_id`, filtered by `LINE_FILTER` (`:27`) = line must have net_weight>0 OR total_weight>0 OR qty>0. Ordered by `h.stock_trf_date DESC`.
  - Selected/aliased columns → frontend `TransferRecord`: `h.id`→transfer_id, `h.challan_no`, `h.stock_trf_date`→transfer_date + `TO_CHAR(..,'YYYY-MM')`→transfer_month, `h.from_site`→from_warehouse, `h.to_site`→to_warehouse, `h.vehicle_no`, `h.driver_name`, `h.status`, `h.created_by`, `h.remark`; line: `l.item_desc_raw`→item_description, `l.item_category`, `l.sub_category`, `l.rm_pm_fg_type`→material_type, `l.lot_number`, `l.qty`, `l.uom`, `l.pack_size`, `ROUND(l.net_weight,2)`, `ROUND(l.total_weight,2)`.
- Box counts (`:84-91`): `SELECT header_id, COUNT(*) FROM interunit_transfer_boxes GROUP BY header_id` → `rec["box_count"]`.
- Received status (`:94-100`): `SELECT transfer_out_id, status FROM interunit_transfer_in_header` → `rec["received_status"]` (default `"Not Received"`).
- Issues (`:102-141`): from `interunit_transfer_in_boxes tib JOIN interunit_transfer_in_header tih` where `tib.issue` JSONB is non-null/non-empty. Extracts `tib.issue->>'remarks'`, `->>'actual_qty'`, `->>'actual_total_weight'`, `tib.article`, `tib.net_weight`. Grouped by `transfer_out_id` into `issue_count`, `issue_items` (comma-joined articles), `issue_weight` (Σ net_weight), `issue_details[]`, and `has_issue = issue_count > 0`.
- Returns `{ records, total, as_of_date }`.

**`GET /filter-options`** (`transfer_dashboard_server.py:150-196`): six distinct-value queries:
- `from_warehouses` / `to_warehouses` — distinct `h.from_site` / `h.to_site` from header⋈lines with LINE_FILTER (`:156-168`).
- `statuses` — distinct `status` from header (`:170-173`).
- `item_categories` — distinct `l.item_category` (LINE_FILTER) (`:175-179`).
- `material_types` — distinct `l.rm_pm_fg_type` (LINE_FILTER) (`:181-185`).
- `created_by` — distinct `created_by` from header (`:187-190`).

**DB tables touched** (read-only SELECTs; no writes):
- `interunit_transfers_header`
- `interunit_transfers_lines`
- `interunit_transfer_boxes`
- `interunit_transfer_in_header`
- `interunit_transfer_in_boxes` (issue JSONB)

(Listed in the module docstring `transfer_dashboard_server.py:3-4`, plus `interunit_transfer_in_boxes` used at `:110`.)

---

## 14. Cross-module linkages

- **Transfer landing page** — only outbound link, via back arrow → `/${company}/transfer` (`page.tsx:364`). This dashboard is the "View Summary" destination from that landing page.
- **Transfer-In module** — received status and issue data come from `interunit_transfer_in_header` / `interunit_transfer_in_boxes` (the Transfer-In side), so the dashboard reflects state created by the Transfer-In flow (documented separately in `02-transfer-in.md`).
- **Shared frontend libs reused across dashboards**:
  - `lib/search/recordSearch.ts` — same smart-search engine used by inward / cold-storage / RTV / job-work dashboards (`recordSearch.ts:1-15`).
  - `lib/hooks/usePersistedState.ts` — generic sessionStorage-backed state used by other dashboards.
  - `lib/constants/warehouses.ts` — single source of truth for warehouse codes/aliases/display names across the whole app.
  - `lib/categories/canonicalize.ts` — shared category folding.
  - `lib/transfer/buildSummary.ts` — transfer-specific grouping engine (only used here, but mirrors the inward dashboard's pattern).
- **Auth** — `useAuthStore` (Bearer token, `user`, `hasPermission`) and `PermissionGuard` are app-wide.
- **Sibling backend dashboards** mounted alongside in `main.py`: `inward_dashboard_router`, `jobwork_dashboard_router`, `dashboard_router` (same architectural pattern).

---

## 15. Gotchas

1. **Hooks after a conditional return.** The email-gate early `return` (`page.tsx:70-80`) sits *before* every `useState`/`useEffect`/`useMemo` (declared from `page.tsx:82`). React's Rules of Hooks forbid conditional hook execution; this works only because `hasAccess` is stable per render for a given user, but it is fragile (e.g., if `user` flips from undefined→defined the hook order changes and React will error). Worth flagging.
2. **Double access gate.** Both the email allowlist AND `PermissionGuard module="transfer" action="view"` must pass. A user with transfer-view permission but a non-allowlisted email still sees "Access Restricted". The allowlist is a hardcoded 2-email array (`page.tsx:23`).
3. **All data loaded at once, no pagination.** Every transfer line is fetched and rendered client-side. Scales poorly as transfer volume grows; the entire tree re-aggregates in JS on every filter/search/sort change (multiple `useMemo`s over `allRecords`/`filtered`).
4. **Two caches with different stores and lifetimes.** Records use **localStorage** (`v1`, persists across sessions); filter/view state uses **sessionStorage** (per-tab). A schema change to `TransferRecord` shape requires bumping the `v1` cache key or stale shapes can be served on first paint. `clearTransferCache` exists but is never invoked here.
5. **String date comparison.** Date filtering compares ISO strings directly (`page.tsx:243-244`). Correct only because backend emits `YYYY-MM-DD`; any non-ISO/empty `transfer_date` would mis-sort/mis-filter.
6. **`warehouses_active` KPI and `fmtR` are dead code** (computed/declared but never rendered — `page.tsx:263, 34`).
7. **Popup weight edits are non-persistent.** Inline Net/Gross edits in `TransferDetailPopup` only affect that view's totals; nothing is saved to the backend (`page.tsx:940-942`). Easy to mistake for an editable form.
8. **`TransferRows` middle "TRs" cell is intentionally blank** — `{tx.line_count > 1 ? "" : ""}` always renders empty (`page.tsx:1103`), i.e. a no-op ternary; transfer-detail rows never show a TR count.
9. **`received_status` default mismatch.** Backend defaults missing received status to `"Not Received"` (`transfer_dashboard_server.py:100`), but the `not_received` KPI counts anything `!== "Received"` (`page.tsx:262`), so transfers with no transfer-in row are counted as not-received — by design, but a source of confusion vs. the "Received" status chip.
10. **Status chip `pending` color exists but backend statuses are header `status` values** (e.g. Dispatch/Approved); "Pending" is treated as pending in KPIs/buildSummary (`page.tsx:261`, `buildSummary.ts:73`) but may not appear as a distinct DB status — verify against actual `interunit_transfers_header.status` domain.
11. **WhatsApp button is a disabled placeholder** (`page.tsx:386-388`) — no share-to-WhatsApp implemented despite the icon.
12. **`issue_count`/`issue_weight` are per-transfer but stored on every line record.** The backend stamps the same issue aggregate onto each line of a transfer (`transfer_dashboard_server.py:135-141`); the `issue_items` KPI sums `issue_count` over *all has_issue rows* (`page.tsx:265`), which counts a transfer's issue_count once per line — likely an over-count if a transfer has multiple lines (worth verifying expected semantics).
