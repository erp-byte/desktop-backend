"""The PO parser must not source `uom` from the Excel.

`uom` is the per-unit weight in kg held by `all_sku.uom` (FRONTEND_API_DOC.md,
"What gets matched from all_sku": `po_weight = pack_count x all_sku.uom`).

Col K is Tally's "Alt. Units" -- a secondary *quantity*, not a unit weight. It
is populated on ~1.5% of lines with values like 20000.0 / 2880.0 / 750.0. While
the parser wrote that number into `uom`, the master-fill guard in
`_enrich_line_from_master` ("fill only when the parsed value is blank") saw a
non-empty string and refused to overwrite it, so all_sku could never supply the
unit weight on exactly those lines -- and po_weight became pack_count x 20000.
"""
import io

import openpyxl

from app.modules.purchase.services.parser import parse_po_book


def _book(*line_rows):
    """A minimal PO book: letterhead through row 11, header row 12, data from 13."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for _ in range(11):
        ws.append([None])
    ws.append(["Date", "Particulars", "Voucher Type", "Voucher No."])

    header = [None] * 13
    header[0] = "1-Apr-25"
    header[1] = "SOME VENDOR"
    header[2] = "Purchase Order"
    header[3] = "CF/PO/2025-26/00001"
    header[12] = 1000.0
    ws.append(header)

    for row in line_rows:
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _line_row(qty, alt_units, rate=25.0, amount=250.0, name="Deri Dates"):
    row = [None] * 13
    row[1] = name
    row[9] = qty
    row[10] = alt_units
    row[11] = rate
    row[12] = amount
    return row


def _only_line(*args, **kw):
    parsed = parse_po_book(_book(_line_row(*args, **kw)))
    assert len(parsed) == 1, f"expected 1 PO, got {len(parsed)}"
    assert len(parsed[0]["lines"]) == 1, "expected 1 line"
    return parsed[0]["lines"][0]


def test_uom_is_not_taken_from_the_alt_units_column():
    """The whole point: leave uom empty so all_sku fills it."""
    assert _only_line(10, 20000.0)["uom"] is None


def test_uom_is_none_even_when_alt_units_is_absent():
    assert _only_line(10, None)["uom"] is None


def test_alt_units_is_preserved_under_its_own_key():
    """Not discarded -- PreviewLine is extra='allow', so it rides along for
    anyone who wants Tally's secondary quantity."""
    assert _only_line(10, 20000.0)["alt_units"] == 20000.0


def test_alt_units_is_none_when_the_column_is_blank():
    assert _only_line(10, None)["alt_units"] is None


def test_alt_units_does_not_swallow_a_text_cell_as_zero():
    """safe_float_zero turns junk into 0.0; alt_units is informational, so a
    zero here must not read as 'the secondary quantity was zero'."""
    assert _only_line(10, "Kgs")["alt_units"] is None


def test_the_other_parsed_line_fields_are_untouched():
    line = _only_line(10, 20000.0, rate=25.0, amount=250.0)
    assert line["sku_name"] == "Deri Dates"
    assert line["rate"] == 25.0
    assert line["amount"] == 250.0
