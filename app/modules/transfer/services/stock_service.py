"""Receive-side helper (P8b-2) and the asyncpg coercions the transfer services share.

RECORD ONLY. The stock tables ({cfpl,cdpl}_boxes_v2, _bulk_entry_boxes, _cold_stocks)
are kept by IMS; the inter-unit transfer (dispatch, receipt and every undo) never
INSERTs, UPDATEs or DELETEs a row in them — only the separate inner cold transfer
(inner_cold_service) edits cold-stock lot / location, on purpose. pick_from_pending (a port of pending_stock_tools.pick_from_pending) therefore only
clears a received transfer's in-transit rows (pending_transfer_stock). The reference
also posted cold_stocks rows there and, as a legacy fallback, from the client's
cold_storage_items (interunit_tools._insert_cold_storage_items); neither is ported.

asyncpg is strict about column types (the reference relied on psycopg2's implicit
casting), so numeric columns are coerced to Decimal and date columns to date here
(_dec / _date), for the services that write transfer records.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def _dec(v):
    """Coerce to Decimal for a NUMERIC column, or None."""
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date(v):
    """Coerce to a date for a DATE column, or None (tolerates several formats)."""
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"))


def _rows_affected(tag) -> int:
    """The row count of an asyncpg status tag ('DELETE 3' -> 3; anything else -> 0)."""
    last = (tag or "").split()[-1:] or ["0"]
    return int(last[0]) if last[0].isdigit() else 0


async def pick_from_pending(conn, transfer_out_id: int, box_ids: set | None = None, *,
                            row_ids: list | None = None) -> int:
    """Clear the 'In Transit' pending rows of a received transfer-out. When `row_ids` (the
    pending rows' ids) or `box_ids` is given only those rows / boxes are cleared (the rest
    stay In Transit — close-with-shortage clears the received subset, matched box by box,
    and writes the shortfall off); otherwise every In-Transit row is (the full-finalize
    path). Returns the count cleared.

    Record only: the pending row is deleted and nothing is inserted into a stock table
    (the reference posted the box to its destination cold_stocks here; IMS keeps stock
    now). The interunit_transfer_in_boxes rows are the record of what arrived."""
    if row_ids is not None:
        tag = await conn.execute(
            "DELETE FROM pending_transfer_stock WHERE transfer_out_id = $1 "
            "AND status = 'In Transit' AND id = ANY($2::bigint[])",
            transfer_out_id, list(row_ids),
        )
    elif box_ids is not None:
        tag = await conn.execute(
            "DELETE FROM pending_transfer_stock WHERE transfer_out_id = $1 "
            "AND status = 'In Transit' AND box_id = ANY($2::text[])",
            transfer_out_id, list(box_ids),
        )
    else:
        tag = await conn.execute(
            "DELETE FROM pending_transfer_stock WHERE transfer_out_id = $1 AND status = 'In Transit'",
            transfer_out_id,
        )
    return _rows_affected(tag)
