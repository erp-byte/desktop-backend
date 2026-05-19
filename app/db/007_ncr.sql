-- =========================================================================
-- 007_ncr.sql
-- Schema for the documented /api/v1/ncr endpoints (NCR record + CAPA rounds
-- + verification + audit log + permission seeds).
--
-- Tables:
--   • ncr_record            — header
--   • ncr_parameter_detail  — failed-parameter snapshot at raise time
--   • ncr_supplier_action   — CAPA rounds (one row per round)
--   • ncr_event_log         — append-only audit (ncr_no, event_type, payload)
--
-- ncr_no format: 'NCR-YYYY-MM-DD-NNNNN' minted server-side via nextval()
-- against a global sequence (date prefix is informational; sequence is
-- monotonic across days for simplicity).
--
-- Idempotent. Safe to re-run. Requires PostgreSQL ≥ 11.
-- =========================================================================

-- ── id sequence ──────────────────────────────────────────────────────────

CREATE SEQUENCE IF NOT EXISTS ncr_record_seq START 1;

-- ── ncr_record ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ncr_record (
    ncr_no                    TEXT          PRIMARY KEY,
    status                    TEXT          NOT NULL DEFAULT 'raised',
    severity                  TEXT,                                          -- minor|major|critical
    raised_via                TEXT,                                          -- qc_auto|qc_override|manual

    -- Cross-references (all optional anchors)
    inspection_id             TEXT,
    transaction_no            TEXT,
    line_number               INT,
    sku_id                    INT,
    sku_name                  TEXT,
    supplier_id               INT          NOT NULL,
    supplier_name             TEXT,
    lot_number                TEXT,

    -- Issue
    rejected_qty              NUMERIC      NOT NULL,
    summary                   TEXT,
    documented_date           DATE,

    -- Disposition
    disposition               TEXT,
    disposition_qty           NUMERIC,
    financial_impact          NUMERIC,
    rationale                 TEXT,
    rtv_vehicle_no            TEXT,
    rtv_lr_no                 TEXT,
    concurrence_user_id       TEXT,
    disposition_at            TIMESTAMPTZ,
    disposition_by            TEXT,

    -- Lifecycle
    raised_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    raised_by                 TEXT,
    requires_dual_approval    BOOLEAN      NOT NULL DEFAULT FALSE,
    dual_approval_by          TEXT,
    dual_approval_at          TIMESTAMPTZ,

    -- Cancel
    cancel_reason             TEXT,
    cancelled_at              TIMESTAMPTZ,
    cancelled_by              TEXT,

    -- Reopen
    reopen_reason             TEXT,
    reopened_at               TIMESTAMPTZ,
    reopened_by               TEXT,

    -- Closure
    closed_at                 TIMESTAMPTZ,
    closure_tat_days          NUMERIC
);

ALTER TABLE ncr_record DROP CONSTRAINT IF EXISTS ncr_record_status_check;
ALTER TABLE ncr_record ADD  CONSTRAINT ncr_record_status_check CHECK (
    status IN (
        'raised', 'dispositioned', 'capa_pending', 'capa_submitted',
        'capa_failed_verification', 'closed', 'cancelled'
    )
);

ALTER TABLE ncr_record DROP CONSTRAINT IF EXISTS ncr_record_severity_check;
ALTER TABLE ncr_record ADD  CONSTRAINT ncr_record_severity_check CHECK (
    severity IS NULL OR severity IN ('minor', 'major', 'critical')
);

ALTER TABLE ncr_record DROP CONSTRAINT IF EXISTS ncr_record_disposition_check;
ALTER TABLE ncr_record ADD  CONSTRAINT ncr_record_disposition_check CHECK (
    disposition IS NULL OR disposition IN (
        'accept_with_concession', 'reject', 'rework', 'return_to_vendor', 'scrap'
    )
);

ALTER TABLE ncr_record DROP CONSTRAINT IF EXISTS ncr_record_qty_positive;
ALTER TABLE ncr_record ADD  CONSTRAINT ncr_record_qty_positive
    CHECK (rejected_qty > 0);

CREATE INDEX IF NOT EXISTS idx_ncr_status            ON ncr_record(status);
CREATE INDEX IF NOT EXISTS idx_ncr_severity          ON ncr_record(severity);
CREATE INDEX IF NOT EXISTS idx_ncr_supplier          ON ncr_record(supplier_id);
CREATE INDEX IF NOT EXISTS idx_ncr_sku               ON ncr_record(sku_id);
CREATE INDEX IF NOT EXISTS idx_ncr_inspection        ON ncr_record(inspection_id);
CREATE INDEX IF NOT EXISTS idx_ncr_transaction       ON ncr_record(transaction_no);
CREATE INDEX IF NOT EXISTS idx_ncr_raised_at         ON ncr_record(raised_at DESC);

-- ── ncr_parameter_detail ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ncr_parameter_detail (
    detail_id            BIGSERIAL    PRIMARY KEY,
    ncr_no               TEXT         NOT NULL REFERENCES ncr_record(ncr_no) ON DELETE CASCADE,
    parameter_id         INT,
    parameter_name       TEXT,
    observed_value_text  TEXT,
    observed_value_num   NUMERIC,
    spec_min             NUMERIC,
    spec_max             NUMERIC,
    deviation_pct        NUMERIC,
    severity             TEXT,
    notes                TEXT,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE ncr_parameter_detail DROP CONSTRAINT IF EXISTS ncr_param_severity_check;
ALTER TABLE ncr_parameter_detail ADD  CONSTRAINT ncr_param_severity_check CHECK (
    severity IS NULL OR severity IN ('minor', 'major', 'critical')
);

CREATE INDEX IF NOT EXISTS idx_ncr_param_ncr ON ncr_parameter_detail(ncr_no);

-- ── ncr_supplier_action (CAPA rounds) ────────────────────────────────────

CREATE TABLE IF NOT EXISTS ncr_supplier_action (
    action_id                       BIGSERIAL    PRIMARY KEY,
    ncr_no                          TEXT         NOT NULL REFERENCES ncr_record(ncr_no) ON DELETE CASCADE,
    round_no                        INT          NOT NULL,
    root_cause                      TEXT         NOT NULL,
    corrective_action               TEXT         NOT NULL,
    preventive_action               TEXT,
    target_date                     DATE         NOT NULL,
    responsible_person              TEXT         NOT NULL,
    evidence_s3_keys                TEXT[]       NOT NULL DEFAULT '{}',
    submitted_at                    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    submitted_by                    TEXT,
    submitted_via                   TEXT         NOT NULL DEFAULT 'purchase_relay',

    -- Verification (filled by /verify)
    verified_at                     TIMESTAMPTZ,
    verified_by                     TEXT,
    is_effective                    BOOLEAN,
    verification_notes              TEXT,
    verification_evidence_s3_keys   TEXT[]       NOT NULL DEFAULT '{}'
);

ALTER TABLE ncr_supplier_action DROP CONSTRAINT IF EXISTS ncr_capa_via_check;
ALTER TABLE ncr_supplier_action ADD  CONSTRAINT ncr_capa_via_check CHECK (
    submitted_via IN ('vendor_email', 'vendor_portal', 'purchase_relay')
);

ALTER TABLE ncr_supplier_action DROP CONSTRAINT IF EXISTS ncr_capa_round_unique;
ALTER TABLE ncr_supplier_action ADD  CONSTRAINT ncr_capa_round_unique
    UNIQUE (ncr_no, round_no);

CREATE INDEX IF NOT EXISTS idx_ncr_capa_ncr      ON ncr_supplier_action(ncr_no);
CREATE INDEX IF NOT EXISTS idx_ncr_capa_unverified ON ncr_supplier_action(ncr_no)
    WHERE verified_at IS NULL;

-- ── ncr_event_log ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ncr_event_log (
    event_id      BIGSERIAL    PRIMARY KEY,
    event_type    TEXT         NOT NULL,
    entity_type   TEXT         NOT NULL DEFAULT 'ncr',
    entity_id     TEXT         NOT NULL,        -- ncr_no
    occurred_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    occurred_by   TEXT,
    payload       JSONB
);

CREATE INDEX IF NOT EXISTS idx_ncr_event_entity ON ncr_event_log(entity_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_ncr_event_type   ON ncr_event_log(event_type, occurred_at DESC);

-- ── permission seeds ─────────────────────────────────────────────────────

INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description) VALUES
    ('ncr', 'record', NULL, 'read',    'View NCR list / detail / audit'),
    ('ncr', 'record', NULL, 'create',  'Manually raise an NCR'),
    ('ncr', 'record', NULL, 'update',  'Edit NCR header / disposition before CAPA'),
    ('ncr', 'record', NULL, 'approve', 'Submit disposition / cancel / reopen'),
    ('ncr', 'capa',   NULL, 'create',  'File a CAPA round'),
    ('ncr', 'capa',   NULL, 'update',  'Edit an unverified CAPA round'),
    ('ncr', 'capa',   NULL, 'delete',  'Delete an unverified CAPA round'),
    ('ncr', 'capa',   NULL, 'verify',  'Verify a CAPA round (closes or reopens NCR)')
ON CONFLICT (module, sub_module, sub_sub_module, action) DO NOTHING;

-- Admin: all NCR perms
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'admin' AND p.module = 'ncr'
ON CONFLICT DO NOTHING;

-- qc_inspector: read + capa.verify (downstream of QC verdicts)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'qc_inspector'
   AND p.module = 'ncr'
   AND ((p.sub_module = 'record' AND p.action = 'read')
     OR (p.sub_module = 'capa'   AND p.action = 'verify'))
ON CONFLICT DO NOTHING;

-- purchase_manager: read + create + update + approve + capa.* (vendor-side ownership)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'purchase_manager'
   AND p.module = 'ncr'
   AND p.action <> 'verify'                    -- verifier must not be raiser/dispositioner
ON CONFLICT DO NOTHING;

-- store_head: read + approve (cancel/reopen oversight)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'ncr'
   AND ((p.sub_module = 'record' AND p.action IN ('read', 'approve')))
ON CONFLICT DO NOTHING;

-- viewer: read only
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'viewer'
   AND p.module = 'ncr' AND p.action = 'read'
ON CONFLICT DO NOTHING;
