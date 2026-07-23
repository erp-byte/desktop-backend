-- 080_requisition_npd_targets.sql
-- Multiple NPD target articles per requisition. The single npd_target_name / pcs /
-- weight_per_piece / quantity on sample_requisitions stays as the MIRROR of the
-- first target (backward compat: search, notifications, draft-BOM + single-card
-- develop all read the header). This child table holds the FULL list — each target
-- an independent product with its own pcs × weight_per_piece → quantity (kg).
-- Applied out-of-band to the live DB (like samples 068-079). Idempotent.
CREATE TABLE IF NOT EXISTS sample_requisition_npd_targets (
    id                SERIAL PRIMARY KEY,
    requisition_id    INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    pcs               NUMERIC,
    weight_per_piece  NUMERIC,
    quantity          NUMERIC,               -- derived = pcs × weight_per_piece (kg)
    line_order        INT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sample_req_npd_targets ON sample_requisition_npd_targets(requisition_id);
