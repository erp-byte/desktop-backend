# Database Overview — All Modules

High-level cross-module entity relationship diagram showing how all major tables connect.

```plantuml
@startuml DB_Overview
skinparam linetype ortho
skinparam ranksep 80
skinparam nodesep 40
hide empty members

entity "so_header" as SOH {
  so_id          : SERIAL  <<PK>>
  --
  so_number      : TEXT
  customer_name  : TEXT
  company        : TEXT
}

entity "so_line" as SOL {
  so_line_id     : SERIAL  <<PK>>
  so_id          : INT     <<FK>>
  --
  sku_name       : TEXT
  quantity       : NUMERIC
  status         : TEXT
}

entity "all_sku" as SKU {
  sku_id         : SERIAL  <<PK>>
  --
  particulars    : TEXT
  item_type      : TEXT
  item_group     : TEXT
}

entity "so_fulfillment" as SF {
  fulfillment_id  : SERIAL  <<PK>>
  so_line_id      : INT     <<FK>>
  so_id           : INT     <<FK>>
  --
  fg_sku_name     : TEXT
  pending_qty_kg  : NUMERIC
  order_status    : TEXT
}

entity "production_plan" as PP {
  plan_id        : SERIAL  <<PK>>
  --
  entity         : TEXT
  plan_date      : DATE
  status         : TEXT
}

entity "production_plan_line" as PPL {
  plan_line_id   : SERIAL  <<PK>>
  plan_id        : INT     <<FK>>
  bom_id         : INT     <<FK>>
  machine_id     : INT     <<FK>>
  --
  fg_sku_name    : TEXT
  planned_qty_kg : NUMERIC
}

entity "bom_header" as BOM {
  bom_id         : SERIAL  <<PK>>
  --
  fg_sku_name    : TEXT
  pack_size_kg   : NUMERIC
}

entity "machine" as MACH {
  machine_id     : SERIAL  <<PK>>
  --
  machine_name   : TEXT
  floor          : TEXT
  factory        : TEXT
}

entity "production_order" as PO {
  prod_order_id  : SERIAL  <<PK>>
  plan_line_id   : INT     <<FK>>
  bom_id         : INT     <<FK>>
  --
  batch_number   : TEXT
  fg_sku_name    : TEXT
  status         : TEXT
}

entity "job_card" as JC {
  job_card_id    : SERIAL  <<PK>>
  prod_order_id  : INT     <<FK>>
  machine_id     : INT     <<FK>>
  --
  stage          : TEXT
  status         : TEXT
}

entity "floor_inventory" as FI {
  inventory_id   : SERIAL  <<PK>>
  --
  sku_name       : TEXT
  floor_location : TEXT
  quantity_kg    : NUMERIC
}

entity "inventory_batch" as IB {
  batch_id             : TEXT  <<PK>>
  transaction_no       : TEXT  <<FK>>
  blocked_for_so_id    : INT   <<FK>>
  --
  sku_name             : TEXT
  status               : TEXT
  inward_date          : DATE
}

entity "po_header" as POH {
  transaction_no        : TEXT  <<PK>>
  --
  entity                : TEXT
  vendor_supplier_name  : TEXT
  status                : TEXT
}

entity "po_box" as POB {
  box_id         : TEXT  <<PK>>
  transaction_no : TEXT  <<FK>>
  --
  net_weight     : NUMERIC
  lot_number     : TEXT
}

entity "purchase_indent" as PI {
  indent_id      : SERIAL  <<PK>>
  plan_line_id   : INT     <<FK>>
  --
  material_sku_name : TEXT
  status            : TEXT
}

entity "auth_user" as USR {
  user_id        : SERIAL  <<PK>>
  role_id        : INT     <<FK>>
  --
  phone          : TEXT
  is_active      : BOOLEAN
}

entity "auth_role" as ROLE {
  role_id        : SERIAL  <<PK>>
  --
  role_name      : TEXT
}

SOH  ||--o{ SOL  : "so_line.so_id"
SOL  ||--o{ SF   : "so_fulfillment.so_line_id"
SOH  ||--o{ SF   : "so_fulfillment.so_id"
PPL  }o..o{ SF   : "linked_so_fulfillment_ids[]"
PP   ||--o{ PPL  : "plan_line.plan_id"
PPL  }o--|| BOM  : "plan_line.bom_id"
PPL  }o--|| MACH : "plan_line.machine_id"
PPL  ||--o{ PO   : "prod_order.plan_line_id"
PO   }o--|| BOM  : "prod_order.bom_id"
PO   ||--o{ JC   : "job_card.prod_order_id"
JC   }o--|| MACH : "job_card.machine_id"
PPL  ||--o{ PI   : "purchase_indent.plan_line_id"
POH  ||--o{ POB  : "po_box.transaction_no"
POH  ||--o{ IB   : "inv_batch.transaction_no"
SOH  ||--o{ IB   : "inv_batch.blocked_for_so_id"
USR  }o--|| ROLE : "auth_user.role_id"

@enduml
```

## File Index

| File | Module | Key Tables |
|------|--------|-----------|
| [01_so_creation.md](01_so_creation.md) | Sales Order creation | so_header, so_line, all_sku, so_gst_reconciliation |
| [02_so_fulfillment.md](02_so_fulfillment.md) | SO fulfillment tracking | so_fulfillment, so_revision_log |
| [03_planning.md](03_planning.md) | Production plan & dashboard | production_plan, production_plan_line |
| [04_bom_master.md](04_bom_master.md) | Bill of Materials | bom_header, bom_line, bom_process_route |
| [05_machines_floors.md](05_machines_floors.md) | Machines & capacity | machine, machine_capacity |
| [06_production_orders.md](06_production_orders.md) | Production orders | production_order |
| [07_job_cards.md](07_job_cards.md) | Job cards & annexures | job_card + 10 annexure tables |
| [08_inventory_store.md](08_inventory_store.md) | Floor inventory & store | floor_inventory, floor_movement, offgrade_*, purchase_indent |
| [09_purchase_orders.md](09_purchase_orders.md) | Purchase orders | po_header, po_line, po_section, po_box |
| [10_ims_module.md](10_ims_module.md) | Internal Material System | production_indent, issue_note, lot_block, qc_inspection, rtv_disposition |
| [11_auth.md](11_auth.md) | Auth & access control | auth_role, auth_user, auth_permission, auth_session |
| [12_analytics.md](12_analytics.md) | Analytics & day-end | process_loss, yield_summary, day_end_balance_scan, discrepancy_report |
| [13_inventory_ledger.md](13_inventory_ledger.md) | Inventory batch ledger | inventory_batch, inventory_event_log, batch_block_history |
