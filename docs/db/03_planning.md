# Production Planning & Plan Dashboard

Plan header, plan lines, and their connections to fulfillment, BOM, and machines.

```plantuml
@startuml Production_Planning
skinparam linetype ortho
skinparam ranksep 70
hide empty members

entity "so_fulfillment" as SF {
  fulfillment_id   : SERIAL  <<PK>>
  --
  fg_sku_name      : TEXT
  pending_qty_kg   : NUMERIC
  order_status     : TEXT
  priority         : INT
}

entity "bom_header" as BOM {
  bom_id           : SERIAL  <<PK>>
  --
  fg_sku_name      : TEXT
  pack_size_kg     : NUMERIC
  entity           : TEXT
}

entity "machine" as MACH {
  machine_id       : SERIAL  <<PK>>
  --
  machine_name     : TEXT
  floor            : TEXT
  entity           : TEXT
}

entity "production_plan" as PP {
  plan_id            : SERIAL  <<PK>>
  previous_plan_id   : INT     <<FK>>
  --
  plan_name          : TEXT
  entity             : TEXT
  plan_type          : TEXT
  plan_date          : DATE
  date_from          : DATE
  date_to            : DATE
  status             : TEXT
  ai_generated       : BOOLEAN
  ai_analysis_json   : JSONB
  revision_number    : INT
  approved_by        : TEXT
  approved_at        : TIMESTAMPTZ
  created_at         : TIMESTAMPTZ
}

entity "production_plan_line" as PPL {
  plan_line_id               : SERIAL  <<PK>>
  plan_id                    : INT     <<FK>>
  bom_id                     : INT     <<FK>>
  machine_id                 : INT     <<FK>>
  --
  fg_sku_name                : TEXT
  customer_name              : TEXT
  planned_qty_kg             : NUMERIC
  planned_qty_units          : INT
  priority                   : INT
  shift                      : TEXT
  stage_sequence             : TEXT[]
  estimated_hours            : NUMERIC
  linked_so_fulfillment_ids  : INT[]
  reasoning                  : TEXT
  status                     : TEXT
  created_at                 : TIMESTAMPTZ
}

entity "production_order" as PO {
  prod_order_id    : SERIAL  <<PK>>
  plan_line_id     : INT     <<FK>>
  --
  prod_order_number : TEXT
  batch_number      : TEXT
  fg_sku_name       : TEXT
  status            : TEXT
}

entity "purchase_indent" as PI {
  indent_id        : SERIAL  <<PK>>
  plan_line_id     : INT     <<FK>>
  --
  indent_number    : TEXT
  material_sku_name : TEXT
  status           : TEXT
}

entity "ai_recommendation" as AI {
  recommendation_id   : SERIAL  <<PK>>
  plan_id             : INT     <<FK>>
  --
  recommendation_type : TEXT
  status              : TEXT
  tokens_used         : INT
}

PP  ||--o{ PPL  : "plan_line.plan_id"
PP  ||--o{ PP   : "previous_plan_id\n(revision chain)"
PPL }o--|| BOM  : "plan_line.bom_id"
PPL }o--|| MACH : "plan_line.machine_id"
PPL }o..o{ SF   : "linked_so_fulfillment_ids[]\n(array, no hard FK)"
PPL ||--o{ PO   : "prod_order.plan_line_id"
PPL ||--o{ PI   : "purchase_indent.plan_line_id"
PP  ||--o{ AI   : "ai_recommendation.plan_id"

note right of PP
  plan_type: daily | weekly | full
  status: draft | approved | executed | cancelled

  Revision chain: each revision creates
  a new plan_id with previous_plan_id
  pointing to the prior version.
end note

note right of PPL
  stage_sequence[] drives job card generation.
  e.g. {'sorting','weighing','sealing','metal_detection'}

  linked_so_fulfillment_ids[] is an INT array
  managed in application code (no DB FK constraint).
end note

@enduml
```

## Field Relations Summary

| Field | Table | Points To | Purpose |
|-------|-------|-----------|---------|
| `production_plan_line.plan_id` | production_plan_line | `production_plan.plan_id` | Which plan this line belongs to |
| `production_plan_line.bom_id` | production_plan_line | `bom_header.bom_id` | BOM for this product |
| `production_plan_line.machine_id` | production_plan_line | `machine.machine_id` | Machine assigned for this run |
| `production_plan_line.linked_so_fulfillment_ids` | production_plan_line | `so_fulfillment.fulfillment_id[]` | Open orders this plan line addresses |
| `production_plan.previous_plan_id` | production_plan | `production_plan.plan_id` | Self-ref: prior revision of this plan |
| `production_order.plan_line_id` | production_order | `production_plan_line.plan_line_id` | Which plan line spawned this order |
| `purchase_indent.plan_line_id` | purchase_indent | `production_plan_line.plan_line_id` | Indent raised for material shortage |
| `ai_recommendation.plan_id` | ai_recommendation | `production_plan.plan_id` | Claude suggestion linked to a plan |

## Plan Status Flow

```
draft → approved → executed
      ↘ cancelled

Revision: approved → new draft (previous_plan_id set) → approved
```
