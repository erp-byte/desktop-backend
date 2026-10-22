"""The Oct 2026 PO Book export (22102026POBOOK.xlsx).

New against Aug 2026: a "Consignee Address" column between Particulars and
Voucher Type, round-off headed "Rounding Off" (was "Round Off"), and a
cancelled voucher printed as a bare "(cancelled )" row with no lines.
"""
import pytest

from app.modules.purchase.services.parser import parse_po_book
from tests.services.test_po_parser_columns import _book

# Exact header row from the workbook (first 22 of its 42 columns).
OCT_2026 = [
    "Date", "Particulars", "Consignee Address", "Voucher Type", "Voucher No.",
    "Order Reference No.", "Narration", "Quantity", "Alt. Units", "Rate", "Value",
    "Gross Total", "Purchase of Raw Material - Local", "SGST (ITC)", "CGST (ITC)",
    "Purchase of Packing Material - Local", "IGST (ITC)", "APMC Tax", "Rounding Off",
    "Packing Charges ( Purchase )", "Electrical Installations & Equipments",
    "Transportation Charges - Purchase",
]
W202 = ("W-202(A), MIDC,, TTC Industrial Area, Khairne,, Navi Mumbai - 400710, "
        "FSSAI NO. 10016022005363, FSSAI NO. 11521998000014, E-Mail : contact@candorfoods.in")
A185 = ("A-185 , MIDC TTC Industrial Area,, Khairane, Thane Belapur Road,, "
        "Navi Mumbai 400709, FSSAI NO. 10016022005363, FSSAI NO. 11521998000014")

# Real vouchers: CF/PO/2026-27/01815 (SAI GAJANAN CO) and 01886 (cancelled).
SAI_GAJANAN = {
    "Date": "1-Oct-26", "Particulars": "SAI GAJANAN CO", "Consignee Address": A185,
    "Voucher Type": "HO Purchase Order", "Voucher No.": "CF/PO/2026-27/01815",
    "Order Reference No.": "CF/PO/2026-27/01815", "Quantity": 1985.9,
    "Value": 1046569.3, "Gross Total": 1098898,
    "Purchase of Raw Material - Local": 1046569.3,
    "SGST (ITC)": 26164.23, "CGST (ITC)": 26164.23, "Rounding Off": 0.24,
}
SAI_LINE = {"Particulars": "Water Melon Seed", "Quantity": 1985.9, "Rate": 527, "Value": 1046569.3}
CANCELLED = {
    "Date": "10-Oct-26", "Particulars": "(cancelled )", "Voucher Type": "HO Purchase Order",
    "Voucher No.": "CF/PO/2026-27/01886", "Order Reference No.": "CF/PO/2026-27/01886",
}
MEHTA = {
    "Date": "12-Oct-26", "Particulars": "Mehta Enterprises", "Consignee Address": W202,
    "Voucher Type": "HO Purchase Order", "Voucher No.": "CF/PO/2026-27/01887",
    "Quantity": 16, "Value": 22800, "Gross Total": 27140,
}
MEHTA_LINE = {"Particulars": "Test Sieve Mesh", "Quantity": 2, "Rate": 1200, "Value": 2400}


def _parse(*rows):
    header, *rest = rows
    return parse_po_book(_book(OCT_2026, header, *rest))


def test_consignee_address_is_read():
    po = _parse(SAI_GAJANAN, SAI_LINE)[0]
    assert po["consignee_address"] == A185


def test_columns_after_the_address_are_not_shifted():
    po = _parse(SAI_GAJANAN, SAI_LINE)[0]
    assert po["vendor_supplier_name"] == "SAI GAJANAN CO"
    assert po["voucher_type"] == "HO Purchase Order"
    assert po["po_number"] == "CF/PO/2026-27/01815"
    assert po["total_amount"] == 1046569.3
    line = po["lines"][0]
    assert (line["sku_name"], line["pack_count"], line["rate"], line["amount"]) == \
        ("Water Melon Seed", 1985.9, 527.0, 1046569.3)


@pytest.mark.parametrize("field,expected", [
    ("gross_total", 1098898.0),
    ("sgst_amount", 26164.23),
    ("cgst_amount", 26164.23),
    ("round_off", 0.24),          # headed "Rounding Off" in this export
])
def test_gl_columns(field, expected):
    assert _parse(SAI_GAJANAN, SAI_LINE)[0][field] == expected


def test_cancelled_voucher_is_skipped():
    pos = _parse(SAI_GAJANAN, SAI_LINE, CANCELLED, MEHTA, MEHTA_LINE, MEHTA_LINE)
    assert [p["po_number"] for p in pos] == ["CF/PO/2026-27/01815", "CF/PO/2026-27/01887"]
    assert all(p["vendor_supplier_name"] != "(cancelled )" for p in pos)


def test_neighbours_of_a_cancelled_voucher_keep_their_lines():
    pos = _parse(SAI_GAJANAN, SAI_LINE, CANCELLED, MEHTA, MEHTA_LINE, MEHTA_LINE)
    assert len(pos[0]["lines"]) == 1
    assert len(pos[1]["lines"]) == 2
    assert pos[1]["consignee_address"] == W202


def test_cancelled_voucher_first_in_the_sheet():
    pos = _parse(CANCELLED, MEHTA, MEHTA_LINE)
    assert [p["po_number"] for p in pos] == ["CF/PO/2026-27/01887"]
