"""Where an article is in stock right now — for the store, before it issues.

The Production Indents screen asks for the articles on its page. Each comes back
with its Fresh Stock at every warehouse + floor where it is above zero, read
through Stock Take's own by-place query (latest_stock_service.fetch_stock_by_place),
so the figures are the Stock Take page's figures, cold stores included. Off grade
is left out: a request is not issued from it.

Unlike a request's available_qty — a snapshot of the requesting floor, frozen
when the floor raised it — this is live and spans every place.

Limited to the places the caller may read in Stock Take, through the same clamp
the Stock Take page and its download use (stock_take.router._clamp_to_read_scope).

Read-only.
"""
from __future__ import annotations

from typing import Any, Iterable

import asyncpg

from app.modules.floor_requisition import rules
from app.modules.stock_take import floors as _floors
from app.modules.stock_take.router import _clamp_to_read_scope
from app.modules.stock_take.services import latest_stock_service

#: More than a page of requests can name; a longer list is cut, not refused.
MAX_ARTICLES = 200


async def floor_availability(conn: asyncpg.Connection, user: Any, articles: Iterable[str]) -> dict[str, Any]:
    """{"articles": {ARTICLE KEY: [place, ...]}}, every asked-for key present.

    A place is {warehouse, warehouse_label, floor, kg, qty}, largest kg first;
    kg is total_weight and qty total_quantity, so a kg request and a pcs request
    can each read their own unit (rules.snapshot makes the same split). An
    article in stock nowhere the caller can see maps to [].
    """
    keys = sorted({k for k in (rules.article_key(a) for a in articles) if k})[:MAX_ARTICLES]
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in keys}
    if not keys:
        return {"articles": out}

    places = await latest_stock_service.fetch_places(conn)
    warehouse, _, place_scope = _clamp_to_read_scope(user, places, None, None)
    rows, _ = await latest_stock_service.fetch_stock_by_place(
        conn, warehouse=warehouse, place_scope=place_scope,
        item_keys=keys, stock_type=["Fresh Stock"])

    for r in rows:
        kg, qty = round(r["total_weight"], 3), round(r["total_quantity"], 3)
        if kg <= 0 and qty <= 0:
            continue
        out.setdefault(r["item_key"], []).append({
            "warehouse": r["warehouse"],
            "warehouse_label": _floors.warehouse_label(r["warehouse"]),
            "floor": r["floor"],
            "kg": kg,
            "qty": qty,
        })
    for found in out.values():
        found.sort(key=lambda p: (-p["kg"], -p["qty"]))
    return {"articles": out}
