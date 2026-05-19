"""Schemas for SO update (preview + confirm + manual) endpoints."""

from pydantic import BaseModel

from app.modules.so.schemas.header import SODetail, ManualUpdateHeaderInput
from app.modules.so.schemas.line import ManualUpdateLineInput


class FieldChange(BaseModel):
    """A single field that changed between DB and Excel."""
    field: str
    old_value: str | None = None
    new_value: str | None = None


class LineChange(BaseModel):
    """A line item with its changes."""
    line_number: int
    sku_name: str | None = None
    change_type: str  # "modified", "added", "removed"
    changes: list[FieldChange]


class HeaderChange(BaseModel):
    """Header-level field changes."""
    field: str
    old_value: str | None = None
    new_value: str | None = None


class SOUpdateDiff(BaseModel):
    """Diff for a single SO: current state vs Excel state."""
    so_id: int
    so_number: str
    header_changes: list[HeaderChange]
    line_changes: list[LineChange]
    current_header: dict
    new_header: dict
    current_lines: list[dict]
    new_lines: list[dict]


class SOUpdatePreviewResponse(BaseModel):
    """Preview of all SOs with changes — only changed SOs are included."""
    file_hash: str = ""
    total_in_file: int
    unchanged_count: int
    changed_count: int
    not_found_so_numbers: list[str]
    changes: list[SOUpdateDiff]


class SOUpdateConfirmRequest(BaseModel):
    """Confirm update for specific SOs by so_id."""
    so_ids: list[int]


class SOUpdateConfirmResponse(BaseModel):
    """Result of confirmed updates."""
    updated_count: int
    updated_so_numbers: list[str]
    sales_orders: list[SODetail]


class SOManualUpdateRequest(BaseModel):
    """Manual update for a single SO — frontend sends old + new state."""
    so_number: str
    old_header: ManualUpdateHeaderInput
    new_header: ManualUpdateHeaderInput
    old_lines: list[ManualUpdateLineInput]
    new_lines: list[ManualUpdateLineInput]


class SOManualUpdateResponse(BaseModel):
    """Result of a single manual SO update."""
    so_id: int
    so_number: str
    header_changes: list[HeaderChange]
    line_changes: list[LineChange]
    sales_order: SODetail
