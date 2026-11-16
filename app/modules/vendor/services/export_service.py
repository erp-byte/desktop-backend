"""Rows for the supplier-master Excel export: the DB reads and the mapping.

`fetch_supplier_master_rows` reads the five tables behind the sheet and
`build_export_rows` (pure, no DB) turns them into one dict per vendor keyed like
export_xlsx.COLUMNS.

The reads are five plain SELECTs with NO joins, combined in Python. The vendor
tables live in an external schema whose column types we cannot inspect, so each
SELECT only names columns the existing services already read (vendor_service.
_VENDOR_COLUMNS, banking_service._BANK_COLUMNS, ...) and ids are matched as text
rather than trusting a JOIN to compare them. Read-only: nothing here writes.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


# Every name here is in vendor_service._VENDOR_COLUMNS.
_VENDOR_EXPORT_COLUMNS = """
    vendor_id, supplier_code, supplier_reg_year, name, status,
    supplier_type_id, firm_status_id, business_type_id, category_code_id,
    sub_category, local_os_id, core_business, contact_person, designation,
    phone_company, mobile, email, website, address_line, state, city,
    pin_code, fssai_no, brc_other, cin_no, pan_no, gstn, iec_no,
    pollution_epr, tin_tan, is_msme, msme_type_id,
    uam_udyam_no, business_turnover_3y, capabilities, remarks, reference,
    scoc_status_id, kyc_status_id, doc_status_id,
    supplier_type_other, firm_status_other, business_type_other
"""

# A subset of banking_service._BANK_COLUMNS.
_BANK_EXPORT_COLUMNS = """
    bank_id, vendor_id, bank_name, account_no, account_name, branch, ifsc, swift,
    account_type_id, is_primary, is_active, created_at
"""

_STATUS_TEXT = {"active": "Active", "inactive": "Inactive", "blacklisted": "Black Listed"}

#: Order of the "Documentation Received" list; a doc_type not named here follows, A-Z.
_DOC_ORDER = ("GST", "PAN", "FSSAI", "MSME", "UDYAM", "IEC", "CIN", "BRC", "EPR",
              "POLLUTION", "TIN", "TAN", "CONTRACT", "OTHER")

# A supplier code or PIN is written as a number, as the company sheet has them,
# only when that loses nothing: an all-digit value with no leading zero to drop
# and no more than the 15 digits Excel keeps exactly. Anything else stays text.
_NUMERIC_CODE = re.compile(r"[1-9][0-9]{0,14}|0")
_PIN = re.compile(r"[1-9][0-9]{5}")
_OS_WORD = re.compile(r"\bOS\b", re.IGNORECASE)


# ── read ─────────────────────────────────────────────────────────────────


async def fetch_supplier_master_rows(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Every non-deleted vendor as a supplier-master sheet row, in sheet order."""
    async with pool.acquire() as conn:
        vendors = await conn.fetch(
            f"SELECT {_VENDOR_EXPORT_COLUMNS} FROM vendor_master WHERE is_deleted = false"
        )
        try:
            lookups = await conn.fetch(
                "SELECT lookup_id, lookup_type, code, label FROM lookup_value"
            )
        except asyncpg.UndefinedTableError:
            # Defensive, as in lookups_router: a DB that has not bootstrapped
            # lookup_value still exports, with the dropdown columns blank.
            logger.warning("supplier-master export: lookup_value is missing; labels left blank")
            lookups = []
        banking = await conn.fetch(f"SELECT {_BANK_EXPORT_COLUMNS} FROM vendor_banking")
        documents = await conn.fetch("SELECT vendor_id, doc_type FROM vendor_document")
        contracts = await conn.fetch(
            "SELECT vendor_id, signed_date, effective_from FROM vendor_contract"
        )
    return build_export_rows(vendors, lookups, banking, documents, contracts)


# ── mapping helpers ──────────────────────────────────────────────────────


def _text(value: Any) -> str | None:
    """Stripped text; None for NULL and for blank."""
    if value is None:
        return None
    return str(value).strip() or None


def _key(value: Any) -> str | None:
    """An id as the text it is matched on, whatever type the column hands back."""
    return _text(value)


def _is_others(lookup: dict[str, Any]) -> bool:
    """The dropdown's "Others" option, whose real value was typed alongside it."""
    return ((_text(lookup["code"]) or "").upper() == "OTHERS"
            or (_text(lookup["label"]) or "").lower() in ("others", "other"))


def _label_or_typed(lookup: dict[str, Any] | None, typed: Any) -> str | None:
    """A dropdown's label, or what was typed next to it when the choice is "Others"."""
    if lookup is None:
        return None
    typed = _text(typed)
    return typed if typed and _is_others(lookup) else _text(lookup["label"])


def _local_os(lookup: dict[str, Any] | None) -> str | None:
    """The sheet says LOCAL or OS; the dropdown says "Local" or "Import (OS)"."""
    if lookup is None:
        return None
    code, label = (_text(lookup["code"]) or "").upper(), _text(lookup["label"]) or ""
    if code == "LOCAL":
        return "LOCAL"
    # "OS" as a word of its own, so a label like "Cost centre" is not read as OS.
    if code in ("IMPORT", "OS") or _OS_WORD.search(label) or "import" in label.lower():
        return "OS"
    return label.upper() or None


def _number_or_text(value: Any, pattern: re.Pattern[str]) -> int | str | None:
    """An int when the stripped text matches `pattern` in full, else the text."""
    text = _text(value)
    if text is not None and pattern.fullmatch(text):
        return int(text)
    return text


def _status(raw: str) -> str | None:
    if not raw:
        return None
    return _STATUS_TEXT.get(raw, raw.title())


def _created(bank: dict[str, Any]) -> float:
    """created_at as a sortable number; a row without one counts as the oldest."""
    at = bank.get("created_at")
    if not isinstance(at, datetime):
        return float("-inf")
    if at.utcoffset() is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.timestamp()


def _pick_bank(banks: list[dict[str, Any]]) -> dict[str, Any]:
    """The one account the sheet has room for: the primary, else whichever was
    created last (bank_id settles a tie, so two downloads agree).

    An account switched off in the ERP (is_active = false) is never shown: this
    is the column people pay from. A vendor with no other account gets blank bank
    columns, which is how the ERP itself treats it ("No bank"). A row whose flag
    is NULL is kept: only an explicit "off" hides an account."""
    usable = [bank for bank in banks if bank.get("is_active") is not False]
    if not usable:
        return {}

    def rank(bank: dict[str, Any]) -> tuple[bool, float, str]:
        return bool(bank.get("is_primary")), _created(bank), _text(bank.get("bank_id")) or ""

    return max(usable, key=rank)


def _as_date(value: Any) -> date | None:
    """signed_date / effective_from are DATE columns; a timestamp is cut to its day."""
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _contract_date(contracts: list[dict[str, Any]]) -> date | None:
    """The latest signing date; when no contract has one, the latest start date."""
    for column in ("signed_date", "effective_from"):
        dates = [d for d in (_as_date(c.get(column)) for c in contracts) if d is not None]
        if dates:
            return max(dates)
    return None


def _documentation(doc_types: set[str]) -> str | None:
    known = [t for t in _DOC_ORDER if t in doc_types]
    return ", ".join(known + sorted(doc_types.difference(_DOC_ORDER))) or None


def _sort_key(row: dict[str, Any]) -> tuple[int, int, str, str]:
    """Numeric supplier codes first, in numeric order, then the other codes A-Z."""
    code, name = row["supplier_code"], (row["name"] or "").lower()
    if isinstance(code, int):
        return 0, code, "", name
    if code is None:
        return 2, 0, "", name
    return 1, 0, code.lower(), name


# ── mapping ──────────────────────────────────────────────────────────────


def build_export_rows(
    vendors: list[Any],
    lookups: list[Any],
    banking: list[Any],
    documents: list[Any],
    contracts: list[Any],
) -> list[dict[str, Any]]:
    """One dict per vendor, keyed like export_xlsx.COLUMNS (without sr_no, which
    the workbook numbers) plus "_status", the raw lower-cased status the INACTIVE
    sheet is picked on. Pure: takes dicts or asyncpg Records, touches no DB.

    Text is stripped and blank becomes None. A lookup id with no lookup row gives
    None, never the raw id.
    """
    lookup_by_id = {_key(l["lookup_id"]): l for l in map(dict, lookups)}

    banks_by_vendor: dict[str | None, list[dict[str, Any]]] = {}
    for bank in map(dict, banking):
        banks_by_vendor.setdefault(_key(bank["vendor_id"]), []).append(bank)

    docs_by_vendor: dict[str | None, set[str]] = {}
    for doc in map(dict, documents):
        doc_type = _text(doc["doc_type"])
        if doc_type:
            docs_by_vendor.setdefault(_key(doc["vendor_id"]), set()).add(doc_type.upper())

    contracts_by_vendor: dict[str | None, list[dict[str, Any]]] = {}
    for contract in map(dict, contracts):
        contracts_by_vendor.setdefault(_key(contract["vendor_id"]), []).append(contract)

    def lookup(lookup_id: Any) -> dict[str, Any] | None:
        key = _key(lookup_id)
        return lookup_by_id.get(key) if key else None

    def label(lookup_id: Any) -> str | None:
        found = lookup(lookup_id)
        return _text(found["label"]) if found else None

    rows: list[dict[str, Any]] = []
    for v in map(dict, vendors):
        vendor_id = _key(v["vendor_id"])
        banks = banks_by_vendor.get(vendor_id)
        bank = _pick_bank(banks) if banks else {}
        vendor_contracts = contracts_by_vendor.get(vendor_id, [])
        status = (_text(v["status"]) or "").lower()

        rows.append({
            "category": label(v["category_code_id"]),
            "sub_category": _text(v["sub_category"]),
            "local_os": _local_os(lookup(v["local_os_id"])),
            "supplier_code": _number_or_text(v["supplier_code"], _NUMERIC_CODE),
            "supplier_reg_year": _text(v["supplier_reg_year"]),
            "supplier_type": _label_or_typed(
                lookup(v["supplier_type_id"]), v["supplier_type_other"]),
            "name": _text(v["name"]),
            "status": _status(status),
            "firm_status": _label_or_typed(
                lookup(v["firm_status_id"]), v["firm_status_other"]),
            "business_type": _label_or_typed(
                lookup(v["business_type_id"]), v["business_type_other"]),
            "core_business": _text(v["core_business"]),
            "contact_person": _text(v["contact_person"]),
            "designation": _text(v["designation"]),
            "address": _text(v["address_line"]),
            "state": _text(v["state"]),
            "city": _text(v["city"]),
            "pin_code": _number_or_text(v["pin_code"], _PIN),
            "phone_company": _text(v["phone_company"]),
            "mobile": _text(v["mobile"]),
            "email": _text(v["email"]),
            "website": _text(v["website"]),
            "fssai": _text(v["fssai_no"]),
            "brc_other": _text(v["brc_other"]),
            "cin": _text(v["cin_no"]),
            "pan": _text(v["pan_no"]),
            "gstn": _text(v["gstn"]),
            "msme": None if v["is_msme"] is None else "YES" if v["is_msme"] else "NO",
            "msme_type": label(v["msme_type_id"]),
            "udyam": _text(v["uam_udyam_no"]),
            "bank_name": _text(bank.get("bank_name")),
            # Kept as text: an account number's leading zeros matter.
            "bank_account_no": _text(bank.get("account_no")),
            "bank_account_name": _text(bank.get("account_name")),
            "bank_branch": _text(bank.get("branch")),
            "bank_ifsc": _text(bank.get("ifsc")),
            "bank_swift": _text(bank.get("swift")),
            "scoc": label(v["scoc_status_id"]),
            "kyc": label(v["kyc_status_id"]),
            "bank_account_type": label(bank.get("account_type_id")),
            "iec": _text(v["iec_no"]),
            "turnover": _text(v["business_turnover_3y"]),
            "capabilities": _text(v["capabilities"]),
            "remarks": _text(v["remarks"]),
            "contract_signed": "Y" if vendor_contracts else "N",
            "contract_dated": _contract_date(vendor_contracts),
            # With no document on file, the vendor's document status stands in.
            "documentation": (_documentation(docs_by_vendor.get(vendor_id, set()))
                              or label(v["doc_status_id"])),
            # The ERP has no field for these two: the columns stay blank by design,
            # for whoever keeps the sheet to fill in by hand.
            "performance": None,
            "hard_copy": None,
            "reference": _text(v["reference"]),
            "pollution_epr": _text(v["pollution_epr"]),
            "tin_tan": _text(v["tin_tan"]),
            "_status": status,
        })

    rows.sort(key=_sort_key)
    return rows
