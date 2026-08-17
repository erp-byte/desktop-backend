"""Rollback integration test: migration 090 against the real auth tables.

Applies 090_sku_lookup_permission.sql inside a transaction, asserts the
permission decisions it is supposed to change (and the ones it must NOT), then
rolls back — nothing is left behind. Also re-applies it a second time to prove
the NULL-sub_sub_module insert really is idempotent (ON CONFLICT alone is not,
see 084).

Run:  PYTHONPATH=. python tests/services/test_sku_lookup_permission_rollback.py
"""
import asyncio
import re
from pathlib import Path

import asyncpg

from app.config import Settings
from app.modules.auth.services.permission_service import check_permission

MIGRATION = Path(__file__).parents[2] / "app" / "db" / "090_sku_lookup_permission.sql"

GRANTED = ("npd_team", "business_head", "sales", "planner")
# Holds bare so:view only — must keep reaching the lookup through the sub -> NULL
# fallback, and must not be affected by the migration at all.
INCUMBENT = "so_creator"
# Hits the same 403 through Material In, deliberately out of scope for 090.
UNTOUCHED = "store_head"


async def _role_ids(conn, *names):
    rows = await conn.fetch(
        "SELECT role_name, role_id FROM auth_role WHERE role_name = ANY($1)", list(names))
    found = {r["role_name"]: r["role_id"] for r in rows}
    missing = set(names) - set(found)
    assert not missing, f"roles absent from this DB: {sorted(missing)}"
    return found


def _can(conn, role_id, sub_module):
    return check_permission(conn, [role_id], False, "so",
                            sub_module=sub_module, action="view")


async def _catalog_row_count(conn):
    return await conn.fetchval("""
        SELECT count(*) FROM auth_permission
         WHERE module='so' AND sub_module='sku_lookup'
           AND sub_sub_module IS NULL AND action='view'
    """)


async def main() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    # The runner feeds each file to the server whole; BEGIN/COMMIT inside would
    # close the test's own transaction, so strip the wrapper for this harness.
    body = re.sub(r"^\s*(BEGIN|COMMIT)\s*;\s*$", "", sql, flags=re.M | re.I)

    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=15, statement_cache_size=0)
    tx = conn.transaction()
    await tx.start()
    try:
        ids = await _role_ids(conn, *GRANTED, INCUMBENT, UNTOUCHED)

        # ── before ──────────────────────────────────────────────────────────
        assert await _catalog_row_count(conn) == 0, "so/sku_lookup already exists"
        for role in GRANTED:
            assert not await _can(conn, ids[role], "sku_lookup"), \
                f"{role} could already reach the lookup — bug premise is wrong"
        assert await _can(conn, ids[INCUMBENT], "sku_lookup"), \
            f"{INCUMBENT} should already pass via the (so, NULL, NULL, view) fallback"

        # ── apply ───────────────────────────────────────────────────────────
        await conn.execute(body)

        assert await _catalog_row_count(conn) == 1
        for role in GRANTED:
            assert await _can(conn, ids[role], "sku_lookup"), \
                f"{role} still cannot reach /so/sku-lookup"
            # The whole point of the sub-module: no sales-order read access.
            assert not await _can(conn, ids[role], None), \
                f"{role} was handed the bare so:view surface"

        assert await _can(conn, ids[INCUMBENT], "sku_lookup"), \
            f"{INCUMBENT} lost the lookup — regression"
        assert await _can(conn, ids[INCUMBENT], None), \
            f"{INCUMBENT} lost bare so:view — regression"
        assert not await _can(conn, ids[UNTOUCHED], "sku_lookup"), \
            f"{UNTOUCHED} was granted out of scope"

        # ── re-apply: NULL-safe idempotence ─────────────────────────────────
        await conn.execute(body)
        assert await _catalog_row_count(conn) == 1, \
            "re-run duplicated the catalog row (the NULLS-DISTINCT trap)"

        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
