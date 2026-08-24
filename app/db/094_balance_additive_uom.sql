-- 094_balance_additive_uom — a unit of measure on the balance-material and
-- additive lines.
--
-- WHY THIS EXISTS
-- Both tables record their quantity in a column literally named `qty_kg` and
-- carry no unit alongside it. That is fine while every line is a weight, and
-- wrong the moment a PACKAGING line appears: a zipper pouch is counted in
-- pieces, so "0.00 kg of PM24-Vedaka Purple Zipper Pouch" is not a small number,
-- it is a category error. The Accounting Summary now shows one row per RM AND
-- PM, which is what surfaced it — the two sibling tables it reads alongside,
-- job_card_material_consumption_v2 and job_card_byproducts_v2, have carried a
-- `uom` column all along, so these two were the odd ones out.
--
-- WHAT IT DOES
--   1. uom TEXT on job_card_balance_material_v2 and
--      job_card_additive_consumption_v2.
--   2. Stamps existing rows 'KGS'.
--
-- The backfill is not a guess. The quantity column on both tables IS `qty_kg`,
-- and every writer to date has put kilograms in it, so 'KGS' is what those rows
-- have always meant — this records the unit that was previously implicit rather
-- than inventing one. The column is left NULLABLE on purpose: a NULL from a
-- future writer that forgot to send one reads as "unit unknown", which the UI
-- can show honestly, whereas a NOT NULL DEFAULT 'KGS' would silently relabel a
-- pieces quantity as kilograms — the exact bug this migration exists to end.
--
-- ⚠ DEPLOY ORDER — this bit matters, and we learned it the hard way.
-- 092 shipped in the same commit as the code that read its columns, the deploy
-- pulled the code without running the migrator, and every
-- GET /job-cards-v2/{id} returned 500 with
-- `column "deleted_at" does not exist`. This file has the same shape.
--
-- RUN THIS BEFORE DEPLOYING THE BACKEND THAT WRITES `uom`.
--
-- The blast radius is narrower than 092's, because the CRUD read path uses
-- SELECT * (services/jc_accounting_crud.py:259) — reads keep working without
-- this migration, they simply return no `uom` key. It is the WRITE path that
-- names the column explicitly (the `values` tuples in _BALANCE / _ADDITIVES), so
-- if the code lands first the symptom is a failing SAVE, not a failing read.
--
-- ⚠ LOCKING: ALTER TABLE ... ADD COLUMN with no DEFAULT is a catalog-only change
-- in PG 11+ — no table rewrite. Both tables are small (hundreds of rows), so the
-- two UPDATEs are sub-second.
--
-- Idempotent — safe to re-run. The UPDATEs are guarded on uom IS NULL, so a
-- re-run cannot overwrite a unit a writer has since set (e.g. 'PCS' on a
-- packaging line).

-- ── 1. The column ───────────────────────────────────────────────────────────
ALTER TABLE job_card_balance_material_v2     ADD COLUMN IF NOT EXISTS uom TEXT;
ALTER TABLE job_card_additive_consumption_v2 ADD COLUMN IF NOT EXISTS uom TEXT;

-- ── 2. Record the unit those rows already meant ─────────────────────────────
UPDATE job_card_balance_material_v2
   SET uom = 'KGS'
 WHERE uom IS NULL;

UPDATE job_card_additive_consumption_v2
   SET uom = 'KGS'
 WHERE uom IS NULL;
