# Jobwork Summary — `/[company]/transfer/jobwork/dashboard`

| | |
|---|---|
| **File** | `d:\test\frontend-\app\[company]\transfer\jobwork\dashboard\page.tsx` (750 lines) |
| **URL** | `/{company}/transfer/jobwork/dashboard` (e.g. `/candor/transfer/jobwork/dashboard`) |
| **Component** | `JobworkDashboardPage` (default export, `"use client"`) — line 70 |
| **Purpose** | A read-only **summary/reports dashboard** for Job Work Orders (JWOs). Pulls all JWOs from the backend on mount, then does all KPI / grouping / filtering / search **client-side** over the in-memory `jwoRows` array. Lets a user slice JWOs by vendor / item / process / status / loss-status / date, group the result five ways, and drill into per-JWO and per-Inward-Receipt detail. Copy-to-clipboard works; Excel and WhatsApp export are stubbed. |

> Directory note: this is `jobwork/dashboard`, **distinct from** the operational `job-work/` directory (`d:\test\frontend-\app\[company]\transfer\job-work\…`, which holds `page.tsx`, `dc/[challanId]`, `material-out`). This page is the analytics roll-up; `job-work/` is the transactional flow.

---

## 1. Route & params

- **Route segment**: `app/[company]/transfer/jobwork/dashboard/page.tsx` → URL `/[company]/transfer/jobwork/dashboard`.
- **Dynamic param**: `[company]`. Typed via `interface Props { params: { company: Company } }` (line 68). `Company` imported from `@/types/auth` (line 11).
- Destructured at line 71: `const { company } = params`.
- No search/query params are read from the URL; this is a Next.js App Router **page** receiving `params` synchronously as a prop (not via `useParams`/`useSearchParams`).
- `company` is used for: header subtitle uppercase (line 398), the copy-export header (line 348), and is **NOT** passed to the data-fetch (see Gotchas — the fetch is company-agnostic).

---

## 2. Layout & structure

Root: `<div className="space-y-5 p-4 md:p-6">` (line 385). Top-to-bottom:

1. **Header bar** (lines 386–407): back button + `BarChart3` icon + title "Jobwork Summary" + subtitle line; right side has Copy / Excel / WhatsApp buttons.
2. **Filters `Card`** (lines 409–526): filter header w/ active-count badge + "Clear all"; then Search input, Date Range, Vendor chips, Item chips, Process Type chips, and a 2-col grid of JWO Status + Loss Status chips.
3. **KPI Cards grid** (lines 528–554): 6 cards, responsive `grid-cols-2 md:grid-cols-3 lg:grid-cols-6`.
4. **Group-By toggle row** (lines 556–569): 5 pill buttons.
5. **Summary Table** (lines 571–625): either an empty-state `Card` (lines 572–579) or a `Card` wrapping an `overflow-x-auto` `<table>` whose body is a list of `<GroupSection>` rows.
6. **`GroupSection` sub-component** (lines 634–748): renders the grouped summary row + (when expanded) per-JWO rows + (when a JWO is expanded) a nested Inward-Receipt table.

Imported UI primitives: `Card`/`CardContent` (`@/components/ui/card`), `Button` (`@/components/ui/button`), `Badge` (`@/components/ui/badge`), `Input` (`@/components/ui/input`) — lines 5–8. Icons from `lucide-react` (lines 22–25).

---

## 3. Dashboards / KPI cards / charts / chips

### KPI cards (6) — computed in `kpis` useMemo (lines 248–257), rendered lines 530–553

All metrics are computed over `filtered` (the post-filter, post-search JWO list).

| # | Label | Line (render) | Metric / value | Computation (line) | Source |
|---|---|---|---|---|---|
| 1 | **Total JWOs** | 531–532 | `kpis.total` (count) | `filtered.length` (249) | `filtered` |
| 2 | **Dispatched** (Kgs) | 535–536 | `fmtKgs(kpis.dispatched)` | `Σ j.qty_dispatched` (250) | `filtered` |
| 3 | **FG Received** (Kgs) | 539–540 | `fmtKgs(kpis.fg)` | `Σ j.fg_received` (251) | `filtered` |
| 4 | **Avg Loss** (%) | 543–544 | `kpis.avgLoss` | mean of `actual_loss_pct` over rows where `actual_loss_pct > 0`, 1-dp; `0` if none (252–253) | `filtered` |
| 5 | **Open / Pending** | 547–548 | `kpis.openPending` | count where `jwo_status === "Open" \|\| "Partially Received"` (254) | `filtered` |
| 6 | **Excess Loss** | 551–552 | `kpis.excessFlags` | count where `loss_status === "Excess Loss"` (255) | `filtered` |

- **Conditional card styling**: card 5 gets `border-amber-400 bg-amber-50/50` when `openPending > 0` (line 546); card 6 gets `border-red-400 bg-red-50/50` when `excessFlags > 0` (line 550).
- Card icons: `Package`(blue), `TrendingUp`(indigo), `CheckCircle`(green), `TrendingUp`(violet), `Clock`(amber), `AlertTriangle`(red).
- **No charts** — there is no graph/SVG/canvas. "Aggregation" is realized as the grouped summary table (Section 4), not as bar/line/pie charts. (Charts: N/A.)

### Chips (interactive filter pills)

Two chip components are defined inline:

- **`Chip`** (lines 360–375): generic filter pill. Props `{ label, active, available, onClick }`. Three visual states: active (dark `bg-gray-900 text-white`), available (white, hover), unavailable (`bg-gray-50 text-gray-300 cursor-not-allowed`). Disabled when `!available && !active` (line 363). Shows a `×` when active (line 373). Used for Vendor/Item/Process/Status/Loss filters.
- **`StatusChip`** (lines 378–382): non-interactive colored label. Props `{ label, colorMap }`. Looks up `colorMap[label]` else gray fallback. Used in the JWO rows for `loss_status` (via `LOSS_COLORS`) and `jwo_status` (via `STATUS_COLORS`), and in the IR sub-table for `loss_status`. Color maps: `LOSS_COLORS` (lines 34–39), `STATUS_COLORS` (lines 40–46).

### Group-By pills (lines 559–568)

Five toggle pills driven by `GROUP_OPTIONS` (lines 48–54): Vendor / Item / Process / Month / Status (values `vendor`, `item`, `process_type`, `month`, `jwo_status`). Active pill = dark. Clicking sets `groupBy` and resets both expansion sets (line 562).

---

## 4. Tables & columns

### A. Grouped summary table (main) — header lines 584–602, body via `GroupSection` lines 647–663

Column header label adapts to `groupBy` (line 588: Month / Vendor / Item / Process / Status). All numeric `fmtKgs`-formatted (no decimals).

| # | Column | Source field (`JobworkSummaryRow`) | Render line | Notes |
|---|---|---|---|---|
| 0 | expander caret | — | 650 | `ChevronDown`/`ChevronRight` |
| 1 | Group label | `group_label` | 651 | month labels humanized via `monthLabel` |
| 2 | JWOs | `num_jwos` | 652 | |
| 3 | Dispatched | `total_dispatched_kgs` | 653 | |
| 4 | FG Recvd | `total_fg_received_kgs` | 654 | |
| 5 | Waste | `total_waste_received_kgs` | 655 | |
| 6 | Rejection | `total_rejection_kgs` | 656 | |
| 7 | Unaccounted | `unaccounted_balance_kgs` | 657 | amber+bold when `> 0` |
| 8 | Loss % | `avg_loss_pct` | 658 | suffixed `%` |
| 9 | Open | `open_jwos` | 659 | shows `-` when 0 |
| 10 | Overdue | `overdue_jwos` | 660 | red+bold when `> 0`, `-` when 0 |
| 11 | Excess | `excess_loss_flags` | 661 | red+bold when `> 0`, `-` when 0 |
| 12 | TAT | `avg_turnaround_days` | 662 | `-` when 0 |

Aggregation that builds these rows: `grouped` useMemo (lines 260–310) — see Section 10.

### B. Expanded per-JWO rows (nested in each group) — lines 671–701

Rendered when the group is expanded (`isOpen`), sorted by `dispatch_date` descending (line 665). Each row is a `JobworkDetailRow`.

| Column | Source (`JobworkDetailRow`) | Render line | Notes |
|---|---|---|---|
| caret / spinner | — | 677–679 | `Loader2` spin while IR loading |
| JWO id + date | `jwo_id`, `dispatch_date` | 680–683 | id mono-font |
| Vendor / Item · Process | `vendor_name`, `item_name`, `process_type` | 684–687 | two-line |
| Dispatched | `qty_dispatched` | 688 | |
| FG | `fg_received` | 689 | |
| Waste | `waste_received` | 690 | |
| Rejection | `rejection` | 691 | |
| Unaccounted | `unaccounted_balance` | 692 | amber bg when `> 0` |
| Loss % | `actual_loss_pct` | 693 | `-` if `<= 0` |
| Loss + JWO status chips | `loss_status`, `jwo_status` | 694–699 | two `StatusChip`s (`colSpan={3}`) |
| TAT | `turnaround_days` | 700 | `??` → `-` |

Overdue JWO rows highlighted `bg-red-50/60` (line 666/674): condition = status Open/Partially Received AND `> 30` days since dispatch.

### C. Inward-Receipt (IR) sub-table — lines 704–739

Page-in-page table shown when an individual JWO row is expanded **and** `receipts.length > 0`. Each row is an `InwardReceipt`.

| Column | Source (`InwardReceipt`) | Render line |
|---|---|---|
| IR No. | `ir_number` | 722 |
| Date | `ir_date` | 723 |
| Type | `receipt_type` | 724–726 (Badge: `Final`→default, else outline) |
| FG Qty | `fg_qty_received` | 727 |
| Waste | `waste_qty_received` | 728 |
| Rejection | `rejection_qty` | 729 |
| Loss % | `actual_loss_pct` | 730 |
| Status | `loss_status` | 731 (`StatusChip` + `LOSS_COLORS`) |
| Remarks | `remarks` | 732 (truncated `max-w-[180px]`) |

**Important**: IR receipts are never populated with real data — `toggleJWO` always sets `jwoReceipts[id] = []` (line 341). So in practice the "No inward receipts recorded" branch (lines 740–742) always renders, and table C is currently dead/placeholder UI. See Gotchas.

---

## 5. Filters / date ranges / search

State (lines 154–173): `selVendors`, `selItems`, `selProcess`, `selStatus`, `selLoss` (each a `Set<string>`), `dateFrom`, `dateTo` (strings), `groupBy` (`GroupByOption`, default `"vendor"`), `searchQuery` (string).

- **Search** (UI lines 427–444): single text input. Backed by `makeRecordSearch<JobworkDetailRow>` (line 170–173) over fields `searchFields` (lines 166–169): `jwo_id`, `vendor_name`, `item_name`, `process_type`, `jwo_status`, `loss_status`, `dispatch_date`. Behavior (from `recordSearch.ts`): whitespace-split, **AND** semantics across terms, case-insensitive substring; empty query matches all. Clear-`×` button at line 438–442.
- **Date Range** (UI lines 446–459): two `<input type="date">` (`dateFrom`/`dateTo`). Filter compares ISO `dispatch_date` lexically: `j.dispatch_date < dateFrom` and `> dateTo` (lines 189–190). Clear-`×` resets both (line 454).
- **Vendor** (lines 461–472): chips from `[...new Set(jwoRows.map(j => j.vendor_name))].sort()` (line 468). Availability = `availableVendors.includes(v)`.
- **Item / Article** (lines 474–485): chips from distinct `item_name` (line 481).
- **Process Type** (lines 487–498): chips hardcoded to `["Deseeding","Cracking","Slicing","Dicing","Thermopacking","Stuffing"]` (line 494).
- **JWO Status** (lines 502–512): hardcoded `["Open","Partially Received","Fully Received","Reconciled","Closed"]` (line 508).
- **Loss Status** (lines 513–523): hardcoded `["Normal","Excess Loss","Underweight Waste","Pending"]` (line 519).

**Cascading availability**: each filter dimension recomputes which of its values are still reachable given the *other* selected filters (`availableVendors` 197–205, `availableItems` 207–215, `availableProcess` 217–225, `availableStatuses` 227–235, `availableLoss` 237–245). A chip becomes greyed/disabled when it would produce zero rows under the current cross-filters but is not itself selected.

**Application**: `filtered` useMemo (lines 182–194) applies all five Set filters + date range + `searchMatch` with AND logic. Empty Set = "no filter on this dimension".

**Filter count badge**: `filterCount` (line 313) counts active dimensions (each Set with size>0, plus dateFrom, plus dateTo). Rendered as a `Badge` "{n} active" (line 417) with a "Clear all" button (lines 420–424 → `clearAll`, lines 322–325). Date-only inline clear at 453–457.

---

## 6. Buttons

| Label | Line(s) | Handler | Action / Redirect |
|---|---|---|---|
| Back (ghost, `ArrowLeft`) | 389–391 | `() => router.back()` | Browser history back |
| **Copy** (`Copy`) | 403 | `handleCopy` (347–357) | Builds plain-text summary (KPIs + grouped rows) and `navigator.clipboard.writeText`; toast "Copied to clipboard!" |
| **Excel** (`Download`) | 404 | — (`disabled`) | Stub; no handler |
| **WhatsApp** (`Send`) | 405 | — (`disabled`, `title="Coming Soon"`) | Stub; no handler |
| Clear all (filters) | 421–424 | `clearAll` (322–325) | Resets all 5 Sets + dateFrom/dateTo |
| Search clear `×` | 439–441 | `setSearchQuery("")` | Clears search |
| Date clear `×` | 454–456 | `setDateFrom(""); setDateTo("")` | Clears date range |
| Filter `Chip` (×N, all dims) | 360–375 | `toggle(set, val, setter)` (316–320) | Adds/removes a value from that dimension's Set |
| Group-By pills (×5) | 559–568 | inline `setGroupBy(o.value); reset expansions` (562) | Re-groups table |
| "Clear all filters" (empty state) | 577 | `clearAll` | Resets filters |
| Group summary row (whole `<tr>`) | 649 | `onToggle` → `toggleGroupRow` (327–331) | Expand/collapse group |
| JWO detail row (whole `<tr>`) | 673–676 | `onToggleJWO` → `toggleJWO` (333–344) | Expand/collapse JWO; lazy-loads IR (stub) |

---

## 7. Pagination

**None.** The page fetches up to 1000 rows in one call (`/job-work/list?per_page=1000`, line 85) and renders everything client-side. No pager UI, no infinite scroll, no `page`/`per_page` state on the component. The only scroll affordance is the table's `overflow-x-auto` wrapper (line 582). If a company exceeds 1000 JWOs, excess rows are silently dropped (see Gotchas).

---

## 8. Page-in-page & hover actions

**Page-in-page (two nested levels):**
1. **Group → JWO rows**: expanding a summary row (`expandedGroups`) reveals its `_jwos` as nested table rows (lines 665–701), sorted by dispatch date desc.
2. **JWO → IR sub-table**: expanding a JWO row (`expandedJWOs`) reveals a nested IR table (lines 704–738) inside a bordered/shadowed card (`border-l-4 border-blue-200 ml-14`), or a "No inward receipts recorded" line (741) when empty. The IR data is lazy-fetched by `toggleJWO` (currently stubbed to `[]`).

**Hover actions:**
- Group rows: `hover:bg-gray-50/60 cursor-pointer` (line 649).
- JWO rows: `hover:bg-gray-100/60` (line 674).
- IR rows: `hover:bg-blue-50/20` (line 721).
- `Chip` available state: `hover:border-gray-500 hover:bg-gray-50` (line 368).
- Group-By pills: `hover:border-gray-500` (line 564).
- Search/date clear `×`: `hover:text-gray-600` (line 440).
- No tooltips except the WhatsApp button `title="Coming Soon"` (line 405). No right-click/context menus.

---

## 9. Keyboard / click directions

- **No explicit keyboard handlers** (no `onKeyDown`/`onKeyUp`, no shortcuts). Native browser behavior only: `<Input>` typing for search and date, Enter has no special meaning (filtering is reactive on every keystroke).
- **Click directions**: entire group and JWO rows are clickable to toggle expansion (caret is decorative, not a separate hit target). Chips toggle on click. Group-By pills switch on click. The whole document is click-driven; filtering/searching is instantaneous (no submit button).

---

## 10. Functionality & logic flows

### Data loading (lines 79–152)
- On mount (`useEffect`, empty dep array w/ eslint-disable at 151), sets `dataLoading=true`, fetches `${NEXT_PUBLIC_API_URL || "http://localhost:8000"}/job-work/list?per_page=1000` (lines 84–85).
- On non-OK response throws `HTTP {status}` (line 86).
- Maps `data.records` → `JobworkDetailRow[]` (lines 108–138) with these transforms:
  - `dispatched` = `total_net_weight || total_weight || 0` (109).
  - `fg/waste/rejection` from `fg_received_kgs / waste_received_kgs / rejection_kgs` (110–112).
  - `unaccounted` = `unaccounted_kgs` else `max(0, dispatched - fg - waste - rejection)` (113).
  - `lossPct` = `actual_loss_pct` (114).
  - `jwo_status` derived from backend `r.status` string (115–120): `received`/`fully_received`→Fully Received, `partial`/`partially_received`→Partially Received, `closed`→Closed, `reconciled`→Reconciled, else→Open.
  - `jwo_id` = `r.challan_no || "JWO-{id}"` (123); `dispatch_date` = `job_work_date` first 10 chars (124); `vendor_name` = `r.to_party` (126); `item_name` = `r.item_descriptions` (127); `process_type` via `normalizeProcess(r.sub_category)` (127).
  - `normalizeProcess` (90–94): case-insensitive match against the 6 allowed processes, defaulting to `"Cracking"`.
  - `loss_status` via `computeLossStatus` (95–101): `Pending` if dispatched ≤ 0 or fg+waste+rejection==0; `Excess Loss` if lossPct > 10; `Underweight Waste` if waste>0 and waste/dispatched*100 < 2; else `Normal`.
  - `turnaround_days` via `turnaroundDays(job_work_date, last_receipt_date)` (102–107): rounded day diff, `null` if either missing/invalid/negative.
- Sets `jwoRows` (139). Uses `cancelled` flag to avoid setState after unmount (cleanup line 150).
- **Error path** (140–144): toast "Job Work data error" (destructive variant) + `setJwoRows([])`.
- `dataLoading` is set but **never consumed in the render** (no loading spinner / skeleton on the page body). See Gotchas.

### Aggregation (`grouped`, lines 260–310)
- Buckets `filtered` into a `Map<string, JobworkDetailRow[]>` keyed by the active `groupBy` dimension (264–271; month key via `getMonth` = first 7 chars).
- Per bucket sums dispatched/fg/waste/rej; computes `unaccounted = dispatched - fg - waste - rej` (279); `avgLoss` = mean of positive-loss rows, 1-dp (280–281); `open` count (282); `overdue` = open/partial rows with `> 30` days since dispatch (283–287); `excessFlags` (288); `avgTat` = mean of non-null `turnaround_days`, rounded (289–290).
- Pushes `JobworkSummaryRow & { _jwos }` (292–306); month labels humanized via `monthLabel` (293).
- Sorts groups by `total_dispatched_kgs` desc (308).

### Caching / persistence
- **No caching, no localStorage, no React Query / SWR.** All derived values are `useMemo`s recomputed from state. IR receipts memo-cache only within `jwoReceipts` for the session (and are always `[]`).

### Export / snapshot
- **Copy** (`handleCopy`, 347–357): only working export. Produces a fixed-width text snapshot: title with `company.toUpperCase()` + today's ISO date, the 6 KPI lines, then one padded line per grouped row (`padEnd(30)` label, `padStart(3)` count, `padStart(10)` kgs, loss %). Writes to clipboard, toasts success.
- **Excel** and **WhatsApp**: disabled buttons, not wired (`JobworkApiService.exportExcel` exists but is **not imported/used** here).

---

## 11. Redirects

- The only navigation is **`router.back()`** from the header back button (line 389) — returns to the previous history entry, not a fixed route.
- There are **no `router.push`/`router.replace`** calls and **no `<Link>`s**. Group/JWO/IR drill-downs are in-page expansions, not route changes. The page never navigates to a JWO detail or `job-work` operational page.

---

## 12. API calls

| Method | Endpoint | Params | Purpose |
|---|---|---|---|
| `GET` (raw `fetch`) | `{NEXT_PUBLIC_API_URL}/job-work/list` | `per_page=1000` (query) | Sole data source; returns `{ records: [...] }` mapped to `JobworkDetailRow[]` on mount (lines 85–138) |
| (stub) `await new Promise(setTimeout 200ms)` | — | — | `toggleJWO` simulates an IR fetch then sets `jwoReceipts[id]=[]` (lines 340–341). No real IR API is called. |

Notes:
- The fetch uses **no auth headers** and **no `company` param** (unlike `JobworkApiService`, which sets `Authorization` + `company`). This page bypasses `jobworkApiService.ts` entirely.
- `JobworkApiService` (`d:\test\frontend-\lib\jobworkApiService.ts`) defines `/jobwork/dashboard/summary`, `/filter-options`, `/group-details`, `/jwo-receipts/{id}`, `/export-excel` — **none are used by this page**. They appear to be the intended/legacy API surface; the page instead computes everything client-side from `/job-work/list`.

---

## 13. Backend & DB wiring touched

- **Endpoint consumed**: `GET /job-work/list?per_page=1000`. The route definition was **not found** in the indexed backend roots (`D:\Consumption\New\Backend\app`, `c:\Candor\…\Consumption\Backend\app`, `D:\Consumption\Android`); only `vendor/router.py` and `lookups_router.py` reference `job-work` strings, and none expose the `challan_no`/`to_party`/`item_descriptions`/`fg_received_kgs` field set this page maps. So the live `/job-work/list` handler/model is outside the accessible trees (or served by a deployment not present locally).
- **Expected response fields** the page reads from each record (lines 108–136): `id`, `challan_no`, `job_work_date`, `to_party`, `item_descriptions`, `sub_category`, `total_net_weight`/`total_weight`, `fg_received_kgs`, `waste_received_kgs`, `rejection_kgs`, `unaccounted_kgs`, `actual_loss_pct`, `status`, `last_receipt_date`. These imply a JWO/challan table with per-JWO receipt aggregates already computed server-side (the comment at lines 27–32 calls them "real per-JWO aggregates").
- **No writes** — this is a read-only dashboard; no POST/PUT/DELETE, no DB mutation.

---

## 14. Cross-module linkages

- **Shared smart-search**: `makeRecordSearch` from `@/lib/search/recordSearch` (line 9) is the same predicate used by the transfer, inward, cold-storage, and RTV dashboards (per its doc comment) — consistent multi-term AND search across modules.
- **Shared types**: `@/types/jobwork` (`JobworkDetailRow`, `JobworkSummaryRow`, `InwardReceipt`, `GroupByOption`, `ProcessType`, `JWOStatus`, `LossStatus`) and `Company` from `@/types/auth`.
- **Sibling Jobwork operational pages** (not navigated to from here, but same domain): `app/[company]/transfer/job-work/page.tsx`, `…/job-work/dc/[challanId]/page.tsx`, `…/job-work/material-out/page.tsx`. This dashboard reports on data those flows create (challans → JWOs). Linked from `components/layout/sidebar.tsx`.
- **Unused service layer**: `lib/jobworkApiService.ts` (the `/jobwork/dashboard/*` API) is the cross-module-consistent service the page would normally use but does not.
- **Inward Receipts** concept overlaps with the Inward module (the nested IR table mirrors inward receipt records), tying Jobwork to the Inward aggregate-sync work noted in project memory.

---

## 15. Gotchas

1. **`dataLoading` is dead UI state**: set true/false (lines 77, 82, 146) but never read in JSX — there is **no loading spinner/skeleton** for the initial fetch. On a slow load the page shows zeroed KPIs and an empty table, which can read as "no data" rather than "loading".
2. **IR sub-table is permanently empty**: `toggleJWO` hardcodes `jwoReceipts[id] = []` after a fake 200ms delay (lines 340–341). The real IR fetch (`JobworkApiService.getJWOReceipts`) is not wired, so the per-JWO IR drill-down always shows "No inward receipts recorded" (line 741). Table C (lines 704–738) is currently unreachable.
3. **No company scoping on fetch**: `/job-work/list` is called without `company` or auth headers (line 85), unlike `jobworkApiService.ts`. If the backend doesn't infer company from session, the dashboard could show cross-company data; `company` is only used cosmetically (header/copy).
4. **Hard 1000-row cap, no pagination**: `per_page=1000` (line 85). Beyond 1000 JWOs, rows are silently truncated and all KPIs/aggregations under-count.
5. **Excel & WhatsApp buttons are stubs** (lines 404–405) — `disabled`, no handlers, despite an existing `exportExcel` service method.
6. **Avg Loss excludes zero/negative-loss JWOs** (lines 252–253, 280–281): the mean only averages rows where `actual_loss_pct > 0`, so the KPI is "average loss among JWOs that had loss," not across all dispatched JWOs — easy to misread.
7. **Date filter is lexical string compare** (lines 189–190): relies on `dispatch_date` being a zero-padded `YYYY-MM-DD` (guaranteed by the `.substring(0,10)` at line 124). Any non-ISO date would mis-sort/mis-filter.
8. **In-place sort mutation** (line 665): `jwos.sort(...)` sorts the `_jwos` array in place during render; harmless here (stable-ish, idempotent) but technically mutates memoized data on each expand-render.
9. **Process default fallback to "Cracking"** (`normalizeProcess`, line 93): any unrecognized `sub_category` silently becomes "Cracking", which can mis-bucket JWOs under the Process group-by and Process filter.
10. **`overdue` uses local `Date.now()` vs string date** (lines 283–287, 666): `new Date(dispatch_date)` parses the date at UTC midnight; the >30-day threshold is timezone-sensitive at the boundary.
11. **Legacy mock array referenced in comments only** (lines 27–32): the comment mentions a dev-fallback mock array, but no such array exists in the file — the catch path just sets `[]`. The comment is stale.
12. **Directory confusion risk**: `jobwork/` (this analytics page) vs `job-work/` (operational). Easy to edit the wrong file.
