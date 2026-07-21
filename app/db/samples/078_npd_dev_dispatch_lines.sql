-- 078_npd_dev_dispatch_lines.sql
-- Partial out: a CLOSED dev job card's finalized FG sample can be issued out in
-- MULTIPLE parts (each fires its own 265 Goods Issue and prints its own outpass),
-- instead of the single-shot dispatch in 046. One row per partial dispatch; the
-- running total (Σqty) is bounded by the card's output_qty in npd_dev_service —
-- the SELECT … FOR UPDATE on the parent makes the sum-check race-safe, so no table
-- CHECK (a CHECK can't read the parent's output_qty). The card stays CLOSED; the
-- 046 dispatch_* columns on npd_dev_job_cards are kept as a "latest dispatch"
-- mirror so the existing list/detail/full-outpass display keeps working untouched.
--
-- Applied out-of-band to the live DB (like samples 068-077); NOT wired into
-- scripts/migrate.py. Idempotent. Safe to re-run.

-- dispatch_id is an app-supplied 8-digit time-based BIGINT (new_short_time_id +
-- insert_with_pk_retry), the same handle pattern as phase_id — NOT a SERIAL.
CREATE TABLE IF NOT EXISTS npd_dev_dispatch (
    dispatch_id     BIGINT PRIMARY KEY,
    dev_jc_id       BIGINT NOT NULL REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE,
    seq             INT NOT NULL,                         -- 1,2,3… per card; the outpass sub-number
    qty             NUMERIC(15, 3) NOT NULL CHECK (qty > 0),
    recipient       TEXT,
    mat_doc_id      TEXT,                                 -- the 265 Goods Issue doc for this part
    notes           TEXT,
    dispatched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_by   INT REFERENCES auth_user(user_id),
    UNIQUE (dev_jc_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_npd_dev_dispatch_jc ON npd_dev_dispatch(dev_jc_id);

-- Backfill: cards dispatched under the old single-shot model (046) have the mirror
-- columns set but no ledger row, which would make remaining_qty overstate the
-- balance. Seed one seq=1 row from the mirror (reusing the card's own id as the
-- dispatch_id — unique per card; a future new_short_time_id() collision is handled
-- by insert_with_pk_retry in the service). Only when no ledger row exists yet.
INSERT INTO npd_dev_dispatch
    (dispatch_id, dev_jc_id, seq, qty, recipient, mat_doc_id, dispatched_at, dispatched_by)
SELECT jc.id, jc.id, 1, jc.dispatch_qty, jc.dispatch_recipient, jc.dispatch_mat_doc_id,
       jc.dispatched_at, jc.dispatched_by
  FROM npd_dev_job_cards jc
 WHERE jc.dispatched_at IS NOT NULL
   AND jc.dispatch_qty IS NOT NULL
   AND jc.dispatch_qty > 0
   AND NOT EXISTS (SELECT 1 FROM npd_dev_dispatch d WHERE d.dev_jc_id = jc.id);
