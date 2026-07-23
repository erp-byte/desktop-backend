# Candor purchase flow — Vendor / PO → Goods inward → RM stock

End-to-end procurement path in the v2 stack: from a purchase order (or a
no-PO walk-in) through stores receipt, boxing/QR, and QC intimation, to raw
material landing in inventory — where it feeds the
[SO → Job-Card flow](./so-to-jobcard-flow.md) as an RM indent.
Endpoints and tables below are the real ones in `server_replica/app/modules/purchase`.

## Flow diagram

```mermaid
flowchart TD
    V["Vendor master<br/>(purchase/vendor-management)"] -.->|supplier for PO| P1

    subgraph BUY["Purchase Team — create PO"]
        P1["Upload PO Excel<br/>(purchase/po-creation)"] --> P2["Preview · POST /po/preview<br/>match SKUs vs master_items<br/>duplicate detection · rate-variance warnings<br/>(read-only, no DB writes)"]
        P2 --> P3["Commit · POST /po/commit<br/>create / update / upsert<br/>po_header + po_line"]
        P3 --> P4["PO recorded · status = pending"]
    end

    P4 --> R0{"Goods arrive at gate"}
    R0 -->|against a PO| S1["Stores receive · PUT /{txn}/receive<br/>GRN no + date · invoice · vehicle · transporter<br/>LR · net weights · warehouse · inward authority"]
    R0 -->|no PO| W1["Walk-in intimation · POST /walk-in-intimation<br/>pick SKUs from master · txn = WI-YYYYMMDDHHMMSS"]

    subgraph INWARD["Stores Team — goods inward (purchase/material-in)"]
        S1 --> B1["Create boxes · POST /{txn}/boxes<br/>split into boxes + QR labels<br/>→ INSERT inventory_batch (type=rm · INWARD)"]
        B1 --> B2["Print box QR (…/boxes/print)"]
    end

    B2 --> I1["QC inward intimation · POST /{txn}/intimation<br/>WhatsApp QC team + article-list image"]
    W1 --> I1

    I1 --> Q1["QC inward inspection<br/>(QC app — separate)"]
    Q1 -->|accepted| INV["RM usable in inventory<br/>inventory_batch · box lookup by QR"]
    Q1 -->|rejected / hold| RET["Quarantine / return to vendor"]

    INV --> JC["→ RM indent into Job Cards<br/>(enters the SO → Job-Card flow)"]
```

## Phase → mechanics

| Phase | Endpoint(s) | Key tables | Result |
|---|---|---|---|
| Vendor master | `purchase/vendor-management` CRUD | vendor tables | supplier available for POs |
| PO preview | `POST /po/preview` | reads `master_items`, `po_header` | matched lines, warnings — **no writes** |
| PO commit | `POST /po/commit` | `po_header`, `po_line` | PO stored · `status=pending` |
| Stores receive | `PUT /{txn}/receive` | `po_header` (GRN, logistics) | goods-inward + GRN recorded |
| Boxing | `POST /{txn}/boxes` · `…/boxes/print` | `po_box`, **`inventory_batch` (rm · INWARD)** | RM posted to stock + QR labels |
| QC intimation | `POST /{txn}/intimation` | QC arrivals log | QC team notified (WhatsApp) |
| Walk-in (no PO) | `POST /walk-in-intimation` | QC arrivals (`WI-*` txn) | ad-hoc arrival logged + QC notified |
| QC inspection | QC app (separate) | — | accept → usable · reject → quarantine/return |

## Notes

- **Two entry paths.** Material either arrives **against a PO** (Purchase Team
  ingested it first → Stores receives against that `TR-*` transaction) or as a
  **walk-in** with no PO (`WI-*` transaction generated on the spot from the SKU
  master). Both converge on the QC inward intimation.
- **When stock is created.** RM enters inventory at **box creation**
  (`inventory_batch` type `rm`, movement `INWARD`) — not at PO commit and not at
  QC. QC inward intimation is a best-effort WhatsApp notification (arrivals are
  persisted first so they survive a WA outage); the actual pass/fail inspection
  lives in the separate QC app.
- **No PO approval gate.** `preview → commit` *is* the creation; a committed PO
  is immediately `pending`. Lines are replaced wholesale on update (the preview
  Excel is treated as the source of truth for that PO).
- **Hand-off to production.** Boxed, QC-cleared RM is what the production side
  indents onto a job card (`job_card_rm_indent_v2`) — the join point with
  [so-to-jobcard-flow.md](./so-to-jobcard-flow.md).
