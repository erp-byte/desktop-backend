-- Production Planning Module migrations (v1)
-- Idempotent ALTER TABLE statements go here as schema evolves.
-- Pattern: DO $$ BEGIN ... EXCEPTION WHEN ... END $$;

-- Migration 1: Add FG Master columns to bom_header
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bom_header' AND column_name='sub_group') THEN
        ALTER TABLE bom_header ADD COLUMN sub_group TEXT;
        ALTER TABLE bom_header ADD COLUMN process_category TEXT;
        ALTER TABLE bom_header ADD COLUMN business_unit TEXT;
        ALTER TABLE bom_header ADD COLUMN factory TEXT;
        ALTER TABLE bom_header ADD COLUMN floors TEXT[];
        ALTER TABLE bom_header ADD COLUMN machines TEXT[];
        ALTER TABLE bom_header ADD COLUMN shelf_life_days INT;
        ALTER TABLE bom_header ADD COLUMN gst_rate NUMERIC(5,3);
        ALTER TABLE bom_header ADD COLUMN hsn_sac TEXT;
        ALTER TABLE bom_header ADD COLUMN inventory_group TEXT;
        ALTER TABLE bom_header ADD COLUMN customer_code TEXT;
    END IF;
END
$$;

-- Migration 2: Add enrichment columns to bom_line
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bom_line' AND column_name='unit_rate_inr') THEN
        ALTER TABLE bom_line ADD COLUMN unit_rate_inr NUMERIC(15,3);
        ALTER TABLE bom_line ADD COLUMN process_stage TEXT;
    END IF;
END
$$;

-- Migration 3: Add allocation column to machine
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='machine' AND column_name='allocation') THEN
        ALTER TABLE machine ADD COLUMN allocation TEXT NOT NULL DEFAULT 'idle';
    END IF;
END
$$;

-- Migration 4: Add PDF fields to job_card (Section 1 & 3 of CFC/PRD/JC/V3.0)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='job_card' AND column_name='sales_order_ref') THEN
        ALTER TABLE job_card ADD COLUMN sales_order_ref TEXT;
        ALTER TABLE job_card ADD COLUMN article_code TEXT;
        ALTER TABLE job_card ADD COLUMN mrp NUMERIC(15,3);
        ALTER TABLE job_card ADD COLUMN ean TEXT;
        ALTER TABLE job_card ADD COLUMN bu TEXT;
        ALTER TABLE job_card ADD COLUMN fumigation BOOLEAN DEFAULT FALSE;
        ALTER TABLE job_card ADD COLUMN metal_detector_used BOOLEAN DEFAULT FALSE;
        ALTER TABLE job_card ADD COLUMN roasting_pasteurization BOOLEAN DEFAULT FALSE;
        ALTER TABLE job_card ADD COLUMN control_sample_gm NUMERIC(10,2);
        ALTER TABLE job_card ADD COLUMN magnets_used BOOLEAN DEFAULT FALSE;
    END IF;
END
$$;

-- Migration 5: Add Annexure A/B fields to job_card_metal_detection
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='job_card_metal_detection' AND column_name='seal_check') THEN
        ALTER TABLE job_card_metal_detection ADD COLUMN seal_check BOOLEAN;
        ALTER TABLE job_card_metal_detection ADD COLUMN seal_failed_units INT DEFAULT 0;
        ALTER TABLE job_card_metal_detection ADD COLUMN wt_check BOOLEAN;
        ALTER TABLE job_card_metal_detection ADD COLUMN wt_failed_units INT DEFAULT 0;
        ALTER TABLE job_card_metal_detection ADD COLUMN dough_temp_c NUMERIC(10,2);
        ALTER TABLE job_card_metal_detection ADD COLUMN oven_temp_c NUMERIC(10,2);
        ALTER TABLE job_card_metal_detection ADD COLUMN baking_temp_c NUMERIC(10,2);
    END IF;
END
$$;

-- Migration 6: Add Annexure B fields to job_card_weight_check
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='job_card_weight_check' AND column_name='target_wt_g') THEN
        ALTER TABLE job_card_weight_check ADD COLUMN target_wt_g NUMERIC(10,2);
        ALTER TABLE job_card_weight_check ADD COLUMN tolerance_g NUMERIC(10,2);
        ALTER TABLE job_card_weight_check ADD COLUMN accept_range_min NUMERIC(10,2);
        ALTER TABLE job_card_weight_check ADD COLUMN accept_range_max NUMERIC(10,2);
    END IF;
END
$$;

-- Migration 7: Create job_card_sign_off table (Section 6 + Annexure page 4)
CREATE TABLE IF NOT EXISTS job_card_sign_off (
    sign_off_id     SERIAL PRIMARY KEY,
    job_card_id     INT NOT NULL REFERENCES job_card(job_card_id),
    sign_off_type   TEXT NOT NULL,
    name            TEXT,
    signed_at       TIMESTAMPTZ,
    UNIQUE (job_card_id, sign_off_type)
);

CREATE INDEX IF NOT EXISTS idx_sign_off_jc ON job_card_sign_off(job_card_id);
