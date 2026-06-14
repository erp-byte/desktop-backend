"""Pydantic request/response models for the Sample Issuing module.

Response bodies are returned as plain dicts assembled by the services (they
carry nested articles / approvals / audit), so only request models are typed
here. Article selection requires a sku_id sourced from /api/v1/so/sku-lookup —
free-text articles are rejected (spec §15.1).
"""
from __future__ import annotations

from datetime import date
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
    description: Optional[str] = None        # free-text request description
    base_bom_id: Optional[int] = None
    npd_target_name: Optional[str] = None    # requested new NPD article name
    pcs: Optional[float] = None              # number of pieces
    weight_per_piece: Optional[float] = None # weight per piece (kg)
    quantity: Optional[float] = None         # total = pcs × weight_per_piece (kg)
    internal_override: bool = False
    transporter_name: Optional[str] = None   # optional / nullable
    vehicle_number: Optional[str] = None     # optional / nullable
    # Customer + dispatch planning (carried onto the dev job card).
    company_name: Optional[str] = None
    customer_name: Optional[str] = None
    customer_contact: Optional[str] = None
    customer_ship_to_address: Optional[str] = None
    mode_of_transport: Optional[str] = None
    expected_dispatch_date: Optional[date] = None    # by BD team
    confirmed_dispatch_date: Optional[date] = None   # by NPD
    articles: list[ArticleIn] = Field(default_factory=list)


class RequisitionUpdate(BaseModel):
    warehouse: Optional[Warehouse] = None
    npd_target_name: Optional[str] = None
    requestor_team: Optional[str] = None
    purpose_tag: Optional[PurposeTag] = None
    purpose_note: Optional[str] = None
    description: Optional[str] = None
    base_bom_id: Optional[int] = None
    pcs: Optional[float] = None
    weight_per_piece: Optional[float] = None
    quantity: Optional[float] = None
    transporter_name: Optional[str] = None
    vehicle_number: Optional[str] = None
    company_name: Optional[str] = None
    customer_name: Optional[str] = None
    customer_contact: Optional[str] = None
    customer_ship_to_address: Optional[str] = None
    mode_of_transport: Optional[str] = None
    expected_dispatch_date: Optional[date] = None
    confirmed_dispatch_date: Optional[date] = None
    articles: Optional[list[ArticleIn]] = None


# ── NPD sample requisition (the NPD-first create form) ─────────────────────
# A pure request (no article lines, no recipe — those come later on /develop).
# NPD-mandatory fields are enforced here at the Pydantic boundary; the warehouse
# set is the 5 the NPD form offers (a subset of the full Warehouse CHECK).
NpdSampleType = Literal["NPD", "TRIAL"]                       # NPD Internal / Pilot Customer trial
NpdWarehouse = Literal["W202", "A185", "A68", "F53", "A101"]


class NpdRequisitionCreate(BaseModel):
    sample_type: NpdSampleType                               # required
    npd_target_name: str = Field(min_length=1)               # required: target NPD article
    pcs: float = Field(gt=0)                                 # required: number of pieces
    weight_per_piece: float = Field(gt=0)                    # required: kg per piece
    quantity: Optional[float] = None                         # computed server-side = pcs × weight
    warehouse: NpdWarehouse                                  # required
    company_name: str = Field(min_length=1)                  # required
    customer_name: str = Field(min_length=1)                 # required
    customer_contact: Optional[str] = None                   # nullable
    customer_ship_to_address: Optional[str] = None           # nullable
    mode_of_transport: Optional[str] = None                  # nullable
    expected_dispatch_date: Optional[date] = None            # by BD team (nullable)
    description: Optional[str] = None                        # nullable
    purpose_tag: Optional[PurposeTag] = None                 # nullable
    requestor_team: Optional[str] = None                     # nullable


class ApprovalAction(BaseModel):
    action: Literal["APPROVED", "REJECTED"]
    remarks: Optional[str] = None


class NpdReviewBody(BaseModel):
    # NPD team's verdict on a BH-sent request. Reason required for reject + hold
    # (enforced in the service). start_date is the date the hold takes effect —
    # only meaningful for HOLD; ignored otherwise.
    action: Literal["ACCEPT", "APPROVE", "REJECT", "HOLD"]
    reason: Optional[str] = None
    start_date: Optional[date] = None


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
    pcs: Optional[float] = None
    weight_per_piece: Optional[float] = None
    uom: Optional[str] = None
    source_requisition_id: Optional[int] = None   # set when started from a request's "Develop"
    # Customer + dispatch planning. Inherited from the source requisition when
    # omitted (the service back-fills from source_requisition_id).
    company_name: Optional[str] = None
    customer_name: Optional[str] = None
    customer_contact: Optional[str] = None
    customer_ship_to_address: Optional[str] = None
    mode_of_transport: Optional[str] = None
    expected_dispatch_date: Optional[date] = None
    confirmed_dispatch_date: Optional[date] = None
    clone_from_base: bool = False
    lines: list[NpdLineIn] = Field(default_factory=list)


class DevJobCardClose(BaseModel):
    promote_phase_id: Optional[int] = None   # which phase's recipe becomes the live BOM
    output_qty: Optional[float] = Field(default=None, ge=0)
    output_uom: Optional[str] = None
    yield_pct: Optional[float] = Field(default=None, ge=0)   # server recomputes when rm_consumed_qty given
    rm_consumed_qty: Optional[float] = Field(default=None, ge=0)
    wastage_qty: Optional[float] = Field(default=None, ge=0)
    extra_give_away_qty: Optional[float] = Field(default=None, ge=0)
    output_notes: Optional[str] = None


class PromoteApprovalBody(BaseModel):
    action: Literal["ACCEPT", "REJECT"]
    remarks: Optional[str] = None
    # Only needed when one user holds BOTH gates (inventory_manager who is also the
    # requestor) — names which gate this action applies to so each is actioned once.
    approver_kind: Optional[Literal["INV_MGR", "REQUESTOR_BH"]] = None


class PromoteEmailReject(BaseModel):
    """Reject a promote gate from the email-driven reason dialog on the web app. PUBLIC
    (no session) — authenticated by the recipient `email` owning the gate; a reason is
    required."""
    dev_jc_id: int
    approver_kind: Literal["INV_MGR", "REQUESTOR_BH"]
    email: str
    remarks: str


class DevDispatchBody(BaseModel):
    recipient: Optional[str] = None
    qty: Optional[float] = Field(default=None, ge=0)


# Trial phases (multi-day) on a development job card.
class DevPhaseCreate(BaseModel):
    name: str = Field(min_length=1)
    # Which phase's recipe to clone as a starting point. Omit → clone the latest
    # phase (or the card base recipe if this is the first phase).
    clone_from_phase_id: Optional[int] = None


class DevPhaseComplete(BaseModel):
    # Per-phase output + material accounting (same shape as the card-level close);
    # yield_pct is recomputed server-side from output / rm_consumed.
    output_qty: Optional[float] = Field(default=None, ge=0)
    output_uom: Optional[str] = None
    rm_consumed_qty: Optional[float] = Field(default=None, ge=0)
    wastage_qty: Optional[float] = Field(default=None, ge=0)
    extra_give_away_qty: Optional[float] = Field(default=None, ge=0)
    notes: Optional[str] = None


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
