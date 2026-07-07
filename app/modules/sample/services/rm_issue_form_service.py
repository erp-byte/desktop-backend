"""RM Issue / Collection Form (Document 015) — NPD plan §10/§11.

A maker-checker requisition that backs Step A of the inventory accounting
backbone. The NPD author *raises an indent* (snapshots the recipe RM lines); the
Store *approves* and then *issues* — recording issued_qty + lot_no per line,
which fires the 265 Goods Issue for OWN lines (customer / off-master lines are
recorded for traceability only, no posting). Maker (requested_by) can never be
the checker (issued_by / approver) — segregation of duties.

State machine: DRAFT -> SUBMITTED -> APPROVED -> ISSUED -> CLOSED (+ CANCELLED).
Numbering: RMIF-YYYYMMDD-NNNN via seq_rm_issue_form.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

from app.modules.sample.services import sample_inventory_service as inv
from app.modules.sample.services import sample_notify_service as notify


def _gen_form_number(seq_val: int) -> str:
    return f"RMIF-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{seq_val:04d}"


def _form_mail_body(form: dict, verb: str) -> str:
    """Plain-text email body mirroring Document 015 (header + RM table)."""
    head = (
        f"RM Issue / Collection Form (Document 015) — {verb.upper()}\n\n"
        f"Form No.      : {form['form_number']}\n"
        f"Trial         : {form.get('trial_name') or '—'}\n"
        f"Product       : {form.get('product_name') or '—'}\n"
        f"Customer      : {form.get('customer_name') or 'Internal Use'}\n"
        f"Purpose       : {form.get('purpose_tag') or '—'}\n"
        f"Status        : {form['status']}\n\n"
        f"{'#':<3} {'Raw Material':<28} {'Own/Cust':<9} {'Reqd':>10} {'Issued':>10}  UOM\n"
        f"{'-' * 74}\n"
    )
    rows = ""
    for i, ln in enumerate(form.get("lines") or [], 1):
        own = "CUSTOMER" if ln.get("ownership") == "CUSTOMER" else "OWN"
        reqd = ln.get("reqd_qty")
        issued = ln.get("issued_qty")
        rows += (f"{i:<3} {str(ln.get('sku_name') or '')[:28]:<28} {own:<9} "
                 f"{(str(reqd) if reqd is not None else '—'):>10} "
                 f"{(str(issued) if issued is not None else '—'):>10}  {ln.get('uom') or 'kg'}\n")
    return head + rows


async def _fetch(conn, form_id: int) -> dict:
    row = await conn.fetchrow("SELECT * FROM rm_issue_form WHERE id = $1", form_id)
    if not row:
        raise HTTPException(404, detail={"error": "not_found",
                                         "message": f"RM issue form {form_id} not found",
                                         "details": {"id": form_id}})
    return dict(row)


def _assert_maker_not_checker(form: dict, user) -> None:
    if form.get("requested_by") is not None and form["requested_by"] == user.user_id:
        raise HTTPException(409, detail={
            "error": "maker_is_checker",
            "message": "The person who raised the indent cannot approve / issue it (segregation of duties)",
            "details": {"requested_by": form["requested_by"]}})


async def get_form(conn, form_id: int) -> dict:
    form = await _fetch(conn, form_id)
    lines = await conn.fetch(
        "SELECT * FROM rm_issue_form_lines WHERE form_id = $1 ORDER BY line_order, id", form_id)
    form["lines"] = [dict(r) for r in lines]
    return form


async def list_forms(conn, *, status: str | None = None, source_type: str | None = None,
                     limit: int = 50, offset: int = 0) -> list[dict]:
    clauses, params = [], []
    if status:
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    if source_type:
        params.append(source_type)
        clauses.append(f"source_type = ${len(params)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([limit, offset])
    rows = await conn.fetch(
        f"""SELECT f.*, (SELECT COUNT(*) FROM rm_issue_form_lines l WHERE l.form_id = f.id) AS line_count
             FROM rm_issue_form f{where}
             ORDER BY f.created_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
        *params)
    return [dict(r) for r in rows]


async def raise_indent(conn, *, payload: dict, user) -> dict:
    """Create the form (a Raise Indent) snapshotting the recipe RM lines. When
    `submit` is true the form goes straight to SUBMITTED and stamps the maker."""
    submit = payload.get("submit", True)
    async with conn.transaction():
        seq = await conn.fetchval("SELECT nextval('seq_rm_issue_form')")
        number = _gen_form_number(seq)
        form_id = await conn.fetchval(
            """
            INSERT INTO rm_issue_form
                (form_number, trial_name, product_name, customer_name, purpose_tag,
                 source_type, source_id, requisition_id, status, requested_by, requested_at, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING id
            """,
            number, payload.get("trial_name"), payload.get("product_name"),
            payload.get("customer_name"), payload.get("purpose_tag"),
            payload.get("source_type"), payload.get("source_id"), payload.get("requisition_id"),
            "SUBMITTED" if submit else "DRAFT", user.user_id,
            datetime.now(timezone.utc) if submit else None, payload.get("notes"))
        for i, ln in enumerate(payload.get("lines") or []):
            await conn.execute(
                """
                INSERT INTO rm_issue_form_lines
                    (form_id, sku_id, sku_name, location, reqd_qty, uom,
                     ownership, is_off_master, notes, line_order)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                form_id, ln.get("sku_id"), ln["sku_name"], ln.get("location"),
                ln.get("reqd_qty", 0), ln.get("uom", "kg"), ln.get("ownership", "OWN"),
                ln.get("is_off_master", False), ln.get("notes"), ln.get("line_order", i))
    form = await get_form(conn, form_id)
    # Notify the Store/Inventory team once the indent is committed (§11). After
    # commit so a mail outage can never roll back the form.
    if form["status"] == "SUBMITTED":
        await notify.notify_event(
            conn, event="RM_FORM_RAISED", message=f"RM Issue Form {form['form_number']} raised"
            f"{(' for ' + form['trial_name']) if form.get('trial_name') else ''}",
            subject=f"[RM Issue Form] {form['form_number']} raised",
            mail_body=_form_mail_body(form, "raised"),
            related_id=form_id, related_type="RM_ISSUE_FORM", entity=form["entity"])
    return form


async def submit_form(conn, form_id: int, *, user) -> dict:
    form = await _fetch(conn, form_id)
    if form["status"] != "DRAFT":
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Only a DRAFT form can be submitted",
                                         "details": {"status": form["status"]}})
    await conn.execute(
        "UPDATE rm_issue_form SET status='SUBMITTED', requested_by=$1, requested_at=NOW(), updated_at=NOW() WHERE id=$2",
        user.user_id, form_id)
    return await get_form(conn, form_id)


async def approve_form(conn, form_id: int, *, user) -> dict:
    """Store (checker) approves the indent. SUBMITTED -> APPROVED."""
    form = await _fetch(conn, form_id)
    if form["status"] != "SUBMITTED":
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Only a SUBMITTED form can be approved",
                                         "details": {"status": form["status"]}})
    _assert_maker_not_checker(form, user)
    await conn.execute(
        "UPDATE rm_issue_form SET status='APPROVED', updated_at=NOW() WHERE id=$1", form_id)
    return await get_form(conn, form_id)


async def issue_form(conn, form_id: int, *, issued: dict[int, dict], user) -> dict:
    """Store records issued_qty + lot_no per line and FIRES Step A (265). OWN lines
    decrement stock; customer / off-master lines are recorded only. APPROVED -> ISSUED.
    `issued` maps line_id -> {"issued_qty": float, "lot_no": str | None}."""
    issued = issued or {}
    async with conn.transaction():
        form = await _fetch(conn, form_id)
        if form["status"] != "APPROVED":
            raise HTTPException(409, detail={"error": "wrong_status",
                                             "message": "Only an APPROVED form can be issued",
                                             "details": {"status": form["status"]}})
        _assert_maker_not_checker(form, user)

        rows = await conn.fetch(
            "SELECT * FROM rm_issue_form_lines WHERE form_id = $1 ORDER BY line_order, id FOR UPDATE", form_id)
        issue_lines = []
        for ln in rows:
            sel = issued.get(ln["id"])
            qty = float(sel["issued_qty"]) if sel and sel.get("issued_qty") is not None else float(ln["issued_qty"] or 0)
            lot = (sel.get("lot_no") if sel else None) or ln["lot_no"]
            await conn.execute(
                "UPDATE rm_issue_form_lines SET issued_qty=$1, lot_no=$2 WHERE id=$3", qty, lot, ln["id"])
            issue_lines.append({
                "sku_name": ln["sku_name"], "qty": qty, "uom": ln["uom"],
                "ownership": ln["ownership"], "is_off_master": ln["is_off_master"],
                "required_qty": float(ln["reqd_qty"] or 0)})

        result = await inv.post_goods_issue(
            conn, lines=issue_lines, reference_id=form_id, reference_type="RM_ISSUE_FORM",
            entity=form["entity"], from_warehouse=None, user=user,
            variance_source=("RM_ISSUE_FORM", form_id),
            notes=f"RM issue form {form['form_number']}")

        await conn.execute(
            """UPDATE rm_issue_form
                  SET status='ISSUED', issued_by=$1, issued_at=NOW(),
                      issue_mat_doc_id=$2, updated_at=NOW()
                WHERE id=$3""",
            user.user_id, result.get("mat_doc_id"), form_id)
    form = await get_form(conn, form_id)
    # Notify the NPD team that their RM has been issued (§11). Post-commit.
    await notify.notify_event(
        conn, event="RM_FORM_ISSUED", message=f"RM Issue Form {form['form_number']} issued by the Store",
        subject=f"[RM Issue Form] {form['form_number']} issued",
        mail_body=_form_mail_body(form, "issued"),
        related_id=form_id, related_type="RM_ISSUE_FORM", entity=form["entity"])
    return form


async def cancel_form(conn, form_id: int, *, reason: str, user) -> dict:
    form = await _fetch(conn, form_id)
    if form["status"] in ("ISSUED", "CLOSED", "CANCELLED"):
        raise HTTPException(409, detail={"error": "wrong_status",
                                         "message": "Form can no longer be cancelled",
                                         "details": {"status": form["status"]}})
    await conn.execute(
        "UPDATE rm_issue_form SET status='CANCELLED', cancellation_reason=$1, updated_at=NOW() WHERE id=$2",
        reason, form_id)
    return await get_form(conn, form_id)
