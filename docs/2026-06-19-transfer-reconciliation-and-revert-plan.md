# Transfer Reconciliation & Revert — Implementation Plan

> **For agentic workers:** This plan covers the **interunit Transfer** and **Cold Transfer** modules of the IMS app (`c:\Backup\backend` + `c:\Backup\frontend`). Per the requester's instruction it is written at the **contract / schema / SQL-sketch level, not as finished code** — every task names the exact existing table, column, status string, function, endpoint, and file so an executor can implement against real wiring. When you build it, drive it task-by-task with `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Steps use `- [ ]` checkboxes.

**Goal:** Add a controlled, auditable reconciliation layer to the transfers module so a partially-received transfer (e.g. 1000 dispatched / 977 received / 23 short) can be resolved through one of four explicit dispositions — *never dispatched*, *return of excess*, *full-lot return*, or *missing* — each with the correct inventory movement, dispatch-team acknowledgement where required, and a missing-items alert (email + WhatsApp) to the inventory manager and admin team.

**Architecture:** Reuse the existing `pending_transfer_stock` row as the live record of each in-transit / short box, and the existing audit tables `cold_stock_disposition` and `transfer_box_reconciliation` as the immutable history. Replace the blunt write-off in `close_transfer_in_with_shortage` with disposition-specific handlers that re-insert short boxes back into the source stock table (never-dispatched), open a return leg that the origin warehouse must acknowledge (excess / full-lot return), or flag boxes missing and fire notifications. No new stock model is introduced; we extend the status vocabulary of `pending_transfer_stock` and the `disposition_type` vocabulary of `cold_stock_disposition`.

**Tech Stack:** FastAPI (Python) + SQLAlchemy Core (`text()` raw SQL) on AWS RDS Postgres `warehouse_db`; Next.js (App Router, TypeScript) frontend; existing `smtplib`-based email (`shared/email_notifier.py`) and Meta Graph WhatsApp (`shared/whatsapp.py`).

---

## Global Constraints

- **Backend is `c:\Backup\backend` only.** Ignore `c:\Candor\SSD files of Candor\Consumption\Backend` — it is an unrelated FastAPI project with no `/inward` or `/interunit` routes.
- **Frontend base URL** is read from `process.env.NEXT_PUBLIC_API_URL` (NOT `NEXT_PUBLIC_API_BASE_URL`); call sites fall back to `http://localhost:8000`.
- **Raw SQL via `text()`** is the house style for transfer code — match `interunit_tools.py` / `pending_stock_tools.py` / `cold_transfer_in_tools.py`. Do not introduce an ORM model layer for these tables.
- **All stock is tracked at BOX level** — one row per physical box in `*_cold_stocks` / `*_boxes_v2` / `*_bulk_entry_boxes`. Box identity is the pair `(box_id, transaction_no)`; this pair is `UNIQUE` everywhere it appears. Never aggregate-update a quantity column; always insert/delete whole box rows.
- **Cold quantity column is `weight_kg`; warehouse box quantity is `net_weight`.** Cold rate column is `last_purchase_rate` (NOT `rate`).
- **Every stock-moving action must be wrapped in a single DB transaction** and commit once at the end, mirroring existing handlers (`finalize_transfer_in`, `delete_transfer_in`, `unpick_to_pending`).
- **Authorisation gates** already exist and must be reused/extended, not bypassed: `TRANSFER_IN_DELETE_ALLOWED_EMAILS`, `TRANSFER_IN_REOPEN_ALLOWED_EMAILS` (in `interunit_tools.py`).
- **Cold destinations** are exactly `{"savla d-39", "savla d-514", "rishi", "supreme"}` → `cfpl_cold_stocks` for Savla, `cdpl_cold_stocks` for Rishi/Supreme (`cold_transfer_in_tools.py` lines 26-41). Use `_cold_stocks_table()` / `_is_cold_site()` — never hardcode the routing again.
- **Preserve the cold staging-header invariant:** a cold transfer-IN reuses the `interunit_transfer_in_header` id as the `cold_transfer_in_headers` id. The staging purge in `finalize_cold_transfer_in` is gated on `in_status == "Received"` and guarded by `transfer_out_id`. Any new cold disposition must keep that gate or it will spawn duplicate/orphan headers (see the 2026-06-19 orphan bug).

---

## Current State (verified against code — read before building)

### Tables and their roles

| Table | Role | Key status column & values |
|---|---|---|
| `interunit_transfers_header` | Transfer-OUT order header | `status`: `'Dispatch'` → `'Partial'` → `'Received'`; also `has_variance` (bool), `unallocated_boxes` (int) |
| `interunit_transfers_lines` | Transfer-OUT line items | — |
| `interunit_transfer_boxes` | Transfer-OUT physical boxes (what was dispatched) | — |
| `interunit_transfer_in_header` | Transfer-IN GRN header (interunit receive + cold staging) | `status`: `'Pending'` / `'Received'` |
| `interunit_transfer_in_boxes` | Transfer-IN acknowledged boxes; `issue` JSON for discrepancies | `is_matched` (bool), `issue` (json) |
| `cold_transfer_in_headers` | Cold receive header (id shared with staging header) | `status`: `'Pending'` / `'Received'` |
| `cold_transfer_inboxes` | Cold receive boxes | — |
| `pending_transfer_stock` | **In-transit holding** — the live record of dispatched-but-not-received boxes | `status`: `'In Transit'` / `'Cancelled'` |
| `cfpl_cold_stocks` / `cdpl_cold_stocks` | Cold on-hand stock (1 row = 1 box) | qty `weight_kg`, rate `last_purchase_rate` |
| `cfpl_boxes_v2` / `cdpl_boxes_v2` | Warehouse on-hand stock (modern) | qty `net_weight` |
| `cfpl_bulk_entry_boxes` / `cdpl_bulk_entry_boxes` | Warehouse stock (legacy; some source/restore paths still target these) | — |
| `transfer_box_reconciliation` | Audit: per-box scan reconciliation | `reconciliation_status`: `matched` / `overridden` / `fungible_swap` / `copied` / `conflict` / `overridden_no_source` |
| `cold_stock_disposition` | Audit: why a box left source, and revert trail | `disposition_type`: `transfer_out_pending` / `direct_out` / `job_work_out` / `consumption` / `outward` / `manual_correction`; `reverted` (bool), `reverted_reason`, `snapshot_data` (jsonb) |

### How a box moves today (the mechanics we are extending)

1. **Transfer OUT** — `create_transfer` (`interunit_tools.py:780`) / `create_cold_transfer_out` (`cold_transfer_out_tools.py:130`) → `park_in_pending` (`pending_stock_tools.py:1112`): for each box it **DELETEs the row from the source stock table** (`DELETE FROM {source_table} WHERE id = :rid`) and **INSERTs a `pending_transfer_stock` row** with `status='In Transit'`, storing `source_table`, `source_row_id`, and (for cold) a `cold_storage_data` JSONB snapshot. **The box no longer exists in source stock — it lives only in `pending_transfer_stock`.**
2. **Transfer IN receive** — `create_pending_transfer_in` → `acknowledge_pending_boxes_batch` → `finalize_transfer_in` (`interunit_tools.py:2496`) → `pick_from_pending` (`pending_stock_tools.py:1672`): each acknowledged box's pending row is **DELETEd**. For **cold** destinations, `finalize_cold_transfer_in` (`cold_transfer_in_tools.py:213`) additionally **INSERTs into `*_cold_stocks`**. For **warehouse** destinations the box is NOT re-inserted into `*_boxes_v2` (those are read-only for transfers) — the acknowledged record in `interunit_transfer_in_boxes` is the authoritative receipt.
3. **Status reconcile** — `count_remaining_in_transit(transfer_out_id)` (`pending_stock_tools.py:1797`) counts `pending_transfer_stock` rows still `'In Transit'` (excluding `LINE-%` sentinels). When it hits 0, both headers flip to `'Received'`; otherwise they stay `'Dispatch'`/`'Pending'`. Cold mirror: `_reconcile_statuses` (`cold_transfer_in_tools.py:743`).

### The gap (why this plan exists)

When 977 of 1000 are received, **23 `pending_transfer_stock` rows stay `'In Transit'`**. The only existing resolutions are:

- **Wait** for the boxes to arrive and finalize again, or
- **`close_transfer_in_with_shortage`** (`interunit_tools.py:2427`, route `POST /interunit/transfer-in/{header_id}/close-with-shortage`) — which simply runs `DELETE FROM pending_transfer_stock WHERE transfer_out_id=:tid AND status='In Transit'` and marks both headers `'Received'`. **The 23 boxes vanish from the system with no disposition, no return to source, no missing flag.** That is the data leak the requester described ("it might be leaked in the system and never found").

There is **no** mechanism today for: returning never-dispatched stock to source, returning excess/whole lots with dispatch-team acknowledgement, or raising a missing-items alert. This plan adds them.

---

## Use-case → disposition decision matrix

| # | Requester's scenario | Disposition | Physical reality | Inventory movement | Acknowledgement needed? |
|---|---|---|---|---|---|
| 1 | 23 of 1000 not yet received — show as pending | *(visibility only)* | Boxes still genuinely in transit | None (stay `'In Transit'`) | No |
| 2 | "Received 977, rest never sent" (vehicle full / forgot / couldn't load) | **`never_dispatched`** | 23 never left the source warehouse | Re-insert 23 into **source** stock; receive = 977 | No (source-side correction; reason + approver captured) |
| 3 | Store needed 977, got 1000, sends 23 back | **`return_excess`** | 23 were received then physically returned | Remove 23 from **destination**, open return leg, **origin re-receives on ack** | **Yes — origin/dispatch team acknowledges** |
| 4 | Entire lot returned | **`return_full_lot`** | All boxes received then returned | Same as #3 for the whole lot | **Yes — origin/dispatch team acknowledges** |
| 5 | 23 (or a returned box) never found by either side | **`missing`** | Boxes physically lost | None moved; box marked Missing | No — but **alert** inventory manager + admin (email + WhatsApp) |

**Key distinction the executor must respect:**
- Use-case **2** operates on boxes **still `'In Transit'`** (never received) → they go back to **source**, no second party needed.
- Use-cases **3 & 4** operate on boxes **already received** (already removed from `pending_transfer_stock`, already in destination stock for cold) → they need a **new return leg** and the **origin warehouse must acknowledge** before stock is restored there.
- Use-case **5** can be reached either from a stuck `'In Transit'` box (sender claims sent, receiver never got it) or from an un-acknowledged return leg (return shipment lost).

---

## Target data model (extend existing tables — minimal additions)

### A. New status values for `pending_transfer_stock.status`

Existing: `'In Transit'`, `'Cancelled'`. Add:

| New value | Meaning |
|---|---|
| `'Return In Transit'` | A received box being shipped back to origin, awaiting origin acknowledgement (use-cases 3 & 4) |
| `'Missing'` | Box flagged lost; retained as an unaccounted record until written off (use-case 5) |

Terminal dispositions (`never_dispatched` returned-to-source, and acknowledged returns) **delete** the `pending_transfer_stock` row and write the history to `cold_stock_disposition`, matching how `pick_from_pending` / `unpick_to_pending` treat the table as "live only".

### B. New columns on `pending_transfer_stock`

```sql
ALTER TABLE pending_transfer_stock
  ADD COLUMN IF NOT EXISTS return_direction      VARCHAR(20),   -- NULL | 'to_source'
  ADD COLUMN IF NOT EXISTS return_reason         TEXT,
  ADD COLUMN IF NOT EXISTS return_initiated_by   VARCHAR(120),
  ADD COLUMN IF NOT EXISTS return_initiated_at   TIMESTAMP,
  ADD COLUMN IF NOT EXISTS return_acknowledged_by VARCHAR(120),
  ADD COLUMN IF NOT EXISTS return_acknowledged_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS missing_flagged_by    VARCHAR(120),
  ADD COLUMN IF NOT EXISTS missing_flagged_at    TIMESTAMP,
  ADD COLUMN IF NOT EXISTS source_snapshot       JSONB;          -- full source-row snapshot (see Task 1.2)
```
> `cold_storage_data` already snapshots cold source rows. `source_snapshot` generalises that to warehouse sources too, so any box (cold or warehouse) can be rebuilt into its origin table. Keep both for backward compatibility; new code reads `COALESCE(source_snapshot, cold_storage_data)`.

### C. New `disposition_type` values for `cold_stock_disposition`

Existing enum-by-convention: `transfer_out_pending`, `direct_out`, `job_work_out`, `consumption`, `outward`, `manual_correction`. Add (string values, no DB enum to alter — column is `VARCHAR(30)`):

- `transfer_never_dispatched`
- `transfer_return_excess`
- `transfer_return_full_lot`
- `transfer_missing`

Each disposition row records `box_id`, `transaction_no`, `from_company`, `from_site`, `disposition_ref_table='pending_transfer_stock'`, `disposition_ref_id`, `disposed_by`, `reverted`/`reverted_reason` where applicable, `snapshot_data`, and `notes` (free-text reason / approver).

### D. (Optional) reconciliation header view — no new table required

A per-transfer "open reconciliation" panel is fully derivable from existing data: `transfer_out_id` + `count_remaining_in_transit()` + the `pending_transfer_stock` rows. **Do not** add a header table unless the team later wants an approval workflow with its own lifecycle; this plan keeps state on the box rows.

---

## Backend — files to touch

- **`services/ims_service/pending_stock_tools.py`** — new movement primitives: `repark_to_source`, `open_return_leg`, `acknowledge_return`, `flag_missing`, plus extend `park_in_pending` to always write `source_snapshot`.
- **`services/ims_service/interunit_tools.py`** — new orchestration functions that the routes call: `rectify_never_dispatched`, `initiate_transfer_return`, `acknowledge_transfer_return`, `flag_transfer_boxes_missing`, `list_open_reconciliations`, `list_pending_returns`. Reuse `count_remaining_in_transit`, `_map_transfer_in_header`, `_fetch_transfer_in_boxes`.
- **`services/ims_service/cold_transfer_in_tools.py`** — cold-aware branches for return/never-dispatched (re-insert into `*_cold_stocks` via `_cold_stocks_table`, respect the staging-header gate). Reuse `_cold_row_to_json`, `delete_cold_transfer_in`'s re-park pattern.
- **`services/ims_service/interunit_server.py`** — register the new routes next to the existing transfer routes.
- **`services/ims_service/interunit_models.py`** — Pydantic request/response models for the new endpoints.
- **`shared/email_notifier.py`** — `notify_transfer_missing_boxes(...)`.
- **`shared/whatsapp.py`** — `send_transfer_missing_notification(...)`.
- **`shared/config_loader.py` + `.env`** — `INVENTORY_MANAGER_EMAIL`, `ADMIN_TEAM_EMAILS`, `INVENTORY_MANAGER_WHATSAPP`.

### New endpoints (register in `interunit_server.py`)

| Method | Path | Backend fn | Purpose |
|---|---|---|---|
| GET | `/interunit/transfer-in/{transfer_out_id}/reconciliation` | `list_open_reconciliations` | Use-case 1: the 23 still-in-transit boxes + summary |
| POST | `/interunit/transfer-in/{transfer_out_id}/rectify-never-dispatched` | `rectify_never_dispatched` | Use-case 2: return short boxes to source, close as received-short |
| POST | `/interunit/transfer-in/{transfer_out_id}/return` | `initiate_transfer_return` | Use-cases 3 & 4: open a return leg (excess or full lot) |
| GET | `/interunit/returns/pending` | `list_pending_returns` | Origin/dispatch team's "returns to acknowledge" queue |
| POST | `/interunit/returns/{pending_stock_id}/acknowledge` | `acknowledge_transfer_return` | Use-cases 3 & 4: origin re-receives returned stock |
| POST | `/interunit/transfer-in/{transfer_out_id}/flag-missing` | `flag_transfer_boxes_missing` | Use-case 5: mark boxes missing + notify |

> **Cold note:** the same routes serve cold transfers (cold is just a `transfer_out_id` whose `to_site` is a cold destination). Inside each handler, branch on `_is_cold_site(from_site)` / `_is_cold_destination(to_site)` to choose `*_cold_stocks` vs `*_boxes_v2`/`*_bulk_entry_boxes` and to honour the staging-header gate. Do not create a parallel cold route set.

---

## Phase 0 — Schema & audit groundwork

### Task 0.1: Add reconciliation columns and snapshot column

**Files:**
- Create migration: `services/ims_service/migrations/20260619_transfer_reconciliation.sql` (follow the existing migration style under `services/cold_storage_service/migrations/`)
- Modify: schema-ensure block that runs at service start (the same place that ensures `from_cold_unit` / `inward_transaction_no`, e.g. `interunit_tools.py` lines ~75-106) — add `ADD COLUMN IF NOT EXISTS` guards so prod RDS is migrated idempotently on boot.

- [ ] **Step 1:** Write the `ALTER TABLE pending_transfer_stock ADD COLUMN IF NOT EXISTS ...` block from section B above into the migration file.
- [ ] **Step 2:** Confirm `cold_stock_disposition` and `transfer_box_reconciliation` already exist (created in `pending_stock_tools.py` lines ~230-292). No DDL needed beyond documenting the new `disposition_type` string values.
- [ ] **Step 3:** Add the same `ADD COLUMN IF NOT EXISTS` guards to the boot-time ensure block so a fresh deploy self-heals.
- [ ] **Step 4 (verify):** Run the service against a scratch DB; confirm the columns exist (`\d pending_transfer_stock`) and re-running is a no-op.

### Task 0.2: Capture a full `source_snapshot` at park time

**Files:** Modify `park_in_pending` (`pending_stock_tools.py:1112`).

**Interfaces — Produces:** every new `pending_transfer_stock` row carries `source_snapshot` = JSON of the *entire* source stock row (cold or warehouse) keyed by column name, so it can be rebuilt verbatim into `source_table`.

- [ ] **Step 1:** Before the `DELETE FROM {source_table} WHERE id=:rid`, `SELECT *` the source row and serialise it (reuse `_cold_row_to_json` for cold; write a generic `_row_to_json` for warehouse rows) into `source_snapshot`.
- [ ] **Step 2:** Keep writing `cold_storage_data` as today (back-compat). New readers use `COALESCE(source_snapshot, cold_storage_data)`.
- [ ] **Step 3 (verify):** Park one cold box and one warehouse box; assert both rows have a non-null `source_snapshot` containing the original `box_id`, `transaction_no`, and source `id`.

> **Why this matters:** use-case 2 (and acknowledged returns) must rebuild the box in its origin table. Cold already had a snapshot; warehouse sources did not. Without this, returning a never-dispatched warehouse box would lose its original columns.

---

## Phase 1 — Reconciliation visibility (use-case 1)

### Task 1.1: `list_open_reconciliations` backend

**Files:**
- Modify: `interunit_tools.py` (new function), `interunit_server.py` (new GET route), `interunit_models.py` (response model).

**Interfaces — Produces:**
```
GET /interunit/transfer-in/{transfer_out_id}/reconciliation
→ {
    transfer_out_id, challan_no, from_site, to_site,
    ordered_boxes:   int,   # COUNT(interunit_transfer_boxes WHERE header_id=transfer_out_id)
    received_boxes:  int,   # ordered - remaining_in_transit  (or COUNT of interunit_transfer_in_boxes)
    remaining_in_transit: int,           # count_remaining_in_transit(transfer_out_id)
    pending_boxes: [ { pending_stock_id, box_id, transaction_no, article, lot_no,
                       weight_kg, status, from_site, to_site } ],
    eligible_dispositions: [ 'never_dispatched', 'return_excess', 'return_full_lot', 'missing' ]
  }
```

- [ ] **Step 1:** Implement the function: read header from `interunit_transfers_header`, box count from `interunit_transfer_boxes`, remaining via `count_remaining_in_transit`, and the per-box list from `pending_transfer_stock WHERE transfer_out_id=:tid AND status IN ('In Transit','Return In Transit','Missing') AND COALESCE(box_id,'') NOT LIKE 'LINE-%'`.
- [ ] **Step 2:** Register the GET route in `interunit_server.py` adjacent to `/interunit/transfer-in/...`.
- [ ] **Step 3 (verify):** For a transfer with 1000 dispatched / 977 picked, assert `remaining_in_transit == 23` and `len(pending_boxes) == 23`.

### Task 1.2: Surface "X boxes pending" in the UI

**Files:**
- Modify: `frontend/app/[company]/transfer/page.tsx` (Transfer-In tab rows + the existing "Pending Transfers Modal").
- Modify: `frontend/app/[company]/transfer/dashboard/page.tsx` (already computes `pending_count` / `notReceived` from `received_status`; add a drill-in link to the reconciliation panel).
- Modify: `frontend/lib/interunitApiService.ts` — add `getReconciliation(transferOutId)`.

- [ ] **Step 1:** Add `getReconciliation` to `interunitApiService` calling the Task 1.1 endpoint (reuse the existing `fetchJSON` + Bearer-token helper).
- [ ] **Step 2:** On a partially-received Transfer-In row, render an amber badge `"{remaining_in_transit} of {ordered_boxes} pending"` and a **Reconcile** action that opens the panel.
- [ ] **Step 3 (verify):** Load a known partial transfer; confirm the badge shows `23 of 1000 pending`.

---

## Phase 2 — Rectify: "never dispatched" (use-case 2)

### Task 2.1: `repark_to_source` primitive

**Files:** Modify `pending_stock_tools.py` (new function).

**Interfaces — Produces:**
```
repark_to_source(pending_stock_id: int, db: Session) -> dict
  # Re-inserts ONE 'In Transit' (or 'Return In Transit') pending box back into its
  # source stock table from source_snapshot, then DELETEs the pending row.
```

- [ ] **Step 1:** Read the pending row; resolve target table = its `source_table` (cold → `cfpl_cold_stocks`/`cdpl_cold_stocks`; warehouse → `cfpl_bulk_entry_boxes`/`cdpl_bulk_entry_boxes`, matching what `park_in_pending` recorded).
- [ ] **Step 2:** Rebuild the row from `COALESCE(source_snapshot, cold_storage_data)`; `INSERT` into the source table. Strip the old primary-key `id` so a fresh sequence id is assigned; preserve `box_id` + `transaction_no` (the unique pair). For cold use column `last_purchase_rate` and recompute `total_inventory_kgs = no_of_cartons * weight_kg`.
- [ ] **Step 3:** `DELETE FROM pending_transfer_stock WHERE id=:pid`.
- [ ] **Step 4:** Mirror the `unpick_to_pending` re-insert shape (`pending_stock_tools.py:1879-1925`) for column handling so cold/warehouse parity holds.
- [ ] **Step 5 (verify):** Park a box (source row gone), call `repark_to_source`, assert the box reappears in the source table with the same `(box_id, transaction_no)` and the pending row is gone.

### Task 2.2: `rectify_never_dispatched` orchestration + audit

**Files:** Modify `interunit_tools.py` (new fn), `cold_transfer_in_tools.py` (cold branch for staging gate), `interunit_server.py` (route), `interunit_models.py` (request model `RectifyNeverDispatched { pending_stock_ids?: int[], all_remaining?: bool, reason: str, approved_by: str }`).

**Behaviour:**
- [ ] **Step 1:** Validate the caller-selected boxes are `pending_transfer_stock` rows for this `transfer_out_id` with `status='In Transit'`. If `all_remaining` is true, select every remaining in-transit row.
- [ ] **Step 2:** For each, call `repark_to_source`, then INSERT a `cold_stock_disposition` row with `disposition_type='transfer_never_dispatched'`, `reverted=TRUE`, `reverted_reason=reason`, `disposed_by=approved_by`, `disposition_ref_table='pending_transfer_stock'`, `notes` = reason/approver, `snapshot_data` = the box snapshot.
- [ ] **Step 3:** After re-parking, recompute status: the received count is final (977). Mark the transfer **Received-short** — set `interunit_transfers_header.status='Received'`, `has_variance=TRUE`, `unallocated_boxes=<count returned>`, and append a `condition_remarks` note on the GRN header exactly like `close_transfer_in_with_shortage` does (so audit reads consistently). **Do NOT delete the in-transit rows blindly** — `repark_to_source` already consumed them.
- [ ] **Step 4 (cold):** If `to_site` is cold and a `cold_transfer_in_headers` row exists, set its `status='Received'` and respect the staging-header purge gate (`finalize_cold_transfer_in` lines ~348-358) — purge staging only when nothing remains in transit.
- [ ] **Step 5:** Register `POST /interunit/transfer-in/{transfer_out_id}/rectify-never-dispatched`. Gate to an allowed-emails list (extend the existing `TRANSFER_IN_*_ALLOWED_EMAILS` pattern; the reason+approver capture is mandatory).
- [ ] **Step 6 (verify):** 1000 dispatched / 977 received; rectify-never-dispatched the 23 with reason "vehicle full"; assert: 23 boxes back in source stock, 23 `cold_stock_disposition` rows with `transfer_never_dispatched`, header `Received` + `has_variance=TRUE` + `unallocated_boxes=23`, `count_remaining_in_transit==0`.

### Task 2.3: "Rectify entry" UI

**Files:** Modify `frontend/app/[company]/transfer/transferIn/page.tsx` and/or the reconciliation panel from Task 1.2; add `rectifyNeverDispatched(transferOutId, payload)` to `interunitApiService.ts`.

- [ ] **Step 1:** In the reconciliation panel, add a **"Rectify — never sent (return to origin stock)"** button. Open a modal requiring a free-text reason and an approver name (mandatory) and a box-selection (default: all remaining).
- [ ] **Step 2:** On submit, call the Task 2.2 endpoint; on success refresh the panel and the transfer list; show "23 boxes returned to {from_site} stock; transfer closed as received-short."
- [ ] **Step 3 (verify):** Click-through on a real partial transfer; confirm UI reflects 977 received and the source warehouse stock count rose by 23.

---

## Phase 3 — Returns with dispatch-team acknowledgement (use-cases 3 & 4)

### Task 3.1: `open_return_leg` primitive

**Files:** Modify `pending_stock_tools.py` (new fn); cold branch in `cold_transfer_in_tools.py`.

**Interfaces — Produces:**
```
open_return_leg(transfer_out_id, boxes: [{box_id, transaction_no}], reason, initiated_by,
                return_type: 'return_excess'|'return_full_lot', db) -> dict
```
For boxes **already received** (no longer in `pending_transfer_stock`):
- [ ] **Step 1:** Remove each box from the **destination** stock: cold → `DELETE FROM {dest_cold_table} WHERE box_id=:b AND transaction_no=:t` (capture snapshot first via `_cold_row_to_json`); warehouse → there is no destination stock row (boxes_v2 read-only), so source the snapshot from `interunit_transfer_in_boxes` + original `interunit_transfer_boxes`.
- [ ] **Step 2:** INSERT a NEW `pending_transfer_stock` row per box with `status='Return In Transit'`, `return_direction='to_source'`, `transfer_out_id` (same transfer), swapped `from_site`/`to_site` (origin becomes destination of the return), `return_reason=reason`, `return_initiated_by=initiated_by`, `return_initiated_at=NOW()`, and `source_snapshot` = the captured destination snapshot (so the origin can rebuild it).
- [ ] **Step 3:** INSERT a `cold_stock_disposition` row with `disposition_type=return_type`, `disposed_by=initiated_by`, `notes=reason`.
- [ ] **Step 4 (verify):** For a fully-received transfer, open a return of 23 boxes; assert 23 destination cold rows removed and 23 `pending_transfer_stock` rows with `status='Return In Transit'`.

### Task 3.2: `initiate_transfer_return` + route

**Files:** Modify `interunit_tools.py`, `interunit_server.py`, `interunit_models.py` (`InitiateReturn { box_ids?: [{box_id, transaction_no}], full_lot?: bool, reason, initiated_by }`).

- [ ] **Step 1:** If `full_lot`, select all boxes of the transfer's lot(s) from `interunit_transfer_in_boxes`; else use the provided list. Call `open_return_leg` with the right `return_type`.
- [ ] **Step 2:** Register `POST /interunit/transfer-in/{transfer_out_id}/return`. The **receiving/store** side initiates this.
- [ ] **Step 3 (verify):** Initiate a full-lot return; assert all boxes flip to `'Return In Transit'`.

### Task 3.3: `acknowledge_return` primitive + `acknowledge_transfer_return` + queue

**Files:** Modify `pending_stock_tools.py`, `interunit_tools.py`, `interunit_server.py`, `interunit_models.py`.

**Interfaces — Produces:**
```
GET  /interunit/returns/pending?warehouse={origin_site}
     → list of 'Return In Transit' rows grouped by transfer_out_id (the dispatch team's inbox)
POST /interunit/returns/{pending_stock_id}/acknowledge   { acknowledged_by }
     → re-inserts the box into the ORIGIN source stock and closes the return
```

- [ ] **Step 1:** `acknowledge_return(pending_stock_id, acknowledged_by, db)`: read the `'Return In Transit'` row; rebuild into the **origin** source table from `source_snapshot` (cold → `_cold_stocks_table(origin)`; warehouse → `*_bulk_entry_boxes`); set `return_acknowledged_by/at`; then DELETE the pending row; INSERT/UPDATE the `cold_stock_disposition` audit row to `reverted=TRUE, reverted_reason='return acknowledged by '+acknowledged_by`.
- [ ] **Step 2:** `list_pending_returns(warehouse, db)`: `SELECT ... FROM pending_transfer_stock WHERE status='Return In Transit' AND to_site=:warehouse` grouped by `transfer_out_id`.
- [ ] **Step 3:** Register both routes. Acknowledge is **origin/dispatch-team gated** — only the dispatching warehouse's users may acknowledge (capture `acknowledged_by`); this is the control the requester asked for so returns can't "leak."
- [ ] **Step 4 (verify):** Initiate a return of 23 → they appear in `/interunit/returns/pending?warehouse={origin}` → acknowledge each → assert 23 rows reappear in origin source stock and zero `'Return In Transit'` remain.

### Task 3.4: Returns UI (initiate + acknowledge)

**Files:**
- Modify `frontend/app/[company]/transfer/transferIn/page.tsx` (and cold `coldtransfer-in/page.tsx`) — **"Return to origin"** action (excess or full-lot) on a received transfer; modal for box selection + reason.
- Create `frontend/app/[company]/transfer/returns/page.tsx` — the **dispatch team's "Returns to acknowledge"** queue, listing `/interunit/returns/pending` and an **Acknowledge receipt** button per row.
- Modify `interunitApiService.ts` — `initiateReturn`, `listPendingReturns`, `acknowledgeReturn`.

- [ ] **Step 1:** Build the initiate-return modal; on submit call `initiateReturn`.
- [ ] **Step 2:** Build the returns queue page; wire `acknowledgeReturn` per row; reflect counts.
- [ ] **Step 3 (verify):** End-to-end: store returns 23 → dispatch team sees them in the queue → acknowledges → origin stock restored. Confirm a returned box that is NOT acknowledged stays visible (cannot silently disappear).

---

## Phase 4 — Missing-items flag & notifications (use-case 5)

### Task 4.1: Notification config

**Files:** Modify `shared/config_loader.py` + `.env`.

- [ ] **Step 1:** Add settings `INVENTORY_MANAGER_EMAIL`, `ADMIN_TEAM_EMAILS` (comma-separated → list), `INVENTORY_MANAGER_WHATSAPP` (E.164 phone). Mirror how `SMTP_*` / `WHATSAPP_*` are loaded.
- [ ] **Step 2 (verify):** Boot the service; assert the new settings load (and the service does not crash when WhatsApp creds are absent — `send_whatsapp_message` already no-ops without credentials).

### Task 4.2: `notify_transfer_missing_boxes` (email) + `send_transfer_missing_notification` (WhatsApp)

**Files:** Modify `shared/email_notifier.py`, `shared/whatsapp.py`.

- [ ] **Step 1:** `notify_transfer_missing_boxes(transfer_out_id, challan_no, from_site, to_site, missing_boxes: list, flagged_by, reason)` — build subject `"[IMS] Missing transfer boxes — {challan_no}"`, an HTML + plain body listing each missing `box_id`/`lot_no`/`weight_kg`, and call the existing `_send_email_background(subject, html, plain, to=INVENTORY_MANAGER_EMAIL, cc=ADMIN_TEAM_EMAILS)`. Reuse the existing thread-based sender — do not block the request.
- [ ] **Step 2:** `send_transfer_missing_notification(...)` — compose a concise text summary and call the existing `send_whatsapp_message(to=INVENTORY_MANAGER_WHATSAPP, text=...)`.
- [ ] **Step 3 (verify):** Call both with a fake transfer to a test inbox/number; confirm delivery and that failures are logged, not raised (match existing `notify_rtv_*` behaviour).

### Task 4.3: `flag_transfer_boxes_missing` + route

**Files:** Modify `pending_stock_tools.py` (`flag_missing` primitive), `interunit_tools.py` (orchestration), `interunit_server.py` (route), `interunit_models.py` (`FlagMissing { pending_stock_ids: int[], reason, flagged_by }`).

- [ ] **Step 1:** `flag_missing`: for each selected `pending_transfer_stock` row (status `'In Transit'` or `'Return In Transit'`), set `status='Missing'`, `missing_flagged_by`, `missing_flagged_at`; INSERT a `cold_stock_disposition` row `disposition_type='transfer_missing'`, `notes=reason`. **Do not move stock** — a missing box must remain an unaccounted record (the requester: "might be leaked and never found").
- [ ] **Step 2:** `flag_transfer_boxes_missing`: run `flag_missing` for the selection, then call `notify_transfer_missing_boxes` + `send_transfer_missing_notification` with the box details.
- [ ] **Step 3:** Register `POST /interunit/transfer-in/{transfer_out_id}/flag-missing`.
- [ ] **Step 4:** Keep `'Missing'` rows OUT of `count_remaining_in_transit` so a fully-dispositioned transfer can still reconcile (audit them separately). Confirm `count_remaining_in_transit` SQL (`pending_stock_tools.py:1797`) needs `AND status='In Transit'` — it already filters on that, so `'Missing'` is naturally excluded; verify no other counter double-counts.
- [ ] **Step 5 (verify):** Flag 23 boxes missing; assert: rows `status='Missing'`, 23 disposition rows, one email queued to `INVENTORY_MANAGER_EMAIL` cc admins, one WhatsApp attempt, and the transfer can now be closed.

### Task 4.4: "Flag missing" UI

**Files:** Modify the reconciliation panel (Task 1.2) and the returns queue (Task 3.4); add `flagMissing(transferOutId, payload)` to `interunitApiService.ts`.

- [ ] **Step 1:** Add a **"Flag as missing"** button (with mandatory reason) on both stuck in-transit boxes and un-acknowledged returns.
- [ ] **Step 2:** On success show "Flagged {n} boxes missing — inventory manager & admin notified."
- [ ] **Step 3 (verify):** Flag from the UI; confirm the notification fires and the box shows a red "Missing" badge.

---

## Phase 5 — Retire / wrap the blunt write-off

### Task 5.1: Make `close_transfer_in_with_shortage` route to a disposition

**Files:** Modify `interunit_tools.py:2427` + the `close-with-shortage` route, and `frontend/app/[company]/transfer/transferIn/page.tsx` (Close-with-Shortage button).

- [ ] **Step 1:** Change the existing "Close with Shortage" button to first open the **reconciliation panel** (Phase 1) so the user must pick a disposition (never-dispatched / return / missing) instead of silently deleting in-transit rows.
- [ ] **Step 2:** Keep `close_transfer_in_with_shortage` as a last-resort "write off (lost, no follow-up)" path **but** make it record `cold_stock_disposition` rows (`transfer_missing`, `reverted=FALSE`) and fire the missing-items notification rather than a bare `DELETE`. No box may leave the system without a disposition row.
- [ ] **Step 3 (verify):** Old callers still close the transfer, but now every written-off box has an audit row and an alert; assert no path performs an un-logged `DELETE FROM pending_transfer_stock`.

---

## Edge cases & invariants the executor MUST preserve

1. **Cold staging-header sharing.** Cold receives reuse the `interunit_transfer_in_header` id as the `cold_transfer_in_headers` id. Any cold disposition that flips status to `Received` must apply the staging purge **only when `count_remaining_in_transit==0`** (the `finalize_cold_transfer_in` gate, lines ~348-358), or it will orphan/duplicate headers (the 2026-06-19 bug — 14 orphans, ~1683 box rows). Partial cold dispositions must leave staging intact.
2. **`list_transfer_ins` NOT EXISTS filter** (`interunit_tools.py:2769`) hides interunit headers that have a matching cold header. New cold dispositions must not create a second interunit header that re-surfaces in that list.
3. **`reopen_transfer_in` for cold is already fragile** — it looks for staging `WHERE status='Received'` but cold reconcile leaves staging `'Pending'`. Do not build the return/never-dispatched flows on top of reopen; use the new primitives (`repark_to_source`, `open_return_leg`).
4. **`LINE-%` sentinel rows** in `pending_transfer_stock` are line-only placeholders, not physical boxes. All reconciliation selects must keep the existing `COALESCE(box_id,'') NOT LIKE 'LINE-%'` guard.
5. **`(box_id, transaction_no)` uniqueness** holds in every stock table and in `pending_transfer_stock`. Re-inserting must preserve the pair and let the table assign a fresh `id`; never re-use the old PK.
6. **Warehouse destinations have no destination stock row** (`*_boxes_v2` read-only for transfers). For return-of-excess from a warehouse destination, source the snapshot from `interunit_transfer_in_boxes` + `interunit_transfer_boxes`, not from a non-existent destination row.
7. **Notifications must never break a transaction.** Send email/WhatsApp **after commit**, in the existing background-thread / no-op-on-missing-creds pattern (`_send_email_background`, `send_whatsapp_message`).
8. **Idempotency.** Every disposition endpoint must be safe to retry — re-running rectify/return/ack on already-dispositioned boxes should be a no-op, not a double-insert (guard on the box's current `status` before acting).

---

## Open decisions for the requester (resolve before Phase 2)

1. **Approval authority** — who may execute each disposition? (Suggest reusing `TRANSFER_IN_DELETE_ALLOWED_EMAILS` for never-dispatched/missing, and gating return-acknowledge to the dispatching warehouse's users.) The matrix says "upon discussion with the team" — should the reason+approver fields be free-text, or a fixed reason-code dropdown?
2. **Missing-items recipients** — exact `INVENTORY_MANAGER_EMAIL`, `ADMIN_TEAM_EMAILS`, and `INVENTORY_MANAGER_WHATSAPP` values (the codebase has many `@candorfoods.in` constants in `email_notifier.py` but no inventory-manager entry yet).
3. **Auto-missing timeout** — should a box stuck `'In Transit'` (or `'Return In Transit'`) beyond N days auto-flag missing and notify, or is flagging always manual? (Auto would need a scheduled job; none exists today.)
4. **Cold vs warehouse parity** — confirm warehouse stock should be physically restored to `*_bulk_entry_boxes` (legacy) or to `*_boxes_v2` (modern). `park_in_pending` currently records `*_bulk_entry_boxes` as the warehouse source; returns will mirror that unless you want a migration.

---

## Test strategy

- **Unit (pytest, alongside existing `backend/test_*` and `test_cold_finalize_purges_staging.py`):** one test per primitive — `repark_to_source`, `open_return_leg`, `acknowledge_return`, `flag_missing` — asserting exact row movements and audit inserts on a scratch DB.
- **Integration:** the four end-to-end disposition flows from the decision matrix, each asserting `count_remaining_in_transit`, source/destination stock deltas, header status, `cold_stock_disposition` rows, and (mocked) notification calls.
- **Regression guards:** re-run `test_cold_finalize_purges_staging.py` and add a test asserting **no code path performs an un-logged `DELETE FROM pending_transfer_stock`** (grep-style or behavioural).
- **Cold-specific:** a cold partial disposition must NOT purge the staging header; a cold full disposition MUST.

## Rollback

- Phase 0 columns are additive (`ADD COLUMN IF NOT EXISTS`) — safe to leave in place.
- New routes/functions are additive; the legacy `close-with-shortage` keeps working through Phase 4 and is only re-pointed (not deleted) in Phase 5, so each phase is independently revertable by reverting its commit(s).

---

## Self-review notes

- **Coverage:** Use-case 1 → Phase 1; 2 → Phase 2; 3 → Phase 3 (excess) + Phase 4 (if return lost); 4 → Phase 3 (full-lot); 5 → Phase 4. The "acknowledged by dispatch team" requirement is Task 3.3. The "leaked in the system" risk is closed by Phase 5 (no un-logged deletes) + invariant #8.
- **Identifier consistency:** table/column/status/function/route/page names above are quoted from the live code (`interunit_tools.py`, `pending_stock_tools.py`, `cold_transfer_in_tools.py`, `interunit_server.py`, `email_notifier.py`, `whatsapp.py`, and the `frontend/app/[company]/transfer/*` pages). Confirm the cold receive route prefix (`/interunit/cold-transfer-in/...` vs `/cold-transfer-in/...`) against `interunit_server.py` before wiring Task 2.2/3.1 cold branches — the two are used interchangeably in notes and only one is registered.
