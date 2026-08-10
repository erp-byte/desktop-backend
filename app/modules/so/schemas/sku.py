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


class SKUBulkRow(BaseModel):
    """One all_sku row, flat.

    SKULookupResponse.options is four INDEPENDENT distinct arrays, so the
    name -> group -> pack-weight association is lost and no single call can
    rebuild it. Consumers that need the pairing (client-side category
    grouping, net-kg conversion) read these rows instead.

    `uom` is NOT a unit string — it is kg per transacted unit of the SKU,
    NUMERIC(15,3) in the master. NULL and 0 both mean "no pack weight".
    """

    particulars: str
    item_type: str | None = None
    item_group: str | None = None
    sub_group: str | None = None
    uom: Decimal3 = None
    sale_group: str | None = None
    sfg_code: str | None = None


class SKUDropdownOptions(BaseModel):
    item_types: list[str]
    particulars: list[str]
    item_groups: list[str]
    sub_groups: list[str]
    sales_groups: list[str]


class SKULookupResponse(BaseModel):
    options: SKUDropdownOptions
    selected_item: SKUDetail | None = None
