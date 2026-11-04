"""Customer-Returns approval decision — who may decide, then an idempotent status
transition + audit.

Shared by every decision channel — the in-app decision endpoint, the emailed
magic link (GET /email-action only shows a confirmation page; its POST decides)
and the WhatsApp quick-reply tap — and each passes the deciding person's e-mail,
so one set of rules covers all three:

  * WHO. Only the CR's approvers may decide: its Business Head (the header's
    business_head name resolved to an e-mail exactly as the approval mail does —
    mail_service.resolve_approvers) plus the standing deputy approvers
    (cr_email_routing kind 'deputy_approver'). E-mails are compared trimmed and
    case-insensitively. Nobody else: no admin override, and the person who raised
    the CR (created_by) never decides it, even when they are its BH or a deputy —
    another approver must. Refusals are 403 not_an_approver / 403 own_return, and
    409 no_approver when the BH can't be resolved and no deputy is configured.
  * WHEN. Decisions apply only from an undecided state, Pending or On Hold
    (_NON_TERMINAL; a NULL or blank status is Pending). Approved, Submitted
    (approved, then the warehouse saved its boxes) and Rejected are decided: an
    attempt is reported as already-actioned and nothing is written — so a stale
    mail link or WhatsApp button can never re-open a decision. (The header PUT
    cannot set a status at all — CRHeaderUpdate has no such field.)

  * ON WHAT. A decision is taken on the figures the decider was shown (``seen``):
    each mail link and WhatsApp card carries the fingerprint of the figures it was
    sent with (query_service.figures_version), and the review page sends the one it
    loaded. Once the return's figures differ — it was edited after that — the
    decision is refused (409 figures_changed) instead of approving figures nobody
    saw; the latest request, or the page reloaded, decides it. A link or card sent
    before they carried a fingerprint decides only while no request was sent and no
    figure changed after it (approval_log).

Writes status + approved_by (the decider's e-mail) + approved_at (+ optional
remark). The UPDATE is guarded on the undecided states, read exactly as _status()
reads them, so two approvers acting at once is safe — the first wins, the second
is told it is already actioned. Approving a cold-store return also puts its printed
cartons in cold stock, in the same transaction (cold_stock_service).
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.customer_returns.services import approval_log, cold_stock_service, mail_service, query_service
from app.modules.customer_returns.tables import cr_table_names

ACTION_TO_STATUS = {"approve": "Approved", "reject": "Rejected", "hold": "On Hold"}
# The undecided states: the only ones a decision can move a CR out of.
_NON_TERMINAL = ["Pending", "On Hold"]
# What the server's own replies (the mail-link card, the WhatsApp text) call a
# status. 'Submitted' is the stored value for an approved CR whose boxes were saved.
_STATUS_LABEL = {"Submitted": "Approved (boxes saved)"}


def _status(status) -> str:
    """A stored status as the decision rules read it: spaces trimmed, NULL or blank
    being Pending. The guarded UPDATE reads it the same way in SQL —
    COALESCE(NULLIF(btrim(status), ''), 'Pending'), btrim dropping spaces only — so
    a CR this side calls undecided can never fail the UPDATE and be reported as
    already actioned."""
    return (str(status).strip(" ") if status is not None else "") or "Pending"


def status_label(status) -> str:
    """Display name of a CR status; a NULL or blank status reads as Pending, as on the read side."""
    s = _status(status)
    return _STATUS_LABEL.get(s, s)


def is_undecided(status) -> bool:
    """True while a decision can still be taken (Pending / On Hold; NULL or blank is Pending)."""
    return _status(status) in _NON_TERMINAL


def is_rejected(status) -> bool:
    """True once the return was rejected: nothing on it may change any more."""
    return _status(status) == ACTION_TO_STATUS["reject"]


def is_approved(status) -> bool:
    """True once the return was approved (Approved, or Submitted: its boxes saved since)."""
    return _status(status) in (ACTION_TO_STATUS["approve"], "Submitted")


def carton_refusal(cr_id, status, box_id) -> str | None:
    """Why a carton of this return can't be issued (Stores sending it on a request,
    the job card's raw-material scan), or None once the return is approved. Its
    boxes are printed before the decision, so a label alone proves nothing: a
    rejected return's cartons go back, an undecided one's wait for the decision."""
    if is_approved(status):
        return None
    if is_rejected(status):
        return f"Box {box_id} is on customer return {cr_id}, which was rejected, so it can't be issued."
    return (f"Box {box_id} is on customer return {cr_id}, which is {status_label(status)} and not "
            "approved yet, so it can't be issued until it is approved.")


def _norm(email) -> str:
    return str(email or "").strip().lower()


def _names(people: list[dict]) -> str:
    names = [p.get("name") or p.get("email") or "" for p in people]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def _refuse(status_code: int, error: str, message: str, **details) -> HTTPException:
    return HTTPException(status_code, detail={"error": error, "message": message, "details": details})


def error_message(exc: HTTPException) -> str:
    """The readable text of a refusal raised here (or any HTTPException)."""
    d = exc.detail
    if isinstance(d, dict):
        return str(d.get("message") or d.get("error") or "Not allowed")
    return str(d or "Not allowed")


async def _load(conn, company: str, cr_id: str) -> dict:
    header = cr_table_names(company)["header"]  # whitelisted (CFPL/CDPL) — safe to interpolate
    row = await conn.fetchrow(
        f"SELECT status, business_head, created_by, customer, factory_unit, invoice_number "
        f"FROM {header} WHERE rtv_id = $1", cr_id)
    if not row:
        raise _refuse(404, "customer_return_not_found", f"No customer return {cr_id}", rtv_id=cr_id)
    return dict(row)


async def _authorise(conn, company: str, cr_id: str, actor_email) -> tuple[dict, list[dict]]:
    """The header + its approvers, once ``actor_email`` is shown to be allowed to
    decide this CR; else the refusal (see the module docstring)."""
    row = await _load(conn, company, cr_id)
    routing = await mail_service.fetch_routing(conn)
    approvers = mail_service.resolve_approvers(routing, row.get("business_head"))
    details = {"rtv_id": cr_id, "approvers": approvers}
    if not approvers:
        bh = str(row.get("business_head") or "").strip()
        why = f"its Business Head “{bh}” has no e-mail on file" if bh else "it has no Business Head"
        raise _refuse(409, "no_approver",
                      f"Nobody can decide customer return {cr_id}: {why} and no deputy approver "
                      f"is configured.", **details)
    me = _norm(actor_email)
    if not me:
        raise _refuse(403, "not_an_approver",
                      f"Your account has no e-mail address, so it can't be matched to an approver. "
                      f"Only {_names(approvers)} can decide customer return {cr_id}.", **details)
    if me == _norm(row.get("created_by")):
        others = mail_service.eligible_approvers(approvers, row.get("created_by"))
        who = (f" {_names(others)} must decide it." if others
               else " There is no other approver for it.")
        raise _refuse(403, "own_return",
                      f"You raised customer return {cr_id}, so you can't decide it yourself.{who}",
                      **details)
    if me not in {_norm(a["email"]) for a in approvers}:
        raise _refuse(403, "not_an_approver",
                      f"Only {_names(approvers)} can approve, reject or hold customer return {cr_id}.",
                      **details)
    return row, approvers


# What a decider was shown, as each channel says it (``seen``):
#   {"figures": fp}       the fingerprint a link, card or page carries;
#   {"sent_at": t}        a link or card from before they carried one: when it was sent;
#   + "source": "page"    the review page (the refusal then says to reload it).
# None: nothing to check (an older review page sends no fingerprint, and shows the
# return as it is now anyway).
_STALE = {
    "page": "{cr} was changed after you opened it. Reload it to see its figures, then decide.",
    "request": "{cr} was changed after this request was sent to you, so it doesn't show the "
               "return's figures any more. Decide from its latest approval request, or open it in the ERP.",
}


async def _assert_seen(conn, company: str, cr_id: str, seen: dict | None, *, for_update: bool) -> None:
    """409 figures_changed when the decider was shown other figures than the return's
    (module docstring, ON WHAT)."""
    if not seen:
        return
    tables = cr_table_names(company)
    if seen.get("figures"):
        current = await query_service.read_figures(conn, tables, cr_id, for_update=for_update)
        stale = current is not None and current != seen["figures"]
    else:
        if for_update:   # the same row lock the fingerprint read takes
            await conn.fetchrow(f"SELECT id FROM {tables['header']} WHERE rtv_id = $1 FOR UPDATE", cr_id)
        stale = await approval_log.recorded_since(conn, company, cr_id, seen.get("sent_at"))
    if stale:
        message = _STALE["page" if seen.get("source") == "page" else "request"].format(cr=cr_id)
        raise _refuse(409, "figures_changed", message, rtv_id=cr_id)


async def check_cr_action(conn, company: str, cr_id: str, actor_email, *, seen: dict | None = None) -> dict:
    """Read-only: the checks apply_cr_action makes, without deciding anything.
    Raises the same refusals; returns the header fields, its approvers, whether it
    is already decided and, while it is not, its lines (the email-action
    confirmation page shows them)."""
    row, approvers = await _authorise(conn, company, cr_id, actor_email)
    status = _status(row.get("status"))
    lines = []
    if is_undecided(status):
        await _assert_seen(conn, company, cr_id, seen, for_update=False)
        lines = await query_service.fetch_lines(conn, company, cr_id)
    return {**row, "rtv_id": cr_id, "status": status, "approvers": approvers, "lines": lines,
            "decided": not is_undecided(status)}


async def apply_cr_action(conn, company: str, cr_id: str, action: str, actor_email,
                          remark: str | None = None, *, seen: dict | None = None) -> dict:
    """Decide the CR as ``actor_email``, on the figures ``seen`` (see _STALE). Every
    channel calls this with the e-mail of the person deciding; the who / when / on
    what rules above are enforced here, once."""
    if action not in ACTION_TO_STATUS:
        raise _refuse(400, "invalid_action", "action must be approve, reject or hold", action=action)
    new_status = ACTION_TO_STATUS[action]
    header = cr_table_names(company)["header"]  # whitelisted (CFPL/CDPL) — safe to interpolate

    row, _ = await _authorise(conn, company, cr_id, actor_email)
    status = _status(row.get("status"))
    if not is_undecided(status):
        # Already decided (Approved / Submitted / Rejected) — idempotent, don't re-apply.
        return {"rtv_id": cr_id, "status": status, "already_actioned": True}

    actor = str(actor_email).strip()
    remark = (remark or "").strip() or None
    # Approving a cold return also puts its printed cartons in cold stock, in the same
    # transaction. Its box lock is taken before the status UPDATE, in the order a box
    # write takes them (lock, then the header row), so the two can't deadlock.
    cold = new_status == ACTION_TO_STATUS["approve"] and cold_stock_service.cold_store(row.get("factory_unit"))
    async with conn.transaction():
        if cold:
            await query_service.lock_box_writes(conn, company, cr_id)
        # The figures are read with the header row held, so an edit waits for the
        # decision (and then finds it decided) or lands first and is refused here.
        await _assert_seen(conn, company, cr_id, seen, for_update=True)
        # The guard reads the stored status as _status() does (trimmed; NULL or blank is
        # Pending): COALESCE alone would leave a blank '' as '', match no row, and report
        # an undecided return as already actioned forever.
        updated = await conn.fetchrow(
            f"""
            UPDATE {header}
               SET status = $2, approved_by = $3, approved_at = NOW(),
                   approval_remark = COALESCE($4, approval_remark), updated_at = NOW()
             WHERE rtv_id = $1 AND COALESCE(NULLIF(btrim(status), ''), 'Pending') = ANY($5::text[])
            RETURNING rtv_id, status
            """,
            cr_id, new_status, actor, remark, _NON_TERMINAL,
        )
        if updated and cold:
            await cold_stock_service.sync(conn, company, cr_id)
    if not updated:
        # Lost a race between the SELECT and the guarded UPDATE.
        cur = await conn.fetchrow(f"SELECT status FROM {header} WHERE rtv_id = $1", cr_id)
        return {"rtv_id": cr_id, "status": (_status(cur["status"]) if cur else new_status),
                "already_actioned": True}
    return {"rtv_id": cr_id, "status": updated["status"], "already_actioned": False, "decided_by": actor}
