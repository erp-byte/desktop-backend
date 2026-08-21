"""Pydantic request/response models for the Sample Issuing module.

Response bodies are returned as plain dicts assembled by the services (they
carry nested articles / approvals / audit), so only request models are typed
here. Article selection requires a sku_id sourced from /api/v1/so/sku-lookup —
free-text articles are rejected (spec §15.1).
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.core.types import Amount2

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


class NpdTargetIn(BaseModel):
    name: str = Field(min_length=1)          # target NPD article name (a new product)
    pcs: float = Field(gt=0)                 # number of pieces
    weight_per_piece: float = Field(gt=0)    # kg per piece — quantity derived server-side


class RequisitionCreate(BaseModel):
    sample_type: SampleType
    warehouse: Warehouse
    # The business head this request is raised FOR (085). Server-validated as an active
    # business_head; it becomes requestor_user_id + business_head_user_id, and mirrors
    # into requestor_team for display. Omitted -> the creator stays the requestor.
    requestor_user_id: Optional[int] = None
    requestor_team: Optional[str] = None
    # Sales point of contact (085). Defaults to the signed-in user when omitted; naming a
    # user resolves their name+email from auth_user, or pass name/email directly for a POC
    # without a login. Display + mail Cc only — drives no approval gate.
    sales_poc_user_id: Optional[int] = None
    sales_poc_name: Optional[str] = None
    sales_poc_email: Optional[str] = None
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


class ProductionRequestBody(BaseModel):
    """Optional note on why this sample's article has to be produced."""
    note: Optional[str] = None


class RequisitionUpdate(BaseModel):
    warehouse: Optional[Warehouse] = None
    npd_target_name: Optional[str] = None
    requestor_user_id: Optional[int] = None   # re-point the requestor BH (085)
    requestor_team: Optional[str] = None
    # Sales point of contact (085). Defaults to the signed-in user when omitted; naming a
    # user resolves their name+email from auth_user, or pass name/email directly for a POC
    # without a login. Display + mail Cc only — drives no approval gate.
    sales_poc_user_id: Optional[int] = None
    sales_poc_name: Optional[str] = None
    sales_poc_email: Optional[str] = None
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
    targets: Optional[list[NpdTargetIn]] = None   # multiple NPD target articles (replace on edit)
    articles: Optional[list[ArticleIn]] = None
    # Billing checklist (NPD/TRIAL): returnable XOR non_returnable; amount is 0
    # unless paid, else > 0 with at most 2 decimals (Amount2 rejects >2dp).
    returnable: Optional[bool] = None
    non_returnable: Optional[bool] = None
    paid: Optional[bool] = None
    amount: Amount2 = None

    @model_validator(mode="after")
    def _check_billing(self):
        if self.returnable and self.non_returnable:
            raise ValueError("returnable and non_returnable cannot both be selected")
        if self.paid is True:
            if not self.amount or self.amount <= 0:
                raise ValueError("amount is required and must be greater than 0 when paid")
        elif self.paid is False:
            self.amount = 0   # paid unticked → amount locked to 0
        return self


# ── NPD sample requisition (the NPD-first create form) ─────────────────────
# A pure request (no article lines, no recipe — those come later on /develop).
# NPD-mandatory fields are enforced here at the Pydantic boundary; the warehouse
# set is the 5 the NPD form offers (a subset of the full Warehouse CHECK).
NpdSampleType = Literal["NPD", "TRIAL"]                       # NPD Internal / Pilot Customer trial
NpdWarehouse = Literal["W202", "A185", "A68", "F53", "A101"]


class NpdRequisitionCreate(BaseModel):
    sample_type: NpdSampleType                               # required
    # Multiple target articles, each an independent product. The single-target
    # fields below MIRROR targets[0] server-side (backward compat). At least one
    # target is required — via `targets` or the legacy single fields (checked in service).
    targets: Optional[list[NpdTargetIn]] = None
    npd_target_name: Optional[str] = Field(default=None, min_length=1)   # legacy single target
    pcs: Optional[float] = Field(default=None, gt=0)         # legacy single target pieces
    weight_per_piece: Optional[float] = Field(default=None, gt=0)  # legacy single target kg/pc
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
    requestor_user_id: Optional[int] = None                  # the BH this is raised for (085)
    requestor_team: Optional[str] = None                     # nullable (mirrors the BH name)
    # Sales point of contact (085). Defaults to the signed-in user when omitted; naming a
    # user resolves their name+email from auth_user, or pass name/email directly for a POC
    # without a login. Display + mail Cc only — drives no approval gate.
    sales_poc_user_id: Optional[int] = None
    sales_poc_name: Optional[str] = None
    sales_poc_email: Optional[str] = None
    # Billing checklist: returnable XOR non_returnable (both optional); amount is
    # 0 unless paid, else mandatory > 0 with at most 2 decimals (Amount2 is strict).
    returnable: bool = False
    non_returnable: bool = False
    paid: bool = False
    amount: Amount2 = 0

    @model_validator(mode="after")
    def _check_billing(self):
        if self.returnable and self.non_returnable:
            raise ValueError("returnable and non_returnable cannot both be selected")
        if self.paid:
            if not self.amount or self.amount <= 0:
                raise ValueError("amount is required and must be greater than 0 when paid")
        else:
            self.amount = 0   # not paid → amount locked to 0
        return self


class ApprovalAction(BaseModel):
    action: Literal["APPROVED", "REJECTED"]
    remarks: Optional[str] = None


class BhSignoffBody(BaseModel):
    """The bound business head's verdict on a held NPD/TRIAL request (086). A reason is
    required to reject (enforced in the service)."""
    action: Literal["APPROVED", "REJECTED", "APPROVE", "REJECT"]
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

# One target article on a dev job card (082): its details + its own base BOM + recipe.
class DevArticleIn(BaseModel):
    name: str = Field(min_length=1)
    pcs: Optional[float] = Field(default=None, ge=0)
    weight_per_piece: Optional[float] = Field(default=None, ge=0)
    base_bom_id: Optional[int] = None
    base_bom_name: Optional[str] = None
    lines: list[NpdLineIn] = Field(default_factory=list)


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
    # Billing checklist (same rule as the requisition). Left None → inherited from the
    # source requisition; an explicit value (incl. on a standalone card) wins.
    returnable: Optional[bool] = None
    non_returnable: Optional[bool] = None
    paid: Optional[bool] = None
    amount: Amount2 = None
    clone_from_base: bool = False
    lines: list[NpdLineIn] = Field(default_factory=list)
    # Per-article develop (082): each article its own product + base BOM + recipe. When
    # present, the service mirrors article #1 onto the card-level fg_sku_name/pcs/weight.
    articles: Optional[list[DevArticleIn]] = None

    @model_validator(mode="after")
    def _check_billing(self):
        if self.returnable and self.non_returnable:
            raise ValueError("returnable and non_returnable cannot both be selected")
        if self.paid:
            if not self.amount or self.amount <= 0:
                raise ValueError("amount is required and must be greater than 0 when paid")
        elif self.paid is False:   # explicitly not paid → amount locked to 0 (None = inherit)
            self.amount = 0
        return self


class DevJobCardDetailsUpdate(BaseModel):
    """Edit a dev job card's customer & dispatch header (company/customer/contact/ship-to/
    transport/expected-dispatch + billing) while it is DRAFT/IN_DEVELOPMENT. PATCH semantics —
    only the fields actually sent are updated (the service uses model_dump(exclude_unset=True))."""
    company_name: Optional[str] = None
    customer_name: Optional[str] = None
    customer_contact: Optional[str] = None
    customer_ship_to_address: Optional[str] = None
    mode_of_transport: Optional[str] = None
    expected_dispatch_date: Optional[date] = None
    returnable: Optional[bool] = None
    non_returnable: Optional[bool] = None
    paid: Optional[bool] = None
    amount: Amount2 = None

    @model_validator(mode="after")
    def _check_billing(self):
        if self.returnable and self.non_returnable:
            raise ValueError("returnable and non_returnable cannot both be selected")
        if self.paid and (not self.amount or self.amount <= 0):
            raise ValueError("amount is required and must be greater than 0 when paid")
        if self.paid is False:
            self.amount = 0
        return self


class DevArticlesReplace(BaseModel):
    # Replace ALL target articles + their recipes on a DRAFT/IN_DEVELOPMENT dev job card.
    articles: list[DevArticleIn] = Field(default_factory=list)


class DevCloseArticleIn(BaseModel):
    # Per-article output + accounting at promote (082/083). Each article's FG is received
    # into R&D under its own name (its own batch) and this output becomes its dispatchable
    # balance. article_id identifies which article row these numbers belong to.
    article_id: int
    output_qty: Optional[float] = Field(default=None, ge=0)
    output_uom: Optional[str] = None
    rm_consumed_qty: Optional[float] = Field(default=None, ge=0)
    wastage_qty: Optional[float] = Field(default=None, ge=0)
    extra_give_away_qty: Optional[float] = Field(default=None, ge=0)


class DevJobCardClose(BaseModel):
    promote_phase_id: Optional[int] = None   # which phase's recipe becomes the live BOM
    output_qty: Optional[float] = Field(default=None, ge=0)
    output_uom: Optional[str] = None
    yield_pct: Optional[float] = Field(default=None, ge=0)   # server recomputes when rm_consumed_qty given
    rm_consumed_qty: Optional[float] = Field(default=None, ge=0)
    wastage_qty: Optional[float] = Field(default=None, ge=0)
    extra_give_away_qty: Optional[float] = Field(default=None, ge=0)
    output_notes: Optional[str] = None
    # Per-article cards (082): the output/accounting entered PER article. When present the
    # promote fans out — each article receives its own FG + gets its own dispatchable output.
    articles: Optional[list[DevCloseArticleIn]] = None


class PromoteApprovalBody(BaseModel):
    action: Literal["ACCEPT", "REJECT"]
    remarks: Optional[str] = None
    # Only needed when one user holds BOTH gates (inventory_manager who is also the
    # requestor) — names which gate this action applies to so each is actioned once.
    approver_kind: Optional[Literal["INV_MGR", "REQUESTOR_BH"]] = None


class BhSignoffEmailReject(BaseModel):
    """Reject a requisition-stage BH approval from the email-driven reason dialog on the
    web app (086). PUBLIC (no session) — authenticated by `email` being the bound business
    head on that request; a reason is required."""
    request_id: int
    email: str
    remarks: str


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
    # Per-article cards (083): dispatch THIS article's finalized output (from its own FG
    # batch, bounded by its own output_qty). Omit for a legacy single-product card.
    article_id: Optional[int] = None
    # Custom unit for this part (084) — e.g. kg / g / pcs / box. Omit → the article's uom.
    uom: Optional[str] = None


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
