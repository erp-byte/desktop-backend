"""Column positions come from the header row, not from hardcoded indices.

Regression origin: 25082026POBOOK.xlsx (Aug 2026) omits the three optional
columns the Jan-Mar export carried -- "Terms of Payment", "Other References",
"Terms of Delivery" -- so every field from Quantity rightwards sits 3 columns
to the left:

    Jan-Mar 26 : [9]=Quantity [10]=Alt. Units [11]=Rate [12]=Value [13]=Gross Total
    Aug 26     : [6]=Quantity  [7]=Alt. Units  [8]=Rate  [9]=Value [10]=Gross Total

Reading fixed indices against the Aug file meant:
  * pack_count  <- the Value column      (CTC TEA stored 13000 "packs", the rupee value)
  * rate/amount <- empty GL columns      (357/357 lines came in with no money)
  * total_amount<- the SGST (ITC) column (PO 01228 stored 325, its SGST, not 13000)

Both layouts are real files in production, so both are pinned here.
"""
import io

import openpyxl
import pytest

from app.modules.purchase.services.parser import parse_po_book

# Exact header rows from the two workbooks.
AUG_2026 = [
    "Date", "Particulars", "Voucher Type", "Voucher No.", "Order Reference No.",
    "Narration", "Quantity", "Alt. Units", "Rate", "Value", "Gross Total",
    "Staff Welfare & Other Expenses", "SGST (ITC)", "CGST (ITC)",
    "Purchase of Raw Material - Local", "Purchase of Packing Material - Local",
    "IGST (ITC)", "Furniture & Fixtures - W202", "APMC Tax",
    "Packing Charges ( Purchase )",
]
JAN_MAR_2026 = [
    "Date", "Particulars", "Voucher Type", "Voucher No.", "Order Reference No.",
    "Narration", "Terms of Payment", "Other References", "Terms of Delivery",
    "Quantity", "Alt. Units", "Rate", "Value", "Gross Total", "Factory Building",
    "SGST (ITC)", "CGST (ITC)", "Round Off",
]


def _book(headers, header_cells, *line_cell_dicts):
    """Build a workbook whose row 12 is `headers`, addressing cells BY NAME."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for _ in range(11):
        ws.append([None])
    ws.append(list(headers))

    def row_for(mapping):
        row = [None] * len(headers)
        for name, value in mapping.items():
            row[headers.index(name)] = value
        return row

    ws.append(row_for(header_cells))
    for line in line_cell_dicts:
        ws.append(row_for(line))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# The real CF/PO/2026-27/01228 voucher: CTC TEA, qty 50 @ 260 = 13,000.
HEADER = {
    "Date": "1-Aug-26", "Particulars": "BEWIN COMMERCIAL OPERATIONS (INDIA)",
    "Voucher Type": "HO Purchase Order", "Voucher No.": "CF/PO/2026-27/01228",
    "Order Reference No.": "CF/PO/2026-27/01228",
    "Quantity": 50, "Value": 13000, "Gross Total": 13650,
    "SGST (ITC)": 325, "CGST (ITC)": 325,
}
LINE = {"Particulars": "CTC TEA", "Quantity": 50, "Rate": 260, "Value": 13000}


@pytest.fixture(params=[AUG_2026, JAN_MAR_2026], ids=["aug-2026", "jan-mar-2026"])
def parsed(request):
    """Same voucher, both column layouts — results must be identical."""
    pos = parse_po_book(_book(request.param, HEADER, LINE))
    assert len(pos) == 1, f"expected 1 PO, got {len(pos)}"
    return pos[0]


def test_line_rate_is_read(parsed):
    assert parsed["lines"][0]["rate"] == 260.0


def test_line_amount_is_read(parsed):
    assert parsed["lines"][0]["amount"] == 13000.0


def test_pack_count_is_the_quantity_not_the_value(parsed):
    """The bug that made 'Salt & Pepper Seasoning' show 17,750 packs."""
    assert parsed["lines"][0]["pack_count"] == 50


def test_sku_name_is_read(parsed):
    assert parsed["lines"][0]["sku_name"] == "CTC TEA"


def test_header_total_amount_is_the_value_not_the_sgst(parsed):
    assert parsed["total_amount"] == 13000.0


def test_header_identity_fields(parsed):
    assert parsed["po_number"] == "CF/PO/2026-27/01228"
    assert parsed["vendor_supplier_name"] == "BEWIN COMMERCIAL OPERATIONS (INDIA)"
    assert parsed["voucher_type"] == "HO Purchase Order"


@pytest.mark.parametrize("field,expected", [
    ("gross_total", 13650.0),
    ("sgst_amount", 325.0),
    ("cgst_amount", 325.0),
])
def test_gl_ledger_columns_map_by_name(parsed, field, expected):
    """GL columns move between exports too — Tally only emits ledgers actually
    used in the period, so their positions cannot be hardcoded either."""
    assert parsed[field] == expected


def test_rate_times_quantity_reconciles_to_amount(parsed):
    line = parsed["lines"][0]
    assert line["pack_count"] * line["rate"] == pytest.approx(line["amount"])


# ── fractional quantities ─────────────────────────────────────────────────
# Tally's Quantity is a decimal measure (kg), not a count of packs. The real
# CF/PO/2026-27/01231 is Rose Petals, 20.8 @ 690 = 14,352.

ROSE_HEADER = {
    "Date": "1-Aug-26", "Particulars": "Siddheshwar Spices",
    "Voucher Type": "HO Purchase Order", "Voucher No.": "CF/PO/2026-27/01231",
    "Quantity": 20.8, "Value": 14352, "Gross Total": 15069.6,
}
ROSE_LINE = {"Particulars": "Rose Petals", "Quantity": 20.8, "Rate": 690, "Value": 14352}


def _rose_line():
    pos = parse_po_book(_book(AUG_2026, ROSE_HEADER, ROSE_LINE))
    return pos[0]["lines"][0]


def test_fractional_quantity_is_not_truncated():
    """int() truncation turned 20.8 into 20 — a 0.8 kg shortfall on every
    fractional line, and any quantity under 1 into a literal 0."""
    assert _rose_line()["pack_count"] == 20.8


def test_fractional_quantity_survives_response_validation():
    """PreviewLine.pack_count must accept the decimal, or the whole preview
    500s with 'Input should be a valid integer'."""
    from app.modules.purchase.schemas.po_api import PreviewLine
    assert PreviewLine(**_rose_line()).pack_count == 20.8


def test_sub_unit_quantity_does_not_become_zero():
    line = parse_po_book(_book(
        AUG_2026, ROSE_HEADER,
        {"Particulars": "Labour Charges", "Quantity": 0.96, "Rate": 27000, "Value": 25920},
    ))[0]["lines"][0]
    assert line["pack_count"] == 0.96
