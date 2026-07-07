"""One-off recovery: restore all_sku.sfg_code on item_type='sfg' rows.

WHY THIS EXISTS
---------------
Running app/db/rollback/sfg_integration_down.sql against a DB that already held
loaded SFG data does `ALTER TABLE all_sku DROP COLUMN sfg_code`, which wipes the
SFG#### business key off every item_type='sfg' row (the rows themselves remain).
Re-adding the column (forward migration 051) leaves it NULL, and the warm-path
catalogue loader (run_master_ingest) is gated `if not sfg_count:` — so it sees the
343 sfg rows and SKIPS re-linking. Result: the sfg_master view (which filters
sfg_code IS NOT NULL) returns 0 rows.

WHAT IT DOES (idempotent, NON-DESTRUCTIVE)
------------------------------------------
Re-links sfg_code by matching each sfg row's particulars (normalised) against
SFG_Catalog_Plug.csv — exactly the name match ingest_sfg_master uses, but:
  * scoped to item_type='sfg' AND sfg_code IS NULL (touches ONLY orphaned SFG rows);
  * UPDATE only — NEVER inserts a new row;
  * collision-guarded: skips ambiguous plug names (one name -> >1 code) and any
    code already taken; reports every skip.

Dry-run by default; pass --apply to write. Reads DATABASE_URL from .env.

    uv run python scripts/relink_sfg_codes.py            # dry run (no writes)
    uv run python scripts/relink_sfg_codes.py --apply    # perform the re-link
"""

import asyncio
import csv
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.core.helpers import normalise_key  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUG = _ROOT / "docs" / "flows" / "Candor_SFG_JobCard_Integration_2026-06-14" / "SFG_Catalog_Plug.csv"


def _load_plug_name_map():
    """normalised particulars -> sfg_code, plus the set of ambiguous names."""
    name2codes: dict[str, set] = {}
    with PLUG.open(encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            code = normalise_key(r.get("sfg_code"))
            nm = normalise_key(r.get("particulars"))
            if code and nm:
                name2codes.setdefault(nm, set()).add(code)
    ambiguous = {n for n, c in name2codes.items() if len(c) > 1}
    name2code = {n: next(iter(c)) for n, c in name2codes.items() if len(c) == 1}
    return name2code, ambiguous


async def main(apply: bool):
    env = dict(
        l.split("=", 1)
        for l in (_ROOT / ".env").read_text().splitlines()
        if l.strip() and not l.startswith("#") and "=" in l
    )
    dsn = env["DATABASE_URL"].strip().strip('"').strip("'")
    name2code, ambiguous = _load_plug_name_map()
    print(f"plug: {len(name2code)} unambiguous names, {len(ambiguous)} ambiguous (will skip)")

    conn = await asyncpg.connect(dsn, statement_cache_size=0)  # pooler-safe
    try:
        rows = await conn.fetch(
            "SELECT sku_id, particulars FROM all_sku "
            "WHERE item_type = 'sfg' AND sfg_code IS NULL ORDER BY sku_id"
        )
        print(f"orphaned sfg rows (sfg_code IS NULL): {len(rows)}")

        plan, skip_ambig, skip_nomatch = [], [], []
        used = set()
        for r in rows:
            nm = normalise_key(r["particulars"])
            if nm in ambiguous:
                skip_ambig.append(r["particulars"]); continue
            code = name2code.get(nm)
            if not code or code in used:
                skip_nomatch.append(r["particulars"]); continue
            plan.append((r["sku_id"], code)); used.add(code)

        print(f"matched (re-linkable): {len(plan)} | ambiguous: {len(skip_ambig)} | no-match: {len(skip_nomatch)}")
        if skip_ambig:
            print("  ambiguous sample:", skip_ambig[:8])
        if skip_nomatch:
            print("  no-match sample:", skip_nomatch[:8])

        if not apply:
            print("\nDRY RUN — no writes. Re-run with --apply to perform the re-link.")
            return

        done = 0
        async with conn.transaction():
            for sku_id, code in plan:
                status = await conn.execute(
                    """UPDATE all_sku SET sfg_code = $1
                         WHERE sku_id = $2 AND sfg_code IS NULL
                           AND NOT EXISTS (SELECT 1 FROM all_sku WHERE sfg_code = $1)""",
                    code, sku_id,
                )
                if status.rsplit(" ", 1)[-1] == "1":
                    done += 1
        print(f"\nAPPLIED: re-linked sfg_code on {done} rows")
        print("  sfg_master rows now:",
              await conn.fetchval("SELECT COUNT(*) FROM sfg_master"))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
