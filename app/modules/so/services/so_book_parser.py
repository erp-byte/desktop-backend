"""
Parser for Sales Order Book Excel files (e.g. "Sales Order Book Jan-March 21 CFPL.xlsx").

Uses a state machine to group line items under their parent SO header.
GST is at the SO header level and gets apportioned to lines by value.
"""

import io
import logging
import re
from datetime import date, datetime

import openpyxl

from app.core.helpers import safe_float_zero as safe_float, safe_str

logger = logging.getLogger(__name__)

# Locations to strip from customer names
_LOCATIONS = (
    "maharashtra", "mah", "pune", "nashik", "mumbai", "navi mumbai", "thane", "nagpur",
    "telangana", "hyderabad", "karnataka", "bengaluru", "bangalore", "chennai", "tamil nadu",
    "ahmedabad", "gujarat", "rajasthan", "delhi", "ncr", "punjab", "haryana", "jharkhand",
    "kerala", "trichy", "andhra pradesh", "andra pradesh", "khurdha", "turbhe", "bhiwandi",
)
_LOCATION_PATTERN = re.compile(
    r"[\s,\-]*\b(?:" + "|".join(re.escape(loc) for loc in _LOCATIONS) + r")\b[\s,\-]*$",
    re.IGNORECASE,
)
_PAREN_PATTERN = re.compile(r"\s*\(.*?\)")


def _parse_date(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, date):
        return val.isoformat()
    s = str(val).strip()
    return s if s else None


def _clean_customer_name(name: str) -> str:
    """Derive common_customer_name from full customer name."""
    cleaned = _PAREN_PATTERN.sub("", name)
    cleaned = _LOCATION_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"[\s,\-]+$", "", cleaned).strip()
    return cleaned[:50]


def _is_header_row(row: tuple) -> bool:
    return row[0] is not None and str(row[0]).strip() != ""


def _is_line_row(row: tuple) -> bool:
    if row[0] is not None and str(row[0]).strip() != "":
        return False
    qty = row[9] if len(row) > 9 else None
    return qty is not None and str(qty).strip() != ""


def _is_grand_total_row(row: tuple) -> bool:
    particulars = safe_str(row[1]) if len(row) > 1 else None
    return particulars is not None and particulars.lower().startswith("grand total")


def parse_so_book(file_bytes: bytes) -> list[dict]:
    """
    Parse a Sales Order Book Excel file.
    Returns a list of SO dicts, each with a 'lines' array.
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active

    current_so = None
    all_orders = []
    warnings = []

    # Skip header block — data starts at row 13 (openpyxl 1-indexed)
    for row_idx, row in enumerate(ws.iter_rows(min_row=13, values_only=True), start=13):
        row_vals = tuple(row)

        # Skip grand total row
        if _is_grand_total_row(row_vals):
            continue

        if _is_header_row(row_vals):
            # Flush previous SO
            if current_so is not None:
                all_orders.append(current_so)

            customer_name = safe_str(row_vals[1]) or ""
            common_customer_name = _clean_customer_name(customer_name)
            igst = safe_float(row_vals[17])

            current_so = {
                "so_number": safe_str(row_vals[3]),
                "so_date": _parse_date(row_vals[0]),
                "customer_name": customer_name,
                "common_customer_name": common_customer_name,
                "company": "CFPL",
                "voucher_type": safe_str(row_vals[2]),
                "payment_terms": safe_str(row_vals[6]),
                "order_reference": safe_str(row_vals[4]),
                "narration": safe_str(row_vals[5]),
                "other_references": safe_str(row_vals[7]),
                "terms_of_delivery": safe_str(row_vals[8]),
                "is_interstate": igst > 0,
                "gross_total": safe_float(row_vals[13]),
                "sales_gst_local": safe_float(row_vals[14]),
                "cgst_amount": safe_float(row_vals[15]),
                "sgst_amount": safe_float(row_vals[16]),
                "igst_amount": igst,
                "packing_charges": safe_float(row_vals[18]),
                "round_off": safe_float(row_vals[19]),
                "freight_charges": safe_float(row_vals[20]),
                "sales_export": safe_float(row_vals[21]),
                "lines": [],
            }

        elif _is_line_row(row_vals):
            if current_so is None:
                warnings.append(f"Orphan line at row {row_idx}: {safe_str(row_vals[1])}")
                continue

            alt_units = safe_float(row_vals[10]) if row_vals[10] is not None and str(row_vals[10]).strip() != "" else None

            current_so["lines"].append({
                "sku_name": safe_str(row_vals[1]),
                "quantity": safe_float(row_vals[9]),
                "alt_units": alt_units,
                "uom": "CTN" if alt_units is not None else "PCS",
                "rate_inr": safe_float(row_vals[11]),
                "amount_inr": safe_float(row_vals[12]),
            })

    # Flush last SO
    if current_so is not None:
        all_orders.append(current_so)

    wb.close()

    # Apportion GST to lines and add line numbers
    for so in all_orders:
        lines = so["lines"]
        sum_values = sum(l["amount_inr"] for l in lines)

        for i, line in enumerate(lines, start=1):
            line["line_number"] = i
            if sum_values > 0:
                ratio = line["amount_inr"] / sum_values
                line["igst_amount"] = round(so["igst_amount"] * ratio, 3)
                line["sgst_amount"] = round(so["sgst_amount"] * ratio, 3)
                line["cgst_amount"] = round(so["cgst_amount"] * ratio, 3)
            else:
                line["igst_amount"] = 0.0
                line["sgst_amount"] = 0.0
                line["cgst_amount"] = 0.0
            line["total_amount_inr"] = round(
                line["amount_inr"] + line["igst_amount"] + line["sgst_amount"] + line["cgst_amount"], 3
            )

        so["total_line_items"] = len(lines)

    # Validation
    for so in all_orders:
        sn = so["so_number"]
        if len(so["lines"]) == 0:
            warnings.append(f"SO {sn} has no line items")
        if not so["gross_total"]:
            warnings.append(f"SO {sn} has zero gross total")
        line_sum = sum(l["amount_inr"] for l in so["lines"])
        if so["sales_gst_local"] and abs(line_sum - so["sales_gst_local"]) > 1:
            warnings.append(f"SO {sn}: line value sum ({line_sum:.2f}) != sales_gst_local ({so['sales_gst_local']:.2f})")
        if so["is_interstate"] and so["sgst_amount"] > 0:
            warnings.append(f"SO {sn}: GST type conflict — interstate but SGST > 0")

    for w in warnings:
        logger.warning("SO Book: %s", w)

    logger.info(
        "Parsed SO Book: %d SOs, %d total lines",
        len(all_orders), sum(len(so["lines"]) for so in all_orders),
    )

    return all_orders
