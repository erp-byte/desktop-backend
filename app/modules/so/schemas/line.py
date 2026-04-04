"""Line-level schemas for SO module."""

from pydantic import BaseModel

from app.core.types import Decimal3, Decimal3Z


class SOLineOut(BaseModel):
    so_line_id: int
    line_number: int
    sku_name: str | None = None
    item_category: str | None = None
    sub_category: str | None = None
    uom: str | None = None
    grp_code: str | None = None
    quantity: Decimal3 = None
    quantity_units: int | None = None
    rate_inr: Decimal3 = None
    rate_type: str | None = None
    amount_inr: Decimal3 = None
    igst_amount: Decimal3 = None
    sgst_amount: Decimal3 = None
    cgst_amount: Decimal3 = None
    total_amount_inr: Decimal3 = None
    apmc_amount: Decimal3 = None
    packing_amount: Decimal3 = None
    freight_amount: Decimal3 = None
    processing_amount: Decimal3 = None
    item_type: str | None = None
    item_description: str | None = None
    sales_group: str | None = None
    match_score: Decimal3 = None
    match_source: str | None = None
    status: str = "pending"


class SOLineInput(BaseModel):
    """Single article line for manual SO entry."""
    sku_name: str
    item_category: str | None = None
    sub_category: str | None = None
    uom: str | None = None
    grp_code: str | None = None
    quantity: Decimal3 = None
    quantity_units: int | None = None
    rate_inr: Decimal3 = None
    amount_inr: Decimal3 = None
    igst_amount: Decimal3Z = 0
    sgst_amount: Decimal3Z = 0
    cgst_amount: Decimal3Z = 0
    apmc_amount: Decimal3Z = 0
    packing_amount: Decimal3Z = 0
    freight_amount: Decimal3Z = 0
    processing_amount: Decimal3Z = 0
    total_amount_inr: Decimal3 = None


class ManualUpdateLineInput(BaseModel):
    """Line fields for manual update. All numeric fields are nullable to preserve DB nulls."""
    line_number: int
    sku_name: str | None = None
    item_category: str | None = None
    sub_category: str | None = None
    uom: str | None = None
    grp_code: str | None = None
    quantity: Decimal3 = None
    quantity_units: int | None = None
    rate_inr: Decimal3 = None
    amount_inr: Decimal3 = None
    igst_amount: Decimal3 = None
    sgst_amount: Decimal3 = None
    cgst_amount: Decimal3 = None
    apmc_amount: Decimal3 = None
    packing_amount: Decimal3 = None
    freight_amount: Decimal3 = None
    processing_amount: Decimal3 = None
    total_amount_inr: Decimal3 = None
