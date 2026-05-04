"""PO header (transaction) schema — purchase order with nested lines."""

from pydantic import BaseModel

from app.core.types import Decimal3
from app.modules.purchase.schemas.line import POLineOut


class POHeaderOut(BaseModel):
    transaction_no: str
    entity: str
    # Purchase Team (Excel)
    po_date: str | None = None
    voucher_type: str | None = None
    po_number: str | None = None
    order_reference_no: str | None = None
    narration: str | None = None
    vendor_supplier_name: str | None = None
    gross_total: Decimal3 = None
    total_amount: Decimal3 = None
    sgst_amount: Decimal3 = None
    cgst_amount: Decimal3 = None
    igst_amount: Decimal3 = None
    round_off: Decimal3 = None
    freight_transport_local: Decimal3 = None
    apmc_tax: Decimal3 = None
    packing_charges: Decimal3 = None
    freight_transport_charges: Decimal3 = None
    loading_unloading_charges: Decimal3 = None
    other_charges_non_gst: Decimal3 = None
    # Stores Team
    customer_party_name: str | None = None
    vehicle_number: str | None = None
    transporter_name: str | None = None
    lr_number: str | None = None
    source_location: str | None = None
    challan_number: str | None = None
    invoice_number: str | None = None
    grn_number: str | None = None
    system_grn_date: str | None = None
    purchased_by: str | None = None
    inward_authority: str | None = None
    warehouse: str | None = None
    # System
    status: str = "pending"
    approved_by: str | None = None
    approved_at: str | None = None
    total_lines: int = 0
    total_boxes: int = 0
    lines: list[POLineOut] = []
