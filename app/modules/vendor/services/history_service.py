"""State-management service for the vendor module.

Every mutating call routes through `record_history()` so a complete
prev-state / new-state / diff snapshot lands in the matching `*_history`
table inside the same DB transaction as the data change. Tables are
append-only (DB trigger enforces it — see migration 030).

Public surface
--------------
`record_history(conn, kind, ...)`
    Used by `vendor_service.update_vendor()` and friends. Must be called
    inside an existing `async with conn.transaction()` so the snapshot
    rolls back together with a failed UPDATE.

`list_history(pool, kind, parent_id, ...)`
`get_history_entry(pool, kind, history_id)`
    Read paths for the History tab on the UI.

`build_revert_patch(history_entry)`
    Derives a PATCH payload that, when applied via the parent's
    `update_*` function, restores the row to its `previous_state`. The
    revert is a *new* history row tagged `operation='revert'` — never an
    in-place rewrite of the old one.

The "kind" string switches the table + parent-column. We use a small
HISTORY_TABLES registry rather than 4 near-duplicate functions so a
schema tweak only needs to land once.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


# ── history table registry ───────────────────────────────────────────────


HISTORY_TABLES: dict[str, dict[str, Any]] = {
    "vendor": {
        "table":   "vendor_master_history",
        "parent_col": "vendor_id",
        "extra_cols": (),
        "ops": {"create", "update", "approve", "delete", "restore", "revert"},
    },
    "banking": {
        "table":   "vendor_banking_history",
        "parent_col": "bank_id",
        "extra_cols": ("vendor_id",),
        "ops": {"create", "update", "delete", "set_primary"},
    },
    "document": {
        "table":   "vendor_document_history",
        "parent_col": "doc_id",
        "extra_cols": ("vendor_id",),
        "ops": {"create", "update", "delete", "append_file"},
    },
    "contract": {
        "table":   "vendor_contract_history",
        "parent_col": "contract_id",
        "extra_cols": ("vendor_id",),
        "ops": {"create", "update", "delete", "append_file"},
    },
}


# ── ID minter ────────────────────────────────────────────────────────────


# Epoch for the time-based PK — anchor at 2025-01-01 UTC so we get
# ~14-digit values for the next few decades. Same pattern as the v2 PK
# helpers in the production module.
_HISTORY_EPOCH = 1735689600  # 2025-01-01 00:00:00 UTC


def new_history_id() -> int:
    """Monotonic-ish BIGINT id: (seconds since epoch * 1000) + 3-digit nonce.

    Collisions are vanishingly unlikely (would require two inserts in the
    same second with the same random nibble). The caller can retry on the
    rare UniqueViolation, but in practice it never fires.
    """
    seconds = int(time.time()) - _HISTORY_EPOCH
    return seconds * 1000 + secrets.randbelow(1000)


# ── JSON normalisation ───────────────────────────────────────────────────


def _jsonable(value: Any) -> Any:
    """Convert asyncpg-returned scalars into JSON-serialisable form.

    asyncpg gives us `datetime` / `date` / `Decimal` straight from the
    driver. JSONB encoding via `json.dumps` chokes on those, so we widen
    them to strings here. Bools / ints / floats / strings / None pass
    through; nested dicts / lists are recursed.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        # Stay numeric in JSON so diffs compare cleanly; lose precision past
        # double, which is fine for monetary fields kept as NUMERIC(15,3).
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    # Unknown — fall back to string. Better than 500ing the audit trail.
    return str(value)


def row_to_state(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """asyncpg row → JSONB-ready dict. None passthrough for create paths."""
    if row is None:
        return None
    return {k: _jsonable(v) for k, v in row.items()}


# ── diff ─────────────────────────────────────────────────────────────────


# Columns that change on every mutation but never warrant a timeline entry.
# Diffing them would drown the UI in noise like `updated_at: {from: ..., to: ...}`.
_DIFF_IGNORE: frozenset[str] = frozenset({
    "updated_at", "updated_by",
    "created_at", "created_by",
    "uploaded_at", "uploaded_by",
})


def compute_diff(
    previous_state: dict[str, Any] | None,
    new_state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return `{field: {from: x, to: y}}` for fields whose value changed.

    On `create` (previous_state=None) we return an empty dict — the create
    row already carries the full new_state, no diff needed.
    """
    if previous_state is None:
        return {}
    diff: dict[str, dict[str, Any]] = {}
    keys = set(previous_state.keys()) | set(new_state.keys())
    for k in keys:
        if k in _DIFF_IGNORE:
            continue
        prev = previous_state.get(k)
        curr = new_state.get(k)
        if prev != curr:
            diff[k] = {"from": prev, "to": curr}
    return diff


# ── record ───────────────────────────────────────────────────────────────


async def record_history(
    conn: asyncpg.Connection,
    kind: str,
    *,
    operation: str,
    parent_id: str,
    new_state: dict[str, Any],
    previous_state: dict[str, Any] | None = None,
    actor_user_id: str | None = None,
    source: str = "manual",
    reason: str | None = None,
    vendor_id: str | None = None,
) -> int:
    """Insert one row into the appropriate history table.

    MUST be called inside an existing transaction on `conn` so the
    snapshot rolls back together with a failed data write.

    Returns the new `history_id`.
    """
    cfg = HISTORY_TABLES.get(kind)
    if cfg is None:
        raise ValueError(f"unknown history kind: {kind!r}")
    if operation not in cfg["ops"]:
        raise ValueError(f"invalid operation {operation!r} for kind {kind!r}")

    history_id = new_history_id()
    diff = compute_diff(previous_state, new_state)

    prev_json = json.dumps(_jsonable(previous_state)) if previous_state is not None else None
    new_json  = json.dumps(_jsonable(new_state))
    diff_json = json.dumps(diff)

    # Build INSERT with the right extra parent column (banking / doc /
    # contract carry an indexed `vendor_id` alongside their own PK).
    base_cols = [
        cfg["parent_col"], "history_id", "operation", "changed_by",
        "previous_state", "new_state", "diff", "source", "reason",
    ]
    base_vals: list[Any] = [
        parent_id, history_id, operation, actor_user_id,
        prev_json, new_json, diff_json, source, reason,
    ]
    if "vendor_id" in cfg["extra_cols"]:
        base_cols.insert(1, "vendor_id")
        base_vals.insert(1, vendor_id)

    placeholders = ", ".join(f"${i + 1}" for i in range(len(base_cols)))
    sql = (
        f"INSERT INTO {cfg['table']} ({', '.join(base_cols)}) "
        f"VALUES ({placeholders})"
    )
    try:
        await conn.execute(sql, *base_vals)
    except asyncpg.UniqueViolationError:
        # Collision on history_id — re-mint and retry once.
        history_id = new_history_id()
        base_vals[base_cols.index("history_id")] = history_id
        await conn.execute(sql, *base_vals)
    return history_id


# ── read ─────────────────────────────────────────────────────────────────


_HISTORY_LIST_COLS = """
    history_id, operation, changed_by, changed_at,
    previous_state, new_state, diff, source, reason
"""


async def list_history(
    pool: asyncpg.Pool,
    kind: str,
    *,
    vendor_id: str | None = None,
    parent_id: str | None = None,
    operation: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """Paginated timeline read.

    Filter knobs: `vendor_id` (always), `parent_id` (banking/doc/contract
    sub-resource view), `operation`, date range.
    """
    cfg = HISTORY_TABLES.get(kind)
    if cfg is None:
        raise ValueError(f"unknown history kind: {kind!r}")
    page = max(1, page)
    page_size = max(1, min(500, page_size))

    where: list[str] = []
    args: list[Any] = []
    parent_col = cfg["parent_col"]

    if parent_id is not None:
        args.append(parent_id)
        where.append(f"{parent_col} = ${len(args)}")
    elif vendor_id is not None and kind == "vendor":
        # Master timeline is keyed by vendor_id directly.
        args.append(vendor_id)
        where.append(f"vendor_id = ${len(args)}")
    elif vendor_id is not None:
        # Child timelines: scope to this vendor across all child ids.
        args.append(vendor_id)
        where.append(f"vendor_id = ${len(args)}")

    if operation:
        args.append(operation)
        where.append(f"operation = ${len(args)}")
    if from_date:
        args.append(from_date)
        where.append(f"changed_at >= ${len(args)}")
    if to_date:
        args.append(to_date)
        where.append(f"changed_at <= ${len(args)}")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    count_sql = f"SELECT count(*) FROM {cfg['table']}{where_sql}"
    list_sql = (
        f"SELECT {_HISTORY_LIST_COLS} FROM {cfg['table']}{where_sql} "
        f"ORDER BY changed_at DESC, history_id DESC "
        f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}"
    )

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_sql, *args)
        rows = await conn.fetch(list_sql, *args, page_size, (page - 1) * page_size)

    return [_row_with_json(r) for r in rows], int(total or 0)


def _row_with_json(row: asyncpg.Record) -> dict[str, Any]:
    """asyncpg returns JSONB as raw strings unless a codec is registered.
    Decode here so the response shape matches the JS client's expectations.
    """
    d = dict(row)
    for k in ("previous_state", "new_state", "diff"):
        v = d.get(k)
        if isinstance(v, (str, bytes)):
            try:
                d[k] = json.loads(v)
            except (TypeError, ValueError):
                pass
    return d


async def get_history_entry(
    pool: asyncpg.Pool,
    kind: str,
    history_id: int,
    *,
    vendor_id: str | None = None,
) -> dict[str, Any] | None:
    cfg = HISTORY_TABLES.get(kind)
    if cfg is None:
        raise ValueError(f"unknown history kind: {kind!r}")
    sql = (
        f"SELECT {_HISTORY_LIST_COLS}, {cfg['parent_col']}"
        f"{', vendor_id' if 'vendor_id' in cfg['extra_cols'] else ''}"
        f" FROM {cfg['table']} WHERE history_id = $1"
    )
    args: list[Any] = [history_id]
    if vendor_id is not None and kind == "vendor":
        sql += " AND vendor_id = $2"
        args.append(vendor_id)
    elif vendor_id is not None:
        sql += " AND vendor_id = $2"
        args.append(vendor_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    return _row_with_json(row) if row else None


# ── revert ───────────────────────────────────────────────────────────────


# Columns that revert should NEVER overwrite: server-managed identity /
# audit columns. Trying to "revert" these would either break referential
# integrity or be a meaningless no-op.
_NEVER_REVERT: frozenset[str] = frozenset({
    "vendor_id", "bank_id", "doc_id", "contract_id",
    "created_at", "created_by",
    "updated_at", "updated_by",
    "uploaded_at", "uploaded_by",
    "approved_at", "approved_by",
    "supplier_code",   # uniqueness-bearing; reverting silently could clash
    "is_deleted",      # soft-delete flag is driven by its own endpoint
})


def build_revert_patch(history_entry: dict[str, Any]) -> dict[str, Any]:
    """Pull just the *changed* fields out of `previous_state` so the revert
    PATCH is minimal — same shape the update_* services expect.

    For a `create` entry there's no previous_state, so we raise — the UI
    blocks the Revert button in that case.
    """
    prev = history_entry.get("previous_state")
    if not prev:
        raise ValueError("cannot revert a 'create' entry — there is no prior state")
    diff = history_entry.get("diff") or {}
    patch: dict[str, Any] = {}
    for field in diff.keys():
        if field in _NEVER_REVERT:
            continue
        if field in prev:
            patch[field] = prev[field]
    return patch
