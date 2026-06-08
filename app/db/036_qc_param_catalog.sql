-- =========================================================================
-- 036_qc_param_catalog.sql
--
-- RM Check Parameters → ONE table. Extends the existing qc_parameter catalog
-- (from 034) with the richer metadata carried by data/candor_rm_ncr_fields.xlsx
-- ("RM Check Parameters" sheet) and seeds the 34 *measurable* check parameters.
--
-- Only the measurable parameters live here (Organoleptic, Physical, Defects,
-- Dates-specific, Foreign/Pest, Moisture, Chemical, Nut-specific). The sheet's
-- lot-info / sampling / acceptance / free-text rows are NOT parameters — they
-- are header columns on po_header / po_line / qc_inspection and are out of
-- scope for this catalog.
--
-- `code` is the stable identifier (referenced by readings + NCR param details).
-- `value_kind` ('num'|'text') tells the UI / readings layer which observed
-- value column to use (qc_inward_reading.observed_value_num vs _text).
--
-- Fully idempotent — safe to re-run. PostgreSQL >= 13.
-- =========================================================================


-- ── 1. Extend the catalog ────────────────────────────────────────────────
ALTER TABLE qc_parameter ADD COLUMN IF NOT EXISTS code        TEXT;
ALTER TABLE qc_parameter ADD COLUMN IF NOT EXISTS param_group TEXT;
ALTER TABLE qc_parameter ADD COLUMN IF NOT EXISTS data_type   TEXT;
ALTER TABLE qc_parameter ADD COLUMN IF NOT EXISTS value_kind  TEXT;   -- 'num' | 'text'
ALTER TABLE qc_parameter ADD COLUMN IF NOT EXISTS spec_note   TEXT;   -- free-text limit from the sheet
ALTER TABLE qc_parameter ADD COLUMN IF NOT EXISTS is_active   BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE qc_parameter ADD COLUMN IF NOT EXISTS sort_order  INT;


-- ── 2. Reconcile the 3 legacy 034 seed rows to canonical codes ───────────
-- (keeps their parameter_id stable so the qc_sku_spec rows seeded for sku_id=1
--  keep pointing at the right parameter)
UPDATE qc_parameter SET code = 'ACID_VALUE'   WHERE code IS NULL AND name = 'Acid Value';
UPDATE qc_parameter SET code = 'PEROXIDE_VAL' WHERE code IS NULL AND name = 'Peroxide Value';
UPDATE qc_parameter SET code = 'MOIST_IR'     WHERE code IS NULL AND name = 'Moisture';


-- ── 3. Code is the stable unique key going forward ───────────────────────
CREATE UNIQUE INDEX IF NOT EXISTS uq_qc_parameter_code ON qc_parameter(code);


-- ── 4. Seed / upsert the 34 measurable parameters (keyed on code) ────────
INSERT INTO qc_parameter (name, unit, code, param_group, data_type, value_kind, spec_note, sort_order)
VALUES
    -- Organoleptic
    ('Taste',                          NULL,         'TASTE',          'Organoleptic',  'enum',    'text', 'OK / not OK',              10),
    ('Appearance',                     NULL,         'APPEARANCE',     'Organoleptic',  'enum',    'text', 'OK / not OK',              20),
    ('Colour',                         NULL,         'COLOR',          'Organoleptic',  'enum',    'text', 'OK / not OK',              30),
    ('Flavour / odour',                NULL,         'FLAVOR_ODOR',    'Organoleptic',  'enum',    'text', 'OK / not OK',              40),
    ('Rancidity',                      NULL,         'RANCIDITY',      'Organoleptic',  'enum',    'text', 'None',                     50),
    -- Physical
    ('Count per oz / kg (size grading)', 'count',    'COUNT',          'Physical',      'numeric', 'num',  'Per article (e.g. 25/27 almond)', 60),
    -- Defects
    ('Discoloured units',              '%',          'DISCOLOURED',    'Defects',       'numeric', 'num',  'Per spec',                 70),
    ('Damaged',                        '%',          'DAMAGED',        'Defects',       'numeric', 'num',  'Per spec',                 80),
    ('Other edible grains - impurity', '% by mass',  'OTHER_GRAINS',   'Defects',       'numeric', 'num',  'Per spec',                 90),
    ('Blemished',                      '%',          'BLEMISHED',      'Defects',       'numeric', 'num',  'Per spec',                 100),
    ('Total rejection',                '%',          'TOTAL_REJ_PCT',  'Defects',       'numeric', 'num',  'Computed',                 110),
    -- Dates-specific
    ('Dried (dates)',                  '%',          'DRIED',          'Dates-specific','numeric', 'num',  'Per spec',                 120),
    ('Loose skin (dates)',             '%',          'LOOSE_SKIN',     'Dates-specific','numeric', 'num',  'Per spec',                 130),
    ('Sugared (dates)',                '%',          'SUGARED',        'Dates-specific','numeric', 'num',  'Per spec',                 140),
    ('Brix (sugar content)',           'deg Bx',     'BRIX',           'Dates-specific','numeric', 'num',  'Per spec',                 150),
    ('Muddy (dates)',                  NULL,         'MUDDY',          'Dates-specific','enum',    'text', 'OK / not OK',              160),
    ('Shrivelled (dates)',             NULL,         'SHRIVELLED',     'Dates-specific','enum',    'text', 'OK / not OK',              170),
    -- Foreign / Pest
    ('Extraneous vegetable matter',    '%',          'EVM',            'Foreign / Pest','numeric', 'num',  'Per spec',                 180),
    ('Foreign matter / insect / mould / rodent', '%','FM_INSECT_MOULD','Foreign / Pest','numeric', 'num',  '<= 1%',                    190),
    ('Infestation / infested',         '%',          'INFESTATION',    'Foreign / Pest','numeric', 'num',  '0% (zero tolerance)',      200),
    -- Moisture
    ('Moisture by IR analyzer',        '%',          'MOIST_IR',       'Moisture',      'numeric', 'num',  '<= 11.5% (max)',           210),
    ('Weight of petri dish',           'g',          'PETRI_WT',       'Moisture',      'numeric', 'num',  NULL,                       220),
    ('Weight of sample (M)',           'g',          'SAMPLE_WT_M',    'Moisture',      'numeric', 'num',  NULL,                       230),
    ('Petri + sample (M1)',            'g',          'M1_WT',          'Moisture',      'numeric', 'num',  NULL,                       240),
    ('Dry weight (M2)',                'g',          'M2_WT',          'Moisture',      'numeric', 'num',  NULL,                       250),
    ('Moisture % = (M1-M2)/M*100',     '%',          'MOIST_GRAV',     'Moisture',      'numeric', 'num',  '<= 11.5% (max)',           260),
    -- Chemical
    ('Acid value',                     'ml NaOH/g',  'ACID_VALUE',     'Chemical',      'numeric', 'num',  'Per spec',                 270),
    ('Peroxide value',                 'meq/kg',     'PEROXIDE_VAL',   'Chemical',      'numeric', 'num',  'Per spec',                 280),
    ('Fat content',                    '%',          'FAT_PCT',        'Chemical',      'numeric', 'num',  'Per spec',                 290),
    ('Salt content',                   '%',          'SALT_PCT',       'Chemical',      'numeric', 'num',  'Per spec',                 300),
    -- Nut-specific
    ('Tonch (cashew defect)',          '%',          'TONCH',          'Nut-specific',  'numeric', 'num',  'Per spec',                 310),
    ('Stem (raisins/dates)',           '%',          'STEM',           'Nut-specific',  'numeric', 'num',  'Per spec',                 320),
    ('Empty shell (pista/walnut)',     '%',          'EMPTY_SHELL',    'Nut-specific',  'numeric', 'num',  'Per spec',                 330),
    ('Broken / split kernels',         '%',          'BROKEN_SPLIT',   'Nut-specific',  'numeric', 'num',  'Per spec',                 340)
ON CONFLICT (code) DO UPDATE SET
    name        = EXCLUDED.name,
    unit        = EXCLUDED.unit,
    param_group = EXCLUDED.param_group,
    data_type   = EXCLUDED.data_type,
    value_kind  = EXCLUDED.value_kind,
    spec_note   = EXCLUDED.spec_note,
    sort_order  = EXCLUDED.sort_order;
