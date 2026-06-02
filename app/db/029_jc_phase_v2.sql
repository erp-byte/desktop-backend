-- =========================================================================
-- Migration 029: Phased Closure (R13) - schema only
--
-- Creates job_card_phase_v2 (per-day phase rows inside a JC) and adds
-- phase_id FK columns on the three child tables that need to be tagged
-- per phase:
--   - job_card_output_v2
--   - job_card_partial_dispatch_v2
--   - job_card_shift_log_v2
--
-- Data backfill (one synthesized closed phase per existing JC + stamp
-- existing child rows) lives in scripts/029_backfill_phases.py so it can
-- use the canonical new_short_time_id() + insert_with_pk_retry() pattern
-- from app/core/helpers.py instead of bolting on a parallel sequence.
-- That keeps every v2 PK on the same generator and avoids drift.
--
-- Run order:
--   1. psql ... -f app/db/029_jc_phase_v2.sql           (this file - DDL)
--   2. PYTHONPATH=. python scripts/029_backfill_phases.py  (data move)
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- 1. job_card_phase_v2 - per-day phase rows
--
-- One row per (JC, calendar day). UNIQUE constraints prevent duplicates
-- on re-open / clock skew. The partial UNIQUE index enforces the
-- "single open phase per JC" invariant at the DB layer so the close
-- transaction can rely on it.
CREATE TABLE IF NOT EXISTS job_card_phase_v2 (
    phase_id            BIGINT PRIMARY KEY,
    job_card_id         BIGINT NOT NULL REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    phase_number        INT NOT NULL CHECK (phase_number > 0),
    phase_date          DATE NOT NULL,
    planned_qty_kg      NUMERIC(15,3),
    produced_qty_kg     NUMERIC(15,3),
    extra_give_away_qty NUMERIC(15,3) NOT NULL DEFAULT 0,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at            TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
        'open', 'closed', 'cancelled'
    )),
    closed_by           TEXT,
    closed_at           TIMESTAMPTZ,
    notes               TEXT,
    UNIQUE (job_card_id, phase_number),
    UNIQUE (job_card_id, phase_date)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_jcphase_one_open
    ON job_card_phase_v2(job_card_id)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_jcphase_jc     ON job_card_phase_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcphase_date   ON job_card_phase_v2(phase_date);
CREATE INDEX IF NOT EXISTS idx_jcphase_status ON job_card_phase_v2(status);

-- 2. Add phase_id FK columns on the three child tables
ALTER TABLE job_card_output_v2
    ADD COLUMN IF NOT EXISTS phase_id BIGINT REFERENCES job_card_phase_v2(phase_id);
ALTER TABLE job_card_partial_dispatch_v2
    ADD COLUMN IF NOT EXISTS phase_id BIGINT REFERENCES job_card_phase_v2(phase_id);
ALTER TABLE job_card_shift_log_v2
    ADD COLUMN IF NOT EXISTS phase_id BIGINT REFERENCES job_card_phase_v2(phase_id);

CREATE INDEX IF NOT EXISTS idx_jco_v2_phase  ON job_card_output_v2(phase_id);
CREATE INDEX IF NOT EXISTS idx_jcpd_v2_phase ON job_card_partial_dispatch_v2(phase_id);
CREATE INDEX IF NOT EXISTS idx_jcsl_v2_phase ON job_card_shift_log_v2(phase_id);

COMMIT;
