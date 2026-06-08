# Delivery Challan (DC) Print Page — `/[company]/transfer/dc/[transferId]`

| | |
|---|---|
| **File** | `d:\test\frontend-\app\[company]\transfer\dc\[transferId]\page.tsx` (80 lines) |
| **Primary child component** | `d:\test\frontend-\components\transfer\DeliveryChallan.tsx` (600 lines) |
| **URL pattern** | `/{company}/transfer/dc/{transferId}` (Next.js App Router dynamic segment) |
| **Purpose** | A **print-only / document** page. It loads a single inter-unit stock transfer by id, then renders a printable A4 **Delivery Challan** plus a tear-off **Gate Pass** and automatically triggers the browser print dialog. Nothing is editable here; the page exists solely to produce a paper document for the vehicle/driver moving stock between Candor warehouses. |

This document covers ONLY this page and its directly-imported render component (`DeliveryChallan`) plus the warehouse-address constant it consumes.

---

## 1. Route & params

- Next.js App Router page; the route is defined purely by directory nesting: `app/[company]/transfer/dc/[transferId]/page.tsx`.
- Component signature `DCPage({ params })` with a typed prop interface (`page.tsx:10-17`):
  - `params.company: Company` — company segment (e.g. `candor`). `Company` is imported from `@/types/auth` (`page.tsx:8`), which re-exports it from `@/lib/api` (`types/auth.ts:2-4`).
  - `params.transferId: string` — the transfer header id (matches the `t.id` used by the dashboard's DC button).
- Both are destructured immediately: `const { company, transferId } = params` (`page.tsx:18`).
- This is a **server-segment route rendered as a client component** — the file opens with `"use client"` (`page.tsx:1`), so `params` are received directly as props (no `useParams()` / `useSearchParams()`). No query-string params are read.

---

## 2. Printable layout & structure

The page itself (`page.tsx`) renders one of three states and otherwise delegates the whole document to `<DeliveryChallan>`:

**Page-level states (`page.tsx`):**
1. **Loading** (`page.tsx:43-52`) — full-screen centered spinner (`Loader2`, `animate-spin`, blue) with text `Loading DC...`.
2. **Error / not found** (`page.tsx:54-60`) — full-screen centered red text `Error: {error || 'Transfer not found'}`. Triggered when the fetch throws OR `transferData` is falsy.
3. **Success** (`page.tsx:62-78`) — renders `<DeliveryChallan ... />` with all props mapped from `transferData` (see §4).

**Document structure inside `DeliveryChallan.tsx` (top → bottom):**

The whole document is wrapped in `<div className="w-full bg-white dc-print-content" style={{ padding: '0.5cm 1.25cm' }}>` (`DeliveryChallan.tsx:218`). The `dc-print-content` class is the print anchor (see §6).

**A. Delivery Challan pages (paginated, 10 consolidated items per page)** — `DeliveryChallan.tsx:220-362`
Items are consolidated then chunked into pages of 10 (`itemsPerPage = 10`, `DeliveryChallan.tsx:130-134`). Each page is a `<div className="dc-page">` with `pageBreakAfter: 'always'` except the last (`DeliveryChallan.tsx:221`). Each page renders one `<table>` (`tableLayout: 'auto'`, `DeliveryChallan.tsx:222-232`) whose `<thead>` is produced by `renderDCHeader(pageNum, isLastPage)` (`DeliveryChallan.tsx:137-215`):

  - **A1. Title / logo band** (`DeliveryChallan.tsx:139-158`) — centered, bottom border `2px solid #000`. Contains `/candor-logo.jpg` (`height 60px`), the brand text **CANDOR FOODS** (20px, color `#8B4049`), subtitle **DELIVERY CHALLAN** (14px), and on pages > 1 a `Page {pageNum}` line.
  - **A2. Transfer No / Date row** (`DeliveryChallan.tsx:159-166`) — two cells: `Transfer No: {dcNumber}` and `Date: {requestDate}`.
  - **A3. FROM / TO parties row** (`DeliveryChallan.tsx:167-182`) — two cells, both prefixed `FROM: Candor Foods` / `TO: Candor Foods`, then warehouse **name** (bold) and **address** (gray) resolved from `warehouseAddresses[fromWarehouse]` / `[toWarehouse]`, falling back to the raw code.
  - **A4. Vehicle No / Driver Name row** (`DeliveryChallan.tsx:183-190`).
  - **A5. Total Count (PM) banner** (`DeliveryChallan.tsx:191-200`) — *conditional*, only when `showCountColumn` is true. Highlighted band (`#fdf8f4`, color `#8B4049`) showing `Total Count (PM): {totalPMCount}`.
  - **A6. Column header row** (`DeliveryChallan.tsx:201-213`) — gray `#e0e0e0`; columns listed in §3.
  - **Body rows** (`DeliveryChallan.tsx:264-301`) — one row per consolidated item.
  - **Last-page-only summary** (`DeliveryChallan.tsx:303-358`):
    - **Totals row** (`DeliveryChallan.tsx:307-332`) — beige `#f0ebe3`.
    - **Reason row** (`DeliveryChallan.tsx:334-338`) — `Reason: {reasonDescription}` spanning all columns.
    - **Auth Sign row** (`DeliveryChallan.tsx:339-343`) — `Auth Sign : _________________________`.
    - **Footer disclaimer** (`DeliveryChallan.tsx:345-356`) — italic, `This is a computer-generated delivery challan. No signature required.`

**B. "CUT HERE" separator** — `DeliveryChallan.tsx:364-382`
A `2px dashed #999` horizontal rule with an absolutely-positioned centered label `✂ CUT HERE`. `pageBreakBefore: 'avoid'`.

**C. Gate Pass section (compact)** — `DeliveryChallan.tsx:384-548`
A second `<table>` (`tableLayout: 'fixed'`, `pageBreakInside: 'avoid'`, `DeliveryChallan.tsx:385-393`). Column count is **6 if `hasPMItems`, else 5** (`<colgroup>` at `DeliveryChallan.tsx:394-401`).
  - **C1. Gate Pass title band** (`DeliveryChallan.tsx:402-422`) — gray `#f0f0f0`, logo (`50px`) + `CANDOR FOODS - GATE PASS` (18px, `#8B4049`).
  - **C2. Info row 1** (`DeliveryChallan.tsx:426-439`) — `Transfer No`, `Date`, `Vehicle`, `Driver`.
  - **C3. Info row 2** (`DeliveryChallan.tsx:441-448`) — `From` / `To` (warehouse name only, no address).
  - **C4. ITEMS SUMMARY banner** (`DeliveryChallan.tsx:451-455`).
  - **C5. Gate-pass column header** (`DeliveryChallan.tsx:456-463`) — `S.No`, `Item Description`, `Boxes`, `Qty`, `Net Wt (Kg)`, + `Count` when `hasPMItems`.
  - **C6. Gate-pass item rows** (`DeliveryChallan.tsx:464-493`) — iterates `consolidatedItems`.
  - **C7. Gate-pass totals row** (`DeliveryChallan.tsx:496-519`) — `Total Items`, `Total Qty`, `Total Boxes`, `Total Kg`, a **status chip** (`COMPLETE` green / `PARTIAL` red based on `boxesPending > 0`, `DeliveryChallan.tsx:503-510`), + `Total Count` when `hasPMItems`.
  - **C8. Signatures row** (`DeliveryChallan.tsx:521-533`) — `Security Sign` and `Driver Sign` underlined blocks.
  - **C9. Gate-pass footer** (`DeliveryChallan.tsx:535-546`) — `Present this gate pass at security gate • Authorized by: {approvalAuthority}`.

**D. Print stylesheet** — `<style jsx global>` (`DeliveryChallan.tsx:550-597`), see §6.

---

## 3. Tables & columns

### Delivery Challan line-items table (`DeliveryChallan.tsx:222-360`)

Column count `DC_COLS = showCountColumn ? 9 : 8` (`DeliveryChallan.tsx:93`). Header row at `DeliveryChallan.tsx:201-213`:

| # | Column | Source field (per row) | Render | Line |
|---|--------|------------------------|--------|------|
| 1 | **S.No** | running index | `globalIndex + 1` | `DeliveryChallan.tsx:270` |
| 2 | **Item Description** | `item.item_desc_raw || item.item_description` | text, `wordBreak: break-word` (only wrapping column) | `DeliveryChallan.tsx:271-273` |
| 3 | **Category** | `item.item_category` | text, centered | `DeliveryChallan.tsx:274-276` |
| 4 | **No. of Boxes** | `item.box_count` (set during consolidation) | `toLocaleString('en-IN')`, bold | `DeliveryChallan.tsx:277-279` |
| 5 | **Qty** | `item.qty || item.quantity` | `toLocaleString('en-IN')`, bold | `DeliveryChallan.tsx:280-282` |
| 6 | **UOM** | `item.uom` | text | `DeliveryChallan.tsx:283-285` |
| 7 | **Pack Size (kg)** | `item.pack_size` (hidden if `'0'`) | 3-decimal locale string | `DeliveryChallan.tsx:286-288` |
| 8 | **Net Wt (kg)** | `item.net_weight` | 3-decimal locale string, right-aligned | `DeliveryChallan.tsx:289-291` |
| 9 | **Count** *(conditional)* | `itemCountFor(item)` = `unit_pack_size × qty` for PM/packaging | locale string or `—`; shown only when `showCountColumn` | `DeliveryChallan.tsx:292-298` |

**Totals row** (`DeliveryChallan.tsx:307-332`), values aligned under their headers:
- Label cell (`colSpan=3`): `TOTAL ({consolidatedItems.length} item(s)):`
- Under **No. of Boxes**: `items.length` (raw box count, **not** consolidated) — `DeliveryChallan.tsx:311-313`.
- Under **Qty**: `totalQtyRequired` prop — `DeliveryChallan.tsx:314-316`.
- Under **Net Wt**: sum of `consolidatedItems[].net_weight` (3-dec) — `DeliveryChallan.tsx:319-321`.
- Under **Count** (conditional): `totalPMCount` or `—` — `DeliveryChallan.tsx:322-331`.

### Gate Pass items table (`DeliveryChallan.tsx:456-519`)

| # | Column | Source | Line |
|---|--------|--------|------|
| 1 | S.No | index+1 | `DeliveryChallan.tsx:471` |
| 2 | Item Description | `item.item_desc_raw || item.item_description` | `DeliveryChallan.tsx:472-474` |
| 3 | Boxes | `item.box_count` | `DeliveryChallan.tsx:475-477` |
| 4 | Qty | `item.qty || item.quantity` | `DeliveryChallan.tsx:478-480` |
| 5 | Net Wt (Kg) | `item.net_weight` (2-dec) | `DeliveryChallan.tsx:481-483` |
| 6 | Count *(if `hasPMItems`)* | PM-only `unit_pack_size × qty` | `DeliveryChallan.tsx:484-490` |

Gate-pass totals row (`DeliveryChallan.tsx:496-519`): `Total Items`, `Total Qty` (= `totalQtyRequired`), `Total Boxes` (= `items.length`), `Total Kg`, status chip, `Total Count`.

### Item consolidation logic (`DeliveryChallan.tsx:95-127`)

Both tables render `consolidatedItems`, a `useMemo` keyed on `items`. Grouping key = `${item_description}__${item_category}__${pack_size}` (uppercased/trimmed). For collisions it **sums `qty`**, **sums `net_weight`**, and **increments `box_count`** (each raw `items[]` entry counts as one box). So "No. of Boxes" per row = number of raw lines that consolidated together; total box count = `items.length`.

---

## 4. Data shown & sources

Single source: the response of `InterunitApiService.getTransferById(company, transferId)` stored in `transferData` (`page.tsx:28-29`). The page maps that object to `DeliveryChallan` props with defensive `||` fallbacks (`page.tsx:62-77`):

| DC prop | Source field(s) in `transferData` | Fallback | Line |
|---------|-----------------------------------|----------|------|
| `dcNumber` | `challan_no` → `request_no` | `'N/A'` | `page.tsx:64` |
| `requestDate` | `transfer_date` → `stock_trf_date` | — | `page.tsx:65` |
| `fromWarehouse` | `from_warehouse` → `from_site` | — | `page.tsx:66` |
| `toWarehouse` | `to_warehouse` → `to_site` | — | `page.tsx:67` |
| `vehicleNumber` | `vehicle_number` → `vehicle_no` | `'N/A'` | `page.tsx:68` |
| `driverName` | `driver_name` | `'N/A'` | `page.tsx:69` |
| `approvalAuthority` | `approval_authority` → `approved_by` | `'N/A'` | `page.tsx:70` |
| `reasonDescription` | `reason_code` → `remark` | `'N/A'` | `page.tsx:71` |
| `items` | `lines` → `items` | `[]` | `page.tsx:72` |
| `totalQtyRequired` | `total_qty_required`, else sum of `lines[].quantity\|qty` | computed | `page.tsx:73` |
| `boxesProvided` | `(transferData.boxes || []).length` | `0` | `page.tsx:74` |
| `boxesPending` | hard-coded `0` | — | `page.tsx:75` |
| `warehouseAddresses` | `WAREHOUSE_ADDRESSES` constant | — | `page.tsx:76` |

**Company / address details:** Not from the API. The brand block ("CANDOR FOODS", logo `/candor-logo.jpg`) is hard-coded in `DeliveryChallan.tsx`. Warehouse **name + address** come from `WAREHOUSE_ADDRESSES` in `d:\test\frontend-\lib\constants\warehouses.ts` (`warehouses.ts:222-224`), which is derived from the `WAREHOUSES` master map (`warehouses.ts:14-60`) — e.g. `W202`, `A185`, `A101`, `A68`, `F53`, `Savla D-39`, `Savla D-514`, `Rishi`, `Supreme`. Lookup is by exact code (`warehouseAddresses[fromWarehouse]`); unknown codes fall through to the raw string.

**Per-line item fields consumed** (`items[]`/`lines[]`): `item_description`, `item_desc_raw`, `item_category`, `pack_size`, `qty`/`quantity`, `uom`, `net_weight`, `material_type`/`rm_pm_fg_type`, `unit_pack_size`. **Lots are not displayed** on this document — there is no lot/batch column; box-level/lot data (`transferData.boxes`) is only used for the `boxesProvided` count.

**PM / count detection:** `isCountableItem` (`DeliveryChallan.tsx:60-64`) returns true when `material_type`/`rm_pm_fg_type` is `PM` or `item_category` is `PACKAGING`. `showCountColumn` is also forced true when the source warehouse matches A-68 via regex on the code or its display name (`DeliveryChallan.tsx:70-73`).

---

## 5. Buttons

This is a print/document page with **no interactive buttons** — there is no Print, Back, or Download button rendered. Printing is automatic (see §6).

| Label | Line | Handler | Action |
|-------|------|---------|--------|
| *(none)* | — | — | The page has no `<button>`/`<Button>` elements. Print is auto-triggered; the user uses the browser's native print dialog (and its Save-as-PDF for "download"). To leave the page the user relies on the browser Back button. |

---

## 6. Print mechanics

- **Auto-print on load: YES.** `DeliveryChallan` runs a `useEffect` on mount (`DeliveryChallan.tsx:37-57`) that, after logging all props to the console (`DeliveryChallan.tsx:38-50`), schedules `window.print()` via `setTimeout(..., 500)` (`DeliveryChallan.tsx:52-54`). The 500 ms delay lets the layout/logo settle before the dialog opens. The timer is cleared on unmount (`DeliveryChallan.tsx:56`). Dependency array is `[]`, so it fires exactly once.
- **No `react-to-print`** library is used — it is the native `window.print()`.
- **CSS `@media print`** (`DeliveryChallan.tsx:550-590`):
  - `@page { size: A4; margin: 0; }` (`DeliveryChallan.tsx:552-555`).
  - Hide-all-then-reveal pattern: `body * { visibility: hidden }` then `.dc-print-content, .dc-print-content * { visibility: visible }` (`DeliveryChallan.tsx:557-565`) — so only the challan/gate-pass content prints, not any surrounding chrome.
  - `.dc-print-content` is absolutely positioned at top-left, `width:100%`, `height:100vh`, `display:flex; flex-direction:column` (`DeliveryChallan.tsx:569-577`).
  - `print-color-adjust: exact` / `-webkit-print-color-adjust: exact` forced on `body` and `*` so the maroon brand color and shaded bands print (`DeliveryChallan.tsx:579-589`).
  - `@media screen { body { background: #f5f5f5 } }` (`DeliveryChallan.tsx:592-596`) for on-screen preview.
- **Pagination for print:** the items table is split into `dc-page` chunks of 10 with `pageBreakAfter: 'always'` between pages (`DeliveryChallan.tsx:130-134`, `221`); the Gate Pass uses `pageBreakInside: 'avoid'` (`DeliveryChallan.tsx:391`); the cut line uses `pageBreakBefore: 'avoid'` (`DeliveryChallan.tsx:369`).

---

## 7. Keyboard / click directions

- No custom keyboard handlers, focus traps, or click handlers are defined on this page or in `DeliveryChallan`.
- All interaction is delegated to the **browser's native print dialog** (auto-opened), which carries its own keyboard handling (Enter to print, Esc to cancel, Ctrl/Cmd+P to re-open).
- N/A otherwise.

---

## 8. Redirects

- **No programmatic redirects.** The page never calls `router.push`/`router.replace`/`redirect()`. On error it stays put and shows inline red error text (`page.tsx:54-60`); on missing `transferId` the fetch effect simply does not run (`page.tsx:38-40`). Leaving the page is entirely user-driven (browser Back).

---

## 9. API calls

| Method/Verb | Endpoint | Params | Purpose | Line |
|-------------|----------|--------|---------|------|
| `InterunitApiService.getTransferById(company, transferId)` | `GET {NEXT_PUBLIC_API_URL}/interunit/transfers/{transferId}` | path: `transferId`; `company` is accepted by the service method but **not used in the URL** (`interunitApiService.ts:465-473`) | Fetch the full transfer header + lines + boxes used to render the challan | `page.tsx:28`; service `interunitApiService.ts:465-473` |

Mechanics:
- `API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'` (`interunitApiService.ts:4`).
- All requests go through `fetchJSON` (`interunitApiService.ts:92-...`), which injects auth headers via `getAuthHeaders()` (`interunitApiService.ts:80-90`): `Accept`/`Content-Type: application/json` plus `Authorization: Bearer <accessToken>` pulled from the Zustand `useAuthStore` (`interunitApiService.ts:81`). Non-2xx responses throw an `Error` carrying a `.response` object with `data`/`status`/`detail` (`interunitApiService.ts:101-127`), which the page surfaces as the inline error message (`page.tsx:30-32`).

---

## 10. Backend & DB wiring touched

- The page calls **one** backend endpoint: `GET /interunit/transfers/{transferId}` (constructed at `interunitApiService.ts:467`).
- The backend handler for the `interunit` router is **not present in any of the indexed/accessible workspace directories** (searched the production module router `c:\Candor\SSD files of Candor\Consumption\Backend\app\modules\production\router.py` and the whole `Backend\app` tree — no `interunit` route definitions found; the only interunit Python files found are MCP-server scaffolding under `ims-app-backend\services\ims_service\interunit_*.py`, not the FastAPI route). So the exact SQL/ORM and table names backing this endpoint **cannot be confirmed from the available source**.
- From the response shape consumed by the frontend, the endpoint clearly returns a transfer header (`challan_no`/`request_no`, `transfer_date`/`stock_trf_date`, `from_warehouse`/`to_warehouse`, `vehicle_number`, `driver_name`, `approval_authority`/`approved_by`, `reason_code`/`remark`), a `lines`/`items` array of line items, and a `boxes` array (box-level scan records). No DB writes are performed by this page — it is read-only (`GET`) and produces no mutations.

---

## 11. Cross-module linkages

- **Entry point:** Reached only by the **"DC" / Printer button** on the Transfer dashboard (`d:\test\frontend-\app\[company]\transfer\page.tsx`), which calls `router.push(\`/${company}/transfer/dc/${t.id}\`)` from four list/card variants: lines **854, 947, 1567, 1648**. `t.id` is the transfer header id passed as `transferId`.
- **Shared service:** `InterunitApiService` (`d:\test\frontend-\lib\interunitApiService.ts`) is the same client used across the Transfer module (transfer-in, transfer-out, acknowledgement/STBR reconciliation, etc.); this page only touches `getTransferById`.
- **Shared constant:** `WAREHOUSE_ADDRESSES` / `WAREHOUSES` from `d:\test\frontend-\lib\constants\warehouses.ts` — the single source of truth for warehouse names/addresses used by every warehouse Select and document across the app (note: the file's display-name helpers like `getDisplayWarehouseName` are **not** used here; the DC does a raw `warehouseAddresses[code]` lookup).
- **Shared auth:** Zustand `useAuthStore` for the bearer token (`interunitApiService.ts:2, 81`).
- **Shared asset:** `d:\test\frontend-\public\candor-logo.jpg` (referenced as `/candor-logo.jpg`).
- **Type:** `Company` from `@/types/auth` → `@/lib/api`.

---

## 12. Gotchas

1. **`company` param is dead weight in the API call.** `getTransferById(company, transferId)` ignores `company` when building the URL (`interunitApiService.ts:467`). The challan is fetched purely by `transferId`; the company segment in the URL has no effect on the data returned.
2. **`boxesPending` is hard-coded to `0`** (`page.tsx:75`). Consequently the Gate-Pass status chip **always shows `COMPLETE`** (green) — the `PARTIAL` (red) branch at `DeliveryChallan.tsx:503-510` is effectively unreachable from this page, even for partially-loaded transfers.
3. **"No. of Boxes total" vs per-row boxes are inconsistent in spirit.** The totals row "No. of Boxes" cell uses raw `items.length` (`DeliveryChallan.tsx:312`, `499`), while per-row "No. of Boxes" is the consolidated `box_count`. They sum to the same number only because each raw line == one box, but the labels can mislead if a single `lines[]` entry ever represents multiple boxes.
4. **Auto-print can fire before data/images settle on slow connections.** The 500 ms `setTimeout` (`DeliveryChallan.tsx:52`) is a fixed delay, not an `onload` for the logo image — a slow logo load could print before the image appears. There's no re-print button to recover (§5).
5. **No lot/batch info on the document.** Despite this being an inter-unit stock transfer (lot-tracked elsewhere in the module), the printed challan shows no lot numbers; `transferData.boxes` is used only for a count that is then discarded (`boxesProvided` is passed but never rendered).
6. **Heavy `console.log` noise in production.** The mount effect (`DeliveryChallan.tsx:38-50`) and the consolidation memo (`DeliveryChallan.tsx:99-101, 125`) log full item payloads to the console on every render/print.
7. **Warehouse lookup is exact-match only.** `warehouseAddresses[fromWarehouse]` uses the raw code with no normalization (`normalizeWarehouseName`/alias map from `warehouses.ts` is bypassed). If the backend stores a legacy/alias spelling (e.g. `old_savla`), the name/address will fall back to the raw string instead of the canonical address.
8. **`requestDate` is printed as-is.** Whatever string the backend returns for `transfer_date`/`stock_trf_date` is rendered verbatim (`DeliveryChallan.tsx:164, 431`) — no date formatting/locale handling.
9. **Pagination is purely by item count (10/page), not by measured height.** A page with very long descriptions could still overflow A4 since the chunk size is fixed (`DeliveryChallan.tsx:130`).
