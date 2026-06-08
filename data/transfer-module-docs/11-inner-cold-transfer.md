# Inner Cold Transfer — `/[company]/transfer/innercoldtransfer`

| | |
|---|---|
| **File** | `d:\test\frontend-\app\[company]\transfer\innercoldtransfer\page.tsx` (910 lines) |
| **URL** | `/[company]/transfer/innercoldtransfer` (create) and `/[company]/transfer/innercoldtransfer?editChallan=<challan>` (edit) |
| **Component** | `InnerColdTransferPage` (default export), `page.tsx:208` |
| **Purpose** | A FORM for moving stock *within* cold storage: relabel a lot (`old_lot → new_lot`) and/or change the storage sub-unit (storage location) for selected boxes/cartons. This is NOT an inter-unit transfer — it writes against the **cold-storage** module endpoints (`/cold-storage/inner-transfer`, `/cold-storage/stocks/search`, `/cold-storage/storage-locations`) and directly mutates the company cold-stocks tables (`cfpl_cold_stocks` / `cdpl_cold_stocks`). It does NOT use `interunitApiService` or any `/interunit` endpoint. |

Backend router that serves all endpoints used here: `d:\test\ims-app-backend\services\ims_service\cold_storage_server.py` (router prefix `/cold-storage`, `cold_storage_server.py:15`).

---

## 1. Route & params

- **Route segment**: `app/[company]/transfer/innercoldtransfer/page.tsx`. `params.company` (typed `Company`) is read at `page.tsx:209` and used to: scope stock search (`company` query param), POST payload `company`, the `?company=` on storage-locations fetch, and all back/redirect routes (`/${company}/transfer`).
- **Query param — `editChallan`**: read via `useSearchParams()` at `page.tsx:211`, `searchParams.get('editChallan')` at `page.tsx:215`.
  - `isEditMode = !!editChallan` (`page.tsx:216`) is the single create-vs-edit switch.
- **Create mode** (`editChallan` absent):
  - `transferNo` initialized to a freshly generated number `ICT<YYYYMMDDHHmm>` via `generateTransferNo()` (`page.tsx:219-227`, used at `page.tsx:229`).
  - `transferEntries` starts empty (`page.tsx:293`).
- **Edit mode** (`editChallan` present):
  - The edit-load `useEffect` (`page.tsx:297-344`) GETs `/cold-storage/inner-transfer/{editChallan}` and hydrates `transferNo`, `formData`, and `transferEntries`. Loaded lines are flagged `isExisting: true` (`page.tsx:332`).
  - Header label switches to "Edit Inner Cold Transfer" (`page.tsx:568`).
  - `editLoading` (`page.tsx:294`) drives a full-screen spinner while data loads (`page.tsx:546-555`).
- **Auth gating**: There is **NO explicit auth gating in this page**. `useAuthStore()` is destructured at `page.tsx:213` (`const { user } = useAuthStore()`) but `user` is **never referenced** anywhere else in the file. There is no redirect-on-unauthenticated, no role check, and no guard. (Note: the page's own `fetch` calls send NO Authorization header — only `Accept`/`Content-Type` — so even token-based gating is absent on its raw fetches; see §11.) Any auth enforcement would have to come from a parent layout, not this component.

---

## 2. Layout & structure

Top-level is a `<form onSubmit={handleSubmit}>` wrapper (`page.tsx:558`) with a `max-w-5xl` centered container (`page.tsx:559`). Sections top-to-bottom:

1. **Top navigation bar** (`page.tsx:562-571`): "Back" ghost button + title (`Inner Cold Transfer` / `Edit Inner Cold Transfer`) + sub-line "Transfer No: {transferNo}".
2. **Request Header card** (`page.tsx:574-634`): cyan→blue gradient header. Fields: Transfer No (readonly), Transfer Date, Inner Stock Transfer (location chips), Reason (select), Reason Description (textarea).
3. **Article Entry section** (`page.tsx:636-757`): heading + "Add Article" button; repeats one editable card per `article` in `articles[]`. Each card holds the stock-search box, auto-filled fields, editable box-count/new-lot fields, change-location chips, and an "Add to Transfer List" button.
4. **Transfer List card** (`page.tsx:760-879`): the staged entries (`transferEntries`) shown as a mobile card list (`page.tsx:789-825`) + desktop table (`page.tsx:827-875`); empty-state placeholder (`page.tsx:780-785`).
5. **Validation Errors card** (`page.tsx:882-893`): red panel listing `validationErrors[]`.
6. **Submit button row** (`page.tsx:896-905`).

Responsive pattern: search results and the transfer list each render twice — a `md:hidden` mobile card layout and a `hidden md:block` desktop table.

---

## 3. Form fields

Two state objects back the form: `formData` (header, `page.tsx:252-257`) and per-row `articles[]` (`Article` interface `page.tsx:260-271`, state `page.tsx:273-277`). Staged rows live in `transferEntries[]` (`TransferEntry` interface `page.tsx:280-291`).

### Header fields (`formData`)

| Field | Type | Required / Validation | Default | Source / Line |
|---|---|---|---|---|
| Transfer No (`transferNo`) | text Input, **readonly** | n/a (auto) | `editChallan` or `ICT<YYYYMMDDHHmm>` | `page.tsx:229`, rendered `page.tsx:584`; in edit set from `data.challan_no` `page.tsx:307` |
| Transfer Date (`transferName`) | text Input (free text, `DD-MM-YYYY`) | Marked `*` in label; **NOT validated** in `handleSubmit` | `currentDate` = `DD-MM-YYYY` today (`page.tsx:231`, `253`) | rendered `page.tsx:590-592`; edit set from `data.transfer_date` `page.tsx:309` |
| Inner Stock Transfer (`fromWarehouse`) | Location chips (single-select) | **Required** — `if (!formData.fromWarehouse)` error "Inner Stock Transfer selection is required" (`page.tsx:473`) | `""` | `LocationChips` `page.tsx:598-601`; edit set from `data.from_warehouse` `page.tsx:311`; may auto-fill from selected stock `page.tsx:419-423` |
| Reason (`reason`) | Select (5 fixed options) | **Required** — `if (!formData.reason)` (`page.tsx:474`) | `""` | `page.tsx:610-621`; edit set from `data.reason_code` `page.tsx:312` |
| Reason Description (`reasonDescription`) | Textarea | **Required** — `if (!formData.reasonDescription?.trim())` (`page.tsx:475`) | `""` | `page.tsx:627-630`; edit set from `data.remark` `page.tsx:312` |

All header edits route through `handleInputChange(field, value)` (`page.tsx:350-352`).

### Per-article fields (`articles[]`, `Article` `page.tsx:260-271`)

| Field | Type | Required / Validation | Default | Source / Line |
|---|---|---|---|---|
| `stock_record_id` | hidden (number\|null) | set on stock select | `null` | from `record.id` `page.tsx:407` |
| Item Category (`item_category`) | Input, **readonly** (`bg-muted`) | n/a | `""` | auto from `record.group_name` `page.tsx:408`; rendered `page.tsx:679` |
| Item Description (`item_description`) | Input, **readonly** | gate for "Add to List" (`page.tsx:434`) | `""` | auto from `record.item_description` `page.tsx:409`; rendered `page.tsx:683` |
| Weight (kg) (`net_weight`) | Input, **readonly** | n/a | `0` | auto from `record.weight_kg` `page.tsx:410`; rendered `page.tsx:687` — this is **per-box** weight |
| Total Weight (kgs) | derived display, readonly | n/a | computed | `quantity_units × net_weight` `page.tsx:691` |
| No. of Boxes/Cartons (`quantity_units`) | number Input, `min=1`, `max=available_boxes` | **Required** `> 0` for "Add to List" (`page.tsx:438`); clamps to `available_boxes` with toast (`page.tsx:702-710`) | `0` | user entry `page.tsx:699-711` |
| `available_boxes` | hidden display only | n/a | `0` | `Math.ceil(record.net_qty_on_cartons)` `page.tsx:400`,`411`; shown as "Available: N boxes" `page.tsx:712-714` |
| Old Lot Number (`lot_number`) | Input, **readonly** (`bg-muted font-mono`) | n/a | `""` | auto from `record.lot_no` `page.tsx:409`(`String`); rendered `page.tsx:718` |
| **New Lot Number** (`new_lot_number`) | text Input (orange-bordered, `font-mono`) | **Required** — `if (!article.new_lot_number.trim())` (`page.tsx:442`) | `""` | user entry `page.tsx:722-723` |
| **New Storage Location** (`new_storage_location`) | Location chips (single-select) | **Optional** | `""` | `LocationChips` `page.tsx:736-739`; "Will move to: …" hint `page.tsx:740-744` |

### Staged entry fields (`TransferEntry`, `page.tsx:280-291`)

Built in `handleAddToList` (`page.tsx:446-456`): `id`, `stock_record_id`, `item_category`, `item_description`, `net_weight`, `quantity_units`, `old_lot_number` (= article `lot_number`), `new_lot_number`, `new_storage_location`, and `isExisting?` (only set `true` on edit-loaded rows).

---

## 4. Box / lot selection

This page selects **stock groups** (aggregated by item+lot+inward+mark+location+unit on the backend), not individual box IDs. There is **no box-picker grid** here (unlike `pickBoxes` which exists in the service but is NOT called by this page).

- **Stock search → article fill**: `ColdStorageStockSearch` (`page.tsx:52-206`) lets the user search by lot number and/or description. Selecting a result calls `onSelect → handleSelectColdStorageStock(article.id, record)` (`page.tsx:399-429`), which:
  - sets `stock_record_id`, `item_category` (group_name), `item_description`, `lot_number` (old lot), `net_weight` (per-box kg), `available_boxes` (`Math.ceil(net_qty_on_cartons)`), and resets `quantity_units`, `new_lot_number`, `new_storage_location` (`page.tsx:404-415`);
  - auto-derives `fromWarehouse` from the record's `storage_location`/`unit` if none chosen yet, via `deriveWarehouseFromRecord` (`page.tsx:383-397`, applied `page.tsx:419-423`);
  - fires a "Stock Selected" toast (`page.tsx:425-428`).
- **Box-count entry / lot change**: the user types `quantity_units` (boxes to relabel), enters the mandatory `new_lot_number`, and optionally picks a `new_storage_location`. Box count is clamped to `available_boxes` (`page.tsx:704-708`).
- **Add to list**: `handleAddToList` (`page.tsx:433-460`) validates (description present, qty > 0, new lot present) then pushes a `TransferEntry` capturing the `old_lot_number → new_lot_number` mapping plus optional location move. Toast shows `Lot X → Y`.
- **Backend box/lot mechanics** (informational): the POST handler relabels actual cold-stock rows. If `transfer_qty == current_cartons` it updates `lot_no` (and optional `storage_location`) on all matching rows (`cold_storage_server.py:807-820`); for partial transfers it relabels N rows and **splits** a row when a fractional carton remains (reduce original + INSERT new row, `cold_storage_server.py:821-901`).

---

## 5. Dropdowns & data sources

| Control | Options / Source | Line |
|---|---|---|
| **Inner Stock Transfer** chips (`fromWarehouse`) | Hard-coded `COLD_STORAGE_LOCATIONS = ["Savla Bond","Savla D-39","Savla D-514","Rishi","Supreme"]` via `LocationChips` | const `page.tsx:18`, component `page.tsx:20-43`, used `page.tsx:598` |
| **Change Storage Location** chips (`new_storage_location`) | Same `COLD_STORAGE_LOCATIONS` constant via `LocationChips` | `page.tsx:736-739` |
| **Reason** select | Hard-coded: Stock Requirement, Material Movement, Inventory Balancing, Space Management, Other | `page.tsx:615-619` |
| **Storage locations** (`storageLocations` state) | `GET /cold-storage/storage-locations?company=<company>` on mount (`page.tsx:236-250`) returns distinct DB values from both cold-stock tables (`cold_storage_server.py:33-53`) | **NOTE: this state is fetched but never rendered/used** — the chips use the hard-coded constant, not this fetched list. Dead data. |
| **Cold storage stock search** | `ColdStorageApiService.searchColdStorageStocks(params)` → `GET /cold-storage/stocks/search` | service `coldStorageApiService.ts:74-84`; call `page.tsx:78` |

`ColdStorageStockSearch` (`page.tsx:52-206`) details: two debounced inputs (lot no, group/description), 400 ms debounce (`page.tsx:84-87`), live search effect (`page.tsx:89-92`). Query params built at `page.tsx:74-77`: `limit:"200"`, `company`, optional `lot_no`, `q` (description), `storage_location` (= the chosen `fromWarehouse`, passed as `storageLocation` prop `page.tsx:670`). Results render as mobile cards (`page.tsx:139-159`) and desktop table (`page.tsx:161-198`) with columns #, Inward Dt, Unit, Item Description, Item Mark, Lot No, Qty of Cartons, Weight (kg), Total Inv (computed `cartons×weight` `page.tsx:189`), Storage Location, Action.

---

## 6. Buttons

| Label | Line | Handler | Action / Redirect |
|---|---|---|---|
| Back (←) | `page.tsx:563-566` | inline `router.push('/${company}/transfer')` | Redirects to transfer dashboard |
| Clear (X) on lot search input | `page.tsx:110-113` | inline | Clears `lotNoSearch`, results |
| Clear (X) on desc search input | `page.tsx:120-123` | inline | Clears `descSearch`, results |
| Select (per search result, mobile) | `page.tsx:149` | `handleSelect(record)` → `onSelect` | Fills the article from stock |
| Select (per search result, desktop) | `page.tsx:192` | `handleSelect(record)` → `onSelect` | Fills the article from stock |
| Add Article | `page.tsx:645-647` | `addArticle` (`page.tsx:356-363`) | Appends a blank `Article` to `articles[]` |
| Trash (per article) | `page.tsx:658-661` | `removeArticle(id)` (`page.tsx:365-372`) | Removes article (blocked if only 1 left, toast) |
| Add to Transfer List | `page.tsx:749-752` | `handleAddToList(article)` (`page.tsx:433-460`) | Validates + pushes a `TransferEntry` |
| Clear New | `page.tsx:771-775` | inline `setTransferEntries(prev => prev.filter(e => e.isExisting))` | Removes all non-`isExisting` entries (only shown when new entries exist, `page.tsx:770`) |
| Remove entry (X) — mobile | `page.tsx:804-808` | `handleRemoveEntry(entry.id)` (`page.tsx:462-465`) | Removes one staged entry |
| Remove entry (X) — desktop | `page.tsx:865-869` | `handleRemoveEntry(entry.id)` | Removes one staged entry |
| Submit / Update (type=submit) | `page.tsx:897-904` | `handleSubmit` (form `onSubmit`, `page.tsx:469`) | POST then redirect to dashboard. Disabled while `submitting`, or when `!isEditMode && transferEntries.length === 0` (`page.tsx:897`). Label shows `(+N new)` count in edit mode (`page.tsx:902`) |

---

## 7. Submit / save flow

`handleSubmit` (`page.tsx:469-544`):

1. `e.preventDefault()` (`page.tsx:470`).
2. **Validation** (`page.tsx:472-487`): requires `fromWarehouse`, `reason`, `reasonDescription`; in **create mode only** requires ≥1 transfer entry (`page.tsx:476`). Errors collected into `validationErrors` and a toast fired; submit aborts.
3. **Line selection** (`page.tsx:479-481`): in edit mode only NON-existing entries are sent (`transferEntries.filter(e => !e.isExisting)`); in create mode all entries.
4. **Payload** (`page.tsx:494-514`) — single object with `company`, a `header` `{ challan_no, transfer_name, from_warehouse, remark (= reasonDescription || reason), reason_code, transfer_type: "INNER_COLD" }`, and `lines[]` mapping each entry to `{ stock_record_id, item_category, item_description, net_weight, quantity (= quantity_units), old_lot_number, new_lot_number, new_storage_location || null }`.
5. **Endpoint** (`page.tsx:516-521`): `POST {NEXT_PUBLIC_API_URL}/cold-storage/inner-transfer` (raw `fetch`, headers `Accept` + `Content-Type` only — no auth header).
   - **Same endpoint for create AND edit.** There is no PUT and no DELETE-then-recreate. Edit is "append new lines to the same `challan_no`" — backend always INSERTs new `inner_cold_transfer` rows for the submitted lines (`cold_storage_server.py:904-934`); existing edit-loaded rows are filtered out client-side so they are not re-applied.
6. **Response handling** (`page.tsx:523-537`): non-OK → throw with `errorData.detail`. If `result.errors.length > 0` → "Partial Success" toast and `return` (no redirect). Else success toast ("Transfer Updated"/"Transfer Submitted" + `result.updated_records`) and **`router.push('/${company}/transfer')`** (`page.tsx:537`).
7. `submitting` toggles spinner/disable (`page.tsx:490`, `542`).

Backend POST `/cold-storage/inner-transfer` (`cold_storage_server.py:714-963`): ensures the `inner_cold_transfer` table (`cold_storage_server.py:723`), resolves each line's cold table (`_resolve_record_table`, cfpl first then cdpl), validates qty ≤ available cartons, relabels lot (and optional location), inserts an audit row per line, commits, returns `{ status, updated_records, errors, challan_no }`.

---

## 8. Page-in-page & hover actions

- **Page-in-page**: The embedded `ColdStorageStockSearch` panel (`page.tsx:667-673`) inside each article card is the only nested sub-form — an inline live-search widget, not a modal/dialog. There are no `<Dialog>`/popover overlays on this page.
- **Hover actions**: minor only — chip hover styling (`page.tsx:33`), search-result table row hover (`page.tsx:845` `hover:bg-gray-50`), and remove/clear buttons with `hover:` color/bg states (`page.tsx:806`, `867`, `112`). No hover-reveal action menus.

---

## 9. Keyboard / click directions

- **Form submit**: native `<form onSubmit>` — pressing Enter inside a text field can submit the form (standard browser behavior); the Submit button is `type="submit"` (`page.tsx:897`). All non-submitting buttons are explicitly `type="button"` (e.g. `page.tsx:563`, `645`, `749`, `771`, `804`) to avoid accidental submits.
- **Search**: no explicit Enter handler — searching is **debounced-on-type** (400 ms, `page.tsx:84-92`); the X buttons clear inputs.
- **Click-driven**: location chips toggle on click (clicking the selected chip deselects → `""`, `page.tsx:29`); stock selection, add-to-list, and remove are all click handlers. No drag-and-drop, no arrow-key navigation.

---

## 10. Redirects

| Trigger | Destination | Line |
|---|---|---|
| Back button | `/${company}/transfer` | `page.tsx:563` |
| Successful submit (no `result.errors`) | `/${company}/transfer` | `page.tsx:537` |

No redirect on partial-success (errors present) or on failure — the user stays on the page (`page.tsx:533`, `538-540`). No auth/unauthenticated redirect exists in this page.

---

## 11. API calls

All calls are made from `{NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'}`. Three of the four are **raw `fetch` calls inside the page** (NOT via any service, NOT `interunitApiService`); only the stock search goes through `ColdStorageApiService`.

| Method | Endpoint | Params / Body | Purpose | Line |
|---|---|---|---|---|
| GET | `/cold-storage/storage-locations` | query `?company=<company>` | Fetch distinct storage locations on mount (result stored but **unused** in UI) | raw fetch `page.tsx:239-243`; backend `cold_storage_server.py:33-53` |
| GET | `/cold-storage/inner-transfer/{editChallan}` | path `challan_no` | Load existing transfer header + lines for edit mode | raw fetch `page.tsx:302-303`; backend `cold_storage_server.py:1060-1121` |
| GET | `/cold-storage/stocks/search` | query `limit=200`, `company`, optional `lot_no`, `q`, `storage_location` | Search cold-storage stock groups to fill an article | via `ColdStorageApiService.searchColdStorageStocks` (`coldStorageApiService.ts:74-84`), called `page.tsx:78`; backend `cold_storage_server.py:371-499` |
| POST | `/cold-storage/inner-transfer` | JSON body `{ company, header{…, transfer_type:"INNER_COLD"}, lines[] }` | Create or append the inner cold transfer (relabel lot / change location) | raw fetch `page.tsx:516-521`; backend `cold_storage_server.py:714-963` |

Notes:
- `ColdStorageApiService.searchColdStorageStocks` DOES send a Bearer token (`getAuthHeaders`, `coldStorageApiService.ts:19-29`, `81`). The three raw page-level fetches do **NOT** send Authorization headers (`page.tsx:240`, `303`, `519`).
- `ColdStorageApiService.pickBoxes` (`coldStorageApiService.ts:86-105`) exists but is **not used** by this page.
- The `coldStorageApi` CRUD object (`coldStorageApiService.ts:108-208`) is **not imported/used** here.

---

## 12. Backend & DB wiring touched

Endpoint: `POST /cold-storage/inner-transfer` → `inner_cold_transfer(payload, db)` (`cold_storage_server.py:714-963`).

**Pydantic models** (`cold_storage_server.py:564-587`): `InnerTransferLine` (requires `quantity:int`, `old_lot_number:str`, `new_lot_number:str`; optional `stock_record_id`, `item_category`, `item_description`, `net_weight`, `new_storage_location`), `InnerTransferHeader`, `InnerTransferPayload`.

**Tables touched:**
- **`cfpl_cold_stocks` / `cdpl_cold_stocks`** (`COMPANY_TABLE_MAP`, `cold_storage_server.py:17-20`): the actual stock rows. Per line, `_resolve_record_table(stock_record_id)` (`cold_storage_server.py:639-648`) finds which table holds the record (**cfpl first, cdpl fallback** — the URL `company` is effectively ignored for resolution). Then:
  - **Full transfer** (`transfer_qty == current_cartons`): `UPDATE … SET lot_no = :new_lot` (and `storage_location` if `new_storage_location` given) on all matching rows (`cold_storage_server.py:807-820`).
  - **Partial transfer**: relabels first N rows; if a row must be split, it reduces the original row's cartons/weight/value and INSERTs a new row carrying the new lot/location (`cold_storage_server.py:821-901`).
  - Matching set is grouped by `item_description + lot_no + inward_no` (`cold_storage_server.py:760-775`); per-carton weight/value derived from the reference row (`cold_storage_server.py:796-800`).
- **`inner_cold_transfer`** (audit/header+line table): auto-created via `_ensure_inner_cold_transfer_table` (`cold_storage_server.py:590-636`) — also back-fills `new_storage_location` and `old_storage_location` columns if missing. One row INSERTed per submitted line (`cold_storage_server.py:904-934`) storing `challan_no, transfer_date, from_warehouse, reason_code, remark, stock_record_id, item_category, item_description, net_weight_kg (= per-carton weight × qty, `cold_storage_server.py:802`,`926`), quantity, old_lot_number, new_lot_number, new_storage_location, old_storage_location (= ref_row.storage_location), transfer_type`.

**Reads for edit mode**: `GET /cold-storage/inner-transfer/{challan_no}` (`cold_storage_server.py:1060-1121`) aggregates a header (MIN over the challan group) and returns all lines. Returns `net_weight_kg` as the **total** transferred weight; the page divides by `quantity` to recover per-box weight (`page.tsx:318-334`).

**Related (not called by this page)**: `DELETE /cold-storage/inner-transfer/{challan_no}` (`cold_storage_server.py:1124-1153`, restricted to `INNER_COLD_DELETE_ALLOWED_EMAILS = {hrithik@, yash@}` `cold_storage_server.py:1057`), `DELETE …/line/{audit_id}` reversal (`cold_storage_server.py:702-711`, `reverse_inner_transfer_line` `cold_storage_server.py:651`), and `GET …/inner-transfer/list` (`cold_storage_server.py:966`).

---

## 13. Cross-module linkages

This page is the **Transfer ↔ Cold-Storage bridge**: it lives under the Transfer module route tree (`/[company]/transfer/innercoldtransfer`) but operates entirely on the Cold-Storage module's data and endpoints.

- **Transfer module**: reached from the Transfer dashboard (`d:\test\frontend-\app\[company]\transfer\page.tsx`, which also references `inner-transfer` / `INNER_COLD`). Back and post-submit redirects both return to `/${company}/transfer` (`page.tsx:537`, `563`).
- **Cold-Storage module**: all data flows through `/cold-storage/*` endpoints in `cold_storage_server.py`; the stock-search client is `coldStorageApiService.ts`. The `cfpl_cold_stocks`/`cdpl_cold_stocks` tables are the same tables surfaced by the Cold-Storage inventory views and the Inward (cold) flow.
- **No inter-unit linkage**: deliberately does NOT touch `/interunit`, `interunitApiService`, or any IMS pending-transfer/reconcile pipeline. It is an intra-cold relabel/relocate, not a unit-to-unit movement.

---

## 14. Gotchas

1. **Fetched `storageLocations` is dead data** — `GET /cold-storage/storage-locations` runs on mount (`page.tsx:236-250`) and sets `storageLocations`, but the chips use the **hard-coded** `COLD_STORAGE_LOCATIONS` constant (`page.tsx:18`). The fetched list is never rendered. Locations not in the constant (e.g. a new warehouse in the DB) cannot be selected.
2. **No auth on raw fetches** — the three page-level fetches (storage-locations, edit-load, submit) send no Authorization header (`page.tsx:240`,`303`,`519`); only the service-based stock search does. And `user` from `useAuthStore` is destructured but unused (`page.tsx:213`).
3. **Edit appends, never replaces** — submitting in edit mode only sends `isExisting === false` lines (`page.tsx:479-481`) and the backend always INSERTs. There is no way to edit/remove an already-saved line from this form (removing a `SAVED` row client-side just drops it from the list; it is NOT deleted server-side). Header field edits in edit mode are sent on the new lines' inserts but old rows keep their original header values (header is MIN-aggregated on read).
4. **`fromWarehouse` is a misnomer / not a true source filter** — it's labeled "Inner Stock Transfer", drives the search's `storage_location` filter, and is stored as `from_warehouse`, but the backend resolves the actual table purely from `stock_record_id` (cfpl-first), ignoring the URL `company` for resolution (`cold_storage_server.py:737`, `461-463`).
5. **Per-box vs total weight** — `net_weight` in the article/form is **per-box**; the DB column `net_weight_kg` stores the **total** (`qty × per-box`). Edit-load divides back out (`page.tsx:318-334`). Off-by-rounding is possible (rounded to 3 dp on read, `page.tsx:321`).
6. **Transfer Date is required by label but not validated** — `*` on the label (`page.tsx:589`) but `handleSubmit` never checks `transferName` (`page.tsx:472-476`); it can be blank/garbage.
7. **Box count clamps silently to availability** — entering more than `available_boxes` snaps the value down and toasts (`page.tsx:704-708`); but `available_boxes` derives from `Math.ceil(net_qty_on_cartons)` (`page.tsx:400`) which can over-state availability for fractional cartons.
8. **Stock search ignores `company`** — the backend `company` param is explicitly "Ignored — always searches cfpl first, then cdpl" (`cold_storage_server.py:373`, `461-472`), so a CDPL user may see CFPL stock if CFPL has matches first.
9. **Partial-success keeps you on the page** — if `result.errors` is non-empty, the toast says "Partial Success" and the function returns WITHOUT redirect (`page.tsx:531-534`); some rows may already be committed (backend commits once at the end, `cold_storage_server.py:942-943`), so re-submitting can double-apply lines that succeeded.
10. **New `transferNo` regenerates on each fresh mount in create mode** — based on minute-precision timestamp (`page.tsx:226`); two transfers created within the same minute would collide on `challan_no`.
