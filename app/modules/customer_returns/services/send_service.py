"""Customer-Returns approval request: send it, record it, and send it again after a
save changes the figures.

send_for_approval is the Send (and Re-send): allowed only while the return is
undecided, it mails every approver who can decide (mail_service.notify_cr_created)
and sends them the WhatsApp card (wa_notify.notify_cr_head_approval), each carrying
the fingerprint of the figures it shows, and records the request in cr_approval_log
when it reached anyone. Sending stays best-effort — a channel that fails never fails
the request — but the result says what actually happened.

after_save is what a save does next: send when asked (Save & send), or on its own
when the save changed the figures of a return already sent, so the approvers get
the figures they now decide on; the earlier links and cards no longer decide it.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from app.modules.customer_returns.services import (
    approval_log, approval_service, mail_service, query_service, wa_notify,
)
from app.modules.customer_returns.tables import cr_table_names

logger = logging.getLogger(__name__)


def _send_status(mail: dict, wa: dict, has_decider: bool) -> str:
    """'sent' when every mail copy went out and WhatsApp did not fail (skipped — not
    configured, no number on file — is not a failure: the mail carries the same
    buttons); 'failed' when nobody who can decide was reached on either channel, or
    nobody can decide the CR at all; 'partial' in between. Only deciders count —
    the person who raised the return is never one (mail_service.eligible_approvers)."""
    if has_decider and mail.get("sent") and wa.get("status") in ("sent", "skipped"):
        return "sent"
    reached = has_decider and (mail.get("approvers_mailed", 0) > 0 or wa.get("sent", 0) > 0)
    return "partial" if reached else "failed"


def _public_wa(wa: dict) -> dict:
    """The WhatsApp outcome for the response: counts, reason and who was reached —
    no phone numbers or wamids (a wamid is what a forged tap would quote)."""
    out = {k: wa[k] for k in ("status", "sent", "failed", "reason") if k in wa}
    out["recipients"] = [{"name": r.get("name"), "sent": bool(r.get("sent"))}
                         for r in wa.get("recipients") or []]
    return out


async def send_for_approval(conn, company: str, cr_id: str, sent_by: str, *,
                            note: str | None = None) -> dict:
    """Send the approval request (module docstring). 409 already_decided once the
    return is decided. ``note`` heads the mail (why it is sent again).

      approvers [{name, email}] who can decide it, BH first — never its creator
      mail      {sent, to, cc_count, approvers_mailed, reason?, failed?}
                reason: smtp_not_configured | no_recipients | smtp_error | error
      whatsapp  {status: sent | partial | failed | skipped, sent, failed, reason?,
                 recipients: [{name, sent}]}
      status    sent | partial | failed (_send_status), with a reason when nobody
                can decide the CR: 'no_approver' (no BH resolves and no deputy is
                configured) or 'only_creator' (its only approver raised it).
    """
    detail = await query_service.get_cr(conn, company, cr_id)
    if not approval_service.is_undecided(detail.get("status")):
        label = approval_service.status_label(detail.get("status"))
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_decided",
                "message": f"{cr_id} is already “{label}”, so it can't be sent for approval again.",
                "details": {"rtv_id": cr_id, "status": detail.get("status")},
            },
        )
    detail["company"] = company
    rec = await mail_service.notify_cr_created(conn, detail, note=note)
    # WhatsApp head-approval to every approver who can decide (image header +
    # Approve/Reject/Hold buttons). Best-effort — never raises; skipped if
    # WABA/phone unconfigured.
    wa = await wa_notify.notify_cr_head_approval(conn, detail)
    deciders = rec["deciders"]
    out = {
        "status": _send_status(rec["mail"], wa, bool(deciders)),
        "approver": rec["approver"],
        "approvers": deciders,
        "to": rec["to"],
        "cc_count": len(rec["cc"]),
        "mail": rec["mail"],
        "whatsapp": _public_wa(wa),
    }
    if not deciders:
        out["reason"] = "only_creator" if rec["approvers"] else "no_approver"
    if out["status"] != "sent":
        logger.warning("[cr] send-for-approval %s cr=%s mail=%s whatsapp=%s",
                       out["status"], cr_id, rec["mail"], out["whatsapp"])
    if out["status"] != "failed":   # it reached someone who can decide it
        try:
            await approval_log.record(conn, company, cr_id, "sent", query_service.figures_version(detail),
                                      sent_by, {"status": out["status"], "mail": rec["mail"],
                                                "whatsapp": out["whatsapp"]})
        except Exception:  # noqa: BLE001 — the request went out; only its record failed
            logger.exception("[cr] approval request not recorded cr=%s", cr_id)
    return out


async def after_save(conn, company: str, cr_id: str, *, send: bool, figures_changed: bool,
                     saved_by: str) -> tuple[dict | None, str | None]:
    """Send the request after a save, when asked to or when the save changed the
    figures of a return already sent for approval. Returns (the send's result or
    None, why it was sent: 'requested' | 'figures_changed' | None). The save has
    committed, so a send refused by a decision that landed since is reported as
    {status: failed, reason: already_decided, message}, never raised."""
    if send:
        why = "requested"
    elif figures_changed and await _was_sent(conn, company, cr_id):
        why = "figures_changed"
    else:
        return None, None
    note = (f"{saved_by} changed this customer return, so it is sent again with its new figures. "
            "The buttons in earlier messages about it no longer work." if why == "figures_changed" else None)
    try:
        return await send_for_approval(conn, company, cr_id, saved_by, note=note), why
    except HTTPException as exc:
        return {"status": "failed", "reason": "already_decided",
                "message": approval_service.error_message(exc)}, why


async def _was_sent(conn, company: str, cr_id: str) -> bool:
    """Sent for approval before: a request recorded (or a WhatsApp card from before
    requests were recorded), or an approver has put it on hold."""
    status = await conn.fetchval(
        f"SELECT status FROM {cr_table_names(company)['header']} WHERE rtv_id = $1", cr_id)
    return (approval_service.status_label(status) == approval_service.ACTION_TO_STATUS["hold"]
            or await approval_log.was_sent(conn, company, cr_id))
