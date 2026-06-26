# SO Fulfillment

Bridges the SO module to production planning. Tracks how much of each SO line has been produced and dispatched.

```plantuml
@startuml SO_Fulfillment
skinparam linetype ortho
skinparam ranksep 70
hide empty members

entity "so_header" as SOH {
  so_id              : SERIAL  <<PK>>
  --
  so_number          : TEXT
  customer_name      : TEXT
}

entity "so_line" as SOL {
  so_line_id         : SERIAL  <<PK>>
  so_id              : INT     <<FK>>
  --
  sku_name           : TEXT
  quantity           : NUMERIC
  status             : TEXT
}

entity "so_fulfillment" as SF {
  fulfillment_id       : SERIAL   <<PK>>
  so_line_id           : INT      <<FK>>
  so_id                : INT      <<FK>>
  carryforward_from_id : INT      <<FK>>
  --
  financial_year       : TEXT
  fg_sku_name          : TEXT
  customer_name        : TEXT
  original_qty_kg      : NUMERIC
  revised_qty_kg       : NUMERIC
  pending_qty_kg       : NUMERIC
  produced_qty_kg      : NUMERIC
  dispatched_qty_kg    : NUMERIC
  order_status         : TEXT
  delivery_deadline    : DATE
  priority             : INT
  entity               : TEXT
  created_at           : TIMESTAMPTZ
  updated_at           : TIMESTAMPTZ
}

entity "so_revision_log" as SRL {
  revision_id     : SERIAL  <<PK>>
  fulfillment_id  : INT     <<FK>>
  --
  revision_type   : TEXT
  old_value       : TEXT
  new_value       : TEXT
  reason          : TEXT
  revised_by      : TEXT
  revised_at      : TIMESTAMPTZ
}

entity "production_plan_line" as PPL {
  plan_line_id              : SERIAL  <<PK>>
  --
  linked_so_fulfillment_ids : INT[]
  fg_sku_name               : TEXT
  planned_qty_kg            : NUMERIC
}

SOH ||--o{ SOL  : "so_line.so_id"
SOL ||--o{ SF   : "so_fulfillment.so_line_id\nUNIQUE per financial_year"
SOH ||--o{ SF   : "so_fulfillment.so_id"
SF  ||--o{ SRL  : "so_revision_log.fulfillment_id"
SF  ||--o{ SF   : "carryforward_from_id\n(self-reference)"
PPL }o..o{ SF   : "linked_so_fulfillment_ids[]\n(array, no hard FK)"

note right of SF
  order_status values:
    open | partial | fulfilled
    carryforward | cancelled

  pending_qty_kg =
    original_qty_kg
    - produced_qty_kg
    - dispatched_qty_kg

  UNIQUE(so_line_id, financial_year)
end note

note right of SRL
  revision_type values:
    qty_change | date_change
    carryforward | cancel

  Inserted on every planner revision.
end note

@enduml
```

## Field Relations Summary

| Field | Table | Points To | Purpose |
|-------|-------|-----------|---------|
| `so_fulfillment.so_line_id` | so_fulfillment | `so_line.so_line_id` | Which SO line this fulfillment tracks |
| `so_fulfillment.so_id` | so_fulfillment | `so_header.so_id` | Shortcut to parent SO header |
| `so_fulfillment.carryforward_from_id` | so_fulfillment | `so_fulfillment.fulfillment_id` | Self-ref: which previous record was carried forward |
| `so_revision_log.fulfillment_id` | so_revision_log | `so_fulfillment.fulfillment_id` | Revision belongs to one fulfillment row |
| `production_plan_line.linked_so_fulfillment_ids` | production_plan_line | `so_fulfillment.fulfillment_id[]` | Open orders this plan line addresses |

## Status Flow

```
open → partial → fulfilled
     ↘ carryforward  (new row created for next financial year)
     ↘ cancelled
```
