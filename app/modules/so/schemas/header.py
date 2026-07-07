"""SO header-level schemas."""

from pydantic import BaseModel

from app.modules.so.schemas.line import SOLineOut, SOLineInput, ManualUpdateLineInput
from app.modules.so.schemas.gst import GSTReconLineOut


class SOLineWithRecon(BaseModel):
    """Line item with its GST reconciliation result inline."""
    line: SOLineOut
    gst_recon: GSTReconLineOut
    # Set by the upload reconciler when an existing SO is re-ingested. None
    # for fresh inserts so callers can branch on presence.
    #   "new"       — article wasn't on file under this SO, just added
    #   "bumped"    — qty was increased to the incoming total
    #   "warning"   — incoming qty < existing; no change applied
    #   "unchanged" — incoming qty == existing; metadata may still differ
    reconcile_status: str | None = None
    reconcile_note: str | None = None
    qty_delta_kg: float | None = None      # delta against existing quantity_units (kg)
    qty_delta_units: float | None = None   # delta against existing quantity (pack count)


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
    # Reconcile-specific fields. False / 0 / [] for fresh SOs.
    was_existing: bool = False
    added_line_count: int = 0
    qty_bumped_count: int = 0
    qty_warning_count: int = 0


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
