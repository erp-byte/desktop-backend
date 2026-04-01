"""Purchase query helpers — WHERE builder, fetch + build helpers for view/export."""

import logging
from datetime import date

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def build_where_clause(
    *,
    search, entity, vendor, customer, date_from, date_to,
    status, warehouse, item_category, sub_category, item_type,
) -> tuple[str, list]:
    """Build a WHERE clause and params list from filter values."""
    conditions = []
    params: list = []
    param_idx = 0

    if entity:
        param_idx += 1
        conditions.append(f"h.entity = ${param_idx}")
        params.append(entity.lower())

    if search:
        param_idx += 1
        search_param = f"%{search}%"
        conditions.append(
            f"(h.transaction_no ILIKE ${param_idx}"
            f" OR h.vendor_supplier_name ILIKE ${param_idx}"
            f" OR h.customer_party_name ILIKE ${param_idx}"
            f" OR h.po_number ILIKE ${param_idx}"
            f" OR h.invoice_number ILIKE ${param_idx})"
        )
        params.append(search_param)

    for col, val in [
        ("h.vendor_supplier_name", vendor),
        ("h.customer_party_name", customer),
        ("h.warehouse", warehouse),
        ("h.status", status),
    ]:
        if val:
            vals = [v.strip() for v in val.split(",") if v.strip()]
            placeholders = []
            for v in vals:
                param_idx += 1
                placeholders.append(f"${param_idx}")
                params.append(v)
            conditions.append(f"{col} IN ({', '.join(placeholders)})")

    if date_from or date_to:
        try:
            d1 = date.fromisoformat(date_from) if date_from else None
            d2 = date.fromisoformat(date_to) if date_to else None
        except ValueError:
            raise HTTPException(400, detail="Invalid date format. Use YYYY-MM-DD.")

        if d1 and d2:
            start, end = min(d1, d2), max(d1, d2)
            if start == end:
                param_idx += 1
                conditions.append(f"h.po_date = ${param_idx}")
                params.append(start)
            else:
                param_idx += 1
                sp = param_idx
                param_idx += 1
                ep = param_idx
                conditions.append(f"h.po_date >= ${sp} AND h.po_date <= ${ep}")
                params.append(start)
                params.append(end)
        elif d1:
            param_idx += 1
            conditions.append(f"h.po_date >= ${param_idx}")
            params.append(d1)
        else:
            param_idx += 1
            conditions.append(f"h.po_date <= ${param_idx}")
            params.append(d2)

    for col, val in [
        ("item_category", item_category),
        ("sub_category", sub_category),
        ("item_type", item_type),
    ]:
        if val:
            vals = [v.strip() for v in val.split(",") if v.strip()]
            placeholders = []
            for v in vals:
                param_idx += 1
                placeholders.append(f"${param_idx}")
                params.append(v)
            conditions.append(
                f"EXISTS (SELECT 1 FROM po_line pl"
                f" WHERE pl.transaction_no = h.transaction_no"
                f" AND pl.{col} IN ({', '.join(placeholders)}))"
            )

    where_clause = (" AND ".join(conditions)) if conditions else "TRUE"
    return where_clause, params


def _float_or_none(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _str_or_none(val) -> str | None:
    if val is None:
        return None
    return str(val)


def build_po_detail(header, lines, sections_by_line, boxes_by_line_section) -> dict:
    """Build a single POHeaderOut-compatible dict."""
    h = header
    txn_no = h["transaction_no"]

    line_dicts = []
    total_box_count = 0

    for l in lines:
        ln = l["line_number"]
        line_sections = sections_by_line.get(ln, [])
        line_box_count = 0

        section_dicts = []
        for s in line_sections:
            sn = s["section_number"]
            section_boxes = boxes_by_line_section.get((ln, sn), [])
            line_box_count += len(section_boxes)

            section_dicts.append({
                "transaction_no": txn_no,
                "line_number": ln,
                "section_number": sn,
                "lot_number": s.get("lot_number"),
                "box_count": int(s["box_count"]) if s.get("box_count") is not None else None,
                "manufacturing_date": s.get("manufacturing_date"),
                "expiry_date": s.get("expiry_date"),
                "total_boxes": len(section_boxes),
                "boxes": [
                    {
                        "box_id": b["box_id"],
                        "transaction_no": txn_no,
                        "line_number": ln,
                        "section_number": sn,
                        "box_number": b["box_number"],
                        "net_weight": _float_or_none(b.get("net_weight")),
                        "gross_weight": _float_or_none(b.get("gross_weight")),
                        "lot_number": b.get("lot_number"),
                        "count": int(b["count"]) if b.get("count") is not None else None,
                    }
                    for b in section_boxes
                ],
            })

        total_box_count += line_box_count

        line_dicts.append({
            "transaction_no": txn_no,
            "line_number": ln,
            "sku_name": l.get("sku_name"),
            "uom": l.get("uom"),
            "pack_count": int(l["pack_count"]) if l.get("pack_count") is not None else None,
            "po_weight": _float_or_none(l.get("po_weight")),
            "rate": _float_or_none(l.get("rate")),
            "amount": _float_or_none(l.get("amount")),
            "particulars": l.get("particulars"),
            "item_category": l.get("item_category"),
            "sub_category": l.get("sub_category"),
            "item_type": l.get("item_type"),
            "sales_group": l.get("sales_group"),
            "gst_rate": _float_or_none(l.get("gst_rate")),
            "match_score": _float_or_none(l.get("match_score")),
            "match_source": l.get("match_source"),
            "carton_weight": _float_or_none(l.get("carton_weight")),
            "status": l.get("status", "pending"),
            "total_sections": len(section_dicts),
            "total_boxes": line_box_count,
            "sections": section_dicts,
        })

    return {
        "transaction_no": txn_no,
        "entity": h.get("entity"),
        "po_date": _str_or_none(h.get("po_date")),
        "voucher_type": h.get("voucher_type"),
        "po_number": h.get("po_number"),
        "order_reference_no": h.get("order_reference_no"),
        "narration": h.get("narration"),
        "vendor_supplier_name": h.get("vendor_supplier_name"),
        "gross_total": _float_or_none(h.get("gross_total")),
        "total_amount": _float_or_none(h.get("total_amount")),
        "sgst_amount": _float_or_none(h.get("sgst_amount")),
        "cgst_amount": _float_or_none(h.get("cgst_amount")),
        "igst_amount": _float_or_none(h.get("igst_amount")),
        "round_off": _float_or_none(h.get("round_off")),
        "freight_transport_local": _float_or_none(h.get("freight_transport_local")),
        "apmc_tax": _float_or_none(h.get("apmc_tax")),
        "packing_charges": _float_or_none(h.get("packing_charges")),
        "freight_transport_charges": _float_or_none(h.get("freight_transport_charges")),
        "loading_unloading_charges": _float_or_none(h.get("loading_unloading_charges")),
        "other_charges_non_gst": _float_or_none(h.get("other_charges_non_gst")),
        "customer_party_name": h.get("customer_party_name"),
        "vehicle_number": h.get("vehicle_number"),
        "transporter_name": h.get("transporter_name"),
        "lr_number": h.get("lr_number"),
        "source_location": h.get("source_location"),
        "destination_location": h.get("destination_location"),
        "challan_number": h.get("challan_number"),
        "invoice_number": h.get("invoice_number"),
        "grn_number": h.get("grn_number"),
        "system_grn_date": _str_or_none(h.get("system_grn_date")),
        "purchased_by": h.get("purchased_by"),
        "inward_authority": h.get("inward_authority"),
        "warehouse": h.get("warehouse"),
        "status": h.get("status", "pending"),
        "approved_by": h.get("approved_by"),
        "approved_at": _str_or_none(h.get("approved_at")),
        "total_lines": len(lines),
        "total_boxes": total_box_count,
        "lines": line_dicts,
    }


async def fetch_po_details(
    pool, transaction_nos: list[str], headers: list
) -> list[dict]:
    """Fetch lines + sections + boxes for a batch of transactions and build detail dicts."""
    if not transaction_nos:
        return []

    lines = await pool.fetch(
        "SELECT * FROM po_line WHERE transaction_no = ANY($1) ORDER BY transaction_no, line_number",
        transaction_nos,
    )
    sections = await pool.fetch(
        "SELECT * FROM po_section WHERE transaction_no = ANY($1) ORDER BY transaction_no, line_number, section_number",
        transaction_nos,
    )
    boxes = await pool.fetch(
        "SELECT * FROM po_box WHERE transaction_no = ANY($1) ORDER BY transaction_no, line_number, section_number, box_number",
        transaction_nos,
    )

    lines_by_txn: dict[str, list] = {}
    for l in lines:
        lines_by_txn.setdefault(l["transaction_no"], []).append(l)

    sections_by_txn_line: dict[str, dict[int, list]] = {}
    for s in sections:
        txn = s["transaction_no"]
        ln = s["line_number"]
        sections_by_txn_line.setdefault(txn, {}).setdefault(ln, []).append(s)

    boxes_by_txn_line_section: dict[str, dict[tuple[int, int], list]] = {}
    for b in boxes:
        txn = b["transaction_no"]
        key = (b["line_number"], b["section_number"])
        boxes_by_txn_line_section.setdefault(txn, {}).setdefault(key, []).append(b)

    results = []
    for h in headers:
        txn_no = h["transaction_no"]
        txn_lines = lines_by_txn.get(txn_no, [])
        txn_sections = sections_by_txn_line.get(txn_no, {})
        txn_boxes = boxes_by_txn_line_section.get(txn_no, {})
        results.append(build_po_detail(h, txn_lines, txn_sections, txn_boxes))

    return results
