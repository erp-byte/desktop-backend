"""Purchase Order Book Excel parser — state-machine parser like so_book_parser."""

import io
import logging
import re
from datetime import date, datetime

import openpyxl

from app.core.helpers import safe_float_zero as _safe_float, safe_str as _safe_str

logger = logging.getLogger(__name__)


def _parse_date(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, date):
        return val.isoformat()
    s = str(val).strip()
    return s if s else None


def _is_header_row(row: tuple) -> bool:
    """Header row has a date in col 1 (col A)."""
    return row[0] is not None and str(row[0]).strip() != ""


def _is_line_row(row: tuple) -> bool:
    """Line item row: col A empty, but Particulars (col B) has a value and Quantity (col J) has a value."""
    if row[0] is not None and str(row[0]).strip() != "":
        return False
    particulars = row[1] if len(row) > 1 else None
    qty = row[9] if len(row) > 9 else None
    return particulars is not None and str(particulars).strip() != "" and qty is not None and str(qty).strip() != ""


def _is_grand_total_row(row: tuple) -> bool:
    particulars = _safe_str(row[1]) if len(row) > 1 else None
    return particulars is not None and particulars.lower().startswith("grand total")


# Excel column index → GL account field name mapping (0-indexed)
GL_COLUMN_MAP = {
    13: "gross_total",
    15: "sgst_amount",
    16: "cgst_amount",
    17: "round_off",
    20: "igst_amount",
    23: "packing_charges",
    26: "freight_transport_local",
    27: "apmc_tax",
    28: "other_charges_non_gst",
    40: "freight_transport_charges",
    42: "loading_unloading_charges",
}


def parse_po_book(file_bytes: bytes) -> list[dict]:
    """
    Parse a Purchase Order Book Excel file.
    Returns a list of PO dicts, each with a 'lines' array.
    Uses state-machine approach: header row starts a new PO, line rows add articles.
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active

    current_po = None
    all_orders = []

    # Data starts at row 13 (openpyxl 1-indexed), header info in rows 1-12
    for row_idx, row in enumerate(ws.iter_rows(min_row=13, values_only=True), start=13):
        row_vals = tuple(row)

        if _is_grand_total_row(row_vals):
            continue

        if _is_header_row(row_vals):
            # Flush previous PO
            if current_po is not None:
                all_orders.append(current_po)

            current_po = {
                "po_date": _parse_date(row_vals[0]),
                "vendor_supplier_name": _safe_str(row_vals[1]),
                "voucher_type": _safe_str(row_vals[2]),
                "po_number": _safe_str(row_vals[3]),
                "order_reference_no": _safe_str(row_vals[4]),
                "narration": _safe_str(row_vals[5]),
                "total_amount": _safe_float(row_vals[12]) if len(row_vals) > 12 else None,
                "lines": [],
            }

            # Extract GL account amounts from header row
            for col_idx, field_name in GL_COLUMN_MAP.items():
                if col_idx < len(row_vals):
                    val = _safe_float(row_vals[col_idx])
                    if val != 0:
                        current_po[field_name] = val

        elif _is_line_row(row_vals):
            if current_po is None:
                continue

            alt_units = _safe_float(row_vals[10]) if len(row_vals) > 10 and row_vals[10] is not None and str(row_vals[10]).strip() != "" else None

            current_po["lines"].append({
                "sku_name": _safe_str(row_vals[1]),
                "pack_count": int(_safe_float(row_vals[9])) if row_vals[9] is not None else None,
                "uom": str(alt_units) if alt_units is not None else None,
                "rate": _safe_float(row_vals[11]),
                "amount": _safe_float(row_vals[12]),
            })

    # Flush last PO
    if current_po is not None:
        all_orders.append(current_po)

    wb.close()

    # Add line numbers
    for po in all_orders:
        for i, line in enumerate(po["lines"], start=1):
            line["line_number"] = i

    logger.info(
        "Parsed PO Book: %d POs, %d total lines",
        len(all_orders), sum(len(po["lines"]) for po in all_orders),
    )

    return all_orders
