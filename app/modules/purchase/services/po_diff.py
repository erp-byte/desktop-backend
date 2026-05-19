"""Header diff + warning helpers for /preview.

Produces:
    • a `diff` dict { field: {"old": ..., "new": ...} } for changed fields
    • a `warnings` list of human-readable strings (e.g. rate variance vs last PO)

Pure functions — no DB access. Callers (preview service) supply the existing
header dict if one was found in the DB, plus the incoming dict from Excel.
"""

from __future__ import annotations

from typing import Any


# Fields we surface in the header diff. Stores-team fields (vehicle_number,
# transporter_name, etc.) are intentionally excluded — those belong to the
# /receive flow, not the /preview flow.
_DIFFABLE_HEADER_FIELDS: tuple[str, ...] = (
    "po_number",
    "po_date",
    "voucher_type",
    "order_reference_no",
    "narration",
    "vendor_supplier_name",
    "supplier_id",
    "gross_total",
    "total_amount",
    "sgst_amount",
    "cgst_amount",
    "igst_amount",
    "round_off",
    "freight_transport_local",
    "apmc_tax",
    "packing_charges",
    "freight_transport_charges",
    "loading_unloading_charges",
    "other_charges_non_gst",
)


def _normalise(v: Any) -> Any:
    """Cheap value normalisation for diff comparison. Floats lose precision
    quirks; None and empty string compare equal."""
    if v is None or v == "":
        return None
    if isinstance(v, float):
        return round(v, 4)
    return v


def header_diff(existing: dict | None, incoming: dict) -> dict[str, dict[str, Any]] | None:
    """Return {field: {old, new}} for fields whose normalised values differ.
    Returns None if `existing` is None (no prior PO) or no fields changed."""
    if existing is None:
        return None
    out: dict[str, dict[str, Any]] = {}
    for f in _DIFFABLE_HEADER_FIELDS:
        old = _normalise(existing.get(f))
        new = _normalise(incoming.get(f))
        if old != new:
            out[f] = {"old": existing.get(f), "new": incoming.get(f)}
    return out or None


# ── Per-line warnings ────────────────────────────────────────────────────


_RATE_VARIANCE_THRESHOLD = 0.10  # 10% — flag if rate moved by more than this


def line_warnings(incoming_lines: list[dict], last_seen_rates: dict[str, float]) -> list[str]:
    """Generate human-readable warnings.

    Args:
        incoming_lines: list of line dicts from the parser (with `sku_name`, `rate`, `line_number`).
        last_seen_rates: dict[sku_name → most-recent rate from any prior PO]
                         Pass {} if no history available — function returns no warnings.
    """
    out: list[str] = []
    for line in incoming_lines:
        sku = line.get("sku_name")
        rate = line.get("rate")
        if not sku or rate is None:
            continue
        prior = last_seen_rates.get(sku)
        if prior is None or prior == 0:
            continue
        delta = abs(rate - prior) / prior
        if delta >= _RATE_VARIANCE_THRESHOLD:
            pct = round(delta * 100)
            ln = line.get("line_number", "?")
            out.append(f"Line {ln}: rate differs from last PO by {pct}%")
    return out
