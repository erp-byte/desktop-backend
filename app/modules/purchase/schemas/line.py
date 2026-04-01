"""PO line (article) schema — per-transaction line items with nested sections."""

from pydantic import BaseModel

from app.core.types import Decimal3
from app.modules.purchase.schemas.section import SectionOut


class POLineOut(BaseModel):
    transaction_no: str
    line_number: int
    # Purchase Team (Excel + matched)
    sku_name: str | None = None
    uom: str | None = None
    pack_count: int | None = None
    po_weight: Decimal3 = None
    rate: Decimal3 = None
    amount: Decimal3 = None
    particulars: str | None = None
    item_category: str | None = None
    sub_category: str | None = None
    item_type: str | None = None
    sales_group: str | None = None
    gst_rate: Decimal3 = None
    match_score: Decimal3 = None
    match_source: str | None = None
    # Stores Team
    carton_weight: Decimal3 = None
    # System
    status: str = "pending"
    total_sections: int = 0
    total_boxes: int = 0
    sections: list[SectionOut] = []
