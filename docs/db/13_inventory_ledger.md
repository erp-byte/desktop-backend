# Inventory Batch Ledger

Single source of truth for all batch-level inventory. Tracks FIFO ordering, SO blocking, and a complete immutable event log.

```plantuml
@startuml Inventory_Ledger
skinparam linetype ortho
skinparam ranksep 65
hide empty members

entity "po_header" as POH {
  transaction_no        : TEXT  <<PK>>
  --
  vendor_supplier_name  : TEXT
  entity                : TEXT
}

entity "so_header" as SOH {
  so_id          : SERIAL  <<PK>>
  --
  so_number      : TEXT
  customer_name  : TEXT
}

entity "inventory_batch" as IB {
  batch_id             : TEXT     <<PK>>
  transaction_no       : TEXT     <<FK>>
  blocked_for_so_id    : INT      <<FK>>
  --
  sku_name             : TEXT
  item_type            : TEXT
  lot_number           : TEXT
  source               : TEXT
  inward_date          : DATE
  manufacturing_date   : DATE
  expiry_date          : DATE
  original_qty_kg      : NUMERIC
  current_qty_kg       : NUMERIC
  warehouse_id         : TEXT
  floor_id             : TEXT
  status               : TEXT
  blocked_by           : TEXT
  blocked_at           : TIMESTAMPTZ
  block_reason         : TEXT
  flag_reason          : TEXT
  flag_detail          : TEXT
  ownership            : TEXT
  entity               : TEXT
  created_at           : TIMESTAMPTZ
  updated_at           : TIMESTAMPTZ
}

entity "inventory_event_log" as IEL {
  event_id        : SERIAL  <<PK>>
  batch_id        : TEXT    <<FK>>
  --
  event_type      : TEXT
  from_status     : TEXT
  to_status       : TEXT
  from_location   : TEXT
  to_location     : TEXT
  quantity_kg     : NUMERIC
  reference_type  : TEXT
  reference_id    : INT
  so_id           : INT
  performed_by    : TEXT
  notes           : TEXT
  created_at      : TIMESTAMPTZ
}

entity "batch_block_history" as BBH {
  id            : SERIAL  <<PK>>
  batch_id      : TEXT    <<FK>>
  --
  action        : TEXT
  so_id         : INT
  blocked_by    : TEXT
  override_by   : TEXT
  override_note : TEXT
  created_at    : TIMESTAMPTZ
}

entity "internal_issue_note" as IIN {
  note_id              : SERIAL  <<PK>>
  batch_id             : TEXT    <<FK>>
  --
  note_number          : TEXT
  sku_name             : TEXT
  quantity_kg          : NUMERIC
  source_warehouse     : TEXT
  source_floor         : TEXT
  destination_floor    : TEXT
  purpose              : TEXT
  requested_by         : TEXT
  approved_by          : TEXT
  approved_at          : TIMESTAMPTZ
  status               : TEXT
  entity               : TEXT
  created_at           : TIMESTAMPTZ
}

entity "issue_note_line" as ISNL {
  id           : SERIAL  <<PK>>
  --
  lot_id       : TEXT
  tr_number    : TEXT
  box_id       : TEXT
  sku          : TEXT
  net_wt_issued : NUMERIC
}

POH ||--o{ IB   : "inventory_batch.transaction_no\n(NULL for non-PO sources)"
SOH ||--o{ IB   : "inventory_batch.blocked_for_so_id\n(when status=BLOCKED)"
IB  ||--o{ IEL  : "inventory_event_log.batch_id\n(immutable audit trail)"
IB  ||--o{ BBH  : "batch_block_history.batch_id"
IB  ||--o{ IIN  : "internal_issue_note.batch_id"
ISNL ..> IB    : "lot_id -> batch_id\n(soft ref)"

note right of IB
  batch_id = QR code or generated ID.
  source: INWARD | STOCK_TAKE | PRODUCTION | RETURN
  status: AVAILABLE | BLOCKED | ISSUED
    | IN_TRANSIT | INTERNAL_HOLD | FLAGGED

  FIFO key = inward_date ASC

  BLOCKED = reserved for a specific SO.
  FLAGGED = quality concern, skipped in FIFO.
  ownership: FLOOR | STORES
end note

note right of IEL
  Append-only. Never updated or deleted.
  Every inventory_batch change = one row here.

  event_type: CREATED | MOVED | BLOCKED
    | UNBLOCKED | ISSUED | RETURNED
    | FLAGGED | ADJUSTED | OVERRIDE

  reference_type + reference_id = polymorphic FK
  (job_card | so | indent | transfer | stock_take)
end note

note right of IIN
  For internal floor transfers NOT tied to an SO.
  Requires approval before execution.
  purpose: sorting | grading | reprocessing
    | qc | other
  status: pending | approved | rejected | completed
  note_number format: IIN-YYYYMMDD-NNN
end note

@enduml
```

## Field Relations Summary

| Field | Table | Points To | Purpose |
|-------|-------|-----------|---------|
| `inventory_batch.transaction_no` | inventory_batch | `po_header.transaction_no` | PO that brought in this batch (NULL for production) |
| `inventory_batch.blocked_for_so_id` | inventory_batch | `so_header.so_id` | SO this batch is reserved for |
| `inventory_event_log.batch_id` | inventory_event_log | `inventory_batch.batch_id` | Event belongs to this batch |
| `batch_block_history.batch_id` | batch_block_history | `inventory_batch.batch_id` | Block/unblock event for this batch |
| `internal_issue_note.batch_id` | internal_issue_note | `inventory_batch.batch_id` | Batch being transferred internally |
| `issue_note_line.lot_id` | issue_note_line (IMS) | `inventory_batch.batch_id` | Soft cross-reference from IMS module |
| `inventory_event_log.reference_id + reference_type` | inventory_event_log | Any table | Polymorphic reference to the triggering record |

## Batch Status Flow

```
AVAILABLE   --> (FIFO pick)        --> ISSUED
            --> (SO block)         --> BLOCKED
            --> (QC flag)          --> FLAGGED
            --> (floor move)       --> IN_TRANSIT --> AVAILABLE
            --> (hold)             --> INTERNAL_HOLD
BLOCKED     --> (override/cancel)  --> AVAILABLE
            --> (issued to JC)     --> ISSUED
FLAGGED     --> (cleared)          --> AVAILABLE
            --> (write-off)        --> off_grade_inventory
```

## FIFO Pick Logic

```
SELECT * FROM inventory_batch
WHERE sku_name    = :sku
  AND entity      = :entity
  AND status      = 'AVAILABLE'
  AND (blocked_for_so_id IS NULL
       OR blocked_for_so_id = :current_so_id)
ORDER BY inward_date ASC
LIMIT 1
```
