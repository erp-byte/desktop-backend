"""Aggregate response schemas for purchase endpoints."""

from pydantic import BaseModel

from app.core.types import Decimal3
from app.modules.purchase.schemas.header import POHeaderOut


class POSummary(BaseModel):
    total_transactions: int
    total_lines: int
    total_boxes: int
    total_amount: Decimal3 = None
    total_tax: Decimal3 = None
    total_net_weight: Decimal3 = None


class POFilterOptions(BaseModel):
    entities: list[str]
    voucher_types: list[str]
    vendors: list[str]
    customers: list[str]
    warehouses: list[str]
    statuses: list[str]
    item_categories: list[str]
    sub_categories: list[str]
    item_types: list[str]


class POViewResponse(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    summary: POSummary
    filter_options: POFilterOptions
    transactions: list[POHeaderOut]


class POExportResponse(BaseModel):
    total: int
    transactions: list[POHeaderOut]


class POUploadResponse(BaseModel):
    summary: POSummary
    transactions: list[POHeaderOut]
