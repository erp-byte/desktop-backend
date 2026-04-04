"""Aggregate response schemas for SO endpoints."""

from pydantic import BaseModel

from app.modules.so.schemas.header import SODetail


class UploadSummary(BaseModel):
    total_sos: int
    total_lines: int
    matched_lines: int
    unmatched_lines: int
    gst_ok: int
    gst_mismatch: int
    gst_warning: int
    so_ok: int = 0
    so_mismatch: int = 0
    so_warning: int = 0


class SOUploadResponse(BaseModel):
    summary: UploadSummary
    sales_orders: list[SODetail]


class FilterOptions(BaseModel):
    # SO header level
    companies: list[str]
    voucher_types: list[str]
    customer_names: list[str]
    common_customer_names: list[str]
    # SO line level
    item_categories: list[str]
    sub_categories: list[str]
    uoms: list[str]
    grp_codes: list[str]
    rate_types: list[str]
    item_types: list[str]
    sales_groups: list[str]
    match_sources: list[str]
    statuses: list[str]


class SOViewResponse(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    summary: UploadSummary
    filter_options: FilterOptions
    sales_orders: list[SODetail]


class SOExportResponse(BaseModel):
    total: int
    sales_orders: list[SODetail]
