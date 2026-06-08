"""Pydantic response schemas for the Transfer module.

Ported from `transfer_backend_reference/services/ims_service/interunit_models.py`
(response classes only — Phase 1 is read/pending/delete). Field names and types
mirror the production API so the web_replica client types match 1:1. Numeric
fields the production API serialised as strings stay strings here; the service
layer is responsible for stringifying asyncpg Decimals to match.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Common ──────────────────────────────────────────────────────────────────
class DeleteResponse(BaseModel):
    success: bool
    message: str


# ── Receive (Transfer-IN) input schemas ──────────────────────────────────────
# Ported from interunit_models.py (PendingTransferInCreate / PendingBoxAcknowledge
# / FinalizeTransferIn). Used by the P8b pending-receive lifecycle endpoints.
class PendingTransferInCreate(BaseModel):
    transfer_out_id: int
    grn_number: str
    receiving_warehouse: str
    received_by: str
    box_condition: Optional[str] = "Good"
    condition_remarks: Optional[str] = None


class PendingBoxAcknowledge(BaseModel):
    box_id: str
    transfer_out_box_id: Optional[int] = None
    article: Optional[str] = None
    batch_number: Optional[str] = None
    lot_number: Optional[str] = None
    transaction_no: Optional[str] = None
    net_weight: Optional[float] = None
    gross_weight: Optional[float] = None
    is_matched: bool = True
    issue: Optional[dict] = None
    line_index: Optional[int] = None
    scan_source: Optional[str] = None
    scanned_by: Optional[str] = None


class FinalizeTransferIn(BaseModel):
    box_condition: Optional[str] = "Good"
    condition_remarks: Optional[str] = None
    cold_storage_items: Optional[List[dict]] = None


# ── Request create input (doc 05) ─────────────────────────────────────────────
# Ported from interunit_models.py FormDataBase / ArticleDataCreate / RequestCreate.
class FormDataCreate(BaseModel):
    request_date: str = Field(..., description="DD-MM-YYYY")
    from_warehouse: str = Field(..., description="Source warehouse site code")
    to_warehouse: str = Field(..., description="Destination warehouse site code")
    reason_description: str = Field(..., description="Reason for the transfer")

    @field_validator("reason_description")
    @classmethod
    def uppercase_reason(cls, v: str) -> str:
        return v.upper() if v else v

    @model_validator(mode="after")
    def warehouses_must_differ(self):
        if self.from_warehouse and self.to_warehouse and self.from_warehouse == self.to_warehouse:
            raise ValueError("From warehouse and to warehouse must be different")
        return self


class ArticleDataCreate(BaseModel):
    material_type: str
    item_category: str
    sub_category: str
    item_description: str
    quantity: Optional[str] = "0"
    uom: Optional[str] = ""
    pack_size: Optional[str] = "0.00"
    unit_pack_size: Optional[str] = None
    net_weight: Optional[str] = "0"
    total_weight: Optional[str] = None
    lot_number: Optional[str] = None

    @field_validator("material_type")
    @classmethod
    def uppercase_material(cls, v: str) -> str:
        return v.upper() if v else v

    @field_validator("uom")
    @classmethod
    def uppercase_uom(cls, v: Optional[str]) -> str:
        return v.upper() if v else (v or "")

    @field_validator("item_category", "sub_category", "item_description")
    @classmethod
    def uppercase_text(cls, v: str) -> str:
        return v.upper() if v else v


class ComputedFields(BaseModel):
    request_no: Optional[str] = None


class RequestCreate(BaseModel):
    form_data: FormDataCreate
    article_data: List[ArticleDataCreate] = Field(..., min_length=1)
    computed_fields: Optional[ComputedFields] = None
    validation_rules: Optional[dict] = None


# ── Dropdowns (doc 05) ────────────────────────────────────────────────────────
class WarehouseSiteResponse(BaseModel):
    id: int
    site_code: str
    site_name: Optional[str] = None
    is_active: Optional[bool] = None


class CategorialSearchItem(BaseModel):
    id: int
    item_description: str
    material_type: Optional[str] = None
    group: Optional[str] = None
    sub_group: Optional[str] = None
    uom: Optional[float] = None


class CategorialSearchResponse(BaseModel):
    items: List[CategorialSearchItem] = []
    meta: dict = {}


class CategorialDropdownResponse(BaseModel):
    selected: dict = {}
    options: dict = {}
    meta: dict = {}


# ── Requests ────────────────────────────────────────────────────────────────
class RequestLineResponse(BaseModel):
    id: int
    request_id: int
    material_type: str
    item_category: str
    sub_category: str
    item_description: str
    quantity: str
    uom: str
    pack_size: str
    unit_pack_size: Optional[str] = None
    net_weight: str
    lot_number: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RequestResponse(BaseModel):
    id: int
    request_no: str
    request_date: str
    from_warehouse: str
    to_warehouse: str
    reason_description: str
    status: str
    reject_reason: Optional[str] = None
    created_by: Optional[str] = None
    created_ts: Optional[datetime] = None
    rejected_ts: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RequestWithLines(RequestResponse):
    lines: List[RequestLineResponse] = []


class RequestListResponse(BaseModel):
    records: List[RequestWithLines] = []
    total: int = 0
    page: int = 1
    per_page: int = 10
    total_pages: int = 0


# ── Transfers (OUT) ──────────────────────────────────────────────────────────
class TransferHeaderResponse(BaseModel):
    id: int
    challan_no: str
    stock_trf_date: str
    from_warehouse: str
    to_warehouse: str
    vehicle_no: str
    driver_name: Optional[str] = None
    approved_by: Optional[str] = None
    remark: Optional[str] = None
    reason_code: Optional[str] = None
    status: str
    request_id: Optional[int] = None
    request_no: Optional[str] = None
    created_by: Optional[str] = None
    created_ts: Optional[datetime] = None
    approved_ts: Optional[datetime] = None
    has_variance: bool = False
    from_cold_unit: Optional[str] = None


class TransferLineResponse(BaseModel):
    id: int
    header_id: int
    material_type: str
    item_category: str
    sub_category: str
    item_description: str
    quantity: str
    uom: str
    pack_size: str
    unit_pack_size: Optional[str] = None
    net_weight: str
    total_weight: str
    batch_number: Optional[str] = None
    lot_number: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BoxResponse(BaseModel):
    id: int
    header_id: int
    transfer_line_id: Optional[int] = None
    box_number: int
    box_id: Optional[str] = None
    article: str
    lot_number: Optional[str] = None
    batch_number: Optional[str] = None
    transaction_no: Optional[str] = None
    net_weight: str
    gross_weight: str
    storage_location: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Per-box / per-lot cold-source attribution used by the hover card.
    source_storage: Optional[str] = None
    source_unit: Optional[str] = None
    lot_origin_unit: Optional[str] = None


class GrnRecord(BaseModel):
    id: int
    grn_number: str = ""
    status: str = ""
    received_by: str = ""
    received_at: Optional[str] = None
    received_boxes: int = 0


class TransferWithLines(TransferHeaderResponse):
    lines: List[TransferLineResponse] = []
    boxes: List[BoxResponse] = []
    grn_records: List[GrnRecord] = []


class TransferListItem(TransferHeaderResponse):
    items_count: int = 0
    boxes_count: int = 0
    total_qty: int = 0
    pending_items: int = 0
    lot_numbers_text: Optional[str] = None


class TransferListResponse(BaseModel):
    records: List[TransferListItem] = []
    total: int = 0
    page: int = 1
    per_page: int = 10
    total_pages: int = 0


class TransferDeleteResponse(BaseModel):
    success: bool
    message: str
    transfer_id: Optional[int] = None
    challan_no: Optional[str] = None


# ── Transfers IN (GRN) ────────────────────────────────────────────────────────
class TransferInBoxResponse(BaseModel):
    id: int
    header_id: int
    box_id: str
    transfer_out_box_id: Optional[int] = None
    article: Optional[str] = None
    batch_number: Optional[str] = None
    lot_number: Optional[str] = None
    transaction_no: Optional[str] = None
    net_weight: Optional[float] = None
    gross_weight: Optional[float] = None
    scanned_at: Optional[datetime] = None
    is_matched: bool = True
    issue: Optional[dict] = None
    line_index: Optional[int] = None


class TransferInHeaderResponse(BaseModel):
    id: int
    transfer_out_id: int
    transfer_out_no: str
    grn_number: str
    grn_date: Optional[datetime] = None
    receiving_warehouse: str
    from_warehouse: Optional[str] = None
    received_by: str
    received_at: Optional[datetime] = None
    box_condition: Optional[str] = None
    condition_remarks: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TransferInDetail(TransferInHeaderResponse):
    boxes: List[TransferInBoxResponse] = []
    total_boxes_scanned: int = 0


class TransferInListItem(TransferInHeaderResponse):
    total_boxes_scanned: int = 0


class TransferInListResponse(BaseModel):
    records: List[TransferInListItem] = []
    total: int = 0
    page: int = 1
    per_page: int = 10
    total_pages: int = 0


# ── Pending stock (in-transit) ────────────────────────────────────────────────
# The production endpoint returns a free-form dict (no response_model). We keep
# the same shape and type the web_replica client manually; documented here for
# reference. Returned by GET /pending-stock.
class PendingTransferRecord(BaseModel):
    transfer_out_id: int
    transfer_out_challan_no: str
    dispatched_at: Optional[datetime] = None
    from_site: Optional[str] = None
    to_site: Optional[str] = None
    from_company: Optional[str] = None
    to_company: Optional[str] = None
    from_storage_type: Optional[str] = None
    to_storage_type: Optional[str] = None
    total_boxes: int = 0
    total_cartons: int = 0
    total_kg: float = 0.0
    dispatched_by: Optional[str] = None
    status: Optional[str] = None
    header_status: Optional[str] = None
    unallocated_boxes: Optional[int] = None
    updated_ts: Optional[datetime] = None


# ── Inner cold (dashboard tab) ────────────────────────────────────────────────
# The production endpoint groups inner_cold_transfer rows by challan_no, each
# challan carrying its relabel lines (lotFrom → lotTo). Mirrors the reference
# `/cold-storage/inner-transfer/list` shape so the dashboard hover card matches.
class InnerColdLine(BaseModel):
    item_description: Optional[str] = None
    item_category: Optional[str] = None
    quantity: Optional[int] = None
    old_lot_number: Optional[str] = None
    new_lot_number: Optional[str] = None
    net_weight_kg: float = 0.0
    new_storage_location: Optional[str] = None


class InnerColdChallan(BaseModel):
    challan_no: Optional[str] = None
    transfer_date: Optional[str] = None
    from_warehouse: Optional[str] = None
    reason_code: Optional[str] = None
    remark: Optional[str] = None
    status: str = "COMPLETED"
    line_count: int = 0
    total_boxes: Optional[int] = None
    created_at: Optional[str] = None
    lines: List[InnerColdLine] = []


class InnerColdListResponse(BaseModel):
    records: List[InnerColdChallan] = []
    total: int = 0
    page: int = 1
    per_page: int = 10
    total_pages: int = 0


# Loose alias for endpoints that return the production's free-form dicts.
JsonDict = dict[str, Any]
