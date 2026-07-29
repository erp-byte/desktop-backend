-- 077_cr_email_routing.sql
-- Customer-Returns approval e-mail routing, moved OUT of hardcoded literals (the
-- legacy shared/email_notifier.py maps + their verbatim FE port) and INTO the DB.
--
-- Split of source of truth:
--   * business_head / sales_poc PEOPLE come from ROLE ASSIGNMENT, not this table:
--     GET /email-routing reads auth_user joined to the `business_head` / `sales`
--     roles (name + email). Manage them on the Admin screen (assign the role +
--     an email), not here.
--   * warehouse_cc / constant_cc / notify_to have NO user/role source, so they
--     live here as config rows:
--       kind        warehouse_cc | constant_cc | notify_to
--       match_type  substring -> canonicalized factory unit CONTAINS the key
--                   code      -> canonicalized unit, separators stripped, EQUALS key
--                   const     -> no key (constant_cc rows + the single notify_to)
--       sort_order  warehouse rows are evaluated in this order (cold substrings
--                   before codes, matching the legacy cold-then-code precedence).
--
-- Idempotent: CREATE IF NOT EXISTS + INSERT ... ON CONFLICT DO NOTHING, and a
-- cleanup DELETE of any person rows an earlier draft of this file seeded (the
-- endpoint now ignores those kinds — people are role-derived).

CREATE TABLE IF NOT EXISTS cr_email_routing (
    id          SERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,
    match_key   TEXT NOT NULL DEFAULT '',
    match_type  TEXT NOT NULL DEFAULT 'name',
    email       TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (kind, match_key, email)
);

-- Business-Head / Sales-POC dropdowns + the recipient matrix read these roles.
-- `business_head` is seeded by 035_sample_roles.sql (in the runner); `sales` is
-- normally applied out-of-band by samples/081_sales_role.sql — guarantee it here
-- too so a fresh migrate.py DB has the role the CR feature depends on. (Role only;
-- its sample-module permission grants stay 081's concern.)
INSERT INTO auth_role (role_name, description, is_admin) VALUES
    ('sales', 'Sales — raises NPD/sample requisitions on behalf of a business head', FALSE)
ON CONFLICT (role_name) DO NOTHING;

-- People are role-derived now — drop any person rows a prior draft seeded here.
DELETE FROM cr_email_routing WHERE kind IN ('business_head', 'sales_poc');

INSERT INTO cr_email_routing (kind, match_key, match_type, email, sort_order) VALUES
    -- Standing TO on every customer-return mail.
    ('notify_to', '', 'const', 'pooja.parkar@candorfoods.in', 0),

    -- Constant CCs on every customer-return mail (incl. one external address).
    ('constant_cc', '', 'const', 'sunil.jasoria@candorfoods.in', 1),
    ('constant_cc', '', 'const', 'b.hrithik@candorfoods.in',     2),
    ('constant_cc', '', 'const', 'billing@candorfoods.in',       3),
    ('constant_cc', '', 'const', 'satyendra@candorfoods.in',     4),
    ('constant_cc', '', 'const', 'sachin.more@candorfoods.in',   5),
    ('constant_cc', '', 'const', 'dipesh.sharma@ofbusiness.in',  6),
    ('constant_cc', '', 'const', 'yash@candorfoods.in',          7),

    -- Warehouse-owner CC (matched against the canonicalized factory unit).
    -- Cold storages (substring) are checked before the code matches.
    ('warehouse_cc', 'savla',   'substring', 'vaibhav.kumkar@candorfoods.in', 1),
    ('warehouse_cc', 'rishi',   'substring', 'vaibhav.kumkar@candorfoods.in', 2),
    ('warehouse_cc', 'supreme', 'substring', 'vaibhav.kumkar@candorfoods.in', 3),
    ('warehouse_cc', 'eskimo',  'substring', 'vaibhav.kumkar@candorfoods.in', 4),
    ('warehouse_cc', 'w202',    'code',      'vaibhav.kumkar@candorfoods.in', 5),
    ('warehouse_cc', 'a185',    'code',      'stores-a185@candorfoods.in',    6),
    ('warehouse_cc', 'a68',     'code',      'pankaj.ranga@candorfoods.in',   7)
ON CONFLICT (kind, match_key, email) DO NOTHING;
