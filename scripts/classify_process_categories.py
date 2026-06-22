"""One-off: classify the PROCESS CATEGORY of pre-existing routing steps.

Backfills ``bom_process_route.practical_operation`` + ``bom_process_route.stage_bucket``
(the "process category" — Slice 2 / Phase 2) for routing steps that were loaded
before classification existed and are therefore still UNCLASSIFIED
(``stage_bucket IS NULL``).

It is the standalone, reviewable wrapper around the SAME logic the app runs at
startup — ``master_ingest.ingest_process_category_rules`` — so this script and the
live ingest can never drift:

  * the WRITE path calls ``ingest_process_category_rules(conn, full=...)`` verbatim
    (it takes the PROCESS_CLASS_LOCK advisory lock and UPDATEs in one statement);
  * the DRY-RUN path replays the SAME selection + ``classify_route_steps`` read-only
    to show exactly what WOULD change, and writes nothing.

WHY a script (not pure SQL): the classifier is combine-aware — a route with
Roasting AND a seasoning token relabels the Roasting step to the combined
"Roast & Flavour/Salt", so it must see each route's FULL ordered step set. That
can't be expressed faithfully in a per-row SQL UPDATE; the rules live in Python
(``classify_route_steps``), exactly per the SFG track's "data lives in Python,
schema in SQL" convention.

────────────────────────────────────────────────────────────────────────────────
SCOPE
────────────────────────────────────────────────────────────────────────────────
DEFAULT (incremental): only routes (bom_ids) that have at least one step with
``stage_bucket IS NULL`` are touched — but ALL steps of each such route are
re-classified so the combine rule is correct. Already-fully-classified routes are
left untouched. This matches "load the process category for the pre-existing
processes that are still unclassified".

``--full``: re-label EVERY route's every step (use only to repair a mis-labelled
set; idempotent — re-writes the same values for unchanged rows).

Unknown tokens are deliberately left NULL (never guessed) — the dry-run reports
the distinct unknown process names so you can extend the classifier / stage_catalog
if any remain.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
    # Preview only (DEFAULT — no writes; prints the full would-change report):
    uv run python scripts/classify_process_categories.py

    # Preview re-labelling ALL routes:
    uv run python scripts/classify_process_categories.py --full

    # Apply to a DB (reads DATABASE_URL from env/.env unless --dsn is given):
    uv run python scripts/classify_process_categories.py --apply
    uv run python scripts/classify_process_categories.py --apply --dsn postgresql://...

Idempotent: a second --apply run is a no-op once every classifiable step carries a
stage_bucket. Requires migration 050 (the practical_operation/stage_bucket columns);
self-checks for it and exits with guidance if absent.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

# Reuse the EXACT classifier + write primitive the live ingest uses.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.modules.production.services.master_ingest import (  # noqa: E402
    classify_route_steps,
    ingest_process_category_rules,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("classify_process_categories")


async def _has_category_columns(conn) -> bool:
    """True when bom_process_route carries the Slice-2 category columns (mig 050)."""
    return bool(await conn.fetchval(
        """
        SELECT COUNT(*) = 2 FROM information_schema.columns
         WHERE table_name = 'bom_process_route'
           AND column_name IN ('practical_operation', 'stage_bucket')
        """
    ))


async def _fetch_target_steps(conn, *, full: bool) -> list[asyncpg.Record]:
    """The rows ingest_process_category_rules would consider — mirrored read-only.

    full=True  -> every route, every step.
    full=False -> only routes that have >=1 NULL-stage_bucket step, but ALL their
                  steps (so the combine rule sees the full ordered set).
    """
    if full:
        return await conn.fetch(
            """
            SELECT bom_id, step_number, process_name,
                   practical_operation, stage_bucket
              FROM bom_process_route
             ORDER BY bom_id, step_number
            """
        )
    return await conn.fetch(
        """
        SELECT bom_id, step_number, process_name,
               practical_operation, stage_bucket
          FROM bom_process_route
         WHERE bom_id IN (
               SELECT DISTINCT bom_id FROM bom_process_route WHERE stage_bucket IS NULL)
         ORDER BY bom_id, step_number
        """
    )


async def _global_counts(conn) -> dict:
    row = await conn.fetchrow(
        """
        SELECT COUNT(*)                                       AS steps,
               COUNT(*) FILTER (WHERE stage_bucket IS NULL)   AS unclassified,
               COUNT(DISTINCT bom_id)                         AS routes,
               COUNT(DISTINCT bom_id) FILTER (WHERE stage_bucket IS NULL) AS routes_with_null
          FROM bom_process_route
        """
    )
    return dict(row)


def _preview(rows: list[asyncpg.Record]) -> dict:
    """Replay the classifier per route; tally what would change. No writes."""
    by_bom: dict[int, list[asyncpg.Record]] = defaultdict(list)
    for r in rows:
        by_bom[r["bom_id"]].append(r)

    net_new = 0          # was NULL -> now classified
    still_null = 0       # was NULL -> still NULL (unknown token)
    changed_value = 0    # had a value -> classifier yields a DIFFERENT value
    rewrite_same = 0     # had a value -> same value (idempotent re-write)
    downgrades = 0       # had a stage_bucket -> classifier yields NULL (DESTRUCTIVE)
    bucket_dist: Counter = Counter()
    unknown_tokens: Counter = Counter()
    sample_new: list[tuple] = []
    sample_changed: list[tuple] = []
    sample_downgrade: list[tuple] = []

    for bom_id, steps in by_bom.items():
        cls = classify_route_steps([s["process_name"] for s in steps])
        for s, c in zip(steps, cls):
            new_bucket = c["stage_bucket"]
            new_op = c["practical_operation"]
            cur_bucket = s["stage_bucket"]
            bucket_dist[new_bucket or "(unclassified)"] += 1
            if cur_bucket is None:
                if new_bucket is None:
                    still_null += 1
                    unknown_tokens[s["process_name"]] += 1
                else:
                    net_new += 1
                    if len(sample_new) < 15:
                        sample_new.append(
                            (bom_id, s["step_number"], s["process_name"], new_op, new_bucket))
            else:
                if new_bucket == cur_bucket and new_op == s["practical_operation"]:
                    rewrite_same += 1
                else:
                    changed_value += 1
                    if new_bucket is None:  # classified -> NULL: would erase a real category
                        downgrades += 1
                        if len(sample_downgrade) < 15:
                            sample_downgrade.append(
                                (bom_id, s["step_number"], s["process_name"],
                                 f"{s['practical_operation']}/{cur_bucket}"))
                    if len(sample_changed) < 15:
                        sample_changed.append(
                            (bom_id, s["step_number"], s["process_name"],
                             f"{s['practical_operation']}/{cur_bucket}",
                             f"{new_op}/{new_bucket}"))

    return {
        "routes": len(by_bom),
        "steps": len(rows),
        "net_new": net_new,
        "still_null": still_null,
        "changed_value": changed_value,
        "rewrite_same": rewrite_same,
        "downgrades": downgrades,
        "bucket_dist": dict(bucket_dist),
        "unknown_tokens": unknown_tokens.most_common(),
        "sample_new": sample_new,
        "sample_changed": sample_changed,
        "sample_downgrade": sample_downgrade,
    }


def _print_preview(scope: str, before: dict, p: dict) -> None:
    logger.info("Mode: %s", scope)
    logger.info(
        "Current state: %d routing steps over %d routes; %d steps unclassified "
        "(stage_bucket IS NULL) across %d routes.",
        before["steps"], before["routes"], before["unclassified"], before["routes_with_null"],
    )
    logger.info("Would touch: %d routes, %d steps.", p["routes"], p["steps"])
    logger.info("  net-new classified (was NULL -> set): %d", p["net_new"])
    logger.info("  remain UNCLASSIFIED (unknown token, left NULL): %d", p["still_null"])
    logger.info("  re-written identically (idempotent): %d", p["rewrite_same"])
    logger.info("  value CHANGED vs current: %d", p["changed_value"])
    if p["downgrades"]:
        logger.warning(
            "  ⚠ %d step(s) would be DOWNGRADED to unclassified (currently carry a "
            "stage_bucket but the classifier yields NULL). This usually means process_name "
            "holds rich routing-plug labels (e.g. 'Create WIP: Blend & Form', 'Sorting + "
            "Packing') that classify_route_steps cannot parse — applying would ERASE real "
            "categories. --apply is BLOCKED unless --allow-downgrade is given.", p["downgrades"])
        for bom_id, step, name, old in p["sample_downgrade"]:
            logger.warning("    bom %s step %s  %r : %s -> NULL", bom_id, step, name, old)
    logger.info("  resulting stage_bucket distribution: %s", p["bucket_dist"])
    if p["sample_new"]:
        logger.info("  sample net-new (bom_id, step, process -> operation / bucket):")
        for bom_id, step, name, op, bucket in p["sample_new"]:
            logger.info("    bom %s step %s  %r -> %s / %s", bom_id, step, name, op, bucket)
    if p["sample_changed"]:
        logger.info("  sample CHANGED (bom_id, step, process : old -> new):")
        for bom_id, step, name, old, new in p["sample_changed"]:
            logger.info("    bom %s step %s  %r : %s -> %s", bom_id, step, name, old, new)
    if p["unknown_tokens"]:
        logger.info(
            "  %d distinct UNKNOWN process names stay NULL (extend classifier/stage_catalog "
            "if these need a category):", len(p["unknown_tokens"]))
        for name, n in p["unknown_tokens"][:25]:
            logger.info("    %5d x  %r", n, name)


async def run(dsn: str, *, apply: bool, full: bool, allow_downgrade: bool) -> None:
    conn = await asyncpg.connect(dsn, statement_cache_size=0)  # pooler-safe
    try:
        if not await _has_category_columns(conn):
            logger.error(
                "bom_process_route.practical_operation/stage_bucket missing — run "
                "migration 050 first:\n"
                "    uv run python scripts/migrate.py   "
                "(or psql -f app/db/050_sfg_foundation.sql)"
            )
            sys.exit(2)

        before = await _global_counts(conn)
        rows = await _fetch_target_steps(conn, full=full)
        preview = _preview(rows)
        scope = "FULL (re-label every route)" if full else "INCREMENTAL (unclassified routes only)"
        _print_preview(scope, before, preview)

        if not apply:
            logger.info(
                "DRY RUN — no writes. Re-run with --apply to classify %d net-new step(s).",
                preview["net_new"])
            return

        if preview["downgrades"] and not allow_downgrade:
            logger.error(
                "ABORT (no writes): %d already-classified step(s) would be blanked to NULL. "
                "These process_name values are not raw tokens the fallback classifier can parse "
                "(likely authoritative routing-plug labels). Re-run with --allow-downgrade ONLY "
                "if you truly intend to erase their categories.", preview["downgrades"])
            sys.exit(3)

        # WRITE via the canonical primitive (advisory-locked, single UPDATE).
        # MUST run inside an outer transaction.
        async with conn.transaction():
            result = await ingest_process_category_rules(conn, full=full)

        after = await _global_counts(conn)
        logger.info(
            "APPLIED: %d step(s) labelled (%d Create-WIP) across %d route(s).",
            result["steps_classified"], result["create_wip_steps"], result["routes"],
        )
        logger.info(
            "Post-state: %d/%d steps now unclassified (was %d). %s",
            after["unclassified"], after["steps"], before["unclassified"],
            "All classifiable steps covered."
            if after["unclassified"] == 0
            else f"{after['unclassified']} remain NULL (unknown tokens — see dry-run list).",
        )
    finally:
        await conn.close()


async def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="apply to the DB (default is a dry-run preview)")
    ap.add_argument("--full", action="store_true",
                    help="re-label EVERY route (default: only routes with an unclassified step)")
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="permit blanking already-classified steps to NULL (DESTRUCTIVE; off by default)")
    ap.add_argument("--dsn", default=None,
                    help="Postgres DSN (defaults to DATABASE_URL from env/.env)")
    args = ap.parse_args()

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("No DSN: pass --dsn or set DATABASE_URL")
        sys.exit(1)

    await run(dsn, apply=args.apply, full=args.full, allow_downgrade=args.allow_downgrade)


if __name__ == "__main__":
    asyncio.run(main())
