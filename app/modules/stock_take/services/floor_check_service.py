"""A floor's daily "checked - no movement" report and the inventory manager's
decision on it (migration 142, stocktake_floor_checks).

Why it exists. Management wants a stock take on every floor every day. A floor
where stock moved has postings that day and so a fresh "last entry" date. A floor
where nothing moved has nothing to post, and its date goes stale on every report.
An adjustment of 0 cannot be posted (the ledger needs units > 0 and kg > 0) and
would claim stock moved, so the floor reports "checked, nothing moved" instead,
once for the day, and an inventory manager approves or rejects it.

The rules, all here so the screen and the WhatsApp tap apply the same ones:
  * a check is for TODAY (IST) only;
  * a floor that already has entries today cannot report "no movement";
  * one check per floor per day: once it is pending or approved, reporting again
    is refused; after a rejection the floor may report again, which puts the same
    row back to pending;
  * only a pending check can be decided, and nobody decides their own check.
Who may report (the posting scope) and who may decide (the approve permission and
the warehouse) is checked by the callers, which hold the user.

Nothing here reads or changes a stock figure.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from .. import floors as _floors
from .business_day import ENTRIES_TABLE, ENTRY_DAY

IST = timezone(timedelta(hours=5, minutes=30))
TABLE = "stocktake_floor_checks"

#: The states a floor can be in for a day, as the screen and the report show them.
STATE_ENTRIES = "entries"        # stock was posted there that day
STATE_APPROVED = "approved"      # no movement, approved by an inventory manager
STATE_PENDING = "pending"        # no movement reported, waiting for approval
STATE_REJECTED = "rejected"      # no movement reported, rejected
STATE_NONE = "none"              # nothing posted and nothing reported


class FloorCheckError(Exception):
    """A rule refused the request. `status` is the HTTP status to answer with."""

    def __init__(self, code: str, message: str, status: int = 409,
                 details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details or {}


NOT_INSTALLED = FloorCheckError(
    "not_installed",
    "Daily floor checks are not set up on this server yet (database migration 142). Tell IT.",
    status=503)


def ist_today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(IST).date()


def _missing_table(exc: BaseException) -> bool:
    return isinstance(exc, asyncpg.exceptions.UndefinedTableError)


# The same rows the daily report reads: submitted counts and adjustments, not
# drafts, and not the rows the 18 Nov 2026 A185 floor merge wrote as Admin.
_ENTRY_FILTER = """
       (status IS NULL OR status <> 'draft')
   AND COALESCE(entry_id, '') NOT LIKE 'MRG%'
"""

_ENTRIES_BY_FLOOR_SQL = f"""
    SELECT REPLACE(UPPER(BTRIM(warehouse)), '-', '')            AS k_wh,
           COALESCE(UPPER(BTRIM(floor_name)), '')               AS k_fl,
           COUNT(*) FILTER (WHERE {ENTRY_DAY} = $2)             AS n_day,
           MAX(created_at)                                      AS last_at,
           (ARRAY_AGG(entered_by ORDER BY created_at DESC, id DESC))[1] AS last_by
      FROM {ENTRIES_TABLE}
     WHERE REPLACE(UPPER(BTRIM(warehouse)), '-', '') = ANY($1::text[])
       AND {ENTRY_DAY} <= $2
       AND {_ENTRY_FILTER}
     GROUP BY 1, 2
"""

_ENTRIES_ON_FLOOR_SQL = f"""
    SELECT COUNT(*) FILTER (WHERE {ENTRY_DAY} = $3)             AS n_day,
           MAX(created_at)                                      AS last_at,
           (ARRAY_AGG(entered_by ORDER BY created_at DESC, id DESC))[1] AS last_by
      FROM {ENTRIES_TABLE}
     WHERE REPLACE(UPPER(BTRIM(warehouse)), '-', '') = $1
       AND COALESCE(UPPER(BTRIM(floor_name)), '') = $2
       AND {ENTRY_DAY} <= $3
       AND {_ENTRY_FILTER}
"""

_COLUMNS = """check_id, check_day, warehouse, floor_name, status, note, checked_by,
              checked_by_user_id, checked_at, submissions, decided_by, decided_by_user_id,
              decided_at, decided_via, reject_reason"""


def check_dict(row: Any) -> dict[str, Any]:
    """A check as the API returns it (timestamps as ISO text)."""
    d = dict(row)
    for k in ("checked_at", "decided_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    if d.get("check_day") is not None:
        d["check_day"] = d["check_day"].isoformat()
    d["floor_label"] = _floors.floor_display(d.get("warehouse"), d.get("floor_name"))
    return d


def floor_state(entries_on_day: int, check: Optional[dict[str, Any]]) -> str:
    """What the day looks like for one floor. Entries win: a floor that posted
    stock that day has been stock-taken, whatever was reported."""
    if entries_on_day:
        return STATE_ENTRIES
    if check:
        return {"approved": STATE_APPROVED, "pending": STATE_PENDING,
                "rejected": STATE_REJECTED}.get(check.get("status"), STATE_NONE)
    return STATE_NONE


# ── reads ────────────────────────────────────────────────────────────────────

async def fetch_checks(conn, day: date, warehouses: list[str]) -> Optional[list[dict[str, Any]]]:
    """The checks for that day at these warehouses; None when the table is missing."""
    try:
        rows = await conn.fetch(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE check_day = $1 AND warehouse = ANY($2::text[])",
            day, warehouses)
    except asyncpg.PostgresError as exc:
        if _missing_table(exc):
            return None
        raise
    return [check_dict(r) for r in rows]


async def floor_status(conn, *, day: date, floors_by_warehouse: dict[str, list[str]]) -> dict[str, Any]:
    """Every floor named in `floors_by_warehouse`, for that day: its last entry up
    to that day, how many entries that day, the check reported that day and the
    state those add up to. Floors keep the order they are given in."""
    whs = [_floors.normalise_warehouse(w) for w in floors_by_warehouse]
    entries = {(r["k_wh"], r["k_fl"]): r
               for r in await conn.fetch(_ENTRIES_BY_FLOOR_SQL, whs, day)} if whs else {}
    checks = await fetch_checks(conn, day, whs) if whs else []
    by_floor = {(c["warehouse"], (c["floor_name"] or "").strip().upper()): c for c in (checks or [])}

    out: list[dict[str, Any]] = []
    for wh, floors in floors_by_warehouse.items():
        code = _floors.normalise_warehouse(wh)
        for floor in floors:
            key = (code, floor.strip().upper())
            e = entries.get(key)
            check = by_floor.get(key)
            n_day = int(e["n_day"]) if e else 0
            last_at = e["last_at"] if e else None
            state = floor_state(n_day, check)
            # The newest thing that shows the floor was looked at: its last entry,
            # or the day's check once it is approved.
            activity = last_at
            if state == STATE_APPROVED and check and check.get("decided_at"):
                decided = datetime.fromisoformat(check["decided_at"])
                if activity is None or decided > activity:
                    activity = decided
            out.append({
                "warehouse": code,
                "warehouse_label": _floors.warehouse_label(code),
                "floor": floor,
                "floor_label": _floors.floor_display(code, floor),
                "last_entry_at": last_at.isoformat() if last_at else None,
                "last_entry_by": (e["last_by"] if e else None),
                "entries_on_day": n_day,
                "check": check,
                "state": state,
                "last_activity_at": activity.isoformat() if activity else None,
            })
    return {"day": day.isoformat(), "today": ist_today().isoformat(),
            "checks_installed": checks is not None, "floors": out}


async def get_check(conn, check_id: int, *, for_update: bool = False) -> Optional[dict[str, Any]]:
    try:
        row = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE check_id = $1" + (" FOR UPDATE" if for_update else ""),
            int(check_id))
    except asyncpg.PostgresError as exc:
        if _missing_table(exc):
            raise NOT_INSTALLED from exc
        raise
    return check_dict(row) if row else None


async def last_entry(conn, warehouse: str, floor: str, day: date) -> dict[str, Any]:
    """{n_day, last_at, last_by} for one floor up to that day."""
    r = await conn.fetchrow(_ENTRIES_ON_FLOOR_SQL, _floors.normalise_warehouse(warehouse),
                            (floor or "").strip().upper(), day)
    return {"n_day": int(r["n_day"] or 0) if r else 0,
            "last_at": r["last_at"] if r else None,
            "last_by": r["last_by"] if r else None}


# ── report ───────────────────────────────────────────────────────────────────

_SUBMIT_SQL = f"""
    INSERT INTO {TABLE} (check_day, warehouse, floor_name, note, checked_by, checked_by_user_id)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (check_day, warehouse, (UPPER(BTRIM(floor_name)))) DO UPDATE
       SET status = 'pending', note = EXCLUDED.note,
           checked_by = EXCLUDED.checked_by, checked_by_user_id = EXCLUDED.checked_by_user_id,
           checked_at = now(), submissions = {TABLE}.submissions + 1,
           decided_by = NULL, decided_by_user_id = NULL, decided_at = NULL,
           decided_via = NULL, reject_reason = NULL, updated_at = now()
     WHERE {TABLE}.status = 'rejected'
    RETURNING {_COLUMNS}
"""


async def submit_check(conn, *, warehouse: str, floor: str, note: Optional[str], actor: str,
                       user_id: Optional[int], day: Optional[date] = None) -> dict[str, Any]:
    """Report "checked, no movement" for this floor today. `warehouse` and `floor`
    must already be resolved against the caller's posting scope.

    Run it inside a transaction: the entries count and the write must see the
    same moment."""
    day = day or ist_today()
    code = _floors.normalise_warehouse(warehouse)
    seen = await last_entry(conn, code, floor, day)
    if seen["n_day"]:
        raise FloorCheckError(
            "floor_has_entries",
            f"{seen['n_day']} entr{'y has' if seen['n_day'] == 1 else 'ies have'} been posted on this "
            f"floor today, so it has been stock-taken already. No check is needed.",
            details={"entries_today": seen["n_day"]})
    note = (note or "").strip() or None
    try:
        row = await conn.fetchrow(_SUBMIT_SQL, day, code, floor, note, actor, user_id)
    except asyncpg.PostgresError as exc:
        if _missing_table(exc):
            raise NOT_INSTALLED from exc
        raise
    if row is None:
        existing = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE check_day = $1 AND warehouse = $2"
            f" AND UPPER(BTRIM(floor_name)) = $3", day, code, floor.strip().upper())
        ex = check_dict(existing) if existing else {}
        what = ("is waiting for approval" if ex.get("status") == "pending"
                else "has been approved" if ex.get("status") == "approved" else "exists")
        raise FloorCheckError(
            "already_reported",
            f"Today's check for this floor {what} (reported by {ex.get('checked_by') or 'someone'}).",
            details={"check": ex})
    return check_dict(row)


# ── decide ───────────────────────────────────────────────────────────────────

_DECIDE_SQL = f"""
    UPDATE {TABLE}
       SET status = $2, decided_by = $3, decided_by_user_id = $4, decided_at = now(),
           decided_via = $5, reject_reason = $6, updated_at = now()
     WHERE check_id = $1 AND status = 'pending'
    RETURNING {_COLUMNS}
"""


def _when(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).astimezone(IST).strftime("%d %b, %H:%M")
    except ValueError:
        return iso


async def decide(conn, *, check_id: int, approve: bool, reason: Optional[str], actor: str,
                 user_id: Optional[int], via: str = "erp") -> dict[str, Any]:
    """Approve or reject a pending check. Run it inside a transaction (the row is
    locked while it is read and decided). The caller has already checked the
    approve permission and the warehouse."""
    check = await get_check(conn, check_id, for_update=True)
    if check is None:
        raise FloorCheckError("not_found", f"There is no floor check #{check_id}.", status=404)
    if check["status"] != "pending":
        raise FloorCheckError(
            "already_decided",
            f"This check was already {check['status']} by {check.get('decided_by') or 'someone'} "
            f"on {_when(check.get('decided_at'))}.",
            details={"check": check})
    if user_id is not None and check.get("checked_by_user_id") == user_id:
        raise FloorCheckError("own_check", "You reported this check yourself, so someone else must decide it.",
                              status=403)
    reason = (reason or "").strip() or None
    row = await conn.fetchrow(_DECIDE_SQL, int(check_id), "approved" if approve else "rejected",
                              actor, user_id, via, None if approve else reason)
    if row is None:      # decided between the read and the write; the lock makes this unlikely
        raise FloorCheckError("already_decided", "This check was decided by someone else just now.")
    return check_dict(row)
