"""Pydantic models for /api/v1/receipt endpoints (COA + invoice)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── COA upload ───────────────────────────────────────────────────────────


class CoaUploadResponse(BaseModel):
    coa_id: str
    s3_key: str
    file_name: str
    file_size_bytes: int
    mime_type: str
    scan_status: str
    coa_status: str
    uploaded_at: str


# ── COA list ─────────────────────────────────────────────────────────────


class CoaListItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    coa_id: str
    transaction_no: str | None = None
    line_number: int | None = None
    po_number: str | None = None
    dock_intimation_id: int | None = None
    qc_intimation_id: int | None = None
    sku_id: int | None = None
    sku_name: str | None = None
    supplier_id: int | None = None
    supplier_name: str | None = None
    lot_number: str | None = None
    vendor_coa_date: str | None = None
    file_name: str | None = None
    file_size_bytes: int | None = None
    mime_type: str | None = None
    scan_status: str = "pending"
    coa_status: str = "active"
    uploaded_by_name: str | None = None
    uploaded_at: str | None = None
    download_url: str | None = None


class CoaListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[CoaListItem]


# ── COA detail ───────────────────────────────────────────────────────────


class ReplacementChainEntry(BaseModel):
    old_coa_id: str
    new_coa_id: str
    replaced_at: str | None = None
    replaced_reason: str | None = None


class CoaDetailResponse(CoaListItem):
    parsed_params_json: dict[str, Any] | None = None
    replaces_coa_id: str | None = None
    replacement_history: list[ReplacementChainEntry] = Field(default_factory=list)
    remarks: str | None = None


# ── COA replace (PUT) ────────────────────────────────────────────────────


class CoaReplaceResponse(BaseModel):
    old_coa_id: str
    new_coa_id: str
    replaced_at: str


# ── Invoice upload ───────────────────────────────────────────────────────


class InvoiceUploadResponse(BaseModel):
    document_id: int
    document_type: str
    s3_key: str
    file_name: str
    file_size_bytes: int
    mime_type: str
    scan_status: str
    uploaded_at: str
