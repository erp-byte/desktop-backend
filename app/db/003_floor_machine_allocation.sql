-- 003_floor_machine_allocation.sql — Floor & machine allocation persistence

ALTER TABLE production_plan_line ADD COLUMN IF NOT EXISTS floor TEXT;
ALTER TABLE production_order ADD COLUMN IF NOT EXISTS machine_id INT REFERENCES machine(machine_id);
