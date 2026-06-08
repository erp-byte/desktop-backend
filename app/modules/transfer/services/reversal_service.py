"""Destructive reversal + backfill (P3b), ported from pending_stock_tools.py:
  - restore_to_source  : pending rows → back to source table, delete pending
  - unpick_to_pending  : reverse a receive (delete from destination, re-park pending)
  - backfill           : park boxes from existing transfers into pending + deduct source

These move inventory across cold_stocks / boxes_v2 / bulk_entry / pending_transfer_stock.
Callers (delete_transfer / delete_transfer_in / backfill endpoint) wrap them in a
transaction. asyncpg type-strictness handled via stock_service._dec / _date / _json.
"""
from __future__ import annotations

import json
from datetime import datetime

from app.modules.transfer.services.stock_service import _date, _dec, _json, _table_exists

COLD_STORAGE_SITE_NAMES = {
    "cold storage", "rishi cold", "savla d-39 cold", "savla d-514 cold",
}
_COLD_TABLES = ("cfpl_cold_stocks", "cdpl_cold_stocks")
_WH_TABLES = ("cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes")


def _is_cold_site(site) -> bool:
    return (site or "").strip().lower() in COLD_STORAGE_SITE_NAMES


def _destination_table(to_storage_type: str, to_company: str) -> str:
    return f"{to_company}_cold_stocks" if to_storage_type == "cold" else f"{to_company}_boxes_v2"


def _cold_row_to_json(row: dict) -> dict:
    def g(name):
        v = row.get(name)
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        try:
            from decimal import Decimal
            if isinstance(v, Decimal):
                return float(v)
        except Exception:
            pass
        return v
    keys = ("inward_dt", "unit", "inward_no", "item_mark", "vakkal", "group_name",
            "item_subgroup", "storage_location", "exporter", "last_purchase_rate",
            "value", "total_inventory_kgs", "spl_remarks")
    return {k: g(k) for k in keys}


async def _find_in_cold_stocks(conn, box_id, tno):
    for table in _COLD_TABLES:
        if not await _table_exists(conn, table):
            continue
        row = await conn.fetchrow(
            f"SELECT * FROM {table} WHERE box_id = $1 AND transaction_no = $2 LIMIT 1", box_id, tno)
        if not row:
            row = await conn.fetchrow(f"SELECT * FROM {table} WHERE box_id = $1 LIMIT 1", box_id)
        if row:
            return table, dict(row)
    return None, None


async def _find_in_bulk_entry(conn, box_id, tno):
    for table in ("cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes"):
        if not await _table_exists(conn, table):
            continue
        row = await conn.fetchrow(
            f"SELECT * FROM {table} WHERE box_id = $1 AND transaction_no = $2 LIMIT 1", box_id, tno)
        if row:
            return table, dict(row)
    return None, None


async def _revert_disposition(conn, *, box_id, transaction_no, disposition_type, reverted_reason=None) -> int:
    try:
        result = await conn.execute(
            """
            UPDATE cold_stock_disposition
            SET reverted = TRUE, reverted_at = CURRENT_TIMESTAMP, reverted_reason = $4
            WHERE box_id = $1 AND transaction_no = $2 AND disposition_type = $3
              AND COALESCE(reverted, FALSE) = FALSE
            """,
            box_id, transaction_no, disposition_type, reverted_reason)
        return int(result.split()[-1]) if result and result.split()[-1].isdigit() else 0
    except Exception:
        return 0


_COLD_INSERT = (
    "INSERT INTO {tbl} (inward_dt, unit, inward_no, item_description, item_mark, vakkal, lot_no, "
    "no_of_cartons, weight_kg, total_inventory_kgs, group_name, item_subgroup, storage_location, "
    "exporter, last_purchase_rate, value, box_id, transaction_no, spl_remarks) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19) ON CONFLICT DO NOTHING"
)


async def restore_to_source(conn, transfer_out_id: int) -> int:
    """Restore every pending row for this transfer-out back to its source table,
    then delete the pending row. Returns count restored."""
    rows = await conn.fetch(
        "SELECT * FROM pending_transfer_stock WHERE transfer_out_id = $1", transfer_out_id)
    restored = 0
    box_num_counters: dict = {}

    for prow in rows:
        p = dict(prow)
        src = p.get("source_table") or ""
        if not src or not await _table_exists(conn, src):
            await conn.execute("DELETE FROM pending_transfer_stock WHERE id = $1", p["id"])
            continue
        cj = _json(p.get("cold_storage_data"))

        if src.endswith("_cold_stocks"):
            tot = cj.get("total_inventory_kgs")
            if tot is None:
                tot = float(p.get("weight_kg") or 0)
            await conn.execute(
                _COLD_INSERT.format(tbl=src),
                _date(cj.get("inward_dt")),
                cj.get("unit") or p.get("from_site"),
                cj.get("inward_no"),
                p.get("item_description"),
                cj.get("item_mark"),
                cj.get("vakkal"),
                p.get("lot_no"),
                _dec(p.get("no_of_cartons") or 1),
                _dec(p.get("weight_kg")),
                _dec(tot),
                cj.get("group_name"),
                cj.get("item_subgroup"),
                cj.get("storage_location") or p.get("from_site"),
                cj.get("exporter"),
                _dec(cj.get("last_purchase_rate")),
                _dec(cj.get("value")),
                p.get("box_id"),
                p.get("transaction_no"),
                cj.get("spl_remarks"),
            )
        else:
            company = src.split("_")[0] if src else ""
            parent = (f"{company}_transactions_v2" if src.endswith("_boxes_v2")
                      else f"{company}_bulk_entry_transactions" if src.endswith("_bulk_entry_boxes") else None)
            target = src
            if parent:
                has_parent = await conn.fetchval(
                    f"SELECT 1 FROM {parent} WHERE transaction_no = $1", p.get("transaction_no"))
                if not has_parent:
                    v2 = f"{company}_boxes_v2"
                    redirected = False
                    if src != v2 and await _table_exists(conn, v2):
                        if await conn.fetchval(f"SELECT 1 FROM {company}_transactions_v2 WHERE transaction_no = $1", p.get("transaction_no")):
                            target, redirected = v2, True
                    if not redirected:
                        await conn.execute("DELETE FROM pending_transfer_stock WHERE id = $1", p["id"])
                        restored += 1
                        continue
            key = (p.get("transaction_no") or "", p.get("article") or p.get("item_description") or "")
            box_num_counters[key] = box_num_counters.get(key, 0) + 1
            await conn.execute(
                f"""INSERT INTO {target}
                    (box_id, transaction_no, article_description, lot_number,
                     net_weight, gross_weight, box_number, count)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING""",
                p.get("box_id"), p.get("transaction_no"),
                p.get("article") or p.get("item_description"), p.get("lot_no"),
                _dec(p.get("net_weight") if p.get("net_weight") is not None else p.get("weight_kg")),
                _dec(p.get("gross_weight")), box_num_counters[key], _dec(p.get("no_of_cartons") or 1),
            )

        await _revert_disposition(conn, box_id=p.get("box_id"), transaction_no=p.get("transaction_no"),
                                  disposition_type="transfer_out_pending",
                                  reverted_reason=f"transfer_out_id={transfer_out_id} cancelled/deleted")
        orig = p.get("original_box_id")
        if orig and orig != p.get("box_id"):
            await _revert_disposition(conn, box_id=orig, transaction_no=p.get("transaction_no"),
                                      disposition_type="transfer_out_pending",
                                      reverted_reason=f"transfer_out_id={transfer_out_id} cancelled (pre-reconcile label)")

        await conn.execute("DELETE FROM pending_transfer_stock WHERE id = $1", p["id"])
        restored += 1

    return restored


_PENDING_INSERT = (
    "INSERT INTO pending_transfer_stock "
    "(transfer_type, transfer_out_id, transfer_out_challan_no, box_id, transaction_no, "
    "from_company, to_company, from_site, to_site, from_storage_type, to_storage_type, "
    "source_table, source_row_id, destination_table, item_description, lot_no, weight_kg, "
    "no_of_cartons, cold_storage_data, gross_weight, net_weight, article, status, dispatched_at, dispatched_by) "
    "VALUES ('INTERUNIT',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,CAST($18 AS JSONB),"
    "$19,$20,$21,'In Transit',$22,$23) ON CONFLICT (box_id, transaction_no) DO NOTHING"
)


async def unpick_to_pending(conn, transfer_in_id: int, transfer_out_id: int) -> int:
    """Reverse a Transfer In: delete its boxes from the destination table and
    re-insert them into pending_transfer_stock (In Transit). Returns count."""
    tout = await conn.fetchrow(
        "SELECT id, challan_no, from_site, to_site, created_by FROM interunit_transfers_header WHERE id = $1",
        transfer_out_id)
    if not tout:
        return 0
    from_site = tout["from_site"] or ""
    to_site = tout["to_site"] or ""
    from_st = "cold" if _is_cold_site(from_site) else "warehouse"
    to_st = "cold" if _is_cold_site(to_site) else "warehouse"

    in_boxes = await conn.fetch(
        """SELECT box_id, transaction_no, article, lot_number, batch_number, net_weight, gross_weight
           FROM interunit_transfer_in_boxes WHERE header_id = $1""", transfer_in_id)

    restored = 0
    now = datetime.now()
    candidate_tables = _COLD_TABLES if to_st == "cold" else ("cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes")

    for brow in in_boxes:
        b = dict(brow)
        if not b.get("box_id") or not b.get("transaction_no"):
            continue
        dest_table = None
        for t in candidate_tables:
            if await _table_exists(conn, t) and await conn.fetchval(
                    f"SELECT id FROM {t} WHERE box_id = $1 AND transaction_no = $2 LIMIT 1",
                    b["box_id"], b["transaction_no"]):
                dest_table = t
                break

        cold_data = None
        item_description = b.get("article") or ""
        lot_no = b.get("lot_number")
        weight_kg = float(b.get("net_weight") or 0)
        no_of_cartons = 1
        if dest_table and dest_table.endswith("_cold_stocks"):
            row = await conn.fetchrow(
                f"SELECT * FROM {dest_table} WHERE box_id = $1 AND transaction_no = $2 LIMIT 1",
                b["box_id"], b["transaction_no"])
            if row:
                rd = dict(row)
                cold_data = _cold_row_to_json(rd)
                item_description = rd.get("item_description") or item_description
                lot_no = rd.get("lot_no") or lot_no
                weight_kg = float(rd.get("weight_kg") or weight_kg)
                no_of_cartons = int(rd.get("no_of_cartons") or 1)

        if dest_table:
            await conn.execute(
                f"DELETE FROM {dest_table} WHERE box_id = $1 AND transaction_no = $2",
                b["box_id"], b["transaction_no"])

        to_company = "cdpl" if (dest_table or "").startswith("cdpl") else "cfpl"
        from_company = "cfpl"
        source_guess = (
            "cfpl_cold_stocks" if from_st == "cold" and to_company == "cfpl"
            else "cdpl_cold_stocks" if from_st == "cold"
            else "cfpl_bulk_entry_boxes" if to_company == "cfpl" else "cdpl_bulk_entry_boxes")
        dest_keep = dest_table or _destination_table(to_st, to_company)

        await conn.execute(
            _PENDING_INSERT,
            transfer_out_id, tout["challan_no"], b["box_id"], b["transaction_no"],
            from_company, to_company, from_site, to_site, from_st, to_st,
            source_guess, None, dest_keep, item_description, lot_no, _dec(weight_kg),
            no_of_cartons, json.dumps(cold_data) if cold_data else None,
            _dec(b.get("gross_weight")), _dec(b.get("net_weight")), b.get("article"),
            now, tout["created_by"] or "system",
        )
        restored += 1

    return restored


async def backfill_pending_from_existing_transfers(conn) -> dict:
    if not await _table_exists(conn, "pending_transfer_stock"):
        return {"error": "pending_transfer_stock table missing"}

    candidates = await conn.fetch(
        """
        SELECT h.id, h.challan_no, h.from_site, h.to_site, h.status, h.created_by, h.created_ts
        FROM interunit_transfers_header h
        WHERE LOWER(COALESCE(h.status,'')) IN ('dispatch','partial','completed','in transit')
          AND NOT EXISTS (SELECT 1 FROM interunit_transfer_in_header ti
                          WHERE ti.transfer_out_id = h.id AND LOWER(COALESCE(ti.status,'')) = 'received')
        ORDER BY h.created_ts ASC
        """)
    s = {"transfers_scanned": len(candidates), "transfers_with_existing_pending": 0,
         "boxes_parked_from_cold": 0, "boxes_parked_from_warehouse": 0,
         "boxes_parked_without_source": 0, "boxes_skipped_already_parked": 0, "boxes_with_missing_id": 0}

    async with conn.transaction():
        for trow in candidates:
            t = dict(trow)
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM pending_transfer_stock WHERE transfer_out_id = $1", t["id"])
            if existing:
                s["transfers_with_existing_pending"] += 1
                continue
            boxes = await conn.fetch(
                """SELECT box_id, transaction_no, article, lot_number, batch_number, net_weight, gross_weight, box_number
                   FROM interunit_transfer_boxes WHERE header_id = $1""", t["id"])
            from_st = "cold" if _is_cold_site(t["from_site"]) else "warehouse"
            to_st = "cold" if _is_cold_site(t["to_site"]) else "warehouse"
            dispatched_at = t["created_ts"] or datetime.now()
            dispatched_by = t["created_by"] or "backfill"

            for brow in boxes:
                b = dict(brow)
                box_id = (b.get("box_id") or "").strip()
                tno = (b.get("transaction_no") or "").strip()
                if not box_id or not tno or tno == "DIRECT":
                    s["boxes_with_missing_id"] += 1
                    continue
                if await conn.fetchval(
                        "SELECT 1 FROM pending_transfer_stock WHERE box_id = $1 AND transaction_no = $2 LIMIT 1", box_id, tno):
                    s["boxes_skipped_already_parked"] += 1
                    continue

                source_table = source_row = cold_data = None
                warehouse_data: dict = {}
                if from_st == "cold":
                    source_table, source_row = await _find_in_cold_stocks(conn, box_id, tno)
                    if source_row is not None:
                        cold_data = _cold_row_to_json(source_row)
                if source_row is None:
                    wh_table, wh_row = await _find_in_bulk_entry(conn, box_id, tno)
                    if wh_row is not None:
                        source_table, source_row = wh_table, wh_row
                        warehouse_data = {
                            "gross_weight": float(wh_row.get("gross_weight") or 0),
                            "net_weight": float(wh_row.get("net_weight") or b.get("net_weight") or 0),
                            "article": wh_row.get("article_description"),
                        }

                if cold_data is not None and source_row is not None:
                    item_description = source_row.get("item_description") or b.get("article") or ""
                    lot_no = source_row.get("lot_no") or b.get("lot_number")
                    weight_kg = float(source_row.get("weight_kg") or b.get("net_weight") or 0)
                    no_of_cartons = int(source_row.get("no_of_cartons") or 1)
                elif warehouse_data:
                    item_description = warehouse_data.get("article") or b.get("article") or ""
                    lot_no = b.get("lot_number")
                    weight_kg = float(warehouse_data.get("net_weight") or b.get("net_weight") or 0)
                    no_of_cartons = 1
                else:
                    item_description = b.get("article") or ""
                    lot_no = b.get("lot_number")
                    weight_kg = float(b.get("net_weight") or 0)
                    no_of_cartons = 1
                    site_lower = (t.get("from_site") or "").strip().lower()
                    gc = "cdpl" if ("rishi" in site_lower or "cdpl" in site_lower) else "cfpl"
                    source_table = f"{gc}_cold_stocks" if from_st == "cold" else f"{gc}_bulk_entry_boxes"

                from_company = "cfpl" if (source_table or "").startswith("cfpl") else "cdpl"
                to_company = from_company
                destination_table = _destination_table(to_st, to_company)

                await conn.execute(
                    _PENDING_INSERT,
                    t["id"], t["challan_no"], box_id, tno, from_company, to_company,
                    t["from_site"], t["to_site"], from_st, to_st, source_table,
                    source_row.get("id") if source_row is not None else None, destination_table,
                    item_description, lot_no, _dec(weight_kg), no_of_cartons,
                    json.dumps(cold_data) if cold_data else None,
                    _dec(warehouse_data.get("gross_weight") if warehouse_data else b.get("gross_weight")),
                    _dec(warehouse_data.get("net_weight") if warehouse_data else b.get("net_weight")),
                    warehouse_data.get("article") or b.get("article"),
                    dispatched_at, dispatched_by,
                )
                if source_row is not None and source_row.get("id") is not None and source_table:
                    await conn.execute(f"DELETE FROM {source_table} WHERE id = $1", source_row["id"])

                if cold_data is not None:
                    s["boxes_parked_from_cold"] += 1
                elif warehouse_data:
                    s["boxes_parked_from_warehouse"] += 1
                else:
                    s["boxes_parked_without_source"] += 1

    return s
