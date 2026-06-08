# Transfer Module — Page Documentation Index

Exhaustive, **per-page** documentation for every route under the Transfer module (`frontend-/app/[company]/transfer/`). Each file documents ONLY its own page: layout, buttons, pagination, page-in-page (modals/dialogs/hover), hover actions, keyboard/click directions, redirects, all logic, chips/KPIs/dashboards, tables, the specific API calls it makes, and the backend/DB tables + cross-module linkages those calls touch.

Base URL example: `https://candorims.netlify.app/CFPL/transfer` → route `/[company]/transfer`.

---

## Page map

| # | Doc | Route | Kind | Source file |
|---|---|---|---|---|
| 01 | [Transfer Dashboard](01-transfer-dashboard.md) | `/[company]/transfer` | Dashboard + 5 tabs | `transfer/page.tsx` (1680) |
| 02 | [Transfer In (Receive)](02-transfer-in.md) | `/[company]/transfer/transferIn` | Receive/GRN form | `transferIn/page.tsx` (3197) |
| 03 | [Transfer-In View](03-transfer-in-view.md) | `/[company]/transfer/transferIn/[transferInId]` | Read-only viewer | `transferIn/[transferInId]/page.tsx` (339) |
| 04 | [Transfer Summary Dashboard](04-summary-dashboard.md) | `/[company]/transfer/dashboard` | Analytics dashboard | `dashboard/page.tsx` (1131) |
| 05 | [New Transfer Request](05-new-request.md) | `/[company]/transfer/request` | Form | `request/page.tsx` (910) |
| 06 | [Request View](06-request-view.md) | `/[company]/transfer/request/[requestId]` | Read-only viewer | `request/[requestId]/page.tsx` (373) |
| 07 | [Transfer Form (Accept Request)](07-transferform-accept-request.md) | `/[company]/transfer/transferform?requestId=` | Transfer-OUT form | `transferform/page.tsx` (3053) |
| 08 | [Direct Transfer Out](08-direct-transfer-out.md) | `/[company]/transfer/directtransferform` (+`?editId=`) | Transfer-OUT create/edit | `directtransferform/page.tsx` (4135) |
| 09 | [Transfer-Out View](09-transfer-out-view.md) | `/[company]/transfer/view/[transferId]` | Read-only viewer | `view/[transferId]/page.tsx` (632) |
| 10 | [Delivery Challan (print)](10-delivery-challan.md) | `/[company]/transfer/dc/[transferId]` | Print/auto-print | `dc/[transferId]/page.tsx` (80) + `DeliveryChallan.tsx` (600) |
| 11 | [Inner Cold Transfer](11-inner-cold-transfer.md) | `/[company]/transfer/innercoldtransfer` (+`?editChallan=`) | Cold-storage bridge form | `innercoldtransfer/page.tsx` (910) |
| 12 | [Job Work Dashboard](12-job-work-dashboard.md) | `/[company]/transfer/job-work` | Dashboard + 3 tabs | `job-work/page.tsx` (2874) |
| 13 | [Job Work Material-Out](13-job-work-material-out.md) | `/[company]/transfer/job-work/material-out` (+`?edit=`) | Form + embedded list | `job-work/material-out/page.tsx` (1983) |
| 14 | [Job Work DC (print)](14-job-work-dc.md) | `/[company]/transfer/job-work/dc/[challanId]` | Print/auto-print | `job-work/dc/[challanId]/page.tsx` (141) + `JobWorkDC.tsx` (427) |
| 15 | [Jobwork Dashboard (reports)](15-jobwork-dashboard.md) | `/[company]/transfer/jobwork/dashboard` | Analytics dashboard | `jobwork/dashboard/page.tsx` (750) |

> Note: there is also a stray `components/transfer/transferIn/page.tsx` — it is NOT a route (a component file under `components/`), so it has no dedicated doc.

---

## Route tree

```
/[company]/transfer
├── (dashboard, 5 tabs: Requests / Transfer Out / Transfer In / Inner Cold / All)   → 01
├── dashboard                       (View Summary analytics)                        → 04
├── request                         (new request form)                              → 05
│   └── [requestId]                 (request view)                                  → 06
├── transferform?requestId=         (accept request → transfer-out)                 → 07
├── directtransferform (?editId=)   (direct transfer-out create/edit)               → 08
├── view/[transferId]               (transfer-out view)                             → 09
├── dc/[transferId]                 (delivery challan print)                        → 10
├── transferIn (?resume=)           (receive / GRN)                                 → 02
│   └── [transferInId]              (transfer-in view)                              → 03
├── innercoldtransfer (?editChallan=) (cold sub-unit / lot-relabel)                 → 11
├── job-work                        (job-work dashboard)                            → 12
│   ├── material-out (?edit=)        (material-out form)                            → 13
│   └── dc/[challanId]               (job-work DC print)                            → 14
└── jobwork/dashboard               (jobwork reports dashboard)                      → 15
```

---

## Cross-module linkages (at a glance)

- **Cold Storage** (`cfpl_cold_stocks` / `cdpl_cold_stocks`): the source for cold transfer-outs (deducted on dispatch) and the destination for transfer-ins (written on receive). Inner Cold Transfer (11) writes these directly via `/cold-storage/*`. Hover/Pending stock joins these for per-lot `lot_origin_unit`.
- **Inward** (`bulk_entry_boxes`): the source for warehouse transfer-outs (deducted on dispatch); the article/SKU dropdowns on the request/transfer forms reuse the **inward** `/inward/sku-dropdown` endpoint.
- **Pending Transfer Stock** (`pending_transfer_stock`): the in-transit ledger. Transfer-OUT parks rows here (deducting source); Transfer-IN picks from here (writing destination); the dashboard's Pending modal (01) reads/syncs it.
- **Job Work** (`jb_materialout_header/lines`, `/job-work/*`): a sub-section living under the transfer route; material-out deducts cold stock and writes `cold_stock_disposition` audit rows.

---

## API surface used across the module

- **`/interunit/*`** (via `lib/interunitApiService.ts`): requests, transfers (CRUD + list + detail), transfer-in (pending/acknowledge/finalize/edit/reopen/reconciliation), pending-stock list + backfill, dropdowns, stats, utils. Full method table in [02](02-transfer-in.md#7-api-client--interunitapiservicets-methods-used-by-this-page--full-reference).
- **`/transfer-dashboard/*`** (via `transferDashboardApi.ts`): all-data + filter-options for the summary dashboard (04).
- **`/cold-storage/*`** (raw fetch): inner-transfer create/list/delete, stock search, pick-boxes, storage-locations (08, 11, and the dashboard's Inner-Cold tab in 01).
- **`/job-work/*`** (raw fetch / `jobworkApiService.ts`): out/search, material-in, list, reports/dashboard, DC fetch (12–15).
- **`/inward/sku-dropdown`, `/interunit/categorial-search`**: article/SKU lookups on the forms (05, 07, 08).

---

## Common patterns & recurring gotchas (cross-page)

- **Company-agnostic backend:** the `[company]` segment is largely a frontend routing concern; several service methods accept `company` but don't put it in the URL. Some pages hardcode a company name on the payload (08, 13, 14).
- **Status label vs reality:** transfer-out forms always submit status `"Dispatch"`, but the backend downgrades to `"Partial"` when scanned boxes < ordered qty — the UI doesn't reflect this (07, 08).
- **Raw fetch vs service:** Inner-Cold, In-Transit count, several deletes, the transfer-out view (09), and job-work/jobwork dashboards bypass the shared API service and call raw `fetch` (sometimes without auth headers and with a hardcoded `localhost:8000` fallback).
- **Read-only viewers** (03, 06, 09): only "Back" buttons; all write actions live on the list/dashboard pages.
- **Print pages** (10, 14): thin loaders that auto-`window.print()` after a short timeout; layout lives in `DeliveryChallan.tsx` / `JobWorkDC.tsx`; no on-screen print button.
- **"Edited" badge:** driven by `interunit_transfers_header.edited_at`, written only by the edit path (`update_transfer` / `editTransferIn`) — not by routine churn.
- **Box-id/transaction-no & QR scheme** is shared with Inward; the cold path is heavily guarded against a known "boxes collapsed to 1" inventory-loss bug (08, 13).

---

*Generated per-page from the live source. Each doc carries `file:line` references; verify against the current code before relying on a specific line number, as files evolve.*
