# Transfer Dashboard — `/[company]/transfer`

**File:** `frontend-/app/[company]/transfer/page.tsx` (1680 lines, `"use client"`)
**URL:** `/CFPL/transfer` (the `[company]` segment, e.g. CFPL/CDPL)
**Page-in-page components (documented here):** `components/transfer/ChallanHoverCard.tsx`, `components/transfer/PendingTransfersModal.tsx`

> Scope: this file documents ONLY the main transfer dashboard page and the two components it embeds (the hover card and the Pending Transfers modal). Linked destination pages (request, transferform, view, dc, transferIn, innercoldtransfer, dashboard) each have their own MD file.

---

## 1. Route & params

- Dynamic segment `[company]` → `params.company` (e.g. `CFPL`), destructured at `page.tsx:32`. Used as the prefix on every `router.push` target and passed to the In-Transit count fetch (`?company=cfpl`, `page.tsx:383`) and `<PendingTransfersModal company={company}/>` (`page.tsx:1669`).
- **Auth/role gating** via `useAuthStore()` (`page.tsx:35`):
  - `canDelete = user?.email === 'yash@candorfoods.in'` (`:36`) — gates Request/Transfer-Out/Transfer-In delete buttons.
  - `canDeleteInnerCold = email ∈ {hrithik, yash}@candorfoods.in` (`:37`) — gates Inner-Cold delete.
  - Modal `userRole` (`:1671-1675`): `"developer"` if `user.isDeveloper`, else role from `user.companies.find(c => c.code==company)?.role`.
  - No page-level auth redirect; gating is per-button only.
- **Default warehouse** (`:43-53`): `getUserDefaultWarehouses(user.name)` → 1 default sets `warehouseFilter` to that code; >1 → sentinel `"my_warehouses"`; 0 → stays `"all"`.

---

## 2. Layout (top → bottom)

Root `<div className="p-3 ... min-h-screen">` (`:486`):
1. **Header** (`:488-521`): title "Inter-Unit Transfer" (`ArrowRightLeft` icon) + 3 action buttons (Pending Transfers / View Summary / New Request).
2. **Stat cards grid** (`:523-536`): 5 `StatCard`s, `grid-cols-2 sm:3 lg:5`.
3. **Tabs** (`:539-1664`): Radix `<Tabs>` with 5 triggers + 5 panels.
4. **PendingTransfersModal** (`:1666-1676`): rendered once, controlled by `pendingModalOpen`.

Internal sub-components: `StatCard` (`:399`), `EmptyState` (`:418`), `PaginationBar` (`:435`), `SectionHeader` (`:454`), `LoadingSkeleton` (`:470`).

### Tabs (`:540-556`)
| Value | Label (desktop/mobile) | Icon | Content |
|---|---|---|---|
| `request` | Requests / Req | `FileText` | `:558-724` |
| `transferout` *(default, `:38`)* | Transfer Out / Out | `Send` | `:726-968` |
| `transferin` | Transfer In / In | `Inbox` | `:970-1310` |
| `innercold` | Inner Cold / Cold | `PackageCheck` | `:1312-1499` |
| `details` | All Transfers / All | `Package` | `:1501-1663` |

Switching: `onValueChange={setActiveTab}`; active trigger styled dark; `TabsList` horizontally scrollable. Each panel: header → optional filter bar → `LoadingSkeleton` | `EmptyState` | dual render (mobile card list `md:hidden` + desktop `<table>` `hidden md:block`) → `PaginationBar`.

---

## 3. KPI / Stat cards (`:523-536`)

`StatCard` (`:399-416`) renders label + value + colored icon; clickable only when `onClick` provided.

| # | Label | Value | Source | onClick |
|---|---|---|---|---|
| 1 | Requests | `totalRecords` | `getRequests` → `response.total` (`:112`) | — |
| 2 | Pending | `pendingRequests` | `requests.filter(r=>r.status==='Pending').length` — **client-side over loaded array only, case-sensitive** (`:376`) | — |
| 3 | Transfers Out | `transfersTotal` | `getTransfers` → `response.total` (`:134`) | — |
| 4 | Transfers In | `transferInsTotal` | `getTransferIns` → `response.total` (`:156`) | — |
| 5 | In Transit | `inTransitCount` | `GET /interunit/pending-stock?company=` → `data.total` (`:380-390`) | **`setPendingModalOpen(true)`** (`:534`) |

`loadInTransitCount` runs on mount and on each modal-close (`:391-395`); keeps prior count on error.

---

## 4. Warehouse filter / chips

No pill chips — it's a shadcn `Select` repeated in Requests/Transfer-Out/Transfer-In filter bars (`:581, :768, :1017`); Inner-Cold and All-Transfers have none. Shared state `warehouseFilter` (default `"all"`, `:39`).

Options: `all`, `my_warehouses` (only if >1 default), one item per `getAllWarehouseCodes()` (`W202, A185, A101, A68, F53, Savla D-39, Savla D-514, Rishi, Supreme`), labeled via `getDisplayWarehouseName` (so `Supreme`→"Supreme Cold").

**`warehouseMatches(...whs)`** (`:235-254`): `"all"`→true; else splits each candidate on commas (so `from_cold_unit="Rishi, Savla D-39"` matches both chips), normalizes via `normalizeWarehouseName`, then matches against the selected filter (or the `my_warehouses` set). Applied to:
- `filteredTransfers`: `(from_warehouse, to_warehouse, from_cold_unit)` (`:261`)
- `filteredRequests`: `(from_warehouse, to_warehouse)` (`:265`)
- `filteredTransferIns`: `(from_warehouse, receiving_warehouse, from_cold_unit)` (`:269`)

Card "N records" counts show SERVER totals, not filtered counts.

---

## 5. Search (`searchMatch`, `:220-227`)

Client-side reduce/filter (no redirect). Empty query → all; else lowercase substring match over the field list. Three independent boxes (each with clear-`X`):

| Tab | State | Fields searched |
|---|---|---|
| Requests | `requestSearch` | `request_no, from_warehouse, to_warehouse, request_date, status` (`:266`) |
| Transfer Out | `transferOutSearch` | `challan_no, from_warehouse, to_warehouse, from_cold_unit, stock_trf_date, status, vehicle_no, lot_numbers_text` (`:262`) |
| Transfer In | `transferInSearch` | `grn_number, transfer_out_no, receiving_warehouse, from_warehouse, received_by, status, grn_date` (`:270`) |

Inner-Cold / All-Transfers have no search box.

---

## 6. Sorting & grouping

**No user-facing sort/group controls.** Server sort is hard-coded in loaders: transfers `created_ts desc` (`:129`), transfer-ins `created_at desc` (`:151`); requests/inner-cold unsorted. The only grouping is inside hover-card `fetchLines` (group by item/lot — see §11).

---

## 7. Buttons (every button)

| Label | Line | Handler | Action / Redirect |
|---|---|---|---|
| Pending Transfers | `:497` | `setPendingModalOpen(true)` | Opens Pending modal |
| View Summary | `:505` | `router.push` | `/${company}/transfer/dashboard` |
| New Request | `:513` | `router.push` | `/${company}/transfer/request` |
| In Transit card | `:529` | `setPendingModalOpen(true)` | Opens Pending modal |
| Refresh (Requests) | `:561` | `loadRequests(currentPage)` | Reload |
| Clear search ×3 | `:574,:761,:1010` | `set*Search("")` | Clear |
| Requests → View | `:637/:698` | `router.push` | `/transfer/request/${req.id}` |
| Requests → Accept | `:641/:702` | `handleApproveRequest` | `router.push('/transfer/transferform?requestId=${id}')` (`:273-275`); disabled unless status=pending |
| Requests → Delete | `:646/:707` | `handleDeleteRequest` | confirm→`deleteRequest`→reload (`:277-287`); `canDelete` only |
| Direct Transfer Out | `:735` | `router.push` | `/transfer/directtransferform` |
| Transfer-Out → View | `:842/:935` | `router.push` | `/transfer/view/${t.id}` |
| Transfer-Out → Edit | `:847/:940` | `router.push` | `/transfer/directtransferform?editId=${t.id}`; disabled if Received/Completed |
| Transfer-Out → DC | `:853/:946` | `router.push` | `/transfer/dc/${t.id}` |
| Transfer-Out → Delete | `:858/:951` | `handleDeleteTransfer` | confirm→`deleteTransfer`→reload (`:289-299`); `canDelete` |
| Create Transfer IN (CTA) | `:986` | `router.push` | `/transfer/transferIn` |
| Transfer-In → Resume *(status=pending)* | `:1106/:1285` | `router.push` | `/transfer/transferIn?resume=${transfer_out_no}` |
| Transfer-In → View | `:1110/:1290` | `router.push` | `/transfer/transferIn/${ti.id}` |
| Transfer-In → Delete | `:1114/:1294` | `handleDeleteTransferIn` | confirm→raw `DELETE /interunit/transfer-in/${id}?user_email=` → reload (`:301-316`); `canDelete` |
| New Transfer (Inner Cold) | `:1321` | `router.push` | `/transfer/innercoldtransfer` |
| Inner-Cold → Edit | `:1390/:1477` | `router.push` | `/transfer/innercoldtransfer?editChallan=${challan_no}` |
| Inner-Cold → Delete | `:1395/:1482` | `handleDeleteInnerCold` | confirm→raw `DELETE /cold-storage/inner-transfer/${challan}?user_email=` → reload (`:318-333`); `canDeleteInnerCold` |
| All-Transfers → View | `:1561/:1642` | `router.push` | `/transfer/view/${t.id}` |
| All-Transfers → DC | `:1566/:1647` | `router.push` | `/transfer/dc/${t.id}` |
| Pagination Prev/Next | `:444-448` | `onPageChange(page±1)` | Load adjacent page |

**No whole-row onClick** — the challan/GRN/request cell is a hover `ChallanHoverCard`; actions are explicit buttons.

---

## 8. Pagination

- `perPage = 15` (`:61`); `FILTER_FETCH_SIZE = 500` (`:92`).
- Per-tab page state: Requests (`currentPage/...`), Transfer-Out & All-Transfers **share** `transfersPage/...`, Transfer-In (`transferInsPage/...`), Inner-Cold (`innerColdPage/...`).
- Filter-active flags (`:93-98`): when a filter is active, loader fetches `page:1, per_page:500`, sets `total_pages=1`, hides `PaginationBar`, and filters client-side (avoids "filter on page 1 shows nothing"). No filter → server pagination at 15.
- `PaginationBar` (`:435-452`): "Showing X-Y of Z", Prev (disabled p1), "{page}/{tp}", Next (disabled last). Renders only when `tp>1`.

---

## 9. Data loading

| Loader | Service / call | Params | Error toast |
|---|---|---|---|
| `loadRequests` (`:101`) | `InterunitApiService.getRequests` → `GET /interunit/requests` | `{page, per_page}` | "Failed to load requests." |
| `loadTransfers` (`:122`) | `getTransfers` → `GET /interunit/transfers` | `{page, per_page, sort_by:created_ts, sort_order:desc}` | "Failed to load transfers." |
| `loadTransferIns` (`:144`) | `getTransferIns` → `GET /interunit/transfer-in` | `{page, per_page, sort_by:created_at, sort_order:desc}` | "Failed to load transfer INs." |
| `loadInnerColdTransfers` (`:166`) | **raw fetch** `GET /cold-storage/inner-transfer/list?page=&per_page=` | query | "Failed to load inner cold transfers." |
| `loadInTransitCount` (`:380`) | raw fetch `GET /interunit/pending-stock?company=` | — | silent |

**Triggers:** mount→`loadRequests(1)` (`:185`); on `activeTab` change lazily loads if the tab's array is empty (`:187-192`); filter/warehouse change reloads page 1 (`:196-211`).

**Caching:** NONE (no localStorage/SWR/TTL). The only "cache-like" behavior is the lazy-load guard (`array.length===0` skips refetch on tab switch) and `loadInTransitCount` keeping prior value on error.

> Note: an earlier session claimed stale-while-revalidate caching here; the current file has none. Document reflects the actual code.

---

## 10. Redirects (all `router.push`, prefixed `/${company}/transfer/`)

`dashboard`, `request`, `request/${id}`, `transferform?requestId=${id}`, `directtransferform` & `?editId=${id}`, `view/${id}`, `dc/${id}`, `transferIn`, `transferIn?resume=${transfer_out_no}`, `transferIn/${id}`, `innercoldtransfer` & `?editChallan=${challan}`. No navigation outside the `transfer` namespace.

---

## 11. Hover triggers — `ChallanHoverCard` (page-in-page #1)

The identifier cell in every list is a `<ChallanHoverCard>`. Per-tab props:
- **Requests** (`:610/:674`): static `lines` from `req.lines`, `reason=status`. No fetch.
- **Transfer Out** (`:796/:887`): `fetchLines` → `GET /interunit/transfers/${t.id}` (Bearer token); `fromColdUnit = data.from_cold_unit || t.from_cold_unit`; `lines = boxes.length ? groupBoxesByItem(boxes, fromColdUnit) : groupLinesByItem(lines, fromColdUnit)`; meta = Vehicle/Driver(/Variance on mobile).
- **Transfer In** (`:1046/:1184`): `fetchLines` → `GET /interunit/transfer-in/${ti.id}`; groups boxes inline by `article||lot`; builds `discrepanciesMap` (issue/unmatched). meta = Received by/Condition/Issues/Unmatched/Status.
- **Inner Cold** (`:1347/:1425`): static lines with `lotFrom→lotTo`. No fetch.
- **All Transfers** (`:1518/:1594`): same as Transfer Out.

`displayFromSite/displayToSite` (`:360-363`) normalize `from_warehouse||from_site` etc. via `getDisplayWarehouseName`.

### ChallanHoverCard component (`components/transfer/ChallanHoverCard.tsx`)
- **Render:** `createPortal` to `document.body` (escapes overflow). Trigger is a dotted-underline blue span, **hover-only** (`onMouseEnter=open`, `onMouseLeave=scheduleClose`).
- **Props:** `challanNo, from, to, reason, lines?, fetchLines?, meta?, discrepancies?` (`:31-48`).
- **Open/close:** `open` (`:88-102`) computes position, shows, and (once) awaits `fetchLines`; failure caches `[]` so no retry. `scheduleClose` = 180 ms hide timer (`:104`); `cancelClose` keeps it open when the cursor moves onto the card (`:108`).
- **Positioning** `computePosition` (`:63-86`): width 304 (340 if discrepancies), maxH 360, margin 8, gap 6; prefers ABOVE when `spaceAbove≥120 || ≥spaceBelow`, else BELOW; horizontal clamp to viewport. `position: fixed`.
- **Render blocks:** header (`from →(ArrowRight) to`), Reason row, meta chips (tones default/warn/success via `toneClass`), ITEMS list (name, "{qty} boxes", "Wt: {kg} kg", "Count:" rose for PM, "Lot:" indigo mono, violet **"From: {sourceStorage}"** chip, `lotFrom→lotTo`), Discrepancies block (article, count, lot, remark, Net/Gross/CasePack, "N unmatched").
- **`groupLinesByItem(lines, fallbackUnit?)`** (`:270-317`): aggregates by `name||lot`; sums qty/netWeight; `count = unitPackSize×qty` for PM/packaging (`isCountableLine`, `:264`); **`sourceStorage = lotOriginUnit || fallbackUnit`** (`:314`).
- **`groupBoxesByItem(boxes, fallbackUnit?)`** (`:319-382`): qty=1/box; `sourceStorage` priority: `lotOriginUnit` → most-common per-box `source_unit/source_storage` → `fallbackUnit` (`:362-372`). *(The `fallbackUnit` arg was added so the cold "From" chip still shows on dispatched transfers whose cold_stocks rows are consumed.)*

---

## 12. Pending Transfers modal (page-in-page #2) — `components/transfer/PendingTransfersModal.tsx`

In-transit ("Pending Stock") modal opened by the header button and the In-Transit card. `createPortal` to body; ESC + backdrop-click close (`:148-156, :283`).

- **Props** (`:32-39`): `open, onClose, company, apiBaseUrl?, userEmail?, userRole?`. `apiUrl = apiBaseUrl || NEXT_PUBLIC_API_URL || http://localhost:8000` (`:66`).
- **`PendingTransferRecord`** (`:12-30`): `transfer_out_id, transfer_out_challan_no, dispatched_at, from_site/to_site, from_company/to_company, from_storage_type/to_storage_type, total_boxes, total_cartons, total_kg, dispatched_by, status, header_status, unallocated_boxes?, updated_ts?`.
- **`loadData`** (`:88-124`): `GET /interunit/pending-stock?company&search&from_date&to_date`; sets records + `filter_options.{from_sites,to_sites,*_counts}`; prunes selected chips no longer present (anti filter-lock).
- **Auto-sync on open** (`:128-146` + `handleSyncExisting` `:193-249`): `justOpenedRef` fires `loadData()` + `handleSyncExisting(true)` (silent). The sync POSTs `/interunit/pending-stock/backfill?user_email&user_role` (only if `canCancel`), banners Synced/Already-in-sync/Nothing/**"Sync failed: …Refreshing data anyway…"**, and ALWAYS `loadData()` in finally. *(The "Sync failed: Failed to fetch" the user saw = this POST during a server-reload window; the GET list still loads.)*
- **Filters** (`:82-86, 159-191`): search + from/to date (server-side via `loadData`), warehouse chips (client-side `filteredRecords`), Clear filters.
- **Totals bar** (`:167-176, 435-450`): Transfers / Total boxes / Total weight (sum over `filteredRecords`).
- **Table** (`:467-634`): DATE (`formatDate(dispatched_at)`), CHALLAN NO (embedded `ChallanHoverCard`, `fetchLines`→`GET /interunit/transfers/${id}`), FROM→TO (+storage-type sublabel), BOXES, CARTONS, WEIGHT, DISPATCHED BY, STATUS, ACTION.
- **Row badges** (`:578-609`): primary status (`Partial`→"Partial (GRN raised)" amber, else sky); **"{unallocated_boxes} short"** (rose, when >0); **"Edited {date}"** (violet, when `updated_ts` truthy). *(After the recent fix, `updated_ts` here is sourced from the genuine-edit marker `edited_at`, so the badge no longer fires on every transfer.)*
- **Cancel action** (`:251-275, 610-702`): gated by `canCancel(email,role)` (`ALLOWED_CANCEL_EMAILS={yash, b.hrithik}`, `ADMIN_ROLES={admin, developer}`); confirm dialog → `DELETE /interunit/transfers/${id}?user_email&user_role` → restores boxes to source → `loadData`.

---

## 13. Backend & DB wiring touched by THIS page

| UI action | Endpoint | Backend table(s) |
|---|---|---|
| Load requests | `GET /interunit/requests` | `interunit_transfer_requests*` |
| Load transfers | `GET /interunit/transfers` | `interunit_transfers_header/lines/boxes` |
| Load transfer-ins | `GET /interunit/transfer-in` | `interunit_transfer_in_header/boxes` |
| Hover (out) | `GET /interunit/transfers/{id}` | header+lines+boxes, per-lot `lot_origin_unit` from `cfpl/cdpl_cold_stocks` + `pending_transfer_stock` |
| Hover (in) | `GET /interunit/transfer-in/{id}` | transfer-in header+boxes |
| In-Transit count / Pending list | `GET /interunit/pending-stock` | `pending_transfer_stock` (+ live `interunit_transfers_header`) |
| Pending auto-sync | `POST /interunit/pending-stock/backfill` | parks into `pending_transfer_stock`, deducts `cold_stocks`/`bulk_entry_boxes` |
| Cancel pending | `DELETE /interunit/transfers/{id}` | restores `pending_transfer_stock` → source |
| Delete request/transfer | `DELETE /interunit/requests/{id}`, `/interunit/transfers/{id}` | respective tables |
| Inner-cold list/delete | `GET/DELETE /cold-storage/inner-transfer/...` | cold-storage inner-transfer tables (other module) |

**Cross-module:** Inner-Cold and In-Transit-count use raw `fetch` (bypass the API service); Inner-Cold links to the **Cold Storage** module. The hover/pending data joins to **Cold Storage** (`cfpl/cdpl_cold_stocks`) and **Inward** (`bulk_entry_boxes`) as transfer sources.

---

## 14. Keyboard / click-direction notes

- **Page level:** no ESC/click-outside handlers; the only confirmations are native `window.confirm` in the 4 delete handlers.
- **Hover card:** hover-in opens (after position calc), hover-out closes after 180 ms; moving onto the card cancels close.
- **Pending modal:** ESC closes; backdrop click closes; chip click toggles filter; row challan hover opens nested hover card.

## 15. Gotchas

- "Pending" KPI counts only the loaded `requests` array (current page / up to 500), case-sensitive `'Pending'`.
- Transfer-Out and All-Transfers **share** `transfers` state; All-Transfers renders the unfiltered array (no search/warehouse filter there).
- Inner-Cold list, In-Transit count, and Transfer-In/Inner-Cold deletes use raw `fetch`, not `InterunitApiService`.
