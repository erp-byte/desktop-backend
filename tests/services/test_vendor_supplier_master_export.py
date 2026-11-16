"""GET /api/v1/vendors/export: every vendor laid out as the company's
"MIS Master - Suppliers data" workbook (51 columns, sheets "Registered" and
"INACTIVE").

Three layers, and none of them touches a database:

  * export_xlsx     the openpyxl builder: headers, widths, styling, cell typing
  * export_service  build_export_rows, the pure vendor_master + lookup + banking +
                    document + contract -> sheet-row mapping, and the five reads
                    behind it against a fake connection
  * the route       through the real vendor router and its auth dependency; the
                    token round trip and the permission lookup are stubbed one
                    level lower, as test_cr_dates_read does

Every vendor in here is invented ("Test Vendor 1"). The company's own workbook
holds real supplier data and is not committed: the conformance test reads its
header row and column widths only, and skips when the file is absent.

Run:  PYTHONPATH=. python -m pytest tests/services/test_vendor_supplier_master_export.py
"""
from __future__ import annotations

import asyncio
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import MappingProxyType

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.core.middleware import request_context
from app.modules.auth.middleware import AuthUser
from app.modules.vendor import router as R
from app.modules.vendor.services import (
    banking_service,
    contract_service,
    document_service,
    export_service,
    export_xlsx,
    vendor_service,
)

IST = timezone(timedelta(hours=5, minutes=30))
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BACKEND_ROOT = Path(__file__).resolve().parents[2]
#: The company's workbook, when someone has dropped a copy at the backend root.
REFERENCE_GLOB = "001_A_MIS Master_Suppliers_data_*.xlsx"

#: key -> 1-based column number, and key -> column letter.
COL = {key: i for i, (key, _h, _w, _a) in enumerate(export_xlsx.COLUMNS, start=1)}
LETTER = {key: get_column_letter(i) for key, i in COL.items()}
SHEET_KEYS = [key for key in COL if key != "sr_no"]


def _cols(block: str) -> list[str]:
    """A service's column-list constant ("a, b,\\n c") as names."""
    return [c.strip() for c in block.split(",") if c.strip()]


# ── Invented rows, shaped like the tables they stand in for ──────────────────
def _vendor(n: int, **over) -> dict:
    """A vendor_master row: every column the vendor service reads, NULL unless set."""
    row = dict.fromkeys(_cols(vendor_service._VENDOR_COLUMNS))
    row.update(vendor_id=f"v{n}", supplier_code=str(n), name=f"Test Vendor {n}",
               status="active", is_msme=False, is_deleted=False)
    row.update(over)
    return row


def _lk(lookup_id: str, lookup_type: str, code: str, label: str) -> dict:
    return {"lookup_id": lookup_id, "lookup_type": lookup_type, "code": code, "label": label,
            "parent_lookup_id": None, "display_order": 0, "is_active": True,
            "effective_from": None, "effective_to": None, "created_at": None}


_bank_seq = iter(range(1, 10_000))


def _bank(vendor_id: str, **over) -> dict:
    row = dict.fromkeys(_cols(banking_service._BANK_COLUMNS))
    row.update(bank_id=f"b{next(_bank_seq)}", vendor_id=vendor_id, bank_name="Test Bank",
               account_no="1234567890", account_name="Test Account", is_primary=False,
               is_active=True)
    row.update(over)
    return row


def _doc(vendor_id: str, doc_type: str | None) -> dict:
    row = dict.fromkeys(_cols(document_service._DOC_COLUMNS))
    row.update(vendor_id=vendor_id, doc_type=doc_type)
    return row


def _contract(vendor_id: str, signed_date: date | None = None,
              effective_from: date | None = None) -> dict:
    row = dict.fromkeys(_cols(contract_service._CONTRACT_COLUMNS))
    row.update(vendor_id=vendor_id, signed_date=signed_date, effective_from=effective_from)
    return row


LOOKUPS = [
    _lk("st-norm", "SUPPLIER_TYPE", "NORMAL_SUPPLIER", "Normal Supplier"),
    _lk("st-oth", "SUPPLIER_TYPE", "OTHERS", "Others"),
    _lk("fs-pvt", "FIRM_STATUS", "PRIVATE_LTD", "Private Ltd Company"),
    _lk("fs-oth", "FIRM_STATUS", "OTHERS", "Others"),
    _lk("bt-mfr", "BUSINESS_TYPE", "MANUFACTURER", "Manufacturer"),
    _lk("bt-oth", "BUSINESS_TYPE", "MISC", "other"),          # "Other" by label only
    _lk("cat-rm", "CATEGORY_CODE", "RAW_MATERIAL", "Raw Material"),
    _lk("los-local", "LOCAL_OS", "LOCAL", "Local"),
    _lk("los-import", "LOCAL_OS", "IMPORT", "Import (OS)"),
    _lk("los-os", "LOCAL_OS", "OS", "Outstation"),
    _lk("los-label-os", "LOCAL_OS", "L1", "Out of state / OS"),
    _lk("los-label-imp", "LOCAL_OS", "L2", "Imported goods"),
    _lk("los-sez", "LOCAL_OS", "SEZ", "Sez unit"),
    _lk("los-cost", "LOCAL_OS", "L3", "Cost centre"),         # "os" inside a word is not OS
    _lk("msme-micro", "MSME_TYPE", "MICRO", "Micro"),
    _lk("acct-cur", "ACCOUNT_TYPE", "CURRENT", "Current"),
    _lk("acct-sav", "ACCOUNT_TYPE", "SAVINGS", "Savings"),
    _lk("kyc-ver", "KYC_STATUS", "VERIFIED", "Verified"),
    _lk("kyc-pen", "KYC_STATUS", "PENDING", "Pending"),
    _lk("doc-rev", "DOC_STATUS", "IN_REVIEW", "In Review"),
]


def _rows(vendors, banking=(), documents=(), contracts=(), lookups=LOOKUPS) -> list[dict]:
    return export_service.build_export_rows(
        list(vendors), list(lookups), list(banking), list(documents), list(contracts))


def _one(vendor, **tables) -> dict:
    [row] = _rows([vendor], **tables)
    return row


def _sheet_row(**over) -> dict:
    """One row as build_export_rows hands it to the workbook builder."""
    row = dict.fromkeys(SHEET_KEYS)
    row.update(supplier_code=1, name="Test Vendor 1", status="Active", _status="active")
    row.update(over)
    return row


def _book(rows: list[dict]):
    return load_workbook(export_xlsx.build_supplier_master_workbook(rows))


def _widths(ws) -> dict[int, float]:
    """Column number -> width. One <col> may span a run of columns (min..max)."""
    out: dict[int, float] = {}
    for dim in ws.column_dimensions.values():
        for i in range(dim.min, dim.max + 1):
            out[i] = dim.width
    return out


# ── Layout ───────────────────────────────────────────────────────────────────
def test_there_are_51_columns_ending_at_ay():
    assert len(export_xlsx.HEADERS) == 51
    assert export_xlsx.HEADERS == [header for _k, header, _w, _a in export_xlsx.COLUMNS]
    assert len(COL) == 51                                   # the keys are unique
    assert (LETTER["sr_no"], LETTER["name"], LETTER["tin_tan"]) == ("A", "H", "AY")
    assert export_xlsx.SHEET_REGISTERED == "Registered"
    assert export_xlsx.SHEET_INACTIVE == "INACTIVE"
    # The reference's own oddities are kept verbatim: a trailing space, double
    # spaces, and line breaks inside a header.
    for odd in ("Beneficiary Bank ", "Beneficiary Bank Account  No.", "IFSC  Code",
                "FILE  (Hard Copy)", "UAM No.\nUDYAM-", "Import / Export Reg.\nIEC",
                "Business Turnover\n(Last three Years)",
                "Reference\n(whoever referred, need to mention)",
                "Pollution\n/ EPR Registration"):
        assert odd in export_xlsx.HEADERS


#: Which value sits under which header, written out independently of COLUMNS. Every
#: other test finds its cell through COLUMNS itself, so without this literal two
#: keys could swap (account numbers under "IFSC Code") with everything still green,
#: and the header text would be checked only where the company workbook is present.
EXPECTED_PAIRS = [
    ("sr_no", "Sr.No."),
    ("category", "Category Code"),
    ("sub_category", "Sub Category (details of item which is supplied)"),
    ("local_os", "Local /OS"),
    ("supplier_code", "Supplier Code"),
    ("supplier_reg_year", "Supplier Reg. Year"),
    ("supplier_type", "Supplier Type"),
    ("name", "Name of the Supplier Company"),
    ("status", "STATUS (ACTIVE / IN ACTIVE)/ Black Listed"),
    ("firm_status", "FIRM STATUS / Constitution of Business"),
    ("business_type", "BUSINESS TYPE"),
    ("core_business", "Core Business / Main Products / Business Details"),
    ("contact_person", "Contact Person"),
    ("designation", "Designation of person"),
    ("address", "Contact Address"),
    ("state", "State"),
    ("city", "City (District)"),
    ("pin_code", "PIN Code"),
    ("phone_company", "Contact Details -Company"),
    ("mobile", "Contact Mobile Nos."),
    ("email", "E-mail"),
    ("website", "Website"),
    ("fssai", "FSSAI"),
    ("brc_other", "BRC / Others"),
    ("cin", "CIN No."),
    ("pan", "PAN No"),
    ("gstn", "GSTN"),
    ("msme", "MSME (Yes / No)"),
    ("msme_type", "MSME TYPE"),
    ("udyam", "UAM No.\nUDYAM-"),
    ("bank_name", "Beneficiary Bank "),
    ("bank_account_no", "Beneficiary Bank Account  No."),
    ("bank_account_name", "Beneficiary Account Name"),
    ("bank_branch", "Branch Name & Address"),
    ("bank_ifsc", "IFSC  Code"),
    ("bank_swift", "SWIFT Code"),
    ("scoc", "SCOC-SUPPLY CODE OF CONDUCT"),
    ("kyc", "KYC Submitted"),
    ("bank_account_type", "Type of Account (Saving / Current / CC)"),
    ("iec", "Import / Export Reg.\nIEC"),
    ("turnover", "Business Turnover\n(Last three Years)"),
    ("capabilities", "Supplier Capabilities"),
    ("remarks", "REMARKS"),
    ("contract_signed", "Contract AG Signed (Y/N)"),
    ("contract_dated", "Contract AG dated"),
    ("documentation", "Documentation Received"),
    ("performance", "Performance of Supplier (including documentation)"),
    ("hard_copy", "FILE  (Hard Copy)"),
    ("reference", "Reference\n(whoever referred, need to mention)"),
    ("pollution_epr", "Pollution\n/ EPR Registration"),
    ("tin_tan", "TIN/ TAN / Other Registrations (pl mention)"),
]


def test_each_value_sits_under_its_own_header():
    assert [(key, header) for key, header, _w, _a in export_xlsx.COLUMNS] == EXPECTED_PAIRS
    # ...and end to end: a distinct value per key, read back by header text.
    row = {key: f"value of {key}" for key in SHEET_KEYS}
    row.update(contract_dated=date(2026, 2, 15), _status="active")
    ws = _book([row])["Registered"]
    by_header = {cell.value: ws.cell(row=2, column=cell.column).value for cell in ws[1]}
    for key, header in EXPECTED_PAIRS:
        if key == "sr_no":
            assert by_header[header] == 1
        elif key == "contract_dated":
            assert by_header[header].date() == date(2026, 2, 15)
        else:
            assert by_header[header] == f"value of {key}", header


def test_an_empty_export_is_both_sheets_with_the_header_row_only():
    wb = _book([])
    assert wb.sheetnames == ["Registered", "INACTIVE"]
    for ws in wb.worksheets:
        assert [c.value for c in ws[1]] == export_xlsx.HEADERS
        assert ws.max_row == 1 and ws.max_column == 51
        assert ws.freeze_panes == "A2"
        assert ws.auto_filter.ref == "A1:AY1"
        assert not ws.data_validations.dataValidation
        assert not list(ws.conditional_formatting)


def test_headers_and_widths_match_the_company_workbook():
    paths = sorted(BACKEND_ROOT.glob(REFERENCE_GLOB))
    if not paths:
        pytest.skip("the reference workbook is not committed; drop a copy at the backend root")
    for path in paths:
        ws = load_workbook(path)["Registered"]
        assert [c.value for c in ws[1]] == export_xlsx.HEADERS, path.name
        reference = _widths(ws)
        # _widths expands the sheet's column runs: the workbook stores AI:AK as
        # one 14.66-wide run, which openpyxl reports on AI alone.
        for key, _header, width, _align in export_xlsx.COLUMNS:
            assert width == round(reference[COL[key]], 1), (path.name, LETTER[key])


def test_column_widths_freeze_and_filter():
    wb = _book([_sheet_row(), _sheet_row(supplier_code=2), _sheet_row(supplier_code=3)])
    for ws in wb.worksheets:
        widths = _widths(ws)
        assert [widths[COL[key]] for key in COL] == [w for _k, _h, w, _a in export_xlsx.COLUMNS]
        assert ws.freeze_panes == "A2"
    assert wb["Registered"].auto_filter.ref == "A1:AY4"
    assert wb["INACTIVE"].auto_filter.ref == "A1:AY1"


def test_header_row_styling():
    wb = _book([_sheet_row()])
    for ws in wb.worksheets:
        # No fixed height: Excel auto-fits the wrapped header, as it does for the
        # company workbook. A fixed 58.2 clipped the tallest header at 100% scale.
        assert ws.row_dimensions[1].height is None
        for cell in ws[1]:
            assert cell.font.name == "Calibri" and cell.font.sz == 11 and cell.font.b is True
            assert cell.font.color.rgb.endswith("FFFFFF")
            assert cell.fill.fill_type == "solid" and cell.fill.fgColor.rgb.endswith("305496")
            a = cell.alignment
            assert (a.horizontal, a.vertical, a.wrap_text) == ("center", "center", True)
            b = cell.border
            assert (b.left.style, b.right.style, b.top.style, b.bottom.style) == ("medium",) * 4


def test_every_data_cell_is_styled_even_an_empty_one():
    # The second row has nothing but its Sr.No.: 50 empty cells that still carry the grid.
    wb = _book([_sheet_row(), dict.fromkeys(SHEET_KEYS)])
    ws = wb["Registered"]
    assert ws.max_row == 3
    for r in (2, 3):
        assert ws.row_dimensions[r].height is None          # Excel auto-fits wrapped rows
        for key, _header, _width, halign in export_xlsx.COLUMNS:
            cell = ws.cell(row=r, column=COL[key])
            where = (r, LETTER[key])
            assert cell.font.name == "Calibri" and cell.font.sz == 11 and not cell.font.b, where
            a = cell.alignment
            assert (a.horizontal, a.vertical, a.wrap_text) == (halign, "center", True), where
            b = cell.border
            assert (b.left.style, b.right.style, b.top.style, b.bottom.style) == ("thin",) * 4, where
            if key in ("category", "core_business"):        # the sheet's two green columns
                assert LETTER[key] in ("B", "L")
                assert cell.fill.fill_type == "solid" and cell.fill.fgColor.rgb.endswith("E2EFDA")
            else:
                assert cell.fill.fill_type is None, where
    assert ws.cell(row=3, column=COL["remarks"]).value is None
    left = [LETTER[key] for key, _h, _w, halign in export_xlsx.COLUMNS if halign == "left"]
    assert left == ["H", "AG"]                              # everything else is centred


def test_local_os_dropdown_and_duplicate_code_highlight_cover_the_data_rows():
    wb = _book([_sheet_row(supplier_code=n) for n in (1, 2, 3)]
               + [_sheet_row(supplier_code=n, _status="inactive") for n in (4, 5)])
    for ws, last in ((wb["Registered"], 6), (wb["INACTIVE"], 3)):
        [dv] = ws.data_validations.dataValidation
        assert dv.type == "list" and dv.formula1 == '"LOCAL,OS"'
        assert str(dv.sqref) == f"D2:D{last}"
        assert dv.allowBlank is True
        assert not dv.showErrorMessage                      # legacy values are not rejected
        [cf] = list(ws.conditional_formatting)
        assert str(cf.sqref) == f"E2:E{last}"
        [rule] = cf.rules
        assert rule.type == "duplicateValues"
        assert rule.dxf.font.color.rgb.endswith("9C0006")
        assert rule.dxf.fill.bgColor.rgb.endswith("FFC7CE")


# ── Cell values ──────────────────────────────────────────────────────────────
def test_sr_no_counts_from_one_on_each_sheet_and_inactive_holds_only_inactive():
    rows = [_sheet_row(supplier_code=1, name="Test Vendor 1"),
            _sheet_row(supplier_code=2, name="Test Vendor 2", status="Inactive", _status="inactive"),
            _sheet_row(supplier_code=3, name="Test Vendor 3", status="Black Listed", _status="blacklisted"),
            _sheet_row(supplier_code=4, name="Test Vendor 4", status="Inactive", _status="inactive")]
    wb = _book(rows)
    reg, inactive = wb["Registered"], wb["INACTIVE"]
    name = COL["name"]
    assert [(reg.cell(row=r, column=1).value, reg.cell(row=r, column=name).value)
            for r in range(2, reg.max_row + 1)] == [
        (1, "Test Vendor 1"), (2, "Test Vendor 2"), (3, "Test Vendor 3"), (4, "Test Vendor 4")]
    assert [(inactive.cell(row=r, column=1).value, inactive.cell(row=r, column=name).value)
            for r in range(2, inactive.max_row + 1)] == [(1, "Test Vendor 2"), (2, "Test Vendor 4")]
    for ws in (reg, inactive):
        sr = ws.cell(row=2, column=1)
        assert type(sr.value) is int and sr.number_format == "0"
    assert "_status" not in [c.value for c in reg[1]]       # the private key is not a column


def test_values_keep_their_types():
    ws = _book([_sheet_row(supplier_code=1042, pin_code=400001, bank_account_no="000123456789",
                           mobile="09000000001", msme="YES")])["Registered"]
    code, pin = ws.cell(row=2, column=COL["supplier_code"]), ws.cell(row=2, column=COL["pin_code"])
    assert (code.value, code.data_type) == (1042, "n")
    assert (pin.value, pin.data_type) == (400001, "n")
    account = ws.cell(row=2, column=COL["bank_account_no"])
    assert (account.value, account.data_type) == ("000123456789", "s")      # leading zeros kept
    assert ws.cell(row=2, column=COL["mobile"]).value == "09000000001"
    assert ws.cell(row=2, column=COL["msme"]).value == "YES"
    # "" is written as an empty cell, the same as None.
    ws = _book([_sheet_row(remarks="", website=None)])["Registered"]
    assert ws.cell(row=2, column=COL["remarks"]).value is None
    assert ws.cell(row=2, column=COL["website"]).value is None


def test_the_contract_date_is_a_real_excel_date():
    aware = datetime(2026, 4, 30, 20, 0, tzinfo=timezone.utc)          # 01:30 IST on 1 May
    ws = _book([_sheet_row(contract_dated=date(2026, 3, 5)),
                _sheet_row(supplier_code=2, contract_dated=datetime(2026, 3, 6, 10, 30)),
                _sheet_row(supplier_code=3, contract_dated=aware),     # openpyxl rejects tz-aware
                _sheet_row(supplier_code=4)])["Registered"]
    col = COL["contract_dated"]
    for r, expected in ((2, datetime(2026, 3, 5)), (3, datetime(2026, 3, 6, 10, 30)),
                        (4, datetime(2026, 5, 1, 1, 30))):
        cell = ws.cell(row=r, column=col)
        assert cell.is_date and cell.number_format == "d-mmm-yy", r
        assert cell.value == expected
    assert ws.cell(row=5, column=col).value is None


def test_text_that_starts_with_an_equals_sign_is_never_a_formula():
    formula = '=HYPERLINK("http://example.invalid","Test Vendor 1")'
    stream = export_xlsx.build_supplier_master_workbook(
        [_sheet_row(name=formula, remarks="=1+1", reference="=", bank_name="#N/A",
                    _status="inactive")])
    wb = load_workbook(BytesIO(stream.getvalue()))
    for ws in wb.worksheets:                                # Registered and INACTIVE alike
        name = ws.cell(row=2, column=COL["name"])
        assert (name.value, name.data_type) == (formula, "s")
        remarks = ws.cell(row=2, column=COL["remarks"])
        assert (remarks.value, remarks.data_type) == ("=1+1", "s")
        assert ws.cell(row=2, column=COL["reference"]).value == "="
        # Text that is exactly an Excel error word stays text, not an error value.
        bank = ws.cell(row=2, column=COL["bank_name"])
        assert (bank.value, bank.data_type) == ("#N/A", "s")
    # ...and in the file itself: no sheet carries a formula element.
    with zipfile.ZipFile(BytesIO(stream.getvalue())) as z:
        sheets = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]
        assert len(sheets) == 2
        for n in sheets:
            assert not re.search(rb"<f[ >/]", z.read(n)), n


def test_control_characters_are_dropped_instead_of_failing_the_export():
    ws = _book([_sheet_row(name="Test\x0bVendor\x00 1", remarks="\x0b", address="Line 1\nLine 2")])["Registered"]
    assert ws.cell(row=2, column=COL["name"]).value == "TestVendor 1"
    assert ws.cell(row=2, column=COL["remarks"]).value is None
    assert ws.cell(row=2, column=COL["address"]).value == "Line 1\nLine 2"   # a line break is legal


def test_characters_xml_cannot_carry_do_not_make_the_file_unopenable():
    # U+FFFE / U+FFFF do not make openpyxl raise: without the scrub they are
    # written raw, the save succeeds and the sheet XML is not well-formed, so the
    # API answers 200 with a file nothing can open. Reloading is the assertion.
    bad = chr(0xFFFE) + chr(0xFFFF)
    ws = _book([_sheet_row(name=f"Test{bad} Vendor 1", remarks=bad)])["Registered"]
    assert ws.cell(row=2, column=COL["name"]).value == "Test Vendor 1"
    assert ws.cell(row=2, column=COL["remarks"]).value is None


# ── Filename ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("day, expected", [
    (date(2026, 11, 16), "001_A_MIS Master_Suppliers_data_2026-27_16Nov2026.xlsx"),
    (date(2027, 3, 31), "001_A_MIS Master_Suppliers_data_2026-27_31Mar2027.xlsx"),
    (date(2027, 4, 1), "001_A_MIS Master_Suppliers_data_2027-28_01Apr2027.xlsx"),
    (date(2027, 1, 5), "001_A_MIS Master_Suppliers_data_2026-27_05Jan2027.xlsx"),
    (date(2099, 12, 9), "001_A_MIS Master_Suppliers_data_2099-00_09Dec2099.xlsx"),
])
def test_export_filename_carries_the_indian_financial_year(day, expected):
    assert export_xlsx.export_filename(day) == expected


# ── build_export_rows: vendor tables -> sheet rows ───────────────────────────
def test_a_row_has_every_sheet_column_and_the_raw_status():
    row = _one(_vendor(1, status="Inactive"))
    assert set(row) == set(SHEET_KEYS) | {"_status"}
    assert row["_status"] == "inactive" and row["status"] == "Inactive"


def test_text_columns_are_carried_over_stripped_and_blank_is_none():
    row = _one(_vendor(
        1, name="  Test Vendor 1 ", sub_category=" Cashew ", supplier_reg_year="2024-25",
        core_business="Trading", contact_person="Test Person", designation="Manager",
        phone_company="022-00000000", mobile="09000000001", email="a@example.invalid",
        website="example.invalid", address_line="1 Test Road", state="Maharashtra",
        city="Mumbai", fssai_no="10000000000001", brc_other="BRC", cin_no="U00000MH2020PTC000001",
        pan_no="AAAAA0000A", gstn="27AAAAA0000A1Z5", iec_no="0000000001",
        uam_udyam_no="UDYAM-MH-00-0000001", business_turnover_3y="1 Cr", capabilities="Cold chain",
        remarks=" Pays on time ", reference=" Test Referrer ", pollution_epr="EPR-1", tin_tan="TAN-1"))
    assert row["name"] == "Test Vendor 1"
    assert row["sub_category"] == "Cashew"
    assert row["supplier_reg_year"] == "2024-25"
    assert (row["core_business"], row["contact_person"], row["designation"]) == (
        "Trading", "Test Person", "Manager")
    assert (row["phone_company"], row["mobile"]) == ("022-00000000", "09000000001")   # text
    assert (row["email"], row["website"]) == ("a@example.invalid", "example.invalid")
    assert (row["address"], row["state"], row["city"]) == ("1 Test Road", "Maharashtra", "Mumbai")
    assert (row["fssai"], row["brc_other"], row["cin"]) == (
        "10000000000001", "BRC", "U00000MH2020PTC000001")
    assert (row["pan"], row["gstn"], row["iec"]) == ("AAAAA0000A", "27AAAAA0000A1Z5", "0000000001")
    assert (row["udyam"], row["turnover"], row["capabilities"]) == (
        "UDYAM-MH-00-0000001", "1 Cr", "Cold chain")
    assert (row["pollution_epr"], row["tin_tan"]) == ("EPR-1", "TAN-1")
    assert (row["remarks"], row["reference"]) == ("Pays on time", "Test Referrer")
    assert row["performance"] is None and row["hard_copy"] is None     # no such field in the ERP
    blank = _one(_vendor(2, remarks="   ", reference=""))
    assert blank["remarks"] is None and blank["reference"] is None


def test_lookup_ids_become_labels_and_an_unknown_id_is_blank():
    row = _one(_vendor(1, category_code_id="cat-rm", supplier_type_id="st-norm",
                       firm_status_id="fs-pvt", business_type_id="bt-mfr",
                       msme_type_id="msme-micro", scoc_status_id="kyc-ver", kyc_status_id="kyc-pen"))
    assert row["category"] == "Raw Material"
    assert (row["supplier_type"], row["firm_status"], row["business_type"]) == (
        "Normal Supplier", "Private Ltd Company", "Manufacturer")
    assert (row["msme_type"], row["scoc"], row["kyc"]) == ("Micro", "Verified", "Pending")

    unknown = _one(_vendor(2, category_code_id="gone-1", supplier_type_id="gone-2",
                           firm_status_id="gone-3", business_type_id="gone-4",
                           local_os_id="gone-5", msme_type_id="gone-6", scoc_status_id="gone-7",
                           kyc_status_id="gone-8", doc_status_id="gone-9"),
                   banking=[_bank("v2", account_type_id="gone-10")])
    for key in ("category", "supplier_type", "firm_status", "business_type", "local_os",
                "msme_type", "scoc", "kyc", "documentation", "bank_account_type"):
        assert unknown[key] is None, key                    # never the raw id
    assert not [v for v in unknown.values() if isinstance(v, str) and v.startswith("gone-")]


def test_others_is_replaced_by_what_was_typed():
    typed = _one(_vendor(1, supplier_type_id="st-oth", supplier_type_other=" Freight agent ",
                         firm_status_id="fs-oth", firm_status_other="Trust",
                         business_type_id="bt-oth", business_type_other="Cold store"))
    assert (typed["supplier_type"], typed["firm_status"], typed["business_type"]) == (
        "Freight agent", "Trust", "Cold store")
    # "Others" with nothing typed stays "Others"...
    bare = _one(_vendor(2, supplier_type_id="st-oth", supplier_type_other="  ",
                        firm_status_id="fs-oth", business_type_id="bt-oth"))
    assert (bare["supplier_type"], bare["firm_status"], bare["business_type"]) == (
        "Others", "Others", "other")
    # ...and a stale typed value does not override a real choice.
    stale = _one(_vendor(3, supplier_type_id="st-norm", supplier_type_other="Freight agent"))
    assert stale["supplier_type"] == "Normal Supplier"


@pytest.mark.parametrize("lookup_id, expected", [
    ("los-local", "LOCAL"), ("los-import", "OS"), ("los-os", "OS"), ("los-label-os", "OS"),
    ("los-label-imp", "OS"), ("los-sez", "SEZ UNIT"), ("los-cost", "COST CENTRE"), (None, None),
])
def test_local_os_is_spelt_the_way_the_sheet_spells_it(lookup_id, expected):
    assert _one(_vendor(1, local_os_id=lookup_id))["local_os"] == expected


@pytest.mark.parametrize("status, shown, raw", [
    ("active", "Active", "active"), ("inactive", "Inactive", "inactive"),
    ("blacklisted", "Black Listed", "blacklisted"), (" INACTIVE ", "Inactive", "inactive"),
    ("on hold", "On Hold", "on hold"), (None, None, ""),
])
def test_status_text(status, shown, raw):
    row = _one(_vendor(1, status=status))
    assert (row["status"], row["_status"]) == (shown, raw)


def test_msme_is_yes_no_or_blank():
    assert [_one(_vendor(1, is_msme=flag))["msme"] for flag in (True, False, None)] == ["YES", "NO", None]


def test_supplier_code_and_pin_code_are_numbers_only_when_they_are_numbers():
    numeric = _one(_vendor(1, supplier_code=" 1042 ", pin_code="400001"))
    assert numeric["supplier_code"] == 1042 and type(numeric["supplier_code"]) is int
    assert numeric["pin_code"] == 400001 and type(numeric["pin_code"]) is int
    minted = _one(_vendor(2, supplier_code="SC26-0001", pin_code="400 001"))
    assert minted["supplier_code"] == "SC26-0001"
    assert minted["pin_code"] == "400 001"
    assert _one(_vendor(3, pin_code="40001"))["pin_code"] == "40001"   # not six digits
    assert _one(_vendor(4, pin_code=None))["pin_code"] is None
    # A number only when nothing is lost: a leading zero, or more digits than Excel
    # keeps exactly, stays text. "0042" and "42" are different codes.
    padded = _one(_vendor(5, supplier_code="0042", pin_code="012345"))
    assert (padded["supplier_code"], padded["pin_code"]) == ("0042", "012345")
    assert _one(_vendor(6, supplier_code="1234567890123456"))["supplier_code"] == "1234567890123456"
    assert _one(_vendor(7, supplier_code="123456789012345"))["supplier_code"] == 123456789012345


def test_one_bank_row_is_chosen_per_vendor():
    old, new = datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2026, 6, 1, tzinfo=timezone.utc)

    def chosen(*banking) -> str | None:
        return _one(_vendor(1), banking=list(banking))["bank_name"]

    # Primary and active beats a newer row that is not primary.
    assert chosen(_bank("v1", bank_name="Newer", created_at=new),
                  _bank("v1", bank_name="Primary", is_primary=True, created_at=old)) == "Primary"
    # A primary that has been switched off loses to an active row.
    assert chosen(_bank("v1", bank_name="Off primary", is_primary=True, is_active=False, created_at=new),
                  _bank("v1", bank_name="Active", created_at=old)) == "Active"
    # Two active rows, neither primary: the newer one; an undated row counts as oldest.
    assert chosen(_bank("v1", bank_name="Older", created_at=old),
                  _bank("v1", bank_name="Undated", created_at=None),
                  _bank("v1", bank_name="Newer", created_at=new)) == "Newer"
    # Another vendor's account is never borrowed.
    assert chosen(_bank("v2", bank_name="Someone else", is_primary=True)) is None


def test_an_account_switched_off_in_the_erp_is_never_shown():
    # This is the column people pay from. The ERP calls such a vendor "No bank".
    row = _one(_vendor(1), banking=[
        _bank("v1", bank_name="Off primary", account_no="555500005555", ifsc="TEST0000002",
              account_type_id="acct-cur", is_primary=True, is_active=False),
        _bank("v1", bank_name="Off", is_active=False)])
    assert [row[k] for k in SHEET_KEYS if k.startswith("bank_")] == [None] * 7
    # Only an explicit "off" hides an account: a row whose flag is NULL is kept.
    kept = _one(_vendor(1), banking=[_bank("v1", bank_name="Unflagged", is_active=None)])
    assert kept["bank_name"] == "Unflagged"


def test_a_tie_between_two_accounts_is_settled_the_same_way_every_time():
    # Rows saved in one transaction share created_at; the table has no fixed order.
    at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    a = _bank("v1", bank_id="b-100", bank_name="A", created_at=at)
    b = _bank("v1", bank_id="b-200", bank_name="B", created_at=at)
    assert _one(_vendor(1), banking=[a, b])["bank_name"] == "B"
    assert _one(_vendor(1), banking=[b, a])["bank_name"] == "B"


def test_ids_are_matched_as_text_whatever_type_each_table_hands_back():
    import uuid
    vid = uuid.UUID("00000000-0000-4000-8000-000000000001")
    row = _one(_vendor(1, vendor_id=vid), banking=[_bank(str(vid), bank_name="Matched")],
               documents=[_doc(str(vid), "PAN")], contracts=[_contract(str(vid))])
    assert (row["bank_name"], row["documentation"], row["contract_signed"]) == ("Matched", "PAN", "Y")


def test_bank_columns_come_from_the_chosen_row():
    row = _one(_vendor(1), banking=[
        _bank("v1", bank_name=" Test Bank ", account_no="000123456789", account_name="Test Vendor 1",
              branch="Test Branch, Mumbai", ifsc="TEST0000001", swift="TESTINBB",
              account_type_id="acct-cur", is_primary=True),
        _bank("v1", bank_name="Second Bank", account_no="999", account_type_id="acct-sav")])
    assert row["bank_name"] == "Test Bank"
    assert row["bank_account_no"] == "000123456789" and type(row["bank_account_no"]) is str
    assert (row["bank_account_name"], row["bank_branch"]) == ("Test Vendor 1", "Test Branch, Mumbai")
    assert (row["bank_ifsc"], row["bank_swift"]) == ("TEST0000001", "TESTINBB")
    assert row["bank_account_type"] == "Current"
    none = _one(_vendor(2))
    assert [none[k] for k in SHEET_KEYS if k.startswith("bank_")] == [None] * 7


def test_documentation_lists_the_doc_types_in_sheet_order():
    row = _one(_vendor(1, doc_status_id="doc-rev"), documents=[
        _doc("v1", "contract"), _doc("v1", "PAN"), _doc("v1", " gst "), _doc("v1", "PAN"),
        _doc("v1", "Zed cert"), _doc("v1", "ISO"), _doc("v1", ""), _doc("v1", None),
        _doc("v1", "OTHER"), _doc("v1", "FSSAI"), _doc("v2", "MSME")])
    assert row["documentation"] == "GST, PAN, FSSAI, CONTRACT, OTHER, ISO, ZED CERT"
    every = _one(_vendor(1), documents=[_doc("v1", t) for t in (
        "OTHER", "CONTRACT", "TAN", "TIN", "POLLUTION", "EPR", "BRC", "CIN", "IEC", "UDYAM",
        "MSME", "FSSAI", "PAN", "GST")])
    assert every["documentation"] == ("GST, PAN, FSSAI, MSME, UDYAM, IEC, CIN, BRC, EPR, "
                                      "POLLUTION, TIN, TAN, CONTRACT, OTHER")
    # No documents on file: the document status stands in, when there is one.
    assert _one(_vendor(2, doc_status_id="doc-rev"))["documentation"] == "In Review"
    assert _one(_vendor(3, doc_status_id="doc-rev"), documents=[_doc("v3", " ")])["documentation"] == "In Review"
    assert _one(_vendor(4))["documentation"] is None


def test_contract_signed_and_its_latest_date():
    none = _one(_vendor(1))
    assert (none["contract_signed"], none["contract_dated"]) == ("N", None)
    signed = _one(_vendor(1), contracts=[
        _contract("v1", signed_date=date(2025, 4, 1), effective_from=date(2027, 1, 1)),
        _contract("v1", signed_date=date(2026, 2, 15)),
        _contract("v1", effective_from=date(2026, 12, 1)),
        _contract("v2", signed_date=date(2030, 1, 1))])
    assert (signed["contract_signed"], signed["contract_dated"]) == ("Y", date(2026, 2, 15))
    unsigned = _one(_vendor(1), contracts=[_contract("v1", effective_from=date(2026, 1, 1)),
                                           _contract("v1", effective_from=date(2026, 5, 1))])
    assert (unsigned["contract_signed"], unsigned["contract_dated"]) == ("Y", date(2026, 5, 1))
    undated = _one(_vendor(1), contracts=[_contract("v1")])
    assert (undated["contract_signed"], undated["contract_dated"]) == ("Y", None)


def test_rows_are_sorted_numeric_codes_first():
    rows = _rows([
        _vendor(1, supplier_code="SC26-0002", name="Test Vendor F"),
        _vendor(2, supplier_code="10", name="Test Vendor C"),
        _vendor(3, supplier_code="sc26-0001", name="Test Vendor E"),
        _vendor(4, supplier_code="9", name="Test Vendor B"),
        _vendor(5, supplier_code="2", name="Test Vendor A"),
        _vendor(6, supplier_code="AB-7", name="test vendor h"),
        _vendor(7, supplier_code="AB-7", name="Test Vendor G"),
    ])
    assert [(r["supplier_code"], r["name"]) for r in rows] == [
        (2, "Test Vendor A"), (9, "Test Vendor B"), (10, "Test Vendor C"),       # 9 before 10
        ("AB-7", "Test Vendor G"), ("AB-7", "test vendor h"),                    # tie: by name
        ("sc26-0001", "Test Vendor E"), ("SC26-0002", "Test Vendor F"),          # case-blind
    ]


def test_record_like_rows_are_accepted():
    # asyncpg Records are mappings, not dicts.
    rows = export_service.build_export_rows(
        [MappingProxyType(_vendor(1, category_code_id="cat-rm"))],
        [MappingProxyType(l) for l in LOOKUPS],
        [MappingProxyType(_bank("v1", bank_name="Test Bank"))],
        [MappingProxyType(_doc("v1", "PAN"))],
        [MappingProxyType(_contract("v1", signed_date=date(2026, 1, 2)))])
    [row] = rows
    assert (row["category"], row["bank_name"], row["documentation"], row["contract_dated"]) == (
        "Raw Material", "Test Bank", "PAN", date(2026, 1, 2))


def test_mapped_rows_go_through_the_workbook_builder():
    rows = _rows([_vendor(2, status="inactive", supplier_code="SC26-0001", name="=Test Vendor 2"),
                  _vendor(1, supplier_code="7", pin_code="400001", local_os_id="los-import")],
                 banking=[_bank("v1", account_no="000123", is_primary=True)],
                 contracts=[_contract("v1", signed_date=date(2026, 2, 15))])
    wb = _book(rows)
    reg, inactive = wb["Registered"], wb["INACTIVE"]
    assert [reg.cell(row=r, column=COL["supplier_code"]).value for r in (2, 3)] == [7, "SC26-0001"]
    assert reg.cell(row=2, column=COL["local_os"]).value == "OS"
    assert reg.cell(row=2, column=COL["bank_account_no"]).value == "000123"
    assert reg.cell(row=2, column=COL["contract_signed"]).value == "Y"
    assert reg.cell(row=2, column=COL["contract_dated"]).value == datetime(2026, 2, 15)
    assert reg.cell(row=3, column=COL["status"]).value == "Inactive"
    assert [inactive.cell(row=2, column=c).value for c in (1, COL["name"])] == [1, "=Test Vendor 2"]
    assert inactive.max_row == 2


# ── The five reads ───────────────────────────────────────────────────────────
class FakeConn:
    """The five vendor tables as lists of dicts. Answers `SELECT cols FROM table`
    (vendor_master with its soft-delete filter) and nothing else: a join, a write
    or a column the table does not have fails the test."""

    def __init__(self, vendors=(), lookups=LOOKUPS, banking=(), documents=(), contracts=(),
                 lookup_table=True):
        self.tables = {"vendor_master": list(vendors), "lookup_value": list(lookups),
                       "vendor_banking": list(banking), "vendor_document": list(documents),
                       "vendor_contract": list(contracts)}
        self.lookup_table = lookup_table
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append(s)
        assert not args, args
        m = re.fullmatch(r"SELECT (.+?) FROM (\w+)( WHERE is_deleted = false)?", s)
        if not m:
            raise AssertionError(f"unexpected fetch: {s[:100]}")
        columns, table = [c.strip() for c in m[1].split(",")], m[2]
        assert bool(m[3]) == (table == "vendor_master"), s
        if table == "lookup_value" and not self.lookup_table:
            raise asyncpg.UndefinedTableError('relation "lookup_value" does not exist')
        rows = self.tables[table]
        if table == "vendor_master":
            rows = [r for r in rows if not r["is_deleted"]]
        return [MappingProxyType({c: r[c] for c in columns}) for r in rows]


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.acquired = 0

    def acquire(self):
        pool = self

        class _Acquired:
            async def __aenter__(self):
                pool.acquired += 1
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Acquired()


def _fetch(conn) -> tuple[list[dict], _Pool]:
    pool = _Pool(conn)
    return asyncio.run(export_service.fetch_supplier_master_rows(pool)), pool


def _seeded_conn(**over) -> FakeConn:
    tables = dict(
        vendors=[_vendor(1, supplier_code="12", category_code_id="cat-rm", status="inactive"),
                 _vendor(2, supplier_code="3", local_os_id="los-local"),
                 _vendor(3, supplier_code="1", name="Test Vendor Deleted", is_deleted=True)],
        banking=[_bank("v2", account_no="000123456789", is_primary=True, account_type_id="acct-cur"),
                 _bank("v3", account_no="555")],
        documents=[_doc("v2", "PAN"), _doc("v2", "GST")],
        contracts=[_contract("v2", signed_date=date(2026, 2, 15))])
    tables.update(over)
    return FakeConn(**tables)


def test_fetch_makes_five_plain_reads_on_one_connection():
    conn = _seeded_conn()
    rows, pool = _fetch(conn)
    assert pool.acquired == 1
    assert [re.search(r" FROM (\w+)", s)[1] for s in conn.calls] == [
        "vendor_master", "lookup_value", "vendor_banking", "vendor_document", "vendor_contract"]
    for s in conn.calls:
        assert s.startswith("SELECT ") and " JOIN " not in s.upper(), s
    # Soft-deleted vendors are left out; the rest are mapped and sorted.
    assert [(r["supplier_code"], r["name"]) for r in rows] == [(3, "Test Vendor 2"), (12, "Test Vendor 1")]
    assert rows[0]["local_os"] == "LOCAL" and rows[0]["documentation"] == "GST, PAN"
    assert (rows[0]["bank_account_no"], rows[0]["bank_account_type"]) == ("000123456789", "Current")
    assert (rows[0]["contract_signed"], rows[0]["contract_dated"]) == ("Y", date(2026, 2, 15))
    assert (rows[1]["category"], rows[1]["_status"]) == ("Raw Material", "inactive")


def test_fetch_reads_only_columns_the_existing_services_already_read():
    conn = _seeded_conn()
    _fetch(conn)
    selected = {re.search(r" FROM (\w+)", s)[1]: _cols(s[len("SELECT "):s.index(" FROM ")])
                for s in conn.calls}
    proven = {"vendor_master": vendor_service._VENDOR_COLUMNS,
              "vendor_banking": banking_service._BANK_COLUMNS,
              "vendor_document": document_service._DOC_COLUMNS,
              "vendor_contract": contract_service._CONTRACT_COLUMNS}
    for table, block in proven.items():
        assert set(selected[table]) <= set(_cols(block)), table
        assert len(selected[table]) == len(set(selected[table])), table
    assert selected["lookup_value"] == ["lookup_id", "lookup_type", "code", "label"]
    assert len(selected["vendor_master"]) == 43


def test_a_missing_lookup_table_exports_without_labels():
    rows, _pool = _fetch(_seeded_conn(lookup_table=False))
    assert [r["name"] for r in rows] == ["Test Vendor 2", "Test Vendor 1"]
    assert rows[0]["local_os"] is None and rows[1]["category"] is None
    assert rows[0]["bank_account_no"] == "000123456789"     # the other tables still load


# ── The route ────────────────────────────────────────────────────────────────
def _user(email, *, is_admin=False):
    return AuthUser(user_id=7, phone="9000000007", full_name="", email=email, entity="cfpl",
                    role_id=3, role_name="", is_admin=is_admin)


VIEWER = _user("viewer@example.invalid")


@pytest.fixture
def client(monkeypatch):
    """(conn, user, deny) -> TestClient on the real vendor router behind the real
    auth dependency. `deny` lists the (module, sub_module, action) permissions the
    caller lacks; `client.asked` lists every permission a route checked."""
    import app.modules.auth.services.auth_service as auth_service
    import app.modules.auth.services.permission_service as permission_service

    asked: list[tuple] = []

    def _make(conn, user=VIEWER, *, deny=()):
        async def _validate(_conn, _token):
            return {"user_id": user.user_id, "phone": user.phone, "email": user.email,
                    "role_id": user.role_id, "is_admin": user.is_admin,
                    "allowed_warehouses": user.allowed_warehouses}

        async def _check(_conn, _role_ids, _is_admin, module, sub_module, sub_sub_module, action, **_scope):
            asked.append((module, sub_module, sub_sub_module, action))
            return (module, sub_module, action) not in deny

        monkeypatch.setattr(auth_service, "validate_session", _validate)
        monkeypatch.setattr(permission_service, "check_permission", _check)
        app = FastAPI()
        request_context.install(app)
        app.include_router(R.router)
        app.state.db_pool = _Pool(conn)
        return TestClient(app, headers={"Authorization": "Bearer test-token"})

    _make.asked = asked
    return _make


def test_the_route_returns_the_workbook(client):
    conn = _seeded_conn()
    before = datetime.now(IST).date()
    res = client(conn).get("/api/v1/vendors/export")
    after = datetime.now(IST).date()
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == XLSX
    disposition = res.headers["content-disposition"]
    assert re.fullmatch(r'attachment; filename="001_A_MIS Master_Suppliers_data_'
                        r'\d{4}-\d{2}_\d{2}[A-Z][a-z]{2}\d{4}\.xlsx"', disposition), disposition
    assert disposition in {f'attachment; filename="{export_xlsx.export_filename(day)}"'
                           for day in (before, after)}                  # today, on the IST clock
    assert res.headers["x-total-rows"] == "2"                           # the deleted vendor is not counted
    # Bank details: never cached, and sent whole with its length.
    assert res.headers["cache-control"] == "no-store"
    assert int(res.headers["content-length"]) == len(res.content)
    wb = load_workbook(BytesIO(res.content))
    assert wb.sheetnames == ["Registered", "INACTIVE"]
    reg, inactive = wb["Registered"], wb["INACTIVE"]
    assert [c.value for c in reg[1]] == export_xlsx.HEADERS
    assert [[reg.cell(row=r, column=c).value for c in (1, COL["supplier_code"], COL["name"])]
            for r in range(2, reg.max_row + 1)] == [[1, 3, "Test Vendor 2"], [2, 12, "Test Vendor 1"]]
    assert reg.cell(row=2, column=COL["bank_account_no"]).value == "000123456789"
    assert [[inactive.cell(row=r, column=c).value for c in (1, COL["name"])]
            for r in range(2, inactive.max_row + 1)] == [[1, "Test Vendor 1"]]
    # The sheet carries bank account numbers, so both permissions were checked.
    assert ("vendor", "master", None, "view") in client.asked
    assert ("vendor", "banking", None, "view") in client.asked
    assert all(s.startswith("SELECT ") for s in conn.calls) and len(conn.calls) == 5


def test_the_file_name_carries_the_ist_date_not_the_servers(client, monkeypatch):
    # 20:00 UTC on 31 March is already 1 April in India, and a new financial year.
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(R, "datetime", _Clock)
    res = client(_seeded_conn()).get("/api/v1/vendors/export")
    assert res.status_code == 200, res.text
    assert res.headers["content-disposition"] == (
        'attachment; filename="001_A_MIS Master_Suppliers_data_2026-27_01Apr2026.xlsx"')


def test_the_route_exports_an_empty_master(client):
    res = client(FakeConn()).get("/api/v1/vendors/export")
    assert res.status_code == 200, res.text
    assert res.headers["x-total-rows"] == "0"
    wb = load_workbook(BytesIO(res.content))
    assert [ws.max_row for ws in wb.worksheets] == [1, 1]


def test_export_is_declared_before_the_vendor_id_route():
    # Starlette matches in declaration order: after /{vendor_id}, "export" would
    # be looked up as a vendor id and answer 404.
    gets = [r.path for r in R.router.routes if "GET" in getattr(r, "methods", ())]
    assert gets.index("/api/v1/vendors/export") < gets.index("/api/v1/vendors/{vendor_id}")
    assert gets.index("/api/v1/vendors/search") < gets.index("/api/v1/vendors/export")
    assert gets.count("/api/v1/vendors/export") == 1


@pytest.mark.parametrize("denied", [("vendor", "banking", "view"), ("vendor", "master", "view")])
def test_the_route_needs_both_master_and_banking_view(client, denied):
    conn = _seeded_conn()
    res = client(conn, deny={denied}).get("/api/v1/vendors/export")
    assert res.status_code == 403, res.text
    assert not conn.calls                                   # nothing was read for them


def test_the_route_needs_a_token(client):
    conn = _seeded_conn()
    c = client(conn)
    c.headers.pop("Authorization")
    assert c.get("/api/v1/vendors/export").status_code == 401
    assert not conn.calls
