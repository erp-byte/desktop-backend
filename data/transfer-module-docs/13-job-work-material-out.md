# Job Work Material-Out — `/[company]/transfer/job-work/material-out`

| | |
|---|---|
| **File** | `d:\test\frontend-\app\[company]\transfer\job-work\material-out\page.tsx` (1983 lines) |
| **URL** | `/{company}/transfer/job-work/material-out` (also `?edit={id}` for edit mode) |
| **Purpose** | Create (or edit) a Job Work **Material Out** challan — a document recording materials sent to a 3rd-party vendor for processing (de-seeding, dicing, etc.). Supports normal warehouse dispatch, **cold-storage FIFO box-picking**, QR-scan box capture, and manual box lookup. On submit it persists a header + line items, deducts cold-storage stock, and routes to the DC print page. |

This is **both a form and an embedded list** (a master form with a working "Article Entry" sub-form that feeds an "Added Items" table). It is **not** a list/index page — there is no listing of existing challans here.

---

## 1. Route & params

- Next.js App Router dynamic segment: `[company]`. Props typed as `MaterialOutPageProps` with `params.company: Company` (`page.tsx:27-31`, `:384-385`).
- Client component (`"use client"`, `:1`).
- Query param `edit` read via `useSearchParams()` (`:387`, `:392`). `editId = searchParams.get('edit')`; `isEditMode = !!editId` (`:392-393`). When present, page loads the existing record (PUT on submit instead of POST).
- `company` is used directly in API URLs and child-component props; uppercased inside `dropdownApi` (`api.ts:630`).

---

## 2. Layout & structure

Root: `<div>` with `p-3..lg:p-6`, gray background, min-h-screen (`:1275`). Vertical sections:

1. **Page header** (`:1276-1291`) — back button (→ `/{company}/transfer/job-work`), `Send` icon, title toggles `"Edit Material Out"` / `"Material Out - Job Work"`, subtitle.
2. **Edit loading overlay** (`:1294-1299`) — spinner shown while `editLoading`.
3. **`<form onSubmit={handleSubmitOut}>`** (`:1302`, hidden while `editLoading`) containing:
   - **Card: Material Out - Challan Details** (`:1305-1360`) — Challan No, Dated, From Warehouse, E-Way Bill No.
   - **Card: Dispatch To** (`:1363-1458`) — vendor select + party fields + sub-category.
   - **Card: Transport & Dispatch Details** (`:1461-1485`) — Motor Vehicle No, Dispatched Through.
   - **Article Management** section (`:1488-1742`) — "Add Article" + a list of editable **Article Entry** sub-forms (`articles.map`). Each sub-form renders one of two modes depending on whether From Warehouse = "Cold storage".
   - **Card: Scan QR / Manual Box Entry** (`:1745-1846`) — camera scanner + manual box fetch.
   - **Card: Added Items table** (`:1849-1916`, only if `articlesList.length > 0`).
   - **Card: Remarks** (`:1919-1927`).
   - **Submit bar** (`:1930-1938`) — Cancel + Submit.
4. **Cold Transfer Summary Dialog** (`:1942-1979`, outside form) — appears post-submit for cold-storage dispatches.

Two parallel data structures drive the page:
- `articles: Article[]` (`:488`) — the **working/draft** sub-forms (default one `emptyArticle()`).
- `articlesList: JobWorkEntry[]` (`:489`) — the **committed line items** shown in the table and submitted. Entries are added via "Add to Articles List", QR scan, or manual box fetch.

---

## 3. Form fields

### Challan / header (`headerData` state, `:406-418`)

| Field | Type | Required/Validation | Default | Source |
|---|---|---|---|---|
| Challan No | text Input + regen button | not validated; editable | `generateChallanNo()` → `JB{YYYYMMDDHHmm}` (`:399-402`) | local state `challanNo` (`:404`) |
| Dated (`jobWorkDate`) | `<input type=date>` (display `dd-mm-yyyy` ↔ value `yyyy-mm-dd`, `:1328-1334`) | none | today as `dd-mm-yyyy` (`:397`) | `headerData.jobWorkDate` |
| From Warehouse | Select (`:1338-1350`) | **Required** (`:1105`) | `""` | hardcoded: W202, A185, A101, A68, F53, **Cold storage** |
| E-Way Bill No | text Input | none | `""` | `headerData.e_way_bill_no` |
| Dispatched Through | text Input (`:1479`) | none | `""` | `headerData.dispatched_through` |
| Remarks | Textarea (`:1924`) | none | `""` | `headerData.remarks` |

### Dispatch To (`dispatchTo` state, `:468-478`)

| Field | Type | Required/Validation | Default | Source |
|---|---|---|---|---|
| Name | Select of vendors + "Other" → free text when Other (`:1376-1391`) | **Required** (`dispatchTo.name`, `:1106`) | `""` | hardcoded `vendorList` (`:420-446`) |
| Address | text Input | none | autofilled from vendor | `vendorList` / manual |
| State | text Input | none | `"Maharashtra"` | `:471` |
| City (District) | text Input | none | `""` | vendor / manual |
| PIN Code | text Input | none | `""` | vendor / manual |
| Contact Details - Company | text Input | none | `""` | vendor / manual |
| Contact Mobile Nos. | text Input | none | `""` | vendor / manual |
| E-mail | `type=email` Input | none | `""` | vendor / manual |
| Sub Category | Select (`subCatOptions`) + "Other" → free text (`:1430-1452`) | none | `""` | `subCatOptions` (`:493`): De seeding, Dicing, Cracking, Stuffing, Vacuum Packaging, Slicing |

`handleVendorSelect` (`:448-466`) autofills all party fields from the chosen vendor and toggles `subCatIsOther`. Selecting "Other" clears the block and shows the free-text name input.

### Transport (`transferInfo` state, `:480-486`)

| Field | Type | Required/Validation | Default | Source |
|---|---|---|---|---|
| Motor Vehicle No | text Input (`:1474`) | **Required** (`:1108`) | `""` | `transferInfo.vehicleNumber` |

Note: `transferInfo` also holds `driverName`, `authorizedPerson`, etc., used in payload (`:1119-1120`, `:1154-1155`) but **no UI inputs exist on this page** for driver/authorized person (only vehicle number is rendered). `driverName === "other"` branch (`:1120`) is effectively dead UI-wise here.

### Article Entry sub-form — non-cold-storage mode (`:1593-1738`)

| Field | Type | Required/Validation | Default | Source |
|---|---|---|---|---|
| Quick Search Item | text Input w/ dropdown (`:1595-1641`) | min 2 chars to trigger | — | `/interunit/categorial-search` |
| Material Type * | `MaterialTypeDropdown` (`:1647`) | none enforced (UI `*`) | `""` | dropdown (RM/PM/FG) |
| Item Category * | `ItemCategoryDropdown` | disabled until material type | `""` | dropdown |
| Sub Category | `SubCategoryDropdown` | disabled until category | `""` | dropdown |
| Item Description * | `ItemDescriptionDropdown` (`:1661-1664`) | disabled until category+sub | `""` | dropdown; selecting also fetches SKU + UOM |
| Unit Pack Size/Count | number (`:1672`) | FG requires >0 at add-time (`:943-946`) | `0` | manual / auto from item |
| UOM | Select BOX/BAG/CARTON (`:1678`) | none | `""` | hardcoded |
| Case Pack/Box Wt. (`packaging_type`) | number (`:1689`) | none | `0` | manual |
| Quantity (Box/Bags) (`quantity_units`) | text→number (`:1695`) | none | `0` | manual |
| Net Weight (Kg) | number (`:1704`) | auto-calculated, editable | `0` | `calculateNetWeight` |
| Total Wt (Kg) (Gross) (`total_weight`) | number (`:1710`) | none | `0` | manual |
| Lot Number (Optional) | text (`:1716`) | optional | `""` | manual |
| Item Remarks (`line_remarks`) | text (`:1726`) | none | `""` | manual |

`hsn_sac` defaults `"08041020"`, `gst_rate` defaults `"0%"` (`:370-371`), no UI field — carried into payload.

### Article Entry sub-form — cold-storage mode (`:1515-1591`)

Active when `headerData.fromWarehouse === "Cold storage"` (`isColdStorageFrom`, `:895`). Most fields auto-fill from the cold-storage stock search (read-only):

| Field | Type | Required/Validation | Default | Source |
|---|---|---|---|---|
| Item Category | readOnly Input (`:1529`) | — | auto | cold stock record |
| Item Description | readOnly Input (`:1533`) | — | auto | cold stock record |
| Weight per Box (kg) (`net_weight`) | readOnly Input (`:1537`) | — | auto | `record.weight_kg` |
| Total Weight (kgs) | readOnly computed `qty*net_weight` (`:1541`) | — | auto | computed |
| No. of Boxes/Cartons * (`quantity_units`) | number, `max=cs_max_boxes` (`:1549`) | clamped to available (`:1553-1558`); ≤ `cs_max_boxes` at add (`:937-940`) | `0` | manual |
| UOM * | Select BOX/CARTON/BAG (`:1567`) | UI `*` | `""` | hardcoded |
| Lot Number * | text (`:1578`) | UI `*` | auto from record | manual/auto |

### Manual Box Entry (`:1789-1844`)

| Field | Type | Required/Validation | Default | Source |
|---|---|---|---|---|
| Box Number | text (`:1798`) | **Required** before fetch (`:513`) | `""` | `manualBoxId` |
| Transaction No | text (`:1814`, id `jw-manual-txn-input`) | **Required** before fetch (`:513`) | `""` | `manualTransactionNo` |

---

## 4. Material / box selection / scanning

Three independent ways to populate `articlesList`:

1. **Article Entry sub-form → "Add to Articles List"** (`handleAddToList`, `:929-1087`):
   - Non-cold: builds **one** `JobWorkEntry` with the full quantity (`:1054-1079`).
   - Cold-storage (`cs_max_boxes !== null`): validates qty ≤ available, requires item/lot/inward/company, then calls `ColdStorageApiService.pickBoxes` (`:964-971`) to fetch **unique per-box IDs in FIFO order** and creates **one entry per box** (`:990-1048`), each carrying its own `boxId`, `transactionNo`, and a `coldStockSnapshot` for restore-on-delete. Guards reject if fewer boxes returned than requested (`:977-980`) or duplicate `box_id`s (`:981-985`). Comment notes this fixes `TRANS202605131331`-style inventory loss where 700 boxes collapsed to 1.

2. **QR Camera Scan** (`HighPerformanceQRScanner` → `handleQRScanSuccess`, `:582-669`):
   - Parses JSON QR. Detects format: Bulk-Entry (`tx` starts `BE`), new format (`tx`+`bi`), or legacy `TX`/`CONS`.
   - Looks up box details from the matching `interunit` endpoint (`:610`, `:617`, `:625`), merges into `boxData`, and appends a single-box `JobWorkEntry` (qty "1").
   - Duplicate guard on `boxId`+`transactionNo` (`:600-604`); re-entrancy guard `isProcessingRef` (`:583-584`, `:667`).

3. **Manual Box Fetch** (`handleManualBoxFetch`, `:512-579`):
   - Requires both Box Number + Transaction No; duplicate guard (`:519-525`).
   - GETs `/interunit/box-lookup/{company}?box_number=&transaction_no=` (`:529`), appends a single-box entry, clears inputs.

The cold-storage stock search itself is the `ColdStorageStockSearch` component (`:128-289`): debounced (400 ms) search by lot no and/or group/description against a chosen cold company (CFPL/CDPL), rendering a results table whose "Select" button calls `handleSelectColdStorageStock` (`:898-926`) to populate the article's read-only fields and cold metadata (`cs_box_id`, `cs_transaction_no`, `cs_inward_no`, `cs_company`, `cold_company` mapped to "Savla D-39"/"Rishi", `:899`).

---

## 5. Tables & columns

### A. Cold-storage stock search results (`:242-286`)
Columns: # · Inward Dt · Unit · Item Description · Item Mark · Lot No · Qty of Cartons · Weight (kg) · Total Inv (kgs) (= cartons×weight) · **Action** (Select button). Scrollable, sticky header, footer "Showing N results".

### B. Added Items table (`:1858-1912`, only when `articlesList.length > 0`)
Header (`:1862-1871`): `#` · Description · Box ID · Transaction No · Qty (read-only display) · **Boxes** (editable `quantity` Input) · **Net Wt (Kg)** (editable `netWeight` Input) · Total Wt (Kg) (display) · **Process** (shows `dispatchTo.sub_category`) · **Action** (delete).
- `Qty` cell shows `entry.quantity` read-only (`:1881`); the **Boxes** cell is an editable number bound to the same `quantity` field via `updateListEntry` (`:1883`) — both reflect the same value.
- Net Wt is editable inline (`:1887`, step 0.001).
- `tfoot` totals (`:1901-1910`): sum of quantity (twice), sum netWeight (3dp), sum totalWeight (3dp).

---

## 6. Dropdowns & data sources

| Dropdown | Component / source | Backing API |
|---|---|---|
| Material Type | `MaterialTypeDropdown` (`:34-63`) — filters to RM/PM/FG; falls back to those 3 on error | `dropdownApi.fetchDropdown` → `/inward/sku-dropdown` (`api.ts:641`) |
| Item Category | `ItemCategoryDropdown` (`:66-77`) → `useItemCategories` | `/inward/sku-dropdown` |
| Sub Category | `SubCategoryDropdown` (`:80-91`) → `useSubCategories` | `/inward/sku-dropdown` |
| Item Description | `ItemDescriptionDropdown` (`:94-123`) → `useCategorialItemDescriptions` | `/interunit/categorial-dropdown` (`useDropdownData.ts:540`); returns UOM values |
| SKU lookup (on item select) | `dropdownApi.fetchSkuId` (`:110`) | `/inward/sku-dropdown`-family SKU endpoint (`api.ts:685`) |
| Quick item search | inline `handleItemSearch` (`:843-866`) | `/interunit/categorial-search?search=&limit=200` |
| Cold company switcher | inline Select CFPL/CDPL (`:192-200`) | drives `coldCompany` for search/pick |
| From Warehouse | hardcoded Select (`:1343-1348`) | static |
| Vendor Name | hardcoded `vendorList` (`:420-446`) + "Other" | static |
| Dispatch Sub Category | hardcoded `subCatOptions` (`:493`) + "Other" | static |
| UOM (both modes) | hardcoded Select (`:1567-1573`, `:1678-1684`) | static |

`SearchableSelect` (`@/components/ui/searchable-select`) is the shared searchable dropdown used by the four category dropdowns. Item-description selection (`:103-116`) also pushes `item_description`, `unit_pack_size` (from UOM), and resolved `sku_id` into the article via `updateArticle`.

---

## 7. Buttons

| Label | Line | Handler | Action/Redirect |
|---|---|---|---|
| Back (← icon) | `:1278` | `router.push` | → `/{company}/transfer/job-work` |
| Regenerate challan (↻) | `:1318-1323` | `setChallanNo(generateChallanNo())` | new `JB…` number |
| Add Article | `:1496-1498` | `addArticle` | appends empty `Article` sub-form |
| Remove Article (🗑, per article when >1) | `:1508` | `removeArticle(id)` | removes draft article; blocks last one |
| Select (cold stock row) | `:273-275` | `handleSelect`→`onSelect` | fills article from stock record |
| Add to Articles List (cold + normal) | `:1586-1589`, `:1733-1736` | `handleAddToList(article)` | validates, FIFO-picks boxes if cold, appends to `articlesList`, resets the draft article |
| Start Camera Scan | `:1759-1765` | `setShowScanner(true)` | mounts QR scanner |
| Fetch Box | `:1828-1839` | `handleManualBoxFetch` | box lookup → append entry |
| Delete (added item row, 🗑) | `:1893-1896` | `removeFromList(id)` | removes line from `articlesList` |
| Copy (in summary dialog) | `:1951-1962` | `navigator.clipboard.writeText` | copies summary text, shows ✔ 2s |
| OK (summary dialog) | `:1968-1975` | close + `router.push` | → DC page |
| Cancel | `:1931-1932` | `router.push` | → `/{company}/transfer/job-work` |
| Submit / Update Material Out | `:1933-1937` | form `submit` → `handleSubmitOut` | POST/PUT (see §8); disabled while submitting or empty list |

Quick-search result items are buttons (`:1623`) using `onMouseDown` (preventDefault) → `handleItemSelect`.

---

## 8. Submit / save flow

`handleSubmitOut` (`:1101-1268`):

1. **Validation** (`:1103-1113`): From Warehouse, Dispatch-To name, ≥1 item in `articlesList`, Vehicle number. Errors toasted joined by " • ".
2. Sets `submitting`, computes `totalKgs`/`totalAmount` from `articlesList` (`:1122-1123`), and builds `payload` (`:1125-1202`):
   - Hardcoded `company` block = **CANDOR DATES PRIVATE LIMITED** with fixed GSTIN/FSSAI/address (`:1127-1135`) — *not* derived from the URL `company`.
   - `dispatch_to` and duplicated `party` = `dispatchTo` (`:1136-1137`).
   - `header` block (`:1143-1158`) with `type: "OUT"`.
   - `line_items` mapped from `articlesList` (`:1159-1189`), including `box_id`, `transaction_no`, `cold_unit`, `item_mark`, `cold_stock_snapshot`. `clean()` (`:1117`) strips `"N/A"`/falsy.
   - `totals`, `tax_summary` (tax 0/NIL, hsn from first line).
3. **API call** (`:1204-1218`): `PUT /job-work/out/{editId}` if edit else `POST /job-work/out`, both with `?created_by={user.email}` query (`:1206-1208`). JSON body = payload.
4. **On success** (`:1220-1262`):
   - Toast, `clearSavedData()` (clears the localStorage draft).
   - Saves payload to `sessionStorage["jw-dc-{challanNo}"]` for the DC page (`:1225`).
   - **If cold-storage dispatch**: builds a unit-wise / item-mark-consolidated text summary and opens `coldTransferPopup` dialog (`:1229-1258`) instead of navigating immediately.
   - **Else**: `router.push('/{company}/transfer/job-work/dc/{challanNo}')` (`:1261`).
5. **On error** (`:1263-1264`): toast with `err.detail` or status. `finally` clears `submitting`.

**Draft persistence:** `useFormPersistence` (`:676-682`) saves `headerData`, `transferInfo`, `articles`, `articlesList`, `challanNo` to localStorage under key `draft-jw-edit-{editId}` (edit) or `draft-job-work-out` (new), debounced 300 ms, restored on mount.

**Edit load:** `useEffect` on `editId` (`:685-783`) GETs `/job-work/out-by-id/{editId}`, hydrates header/dispatch/transfer state and maps `data.items` into `articlesList` (`:745-773`).

---

## 9. Page-in-page & hover actions

- **Cold Storage Stock Search** is a self-contained embedded sub-component (`ColdStorageStockSearch`, `:128-289`) rendered inside each cold-mode article (`:1519-1522`) — its own company switcher, debounced inputs, and results table.
- **Quick item search** renders an absolutely-positioned dropdown overlay (`z-50`) beneath the input (`:1620-1640`); closes on blur after 200 ms (`:1610`).
- **Cold Transfer Summary** is a modal `Dialog` (`:1942-1979`) that cannot be dismissed by outside click (`onOpenChange={() => {}}`, `onPointerDownOutside preventDefault`, `:1942/:1945`) — only the OK button closes it (and navigates).
- Hover affordances: title tooltips on truncated Box ID / Transaction No cells (`:1879-1880`), delete-row hover red bg (`:1894`), copy-button hover bg (`:1953`).

---

## 10. Keyboard / click directions

- Number inputs use `onWheel={e => e.currentTarget.blur()}` to prevent scroll-wheel value changes (`:1674`, `:1691`, `:1706`, `:1712`).
- Manual Box Entry keyboard flow: Enter in **Box Number** focuses the Transaction-No input (`#jw-manual-txn-input`, `:1804-1809`); Enter in **Transaction No** triggers `handleManualBoxFetch` (`:1820-1825`).
- Quick-search results use `onMouseDown`+`preventDefault` (`:1625`) so the click registers before the input's blur closes the dropdown.

---

## 11. Redirects

| Trigger | Destination |
|---|---|
| Back button (`:1278`) / Cancel (`:1931`) | `/{company}/transfer/job-work` |
| Successful submit, **non-cold** (`:1261`) | `/{company}/transfer/job-work/dc/{encodeURIComponent(challanNo)}` |
| Summary dialog **OK**, **cold** (`:1972`) | `/{company}/transfer/job-work/dc/{encodeURIComponent(challanNo)}` |

---

## 12. API calls

| Method | Endpoint | Params | Purpose |
|---|---|---|---|
| GET | `/inward/sku-dropdown` | `company`, `material_type?`, `item_category?`, `sub_category?`, `search?`, `limit` | Material type / category / sub-category options (via `dropdownApi.fetchDropdown`, `api.ts:641`) |
| GET | `/inward/sku-dropdown` (SKU resolve) | `company`, `item_description`, `item_category?`, `sub_category?`, `material_type?` | Resolve `sku_id` on item-description select (`fetchSkuId`, `:110`, `api.ts:685`) |
| GET | `/interunit/categorial-dropdown` | `material_type`, `item_category`, `sub_category`, `limit=500` | Item descriptions + UOM (`useCategorialItemDescriptions`, `useDropdownData.ts:540`) |
| GET | `/interunit/categorial-search` | `search`, `limit=200` | Quick item search (`:854`) |
| GET | `/cold-storage/stocks/search` | `company`, `lot_no?`, `q?` | Cold-storage stock search (`ColdStorageApiService.searchColdStorageStocks`, `coldStorageApiService.ts:79`) |
| GET | `/cold-storage/stocks/pick-boxes` | `company`, `item_description`, `lot_no`, `inward_no`, `qty` | FIFO unique per-box IDs for cold dispatch (`pickBoxes`, `coldStorageApiService.ts:100`) |
| GET | `/interunit/box-lookup/{company}` | `box_number`, `transaction_no` | Manual box fetch + legacy `TX`/`CONS` QR (`:529`, `:625`) |
| GET | `/interunit/box-lookup-by-id/{company}` | `box_id`, `transaction_no` | New-format QR lookup (`:610`) |
| GET | `/interunit/bulk-entry-box-lookup/{company}` | `box_id`, `transaction_no` | Bulk-entry (`BE…`) QR lookup (`:617`) |
| GET | `/job-work/out-by-id/{editId}` | path | Load record for edit (`:690`) |
| POST | `/job-work/out` | `?created_by={email}` + JSON payload | Create Material Out (`:1208-1212`) |
| PUT | `/job-work/out/{editId}` | `?created_by={email}` + JSON payload | Update Material Out (edit mode) (`:1207-1210`) |

Base URL: `process.env.NEXT_PUBLIC_API_URL` (fallback `http://localhost:8000`). Cold-storage service attaches Bearer token from `useAuthStore` (`coldStorageApiService.ts:19-29`); the inline `fetch` calls on this page send only `Accept`/`Content-Type` (no auth header).

---

## 13. Backend & DB wiring touched

Backend file: `d:\test\ims-app-backend\services\ims_service\job_work_server.py`.

- **POST `/out`** `submit_material_out` (`job_work_server.py:562-674`):
  - Inserts header into **`jb_materialout_header`** (`:574-616`) with `type='OUT'`, `status='sent'`, full `dispatch_to` JSON + raw `payload` JSON + `created_by`.
  - Inserts each line into **`jb_materialout_lines`** (`:624-661`) including `box_id`, `transaction_no`, `cold_unit`, `item_mark`.
  - Calls `_deduct_cold_storage_stock(db, header_id, line_items)` (`:665`, def `:372`): for every line with `box_id`+`transaction_no`+`cold_unit`, snapshots the cold row, **deletes** it from the resolved cold table, and writes a `cold_stock_disposition` audit row so a later Transfer-In scan can resolve the relabeled box. Soft-fails on audit-helper errors.
  - Fires `notify_job_work_material_out_created(...)` notification (`:670`).
- **PUT `/out/{record_id}`** `update_material_out` (`:681+`): 404 if missing; updates header (`:700-712`) and (further down) re-writes line items.
- `/job-work/out-by-id/{record_id}` (`:1125`) returns the record for the edit-mode loader.
- `cold-storage/stocks/search` and `/pick-boxes` live in `cold_storage_server.py`; the `box-lookup*`/`categorial-*` endpoints live in `interunit_server.py`.

---

## 14. Cross-module linkages

- **Job Work DC / print page**: success redirects to `/{company}/transfer/job-work/dc/{challanNo}` and pre-seeds `sessionStorage["jw-dc-{challanNo}"]` with the full payload (`:1225`).
- **Job Work index**: back/cancel return to `/{company}/transfer/job-work`.
- **Cold Storage module**: reads stock via `/cold-storage/stocks/*`; submit **deducts** cold inventory and writes `cold_stock_disposition` rows — directly coupled to the cold-storage and pending-transfer reconciliation flows.
- **Interunit / box inventory**: QR + manual box lookups resolve boxes created by interunit transfers/bulk-entry; `box_id`/`transaction_no` carried through tie this OUT document to those source boxes.
- **Transfer-In / Material-In**: backend comment (`job_work_server.py:376-378`) notes dispositions let a later Transfer-In scan treat a "missing" box as a legitimate fungible relabel from this OUT pool.
- **Auth**: `useAuthStore` provides `user.email` (→ `created_by`) and the Bearer token used by cold-storage calls.

---

## 15. Gotchas

- **Hardcoded company in payload**: the submitted `payload.company` is always *CANDOR DATES PRIVATE LIMITED* with a fixed GSTIN/FSSAI (`:1127-1135`), ignoring the URL `company`. Cold-storage searches/picks use the in-search CFPL/CDPL switcher (`cs_company`), explicitly **not** the URL company (comment `:962-963`).
- **Cold-storage per-box uniqueness is critical**: `handleAddToList` refuses to add cold items unless `pickBoxes` returns enough unique `box_id`s (`:977-985`). The comment (`:952-954`) ties this to a real incident (`TRANS202605131331`) where duplicating `cs_box_id` collapsed 700 boxes to 1 on receive. Each cold box becomes its **own** line entry (qty 1).
- **FG net-weight trap**: FG items silently mis-calculate net weight without a Unit Pack Size; add is blocked with a toast (`:943-946`). For PM, `unit_pack_size` changes are intentionally excluded from recalculation (`:821`).
- **Qty vs Boxes column ambiguity**: in the Added Items table the read-only "Qty" and editable "Boxes" cells both bind to `entry.quantity` (`:1881` vs `:1883`); footer sums quantity twice (`:1904-1905`).
- **Driver/authorized-person inputs absent**: payload carries them (`:1154-1155`) but there are no UI fields on this page; the `driverName === "other"` branch (`:1120`) can never fire here.
- **Net-weight semantics differ by source**: cold entries store **per-box** weight; non-cold stores the article's total net weight as one line — relevant when reading totals.
- **Summary dialog is modal-locked**: it can only be dismissed via OK, which always navigates to the DC page (`:1972`) — there is no way to stay on the form after a cold-storage submit.
- **No auth header on inline fetches**: page-level `fetch` calls (box lookup, submit, edit-load, quick search) send no Authorization header; only the cold-storage service helper attaches the Bearer token.
- **Challan number is client-generated and editable** (`generateChallanNo`, `:399`) — collisions/duplicates are not prevented client-side.
