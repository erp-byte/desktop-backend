# Auth & Access Control

Users, roles, hierarchical permissions, role-permission mapping, and DB-managed sessions.

```plantuml
@startuml Auth
skinparam linetype ortho
skinparam ranksep 65
hide empty members

entity "auth_role" as ROLE {
  role_id       : SERIAL  <<PK>>
  --
  role_name     : TEXT
  description   : TEXT
  is_admin      : BOOLEAN
  created_at    : TIMESTAMPTZ
}

entity "auth_user" as USR {
  user_id             : SERIAL  <<PK>>
  role_id             : INT     <<FK>>
  --
  phone               : TEXT
  password_encrypted  : TEXT
  full_name           : TEXT
  email               : TEXT
  entity              : TEXT
  is_active           : BOOLEAN
  created_at          : TIMESTAMPTZ
  last_login_at       : TIMESTAMPTZ
}

entity "auth_permission" as PERM {
  permission_id    : SERIAL  <<PK>>
  --
  module           : TEXT
  sub_module       : TEXT
  sub_sub_module   : TEXT
  action           : TEXT
  description      : TEXT
}

entity "auth_role_permission" as RRP {
  role_id           : INT   <<PK>> <<FK>>
  permission_id     : INT   <<PK>> <<FK>>
  --
  allowed_entities   : TEXT[]
  allowed_warehouses : TEXT[]
  allowed_floors     : TEXT[]
}

entity "auth_session" as SESS {
  session_id         : SERIAL  <<PK>>
  user_id            : INT     <<FK>>
  --
  token              : TEXT
  ip_address         : TEXT
  user_agent         : TEXT
  created_at         : TIMESTAMPTZ
  expires_at         : TIMESTAMPTZ
  last_activity_at   : TIMESTAMPTZ
  is_active          : BOOLEAN
}

ROLE ||--o{ USR  : "auth_user.role_id"
ROLE ||--o{ RRP  : "role_permission.role_id"
PERM ||--o{ RRP  : "role_permission.permission_id"
USR  ||--o{ SESS : "auth_session.user_id"

note right of PERM
  3-level hierarchy:
    module -> sub_module -> sub_sub_module

  UNIQUE(module, sub_module, sub_sub_module, action)

  Examples:
    (production, job_cards, close,   create)
    (production, plans,     approve, create)
    (so,         NULL,      NULL,    view  )
    (purchase,   receive,   NULL,    create)
end note

note right of RRP
  Scope arrays restrict access:
    allowed_entities[]   e.g. ['cfpl']
    allowed_warehouses[] e.g. ['WH-01']
    allowed_floors[]     e.g. ['floor_1']
  NULL array = no restriction (all allowed).
end note

note right of SESS
  Token is Fernet-encrypted.
  Revoke by setting is_active = FALSE.
  expires_at enforced by API middleware.
end note

@enduml
```

## Field Relations Summary

| Field | Table | Points To | Purpose |
|-------|-------|-----------|---------|
| `auth_user.role_id` | auth_user | `auth_role.role_id` | User's assigned role |
| `auth_role_permission.role_id` | auth_role_permission | `auth_role.role_id` | Role side of the mapping |
| `auth_role_permission.permission_id` | auth_role_permission | `auth_permission.permission_id` | Permission side of the mapping |
| `auth_session.user_id` | auth_session | `auth_user.user_id` | Session belongs to a user |

## Permission Check Logic

```
To check if user U can perform action A on module M / sub_module S:

  1. Get role_id from auth_user WHERE user_id = U
  2. Find permission_id from auth_permission WHERE
       module = M AND sub_module = S AND action = A
  3. Check auth_role_permission WHERE
       role_id = role_id AND permission_id = permission_id
  4. Validate allowed_entities / allowed_floors scope if set
```

## Default Roles

| Role | Key Access |
|------|-----------|
| `admin` | All permissions |
| `planner` | plans, fulfillment, MRP, indents, AI |
| `stores_manager` | inventory, day_end, offgrade |
| `team_leader` | job_cards lifecycle & output |
| `qc_inspector` | job_cards annexures + sign_offs |
| `floor_manager` | inventory, day_end, discrepancy |
| `purchase_manager` | purchase module + indents view |
| `viewer` | All `view` actions only |
