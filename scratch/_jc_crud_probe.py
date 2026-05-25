"""Verification probe for job-card PATCH + DELETE endpoints.

Runs through the headline scenarios from the spec:
  - Partial update preserves unspecified columns (the "10 fields, change 1, others intact" rule)
  - Empty body returns 422
  - Explicit null clears the column
  - DELETE with reason < 3 chars returns 422

Usage:
  1. Ensure DDL from spec §13 has been run.
  2. Start the dev server: uvicorn app.main:app --reload --port 8000
  3. Set env vars and run:

     DB_URL='postgresql://...' TOKEN='<bearer>' python _jc_crud_probe.py <existing_job_card_id>

The chosen job_card_id must have status in {locked, unlocked, assigned,
material_received, in_progress}. The probe reads its current state, runs
several PATCHes, and restores the original floor value at the end.
"""
import asyncio
import os
import sys

import asyncpg
import httpx

DB_URL = os.environ["DB_URL"]
BASE = os.environ.get("BASE_URL", "http://localhost:8000")
TOKEN = os.environ["TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def snapshot(conn, jc_id):
    return dict(await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", jc_id))


async def main(jc_id: int):
    db = await asyncpg.connect(DB_URL)
    client = httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=10.0)
    try:
        before = await snapshot(db, jc_id)
        print(f"[before] status={before['status']} floor={before['floor']!r} "
              f"team_leader={before['assigned_to_team_leader']!r} "
              f"customer_name={before['customer_name']!r}")

        print("\n=== Test 1: PATCH with one field, verify rest unchanged ===")
        r = await client.patch(
            f"/api/v1/production/job-cards/{jc_id}",
            json={"floor": "TEST_FLOOR_A", "updated_by": "probe"},
        )
        print(f"  status={r.status_code} body={r.json()}")
        assert r.status_code == 200, "expected 200"
        after = await snapshot(db, jc_id)
        assert after["floor"] == "TEST_FLOOR_A", "floor should be updated"
        for col in ("status", "assigned_to_team_leader", "customer_name",
                    "batch_number", "batch_size_kg", "machine_id",
                    "factory", "process_name", "stage", "bom_id"):
            assert after[col] == before[col], (
                f"{col} should be unchanged: {before[col]!r} -> {after[col]!r}")
        assert after["updated_at"] is not None, "updated_at must be stamped"
        assert after["updated_by"] == "probe"
        print("  ok unspecified columns preserved, audit columns stamped")

        print("\n=== Test 2: PATCH with only updated_by returns 422 ===")
        r = await client.patch(
            f"/api/v1/production/job-cards/{jc_id}",
            json={"updated_by": "probe"},
        )
        print(f"  status={r.status_code} body={r.json()}")
        assert r.status_code == 422
        print("  ok no editable fields rejected")

        print("\n=== Test 3: PATCH with floor=null clears the column ===")
        r = await client.patch(
            f"/api/v1/production/job-cards/{jc_id}",
            json={"floor": None, "updated_by": "probe"},
        )
        print(f"  status={r.status_code}")
        assert r.status_code == 200
        after = await snapshot(db, jc_id)
        assert after["floor"] is None, f"floor should be NULL, got {after['floor']!r}"
        print("  ok explicit null cleared the column")

        await client.patch(
            f"/api/v1/production/job-cards/{jc_id}",
            json={"floor": before["floor"], "updated_by": "probe"},
        )
        print(f"  (restored floor to {before['floor']!r})")

        print("\n=== Test 4: DELETE with reason < 3 chars returns 422 ===")
        r = await client.request(
            "DELETE", f"/api/v1/production/job-cards/{jc_id}",
            json={"cancellation_reason": "x", "deleted_by": "probe"},
        )
        print(f"  status={r.status_code}")
        assert r.status_code == 422
        print("  ok short reason rejected")

        print("\nAll PATCH probes passed. DELETE probe skipped to preserve test data.")
        print("To exercise DELETE: pass a throwaway JC id and uncomment the block at the bottom.")

        # Uncomment to exercise the actual soft-delete:
        # r = await client.request(
        #     "DELETE", f"/api/v1/production/job-cards/{jc_id}",
        #     json={"cancellation_reason": "test cancellation", "deleted_by": "probe"},
        # )
        # assert r.status_code == 200
        # after = await snapshot(db, jc_id)
        # assert after["deleted_at"] is not None
        # assert after["status"] == "cancelled"
        # assert after["cancellation_reason"] == "test cancellation"

    finally:
        await client.aclose()
        await db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(int(sys.argv[1])))
