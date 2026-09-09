"""Request/response models for Job Work Material Out.

The reference endpoint (`job_work_server.submit_material_out`) is declared
`payload: dict` — no model, no validation. Missing fields silently became `""`
or `0`, and a malformed `quantity` silently wrote a zero-weight challan. These
models close that hole WITHOUT breaking the wire: every shape the production
form sends still validates, because the aliases and the `_fold_*` validators
below reproduce the reference's fallback chains explicitly.

Two client shapes are accepted:
  • IMS  — nested `quantity: {kgs, boxes}`, `description`, root-level
           `challan_no` / `dated` / `motor_vehicle_no` / `remarks`
  • CFERP — flat `quantity_kgs` / `quantity_boxes`, `item_description`,
           everything on `header` (see web_replica/src/lib/jobWork.ts)

Unknown keys are preserved, not rejected: the reference stores the entire raw
body in `jb_materialout_header.payload`, and blocks like `company` / `totals` /
`tax_summary` have no columns and survive only there.
"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import (
    AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator)


def _first(*vals: Any) -> Any:
    """First value that is neither None nor blank.

    An `or`-chain, matching the reference exactly: an explicit "" falls through
    to the next candidate rather than winning. This is what makes
    `header.to_party = ""` resolve to `dispatch_to.name`.
    """
    for v in vals:
        if v is not None and v != "":
            return v
    return None


def _as_dict(v: Any) -> dict:
    """A sub-object as a plain dict, whether it arrived as JSON or as a model."""
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, BaseModel):
        return v.model_dump()
    return {}


# ── Party ────────────────────────────────────────────────────────────────────
class JobWorkParty(BaseModel):
    """The job worker. Stored both as typed party_* columns and whole as
    `jb_materialout_header.dispatch_to` JSONB."""

    model_config = ConfigDict(extra="allow")

    name: str = ""
    address: str = ""
    state: str = ""
    city: str = ""
    pin_code: str = ""
    contact_company: str = ""
    contact_mobile: str = ""
    email: str = ""
    sub_category: str = ""  # the process: De seeding / Dicing / Cracking / …


# ── Header ───────────────────────────────────────────────────────────────────
class JobWorkHeaderCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    challan_no: str = ""
    job_work_date: str = ""
    from_warehouse: str = ""
    to_party: str = ""
    party_address: str = ""
    contact_person: str = ""
    contact_number: str = ""
    purpose_of_work: str = ""
    expected_return_date: str = ""
    vehicle_no: str = ""
    driver_name: str = ""
    authorized_person: str = ""
    remarks: str = ""
    e_way_bill_no: str = ""
    dispatched_through: str = ""
    # `type` is accepted for wire compatibility and ignored — the INSERT writes
    # 'OUT'/'sent' literally, exactly as the reference does. There is no way to
    # create a record in another state through this endpoint.


# ── Line ─────────────────────────────────────────────────────────────────────
class JobWorkQuantity(BaseModel):
    """IMS nests quantity as an object. The reference read it with
    `isinstance(qty, dict)` and fell back to 0 for BOTH values when that failed —
    so `quantity: 5` silently produced a zero-weight, zero-box line. Typing it
    turns that into a 422."""

    kgs: float = 0.0
    boxes: int = 0


class JobWorkLineCreate(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sl_no: int = 0
    item_description: str = Field(
        default="", validation_alias=AliasChoices("item_description", "description"))
    material_type: str = ""
    item_category: str = ""   # blank → backfilled from all_sku, see create_service
    sub_category: str = ""

    quantity_kgs: float = 0.0
    quantity_boxes: int = 0

    rate_per_kg: float = 0.0
    amount: float = 0.0
    uom: str = ""

    # VARCHAR(20) in the shared schema, not NUMERIC. The form sends numbers for
    # some of these and strings for others; `_stringify_weights` normalises.
    case_pack: str = ""
    net_weight: str = ""
    total_weight: str = ""

    batch_number: str = ""
    lot_number: str = ""
    manufacturing_date: str = ""
    expiry_date: str = ""
    line_remarks: str = Field(
        default="", validation_alias=AliasChoices("remarks", "line_remarks"))

    item_mark: str = ""
    # All three are required together to deduct cold stock — see create_service.
    box_id: str = ""
    transaction_no: str = ""
    cold_unit: str = ""
    # Client-side copy of the cold row, used only when the live row has already
    # gone. Note the name: the field is `cold_stock_snapshot`, the column is
    # `cold_storage_snapshot`.
    cold_stock_snapshot: Optional[dict] = None

    @field_validator("case_pack", "net_weight", "total_weight", mode="before")
    @classmethod
    def _stringify_weights(cls, v: Any) -> Any:
        """Number -> str, preserving the literal the client sent.

        Declaring these `str | float` instead would let Pydantic widen an int to
        a float first, turning a pack size of `25` into the string `"25.0"` — a
        different value in a VARCHAR column that IMS reads back and displays.
        `str(v)` is what the reference does, so an int stays "25".
        """
        if v is None:
            return ""
        if isinstance(v, bool):        # bool is an int subclass; never a weight
            return str(v)
        if isinstance(v, (int, float)):
            return str(v)
        return v

    @model_validator(mode="before")
    @classmethod
    def _fold_quantity(cls, data: Any) -> Any:
        """Flatten IMS's nested `quantity: {kgs, boxes}` onto the flat fields.

        Unlike the reference, a `quantity` that is present but not an object is
        an error rather than a silent zero — writing a zero-weight challan is
        the more expensive outcome.
        """
        if not isinstance(data, dict) or "quantity" not in data:
            return data
        qty = data.get("quantity")
        if qty is None:
            return data
        if not isinstance(qty, dict):
            raise ValueError(
                "quantity must be an object like {\"kgs\": 12.5, \"boxes\": 3}; "
                f"got {type(qty).__name__}. Use quantity_kgs / quantity_boxes for scalars."
            )
        parsed = JobWorkQuantity.model_validate(qty)
        data = dict(data)
        # Nested wins only where the flat field was not supplied.
        data.setdefault("quantity_kgs", parsed.kgs)
        data.setdefault("quantity_boxes", parsed.boxes)
        return data


# ── Request ──────────────────────────────────────────────────────────────────
class JobWorkCreateRequest(BaseModel):
    """`extra="allow"` is load-bearing: `company`, `party`, `totals`,
    `tax_summary` and `document_type` have no columns and are round-tripped
    through the `payload` JSONB."""

    model_config = ConfigDict(extra="allow")

    header: JobWorkHeaderCreate = Field(default_factory=JobWorkHeaderCreate)
    dispatch_to: JobWorkParty = Field(default_factory=JobWorkParty)
    line_items: List[JobWorkLineCreate] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _fold_root_fallbacks(cls, data: Any) -> Any:
        """Fold IMS's root-level duplicates down onto `header` before validation.

        The production form sends the same value in two places (`challan_no` at
        the root AND on `header`), and two fields ONLY at the root
        (`e_way_bill_no`, `dispatched_through`). Resolving it here means the
        service reads one place and the fallback order is testable.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        # Values may arrive as dicts (the wire) or as model instances (Python
        # callers, e.g. tests); `_as_dict` flattens both.
        header = _as_dict(data.get("header"))
        party = _as_dict(data.get("dispatch_to")) or _as_dict(data.get("party"))

        for target, candidates in (
            ("challan_no",         (header.get("challan_no"), data.get("challan_no"))),
            ("job_work_date",      (header.get("job_work_date"), data.get("dated"))),
            ("vehicle_no",         (header.get("vehicle_no"), data.get("motor_vehicle_no"))),
            ("remarks",            (header.get("remarks"), data.get("remarks"))),
            ("e_way_bill_no",      (header.get("e_way_bill_no"), data.get("e_way_bill_no"))),
            ("dispatched_through", (header.get("dispatched_through"), data.get("dispatched_through"))),
            # Party details double as header columns when the header omits them.
            ("to_party",           (header.get("to_party"), party.get("name"))),
            ("party_address",      (header.get("party_address"), party.get("address"))),
            ("purpose_of_work",    (header.get("purpose_of_work"), party.get("sub_category"))),
        ):
            header[target] = _first(*candidates) or ""

        data["header"] = header
        # `party` is IMS's alias for dispatch_to; accept it when dispatch_to is absent.
        if not data.get("dispatch_to") and party:
            data["dispatch_to"] = party
        return data

    @model_validator(mode="after")
    def _require_essentials(self) -> "JobWorkCreateRequest":
        """The four things without which the challan is not a document. The
        reference accepted a completely empty body and wrote a blank row."""
        missing: list[str] = []
        if not self.header.challan_no.strip():
            missing.append("header.challan_no")
        if not self.header.from_warehouse.strip():
            missing.append("header.from_warehouse")
        if not (self.header.to_party.strip() or self.dispatch_to.name.strip()):
            missing.append("header.to_party (or dispatch_to.name)")
        if not self.line_items:
            missing.append("line_items (at least one)")
        if missing:
            raise ValueError("missing required field(s): " + ", ".join(missing))

        for idx, line in enumerate(self.line_items):
            if not line.item_description.strip():
                raise ValueError(
                    f"line_items[{idx}].item_description is required "
                    "(or send it as `description`)")
        return self


# ── Response ─────────────────────────────────────────────────────────────────
class JobWorkLineOut(BaseModel):
    id: int
    sl_no: Optional[int] = None
    item_description: Optional[str] = None
    material_type: Optional[str] = None
    item_category: Optional[str] = None
    sub_category: Optional[str] = None
    quantity_kgs: float = 0.0
    quantity_boxes: int = 0
    rate_per_kg: float = 0.0
    amount: float = 0.0
    uom: Optional[str] = None
    case_pack: Optional[str] = None
    net_weight: Optional[str] = None
    total_weight: Optional[str] = None
    batch_number: Optional[str] = None
    lot_number: Optional[str] = None
    manufacturing_date: Optional[str] = None
    expiry_date: Optional[str] = None
    line_remarks: Optional[str] = None
    cold_unit: Optional[str] = None
    item_mark: Optional[str] = None
    box_id: Optional[str] = None
    transaction_no: Optional[str] = None
    #: True when this line's cold row was found and removed from inventory by
    #: this dispatch (the snapshot is the proof of what left).
    cold_deducted: bool = False


class JobWorkRecordOut(BaseModel):
    id: int
    challan_no: str
    job_work_date: Optional[str] = None
    from_warehouse: Optional[str] = None
    to_party: Optional[str] = None
    party_address: Optional[str] = None
    party_state: Optional[str] = None
    party_city: Optional[str] = None
    party_pin_code: Optional[str] = None
    party_contact_company: Optional[str] = None
    party_contact_mobile: Optional[str] = None
    party_email: Optional[str] = None
    sub_category: Optional[str] = None
    contact_person: Optional[str] = None
    contact_number: Optional[str] = None
    purpose_of_work: Optional[str] = None
    expected_return_date: Optional[str] = None
    vehicle_no: Optional[str] = None
    driver_name: Optional[str] = None
    authorized_person: Optional[str] = None
    remarks: Optional[str] = None
    e_way_bill_no: Optional[str] = None
    dispatched_through: Optional[str] = None
    type: str = "OUT"
    status: str = "sent"
    dispatch_to: Optional[dict] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    lines: List[JobWorkLineOut] = Field(default_factory=list)
    #: How many cold-storage boxes this dispatch actually removed from inventory.
    #: Absent from the reference response — without it the caller cannot tell a
    #: fully-deducted dispatch from one whose boxes were already gone.
    cold_boxes_deducted: int = 0
