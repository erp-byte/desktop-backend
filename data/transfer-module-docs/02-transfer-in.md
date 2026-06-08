# Transfer In (Receive) — `/[company]/transfer/transferIn`

**File:** `frontend-/app/[company]/transfer/transferIn/page.tsx` (3197 lines, `"use client"`)
**URL:** `/CFPL/transfer/transferIn` (optional `?resume=<transferNo>`)
**Embedded dialog:** `EditReceiptDialog` (defined inline, `:3062-3197`)
**API client:** `frontend-/lib/interunitApiService.ts`

> Scope: the receive/GRN page. The Transfer-In **detail** view (`transferIn/[transferInId]`) is a separate MD file. `ChallanHoverCard` is NOT used on this page.

---

## 1. Route, purpose & relation to the dashboard

- Component `TransferInPage({ params: { company } })` (`:28-34`). Hooks: `useRouter`, `useSearchParams` (`:36-37`), `useAuthStore` (`:38`).
- **Purpose:** receive an incoming transfer-out — search by transfer number, acknowledge each box/article (scan or manual), raise discrepancies, print/generate QR labels, then finalize into a GRN (`:1973-1975`).
- **Navigation:**
  - Back button (`ArrowLeft`, `:1962`) → `router.push('/${company}/transfer')`.
  - On successful Confirm Receipt, after 2 s → `router.push('/${company}/transfer')` (`:1947-1949`).
  - **Resume deep-link:** `?resume=<transferNo>` read via `useSearchParams` (`:566`); effect guarded by `resumeHandledRef` (`:568-578`) sets the number, clears stale data, and calls `loadTransferDetails(resumeTransferNo)`. This is how the dashboard's "Resume" button enters an in-progress receipt.

---

## 2. Layout (vertical card stack, conditional on `transferData`, `:2044`)

1. **Header bar** (`:1960-2006`): back button, "Transfer IN" (`Inbox`), + conditional **Re-open** / **Edit** buttons + `EditReceiptDialog`.
2. **Find Transfer** card (`:2009-2041`): transfer-number `Input` (Enter triggers search, `:2024`), Search button (disabled while loading/empty).
3. **Transfer Route Info** card (`:2047-2066`): `from_warehouse → to_warehouse` chips (`Building2`), outline badge `challan_no || transfer_no`.
4. **Lot Dedicator** card (cold only, `:2069-2082`) — see §5.
5. **Cold Storage Details** card (cold only, `:2083-2204`): per-item editable block — Company (CFPL/CDPL), Inward Date, Vakkal, Lot No (positive-int validated, `:2141-2158`), Item Mark, Group, Sub Group, Storage Location, Exporter, Rate (₹/kg), computed Value (Weight×Rate, `:2185`), Spl. Remarks.
6. **Box & Article Acknowledgement** card (`:2207-2863`) — the core (scanner, bulk-print, article table, per-article reprint).
7. **Totals Summary** card (`:2866-2917`): Total Boxes / Qty / Net Wt / Gross Wt.
8. **Condition Assessment** card (`:2920-2953`): Box Condition (Good/Damaged/Partial) + Remarks.
9. **STBR Reconciliation Summary** card (conditional, `:2955-2987`) — only if box-id swaps occurred.
10. **Confirm Receipt** card (`:2990-3018`): full-width submit, disabled unless `allMatched`.
11. **Empty state** (`:3022`) / **loading skeleton** (`:3039`).

No tabs, no KPI cards, no pagination (one transfer at a time). Single transfer-number lookup (no list search).

**Authorization gate** (`:119-121`): `AUTHORIZED_ACKNOWLEDGE_USERS = [yash, b.hrithik, sunil.jasoria]@candorfoods.in`; `isAuthorizedUser` gates the "Acknowledge All" buttons (`:2235, :2391`).

**Article table columns** (desktop `:2437-2451`): Sr.No | Item | Transaction No | Box ID | Case Pack | Qty | Net Wt | Total Wt | Batch (if `hasBatchData`) | Lot | Action (sticky). Row tint: emerald=matched, red=issued (`:2487`).

---

## 3. Edit feature (b.hrithik-gated)

- **Gate** (`:124`): `canReopenReceived = user.email === "b.hrithik@candorfoods.in"` (also enforced server-side).
- **When shown** (`:1989-1998`): `canReopenReceived && transferData.status === "Received"`. Teal `Pencil` "Edit receipt" → `setEditOpen(true)`.
- **`EditReceiptDialog`** (`:3062-3197`): props `open, transferOutId, userEmail, onClose, onSaved`.
  - **Load** (`:3074-3101`): `InterunitApiService.getTransferInByTransferOut(transferOutId)` → seeds header (`grn_number, receiving_warehouse, box_condition, condition_remarks, status`) + maps `header.boxes` to editable rows `{box_id, article, lot_number, net_weight, gross_weight}`. If no header → toast + close.
  - **Editable fields** (`:3154-3182`): header GRN No / Receiving Warehouse / Box Condition / Condition Remarks; box rows: `box_id` read-only, Article/Lot/Net Wt/Gross Wt editable.
  - **Validation** (`:3110-3117`): `num(v)` returns `null` for blank → "leave unchanged" (avoids `Number(" ")===0` zeroing weights).
  - **Save** (`:3106-3142`): `InterunitApiService.editTransferIn(transferOutId, payload, userEmail)` (PUT). On success → toast "Receipt updated — synced to source transfer & destination stock." → `onSaved()` (re-runs `loadTransferDetails`) → `onClose()`.
  - **"Both ends" behavior:** saving updates the transfer-in receipt header+boxes, the source transfer-OUT boxes, AND destination cold-storage stock; backend COALESCEs nulls so omitted fields are never blanked.

---

## 4. Re-open feature (b.hrithik-gated)

- Gate `canReopenReceived` (`:124`). Shown (`:1977-1988`) only when `Received`. Amber `AlertTriangle` "Re-open receipt".
- **`handleReopenReceipt`** (`:544-563`): `window.confirm` ("Stock will be moved back to in-transit…") → `InterunitApiService.reopenTransferIn(transferData.id, user.email)` (POST, passes the **transfer-OUT id**). On success → toast → `loadTransferDetails(...)` → adopts the re-opened Pending header (`setPendingHeaderId(result.id)`, `setPendingGrnNumber(result.grn_number)`) so subsequent edits don't create a duplicate.
- **Effect:** moves Received → Pending/in-transit (reverses receipt stock movement, keeps boxes), enabling un-acknowledge → fix lot/issue → Confirm again.

---

## 5. Lot Dedicator

- Component `LotRangeDedicator` (from `@/components/modules/inward/LotRangeDedicator`, `:26`). Card gated by `isColdStorageTransfer && boxes.length > 0` (`:2069-2082`).
- Maps a box-number range → a lot; stored in `boxLotRanges` (`:111`).
- **Override** `dedicatedLot(b)` (`:112-117`): finds the first `LotRange` covering `Number(b.box_number)`, returns its `.lot`. Precedence: `dedicatedLot(...) || getColdLotNo(article) || line.lot_number || null` — applied at every acknowledge/print/confirm site (`:634, :731, :768, :901, :967, :1032, :1072, :1237, :1588, :1803…`). First match wins; no overlap dedup.

---

## 6. Buttons / flows / discrepancy display

- **Search** (`:2030`) → `loadTransferDetails(transferNumber)` (`:291-537`): `getTransferByNumber` → sets `transferData`, inits maps, auto-fetches cold-storage + categorial details per item, then `getPendingByTransferOut` to **resume** an in-progress GRN.
- **Acknowledge All (N)** (`:2236`) → `handleAcknowledgeAll` (`:951-1015`) via `acknowledgeBatch`.
- **Camera Scan** (`:2254`): `HighPerformanceQRScanner`; `handleAckQRScan` (`:1102-1215`) parses QR `{tx,bi}`/`{cn}`, matches box_id+transaction_no, auto-acks, sets scan chip.
- **Bulk Print QR** (`:2360`, cold-from): `handleBulkPrintQR` (`:1529-1727`) batch-acks a range, recomputes gross=net+empty-carton, prints 4×2in labels.
- **Generate QR ID's** (`:2412`): `handleGenerateQRs` (`:1372-1396`) assigns client-side `TR-…` txn + `<epoch8>-<n>` box ids when missing.
- **Per-row Action** (`:2531-2578`): branches on `hasExistingQRData` → Print QR (`handlePrintQR`) or Acknowledge (`handleAcknowledgeLine`) + Issue; green "Acknowledged" badge clickable to un-ack.
- **Issue form** (`:2581/:2747`): Case Pack / Net / Total / Remarks → `handleSubmitIssue` (`:859-942`) posts unmatched ack with `issue`; cold-from auto-reprints issue QR. The "Apply same correction to all N other pending boxes" checkbox (`applyToAllIssue`) appears when other unresolved lines of the same item exist (`:2616-2634`).
- **Per-article Box Range Reprint** (`:2795-2849`): `handlePrintRange` (`:1399-1453`).
- **Confirm Receipt** (`:2992`) → `handleConfirmReceipt` (`:1729-1956`): if `pendingHeaderId` → re-sync via `acknowledgeBatch` then `finalizeTransferIn`; else fallback `createTransferIn`. Disabled unless `allMatched`.

**Badges:** challan/transfer no (`:2061`); resolved/issues/pending counts (`:2220`); per-row Acknowledged (emerald, clickable)/Issue (red)/"↻ Reconciled" (amber STBR micro-badge, `:2515`); 4-variant scan chip (`:2289`); confirm-button text reflects state (`:3003-3015`). Issued rows tint red; mobile shows "Discrepancy Reported" block (`:2734`).

---

## 7. API client — `interunitApiService.ts` (methods used by this page + full reference)

- `API_BASE_URL = NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'` (`:4`). `getAuthHeaders()` (`:80`) adds Bearer token. `fetchJSON` (`:92-141`) throws on non-OK with `error.response={data,status,detail}` (page reads 409=duplicate scan, 422=reconciliation conflict).

| Method | Verb | Endpoint (after `/interunit`) | Used for |
|---|---|---|---|
| `getTransferByNumber(company, no)` `:619` | GET ×2 | `/transfers?challan_no=&per_page=1` then `/transfers/{id}` | Resolve challan→id→full detail (Search) |
| `getPendingByTransferOut(id)` `:578` | GET | `/transfer-in/pending/by-transfer-out/{id}` | Resume check `{exists, header}` |
| `createPendingTransferIn(payload)` `:510` | POST | `/transfer-in/pending` | Create pending GRN header |
| `acknowledgeBox(headerId, payload)` `:524` | POST | `/transfer-in/{headerId}/acknowledge` | Single box/line ack or issue → incl. `.reconciliation` |
| `unacknowledgeBox(headerId, boxId)` `:545` | DELETE | `/transfer-in/{headerId}/acknowledge/{boxId}` | Un-acknowledge |
| `acknowledgeBatch(headerId, boxes[])` `:551` | POST | `/transfer-in/{headerId}/acknowledge-batch` | Bulk ack → `conflicts[]` |
| `finalizeTransferIn(headerId, payload)` `:567` | POST | `/transfer-in/{headerId}/finalize` | Finalize pending GRN → posts stock |
| `createTransferIn(payload)` `:476` | POST | `/transfer-in` | Fallback bulk receipt create |
| `getTransferInReconciliation(id)` `:563` | GET | `/transfer-in/{id}/reconciliation` | STBR audit report |
| `reopenTransferIn(transferOutId, email)` `:589` | POST | `/transfer-in/reopen-by-transfer-out/{id}?user_email=` | Re-open (b.hrithik) |
| `getTransferInByTransferOut(id)` `:596` | GET | `/transfer-in/by-transfer-out/{id}` | Pre-fill Edit dialog |
| `editTransferIn(id, payload, email)` `:605` | PUT | `/transfer-in/by-transfer-out/{id}/edit?user_email=` | Privileged edit; syncs receipt+source+destination (b.hrithik) |

*(Other class methods — dropdowns, requests, transfers CRUD, stats — exist but are used by other pages.)*

---

## 8. Backend & DB wiring touched by THIS page

- **Receive lifecycle:** `pending_transfer_stock` (In Transit) → `pick_from_pending` on acknowledge/finalize → writes destination `cfpl/cdpl_cold_stocks` (or warehouse) → `interunit_transfer_in_header/boxes` records the GRN → transfer-out header status → `Received`.
- **STBR (Scan-Time Box-ID Reconciliation):** mismatched scanned box-ids are reconciled against pending rows; `reconciled`/`original_box_id`/`reconciliation_id` columns on `interunit_transfer_in_boxes` + `pending_transfer_stock`; audit via `cold_stock_disposition`.
- **Edit/Re-open:** `editTransferIn` → 3-table sync (transfer-in boxes + source transfer-out boxes + destination cold stock) and stamps `interunit_transfers_header.edited_at`; `reopenTransferIn` → reverses receipt, re-parks into `pending_transfer_stock`, status Received→Pending/In Transit.

**Cross-module:** destination is the **Cold Storage** module (`cfpl/cdpl_cold_stocks`); warehouse destinations relate to **Inward** (`bulk_entry_boxes`). QR scanning ties to the box-id/transaction-no scheme shared with Inward.

---

## 9. Keyboard / click directions

- **Enter** in the transfer-number input triggers Search (`:2024`).
- Camera scanner drives auto-acknowledge on each successful decode.
- Confirm/Re-open use `window.confirm`; Edit opens a modal dialog (backdrop/Cancel close).
- No ESC handler on this page (it's a full page, not a modal).
