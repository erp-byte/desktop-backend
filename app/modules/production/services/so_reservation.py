"""What a plan line holds back on its sales order rows.

so_fulfillment_v2.planned_qty_kg / _units is the qty of each SO row that live
(non-cancelled) plans have taken; SO Creation shows original - dispatched -
planned as still pending. Nothing records it per line: a line adds its qty when
it is created and gives it back when it is cancelled. So every change to a
line's qty has to move the reservation by the same amount, or SO Creation shows
the wrong pending qty (audit 2026-10-23: line edits and amendments didn't).

A line tied to one SO row (nearly all of them) holds its whole qty there. A line
tied to several, made by merging same-article lines, holds on each row what that
row has reserved less the qty of the other live single-row lines on it. On RDS
(2026-10-24) that adds up exactly to the line's qty for all 37 such lines, and no
row is shared by two of them. Giving back used to take the line's WHOLE qty off
EVERY row it named.
"""
from __future__ import annotations

from asyncpg.exceptions import CheckViolationError

_STEP = 0.0005  # below one NUMERIC(12,3) step


class OverAllocation(ValueError):
    """The SO rows haven't that much qty left to plan."""


def split_release(amount, held: dict) -> dict:
    """Take `amount` off the rows in proportion to what each holds, never more
    than a row holds (so never more than they hold together). The last row takes
    the rounding."""
    amount = max(0.0, float(amount or 0))
    rows = [(f, max(0.0, float(h or 0))) for f, h in held.items()]
    rows = [(f, h) for f, h in rows if h >= _STEP]
    total = sum(h for _, h in rows)
    if amount < _STEP or total < _STEP:
        return {}
    left = round(min(amount, total), 3)
    out = {}
    for i, (f, h) in enumerate(rows):
        part = left if i == len(rows) - 1 else round(min(amount, total) * h / total, 3)
        part = round(min(part, h, left), 3)
        if part >= _STEP:
            out[f] = part
            left = round(left - part, 3)
    return out


def split_reserve(amount, room: list) -> dict | None:
    """Fill each row's pending qty in turn ([(row, pending)]; pending None = no
    limit, as for units on a row with no ordered units). None when the rows
    together haven't `amount` left."""
    left = round(max(0.0, float(amount or 0)), 3)
    out = {}
    for f, r in room:
        if left < _STEP:
            break
        part = left if r is None else min(left, max(0.0, round(float(r), 3)))
        if part >= _STEP:
            out[f] = part
            left = round(left - part, 3)
    return out if left < _STEP else None


# Each named row with what it has reserved and has left, and the qty the OTHER
# live single-row lines hold on it ($2 = the line being moved; NULL on create).
_ROWS_SQL = """
SELECT sf.so_fulfillment_id AS fid,
       sf.planned_qty_kg, sf.planned_qty_units,
       sf.pending_qty_kg, sf.pending_qty_units,
       COALESCE((SELECT SUM(l.planned_qty_kg)
                   FROM production_plan_line_v2 l
                   JOIN production_plan_v2 p ON p.plan_id = l.plan_id
                  WHERE p.status <> 'cancelled'
                    AND l.plan_line_id IS DISTINCT FROM $2::bigint
                    AND cardinality(l.linked_so_fulfillment_ids) = 1
                    AND l.linked_so_fulfillment_ids[1] = sf.so_fulfillment_id), 0) AS others_kg,
       COALESCE((SELECT SUM(l.planned_qty_units)
                   FROM production_plan_line_v2 l
                   JOIN production_plan_v2 p ON p.plan_id = l.plan_id
                  WHERE p.status <> 'cancelled'
                    AND l.plan_line_id IS DISTINCT FROM $2::bigint
                    AND cardinality(l.linked_so_fulfillment_ids) = 1
                    AND l.linked_so_fulfillment_ids[1] = sf.so_fulfillment_id), 0) AS others_units
  FROM so_fulfillment_v2 sf
 WHERE sf.so_fulfillment_id = ANY($1::bigint[])
   FOR UPDATE OF sf
"""


def _ids(fids) -> list[int]:
    return list(dict.fromkeys(int(f) for f in (fids or []) if f is not None))


async def _rows(conn, fids: list[int], plan_line_id) -> list:
    by_id = {r["fid"]: r for r in await conn.fetch(_ROWS_SQL, fids, plan_line_id)}
    return [by_id[f] for f in fids if f in by_id]


def _pending(v):
    return None if v is None else float(v)


async def reserve(conn, fids, *, kg, units) -> dict:
    """Take kg / units of the SO rows for a line (a new line, or more qty).
    Returns {row: (kg, units)}; raises OverAllocation when the SO hasn't it."""
    fids = _ids(fids)
    kg, units = max(0.0, float(kg or 0)), max(0.0, float(units or 0))
    if not fids or (kg < _STEP and units < _STEP):
        return {}
    if len(fids) == 1:
        parts = {fids[0]: (kg, units)}
    else:
        rows = await _rows(conn, fids, None)
        by_kg = split_reserve(kg, [(r["fid"], _pending(r["pending_qty_kg"])) for r in rows])
        by_units = split_reserve(units, [(r["fid"], _pending(r["pending_qty_units"])) for r in rows])
        if by_kg is None or by_units is None:
            raise OverAllocation(
                f"Would over-allocate fulfillment {fids}: requested {kg} kg / {units} pcs "
                "but pending isn't enough.")
        parts = {f: (by_kg.get(f, 0.0), by_units.get(f, 0.0))
                 for f in fids if f in by_kg or f in by_units}
    for f, (k, u) in parts.items():
        try:
            await conn.execute(
                """
                UPDATE so_fulfillment_v2
                   SET planned_qty_kg    = planned_qty_kg    + $1,
                       planned_qty_units = planned_qty_units + $2
                 WHERE so_fulfillment_id = $3
                """,
                k, u, f,
            )
        except CheckViolationError as exc:
            # chk_pending_*_nonneg: the SO row hasn't that much left to plan.
            raise OverAllocation(
                f"Would over-allocate fulfillment {fids}: requested {kg} kg / {units} pcs "
                f"but pending isn't enough. ({getattr(exc, 'constraint_name', None)})"
            ) from exc
    return parts


async def release(conn, plan_line_id, fids, *, kg, units) -> dict:
    """Give back kg / units a line holds on its SO rows (a cancel, or less qty).
    Returns {row: (kg, units)} — what each row is asked to give back."""
    fids = _ids(fids)
    kg, units = max(0.0, float(kg or 0)), max(0.0, float(units or 0))
    if not fids or (kg < _STEP and units < _STEP):
        return {}
    if len(fids) == 1:
        parts = {fids[0]: (kg, units)}
    else:
        rows = await _rows(conn, fids, plan_line_id)
        by_kg = split_release(kg, {r["fid"]: float(r["planned_qty_kg"] or 0) - float(r["others_kg"] or 0)
                                   for r in rows})
        by_units = split_release(units, {r["fid"]: float(r["planned_qty_units"] or 0)
                                         - float(r["others_units"] or 0) for r in rows})
        parts = {f: (by_kg.get(f, 0.0), by_units.get(f, 0.0))
                 for f in fids if f in by_kg or f in by_units}
    for f, (k, u) in parts.items():
        # GREATEST(0, ...): a row some earlier bug left holding less never goes negative.
        await conn.execute(
            """
            UPDATE so_fulfillment_v2
               SET planned_qty_kg    = GREATEST(0, planned_qty_kg    - $1),
                   planned_qty_units = GREATEST(0, planned_qty_units - $2)
             WHERE so_fulfillment_id = $3
            """,
            k, u, f,
        )
    return parts


async def move(conn, plan_line_id, fids, *, delta_kg, delta_units) -> dict:
    """Move what a line holds by its qty change (signed; kg and units apart)."""
    dk, du = float(delta_kg or 0), float(delta_units or 0)
    taken = await reserve(conn, fids, kg=max(dk, 0.0), units=max(du, 0.0))
    given = await release(conn, plan_line_id, fids, kg=max(-dk, 0.0), units=max(-du, 0.0))
    return {"reserved": taken, "released": given}


def total(parts: dict) -> tuple[float, float]:
    """(kg, units) summed over a reserve / release result."""
    return (round(sum(k for k, _ in parts.values()), 3),
            round(sum(u for _, u in parts.values()), 3))
