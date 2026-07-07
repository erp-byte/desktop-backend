"""Phase 9 — SFG seam minting CLI (opt-in, idempotent, dedup-correct).

Bar-line-routed (061) and gap-promoted (058) FGs grow Create-WIP transform steps
in ``bom_process_route`` that carry NO SFG#### seam (output_code NULL). Such a
chain can never spawn a real SFG / WIP batch. This script mints (or REUSES) an
SFG#### per unseamed Create-WIP step and stamps the chain seam (output_code on
the producer, input_code on the consumer) via
``app.modules.production.services.seam_mint_service.mint_sfg_seam_for_chains``.

It is DELIBERATELY a script, NOT wired into run_master_ingest startup: minting
catalogue rows + mutating routing seams is a deliberate, audited action. Run it
AFTER apply_bar_line_override / promote_fg_master_gaps have grown the routes.

DEDUP IS CORRECT: two FGs that are the same recipe in different pack sizes (same
base_recipe + create_wip_operation) SHARE ONE minted SFG (minted=1, not 2). An
existing catalogue SFG matching the key is REUSED rather than re-minted. See the
service module docstring for the exact dedup key + the no-recipe-signature
limitation.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
    # Preview only (DEFAULT — no writes; prints minted/reused/seamed + a sample):
    uv run python scripts/mint_sfg_seam.py

    # Apply to a DB (reads DATABASE_URL from env/.env unless --dsn is given):
    uv run python scripts/mint_sfg_seam.py --apply
    uv run python scripts/mint_sfg_seam.py --apply --dsn postgresql://...

    # Limit to one FG's chain (preview or apply):
    uv run python scripts/mint_sfg_seam.py --bom-id 1234 --apply

Idempotent: a second --apply run mints 0 (every Create-WIP step already carries
its output_code, so it is skipped). The work-queue view ``sfg_seam_pending``
(migration 062) drains to EMPTY once every transform chain is seamed.

Requires migration 051 (all_sku.sfg_code). The service self-checks for it and
skips with a warning (status='schema_missing') if absent.
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

# Reuse the live service (same dedup + mint + seam-stamp logic the tests exercise).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.modules.production.services.seam_mint_service import (  # noqa: E402
    mint_sfg_seam_for_chains,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("mint_sfg_seam")


def _print_report(result: dict, *, sample: int = 10) -> None:
    if result.get("status") == "schema_missing":
        logger.error(
            "all_sku.sfg_code missing — run migration 051 first:\n"
            "    uv run python scripts/migrate.py   (or psql -f the 051 migration)"
        )
        return
    logger.info(
        "Chains with unseamed Create-WIP steps: %d  |  minted=%d  reused=%d  steps_seamed=%d",
        result["chains"], result["minted"], result["reused"], result["steps_seamed"],
    )
    for s in result.get("sample", [])[:sample]:
        logger.info(
            "  bom_id=%s step=%s  '%s'  [%s] op=%s  -> %s (%s)  consumer_step=%s",
            s["bom_id"], s["step_number"], s["fg_sku_name"], s["base_recipe"],
            s["operation"], s["sfg_code"], s["origin"], s["consumer_step"],
        )


async def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true",
                    help="apply to the DB (default is a dry-run preview)")
    ap.add_argument("--dsn", default=None,
                    help="Postgres DSN (defaults to DATABASE_URL from env/.env)")
    ap.add_argument("--bom-id", type=int, default=None,
                    help="limit minting to a single bom_id's chain")
    ap.add_argument("--sample", type=int, default=10,
                    help="how many seamed steps to print (default 10)")
    args = ap.parse_args()

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("No DSN: pass --dsn or set DATABASE_URL")
        sys.exit(1)

    conn = await asyncpg.connect(dsn, statement_cache_size=0)  # pooler-safe
    try:
        if not args.apply:
            async with conn.transaction():
                # next_sfg_code (used by the dry-run dedup path indirectly) and the
                # service's preconditions want an outer txn; a dry run writes nothing.
                result = await mint_sfg_seam_for_chains(
                    conn, dry_run=True, only_bom_id=args.bom_id,
                )
            _print_report(result, sample=args.sample)
            if result.get("status") == "ok":
                logger.info(
                    "DRY RUN — no writes. Re-run with --apply to mint/reuse the "
                    "%d SFG(s) and seam %d step(s).",
                    result["minted"], result["steps_seamed"],
                )
        else:
            async with conn.transaction():
                result = await mint_sfg_seam_for_chains(
                    conn, dry_run=False, only_bom_id=args.bom_id,
                )
            _print_report(result, sample=args.sample)
            if result.get("status") == "ok":
                logger.info(
                    "APPLIED: %d SFG(s) minted, %d reused, %d step(s) seamed "
                    "(producer output_code + consumer input_code stamped).",
                    result["minted"], result["reused"], result["steps_seamed"],
                )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
