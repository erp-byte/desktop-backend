"""SO header-level schemas."""

from pydantic import BaseModel

from app.modules.so.schemas.line import SOLineOut, SOLineInput, ManualUpdateLineInput
from app.modules.so.schemas.gst import GSTReconLineOut


class SOLineWithRecon(BaseModel):
    """Line item with its GST reconciliation result inline."""
    line: SOLineOut
    gst_recon: GSTReconLineOut


class SODetail(BaseModel):
    """Full SO with all lines and their reconciliation."""
    so_id: int
    so_number: str | None = None
    so_date: str | None = None
    customer_name: str | None = None
    common_customer_name: str | None = None
    company: str | None = None
    voucher_type: str | None = None
    total_lines: int
    gst_ok: int
    gst_mismatch: int
    gst_warning: int
    lines: list[SOLineWithRecon]


class SOHeaderOut(BaseModel):
    so_id: int
    so_number: str | None = None
    so_date: str | None = None
    customer_name: str | None = None
    common_customer_name: str | None = None
    company: str | None = None
    voucher_type: str | None = None
    total_lines: int
    lines: list[SOLineOut]


class SOCreateRequest(BaseModel):
    """Manual SO creation with multiple articles."""
    so_number: str
    so_date: str | None = None
    customer_name: str | None = None
    common_customer_name: str | None = None
    company: str | None = None
    voucher_type: str | None = None
    lines: list[SOLineInput]


class ManualUpdateHeaderInput(BaseModel):
    """Header fields for manual update."""
    so_date: str | None = None
    customer_name: str | None = None
    common_customer_name: str | None = None
    company: str | None = None
    voucher_type: str | None = None
