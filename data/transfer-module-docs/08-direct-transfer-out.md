# Direct Transfer OUT (form) — `/[company]/transfer/directtransferform`

**File:** `frontend-/app/[company]/transfer/directtransferform/page.tsx` (4135 lines, `"use client"`)
**URL:** `/CFPL/transfer/directtransferform` — create mode. Edit mode via `?editId=<transferId>`. (Also tolerates a legacy `?requestId=<id>` prefill path, see §2.)
**Component:** `NewTransferRequestPage({ params: { company } })` (`:815`). The function name is historical — this is the **Transfer OUT** form, titled "Transfer OUT" / "Edit Transfer OUT" (`:2763`).
**API clients:** `frontend-/lib/interunitApiService.ts` (`InterunitApiService`), `frontend-/lib/api/coldStorageApiService.ts` (`ColdStorageApiService`), plus raw `fetch` to several `/interunit/*` and `/inward/*` endpoints, and `dropdownApi` from `@/lib/api`.

> Scope: the primary form to **create a Transfer OUT directly** (no prior REQ request) and to **edit** an existing one. Submits with status **Dispatch** (or **Partial** server-side). On submit the source stock (cold or warehouse) is deducted and the boxes are parked into `pending_transfer_stock` as "In Transit". The receive side (`transferIn`) is a separate doc.

---

## 1. Route & params

- **Component / hooks** (`:815-820`): `useRouter`, `useSearchParams`, `useToast`, `useAuthStore` (`user` used as `created_by`).
- **`?editId`** (`:826-827`): `editIdFromUrl = searchParams.get('editId')`; `isEditMode = !!editIdFromUrl`. Drives the edit-prefill effect, the persistence key, the heading, the submit verb, and the button label.
- **`?requestId`** (`:823`): `requestIdFromUrl = searchParams.get('requestId')`. Legacy/secondary path — prefills header + first article from an existing REQ request (`:1056-1193`). When set, the first article's Material Type / Category / Sub-category / Description are **locked** ("🔒 Loaded from request", `:3405-3407, :3435-3437, :3466-3468, :3485-3487`). Direct-OUT navigation normally omits this, so the request panel and locks are usually absent. `request_id` is hard-coded to `null` in the submit payload regardless (`:2640`).
- **Create vs edit:**
  - **Create:** generates `transferNo` client-side via `generateTransferNo()` → `TRANS<YYYYMMDDHHMM>` (`:831-845`); `requestDate` = today `DD-MM-YYYY` (`:848-849`, forced back to today on mount at `:986-992`).
  - **Edit:** `loadTransferForEdit()` (`:1196-1355`) fetches `getTransferById(company, editId)` and prefills header, transfer info, first article, and the box list (see §7). Shows a full-card loading overlay while `editLoading` (`:2770-2775`).
- **Auth gating:** none on this page. Any authenticated user reaching the route can create/edit. `user?.name || user?.email || 'unknown'` is passed as `created_by` on create only (`:2665`). (Backend `PUT` does not take `created_by`.) Delete-permission gating exists server-side but is not used by this form.

---

## 2. Layout & structure

Single scrolling `<form onSubmit={handleSubmit}>` (`:2747`) wrapping a vertical stack inside `p-3..6 space-y-4` (`:2748`). Top-to-bottom:

1. **Header bar** (`:2750-2767`): back button → `/${company}/transfer`; title "Transfer OUT" / "Edit Transfer OUT" with `Send` icon; subtitle "Transfer No: `{transferNo}`".
2. **Edit loading overlay** (`:2770-2775`): spinner shown while `editLoading`.
3. **Request Header card** (`:2778-2942`): Request No (read-only), Manual Transfer Date (read-only), From / To warehouse selects, Reason select, Reason Description textarea. See §3.
4. **Scan QR Code card** (`:2945-3045`): "Start Camera Scan" → `HighPerformanceQRScanner`; OR divider; **Manual Box Entry** (Box Number + Transaction No + Fetch Box). See §4.
5. **Transfer Information card** (`:3048-3132`): Vehicle Number, Driver Name, Approval Authority. See §3.
6. **Article Management** section (`:3133-3629`): "Add Article" button + one editable block per article. Each block is **either** the Cold Storage stock-search form **or** the regular manual-entry form (toggled per-article). See §4/§5.
7. **Items from Request** card (`:3632-3747`): only when `loadedItems.length > 0` (i.e. `?requestId` path). Read-only summary of all request lines.
8. **Scanned Boxes / Articles list** card (`:3750-4066`): the consolidated line/box list (mobile cards + desktop table), inline-editable, with a summary footer. Wrapped in `BoxScrollContainer` (`:3802`). See §8.
9. **Submit card** (`:4069-4092`): "Transfer will be submitted with **Dispatch** status" + Cancel + Submit/Update.
10. **Cold Transfer Summary dialog** (`:4098-4129`): modal shown post-submit when cold storage is involved. See §8/§7.

---

## 3. Form fields

### Header (`formData`, initial `:853-859`) + transfer info (`transferInfo`, `:930-937`)

| Field | Type | Required / Validation | Default | Source |
|---|---|---|---|---|
| Request No | read-only `Input` (`:2792-2798`) | none (informational) | `""`; set from REQ (`:1066`) or edited transfer's `request_no` (`:1208`) | `requestNo` state |
| Manual Transfer Date | read-only `Input` (`:2804-2810`) | always today | `currentDate` = today `DD-MM-YYYY` (`:849`); reset on mount (`:986-992`) | `formData.requestDate` |
| From (Requesting Warehouse) * | `Select` (`:2817-2834`) | required (`:2483`); must differ from To (`:2491`) | `""` | static list (§5) |
| To (Supplying Warehouse) * | `Select` (`:2850-2886`) | required (`:2487`); must differ from From | `""` | static list (§5) |
| Reason * | `Select` (`:2895-2910`) | required & non-blank (`:2495`) | `""` | static list (§5) |
| Reason Description * | `Textarea` (`:2923-2935`) | required & non-blank (`:2499`) | `""` | free text |
| Vehicle Number * | `Select` (`:3062-3075`) | required (`:2518`); if "other", `vehicleNumberOther` required (`:2522`) | `""` | static list (§5) |
| Vehicle Number (Other) | `Input`, shown when `=== 'other'` (`:3076-3084`) | required only when "other" selected | `""` | free text |
| Driver Name * | `Select` (`:3092-3105`) | required (`:2526`); if "other", `driverNameOther` required (`:2530`) | `""` | static list (§5) |
| Driver Name (Other) | `Input`, shown when `=== 'other'` (`:3106-3114`) | required only when "other" selected | `""` | free text |
| Approval Authority * | free-text `Input` → `approvalAuthorityOther` (`:3122-3128`) | required & non-blank (`:2534`) | `""` | free text (no dropdown — always typed) |

`handleInputChange` (`:1392-1410`): on `fromWarehouse` changing **away from** a cold-storage value, all `cs_*` fields on every article are cleared and `articleEntryMode` is reset (`:1395-1407`). `handleTransferInfoChange` (`:1583-1588`) is a plain setter.

### Article line fields (per `Article`, interface `:862-894`, regular-mode JSX `:3394-3611`)

| Field | Type | Required / Validation | Default | Source |
|---|---|---|---|---|
| Quick Search Item | `Input` typeahead (`:3339-3354`) | ≥2 chars to fire (`:1608`) | `""` | `GET /interunit/categorial-search` (§11) |
| Material Type * | `MaterialTypeDropdown` (`:3398-3404`) | (no submit-level check; gated by add-to-list) | `""` | `dropdownApi.fetchDropdown` → RM/PM/FG (`:34-103`) |
| Item Category * | `ItemCategoryDropdown` (`:3413-3434`) | — | `""` | `useItemCategories` (categorial_inv) |
| Sub Category * | `SubCategoryDropdown` (`:3443-3465`) | — | `""` | `useSubCategories` |
| Item Description * | `ItemDescriptionDropdown` (`:3474-3484`) | required by `handleAddArticleToList` (`:1677`) | `""` | `useCategorialItemDescriptions`; selecting also auto-fills `unit_pack_size` from UOM and fetches `sku_id` (`:201-247`) |
| Unit Pack Size/Count | number `Input` (`:3493-3502`) | for FG/RM (non-cold), must be >0 before add (`:1698-1707`) | `0` | manual or auto from item |
| UOM | `Select` (`:3508-3527`) | — | `""` | static (BOX/BAG/CARTON; +BUNDLES/ROLLS/PCS if PM) |
| Case Pack/Box Wt. (`packaging_type`) | number `Input` (`:3535-3544`) | — | `0` | manual; drives net-weight calc |
| Quantity (Box/Bags) (`quantity_units`) | text→number `Input` (`:3550-3556`) | defaults to 1 in calc | `1` (first article) / `0` (added) | manual |
| Net Weight (Kg) | number `Input` (`:3564-3574`) | must be ≤ Total Wt per box (`:2509-2515`, inline `:3575-3577`) | auto-calc | `calculateNetWeight` (`:1522-1535`) |
| Total Wt (Kg) (Gross) | number `Input` (`:3585-3595`) | net ≤ gross | `0` | manual |
| Lot Number (Optional) | text `Input` (`:3603-3609`) | optional in regular mode; **required** in cold mode | `""` | manual / cold-storage select |

Cold-storage mode fields (`:3223-3318`): Item Category, Item Description, Weight(kg) are **read-only** (auto-filled from stock); Total Weight (kgs) auto-calc = qty×net; editable are **No. of Boxes/Cartons*** (capped at `cs_max_boxes`, `:3269-3289`), **UOM*** (`:3290-3309`), **Lot Number*** (`:3310-3317`).

`updateArticle` (`:1537-1581`): clears dependents on material/category/sub-category change; recomputes `total_amount`; auto-recalculates `net_weight` for non-cold articles when qty/packaging/unit_pack_size/material change (skipped for PM `unit_pack_size`, and for cold articles whose `net_weight` stays per-box).

---

## 4. Box scanning / source-stock selection / QR

### Source picker: Cold vs Warehouse

The per-article block switches form based on whether **From** warehouse is a cold-storage value: `COLD_STORAGE_WAREHOUSES = ["Cold Storage", "Rishi", "Savla D-39", "Savla D-514", "Supreme"]` (`:268`); `isColdStorageFrom` (`:1466`). Note "Cold Storage" is the only cold value selectable in the **From** dropdown (`:2830`); the others appear in **To** only.

- **Cold mode** (`isColdStorageFrom && entryMode === "cold-storage"`, `:3212`): renders `ColdStorageStockSearch` (`:577-813`, embedded `:3216-3219`). It searches the cold-stock master and lets the user pick a lot row:
  - Company switcher CFPL/CDPL (`:689-697`) — **independent of URL company**; a CFPL user may pick CDPL stock and vice-versa.
  - Debounced (400ms) search by Lot Number and/or Group/Item Description (`:594-627`) via `ColdStorageApiService.searchColdStorageStocks` → `GET /cold-storage/stocks/search` (`:74-84`).
  - Results table (`:753-810`) shows Inward Dt, Unit, Item Description, Item Mark, Lot No, **Qty of Cartons** (with pending hover), Weight, Total Inv, + **Select** button.
  - **Pending hover** `CartonCellWithPending` (`:321-575`): for each unique (lot, description) it fetches `GET /interunit/pending-stock/by-lot` (`:638-680`) and, when `pending_cartons>0`, shows "+N in transit" with a portal tooltip listing each in-transit challan (status chip Partial/Dispatch, Variance/Edited badges, route, cartons/weight, people, vehicle/driver, reason/remark). **Key note (`:309-333`):** available = `net_qty_on_cartons` as-is; pending is **NOT** subtracted (parking already deletes dispatched boxes from `cold_stocks`).
  - On **Select** → `handleSelectColdStorageStock(articleId, record, sourceCompany)` (`:1472-1519`): fills `item_category`(group), `item_description`, `lot_number`, per-box `net_weight`=`weight_kg`, and the hidden `cs_*` identifiers — `cs_max_boxes`=`ceil(net_qty_on_cartons)`, `cs_box_id`, `cs_transaction_no`, `cs_inward_no`, `cs_company`=`sourceCompany`, `cs_total_inventory_kgs`, `cs_item_mark`. `quantity_units` reset to 0.
- **Warehouse / regular mode** (`:3333-3624`): Quick Search + the full manual article grid (§3). Toggle button "Switch to Manual Entry / Switch to Cold Storage" (`:3157-3192`) flips `articleEntryMode[article.id]` and clears `cs_*` when going to regular.

### Add to Articles List → per-box expansion (`handleAddArticleToList`, `:1675-1872`)

Converts one article into **one `scannedBoxes` entry per quantity unit** (weights divided per box for non-cold; per-box weight kept for cold). Guards: Item Description required (`:1677`); cold qty ≤ `cs_max_boxes` (`:1688`); FG/RM (non-cold) needs `unit_pack_size>0` (`:1698`).

- **Cold articles** call `ColdStorageApiService.pickBoxes` → `GET /cold-storage/stocks/pick-boxes` (`:1747-1754`) to fetch **unique FIFO box_ids** (one per box). Hard guards: requires `item_description`/`lot_number`/`cs_inward_no` (`:1727`) and `cs_company` (`:1738`); aborts if fewer boxes returned than `qty` (`:1764`) or if duplicate box_ids returned (`:1772`). This prevents the historic "700 boxes collapsed to 1" inventory loss (comment `:1722-1724`). Each box gets the picked `box_id` + `transaction_no`; non-cold boxes get `transactionNo='DIRECT'` and `boxId = sku_id || 'N/A'` (`:1793-1798`).
- After adding, the source article fields are reset (`:1840-1871`).

### QR scanning (`handleQRScanSuccess`, `:2033-2439`)

Camera scanner `HighPerformanceQRScanner` (`onScanSuccess`/`onScanError`/`onClose`, `:2972-2976`). `isProcessingRef` debounces (`:2035`, reset after 500ms `:2434-2437`). Parses JSON; classifies:
- **Bulk Entry QR** `tx` starts `BE` (`:2049`) → `GET /interunit/bulk-entry-box-lookup/{company}` (`:2141`).
- **New TR QR** `{tx,bi}` not BE (`:2051`) → `GET /interunit/box-lookup-by-id/{company}` (`:2094`).
- **Old TX/CONS** transaction (`:2178`) → `GET /inward/{company}/{transactionNo}` (`:2180`), matched by box/sku/box_number.
- Duplicate detection by tx+bi (new/bulk) or tx+sku+boxNumber (old) (`:2065-2087`); duplicates alert + toast and abort (`:2076`).
- Else treats QR as a **request QR** and auto-fills header + first article (`:2362-2384`), or plain box-id text starting CONS/TR (`:2388-2426`).
- Each scanned box pushed to `scannedBoxes` and counters/toasts updated against `articles[0].quantity_units` (`:2307-2360`).

### Manual box entry (`handleManualBoxFetch`, `:1940-2031`)

Box Number + Transaction No (`:2994-3038`; Enter on Box Number focuses Txn, Enter on Txn fetches). Duplicate guard by boxNumberInArray+txn (`:1951`). Calls `GET /interunit/box-lookup/{company}?box_number=&transaction_no=` (`:1965`) and appends the box.

`updateScannedBox` (`:1926-1937`): inline edits on each box; changing **Case Pack** recomputes `netWeight = casePack × packageSize`. `handleRemoveBox` (`:1875-1922`) removes and decrements `loadedItems` counters.

---

## 5. Dropdowns & data sources

- **From warehouse** (`:2825-2830`, static): W202, A185, A101, A68, F53, **Cold Storage**.
- **To warehouse** (`:2866-2882`, static): W202, A185, A101, A68, F53, **Rishi, Savla D-39, Savla D-514, Supreme**.
- **Reason** (`:2903-2908`, static): Stock Requirement, Material Movement, Production Need, Customer Order, Inventory Balancing, Other.
- **Vehicle Number** (`:3070-3073`, static): MH43BP6885, MH43BX1881, MH46BM5987 (Contract Vehicle), Other. (Edit-mode "known vehicles" list `:1221`.)
- **Driver Name** (`:3100-3103`, static): Tukaram (+919930056340), Sachin (8692885298), Gopal (+919975887148), Other. (Edit-mode "known drivers" list `:1225`; `getDriverPhone` map `:2727-2735` is defined but only legacy.)
- **UOM** (`:3516-3525` regular, `:3297-3306` cold): BOX, BAG, CARTON, + BUNDLES/ROLLS/PCS when material is PM.
- **Material Type** (`MaterialTypeDropdown`, `:34-103`): `dropdownApi.fetchDropdown({company,limit:1000})`, filtered to RM/PM/FG; falls back to hard-coded RM/PM/FG on error.
- **Item Category / Sub Category / Item Description** (`:106-265`): `useItemCategories`, `useSubCategories`, `useCategorialItemDescriptions` from `@/lib/hooks/useDropdownData` (categorial_inv table). Hard-coded fallbacks `:1004-1050` if API empty. Selecting a description auto-fills `unit_pack_size` and fetches `sku_id` via `dropdownApi.fetchSkuId` (`:217-242`).
- **Quick Search Item** (`:1600-1631`): `GET /interunit/categorial-search?search=&limit=200`; selecting fills material/category/sub/description/sku_id/unit_pack_size (`:1633-1671`).
- **Cold Storage company switcher** (`:689-697`): CFPL / CDPL (independent of URL company).

---

## 6. Buttons

| Label | Line | Handler | Action / Redirect |
|---|---|---|---|
| Back (`ArrowLeft`) | `:2751-2759` | inline | `router.push('/${company}/transfer')` |
| Start Camera Scan | `:2958-2964` | `setShowScanner(true)` | opens QR scanner |
| (scanner) Close | `:2975` | `setShowScanner(false)` | closes scanner |
| Fetch Box | `:3027-3038` | `handleManualBoxFetch` | `GET /interunit/box-lookup/{company}` → appends box |
| Add Article | `:3143-3146` | `addArticle` (`:1414-1447`) | appends a blank article block |
| Switch to Manual / Cold Storage | `:3158-3191` | inline | toggles `articleEntryMode[id]`; clears `cs_*` on→regular |
| Trash (remove article) | `:3199-3206` | `removeArticle(id)` (`:1449-1463`) | removes article (min 1 enforced) |
| Add to Articles List (cold) | `:3322-3329` | `handleAddArticleToList` | pickBoxes + expands to `scannedBoxes` |
| Add to Articles List (regular) | `:3615-3622` | `handleAddArticleToList` | expands article to `scannedBoxes` |
| Clear All | `:3780-3788` | inline | empties `scannedBoxes`, resets `boxIdCounterRef=1` |
| X (remove box) — mobile/desktop | `:3820-3828` / `:4003-4011` | `handleRemoveBox(box.id)` | removes a scanned box |
| Cancel | `:4076-4081` | `router.back()` | browser back |
| Submit Transfer / Update Transfer | `:4082-4088` | form submit → `handleSubmit` | create or update (disabled if `scannedBoxes.length===0`) |
| Copy (summary dialog) | `:4104-4114` | `navigator.clipboard.writeText` | copies summary text |
| OK (summary dialog) | `:4118-4126` | inline | closes dialog + `router.push('/${company}/transfer')` |

(`handlePrintDC` / `handleDownloadDC` exist at `:2462-2473` but are not wired to any button on this page.)

---

## 7. Submit / create-update flow (`handleSubmit`, `:2475-2724`)

1. **Validation** (`:2480-2547`), collected into one toast: From & To required and different; Reason & Reason Description non-blank; **at least one box in `scannedBoxes`** (`:2504`); per-box net ≤ gross (`:2509-2515`); Vehicle (and Other) ; Driver (and Other); Approval Authority non-blank. No request_no required (direct transfer).
2. **Build `lines`** from `scannedBoxes` (`:2562-2575`) — one line per box, `'N/A'`→`""`/null cleaned (`clean`/`cleanNull` `:2557-2558`). Weights already in Kg.
3. **Build `boxes`** = only boxes with `transactionNo !== 'DIRECT'` (`:2589`) — i.e. real QR-scanned / cold-picked boxes get box-level rows; manually-typed `DIRECT` articles are line-only. Box payload: `box_number, box_id, article, lot_number, batch_number, transaction_no, net_weight, gross_weight` (`:2600-2613`).
4. **Payload** (`:2624-2641`): `header{ challan_no, stock_trf_date(=requestDate), from_warehouse, to_warehouse, vehicle_no, driver_name, approved_by, remark(=reasonDescription||reason), reason_code(=reason), is_xbond:false, new_lot_number:null }`, `lines`, `boxes`, `request_id:null`.
5. **Dispatch:**
   - **Edit:** `InterunitApiService.updateTransfer(Number(editId), payload)` → `PUT /interunit/transfers/{id}` (`:2659`).
   - **Create:** `InterunitApiService.submitTransfer(company, payload, user)` → `POST /interunit/transfers?created_by=` (`:2665`).
6. **Status logic (server-side):** header is inserted with `'Dispatch'` (`interunit_tools.py:832`). After parking, status is recomputed (`:1055-1068`): if box rows exist, `total_expected = Σ line.qty`, `actual_scanned = len(boxes)`; status = **"Dispatch"** if `actual_scanned >= total_expected` else **"Partial"**. The UI banner always says "Dispatch" (`:4073`) but the persisted value may be Partial. (For pure article-only/line-level transfers with no box rows, status remains "Dispatch".)
7. **Park into pending stock (server-side):** boxes → `park_in_pending` (`interunit_tools.py:1001-1011`), which inserts an "In Transit" row into `pending_transfer_stock` and **DELETEs the source row** (cold or bulk-entry). Box-less transfers → `park_lines_in_pending` (tracking-only, no inventory change). Then `reconcile_transfer_to_order` fills any box shortfall by lot (`:1083`).
8. **Over-order / stock guards:** front-end cold limit at add time (`qty > cs_max_boxes` blocked, `:1688`, `:3277-3282`); `pickBoxes` aborts if insufficient unique boxes (`:1764`). Server-side, cold-source boxes that exist in neither `cold_stocks` nor pending In-Transit are rejected (`interunit_tools.py:770-808`). Scanned-vs-expected mismatch is surfaced as Partial status rather than blocking. The Scanned Boxes summary shows live Request Qty / Remaining (`:4026-4040`).
9. **Post-submit (`:2673-2713`):** clears the localStorage draft (`clearSavedData`). If **cold storage is involved** (`fromWh` or `toWh` in `COLD_STORAGE_WAREHOUSES`, `:2679`), builds a per-item summary (Item Mark / No of Boxes / Lot Number + From/To + Vehicle) and shows the **Cold Transfer Summary dialog** (`:2681-2707`); redirect happens only when the user clicks **OK** (`:4118-4126`). Otherwise redirects to `/${company}/transfer` after 1.5s (`:2710-2712`). Errors → destructive toast (`:2715-2723`).

### Edit-mode prefill (`loadTransferForEdit`, `:1196-1355`)

`getTransferById` → sets `transferNo`(=challan_no), `requestNo`, header fields (`reason_code`→reason, `remark`→reasonDescription), maps vehicle/driver to preset-or-"other" (`:1221-1236`), first-article dropdowns from `lines[0]` (`:1239-1260`). Boxes: prefer `transfer.boxes` (QR boxes with per-box weights, `:1264-1304`); otherwise rebuild from `transfer.lines` as `DIRECT` manual entries (`:1305-1336`). `edited_at` is written **only** by the backend `update_transfer` (`interunit_tools.py:1689`), powering the "Edited" badge in pending hovers.

---

## 8. Page-in-page & hover actions

- **Cold Transfer Summary dialog** (`:4098-4129`): non-dismissable modal (`onOpenChange={()=>{}}`, `onPointerDownOutside` prevented) shown only for cold-involved transfers. Copy button (`:4104-4114`) with Check/Copy toggle; OK closes + redirects.
- **Pending-transfers hover/tooltip** `CartonCellWithPending` (`:321-575`): inside the cold-stock search results, hovering/clicking a carton count opens a `createPortal`-to-`document.body` card listing in-transit challans (status chip, Variance/Edited badges, route, totals, people, logistics, reason/remark). Hover-intent timers (`:389-401`). Renders only when `pending_cartons>0`.
- **BoxScrollContainer** (`:3802-3805`, component `components/modules/inward/BoxScrollContainer.tsx`): wraps the scanned-box list with a box-number navigator (jump/scroll to a box by number/lot/article). Receives `boxCount` + `boxForms` (box_number/lot_number/article_description per box) and a `registerRef` render-prop used by both mobile cards (`:3810`) and desktop rows.
- **Quick-search dropdown** (`:3362-3391`): floating results panel under the per-article search input.

---

## 9. Keyboard / click directions

- **Manual Box Entry** (`:3003-3008`, `:3019-3024`): Enter on Box Number → focuses Transaction No input (`#manual-txn-input`); Enter on Transaction No → triggers `handleManualBoxFetch`.
- **Cold-stock No. of Boxes** clamp (`:3275-3283`): typing a value above `cs_max_boxes` toasts "Limit Exceeded" and clamps to max.
- **Number inputs** use `onWheel={e => e.currentTarget.blur()}` throughout (`:3500, :3542, :3571, :3592`, and all box-table inputs) so scroll doesn't change values.
- **Quick search** (`:3344-3352`): focus reopens prior results; blur closes after 200ms so a result click can fire first; selection via `onMouseDown` (preventDefault) at `:3369`.
- **Cold search** (`:712, :732`): X buttons clear lot/description fields and reset results.
- **QR scanner**: clicking a result Select / scanning fires the respective handlers (no special key bindings beyond the scanner component's own).

---

## 10. Redirects

- Back button → `/${company}/transfer` (`:2755`).
- Cancel button → `router.back()` (`:4079`).
- Successful submit, **no cold storage** → `/${company}/transfer` after 1.5s (`:2710-2712`).
- Successful submit, **cold involved** → dialog; OK → `/${company}/transfer` (`:4122`).
- No redirect on validation or submit error (toast only).

---

## 11. API calls

| Method | Endpoint | Params / Body | Purpose | Resolved at |
|---|---|---|---|---|
| POST | `/interunit/transfers?created_by=<user>` | `{header, lines, boxes, request_id:null}` | **Create** transfer OUT | `submitTransfer` (`interunitApiService.ts:382-404`); server `interunit_server.py:239`, `interunit_tools.py:818` |
| PUT | `/interunit/transfers/{id}` | same payload | **Update** transfer OUT (writes `edited_at`) | `updateTransfer` (`:445-462`); server `:276`, `interunit_tools.py:1405` |
| GET | `/interunit/transfers/{id}` | — | Load transfer for edit prefill | `getTransferById` (`:465-473`); server `:268` |
| GET | `/interunit/requests/{id}` | — | Legacy `?requestId` prefill | `getRequest` (`:318-320`) |
| GET | `/cold-storage/stocks/search` | `company,lot_no?,q?` | Cold-stock lot search | `searchColdStorageStocks` (`coldStorageApiService.ts:74-84`); server `cold_storage_server.py:371` |
| GET | `/cold-storage/stocks/pick-boxes` | `company,item_description,lot_no,inward_no,qty` | FIFO per-box id pick (cold add) | `pickBoxes` (`:86-105`); server `cold_storage_server.py:502` |
| GET | `/interunit/pending-stock/by-lot` | `lot_no,item_description,from_company` | In-transit qty for hover tooltip | raw `fetch` (`:663`); server `interunit_server.py:109` |
| GET | `/interunit/categorial-search` | `search,limit=200` | Quick item search | raw `fetch` (`:1618`) |
| GET | `/interunit/box-lookup/{company}` | `box_number,transaction_no` | Manual box fetch | raw `fetch` (`:1965`) |
| GET | `/interunit/box-lookup-by-id/{company}` | `box_id,transaction_no` | New TR-QR box lookup | raw `fetch` (`:2094`) |
| GET | `/interunit/bulk-entry-box-lookup/{company}` | `box_id,transaction_no` | Bulk-entry (BE) QR lookup | raw `fetch` (`:2141`) |
| GET | `/inward/{company}/{transactionNo}` | — | Old TX/CONS QR transaction lookup | raw `fetch` (`:2180`) |
| (hook) | dropdown endpoints | via `dropdownApi.fetchDropdown` / `fetchSkuId` / `useDropdownData` | material types, categories, sku_id | `:55, :217, :995-1001` |

`NEXT_PUBLIC_API_URL` (default `http://localhost:8000`) is the base for raw fetches; auth bearer is attached by the service layer (`interunitApiService.ts:80-90`, `coldStorageApiService.ts:19-29`) but raw fetches on this page do **not** add the bearer header.

---

## 12. Backend & DB wiring touched

On **POST/PUT** (`interunit_tools.py`):
- **`interunit_transfers_header`** — inserted with `status='Dispatch'` (`:832`); status later patched to Dispatch/Partial (`:1055-1068`, update `:1665-1677`); `from_cold_unit` set for cold-source transfers from parked JSONB (`:1020-1038`); **`edited_at`** written on update only (`:1689`).
- **`interunit_transfers_lines`** — one row per submitted line; net/total weight from frontend or recomputed (`:878-909`); on update, old lines deleted then re-inserted (`:1464`).
- **`pending_transfer_stock`** — `park_in_pending` (`pending_stock_tools.py:1096-1258`) inserts one "In Transit" row per box (`ON CONFLICT (box_id,transaction_no) DO NOTHING`) and **DELETEs the source row** (`DELETE FROM {source_table} ... :rid`, `:1224-1227`); writes a disposition-ledger audit row (`:1232`). Box-less transfers → `park_lines_in_pending` (tracking-only, no inventory move). On update, existing pending rows are first rolled back to source before re-parking (`:1417`).
- **Source deduction:** cold-source boxes resolved via `_find_in_cold_stocks` (**`cfpl_cold_stocks` / `cdpl_cold_stocks`**), warehouse boxes via `_find_in_bulk_entry` (**`{company}_bulk_entry_boxes`** / fallback **`{company}_boxes_v2` + `_transactions_v2`**); the matched row is deleted as above. Cold availability shown in the UI is **not** re-decremented for in-transit (parking already removed it — `:309-333`).
- **`interunit_transfer_requests`** — set to `'Transferred'` only if `request_id` present (always null here, so untouched) (`:1070-1079`).
- `reconcile_transfer_to_order(header_id, ...)` (`:1083`, `:1680`) reconciles pending rows up to the ordered lot/qty.

---

## 13. Cross-module linkages

- **Cold Storage module:** stock search, FIFO `pick-boxes`, and `cfpl/cdpl_cold_stocks` deduction tie this form to the Cold Storage inventory (`coldStorageApiService.ts`, `cold_storage_server.py`).
- **Inward module:** old TX/CONS QR codes resolve against `/inward/{company}/{tx}`; warehouse boxes deduct from `{company}_boxes_v2`/`bulk_entry_boxes` (the inward inventory tables). `BoxScrollContainer` is shared from `components/modules/inward/`.
- **Transfer IN (receive):** parked `pending_transfer_stock` "In Transit" rows created here are consumed by the receive flow (`transferIn` page) which moves them into the destination. The pending hover (`pending-stock/by-lot`) reads the same ledger.
- **Transfer dashboard / Transfer OUT records:** created transfers appear there (`from_cold_unit` chip filter for cold sub-units); status Dispatch/Partial drives badges.
- **Categorial inventory (`categorial_inv`):** category/sub-category/description dropdowns, quick-search, and `sku_id` lookup.
- **Auth store:** `useAuthStore().user` → `created_by` on create; bearer token in service layer.
- **`useFormPersistence`** (`hooks/useFormPersistence.ts`): localStorage draft under `draft-directtransfer` (create) or `draft-edit-transfer-<editId>` (edit), `:966`. Persists formData, articles, transferInfo, transferNo, requestNo, scannedBoxes (with boxIdCounter resync `:972-981`), loadedItems. Cleared on successful submit.

---

## 14. Gotchas

- **Component name is misleading:** `NewTransferRequestPage` is the **Transfer OUT** form, not a request form.
- **UI always shows "Dispatch"** (`:4073`) even though the server may persist **"Partial"** when scanned boxes < ordered qty (`interunit_tools.py:1059`).
- **`DIRECT` boxes are line-only:** manually-typed articles get `transactionNo='DIRECT'` and are **excluded** from the `boxes` payload (`:2589`), so they create no `pending_transfer_stock` box rows and **do not deduct source inventory** — only line-level (tracking-only) pending. Real deduction happens only for QR-scanned / cold-picked boxes.
- **Cold per-box id integrity:** `pickBoxes` must return one **unique** `box_id` per box; the form aborts on shortfall or duplicates (`:1764`, `:1772`) to avoid the documented "boxes collapse to 1 on receive" inventory loss (`:1722-1724`).
- **Cold company ≠ URL company:** the in-article cold search switcher (`cs_company`) is the source of truth for `pickBoxes`; a CFPL-URL user can dispatch CDPL stock (`:1735-1745`).
- **Available cartons are NOT reduced by pending** in the search results — parking already deleted dispatched boxes from `cold_stocks` (`:309-333`); the "+N in transit" is informational only.
- **Net ≤ Gross** is validated per box at submit (`:2509-2515`) and inline on the article net-weight field (`:3573-3577`), but the box table inputs themselves are not individually blocked while typing.
- **localStorage drafts:** restored on mount; an old cached `requestDate` is force-reset to today (`:986-992`). `boxIdCounterRef` is resynced from restored boxes to avoid id collisions (`:972-981`).
- **`?requestId` locks:** if reached via a request, the first article's classification fields are read-only with a 🔒 hint; not applicable to a pure direct-OUT entry.
- **Raw fetches skip the auth bearer:** the several `fetch(...)` calls on this page (quick search, box lookups, pending-by-lot, inward) do not attach the bearer token that the service-layer clients add — relevant if those endpoints become auth-gated.
- **Submit disabled until ≥1 box** (`:4084`); the "Add to Articles List" step (or scanning) is mandatory — filling the article grid alone is not enough.
