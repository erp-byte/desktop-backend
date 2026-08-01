"""Threaded NPD mail — ONE trail per transaction (ports customer_returns/mail_service).

Every mail about a single NPD transaction lands in one conversation:
  * deterministic thread id ``<NPD-{request_id}@candorfoods.in>`` — pure function of the
    requisition's request_id, no DB column. Set as Message-ID on the "created" mail, as
    In-Reply-To + References on every later mail (review, accept, hold, hold re-offer,
    reminder, promote request, promote decision, dispatch).
  * ONE constant subject for the whole trail. Gmail breaks a conversation the moment the
    subject changes — even with a correct In-Reply-To/References chain — so the status is
    conveyed in the body, never in the subject.
  * a dev job card raised from a requisition is the SAME transaction: its promote and
    dispatch mails reply into that requisition's trail. A standalone card (no
    source_requisition_id) is its own transaction and roots its own thread — a different
    transaction never shares a trail.
  * ONE recipient set per transaction (resolve_recipients), so everyone on the trail sees
    every step — not just the events addressed to their own role.
  * action buttons go to the GATE HOLDER ALONE: a buttoned copy addressed to that one
    person with no Cc, plus a button-less copy of the same card to everyone else. A signed
    action URL never appears in a mail with more than one recipient.

Best-effort throughout: a missing SMTP config or a send failure is logged and swallowed so
it can never fail the API request that triggered it. Recipients resolve from auth_user
(email + role), NOT the broken users join.
"""
from __future__ import annotations
import html as _html
import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import quote

from app.config import Settings
from app.core.mail_identity import Module, SubjectPolicy, stamp

logger = logging.getLogger(__name__)

REMINDER_MIN_HOURS = 24
REMINDER_MAX = 5

_PROMOTE_GATE_LABEL = {
    "INV_MGR": "Inventory manager",
    "REQUESTOR_BH": "Business head",
}


# ── recipient resolution ─────────────────────────────────────────────────────
async def _emails_for_role(conn, role: str) -> list[str]:
    rows = await conn.fetch(
        """SELECT a.email FROM auth_user a JOIN auth_role r ON a.role_id = r.role_id
            WHERE r.role_name = $1 AND COALESCE(a.is_active, TRUE)
              AND a.email IS NOT NULL AND btrim(a.email) <> ''""", role)
    return [r["email"] for r in rows]


async def _email_for_user(conn, uid) -> str | None:
    if not uid:
        return None
    return await conn.fetchval("SELECT email FROM auth_user WHERE user_id = $1", uid)


async def _name_for_user(conn, uid, fallback=None) -> str | None:
    """The actor's real full_name from the DB, preferred over whatever the calling channel
    supplied — the WhatsApp path builds a stub user whose full_name is the literal
    "WhatsApp", which would otherwise read as "approved by WhatsApp" in the trail."""
    if uid:
        try:
            name = await conn.fetchval("SELECT full_name FROM auth_user WHERE user_id = $1", uid)
            if name and str(name).strip():
                return str(name).strip()
        except Exception:  # noqa: BLE001 — a name lookup must never break a best-effort mail
            logger.exception("[sample-mail] full_name lookup failed for user %s", uid)
    return fallback


def _dedupe(addrs) -> list[str]:
    """Order-preserving, case-insensitive dedupe; drops blanks/None."""
    seen: set[str] = set()
    out: list[str] = []
    for a in addrs or []:
        n = (a or "").strip()
        if not n or n.lower() in seen:
            continue
        seen.add(n.lower())
        out.append(n)
    return out


async def resolve_recipients(conn, req: dict | None) -> dict:
    """{to, cc, npd, inventory, production, requestor, sales_poc} — ONE recipient set for
    the whole transaction.

    Port of customer_returns.resolve_recipients, resolved from roles + the requisition row
    (the sample module has no cr_email_routing equivalent, and this introduces none). TO
    leads with the requestor — the person the trail is about. CC is scoped to who is
    actually involved in THIS sample type: inventory + the sales POC always, npd_team only
    for NPD/TRIAL, production only for the types that raise a job card. Falls back to the
    npd_team pool when no requestor resolves, so a mail is never addressed to nobody.
    """
    stype = ((req or {}).get("sample_type") or "").upper()
    # A dev job card with no source requisition (req=None) is an NPD context by definition.
    is_npd = not req or stype in ("NPD", "TRIAL")
    produces = stype in ("BASIS_FG", "NPD", "TRIAL")   # types that run a job card

    npd = _dedupe(await _emails_for_role(conn, "npd_team"))
    inv = _dedupe(await _emails_for_role(conn, "inventory_manager"))
    # Production only joins the trail for types that actually raise a job card — a
    # BASIS_RM issue never reaches the floor, and Cc'ing them on it is just noise.
    prod = _dedupe(await _emails_for_role(conn, "floor_manager")) if produces else []
    requestor = await _email_for_user(conn, (req or {}).get("requestor_user_id"))
    # Sales POC — the stored address wins (it is the snapshot taken when the POC was
    # chosen, and may belong to someone with no login); fall back to the named user's
    # current address. Cc only: they follow the trail but approve nothing.
    poc = (req or {}).get("sales_poc_email") or None
    if not poc:
        poc = await _email_for_user(conn, (req or {}).get("sales_poc_user_id"))
    to = _dedupe([requestor]) or list(npd)
    low = {a.lower() for a in to}
    pool = (npd if is_npd else []) + inv + prod + ([poc] if poc else [])
    cc = [a for a in _dedupe(pool) if a.lower() not in low]
    return {"to": to, "cc": cc, "npd": npd, "inventory": inv, "production": prod,
            "requestor": requestor, "sales_poc": poc}


# ── threading ────────────────────────────────────────────────────────────────
def _thread_key(request_id) -> str:
    """The transaction's thread anchor — pure function of the requisition's request_id,
    so every mail resolves to the same root with no stored state (CR's rule)."""
    return f"<NPD-{request_id}@candorfoods.in>"


def _jc_thread_key(dev_jc_id) -> str:
    """Anchor for a STANDALONE dev job card — one with no source requisition, hence its
    own transaction and its own trail."""
    return f"<NPD-JC-{dev_jc_id}@candorfoods.in>"


def _thread_subject(request_id, sample_type=None) -> str:
    """The ONE constant subject for the whole trail. Gmail breaks a conversation when the
    subject changes — even with a correct In-Reply-To/References chain — so every mail in
    the transaction MUST share this exact string; the action/status goes in the body.

    sample_type is fixed for the life of a requisition (it is not editable), so deriving
    the subject from it is stable across the trail. NPD/TRIAL keeps the original wording
    byte-for-byte so trails already running in people's mailboxes don't split; the other
    types drop the "NPD" prefix, which was simply wrong on a BASIS_RM / INTERNAL request."""
    if (sample_type or "").upper() in ("", "NPD", "TRIAL"):
        return f"NPD Sample Request {request_id}"
    return f"Sample Request {request_id}"


def _jc_thread_subject(dev_jc_id) -> str:
    return f"NPD Dev Job Card {dev_jc_id}"


def _with_sales_poc(jc: dict, src: dict | None) -> dict:
    """Copy the source requisition's sales POC onto a dev-JC dict for rendering. The job
    card has no POC columns of its own, so without this the promote / dispatch cards would
    show an em-dash while the requisition cards in the SAME thread show the name."""
    return {**jc,
            "sales_poc_name": (src or {}).get("sales_poc_name"),
            "sales_poc_email": (src or {}).get("sales_poc_email")}


async def _jc_thread(conn, dev_jc_id) -> tuple[str, str, dict | None]:
    """(thread key, subject, source requisition) for a dev job card.

    A card raised from a requisition is the SAME transaction as that requisition, so its
    promote / dispatch mails reply into the requisition's trail under the identical
    subject. A standalone card roots its own thread. The JOIN yields no row when
    source_requisition_id is NULL (or dangles), which is exactly the standalone case."""
    src = await conn.fetchrow(
        """SELECT r.* FROM npd_dev_job_cards j
             JOIN sample_requisitions r ON r.id = j.source_requisition_id
            WHERE j.id = $1""", dev_jc_id)
    if src is not None:
        rid = src["request_id"]
        return _thread_key(rid), _thread_subject(rid, src["sample_type"]), dict(src)
    return _jc_thread_key(dev_jc_id), _jc_thread_subject(dev_jc_id), None


# ── transport ────────────────────────────────────────────────────────────────
def _module_for(sample_type) -> Module:
    """NPD/TRIAL mail is labelled "Candor · NPD". The general sample-issuing flow wants its
    own identity so a raw-material sample never arrives calling itself NPD — but
    Module.SAMPLES is owned by app/core/mail_identity.py and may not be defined there yet,
    so this degrades to NPD rather than raising into a best-effort send. Both use the
    ANCHOR policy, so the glyph stays constant per module and threading is unaffected."""
    if (sample_type or "").upper() in ("", "NPD", "TRIAL"):
        return Module.NPD
    return getattr(Module, "SAMPLES", Module.NPD)


def _send(subject, html, to_addrs, *, cc=None, msgid=None, in_reply_to=None,
          entity_type="SampleRequest", entity_id=None, event=None,
          status=None, actor=None, module: Module = Module.NPD):
    """Send one HTML email on a background thread. Returns the Message-ID used, or None
    when SMTP is unconfigured / there are no recipients. Never raises.

    The entity/event arguments only feed the X-Candor-* headers and the sender
    name — they never reach the subject, which stays the thread anchor."""
    s = Settings()
    host = (s.SMTP_HOST or "").strip()
    sender = (s.SMTP_EMAIL or "").strip()
    pw = (s.SMTP_APP_PASSWORD or "").strip()
    to_addrs = _dedupe(to_addrs)
    cc = [a for a in _dedupe(cc) if a.lower() not in {t.lower() for t in to_addrs}]
    if not host or not to_addrs:
        logger.info("[sample-mail] skip (host=%s to=%s) subject=%s", bool(host), to_addrs, subject)
        return None
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    mid = msgid or make_msgid(domain="candorfoods.in")
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content("This message requires an HTML-capable mail client.")
    msg.add_alternative(html, subtype="html")

    # ANCHOR: the whole point of _thread_subject is that this string never varies
    # across a trail, so identity may only prefix a CONSTANT module glyph. The
    # action stays in the body banner, exactly as before.
    stamp(msg, module=module, policy=SubjectPolicy.ANCHOR,
          entity_type=entity_type, entity_id=entity_id, event=event,
          status=status, actor=actor, sender=sender)

    rcpts = list(dict.fromkeys([*to_addrs, *cc]))

    def _go():
        try:
            with smtplib.SMTP(host, s.SMTP_PORT, timeout=15) as srv:
                srv.starttls(context=ssl.create_default_context())
                if sender:
                    srv.login(sender, pw)
                srv.send_message(msg, from_addr=sender, to_addrs=rcpts)
            logger.info("[sample-mail] sent subject=%r to=%s cc=%s", subject, to_addrs, cc)
        except Exception:  # noqa: BLE001 — best-effort; transport failure must not break the request
            logger.exception("[sample-mail] send failed subject=%r", subject)

    threading.Thread(target=_go, daemon=True).start()
    return mid


def _broadcast(subject, html, rec: dict, *, thread: str, exclude=(), root: bool = False,
               module: Module = Module.NPD, **send_kw) -> None:
    """Send ONE button-less copy to the transaction's recipient set, minus the gate holders
    who already received their own buttoned copy — CR's others_to / others_cc split.

    root=True anchors the trail (Message-ID = the thread key); otherwise the mail replies
    into it with a fresh Message-ID. Promoting Cc to To keeps a mail deliverable when every
    To address was a gate holder."""
    skip = {(a or "").strip().lower() for a in exclude}
    to = [a for a in rec["to"] if a.lower() not in skip]
    cc = [a for a in rec["cc"] if a.lower() not in skip]
    if not to:
        to, cc = cc, []
    if not to:
        return
    _send(subject, html, to, cc=cc,
          msgid=thread if root else None,
          in_reply_to=None if root else thread,
          module=module, **send_kw)


# ── link builders ────────────────────────────────────────────────────────────
# Accept hits the backend (GET confirm page → POST) carrying the reviewer's email for
# the recipient-match auth. Hold links STRAIGHT to the sample page on the web app (no
# backend hop) — the reviewer records the hold there, same as the in-app flow.
def _accept_url(request_id, email: str) -> str:
    from app.modules.sample.services.email_link_token import sign
    base = Settings().PUBLIC_BACKEND_URL.rstrip("/")
    t = sign("npd", request_id, email)
    return (f"{base}/api/v1/sample/email/npd-action?request_id={request_id}"
            f"&status=accept&email={quote(email)}&t={t}")


def _hold_url(pk_id) -> str:
    web = Settings().WEB_APP_URL.rstrip("/")
    return f"{web}/modules/sample/{pk_id}"


def _fmt(v, dash: str = "—") -> str:
    """HTML-escaped single-line value; em-dash when blank. All values rendered into
    the email pass through here (free-text fields are user-controlled)."""
    s = "" if v is None else str(v).strip()
    return _html.escape(s) if s else dash


# ── design tokens ────────────────────────────────────────────────────────────
# Email kills the usual levers: no web fonts (Outlook ignores them), no flexbox, no
# reliable <style> block. So hierarchy here is carried by SIZE, WEIGHT and ALIGNMENT
# only, and every value lives in one place so the scale stays consistent across cards.
_FONT   = "-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
_T_ID      = 28   # the request id — the one thing to recognise at a glance
_T_EYEBROW = 13
_T_KEY     = 23   # key-figure band
_T_LEAD    = 16   # intro sentence
_T_VALUE   = 15   # table value
_T_LABEL   = 14   # table label
_T_PILL    = 12
_T_FOOT    = 12
_INK    = "#0f172a"   # near-black, not pure — survives dark-mode inversion better
_MUTED  = "#64748b"
_FAINT  = "#94a3b8"
_RULE   = "#e8ecf1"
_PANEL  = "#f7f9fb"


def _kv_rows(fields) -> str:
    """Label/value rows for every detail card.

    Each field is (label, value) or (label, value, kind). `kind` sizes the row to its
    CONTENT rather than forcing one shape on everything:
        "num"    right-aligned, tabular figures — quantities and money line up
        "strong" larger + bolder, for the row that matters most on this card
        "muted"  de-emphasised, for values that are usually blank
    Values must already be _fmt()-escaped by the caller."""
    out = []
    for f in fields:
        label, value = f[0], f[1]
        kind = f[2] if len(f) > 2 else "text"
        align = "right" if kind == "num" else "left"
        size = 17 if kind == "strong" else _T_VALUE
        weight = 700 if kind in ("strong", "num") else 600
        colour = _FAINT if kind == "muted" else _INK
        # tabular-nums keeps 1,250.00 and 950.00 aligned on the decimal; harmless
        # where the client ignores it.
        numeric = "font-variant-numeric:tabular-nums;" if kind == "num" else ""
        out.append(
            '<tr>'
            f'<td style="padding:10px 16px 10px 0;color:{_MUTED};font-size:{_T_LABEL}px;'
            f'line-height:1.45;width:40%;vertical-align:top;border-bottom:1px solid {_RULE}">{label}</td>'
            f'<td align="{align}" style="padding:10px 0;color:{colour};font-size:{size}px;'
            f'font-weight:{weight};line-height:1.45;{numeric}word-break:break-word;'
            f'border-bottom:1px solid {_RULE}">{value}</td>'
            '</tr>')
    return "".join(out)


def _key_figures(items) -> str:
    """The band of 1-3 numbers a reader should absorb in one second, before any prose.

    This is the one loud element on the card; everything around it stays quiet so it
    keeps its force. Laid out as equal-width table cells because email has no flexbox.
    `items` is [(label, value)] — already escaped."""
    items = [(l, v) for l, v in (items or []) if v not in (None, "", "—")]
    if not items:
        return ""
    w = 100 // len(items)
    cells = "".join(
        f'<td width="{w}%" style="padding:2px 10px 2px 0;vertical-align:top">'
        f'<div style="font-size:11px;color:{_MUTED};text-transform:uppercase;'
        f'letter-spacing:.08em;line-height:1.3;margin-bottom:4px">{l}</div>'
        f'<div style="font-size:{_T_KEY}px;font-weight:700;color:{_INK};line-height:1.2;'
        f'font-variant-numeric:tabular-nums;word-break:break-word">{v}</div></td>'
        for l, v in items)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{_PANEL};border:1px solid {_RULE};border-radius:8px;'
            f'padding:14px 16px;margin-bottom:4px"><tr>{cells}</tr></table>'
            '<div style="height:18px"></div>')


def _callout(text, tone: str = "#64748b") -> str:
    """A reason / remark, given its own tinted block with a coloured spine. It used to be
    a small grey line that read as an afterthought — but on a rejection or a hold it is
    the single most important sentence in the mail."""
    if not (text and str(text).strip()):
        return ""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:14px 0 2px"><tr>'
            f'<td style="width:4px;background:{tone};border-radius:2px"></td>'
            f'<td style="padding:10px 14px;background:{_PANEL};border-radius:0 6px 6px 0">'
            f'<div style="font-size:11px;color:{_MUTED};text-transform:uppercase;'
            f'letter-spacing:.08em;margin-bottom:3px">Reason</div>'
            f'<div style="font-size:{_T_VALUE}px;color:{_INK};line-height:1.5">'
            f'{_fmt(text)}</div></td></tr></table>')


def _desc_block(text) -> str:
    """The optional 'Description' panel row appended under a detail grid. Empty when blank."""
    if not (text and str(text).strip()):
        return ""
    return (
        '<tr><td colspan="2" style="padding-top:16px">'
        f'<div style="font-size:11px;color:{_FAINT};text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Description</div>'
        f'<div style="font-size:{_T_VALUE}px;color:{_INK};line-height:1.6;background:{_PANEL};border:1px solid {_RULE};'
        f'border-radius:6px;padding:12px 14px;word-break:break-word">{_fmt(text)}</div>'
        '</td></tr>')


def _shell(*, hdr: str, eyebrow: str, title: str, inner: str, footer: str) -> str:
    """The one email-client-safe card chrome every mail in the trail renders into
    (table-based + inline styles for Gmail/Outlook safety). Keeping it in a single place
    is what stops the review / notice / promote / dispatch cards from drifting apart."""
    return f"""<!doctype html><html><head><meta name="color-scheme" content="light">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#eef1f5">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5;padding:28px 12px">
 <tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border:1px solid {_RULE};border-radius:12px;overflow:hidden;font-family:{_FONT}">
   <tr><td style="background:{hdr};padding:22px 26px">
     <div style="color:#ffffff;font-size:{_T_EYEBROW}px;font-weight:600;opacity:.9;letter-spacing:.1em;text-transform:uppercase">{eyebrow}</div>
     <div style="color:#ffffff;font-size:{_T_ID}px;font-weight:700;line-height:1.15;margin-top:6px;word-break:break-word">{title}</div>
   </td></tr>
   {inner}
   <tr><td style="padding:16px 26px;background:{_PANEL};border-top:1px solid {_RULE}">
     <p style="margin:0;font-size:{_T_FOOT}px;color:{_MUTED};line-height:1.55">{footer}</p>
   </td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""


def _pill(label: str, bg: str) -> str:
    return ('<div style="margin:0 0 16px">'
            f'<span style="display:inline-block;background:{bg};color:#ffffff;font-size:{_T_PILL}px;'
            'font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:6px 14px;'
            f'border-radius:999px">{label}</span></div>')


def _buttons(pairs) -> str:
    """The action-button row. `pairs` is [(label, url, colour)] — empty renders nothing,
    which is exactly how the button-less broadcast copy is produced from the same card."""
    if not pairs:
        return ""
    cells = "".join(
        f'<td style="padding-right:12px"><a href="{url}" style="display:inline-block;'
        f'background:{colour};color:#ffffff;font-size:16px;font-weight:600;text-decoration:none;'
        f'padding:14px 34px;border-radius:8px;line-height:1.2">{label}</a></td>'
        for label, url, colour in pairs)
    return ('<tr><td style="padding:20px 26px 28px">'
            f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>'
            '</td></tr>')


# ── requisition detail card ──────────────────────────────────────────────────
def _detail_table(req: dict) -> str:
    """The request-detail field grid + optional description block — shared verbatim by
    every requisition mail in the trail so the cards never drift."""
    exp = req.get("expected_dispatch_date")
    exp = str(exp)[:10] if exp else "TBC"
    wpp, qty = req.get("weight_per_piece"), req.get("quantity")
    fields = [
        ("Company", _fmt(req.get("company_name"))),
        ("Customer", _fmt(req.get("customer_name"))),
        ("Customer contact", _fmt(req.get("customer_contact"))),
        ("Target NPD article", _fmt(req.get("npd_target_name"))),
        ("Pcs", _fmt(req.get("pcs")), "num"),
        ("Weight per piece", f"{_fmt(wpp)} kg" if wpp is not None else "—", "num"),
        ("Quantity", f"{_fmt(qty)} kg" if qty is not None else "—", "num"),
        ("Warehouse", _fmt(req.get("warehouse"))),
        ("Purpose", _fmt(req.get("purpose_tag") or req.get("purpose_note"))),
        ("Mode of transport", _fmt(req.get("mode_of_transport"))),
        ("Expected dispatch", _fmt(exp)),
        ("Business head", _fmt(req.get("requestor_team"))),
        ("Sales POC", _fmt(req.get("sales_poc_name") or req.get("sales_poc_email"))),
        ("Return type", "Returnable" if req.get("returnable")
                        else "Non-returnable" if req.get("non_returnable") else "—"),
        ("Paid", "Yes" if req.get("paid") else "No"),
        ("Amount", f"{float(req.get('amount')):,.2f}"
                   if (req.get("paid") and req.get("amount") is not None) else "—", "num"),
    ]
    # The three things a reader actually looks for on a sample request, pulled out of the
    # grid and set large. Everything below stays quiet so these keep their weight.
    keys = [("Quantity", f"{_fmt(qty)} kg" if qty is not None else ""),
            ("Expected dispatch", _fmt(exp)),
            ("Warehouse", _fmt(req.get("warehouse")))]
    return (_key_figures(keys) +
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'{_kv_rows(fields)}{_desc_block(req.get("description"))}</table>')


_TRAIL_FOOTER = ("This is one message in the mail trail for this sample request — every "
                 "update on it lands in this same conversation.")
_ACTION_FOOTER = ("Sent to you as the approver on this step — the action buttons are in "
                  "your copy only. Everyone else on the trail receives it without them.")


def _review_html(req: dict, reviewer_email: str | None) -> str:
    """The NPD review card. With `reviewer_email` it carries Accept / Hold buttons whose
    Accept link embeds THAT reviewer's email for the recipient-match auth — so this copy
    goes to them alone. With None it is the identical card, buttons stripped, for the
    button-less broadcast to the rest of the trail."""
    rid = req.get("request_id")
    type_label = "Customer trial" if req.get("sample_type") == "TRIAL" else "NPD"
    if reviewer_email:
        intro = (f"A new {type_label} sample request needs your review. Tap <b>Accept</b> to "
                 "approve, or <b>Hold</b> to open it on the portal and record a reason.")
        pairs = [("&#10003;&nbsp; Accept", _accept_url(rid, reviewer_email), "#16a34a"),
                 ("&#9208;&nbsp; Hold", _hold_url(req.get("id")), "#f59e0b")]
        footer = _ACTION_FOOTER
    else:
        intro = (f"A {type_label} sample request has been raised and is with the NPD team for "
                 "review. Sharing the details for your visibility — no action is required here.")
        pairs = []
        footer = _TRAIL_FOOTER
    inner = (f'<tr><td style="padding:26px 26px 6px">'
             f'<p style="margin:0 0 18px;font-size:{_T_LEAD}px;color:{_INK};line-height:1.6">{intro}</p>'
             f'{_detail_table(req)}</td></tr>{_buttons(pairs)}')
    return _shell(hdr="#ec7211", eyebrow=f"NEW {type_label.upper()} SAMPLE REQUEST",
                  title=_fmt(rid), inner=inner, footer=footer)


# event -> (header colour, eyebrow, intro line, status-pill label, pill colour).
# "created" is the neutral logged notice that ROOTS the trail; the others mirror the
# NPD outcome as replies into it.
# event -> (header colour, eyebrow, intro, status-pill label, pill colour).
# One entry per state the requisition can reach, so the WHOLE lifecycle lands in the trail
# instead of only the NPD steps. Unknown events fall back to a neutral card, so adding a
# state in the service layer can never crash a send.
_REQ_EVENT = {
    "created":   ("#ec7211", "SAMPLE REQUEST LOGGED",     "A new sample request has been logged in the ERP. This notice opens the mail trail for it — every later update replies into this same conversation.", "Logged",   "#ec7211"),
    "submitted": ("#0ea5e9", "SUBMITTED FOR APPROVAL",    "This sample request has been submitted and is awaiting business-head approval.", "Submitted", "#0284c7"),
    "approved":  ("#16a34a", "APPROVED BY BUSINESS HEAD", "The business head has approved this sample request. It now moves to issuing / production.", "Approved", "#16a34a"),
    "rejected":  ("#dc2626", "REJECTED BY BUSINESS HEAD", "The business head has rejected this sample request.", "Rejected", "#dc2626"),
    "accepted":  ("#16a34a", "SAMPLE REQUEST ACCEPTED",   "The NPD team has accepted this sample request.", "Accepted", "#16a34a"),
    "on hold":   ("#f59e0b", "SAMPLE REQUEST ON HOLD",    "The NPD team has placed this sample request on hold.", "On hold", "#b45309"),
    "in production": ("#7c3aed", "SENT TO PRODUCTION",    "A production order and job cards have been raised to make this sample. Production — this requisition is the reason for the run.", "In production", "#7c3aed"),
    "packing":   ("#7c3aed", "PRODUCTION COMPLETE",       "Production of this sample is complete and it has moved to packing.", "Packing", "#7c3aed"),
    "ready":     ("#0f766e", "READY FOR DISPATCH",        "This sample is packed and ready for dispatch. Inventory can now verify it and issue the gate pass.", "Ready", "#0f766e"),
    "issued":    ("#0f766e", "MATERIAL ISSUED",           "The sample material has been issued from stock (movement 265) and is ready for dispatch.", "Issued", "#0f766e"),
    "verified":  ("#0284c7", "VERIFIED BY INVENTORY",     "The inventory manager has verified this sample against the request. Gate-pass issuance is next.", "Verified", "#0284c7"),
    "gate pass issued": ("#0f766e", "GATE PASS ISSUED",   "The gate pass / delivery challan for this sample has been issued and can be printed from the portal.", "Gate pass issued", "#0f766e"),
    "internally dispatched": ("#0f766e", "DISPATCHED INTERNALLY", "This internal sample has been dispatched. The internal flow raises no gate pass.", "Dispatched", "#0f766e"),
    "converted": ("#7c3aed", "CONVERTED TO EXTERNAL",     "This internal sample has been converted to an external issue; a gate pass now covers it.", "Converted", "#7c3aed"),
    "closed":    ("#4b5563", "SAMPLE REQUEST CLOSED",     "This sample request is closed. No further action is expected.", "Closed", "#4b5563"),
    "cancelled": ("#dc2626", "SAMPLE REQUEST CANCELLED",  "This sample request has been cancelled.", "Cancelled", "#dc2626"),
}


def _requisition_event_html(req: dict, event: str, reason=None,
                            link: str | None = None, link_label: str | None = None) -> str:
    """Button-less requisition notice for any lifecycle event. Carries the outcome reason
    when there is one, so a step is ONE mail in the trail rather than a detail card plus a
    separate one-line note. `link` renders a single NAVIGATION button (e.g. open the gate
    pass on the portal) — never a signed action URL, so it stays safe on the broadcast
    copy that reaches the whole trail."""
    hdr, eyebrow, intro, pill, pill_bg = _REQ_EVENT.get(
        event, ("#6b7280", f"SAMPLE REQUEST {(event or '').upper()}",
                "Sharing the details of this sample request for your visibility.",
                _fmt(event).upper() if event else "Update", "#6b7280"))
    note = _callout(reason, hdr)
    inner = (f'<tr><td style="padding:26px 26px 6px">{_pill(pill, pill_bg)}'
             f'<p style="margin:0 0 4px;font-size:{_T_LEAD}px;color:{_INK};line-height:1.6">{intro}</p>'
             f'{note}<div style="height:12px"></div>{_detail_table(req)}</td></tr>'
             f'{_buttons([(link_label or "Open on the portal", link, hdr)] if link else [])}')
    return _shell(hdr=hdr, eyebrow=eyebrow, title=_fmt(req.get("request_id")),
                  inner=inner, footer=_TRAIL_FOOTER)


# ── promote dual-approval gate ───────────────────────────────────────────────
# Both buttons hit GET /email/promote-action?dev_jc_id&approver_kind&status&email.
# Approve carries the recipient's email for the gate-match auth; Reject just needs
# the dev_jc_id (it redirects to the portal job-card page to capture a reason).
def _promote_approve_url(dev_jc_id, approver_kind: str, email: str) -> str:
    from app.modules.sample.services.email_link_token import sign
    base = Settings().PUBLIC_BACKEND_URL.rstrip("/")
    t = sign("promote", dev_jc_id, approver_kind, email)
    return (f"{base}/api/v1/sample/email/promote-action?dev_jc_id={dev_jc_id}"
            f"&approver_kind={approver_kind}&status=approve&email={quote(email)}&t={t}")


def _promote_reject_url(dev_jc_id, approver_kind: str, email: str) -> str:
    # Reject opens the job-card page on the web app, which pops a reason dialog and
    # submits the reject through the email-authenticated endpoint (the email is carried
    # so the submit can be authenticated — checking is mandatory).
    web = Settings().WEB_APP_URL.rstrip("/")
    return (f"{web}/modules/npd-development/job-cards/{dev_jc_id}"
            f"?promote_reject={approver_kind}&email={quote(email)}")


def _promote_detail_table(jc: dict) -> str:
    """Dev-JC detail grid for the promote / dispatch mails (mirrors _detail_table's look)."""
    exp = jc.get("expected_dispatch_date")
    exp = str(exp)[:10] if exp else "TBC"
    tq, uom = jc.get("target_qty"), (jc.get("uom") or "kg")
    fields = [
        ("Dev job card", _fmt(jc.get("id"))),
        ("Title", _fmt(jc.get("title"))),
        ("Target FG article", _fmt(jc.get("fg_sku_name") or jc.get("title"))),
        ("Target qty", f"{_fmt(tq)} {_fmt(uom)}" if tq is not None else "—", "num"),
        ("Warehouse", _fmt(jc.get("warehouse"))),
        ("Company", _fmt(jc.get("company_name"))),
        ("Customer", _fmt(jc.get("customer_name"))),
        ("Customer contact", _fmt(jc.get("customer_contact"))),
        ("Sales POC", _fmt(jc.get("sales_poc_name") or jc.get("sales_poc_email"))),
        ("Expected dispatch", _fmt(exp)),
        ("Return type", "Returnable" if jc.get("returnable")
                        else "Non-returnable" if jc.get("non_returnable") else "—"),
        ("Paid", "Yes" if jc.get("paid") else "No"),
        ("Amount", f"{float(jc.get('amount')):,.2f}"
                   if (jc.get("paid") and jc.get("amount") is not None) else "—", "num"),
    ]
    keys = [("Target qty", f"{_fmt(tq)} {_fmt(uom)}" if tq is not None else ""),
            ("Expected dispatch", _fmt(exp)),
            ("Warehouse", _fmt(jc.get("warehouse")))]
    return (_key_figures(keys) +
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'{_kv_rows(fields)}{_desc_block(jc.get("description"))}</table>')


def _promote_html(jc: dict, approver_label: str | None,
                  approve_url: str | None, reject_url: str | None) -> str:
    """The promote-approval card. With an approver_label + urls it carries Approve / Reject
    for THAT gate and goes to that approver alone. With None it is the same card, buttons
    stripped, for the button-less broadcast to the rest of the trail."""
    if approver_label and approve_url and reject_url:
        badge = ('<div style="margin:0 0 14px">'
                 '<span style="display:inline-block;background:#eef2ff;color:#4338ca;font-size:11px;'
                 'font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:4px 12px;'
                 f'border-radius:999px">Your gate: {_fmt(approver_label)}</span></div>')
        intro = ("A developed recipe is ready to be promoted into a live BOM. <b>Both</b> gates "
                 "— Inventory manager and Business head — must approve before it "
                 "goes live. Tap <b>Approve</b> to clear your gate, or <b>Reject</b> to open the "
                 "job card on the portal and record a reason.")
        pairs = [("&#10003;&nbsp; Approve", approve_url, "#16a34a"),
                 ("&#10007;&nbsp; Reject", reject_url, "#dc2626")]
        footer = _ACTION_FOOTER
    else:
        badge = _pill("Awaiting approval", "#4f46e5")
        intro = ("A developed recipe is ready to be promoted into a live BOM and is awaiting its "
                 "two approval gates — Inventory manager and Business head. Sharing "
                 "the details for your visibility; the outcome will follow in this trail.")
        pairs = []
        footer = _TRAIL_FOOTER
    inner = (f'<tr><td style="padding:26px 26px 6px">{badge}'
             f'<p style="margin:0 0 18px;font-size:{_T_LEAD}px;color:{_INK};line-height:1.6">{intro}</p>'
             f'{_promote_detail_table(jc)}</td></tr>{_buttons(pairs)}')
    return _shell(hdr="#4f46e5", eyebrow="PROMOTE APPROVAL NEEDED",
                  title=f"Dev JC {_fmt(jc.get('id'))}", inner=inner, footer=footer)


def _promote_status_html(jc: dict, *, gate: str, action: str, actor_name,
                         remarks, result: dict | None) -> str:
    """The promote-decision notice — button-less, replies into the trail. Fires on EVERY
    gate decision (both gates, accept and reject) from whichever channel recorded it:
    WhatsApp, the email button, or the in-app endpoint."""
    status = (result or {}).get("status")
    remaining = (result or {}).get("remaining")
    approved = (action or "").upper() in ("ACCEPT", "APPROVE")
    gate_label = _PROMOTE_GATE_LABEL.get((gate or "").upper()) or _fmt(gate)
    actor = _fmt(actor_name, "an approver")

    if not approved:
        hdr, eyebrow, pill, pill_bg = "#dc2626", "PROMOTE REJECTED", "Rejected", "#dc2626"
        intro = (f"The <b>{gate_label}</b> gate was <b>rejected</b> by {actor}. "
                 "The promote has been voided — the recipe was not promoted into a live BOM.")
    elif status == "PROMOTED":
        hdr, eyebrow, pill, pill_bg = "#16a34a", "PROMOTED", "Promoted", "#16a34a"
        intro = (f"The <b>{gate_label}</b> gate was <b>approved</b> by {actor}. Both gates are "
                 "now clear — the recipe has been promoted into a live BOM.")
    else:
        hdr, eyebrow, pill, pill_bg = "#4f46e5", "PROMOTE GATE APPROVED", "Awaiting approval", "#4f46e5"
        try:
            n = int(remaining) if remaining is not None else 0
        except (TypeError, ValueError):
            n = 0
        left = f"{n} gate{'' if n == 1 else 's'} still to approve" if n else "the remaining gate is still to approve"
        intro = (f"The <b>{gate_label}</b> gate was <b>approved</b> by {actor} — {left} "
                 "before the recipe is promoted.")

    note = _callout(remarks, hdr)
    inner = (f'<tr><td style="padding:26px 26px 6px">{_pill(pill, pill_bg)}'
             f'<p style="margin:0 0 4px;font-size:{_T_LEAD}px;color:{_INK};line-height:1.6">{intro}</p>'
             f'{note}<div style="height:12px"></div>{_promote_detail_table(jc)}</td></tr>')
    return _shell(hdr=hdr, eyebrow=eyebrow, title=f"Dev JC {_fmt(jc.get('id'))}",
                  inner=inner, footer=_TRAIL_FOOTER)


def _dispatch_html(jc: dict, *, dispatch_id, seq, qty, uom, recipient, actor_name,
                   dc_url: str) -> str:
    """The closing entry in the trail — the FG sample has gone out. Carries the dispatch
    summary in full text (so it stands alone) plus a link to the Delivery Challan + Gate
    Pass print page for this specific partial out."""
    fields = [
        ("Recipient", _fmt(recipient)),
        ("Dispatched by", _fmt(actor_name)),
        ("Dev job card", _fmt(jc.get("id"))),
        ("FG article", _fmt(jc.get("fg_sku_name") or jc.get("title"))),
        ("Sales POC", _fmt(jc.get("sales_poc_name") or jc.get("sales_poc_email"))),
        ("Warehouse", _fmt(jc.get("warehouse"))),
        ("Company", _fmt(jc.get("company_name"))),
        ("Customer", _fmt(jc.get("customer_name"))),
    ]
    inner = (
        f'<tr><td style="padding:26px 26px 6px">{_pill("Dispatched", "#0f766e")}'
        f'<p style="margin:0 0 18px;font-size:{_T_LEAD}px;color:{_INK};line-height:1.6">'
        'The developed FG sample has been dispatched. The Delivery Challan &amp; Gate Pass '
        'for this dispatch is available below.</p>'
        + _key_figures([("Dispatched qty", f"{_fmt(qty)} {_fmt(uom)}"),
                        ("Outpass no", _fmt(f"{jc.get('id')}-{seq}" if seq else dispatch_id))])
        + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{_kv_rows(fields)}</table>'
        '</td></tr>'
        f'{_buttons([("&#128196;&nbsp; Open Delivery Challan", dc_url, "#0f766e")])}')
    return _shell(hdr="#0f766e", eyebrow="SAMPLE DISPATCHED — DELIVERY CHALLAN",
                  title=f"Dev JC {_fmt(jc.get('id'))}", inner=inner,
                  footer="The Delivery Challan opens on the portal for printing / saving as PDF. "
                         "It is restricted to the NPD team and admins; the dispatch details above "
                         "are complete on their own.")


# ── public notifiers ─────────────────────────────────────────────────────────
async def notify_requisition_event(conn, req: dict, *, event: str, reason=None,
                                   link: str | None = None,
                                   link_label: str | None = None) -> None:
    """Button-less requisition notice to the whole trail — the ONE mail for this step, for
    EVERY sample type and every lifecycle state. event='created' ROOTS the thread
    (Message-ID = the transaction's thread key); everything else replies into it."""
    rid = req.get("request_id")
    rec = await resolve_recipients(conn, req)
    _broadcast(_thread_subject(rid, req.get("sample_type")),
               _requisition_event_html(req, event, reason, link, link_label), rec,
               thread=_thread_key(rid), root=(event == "created"),
               module=_module_for(req.get("sample_type")),
               entity_id=str(rid), event=event, status=event)


async def notify_npd_review_email(conn, req: dict) -> None:
    """The NPD review step: a buttoned Accept/Hold card to each npd_team reviewer addressed
    to them ALONE (their Accept link embeds their own email), plus the identical card with
    buttons stripped to everyone else on the trail. Both reply into the transaction thread.

    Also serves the hold→re-offer loop: on a recorded HOLD the same buttoned card is sent
    again as another reply, so the reviewer can Accept to end it or Hold again. Human-driven
    (one re-send per recorded hold), so there is no runaway loop. Best-effort, never raises."""
    rid = req.get("request_id")
    rec = await resolve_recipients(conn, req)
    thread, subject = _thread_key(rid), _thread_subject(rid, req.get("sample_type"))
    reviewers = rec["npd"]
    if not reviewers:
        logger.warning("[sample-mail] no npd_team emails — review mail has no approver for req %s",
                       req.get("id"))
    for em in reviewers:
        _send(subject, _review_html(req, em), [em], in_reply_to=thread)
    _broadcast(subject, _review_html(req, None), rec, thread=thread, exclude=reviewers)


async def notify_promote_review_email(conn, *, dev_jc_id, requestor_uid=None) -> None:
    """On a dev-JC promote request — a buttoned Approve/Reject card to each gate holder
    addressed to them ALONE (their Approve link carries their own email + gate), plus the
    identical card with buttons stripped to everyone else on the trail.

    Threads into the SOURCE REQUISITION's trail when the card came from one; a standalone
    card roots its own. Best-effort, never raises."""
    jc = await conn.fetchrow("SELECT * FROM npd_dev_job_cards WHERE id = $1", dev_jc_id)
    if jc is None:
        return
    thread, subject, src = await _jc_thread(conn, dev_jc_id)
    jc = _with_sales_poc(dict(jc), src)
    rec = await resolve_recipients(conn, src)

    gates: list[tuple[str, str, str]] = [
        (em, _PROMOTE_GATE_LABEL["INV_MGR"], "INV_MGR") for em in rec["inventory"]]
    if requestor_uid:
        bh = await _email_for_user(conn, requestor_uid)
        if bh:
            gates.append((bh, _PROMOTE_GATE_LABEL["REQUESTOR_BH"], "REQUESTOR_BH"))

    for em, label, kind in gates:
        _send(subject, _promote_html(jc, label,
                                     _promote_approve_url(dev_jc_id, kind, em),
                                     _promote_reject_url(dev_jc_id, kind, em)),
              [em], in_reply_to=thread)
    # A standalone card has no "created" mail to anchor it — its promote broadcast roots
    # the trail instead.
    _broadcast(subject, _promote_html(jc, None, None, None), rec, thread=thread,
               exclude=[e for e, _, _ in gates], root=(src is None))


async def notify_promote_status_email(conn, *, dev_jc_id, gate: str, action: str,
                                      actor_user_id=None, actor_name=None, remarks=None,
                                      result: dict | None = None) -> None:
    """The promote DECISION, as a button-less reply into the trail. Called from
    act_promote_approval — the single choke point every channel funnels through (WhatsApp,
    the email button, the in-app endpoint) — so a WhatsApp approval lands in the mail trail
    by construction. Best-effort, never raises."""
    jc = await conn.fetchrow("SELECT * FROM npd_dev_job_cards WHERE id = $1", dev_jc_id)
    if jc is None:
        return
    thread, subject, src = await _jc_thread(conn, dev_jc_id)
    rec = await resolve_recipients(conn, src)
    actor = await _name_for_user(conn, actor_user_id, actor_name)
    html = _promote_status_html(_with_sales_poc(dict(jc), src), gate=gate, action=action, actor_name=actor,
                                remarks=remarks, result=result)
    _broadcast(subject, html, rec, thread=thread)


async def notify_dev_dispatch_email(conn, *, dev_jc_id, dispatch_id=None, seq=None,
                                    qty=None, uom=None, recipient=None,
                                    actor_user_id=None, actor_name=None) -> None:
    """The closing entry in the trail — the FG sample has gone out, with a link to this
    dispatch's Delivery Challan + Gate Pass. Called from dispatch_dev_sample after commit.
    Best-effort, never raises."""
    jc = await conn.fetchrow("SELECT * FROM npd_dev_job_cards WHERE id = $1", dev_jc_id)
    if jc is None:
        return
    thread, subject, src = await _jc_thread(conn, dev_jc_id)
    rec = await resolve_recipients(conn, src)
    web = Settings().WEB_APP_URL.rstrip("/")
    dc_url = f"{web}/modules/npd-development/job-cards/{dev_jc_id}/gate-pass"
    if dispatch_id is not None:
        dc_url += f"?dispatch={dispatch_id}"
    actor = await _name_for_user(conn, actor_user_id, actor_name)
    html = _dispatch_html(_with_sales_poc(dict(jc), src), dispatch_id=dispatch_id, seq=seq, qty=qty, uom=uom,
                          recipient=recipient, actor_name=actor, dc_url=dc_url)
    _broadcast(subject, html, rec, thread=thread)


async def send_due_reminders(conn) -> int:
    """Reminder reply on the trail for NPD/TRIAL requests still SUBMITTED/ON_HOLD, capped at
    REMINDER_MAX and no sooner than REMINDER_MIN_HOURS apart. Returns # requests nudged.
    Meant to be run on a cron (the cadence guards make frequent calls safe no-ops).

    Only the buttoned copy goes out — a reminder is a nudge to the people who can act, so
    the rest of the trail is not re-mailed every 24h.

    A transaction-scoped advisory lock makes two concurrent sweeps mutually exclusive: a
    second caller that can't grab the lock returns 0 immediately, so the reminder_count
    never gets double-incremented by overlapping runs."""
    async with conn.transaction():
        got = await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", 8472013)
        if not got:
            return 0
        rows = await conn.fetch(
            """SELECT *
                 FROM sample_requisitions
                WHERE deleted_at IS NULL AND sample_type IN ('NPD','TRIAL')
                  AND status IN ('SUBMITTED','ON_HOLD')
                  AND reminder_count < $1
                  AND (last_reminder_at IS NULL OR last_reminder_at < NOW() - make_interval(hours => $2))""",
            REMINDER_MAX, REMINDER_MIN_HOURS)
        recips = _dedupe(await _emails_for_role(conn, "npd_team"))
        if not recips:
            return 0
        sent = 0
        for r in rows:
            rid = r["request_id"]
            subject, thread = _thread_subject(rid, r["sample_type"]), _thread_key(rid)
            for em in recips:
                _send(subject, _review_html(dict(r), em), [em], in_reply_to=thread)
            await conn.execute(
                "UPDATE sample_requisitions SET reminder_count = reminder_count + 1, last_reminder_at = NOW() WHERE id = $1",
                r["id"])
            sent += 1
        return sent
