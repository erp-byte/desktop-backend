"""Create a new transfer request (doc 05).

Ported from `transfer_backend_reference/.../interunit_tools.py::create_request`
(sync SQLAlchemy → asyncpg). Inserts a header row into
`interunit_transfer_requests` with status 'Pending' plus one
`interunit_transfer_request_lines` row per article, inside a single
transaction. Reuses the query_service row mappers so the response envelope
matches the list/get endpoints (RequestWithLines).

The From and To sites are stored by their canonical names (app.core.sites:
'ESKIMO' -> 'Eskimo', 'D-39' -> 'Savla D-39'), whatever spelling the form sent;
the transfer dispatched from the request takes them from here.

update_request edits a request while it is Pending (before the store accepts it): only
its raiser or an admin, and the lines are replaced as a whole."""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.core.sites import canonical_site, same_site
from app.modules.auth.middleware import AuthUser
from app.modules.transfer import permissions, schemas
from app.modules.transfer.services import request_notify
from app.modules.transfer.services.query_service import (
    _convert_date,
    _map_request_header,
    _map_request_line,
)

# The request header as create and update return it (mapped by _map_request_header).
_HEADER_COLUMNS = (
    "id, request_no, request_date, from_site, to_site, reason_code, remarks, status, "
    "reject_reason, created_by, created_ts, rejected_ts, updated_at, "
    "store_decision, store_decided_by, store_decided_at, store_reject_reason, "
    "manager_decision, manager_decided_by, manager_decided_at, manager_reject_reason, "
    "store_hold_reason, manager_hold_reason"
)


# DB chk_uom constraint on interunit_transfer_request_lines. Anything outside
# this set (e.g. the form's legacy "BAG") is stored as NULL rather than raising
# a CheckViolation 500 — NULL satisfies the CHECK.
_ALLOWED_UOM = {"KG", "PCS", "BOX", "CARTON"}


def _generate_request_no() -> str:
    """Fallback request number (minute precision) — mirrors reference."""
    return "REQ" + datetime.now().strftime("%Y%m%d%H%M")


def _uom(v) -> str | None:
    u = (v or "").upper().strip()
    return u if u in _ALLOWED_UOM else None


def _f(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(float(v)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


async def create_request(conn, data: schemas.RequestCreate, created_by: str) -> dict:
    request_date = _convert_date(data.form_data.request_date)
    request_no = (
        data.computed_fields.request_no
        if data.computed_fields and data.computed_fields.request_no
        else _generate_request_no()
    )

    async with conn.transaction():
        async def _ins_request():
            return await conn.fetchrow(
                f"""
                INSERT INTO interunit_transfer_requests
                    (request_no, request_date, from_site, to_site,
                     reason_code, remarks, status, created_by, created_ts, id)
                VALUES ($1, $2, $3, $4, $5, $6, 'Pending', $7, $8, $9)
                RETURNING {_HEADER_COLUMNS}
                """,
                request_no,
                request_date,
                canonical_site(data.form_data.from_warehouse),
                canonical_site(data.form_data.to_warehouse),
                data.form_data.reason_description or "General Transfer",
                data.form_data.reason_description or "No remarks",
                created_by,
                datetime.now(),
                new_short_time_id(),
            )
        header = await insert_with_pk_retry(conn, _ins_request)
        request_id = header["id"]
        lines = await _insert_lines(conn, request_id, data.article_data)

    result = _map_request_header(dict(header))
    result["lines"] = lines
    # Committed: tell the supplying site's store (in the background, best-effort).
    request_notify.notify("raised", request_id)
    return result


async def _insert_lines(conn, request_id: int, article_data) -> list:
    """Insert a request's lines (create, and an edit after it drops the old ones) and
    return them as the API maps them. The caller owns the transaction."""
    lines = []
    for line in article_data:
        pack_size_f = _f(line.pack_size)
        qty_i = _i(line.quantity)
        unit_pack = _f(line.unit_pack_size)

        fe_net = _f(line.net_weight)
        if fe_net > 0:
            net_weight = round(fe_net, 3)
        elif (line.material_type or "").upper() == "FG":
            net_weight = round(unit_pack * pack_size_f * qty_i, 3)
        else:
            net_weight = round(pack_size_f * qty_i, 3)

        fe_total = _f(line.total_weight)
        total_weight = round(fe_total, 3) if fe_total > 0 else net_weight

        async def _ins_request_line(line=line, pack_size_f=pack_size_f, qty_i=qty_i,
                                    unit_pack=unit_pack, net_weight=net_weight,
                                    total_weight=total_weight):
            return await conn.fetchrow(
                """
                INSERT INTO interunit_transfer_request_lines
                    (request_id, rm_pm_fg_type, item_category, sub_category,
                     item_desc_raw, pack_size, qty, uom,
                     unit_pack_size, net_weight, total_weight, lot_number, id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                RETURNING id, request_id, rm_pm_fg_type, item_category, sub_category,
                          item_desc_raw, pack_size, qty, uom,
                          unit_pack_size, net_weight, total_weight, lot_number,
                          created_at, updated_at
                """,
                request_id,
                line.material_type,
                line.item_category,
                line.sub_category,
                line.item_description,
                pack_size_f,
                qty_i,
                _uom(line.uom),
                unit_pack,
                net_weight,
                total_weight,
                line.lot_number,
                new_short_time_id(),
            )
        row = await insert_with_pk_retry(conn, _ins_request_line)
        lines.append(_map_request_line(dict(row)))
    return lines


async def update_request(conn, request_id: int, data: schemas.RequestCreate, user: AuthUser) -> dict:
    """Edit a request while it is Pending, i.e. before the store has accepted it: the
    date, route, reason and lines are replaced by the form's (the request number, the
    raiser and the status stay). Only the raiser or an admin may edit it
    (permissions.assert_can_edit_request), and a warehouse-locked raiser only for their
    own site (assert_can_request_for, the router checks the same on create). The row is
    locked first, so an edit never runs alongside the store's decision: a request the
    store has just accepted or rejected is refused with 409.

    A request the store has put on hold is still Pending and is edited the same way; the
    edit takes the hold off (store_decision / by / at back to NULL; store_hold_reason
    stays as history) and the store is sent the "raised" notice again."""
    async with conn.transaction():
        req = await conn.fetchrow(
            "SELECT id, request_no, status, created_by, from_site, store_decision "
            "FROM interunit_transfer_requests WHERE id = $1 FOR UPDATE", request_id)
        if not req:
            raise HTTPException(404, "Request not found")
        permissions.assert_can_edit_request(user, req["created_by"])
        status = req["status"] or "Pending"
        if status != "Pending":
            raise HTTPException(
                409, f"Request {req['request_no'] or request_id} is {status}; only a Pending request "
                     "can be edited, before the store accepts it.")
        permissions.assert_can_request_for(user, data.form_data.to_warehouse)
        held = req["store_decision"] == "Held"
        if held:
            await conn.execute(
                "UPDATE interunit_transfer_requests "
                "SET store_decision = NULL, store_decided_by = NULL, store_decided_at = NULL WHERE id = $1",
                request_id)
        header = await conn.fetchrow(
            f"""
            UPDATE interunit_transfer_requests
            SET request_date = $1, from_site = $2, to_site = $3, reason_code = $4,
                remarks = $5, updated_at = $6
            WHERE id = $7
            RETURNING {_HEADER_COLUMNS}
            """,
            _convert_date(data.form_data.request_date),
            canonical_site(data.form_data.from_warehouse),
            canonical_site(data.form_data.to_warehouse),
            data.form_data.reason_description or "General Transfer",
            data.form_data.reason_description or "No remarks",
            datetime.now(),
            request_id,
        )
        await conn.execute(
            "DELETE FROM interunit_transfer_request_lines WHERE request_id = $1", request_id)
        lines = await _insert_lines(conn, request_id, data.article_data)

    result = _map_request_header(dict(header))
    result["lines"] = lines
    # Committed. A new supplying site's store has not been told about the request, and a
    # store that held it is to answer the edited one: send it the "raised" notice. (An
    # answer from the old site's store is refused by the decision service's warehouse lock.)
    if held or not same_site(req["from_site"], header["from_site"]):
        request_notify.notify("raised", request_id)
    return result
