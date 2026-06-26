# Transfer Reconciliation & Revert — Process Flow

Companion to [2026-06-19-transfer-reconciliation-and-revert-plan.md](2026-06-19-transfer-reconciliation-and-revert-plan.md).
Open in a Markdown **preview** pane to render the Mermaid diagrams.

**Legend**

| Colour | Meaning |
|---|---|
| ⬜ Grey | **Existing** today — unchanged |
| 🟥 Red | **Existing GAP** being replaced (the blind write-off) |
| 🟩 Green | **NEW** in this plan |
| 🟦 Blue | Database table / stock store |
| 🟨 Yellow | Notification (existing infra, new trigger) |

---

## 1. End-to-end process flow

```mermaid
flowchart TD
    %% ============ EXISTING: DISPATCH ============
    A["Transfer OUT created<br/>create_transfer / create_cold_transfer_out"]:::exist
    A --> B["park_in_pending<br/>DELETE row from SOURCE stock table"]:::exist
    B --> PEND[("pending_transfer_stock<br/>status = 'In Transit'")]:::store
    B -.->|"NEW: also snapshot full source row"| SNAP["source_snapshot (JSONB)"]:::new

    %% ============ EXISTING: RECEIVE ============
    PEND --> D["Receive scan<br/>acknowledge_pending_boxes_batch"]:::exist
    D --> E["finalize_transfer_in<br/>pick_from_pending → DELETE pending row<br/>(cold: INSERT into *_cold_stocks)"]:::exist
    E --> F{"count_remaining_in_transit == 0 ?"}:::exist
    F -->|Yes| G["Both headers → 'Received'<br/>(clean, fully received)"]:::exist
    F -->|"No — e.g. 977 of 1000"| H["Headers stay Dispatch / Pending<br/>23 boxes still 'In Transit'"]:::exist

    %% ============ OLD GAP (being replaced) ============
    H -.->|"OLD path"| OLD["close_transfer_in_with_shortage<br/>DELETE remaining in-transit rows<br/>❌ boxes vanish, no disposition"]:::gap

    %% ============ NEW: RECONCILIATION ENTRY ============
    H ==>|"NEW path"| R["Reconciliation panel<br/>list_open_reconciliations<br/>GET /transfer-in/{id}/reconciliation"]:::new
    R --> DEC{"Choose disposition"}:::new

    %% ============ NEW: DISPOSITION 2 — NEVER DISPATCHED ============
    DEC -->|"Never sent<br/>(vehicle full / forgot)"| ND["rectify_never_dispatched<br/>POST .../rectify-never-dispatched"]:::new
    ND --> NDP["repark_to_source<br/>(rebuild box from source_snapshot)"]:::new
    NDP --> SRC[("SOURCE stock table<br/>cfpl/cdpl_cold_stocks · *_bulk_entry_boxes")]:::store
    NDP --> NDC["header → 'Received'<br/>has_variance = TRUE · unallocated_boxes = 23"]:::new

    %% ============ NEW: DISPOSITION 3 & 4 — RETURNS ============
    DEC -->|"Returned excess /<br/>full lot (already received)"| RET["initiate_transfer_return<br/>POST .../return"]:::new
    RET --> RLEG["open_return_leg<br/>remove from DEST stock"]:::new
    RLEG --> RP[("pending_transfer_stock<br/>status = 'Return In Transit'<br/>return_direction = 'to_source'")]:::store
    RP --> QUEUE["Dispatch-team Returns queue<br/>list_pending_returns<br/>GET /returns/pending?warehouse="]:::new
    QUEUE --> ACK{"Origin acknowledges?"}:::new
    ACK -->|Yes| ACKR["acknowledge_transfer_return<br/>POST /returns/{id}/acknowledge<br/>acknowledge_return → rebuild in ORIGIN"]:::new
    ACKR --> SRC
    ACK -->|"Never arrives"| MISS

    %% ============ NEW: DISPOSITION 5 — MISSING ============
    DEC -->|"Never found by either side"| MISS["flag_transfer_boxes_missing<br/>POST .../flag-missing"]:::new
    MISS --> MISSP[("pending_transfer_stock<br/>status = 'Missing'<br/>(stock NOT moved)")]:::store
    MISS --> NOTE["notify_transfer_missing_boxes (email)<br/>send_transfer_missing_notification (WhatsApp)"]:::notify
    NOTE --> WHO["Inventory Manager + Admin team"]:::notify

    %% ============ AUDIT (existing table, new types) ============
    NDP --> AUD[("cold_stock_disposition (AUDIT)<br/>transfer_never_dispatched ·<br/>transfer_return_excess / _full_lot ·<br/>transfer_missing")]:::store
    ACKR --> AUD
    MISS --> AUD

    %% ============ PHASE 5: rewire old gap ============
    OLD ==>|"Phase 5: rewire"| MISS

    classDef exist fill:#ececec,stroke:#888,color:#222;
    classDef new fill:#d4f7d4,stroke:#1a7f1a,stroke-width:3px,color:#0a3d0a;
    classDef gap fill:#ffe0e0,stroke:#cc0000,stroke-width:2px,color:#7a0000;
    classDef store fill:#e0ecff,stroke:#3366cc,color:#13306b;
    classDef notify fill:#fff3cd,stroke:#d39e00,color:#7a5b00;
```

---

## 2. What connects to what (data + control wiring)

```mermaid
flowchart LR
    subgraph FE["Frontend (Next.js) — frontend/app/[company]/transfer"]
        F1["transfer/page.tsx<br/>list + pending badge"]:::exist
        F2["transferIn/page.tsx<br/>+ Rectify / Return / Flag buttons"]:::newedge
        F3["cold-transfer/coldtransfer-in/page.tsx"]:::exist
        F4["transfer/returns/page.tsx<br/>(NEW) dispatch-team queue"]:::new
        F5["lib/interunitApiService.ts<br/>+ getReconciliation, rectify,<br/>initiateReturn, acknowledgeReturn, flagMissing"]:::newedge
    end

    subgraph API["Backend routes — interunit_server.py"]
        R1["GET .../reconciliation"]:::new
        R2["POST .../rectify-never-dispatched"]:::new
        R3["POST .../return"]:::new
        R4["GET /returns/pending"]:::new
        R5["POST /returns/{id}/acknowledge"]:::new
        R6["POST .../flag-missing"]:::new
    end

    subgraph LOGIC["Backend logic"]
        L1["interunit_tools.py<br/>orchestration fns"]:::newedge
        L2["pending_stock_tools.py<br/>repark_to_source · open_return_leg ·<br/>acknowledge_return · flag_missing"]:::newedge
        L3["cold_transfer_in_tools.py<br/>cold branches + staging gate"]:::newedge
        L4["shared/email_notifier.py + whatsapp.py"]:::notify
    end

    subgraph DB["Postgres warehouse_db"]
        T1[("pending_transfer_stock<br/>+ new statuses & columns")]:::newedge
        T2[("cfpl/cdpl_cold_stocks<br/>*_bulk_entry_boxes")]:::exist
        T3[("cold_stock_disposition (audit)<br/>+ new disposition_type values")]:::newedge
        T4[("interunit_transfers_header<br/>has_variance · unallocated_boxes")]:::exist
    end

    F1 & F2 & F3 & F4 --> F5 --> R1 & R2 & R3 & R4 & R5 & R6
    R1 & R2 & R3 & R4 & R5 & R6 --> L1 --> L2 & L3
    L2 & L3 --> T1 & T2 & T3 & T4
    R6 --> L4
    L1 --> L4

    classDef exist fill:#ececec,stroke:#888,color:#222;
    classDef new fill:#d4f7d4,stroke:#1a7f1a,stroke-width:3px,color:#0a3d0a;
    classDef newedge fill:#eafbe7,stroke:#1a7f1a,stroke-width:2px,stroke-dasharray:4 2,color:#0a3d0a;
    classDef notify fill:#fff3cd,stroke:#d39e00,color:#7a5b00;
```

> 🟩 solid green = brand-new file/route/function · 🟩 dashed green = **existing file, modified/extended** · ⬜ grey = unchanged.

---

## 3. Stock-movement summary (where each box ends up)

| Disposition | Trigger (NEW) | Box leaves | Box lands | 2nd-party ack | Alert | Header outcome |
|---|---|---|---|---|---|---|
| In-transit (no action) | — | source (already) | still `pending_transfer_stock` | — | — | stays Dispatch/Pending |
| **Never dispatched** | `rectify_never_dispatched` | `pending_transfer_stock` | **back to SOURCE stock** | No | — | Received + `has_variance` |
| **Return excess** | `initiate_transfer_return` | DEST stock | SOURCE (on ack) | **Origin acks** | — | Received-short |
| **Return full lot** | `initiate_transfer_return` | DEST stock | SOURCE (on ack) | **Origin acks** | — | Received-short |
| **Missing** | `flag_transfer_boxes_missing` | nothing moved | `status='Missing'` | No | **Email + WhatsApp** | can close |

---

## 4. The single most important change

```mermaid
flowchart LR
    OLD["BEFORE<br/>23 short boxes →<br/>DELETE FROM pending_transfer_stock<br/>❌ gone, untraceable"]:::gap
    NEW["AFTER<br/>23 short boxes → explicit disposition<br/>✅ returned to stock, OR<br/>✅ returned + acknowledged, OR<br/>✅ flagged Missing + alert<br/>— always an audit row"]:::new
    OLD ==> NEW

    classDef gap fill:#ffe0e0,stroke:#cc0000,stroke-width:2px,color:#7a0000;
    classDef new fill:#d4f7d4,stroke:#1a7f1a,stroke-width:3px,color:#0a3d0a;
```

**Invariant the new design enforces:** no box may leave `pending_transfer_stock` without a `cold_stock_disposition` audit row — closing the "leaked in the system and never found" hole.
