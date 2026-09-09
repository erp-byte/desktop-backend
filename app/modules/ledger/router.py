"""GET /api/v1/ledger — read-only feed for the Inventory Ledger module.

The frontend derives every screen (stock summary tree, group drill, item hub,
ageing, FIFO) from one flat leaf feed, so this single endpoint drives the module.

Only the Inward column is sourced. The other six movement columns are zero, which
means the derived Closing is NOT a stock balance — the UI renders an "Inward only"
chip to prevent that being misread.

Read-only by design. No POST/PATCH/DELETE on this router.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Depends, Query, Request

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.ledger.services.leaves_service import (
    ENTITIES, SHIFTS, fetch_activity, fetch_leaves,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ledger", tags=["Ledger"])


def _day(value: Optional[str], field: str) -> Optional[date]:
    """Parse an inclusive YYYY-MM-DD bound, rejecting rather than ignoring.

    Dropping an unusable bound would silently widen the window to all time,
    which looks exactly like success on screen.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, detail={
            "error": "invalid_date", "message": f"{field} must be YYYY-MM-DD",
            "details": {field: value}}) from exc


@router.get("/leaves")
async def list_leaves(
    request: Request,
    entity: str = Query(
        "both",
        description="Entity scope: cfpl, cdpl, or both.",
    ),
    date_from: Optional[str] = Query(
        None, alias="from",
        description="Inclusive entry_date lower bound, YYYY-MM-DD. Both or neither.",
    ),
    date_to: Optional[str] = Query(
        None, alias="to",
        description="Inclusive entry_date upper bound, YYYY-MM-DD. Both or neither.",
    ),
    days: Optional[list[str]] = Query(
        None, alias="day",
        description="Exact entry_dates, repeated (?day=A&day=B). Alternative to from/to.",
    ),
    shift: str = Query(
        "all",
        description="all | am (keyed before 14:00 IST) | pm (14:00 IST or later).",
    ),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Flat inward leaf rows, one per SKU x godown x material type x entity.

    With `from`/`to` the leaf is the aggregate FOR THAT WINDOW rather than for
    all time. The window is applied before the GROUP BY — there is no date on a
    leaf, so a caller cannot apply it afterwards.
    """
    if entity not in (*ENTITIES, "both"):
        entity = "both"
    if shift not in SHIFTS:
        raise HTTPException(400, detail={
            "error": "invalid_shift",
            "message": f"shift must be one of {sorted(SHIFTS)}",
            "details": {"shift": shift}})
    lo, hi = _day(date_from, "from"), _day(date_to, "to")
    if (lo is None) != (hi is None):
        raise HTTPException(400, detail={
            "error": "invalid_range",
            "message": "from and to must be given together",
            "details": {"from": date_from, "to": date_to}})
    if lo is not None and lo > hi:
        raise HTTPException(400, detail={
            "error": "invalid_range", "message": "from is after to",
            "details": {"from": date_from, "to": date_to}})
    picked = sorted({_day(d, "day") for d in days}) if days else None
    if picked and lo is not None:
        raise HTTPException(400, detail={
            "error": "invalid_range",
            "message": "pass either from/to or day, not both",
            "details": {"from": date_from, "to": date_to, "day": days}})

    # Echoed back so the caller can render the scope it actually got rather
    # than the scope it believes it asked for.
    applied = {"entity": entity, "from": lo.isoformat() if lo else None,
               "to": hi.isoformat() if hi else None,
               "days": [d.isoformat() for d in picked] if picked else None,
               "shift": shift}
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            data = await fetch_leaves(conn, entity=entity, date_from=lo,
                                      date_to=hi, days=picked, shift=shift)
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as exc:
            # These legacy schemas are inferred from query code, not verified, so
            # both a missing table and a missing column are expected environment
            # states rather than bugs. fetch_leaves already degrades per-entity;
            # this is the backstop for anything it does not cover. The frontend
            # renders an empty state, so a 500 would just be noise.
            log.warning(
                "ledger: /leaves returning empty for entity=%s — legacy inward "
                "schema is absent (%s: %s)", entity, type(exc).__name__, exc,
            )
            return {"data": [], "applied": applied}
    return {"data": data, "applied": applied}


@router.get("/activity")
async def list_activity(
    request: Request,
    entity: str = Query("both", description="Entity scope: cfpl, cdpl, or both."),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Which entry_dates have inward documents, and how each splits by shift.

    Drives the day pills: a day with no row here gets no dot and is not
    selectable, so the operator is never sent to an empty window.
    """
    if entity not in (*ENTITIES, "both"):
        entity = "both"
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            return await fetch_activity(conn, entity=entity)
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as exc:
            # Same posture as /leaves: an absent legacy schema is an environment
            # state, not a bug, and an empty pill strip renders correctly.
            log.warning("ledger: /activity returning empty for entity=%s (%s: %s)",
                        entity, type(exc).__name__, exc)
            return {"days": [], "min_date": None, "max_date": None,
                    "shift_cutoff_hour": 14}
