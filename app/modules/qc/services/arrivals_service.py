"""QC material arrivals — read views for the Material In tab.

Surfaces, per transaction, the QC state of each dock arrival (qc_intimation)
joined to its latest inward inspection, so Material In can:
  - show per-article QC status (Arrived / In QC / Accepted / Rejected), and
  - filter transactions by Pending arrival / Arrived / Completed.

"Pending arrival" is the ABSENCE of any arrival for a transaction, so it is
inferred on the client (a transaction missing from the summary) rather than
returned here.
"""

from __future__ import annotations

from typing import Any


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            s = v.isoformat()
            return s.replace("+00:00", "Z") if "T" in s else s
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _arrival_state(inspection_status: str | None, verdict: str | None) -> str:
    """Per-arrival display state.

    accepted / rejected once a verdict exists; in_qc while an inspection is
    open; otherwise arrived (no inspection yet, or a cancelled one).
    """
    if verdict == "passed":
        return "accepted"
    if verdict == "failed":
        return "rejected"
    if inspection_status in ("in_progress", "readings_submitted"):
        return "in_qc"
    return "arrived"


# Latest inspection per intimation (highest inspection_id wins).
_LATEST_INSP = """
    LEFT JOIN LATERAL (
        SELECT inspection_id, status, verdict, approved_at
          FROM qc_inward_inspection
         WHERE qc_intimation_id = i.qc_intimation_id
         ORDER BY inspection_id DESC
         LIMIT 1
    ) insp ON TRUE
"""


async def list_arrivals(pool, transaction_no: str) -> list[dict]:
    """All dock arrivals for one transaction, each with its latest QC state."""
    rows = await pool.fetch(
        f"""
        SELECT i.qc_intimation_id, i.transaction_no, i.po_number,
               i.sku_id, i.sku_name, i.sku_name_raw, i.lot_number,
               i.invoice_no, i.vehicle_no, i.created_at,
               insp.inspection_id, insp.status AS inspection_status,
               insp.verdict, insp.approved_at
          FROM qc_intimation i
          {_LATEST_INSP}
         WHERE i.transaction_no = $1
         ORDER BY i.sku_name NULLS LAST, i.created_at, i.qc_intimation_id
        """,
        transaction_no,
    )
    return [
        {
            "qc_intimation_id": r["qc_intimation_id"],
            "transaction_no": r["transaction_no"],
            "po_number": r["po_number"],
            "sku_id": r["sku_id"],
            "sku_name": r["sku_name"],
            "sku_name_raw": r["sku_name_raw"],
            "lot_number": r["lot_number"],
            "invoice_no": r["invoice_no"],
            "vehicle_no": r["vehicle_no"],
            "created_at": _iso(r["created_at"]),
            "inspection_id": r["inspection_id"],
            "inspection_status": r["inspection_status"],
            "verdict": r["verdict"],
            "approved_at": _iso(r["approved_at"]),
            "qc_state": _arrival_state(r["inspection_status"], r["verdict"]),
        }
        for r in rows
    ]


async def summary(pool, transaction_nos: list[str]) -> list[dict]:
    """Per-transaction QC rollup for the transactions that have arrivals.

    overall status:
      arrived   — at least one arrival is still awaiting QC or in QC
      completed — every arrival has a verdict (passed/failed)
    (Transactions with no arrivals are absent → client treats as pending_arrival.)
    """
    if not transaction_nos:
        return []
    rows = await pool.fetch(
        f"""
        SELECT i.transaction_no,
               COUNT(*)                                                   AS total,
               COUNT(*) FILTER (
                   WHERE insp.verdict IS NULL
                     AND (insp.status IS NULL OR insp.status = 'cancelled')
               )                                                          AS awaiting,
               COUNT(*) FILTER (
                   WHERE insp.status IN ('in_progress', 'readings_submitted')
               )                                                          AS in_qc,
               COUNT(*) FILTER (WHERE insp.verdict = 'passed')            AS accepted,
               COUNT(*) FILTER (WHERE insp.verdict = 'failed')            AS rejected
          FROM qc_intimation i
          {_LATEST_INSP}
         WHERE i.transaction_no = ANY($1::text[])
         GROUP BY i.transaction_no
        """,
        transaction_nos,
    )
    out: list[dict] = []
    for r in rows:
        total = r["total"] or 0
        awaiting = r["awaiting"] or 0
        in_qc = r["in_qc"] or 0
        accepted = r["accepted"] or 0
        rejected = r["rejected"] or 0
        status = "arrived" if (awaiting + in_qc) > 0 else "completed"
        out.append(
            {
                "transaction_no": r["transaction_no"],
                "status": status,
                "total": total,
                "awaiting": awaiting,
                "in_qc": in_qc,
                "accepted": accepted,
                "rejected": rejected,
            }
        )
    return out
