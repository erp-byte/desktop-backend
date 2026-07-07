"""Stock-posting core for the receive flow (P8b-2) — the IRREVERSIBLE part.

Ported from pending_stock_tools.pick_from_pending and
interunit_tools._insert_cold_storage_items. These move inventory:
pick_from_pending writes destination cold_stocks rows and deletes the matching
pending_transfer_stock rows; the legacy fallback inserts cold_stocks directly.

asyncpg is strict about column types (the reference relied on psycopg2's implicit
casting), so numeric columns are coerced to Decimal and date columns to date here.
Destination table names are whitelisted (never interpolated from arbitrary input).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Only these destination tables get a cold_stocks INSERT. Any other destination
# (e.g. a warehouse boxes table) just has its pending row deleted — the
# interunit_transfer_in_boxes records are the final state for warehouse dests.
_COLD_TABLES = {"cfpl_cold_stocks", "cdpl_cold_stocks"}


def _json(v):
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return {}


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


_COLD_INSERT_COLS = (
    "inward_dt, unit, inward_no, item_description, item_mark, vakkal, lot_no, "
    "no_of_cartons, weight_kg, total_inventory_kgs, group_name, item_subgroup, "
    "storage_location, exporter, last_purchase_rate, value, box_id, transaction_no, spl_remarks"
)
_COLD_INSERT_PH = ", ".join(f"${i}" for i in range(1, 20))


async def pick_from_pending(conn, transfer_out_id: int, challan_no_for_inward: str | None = None,
                            box_ids: set | None = None) -> int:
    """Move 'In Transit' pending rows for this transfer-out into their destination
    cold_stocks table, then delete the pending row. Returns count. When `box_ids`
    is given, only those boxes are posted (the rest stay In Transit — used by
    close-with-shortage to post the received subset and leave the shortfall to be
    written off); otherwise every In-Transit row is posted (the full-finalize path)."""
    if box_ids is not None:
        pending = await conn.fetch(
            "SELECT * FROM pending_transfer_stock WHERE transfer_out_id = $1 "
            "AND status = 'In Transit' AND box_id = ANY($2::text[])",
            transfer_out_id, list(box_ids),
        )
    else:
        pending = await conn.fetch(
            "SELECT * FROM pending_transfer_stock WHERE transfer_out_id = $1 AND status = 'In Transit'",
            transfer_out_id,
        )
    picked = 0
    for prow in pending:
        p = dict(prow)
        dest = p.get("destination_table") or ""
        if dest in _COLD_TABLES and await _table_exists(conn, dest):
            cj = _json(p.get("cold_storage_data"))
            tot_inv = cj.get("total_inventory_kgs")
            if tot_inv is None:
                tot_inv = float(p.get("weight_kg") or 0)
            await conn.execute(
                f"INSERT INTO {dest} ({_COLD_INSERT_COLS}) VALUES ({_COLD_INSERT_PH}) ON CONFLICT DO NOTHING",
                _date(cj.get("inward_dt")),
                cj.get("unit") or p.get("to_site"),
                challan_no_for_inward or cj.get("inward_no") or p.get("transfer_out_challan_no"),
                p.get("item_description"),
                cj.get("item_mark"),
                cj.get("vakkal"),
                p.get("lot_no"),
                _dec(p.get("no_of_cartons") or 1),
                _dec(p.get("weight_kg")),
                _dec(tot_inv),
                cj.get("group_name"),
                cj.get("item_subgroup"),
                cj.get("storage_location") or p.get("to_site"),
                cj.get("exporter"),
                _dec(cj.get("last_purchase_rate")),
                _dec(cj.get("value")),
                p.get("box_id"),
                p.get("transaction_no"),
                cj.get("spl_remarks"),
            )
        await conn.execute("DELETE FROM pending_transfer_stock WHERE id = $1", p["id"])
        picked += 1
    return picked


_TABLE_BY_COMPANY = {"cfpl": "cfpl_cold_stocks", "cdpl": "cdpl_cold_stocks"}


def _g(item, key):
    """Read a field from a cold-storage item that may be a dict or a model."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


async def insert_cold_storage_items(conn, header_id: int, items, challan_no: str, to_site: str | None = None) -> None:
    """Legacy fallback (finalize/create when no pending rows exist): insert
    cold-storage items straight into cfpl/cdpl_cold_stocks. Mirrors
    interunit_tools._insert_cold_storage_items."""
    cols_with_box = _COLD_INSERT_COLS
    ph_with_box = _COLD_INSERT_PH
    cols_no_box = (
        "inward_dt, unit, inward_no, item_description, item_mark, vakkal, lot_no, "
        "no_of_cartons, weight_kg, total_inventory_kgs, group_name, item_subgroup, "
        "storage_location, exporter, last_purchase_rate, value, spl_remarks"
    )
    ph_no_box = ", ".join(f"${i}" for i in range(1, 18))

    for cs in items or []:
        company = (_g(cs, "cold_company") or "").strip().lower()
        tbl = _TABLE_BY_COMPANY.get(company)
        if not tbl:
            continue
        rate = _g(cs, "rate") or 0
        box_details = _g(cs, "box_details")
        storage = _g(cs, "storage_location") or to_site
        if box_details:
            for bd in box_details:
                bw = float(_g(bd, "weight_kg") or 0)
                value_per_box = (bw * rate) if (rate and bw) else None
                await conn.execute(
                    f"INSERT INTO {tbl} ({cols_with_box}) VALUES ({ph_with_box})",
                    _date(_g(cs, "inward_dt")), storage, challan_no,
                    _g(cs, "item_description"), _g(cs, "item_mark"), _g(cs, "vakkal"), _g(cs, "lot_no"),
                    _dec(1), _dec(_g(bd, "weight_kg")), _dec(_g(bd, "weight_kg")),
                    _g(cs, "group_name"), _g(cs, "item_subgroup"), storage,
                    _g(cs, "exporter"), _dec(rate or None), _dec(value_per_box),
                    _g(bd, "box_id"), _g(bd, "transaction_no"), _g(cs, "spl_remarks"),
                )
        else:
            await conn.execute(
                f"INSERT INTO {tbl} ({cols_no_box}) VALUES ({ph_no_box})",
                _date(_g(cs, "inward_dt")), storage, challan_no,
                _g(cs, "item_description"), _g(cs, "item_mark"), _g(cs, "vakkal"), _g(cs, "lot_no"),
                _dec(_g(cs, "no_of_cartons")), _dec(_g(cs, "weight_kg")), _dec(_g(cs, "weight_kg")),
                _g(cs, "group_name"), _g(cs, "item_subgroup"), storage,
                _g(cs, "exporter"), _dec(rate or None), _dec(_g(cs, "value")), _g(cs, "spl_remarks"),
            )
