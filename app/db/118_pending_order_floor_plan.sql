-- ===========================================================================
-- 118_pending_order_floor_plan.sql
-- Pending orders, floor-wise plan: the "Pending orders <dd.mm.yyyy> - Floor
-- wise plan" workbook, Sheet1 (header on row 2, data from row 3, columns
-- A..AL; column H "SO No" is hidden in the workbook). An imported planning
-- snapshot, independent of so_header / so_fulfillment_v2.
--
-- The sheet has one row per ITEM LINE, and an SO No repeats on every line of
-- its order (24.09.2026 file: 1,686 lines, 318 distinct SO Nos, up to 65 lines
-- on one SO). SO No can only be UNIQUE on a one-row-per-order table, so the
-- sheet is split in two:
--
--   pending_order       one row per order; so_number is UNIQUE. Holds the
--                       columns that never vary across an order's lines
--                       (dates, company, PO No, customer, location, sales
--                       head, ageing).
--   pending_order_line  one row per sheet row, tied to its order by
--                       pending_order_id, numbered by line_number in sheet order.
--
-- pending_order_id and pending_order_line_id are app-supplied time ids
-- (app.core.helpers.new_short_time_id, the last 8 digits of epoch-ms) inserted
-- through insert_with_pk_retry. Each must be the PRIMARY KEY: that helper only
-- retries a unique violation whose constraint name contains "pkey". The last
-- 8 digits start with 0 about 10% of the time, so the CHECK is 0..99999999 and
-- an id can print with fewer than 8 digits (to_char(id, 'FM00000000') where 8
-- characters are needed). No SQL DEFAULT: a clock default collides on
-- multi-row inserts. The ids wrap about every 28 hours and are not in date
-- order, so order by order_date and line_number.
--
-- Blank SO No: 416 lines (103 POs) in the 24.09.2026 file have no SO yet.
-- so_number is nullable (UNIQUE treats NULLs as distinct) and an order with no
-- SO is keyed by (company, po_number, order_date) through the partial unique
-- index uq_pending_order_no_so. order_date is in the key because PO 5115914651
-- appears with two order dates (rows 1169 and 1291: same location, item and
-- qty; possibly one order entered twice).
--
-- Import rules (one transaction per file; the sheet holds pasted values, no
-- formulas):
--   * Clean text with the helpers.py whitespace normaliser (NBSP, zero-width,
--     newlines), then trim. SO No / PO No arrive as numbers for some customers
--     (8700942817): store the digits as TEXT with no ".0". Seen: SO
--     ' PO-MH/26-27/0034', SO and PO '5/26-27 ', Locations with a leading
--     newline.
--   * "Ageing Days" '#N/A' -> NULL (all 53 are on orders dated the report
--     date, Over Days 0, which the sheet's bucket lookup does not cover).
--   * "PO Closur Date1" is text 'dd.mm.yyyy' -> DATE.
--   * report_date is the sheet's "Current Date" (2026-09-23 in the file named
--     24.09.2026), not the date in the file name.
--   * Parse numbers as Decimal, not helpers.safe_float (it rounds to 3 dp).
--     Rounding to the column scale removes float noise (3.5999999999999996 ->
--     3.600) and also sub-scale digits of line_fill, pending_value_cr and the
--     *_value columns.
--   * Every line of an order must carry the same header values; stop and
--     report if not.
--   * Match each order, in this order:
--       1. SO No present: the row with that so_number; if its company,
--          po_number or customer_name differ, stop and report (SO Nos are
--          mostly customer PO numbers, and short ones such as '1' or '3/26-27'
--          can repeat across customers). Else the row with so_number IS NULL
--          and the same (company, po_number, order_date): UPDATE its
--          so_number. Else insert.
--       2. SO No blank, and an order in the same file has an SO and the same
--          (company, po_number, order_date): the line belongs to that order
--          (24.09.2026: row 64 -> SO 8700941775), and any blank-SO row for
--          that key is deleted. Else upsert ON CONFLICT (company, po_number,
--          order_date) WHERE so_number IS NULL. ON CONFLICT (so_number) never
--          matches a NULL.
--       3. Leave pending_order_id out of DO UPDATE SET, set updated_at = now()
--          (there is no trigger), and replace the order's lines. Line ids change
--          on every import, so reference (pending_order_id, line_number); the
--          planning columns are overwritten by the file.
--       4. DELETE FROM pending_order WHERE report_date < this file's
--          report_date (no longer pending; lines cascade). Refuse a file older
--          than MAX(report_date).
--
-- The derived columns are stored as the sheet has them, not GENERATED: Total Kg
-- disagrees with Uom x Pending Qty on 21 lines, and In CR is blank on 132.
--
-- Idempotent: IF NOT EXISTS throughout.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS pending_order (
    pending_order_id  BIGINT PRIMARY KEY
                      CHECK (pending_order_id BETWEEN 0 AND 99999999),
    so_number         TEXT UNIQUE                       -- SO No
                      CHECK (so_number = BTRIM(so_number) AND so_number <> ''),
    company           TEXT NOT NULL,                    -- Company (CDPL / CFPL)
    po_number         TEXT NOT NULL                     -- PO No
                      CHECK (po_number = BTRIM(po_number) AND po_number <> ''),
    po_date           DATE NOT NULL,                    -- PO Date
    order_date        DATE NOT NULL,                    -- Order Date
    customer_name     TEXT NOT NULL,                    -- Customer Name
    location          TEXT,                             -- Location (delivery point / DC)
    sales_head        TEXT,                             -- Sales Head
    po_closure_date   DATE,                             -- PO Closur Date1
    report_date       DATE NOT NULL,                    -- Current Date
    over_days         INT,                              -- Over Days = report_date - order_date
    ageing_bucket     TEXT,                             -- Ageing Days ('>20', '11-15', ...)

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Orders with no SO yet: one row per company + PO + order date.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_order_no_so
    ON pending_order (company, po_number, order_date)
    WHERE so_number IS NULL;

CREATE INDEX IF NOT EXISTS idx_pending_order_customer
    ON pending_order (customer_name, order_date);

CREATE TABLE IF NOT EXISTS pending_order_line (
    pending_order_line_id  BIGINT PRIMARY KEY
                           CHECK (pending_order_line_id BETWEEN 0 AND 99999999),
    pending_order_id       BIGINT NOT NULL
                           REFERENCES pending_order(pending_order_id) ON DELETE CASCADE,
    line_number            INT NOT NULL CHECK (line_number > 0),  -- sheet order within the order

    po_validity_group      TEXT,                     -- PO Validity/Group
    order_group            TEXT,                     -- Group
    pack_warehouse         TEXT NOT NULL,            -- Packed to be (W-202 / A-185)
    executed_by            TEXT,                     -- Executed By (a team, e.g. Nuts / PL, not a user)
    floor_label            TEXT,                     -- Floor, as the sheet labels it (Upper, 1st Floor-M,
                                                     --   Bar Line-H + F ...), not a declared floor name
    floor_person           TEXT,                     -- Person
    sku_name               TEXT NOT NULL,            -- Description

    kg_per_unit            NUMERIC(15,3) NOT NULL CHECK (kg_per_unit > 0),   -- Uom
    unit                   TEXT NOT NULL,                                    -- UNITS/KGS (Kgs / Units / Box)
    po_qty                 NUMERIC(14,3) NOT NULL CHECK (po_qty > 0),        -- Purchase Order Qty.
    dispatch_qty           NUMERIC(14,3) CHECK (dispatch_qty >= 0),          -- Dispatch Qty.
    pending_qty            NUMERIC(14,3) NOT NULL CHECK (pending_qty >= 0),  -- Pending Qty. = po_qty - dispatch_qty
    total_kg               NUMERIC(14,3) NOT NULL,                           -- Total Kg, ~ kg_per_unit x pending_qty

    dispatch_date          DATE,                     -- Dispatch Date
    invoice_no             TEXT,                     -- Invoice No.
    dispatch_days          INT,                      -- Days = dispatch_date - order_date

    basic_rate             NUMERIC(18,10) NOT NULL,  -- Basic Rate (sheet rates carry up to 13 dp)
    basic_value            NUMERIC(18,4) NOT NULL,   -- Basic Value = po_qty x basic_rate
    line_fill              NUMERIC(9,6),             -- Line Fill = dispatch_qty / po_qty
    pending_value          NUMERIC(18,4) NOT NULL,   -- Pending Value = pending_qty x basic_rate
    pending_value_cr       NUMERIC(14,8),            -- In CR = pending_value / 1,00,00,000
    dispatch_value         NUMERIC(18,4),            -- Dispatch Value = dispatch_qty x basic_rate

    planned_dispatch_date          DATE,             -- Planned Dispatched Date
    pending_planned_dispatch_date  DATE,             -- Pending Planned Dispatched Date
    remark                 TEXT,                     -- Remark
    remarks_1              TEXT,                     -- Remarks 1

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (pending_order_id, line_number)
);

CREATE INDEX IF NOT EXISTS idx_pending_order_line_floor
    ON pending_order_line (pack_warehouse, floor_label, floor_person);
