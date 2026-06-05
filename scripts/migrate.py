"""Standalone DB migration runner.

Run before each Lambda deploy to apply schema changes:

    python scripts/migrate.py

Reads DATABASE_URL from environment or .env file.
All SQL files are idempotent (IF NOT EXISTS / ON CONFLICT).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DB_DIR = Path(__file__).parent.parent / "app" / "db"

# Applied in dependency order — schemas before their migrations
SQL_FILES = [
    DB_DIR / "schema.sql",
    DB_DIR / "migrate.sql",
    DB_DIR / "po_schema.sql",
    DB_DIR / "po_migrate.sql",
    DB_DIR / "production_schema.sql",
    DB_DIR / "production_migrate.sql",
    DB_DIR / "auth_schema.sql",
    DB_DIR / "ims_new_schema.sql",
    DB_DIR / "sap_mm_align.sql",
    DB_DIR / "001_job_card_chain.sql",
    DB_DIR / "030_vendor_history.sql",
    DB_DIR / "031_bom_bar_line_process.sql",
    DB_DIR / "seed_test_data.sql",
    # 032 backfills uom_match for SO Book uploads that landed before the
    # reconciliation switched from string-UOM equality to the kg/pack-count
    # math check. Idempotent — only touches rows where uom_match IS NULL.
    DB_DIR / "032_so_uom_recon_backfill.sql",
    # 033 stages OTPs for the WhatsApp-based self-service password reset
    # that replaced the admin-only reset path. One row per user (PK), 60s
    # TTL, deleted on successful reset.
    DB_DIR / "033_password_reset_otp.sql",
    # ── Sample Issuing module (app/db/samples/) ────────────────────────
    # 035 adds the business_head / npd_team roles + the `sample` permission
    # catalog (inventory_manager already exists from 028).
    DB_DIR / "samples" / "035_sample_roles.sql",
    # 036 builds the GENERIC gate_passes table + registers sample movement
    # types 265/266. Must run before 037 (sample tables FK gate_passes).
    DB_DIR / "samples" / "036_gate_passes.sql",
    # 037 creates the sample_* tables, seeds the approval role-map with real
    # roles, and extends material_document. Depends on 035 + 036.
    DB_DIR / "samples" / "037_sample_module.sql",
    # 038 extends the legacy job_card table (sample_requisition_id,
    # jobcard_type) and adds the sample_requisitions.linked_job_card_id FK.
    DB_DIR / "samples" / "038_job_card_sample_fk.sql",
    # 039 repairs the missing uq_jcmc_v2_jc_material unique index that
    # job_card_v2.upsert_consumption_lines' ON CONFLICT relies on (declared in
    # the orphaned 018_jc_accounting_v2.sql). De-dupes then creates the index;
    # to_regclass-guarded so it no-ops if the table isn't present yet.
    DB_DIR / "039_fix_jcmc_unique_index.sql",
    # 040 repairs the missing uq_byproducts_jc_cat_mat expression index that
    # jc_accounting_v2.save_byproducts' ON CONFLICT relies on (declared in the
    # orphaned 034_byproducts_material_attribution.sql). De-dupes, drops the
    # stale 2-col UNIQUE, then creates the index; guarded like 039.
    DB_DIR / "040_fix_byproducts_unique_index.sql",
    # 041 reconciles the remaining unique indexes declared in runner-orphaned
    # migrations (011/017/021/029/032_b11/005): idx_bom_override_v2_unique
    # (ON CONFLICT-backed, de-duped keep-latest) + the integrity partial
    # indexes (created only when data is clean, else NOTICE + skip). Guarded.
    DB_DIR / "041_reconcile_orphaned_unique_indexes.sql",
]


async def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable not set")
        sys.exit(1)

    logger.info("Connecting to database...")
    conn = await asyncpg.connect(database_url)
    try:
        for sql_file in SQL_FILES:
            if not sql_file.exists():
                logger.warning("Skipping missing file: %s", sql_file.name)
                continue
            logger.info("Applying %s ...", sql_file.name)
            sql = sql_file.read_text(encoding="utf-8")
            await conn.execute(sql)
            logger.info("  OK")
        logger.info("All migrations applied successfully.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
