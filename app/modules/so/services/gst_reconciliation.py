import logging

from app.modules.so.services.item_matcher import MasterItem

logger = logging.getLogger(__name__)


def reconcile_line(
    line_data: dict,
    master_item: MasterItem | None,
) -> dict:
    """
    Run all GST reconciliation checks for a single line item.
    Returns a dict ready for INSERT into so_gst_reconciliation.
    """
    amount_inr = line_data.get("amount_inr") or 0
    igst = line_data.get("igst_amount") or 0
    sgst = line_data.get("sgst_amount") or 0
    cgst = line_data.get("cgst_amount") or 0
    apmc = line_data.get("apmc_amount") or 0
    packing = line_data.get("packing_amount") or 0
    freight = line_data.get("freight_amount") or 0
    processing = line_data.get("processing_amount") or 0
    total_with_gst = line_data.get("total_amount_inr") or 0
    excel_uom = line_data.get("uom")

    actual_gst = igst + sgst + cgst
    other_charges = apmc + packing + freight + processing
    notes_parts = []
    status = "ok"

    # 1. Compute actual GST rate
    actual_gst_rate = None
    if amount_inr and amount_inr != 0:
        actual_gst_rate = round(actual_gst / amount_inr, 3)

    # 2. Expected GST rate from master
    expected_gst_rate = None
    if master_item and master_item.gst is not None:
        expected_gst_rate = master_item.gst

    # 3. GST type: IGST vs SGST/CGST
    has_igst = igst > 0
    has_sgst_cgst = sgst > 0 or cgst > 0
    gst_type = "IGST" if has_igst else "SGST_CGST" if has_sgst_cgst else None

    # 4. GST type validity — should not have both IGST and SGST/CGST
    gst_type_valid = not (has_igst and has_sgst_cgst)
    if not gst_type_valid:
        notes_parts.append("Both IGST and SGST/CGST are non-zero")
        status = "mismatch"

    # 5. SGST == CGST check (only for intra-state)
    sgst_cgst_equal = None
    if has_sgst_cgst and not has_igst:
        sgst_cgst_equal = abs(sgst - cgst) < 0.01
        if not sgst_cgst_equal:
            notes_parts.append(f"SGST ({sgst}) != CGST ({cgst})")
            status = "mismatch"

    # 6. Total with GST check: amount_inr + gst + charges == total_amount_inr
    total_with_gst_valid = None
    if amount_inr and total_with_gst:
        expected_total = amount_inr + actual_gst + other_charges
        total_with_gst_valid = abs(expected_total - total_with_gst) <= 1.0
        if not total_with_gst_valid:
            charge_detail = ""
            if other_charges > 0:
                parts = []
                if apmc: parts.append(f"APMC={apmc}")
                if packing: parts.append(f"Packing={packing}")
                if freight: parts.append(f"Freight={freight}")
                if processing: parts.append(f"Processing={processing}")
                charge_detail = f" + Charges ({' + '.join(parts)}={other_charges})"
            notes_parts.append(
                f"With GST Amt ({total_with_gst}) != Without GST ({amount_inr}) + GST ({actual_gst}){charge_detail}"
            )
            status = "mismatch"

    # 7. UOM consistency.
    #
    # master_item.uom is a kg-per-unit factor (1.0 → rate per kg, 0.5 → half-kg
    # pack, etc.). Two cases for the Excel UOM cell:
    #
    #   a) Numeric (e.g. "0.5", "1") — compare it directly against the master
    #      factor.
    #   b) Alphabetical (e.g. "PCS", "CTN", "BOX") — string equality is
    #      meaningless against a float, so verify the underlying relationship
    #      instead: quantity_units (kg) ÷ master.uom (kg-per-unit) should equal
    #      the pack count (quantity).
    #
    # If we can't compute either form (master uom is 0 / missing, qty is 0,
    # or excel uom blank), uom_match stays None and the row's status is
    # untouched — neither a pass nor a fail, just absent from the validation
    # panel.
    uom_match = None
    if master_item and master_item.uom is not None:
        if excel_uom is not None:
            try:
                excel_uom_float = float(excel_uom)
                uom_match = abs(excel_uom_float - master_item.uom) < 0.001
                if not uom_match:
                    notes_parts.append(
                        f"UOM mismatch: Excel={excel_uom}, Master={master_item.uom}"
                    )
                    if status == "ok":
                        status = "warning"
            except (ValueError, TypeError):
                # Non-numeric label — fall back to the kg / pack-count check.
                # quantity is the pack count from the Sales Register; quantity_units
                # is the kg total. ingest_sales_register/so_book normalises both
                # into the line_data dict.
                quantity = line_data.get("quantity") or 0
                quantity_units = line_data.get("quantity_units") or 0
                if master_item.uom > 0 and quantity and quantity_units:
                    expected_units = quantity_units / master_item.uom
                    # < 0.5 packs of slack covers floating-point + Excel
                    # rounding without admitting genuine miscounts (pack
                    # counts are integers in the real world).
                    uom_match = abs(expected_units - quantity) < 0.5
                    if not uom_match:
                        notes_parts.append(
                            f"UOM check: {quantity_units} kg / {master_item.uom} kg-per-{excel_uom} "
                            f"= {expected_units:.2f} units (expected {quantity})"
                        )
                        if status == "ok":
                            status = "warning"
                # If qty is zero/blank or master.uom is 0 (packaging material
                # with no per-unit weight), we can't derive an expected pack
                # count — leave uom_match=None so the check is simply absent.

    # 8. Item type flag — warn if RM or PM being sold
    item_type_flag = None
    if master_item and master_item.item_type:
        it = master_item.item_type.lower()
        if it == "rm":
            item_type_flag = "RM_SOLD"
            notes_parts.append("Raw Material being sold")
            if status == "ok":
                status = "warning"
        elif it == "pm":
            item_type_flag = "PM_SOLD"
            notes_parts.append("Packaging Material being sold")
            if status == "ok":
                status = "warning"

    # 9. Rate type
    rate_type = None
    if master_item and master_item.uom is not None:
        rate_type = "per_kg" if master_item.uom == 1 else "per_unit"

    # Expected GST amount (if we had the rate)
    expected_gst_amount = None
    gst_difference = None
    if expected_gst_rate is not None and amount_inr:
        expected_gst_amount = round(amount_inr * expected_gst_rate, 3)
        gst_difference = round(actual_gst - expected_gst_amount, 3)
        if abs(gst_difference) > 1.0:
            notes_parts.append(
                f"GST amount mismatch: expected {expected_gst_amount}, actual {actual_gst}"
            )
            status = "mismatch"

    return {
        "expected_gst_rate": expected_gst_rate,
        "actual_gst_rate": actual_gst_rate,
        "expected_gst_amount": expected_gst_amount,
        "actual_gst_amount": actual_gst,
        "gst_difference": gst_difference,
        "gst_type": gst_type,
        "gst_type_valid": gst_type_valid,
        "sgst_cgst_equal": sgst_cgst_equal,
        "total_with_gst_valid": total_with_gst_valid,
        "uom_match": uom_match,
        "item_type_flag": item_type_flag,
        "rate_type": rate_type,
        # Matched master fields
        "matched_item_description": master_item.particulars if master_item else None,
        "matched_item_type": master_item.item_type if master_item else None,
        "matched_item_category": master_item.group if master_item else None,
        "matched_sub_category": master_item.sub_group if master_item else None,
        "matched_sales_group": master_item.sale_group if master_item else None,
        "matched_uom": master_item.uom if master_item else None,
        "match_score": None,  # filled by caller with the actual score
        "status": status,
        "notes": "; ".join(notes_parts) if notes_parts else None,
    }
