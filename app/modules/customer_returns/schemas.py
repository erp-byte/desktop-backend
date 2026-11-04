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
    # Its boxes, saved with it in one transaction (none printed yet: printing a box
    # creates the return first). None: no boxes.
    boxes: Optional[List["CRBulkBoxItem"]] = None


class CRHeaderUpdate(BaseModel):
    """Header edit (PUT /{company}/{cr_id}). Deliberately no ``status``: a return is
    decided only through approval_service.apply_cr_action (in-app POST /approve, the
    mailed link, the WhatsApp tap), which checks who decides and when and records
    approved_by / approved_at. An incoming ``status`` is dropped like any unknown key
    (pydantic's default extra='ignore'), so a body with nothing else is the 400
    empty_update."""
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
    # Fingerprint of the box set as read (query_service.box_version); a page sends it
    # back as base_version with each box print and save.
    box_version: Optional[str] = None
    # Fingerprint of the figures an approver decides on (query_service.figures_version);
    # the review page sends it back with a decision.
    figures_version: Optional[str] = None
    # The latest approval request recorded (cr_approval_log): {sent_at, sent_by,
    # figures_current: whether it showed the figures the return has now}. None: none
    # recorded (never sent from the ERP, or sent before requests were recorded).
    approval_request: Optional[dict] = None


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
    printed_boxes: int = 0


class CRLinesUpdateResponse(BaseModel):
    status: str
    rtv_id: str
    lines_count: int
    boxes_removed: int = 0                # unprinted boxes of the articles taken off the return
    box_version: Optional[str] = None     # the box-set fingerprint after it, for the next box save


# ── box models (Phase 2) ────────────────────────────────────────────────
Decimal18_3 = Annotated[Decimal, Field(max_digits=18, decimal_places=3)]


class CRBoxUpsertRequest(BaseModel):
    article_description: str
    box_number: int = Field(..., ge=1)
    # The label the page believes this box has (None: unprinted), and the box-set
    # fingerprint it loaded. A mismatch is refused (409) instead of overwriting.
    box_id: Optional[str] = None
    base_version: Optional[str] = None
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
    box_version: Optional[str] = None


class CRBulkBoxItem(BaseModel):
    article_description: str
    box_number: int = Field(..., ge=1)
    box_id: Optional[str] = None  # the label the page believes it has; None = unprinted
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
    base_version: Optional[str] = None  # the box-set fingerprint the page loaded


class CRBulkBoxUpdateResponse(BaseModel):
    status: str
    rtv_id: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    kept_printed: int = 0  # printed boxes missing from the save, kept: a save never deletes a label
    box_version: Optional[str] = None
    # printed boxes whose label shows a value this save changed: to be reprinted
    reprint: List[dict] = Field(default_factory=list)


class CRSaveRequest(BaseModel):
    """PUT /{company}/{cr_id}/save: the whole form in one transaction — the header,
    the lines and (unless None) the boxes, all saved or none."""
    header: CRHeaderUpdate
    lines: List[CRLineCreate] = Field(..., min_length=1)
    boxes: Optional[List[CRBulkBoxItem]] = None   # None: leave the boxes as they are
    base_version: Optional[str] = None            # the box-set fingerprint the page loaded
    send: bool = False                            # then send it for approval (Save & send)


class CRSaveResponse(BaseModel):
    status: str
    rtv_id: str
    lines_count: int
    boxes_removed: int = 0       # unprinted boxes of the articles taken off the return
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    kept_printed: int = 0
    box_version: Optional[str] = None
    reprint: List[dict] = Field(default_factory=list)
    figures_version: Optional[str] = None
    figures_changed: bool = False   # the figures an approver decides on changed
    # The approval request sent after the save (send-for-approval's result), and why:
    # 'requested' (Save & send) or 'figures_changed' (sent again on its own, since it
    # had been sent before). None: not sent.
    approval: Optional[dict] = None
    sent_because: Optional[str] = None


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


CRCreate.model_rebuild()
