# Transfer Dashboard — Functionality Checklist

Derived from [00-README.md](00-README.md) (module context) and [01-transfer-dashboard.md](01-transfer-dashboard.md).
Scope: the main dashboard page `/[company]/transfer` (`frontend-/app/[company]/transfer/page.tsx`) and its two embedded page-in-page components — `ChallanHoverCard` and `PendingTransfersModal`.

Use as a QA / verification / regression checklist. Each box is an independently testable behaviour.

---

## A. Route, params & access control

- [ ] Page loads at `/[company]/transfer` (e.g. `/CFPL/transfer`, `/CDPL/transfer`); `[company]` is read into `params.company`.
- [ ] `company` is used as the prefix on **every** `router.push` target (no navigation escapes the `/transfer` namespace).
- [ ] `company` is passed to the In-Transit count fetch (`?company=cfpl`) and to `<PendingTransfersModal company=… />`.
- [ ] **No page-level auth redirect** — page renders for any authenticated user; gating is per-button only.
- [ ] `canDelete` is true **only** for `yash@candorfoods.in` → gates Request / Transfer-Out / Transfer-In delete buttons.
- [ ] `canDeleteInnerCold` is true only for `hrithik` + `yash` `@candorfoods.in` → gates Inner-Cold delete.
- [ ] Modal `userRole` resolves to `"developer"` when `user.isDeveloper`, else the role from `user.companies.find(c => c.code === company)`.
- [ ] Default warehouse logic: exactly 1 default → `warehouseFilter` set to that code; >1 → sentinel `"my_warehouses"`; 0 → stays `"all"`.

## B. Layout & tabs

- [ ] Header shows title "Inter-Unit Transfer" with `ArrowRightLeft` icon + 3 action buttons (Pending Transfers / View Summary / New Request).
- [ ] Stat-card grid renders 5 cards, responsive `grid-cols-2 sm:3 lg:5`.
- [ ] Five tabs render: Requests, Transfer Out, Transfer In, Inner Cold, All Transfers (each with desktop + mobile label and icon).
- [ ] **Default active tab is `transferout`** (Transfer Out), not Requests.
- [ ] Tab switching via `setActiveTab`; active trigger styled dark; `TabsList` horizontally scrollable on small screens.
- [ ] Each tab panel renders the correct sequence: header → optional filter bar → `LoadingSkeleton` | `EmptyState` | dual render → `PaginationBar`.
- [ ] **Dual render** works: mobile card list (`md:hidden`) and desktop `<table>` (`hidden md:block`) show equivalent data.
- [ ] `PendingTransfersModal` is rendered once and controlled by `pendingModalOpen`.

## C. KPI / stat cards

- [ ] **Requests** card shows server `total` from `getRequests`.
- [ ] **Pending** card counts `requests.filter(status === 'Pending')` — confirm it is **client-side over the loaded array only** and **case-sensitive** (known gotcha).
- [ ] **Transfers Out** card shows server `total` from `getTransfers`.
- [ ] **Transfers In** card shows server `total` from `getTransferIns`.
- [ ] **In Transit** card shows `data.total` from `GET /interunit/pending-stock`; clicking it opens the Pending modal.
- [ ] `loadInTransitCount` runs on mount **and** on every modal-close; keeps the prior count on error (no flicker to 0).
- [ ] Card "N records" counts reflect **server totals**, not filtered counts.

## D. Warehouse filter

- [ ] Warehouse `Select` appears in Requests / Transfer-Out / Transfer-In filter bars; **absent** on Inner-Cold and All-Transfers.
- [ ] Options: `all`, `my_warehouses` (only when >1 default), plus one item per `getAllWarehouseCodes()` (W202, A185, A101, A68, F53, Savla D-39, Savla D-514, Rishi, Supreme).
- [ ] Display names mapped via `getDisplayWarehouseName` (e.g. `Supreme` → "Supreme Cold").
- [ ] `warehouseMatches`: `"all"` → always true.
- [ ] Comma-split candidates match (e.g. `from_cold_unit = "Rishi, Savla D-39"` matches both the Rishi and Savla D-39 filters).
- [ ] Filter applies to the correct fields per tab: Transfers (`from_warehouse, to_warehouse, from_cold_unit`), Requests (`from_warehouse, to_warehouse`), Transfer-Ins (`from_warehouse, receiving_warehouse, from_cold_unit`).

## E. Search

- [ ] Three independent search boxes (Requests / Transfer Out / Transfer In), each with a clear-`X`; **no** search on Inner-Cold or All-Transfers.
- [ ] Empty query → returns all; otherwise lowercase substring match (client-side, no redirect).
- [ ] Requests search covers: `request_no, from_warehouse, to_warehouse, request_date, status`.
- [ ] Transfer-Out search covers: `challan_no, from_warehouse, to_warehouse, from_cold_unit, stock_trf_date, status, vehicle_no, lot_numbers_text`.
- [ ] Transfer-In search covers: `grn_number, transfer_out_no, receiving_warehouse, from_warehouse, received_by, status, grn_date`.

## F. Sorting & pagination

- [ ] No user-facing sort/group controls exist.
- [ ] Server sort is fixed: transfers `created_ts desc`, transfer-ins `created_at desc`; requests / inner-cold unsorted.
- [ ] `perPage = 15` for normal server pagination.
- [ ] **Transfer-Out and All-Transfers share** `transfersPage` state; Requests / Transfer-In / Inner-Cold each have their own.
- [ ] When a filter/search is active: loader fetches `page:1, per_page:500`, sets `total_pages=1`, **hides** `PaginationBar`, filters client-side.
- [ ] `PaginationBar` shows "Showing X-Y of Z", Prev (disabled on page 1), `{page}/{tp}`, Next (disabled on last); renders **only** when `total_pages > 1`.

## G. Buttons & redirects (per row / header)

- [ ] **Pending Transfers** (header) → opens Pending modal.
- [ ] **View Summary** → `/{company}/transfer/dashboard`.
- [ ] **New Request** → `/{company}/transfer/request`.
- [ ] Requests → **View** → `/transfer/request/{id}`.
- [ ] Requests → **Accept** → `/transfer/transferform?requestId={id}`; **disabled unless status = pending**.
- [ ] Requests → **Delete** → confirm → `deleteRequest` → reload; visible only when `canDelete`.
- [ ] **Direct Transfer Out** → `/transfer/directtransferform`.
- [ ] Transfer-Out → **View** → `/transfer/view/{id}`.
- [ ] Transfer-Out → **Edit** → `/transfer/directtransferform?editId={id}`; **disabled if status Received/Completed**.
- [ ] Transfer-Out → **DC** → `/transfer/dc/{id}`.
- [ ] Transfer-Out → **Delete** → confirm → `deleteTransfer` → reload; `canDelete` only.
- [ ] **Create Transfer IN** CTA → `/transfer/transferIn`.
- [ ] Transfer-In → **Resume** (status = pending only) → `/transfer/transferIn?resume={transfer_out_no}`.
- [ ] Transfer-In → **View** → `/transfer/transferIn/{id}`.
- [ ] Transfer-In → **Delete** → confirm → raw `DELETE /interunit/transfer-in/{id}?user_email=` → reload; `canDelete` only.
- [ ] **New Transfer (Inner Cold)** → `/transfer/innercoldtransfer`.
- [ ] Inner-Cold → **Edit** → `/transfer/innercoldtransfer?editChallan={challan_no}`.
- [ ] Inner-Cold → **Delete** → confirm → raw `DELETE /cold-storage/inner-transfer/{challan}?user_email=` → reload; `canDeleteInnerCold` only.
- [ ] All-Transfers → **View** → `/transfer/view/{id}`; **DC** → `/transfer/dc/{id}`.
- [ ] Pagination **Prev/Next** load the adjacent page.
- [ ] **No whole-row onClick** — the identifier cell is a hover card; all actions are explicit buttons.

## H. Data loading & caching

- [ ] `loadRequests` → `GET /interunit/requests {page, per_page}`; failure shows "Failed to load requests."
- [ ] `loadTransfers` → `GET /interunit/transfers {…, sort_by:created_ts, sort_order:desc}`; failure toast.
- [ ] `loadTransferIns` → `GET /interunit/transfer-in {…, sort_by:created_at, sort_order:desc}`; failure toast.
- [ ] `loadInnerColdTransfers` → **raw fetch** `GET /cold-storage/inner-transfer/list`; failure toast.
- [ ] `loadInTransitCount` → raw fetch `GET /interunit/pending-stock?company=`; fails **silently** (no toast).
- [ ] Triggers: mount → `loadRequests(1)`; tab change → lazy-load only if that tab's array is empty; filter/warehouse change → reload page 1.
- [ ] **No caching** (no localStorage / SWR / TTL). Only the lazy-load guard (`array.length === 0`) and "keep prior In-Transit count on error" behave cache-like.

## I. ChallanHoverCard (page-in-page #1)

- [ ] The identifier cell in every list is a `ChallanHoverCard` — a dotted-underline blue span, **hover-only** (no click to open).
- [ ] Card renders via `createPortal` to `document.body` (escapes table overflow), `position: fixed`.
- [ ] Open computes position and (once) awaits `fetchLines`; on failure caches `[]` so it does **not** retry.
- [ ] Close after **180 ms** hover-out; moving the cursor onto the card cancels the close.
- [ ] Positioning prefers ABOVE when `spaceAbove ≥ 120 || ≥ spaceBelow`, else BELOW; clamps horizontally to viewport; width 304 (340 when discrepancies present), maxH 360.
- [ ] **Requests** hover uses static `req.lines`, `reason = status`, **no fetch**.
- [ ] **Inner Cold** hover uses static lines with `lotFrom → lotTo`, **no fetch**.
- [ ] **Transfer Out / All Transfers** hover → `GET /interunit/transfers/{id}` (Bearer token); groups boxes (or lines) by item; meta shows Vehicle/Driver (+Variance on mobile).
- [ ] **Transfer In** hover → `GET /interunit/transfer-in/{id}`; groups boxes by `article||lot`; builds discrepancies map; meta shows Received by / Condition / Issues / Unmatched / Status.
- [ ] `groupLinesByItem` aggregates by `name||lot`, sums qty/netWeight; `count = unitPackSize × qty` for PM/packaging lines; `sourceStorage = lotOriginUnit || fallbackUnit`.
- [ ] `groupBoxesByItem` uses qty=1/box; `sourceStorage` priority `lotOriginUnit` → most-common per-box `source_unit/source_storage` → `fallbackUnit`.
- [ ] Cold **"From: {sourceStorage}"** chip still shows on dispatched transfers whose `cold_stocks` rows are already consumed (fallbackUnit path).
- [ ] Item rows display: name, "{qty} boxes", "Wt: {kg} kg", "Count:" (rose, PM), "Lot:" (indigo mono), violet "From:" chip, `lotFrom→lotTo`.

## J. PendingTransfersModal (page-in-page #2)

- [ ] Opens from header **Pending Transfers** button and the **In-Transit** card; closes on ESC and backdrop click.
- [ ] `apiUrl` resolves `apiBaseUrl || NEXT_PUBLIC_API_URL || http://localhost:8000`.
- [ ] `loadData` → `GET /interunit/pending-stock?company&search&from_date&to_date`; populates records + filter options; **prunes selected chips no longer present** (anti filter-lock).
- [ ] **Auto-sync on open**: `justOpenedRef` fires `loadData()` + silent `handleSyncExisting(true)`.
- [ ] Sync POSTs `/interunit/pending-stock/backfill?user_email&user_role` **only if `canCancel`**; banners Synced / Already-in-sync / Nothing / "Sync failed: … Refreshing data anyway…".
- [ ] Sync **always** calls `loadData()` in `finally` (so a "Sync failed: Failed to fetch" during a server-reload window still loads the list).
- [ ] Filters: search + from/to date are **server-side**; warehouse chips are **client-side**; Clear-filters works.
- [ ] Totals bar sums Transfers / Total boxes / Total weight over the **filtered** records.
- [ ] Table columns: DATE, CHALLAN NO (embedded hover card → `GET /interunit/transfers/{id}`), FROM→TO (+ storage-type sublabel), BOXES, CARTONS, WEIGHT, DISPATCHED BY, STATUS, ACTION.
- [ ] Row badges: primary status (`Partial` → "Partial (GRN raised)" amber, else sky); "{unallocated_boxes} short" (rose, when >0); "Edited {date}" (violet, when `updated_ts` truthy).
- [ ] "Edited" badge fires only on genuine edits (`updated_ts` sourced from `edited_at`), not on every transfer.
- [ ] **Cancel** action gated by `canCancel` (emails `yash`, `b.hrithik`; roles `admin`, `developer`); confirm → `DELETE /interunit/transfers/{id}?user_email&user_role` → restores boxes to source → `loadData`.

## K. Keyboard / interaction

- [ ] Page level: no ESC / click-outside handlers; the only confirmations are native `window.confirm` in the 4 delete handlers.
- [ ] Hover card: hover-in opens (after position calc), hover-out closes after 180 ms, cursor-onto-card cancels close.
- [ ] Pending modal: ESC closes, backdrop click closes, chip click toggles filter, row challan hover opens the nested hover card.

## L. Backend / DB wiring (verify endpoints hit the right tables)

- [ ] Load requests → `GET /interunit/requests` → `interunit_transfer_requests*`.
- [ ] Load transfers → `GET /interunit/transfers` → `interunit_transfers_header/lines/boxes`.
- [ ] Load transfer-ins → `GET /interunit/transfer-in` → `interunit_transfer_in_header/boxes`.
- [ ] Hover (out) → `GET /interunit/transfers/{id}` → header+lines+boxes, per-lot `lot_origin_unit` from `cfpl/cdpl_cold_stocks` + `pending_transfer_stock`.
- [ ] Hover (in) → `GET /interunit/transfer-in/{id}` → transfer-in header+boxes.
- [ ] In-Transit count / Pending list → `GET /interunit/pending-stock` → `pending_transfer_stock` (+ live `interunit_transfers_header`).
- [ ] Pending auto-sync → `POST /interunit/pending-stock/backfill` → parks into `pending_transfer_stock`, deducts `cold_stocks` / `bulk_entry_boxes`.
- [ ] Cancel pending → `DELETE /interunit/transfers/{id}` → restores `pending_transfer_stock` → source.
- [ ] Delete request/transfer → `DELETE /interunit/requests/{id}`, `/interunit/transfers/{id}` → respective tables.
- [ ] Inner-cold list/delete → `GET/DELETE /cold-storage/inner-transfer/...` → Cold Storage module tables.

## M. Cross-cutting gotchas to verify (from README §"Common patterns")

- [ ] **Pending KPI** counts only the loaded `requests` array (current page / up to 500) and is case-sensitive on `'Pending'`.
- [ ] Transfer-Out and All-Transfers share `transfers` state; **All-Transfers renders the unfiltered array** (no search/warehouse filter there).
- [ ] Inner-Cold list, In-Transit count, and Transfer-In/Inner-Cold deletes use **raw `fetch`** (not `InterunitApiService`) — confirm auth headers / `localhost:8000` fallback behave correctly behind the HTTPS proxy.
- [ ] Status label vs reality: transfer-out forms submit `"Dispatch"`, but backend downgrades to `"Partial"` when scanned boxes < ordered qty — confirm the dashboard reflects the real status from the API.
- [ ] Read-only viewers reached from this page (request/transfer-out/transfer-in views) expose only "Back"; all write actions stay on this dashboard.
