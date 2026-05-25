# Sales Order (SO) Creation

Tables involved in uploading, parsing, and managing sales orders.

```plantuml
@startuml SO_Creation
skinparam linetype ortho
skinparam ranksep 60
hide empty members

entity "so_header" as SOH {
  so_id              : SERIAL      <<PK>>
  --
  so_number          : TEXT
  so_date            : DATE
  customer_name      : TEXT
  common_customer_name : TEXT
  company            : TEXT
  voucher_type       : TEXT
  extraction_status  : TEXT
  created_at         : TIMESTAMPTZ
}

entity "so_line" as SOL {
  so_line_id         : SERIAL      <<PK>>
  so_id              : INT         <<FK>>
  --
  line_number        : INT
  sku_name           : TEXT
  item_category      : TEXT
  sub_category       : TEXT
  uom                : TEXT
  grp_code           : TEXT
  quantity           : NUMERIC
  quantity_units     : INT
  rate_inr           : NUMERIC
  rate_type          : TEXT
  amount_inr         : NUMERIC
  igst_amount        : NUMERIC
  sgst_amount        : NUMERIC
  cgst_amount        : NUMERIC
  total_amount_inr   : NUMERIC
  apmc_amount        : NUMERIC
  packing_amount     : NUMERIC
  freight_amount     : NUMERIC
  processing_amount  : NUMERIC
  item_type          : TEXT
  item_description   : TEXT
  sales_group        : TEXT
  match_score        : NUMERIC
  match_source       : TEXT
  release_mode       : TEXT
  status             : TEXT
  created_at         : TIMESTAMPTZ
}

entity "all_sku" as SKU {
  sku_id             : SERIAL      <<PK>>
  --
  particulars        : TEXT
  item_type          : TEXT
  item_group         : TEXT
  sub_group          : TEXT
  uom                : NUMERIC
  sale_group         : TEXT
  gst                : NUMERIC
  created_at         : TIMESTAMPTZ
}

entity "so_gst_reconciliation" as GST {
  recon_id              : SERIAL   <<PK>>
  so_line_id            : INT      <<FK>>
  so_id                 : INT      <<FK>>
  --
  expected_gst_rate     : NUMERIC
  actual_gst_rate       : NUMERIC
  expected_gst_amount   : NUMERIC
  actual_gst_amount     : NUMERIC
  gst_difference        : NUMERIC
  gst_type              : TEXT
  gst_type_valid        : BOOLEAN
  sgst_cgst_equal       : BOOLEAN
  total_with_gst_valid  : BOOLEAN
  uom_match             : BOOLEAN
  item_type_flag        : TEXT
  rate_type             : TEXT
  matched_item_description : TEXT
  matched_item_type     : TEXT
  matched_item_category : TEXT
  matched_sub_category  : TEXT
  matched_sales_group   : TEXT
  matched_uom           : NUMERIC
  match_score           : NUMERIC
  status                : TEXT
  notes                 : TEXT
  created_at            : TIMESTAMPTZ
}

entity "log_edit" as LOG {
  log_id        : SERIAL   <<PK>>
  --
  table_name    : TEXT
  record_id     : INT
  field_name    : TEXT
  action        : TEXT
  old_value     : TEXT
  new_value     : TEXT
  changed_by    : INT
  changed_at    : TIMESTAMPTZ
  request_id    : TEXT
  module        : TEXT
}

SOH ||--o{ SOL : "so_line.so_id"
SOL ||--o| GST : "gst_recon.so_line_id"
SOH ||--o{ GST : "gst_recon.so_id"
SOL ..> SKU   : "sku_name matches\nall_sku.particulars\n(soft, no DB FK)"

note right of SOL
  UNIQUE(so_id, line_number)
  status default = 'pending'
  release_mode default = 'all_upfront'
end note

note right of SKU
  No hard FK from so_line.
  Matched via fuzzy logic during
  SO parsing to populate item_category,
  item_type, sales_group, match_score.
end note

note bottom of LOG
  Generic audit table.
  record_id = PK of any changed row.
  module = 'so_intake' for SO changes.
  changed_by = auth_user.user_id (soft).
end note

@enduml
```

## Field Relations Summary

| Field | Table | Points To | Purpose |
|-------|-------|-----------|---------|
| `so_line.so_id` | so_line | `so_header.so_id` | Links each line item to its parent SO |
| `so_gst_reconciliation.so_line_id` | so_gst_reconciliation | `so_line.so_line_id` | One recon record per SO line |
| `so_gst_reconciliation.so_id` | so_gst_reconciliation | `so_header.so_id` | Direct SO header shortcut |
| `so_line.sku_name` | so_line | `all_sku.particulars` | Soft match — fuzzy-matched during SO parsing |
| `log_edit.record_id` | log_edit | Any table PK | Generic audit trail |
| `log_edit.changed_by` | log_edit | `auth_user.user_id` | Who made the change (soft ref) |

## Status Flow

```
so_header.extraction_status : pending → extracted → failed
so_line.status              : pending → approved → rejected
so_gst_reconciliation.status: ok | warning | error
```
