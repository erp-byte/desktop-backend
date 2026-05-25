"""Sample box-id patterns on both sides to understand format and discrepancy types."""
import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        print("=== Sample OUT box_ids ===")
        rows = await conn.fetch(
            "SELECT DISTINCT box_id FROM interunit_transfer_boxes WHERE box_id IS NOT NULL ORDER BY box_id LIMIT 20"
        )
        for r in rows:
            print(f"  {r['box_id']!r}")

        print("\n=== Sample IN box_ids ===")
        rows = await conn.fetch(
            "SELECT DISTINCT box_id FROM interunit_transfer_in_boxes WHERE box_id IS NOT NULL ORDER BY box_id LIMIT 20"
        )
        for r in rows:
            print(f"  {r['box_id']!r}")

        # categorize discrepant pairs
        print("\n=== Discrepancy categorization (status=Received on both sides) ===")
        rows = await conn.fetch(
            """
            WITH oc AS (
              SELECT header_id, COUNT(*) c, ARRAY_AGG(DISTINCT box_id) ids
              FROM interunit_transfer_boxes WHERE box_id IS NOT NULL GROUP BY header_id
            ),
            ic AS (
              SELECT header_id, COUNT(*) c, ARRAY_AGG(DISTINCT box_id) ids,
                     COUNT(*) FILTER (WHERE is_matched = FALSE) unmatched
              FROM interunit_transfer_in_boxes WHERE box_id IS NOT NULL GROUP BY header_id
            )
            SELECT
              CASE
                WHEN COALESCE(oc.c,0)=0 AND COALESCE(ic.c,0)>0 THEN 'out_has_no_boxes_but_in_does'
                WHEN COALESCE(oc.c,0)>0 AND COALESCE(ic.c,0)=0 THEN 'in_has_no_boxes_but_out_does'
                WHEN COALESCE(oc.c,0)<>COALESCE(ic.c,0)         THEN 'box_count_differs_both_sides_have_boxes'
                WHEN oc.ids <> ic.ids                            THEN 'same_count_diff_ids'
                ELSE 'other'
              END AS bucket,
              COUNT(*) AS n
            FROM interunit_transfers_header oh
            JOIN interunit_transfer_in_header ih ON ih.transfer_out_id = oh.id
            LEFT JOIN oc ON oc.header_id = oh.id
            LEFT JOIN ic ON ic.header_id = ih.id
            WHERE oh.status='Received' AND ih.status='Received'
              AND (
                COALESCE(oc.c,0)<>COALESCE(ic.c,0)
                OR COALESCE(ic.unmatched,0)>0
                OR EXISTS (SELECT 1 FROM unnest(COALESCE(oc.ids,'{}'::text[])) x WHERE x NOT IN (SELECT unnest(COALESCE(ic.ids,'{}'::text[]))))
                OR EXISTS (SELECT 1 FROM unnest(COALESCE(ic.ids,'{}'::text[])) x WHERE x NOT IN (SELECT unnest(COALESCE(oc.ids,'{}'::text[]))))
              )
            GROUP BY bucket
            ORDER BY n DESC
            """
        )
        for r in rows:
            print(f"  {r['bucket']:45} {r['n']}")
    finally:
        await conn.close()


asyncio.run(main())
