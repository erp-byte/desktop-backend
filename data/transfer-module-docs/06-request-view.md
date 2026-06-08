# Request Details — `/[company]/transfer/request/[requestId]`

- **File:** `d:\test\frontend-\app\[company]\transfer\request\[requestId]\page.tsx` (373 lines)
- **URL:** `/{company}/transfer/request/{requestId}` (e.g. `/candor/transfer/request/482`)
- **Purpose:** Read-only detail/view page for a single inter-unit transfer **Request**. Loads one request by its numeric `id`, renders the request header (route, dates, status, reason, reject reason), summary stats (total items + total net weight), and a per-line item breakdown. It contains **no** approve / reject / edit / delete actions — those live on the Transfer list/dashboard page. The only navigation control is a "Back to Transfers" button.

> Component: `RequestViewPage` — `"use client"` (page.tsx:1, default export at page.tsx:21).

---

## 1. Route & params

- **Props shape** (page.tsx:14-19): `params: { company: Company; requestId: string }`. `company` is typed as `Company` from `@/types/auth` (page.tsx:11); `requestId` is a route string.
- **Destructure** (page.tsx:22): `const { company, requestId } = params`.
- **id read & use:**
  - On mount and whenever `requestId` changes, `loadRequestDetails()` runs via `useEffect` with dependency `[requestId]` (page.tsx:29-31).
  - The id is coerced to a number for the API call: `InterunitApiService.getRequest(Number(requestId))` (page.tsx:36). No validation/`isNaN` guard on the coercion — a non-numeric route segment would send `NaN` to the API (see Gotchas).
  - `company` is **only** used to build "Back" redirect URLs (`/${company}/transfer`) at page.tsx:117 and page.tsx:137; it is never sent to the API.
- **Auth / role gating:** **None on this page.** There is no role check, no `useAuthStore`/permission read, and no `canDelete`/`canApprove` flag. Any authenticated user who can reach the route sees the full read-only view. (Auth token injection happens transparently inside the API service via `getAuthHeaders()` — see §11/§12.) The page is purely a viewer, so there are no gated actions to protect.

---

## 2. Layout & structure

Top-to-bottom (within the success render branch, page.tsx:129-371):

1. **Page wrapper** (page.tsx:130): `div.p-3 sm:p-4 lg:p-6 space-y-4 bg-gray-100 min-h-screen`.
2. **Header row** (page.tsx:132-149): flex `justify-between`.
   - Left: "Back" button (`ArrowLeft` icon) + title block ("Request Details" `<h1>` + `request.request_no` subtitle).
   - Right: status `Badge` via `getStatusBadge(request.status)` (page.tsx:148).
3. **Request Information `Card`** (page.tsx:152-264):
   - `CardHeader` titled "Request Information" (page.tsx:153-155).
   - `CardContent` with a responsive info grid `grid-cols-1 md:grid-cols-2 lg:grid-cols-3` (page.tsx:157) holding labeled fields.
   - "Summary Stats" sub-block (page.tsx:249-262): two centered stat tiles in a `grid-cols-2`.
4. **Items List `Card`** (page.tsx:267-356): rendered only when `request.lines?.length > 0`. Title "Items Details (N)" with `Package` icon. Body is a `space-y-4` stack of one nested `Card` per line item.
5. **"No Items" empty-state `Card`** (page.tsx:359-369): rendered only when there are zero lines.

**Render branches before the main layout:**
- **Loading** (page.tsx:99-108): centered `Loader2` spinner + "Loading request details...".
- **Not found** (page.tsx:110-127): `request === null` after load → a `Card` with "Request not found" + a "Back to Transfers" button.

Imported UI primitives: `Card`/`CardContent`/`CardHeader`/`CardTitle` (`@/components/ui/card`, page.tsx:5), `Button` (`@/components/ui/button`, page.tsx:6), `Badge` (`@/components/ui/badge`, page.tsx:7). Icons from `lucide-react` (page.tsx:8): `ArrowLeft, Package, Calendar, MapPin, FileText, Loader2, User, Clock`.

---

## 3. KPI cards / chips / status display

**Status display** — `getStatusBadge(status)` (page.tsx:50-64). A `switch` on `status.toLowerCase()`:

| status (lowercased) | Badge label | Badge classes (page.tsx) |
|---|---|---|
| `pending` | Pending | `bg-yellow-100 text-yellow-800 border-yellow-300` (53) |
| `approved` **or** `accept` | Approved | `bg-green-100 text-green-800 border-green-300` (54-56) |
| `rejected` | Rejected | `bg-red-100 text-red-800 border-red-300` (57-58) |
| `cancelled` | Cancelled | `bg-gray-100 text-gray-800 border-gray-300` (59-60) |
| anything else (default) | raw `{status}` | default `Badge` styling (61-62) |

Note: both `approved` and `accept` collapse to the green "Approved" badge — the list page's "Accept" action writes a status that displays here as "Approved".

The status badge is rendered **twice**: in the header (page.tsx:148) and inside the info grid "Status" field (page.tsx:182).

**Underlying `Badge` component** (`components/ui/badge.tsx`): CVA-based `<span>` with variants `default | secondary | destructive | outline`; all status badges above pass custom `className` over the `default` variant.

**Summary stat tiles** (page.tsx:249-262) — the closest thing to "KPI cards":

| Tile | Value | Computation |
|---|---|---|
| **Total Items** | `request.lines?.length \|\| 0` | count of line items (page.tsx:253). Blue tile `bg-blue-50 border-blue-200`. |
| **Total Net Weight** | `…reduce((sum,line)=>sum+(parseFloat(line.net_weight)\|\|0),0).toFixed(2) \|\| "0.00"` + `" kg"` | sums each line's `net_weight` (string → `parseFloat`), 2-dp (page.tsx:257-259). Violet tile `bg-violet-50 border-violet-200`. |

**Info-grid chips/fields** (page.tsx:157-246), each an icon + label + value:
- Request Number (`FileText`, page.tsx:159-165) → `request.request_no`.
- Request Date (`Calendar`, page.tsx:168-174) → `formatDate(request.request_date)`.
- Status (`Clock`, page.tsx:177-183) → `getStatusBadge(...)`.
- From Warehouse (`MapPin`, page.tsx:186-192) → `getDisplayWarehouseName(request.from_warehouse)`.
- To Warehouse (`MapPin`, page.tsx:195-201) → `getDisplayWarehouseName(request.to_warehouse)`.
- Created By (`User`, page.tsx:204-212) — conditional on `request.created_by`.
- Created At (`Clock`, page.tsx:215-223) — conditional on `request.created_ts`, via `formatDateTime(...)`.
- Reason Description (`FileText`, page.tsx:226-234) — conditional on `request.reason_description`; spans full width (`md:col-span-2 lg:col-span-3`).
- Reject Reason (`FileText`, page.tsx:237-245) — conditional on `request.reject_reason`; full-width, red-styled box `bg-red-50 border-red-200`.

`getDisplayWarehouseName` (from `@/lib/constants/warehouses`, page.tsx:12; impl `warehouses.ts:204-208`) normalizes raw warehouse codes/aliases to a canonical code, then applies a display-name override (e.g. `Supreme` → `Supreme Cold`). Plain codes like `W202` pass through unchanged.

---

## 4. Tables & columns

There is **no `<table>`** on this page. Line items are rendered as a vertical stack of nested **`Card`s** (one card per line), not table rows — `request.lines.map(...)` at page.tsx:279-352.

Per-line card structure:
- **Card header** (page.tsx:283-290, blue `bg-blue-50`): two badges — `Item #{index+1}` (blue solid, page.tsx:286) and `{line.material_type}` (outline, page.tsx:287). `isFG = line.material_type?.toUpperCase() === "FG"` is computed (page.tsx:280) but currently **unused** in the render.
- **Item Description block** (page.tsx:293-296): `line.item_description`.
- **Details grid** `grid-cols-2 md:grid-cols-3 lg:grid-cols-4` (page.tsx:299-348). Effective "columns" (label → value field):

| Field label | Value (source) | page.tsx | Conditional? |
|---|---|---|---|
| Material Type | `line.material_type` | 301-303 | always |
| Category | `line.item_category` | 306-307 | always |
| Sub Category | `line.sub_category` | 311-312 | always |
| Quantity | `line.quantity` | 316-317 | always |
| UOM | `line.uom` | 321-322 | always |
| Case Pack/Box wt(kg) | `line.pack_size` | 326-327 | always |
| Unit Pack Size/Count | `line.unit_pack_size` | 330-334 | only if `unit_pack_size` truthy **and** `!== "0"` |
| Net Weight (Kg) | `line.net_weight` + `" kg"` | 337-340 | always |
| Lot Number | `line.lot_number` | 342-347 | only if `lot_number` truthy |

> Each line card is keyed by `line.id` (page.tsx:282). The line shape comes from the backend `RequestLineResponse` (see §12); note `unit_pack_size` is rendered here even though it is **absent from the frontend `RequestLine` TS interface** in `interunitApiService.ts:206-222` (that interface lists `package_size` instead) — accessed loosely at runtime (see Gotchas).

---

## 5. Buttons

| Label | Line | Handler | Action / Redirect | Gating |
|---|---|---|---|---|
| **Back to Transfers** (not-found state) | page.tsx:116-122 | inline `onClick` | `router.push(\`/${company}/transfer\`)` | Only rendered when `request === null` after load |
| **Back** (header) | page.tsx:134-142 | inline `onClick` | `router.push(\`/${company}/transfer\`)` | Always (in success render) |

That is the complete button set. **No Approve, Reject, Edit, Delete, Print, or Save buttons exist on this page** — it is read-only. Approve ("Accept"), Reject and Delete are implemented on the Transfer list page (`d:\test\frontend-\app\[company]\transfer\page.tsx`, e.g. `handleApproveRequest` / `handleDeleteRequest`, gated by `req.status.toLowerCase() !== 'pending'` and a `canDelete` flag; see lines 641-712 there). This view page is reached from that list via the `Eye` (view) buttons at transfer/page.tsx:637 and :698.

Both buttons use `variant="outline"`; the header Back is `size="sm"` (`Button` from `components/ui/button.tsx`; outline variant = bordered/transparent).

---

## 6. Pagination

**None.** This is a single-record detail page. No page/per_page controls; `getRequest(id)` returns one record with all its lines. (Pagination exists only on the list endpoint `getRequests`, not used here.)

---

## 7. Page-in-page & hover actions

**None.** No dialogs, modals, popovers, sheets, hover-cards, or tooltips are rendered on this page. In particular there is **no reject-reason modal** — the reject reason is shown read-only as a static field (page.tsx:237-245) when present; capturing a reject reason happens elsewhere (the list/dashboard flow). No nested routed sub-views either.

---

## 8. Keyboard / ESC / click directions

- **No custom keyboard handlers, no ESC handling, no focus traps** (consistent with there being no dialogs).
- **Click directions** (all push the same destination):
  - "Back" / "Back to Transfers" → navigate to `/{company}/transfer` (page.tsx:117, 137).
- Default browser behavior applies for tab/focus on the two buttons; no `onKeyDown`/`onKeyUp` anywhere in the file.

---

## 9. Functionality & logic flows

### Load-by-id flow (the only data flow on the page)
1. Component mounts → `useEffect([requestId])` calls `loadRequestDetails()` (page.tsx:29-31).
2. `loadRequestDetails` (page.tsx:33-48): sets `loading = true`; `await InterunitApiService.getRequest(Number(requestId))`; on success `setRequest(data)`.
3. On error: logs to console (page.tsx:39), shows a destructive `toast` ("Error" / `error.message || "Failed to load request details"`, page.tsx:40-44). `request` stays `null`.
4. `finally`: `setLoading(false)` (page.tsx:46).
5. Render gating: `loading` → spinner (page.tsx:99); else `!request` → "Request not found" card (page.tsx:110); else → full detail layout (page.tsx:129).

### Approve → transfer-form redirect flow
- **Not present on this page.** This viewer does not approve and does not redirect to a transfer form. The "Approve/Accept → create transfer" behavior lives on the list page. (Documenting per instructions: N/A here.)

### Reject flow
- **Not present on this page.** Only the *result* of a prior rejection is displayed (the read-only "Reject Reason" field and the "Rejected" status badge). N/A here.

### Edit flow
- **Not present on this page.** No edit button, no editable inputs, no `updateRequest` call. `InterunitApiService.updateRequest` exists in the service (interunitApiService.ts:322-334) but is **not** imported/used here. N/A here.

### Derived/formatting logic
- `formatDate(dateString)` (page.tsx:66-80): returns `"N/A"` for empty; passes through already-`DD-MM-YYYY` strings (regex `^\d{2}-\d{2}-\d{4}$`); otherwise `new Date(...)` → `en-GB` `dd/mm/yyyy` then `/`→`-`. Falls back to the raw string on invalid/`NaN`.
- `formatDateTime(dateString)` (page.tsx:82-97): like above but includes 2-digit hour+minute; used for "Created At".
- Total net weight reduction over lines (page.tsx:257-259) and item count (page.tsx:253).

---

## 10. Redirects

| Trigger | Destination | Line | Mechanism |
|---|---|---|---|
| Click "Back" (header) | `/{company}/transfer` | page.tsx:137 | `router.push` (`useRouter` from `next/navigation`, page.tsx:4/23) |
| Click "Back to Transfers" (not-found card) | `/{company}/transfer` | page.tsx:117 | `router.push` |

No automatic/programmatic redirects (no `router.replace`, no redirect on error — errors only toast and leave the not-found card). No redirect after any mutation (there are no mutations).

---

## 11. API calls

All via `InterunitApiService` (`@/lib/interunitApiService`, page.tsx:10). Base URL `process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'` (interunitApiService.ts:4). Auth: `Authorization: Bearer <accessToken>` injected from `useAuthStore` by `getAuthHeaders()` (interunitApiService.ts:80-90).

**Used by this page:**

| Method / Verb | Endpoint | Params | Purpose |
|---|---|---|---|
| GET | `/interunit/requests/{requestId}` | path: `Number(requestId)` | `InterunitApiService.getRequest` (interunitApiService.ts:318-320) — fetch one request header + lines. Called in `loadRequestDetails` (page.tsx:36). Returns `RequestResponse` (TS) / `RequestWithLines` (backend). |

**Available but NOT called from this page** (listed for completeness — relevant siblings):

| Method / Verb | Endpoint | Service fn | Note |
|---|---|---|---|
| GET | `/interunit/requests` | `getRequests` (interunitApiService.ts:287-316) | list/pagination — used by the list page, not here. |
| PUT | `/interunit/requests/{id}` | `updateRequest` (interunitApiService.ts:322-334) | accept/reject status + `reject_reason` + `rejected_ts` — not used here. |
| DELETE | `/interunit/requests/{id}?user_email=…` | `deleteRequest` (interunitApiService.ts:336-346) | not used here. |

`fetchJSON` (interunitApiService.ts:92-141) throws an `Error` (with `.response.status`/`.detail`) on non-2xx; that error message is what surfaces in the page's catch/toast.

---

## 12. Backend & DB wiring touched

Only the **GET request-by-id** path is exercised by this page.

- **Route:** `@router.get("/requests/{request_id}", response_model=RequestWithLines)` → `get_request_endpoint(request_id: int, db)` (`d:\test\ims-app-backend\services\ims_service\interunit_server.py:209-214`), which delegates to `get_request(request_id, db)`.
- **Handler:** `get_request` (`interunit_tools.py:392-409`):
  - Header query `SELECT id, request_no, request_date, from_site, to_site, reason_code, remarks, status, reject_reason, created_by, created_ts, rejected_ts, updated_at FROM interunit_transfer_requests WHERE id = :rid` (tools:393-402). Raises `HTTPException(404, "Request not found")` if no row (tools:404-405) → frontend shows toast + "Request not found" card.
  - `result = _map_header_row(row)` then `result["lines"] = _fetch_lines(db, request_id)` (tools:407-408).
- **Header field mapping** `_map_header_row` (`interunit_tools.py:136-150`) — note the DB→API renames the page relies on:
  - `from_site` → `from_warehouse`, `to_site` → `to_warehouse` (tools:141-142).
  - `reason_code` → `reason_description` (tools:143). (`remarks` is selected but not surfaced into this field.)
  - `request_date` formatted server-side as `"%d-%m-%Y"` (tools:140) — which is why `formatDate` passes it through unchanged.
  - `status` defaults to `"Pending"` if null (tools:144).
- **Lines query** `_fetch_lines` (`interunit_tools.py:153+`): `SELECT id, request_id, rm_pm_fg_type, item_category, sub_category, item_desc_raw, pack_size, qty, uom, unit_pack_size, net_weight, total_weight, lot_number, created_at, updated_at FROM interunit_transfer_request_lines` (tools:155-160). Column renames the page consumes: `rm_pm_fg_type`→`material_type`, `item_desc_raw`→`item_description`, `qty`→`quantity`; `unit_pack_size` and `net_weight` stringified (tools:~120-131, esp. :128-129).
- **Tables:** `interunit_transfer_requests` (header) and `interunit_transfer_request_lines` (lines). No writes from this page (read-only GET).
- **Response models:** `RequestWithLines` extends `RequestResponse` with `lines: List[RequestLineResponse]` (`interunit_models.py:111-127`). `RequestLineResponse` includes `unit_pack_size: Optional[str]` (models:104) — present in the backend but missing from the frontend `RequestLine` TS type.

---

## 13. Cross-module linkages

- **Transfer list / dashboard** (`d:\test\frontend-\app\[company]\transfer\page.tsx`): this page is the target of the `Eye` view buttons there (`router.push(\`/${company}/transfer/request/${req.id}\`)`, transfer/page.tsx:637 and :698). All write actions on requests (Accept/Reject/Delete) live on that list page, not here. Back buttons on this page return to `/{company}/transfer`.
- **Warehouse constants** (`@/lib/constants/warehouses`, `warehouses.ts`): shared single-source-of-truth used for display normalization (`getDisplayWarehouseName`); same module powers the list page and forms, so labels stay consistent module-wide.
- **API service** (`@/lib/interunitApiService`): shared `InterunitApiService` + `RequestResponse` type, reused across the whole inter-unit transfer module (requests, transfers, transfer-in, reconciliation/STBR).
- **Auth store** (`@/lib/stores/auth`, via the service): supplies the bearer token; `Company` type from `@/types/auth` types the route param.
- **Toast** (`@/hooks/use-toast`): shared notification hook (page.tsx:9).
- **Type relationship to create/edit forms:** the request "lines" rendered here correspond to `ArticleData`/`RequestLine` produced by the create flow (`transformFormDataToApi`, interunitApiService.ts:646-712); FG net-weight is computed server-side as `unit_pack_size * pack_size * qty` when not frontend-provided (tools:238-239), which is the weight summed in the Total Net Weight tile.

---

## 14. Gotchas

1. **`Number(requestId)` is unguarded** (page.tsx:36). A non-numeric route segment produces `NaN` → request to `/interunit/requests/NaN`; backend `request_id: int` path coercion will 422/404, surfacing as a toast + "Request not found" card. No client-side validation.
2. **`unit_pack_size` is rendered but not in the frontend type.** The line card reads `line.unit_pack_size` (page.tsx:330-334) but the `RequestLine` interface (interunitApiService.ts:206-222) declares `package_size` instead and has no `unit_pack_size`. It works only because the backend returns `unit_pack_size` (models:104) and `getRequest` is typed loosely enough at the call site; TS would flag this if the field were strictly typed. The display condition also requires `!== "0"`, so a literal string `"0"` hides the field.
3. **Status string coupling.** `getStatusBadge` lowercases and maps both `approved` and `accept` to the green "Approved" badge (page.tsx:54-56). Any other server status (e.g. `"draft"`) falls through to the raw-text default badge (page.tsx:61-62). The list page writes the status; if it ever sends a new value, this page shows it verbatim.
4. **Status badge rendered twice** (header page.tsx:148 + info grid page.tsx:182) — intentional but redundant; keep them in sync if styling changes.
5. **`isFG` is dead code** (page.tsx:280): computed per line but never used in JSX. Likely a leftover from an intended FG-specific rendering branch.
6. **Total Net Weight relies on string parsing** (page.tsx:258): `parseFloat(line.net_weight) || 0`. If a line's `net_weight` is non-numeric/empty it silently contributes 0; the displayed total can differ from any server-side aggregate. The `.toFixed(2) || "0.00"` tail is effectively unreachable (`toFixed` always returns a non-empty string), so the `|| "0.00"` only guards the optional-chained `undefined` from `request.lines?.reduce`.
7. **`reason_description` actually maps from DB `reason_code`** (tools:143), not a free-text remarks column (`remarks` is selected but dropped). So the "Reason Description" field shows the reason *code*, which may look terse compared to user expectation.
8. **No loading/optimistic re-fetch after external changes.** The page only loads once per `requestId`. If the request is approved/rejected elsewhere while this view is open, it will not refresh until remount/navigation.
9. **Read-only by design** — anyone reviewing this expecting approve/reject/edit controls should look at the Transfer list page; their absence here is intentional, not a missing feature.
