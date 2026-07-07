"""Bulk extraction + staged submit (the new onboarding pipeline).

Phase 1 — `extract_bulk()`:
    Accepts N files BEFORE any vendor row exists. For each file:
        1. MIME-sniff + size validate (caller does this).
        2. PUT to S3 under a temp staging prefix `vendors/_staging/{staging_id}/...`.
        3. Run `claude_extractor.extract_document_fields(...)` per file.
        4. Aggregate vendor / banking / document / contract suggestions
           across files; surface cross-doc conflicts.
        5. INSERT one `vendor_extraction_staging` row (payload as JSONB).
    Returns the staging_id + the consolidated payload to the caller.

Phase 2 — `submit_staged()`:
    Loads the staging row, applies the operator's edited payload, and
    commits everything in a SINGLE transaction:
        * INSERT vendor_master (history='create', source='extraction')
        * INSERT each vendor_banking row (history='create')
        * INSERT each vendor_document row (history='create')
        * INSERT each vendor_contract row (history='create')
        * UPDATE vendor_extraction_staging.consumed_at + consumed_vendor_id
    Rolls back the whole tree if any step fails — caller can retry with
    the same staging_id.

Staging objects live under a TTL (`expires_at` column, default 24h). A
nightly job (not in scope for this module) sweeps expired rows + their
S3 objects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.modules.vendor import storage as vendor_storage
from app.modules.vendor.schemas import ExtractedDocFields, csv_to_list, list_to_csv
from app.modules.vendor.services import (
    banking_service,
    claude_extractor,
    contract_service,
    document_service,
    vendor_service,
)

logger = logging.getLogger(__name__)


# ── doc-type → field-mapping rules ───────────────────────────────────────


# How each doc type pushes its extracted scalars into the vendor master.
# Only fields with a confident mapping are listed.
_VENDOR_FIELD_MAP: dict[str, dict[str, str]] = {
    "GST":       {"doc_number": "gstn", "business_name": "name", "address": "address_line"},
    "PAN":       {"doc_number": "pan_no", "holder_name": "name"},
    "FSSAI":     {"doc_number": "fssai_no"},
    "CIN":       {"doc_number": "cin_no", "business_name": "name"},
    "IEC":       {"doc_number": "iec_no"},
    "UDYAM":     {"doc_number": "uam_udyam_no"},
    "MSME":      {"doc_number": "uam_udyam_no"},
    "TIN":       {"doc_number": "tin_tan", "business_name": "name"},
    "TAN":       {"doc_number": "tin_tan", "holder_name": "name"},
    "POLLUTION": {"doc_number": "pollution_epr"},
    "EPR":       {"doc_number": "pollution_epr"},
    "BRC":       {"doc_number": "brc_other"},
}

# Direct field-to-column mapping applied to EVERY extracted doc.
# Each doc may carry multiple identifiers (GST cert shows PAN inside the
# GSTIN; invoices show GSTIN + PAN + CIN + contact). We extract whatever
# the model found and route each named slot to its vendor_master column.
# This replaces the doc-type-specific _VENDOR_FIELD_MAP that only looked
# at one identifier per doc.
_DIRECT_MAP: dict[str, str] = {
    # Identifiers
    "gstin":         "gstn",
    "pan":           "pan_no",
    "cin":           "cin_no",
    "fssai":         "fssai_no",
    "iec":           "iec_no",
    "udyam":         "uam_udyam_no",
    "tin":           "tin_tan",
    "tan":           "tin_tan",
    "pollution_epr": "pollution_epr",
    "brc_other":     "brc_other",
    # Entity / address
    "business_name":  "name",
    "holder_name":    "name",
    "contact_person": "contact_person",
    "designation":    "designation",
    "address":        "address_line",
    "city":           "city",
    "state":          "state",
    "pin_code":       "pin_code",
    # Contact
    "mobile":  "mobile",
    "phone":   "phone_company",
    "email":   "email",
    "website": "website",
    # Supplier metadata (Step 2 Basics)
    "nature_of_business": "sub_category",
    "core_business":      "core_business",
    "supplier_reg_year":  "supplier_reg_year",
}

# Dropdown label hints — these are routed via `<column>_label` keys.
# The frontend's `_setFieldValue` matches the label against the SELECT's
# loaded option text. business_category drives the CATEGORY_CODE dropdown;
# supplier_type drives SUPPLIER_TYPE.
_LABEL_HINT_MAP: dict[str, str] = {
    "business_category": "category_code_id_label",
    "supplier_type":     "supplier_type_id_label",
}


# Identifier-pattern inference — when the operator uploaded with "Auto-detect"
# (doc_type=OTHER), figure out what the document actually is from the
# extracted doc_number. This lets us apply the correct typed mapping
# (e.g. a PAN value lands in `pan_no`, not just `name`).
import re as _re  # local alias so we don't pollute module-level namespace

_IDENTIFIER_PATTERNS: tuple[tuple[str, _re.Pattern[str]], ...] = (
    ("GSTIN", _re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")),
    ("PAN",   _re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")),
    ("CIN",   _re.compile(r"^[LUu][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")),
    ("UDYAM", _re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")),
    ("FSSAI", _re.compile(r"^[0-9]{14}$")),
    ("IEC",   _re.compile(r"^[0-9]{10}$")),
)


def _infer_doc_type(extracted: ExtractedDocFields) -> str | None:
    """Pattern-match the extracted doc_number to a known doc type.

    Returns the canonical doc-type key (matching `_VENDOR_FIELD_MAP`) or
    None when nothing matches. Used as a fallback when the operator
    didn't declare a type up front.
    """
    val = (extracted.doc_number or "").strip().upper()
    if not val:
        return None
    for kind, pattern in _IDENTIFIER_PATTERNS:
        if pattern.fullmatch(val):
            # Both kinds map to GST in our field map (GSTIN is the
            # identifier; GST is the doc type that holds it).
            return "GST" if kind == "GSTIN" else kind
    return None


# ── Dropdown label hints ─────────────────────────────────────────────────
#
# The vendor form has several `<select data-lookup="X">` fields backed by
# `lookup_value` rows (firm_status, business_type, msme_type, …). The
# backend can't write `lookup_id` values directly (those vary per DB), so
# we emit human-readable label hints and let the frontend match each
# against its dropdown's loaded options.
#
# The hints are emitted as `<column>_label` keys alongside the typed
# values in `vendor_fields`. The frontend's hydrator picks them up,
# does a case-insensitive contains-match against the live `<option>`
# text, and sets the select to the matching `lookup_id` value.


# CIN suffix 3-letter category code → business-type label.
# Per MCA conventions: chars 8-10 of a CIN identify the constitution.
_CIN_BUSINESS_TYPE: dict[str, str] = {
    "PTC": "Private Limited",
    "PLC": "Public Limited",
    "OPC": "One Person Company",
    "GAP": "General Association of Persons",
    "GAT": "Government",
    "NPL": "Not for Profit",
    "FTC": "Foreign Company",
    "SGC": "State Government Company",
    "ULL": "Unlimited Liability",
}


def _cin_business_type(cin: str | None) -> str | None:
    """Decode the constitution from a CIN. CIN format (21 chars):
    L|U(1) + industry(5) + state(2) + year(4) + constitution(3) + serial(6).
    Constitution is at positions 12-14 (zero-indexed), i.e. slice 12:15.
    """
    if not cin or len(cin) < 15:
        return None
    code = cin[12:15].upper()
    return _CIN_BUSINESS_TYPE.get(code)


# Name-suffix tokens → firm status / business type label hints.
# Matches the trailing words of the legal name (case-insensitive).
_NAME_SUFFIX_HINTS: tuple[tuple[_re.Pattern[str], str, str], ...] = (
    # (regex, firm_status_label, business_type_label)
    (_re.compile(r"\bprivate\s+limited\b|\bpvt\.?\s*ltd\.?\b", _re.IGNORECASE),
        "Private Limited", "Private Limited"),
    (_re.compile(r"\bpublic\s+limited\b|\blimited\b|\bltd\.?\b", _re.IGNORECASE),
        "Public Limited",  "Public Limited"),
    (_re.compile(r"\bllp\b|\blimited\s+liability\s+partnership\b", _re.IGNORECASE),
        "LLP", "LLP"),
    (_re.compile(r"\bopc\b|\bone\s+person\s+company\b", _re.IGNORECASE),
        "One Person Company", "One Person Company"),
    (_re.compile(r"\bpartnership\b", _re.IGNORECASE),
        "Partnership", "Partnership"),
    (_re.compile(r"\bproprietor(ship)?\b|\bsole\s+prop\b", _re.IGNORECASE),
        "Proprietorship", "Proprietorship"),
    (_re.compile(r"\bhuf\b|\bhindu\s+undivided\s+family\b", _re.IGNORECASE),
        "HUF", "HUF"),
    (_re.compile(r"\btrust\b|\bsociety\b", _re.IGNORECASE),
        "Trust / Society", "Trust / Society"),
)


def _firm_status_from_name(name: str | None) -> tuple[str | None, str | None]:
    """Return (firm_status_label, business_type_label) hints from a legal name.

    Matched in order — first hit wins. Most names end in "Pvt Ltd" /
    "LLP" / etc., which uniquely identifies both fields. We surface
    both because some installations keep them separate even though the
    options usually overlap.
    """
    if not name:
        return None, None
    for pattern, firm_label, biz_label in _NAME_SUFFIX_HINTS:
        if pattern.search(name):
            return firm_label, biz_label
    return None, None


def _msme_type_from_category(category: str | None) -> str | None:
    """Extracted `category` from an MSME / UDYAM doc → msme_type label.

    The model puts "Micro" / "Small" / "Medium" into the category field
    when reading an MSME registration. We normalise to title-case so the
    frontend matches against the dropdown options reliably.
    """
    if not category:
        return None
    norm = category.strip().lower()
    for kw in ("micro", "small", "medium"):
        if kw in norm:
            return kw.capitalize()
    return None


# ── Banking / contract row extraction ────────────────────────────────────
# Both are pulled from the typed fields the model now returns directly.
# A row is built only when at least one substantive field is present, so
# uploads that don't carry banking info don't generate empty rows.


def _maybe_banking_row(ext: ExtractedDocFields) -> dict[str, Any] | None:
    """Build a banking row when the doc carried account details.

    Cancelled cheques, passbook pages, bank letters, and some invoices
    list the supplier's account. We require at least a bank_name OR an
    account_no to consider it a real row — otherwise a doc that
    happens to mention "Bank of India" in the address line wouldn't
    falsely add a row.
    """
    has_signal = bool(
        (ext.bank_name and ext.bank_name.strip()) or
        (ext.bank_account_no and ext.bank_account_no.strip())
    )
    if not has_signal:
        return None
    return {
        "bank_name":       ext.bank_name or "",
        "account_no":      ext.bank_account_no or "",
        "account_name":    ext.bank_account_name or ext.business_name or "",
        "branch":          ext.bank_branch or "",
        "ifsc":            ext.bank_ifsc or "",
        "swift":           ext.bank_swift or "",
        # The frontend keeps `account_type_id` as a SELECT — surface the
        # raw label so it can label-match against the ACCOUNT_TYPE dropdown.
        "account_type_label": ext.bank_account_type or "",
        "is_primary":      False,   # operator picks which is primary
        "is_active":       True,
    }


def _maybe_contract_row(s3_url: str, ext: ExtractedDocFields) -> dict[str, Any] | None:
    """Build a contract row when the doc is itself a contract / MSA / NDA.

    Triggered by any contract-specific field being populated. We pass
    through the S3 URL so the contract row keeps the original PDF
    reference once persisted.
    """
    has_signal = bool(
        (ext.contract_type and ext.contract_type.strip()) or
        ext.contract_value is not None or
        (ext.contract_summary and ext.contract_summary.strip())
    )
    if not has_signal:
        return None
    return {
        "s3_url":         s3_url,
        "contract_type":  ext.contract_type,
        # The model populates issued_on / valid_from / valid_to in the
        # generic date slots for contract docs too — alias them onto
        # the contract field names the row schema expects.
        "signed_date":    ext.issued_on,
        "effective_from": ext.valid_from,
        "effective_to":   ext.valid_to,
        "value_inr":      ext.contract_value,
        "summary":        ext.contract_summary,
        "scoc_signed":    False,
        "auto_renew":     False,
    }


# ── conflict detection ───────────────────────────────────────────────────


def _detect_conflicts(
    field_sources: dict[str, list[tuple[str, Any]]],
) -> list[dict[str, Any]]:
    """For each vendor-master field, if multiple docs proposed *different*
    values, surface them so the UI can prompt the operator to choose.

    Identical values across sources collapse silently (no false alarm).
    """
    conflicts: list[dict[str, Any]] = []
    for field, hits in field_sources.items():
        # hits: [(source_doc_type, value), ...]
        unique = []
        seen = set()
        for src, val in hits:
            sig = str(val).strip().lower() if val is not None else ""
            if not sig or sig in seen:
                continue
            seen.add(sig)
            unique.append((src, val))
        if len(unique) >= 2:
            conflicts.append({
                "field":       field,
                "values_seen": [v for _, v in unique],
                "sources":     [s for s, _ in unique],
            })
    return conflicts


# ── aggregation ──────────────────────────────────────────────────────────


def _aggregate(
    extracted_per_doc: list[tuple[str, str, ExtractedDocFields]],
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Take (s3_url, doc_type, ExtractedDocFields) per file → tuple of:
        (vendor_fields, banking_fields, documents_out, conflicts, contracts_out).

    Banking and contract rows are populated when the model found
    account / contract details on the uploaded docs (cancelled cheque,
    passbook, MSA, NDA, etc.). Operator can edit / add / remove in the
    wizard.
    """
    vendor_fields: dict[str, Any] = {}
    banking_fields: list[dict[str, Any]] = []
    contracts_out: list[dict[str, Any]] = []
    documents_out: list[dict[str, Any]] = []
    field_sources: dict[str, list[tuple[str, Any]]] = {}

    for s3_url, doc_type, ext in extracted_per_doc:
        # When the operator picked "Auto-detect" the doc_type is OTHER
        # (or empty). Try to infer the real type from the extracted
        # doc_number pattern so we can apply the right typed mapping
        # (PAN → pan_no, GST → gstn, etc.) — otherwise the vendor form
        # would never pre-fill for auto-detected uploads.
        effective_type = doc_type
        if doc_type in ("OTHER", "", None) or doc_type not in _VENDOR_FIELD_MAP:
            inferred = _infer_doc_type(ext)
            if inferred:
                effective_type = inferred

        # Per-doc representation kept for `documents[]` in the response;
        # the operator can edit each before submit. Surface every field
        # the model populated so the UI / operator can verify the
        # extraction independently per file.
        documents_out.append({
            "s3_url":             s3_url,
            "doc_type":           effective_type,
            "doc_type_declared":  doc_type,
            "doc_number":         ext.doc_number,
            # Cross-identifiers
            "gstin":              ext.gstin,
            "pan":                ext.pan,
            "cin":                ext.cin,
            "fssai":              ext.fssai,
            "iec":                ext.iec,
            "udyam":              ext.udyam,
            "tin":                ext.tin,
            "tan":                ext.tan,
            "pollution_epr":      ext.pollution_epr,
            "brc_other":          ext.brc_other,
            # Dates
            "issued_on":          ext.issued_on,
            "valid_from":         ext.valid_from,
            "valid_to":           ext.valid_to,
            # Entity / supplier meta
            "business_name":      ext.business_name,
            "holder_name":        ext.holder_name,
            "issuing_authority":  ext.issuing_authority,
            "contact_person":     ext.contact_person,
            "designation":        ext.designation,
            "business_category":  ext.business_category,
            "supplier_type":      ext.supplier_type,
            "nature_of_business": ext.nature_of_business,
            "core_business":      ext.core_business,
            "supplier_reg_year":  ext.supplier_reg_year,
            # Address breakdown
            "address":            ext.address,
            "city":               ext.city,
            "state":              ext.state,
            "pin_code":           ext.pin_code,
            # Contact
            "mobile":             ext.mobile,
            "phone":              ext.phone,
            "email":              ext.email,
            "website":            ext.website,
            # Banking + contract (when the doc was those)
            "bank_name":          ext.bank_name,
            "bank_branch":        ext.bank_branch,
            "bank_account_no":    ext.bank_account_no,
            "bank_account_name":  ext.bank_account_name,
            "bank_ifsc":          ext.bank_ifsc,
            "bank_swift":         ext.bank_swift,
            "bank_account_type":  ext.bank_account_type,
            "contract_type":      ext.contract_type,
            "contract_value":     ext.contract_value,
            "contract_summary":   ext.contract_summary,
            "category":           ext.category,
            "additional_notes":   ext.additional_notes,
            "extraction_status":  ext.extraction_status,
            "extraction_error":   ext.extraction_error,
        })

        # Every typed slot the model populated lands on the vendor master.
        # No doc-type gating: a PAN doc that happens to show the GSTIN
        # contributes both. First non-empty value per column wins;
        # conflict detection runs on the full set of seen values.
        for src_key, vendor_col in _DIRECT_MAP.items():
            val = getattr(ext, src_key, None)
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            field_sources.setdefault(vendor_col, []).append((effective_type, val))
            vendor_fields.setdefault(vendor_col, val)

        # Dropdown label hints — same idea but the value is a free-text
        # label the frontend matches against its lookup-backed select.
        for src_key, hint_key in _LABEL_HINT_MAP.items():
            val = getattr(ext, src_key, None)
            if val and isinstance(val, str) and val.strip():
                vendor_fields.setdefault(hint_key, val.strip())

        # Banking row if the doc carried account details (cancelled
        # cheque, passbook, bank letter, invoice letterhead).
        b_row = _maybe_banking_row(ext)
        if b_row:
            banking_fields.append(b_row)

        # Contract row if the doc is itself a contract / MSA / NDA.
        c_row = _maybe_contract_row(s3_url, ext)
        if c_row:
            contracts_out.append(c_row)

        # ── Dropdown label hints + typed extras ────────────────────────
        # Emitted as `<column>_label` keys; the frontend matches the
        # label against the dropdown's loaded option text.

        # MSME / UDYAM ⇒ flip is_msme and pull the registration date.
        if effective_type in ("MSME", "UDYAM"):
            vendor_fields.setdefault("is_msme", True)
            if ext.valid_from:
                vendor_fields.setdefault("msme_registration_date", ext.valid_from)
            msme_label = _msme_type_from_category(ext.category)
            if msme_label:
                vendor_fields.setdefault("msme_type_id_label", msme_label)

        # CIN ⇒ business_type label from the 3-letter constitution code.
        if effective_type == "CIN" and ext.doc_number:
            biz_label = _cin_business_type(ext.doc_number)
            if biz_label:
                vendor_fields.setdefault("business_type_id_label", biz_label)

        # Legal-name suffix ⇒ firm_status + business_type hints.
        # Runs on every doc that returned a business_name; the first
        # hit per field wins via setdefault().
        firm_label, biz_label = _firm_status_from_name(ext.business_name)
        if firm_label:
            vendor_fields.setdefault("firm_status_id_label", firm_label)
        if biz_label:
            vendor_fields.setdefault("business_type_id_label", biz_label)

    conflicts = _detect_conflicts(field_sources)
    return vendor_fields, banking_fields, documents_out, conflicts, contracts_out


# ── Phase 1: extract_bulk ────────────────────────────────────────────────


def _new_staging_id() -> str:
    """`ext_` + 12 hex chars. Url-safe, easy to spot in logs."""
    return "ext_" + secrets.token_hex(6)


async def extract_bulk(
    pool: asyncpg.Pool,
    settings: Any,
    files: list[tuple[bytes, str, str | None, str | None]],
    actor_user_id: str,
) -> dict[str, Any]:
    """Phase-1 entrypoint.

    `files` is a list of `(content_bytes, mime_type, original_filename,
    declared_doc_type_or_None)`. The router pre-validates MIME + size.

    Returns the consolidated extraction payload, also persisted in
    `vendor_extraction_staging` keyed by `staging_id`.
    """
    staging_id = _new_staging_id()
    s3_prefix  = f"vendors/_staging/{staging_id}"
    backend    = vendor_storage.get_vendor_storage(settings)

    # 1. For each file: S3 PUT and Claude extraction run CONCURRENTLY.
    # Neither path depends on the other (both consume the same in-memory
    # bytes), so firing them together cuts wall-clock to ~max(S3, Claude)
    # per file instead of S3 + Claude. We tolerate independent failures —
    # a successful Claude extraction with a failed S3 upload still pre-
    # fills the vendor form; a successful S3 upload with a failed Claude
    # extraction still parks the file for the operator to fill manually.
    async def _process_one(
        idx: int,
        content: bytes,
        mime: str,
        fname: str | None,
        declared_type: str | None,
    ) -> tuple[int, str | None, str, ExtractedDocFields | Exception, Exception | None]:
        doc_type = declared_type or "OTHER"
        key = vendor_storage.new_vendor_key(
            supplier_code=staging_id,
            doc_type=doc_type,
            mime_type=mime,
            original_filename=fname,
        ).replace("vendors/", "vendors/_staging/", 1)

        # boto3 PUT is sync — run it on a thread so it doesn't block the
        # event loop while Claude streams.
        s3_task      = asyncio.to_thread(backend.put, key, content, mime)
        extract_task = claude_extractor.extract_document_fields(
            file_bytes=content, mime_type=mime, doc_type=doc_type,
        )

        # `return_exceptions=True` so one path failing doesn't cancel the
        # other (gather's default is to raise + cancel siblings).
        s3_result, ext_result = await asyncio.gather(
            s3_task, extract_task, return_exceptions=True,
        )

        if isinstance(s3_result, Exception):
            s3_url, s3_err = None, s3_result
        else:
            s3_url, s3_err = s3_result, None

        # claude_extractor never raises (it returns a failed
        # ExtractedDocFields on any exception). The Exception branch
        # here is belt-and-braces for an unexpected `to_thread` shape.
        return idx, s3_url, doc_type, ext_result, s3_err

    results = await asyncio.gather(*[
        _process_one(i, c, m, f, t) for i, (c, m, f, t) in enumerate(files)
    ])

    # 2. Demultiplex results into the ordered (s3_url, doc_type, ext)
    # tuples the aggregator expects, plus a failures list for S3 errors.
    failures: list[dict[str, Any]] = []
    ordered: list[tuple[str, str, ExtractedDocFields]] = []

    for idx, url, doc_type, ext_result, s3_err in results:
        # Coerce any stray exception from the extraction task into a
        # failed ExtractedDocFields so downstream code stays uniform.
        if isinstance(ext_result, Exception):
            ext_result = ExtractedDocFields(
                extraction_status="failed",
                extraction_error=repr(ext_result),
            )
        if s3_err is not None:
            failures.append({
                "index": idx,
                "doc_type": doc_type,
                "error": f"upload_failed: {s3_err!r}",
            })
        # Include in documents even on S3 failure — the extracted scalar
        # fields still pre-fill the vendor form. The UI filters out docs
        # without `s3_url` when building the submit-staged payload.
        ordered.append((url or "", doc_type, ext_result))

    # 3. Aggregate suggestions across docs.
    vendor_fields, banking_fields, documents_out, conflicts, contracts_out = _aggregate(ordered)

    payload = {
        "staging_id":     staging_id,
        "vendor_fields":  vendor_fields,
        "banking_fields": banking_fields,
        "documents":      documents_out,
        "contracts":      contracts_out,
        "conflicts":      conflicts,
        "failures":       failures,
    }

    # 4. Persist the staging row. If the migration hasn't been applied we
    # surface a structured error instead of a 500 stacktrace.
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO vendor_extraction_staging
                  (staging_id, created_by, payload, s3_staging_prefix)
                VALUES ($1, $2, $3::jsonb, $4)
                """,
                staging_id, actor_user_id, json.dumps(payload), s3_prefix,
            )
        except asyncpg.UndefinedTableError as e:
            raise StagingError(
                "migration_not_applied",
                "vendor_extraction_staging table missing — apply migration 030_vendor_history.sql",
            ) from e
    return payload


# ── Phase 2: submit_staged ───────────────────────────────────────────────


class StagingError(ValueError):
    """Raised on staging-id problems (not found, expired, already consumed).
    Carries a stable `code` for the router to map onto a 4xx envelope."""

    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


async def submit_staged(
    pool: asyncpg.Pool,
    settings: Any,
    *,
    staging_id: str | None,
    vendor: dict[str, Any],
    banking: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
    actor_user_id: str,
) -> dict[str, Any]:
    """Phase-2 entrypoint.

    All four tables commit together. The staging row (if any) is marked
    consumed; failures roll the whole tree back so the caller can retry
    with the same `staging_id`.

    `documents[*]` carries the already-uploaded s3_url from phase-1 plus
    the operator's edited scalar fields. We don't re-extract.
    """
    # Locate the staging row when `staging_id` is supplied. The endpoint
    # also accepts a fully-manual submit (no extraction phase), in which
    # case staging_id is None and we just write directly.
    staging_row: dict[str, Any] | None = None
    if staging_id:
        async with pool.acquire() as conn:
            staging_row = await conn.fetchrow(
                "SELECT staging_id, expires_at, consumed_at, s3_staging_prefix "
                "  FROM vendor_extraction_staging WHERE staging_id = $1",
                staging_id,
            )
        if staging_row is None:
            raise StagingError("staging_not_found", f"staging_id {staging_id!r} not found")
        if staging_row["consumed_at"] is not None:
            raise StagingError(
                "staging_already_consumed",
                "this staging payload was already submitted",
            )
        if staging_row["expires_at"] < datetime.now(timezone.utc):
            raise StagingError("staging_expired", "staging payload expired — re-run extraction")

    # The vendor row commits first inside a single transaction. Children
    # are inserted using their service helpers but with the same `conn`
    # so a failure rolls back vendor + children + staging mark together.
    async with pool.acquire() as conn:
        async with conn.transaction():
            vendor_row = await _insert_vendor_in_conn(
                conn, vendor, actor_user_id, source="extraction" if staging_id else "manual",
            )
            vendor_id = vendor_row["vendor_id"]

            banking_rows = []
            for b in banking:
                banking_rows.append(
                    await _insert_banking_in_conn(
                        conn, vendor_id, b, actor_user_id,
                        source="extraction" if staging_id else "manual",
                    )
                )
            document_rows = []
            for d in documents:
                document_rows.append(
                    await _insert_document_in_conn(
                        conn, vendor_id, d, actor_user_id,
                        source="extraction" if staging_id else "manual",
                    )
                )
            contract_rows = []
            for c in contracts:
                contract_rows.append(
                    await _insert_contract_in_conn(
                        conn, vendor_id, c, actor_user_id,
                        source="extraction" if staging_id else "manual",
                    )
                )

            if staging_id:
                await conn.execute(
                    """
                    UPDATE vendor_extraction_staging
                       SET consumed_at = now(),
                           consumed_vendor_id = $2
                     WHERE staging_id = $1
                    """,
                    staging_id, vendor_id,
                )

    return {
        "vendor":    vendor_row,
        "banking":   banking_rows,
        "documents": document_rows,
        "contracts": contract_rows,
    }


# ── txn-bound inserts (re-implement the service helpers on a shared conn) ─


# We can't reuse the service-layer helpers because they each `acquire()` a
# fresh connection. To keep submit_staged atomic across all 4 tables we
# repeat the INSERT + history-record body inline using the shared `conn`.

from app.modules.vendor.services import history_service  # noqa: E402
from app.modules.vendor.services.vendor_service import _VENDOR_COLUMNS, _next_supplier_code  # noqa: E402


async def _insert_vendor_in_conn(
    conn: asyncpg.Connection,
    payload: dict[str, Any],
    actor_user_id: str | None,
    *,
    source: str,
) -> dict[str, Any]:
    data = dict(payload)
    data.setdefault("status", "active")
    data["created_by"] = actor_user_id
    data["updated_by"] = actor_user_id
    if not data.get("supplier_code"):
        data["supplier_code"] = await _next_supplier_code(conn)
    cols = [
        "supplier_code", "supplier_reg_year", "name", "status",
        "supplier_type_id", "firm_status_id", "business_type_id",
        "category_code_id", "sub_category", "local_os_id",
        "core_business", "contact_person", "designation", "phone_company",
        "mobile", "email", "website", "address_line", "state", "city",
        "pin_code", "fssai_no", "brc_other", "cin_no", "pan_no", "gstn",
        "iec_no", "pollution_epr", "tin_tan", "is_msme",
        "msme_registration_date", "msme_type_id", "uam_udyam_no",
        "business_turnover_3y", "capabilities", "remarks", "reference",
        "scoc_status_id", "kyc_status_id", "doc_status_id", "approved_by",
        "approved_at", "created_by", "updated_by",
    ]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    values = [data.get(c) for c in cols]
    sql = (
        f"INSERT INTO vendor_master ({', '.join(cols)}) "
        f"VALUES ({placeholders}) RETURNING {_VENDOR_COLUMNS}"
    )
    row = await conn.fetchrow(sql, *values)
    result = dict(row)
    await history_service.record_history(
        conn, "vendor",
        operation="create",
        parent_id=str(result["vendor_id"]),
        new_state=result,
        actor_user_id=actor_user_id,
        source=source,
    )
    return result


async def _insert_banking_in_conn(
    conn: asyncpg.Connection,
    vendor_id: str,
    payload: dict[str, Any],
    actor_user_id: str | None,
    *,
    source: str,
) -> dict[str, Any]:
    cols = [
        "vendor_id", "bank_name", "account_no", "account_name", "branch",
        "ifsc", "swift", "account_type_id", "is_primary", "is_active",
        "valid_from", "valid_to",
    ]
    data = dict(payload)
    data["vendor_id"] = vendor_id
    data.setdefault("is_active", True)
    if data.get("is_primary"):
        await conn.execute(
            "UPDATE vendor_banking SET is_primary = false "
            " WHERE vendor_id = $1 AND is_primary = true",
            vendor_id,
        )
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    values = [data.get(c) for c in cols]
    row = await conn.fetchrow(
        f"INSERT INTO vendor_banking ({', '.join(cols)}) "
        f"VALUES ({placeholders}) RETURNING bank_id, vendor_id, bank_name, "
        f"account_no, account_name, branch, ifsc, swift, account_type_id, "
        f"is_primary, is_active, valid_from, valid_to, created_at",
        *values,
    )
    result = dict(row)
    await history_service.record_history(
        conn, "banking",
        operation="create",
        parent_id=str(result["bank_id"]),
        vendor_id=str(vendor_id),
        new_state=result,
        actor_user_id=actor_user_id,
        source=source,
    )
    return result


async def _insert_document_in_conn(
    conn: asyncpg.Connection,
    vendor_id: str,
    payload: dict[str, Any],
    actor_user_id: str | None,
    *,
    source: str,
) -> dict[str, Any]:
    cols = [
        "vendor_id", "doc_type", "doc_number", "s3_urls", "issued_on",
        "valid_from", "valid_to", "status_id", "uploaded_by",
    ]
    data = dict(payload)
    data["vendor_id"] = vendor_id
    data["uploaded_by"] = actor_user_id
    # The wire shape from /submit-staged sends `s3_url` (singular); fold
    # it into the CSV column the table expects.
    if "s3_url" in data and not data.get("s3_urls"):
        single = data.pop("s3_url")
        data["s3_urls"] = list_to_csv([single] if single else [])
    else:
        data["s3_urls"] = list_to_csv(csv_to_list(data.get("s3_urls") or ""))
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    values = [data.get(c) for c in cols]
    row = await conn.fetchrow(
        f"INSERT INTO vendor_document ({', '.join(cols)}) "
        f"VALUES ({placeholders}) RETURNING doc_id, vendor_id, doc_type, "
        f"doc_number, s3_urls, issued_on, valid_from, valid_to, status_id, "
        f"uploaded_by, uploaded_at",
        *values,
    )
    result = dict(row)
    await history_service.record_history(
        conn, "document",
        operation="create",
        parent_id=str(result["doc_id"]),
        vendor_id=str(vendor_id),
        new_state=result,
        actor_user_id=actor_user_id,
        source=source,
    )
    return result


async def _insert_contract_in_conn(
    conn: asyncpg.Connection,
    vendor_id: str,
    payload: dict[str, Any],
    actor_user_id: str | None,
    *,
    source: str,
) -> dict[str, Any]:
    cols = [
        "vendor_id", "contract_type", "signed_date", "effective_from",
        "effective_to", "s3_urls", "scoc_signed", "value_inr",
        "auto_renew", "created_by",
    ]
    data = dict(payload)
    data["vendor_id"] = vendor_id
    data["created_by"] = actor_user_id
    if "s3_url" in data and not data.get("s3_urls"):
        single = data.pop("s3_url")
        data["s3_urls"] = list_to_csv([single] if single else [])
    else:
        data["s3_urls"] = list_to_csv(csv_to_list(data.get("s3_urls") or ""))
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    values = [data.get(c) for c in cols]
    row = await conn.fetchrow(
        f"INSERT INTO vendor_contract ({', '.join(cols)}) "
        f"VALUES ({placeholders}) RETURNING contract_id, vendor_id, "
        f"contract_type, signed_date, effective_from, effective_to, s3_urls, "
        f"scoc_signed, value_inr, auto_renew, created_by, created_at",
        *values,
    )
    result = dict(row)
    await history_service.record_history(
        conn, "contract",
        operation="create",
        parent_id=str(result["contract_id"]),
        vendor_id=str(vendor_id),
        new_state=result,
        actor_user_id=actor_user_id,
        source=source,
    )
    return result
