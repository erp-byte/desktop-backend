"""Back up every Stock Take table from RDS into one local SQLite file. Read-only.

    .venv/Scripts/python.exe scripts/backup_stock_take_tables.py [--list]

Tables: new_stock_entries (counts + adjustment mirror rows), stocktake_transactions
(the append-only adjustment ledger) and stocktake_entries (the floor app's table),
plus any other public table whose name starts with stocktake_ / stock_take_.

Written to ERP/backups/stock_take_backup_<YYYY-MM-DD>_<HHMM>IST.sqlite. Every
column is kept under its own name; dates and timestamps as ISO text, numerics as
text (exact), JSON as text. A `_manifest` table records each table's row count,
when the snapshot was taken, and the checks a restore can be compared against.

One REPEATABLE READ, READ ONLY transaction, so all tables come from the same
moment. Nothing is written to the database.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ROOT.parent / "backups"
FIXED = ("new_stock_entries", "stocktake_transactions", "stocktake_entries")


def rds_dsn() -> str:
    env = (ROOT / ".env").read_text()
    for dsn in re.findall(r"^\s*#?\s*DATABASE_URL=(\S+)", env, re.M):
        if "rds.amazonaws.com" in dsn.split("@", 1)[-1]:
            return dsn.replace("postgresql+asyncpg://", "postgresql://")
    sys.exit("no DATABASE_URL line in backend/.env points at RDS")


def to_sqlite(v):
    if v is None or isinstance(v, (int, float, str, bytes)):
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


async def main(a) -> None:
    conn = await asyncpg.connect(rds_dsn(), timeout=30)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            names = [r["table_name"] for r in await conn.fetch(
                """SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                      AND (table_name = ANY($1::text[])
                           OR table_name LIKE 'stocktake\\_%' OR table_name LIKE 'stock\\_take\\_%')
                    ORDER BY table_name""", list(FIXED))]
            taken = await conn.fetchval("SELECT now() AT TIME ZONE 'Asia/Kolkata'")
            counts = {t: await conn.fetchval(f'SELECT count(*) FROM "{t}"') for t in names}
            print(f"snapshot at {taken:%d %b %Y %H:%M:%S} IST")
            for t in names:
                print(f"  {t:<34} {counts[t]:>9,} rows")
            if a.list:
                return

            BACKUPS.mkdir(exist_ok=True)
            out = BACKUPS / f"stock_take_backup_{taken:%Y-%m-%d_%H%M}IST.sqlite"
            if out.exists():
                sys.exit(f"{out} already exists; not overwriting a backup")
            db = sqlite3.connect(out)
            db.execute("CREATE TABLE _manifest (table_name TEXT PRIMARY KEY, row_count INTEGER, "
                       "taken_at_ist TEXT, source TEXT, checks TEXT)")
            for t in names:
                cols = [(r["column_name"], r["data_type"]) for r in await conn.fetch(
                    """SELECT column_name, data_type FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = $1 ORDER BY ordinal_position""", t)]
                aff = {"integer": "INTEGER", "bigint": "INTEGER", "smallint": "INTEGER",
                       "boolean": "INTEGER", "double precision": "REAL", "real": "REAL"}
                db.execute(f'CREATE TABLE "{t}" (' + ", ".join(
                    f'"{c}" {aff.get(dt, "TEXT")}' for c, dt in cols) + ")")
                ph = ", ".join("?" for _ in cols)
                n = 0
                async for r in conn.cursor(f'SELECT * FROM "{t}"', prefetch=5000):
                    db.execute(f'INSERT INTO "{t}" VALUES ({ph})', [to_sqlite(r[c]) for c, _ in cols])
                    n += 1
                checks = {}
                colnames = {c for c, _ in cols}
                for c in ("total_weight", "qty_kg"):
                    if c in colnames:
                        checks[f"sum_{c}"] = str(await conn.fetchval(f'SELECT COALESCE(SUM("{c}"), 0) FROM "{t}"'))
                for c in ("id", "txn_id"):
                    if c in colnames:
                        checks[f"max_{c}"] = await conn.fetchval(f'SELECT MAX("{c}") FROM "{t}"')
                if n != counts[t]:
                    sys.exit(f"{t}: wrote {n} rows but counted {counts[t]}; backup incomplete")
                db.execute("INSERT INTO _manifest VALUES (?, ?, ?, ?, ?)",
                           (t, n, taken.isoformat(), "RDS warehouse_db", json.dumps(checks, default=str)))
                db.commit()
            db.close()
    finally:
        await conn.close()
    print(f"\nbackup written: {out}  ({out.stat().st_size / 1e6:,.1f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="only list the tables and row counts")
    asyncio.run(main(ap.parse_args()))
