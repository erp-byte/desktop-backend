"""A workbook whose header row cannot be found must be REJECTED, not guessed at.

Reported symptom: the PO preview came back with cards reading

    PO Number 01-02-2021 · Supplier INATRB · Date ATTARI ROAD (INATRB) · Lines 0

Root cause: `_detect_columns` found no header row and silently fell back to a
hardcoded "Jan-Mar 2026" column map (date=0, particulars=1, voucher_no=3).
Against a workbook that never agreed to those positions:

  * `_is_header_row` fires on every row that has *anything* in column 0, so
    each data row minted its own PO;
  * `_is_line_row` can then never fire (a row is one or the other), so every
    PO came back with `lines: []` — "the SKU lines are not getting fetched";
  * the identity fields were read off whatever happened to sit at 0/1/3, which
    is how an address landed in the Date field.

Reproduced with a real export (inward_CFPL_20260923.xlsx): 1509 POs, 0 lines,
HTTP 200, no error shown to the user.

Silently mis-reading a workbook is worse than refusing it: preview feeds
commit, so the garbage is one click away from po_header/po_line.
"""
import io

import openpyxl
import pytest

from app.modules.purchase.services.parser import PoBookFormatError, parse_po_book

# The header row of a real ERP inward export — no "Particulars", no "Voucher".
# This is the workbook that produced the 1509-POs/0-lines preview.
INWARD_EXPORT = [
    "Source", "Warehouse", "Transaction No", "Entry Date", "Status",
    "Vehicle Number", "Transporter", "LR Number", "Vendor / Supplier",
    "Customer / Party", "Source Location", "Destination", "Challan Number",
    "Invoice Number", "PO Number", "GRN Number", "GRN Quantity",
    "System GRN Date", "Purchased By", "Unit Rate", "Item Description",
]

PO_BOOK = [
    "Date", "Particulars", "Voucher Type", "Voucher No.", "Order Reference No.",
    "Narration", "Quantity", "Alt. Units", "Rate", "Value", "Gross Total",
]
PO_HEADER = {
    "Date": "1-Aug-26", "Particulars": "BEWIN COMMERCIAL OPERATIONS (INDIA)",
    "Voucher Type": "HO Purchase Order", "Voucher No.": "CF/PO/2026-27/01228",
    "Quantity": 50, "Value": 13000,
}
PO_LINE = {"Particulars": "CTC TEA", "Quantity": 50, "Rate": 260, "Value": 13000}


def _book(headers, rows, *, letterhead_rows: int) -> bytes:
    """Workbook with `letterhead_rows` junk rows, then `headers`, then `rows`."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for _ in range(letterhead_rows):
        ws.append(["Candor Foods Private Limited"])
    ws.append(list(headers))
    for mapping in rows:
        row = [None] * len(headers)
        for name, value in mapping.items():
            row[headers.index(name)] = value
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_foreign_workbook_is_rejected_not_guessed():
    """The inward export is not a PO Book. Refuse it instead of inventing POs."""
    data = _book(
        INWARD_EXPORT,
        [{"Source": "Inward", "Warehouse": "A68", "Entry Date": "2026-09-23"}] * 5,
        letterhead_rows=0,
    )
    with pytest.raises(PoBookFormatError):
        parse_po_book(data)


def test_rejection_message_names_the_columns_it_needs():
    """The user has to be able to tell WHY their file was refused."""
    data = _book(INWARD_EXPORT, [{"Source": "Inward"}], letterhead_rows=0)
    with pytest.raises(PoBookFormatError) as excinfo:
        parse_po_book(data)
    msg = str(excinfo.value).lower()
    assert "particulars" in msg
    assert "quantity" in msg


def test_no_workbook_ever_parses_to_pos_with_zero_lines():
    """The exact reported shape: POs minted, every one of them line-less."""
    data = _book(
        INWARD_EXPORT,
        [{"Source": "Inward", "Warehouse": "A68"}] * 20,
        letterhead_rows=0,
    )
    try:
        pos = parse_po_book(data)
    except PoBookFormatError:
        return  # refused — correct
    assert not pos or any(po["lines"] for po in pos), (
        f"{len(pos)} POs parsed and not one has a line — the column map is wrong"
    )


@pytest.mark.parametrize("letterhead_rows", [0, 11, 14, 20, 30])
def test_po_book_is_found_under_a_letterhead_of_any_height(letterhead_rows):
    """Tally repeats the company letterhead above the header row, and its
    height varies with how many FSSAI/CIN/UDYAM lines the entity carries.
    A header row at row 22 is still a header row."""
    data = _book(PO_BOOK, [PO_HEADER, PO_LINE], letterhead_rows=letterhead_rows)
    pos = parse_po_book(data)
    assert len(pos) == 1
    assert pos[0]["po_number"] == "CF/PO/2026-27/01228"
    assert [l["sku_name"] for l in pos[0]["lines"]] == ["CTC TEA"]
    assert pos[0]["lines"][0]["rate"] == 260.0


# ── the refusal has to reach the user, not become a generic 500/400 ──────────


def test_preview_returns_an_actionable_400_not_a_generic_read_error():
    """`preview` wraps parser exceptions in a generic "Could not read Excel
    file" 400. That message is wrong here — the file read fine, it is simply
    not a PO Book — and it hides the one hint the user can act on."""
    import asyncio

    from app.core.middleware.request_context import AuthError
    from app.modules.purchase.services import po_preview

    data = _book(INWARD_EXPORT, [{"Source": "Inward"}], letterhead_rows=0)

    with pytest.raises(AuthError) as excinfo:
        # Parsing happens before any DB access, so no pool is needed.
        asyncio.run(po_preview.preview(
            None, file_bytes=data, filename="inward_CFPL_20260923.xlsx",
            master_items=[], entity="cfpl",
        ))

    err = excinfo.value
    assert err.status_code == 400
    assert err.code == "unrecognised_po_book"
    assert "particulars" in err.message.lower()
