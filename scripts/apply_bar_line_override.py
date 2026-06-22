"""G4 — Bar-Line Process override CLI (opt-in, idempotent, audited).

84 bar-line value-added FGs carry a RICHER routing in
``bom_header.bar_line_process`` than the one derived from ``process_category``,
but it is unused. This script (re)derives those FGs' ``bom_process_route`` from
``bar_line_process`` via app.modules.production.services.bar_line_service.

It is DELIBERATELY a script, NOT wired into run_master_ingest startup: it
OVERRIDES routing that already works, so it must be a conscious, audited action.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
    # Preview only (DEFAULT — no writes; prints how many FGs + sample routes):
    uv run python scripts/apply_bar_line_override.py

    # Apply to a DB (reads DATABASE_URL from env/.env unless --dsn is given):
    uv run python scripts/apply_bar_line_override.py --apply
    uv run python scripts/apply_bar_line_override.py --apply --dsn postgresql://...

    # Limit to one FG (preview or apply):
    uv run python scripts/apply_bar_line_override.py --bom-id 1234 --apply

Idempotent: a second --apply run re-derives the SAME steps and converges
(bom_process_route is DELETEd then re-INSERTed deterministically;
bar_line_routed is already TRUE).

Requires migration 061 (bom_header.bar_line_routed). The service self-checks for
it and skips with a warning if absent.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

# Reuse the live service (same split + classify + REPLACE logic the app uses).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.modules.production.services.bar_line_service import (  # noqa: E402
    apply_bar_line_override,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("apply_bar_line_override")


def _print_report(result: dict, *, sample: int = 5) -> None:
    if result.get("status") == "schema_missing":
        logger.error(
            "bom_header.bar_line_routed missing — run migration 061 first:\n"
            "    uv run python scripts/migrate.py   (or psql -f the 061 migration)"
        )
        return
    logger.info("Bar-line FGs with a non-empty bar_line_process: %d", result["headers"])
    fgs = result.get("fgs", [])
    for fg in fgs[:sample]:
        steps = " -> ".join(
            f"{d['process_name']}[{d['stage_bucket'] or 'UNCLASSIFIED'}]"
            for d in fg["derived"]
        )
        logger.info("  bom_id=%s  %s", fg["bom_id"], fg["fg_sku_name"])
        logger.info("      %d steps: %s", fg["steps"], steps)
    if len(fgs) > sample:
        logger.info("  ... and %d more FGs", len(fgs) - sample)


async def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true",
                    help="apply to the DB (default is a dry-run preview)")
    ap.add_argument("--dsn", default=None,
                    help="Postgres DSN (defaults to DATABASE_URL from env/.env)")
    ap.add_argument("--bom-id", type=int, default=None,
                    help="limit the override to a single bom_id")
    ap.add_argument("--sample", type=int, default=5,
                    help="how many derived routes to print (default 5)")
    ap.add_argument("--force", action="store_true",
                    help="re-route headers already bar_line_routed=TRUE "
                         "(WARNING: clears any minted SFG seam columns — re-run "
                         "mint_sfg_seam afterwards). Default skips them.")
    args = ap.parse_args()

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("No DSN: pass --dsn or set DATABASE_URL")
        sys.exit(1)

    conn = await asyncpg.connect(dsn, statement_cache_size=0)  # pooler-safe
    try:
        if not args.apply:
            result = await apply_bar_line_override(
                conn, dry_run=True, only_bom_id=args.bom_id, force=args.force,
            )
            _print_report(result, sample=args.sample)
            if result.get("status") == "ok":
                logger.info(
                    "DRY RUN — no writes. Re-run with --apply to (re)route the "
                    "%d FG(s) from bar_line_process.", result["headers"],
                )
        else:
            async with conn.transaction():
                result = await apply_bar_line_override(
                    conn, dry_run=False, only_bom_id=args.bom_id, force=args.force,
                )
            _print_report(result, sample=args.sample)
            if result.get("status") == "ok":
                logger.info(
                    "APPLIED: %d FG(s) routed from bar_line_process, %d steps "
                    "written (%d skipped-empty). bar_line_routed=TRUE set on each.",
                    result["routed"], result["steps_written"],
                    result["skipped_empty"],
                )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
