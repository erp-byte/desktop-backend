"""Pure openpyxl builder for the supplier-master Excel export (no DB access).

Lays the vendors out as the company's "MIS Master - Suppliers data" workbook:
sheet "Registered" holds every vendor, sheet "INACTIVE" the inactive ones again,
both with the same 51 columns. Header text, column order and widths are that
workbook's own, so a download can be pasted over it. Returns a BytesIO stream.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.formatting.rule import Rule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

# (row key, header, column width, horizontal alignment), in sheet order A..AY.
# The headers are VERBATIM from the company workbook, oddities included: the
# trailing space in "Beneficiary Bank ", the double spaces in "Account  No.",
# "IFSC  Code" and "FILE  (Hard Copy)", and the line breaks. Do not tidy them.
COLUMNS: list[tuple[str, str, float, str]] = [
    ("sr_no", "Sr.No.", 6.6, "center"),
    ("category", "Category Code", 17.9, "center"),
    ("sub_category", "Sub Category (details of item which is supplied)", 27.1, "center"),
    ("local_os", "Local /OS", 15.7, "center"),
    ("supplier_code", "Supplier Code", 11.3, "center"),
    ("supplier_reg_year", "Supplier Reg. Year", 12.6, "center"),
    ("supplier_type", "Supplier Type", 18.7, "center"),
    ("name", "Name of the Supplier Company", 43.6, "left"),
    ("status", "STATUS (ACTIVE / IN ACTIVE)/ Black Listed", 13.1, "center"),
    ("firm_status", "FIRM STATUS / Constitution of Business", 26.6, "center"),
    ("business_type", "BUSINESS TYPE", 15.6, "center"),
    ("core_business", "Core Business / Main Products / Business Details", 18.6, "center"),
    ("contact_person", "Contact Person", 31.6, "center"),
    ("designation", "Designation of person", 21.4, "center"),
    ("address", "Contact Address", 37.1, "center"),
    ("state", "State", 17.3, "center"),
    ("city", "City (District)", 15.4, "center"),
    ("pin_code", "PIN Code", 12.4, "center"),
    ("phone_company", "Contact Details -Company", 17.7, "center"),
    ("mobile", "Contact Mobile Nos.", 17.0, "center"),
    ("email", "E-mail", 34.1, "center"),
    ("website", "Website", 37.7, "center"),
    ("fssai", "FSSAI", 23.3, "center"),
    ("brc_other", "BRC / Others", 17.7, "center"),
    ("cin", "CIN No.", 26.3, "center"),
    ("pan", "PAN No", 13.7, "center"),
    ("gstn", "GSTN", 19.3, "center"),
    ("msme", "MSME (Yes / No)", 14.0, "center"),
    ("msme_type", "MSME TYPE", 8.6, "center"),
    ("udyam", "UAM No.\nUDYAM-", 16.3, "center"),
    ("bank_name", "Beneficiary Bank ", 23.4, "center"),
    ("bank_account_no", "Beneficiary Bank Account  No.", 22.0, "center"),
    ("bank_account_name", "Beneficiary Account Name", 25.7, "left"),
    ("bank_branch", "Branch Name & Address", 24.3, "center"),
    ("bank_ifsc", "IFSC  Code", 14.7, "center"),
    ("bank_swift", "SWIFT Code", 14.7, "center"),
    ("scoc", "SCOC-SUPPLY CODE OF CONDUCT", 14.7, "center"),
    ("kyc", "KYC Submitted", 11.0, "center"),
    ("bank_account_type", "Type of Account (Saving / Current / CC)", 17.6, "center"),
    ("iec", "Import / Export Reg.\nIEC", 13.3, "center"),
    ("turnover", "Business Turnover\n(Last three Years)", 15.9, "center"),
    ("capabilities", "Supplier Capabilities", 13.7, "center"),
    ("remarks", "REMARKS", 21.0, "center"),
    ("contract_signed", "Contract AG Signed (Y/N)", 13.4, "center"),
    ("contract_dated", "Contract AG dated", 13.3, "center"),
    ("documentation", "Documentation Received", 33.7, "center"),
    ("performance", "Performance of Supplier (including documentation)", 14.9, "center"),
    ("hard_copy", "FILE  (Hard Copy)", 13.3, "center"),
    ("reference", "Reference\n(whoever referred, need to mention)", 29.3, "center"),
    ("pollution_epr", "Pollution\n/ EPR Registration", 26.7, "center"),
    ("tin_tan", "TIN/ TAN / Other Registrations (pl mention)", 38.7, "center"),
]
HEADERS = [header for _key, header, _width, _halign in COLUMNS]

SHEET_REGISTERED = "Registered"
SHEET_INACTIVE = "INACTIVE"

# English month abbreviations for the file name: strftime("%b") follows the locale.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_IST = timezone(timedelta(hours=5, minutes=30))

_LAST_COLUMN = get_column_letter(len(COLUMNS))              # "AY"
_LOCAL_OS_COLUMN = get_column_letter([c[0] for c in COLUMNS].index("local_os") + 1)
_SUPPLIER_CODE_COLUMN = get_column_letter([c[0] for c in COLUMNS].index("supplier_code") + 1)

# Date columns are real Excel dates, so they sort and filter as dates.
_DATE_FORMATS = {"contract_dated": "d-mmm-yy"}

# Characters XML 1.0 cannot carry. openpyxl raises on the C0 controls and, with
# no lxml installed, writes the rest raw: one such character in one vendor's
# remarks would make the whole file unopenable while the API still answers 200.
_UNWRITABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

_HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
# 305496 is what Excel shows for the workbook's header (its theme blue, 25% darker).
_HEADER_FILL = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_MEDIUM = Side(style="medium")
_HEADER_BORDER = Border(left=_MEDIUM, right=_MEDIUM, top=_MEDIUM, bottom=_MEDIUM)

_DATA_FONT = Font(name="Calibri", size=11)
_DATA_ALIGN = {
    "center": Alignment(horizontal="center", vertical="center", wrap_text=True),
    "left": Alignment(horizontal="left", vertical="center", wrap_text=True),
}
_THIN = Side(style="thin")
_DATA_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
# The company workbook shades these two columns light green.
_GREEN_FILL = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
_GREEN_COLUMNS = ("category", "core_business")

# Excel's "light red fill with dark red text", shown on a supplier code used twice.
_DUPLICATE_STYLE = DifferentialStyle(font=Font(color="9C0006"), fill=PatternFill(bgColor="FFC7CE"))


def export_filename(day: date) -> str:
    """The company's file name for a download made on `day`, carrying the Indian
    financial year (April to March) that day falls in:
    16 Nov 2026 -> "001_A_MIS Master_Suppliers_data_2026-27_16Nov2026.xlsx"."""
    start = day.year if day.month >= 4 else day.year - 1
    fy = f"{start}-{(start + 1) % 100:02d}"
    return f"001_A_MIS Master_Suppliers_data_{fy}_{day.day:02d}{_MONTHS[day.month - 1]}{day.year}.xlsx"


def _cell_value(v):
    """None and "" are an empty cell. openpyxl refuses aware datetimes (written as
    their IST wall-clock time); characters the file cannot carry are dropped."""
    if isinstance(v, datetime) and v.utcoffset() is not None:
        return v.astimezone(_IST).replace(tzinfo=None)
    if isinstance(v, str):
        return _UNWRITABLE.sub("", v) or None
    return v


def _write_sheet(ws, rows: list[dict]) -> None:
    for col_idx, (_key, header, width, _halign) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN
        cell.border = _HEADER_BORDER
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # Every cell of a data row is styled, the empty ones too, so the grid has no
    # holes. No row height is set anywhere, the header row included: Excel
    # auto-fits wrapped text on open, as it does for the company workbook, whose
    # header is 58 pt on one PC and 76 pt on another. A fixed height clips it.
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, (key, _header, _width, halign) in enumerate(COLUMNS, start=1):
            value = row_idx - 1 if key == "sr_no" else _cell_value(row.get(key))
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            # Names and remarks are typed by users: openpyxl would store text that
            # starts with "=" as a live formula, and text that is exactly an Excel
            # error word ("#N/A") as an error value. Keep both as text.
            if cell.data_type in ("f", "e"):
                cell.data_type = "s"
            cell.font = _DATA_FONT
            cell.alignment = _DATA_ALIGN[halign]
            cell.border = _DATA_BORDER
            if key in _GREEN_COLUMNS:
                cell.fill = _GREEN_FILL
            if key == "sr_no":
                cell.number_format = "0"
            elif key in _DATE_FORMATS and isinstance(value, date):
                cell.number_format = _DATE_FORMATS[key]

    last_row = max(1, 1 + len(rows))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{_LAST_COLUMN}{last_row}"
    if rows:
        # A pick list for new entries. No error alert: legacy values other than
        # LOCAL / OS must not be rejected when a cell is edited.
        local_os = DataValidation(type="list", formula1='"LOCAL,OS"', allow_blank=True,
                                  showErrorMessage=False)
        local_os.add(f"{_LOCAL_OS_COLUMN}2:{_LOCAL_OS_COLUMN}{last_row}")
        ws.add_data_validation(local_os)
        ws.conditional_formatting.add(
            f"{_SUPPLIER_CODE_COLUMN}2:{_SUPPLIER_CODE_COLUMN}{last_row}",
            Rule(type="duplicateValues", dxf=_DUPLICATE_STYLE))


def build_supplier_master_workbook(rows: list[dict]) -> BytesIO:
    """`rows` are export_service.build_export_rows dicts, already in sheet order.
    Sr.No. is numbered here and restarts at 1 on each sheet."""
    wb = Workbook()
    registered = wb.active
    registered.title = SHEET_REGISTERED
    _write_sheet(registered, rows)
    _write_sheet(wb.create_sheet(SHEET_INACTIVE),
                 [row for row in rows if row.get("_status") == "inactive"])

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
