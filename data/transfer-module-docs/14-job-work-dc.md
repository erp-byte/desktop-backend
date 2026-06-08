# Job Work Delivery Challan (Print) — `/[company]/transfer/job-work/dc/[challanId]`

- **File:** `d:\test\frontend-\app\[company]\transfer\job-work\dc\[challanId]\page.tsx`
- **Print component:** `d:\test\frontend-\components\transfer\JobWorkDC.tsx`
- **URL:** `/[company]/transfer/job-work/dc/[challanId]` (e.g. `/candor/transfer/job-work/dc/JW-OUT-0001`)
- **Purpose:** A self-contained, auto-printing document page for a Job Work "Material Out" delivery challan. It loads one challan by `challanId`, maps its data into the `JobWorkDC` presentational component, and renders a printable A4 **Delivery Challan** plus a tear-off **Gate Pass**. Used both immediately after submitting a Material Out (data handed off via `sessionStorage`) and later for reprints (data re-fetched from the backend).

---

## 1. Route & params

- Next.js App Router dynamic segment page. `"use client"` component (page.tsx:1).
- Props type `DCPageProps` (page.tsx:8-13) declares `params: { company: string; challanId: string }`.
- Destructured at page.tsx:16: `const { company, challanId } = params`.
  - `company` — tenant/company slug from the URL; **only used for navigation context**, not passed into the document body (the "Go Back" path relies on `router.back()`, and company branding is hard-coded — see Gotchas).
  - `challanId` — the challan number; drives both the `sessionStorage` key and the API fetch.
- `useRouter()` from `next/navigation` (page.tsx:4,17) — used only for the error-state "Go Back" button (page.tsx:67).

Local state (page.tsx:18-20):
- `data: any` — the loaded challan record (null until loaded).
- `loading: boolean` — true until the load resolves (default `true`).
- `error: string | null` — error message for the failure view.

---

## 2. Printable layout & structure (top → bottom)

The page itself renders one of three views depending on state; the actual printable layout lives in `JobWorkDC`.

**page.tsx view states:**
1. **Loading** (page.tsx:51-60): centered spinner (`Loader2`, orange) + "Loading Delivery Challan..." over `min-h-screen bg-gray-50`.
2. **Error / no data** (page.tsx:62-71): red error text (`error` or `"No data found"`) + a "Go Back" link button.
3. **Success** (page.tsx:117-139): renders `<JobWorkDC ... />` with mapped props.

**`JobWorkDC` printable document** (`JobWorkDC.tsx`), top → bottom. The whole thing lives in a wrapper `<div className="w-full bg-white jw-dc-print">` with `padding: 0.5cm 1cm` (JobWorkDC.tsx:189). The `jw-dc-print` class is the print-isolation hook (see §6).

**A. Delivery Challan pages** (paginated, JobWorkDC.tsx:191-284). Items are chunked `ITEMS_PER_PAGE = 8` (JobWorkDC.tsx:92-96); each chunk becomes its own `<table>` with `pageBreakAfter: 'always'` except the last (JobWorkDC.tsx:194). Each page's `<thead>` is `renderHeader(pageNum)` (JobWorkDC.tsx:106-186):
   - **Company header band** (JobWorkDC.tsx:109-127): `/candor-logo.jpg` logo, hard-coded title "CANDOR DATES PRIVATE LIMITED", `company.address`, line of `GSTIN | FSSAI | Email`, and a right-aligned boxed "DELIVERY CHALLAN" badge with sub-label "Job Work - Material Out". On pages 2+, shows "Page N of M" (JobWorkDC.tsx:123).
   - **Challan info row** (JobWorkDC.tsx:130-143): Challan No (accent color), Date, E-Way Bill (or `'N/A'`), Vehicle No.
   - **Consignor / Consignee row** (JobWorkDC.tsx:146-162): left = "CONSIGNOR (From)" with `company.name`, `Warehouse: fromWarehouse`, address, state/GSTIN; right = "CONSIGNEE (Dispatch To)" with `dispatchTo.name`, address, optional city/pin, state, contact mobile.
   - **Purpose & transport row** (JobWorkDC.tsx:165-173): "Purpose of Work" (+ optional ` — sub_category`), Driver, optional "Dispatched Through".
   - **Column header row** (JobWorkDC.tsx:176-184): see §3.
   - **Body rows** (JobWorkDC.tsx:198-211): one row per consolidated item, zebra-striped.
   - **Empty filler rows** on the last page to keep height consistent (JobWorkDC.tsx:214-220), only when `pageItems.length < ITEMS_PER_PAGE`.
   - **Totals row** — last page only (JobWorkDC.tsx:223-229).
   - **Remarks / Expected return / job-work note** — last page only (JobWorkDC.tsx:232-248).
   - **Signature row** (JobWorkDC.tsx:251-268): "Prepared By", "Received By (Party)", "Authorized Signatory" (with `authorizedPerson`).
   - **Computer-generated footer note** (JobWorkDC.tsx:270-277).

**B. Cut line** (JobWorkDC.tsx:287-292): dashed separator with centered scissors label "✂ CUT HERE - GATE PASS BELOW".

**C. Gate Pass** (JobWorkDC.tsx:295-394), `pageBreakInside: 'avoid'`:
   - Header band with logo + "GATE PASS" + "Job Work - Material Out" (JobWorkDC.tsx:297-307).
   - Info rows: Challan No, Date, Vehicle, Driver, From; then Dispatch To + Purpose (JobWorkDC.tsx:311-321).
   - "MATERIAL SUMMARY" header + column headers (JobWorkDC.tsx:324-334).
   - Up to **6** consolidated items (`.slice(0, 6)`, JobWorkDC.tsx:337-346); if more, an italic "... and N more item(s) - refer Delivery Challan for full details" row (JobWorkDC.tsx:347-353).
   - Gate-pass totals row (JobWorkDC.tsx:356-362): Total Items count, total boxes, total net wt, total of `total_weight`.
   - Signature row: "Security Sign & Stamp", "Driver Sign", "Authorized By" (JobWorkDC.tsx:365-382).
   - Footer: "Present this gate pass at security gate | {company.name} | Challan: {challanNo}" (JobWorkDC.tsx:385-392).

---

## 3. Tables & columns

There are **two tables** plus their totals.

### A. Delivery Challan line-item table (per page)
Column headers at JobWorkDC.tsx:176-184; body cells at JobWorkDC.tsx:198-211.

| # | Column | Header line | Body source (consolidated item) | Notes |
|---|--------|-------------|----------------------------------|-------|
| 1 | S.No | 177 | `item.sl_no` (200) | Re-numbered per consolidated group (JobWorkDC.tsx:89) |
| 2 | Item Description | 178 | `item.item_description` (202) + sub-line `material_type / item_category / sub_category` (203) | Sub-line only if `item_category` present |
| 3 | Lot No | 179 | `item.lot_number || '-'` (205) | Monospace |
| 4 | UOM | 180 | `item.uom || 'KG'` (206) | |
| 5 | Case Pack | 181 | `item.unit_pack_size || '-'` (207) | |
| 6 | Boxes | 182 | `item.quantity_boxes` (208) | Bold |
| 7 | Net Wt (Kg) | 183 | `item.net_weight.toFixed(3)` (209) | Right, bold |

**Challan totals row** (last page, JobWorkDC.tsx:225-229): "TOTAL" label spans cols 1-5; col 6 = `totals.total_boxes`; col 7 = `totals.total_quantity_kgs.toFixed(3)`. (No amount column on the challan despite amount being computed — see Gotchas.)

### B. Gate Pass material-summary table
Headers at JobWorkDC.tsx:327-334; body at JobWorkDC.tsx:337-346.

| # | Column | Header line | Body source | Notes |
|---|--------|-------------|-------------|-------|
| 1 | S.No | 328 | `item.sl_no` (339) | |
| 2 | Item Description | 329 | `item.item_description` (340) | |
| 3 | Lot No | 330 | `item.lot_number || '-'` (341) | Monospace |
| 4 | Boxes | 331 | `item.quantity_boxes` (342) | Bold |
| 5 | Net Wt (Kg) | 332 | `item.net_weight.toFixed(3)` (343) | |
| 6 | Total Wt (Kg) | 333 | `item.total_weight.toFixed(3)` (344) | |

**Gate-pass totals row** (JobWorkDC.tsx:356-362): "Total Items: N" (N = `consolidatedItems.length`), `totals.total_boxes`, `totals.total_quantity_kgs.toFixed(3)`, and `consolidatedItems.reduce(... total_weight)` summed inline.

### Consolidation logic (shared by both tables)
`consolidatedItems` (`useMemo`, JobWorkDC.tsx:75-90): groups raw `lineItems` by key `` `${item_description}||${lot_number}` ``, summing `quantity_boxes`, `net_weight`, `total_weight`, and `amount`; then re-assigns `sl_no` sequentially. So duplicate item+lot rows collapse into one line, and the displayed S.No differs from the source `sl_no`.

---

## 4. Data shown & sources

`data` is loaded by the `useEffect` (page.tsx:22-49) from **two possible shapes**, detected at page.tsx:77 via `isFromApi = !data.header && !data.line_items`:

1. **`sessionStorage` (just-submitted)** — key `` `jw-dc-${challanId}` `` (page.tsx:24). Written by the Material Out page on submit (material-out/page.tsx:1225) as the full submit `payload`, which contains nested `header`, `line_items`, `company`, `totals`, `dispatch_to`, etc. This is preferred and returns early without an API call (page.tsx:26-32).
2. **API `GET /job-work/out/{challan}`** (page.tsx:35-48) — flat shape: `items`, `from_warehouse`, `driver_name`, `vehicle_no`, etc. at the root (see §10 for full field list).

**Prop mapping (page.tsx:79-138):**
- `companyInfo` (page.tsx:79-87): `data.company` if present, else a **hard-coded Candor default** (name, address, GSTIN `27AAKCC3130A1Z9`, FSSAI `11522998001846`, state Maharashtra/27, email `accounts@candorfoods.in`).
- `header` = `data.header || {}` (page.tsx:89).
- `dispatchTo` (page.tsx:90): merges `data.dispatch_to || data.party || {}` and back-fills `sub_category` from several places.
- `rawItems` = `data.line_items || data.items || []` (page.tsx:91).
- `totalsData` = `data.totals || {}` (page.tsx:92).
- `lineItems` (page.tsx:94-111): each raw item normalized with many fallbacks — `hsn_sac` defaults `'08041020'`, `gst_rate` `'0%'`, `uom` `'KG'`; `quantity_boxes` from `quantity_boxes ?? quantity.boxes ?? parseInt(case_pack)`; weights `parseFloat`'d with `quantity.kgs` fallbacks; `lot_number` from `lot_number || batch_number`.
- Computed totals (page.tsx:113-115): `totalBoxes`, `totalKgs`, `totalAmount` summed from `lineItems`.
- Props passed to `JobWorkDC` (page.tsx:118-138): each field reads `data.* || header.* || (isFromApi ? data.* : '') || ''`, covering both shapes (e.g. `motorVehicleNo` at 124, `driverName` at 125, `expectedReturnDate` at 129). `totals` (page.tsx:133-137) prefers `totalsData` values, falling back to the locally computed sums; `total_boxes` always uses the computed `totalBoxes`.

---

## 5. Buttons

| Label | Line | Handler | Action |
|-------|------|---------|--------|
| Go Back | page.tsx:67 | `onClick={() => router.back()}` | Navigates to the previous history entry. **Only rendered in the error/no-data view.** |

There is **no on-screen Print button and no Download button.** Printing is fully automatic (see §6). The success/print view (`JobWorkDC`) renders no buttons at all — it is a pure document.

---

## 6. Print mechanics

- **Auto-print:** `JobWorkDC` triggers printing itself via `useEffect` (JobWorkDC.tsx:69-72): `setTimeout(() => { window.print() }, 600)` on mount, with cleanup `clearTimeout`. The 600 ms delay lets the logo image and layout settle before the browser print dialog opens. Uses the native `window.print()` — **no** `react-to-print`, no third-party library.
- **Print isolation via CSS** (`<style jsx global>` at JobWorkDC.tsx:396-424):
  - `@page { size: A4; margin: 0; }` (JobWorkDC.tsx:398-401).
  - `body * { visibility: hidden; }` then `.jw-dc-print, .jw-dc-print * { visibility: visible; }` (JobWorkDC.tsx:402-403) — hides everything except the document subtree. The wrapper carries the `jw-dc-print` class (JobWorkDC.tsx:189).
  - `.jw-dc-print { position: absolute; left:0; top:0; width:100%; }` (JobWorkDC.tsx:404-409) — pins the document to the page origin for print.
  - `print-color-adjust: exact` / `-webkit-print-color-adjust: exact` on `body` and `*` (JobWorkDC.tsx:410-419) — forces the colored bands/accents to print.
  - `@media screen { body { background:#e8e4df; } }` (JobWorkDC.tsx:421-423) — gray page background on screen only.
- **Page breaks:** challan pages use `pageBreakAfter: 'always'` between pages (JobWorkDC.tsx:194); cut line uses `pageBreakBefore: 'avoid'` (JobWorkDC.tsx:287); gate pass uses `pageBreakInside: 'avoid'` (JobWorkDC.tsx:295).

---

## 7. Keyboard / click directions

- **None implemented in code.** No `onKeyDown`/keyboard listeners exist on either file. The user-facing flow is: the browser's print dialog auto-opens (§6), and the user confirms/cancels there. Closing/canceling the dialog leaves the document visible on screen for manual re-print (Ctrl+P).
- The only click target is the error-view "Go Back" link (page.tsx:67).

---

## 8. Redirects

- **No automatic route redirects.** This page never calls `router.push`/`replace`.
- The only navigation is the manual `router.back()` from the error view (page.tsx:67).
- (Inbound: the page is *navigated to* from Material Out submit and from the Job Work list — see §11.)

---

## 9. API calls

| Method | Endpoint | Params | Purpose |
|--------|----------|--------|---------|
| GET | `${NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'}/job-work/out/{challanId}` | path: `challanId` (URL-encoded, page.tsx:37); header `Accept: application/json` (page.tsx:38) | Load a single Material Out challan by challan number for the DC/Gate Pass print. Only called when `sessionStorage` has no cached payload. Non-OK → throws `Failed to load challan: {status}` (page.tsx:39); 404 from backend → error view. |

No other network calls. No POST/PUT/DELETE from this page (it is read-only). The submit that *creates* the record happens on the Material Out page (`POST`/`PUT /job-work/out`, material-out/page.tsx:1206-1213), not here.

---

## 10. Backend & DB wiring touched

- **Endpoint:** `GET /job-work/out/{challan_no}` → `get_material_out_by_challan` in `d:\test\ims-app-backend\services\ims_service\job_work_server.py:1218-1303` (router prefix gives the `/job-work` base; route declared at :1218).
- Calls `_ensure_tables(db)` (job_work_server.py:1223) to create/migrate tables if missing.
- **Header query** (job_work_server.py:1225-1234): selects from `jb_materialout_header` where `LOWER(challan_no) = LOWER(:challan_no)`, `ORDER BY created_at DESC LIMIT 1` (newest wins if duplicate challan numbers exist). 404 if none (job_work_server.py:1236-1237).
- `dispatch_to` column is JSON, parsed with `json.loads` fallback to `{}` (job_work_server.py:1242-1247).
- **Lines query** (job_work_server.py:1249-1258): selects from `jb_materialout_lines` where `header_id = :header_id` ordered by `sl_no`.
- **Response shape** (job_work_server.py:1260-1303) — flat root object: `id, challan_no, job_work_date, from_warehouse, to_party, status, vehicle_no, sub_category, dispatch_to (object), driver_name, authorized_person, remarks, party_address, purpose_of_work, contact_person, contact_number, expected_return_date`, plus `items[]` with `sl_no, item_description, sub_category, uom, quantity_boxes, net_weight, total_weight, lot_number, remarks, rate_per_kg, amount, material_type, item_category, batch_number, manufacturing_date, expiry_date, box_id, transaction_no, cold_unit, item_mark`.
- **Tables:** `jb_materialout_header`, `jb_materialout_lines` (both in `job_work_server.py`). This page performs read-only access; no DB writes are made by the DC page.

---

## 11. Cross-module linkages

- **Inbound navigation (entry points):**
  - **Material Out submit** — `d:\test\frontend-\app\[company]\transfer\job-work\material-out\page.tsx`:
    - Writes `sessionStorage['jw-dc-${challanNo}']` = full submit payload (material-out/page.tsx:1225).
    - `router.push('/${company}/transfer/job-work/dc/${encodeURIComponent(challanNo)}')` after non-cold-storage submit (material-out/page.tsx:1261) and from the cold-storage popup path (material-out/page.tsx:1972).
  - **Job Work list page** — `d:\test\frontend-\app\[company]\transfer\job-work\page.tsx`: "print DC" actions push the same route (page.tsx:2197, page.tsx:2246), with **no** sessionStorage, so those always go through the API fetch.
- **Shared component:** `JobWorkDC` (`components/transfer/JobWorkDC.tsx`) is the document renderer; only this DC page imports it (grep confirms no other importer).
- **Backend module:** Job Work IMS service (`job_work_server.py`); the same module also owns Material In / Inward Receipt endpoints referenced near this code (e.g. `POST /job-work/material-in`).
- **Asset:** `/candor-logo.jpg` (served from `public/`) is embedded in both the challan header and gate pass.

---

## 12. Gotchas

- **Hard-coded company branding.** The visible company title "CANDOR DATES PRIVATE LIMITED" is hard-coded in the challan header (JobWorkDC.tsx:114) regardless of `company.name`; only address/GSTIN/FSSAI/email come from the `company` prop. The `[company]` route param is **not** used to brand the document — a different tenant would still print "Candor Dates".
- **Default company fallback.** When the source lacks `data.company` (always the case for the API path, which returns no `company` object), the page injects the hard-coded Candor default (page.tsx:79-87). So the GSTIN/address etc. are constants, not from the DB record.
- **Two divergent data shapes.** sessionStorage carries a *nested* payload (`header`, `line_items`, `company`, `totals`); the API returns a *flat* shape (`items`, root-level fields, no `company`/`totals`). The `isFromApi` switch (page.tsx:77) and the layered `||` fallbacks (page.tsx:118-137) bridge them, but it means reprint-from-list and print-after-submit can show subtly different data (e.g. amount/totals presence).
- **`amount` computed but never shown.** `lineItems[].amount`, `totalAmount` (page.tsx:109,115) and `totals.total_amount` (page.tsx:136) are computed and the consolidation even sums `amount` (JobWorkDC.tsx:84), but no template column or row renders any amount/value. The document is quantity/weight-only (job-work is non-sale).
- **Consolidation hides per-box detail.** Items are merged by `item_description + lot_number` (JobWorkDC.tsx:75-90), so box-level fields (`box_id`, `transaction_no`, `item_mark`, batch/expiry from the API) are dropped from the printed output; S.No is renumbered and won't match the source `sl_no`.
- **Gate pass truncates at 6 items.** Only the first 6 consolidated items print on the gate pass (JobWorkDC.tsx:337); the rest are summarized as "... and N more" — but the gate-pass totals still reflect all items (JobWorkDC.tsx:356-362), so the listed lines can under-represent the total counts.
- **Auto-print race.** `window.print()` fires after a fixed 600 ms (JobWorkDC.tsx:70). If the logo image or a large multi-page table hasn't painted, the print preview can be incomplete; there's no `onload`/image-ready gate. There is also no on-screen Print button to retry (user must Ctrl+P).
- **sessionStorage staleness.** The key `jw-dc-${challanId}` is set on submit and read with preference over the API (page.tsx:26-32). If the record is later edited, an old tab/session reprint could still serve the stale cached payload. The cache is never explicitly cleared by this page.
- **`window`/`sessionStorage` at module-eval.** Access is inside `useEffect` (client-only) so SSR-safe, but the page is `"use client"` and assumes a browser; the `NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'` fallback (page.tsx:37) will silently point at localhost if the env var is unset in a deployed build.
- **`data: any`.** No typing on the loaded record (page.tsx:18); all shape handling is defensive `||`/`??` chains, so a malformed record fails quietly to blanks rather than erroring.
