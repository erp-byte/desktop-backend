"""Pydantic request/response models for the Sample Issuing module.

Response bodies are returned as plain dicts assembled by the services (they
carry nested articles / approvals / audit), so only request models are typed
here. Article selection requires a sku_id sourced from /api/v1/so/sku-lookup —
free-text articles are rejected (spec §15.1).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

SampleType = Literal["BASIS_RM", "BASIS_FG", "NPD", "INTERNAL"]
ArticleRole = Literal["RM", "FG", "NPD_INPUT", "NPD_OUTPUT"]
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
    entity: Optional[str] = None
    requestor_team: Optional[str] = None
    purpose_tag: Optional[PurposeTag] = None
    purpose_note: Optional[str] = None
    base_bom_id: Optional[int] = None
    internal_override: bool = False
    articles: list[ArticleIn] = Field(default_factory=list)


class RequisitionUpdate(BaseModel):
    requestor_team: Optional[str] = None
    purpose_tag: Optional[PurposeTag] = None
    purpose_note: Optional[str] = None
    base_bom_id: Optional[int] = None
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
