-- =========================================================================
-- 030_vendor_history.sql
--
-- State-management for the vendor module. Every mutation to vendor_master,
-- vendor_banking, vendor_document, vendor_contract writes a snapshot row
-- to its *_history sibling: the row state BEFORE the change (previous_state),
-- the row state AFTER (new_state), and a JSON diff of just the keys that
-- moved. Soft-delete + approval also write history rows so the audit trail
-- is gap-free.
--
-- Also adds:
--   • vendor_extraction_staging: pre-vendor multi-doc extraction. Phase-1
--     of the new "extract → review → submit" onboarding flow uploads files
--     and parks the suggested vendor / banking / document / contract fields
--     here keyed by a `staging_id`. A nightly sweep deletes rows older than
--     24h (and the matching S3 staging objects).
--
-- Idempotent. Safe to re-run. PostgreSQL ≥ 13 (gen_random_uuid).
-- =========================================================================

-- ── extraction staging ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS vendor_extraction_staging (
    staging_id      VARCHAR(40)  PRIMARY KEY,
    created_by      VARCHAR(64)  NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- Consolidated extraction payload. Shape:
    --   { vendor_fields: {...},
    --     banking_fields: [...],
    --     documents: [{s3_url, doc_type, doc_number, valid_to, ...}],
    --     contracts: [...],
    --     conflicts: [{field, values_seen, sources}],
    --     failures:  [{filename, error}] }
    payload         JSONB        NOT NULL,
    -- S3 prefix where the temp files live until promotion on submit.
    s3_staging_prefix VARCHAR(256) NOT NULL,
    -- Hard TTL — sweep job deletes rows past this timestamp + their S3 objects.
    expires_at      TIMESTAMPTZ  NOT NULL DEFAULT now() + INTERVAL '24 hours',
    -- Set when the user submits — keeps the row around for audit but stops
    -- the sweep from purging it. NULL while still draftable.
    consumed_at     TIMESTAMPTZ,
    consumed_vendor_id VARCHAR(32) REFERENCES vendor_master(vendor_id)
);

CREATE INDEX IF NOT EXISTS idx_vendor_staging_created_by
    ON vendor_extraction_staging(created_by, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vendor_staging_expires
    ON vendor_extraction_staging(expires_at)
    WHERE consumed_at IS NULL;


-- ── history tables ────────────────────────────────────────────────────────
--
-- One table per parent. Shared shape so the service layer + API can be
-- generic. `history_id` is an 8-digit-time-based BIGINT minted by
-- new_short_time_id() in Python (same pattern as the v2 PKs).
--
-- previous_state: NULL on `create`, full row JSON otherwise.
-- new_state:      full row JSON post-change.
-- diff:           { field: { from: x, to: y } }  — keys present only when
--                  values actually changed.
-- source:         'manual' | 'extraction' | 'approve' | 'revert' | 'system'
-- reason:         optional human-supplied note (e.g. "GST cert renewed")

CREATE TABLE IF NOT EXISTS vendor_master_history (
    history_id      BIGINT       PRIMARY KEY,
    vendor_id       VARCHAR(32)  NOT NULL,
    operation       VARCHAR(16)  NOT NULL
                    CHECK (operation IN ('create','update','approve','delete','restore','revert')),
    changed_by      VARCHAR(64),
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    previous_state  JSONB,
    new_state       JSONB        NOT NULL,
    diff            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    source          VARCHAR(32)  NOT NULL DEFAULT 'manual',
    reason          TEXT,
    CONSTRAINT fk_vmh_vendor FOREIGN KEY (vendor_id)
        REFERENCES vendor_master(vendor_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vmh_vendor
    ON vendor_master_history(vendor_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_vmh_changed_at
    ON vendor_master_history(changed_at DESC);


CREATE TABLE IF NOT EXISTS vendor_banking_history (
    history_id      BIGINT       PRIMARY KEY,
    bank_id         VARCHAR(32)  NOT NULL,
    vendor_id       VARCHAR(32)  NOT NULL,
    operation       VARCHAR(16)  NOT NULL
                    CHECK (operation IN ('create','update','delete','set_primary')),
    changed_by      VARCHAR(64),
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    previous_state  JSONB,
    new_state       JSONB        NOT NULL,
    diff            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    source          VARCHAR(32)  NOT NULL DEFAULT 'manual',
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_vbh_bank
    ON vendor_banking_history(bank_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_vbh_vendor
    ON vendor_banking_history(vendor_id, changed_at DESC);


CREATE TABLE IF NOT EXISTS vendor_document_history (
    history_id      BIGINT       PRIMARY KEY,
    doc_id          VARCHAR(32)  NOT NULL,
    vendor_id       VARCHAR(32)  NOT NULL,
    operation       VARCHAR(16)  NOT NULL
                    CHECK (operation IN ('create','update','delete','append_file')),
    changed_by      VARCHAR(64),
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    previous_state  JSONB,
    new_state       JSONB        NOT NULL,
    diff            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    source          VARCHAR(32)  NOT NULL DEFAULT 'manual',
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_vdh_doc
    ON vendor_document_history(doc_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_vdh_vendor
    ON vendor_document_history(vendor_id, changed_at DESC);


CREATE TABLE IF NOT EXISTS vendor_contract_history (
    history_id      BIGINT       PRIMARY KEY,
    contract_id     VARCHAR(32)  NOT NULL,
    vendor_id       VARCHAR(32)  NOT NULL,
    operation       VARCHAR(16)  NOT NULL
                    CHECK (operation IN ('create','update','delete','append_file')),
    changed_by      VARCHAR(64),
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    previous_state  JSONB,
    new_state       JSONB        NOT NULL,
    diff            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    source          VARCHAR(32)  NOT NULL DEFAULT 'manual',
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_vch_contract
    ON vendor_contract_history(contract_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_vch_vendor
    ON vendor_contract_history(vendor_id, changed_at DESC);


-- ── append-only guard ─────────────────────────────────────────────────────
--
-- The whole point of the audit trail is that nobody can quietly rewrite it.
-- Corrections go via a fresh "revert" snapshot, never an UPDATE. We allow
-- DELETE on the parent row (CASCADE wipes history) so vendor deletion still
-- removes the child rows, but block in-place UPDATE.

CREATE OR REPLACE FUNCTION vendor_history_block_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'vendor history tables are append-only (UPDATE blocked on %)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_vmh_no_update'
    ) THEN
        CREATE TRIGGER trg_vmh_no_update
            BEFORE UPDATE ON vendor_master_history
            FOR EACH ROW EXECUTE FUNCTION vendor_history_block_update();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_vbh_no_update'
    ) THEN
        CREATE TRIGGER trg_vbh_no_update
            BEFORE UPDATE ON vendor_banking_history
            FOR EACH ROW EXECUTE FUNCTION vendor_history_block_update();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_vdh_no_update'
    ) THEN
        CREATE TRIGGER trg_vdh_no_update
            BEFORE UPDATE ON vendor_document_history
            FOR EACH ROW EXECUTE FUNCTION vendor_history_block_update();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_vch_no_update'
    ) THEN
        CREATE TRIGGER trg_vch_no_update
            BEFORE UPDATE ON vendor_contract_history
            FOR EACH ROW EXECUTE FUNCTION vendor_history_block_update();
    END IF;
END$$;


-- ── seed permission rows (action-scoped) ──────────────────────────────────
--
-- New permission strings:
--   vendor.master.view_history    — read history endpoints
--   vendor.master.revert          — apply previous_state as a new patch
--   vendor.master.extract         — call POST /vendors/extract-bulk
--
-- These are read by the same `check_permission()` path used by the rest of
-- the vendor surface. If the auth_permission table doesn't exist yet (fresh
-- DB), the inserts are skipped silently.

-- Uses NOT EXISTS rather than ON CONFLICT because the local auth_permission
-- schema doesn't always have a UNIQUE constraint on (module, sub_module,
-- action). NOT EXISTS works regardless of which constraints are in place.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'auth_permission') THEN
        INSERT INTO auth_permission (module, sub_module, action, description)
        SELECT v.module, v.sub_module, v.action, v.description
          FROM (VALUES
              ('vendor', 'master', 'view_history', 'Read vendor master history timeline'),
              ('vendor', 'master', 'revert',       'Restore a previous vendor snapshot'),
              ('vendor', 'master', 'extract',      'Run bulk document extraction before vendor creation')
          ) AS v(module, sub_module, action, description)
         WHERE NOT EXISTS (
             SELECT 1 FROM auth_permission ap
              WHERE ap.module     = v.module
                AND ap.sub_module = v.sub_module
                AND ap.action     = v.action
         );
    END IF;
END$$;
