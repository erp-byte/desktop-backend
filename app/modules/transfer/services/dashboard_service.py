"""Transfer Summary dashboard reads (doc 04).

Ported from `transfer_backend_reference/.../transfer_dashboard_server.py`
(get_all_data + get_filter_options) from sync SQLAlchemy `text()` to asyncpg.

The reference loads ALL transfer-line records once and does every KPI / filter /
aggregation client-side. We keep that contract but add server-side warehouse
scoping (a user locked to A185 must only see rows where A185 is the source or
destination) — consistent with the other transfer list endpoints. `scope` is the
list returned by `permissions.scope_warehouses(user)`; empty means unrestricted
(admins / unscoped users see everything).

Porting notes:
  * `:named` / `.format()` params → positional `$N`.
  * SQLAlchemy Row attribute access → `dict(record)` + `.get()`.
  * ROUND(...::numeric, 2) comes back as Decimal → coerced via `float()`.
  * `issue->>'k'` JSONB text extraction is identical in asyncpg.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import HTTPException

# Only include transfers with actual line items (mirrors reference LINE_FILTER).
LINE_FILTER = (
    "(COALESCE(l.net_weight, 0) > 0 OR COALESCE(l.total_weight, 0) > 0 "
    "OR COALESCE(l.qty, 0) > 0)"
)

_NUMERIC = {"qty", "pack_size", "net_weight", "total_weight"}
_INT = {"transfer_id"}


def _parse_date(s: str) -> date:
    """ISO YYYY-MM-DD → date (asyncpg binds DATE params as date objects)."""
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid date '{s}'. Use YYYY-MM-DD.")


def _norm_wh(w: Any) -> str:
    """lower-case + drop hyphens so 'A-185'/'A185' compare equal — matches the
    REPLACE(LOWER(col),'-','') applied to the DB column."""
    return str(w).strip().lower().replace("-", "")


def _scope_clause(scope: Optional[list], param_idx: int) -> str:
    """SQL fragment restricting to rows whose from_site OR to_site is in `scope`.
    `h` is assumed to be the header alias. Empty/None scope → always-true."""
    return (
        f"(REPLACE(LOWER(COALESCE(h.from_site,'')),'-','') = ANY(${param_idx}::text[]) "
        f"OR REPLACE(LOWER(COALESCE(h.to_site,'')),'-','') = ANY(${param_idx}::text[]))"
    )


async def get_all_data(
    conn, *, scope: Optional[list] = None,
    from_date: Optional[str] = None, to_date: Optional[str] = None,
) -> dict:
    """All transfer line records joined with box counts, received status, and
    issue details. Only transfers with actual items. Scoped to `scope`.

    `from_date`/`to_date` (ISO YYYY-MM-DD) bound `stock_trf_date` so the client
    can load a recent window by default and widen on demand — both omitted means
    all-time."""
    norm_scope = [_norm_wh(w) for w in (scope or []) if str(w).strip()]
    where = [LINE_FILTER]
    args: list = []
    if norm_scope:
        args.append(norm_scope)
        where.append(_scope_clause(norm_scope, len(args)))
    if from_date:
        args.append(_parse_date(from_date))
        where.append(f"h.stock_trf_date >= ${len(args)}")
    if to_date:
        args.append(_parse_date(to_date))
        where.append(f"h.stock_trf_date <= ${len(args)}")

    sql = f"""
        SELECT
            h.id AS transfer_id,
            COALESCE(h.challan_no, '') AS challan_no,
            h.stock_trf_date::text AS transfer_date,
            TO_CHAR(h.stock_trf_date, 'YYYY-MM') AS transfer_month,
            COALESCE(h.from_site, '') AS from_warehouse,
            COALESCE(h.to_site, '') AS to_warehouse,
            COALESCE(h.vehicle_no, '') AS vehicle_no,
            COALESCE(h.driver_name, '') AS driver_name,
            COALESCE(h.status, '') AS status,
            COALESCE(h.created_by, '') AS created_by,
            COALESCE(h.remark, '') AS remark,
            COALESCE(l.item_desc_raw, '') AS item_description,
            COALESCE(l.item_category, '') AS item_category,
            COALESCE(l.sub_category, '') AS sub_category,
            COALESCE(l.rm_pm_fg_type, '') AS material_type,
            COALESCE(l.lot_number, '') AS lot_number,
            COALESCE(l.qty, 0) AS qty,
            COALESCE(l.uom, '') AS uom,
            COALESCE(l.pack_size, 0) AS pack_size,
            ROUND(COALESCE(l.net_weight, 0)::numeric, 2) AS net_weight,
            ROUND(COALESCE(l.total_weight, 0)::numeric, 2) AS total_weight
        FROM interunit_transfers_header h
        INNER JOIN interunit_transfers_lines l ON h.id = l.header_id
        WHERE {' AND '.join(where)}
        ORDER BY h.stock_trf_date DESC NULLS LAST
    """
    try:
        rows = await conn.fetch(sql, *args)
    except Exception as e:  # noqa: BLE001 — surface DB errors as 500 like the reference
        raise HTTPException(500, f"Failed to load transfer dashboard data: {e}")

    records: list[dict] = []
    for row in rows:
        r = dict(row)
        rec: dict = {}
        for c, v in r.items():
            if v is None:
                rec[c] = 0 if (c in _NUMERIC or c in _INT) else ""
            elif c in _NUMERIC:
                rec[c] = round(float(v), 2)
            elif c in _INT:
                rec[c] = int(v)
            else:
                rec[c] = str(v)
        records.append(rec)

    if not records:
        return {"records": [], "total": 0, "as_of_date": date.today().isoformat()}

    transfer_ids = list({rec["transfer_id"] for rec in records})

    # Box counts per transfer.
    box_rows = await conn.fetch(
        """
        SELECT header_id, COUNT(*) AS box_count
        FROM interunit_transfer_boxes
        WHERE header_id = ANY($1::int[])
        GROUP BY header_id
        """,
        transfer_ids,
    )
    box_counts = {int(r["header_id"]): int(r["box_count"]) for r in box_rows}

    # Transfer-in received status per transfer.
    tin_rows = await conn.fetch(
        """
        SELECT transfer_out_id, status AS tin_status
        FROM interunit_transfer_in_header
        WHERE transfer_out_id = ANY($1::int[])
        """,
        transfer_ids,
    )
    tin_map = {int(r["transfer_out_id"]): r["tin_status"] for r in tin_rows}

    # Issue details per transfer (from received boxes carrying a non-empty issue).
    issue_rows = await conn.fetch(
        """
        SELECT tih.transfer_out_id,
               tib.article,
               tib.issue->>'remarks' AS issue_remarks,
               tib.issue->>'actual_qty' AS actual_qty,
               tib.issue->>'actual_total_weight' AS actual_total_weight,
               COALESCE(tib.net_weight, 0) AS net_weight
        FROM interunit_transfer_in_boxes tib
        JOIN interunit_transfer_in_header tih ON tib.header_id = tih.id
        WHERE tih.transfer_out_id = ANY($1::int[])
          AND tib.issue IS NOT NULL
          AND tib.issue::text != 'null'
          AND tib.issue::text != '{}'
        ORDER BY tih.transfer_out_id, tib.article
        """,
        transfer_ids,
    )
    issue_map: dict[int, dict] = {}
    for r in issue_rows:
        tid = int(r["transfer_out_id"])
        entry = issue_map.setdefault(
            tid, {"issue_count": 0, "issue_items": [], "issue_weight": 0.0, "issue_details": []}
        )
        entry["issue_count"] += 1
        entry["issue_weight"] += float(r["net_weight"] or 0)
        article = r["article"]
        if article and article not in entry["issue_items"]:
            entry["issue_items"].append(article)
        entry["issue_details"].append({
            "article": article or "",
            "remarks": r["issue_remarks"] or "",
            "actual_qty": r["actual_qty"] or "",
            "actual_total_weight": r["actual_total_weight"] or "",
        })

    for rec in records:
        tid = rec["transfer_id"]
        rec["box_count"] = box_counts.get(tid, 0)
        rec["received_status"] = tin_map.get(tid, "Not Received")
        iss = issue_map.get(tid, {})
        rec["issue_count"] = iss.get("issue_count", 0)
        rec["issue_items"] = ", ".join(iss.get("issue_items", []))
        rec["issue_weight"] = round(iss.get("issue_weight", 0), 2)
        rec["issue_details"] = iss.get("issue_details", [])
        rec["has_issue"] = rec["issue_count"] > 0

    return {"records": records, "total": len(records), "as_of_date": date.today().isoformat()}


async def get_filter_options(conn, *, scope: Optional[list] = None) -> dict:
    """Distinct values for the filter chips. Scoped to `scope` (so a locked user
    only sees chip values that actually appear in their visible rows)."""
    norm_scope = [_norm_wh(w) for w in (scope or []) if str(w).strip()]

    def _scoped(base_where: str) -> tuple[str, list]:
        """Append the scope clause + return (where_sql, args)."""
        if norm_scope:
            return f"{base_where} AND {_scope_clause(norm_scope, 1)}", [norm_scope]
        return base_where, []

    result: dict[str, list] = {}

    w, a = _scoped(f"h.from_site IS NOT NULL AND h.from_site != '' AND {LINE_FILTER}")
    result["from_warehouses"] = [r[0] for r in await conn.fetch(
        f"""SELECT DISTINCT h.from_site FROM interunit_transfers_header h
            INNER JOIN interunit_transfers_lines l ON h.id = l.header_id
            WHERE {w} ORDER BY h.from_site""", *a)]

    w, a = _scoped(f"h.to_site IS NOT NULL AND h.to_site != '' AND {LINE_FILTER}")
    result["to_warehouses"] = [r[0] for r in await conn.fetch(
        f"""SELECT DISTINCT h.to_site FROM interunit_transfers_header h
            INNER JOIN interunit_transfers_lines l ON h.id = l.header_id
            WHERE {w} ORDER BY h.to_site""", *a)]

    w, a = _scoped("h.status IS NOT NULL AND h.status != ''")
    result["statuses"] = [r[0] for r in await conn.fetch(
        f"""SELECT DISTINCT h.status FROM interunit_transfers_header h
            WHERE {w} ORDER BY h.status""", *a)]

    w, a = _scoped(f"l.item_category IS NOT NULL AND l.item_category != '' AND {LINE_FILTER}")
    result["item_categories"] = [r[0] for r in await conn.fetch(
        f"""SELECT DISTINCT l.item_category FROM interunit_transfers_lines l
            INNER JOIN interunit_transfers_header h ON h.id = l.header_id
            WHERE {w} ORDER BY l.item_category""", *a)]

    w, a = _scoped(f"l.rm_pm_fg_type IS NOT NULL AND l.rm_pm_fg_type != '' AND {LINE_FILTER}")
    result["material_types"] = [r[0] for r in await conn.fetch(
        f"""SELECT DISTINCT l.rm_pm_fg_type FROM interunit_transfers_lines l
            INNER JOIN interunit_transfers_header h ON h.id = l.header_id
            WHERE {w} ORDER BY l.rm_pm_fg_type""", *a)]

    w, a = _scoped("h.created_by IS NOT NULL AND h.created_by != ''")
    result["created_by"] = [r[0] for r in await conn.fetch(
        f"""SELECT DISTINCT h.created_by FROM interunit_transfers_header h
            WHERE {w} ORDER BY h.created_by""", *a)]

    return result
