# Transfer Details (Transfer-OUT View) — `/[company]/transfer/view/[transferId]`

| | |
|---|---|
| **File** | `d:\test\frontend-\app\[company]\transfer\view\[transferId]\page.tsx` |
| **URL** | `/{company}/transfer/view/{transferId}` (e.g. `/cfpl/transfer/view/123`) |
| **Component** | `TransferViewPage` (default export, `"use client"`) — `page.tsx:73` |
| **Purpose** | Read-only detail view of a single Transfer-OUT (interunit dispatch). Loads the transfer by numeric id and renders header info, consolidated item cards, and per-box scanned cards with weight rollups. Pure display — no edit, no actions, no DC print. |

---

## 1. Route & params

- Next.js App-Router dynamic segment page; props typed as `TransferViewPageProps` (`page.tsx:13-18`).
- `params.company: Company` and `params.transferId: string` are destructured at `page.tsx:74`.
- `transferId` is the **numeric DB id** of `interunit_transfers_header.id` (not the challan_no). It is passed straight into the fetch URL (`page.tsx:91`) and is the only dependency of the load `useEffect` (`page.tsx:82-84`).
- `company` is used **only** for back-navigation redirects (`page.tsx:217`, `page.tsx:237`). It is NOT sent to the API — the GET-by-id endpoint is company-agnostic (resolves company from the row itself).

---

## 2. Layout & structure

Top-level wrapper: `<div className="p-3 sm:p-4 lg:p-6 space-y-4 bg-gray-100 min-h-screen">` (`page.tsx:230`).

Render order (top → bottom):
1. **Header bar** — Back button + title + status badge (`page.tsx:232-251`).
2. **Transfer Information card** — header field grid + summary stats (`page.tsx:254-375`).
3. **Items Details card** — consolidated item cards, conditionally rendered when `consolidatedLines.length > 0` (`page.tsx:378-493`).
4. **Scanned Boxes Details card** — per-box cards + boxes summary, conditionally rendered when `transfer.boxes.length > 0` (`page.tsx:496-616`).
5. **No Boxes Scanned empty-state card** — mutually exclusive with #4, shown when no boxes (`page.tsx:619-629`).

Two earlier early-return branches before the main render:
- **Loading state** (`page.tsx:199-208`): centered `Loader2` spinner + "Loading transfer details...".
- **Not-found state** (`page.tsx:210-227`): `transfer === null` after load → "Transfer not found" card with a Back-to-Transfers button.

Imported UI primitives (all from local shadcn-style components): `Card`/`CardContent`/`CardHeader`/`CardTitle` (`@/components/ui/card`, `page.tsx:5`), `Button` (`page.tsx:6`), `Badge` (`page.tsx:7`). Icons from `lucide-react` (`page.tsx:8`): `ArrowLeft`, `Package`, `Truck`, `User`, `Calendar`, `MapPin`, `FileText`, `Loader2`. `useToast` from `@/hooks/use-toast` (`page.tsx:9`) drives error toasts only.

Header field grid layout: `grid-cols-1 md:grid-cols-2 lg:grid-cols-3` (`page.tsx:259`). Item-detail inner grid: `grid-cols-2 md:grid-cols-3 lg:grid-cols-4` (`page.tsx:413`). Box card grid: `grid-cols-1 md:grid-cols-2 lg:grid-cols-3` (`page.tsx:510`).

---

## 3. KPI cards / chips / status & variance display

**Status badge** — `getStatusBadge(status)` (`page.tsx:128-147`), rendered once in the header at `page.tsx:249`. Lower-cases the status then switch-maps (colors are Tailwind classes):

| Status (input, lowercased) | Rendered label | Badge color |
|---|---|---|
| `pending` | Pending | yellow |
| `approved`, `accept` | Approved | green |
| `in transit` | In Transit | blue |
| `partially transferred`, `partiallytransferred`, `partial` | Partially Transferred | orange |
| `completed`, `dispatch` | Dispatch | yellow |
| (any other) | raw `status` string | default Badge |

**Summary stats (Transfer Information card)** — `page.tsx:362-372`, a 2-col grid:
- **Items** = `consolidatedLines.length` (blue card) — `page.tsx:366`.
- **Boxes Scanned** = `transfer.boxes?.length || 0` (green card) — `page.tsx:370`.

**Boxes Summary (Scanned Boxes card)** — `page.tsx:587-613`, a `grid-cols-2 md:grid-cols-4`:
- **Total Boxes** = `transfer.boxes.length` (`page.tsx:592`).
- **Total Net Weight** = `Σ parseFloat(box.net_weight)`, `.toFixed(2)` kg (`page.tsx:596-598`).
- **Total Gross Weight** = `Σ parseFloat(box.gross_weight)`, `.toFixed(2)` kg (`page.tsx:602-604`).
- **Avg Weight/Box** = total net / `boxes.length`, `.toFixed(2)` kg (`page.tsx:608-610`).

**Per-item chips** (inside each item card header, `page.tsx:396-401`): `Item #{n}` badge, a `material_type` outline badge, and — when the consolidation merged >1 line — a `{_box_count} boxes` amber badge.

**Per-box chip** (`page.tsx:519`): static green `Scanned` badge on every box card.

**Variance display — NONE (gotcha).** The `TransferDetail` interface declares `has_variance: boolean` (`page.tsx:42`) and the backend populates it (`interunit_tools.py:527`), but **this page never reads or renders `has_variance`** anywhere. There is no variance chip, banner, or column on this view. Variance is surfaced on the Transfer-IN reconciliation pages, not here.

**Cold-unit / lot-origin chip — NONE (gotcha).** Backend attaches `lot_origin_unit` to every box and line and `source_unit`/`source_storage` to boxes (`interunit_tools.py:1359-1361`, `interunit_tools.py:553-554`), and the header carries `from_cold_unit` (`interunit_tools.py:528`). **This page renders none of them** — no "From: Savla D-39" chip. Those fields are not in the local `TransferDetail` type either. See §10/§14.

---

## 4. Tables & columns

There are **no HTML `<table>` elements** on this page. Both lines and boxes are rendered as **card grids**, not tables. Documented below as their card "columns" (label → value source), since each is a fixed field set.

### 4a. Items Details — consolidated item card (`page.tsx:390-489`)

Iterates `consolidatedLines` (see §9 for the consolidation). Each card shows:

| Field (label) | Value source | Line | Conditional |
|---|---|---|---|
| Item # | `index + 1` | `page.tsx:397` | always |
| Material Type (header badge) | `line.material_type` | `page.tsx:398` | always |
| `{n} boxes` badge | `line._box_count` | `page.tsx:399-401` | only if `_box_count > 1` |
| Item Description (prominent block) | `line.item_description` | `page.tsx:407-410` | always |
| Material Type | `line.material_type` | `page.tsx:415-418` | always |
| Category | `line.item_category` | `page.tsx:421-424` | always |
| Sub Category | `line.sub_category` | `page.tsx:427-430` | always |
| Quantity | `line.quantity` | `page.tsx:433-436` | always |
| UOM | `line.uom` | `page.tsx:439-442` | always |
| Pack Size (gm/Kg) | `line.pack_size`; unit label = `gm` if FG else `Kg` | `page.tsx:445-448` | always |
| Unit Pack Size/Count | `line.unit_pack_size` | `page.tsx:451-456` | only if set and `!== '0'` |
| Net Weight (Kg) | `line.net_weight` + " kg" | `page.tsx:459-462` | always |
| Total Weight (Kg) | `line.total_weight` + " kg" | `page.tsx:465-468` | always |
| Batch Number | `line.batch_number` | `page.tsx:471-476` | only if truthy |
| Lot Number | `line.lot_number` | `page.tsx:479-484` | only if truthy |

`isFG` flag = `line.material_type?.toUpperCase() === 'FG'` (`page.tsx:391`) — drives the Pack Size unit label only.

### 4b. Scanned Boxes Details — per-box card (`page.tsx:511-583`)

Iterates `transfer.boxes` (raw, **not** consolidated; key = `box.id`). Each card shows:

| Field (label) | Value source | Line | Conditional |
|---|---|---|---|
| Box # (header) | `box.box_number` | `page.tsx:517` | always |
| `Scanned` badge | static | `page.tsx:519` | always |
| Article / Item | `box.article` | `page.tsx:525-528` | always |
| Lot Number | `box.lot_number || 'N/A'` | `page.tsx:533-536` | always |
| Box ID | `box.box_id || 'N/A'` | `page.tsx:539-542` | always |
| Batch Number | `box.batch_number || 'N/A'` | `page.tsx:545-548` | always |
| Transaction No | `box.transaction_no || 'N/A'` | `page.tsx:551-554` | always |
| Net Weight (blue) | `box.net_weight` + " kg" | `page.tsx:560-563` | always |
| Gross Weight (purple) | `box.gross_weight` + " kg" | `page.tsx:566-569` | always |
| Scanned: {date} | `formatDate(box.created_at)` | `page.tsx:573-579` | only if `box.created_at` truthy |

---

## 5. Buttons

| Label | Location (line) | Handler | Action / Redirect |
|---|---|---|---|
| **Back** (`ArrowLeft`) | header bar, `page.tsx:234-242` | inline `onClick` | `router.push('/{company}/transfer')` (`page.tsx:237`) |
| **Back to Transfers** (`ArrowLeft`) | not-found state, `page.tsx:216-223` | inline `onClick` | `router.push('/{company}/transfer')` (`page.tsx:217`) |

That is the complete set. **There is NO Print, NO Delivery Challan (DC), NO Edit, NO Delete, NO Approve, and NO Receive button on this page.** Printing the DC is a separate sibling page (see §13). Edit/delete live on the dashboard/list page.

---

## 6. Pagination

**None.** All lines and all boxes are rendered at once. No `page`/`per_page`, no infinite scroll, no "show more". For large transfers every box card renders eagerly.

---

## 7. Page-in-page & hover actions

- **Page-in-page: None.** No modals, no drawers, no tabs, no nested routers. Single flat scrollable page.
- **Hover actions: None functional.** The only hover effect is cosmetic: box cards have `hover:shadow-md transition-shadow` (`page.tsx:512`). No hover card, tooltip, popover, or click-to-expand. (Contrast with the dashboard's pending-transfer hover card that consumes the backend's `lot_origin_unit`/`grn_records` — those backend extras are unused here.)

---

## 8. Keyboard / click directions

- **Clicks:** only the two Back buttons (§5). Item cards and box cards are **not clickable** — no row drill-down, no navigation into a box/lot.
- **Keyboard:** no custom key handlers, no shortcuts, no focus traps. Default browser tab/enter behavior on the two buttons only.

---

## 9. Functionality & logic flows

### Load by id (`page.tsx:82-125`)
- `useEffect` on mount and whenever `transferId` changes → calls `loadTransferDetails()` (`page.tsx:82-84`).
- `loadTransferDetails` (`page.tsx:86-125`): sets `loading=true`, builds `url = ${API_BASE_URL}/interunit/transfers/${transferId}` where `API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'` (`page.tsx:90-91`).
- Plain `fetch` (no shared `InterunitApiService`) with `Accept`/`Content-Type: application/json` headers (`page.tsx:93-98`). Non-OK response throws (`page.tsx:100-102`).
- On success: heavy `console.log` debug dump of the full payload and per-box keys (`page.tsx:106-112`), then `setTransfer(data)` (`page.tsx:114`).
- On error: `console.error` + destructive `toast` (`page.tsx:116-121`). `finally` always clears `loading` (`page.tsx:122-124`).

### `consolidatedLines` (`page.tsx:175-197`) — the core aggregation
- `useMemo` keyed on `transfer?.lines`.
- Groups lines by composite key `desc__cat__packsize`, where `desc`/`cat` are trimmed + UPPER-cased and `pack_size` defaults to `'0'` (`page.tsx:180-183`).
- On a merge: sums `quantity` (numeric add, kept as string), sums `net_weight` and `total_weight` (`.toFixed(3)`), and increments `_box_count` (`page.tsx:185-190`).
- First occurrence: stored with `_box_count: 1` (`page.tsx:192`).
- Returns `Array.from(lineMap.values())` (`page.tsx:196`). Drives the Items grid AND the "Items" KPI count.

### Boxes
- Rendered **raw** from `transfer.boxes` (no consolidation) — each scanned box gets its own card (`page.tsx:511`).
- Box-summary weights computed inline via `reduce` (§3).

### Lot-origin / cold-unit display
- **Not implemented on the frontend.** The backend computes a per-lot dominant cold sub-unit (`lot_origin_unit`) and per-box `source_unit`/`source_storage` (see §12), but this page neither types nor renders them. Lot is shown only as a plain `Lot Number` text field on items and boxes (§4).

### Date formatting (`formatDate`, `page.tsx:150-172`)
- Returns `'N/A'` for empty (`page.tsx:151`).
- If already `DD-MM-YYYY`, returns unchanged (`page.tsx:153-156`).
- Otherwise `new Date(...)` → `toLocaleDateString('en-GB', {2-digit day/month, numeric year})` with `/`→`-` (`page.tsx:159-168`). On parse failure returns the raw string. Used for Transfer Date (`page.tsx:286`) and box Scanned date (`page.tsx:576`).

### Field fallbacks (dual-key tolerance)
The page reads several fields with `||` fallbacks because header/list payload shapes differ:
- Transfer Number: `challan_no || transfer_no` (`page.tsx:245`, `266`).
- Transfer Date: `stock_trf_date || transfer_date` (`page.tsx:286`).
- From/To Warehouse: `from_site || from_warehouse`, `to_site || to_warehouse` (`page.tsx:295`, `304`).
- Vehicle: `vehicle_no || vehicle_number` (`page.tsx:313`).

Conditionally-rendered header fields (hidden when falsy): Request Number (`page.tsx:270`), Driver Name (`page.tsx:317`), Approval Authority (`page.tsx:328`), Created By (`page.tsx:339`), Reason (`page.tsx:350`).

Warehouse names are run through `getDisplayWarehouseName()` (`page.tsx:295`, `304`) from `@/lib/constants/warehouses` (`page.tsx:11`), which normalizes aliases (e.g. `savla bond` → `Savla D-39`) and applies display overrides (`Supreme` → `Supreme Cold`) — see `warehouses.ts:204-208`, `warehouses.ts:179-192`.

---

## 10. Redirects

| Trigger | Destination | Line |
|---|---|---|
| Click **Back** (header) | `/{company}/transfer` | `page.tsx:237` |
| Click **Back to Transfers** (not-found state) | `/{company}/transfer` | `page.tsx:217` |

Both via `router.push` (`useRouter` from `next/navigation`, `page.tsx:4`/`75`). No automatic redirects on load/error (errors stay on-page with a toast).

---

## 11. API calls

| Verb | Endpoint | Params | Purpose | FE line / BE line |
|---|---|---|---|---|
| GET | `/interunit/transfers/{transferId}` | path: `transferId` (numeric header id) | Load full transfer (header + lines + boxes + backend extras) by id. Backend handler `get_transfer_endpoint` → `get_transfer`. | FE `page.tsx:91-98`; BE route `interunit_server.py:268-273`; impl `interunit_tools.py:1287` |

- Single API call. Built and fired manually with `fetch` — **does not** use `InterunitApiService.getTransferById` (the DC sibling page does, `dc/[transferId]/page.tsx:28`). Router prefix `/interunit` confirmed at `interunit_server.py:65`.
- Response model `TransferWithLines`. Fields consumed by this page: header scalars, `lines[]`, `boxes[]`. Fields returned but **ignored** by this page: `from_cold_unit`, per-row `lot_origin_unit`, per-box `source_unit`/`source_storage`, `grn_records[]`, `has_variance`.

---

## 12. Backend & DB wiring touched

Handler chain: `interunit_server.py:268` → `interunit_tools.py:1287 get_transfer()`.

- **`interunit_transfers_header h`** (`interunit_tools.py:1289-1301`): selects `id, challan_no, stock_trf_date, from_site, to_site, vehicle_no, driver_name, approved_by, remark, reason_code, status, request_id, created_by, created_ts, approved_ts, has_variance, from_cold_unit`. `LEFT JOIN interunit_transfer_requests r ON h.request_id = r.id` to pull `r.request_no`. 404 if no row (`interunit_tools.py:1303-1304`). Mapped by `_map_transfer_header` (`interunit_tools.py:509-529`) — note `stock_trf_date` is formatted `%d-%m-%Y`, which is why `formatDate` short-circuits on `DD-MM-YYYY`.
- **`interunit_transfers_lines`** (`_fetch_transfer_lines`, `interunit_tools.py:558-571`): `WHERE header_id = :hid ORDER BY id`. Columns `rm_pm_fg_type, item_category, sub_category, item_desc_raw, pack_size, qty, uom, unit_pack_size, net_weight, total_weight, batch_number, lot_number`. Mapped by `_map_transfer_line` (`interunit_tools.py:488-506`) — `rm_pm_fg_type`→`material_type`, `item_desc_raw`→`item_description`, `qty`→`quantity`.
- **`interunit_transfer_boxes itb`** (`_fetch_boxes`, `interunit_tools.py:574-607`): `WHERE header_id = :hid ORDER BY box_number`. `LEFT JOIN pending_transfer_stock pts ON pts.box_id = itb.box_id AND pts.status = 'In Transit'` to derive `source_storage` (`cold_storage_data->>'storage_location'`) and a SQL-normalized `source_unit` (`cold_storage_data->>'unit'` canonicalized to Savla D-39 / Savla D-514 / Rishi / Supreme Cold). Mapped by `_map_box_row` (`interunit_tools.py:532-555`). (These box source fields are unused by this page.)
- **Per-lot dominant cold unit** (`interunit_tools.py:1310-1363`): collects distinct non-empty lot numbers from boxes+lines, then a CTE over `cfpl_cold_stocks` ∪ `cdpl_cold_stocks` ∪ `pending_transfer_stock.cold_storage_data->>'unit'`, normalizes raw unit strings, ranks by row-count per lot, and attaches the winning `lot_origin_unit` to every box AND line. Wrapped in try/except → logs a warning on failure (does not break the response). **Frontend ignores this.**
- **GRN records** (`interunit_tools.py:1365-1392`): `interunit_transfer_in_header tih LEFT JOIN interunit_transfer_in_boxes tib` where `tih.transfer_out_id = :tid`, returns `grn_records[]`. **Frontend ignores this.**

---

## 13. Cross-module linkages

- **Transfer list / dashboard** (`/{company}/transfer`, `frontend-\app\[company]\transfer\page.tsx`): the only navigation target (both Back buttons). The list page is presumably what links *into* this view via the `transferId`.
- **Delivery Challan (DC) print page** (`/{company}/transfer/dc/[transferId]`, `frontend-\app\[company]\transfer\dc\[transferId]\page.tsx`): sibling route that fetches the **same** transfer via `InterunitApiService.getTransferById(company, transferId)` (`dc/[transferId]/page.tsx:28`) and renders the printable `DeliveryChallan` component (`@/components/transfer/DeliveryChallan`). This view page does **not** link to it.
- **Transfer-IN / GRN module** (`interunit_transfer_in_*` tables): related via `grn_records` in the payload and the reconciliation endpoints in `interunit_server.py`, but not surfaced on this page.
- **Cold-storage module** (`cfpl_cold_stocks` / `cdpl_cold_stocks` / `pending_transfer_stock`): joined server-side to attribute lot origin and box source, but unused in this view.
- **Warehouse constants** (`@/lib/constants/warehouses`): `getDisplayWarehouseName` used for From/To display normalization.

---

## 14. Gotchas

1. **`has_variance` is fetched but never displayed.** Despite being in the local type (`page.tsx:42`) and the payload (`interunit_tools.py:527`), there is no variance UI on this page. Anyone expecting a variance flag here will not find one — it lives on Transfer-IN reconciliation.
2. **Cold sub-unit / lot-origin chip is missing.** Backend does substantial work to compute `lot_origin_unit`, per-box `source_unit`/`source_storage`, and header `from_cold_unit` (§12), but the frontend type omits them and the JSX never renders them. Lot is shown as bare text only. If the requirement is "show which cold unit the lot came from," it is wired in the API but not in this view.
3. **Two competing field names per concept.** Header fields are read with `||` fallbacks (`challan_no || transfer_no`, `stock_trf_date || transfer_date`, `from_site || from_warehouse`, `vehicle_no || vehicle_number`). The GET-by-id backend only ever returns the *second-listed* warehouse keys (`from_warehouse`/`to_warehouse` via `_map_transfer_header`) and `stock_trf_date`/`vehicle_no`; the first-listed keys (`from_site`, `transfer_no`, `transfer_date`, `vehicle_number`) come from a *different* (list) payload shape — so the fallbacks exist to tolerate both sources.
4. **Lines are consolidated; boxes are not.** The "Items" KPI counts `consolidatedLines` (deduped by desc+cat+pack_size), while "Boxes Scanned" counts raw boxes. Item count will usually be far smaller than box count; they are not directly comparable.
5. **Consolidation key uppercases & trims desc/cat but not pack_size.** Two lines with the same item but differing pack sizes stay separate; differing only by whitespace/case in description merge. Quantities are summed as parsed floats then re-stringified — non-numeric quantities silently become `0`.
6. **Manual `fetch`, not the shared service.** This page bypasses `InterunitApiService` (used everywhere else, including the sibling DC page) and hardcodes the URL with a `localhost:8000` fallback. If `NEXT_PUBLIC_API_URL` is unset in prod, it silently targets localhost.
7. **Heavy `console.log` debug left in production code** (`page.tsx:106-112`) — dumps the entire transfer payload (incl. every box) to the browser console on each load.
8. **No DC/print button despite a DC page existing.** Users must reach `/transfer/dc/{id}` by some other path; this detail view offers no link to print the challan.
9. **`box.box_id` and several box fields fall back to `'N/A'`** in the UI (`page.tsx:535-553`); backend already coalesces many to `""`, so `'N/A'` only appears for genuinely null lot/batch/transaction values.
10. **No auth/permission gating in the component.** Unlike delete (which has `_check_delete_permission`, `interunit_server.py:292`), this read view performs no role check client-side and sends no user identity to the API.
