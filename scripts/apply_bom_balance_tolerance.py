"""Set every BOM's batch-balance tolerance to the designed 0.1 % (opt-in, idempotent).

bom_header.allowed_balance_tolerance_pct is a FRACTION: a batch balances when
|difference| / input <= it (jc_accounting_v2.save_accounting). The column's
default is 0.001 (0.1 %: 100 g on 100 kg), but on 2026-10-23 every one of the
1,889 BOMs on the live database held 0.0001 (0.01 %: 10 g on 100 kg) — ten
times tighter than designed, which is why batches could hardly be closed. Agreed
with the business on 2026-10-23: 0.1 % of input.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
    # Preview only (DEFAULT — no writes): host, current values, what would change
    uv run python scripts/apply_bom_balance_tolerance.py

    # Apply (one transaction) to the DATABASE_URL in env/.env, or --dsn
    uv run python scripts/apply_bom_balance_tolerance.py --apply
    uv run python scripts/apply_bom_balance_tolerance.py --apply --dsn postgresql://...

Idempotent: only rows not already at the target change, so a second --apply
changes nothing. Saved accounting rows keep their old verdict until the batch's
Accounting is saved again (the next save re-judges it on the new tolerance).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from decimal import Decimal
from urllib.parse import urlparse

import asyncpg
from dotenv import load_dotenv

load_dotenv()

TARGET = Decimal("0.001")   # 0.1 % — the column default and the agreed value


async def _counts(conn) -> list[tuple[Decimal | None, int]]:
    rows = await conn.fetch(
        "SELECT allowed_balance_tolerance_pct AS v, COUNT(*) AS n "
        "FROM bom_header GROUP BY 1 ORDER BY 2 DESC"
    )
    return [(r["v"], r["n"]) for r in rows]


def _show(title: str, counts) -> None:
    print(title)
    for v, n in counts:
        label = "NULL" if v is None else f"{v} ({float(v) * 100:g} %)"
        print(f"  {label:<22} {n:>6} BOMs")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the change (default: preview only)")
    ap.add_argument("--dsn", help="database URL (default: DATABASE_URL from env/.env)")
    args = ap.parse_args()

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("No database: set DATABASE_URL or pass --dsn")
    print(f"Database host: {urlparse(dsn).hostname}")
    print(f"Target tolerance: {TARGET} ({float(TARGET) * 100:g} % of input)\n")

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        # The preview reads inside a READ ONLY transaction: it cannot write.
        async with conn.transaction(readonly=True):
            _show("Now:", await _counts(conn))
            to_change = await conn.fetchval(
                "SELECT COUNT(*) FROM bom_header "
                "WHERE allowed_balance_tolerance_pct IS DISTINCT FROM $1", TARGET,
            )
        print(f"\nBOMs that would change: {to_change}")
        if not args.apply:
            print("\nPreview only — nothing written. Re-run with --apply to change them.")
            return
        async with conn.transaction():
            status = await conn.execute(
                "UPDATE bom_header SET allowed_balance_tolerance_pct = $1 "
                "WHERE allowed_balance_tolerance_pct IS DISTINCT FROM $1", TARGET,
            )
        print(f"\nApplied: {status}")
        _show("After:", await _counts(conn))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
