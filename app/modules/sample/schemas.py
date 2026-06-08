"""Pydantic request/response models for the Sample Issuing module.

Response bodies are returned as plain dicts assembled by the services (they
carry nested articles / approvals / audit), so only request models are typed
here. Article selection requires a sku_id sourced from /api/v1/so/sku-lookup —
free-text articles are rejected (spec §15.1).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

SampleType = Literal["BASIS_RM", "BASIS_FG", "NPD", "INTERNAL", "TRIAL"]
ArticleRole = Literal["RM", "FG", "NPD_INPUT", "NPD_OUTPUT"]
Warehouse = Literal["W202", "A185", "A68", "F53", "A101", "D-39", "D-514", "Rishi", "Supreme"]
PurposeTag = Literal[
    "CUSTOMER_DISPLAY", "CUSTOMER_ISSUE", "TASTING_SENSORY",
    "PHYSICAL_PARAMETERS", "INTERNAL_OTHER",
]


class ArticleIn(BaseModel):
    sku_id: int
    sku_name: str
    required_qty: float = Field(gt=0)
    uom: str
    article_role: ArticleRole
    pack_size_kg: Optional[float] = None
    notes: Optional[str] = None


class RequisitionCreate(BaseModel):
    sample_type: SampleType
    warehouse: Warehouse
    requestor_team: Optional[str] = None
    purpose_tag: Optional[PurposeTag] = None
    purpose_note: Optional[str] = None
    base_bom_id: Optional[int] = None
    npd_target_name: Optional[str] = None    # requested new NPD article name
    quantity: Optional[float] = None         # requested quantity (free float)
    internal_override: bool = False
    transporter_name: Optional[str] = None   # optional / nullable
    vehicle_number: Optional[str] = None     # optional / nullable
    articles: list[ArticleIn] = Field(default_factory=list)


class RequisitionUpdate(BaseModel):
    requestor_team: Optional[str] = None
    purpose_tag: Optional[PurposeTag] = None
    purpose_note: Optional[str] = None
    base_bom_id: Optional[int] = None
    quantity: Optional[float] = None
    transporter_name: Optional[str] = None
    vehicle_number: Optional[str] = None
    articles: Optional[list[ArticleIn]] = None


class ApprovalAction(BaseModel):
    action: Literal["APPROVED", "REJECTED"]
    remarks: Optional[str] = None


class CancelBody(BaseModel):
    reason: str


class IssuedLine(BaseModel):
    article_id: int
    qty: float = Field(gt=0)


class OutwardBody(BaseModel):
    from_location: Optional[str] = None
    issued: Optional[list[IssuedLine]] = None


# ── NPD draft BOM ─────────────────────────────────────────────────────────
class NpdLineIn(BaseModel):
    # Nullable: clone-from-base lines are name-based (bom_line has no sku_id), so
    # npd_draft_bom_lines.sku_id is nullable and edits re-send those lines as-is.
    sku_id: Optional[int] = None
    sku_name: str
    qty: float = Field(ge=0)
    uom: str
    item_type: Optional[Literal["rm", "pm"]] = None
    delta_type: Literal["UNCHANGED", "ADDED", "MODIFIED", "REMOVED"] = "UNCHANGED"
    original_qty: Optional[float] = None
    # Per-ingredient ownership (NPD plan §3): CUSTOMER / off-master lines are
    # traceability-only — the accounting backbone posts inventory for OWN lines.
    ownership: Literal["OWN", "CUSTOMER"] = "OWN"
    is_off_master: bool = False
    customer_lot_ref: Optional[str] = None
    received_qty: Optional[float] = Field(default=None, ge=0)
    line_order: int = 0
    notes: Optional[str] = None


class NpdDraftCreate(BaseModel):
    base_bom_id: Optional[int] = None
    fg_sku_id: Optional[int] = None
    fg_sku_name: Optional[str] = None
    description: Optional[str] = None
    clone_from_base: bool = False
    lines: Optional[list[NpdLineIn]] = None


class NpdLinesReplace(BaseModel):
    lines: list[NpdLineIn]


# ── Standalone NPD development job cards ───────────────────────────────────
# Pure R&D, decoupled from sample requisitions. The recipe lines reuse NpdLineIn
# (same name-based, sku_id-nullable shape as the requisition draft BOM lines).
class DevJobCardCreate(BaseModel):
    title: str
    description: Optional[str] = None
    warehouse: Optional[Warehouse] = None
    base_bom_id: Optional[int] = None
    fg_sku_id: Optional[int] = None
    fg_sku_name: Optional[str] = None
    target_qty: Optional[float] = Field(default=None, ge=0)
    uom: Optional[str] = None
    clone_from_base: bool = False
    lines: list[NpdLineIn] = Field(default_factory=list)


class DevJobCardClose(BaseModel):
    output_qty: Optional[float] = Field(default=None, ge=0)
    output_uom: Optional[str] = None
    yield_pct: Optional[float] = Field(default=None, ge=0)   # server recomputes when rm_consumed_qty given
    rm_consumed_qty: Optional[float] = Field(default=None, ge=0)
    wastage_qty: Optional[float] = Field(default=None, ge=0)
    extra_give_away_qty: Optional[float] = Field(default=None, ge=0)
    output_notes: Optional[str] = None


class DevDispatchBody(BaseModel):
    recipient: Optional[str] = None
    qty: Optional[float] = Field(default=None, ge=0)


# ── RM Issue / Collection Form (Document 015, NPD plan §10) ────────────────
class RmIssueLineIn(BaseModel):
    sku_id: Optional[int] = None
    sku_name: str
    location: Optional[str] = None
    reqd_qty: float = Field(ge=0)
    uom: str = "kg"
    ownership: Literal["OWN", "CUSTOMER"] = "OWN"
    is_off_master: bool = False
    notes: Optional[str] = None
    line_order: int = 0


class RmIssueFormCreate(BaseModel):
    trial_name: Optional[str] = None
    product_name: Optional[str] = None
    customer_name: Optional[str] = None
    purpose_tag: Optional[str] = None
    source_type: Optional[str] = None       # NPD_DEV_JC | SAMPLE_REQ | STANDALONE
    source_id: Optional[int] = None
    requisition_id: Optional[int] = None
    notes: Optional[str] = None
    submit: bool = True                      # raise (SUBMITTED) vs save DRAFT
    lines: list[RmIssueLineIn] = Field(default_factory=list)


class RmIssueLineResult(BaseModel):
    line_id: int
    issued_qty: float = Field(ge=0)
    lot_no: Optional[str] = None


class RmIssueBody(BaseModel):
    issued: list[RmIssueLineResult] = Field(default_factory=list)


# ── Gate pass / conversion ────────────────────────────────────────────────
class GatePassIssueBody(BaseModel):
    recipient_name: Optional[str] = None
    recipient_contact: Optional[str] = None
    vehicle_carrier: Optional[str] = None
    driver_name: Optional[str] = None
    from_location: Optional[str] = None


class InvVerifyBody(BaseModel):
    remarks: Optional[str] = None


class VoidBody(BaseModel):
    reason: str


class ConvertFullBody(GatePassIssueBody):
    remarks: Optional[str] = None


class ConvertPartialBody(GatePassIssueBody):
    qty: float = Field(gt=0)
    remarks: Optional[str] = None
