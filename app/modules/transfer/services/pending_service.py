"""In-transit (pending_transfer_stock) read + backfill.

Ported from `transfer_backend_reference/.../pending_stock_tools.py`
(list_pending_transfers, backfill_pending_from_existing_transfers) to asyncpg.

`list_pending_transfers` is enriched with `unallocated_boxes` + `updated_ts`
(from interunit_transfers_header.edited_at) which the dashboard's Pending modal
renders as the "short" / "Edited" row badges (the doc-mandated contract).
backfill is a destructive inventory operation — implemented in P3b.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

# Cold-unit alias map (canonical → DB variants), reconstructed from
# inward_tools._WAREHOUSE_ALIASES (the reference's `shared/` pkg isn't vendored).
_SITE_ALIASES: dict[str, list[str]] = {
    "Savla D-39": ["savla d-39", "savla d39", "savla-d39", "savla-d-39",
                   "savla d-39 cold", "savla d39 cold", "old savla", "savla bond",
                   "d-39", "d39", "savla"],
    "Savla D-514": ["savla d-514", "savla d514", "savla-d514", "savla-d-514",
                    "savla d-514 cold", "savla d514 cold", "new savla", "d-514", "d514"],
    "Rishi": ["rishi", "rishi cold", "rishi cold storage", "rishi cold storage pvt ltd"],
    "Supreme": ["supreme", "supreme cold", "supreme cold storage"],
}
_SITE_ALIAS_TO_CANON: dict[str, str] = {
    alias: canon for canon, aliases in _SITE_ALIASES.items() for alias in aliases
}


def _normalize_site(raw: Optional[str]) -> str:
    """Collapse warehouse name variants to canonical codes
    (e.g. 'Warehouse A68' → 'A68', 'savla d-39 cold' → 'Savla D-39')."""
    if not raw:
        return raw or ""
    key = raw.strip().lower().replace("_", " ")
    if key in _SITE_ALIAS_TO_CANON:
        return _SITE_ALIAS_TO_CANON[key]
    if key.startswith("warehouse "):
        return raw.strip()[len("warehouse "):].strip()
    return raw.strip()


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"))


async def list_pending_transfers(conn, *, from_site=None, to_site=None, company=None,
                                 from_date=None, to_date=None, search=None, scope=None) -> dict:
    if not await _table_exists(conn, "pending_transfer_stock"):
        return {"records": [], "total": 0,
                "filter_options": {"from_sites": [], "to_sites": [],
                                   "from_site_counts": {}, "to_site_counts": {}}}

    # Company scoping intentionally disabled (single unified CFPL+CDPL view).
    _ = company

    outer_clauses: list = []
    args: list = []
    if from_site:
        args.append(from_site)
        outer_clauses.append(f"from_site = ${len(args)}")
    if to_site:
        args.append(to_site)
        outer_clauses.append(f"to_site = ${len(args)}")
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
        args.append([str(w).strip().lower().replace("-", "") for w in scope])
        n = len(args)
        outer_clauses.append(
            f"(REPLACE(LOWER(from_site),'-','') = ANY(${n}::text[]) "
            f"OR REPLACE(LOWER(to_site),'-','') = ANY(${n}::text[]))")

    outer_where = ("WHERE " + " AND ".join(outer_clauses)) if outer_clauses else ""

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
                MAX(ith.edited_at)                  AS updated_ts
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
                CASE WHEN LOWER(COALESCE(h.from_site,'')) SIMILAR TO
                     '%(cold|rishi|savla|supreme)%' THEN 'cold' ELSE 'warehouse' END
                                                    AS from_storage_type,
                CASE WHEN LOWER(COALESCE(h.to_site,'')) SIMILAR TO
                     '%(cold|rishi|savla|supreme)%' THEN 'cold' ELSE 'warehouse' END
                                                    AS to_storage_type,
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
                h.edited_at                         AS updated_ts
            FROM interunit_transfers_header h
            WHERE h.status IN ('Dispatch', 'Partial')
              AND NOT EXISTS (
                  SELECT 1 FROM pending_transfer_stock p
                  WHERE p.transfer_out_id = h.id AND p.status = 'In Transit'
              )
        )
        SELECT * FROM combined
        {outer_where}
        ORDER BY dispatched_at DESC
        """,
        *args,
    )

    records = []
    for row in rows:
        r = dict(row)
        records.append({
            "transfer_out_id": r["transfer_out_id"],
            "transfer_out_challan_no": r["transfer_out_challan_no"],
            "dispatched_at": r["dispatched_at"].isoformat() if r["dispatched_at"] else None,
            "from_site": _normalize_site(r["from_site"]),
            "to_site": _normalize_site(r["to_site"]),
            "from_company": r["from_company"],
            "to_company": r["to_company"],
            "from_storage_type": r["from_storage_type"],
            "to_storage_type": r["to_storage_type"],
            "total_boxes": int(r["total_boxes"] or 0),
            "total_cartons": float(r["total_cartons"] or 0),
            "total_kg": float(r["total_kg"] or 0),
            "dispatched_by": r["dispatched_by"] or "",
            "status": "In Transit",
            "header_status": r.get("header_status") or "Dispatch",
            "unallocated_boxes": int(r["unallocated_boxes"]) if r.get("unallocated_boxes") is not None else None,
            "updated_ts": r["updated_ts"].isoformat() if r.get("updated_ts") else None,
        })

    # Filter chips: distinct sites from in-transit data ∪ active warehouse_sites.
    site_rows = await conn.fetch(
        """
        SELECT pts.from_site, pts.to_site, COUNT(DISTINCT pts.transfer_out_id) AS n
        FROM pending_transfer_stock pts
        JOIN interunit_transfers_header ith ON ith.id = pts.transfer_out_id AND ith.status != 'Received'
        WHERE pts.status = 'In Transit'
        GROUP BY pts.from_site, pts.to_site
        UNION ALL
        SELECT h.from_site, h.to_site, COUNT(DISTINCT h.id) AS n
        FROM interunit_transfers_header h
        WHERE h.status IN ('Dispatch', 'Partial')
          AND NOT EXISTS (
              SELECT 1 FROM pending_transfer_stock p WHERE p.transfer_out_id = h.id AND p.status = 'In Transit'
          )
        GROUP BY h.from_site, h.to_site
        """,
    )
    from_counts: dict = {}
    to_counts: dict = {}
    for sr in site_rows:
        if sr["from_site"]:
            k = _normalize_site(sr["from_site"])
            from_counts[k] = from_counts.get(k, 0) + int(sr["n"] or 0)
        if sr["to_site"]:
            k = _normalize_site(sr["to_site"])
            to_counts[k] = to_counts.get(k, 0) + int(sr["n"] or 0)

    all_sites: list = []
    if await _table_exists(conn, "warehouse_sites"):
        try:
            ws_rows = await conn.fetch(
                "SELECT site_name FROM warehouse_sites WHERE COALESCE(is_active, true) = true ORDER BY site_name")
            all_sites = [_normalize_site(w["site_name"]) for w in ws_rows if w["site_name"]]
        except Exception:
            all_sites = []

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


async def backfill_pending_from_existing_transfers(conn) -> dict:
    # Destructive: parks boxes from existing transfers into pending + deducts source.
    from app.modules.transfer.services import reversal_service
    return await reversal_service.backfill_pending_from_existing_transfers(conn)
