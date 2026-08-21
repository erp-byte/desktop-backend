"""FG Dispatch — notify billing / operations / stores when a plan line's
packaging stage is done, with the job-card details + operator-entered transport.

The "Dispatch to" action on the Plan List targets the packaging (output_kind='FG')
job card of a single article, for one of its batches (the "phase"). It assembles
a plain-text body (job card number, article, qty kg/units, no. of boxes,
warehouse, floor, phase/batch number, customer name + location, optional
transport), resolves the To/CC role recipients to active users' emails, sends via
the existing SMTP primitive (mail_service._send — best-effort, no-op if SMTP is
unset), and records the dispatch in fg_dispatch_log_v2.
"""
from __future__ import annotations

import asyncio
import logging

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.production.services import mail_service

logger = logging.getLogger(__name__)

# Recipient roles (auth_role.role_name). Missing roles are seeded by migration
# 065; 'stores' reuses the existing store_head role. A role with no active users
# simply contributes no email (graceful) until an admin assigns one.
DISPATCH_TO_ROLES = ('billing', 'candor_operations', 'store_head')
DISPATCH_CC_ROLES = ('business_head', 'operations_head', 'inventory_manager', 'production_manager')


async def _emails_for_roles(conn, roles) -> list[str]:
    """Active users' emails for the given role_names — across BOTH the primary
    role (auth_user.role_id) and secondary roles (auth_user_role), deduped."""
    if not roles:
        return []
    rows = await conn.fetch(
        """
        SELECT au.email FROM auth_user au
        JOIN auth_role r ON r.role_id = au.role_id
        WHERE r.role_name = ANY($1) AND au.is_active AND COALESCE(au.email, '') <> ''
        UNION
        SELECT au.email FROM auth_user au
        JOIN auth_user_role ur ON ur.user_id = au.user_id
        JOIN auth_role r ON r.role_id = ur.role_id
        WHERE r.role_name = ANY($1) AND au.is_active AND COALESCE(au.email, '') <> ''
        """,
        list(roles),
    )
    return [r["email"] for r in rows if r["email"]]


async def _packaging_jc(conn, plan_line_id: int):
    """The packaging (Final-FG) job card of a plan line — output_kind='FG',
    last in the chain. Returns None if the line has no FG job card yet."""
    return await conn.fetchrow(
        """
        SELECT j.job_card_id, j.job_card_number, j.plan_id, j.fg_sku_name,
               j.customer_name, j.factory, j.floor, j.entity, j.status, j.uom
        FROM job_card_v2 j
        WHERE j.plan_line_id = $1 AND j.output_kind = 'FG' AND j.deleted_at IS NULL
        ORDER BY j.step_number DESC
        LIMIT 1
        """,
        plan_line_id,
    )


async def _batches_for_jc(conn, job_card_id: int) -> list[dict]:
    """Batches of the packaging job card (the batch selector source). Actual FG
    qty per batch comes from job_card_output_v2 (output_kind='FG'), falling back
    to the batch's fg_actual_* / produced_qty_kg."""
    rows = await conn.fetch(
        """
        SELECT b.batch_id, b.batch_number, b.batch_date, b.status,
               b.produced_qty_kg, b.fg_actual_kg, b.fg_actual_units,
               COALESCE((SELECT SUM(o.output_qty_kg) FROM job_card_output_v2 o
                          WHERE o.job_card_id = $1 AND o.batch_id = b.batch_id
                            AND o.output_kind = 'FG'
                            AND o.deleted_at IS NULL), 0) AS out_kg,
               COALESCE((SELECT SUM(o.output_qty_units) FROM job_card_output_v2 o
                          WHERE o.job_card_id = $1 AND o.batch_id = b.batch_id
                            AND o.output_kind = 'FG'
                            AND o.deleted_at IS NULL), 0) AS out_units
        FROM job_card_batch_v2 b
        WHERE b.job_card_id = $1
        ORDER BY b.batch_number
        """,
        job_card_id,
    )
    out: list[dict] = []
    for b in rows:
        kg = float(b["out_kg"] or 0) or float(b["fg_actual_kg"] or 0) or float(b["produced_qty_kg"] or 0)
        units = float(b["out_units"] or 0) or float(b["fg_actual_units"] or 0)
        out.append({
            "batch_id": b["batch_id"],
            "batch_number": b["batch_number"],
            "batch_date": b["batch_date"].isoformat() if b["batch_date"] else None,
            "status": b["status"],
            "qty_kg": kg,
            "qty_units": units,
        })
    return out


async def get_line_dispatch_info(conn, plan_line_id: int) -> dict:
    """Prefill payload for the Dispatch modal: the packaging job card header +
    its batches (default selection = packaging stage, never another WIP process)
    + the resolved recipient role labels."""
    jc = await _packaging_jc(conn, plan_line_id)
    if not jc:
        return {"exists": False, "message": "No packaging job card for this article yet."}
    batches = await _batches_for_jc(conn, jc["job_card_id"])
    return {
        "exists": True,
        "plan_id": jc["plan_id"],
        "plan_line_id": plan_line_id,
        "job_card_id": jc["job_card_id"],
        "job_card_number": jc["job_card_number"],
        "fg_sku_name": jc["fg_sku_name"],
        "customer_name": jc["customer_name"],
        "warehouse": jc["factory"],
        "floor": jc["floor"],
        "uom": jc["uom"],
        "packaging_status": jc["status"],
        "packaging_completed": jc["status"] in ("completed", "closed"),
        "batches": batches,
        "to_roles": list(DISPATCH_TO_ROLES),
        "cc_roles": list(DISPATCH_CC_ROLES),
    }


def _fmt(v) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        # trim trailing .0 for whole numbers
        return f"{v:g}"
    return str(v)


def _compose_body(*, jc_number, article, qty_kg, qty_units, num_boxes, warehouse, floor,
                  batch_number, customer_name, customer_location,
                  vehicle_number, transporter, transport_location) -> str:
    lines = [
        "A finished-goods dispatch has been raised from the packaging stage.",
        "",
        "── Job card ──",
        f"Job card number : {_fmt(jc_number)}",
        f"Article         : {_fmt(article)}",
        f"Qty (kg)        : {_fmt(qty_kg)}",
        f"Qty (units)     : {_fmt(qty_units)}",
        f"No. of boxes    : {_fmt(num_boxes)}",
        f"Warehouse       : {_fmt(warehouse)}",
        f"Floor           : {_fmt(floor)}",
        f"Phase / batch   : {_fmt(batch_number)}",
        "",
        "── Customer ──",
        f"Name            : {_fmt(customer_name)}",
        f"Location        : {_fmt(customer_location)}",
    ]
    if vehicle_number or transporter or transport_location:
        lines += [
            "",
            "── Transport ──",
            f"Vehicle number  : {_fmt(vehicle_number)}",
            f"Transporter     : {_fmt(transporter)}",
            f"Location        : {_fmt(transport_location)}",
        ]
    return "\n".join(lines)


async def dispatch_fg(conn, plan_line_id: int, *, batch_id: int, num_boxes=None,
                      customer_location=None, vehicle_number=None,
                      transporter=None, transport_location=None,
                      dispatched_by: str | None = None) -> dict:
    """Send the dispatch email (To: billing/candor_operations/store_head,
    CC: business_head/operations_head/inventory_manager/production_manager) and
    record it. Best-effort email (never raises); always records the row."""
    jc = await _packaging_jc(conn, plan_line_id)
    if not jc:
        return {"error": "no_packaging_jc", "message": "No packaging job card for this article."}
    batches = await _batches_for_jc(conn, jc["job_card_id"])
    batch = next((b for b in batches if b["batch_id"] == batch_id), None)
    if batch is None:
        return {"error": "batch_not_found",
                "message": "Selected batch not found on the packaging job card."}

    subject = (f"FG Dispatch — {jc['fg_sku_name']} "
               f"(JC {jc['job_card_number']}, batch {batch['batch_number']})")
    body = _compose_body(
        jc_number=jc["job_card_number"], article=jc["fg_sku_name"],
        qty_kg=batch["qty_kg"], qty_units=batch["qty_units"], num_boxes=num_boxes,
        warehouse=jc["factory"], floor=jc["floor"], batch_number=batch["batch_number"],
        customer_name=jc["customer_name"], customer_location=customer_location,
        vehicle_number=vehicle_number, transporter=transporter,
        transport_location=transport_location,
    )
    to_emails = await _emails_for_roles(conn, DISPATCH_TO_ROLES)
    cc_emails = await _emails_for_roles(conn, DISPATCH_CC_ROLES)

    email_sent = False
    if to_emails or cc_emails:
        try:
            # _send is synchronous/blocking; run off the event loop. It is itself
            # best-effort (catches everything, no-op when SMTP_HOST is unset).
            await asyncio.to_thread(mail_service._send, subject, body, to_emails, cc_emails)
            email_sent = True
        except Exception:
            logger.exception("FG dispatch email failed for plan_line_id=%s", plan_line_id)

    async def _insert():
        return await conn.fetchval(
            """
            INSERT INTO fg_dispatch_log_v2 (
                dispatch_id, plan_id, plan_line_id, job_card_id, job_card_number,
                batch_id, batch_number, fg_sku_name, qty_kg, qty_units, num_boxes,
                warehouse, floor, customer_name, customer_location,
                vehicle_number, transporter, transport_location,
                to_emails, cc_emails, subject, body, email_sent, dispatched_by
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                $19,$20,$21,$22,$23,$24
            ) RETURNING dispatch_id
            """,
            new_short_time_id(), jc["plan_id"], plan_line_id, jc["job_card_id"],
            jc["job_card_number"], batch["batch_id"], batch["batch_number"], jc["fg_sku_name"],
            batch["qty_kg"], batch["qty_units"], num_boxes,
            jc["factory"], jc["floor"], jc["customer_name"], customer_location,
            vehicle_number, transporter, transport_location,
            to_emails, cc_emails, subject, body, email_sent, dispatched_by,
        )
    dispatch_id = await insert_with_pk_retry(conn, _insert)

    return {
        "dispatched": True,
        "dispatch_id": dispatch_id,
        "email_sent": email_sent,
        "to": to_emails,
        "cc": cc_emails,
        "subject": subject,
        "body": body,
    }
