"""Pydantic request/response models for /api/v1/customer-returns.

Field names mirror the source RTV contract verbatim (FE compatibility);
numeric fields are kept as strings in responses to match the production API.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Company = Literal["CFPL", "CDPL"]


# ── requests ────────────────────────────────────────────────────────────
class CRHeaderCreate(BaseModel):
    factory_unit: str
    customer: str
    invoice_number: Optional[str] = None
    challan_no: Optional[str] = None
    dn_no: Optional[str] = None
    conversion: Optional[str] = "0"
    sales_poc: Optional[str] = None
    sales_poc_email: Optional[str] = None
    business_head: Optional[str] = None
    remark: Optional[str] = None
    vehicle_number: Optional[str] = None
    transporter_name: Optional[str] = None
    driver_name: Optional[str] = None
    inward_manager: Optional[str] = None


class CRLineCreate(BaseModel):
    material_type: str
    item_category: str
    sub_category: str
    item_description: str
    sale_group: Optional[str] = None      # auto-filled from all_sku on item pick (legacy parity)
    uom: str
    qty: str = "0"
    rate: str = "0"
    value: str = "0"
    conversion: Optional[str] = None      # line-level conversion (legacy sends = uom)
    net_weight: Optional[str] = "0"
    carton_weight: Optional[str] = "0"
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None

    @field_validator("material_type", "uom")
    @classmethod
    def uppercase_codes(cls, v: str) -> str:
        return v.upper() if v else v


class CRCreate(BaseModel):
    company: Company
    header: CRHeaderCreate
    lines: List[CRLineCreate] = Field(..., min_length=1)


class CRHeaderUpdate(BaseModel):
    factory_unit: Optional[str] = None
    customer: Optional[str] = None
    invoice_number: Optional[str] = None
    challan_no: Optional[str] = None
    dn_no: Optional[str] = None
    conversion: Optional[str] = None
    sales_poc: Optional[str] = None
    sales_poc_email: Optional[str] = None
    business_head: Optional[str] = None
    remark: Optional[str] = None
    status: Optional[str] = None
    vehicle_number: Optional[str] = None
    transporter_name: Optional[str] = None
    driver_name: Optional[str] = None
    inward_manager: Optional[str] = None


class CRLinesUpdateRequest(BaseModel):
    lines: List[CRLineCreate] = Field(..., min_length=1)


# ── responses ───────────────────────────────────────────────────────────
class CRLineResponse(BaseModel):
    rtv_id: str
    item_description: str
    material_type: str
    item_category: str
    sub_category: str
    sale_group: Optional[str] = None
    uom: str
    qty: str
    rate: str
    value: str
    conversion: Optional[str] = None
    net_weight: str
    carton_weight: str
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CRBoxResponse(BaseModel):
    rtv_id: str
    article_description: str
    box_number: int
    box_id: Optional[str] = None
    uom: Optional[str] = None
    conversion: Optional[str] = None
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None
    net_weight: str
    gross_weight: str
    count: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CRHeaderResponse(BaseModel):
    rtv_id: str
    rtv_date: Optional[datetime] = None
    factory_unit: str
    customer: str
    invoice_number: Optional[str] = None
    challan_no: Optional[str] = None
    dn_no: Optional[str] = None
    conversion: Optional[str] = None
    sales_poc: Optional[str] = None
    sales_poc_email: Optional[str] = None
    business_head: Optional[str] = None
    remark: Optional[str] = None
    vehicle_number: Optional[str] = None
    transporter_name: Optional[str] = None
    driver_name: Optional[str] = None
    inward_manager: Optional[str] = None
    status: str
    created_by: Optional[str] = None
    created_ts: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CRWithDetails(CRHeaderResponse):
    lines: List[CRLineResponse] = []
    boxes: List[CRBoxResponse] = []


class CRListItem(CRHeaderResponse):
    items_count: int = 0
    boxes_count: int = 0
    total_qty: int = 0
    total_net_weight: float = 0


class CRListResponse(BaseModel):
    records: List[CRListItem] = []
    total: int = 0
    page: int = 1
    per_page: int = 10
    total_pages: int = 0


class CRDeleteResponse(BaseModel):
    success: bool
    message: str
    rtv_id: Optional[str] = None
    lines_count: int = 0
    boxes_count: int = 0


class CRLinesUpdateResponse(BaseModel):
    status: str
    rtv_id: str
    lines_count: int


# ── box models (Phase 2) ────────────────────────────────────────────────
Decimal18_3 = Annotated[Decimal, Field(max_digits=18, decimal_places=3)]


class CRBoxUpsertRequest(BaseModel):
    article_description: str
    box_number: int = Field(..., ge=1)
    uom: Optional[str] = None
    conversion: Optional[str] = None
    net_weight: Optional[Decimal18_3] = None
    gross_weight: Optional[Decimal18_3] = None
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None
    count: Optional[int] = None


class CRBoxUpsertResponse(BaseModel):
    status: str
    box_id: str
    rtv_id: str
    article_description: str
    box_number: int


class CRBulkBoxItem(BaseModel):
    article_description: str
    box_number: int = Field(..., ge=1)
    uom: Optional[str] = None
    conversion: Optional[str] = None
    lot_number: Optional[str] = None
    item_mark: Optional[str] = None
    spl_remarks: Optional[str] = None
    vakkal: Optional[str] = None
    net_weight: Optional[Decimal18_3] = None
    gross_weight: Optional[Decimal18_3] = None
    count: Optional[int] = None


class CRBulkBoxUpdateRequest(BaseModel):
    boxes: List[CRBulkBoxItem] = Field(default_factory=list)


class CRBulkBoxUpdateResponse(BaseModel):
    status: str
    rtv_id: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0


class CRBoxEditLogEntry(BaseModel):
    field_name: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None


class CRBoxEditLogRequest(BaseModel):
    email_id: str  # accepted for FE compat; the SERVICE ignores it and uses user.email (JWT)
    box_id: str
    rtv_id: str
    changes: List[CRBoxEditLogEntry]


class CRBoxEditLogResponse(BaseModel):
    status: str
    entries: int
