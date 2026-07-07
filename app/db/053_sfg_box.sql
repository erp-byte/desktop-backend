-- ===========================================================================
-- 053_sfg_box.sql   (CANONICAL — SFG / Job-Card integration, Slice 6)
-- ---------------------------------------------------------------------------
-- WIP-completion QR box labels & physical SFG movement verification.
-- Production-side MIRROR of po_box: one row per physical SFG box/bag produced
-- at a WIP-stage completion. box_id is the QR payload; the box carries the
-- producing job-card id (the "transaction JC id") + the SFG#### + its weight.
-- These rows are the physical sub-split of ONE WIP inventory_batch (Slice 5).
--
-- ID CONVENTION (gate G5 → 8-digit app-supplied, same as the rest of the SFG /
-- Job-Card work): box_id is an 8-digit BIGINT primary key minted by the Slice-6
-- sfg_box_service via new_short_time_id() + insert_with_pk_retry() (NOT a
-- 'SFGB-<jc#>-<n>' text code). The QR encodes that number. parent_box_id is the
-- BIGINT self-reference (deferred). Unlike po_box (TEXT box_id), this side keys
-- on the numeric id to match job_card_v2 / all_sku SFG rows / so_line / ncr.
--
-- DEFERRED (per scope): full batch/lot genealogy. `lot_number` and
-- `parent_box_id` are present but MUST stay NULL until the issue-note +
-- lot-picker alignment lands.
--
-- Standalone table (no FK to job_card_v2, to stay safe in the v2-orphaning
-- runner and match how po_box references its own transaction). Idempotent.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS sfg_box (
    box_id                    BIGINT PRIMARY KEY,               -- QR payload: 8-digit app-supplied id (new_short_time_id)
    job_card_id               BIGINT NOT NULL,                  -- producing WIP JC = transaction JC id
    job_card_number           TEXT,                             -- PLAN-{plan}-L{line}-S{step}
    sfg_code                  TEXT   NOT NULL,                  -- SFG#### (all_sku item_type='sfg')
    entity                    TEXT,                             -- cfpl | cdpl
    floor                     TEXT,                             -- producing floor / unit
    stage_bucket              TEXT,                             -- 'Create WIP'
    box_number                INT    NOT NULL,                  -- 1 .. total_boxes
    total_boxes               INT    NOT NULL,
    net_weight                NUMERIC(15,3) NOT NULL,           -- weighed kg in THIS box
    gross_weight              NUMERIC(15,3),                    -- optional (tare + net)
    status                    TEXT   NOT NULL DEFAULT 'PRINTED',-- PRINTED -> DISPATCHED -> RECEIVED -> CONSUMED
    source_inventory_batch_id TEXT,                             -- the Slice-5 WIP inventory_batch this box came from
    received_into_job_card_id BIGINT,                           -- downstream JC that scanned this box in
    lot_number                TEXT,                             -- DEFERRED (batch traceability) — keep NULL
    parent_box_id             BIGINT,                           -- DEFERRED (box genealogy, self-ref to box_id) — keep NULL
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_sfg_box_status
        CHECK (status IN ('PRINTED','DISPATCHED','RECEIVED','CONSUMED')),
    CONSTRAINT chk_sfg_box_number
        CHECK (box_number >= 1 AND box_number <= total_boxes),
    -- One physical box position per producing JC — the schema backstop that
    -- makes a concurrent double-split collide (the service also takes a per-JC
    -- advisory lock; this catches any non-locked path).
    CONSTRAINT uq_sfg_box_jc_boxnum UNIQUE (job_card_id, box_number)
);

-- Idempotent upgrade for a DB that already has an OLDER sfg_box (box_id TEXT):
-- CREATE TABLE IF NOT EXISTS above is a no-op there, so convert the columns +
-- add the unique constraint explicitly. Guarded so a fresh build (already
-- BIGINT) and a re-run both no-op. The cast assumes box_id values are numeric
-- (the feature is unreleased, so the table is empty in practice).
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
         WHERE table_name = 'sfg_box' AND column_name = 'box_id') = 'text' THEN
        ALTER TABLE sfg_box ALTER COLUMN box_id        TYPE BIGINT USING box_id::bigint;
        ALTER TABLE sfg_box ALTER COLUMN parent_box_id TYPE BIGINT USING parent_box_id::bigint;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_sfg_box_jc_boxnum'
    ) THEN
        ALTER TABLE sfg_box ADD CONSTRAINT uq_sfg_box_jc_boxnum UNIQUE (job_card_id, box_number);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_sfg_box_jc   ON sfg_box(job_card_id);
CREATE INDEX IF NOT EXISTS idx_sfg_box_sku  ON sfg_box(sfg_code, status);
CREATE INDEX IF NOT EXISTS idx_sfg_box_recv ON sfg_box(received_into_job_card_id);

COMMENT ON TABLE sfg_box IS
    'Physical SFG boxes/bags produced at a WIP-stage completion. QR(box_id) printed and '
    'scan-verified between floors/stages/units (mirror of po_box). SUM(net_weight) per '
    'job_card_id reconciles to that JC''s WIP inventory_batch. Batch/lot genealogy deferred.';
