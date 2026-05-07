# app/modules/production/schemas/job_card_edit.py
"""Request schemas for PATCH and DELETE on job cards and annexures.

Every PATCH model has all fields Optional with default=None. The router
converts the request body via `body.model_dump(exclude_unset=True)` so only
fields the client actually sent appear in the dict — that is what guarantees
the 'preserve unspecified columns' behavior.
"""

from pydantic import BaseModel, Field


class JobCardPatchRequest(BaseModel):
    machine_id:              int | None       = None
    assigned_to_team_leader: str | None       = None
    team_members:            list[str] | None = None
    factory:                 str | None       = None
    floor:                   str | None       = None
    customer_name:           str | None       = None
    batch_number:            str | None       = None
    batch_size_kg:           float | None     = Field(None, gt=0)
    bom_id:                  int | None       = None
    process_name:            str | None       = None
    stage:                   str | None       = None
    updated_by:              str


class JobCardCancelRequest(BaseModel):
    cancellation_reason: str = Field(..., min_length=3)
    deleted_by:          str


class EnvironmentPatchRequest(BaseModel):
    parameter_name: str | None = None
    value:          str | None = None
    updated_by:     str


class MetalDetectionPatchRequest(BaseModel):
    check_type:   str | None  = None
    fe_pass:      bool | None = None
    nfe_pass:     bool | None = None
    ss_pass:      bool | None = None
    failed_units: int | None  = Field(None, ge=0)
    remarks:      str | None  = None
    updated_by:   str


class WeightCheckPatchRequest(BaseModel):
    sample_number:  int | None   = Field(None, gt=0)
    net_weight:     float | None = Field(None, ge=0)
    gross_weight:   float | None = Field(None, ge=0)
    leak_test_pass: bool | None  = None
    updated_by:     str


class LossReconciliationPatchRequest(BaseModel):
    loss_category:     str | None   = None
    budgeted_loss_pct: float | None = Field(None, ge=0)
    budgeted_loss_kg:  float | None = Field(None, ge=0)
    actual_loss_kg:    float | None = Field(None, ge=0)
    variance_kg:       float | None = None
    remarks:           str | None   = None
    updated_by:        str


class RemarkPatchRequest(BaseModel):
    remark_type: str | None = None
    content:     str | None = None
    updated_by:  str


class AnnexureDeleteRequest(BaseModel):
    """Used for all 5 annexure DELETE endpoints. Body required to capture deleted_by."""
    deleted_by: str
