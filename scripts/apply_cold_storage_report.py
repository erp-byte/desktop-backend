"""Apply a cold storage report load (docs/YYYY-MM-DD-cold-storage-*.sql) to RDS.

Runs the file's body in ONE transaction, prints its check queries, then commits.
With --dry-run it prints the same checks and rolls back, writing nothing.

    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\apply_cold_storage_report.py `
        .\\backend\\docs\\2026-10-19-cold-storage-opening-stock.sql --dry-run

The RDS DSN is whichever DATABASE_URL line in backend/.env points at RDS,
active or commented out. The script refuses to run against any other host.
"""
import asyncio
import re
import sys
from pathlib import Path

import asyncpg

ENV = Path(__file__).resolve().parent.parent / ".env"
CHECK_MARK = "-- ── Check"


class _Rollback(Exception):
    pass


def _split(sql: str) -> tuple[str, list[str]]:
    """The writes (between BEGIN; and the first check) and the check SELECTs."""
    body = sql.split("BEGIN;", 1)[1].split(CHECK_MARK, 1)[0]
    tail = sql.split(CHECK_MARK, 1)[1].split("\n", 1)[1].rsplit("COMMIT;", 1)[0]
    return body, [s.strip() for s in tail.split(";") if "SELECT" in s]


async def main(path: str, dry_run: bool) -> None:
    # Whichever DATABASE_URL line (active or commented out) points at RDS.
    dsns = re.findall(r"^\s*#?\s*DATABASE_URL=(postgresql://\S+)", ENV.read_text(), re.M)
    dsn = next((d for d in dsns if "rds.amazonaws.com" in d.split("@", 1)[-1]), None)
    if not dsn:
        sys.exit("refusing: no DATABASE_URL line in backend/.env points at the RDS host")
    host = dsn.split("@", 1)[1].split("/", 1)[0]
    body, checks = _split(Path(path).read_text(encoding="utf-8"))
    print(f"target: {host}  file: {Path(path).name}  mode: {'DRY RUN' if dry_run else 'APPLY'}")

    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute(body)
            for sql in checks:
                rows = await conn.fetch(sql)
                print(f"\n--- {len(rows)} row(s)")
                for r in rows:
                    print("  " + " | ".join(str(v) for v in r.values()))
            if dry_run:
                raise _Rollback
        print("\nCOMMITTED")
    except _Rollback:
        print("\nROLLED BACK (dry run) - nothing written")
    finally:
        await conn.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) != 1:
        sys.exit(__doc__)
    asyncio.run(main(args[0], "--dry-run" in sys.argv))
