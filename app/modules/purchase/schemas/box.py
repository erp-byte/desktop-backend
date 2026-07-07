"""Box-level schema — individual box weights per article."""

from pydantic import BaseModel

from app.core.types import Decimal3


class BoxOut(BaseModel):
    box_id: str
    transaction_no: str
    line_number: int
    section_number: int
    box_number: int
    net_weight: Decimal3 = None
    gross_weight: Decimal3 = None
    lot_number: str | None = None
    count: int | None = None
