"""Type-ahead lookups backing the BOM form: GET /api/v1/bom/lookups/*.

Read-only. Deliberately NOT a re-use of the SO module's /sku-lookup: that
endpoint returns every matching all_sku row with SELECT * and no LIMIT (it
feeds a four-level cascade of dropdowns), hides sfg unless asked for, and
carries no item_type per name. The BOM line picker needs the opposite — a
SHORT, ranked list WITH the type, so that picking an article can set the
line's rm / pm / sfg in the same click.

FG articles are included on purpose. Hamper BOMs list finished goods as rm
components ('PM24-Hamper-3 Jar Jute Bag' carries 'Carnival Roasted & Salted
Pistachio 200g'), so an rm-only search would hide exactly what a hamper
author is looking for. The picker shows the type beside each name instead.

Processes come from bom_process_route itself — there is no process master
table. Only ~15 distinct names exist in production, dominated by 'Sorting +
Packing', 'Packing' and the 'Create WIP: …' family, so the list is ranked by
how many BOMs use each name and the form still accepts a brand-new name.
"""
from __future__ import annotations

from app.modules.bom.services.bom_aggregate_service import _serialize_row

# Same fold as /sku-lookup ("toor  dal" -> "%toor%dal%") so the two pickers
# agree on what matches, plus LIKE-metacharacter escaping, which /sku-lookup
# skips: a '_' in a query like 'PM24_Hamper' would otherwise match any single
# character. Postgres' default LIKE escape is backslash.
def _token_pattern(q: str) -> str:
    tokens = q.strip().split()
    return "%" + "%".join(_escape_like(t.lower()) for t in tokens) + "%"


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Prefix matches first ($2 = 'q%'), then shorter names (a closer match for a
# short query), then alphabetical. {type_clause} is either empty or a fixed
# `AND lower(item_type) = $3`; the user's text only ever travels as a bind
# parameter.
_MATERIAL_SQL = """
SELECT sku_id, particulars, item_type, item_group, sub_group
FROM all_sku
WHERE particulars IS NOT NULL
  AND lower(regexp_replace(particulars, '\\s+', ' ', 'g')) LIKE $1
  {type_clause}
ORDER BY (lower(particulars) LIKE $2) DESC, length(particulars) ASC, particulars ASC
LIMIT ${limit_idx}
"""

_PROCESS_SQL = """
SELECT process_name, MIN(stage) AS stage, COUNT(DISTINCT bom_id) AS boms
FROM bom_process_route
WHERE process_name IS NOT NULL AND process_name <> ''
  {q_clause}
GROUP BY process_name
ORDER BY boms DESC, process_name ASC
LIMIT ${limit_idx}
"""


async def search_materials(conn, q: str, *, item_type: str | None = None,
                           limit: int = 20) -> list[dict]:
    """Ranked all_sku rows whose name contains every token of `q`, in order.

    A blank query returns nothing rather than the first N rows of the master:
    the picker is a type-ahead, and 4,000 rows of "everything" is not a
    suggestion.
    """
    if not q or not q.strip():
        return []
    pattern = _token_pattern(q)
    prefix = _escape_like(" ".join(q.strip().split()).lower()) + "%"
    params: list = [pattern, prefix]
    type_clause = ""
    if item_type:
        params.append(item_type.strip().lower())
        type_clause = f"AND lower(item_type) = ${len(params)}"
    params.append(limit)
    rows = await conn.fetch(
        _MATERIAL_SQL.format(type_clause=type_clause, limit_idx=len(params)),
        *params,
    )
    return [_serialize_row(r) for r in rows]


async def search_processes(conn, q: str = "", *, limit: int = 30) -> list[dict]:
    """Distinct process names from bom_process_route, most-used first.

    Blank `q` lists them all (there are only a handful) so the picker can open
    on focus; otherwise a case-insensitive substring match.
    """
    params: list = []
    q_clause = ""
    if q and q.strip():
        params.append(_token_pattern(q))
        q_clause = f"AND lower(process_name) LIKE ${len(params)}"
    params.append(limit)
    rows = await conn.fetch(
        _PROCESS_SQL.format(q_clause=q_clause, limit_idx=len(params)),
        *params,
    )
    return [_serialize_row(r) for r in rows]
