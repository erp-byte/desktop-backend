"""GST reconciliation schemas."""

from pydantic import BaseModel

from app.core.types import Decimal3


class GSTReconLineOut(BaseModel):
    so_line_id: int
    line_number: int | None = None
    sku_name: str | None = None
    expected_gst_rate: Decimal3 = None
    actual_gst_rate: Decimal3 = None
    expected_gst_amount: Decimal3 = None
    actual_gst_amount: Decimal3 = None
    gst_difference: Decimal3 = None
    gst_type: str | None = None
    gst_type_valid: bool | None = None
    sgst_cgst_equal: bool | None = None
    total_with_gst_valid: bool | None = None
    uom_match: bool | None = None
    item_type_flag: str | None = None
    rate_type: str | None = None
    matched_item_description: str | None = None
    matched_item_type: str | None = None
    matched_item_category: str | None = None
    matched_sub_category: str | None = None
    matched_sales_group: str | None = None
    matched_uom: Decimal3 = None
    match_score: Decimal3 = None
    status: str = "ok"
    notes: str | None = None


class GSTReconResponse(BaseModel):
    so_id: int
    total_lines: int
    ok_count: int
    mismatch_count: int
    warning_count: int
    lines: list[GSTReconLineOut]


class GSTReconSummary(BaseModel):
    total_sos: int
    total_lines: int
    ok_count: int
    mismatch_count: int
    warning_count: int
