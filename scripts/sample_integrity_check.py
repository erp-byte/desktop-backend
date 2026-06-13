"""Nightly sample integrity check (checklist A11 / spec §9.9).

    python scripts/sample_integrity_check.py

Reports cross-table drift for the sample module and raises a store_alert per
finding (alert_type 'sample_integrity_drift', target_team 'stores'). A clean
dataset prints "0 drift". Reads DATABASE_URL from environment or .env. Wire to
cron / a scheduled task to run nightly.
"""
import asyncio
import logging
import os
import sys

import asyncpg
from dotenv import load_dotenv

# Make `app` importable when run from the repo root or scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modules.sample.services.integrity_service import run_integrity_check  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("sample_integrity")


async def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)
    conn = await asyncpg.connect(database_url)
    try:
        drifts = await run_integrity_check(conn, emit=True)
        if not drifts:
            logger.info("0 drift — sample dataset is consistent.")
        else:
            logger.warning("%d drift finding(s):", len(drifts))
            for d in drifts:
                logger.warning("  %s [%s] %s", d["request_id"], d["check"], d["detail"])
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
