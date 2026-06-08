"""Sample numbering stress test (checklist C2 / acceptance §15.13).

    python scripts/sample_stress_numbering.py [N]   # default N=100

Fires N concurrent requisition creates and N concurrent gate-pass inserts (each
on its own connection) and asserts every SMP-YYYYMMDD-NNNN / GP-YYYYMMDD-NNNN
number is unique — i.e. the nextval-based numbering is collision-safe under load
and the UNIQUE constraints never trip.

Each insert runs in a transaction that is ROLLED BACK, so the rows never persist;
the sequence still advances (sequences are non-transactional — that IS the
mechanism we're proving). Reads DATABASE_URL from the environment / .env.
"""
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modules.sample.services import requisition_service as req_svc  # noqa: E402
from app.modules.sample.services.gate_pass_service import _gen_gate_pass_number  # noqa: E402

load_dotenv()
WAREHOUSE = "W202"     # sample requisition / gate_pass warehouse (post-040 rename)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 100


class FakeUser:
    def __init__(self, user_id):
        self.user_id = user_id
        self.role_name = "planner"
        self.entity = "cfpl"   # unused by create now; kept for any service that reads it
        self.full_name = "stress"
        self.is_admin = False


async def _seed_refs(pool):
    async with pool.acquire() as conn:
        uid = await conn.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
        sku = await conn.fetchrow("SELECT sku_id, particulars FROM all_sku ORDER BY sku_id LIMIT 1")
    if uid is None or sku is None:
        print("Need at least one auth_user and one all_sku row to run the stress test.")
        sys.exit(1)
    return uid, sku["sku_id"], sku["particulars"]


async def _one_requisition(pool, user, sku_id, sku_name):
    """Create a requisition concurrently; return its number; roll back the row."""
    conn = await pool.acquire()
    tr = conn.transaction()
    await tr.start()
    try:
        req = await req_svc.create_requisition(conn, payload={
            "sample_type": "BASIS_RM", "warehouse": WAREHOUSE,
            "articles": [{"sku_id": sku_id, "sku_name": sku_name, "required_qty": 1,
                          "uom": "kg", "article_role": "RM"}]}, user=user)
        return req["requisition_number"]
    finally:
        await tr.rollback()
        await pool.release(conn)


async def _one_gate_pass(pool, uid):
    """Insert a gate pass concurrently (exercises seq_gate_pass + UNIQUE); roll back."""
    conn = await pool.acquire()
    tr = conn.transaction()
    await tr.start()
    try:
        seq = await conn.fetchval("SELECT nextval('seq_gate_pass')")
        number = _gen_gate_pass_number(seq)
        await conn.execute(
            "INSERT INTO gate_passes (gate_pass_number, gate_pass_type, issued_by, warehouse) "
            "VALUES ($1, 'SAMPLE', $2, $3)", number, uid, WAREHOUSE)
        return number
    finally:
        await tr.rollback()
        await pool.release(conn)


async def _run(label, coro_factory, n):
    results = await asyncio.gather(*[coro_factory() for _ in range(n)], return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    numbers = [r for r in results if not isinstance(r, Exception)]
    distinct = len(set(numbers))
    ok = len(errors) == 0 and distinct == n
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {len(numbers)} created, "
          f"{distinct} distinct, {len(errors)} errors")
    if errors:
        print(f"        first error: {type(errors[0]).__name__}: {errors[0]}")
    if distinct != len(numbers):
        print("        DUPLICATE NUMBERS DETECTED")
    return ok


async def main():
    db = os.environ.get("DATABASE_URL")
    if not db:
        print("DATABASE_URL not set"); sys.exit(1)
    pool = await asyncpg.create_pool(db, min_size=4, max_size=20)
    try:
        uid, sku_id, sku_name = await _seed_refs(pool)
        user = FakeUser(uid)
        print(f"Firing {N} concurrent inserts each...")
        a = await _run("requisition SMP- numbers", lambda: _one_requisition(pool, user, sku_id, sku_name), N)
        b = await _run("gate-pass GP- numbers", lambda: _one_gate_pass(pool, uid), N)
    finally:
        await pool.close()
    sys.exit(0 if (a and b) else 1)


if __name__ == "__main__":
    asyncio.run(main())
