"""Pydantic models for the documented /api/v1/po endpoints.

Shapes mirror the spec exactly. Per-PO bodies use `extra="allow"` so the
preview round-trip can carry arbitrary fields back through commit unchanged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ── /preview ─────────────────────────────────────────────────────────────


class MatchedItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    sku_id: str | None = None
    sku_name: str | None = None
    item_category: str | None = None
    sub_category: str | None = None
    item_type: str | None = None
    sales_group: str | None = None
    gst_rate: float | None = None


class PreviewLine(BaseModel):
    model_config = ConfigDict(extra="allow")
    line_number: int
    sku_name: str | None = None
    uom: str | None = None
    pack_count: int | None = None
    po_weight: float | None = None
    rate: float | None = None
    amount: float | None = None
    particulars: str | None = None
    item_category: str | None = None
    sub_category: str | None = None
    item_type: str | None = None
    sales_group: str | None = None
    gst_rate: float | None = None
    match_score: float | None = None
    match_source: str | None = None       # exact | fuzzy | none
    matched_item: MatchedItem | None = None


class PreviewHeader(BaseModel):
    model_config = ConfigDict(extra="allow")
    po_number: str | None = None
    po_date: str | None = None
    voucher_type: str | None = None
    order_reference_no: str | None = None
    narration: str | None = None
    vendor_supplier_name: str | None = None
    supplier_id: str | None = None
    gross_total: float | None = None
    total_amount: float | None = None
    sgst_amount: float | None = None
    cgst_amount: float | None = None
    igst_amount: float | None = None
    round_off: float | None = None
    freight_transport_local: float | None = None
    apmc_tax: float | None = None
    packing_charges: float | None = None
    freight_transport_charges: float | None = None
    loading_unloading_charges: float | None = None
    other_charges_non_gst: float | None = None


class PreviewPo(BaseModel):
    model_config = ConfigDict(extra="allow")
    is_duplicate: bool
    duplicate_key: str
    transaction_no: str
    header: PreviewHeader
    incoming: dict[str, Any]
    existing: PreviewHeader | None = None
    diff: dict[str, Any] | None = None
    lines: list[PreviewLine]
    warnings: list[str] = Field(default_factory=list)


class PreviewSummary(BaseModel):
    total_pos: int
    new: int
    duplicates: int
    matched_lines: int
    unmatched_lines: int


class PreviewResponse(BaseModel):
    summary: PreviewSummary
    pos: list[PreviewPo]


# ── /commit ──────────────────────────────────────────────────────────────


class CommitPo(BaseModel):
    """Per-PO commit body. extra='allow' so any preview item validates."""
    model_config = ConfigDict(extra="allow")
    duplicate_key: str | None = None
    transaction_no: str | None = None
    header: dict[str, Any]
    lines: list[dict[str, Any]] = Field(default_factory=list)
    incoming: dict[str, Any] | None = None


CommitMode = Literal["create_only", "update_only", "upsert"]


class CommitRequest(BaseModel):
    entity: str = Field(pattern="^(cfpl|cdpl)$")
    mode: CommitMode = "upsert"
    pos: list[CommitPo] = Field(min_length=1)


class CommitError(BaseModel):
    po_number: str | None = None
    transaction_no: str | None = None
    duplicate_key: str | None = None
    reason: str


class CommitResponse(BaseModel):
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    skipped_duplicates: list[str] = Field(default_factory=list)
    skipped_missing: list[str] = Field(default_factory=list)
    errors: list[CommitError] = Field(default_factory=list)


# ── GET /api/v1/po (list) ────────────────────────────────────────────────


class PoListItem(BaseModel):
    transaction_no: str
    entity: str
    po_number: str | None = None
    po_date: str | None = None
    voucher_type: str | None = None
    order_reference_no: str | None = None
    narration: str | None = None
    vendor_supplier_name: str | None = None
    supplier_id: str | None = None
    gross_total: float | None = None
    total_amount: float | None = None
    sgst_amount: float | None = None
    cgst_amount: float | None = None
    igst_amount: float | None = None
    round_off: float | None = None
    freight_transport_local: float | None = None
    apmc_tax: float | None = None
    packing_charges: float | None = None
    freight_transport_charges: float | None = None
    loading_unloading_charges: float | None = None
    other_charges_non_gst: float | None = None
    deleted_at: str | None = None


class PoListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool
    items: list[PoListItem]


# ── GET /api/v1/po/{transaction_no} (detail) ─────────────────────────────


class PoDetailResponse(BaseModel):
    header: PoListItem


# ── GET /api/v1/po/{transaction_no}/lines ────────────────────────────────


class PoLineOut(BaseModel):
    transaction_no: str
    line_number: int
    sku_name: str | None = None
    uom: str | None = None
    pack_count: int | None = None
    po_weight: float | None = None
    rate: float | None = None
    amount: float | None = None
    particulars: str | None = None
    item_category: str | None = None
    sub_category: str | None = None
    item_type: str | None = None
    sales_group: str | None = None
    gst_rate: float | None = None
    match_score: float | None = None
    match_source: str | None = None


class PoLinesResponse(BaseModel):
    header: PoListItem
    total_lines: int
    lines: list[PoLineOut]


# ── DELETE /api/v1/po/{transaction_no} ───────────────────────────────────


class DependentRecords(BaseModel):
    dock_arrivals: int = 0
    grns: int = 0
    po_boxes: int = 0


class PoDeleteResponse(BaseModel):
    transaction_no: str
    entity: str
    po_number: str | None
    deleted_at: str
    deleted_by: str
    delete_reason: str
    dependent_records: DependentRecords
