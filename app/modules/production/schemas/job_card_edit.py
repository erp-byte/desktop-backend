# app/modules/production/schemas/job_card_edit.py
"""Request schemas for PATCH and DELETE on job cards and annexures.

Every PATCH model has all fields Optional with default=None. The router
converts the request body via `body.model_dump(exclude_unset=True)` so only
fields the client actually sent appear in the dict — that is what guarantees
the 'preserve unspecified columns' behavior.
"""

from typing import Optional, List
from pydantic import BaseModel, Field


class JobCardPatchRequest(BaseModel):
    machine_id:              Optional[int]       = None
    assigned_to_team_leader: Optional[str]       = None
    team_members:            Optional[List[str]] = None
    factory:                 Optional[str]       = None
    floor:                   Optional[str]       = None
    customer_name:           Optional[str]       = None
    batch_number:            Optional[str]       = None
    batch_size_kg:           Optional[float]     = Field(None, gt=0)
    bom_id:                  Optional[int]       = None
    process_name:            Optional[str]       = None
    stage:                   Optional[str]       = None
    updated_by:              str


class JobCardCancelRequest(BaseModel):
    cancellation_reason: str = Field(..., min_length=3)
    deleted_by:          str


class EnvironmentPatchRequest(BaseModel):
    parameter_name: Optional[str] = None
    value:          Optional[str] = None
    updated_by:     str


class MetalDetectionPatchRequest(BaseModel):
    check_type:   Optional[str]  = None
    fe_pass:      Optional[bool] = None
    nfe_pass:     Optional[bool] = None
    ss_pass:      Optional[bool] = None
    failed_units: Optional[int]  = Field(None, ge=0)
    remarks:      Optional[str]  = None
    updated_by:   str


class WeightCheckPatchRequest(BaseModel):
    sample_number:  Optional[int]   = Field(None, gt=0)
    net_weight:     Optional[float] = Field(None, ge=0)
    gross_weight:   Optional[float] = Field(None, ge=0)
    leak_test_pass: Optional[bool]  = None
    updated_by:     str


class LossReconciliationPatchRequest(BaseModel):
    loss_category:     Optional[str]   = None
    budgeted_loss_pct: Optional[float] = Field(None, ge=0)
    budgeted_loss_kg:  Optional[float] = Field(None, ge=0)
    actual_loss_kg:    Optional[float] = Field(None, ge=0)
    variance_kg:       Optional[float] = None
    remarks:           Optional[str]   = None
    updated_by:        str


class RemarkPatchRequest(BaseModel):
    remark_type: Optional[str] = None
    content:     Optional[str] = None
    updated_by:  str


class AnnexureDeleteRequest(BaseModel):
    """Used for all 5 annexure DELETE endpoints. Body required to capture deleted_by."""
    deleted_by: str
