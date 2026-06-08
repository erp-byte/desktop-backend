# Transfer OUT Form (Accept Request) — `/[company]/transfer/transferform`

| | |
|---|---|
| **File** | `d:\test\frontend-\app\[company]\transfer\transferform\page.tsx` (3053 lines) |
| **Component** | `NewTransferRequestPage` (default export, `page.tsx:264`) |
| **Route** | `/[company]/transfer/transferform` (e.g. `/CFPL/transfer/transferform?requestId=42`) |
| **Typical entry** | Launched from the Transfer dashboard "Accept"/"Create Transfer" action on an interunit *request*, passing `?requestId=<id>`. |
| **Purpose** | A transfer-**OUT** (dispatch) form. It pre-fills header + first line from an existing interunit request, lets the operator scan/add the physical boxes being shipped, capture transport details, then `POST`s a transfer to `/interunit/transfers`. The new transfer is created with **`Dispatch`** status (or **`Partial`** if fewer boxes than ordered qty are present) and the source stock is parked into `pending_transfer_stock` (in-transit). The originating request is flipped to **`Transferred`**. |

> Note: despite the component name `NewTransferRequestPage` and the page title heading "Transfer OUT", this page creates a **transfer**, not a request. The naming is legacy.

---

## 1. Route & params

- **`requestId` query param** — read at `page.tsx:272` via `searchParams.get('requestId')` into `requestIdFromUrl`. This is the **only** query param the page reads.
- **Prefill effect** — `loadRequestDetails()` in the `useEffect` at `page.tsx:482-634`, dependency `[requestIdFromUrl]`:
  - If **no `requestId`** (`page.tsx:484-489`): clears any stale persisted draft via `clearSavedData()` and removes `localStorage["draft-transfer-requestId"]`, then returns. The form opens blank (still usable as a manual transfer-out).
  - If `requestId` **differs** from the previously stored `draft-transfer-requestId` (`page.tsx:492-498`): resets `scannedBoxes`, `boxIdCounterRef` (→1), `loadedItems`, and clears the persisted draft — so switching requests doesn't carry boxes across.
  - Stores `requestId` to `localStorage["draft-transfer-requestId"]` (`page.tsx:499`).
  - Fetches the request: `InterunitApiService.getRequest(parseInt(requestIdFromUrl))` → `GET /interunit/requests/{id}` (`page.tsx:504`).
  - Sets `requestNo` from `request.request_no` (`page.tsx:507`).
  - Populates header `formData` (`page.tsx:519-526`): `requestDate`, `fromWarehouse`, `toWarehouse` (via `normalizeWarehouse` at `:510-516` — `"N/A"` → `""`), `reason` forced to `""` (user must re-pick), `reasonDescription` from `request.reason_description`.
  - Populates **only the first article (index 0)** from `request.lines[0]` (`page.tsx:529-597`): `material_type`, `item_category`, `sub_category`, `item_description` (each run through `normalizeField` at `:533-540` — trims, preserves case; `"N/A"`/null→`""`). Several case-normalizing helpers are declared (`toTitleCase`, `toCamelCase`, `normalizeCase` `:543-578`) but only `normalizeField` is actually applied to the loaded values.
  - Stores **all** request lines into `loadedItems` (`page.tsx:600-604`) with `scanned_count: 0` and `pending = quantity`. These render as a read-only "Items from Request" panel; only line 0 becomes the editable article.
  - Success toast "✅ Request Loaded & Auto-Filled" (`page.tsx:613-622`); error toast on failure (`page.tsx:623-630`).
- **SKU resolution after prefill** — a second `useEffect` (`page.tsx:637-669`) auto-fetches the SKU id for the auto-filled article once material_type/category/sub_category/description are set and `sku_id` is null, via `dropdownApi.fetchSkuId(...)`.
- **Auth gating** — **No explicit gate on this page.** `user` is read from `useAuthStore()` (`page.tsx:269`) only to stamp the dispatcher name on submit. There is no role/login redirect inside the component; access control is assumed to be handled by the surrounding `[company]` layout. The API client (`interunitApiService.ts:80-90`) attaches the bearer token from the auth store to every call.

---

## 2. Layout & structure

Single scrolling page wrapped in one `<form onSubmit={handleSubmit}>` (`page.tsx:1781`, closes `:3049`). Top-to-bottom:

1. **Header bar** (`page.tsx:1784-1801`) — back arrow button → `/[company]/transfer`, title "Transfer OUT" (Send icon), and the auto-generated `transferNo` shown as subtext.
2. **Request Header card** (`page.tsx:1803-1968`) — Request No (read-only), Request Date, From/To warehouse, Reason, Reason Description.
3. **Scan QR Code card** (`page.tsx:1971-2071`) — camera scanner toggle + "OR" divider + Manual Box Entry (box number + transaction no).
4. **Transfer Information card** (`page.tsx:2074-2158`) — Vehicle Number, Driver Name, Approval Authority.
5. **Article Management section** (`page.tsx:2159-2466`) — "Add Article" button + one editable card per article (quick search, MaterialType→Category→SubCategory→Description dropdowns, pack/qty/weight fields, "Add to Articles List").
6. **Items from Request panel** (`page.tsx:2469-2588`) — read-only, rendered only when `loadedItems.length > 0`; shows all request lines with scanned/pending counters.
7. **Scanned Boxes / Articles list card** (`page.tsx:2591-2910`) — table (desktop) / cards (mobile) of `scannedBoxes`, inline-editable weights, totals summary, wrapped in `BoxScrollContainer`.
8. **Weight Comparison card** (`page.tsx:2912-2960`) — requested vs actual net weight (only when `loadedItems.length > 0`).
9. **Validation Errors card** (`page.tsx:2962-2985`) — list of blocking errors (only when present).
10. **Submit section** (`page.tsx:2987-3010`) — Cancel + "Submit Transfer", with a note that status will be `Dispatch`.
11. **Cold Transfer Summary dialog** (`page.tsx:3015-3047`) — modal shown post-submit when a cold warehouse is involved.

Styling: Tailwind cards (`Card`/`CardHeader`/`CardContent`), `bg-gray-50 min-h-screen` page background, responsive grids.

---

## 3. Form fields

### Header fields (`formData` state, `page.tsx:297-303`)

| Field | Type | Required / Validation | Default | Source |
|---|---|---|---|---|
| Request No | read-only `Input` (`:1817-1823`) | Required at submit (`:1540-1542` "Request number is required") | `""` | `requestNo` state; set from `request.request_no` on prefill (`:507`) or QR (`:1423`). Not user-editable. |
| Request Date | text `Input` DD-MM-YYYY (`:1829-1836`) | Marked `*`; not enforced in validation | today `DD-MM-YYYY` (`:293`); reset to today on mount (`:414-418`) | `formData.requestDate`; overwritten by `request.request_date` on prefill (`:520`). |
| From (Requesting Warehouse) | `Select` (`:1843-1860`) | Required + must differ from To (`:1544-1554`) | `""`; prefilled from `request.from_warehouse` | Hardcoded options. |
| To (Supplying Warehouse) | `Select` (`:1876-1912`) | Required + must differ from From (`:1548-1554`) | `""`; prefilled from `request.to_warehouse` | Hardcoded options. |
| Reason | `Select` (`:1921-1936`) | Required (`:1556-1558`) | `""` (forced blank on prefill, `:523`) | Hardcoded reason codes. |
| Reason Description | `Textarea` (`:1949-1961`) | Required, non-empty trim (`:1560-1562`) | `""`; prefilled from `request.reason_description` (`:524`) | User input. |
| Transfer No | header subtext only (`:1799`) | n/a (auto) | `generateTransferNo()` = `TRANS<YYYYMMDDHHMM>` (`:275-283`, `:289`) | Auto-generated; sent as `challan_no`. |

### Transport fields (`transferInfo` state, `page.tsx:358-365`)

| Field | Type | Required / Validation | Default | Source |
|---|---|---|---|---|
| Vehicle Number | `Select` (`:2088-2101`) + conditional "Other" `Input` (`:2102-2110`) | Required (`:1590-1592`); if `"other"`, the Other text required (`:1594-1596`) | `""` | Hardcoded vehicles + "Other". |
| Driver Name | `Select` (`:2118-2131`) + conditional "Other" `Input` (`:2132-2140`) | Required (`:1598-1600`); if `"other"`, Other text required (`:1602-1604`) | `""` | Hardcoded drivers + "Other". |
| Approval Authority | free-text `Input` (`:2148-2154`) bound to `approvalAuthorityOther` | Required, non-empty trim (`:1606-1608`) | `""` | User input (no dropdown — always the "Other" text field). |

### Per-article line fields (`Article` interface `page.tsx:306-329`; rendered `:2260-2450`)

| Field | Type | Required / Validation | Default | Source |
|---|---|---|---|---|
| Material Type | `MaterialTypeDropdown` (`:2264-2284`) | Required per article (`:1570-1572`) | `""` (prefilled line 0) | `dropdownApi.fetchDropdown` `material_types`, fallback RM/PM/FG (`:55-76`). |
| Item Category | `ItemCategoryDropdown` (`:2290-2311`) | Required (`:1574-1576`) | `""` | `useItemCategories` (categorial). Disabled until material_type set. |
| Sub Category | `SubCategoryDropdown` (`:2317-2339`) | Required (`:1578-1580`) | `""` | `useSubCategories`. Disabled until category set. |
| Item Description | `ItemDescriptionDropdown` (`:2345-2355`) | Required (`:1582-1584`) | `""` | `useCategorialItemDescriptions`; selecting auto-fills `unit_pack_size` (uom) + fetches `sku_id` (`:199-247`). |
| Unit Pack Size/Count | number `Input` (`:2361-2370`) | FG must be > 0 to "Add to List" (`:861-868`); not enforced at submit | `0` | Auto-filled from item uom on selection; editable. |
| UOM | `Select` (`:2376-2390`) | Not required | `""` | Hardcoded KG/PCS/BOX/BAG/CARTON. |
| Case Pack/Box Wt. | number `Input` (`pack_size`) (`:2396-2405`) | Not required | `0` | User input; drives net-weight calc. |
| Quantity (Box/Bags) | number `Input` (`quantity_units`) (`:2411-2419`) | Not required (defaults 1) | `1` | User input; = number of boxes generated by "Add to List". |
| Net Weight (Kg) | number `Input` (`:2425-2434`) | Not required | `0` (auto via `calculateNetWeight`) | Auto-calculated on qty/pack/ups/material change (`:767-772`), editable. |
| Lot Number (Optional) | text `Input` (`:2442-2448`) | Optional | `""` | User input. |

**Net weight calc** (`calculateNetWeight`, `page.tsx:725-738`): FG → `(unit_pack_size × pack_size) × quantity`; RM/PM/RTV → `quantity × pack_size`.

**Cascade clearing** (`updateArticle`, `page.tsx:740-778`): changing material_type clears category/sub/description/sku + resets unit_pack_size; changing category clears sub/description/sku; changing sub clears description/sku. `total_amount` recomputed on unit_rate/qty change. PM items skip net-weight recalc when unit_pack_size changes (`:769`).

### Inline-editable scanned-box fields (`scannedBoxes`, via `updateScannedBox` `page.tsx:990-1001`)

Editable in the Scanned Boxes table: **Case Pack** (`packagingType`), **Unit Pack Size/Count** (`packageSize`), **Net Wt** (`netWeight`), **Total Wt** (`totalWeight`). Changing Case Pack auto-recomputes `netWeight = casePack × packageSize` (`:994-998`).

---

## 4. Box scanning / QR

Yes — two ingestion paths plus a camera scanner.

**Camera scanner** (`HighPerformanceQRScanner`, `page.tsx:1998-2002`; component `d:\test\frontend-\components\transfer\high-performance-qr-scanner.tsx`): uses native `BarcodeDetector` with a `qr-scanner` library fallback, 2-second per-value cooldown (`SCAN_COOLDOWN_MS`), and an embedded manual-entry fallback. Toggled by `showScanner` (`:392`, started via "Start Camera Scan" `:1984-1990`). On success it calls `handleQRScanSuccess`; the scanner auto-closes after a successful scan (`:1106`).

**`handleQRScanSuccess`** (`page.tsx:1097-1496`) — the core scan handler:
- Guarded by `isProcessingRef` (`:1099-1103`) to dedupe rapid scans; reset after 500ms (`:1492-1494`).
- Parses `decodedText` as JSON. Detects format by keys:
  - **Bulk Entry QR** `{"tx":"BE-…","bi":…}` → lookup `GET /interunit/bulk-entry-box-lookup/{company}?box_id=&transaction_no=` (`:1201-1238`).
  - **New transfer QR** `{"tx":"TR-…","bi":…}` (not BE) → lookup `GET /interunit/box-lookup-by-id/{company}?box_id=&transaction_no=` (`:1156-1199`).
  - **Old TX/CONS** transaction → `GET /inward/{company}/{transactionNo}`, then matches box/article in the response by transaction_no+box_number, sku_id, item_description (`:1240-1328`).
- **Duplicate detection** (`:1129-1151`): new/bulk format uses `transactionNo + boxId`; old format uses `transactionNo + skuId + boxNumber`. Duplicate → `alert()` + destructive toast, abort.
- Each accepted scan pushes a normalized box object onto `scannedBoxes` with a unique `id`/`boxNumber` from `boxIdCounterRef` (`:1330-1363`), and updates `loadedItems` scanned/pending counters by sku/description match (`:1400-1415`). Scanned-vs-request-qty toasts at `:1373-1397`.
- **Non-box request QR** (`:1420-1442`): if the QR carries `request_no`/`from_warehouse`/`to_warehouse`/`item_description`/`quantity`, those header/article fields are auto-filled instead.
- **Plain-text box id** starting `CONS`/`TR` (`:1447-1483`): added as a bare box row with placeholder fields.
- `handleQRScanError` (`:1498-1505`) → destructive toast.

**Manual Box Entry** (`handleManualBoxFetch`, `page.tsx:1004-1095`): requires Box Number + Transaction No (`:1005-1012`); dedupes on `boxNumberInArray + transactionNo` (`:1014-1025`); calls `GET /interunit/box-lookup/{company}?box_number=&transaction_no=` (`:1029`); on success appends a normalized box and clears inputs.

**Box id / transaction-no handling**: each scanned/manual box stores `boxId`, `transactionNo`, `boxNumberInArray`. On submit (`:1669-1682`) these become the box payload's `box_id` / `transaction_no` (`"DIRECT"` for boxes created via "Add to Articles List", `:889`). The backend uses `(box_id, transaction_no)` to find and deduct the source row and as the `pending_transfer_stock` conflict key.

**Remove / clear**: `handleRemoveBox` (`:939-986`, decrements counters), per-row X buttons (`:2674`, `:2857`), and "Clear All" (`:2634`, resets boxes + counter).

---

## 5. Dropdowns & data sources

| Dropdown | Source | Notes |
|---|---|---|
| Material Type | `MaterialTypeDropdown` (`:32-101`) → `dropdownApi.fetchDropdown({company, limit:1000})` → `GET /inward/sku-dropdown`, reads `options.material_types`; fallback RM/PM/FG. | Per-article. |
| Item Category | `useItemCategories` (`useDropdownData.ts:71-117`) → `fetchDropdown` `options.item_categories`. | Depends on material_type. Fallback list `:430-434`. |
| Sub Category | `useSubCategories` (`useDropdownData.ts:123+`) → `fetchDropdown` `options.sub_categories`. | Depends on material_type + category. |
| Item Description | `useCategorialItemDescriptions` (`useDropdownData.ts:512-563`) → `GET /interunit/categorial-dropdown`. | Carries per-item `uom` → auto-fills unit_pack_size; selection triggers SKU fetch. |
| Quick Search Item | `GET /interunit/categorial-search?search=&limit=200` (`page.tsx:797`) | Direct fetch, 300ms debounce; min 2 chars; fills all classification fields + sku + unit_pack_size (`:812-839`). |
| UOM | hardcoded `Select` (`:2384-2388`): KG/PCS/BOX/BAG/CARTON. | — |
| From Warehouse | hardcoded (`:1851-1856`): W202, A185, A101, A68, F53, Cold Storage. | — |
| To Warehouse | hardcoded (`:1892-1908`): W202, A185, A101, A68, F53, Rishi, Savla D-39, Savla D-514, Supreme. | Cold sub-units only appear as destinations. |
| Reason | hardcoded (`:1929-1934`): Stock Requirement, Material Movement, Production Need, Customer Order, Inventory Balancing, Other. | — |
| Vehicle Number | hardcoded (`:2096-2099`): MH43BP6885, MH43BX1881, MH46BM5987 (Contract Vehicle), Other. | — |
| Driver Name | hardcoded (`:2126-2129`): Tukaram (+919930056340), Sachin (8692885298), Gopal (+919975887148), Other. | A separate `getDriverPhone` map (`:1771-1779`, Tukaram/Sayaji/Prashant/Shantilal) exists but is unused in submit. |
| Approval Authority | free text (no dropdown). | — |

`COLD_STORAGE_WAREHOUSES` (`page.tsx:374`) = `["Cold Storage","Rishi","Savla D-39","Savla D-514","Supreme"]` — used to trigger the post-submit cold summary popup.

---

## 6. Buttons

| Label | Line | Handler | Action / Redirect |
|---|---|---|---|
| Back arrow | `:1785-1793` | inline | `router.push('/[company]/transfer')` |
| 📷 Start Camera Scan | `:1984-1990` | `setShowScanner(true)` | Opens `HighPerformanceQRScanner` |
| Fetch Box | `:2053-2064` | `handleManualBoxFetch` | Box-lookup API → append scanned box |
| Add Article | `:2169-2172` | `addArticle` (`:680-706`) | Appends a blank article card |
| Trash (remove article) | `:2189-2196` | `removeArticle(id)` (`:708-722`) | Removes article (min 1 enforced) |
| Add to Articles List | `:2454-2461` | `handleAddArticleToList(article)` (`:850-936`) | Generates `quantity_units` box rows (transaction_no `"DIRECT"`) into `scannedBoxes`; updates counters |
| Clear All | `:2629-2639` | inline | Empties `scannedBoxes`, resets `boxIdCounterRef` |
| X (remove box, mobile) | `:2670-2678` | `handleRemoveBox(box.id)` | Removes that box |
| X (remove box, desktop) | `:2853-2861` | `handleRemoveBox(box.id)` | Removes that box |
| Cancel | `:2995-3000` | `router.back()` | Browser back |
| Submit Transfer | `:3001-3006` | form `onSubmit` → `handleSubmit` (`:1532`) | Validate → `POST /interunit/transfers` |
| Cold popup copy | `:3022-3032` | inline | Copies summary text to clipboard |
| Cold popup OK | `:3036-3043` | inline | Closes popup → `router.push('/[company]/transfer')` |
| Go / pagination (in BoxScrollContainer) | component | `goTo` | Scrolls to a box by # or lot; pagination unused here (single page) |

---

## 7. Submit / create-transfer flow

`handleSubmit` (`page.tsx:1532-1768`):

1. **Validation** (`:1537-1633`) — collects all errors into `validationErrors`: requestNo, from/to warehouse (required + must differ), reason, reasonDescription, each article's material_type/category/sub_category/description, vehicle (+Other), driver (+Other), approval authority. **Box-count validation is commented out** (`:1610-1619`) — you can submit with zero scanned boxes. On error: toast + `window.scrollTo(bottom)` and abort.
2. **Payload build** (`:1645-1684`):
   - `header`: `challan_no = transferNo`, `stock_trf_date = formData.requestDate`, from/to, `vehicle_no` (resolves Other), `driver_name`, `approved_by` (= approval authority or null), `remark = reasonDescription || reason`, `reason_code = reason`.
   - `lines`: from `articles` — material/category/sub/description, `quantity`, `uom`, `pack_size`, `unit_pack_size` (or null), `batch_number`/`lot_number` sent as **null** (line-level lot is dropped here). Note: `net_weight`/`total_weight` are **not** in the line payload from this page, so the backend recalculates per-line weight (see below).
   - `boxes`: from `scannedBoxes` — `box_number`, `box_id`, `article`, `lot_number`, `batch_number`, `transaction_no`, `net_weight`, `gross_weight` (3-dp strings).
   - `request_id`: `parseInt(requestIdFromUrl)` or null (`:1683`).
3. **API call** (`:1700`): `InterunitApiService.submitTransfer(company, payload, user?.name||user?.email||'unknown')` → `POST /interunit/transfers?created_by=<email>` (`interunitApiService.ts:382-404`).
4. **Success** (`:1703-1754`): success toast; `clearSavedData()` + remove `draft-transfer-requestId`.
   - **If a cold warehouse is involved** (`isColdInvolved`, `:1716`): build a per-item summary (from scannedBoxes if any, else articles) with Item Mark / No of Boxes / Lot, plus From/To and Vehicle, and open the **Cold Transfer Summary** dialog (`setColdTransferPopup`, `:1748`). Redirect happens only when the operator clicks OK.
   - **Otherwise** (`:1750-1753`): `setTimeout(() => router.push('/[company]/transfer'), 1500)`.
5. **Failure** (`:1756-1767`): logs + destructive toast.

**Backend status logic** (`create_transfer`, `interunit_tools.py:818-1100`):
- Header inserted with `status='Dispatch'` (`:832`).
- Lines inserted; per-line `net_weight` uses frontend value if provided else recomputes (FG vs RM/PM) (`:858-909`).
- If no boxes and source is a warehouse, the backend tries `_auto_derive_warehouse_boxes` (FIFO) (`:921-929`); failures fall through to line-level pending.
- Boxes inserted into `interunit_transfer_boxes`, with duplicate `(box_id, transaction_no)` rejected (`:934-954`) and cold-source double-dispatch guarded (`:957`).
- **`park_in_pending`** (`:1002-1011`) parks each box into `pending_transfer_stock` (status `In Transit`), deducting the source row.
- **Status recompute** (`:1056-1068`): if boxes exist, `total_expected = Σ line.qty`; `status = "Dispatch" if len(boxes) >= total_expected else "Partial"`. (Box-less transfers stay `Dispatch`.)
- **Request flip** (`:1071-1079`): if `request_id`, `interunit_transfer_requests.status → 'Transferred'`.
- `reconcile_transfer_to_order` (`:1083`) records any ordered-vs-shipped gap on the header (flag-only).

So the "Dispatch/Partial" implication: a fully-scanned/box-derived transfer dispatches as `Dispatch`; a short-shipped one (fewer boxes than ordered qty) is saved as `Partial`. The UI's submit note always says "Dispatch" (`:2992`) and does not reflect the Partial possibility.

---

## 8. Page-in-page & hover actions

- **Cold Transfer Summary Dialog** (`page.tsx:3015-3047`) — a modal "page-in-page" shown after a successful cold-involved submit. Non-dismissible by outside click (`onPointerDownOutside` prevented, `onOpenChange` no-op); contains the copyable summary and an OK button that closes + redirects.
- **Quick Search dropdown** (`:2228-2257`) — a floating result list under each article's search box; items are buttons (`onMouseDown` → `handleItemSelect`).
- **Hover actions**: scanned-box table rows highlight on hover (`hover:bg-gray-50`, `:2777`); truncated cells use `title=` tooltips for item description (`:2782`), category (`:2790`), and the loaded-items summary (`:2617`). The Weight Comparison card (`:2912-2960`) is a derived inline panel (not a popup). No row-level context menus.

---

## 9. Keyboard / click directions

- **Manual Box Entry — Enter chaining**: Enter in Box Number focuses the Transaction No input (`:2029-2034`); Enter in Transaction No triggers `handleManualBoxFetch` (`:2045-2050`).
- **Number inputs — wheel guard**: `onWheel={(e) => e.currentTarget.blur()}` on all numeric inputs (pack size, qty, net weight, scanned-box weight cells) to stop scroll-changes-value (`:2368`, `:2403`, `:2417`, `:2432`, and box-table inputs `:2699+`/`:2801+`).
- **Quick Search focus/blur**: focus reopens results if present (`:2214-2218`); blur closes after 200ms so the click registers (`:2219-2221`).
- **BoxScrollContainer "Go"** (`BoxScrollContainer.tsx:152-166`): Enter in the box/lot search field scrolls to and focuses the matching box's first editable input.
- **Scanner cooldown**: the QR scanner enforces a 2s same-value cooldown and the page enforces `isProcessingRef` dedupe so a held QR isn't double-added.

---

## 10. Redirects

| Trigger | Destination |
|---|---|
| Back arrow (`:1789`) | `/[company]/transfer` |
| Cancel (`:2998`) | `router.back()` |
| Successful submit, **no cold** (`:1751-1753`) | `/[company]/transfer` (after 1.5s) |
| Successful submit, **cold involved** | stays; redirects to `/[company]/transfer` only on dialog **OK** (`:3040`) |
| No `requestId` on load | no redirect — opens blank form |

---

## 11. API calls

| Method | Endpoint | Params / Body | Purpose | Resolver |
|---|---|---|---|---|
| GET | `/interunit/requests/{id}` | path id | Load request to prefill (header + lines) | `InterunitApiService.getRequest` (`interunitApiService.ts:318-320`), called `page.tsx:504` |
| GET | `/inward/sku-dropdown` | `company, material_type, item_category, sub_category, search, limit` | Material type / category / sub-category options | `dropdownApi.fetchDropdown` (`api.ts:617-683`); via `MaterialTypeDropdown`, `useItemCategories`, `useSubCategories` |
| GET | `/interunit/categorial-dropdown` | `material_type, item_category, sub_category` | Item description options (categorial_inv, carries uom) | `useCategorialItemDescriptions` (`useDropdownData.ts:512-563`) |
| GET | `/inward/sku-id` | `company, item_description, item_category, sub_category, material_type` | Resolve `sku_id` (+ uom) for a chosen item | `dropdownApi.fetchSkuId` (`api.ts:685+`), called `:215`, `:646` |
| GET | `/interunit/categorial-search` | `search, limit=200` | Quick-search items → auto-fill article | direct `fetch` (`page.tsx:797`) |
| GET | `/interunit/box-lookup/{company}` | `box_number, transaction_no` | Manual box fetch | direct `fetch` (`page.tsx:1029`) |
| GET | `/interunit/box-lookup-by-id/{company}` | `box_id, transaction_no` | New `TR-` QR box lookup | direct `fetch` (`page.tsx:1158`) |
| GET | `/interunit/bulk-entry-box-lookup/{company}` | `box_id, transaction_no` | `BE-` QR box lookup (bulk_entry_boxes) | direct `fetch` (`page.tsx:1204`) |
| GET | `/inward/{company}/{transaction_no}` | path | Old `TX`/`CONS` QR transaction lookup | direct `fetch` (`page.tsx:1242`) |
| POST | `/interunit/transfers?created_by=<email>` | `{header, lines[], boxes[], request_id}` | **Create the transfer-OUT** | `InterunitApiService.submitTransfer` (`interunitApiService.ts:382-404`), called `page.tsx:1700`; backend `create_transfer_endpoint` (`interunit_server.py:239-245`) → `create_transfer` (`interunit_tools.py:818`) |

All `InterunitApiService` calls attach the bearer token via `getAuthHeaders` (`interunitApiService.ts:80-90`). The raw `fetch` calls (box lookups, quick search) do **not** send auth headers.

---

## 12. Backend & DB wiring touched

Driven by `create_transfer` (`interunit_tools.py:818-1100`) and `park_in_pending` (`pending_stock_tools.py:1096+`):

- **`interunit_transfers_header`** — one row inserted (`:823-852`): challan_no, stock_trf_date, from_site, to_site, vehicle_no, driver_name, approved_by, remark, reason_code, `status` (initially `'Dispatch'`, later updated to `Dispatch`/`Partial`), request_id, created_by. For cold-source transfers, `from_cold_unit` is set to the canonical sub-cold list (`:1020-1038`).
- **`interunit_transfers_lines`** — one row per article (`:878-909`): rm_pm_fg_type, item_category, sub_category, item_desc_raw, pack_size, qty, uom, unit_pack_size, net_weight, total_weight, batch_number, lot_number. (Lot/batch arrive null from this page.)
- **`interunit_transfer_boxes`** — one row per scanned/derived box (`:969-996`): header_id, transfer_line_id (article-matched), box_number, box_id, article, lot_number, batch_number, transaction_no, net_weight, gross_weight. Duplicate `(box_id, transaction_no)` rejected (HTTP 400).
- **`pending_transfer_stock`** — in-transit parking (`park_in_pending`, `pending_stock_tools.py:1170-1192`): inserts a row per box (`status='In Transit'`, conflict key `(box_id, transaction_no)`) capturing source_table/source_row_id/destination_table, item/lot/batch/weights, and a `cold_storage_data` JSONB snapshot for cold sources. Box-less transfers use `park_lines_in_pending` (`:1278+`) which is **tracking-only** (no inventory move).
- **Source-inventory deduction**:
  - **Cold source** → `_find_in_cold_stocks` locates the matching **`cold_stocks`** row (by box_id + transaction_no + lot) and that quantity is moved to in-transit (`pending_stock_tools.py:1131-1146`).
  - **Warehouse source** → `_find_in_bulk_entry` locates the **`bulk_entry_boxes`** row and deducts it (`:1147-1164`).
- **`interunit_transfer_requests`** — when `request_id` present, `status → 'Transferred'` (`:1071-1079`). This is what closes out the accepted request.
- **Reconcile** — `reconcile_transfer_to_order` (`:1083`) compares ordered (lines) vs parked (boxes) and records the gap on the header (flag-only; does not move cold_stocks).

Note: actual destination stock (cold_stocks / bulk_entry_boxes at the receiving site) is **not** written here — that happens on Transfer-IN finalize, when the parked in-transit rows are consumed.

---

## 13. Cross-module linkages

- **Transfer dashboard** (`/[company]/transfer`) — the entry point that launches this form with `?requestId`, and the redirect target after submit. Documented in `01-transfer-dashboard.md`.
- **Interunit requests** — consumes a request (`GET /interunit/requests/{id}`) and, on success, flips it to `Transferred`. Created by the request-creation form (sibling page).
- **Transfer IN** (`02-transfer-in.md`) — the downstream module that receives/acknowledges the `pending_transfer_stock` rows this page parks; reconciliation (`ReconciliationInfo` / STBR types in `interunitApiService.ts:6-78`) consumes the box_id/transaction_no this form writes.
- **Inward module** — old `TX`/`CONS` QR scans read inward transactions via `/inward/{company}/{transaction_no}`; SKU/category dropdowns hit `/inward/sku-dropdown` and `/inward/sku-id`.
- **Bulk entry** — `BE-` QRs and warehouse-source deductions read `bulk_entry_boxes` (`/interunit/bulk-entry-box-lookup`).
- **Cold storage** — cold-source deductions read `cold_stocks`; cold involvement triggers the summary popup and `from_cold_unit` tagging used by the dashboard's cold sub-unit chip filter.
- **Shared components**: `HighPerformanceQRScanner` (transfer), `BoxScrollContainer` (inward module — `components/modules/inward/`), `useFormPersistence`, `useDropdownData`, `SearchableSelect`.

---

## 14. Gotchas

1. **Only line 0 is editable.** Even when a request has multiple lines, only `request.lines[0]` becomes the editable article (`:529-597`); the rest live read-only in the "Items from Request" panel. The note at `:2581-2585` claims "all items will be included," but the submit payload's `lines` come only from the editable `articles` array — extra request lines are **not** auto-added unless the operator re-adds them. This is a real divergence to watch.
2. **Box scanning is not enforced.** The "scan at least one box" and "every box must have SKU" checks are commented out (`:1610-1619`), so a transfer can be submitted with zero boxes. The backend then either FIFO-derives boxes (warehouse) or parks line-level tracking rows only.
3. **Submit note always says "Dispatch"** (`:2992`) but the backend may persist **`Partial`** when fewer boxes than ordered qty are present (`interunit_tools.py:1056-1068`). The UI does not surface the Partial outcome.
4. **Line-level lot/batch are dropped** in the submit payload (`batch_number: null, lot_number: null`, `:1666-1667`) even though the article form has a Lot Number field — lot only travels via scanned-box rows. The cold summary popup does read article lot, but the DB line gets null.
5. **`reason` is reset to blank on prefill** (`:523`) even if the request had one — the operator must re-pick a Reason or validation blocks submit.
6. **localStorage draft persistence** (`useFormPersistence`, key `draft-transfer`) restores formData/articles/boxes/etc. across refreshes. Switching `requestId` clears it, but opening the form **without** a `requestId` after a prior session also clears it (`:484-489`). The requestDate is force-reset to today on mount (`:414-418`) to avoid a stale cached date.
7. **Duplicate-detection keys differ by QR format** (`:1129-1137`): new/bulk use `transactionNo+boxId`; old use `transactionNo+skuId+boxNumber`. A box scanned in both old and new formats could slip past dedupe.
8. **"Add to Articles List" boxes use `transaction_no = "DIRECT"`** (`:889`), which `park_in_pending` explicitly skips for source deduction (`pending_stock_tools.py:1123`) — those represent quantity-only entries, not real physical scanned boxes.
9. **Approval Authority is free text**, bound to `approvalAuthorityOther` (`:2150`) — there is never a real dropdown despite the "Other" naming.
10. **`getDriverPhone` map is dead code** (`:1771-1779`); driver phone is embedded in the dropdown label string instead, and the raw label (incl. phone) is what's submitted as `driver_name`.
11. **Raw `fetch` lookups skip auth headers** (box lookups, quick search) while `InterunitApiService` calls include the bearer token — an inconsistency if those endpoints later require auth.
