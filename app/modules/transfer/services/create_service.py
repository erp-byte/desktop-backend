"""Transfer-OUT create + update (docs 07 / 08).

Ported from interunit_tools.create_transfer / update_transfer + cold_transfer_out_tools
into one unified cold+warehouse path. create_transfer and update_transfer share
`_persist_lines_boxes_and_park`, which:
  - inserts the lines (net-weight recompute) and boxes (duplicate (box_id, transaction_no)
    rejected — the guard against the "boxes collapsed to 1" inventory-loss bug; each box
    matched to its line by article);
  - parks each scanned/derived box into pending_transfer_stock, deducting the source row
    from cold_stocks or bulk_entry_boxes (reversal_service.park_boxes);
  - parks box-less manual lines as tracking-only rows so manual stock is never dropped;
  - tags from_cold_unit for cold-source transfers;
  - recomputes Dispatch vs Partial;
  - records the ordered-vs-shipped gap on the header (flag-only, warehouse source only).

create_transfer additionally inserts the header (status 'Dispatch') and flips the
originating request to 'Transferred'. update_transfer rolls back the prior pending rows
to source (restore_to_source) before re-inserting/re-parking, then stamps edited_at.
Everything runs inside one transaction (atomic dispatch/edit + deduction).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.transfer import schemas
from app.modules.transfer.services import query_service, reversal_service
from app.modules.transfer.services.stock_service import _date, _dec


def _gen_challan_no() -> str:
    return "TRANS" + datetime.now().strftime("%Y%m%d%H%M")


def _line_weights(line: schemas.TransferLineCreate) -> tuple[float, float]:
    """(net_weight, total_weight): use the frontend value when supplied, else recompute —
    FG = unit_pack_size * pack_size * qty; RM/PM/other = pack_size * qty."""
    pack_size = float(line.pack_size) if line.pack_size else 0.0
    qty = int(line.quantity) if line.quantity else 1
    ups = float(line.unit_pack_size) if line.unit_pack_size else 1.0
    fe_net = float(line.net_weight) if line.net_weight else 0.0
    if fe_net > 0:
        net = round(fe_net, 3)
    elif (line.material_type or "").upper() == "FG":
        net = round(ups * pack_size * qty, 3)
    else:
        net = round(pack_size * qty, 3)
    fe_total = float(line.total_weight) if line.total_weight else 0.0
    total = round(fe_total, 3) if fe_total > 0 else net
    return net, total


async def _persist_lines_boxes_and_park(
    conn, *, header_id: int, challan_no: str, from_warehouse: str, to_warehouse: str,
    lines_in: list, boxes_in: list | None, stock_trf_date, created_by: str, is_cold_source: bool,
) -> None:
    """Insert lines + boxes for an existing header, park to pending (deducting source),
    park box-less lines, tag from_cold_unit, recompute status, and set the warehouse
    reconcile flag. The caller owns the transaction and the header row."""
    # ── Lines ──
    line_rows: list[dict] = []
    for line in lines_in:
        net, total = _line_weights(line)
        qty = int(line.quantity) if line.quantity else 1
        async def _ins_line(line=line, qty=qty, net=net, total=total):
            return await conn.fetchrow(
                """
                INSERT INTO interunit_transfers_lines
                    (header_id, rm_pm_fg_type, item_category, sub_category, item_desc_raw,
                     pack_size, qty, uom, unit_pack_size, net_weight, total_weight,
                     batch_number, lot_number, vakkal, id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                RETURNING id, item_desc_raw, lot_number, qty
                """,
                header_id, line.material_type, line.item_category, line.sub_category,
                line.item_description, _dec(line.pack_size) or 0, qty, line.uom or None,
                _dec(line.unit_pack_size) or 0, _dec(net), _dec(total),
                line.batch_number or "", line.lot_number or "", line.vakkal or "",
                new_short_time_id(),
            )
        row = await insert_with_pk_retry(conn, _ins_line)
        # Stash the computed weights so park_lines doesn't have to re-derive them.
        line_rows.append({**dict(row), "net": net, "total": total})

    # ── Boxes ── reject duplicate (box_id, transaction_no) within this transfer.
    box_input = list(boxes_in or [])
    seen: set = set()
    for box in box_input:
        bid = (box.box_id or "").strip()
        tno = (box.transaction_no or "").strip()
        if bid and tno and tno != "DIRECT":
            if (bid, tno) in seen:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Duplicate box_id '{bid}' for transaction '{tno}' in this transfer. "
                        "Every physical box must carry a unique box_id."
                    ),
                )
            seen.add((bid, tno))

    line_id_by_article = {(l["item_desc_raw"] or "").strip().upper(): l["id"] for l in line_rows}
    fallback_line_id = line_rows[0]["id"] if line_rows else None
    box_dicts: list[dict] = []
    for box in box_input:
        matched = line_id_by_article.get((box.article or "").strip().upper(), fallback_line_id)
        async def _ins_box(box=box, matched=matched):
            return await conn.execute(
                """
                INSERT INTO interunit_transfer_boxes
                    (header_id, transfer_line_id, box_number, box_id, article, lot_number,
                     batch_number, transaction_no, net_weight, gross_weight, id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                header_id, matched, box.box_number, box.box_id or "", box.article,
                box.lot_number or "", box.batch_number or "", box.transaction_no or "",
                _dec(box.net_weight), _dec(box.gross_weight), new_short_time_id(),
            )
        await insert_with_pk_retry(conn, _ins_box)
        box_dicts.append({
            "box_id": box.box_id, "transaction_no": box.transaction_no,
            "article": box.article, "lot_number": box.lot_number,
            "batch_number": box.batch_number,
            "net_weight": box.net_weight, "gross_weight": box.gross_weight,
        })

    # Stamp pending rows with the transfer's initiation date (midnight of stock_trf_date)
    # as a datetime — pending_transfer_stock.dispatched_at is a timestamp.
    dispatched_at = datetime.combine(stock_trf_date, datetime.min.time())

    # ── Park boxes → deduct source (cold_stocks / bulk_entry_boxes) ──
    if box_dicts:
        await reversal_service.park_boxes(
            conn, transfer_out_id=header_id, challan_no=challan_no,
            from_site=from_warehouse, to_site=to_warehouse,
            boxes=box_dicts, dispatched_at=dispatched_at, dispatched_by=created_by,
        )

    # ── Park box-less lines (manual entries) so manual stock is never dropped ──
    covered: dict = {}
    for box in box_input:
        k = ((box.article or "").strip().upper(), (box.lot_number or "").strip())
        covered[k] = covered.get(k, 0) + 1
    uncovered: list[dict] = []
    for l in line_rows:
        k = ((l["item_desc_raw"] or "").strip().upper(), (l["lot_number"] or "").strip())
        if covered.get(k, 0) > 0:
            covered[k] -= 1
        else:
            uncovered.append(l)
    if uncovered:
        await reversal_service.park_lines(
            conn, transfer_out_id=header_id, challan_no=challan_no,
            from_site=from_warehouse, to_site=to_warehouse,
            lines=[{
                "id": l["id"], "item_desc_raw": l["item_desc_raw"], "qty": l["qty"],
                "net_weight": l["net"], "total_weight": l["total"], "lot_number": l["lot_number"],
            } for l in uncovered],
            dispatched_at=dispatched_at, dispatched_by=created_by,
        )

    # ── from_cold_unit tagging (cold source): canonical sub-cold list from the JSONB
    #     snapshots park_boxes just wrote (e.g. "Rishi, Savla D-39"). ──
    if box_dicts and is_cold_source:
        unit_rows = await conn.fetch(
            """
            SELECT DISTINCT cold_storage_data->>'unit' AS u
            FROM pending_transfer_stock
            WHERE transfer_out_id = $1 AND cold_storage_data IS NOT NULL
              AND cold_storage_data->>'unit' IS NOT NULL
            """,
            header_id,
        )
        # cold-specific normalizer (canonical "Supreme Cold", not "Supreme") so from_cold_unit
        # matches the Transfer-Out list's cold-unit ILIKE filter; None (non-cold) is dropped.
        canon = sorted({u for r in unit_rows if r["u"] and (u := query_service._normalize_cold_unit(r["u"]))})
        if canon:
            await conn.execute(
                "UPDATE interunit_transfers_header SET from_cold_unit = $1 WHERE id = $2",
                ", ".join(canon), header_id,
            )

    # ── Status: Dispatch when boxes + box-less lines cover the ordered qty, else Partial. ──
    if box_dicts:
        total_expected = sum(int(l["qty"] or 0) for l in line_rows)
        actual_dispatched = len(box_dicts) + len(uncovered)
        status = "Dispatch" if actual_dispatched >= total_expected else "Partial"
        await conn.execute(
            "UPDATE interunit_transfers_header SET status = $1 WHERE id = $2", status, header_id)

    # ── Reconcile (flag-only). Warehouse source only: record the ordered-vs-shipped box gap.
    #     Cold source isn't reconciled here — the parked boxes are the in-transit truth. ──
    if not is_cold_source:
        box_count = await conn.fetchval(
            "SELECT COUNT(*) FROM interunit_transfer_boxes WHERE header_id = $1", header_id) or 0
        parked = await conn.fetchval(
            "SELECT COUNT(*) FROM pending_transfer_stock WHERE transfer_out_id = $1 AND status = 'In Transit'",
            header_id) or 0
        await conn.execute(
            "UPDATE interunit_transfers_header SET unallocated_boxes = $1 WHERE id = $2",
            max(int(box_count) - int(parked), 0), header_id,
        )


async def create_transfer(conn, data: schemas.TransferCreate, created_by: str) -> dict:
    h = data.header
    challan_no = h.challan_no or _gen_challan_no()
    stock_trf_date = _date(h.stock_trf_date) or datetime.now().date()
    is_cold_source = reversal_service._is_cold_site(h.from_warehouse)

    async with conn.transaction():
        async def _ins_header():
            return await conn.fetchrow(
                """
                INSERT INTO interunit_transfers_header
                    (challan_no, stock_trf_date, from_site, to_site, vehicle_no, driver_name,
                     approved_by, remark, reason_code, status, request_id, created_by, created_ts, id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'Dispatch', $10, $11, $12, $13)
                RETURNING id, challan_no
                """,
                challan_no, stock_trf_date, h.from_warehouse, h.to_warehouse, h.vehicle_no,
                h.driver_name, h.approved_by, h.remark, h.reason_code, data.request_id,
                created_by, datetime.now(), new_short_time_id(),
            )
        header = await insert_with_pk_retry(conn, _ins_header)
        header_id = header["id"]

        await _persist_lines_boxes_and_park(
            conn, header_id=header_id, challan_no=challan_no,
            from_warehouse=h.from_warehouse, to_warehouse=h.to_warehouse,
            lines_in=data.lines, boxes_in=data.boxes, stock_trf_date=stock_trf_date,
            created_by=created_by, is_cold_source=is_cold_source,
        )

        # ── Originating request → Transferred (create only) ──
        if data.request_id:
            await conn.execute(
                "UPDATE interunit_transfer_requests SET status = 'Transferred', updated_at = $1 WHERE id = $2",
                datetime.now(), data.request_id,
            )

    # Read back AFTER commit — get_transfer's best-effort enrichment queries swallow errors,
    # which inside the write txn would poison the connection and roll back the dispatch.
    return await query_service.get_transfer(conn, header_id)


async def update_transfer(conn, transfer_id: int, data: schemas.TransferCreate) -> dict:
    """Edit an existing transfer-OUT (doc 08, ?editId). Rolls back the prior pending rows
    to source, replaces lines+boxes, re-parks, and stamps edited_at. challan_no / created_by
    / created_ts are preserved. No status restriction (the dashboard gates edit for
    Received/Completed) — matches the reference update_transfer."""
    existing = await conn.fetchrow(
        "SELECT id, challan_no, created_by FROM interunit_transfers_header WHERE id = $1", transfer_id)
    if not existing:
        raise HTTPException(404, "Transfer not found")

    h = data.header
    stock_trf_date = _date(h.stock_trf_date) or datetime.now().date()
    is_cold_source = reversal_service._is_cold_site(h.from_warehouse)
    dispatched_by = existing["created_by"] or "system"

    async with conn.transaction():
        # Undo the prior source deductions before re-parking (replace lines + boxes).
        await reversal_service.restore_to_source(conn, transfer_id)

        await conn.execute(
            """
            UPDATE interunit_transfers_header
            SET stock_trf_date = $1, from_site = $2, to_site = $3, vehicle_no = $4,
                driver_name = $5, approved_by = $6, remark = $7, reason_code = $8,
                request_id = $9
            WHERE id = $10
            """,
            stock_trf_date, h.from_warehouse, h.to_warehouse, h.vehicle_no, h.driver_name,
            h.approved_by, h.remark, h.reason_code, data.request_id, transfer_id,
        )
        await conn.execute("DELETE FROM interunit_transfer_boxes WHERE header_id = $1", transfer_id)
        await conn.execute("DELETE FROM interunit_transfers_lines WHERE header_id = $1", transfer_id)

        await _persist_lines_boxes_and_park(
            conn, header_id=transfer_id, challan_no=existing["challan_no"],
            from_warehouse=h.from_warehouse, to_warehouse=h.to_warehouse,
            lines_in=data.lines, boxes_in=data.boxes, stock_trf_date=stock_trf_date,
            created_by=dispatched_by, is_cold_source=is_cold_source,
        )

        # Genuine edit marker — edited_at is written ONLY here, so the pending list's
        # "Edited" badge is honest (updated_ts moves on routine churn).
        await conn.execute(
            "UPDATE interunit_transfers_header SET edited_at = $1 WHERE id = $2",
            datetime.now(), transfer_id)

    return await query_service.get_transfer(conn, transfer_id)
