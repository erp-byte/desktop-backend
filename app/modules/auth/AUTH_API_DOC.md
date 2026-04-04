# Authentication & Access Control — API Documentation

**Base URL:** `/api/v1/auth`
**Total Endpoints:** 19
**Total Tables:** 5

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Database Schema](#2-database-schema)
3. [Auth Flow](#3-auth-flow)
4. [Endpoints](#4-endpoints)
5. [Permission Hierarchy](#5-permission-hierarchy)
6. [Default Roles](#6-default-roles)
7. [MCP Tool Mapping](#7-mcp-tool-mapping)

---

## 1. System Overview

### Architecture

```
┌──────────────┐     POST /login          ┌─────────────────────┐
│  Client       │ ─────────────────────── │  Auth Service         │
│  (Desktop/    │  {phone, password}      │                       │
│   Claude MCP) │                          │  1. Lookup auth_user  │
│               │                          │  2. AES decrypt+verify│
│               │  ◄─── {token, user,     │  3. Create session    │
│               │        permissions}      │  4. Return token      │
└──────┬───────┘                          └─────────────────────┘
       │
       │  Authorization: Bearer <token>
       │  (every subsequent request)
       ▼
┌──────────────────────────────────────────────────────────────┐
│                     Auth Middleware                            │
│                                                               │
│  1. Extract Bearer token from header                         │
│  2. Lookup auth_session (active + not expired)               │
│  3. Load user + role + is_admin                              │
│  4. Check permission for endpoint (module/sub/action)        │
│  5. Check scope (entity/warehouse/floor)                     │
│  6. If admin → bypass all checks                             │
│  7. If allowed → proceed                                      │
│  8. If denied → 403 Forbidden                                │
└──────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Password storage | AES-256 Fernet (reversible) | Admin can recover passwords |
| Session management | DB-stored tokens (not JWT) | Server can revoke instantly |
| Session expiry | 3 hours | Security for shared devices |
| Permission model | Hierarchical (module→sub→sub_sub×action) | Flexible, infinitely deep |
| Scope restrictions | Per entity/warehouse/floor | Row-level access control |
| Admin bypass | `is_admin=TRUE` on role | No permission checks needed |

---

## 2. Database Schema

### Entity Relationship

```
auth_role (1) ────── (N) auth_user
    │                        │
    │                        │
    │ (N)                    │ (N)
    ▼                        ▼
auth_role_permission    auth_session
    │
    │ (N)
    ▼
auth_permission
```

### Table: `auth_role`

| Column | Type | Description |
|--------|------|-------------|
| role_id | SERIAL PK | Auto-increment |
| role_name | TEXT UNIQUE | admin, planner, stores_manager, etc. |
| description | TEXT | Human-readable description |
| is_admin | BOOLEAN | TRUE = bypasses ALL permission checks |
| created_at | TIMESTAMPTZ | |

### Table: `auth_user`

| Column | Type | Description |
|--------|------|-------------|
| user_id | SERIAL PK | |
| phone | TEXT UNIQUE | Login identifier |
| password_encrypted | TEXT | AES-256 Fernet encrypted (reversible with AUTH_ENCRYPTION_KEY) |
| full_name | TEXT | Display name |
| email | TEXT | Optional |
| role_id | INT FK → auth_role | User's role |
| entity | TEXT | cfpl / cdpl / NULL (both) |
| is_active | BOOLEAN | FALSE = cannot login |
| created_at | TIMESTAMPTZ | |
| last_login_at | TIMESTAMPTZ | Updated on each login |

### Table: `auth_permission`

| Column | Type | Description |
|--------|------|-------------|
| permission_id | SERIAL PK | |
| module | TEXT | Top level: production, purchase, so, auth |
| sub_module | TEXT | Second level: plans, job_cards, indents, inventory |
| sub_sub_module | TEXT | Third level: approve, annexures, force_unlock, lifecycle |
| action | TEXT | view / create / edit / delete |
| description | TEXT | Human-readable |
| UNIQUE | | (module, sub_module, sub_sub_module, action) |

### Table: `auth_role_permission`

| Column | Type | Description |
|--------|------|-------------|
| role_id | INT FK → auth_role | |
| permission_id | INT FK → auth_permission | |
| allowed_entities | TEXT[] | {'cfpl'} or NULL (all) |
| allowed_warehouses | TEXT[] | {'W202'} or NULL (all) |
| allowed_floors | TEXT[] | {'1st Floor','tarraes'} or NULL (all) |
| PK | | (role_id, permission_id) |

### Table: `auth_session`

| Column | Type | Description |
|--------|------|-------------|
| session_id | SERIAL PK | |
| user_id | INT FK → auth_user | |
| token | TEXT UNIQUE | UUID v4 bearer token |
| ip_address | TEXT | Client IP at login |
| user_agent | TEXT | Browser/client info |
| created_at | TIMESTAMPTZ | |
| expires_at | TIMESTAMPTZ | created_at + 3 hours |
| last_activity_at | TIMESTAMPTZ | Updated on every request |
| is_active | BOOLEAN | FALSE = revoked |

---

## 3. Auth Flow

### Login

```
POST /api/v1/auth/login
  ↓
Check phone exists in auth_user (is_active=TRUE)
  ↓
AES-256 decrypt stored password, compare with provided
  ↓
Match? → Create auth_session (token=UUID, expires=NOW+3h)
       → Update auth_user.last_login_at
       → Load role + permissions
       → Return token + user + permissions
  ↓
No match? → 401 "Invalid phone number or password"
```

### Authenticated Request

```
Any request with Authorization: Bearer <token>
  ↓
Lookup auth_session WHERE token AND is_active AND expires_at > NOW()
  ↓
Not found? → 401 "Invalid or expired session"
  ↓
Load auth_user + auth_role
  ↓
User inactive? → 401
  ↓
Check permission for endpoint:
  - is_admin = TRUE? → ALLOW (skip all checks)
  - Find matching permission (module, sub_module, sub_sub_module, action)
  - Check scope: entity/warehouse/floor within allowed_*
  ↓
Permission granted? → Process request
Permission denied? → 403 "Forbidden"
```

### Session Lifecycle

```
Login → Token created (active, expires in 3h)
  ↓
Every request → last_activity_at updated
  ↓
3 hours pass → Token expires (auth fails)
  OR
Logout → Token deactivated (is_active=FALSE)
  OR
Admin deactivates user → ALL user sessions deactivated
  OR
Password changed → ALL sessions invalidated
```

### Password Encryption

```
Encrypt: plaintext → Fernet(AUTH_ENCRYPTION_KEY).encrypt() → stored
Decrypt: stored → Fernet(AUTH_ENCRYPTION_KEY).decrypt() → plaintext
Verify:  decrypt(stored) == provided_plaintext

Key: AUTH_ENCRYPTION_KEY environment variable (Fernet base64 key)
Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 4. Endpoints

### 4.1 Login (no auth required)

**`POST /api/v1/auth/login`**

**Request:**
```json
{
  "phone": "9876543210",
  "password": "mypassword123"
}
```

**Response (200):**
```json
{
  "token": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "session_id": 1,
  "expires_at": "2026-04-01T06:00:00",
  "user": {
    "user_id": 1,
    "phone": "9876543210",
    "full_name": "Kaushal Patel",
    "email": "kaushal@candorfoods.in",
    "entity": null,
    "role": {
      "role_id": 1,
      "role_name": "admin",
      "is_admin": true
    }
  },
  "permissions": [
    {
      "module": "production",
      "sub_module": "plans",
      "sub_sub_module": null,
      "action": "view",
      "description": "View production plans",
      "allowed_entities": null,
      "allowed_warehouses": null,
      "allowed_floors": null
    }
  ]
}
```

**Error (401):**
```json
{ "detail": "Invalid phone number or password" }
```

---

### 4.2 Logout

**`POST /api/v1/auth/logout`**

**Headers:** `Authorization: Bearer <token>`

**Response (200):**
```json
{ "logged_out": true }
```

---

### 4.3 Get Current User

**`GET /api/v1/auth/me`**

**Headers:** `Authorization: Bearer <token>`

**Response (200):**
```json
{
  "user_id": 1,
  "phone": "9876543210",
  "full_name": "Kaushal Patel",
  "email": "kaushal@candorfoods.in",
  "entity": null,
  "role_id": 1,
  "role_name": "admin",
  "is_admin": true,
  "session_id": 1,
  "permissions": [
    { "module": "production", "sub_module": "plans", "action": "view", "..." : "..." }
  ]
}
```

---

### 4.4 Change Password

**`POST /api/v1/auth/change-password`**

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{
  "old_password": "myoldpassword",
  "new_password": "mynewpassword"
}
```

**Response (200):**
```json
{ "changed": true, "message": "Password changed. All sessions invalidated." }
```

**Error (400):**
```json
{ "detail": "Current password is incorrect" }
```

---

### 4.5 Create User (admin only)

**`POST /api/v1/auth/users`**

**Request:**
```json
{
  "phone": "9876543211",
  "password": "temppassword",
  "full_name": "Ramesh Kumar",
  "role_id": 4,
  "email": "ramesh@candorfoods.in",
  "entity": "cfpl"
}
```

**Response (200):**
```json
{
  "user_id": 2,
  "phone": "9876543211",
  "full_name": "Ramesh Kumar",
  "role_id": 4
}
```

**Error (409):**
```json
{ "detail": "Phone number already registered" }
```

---

### 4.6 List Users (admin only)

**`GET /api/v1/auth/users`**

**Response (200):**
```json
[
  {
    "user_id": 1,
    "phone": "9876543210",
    "full_name": "Kaushal Patel",
    "email": "kaushal@candorfoods.in",
    "entity": null,
    "is_active": true,
    "created_at": "2026-04-01T00:00:00Z",
    "last_login_at": "2026-04-01T03:00:00Z",
    "role_id": 1,
    "role_name": "admin",
    "is_admin": true
  }
]
```

---

### 4.7 Edit User (admin only)

**`PUT /api/v1/auth/users/{user_id}`**

**Request** (only send fields to change):
```json
{
  "role_id": 2,
  "entity": "cfpl",
  "is_active": false
}
```

**Response (200):**
```json
{ "user_id": 2, "updated": true }
```

---

### 4.8 Deactivate User (admin only)

**`DELETE /api/v1/auth/users/{user_id}`**

Deactivates user AND invalidates all their sessions.

**Response (200):**
```json
{ "user_id": 2, "deactivated": true }
```

---

### 4.9 List Roles (admin only)

**`GET /api/v1/auth/roles`**

**Response (200):**
```json
[
  { "role_id": 1, "role_name": "admin", "description": "Full unrestricted access", "is_admin": true, "permission_count": 62 },
  { "role_id": 2, "role_name": "planner", "description": "Production planner", "is_admin": false, "permission_count": 35 },
  { "role_id": 4, "role_name": "team_leader", "description": "Team leader — job card execution", "is_admin": false, "permission_count": 18 },
  { "role_id": 8, "role_name": "viewer", "description": "Read-only access", "is_admin": false, "permission_count": 20 }
]
```

---

### 4.10 Create Role (admin only)

**`POST /api/v1/auth/roles`**

**Request:**
```json
{
  "role_name": "shift_supervisor",
  "description": "Night shift supervisor with limited access",
  "is_admin": false
}
```

**Response (200):**
```json
{ "role_id": 9, "role_name": "shift_supervisor" }
```

---

### 4.11 Set Role Permissions (admin only)

**`PUT /api/v1/auth/roles/{role_id}/permissions`**

Replaces ALL permissions for the role.

**Request:**
```json
{
  "permission_ids": [1, 2, 5, 10, 24, 25],
  "allowed_entities": ["cfpl"],
  "allowed_warehouses": ["W202"],
  "allowed_floors": null
}
```

**Response (200):**
```json
{ "role_id": 9, "permissions_set": 6 }
```

---

### 4.12 Get Role Permissions (admin only)

**`GET /api/v1/auth/roles/{role_id}/permissions`**

**Response (200):**
```json
{
  "role": {
    "role_id": 4,
    "role_name": "team_leader",
    "description": "Team leader — job card execution",
    "is_admin": false
  },
  "permissions": [
    {
      "permission_id": 24,
      "module": "production",
      "sub_module": "job_cards",
      "sub_sub_module": null,
      "action": "view",
      "description": "View job cards",
      "allowed_entities": null,
      "allowed_warehouses": null,
      "allowed_floors": null
    },
    {
      "permission_id": 25,
      "module": "production",
      "sub_module": "job_cards",
      "sub_sub_module": "lifecycle",
      "action": "edit",
      "description": "Assign/start/complete job cards",
      "allowed_entities": ["cfpl"],
      "allowed_warehouses": null,
      "allowed_floors": ["1st Floor", "tarraes"]
    }
  ]
}
```

---

### 4.13 List Permissions (admin only)

**`GET /api/v1/auth/permissions?module=production`**

Optional `module` filter.

**Response (200):**
```json
[
  { "permission_id": 1, "module": "production", "sub_module": "fulfillment", "sub_sub_module": null, "action": "view", "description": "View fulfillment records" },
  { "permission_id": 2, "module": "production", "sub_module": "fulfillment", "sub_sub_module": null, "action": "create", "description": "Sync fulfillment" },
  { "permission_id": 10, "module": "production", "sub_module": "plans", "sub_sub_module": "approve", "action": "create", "description": "Approve plans" }
]
```

---

### 4.14 Permission Hierarchy Tree (admin only)

**`GET /api/v1/auth/permissions/hierarchy`**

**Response (200):**
```json
{
  "production": {
    "fulfillment": {
      "_root": [
        { "permission_id": 1, "action": "view", "description": "View fulfillment records" },
        { "permission_id": 2, "action": "create", "description": "Sync fulfillment" },
        { "permission_id": 3, "action": "edit", "description": "Revise fulfillment" },
        { "permission_id": 4, "action": "delete", "description": "Cancel fulfillment" }
      ],
      "carryforward": [
        { "permission_id": 5, "action": "create", "description": "Carry forward orders" }
      ]
    },
    "plans": {
      "_root": [
        { "permission_id": 6, "action": "view", "description": "View production plans" },
        { "permission_id": 7, "action": "create", "description": "Create/generate plans" },
        { "permission_id": 8, "action": "edit", "description": "Edit plan lines" },
        { "permission_id": 9, "action": "delete", "description": "Delete plan lines" }
      ],
      "approve": [
        { "permission_id": 10, "action": "create", "description": "Approve plans" }
      ],
      "cancel": [
        { "permission_id": 11, "action": "create", "description": "Cancel plans" }
      ]
    },
    "job_cards": {
      "_root": [
        { "permission_id": 24, "action": "view", "description": "View job cards" }
      ],
      "lifecycle": [
        { "permission_id": 25, "action": "edit", "description": "Assign/start/complete job cards" }
      ],
      "force_unlock": [
        { "permission_id": 30, "action": "create", "description": "Force unlock job cards" }
      ]
    }
  },
  "purchase": {
    "_root": {
      "_root": [
        { "permission_id": 52, "action": "view", "description": "View purchase orders" },
        { "permission_id": 53, "action": "create", "description": "Upload purchase orders" }
      ]
    }
  },
  "auth": {
    "users": {
      "_root": [
        { "permission_id": 57, "action": "view", "description": "View users" },
        { "permission_id": 58, "action": "create", "description": "Create users" }
      ]
    }
  }
}
```

---

### 4.15 List Modules (admin only)

**`GET /api/v1/auth/modules`**

**Response (200):**
```json
[
  { "module": "auth", "sub_modules": ["users", "roles"], "permission_count": 7 },
  { "module": "production", "sub_modules": ["fulfillment", "plans", "mrp", "indents", "alerts", "orders", "job_cards", "inventory", "offgrade", "loss", "day_end", "discrepancy", "ai", "yield"], "permission_count": 49 },
  { "module": "purchase", "sub_modules": ["receive", "boxes"], "permission_count": 4 },
  { "module": "so", "sub_modules": [], "permission_count": 3 }
]
```

---

### 4.16 Create Module (admin only)

**`POST /api/v1/auth/modules`**

Auto-creates view/create/edit/delete permissions for each sub-module.

**Request:**
```json
{
  "module": "quality",
  "sub_modules": ["inspections", "certificates", "calibration"]
}
```

**Response (200):**
```json
{
  "module": "quality",
  "sub_modules": ["inspections", "certificates", "calibration"],
  "permissions_created": 12
}
```

This creates:
```
quality.inspections.view
quality.inspections.create
quality.inspections.edit
quality.inspections.delete
quality.certificates.view
quality.certificates.create
quality.certificates.edit
quality.certificates.delete
quality.calibration.view
quality.calibration.create
quality.calibration.edit
quality.calibration.delete
```

---

### 4.17 Create Permission (admin only)

**`POST /api/v1/auth/permissions/create`**

Create a single granular permission.

**Request:**
```json
{
  "module": "production",
  "sub_module": "job_cards",
  "sub_sub_module": "metal_detection_override",
  "action": "create",
  "description": "Override metal detection failure"
}
```

**Response (200):**
```json
{
  "permission_id": 63,
  "module": "production",
  "sub_module": "job_cards",
  "action": "create"
}
```

---

### 4.18 Edit Permission (admin only)

**`PUT /api/v1/auth/permissions/{permission_id}`**

**Request** (only send fields to change):
```json
{
  "description": "Updated description for this permission"
}
```

**Response (200):**
```json
{ "permission_id": 63, "updated": true }
```

---

### 4.19 Delete Permission (admin only)

**`DELETE /api/v1/auth/permissions/{permission_id}`**

Removes the permission AND removes it from all role mappings.

**Response (200):**
```json
{ "permission_id": 63, "deleted": true }
```

---

## 5. Permission Hierarchy

Permissions follow a 3-level hierarchy with 4 actions at each level:

```
module                          (level 1)
  └── sub_module                (level 2)
        └── sub_sub_module      (level 3)
              └── action: view | create | edit | delete

Permission check order (most specific first):
  1. Exact match: (module, sub_module, sub_sub_module, action)
  2. Fallback:    (module, sub_module, NULL, action)
  3. Fallback:    (module, NULL, NULL, action)

Example: Checking "can user approve a plan?"
  → Check: (production, plans, approve, create) ← exact
  → Fallback: (production, plans, NULL, create) ← broader
  → Fallback: (production, NULL, NULL, create) ← broadest
```

### Scope Restrictions

Each role-permission mapping can have scope restrictions:

| Scope | Column | Example | Effect |
|-------|--------|---------|--------|
| Entity | allowed_entities | `{'cfpl'}` | Can only access CFPL data |
| Warehouse | allowed_warehouses | `{'W202'}` | Can only access W202 warehouse |
| Floor | allowed_floors | `{'1st Floor','tarraes'}` | Can only access these floors |

`NULL` = no restriction (all allowed).

**Example:** Team leader restricted to CFPL, 1st Floor only:
```json
{
  "role_id": 4,
  "permission_id": 25,
  "allowed_entities": ["cfpl"],
  "allowed_warehouses": ["W202"],
  "allowed_floors": ["1st Floor"]
}
```

---

## 6. Default Roles

| Role | is_admin | Access Summary |
|------|----------|----------------|
| **admin** | TRUE | Everything — bypasses all permission checks |
| **planner** | FALSE | Plans (full CRUD), fulfillment, MRP, indents, AI, orders, + view all |
| **stores_manager** | FALSE | Inventory (full), day-end (full), offgrade, + view all |
| **team_leader** | FALSE | Job cards (lifecycle, output, annexures), + view all |
| **qc_inspector** | FALSE | Job card annexures + sign-offs, + view all |
| **floor_manager** | FALSE | Inventory, day-end, discrepancy, + job card view |
| **purchase_manager** | FALSE | Purchase module (full), indents + alerts (view/create) |
| **viewer** | FALSE | View-only across all modules |

---

## 7. MCP Tool Mapping

Every MCP tool maps to a permission. When Claude Desktop sends a bearer token, the auth middleware checks the user's role against the tool's required permission.

| MCP Tool | Required Permission |
|----------|-------------------|
| `ping` | production.*.view |
| `sync_fulfillment` | production.fulfillment.create |
| `get_fulfillment_list` | production.fulfillment.view |
| `save_production_plan` | production.plans.create |
| `approve_plan` | production.plans.approve.create |
| `list_job_cards` | production.job_cards.view |
| `assign_job_card` | production.job_cards.lifecycle.edit |
| `force_unlock_job_card` | production.job_cards.force_unlock.create |
| `record_output` | production.job_cards.output.create |
| `add_environment_data` | production.job_cards.annexures.create |
| `move_material` | production.inventory.move.create |
| `submit_dispatch` | production.day_end.dispatch.create |
| `report_discrepancy` | production.discrepancy.create |
| `resolve_discrepancy` | production.discrepancy.resolve.create |

Full mapping: 77 tools → 77 permission checks (see `permission_service.py` → `MCP_TOOL_PERMISSIONS`).

---

## Error Codes

| HTTP Code | When | Response |
|-----------|------|----------|
| 200 | Success | Varies |
| 401 | No token / invalid token / expired session / wrong password | `{"detail": "..."}` |
| 403 | Valid token but insufficient permissions / admin required | `{"detail": "Admin access required"}` or `{"detail": "Forbidden"}` |
| 409 | Duplicate phone / duplicate permission | `{"detail": "Phone number already registered"}` |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `AUTH_ENCRYPTION_KEY` | Yes | Fernet key for AES-256 password encryption. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

---

## Quick Start

### 1. Generate encryption key
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Add to .env: AUTH_ENCRYPTION_KEY=<generated_key>
```

### 2. Start server (creates tables + seeds roles/permissions)
```bash
python -m uvicorn app.main:app --reload
```

### 3. Create admin user (first time — via direct DB or API)
```sql
-- Or use the API after creating the first admin manually:
INSERT INTO auth_user (phone, password_encrypted, full_name, role_id)
VALUES ('9876543210', '<encrypted_password>', 'Admin User', 1);
```

### 4. Login
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"phone":"9876543210","password":"admin123"}'
```

### 5. Use token in all requests
```bash
curl http://localhost:8000/api/v1/production/health \
  -H "Authorization: Bearer <token_from_login>"
```

### 6. Configure Claude Desktop
```json
{
  "mcpServers": {
    "candor-production": {
      "type": "streamable-http",
      "url": "https://desktop-backend-vhf0.onrender.com/",
      "headers": {
        "Authorization": "Bearer <token_from_login>"
      }
    }
  }
}
```
