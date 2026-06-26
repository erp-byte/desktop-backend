# BOM & Master Data

Bill of Materials: header, line items (raw material + packaging), and process route (manufacturing steps).

```plantuml
@startuml BOM_Master
skinparam linetype ortho
skinparam ranksep 60
hide empty members

entity "all_sku" as SKU {
  sku_id         : SERIAL  <<PK>>
  --
  particulars    : TEXT
  item_type      : TEXT
  item_group     : TEXT
  sub_group      : TEXT
  uom            : NUMERIC
  sale_group     : TEXT
  gst            : NUMERIC
  created_at     : TIMESTAMPTZ
}

entity "bom_header" as BOMH {
  bom_id          : SERIAL   <<PK>>
  --
  fg_sku_name     : TEXT
  customer_name   : TEXT
  pack_size_kg    : NUMERIC
  version         : INT
  is_active       : BOOLEAN
  effective_from  : DATE
  effective_to    : DATE
  item_group      : TEXT
  entity          : TEXT
  notes           : TEXT
  created_at      : TIMESTAMPTZ
}

entity "bom_line" as BOML {
  bom_line_id          : SERIAL   <<PK>>
  bom_id               : INT      <<FK>>
  --
  line_number          : INT
  material_sku_name    : TEXT
  item_type            : TEXT
  quantity_per_unit    : NUMERIC
  uom                  : TEXT
  loss_pct             : NUMERIC
  godown               : TEXT
  can_use_offgrade     : BOOLEAN
  offgrade_max_pct     : NUMERIC
  created_at           : TIMESTAMPTZ
}

entity "bom_process_route" as BPR {
  route_id        : SERIAL   <<PK>>
  bom_id          : INT      <<FK>>
  --
  step_number     : INT
  process_name    : TEXT
  stage           : TEXT
  std_time_min    : NUMERIC
  loss_pct        : NUMERIC
  qc_check        : TEXT
  machine_type    : TEXT
  created_at      : TIMESTAMPTZ
}

entity "production_plan_line" as PPL {
  plan_line_id   : SERIAL  <<PK>>
  bom_id         : INT     <<FK>>
  --
  fg_sku_name    : TEXT
  planned_qty_kg : NUMERIC
}

entity "production_order" as PO {
  prod_order_id  : SERIAL  <<PK>>
  bom_id         : INT     <<FK>>
  --
  fg_sku_name    : TEXT
  batch_number   : TEXT
}

entity "job_card" as JC {
  job_card_id    : SERIAL  <<PK>>
  bom_id         : INT     <<FK>>
  --
  stage          : TEXT
  step_number    : INT
}

BOMH ||--o{ BOML : "bom_line.bom_id\n(RM + PM materials)"
BOMH ||--o{ BPR  : "bom_process_route.bom_id\n(ordered steps)"
PPL  }o--|| BOMH : "plan_line.bom_id"
PO   }o--|| BOMH : "prod_order.bom_id"
JC   }o--|| BOMH : "job_card.bom_id"
SKU  ..>  BOMH   : "fg_sku_name matches\nall_sku.particulars\n(soft)"
SKU  ..>  BOML   : "material_sku_name matches\nall_sku.particulars\n(soft)"

note right of BOMH
  One BOM per (fg_sku_name, customer_name, pack_size_kg).
  customer_name = NULL means a generic BOM.
  Only is_active = TRUE rows are used for planning.
  UNIQUE enforced in application, not DB.
end note

note right of BOML
  item_type = 'rm' -> Raw Material
  item_type = 'pm' -> Packaging Material
  godown: 'RM Store' or 'PM Store'
  UNIQUE(bom_id, line_number)
end note

note right of BPR
  Each step becomes one job_card when a
  production order is created.
  UNIQUE(bom_id, step_number)
  machine_type is a hint for machine selection
  (matched against machine.machine_type).
end note

@enduml
```

## Field Relations Summary

| Field | Table | Points To | Purpose |
|-------|-------|-----------|---------|
| `bom_line.bom_id` | bom_line | `bom_header.bom_id` | Line belongs to this BOM |
| `bom_process_route.bom_id` | bom_process_route | `bom_header.bom_id` | Route step belongs to this BOM |
| `bom_header.fg_sku_name` | bom_header | `all_sku.particulars` | Soft match — the finished good name |
| `bom_line.material_sku_name` | bom_line | `all_sku.particulars` | Soft match — RM/PM ingredient name |
| `production_plan_line.bom_id` | production_plan_line | `bom_header.bom_id` | BOM used in planning |
| `production_order.bom_id` | production_order | `bom_header.bom_id` | BOM locked at order creation |
| `job_card.bom_id` | job_card | `bom_header.bom_id` | BOM referenced during execution |

## BOM Lookup Priority

```
1. Look for BOM matching (fg_sku_name, customer_name, pack_size_kg) → customer-specific
2. Fall back to (fg_sku_name, NULL, pack_size_kg)                   → generic BOM
```
