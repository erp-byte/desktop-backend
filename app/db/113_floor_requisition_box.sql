-- ===========================================================================
-- 113_floor_requisition_box.sql
-- Stores -> Production Indents -> Scan: the boxes store sends for a floor
-- requisition (app/modules/floor_requisition/services/box_service.py).
--
--   * sfg_box.item_type gains 'rm': a box store printed a sticker for under
--     Manual print. The job card's Boxes printing list and batch caps read
--     item_type = 'sfg' only, so these never show there; the job card's Raw
--     Material scanner still finds them by carton_id.
--   * floor_requisition_box: one row per box scanned or printed for a request.
--     The floor still scans the boxes into the job card (jc_box_scan), so RM
--     issued is counted there, once.
--
-- MUST follow 067 / 073 (sfg_box) and 111 (floor_requisition). Idempotent.
-- ===========================================================================

ALTER TABLE sfg_box DROP CONSTRAINT IF EXISTS chk_box_item_type;
ALTER TABLE sfg_box ADD  CONSTRAINT chk_box_item_type CHECK (item_type IN ('sfg','fg','rm'));

CREATE TABLE IF NOT EXISTS floor_requisition_box (
    requisition_id  BIGINT        NOT NULL REFERENCES floor_requisition (requisition_id),
    box_code        TEXT          NOT NULL,                -- carton_id / box_id
    source          TEXT          NOT NULL,                -- 'printed' | 'scanned'
    box_table       TEXT          NOT NULL,                -- where the box was found
    box_number      INT,                                   -- printed only: "Box #" on the sticker
    transaction_no  TEXT,
    article         TEXT          NOT NULL,
    stock_type      TEXT,                                  -- printed only: Fresh Stock | Off Grade/Rejection
    lot_number      TEXT,
    net_weight      NUMERIC(15,3),
    gross_weight    NUMERIC(15,3),
    count           INT,
    recorded_by     TEXT          NOT NULL,
    recorded_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (requisition_id, box_code),
    CONSTRAINT chk_frb_source     CHECK (source IN ('printed','scanned')),
    CONSTRAINT chk_frb_box_number CHECK (box_number IS NULL OR box_number >= 1),
    CONSTRAINT chk_frb_printed    CHECK (source <> 'printed' OR box_number IS NOT NULL)
);

-- One sticker number per request.
CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_box_number
    ON floor_requisition_box (requisition_id, box_number) WHERE box_number IS NOT NULL;

-- "Which request was this box sent on?" from the box side.
CREATE INDEX IF NOT EXISTS idx_floor_requisition_box_code
    ON floor_requisition_box (box_code);
