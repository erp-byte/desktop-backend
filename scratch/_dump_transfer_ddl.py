"""READ-ONLY: reconstruct CREATE TABLE DDL for the inter-unit transfer tables
from the live schema (information_schema + pg_catalog). No writes, no locks of
concern — pure introspection. Prints DDL to stdout."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

TABLE_FILTER = """
    table_schema = 'public' AND (
        table_name LIKE 'interunit_%'
        OR table_name IN (
            'pending_transfer_stock',
            'transfer_box_reconciliation',
            'cold_stock_disposition',
            'inner_cold_transfer'
        )
    )
"""


def render_type(col) -> str:
    dt = col["data_type"]
    udt = col["udt_name"]
    if dt == "character varying":
        n = col["character_maximum_length"]
        return f"VARCHAR({n})" if n else "VARCHAR"
    if dt == "character":
        n = col["character_maximum_length"]
        return f"CHAR({n})" if n else "CHAR"
    if dt == "numeric":
        p, s = col["numeric_precision"], col["numeric_scale"]
        if p is not None and s is not None:
            return f"NUMERIC({p},{s})"
        return "NUMERIC"
    if dt == "ARRAY":
        # udt_name is like _text, _varchar, _int4
        base = {"_text": "TEXT", "_varchar": "VARCHAR", "_int4": "INTEGER",
                "_int8": "BIGINT", "_numeric": "NUMERIC"}.get(udt, udt.lstrip("_").upper())
        return f"{base}[]"
    return {
        "integer": "INTEGER",
        "bigint": "BIGINT",
        "smallint": "SMALLINT",
        "boolean": "BOOLEAN",
        "text": "TEXT",
        "date": "DATE",
        "double precision": "DOUBLE PRECISION",
        "real": "REAL",
        "json": "JSON",
        "jsonb": "JSONB",
        "timestamp without time zone": "TIMESTAMP",
        "timestamp with time zone": "TIMESTAMPTZ",
        "uuid": "UUID",
    }.get(dt, dt.upper())


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        tables = [r["table_name"] for r in await conn.fetch(
            f"SELECT table_name FROM information_schema.tables WHERE {TABLE_FILTER} ORDER BY table_name"
        )]
        print(f"-- Found {len(tables)} tables: {', '.join(tables)}\n")

        for name in tables:
            cols = await conn.fetch(
                """
                SELECT column_name, data_type, character_maximum_length,
                       numeric_precision, numeric_scale, is_nullable,
                       column_default, udt_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=$1
                ORDER BY ordinal_position
                """, name)

            pk_cols = [r["attname"] for r in await conn.fetch(
                """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = $1::regclass AND i.indisprimary
                ORDER BY a.attnum
                """, name)]

            lines = []
            for c in cols:
                default = c["column_default"]
                col_type = render_type(c)
                # Collapse serial: integer/bigint with nextval default -> SERIAL/BIGSERIAL
                is_serial = default and "nextval(" in default and c["data_type"] in ("integer", "bigint")
                if is_serial:
                    col_type = "BIGSERIAL" if c["data_type"] == "bigint" else "SERIAL"
                    default = None
                seg = f'    {c["column_name"]} {col_type}'
                if c["is_nullable"] == "NO" and not is_serial:
                    seg += " NOT NULL"
                if default:
                    seg += f" DEFAULT {default}"
                lines.append(seg)

            if pk_cols:
                lines.append(f'    PRIMARY KEY ({", ".join(pk_cols)})')

            # indexes (non-PK)
            idx = await conn.fetch(
                """
                SELECT indexdef FROM pg_indexes
                WHERE schemaname='public' AND tablename=$1
                """, name)
            idx_defs = [r["indexdef"] for r in idx if "PRIMARY KEY" not in r["indexdef"]
                        and "_pkey" not in r["indexdef"]]

            print(f"CREATE TABLE IF NOT EXISTS {name} (")
            print(",\n".join(lines))
            print(");")
            for d in idx_defs:
                print(d.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS")
                       .replace("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS") + ";")
            print()
    finally:
        await conn.close()


asyncio.run(main())
