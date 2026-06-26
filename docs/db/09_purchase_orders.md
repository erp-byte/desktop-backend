# Purchase Orders & Receiving

PO upload by the purchase team and physical receiving by the stores team with lot/box tracking.

```plantuml
@startuml Purchase_Orders
skinparam linetype ortho
skinparam ranksep 65
hide empty members

entity "po_header" as POH {
  transaction_no        : TEXT     <<PK>>
  --
  entity                : TEXT
  po_date               : DATE
  voucher_type          : TEXT
  po_number             : TEXT
  order_reference_no    : TEXT
  narration             : TEXT
  vendor_supplier_name  : TEXT
  gross_total           : NUMERIC
  total_amount          : NUMERIC
  sgst_amount           : NUMERIC
  cgst_amount           : NUMERIC
  igst_amount           : NUMERIC
  round_off             : NUMERIC
  freight_transport_local      : NUMERIC
  apmc_tax                     : NUMERIC
  packing_charges              : NUMERIC
  freight_transport_charges    : NUMERIC
  loading_unloading_charges    : NUMERIC
  other_charges_non_gst        : NUMERIC
  customer_party_name   : TEXT
  vehicle_number        : TEXT
  transporter_name      : TEXT
  lr_number             : TEXT
  source_location       : TEXT
  destination_location  : TEXT
  challan_number        : TEXT
  invoice_number        : TEXT
  grn_number            : TEXT
  system_grn_date       : TIMESTAMPTZ
  purchased_by          : TEXT
  inward_authority      : TEXT
  warehouse             : TEXT
  status                : TEXT
  approved_by           : TEXT
  approved_at           : TIMESTAMPTZ
  created_at            : TIMESTAMPTZ
}

entity "po_line" as POL {
  transaction_no   : TEXT     <<PK>> <<FK>>
  line_number      : INT      <<PK>>
  --
  sku_name         : TEXT
  uom              : TEXT
  pack_count       : INT
  po_weight        : NUMERIC
  rate             : NUMERIC
  amount           : NUMERIC
  particulars      : TEXT
  item_category    : TEXT
  sub_category     : TEXT
  item_type        : TEXT
  sales_group      : TEXT
  gst_rate         : NUMERIC
  match_score      : NUMERIC
  match_source     : TEXT
  carton_weight    : NUMERIC
  status           : TEXT
  created_at       : TIMESTAMPTZ
}

entity "po_section" as POS {
  transaction_no   : TEXT  <<PK>> <<FK>>
  line_number      : INT   <<PK>> <<FK>>
  section_number   : INT   <<PK>>
  --
  lot_number       : TEXT
  box_count        : INT
  manufacturing_date : TEXT
  expiry_date      : TEXT
  created_at       : TIMESTAMPTZ
}

entity "po_box" as POB {
  box_id           : TEXT     <<PK>>
  transaction_no   : TEXT     <<FK>>
  line_number      : INT      <<FK>>
  section_number   : INT      <<FK>>
  --
  box_number       : INT
  net_weight       : NUMERIC
  gross_weight     : NUMERIC
  lot_number       : TEXT
  count            : INT
  created_at       : TIMESTAMPTZ
}

entity "job_card_rm_indent" as JCRM {
  rm_indent_id     : SERIAL  <<PK>>
  --
  scanned_box_ids  : TEXT[]
  batch_no         : TEXT
}

entity "floor_movement" as FM {
  movement_id      : SERIAL  <<PK>>
  --
  scanned_qr_codes : TEXT[]
  reason           : TEXT
}

POH ||--o{ POL  : "po_line.transaction_no"
POL ||--o{ POS  : "po_section.(txn_no, line_no)"
POS ||--o{ POB  : "po_box.(txn_no, line_no, section_no)"
JCRM ..> POB   : "scanned_box_ids[]\n(soft ref)"
FM   ..> POB   : "scanned_qr_codes[]\n(soft ref)"

note right of POH
  transaction_no = natural PK from Tally/SAP
  export (not a SERIAL).

  Two-phase filling:
  Phase 1 (Purchase Team):
    financial fields from Excel upload
  Phase 2 (Stores Team):
    logistics fields after physical receipt
    + grn_number, warehouse, vehicle_number
end note

note right of POS
  Groups boxes with the same lot/batch
  within a single PO line.
  One PO line can have multiple sections
  if material arrives in different batches.
end note

note right of POB
  box_id = QR code printed on each carton.
  Scanned during:
    - PO receiving
    - RM issue to job cards
    - Floor movements
    - Day-end balance scan
end note

@enduml
```

## Field Relations Summary

| Field | Table | Points To | Purpose |
|-------|-------|-----------|---------|
| `po_line.transaction_no` | po_line | `po_header.transaction_no` | Line belongs to this PO header |
| `po_section.(transaction_no, line_number)` | po_section | `po_line.(transaction_no, line_number)` | Section belongs to this PO line |
| `po_box.(transaction_no, line_number, section_number)` | po_box | `po_section.(...)` | Box belongs to this section |
| `po_box.transaction_no` | po_box | `po_header.transaction_no` | Direct shortcut to PO header |
| `job_card_rm_indent.scanned_box_ids` | job_card_rm_indent | `po_box.box_id[]` | Boxes used for RM issue |
| `job_card_rm_indent.batch_no` | job_card_rm_indent | `po_section.lot_number` | Lot traceability (soft ref) |
| `floor_movement.scanned_qr_codes` | floor_movement | `po_box.box_id[]` | Boxes involved in the movement |

## Hierarchy

```
po_header      (1 PO)
  └── po_line       (1 per article/SKU)
        └── po_section    (1 per lot/batch within the line)
              └── po_box       (1 per physical carton — has QR code)
```
