"""Response of POST /api/v1/so/upload-sales-invoices (SO Creation's second
upload: the Tally Sales Register). saved is True only for ?save=true, after
the invoices were written to sales_invoice / sales_invoice_line."""

from datetime import date

from pydantic import BaseModel


class SalesInvoiceLineOut(BaseModel):
    line_no: int
    particulars: str
    quantity: float | None = None
    alt_units: float | None = None          # Tally Alt. Units (kg)
    rate: float | None = None
    value: float | None = None
    matched_article: str | None = None      # master-list article it matched
    match_score: float = 0.0                # 0-1
    item_type: str | None = None
    kg: float | None = None
    kg_source: str | None = None            # "alt_units" | "master_uom" | None
    so_line_id: int | None = None           # SO line this item was dispatched against
    so_line_article: str | None = None
    so_line_score: float = 0.0              # 0-1


class SalesInvoiceOut(BaseModel):
    voucher_no: str
    voucher_type: str | None = None
    date: date
    party: str | None = None
    buyer: str | None = None
    buyer_address: str | None = None
    consignee: str | None = None
    consignee_address: str | None = None
    reference: str | None = None            # Voucher Ref. No. as written
    quoted_so_number: str | None = None     # reference written like CF-SO/26-27/1404
    so_number: str | None = None            # the SO in the ERP the reference names
    so_found: bool = False
    narration: str | None = None
    dispatch_from: str | None = None        # site read off the narration
    cartons: int | None = None
    replaces_invoice: str | None = None     # "Against Old Invoice No.: ..."
    cancelled: bool = False
    quantity: float | None = None
    alt_units: float | None = None
    value: float | None = None
    gross_total: float | None = None
    ledgers: dict[str, float] = {}          # tax / charge columns, by header
    lines: list[SalesInvoiceLineOut] = []
    issues: list[str] = []


class SalesInvoiceSummary(BaseModel):
    company: str | None = None
    period: str | None = None
    sheet: str
    invoices: int                           # not cancelled
    cancelled: int
    by_voucher_type: dict[str, int]
    by_dispatch_site: dict[str, int]
    parties: int
    lines: int
    total_value: float
    total_gross: float
    total_kg: float
    lines_without_kg: int
    date_from: date | None = None
    date_to: date | None = None
    linked_to_so: int                       # invoices whose reference is an SO in the ERP
    sos_invoiced: int                       # distinct SOs those invoices belong to
    so_not_found: int                       # quote a CF-SO number the ERP doesn't have
    reference_not_an_so: int                # quote something else (customer PO, ...) not an SO here
    without_reference: int
    lines_on_so_line: int                   # items tied to a line of their SO
    lines_not_on_so_line: int               # items on a linked invoice with no such SO line
    unmatched_articles: int
    low_confidence_articles: int
    invoices_with_issues: int
    duplicate_vouchers: int
    ledger_columns: list[str]


class UnmatchedArticle(BaseModel):
    particulars: str
    lines: int


class LowConfidenceArticle(BaseModel):
    particulars: str
    lines: int
    matched_article: str
    match_score: float


class SalesInvoiceSaveResult(BaseModel):
    inserted: int                           # invoice numbers new to the ERP
    updated: int                            # already saved, refreshed from this file
    lines: int


class SalesInvoiceUploadResponse(BaseModel):
    saved: bool = False
    save_result: SalesInvoiceSaveResult | None = None
    summary: SalesInvoiceSummary
    issues: list[str]
    unmatched_articles: list[UnmatchedArticle]
    low_confidence_articles: list[LowConfidenceArticle]
    so_numbers_not_found: list[str]
    invoices: list[SalesInvoiceOut]
