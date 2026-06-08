# Job Work Dashboard — `/[company]/transfer/job-work`

- **File:** `d:\test\frontend-\app\[company]\transfer\job-work\page.tsx` (2874 lines)
- **URL:** `/{company}/transfer/job-work` (e.g. `/cfpl/transfer/job-work`)
- **Purpose:** Single-page console for **outsourced processing (Job Work)**. It is a three-tab workspace that (a) lists all Job Work Orders / Material-Out challans, (b) records **Material In (Inward Receipts)** against a dispatched challan — including FG/Waste/Rejection reconciliation, loss-tolerance gating, and box-ID + QR-label generation, and (c) renders a **Reports/Dashboard** with KPI cards and process/vendor/item/trend/matrix breakdowns. The "create" of a new outward dispatch is delegated to a child route (`material-out`); this page is the hub around it.

---

## 1. Route & params

- App-Router dynamic segment page. Default export `JobWorkPage({ params })` — `page.tsx:110`.
- Props interface `JobWorkPageProps` — `page.tsx:51-55`: `params: { company: Company }`.
- `const { company } = params` — `page.tsx:111`. `company` is the only route param; it is interpolated into all `router.push` targets and used to seed the cold-storage company default (`page.tsx:158`).
- **No search/query params are read by this page.** It is `"use client"` (`page.tsx:1`) and does not use `useSearchParams`. (Note: the child `material-out` route it links to *does* consume `?edit=` and `?challan=`, but those are not parsed here.)
- Hooks initialized: `useRouter()` `page.tsx:112`, `useToast()` `page.tsx:113`, `useAuthStore()` → `user` `page.tsx:114`.
- **Delete gate:** `DELETE_ALLOWED_EMAILS = ["b.hrithik@candorfoods.in","yash@candorfoods.in"]` `page.tsx:115`; `canDelete` computed from `user.email` `page.tsx:116`. Controls visibility of every Delete button on the page.

---

## 2. Layout & structure (tabs/sections)

Outer wrapper `max-w-7xl` container — `page.tsx:978`.

**Header** — `page.tsx:980-994`:
- Back button (ghost, ArrowLeft) → `/${company}/transfer` `page.tsx:982-984`.
- Title "Job Work" + subtitle "Outsourced processing — Material Out & Inward Receipts" `page.tsx:986-988`.
- Primary action "New Material Out" → `/${company}/transfer/job-work/material-out` `page.tsx:990-993`.

**Tabs** — `<Tabs value={activeTab} onValueChange={setActiveTab}>` `page.tsx:996`; `activeTab` state defaults to `"records"` `page.tsx:118`. `TabsList` is a 3-column grid `page.tsx:997-1007`:

| Tab value | Label | Icon | Defined at | Content at |
|-----------|-------|------|-----------|-----------|
| `records` | Records | ClipboardList | `page.tsx:998-1000` | `page.tsx:2051-2278` |
| `create-in` | Material In / "In" | Inbox | `page.tsx:1001-1003` | `page.tsx:1012-2046` |
| `reports` | Reports / "Report" | BarChart3 | `page.tsx:1004-1006` | `page.tsx:2283-2675` |

**Material In tab** is the largest; its internal sections (all conditionally rendered):
1. Search Material Out Challan card — `page.tsx:1016-1044`.
2. Inward Receipt Records list (shown only when `!miFoundRecord`) — `page.tsx:1047-1225`.
3. Found Record (JWO) Summary + Cumulative Summary + Prior IR history + Loss-config banner — `page.tsx:1228-1329`.
4. Inward Challan No + Receipt Date — `page.tsx:1332-1360`.
5. Items table (FG / Waste / Rejection entry) — `page.tsx:1363-1488`.
6. Loss tolerance alert — `page.tsx:1491-1497`.
7. Return transport details — `page.tsx:1500-1528`.
8. Empty-state placeholder (when `!miFoundRecord`) — `page.tsx:1531-1546`.
9. **Article Entry — Box IDs & QR labels** (shown only when `miFoundRecord`) — `page.tsx:1554-2008`.
10. Submit row (Clear / Submit Partial / Submit Final) — `page.tsx:2011-2044`.

**Records tab:** header+refresh `page.tsx:2053-2062`, filters `page.tsx:2064-2099`, desktop table `page.tsx:2130-2214`, mobile cards `page.tsx:2216-2260`, pagination `page.tsx:2262-2274`.

**Reports tab:** filters card `page.tsx:2286-2357`, KPI grid `page.tsx:2366-2386`, status distribution `page.tsx:2388-2406`, sub-view tab bar + search `page.tsx:2408-2433`, five breakdown views `page.tsx:2435-2612`, Inward Summary panel `page.tsx:2614-2660`.

**Modal:** View Inward Receipt dialog — `page.tsx:2679-2870` (rendered outside the Tabs, page-level).

---

## 3. KPI cards / chips / dashboards

### 3a. Reports KPI cards (5) — `page.tsx:2366-2386`
Array config at `page.tsx:2367-2372`. All values from `rptSummary` (= `rptData.summary`, `page.tsx:950`), which comes from `GET /job-work/reports/dashboard`.

| Label | Metric / Value expr | Computation | Source field |
|-------|--------------------|-------------|--------------|
| Total JWOs | `rptSummary.total_jwo ?? 0` | count | `summary.total_jwo` |
| Dispatched | `(total_dispatched_kgs).toLocaleString() kg` | sum | `summary.total_dispatched_kgs` |
| FG Received | `(total_fg_kgs).toLocaleString() kg` | sum | `summary.total_fg_kgs` |
| Waste + Rejection | `(total_waste_kgs + total_rejection_kgs).toLocaleString() kg` | client sum of two fields `page.tsx:2371`; amber styling | `summary.total_waste_kgs`, `summary.total_rejection_kgs` |
| Overall Loss | `${overall_loss_pct}%` | server-computed; turns **red** when `> 10` (`red: rptSummary.overall_loss_pct > 10` `page.tsx:2372`) | `summary.overall_loss_pct` |

### 3b. Status Distribution chips — `page.tsx:2388-2406`
`Object.entries(rptStatusCounts)` (`rptData.status_counts`, `page.tsx:951`) → one chip per status = `getStatusBadge(status)` + count `page.tsx:2397-2402`. Empty → "No data" `page.tsx:2403`.

### 3c. Inward Summary panel (4 stat tiles + stacked bar) — `page.tsx:2614-2660`
- Inward Receipts = `rptSummary.total_irs` `page.tsx:2624`.
- FG Received (Kg) = `rptSummary.total_fg_kgs` `page.tsx:2628`.
- Waste (Kg) = `rptSummary.total_waste_kgs` `page.tsx:2632`.
- Rejection (Kg) = `rptSummary.total_rejection_kgs` `page.tsx:2636`.
- "% accounted" = `(total_dispatched_kgs - unaccounted_kgs)/total_dispatched_kgs*100` `page.tsx:2644`.
- Stacked bar segments (FG/Waste/Rejection as % of dispatched) `page.tsx:2647-2649`; remaining grey = unaccounted (legend `page.tsx:2651-2655`).

### 3d. Material-In **Found-Record** summary tiles — `page.tsx:1238-1277`
Five mini-cards: Date, Vendor, Process, **Material-In Warehouse (a Select, not static)** `page.tsx:1251-1269`, Status (humanized from `miFoundRecord.status`/`miReceiveCount` `page.tsx:1271-1276`). Source = `miFoundRecord` from `GET /job-work/out/search`.

### 3e. Material-In **Cumulative Summary** panel — `page.tsx:1281-1317`
Shown when `miReceiveCount > 0`. Five inline stats computed client-side from `totals` (`page.tsx:861-870`): Total Dispatched, FG Received, Waste Received (if `showWasteColumn`), Rejection, Remaining Balance (`sent − prev_fg − prev_waste − prev_rejection`). Below it, **Receipt History** list iterating `miPriorIRs` `page.tsx:1295-1315`.

### 3f. FG / Rejection budget chips (Article Entry) — `page.tsx:1949-1977`
Two badges comparing generated-box net weight vs the receipt's FG/Rejection caps:
- `aeFgLimit` = Σ `fg_kgs` over items `page.tsx:275`; `aeUsedFg` = Σ net wt of FG boxes `page.tsx:277-279`.
- `aeRejLimit` = Σ `rejection_kgs` `page.tsx:276`; `aeUsedRej` = Σ net wt of REJECTION boxes `page.tsx:280-282`.
- State: EXCEEDS (red) / Matched (green) / "Rem: X kg" (amber) `page.tsx:1960-1961`, `page.tsx:1972-1973`.

### 3g. Items-table footer & loss badges
Per-row Loss% badge with red/amber/green thresholds `page.tsx:1435-1442`; expandable per-row "W+R%" + "Unacctd%" `page.tsx:1453-1460`. Footer cumulative totals `page.tsx:1465-1483` use `totals` + `overallLossPct` (`page.tsx:873`) and `overallUnaccountedLossPct` (`page.tsx:874`).

---

## 4. Tables & columns

### T1. Inward Receipt Records (Material In tab) — `page.tsx:1080-1174`
Source `miRecords` (`GET /job-work/material-in/list`). Columns:
| # | Column | Source field | Line |
|---|--------|-------------|------|
| 1 | Challan No | `rec.challan_no \|\| rec.ir_number` — wrapped in **ChallanHoverCard** | `page.tsx:1098-1143` |
| 2 | JWO Challan (lg only) | `rec.jwo_challan` | `page.tsx:1144` |
| 3 | Date | `rec.receipt_date` | `page.tsx:1145` |
| 4 | Type (lg) | `rec.receipt_type` → Final/Partial badge | `page.tsx:1146-1150` |
| 5 | Party | `rec.to_party` | `page.tsx:1151` |
| 6 | FG (Kg) | `rec.total_fg_kgs` | `page.tsx:1152` |
| 7 | Waste (Kg) | `rec.total_waste_kgs` | `page.tsx:1153` |
| 8 | Rejection (Kg) | `rec.total_rejection_kgs` | `page.tsx:1154` |
| 9 | Actions | View / Delete | `page.tsx:1155-1169` |
Mobile card equivalent `page.tsx:1178-1208`.

### T2. Inward Receipt — Enter Quantities (editable) — `page.tsx:1376-1484`
Source `miItems`. Columns: `#` (`sl_no`), Item Description, Disp.(Kg) `sent_kgs`, **FG Received (Kg)** editable, **{wasteLabel} (Kg)** editable + free-text "material type" (only when `showWasteColumn`), **Rejection (Kg)** editable, Accntd. `total_accounted_kgs`, Unaccntd. `unaccounted_kgs` (shows "OVER" if negative), Loss%. `wasteLabel = miLossConfig.loss_component || "Waste/Byproduct"` `page.tsx:896`. `showWasteColumn = !isThermopacking` `page.tsx:895` (hidden for Thermopacking process). Footer = cumulative totals `page.tsx:1465-1483`.

### T3. Generated Articles (Box IDs) — `page.tsx:1861-1944`
Source `aeGeneratedArticles`. Wrapped in `<BoxScrollContainer>` `page.tsx:1856-1947` (provides box-search/scroll + ref registration via render-prop `registerRef`). Columns: `#`, Transaction No, Box ID, Item Description (+ REJECTION chip), Group (`item_group / sub_group`), **Case Pack** editable, **Net Wt (Kg)** editable, **Gross Wt (Kg)** editable, QR (per-row print), Del. Case-pack↔net-weight are bidirectionally linked through `uom` via `round3` `page.tsx:1892-1919`.

### T4. Job Work Records (Records tab) — `page.tsx:2131-2213`
Source `records` (`GET /job-work/list`). Columns:
| Column | Source / behavior | Line |
|--------|-------------------|------|
| Challan No | `rec.challan_no` wrapped in a **shadcn Tooltip** (NOT ChallanHoverCard) showing Status/From→To/Date/Qty·Wt/Process/Items/Reason | `page.tsx:2147-2175` |
| Status | `getStatusBadge(rec.status)` | `page.tsx:2176` |
| From → To | `getDisplayWarehouseName(rec.from_warehouse) → rec.to_party` | `page.tsx:2177` |
| Item Description | `rec.item_descriptions` (truncated) | `page.tsx:2178` |
| Date (lg) | `rec.job_work_date` | `page.tsx:2179` |
| Total Qty (lg) | `rec.total_qty` | `page.tsx:2180` |
| Total Wt (Kg) | `rec.total_weight.toFixed(2)` | `page.tsx:2181` |
| Actions | Add Stock / Edit / DC Print / Delete | `page.tsx:2182-2208` |
Mobile cards `page.tsx:2217-2259`.

### T5–T8. Reports breakdown tables
- **By Process** `page.tsx:2442-2468`: Process, JWOs, Dispatched(Kg), FG Received(Kg), Volume bar (`fProcess`).
- **By Vendor** `page.tsx:2480-2504`: Vendor, JWOs, Dispatched(Kg), Volume bar (`fVendor`).
- **By Item** `page.tsx:2516-2542`: Item, JWOs, Boxes, Dispatched(Kg), Volume bar (`fItem`).
- **Monthly Trend** `page.tsx:2554-2568`: per-month horizontal bar of `dispatched_kgs` + JWO count (`rptMonthly`).
- **Vendor × Item Matrix (Top 20)** `page.tsx:2583-2609`: Vendor, Item, JWOs, Dispatched(Kg), Volume bar (`fMatrix`).
Bar widths via `barWidth(val,max)` `page.tsx:975`.

### T9. View IR dialog line-items table — `page.tsx:2742-2818`
Columns: #, Item, Sent, FG(Kg), Waste(Kg)(+waste_type sub-line), Reject(Kg), Unacctd(Kg) (`max(0, sent − fg − waste − rejection)` `page.tsx:2767`), Loss% (with min–max band + over-limit red). Footer totals `page.tsx:2799-2817`. Cumulative summary tiles `page.tsx:2822-2862`.

---

## 5. Buttons

| Label | Line(s) | Handler | Action / Redirect |
|-------|---------|---------|-------------------|
| Back (ArrowLeft) | 982 | inline | `router.push(/${company}/transfer)` |
| New Material Out (header) | 990 | inline | `router.push(/${company}/transfer/job-work/material-out)` |
| Find Record | 1036 | `handleSearchMaterialOut` | `GET /job-work/out/search` |
| Refresh (IR records) | 1057 | inline | `loadMiRecords(miRecordsPage)` |
| View (Eye, IR row) | 1157,1192 | `handleViewIR(rec.id)` | opens View IR dialog; `GET /job-work/material-in/{id}` |
| Delete (IR row) | 1163,1198 | `handleDeleteMiRecord` | `DELETE /job-work/material-in/{id}` (canDelete only) |
| Prev / Next (IR records) | 1217,1218 | `loadMiRecords(±1)` | pagination |
| Material-In Warehouse Select | 1253 | `setMiInwardWarehouse` | drives cold-storage detection/routing |
| Loss-row expand `+/−` | 1443 | `setExpandedLossRows` | toggles per-row loss detail |
| Item-desc clear (Trash2) | 1608 | inline | clears AE selected item |
| Entry Type: Finished Goods | 1620 | `setAeBoxType('FG')` | toggle |
| Entry Type: Rejection | 1627 | `setAeBoxType('REJECTION')` | toggle |
| Generate Entries | 1826 | `generateArticleEntries` | builds box rows (client only) |
| Clear All (articles) | 1844 | `setAeGeneratedArticles([])` | clears generated boxes |
| Print QR Labels (bulk) | 1848 | `handlePrintArticleQR()` | iframe print of all boxes |
| Print QR (per row) | 1928 | `handlePrintArticleQR(idx)` | iframe print of one box |
| Del (article row) | 1935 | inline filter | removes one box |
| Add Box | 1981 | inline | appends a box to last txn |
| Clear (submit row) | 2013 | inline | resets Material-In form |
| Submit Partial | 2017 | `handleSubmitMaterialIn(e,'partial')` | `POST /job-work/material-in` |
| Submit Final | 2024 | `handleSubmitMaterialIn(e,'final')` | `POST /job-work/material-in`; disabled unless `canSubmitFinal` |
| Refresh (records) | 2058 | `loadRecords(recordsPage)` | reload |
| Clear (records filter) | 2094 | inline | reset challan/status/date filters |
| New Material Out (empty state) | 2123 | inline | `router.push(.../material-out)` |
| Add Stock (Plus, record row) | 2185,2234 | `handleAddStock(rec.challan_no)` | switches to Material In tab + preloads challan |
| Edit (Pencil, record row) | 2191,2240 | inline | `router.push(.../material-out?edit=${rec.id})` |
| DC Print (record row) | 2196,2245 | inline | `router.push(.../job-work/dc/${rec.challan_no})` |
| Delete (record row) | 2201,2250 | `handleDeleteRecord` | `DELETE /job-work/{id}` (canDelete only) |
| Quick range (Today/This Month/Last Month/All Time) | 2306-2314 | inline date setters | sets `rptFilterFrom/To` |
| Clear All (reports filters) | 2294 | inline | reset all rpt filters |
| Sub-view tabs (Process/Vendor/Item/Trend/Matrix) | 2418 | `setRptActiveView(key)` | switch breakdown view |
| Search clear `×` (reports) | 2430 | `setRptSearch("")` | clear in-results search |

---

## 6. Search / filters

- **Material In — challan search:** `miSearchChallan` input `page.tsx:1030`, Enter or "Find Record" → `handleSearchMaterialOut` `page.tsx:498-566`. Resolves an outward challan into editable line items.
- **Article-entry item search:** debounced (300ms) `aeSearchText` → `GET /job-work/all-sku-search` `page.tsx:230-245`; dropdown of results `page.tsx:1581-1597`; selection via `handleAeSearchSelect` `page.tsx:247-260` (also pulls `uom`). Min length 2 `page.tsx:231`.
- **Records tab filters** `page.tsx:2064-2099`: challan text (`recordsFilterChallan`, Enter → `loadRecords(1)` `page.tsx:2072`), status Select (`all/sent/partially_received/fully_received` `page.tsx:2081-2084`), date input. `useEffect` auto-reloads on status/date change `page.tsx:831`; challan filter requires Enter. All three appended to `GET /job-work/list` `page.tsx:808-810`.
- **Reports filters** `page.tsx:2316-2355`: Process / Vendor / Item Selects + From/To dates; options merge hardcoded `PROCESS_OPTIONS`/`VENDOR_OPTIONS` (`page.tsx:964-965`) with DB `filter_options` (`page.tsx:967-972`). Auto-reload via debounced (100ms) `useEffect` `page.tsx:942-948`.
- **Reports in-results search** (`rptSearch`): client-side filter over the active breakdown only (`fProcess/fVendor/fItem/fMatrix` `page.tsx:959-962`); hidden on "trend" view `page.tsx:2426`.

---

## 7. Pagination

Two independent paginators (both server-driven, Prev/Next style):
- **Records tab** — `recordsPage`/`recordsTotalPages`/`recordsTotal` `page.tsx:797-799`; `per_page=15` `page.tsx:807`; UI `page.tsx:2262-2274` ("Showing X-Y of N", page n/m). Only shown when `recordsTotalPages > 1`.
- **Inward Receipt Records (Material In)** — `miRecordsPage`/`miRecordsTotalPages`/`miRecordsTotal` `page.tsx:440-442`; `per_page=10` `page.tsx:469`; UI `page.tsx:1211-1221`.
- Reports breakdown tables are NOT paginated (server caps matrix at "Top 20"). The `BoxScrollContainer` around generated articles supports pagination props but they are **not passed** here (single-page list), so it acts as a search/scroll helper only.

---

## 8. Page-in-page & hover actions

### ChallanHoverCard — used in T1 (Inward Receipt Records), `page.tsx:1099-1142`
Component: `d:\test\frontend-\components\transfer\ChallanHoverCard.tsx` (`ChallanHoverCard.tsx:31-262`). Hover (mouseEnter) opens a portal card; lazy-fetches once and caches in `fetched` state (`ChallanHoverCard.tsx:88-102`); 180ms close delay with mouse-over cancel (`ChallanHoverCard.tsx:104-110`); positioned via `getBoundingClientRect` to flip above/below viewport (`ChallanHoverCard.tsx:63-86`).

**Props passed on this page** (`page.tsx:1099-1142`):
- `challanNo={rec.challan_no || rec.ir_number || "-"}` — the underlined dotted trigger text.
- `from={rec.jwo_challan}` — left chip.
- `to={rec.to_party}` — right chip (arrow shown only if `to !== from`).
- `fetchLines={async () => …}` — **the only data path used; `lines`/`meta`/`discrepancies` props are NOT passed**, so the card relies entirely on this fetch.

**`fetchLines` target & shaping** (`page.tsx:1103-1141`):
- Endpoint: `GET ${API}/job-work/material-in/${rec.id}` `page.tsx:1105` — i.e. the **single inward-receipt detail** endpoint (same one `handleViewIR` uses).
- Reads `data.receipt` (`recv`), `data.cumulative` (`cum`), `data.lines`.
- Builds `HoverLine[]`: `name` = item description, appending `· W:{w}kg R:{rj}kg` tail when waste/rejection present `page.tsx:1115-1120`; `qty` = `finished_goods_boxes ?? sent_boxes`; `netWeight` = FG kgs `.toFixed(2)`.
- Builds `HoverMeta[]` chips (`page.tsx:1125-1136`): IR number; Type (Final=success/Partial=default); Inward WH; Vehicle; Driver; FG (success); Waste/Rejection (warn, only if >0); Dispatched; Remaining (warn, if >0); **Loss %** with `tone: warn` when `cum_loss_pct > (recv.max_loss_pct ?? 10)`.
- No `discrepancies` are produced here (the card supports them generally, but this usage does not pass any). Grouping helpers `groupLinesByItem`/`groupBoxesByItem` (`ChallanHoverCard.tsx:270-382`) are **exported but not used by this page** — line shaping here is inline/manual.

> Note: the Records-tab challan uses a plain shadcn `Tooltip` (`page.tsx:2148-2174`), **not** ChallanHoverCard.

### Article Entry "page-in-page" — `page.tsx:1554-2008`
A full secondary workflow embedded in the Material-In tab (shown only when a JWO is found). Item search dropdown `page.tsx:1581-1597`, cascading Group/Sub/Description Selects `page.tsx:1643-1701`, cold-storage detail sub-panel `page.tsx:1745-1822`, and the QR print engine (`handlePrintArticleQR`, hidden-iframe + 4"×2" label HTML, `page.tsx:341-435`).

### Modal: View Inward Receipt dialog — `page.tsx:2679-2870`
`Dialog` controlled by `viewIROpen`/`viewIRData`/`viewIRLoading` (`page.tsx:445-447`). Opened by `handleViewIR(irId)` `page.tsx:449-464` → `GET /job-work/material-in/{irId}`. Renders receipt header, line-items table, and cumulative summary.

### Confirm dialogs
Native `window.confirm` used in `handleDeleteMiRecord` `page.tsx:485` and `handleDeleteRecord` `page.tsx:835`.

---

## 9. Keyboard / click directions

- **Enter** in Material-In challan input → `handleSearchMaterialOut` (`onKeyDown`, `page.tsx:1032`).
- **Enter** in Records challan filter → `loadRecords(1)` (`page.tsx:2072`).
- **onWheel → blur** on every numeric `Input` to prevent scroll-wheel value changes (e.g. `page.tsx:1402,1410,1422,1710,1719,1732,1739,1903,1917,1924`).
- Article-search dropdown selection uses **`onMouseDown` + `preventDefault`** (`page.tsx:1585`) so the click registers before the input's `onBlur` (200ms delayed close, `page.tsx:1575`) hides the list.
- ChallanHoverCard opens on **mouseEnter**, closes 180ms after **mouseLeave** unless re-entered (`ChallanHoverCard.tsx:121-122,129-130`).
- Hover trigger has `cursor-default` (not a link) — it is informational, not clickable (`ChallanHoverCard.tsx:123`).

---

## 10. Functionality & logic flows (data loading, caching)

**Mount/effect loads:**
- `loadItemGroups()` on mount/`company` change → `GET /job-work/all-sku-dropdown` (item categories) `page.tsx:180-193`.
- `loadRecords(1)` on `recordsFilterStatus`/`recordsFilterDate` change `page.tsx:831`.
- `loadMiRecords(1)` when `activeTab === "create-in"` `page.tsx:832`.
- Reports auto-load (debounced 100ms) when on reports tab or any rpt filter changes `page.tsx:942-948`.
- Debounced all-sku search effect `page.tsx:230-245`.

**Material-In flow:** search challan → `handleSearchMaterialOut` populates `miFoundRecord`, `miInwardWarehouse`, `miLossConfig`, `miReceiveCount`, `miPriorIRs`, and maps line items into `miItems` (sent kgs = `net_weight || quantity_kgs`, prev FG/waste/rejection, unaccounted) `page.tsx:516-560`. Editing a cell → `updateIRItem` recomputes accounted/unaccounted/loss `page.tsx:635-648`. Submit → `validateMaterialIn` `page.tsx:655-669` then `handleSubmitMaterialIn` builds payload (items + generated boxes) and POSTs `page.tsx:689-790`; on success resets the whole form and reloads both record lists `page.tsx:768-784`.

**Loss/tolerance logic:** `getLossToleranceStatus` classifies normal/underweight/excess vs `miLossConfig` min/max `page.tsx:672-686`. `canSubmitFinal = isFullyAccounted || isWithinFinalTolerance` where final tolerance is ±0.2% unaccounted `page.tsx:875-876`.

**Article entry logic:** `generateArticleEntries` validates against FG/Rejection caps, builds a `TR-YYYYMMDDHHMMSS` txn no + `{last8ofDate.now}-{i}` box IDs `page.tsx:285-338`. `case_pack ↔ net_weight` linked via `uom` `page.tsx:262-271,1892-1919`. QR labels printed through a hidden iframe `page.tsx:341-435`.

**Caching:** Minimal/none beyond React state — the only memo-like behavior is ChallanHoverCard's `fetched`/`fetchedMeta` (fetch-once-per-mount per row, `ChallanHoverCard.tsx:88-102`). All list/report loads refetch each call; no SWR/React-Query.

---

## 11. Redirects (all `router.push`)

| Trigger | Target | Line |
|---------|--------|------|
| Back button | `/${company}/transfer` | 982 |
| New Material Out (header & empty state) | `/${company}/transfer/job-work/material-out` | 990, 2123 |
| Edit record | `/${company}/transfer/job-work/material-out?edit=${rec.id}` | 2192, 2241 |
| DC Print | `/${company}/transfer/job-work/dc/${rec.challan_no}` | 2197, 2246 |

**In-page (no route change):** "Add Stock" → `handleAddStock` sets `activeTab="create-in"` and preloads the challan via `/job-work/out/search` `page.tsx:569-632` (not a redirect — tab switch + setTimeout fetch).

---

## 12. API calls

Base URL: `process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'`. All endpoints are under the **`/job-work`** prefix (FastAPI job-work router; this is a standalone module under `transfer`, not `/interunit`). All raw `fetch` (no axios wrapper).

| Verb | Endpoint | Params / Body | Purpose | Line |
|------|----------|---------------|---------|------|
| GET | `/job-work/all-sku-dropdown` | — | Item-category options (mount) | 184 |
| GET | `/job-work/all-sku-dropdown` | `?item_category=` | Sub-category options | 204 |
| GET | `/job-work/all-sku-dropdown` | `?item_category=&sub_category=` | Item-description options | 220 |
| GET | `/job-work/all-sku-search` | `?search=&limit=100` | Debounced item search | 235 |
| GET | `/job-work/sku-detail` | `?description=` | Fetch `uom` for a description (Select path) | 1680 |
| GET | `/job-work/out/search` | `?challan_no=` | Resolve outward challan → record + line_items + loss_config + prior_irs | 510, 584 |
| GET | `/job-work/material-in/list` | `?page=&per_page=10` | Inward-receipt list (T1) | 469 |
| GET | `/job-work/material-in/{id}` | path id | IR detail — used by View dialog **and** ChallanHoverCard `fetchLines` | 453, 1105 |
| POST | `/job-work/material-in` | `?created_by=` + JSON payload (items + boxes) | Record inward receipt | 756 |
| DELETE | `/job-work/material-in/{id}` | `?user_email=` | Delete IR (canDelete) | 487 |
| GET | `/job-work/list` | `?page=&per_page=15&challan_no=&status=&date=` | Job Work records (T4) | 811 |
| DELETE | `/job-work/{id}` | `?user_email=` | Delete JWO record (canDelete) | 837 |
| GET | `/job-work/reports/dashboard` | `?sub_category=&vendor=&item=&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY` | Reports/KPI dashboard data | 927 |

POST payload shape (`page.tsx:700-753`): `challan_no`, `original_challan_no`, `original_record_id`, `receipt_type`, `received_date`, `vehicle_no`, `driver_name`, `remarks`, `inward_warehouse`, `cold_company`/`cold_inward_date` (only when cold), optional `loss_config`, `items[]` (sl_no, description, sent_kgs/boxes, finished_goods_kgs, waste_kgs, waste_type/desc, rejection_kgs …), `boxes[]` (transaction_no, box_id, box_number, item/group/sub_group, net/gross weight, cold fields, `box_type`, `unit_pack_size`).

---

## 13. Backend & DB wiring touched

This is a frontend doc; backend specifics are inferred from the contract above (no backend files read):
- A FastAPI **`/job-work`** router backs every call. Response envelopes consumed: `{ options:{ item_categories|sub_categories|item_descriptions } }`, `{ items:[…] }`, `{ uom }`, `{ record, line_items, loss_config, receive_count, prior_irs }`, `{ records, total, total_pages }`, `{ receipt, lines, cumulative }`, `{ summary, status_counts, by_process, by_vendor, by_item, monthly_trend, vendor_item_matrix, filter_options }`.
- **SKU master:** `all_sku` table feeds dropdowns/search and `uom` (per-unit kg) — referenced in comments `page.tsx:46-48, 184, 254-256`.
- **Loss config:** server returns per-process `min_loss_pct/max_loss_pct/loss_component/waste_with_partial/single_shot` (interface `page.tsx:80-86`); echoed back in POST `page.tsx:712-719`.
- **Status lifecycle (DB `status` values):** `sent`, `partially_received`, `fully_received`, `reconciled`, `closed`, `cancelled` — mapped to badges `page.tsx:847-858`.
- **Cold-storage stocks:** `cold_company` (`cfpl`/`cdpl`) + cold fields written when an inward warehouse is a cold one; ChallanHoverCard's grouping helpers reference `cold_stocks`/`lot_origin_unit` (`ChallanHoverCard.tsx:298-301, 351-357`) — relevant to the broader transfer DB though those helpers are unused on this page.
- **Audit:** `created_by` (POST) and `user_email` (DELETE) passed for attribution/authorization.

---

## 14. Cross-module linkages

- **Material Out child route** `/[company]/transfer/job-work/material-out` (create + `?edit=` + the dispatch this page reconciles against). This page never creates outward dispatches itself.
- **DC print route** `/[company]/transfer/job-work/dc/{challan_no}`.
- **Transfer dashboard** `/[company]/transfer` (back nav).
- **Inward module:** reuses `BoxScrollContainer` from `components/modules/inward/BoxScrollContainer.tsx` (`page.tsx:26`) and prints **QR labels in the exact inward format** (4"×2", QR-left/info-right) `page.tsx:340-435`.
- **Shared transfer component:** `ChallanHoverCard` (`components/transfer/ChallanHoverCard.tsx`) — shared across transfer pages.
- **Warehouse constants:** `lib/constants/warehouses.ts` → `getDisplayWarehouseName`, `isColdWarehouse` (`page.tsx:11`); cold warehouses are Savla D-39, Savla D-514, Rishi, Supreme (`warehouses.ts:40-59`).
- **Auth/Toast:** `useAuthStore` (`lib/stores/auth`) and `use-toast` hook.

---

## 15. Gotchas

1. **Two different challan UIs.** The Records tab uses a plain shadcn `Tooltip` (`page.tsx:2148`), while the Material-In IR list uses **ChallanHoverCard** with a lazy `fetchLines` (`page.tsx:1099`). Easy to confuse "the hover card" between tabs.
2. **ChallanHoverCard fetch hits the IR-detail endpoint, not a `/lines` endpoint.** `fetchLines` calls `/job-work/material-in/{rec.id}` (`page.tsx:1105`) — the *same* endpoint the View dialog uses — and reshapes its `lines/receipt/cumulative` client-side. The exported `groupLinesByItem`/`groupBoxesByItem` helpers are **not** used here.
3. **`fetchLines` reads `rec.*` for some meta but `recv.*` for others.** FG/Waste/Rejection meta come from the *list row* `rec` (`page.tsx:1131-1133`) while IR/Type/WH/Vehicle come from the fetched `recv` (`page.tsx:1126-1130`); if list vs detail diverge they can disagree. It also assumes `rec.total_fg_kgs` etc. are numbers (`.toFixed`) — a null would throw inside the fetch's try (silently caught → empty card).
4. **Hover card caches once per mount.** `fetched` is never invalidated (`ChallanHoverCard.tsx:88-102`); editing/deleting a record won't refresh an already-opened card until remount.
5. **Submit Final gating is ±0.2% unaccounted, not "fully accounted".** `canSubmitFinal` allows submit when within ±0.2% even if not perfectly balanced (`page.tsx:875-876`). The Partial button has no such gate.
6. **`sent_kgs` prefers NET weight.** Dispatched qty uses `net_weight || quantity_kgs` (gross is legacy fallback) — duplicated in both `handleSearchMaterialOut` and `handleAddStock` (`page.tsx:533, 602`); keep both in sync.
7. **Thermopacking hides the Waste column** (`showWasteColumn = !isThermopacking`, `page.tsx:894-895`); waste inputs/totals vanish, and `wasteLabel` comes from `loss_component`.
8. **Cold-storage auto-routing can override user intent.** A `useEffect` on `miInwardWarehouse` forces `aeColdCompany` to cfpl/cdpl by name match (`page.tsx:885-890`); the user can re-select but a later warehouse change re-overrides.
9. **Box IDs are time-derived, not server-issued.** `box_id = ${Date.now().slice(-8)}-${i}` and `transaction_no = TR-<timestamp>` are generated client-side (`page.tsx:306-313`); rapid generation in the same second could collide on the base.
10. **Date format juggling.** Receipt/inward dates are stored as `DD-MM-YYYY` in state but `<input type=date>` needs `YYYY-MM-DD`, converted inline both ways (`page.tsx:1344-1354, 1765-1772`). Report date filters send `DD-MM-YYYY` to the API (`page.tsx:920-925`).
11. **`handleAddStock` uses a `setTimeout(…,100)`** to fetch after state settles (`page.tsx:581`) — a race-prone pattern; if the search endpoint is slow the tab is already switched with an empty form.
12. **Delete is email-allowlisted client-side only** (`page.tsx:115-116`); real protection must be enforced by the backend (the `user_email` query param is the only signal sent).
13. **BoxScrollContainer pagination props are unused here** — it's effectively just a search/scroll wrapper for the generated-articles table (`page.tsx:1856-1859`).
14. **Reports search box is hidden on the Trend view** (`page.tsx:2426`) and filters only the active breakdown array, not the KPI cards or summary.
