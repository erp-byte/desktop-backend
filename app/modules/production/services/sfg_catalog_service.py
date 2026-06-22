"""SFG catalogue read layer — thin queries over the 055 derived views.

These back the design-ref §8.1/§9.2 "standalone catalogue artifacts":
    sfg_master      -> the dedicated SFG catalogue (from all_sku item_type='sfg')
    sfg_where_used  -> reverse index SFG#### -> consuming FGs
    fg_sfg_binding  -> FG↔stage↔SFG#### binding (per routing step)

All three are VIEWS (no second physical copy), so these are pure SELECTs and there
is no write path here. Entity scoping is applied where the underlying row carries
an FG entity (where-used / binding); sfg_master itself is entity-agnostic
(all_sku has no entity column — the SFG catalogue is shared across cfpl/cdpl).
"""

import logging

logger = logging.getLogger(__name__)


async def list_sfg_master(conn, *, search: str | None = None,
                          sfg_code: str | None = None,
                          page: int = 1, page_size: int = 50) -> dict:
    """The dedicated SFG catalogue (design ref §8.1), paginated.

    `search` matches sfg_code OR sfg_name (case-insensitive); `sfg_code` is an
    exact-code filter (canonical SFG#### key — no substring over-match)."""
    # Clamp defensively — the HTTP layer validates page/page_size, but a direct
    # caller passing page_size=0 would make total_pages divide by zero (and
    # LIMIT 0 return nothing) and a page<1 would yield a negative OFFSET.
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    conditions: list[str] = []
    params: list = []
    idx = 1
    if sfg_code:
        conditions.append(f"sfg_code = ${idx}"); params.append(sfg_code); idx += 1
    if search:
        conditions.append(f"(sfg_code ILIKE ${idx} OR sfg_name ILIKE ${idx})")
        params.append(f"%{search}%"); idx += 1
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size

    total = await conn.fetchval(f"SELECT COUNT(*) FROM sfg_master{where}", *params)
    rows = await conn.fetch(
        f"""
        SELECT sku_id, sfg_code, sfg_name, item_group, sub_group, uom,
               sale_group, gst, produced_at_stage, consumed_by_fg_count,
               base_recipe, create_wip_operation, sfg_origin,
               va_article, primary_bu, created_at
        FROM sfg_master{where}
        ORDER BY sfg_code
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params, page_size, offset,
    )
    return {
        "results": [dict(r) for r in rows],
        "pagination": {"page": page, "page_size": page_size, "total": total or 0,
                       "total_pages": ((total + page_size - 1) // page_size) if total else 0},
    }


async def get_sfg_where_used(conn, sfg_code: str, *, entity: str | None = None) -> dict:
    """Reverse index (design ref §9.2): which FGs consume this SFG####.

    Exact-code match on the canonical key. Optional entity filter (the consuming
    FG's entity) for scope enforcement."""
    conditions = ["sfg_code = $1"]
    params: list = [sfg_code]
    idx = 2
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    where = " AND ".join(conditions)
    rows = await conn.fetch(
        f"""
        SELECT sfg_code, sfg_name, bom_id, fg_sku_name, entity,
               consumed_at_step, consumed_at_stage
        FROM sfg_where_used
        WHERE {where}
        ORDER BY fg_sku_name, consumed_at_step
        """,
        *params,
    )
    return {"sfg_code": sfg_code, "consumed_by": [dict(r) for r in rows]}


async def list_wip_stock(conn, entity: str, *, search: str | None = None,
                         page: int = 1, page_size: int = 50) -> dict:
    """WIP/SFG on-hand stock, grouped by SFG#### (for the WIP-Stock screen).

    Sums AVAILABLE, in-quantity WIP batches per sfg_code (PT1/057 canonical key),
    joined to sfg_master for the article name. Entity-scoped. `search` matches the
    code or the name."""
    page = max(1, page)  # defensive: avoid /0 in total_pages, negative OFFSET
    page_size = max(1, min(page_size, 500))
    conditions = ["b.item_type = 'wip'", "b.status = 'AVAILABLE'",
                  "b.current_qty_kg > 0", "b.entity = $1", "b.sfg_code IS NOT NULL"]
    params: list = [entity]
    idx = 2
    if search:
        conditions.append(f"(b.sfg_code ILIKE ${idx} OR m.sfg_name ILIKE ${idx})")
        params.append(f"%{search}%"); idx += 1
    where = " AND ".join(conditions)
    offset = (page - 1) * page_size

    total = await conn.fetchval(
        f"SELECT COUNT(DISTINCT b.sfg_code) FROM inventory_batch b "
        f"LEFT JOIN sfg_master m ON m.sfg_code = b.sfg_code WHERE {where}",
        *params,
    )
    rows = await conn.fetch(
        f"""
        SELECT b.sfg_code,
               MAX(m.sfg_name)                       AS sfg_name,
               MAX(m.base_recipe)                    AS base_recipe,
               COALESCE(SUM(b.current_qty_kg), 0)    AS total_qty_kg,
               COUNT(*)                              AS batch_count,
               MIN(b.inward_date)                    AS oldest_inward,
               ARRAY_AGG(DISTINCT b.floor_id)        AS floors
        FROM inventory_batch b
        LEFT JOIN sfg_master m ON m.sfg_code = b.sfg_code
        WHERE {where}
        GROUP BY b.sfg_code
        ORDER BY total_qty_kg DESC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params, page_size, offset,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["total_qty_kg"] = float(d["total_qty_kg"] or 0)
        if d.get("oldest_inward"):
            d["oldest_inward"] = str(d["oldest_inward"])
        d["floors"] = [f for f in (d.get("floors") or []) if f]
        out.append(d)
    return {
        "results": out,
        "pagination": {"page": page, "page_size": page_size, "total": total or 0,
                       "total_pages": ((total + page_size - 1) // page_size) if total else 0},
    }


async def get_fg_sfg_binding(conn, *, bom_id: int | None = None,
                             fg_sku_name: str | None = None,
                             sfg_code: str | None = None,
                             entity: str | None = None) -> dict:
    """FG↔stage↔SFG#### binding (design ref §9.2), filterable by FG (bom_id or
    name), by SFG code, and/or entity. At least one of bom_id/fg_sku_name/sfg_code
    is expected for a focused result; with none it returns the whole binding."""
    conditions: list[str] = []
    params: list = []
    idx = 1
    if bom_id is not None:
        conditions.append(f"bom_id = ${idx}"); params.append(bom_id); idx += 1
    if fg_sku_name:
        conditions.append(f"fg_sku_name ILIKE ${idx}"); params.append(f"%{fg_sku_name}%"); idx += 1
    if sfg_code:
        conditions.append(f"sfg_code = ${idx}"); params.append(sfg_code); idx += 1
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = await conn.fetch(
        f"""
        SELECT bom_id, fg_sku_name, entity, customer_name, step_number,
               process_name, practical_operation, stage_bucket,
               input_kind, input_code, output_kind, output_code,
               sfg_code, binding_role
        FROM fg_sfg_binding{where}
        ORDER BY bom_id, step_number
        """,
        *params,
    )
    return {"results": [dict(r) for r in rows]}
