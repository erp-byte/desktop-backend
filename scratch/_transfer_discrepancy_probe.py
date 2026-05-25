"""
Final report: completed interunit transfers (status='Received' on both sides) whose
transfer-out boxes do not match transfer-in boxes by box_id.

We split results into two buckets:
  A) BOX-ID MISMATCH (both sides scanned boxes, but the id sets differ)
     -> these are the "box id error" cases the user asked about
  B) OUT-SIDE HAS NO BOX SCANS (in side scanned, out side empty)
     -> dispatch never recorded box ids; in-side scans cannot be reconciled

Writes _transfer_discrepancies.csv with all rows tagged by bucket.
"""
import asyncio
import asyncpg
import csv
import os
import sys
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

# Force utf-8 stdout on Windows console
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

QUERY = """
WITH oc AS (
    SELECT header_id,
           COUNT(*) AS out_count,
           ARRAY_AGG(DISTINCT box_id) AS out_ids
    FROM interunit_transfer_boxes
    WHERE box_id IS NOT NULL
    GROUP BY header_id
),
ic AS (
    SELECT header_id,
           COUNT(*) AS in_count,
           ARRAY_AGG(DISTINCT box_id) AS in_ids,
           COUNT(*) FILTER (WHERE is_matched = FALSE) AS unmatched_in_count
    FROM interunit_transfer_in_boxes
    WHERE box_id IS NOT NULL
    GROUP BY header_id
)
SELECT
    oh.id                                  AS out_header_id,
    ih.id                                  AS in_header_id,
    oh.stock_trf_date                      AS transfer_date,
    oh.challan_no                          AS txn_number,
    oh.from_site                           AS from_site,
    oh.to_site                             AS to_site,
    ih.receiving_warehouse                 AS receiving_warehouse,
    ih.grn_number                          AS grn_number,
    ih.received_at                         AS received_at,
    oh.has_variance                        AS out_has_variance,
    COALESCE(oc.out_count, 0)              AS out_box_count,
    COALESCE(ic.in_count, 0)               AS in_box_count,
    COALESCE(ic.in_count, 0) - COALESCE(oc.out_count, 0) AS diff,
    COALESCE(ic.unmatched_in_count, 0)     AS unmatched_in_count,
    ARRAY(
        SELECT unnest(COALESCE(oc.out_ids, '{}'::text[]))
        EXCEPT
        SELECT unnest(COALESCE(ic.in_ids,  '{}'::text[]))
    ) AS only_in_out,
    ARRAY(
        SELECT unnest(COALESCE(ic.in_ids,  '{}'::text[]))
        EXCEPT
        SELECT unnest(COALESCE(oc.out_ids, '{}'::text[]))
    ) AS only_in_in,
    CASE
        WHEN COALESCE(oc.out_count,0)=0 AND COALESCE(ic.in_count,0)>0
            THEN 'NO_OUT_SCANS'
        WHEN COALESCE(oc.out_count,0)>0 AND COALESCE(ic.in_count,0)=0
            THEN 'NO_IN_SCANS'
        ELSE 'BOX_ID_MISMATCH'
    END AS bucket
FROM interunit_transfers_header   oh
JOIN interunit_transfer_in_header ih ON ih.transfer_out_id = oh.id
LEFT JOIN oc ON oc.header_id = oh.id
LEFT JOIN ic ON ic.header_id = ih.id
WHERE oh.status = 'Received'
  AND ih.status = 'Received'
  AND (
        COALESCE(oc.out_count,0) <> COALESCE(ic.in_count,0)
     OR COALESCE(ic.unmatched_in_count,0) > 0
     OR EXISTS (
          SELECT 1 FROM unnest(COALESCE(oc.out_ids,'{}'::text[])) ob
          WHERE ob NOT IN (SELECT unnest(COALESCE(ic.in_ids,'{}'::text[])))
        )
     OR EXISTS (
          SELECT 1 FROM unnest(COALESCE(ic.in_ids,'{}'::text[])) ib
          WHERE ib NOT IN (SELECT unnest(COALESCE(oc.out_ids,'{}'::text[])))
        )
  )
ORDER BY oh.stock_trf_date DESC NULLS LAST, oh.id DESC
"""


def fmt_ids(xs, max_items=6):
    if not xs:
        return "-"
    s = ", ".join(xs[:max_items])
    if len(xs) > max_items:
        s += f"  (+{len(xs) - max_items} more)"
    return s


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(QUERY)
    finally:
        await conn.close()

    by_bucket = {"BOX_ID_MISMATCH": [], "NO_OUT_SCANS": [], "NO_IN_SCANS": []}
    for r in rows:
        by_bucket[r["bucket"]].append(r)

    print(f"Total discrepant pairs: {len(rows)}\n")
    print(f"  BOX_ID_MISMATCH (both sides have boxes but IDs differ): {len(by_bucket['BOX_ID_MISMATCH'])}")
    print(f"  NO_OUT_SCANS    (dispatch recorded no boxes):           {len(by_bucket['NO_OUT_SCANS'])}")
    print(f"  NO_IN_SCANS     (receipt recorded no boxes):            {len(by_bucket['NO_IN_SCANS'])}")

    def print_bucket(name, items):
        if not items:
            return
        print(f"\n\n========== {name} ({len(items)}) ==========")
        for r in items:
            print(
                f"\n  date={r['transfer_date']}  txn={r['txn_number']}  "
                f"from={r['from_site']} -> to={r['to_site']} (received_at={r['receiving_warehouse']})\n"
                f"    grn={r['grn_number'] or '-'}  received_at={r['received_at']}\n"
                f"    out_boxes={r['out_box_count']}  in_boxes={r['in_box_count']}  diff={r['diff']}  "
                f"unmatched_in={r['unmatched_in_count']}  has_variance={r['out_has_variance']}\n"
                f"    sent-but-not-received ({len(r['only_in_out'])}): {fmt_ids(r['only_in_out'])}\n"
                f"    received-but-not-sent ({len(r['only_in_in'])}): {fmt_ids(r['only_in_in'])}"
            )

    print_bucket("BOX_ID_MISMATCH", by_bucket["BOX_ID_MISMATCH"])
    print_bucket("NO_OUT_SCANS",    by_bucket["NO_OUT_SCANS"])
    print_bucket("NO_IN_SCANS",     by_bucket["NO_IN_SCANS"])

    csv_path = "_transfer_discrepancies.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "bucket", "transfer_date", "txn_number",
            "from_site", "to_site", "receiving_warehouse", "grn_number",
            "received_at", "out_box_count", "in_box_count", "diff",
            "unmatched_in_count", "out_has_variance",
            "out_header_id", "in_header_id",
            "only_in_out_count", "only_in_in_count",
            "only_in_out_box_ids", "only_in_in_box_ids",
        ])
        for r in rows:
            w.writerow([
                r["bucket"], r["transfer_date"], r["txn_number"],
                r["from_site"], r["to_site"], r["receiving_warehouse"], r["grn_number"],
                r["received_at"], r["out_box_count"], r["in_box_count"], r["diff"],
                r["unmatched_in_count"], r["out_has_variance"],
                r["out_header_id"], r["in_header_id"],
                len(r["only_in_out"]), len(r["only_in_in"]),
                "|".join(r["only_in_out"]), "|".join(r["only_in_in"]),
            ])
    print(f"\n\nCSV written to {csv_path}")


asyncio.run(main())
