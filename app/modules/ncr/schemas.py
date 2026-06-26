"""Pydantic models for /api/v1/ncr endpoints."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ── enums ────────────────────────────────────────────────────────────────


Severity = Literal["minor", "major", "critical"]
Disposition = Literal[
    "accept_with_concession", "reject", "rework", "return_to_vendor", "scrap",
]
SubmittedVia = Literal["vendor_email", "vendor_portal", "purchase_relay"]
NcrStatus = Literal[
    "raised", "dispositioned", "capa_pending", "capa_submitted",
    "capa_failed_verification", "closed", "cancelled",
]


# ── 2.1 raise ────────────────────────────────────────────────────────────


class ParameterDetailDraft(BaseModel):
    parameter_id: int
    observed_value: str = Field(min_length=1)
    spec_min: float | None = None
    spec_max: float | None = None
    deviation_pct: float | None = None
    severity: Severity | None = None


class NcrRaiseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inspection_id: str | None = None
    transaction_no: str | None = None
    line_number: int | None = None
    sku_id: int | None = None
    supplier_id: int
    lot_number: str | None = None
    rejected_qty: float = Field(gt=0)
    severity: Severity | None = None
    summary: str = Field(min_length=10)
    documented_date: str | None = None
    parameter_details: list[ParameterDetailDraft] = Field(default_factory=list)


class NcrRaiseResponse(BaseModel):
    ncr_no: str
    status: str
    severity: str
    raised_at: str
    raised_by: str
    requires_dual_approval: bool
    notifications_dispatched: int = 0


# ── 2.2 list ─────────────────────────────────────────────────────────────


class NcrListItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ncr_no: str
    status: str
    severity: str | None = None
    inspection_id: str | None = None
    transaction_no: str | None = None
    po_number: str | None = None
    sku_id: int | None = None
    sku_name: str | None = None
    supplier_id: int
    supplier_name: str | None = None
    lot_number: str | None = None
    rejected_qty: float
    disposition: str | None = None
    raised_at: str
    raised_by_name: str | None = None
    capa_rounds_count: int = 0
    closure_tat_days: float | None = None
    closed_at: str | None = None


class NcrListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[NcrListItem]


# ── 2.3 detail ───────────────────────────────────────────────────────────


class NcrParameterDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    detail_id: int
    parameter_id: int | None = None
    parameter_name: str | None = None
    observed_value_text: str | None = None
    observed_value_num: float | None = None
    spec_min: float | None = None
    spec_max: float | None = None
    deviation_pct: float | None = None
    severity: str | None = None
    notes: str | None = None
    created_at: str


class CapaRound(BaseModel):
    model_config = ConfigDict(extra="allow")
    action_id: int
    round_no: int
    root_cause: str
    corrective_action: str
    preventive_action: str | None = None
    target_date: str
    responsible_person: str
    evidence_s3_keys: list[str] = Field(default_factory=list)
    submitted_at: str
    submitted_via: str
    verified_at: str | None = None
    verified_by: str | None = None
    is_effective: bool | None = None
    verification_notes: str | None = None
    verification_evidence_s3_keys: list[str] | None = None


class NcrVerification(BaseModel):
    model_config = ConfigDict(extra="allow")
    action_id: int
    is_effective: bool
    verification_notes: str
    verification_evidence_s3_keys: list[str]
    verified_at: str
    verified_by: str
    verified_by_name: str | None = None


class NcrDetailResponse(NcrListItem):
    summary: str | None = None
    documented_date: str | None = None
    parameter_details: list[NcrParameterDetail] = Field(default_factory=list)
    supplier_actions: list[CapaRound] = Field(default_factory=list)
    verification: NcrVerification | None = None
    cancel_reason: str | None = None
    reopen_reason: str | None = None
    dual_approval_by_name: str | None = None


# ── 2.4 update header ────────────────────────────────────────────────────


class NcrUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Severity | None = None
    summary: str | None = Field(default=None, min_length=10)
    documented_date: str | None = None


# ── 2.5 / 2.6 disposition ────────────────────────────────────────────────


class DispositionRequest(BaseModel):
    disposition: Disposition
    disposition_qty: float = Field(gt=0)
    financial_impact: float | None = None
    rationale: str = Field(min_length=10)
    rtv_vehicle_no: str | None = None
    rtv_lr_no: str | None = None
    concurrence_user_id: str | None = None


class DispositionResponse(BaseModel):
    ncr_no: str
    disposition: str
    disposition_at: str
    next_step: Literal["capa_required", "capa_optional"]


class DispositionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disposition: Disposition | None = None
    disposition_qty: float | None = Field(default=None, gt=0)
    rationale: str | None = Field(default=None, min_length=10)
    edit_reason: str = Field(min_length=10)


# ── 2.7 / 2.8 / 2.9 CAPA ─────────────────────────────────────────────────


class CapaSubmitRequest(BaseModel):
    root_cause: str = Field(min_length=10)
    corrective_action: str = Field(min_length=10)
    preventive_action: str | None = None
    target_date: str
    responsible_person: str = Field(min_length=1)
    evidence_s3_keys: list[str] = Field(default_factory=list)
    submitted_via: SubmittedVia = "purchase_relay"


class CapaResponse(BaseModel):
    action_id: int
    ncr_no: str
    round_no: int
    submitted_at: str


class CapaUpdateRequest(BaseModel):
    root_cause: str | None = None
    corrective_action: str | None = None
    preventive_action: str | None = None
    target_date: str | None = None
    responsible_person: str | None = None
    evidence_s3_keys: list[str] | None = None
    edit_reason: str = Field(min_length=10)


# ── 2.10 verify ──────────────────────────────────────────────────────────


class VerifyRequest(BaseModel):
    action_id: int
    is_effective: bool
    verification_notes: str = Field(min_length=10)
    verification_evidence_s3_keys: list[str] = Field(default_factory=list)


class VerifyResponse(BaseModel):
    ncr_no: str
    action_id: int
    is_effective: bool
    verified_at: str
    new_status: str
    closure_tat_days: float | None = None


# ── 2.11 / 2.12 cancel + reopen ──────────────────────────────────────────


class NcrCancelRequest(BaseModel):
    reason: str = Field(min_length=10)


class NcrReopenRequest(BaseModel):
    reason: str = Field(min_length=10)
    dual_approver_user_id: str = Field(min_length=1)


# ── 2.5b dual-approve (CR-01 fix) ────────────────────────────────────────


class DualApproveRequest(BaseModel):
    """Record the second-pair-of-eyes approver on a critical NCR so closure
    can proceed. The approver must be a different user from the actor and
    must not have been the raiser/dispositioner/concurrer."""
    approver_user_id: str = Field(min_length=1)
    reason: str = Field(min_length=10)


class DualApproveResponse(BaseModel):
    ncr_no: str
    dual_approval_by: str
    dual_approval_at: str


# ── 2.13 audit ───────────────────────────────────────────────────────────


class NcrAuditEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    event_id: int
    event_type: str
    entity_type: str
    entity_id: str
    occurred_at: str
    occurred_by: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
