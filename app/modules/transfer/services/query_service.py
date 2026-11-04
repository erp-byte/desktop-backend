"""Read paths for requests / transfers / transfer-ins.

Ported from `transfer_backend_reference/.../interunit_tools.py`
(list_requests, get_request, list_transfers, get_transfer, list_transfer_ins,
get_transfer_in + their row mappers) from sync SQLAlchemy `text()` to asyncpg.

Porting notes:
  * `:named` params → positional `$N`.
  * SQLAlchemy Row attribute access (`row.id`) → `dict(record)` + `.get()`.
  * JSONB columns come back from asyncpg as text → `_json()` decodes them.
  * DB columns `from_site`/`to_site`/`reason_code` map to API
    `from_warehouse`/`to_warehouse`/`reason_description`, exactly as the
    reference mappers did.

Sites are compared as sites (app.core.sites): a list filter or a user's warehouse
scope matches every spelling of each site ('D-39' finds rows stored 'Savla D-39'
or 'Old Savla', 'A185' rows stored 'Warehouse A185'), by binding
sites.scope_keys(...) into site_scope_sql; a detail read of a row outside the
user's scope is 404 (permissions.in_scope).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException

from app.core.sites import canonical_site, cold_company, scope_keys
from app.core.warehouse_scope import WAREHOUSE_NORM_ANY_SQL
from app.modules.transfer import permissions


def _normalize_cold_unit(raw: Optional[str]) -> Optional[str]:
    """The canonical name of the cold store `raw` names ('D-39' -> 'Savla D-39',
    'Supreme Cold' -> 'Supreme', 'ESKIMO' -> 'Eskimo'); None for anything else: a
    warehouse, the legacy 'Cold Storage' label (no one store) or an unknown name such
    as bare 'Savla'. The rule create_service writes from_cold_unit by."""
    return canonical_site(raw) if cold_company(raw) else None


def site_scope_sql(param: str, *, sites=(), units=()) -> str:
    """SQL that is true when a column of `sites` (one site each) or one unit of a column
    of `units` (a comma list of cold units, as from_cold_unit holds 'Rishi, Savla D-39')
    names a site bound at `param`. Bind sites.scope_keys(...) there, so every spelling
    of each site matches: 'D-39' also finds 'Savla D-39' and 'Old Savla'."""
    parts = [WAREHOUSE_NORM_ANY_SQL(col, param) for col in sites]
    parts += [f"EXISTS (SELECT 1 FROM unnest(string_to_array({col}, ',')) AS cu(unit) "
              f"WHERE {WAREHOUSE_NORM_ANY_SQL('cu.unit', param)})" for col in units]
    return "(" + " OR ".join(parts) + ")"


def _convert_date(date_str: str):
    """DD-MM-YYYY → date. Mirrors interunit_tools._convert_date."""
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use DD-MM-YYYY")


def _json(val: Any) -> Optional[dict]:
    """asyncpg returns JSONB as text; decode to dict (tolerate dict / None)."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (TypeError, ValueError):
        return None


def _s(val: Any, default: str = "0") -> str:
    return str(val) if val is not None else default


def _total_pages(total: int, per_page: int) -> int:
    return (total + per_page - 1) // per_page if total else 0


# ── Row mappers (DB column dict → API dict) ───────────────────────────────────
def _map_request_line(r: dict) -> dict:
    return {
        "id": r["id"],
        "request_id": r["request_id"],
        "material_type": r.get("rm_pm_fg_type") or "",
        "item_category": r.get("item_category") or "",
        "sub_category": r.get("sub_category") or "",
        "item_description": r.get("item_desc_raw") or "",
        "quantity": _s(r.get("qty")),
        "uom": r.get("uom") or "",
        "pack_size": _s(r.get("pack_size")),
        "unit_pack_size": _s(r["unit_pack_size"], None) if r.get("unit_pack_size") is not None else None,
        "net_weight": _s(r.get("net_weight")),
        "lot_number": r.get("lot_number") or "",
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def _map_request_header(r: dict) -> dict:
    rd = r.get("request_date")
    return {
        "id": r["id"],
        "request_no": r.get("request_no") or "",
        "request_date": rd.strftime("%d-%m-%Y") if rd else "",
        "from_warehouse": r.get("from_site") or "",
        "to_warehouse": r.get("to_site") or "",
        "reason_description": r.get("reason_code") or "",
        "status": r.get("status") or "Pending",
        "reject_reason": r.get("reject_reason"),
        "created_by": r.get("created_by"),
        "created_ts": r.get("created_ts"),
        "rejected_ts": r.get("rejected_ts"),
        "updated_at": r.get("updated_at"),
        "store_decision": r.get("store_decision"),
        "store_decided_by": r.get("store_decided_by"),
        "store_decided_at": r.get("store_decided_at"),
        "store_reject_reason": r.get("store_reject_reason"),
        "manager_decision": r.get("manager_decision"),
        "manager_decided_by": r.get("manager_decided_by"),
        "manager_decided_at": r.get("manager_decided_at"),
        "manager_reject_reason": r.get("manager_reject_reason"),
        "store_hold_reason": r.get("store_hold_reason"),
        "manager_hold_reason": r.get("manager_hold_reason"),
        # The request's newest transfer (newest_transfers); filled by get / list.
        "transfer_id": r.get("transfer_id"),
        "transfer_challan_no": r.get("transfer_challan_no"),
        "transfer_status": r.get("transfer_status"),
    }


def _with_transfer(item: dict, transfer: dict | None) -> dict:
    """A mapped request with its newest transfer's id, challan and status (all None
    without one)."""
    item["transfer_id"] = transfer["id"] if transfer else None
    item["transfer_challan_no"] = transfer["challan_no"] if transfer else None
    item["transfer_status"] = transfer["status"] if transfer else None
    return item


def _map_transfer_line(r: dict) -> dict:
    return {
        "id": r["id"],
        "header_id": r["header_id"],
        "material_type": r.get("rm_pm_fg_type") or "",
        "item_category": r.get("item_category") or "",
        "sub_category": r.get("sub_category") or "",
        "item_description": r.get("item_desc_raw") or "",
        "quantity": _s(r.get("qty")),
        "uom": r.get("uom") or "",
        "pack_size": _s(r.get("pack_size")),
        "unit_pack_size": _s(r["unit_pack_size"], None) if r.get("unit_pack_size") is not None else None,
        "net_weight": _s(r.get("net_weight")),
        "total_weight": _s(r.get("total_weight")),
        "batch_number": r.get("batch_number") or "",
        "lot_number": r.get("lot_number") or "",
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def _map_transfer_header(r: dict) -> dict:
    sd = r.get("stock_trf_date")
    return {
        "id": r["id"],
        "challan_no": r.get("challan_no") or "",
        "stock_trf_date": sd.strftime("%d-%m-%Y") if sd else "",
        "from_warehouse": r.get("from_site") or "",
        "to_warehouse": r.get("to_site") or "",
        "vehicle_no": r.get("vehicle_no") or "",
        "driver_name": r.get("driver_name"),
        "approved_by": r.get("approved_by"),
        "remark": r.get("remark"),
        "reason_code": r.get("reason_code"),
        "status": r.get("status") or "Pending",
        "request_id": r.get("request_id"),
        "request_no": r.get("request_no"),
        "created_by": r.get("created_by"),
        "created_ts": r.get("created_ts"),
        "approved_ts": r.get("approved_ts"),
        "has_variance": bool(r.get("has_variance")),
        "from_cold_unit": r.get("from_cold_unit") or None,
    }


def _map_box(r: dict) -> dict:
    box_id = r.get("box_id")
    return {
        "id": r["id"],
        "header_id": r["header_id"],
        "transfer_line_id": r.get("transfer_line_id"),
        "box_number": r.get("box_number"),
        "box_id": box_id if box_id else "",
        "article": r.get("article") or "",
        "lot_number": r.get("lot_number"),
        "batch_number": r.get("batch_number"),
        "transaction_no": r.get("transaction_no"),
        "net_weight": _s(r.get("net_weight")),
        "gross_weight": _s(r.get("gross_weight")),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        "source_storage": r.get("source_storage") or None,
        "source_unit": r.get("source_unit") or None,
    }


def _map_transfer_in_header(r: dict) -> dict:
    result = {
        "id": r["id"],
        "transfer_out_id": r["transfer_out_id"],
        "transfer_out_no": r.get("transfer_out_no") or "",
        "grn_number": r.get("grn_number") or "",
        "grn_date": r.get("grn_date"),
        "receiving_warehouse": r.get("receiving_warehouse") or "",
        "received_by": r.get("received_by") or "",
        "received_at": r.get("received_at"),
        "box_condition": r.get("box_condition"),
        "condition_remarks": r.get("condition_remarks"),
        "status": r.get("status") or "Received",
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        # The cold units the transfer's boxes came from (the list and the read join it in).
        "from_cold_unit": r.get("from_cold_unit") or None,
    }
    if r.get("from_warehouse"):
        result["from_warehouse"] = r["from_warehouse"]
    return result


async def _fetch_transfer_in_boxes_q(conn, header_id: int) -> list:
    """Shared transfer-in box fetch (used by get_transfer_in + receive_service)."""
    rows = await conn.fetch(
        """
        SELECT id, header_id, box_id, article, batch_number,
               lot_number, transaction_no, net_weight, gross_weight,
               scanned_at, is_matched, transfer_out_box_id, issue, line_index,
               inward_box_id
        FROM interunit_transfer_in_boxes
        WHERE header_id = $1
        ORDER BY scanned_at
        """,
        header_id,
    )
    return [_map_transfer_in_box(dict(r)) for r in rows]


def _map_transfer_in_box(r: dict) -> dict:
    return {
        "id": r["id"],
        "header_id": r["header_id"],
        "box_id": r.get("box_id") or "",
        "transfer_out_box_id": r.get("transfer_out_box_id"),
        "article": r.get("article"),
        "batch_number": r.get("batch_number"),
        "lot_number": r.get("lot_number"),
        "transaction_no": r.get("transaction_no"),
        "net_weight": float(r["net_weight"]) if r.get("net_weight") is not None else None,
        "gross_weight": float(r["gross_weight"]) if r.get("gross_weight") is not None else None,
        "scanned_at": r.get("scanned_at"),
        "is_matched": r["is_matched"] if r.get("is_matched") is not None else True,
        "issue": _json(r.get("issue")),
        "line_index": r.get("line_index"),
    }


# ── Requests ──────────────────────────────────────────────────────────────
async def newest_transfers(conn, request_ids) -> dict:
    """{request id: its newest transfer} for the given requests: of the
    interunit_transfers_header rows with that request_id, the one created last (by
    created_ts — a row without one is the oldest — then by id), as a dict; a request
    without a transfer is left out. Read only, one query. The request reads (get / list)
    show it, and the scan screen saves its boxes to it (scan_service)."""
    ids = list(dict.fromkeys(request_ids))
    if not ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT id, challan_no, status, request_id, stock_trf_date, from_site, to_site, remark,
               vehicle_no, driver_name, approved_by, created_by, created_ts
        FROM interunit_transfers_header
        WHERE request_id = ANY($1::bigint[])
        """,
        ids,
    )
    def newness(t):
        return (t["created_ts"] is not None, t["created_ts"], t["id"])
    out: dict = {}
    for t in map(dict, rows):
        held = out.get(t["request_id"])
        if held is None or newness(t) > newness(held):
            out[t["request_id"]] = t
    return out


async def list_requests(conn, *, page, per_page, status=None,
                        from_warehouse=None, to_warehouse=None, scope=None) -> dict:
    # `scope` = the caller's allowed warehouses (empty/None = unrestricted). When
    # set, only requests where one of those is the source OR destination are
    # returned. from_warehouse/to_warehouse are optional explicit narrowing. Each
    # matches every spelling of its sites (site_scope_sql).
    clauses = ["r.status != 'Deleted'"]
    args: list = []
    if status:
        args.append(status)
        clauses.append(f"r.status = ${len(args)}")
    if from_warehouse:
        args.append(scope_keys(from_warehouse))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("r.from_site",)))
    if to_warehouse:
        args.append(scope_keys(to_warehouse))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("r.to_site",)))
    if scope:
        args.append(scope_keys(scope))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("r.from_site", "r.to_site")))

    where = "WHERE " + " AND ".join(clauses)
    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM interunit_transfer_requests r {where}", *args)

    offset = (page - 1) * per_page
    rows = await conn.fetch(
        f"""
        SELECT id, request_no, request_date, from_site, to_site,
               reason_code, remarks, status, reject_reason,
               created_by, created_ts, rejected_ts, updated_at,
               store_decision, store_decided_by, store_decided_at, store_reject_reason,
               manager_decision, manager_decided_by, manager_decided_at, manager_reject_reason,
               store_hold_reason, manager_hold_reason
        FROM interunit_transfer_requests r
        {where}
        ORDER BY r.created_ts DESC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, per_page, offset,
    )

    records = [dict(row) for row in rows]
    result = [_map_request_header(r) for r in records]

    if result:
        request_ids = [r["id"] for r in records]
        line_rows = await conn.fetch(
            """
            SELECT id, request_id, rm_pm_fg_type, item_category, sub_category,
                   item_desc_raw, pack_size, qty, uom, unit_pack_size,
                   net_weight, total_weight, lot_number, created_at, updated_at
            FROM interunit_transfer_request_lines
            WHERE request_id = ANY($1::int[])
            ORDER BY id
            """,
            request_ids,
        )
        by_req: dict = {}
        for lr in line_rows:
            d = dict(lr)
            by_req.setdefault(d["request_id"], []).append(_map_request_line(d))
        transfers = await newest_transfers(conn, request_ids)
        for item in result:
            item["lines"] = by_req.get(item["id"], [])
            _with_transfer(item, transfers.get(item["id"]))

    return {
        "records": result,
        "total": total or 0,
        "page": page,
        "per_page": per_page,
        "total_pages": _total_pages(total or 0, per_page),
    }


async def get_request(conn, request_id: int, *, scope=None) -> dict:
    """One request with its lines and newest transfer. With `scope` (the reader's
    warehouses, permissions.scope_warehouses), a request neither from nor to one of them
    is 404, as if it did not exist."""
    row = await conn.fetchrow(
        """
        SELECT id, request_no, request_date, from_site, to_site,
               reason_code, remarks, status, reject_reason,
               created_by, created_ts, rejected_ts, updated_at,
               store_decision, store_decided_by, store_decided_at, store_reject_reason,
               manager_decision, manager_decided_by, manager_decided_at, manager_reject_reason,
               store_hold_reason, manager_hold_reason
        FROM interunit_transfer_requests
        WHERE id = $1
        """,
        request_id,
    )
    if not row or not permissions.in_scope(scope, row["from_site"], row["to_site"]):
        raise HTTPException(404, "Request not found")
    result = _map_request_header(dict(row))
    line_rows = await conn.fetch(
        """
        SELECT id, request_id, rm_pm_fg_type, item_category, sub_category,
               item_desc_raw, pack_size, qty, uom, unit_pack_size,
               net_weight, total_weight, lot_number, created_at, updated_at
        FROM interunit_transfer_request_lines
        WHERE request_id = $1
        ORDER BY id
        """,
        request_id,
    )
    result["lines"] = [_map_request_line(dict(r)) for r in line_rows]
    return _with_transfer(result, (await newest_transfers(conn, [request_id])).get(request_id))


# ── Transfers (OUT) ───────────────────────────────────────────────────────
_VALID_TRANSFER_SORT = {"challan_no", "stock_trf_date", "from_site", "to_site", "status", "created_ts"}


async def list_transfers(conn, *, page, per_page, status=None, from_site=None,
                        to_site=None, from_date=None, to_date=None, challan_no=None,
                        sort_by="created_ts", sort_order="desc", scope=None) -> dict:
    clauses = ["1=1"]
    args: list = []
    if status:
        args.append(status)
        clauses.append(f"h.status = ${len(args)}")
    if from_site:
        # Sent from that site, or (a cold dispatch) with boxes from that cold unit; any
        # spelling of either ('Supreme' also finds from_cold_unit 'Supreme Cold').
        args.append(scope_keys(from_site))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("h.from_site",), units=("h.from_cold_unit",)))
    if to_site:
        args.append(scope_keys(to_site))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("h.to_site",)))
    if from_date:
        args.append(_convert_date(from_date))
        clauses.append(f"h.stock_trf_date >= ${len(args)}")
    if to_date:
        args.append(_convert_date(to_date))
        clauses.append(f"h.stock_trf_date <= ${len(args)}")
    if challan_no:
        args.append(challan_no)
        clauses.append(f"h.challan_no = ${len(args)}")
    if scope:
        # From or To one of the caller's sites, or with boxes from one of them (each unit
        # of from_cold_unit, which can name several: 'Rishi, Savla D-39').
        args.append(scope_keys(scope))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("h.from_site", "h.to_site"),
                                      units=("h.from_cold_unit",)))

    where = " AND ".join(clauses)
    if sort_by not in _VALID_TRANSFER_SORT:
        sort_by = "created_ts"
    direction = "DESC" if str(sort_order).lower() == "desc" else "ASC"

    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM interunit_transfers_header h WHERE {where}", *args)

    offset = (page - 1) * per_page
    rows = await conn.fetch(
        f"""
        SELECT
            h.id, h.challan_no, h.stock_trf_date, h.from_site, h.to_site,
            h.vehicle_no, h.driver_name, h.remark, h.reason_code,
            h.status, h.request_id, h.created_by, h.created_ts,
            h.approved_by, h.approved_ts, h.has_variance, h.from_cold_unit,
            r.request_no,
            COALESCE(lc.items_count, 0) AS items_count,
            COALESCE(bc.boxes_count, 0) AS boxes_count,
            COALESCE(lc.total_qty, 0) AS total_qty,
            COALESCE(lt.lot_numbers_text, '') AS lot_numbers_text
        FROM interunit_transfers_header h
        LEFT JOIN interunit_transfer_requests r ON h.request_id = r.id
        LEFT JOIN (
            SELECT header_id,
                   COUNT(DISTINCT item_desc_raw) AS items_count,
                   COALESCE(SUM(qty), 0) AS total_qty
            FROM interunit_transfers_lines
            GROUP BY header_id
        ) lc ON h.id = lc.header_id
        LEFT JOIN (
            SELECT header_id,
                   COUNT(DISTINCT COALESCE(box_id, id::text)) AS boxes_count
            FROM interunit_transfer_boxes
            GROUP BY header_id
        ) bc ON h.id = bc.header_id
        LEFT JOIN (
            SELECT header_id,
                   STRING_AGG(DISTINCT lot_number, ' ') AS lot_numbers_text
            FROM interunit_transfer_boxes
            WHERE lot_number IS NOT NULL AND lot_number <> ''
            GROUP BY header_id
        ) lt ON h.id = lt.header_id
        WHERE {where}
        ORDER BY h.{sort_by} {direction}
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, per_page, offset,
    )

    records = []
    for row in rows:
        r = dict(row)
        item = _map_transfer_header(r)
        item["items_count"] = r.get("items_count") or 0
        item["boxes_count"] = r.get("boxes_count") or 0
        item["total_qty"] = r.get("total_qty") or 0
        item["pending_items"] = max(0, int(r.get("total_qty") or 0) - int(r.get("boxes_count") or 0))
        item["lot_numbers_text"] = r.get("lot_numbers_text") or ""
        records.append(item)

    return {
        "records": records,
        "total": total or 0,
        "page": page,
        "per_page": per_page,
        "total_pages": _total_pages(total or 0, per_page),
    }


async def _fetch_transfer_boxes(conn, header_id: int) -> list:
    # Each box with its own in-transit row on THIS transfer (box_id AND transaction_no,
    # trimmed): a box_id alone is not unique, and another transfer's row is not this
    # box's source. source_unit is the snapshot's unit as a canonical site name.
    rows = await conn.fetch(
        """
        SELECT itb.id, itb.header_id, itb.transfer_line_id, itb.box_number,
               itb.box_id, itb.article, itb.lot_number, itb.batch_number,
               itb.transaction_no, itb.net_weight, itb.gross_weight,
               itb.created_at, itb.updated_at,
               pts.cold_storage_data->>'storage_location' AS source_storage,
               pts.cold_storage_data->>'unit' AS source_unit
        FROM interunit_transfer_boxes itb
        LEFT JOIN pending_transfer_stock pts
            ON pts.transfer_out_id = itb.header_id
           AND btrim(pts.box_id) = btrim(itb.box_id)
           AND btrim(pts.transaction_no) = btrim(itb.transaction_no)
           AND pts.status = 'In Transit'
        WHERE itb.header_id = $1
        ORDER BY itb.box_number
        """,
        header_id,
    )
    return [_map_box({**dict(r), "source_unit": canonical_site(r["source_unit"])}) for r in rows]


async def transfer_sites(conn, transfer_id: int) -> dict | None:
    """The stored From, To and cold units of a transfer ({from_site, to_site,
    from_cold_unit}), for the profile lock and the read scope; None when there is no
    such transfer. Read only."""
    row = await conn.fetchrow(
        "SELECT from_site, to_site, from_cold_unit FROM interunit_transfers_header WHERE id = $1",
        transfer_id)
    return dict(row) if row else None


async def receipt_to_site(conn, transfer_in_id: int) -> str | None:
    """The To site of the transfer a receipt (GRN) is for; None when there is no such
    receipt or transfer. Read only."""
    return await conn.fetchval(
        "SELECT t.to_site FROM interunit_transfer_in_header ih "
        "JOIN interunit_transfers_header t ON t.id = ih.transfer_out_id WHERE ih.id = $1",
        transfer_in_id)


async def get_transfer(conn, transfer_id: int, *, scope=None) -> dict:
    """One transfer with its lines, boxes and receipts. With `scope` (the reader's
    warehouses, permissions.scope_warehouses), a transfer whose From, To and cold units
    are none of them is 404, as if it did not exist: the From's store, the To's (the
    receive screen) and a cold unit's still read it (the challan, the hover cards)."""
    row = await conn.fetchrow(
        """
        SELECT h.id, h.challan_no, h.stock_trf_date, h.from_site, h.to_site,
               h.vehicle_no, h.driver_name, h.approved_by, h.remark,
               h.reason_code, h.status, h.request_id, h.created_by,
               h.created_ts, h.approved_ts, h.has_variance, h.from_cold_unit,
               r.request_no
        FROM interunit_transfers_header h
        LEFT JOIN interunit_transfer_requests r ON h.request_id = r.id
        WHERE h.id = $1
        """,
        transfer_id,
    )
    if not row or not permissions.in_scope(scope, row["from_site"], row["to_site"], row["from_cold_unit"]):
        raise HTTPException(404, "Transfer not found")

    result = _map_transfer_header(dict(row))

    # Enrich blank category/sub_category/uom from the all_sku master (matched on
    # description, preferring the row whose item_type matches the line). COALESCE
    # so any value already stored on the line wins; only blanks get filled.
    # ponytail: UPPER(TRIM()) skips idx_all_sku_particulars, but lines/transfer is
    # tiny and all_sku is a bounded master — add a functional index if it ever bites.
    line_rows = await conn.fetch(
        """
        SELECT l.id, l.header_id, l.rm_pm_fg_type,
               COALESCE(NULLIF(l.item_category, ''), sk.item_group) AS item_category,
               COALESCE(NULLIF(l.sub_category, ''), sk.sub_group)   AS sub_category,
               l.item_desc_raw, l.pack_size, l.qty,
               COALESCE(NULLIF(l.uom, ''), sk.uom::text)            AS uom,
               l.unit_pack_size, l.net_weight, l.total_weight,
               l.batch_number, l.lot_number, l.created_at, l.updated_at
        FROM interunit_transfers_lines l
        LEFT JOIN LATERAL (
            SELECT item_group, sub_group, uom
            FROM public.all_sku
            WHERE UPPER(TRIM(particulars)) = UPPER(TRIM(l.item_desc_raw))
              AND (COALESCE(l.rm_pm_fg_type, '') = ''
                   OR UPPER(item_type) = UPPER(l.rm_pm_fg_type))
            ORDER BY (UPPER(COALESCE(item_type, '')) = UPPER(COALESCE(l.rm_pm_fg_type, ''))) DESC,
                     sku_id
            LIMIT 1
        ) sk ON TRUE
        WHERE l.header_id = $1
        ORDER BY l.id
        """,
        transfer_id,
    )
    result["lines"] = [_map_transfer_line(dict(r)) for r in line_rows]
    result["boxes"] = await _fetch_transfer_boxes(conn, transfer_id)

    # Per-lot dominant sub-cold attribution (mirrors interunit_tools.get_transfer): the
    # cold store most of the lot's cold rows and cold snapshots name, by canonical name
    # (_normalize_cold_unit; a tie goes to the name first in order). The database counts
    # each raw unit spelling; the spellings are folded into stores here.
    try:
        lot_numbers = sorted({(b.get("lot_number") or "").strip() for b in result["boxes"]} - {""})
        lot_origin_unit: dict[str, str] = {}
        if lot_numbers:
            origin_rows = await conn.fetch(
                """
                SELECT lot_no, raw_u, COUNT(*) AS n
                FROM (
                    SELECT lot_no, unit AS raw_u FROM cfpl_cold_stocks WHERE lot_no = ANY($1::text[])
                    UNION ALL
                    SELECT lot_no, unit AS raw_u FROM cdpl_cold_stocks WHERE lot_no = ANY($1::text[])
                    UNION ALL
                    SELECT lot_no, cold_storage_data->>'unit' AS raw_u
                    FROM pending_transfer_stock
                    WHERE lot_no = ANY($1::text[]) AND cold_storage_data IS NOT NULL
                ) lot_sources
                WHERE raw_u IS NOT NULL
                GROUP BY lot_no, raw_u
                """,
                lot_numbers,
            )
            counts: dict[str, dict[str, int]] = {}
            for orow in origin_rows:
                unit = _normalize_cold_unit(orow["raw_u"])
                if unit:
                    per_lot = counts.setdefault(orow["lot_no"], {})
                    per_lot[unit] = per_lot.get(unit, 0) + int(orow["n"] or 0)
            lot_origin_unit = {lot: min(per_lot, key=lambda u: (-per_lot[u], u))
                               for lot, per_lot in counts.items()}
        for b in result["boxes"]:
            lot = (b.get("lot_number") or "").strip()
            b["lot_origin_unit"] = lot_origin_unit.get(lot)
    except Exception:
        pass

    # Attach GRN (Transfer-In) records so the hover card can show receipt state.
    try:
        grn_rows = await conn.fetch(
            """
            SELECT tih.id, tih.grn_number, tih.status, tih.received_by, tih.received_at,
                   COUNT(tib.id) AS received_boxes
            FROM interunit_transfer_in_header tih
            LEFT JOIN interunit_transfer_in_boxes tib ON tib.header_id = tih.id
            WHERE tih.transfer_out_id = $1
            GROUP BY tih.id, tih.grn_number, tih.status, tih.received_by, tih.received_at
            ORDER BY tih.created_at DESC
            """,
            transfer_id,
        )
        result["grn_records"] = [
            {
                "id": g["id"],
                "grn_number": g["grn_number"] or "",
                "status": g["status"] or "",
                "received_by": g["received_by"] or "",
                "received_at": g["received_at"].isoformat() if g["received_at"] else None,
                "received_boxes": int(g["received_boxes"] or 0),
            }
            for g in grn_rows
        ]
    except Exception:
        result["grn_records"] = []

    return result


# ── Transfers IN (GRN) ────────────────────────────────────────────────────────
_VALID_TRANSFER_IN_SORT = {"grn_number", "grn_date", "receiving_warehouse", "status", "created_at"}


async def list_transfer_ins(conn, *, page, per_page, receiving_warehouse=None,
                            from_date=None, to_date=None,
                            sort_by="created_at", sort_order="desc", scope=None) -> dict:
    clauses = ["1=1"]
    args: list = []
    if receiving_warehouse:
        args.append(scope_keys(receiving_warehouse))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("h.receiving_warehouse",)))
    if from_date:
        args.append(_convert_date(from_date))
        clauses.append(f"h.grn_date >= ${len(args)}")
    if to_date:
        args.append(_convert_date(to_date))
        clauses.append(f"h.grn_date <= ${len(args)}")
    if scope:
        # Scoped on the receiving warehouse, or the transfer-out's From, To or cold units
        # (as list_transfers scopes the transfer itself).
        args.append(scope_keys(scope))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("h.receiving_warehouse", "t.from_site", "t.to_site"),
                                      units=("t.from_cold_unit",)))

    where = " AND ".join(clauses)
    if sort_by not in _VALID_TRANSFER_IN_SORT:
        sort_by = "created_at"
    direction = "DESC" if str(sort_order).lower() == "desc" else "ASC"

    # The join to the source header lets the scope clause reference its sites;
    # it's a 1:1 FK so the COUNT is unaffected when no scope is applied.
    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM interunit_transfer_in_header h "
        f"LEFT JOIN interunit_transfers_header t ON h.transfer_out_id = t.id WHERE {where}", *args)

    offset = (page - 1) * per_page
    rows = await conn.fetch(
        f"""
        SELECT
            h.id, h.transfer_out_id, h.transfer_out_no, h.grn_number,
            h.grn_date, h.receiving_warehouse, h.received_by, h.received_at,
            h.box_condition, h.condition_remarks, h.status,
            h.created_at, h.updated_at,
            COUNT(b.id) AS total_boxes_scanned,
            t.from_site AS from_warehouse, t.from_cold_unit
        FROM interunit_transfer_in_header h
        LEFT JOIN interunit_transfer_in_boxes b ON h.id = b.header_id
        LEFT JOIN interunit_transfers_header t ON h.transfer_out_id = t.id
        WHERE {where}
        GROUP BY h.id, t.from_site, t.from_cold_unit
        ORDER BY h.{sort_by} {direction}
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, per_page, offset,
    )

    records = []
    for row in rows:
        r = dict(row)
        item = _map_transfer_in_header(r)
        item["total_boxes_scanned"] = r.get("total_boxes_scanned") or 0
        records.append(item)

    return {
        "records": records,
        "total": total or 0,
        "page": page,
        "per_page": per_page,
        "total_pages": _total_pages(total or 0, per_page),
    }


async def get_transfer_in(conn, transfer_in_id: int, *, scope=None) -> dict:
    """One receipt (GRN) with its boxes. With `scope` (permissions.scope_warehouses), a
    receipt whose receiving warehouse and transfer-out From, To and cold units are none
    of the reader's sites is 404, as list_transfer_ins leaves it out."""
    row = await conn.fetchrow(
        """
        SELECT h.id, h.transfer_out_id, h.transfer_out_no, h.grn_number,
               h.grn_date, h.receiving_warehouse, h.received_by, h.received_at,
               h.box_condition, h.condition_remarks, h.status,
               h.inward_transaction_no, h.created_at, h.updated_at,
               t.from_site AS from_warehouse, t.to_site AS transfer_to_site, t.from_cold_unit
        FROM interunit_transfer_in_header h
        LEFT JOIN interunit_transfers_header t ON h.transfer_out_id = t.id
        WHERE h.id = $1
        """,
        transfer_in_id,
    )
    if not row or not permissions.in_scope(scope, row["receiving_warehouse"], row["from_warehouse"],
                                           row["transfer_to_site"], row["from_cold_unit"]):
        raise HTTPException(404, "Transfer IN not found")

    box_rows = await conn.fetch(
        """
        SELECT id, header_id, box_id, article, batch_number,
               lot_number, transaction_no, net_weight, gross_weight,
               scanned_at, is_matched, transfer_out_box_id, issue, line_index,
               inward_box_id
        FROM interunit_transfer_in_boxes
        WHERE header_id = $1
        ORDER BY scanned_at
        """,
        transfer_in_id,
    )
    boxes = [_map_transfer_in_box(dict(r)) for r in box_rows]

    result = _map_transfer_in_header(dict(row))
    result["boxes"] = boxes
    result["total_boxes_scanned"] = len(boxes)
    return result
