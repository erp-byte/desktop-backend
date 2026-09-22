"""Floor requisitions — raise, list, issue, receive, cancel.

One row of floor_requisition (app/db/111_floor_requisition.sql) per request for
one article. Raised by the floor from the job card tab, issued by store, received
by the floor; cancelled while still raised. TRACKING ONLY: nothing here touches
new_stock_entries or stocktake_transactions.

The browser sends only the job card, the article, the quantity and a note. The
place, the unit and the snapshot of what the floor was looking at are read from
the database here, so a request cannot claim a floor, unit or shortage the job
card does not have.

Every write must run inside the caller's transaction. Refusals are raised as
RequisitionError (the router turns them into HTTP errors); a place outside the
caller's grants raises place_scope's HTTPException directly.
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.floor_requisition import rules
from app.modules.stock_take import place_scope
from app.modules.stock_take.services import floor_stock_service
from app.modules.stock_take.services.transactions_service import _normalise_warehouse

OPEN_INDEX = "uq_floor_requisition_open"
STATUSES = ("raised", "issued", "received", "cancelled")
MAX_PAGE_SIZE = 500
_TEXT_LIMIT = 500

COLS = """requisition_id, job_card_id, warehouse, floor, material_sku_name, item_type,
          requested_qty, requested_unit, required_qty, required_unit,
          available_qty, available_unit, shortage_qty, shortage_unit,
          issued_qty, issued_unit, status, note, issue_note, cancel_reason,
          raised_by, raised_at, issued_by, issued_at, received_by, received_at,
          cancelled_by, cancelled_at"""

# What the store needs to know about the job card behind a request, read for the
# list only: a request row carries just the internal job_card_id. One read per
# page, not a JOIN, so the list's WHERE clause (floor, status, job_card_id are
# columns of both tables) stays unqualified and the count query untouched.
_JOB_CARD_KEYS = ("job_card_id", "job_card_number", "fg_sku_name", "customer_name", "batch_number",
                  "process_name", "stage", "status", "entity")
_JOB_CARDS_SQL = f"""
    SELECT {', '.join(_JOB_CARD_KEYS)}
      FROM job_card_v2
     WHERE job_card_id = ANY($1::bigint[])
"""

_QTY = ("requested_qty", "required_qty", "available_qty", "shortage_qty", "issued_qty")
_TS = ("raised_at", "issued_at", "received_at", "cancelled_at")

# The article's indent lines on this job card, RM before PM, in line order.
_INDENTS_SQL = """
    SELECT 'RM' AS item_type, material_sku_name, uom, gross_qty, reqd_qty, rm_indent_id AS line_id
      FROM job_card_rm_indent_v2
     WHERE job_card_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2
    UNION ALL
    SELECT 'PM', material_sku_name, uom, gross_qty, reqd_qty, pm_indent_id
      FROM job_card_pm_indent_v2
     WHERE job_card_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2
     ORDER BY item_type DESC, line_id
"""


class RequisitionError(Exception):
    """A refusal: the router answers HTTP `http_status` with {error, message, details}."""

    # The HTTP status is `http_status`, not `status`: a detail key named
    # `status` (the requisition's current status, on a 409) would otherwise
    # collide with it in **details.
    def __init__(self, http_status: int, error: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = http_status
        self.error = error
        self.message = message
        self.details = details


def row_out(row) -> dict[str, Any]:
    out = dict(row)
    for k in _QTY:
        out[k] = float(out[k]) if out.get(k) is not None else None
    for k in _TS:
        out[k] = out[k].isoformat() if out.get(k) is not None else None
    return out


def _clean(text: Optional[str]) -> Optional[str]:
    t = (text or "").strip()
    return t[:_TEXT_LIMIT] or None


def _not_found(requisition_id: int) -> RequisitionError:
    return RequisitionError(404, "not_found", f"Requisition {requisition_id} does not exist.",
                            requisition_id=requisition_id)


def _qty_or_refuse(value: Any, unit: str) -> Any:
    try:
        return rules.parse_qty(value, unit)
    except rules.QtyError as exc:
        raise RequisitionError(400, "qty_invalid", str(exc), unit=unit) from None


def _is_open_clash(exc: asyncpg.UniqueViolationError) -> bool:
    return (getattr(exc, "constraint_name", None) or "") == OPEN_INDEX or OPEN_INDEX in str(exc)


async def raise_requisition(conn, user, *, job_card_id: int, material_sku_name: str,
                            requested_qty: Any, note: Optional[str]) -> dict[str, Any]:
    jc = await conn.fetchrow(
        "SELECT job_card_id, factory, floor, bom_id FROM job_card_v2 WHERE job_card_id = $1",
        job_card_id)
    if not jc:
        raise RequisitionError(404, "job_card_not_found", f"Job card {job_card_id} does not exist.",
                               job_card_id=job_card_id)
    warehouse = _normalise_warehouse(jc["factory"])
    floor = (jc["floor"] or "").strip()
    if not warehouse or not floor:
        raise RequisitionError(422, "job_card_has_no_place",
                               "This job card has no plant or floor, so material cannot be requested to it.",
                               job_card_id=job_card_id)
    place_scope.assert_place_allowed(user, warehouse, floor)

    key = rules.article_key(material_sku_name)
    from app.modules.production.services import jc_bom_changes
    # BOM changes (spec 2f): the plan-line lock first, then the article's live change.
    await jc_bom_changes.lock_line_for_card(conn, job_card_id)
    change = await jc_bom_changes.live_change_for(conn, job_card_id, material_sku_name) if key else None
    if change is not None and change["change_type"] == "removed":
        raise RequisitionError(422, "article_removed_from_job_card",
                               f"{(material_sku_name or '').strip()} was removed from this job card's BOM.",
                               material_sku_name=material_sku_name)
    bom = None
    indents: list = []
    if key:
        if jc["bom_id"] is not None:
            bom = await conn.fetchrow(
                "SELECT material_sku_name, item_type FROM bom_line "
                "WHERE bom_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2 "
                "ORDER BY line_number LIMIT 1",
                jc["bom_id"], key)
        indents = list(await conn.fetch(_INDENTS_SQL, job_card_id, key))
    added = change if (change is not None and change["change_type"] == "added") else None
    if bom is None and not indents and added is None:
        raise RequisitionError(422, "article_not_on_job_card",
                               f"{(material_sku_name or '').strip()!r} is not on this job card's BOM or indents.",
                               material_sku_name=material_sku_name)

    if bom is None and not indents:
        # An article added to this job card (job_card_bom_change).
        article = added["material_sku_name"].strip()
        item_type = rules.article_key(added["item_type"]) or None
        unit = rules.unit_for(None, item_type)
        indent_lines = ([(unit, added["required_qty"], added["required_qty"])]
                        if added.get("required_qty") is not None else [])
    else:
        first = bom if bom is not None else indents[0]
        article = first["material_sku_name"].strip()
        item_type = rules.article_key(first["item_type"]) or None
        unit = rules.unit_for(indents[0]["uom"] if indents else None, item_type)
        indent_lines = [(line["uom"], line["gross_qty"], line["reqd_qty"]) for line in indents]
    qty = _qty_or_refuse(requested_qty, unit)

    stock = await floor_stock_service.fetch_floor_stock(conn, warehouse=warehouse, floor=floor)
    snap = rules.snapshot(
        unit=unit,
        indent_lines=indent_lines,
        stock=[s for s in stock["items"] if rules.article_key(s["item_name"]) == key],
    )
    actor = rules.actor_name(user)

    async def _insert():
        # A fresh number on EACH attempt, so a same-millisecond clash can retry.
        return await conn.fetchrow(
            f"""
            INSERT INTO floor_requisition (
                requisition_id, job_card_id, warehouse, floor, material_sku_name, item_type,
                requested_qty, requested_unit, required_qty, required_unit,
                available_qty, available_unit, shortage_qty, shortage_unit,
                note, raised_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            RETURNING {COLS}
            """,
            new_short_time_id(), job_card_id, warehouse, floor, article, item_type,
            qty, unit,
            snap.required, unit if snap.required is not None else None,
            snap.available, unit,
            snap.shortage, unit if snap.shortage is not None else None,
            _clean(note), actor,
        )

    try:
        row = await insert_with_pk_retry(conn, _insert)
    except asyncpg.UniqueViolationError as exc:
        if not _is_open_clash(exc):
            raise
        open_id = await conn.fetchval(
            "SELECT requisition_id FROM floor_requisition "
            "WHERE job_card_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2 AND status = 'raised'",
            job_card_id, key)
        raise RequisitionError(409, "open_requisition_exists",
                               f"Requisition {open_id} for this article is still waiting for store.",
                               requisition_id=open_id) from None
    return row_out(row)


async def _load(conn, user, requisition_id: int):
    row = await conn.fetchrow(f"SELECT {COLS} FROM floor_requisition WHERE requisition_id = $1",
                              requisition_id)
    if not row:
        raise _not_found(requisition_id)
    place_scope.assert_place_allowed(user, row["warehouse"], row["floor"])
    return row


async def _transition(conn, requisition_id: int, from_status: str, set_sql: str,
                      *args: Any) -> dict[str, Any]:
    """One guarded UPDATE: it moves the row only if it is still in `from_status`,
    so two people acting at once cannot both win. set_sql's parameters start at $3."""
    row = await conn.fetchrow(
        f"UPDATE floor_requisition SET {set_sql} "
        f"WHERE requisition_id = $1 AND status = $2 RETURNING {COLS}",
        requisition_id, from_status, *args)
    if row:
        return row_out(row)
    current = await conn.fetchval("SELECT status FROM floor_requisition WHERE requisition_id = $1",
                                  requisition_id)
    if current is None:
        raise _not_found(requisition_id)
    raise RequisitionError(409, "status_changed",
                           f"Requisition {requisition_id} is already {current}. Reload to see it.",
                           requisition_id=requisition_id, status=current)


async def issue_requisition(conn, user, requisition_id: int, *, issued_qty: Any,
                            issue_note: Optional[str]) -> dict[str, Any]:
    row = await _load(conn, user, requisition_id)
    qty = _qty_or_refuse(issued_qty, row["requested_unit"])
    return await _transition(
        conn, requisition_id, "raised",
        "status = 'issued', issued_qty = $3, issued_unit = requested_unit, "
        "issue_note = $4, issued_by = $5, issued_at = now()",
        qty, _clean(issue_note), rules.actor_name(user))


async def receive_requisition(conn, user, requisition_id: int) -> dict[str, Any]:
    await _load(conn, user, requisition_id)
    return await _transition(
        conn, requisition_id, "issued",
        "status = 'received', received_by = $3, received_at = now()",
        rules.actor_name(user))


async def cancel_requisition(conn, user, requisition_id: int, *,
                             reason: Optional[str]) -> dict[str, Any]:
    await _load(conn, user, requisition_id)
    why = _clean(reason)
    if not why:
        raise RequisitionError(400, "reason_required", "Give a reason for cancelling.",
                               requisition_id=requisition_id)
    return await _transition(
        conn, requisition_id, "raised",
        "status = 'cancelled', cancel_reason = $3, cancelled_by = $4, cancelled_at = now()",
        why, rules.actor_name(user))


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def list_requisitions(conn, user, *, status: Optional[str] = None,
                            warehouse: Optional[str] = None, floor: Optional[str] = None,
                            job_card_id: Optional[int] = None, search: Optional[str] = None,
                            page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """Newest raised first, only inside the caller's granted places."""
    where: list[str] = []
    args: list[Any] = []

    def add(sql: str, value: Any) -> None:
        args.append(value)
        where.append(sql.format(f"${len(args)}"))

    granted_w, granted_f = place_scope.granted(user)
    if granted_w:
        add("warehouse = ANY({})", granted_w)
    if granted_f:
        add("UPPER(floor) = ANY({})", granted_f)
    if status:
        add("status = {}", status)
    if warehouse and warehouse.strip():
        add("warehouse = {}", _normalise_warehouse(warehouse))
    if floor and floor.strip():
        add("UPPER(floor) = {}", floor.strip().upper())
    if job_card_id is not None:
        add("job_card_id = {}", job_card_id)
    for word in (search or "").upper().split():
        add("UPPER(material_sku_name) LIKE {}", _like(word))

    clause = f" WHERE {' AND '.join(where)}" if where else ""
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), MAX_PAGE_SIZE)
    total = await conn.fetchval(f"SELECT COUNT(*) FROM floor_requisition{clause}", *args)
    rows = await conn.fetch(
        f"SELECT {COLS} FROM floor_requisition{clause} "
        f"ORDER BY raised_at DESC, requisition_id DESC "
        f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
        *args, page_size, (page - 1) * page_size)
    cards = await job_cards_by_id(conn, [r["job_card_id"] for r in rows])
    responses = await store_responses_by_id(conn, [r["requisition_id"] for r in rows])
    items = [{**row_out(r), "job_card": cards.get(r["job_card_id"]),
              "store_response": responses.get(r["requisition_id"])} for r in rows]
    return {"items": items, "total": int(total or 0), "page": page, "page_size": page_size}


async def has_store_response_columns(conn) -> bool:
    """Whether migration 112 has run. Checked instead of assumed, so the backend can
    deploy before the migration without the list breaking."""
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'floor_requisition' AND column_name = 'store_response')"))


async def store_responses_by_id(conn, ids: list[int]) -> dict[int, dict[str, Any]]:
    """requisition_id -> store's latest WhatsApp reply {response, by, at}, for the
    requests that have one. Empty when migration 112 has not run."""
    wanted = sorted({int(i) for i in ids if i is not None})
    if not wanted or not await has_store_response_columns(conn):
        return {}
    rows = await conn.fetch(
        "SELECT requisition_id, store_response, store_response_by, store_response_at "
        "FROM floor_requisition WHERE requisition_id = ANY($1::bigint[]) AND store_response IS NOT NULL",
        wanted)
    return {r["requisition_id"]: {"response": r["store_response"], "by": r["store_response_by"],
                                  "at": r["store_response_at"].isoformat() if r["store_response_at"] else None}
            for r in rows}


async def job_cards_by_id(conn, ids: list[int]) -> dict[int, dict[str, Any]]:
    """job_card_id -> the job card's number, product, customer, batch and stage.

    A request's job card cannot be deleted (ON DELETE RESTRICT), but a soft-deleted
    or cancelled one still reads here: the request is history and names what it was
    for. Only ids on the page are read, sorted so the statement is stable."""
    wanted = sorted({int(i) for i in ids if i is not None})
    if not wanted:
        return {}
    rows = await conn.fetch(_JOB_CARDS_SQL, wanted)
    return {r["job_card_id"]: {k: r[k] for k in _JOB_CARD_KEYS} for r in rows}
