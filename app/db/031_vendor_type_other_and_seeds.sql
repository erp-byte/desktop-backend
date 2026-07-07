-- =========================================================================
-- 031_vendor_type_other_and_seeds.sql
--
-- Makes the vendor onboarding classification dropdowns work end-to-end:
--
--   0. gen_short_id()          — PK generator for lookup_value (idempotent).
--   1. lookup_value            — the dropdown master the form reads via
--                                GET /api/v1/lookups?type=…  Created IF NOT
--                                EXISTS so this is a safe no-op where the
--                                table already exists. (The lookups endpoint
--                                returns [] on a missing table, which is why
--                                EVERY dropdown renders empty when it's absent.)
--   2. vendor_master.*_other   — free-text companion columns. Three of the
--                                dropdowns (Supplier Type, Firm Status,
--                                Business Type) carry an "Others" option; when
--                                the operator picks it the form shows a text
--                                box whose value lands in the matching column.
--                                (MSME Type has no "Others" — its tail is
--                                 "Not Available" — so it gets no column.)
--   3. seeds                    — the canonical dropdown options.
--
-- Fully self-contained and idempotent. Safe to re-run. Run against the DB
-- that backs the app (the one DATABASE_URL points at).
-- =========================================================================

-- ── 0. Short-id generator (lookup_value PK default) ───────────────────────
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


-- ── 1. lookup_value master (only created if the DB doesn't have it) ────────
CREATE TABLE IF NOT EXISTS lookup_value (
  lookup_id        text PRIMARY KEY DEFAULT gen_short_id(),
  lookup_type      text NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_lookup_type_active
  ON lookup_value(lookup_type) WHERE is_active = true;


-- ── 2. vendor_master free-text companion columns ──────────────────────────
ALTER TABLE vendor_master
    ADD COLUMN IF NOT EXISTS supplier_type_other  text,
    ADD COLUMN IF NOT EXISTS firm_status_other    text,
    ADD COLUMN IF NOT EXISTS business_type_other  text;


-- ── 3. Dropdown option seeds ──────────────────────────────────────────────
-- lookup_id is minted explicitly with gen_short_id() so this doesn't depend on
-- the column's DEFAULT (works even if a pre-existing lookup_value lacks one).
-- WHERE NOT EXISTS keeps it idempotent. The frontend reveals the free-text box
-- when the chosen option's label = 'Others'.
INSERT INTO lookup_value (lookup_id, lookup_type, code, label, display_order, is_active)
SELECT gen_short_id(), v.lookup_type, v.code, v.label, v.display_order, true
  FROM (VALUES
      -- Supplier Type
      ('SUPPLIER_TYPE', 'IMPORTER',          'Importer',                      1),
      ('SUPPLIER_TYPE', 'CONTRACTOR',        'Contractor',                    2),
      ('SUPPLIER_TYPE', 'NORMAL_SUPPLIER',   'Normal Supplier',               3),
      ('SUPPLIER_TYPE', 'TRANSPORTER',       'Transporter',                   4),
      ('SUPPLIER_TYPE', 'OTHERS',            'Others',                       99),

      -- Type of Company / Firm Status
      ('FIRM_STATUS',   'HUF',               'HUF',                           1),
      ('FIRM_STATUS',   'INCORPORATED_CO',   'Incorporated Co.',              2),
      ('FIRM_STATUS',   'LLP',               'Limited Liability Partnership', 3),
      ('FIRM_STATUS',   'PARTNERSHIP',       'Partnership',                   4),
      ('FIRM_STATUS',   'PUBLIC_LTD',        'Public Ltd',                    5),
      ('FIRM_STATUS',   'PSU',               'Public Sector Unit (PSU)',      6),
      ('FIRM_STATUS',   'PRIVATE_LTD',       'Private Ltd Company',           7),
      ('FIRM_STATUS',   'SOLE_PROPRIETARY',  'Sole Proprietary',              8),
      ('FIRM_STATUS',   'OTHERS',            'Others',                       99),

      -- Business Type
      ('BUSINESS_TYPE', 'MANUFACTURER',      'Manufacturer',                  1),
      ('BUSINESS_TYPE', 'IMPORTER',          'Importer',                      2),
      ('BUSINESS_TYPE', 'EXPORTER',          'Exporter',                      3),
      ('BUSINESS_TYPE', 'DISTRIBUTOR',       'Distributor',                   4),
      ('BUSINESS_TYPE', 'WHOLESALER',        'Wholesaler',                    5),
      ('BUSINESS_TYPE', 'STOCKIST',          'Stockist',                      6),
      ('BUSINESS_TYPE', 'DEALER',            'Dealer',                        7),
      ('BUSINESS_TYPE', 'RETAILER',          'Retailer',                      8),
      ('BUSINESS_TYPE', 'TRADER',            'Trader',                        9),
      ('BUSINESS_TYPE', 'BROKER_AGENT',      'Broker / Agent',               10),
      ('BUSINESS_TYPE', 'CONTRACTOR',        'Contractor',                   11),
      ('BUSINESS_TYPE', 'SUB_CONTRACTOR',    'Sub-Contractor',               12),
      ('BUSINESS_TYPE', 'JOB_WORKER',        'Job Worker',                   13),
      ('BUSINESS_TYPE', 'SERVICE_PROVIDER',  'Service Provider',             14),
      ('BUSINESS_TYPE', 'CONSULTANT',        'Consultant',                   15),
      ('BUSINESS_TYPE', 'FRANCHISE',         'Franchise',                    16),
      ('BUSINESS_TYPE', 'ECOMMERCE_SELLER',  'E-commerce Seller',            17),
      ('BUSINESS_TYPE', 'OTHERS',            'Others',                       99),

      -- MSME Type (no "Others" — tail is "Not Available")
      ('MSME_TYPE',     'MICRO',             'Micro',                         1),
      ('MSME_TYPE',     'SMALL',             'Small',                         2),
      ('MSME_TYPE',     'MEDIUM',            'Medium',                        3),
      ('MSME_TYPE',     'NOT_AVAILABLE',     'Not Available',                99),

      -- Account Type (banking row dropdown)
      ('ACCOUNT_TYPE',  'SAVINGS',           'Savings',                       1),
      ('ACCOUNT_TYPE',  'CURRENT',           'Current',                       2),
      ('ACCOUNT_TYPE',  'OD',                'Overdraft (OD)',                3),
      ('ACCOUNT_TYPE',  'CC',                'Cash Credit (CC)',              4),

      -- KYC status (drives BOTH the "SCOC status" and "KYC status" dropdowns —
      -- the Electron form points both at data-lookup="KYC_STATUS")
      ('KYC_STATUS',    'PENDING',           'Pending',                       1),
      ('KYC_STATUS',    'IN_REVIEW',         'In Review',                     2),
      ('KYC_STATUS',    'VERIFIED',          'Verified',                      3),
      ('KYC_STATUS',    'REJECTED',          'Rejected',                      4),

      -- Document status
      ('DOC_STATUS',    'PENDING',           'Pending',                       1),
      ('DOC_STATUS',    'IN_REVIEW',         'In Review',                     2),
      ('DOC_STATUS',    'VERIFIED',          'Verified',                      3),
      ('DOC_STATUS',    'REJECTED',          'Rejected',                      4),

      -- Local / OS (sourcing origin)
      ('LOCAL_OS',      'LOCAL',             'Local',                         1),
      ('LOCAL_OS',      'IMPORT',            'Import (OS)',                    2),

      -- Category — PLACEHOLDER defaults; replace with your real category codes.
      ('CATEGORY_CODE', 'RAW_MATERIAL',      'Raw Material',                  1),
      ('CATEGORY_CODE', 'PACKAGING',         'Packaging Material',            2),
      ('CATEGORY_CODE', 'FINISHED_GOODS',    'Finished Goods',                3),
      ('CATEGORY_CODE', 'CONSUMABLES',       'Consumables',                   4),
      ('CATEGORY_CODE', 'SERVICES',          'Services',                      5),
      ('CATEGORY_CODE', 'OTHERS',            'Others',                       99)
  ) AS v(lookup_type, code, label, display_order)
 WHERE NOT EXISTS (
     SELECT 1 FROM lookup_value lv
      WHERE lv.lookup_type = v.lookup_type
        AND lv.code        = v.code
 );
