# Production Orders

A production order is created from an approved plan line. It carries one batch through the full process.

```plantuml
@startuml Production_Orders
skinparam linetype ortho
skinparam ranksep 70
hide empty members

entity "production_plan_line" as PPL {
  plan_line_id     : SERIAL  <<PK>>
  plan_id          : INT     <<FK>>
  bom_id           : INT     <<FK>>
  --
  fg_sku_name      : TEXT
  planned_qty_kg   : NUMERIC
  status           : TEXT
}

entity "bom_header" as BOMH {
  bom_id           : SERIAL  <<PK>>
  --
  fg_sku_name      : TEXT
  pack_size_kg     : NUMERIC
}

entity "production_order" as PO {
  prod_order_id    : SERIAL  <<PK>>
  plan_line_id     : INT     <<FK>>
  bom_id           : INT     <<FK>>
  --
  prod_order_number : TEXT
  fg_sku_name      : TEXT
  customer_name    : TEXT
  batch_number     : TEXT
  batch_size_kg    : NUMERIC
  net_wt_per_unit  : NUMERIC
  best_before      : DATE
  total_stages     : INT
  status           : TEXT
  entity           : TEXT
  factory          : TEXT
  floor            : TEXT
  created_at       : TIMESTAMPTZ
}

entity "job_card" as JC {
  job_card_id      : SERIAL  <<PK>>
  prod_order_id    : INT     <<FK>>
  --
  job_card_number  : TEXT
  step_number      : INT
  stage            : TEXT
  batch_number     : TEXT
  status           : TEXT
}

PPL  ||--o{ PO   : "prod_order.plan_line_id"
BOMH ||--o{ PO   : "prod_order.bom_id\n(locked at creation)"
PO   ||--o{ JC   : "job_card.prod_order_id\n(one JC per stage)"

note right of PO
  prod_order_number: PO-YYYY-NNNN  e.g. PO-2026-0042
  batch_number:      BYYY-NNN      e.g. B2026-042

  total_stages = count of bom_process_route rows
    for this bom_id.

  status:
    created | job_cards_issued | in_progress
    completed | cancelled
end note

note right of JC
  job_card_number: PO-2026-0042/1
    (prod_order_number / step_number)

  batch_number, fg_sku_name, customer_name
  are denormalized copies from production_order
  for quick access without joins.
end note

@enduml
```

## Field Relations Summary

| Field | Table | Points To | Purpose |
|-------|-------|-----------|---------|
| `production_order.plan_line_id` | production_order | `production_plan_line.plan_line_id` | Approved plan line that spawned this order |
| `production_order.bom_id` | production_order | `bom_header.bom_id` | BOM snapshot locked at order creation |
| `job_card.prod_order_id` | job_card | `production_order.prod_order_id` | All job cards belonging to this batch |

## Denormalized Fields on `job_card`

Copied from upstream at creation — do **not** auto-update if upstream changes:

| Field on `job_card` | Copied from |
|---------------------|-------------|
| `fg_sku_name` | `production_order.fg_sku_name` |
| `customer_name` | `production_order.customer_name` |
| `batch_number` | `production_order.batch_number` |
| `batch_size_kg` | `production_order.batch_size_kg` |
| `floor` | `machine.floor` at assignment time |
| `factory` | `machine.factory` at assignment time |

## Status Flow

```
production_order.status:
  created → job_cards_issued → in_progress → completed
           ↘ cancelled (at any stage)
```
