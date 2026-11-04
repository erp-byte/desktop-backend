-- 135_cr_cold_stock_rows.sql — the cold-stock rows the ERP made from cold returns.
--
-- Once a customer return to a cold store (Savla D-39 / D-514 / Bond, Rishi, Supreme,
-- Eskimo) is approved, each printed box becomes one row of {cfpl,cdpl}_cold_stocks, as
-- IMS did until it closed on 2026-10-19
-- (app/modules/customer_returns/services/cold_stock_service.py). Each row made is
-- recorded here, with what was copied from its box:
--   * a later edit of the box (net weight, lot, mark, vakkal, remarks) is carried to its
--     row, and nothing else is touched, so an inner cold transfer's relabel stays;
--   * a box whose row has gone is not made again;
--   * a row listed here is the ERP's, not IMS's: a return with IMS's rows keeps its
--     boxes locked, a return with the ERP's doesn't.
--
-- Until this table exists the backend copies nothing and logs a warning, so apply it
-- before the deploy: a return approved without it is copied only at its next box print
-- or save. Idempotent.

CREATE TABLE IF NOT EXISTS cr_cold_stock_rows (
    id             BIGSERIAL PRIMARY KEY,
    company        TEXT        NOT NULL CHECK (company IN ('CFPL', 'CDPL')),
    rtv_id         TEXT        NOT NULL,
    box_id         TEXT        NOT NULL,
    cold_table     TEXT        NOT NULL CHECK (cold_table IN ('cfpl_cold_stocks', 'cdpl_cold_stocks')),
    cold_stock_id  BIGINT      NOT NULL,
    -- as last copied from the box (the line's value where the box had none)
    net_weight     NUMERIC,
    lot_no         TEXT,
    item_mark      TEXT,
    vakkal         TEXT,
    spl_remarks    TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company, rtv_id, box_id)
);

CREATE INDEX IF NOT EXISTS idx_cr_cold_stock_rows_row ON cr_cold_stock_rows (cold_table, cold_stock_id);
