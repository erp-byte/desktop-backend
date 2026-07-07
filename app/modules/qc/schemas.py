"""Pydantic v2 schemas for the QC Inward Inspection module.

Covers every shape used by /api/v1/qc endpoints:
  readings & audit, list/detail, intimations picker,
  and all request/response bodies for state transitions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ── Readings & audit ─────────────────────────────────────────────────────


class Reading(BaseModel):
    """A single parameter reading captured during an inspection."""

    reading_id: int
    parameter_id: int | None = None
    parameter_name: str | None = None
    parameter_unit: str | None = None
    observed_value_num: float | None = None
    observed_value_text: str | None = None
    spec_min: float | None = None
    spec_max: float | None = None
    spec_target: float | None = None
    is_within_spec: bool | None = None
    severity: str | None = None
    deviation_pct: float | None = None
    method: str | None = None
    instrument: str | None = None
    notes: str | None = None
    recorded_at: str | None = None


class AuditEvent(BaseModel):
    """One entry in the inspection audit timeline."""

    event_type: str
    from_state: str | None = None
    to_state: str | None = None
    occurred_at: str | None = None
    actor_user_id: int | None = None
    payload_diff: dict | None = None


# ── List / detail ────────────────────────────────────────────────────────


class InspectionListItem(BaseModel):
    """Row returned by GET /inspection list."""

    inspection_id: int
    po_number: str | None = None
    transaction_no: str | None = None
    sku_name: str | None = None
    sku_name_raw: str | None = None
    sku_id: int | None = None
    vehicle_no: str | None = None
    warehouse: str | None = None
    verdict: str | None = None
    status: str | None = None
    decision: str | None = None


class InspectionListResponse(BaseModel):
    """Paginated list of inspections."""

    items: list[InspectionListItem] = Field(default_factory=list)
    total: int
    total_pages: int
    page: int
    page_size: int


class InspectionDetail(BaseModel):
    """Full inspection detail including nested readings."""

    inspection_id: int
    inspection_ref: str | None = None
    qc_intimation_id: int | None = None
    status: str | None = None
    verdict: str | None = None
    sample_size: int | None = None
    inspection_method: str | None = None
    inspector_user_id: int | None = None
    started_at: str | None = None
    started_by: int | None = None
    started_by_name: str | None = None
    verdict_at: str | None = None
    accepted_qty: float | None = None
    rejected_qty: float | None = None
    ncr_no: str | None = None
    cancelled_at: str | None = None
    cancelled_by_name: str | None = None
    cancel_reason: str | None = None
    reopened_at: str | None = None
    reopen_reason: str | None = None
    verdict_overridden_by: int | None = None
    verdict_overridden_by_name: str | None = None
    override_reason: str | None = None
    approved_by: int | None = None
    approved_by_name: str | None = None
    approved_at: str | None = None
    remarks: str | None = None
    # Denormalised display fields
    po_number: str | None = None
    transaction_no: str | None = None
    sku_id: int | None = None
    sku_name: str | None = None
    sku_name_raw: str | None = None
    supplier_id: int | None = None
    supplier_name: str | None = None
    lot_number: str | None = None
    vehicle_no: str | None = None
    warehouse: str | None = None
    readings: list[Reading] = Field(default_factory=list)


# ── Intimations picker ───────────────────────────────────────────────────


class IntimationItem(BaseModel):
    """One row in the pending intimations picker."""

    qc_intimation_id: int
    sku_name: str | None = None
    sku_name_raw: str | None = None
    sku_id: int | None = None
    po_number: str | None = None
    transaction_no: str | None = None
    supplier_name: str | None = None
    supplier_id: int | None = None
    lot_number: str | None = None
    coa_received: bool = False


class IntimationListResponse(BaseModel):
    """List of pending intimations available to start an inspection from."""

    items: list[IntimationItem] = Field(default_factory=list)


# ── Request / response bodies ────────────────────────────────────────────


class StartRequest(BaseModel):
    """POST /inspection/start — create a new inspection from an intimation."""

    qc_intimation_id: int
    sample_size: int = Field(ge=1)
    inspection_method: str = "combined"
    remarks: str | None = None


class StartResponse(BaseModel):
    """Returned after a successful inspection start."""

    inspection_id: int


class ReadingInput(BaseModel):
    """One reading in a batch submission."""

    parameter_id: int
    observed_value_num: float | None = None
    observed_value_text: str | None = None
    method: str | None = None
    instrument: str | None = None
    notes: str | None = None


class ReadingsBatchRequest(BaseModel):
    """POST /inspection/{id}/readings — submit one or more readings."""

    readings: list[ReadingInput] = Field(min_length=1)


class ReadingsBatchResponse(BaseModel):
    """Summary after a batch readings insert."""

    inserted_count: int
    out_of_spec_count: int


class ReadingUpdateRequest(BaseModel):
    """PUT /inspection/{id}/readings/{rid} — edit a single reading."""

    observed_value_num: float | None = None
    observed_value_text: str | None = None
    method: str | None = None
    instrument: str | None = None
    notes: str | None = None


class ReadingDeleteRequest(BaseModel):
    """DELETE /inspection/{id}/readings/{rid} — record deletion reason."""

    reason: str | None = None


class VerdictRequest(BaseModel):
    """POST /inspection/{id}/verdict — set pass/fail verdict."""

    verdict: Literal["passed", "failed"]
    accepted_qty: float | None = None
    rejected_qty: float | None = None
    summary_remarks: str | None = None


class VerdictResponse(BaseModel):
    """Returned after verdict is recorded."""

    verdict: str
    next_step: str | None = None


class OverrideRequest(BaseModel):
    """POST /inspection/{id}/verdict/override — manager/admin override."""

    new_verdict: Literal["passed", "failed"]
    reason: str = Field(min_length=10)


class OverrideResponse(BaseModel):
    """Returned after a verdict override."""

    old_verdict: str | None = None
    new_verdict: str


class CancelRequest(BaseModel):
    """POST /inspection/{id}/cancel — cancel an in-progress inspection."""

    reason: str = Field(min_length=3)


class ReopenRequest(BaseModel):
    """POST /inspection/{id}/reopen — reopen a cancelled inspection."""

    reason: str = Field(min_length=3)


class HeaderUpdateRequest(BaseModel):
    """PUT /inspection/{id} — partial header update."""

    sample_size: int | None = None
    inspection_method: str | None = None
    inspector_user_id: int | None = None
    remarks: str | None = None


class OkResponse(BaseModel):
    """Generic success acknowledgement."""

    ok: bool = True


class ReportResponse(BaseModel):
    """Stub response for RM/NCR report generation."""

    report_id: str
    download_url: str | None = None


# ── Material arrivals (Material In QC status) ─────────────────────────────


class ArrivalItem(BaseModel):
    """One dock arrival (qc_intimation) + its latest inspection state."""

    qc_intimation_id: int
    transaction_no: str | None = None
    po_number: str | None = None
    sku_id: int | None = None
    sku_name: str | None = None
    sku_name_raw: str | None = None
    lot_number: str | None = None
    invoice_no: str | None = None
    vehicle_no: str | None = None
    created_at: str | None = None
    inspection_id: int | None = None
    inspection_status: str | None = None
    verdict: str | None = None
    approved_at: str | None = None
    qc_state: str  # arrived | in_qc | accepted | rejected


class ArrivalListResponse(BaseModel):
    items: list[ArrivalItem] = Field(default_factory=list)


class ArrivalSummaryItem(BaseModel):
    """Per-transaction QC rollup used for the row badge + status filter."""

    transaction_no: str
    status: str  # arrived | completed  (pending_arrival inferred client-side)
    total: int
    awaiting: int
    in_qc: int
    accepted: int
    rejected: int


class ArrivalSummaryResponse(BaseModel):
    items: list[ArrivalSummaryItem] = Field(default_factory=list)
