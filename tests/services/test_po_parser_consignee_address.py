"""Consignee Address: an optional workbook column beside Particulars, read per PO.

It is a voucher-level field like the supplier, so it comes off the voucher
row; a line row only fills it when the voucher row left it blank. Workbooks
without the column still parse, with the address left None.
"""
import pytest

from app.modules.purchase.services.parser import parse_po_book
from tests.services.test_po_parser_columns import AUG_2026, HEADER, LINE, _book

WITH_ADDRESS = AUG_2026[:2] + ["Consignee Address"] + AUG_2026[2:]
ADDRESS = "Plot 12, MIDC Turbhe, Navi Mumbai 400705"


def _parse(headers, header_cells, *lines):
    pos = parse_po_book(_book(headers, header_cells, *lines))
    assert len(pos) == 1
    return pos[0]


def test_address_read_from_voucher_row():
    po = _parse(WITH_ADDRESS, {**HEADER, "Consignee Address": ADDRESS}, LINE)
    assert po["consignee_address"] == ADDRESS


def test_address_does_not_disturb_other_columns():
    po = _parse(WITH_ADDRESS, {**HEADER, "Consignee Address": ADDRESS}, LINE)
    assert po["vendor_supplier_name"] == HEADER["Particulars"]
    assert po["lines"][0]["sku_name"] == "CTC TEA"
    assert po["lines"][0]["pack_count"] == 50


def test_address_falls_back_to_first_line_that_has_one():
    po = _parse(WITH_ADDRESS, HEADER,
                LINE, {**LINE, "Consignee Address": ADDRESS},
                {**LINE, "Consignee Address": "somewhere else"})
    assert po["consignee_address"] == ADDRESS


def test_voucher_row_address_wins_over_lines():
    po = _parse(WITH_ADDRESS, {**HEADER, "Consignee Address": ADDRESS},
                {**LINE, "Consignee Address": "somewhere else"})
    assert po["consignee_address"] == ADDRESS


def test_workbook_without_the_column_still_parses():
    po = _parse(AUG_2026, HEADER, LINE)
    assert po["consignee_address"] is None
    assert len(po["lines"]) == 1


@pytest.mark.parametrize("other", ["Consignee Name", "Consignee GSTIN"])
def test_other_consignee_columns_are_not_taken_as_the_address(other):
    headers = AUG_2026[:2] + [other, "Consignee Address"] + AUG_2026[2:]
    po = _parse(headers, {**HEADER, other: "not an address",
                          "Consignee Address": ADDRESS}, LINE)
    assert po["consignee_address"] == ADDRESS
