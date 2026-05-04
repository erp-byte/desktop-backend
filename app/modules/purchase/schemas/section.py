"""Section-level schema — lot grouping with nested boxes per article."""

from pydantic import BaseModel

from app.modules.purchase.schemas.box import BoxOut


class SectionOut(BaseModel):
    transaction_no: str
    line_number: int
    section_number: int
    lot_number: str | None = None
    box_count: int | None = None
    manufacturing_date: str | None = None
    expiry_date: str | None = None
    total_boxes: int = 0
    boxes: list[BoxOut] = []
