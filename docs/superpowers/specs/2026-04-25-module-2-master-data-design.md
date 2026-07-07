# Module 2: Master Data — Design Spec

**Date:** 2026-04-25
**Status:** Pending review
**Approach:** GraphQL-only (Strawberry on FastAPI), SQLAlchemy 2.x async, no caching, auth scaffolded as no-op

---

## Goal

CRUD over four reference domains (`lookup_value`, `all_sku`, `customer_master`, `qc_parameter_master`) exposed through a single GraphQL endpoint at `POST /graphql`. No REST endpoints for these domains. Establishes the GraphQL + SQLAlchemy 2.x + Strawberry pattern that Modules 3–10 will reuse.

## Roadmap context

This is **Module 2 of Phase 1** (new modules first, existing REST untouched). The locked sequence:

| Phase | What | Modules / Scope |
|---|---|---|
| 1 | Build new modules as GraphQL, parallel to existing REST | Modules 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 |
| 2 | Migrate existing REST modules to GraphQL one at a time | webhooks/ws → so → purchase → production → amendment → auth |
| 3 | Remove FastAPI REST scaffolding entirely | Cleanup once all clients are off REST |

Module 2 does **not** touch the existing `/api/v1/auth`, `/api/v1/so`, `/api/v1/purchase`, `/api/v1/production`, `/api/v1/amendment`, `/webhooks`, `/ws`, `/internal/events` REST surfaces. Those keep serving the Android app and frontend during Phase 1.

## Locked decisions (agreed during brainstorming)

| # | Decision |
|---|---|
| Stack | GraphQL via Strawberry; SQLAlchemy 2.x async sharing the existing `asyncpg` pool; same FastAPI app + same Postgres DB as today |
| Cache | **No cache**. No Redis, no in-process TTL, no `@cacheControl` directive. Postgres directly on every read |
| Auth | `@auth` directive scaffolded but **no-op** — every check returns allow, audit context uses hard-coded `system` actor. Module 1 (Auth from prompts) is deferred; existing `auth_user`/`auth_role`/JWT middleware is **not** wired into Module 2 |
| SKU writes | Excel ingest remains sole writer; `createSKU`/`updateSKU` are **not** exposed in GraphQL. SKU is read-only |
| `deactivateCustomer` | **No open-SO precondition.** Soft-deactivation always succeeds. Consuming UIs handle "deactivated customer with active references" gracefully |
| Customer scope | Standard B2B India: `customer_master` + `customer_ship_to` (1:N). Includes credit fields, payment terms, entity scope. No banking, no compliance docs, no SKU code mapping (deferred to Module 2.5) |

## 1. Scope

| Domain | Table | Existing? | Notes |
|---|---|---|---|
| Dropdowns (11 types) | `lookup_value` | NEW | Single table, type-discriminated |
| SKU master | `all_sku` | EXISTS — 3,685 rows | Column is `gst` not `gst_rate`; has `batch_strategy`, `min_shelf_life_days`. Read-only via GraphQL |
| Customer master | `customer_master` + `customer_ship_to` | NEW | Backfill from 7 free-text source tables |
| QC parameters | `qc_parameter_master` | NEW | Catalog only; specs live in Module 8 |

## 2. Database

### 2.1 `gen_short_id()` Postgres function (NEW — required by 2.2 and 2.3)

The user's spec assumes `gen_short_id()` as a `DEFAULT` for text PKs but no such function exists in the DB. Define it once for use across Modules 2–10:

```sql
CREATE OR REPLACE FUNCTION gen_short_id() RETURNS text AS $$
DECLARE
  alphabet constant text := 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  result text := '';
  i int;
BEGIN
  FOR i IN 1..12 LOOP
    result := result || substr(alphabet, 1 + floor(random() * 62)::int, 1);
  END LOOP;
  RETURN result;
END;
$$ LANGUAGE plpgsql VOLATILE;
```

12-char base62 random. Collision space ~3.2 × 10²¹; PK uniqueness will catch the astronomical collision case via constraint retry in the service layer. This deliberately mirrors the style of the app-side `gen_short_id` pattern used by `po_box.box_id` (timestamp + counter), but DB-side so it can be a column default.

### 2.2 `lookup_value` (NEW)

```sql
CREATE TABLE lookup_value (
  lookup_id        text PRIMARY KEY DEFAULT gen_short_id(),
  lookup_type      text NOT NULL CHECK (lookup_type IN (
                     'SUPPLIER_TYPE','FIRM_STATUS','BUSINESS_TYPE','MSME_TYPE',
                     'CATEGORY_CODE','ACCOUNT_TYPE','KYC_STATUS','DOC_STATUS',
                     'PERFORMANCE_BAND','LOCAL_OS','STATE_CODE')),
  code             text NOT NULL,
  label            text NOT NULL,
  parent_lookup_id text REFERENCES lookup_value(lookup_id),
  display_order    int DEFAULT 0,
  is_active        bool DEFAULT true,
  effective_from   date DEFAULT current_date,
  effective_to     date,
  created_at       timestamptz DEFAULT now(),
  UNIQUE (lookup_type, code)
);
CREATE INDEX idx_lookup_type_active ON lookup_value(lookup_type) WHERE is_active = true;

-- Code immutability trigger (custom SQLSTATE; see §5.3)
CREATE OR REPLACE FUNCTION lookup_code_immutable() RETURNS trigger AS $$
BEGIN
  IF NEW.code IS DISTINCT FROM OLD.code THEN
    RAISE EXCEPTION USING
      ERRCODE = 'P0001',
      MESSAGE = 'lookup_value.code is immutable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_lookup_code_immutable
  BEFORE UPDATE ON lookup_value
  FOR EACH ROW EXECUTE FUNCTION lookup_code_immutable();
```

`parent_lookup_id` is **kept as a forward-looking nullable column** for cascading dropdowns in future modules, but Module 2's GraphQL schema does **not** expose `parent`/`children` fields and there are **no** DataLoaders for cascade traversal. None of the 11 lookup types listed have natural parent/child relationships today.

#### Seed (60 rows total)

Inserted as part of Module 2's deployment migration. Codes are stable identifiers; labels are display strings. **STATE_CODE** (36 rows = 28 states + 8 UTs) is included here — `customer_master.billing_state_id` FKs to it.

```sql
INSERT INTO lookup_value (lookup_type, code, label, display_order) VALUES
  -- SUPPLIER_TYPE (5)
  ('SUPPLIER_TYPE','IMPORTER','Importer',1),
  ('SUPPLIER_TYPE','CONTRACTOR','Contractor',2),
  ('SUPPLIER_TYPE','NORMAL','Normal Supplier',3),
  ('SUPPLIER_TYPE','TRANSPORTER','Transporter',4),
  ('SUPPLIER_TYPE','OTHERS','Others',99),
  -- FIRM_STATUS (9)
  ('FIRM_STATUS','HUF','HUF',1),
  ('FIRM_STATUS','INC_CO','Inc Co',2),
  ('FIRM_STATUS','LLP','LLP',3),
  ('FIRM_STATUS','PARTNERSHIP','Partnership',4),
  ('FIRM_STATUS','PUBLIC_LTD','Public Ltd',5),
  ('FIRM_STATUS','PSU','PSU',6),
  ('FIRM_STATUS','PVT_LTD','Pvt Ltd',7),
  ('FIRM_STATUS','SOLE_PROP','Sole Prop',8),
  ('FIRM_STATUS','OTHERS','Others',99),
  -- BUSINESS_TYPE (18)
  ('BUSINESS_TYPE','MANUFACTURER','Manufacturer',1),
  ('BUSINESS_TYPE','IMPORTER','Importer',2),
  ('BUSINESS_TYPE','EXPORTER','Exporter',3),
  ('BUSINESS_TYPE','DISTRIBUTOR','Distributor',4),
  ('BUSINESS_TYPE','WHOLESALER','Wholesaler',5),
  ('BUSINESS_TYPE','STOCKIST','Stockist',6),
  ('BUSINESS_TYPE','DEALER','Dealer',7),
  ('BUSINESS_TYPE','RETAILER','Retailer',8),
  ('BUSINESS_TYPE','TRADER','Trader',9),
  ('BUSINESS_TYPE','BROKER_AGENT','Broker/Agent',10),
  ('BUSINESS_TYPE','CONTRACTOR','Contractor',11),
  ('BUSINESS_TYPE','SUB_CONTRACTOR','Sub-Contractor',12),
  ('BUSINESS_TYPE','JOB_WORKER','Job Worker',13),
  ('BUSINESS_TYPE','SERVICE_PROVIDER','Service Provider',14),
  ('BUSINESS_TYPE','CONSULTANT','Consultant',15),
  ('BUSINESS_TYPE','FRANCHISE','Franchise',16),
  ('BUSINESS_TYPE','ECOM_SELLER','E-com Seller',17),
  ('BUSINESS_TYPE','OTHERS','Others',99),
  -- CATEGORY_CODE (8)
  ('CATEGORY_CODE','RM','Raw Material',1),
  ('CATEGORY_CODE','PKG','Packaging Material',2),
  ('CATEGORY_CODE','CAPITAL','Capital',3),
  ('CATEGORY_CODE','CONSTRUCTION','Construction',4),
  ('CATEGORY_CODE','CONSUMABLES','Consumables',5),
  ('CATEGORY_CODE','SERVICES','Services',6),
  ('CATEGORY_CODE','IMPORT','Import',7),
  ('CATEGORY_CODE','OTHERS','Others',99),
  -- MSME_TYPE (4)
  ('MSME_TYPE','MICRO','Micro',1),
  ('MSME_TYPE','SMALL','Small',2),
  ('MSME_TYPE','MEDIUM','Medium',3),
  ('MSME_TYPE','NA','Not Available',99),
  -- LOCAL_OS (2)
  ('LOCAL_OS','LOCAL','Local',1),
  ('LOCAL_OS','OUT_STATION','Out-Station',2),
  -- ACCOUNT_TYPE (4)
  ('ACCOUNT_TYPE','SAVINGS','Savings',1),
  ('ACCOUNT_TYPE','CURRENT','Current',2),
  ('ACCOUNT_TYPE','CC','CC',3),
  ('ACCOUNT_TYPE','CBS','CBS',4),
  -- KYC_STATUS (3)
  ('KYC_STATUS','RECEIVED','Received',1),
  ('KYC_STATUS','PENDING','Pending',2),
  ('KYC_STATUS','NOT_REQUIRED','Not Required',3),
  -- DOC_STATUS (3)
  ('DOC_STATUS','COMPLETE','Complete',1),
  ('DOC_STATUS','INCOMPLETE','Incomplete',2),
  ('DOC_STATUS','PENDING','Pending',3),
  -- PERFORMANCE_BAND (5)
  ('PERFORMANCE_BAND','EXCELLENT','Excellent',1),
  ('PERFORMANCE_BAND','VERY_GOOD','Very Good',2),
  ('PERFORMANCE_BAND','GOOD','Good',3),
  ('PERFORMANCE_BAND','AVERAGE','Average',4),
  ('PERFORMANCE_BAND','POOR','Poor',5);
-- STATE_CODE seeds (36 rows): see deployment script in §13
```

### 2.3 `all_sku` (EXISTS — DO NOT recreate)

Module 2 changes only:

```sql
ALTER TABLE all_sku ADD COLUMN IF NOT EXISTS is_active   bool        DEFAULT true;
ALTER TABLE all_sku ADD COLUMN IF NOT EXISTS updated_at  timestamptz DEFAULT now();
ALTER TABLE all_sku ADD COLUMN IF NOT EXISTS updated_by  text;

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_all_sku_particulars_trgm
  ON all_sku USING gin (particulars gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_all_sku_item_type
  ON all_sku(item_type) WHERE is_active = true;
```

The existing `gst` column name is preserved. GraphQL exposes it as `SKU.gstRate` via a field resolver (see §5.5).

### 2.4 `customer_master` + `customer_ship_to` (NEW)

```sql
CREATE TABLE customer_master (
  customer_id           text PRIMARY KEY DEFAULT gen_short_id(),
  customer_code         text UNIQUE NOT NULL,
  legal_name            text NOT NULL,
  common_name           text NOT NULL,
  gstin                 text CHECK (gstin IS NULL OR length(gstin) = 15),
  pan                   text CHECK (pan   IS NULL OR length(pan)   = 10),
  billing_line1         text,
  billing_line2         text,
  billing_city          text,
  billing_state_id      text REFERENCES lookup_value(lookup_id),
  billing_pincode       text CHECK (billing_pincode IS NULL OR length(billing_pincode) = 6),
  primary_contact_name  text,
  primary_contact_phone text,
  primary_contact_email text,
  credit_limit_inr      numeric(15,2) DEFAULT 0,
  credit_days_net       int DEFAULT 0 CHECK (credit_days_net BETWEEN 0 AND 180),
  payment_terms         text,
  entity_scope          text NOT NULL DEFAULT 'both'
                          CHECK (entity_scope IN ('cfpl','cdpl','both')),
  is_active             bool DEFAULT true,
  created_at            timestamptz DEFAULT now(),
  created_by            text,
  updated_at            timestamptz DEFAULT now(),
  updated_by            text
);
CREATE INDEX        idx_customer_common_name_trgm ON customer_master USING gin (common_name gin_trgm_ops);
CREATE INDEX        idx_customer_legal_name_trgm  ON customer_master USING gin (legal_name  gin_trgm_ops);
CREATE INDEX        idx_customer_active           ON customer_master(is_active) WHERE is_active = true;
CREATE UNIQUE INDEX idx_customer_gstin            ON customer_master(gstin)     WHERE gstin IS NOT NULL;

CREATE TABLE customer_ship_to (
  ship_to_id    text PRIMARY KEY DEFAULT gen_short_id(),
  customer_id   text NOT NULL REFERENCES customer_master(customer_id) ON DELETE CASCADE,
  label         text NOT NULL,
  contact_name  text,
  contact_phone text,
  line1         text NOT NULL,
  line2         text,
  city          text,
  state_id      text REFERENCES lookup_value(lookup_id),
  pincode       text CHECK (pincode IS NULL OR length(pincode) = 6),
  gstin         text CHECK (gstin   IS NULL OR length(gstin)   = 15),
  is_default    bool DEFAULT false,
  is_active     bool DEFAULT true,
  created_at    timestamptz DEFAULT now()
);
CREATE INDEX        idx_ship_to_customer ON customer_ship_to(customer_id) WHERE is_active = true;
CREATE UNIQUE INDEX idx_ship_to_default  ON customer_ship_to(customer_id) WHERE is_default = true;
```

### 2.5 `qc_parameter_master` (NEW)

```sql
CREATE TABLE qc_parameter_master (
  param_id        text PRIMARY KEY,                  -- e.g. 'MOIST', 'AFLA_B1'; immutable
  param_name      text NOT NULL,
  param_group     text NOT NULL CHECK (param_group IN
                    ('PHYSICAL','CHEMICAL','MICROBIOLOGICAL','ORGANOLEPTIC','DOCUMENTATION')),
  data_type       text NOT NULL CHECK (data_type IN ('NUMERIC','BOOLEAN','ENUM','TEXT')),
  uom             text,
  is_food_safety  bool DEFAULT false,
  default_method  text,
  is_active       bool DEFAULT true,
  created_at      timestamptz DEFAULT now(),
  created_by      text,
  updated_at      timestamptz DEFAULT now(),
  updated_by      text,
  CHECK (param_id ~ '^[A-Z][A-Z0-9_]{2,30}$')
);
CREATE INDEX idx_qc_param_group  ON qc_parameter_master(param_group) WHERE is_active = true;

CREATE OR REPLACE FUNCTION qc_param_id_immutable() RETURNS trigger AS $$
BEGIN
  IF NEW.param_id IS DISTINCT FROM OLD.param_id THEN
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'qc_parameter_master.param_id is immutable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_qc_param_id_immutable
  BEFORE UPDATE ON qc_parameter_master
  FOR EACH ROW EXECUTE FUNCTION qc_param_id_immutable();
```

### 2.6 Customer backfill (post-create migration)

Reduces 7 free-text customer references across the codebase to FKs. Free-text columns are kept read-only for 2 weeks, then dropped via a follow-up migration after Modules 6/7 have been verified.

```sql
BEGIN;

CREATE TEMP TABLE _customer_seed (name text);
INSERT INTO _customer_seed
  SELECT DISTINCT trim(customer_name)         FROM so_header             WHERE customer_name        IS NOT NULL AND trim(customer_name) <> ''
  UNION SELECT DISTINCT trim(common_customer_name) FROM so_header        WHERE common_customer_name IS NOT NULL AND trim(common_customer_name) <> ''
  UNION SELECT DISTINCT trim(customer_party_name)  FROM po_header        WHERE customer_party_name  IS NOT NULL AND trim(customer_party_name) <> ''
  UNION SELECT DISTINCT trim(customer_name)         FROM bom_header             WHERE customer_name IS NOT NULL AND trim(customer_name) <> ''
  UNION SELECT DISTINCT trim(customer_name)         FROM production_plan_line   WHERE customer_name IS NOT NULL AND trim(customer_name) <> ''
  UNION SELECT DISTINCT trim(customer_name)         FROM production_order       WHERE customer_name IS NOT NULL AND trim(customer_name) <> ''
  UNION SELECT DISTINCT trim(customer_name)         FROM qc_inspection          WHERE customer_name IS NOT NULL AND trim(customer_name) <> ''
  UNION SELECT DISTINCT trim(blocked_for_customer)  FROM lot_block              WHERE blocked_for_customer IS NOT NULL AND trim(blocked_for_customer) <> '';

INSERT INTO customer_master (customer_code, legal_name, common_name, entity_scope, is_active)
SELECT 'CUST/' || lpad((row_number() OVER (ORDER BY name))::text, 4, '0'),
       name, name, 'both', true
FROM _customer_seed;

ALTER TABLE so_header             ADD COLUMN IF NOT EXISTS customer_id text REFERENCES customer_master(customer_id);
ALTER TABLE po_header             ADD COLUMN IF NOT EXISTS customer_id text REFERENCES customer_master(customer_id);
ALTER TABLE bom_header            ADD COLUMN IF NOT EXISTS customer_id text REFERENCES customer_master(customer_id);
ALTER TABLE production_plan_line  ADD COLUMN IF NOT EXISTS customer_id text REFERENCES customer_master(customer_id);
ALTER TABLE production_order      ADD COLUMN IF NOT EXISTS customer_id text REFERENCES customer_master(customer_id);
ALTER TABLE qc_inspection         ADD COLUMN IF NOT EXISTS customer_id text REFERENCES customer_master(customer_id);
ALTER TABLE lot_block             ADD COLUMN IF NOT EXISTS customer_id text REFERENCES customer_master(customer_id);

UPDATE so_header s             SET customer_id = c.customer_id FROM customer_master c WHERE lower(trim(s.customer_name))         = lower(c.legal_name) AND s.customer_id  IS NULL;
UPDATE po_header p             SET customer_id = c.customer_id FROM customer_master c WHERE lower(trim(p.customer_party_name))   = lower(c.legal_name) AND p.customer_id  IS NULL;
UPDATE bom_header b            SET customer_id = c.customer_id FROM customer_master c WHERE lower(trim(b.customer_name))         = lower(c.legal_name) AND b.customer_id  IS NULL;
UPDATE production_plan_line pl SET customer_id = c.customer_id FROM customer_master c WHERE lower(trim(pl.customer_name))        = lower(c.legal_name) AND pl.customer_id IS NULL;
UPDATE production_order po     SET customer_id = c.customer_id FROM customer_master c WHERE lower(trim(po.customer_name))        = lower(c.legal_name) AND po.customer_id IS NULL;
UPDATE qc_inspection q         SET customer_id = c.customer_id FROM customer_master c WHERE lower(trim(q.customer_name))         = lower(c.legal_name) AND q.customer_id  IS NULL;
UPDATE lot_block l             SET customer_id = c.customer_id FROM customer_master c WHERE lower(trim(l.blocked_for_customer))  = lower(c.legal_name) AND l.customer_id  IS NULL;

-- Diagnostic: rows that did not match (expected near-zero; investigate residuals manually)
SELECT 'so_header'             AS tbl, count(*) AS unmatched FROM so_header             WHERE customer_name        IS NOT NULL AND customer_id IS NULL
UNION ALL SELECT 'po_header',             count(*) FROM po_header             WHERE customer_party_name  IS NOT NULL AND customer_id IS NULL
UNION ALL SELECT 'bom_header',            count(*) FROM bom_header            WHERE customer_name        IS NOT NULL AND customer_id IS NULL
UNION ALL SELECT 'production_plan_line',  count(*) FROM production_plan_line  WHERE customer_name        IS NOT NULL AND customer_id IS NULL
UNION ALL SELECT 'production_order',      count(*) FROM production_order      WHERE customer_name        IS NOT NULL AND customer_id IS NULL
UNION ALL SELECT 'qc_inspection',         count(*) FROM qc_inspection         WHERE customer_name        IS NOT NULL AND customer_id IS NULL
UNION ALL SELECT 'lot_block',             count(*) FROM lot_block             WHERE blocked_for_customer IS NOT NULL AND customer_id IS NULL;

COMMIT;
```

Acceptance threshold: < 5 % of source rows unmatched. Residuals get manually merged in Module 2.5.

## 3. GraphQL schema (SDL)

Single SDL file `app/modules/master_data/schema.graphql`. Loaded via Strawberry. Combined with future module schemas in `app/schema.py` via `extend type Query` / `extend type Mutation`.

```graphql
# ---------- Common ----------
scalar Decimal
scalar DateTime
scalar Date

interface Node { id: ID! }

interface AuditedEntity {
  createdAt: DateTime!
  updatedAt: DateTime
  createdBy: String     # actor id; resolves to User in Phase 2 once Module 1 lands
  updatedBy: String
}

input PageInput { first: Int = 50  after: String }
type PageInfo  { hasNextPage: Boolean!  endCursor: String }
input DateRangeInput { from: Date!  to: Date! }

# ---------- Lookup ----------
type LookupValue implements Node {
  id: ID!
  lookupType: LookupType!
  code: String!
  label: String!
  displayOrder: Int!
  isActive: Boolean!
  effectiveFrom: Date
  effectiveTo: Date
}
enum LookupType {
  SUPPLIER_TYPE FIRM_STATUS BUSINESS_TYPE MSME_TYPE CATEGORY_CODE
  ACCOUNT_TYPE KYC_STATUS DOC_STATUS PERFORMANCE_BAND LOCAL_OS STATE_CODE
}
input CreateLookupInput {
  lookupType: LookupType!
  code: String!
  label: String!
  displayOrder: Int = 0
}

# ---------- SKU (read-only) ----------
type SKU implements Node {
  id: ID!
  skuId: Int!
  particulars: String!
  itemType: String        # free text in DB; not enum-constrained today
  itemGroup: String
  subGroup: String
  uom: String
  saleGroup: String
  gstRate: Decimal!       # maps to all_sku.gst column
  batchStrategy: BatchStrategy!
  minShelfLifeDays: Int!
  isActive: Boolean!
}
enum BatchStrategy { FIFO FEFO }
input SKUFilter {
  itemType: String
  itemGroup: String
  search: String
  isActive: Boolean = true
}
type SKUConnection { edges: [SKUEdge!]! pageInfo: PageInfo! }
type SKUEdge { cursor: String! node: SKU! }

# ---------- Customer ----------
type Customer implements Node & AuditedEntity {
  id: ID!
  customerCode: String!
  legalName: String!
  commonName: String!
  gstin: String
  pan: String
  billingAddress: Address!
  primaryContact: Contact!
  creditLimitInr: Decimal!
  creditDaysNet: Int!
  paymentTerms: String
  entityScope: EntityScope!
  isActive: Boolean!
  shipToAddresses: [ShipToAddress!]!
  defaultShipTo: ShipToAddress
  createdAt: DateTime!
  updatedAt: DateTime
  createdBy: String
  updatedBy: String
}
type Address {
  line1: String
  line2: String
  city: String
  state: LookupValue
  pincode: String
}
type Contact { name: String  phone: String  email: String }
type ShipToAddress implements Node {
  id: ID!
  customer: Customer!
  label: String!
  contactName: String
  contactPhone: String
  line1: String!
  line2: String
  city: String
  state: LookupValue
  pincode: String
  gstin: String
  isDefault: Boolean!
  isActive: Boolean!
}
enum EntityScope { CFPL CDPL BOTH }
type CustomerConnection { edges: [CustomerEdge!]! pageInfo: PageInfo! }
type CustomerEdge       { cursor: String! node: Customer! }

input CustomerFilter {
  search: String
  entityScope: EntityScope
  isActive: Boolean = true
}
input AddressInput { line1: String  line2: String  city: String  stateId: ID  pincode: String }
input ContactInput { name: String   phone: String  email: String }
input CreateCustomerInput {
  customerCode: String!
  legalName: String!
  commonName: String!
  gstin: String
  pan: String
  billingAddress: AddressInput!
  primaryContact: ContactInput!
  creditLimitInr: Decimal = 0
  creditDaysNet: Int = 0
  paymentTerms: String
  entityScope: EntityScope = BOTH
  initialShipTo: [CreateShipToInput!]
}
input UpdateCustomerInput {
  legalName: String
  commonName: String
  gstin: String
  pan: String
  billingAddress: AddressInput
  primaryContact: ContactInput
  creditLimitInr: Decimal
  creditDaysNet: Int
  paymentTerms: String
  entityScope: EntityScope
  isActive: Boolean
}
input CreateShipToInput {
  label: String!
  contactName: String
  contactPhone: String
  line1: String!
  line2: String
  city: String
  stateId: ID
  pincode: String
  gstin: String
  isDefault: Boolean = false
}

# ---------- QC parameter ----------
type QCParameter {
  paramId: ID!
  paramName: String!
  paramGroup: QCParamGroup!
  dataType: QCParamDataType!
  uom: String
  isFoodSafety: Boolean!
  defaultMethod: String
  isActive: Boolean!
}
enum QCParamGroup    { PHYSICAL CHEMICAL MICROBIOLOGICAL ORGANOLEPTIC DOCUMENTATION }
enum QCParamDataType { NUMERIC BOOLEAN ENUM TEXT }
input CreateQCParamInput {
  paramId: ID!
  paramName: String!
  paramGroup: QCParamGroup!
  dataType: QCParamDataType!
  uom: String
  isFoodSafety: Boolean = false
  defaultMethod: String
}

# ---------- Roots ----------
extend type Query {
  # Lookup
  lookups(type: LookupType!, activeOnly: Boolean = true): [LookupValue!]!
  lookup(id: ID!): LookupValue
  lookupByCode(type: LookupType!, code: String!): LookupValue

  # SKU (read-only)
  sku(id: Int!): SKU
  skus(filter: SKUFilter, page: PageInput): SKUConnection!
  searchSKU(q: String!, limit: Int = 10): [SKU!]!

  # Customer
  customer(id: ID!): Customer
  customerByCode(code: String!): Customer
  customers(filter: CustomerFilter, page: PageInput): CustomerConnection!
  searchCustomersByName(q: String!, limit: Int = 10): [Customer!]!
  shipToAddresses(customerId: ID!): [ShipToAddress!]!

  # QC parameters
  qcParameters(activeOnly: Boolean = true): [QCParameter!]!
  qcParameter(paramId: ID!): QCParameter
}

extend type Mutation {
  # Lookup
  createLookup(input: CreateLookupInput!): LookupValue! @auth(requires: ["IT","ADMIN"])
  updateLookupLabel(id: ID!, label: String!): LookupValue! @auth(requires: ["IT","ADMIN"])
  deactivateLookup(id: ID!): LookupValue! @auth(requires: ["IT","ADMIN"])

  # Customer
  createCustomer(input: CreateCustomerInput!): Customer! @auth(requires: ["PURCHASE","SCM","ADMIN"])
  updateCustomer(id: ID!, input: UpdateCustomerInput!): Customer! @auth(requires: ["PURCHASE","SCM","ADMIN"])
  deactivateCustomer(id: ID!, reason: String!): Customer! @auth(requires: ["SCM","ADMIN"])
  addShipToAddress(customerId: ID!, input: CreateShipToInput!): ShipToAddress! @auth(requires: ["PURCHASE","SCM","ADMIN"])
  updateShipToAddress(id: ID!, input: CreateShipToInput!): ShipToAddress!     @auth(requires: ["PURCHASE","SCM","ADMIN"])
  setDefaultShipTo(id: ID!): ShipToAddress!                                    @auth(requires: ["PURCHASE","SCM","ADMIN"])
  deactivateShipToAddress(id: ID!): ShipToAddress!                             @auth(requires: ["PURCHASE","SCM","ADMIN"])

  # QC parameter
  createQCParameter(input: CreateQCParamInput!): QCParameter! @auth(requires: ["FSTL","ADMIN"])
  updateQCParameterStatus(paramId: ID!, isActive: Boolean!): QCParameter! @auth(requires: ["FSTL","ADMIN"])
}

directive @auth(requires: [String!]!) on FIELD_DEFINITION
```

**Removed from user's original spec:** `createSKU`, `updateSKU` (Decision 3a — Excel is the sole writer), `LookupValue.parent` and `LookupValue.children` (no cascading dropdowns in scope), `Customer.openSoCount` and `Customer.totalRevenueInr` (Decision 4 — no cross-module SO queries; revisit during Module 11 reports), `SKUItemType` enum (existing `all_sku.item_type` is free text; constraining via enum requires data cleanup that is out of scope).

## 4. Project layout

```
app/modules/master_data/
├── __init__.py
├── schema.graphql              # the SDL above
├── types.py                    # Strawberry types
├── inputs.py                   # Strawberry inputs
├── enums.py                    # GraphQL enums
├── models.py                   # SQLAlchemy models
├── dataloaders.py              # all DataLoaders
├── resolvers/
│   ├── __init__.py
│   ├── lookup.py
│   ├── sku.py
│   ├── customer.py
│   └── qc_parameter.py
├── services/                   # business logic, no GraphQL imports
│   ├── __init__.py
│   ├── lookup_service.py
│   ├── sku_service.py
│   ├── customer_service.py
│   └── qc_param_service.py
├── validators.py               # gstin, pan, pincode, code regexes
└── tests/
    ├── unit/
    ├── integration/
    └── permissions/

app/core/
├── audit_context.py            # SET LOCAL session-var helper
├── auth_directive.py           # @auth no-op directive (Decision 2a)
├── dataloader_factory.py       # per-request DataLoader wiring
├── graphql_app.py              # Strawberry FastAPI integration
└── sqlalchemy.py               # async engine + session factory sharing the asyncpg pool

app/schema.py                   # combines all module schemas
```

Strawberry router mount in `app/main.py`:

```python
from app.core.graphql_app import graphql_router
app.include_router(graphql_router, prefix="/graphql")
```

No REST routes added or removed in `app/main.py`. The existing `/api/v1/*`, `/webhooks/*`, `/ws/*`, `/internal/events`, `/health` keep working.

## 5. Resolver details

### 5.1 `lookups(type, activeOnly)`

```python
# resolvers/lookup.py
@strawberry.field
async def lookups(
    self,
    info: Info,
    type: LookupType,
    active_only: bool = True,
) -> list[LookupValue]:
    return await lookup_service.list_by_type(
        info.context["db"], type.value, active_only
    )
```

No cache. Lookups table is small (~60 rows); a primary-key indexed query is sub-millisecond.

### 5.2 `createLookup`

```python
async def create_lookup(db, actor, dto: CreateLookupDTO) -> LookupValue:
    if not LOOKUP_CODE_REGEX.match(dto.code):
        raise GraphQLError("code must match ^[A-Z][A-Z0-9_]{1,40}$",
                           extensions={"code": "VALIDATION"})
    row = LookupValue(
        lookup_type=dto.lookup_type, code=dto.code, label=dto.label,
        display_order=dto.display_order,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as e:
        if "lookup_value_lookup_type_code_key" in str(e.orig):
            raise GraphQLError("Duplicate code for type",
                               extensions={"code": "CONFLICT"})
        raise
    return row
```

### 5.3 `updateLookupLabel`

`code` is immutable. Mutation accepts `label` only. The DB trigger `trg_lookup_code_immutable` (§2.2) raises with custom SQLSTATE `P0001`. The error mapper in `core/error_mapping.py` translates `P0001` → `VALIDATION` (not `FORBIDDEN` — this is a domain rule, not auth).

### 5.4 `deactivateLookup`

Soft delete only — sets `is_active = false`. No FK precondition check; FKs from Modules 3+ to `lookup_value` reference `lookup_id`, not `is_active`, so deactivation never violates a constraint. Consuming services filter on `is_active = true` themselves.

### 5.5 `sku(id)` and `skus(filter, page)`

Cursor pagination using `(created_at desc, sku_id desc)` composite cursor:

```python
async def skus(db, filter: SKUFilterDTO, first: int, after: str | None) -> SKUConnection:
    q = select(SKU).where(SKU.is_active == filter.is_active)
    if filter.item_type:
        q = q.where(SKU.item_type == filter.item_type)
    if filter.item_group:
        q = q.where(SKU.item_group == filter.item_group)
    if filter.search:
        q = q.where(SKU.particulars.op("%")(filter.search))                  # pg_trgm operator
        q = q.order_by(func.similarity(SKU.particulars, filter.search).desc(), SKU.sku_id.desc())
    else:
        q = q.order_by(SKU.created_at.desc(), SKU.sku_id.desc())
    if after:
        cursor = decode_cursor(after)
        q = q.where(or_(
            SKU.created_at < cursor.ts,
            and_(SKU.created_at == cursor.ts, SKU.sku_id < cursor.id),
        ))
    rows = (await db.execute(q.limit(first + 1))).scalars().all()
    has_next = len(rows) > first
    rows = rows[:first]
    return SKUConnection(
        edges=[SKUEdge(cursor=encode_cursor(r), node=r) for r in rows],
        page_info=PageInfo(has_next_page=has_next,
                           end_cursor=encode_cursor(rows[-1]) if rows else None),
    )
```

`searchSKU(q)` uses GIN trigram with `op("%")` and `similarity()` ordering. No pagination, hard limit 10.

`gstRate` field resolver maps DB `gst` to GraphQL `gstRate`:

```python
@strawberry.field
def gst_rate(self) -> Decimal:
    return self.gst
```

### 5.6 (intentionally vacated)

`createSKU` and `updateSKU` are **not** exposed (Decision 3a). SKU writes happen exclusively through the existing `master_ingest.py` Excel pipeline. If a stray SKU needs editing, do it via direct SQL during a maintenance window — Module 2.5 may revisit.

### 5.7 `customer(id)`, `customerByCode(code)`, `customers(filter, page)`

Per-request DataLoaders (no cross-request caching):

```python
class CustomerLoaders:
    by_id: DataLoader[str, Customer]
    by_code: DataLoader[str, Customer]
    ship_tos_by_customer:           DataLoader[str, list[ShipToAddress]]
    default_ship_to_by_customer:    DataLoader[str, ShipToAddress | None]
    state_lookup:                   DataLoader[str, LookupValue]
```

Field resolvers:

```python
@strawberry.field
async def ship_to_addresses(self, info: Info) -> list[ShipToAddress]:
    return await info.context["loaders"].ship_tos_by_customer.load(self.id)

@strawberry.field
async def default_ship_to(self, info: Info) -> ShipToAddress | None:
    return await info.context["loaders"].default_ship_to_by_customer.load(self.id)

@strawberry.field
async def billing_address(self, info: Info) -> Address:
    state = None
    if self.billing_state_id:
        state = await info.context["loaders"].state_lookup.load(self.billing_state_id)
    return Address(
        line1=self.billing_line1, line2=self.billing_line2,
        city=self.billing_city, state=state, pincode=self.billing_pincode,
    )
```

Removed: `Customer.openSoCount`, `Customer.totalRevenueInr` (Decision 4 — no cross-module SO queries).

### 5.8 `createCustomer`

```python
async def create_customer(db, actor, dto: CreateCustomerDTO) -> Customer:
    if dto.gstin and not GSTIN_REGEX.match(dto.gstin):
        raise GraphQLError("Invalid GSTIN format", extensions={"code": "VALIDATION"})
    if dto.pan and not PAN_REGEX.match(dto.pan):
        raise GraphQLError("Invalid PAN format", extensions={"code": "VALIDATION"})
    if not CUSTOMER_CODE_REGEX.match(dto.customer_code):
        raise GraphQLError("customer_code must match CUST/NNNN",
                           extensions={"code": "VALIDATION"})
    if dto.billing_address.pincode and not PINCODE_REGEX.match(dto.billing_address.pincode):
        raise GraphQLError("Invalid pincode", extensions={"code": "VALIDATION"})
    if dto.primary_contact.email and not EMAIL_REGEX.match(dto.primary_contact.email):
        raise GraphQLError("Invalid email", extensions={"code": "VALIDATION"})

    async with db.begin_nested():                # SAVEPOINT inside the request transaction
        customer = Customer(
            customer_code=dto.customer_code,
            legal_name=dto.legal_name, common_name=dto.common_name,
            gstin=dto.gstin, pan=dto.pan,
            billing_line1=dto.billing_address.line1,
            billing_line2=dto.billing_address.line2,
            billing_city=dto.billing_address.city,
            billing_state_id=dto.billing_address.state_id,
            billing_pincode=dto.billing_address.pincode,
            primary_contact_name=dto.primary_contact.name,
            primary_contact_phone=dto.primary_contact.phone,
            primary_contact_email=dto.primary_contact.email,
            credit_limit_inr=dto.credit_limit_inr,
            credit_days_net=dto.credit_days_net,
            payment_terms=dto.payment_terms,
            entity_scope=dto.entity_scope.value.lower(),
            created_by=actor.id,
            updated_by=actor.id,
        )
        db.add(customer)
        try:
            await db.flush()
        except IntegrityError as e:
            msg = str(e.orig)
            if "customer_master_customer_code_key" in msg:
                raise GraphQLError("Duplicate customer_code", extensions={"code": "CONFLICT"})
            if "idx_customer_gstin" in msg:
                raise GraphQLError("Duplicate GSTIN",         extensions={"code": "CONFLICT"})
            raise

        if dto.initial_ship_to:
            default_count = sum(1 for s in dto.initial_ship_to if s.is_default)
            if default_count > 1:
                raise GraphQLError("Only one ship-to can be default",
                                   extensions={"code": "VALIDATION"})
            for s in dto.initial_ship_to:
                db.add(ShipToAddress(customer_id=customer.customer_id, **s.dict()))

    return customer
```

### 5.9 `updateCustomer`

Partial update; each input field optional. If `gstin` changes, re-validate format and uniqueness. Sets `updated_at = now()`, `updated_by = actor.id`. The audit trigger captures the old/new field-level diff (Module 10 will surface this).

### 5.10 `deactivateCustomer`

```python
async def deactivate_customer(db, actor, customer_id: str, reason: str) -> Customer:
    customer = await db.get(Customer, customer_id)
    if not customer:
        raise GraphQLError("Not found", extensions={"code": "NOT_FOUND"})
    customer.is_active = False
    customer.updated_by = actor.id
    customer.updated_at = func.now()
    # `reason` is captured by the audit trigger via the application_name session var
    return customer
```

**No open-SO precondition** (Decision 4). A deactivated customer can still appear in open SO/PO/BOM/production rows. Consuming UIs are responsible for displaying deactivated customers with a visual marker (typically grey) and blocking new references at form level.

### 5.11 `addShipToAddress`

If `is_default = true`, unset prior default in same transaction (the partial unique index would otherwise raise):

```python
async def add_ship_to(db, actor, customer_id: str, dto: CreateShipToDTO) -> ShipToAddress:
    async with db.begin_nested():
        if dto.is_default:
            await db.execute(
                update(ShipToAddress)
                .where(ShipToAddress.customer_id == customer_id)
                .where(ShipToAddress.is_default.is_(True))
                .values(is_default=False)
            )
        st = ShipToAddress(customer_id=customer_id, **dto.dict())
        db.add(st)
        await db.flush()
    return st
```

### 5.12 `setDefaultShipTo`

Same SAVEPOINT pattern. Locks the prior default row before flipping its flag (`SELECT ... FOR UPDATE`) so two concurrent calls cannot both succeed:

```python
async def set_default_ship_to(db, actor, ship_to_id: str) -> ShipToAddress:
    target = await db.get(ShipToAddress, ship_to_id, with_for_update=True)
    if not target:
        raise GraphQLError("Not found", extensions={"code": "NOT_FOUND"})
    async with db.begin_nested():
        await db.execute(
            update(ShipToAddress)
            .where(ShipToAddress.customer_id == target.customer_id)
            .where(ShipToAddress.is_default.is_(True))
            .values(is_default=False)
        )
        target.is_default = True
        await db.flush()
    return target
```

### 5.13 `qcParameters` / `createQCParameter` / `updateQCParameterStatus`

`param_id` regex enforced both in Python (`QC_PARAM_ID_REGEX = re.compile(r"^[A-Z][A-Z0-9_]{2,30}$")`) and at the DB CHECK level. Updates of `param_id` rejected by the immutability trigger (§2.5) with SQLSTATE `P0001` → mapped to `VALIDATION`.

## 6. Validators (`app/modules/master_data/validators.py`)

```python
import re
from decimal import Decimal

GSTIN_REGEX         = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][Z][0-9A-Z]$")
PAN_REGEX           = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PINCODE_REGEX       = re.compile(r"^[1-9][0-9]{5}$")
PHONE_E164          = re.compile(r"^\+?[1-9]\d{9,14}$")
EMAIL_REGEX         = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CUSTOMER_CODE_REGEX = re.compile(r"^CUST/\d{4,}$")
LOOKUP_CODE_REGEX   = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")
QC_PARAM_ID_REGEX   = re.compile(r"^[A-Z][A-Z0-9_]{2,30}$")

def validate_gst_rate(v: Decimal) -> None:
    if v < 0 or v > Decimal("1.0"):
        raise ValueError("gst_rate must be 0-1.0")
```

## 7. `@auth` directive — no-op stub (Decision 2a)

`app/core/auth_directive.py`:

```python
import strawberry
from strawberry.schema_directive import Location
from strawberry.permission import BasePermission
from strawberry.types import Info

@strawberry.schema_directive(locations=[Location.FIELD_DEFINITION])
class Auth:
    requires: list[str]

class AllowAll(BasePermission):
    """Decision 2a: no-op until Module 1 (Auth) lands. Always allows."""
    message = "unreachable"
    async def has_permission(self, source, info: Info, **kwargs) -> bool:
        return True
```

The directive is recorded in the SDL so clients see role requirements (documentation-only). Resolver dispatch ignores `requires` for now. When Module 1 lands, swap `AllowAll` for a real `RoleCheck` and wire `info.context["user"]` from the existing JWT middleware. No schema-level changes will be needed.

The audit context (§8) uses a hard-coded `system` actor:

```python
SYSTEM_ACTOR = SimpleNamespace(id="system", role=SimpleNamespace(code="SYSTEM"))
```

## 8. Audit context (per request)

`app/core/audit_context.py`:

```python
import uuid
from contextlib import asynccontextmanager
from sqlalchemy import text

@asynccontextmanager
async def audit_session(db, actor):
    """Set Postgres session vars before any write so audit triggers tag rows."""
    commit_id = str(uuid.uuid4())
    await db.execute(text("SET LOCAL app.commit_id     = :v"), {"v": commit_id})
    await db.execute(text("SET LOCAL app.actor_user_id = :v"), {"v": actor.id})
    await db.execute(text("SET LOCAL app.actor_role    = :v"), {"v": actor.role.code})
    yield commit_id
```

Every mutation wraps its DB work in `audit_session(db, SYSTEM_ACTOR)`. Module 10's audit triggers (added later) read these session vars to write `audit_log` rows. For Module 2 the triggers do not yet exist, but setting the session vars now is harmless and avoids retrofitting every resolver later.

## 9. Permission matrix (documentation only)

| Mutation | Roles allowed when Module 1 lands |
|---|---|
| `createLookup`, `updateLookupLabel`, `deactivateLookup` | IT, ADMIN |
| `createCustomer`, `updateCustomer` | PURCHASE, SCM, ADMIN |
| `deactivateCustomer` | SCM, ADMIN |
| `addShipToAddress`, `updateShipToAddress`, `setDefaultShipTo`, `deactivateShipToAddress` | PURCHASE, SCM, ADMIN |
| `createQCParameter`, `updateQCParameterStatus` | FSTL, ADMIN |
| All Query fields | Any authenticated user |

Currently every mutation is open. Track this in `docs/security/auth-rollout.md` for Phase 2.

## 10. Test plan

### 10.1 Unit (mocked DB)

- Validator regex coverage: GSTIN with all 36 state prefixes, PAN edge cases, pincode, phone E.164, email
- Pagination cursor encode/decode round-trip
- `gstRate` ↔ `gst` column mapping correct
- Default ship-to invariant logic (only one true at a time)

### 10.2 Integration (Postgres testcontainer)

- Create lookup → unique constraint blocks duplicate (CONFLICT)
- Update lookup `code` → trigger rejects with `P0001` (mapped to VALIDATION)
- Deactivate lookup with FK references → soft-delete succeeds
- SKU trigram search returns ranked results; EXPLAIN confirms GIN index hit (no seq scan)
- Create customer with all 5 invalid fields → individual VALIDATION errors
- Backfill script idempotent (running twice produces same row count)
- Default ship-to: 2 concurrent `setDefaultShipTo` → only one wins (the other sees the FOR UPDATE lock)
- `deactivateCustomer` always succeeds regardless of references (Decision 4)

### 10.3 GraphQL execution (Strawberry test client)

- Each query and mutation invoked end-to-end
- Error response shape: `{ errors: [{ message, extensions: { code } }] }`
- DataLoader batching: query `customers(first:50){shipToAddresses{...}}` triggers 1 customer SQL + 1 ship_to SQL (not 51)
- `@auth` directive present in introspected SDL

### 10.4 Permission matrix (currently expects 200 OK on all)

For every mutation × every role: assert OK. When Module 1 lands, this test becomes the matrix in §9.

### 10.5 Audit-trigger smoke

For now, only verifies that `SET LOCAL app.commit_id` runs without error inside every mutation transaction (`SELECT current_setting('app.commit_id')` returns a UUID-shape value). Full audit-row assertions added in Module 10.

Coverage targets: 85 % services, 70 % resolvers, 100 % validators.

## 11. Code review checklist (mandatory per PR)

1. DataLoader used for every parent → child resolver (`Customer.shipToAddresses`, `Customer.defaultShipTo`, `Customer.billingAddress.state`, `ShipToAddress.state`)
2. All mutations wrapped in `audit_session()` — Postgres session vars set before any write
3. `@auth` directive present on every mutation; `requires` matches the matrix in §9 (currently no-op)
4. Pydantic input validation runs before any DB call (gstin, pan, pincode, phone, email, code regexes)
5. DB CHECK constraints duplicate the Pydantic validation (defense in depth)
6. `IntegrityError` mapped to specific `extensions.code` (`CONFLICT` for unique violation, `VALIDATION` for check violation)
7. No `select *` — explicit column lists or SQLAlchemy ORM
8. Cursor pagination on every list resolver; no offset/limit on tables > 1 000 rows
9. Decimal used for `gstRate`, `creditLimitInr`; no float anywhere
10. Trigram indexes verified via EXPLAIN in integration test (no seq scan on `searchCustomersByName` / `searchSKU`)
11. Tests cover unit + integration + GraphQL + permission + audit-trigger smoke
12. No raw SQL string interpolation; all queries parameterized
13. No business logic in resolvers — services package owns it; resolvers are glue
14. Default ship-to invariant test exists and passes under concurrency
15. Backfill script tested on a copy of production data, not just empty DB
16. No REST routes added for these four domains (CI grep gate, see §12)
17. Structured logging: every resolver logs `{commit_id, actor_id, request_id, resolver, duration_ms}`
18. Lookup `code` immutability tested at trigger level (DB-level, not just app)
19. `customer_code` regex enforced by both Pydantic and DB CHECK
20. GSTIN uniqueness enforced via partial unique index (NULL allowed multiple times)
21. DataLoader cache scoped to single request (no cross-request leakage — verify by hitting the same `Customer.id` from two parallel requests and observing two SQL fires)
22. p95 latency: query < 100 ms, mutation < 200 ms, search < 150 ms — measured in CI

## 12. REST stance

There are **no** existing REST routes for `lookup_value`, `customer_master`, `qc_parameter_master`, or read-only `all_sku` queries. Module 2 simply does not add any.

CI gate (run on every PR):

```bash
git grep -E '@(app|router)\.(get|post|put|delete|patch)' app/modules/master_data \
  && echo "ERROR: REST routes found in master_data module" && exit 1 \
  || echo "OK: master_data is GraphQL-only"
```

If any future contributor adds an `/api/lookups`, `/api/customers`, `/api/skus`, `/api/qc-parameters` route, the CI gate blocks the PR. Active enforcement, not just documentation.

## 13. Deployment sequence

1. Add `gen_short_id()` Postgres function (§2.1)
2. Create `lookup_value` + immutability trigger (§2.2)
3. Insert all 60 lookup seeds **including the 36 STATE_CODE rows** — `customer_master.billing_state_id` FKs to it
4. ALTER `all_sku` (add `is_active`, `updated_at`, `updated_by`, trigram index) (§2.3)
5. Create `customer_master` + `customer_ship_to` (§2.4)
6. Create `qc_parameter_master` + immutability trigger (§2.5)
7. Run customer backfill (§2.6); review unmatched diagnostic; manually merge < 5 % residuals
8. Deploy backend with Module 2 enabled; mount `/graphql` alongside existing REST
9. Smoke test via Strawberry's playground at `/graphql`: basic CRUD round-trip per entity
10. Monitor for 2 weeks; once Module 6 (PO) is live and consuming `customer_master`, drop free-text `customer_name` / `customer_party_name` / `common_customer_name` / `blocked_for_customer` columns via follow-up migration

## 14. Out of scope (deferred)

- `mergeCustomers(keepId, mergeIds)` — Module 2.5
- Customer compliance documents in S3 — not requested
- Customer banking — not requested
- SKU-to-customer code mapping — not requested
- Bulk import of customers via CSV — Module 2.5
- `Customer.openSoCount`, `Customer.totalRevenueInr` — Module 11 (Reports), once cross-module aggregation patterns are in place
- `createSKU`, `updateSKU` — Excel ingest is the source of truth (Decision 3a). Possibly revisited in Module 2.5
- `LookupValue.parent` / `LookupValue.children` — no cascading dropdowns in the 11 types listed; column kept as forward-compatible nullable

## 15. Acceptance criteria

- All GraphQL operations in §3 callable from Strawberry's playground at `POST /graphql`
- No REST endpoints for these four domains (CI grep gate passes)
- All 22 senior code-review items in §11 pass on PR
- Test coverage thresholds met (85 % services, 70 % resolvers, 100 % validators)
- Backfill script reduces unmatched rows to < 5 % of source rows
- p95 latency targets met under load test (1 000 RPS for queries, 100 RPS for mutations)
- `@auth` directive present in introspected SDL on every mutation, even though it currently no-ops
- Audit-context session vars (`app.commit_id`, `app.actor_user_id`, `app.actor_role`) set before every mutation flushes
