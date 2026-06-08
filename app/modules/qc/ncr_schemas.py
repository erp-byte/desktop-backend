"""Pydantic v2 schemas for the NCR (Non-Conformance Report) module.

Covers GET /api/v1/qc/parameters (catalog) and the /api/v1/qc/ncr endpoints.
The two 1:N detail sets (failed parameters, supplier CAPA actions) are modelled
as nested lists and persisted into the ncr_record JSONB columns.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ── Parameter catalog ─────────────────────────────────────────────────────


class ParameterItem(BaseModel):
    parameter_id: int
    code: str
    name: str
    unit: str | None = None
    param_group: str | None = None
    data_type: str | None = None
    value_kind: str | None = None   # 'num' | 'text'
    spec_note: str | None = None
    sort_order: int | None = None
    is_active: bool = True


class ParameterListResponse(BaseModel):
    items: list[ParameterItem] = Field(default_factory=list)


# ── NCR folded detail rows ────────────────────────────────────────────────


class FailedParameter(BaseModel):
    param_code: str | None = None
    reading_id: int | None = None
    spec_value: float | None = None
    actual_value: float | None = None
    deviation_type: Literal["above_max", "below_min", "presence", "absence"] | None = None
    severity: Literal["critical", "major", "minor"] | None = None
    quantity_impacted_kg: float | None = None
    disposition: str | None = None


class SupplierAction(BaseModel):
    action_type: Literal["root_cause", "correction", "preventive"] | None = None
    description: str | None = None
    responsible_party: str | None = None
    target_date: str | None = None
    actual_closure_date: str | None = None
    evidence_file_url: str | None = None
    verified_by: int | None = None
    verification_date: str | None = None
    is_effective: bool | None = None


# ── List / detail ─────────────────────────────────────────────────────────


class NcrListItem(BaseModel):
    ncr_id: int
    ncr_no: str | None = None
    supplier_id: int | None = None
    supplier_name: str | None = None
    transaction_no: str | None = None
    product_description: str | None = None
    status: str | None = None
    severity_rollup: str | None = None
    disposition: str | None = None
    food_safety_flag: bool = False
    documented_date: str | None = None
    created_at: str | None = None
    param_count: int = 0


class NcrListResponse(BaseModel):
    items: list[NcrListItem] = Field(default_factory=list)
    total: int
    total_pages: int
    page: int
    page_size: int


class NcrDetail(BaseModel):
    ncr_id: int
    ncr_no: str | None = None
    # header
    supplier_id: int | None = None
    supplier_name: str | None = None
    other_party: str | None = None
    detected_by: int | None = None
    # lot info
    invoice_challan_ref: str | None = None
    batch_no: str | None = None
    transaction_no: str | None = None
    line_number: int | None = None
    product_description: str | None = None
    rc_no: str | None = None
    quantity: float | None = None
    # reason
    reason_nonconformity: str | None = None
    food_safety_flag: bool = False
    documented_by: int | None = None
    documented_date: str | None = None
    # disposition
    disposition: str | None = None
    financial_action: str | None = None
    supplier_action_type: str | None = None
    target_completion_date: str | None = None
    # sign-off
    authorized_person: int | None = None
    authorized_date: str | None = None
    received_by: int | None = None
    received_date: str | None = None
    created_by: int | None = None
    approved_by: int | None = None
    # computed
    ncr_category: str | None = None
    severity_rollup: str | None = None
    financial_impact_inr: float | None = None
    closure_tat_days: int | None = None
    status: str | None = None
    # folded details
    failed_parameters: list[FailedParameter] = Field(default_factory=list)
    supplier_actions: list[SupplierAction] = Field(default_factory=list)
    created_at: str | None = None


# ── Request bodies ────────────────────────────────────────────────────────


class NcrCreateRequest(BaseModel):
    """POST /api/v1/qc/ncr — create. If from_inspection_id is set, the server
    prefills supplier/lot/product fields and the failed_parameters list from the
    inspection's out-of-spec readings (caller-supplied fields still win)."""

    from_inspection_id: int | None = None
    ncr_no: str | None = None
    supplier_id: int | None = None
    supplier_name: str | None = None
    other_party: str | None = None
    detected_by: int | None = None
    invoice_challan_ref: str | None = None
    batch_no: str | None = None
    transaction_no: str | None = None
    line_number: int | None = None
    product_description: str | None = None
    rc_no: str | None = None
    quantity: float | None = None
    reason_nonconformity: str | None = None
    food_safety_flag: bool | None = None
    documented_date: str | None = None
    disposition: Literal["rejected", "returned", "accepted_dispensation"] | None = None
    financial_action: Literal["correction", "credit", "debit"] | None = None
    supplier_action_type: Literal["info_only", "investigation_required"] | None = None
    target_completion_date: str | None = None
    failed_parameters: list[FailedParameter] | None = None
    supplier_actions: list[SupplierAction] | None = None


class NcrUpdateRequest(BaseModel):
    """PATCH /api/v1/qc/ncr/{id} — partial update of any field. Provided fields
    replace; omitted fields are left untouched."""

    supplier_id: int | None = None
    supplier_name: str | None = None
    other_party: str | None = None
    detected_by: int | None = None
    invoice_challan_ref: str | None = None
    batch_no: str | None = None
    transaction_no: str | None = None
    line_number: int | None = None
    product_description: str | None = None
    rc_no: str | None = None
    quantity: float | None = None
    reason_nonconformity: str | None = None
    food_safety_flag: bool | None = None
    documented_by: int | None = None
    documented_date: str | None = None
    disposition: Literal["rejected", "returned", "accepted_dispensation"] | None = None
    financial_action: Literal["correction", "credit", "debit"] | None = None
    supplier_action_type: Literal["info_only", "investigation_required"] | None = None
    target_completion_date: str | None = None
    authorized_person: int | None = None
    authorized_date: str | None = None
    received_by: int | None = None
    received_date: str | None = None
    approved_by: int | None = None
    ncr_category: str | None = None
    financial_impact_inr: float | None = None
    status: Literal["open", "in_supplier_action", "closed"] | None = None
    failed_parameters: list[FailedParameter] | None = None
    supplier_actions: list[SupplierAction] | None = None


class NcrCreateResponse(BaseModel):
    ncr_id: int
    ncr_no: str | None = None
