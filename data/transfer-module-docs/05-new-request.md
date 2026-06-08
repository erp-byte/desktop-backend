# New Transfer Request — `/[company]/transfer/request`

| | |
|---|---|
| **File** | `d:\test\frontend-\app\[company]\transfer\request\page.tsx` (910 source lines) |
| **URL** | `/{company}/transfer/request` (e.g. `/cfpl/transfer/request`) |
| **Purpose** | Create a new **interunit transfer request** (the "ask" that precedes an actual Transfer Out). Captures a request header (date, from/to warehouse, reason) plus one or more article lines, and POSTs them to the backend with status **Pending**. |

This document covers ONLY this page. Helper modules are referenced where they shape this page's behaviour:
- `d:\test\frontend-\lib\interunitApiService.ts` — `InterunitApiService`, `transformFormDataToApi`, `validateRequestData`
- `d:\test\frontend-\lib\hooks\useDropdownData.ts` — cascading dropdown hooks
- `d:\test\frontend-\lib\api.ts` — `dropdownApi.fetchDropdown`
- `d:\test\frontend-\components\ui\searchable-select.tsx` — `SearchableSelect`
- `d:\test\ims-app-backend\services\ims_service\interunit_server.py` / `interunit_tools.py` / `interunit_models.py` — backend route, persistence, schema

---

## 1. Route & params

- **Route segment:** App-Router page at `app/[company]/transfer/request/page.tsx`. Dynamic segment `[company]`.
- **Props:** `NewTransferRequestPageProps` (`page.tsx:21-25`) — `params: { company: Company }`. `Company` type from `@/types/auth` (`page.tsx:19`).
- **Destructured at** `page.tsx:96`: `const { company } = params`.
- **No `searchParams`** are read. No query string is consumed.
- `company` is passed downstream to: `MaterialTypeDropdown` (`page.tsx:687`), the dropdown hooks `useItemCategories` / `useSubCategories` / `useItemDescriptions` (`page.tsx:229-236`), and the post-submit redirect (`page.tsx:397`).
- `"use client"` component (`page.tsx:1`) — fully client-rendered; no server component / data-fetching at the route level.

---

## 2. Layout & structure

Everything is wrapped in a single `<form id="transfer-request-form" onSubmit={handleSubmit}>` (`page.tsx:426`). Top-level container: `div.p-3...space-y-4 bg-gray-50 min-h-screen` (`page.tsx:427`).

Vertical sections, top to bottom:

1. **Header** (`page.tsx:429-446`) — back button (`ArrowLeft`), title "New Transfer Request" with `FileText` icon, and a sub-line showing the generated **Request No** (`page.tsx:444`).
2. **Request Header Card** (`page.tsx:449-559`) — `Card` with blue/indigo gradient header "Request Header". Body is a 1→2 column responsive grid (`page.tsx:457`) holding Request Date, From Warehouse, To Warehouse, and a full-width Reason Description textarea.
3. **Article Management Section** (`page.tsx:562-906`), `id="article-section"`:
   - Section heading + **Add Article** button (`page.tsx:564-575`).
   - **Saved Articles list** — one emerald-bordered read-only `Card` per item in `articlesList` (`page.tsx:578-606`).
   - **Article Details Form** — violet/purple `Card` "Article {n}" (`page.tsx:609-866`) containing: a Quick Search box, the 4 cascading dropdowns, and 6 common numeric/text fields.
   - **Submit Footer** `Card` (`page.tsx:869-905`) — "Pending status" note + Cancel + Submit buttons.

Component library: shadcn/ui primitives — `Card`, `Button`, `Input`, `Label`, `Textarea`, `Select*`, `Badge` (`page.tsx:6-14`). Icons from `lucide-react` (`page.tsx:13`). `Badge` is imported but **not used** on this page.

---

## 3. Form fields

State is split into three buckets: `formData` (header, `page.tsx:125-131`), `articleData` (the in-progress article, `page.tsx:133-144`), and `articlesList` (committed articles, `page.tsx:147-158`).

### Header fields (`formData`)

| Field | Type | Required / Validation | Default | Source |
|---|---|---|---|---|
| Request Date | Free-text `<Input type="text">` (`page.tsx:463-470`) | Label marked `*`. No client field-level guard, but `validateRequestData` requires non-empty `request_date` (`interunitApiService.ts:719-721`). Backend parses strictly as `DD-MM-YYYY` (`interunit_tools.py:110-114`). | `currentDate` = today as `DD-MM-YYYY` via `toLocaleDateString('en-GB')` then `/`→`-` (`page.tsx:112-116`, set into state `page.tsx:126`). | User-typed; placeholder `"17-10-2025"`. |
| From (Requesting Warehouse) | `Select` (`page.tsx:478-505`) | Label `*`. `validateRequestData` requires non-empty `from_warehouse` AND `from !== to` (`interunitApiService.ts:723-730`). | `""` (`page.tsx:127`). | `warehouseSites` API; hardcoded fallback list if API empty (`page.tsx:495-500`). |
| To (Supplying Warehouse) | `Select` (`page.tsx:512-541`) | Label `*`. `validateRequestData` requires non-empty `to_warehouse` AND `from !== to` (`interunitApiService.ts:725-730`). | `""` (`page.tsx:128`). | `warehouseSites` API; **different** hardcoded fallback (`page.tsx:529-538`). |
| Reason Description | `Textarea` (`page.tsx:549-555`) | Label `*`. Required non-empty in `validateRequestData` (`interunitApiService.ts:731-733`). Backend uppercases it (`interunit_models.py:17-20`). | `""` (`page.tsx:130`). | User-typed; placeholder `"Enter short description about Reason..."`. |
| `reason` (state key) | — | Held in `formData` (`page.tsx:129`) but **never bound to any input** and never sent. Dead field. | `""` | n/a |

### Per-article fields (`articleData` → repeated into `articlesList`)

| Field | Type | Required / Validation | Default | Source |
|---|---|---|---|---|
| Quick Search Item | `Input` text (`page.tsx:623-629`) | Optional helper, not persisted. Debounced 300ms; fires only at ≥2 chars (`page.tsx:184`, `page.tsx:208-212`). | `searchQuery=""` (`page.tsx:163`) | `/interunit/categorial-search` (auto-fills the 4 dropdowns + package size). |
| Material Type | `MaterialTypeDropdown` (custom `SearchableSelect`) (`page.tsx:684-688`) | Label `*`. Required to **Add Article** (`page.tsx:302`) and in `validateRequestData` (`interunitApiService.ts:741-742`). | `""` (`page.tsx:134`) | Dropdown API `material_types`, filtered to `RM/PM/FG` (`page.tsx:52-54`). |
| Item Category | `SearchableSelect` (`page.tsx:696-712`) | Label `*`. Required in `validateRequestData` (`interunitApiService.ts:744-745`). Disabled until Material Type chosen (`page.tsx:711`). | `""` (`page.tsx:135`) | `useItemCategories` (`page.tsx:229`). |
| Sub Category | `SearchableSelect` (`page.tsx:720-736`) | Label `*`. Required in `validateRequestData` (`interunitApiService.ts:747-748`). Disabled until Category chosen (`page.tsx:735`). | `""` (`page.tsx:136`) | `useSubCategories` (`page.tsx:230`). |
| Item Description | `SearchableSelect` (`page.tsx:744-760`) | Label `*`. Required to **Add Article** (`page.tsx:302`) and in `validateRequestData` (`interunitApiService.ts:750-751`). Disabled until Category + Sub Category chosen (`page.tsx:759`). | `""` (`page.tsx:137`) | `useItemDescriptions` (`page.tsx:231-236`). Selecting auto-fills `packageSize` from option `uom` (`page.tsx:271-276`). |
| Unit Pack Size/Count (`packageSize`) | `Input type=number` step `any` min `0`, `onWheel` blur (`page.tsx:771-780`) | Optional in this form. **Conditionally required for FG** in `validateRequestData` (must be non-empty and not `'0'`) (`interunitApiService.ts:753-755`). | `""` (`page.tsx:142`) | User-typed or auto-filled from search/description `uom`. |
| UOM | `Select` (`page.tsx:788-800`) | No `*`. Not validated. | `""` (`page.tsx:139`) | **Hardcoded** 3 options: BOX / CARTON / BAG (`page.tsx:796-798`). |
| Case Pack/Box Wt. (`packSize`) | `Input type=number` step `any` min `0`, `onWheel` blur (`page.tsx:808-817`) | No `*`. Not validated. Feeds net-weight calc. | `""` (`page.tsx:141`) | User-typed; placeholder `"0.00"`. |
| Quantity (Box/Bags) | `Input type=text` (note: text, not number) (`page.tsx:825-831`) | No `*`. Not validated. Feeds net-weight calc. | `""` (`page.tsx:138`) | User-typed; placeholder `"0"`. |
| Net Weight (Kg) | `Input type=number` step `any` min `0`, `onWheel` blur (`page.tsx:839-848`) | No `*`. Auto-calculated but editable. | `"0"` (`page.tsx:142`) | `calculateNetWeight` (`page.tsx:287-299`); user-overridable. |
| Lot Number | `Input type=text` (`page.tsx:856-862`) | Explicitly labelled **(Optional)** (`page.tsx:854`). | `""` (`page.tsx:144`) | User-typed. |

> Note on defaults after first add: `handleAddArticle` resets `articleData` with `quantity:"1"`, `packSize:"1"`, `netWeight:"0"` (`page.tsx:311-322`) — slightly different from the initial-mount defaults (`quantity:""`, `packSize:""`).

---

## 4. Dropdowns & data sources

| Dropdown | Component | Hook / API | Underlying endpoint | Notes |
|---|---|---|---|---|
| From / To Warehouse | `Select` (`page.tsx:478`, `512`) | `InterunitApiService.getWarehouseSites()` (`page.tsx:342`) | `GET /interunit/dropdowns/warehouse-sites?active_only=true` (`interunitApiService.ts:251-253`) → `warehouse_sites` table (`interunit_tools.py:172-185`) | Both render the same `warehouseSites` list; value = `site_code`. If empty, **distinct hardcoded fallbacks** (From: `page.tsx:495-500`; To: `page.tsx:529-538`). |
| Material Type | `MaterialTypeDropdown` (`page.tsx:28-93`) | `dropdownApi.fetchDropdown({ company, limit:1000 })` (`page.tsx:49`) | `GET /inward/sku-dropdown` (`api.ts:641`) → reads `data.options.material_types` | Filtered to allowed `['RM','PM','FG']` (`page.tsx:52-54`). Hardcoded RM/PM/FG fallback on missing/failed (`page.tsx:58-72`). Fetched once on mount (`useEffect` deps `[]`, `page.tsx:79`). |
| Item Category | `SearchableSelect` (`page.tsx:696`) | `useItemCategories({ company, material_type })` (`page.tsx:229`) | `GET /inward/sku-dropdown?...&material_type=...` → `options.item_categories` (`useDropdownData.ts:71-117`, `api.ts:617`) | Cleared/disabled until Material Type set; refetches when material_type changes. |
| Sub Category | `SearchableSelect` (`page.tsx:720`) | `useSubCategories(itemCategory, { company, material_type })` (`page.tsx:230`) | `GET /inward/sku-dropdown?...&item_category=...` → `options.sub_categories` (`useDropdownData.ts:123-174`) | Requires both `categoryId` and `material_type` (`useDropdownData.ts:131`). |
| Item Description | `SearchableSelect` (`page.tsx:744`) | `useItemDescriptions({ company, material_type, item_category, sub_category })` (`page.tsx:231-236`) | `GET /inward/sku-dropdown?...` (limit 500) → `options.item_descriptions` (+ `item_ids`, `uom`) (`useDropdownData.ts:179-249`) | Options carry `uom` used to auto-fill Unit Pack Size (`page.tsx:271-276`). |
| Quick Search | plain `Input` + custom dropdown (`page.tsx:623`, `637-667`) | inline `fetch` in `doArticleSearch` (`page.tsx:183-206`) | `GET /interunit/categorial-search?search=...&limit=200` (`page.tsx:192`) → backend `categorial_global_search` over `all_sku.particulars` (`interunit_server.py:688-696`) | Returns `items[]` (`id, item_description, material_type, group, sub_group, uom`) + `meta.total_items`. |
| UOM | `Select` (`page.tsx:788`) | none — **static** | n/a | Hardcoded BOX / CARTON / BAG (`page.tsx:796-798`). The service's `getUOM()` / `getMaterialTypes()` / `getApprovalAuthorities()` (`interunitApiService.ts:255-265`) are **NOT used** by this page. |

**Approval authorities:** N/A — this page has no approval-authority field or dropdown. (`InterunitApiService.getApprovalAuthorities` exists but is not invoked here.)

**Unused import:** `useCategorialItemDescriptions` is imported (`page.tsx:5`) but **not called** — the page uses the `/inward/sku-dropdown`-backed hooks instead of the `/interunit/categorial-dropdown` hook.

---

## 5. Buttons

| Label | Location (line) | Type | Handler | Action / Redirect |
|---|---|---|---|---|
| Back (`ArrowLeft` icon) | `page.tsx:430-438` | `button` | inline `onClick` | `router.push(\`/${company}/transfer\`)` — back to dashboard. |
| Add Article | `page.tsx:571-574` | `button` | `handleAddArticle` (`page.tsx:301-327`) | Validates Material Type + Item Description present (else destructive toast); pushes a copy of `articleData` into `articlesList`; resets the working article; shows "Article Added" toast. No navigation. |
| Remove | `page.tsx:583-591` (per saved card) | `button` | `handleRemoveArticle(index)` (`page.tsx:329-335`) | Filters that index out of `articlesList`; shows "Article Removed" toast. |
| Cancel | `page.tsx:876-883` | `button` | inline `router.back()` | Browser-history back (not a fixed route). |
| Submit Request | `page.tsx:884-901` | `submit` (`form="transfer-request-form"`) | `handleSubmit` (`page.tsx:359-423`) | Validates → POST create request → success toast → redirect to dashboard. Disabled + spinner while `isSubmitting` (`page.tsx:887-900`). |

Search-result rows (`page.tsx:641-660`) are `type="button"` entries; clicking calls `handleSelectSearchItem` (`page.tsx:214-226`) — covered in §8.

---

## 6. Validation logic

Two layers; there is **no per-field inline error rendering** on the header/article inputs (the `*` labels are visual only). Errors surface via toasts.

**a) Add-Article gate** (`handleAddArticle`, `page.tsx:301-309`):
- Requires `articleData.materialType` AND `articleData.itemDescription` to be non-empty; otherwise destructive toast "Incomplete Article" and the add is aborted.

**b) Submit-time validation** runs in `handleSubmit` (`page.tsx:377-386`) by calling `validateRequestData(apiData.form_data, apiData.article_data)` (`interunitApiService.ts:715-759`). If the returned array is non-empty, a single destructive "Validation Error" toast joins all messages with `", "` and submission stops.

`validateRequestData` rules:
- `request_date` required (`interunitApiService.ts:719`).
- `from_warehouse` required (`:723`).
- `to_warehouse` required (`:725`).
- `from_warehouse !== to_warehouse` (`:728-730`) — equality check; note this fires even when **both are empty strings** (two blanks are "equal"), but the required checks already flag blanks.
- `reason_description` required (`:731`).
- `article_data.length > 0` (`:736-738`).
- Per article: `material_type`, `item_category`, `sub_category`, `item_description` each required (`:740-752`).
- Per article: if `material_type === 'FG'`, `package_size` must be present and `!== '0'` (`:753-755`).

**Net-weight calculation** (`calculateNetWeight`, `page.tsx:287-299`), always returns Kg:
- FG: `(packageSize * packSize) * quantity`, 3 decimals (`page.tsx:291-294`).
- non-FG: `quantity * packSize`, 2 decimals (`page.tsx:295-298`).
- Recomputed on change of `quantity`, `packSize`, `packageSize`, `materialType`, `itemDescription` (`page.tsx:278-280`). User can still overwrite the field manually (`page.tsx:844`).

**Cascade reset** (`handleArticleChange`, `page.tsx:255-284`): changing `materialType` clears category/sub/description; changing `itemCategory` clears sub/description; changing `subCategory` clears description.

**Backend validation** (defense in depth): `FormDataBase.warehouses_must_differ` (`interunit_models.py:22-26`), date format `DD-MM-YYYY` enforced (`interunit_tools.py:110-114`), `article_data` `min_length=1` (`interunit_models.py:75`), and uppercase coercion of reason/material/uom/category text (`interunit_models.py:17-55`).

---

## 7. Submit / save flow

`handleSubmit` (`page.tsx:359-423`):

1. `e.preventDefault()`; set `isSubmitting=true` (`page.tsx:360-361`).
2. **Assemble article list** (`page.tsx:364-368`): `allArticles = [...articlesList, ...(hasCurrentArticle || articlesList.length===0 ? [articleData] : [])]`. So the in-progress `articleData` is included if it has any material type / description, **or** if no articles were ever added (guarantees ≥1 line). This means a user can submit without ever pressing "Add Article".
3. **Format date** to `DD-MM-YYYY` via `formatDateForAPI` (`page.tsx:245-253`, applied `page.tsx:370-373`).
4. **Transform** to API shape via `transformFormDataToApi(formattedFormData, allArticles, requestNo)` (`page.tsx:375`, impl `interunitApiService.ts:646-712`). Maps camelCase→snake_case, defaults `package_size`/`net_weight`→`"0"`, `lot_number`→`""`, and stamps `computed_fields.request_no = requestNo`.
5. **Validate** (`page.tsx:377-386`) — see §6.
6. **POST** `InterunitApiService.createRequest(apiData, user?.email || 'unknown')` (`page.tsx:388`).
7. On success: read `response.request_no` (fallback `'N/A'`), show success toast (`page.tsx:390-395`), then `router.push(\`/${company}/transfer\`)` (`page.tsx:397`).
8. On error: parse `error.response.data` for `detail`/`message`/string/JSON and show destructive toast (`page.tsx:399-419`).
9. `finally`: `setIsSubmitting(false)` (`page.tsx:421`).

**Request number generation** (`requestNo`): generated **client-side at render** as `REQ{YYYYMMDDHHMMS}` (`page.tsx:101-109`) and shown in the header (`page.tsx:444`). It is passed through `transformFormDataToApi` into `computed_fields.request_no`. The backend honours the supplied `request_no` if present, else generates `REQ{YYYYMMDDHHMM}` (note: minute precision, no seconds) (`interunit_tools.py:106-107`, `create_request` `interunit_tools.py:194-198`). The displayed `requestNo` is computed once per render and is not re-stamped on submit; the response `request_no` is what the toast reports.

**Backend create** (`create_request`, `interunit_tools.py:191-288`): parses date, inserts a header row into `interunit_transfer_requests` with `status='Pending'` (`interunit_tools.py:200-222`), then one `interunit_transfer_request_lines` row per article (`interunit_tools.py:226-284`). Endpoint: `POST /interunit/requests?created_by=<email>` → 201 `RequestWithLines` (`interunit_server.py:187-193`).

---

## 8. Page-in-page & hover actions

- **Quick Search overlay** (`page.tsx:637-667`): an absolutely-positioned (`z-50`) results panel under the search input, max-height `min(480px,60vh)`, scrollable. Each row shows description + `material_type`/`group`/`sub_group` chips + `ID:` badge, and a sticky footer "Showing X of Y results" (`page.tsx:663-665`). Selecting a row (`handleSelectSearchItem`, `page.tsx:214-226`) populates `materialType`, `itemCategory`(=`group`), `subCategory`(=`sub_group`), `itemDescription`, and `packageSize`(=`uom`) into `articleData`, then clears the query and hides the panel. Hovering rows: `hover:bg-gray-50` (`page.tsx:644`).
- **"No items found"** state (`page.tsx:669-673`) when query ≥2 chars yields zero results.
- **Saved-article cards** (`page.tsx:578-606`): read-only mini summary (Type, Category, Sub Category, Description, Qty+UOM, Pack Size, Net Weight) with a hover-styled Remove button (`hover:bg-red-50`, `page.tsx:588`).
- **`SearchableSelect`** is itself a popover-in-page (Radix `Popover` + `Command`) with client-side filtering (`searchable-select.tsx:75-160`); `shouldFilter={false}` with manual `filteredOptions` (`searchable-select.tsx:54-60`, `109`).

---

## 9. Keyboard / click directions

- **Outside-click**: a `mousedown` listener on `document` closes the Quick Search results panel when the click is outside `searchWrapperRef` (`page.tsx:173-181`).
- **Debounced typing**: search input debounces 300ms (`page.tsx:208-212`); searches only at ≥2 chars (`page.tsx:184`).
- **Focus**: focusing the search input re-opens results if any exist (`page.tsx:626`).
- **Mouse wheel**: numeric inputs (Unit Pack Size, Case Pack, Net Weight) blur on `onWheel` to stop scroll-to-change (`page.tsx:777`, `814`, `845`).
- **Enter / form submit**: pressing Enter inside the form submits via the `<form onSubmit>` (`page.tsx:426`); the Submit button is `type="submit"` bound by `form="transfer-request-form"` (`page.tsx:885-886`).
- `SearchableSelect` supports arrow-key/typeahead via the underlying `Command` palette (`searchable-select.tsx:109-115`).

---

## 10. Redirects

| Trigger | Destination | Line |
|---|---|---|
| Back button | `/{company}/transfer` (push) | `page.tsx:434` |
| Cancel button | `router.back()` (history) | `page.tsx:879` |
| Successful submit | `/{company}/transfer` (push) | `page.tsx:397` |

No auth/guard redirects are present in this file (it reads `user` from `useAuthStore` only for the `created_by` email, `page.tsx:99`, `page.tsx:388`).

---

## 11. API calls

| Method | Endpoint | Params / Body | Purpose | Resolver |
|---|---|---|---|---|
| GET | `/interunit/dropdowns/warehouse-sites` | `active_only=true` | Populate From/To warehouse selects | `InterunitApiService.getWarehouseSites` (`interunitApiService.ts:251-253`); called `page.tsx:342` |
| GET | `/inward/sku-dropdown` | `company`, `material_type?`, `item_category?`, `sub_category?`, `search?`, `limit/offset?` | Material types + cascading category/sub/description options | `dropdownApi.fetchDropdown` (`api.ts:617-683`); via `MaterialTypeDropdown` (`page.tsx:49`) and hooks (`page.tsx:229-236`) |
| GET | `/interunit/categorial-search` | `search`, `limit=200` | Quick-search items to auto-fill article | inline `fetch` (`page.tsx:192-196`); backend `interunit_server.py:688-696` |
| POST | `/interunit/requests` | query `created_by=<email>`; body `RequestCreate` | Create the transfer request + lines | `InterunitApiService.createRequest` (`interunitApiService.ts:268-285`); called `page.tsx:388` |

POST body shape (built by `transformFormDataToApi`, `interunitApiService.ts:679-712`):
```
{ form_data: { request_date, from_warehouse, to_warehouse, reason_description },
  article_data: [ { material_type, item_category, sub_category, item_description,
                    quantity, uom, pack_size, package_size, net_weight, lot_number } ],
  computed_fields: { request_no },
  validation_rules: { ...flags, material_type_enum:["RM","PM","FG","RTV"], ... } }
```
Auth: `createRequest` goes through `fetchJSON`, which attaches `Authorization: Bearer <accessToken>` from `useAuthStore` (`interunitApiService.ts:80-90`). The two inline/`dropdownApi` GETs send only `Accept: application/json` (no bearer token) (`page.tsx:193`, `api.ts:644-650`).

---

## 12. Backend & DB wiring touched

- **Table `interunit_transfer_requests`** (header) — INSERT in `create_request` (`interunit_tools.py:200-222`). Columns written: `request_no, request_date, from_site, to_site, reason_code, remarks, status('Pending'), created_by, created_ts`.
  - **Field mapping**: form `from_warehouse`→`from_site`, `to_warehouse`→`to_site`; `reason_description`→**both** `reason_code` and `remarks` (defaulting to `"General Transfer"` / `"No remarks"` if blank, `interunit_tools.py:217-218`).
- **Table `interunit_transfer_request_lines`** (lines) — one INSERT per article (`interunit_tools.py:254-283`). Columns: `request_id, rm_pm_fg_type, item_category, sub_category, item_desc_raw, pack_size, qty, uom, unit_pack_size, net_weight, total_weight, lot_number`.
  - `qty` is cast to **int** (`interunit_tools.py:229`); `pack_size`/`unit_pack_size` to float.
  - Net weight: backend prefers a provided `net_weight>0`, else recomputes (FG: `unit_pack_size*pack_size*qty`; else `pack_size*qty`) (`interunit_tools.py:236-241`).
- **Table `warehouse_sites`** — SELECT for the warehouse dropdown (`interunit_tools.py:172-185`).
- **`all_sku` / `all_sku.particulars`** — backs the Quick Search (`categorial_global_search`, `interunit_server.py:688-696`).
- **`/inward/sku-dropdown`** source — backs the cascading dropdowns (different service; `api.ts:641`).
- **Pydantic schema** `RequestCreate`/`FormDataBase`/`ArticleDataCreate` (`interunit_models.py:11-77`) defines accepted body; default `material_type_enum` server-side is `["RM","PM","FG"]` (`interunit_models.py:68`).

---

## 13. Cross-module linkages

- **Inward module reuse**: the cascading dropdowns and `MaterialTypeDropdown` are intentionally built to "match the inward module" (comment `page.tsx:27`), hitting the inward `/inward/sku-dropdown` endpoint via `dropdownApi` (`api.ts:641`).
- **Transfer dashboard**: both the Back button and the post-submit redirect land on `/{company}/transfer` (`page.tsx:434`, `397`) — the dashboard documented in `01-transfer-dashboard.md`.
- **Downstream Transfer Out**: the created request (status Pending) is the input a later Transfer Out consumes; `interunit_transfer_requests.id` is referenced by transfer headers via `request_id` (`interunit_tools.py:1185`, `1297`).
- **Auth store**: `useAuthStore` supplies `user.email` for `created_by` (`page.tsx:99`, `388`) and the bearer token used by `createRequest` (`interunitApiService.ts:81`).
- **Shared service** `InterunitApiService` and `transformFormDataToApi`/`validateRequestData` are shared across the transfer pages (request, transfer form, transfer-in).

---

## 14. Gotchas

1. **Field-name mismatch frontend↔backend.** The frontend sends `package_size` (from the "Unit Pack Size/Count" input) and **never sends `unit_pack_size` or `total_weight`** (`transformFormDataToApi`, `interunitApiService.ts:686-697`). But the backend `ArticleDataCreate` model has `unit_pack_size` and `total_weight` (no `package_size`) (`interunit_models.py:29-40`), and `create_request` reads `line.unit_pack_size` / `line.total_weight` (`interunit_tools.py:230`, `244`). Net effect: `package_size` is silently dropped server-side, `unit_pack_size` is stored as `0`, and FG net-weight recompute would be wrong — **except** the frontend always supplies a positive `net_weight`, which the backend then prefers (`interunit_tools.py:236-237`). The FG "package size required" check is therefore a frontend-only guard; the value it guards is not actually persisted under that name.
2. **`quantity` input is `type="text"`** (`page.tsx:826`), unlike the other numeric fields. Non-numeric entries pass through `parseFloat` (→0) in `calculateNetWeight` (`page.tsx:288`) and `int(...)` server-side could raise on non-integer strings (`interunit_tools.py:229`).
3. **Request number is decorative on the client.** `requestNo` (seconds precision, `page.tsx:109`) is computed once at render; the backend may regenerate at minute precision if it ever falls back (`interunit_tools.py:107`). Two requests submitted in the same minute from a client fallback path could collide — but normally the client value wins.
4. **Two different warehouse fallback lists.** From-warehouse fallback (`page.tsx:495-500`) and To-warehouse fallback (`page.tsx:529-538`) differ (To includes Savla/Rishi/Supreme). These only appear if the warehouse API returns empty.
5. **Submit without "Add Article" works.** Because `handleSubmit` injects the in-progress `articleData` when the list is empty (`page.tsx:366-368`), users can submit a single-line request without ever pressing Add Article. Conversely, if they added articles AND left the working article blank, only the saved ones are sent.
6. **No equality short-circuit for blank warehouses.** `from === to` validation (`interunitApiService.ts:728`) treats two empty strings as equal — harmless because the required-field checks also fire, but the joined toast may show both messages.
7. **Reason mapped to two columns.** `reason_description` populates both `reason_code` and `remarks` (`interunit_tools.py:217-218`); there is no separate reason-code dropdown despite the `reason` state key (`page.tsx:129`, unused).
8. **Unused imports/state.** `Badge` (`page.tsx:14`), `List` icon (`page.tsx:13`), `useCategorialItemDescriptions` (`page.tsx:5`), `formData.reason` (`page.tsx:129`), and the `searchResults` typing field `uom` are present but not driving UI. `articleData.uom`/`packSize` defaults also diverge pre/post first-add.
9. **No optimistic lock / duplicate guard on the form.** Rapid double-submit is only mitigated by the `isSubmitting` disable (`page.tsx:887`); there is no idempotency key beyond the client `request_no`.
