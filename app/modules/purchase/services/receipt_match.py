"""When a PO line, and a PO, counts as received in full.

One rule for both the stored po_header.status (router._refresh_receiving_status,
run on every box change) and GET /po/{txn}/receipt-summary (the ✓ per line in
the Material In Completed view), so the two never disagree.

A line is matched when what arrived covers what was ordered:
  * weight -- the ordered weight (po_line.po_weight) is the measure: received
    net weight must reach 99% of it (po_query._WEIGHT_MATCH_FACTOR);
  * count  -- checked only when the boxes record a count (po_box.count), or
    when the line has no ordered weight to go by (then the number of boxes
    stands in). Tally's Quantity (pack_count) is kg for raw material, so
    comparing it with a number of boxes would never match.

A PO's receiving status:
  pending            -- no box yet
  extra received     -- more net weight than the ordered weight, in total
  fully received     -- every line matched
  partially received -- otherwise
"""

from __future__ import annotations

from app.modules.purchase.services.po_query import _WEIGHT_MATCH_FACTOR

# Per line of a PO: ordered weight and quantity, and what its boxes add up to.
LINE_RECEIPT_SQL = """
    SELECT l.line_number, l.sku_name, l.particulars, l.pack_count, l.po_weight,
           COALESCE(r.rec_wt, 0)  AS received_weight,
           r.counted              AS counted,
           COALESCE(r.n_boxes, 0) AS received_boxes
      FROM po_line l
      LEFT JOIN LATERAL (
          SELECT SUM(b.net_weight) AS rec_wt,
                 SUM(b.count)      AS counted,
                 COUNT(b.box_id)   AS n_boxes
            FROM po_box b
           WHERE b.transaction_no = l.transaction_no
             AND b.line_number    = l.line_number
      ) r ON TRUE
     WHERE l.transaction_no = $1
     ORDER BY l.line_number
"""


def line_match(row) -> dict:
    """The receipt of one LINE_RECEIPT_SQL row, with its matched flags."""
    ordered_weight = float(row["po_weight"]) if row["po_weight"] is not None else None
    received_weight = float(row["received_weight"] or 0)
    ordered_count = float(row["pack_count"]) if row["pack_count"] is not None else None
    n_boxes = int(row["received_boxes"] or 0)
    counted = row["counted"]
    received_count = int(counted) if counted is not None else n_boxes
    weight_matched = ordered_weight is None or received_weight >= ordered_weight * _WEIGHT_MATCH_FACTOR
    check_count = ordered_count is not None and (counted is not None or ordered_weight is None)
    count_matched = not check_count or received_count >= ordered_count
    return {
        "line_number": row["line_number"],
        "ordered_weight": ordered_weight,
        "received_weight": received_weight,
        "ordered_count": int(ordered_count) if ordered_count is not None else None,
        "received_count": received_count,
        "received_boxes": n_boxes,
        "weight_matched": weight_matched,
        "count_matched": count_matched,
        "matched": weight_matched and count_matched,
    }


def receiving_status(lines: list[dict]) -> str:
    """po_header.status for a PO from its line_match results."""
    if not any(l["received_boxes"] for l in lines):
        return "pending"
    received = sum(l["received_weight"] for l in lines)
    ordered = sum(l["ordered_weight"] or 0 for l in lines)
    if ordered > 0 and received > ordered:   # no ordered weight: nothing to exceed
        return "extra received"
    if lines and all(l["matched"] for l in lines):
        return "fully received"
    return "partially received"
