# Transfer-In View — `/[company]/transfer/transferIn/[transferInId]`

| | |
|---|---|
| **File** | `d:\test\frontend-\app\[company]\transfer\transferIn\[transferInId]\page.tsx` (339 lines) |
| **URL** | `/{company}/transfer/transferIn/{transferInId}` (e.g. `/cfpl/transfer/transferIn/1234`) |
| **Purpose** | Read-only DETAIL/VIEW of a single Transfer-IN receipt (GRN). Shows header metadata (sender/receiver/date/condition), a totals summary, and the list of received boxes grouped by article. No editing happens on this page — it is a pure viewer for one `interunit_transfer_in_header` record and its `interunit_transfer_in_boxes`. |

This document covers ONLY this page and the single backend endpoint it calls (`GET /interunit/transfer-in/{id}`). Other Transfer-IN actions (receive, acknowledge, finalize, edit, generate-QRs) live on different pages/endpoints and are out of scope here.

---

## 1. Route & params

- **Dynamic segments:** two — `[company]` and `[transferInId]`. Both arrive via the Next.js App-Router `params` prop typed as `TransferInViewPageProps` (`page.tsx:16-21`).
- **Destructure:** `const { company, transferInId } = params` (`page.tsx:24`). `company` is typed `Company` (from `@/types/auth`); `transferInId` is a `string`.
- **How the id is used:** the string id is coerced with `Number(transferInId)` and passed to `InterunitApiService.getTransferInById(...)` inside the load effect (`page.tsx:35`). `company` is **only** used to build redirect URLs implicitly via `router.back()` — it is NOT passed to the API call (the backend looks the record up by primary key alone).
- **Auth / role gating:** **None on this page.** There is no `useAuthStore`, no email/role check, no redirect-if-unauthenticated guard, and no conditional rendering based on user. The component is `"use client"` and renders for anyone who reaches the route. (Auth is applied only indirectly: `InterunitApiService` attaches a Bearer token from the auth store to the request — see §11.) The backend endpoint itself (`GET /interunit/transfer-in/{id}`) has **no auth dependency** either (`interunit_server.py:636-641`).

---

## 2. Layout & structure

Root: `<div className="... max-w-5xl mx-auto space-y-4 bg-gray-50 min-h-screen">` (`page.tsx:102`). Top → bottom:

1. **Header bar** (`page.tsx:104-123`) — back button + GRN number (`Inbox` icon) + sub-line `Transfer IN — {transfer_out_no}` + status `Badge` (right-aligned).
2. **Info Cards grid** — 5 cards, `grid-cols-2 sm:grid-cols-5` (`page.tsx:126-176`): From (Sender), To (Receiver), Received By, Date, Condition.
3. **Condition Remarks card** — conditional, only if `data.condition_remarks` truthy (`page.tsx:179-186`).
4. **Totals Summary card** — 5 KPI tiles in a `grid-cols-2 sm:grid-cols-5` (`page.tsx:189-214`).
5. **"Received Items" card** (`page.tsx:217-335`):
   - `CardHeader` with `FileText` icon + title `Received Items ({totalBoxes})` (`page.tsx:218-223`).
   - `CardContent` (`p-0`) iterates `groupedBoxes` (one block per article, `page.tsx:225-327`). Each article block has: a group header strip (article name + box count, `page.tsx:228-238`), a **desktop table** (`hidden md:block`, `page.tsx:241-288`), and a **mobile card list** (`md:hidden`, `page.tsx:291-325`).
   - Empty state: `No items recorded in this transfer-in.` when `totalBoxes === 0` (`page.tsx:329-333`).

**Pre-data render states** (returned before the main layout):
- **Loading** (`page.tsx:72-86`): back button + spinner + "Loading transfer-in details…".
- **Not found** (`page.tsx:88-99`): back button + "Transfer-in not found." (rendered when `data` is null after load — e.g. fetch threw and was caught).

---

## 3. Dashboards / KPI cards / chips

### Info cards (`page.tsx:126-176`)
| Card | Label | Value | Computation |
|---|---|---|---|
| From (Sender) | `From (Sender)` | `data.from_warehouse \|\| "N/A"` | Backend JOIN `interunit_transfers_header.from_site` (`interunit_tools.py:1798-1799`, `2809`) |
| To (Receiver) | `To (Receiver)` | `data.receiving_warehouse \|\| "N/A"` | Header column |
| Received By | `Received By` | `data.received_by \|\| "N/A"` | Header column |
| Date | `Date` | `formatDate(data.grn_date)` | `grn_date` formatted `dd-mm-yyyy` (`page.tsx:47-52`) |
| Condition | `Condition` | `Badge` of `data.box_condition \|\| "N/A"` | Color-coded: Good→emerald, Damaged→red, else→orange (`page.tsx:169-173`) |

### Header status chip (`page.tsx:117-122`)
`data.status`; emerald when `"Received"`, amber otherwise (e.g. `"Pending"`).

### Totals Summary tiles (`page.tsx:189-214`)
| Tile | Label | Value | Computation (`page.tsx:66-70`) |
|---|---|---|---|
| Total Boxes | `Total Boxes` | `totalBoxes` | `data.boxes?.length \|\| 0` |
| Matched | `Matched` | `matchedBoxes` | count of boxes where `b.is_matched` truthy |
| Issues | `Issues` | `issuedBoxes` | count of boxes where `b.issue` truthy |
| Net Weight | `Net Weight` | `{totalNetWeight.toFixed(2)} kg` | `reduce(sum + (b.net_weight \|\| 0))` |
| Gross Weight | `Gross Weight` | `{totalGrossWeight.toFixed(2)} kg` | `reduce(sum + (b.gross_weight \|\| 0))` |

### Per-article group chip (`page.tsx:233-236`)
Article name + `{artBoxes.length} box(es)` count badge.

---

## 4. Tables & columns

### Desktop "Received Items" table — one per article group (`page.tsx:241-288`)
Rendered inside `hidden md:block`. Row background tint: red (`bg-red-50/30`) if the box has an issue, emerald (`bg-emerald-50/30`) if matched, else none (`page.tsx:259`).

| # | Column header | Renders (`file:line`) |
|---|---|---|
| 1 | `#` | Running index `idx + 1` (per article group, 1-based) with `Hash` icon, mono pill (`page.tsx:260-264`) |
| 2 | `Box ID` | `b.box_id \|\| "-"` (mono) (`page.tsx:265`) |
| 3 | `Transaction No` | `b.transaction_no \|\| "-"` (mono) (`page.tsx:266`) |
| 4 | `Batch / Lot` | `b.batch_number \|\| b.lot_number \|\| "-"` (mono) (`page.tsx:267`) |
| 5 | `Net Wt` (right) | `b.net_weight != null ? "{n} kg" : "-"` (`page.tsx:268`) |
| 6 | `Gross Wt` (right) | `b.gross_weight != null ? "{n} kg" : "-"` (`page.tsx:269`) |
| 7 | `Status` (center) | Badge: `Issue` (red, `AlertTriangle`) if `b.issue`; else `OK` (emerald, `CheckCircle`) if `b.is_matched`; else `—` (`page.tsx:270-282`) |

### Mobile card list — one per article group (`page.tsx:291-325`)
Rendered inside `md:hidden`. Each box is a stacked card, not a table row:
- Top line: `#{idx+1}` pill + `box_id`, plus an `Issue`/`OK` badge (no `—` fallback on mobile) (`page.tsx:297-307`).
- 2-col grid: `Trans:` (`transaction_no`), `Lot:` (`lot_number`), `Net:` (`net_weight kg`), `Gross:` (`gross_weight kg`) (`page.tsx:308-313`). NOTE: mobile uses `lot_number` only (not the `batch_number || lot_number` fallback the desktop uses).
- **Issue detail block** (mobile-only, `page.tsx:314-321`): when `hasIssue && issueData`, shows a red panel with `Actual Qty` (`issueData.actual_qty`), `Actual Wt` (`issueData.actual_total_weight`), `Remarks` (`issueData.remarks`). Each line conditional on the field being present. This block has **no desktop equivalent** — issue details are only visible on mobile.

---

## 5. Buttons

| Label | Line | Handler | Action / Redirect |
|---|---|---|---|
| Back (`ArrowLeft` icon, loading state) | `page.tsx:76` | `onClick={() => router.back()}` | Browser back — returns to previous page (typically the transfer dashboard) |
| Back (`ArrowLeft` icon, not-found state) | `page.tsx:92` | `onClick={() => router.back()}` | Browser back |
| Back (`ArrowLeft` icon, main header) | `page.tsx:105` | `onClick={() => router.back()}` | Browser back |

There are **no other buttons** — no Edit, Print, QR, Finalize, Delete, or Acknowledge controls on this view page. It is read-only. (Those actions exist elsewhere in the module against other endpoints.)

---

## 6. Pagination

**None.** All boxes for the receipt are loaded in a single fetch and rendered in full (grouped by article). There is no page/per-page state, no infinite scroll, and no server-side paging on this route. (The backend `get_transfer_in` returns every box via `_fetch_transfer_in_boxes`, `interunit_tools.py:1823-1836`.)

---

## 7. Page-in-page & hover actions

**None.** No modals, dialogs, drawers, popovers, hover cards, or tooltips (beyond a native `title` attr elsewhere — none here). No `Dialog`/`Sheet`/`HoverCard` imports. The issue details on mobile (`page.tsx:314-321`) are rendered inline, not in any overlay. Nothing fetches on hover/click beyond the initial load.

---

## 8. Keyboard / ESC / click directions

**None.** No `onKeyDown`/`onKeyUp` handlers, no ESC handling, no focus trapping, no global key listeners. The only interaction is clicking the Back button (`router.back()`). There are no clickable rows, links into other records, or click-to-expand affordances.

---

## 9. Functionality & logic flows

**Component:** `TransferInViewPage` (`page.tsx:23`), default export, `"use client"`.

**State (`page.tsx:28-29`):**
- `data: any` — the full transfer-in detail object (header fields + `boxes[]`), initially `null`.
- `loading: boolean` — initially `true`.

**Data loading (`useEffect`, `page.tsx:31-45`):**
1. Sets `loading = true`.
2. `await InterunitApiService.getTransferInById(Number(transferInId))`.
3. On success → `setData(result)`.
4. On error → `console.error` + destructive toast `"Failed to load transfer-in details"` (`page.tsx:37-39`); `data` stays `null` → renders the "not found" state.
5. `finally` → `loading = false`.
6. Dependency array `[transferInId, toast]` — refetches if the id changes.

**Helper `formatDate` (`page.tsx:47-52`):** `new Date(d).toLocaleDateString("en-GB", {2-digit dmy})` then `.replace(/\//g, "-")` → `dd-mm-yyyy`; returns `"N/A"` on null or throw.

**Computed value `groupedBoxes` (`useMemo`, `page.tsx:55-64`):** builds `Record<article, box[]>` from `data.boxes`, keying on `b.article || "Unknown"`. Memoized on `[data]`. Drives the per-article rendering loop (`page.tsx:225`).

**Derived totals (recomputed every render, `page.tsx:66-70`):** `totalBoxes`, `matchedBoxes`, `issuedBoxes`, `totalNetWeight`, `totalGrossWeight` — see §3 for formulas.

**Per-row issue parsing (`page.tsx:256-257`, `293-294`):** `issueData = typeof b.issue === "string" ? JSON.parse(b.issue) : b.issue`. Handles both a JSON-string and an already-parsed object. NOTE: `issueData` is computed in the desktop branch (`page.tsx:257`) but only actually consumed in the mobile branch; in the desktop table the `issue` presence drives only the badge.

**Print / QR:** **None on this page.** No print stylesheet, `window.print()`, QR generation, or barcode rendering.

---

## 10. Redirects

**Out (navigation away):**
- `router.back()` — three places, all three Back buttons (`page.tsx:76`, `92`, `105`). Returns to the prior history entry (no hard-coded target). No `router.push` anywhere on this page.

**In (how users arrive here):** from the main transfer dashboard `d:\test\frontend-\app\[company]\transfer\page.tsx`:
- `router.push(`/${company}/transfer/transferIn/${ti.id}`)` at `transfer/page.tsx:1110` (a "view" outline button) and `transfer/page.tsx:1290` (a `View`-titled icon button).

There are **no outward `router.push` redirects** from the view page itself (e.g. to edit, to the source transfer-out, or to cold storage).

---

## 11. API calls

| Method | Endpoint | Params | Purpose |
|---|---|---|---|
| GET | `{API_BASE_URL}/interunit/transfer-in/{transferInId}` | path: `transferInId` (number) | Fetch the full transfer-in detail (header + boxes). Called via `InterunitApiService.getTransferInById(Number(transferInId))` |

Details:
- **Service method:** `getTransferInById(transferInId: number)` → `fetchJSON(`${API_BASE_URL}/interunit/transfer-in/${transferInId}`)` (`d:\test\frontend-\lib\interunitApiService.ts:504-506`).
- **`API_BASE_URL`** = `process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'` (`interunitApiService.ts:4`). Full path therefore: `{NEXT_PUBLIC_API_URL}/interunit/transfer-in/{id}`.
- **Auth header:** `fetchJSON` calls `getAuthHeaders()` which attaches `Authorization: Bearer <accessToken>` from the auth store when present (`interunitApiService.ts:82-99`); always sends `Content-Type: application/json`.
- **Error handling:** non-OK responses throw an `Error` carrying `.response.{data,status,detail}` (`interunitApiService.ts:100-129`); the page's `catch` surfaces `err.message` in a toast.
- This is the **only** network call the page makes — one GET on mount. No POST/PUT/DELETE.

---

## 12. Backend & DB wiring touched

The single endpoint resolves to `get_transfer_in_endpoint` (`interunit_server.py:636-641`, router prefix `/interunit` at `interunit_server.py:65`), which delegates to `get_transfer_in(transfer_in_id, db)` in `interunit_tools.py:2801-2825`. Response model `TransferInDetail` (`interunit_models.py:457-459`).

**Tables READ (no writes from this endpoint — it is a pure SELECT):**

1. **`interunit_transfer_in_header`** (`interunit_tools.py:2804-2812`) — the receipt header. Columns selected: `id, transfer_out_id, transfer_out_no, grn_number, grn_date, receiving_warehouse, received_by, received_at, box_condition, condition_remarks, status, inward_transaction_no, created_at, updated_at`. Mapped to the response by `_map_transfer_in_header` (`interunit_tools.py:1780-1800`). 404 raised if no row (`interunit_tools.py:2817-2818`).
2. **`interunit_transfers_header`** (LEFT JOIN, `interunit_tools.py:2811`) — the source transfer-OUT header. Only `t.from_site AS from_warehouse` is pulled; surfaces as the "From (Sender)" card. Join key: `h.transfer_out_id = t.id`.
3. **`interunit_transfer_in_boxes`** (`interunit_tools.py:1823-1836`, called at `2820`) — the per-box detail. Columns: `id, header_id, box_id, article, batch_number, lot_number, transaction_no, net_weight, gross_weight, scanned_at, is_matched, transfer_out_box_id, issue, line_index, inward_box_id`. Ordered by `scanned_at`. Mapped by `_map_transfer_in_box` (`interunit_tools.py:1803-1820`). Drives the article-grouped tables and all totals.

**Not touched by this endpoint:** `pending_transfer_stock`, `cfpl_cold_stocks` / `cdpl_cold_stocks`, `interunit_transfer_boxes` (transfer-out boxes). Those are read/written by sibling endpoints (acknowledge, finalize, edit, reconciliation) but NOT by the view path. The `box_condition` color logic and `issue` JSON shape (`{actual_qty, actual_total_weight, remarks}`) are documented in `interunit_models.py:318`.

---

## 13. Cross-module linkages

These are **data-level** links present in the fetched payload but **not surfaced as UI navigation** on this read-only page:

- **Source Transfer-OUT:** `data.transfer_out_id` / `data.transfer_out_no` (header sub-line, `page.tsx:114`) and the JOIN to `interunit_transfers_header` for `from_warehouse`. The page does NOT link back to the transfer-out record.
- **Cold Storage:** the boxes ultimately land in `cfpl/cdpl_cold_stocks` (keyed by `box_id` + `transaction_no`) on finalize/edit, but this view does NOT query or render cold-stock state — `transaction_no` is shown as a plain column only.
- **Inward module:** the header carries `inward_transaction_no` and each box carries `inward_box_id` (both selected by the backend, `interunit_tools.py:1819`, `1795`, `2807`), indicating linkage to the Inward/IMS aggregate flow. **Neither field is rendered anywhere on this page** — they are fetched but unused by the UI.
- **QR scheme:** QR codes for boxes are produced by a separate endpoint (`POST /transfer-in/{header_id}/generate-qrs`, `interunit_server.py:392`). This view does not generate, display, or print QR codes.
- **STBR (scan-time box-id reconciliation):** the `issue` field and `is_matched` flag reflect reconciliation outcomes (see `interunitApiService.ts:6-12` for `ReconciliationStatus`), but this page only displays the resulting flags; it does not invoke the reconciliation endpoint (`GET /transfer-in/{id}/reconciliation`, `interunit_server.py:403`).

---

## 14. Gotchas

1. **`data` is `any` (`page.tsx:28`)** — no typing against `TransferInDetail`. Field-name drift between backend and page (e.g. a renamed column) would fail silently as `undefined`/`"N/A"` rather than a type error.
2. **Mobile vs desktop divergence:** desktop "Batch / Lot" column shows `batch_number || lot_number` (`page.tsx:267`); mobile shows `lot_number` only (`page.tsx:310`). **Issue details (`actual_qty`/`actual_total_weight`/`remarks`) are visible ONLY on mobile** (`page.tsx:314-321`) — a desktop user with `md:` breakpoint sees just the red "Issue" badge with no detail.
3. **Dead variable:** `issueData` is computed in the desktop map (`page.tsx:257`) but never used there — only the mobile branch consumes it.
4. **`issue` may be string or object:** parsed defensively with `JSON.parse` (`page.tsx:257`, `294`). A malformed JSON string would throw inside render and could blank the table — there is no try/catch around the parse.
5. **No auth/role gate (§1):** anyone reaching the URL renders the page; the backend endpoint also has no auth dependency. Access control relies entirely on the route being unlinked from unauthorized navigation, not on enforcement.
6. **`router.back()` is non-deterministic:** if the page is opened directly (deep link / refresh) with no history, Back may do nothing or leave the app. There is no explicit fallback to `/{company}/transfer`.
7. **Per-article index resets:** the `#` column is `idx+1` **within each article group** (`page.tsx:262`, `299`), not a global box number — two boxes in different articles can both show `#1`.
8. **Status default:** backend defaults `status` to `"Received"` when null (`interunit_tools.py:1792`); the page treats anything not exactly `"Received"` (e.g. `"Pending"`) as the amber state (`page.tsx:118`).
9. **`from_warehouse` only present when JOIN matches:** `_map_transfer_in_header` only adds `from_warehouse` to the dict if the JOIN returned a truthy value (`interunit_tools.py:1798-1799`); otherwise the field is absent and the card shows `"N/A"`.
