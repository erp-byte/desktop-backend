-- =========================================================================
-- Migration 037: Sample Issuing module — core tables + extensions
--
-- Creates the sample-specific entities and wires them to existing
-- infrastructure (all_sku, bom_header, auth_user, gate_passes [036],
-- material_document). Reconciled to THIS repo:
--   • entity columns use CHECK (entity IN ('cfpl','cdpl')) like every other
--     domain table here.
--   • sample_approval_role_map is seeded with REAL roles (business_head,
--     floor_manager, inventory_manager) — no stand-ins.
--   • gate passes are the generic gate_passes table (036); sample-only fields
--     live in gate_pass_sample_details below.
--   • linked_job_card_id is a plain BIGINT (no FK yet). The job_card vs
--     job_card_v2 target + the job_card back-reference column land in a
--     follow-up migration (038) once that decision is confirmed.
--
-- Depends on: 035 (business_head / npd_team roles), 036 (gate_passes),
--             schema.sql (all_sku), production_schema.sql (bom_header),
--             auth_schema.sql (auth_user, auth_role), sap_mm_align.sql
--             (material_document).
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- =========================================================================
-- 1. sample_requisitions — central requisition record
-- =========================================================================
CREATE TABLE IF NOT EXISTS sample_requisitions (
    id                        SERIAL PRIMARY KEY,
    requisition_number        TEXT UNIQUE NOT NULL,                 -- SMP-YYYY-NNNN (app-formatted via sequence)
    sample_type               TEXT NOT NULL
                              CHECK (sample_type IN ('BASIS_RM','BASIS_FG','NPD','INTERNAL')),
    status                    TEXT NOT NULL DEFAULT 'DRAFT'
                              CHECK (status IN (
                                  'DRAFT','SUBMITTED','BH_APPROVED','BH_REJECTED',
                                  'IN_PRODUCTION','PACKING','READY_FOR_DISPATCH',
                                  'INTERNALLY_DISPATCHED','PARTIALLY_CONVERTED',
                                  'GATE_PASS_ISSUED','CLOSED','CANCELLED'
                              )),
    requestor_user_id         INT NOT NULL REFERENCES auth_user(user_id),
    requestor_team            TEXT,
    business_head_user_id     INT REFERENCES auth_user(user_id),    -- target approver, resolved at submission
    purpose_tag               TEXT
                              CHECK (purpose_tag IN (
                                  'CUSTOMER_DISPLAY','CUSTOMER_ISSUE','TASTING_SENSORY',
                                  'PHYSICAL_PARAMETERS','INTERNAL_OTHER'
                              )),
    purpose_note              TEXT,
    base_bom_id               INT REFERENCES bom_header(bom_id),    -- NPD base BOM
    npd_draft_bom_id          INT,                                  -- FK added below, after npd_draft_boms exists
    linked_job_card_id        BIGINT,                               -- FK added in 038 (job_card vs job_card_v2 TBC)
    linked_gate_pass_id       INT,                                  -- FK added below, references gate_passes(id)
    converted_from_id         INT REFERENCES sample_requisitions(id), -- redirect / partial-conversion chain
    internal_override         BOOLEAN NOT NULL DEFAULT FALSE,
    converted_to_external     BOOLEAN NOT NULL DEFAULT FALSE,
    entity                    TEXT NOT NULL CHECK (entity IN ('cfpl','cdpl')),
    cancellation_reason       TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by                INT REFERENCES auth_user(user_id),
    updated_by                INT REFERENCES auth_user(user_id),
    deleted_at                TIMESTAMPTZ,                          -- soft-delete (matches job_card convention)
    deleted_by                INT REFERENCES auth_user(user_id)
);

CREATE INDEX IF NOT EXISTS idx_sample_req_status     ON sample_requisitions(status) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sample_req_type       ON sample_requisitions(sample_type) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sample_req_entity     ON sample_requisitions(entity);
CREATE INDEX IF NOT EXISTS idx_sample_req_requestor  ON sample_requisitions(requestor_user_id);
CREATE INDEX IF NOT EXISTS idx_sample_req_created_at ON sample_requisitions(created_at);

CREATE SEQUENCE IF NOT EXISTS sample_requisition_seq START 1;

-- =========================================================================
-- 2. sample_requisition_articles — article line items
-- =========================================================================
CREATE TABLE IF NOT EXISTS sample_requisition_articles (
    id                SERIAL PRIMARY KEY,
    requisition_id    INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    sku_id            INT NOT NULL REFERENCES all_sku(sku_id),
    sku_name          TEXT NOT NULL,                                -- snapshot of all_sku.particulars at request time
    required_qty      NUMERIC NOT NULL CHECK (required_qty > 0),
    issued_qty        NUMERIC CHECK (issued_qty >= 0),
    uom               TEXT NOT NULL,                                -- display string from sku-lookup (all_sku.uom is numeric)
    article_role      TEXT NOT NULL
                      CHECK (article_role IN ('RM','FG','NPD_INPUT','NPD_OUTPUT')),
    pack_size_kg      NUMERIC,
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sample_req_articles_req ON sample_requisition_articles(requisition_id);
CREATE INDEX IF NOT EXISTS idx_sample_req_articles_sku ON sample_requisition_articles(sku_id);

-- =========================================================================
-- 3. sample_approvals — approval chain per requisition stage
-- =========================================================================
CREATE TABLE IF NOT EXISTS sample_approvals (
    id                SERIAL PRIMARY KEY,
    requisition_id    INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    approval_stage    TEXT NOT NULL
                      CHECK (approval_stage IN (
                          'BH_APPROVAL','PRODUCTION_ACK','INV_MGR_VERIFICATION',
                          'INV_MGR_SIGNOFF','CONVERSION_APPROVAL','CONVERSION_INV_MGR_SIGNOFF'
                      )),
    approver_user_id  INT NOT NULL REFERENCES auth_user(user_id),
    role_at_action    TEXT NOT NULL,                                -- snapshot of role_name at action time
    action            TEXT NOT NULL DEFAULT 'PENDING'
                      CHECK (action IN ('PENDING','APPROVED','REJECTED')),
    remarks           TEXT,
    sequence_no       INT NOT NULL,                                 -- ordering of stages on a requisition
    actioned_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_active_approval UNIQUE (requisition_id, approval_stage, sequence_no)
);

CREATE INDEX IF NOT EXISTS idx_sample_approvals_req      ON sample_approvals(requisition_id);
CREATE INDEX IF NOT EXISTS idx_sample_approvals_approver ON sample_approvals(approver_user_id);

-- =========================================================================
-- 4. npd_draft_boms — NPD draft BOM (isolated from live bom_header)
-- =========================================================================
CREATE TABLE IF NOT EXISTS npd_draft_boms (
    id                    SERIAL PRIMARY KEY,
    requisition_id        INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    base_bom_id           INT REFERENCES bom_header(bom_id),
    fg_sku_id             INT REFERENCES all_sku(sku_id),
    fg_sku_name           TEXT,                                     -- snapshot
    description           TEXT,
    status                TEXT NOT NULL DEFAULT 'DRAFT'
                          CHECK (status IN ('DRAFT','USED','PROMOTED','ARCHIVED')),
    promoted_bom_id       INT REFERENCES bom_header(bom_id),        -- set when promoted to a live BOM
    promoted_at           TIMESTAMPTZ,
    promoted_by           INT REFERENCES auth_user(user_id),
    promotion_approval_id INT REFERENCES sample_approvals(id),
    created_by            INT REFERENCES auth_user(user_id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_npd_draft_boms_req    ON npd_draft_boms(requisition_id);
CREATE INDEX IF NOT EXISTS idx_npd_draft_boms_status ON npd_draft_boms(status);

-- =========================================================================
-- 5. npd_draft_bom_lines — NPD draft BOM line items
-- =========================================================================
CREATE TABLE IF NOT EXISTS npd_draft_bom_lines (
    id                SERIAL PRIMARY KEY,
    draft_bom_id      INT NOT NULL REFERENCES npd_draft_boms(id) ON DELETE CASCADE,
    sku_id            INT NOT NULL REFERENCES all_sku(sku_id),
    sku_name          TEXT NOT NULL,                                -- snapshot
    qty               NUMERIC NOT NULL CHECK (qty >= 0),
    uom               TEXT NOT NULL,
    item_type         TEXT CHECK (item_type IN ('rm','pm')),        -- matches bom_line.item_type
    delta_type        TEXT NOT NULL DEFAULT 'UNCHANGED'
                      CHECK (delta_type IN ('UNCHANGED','ADDED','MODIFIED','REMOVED')),
    original_qty      NUMERIC,                                      -- only for MODIFIED
    line_order        INT NOT NULL DEFAULT 0,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_npd_draft_bom_lines_draft ON npd_draft_bom_lines(draft_bom_id);

-- =========================================================================
-- 6. sample_audit_log — full audit trail per requisition
-- =========================================================================
CREATE TABLE IF NOT EXISTS sample_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    requisition_id  INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,                                  -- STATUS_CHANGE, APPROVAL, ARTICLE_EDIT, CONVERSION, GATE_PASS_PRINT, VOID, ...
    old_value       JSONB,
    new_value       JSONB,
    actor_user_id   INT REFERENCES auth_user(user_id),
    actor_role      TEXT,
    remarks         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sample_audit_req        ON sample_audit_log(requisition_id);
CREATE INDEX IF NOT EXISTS idx_sample_audit_created_at ON sample_audit_log(created_at);

-- =========================================================================
-- 7. sample_approval_role_map — config-driven approval matrix (REAL roles)
-- =========================================================================
CREATE TABLE IF NOT EXISTS sample_approval_role_map (
    id              SERIAL PRIMARY KEY,
    approval_stage  TEXT NOT NULL
                    CHECK (approval_stage IN (
                        'BH_APPROVAL','PRODUCTION_ACK','INV_MGR_VERIFICATION',
                        'INV_MGR_SIGNOFF','CONVERSION_APPROVAL','CONVERSION_INV_MGR_SIGNOFF'
                    )),
    sample_type     TEXT
                    CHECK (sample_type IN ('BASIS_RM','BASIS_FG','NPD','INTERNAL','*')),
    entity          TEXT NOT NULL DEFAULT '*',                     -- '*' = all entities
    required_role   TEXT NOT NULL REFERENCES auth_role(role_name),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (approval_stage, sample_type, entity, required_role)
);

INSERT INTO sample_approval_role_map (approval_stage, sample_type, required_role) VALUES
    ('BH_APPROVAL',                '*',        'business_head'),
    ('PRODUCTION_ACK',             'BASIS_FG', 'floor_manager'),
    ('PRODUCTION_ACK',             'NPD',      'floor_manager'),
    ('INV_MGR_VERIFICATION',       '*',        'inventory_manager'),
    ('INV_MGR_SIGNOFF',            '*',        'inventory_manager'),
    ('CONVERSION_APPROVAL',        '*',        'business_head'),
    ('CONVERSION_INV_MGR_SIGNOFF', '*',        'inventory_manager')
ON CONFLICT (approval_stage, sample_type, entity, required_role) DO NOTHING;

-- =========================================================================
-- 8. sample_config — system flags
-- =========================================================================
CREATE TABLE IF NOT EXISTS sample_config (
    config_key    TEXT PRIMARY KEY,
    config_value  TEXT NOT NULL,
    description   TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by    INT REFERENCES auth_user(user_id)
);

INSERT INTO sample_config (config_key, config_value, description) VALUES
    ('SAMPLE_SAME_DAY_REAPPROVAL_REQUIRED', 'true',
        'If false, converting an internal sample to a gate pass within the same day skips fresh BH approval (still records a re-confirm action).'),
    ('SAMPLE_INTERNAL_OTHER_CONVERSION_ALLOWED', 'false',
        'If true, INTERNAL_OTHER purpose can be converted to a gate pass without a BH override.'),
    ('SAMPLE_AUTO_CLOSE_INTERNAL_AFTER_DAYS', '30',
        'Auto-CLOSE INTERNALLY_DISPATCHED records after N days with no conversion.')
ON CONFLICT (config_key) DO NOTHING;

-- =========================================================================
-- 9. gate_pass_sample_details — sample-specific 1:1 companion to gate_passes
-- =========================================================================
CREATE TABLE IF NOT EXISTS gate_pass_sample_details (
    gate_pass_id            INT PRIMARY KEY REFERENCES gate_passes(id) ON DELETE CASCADE,
    requisition_id          INT NOT NULL REFERENCES sample_requisitions(id),
    original_requisition_id INT REFERENCES sample_requisitions(id),  -- for redirected / converted passes
    sample_type             TEXT,                                    -- snapshot for print (NPD badge, etc.)
    purpose_tag             TEXT,
    purpose_note            TEXT,
    converted_from_internal BOOLEAN NOT NULL DEFAULT FALSE,
    conversion_qty          NUMERIC,                                 -- partial-conversion bookkeeping
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gp_sample_req ON gate_pass_sample_details(requisition_id);

-- =========================================================================
-- 10. Back-fill deferred FKs on sample_requisitions
--     (forward references resolved now that target tables exist)
-- =========================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sample_req_npd_draft') THEN
        ALTER TABLE sample_requisitions
            ADD CONSTRAINT fk_sample_req_npd_draft
            FOREIGN KEY (npd_draft_bom_id) REFERENCES npd_draft_boms(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sample_req_gate_pass') THEN
        ALTER TABLE sample_requisitions
            ADD CONSTRAINT fk_sample_req_gate_pass
            FOREIGN KEY (linked_gate_pass_id) REFERENCES gate_passes(id);
    END IF;
END $$;

-- =========================================================================
-- 11. material_document extensions (sample linkage)
--     reference_type='SAMPLE_REQ' + movement_type 265/266 (registered in 036)
--     identify sample movements without a parallel discriminator column.
-- =========================================================================
ALTER TABLE material_document
    ADD COLUMN IF NOT EXISTS sample_requisition_id INT REFERENCES sample_requisitions(id),
    ADD COLUMN IF NOT EXISTS gate_pass_id          INT REFERENCES gate_passes(id),
    ADD COLUMN IF NOT EXISTS converted_to_external BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_matdoc_sample_req
    ON material_document(sample_requisition_id)
    WHERE sample_requisition_id IS NOT NULL;

COMMIT;
