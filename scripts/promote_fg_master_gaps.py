"""Slice-7 data-quality close-out — promote reconciliation-gap articles into FG Master.

Closes the two routing gaps the SFG/Job-Card playbook (SLICE 7) targets so that
more SKUs can spawn routed job cards:

    * 403  CATALOG_FG_NOT_IN_FG_MASTER  — an all_sku FG with no Process Category,
           so it has no bom_process_route and never enters the SFG/job-card build.
    * 238  BOM_PARENT_NO_ROUTING        — a BOM parent (recipe exists) with no
           Process Category / routing.

(Counts are the gap_flags substring-containment counts in the source CSV
 docs/flows/Candor_SFG_JobCard_Integration_2026-06-14/Article_Master_FINAL.csv;
 the two gap sets overlap by 163 rows, so their UNION is 450 distinct articles.)

"Promote into FG Master with a Process Category" maps onto the data model exactly
as master_ingest.ingest_fg_master does for the FG_Master_Completion.xlsx rows:
upsert a bom_header carrying `process_category`, then derive its bom_process_route
steps by splitting the category on '+'. There is NO separate fg_master table — FG
Master == the bom_header rows that carry process_category + their derived routes.

────────────────────────────────────────────────────────────────────────────────
SCOPE — what this promotes, and what it deliberately does NOT
────────────────────────────────────────────────────────────────────────────────
The ONLY business rule the playbook gives is:  dates articles ⇒ "Sorting + Packing".
None of the 450 gap articles carries a process_category in any source, and the
authoritative routing plug (JC_Routing_Plug.csv) covers 0 of them — they are gaps
precisely because no routing source exists. So this script promotes ONLY the
cleanly-ruleable subset and reports the rest for a business decision:

  PROMOTED  — catalogued (in_all_sku=TRUE) date articles in the gap union whose
              NAME carries no transform signal (no Bar/Paste/Spread/Syrup/Powder/
              Stuffed/Coated/Choco/Bites/…). These are whole dates in retail packs:
              "Sorting + Packing" is correct for them.   (~108 articles)

  SKIPPED (reported, NOT promoted):
    * transform-signal date products (Date Paste / Syrup / Bar / Bites / Coated /
      Powder …) — they need a real multi-step routing (bar-forming, enrobing,
      milling), NOT Sorting + Packing.                              (~32)
    * uncatalogued date articles (in_all_sku=FALSE) — these also carry
      ARTICLE_NOT_IN_CATALOG (a different gap, G4) and several are Date Bars;
      catalogue + route them deliberately.                          (~9)
    * the ~301 NON-date gap articles (trail mixes, coated peanuts, roasted /
      chocolate nuts, …) — no business rule and no routing source. GUESSING
      "Sorting + Packing" for a roasted/coated/flavoured product is WRONG.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
    # Preview only (DEFAULT — no writes; prints the full promote / skip report):
    uv run python scripts/promote_fg_master_gaps.py

    # Apply to a DB (reads DATABASE_URL from env/.env unless --dsn is given):
    uv run python scripts/promote_fg_master_gaps.py --apply
    uv run python scripts/promote_fg_master_gaps.py --apply --dsn postgresql://...

Idempotent: a second --apply run is a near no-op (bom_header upsert is ON CONFLICT
DO NOTHING on the active-FG unique index; routes are ON CONFLICT (bom_id,
step_number) DO NOTHING). Re-running only stamps promoted_from_gap on rows it
covers and fills any missing route step.

Requires migration 058 (bom_header.promoted_from_gap). Self-checks for it and
exits with guidance if absent.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import re
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

# Reuse the SAME normalisation the live ingest uses, so a name that already lives
# in all_sku / bom_header (possibly NBSP/mojibake-bearing) matches the CSV name.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.modules.production.services.master_ingest import _split_process_category  # noqa: E402

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("promote_fg_master_gaps")

# ── source data ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
ARTICLE_MASTER_CSV = (
    _REPO_ROOT / "docs" / "flows" / "Candor_SFG_JobCard_Integration_2026-06-14"
    / "Article_Master_FINAL.csv"
)

# The two gap flags this task clears (substring match on the multi-valued,
# '; '-separated gap_flags column).
GAP_CATALOG = "CATALOG_FG_NOT_IN_FG_MASTER"
GAP_BOM = "BOM_PARENT_NO_ROUTING"

# The one process category the playbook authorises for dates.
DATES_PROCESS_CATEGORY = "Sorting + Packing"

# Name signals that a "date" article is actually a transformed product (needs a
# real routing — bar-forming / enrobing / milling), so it must NOT be auto-routed
# as plain Sorting + Packing. Word-boundary so "Update" etc. can't false-match.
_TRANSFORM_SIGNAL = re.compile(
    r"\b(bar|paste|spread|syrup|powder|stuffed|coated|choco|chocolate|"
    r"roast|roasted|ball|bites?|filled|enrob|enrobed)\b",
    re.IGNORECASE,
)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes", "y", "t")


def _is_date_article(name: str) -> bool:
    return "date" in (name or "").lower()


def _csv_entity(val: str | None) -> str | None:
    """Map the CSV catalog_entity to a bom_header.entity value (CHECK IN
    ('cfpl','cdpl')). 'cfpl+cdpl' and '' → None (NULL passes the CHECK; the row is
    a generic, entity-agnostic header)."""
    v = (val or "").strip().lower()
    return v if v in ("cfpl", "cdpl") else None


# ── classification of the gap union ─────────────────────────────────────────

def load_gap_union(csv_path: Path) -> list[dict]:
    """Return the rows whose gap_flags contain EITHER target gap (the 450-row union)."""
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [
        r for r in rows
        if GAP_CATALOG in (r.get("gap_flags") or "") or GAP_BOM in (r.get("gap_flags") or "")
    ]


def classify(union: list[dict]) -> dict[str, list[dict]]:
    """Partition the union into promote / skip buckets with a reason each."""
    buckets: dict[str, list[dict]] = {
        "promote": [],          # catalogued plain dates → Sorting + Packing
        "skip_transform_date": [],   # date products that need real routing
        "skip_uncatalogued_date": [],  # date but not in all_sku (also G4)
        "skip_non_date": [],    # everything else — no rule, no routing source
    }
    for r in union:
        name = r["article"]
        if not _is_date_article(name):
            buckets["skip_non_date"].append(r)
        elif not _truthy(r.get("in_all_sku")):
            buckets["skip_uncatalogued_date"].append(r)
        elif _TRANSFORM_SIGNAL.search(name):
            buckets["skip_transform_date"].append(r)
        else:
            buckets["promote"].append(r)
    return buckets


# ── DB promotion (mirrors master_ingest.ingest_fg_master) ───────────────────

async def _has_promoted_column(conn) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'bom_header' AND column_name = 'promoted_from_gap'
        )
        """
    ))


async def promote_one(conn, article: str, entity: str | None,
                      process_category: str = DATES_PROCESS_CATEGORY) -> dict:
    """Upsert one bom_header (carrying ``process_category``) + derive its
    bom_process_route steps, stamping promoted_from_gap. Returns per-row outcome.

    The canonical gap-promotion primitive — SHARED with the Routing-Gap feature
    (app/modules/production/services/routing_gap_service.py imports it) so the
    online "apply assignments" path and this one-off script derive routes via the
    EXACT same upsert + split-on-'+' logic. ``process_category`` defaults to the
    Slice-7 dates rule so the script's call sites are unchanged.

    Mirrors ingest_fg_master: INSERT ... ON CONFLICT DO NOTHING (the active-FG
    partial unique index uq_bom_header_active_fg makes this a true upsert), then
    fetch the bom_id of the existing/new header, then route from the category.
    """
    inserted = await conn.fetchval(
        """
        INSERT INTO bom_header (fg_sku_name, customer_name, entity,
                                process_category, promoted_from_gap)
        VALUES ($1, NULL, $2, $3, TRUE)
        ON CONFLICT DO NOTHING
        RETURNING bom_id
        """,
        article, entity, process_category,
    )

    net_new = inserted is not None
    if net_new:
        bom_id = inserted
    else:
        # Pre-existing header (the BOM_PARENT_NO_ROUTING case, or a re-run): route
        # it in place and mark it promoted. The INSERT conflicts on
        # uq_bom_header_active_fg (fg_sku_name WHERE is_active) which ignores
        # customer_name — so the conflicting row may be a CUSTOMER-specific header.
        # Prefer the generic (customer-less) one, else fall back to whatever active
        # header carries the name (so a customer-only collision still routes the
        # article instead of dead-ending at 'no_header').
        bom_id = await conn.fetchval(
            "SELECT bom_id FROM bom_header "
            "WHERE fg_sku_name = $1 "
            "ORDER BY (customer_name IS NULL) DESC, is_active DESC, version DESC LIMIT 1",
            article,
        )
        if bom_id is None:
            return {"article": article, "status": "no_header", "bom_id": None}
        # Set the category too (only if still empty — never clobber a real one)
        # so the article actually leaves the "no Process Category" gap; flag it.
        await conn.execute(
            """
            UPDATE bom_header
               SET promoted_from_gap = TRUE,
                   process_category  = COALESCE(NULLIF(process_category, ''), $2)
             WHERE bom_id = $1
            """,
            bom_id, process_category,
        )

    # Derive the process route from the category (split on '+'), exactly like
    # ingest_fg_master. Idempotent on (bom_id, step_number).
    steps = _split_process_category(process_category)
    routes_added = 0
    for step_num, step_name in enumerate(steps, 1):
        stage = step_name.lower().replace(" ", "_")
        status = await conn.execute(
            """
            INSERT INTO bom_process_route (bom_id, step_number, process_name, stage)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (bom_id, step_number) DO NOTHING
            """,
            bom_id, step_num, step_name, stage,
        )
        # Robust command-tag parse: "INSERT 0 1" -> inserted, "INSERT 0 0" -> conflict
        # skip. endswith("1") would also (wrongly) match "INSERT 0 11" if ever batched.
        if status.rsplit(" ", 1)[-1] != "0":
            routes_added += 1

    return {
        "article": article,
        "status": "created" if net_new else "routed",
        "bom_id": bom_id,
        "net_new": net_new,
        "routes_added": routes_added,
    }


async def apply_promotion(dsn: str, to_promote: list[dict]) -> dict:
    conn = await asyncpg.connect(dsn, statement_cache_size=0)  # pooler-safe
    try:
        if not await _has_promoted_column(conn):
            logger.error(
                "bom_header.promoted_from_gap missing — run migration 058 first:\n"
                "    uv run python scripts/migrate.py   (or psql -f app/db/058_fg_master_gap_promotion.sql)"
            )
            sys.exit(2)

        created = routed = no_header = total_routes = 0
        net_new_ids: list[int] = []
        async with conn.transaction():
            for r in to_promote:
                entity = _csv_entity(r.get("catalog_entity"))
                out = await promote_one(conn, r["article"], entity)
                if out["status"] == "created":
                    created += 1
                    net_new_ids.append(out["bom_id"])
                elif out["status"] == "routed":
                    routed += 1
                elif out["status"] == "no_header":
                    no_header += 1
                    logger.warning("No generic header found for routed-gap article: %s", r["article"])
                total_routes += out.get("routes_added", 0)
    finally:
        await conn.close()

    return {
        "created_headers": created,
        "routed_existing": routed,
        "no_header_skipped": no_header,
        "routes_added": total_routes,
        "net_new_bom_ids": net_new_ids,
    }


# ── report ──────────────────────────────────────────────────────────────────

def print_report(union: list[dict], buckets: dict[str, list[dict]]) -> None:
    n_cat = sum(1 for r in union if GAP_CATALOG in (r.get("gap_flags") or ""))
    n_bom = sum(1 for r in union if GAP_BOM in (r.get("gap_flags") or ""))
    logger.info("Source: %s", ARTICLE_MASTER_CSV)
    logger.info("Gap union: %d articles  (%s=%d, %s=%d, overlap=%d)",
                len(union), GAP_CATALOG, n_cat, GAP_BOM, n_bom, n_cat + n_bom - len(union))
    logger.info("  PROMOTE (catalogued plain dates → '%s'): %d",
                DATES_PROCESS_CATEGORY, len(buckets["promote"]))
    logger.info("  SKIP transform-signal date products (need real routing): %d",
                len(buckets["skip_transform_date"]))
    logger.info("  SKIP uncatalogued date articles (also ARTICLE_NOT_IN_CATALOG): %d",
                len(buckets["skip_uncatalogued_date"]))
    logger.info("  SKIP non-date gap articles (no rule, no routing source): %d",
                len(buckets["skip_non_date"]))


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="apply to the DB (default is a dry-run preview)")
    ap.add_argument("--dsn", default=None,
                    help="Postgres DSN (defaults to DATABASE_URL from env/.env)")
    ap.add_argument("--csv", default=str(ARTICLE_MASTER_CSV),
                    help="override the Article_Master_FINAL.csv path")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        logger.error("Source CSV not found: %s", csv_path)
        sys.exit(1)

    union = load_gap_union(csv_path)
    buckets = classify(union)
    print_report(union, buckets)

    if not args.apply:
        logger.info("DRY RUN — no writes. Re-run with --apply to promote the %d articles.",
                    len(buckets["promote"]))
        # Surface the actionable lists for the reviewer.
        logger.info("First 10 PROMOTE: %s",
                    [r["article"] for r in buckets["promote"][:10]])
        logger.info("First 10 SKIP non-date (decision needed): %s",
                    [r["article"] for r in buckets["skip_non_date"][:10]])
        return

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("No DSN: pass --dsn or set DATABASE_URL")
        sys.exit(1)

    result = await apply_promotion(dsn, buckets["promote"])
    logger.info(
        "APPLIED: %d new headers, %d existing headers routed, %d routes added "
        "(%d had no generic header). Net-new bom_ids: %s",
        result["created_headers"], result["routed_existing"],
        result["routes_added"], result["no_header_skipped"],
        result["net_new_bom_ids"],
    )


if __name__ == "__main__":
    asyncio.run(main())
