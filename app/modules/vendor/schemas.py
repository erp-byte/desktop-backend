"""Pydantic models for /api/v1/vendors endpoints.

Shape highlights:
    * `s3_urls` is exposed as a comma-separated string on the wire
      (`"url1,url2,url3"`) and stored that way in the DB after the DDL
      block migration. Helpers `csv_to_list` / `list_to_csv` translate
      between the wire/DB string and a Python list when needed inside
      service code.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── enums ────────────────────────────────────────────────────────────────


VendorStatus = Literal["active", "inactive", "blacklisted"]
ContractType = Literal["yearly", "one-time", "NDA", "MSA"]
# Order chosen to match the DB CHECK constraint values currently seeded.
DocType = Literal[
    "FSSAI", "BRC", "PAN", "GST", "MSME", "UDYAM", "IEC", "EPR",
    "CIN", "TIN", "TAN", "POLLUTION", "CONTRACT", "OTHER",
]


# ── Indian compliance regex patterns ─────────────────────────────────────


# GSTIN: 15 chars — 2 state digits, 5 PAN letters, 4 PAN digits, 1 PAN letter,
# 1 entity digit/letter, literal 'Z', 1 checksum digit/letter
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
# PAN: 10 chars — AAAAA9999A
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
# FSSAI: 14 digits
_FSSAI_RE = re.compile(r"^[0-9]{14}$")
# CIN: 21 chars — L/U + 5 digits + 2 state letters + 4 year digits + 3 letters + 6 digits
_CIN_RE = re.compile(r"^[LUu][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
# IEC: 10 digits
_IEC_RE = re.compile(r"^[0-9]{10}$")
# UDYAM: UDYAM-XX-00-0000000
_UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")


def _validate_pattern(value: str | None, pattern: re.Pattern[str], label: str) -> str | None:
    """Return upper-cased / stripped value when it matches, else raise.

    Empty / None passes through unchanged so the validator stays
    compatible with the optional + nullable column shape.
    """
    if value is None:
        return None
    v = value.strip().upper()
    if not v:
        return None
    if not pattern.fullmatch(v):
        raise ValueError(f"invalid {label} format: {value!r}")
    return v


# ── csv <-> list helpers (s3_urls translation) ───────────────────────────


def csv_to_list(csv: str | None) -> list[str]:
    if not csv:
        return []
    return [u.strip() for u in csv.split(",") if u.strip()]


def list_to_csv(urls: list[str] | None) -> str:
    if not urls:
        return ""
    return ",".join(u.strip() for u in urls if u and u.strip())


# ── vendor_master ────────────────────────────────────────────────────────


class VendorBase(BaseModel):
    model_config = ConfigDict(extra="ignore")
    supplier_code: str | None = Field(default=None, max_length=64)
    supplier_reg_year: str | None = Field(default=None, max_length=16)
    name: str = Field(min_length=2, max_length=512)
    status: VendorStatus = "active"

    # FKs to lookup_value (optional)
    supplier_type_id: str | None = None
    firm_status_id: str | None = None
    business_type_id: str | None = None
    category_code_id: str | None = None
    sub_category: str | None = None
    local_os_id: str | None = None
    msme_type_id: str | None = None
    scoc_status_id: str | None = None
    kyc_status_id: str | None = None
    doc_status_id: str | None = None

    # Free-text "Others" companion values. Populated only when the matching
    # *_id lookup resolves to the "Others" option on the form; NULL otherwise.
    supplier_type_other: str | None = Field(default=None, max_length=200)
    firm_status_other: str | None = Field(default=None, max_length=200)
    business_type_other: str | None = Field(default=None, max_length=200)

    core_business: str | None = None
    contact_person: str | None = None
    designation: str | None = None
    phone_company: str | None = None
    mobile: str | None = None
    email: str | None = None
    website: str | None = None
    address_line: str | None = None
    state: str | None = None
    city: str | None = None
    pin_code: str | None = None

    # Indian compliance fields
    fssai_no: str | None = None
    brc_other: str | None = None
    cin_no: str | None = None
    pan_no: str | None = None
    gstn: str | None = None
    iec_no: str | None = None
    pollution_epr: str | None = None
    tin_tan: str | None = None

    is_msme: bool = False
    msme_registration_date: date | None = None
    uam_udyam_no: str | None = None
    business_turnover_3y: str | None = None
    capabilities: str | None = None
    remarks: str | None = None
    reference: str | None = None

    # ── Indian compliance format validators ────────────────────────────
    # `mode="before"` lets the validator normalise (strip / upper) before
    # type coercion. Empty string ⇒ None ⇒ skip validation.

    @field_validator("gstn", mode="before")
    @classmethod
    def _v_gstn(cls, v: Any) -> str | None:
        return _validate_pattern(v, _GSTIN_RE, "GSTIN")

    @field_validator("pan_no", mode="before")
    @classmethod
    def _v_pan(cls, v: Any) -> str | None:
        return _validate_pattern(v, _PAN_RE, "PAN")

    @field_validator("fssai_no", mode="before")
    @classmethod
    def _v_fssai(cls, v: Any) -> str | None:
        return _validate_pattern(v, _FSSAI_RE, "FSSAI (14 digits)")

    @field_validator("cin_no", mode="before")
    @classmethod
    def _v_cin(cls, v: Any) -> str | None:
        return _validate_pattern(v, _CIN_RE, "CIN (21 chars)")

    @field_validator("iec_no", mode="before")
    @classmethod
    def _v_iec(cls, v: Any) -> str | None:
        return _validate_pattern(v, _IEC_RE, "IEC (10 digits)")

    @field_validator("uam_udyam_no", mode="before")
    @classmethod
    def _v_udyam(cls, v: Any) -> str | None:
        return _validate_pattern(v, _UDYAM_RE, "UDYAM (UDYAM-XX-00-0000000)")


class VendorCreateRequest(VendorBase):
    """POST /api/v1/vendors body."""


class VendorUpdateRequest(BaseModel):
    """PATCH /api/v1/vendors/{vendor_id} — every field optional."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    supplier_code: str | None = None
    supplier_reg_year: str | None = None
    name: str | None = None
    status: VendorStatus | None = None

    supplier_type_id: str | None = None
    firm_status_id: str | None = None
    business_type_id: str | None = None
    category_code_id: str | None = None
    sub_category: str | None = None
    local_os_id: str | None = None
    msme_type_id: str | None = None
    scoc_status_id: str | None = None
    kyc_status_id: str | None = None
    doc_status_id: str | None = None

    supplier_type_other: str | None = None
    firm_status_other: str | None = None
    business_type_other: str | None = None

    core_business: str | None = None
    contact_person: str | None = None
    designation: str | None = None
    phone_company: str | None = None
    mobile: str | None = None
    email: str | None = None
    website: str | None = None
    address_line: str | None = None
    state: str | None = None
    city: str | None = None
    pin_code: str | None = None

    fssai_no: str | None = None
    brc_other: str | None = None
    cin_no: str | None = None
    pan_no: str | None = None
    gstn: str | None = None
    iec_no: str | None = None
    pollution_epr: str | None = None
    tin_tan: str | None = None

    is_msme: bool | None = None
    msme_registration_date: date | None = None
    uam_udyam_no: str | None = None
    business_turnover_3y: str | None = None
    capabilities: str | None = None
    remarks: str | None = None
    reference: str | None = None
    # Optional "why" note that lands on the history row but never on the
    # vendor_master table itself. Stripped by the service layer.
    reason: str | None = Field(default=None, alias="_reason")
    # `approved_by` / `approved_at` are intentionally NOT patchable here.
    # Use POST /api/v1/vendors/{id}/approve (admin / SCM Head) instead.

    # Same compliance validators as VendorBase — duplicated here because
    # `VendorUpdateRequest` does not inherit from `VendorBase` (different
    # required/nullability rules).

    @field_validator("gstn", mode="before")
    @classmethod
    def _v_gstn(cls, v: Any) -> str | None:
        return _validate_pattern(v, _GSTIN_RE, "GSTIN")

    @field_validator("pan_no", mode="before")
    @classmethod
    def _v_pan(cls, v: Any) -> str | None:
        return _validate_pattern(v, _PAN_RE, "PAN")

    @field_validator("fssai_no", mode="before")
    @classmethod
    def _v_fssai(cls, v: Any) -> str | None:
        return _validate_pattern(v, _FSSAI_RE, "FSSAI (14 digits)")

    @field_validator("cin_no", mode="before")
    @classmethod
    def _v_cin(cls, v: Any) -> str | None:
        return _validate_pattern(v, _CIN_RE, "CIN (21 chars)")

    @field_validator("iec_no", mode="before")
    @classmethod
    def _v_iec(cls, v: Any) -> str | None:
        return _validate_pattern(v, _IEC_RE, "IEC (10 digits)")

    @field_validator("uam_udyam_no", mode="before")
    @classmethod
    def _v_udyam(cls, v: Any) -> str | None:
        return _validate_pattern(v, _UDYAM_RE, "UDYAM (UDYAM-XX-00-0000000)")


class VendorResponse(BaseModel):
    """Full vendor row returned by GET endpoints."""

    model_config = ConfigDict(extra="allow")
    vendor_id: str
    supplier_code: str
    name: str
    status: VendorStatus
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    is_deleted: bool = False
    # `extra=allow` carries the rest of the columns through as-is.


class VendorCounts(BaseModel):
    """Per-vendor aggregate of related sub-rows. Returned by list /
    search endpoints so the UI can render a summary card without firing
    a separate request per vendor."""
    documents: int = 0
    contracts: int = 0
    banking:   int = 0


class VendorListRow(VendorResponse):
    """Enriched vendor row for list / search endpoints.

    `is_approved` collapses the two approval columns into a single
    boolean the UI can sort on. `counts` is the per-vendor sub-row
    aggregate (one row per vendor; no N+1 fetches from the client).
    `has_primary_banking` mirrors the approval pre-condition the
    /approve endpoint enforces, so the UI can flag pending vendors
    that are blocked on banking setup."""
    is_approved:         bool = False
    has_primary_banking: bool = False
    counts:              VendorCounts = Field(default_factory=VendorCounts)


# Approval filter used by the enriched list / search endpoints.
ApprovalFilter = Literal["approved", "pending"]


class VendorListResponse(BaseModel):
    """Response shape for the paginated list endpoints. Rows are sorted
    approved-first, then alphabetically by name — the UI can group on
    `is_approved` for two visual sections, or render the flat list as-is."""
    vendors:   list[VendorListRow]
    total:     int
    page:      int
    page_size: int


class VendorSearchResponse(BaseModel):
    """Response shape for the no-pagination search endpoint.

    `truncated=true` means the result hit the server-side hard cap
    (default 1000) before exhausting matches — the UI should prompt the
    user to narrow the query. `total_returned` is the row count in
    `vendors`; there is no separate `total` since we don't run a
    pre-count query (the point of this endpoint is one round-trip)."""
    vendors:        list[VendorListRow]
    total_returned: int
    truncated:      bool


# ── vendor_banking ───────────────────────────────────────────────────────


class BankingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bank_name: str = Field(min_length=1)
    account_no: str = Field(min_length=4)
    account_name: str = Field(min_length=1)
    branch: str | None = None
    ifsc: str | None = Field(default=None, max_length=11)
    swift: str | None = Field(default=None, max_length=11)
    account_type_id: str | None = None
    is_primary: bool = False
    is_active: bool = True
    valid_from: date | None = None
    valid_to: date | None = None

    @field_validator("ifsc")
    @classmethod
    def _ifsc_format(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        v = v.upper().strip()
        # IFSC: AAAA0XXXXXX  (4 letters, 0, 6 alphanumeric)
        if len(v) != 11 or not v[:4].isalpha() or v[4] != "0":
            raise ValueError("ifsc must be 11 chars: 4 letters, '0', 6 alphanumeric")
        return v


class BankingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    bank_name: str | None = None
    account_no: str | None = None
    account_name: str | None = None
    branch: str | None = None
    ifsc: str | None = None
    swift: str | None = None
    account_type_id: str | None = None
    is_primary: bool | None = None
    is_active: bool | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    reason: str | None = Field(default=None, alias="_reason")


class BankingResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    bank_id: str
    vendor_id: str
    bank_name: str
    account_no: str
    account_name: str
    is_primary: bool
    is_active: bool


# ── vendor_document ──────────────────────────────────────────────────────


class DocumentManualCreate(BaseModel):
    """POST /api/v1/vendors/{vendor_id}/documents — manual entry path.

    Client supplies all fields, including (optionally) a comma-separated
    `s3_urls` string of already-uploaded files.
    """

    model_config = ConfigDict(extra="ignore")
    doc_type: DocType
    doc_number: str | None = None
    s3_urls: str = ""  # comma-separated
    issued_on: date | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    status_id: str | None = None


class DocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    doc_type: DocType | None = None
    doc_number: str | None = None
    s3_urls: str | None = None
    issued_on: date | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    status_id: str | None = None
    reason: str | None = Field(default=None, alias="_reason")


class DocumentResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    doc_id: str
    vendor_id: str
    doc_type: str
    doc_number: str | None = None
    s3_urls: str = ""  # comma-separated
    issued_on: date | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    status_id: str | None = None
    uploaded_by: str | None = None
    uploaded_at: datetime | None = None


# ── extraction (Claude API) ──────────────────────────────────────────────


# Status the Claude extractor reports alongside the parsed fields so the
# caller can distinguish "extraction succeeded with empty fields" (rare —
# truly unparseable doc) from "extraction failed" (rate limit, network,
# bad API key, password-protected PDF, …).
ExtractionStatus = Literal["ok", "failed", "skipped"]


class ExtractedDocFields(BaseModel):
    """Generic extraction result — fields the LLM populates when present.

    The router maps populated keys back to vendor_document columns:
        doc_number       → vendor_document.doc_number
        issued_on        → vendor_document.issued_on
        valid_from       → vendor_document.valid_from
        valid_to         → vendor_document.valid_to
    Anything else useful goes into `additional_notes` as a free-text blob.

    `extraction_status` indicates whether the LLM call succeeded:
        * "ok"      — model returned a parsed payload (fields may still be null)
        * "failed"  — model call raised / returned unparseable output
        * "skipped" — extraction was bypassed (no API key, etc.)
    `extraction_error` carries a short repr of the underlying error when
    status is "failed".

    Why all-string / no-typed-dates: Anthropic's structured-output parser
    compiles this schema into a constrained grammar. Date format
    constraints (`format: date`) and nested objects (`additional_fields:
    list[Model]`) push the grammar over the "Schema is too complex" /
    "Grammar compilation timed out" cliff. Strings parse just fine on
    our side — Postgres + Pydantic accept ISO dates implicitly.
    """

    model_config = ConfigDict(extra="ignore")

    # ── Primary id for this specific document ──────────────────────────
    # Whatever matches the document's doc_type (PAN value for PAN doc,
    # GSTIN for GST doc, etc.). Persisted into vendor_document.doc_number.
    doc_number: str | None = Field(
        default=None,
        description="Primary identifier on this document (matches its doc_type).",
    )

    # ── Cross-identifier extraction ────────────────────────────────────
    # A single doc (invoice, GST cert, MOA, registration) often carries
    # SEVERAL identifiers. We pull every one we can find so the vendor
    # master pre-fills as completely as possible from a single upload.
    # Each is the canonical Indian format, upper-cased, no spaces.
    gstin:         str | None = None    # 15-char GST registration
    pan:           str | None = None    # 10-char PAN
    cin:           str | None = None    # 21-char Corporate Identification Number
    fssai:         str | None = None    # 14-digit food-safety license
    iec:           str | None = None    # 10-digit Importer Exporter Code
    udyam:         str | None = None    # UDYAM-XX-00-0000000
    tin:           str | None = None    # Tax Identification Number
    tan:           str | None = None    # Tax Deduction Account Number
    pollution_epr: str | None = None    # PCB consent / EPR registration
    brc_other:     str | None = None    # BRC global standards no.

    # ── Dates (ISO YYYY-MM-DD strings) ─────────────────────────────────
    issued_on:  str | None = None
    valid_from: str | None = None
    valid_to:   str | None = None

    # ── Entity / person ────────────────────────────────────────────────
    business_name:     str | None = None    # legal company name
    holder_name:       str | None = None    # individual (proprietor, signatory)
    issuing_authority: str | None = None    # who issued the cert
    designation:       str | None = None    # contact's role (CEO, Director, …)
    contact_person:    str | None = None    # name of the person to talk to

    # ── Supplier metadata (drives Step 2 Basics dropdowns) ─────────────
    # `business_category` is a human label like "Manufacturer", "Trader",
    # "Restaurant", "Service Provider". The frontend matches it against
    # the CATEGORY_CODE / SUPPLIER_TYPE dropdown options.
    business_category:    str | None = None
    supplier_type:        str | None = None    # Manufacturer / Trader / Service Provider
    nature_of_business:   str | None = None    # FSSAI "Kind of Business" / GST nature line
    core_business:        str | None = None    # one-line description of what they do
    supplier_reg_year:    str | None = None    # year of incorporation / registration

    # ── Address breakdown ──────────────────────────────────────────────
    address:  str | None = None
    city:     str | None = None
    state:    str | None = None
    pin_code: str | None = None

    # ── Contact details (common on invoices) ───────────────────────────
    mobile:  str | None = None
    phone:   str | None = None
    email:   str | None = None
    website: str | None = None

    # ── Banking (Step 4) — populated when the upload is a cancelled ────
    # cheque, passbook page, bank letter, or any doc showing account details.
    bank_name:         str | None = None
    bank_branch:       str | None = None
    bank_account_no:   str | None = None
    bank_account_name: str | None = None    # account holder
    bank_ifsc:         str | None = None
    bank_swift:        str | None = None
    bank_account_type: str | None = None    # Savings / Current / Cash Credit / OD

    # ── Contract (Step 5) — populated when the upload is a contract / MSA / NDA. ──
    # `signed_date`, `effective_from`, `effective_to` overlap with the
    # generic date fields but are surfaced separately so the model can
    # tell us this row belongs in the Contracts step. `value_inr` is a
    # float (drop currency symbols, commas).
    contract_type:    str | None = None     # yearly | one-time | NDA | MSA
    contract_value:   float | None = None   # total contract value in INR
    contract_summary: str | None = None     # <=200 chars

    # ── Type-specific extras ───────────────────────────────────────────
    category: str | None = None              # MSME size (Micro/Small/Medium), etc.

    # ── Free-form bucket ───────────────────────────────────────────────
    additional_notes: str | None = Field(
        default=None,
        description="Anything else useful that doesn't fit the typed fields (trade name, jurisdiction, etc.).",
    )

    # ── Meta ───────────────────────────────────────────────────────────
    extraction_status: ExtractionStatus = "ok"
    extraction_error:  str | None = None


class DocExtractResponse(BaseModel):
    """Dry-run extraction result — file is in S3 but no DB row written."""

    s3_url: str
    doc_type: DocType
    extracted: ExtractedDocFields


class DocumentUploadResponse(BaseModel):
    """One-shot upload + extract + save result."""

    document: DocumentResponse
    extracted: ExtractedDocFields


# ── vendor_contract ──────────────────────────────────────────────────────


class ContractManualCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    contract_type: ContractType | None = None
    signed_date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    s3_urls: str = ""
    scoc_signed: bool = False
    value_inr: float | None = None
    auto_renew: bool = False


class ContractUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    contract_type: ContractType | None = None
    signed_date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    s3_urls: str | None = None
    scoc_signed: bool | None = None
    value_inr: float | None = None
    auto_renew: bool | None = None
    reason: str | None = Field(default=None, alias="_reason")


class ContractResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    contract_id: str
    vendor_id: str
    contract_type: str | None = None
    signed_date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    s3_urls: str = ""
    scoc_signed: bool = False
    value_inr: float | None = None
    auto_renew: bool = False
    created_by: str | None = None
    created_at: datetime | None = None


class ExtractedContractFields(BaseModel):
    # Plain strings + flat schema for the same Anthropic-schema-complexity
    # reason as ExtractedDocFields. Dates are ISO YYYY-MM-DD; downstream
    # consumers accept these as-is.
    model_config = ConfigDict(extra="ignore")
    contract_type: ContractType | None = None
    signed_date: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    value_inr: float | None = None
    counterparty: str | None = None
    summary: str | None = None
    additional_notes: str | None = None
    extraction_status: ExtractionStatus = "ok"
    extraction_error: str | None = None


class ContractExtractResponse(BaseModel):
    s3_url: str
    extracted: ExtractedContractFields


class ContractUploadResponse(BaseModel):
    contract: ContractResponse
    extracted: ExtractedContractFields


# ── nested detail GET response ───────────────────────────────────────────


class VendorDetailResponse(BaseModel):
    vendor: VendorResponse
    banking: list[BankingResponse] = Field(default_factory=list)
    documents: list[DocumentResponse] = Field(default_factory=list)
    contracts: list[ContractResponse] = Field(default_factory=list)


# ── combined create (vendor + documents in one multipart request) ────────


class DocumentUploadFailure(BaseModel):
    """Per-file failure record when /with-documents partially succeeds."""

    doc_type: str
    filename: str | None = None
    error: str


class VendorWithDocumentsResponse(BaseModel):
    """Result of POST /api/v1/vendors/with-documents.

    `vendor` is always present once the row is committed. `documents`
    holds the rows + extracted fields for files that succeeded;
    `failures` lists per-file errors so the caller can retry only the
    ones that didn't make it.
    """

    vendor: VendorResponse
    documents: list[DocumentUploadResponse] = Field(default_factory=list)
    failures: list[DocumentUploadFailure] = Field(default_factory=list)


# ── bulk extraction (phase-1) ────────────────────────────────────────────


class ExtractBulkResponse(BaseModel):
    """Response from POST /vendors/extract-bulk.

    Free-form: the per-tab suggestions live in `vendor_fields` /
    `banking_fields` / `documents` / `contracts`. The UI hydrates the
    form from these and POSTs the final shape back to /submit-staged.
    """

    model_config = ConfigDict(extra="allow")
    staging_id: str
    vendor_fields: dict[str, Any] = Field(default_factory=dict)
    banking_fields: list[dict[str, Any]] = Field(default_factory=list)
    documents: list[dict[str, Any]] = Field(default_factory=list)
    contracts: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)


# ── staged submit (phase-2) ──────────────────────────────────────────────


class StagedBankingItem(BaseModel):
    """One banking row in /submit-staged. Mirrors BankingCreateRequest but
    every field is optional — the operator may have edited any of them."""

    model_config = ConfigDict(extra="ignore")
    bank_name: str
    account_no: str
    account_name: str
    branch: str | None = None
    ifsc: str | None = None
    swift: str | None = None
    account_type_id: str | None = None
    is_primary: bool = False
    is_active: bool = True
    valid_from: date | None = None
    valid_to: date | None = None


class StagedDocumentItem(BaseModel):
    """One vendor_document row pre-uploaded in phase-1."""

    model_config = ConfigDict(extra="ignore")
    doc_type: DocType
    s3_url: str | None = None  # alias for s3_urls CSV with one entry
    s3_urls: str | None = None
    doc_number: str | None = None
    issued_on: date | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    status_id: str | None = None


class StagedContractItem(BaseModel):
    """One vendor_contract row."""

    model_config = ConfigDict(extra="ignore")
    contract_type: ContractType | None = None
    s3_url: str | None = None
    s3_urls: str | None = None
    signed_date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    value_inr: float | None = None
    scoc_signed: bool = False
    auto_renew: bool = False


class SubmitStagedRequest(BaseModel):
    """POST /vendors/submit-staged body."""

    model_config = ConfigDict(extra="ignore")
    staging_id: str | None = None
    vendor: VendorCreateRequest
    banking: list[StagedBankingItem] = Field(default_factory=list)
    documents: list[StagedDocumentItem] = Field(default_factory=list)
    contracts: list[StagedContractItem] = Field(default_factory=list)


class SubmitStagedResponse(BaseModel):
    """Result of /submit-staged."""

    model_config = ConfigDict(extra="allow")
    vendor: VendorResponse
    banking: list[BankingResponse] = Field(default_factory=list)
    documents: list[DocumentResponse] = Field(default_factory=list)
    contracts: list[ContractResponse] = Field(default_factory=list)


# ── history ──────────────────────────────────────────────────────────────


HistoryOperation = Literal[
    "create", "update", "approve", "delete", "restore", "revert",
    "set_primary", "append_file",
]


class HistoryEntry(BaseModel):
    """One row from any of the *_history tables. JSON columns are decoded
    server-side so the wire shape matches the UI's expectation."""

    model_config = ConfigDict(extra="allow")
    history_id: int
    operation: HistoryOperation
    changed_by: str | None = None
    changed_at: datetime
    previous_state: dict[str, Any] | None = None
    new_state: dict[str, Any]
    diff: dict[str, dict[str, Any]] = Field(default_factory=dict)
    source: str = "manual"
    reason: str | None = None


class HistoryListResponse(BaseModel):
    entries: list[HistoryEntry]
    total: int
    page: int
    page_size: int


class HistoryRevertRequest(BaseModel):
    """POST /vendors/{id}/history/{hid}/revert body."""

    model_config = ConfigDict(extra="ignore")
    reason: str | None = None
