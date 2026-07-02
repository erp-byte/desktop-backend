"""Verifies migration 070 created the customer-returns tables with the expected
natural-key primary keys. Read-only; safe against any DB. Run:

    PYTHONPATH=. python tests/services/test_cr_migration.py
"""
import asyncio
import asyncpg
from app.config import Settings

EXPECTED_PK = {
    "cfpl_customer_return_header": ["rtv_id"],
    "cdpl_customer_return_header": ["rtv_id"],
    "cfpl_customer_return_lines": ["rtv_id", "item_description"],
    "cdpl_customer_return_lines": ["rtv_id", "item_description"],
    "cfpl_customer_return_boxes": ["rtv_id", "article_description", "box_number"],
    "cdpl_customer_return_boxes": ["rtv_id", "article_description", "box_number"],
}


async def _pk_cols(conn, table: str) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT a.attname
          FROM pg_index i
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
         WHERE i.indrelid = to_regclass($1) AND i.indisprimary
         ORDER BY array_position(i.indkey, a.attnum)
        """,
        table,
    )
    return [r["attname"] for r in rows]


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    try:
        for table, expected in EXPECTED_PK.items():
            assert await conn.fetchval("SELECT to_regclass($1)", table) is not None, \
                f"missing table {table} — run scripts/migrate.py"
            got = await _pk_cols(conn, table)
            assert got == expected, f"{table} PK expected {expected}, got {got}"
        assert await conn.fetchval("SELECT to_regclass('box_edit_logs')") is not None, \
            "missing box_edit_logs"
        print("ASSERTIONS PASSED")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
