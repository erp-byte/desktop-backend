"""Dropdown / lookup reads for the New Request form (doc 05).

Ported from `transfer_backend_reference/.../interunit_tools.py`
(get_warehouse_sites, categorial_global_search, categorial_dropdown) to asyncpg.
The cascading dropdowns + quick search are backed by `all_sku`
(item_type → item_group → sub_group → particulars), keeping the form
self-contained (no dependency on the inward service)."""
from __future__ import annotations

from typing import Optional

_SKU = "public.all_sku"

# Material priority: RM → FG → PM (mirrors reference ordering).
_MT_ORDER = """
    CASE LOWER(mt)
        WHEN 'rm' THEN 1 WHEN 'fg' THEN 2 WHEN 'pm' THEN 3 ELSE 4
    END
"""


def _uom(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def get_warehouse_sites(conn, active_only: bool) -> list[dict]:
    where = "WHERE COALESCE(is_active, true) = true" if active_only else ""
    rows = await conn.fetch(
        f"""
        SELECT id, site_code, site_name, is_active
        FROM warehouse_sites
        {where}
        ORDER BY site_code ASC
        """
    )
    return [
        {"id": r["id"], "site_code": r["site_code"], "site_name": r["site_name"], "is_active": r["is_active"]}
        for r in rows
    ]


async def categorial_search(conn, search: Optional[str], limit: int, offset: int) -> dict:
    """Global search on all_sku.particulars — bypasses the hierarchy."""
    term = search.strip() if search else None
    args: list = []
    where = "1=1"
    if term:
        args.append(f"%{term.lower()}%")
        where = "LOWER(particulars) LIKE $1"

    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT UPPER(particulars), UPPER(item_type) FROM {_SKU} WHERE {where}) t",
        *args,
    )

    rows = await conn.fetch(
        f"""
        SELECT desc_upper, mt, grp, sc, uom FROM (
            SELECT DISTINCT ON (UPPER(particulars), UPPER(item_type))
                   UPPER(particulars) AS desc_upper,
                   UPPER(item_type) AS mt,
                   UPPER(item_group) AS grp,
                   UPPER(sub_group) AS sc,
                   uom
            FROM {_SKU}
            WHERE {where}
            ORDER BY UPPER(particulars) ASC, UPPER(item_type) ASC
        ) sub
        ORDER BY {_MT_ORDER.replace('mt', 'sub.mt')}, sub.desc_upper ASC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, limit, offset,
    )

    items = [
        {
            "id": idx + 1 + offset,
            "item_description": r["desc_upper"] or "",
            "material_type": r["mt"],
            "group": r["grp"],
            "sub_group": r["sc"],
            "uom": _uom(r["uom"]),
        }
        for idx, r in enumerate(rows)
    ]
    return {
        "items": items,
        "meta": {
            "total_items": total, "limit": limit, "offset": offset,
            "search": term, "has_more": (offset + limit) < (total or 0),
        },
    }


async def categorial_dropdown(
    conn, material_type: Optional[str], item_category: Optional[str],
    sub_category: Optional[str], search: Optional[str], limit: int, offset: int,
) -> dict:
    """Cascading dropdown: item_type → item_group → sub_group → particulars."""
    mt = material_type.strip() if material_type else None
    ic = item_category.strip() if item_category else None
    sc = sub_category.strip() if sub_category else None
    term = search.strip() if search else None

    material_types = [r["mt"] for r in await conn.fetch(
        f"""
        SELECT mt FROM (
            SELECT DISTINCT UPPER(item_type) AS mt FROM {_SKU}
            WHERE item_type IS NOT NULL AND UPPER(item_type) IN ('RM','PM','FG')
        ) sub
        ORDER BY {_MT_ORDER.replace('mt', 'sub.mt')}
        """
    )]

    item_categories: list = []
    if mt:
        item_categories = [r["grp"] for r in await conn.fetch(
            f"""
            SELECT DISTINCT UPPER(item_group) AS grp FROM {_SKU}
            WHERE UPPER(item_type) = UPPER($1) AND item_group IS NOT NULL
            ORDER BY grp ASC
            """, mt,
        )]

    sub_categories: list = []
    if mt and ic:
        sub_categories = [r["sc"] for r in await conn.fetch(
            f"""
            SELECT DISTINCT UPPER(sub_group) AS sc FROM {_SKU}
            WHERE UPPER(item_type) = UPPER($1) AND UPPER(item_group) = UPPER($2)
              AND sub_group IS NOT NULL
            ORDER BY sc ASC
            """, mt, ic,
        )]

    item_descs: list = []
    uom_values: list = []
    total_descs = 0
    if mt and ic and sc:
        args: list = [mt, ic, sc]
        where = ["UPPER(item_type) = UPPER($1)", "UPPER(item_group) = UPPER($2)", "UPPER(sub_group) = UPPER($3)"]
        if term:
            args.append(f"%{term.lower()}%")
            where.append(f"LOWER(particulars) LIKE ${len(args)}")
        where_sql = " AND ".join(where)

        total_descs = await conn.fetchval(
            f"SELECT COUNT(DISTINCT UPPER(particulars)) FROM {_SKU} WHERE {where_sql}", *args,
        )
        rows = await conn.fetch(
            f"""
            SELECT desc_upper, uom FROM (
                SELECT DISTINCT ON (UPPER(particulars)) UPPER(particulars) AS desc_upper, uom
                FROM {_SKU}
                WHERE {where_sql} AND particulars IS NOT NULL
                ORDER BY UPPER(particulars) ASC
            ) sub
            ORDER BY sub.desc_upper ASC
            LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
            """, *args, limit, offset,
        )
        item_descs = [r["desc_upper"] for r in rows]
        uom_values = [_uom(r["uom"]) for r in rows]

    return {
        "selected": {"material_type": mt, "item_category": ic, "sub_category": sc},
        "options": {
            "material_types": material_types,
            "item_categories": item_categories,
            "sub_categories": sub_categories,
            "item_descriptions": item_descs,
            "uom_values": uom_values,
        },
        "meta": {
            "total_material_types": len(material_types),
            "total_item_descriptions": total_descs,
            "total_categories": len(item_categories),
            "total_sub_categories": len(sub_categories),
            "limit": limit, "offset": offset, "search": term,
        },
    }
