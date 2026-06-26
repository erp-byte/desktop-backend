# Machines & Floors

Machine master data and per-machine capacity per product group per stage.

```plantuml
@startuml Machines_Floors
skinparam linetype ortho
skinparam ranksep 70
hide empty members

entity "machine" as MACH {
  machine_id       : SERIAL  <<PK>>
  --
  machine_name     : TEXT
  machine_type     : TEXT
  category         : TEXT
  capable_stages   : TEXT[]
  floor            : TEXT
  factory          : TEXT
  status           : TEXT
  entity           : TEXT
  created_at       : TIMESTAMPTZ
}

entity "machine_capacity" as MC {
  capacity_id         : SERIAL  <<PK>>
  machine_id          : INT     <<FK>>
  --
  stage               : TEXT
  item_group          : TEXT
  capacity_kg_per_hr  : NUMERIC
  created_at          : TIMESTAMPTZ
}

entity "production_plan_line" as PPL {
  plan_line_id     : SERIAL  <<PK>>
  machine_id       : INT     <<FK>>
  --
  fg_sku_name      : TEXT
  planned_qty_kg   : NUMERIC
  estimated_hours  : NUMERIC
}

entity "job_card" as JC {
  job_card_id      : SERIAL  <<PK>>
  machine_id       : INT     <<FK>>
  --
  stage            : TEXT
  floor            : TEXT
  factory          : TEXT
}

entity "bom_process_route" as BPR {
  route_id         : SERIAL  <<PK>>
  --
  stage            : TEXT
  machine_type     : TEXT
}

MACH ||--o{ MC   : "machine_capacity.machine_id\n(capacity per stage per item_group)"
PPL  }o--|| MACH : "plan_line.machine_id"
JC   }o--|| MACH : "job_card.machine_id"
BPR  ..>   MACH  : "machine_type hint\n(no hard FK)"

note right of MACH
  machine_type: sorting_table | sealer |
    scale | metal_detector | etc.
  category: processing | packaging | quality
  capable_stages[]: set of stages this
    machine can perform.
  status: active | maintenance | retired

  floor and factory are plain TEXT labels
  (no separate floor/factory master table).
end note

note right of MC
  UNIQUE(machine_id, stage, item_group)

  Used by AI planner to calculate estimated_hours:
    estimated_hours =
      planned_qty_kg / capacity_kg_per_hr
end note

note right of JC
  job_card.floor and job_card.factory are
  copied from machine at creation time
  (denormalized snapshot).
end note

@enduml
```

## Field Relations Summary

| Field | Table | Points To | Purpose |
|-------|-------|-----------|---------|
| `machine_capacity.machine_id` | machine_capacity | `machine.machine_id` | Capacity row belongs to this machine |
| `production_plan_line.machine_id` | production_plan_line | `machine.machine_id` | Machine assigned at planning time |
| `job_card.machine_id` | job_card | `machine.machine_id` | Physical machine running this job card |
| `bom_process_route.machine_type` | bom_process_route | — | Text hint; code queries `machine` by type |

## Floors

Floors are **not a separate table**. They exist as the `floor` TEXT column on `machine` and `job_card`. The GET `/floors` API derives the virtual floor list from distinct `machine.floor` values and joins active job card counts.

## Capacity Formula

```
item_group = bom_header.item_group        (e.g. 'cashew')
stage      = bom_process_route.stage      (e.g. 'weighing')

capacity = machine_capacity WHERE
             machine_id = plan_line.machine_id
             AND stage      = stage
             AND item_group = item_group

estimated_hours = planned_qty_kg / capacity.capacity_kg_per_hr
```
