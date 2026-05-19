-- ═══════════════════════════════════════════════════════════════
-- CLEANUP: Remove all test production transactions
-- PRESERVES: machine, machine_capacity, bom_*, floor_inventory,
--            inventory_batch, offgrade_reuse_rule, all_sku
-- ═══════════════════════════════════════════════════════════════

-- Job card child tables (FK dependencies first)
TRUNCATE TABLE job_card_remarks              CASCADE;
TRUNCATE TABLE job_card_loss_reconciliation  CASCADE;
TRUNCATE TABLE job_card_weight_check         CASCADE;
TRUNCATE TABLE job_card_metal_detection      CASCADE;
TRUNCATE TABLE job_card_environment          CASCADE;
TRUNCATE TABLE job_card_output               CASCADE;
TRUNCATE TABLE job_card_process_step         CASCADE;
TRUNCATE TABLE job_card_pm_indent            CASCADE;
TRUNCATE TABLE job_card_rm_indent            CASCADE;
TRUNCATE TABLE job_card_material_accounting  CASCADE;
TRUNCATE TABLE job_card                      CASCADE;

-- Production orders & plans
TRUNCATE TABLE production_order              CASCADE;
TRUNCATE TABLE production_plan_line          CASCADE;
TRUNCATE TABLE production_plan               CASCADE;

-- Fulfillment & SO revision
TRUNCATE TABLE fulfillment_floor_stock       CASCADE;
TRUNCATE TABLE so_revision_log               CASCADE;
TRUNCATE TABLE so_fulfillment                CASCADE;

-- Indents
TRUNCATE TABLE purchase_indent               CASCADE;

-- Movements & floor transactions
TRUNCATE TABLE floor_movement                CASCADE;
TRUNCATE TABLE internal_issue_note           CASCADE;

-- Quality & yield
TRUNCATE TABLE quality_inspection            CASCADE;
TRUNCATE TABLE yield_summary                 CASCADE;
TRUNCATE TABLE process_loss                  CASCADE;

-- Offgrade transactions (keep rules)
TRUNCATE TABLE offgrade_consumption          CASCADE;
TRUNCATE TABLE offgrade_inventory            CASCADE;

-- Day-end & discrepancy
TRUNCATE TABLE day_end_balance_scan_line     CASCADE;
TRUNCATE TABLE day_end_balance_scan          CASCADE;
TRUNCATE TABLE discrepancy_report            CASCADE;

-- Alerts & AI logs
TRUNCATE TABLE store_alert                   CASCADE;
TRUNCATE TABLE ai_recommendation             CASCADE;

-- Batch block history & event logs
TRUNCATE TABLE batch_block_history           CASCADE;
TRUNCATE TABLE inventory_event_log           CASCADE;
