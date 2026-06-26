"""Inner-cold transfer list + delete + create + get (doc 11 form + dashboard tab).

Ported from the reference cold_storage_server.py inner_cold_transfer endpoints. Records
are grouped by challan_no, each carrying its relabel lines. create_inner_transfer relabels
the actual cold-stock rows (cfpl_cold_stocks / cdpl_cold_stocks): a full transfer updates
lot_no on every matching row; a partial transfer relabels N rows and splits the boundary
row. Those tables are externally managed (absent in dev), so the create is two-phase +
atomic: validate every line first (no mutation) — if any line is invalid, nothing is
written; otherwise all lines apply in one transaction (safer than the reference's
partial-commit, which could double-apply on resubmit).
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.transfer.services.stock_service import _dec

_COLD_TABLES = ("cfpl_cold_stocks", "cdpl_cold_stocks")


async def _table_exists(conn) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass('public.inner_cold_transfer')"))


async def _regclass(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"))


async def _resolve_record_table(conn, record_id: int):
    """Which cold table holds this record id (cfpl first, cdpl fallback)?"""
    for t in _COLD_TABLES:
        if await _regclass(conn, t) and await conn.fetchval(f"SELECT 1 FROM {t} WHERE id = $1", record_id):
            return t
    return None


async def list_inner_cold(conn, *, page, per_page, scope=None) -> dict:
    if not await _table_exists(conn):
        return {"records": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

    # Warehouse scope (empty/None = unrestricted) filters by from_warehouse.
    where, scope_args = "", []
    if scope:
        scope_args = [[str(w).strip().lower().replace("-", "") for w in scope]]
        where = "WHERE REPLACE(LOWER(from_warehouse),'-','') = ANY($1::text[])"

    total = await conn.fetchval(
        f"SELECT COUNT(DISTINCT challan_no) FROM inner_cold_transfer {where}", *scope_args) or 0
    total_pages = max(1, -(-total // per_page)) if total else 0
    offset = (page - 1) * per_page

    challans = await conn.fetch(
        f"""
        SELECT
            challan_no,
            MIN(transfer_date)   AS transfer_date,
            MIN(from_warehouse)  AS from_warehouse,
            MIN(reason_code)     AS reason_code,
            MIN(remark)          AS remark,
            MIN(status)          AS status,
            COUNT(*)             AS line_count,
            SUM(quantity)        AS total_boxes,
            MIN(created_at)      AS created_at
        FROM inner_cold_transfer
        {where}
        GROUP BY challan_no
        ORDER BY MIN(created_at) DESC
        LIMIT ${len(scope_args) + 1} OFFSET ${len(scope_args) + 2}
        """,
        *scope_args, per_page, offset,
    )

    records = []
    for c in challans:
        cr = dict(c)
        line_rows = await conn.fetch(
            """
            SELECT item_description, item_category, quantity,
                   old_lot_number, new_lot_number, net_weight_kg,
                   new_storage_location
            FROM inner_cold_transfer
            WHERE challan_no = $1
            ORDER BY id
            """,
            cr["challan_no"],
        )
        records.append({
            "challan_no": cr["challan_no"],
            "transfer_date": cr["transfer_date"],
            "from_warehouse": cr["from_warehouse"],
            "reason_code": cr["reason_code"],
            "remark": cr["remark"],
            "status": cr["status"] or "COMPLETED",
            "line_count": cr["line_count"],
            "total_boxes": int(cr["total_boxes"]) if cr["total_boxes"] is not None else None,
            "created_at": str(cr["created_at"]) if cr["created_at"] else None,
            "lines": [
                {
                    "item_description": ln["item_description"],
                    "item_category": ln["item_category"],
                    "quantity": ln["quantity"],
                    "old_lot_number": ln["old_lot_number"],
                    "new_lot_number": ln["new_lot_number"],
                    "net_weight_kg": float(ln["net_weight_kg"]) if ln["net_weight_kg"] else 0,
                    "new_storage_location": ln["new_storage_location"],
                }
                for ln in line_rows
            ],
        })

    return {
        "records": records,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


async def get_inner_transfer(conn, challan_no: str) -> dict:
    """Header (MIN-aggregated over the challan group) + lines, for the edit-mode prefill.
    net_weight_kg is the TOTAL transferred weight; the frontend divides by quantity for
    the per-box weight."""
    if not await _table_exists(conn):
        raise HTTPException(404, "Inner cold transfer table not found")
    header = await conn.fetchrow(
        """
        SELECT challan_no, MIN(transfer_date) AS transfer_date, MIN(from_warehouse) AS from_warehouse,
               MIN(reason_code) AS reason_code, MIN(remark) AS remark, MIN(status) AS status,
               MIN(created_at) AS created_at
        FROM inner_cold_transfer WHERE challan_no = $1 GROUP BY challan_no
        """,
        challan_no,
    )
    if not header:
        raise HTTPException(404, "Inner cold transfer not found")
    lines = await conn.fetch(
        """
        SELECT id, stock_record_id, item_category, item_description, net_weight_kg, quantity,
               old_lot_number, new_lot_number, new_storage_location
        FROM inner_cold_transfer WHERE challan_no = $1 ORDER BY id
        """,
        challan_no,
    )
    return {
        "challan_no": header["challan_no"],
        "transfer_date": header["transfer_date"],
        "from_warehouse": header["from_warehouse"],
        "reason_code": header["reason_code"],
        "remark": header["remark"],
        "status": header["status"] or "COMPLETED",
        "created_at": str(header["created_at"]) if header["created_at"] else None,
        "lines": [{
            "id": ln["id"],
            "stock_record_id": ln["stock_record_id"],
            "item_category": ln["item_category"],
            "item_description": ln["item_description"],
            "net_weight_kg": float(ln["net_weight_kg"]) if ln["net_weight_kg"] else 0,
            "quantity": ln["quantity"],
            "old_lot_number": ln["old_lot_number"],
            "new_lot_number": ln["new_lot_number"],
            "new_storage_location": ln["new_storage_location"],
        } for ln in lines],
    }


async def create_inner_transfer(conn, payload) -> dict:
    """Relabel/relocate boxes within cold storage. Two-phase + atomic (see module doc)."""
    if not await _table_exists(conn):
        raise HTTPException(404, "Inner cold transfer table not available")

    h = payload.header
    # ── Phase 1: validate every line (NO writes). `consumed` tracks cartons already claimed
    #    by earlier lines drawing from the SAME source group, so two lines out of one lot
    #    can't collectively over-transfer (each line's availability nets out the prior ones).
    #    We deliberately do NOT freeze the rows here — phase 2 re-reads them live so an
    #    earlier line's relabel in this batch is reflected (the bug a single snapshot caused). ──
    plans: list[dict] = []
    errors: list[str] = []
    consumed: dict[tuple, float] = {}
    for idx, line in enumerate(payload.lines):
        n = idx + 1
        if not line.stock_record_id:
            errors.append(f"Line {n}: stock_record_id is required"); continue
        cold_table = await _resolve_record_table(conn, line.stock_record_id)
        if not cold_table:
            errors.append(f"Line {n}: record {line.stock_record_id} not found in any cold table"); continue
        ref = await conn.fetchrow(f"SELECT * FROM {cold_table} WHERE id = $1", line.stock_record_id)
        if not ref:
            errors.append(f"Line {n}: record {line.stock_record_id} not found"); continue
        # Use the line's explicit old lot (not ref.lot_no, which an earlier line in this
        # payload may already have relabeled) to find the source group.
        source_lot = line.old_lot_number or ref["lot_no"]
        group_total = await conn.fetchval(
            f"""SELECT COALESCE(SUM(no_of_cartons), 0) FROM {cold_table}
                WHERE item_description = $1 AND COALESCE(lot_no,'') = COALESCE($2,'')
                  AND COALESCE(inward_no,'') = COALESCE($3,'')""",
            ref["item_description"], source_lot, ref["inward_no"],
        )
        qty = int(line.quantity)
        key = (cold_table, ref["item_description"], source_lot or "", ref["inward_no"] or "")
        avail = float(group_total or 0) - consumed.get(key, 0)
        if qty <= 0:
            errors.append(f"Line {n}: quantity must be greater than 0"); continue
        if qty > avail:
            errors.append(f"Line {n}: transfer qty ({qty}) exceeds available ({avail:g})"); continue
        consumed[key] = consumed.get(key, 0) + qty
        ref_cartons = float(ref["no_of_cartons"]) if ref["no_of_cartons"] else 1
        wt_per = (float(ref["weight_kg"]) / ref_cartons) if (ref["weight_kg"] and ref_cartons > 0) else 0
        plans.append({
            "line": line, "cold_table": cold_table, "ref": dict(ref), "qty": qty,
            "source_lot": source_lot, "transferred_weight": round(wt_per * qty, 3),
            "new_location": line.new_storage_location or None,
        })

    if errors:
        # Atomic: a bad line aborts the whole batch — nothing was mutated in phase 1.
        return {"status": "error", "updated_records": 0, "errors": errors, "challan_no": h.challan_no}

    # ── Phase 2: apply every plan in one transaction. Re-read the live rows per plan (filtered
    #    by the source lot) so rows an earlier line already relabeled are excluded — the lot
    #    filter naturally yields only the still-unmoved cartons. ──
    async with conn.transaction():
        for p in plans:
            line, tbl, ref = p["line"], p["cold_table"], p["ref"]
            new_lot, new_location = line.new_lot_number, p["new_location"]
            live_rows = await conn.fetch(
                f"""SELECT id, no_of_cartons, weight_kg, value FROM {tbl}
                    WHERE item_description = $1 AND COALESCE(lot_no,'') = COALESCE($2,'')
                      AND COALESCE(inward_no,'') = COALESCE($3,'') ORDER BY id ASC""",
                ref["item_description"], p["source_lot"], ref["inward_no"],
            )
            current = sum(float(r["no_of_cartons"] or 0) for r in live_rows)
            qty = p["qty"]
            if qty > current:
                # Stock changed since validation (concurrent edit / earlier line) — abort atomically.
                raise HTTPException(409, f"Lot {p['source_lot']} changed during submit; please retry.")
            if qty == current:
                for r in live_rows:
                    if new_location:
                        await conn.execute(f"UPDATE {tbl} SET lot_no=$1, storage_location=$2 WHERE id=$3", new_lot, new_location, r["id"])
                    else:
                        await conn.execute(f"UPDATE {tbl} SET lot_no=$1 WHERE id=$2", new_lot, r["id"])
            else:
                remaining = qty
                for r in live_rows:
                    if remaining <= 0:
                        break
                    rc = float(r["no_of_cartons"] or 0)
                    take = rc if rc <= remaining else remaining
                    remaining -= take
                    if take >= rc:
                        if new_location:
                            await conn.execute(f"UPDATE {tbl} SET lot_no=$1, storage_location=$2 WHERE id=$3", new_lot, new_location, r["id"])
                        else:
                            await conn.execute(f"UPDATE {tbl} SET lot_no=$1 WHERE id=$2", new_lot, r["id"])
                    else:
                        # Split: reduce the original row, insert a new row carrying the moved cartons.
                        r_wt = float(r["weight_kg"] or 0); r_val = float(r["value"] or 0)
                        wtp = r_wt / rc if rc > 0 else 0
                        valp = r_val / rc if rc > 0 else 0
                        rem = rc - take
                        await conn.execute(
                            f"""UPDATE {tbl} SET no_of_cartons=$1, weight_kg=$2, total_inventory_kgs=$2, value=$3 WHERE id=$4""",
                            _dec(rem), _dec(round(wtp * rem, 3)), _dec(round(valp * rem, 2)), r["id"])
                        await conn.execute(
                            f"""INSERT INTO {tbl}
                                (inward_dt, unit, inward_no, item_description, item_mark, vakkal, lot_no,
                                 no_of_cartons, weight_kg, total_inventory_kgs, group_name, storage_location,
                                 exporter, last_purchase_rate, value, box_id, transaction_no)
                                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$9,$10,$11,$12,$13,$14,NULL,NULL)""",
                            ref["inward_dt"], ref["unit"], ref["inward_no"], ref["item_description"], ref["item_mark"],
                            ref["vakkal"], new_lot, _dec(take), _dec(round(wtp * take, 3)), ref["group_name"],
                            new_location or ref["storage_location"], ref["exporter"],
                            _dec(ref["last_purchase_rate"]) if ref["last_purchase_rate"] is not None else None,
                            _dec(round(valp * take, 2)),
                        )

            await conn.execute(
                """INSERT INTO inner_cold_transfer
                    (challan_no, transfer_date, from_warehouse, reason_code, remark, stock_record_id,
                     item_category, item_description, net_weight_kg, quantity, old_lot_number,
                     new_lot_number, new_storage_location, old_storage_location, transfer_type)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
                h.challan_no, h.transfer_name, h.from_warehouse, h.reason_code, h.remark,
                line.stock_record_id, line.item_category, line.item_description, _dec(p["transferred_weight"]),
                p["qty"], line.old_lot_number, new_lot, new_location, ref["storage_location"],
                h.transfer_type or "INNER_COLD",
            )

    return {"status": "success", "updated_records": len(plans), "errors": [], "challan_no": h.challan_no}


async def delete_inner_cold(conn, challan_no: str) -> dict:
    if not await _table_exists(conn):
        raise HTTPException(404, "Inner cold transfer table not found")
    result = await conn.execute(
        "DELETE FROM inner_cold_transfer WHERE challan_no = $1", challan_no)
    # asyncpg returns a status string like "DELETE 3"; rowcount is the trailing int.
    deleted = int(result.split()[-1]) if result and result.split()[-1].isdigit() else 0
    if deleted == 0:
        raise HTTPException(404, "Inner cold transfer not found")
    return {"success": True, "message": f"Inner cold transfer {challan_no} deleted successfully."}
