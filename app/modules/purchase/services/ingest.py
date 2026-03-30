"""PO ingestion — create po_header + po_line from parsed Excel + all_sku matching."""

import logging
from datetime import date as date_type, datetime, timedelta

from app.modules.so.services.item_matcher import MasterItem, match_sku
from app.modules.purchase.services.parser import parse_po_book

logger = logging.getLogger(__name__)


async def ingest_po_book(
    pool,
    file_bytes: bytes,
    master_items: list[MasterItem],
    entity: str,
) -> dict:
    """
    Parse PO Book Excel, create po_header + po_line rows with all_sku matching.
    """
    parsed_orders = parse_po_book(file_bytes)

    all_details = []
    total_lines = 0
    total_matched = 0
    total_unmatched = 0

    # Generate unique transaction_no: TR-YYYYMMDDHHMMSS, +1 sec per PO
    base_time = datetime.now()

    async with pool.acquire() as conn:
        async with conn.transaction():
            for po_idx, po in enumerate(parsed_orders, start=0):
                po_number = po.get("po_number") or f"PO-{po_idx + 1}"
                txn_time = base_time + timedelta(seconds=po_idx)
                transaction_no = f"TR-{txn_time.strftime('%Y%m%d%H%M%S')}"

                po_date = None
                raw_date = po.get("po_date")
                if raw_date:
                    try:
                        po_date = date_type.fromisoformat(raw_date)
                    except (ValueError, TypeError):
                        pass

                await conn.execute(
                    """
                    INSERT INTO po_header (
                        transaction_no, entity, po_date, voucher_type, po_number,
                        order_reference_no, narration, vendor_supplier_name,
                        gross_total, total_amount,
                        sgst_amount, cgst_amount, igst_amount, round_off,
                        freight_transport_local, apmc_tax, packing_charges,
                        freight_transport_charges, loading_unloading_charges,
                        other_charges_non_gst
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8,
                        $9, $10,
                        $11, $12, $13, $14,
                        $15, $16, $17,
                        $18, $19,
                        $20
                    )
                    """,
                    transaction_no,
                    entity.lower(),
                    po_date,
                    po.get("voucher_type"),
                    po_number,
                    po.get("order_reference_no"),
                    po.get("narration"),
                    po.get("vendor_supplier_name"),
                    po.get("gross_total"),
                    po.get("total_amount"),
                    po.get("sgst_amount"),
                    po.get("cgst_amount"),
                    po.get("igst_amount"),
                    po.get("round_off"),
                    po.get("freight_transport_local"),
                    po.get("apmc_tax"),
                    po.get("packing_charges"),
                    po.get("freight_transport_charges"),
                    po.get("loading_unloading_charges"),
                    po.get("other_charges_non_gst"),
                )

                line_dicts = []

                for line in po.get("lines", []):
                    article = line.get("sku_name")
                    matched_item, score = match_sku(article, master_items) if article else (None, 0.0)

                    if matched_item:
                        total_matched += 1
                    else:
                        total_unmatched += 1

                    pack_count = line.get("pack_count")
                    po_weight = None
                    uom_val = None
                    if matched_item and matched_item.uom is not None:
                        uom_val = matched_item.uom
                        if pack_count is not None and uom_val > 0:
                            po_weight = round(pack_count * uom_val, 3)

                    uom_str = line.get("uom") or (str(uom_val) if uom_val is not None else None)

                    await conn.execute(
                        """
                        INSERT INTO po_line (
                            transaction_no, line_number, sku_name, uom,
                            pack_count, po_weight, rate, amount,
                            particulars, item_category, sub_category, item_type,
                            sales_group, gst_rate, match_score, match_source
                        )
                        VALUES (
                            $1, $2, $3, $4,
                            $5, $6, $7, $8,
                            $9, $10, $11, $12,
                            $13, $14, $15, $16
                        )
                        """,
                        transaction_no,
                        line.get("line_number", 1),
                        article,
                        uom_str,
                        pack_count,
                        po_weight,
                        line.get("rate"),
                        line.get("amount"),
                        matched_item.particulars if matched_item else None,
                        matched_item.group if matched_item else None,
                        matched_item.sub_group if matched_item else None,
                        matched_item.item_type if matched_item else None,
                        matched_item.sale_group if matched_item else None,
                        matched_item.gst if matched_item else None,
                        score if matched_item else None,
                        "all_sku" if matched_item else None,
                    )
                    total_lines += 1

                    line_dicts.append({
                        "transaction_no": transaction_no,
                        "line_number": line.get("line_number", 1),
                        "sku_name": article,
                        "uom": uom_str,
                        "pack_count": pack_count,
                        "po_weight": po_weight,
                        "rate": line.get("rate"),
                        "amount": line.get("amount"),
                        "particulars": matched_item.particulars if matched_item else None,
                        "item_category": matched_item.group if matched_item else None,
                        "sub_category": matched_item.sub_group if matched_item else None,
                        "item_type": matched_item.item_type if matched_item else None,
                        "sales_group": matched_item.sale_group if matched_item else None,
                        "gst_rate": matched_item.gst if matched_item else None,
                        "match_score": score if matched_item else None,
                        "match_source": "all_sku" if matched_item else None,
                        "status": "pending",
                        "total_sections": 0,
                        "total_boxes": 0,
                        "sections": [],
                    })

                all_details.append({
                    "transaction_no": transaction_no,
                    "entity": entity.lower(),
                    "po_date": po.get("po_date"),
                    "voucher_type": po.get("voucher_type"),
                    "po_number": po_number,
                    "order_reference_no": po.get("order_reference_no"),
                    "narration": po.get("narration"),
                    "vendor_supplier_name": po.get("vendor_supplier_name"),
                    "gross_total": po.get("gross_total"),
                    "total_amount": po.get("total_amount"),
                    "sgst_amount": po.get("sgst_amount"),
                    "cgst_amount": po.get("cgst_amount"),
                    "igst_amount": po.get("igst_amount"),
                    "round_off": po.get("round_off"),
                    "freight_transport_local": po.get("freight_transport_local"),
                    "apmc_tax": po.get("apmc_tax"),
                    "packing_charges": po.get("packing_charges"),
                    "freight_transport_charges": po.get("freight_transport_charges"),
                    "loading_unloading_charges": po.get("loading_unloading_charges"),
                    "other_charges_non_gst": po.get("other_charges_non_gst"),
                    "status": "pending",
                    "total_lines": len(line_dicts),
                    "total_boxes": 0,
                    "lines": line_dicts,
                })

    logger.info(
        "PO Book ingested: %d POs, %d lines (%d matched, %d unmatched)",
        len(all_details), total_lines, total_matched, total_unmatched,
    )

    return {
        "summary": {
            "total_transactions": len(all_details),
            "total_lines": total_lines,
            "total_boxes": 0,
            "total_amount": sum(d.get("gross_total") or 0 for d in all_details),
        },
        "transactions": all_details,
    }
