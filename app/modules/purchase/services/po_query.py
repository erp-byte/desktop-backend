"""GET /api/v1/po (list) + /{txn} (detail) + /{txn}/lines query helpers.

WHERE-clause builder for the rich filter set:
    • Equality: transaction_no, entity, po_number, voucher_type,
      order_reference_no, supplier_id, vendor_supplier_name
    • ILIKE contains: po_number_contains, order_reference_no_contains,
      vendor_supplier_name_contains, narration_contains
    • Date range: po_date_from, po_date_to
    • Numeric ranges (_min/_max) on every monetary column
    • Visibility: include_deleted toggle
    • Pagination + sort
    • Entity scope for non-admin actors

All filter values are bound as parameters — no f-string user input. Column
names come from a hardcoded allowlist (`_NUMERIC_RANGE_FIELDS`).
"""

from __future__ import annotations

from datetime import date as date_type
from typing import Any

from app.core.middleware.request_context import AuthError


_NUMERIC_RANGE_FIELDS: tuple[str, ...] = (
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

_EQUALITY_FIELDS: tuple[str, ...] = (
    "transaction_no",
    "entity",
    "po_number",
    "voucher_type",
    "order_reference_no",
    "supplier_id",
    "vendor_supplier_name",
)

_ILIKE_FIELDS: dict[str, str] = {
    "po_number_contains": "po_number",
    "order_reference_no_contains": "order_reference_no",
    "vendor_supplier_name_contains": "vendor_supplier_name",
    "narration_contains": "narration",
}

_SORTABLE_COLUMNS: frozenset[str] = frozenset({
    "po_date", "po_number", "vendor_supplier_name", "supplier_id",
    "gross_total", "total_amount", "transaction_no", "created_at",
    # Also-projected columns the PO list UI now offers as sort keys.
    "voucher_type", "order_reference_no", "entity",
})

# "Fully received" tolerance — received net weight must reach at least this
# fraction of the ordered po_weight to count as matched (1% shortfall allowed for
# weighing/moisture drift). Kept in one place so the section filter and the
# receipt-summary endpoint apply the exact same rule.
_WEIGHT_MATCH_FACTOR = 0.99

# A transaction is "received" (→ the Material In "Completed" section) as soon as
# the Stores team enters ANY box for it — the dashboard moves it there on the
# first box change. How much was received (partially vs extra) is a status label
# on po_header, refreshed on every box change (router._refresh_receiving_status).
_RECEIVED_PREDICATE = (
    "EXISTS (SELECT 1 FROM po_box b WHERE b.transaction_no = po_header.transaction_no)"
)

_VALID_SECTIONS: frozenset[str] = frozenset({"today", "pending", "completed"})


def _parse_date(s: str | None, label: str) -> date_type | None:
    if not s:
        return None
    try:
        return date_type.fromisoformat(s)
    except (ValueError, TypeError):
        raise AuthError(
            "validation_error",
            f"Invalid {label}: expected YYYY-MM-DD, got {s!r}",
            400,
        )


def parse_sort(raw: str | None) -> tuple[str, str]:
    """Parse a `field:dir` string into (column, direction). Raises 400 on bad input."""
    if not raw:
        return "po_date", "DESC"
    parts = raw.split(":", 1)
    col = parts[0].strip()
    direction = (parts[1].strip().upper() if len(parts) == 2 else "DESC")
    if col not in _SORTABLE_COLUMNS:
        raise AuthError(
            "validation_error",
            f"Cannot sort by {col!r}. Allowed: {sorted(_SORTABLE_COLUMNS)}",
            400,
        )
    if direction not in ("ASC", "DESC"):
        raise AuthError(
            "validation_error",
            f"Sort direction must be asc or desc, got {direction!r}",
            400,
        )
    return col, direction


def build_where(
    *,
    filters: dict[str, Any],
    include_deleted: bool,
    entity_scope: list[str] | None,
) -> tuple[str, list[Any]]:
    """Return (where_sql, params).

    `entity_scope`: None means "no scope restriction" (admin); a list means
    the user can only see those entities (overrides any user-supplied
    entity= filter that's outside the list).
    """
    conditions: list[str] = []
    params: list[Any] = []
    idx = 0

    def _add(sql_template: str, *vals: Any) -> None:
        """Append a condition with N positional placeholders.
        sql_template uses ${i} placeholders; we substitute the live indices."""
        nonlocal idx
        substituted = sql_template
        for v in vals:
            idx += 1
            substituted = substituted.replace("${i}", f"${idx}", 1)
            params.append(v)
        conditions.append(substituted)

    # Equality
    for f in _EQUALITY_FIELDS:
        v = filters.get(f)
        if v is not None and v != "":
            _add(f"{f} = ${{i}}", v.lower() if f == "entity" else v)

    # ILIKE contains
    for qparam, col in _ILIKE_FIELDS.items():
        v = filters.get(qparam)
        if v:
            _add(f"{col} ILIKE ${{i}}", f"%{v}%")

    # Date range
    df = _parse_date(filters.get("po_date_from"), "po_date_from")
    dt = _parse_date(filters.get("po_date_to"), "po_date_to")
    if df and dt and df > dt:
        raise AuthError(
            "validation_error",
            "po_date_from must be on or before po_date_to",
            400,
        )
    if df:
        _add("po_date >= ${i}", df)
    if dt:
        _add("po_date <= ${i}", dt)

    # Numeric ranges
    for f in _NUMERIC_RANGE_FIELDS:
        lo = filters.get(f"{f}_min")
        hi = filters.get(f"{f}_max")
        if lo is not None and hi is not None and lo > hi:
            raise AuthError(
                "validation_error",
                f"{f}_min must be <= {f}_max",
                400,
            )
        if lo is not None:
            _add(f"{f} >= ${{i}}", lo)
        if hi is not None:
            _add(f"{f} <= ${{i}}", hi)

    # Material In dashboard section partition (today / pending / completed).
    #   today     — po_date is the client's local today, whatever the receipt state
    #   completed — NOT today AND any box has been received (partially/extra)
    #   pending   — NOT today AND no box received yet
    # The three are mutually exclusive (Today takes priority), matching the
    # client's partition, so each section paginates independently server side.
    section = filters.get("section")
    if section:
        section = str(section).lower()
        if section not in _VALID_SECTIONS:
            raise AuthError(
                "validation_error",
                f"Invalid section {section!r}; expected today|pending|completed",
                400,
            )
        today = _parse_date(filters.get("today_date"), "today_date")
        if section == "today":
            if today is not None:
                _add("po_date = ${i}", today)
            else:
                conditions.append("po_date = CURRENT_DATE")
        else:
            if today is not None:
                _add("po_date IS DISTINCT FROM ${i}", today)
            else:
                conditions.append("po_date IS DISTINCT FROM CURRENT_DATE")
            conditions.append(
                f"({_RECEIVED_PREDICATE})"
                if section == "completed"
                else f"NOT ({_RECEIVED_PREDICATE})"
            )

    # Visibility
    if not include_deleted:
        conditions.append("deleted_at IS NULL")

    # Entity scope (admin → None; non-admin → restricted to allowed list)
    if entity_scope is not None:
        if not entity_scope:
            # Empty scope → see nothing
            conditions.append("FALSE")
        else:
            idx += 1
            conditions.append(f"entity = ANY(${idx}::text[])")
            params.append(list(entity_scope))

    where_sql = " AND ".join(conditions) if conditions else "TRUE"
    return where_sql, params


def to_iso_date(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def to_iso_ts(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def header_row_to_listitem(row) -> dict:
    """Project a po_header asyncpg.Record into the PoListItem dict shape."""
    def _f(v):
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "transaction_no": row["transaction_no"],
        "entity": row["entity"],
        "po_number": row["po_number"],
        "po_date": to_iso_date(row["po_date"]),
        "voucher_type": row["voucher_type"],
        "order_reference_no": row["order_reference_no"],
        "narration": row["narration"],
        "vendor_supplier_name": row["vendor_supplier_name"],
        "supplier_id": row["supplier_id"] if "supplier_id" in row.keys() else None,
        "status": row["status"] if "status" in row.keys() else None,
        "gross_total": _f(row["gross_total"]),
        "total_amount": _f(row["total_amount"]),
        "sgst_amount": _f(row["sgst_amount"]),
        "cgst_amount": _f(row["cgst_amount"]),
        "igst_amount": _f(row["igst_amount"]),
        "round_off": _f(row["round_off"]),
        "freight_transport_local": _f(row["freight_transport_local"]),
        "apmc_tax": _f(row["apmc_tax"]),
        "packing_charges": _f(row["packing_charges"]),
        "freight_transport_charges": _f(row["freight_transport_charges"]),
        "loading_unloading_charges": _f(row["loading_unloading_charges"]),
        "other_charges_non_gst": _f(row["other_charges_non_gst"]),
        "deleted_at": to_iso_ts(row["deleted_at"]) if "deleted_at" in row.keys() else None,
    }


def line_row_to_dict(row) -> dict:
    def _f(v):
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "transaction_no": row["transaction_no"],
        "line_number": row["line_number"],
        "sku_name": row["sku_name"],
        "uom": row["uom"],
        "pack_count": int(row["pack_count"]) if row["pack_count"] is not None else None,
        "po_weight": _f(row["po_weight"]),
        "rate": _f(row["rate"]),
        "amount": _f(row["amount"]),
        "particulars": row["particulars"],
        "item_category": row["item_category"],
        "sub_category": row["sub_category"],
        "item_type": row["item_type"],
        "sales_group": row["sales_group"],
        "gst_rate": _f(row["gst_rate"]),
        "match_score": _f(row["match_score"]),
        "match_source": row["match_source"] if "match_source" in row.keys() else None,
    }
