"""SKU lookup (cascading dropdown) schemas."""

from pydantic import BaseModel

from app.core.types import Decimal3


class SKUDetail(BaseModel):
    sku_id: int
    particulars: str
    item_type: str | None = None
    item_group: str | None = None
    sub_group: str | None = None
    uom: Decimal3 = None
    sale_group: str | None = None
    gst: Decimal3 = None


class SKUDropdownOptions(BaseModel):
    item_types: list[str]
    particulars: list[str]
    item_groups: list[str]
    sub_groups: list[str]
    sales_groups: list[str]


class SKULookupResponse(BaseModel):
    options: SKUDropdownOptions
    selected_item: SKUDetail | None = None
