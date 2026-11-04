"""In-transit (pending_transfer_stock) read + backfill.

Ported from `transfer_backend_reference/.../pending_stock_tools.py`
(list_pending_transfers, backfill_pending_from_existing_transfers) to asyncpg.

`list_pending_transfers` is enriched with `unallocated_boxes` + `updated_ts`
(from interunit_transfers_header.edited_at) which the dashboard's Pending modal
renders as the "short" / "Edited" row badges (the doc-mandated contract).
backfill (P3b) parks the boxes of existing transfers into pending — record only,
like a dispatch: stock rows are read and copied, never deducted.

Sites, in records, chips, filters and the user's scope, are sites of the one table
(app.core.sites): every spelling of a site is that site's canonical name ('Warehouse
A185' and 'A-185' are A185, 'ESKIMO' is Eskimo, 'Savla Bond' is a store of its own).

A transfer without in-transit rows is listed by its header when R3 has it in transit:
its status Dispatch / Partial, or IMS's older In Transit / Completed (any case), and no
Received receipt in either ledger (_header_only_where).
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.core.sites import canonical_site, is_cold_site, same_site, scope_keys, site_key
from app.modules.transfer.services import lookup_service
from app.modules.transfer.services.query_service import site_scope_sql


def _normalize_site(raw: Optional[str]) -> str:
    """The canonical name of a site ('Warehouse A68' → 'A68', 'savla d-39 cold' →
    'Savla D-39', 'dev_int' → 'Dev Int'); an unknown name comes back trimmed, '' for none."""
    return canonical_site(raw)


def _storage_type(site: Optional[str]) -> str:
    """'cold' for a cold store (or the legacy 'Cold Storage' label), else 'warehouse'."""
    return "cold" if is_cold_site(site) else "warehouse"


def _chips(names) -> dict[str, str]:
    """{name: its chip} for the stored site names given: the canonical name, one chip per
    site. Spellings of a site the table does not know that differ only in case or
    punctuation ('savla' / 'Savla') share one chip, the first of them in order."""
    label: dict[str, str] = {}
    for name in sorted({_normalize_site(n) for n in names if n} - {""}):
        label.setdefault(site_key(name), name)
    return {n: label[site_key(_normalize_site(n))] for n in names if n and _normalize_site(n)}


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"))


# A transfer's own status says it is in transit, as R3 reads it
# (lookup_service.is_in_transit_status: Dispatch / Partial, or IMS's older In Transit /
# Completed; case and spaces aside). Constants, so inlined.
_IN_TRANSIT_SQL = ("LOWER(BTRIM(COALESCE(h.status, ''))) IN ("
                   + ", ".join(f"'{s}'" for s in sorted(lookup_service._IN_TRANSIT)) + ")")


async def _header_only_where(conn) -> str:
    """Which transfers the in-transit list shows by their header alone: in transit by
    their status (_IN_TRANSIT_SQL), with no in-transit rows, and with no Received receipt
    in either ledger (a GRN, or IMS's cold receipt), which R3 counts as received whatever
    the status says (lookup_service)."""
    gates = [
        _IN_TRANSIT_SQL,
        "NOT EXISTS (SELECT 1 FROM pending_transfer_stock p "
        "WHERE p.transfer_out_id = h.id AND p.status = 'In Transit')",
        "NOT EXISTS (SELECT 1 FROM interunit_transfer_in_header ti "
        "WHERE ti.transfer_out_id = h.id AND LOWER(BTRIM(COALESCE(ti.status, ''))) = 'received')",
    ]
    if await _table_exists(conn, "cold_transfer_in_headers"):
        gates.append("NOT EXISTS (SELECT 1 FROM cold_transfer_in_headers ch "
                     "WHERE ch.transfer_out_id = h.id AND LOWER(BTRIM(COALESCE(ch.status, ''))) = 'received')")
    return " AND ".join(gates)


async def list_pending_transfers(conn, *, from_site=None, to_site=None, company=None,
                                 from_date=None, to_date=None, search=None, scope=None) -> dict:
    if not await _table_exists(conn, "pending_transfer_stock"):
        return {"records": [], "total": 0,
                "filter_options": {"from_sites": [], "to_sites": [],
                                   "from_site_counts": {}, "to_site_counts": {}}}

    # Company scoping intentionally disabled (single unified CFPL+CDPL view).
    _ = company

    # from_site / to_site are applied to the records below, as sites (every spelling).
    outer_clauses: list = []
    args: list = []
    if from_date:
        args.append(from_date)
        outer_clauses.append(f"dispatched_at::date >= ${len(args)}::date")
    if to_date:
        args.append(to_date)
        outer_clauses.append(f"dispatched_at::date <= ${len(args)}::date")
    if search:
        args.append(f"%{search}%")
        outer_clauses.append(f"transfer_out_challan_no ILIKE ${len(args)}")
    if scope:
        # From or To one of the caller's sites, or with boxes from one of them (each unit
        # of the transfer's from_cold_unit), as list_transfers scopes it.
        args.append(scope_keys(scope))
        outer_clauses.append(site_scope_sql(f"${len(args)}", sites=("from_site", "to_site"),
                                            units=("from_cold_unit",)))

    outer_where = ("WHERE " + " AND ".join(outer_clauses)) if outer_clauses else ""
    # A transfer with no in-transit rows is listed when R3 has its boxes in transit.
    header_only = await _header_only_where(conn)

    # A transfer with no in-transit rows has no stored storage types: they are worked out
    # from its sites below (_storage_type), so both kinds of row type a site alike.
    rows = await conn.fetch(
        f"""
        WITH combined AS (
            SELECT
                pts.transfer_out_id,
                pts.transfer_out_challan_no,
                MIN(pts.dispatched_at)              AS dispatched_at,
                pts.from_site,
                pts.to_site,
                pts.from_company,
                pts.to_company,
                pts.from_storage_type,
                pts.to_storage_type,
                COUNT(*)                            AS total_boxes,
                COALESCE(SUM(pts.no_of_cartons), 0) AS total_cartons,
                COALESCE(SUM(pts.weight_kg), 0)     AS total_kg,
                MIN(pts.dispatched_by)              AS dispatched_by,
                MIN(ith.status)                     AS header_status,
                MIN(ith.unallocated_boxes)          AS unallocated_boxes,
                MAX(ith.edited_at)                  AS updated_ts,
                MIN(ith.from_cold_unit)             AS from_cold_unit
            FROM pending_transfer_stock pts
            JOIN interunit_transfers_header ith
                ON ith.id = pts.transfer_out_id AND ith.status != 'Received'
            WHERE pts.status = 'In Transit'
            GROUP BY pts.transfer_out_id, pts.transfer_out_challan_no,
                     pts.from_site, pts.to_site,
                     pts.from_company, pts.to_company,
                     pts.from_storage_type, pts.to_storage_type

            UNION ALL

            SELECT
                h.id                                AS transfer_out_id,
                h.challan_no                        AS transfer_out_challan_no,
                h.created_ts                        AS dispatched_at,
                h.from_site,
                h.to_site,
                NULL                                AS from_company,
                NULL                                AS to_company,
                NULL::text                          AS from_storage_type,
                NULL::text                          AS to_storage_type,
                CASE WHEN (SELECT COUNT(*) FROM interunit_transfer_boxes b WHERE b.header_id = h.id) > 0
                     THEN (SELECT COUNT(*) FROM interunit_transfer_boxes b WHERE b.header_id = h.id)
                     ELSE COALESCE((SELECT SUM(l.qty)::int FROM interunit_transfers_lines l WHERE l.header_id = h.id), 0)
                END                                 AS total_boxes,
                CASE WHEN (SELECT COUNT(*) FROM interunit_transfer_boxes b WHERE b.header_id = h.id) > 0
                     THEN (SELECT COUNT(*) FROM interunit_transfer_boxes b WHERE b.header_id = h.id)
                     ELSE COALESCE((SELECT SUM(l.qty)::int FROM interunit_transfers_lines l WHERE l.header_id = h.id), 0)
                END                                 AS total_cartons,
                CASE WHEN (SELECT COUNT(*) FROM interunit_transfer_boxes b WHERE b.header_id = h.id) > 0
                     THEN COALESCE((SELECT SUM(CAST(b.net_weight AS NUMERIC)) FROM interunit_transfer_boxes b WHERE b.header_id = h.id), 0)
                     ELSE COALESCE((SELECT SUM(CAST(l.net_weight AS NUMERIC)) FROM interunit_transfers_lines l WHERE l.header_id = h.id), 0)
                END                                 AS total_kg,
                COALESCE(h.created_by, '')          AS dispatched_by,
                h.status                            AS header_status,
                h.unallocated_boxes                 AS unallocated_boxes,
                h.edited_at                         AS updated_ts,
                h.from_cold_unit                    AS from_cold_unit
            FROM interunit_transfers_header h
            WHERE {header_only}
        )
        SELECT * FROM combined
        {outer_where}
        ORDER BY dispatched_at DESC
        """,
        *args,
    )

    # Filter chips: distinct sites from in-transit data ∪ active warehouse_sites.
    site_rows = await conn.fetch(
        f"""
        SELECT pts.from_site, pts.to_site, COUNT(DISTINCT pts.transfer_out_id) AS n
        FROM pending_transfer_stock pts
        JOIN interunit_transfers_header ith ON ith.id = pts.transfer_out_id AND ith.status != 'Received'
        WHERE pts.status = 'In Transit'
        GROUP BY pts.from_site, pts.to_site
        UNION ALL
        SELECT h.from_site, h.to_site, COUNT(DISTINCT h.id) AS n
        FROM interunit_transfers_header h
        WHERE {header_only}
        GROUP BY h.from_site, h.to_site
        """,
    )
    # The registered sites, by the code requests store (site_code), else their name.
    registered: list = []
    if await _table_exists(conn, "warehouse_sites"):
        try:
            ws_rows = await conn.fetch(
                "SELECT site_code, site_name FROM warehouse_sites WHERE COALESCE(is_active, true) = true "
                "ORDER BY site_name")
            registered = [w["site_code"] or w["site_name"] for w in ws_rows if w["site_code"] or w["site_name"]]
        except Exception:
            registered = []

    chip = _chips([*(r["from_site"] for r in rows), *(r["to_site"] for r in rows),
                   *(s["from_site"] for s in site_rows), *(s["to_site"] for s in site_rows), *registered])

    records = []
    for row in rows:
        r = dict(row)
        if from_site and not same_site(r["from_site"], from_site):
            continue
        if to_site and not same_site(r["to_site"], to_site):
            continue
        records.append({
            "transfer_out_id": r["transfer_out_id"],
            "transfer_out_challan_no": r["transfer_out_challan_no"],
            "dispatched_at": r["dispatched_at"].isoformat() if r["dispatched_at"] else None,
            "from_site": chip.get(r["from_site"], ""),
            "to_site": chip.get(r["to_site"], ""),
            "from_company": r["from_company"],
            "to_company": r["to_company"],
            "from_storage_type": r["from_storage_type"] or _storage_type(r["from_site"]),
            "to_storage_type": r["to_storage_type"] or _storage_type(r["to_site"]),
            "total_boxes": int(r["total_boxes"] or 0),
            "total_cartons": float(r["total_cartons"] or 0),
            "total_kg": float(r["total_kg"] or 0),
            "dispatched_by": r["dispatched_by"] or "",
            "status": "In Transit",
            "header_status": r.get("header_status") or "Dispatch",
            "unallocated_boxes": int(r["unallocated_boxes"]) if r.get("unallocated_boxes") is not None else None,
            "updated_ts": r["updated_ts"].isoformat() if r.get("updated_ts") else None,
        })

    from_counts: dict = {}
    to_counts: dict = {}
    for sr in site_rows:
        if sr["from_site"] in chip:
            k = chip[sr["from_site"]]
            from_counts[k] = from_counts.get(k, 0) + int(sr["n"] or 0)
        if sr["to_site"] in chip:
            k = chip[sr["to_site"]]
            to_counts[k] = to_counts.get(k, 0) + int(sr["n"] or 0)
    all_sites = [chip[s] for s in registered if s in chip]

    from_chips = sorted({*from_counts.keys(), *all_sites})
    to_chips = sorted({*to_counts.keys(), *all_sites})

    return {
        "records": records,
        "total": len(records),
        "filter_options": {
            "from_sites": from_chips,
            "to_sites": to_chips,
            "from_site_counts": from_counts,
            "to_site_counts": to_counts,
        },
    }


async def pending_by_lot(conn, *, lot_no=None, item_description=None,
                         from_site=None, from_company=None, scope=None) -> dict:
    """In-transit (pending) cartons/kg + per-challan breakdown for a lot+item, used by the
    cold-stock picker's '+N in transit' hover (doc 08). Ported from pending_stock_tools.
    NOTE: record only — a dispatch copies its boxes and leaves their cold_stocks rows
    (IMS keeps that table), so the cold-stock count may already include these in-transit
    boxes. This is display context only; callers do not subtract it from the displayed
    stock.

    Only rows of a transfer that exists and is not Received count, as in
    list_pending_transfers: a stale row of a received or deleted transfer is not in
    transit. from_site matches every spelling of the site; with `scope`
    (permissions.scope_warehouses) only transfers from, to or (cold units) out of one of
    the caller's sites count."""
    if not await _table_exists(conn, "pending_transfer_stock"):
        return {"pending_cartons": 0.0, "pending_kg": 0.0, "box_count": 0, "transfers": []}

    clauses = ["pts.status = 'In Transit'"]
    args: list = []
    if lot_no:
        args.append(lot_no); clauses.append(f"pts.lot_no = ${len(args)}")
    if item_description:
        args.append(item_description); clauses.append(f"pts.item_description = ${len(args)}")
    if from_site:
        args.append(scope_keys(from_site))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("pts.from_site",)))
    if from_company:
        args.append(str(from_company).lower()); clauses.append(f"pts.from_company = ${len(args)}")
    if scope:
        args.append(scope_keys(scope))
        clauses.append(site_scope_sql(f"${len(args)}", sites=("pts.from_site", "pts.to_site"),
                                      units=("h.from_cold_unit",)))
    where = " AND ".join(clauses)
    in_transit = ("FROM pending_transfer_stock pts "
                  "JOIN interunit_transfers_header h ON h.id = pts.transfer_out_id AND h.status <> 'Received'")

    total = await conn.fetchrow(
        f"""SELECT COALESCE(SUM(pts.no_of_cartons), 0) AS pending_cartons,
                   COALESCE(SUM(pts.weight_kg), 0)     AS pending_kg,
                   COUNT(*)                             AS box_count
            {in_transit} WHERE {where}""",
        *args,
    )

    rows = await conn.fetch(
        f"""
        SELECT pts.transfer_out_id, pts.transfer_out_challan_no,
               MIN(pts.dispatched_at)             AS dispatched_at,
               pts.from_site, pts.to_site, pts.from_storage_type, pts.to_storage_type,
               COUNT(*)                            AS box_count,
               COALESCE(SUM(pts.no_of_cartons), 0) AS cartons,
               COALESCE(SUM(pts.weight_kg), 0)     AS weight_kg,
               MIN(pts.dispatched_by)             AS dispatched_by,
               MIN(h.vehicle_no)                  AS vehicle_no,
               MIN(h.driver_name)                 AS driver_name,
               MIN(h.approved_by)                 AS approved_by,
               MIN(h.remark)                      AS remark,
               MIN(h.reason_code)                 AS reason_code,
               MIN(h.status)                      AS transfer_status,
               BOOL_OR(COALESCE(h.has_variance, false)) AS has_variance,
               MAX(h.edited_at)                   AS updated_ts
        {in_transit}
        WHERE {where}
        GROUP BY pts.transfer_out_id, pts.transfer_out_challan_no,
                 pts.from_site, pts.to_site, pts.from_storage_type, pts.to_storage_type
        ORDER BY MIN(pts.dispatched_at) DESC
        """,
        *args,
    )

    return {
        "pending_cartons": float(total["pending_cartons"] or 0),
        "pending_kg": float(total["pending_kg"] or 0),
        "box_count": int(total["box_count"] or 0),
        "transfers": [{
            "transfer_out_id": r["transfer_out_id"],
            "challan_no": r["transfer_out_challan_no"],
            "dispatched_at": r["dispatched_at"].isoformat() if r["dispatched_at"] else None,
            "from_site": _normalize_site(r["from_site"]),
            "to_site": _normalize_site(r["to_site"]),
            "from_storage_type": r["from_storage_type"] or _storage_type(r["from_site"]),
            "to_storage_type": r["to_storage_type"] or _storage_type(r["to_site"]),
            "box_count": int(r["box_count"] or 0),
            "cartons": float(r["cartons"] or 0),
            "weight_kg": float(r["weight_kg"] or 0),
            "dispatched_by": r["dispatched_by"] or "",
            "vehicle_no": r["vehicle_no"] or "",
            "driver_name": r["driver_name"] or "",
            "approved_by": r["approved_by"] or "",
            "remark": r["remark"] or "",
            "reason_code": r["reason_code"] or "",
            "transfer_status": r["transfer_status"] or "",
            "has_variance": bool(r["has_variance"]),
            "updated_ts": r["updated_ts"].isoformat() if r["updated_ts"] else None,
        } for r in rows],
    }


async def backfill_pending_from_existing_transfers(conn) -> dict:
    # Parks boxes from existing transfers into pending (copies; no stock table is written).
    from app.modules.transfer.services import reversal_service
    return await reversal_service.backfill_pending_from_existing_transfers(conn)
