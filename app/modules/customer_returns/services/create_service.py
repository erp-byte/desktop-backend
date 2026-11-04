"""Customer-Returns write side: create/update/delete (header + lines), and the
form's save of the whole return.

Owns its own transaction (mirrors transfer/create_service). The rtv_id string is
the header PK; a same-second collision retries inside a SAVEPOINT with a numeric
suffix so the common id stays 'CR-YYYYMMDDHHMMSS'.

The form saves in one step: create_cr takes the new return's boxes with its header
and lines, and save_cr writes an existing one's header, lines and boxes in one
transaction — a refusal or failure part-way saves none of it. (The header, lines
and boxes endpoints stay for anything else that writes one part.) A save that
changes the figures an approver decides on records it (approval_log), and the
router then sends the return again if it had been sent (send_service.after_save).

Edits and deletes apply only to an undecided return (_lock_undecided_header). A
delete leaves a record of what was deleted and by whom in cr_deletion_log
(app/db/134_cr_deletion_log.sql).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import HTTPException

from app.core.sites import is_cold_site
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import (
    approval_log, approval_service, box_service, mail_service, validate,
)
from app.modules.customer_returns.services import query_service as q
from app.modules.customer_returns.tables import cr_table_names

_IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


def _generate_cr_id() -> str:
    return "CR-" + datetime.now(_IST).strftime("%Y%m%d%H%M%S")


async def _insert_line(conn, tables: dict, header_id: int, line: schemas.CRLineCreate,
                       nums: dict) -> None:
    # Legacy _rtv_lines is keyed by header_id + surrogate id (no rtv_id, no natural
    # unique constraint) — a plain INSERT. Callers always insert under a fresh header
    # (create) or after DELETE-ing the header's lines (update_cr_lines), so there is
    # never a live conflict to upsert against. `nums` is the line's numbers as
    # validate.lines checked them (value = qty x rate, never the browser's).
    qty, rate, value = nums["qty"], nums["rate"], nums["value"]
    net_weight, carton_weight = nums["net_weight"], nums["carton_weight"]
    await conn.execute(
        f"""
        INSERT INTO {tables['lines']}
            (header_id, item_description, material_type, item_category, sub_category, sale_group,
             uom, qty, rate, value, conversion, net_weight, carton_weight,
             lot_number, item_mark, spl_remarks, vakkal)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
        """,
        header_id, line.item_description, line.material_type, line.item_category,
        line.sub_category, line.sale_group, line.uom, qty, rate, value, line.conversion,
        net_weight, carton_weight,
        line.lot_number, line.item_mark, line.spl_remarks, line.vakkal,
    )


async def _insert_header(conn, tables: dict, header: schemas.CRHeaderCreate,
                         created_by: str) -> tuple[str, int]:
    """Insert the header; return (rtv_id, surrogate id). The id is the FK lines/boxes
    hang off in the legacy _rtv_* schema. rtv_id is UNIQUE — a same-second collision
    retries with a numeric suffix inside a SAVEPOINT.

    rtv_date and created_ts are stamped NOW() explicitly, as IMS's create_rtv did,
    rather than left to legacy column defaults that may not exist. NOW() lands in the
    naive columns as UTC (the session TimeZone), like every IMS row; the read side
    shows it in IST. The payload carries no rtv_date (CRHeaderCreate has none)."""
    base = _generate_cr_id()
    conversion = float(validate.number(header.conversion, "Conversion"))
    for attempt in range(6):
        cand = base if attempt == 0 else f"{base}-{attempt}"
        try:
            async with conn.transaction():  # SAVEPOINT — isolates rtv_id-collision retry
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO {tables['header']}
                        (rtv_id, rtv_date, factory_unit, customer, invoice_number, challan_no,
                         dn_no, conversion, sales_poc, sales_poc_email, business_head, remark,
                         vehicle_number, transporter_name, driver_name, inward_manager,
                         status, created_by, created_ts)
                    VALUES ($1,NOW(),$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                            'Pending',$16,NOW())
                    RETURNING id
                    """,
                    cand, header.factory_unit, header.customer, header.invoice_number,
                    header.challan_no, header.dn_no, conversion, header.sales_poc,
                    header.sales_poc_email, header.business_head, header.remark,
                    header.vehicle_number, header.transporter_name, header.driver_name,
                    header.inward_manager, created_by,
                )
            return cand, row["id"]
        except asyncpg.UniqueViolationError:
            continue
    raise HTTPException(
        500,
        detail={"error": "cr_id_generation_failed",
                "message": "Could not allocate a unique CR id"},
    )


async def _check_business_head(conn, business_head, created_by) -> None:
    """The business head chosen must be able to approve: someone, with an e-mail on
    file (the approval mail and the decision checks go by it), and not leaving the
    return with nobody to decide it, since whoever raised a return can't decide it
    (approval_service) and a deputy may not exist. 422 otherwise."""
    name = str(business_head or "").strip()
    if not name:
        raise validate.refuse("required", "Pick a business head: they approve the return.",
                              field="business_head")
    routing = await mail_service.fetch_routing(conn)
    if not mail_service.lookup_business_head_email(routing, name):
        raise validate.refuse(
            "business_head_has_no_email",
            f"{name} has no e-mail on file, so they can't approve the return. Pick another business head.",
            field="business_head", value=name)
    if not mail_service.eligible_approvers(mail_service.resolve_approvers(routing, name), created_by):
        raise validate.refuse(
            "no_other_approver",
            f"{name} raised this return, so they can't approve it, and there is no deputy approver. "
            "Pick another business head.", field="business_head", value=name)


async def _check_duplicate_invoice(conn, tables: dict, *, customer, invoice, challan,
                                   own_id=None) -> None:
    """A customer's invoice (or challan) already on another return that wasn't rejected
    is 409 duplicate_invoice, naming those returns. A second return from one invoice
    can be real (goods sent back in two lots), so the screen asks and, once the user
    confirms, saves with allow_duplicate_invoice."""
    who = str(customer or "").strip()
    inv, ch = str(invoice or "").strip(), str(challan or "").strip()
    if not who or not (inv or ch):
        return
    rows = await conn.fetch(
        f"""
        SELECT rtv_id, status, btrim(invoice_number) AS invoice_number, btrim(challan_no) AS challan_no
          FROM {tables['header']}
         WHERE ($1::bigint IS NULL OR id <> $1)
           AND lower(btrim(customer)) = lower($2)
           AND ((NULLIF($3, '') IS NOT NULL AND lower(btrim(invoice_number)) = lower($3))
             OR (NULLIF($4, '') IS NOT NULL AND lower(btrim(challan_no)) = lower($4)))
           AND COALESCE(btrim(status), '') <> 'Rejected'
         ORDER BY rtv_id LIMIT 10
        """, own_id, who, inv, ch)
    if not rows:
        return
    on_invoice = inv and any((r["invoice_number"] or "").lower() == inv.lower() for r in rows)
    what = f"Invoice {inv}" if on_invoice else f"Challan {ch}"
    others = ", ".join(f"{r['rtv_id']} ({approval_service.status_label(r['status'])})" for r in rows)
    raise HTTPException(
        409,
        detail={"error": "duplicate_invoice",
                "message": f"{what} of {who} is already on {others}.",
                "details": {"customer": who, "invoice_number": inv or None, "challan_no": ch or None,
                            "returns": [{"rtv_id": r["rtv_id"], "status": approval_service.status_label(r["status"])}
                                        for r in rows]}},
    )


async def create_cr(conn, company: str, data: schemas.CRCreate, created_by: str,
                    allow_duplicate_invoice: bool = False) -> dict:
    if data.company.upper() != (company or "").strip().upper():
        raise HTTPException(
            400,
            detail={"error": "company_mismatch",
                    "message": "path company and body company differ",
                    "details": {"path": company, "body": data.company}},
        )
    tables = cr_table_names(company)
    validate.header(data.header)
    nums = validate.lines(data.lines)
    for b in data.boxes or []:
        validate.box(b)
    await _check_business_head(conn, data.header.business_head, created_by)
    if not allow_duplicate_invoice:
        h = data.header
        await _check_duplicate_invoice(conn, tables, customer=h.customer, invoice=h.invoice_number,
                                       challan=h.challan_no)
    async with conn.transaction():
        cr_id, header_id = await _insert_header(conn, tables, data.header, created_by)
        for line, n in zip(data.lines, nums):
            await _insert_line(conn, tables, header_id, line, n)
        if data.boxes:
            # Nothing is stored yet, so a box naming a label is refused (box_changed):
            # a label is printed only on a return that exists.
            incoming = box_service.by_key(data.boxes)
            box_set = await box_service.read_box_set(conn, tables, header_id, cr_id, incoming)
            await box_service.write_box_set(conn, tables, header_id, cr_id, incoming, box_set, created_by)
    # Read back AFTER commit so the response matches get/list exactly.
    return await q.get_cr(conn, company, cr_id)


# No "status": a return is decided only through approval_service.apply_cr_action.
_HEADER_UPDATABLE = [
    "factory_unit", "customer", "invoice_number", "challan_no", "dn_no", "conversion",
    "sales_poc", "sales_poc_email", "business_head", "remark",
    "vehicle_number", "transporter_name", "driver_name", "inward_manager",
]


async def _lock_undecided_header(conn, tables: dict, cr_id: str, *,
                                 doing: str = "its details and lines can't be changed") -> dict:
    """The header row (id, status, factory_unit) of a return that may still be edited
    or deleted, locked until the caller's transaction ends. Only an undecided return
    (Pending / On Hold, as approval_service reads them) may: once Approved, Approved
    (boxes saved) or Rejected, its articles, quantities and rates are what the
    approver decided on, so the edit or delete is 409 already_decided. FOR UPDATE
    makes a decision landing at the same moment wait, never slip in between this
    check and the write. 404 when the return is absent."""
    row = await conn.fetchrow(
        f"SELECT id, status, factory_unit FROM {tables['header']} WHERE rtv_id = $1 FOR UPDATE",
        cr_id)
    if row is None:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    if not approval_service.is_undecided(row["status"]):
        label = approval_service.status_label(row["status"])
        raise HTTPException(
            409,
            detail={"error": "already_decided",
                    "message": f"{cr_id} is already “{label}”, so {doing}.",
                    "details": {"rtv_id": cr_id, "status": label}},
        )
    return dict(row)


async def _refuse_cold_switch_with_boxes(conn, tables: dict, cr_id: str, locked: dict,
                                         new_unit) -> None:
    """A return that has boxes can't move between a cold store and a warehouse: cold
    boxes are refused (box_service), so switching the unit to box it and switching
    back would slip past that refusal. Between two warehouses, or two cold stores, is
    a plain correction and stays allowed."""
    if is_cold_site(locked["factory_unit"]) == is_cold_site(new_unit):
        return
    boxes = await conn.fetchval(
        f"SELECT COUNT(*) FROM {tables['boxes']} WHERE header_id = $1", locked["id"])
    if boxes:
        raise HTTPException(
            409,
            detail={"error": "factory_unit_locked",
                    "message": f"{cr_id} already has boxes, so its factory unit can't be "
                               "changed between a cold store and a warehouse.",
                    "details": {"rtv_id": cr_id, "factory_unit": locked["factory_unit"],
                                "requested": new_unit, "boxes": int(boxes)}},
        )


def _header_sets(provided: dict) -> tuple[list[str], list]:
    """The header UPDATE's `col = $n` list and its values, for the fields provided."""
    sets: list[str] = []
    args: list = []
    for col in _HEADER_UPDATABLE:
        if col in provided:
            val = provided[col]
            if col == "conversion":
                val = float(validate.number(val, "Conversion"))
            args.append(val); sets.append(f"{col} = ${len(args)}")
    sets.append("updated_at = NOW()")
    return sets, args


async def _check_header_change(conn, tables: dict, cr_id: str, provided: dict,
                               allow_duplicate_invoice: bool) -> None:
    """The business-head and duplicate-invoice checks a header edit needs. Made before
    the write's transaction: the approver lookup reads tables that may be missing
    (fetch_routing degrades), and a failed statement would poison it."""
    if provided.keys() & {"business_head", "customer", "invoice_number", "challan_no"}:
        stored = await conn.fetchrow(
            f"SELECT id, customer, invoice_number, challan_no, business_head, created_by "
            f"FROM {tables['header']} WHERE rtv_id = $1", cr_id)
        if stored is not None:     # absent: the lock below says 404
            # Checked only when it changes: an older return keeps the head it was sent to.
            bh = provided.get("business_head")
            if bh is not None and str(bh).strip() != str(stored["business_head"] or "").strip():
                await _check_business_head(conn, bh, stored["created_by"])
            changed = any(k in provided and str(provided[k] or "").strip() != str(stored[k] or "").strip()
                          for k in ("customer", "invoice_number", "challan_no"))
            if changed and not allow_duplicate_invoice:
                await _check_duplicate_invoice(
                    conn, tables, own_id=stored["id"],
                    customer=provided.get("customer", stored["customer"]),
                    invoice=provided.get("invoice_number", stored["invoice_number"]),
                    challan=provided.get("challan_no", stored["challan_no"]))


async def _write_header(conn, tables: dict, cr_id: str, locked: dict, data: schemas.CRHeaderUpdate,
                        provided: dict, sets: list[str], args: list):
    """The header UPDATE, on the row _lock_undecided_header holds."""
    validate.header(data, stored_unit=locked["factory_unit"], partial=True)
    if "factory_unit" in provided:
        await _refuse_cold_switch_with_boxes(conn, tables, cr_id, locked, provided["factory_unit"])
    args = [*args, locked["id"]]
    return await conn.fetchrow(
        f"UPDATE {tables['header']} SET {', '.join(sets)} WHERE id = ${len(args)} "
        f"RETURNING {q.HEADER_COLS}",
        *args,
    )


async def update_cr(conn, company: str, cr_id: str, data: schemas.CRHeaderUpdate,
                    allow_duplicate_invoice: bool = False) -> dict:
    tables = cr_table_names(company)
    provided = data.model_dump(exclude_none=True)
    if not provided:
        raise HTTPException(
            400,
            detail={"error": "empty_update", "message": "Provide at least one field to update"},
        )
    sets, args = _header_sets(provided)
    await _check_header_change(conn, tables, cr_id, provided, allow_duplicate_invoice)
    async with conn.transaction():
        locked = await _lock_undecided_header(conn, tables, cr_id)
        row = await _write_header(conn, tables, cr_id, locked, data, provided, sets, args)
    return q._map_header_row(dict(row))


async def _drop_removed_articles(conn, tables: dict, header_id: int, cr_id: str, new_articles: set) -> int:
    """Boxes of the articles a lines edit takes off the return (renaming one is taking it
    off and putting another on). A box is linked to its line by the article's exact name,
    so left behind it would be an orphan: counted in the list, missing from Excel. A
    printed one has a label on a carton, so the edit is refused (409 article_has_labels);
    unprinted ones go with their line. Boxes that matched no line before are left alone.
    Returns how many boxes were removed."""
    old = {r["item_description"] for r in await conn.fetch(
        f"SELECT item_description FROM {tables['lines']} WHERE header_id = $1", header_id)}
    gone = sorted(a for a in old - new_articles if a is not None)
    if not gone:
        return 0
    rows = await conn.fetch(
        f"SELECT article_description, COUNT(*) AS boxes, "
        f"COUNT(NULLIF(btrim(box_id), '')) AS printed FROM {tables['boxes']} "
        "WHERE header_id = $1 AND article_description = ANY($2::text[]) GROUP BY article_description",
        header_id, gone)
    labelled = [(r["article_description"], int(r["printed"])) for r in rows if r["printed"]]
    if labelled:
        names = ", ".join(f"{a} ({n} printed)" for a, n in labelled)
        raise HTTPException(
            409,
            detail={"error": "article_has_labels",
                    "message": f"{names} can't be removed or renamed: its boxes have labels on cartons. "
                               "Keep the article, or void its labels first.",
                    "details": {"rtv_id": cr_id, "articles": [a for a, _ in labelled]}},
        )
    if not rows:
        return 0
    done = await conn.execute(
        f"DELETE FROM {tables['boxes']} WHERE header_id = $1 AND article_description = ANY($2::text[])",
        header_id, gone)
    return int(str(done).split()[-1]) if str(done).split()[-1].isdigit() else 0


async def _replace_lines(conn, tables: dict, header_id: int, cr_id: str, lines, nums) -> int:
    """Put ``lines`` in place of the stored ones; returns the boxes removed with the
    articles taken off (_drop_removed_articles)."""
    removed = await _drop_removed_articles(
        conn, tables, header_id, cr_id, {line.item_description for line in lines})
    await conn.execute(f"DELETE FROM {tables['lines']} WHERE header_id = $1", header_id)
    for line, n in zip(lines, nums):
        await _insert_line(conn, tables, header_id, line, n)
    # DELETE-ing the lines nulled every box's rtv_line_id (FK ON DELETE SET NULL);
    # re-point each box to its re-created line so IMS's box->line join survives.
    await conn.execute(
        f"""
        UPDATE {tables['boxes']} b SET rtv_line_id = l.id
          FROM {tables['lines']} l
         WHERE b.header_id = $1 AND l.header_id = $1
           AND b.article_description = l.item_description
        """,
        header_id,
    )
    return removed


async def update_cr_lines(conn, company: str, cr_id: str,
                          data: schemas.CRLinesUpdateRequest) -> dict:
    tables = cr_table_names(company)
    nums = validate.lines(data.lines)
    async with conn.transaction():
        header_id = (await _lock_undecided_header(conn, tables, cr_id))["id"]
        removed = await _replace_lines(conn, tables, header_id, cr_id, data.lines, nums)
        version = await q.box_version(conn, tables, header_id)
    return {"status": "updated", "rtv_id": cr_id, "lines_count": len(data.lines),
            "boxes_removed": removed, "box_version": version}


async def save_cr(conn, company: str, cr_id: str, data: schemas.CRSaveRequest, *,
                  actor: str = "", allow_duplicate_invoice: bool = False) -> dict:
    """Save an undecided return as the form holds it — its header, its lines and
    (unless ``data.boxes`` is None) its boxes — in ONE transaction: every refusal
    (a decided return, a printed article taken off, a box changed on another page,
    a bad figure) and every failure saves none of it.

    Everything is checked before anything is written: the values, then the business
    head and the invoice (outside the transaction, as update_cr), then — under the box
    lock and the header row — that the return is undecided and that its boxes are the
    ones the page loaded (base_version). Then the header, the lines (an article taken
    off takes its unprinted boxes; one with labels refuses), and the boxes as one
    full sync (box_service): a printed box keeps its label, one left out is kept, and
    a label the page doesn't know is refused. A cold return whose boxes IMS put in
    cold stock keeps its boxes: a save that would change them is refused.

    The reply says whether the figures an approver decides on changed
    (figures_changed); such a change is recorded (approval_log) in the same
    transaction, so a link sent before it no longer decides the return."""
    tables = cr_table_names(company)
    provided = data.header.model_dump(exclude_none=True)
    sets, args = _header_sets(provided)
    nums = validate.lines(data.lines)
    incoming = None
    if data.boxes is not None:
        for b in data.boxes:
            validate.box(b)
        incoming = box_service.by_key(data.boxes)
    await _check_header_change(conn, tables, cr_id, provided, allow_duplicate_invoice)
    counts = {"inserted": 0, "updated": 0, "deleted": 0, "kept_printed": 0, "reprint": []}
    async with conn.transaction():
        # The box lock before the header row, the order every box write takes them in.
        await q.lock_box_writes(conn, company, cr_id)
        locked = await _lock_undecided_header(conn, tables, cr_id)
        header_id = locked["id"]
        if incoming is not None:
            await box_service.assert_base_version(conn, tables, header_id, cr_id, data.base_version)
        before = await q.read_figures(conn, tables, cr_id)
        if provided:
            await _write_header(conn, tables, cr_id, locked, data.header, provided, sets, args)
        removed = await _replace_lines(conn, tables, header_id, cr_id, data.lines, nums)
        if incoming is not None:
            box_set = await box_service.read_box_set(conn, tables, header_id, cr_id, incoming)
            if box_service.box_set_changes(incoming, box_set):
                await box_service.refuse_ims_cold(conn, tables, cr_id, locked["factory_unit"])
                counts = {k: v for k, v in (await box_service.write_box_set(
                    conn, tables, header_id, cr_id, incoming, box_set, actor)).items() if k != "unchanged"}
        after = await q.read_figures(conn, tables, cr_id)
        if after != before:
            await approval_log.record(conn, company, cr_id, "changed", after, actor)
        version = await q.box_version(conn, tables, header_id)
    return {"status": "saved", "rtv_id": cr_id, "lines_count": len(data.lines), "boxes_removed": removed,
            **counts, "box_version": version, "figures_version": after, "figures_changed": after != before}


async def _record_deletion(conn, company: str, detail: dict, deleted_by: str) -> None:
    """Keep what was deleted, and by whom, in cr_deletion_log. Inside a SAVEPOINT so a
    missing table (migration 134 not applied yet) can't abort the delete; the record
    then goes to the application log instead."""
    boxes = detail.get("boxes") or []
    printed = sum(1 for b in boxes if b.get("box_id"))
    snapshot = json.dumps(detail, default=str)
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO cr_deletion_log
                    (company, rtv_id, status, deleted_by, lines_count, boxes_count,
                     printed_boxes, snapshot)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                """,
                company.upper(), detail["rtv_id"], approval_service.status_label(detail.get("status")),
                deleted_by, len(detail.get("lines") or []), len(boxes), printed, snapshot,
            )
    except asyncpg.UndefinedTableError:
        logger.warning("[cr] deleted %s %s by %s (cr_deletion_log missing; snapshot follows) %s",
                       company.upper(), detail["rtv_id"], deleted_by, snapshot)


async def delete_cr(conn, company: str, cr_id: str, deleted_by: str = "") -> dict:
    """Delete an undecided return with its lines and boxes, and record it. A decided
    one is 409 already_decided: what the approver decided on, and any labels on its
    cartons, must not vanish. The returned ``detail`` is the return as it was, for the
    deletion notice; the response model leaves it out."""
    tables = cr_table_names(company)
    async with conn.transaction():
        await _lock_undecided_header(conn, tables, cr_id, doing="it can't be deleted")
        detail = await q.get_cr(conn, company, cr_id)
        await _record_deletion(conn, company, detail, deleted_by)
        # FK ON DELETE CASCADE (lines.header_id / boxes.header_id) removes them with the header.
        await conn.execute(f"DELETE FROM {tables['header']} WHERE rtv_id = $1", cr_id)
    boxes = detail.get("boxes") or []
    return {"success": True, "message": f"Customer return {cr_id} deleted",
            "rtv_id": cr_id, "lines_count": len(detail.get("lines") or []),
            "boxes_count": len(boxes), "printed_boxes": sum(1 for b in boxes if b.get("box_id")),
            "detail": detail}
